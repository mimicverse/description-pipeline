"""Job journal recovery across service restarts."""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.solidworks_export.errors import BridgeError
from tools.solidworks_export.jobs import JobManager


def _write_journal(root, payload):
    jobs_dir = os.path.join(root, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)
    path = os.path.join(jobs_dir, f"{payload['id']}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle)
    return path


def _payload(job_id, status, **extra):
    payload = {
        "id": job_id,
        "kind": "export-urdf",
        "params": {"out": f"D:/export/{job_id}"},
        "status": status,
        "created_at": time.time(),
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
        "cancel_requested": False,
    }
    payload.update(extra)
    return payload


class JobRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swbridge-jobs-")

    def test_running_job_becomes_interrupted(self):
        _write_journal(self.tmp, _payload("job-1", "running"))
        manager = JobManager(self.tmp)
        job = manager.get("job-1")
        self.assertEqual(job.status, "failed")
        assert job.error is not None
        self.assertEqual(job.error["code"], "job_interrupted")
        self.assertIsNotNone(job.finished_at)

    def test_queued_job_becomes_interrupted(self):
        _write_journal(self.tmp, _payload("job-2", "queued"))
        manager = JobManager(self.tmp)
        self.assertEqual(manager.get("job-2").status, "failed")

    def test_finished_job_survives_restart(self):
        payload = _payload(
            "job-3", "succeeded", result={"out_dir": "D:/export/job-3", "links": 2}, finished_at=time.time()
        )
        _write_journal(self.tmp, payload)
        manager = JobManager(self.tmp)
        job = manager.get("job-3")
        self.assertEqual(job.status, "succeeded")
        assert job.result is not None
        self.assertEqual(job.result["links"], 2)

    def test_malformed_journal_is_ignored(self):
        jobs_dir = os.path.join(self.tmp, "jobs")
        os.makedirs(jobs_dir, exist_ok=True)
        with open(os.path.join(jobs_dir, "broken.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        manager = JobManager(self.tmp)
        self.assertEqual(manager.list_jobs(), [])
        with self.assertRaises(BridgeError):
            manager.get("broken")

    def test_recovered_jobs_are_listed(self):
        _write_journal(self.tmp, _payload("job-4", "running"))
        _write_journal(self.tmp, _payload("job-5", "succeeded", result={"links": 1}, finished_at=time.time()))
        manager = JobManager(self.tmp)
        ids = {entry["id"] for entry in manager.list_jobs()}
        self.assertEqual(ids, {"job-4", "job-5"})

    def test_progress_is_recorded_and_persisted(self):
        manager = JobManager(self.tmp)
        self.addCleanup(manager.close)
        job = manager.submit("noop", {}, lambda current: {"ok": True})
        manager.update_progress(job.id, {"stage": "exporting_meshes", "percent": 40})
        stored = manager.get(job.id)
        assert stored.progress is not None
        self.assertEqual(stored.progress["percent"], 40)
        journal = os.path.join(self.tmp, "jobs", f"{job.id}.json")
        with open(journal, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["progress"]["stage"], "exporting_meshes")

    def test_temporary_windows_file_lock_is_retried(self):
        manager = JobManager(self.tmp)
        self.addCleanup(manager.close)
        real_replace = os.replace
        attempts = []

        def replace(source, destination):
            attempts.append(source)
            if len(attempts) == 1:
                raise PermissionError("simulated reader sharing lock")
            return real_replace(source, destination)

        with patch("tools.solidworks_export.jobs.os.replace", side_effect=replace):
            job = manager.submit("noop", {}, lambda _: {"ok": True})
            manager.close()
        self.assertEqual(job.status, "succeeded")
        self.assertGreaterEqual(len(attempts), 4)

    def test_permanent_journal_failure_does_not_kill_worker(self):
        manager = JobManager(self.tmp)
        self.addCleanup(manager.close)
        from tools.solidworks_export.jobs import Job

        job = Job(id="io-failed", kind="test", params={})
        with patch.object(manager, "_persist", side_effect=OSError("disk unavailable")):
            manager._queue.put((job, lambda _: {}))
            manager._queue.join()
        self.assertEqual(job.status, "failed")
        assert job.error is not None
        self.assertEqual(job.error["code"], "job_journal_failed")
        next_job = manager.submit("noop", {}, lambda _: {"ok": True})
        manager.close()
        self.assertEqual(next_job.status, "succeeded")


if __name__ == "__main__":
    unittest.main()
