"""Opt-in integration proof against a real Airflow metadata database.

The harness provides ``DESCRIPTION_CONFIG_IDENTITY_DSN`` pointing at an isolated,
migrated PostgreSQL-backed Airflow database. The test writes through the real
deployment script and reads through the real BaseHook/health/DAG boundaries.
"""

from __future__ import annotations

import base64
import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from .config_identity_support import (
    activate,
    platform_document,
    run_writer_real_airflow,
)
from .legacy_c663_reader import config_from_airflow_connection as legacy_config_from_airflow_connection

ROOT = Path(__file__).resolve().parents[2]
DSN = os.environ.get("DESCRIPTION_CONFIG_IDENTITY_DSN", "").strip()
CONNECTION_TOOL = ROOT / "deploy" / "operator" / "connection_snapshot.py"
BACKUP_TOOL = ROOT / "deploy" / "operator" / "backup.py"
CONNECTION_MEMBER = "private/connection-before-promotion.json"
FERNET_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"config-identity-real-airflow").digest()).decode()


@unittest.skipUnless(DSN, "DESCRIPTION_CONFIG_IDENTITY_DSN is not configured")
class RealAirflowConfigIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_environ = dict(os.environ)
        self.temporary = tempfile.TemporaryDirectory(prefix="config-identity-airflow-")
        self.root = Path(self.temporary.name)
        self.airflow_home = self.root / "airflow-home"
        self.airflow_home.mkdir()
        self.env = dict(
            os.environ,
            AIRFLOW_HOME=str(self.airflow_home),
            AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=DSN,
            AIRFLOW__CORE__LOAD_EXAMPLES="false",
            AIRFLOW__CORE__FERNET_KEY=FERNET_KEY,
            OPERATOR_STATE=str(self.root / "state"),
            PIPELINE_STORE_ROOT=str(self.root / "state" / "runs"),
            PLATFORM_CONFIG_DIGEST="",
        )
        os.environ.update(self.env)
        from airflow.models.crypto import get_fernet

        get_fernet.cache_clear()

    def tearDown(self) -> None:
        snapshot = getattr(self, "_connection_snapshot", None)
        if snapshot is not None:
            # A failed assertion must not leave the shared real-Airflow connection
            # in the deliberately corrupted state used by this test.
            self._writer(snapshot)
        self.temporary.cleanup()
        os.environ.clear()
        os.environ.update(self.original_environ)

    def _writer(self, snapshot: Path) -> None:
        result = run_writer_real_airflow(self.root, snapshot, self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _config(self, config_digest: str | None):
        from description_pipeline.orchestration.airflow_client import config_from_airflow_connection

        return config_from_airflow_connection(
            "solidworks_windows",
            config_digest=config_digest,
            state_root=self.root / "state",
        )

    def _connection_tool(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CONNECTION_TOOL), *arguments],
            capture_output=True,
            text=True,
            env={
                **self.env,
                "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")]),
            },
            cwd=str(ROOT),
        )

    def _backup_tool(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(BACKUP_TOOL), *arguments],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(ROOT),
        )

    def _legacy_config(self):
        """Run the c663 reader against the current row through Airflow's task preset.

        ``BaseHook`` in Airflow 3 resolves connections from the task-runtime preset, not
        from an ORM session outside a worker.  Presetting the exact restored row exercises
        the old reader unchanged while keeping the test outside the scheduler.
        """

        from airflow.models.connection import Connection
        from airflow.sdk.definitions.connection import Connection as TaskConnection
        from airflow.sdk.execution_time import context as task_context
        from airflow.settings import Session

        with Session() as session:
            row = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one()
            connection = TaskConnection(
                conn_id=row.conn_id,
                conn_type=row.conn_type,
                description=row.description,
                host=row.host,
                login=row.login,
                password=row.password,
                schema=row.schema,
                port=row.port,
                extra=row.extra,
            )
        token = task_context._preset_connections.set({"solidworks_windows": connection})
        try:
            return legacy_config_from_airflow_connection("solidworks_windows")
        finally:
            task_context._preset_connections.reset(token)

    def test_cross_generation_connection_row_restore_uses_c663_reader(self) -> None:
        document_a = platform_document(self.root)
        snapshot_a, record_a = activate(self.root, "gen-1", document_a)
        record_path = self.root / "state" / "install-records" / "gen-1.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record_a, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        document_b = platform_document(self.root)
        document_b["targets"]["m3.0"]["base"] = "release-b"
        snapshot_b, _record_b = activate(self.root, "gen-2", document_b)
        receipt_dir = self.root / "state" / "promotions" / "gen-2"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt = receipt_dir / "receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.promotion/v1",
                    "install_id": "gen-2",
                    "from": str(record_path),
                    "from_sha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
                    "to": str(snapshot_b),
                    "to_sha256": hashlib.sha256(snapshot_b.read_bytes()).hexdigest(),
                    "plan": ["config-reference"],
                    "created_at": "2026-10-17T00:00:00Z",
                    "steps": [],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        from airflow.models.connection import Connection
        from airflow.settings import Session

        resolved = json.loads(snapshot_a.read_text(encoding="utf-8"))["resolved"]
        legacy = {
            "conn_type": "http",
            "description": "legacy endpoint row",
            "host": "127.0.0.1",
            "login": None,
            "password": "t" * 64,
            "schema": None,
            "port": 18765,
            "extra": json.dumps(
                {
                    "handoff_roots": [resolved["storage"]["handoff_root"]],
                    "store_root": resolved["derived"]["store_root"],
                    "repositories": resolved["derived"]["repositories"],
                    "discovery": resolved.get("discovery", {}),
                    "targets": resolved["targets"],
                },
                sort_keys=True,
            ),
        }
        try:
            with Session() as session:
                row = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one_or_none()
                if row is None:
                    row = Connection(conn_id="solidworks_windows")
                    session.add(row)
                for name, value in legacy.items():
                    setattr(row, name, value)
                session.commit()
            with Session() as session:
                encrypted_row = session.query(Connection).filter(
                    Connection.conn_id == "solidworks_windows"
                ).one()
                self.assertTrue(encrypted_row.is_encrypted)
                self.assertEqual(encrypted_row.password, legacy["password"])
                self.assertNotEqual(encrypted_row._password, legacy["password"])

            private = receipt_dir / "evidence" / "private" / "connection-before-promotion.json"
            captured = self._connection_tool(
                "capture",
                "--file",
                str(private),
                "--receipt",
                str(receipt),
            )
            self.assertEqual(captured.returncode, 0, captured.stdout + captured.stderr)
            self._writer(snapshot_b)
            with Session() as session:
                cleared = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one()
                self.assertIn(cleared.password, ("", None))
                self.assertIn(cleared._password, ("", None))
                self.assertFalse(cleared.is_encrypted)
            with self.assertRaisesRegex(Exception, "has no host"):
                self._legacy_config()
            self.assertEqual(self._config(None).config_digest, hashlib.sha256(snapshot_b.read_bytes()).hexdigest())

            staged = receipt_dir / "backup"
            (staged / "private").mkdir(parents=True, exist_ok=True)
            (staged / CONNECTION_MEMBER).write_bytes(private.read_bytes())
            (staged / CONNECTION_MEMBER).chmod(0o600)
            sealed = receipt_dir / "backup.tar.enc"
            sealed.write_bytes(b"sealed backup bytes")
            key = self.root / "state" / "secrets" / "backup.key"
            manifest = receipt_dir / "evidence" / "backup-manifest.json"
            self.assertEqual(self._backup_tool("key", "--path", str(key)).returncode, 0)
            written = self._backup_tool(
                "manifest",
                "--source",
                str(staged),
                "--sealed",
                str(sealed),
                "--key",
                str(key),
                "--out",
                str(manifest),
            )
            self.assertEqual(written.returncode, 0, written.stdout + written.stderr)

            restored = self._connection_tool(
                "restore",
                "--file",
                str(private),
                "--receipt",
                str(receipt),
                "--manifest",
                str(manifest),
                "--sealed",
                str(sealed),
                "--member",
                CONNECTION_MEMBER,
            )
            self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
            self.assertIn("restored exactly", restored.stdout)
            with Session() as session:
                restored_row = session.query(Connection).filter(
                    Connection.conn_id == "solidworks_windows"
                ).one()
                self.assertEqual(restored_row.password, legacy["password"])
                self.assertTrue(restored_row.is_encrypted)
                self.assertNotEqual(restored_row._password, legacy["password"])
            legacy_config = self._legacy_config()
            self.assertEqual(legacy_config.base_url, "http://127.0.0.1:18765")
            self.assertEqual(legacy_config.token, "t" * 64)
            with self.assertRaisesRegex(Exception, "config_identity_legacy"):
                self._config(None)
        finally:
            self._writer(snapshot_b)
            with Session() as session:
                row = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one()
            self.assertEqual(row.host, "")
            self.assertIsNone(row.port)
            self.assertIn(row.password, ("", None))

    def test_real_airflow_connection_switches_and_frozen_runtime_contract(self) -> None:
        document_a = platform_document(self.root)
        document_a["targets"]["m3.0"]["base"] = "release-a"
        snapshot_a, _record = activate(self.root, "gen-1", document_a)
        self._writer(snapshot_a)
        digest_a = self._config(None).config_digest

        health = subprocess.run(
            [
                sys.executable,
                "-m",
                "description_pipeline.orchestration.airflow_client",
                "--conn-id",
                "solidworks_windows",
                "--state-root",
                str(self.root / "state"),
            ],
            capture_output=True,
            text=True,
            env={**self.env, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)])},
            cwd=str(ROOT),
        )
        self.assertEqual(health.returncode, 0, health.stderr)
        self.assertIn(f"config_digest={digest_a}", health.stdout)

        document_b = platform_document(self.root)
        document_b["targets"]["m3.0"]["base"] = "release-b"
        snapshot_b, _record = activate(self.root, "gen-2", document_b)
        self._connection_snapshot = snapshot_b
        self._writer(snapshot_b)
        self.assertEqual(self._config(None).targets["m3.0"]["base"], "release-b")
        frozen = self._config(digest_a)
        self.assertEqual(frozen.targets["m3.0"]["base"], "release-a")

        from airflow.models.connection import Connection
        from airflow.settings import Session
        from description_pipeline.orchestration import portal as portal_module

        with Session() as session:
            row = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one()
        self.assertEqual(row.host, "")
        self.assertIsNone(row.port)
        self.assertIn(row.password, ("", None))
        reference = json.loads(row.extra)["config_identity"]
        self.assertEqual(reference["path"], str(snapshot_b))
        self.assertEqual(portal_module._verified_admission_config_digest(), self._config(None).config_digest)

        with Session() as session:
            row = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one()
            row.host = "127.0.0.1"
            row.port = 8765
            row.password = "legacy-secret"
            row.extra = json.dumps({"store_root": str(self.root / "legacy")})
            session.commit()
        with self.assertRaisesRegex(
            portal_module.PortalError,
            "Platform configuration identity is unavailable; submission was not admitted",
        ):
            portal_module._verified_admission_config_digest()
        self._writer(snapshot_b)
        with Session() as session:
            row = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one()
            session.delete(row)
            session.commit()
        with self.assertRaisesRegex(
            portal_module.PortalError,
            "Platform configuration identity is unavailable; submission was not admitted",
        ):
            portal_module._verified_admission_config_digest()
        self._writer(snapshot_b)

        dag_path = ROOT / "deploy/airflow/dags/solidworks_to_urdf.py"
        spec = importlib.util.spec_from_file_location("config_identity_real_dag", dag_path)
        dag_module = importlib.util.module_from_spec(spec)
        self.env["PLATFORM_CONFIG_DIGEST"] = self._config(None).config_digest
        with mock.patch.dict(os.environ, self.env, clear=False):
            spec.loader.exec_module(dag_module)
            paths = dag_module._work_paths(
                "solidworks_windows", "00000000-0000-4000-8000-000000000001", digest_a
            )
        snapshot_document = json.loads(snapshot_a.read_text(encoding="utf-8"))
        self.assertEqual(
            paths.run_dir,
            Path(snapshot_document["resolved"]["derived"]["store_root"]) / "00000000-0000-4000-8000-000000000001",
        )

        activate(self.root, "gen-1", document_a)
        self._writer(snapshot_a)
        self.assertEqual(self._config(None).targets["m3.0"]["base"], "release-a")
        self.assertEqual(self._config(digest_a).config_digest, digest_a)


if __name__ == "__main__":
    unittest.main()
