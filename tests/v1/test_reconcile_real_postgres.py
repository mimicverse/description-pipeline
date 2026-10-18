"""Opt-in real PostgreSQL transaction proof for the admitted freeze closure.

The test uses real Airflow metadata tables for the DagRun and task rows and
the real PlatformStore transaction.  It does not start scheduler services and
does not touch an installed deployment; the caller supplies an isolated
platform/Airflow database with both schemas already migrated.

Required environment:

* ``DESCRIPTION_RECONCILE_AIRFLOW_DSN``: isolated Airflow metadata DSN
* ``DESCRIPTION_RECONCILE_PLATFORM_DSN``: isolated platform service DSN
* ``DESCRIPTION_RECONCILE_EVIDENCE_DIR``: persistent report directory

The failure log is a test fixture whose full scheduling identity and terminal
try number are inserted into the real Airflow tables.  The incident's original
logs remain a separate immutable archive; this test proves the store's
transaction, not a production-side reconciliation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DAG_ID = "solidworks_to_urdf"
REQUIRED = {
    "airflow_dsn": os.environ.get("DESCRIPTION_RECONCILE_AIRFLOW_DSN", "").strip(),
    "platform_dsn": os.environ.get("DESCRIPTION_RECONCILE_PLATFORM_DSN", "").strip(),
    "evidence": os.environ.get("DESCRIPTION_RECONCILE_EVIDENCE_DIR", "").strip(),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(all(REQUIRED.values()), "DESCRIPTION_RECONCILE_* isolation inputs are not configured")
class ReconcileRealPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.airflow_home = tempfile.mkdtemp(prefix="reconcile-postgres-home-")
        cls.original_environ = dict(os.environ)
        cls.addClassCleanup(cls._cleanup)
        os.environ["AIRFLOW_HOME"] = cls.airflow_home
        os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = REQUIRED["airflow_dsn"]
        os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "false"
        evidence_parent = Path(REQUIRED["evidence"])
        if not evidence_parent.is_absolute():
            raise AssertionError("DESCRIPTION_RECONCILE_EVIDENCE_DIR must be absolute")
        evidence_parent.mkdir(parents=True, exist_ok=True)
        cls.evidence = evidence_parent / f"reconcile-{uuid.uuid4().hex[:12]}"
        cls.evidence.mkdir()

    @classmethod
    def _cleanup(cls) -> None:
        os.environ.clear()
        os.environ.update(cls.original_environ)
        shutil.rmtree(cls.airflow_home, ignore_errors=True)

    def setUp(self) -> None:
        from airflow.models.dagrun import DagRun
        from airflow.models.taskinstance import TaskInstance
        from airflow.settings import Session
        from description_pipeline.platform.store import PlatformStore

        self.dag_run_id = f"portal-reconcile-{uuid.uuid4().hex[:12]}"
        self.run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://description-pipeline/reconcile/{self.dag_run_id}"))
        self.store = PlatformStore.from_environment(
            {"PLATFORM_SERVICE_DSN": REQUIRED["platform_dsn"], "PIPELINE_STORE_ROOT": str(self.evidence / "store")}
        )
        Path(self.evidence, "store").mkdir(exist_ok=True)
        self.store.open_admission(reason="reconcile proof", actor="reconcile-test")
        self.store.admit_run(
            request_id=f"portal-{self.dag_run_id}",
            run_id=self.run_id,
            principal="cli_reconcile:tenant:operator",
            target_hardware="m3.0",
            main_assembly="3.0 总装.SLDASM",
            dispatch={
                "stage": "freeze",
                "operation_key": f"portal-{self.dag_run_id}/freeze:0",
                "params_digest": "a" * 64,
                "dag_id": DAG_ID,
                "dag_run_id": self.dag_run_id,
            },
        )
        with Session() as session:
            session.add(
                DagRun(
                    dag_id=DAG_ID,
                    run_id=self.dag_run_id,
                    run_type="manual",
                    state="failed",
                )
            )
            session.flush()
            for task_id, state, try_number in (
                ("freeze", "failed", 3),
                ("discover", "upstream_failed", 0),
                ("capture", "upstream_failed", 0),
                ("generate", "upstream_failed", 0),
                ("verify", "upstream_failed", 0),
                ("publish", "upstream_failed", 0),
            ):
                session.execute(
                    TaskInstance.__table__.insert().values(
                        dag_id=DAG_ID,
                        run_id=self.dag_run_id,
                        task_id=task_id,
                        map_index=-1,
                        state=state,
                        try_number=try_number,
                        pool="default_pool",
                        pool_slots=1,
                    )
                )
            session.commit()
        self.log = self._write_log(try_number=3)
        self.reconcile = self._reconcile(self.log)

    def _write_log(self, *, try_number: int, run_id: str | None = None) -> Path:
        path = self.evidence / f"failure-{try_number}-{uuid.uuid4().hex[:8]}.log"
        path.write_text(
            json.dumps(
                {
                    "event": "Task failed with exception",
                    "dag_id": DAG_ID,
                    "run_id": self.dag_run_id if run_id is None else run_id,
                    "task_id": "freeze",
                    "map_index": -1,
                    "try_number": try_number,
                    "error_detail": [
                        {
                            "exc_type": "AirflowFailException",
                            "exc_value": "the controlled freeze attempt failed",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def _reconcile(self, log: Path) -> dict:
        evidence = [str(log)]
        return {
            "operation_key": f"operatorctl-reconcile:{self.run_id}:freeze:failed",
            "evidence": evidence,
            "params_digest": hashlib.sha256(
                json.dumps({"outcome": "failed", "evidence": evidence, "binding": None}, sort_keys=True).encode()
            ).hexdigest(),
        }

    def _rows(self) -> dict:
        import psycopg2

        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT stage, state FROM stage WHERE run_id=%s ORDER BY stage",
                (self.run_id,),
            )
            stages = cursor.fetchall()
            cursor.execute(
                    "SELECT operation_key, stage, kind, state, task_id, map_index"
                    " FROM operation WHERE run_id=%s ORDER BY kind, operation_key",
                (self.run_id,),
            )
            operations = cursor.fetchall()
            cursor.execute("SELECT state FROM run WHERE run_id=%s", (self.run_id,))
            run_state = cursor.fetchone()[0]
            cursor.execute(
                "SELECT kind, payload FROM run_event WHERE run_id=%s ORDER BY seq",
                (self.run_id,),
            )
            events = cursor.fetchall()
        return {
            "stages": stages,
            "operations": operations,
            "run_state": run_state,
            "events": events,
        }

    def test_freeze_intent_closes_atomically_and_replays_without_writes(self) -> None:
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="freeze",
            state="failed",
            reconcile=self.reconcile,
        )
        rows = self._rows()
        self.assertEqual(
            rows["stages"],
            [
                ("capture", "blocked"),
                ("discover", "blocked"),
                ("freeze", "failed"),
                ("generate", "blocked"),
                ("publish", "blocked"),
                ("verify", "blocked"),
            ],
        )
        self.assertEqual(rows["run_state"], "failed")
        operations = {row[0]: row for row in rows["operations"]}
        self.assertEqual(
            operations[f"portal-{self.dag_run_id}/freeze:0"],
            (f"portal-{self.dag_run_id}/freeze:0", "freeze", "dispatch", "failed", "freeze", -1),
        )
        self.assertEqual(
            operations[f"operatorctl-reconcile:{self.run_id}:freeze:failed"],
            (
                f"operatorctl-reconcile:{self.run_id}:freeze:failed",
                "freeze",
                "reconcile",
                "succeeded",
                None,
                None,
            ),
        )
        binding = [
            json.loads(payload) if isinstance(payload, str) else payload
            for kind, payload in rows["events"]
            if kind == "binding"
        ]
        self.assertEqual(len(binding), 1)
        self.assertEqual(binding[0]["intent_stage"], "freeze")
        self.assertEqual(binding[0]["task_id"], "freeze")
        self.assertEqual(binding[0]["try_number"], 3)
        self.assertTrue(binding[0]["freeze_dispatched"])
        receipt = self.store.stage_records(self.run_id)["freeze"]["receipt"]
        self.assertEqual(receipt["closure_shape"], "freeze_dispatch_failed")
        self.assertEqual(receipt["freeze_execution"]["try_number"], 3)

        before = self._rows()
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="freeze",
            state="failed",
            reconcile=self.reconcile,
        )
        self.assertEqual(self._rows(), before, "identical evidence is a no-op")
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        changed = self._reconcile(self.log)
        with self.assertRaisesRegex(Exception, "does not match the recorded evidence"):
            self.store.confirm_stage(
                run_id=self.run_id,
                stage="freeze",
                state="failed",
                reconcile=changed,
            )
        self.assertEqual(self._rows(), before, "changed evidence performs zero writes")

    def test_an_earlier_attempt_is_refused_with_zero_writes(self) -> None:
        earlier = self._write_log(try_number=1)
        before = self._rows()
        with self.assertRaisesRegex(Exception, "terminal failed fact"):
            self.store.confirm_stage(
                run_id=self.run_id,
                stage="freeze",
                state="failed",
                reconcile=self._reconcile(earlier),
            )
        self.assertEqual(self._rows(), before)

    def test_a_log_from_another_dag_run_is_refused_with_zero_writes(self) -> None:
        foreign = self._write_log(try_number=3, run_id="portal-foreign-run")
        before = self._rows()
        with self.assertRaisesRegex(Exception, "another DagRun"):
            self.store.confirm_stage(
                run_id=self.run_id,
                stage="freeze",
                state="failed",
                reconcile=self._reconcile(foreign),
            )
        self.assertEqual(self._rows(), before)

    def test_report_records_the_real_transaction_identity(self) -> None:
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="freeze",
            state="failed",
            reconcile=self.reconcile,
        )
        report = {
            "scope": "isolated real PostgreSQL/Airflow metadata transaction; no production DB",
            "dag_run_id": self.dag_run_id,
            "run_id": self.run_id,
            "log": {"path": str(self.log), "sha256": _sha256(self.log), "try_number": 3},
            "rows": self._rows(),
            "receipt": self.store.stage_records(self.run_id)["freeze"]["receipt"],
            "airflow_dsn_configured": bool(REQUIRED["airflow_dsn"]),
            "platform_dsn_configured": bool(REQUIRED["platform_dsn"]),
        }
        path = self.evidence / "reconcile-report.json"
        path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        print(f"reconcile report: {path}")


if __name__ == "__main__":
    unittest.main()
