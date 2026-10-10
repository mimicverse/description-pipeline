"""Live-activity plumbing: contract shape, persistence, exposure and isolation.

Activity is observation only: it never replaces checks, never fabricates counts
or timestamps, never fails a run and never reports an unavailable stage as if
it were reporting work.
"""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from description_pipeline.orchestration import portal as portal_module
from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    EndpointProtocolError,
    HandoffResolution,
    WindowsEndpoint,
    _validate_activity,
)
from description_pipeline.orchestration.linux_store import LinuxStore
from description_pipeline.orchestration.windows import handler
from description_pipeline.sources.solidworks.discovery import _guarded_sink
from description_pipeline.stages import activity_view, merge_activity, normalize_activity
from .endpoint_support import EndpointFixture


class ActivityContractTests(unittest.TestCase):
    def test_normalize_accepts_a_full_record_and_ignores_extras(self):
        record = normalize_activity(
            {
                "phase": "discover",
                "action": "read_components",
                "params": {"document": "robot.SLDASM", "count": 3, "nested": True},
                "current_object": "sub/arm.SLDPRT",
                "completed": 12,
                "total": 30,
                "unit": "instances",
                "at": "emitter-must-not-set",
            }
        )
        self.assertEqual(record["phase"], "discover")
        self.assertEqual(record["current_object"], "sub/arm.SLDPRT")
        self.assertEqual((record["completed"], record["total"], record["unit"]), (12, 30, "instances"))
        self.assertNotIn("at", record)

    def test_normalize_rejects_unusable_records_without_defaulting(self):
        bad = [
            {"phase": "discover"},
            {"phase": "unknown", "action": "read"},
            {"phase": "discover", "action": "Read"},
            {"phase": "discover", "action": "read", "current_object": "/etc/passwd"},
            {"phase": "discover", "action": "read", "current_object": "a/../b"},
            {"phase": "discover", "action": "read", "current_object": "C:/x"},
            {"phase": "discover", "action": "read", "completed": 5},
            {"phase": "discover", "action": "read", "completed": 5, "total": 3},
            {"phase": "discover", "action": "read", "completed": True, "total": 3},
            {"phase": "discover", "action": "read", "params": {"BadKey": 1}},
            {"phase": "discover", "action": "read", "unit": "Instance"},
        ]
        for record in bad:
            with self.subTest(record=record):
                self.assertIsNone(normalize_activity(record))

    def test_merge_replaces_observed_fields_and_keeps_the_run_identity(self):
        first = normalize_activity({"phase": "discover", "action": "read_components", "completed": 5, "total": 10})
        merged, history = merge_activity(None, first, at="t1")
        self.assertEqual((merged["started_at"], merged["seq"]), ("t1", 1))
        self.assertEqual(history["code"], "discover.read_components")

        moved = normalize_activity({"phase": "discover", "action": "read_components", "current_object": "arm.SLDPRT"})
        merged2, history2 = merge_activity(merged, moved, at="t2")
        self.assertIsNone(history2)
        self.assertEqual((merged2["started_at"], merged2["seq"]), ("t1", 2))
        self.assertNotIn("completed", merged2)  # omitted fields are cleared, never carried over
        self.assertEqual(merged2["current_object"], "arm.SLDPRT")

        reset = normalize_activity({"phase": "discover", "action": "read_components", "completed": 2, "total": 3})
        merged3, _ = merge_activity(merged2, reset, at="t3")
        self.assertEqual((merged3["completed"], merged3["total"]), (2, 3))  # a reset is reported, never dropped

        grown = normalize_activity({"phase": "discover", "action": "read_components", "completed": 6, "total": 12})
        merged4, _ = merge_activity(merged3, grown, at="t4")
        self.assertEqual((merged4["completed"], merged4["total"]), (6, 12))

        new_run, history4 = merge_activity(
            merged4, normalize_activity({"phase": "discover", "action": "read_mates"}), at="t5"
        )
        self.assertEqual((new_run["seq"], new_run["started_at"]), (1, "t5"))
        self.assertNotIn("completed", new_run)
        self.assertEqual(history4["code"], "discover.read_mates")

    def test_activity_view_reports_availability_and_the_true_stage_start(self):
        empty = activity_view(None)
        self.assertEqual((empty["available"], empty["state"]), (False, "none"))
        queued = activity_view({"status": "queued"})
        self.assertEqual((queued["available"], queued["state"]), (False, "queued"))
        waiting = activity_view({"status": "running", "events": [{"stage": "discover", "state": "running"}]})
        self.assertEqual((waiting["available"], waiting["state"]), (False, "waiting"))  # honest no-telemetry state

        busy = activity_view(
            {
                "status": "running",
                "events": [{"stage": "discover", "state": "running", "at": "stage-t0"}],
                "activity": {
                    "phase": "discover",
                    "action": "read_components",
                    "current_object": "arm.SLDPRT",
                    "completed": 3,
                    "total": 30,
                    "unit": "instances",
                    "params": {"document": "robot.SLDASM"},
                    "started_at": "t1",
                    "updated_at": "t2",
                },
                "activity_history": [
                    {"at": f"t{i}", "code": "discover.read_components", "object": None} for i in range(1, 9)
                ],
            }
        )
        self.assertEqual((busy["available"], busy["state"]), (True, "busy"))
        self.assertEqual(busy["stage_started_at"], "stage-t0")  # true stage start, not the action start
        self.assertEqual(busy["action_started_at"], "t1")
        self.assertEqual(busy["updated_at"], "t2")
        self.assertEqual(busy["action"], {"code": "discover.read_components", "params": {"document": "robot.SLDASM"}})
        self.assertEqual(busy["counts"], {"done": 3, "total": 30, "unit": "instances"})
        self.assertEqual(len(busy["recent"]), 5)
        self.assertEqual(busy["recent"][0]["at"], "t8")  # newest first

        for status in ("passed", "failed", "native_complete"):
            finished = activity_view(
                {
                    "status": status,
                    "events": [{"stage": "discover", "state": "running", "at": "stage-t0"}],
                    "activity": None,
                    "activity_final": {
                        "phase": "discover",
                        "action": "read_mates",
                        "current_object": "arm.SLDPRT",
                        "completed": 5,
                        "total": 6,
                        "started_at": "t1",
                        "updated_at": "t7",
                    },
                    "activity_history": [{"at": "t1", "code": "discover.read_components", "object": None}],
                }
            )
            self.assertEqual((finished["available"], finished["state"]), (True, "finished"))
            self.assertEqual(finished["updated_at"], "t7")  # the last meaningful observation survives
            self.assertEqual(finished["object"], "arm.SLDPRT")
            self.assertEqual(finished["counts"], {"done": 5, "total": 6})
            self.assertEqual(finished["stage_started_at"], "stage-t0")
            self.assertTrue(finished["recent"])
        legacy = activity_view({"status": "failed"})
        # A record-less terminal stays hidden: no telemetry was ever recorded.
        self.assertEqual((legacy["available"], legacy["state"]), (False, "finished"))


