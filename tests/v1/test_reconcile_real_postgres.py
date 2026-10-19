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

import contextlib
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

    def tearDown(self) -> None:
        with contextlib.suppress(Exception):
            self.store.open_admission(reason="test cleanup", actor="reconcile-test")

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
        from description_pipeline.delivery import BUNDLE_SCHEMA
        from description_pipeline.io import read_data
        from description_pipeline.orchestration.airflow_client import native_run_id
        from description_pipeline.io import digest as pipeline_digest
        from tests.v1.test_platform_store import build_sealed_delivery

        self.dag_run_id = f"portal-bound-discover-{uuid_module.uuid4().hex[:12]}"
        self.run_id = native_run_id(self.dag_run_id)
        self.sealed_root = self.evidence / "store" / "sealed" / self.run_id / "freeze"
        subject, artifacts, self.upload_sha, _native_handoff = build_sealed_delivery(self.sealed_root)
        index = read_data(self.sealed_root / "input" / "handoff.json")
        self.upload_files = dict(index["package_files"])
        self.main_assembly = "cad/cad/robot.SLDASM"
        self.store.open_admission(reason="bound native reconcile proof", actor="reconcile-test")
        self.store.admit_run(
            request_id=f"portal-{self.dag_run_id}",
            run_id=self.run_id,
            principal="cli_reconcile:tenant:operator",
            target_hardware="m3.0",
            main_assembly=self.main_assembly,
            configuration={"purpose": "inspection_preview"},
            baseline={
                "handoff_sha256": self.upload_sha,
                "files": len(self.upload_files),
                "bytes": sum((self.sealed_root / "input" / name).stat().st_size for name in self.upload_files),
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
        freeze_receipt = {
            "schema_version": BUNDLE_SCHEMA,
            "pipeline_id": "solidworks-to-urdf",
            "run_id": self.run_id,
            "stage": "freeze",
            "state": "frozen",
            "passed": False,
            "handoff_sha256": self.upload_sha,
            "subject_sha256": subject,
            "tool": read_data(self.sealed_root / "tool.json"),
            "artifacts": artifacts,
        }
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="freeze",
            state="completed",
            receipt=freeze_receipt,
            checkpoint_uri=f"store://sealed/{self.run_id}/freeze",
        )
        self.native_operation_id = str(uuid_module.uuid4())
        self.native_operation_key = f"{self.run_id}/native:0"
        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operation SET state = 'succeeded' WHERE operation_key = %s",
                (f"portal-{self.dag_run_id}/freeze:0",),
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
                        "main_assembly": self.main_assembly,
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
                                "document": self.main_assembly,
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

    def _prepare_terminal_native_discover_dispatch(self) -> None:
        """Seed the production terminal-discover shape with a succeeded unbound freeze dispatch."""

        import psycopg2

        self._prepare_bound_native_discover()
        error = "SelectionError: a reference resolves outside the staged root"
        error_code = "selection_resolved_outside_stage"
        diagnostic = {
            "count": 1,
            "problems": [
                {
                    "code": error_code,
                    "document": "waist.SLDASM",
                    "instance": "waist-1/virtual",
                    "message": "a reference resolves outside the staged root",
                    "resolved": "C:\\Temp\\swx111\\VC~~\\waist\\virtual.SLDPRT",
                    "stored_location": None,
                }
            ],
            "stage_root": "C:\\description\\selected\\run",
        }
        self.log = self.evidence / f"terminal-native-{uuid.uuid4().hex[:8]}.json"
        self.log.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.job/v1",
                    "pipeline_id": "solidworks-to-urdf",
                    "run_id": self.run_id,
                    "request": {
                        "run_id": self.run_id,
                        "main_assembly": self.main_assembly,
                        "upload_sha256": self.upload_sha,
                        "upload_files": self.upload_files,
                    },
                    "selection": {
                        "state": "reading",
                        "staged_files": self.upload_files,
                        "upload_sha256": self.upload_sha,
                    },
                    "status": "failed",
                    "error": error,
                    "error_code": error_code,
                    "stages": {
                        "stages": [
                            {
                                "id": "discover",
                                "state": "failed",
                                "diagnostic": diagnostic,
                            }
                        ]
                    },
                    "events": [
                        {"stage": "discover", "state": "running"},
                        {"stage": "discover", "state": "failed", "error": error},
                    ],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operation SET task_id=NULL, map_index=NULL"
                " WHERE run_id=%s AND kind='dispatch'",
                (self.run_id,),
            )
            self.assertEqual(cursor.rowcount, 1)
            connection.commit()
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="discover",
            state="failed",
            receipt={
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "pipeline_id": "solidworks-to-urdf",
                "run_id": self.run_id,
                "stage": "discover",
                "state": "failed",
                "passed": False,
                "error": f"AirflowFailException: SolidWorks job {self.run_id} failed: {error}",
                "error_code": error_code,
                "detail": diagnostic,
            },
        )
        self.store.close_admission(reason="terminal native dispatch proof", actor="reconcile-test")
        binding = {
            "operation_id": self.native_operation_id,
            "task_id": "discover",
            "map_index": -1,
        }
        self.bound_reconcile = {
            "operation_key": f"operatorctl-reconcile:{self.run_id}:discover:failed",
            "evidence": [str(self.log)],
            "params_digest": hashlib.sha256(
                json.dumps(
                    {"outcome": "failed", "evidence": [str(self.log)], "binding": binding},
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            "binding": binding,
            "native_receipt": {"path": str(self.log), "native_run_id": self.run_id},
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

    def test_bound_native_discover_refuses_missing_or_corrupt_freeze_bytes(self) -> None:
        for name, mutate in (
            ("missing", lambda: shutil.rmtree(self.sealed_root)),
            (
                "corrupt",
                lambda: (self.sealed_root / "input" / "cad" / "cad" / "robot.SLDASM").write_bytes(
                    b"corrupt-sealed-bytes"
                ),
            ),
        ):
            with self.subTest(name=name):
                self._prepare_bound_native_discover()
                mutate()
                before = self._bound_rows()
                with self.assertRaisesRegex(Exception, "sealed bytes"):
                    self.store.confirm_stage(
                        run_id=self.run_id,
                        stage="discover",
                        state="failed",
                        reconcile=self.bound_reconcile,
                    )
                self.assertEqual(self._bound_rows(), before, "unsealed or changed freeze performs zero writes")

    def test_bound_native_reconcile_repairs_the_legacy_freeze_dispatch_binding(self) -> None:
        self._prepare_bound_native_discover()
        import psycopg2
        from description_pipeline.platform.store import PlatformStoreError

        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operation SET task_id=NULL, map_index=NULL"
                " WHERE run_id=%s AND kind='dispatch'",
                (self.run_id,),
            )
            connection.commit()
        self.store.close_admission(reason="legacy dispatch proof", actor="reconcile-test")
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="discover",
            state="failed",
            reconcile=self.bound_reconcile,
        )
        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT task_id, map_index FROM operation WHERE run_id=%s AND kind='dispatch'",
                (self.run_id,),
            )
            self.assertEqual(cursor.fetchone(), ("freeze", -1))
            cursor.execute(
                "SELECT bucket FROM lifecycle_projection WHERE run_id=%s",
                (self.run_id,),
            )
            self.assertEqual({row[0] for row in cursor.fetchall()}, {"terminal"})
            cursor.execute(
                "SELECT count(*) FROM run_event WHERE run_id=%s AND kind='binding'"
                " AND payload->>'binding_kind'='dispatch'",
                (self.run_id,),
            )
            self.assertEqual(cursor.fetchone()[0], 1)
        before = self._bound_rows()
        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE operation SET task_id=NULL, map_index=NULL"
                " WHERE run_id=%s AND kind='dispatch'",
                (self.run_id,),
            )
            connection.commit()
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="discover",
            state="failed",
            reconcile=self.bound_reconcile,
        )
        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT task_id, map_index FROM operation WHERE run_id=%s AND kind='dispatch'",
                (self.run_id,),
            )
            self.assertEqual(cursor.fetchone(), ("freeze", -1))
        before = self._bound_rows()
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="discover",
            state="failed",
            reconcile=self.bound_reconcile,
        )
        self.assertEqual(self._bound_rows(), before, "the repeated legacy repair is a no-op")
        sealed_file = self.sealed_root / "input" / self.main_assembly
        original = sealed_file.read_bytes()
        sealed_file.write_bytes(original + b"corrupt")
        try:
            with self.assertRaises(PlatformStoreError):
                self.store.confirm_stage(
                    run_id=self.run_id,
                    stage="discover",
                    state="failed",
                    reconcile=self.bound_reconcile,
                )
        finally:
            sealed_file.write_bytes(original)
        self.assertEqual(self._bound_rows(), before, "a changed sealed freeze is refused with zero writes")
        changed_log = self.log.with_name("changed-" + self.log.name)
        changed_log.write_bytes(self.log.read_bytes() + b"\n")
        conflicting = dict(self.bound_reconcile)
        conflicting["evidence"] = [str(changed_log)]
        with self.assertRaises(PlatformStoreError):
            self.store.confirm_stage(
                run_id=self.run_id,
                stage="discover",
                state="failed",
                reconcile=conflicting,
            )
        self.assertEqual(self._bound_rows(), before, "changed repair evidence is refused with zero writes")

    def test_terminal_native_discover_repairs_the_freeze_dispatch_once(self) -> None:
        import psycopg2

        self._prepare_terminal_native_discover_dispatch()
        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state, receipt, checkpoint_uri, original_executed_at, updated_at"
                " FROM stage WHERE run_id=%s AND stage='discover'",
                (self.run_id,),
            )
            original_stage = cursor.fetchone()
            cursor.execute(
                "SELECT state, task_id, map_index FROM operation"
                " WHERE run_id=%s AND kind='dispatch'",
                (self.run_id,),
            )
            self.assertEqual(cursor.fetchone(), ("succeeded", None, None))
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="discover",
            state="failed",
            reconcile=self.bound_reconcile,
        )
        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state, receipt, checkpoint_uri, original_executed_at, updated_at"
                " FROM stage WHERE run_id=%s AND stage='discover'",
                (self.run_id,),
            )
            self.assertEqual(cursor.fetchone(), original_stage, "the terminal discover row remains unchanged")
            cursor.execute(
                "SELECT state, task_id, map_index FROM operation"
                " WHERE run_id=%s AND kind='dispatch'",
                (self.run_id,),
            )
            self.assertEqual(cursor.fetchone(), ("succeeded", "freeze", -1))
            cursor.execute(
                "SELECT bucket FROM lifecycle_projection WHERE run_id=%s",
                (self.run_id,),
            )
            self.assertEqual({row[0] for row in cursor.fetchall()}, {"terminal"})
            cursor.execute(
                "SELECT payload FROM run_event WHERE run_id=%s AND kind='binding'"
                " AND payload->>'binding_kind'='dispatch'",
                (self.run_id,),
            )
            events = cursor.fetchall()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][0]["task_id"], "freeze")
            self.assertEqual(events[0][0]["map_index"], -1)
            self.assertEqual(events[0][0]["native_receipt_sha256"], _sha256(self.log))
        before = self._bound_rows()
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="discover",
            state="failed",
            reconcile=self.bound_reconcile,
        )
        self.assertEqual(self._bound_rows(), before, "the identical terminal repair appends no event")

    def test_new_initial_dispatch_has_no_unknown_on_completion_or_failure(self) -> None:
        import psycopg2
        from types import SimpleNamespace

        from airflow.models import DagBag
        from airflow.models.dagrun import DagRun
        from airflow.models.taskinstance import TaskInstance
        from airflow.settings import Session
        from description_pipeline.delivery import BUNDLE_SCHEMA
        from description_pipeline.io import digest as pipeline_digest
        from description_pipeline.io import read_data
        from description_pipeline.orchestration.airflow_client import native_run_id
        from description_pipeline.orchestration.portal import _initial_dispatch
        from tests.v1.test_platform_store import build_sealed_delivery

        bag = DagBag(str(ROOT / "deploy/airflow/dags"))
        module = __import__(
            bag.dags[DAG_ID].task_dict["freeze"].python_callable.__module__,
            fromlist=["_bind_initial_dispatch"],
        )
        for outcome in ("completed", "failed"):
            with self.subTest(outcome=outcome):
                self.setUp()
                dag_run_id = f"portal-new-start-{outcome}-{uuid.uuid4().hex[:8]}"
                run_id = native_run_id(dag_run_id)
                sealed_root = self.evidence / "store" / "sealed" / run_id / "freeze"
                subject, artifacts, upload_sha, _native_handoff = build_sealed_delivery(sealed_root)
                upload_files = dict(read_data(sealed_root / "input" / "handoff.json")["package_files"])
                main_assembly = "cad/cad/robot.SLDASM"
                self.store.admit_run(
                    request_id=f"portal-{dag_run_id}",
                    run_id=run_id,
                    principal="cli_reconcile:tenant:operator",
                    target_hardware="m3.0",
                    main_assembly=main_assembly,
                    configuration={"purpose": "inspection_preview"},
                    baseline={
                        "handoff_sha256": upload_sha,
                        "files": len(upload_files),
                        "bytes": sum((sealed_root / "input" / name).stat().st_size for name in upload_files),
                        "folder": "M3.0.3_20261016",
                    },
                    dispatch=_initial_dispatch(
                        dag_id=DAG_ID,
                        dag_run_id=dag_run_id,
                        stage="freeze",
                        params_digest=pipeline_digest(
                            {
                                "handoff_path": str(sealed_root),
                                "main_assembly": main_assembly,
                                "target_hardware": "m3.0",
                            }
                        ),
                    ),
                )
                with Session() as session:
                    session.add(
                        DagRun(
                            dag_id=DAG_ID,
                            run_id=dag_run_id,
                            run_type="manual",
                            state="success" if outcome == "completed" else "failed",
                        )
                    )
                    session.execute(
                        TaskInstance.__table__.insert().values(
                            dag_id=DAG_ID,
                            run_id=dag_run_id,
                            task_id="freeze",
                            map_index=-1,
                            state="success" if outcome == "completed" else "failed",
                            try_number=1,
                            pool="default_pool",
                            pool_slots=1,
                        )
                    )
                    session.commit()
                module._bind_initial_dispatch(
                    self.store,
                    run_id=run_id,
                    context={"dag_run": SimpleNamespace(run_id=dag_run_id)},
                )
                with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT task_id, map_index FROM operation"
                        " WHERE run_id=%s AND kind='dispatch'",
                        (run_id,),
                    )
                    self.assertEqual(cursor.fetchone(), ("freeze", -1))
                self.store.start_run(run_id)
                if outcome == "completed":
                    receipt = {
                        "schema_version": BUNDLE_SCHEMA,
                        "pipeline_id": "solidworks-to-urdf",
                        "run_id": run_id,
                        "stage": "freeze",
                        "state": "frozen",
                        "passed": False,
                        "handoff_sha256": upload_sha,
                        "subject_sha256": subject,
                        "tool": read_data(sealed_root / "tool.json"),
                        "artifacts": artifacts,
                    }
                    self.store.confirm_stage(
                        run_id=run_id,
                        stage="freeze",
                        state="completed",
                        receipt=receipt,
                        checkpoint_uri=f"store://sealed/{run_id}/freeze",
                    )
                else:
                    self.store.confirm_stage(
                        run_id=run_id,
                        stage="freeze",
                        state="failed",
                        receipt={
                            "schema_version": BUNDLE_SCHEMA,
                            "pipeline_id": "solidworks-to-urdf",
                            "run_id": run_id,
                            "stage": "freeze",
                            "state": "failed",
                            "passed": False,
                            "error": "AirflowFailException: controlled freeze failure",
                        },
                    )
                with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT object_kind, object_id, bucket FROM lifecycle_projection WHERE run_id=%s",
                        (run_id,),
                    )
                    rows = cursor.fetchall()
                    self.assertTrue(rows)
                    self.assertEqual({row[2] for row in rows}, {"terminal"})
                    cursor.execute(
                        "SELECT bucket FROM lifecycle_projection"
                        " WHERE run_id=%s AND object_kind='scheduling'",
                        (run_id,),
                    )
                    self.assertEqual(cursor.fetchall(), [("terminal",)])

    def test_terminal_dispatch_repair_refuses_partial_foreign_or_ambiguous_facts(self) -> None:
        import psycopg2
        from airflow.models.taskinstance import TaskInstance
        from airflow.settings import Session
        from description_pipeline.platform.store import PlatformStoreError

        for name in (
            "partial_binding",
            "binding_without_event",
            "extra_freeze_task",
            "extra_dispatch",
            "foreign_native",
            "foreign_detail",
        ):
            with self.subTest(name=name):
                self.setUp()
                self._prepare_terminal_native_discover_dispatch()
                with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
                    if name == "partial_binding":
                        cursor.execute(
                            "UPDATE operation SET task_id='freeze'"
                            " WHERE run_id=%s AND kind='dispatch'",
                            (self.run_id,),
                        )
                    elif name == "binding_without_event":
                        cursor.execute(
                            "UPDATE operation SET task_id='freeze', map_index=-1"
                            " WHERE run_id=%s AND kind='dispatch'",
                            (self.run_id,),
                        )
                    elif name == "extra_dispatch":
                        cursor.execute(
                            "INSERT INTO operation"
                            " (operation_id, run_id, stage, kind, state, operation_key, params_digest,"
                            " dag_id, dag_run_id, task_id, map_index)"
                            " VALUES (%s, %s, 'freeze', 'dispatch', 'queued', %s, %s, %s, %s, NULL, NULL)",
                            (
                                str(uuid.uuid4()),
                                self.run_id,
                                f"{self.run_id}/dispatch-extra:0",
                                "b" * 64,
                                DAG_ID,
                                self.dag_run_id,
                            ),
                        )
                    connection.commit()
                if name == "extra_freeze_task":
                    with Session() as session:
                        session.execute(
                            TaskInstance.__table__.insert().values(
                                dag_id=DAG_ID,
                                run_id=self.dag_run_id,
                                task_id="freeze",
                                map_index=0,
                                state="success",
                                try_number=1,
                                pool="default_pool",
                                pool_slots=1,
                            )
                        )
                        session.commit()
                reconcile = self.bound_reconcile
                if name == "foreign_native":
                    foreign = dict(json.loads(self.log.read_text(encoding="utf-8")))
                    foreign["error"] = "SelectionError: a different failure"
                    foreign["events"][-1]["error"] = foreign["error"]
                    path = self.evidence / f"foreign-{uuid.uuid4().hex[:8]}.json"
                    path.write_text(json.dumps(foreign) + "\n", encoding="utf-8")
                    reconcile = {
                        **self.bound_reconcile,
                        "evidence": [str(path)],
                        "native_receipt": {"path": str(path), "native_run_id": self.run_id},
                    }
                elif name == "foreign_detail":
                    foreign = dict(json.loads(self.log.read_text(encoding="utf-8")))
                    stage_rows = foreign["stages"]["stages"]
                    stage_rows[0]["diagnostic"] = {
                        **stage_rows[0]["diagnostic"],
                        "problems": [
                            {
                                **stage_rows[0]["diagnostic"]["problems"][0],
                                "instance": "other-instance",
                            }
                        ],
                    }
                    path = self.evidence / f"foreign-{uuid.uuid4().hex[:8]}.json"
                    path.write_text(json.dumps(foreign) + "\n", encoding="utf-8")
                    reconcile = {
                        **self.bound_reconcile,
                        "evidence": [str(path)],
                        "native_receipt": {"path": str(path), "native_run_id": self.run_id},
                    }
                before = self._bound_rows()
                with self.assertRaises(PlatformStoreError):
                    self.store.confirm_stage(
                        run_id=self.run_id,
                        stage="discover",
                        state="failed",
                        reconcile=reconcile,
                    )
                self.assertEqual(self._bound_rows(), before, "a refused terminal repair performs zero writes")

    def test_legacy_dispatch_repair_refuses_unproven_airflow_identity(self) -> None:
        from description_pipeline.platform.store import PlatformStoreError

        variants = (
            ("missing_dag_run", "UPDATE operation SET dag_run_id='missing-run' WHERE run_id=%s AND kind='dispatch'"),
            ("wrong_dag", "UPDATE operation SET dag_id='another-dag' WHERE run_id=%s AND kind='dispatch'"),
            (
                "wrong_target",
                "UPDATE operation SET operation_key=replace(operation_key, '/freeze:0', '/discover:0')"
                " WHERE run_id=%s AND kind='dispatch'",
            ),
            ("operation_failed", "UPDATE operation SET state='failed' WHERE run_id=%s AND kind='dispatch'"),
            ("extra_mapped_freeze", None),
        )
        import psycopg2

        for name, sql in variants:
            with self.subTest(name=name):
                self._prepare_bound_native_discover()
                with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE operation SET task_id=NULL, map_index=NULL"
                        " WHERE run_id=%s AND kind='dispatch'",
                        (self.run_id,),
                    )
                    if sql is None:
                        from airflow.models.taskinstance import TaskInstance
                        from airflow.settings import Session

                        with Session() as session:
                            session.execute(
                                TaskInstance.__table__.insert().values(
                                    dag_id=DAG_ID,
                                    run_id=self.dag_run_id,
                                    task_id="freeze",
                                    map_index=0,
                                    state="success",
                                    try_number=1,
                                    pool="default_pool",
                                    pool_slots=1,
                                )
                            )
                            session.commit()
                    else:
                        cursor.execute(sql, (self.run_id,))
                    if name == "wrong_target":
                        cursor.execute(
                            "SELECT operation_key FROM operation WHERE run_id=%s AND kind='dispatch'",
                            (self.run_id,),
                        )
                        self.assertTrue(cursor.fetchone()[0].endswith("/discover:0"))
                    connection.commit()
                self.store.close_admission(reason="legacy dispatch proof", actor="reconcile-test")
                before = self._bound_rows()
                with self.assertRaises(PlatformStoreError):
                    self.store.confirm_stage(
                        run_id=self.run_id,
                        stage="discover",
                        state="failed",
                        reconcile=self.bound_reconcile,
                    )
                self.assertEqual(self._bound_rows(), before, "unproven dispatch identity performs zero writes")

    def test_replay_legacy_dispatch_repair_rechecks_admitted_identity(self) -> None:
        from description_pipeline.platform.store import PlatformStoreError
        import psycopg2

        variants = (
            (
                "freeze_handoff_mismatch",
                "UPDATE stage SET receipt=jsonb_set(receipt, '{handoff_sha256}', to_jsonb(%s::text))"
                " WHERE run_id=%s AND stage='freeze'",
            ),
            (
                "request_main_mismatch",
                "UPDATE request SET main_assembly='other.SLDASM' WHERE request_id=%s",
            ),
            (
                "request_file_count_mismatch",
                "UPDATE request SET baseline=jsonb_set(baseline, '{files}', to_jsonb(999))"
                " WHERE request_id=%s",
            ),
        )
        for name, sql in variants:
            with self.subTest(name=name):
                self._prepare_bound_native_discover()
                with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE operation SET task_id=NULL, map_index=NULL"
                        " WHERE run_id=%s AND kind='dispatch'",
                        (self.run_id,),
                    )
                    self.assertEqual(cursor.rowcount, 1)
                    connection.commit()
                self.store.close_admission(reason="legacy replay identity proof", actor="reconcile-test")
                self.store.confirm_stage(
                    run_id=self.run_id,
                    stage="discover",
                    state="failed",
                    reconcile=self.bound_reconcile,
                )
                with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE operation SET task_id=NULL, map_index=NULL"
                        " WHERE run_id=%s AND kind='dispatch'",
                        (self.run_id,),
                    )
                    self.assertEqual(cursor.rowcount, 1)
                    if name == "freeze_handoff_mismatch":
                        params = ("0" * 64, self.run_id)
                    else:
                        params = (f"portal-{self.dag_run_id}",)
                    cursor.execute(sql, params)
                    self.assertEqual(cursor.rowcount, 1)
                    if name == "freeze_handoff_mismatch":
                        cursor.execute(
                            "SELECT receipt->>'handoff_sha256' FROM stage WHERE run_id=%s AND stage='freeze'",
                            (self.run_id,),
                        )
                        self.assertEqual(cursor.fetchone()[0], "0" * 64)
                    elif name == "request_main_mismatch":
                        cursor.execute(
                            "SELECT main_assembly FROM request WHERE request_id=%s",
                            (f"portal-{self.dag_run_id}",),
                        )
                        self.assertEqual(cursor.fetchone()[0], "other.SLDASM")
                    else:
                        cursor.execute(
                            "SELECT baseline->>'files' FROM request WHERE request_id=%s",
                            (f"portal-{self.dag_run_id}",),
                        )
                        self.assertEqual(cursor.fetchone()[0], "999")
                    connection.commit()
                before = self._bound_rows()
                with self.assertRaises(PlatformStoreError):
                    self.store.confirm_stage(
                        run_id=self.run_id,
                        stage="discover",
                        state="failed",
                        reconcile=self.bound_reconcile,
                    )
                self.assertEqual(self._bound_rows(), before, "identity mismatch performs zero writes")

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

    def test_duplicate_registration_binds_the_initial_dispatch_intent(self) -> None:
        operation_key = f"portal-{self.dag_run_id}/freeze:0"
        from description_pipeline.platform.store import OperationConflictError

        with self.assertRaises(OperationConflictError):
            self.store.register_operation(
                run_id=self.run_id,
                stage="freeze",
                kind="dispatch",
                operation_key=operation_key,
                params_digest="a" * 64,
                dispatch={
                    "dag_id": DAG_ID,
                    "dag_run_id": "another-run",
                    "task_id": "freeze",
                    "map_index": -1,
                },
            )
        with self.assertRaises(OperationConflictError):
            self.store.register_operation(
                run_id=self.run_id,
                stage="freeze",
                kind="dispatch",
                operation_key=operation_key,
                params_digest="a" * 64,
                dispatch={
                    "dag_id": DAG_ID,
                    "dag_run_id": self.dag_run_id,
                    "task_id": "discover",
                    "map_index": -1,
                },
            )
        import psycopg2

        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE operation SET task_id='freeze' WHERE operation_key=%s", (operation_key,))
            connection.commit()
        with self.assertRaises(OperationConflictError):
            self.store.register_operation(
                run_id=self.run_id,
                stage="freeze",
                kind="dispatch",
                operation_key=operation_key,
                params_digest="a" * 64,
                dispatch={
                    "dag_id": DAG_ID,
                    "dag_run_id": self.dag_run_id,
                    "task_id": "freeze",
                    "map_index": -1,
                },
            )
        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE operation SET task_id=NULL, map_index=NULL WHERE operation_key=%s", (operation_key,))
            connection.commit()
        self.store.register_operation(
            run_id=self.run_id,
            stage="freeze",
            kind="dispatch",
            operation_key=operation_key,
            params_digest="a" * 64,
            dispatch={
                "dag_id": DAG_ID,
                "dag_run_id": self.dag_run_id,
                "task_id": "freeze",
                "map_index": -1,
            },
        )
        with psycopg2.connect(REQUIRED["platform_dsn"]) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT task_id, map_index FROM operation WHERE operation_key=%s", (operation_key,))
            self.assertEqual(cursor.fetchone(), ("freeze", -1))

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
