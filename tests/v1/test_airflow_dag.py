"""Real Airflow DAG parsing and one mocked-endpoint DAG run (requires the Airflow venv)."""

from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.v1._airflow_env import pinned_airflow_home

ROOT = Path(__file__).resolve().parents[2]
DAG_DIR = ROOT / "deploy/airflow/dags"
#: Absolute and outside the checkout, fixed before Airflow is imported.
AIRFLOW_HOME = pinned_airflow_home()

_MIGRATED = False


def migrated_airflow_home(venv: Path) -> Path:
    """Migrate the isolated home once so the pinned Airflow CLI runs for real."""
    global _MIGRATED
    if not _MIGRATED:
        env = dict(
            os.environ,
            AIRFLOW_HOME=str(AIRFLOW_HOME),
            PYTHONPATH=str(ROOT / "src"),
            AIRFLOW__CORE__LOAD_EXAMPLES="false",
        )
        migrated = subprocess.run(
            [str(venv / "bin/airflow"), "db", "migrate"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if migrated.returncode != 0:
            raise AssertionError("airflow db migrate failed:\n" + (migrated.stdout + migrated.stderr)[-3000:])
        _MIGRATED = True
    return AIRFLOW_HOME


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
        self.assertEqual(set(dag.task_ids), {"resolve_handoff", "start_job", "wait_for_job", "confirm_job"})
        self.assertEqual(set(dag.params), {"handoff_path"})
        handoff_param = dict(dag.params.items())["handoff_path"]
        self.assertEqual(handoff_param.schema["title"], "Engineering folder path")
        self.assertEqual(handoff_param.schema["type"], "string")
        from description_pipeline.stages import contract_markdown

        self.assertIn(contract_markdown(), dag.doc_md)
        self.assertTrue(all(task.doc_md for task in dag.tasks))

    def _module(self):
        spec = importlib.util.spec_from_file_location("test_solidworks_dag", DAG_DIR / "solidworks_to_urdf.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        return module

    def test_failure_stores_terminal_checks_before_raising_and_progress_logs_only_changes(self):
        from types import SimpleNamespace
        from airflow.sdk.exceptions import AirflowFailException
        from tests.v1.protocol_support import protocol_events

        module = self._module()
        request = {"run_id": "protocol-control", "package": "control", "handoff_sha256": "b" * 64, "conn_id": "test"}
        job = {
            "request": {key: request[key] for key in ("run_id", "package", "handoff_sha256")},
            "run_id": request["run_id"],
            "status": "running",
            "events": protocol_events(stages=("freeze",)),
        }

        class TaskInstance:
            def __init__(self):
                self.values = {}

            def xcom_pull(self, *, task_ids, key):
                return self.values.get(key)

            def xcom_push(self, *, key, value):
                self.values[key] = value

        ti = TaskInstance()
        with patch.object(module, "_endpoint", return_value=SimpleNamespace(get_job=lambda _: job)):
            with self.assertLogs(module.log.name, level="INFO") as first:
                self.assertFalse(module._poke(request, ti=ti))
            with self.assertLogs(module.log.name, level="INFO") as unchanged:
                self.assertFalse(module._poke(request, ti=ti))
            self.assertEqual(sum("engineering stage=" in line for line in first.output), 6)
            self.assertFalse(any("engineering stage=" in line for line in unchanged.output))
            job.update(status="failed", error="Native control failed", events=protocol_events(failed_stage="capture"))
            with self.assertLogs(module.log.name, level="INFO") as terminal, self.assertRaises(AirflowFailException):
                module._poke(request, ti=ti)
            stages = {row["id"]: row["state"] for row in ti.values["engineering_stages"]["stages"]}
            self.assertEqual(stages["capture"], "failed")
            self.assertEqual(stages["generate"], "blocked")
            self.assertEqual(sum("engineering result=" in line for line in terminal.output), 6)

    def test_terminal_summary_without_task_instance_warns_and_never_crashes(self):
        from types import SimpleNamespace
        from tests.v1.protocol_support import protocol_events

        module = self._module()
        request = {"run_id": "protocol-control", "package": "control", "handoff_sha256": "b" * 64, "conn_id": "test"}
        job = {
            "request": {key: request[key] for key in ("run_id", "package", "handoff_sha256")},
            "run_id": request["run_id"],
            "status": "passed",
            "events": protocol_events(subject="c" * 64),
        }
        with (
            patch.object(module, "_endpoint", return_value=SimpleNamespace(get_job=lambda _: job)),
            self.assertLogs(module.log.name, level="WARNING") as captured,
        ):
            self.assertTrue(module._poke(request, ti=None))
        self.assertTrue(any("XCom was not stored" in line for line in captured.output))

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
                AIRFLOW_HOME=str(migrated_airflow_home(venv)),
                AIRFLOW__CORE__DAGS_FOLDER=str(DAG_DIR),
                PYTHONPATH=str(ROOT / "src"),
                AIRFLOW_CONN_SOLIDWORKS_WINDOWS=connection,
                SOLIDWORKS_SENSOR_MODE="poke",
                SOLIDWORKS_POLL_INTERVAL="0.2",
                SOLIDWORKS_TIMEOUT="60",
            )
            conf = json.dumps(
                {
                    "handoff_path": "handoff/m3.0",
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
            self.assertEqual(
                job["request"],
                {"run_id": job["run_id"], "package": "handoff/m3.0", "handoff_sha256": "b" * 64},
            )
            self.assertEqual(job["hardware_id"], "m3.0")
            self.assertEqual(job["repository_slug"], "example/m3.0")
            self.assertEqual(job["repository_base"], "feature/m3.0")
            self.assertEqual(server.resolved_paths, ["handoff/m3.0"])

    def test_linked_conf_binding_is_validated(self) -> None:
        from types import SimpleNamespace
        from airflow.sdk.exceptions import AirflowFailException

        module = self._module()

        def context(conf):
            return {"dag_run": SimpleNamespace(conf=conf)}

        self.assertIsNone(module._linked_conf(context({})))
        linked = module._linked_conf(context({"parent_dag_run_id": "portal-parent", "resume_from": "verify"}))
        self.assertEqual(linked["from_stage"], "verify")
        self.assertTrue(linked["parent_run"])
        for conf in (
            {"parent_dag_run_id": "portal-parent"},
            {"resume_from": "verify"},
            {"parent_dag_run_id": "portal-parent", "resume_from": "bogus"},
        ):
            with self.subTest(conf=str(conf)), self.assertRaises(AirflowFailException):
                module._linked_conf(context(conf))

    def test_linked_dag_run_derives_the_retained_upload_from_the_parent_job(self) -> None:
        from tests.v1.test_airflow_client import JOB_SCHEMA, MockEndpoint, PIPELINE_ID
        from description_pipeline.orchestration.airflow_client import native_run_id

        venv = Path(os.environ.get("AIRFLOW_VENV", sys.prefix))
        airflow = venv / "bin" / "airflow"
        self.assertTrue(airflow.is_file(), airflow)
        parent_dag_run_id = "portal-parent-20261009"
        parent_run_id = native_run_id(parent_dag_run_id)
        with MockEndpoint() as server:
            server.jobs[parent_run_id] = {
                "schema_version": JOB_SCHEMA,
                "pipeline_id": PIPELINE_ID,
                "run_id": parent_run_id,
                "status": "failed",
                "events": [{"stage": "freeze", "state": "completed", "at": "t0"}],
                "result": None,
                "error": "verification failed",
                "pokes": 0,
                "request": {"run_id": parent_run_id, "package": "handoff/m3.0", "handoff_sha256": "b" * 64},
            }
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
                AIRFLOW_HOME=str(migrated_airflow_home(venv)),
                AIRFLOW__CORE__DAGS_FOLDER=str(DAG_DIR),
                PYTHONPATH=str(ROOT / "src"),
                AIRFLOW_CONN_SOLIDWORKS_WINDOWS=connection,
                SOLIDWORKS_SENSOR_MODE="poke",
                SOLIDWORKS_POLL_INTERVAL="0.2",
                SOLIDWORKS_TIMEOUT="60",
            )
            conf = json.dumps(
                {
                    "handoff_path": "handoff/m3.0",
                    "parent_dag_run_id": parent_dag_run_id,
                    "resume_from": "verify",
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
            self.assertEqual(server.resolved_paths, [])
            children = [job for job in server.jobs.values() if job["run_id"] != parent_run_id]
            self.assertEqual(len(children), 1)
            child = children[0]
            self.assertEqual(child["status"], "passed")
            self.assertEqual(child["request"]["package"], "handoff/m3.0")
            self.assertEqual(child["request"]["handoff_sha256"], "b" * 64)
            self.assertEqual(child["request"]["resume"], {"parent_run": parent_run_id, "from_stage": "verify"})


if __name__ == "__main__":
    unittest.main()
