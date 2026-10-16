"""The registered delivery snapshot: the run/confirmation read path off the platform records.

The snapshot is what the delivery confirmation consumes: the registered run
row, the recorded event stream and the sealed stage receipts - never a store
working directory or a file projection.  These tests pin that read surface and
the six-stage completeness gate it feeds.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, write_json
from description_pipeline.platform import flow
from description_pipeline.stages import require_complete, stage_view

from .portal_read_support import RegisteredPlatform
from .protocol_support import protocol_events

RUN = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"
SUBJECT = "a" * 64
HANDOFF = "b" * 64


def _publish_receipt(*, submission_state: str = "published", quality_subject: str = SUBJECT) -> dict:
    return {
        "run_id": RUN,
        "stage": "publish",
        "state": submission_state,
        "passed": True,
        "subject_sha256": SUBJECT,
        "handoff_sha256": HANDOFF,
        "quality": {"passed": True, "subject_sha256": quality_subject},
        "submission": {
            "passed": True,
            "state": submission_state,
            "subject_sha256": SUBJECT,
            "commit": "c" * 40,
            "base": "feature/robot",
            "repository_slug": "example/robot",
            "branch": "work/solidworks/robot",
            "url": "https://github.com/example/robot/pull/1",
        },
    }


def _register_delivery(
    platform: RegisteredPlatform, *, submission_state: str = "published", quality_subject: str = SUBJECT
) -> None:
    """One registered six-stage delivery: records, sealed capture identity and check events."""
    platform.admit(RUN)
    platform.register_capture(RUN)
    for stage in ("freeze", "discover", "generate"):
        platform.stage(RUN, stage)
    platform.stage(
        RUN,
        "verify",
        receipt={
            "subject_sha256": SUBJECT,
            "handoff_sha256": HANDOFF,
            "quality": {"passed": True, "subject_sha256": SUBJECT},
        },
    )
    platform.stage(
        RUN, "publish", receipt=_publish_receipt(submission_state=submission_state, quality_subject=quality_subject)
    )
    platform.events(RUN, protocol_events(subject=SUBJECT))


class RegisteredDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="registered-delivery-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.platform = RegisteredPlatform(self.tmp / "platform")

    def test_snapshot_reads_the_registered_records_only(self) -> None:
        _register_delivery(self.platform)
        self.platform.conclude(RUN, "succeeded")
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual(snapshot["run_id"], RUN)
        self.assertEqual(snapshot["status"], "passed")
        self.assertEqual(snapshot["result"]["passed"], True)
        self.assertEqual(snapshot["result"]["quality"]["subject_sha256"], SUBJECT)
        self.assertEqual(snapshot["result"]["submission"]["state"], "published")
        self.assertEqual(snapshot["request"]["handoff_sha256"], HANDOFF)
        # The registered capture tree binds hardware/revision; no chain is consulted here.
        self.assertEqual((snapshot["hardware_id"], snapshot["revision"]), ("robot", "r1"))
        # Check observations and the stage outcomes both came off the recorded stream.
        stages = {stage["id"]: stage for stage in stage_view(snapshot)["stages"]}
        self.assertEqual({stage["state"] for stage in stages.values()}, {"completed"})
        self.assertEqual(stages["verify"]["checks_passed"], stages["verify"]["checks_total"])
        require_complete(stage_view(snapshot))

    def test_confirmation_rows_render_as_stage_state_events(self) -> None:
        """A stage confirmation row without a stage key renders as its stage's state event."""
        _register_delivery(self.platform)
        self.platform.world.events.append((RUN, 999, "stage", "freeze", None, json.dumps({"state": "completed"})))
        events = flow.registered_delivery_events(self.platform.store, RUN)
        freeze = [event for event in events if event.get("stage") == "freeze"]
        self.assertTrue(freeze)
        self.assertEqual(freeze[-1]["state"], "completed")
        self.assertIsInstance(freeze[-1].get("at"), str)
        self.assertTrue(all(event.get("stage") for event in events))

    def test_an_incomplete_registration_is_refused(self) -> None:
        self.platform.admit(RUN)
        _ = self.platform.tree(RUN, "capture")
        for stage in ("freeze", "discover", "capture"):
            self.platform.stage(RUN, stage)
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        with self.assertRaises(PipelineError):
            require_complete(stage_view(snapshot))

    def test_a_run_without_registration_is_refused(self) -> None:
        with self.assertRaisesRegex(PipelineError, "no platform registration"):
            flow.registered_delivery_snapshot(self.platform.store, RUN)

    def test_the_bound_submission_chain_decides_the_result(self) -> None:
        _register_delivery(self.platform, submission_state="failed")
        self.platform.conclude(RUN, "succeeded")
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual(snapshot["result"]["passed"], False)
        _register_delivery(self.platform, quality_subject="f" * 64)
        self.platform.conclude(RUN, "succeeded")
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual(snapshot["result"]["passed"], False)

    def test_a_long_recorded_history_is_read_whole(self) -> None:
        """The reader pages the full recorded history: a fixed page never truncates it."""
        _register_delivery(self.platform)
        self.platform.conclude(RUN, "succeeded")
        protocol = list(self.platform.world.events)
        prefix = [
            (RUN, index, "activity", "capture", None, json.dumps({"stage": "capture", "state": "running"}))
            for index in range(1, 251)
        ]
        self.platform.world.events = prefix + [
            (run, seq + len(prefix), kind, stage, op, payload) for run, seq, kind, stage, op, payload in protocol
        ]
        events = flow.registered_delivery_events(self.platform.store, RUN)
        self.assertEqual(len(events), len(self.platform.world.events))
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual(len(snapshot["events"]), len(self.platform.world.events))
        require_complete(stage_view(snapshot))

    def test_the_registered_run_outcome_is_authoritative(self) -> None:
        """A passing receipt never flips a non-succeeded registered outcome."""
        for state, expected_status in (
            ("failed", "failed"),
            ("cancelled", "failed"),
            ("unresolved", "failed"),
            ("running", "running"),
        ):
            with self.subTest(state=state):
                platform = RegisteredPlatform(self.tmp / f"outcome-{state}")
                _register_delivery(platform)
                platform.conclude(RUN, state)
                snapshot = flow.registered_delivery_snapshot(platform.store, RUN)
                self.assertEqual(snapshot["result"]["passed"], False)
                self.assertEqual(snapshot["status"], expected_status)

    def test_a_damaged_sealed_capture_withholds_identity(self) -> None:
        """The identity is consumed only through the sealed rebinding: forged bytes
        grant nothing, and restoring the registered bytes restores the identity."""
        _register_delivery(self.platform)
        self.platform.conclude(RUN, "succeeded")
        source = self.platform.sealed(f"store://sealed/{RUN}/capture") / "input/source.json"
        original = source.read_bytes()
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual((snapshot["hardware_id"], snapshot["revision"]), ("robot", "r1"))
        source.write_bytes(
            json.dumps({"identity": {"hardware_id": "forged"}, "metadata": {"revision": "forged"}}).encode()
        )
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual((snapshot["hardware_id"], snapshot["revision"]), (None, None))
        source.write_bytes(original)
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual((snapshot["hardware_id"], snapshot["revision"]), ("robot", "r1"))

    def test_reuse_events_copies_the_whole_parent_history(self) -> None:
        """The reuse copy reads the parent through the same paged facility."""
        child = "3f6a5c2e-6c41-4a1e-9c2a-2a1f7a1d0c33"
        self.platform.admit(RUN)
        self.platform.admit(child, from_stage="verify", parent_run_id=RUN)
        self.platform.events(
            RUN,
            [{"stage": "capture", "state": "running", "at": "t"} for _ in range(250)],
        )
        before = len(self.platform.world.events)
        flow.reuse_events(self.platform.store, child, RUN, "verify")
        copied = [row for row in self.platform.world.events[before:] if row[0] == child]
        self.assertEqual(len(copied), 250)


if __name__ == "__main__":
    unittest.main()
