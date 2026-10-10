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
        self.assertEqual(
            set(dag.task_ids),
            {
                "resolve_handoff",
                "start_job",
                "wait_for_job",
                "fetch_capture",
                "run_generate",
                "run_verify",
                "run_publish",
                "confirm_job",
            },
        )
        edges = {task_id: set(task.upstream_task_ids) for task_id, task in dag.task_dict.items()}
        self.assertEqual(edges["start_job"], {"resolve_handoff"})
        self.assertEqual(edges["wait_for_job"], {"start_job"})
        self.assertEqual(edges["fetch_capture"], {"start_job", "wait_for_job"})
        self.assertEqual(edges["run_generate"], {"fetch_capture"})
        self.assertEqual(edges["run_verify"], {"run_generate"})
        self.assertEqual(edges["run_publish"], {"run_verify"})
        self.assertEqual(edges["confirm_job"], {"run_publish"})
        self.assertEqual(set(dag.params), {"handoff_path", "main_assembly"})
        handoff_param = dict(dag.params.items())["handoff_path"]
        self.assertEqual(handoff_param.schema["title"], "Engineering folder path")
        self.assertEqual(handoff_param.schema["type"], "string")
        assembly_param = dict(dag.params.items())["main_assembly"]
        self.assertEqual(assembly_param.schema["title"], "Delivered main assembly")
        self.assertEqual(assembly_param.schema["type"], "string")
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
        from airflow.sdk.exceptions import AirflowFailException

        module = self._module()
        dag = module.dag
        with MockEndpoint() as server:
            connection = json.dumps(
                {
                    "conn_type": "http",
                    "host": "127.0.0.1",
                    "port": server.server.server_address[1],
                    "password": "test-token",
                }
            )
            request = {
                "run_id": module.native_run_id("dag-run-native-half"),
                "package": "handoff/m3.0",
                "handoff_sha256": "b" * 64,
                "conn_id": "solidworks_windows",
            }
            with patch.dict(os.environ, {"AIRFLOW_CONN_SOLIDWORKS_WINDOWS": connection}):
                started = dag.task_dict["start_job"].python_callable(request)
                self.assertIn(started["status"], {"queued", "running"})
                polled = False
                for _ in range(8):
                    if module._poke(started):
                        polled = True
                        break
                self.assertTrue(polled)
                with self.assertRaises(AirflowFailException):
                    # The portable half only starts from the native_complete boundary;
                    # the full split is covered by test_split_workflow/test_linux_split.
                    dag.task_dict["fetch_capture"].python_callable(started)
            self.assertEqual(len(server.jobs), 1)
            job = next(iter(server.jobs.values()))
            self.assertEqual(
                job["request"],
                {"run_id": request["run_id"], "package": "handoff/m3.0", "handoff_sha256": "b" * 64},
            )
            self.assertEqual(job["hardware_id"], "m3.0")
            self.assertEqual(job["repository_slug"], "example/m3.0")
            self.assertEqual(job["repository_base"], "feature/m3.0")

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

    def test_linked_attempt_selection_rules(self) -> None:
        from airflow.sdk.exceptions import AirflowFailException
        from description_pipeline.orchestration.airflow_client import EndpointProtocolError

        module = self._module()
        self.assertIsNone(module._linked_selection(None, None))
        self.assertIsNone(module._linked_selection(None, ""))
        self.assertEqual("parent.SLDASM", module._linked_selection("parent.SLDASM", None))
        self.assertEqual("parent.SLDASM", module._linked_selection("parent.SLDASM", ""))
        self.assertEqual("parent.SLDASM", module._linked_selection("parent.SLDASM", "parent.SLDASM"))
        with self.assertRaises(AirflowFailException):
            module._linked_selection("parent.SLDASM", "other.SLDASM")
        with self.assertRaises(AirflowFailException):
            module._linked_selection(None, "other.SLDASM")
        with self.assertRaises(EndpointProtocolError):
            module._linked_selection("parent.SLDASM", "../other.SLDASM")

    def test_dag_run_with_explicit_main_assembly(self) -> None:
        from tests.v1.test_airflow_client import MockEndpoint

        module = self._module()
        dag = module.dag
        with MockEndpoint() as server:
            connection = json.dumps(
                {
                    "conn_type": "http",
                    "host": "127.0.0.1",
                    "port": server.server.server_address[1],
                    "password": "test-token",
                }
            )
            request = {
                "run_id": module.native_run_id("dag-run-selection"),
                "package": "handoff/m3.0",
                "handoff_sha256": "b" * 64,
                "conn_id": "solidworks_windows",
                "main_assembly": "3.0 总装1008.SLDASM",
            }
            with patch.dict(os.environ, {"AIRFLOW_CONN_SOLIDWORKS_WINDOWS": connection}):
                started = dag.task_dict["start_job"].python_callable(request)
                self.assertIn(started["status"], {"queued", "running"})
            self.assertEqual(len(server.jobs), 1)
            job = next(iter(server.jobs.values()))
            self.assertEqual(
                job["request"],
                {
                    "run_id": request["run_id"],
                    "package": "handoff/m3.0",
                    "handoff_sha256": "b" * 64,
                    "main_assembly": "3.0 总装1008.SLDASM",
                },
            )

    def test_linked_dag_run_derives_the_retained_upload_from_the_parent_job(self) -> None:
        from tests.v1.test_airflow_client import JOB_SCHEMA, MockEndpoint, PIPELINE_ID
        from description_pipeline.orchestration.airflow_client import native_run_id

        from types import SimpleNamespace

        module = self._module()
        dag = module.dag
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
                "request": {
                    "run_id": parent_run_id,
                    "package": "handoff/m3.0",
                    "handoff_sha256": "b" * 64,
                    "main_assembly": "3.0 总装1008.SLDASM",
                },
            }
            connection = json.dumps(
                {
                    "conn_type": "http",
                    "host": "127.0.0.1",
                    "port": server.server.server_address[1],
                    "password": "test-token",
                }
            )
            context = {
                "dag_run": SimpleNamespace(
                    conf={"parent_dag_run_id": parent_dag_run_id, "resume_from": "capture"},
                    run_id="portal-child-1",
                ),
                "params": {"handoff_path": "", "main_assembly": ""},
            }
            with patch.dict(os.environ, {"AIRFLOW_CONN_SOLIDWORKS_WINDOWS": connection}):
                request = dag.task_dict["resolve_handoff"].python_callable(**context)
                self.assertEqual(request["package"], "handoff/m3.0")
                self.assertEqual(request["handoff_sha256"], "b" * 64)
                self.assertEqual(request["main_assembly"], "3.0 总装1008.SLDASM")
                self.assertEqual(request["resume"], {"parent_run": parent_run_id, "from_stage": "capture"})
                started = dag.task_dict["start_job"].python_callable(request)
                polled = False
                for _ in range(8):
                    if module._poke(started):
                        polled = True
                        break
                self.assertTrue(polled)
            self.assertEqual(server.resolved_paths, [])
            children = [job for job in server.jobs.values() if job["run_id"] != parent_run_id]
            self.assertEqual(len(children), 1)
            child = children[0]
            self.assertEqual(child["request"]["package"], "handoff/m3.0")
            self.assertEqual(child["request"]["handoff_sha256"], "b" * 64)
            self.assertEqual(child["request"]["main_assembly"], "3.0 总装1008.SLDASM")
            self.assertEqual(child["request"]["resume"], {"parent_run": parent_run_id, "from_stage": "capture"})


if __name__ == "__main__":
    unittest.main()
