"""Real HTTP, ownership and restart tests; mocked native legs never qualify CAD."""

from __future__ import annotations

import copy
import json
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
from .endpoint_support import MAIN, TOOL, EndpointFixture


class _DelayedWorkerMutex:
    """Wrap ``Jobs.mutex`` and make the queue worker sleep before it acquires.

    Injected by ``test_terminal_publication_is_observably_atomic`` only: the
    lock stays free during the sleep, so a reader can always snapshot the
    intermediate record if the producer publishes a terminal status before the
    rest of its completion record (the pre-fix release/reacquire race).
    """

    def __init__(self, inner, worker, delay):
        self.inner = inner
        self.worker = worker
        self.delay = delay

    def __enter__(self):
        if threading.current_thread() is self.worker:
            time.sleep(self.delay)
        self.inner.acquire()
        return self

    def __exit__(self, *exc):
        self.inner.release()
        return False


class EndpointTests(EndpointFixture, unittest.TestCase):
    def test_selection_flow_reaches_a_sealed_capture_transfer(self):
        calls: list[dict] = []

        def runner(package, output, **kwargs):
            calls.append({"package": Path(package), **kwargs})
            self.assertEqual("capture", kwargs["stop_after"])
            self.assertEqual(MAIN, kwargs["main_assembly"])
            self.assertTrue(Path(package).is_dir())
            return self.capture_transfer_result(output, run_id=kwargs["run_id"], on_event=kwargs.get("on_event"))

        jobs = self.jobs(runner)
        request = self.request()
        created, is_new = jobs.create(request)
        self.assertTrue(is_new)
        self.assertEqual("selecting", created["status"])
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        self.assertNotIn("failure_diagnostics", finished)
        self.assertEqual(1, len(calls))
        self.assertEqual(calls[0]["main_assembly"], MAIN)
        self.assertEqual(set(finished["capture_archive"]), {"name", "sha256", "size", "manifest_sha256"})
        # The operator request binding stays exactly as submitted.
        self.assertEqual(request, finished["request"])
        self.assertEqual(MAIN, finished["main_assembly"])

    def test_missing_publication_target_does_not_stop_native_capture(self):
        config = self.config_for(
            "publication", targets={"another_robot": {"repository_slug": "a/c", "base": "feature/another_robot"}}
        )
        jobs = self.jobs(config=config)
        request = self.request()
        jobs.create(request)
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        self.assertIsNone(finished["repository_slug"])
        self.assertIsNone(finished["repository_base"])

    def test_blocking_native_reader_keeps_the_run_pollable(self):
        started, release = threading.Event(), threading.Event()

        def native_reader(frozen, settings, on_activity=None):
            started.set()
            if not release.wait(10):
                raise RuntimeError("native reader control was not released")
            return copy.deepcopy(self.record)

        jobs = self.jobs(native_reader=native_reader)
        self.addCleanup(release.set)
        request = self.request()
        jobs.create(request)
        self.stage_and_seal(jobs, request["run_id"])
        self.assertTrue(started.wait(5))
        running = jobs.snapshot(request["run_id"])
        self.assertEqual("running", running["status"])
        self.assertEqual("sealed", running["selection"]["state"])
        release.set()
        self.wait_state_any_status(jobs, request["run_id"], {"awaiting_definition"})
        self.complete_definition(jobs, request["run_id"])
        finished = self.await_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))

    def test_idempotent_retry_is_bound_to_exact_request_and_survives_restart(self):
        calls: list[str] = []

        def runner(package, output, **kwargs):
            calls.append(kwargs["run_id"])
            return self.capture_transfer_result(output, run_id=kwargs["run_id"], on_event=kwargs.get("on_event"))

        jobs = self.jobs(runner)
        request = self.request()
        jobs.create(request)
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        result, created = jobs.create(request)
        self.assertFalse(created)
        self.assertEqual("native_complete", result["status"])
        self.assertEqual(1, len(calls))
        with self.assertRaises(RequestError) as error:
            jobs.create({**request, "upload_sha256": "f" * 64})
        self.assertEqual(409, error.exception.status)
        jobs.close()
        resumed = self.jobs(lambda *args, **kwargs: self.fail("A finished job must not rerun"))
        self.assertEqual("native_complete", resumed.snapshot(request["run_id"])["status"])
        self.assertFalse(resumed.create(request)[1])
        self.assertEqual(1, len(calls))

    def test_green_result_without_a_sealed_transfer_is_rejected(self):
        jobs = self.jobs(lambda *args, **kwargs: self.passing_result())
        request = self.request()
        jobs.create(request)
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("failed", finished["status"])
        self.assertIn("sealed capture transfer", finished["error"])

    def test_discovery_failure_retains_diagnostics_across_endpoint_restart(self):
        from description_pipeline.sources.solidworks.errors import CadError
        from description_pipeline.verification.consumer import ConsumerError

        def runner(*args, **kwargs):
            self.fail("Capture ran after the native reader failed")

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

                def native_reader(frozen, settings, on_activity=None, error=error):
                    raise error

                jobs = self.jobs(runner, native_reader=native_reader)
                try:
                    request = self.request()
                    jobs.create(request)
                    finished = self.run_to_terminal(jobs, request["run_id"])
                    self.assertEqual("failed", finished["status"])
                    self.assertEqual(f"{type(error).__name__}: {error}", finished["error"])
                    self.assertEqual(detail, finished["detail"])
                finally:
                    jobs.close()
                recovered = self.jobs(runner)
                try:
                    self.assertEqual(finished, recovered.snapshot(request["run_id"]))
                    self.assertFalse(recovered.create(request)[1])
                finally:
                    recovered.close()

    def test_terminal_publication_is_observably_atomic(self):
        """A terminal snapshot always carries the complete completion record.

        The worker's mutex acquisitions are delayed (test only; the lock stays
        free while it sleeps), which pins the release/reacquire interleaving: if
        a terminal status could be snapshotted before ``completed_at``, the
        terminal event or the sealed failure diagnostics, this observer sees it.
        """

        from description_pipeline.sources.solidworks.errors import CadError

        for scenario in ("failure", "success"):
            with self.subTest(scenario=scenario):
                if scenario == "failure":
                    error = CadError(
                        "cad_read_failed", "Native read failed", detail={"phase": "read", "cause": "lost binding"}
                    )

                    def native_reader(frozen, settings, on_activity=None, error=error):
                        raise error

                    def runner(*args, **kwargs):
                        raise AssertionError("Capture ran after the native reader failed")

                    jobs = self.jobs(runner, native_reader=native_reader)
                    terminal_status = "failed"
                    terminal_event = "failed"
                else:

                    def runner(package, output, **kwargs):
                        return self.capture_transfer_result(
                            output, run_id=kwargs["run_id"], on_event=kwargs.get("on_event")
                        )

                    jobs = self.jobs(runner)
                    terminal_status = "native_complete"
                    terminal_event = "completed"

                jobs.mutex = _DelayedWorkerMutex(jobs.mutex, jobs.thread, 0.02)
                stop = threading.Event()
                violations: list = []
                request = self.request()
                run_id = request["run_id"]

                def observe(
                    jobs=jobs, run_id=run_id, stop=stop, terminal_event=terminal_event, violations=violations
                ):
                    while not stop.is_set():
                        job = jobs.snapshot(run_id)
                        if job["status"] in {"failed", "native_complete", "passed"}:
                            missing = []
                            if not isinstance(job.get("completed_at"), str):
                                missing.append("completed_at")
                            events = job.get("events") or []
                            if not events or events[-1].get("state") != terminal_event:
                                missing.append("terminal_event")
                            if job["status"] == "failed" and not isinstance(job.get("failure_diagnostics"), dict):
                                missing.append("failure_diagnostics")
                            if missing:
                                violations.append({"status": job["status"], "missing": missing})
                                return
                        time.sleep(0.0005)

                jobs.create(request)
                observer = threading.Thread(target=observe, name="terminal-publication-observer", daemon=True)
                observer.start()
                try:
                    finished = self.run_to_terminal(jobs, run_id)
                    self.assertEqual(terminal_status, finished["status"], finished.get("error"))
                finally:
                    jobs.close()
                    stop.set()
                    observer.join(5)
                self.assertEqual([], violations, f"terminal snapshots missed completion fields: {violations}")

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
        requests = [self.request() for _ in range(3)]
        for request in requests:
            jobs.create(request)
        for request in requests:
            self.stage_and_seal(jobs, request["run_id"])
            self.wait_state_any_status(jobs, request["run_id"], {"awaiting_definition"})
            self.complete_definition(jobs, request["run_id"])
        for request in requests:
            self.await_terminal(jobs, request["run_id"])
        self.assertEqual(1, peak)

    def test_extra_fields_path_escape_and_changed_upload_are_rejected(self):
        jobs = self.jobs(lambda *args, **kwargs: self.fail("Unvalidated request ran"))
        for mutation in (
            {"target": "unknown"},
            {"package": "../arm"},
            {"handoff_sha256": "0" * 64},
            {"command": "anything"},
            {"upload_sha256": "0" * 64},
            {"main_assembly": "cad/absent.SLDASM"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(PipelineError):
                jobs.create({**self.request(), **mutation})

    def test_http_authentication_and_request_contract(self):
        jobs = self.jobs(lambda *args, **kwargs: {"passed": False, "error": "expected"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(jobs, self.config["token"]))
        threading.Thread(target=server.serve_forever, daemon=True).start()
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
            body = json.load(response)
            self.assertEqual(request["run_id"], body["run_id"])
            self.assertEqual("awaiting_files", body["selection"]["state"])
        with urllib.request.urlopen(
            urllib.request.Request(address + "/v1/jobs/" + request["run_id"], headers=headers), timeout=2
        ) as response:
            self.assertEqual("selecting", json.load(response)["status"])

    def test_second_endpoint_cannot_acquire_same_state(self):
        self.jobs(lambda *args, **kwargs: {"passed": False})
        with self.assertRaises(PipelineError):
            Jobs(self.config)

    def test_process_exit_releases_ownership_and_fails_interrupted_job(self):
        request = self.request()
        payload = self.root / "child-job.json"
        write_json(payload, {"request": request})
        # A real child process exits from an unfinished selection without
        # Jobs.close(); this exercises kernel lock release rather than deleting
        # a lock file, and the unfinished state must fail on the next start.
        repo = Path(__file__).resolve().parents[2]
        script = (
            "import json,os,sys;from pathlib import Path;"
            f"sys.path.insert(0,{str(repo / 'src')!r});"
            f"sys.path.insert(0,{str(repo)!r});"
            "from unittest.mock import patch;"
            f"patch('description_pipeline.orchestration.windows._native_tool_record',return_value={TOOL!r}).start();"
            "from description_pipeline.orchestration.windows import Jobs,read_config;"
            "payload=json.loads(Path(sys.argv[2]).read_text());"
            "j=Jobs(read_config(Path(sys.argv[1])));"
            "j.create(payload['request']);"
            "j._activity(payload['request']['run_id'],"
            "{'phase':'discover','action':'observe','current_object':'cad/robot.SLDASM'});"
            "os._exit(17)"
        )
        stopped = subprocess.run(
            [sys.executable, "-I", "-c", script, str(self.path), str(payload)], capture_output=True, timeout=20
        )
        self.assertEqual(17, stopped.returncode, stopped.stderr.decode())
        resumed = self.jobs(lambda *a, **k: self.fail("Interrupted CAD job must not restart"))
        resumed.queue.join()
        job = resumed.snapshot(request["run_id"])
        self.assertEqual("failed", job["status"])
        self.assertIn("Endpoint restarted during native execution", job["error"])
        self.assertIsNone(job["activity"])
        self.assertEqual("observe", job["activity_final"]["action"])
        self.assertEqual("cad/robot.SLDASM", job["activity_final"]["current_object"])
        self.assertFalse(resumed.create(request)[1])

    def test_remote_binding_overlapping_roots_and_obsolete_keys_are_rejected(self):
        for change in (
            {"host": "0.0.0.0"},
            {"output_root": str(self.packages / "output")},
            {"handoff_roots": []},
            {"discovery": {}},
            {"token_file": "relative-token"},
        ):
            with self.subTest(change=change):
                write_json(self.path, {**self.config_data, **change})
                with self.assertRaises(PipelineError):
                    read_config(self.path)

    def test_publication_target_accepts_well_formed_branch_names(self):
        for base in ("main", "feature/m3.0", "release/m2.1-rc1", "work/solidworks/m3.0"):
            with self.subTest(base=base):
                write_json(
                    self.path,
                    {
                        **self.config_data,
                        "targets": {"m3_0": {"repository_slug": "mimicverse/description", "base": base}},
                    },
                )
                self.assertEqual(read_config(self.path)["targets"]["m3_0"]["base"], base)

    def test_publication_target_rejects_malformed_branch_names(self):
        for base in ("", " ", "/main", "main/", "main..x", "main.", ".hidden", "main.lock", "feat//x", "a b"):
            with self.subTest(base=base):
                write_json(
                    self.path,
                    {
                        **self.config_data,
                        "targets": {"m3_0": {"repository_slug": "mimicverse/description", "base": base}},
                    },
                )
                with self.assertRaises(PipelineError):
                    read_config(self.path)


if __name__ == "__main__":
    unittest.main()
