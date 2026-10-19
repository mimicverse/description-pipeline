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

    def _prepare_bound_native_discover(self) -> None:
        """Seed one real running run with a bound native operation and a completed freeze."""

        import psycopg2
        import uuid as uuid_module

        from airflow.models.dagrun import DagRun
        from airflow.models.taskinstance import TaskInstance
        from airflow.settings import Session
        from description_pipeline.orchestration.airflow_client import native_run_id
        from description_pipeline.io import digest as pipeline_digest

        self.dag_run_id = f"portal-bound-discover-{uuid_module.uuid4().hex[:12]}"
        self.run_id = native_run_id(self.dag_run_id)
        self.upload_files = {
            "3.0 总装.SLDASM": "a" * 64,
            "parts/bracket.SLDPRT": "b" * 64,
        }
        self.upload_sha = pipeline_digest(self.upload_files)
        self.store.open_admission(reason="bound native reconcile proof", actor="reconcile-test")
        self.store.admit_run(
            request_id=f"portal-{self.dag_run_id}",
            run_id=self.run_id,
            principal="cli_reconcile:tenant:operator",
            target_hardware="m3.0",
            main_assembly="3.0 总装.SLDASM",
            configuration={"purpose": "inspection_preview"},
            baseline={
                "handoff_sha256": self.upload_sha,
                "files": len(self.upload_files),
                "bytes": 123,
                "folder": "M3.0.3_20261016",
            },
            dispatch={
                "stage": "freeze",
                "operation_key": f"portal-{self.dag_run_id}/freeze:0",
                "params_digest": "f" * 64,
                "dag_id": DAG_ID,
                "dag_run_id": self.dag_run_id,
                "task_id": "freeze",
                "map_index": -1,
            },
        )
        self.store.start_run(self.run_id)
        self.native_operation_id = str(uuid_module.uuid4())
        self.native_operation_key = f"{self.run_id}/native:0"
        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operation SET state = 'succeeded' WHERE operation_key = %s",
                (f"portal-{self.dag_run_id}/freeze:0",),
            )
            cursor.execute(
                "INSERT INTO stage (run_id, stage, state, receipt) VALUES (%s, 'freeze', 'completed', '{}'::jsonb)",
                (self.run_id,),
            )
            cursor.execute(
                "INSERT INTO operation"
                " (operation_id, run_id, stage, kind, state, operation_key, params_digest,"
                " dag_id, dag_run_id, task_id, map_index)"
                " VALUES (%s, %s, 'discover', 'native', 'running', %s, %s, %s, %s, 'discover', -1)",
                (
                    self.native_operation_id,
                    self.run_id,
                    self.native_operation_key,
                    "e" * 64,
                    DAG_ID,
                    self.dag_run_id,
                ),
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
                ("freeze", "success", 1),
                ("discover", "failed", 1),
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
        self.log = self.evidence / f"bound-native-{uuid_module.uuid4().hex[:8]}.json"
        self.log.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.job/v1",
                    "pipeline_id": "solidworks-to-urdf",
                    "run_id": self.run_id,
                    "request": {
                        "run_id": self.run_id,
                        "main_assembly": "3.0 总装.SLDASM",
                        "upload_sha256": self.upload_sha,
                        "upload_files": self.upload_files,
                    },
                    "selection": {
                        "state": "reading",
                        "staged_files": self.upload_files,
                        "upload_sha256": self.upload_sha,
                    },
                    "status": "failed",
                    "error": "SelectionError: the staged main opened with CAD-reported errors",
                    "error_code": "selection_open_errors",
                    "detail": {
                        "open_status": {"errors": 2, "warnings": 1},
                        "problems": [
                            {
                                "code": "selection_open_errors",
                                "document": "3.0 总装.SLDASM",
                                "message": "the staged main opened with CAD-reported errors",
                            }
                        ],
                    },
                    "events": [
                        {
                            "stage": "freeze",
                            "state": "failed",
                            "error": "SelectionError: the staged main opened with CAD-reported errors",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.bound_reconcile = {
            "operation_key": f"operatorctl-reconcile:{self.run_id}:discover:failed",
            "evidence": [str(self.log)],
            "params_digest": pipeline_digest(
                {
                    "outcome": "failed",
                    "evidence": [str(self.log)],
                    "binding": {
                        "operation_id": self.native_operation_id,
                        "task_id": "discover",
                        "map_index": -1,
                    },
                    "reported_stage": "freeze",
                }
            ),
            "binding": {
                "operation_id": self.native_operation_id,
                "task_id": "discover",
                "map_index": -1,
            },
            "native_receipt": {"path": str(self.log), "native_run_id": self.run_id},
            "reported_stage": "freeze",
        }

    def _bound_rows(self) -> dict:
        import psycopg2

        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT stage, state FROM stage WHERE run_id=%s ORDER BY stage", (self.run_id,))
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
        return {"stages": stages, "operations": operations, "run_state": run_state, "events": events}

    def test_bound_native_discover_closes_and_replays_atomically(self) -> None:
        self._prepare_bound_native_discover()
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="discover",
            state="failed",
            reconcile=self.bound_reconcile,
        )
        rows = self._bound_rows()
        self.assertEqual(
            rows["stages"],
            [
                ("capture", "blocked"),
                ("discover", "failed"),
                ("freeze", "completed"),
                ("generate", "blocked"),
                ("publish", "blocked"),
                ("verify", "blocked"),
            ],
        )
        self.assertEqual(rows["run_state"], "failed")
        operations = {row[0]: row for row in rows["operations"]}
        self.assertEqual(operations[self.native_operation_key][3], "failed")
        self.assertEqual(
            operations[f"operatorctl-reconcile:{self.run_id}:discover:failed"][3],
            "succeeded",
        )
        receipt = self.store.stage_records(self.run_id)["discover"]["receipt"]
        self.assertEqual(receipt["reported_stage"], "freeze")
        self.assertEqual(receipt["actual_stage"], "discover")
        self.assertEqual(receipt["native_receipt_sha256"], _sha256(self.log))
        before = self._bound_rows()
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="discover",
            state="failed",
            reconcile=self.bound_reconcile,
        )
        self.assertEqual(self._bound_rows(), before, "the identical recovery is a no-op")

    def test_bound_native_discover_refuses_a_green_discover_task_with_zero_writes(self) -> None:
        self._prepare_bound_native_discover()
        from airflow.models.taskinstance import TaskInstance
        from airflow.settings import Session

        with Session() as session:
            session.query(TaskInstance).filter_by(
                dag_id=DAG_ID, run_id=self.dag_run_id, task_id="discover", map_index=-1
            ).update({"state": "success"})
            session.commit()
        before = self._bound_rows()
        with self.assertRaisesRegex(Exception, "real discover task"):
            self.store.confirm_stage(
                run_id=self.run_id,
                stage="discover",
                state="failed",
                reconcile=self.bound_reconcile,
            )
        self.assertEqual(self._bound_rows(), before, "a conflicting Airflow fact performs zero writes")

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
