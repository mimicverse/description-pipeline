"""Linked-run endpoint behavior on the native boundary: reuse, refusals, diagnostics."""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from pathlib import Path

from description_pipeline.io import PipelineError, write_json
from description_pipeline.orchestration.windows import Jobs, RequestError
from .endpoint_support import EndpointFixture

NATIVE_SCOPE = ("freeze", "discover", "capture")
LINUX_STAGES = ("generate", "verify", "publish")


class ResumeEndpointTests(EndpointFixture, unittest.TestCase):
    def await_terminal(self, jobs: Jobs, run_id: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = jobs.snapshot(run_id)
            if job["status"] in {"passed", "failed", "native_complete"}:
                return job
            time.sleep(0.01)
        raise AssertionError(f"{run_id} did not finish: {jobs.snapshot(run_id)['status']}")

    def native_parent(self) -> tuple[Jobs, dict]:
        jobs = self.jobs()
        request = self.request()
        _created, is_new = jobs.create(request)
        self.assertTrue(is_new)
        parent = self.await_terminal(jobs, request["run_id"])
        self.assertEqual(parent["status"], "native_complete", parent.get("error"))
        return jobs, parent

    @staticmethod
    def linked_request(parent: dict, stage: str) -> dict:
        return {
            "run_id": str(uuid.uuid4()),
            "package": parent["request"]["package"],
            "handoff_sha256": parent["request"]["handoff_sha256"],
            "resume": {"parent_run": parent["run_id"], "from_stage": stage},
        }

    def test_capture_resume_reuses_parent_checkpoints_with_provenance(self) -> None:
        jobs, parent = self.native_parent()
        jobs.close()
        calls: list[dict] = []

        def runner(package, output, **kwargs):
            calls.append({"package": package, **kwargs})
            return self.capture_transfer_result(output, run_id=kwargs["run_id"], on_event=kwargs["on_event"])

        child_jobs = self.jobs(runner, preparer=lambda *args, **kwargs: self.fail("discovery must not rerun"))
        request = self.linked_request(parent, "capture")
        child, created = child_jobs.create(request)
        self.assertTrue(created)
        inherited = [event for event in child["events"] if event.get("reuse")]
        self.assertTrue(inherited)
        self.assertEqual({event["reuse"]["parent_run"] for event in inherited}, {parent["run_id"]})
        parent_at = {event.get("at") for event in parent["events"]}
        for event in inherited:
            self.assertIn(event.get("at"), parent_at)
        finished = self.await_terminal(child_jobs, request["run_id"])
        self.assertEqual(finished["status"], "native_complete", finished.get("error"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["resume_from"], "capture")
        self.assertIsNone(calls[0]["seed_dir"])
        self.assertEqual(calls[0]["resume"], request["resume"])
        self.assertEqual(Path(calls[0]["package"]), self.config["state_root"] / "prepared" / parent["run_id"])
        reused_stages = {event["stage"] for event in inherited}
        self.assertEqual(reused_stages, {"freeze", "discover"})

    def test_linux_owned_stages_are_never_rerun_on_the_native_host(self) -> None:
        jobs, parent = self.native_parent()
        rows = {row["stage"]: row for row in jobs.plan(parent["run_id"])["stage_reruns"]}
        for stage in NATIVE_SCOPE:
            self.assertTrue(rows[stage]["eligible"], stage)
        for stage in LINUX_STAGES:
            self.assertFalse(rows[stage]["eligible"], stage)
            self.assertEqual(rows[stage]["reason"], "linux_owned")
            self.assertTrue(rows[stage]["reason_zh"])
            self.assertEqual(rows[stage]["recomputes"], [])
        for stage in LINUX_STAGES:
            with self.assertRaises(RequestError) as caught:
                jobs.create(self.linked_request(parent, stage))
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(caught.exception.payload["reason"], "linux_owned")
            self.assertTrue(caught.exception.payload["reason_zh"])
        with self.assertRaises(RequestError):
            jobs.preview(parent["run_id"])

    def test_dependency_change_blocks_reuse_only(self) -> None:
        registry = self.root / "frozen-names.json"
        write_json(registry, {})
        self.config["discovery"] = {"record_roots": [], "frozen_names_file": registry}
        jobs, parent = self.native_parent()
        write_json(registry, {"arm": "base_link"})
        rows = {row["stage"]: row for row in jobs.plan(parent["run_id"])["stage_reruns"]}
        self.assertTrue(rows["discover"]["eligible"])
        self.assertFalse(rows["capture"]["eligible"])
        self.assertEqual(rows["capture"]["reason"], "dependency_changed")
        self.assertEqual(rows["capture"]["prerequisites"]["earliest_required"], "discover")
        with self.assertRaises(RequestError) as caught:
            jobs.create(self.linked_request(parent, "capture"))
        self.assertEqual(caught.exception.payload["reason"], "dependency_changed")
        self.assertEqual(caught.exception.payload["earliest_required"], "discover")
        accepted = self.linked_request(parent, "discover")
        child, created = jobs.create(accepted)
        self.assertTrue(created)
        self.assertFalse(any(event.get("reuse") for event in child["events"] if event["stage"] == "discover"))
        finished = self.await_terminal(jobs, accepted["run_id"])
        self.assertEqual(finished["status"], "native_complete", finished.get("error"))

    def test_second_linked_job_is_refused_while_one_is_active(self) -> None:
        jobs, parent = self.native_parent()
        jobs.close()
        started, release = threading.Event(), threading.Event()

        def runner(package, output, **kwargs):
            if kwargs.get("resume") is not None:
                started.set()
                if not release.wait(10):
                    raise PipelineError("release timeout")
            return self.capture_transfer_result(output, run_id=kwargs["run_id"], on_event=kwargs["on_event"])

        child_jobs = self.jobs(runner)
        first = self.linked_request(parent, "capture")
        child_jobs.create(first)
        self.assertTrue(started.wait(5))
        second = self.linked_request(parent, "capture")
        with self.assertRaises(RequestError) as caught:
            child_jobs.create(second)
        self.assertEqual(caught.exception.payload["reason"], "already_active")
        self.assertEqual(caught.exception.payload["active_run_id"], first["run_id"])
        release.set()
        self.await_terminal(child_jobs, first["run_id"])
        third = self.linked_request(parent, "capture")
        _, created = child_jobs.create(third)
        self.assertTrue(created)
        self.await_terminal(child_jobs, third["run_id"])

    def test_failed_linked_job_preserves_the_cad_error_code(self) -> None:
        class CadFailure(PipelineError):
            code = "main_selection_ambiguous"

        jobs, parent = self.native_parent()
        jobs.close()

        def runner(package, output, **kwargs):
            raise CadFailure("saved main assembly is ambiguous")

        child_jobs = self.jobs(runner)
        request = self.linked_request(parent, "capture")
        child_jobs.create(request)
        finished = self.await_terminal(child_jobs, request["run_id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "main_selection_ambiguous")
        self.assertIn("ambiguous", finished["error"])

    def test_unrecorded_tool_identity_allows_only_a_freeze_restart(self) -> None:
        jobs, parent = self.native_parent()
        jobs.close()
        job_path = self.config["state_root"] / "jobs" / (parent["run_id"] + ".json")
        import json

        stored = json.loads(job_path.read_text(encoding="utf-8"))
        stored.pop("tool", None)
        job_path.write_text(json.dumps(stored), encoding="utf-8")
        resumed = self.jobs()
        rows = {row["stage"]: row for row in resumed.plan(parent["run_id"])["stage_reruns"]}
        self.assertFalse(rows["capture"]["eligible"])
        self.assertEqual(rows["capture"]["reason"], "tool_changed")
        self.assertEqual(rows["capture"]["prerequisites"]["earliest_required"], "freeze")
        self.assertTrue(rows["freeze"]["eligible"])
        with self.assertRaises(RequestError) as caught:
            resumed.create(self.linked_request(parent, "capture"))
        self.assertEqual(caught.exception.payload["reason"], "tool_changed")
        accepted = self.linked_request(parent, "freeze")
        _, created = resumed.create(accepted)
        self.assertTrue(created)
        finished = self.await_terminal(resumed, accepted["run_id"])
        self.assertEqual(finished["status"], "native_complete", finished.get("error"))

    def test_stale_enqueue_state_is_reprobed_before_reuse(self) -> None:
        jobs, parent = self.native_parent()
        jobs.close()
        request = self.linked_request(parent, "capture")
        package = self.packages / request["package"]
        from description_pipeline.sources.solidworks.revision import package_inventory

        stored = {
            "schema_version": "solidworks-to-urdf.job/v1",
            "pipeline_id": "solidworks-to-urdf",
            "run_id": request["run_id"],
            "request": request,
            "package_files": package_inventory(package),
            "status": "queued",
            "events": [],
            "result": None,
            "error": None,
        }
        write_json(self.config["state_root"] / "jobs" / (request["run_id"] + ".json"), stored)
        prepared = Path(parent["prepared_dir"])
        self.assertTrue(prepared.is_dir())
        (prepared / "robot.yaml").write_text("corrupted after enqueue\n", encoding="utf-8")
        resumed = self.jobs(lambda *args, **kwargs: self.fail("Re-probe must refuse before running"))
        finished = self.await_terminal(resumed, request["run_id"])
        self.assertEqual(finished["status"], "failed")
        self.assertIn("重新开始", finished["error"])


if __name__ == "__main__":
    unittest.main()
