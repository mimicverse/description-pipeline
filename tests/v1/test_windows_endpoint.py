"""Real HTTP and persistent ownership tests; mocked runner never qualifies CAD."""

from __future__ import annotations

import json
import copy
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from description_pipeline.io import PipelineError, write_json
from description_pipeline.orchestration.airflow_client import EndpointConfig, HandoffResolution, WindowsEndpoint
from description_pipeline.orchestration.windows import Jobs, RequestError, handler, read_config
from .endpoint_support import EndpointFixture


class EndpointTests(EndpointFixture, unittest.TestCase):
    def test_airflow_client_can_poll_native_discovery_and_completed_events(self):
        preparing, release = threading.Event(), threading.Event()
        captured = {"stage": "capture", "state": "completed", "at": "2026-01-01T00:00:00+00:00"}

        def prepare(*args, **kwargs):
            preparing.set()
            if not release.wait(5):
                raise RuntimeError("Discovery control was not released")
            return self.prepare(*args, **kwargs)

        def runner(*args, **kwargs):
            kwargs["on_event"](captured)
            return self.capture_transfer_result(args[1], run_id=kwargs["run_id"], on_event=kwargs["on_event"])

        jobs = self.jobs(runner, preparer=prepare)
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
        try:
            running = client.get_job(request["run_id"])
            self.assertEqual("running", running["status"])
            self.assertEqual(("freeze", "running"), (running["events"][0]["stage"], running["events"][0]["state"]))
            stage_states = {stage["id"]: stage["state"] for stage in running["stages"]["stages"]}
            self.assertEqual(stage_states["freeze"], "completed")
            self.assertEqual(stage_states["discover"], "running")
            self.assertIsNotNone(datetime.fromisoformat(running["events"][0]["at"]).tzinfo)
        finally:
            release.set()
        jobs.queue.join()
        completed = client.get_job(request["run_id"])
        self.assertEqual("native_complete", completed["status"])
        self.assertIn(captured, completed["events"])
        self.assertTrue(all(datetime.fromisoformat(event["at"]).tzinfo is not None for event in completed["events"]))

    def test_idempotent_retry_is_bound_to_exact_request_and_survives_restart(self):
        calls = []

        def runner(package, output, **kwargs):
            calls.append(kwargs["run_id"])
            return self.capture_transfer_result(output, run_id=kwargs["run_id"], on_event=kwargs["on_event"])

        jobs = Jobs(self.config, runner=runner, native_preparer=self.prepare)
        request = self.request()
        jobs.create(request)
        jobs.queue.join()
        result, created = jobs.create(request)
        self.assertFalse(created)
        self.assertEqual("native_complete", result["status"])
        self.assertEqual(1, len(calls))
        with self.assertRaises(RequestError) as error:
            jobs.create({**request, "handoff_sha256": "f" * 64})
        self.assertEqual(409, error.exception.status)
        jobs.close()
        resumed = self.jobs(runner)
        self.assertEqual("native_complete", resumed.snapshot(request["run_id"])["status"])
        self.assertFalse(resumed.create(request)[1])
        self.assertEqual(1, len(calls))

    def test_native_or_publication_failure_never_becomes_passing(self):
        jobs = self.jobs(lambda *args, **kwargs: {"passed": True})
        request = self.request()
        jobs.create(request)
        jobs.queue.join()
        self.assertEqual("failed", jobs.snapshot(request["run_id"])["status"])

    def test_green_result_without_a_sealed_transfer_is_rejected(self):
        jobs = self.jobs(lambda *args, **kwargs: self.passing_result())
        request = self.request()
        jobs.create(request)
        jobs.queue.join()
        result = jobs.snapshot(request["run_id"])
        self.assertEqual("failed", result["status"])
        self.assertIn("sealed capture transfer", result["error"])

    def test_discovery_failure_retains_diagnostics_across_endpoint_restart(self):
        from description_pipeline.sources.solidworks.errors import CadError
        from description_pipeline.verification.consumer import ConsumerError

        def runner(*args, **kwargs):
            self.fail("Capture or publication ran after discovery failed")

        for error, detail in (
            (
                ConsumerError(
                    "Consumer loading failed", returncode=1, stderr="ImportError: native library unavailable"
                ),
                {"returncode": 1, "stderr": "ImportError: native library unavailable"},
            ),
            (
                CadError("cad_read_failed", "Native read failed", detail={"phase": "read", "cause": "lost binding"}),
                {"phase": "read", "cause": "lost binding"},
            ),
        ):
            with self.subTest(error=type(error).__name__):

                def prepare(*args, error=error, **kwargs):
                    raise error

                jobs = Jobs(self.config, runner=runner, native_preparer=prepare)
                try:
                    request = self.request()
                    jobs.create(request)
                    jobs.queue.join()
                    result = jobs.snapshot(request["run_id"])
                    self.assertEqual("failed", result["status"])
                    self.assertEqual(f"{type(error).__name__}: {error}", result["error"])
                    self.assertEqual(detail, result["detail"])
                finally:
                    jobs.close()
                recovered = Jobs(self.config, runner=runner, native_preparer=self.prepare)
                try:
                    self.assertEqual(result, recovered.snapshot(request["run_id"]))
                    self.assertFalse(recovered.create(request)[1])
                finally:
                    recovered.close()

    def test_wrong_repository_base_subject_or_quality_cannot_pass(self):
        responses = []
        for section, key, value in (
            ("submission", "repository_slug", "a/other"),
            ("submission", "url", "https://github.com/a/other/pull/1"),
            ("submission", "base", "feature/other"),
            ("submission", "branch", "work/solidworks/other"),
            ("submission", "subject_sha256", "c" * 64),
            ("quality", "passed", False),
            ("quality", "subject_sha256", "c" * 64),
            ("submission", "commit", ""),
        ):
            result = copy.deepcopy(self.passing_result())
            result[section][key] = value
            responses.append(result)
        jobs = self.jobs(lambda *args, **kwargs: responses.pop(0))
        for _ in range(len(responses)):
            request = self.request()
            jobs.create(request)
            jobs.queue.join()
            result = jobs.snapshot(request["run_id"])
            self.assertEqual("failed", result["status"])
            self.assertTrue(result["error"])

    def test_queued_restart_rechecks_frozen_native_bytes(self):
        jobs = Jobs(self.config, runner=lambda *a, **k: self.fail("Unexpected job"), native_preparer=self.prepare)
        jobs.close()
        for changed in (False, True):
            request = self.request()
            package = self.packages / request["package"]
            from description_pipeline.sources.solidworks.revision import package_inventory

            saved = {
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
            if changed:
                (package / "总装.SLDASM").write_bytes(b"Changed while offline")
            state = self.config["state_root"] / "jobs" / (request["run_id"] + ".json")
            write_json(state, saved)
            calls = []

            def runner(*a, calls=calls, **k):
                calls.append(k["run_id"])
                return self.capture_transfer_result(a[1], run_id=k["run_id"], on_event=k["on_event"])

            resumed = Jobs(self.config, runner=runner, native_preparer=self.prepare)
            resumed.queue.join()
            result = resumed.snapshot(request["run_id"])
            self.assertEqual("failed" if changed else "native_complete", result["status"])
            self.assertEqual([] if changed else [request["run_id"]], calls)
            resumed.close()
            recovered = Jobs(self.config, runner=lambda *a, **k: self.fail("Completed job reran"))
            self.assertEqual(result["status"], recovered.snapshot(request["run_id"])["status"])
            recovered.close()

    def test_native_jobs_are_serial(self):
        active, peak = 0, 0

        def runner(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.02)
            active -= 1
            return {"passed": False, "error": "expected control-test failure"}

        jobs = self.jobs(runner)
        for _ in range(3):
            jobs.create(self.request())
        jobs.queue.join()
        self.assertEqual(1, peak)

    def test_extra_fields_path_escape_and_changed_handoff_are_rejected(self):
        jobs = self.jobs(lambda *args, **kwargs: self.fail("Unvalidated request ran"))
        for mutation in (
            {"target": "unknown"},
            {"package": "../arm"},
            {"handoff_sha256": "0" * 64},
            {"command": "anything"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(PipelineError):
                jobs.create({**self.request(), **mutation})

    def test_http_authentication_and_request_contract(self):
        jobs = self.jobs(lambda *args, **kwargs: {"passed": False, "error": "expected"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(jobs, self.config["token"]))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        address = f"http://127.0.0.1:{server.server_port}"
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(address + "/health", timeout=2)
        self.assertEqual(401, error.exception.code)
        request = self.request()
        headers = {"Authorization": "Bearer " + self.config["token"], "Content-Type": "application/json"}
        with urllib.request.urlopen(
            urllib.request.Request(address + "/v1/jobs", data=json.dumps(request).encode(), headers=headers), timeout=2
        ) as response:
            self.assertEqual(202, response.status)
            self.assertEqual(request["run_id"], json.load(response)["run_id"])
        jobs.queue.join()
        with urllib.request.urlopen(
            urllib.request.Request(address + "/v1/jobs/" + request["run_id"], headers=headers), timeout=2
        ) as response:
            self.assertEqual("failed", json.load(response)["status"])

    def test_second_endpoint_cannot_acquire_same_state(self):
        self.jobs(lambda *args, **kwargs: {"passed": False})
        with self.assertRaises(PipelineError):
            Jobs(self.config)

    def test_process_exit_releases_ownership_and_fails_interrupted_job(self):
        request = self.request()
        # A real child process exits from the running job without Jobs.close().
        # This exercises kernel lock release, rather than deleting a lock file.
        script = (
            "import os,sys;from pathlib import Path;"
            f"sys.path.insert(0,{str(Path(__file__).resolve().parents[2] / 'src')!r});"
            f"sys.path.insert(0,{str(Path(__file__).resolve().parents[2])!r});"
            "from tests.v1.endpoint_support import prepare_control;"
            "from unittest.mock import patch;"
            "patch('description_pipeline.orchestration.windows._native_tool_record',"
            "return_value={'name':'native','version':'1.3.1','runtime':{'role':'native'}}).start();"
            "from description_pipeline.orchestration.windows import Jobs,read_config;"
            "j=Jobs(read_config(Path(sys.argv[1])),native_preparer=prepare_control,runner=lambda *a,**k:os._exit(17));"
            f"j.create({request!r});j.queue.join()"
        )
        stopped = subprocess.run([sys.executable, "-I", "-c", script, str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(17, stopped.returncode, stopped.stderr.decode())
        resumed = self.jobs(lambda *a, **k: self.fail("Interrupted CAD job must not restart"))
        resumed.queue.join()
        job = resumed.snapshot(request["run_id"])
        self.assertEqual("failed", job["status"])
        self.assertIn("Endpoint restarted during native execution", job["error"])
        self.assertFalse(resumed.create(request)[1])

    def test_plaintext_remote_binding_and_overlapping_roots_are_rejected(self):
        for change in (
            {"host": "0.0.0.0"},
            {"output_root": str(self.packages / "output")},
            {"handoff_roots": []},
            {"handoff_roots": [str(self.root.anchor)]},
            {"handoff_roots": [str(self.packages)]},
            {"handoff_roots": [str(self.root)]},
        ):
            write_json(self.path, {**self.config_data, **change})
            with self.assertRaises(PipelineError):
                read_config(self.path)
