"""Job persistence, recovery, idempotency and watchdog behaviour."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import CancelledError
from pathlib import Path
from unittest.mock import patch

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import BridgeError  # noqa: E402
from description_pipeline.sources.solidworks.jobs import Job, JobError, JobRunner, JobStore  # noqa: E402
from description_pipeline.sources.solidworks.jsonio import digest_json  # noqa: E402


def wait_for(store: JobStore, job_id: str, states: tuple, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = store.load(job_id)
        if job.state in states:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} stayed in {store.load(job_id).state}")


def freeze_request(**config: object) -> dict[str, object]:
    """A request body with the shape the worker accepts."""

    return {"kind": "freeze", "config": {"provider": "solidworks", **config}}


def doctor_request() -> dict[str, object]:
    """A read-only probe request; unlike a freeze it may be replayed."""

    return {"kind": "doctor", "assembly": "C:/cad/robot.SLDASM"}


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-jobs-"))
        self.store = JobStore(self.tmp)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_interrupted_doctor_is_requeued_with_a_new_attempt(self) -> None:
        request = doctor_request()
        job = Job(
            job_id="job-1",
            request=request,
            request_digest=digest_json(request),
            state="running",
            attempt=1,
        )
        self.store.save(job)
        (self.store.job_dir("job-1") / "staging").mkdir()

        recovered = self.store.recover()

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].state, "queued")
        self.assertEqual(recovered[0].attempt, 2)
        error = recovered[0].error
        assert error is not None
        self.assertEqual(error["code"], "worker_restart")
        self.assertFalse((self.store.job_dir("job-1") / "staging").exists())

    def test_interrupted_freeze_is_failed_and_keeps_its_staging(self) -> None:
        request = freeze_request(assembly="C:/cad/robot.SLDASM")
        self.store.save(
            Job(job_id="job-fz", request=request, request_digest=digest_json(request), state="running", attempt=1)
        )
        staging = self.store.job_dir("job-fz") / "staging"
        (staging / "snapshot.failed-001").mkdir(parents=True)

        recovered = self.store.recover(worker_version="0.3.0")

        # nothing proves the CAD inputs were unchanged while it ran, so the job
        # may not be replayed under the same accepted request identity
        self.assertEqual(recovered, [])
        stored = self.store.load("job-fz")
        self.assertEqual(stored.state, "failed")
        error = stored.error
        assert error is not None
        self.assertEqual(error["code"], "recovered_freeze_unverifiable")
        self.assertEqual(stored.attempt, 1)
        self.assertTrue((staging / "snapshot.failed-001").is_dir())

    def test_freeze_from_another_worker_version_is_not_replayed(self) -> None:
        request = freeze_request()
        self.store.save(
            Job(
                job_id="job-old-version",
                request=request,
                request_digest=digest_json(request),
                state="queued",
                worker_version="0.2.0",
            )
        )

        recovered = self.store.recover(worker_version="0.3.0")

        self.assertEqual(recovered, [])
        stored = self.store.load("job-old-version")
        self.assertEqual(stored.state, "failed")
        error = stored.error
        assert error is not None
        self.assertEqual(error["code"], "recovered_worker_version_changed")

    def test_succeeded_jobs_are_never_recovered(self) -> None:
        self.store.save(Job(job_id="job-2", request={}, request_digest="d2", state="succeeded"))
        self.assertEqual(self.store.recover(), [])

    def test_events_and_heartbeat_are_recorded(self) -> None:
        self.store.save(Job(job_id="job-3", request={}, request_digest="d3"))
        self.store.append_event("job-3", {"event": "progress", "stage": "freeze"})
        self.store.heartbeat("job-3", "freeze")

        self.assertIn("progress", self.store.log_text("job-3"))
        heartbeat = self.store.heartbeat_of("job-3")
        assert heartbeat is not None
        self.assertEqual(heartbeat["stage"], "freeze")

    def test_job_ids_cannot_escape_the_store(self) -> None:
        with self.assertRaises(BridgeError):
            self.store.job_dir("../etc")

    def test_unknown_job_is_not_found_and_bad_ids_are_client_errors(self) -> None:
        """HTTP 语义由 JobError 自带：未知 job 是 404，越界 id 是 400，坏记录才是 500。"""

        with self.assertRaises(JobError) as missing:
            self.store.load("job-missing")
        self.assertEqual(missing.exception.code, "job_not_found")
        self.assertEqual(missing.exception.http_status, 404)

        with self.assertRaises(JobError) as escaped:
            self.store.load("../etc")
        self.assertEqual(escaped.exception.code, "invalid_job_id")
        self.assertEqual(escaped.exception.http_status, 400)

        broken = self.store.root / "job-broken"
        broken.mkdir()
        (broken / "job.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(JobError) as corrupt:
            self.store.load("job-broken")
        self.assertEqual(corrupt.exception.code, "invalid_job_record")
        self.assertEqual(corrupt.exception.http_status, 500)

    def test_a_record_that_no_longer_matches_its_digest_is_failed_not_replayed(self) -> None:
        original = freeze_request(assembly="C:/cad/robot.SLDASM")
        job = Job(job_id="job-tampered", request=original, request_digest=digest_json(original), state="running")
        self.store.save(job)
        # the record is edited after it was accepted: replaying it would run work
        # that was never requested under this job's identity
        tampered = json.loads(json.dumps(original))
        tampered["config"]["assembly"] = "C:/cad/other.SLDASM"
        self.store.update("job-tampered", lambda record: setattr(record, "request", tampered))

        self.assertEqual(self.store.recover(), [])

        stored = self.store.load("job-tampered")
        self.assertEqual(stored.state, "failed")
        error = stored.error
        assert error is not None
        self.assertEqual(error["code"], "recovered_request_unusable")
        self.assertIn("recovered_request_unusable", self.store.log_text("job-tampered"))

    def test_recovery_records_a_version_switch(self) -> None:
        request = doctor_request()
        job = Job(job_id="job-version", request=request, request_digest=digest_json(request), worker_version="0.2.0")
        self.store.save(job)

        recovered = self.store.recover(worker_version="0.3.0")

        self.assertEqual(recovered[0].worker_version, "0.3.0")
        self.assertIn("recovered_under_new_version", self.store.log_text("job-version"))

    def test_active_jobs_are_read_from_the_records(self) -> None:
        request = freeze_request()
        self.store.save(Job(job_id="job-q", request=request, request_digest=digest_json(request), state="queued"))
        self.store.save(Job(job_id="job-r", request=request, request_digest=digest_json(request), state="running"))
        self.store.save(Job(job_id="job-done", request={}, request_digest="x", state="succeeded"))

        active = self.store.active_jobs()

        self.assertEqual(active["queued"], 1)
        self.assertEqual(active["running"], 1)
        self.assertEqual(active["queued_ids"], ["job-q"])
        self.assertEqual(active["running_ids"], ["job-r"])


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-runner-"))
        self.store = JobStore(self.tmp)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_duplicate_request_reuses_the_first_job(self) -> None:
        runner = JobRunner(self.store, lambda job, staging: {"ok": True})
        first = runner.submit({"kind": "freeze", "config": {"provider": "solidworks"}})
        second = runner.submit({"kind": "freeze", "config": {"provider": "solidworks"}})
        runner.close()

        self.assertEqual(first.job_id, second.job_id)
        self.assertIn("duplicate_request_ignored", self.store.log_text(first.job_id))

    def test_generated_job_ids_stay_unique_in_the_same_millisecond(self) -> None:
        runner = JobRunner(self.store, lambda job, staging: {"ok": True}, poll_seconds=0.01)
        try:
            with patch("description_pipeline.sources.solidworks.jobs.time.time", return_value=1_700_000_000.0):
                jobs = [
                    runner.submit({"kind": "doctor", "assembly": f"C:/cad/robot{index}.SLDASM"}) for index in range(5)
                ]
        finally:
            runner.close()

        identifiers = [job.job_id for job in jobs]
        self.assertEqual(len(set(identifiers)), 5)
        # every accepted request must still own its own record
        for job in jobs:
            self.assertEqual(self.store.load(job.job_id).request_digest, job.request_digest)

    def test_an_explicit_job_id_cannot_overwrite_different_evidence(self) -> None:
        runner = JobRunner(self.store, lambda job, staging: {"ok": True}, poll_seconds=0.01)
        try:
            first = runner.submit({"kind": "doctor", "assembly": "C:/cad/robot.SLDASM"}, job_id="job-explicit")
            finished = wait_for(self.store, "job-explicit", ("succeeded",))
            self.assertEqual(finished.request_digest, first.request_digest)

            with self.assertRaises(BridgeError) as raised:
                runner.submit({"kind": "doctor", "assembly": "C:/cad/other.SLDASM"}, job_id="job-explicit")
        finally:
            runner.close()

        self.assertEqual(raised.exception.code, "job_id_in_use")
        # the first job's terminal evidence is untouched
        held = self.store.load("job-explicit")
        self.assertEqual(held.state, "succeeded")
        self.assertEqual(held.request_digest, first.request_digest)

    def test_submit_and_cancel_do_not_deadlock_on_each_others_locks(self) -> None:
        """A submit holding the runner lock must not be waited on by a cancel holding the store lock."""

        running = threading.Event()

        def never_finishes(job, staging):
            running.wait(timeout=30)
            return {"ok": True}

        runner = JobRunner(self.store, never_finishes, poll_seconds=0.02)
        holder = threading.Event()
        release = threading.Event()
        cancel_done = threading.Event()
        original_save = self.store.save

        def blocking_save(job):
            # called from submit while the runner lock is held
            holder.set()
            release.wait(timeout=10)
            return original_save(job)

        def submitting():
            with patch.object(self.store, "save", side_effect=blocking_save):
                runner.submit({"kind": "doctor", "assembly": "C:/cad/new.SLDASM"})

        try:
            runner.submit({"kind": "doctor", "assembly": "C:/cad/busy.SLDASM"})
            victim = runner.submit({"kind": "doctor", "assembly": "C:/cad/victim.SLDASM"})

            # daemons: if the locks are taken in the wrong order the test must
            # fail rather than hang the suite
            submitter = threading.Thread(target=submitting, daemon=True)
            submitter.start()
            self.assertTrue(holder.wait(timeout=5), "the submit never reached the store save")

            def cancelling():
                runner.cancel(victim.job_id)
                cancel_done.set()

            canceller = threading.Thread(target=cancelling, daemon=True)
            canceller.start()
            time.sleep(0.3)
            # let the submit finish: with the locks taken in opposite orders the
            # two threads would now wait for each other forever
            release.set()
            self.assertTrue(cancel_done.wait(timeout=5), "cancel deadlocked against submit")
            submitter.join(timeout=5)
            canceller.join(timeout=5)
        finally:
            release.set()
            running.set()
            runner.close()

        self.assertEqual(self.store.load(victim.job_id).state, "cancelled")

    def test_recovered_jobs_are_adopted_and_run_exactly_once(self) -> None:
        request = freeze_request()
        self.store.save(
            # queued, never started: the only freeze state that may be replayed
            Job(job_id="job-recovered", request=request, request_digest=digest_json(request), state="queued")
        )
        recovered = self.store.recover(worker_version="9.9.9")
        runs: list[str] = []

        def run(job, staging):
            runs.append(job.job_id)
            return {"ok": True}

        # a recovered job is queued in the record, so the runner has to adopt it:
        # without initial_job_ids it would sit "queued" forever
        runner = JobRunner(
            self.store,
            run,
            worker_version="9.9.9",
            poll_seconds=0.01,
            initial_job_ids=[job.job_id for job in recovered],
        )
        try:
            finished = wait_for(self.store, "job-recovered", ("succeeded",))
        finally:
            runner.close()

        self.assertEqual(finished.attempt, 1)
        self.assertEqual(runs, ["job-recovered"])
        self.assertIsNone(finished.error)
        self.assertEqual(self.store.active_jobs()["queued"], 0)

    def test_failure_records_the_error_and_leaves_no_success(self) -> None:
        def explode(job, staging):
            raise BridgeError("cad_document_unsaved", "not saved")

        runner = JobRunner(self.store, explode)
        job = runner.submit({"kind": "freeze"})
        finished = wait_for(self.store, job.job_id, ("failed",))
        runner.close()

        self.assertEqual(finished.error["code"], "cad_document_unsaved")
        self.assertIsNone(finished.result)

    def test_cancel_before_start_marks_cancelled(self) -> None:
        runner = JobRunner(self.store, lambda job, staging: {"ok": True}, poll_seconds=0.05)
        job = runner.submit({"kind": "freeze"})
        runner.cancel(job.job_id)
        finished = wait_for(self.store, job.job_id, ("succeeded", "cancelled"))
        runner.close()

        self.assertEqual(finished.state, "cancelled")
        self.assertIsNone(finished.result)

    def test_watchdog_reports_without_ending_the_job(self) -> None:
        release = time.time() + 1.0

        def slow(job, staging):
            while time.time() < release:
                time.sleep(0.02)
            return {"ok": True}

        runner = JobRunner(self.store, slow, watchdog_seconds=0.1, poll_seconds=0.02)
        job = runner.submit({"kind": "freeze"})
        deadline = time.time() + 5.0
        warned = None
        while time.time() < deadline:
            current = self.store.load(job.job_id)
            if current.error and current.error.get("code") == "cad_call_timeout":
                warned = current
                break
            time.sleep(0.02)
        finished = wait_for(self.store, job.job_id, ("succeeded",), timeout=10.0)
        runner.close()

        self.assertIsNotNone(warned, "watchdog should report the stuck call")
        self.assertEqual(finished.state, "succeeded")
        self.assertIn("watchdog", self.store.log_text(job.job_id))

    def test_startup_phase_fault_leaves_a_terminal_record(self) -> None:
        """staging 清不掉（被占用/磁盘错误）时，job 不能永远停在 queued/running。

        旧实现只在 `_loop` 里记一条 runner_error 事件：记录会停在 queued（或 begin 之后的
        running），既不执行也不失败，`active_jobs()` 仍算它活跃，版本切换因此被永久挡住。
        """

        runner = JobRunner(self.store, lambda job, staging: {"ok": True}, poll_seconds=0.02)
        job_id = "job-startup-fault"
        staging = self.store.job_dir(job_id) / "staging"
        staging.mkdir(parents=True)
        (staging / "locked.bin").write_text("held", encoding="utf-8")

        real_rmtree = shutil.rmtree

        def failing_rmtree(path, *args, **kwargs):
            if str(path).replace("\\", "/").endswith("/staging"):
                raise OSError("injected: staging is locked")
            return real_rmtree(path, *args, **kwargs)

        with patch("description_pipeline.sources.solidworks.jobs.shutil.rmtree", side_effect=failing_rmtree):
            runner.submit(freeze_request(assembly="C:/cad/robot.SLDASM"), job_id=job_id)
            finished = wait_for(self.store, job_id, ("failed", "cancelled"))
        runner.close()

        self.assertEqual(finished.state, "failed")
        self.assertEqual(finished.error["code"], "runner_error")
        self.assertIn("injected: staging is locked", finished.error["message"])
        active = self.store.active_jobs()
        self.assertEqual((active["queued"], active["running"]), (0, 0))
        self.assertIn("runner_error", self.store.log_text(job_id))

    def test_watchdog_never_touches_a_job_that_finished_first(self) -> None:
        """检查与更新之间 job 若已完成，超时注记不得落到终态记录上。"""

        request = freeze_request(assembly="C:/cad/robot.SLDASM")
        self.store.save(
            Job(job_id="job-wd-race", request=request, request_digest=digest_json(request), state="running")
        )
        runner = JobRunner(self.store, lambda job, staging: {"ok": True}, poll_seconds=0.02)
        real_update = self.store.update

        def completing_update(job_id, mutate):
            # 模拟"watchdog 看到 running 之后、真正拿锁之前 job 已完成"
            self.store.save(Job(job_id=job_id, request=request, request_digest=digest_json(request), state="succeeded"))
            return real_update(job_id, mutate)

        with patch.object(self.store, "update", side_effect=completing_update):
            runner._on_watchdog("job-wd-race")
        runner.close()

        finished = self.store.load("job-wd-race")
        self.assertEqual(finished.state, "succeeded")
        self.assertIsNone(finished.error)
        self.assertNotIn("watchdog", self.store.log_text("job-wd-race"))

    def test_watchdog_notes_a_running_job_once(self) -> None:
        request = freeze_request(assembly="C:/cad/robot.SLDASM")
        self.store.save(
            Job(
                job_id="job-wd-running",
                request=request,
                request_digest=digest_json(request),
                state="running",
                stage="readings",
            )
        )
        runner = JobRunner(self.store, lambda job, staging: {"ok": True}, poll_seconds=0.02)
        runner._on_watchdog("job-wd-running")
        runner.close()

        noted = self.store.load("job-wd-running")
        error = noted.error
        assert error is not None
        self.assertEqual(noted.state, "running")
        self.assertEqual(error["code"], "cad_call_timeout")
        self.assertEqual(error["stage"], "readings")
        self.assertIn("watchdog", self.store.log_text("job-wd-running"))

    def test_cancelled_operation_is_recorded_as_cancelled_not_internal_error(self) -> None:
        """已从 runner 队列弹出、但在 CAD 执行器队列里被取消的 job：状态与错误码都应是 cancelled。"""

        def cancelled_while_queued(job, staging):
            self.store.update(job.job_id, lambda record: setattr(record, "cancelled", True))
            raise CancelledError()

        runner = JobRunner(self.store, cancelled_while_queued, poll_seconds=0.02)
        job = runner.submit({"kind": "freeze"})
        finished = wait_for(self.store, job.job_id, ("cancelled",))
        runner.close()

        self.assertEqual(finished.state, "cancelled")
        self.assertEqual(finished.error["code"], "cancelled")
        self.assertEqual(finished.error["reason"]["type"], "CancelledError")
        self.assertIsNone(finished.result)

    def test_unexpected_failure_still_reports_internal_error(self) -> None:
        def explode(job, staging):
            raise RuntimeError("boom")

        runner = JobRunner(self.store, explode, poll_seconds=0.02)
        job = runner.submit({"kind": "freeze"})
        finished = wait_for(self.store, job.job_id, ("failed",))
        runner.close()

        self.assertEqual(finished.state, "failed")
        self.assertEqual(finished.error["code"], "internal_error")
        self.assertEqual(finished.error["type"], "RuntimeError")
