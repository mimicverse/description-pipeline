"""Real Airflow DAG parsing and one mocked-endpoint DAG run (requires the Airflow venv)."""

from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.v1._airflow_env import pinned_airflow_home

from description_pipeline.io import digest
from description_pipeline.orchestration.airflow_client import EndpointConfig

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
    def test_submission_frame_requires_a_configured_target_and_summary(self) -> None:
        bag = DagBag(str(DAG_DIR))
        resolve = bag.dags["solidworks_to_urdf"].task_dict["freeze"].python_callable
        module = sys.modules[resolve.__module__]
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
        )

        def context(conf):
            return {"dag_run": SimpleNamespace(conf=conf, triggering_user_name="ops"), "params": {}}

        with patch.object(module, "config_from_airflow_connection", return_value=config):
            for conf in (
                {"target_hardware": "m4.0", "change_summary": "x"},
                {"change_summary": "x"},
                {"target_hardware": "   ", "change_summary": "x"},
                {"target_hardware": "m3.0"},
                {"target_hardware": "m3.0", "change_summary": "  "},
            ):
                with self.subTest(conf=str(conf)), self.assertRaises(module.AirflowFailException):
                    module._submission_frame(context(conf))
            identity, routing = module._submission_frame(
                context({"target_hardware": "m3.0", "change_summary": "rebind"})
            )
            multiline, _routing = module._submission_frame(
                context({"target_hardware": "m3.0", "change_summary": "第一行\r\n第二行\r第三行"})
            )
        self.assertEqual(routing, {"repository_slug": "mimicverse/description", "base": "main"})
        self.assertEqual(identity["hardware_id"], "m3.0")
        self.assertEqual(identity["change_summary"], "rebind")
        self.assertIsNone(identity["parent_revision"])
        self.assertEqual(multiline["change_summary"], "第一行\n第二行\n第三行")

    def test_freeze_fails_before_any_endpoint_contact(self) -> None:
        from types import SimpleNamespace

        from airflow.sdk.exceptions import AirflowFailException

        module = self._module()
        dag = module.dag
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
        )

        def context(conf):
            return {"dag_run": SimpleNamespace(conf=conf, run_id="portal-child-9"), "params": {}}

        with (
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_endpoint", side_effect=AssertionError("endpoint contacted")),
        ):
            for conf in ({}, {"target_hardware": "m3.0"}, {"change_summary": "x"}):
                with self.subTest(conf=str(conf)), self.assertRaises(AirflowFailException):
                    dag.task_dict["freeze"].python_callable(**context(conf))

    def test_dag_parses(self) -> None:
        bag = DagBag(str(DAG_DIR))
        self.assertEqual(bag.import_errors, {})
        dag = bag.dags["solidworks_to_urdf"]
        self.assertIsNone(dag.schedule)
        self.assertEqual(
            set(dag.task_ids),
            {
                "freeze",
                "discover",
                "capture",
                "generate",
                "verify",
                "publish",
            },
        )
        edges = {task_id: set(task.upstream_task_ids) for task_id, task in dag.task_dict.items()}
        self.assertEqual(edges["discover"], {"freeze"})
        self.assertEqual(edges["capture"], {"freeze", "discover"})
        self.assertEqual(edges["generate"], {"capture"})
        self.assertEqual(edges["verify"], {"generate"})
        self.assertEqual(edges["publish"], {"verify"})
        self.assertEqual(
            set(dag.params), {"handoff_path", "main_assembly", "target_hardware", "change_summary"}
        )
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
        upload = {"a.SLDASM": "a" * 64}
        request = {
            "run_id": "protocol-control",
            "upload_files": upload,
            "upload_sha256": digest(upload),
            "main_assembly": "a.SLDASM",
            "conn_id": "test",
        }
        job = {
            "request": {key: request[key] for key in ("run_id", "upload_files", "upload_sha256", "main_assembly")},
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
        upload = {"a.SLDASM": "a" * 64}
        request = {
            "run_id": "protocol-control",
            "upload_files": upload,
            "upload_sha256": digest(upload),
            "main_assembly": "a.SLDASM",
            "conn_id": "test",
        }
        job = {
            "request": {key: request[key] for key in ("run_id", "upload_files", "upload_sha256", "main_assembly")},
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
            upload = {"assembly.SLDASM": "a" * 64}
            request = {
                "run_id": module.native_run_id("dag-run-native-half"),
                "upload_files": upload,
                "upload_sha256": digest(upload),
                "main_assembly": "assembly.SLDASM",
                "conn_id": "solidworks_windows",
            }
            with patch.dict(os.environ, {"AIRFLOW_CONN_SOLIDWORKS_WINDOWS": connection}):
                started = dag.task_dict["discover"].python_callable(request)
                self.assertFalse(started)  # first poke submits; the reschedule poll continues
                polled = False
                for _ in range(8):
                    if module._poke(request):
                        polled = True
                        break
                self.assertTrue(polled)
                with self.assertRaises(AirflowFailException):
                    # The portable half only starts from the native_complete boundary;
                    # the full split is covered by test_split_workflow/test_linux_split.
                    dag.task_dict["capture"].python_callable(request)
            self.assertEqual(len(server.jobs), 1)
            job = next(iter(server.jobs.values()))
            self.assertEqual(
                job["request"],
                {
                    "run_id": request["run_id"],
                    "upload_files": upload,
                    "upload_sha256": digest(upload),
                    "main_assembly": "assembly.SLDASM",
                },
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
            upload = {"3.0 总装1008.SLDASM": "a" * 64}
            request = {
                "run_id": module.native_run_id("dag-run-selection"),
                "upload_files": upload,
                "upload_sha256": digest(upload),
                "main_assembly": "3.0 总装1008.SLDASM",
                "conn_id": "solidworks_windows",
            }
            with patch.dict(os.environ, {"AIRFLOW_CONN_SOLIDWORKS_WINDOWS": connection}):
                started = dag.task_dict["discover"].python_callable(request)
                self.assertFalse(started)  # first poke submits; the reschedule poll continues
            self.assertEqual(len(server.jobs), 1)
            job = next(iter(server.jobs.values()))
            self.assertEqual(
                job["request"],
                {
                    "run_id": request["run_id"],
                    "upload_files": upload,
                    "upload_sha256": digest(upload),
                    "main_assembly": "3.0 总装1008.SLDASM",
                },
            )

    def test_linked_run_reuses_the_parent_selection_payload(self) -> None:
        from types import SimpleNamespace

        from airflow.sdk.exceptions import AirflowFailException
        from description_pipeline.io import digest
        from description_pipeline.orchestration.airflow_client import native_run_id
        from description_pipeline.request_evidence import freeze_request_evidence

        module = self._module()
        dag = module.dag
        parent_dag_run_id = "portal-parent-20261009"
        parent_run_id = native_run_id(parent_dag_run_id)
        upload = {"3.0 总装1008.SLDASM": "a" * 64, "parts/零件.SLDPRT": "b" * 64}
        parent_job = {
            "request": {
                "run_id": parent_run_id,
                "upload_files": upload,
                "upload_sha256": digest(upload),
                "main_assembly": "3.0 总装1008.SLDASM",
            }
        }
        submitted = {}

        class Stub:
            def get_job(self, run_id):
                if run_id == parent_run_id:
                    # freeze reads the retained parent selection first.
                    return parent_job
                raise module.EndpointNotFound("child job is not created yet")

            def start_staging_job(self, **kwargs):
                submitted.update(kwargs)
                return {"run_id": kwargs["run_id"], "status": "queued"}

        context = {
            "dag_run": SimpleNamespace(
                conf={
                    "parent_dag_run_id": parent_dag_run_id,
                    "resume_from": "capture",
                    "target_hardware": "m3.0",
                    "change_summary": "linked capture rerun",
                },
                run_id="portal-child-1",
            ),
            "params": {"handoff_path": "", "main_assembly": ""},
        }
        import shutil
        import tempfile

        store_root = Path(tempfile.mkdtemp(prefix="dag-store-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        freeze_request_evidence(
            store_root / parent_run_id,
            request_id=parent_dag_run_id,
            target_hardware="m3.0",
            change_summary="linked capture rerun",
            main_assembly="3.0 总装1008.SLDASM",
            handoff_digest=digest(upload),
            platform_config_digest="c" * 64,
        )
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
            store_root=store_root,
        )
        with (
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
        ):
            request = dag.task_dict["freeze"].python_callable(**context)
            self.assertEqual(request["upload_files"], upload)
            self.assertEqual(request["upload_sha256"], digest(upload))
            self.assertEqual(request["main_assembly"], "3.0 总装1008.SLDASM")
            self.assertEqual(request["resume"], {"parent_run": parent_run_id, "from_stage": "capture"})
            started = dag.task_dict["discover"].python_callable(request)
        self.assertFalse(started)  # submission happened; the reschedule poll continues
        self.assertEqual(
            submitted,
            {
                "run_id": request["run_id"],
                "upload_files": upload,
                "main_assembly": "3.0 总装1008.SLDASM",
                "resume": {"parent_run": parent_run_id, "from_stage": "capture"},
            },
        )
        with (
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            self.assertRaises(AirflowFailException),
        ):
            dag.task_dict["freeze"].python_callable(
                **{**context, "params": {"handoff_path": "", "main_assembly": "other.SLDASM"}}
            )

        class LegacyStub:
            def get_job(self, run_id):
                return {"request": {"run_id": parent_run_id, "package": "handoff/m3.0"}}

        with (
            patch.object(module, "_endpoint", return_value=LegacyStub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            self.assertRaises(AirflowFailException),
        ):
            dag.task_dict["freeze"].python_callable(**context)

    def test_linked_publish_rebinds_routing_from_conf_over_null_parent_meta(self) -> None:
        from types import SimpleNamespace

        module = self._module()
        dag = module.dag
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
        )
        parent_meta = {
            "handoff_sha256": "d" * 64,
            "main_assembly": "assembly.SLDASM",
            "repository_slug": None,
            "repository_base": None,
        }
        import shutil
        import tempfile

        from description_pipeline.orchestration.airflow_client import native_run_id
        from description_pipeline.request_evidence import freeze_request_evidence

        store_root = Path(tempfile.mkdtemp(prefix="dag-linked-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        freeze_request_evidence(
            store_root / native_run_id("portal-parent-2"),
            request_id="portal-parent-2",
            target_hardware="m3.0",
            change_summary="rebound publish",
            main_assembly="assembly.SLDASM",
            handoff_digest="d" * 64,
            platform_config_digest="e" * 64,
        )

        class Store:
            def meta(self, run_id):
                return dict(parent_meta)

            def run_dir(self, run_id):
                return store_root / run_id

        context = {
            "dag_run": SimpleNamespace(
                conf={
                    "parent_dag_run_id": "portal-parent-2",
                    "resume_from": "publish",
                    "target_hardware": "m3.0",
                    "change_summary": "rebound publish",
                },
                run_id="portal-child-2",
            ),
            "params": {},
        }
        with (
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_linux_store", return_value=Store()),
            patch.object(module, "_root_capture", return_value=Path("/srv/capture")),
            patch.object(module, "_root_identity", return_value=("m3.0", "r1")),
        ):
            request = dag.task_dict["freeze"].python_callable(**context)
            self.assertTrue(request["linux_owned"])
            self.assertEqual(request["repository_slug"], "mimicverse/description")
            self.assertEqual(request["repository_base"], "main")
            binding = dag.task_dict["capture"].python_callable(request)
        self.assertEqual(binding["repository_slug"], "mimicverse/description")
        self.assertEqual(binding["repository_base"], "main")
        self.assertEqual(binding["handoff_sha256"], "d" * 64)
        self.assertEqual(binding["state"], "linked")


if __name__ == "__main__":
    unittest.main()
