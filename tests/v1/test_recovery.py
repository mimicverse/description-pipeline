"""Regressions for linked-run stage planning (endpoint-side, pure)."""

from __future__ import annotations

import unittest

from description_pipeline.stages import STAGE_IDS
from description_pipeline.orchestration.recovery import stage_reruns, start_plan
from .protocol_support import protocol_events

ALL_OK = {
    "source": "ok",
    "dependency": "ok",
    "discover": "ok",
    "capture": "ok",
    "generate": "ok",
    "receipt": "ok",
    "target": "ok",
}


def job(status: str = "failed") -> dict:
    return {
        "run_id": "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        "status": status,
        "events": protocol_events(subject="a" * 64),
    }


class RecoveryTests(unittest.TestCase):
    def test_every_stage_is_available_when_all_checkpoints_validate(self) -> None:
        for stage in STAGE_IDS:
            with self.subTest(stage=stage):
                plan = start_plan(job(), stage, probe=ALL_OK, tool="ok")
                self.assertTrue(plan["accepted"], plan)
                self.assertEqual(plan["start_stage"], stage)
                self.assertTrue(plan["stage_name_zh"])

    def test_completed_parent_runs_are_allowed(self) -> None:
        plan = start_plan(job(status="passed"), "publish", probe=ALL_OK, tool="ok")
        self.assertTrue(plan["accepted"])

    def test_missing_checkpoint_reports_exact_earliest_stage(self) -> None:
        probe = dict(ALL_OK, generate="absent")
        plan = start_plan(job(), "verify", probe=probe, tool="ok")
        self.assertFalse(plan["accepted"])
        self.assertEqual(plan["reason"], "prerequisite_invalid")
        self.assertEqual(plan["earliest_required"], "generate")
        self.assertIn("生成 URDF", plan["reason_zh"])
        self.assertTrue(start_plan(job(), "generate", probe=probe, tool="ok")["accepted"])

    def test_tool_change_forces_freeze_restart_and_never_mixes_receipts(self) -> None:
        for stage in ("discover", "capture", "generate", "verify", "publish"):
            with self.subTest(stage=stage):
                plan = start_plan(job(), stage, probe=ALL_OK, tool="changed")
                self.assertFalse(plan["accepted"])
                self.assertEqual(plan["reason"], "tool_changed")
                self.assertEqual(plan["earliest_required"], "freeze")
        restart = start_plan(job(), "freeze", probe=ALL_OK, tool="changed")
        self.assertTrue(restart["accepted"], restart)

    def test_changed_or_absent_source_requires_a_fresh_upload(self) -> None:
        for source in ("changed", "absent"):
            for stage in STAGE_IDS:
                with self.subTest(source=source, stage=stage):
                    plan = start_plan(job(), stage, probe=dict(ALL_OK, source=source), tool="ok")
                    self.assertFalse(plan["accepted"])
                    self.assertEqual(plan["reason"], "source_changed")
                    self.assertIsNone(plan["earliest_required"])

    def test_active_run_is_not_ready_and_empty_history_restarts_from_discover(self) -> None:
        active = start_plan({"status": "running", "events": [{}]}, "verify", probe=ALL_OK, tool="ok")
        self.assertEqual(active["reason"], "not_ready")
        empty_job = {"status": "failed", "events": []}
        empty = start_plan(empty_job, "capture", probe=ALL_OK, tool="ok")
        self.assertEqual(empty["reason"], "prerequisite_invalid")
        self.assertEqual(empty["earliest_required"], "freeze")
        self.assertFalse(start_plan(empty_job, "discover", probe=ALL_OK, tool="ok")["accepted"])
        for stage in ("freeze",):
            with self.subTest(stage=stage):
                restart = start_plan(empty_job, stage, probe=ALL_OK, tool="ok")
                self.assertTrue(restart["accepted"], restart)
        gone = start_plan({"status": "failed", "events": []}, "freeze", probe=dict(ALL_OK, source="absent"), tool="ok")
        self.assertEqual(gone["reason"], "source_changed")

    def test_dependency_snapshot_mismatch_only_invalidates_discovery_reuse(self) -> None:
        for dependency in ("changed", "unverifiable"):
            probe = dict(ALL_OK, dependency=dependency)
            for stage in ("capture", "generate", "verify", "publish"):
                with self.subTest(dependency=dependency, stage=stage):
                    plan = start_plan(job(), stage, probe=probe, tool="ok")
                    self.assertFalse(plan["accepted"])
                    self.assertEqual(plan["reason"], "dependency_changed")
                    self.assertEqual(plan["earliest_required"], "discover")
                    self.assertTrue(start_plan(job(), plan["earliest_required"], probe=probe, tool="ok")["accepted"])
            for stage in ("freeze", "discover"):
                with self.subTest(dependency=dependency, stage=stage):
                    self.assertTrue(start_plan(job(), stage, probe=probe, tool="ok")["accepted"])

    def test_incomplete_upstream_stage_names_itself_as_the_earliest_restart(self) -> None:
        failed_freeze = {
            "run_id": "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "status": "failed",
            "events": protocol_events(stages=("freeze",), failed_stage="freeze"),
        }
        for stage in ("discover", "capture", "verify", "publish"):
            with self.subTest(stage=stage):
                plan = start_plan(failed_freeze, stage, probe=ALL_OK, tool="ok")
                self.assertFalse(plan["accepted"])
                self.assertEqual(plan["reason"], "prerequisite_invalid")
                self.assertEqual(plan["earliest_required"], "freeze")
        self.assertTrue(start_plan(failed_freeze, "freeze", probe=ALL_OK, tool="ok")["accepted"])
        failed_discover = {
            "run_id": "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "status": "failed",
            "events": protocol_events(stages=("freeze", "discover"), failed_stage="discover"),
        }
        refused = start_plan(failed_discover, "capture", probe=ALL_OK, tool="ok")
        self.assertEqual(refused["earliest_required"], "discover")
        self.assertTrue(start_plan(failed_discover, "discover", probe=ALL_OK, tool="ok")["accepted"])
        discovered = {
            "run_id": "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "status": "passed",
            "events": protocol_events(stages=("freeze", "discover")),
        }
        plan = start_plan(discovered, "capture", probe=ALL_OK, tool="ok")
        self.assertTrue(plan["accepted"], plan)
        blocked = start_plan(discovered, "verify", probe=dict(ALL_OK, capture="absent"), tool="ok")
        self.assertEqual(blocked["earliest_required"], "capture")

    def test_changed_target_never_reuses_the_parent_binding(self) -> None:
        probe = dict(ALL_OK, target="changed")
        for stage in ("capture", "generate", "verify", "publish"):
            with self.subTest(stage=stage):
                plan = start_plan(job(), stage, probe=probe, tool="ok")
                self.assertFalse(plan["accepted"])
                self.assertEqual(plan["reason"], "target_changed")
                self.assertEqual(plan["earliest_required"], "discover")
                self.assertTrue(start_plan(job(), plan["earliest_required"], probe=probe, tool="ok")["accepted"])
        self.assertTrue(start_plan(job(), "discover", probe=probe, tool="ok")["accepted"])
        self.assertTrue(start_plan(job(), "freeze", probe=probe, tool="ok")["accepted"])
        rows = {row["stage"]: row for row in stage_reruns(job(), probe=probe, tool="ok")}
        self.assertTrue(rows["publish"]["target_changed"])
        self.assertEqual(rows["publish"]["prerequisites"]["target"], "changed")
        self.assertFalse(rows["publish"]["eligible"])
        self.assertTrue(rows["discover"]["eligible"])

    def test_failed_freeze_outranks_a_changed_dependency_snapshot(self) -> None:
        failed_freeze = {
            "run_id": "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "status": "failed",
            "events": protocol_events(stages=("freeze",), failed_stage="freeze"),
        }
        probe = dict(ALL_OK, dependency="unverifiable")
        plan = start_plan(failed_freeze, "publish", probe=probe, tool="ok")
        self.assertEqual(plan["reason"], "prerequisite_invalid")
        self.assertEqual(plan["earliest_required"], "freeze")
        self.assertTrue(start_plan(failed_freeze, plan["earliest_required"], probe=probe, tool="ok")["accepted"])

    def test_dependency_refusal_at_discovery_outranks_a_later_incomplete_capture(self) -> None:
        partial = {
            "run_id": "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "status": "passed",
            "events": protocol_events(stages=("freeze", "discover")),
        }
        probe = dict(ALL_OK, dependency="unverifiable")
        for stage in ("capture", "generate", "verify", "publish"):
            with self.subTest(stage=stage):
                plan = start_plan(partial, stage, probe=probe, tool="ok")
                self.assertFalse(plan["accepted"])
                self.assertEqual(plan["reason"], "dependency_changed")
                self.assertEqual(plan["earliest_required"], "discover")
                self.assertTrue(start_plan(partial, "discover", probe=probe, tool="ok")["accepted"])

    def test_missing_discovery_bytes_outrank_a_later_incomplete_capture(self) -> None:
        partial = {
            "run_id": "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "status": "passed",
            "events": protocol_events(stages=("freeze", "discover")),
        }
        probe = dict(ALL_OK, discover="absent")
        for stage in ("capture", "generate", "verify", "publish"):
            with self.subTest(stage=stage):
                plan = start_plan(partial, stage, probe=probe, tool="ok")
                self.assertFalse(plan["accepted"])
                self.assertEqual(plan["reason"], "prerequisite_invalid")
                self.assertEqual(plan["earliest_required"], "discover")
                self.assertTrue(start_plan(partial, "discover", probe=probe, tool="ok")["accepted"])

    def test_stage_rows_report_recomputes_retains_and_prerequisites(self) -> None:
        rows = {row["stage"]: row for row in stage_reruns(job(), probe=ALL_OK, tool="ok")}
        self.assertEqual(len(rows), 6)
        publish = rows["publish"]
        self.assertEqual(publish["recomputes"], ["publish"])
        self.assertEqual(rows["verify"]["recomputes"], ["verify", "publish"])
        self.assertEqual(rows["freeze"]["recomputes"], list(STAGE_IDS))
        self.assertTrue(all(item["availability"] == "ok" for item in rows["verify"]["retains"]))
        self.assertTrue(all(item["path"] for item in rows["verify"]["retains"]))
        self.assertTrue(all(item["availability"] == "ok" for item in rows["publish"]["retains"]))
        broken = {row["stage"]: row for row in stage_reruns(job(), probe=dict(ALL_OK, discover="absent"), tool="ok")}
        self.assertFalse(broken["capture"]["eligible"])
        self.assertEqual(broken["capture"]["reason"], "prerequisite_invalid")
        self.assertEqual(broken["capture"]["prerequisites"]["earliest_required"], "discover")
        self.assertTrue(broken["discover"]["eligible"])
        self.assertEqual(broken["freeze"]["reason"], "ok")


if __name__ == "__main__":
    unittest.main()
