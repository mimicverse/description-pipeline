"""Real Airflow DAG parsing and one mocked-endpoint DAG run (requires the Airflow venv)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DAG_DIR = ROOT / "deploy/airflow/dags"
AIRFLOW_HOME = Path(os.environ.get("AIRFLOW_HOME", ROOT / "deploy/airflow/home"))

try:
    from airflow.models import DagBag

    AIRFLOW_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only without Airflow installed
    AIRFLOW_AVAILABLE = False


@unittest.skipUnless(AIRFLOW_AVAILABLE, "Airflow is not installed in this interpreter")
class DagTests(unittest.TestCase):
    def test_dag_parses(self) -> None:
        bag = DagBag(str(DAG_DIR))
        self.assertEqual(bag.import_errors, {})
        dag = bag.dags["solidworks_to_urdf"]
        self.assertIsNone(dag.schedule)
        self.assertEqual(set(dag.task_ids), {"validate_request", "start_job", "wait_for_job", "confirm_job"})

    def test_dag_run_against_mock_endpoint(self) -> None:
        from tests.v1.test_airflow_client import MockEndpoint

        venv = Path(os.environ.get("AIRFLOW_VENV", sys.prefix))
        airflow = venv / "bin" / "airflow"
        self.assertTrue(airflow.is_file(), airflow)
        with MockEndpoint() as server:
            connection = json.dumps(
                {
                    "conn_type": "http",
                    "host": "127.0.0.1",
                    "port": server.server.server_address[1],
                    "password": "test-token",
                }
            )
            env = dict(
                os.environ,
                AIRFLOW_HOME=str(AIRFLOW_HOME),
                PYTHONPATH=str(ROOT / "src"),
                AIRFLOW_CONN_SOLIDWORKS_WINDOWS=connection,
                SOLIDWORKS_SENSOR_MODE="poke",
                SOLIDWORKS_POLL_INTERVAL="0.2",
                SOLIDWORKS_TIMEOUT="60",
            )
            conf = json.dumps(
                {
                    "package": "handoff/m3.0",
                    "revision_sha256": "a" * 64,
                    "target": "local",
                    "repository_slug": "example/m3.0",
                    "base": "feature/m3.0",
                }
            )
            result = subprocess.run(
                [str(airflow), "dags", "test", "solidworks_to_urdf", "2026-01-01", "--conf", conf],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertEqual(len(server.jobs), 1)
            job = next(iter(server.jobs.values()))
            self.assertEqual(job["status"], "passed")
            self.assertEqual(job["request"]["package"], "handoff/m3.0")


if __name__ == "__main__":
    unittest.main()
