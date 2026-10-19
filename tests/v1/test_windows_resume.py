"""Linked-run endpoint behavior on the native boundary: reuse, refusals, diagnostics."""

from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch
import uuid
from pathlib import Path

from description_pipeline.io import PipelineError, write_json
from description_pipeline.orchestration.windows import NATIVE_STAGES, Jobs, RequestError
from .endpoint_support import PART, EndpointFixture

LINUX_STAGES = ("generate", "verify", "publish")


class ResumeEndpointTests(EndpointFixture, unittest.TestCase):
    def await_terminal(self, jobs: Jobs, run_id: str, timeout: float = 20.0) -> dict:
        return EndpointFixture.await_terminal(self, jobs, run_id, timeout=timeout)

    def native_parent(self) -> tuple[Jobs, dict]:
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        parent = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual(parent["status"], "native_complete", parent.get("error"))
        return jobs, parent

    @staticmethod
    def linked_request(parent: dict, stage: str) -> dict:
        return {
            "run_id": str(uuid.uuid4()),
            "upload_files": parent["request"]["upload_files"],
            "upload_sha256": parent["request"]["upload_sha256"],
            "main_assembly": parent["request"]["main_assembly"],
            "resume": {"parent_run": parent["run_id"], "from_stage": stage},
        }

    def test_capture_resume_reuses_parent_checkpoints_with_provenance(self) -> None:
        jobs, parent = self.native_parent()
        jobs.close()
        calls: list[dict] = []

        def runner(package, output, **kwargs):
            calls.append({"package": Path(package), **{k: v for k, v in kwargs.items() if k != "backend"}})
            return self.capture_transfer_result(output, run_id=kwargs["run_id"], on_event=kwargs.get("on_event"))

        child_jobs = self.jobs(runner, native_reader=lambda *args, **kwargs: self.fail("discovery must not rerun"))
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
        self.assertEqual(calls[0]["resume"], request["resume"])
        self.assertNotIn("resume_from", calls[0])
        self.assertNotIn("seed_dir", calls[0])
        self.assertEqual(calls[0]["package"], Path(parent["prepared_dir"]))
        self.assertEqual({event["stage"] for event in inherited}, {"freeze", "discover"})

    def test_linux_owned_stages_are_never_rerun_on_the_native_host(self) -> None:
        jobs, parent = self.native_parent()
        rows = {row["stage"]: row for row in jobs.plan(parent["run_id"])["stage_reruns"]}
        for stage in NATIVE_STAGES:
            self.assertTrue(rows[stage]["eligible"], stage)
        for stage in LINUX_STAGES:
            self.assertFalse(rows[stage]["eligible"], stage)
            self.assertEqual(rows[stage]["reason"], "linux_owned")
            self.assertTrue(rows[stage]["message"])
            self.assertEqual(rows[stage]["recomputes"], [])
        for stage in LINUX_STAGES:
            with self.assertRaises(RequestError) as caught:
                jobs.create(self.linked_request(parent, stage))
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(caught.exception.payload["reason"], "linux_owned")
            self.assertIn("Linux", str(caught.exception))
        with self.assertRaises(RequestError):
            jobs.artifact(parent["run_id"], "urdf/robot.urdf")

    def test_changed_staged_bytes_block_capture_reuse_only(self) -> None:
        jobs, parent = self.native_parent()
        source = jobs._source_dir(jobs.jobs[parent["run_id"]]) / PART
        source.write_bytes(b"changed after the parent capture\n")
        rows = {row["stage"]: row for row in jobs.plan(parent["run_id"])["stage_reruns"]}
        self.assertFalse(rows["capture"]["eligible"])
        with self.assertRaises(RequestError) as caught:
            jobs.create(self.linked_request(parent, "capture"))
        self.assertTrue(caught.exception.payload["reason"])
        earliest = caught.exception.payload["earliest_required"]
        self.assertTrue(earliest is None or earliest in NATIVE_STAGES)

    def test_second_linked_job_is_refused_while_one_is_active(self) -> None:
        jobs, parent = self.native_parent()
        jobs.close()
        started, release = threading.Event(), threading.Event()

        def runner(package, output, **kwargs):
            if kwargs.get("resume") is not None:
                started.set()
                if not release.wait(10):
                    raise PipelineError("release timeout")
            return self.capture_transfer_result(output, run_id=kwargs["run_id"], on_event=kwargs.get("on_event"))

        child_jobs = self.jobs(runner)
        first = self.linked_request(parent, "capture")
        child_jobs.create(first)
        self.assertTrue(started.wait(5))
        second = self.linked_request(parent, "capture")
        with self.assertRaises(RequestError) as caught:
            child_jobs.create(second)
        self.assertEqual(caught.exception.payload["reason"], "already_active")
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
        state = self.wait_state_any_status(resumed, accepted["run_id"], {"awaiting_definition"})
        if state["status"] == "awaiting_definition":
            self.complete_definition(resumed, accepted["run_id"])
        finished = self.await_terminal(resumed, accepted["run_id"])
        self.assertEqual(finished["status"], "native_complete", finished.get("error"))

    def test_stale_enqueue_state_is_reprobed_before_reuse(self) -> None:
        jobs, parent = self.native_parent()
        jobs.close()
        request = self.linked_request(parent, "capture")
        parent_path = self.config["state_root"] / "jobs" / (parent["run_id"] + ".json")
        stored = json.loads(parent_path.read_text(encoding="utf-8"))
        stored.update(
            run_id=request["run_id"],
            request=request,
            source_run_id=parent["run_id"],
            status="queued",
            events=[],
            result=None,
            error=None,
            capture_archive=None,
        )
        stored.pop("completed_at", None)
        write_json(self.config["state_root"] / "jobs" / (request["run_id"] + ".json"), stored)
        prepared = Path(parent["prepared_dir"])
        self.assertTrue(prepared.is_dir())
        (prepared / "robot.yaml").write_text("corrupted after enqueue\n", encoding="utf-8")
        resumed = self.jobs(lambda *args, **kwargs: self.fail("Re-probe must refuse before running"))
        finished = self.await_terminal(resumed, request["run_id"])
        self.assertEqual(finished["status"], "failed")
        self.assertIn("restart from that step", finished["error"])

    def test_queued_freeze_restart_can_complete_its_definition_exchange(self) -> None:
        jobs, parent = self.native_parent()
        request = self.linked_request(parent, "freeze")
        with patch.object(jobs.queue, "put"):
            jobs.create(request)
        jobs.close()
        resumed = self.jobs()
        waiting = self.wait_state_any_status(resumed, request["run_id"], {"awaiting_definition"})
        self.assertEqual(waiting["status"], "awaiting_definition", waiting.get("error"))
        self.complete_definition(resumed, request["run_id"])
        finished = self.await_terminal(resumed, request["run_id"])
        self.assertEqual(finished["status"], "native_complete", finished.get("error"))

    def test_restarted_queue_revalidates_its_effective_selection(self) -> None:
        jobs, parent = self.native_parent()
        request = self.linked_request(parent, "capture")
        with patch.object(jobs.queue, "put"):
            jobs.create(request)
        jobs.close()
        ledger = self.config["state_root"] / "jobs" / (request["run_id"] + ".json")
        stored = json.loads(ledger.read_text(encoding="utf-8"))
        stored["main_assembly"] = None
        write_json(ledger, stored)
        resumed = self.jobs(lambda *args, **kwargs: self.fail("Mismatched queued selection must not capture"))
        finished = self.await_terminal(resumed, request["run_id"])
        self.assertEqual(finished["status"], "failed")
        self.assertIn("effective main assembly", finished["error"])


if __name__ == "__main__":
    unittest.main()
