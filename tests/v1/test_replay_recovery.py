"""Queued-replay recovery: clear refusals and execution-time selection revalidation.

These cover the replay edges of the serialized native queue: a linked run restored
from the job ledger must name its retained parent, must still own its selected
source tree, and must re-prove its effective selection before any capture.
"""

from __future__ import annotations

import json
import shutil
import unittest
import uuid
from unittest.mock import patch

from description_pipeline.io import write_json
from .endpoint_support import EndpointFixture


class QueuedReplayRecoveryTests(EndpointFixture, unittest.TestCase):
    def native_parent(self):
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        parent = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual(parent["status"], "native_complete", parent.get("error"))
        return jobs, parent

    def paused_child(self, jobs, parent, stage: str = "capture") -> dict:
        """Create one linked child without letting the live worker consume it."""

        request = {
            "run_id": str(uuid.uuid4()),
            "upload_files": parent["request"]["upload_files"],
            "upload_sha256": parent["request"]["upload_sha256"],
            "main_assembly": parent["request"]["main_assembly"],
            "resume": {"parent_run": parent["run_id"], "from_stage": stage},
        }
        with patch.object(jobs.queue, "put"):
            jobs.create(request)
        return request

    def restarted(self, calls: list):
        return self.jobs(
            lambda *args, **kwargs: calls.append(args) or {"passed": False, "error": "must not capture"},
            native_reader=lambda *args, **kwargs: self.fail("discovery must not rerun"),
        )

    def test_replayed_child_without_its_parent_fails_with_a_clear_error(self) -> None:
        jobs, parent = self.native_parent()
        request = self.paused_child(jobs, parent)
        jobs.close()
        (self.config["state_root"] / "jobs" / (parent["run_id"] + ".json")).unlink()
        calls: list = []
        resumed = self.restarted(calls)
        finished = self.await_terminal(resumed, request["run_id"])
        self.assertEqual("failed", finished["status"])
        self.assertIn("parent native run is unknown", finished["error"])
        self.assertNotIn("KeyError", finished["error"])
        self.assertEqual([], calls)

    def test_replayed_child_without_its_selected_tree_fails_with_a_clear_error(self) -> None:
        jobs, parent = self.native_parent()
        request = self.paused_child(jobs, parent)
        jobs.close()
        shutil.rmtree(self.config["package_root"] / "selected" / parent["run_id"])
        calls: list = []
        resumed = self.restarted(calls)
        finished = self.await_terminal(resumed, request["run_id"])
        self.assertEqual("failed", finished["status"])
        self.assertIn("retained selected source is no longer available", finished["error"])
        self.assertNotIn("Selection inventory", finished["error"])
        self.assertEqual([], calls)

    def test_replayed_child_revalidates_a_forged_manifest_selection(self) -> None:
        jobs, parent = self.native_parent()
        request = self.paused_child(jobs, parent)
        jobs.close()
        ledger = self.config["state_root"] / "jobs" / (request["run_id"] + ".json")
        stored = json.loads(ledger.read_text(encoding="utf-8"))
        stored["selection"]["manifest"]["main_assembly"] = "cad/other.SLDASM"
        write_json(ledger, stored)
        calls: list = []
        resumed = self.restarted(calls)
        finished = self.await_terminal(resumed, request["run_id"])
        self.assertEqual("failed", finished["status"])
        self.assertIn("effective main assembly", finished["error"])
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