class ActivitySinkGuardTests(unittest.TestCase):
    def test_guard_swallows_observer_failures_and_passes_none_through(self):
        self.assertIsNone(_guarded_sink(None))
        received = []
        _guarded_sink(received.append)({"phase": "discover", "action": "read_components"})
        self.assertEqual(received[0]["action"], "read_components")

        def boom(record):
            raise RuntimeError("observer failed")

        _guarded_sink(boom)({"phase": "discover", "action": "read_components"})  # swallowed, never raised


class EndpointActivityTests(EndpointFixture, unittest.TestCase):
    def test_activity_persists_while_blocking_and_keeps_the_final_observation(self):
        preparing, release = threading.Event(), threading.Event()
        seen = {}

        def prepare(*args, **kwargs):
            sink = kwargs.get("on_activity")
            self.assertIsNotNone(sink)
            preparing.set()
            sink(
                {
                    "phase": "discover",
                    "action": "read_components",
                    "current_object": "arm.SLDPRT",
                    "completed": 1,
                    "total": 4,
                }
            )
            sink({"phase": "discover", "action": "read_components", "current_object": "/etc/passwd"})
            sink({"phase": "nonsense", "action": "read_components"})
            sink({"phase": "discover", "action": "read_mates", "completed": 2, "total": 6})
            if not release.wait(5):
                raise RuntimeError("test did not release the preparer")
            seen["called"] = True
            return self.prepare(*args, **kwargs)

        jobs = self.jobs(
            runner=lambda *a, **k: self.capture_transfer_result(a[1], run_id=k["run_id"], on_event=k["on_event"]),
            preparer=prepare,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(jobs, self.config["token"]))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        client = WindowsEndpoint(
            EndpointConfig(f"http://127.0.0.1:{server.server_port}", self.config["token"], timeout=2)
        )
        request = self.request()
        client.start_job(
            run_id=request["run_id"],
            resolution=HandoffResolution(request["package"], request["handoff_sha256"]),
        )
        self.assertTrue(preparing.wait(2))
        running = client.get_job(request["run_id"])
        self.assertEqual(running["status"], "running")
        self.assertEqual((running["activity"]["phase"], running["activity"]["action"]), ("discover", "read_mates"))
        self.assertEqual(running["activity"]["started_at"], running["activity"]["updated_at"])
        self.assertEqual(
            [entry["code"] for entry in running["activity_history"]],
            ["discover.read_components", "discover.read_mates"],
        )
        release.set()
        deadline = time.time() + 5
        final = running
        while time.time() < deadline:
            final = client.get_job(request["run_id"])
            if final["status"] == "native_complete":
                break
            time.sleep(0.05)
        self.assertEqual(final["status"], "native_complete")
        self.assertTrue(seen.get("called"))
        self.assertIsNone(final["activity"])
        self.assertEqual(
            (final["activity_final"]["phase"], final["activity_final"]["action"]), ("discover", "read_mates")
        )
        self.assertEqual(
            [entry["code"] for entry in final["activity_history"]],
            ["discover.read_components", "discover.read_mates"],
        )
        view = activity_view(final)
        self.assertEqual((view["available"], view["state"]), (True, "finished"))
        self.assertEqual(view["counts"], {"done": 2, "total": 6})
        self.assertTrue(view["recent"])

    def test_stale_discovery_activity_is_retired_when_the_stage_ends(self):
        capture_running, capture_release = threading.Event(), threading.Event()

        def prepare(*args, **kwargs):
            sink = kwargs.get("on_activity")
            sink({"phase": "discover", "action": "build_record", "params": {"components": 126}})
            return self.prepare(*args, **kwargs)

        def runner(*args, **kwargs):
            capture_running.set()
            if not capture_release.wait(5):
                raise RuntimeError("test did not release the capture runner")
            return self.capture_transfer_result(args[1], run_id=kwargs["run_id"], on_event=kwargs["on_event"])

        jobs = self.jobs(runner=runner, preparer=prepare)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(jobs, self.config["token"]))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(capture_release.set)
        client = WindowsEndpoint(
            EndpointConfig(f"http://127.0.0.1:{server.server_port}", self.config["token"], timeout=2)
        )
        request = self.request()
        client.start_job(
            run_id=request["run_id"],
            resolution=HandoffResolution(request["package"], request["handoff_sha256"]),
        )
        self.assertTrue(capture_running.wait(2))
        snapshot = client.get_job(request["run_id"])
        self.assertEqual(snapshot["status"], "running")
        self.assertIsNone(snapshot["activity"])  # never busy with a stale discovery action
        self.assertEqual(snapshot["activity_final"]["action"], "build_record")
        view = activity_view(snapshot)
        self.assertEqual((view["available"], view["state"]), (False, "waiting"))
        capture_release.set()
        deadline = time.time() + 5
        final = snapshot
        while time.time() < deadline:
            final = client.get_job(request["run_id"])
            if final["status"] == "native_complete":
                break
            time.sleep(0.05)
        self.assertEqual(final["status"], "native_complete")
        self.assertIsNone(final["activity"])
        self.assertEqual(activity_view(final)["state"], "finished")


