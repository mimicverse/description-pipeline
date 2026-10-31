"""Read-only canonical run projection tests."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from description_pipeline.io import digest
from description_pipeline.platform.queries import read_run_projection, render_stage_summary
from description_pipeline.schemas import (
    RUN_ADMISSION_SCHEMA,
    STAGE_COMMAND_SCHEMA,
)


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.result = []
        self.closed = False
        self.effect_intent_result = []
        self.effect_fact_result = []

    def execute(self, statement: str, parameters) -> None:
        self.statements.append(statement)
        if "FROM run_admission" in statement:
            request = {"run_id": "run-1"}
            self.result = [
                (
                    "request-1",
                    None,
                    "freeze",
                    request,
                    digest(
                        {
                            "from_stage": "freeze",
                            "parent_run_id": None,
                            "request": request,
                            "request_id": "request-1",
                            "run_id": "run-1",
                            "schema_version": RUN_ADMISSION_SCHEMA,
                        }
                    ),
                    datetime(2026, 10, 25, tzinfo=UTC),
                )
            ]
        elif "FROM command\n" in statement:
            request = {"run_id": "run-1"}
            request_digest = digest(
                {
                    "command_id": "freeze-command",
                    "config_snapshot_sha256": "c" * 64,
                    "contract_sha256": "d" * 64,
                    "expected_executor_sha256": "f" * 64,
                    "idempotency_key": "run-1/freeze",
                    "input_artifacts": [],
                    "kind": "stage",
                    "policy_sha256": "e" * 64,
                    "predecessor_decision_id": None,
                    "request": request,
                    "run_id": "run-1",
                    "schema_version": STAGE_COMMAND_SCHEMA,
                    "stage": "freeze",
                }
            )
            self.result = [
                (
                    "freeze-command",
                    "run-1",
                    "stage",
                    "freeze",
                    request,
                    request_digest,
                    "run-1/freeze",
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                    "f" * 64,
                    None,
                )
            ]
        elif "FROM effect_intent AS effect" in statement:
            self.result = self.effect_intent_result
        elif "FROM effect_fact AS fact" in statement:
            self.result = self.effect_fact_result
        else:
            self.result = []

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self) -> None:
        self._cursor = _Cursor()

    def cursor(self):
        return self._cursor


class RunQueryTests(unittest.TestCase):
    def test_projection_is_read_only_and_ordered_by_contract(self):
        connection = _Connection()
        projection = read_run_projection(connection, "run-1")
        statements = " ".join(connection._cursor.statements).upper()
        self.assertNotIn("INSERT ", statements)
        self.assertNotIn("UPDATE ", statements)
        self.assertNotIn("DELETE ", statements)
        self.assertEqual(
            [row["stage"] for row in projection["stages"]],
            ["freeze", "discover", "capture", "generate", "verify", "mjcf", "publish"],
        )
        summary = render_stage_summary(projection)
        self.assertEqual(summary[0]["state"], "waiting")
        self.assertEqual(summary[0]["command_id"], "freeze-command")
        self.assertEqual([row["stage"] for row in summary[1:]], [
            "discover",
            "capture",
            "generate",
            "verify",
            "mjcf",
            "publish",
        ])

    def test_stage_summary_uses_the_canonical_unresolved_state(self):
        projection = {
            "stages": [
                {
                    "stage": "freeze",
                    "state": "unresolved",
                    "decision": {
                        "outcome": "completed",
                        "command_id": "freeze-command",
                        "reason_code": None,
                    },
                    "command": {"command_id": "freeze-command"},
                }
            ]
        }
        summary = render_stage_summary(projection)
        self.assertEqual(summary[0]["state"], "unresolved")

    def test_effect_readout_exposes_the_existing_decisive_fact_without_clearing_unknown(self):
        connection = _Connection()
        cursor = connection._cursor
        now = datetime(2026, 10, 29, tzinfo=UTC)
        cursor.effect_intent_result = [
            (
                "effect-1",
                "freeze-command",
                "airflow_dispatch",
                "airflow_dispatch",
                {"dag_id": "solidworks_to_urdf", "dag_run_id": "run-1"},
                {"state": "pending"},
                digest({"state": "pending"}),
                "outbound-1",
                "2" * 64,
                now,
            )
        ]
        cursor.effect_fact_result = [
            (
                "fact-send",
                "effect-1",
                1,
                "claim-1",
                1,
                "dispatch_authorized",
                "airflow-dispatch",
                None,
                None,
                "3" * 64,
                now,
                None,
                None,
            )
        ]
        projection = read_run_projection(connection, "run-1")
        effect = projection["technical"]["effects"][0]
        self.assertEqual(effect["state"], "unknown")
        self.assertEqual(projection["state"], "unresolved")
        self.assertEqual(
            effect["decisive_fact"],
            {
                "fact_id": "fact-send",
                "fact_type": "dispatch_authorized",
                "state_class": None,
                "claim_id": "claim-1",
                "fence": 1,
            },
        )
        cursor.effect_fact_result.append(
            (
                "fact-timeout",
                "effect-1",
                2,
                "claim-1",
                1,
                "timeout",
                "airflow-dispatch",
                None,
                "payload-1",
                "4" * 64,
                now,
                None,
                None,
            )
        )
        effect = read_run_projection(connection, "run-1")["technical"]["effects"][0]
        self.assertEqual(effect["state"], "unknown")
        self.assertEqual(effect["decisive_fact"]["fact_type"], "timeout")

    def test_effect_projection_keeps_reconciliation_metadata_and_resume_phase(self):
        connection = _Connection()
        cursor = connection._cursor
        cursor.effect_intent_result = [
            (
                "effect-1",
                "freeze-command",
                "airflow_dispatch",
                "airflow_dispatch",
                {"dag_id": "solidwork_to_urdf", "dag_run_id": "run-1"},
                {"state": "pending"},
                digest({"state": "pending"}),
                "outbound-1",
                "2" * 64,
                datetime(2026, 10, 25, tzinfo=UTC),
            )
        ]
        cursor.effect_fact_result = [
            (
                "fact-1",
                "effect-1",
                1,
                None,
                None,
                "conflict",
                "airflow-dispatch",
                None,
                "payload-1",
                "3" * 64,
                datetime(2026, 10, 25, tzinfo=UTC),
                None,
                None,
            ),
            (
                "fact-2",
                "effect-1",
                2,
                None,
                None,
                "reconciled",
                "reconciler",
                "known",
                "payload-2",
                "4" * 64,
                datetime(2026, 10, 25, tzinfo=UTC),
                "metadata-1",
                None,
            ),
        ]
        projection = read_run_projection(connection, "run-1")
        self.assertEqual(projection["technical"]["effects"][0]["state"], "satisfied")
        query = " ".join(cursor.statements)
        self.assertIn("fact.metadata_artifact_id", query)
        self.assertIn("fact.resume_phase", query)


if __name__ == "__main__":
    unittest.main()
