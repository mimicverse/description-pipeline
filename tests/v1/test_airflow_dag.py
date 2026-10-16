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
from description_pipeline.orchestration.feishu_oauth import build_actor_name

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
            return {
                "dag_run": SimpleNamespace(
                    conf=conf, triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke")
                ),
                "params": {},
            }

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

    def test_submission_frame_uses_only_the_authenticated_actor(self) -> None:
        from airflow.sdk.exceptions import AirflowFailException

        bag = DagBag(str(DAG_DIR))
        resolve = bag.dags["solidworks_to_urdf"].task_dict["freeze"].python_callable
        module = sys.modules[resolve.__module__]
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
        )
        forged = {
            "target_hardware": "m3.0",
            "change_summary": "x",
            # A client-shaped conf claim must never name the submitter.
            "submission_principal": "cli_forged:tenant-a:ou_attacker",
        }

        def context(actor):
            return {"dag_run": SimpleNamespace(conf=forged, triggering_user_name=actor), "params": {}}

        with patch.object(module, "config_from_airflow_connection", return_value=config):
            identity, _routing = module._submission_frame(
                context(build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"))
            )
            self.assertEqual(identity["owner"], "cli_smoke:smoke-tenant:ou_smoke")
            for actor in (
                "",
                "   ",
                None,
                "ops",
                42,
                "cli_smoke:smoke-tenant:ou_smoke",
                "cli_smoke:smoke-tenant:ou_smoke|not-json",
                'cli_smoke:smoke-tenant:ou_smoke|" smoke"',
            ):
                with self.subTest(actor=actor), self.assertRaisesRegex(
                    AirflowFailException, "authenticated submitter identity"
                ):
                    module._submission_frame(context(actor))

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
                triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
            ),
            "params": {"handoff_path": "", "main_assembly": ""},
        }
        import shutil
        import tempfile

        store_root = Path(tempfile.mkdtemp(prefix="dag-store-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        freeze_request_evidence(
            store_root / "sealed" / parent_run_id / "freeze",
            request_id=parent_dag_run_id,
            target_hardware="m3.0",
            change_summary="linked capture rerun",
            main_assembly="3.0 总装1008.SLDASM",
            handoff_digest=digest(upload),
            platform_config_digest="c" * 64,
        )
        class _Platform:
            def confirmed_stage(self, run_id, stage):
                return {"handoff_sha256": "d" * 64} if stage in {"freeze", "capture"} else None

            def stage_records(self, run_id):
                return {"freeze": {"state": "completed", "checkpoint_uri": f"store://sealed/{run_id}/freeze"}}

            def sealed_path(self, uri):
                return store_root / "sealed" / uri.removeprefix("store://sealed/")
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
            store_root=store_root,
        )
        with (
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
        ):
            request = dag.task_dict["freeze"].python_callable(**context)
            self.assertEqual(request["upload_files"], upload)
            self.assertEqual(request["upload_sha256"], digest(upload))
            self.assertEqual(request["main_assembly"], "3.0 总装1008.SLDASM")
            self.assertEqual(request["resume"], {"parent_run": parent_run_id, "from_stage": "capture"})
            started = dag.task_dict["discover"].python_callable(request)
        self.assertEqual(
            (store_root / request["run_id"] / "input" / "request.json").read_bytes(),
            (store_root / "sealed" / parent_run_id / "freeze" / "input" / "request.json").read_bytes(),
        )
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
        import shutil
        import tempfile

        from description_pipeline.orchestration.airflow_client import native_run_id
        from description_pipeline.request_evidence import freeze_request_evidence

        store_root = Path(tempfile.mkdtemp(prefix="dag-linked-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        parent_run_id = native_run_id("portal-parent-2")
        freeze_request_evidence(
            store_root / "sealed" / parent_run_id / "freeze",
            request_id="portal-parent-2",
            target_hardware="m3.0",
            change_summary="rebound publish",
            main_assembly="assembly.SLDASM",
            handoff_digest="d" * 64,
            platform_config_digest="e" * 64,
        )
        source = store_root / "sealed" / parent_run_id / "capture" / "input"
        source.mkdir(parents=True)
        (source / "source.json").write_text(
            json.dumps({"identity": {"hardware_id": "m3.0"}, "metadata": {"revision": "r1"}}),
            encoding="utf-8",
        )

        class _Platform:
            def confirmed_stage(self, run_id, stage):
                return {"handoff_sha256": "d" * 64} if stage in {"freeze", "capture"} else None

            def stage_records(self, run_id):
                return {
                    "freeze": {"state": "completed", "checkpoint_uri": f"store://sealed/{run_id}/freeze"},
                    "capture": {"state": "completed", "checkpoint_uri": f"store://sealed/{run_id}/capture"},
                }

            def sealed_path(self, uri):
                return store_root / "sealed" / uri.removeprefix("store://sealed/")

        context = {
            "dag_run": SimpleNamespace(
                conf={
                    "parent_dag_run_id": "portal-parent-2",
                    "resume_from": "publish",
                    "target_hardware": "m3.0",
                    "change_summary": "rebound publish",
                },
                run_id="portal-child-2",
                triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
            ),
            "params": {},
        }
        with (
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
        ):
            request = dag.task_dict["freeze"].python_callable(**context)
            self.assertTrue(request["linux_owned"])
            self.assertEqual(request["repository_slug"], "mimicverse/description")
            self.assertEqual(request["repository_base"], "main")
            binding = dag.task_dict["capture"].python_callable(request)
        self.assertEqual(binding["repository_slug"], "mimicverse/description")
        self.assertEqual(binding["repository_base"], "main")
        self.assertEqual(binding["handoff_sha256"], "d" * 64)
        self.assertEqual(binding["main_assembly"], "assembly.SLDASM")
        self.assertEqual((binding["hardware_id"], binding["revision"]), ("m3.0", "r1"))
        self.assertEqual(binding["state"], "linked")


@unittest.skipUnless(AIRFLOW_AVAILABLE, "Airflow is not installed in this interpreter")
class PlatformRegistrationTests(unittest.TestCase):
    """The production registration wiring: every stage seals its tree and confirms for real."""

    def setUp(self) -> None:
        import tempfile

        bag = DagBag(str(DAG_DIR))
        self.module = sys.modules[bag.dags["solidworks_to_urdf"].task_dict["freeze"].python_callable.__module__]
        self.temporary = tempfile.TemporaryDirectory(prefix="platform-registration-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "store-root"
        self.root.mkdir()
        self.calls: list[dict] = []
        self.started: list[str] = []
        self.live_events: list[dict] = []
        platform = SimpleNamespace(
            confirm_stage=lambda **payload: self.calls.append(payload),
            confirmed_stage=lambda *args, **kwargs: None,
            start_run=lambda run_id: self.started.append(run_id),
            append_event=lambda run_id, *, stage, kind, payload=None: self.live_events.append(
                {"run_id": run_id, "stage": stage, "kind": kind, "payload": payload}
            ),
        )
        binding = patch.object(self.module, "_platform_binding", return_value=(platform, self.root))
        binding.start()
        self.addCleanup(binding.stop)

    def _tree(self, *, run_id: str = "run-1", stage: str = "generate") -> Path:
        from tests.v1.test_platform_store import build_sealed_delivery

        tree = Path(self.temporary.name) / "store-root" / run_id / "stages" / stage
        tree.mkdir(parents=True)
        build_sealed_delivery(tree)
        return tree

    def test_a_completed_portable_stage_is_sealed_and_confirmed(self) -> None:
        from description_pipeline.delivery import read_delivery_manifest

        tree = self._tree(stage="generate")
        subject = read_delivery_manifest(tree)["subject"]["digest"]
        receipt = {"run_id": "run-1", "state": "generated", "subject_sha256": subject}
        with patch.object(self.module.flow, "run_stage", return_value=receipt):
            result = self.module._portable_registered("run-1", "generate", tree=tree)
        self.assertEqual(result, receipt)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["run_id"], "run-1")
        self.assertEqual(call["stage"], "generate")
        self.assertEqual(call["state"], "completed")
        self.assertEqual(call["checkpoint_uri"], "store://sealed/run-1/generate")
        self.assertEqual(call["receipt"]["state"], "generated")
        self.assertIs(call["receipt"]["passed"], False)
        paths = {record["path"] for record in call["receipt"]["artifacts"]}
        self.assertIn("model/robot.json", paths)
        self.assertIn("reports/quality.json", paths)
        self.assertTrue((self.root / "sealed" / "run-1" / "generate" / "manifest.json").is_file())

    def test_a_stage_failure_converges_with_the_actual_products(self) -> None:
        tree = self._tree(stage="generate")
        with (
            patch.object(self.module.flow, "run_stage", side_effect=RuntimeError("stage exploded")),
            self.assertRaises(RuntimeError),
        ):
            self.module._portable_registered("run-1", "generate", tree=tree)
        call = self.calls[0]
        self.assertEqual(call["state"], "failed")
        self.assertEqual(call["receipt"]["state"], "failed")
        self.assertIs(call["receipt"]["passed"], False)
        self.assertIn("stage exploded", call["receipt"]["error"])
        self.assertEqual(call["checkpoint_uri"], "store://sealed/run-1/generate")

    def test_an_early_failure_without_a_tree_registers_its_shape_only(self) -> None:
        absent = Path(self.temporary.name) / "absent"
        with (
            patch.object(self.module.flow, "run_stage", side_effect=RuntimeError("no products")),
            self.assertRaises(RuntimeError),
        ):
            self.module._portable_registered("run-1", "generate", tree=absent)
        call = self.calls[0]
        self.assertEqual(call["state"], "failed")
        self.assertEqual(call["receipt"]["state"], "failed")
        self.assertNotIn("checkpoint_uri", call)

    def test_the_freeze_stage_seals_the_admitted_originals(self) -> None:
        from description_pipeline.delivery import read_delivery_manifest
        from description_pipeline.io import digest as io_digest

        run_dir = Path(self.temporary.name) / "runs" / "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "request.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        intake = Path(self.temporary.name) / "intake"
        (intake / "cad").mkdir(parents=True)
        (intake / "cad" / "part.sldprt").write_bytes(b"cad-bytes")
        (intake / "assembly.sldasm").write_bytes(b"assembly-bytes")
        upload_files = {"assembly.sldasm": "a" * 64, "cad/part.sldprt": "b" * 64}
        from description_pipeline.platform import flow

        paths = flow.RunPaths(run_dir.parent, "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d")
        stages = run_dir / "stages"
        self.module._register_freeze_stage(
            paths, "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d", intake=intake, upload_files=upload_files
        )
        call = self.calls[0]
        self.assertEqual(call["stage"], "freeze")
        self.assertEqual(call["state"], "completed")
        self.assertEqual(call["receipt"]["state"], "frozen")
        self.assertEqual(call["receipt"]["handoff_sha256"], io_digest(upload_files))
        tree = stages / "freeze"
        self.assertTrue((tree / "input" / "cad" / "part.sldprt").is_file())
        self.assertTrue((tree / "input" / "assembly.sldasm").is_file())
        self.assertTrue((tree / "input" / "request.json").is_file())
        manifest = read_delivery_manifest(tree)
        self.assertIn("input/assembly.sldasm", {record["path"] for record in manifest["subject"]["files"]})

    def test_the_freeze_stage_starts_the_admitted_run(self) -> None:
        run_dir = Path(self.temporary.name) / "runs" / "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "request.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        intake = Path(self.temporary.name) / "intake-start"
        intake.mkdir()
        (intake / "assembly.sldasm").write_bytes(b"assembly-bytes")
        from description_pipeline.platform import flow

        paths = flow.RunPaths(run_dir.parent, "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d")
        self.module._register_freeze_stage(
            paths, "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d", intake=intake, upload_files={"assembly.sldasm": "a" * 64}
        )
        self.assertEqual(self.started, ["0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"])

    def test_stage_events_are_mirrored_into_the_live_platform_stream(self) -> None:
        from description_pipeline.delivery import read_delivery_manifest

        tree = self._tree(stage="generate")
        receipt = {
            "run_id": "run-1",
            "state": "generated",
            "subject_sha256": read_delivery_manifest(tree)["subject"]["digest"],
        }

        def fake_stage(platform, root, run_id, stage, **kwargs):
            kwargs["on_event"]({"stage": stage, "state": "running"})
            kwargs["on_event"]({"stage": stage, "state": "completed"})
            return receipt

        with patch.object(self.module.flow, "run_stage", side_effect=fake_stage):
            self.module._portable_registered("run-1", "generate", tree=tree)
        self.assertEqual([event["payload"]["state"] for event in self.live_events], ["running", "completed"])
        self.assertEqual({event["kind"] for event in self.live_events}, {"stage"})
        self.assertEqual({event["run_id"] for event in self.live_events}, {"run-1"})

    def test_the_cache_release_runs_through_the_operation_family(self) -> None:
        import hashlib as hashing
        import uuid
        from types import SimpleNamespace

        from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

        run_dir = Path(self.temporary.name) / "runs" / "run-1"
        capture = run_dir / "capture"
        capture.mkdir(parents=True)
        (capture / "transfer-manifest.json").write_bytes(b"manifest bytes")
        intake = Path(self.temporary.name) / "intake-release"
        (intake / "cad").mkdir(parents=True)
        (intake / "cad" / "robot.SLDASM").write_bytes(b"assembly bytes")
        upload = {"cad/robot.SLDASM": hashing.sha256(b"assembly bytes").hexdigest()}
        released: list[tuple[str, dict]] = []
        settled: list[dict] = []
        digests: list[str] = []

        class _Ops:
            def __init__(self):
                self.state = "new"

            def register_operation(self, *, run_id, stage, kind, operation_key, params_digest, dispatch=None):
                digests.append(params_digest)
                current = {"new": "queued", "running": "running", "settled": "succeeded"}[self.state]
                return SimpleNamespace(operation_id=str(uuid.uuid4()), state=current)

            def stage_records(self, run_id):
                return {"capture": {"original_executed_at": "2026-10-16T00:00:00Z"}}

            def claim_operation(self, *, run_id, stage, operation_key):
                granted = self.state == "new"
                if granted:
                    self.state = "running"
                return SimpleNamespace(granted=granted, record=SimpleNamespace(state="running"))

            def settle_cache_release(self, *, run_id, stage, operation_key, receipt):
                self.state = "settled"
                settled.append(receipt)

        endpoint = SimpleNamespace(
            health=lambda: {
                "release": {
                    "version": "1.4.0",
                    "source_commit": "ab" * 20,
                    "source_sha256": "cd" * 32,
                    "role": "native",
                },
                "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            },
            release_cache=lambda run_id, request: released.append((run_id, request)) or {"status": "released"},
        )
        platform = _Ops()
        self.module._release_registered_cache(
            platform, endpoint, "run-1", capture, intake=intake, upload_files=upload
        )
        self.assertEqual(len(released), 1)
        self.assertEqual(len(settled), 1)
        self.module._release_registered_cache(
            platform, endpoint, "run-1", capture, intake=intake, upload_files=upload
        )
        self.assertEqual(len(released), 1, "a settled operation answers a repeat call without a side effect")
        self.assertEqual(digests[0], digests[1], "the registered parameter digest is stable across retries")

    def test_a_lost_response_replays_the_same_operation_and_settles(self) -> None:
        import hashlib as hashing
        import uuid
        from types import SimpleNamespace

        from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

        run_dir = Path(self.temporary.name) / "runs" / "run-3"
        capture = run_dir / "capture"
        capture.mkdir(parents=True)
        (capture / "transfer-manifest.json").write_bytes(b"manifest bytes")
        intake = Path(self.temporary.name) / "intake-release-3"
        (intake / "cad").mkdir(parents=True)
        (intake / "cad" / "robot.SLDASM").write_bytes(b"assembly bytes")
        upload = {"cad/robot.SLDASM": hashing.sha256(b"assembly bytes").hexdigest()}
        calls: list[str] = []
        settled: list[dict] = []
        digests: list[str] = []
        mode = {"fail": True}

        class _Ops:
            def __init__(self):
                self.state = "new"
                self.operation_id = None

            def register_operation(self, *, run_id, stage, kind, operation_key, params_digest, dispatch=None):
                digests.append(params_digest)
                if self.operation_id is None:
                    self.operation_id = str(uuid.uuid4())
                current = {"new": "queued", "running": "running", "settled": "succeeded"}[self.state]
                return SimpleNamespace(operation_id=self.operation_id, state=current)

            def stage_records(self, run_id):
                return {"capture": {"original_executed_at": "2026-10-16T00:00:00Z"}}

            def claim_operation(self, *, run_id, stage, operation_key):
                granted = self.state == "new"
                if granted:
                    self.state = "running"
                return SimpleNamespace(granted=granted, record=SimpleNamespace(state="running"))

            def settle_cache_release(self, *, run_id, stage, operation_key, receipt):
                self.state = "settled"
                settled.append(receipt)

        def release(run_id, request):
            calls.append(request["operation_id"])
            if mode["fail"]:
                raise RuntimeError("response lost")
            return {"status": "released"}

        endpoint = SimpleNamespace(
            health=lambda: {
                "release": {
                    "version": "1.4.0",
                    "source_commit": "ab" * 20,
                    "source_sha256": "cd" * 32,
                    "role": "native",
                },
                "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            },
            release_cache=release,
        )
        platform = _Ops()
        self.module._release_registered_cache(
            platform, endpoint, "run-3", capture, intake=intake, upload_files=upload
        )
        self.assertEqual(settled, [], "a lost response must not settle")
        mode["fail"] = False
        self.module._release_registered_cache(
            platform, endpoint, "run-3", capture, intake=intake, upload_files=upload
        )
        self.assertEqual(len(calls), 2, "the reconciliation replays the same operation identity")
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(digests[0], digests[1])
        self.assertEqual(len(settled), 1)

    def test_a_failed_cache_release_never_fails_the_stage(self) -> None:
        import hashlib as hashing
        import uuid
        from types import SimpleNamespace

        from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

        run_dir = Path(self.temporary.name) / "runs" / "run-2"
        capture = run_dir / "capture"
        capture.mkdir(parents=True)
        (capture / "transfer-manifest.json").write_bytes(b"manifest bytes")
        intake = Path(self.temporary.name) / "intake-release-2"
        (intake / "cad").mkdir(parents=True)
        (intake / "cad" / "robot.SLDASM").write_bytes(b"assembly bytes")
        upload = {"cad/robot.SLDASM": hashing.sha256(b"assembly bytes").hexdigest()}
        settled: list[dict] = []

        class _Ops:
            def register_operation(self, *, run_id, stage, kind, operation_key, params_digest, dispatch=None):
                return SimpleNamespace(operation_id=str(uuid.uuid4()), state="queued")

            def stage_records(self, run_id):
                return {"capture": {"original_executed_at": "2026-10-16T00:00:00Z"}}

            def claim_operation(self, *, run_id, stage, operation_key):
                return SimpleNamespace(granted=True, record=SimpleNamespace(state="running"))

            def settle_cache_release(self, *, run_id, stage, operation_key, receipt):
                settled.append(receipt)

        def refuse(run_id, request):
            raise RuntimeError("endpoint refuses the release")

        endpoint = SimpleNamespace(
            health=lambda: {
                "release": {
                    "version": "1.4.0",
                    "source_commit": "ab" * 20,
                    "source_sha256": "cd" * 32,
                    "role": "native",
                },
                "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            },
            release_cache=refuse,
        )
        self.module._release_registered_cache(
            _Ops(), endpoint, "run-2", capture, intake=intake, upload_files=upload
        )
        self.assertEqual(settled, [], "a refused release never reports success")

    def test_the_native_stages_seal_one_snapshot_and_confirm_in_order(self) -> None:
        from description_pipeline.io import read_data
        from tests.v1.test_platform_store import build_sealed_delivery

        run_dir = Path(self.temporary.name) / "runs" / "run-1"
        capture = run_dir / "capture"
        capture.mkdir(parents=True)
        build_sealed_delivery(capture)
        platform, store_root = self.module._platform_binding()
        self.module._register_native_stages(platform, store_root, "run-1", capture, run_dir)
        self.assertEqual([call["stage"] for call in self.calls], ["discover", "capture"])
        self.assertEqual([call["state"] for call in self.calls], ["completed", "completed"])
        self.assertEqual([call["receipt"]["state"] for call in self.calls], ["discovered", "native_complete"])
        native_tool = read_data(capture / "reports" / "native-tool.json")
        for call in self.calls:
            self.assertEqual(call["receipt"]["native_tool"], native_tool)
        self.assertTrue((self.root / "sealed" / "run-1" / "discover").is_dir())
        self.assertTrue((self.root / "sealed" / "run-1" / "capture").is_dir())

    def test_a_repeated_freeze_confirmation_consumes_the_confirmed_fact(self) -> None:
        run_dir = Path(self.temporary.name) / "runs" / "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "request.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        intake = Path(self.temporary.name) / "intake-retry"
        intake.mkdir()
        (intake / "assembly.sldasm").write_bytes(b"assembly-bytes")
        upload_files = {"assembly.sldasm": "a" * 64}
        from description_pipeline.platform import flow

        paths = flow.RunPaths(run_dir.parent, "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d")

        records: dict[tuple[str, str], dict] = {}
        confirmations: list[str] = []

        class _RetainingPlatform:
            def start_run(self, run_id):
                return {"run_id": run_id, "state": "running"}

            def confirm_stage(self, *, run_id, stage, state, receipt, checkpoint_uri, original_executed_at=None):
                records[(run_id, stage)] = dict(receipt)
                confirmations.append(stage)

            def confirmed_stage(self, run_id, stage):
                return records.get((run_id, stage))

        platform = _RetainingPlatform()
        with patch.object(self.module, "_platform_binding", return_value=(platform, self.root)):
            self.module._register_freeze_stage(
            paths, "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d", intake=intake, upload_files=upload_files
        )
            self.module._register_freeze_stage(
            paths, "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d", intake=intake, upload_files=upload_files
        )
        self.assertEqual(confirmations, ["freeze"])

    def test_a_failed_registration_tolerates_a_malformed_handoff(self) -> None:
        tree = Path(self.temporary.name) / "malformed"
        self.module._ensure_subject_layout(tree)
        (tree / "reports").mkdir()
        (tree / "reports" / "structural-team.json").write_text("{}", encoding="utf-8")
        (tree / "input" / "handoff.json").write_text("{not json", encoding="utf-8")
        contract, policy = self.module._delivery_manifest_arguments()
        self.module.write_delivery_manifest(tree, contract=contract, policy=policy)
        self.module._converge_failed_stage("run-1", "generate", RuntimeError("bad handoff"), tree=tree)
        call = self.calls[0]
        self.assertEqual(call["state"], "failed")
        self.assertEqual(call["receipt"]["state"], "failed")
        self.assertNotIn("handoff_sha256", call["receipt"])
        paths = {record["path"] for record in call["receipt"]["artifacts"]}
        self.assertIn("reports/structural-team.json", paths)
        self.assertIn("input/handoff.json", paths)
        self.assertTrue(str(call["checkpoint_uri"]).endswith("/generate"))

    def test_a_portable_failure_snapshots_the_actual_products(self) -> None:
        import uuid

        run_id = str(uuid.uuid4())
        run_dir = self.root / run_id
        (run_dir / "reports").mkdir(parents=True)
        (run_dir / "reports" / "structural-team.json").write_text("{}", encoding="utf-8")
        tree = run_dir / "stages" / "generate"
        with (
            patch.object(self.module.flow, "run_stage", side_effect=RuntimeError("no capture")),
            self.assertRaises(RuntimeError),
        ):
            self.module._portable_registered(run_id, "generate", tree=tree)
        call = self.calls[0]
        self.assertEqual(call["state"], "failed")
        paths = {record["path"] for record in call["receipt"]["artifacts"]}
        self.assertIn("reports/structural-team.json", paths)
        self.assertTrue(str(call["checkpoint_uri"]).endswith("/generate"))


@unittest.skipUnless(AIRFLOW_AVAILABLE, "Airflow is not installed in this interpreter")
class LinkedMaterializationTests(unittest.TestCase):
    """A linked attempt materializes reused rows and sealed inputs through the real writer."""

    PARENT = "11111111-1111-4111-8111-111111111111"
    CHILD = "22222222-2222-4222-8222-222222222222"
    GRANDCHILD = "33333333-3333-4333-8333-333333333333"

    def setUp(self) -> None:
        import tempfile

        import hashlib

        from description_pipeline.delivery import write_delivery_manifest
        from description_pipeline.interface_records import CHECK_POLICY_SCHEMA, check_policy_digest
        from description_pipeline.io import digest, read_data, write_json
        from description_pipeline.platform.store import PlatformStore
        from description_pipeline.stages import CONTRACT, CONTRACT_SHA256
        from description_pipeline.verification import solidworks_urdf
        from tests.v1 import test_stage_transfer as native
        from tests.v1.test_platform_store import FakeWorld

        bag = DagBag(str(DAG_DIR))
        self.module = sys.modules[bag.dags["solidworks_to_urdf"].task_dict["freeze"].python_callable.__module__]
        self.temporary = tempfile.TemporaryDirectory(prefix="linked-materialization-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "store-root"
        self.root.mkdir()
        self.world = FakeWorld()
        self.store = PlatformStore(self.world.connect, sealed=self._sealed)
        # A genuine (synthetic-byte) native capture: the transfer really verifies.
        # The native transfer identity is the digest of the sealed input inventory
        # exactly as ``capture_root`` writes it (bare staged names under input/cad).
        handoff = digest({"robot.SLDASM": hashlib.sha256(b"CAD-BYTES").hexdigest()})
        self.tree = native.capture_root(Path(self.temporary.name) / "native", run_id=self.PARENT, handoff=handoff)
        write_json(self.tree / "input/source.json", {"source": "synthetic-native"})
        write_json(self.tree / "input/records/arm.json", {"record": "synthetic-native"})
        from description_pipeline.io import file_digest

        report = read_data(self.tree / "input" / "handoff.json")
        report["package_files"] = {
            **report["package_files"],
            "source.json": file_digest(self.tree / "input/source.json"),
            "records/arm.json": file_digest(self.tree / "input/records/arm.json"),
        }
        write_json(self.tree / "input" / "handoff.json", report)
        for name in ("capture-plan", "capture", "native-runtime"):
            write_json(self.tree / f"evidence/{name}.json", {"synthetic": name})
        (self.tree / "evidence/geometry").mkdir(exist_ok=True)
        (self.tree / "evidence/geometry/part.stl").write_bytes(b"synthetic-stl")
        identity = read_data(self.tree / "evidence/evidence/collection.json")["identity"]
        native.write_manifest(self.tree / "evidence", kind="solidworks", identity=identity, evidence_class="cad")
        transfer = native.seal(
            self.tree, Path(self.temporary.name) / "fixture-native.zip", run_id=self.PARENT, handoff_sha256=handoff
        )
        write_json(self.tree / "transfer-manifest.json", transfer)
        write_json(self.tree / "input/request.json", {"request": "synthetic-frozen-original"})
        self.module._ensure_subject_layout(self.tree)
        write_json(self.tree / "model/robot.json", {"model": "synthetic"})
        (self.tree / "urdf/robot.urdf").write_text("<robot/>\n", encoding="utf-8")
        (self.tree / "meshes/part.stl").write_bytes(b"synthetic-stl")
        write_delivery_manifest(
            self.tree,
            contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
        )
        self.definition = {"source": {"bodies": [{"name": "base_link"}]}, "checks": {}}
        rebuild = patch.object(solidworks_urdf, "rebuilt_definition", return_value=self.definition)
        rebuild.start()
        self.addCleanup(rebuild.stop)

    def _sealed(self, uri: str) -> Path:
        return self.root / "sealed" / uri.removeprefix("store://sealed/")

    def _admit(self, run_id: str, *, parent: str | None = None, from_stage: str | None = None) -> None:
        self.store.admit_run(
            request_id=f"q-{run_id}",
            run_id=run_id,
            principal="portal",
            target_hardware="nd",
            main_assembly="cad/robot.SLDASM",
            dispatch={
                "stage": from_stage or "capture",
                "operation_key": f"{run_id}/dispatch:0",
                "params_digest": "a" * 64,
            },
            from_stage=from_stage,
            parent_run_id=parent,
        )
        self.world.run_states[run_id] = "running"

    def _register(self, run_id: str, stage: str) -> None:
        from description_pipeline.io import read_data

        receipt = {"run_id": run_id, "stage": stage}
        if stage == "freeze":
            receipt["tool"] = read_data(self.tree / "tool.json")
        if stage in {"discover", "capture"}:
            receipt["native_tool"] = read_data(self.tree / "reports" / "native-tool.json")
        self.module._seal_and_confirm(self.store, self.root, run_id, stage, self.tree, receipt, state="completed")

    def _producer(self, run_id: str, stage: str, seeds: list):
        from description_pipeline.delivery import read_delivery_manifest
        from description_pipeline.io import write_json
        from description_pipeline.verification import solidworks_urdf
        from description_pipeline.verification.solidworks_urdf import required_checks
        from tests.v1.test_platform_store import build_sealed_delivery

        def call(seed, output, **kwargs):
            seeds.append((stage, Path(seed)))
            output = Path(output)
            output.mkdir(parents=True, exist_ok=True)
            build_sealed_delivery(output)
            subject = read_delivery_manifest(output)["subject"]["digest"]
            receipt = {
                "run_id": run_id,
                "stage": stage,
                "state": {"verify": "verified", "publish": "published"}[stage],
                "passed": True,
                "subject_sha256": subject,
            }
            expected = required_checks(self.definition)
            checks = [{"id": name, "state": "passed", "passed": True, "details": {}} for name in expected]
            receipt["quality"] = {
                "passed": True,
                "subject_sha256": subject,
                "checks": checks,
                "required_checks": expected,
                "qualifications": solidworks_urdf.qualifications(output, self.definition, checks),
            }
            # The sealed tree must carry the same qualified report the receipt
            # binds inline: the completed gate compares the two deterministically
            # (the manifest subject excludes reports/, so overwriting the
            # placeholder is safe and member registration picks it up).
            write_json(output / "reports" / "quality.json", receipt["quality"])
            if stage == "publish":
                receipt["submission"] = {
                    "passed": True,
                    "state": "published",
                    "url": "https://example.invalid/review/1",
                    "subject_sha256": subject,
                }
            return receipt

        return call

    def _linked_stage(
        self,
        run_id: str,
        stage: str,
        *,
        source: str,
        seeds: list,
        gates: list,
        subject: str,
        repository: Path | None = None,
    ) -> dict:
        tree = self.module.flow.RunPaths(self.root, run_id).stage_output(stage)

        def identity(platform, owner):
            gates.append(("identity", owner))
            return {}

        with (
            patch.object(self.module, "_platform_binding", return_value=(self.store, self.root)),
            patch.object(self.module.flow, "portable_identity_gate", side_effect=identity),
            patch.object(self.module.flow.solidworks, stage, side_effect=self._producer(run_id, stage, seeds)),
        ):
            return self.module._portable_registered(
                run_id,
                stage,
                tree=tree,
                source_run_id=source,
                expected_subject=subject,
                repository=repository,
                base="main",
            )

    def _registered_parent(self) -> None:
        import shutil as _shutil

        self._admit(self.PARENT)
        for stage in ("freeze", "discover", "capture", "generate"):
            self._register(self.PARENT, stage)
        parent = self.module.flow.RunPaths(self.root, self.PARENT)
        _shutil.copytree(self.root / "sealed" / self.PARENT / "capture", parent.capture_dir)
        (parent.run_dir / "input").mkdir()
        _shutil.copy2(self.tree / "input" / "request.json", parent.run_dir / "input" / "request.json")
        # A real passing control for every capture byte, transfer and frozen request.
        self.module.flow.revalidate_capture(
            self.store, self.PARENT, parent.capture_dir, parent.run_dir / "input" / "request.json"
        )

    def test_a_linked_chain_materializes_reused_rows_and_sealed_inputs(self):
        import shutil as _shutil

        self._registered_parent()
        parent_rows = self.store.stage_records(self.PARENT)
        self._admit(self.CHILD, parent=self.PARENT, from_stage="verify")
        seeds, gates = [], []
        receipt = self._linked_stage(
            self.CHILD, "verify", source=self.PARENT, seeds=seeds, gates=gates, subject="b" * 64
        )
        self.assertEqual(receipt["state"], "verified")
        self.assertEqual(seeds, [("verify", self.root / "sealed" / self.PARENT / "generate")])
        self.assertEqual({owner for _, owner in gates}, {self.PARENT})
        child_rows = self.store.stage_records(self.CHILD)
        for stage in ("freeze", "discover", "capture", "generate"):
            row, source = child_rows[stage], parent_rows[stage]
            self.assertEqual((row["state"], row["reused_from_run"]), ("completed", self.PARENT))
            self.assertEqual(row["checkpoint_uri"], source["checkpoint_uri"])
            self.assertEqual(row["original_executed_at"], source["original_executed_at"])
            # The original producer's receipt travels verbatim; nothing is rewritten
            # to fit the new attempt.
            self.assertEqual(row["receipt"], source["receipt"])
            self.assertEqual(row["receipt"]["run_id"], self.PARENT)
        child_paths = self.module.flow.RunPaths(self.root, self.CHILD)
        self.assertTrue(child_paths.capture_dir.is_dir())
        self.assertEqual(
            (child_paths.run_dir / "input" / "request.json").read_bytes(),
            (self.root / "sealed" / self.PARENT / "freeze" / "input" / "request.json").read_bytes(),
        )

        # From here on no mutable parent work tree exists at all.
        _shutil.rmtree(self.root / self.PARENT)
        self._admit(self.GRANDCHILD, parent=self.CHILD, from_stage="verify")
        seeds, gates = [], []
        receipt = self._linked_stage(
            self.GRANDCHILD, "verify", source=self.CHILD, seeds=seeds, gates=gates, subject="b" * 64
        )
        self.assertEqual(receipt["state"], "verified")
        self.assertEqual(seeds, [("verify", self.root / "sealed" / self.PARENT / "generate")])
        self.assertEqual({owner for _, owner in gates}, {self.CHILD})
        self.assertEqual(self.store.stage_records(self.GRANDCHILD)["generate"]["reused_from_run"], self.CHILD)

        seeds, gates = [], []
        receipt = self._linked_stage(
            self.GRANDCHILD,
            "publish",
            source=self.CHILD,
            seeds=seeds,
            gates=gates,
            subject="c" * 64,
            repository=self.root / "model-repository",
        )
        self.assertEqual(receipt["state"], "published")
        self.assertEqual(seeds, [("publish", self.root / "sealed" / self.GRANDCHILD / "verify")])
        self.assertEqual({owner for _, owner in gates}, {self.CHILD})
        self.assertEqual(self.store.stage_records(self.GRANDCHILD)["publish"]["state"], "completed")

    def test_a_removed_parent_work_tree_leaves_the_linked_attempt_intact(self):
        import shutil as _shutil

        self._registered_parent()
        self._admit(self.CHILD, parent=self.PARENT, from_stage="verify")
        _shutil.rmtree(self.root / self.PARENT)
        receipt = self._linked_stage(self.CHILD, "verify", source=self.PARENT, seeds=[], gates=[], subject="b" * 64)
        self.assertEqual(receipt["state"], "verified")

    def test_a_dirty_parent_request_never_replaces_the_materialized_copy(self):
        from description_pipeline.io import write_json

        self._registered_parent()
        self._admit(self.CHILD, parent=self.PARENT, from_stage="verify")
        write_json(self.root / self.PARENT / "input" / "request.json", {"mutable": "untrusted"})
        receipt = self._linked_stage(self.CHILD, "verify", source=self.PARENT, seeds=[], gates=[], subject="b" * 64)
        self.assertEqual(receipt["state"], "verified")
        self.assertEqual(
            (self.root / self.CHILD / "input" / "request.json").read_bytes(),
            (self.root / "sealed" / self.PARENT / "freeze" / "input" / "request.json").read_bytes(),
        )

    def test_a_missing_source_stage_refuses_the_linked_attempt(self):
        from airflow.sdk.exceptions import AirflowFailException

        self._admit(self.PARENT)
        for stage in ("freeze", "discover", "capture"):
            self._register(self.PARENT, stage)
        self._admit(self.CHILD, parent=self.PARENT, from_stage="verify")
        with self.assertRaisesRegex(AirflowFailException, "without a confirmed source stage to reuse"):
            self._linked_stage(self.CHILD, "verify", source=self.PARENT, seeds=[], gates=[], subject="b" * 64)

    def test_a_damaged_reuse_source_refuses_instead_of_falling_back(self):
        from description_pipeline.platform.store import PlatformStoreError

        parent, child = self.PARENT, self.CHILD
        self._admit(parent)
        for stage in ("freeze", "discover", "capture", "generate"):
            self._register(parent, stage)
        state, receipt, _uri, reused, executed = self.world.stages[(parent, "generate")]
        self.world.stages[(parent, "generate")] = (
            state,
            receipt,
            f"store://sealed/{parent}/capture",
            reused,
            executed,
        )
        self._admit(child, parent=parent, from_stage="verify")
        with self.assertRaisesRegex(PlatformStoreError, "names another stage"):
            self._linked_stage(child, "verify", source=parent, seeds=[], gates=[], subject="b" * 64)

    def test_an_otherwise_valid_source_outside_the_lineage_refuses(self):
        from description_pipeline.platform.store import PlatformStoreError

        outsider = "44444444-4444-4444-8444-444444444444"
        self._admit(outsider)
        self._register(outsider, "freeze")
        self._admit(self.PARENT)
        for stage in ("freeze", "discover", "capture", "generate"):
            self._register(self.PARENT, stage)
        # The parent's registered freeze row is retargeted at an outsider tree with
        # matching bytes: only the registered lineage may justify consumption.
        state, receipt, _uri, reused, executed = self.world.stages[(self.PARENT, "freeze")]
        self.world.stages[(self.PARENT, "freeze")] = (
            state,
            receipt,
            f"store://sealed/{outsider}/freeze",
            reused,
            executed,
        )
        self._admit(self.CHILD, parent=self.PARENT, from_stage="verify")
        with self.assertRaisesRegex(PlatformStoreError, "proven origin"):
            self._linked_stage(self.CHILD, "verify", source=self.PARENT, seeds=[], gates=[], subject="b" * 64)

    def test_a_broken_parent_reuse_link_refuses(self):
        from description_pipeline.platform.store import PlatformStoreError

        parent, child, grandchild = self.PARENT, self.CHILD, self.GRANDCHILD
        self._admit(parent)
        for stage in ("freeze", "discover", "capture", "generate"):
            self._register(parent, stage)
        self._admit(child, parent=parent, from_stage="verify")
        self._admit(grandchild, parent=child, from_stage="verify")
        parent_rows = self.store.stage_records(parent)
        # Reusing the grandparent's row directly - skipping the registered parent -
        # is refused even though the receipt and locator exist and verify.
        with self.assertRaisesRegex(PlatformStoreError, "registered parent run"):
            self.store.confirm_stage(
                run_id=grandchild,
                stage="generate",
                state="completed",
                receipt=parent_rows["generate"]["receipt"],
                checkpoint_uri=parent_rows["generate"]["checkpoint_uri"],
                reused_from_run=parent,
            )

    def test_confirming_an_ancestor_locator_without_declared_stage_reuse_refuses(self):
        from description_pipeline.platform.store import PlatformStoreError

        self._registered_parent()
        self._admit(self.CHILD, parent=self.PARENT, from_stage="verify")
        self.module._materialize_linked_attempt(self.store, self.root, self.CHILD, self.PARENT)
        self._admit(self.GRANDCHILD, parent=self.CHILD, from_stage="verify")
        original = self.store.stage_records(self.PARENT)["generate"]
        with self.assertRaisesRegex(PlatformStoreError, "proven origin"):
            self.store.confirm_stage(
                run_id=self.GRANDCHILD,
                stage="generate",
                state="completed",
                receipt=original["receipt"],
                checkpoint_uri=original["checkpoint_uri"],
                original_executed_at=original["original_executed_at"],
            )

    def test_a_removed_direct_parent_reuse_link_refuses_the_grandchild(self):
        from description_pipeline.platform.store import PlatformStoreError

        self._registered_parent()
        self._admit(self.CHILD, parent=self.PARENT, from_stage="verify")
        self.module._materialize_linked_attempt(self.store, self.root, self.CHILD, self.PARENT)
        state, receipt, uri, _reused, executed = self.world.stages[(self.CHILD, "generate")]
        self.world.stages[(self.CHILD, "generate")] = (state, receipt, uri, None, executed)
        self._admit(self.GRANDCHILD, parent=self.CHILD, from_stage="verify")
        original = self.store.stage_records(self.PARENT)["generate"]
        with self.assertRaisesRegex(PlatformStoreError, "proven origin"):
            self.store.confirm_stage(
                run_id=self.GRANDCHILD,
                stage="generate",
                state="completed",
                receipt=original["receipt"],
                checkpoint_uri=original["checkpoint_uri"],
                reused_from_run=self.CHILD,
                original_executed_at=original["original_executed_at"],
            )


if __name__ == "__main__":
    unittest.main()
