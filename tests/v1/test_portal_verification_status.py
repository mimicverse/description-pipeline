"""Verification readiness stays separate from unresolved effect safety."""

from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime, timedelta

from description_pipeline.orchestration.portal import PortalApp, _automatic_summary
from description_pipeline.orchestration.run_ownership import registered_recovery_block

NOW = datetime(2026, 10, 29, 7, 30, tzinfo=UTC)


def pending_projection() -> dict:
    return {
        "state": "unresolved",
        "request": {},
        "by_stage": {},
        "stages": {
            "capture": {"state": "unresolved", "command": {"command_id": "capture-command"}},
            "verify": {"state": "blocked", "decision": None},
        },
        "technical": {
            "effects": [
                {
                    "effect_id": "effect-capture",
                    "command_id": "capture-command",
                    "state": "unknown",
                    "decisive_fact": {
                        "fact_id": "fact-send",
                        "fact_type": "dispatch_authorized",
                        "claim_id": "claim-capture",
                        "fence": 1,
                        "state_class": None,
                    },
                }
            ],
            "claims": [
                {
                    "claim_id": "claim-capture",
                    "command_id": "capture-command",
                    "fence": 1,
                    "closed_at": None,
                    "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
                }
            ],
        },
    }


class PortalVerificationStatusTests(unittest.TestCase):
    def summary(self, projection: dict) -> dict:
        app = PortalApp.__new__(PortalApp)
        return _automatic_summary(app._fact_display_job(projection), projection, now=NOW)

    def test_authorized_wait_is_not_verification_failure_and_keeps_retry_block(self) -> None:
        projection = pending_projection()
        before = copy.deepcopy(projection)
        blocked = registered_recovery_block(projection)
        result = self.summary(projection)
        self.assertEqual(result["state"], "waiting")
        self.assertEqual(result["job_state"], "unresolved")
        self.assertIn("verification has not started", result["message"])
        self.assertNotIn("failed", result["message"])
        self.assertNotIn("subject_sha256", result["message"])
        self.assertEqual(projection, before)
        self.assertEqual(registered_recovery_block(projection), blocked)
        self.assertIsNotNone(blocked)

    def test_timeout_conflict_missing_proof_and_stale_claim_need_reconciliation(self) -> None:
        for reason in ("timeout", "unobservable", "conflict", "missing_fact", "expired", "wrong_fence", "closed"):
            with self.subTest(reason=reason):
                projection = pending_projection()
                effect = projection["technical"]["effects"][0]
                claim = projection["technical"]["claims"][0]
                if reason in {"timeout", "unobservable"}:
                    effect["decisive_fact"]["fact_type"] = "timeout" if reason == "timeout" else "observation"
                    effect["decisive_fact"]["state_class"] = None if reason == "timeout" else "unobservable"
                elif reason == "conflict":
                    effect["state"] = "conflict"
                elif reason == "missing_fact":
                    effect.pop("decisive_fact")
                elif reason == "expired":
                    claim["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
                elif reason == "wrong_fence":
                    claim["fence"] = 2
                else:
                    claim["closed_at"] = NOW.isoformat()
                result = self.summary(projection)
                self.assertEqual(result["state"], "unresolved")
                self.assertIn("reconcile", result["message"])
                self.assertIn("verification has not started", result["message"])
                self.assertNotIn("failed", result["message"])

    def test_registered_yield_is_waiting_but_does_not_hide_another_conflict(self) -> None:
        projection = pending_projection()
        projection["technical"]["claims"][0].update(
            closed_at=NOW.isoformat(),
            closure_code="yielded",
            yield_effect_id="effect-capture",
        )
        projection["stages"]["capture"]["receipt"] = {
            "pending_effect_states": [{"effect_id": "effect-capture", "state": "unknown"}]
        }
        self.assertEqual(self.summary(projection)["state"], "waiting")
        projection["technical"]["effects"].append({"effect_id": "other", "state": "conflict"})
        self.assertEqual(self.summary(projection)["state"], "unresolved")

    def test_historical_yield_does_not_hide_later_unknown_evidence(self) -> None:
        for reason in ("timeout", "unobservable", "missing_fact", "new_fence", "other_effect"):
            with self.subTest(reason=reason):
                projection = pending_projection()
                effect = projection["technical"]["effects"][0]
                claim = projection["technical"]["claims"][0]
                claim.update(
                    closed_at=NOW.isoformat(),
                    closure_code="yielded",
                    yield_effect_id="effect-capture",
                )
                projection["stages"]["capture"]["receipt"] = {
                    "pending_effect_states": [
                        {"effect_id": "effect-capture", "state": "unknown"}
                    ]
                }
                if reason in {"timeout", "unobservable"}:
                    effect["decisive_fact"]["fact_type"] = (
                        "timeout" if reason == "timeout" else "observation"
                    )
                    effect["decisive_fact"]["state_class"] = (
                        None if reason == "timeout" else "unobservable"
                    )
                elif reason == "missing_fact":
                    effect.pop("decisive_fact")
                elif reason == "new_fence":
                    effect["decisive_fact"]["fence"] = 2
                else:
                    claim["yield_effect_id"] = "other-effect"
                before = copy.deepcopy(projection)
                blocked = registered_recovery_block(projection)
                result = self.summary(projection)
                self.assertEqual(result["state"], "unresolved")
                self.assertIn("reconcile", result["message"])
                self.assertEqual(projection, before)
                self.assertEqual(registered_recovery_block(projection), blocked)

    def test_idle_earlier_execution_and_active_verify_are_distinct(self) -> None:
        projection = pending_projection()
        projection["state"] = "running"
        projection["technical"]["effects"] = []
        projection["stages"]["capture"]["state"] = "waiting"
        self.assertEqual(self.summary(projection)["state"], "pending")
        projection["stages"]["capture"]["state"] = "active"
        result = self.summary(projection)
        self.assertEqual(result["state"], "running")
        self.assertIn("verification has not started", result["message"])
        projection["stages"]["verify"]["state"] = "active"
        result = self.summary(projection)
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["message"], "Independent verification is in progress")

    def test_failure_and_cancellation_do_not_fabricate_a_verify_outcome(self) -> None:
        projection = pending_projection()
        projection["state"] = "failed"
        projection["technical"]["effects"] = []
        projection["stages"]["capture"].update(
            state="failed", decision={"outcome": "failed"}, receipt={"error": "Capture failed"}
        )
        result = self.summary(projection)
        self.assertEqual(result["state"], "failed")
        self.assertIn("verification has not started", result["message"])
        projection["state"] = "cancelled"
        projection["stages"]["capture"]["decision"] = {"outcome": "cancelled"}
        self.assertEqual(self.summary(projection)["state"], "cancelled")
        self.assertEqual(_automatic_summary(None, None)["state"], "pending")

    def test_actual_verify_failure_can_report_failure(self) -> None:
        projection = pending_projection()
        projection["state"] = "failed"
        projection["technical"]["effects"] = []
        projection["stages"]["verify"].update(
            state="failed",
            decision={"outcome": "failed"},
            receipt={"error": "Independent verification failed"},
        )
        result = self.summary(projection)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["message"], "Independent verification failed")

    def test_unqualified_pass_does_not_claim_failure(self) -> None:
        result = _automatic_summary(
            {
                "status": "passed",
                "result": {"quality": {"passed": True}, "formal_eligible": False},
            },
            {"stages": {"verify": {"state": "completed", "decision": {"outcome": "completed"}}}},
        )
        self.assertEqual(result["state"], "unverified")
        self.assertNotIn("failed", result["message"])

    def test_unqualified_result_does_not_hide_a_registered_failure_or_cancellation(self) -> None:
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                result = _automatic_summary(
                    {
                        "status": status,
                        "error": "The recorded operation failed",
                        "result": {
                            "quality": {"passed": True},
                            "formal_eligible": False,
                        },
                    },
                    {"stages": {}},
                )
                self.assertEqual(result["state"], status)


if __name__ == "__main__":
    unittest.main()