class ActivityClientValidationTests(unittest.TestCase):
    @staticmethod
    def _valid():
        return {
            "phase": "discover",
            "action": "read_components",
            "at": "t",
            "started_at": "t",
            "updated_at": "t",
            "completed": 1,
            "total": 2,
        }

    def test_accepts_absent_and_valid_blocks(self):
        _validate_activity({})
        _validate_activity(
            {
                "activity": self._valid(),
                "activity_final": self._valid(),
                "activity_history": [{"at": "t", "code": "discover.read_components"}],
            }
        )

    def test_rejects_malformed_blocks(self):
        malformed = [
            {"activity": []},
            {"activity": {"phase": "discover"}},
            {"activity": {**self._valid(), "phase": "nope"}},
            {"activity": {**self._valid(), "updated_at": None}},
            {"activity": {**self._valid(), "completed": 3}},
            {"activity_final": {"phase": "discover"}},
            {"activity_history": {}},
            {"activity_history": [{"code": "x"}]},
        ]
        for job in malformed:
            with self.subTest(job=job), self.assertRaises(EndpointProtocolError):
                _validate_activity(job)


class PortalActivityExposureTests(unittest.TestCase):
    def test_job_snapshot_keeps_activity_and_the_final_observation(self):
        with TemporaryDirectory() as tmp:
            store = LinuxStore(Path(tmp) / "store")
            run_id = str(uuid.uuid4())
            store.update_meta(run_id, state="running", handoff_sha256="b" * 64)
            app = portal_module.PortalApp(
                portal_module.PortalConfig(airflow=object(), endpoint=lambda: None, pipeline_store_root=store.root)
            )
            native = {
                "status": "running",
                "events": [],
                "activity": {
                    "phase": "discover",
                    "action": "read_mates",
                    "started_at": "t1",
                    "updated_at": "t2",
                },
                "activity_final": {
                    "phase": "discover",
                    "action": "read_components",
                    "started_at": "t0",
                    "updated_at": "t0",
                },
                "activity_history": [{"at": "t1", "code": "discover.read_components", "object": None}],
            }

            class Endpoint:
                def get_job(self, identifier):
                    return native

            snapshot = app._job_snapshot(run_id, Endpoint())
            self.assertEqual(snapshot["activity"]["action"], "read_mates")
            self.assertEqual(snapshot["activity_final"]["action"], "read_components")
            self.assertEqual(snapshot["activity_history"][0]["code"], "discover.read_components")
            view = activity_view(snapshot)
            self.assertEqual((view["available"], view["state"], view["stage"]), (True, "busy", "discover"))


if __name__ == "__main__":
    unittest.main()
