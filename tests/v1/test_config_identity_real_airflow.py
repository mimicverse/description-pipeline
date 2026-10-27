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
from itertools import pairwise
from pathlib import Path
from unittest import mock

from .config_identity_support import (
    activate,
    platform_document,
    run_writer_real_airflow,
)

ROOT = Path(__file__).resolve().parents[2]
DSN = os.environ.get("DESCRIPTION_CONFIG_IDENTITY_DSN", "").strip()
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
            PLATFORM_CONFIG_DIGEST="",
        )
        os.environ.update(self.env)
        from airflow.models.crypto import get_fernet

        get_fernet.cache_clear()

    def tearDown(self) -> None:
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

        try:
            with Session() as session:
                row = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one()
                row.host = "127.0.0.1"
                row.port = 8765
                row.password = "legacy-secret"
                row.extra = json.dumps({"legacy": True})
                session.commit()
            with self.assertRaisesRegex(
                portal_module.PortalError,
                "Platform configuration identity is unavailable; submission was not admitted",
            ):
                portal_module._verified_admission_config_digest()
        finally:
            # Restore while generation B is still the active record; the test
            # switches back to generation A later in its normal flow.
            self._writer(snapshot_b)
        try:
            with Session() as session:
                row = session.query(Connection).filter(Connection.conn_id == "solidworks_windows").one()
                session.delete(row)
                session.commit()
            with self.assertRaisesRegex(
                portal_module.PortalError,
                "Platform configuration identity is unavailable; submission was not admitted",
            ):
                portal_module._verified_admission_config_digest()
        finally:
            self._writer(snapshot_b)

        dag_path = ROOT / "deploy/airflow/dags/solidworks_to_urdf.py"
        spec = importlib.util.spec_from_file_location("config_identity_real_dag", dag_path)
        dag_module = importlib.util.module_from_spec(spec)
        self.env["PLATFORM_CONFIG_DIGEST"] = self._config(None).config_digest
        with mock.patch.dict(os.environ, self.env, clear=False):
            spec.loader.exec_module(dag_module)
        from description_pipeline.orchestration.airflow_stage_operator import StageTaskOperator

        dag = dag_module.solidworks_to_urdf_dag
        self.assertEqual(dag.dag_id, "solidworks_to_urdf")
        self.assertEqual(
            set(dag.task_ids),
            {"freeze", "discover", "capture", "generate", "verify", "publish"},
        )
        for task in dag.tasks:
            self.assertIsInstance(task, StageTaskOperator)
            self.assertEqual(task.stage, task.task_id)
        chain = ("freeze", "discover", "capture", "generate", "verify", "publish")
        for upstream, downstream in pairwise(chain):
            self.assertEqual(dag.get_task(downstream).upstream_task_ids, {upstream})
        self.assertFalse(hasattr(dag_module, "_work_paths"))
        activate(self.root, "gen-1", document_a)
        self._writer(snapshot_a)
        self.assertEqual(self._config(None).targets["m3.0"]["base"], "release-a")
        self.assertEqual(self._config(digest_a).config_digest, digest_a)


if __name__ == "__main__":
    unittest.main()
