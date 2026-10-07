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
from http.server import ThreadingHTTPServer
from pathlib import Path

from description_pipeline.io import PipelineError, write_json
from description_pipeline.orchestration.windows import Jobs, RequestError, handler, read_config
from .endpoint_support import EndpointFixture


class EndpointTests(EndpointFixture, unittest.TestCase):
    def test_idempotent_retry_is_bound_to_exact_request_and_survives_restart(self):
        calls = []

        def runner(package, output, **kwargs):
            calls.append(kwargs["run_id"])
            return self.passing_result()

        jobs = Jobs(self.config, runner=runner, native_preparer=self.prepare)
        request = self.request()
        jobs.create(request)
        jobs.queue.join()
        result, created = jobs.create(request)
        self.assertFalse(created)
        self.assertEqual("passed", result["status"])
        self.assertEqual(1, len(calls))
        with self.assertRaises(RequestError) as error:
            jobs.create({**request, "handoff_sha256": "f" * 64})
        self.assertEqual(409, error.exception.status)
        jobs.close()
        resumed = self.jobs(runner)
        self.assertEqual("passed", resumed.snapshot(request["run_id"])["status"])
        self.assertFalse(resumed.create(request)[1])
        self.assertEqual(1, len(calls))

    def test_native_or_publication_failure_never_becomes_passing(self):
        jobs = self.jobs(lambda *args, **kwargs: {"passed": True})
        request = self.request()
        jobs.create(request)
        jobs.queue.join()
        self.assertEqual("failed", jobs.snapshot(request["run_id"])["status"])

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
                return self.passing_result()

            resumed = Jobs(self.config, runner=runner, native_preparer=self.prepare)
            resumed.queue.join()
            result = resumed.snapshot(request["run_id"])
            self.assertEqual("failed" if changed else "passed", result["status"])
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
