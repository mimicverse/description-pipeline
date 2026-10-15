"""The strict settlement checker semantics of the scheduled smoke.

The canonical terminal state (run failed; six stage rows; terminal dispatch and
native operations; one recorded event) is accepted, and the single-field
mutations that the design probe defines as the review boundary are rejected:
missing run events, a queued or unresolved dispatch operation, and a native
operation still running after the capture completed.  Event identity is exact -
a ``stage`` event must correspond to the stage row it describes - and no fixed
event count is assumed.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from tests.v1.split_capture_fixture import CONTROL_LIMITS, REQUIRED_CONTROL_GATES

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "deploy" / "airflow" / "scripts" / "scheduled_smoke.py"

RUN_ID = "portal-checker-only-failed-run"
STAGES = {
    "freeze": "completed",
    "discover": "completed",
    "capture": "completed",
    "generate": "completed",
    "verify": "failed",
    "publish": "blocked",
}
TASK_STATES = {"completed": "success", "failed": "failed", "blocked": "upstream_failed"}


def _load_smoke():
    spec = importlib.util.spec_from_file_location("scheduled_smoke_settlement", SMOKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SettlementCheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_smoke()
        cls.projection = {
            "state": "failed",
            "tasks": [{"task_id": stage, "state": TASK_STATES[state]} for stage, state in STAGES.items()],
        }

    def rows(self) -> dict:
        return {
            "run_state": ["failed"],
            "request": [[f"portal-{RUN_ID}", "fixture", "app:tenant:ordinary-operator"]],
            "operations": [
                ["dispatch-key", None, "dispatch", "succeeded"],
                ["native-key", "capture", "native", "succeeded"],
            ],
            "stages": [[stage, state] for stage, state in STAGES.items()],
            "events": [{"seq": 1, "kind": "checker_baseline", "stage": None, "payload": None}],
        }

    def compare(self, rows: dict) -> dict:
        return {item["name"]: item for item in self.module._settlement_comparisons(rows, self.projection)}

    def test_the_canonical_terminal_state_is_accepted(self) -> None:
        comparisons = self.compare(self.rows())
        self.assertTrue(all(item["passed"] for item in comparisons.values()), comparisons)

    def test_missing_run_events_are_rejected(self) -> None:
        rows = self.rows()
        rows["events"] = []
        self.assertFalse(self.compare(rows)["run_event_identity"]["passed"])

    def test_a_fabricated_stage_event_is_rejected(self) -> None:
        rows = self.rows()
        rows["events"].append({"seq": 2, "kind": "stage", "stage": "verify", "payload": {"state": "completed"}})
        self.assertFalse(self.compare(rows)["run_event_identity"]["passed"])

    def test_legitimate_stage_history_is_accepted(self) -> None:
        """The verified-history case: verify recorded running then failed."""

        rows = self.rows()
        rows["events"] = [
            {"seq": 1, "kind": "stage", "stage": "verify", "payload": {"state": "running"}},
            {"seq": 2, "kind": "stage", "stage": "verify", "payload": {"state": "failed"}},
        ]
        self.assertTrue(self.compare(rows)["run_event_identity"]["passed"])

    def test_a_stage_event_for_a_nonexistent_stage_is_rejected(self) -> None:
        rows = self.rows()
        rows["events"].append({"seq": 2, "kind": "stage", "stage": "nonexistent-stage", "payload": {}})
        self.assertFalse(self.compare(rows)["run_event_identity"]["passed"])

    def test_a_stage_event_without_a_state_is_rejected(self) -> None:
        rows = self.rows()
        rows["events"].append({"seq": 2, "kind": "stage", "stage": "verify", "payload": {}})
        self.assertFalse(self.compare(rows)["run_event_identity"]["passed"])

    def test_a_queued_dispatch_operation_is_rejected(self) -> None:
        rows = self.rows()
        rows["operations"][0][3] = "queued"
        self.assertFalse(self.compare(rows)["dispatch_operation_terminal"]["passed"])

    def test_an_unresolved_dispatch_operation_is_rejected(self) -> None:
        rows = self.rows()
        rows["operations"][0][3] = "unresolved"
        self.assertFalse(self.compare(rows)["dispatch_operation_terminal"]["passed"])

    def test_a_running_native_operation_is_rejected(self) -> None:
        rows = self.rows()
        rows["operations"][1][3] = "running"
        self.assertFalse(self.compare(rows)["native_operation_terminal"]["passed"])

    def test_capture_completed_without_a_native_operation_is_rejected(self) -> None:
        rows = self.rows()
        rows["operations"] = [operation for operation in rows["operations"] if operation[2] != "native"]
        self.assertFalse(self.compare(rows)["native_operation_terminal"]["passed"])

    def test_stage_rows_must_match_the_projection_exactly(self) -> None:
        rows = self.rows()
        rows["stages"] = [row for row in rows["stages"] if row[0] != "publish"]
        self.assertFalse(self.compare(rows)["stage_rows_match_projection"]["passed"])


class QualityRejectionTests(unittest.TestCase):
    """The legitimate control report must pass; missing or failed required gates refuse."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_smoke()
        cls.required = list(REQUIRED_CONTROL_GATES)
        cls.limits = list(CONTROL_LIMITS)

    def control_checks(self) -> list[dict]:
        return [
            {"id": gate, "state": "passed", "passed": True} for gate in self.required
        ] + [{"id": gate, "state": "failed", "passed": False} for gate in self.limits]

    def test_the_required_and_control_sets_are_disjoint(self) -> None:
        self.assertFalse(set(self.required) & set(self.limits))

    def test_the_legitimate_control_report_is_accepted(self) -> None:
        quality = self.module._quality_rejection_comparisons(self.control_checks(), set(self.limits))
        self.assertTrue(quality["executed"], quality)
        self.assertTrue(quality["consistent"], quality)
        self.assertTrue(quality["views_agree"], quality)
        self.assertEqual(quality["unexpected_gates"], [])
        self.assertEqual(quality["missing_required_gates"], [])

    def test_a_missing_required_gate_is_rejected(self) -> None:
        checks = [item for item in self.control_checks() if item["id"] != self.required[0]]
        quality = self.module._quality_rejection_comparisons(checks, set(self.limits))
        self.assertTrue(quality["missing_required_gates"])

    def test_a_not_run_required_gate_is_rejected(self) -> None:
        checks = self.control_checks()
        checks[0] = {"id": self.required[0], "state": "not_run", "passed": False}
        quality = self.module._quality_rejection_comparisons(checks, set(self.limits))
        self.assertFalse(quality["executed"])
        self.assertTrue(quality["missing_required_gates"])

    def test_a_failed_required_gate_is_rejected(self) -> None:
        checks = self.control_checks()
        checks[0] = {"id": self.required[0], "state": "failed", "passed": False}
        quality = self.module._quality_rejection_comparisons(checks, set(self.limits))
        self.assertTrue(quality["missing_required_gates"])

    def test_an_unexpected_failure_is_rejected(self) -> None:
        checks = [*self.control_checks(), {"id": "other.check", "state": "failed", "passed": False}]
        quality = self.module._quality_rejection_comparisons(checks, {*self.limits, "other.check"})
        self.assertEqual(quality["unexpected_gates"], ["other.check"])

    def test_disagreeing_views_are_rejected(self) -> None:
        quality = self.module._quality_rejection_comparisons(self.control_checks(), set(self.limits[:-1]))
        self.assertFalse(quality["views_agree"])


if __name__ == "__main__":
    unittest.main()
