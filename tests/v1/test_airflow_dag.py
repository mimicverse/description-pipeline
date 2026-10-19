"""Real Airflow DAG parsing and one mocked-endpoint DAG run (requires the Airflow venv)."""

from __future__ import annotations

import hashlib
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

from description_pipeline.io import digest, file_digest
from description_pipeline.orchestration.airflow_client import EndpointConfig
from description_pipeline.orchestration.feishu_oauth import build_actor_name

ROOT = Path(__file__).resolve().parents[2]
DAG_DIR = ROOT / "deploy/airflow/dags"
#: Absolute and outside the checkout, fixed before Airflow is imported.
AIRFLOW_HOME = pinned_airflow_home()

_MIGRATED = False


def accepted_freeze_record(
    intake: Path,
    upload_files: dict[str, str],
    config_digest: str,
    main_assembly: str,
    *,
    target_hardware: str = "m3.0",
    purpose: str = "engineering",
) -> dict:
    return {
        "target_hardware": target_hardware,
        "main_assembly": main_assembly,
        "purpose": purpose,
        "configuration": {"purpose": purpose},
        "baseline": {
            "platform_config_digest": config_digest,
            "handoff_sha256": digest(upload_files),
            "files": len(upload_files),
            "bytes": sum((intake / name).stat().st_size for name in upload_files),
            "folder": intake.name,
        },
    }


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


class _OperationRecorder:
    """Minimal operation-family double for task-level tests (no SQL involved)."""

    def __init__(self) -> None:
        self.registered: list[dict] = []
        self.claimed: list[dict] = []
        self.success_settlements: list[dict] = []

    def register_operation(self, **kwargs):
        self.registered.append(dict(kwargs))
        return SimpleNamespace(state="queued")

    def claim_operation(self, **kwargs):
        self.claimed.append(dict(kwargs))
        return SimpleNamespace(granted=True, record=SimpleNamespace(state="running"))

    def settle_native_owner_success(self, **kwargs):
        self.success_settlements.append(dict(kwargs))

    def confirmed_stage(self, _run_id, _stage):
        return None

    def confirm_stage(self, **kwargs):
        return None


class _InitialDispatchBindingDouble:
    def run_record(self, run_id):
        return {"request_id": f"portal-{run_id}"}

    def bind_initial_dispatch(self, **payload):
        self.initial_dispatch = dict(payload)
        return SimpleNamespace(state="queued")


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
            config_digest="c" * 64,
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
                {"target_hardware": "m4.0", "change_summary": "x", "purpose": "engineering"},
                {"change_summary": "x", "purpose": "engineering"},
                {"target_hardware": "   ", "change_summary": "x", "purpose": "engineering"},
                {"target_hardware": "m3.0"},
                {"target_hardware": "m3.0", "change_summary": "  ", "purpose": "engineering"},
                {"target_hardware": "m3.0", "change_summary": "x"},
            ):
                with self.subTest(conf=str(conf)), self.assertRaises(module.AirflowFailException):
                    module._submission_frame(context(conf))
            identity, routing, _config = module._submission_frame(
                context({"target_hardware": "m3.0", "change_summary": "rebind", "purpose": "engineering"})
            )
            multiline, _routing, _config = module._submission_frame(
                context(
                    {"target_hardware": "m3.0", "change_summary": "第一行\r\n第二行\r第三行", "purpose": "engineering"}
                )
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
            config_digest="c" * 64,
        )
        forged = {
            "target_hardware": "m3.0",
            "change_summary": "x",
            "purpose": "engineering",
            # A client-shaped conf claim must never name the submitter.
            "submission_principal": "cli_forged:tenant-a:ou_attacker",
        }

        def context(actor):
            return {"dag_run": SimpleNamespace(conf=forged, triggering_user_name=actor), "params": {}}

        with patch.object(module, "config_from_airflow_connection", return_value=config):
            identity, _routing, _config = module._submission_frame(
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
            config_digest="c" * 64,
        )

        def context(conf):
            return {
                "dag_run": SimpleNamespace(conf=conf, run_id="portal-child-9"),
                "ti": SimpleNamespace(task_id="freeze", map_index=-1),
                "params": {},
            }

        with (
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_endpoint", side_effect=AssertionError("endpoint contacted")),
        ):
            for conf in ({}, {"target_hardware": "m3.0"}, {"change_summary": "x"}):
                with self.subTest(conf=str(conf)), self.assertRaises(AirflowFailException):
                    dag.task_dict["freeze"].python_callable(**context(conf))

    def test_initial_dispatch_binding_uses_the_real_freeze_task_for_every_start_stage(self) -> None:
        module = self._module()

        class Platform:
            def __init__(self, start_stage):
                self.start_stage = start_stage
                self.bound = []

            def run_record(self, run_id):
                return {"request_id": "portal-request", "from_stage": self.start_stage}

            def bind_initial_dispatch(self, **payload):
                self.bound.append(payload)

        dag_run_id = "portal-start-stage"
        task_instance = SimpleNamespace(task_id="freeze", map_index=-1)
        for stage in module.STAGE_IDS:
            with self.subTest(stage=stage):
                platform = Platform(stage)
                module._bind_initial_dispatch(
                    platform,
                    run_id="run-id",
                    context={"dag_run": SimpleNamespace(run_id=dag_run_id), "ti": task_instance},
                )
                self.assertEqual(
                    platform.bound,
                    [
                        {
                            "run_id": "run-id",
                            "stage": stage,
                            "operation_key": f"portal-{dag_run_id}/{stage}:0",
                            "dag_id": "solidworks_to_urdf",
                            "dag_run_id": dag_run_id,
                            "task_id": "freeze",
                            "map_index": -1,
                        }
                    ],
                )
        platform = Platform(None)
        module._bind_initial_dispatch(
            platform,
            run_id="run-id",
            context={"dag_run": SimpleNamespace(run_id=dag_run_id), "ti": task_instance},
        )
        self.assertEqual(platform.bound[0]["stage"], module.STAGE_IDS[0])

    def test_initial_dispatch_binding_refuses_a_missing_or_wrong_task_key(self) -> None:
        from airflow.sdk.exceptions import AirflowFailException

        module = self._module()

        class Platform:
            def __init__(self):
                self.bound = []

            def run_record(self, run_id):
                return {"request_id": "portal-request", "from_stage": "capture"}

            def bind_initial_dispatch(self, **payload):
                self.bound.append(payload)

        contexts = {
            "missing_task_instance": {"dag_run": SimpleNamespace(run_id="portal-start-stage")},
            "wrong_task": {
                "dag_run": SimpleNamespace(run_id="portal-start-stage"),
                "ti": SimpleNamespace(task_id="capture", map_index=-1),
            },
            "missing_map_index": {
                "dag_run": SimpleNamespace(run_id="portal-start-stage"),
                "ti": SimpleNamespace(task_id="freeze"),
            },
            "wrong_map_index": {
                "dag_run": SimpleNamespace(run_id="portal-start-stage"),
                "ti": SimpleNamespace(task_id="freeze", map_index=0),
            },
        }
        for name, context in contexts.items():
            with self.subTest(name=name):
                platform = Platform()
                with self.assertRaises(AirflowFailException):
                    module._bind_initial_dispatch(platform, run_id="run-id", context=context)
                self.assertEqual(platform.bound, [])

    def test_freeze_requires_the_admitted_config_digest(self) -> None:
        import shutil
        import tempfile

        from airflow.sdk.exceptions import AirflowFailException

        module = self._module()
        dag = module.dag
        root = Path(tempfile.mkdtemp(prefix="freeze-digest-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        intake = root / "handoff"
        intake.mkdir()
        (intake / "assembly.SLDASM").write_bytes(b"<assembly/>")
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
            config_digest="a" * 64,
        )

        class Stub:
            def authorized_intake_dir(self, path):
                return intake

        def context(conf):
            return {
                "dag_run": SimpleNamespace(
                    conf=conf,
                    run_id="portal-upload-digest",
                    triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
                ),
                "ti": SimpleNamespace(task_id="freeze", map_index=-1),
                "params": {"handoff_path": str(intake)},
            }

        base = {
            "target_hardware": "m3.0",
            "change_summary": "x",
            "request_id": "portal-upload-digest",
        }
        accepted = {
            "target_hardware": "m3.0",
            "main_assembly": "assembly.SLDASM",
            "purpose": "engineering",
            "configuration": {"purpose": "engineering"},
            "baseline": {},
        }

        class Platform:
            def run_record(self, run_id):
                return {"request_id": "portal-upload-digest"}

            def bind_initial_dispatch(self, **payload):
                return None

            def request_record(self, request_id):
                return accepted

        converged = []
        with (
            patch.object(
                module,
                "_converge_early_freeze_failure",
                side_effect=lambda run_id, error: converged.append((run_id, str(error))),
            ),
            patch.object(
                module,
                "_store_only_platform_binding",
                return_value=(Platform(), root),
            ),
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
        ):
            with self.assertRaisesRegex(AirflowFailException, "carries no platform configuration identity"):
                dag.task_dict["freeze"].python_callable(**context(dict(base)))
            accepted.update(
                accepted_freeze_record(
                    intake,
                    {"assembly.SLDASM": file_digest(intake / "assembly.SLDASM")},
                    "a" * 64,
                    "assembly.SLDASM",
                )
            )
            with self.assertRaisesRegex(AirflowFailException, "does not match"):
                dag.task_dict["freeze"].python_callable(**context({**base, "platform_config_digest": "b" * 64}))
        self.assertEqual(len(converged), 2, "both early refusals must converge the admitted run as failed")
        self.assertIn("carries no platform configuration identity", converged[0][1])
        self.assertIn("does not match", converged[1][1])

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
            set(dag.params), {"handoff_path", "main_assembly", "target_hardware", "change_summary", "purpose"}
        )
        dag_callbacks = dag.on_failure_callback
        dag_callbacks = dag_callbacks if isinstance(dag_callbacks, (list, tuple)) else [dag_callbacks]
        self.assertEqual(
            [getattr(callback, "__name__", None) for callback in dag_callbacks],
            ["_settle_native_owner_on_failure"],
        )
        for task_id in ("discover", "capture"):
            callbacks = dag.task_dict[task_id].on_failure_callback
            callbacks = callbacks if isinstance(callbacks, (list, tuple)) else [callbacks]
            self.assertEqual(
                [getattr(callback, "__name__", None) for callback in callbacks],
                ["_record_native_failure_binding"],
            )
        handoff_param = dict(dag.params.items())["handoff_path"]
        self.assertEqual(handoff_param.schema["title"], "Engineering folder path")
        self.assertEqual(handoff_param.schema["type"], "string")
        assembly_param = dict(dag.params.items())["main_assembly"]
        self.assertEqual(assembly_param.schema["title"], "Delivered main assembly")
        purpose_param = dict(dag.params.items())["purpose"]
        self.assertEqual(purpose_param.schema["title"], "Purpose")
        self.assertEqual(purpose_param.schema["type"], "string")
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

    def _assert_freeze_routing_reaches_publish(self, module, dag, request, store_root):
        """Carry one real freeze return through capture and into publish."""

        job_request = {
            key: request[key] for key in ("run_id", "upload_files", "upload_sha256", "main_assembly")
        }
        if request.get("resume") is not None:
            job_request["resume"] = request["resume"]
        endpoint = SimpleNamespace(
            get_job=lambda run_id: {
                "run_id": run_id,
                "status": "native_complete",
                "request": job_request,
            }
        )
        native_binding = {
            "run_id": request["run_id"],
            "handoff_sha256": request["upload_sha256"],
            "hardware_id": "m3.0",
            "revision": "r1",
            "state": "native",
        }
        with (
            patch.object(module, "_endpoint", return_value=endpoint),
            patch.object(module, "_platform_binding", return_value=(SimpleNamespace(), store_root)),
            patch.object(module.native_capture, "import_native_capture", return_value=dict(native_binding)),
            patch.object(module, "carry_request_evidence", return_value=None),
            patch.object(module, "_register_native_stages", return_value=None),
            patch.object(module, "_release_registered_cache", return_value=None),
            patch.object(module, "_settle_native_owner_success", return_value=None),
        ):
            binding = dag.task_dict["capture"].python_callable(request)
        self.assertEqual(binding["repository_slug"], "mimicverse/description")
        self.assertEqual(binding["repository_base"], "main")

        repository = store_root.parent / "routing-repository"
        publish_config = SimpleNamespace(repositories={"mimicverse/description": repository})
        seen: dict = {}

        def portable(run_id, stage, **kwargs):
            seen.update(run_id=run_id, stage=stage, **kwargs)
            return {
                "state": "published",
                "passed": True,
                "subject_sha256": "a" * 64,
                "submission": {"state": "created"},
            }

        verified = {**binding, "verify": {"subject_sha256": "a" * 64}}
        with (
            patch.object(module, "config_from_airflow_connection", return_value=publish_config),
            patch.object(
                module,
                "_work_paths",
                return_value=SimpleNamespace(output_dir=store_root.parent / "routing-output"),
            ),
            patch.object(module, "_portable_registered", side_effect=portable),
            patch.object(module, "_confirm_delivery", side_effect=lambda value: value),
        ):
            dag.task_dict["publish"].python_callable(verified)
        self.assertEqual(seen["repository"], repository)
        self.assertEqual(seen["base"], "main")

    def test_task_source_identity_reports_the_loaded_dag_file(self) -> None:
        module = self._module()
        source = Path(module.__file__).resolve()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with self.assertLogs(module.log, level="INFO") as captured:
            module._log_task_identity("freeze")
        self.assertIn(
            f"task source identity task=freeze dag_file={source} dag_sha256={digest}",
            captured.output[0],
        )
        self.assertIn("package_file=", captured.output[0])
        self.assertNotIn("package_sha256=<unavailable>", captured.output[0])

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
            "platform_config_digest": "c" * 64,
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

    def test_discover_propagates_a_converged_terminal_native_failure(self):
        from airflow.sdk.exceptions import AirflowFailException

        module = self._module()
        request = {
            "run_id": "c5d1a559-4a25-52eb-a6dc-8855f9b0654d",
            "upload_files": {"assembly.SLDASM": "a" * 64},
            "upload_sha256": "b" * 64,
            "main_assembly": "assembly.SLDASM",
            "conn_id": "test",
            "platform_config_digest": "c" * 64,
        }
        endpoint = SimpleNamespace(get_job=lambda run_id: {"run_id": run_id, "status": "running"})
        original = AirflowFailException(
            "definition derivation failed: the derivation did not pass; its findings must be resolved before capture"
        )
        replacement = AirflowFailException("SolidWorks job failed: definition_derivation_failed")
        replacement.code = "definition_derivation_failed"
        replacement.detail = {"discovery": {"passed": False}}
        replacement.native_terminal_failure_converged = True
        with (
            patch.object(module, "_endpoint", return_value=endpoint),
            patch.object(module, "_poke", side_effect=original),
            patch.object(module, "_converge_terminal_native_failure", return_value=replacement) as converge,
            self.assertRaises(AirflowFailException) as caught,
        ):
            module.dag.task_dict["discover"].python_callable(
                request,
                dag_run=SimpleNamespace(run_id="portal-e2e"),
                ti=SimpleNamespace(task_id="discover", map_index=-1),
            )
        self.assertIs(caught.exception, replacement)
        self.assertIs(caught.exception.__cause__, original)
        converge.assert_called_once_with(endpoint, request)

    def test_terminal_native_failure_convergence_refuses_unknown_or_active_work(self):
        from description_pipeline.orchestration.airflow_client import EndpointError

        module = self._module()
        request = {
            "run_id": "c5d1a559-4a25-52eb-a6dc-8855f9b0654d",
            "upload_files": {"assembly.SLDASM": "a" * 64},
            "upload_sha256": "b" * 64,
            "main_assembly": "assembly.SLDASM",
            "conn_id": "test",
            "platform_config_digest": "c" * 64,
        }
        for job, expected in (
            ({"run_id": request["run_id"], "status": "running", "events": []}, None),
            (
                {
                    "run_id": request["run_id"],
                    "status": "failed",
                    "request": {
                        "run_id": request["run_id"],
                        "upload_files": request["upload_files"],
                        "upload_sha256": request["upload_sha256"],
                        "main_assembly": request["main_assembly"],
                    },
                    "events": [],
                },
                None,
            ),
            (
                {
                    "run_id": request["run_id"],
                    "status": "failed",
                    "request": {
                        "run_id": request["run_id"],
                        "upload_files": request["upload_files"],
                        "upload_sha256": request["upload_sha256"],
                        "main_assembly": request["main_assembly"],
                    },
                    "events": [{"stage": "discover", "state": "running"}],
                },
                None,
            ),
            (
                {
                    "run_id": request["run_id"],
                    "status": "failed",
                    "request": {
                        "run_id": request["run_id"],
                        "upload_files": request["upload_files"],
                        "upload_sha256": request["upload_sha256"],
                        "main_assembly": "other.SLDASM",
                    },
                    "events": [{"stage": "discover", "state": "failed"}],
                },
                None,
            ),
        ):
            endpoint = SimpleNamespace(get_job=lambda run_id, job=job: job)
            with (
                patch.object(module, "_converge_failed_stage") as converge,
                patch.object(module, "_store_failure_report") as report,
            ):
                result = module._converge_terminal_native_failure(endpoint, request)
            self.assertIs(result, expected)
            converge.assert_not_called()
            report.assert_not_called()
        endpoint = SimpleNamespace(
            get_job=lambda run_id: (_ for _ in ()).throw(EndpointError("endpoint unavailable"))
        )
        with patch.object(module, "_converge_failed_stage") as converge:
            self.assertIsNone(module._converge_terminal_native_failure(endpoint, request))
        converge.assert_not_called()

    def test_terminal_native_failure_claims_convergence_only_after_the_store_confirms(self):
        module = self._module()
        request = {
            "run_id": "c5d1a559-4a25-52eb-a6dc-8855f9b0654d",
            "upload_files": {"assembly.SLDASM": "a" * 64},
            "upload_sha256": "b" * 64,
            "main_assembly": "assembly.SLDASM",
            "conn_id": "test",
            "platform_config_digest": "c" * 64,
        }
        job = {
            "run_id": request["run_id"],
            "status": "failed",
            "error": "native failure",
            "error_code": "discovery_failed",
            "request": {
                "run_id": request["run_id"],
                "upload_files": request["upload_files"],
                "upload_sha256": request["upload_sha256"],
                "main_assembly": request["main_assembly"],
            },
            "events": [{"stage": "discover", "state": "failed"}],
        }
        endpoint = SimpleNamespace(get_job=lambda run_id: job)
        for converged in (False, True):
            with (
                patch.object(module, "_converge_failed_stage", return_value=converged),
                patch.object(module, "_store_failure_report", side_effect=RuntimeError("render failed")),
                patch.object(module, "_write_native_failure_receipt") as persist,
            ):
                failure = module._converge_terminal_native_failure(endpoint, request)
            persist.assert_called_once_with(request, job)
            self.assertIsNotNone(failure)
            self.assertEqual(
                getattr(failure, "native_terminal_failure_converged", False),
                converged,
            )

    def test_capture_remote_uncertainty_never_registers_a_local_failure(self):
        from airflow.sdk.exceptions import AirflowFailException
        from description_pipeline.orchestration.airflow_client import EndpointError

        module = self._module()
        upload = {"assembly.SLDASM": "a" * 64}
        request = {
            "run_id": "c5d1a559-4a25-52eb-a6dc-8855f9b0654d",
            "upload_files": upload,
            "upload_sha256": digest(upload),
            "main_assembly": "assembly.SLDASM",
            "conn_id": "test",
            "platform_config_digest": "c" * 64,
        }
        matching = {
            "run_id": request["run_id"],
            "upload_files": upload,
            "upload_sha256": request["upload_sha256"],
            "main_assembly": request["main_assembly"],
        }
        endpoints = (
            SimpleNamespace(
                get_job=lambda run_id: (_ for _ in ()).throw(EndpointError("endpoint unavailable"))
            ),
            SimpleNamespace(get_job=lambda run_id: {"run_id": run_id, "status": "running", "request": matching}),
            SimpleNamespace(
                get_job=lambda run_id: {
                    "run_id": run_id,
                    "status": "native_complete",
                    "request": {**matching, "main_assembly": "other.SLDASM"},
                }
            ),
        )
        for endpoint in endpoints:
            with (
                patch.object(module, "_endpoint", return_value=endpoint),
                patch.object(module, "_converge_failed_stage") as converge,
                self.assertRaises((AirflowFailException, EndpointError)),
            ):
                module.dag.task_dict["capture"].python_callable(request)
            converge.assert_not_called()

    def test_capture_local_failure_converges_after_a_proven_native_complete_job(self):
        import tempfile

        module = self._module()
        upload = {"assembly.SLDASM": "a" * 64}
        request = {
            "run_id": "c5d1a559-4a25-52eb-a6dc-8855f9b0654d",
            "upload_files": upload,
            "upload_sha256": digest(upload),
            "main_assembly": "assembly.SLDASM",
            "conn_id": "test",
            "platform_config_digest": "c" * 64,
        }
        job = {
            "run_id": request["run_id"],
            "status": "native_complete",
            "request": {
                "run_id": request["run_id"],
                "upload_files": upload,
                "upload_sha256": request["upload_sha256"],
                "main_assembly": request["main_assembly"],
            },
        }
        endpoint = SimpleNamespace(get_job=lambda run_id: job)
        with (
            tempfile.TemporaryDirectory(prefix="dag-capture-local-failure-") as store_root,
            patch.object(module, "_endpoint", return_value=endpoint),
            patch.object(module, "_platform_binding", return_value=(SimpleNamespace(), Path(store_root))),
            patch.object(
                module.native_capture,
                "import_native_capture",
                side_effect=RuntimeError("capture import failed"),
            ),
            patch.object(module, "_converge_failed_stage") as converge,
            self.assertRaisesRegex(RuntimeError, "capture import failed"),
        ):
            module.dag.task_dict["capture"].python_callable(request)
        self.assertEqual(converge.call_args.args[:2], (request["run_id"], "capture"))

    def test_capture_retries_owner_settlement_after_a_successful_import(self) -> None:
        import shutil
        import tempfile

        module = self._module()
        upload = {"assembly.SLDASM": "a" * 64}
        intake = Path(tempfile.mkdtemp(prefix="dag-capture-owner-intake-"))
        self.addCleanup(shutil.rmtree, intake, ignore_errors=True)
        request = {
            "run_id": "c5d1a559-4a25-52eb-a6dc-8855f9b0654d",
            "upload_files": upload,
            "upload_sha256": digest(upload),
            "main_assembly": "assembly.SLDASM",
            "conn_id": "test",
            "platform_config_digest": "c" * 64,
            "intake_dir": str(intake),
        }
        job = {
            "run_id": request["run_id"],
            "status": "native_complete",
            "request": {
                "run_id": request["run_id"],
                "upload_files": upload,
                "upload_sha256": request["upload_sha256"],
                "main_assembly": request["main_assembly"],
            },
        }
        endpoint = SimpleNamespace(get_job=lambda run_id: job)
        binding = {
            "run_id": request["run_id"],
            "handoff_sha256": request["upload_sha256"],
            "hardware_id": "m3.0",
            "revision": "r1",
            "state": "native",
        }
        settled: list[dict] = []

        def settle(request_value, job_value):
            settled.append(job_value)
            if len(settled) == 1:
                raise RuntimeError("store unavailable")

        with (
            tempfile.TemporaryDirectory(prefix="dag-capture-owner-retry-") as store_root,
            patch.object(module, "_endpoint", return_value=endpoint),
            patch.object(module, "_platform_binding", return_value=(SimpleNamespace(), Path(store_root))),
            patch.object(module.native_capture, "import_native_capture", return_value=dict(binding)),
            patch.object(module, "carry_request_evidence", return_value=None),
            patch.object(module, "_register_native_stages", return_value=None),
            patch.object(module, "_release_registered_cache", return_value=None),
            patch.object(module, "_converge_failed_stage") as converge,
            patch.object(module, "_settle_native_owner_success", side_effect=settle),
        ):
            with self.assertRaisesRegex(RuntimeError, "store unavailable"):
                module.dag.task_dict["capture"].python_callable(request)
            converge.assert_not_called()
            module.dag.task_dict["capture"].python_callable(request)
        self.assertEqual(settled, [job, job])

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
            "platform_config_digest": "c" * 64,
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
                "platform_config_digest": "c" * 64,
            }
            operations = _OperationRecorder()
            config = EndpointConfig(
                f"http://127.0.0.1:{server.server.server_address[1]}",
                "test-token",
                config_digest="c" * 64,
            )
            with (
                patch.dict(os.environ, {"AIRFLOW_CONN_SOLIDWORKS_WINDOWS": connection}),
                patch.object(module, "_platform_binding", return_value=(operations, None)),
                patch.object(module, "config_from_airflow_connection", return_value=config),
            ):
                started = dag.task_dict["discover"].python_callable(
                    request,
                    dag_run=SimpleNamespace(run_id="portal-native-half"),
                    ti=SimpleNamespace(task_id="discover", map_index=-1),
                )
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
            self.assertEqual([call["kind"] for call in operations.registered], ["native"])
            self.assertEqual(
                [call["operation_key"] for call in operations.registered], [f"{request['run_id']}/native:0"]
            )
            self.assertRegex(operations.registered[0]["params_digest"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                operations.claimed,
                [
                    {
                        "run_id": request["run_id"],
                        "stage": "discover",
                        "operation_key": f"{request['run_id']}/native:0",
                    }
                ],
            )
            self.assertEqual(
                operations.registered[0]["dispatch"],
                {
                    "dag_id": "solidworks_to_urdf",
                    "dag_run_id": "portal-native-half",
                    "task_id": "discover",
                    "map_index": -1,
                },
            )

    def test_a_contextless_native_intent_is_refused_before_any_operation(self) -> None:
        from airflow.sdk.exceptions import AirflowFailException

        module = self._module()
        dag = module.dag
        request = {
            "run_id": module.native_run_id("dag-run-binding"),
            "upload_files": {"assembly.SLDASM": "a" * 64},
            "upload_sha256": "a" * 64,
            "main_assembly": "assembly.SLDASM",
            "conn_id": "solidworks_windows",
            "platform_config_digest": "c" * 64,
        }

        class UnknownEndpoint:
            def get_job(self, run_id):
                raise module.EndpointNotFound(run_id)

        operations = _OperationRecorder()
        with (
            patch.object(module, "_endpoint", return_value=UnknownEndpoint()),
            patch.object(module, "_platform_binding", return_value=(operations, None)),
        ):
            with self.assertRaisesRegex(AirflowFailException, "DagRun binding"):
                dag.task_dict["discover"].python_callable(request)
            with self.assertRaisesRegex(AirflowFailException, "task identity"):
                dag.task_dict["discover"].python_callable(request, dag_run=SimpleNamespace(run_id="portal-binding"))
            with self.assertRaisesRegex(AirflowFailException, "task-instance key"):
                dag.task_dict["discover"].python_callable(
                    request,
                    dag_run=SimpleNamespace(run_id="portal-binding"),
                    task_instance=SimpleNamespace(task_id="discover"),
                )
            with self.assertRaisesRegex(AirflowFailException, "task identity"):
                dag.task_dict["discover"].python_callable(
                    request,
                    dag_run=SimpleNamespace(run_id="portal-binding"),
                    ti=SimpleNamespace(task_id="  ", map_index=-1),
                )
            with self.assertRaisesRegex(AirflowFailException, "task-instance key"):
                dag.task_dict["discover"].python_callable(
                    request,
                    dag_run=SimpleNamespace(run_id="portal-binding"),
                    ti=SimpleNamespace(task_id="discover", map_index=-2),
                )
        self.assertEqual(operations.registered, [])
        self.assertEqual(operations.claimed, [])

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

    def test_admitted_freeze_uses_its_baseline_digest_after_active_changes(self) -> None:
        import shutil
        import tempfile
        from types import SimpleNamespace

        from description_pipeline.orchestration.airflow_client import EndpointConfig, native_run_id

        module = self._module()
        dag = module.dag
        store_root = Path(tempfile.mkdtemp(prefix="dag-admitted-freeze-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        intake = store_root / "intake"
        intake.mkdir()
        (intake / "assembly.SLDASM").write_bytes(b"assembly")
        accepted_files = {"assembly.SLDASM": file_digest(intake / "assembly.SLDASM")}
        dag_run_id = "portal-admitted-freeze"
        run_id = native_run_id(dag_run_id)
        request_id = "portal-admitted-request"
        baseline_digest = "a" * 64
        started: list[str] = []
        confirmed: list[dict] = []
        platform = SimpleNamespace(
            run_record=lambda value: {"request_id": request_id} if value == run_id else None,
            bind_initial_dispatch=lambda **payload: None,
            request_record=lambda value: accepted_freeze_record(
                intake,
                accepted_files,
                baseline_digest,
                "assembly.SLDASM",
            ),
            start_run=lambda value: started.append(value),
            confirmed_stage=lambda *args, **kwargs: None,
            confirm_stage=lambda **payload: confirmed.append(payload),
            stage_records=lambda value: {},
            append_event=lambda *args, **kwargs: None,
        )
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
            store_root=store_root,
            config_digest=baseline_digest,
        )
        observed_digests: list[str | None] = []

        def resolve(_conn_id, *, config_digest=None, **_kwargs):
            observed_digests.append(config_digest)
            if config_digest is None:
                raise AssertionError("ordinary freeze consulted the changed active pointer")
            self.assertEqual(config_digest, baseline_digest)
            return config

        endpoint = SimpleNamespace(authorized_intake_dir=lambda path: intake)
        context = {
            "dag_run": SimpleNamespace(
                conf={
                    "target_hardware": "m3.0",
                    "change_summary": "admitted before active change",
                    "purpose": "engineering",
                    "request_id": request_id,
                },
                run_id=dag_run_id,
                triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
            ),
            "ti": SimpleNamespace(task_id="freeze", map_index=-1),
            "params": {"handoff_path": str(intake), "main_assembly": ""},
        }
        with (
            patch.object(module, "_store_only_platform_binding", return_value=(platform, store_root)),
            patch.object(module, "_platform_binding", return_value=(platform, store_root)),
            patch.object(module, "config_from_airflow_connection", side_effect=resolve),
            patch.object(module, "_endpoint", return_value=endpoint),
        ):
            request = dag.task_dict["freeze"].python_callable(**context)
        self.assertEqual(request["platform_config_digest"], baseline_digest)
        self.assertEqual(request["repository_slug"], "mimicverse/description")
        self.assertEqual(request["repository_base"], "main")
        self.assertTrue(observed_digests)
        self.assertEqual(set(observed_digests), {baseline_digest})
        self.assertEqual(started, [run_id])
        self.assertEqual([row["stage"] for row in confirmed], ["freeze"])
        self._assert_freeze_routing_reaches_publish(module, dag, request, store_root)

    def test_bad_connection_failure_still_registers_failed_and_blocked_rows(self) -> None:
        import shutil
        import tempfile
        from types import SimpleNamespace

        from description_pipeline.orchestration.airflow_client import EndpointProtocolError, native_run_id

        module = self._module()
        dag = module.dag
        store_root = Path(tempfile.mkdtemp(prefix="dag-bad-connection-freeze-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        dag_run_id = "portal-bad-connection"
        run_id = native_run_id(dag_run_id)
        request_id = "portal-bad-connection-request"
        started: list[str] = []
        confirmed: list[dict] = []
        platform = SimpleNamespace(
            run_record=lambda value: {"request_id": request_id} if value == run_id else None,
            bind_initial_dispatch=lambda **payload: None,
            request_record=lambda value: {
                "target_hardware": "m3.0",
                "main_assembly": "",
                "purpose": "engineering",
                "configuration": {"purpose": "engineering"},
                "baseline": {"platform_config_digest": "a" * 64},
            },
            start_run=lambda value: started.append(value),
            confirm_stage=lambda **payload: confirmed.append(payload),
            stage_records=lambda value: {},
        )

        def bad_resolver(*args, **kwargs):
            raise EndpointProtocolError("Airflow connection config identity is invalid: config_identity_legacy")

        context = {
            "dag_run": SimpleNamespace(
                conf={
                    "target_hardware": "m3.0",
                    "change_summary": "bad connection",
                    "purpose": "engineering",
                    "request_id": request_id,
                },
                run_id=dag_run_id,
                triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
            ),
            "ti": SimpleNamespace(task_id="freeze", map_index=-1),
            "params": {"handoff_path": str(store_root / "missing-intake"), "main_assembly": ""},
        }
        with (
            patch.object(module, "_store_only_platform_binding", return_value=(platform, store_root)),
            patch.object(
                module,
                "_platform_binding",
                side_effect=AssertionError("failure convergence must not re-resolve business config"),
            ),
            patch.object(module, "config_from_airflow_connection", side_effect=bad_resolver),
            self.assertRaisesRegex(EndpointProtocolError, "config_identity_legacy"),
        ):
            dag.task_dict["freeze"].python_callable(**context)
        self.assertEqual(started, [run_id])
        states = [(row["stage"], row["state"]) for row in confirmed]
        self.assertIn(("freeze", "failed"), states)
        self.assertEqual(
            [row["stage"] for row in confirmed if row["state"] == "blocked"],
            ["discover", "capture", "generate", "verify", "publish"],
        )

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
                "platform_config_digest": "c" * 64,
            }
            operations = _OperationRecorder()
            config = EndpointConfig(
                f"http://127.0.0.1:{server.server.server_address[1]}",
                "test-token",
                config_digest="c" * 64,
            )
            with (
                patch.dict(os.environ, {"AIRFLOW_CONN_SOLIDWORKS_WINDOWS": connection}),
                patch.object(module, "_platform_binding", return_value=(operations, None)),
                patch.object(module, "config_from_airflow_connection", return_value=config),
            ):
                started = dag.task_dict["discover"].python_callable(
                    request,
                    dag_run=SimpleNamespace(run_id="portal-selection"),
                    task_instance=SimpleNamespace(task_id="discover", map_index=-1),
                )
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
            self.assertEqual([call["kind"] for call in operations.registered], ["native"])
            self.assertEqual(
                operations.claimed,
                [
                    {
                        "run_id": request["run_id"],
                        "stage": "discover",
                        "operation_key": f"{request['run_id']}/native:0",
                    }
                ],
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
                    "purpose": "engineering",
                },
                run_id="portal-child-1",
                triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
            ),
            "ti": SimpleNamespace(task_id="freeze", map_index=-1),
            "params": {"handoff_path": "", "main_assembly": ""},
        }
        import shutil
        import tempfile

        store_root = Path(tempfile.mkdtemp(prefix="dag-store-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        freeze_request_evidence(
            store_root / "sealed" / parent_run_id / "freeze",
            purpose="engineering",
            request_id=parent_dag_run_id,
            target_hardware="m3.0",
            change_summary="linked capture rerun",
            main_assembly="3.0 总装1008.SLDASM",
            handoff_digest=digest(upload),
            platform_config_digest="c" * 64,
        )

        started_child_runs: list[str] = []

        class _Platform(_InitialDispatchBindingDouble):
            def confirmed_stage(self, run_id, stage):
                return {"handoff_sha256": "d" * 64} if stage in {"freeze", "capture"} else None

            def stage_records(self, run_id):
                return {"freeze": {"state": "completed", "checkpoint_uri": f"store://sealed/{run_id}/freeze"}}

            def sealed_path(self, uri):
                return store_root / "sealed" / uri.removeprefix("store://sealed/")

            def register_operation(self, **kwargs):
                return SimpleNamespace(state="queued")

            def claim_operation(self, **kwargs):
                return SimpleNamespace(granted=True, record=SimpleNamespace(state="running"))

            def start_run(self, run_id):
                started_child_runs.append(run_id)

        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
            store_root=store_root,
            config_digest="c" * 64,
        )
        with (
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(
                os.environ,
                {"OPERATOR_STATE": str(store_root.parent), "PIPELINE_STORE_ROOT": str(store_root)},
            ),
        ):
            request = dag.task_dict["freeze"].python_callable(**context)
            self.assertEqual(request["upload_files"], upload)
            self.assertEqual(request["upload_sha256"], digest(upload))
            self.assertEqual(request["main_assembly"], "3.0 总装1008.SLDASM")
            self.assertEqual(request["resume"], {"parent_run": parent_run_id, "from_stage": "capture"})
            self.assertEqual(request["purpose"], "engineering")
            # The linked attempt must declare the parent's purpose: absence and a
            # different inspection class both refuse before any native submission.
            for label, purpose in (("missing", None), ("changed", "inspection_preview")):
                forged_conf = {**context["dag_run"].conf}
                forged_conf.pop("purpose", None)
                if purpose is not None:
                    forged_conf["purpose"] = purpose
                forged = {
                    "dag_run": SimpleNamespace(
                        conf=forged_conf,
                        run_id="portal-child-1",
                        triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
                    ),
                    "ti": SimpleNamespace(task_id="freeze", map_index=-1),
                    "params": {"handoff_path": "", "main_assembly": ""},
                }
                with self.subTest(purpose=label), self.assertRaisesRegex(AirflowFailException, "purpose"):
                    dag.task_dict["freeze"].python_callable(**forged)
            # Every forged refusal below still claims the same admitted child id first;
            # the idempotent start_run proof is the single distinct id.
            self.assertIn(request["run_id"], started_child_runs)
            self.assertEqual(set(started_child_runs), {request["run_id"]})
            started = dag.task_dict["discover"].python_callable(
                request, dag_run=context["dag_run"], ti=SimpleNamespace(task_id="discover", map_index=-1)
            )
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
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(
                os.environ,
                {"OPERATOR_STATE": str(store_root.parent), "PIPELINE_STORE_ROOT": str(store_root)},
            ),
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
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(
                os.environ,
                {"OPERATOR_STATE": str(store_root.parent), "PIPELINE_STORE_ROOT": str(store_root)},
            ),
            self.assertRaises(AirflowFailException),
        ):
            dag.task_dict["freeze"].python_callable(**context)

    def test_linked_freeze_retry_reseals_the_intake_without_a_native_job(self) -> None:
        import os as os_module
        import shutil
        import tempfile
        from types import SimpleNamespace

        from airflow.sdk.exceptions import AirflowFailException
        from description_pipeline.io import digest, read_data
        from description_pipeline.orchestration.airflow_client import EndpointConfig, native_run_id
        from description_pipeline.request_evidence import freeze_request_evidence
        from description_pipeline.sources.solidworks.revision import package_inventory
        from description_pipeline.sources.solidworks.selection import validate_file_index

        module = self._module()
        dag = module.dag
        parent_dag_run_id = "portal-parent-freeze"
        parent_run_id = native_run_id(parent_dag_run_id)
        child_dag_run_id = "portal-child-freeze"
        child_run_id = native_run_id(child_dag_run_id)
        store_root = Path(tempfile.mkdtemp(prefix="dag-freeze-retry-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        intake = store_root / "intake" / "M3.0.3"
        (intake / "parts").mkdir(parents=True)
        (intake / "3.0 总装.SLDASM").write_bytes(b"assembly-bytes")
        (intake / "M3.0.3_20261016.SLDASM").write_bytes(b"delivery-assembly-bytes")
        (intake / "parts" / "bracket.SLDPRT").write_bytes(b"part-bytes")
        upload_files = validate_file_index(package_inventory(intake))
        upload_sha = digest(upload_files)
        baseline = {
            "handoff_sha256": upload_sha,
            "files": len(upload_files),
            "bytes": sum(entry.stat().st_size for entry in intake.rglob("*") if entry.is_file()),
            "folder": "M3.0.3",
            "platform_config_digest": "c" * 64,
        }
        freeze_request_evidence(
            store_root / "sealed" / parent_run_id / "freeze",
            purpose="engineering",
            request_id=f"portal-{parent_dag_run_id}",
            target_hardware="m3.0",
            change_summary="freeze retry",
            main_assembly="3.0 总装.SLDASM",
            handoff_digest=upload_sha,
            platform_config_digest="c" * 64,
        )
        confirmed: list[dict] = []
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
            store_root=store_root,
            config_digest="c" * 64,
        )
        runs = {
            child_run_id: {
                "run_id": child_run_id,
                "state": "queued",
                "request_id": f"portal-{child_dag_run_id}",
                "parent_run_id": parent_run_id,
                "from_stage": "freeze",
                "tool_identity": None,
            },
            parent_run_id: {
                "run_id": parent_run_id,
                "state": "failed",
                "request_id": f"portal-{parent_dag_run_id}",
                "parent_run_id": None,
                "from_stage": None,
                "tool_identity": None,
            },
        }
        requests = {
            f"portal-{parent_dag_run_id}": {
                "request_id": f"portal-{parent_dag_run_id}",
                "principal": "cli_smoke:smoke-tenant:ou_smoke",
                "target_hardware": "m3.0",
                "main_assembly": "3.0 总装.SLDASM",
                "sealed_at": None,
                "purpose": "engineering",
                "configuration": {"purpose": "engineering"},
                "baseline": baseline,
            },
            f"portal-{child_dag_run_id}": {
                "request_id": f"portal-{child_dag_run_id}",
                "principal": "cli_smoke:smoke-tenant:ou_smoke",
                "target_hardware": "m3.0",
                "main_assembly": "M3.0.3_20261016.SLDASM",
                "sealed_at": None,
                "purpose": "engineering",
                "configuration": {"purpose": "engineering"},
                "baseline": baseline,
            },
        }

        class _Platform(_InitialDispatchBindingDouble):
            def start_run(self, run_id):
                return None

            def run_record(self, run_id):
                return runs.get(run_id)

            def request_record(self, request_id):
                return requests.get(request_id)

            def stage_records(self, run_id):
                return {
                    parent_run_id: {
                        "freeze": {
                            "stage": "freeze",
                            "state": "failed",
                            "receipt": {"error": "early freeze refusal"},
                        }
                    }
                }.get(run_id, {})

            def confirmed_stage(self, run_id, stage):
                return None

            def confirm_stage(self, **kwargs):
                confirmed.append(kwargs)

            def append_event(self, run_id, *, stage, kind, payload=None):
                return None

        class Stub:
            def authorized_intake_dir(self, handoff_path):
                self.seen = handoff_path
                return intake

        stub = Stub()
        context = {
            "dag_run": SimpleNamespace(
                conf={
                    "parent_dag_run_id": parent_dag_run_id,
                    "resume_from": "freeze",
                    "handoff_path": str(intake),
                    "purpose": "engineering",
                    "target_hardware": "m3.0",
                    "change_summary": "freeze retry",
                    "main_assembly": "M3.0.3_20261016.SLDASM",
                },
                run_id=child_dag_run_id,
                triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
            ),
            "ti": SimpleNamespace(task_id="freeze", map_index=-1),
            "params": {"handoff_path": "", "main_assembly": ""},
        }
        with (
            patch.object(module, "_endpoint", return_value=stub),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(
                os_module.environ,
                {
                    "PLATFORM_CONFIG_DIGEST": "c" * 64,
                    "OPERATOR_STATE": str(store_root.parent),
                    "PIPELINE_STORE_ROOT": str(store_root),
                },
            ),
        ):
            request = dag.task_dict["freeze"].python_callable(**context)
        self.assertEqual(stub.seen, str(intake))
        self.assertEqual(request["run_id"], child_run_id)
        self.assertEqual(request["upload_files"], upload_files)
        self.assertEqual(request["upload_sha256"], upload_sha)
        self.assertEqual(request["repository_slug"], "mimicverse/description")
        self.assertEqual(request["repository_base"], "main")
        self.assertEqual(request["main_assembly"], "M3.0.3_20261016.SLDASM")
        self.assertEqual(request["request_id"], f"portal-{child_dag_run_id}")
        # The child is a fresh native leg re-sealed from the same intake: no resume binding.
        self.assertNotIn("resume", request)
        self.assertNotIn("linux_owned", request)
        self._assert_freeze_routing_reaches_publish(module, dag, request, store_root)
        self.assertIs(confirmed[-1]["state"], "completed")
        self.assertEqual(confirmed[-1]["run_id"], child_run_id)
        sealed_request = read_data(store_root / "sealed" / child_run_id / "freeze" / "input" / "request.json")
        self.assertEqual(sealed_request["handoff_digest"], upload_sha)
        self.assertEqual(sealed_request["main_assembly"], "M3.0.3_20261016.SLDASM")
        self.assertEqual(requests[f"portal-{parent_dag_run_id}"]["main_assembly"], "3.0 总装.SLDASM")
        self.assertEqual(sealed_request["request_id"], f"portal-{child_dag_run_id}")
        self.assertTrue((store_root / "sealed" / child_run_id / "freeze" / "input" / "handoff.json").is_file())
        # A forged purpose refuses and converges the child as a failed freeze.
        forged = {
            **context,
            "dag_run": SimpleNamespace(
                conf={**context["dag_run"].conf, "purpose": "inspection_preview"},
                run_id=child_dag_run_id,
                triggering_user_name=context["dag_run"].triggering_user_name,
            ),
        }
        with (
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(
                os_module.environ,
                {
                    "PLATFORM_CONFIG_DIGEST": "c" * 64,
                    "OPERATOR_STATE": str(store_root.parent),
                    "PIPELINE_STORE_ROOT": str(store_root),
                },
            ),
            self.assertRaises(AirflowFailException),
        ):
            dag.task_dict["freeze"].python_callable(**forged)
        self.assertTrue(any(row["state"] == "failed" for row in confirmed))
        # A tampered intake refuses and converges instead of resealing changed bytes.
        (intake / "3.0 总装.SLDASM").write_bytes(b"tampered-bytes")
        with (
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(
                os_module.environ,
                {
                    "PLATFORM_CONFIG_DIGEST": "c" * 64,
                    "OPERATOR_STATE": str(store_root.parent),
                    "PIPELINE_STORE_ROOT": str(store_root),
                },
            ),
            self.assertRaises(AirflowFailException),
        ):
            dag.task_dict["freeze"].python_callable(**context)
        self.assertTrue(any(row["state"] == "failed" for row in confirmed))

    def test_linked_freeze_retry_without_parent_freeze_seal_uses_child_admission(self) -> None:
        import hashlib
        import json as json_module
        import os as os_module
        import shutil
        import tempfile
        from types import SimpleNamespace

        from airflow.sdk.exceptions import AirflowFailException
        from description_pipeline.io import digest, read_data
        from description_pipeline.orchestration.airflow_client import EndpointConfig, native_run_id
        from description_pipeline.sources.solidworks.revision import package_inventory
        from description_pipeline.sources.solidworks.selection import validate_file_index

        module = self._module()
        dag = module.dag
        parent_dag_run_id = "portal-parent-freeze-old"
        parent_run_id = native_run_id(parent_dag_run_id)
        child_dag_run_id = "portal-child-freeze-old"
        child_run_id = native_run_id(child_dag_run_id)
        store_root = Path(tempfile.mkdtemp(prefix="dag-freeze-retry-old-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        intake = store_root / "intake" / "M3.0.3_20261016"
        (intake / "parts").mkdir(parents=True)
        (intake / "3.0 总装.SLDASM").write_bytes(b"assembly-bytes")
        (intake / "parts" / "bracket.SLDPRT").write_bytes(b"part-bytes")
        upload_files = validate_file_index(package_inventory(intake))
        upload_sha = digest(upload_files)
        selection = (
            f"selection intake={intake} files={len(upload_files)} upload_sha256={upload_sha} "
            "main_assembly=3.0 总装.SLDASM endpoint_conn=solidworks_windows"
        )
        log_record = {
            "timestamp": "2026-10-16T13:51:10.237754Z",
            "level": "info",
            "event": selection,
            "logger": "unusual_prefix_solidworks_to_urdf",
            "try_number": 1,
            "run_id": parent_dag_run_id,
            "ti_id": "01a144fb-1252-77b2-b363-12c7beb80fad",
            "task_id": "freeze",
            "dag_id": "solidworks_to_urdf",
            "map_index": -1,
            "filename": "solidworks_to_urdf.py",
            "lineno": 1505,
        }
        log_bytes = (json_module.dumps(log_record, ensure_ascii=False) + "\n").encode()
        log_path = store_root / "freeze-attempt-1.log"
        log_path.write_bytes(log_bytes)
        log_sha = hashlib.sha256(log_bytes).hexdigest()
        provenance = {
            "source": "parent_freeze_failure_log",
            "path": str(log_path),
            "sha256": log_sha,
            "parent_run_id": parent_run_id,
        }
        baseline = {
            "handoff_sha256": upload_sha,
            "files": len(upload_files),
            "bytes": sum(entry.stat().st_size for entry in intake.rglob("*") if entry.is_file()),
            "folder": intake.name,
            "provenance": provenance,
            "platform_config_digest": "c" * 64,
        }
        confirmed: list[dict] = []
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
            store_root=store_root,
            config_digest="c" * 64,
        )
        runs = {
            child_run_id: {
                "run_id": child_run_id,
                "state": "queued",
                "request_id": f"portal-{child_dag_run_id}",
                "parent_run_id": parent_run_id,
                "from_stage": "freeze",
                "tool_identity": None,
            },
            parent_run_id: {
                "run_id": parent_run_id,
                "state": "failed",
                "request_id": f"portal-{parent_dag_run_id}",
                "parent_run_id": None,
                "from_stage": None,
                "tool_identity": None,
            },
        }
        requests = {
            f"portal-{parent_dag_run_id}": {
                "request_id": f"portal-{parent_dag_run_id}",
                "principal": "cli_smoke:smoke-tenant:ou_smoke",
                "target_hardware": "m3.0",
                "main_assembly": "3.0 总装.SLDASM",
                "sealed_at": None,
                "purpose": "engineering",
                "configuration": {"purpose": "engineering"},
                "baseline": None,
            },
            f"portal-{child_dag_run_id}": {
                "request_id": f"portal-{child_dag_run_id}",
                "principal": "cli_smoke:smoke-tenant:ou_smoke",
                "target_hardware": "m3.0",
                "main_assembly": "3.0 总装.SLDASM",
                "sealed_at": None,
                "purpose": "engineering",
                "configuration": {"purpose": "engineering"},
                "baseline": dict(baseline),
            },
        }

        class _Platform(_InitialDispatchBindingDouble):
            def start_run(self, run_id):
                return None

            def run_record(self, run_id):
                return runs.get(run_id)

            def request_record(self, request_id):
                return requests.get(request_id)

            def stage_records(self, run_id):
                if run_id != parent_run_id:
                    return {}
                return {
                    "freeze": {
                        "stage": "freeze",
                        "state": "failed",
                        "receipt": {
                            "error": "AirflowFailException: PLATFORM_CONFIG_DIGEST is unavailable",
                            "airflow_failure": {"path": str(log_path), "sha256": log_sha},
                        },
                    }
                }

            def confirmed_stage(self, run_id, stage):
                return None

            def confirm_stage(self, **kwargs):
                confirmed.append(kwargs)

            def append_event(self, run_id, *, stage, kind, payload=None):
                return None

        class Stub:
            def authorized_intake_dir(self, handoff_path):
                return intake

        context = {
            "dag_run": SimpleNamespace(
                conf={
                    "parent_dag_run_id": parent_dag_run_id,
                    "resume_from": "freeze",
                    "handoff_path": str(intake),
                    "purpose": "engineering",
                    "target_hardware": "m3.0",
                    "change_summary": "freeze retry",
                },
                run_id=child_dag_run_id,
                triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
            ),
            "ti": SimpleNamespace(task_id="freeze", map_index=-1),
            "params": {"handoff_path": "", "main_assembly": ""},
        }
        self.assertFalse((store_root / "sealed" / parent_run_id / "freeze" / "input" / "request.json").exists())
        with (
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(
                os_module.environ,
                {
                    "PLATFORM_CONFIG_DIGEST": "c" * 64,
                    "OPERATOR_STATE": str(store_root.parent),
                    "PIPELINE_STORE_ROOT": str(store_root),
                },
            ),
        ):
            request = dag.task_dict["freeze"].python_callable(**context)
        self.assertEqual(request["upload_sha256"], upload_sha)
        self.assertEqual(request["repository_slug"], "mimicverse/description")
        self.assertEqual(request["repository_base"], "main")
        self.assertEqual(request["main_assembly"], "3.0 总装.SLDASM")
        self.assertNotIn("resume", request)
        self.assertIs(confirmed[-1]["state"], "completed")
        sealed_request = read_data(store_root / "sealed" / child_run_id / "freeze" / "input" / "request.json")
        self.assertEqual(sealed_request["handoff_digest"], upload_sha)
        # A child whose recorded provenance disagrees with the re-resolved evidence refuses
        # and converges the child as a failed freeze.
        requests[f"portal-{child_dag_run_id}"]["baseline"]["provenance"] = {
            **provenance,
            "sha256": "d" * 64,
        }
        with (
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(
                os_module.environ,
                {
                    "PLATFORM_CONFIG_DIGEST": "c" * 64,
                    "OPERATOR_STATE": str(store_root.parent),
                    "PIPELINE_STORE_ROOT": str(store_root),
                },
            ),
            self.assertRaises(AirflowFailException),
        ):
            dag.task_dict["freeze"].python_callable(**context)
        self.assertTrue(any(row["state"] == "failed" for row in confirmed))

    def test_linked_freeze_retry_preserved_provenance_reseals_for_the_grandchild(self) -> None:
        import hashlib
        import os as os_module
        import shutil
        import tempfile
        from types import SimpleNamespace

        from airflow.sdk.exceptions import AirflowFailException
        from description_pipeline.io import digest, read_data
        from description_pipeline.orchestration.airflow_client import EndpointConfig, native_run_id
        from description_pipeline.request_evidence import freeze_request_evidence
        from description_pipeline.sources.solidworks.revision import package_inventory
        from description_pipeline.sources.solidworks.selection import validate_file_index

        module = self._module()
        dag = module.dag
        parent_dag_run_id = "portal-child-freeze-keep"
        parent_run_id = native_run_id(parent_dag_run_id)
        grandparent_run_id = "36a79670-ef6e-5b8f-87ae-621b1181999c"
        child_dag_run_id = "portal-grandchild-freeze-keep"
        child_run_id = native_run_id(child_dag_run_id)
        store_root = Path(tempfile.mkdtemp(prefix="dag-freeze-retry-keep-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        intake = store_root / "intake" / "M3.0.3_20261016"
        (intake / "parts").mkdir(parents=True)
        (intake / "3.0 总装.SLDASM").write_bytes(b"assembly-bytes")
        (intake / "parts" / "bracket.SLDPRT").write_bytes(b"part-bytes")
        upload_files = validate_file_index(package_inventory(intake))
        upload_sha = digest(upload_files)
        anchor = store_root / "freeze-attempt-1.log"
        anchor.write_bytes(b"anchor-evidence-bytes")
        provenance = {
            "source": "parent_freeze_failure_log",
            "path": str(anchor),
            "sha256": hashlib.sha256(anchor.read_bytes()).hexdigest(),
            "parent_run_id": grandparent_run_id,
        }
        baseline = {
            "handoff_sha256": upload_sha,
            "files": len(upload_files),
            "bytes": sum(entry.stat().st_size for entry in intake.rglob("*") if entry.is_file()),
            "folder": intake.name,
            "provenance": provenance,
            "platform_config_digest": "c" * 64,
        }
        freeze_request_evidence(
            store_root / "sealed" / parent_run_id / "freeze",
            purpose="engineering",
            request_id=f"portal-{parent_dag_run_id}",
            target_hardware="m3.0",
            change_summary="freeze retry",
            main_assembly="3.0 总装.SLDASM",
            handoff_digest=upload_sha,
            platform_config_digest="c" * 64,
        )
        confirmed: list[dict] = []
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
            store_root=store_root,
            config_digest="c" * 64,
        )
        runs = {
            child_run_id: {
                "run_id": child_run_id,
                "state": "queued",
                "request_id": f"portal-{child_dag_run_id}",
                "parent_run_id": parent_run_id,
                "from_stage": "freeze",
                "tool_identity": None,
            },
            parent_run_id: {
                "run_id": parent_run_id,
                "state": "failed",
                "request_id": f"portal-{parent_dag_run_id}",
                "parent_run_id": None,
                "from_stage": None,
                "tool_identity": None,
            },
        }
        requests = {
            f"portal-{parent_dag_run_id}": {
                "request_id": f"portal-{parent_dag_run_id}",
                "principal": "cli_smoke:smoke-tenant:ou_smoke",
                "target_hardware": "m3.0",
                "main_assembly": "3.0 总装.SLDASM",
                "sealed_at": None,
                "purpose": "engineering",
                "configuration": {"purpose": "engineering"},
                "baseline": dict(baseline),
            },
            f"portal-{child_dag_run_id}": {
                "request_id": f"portal-{child_dag_run_id}",
                "principal": "cli_smoke:smoke-tenant:ou_smoke",
                "target_hardware": "m3.0",
                "main_assembly": "3.0 总装.SLDASM",
                "sealed_at": None,
                "purpose": "engineering",
                "configuration": {"purpose": "engineering"},
                "baseline": dict(baseline),
            },
        }

        class _Platform(_InitialDispatchBindingDouble):
            def start_run(self, run_id):
                return None

            def run_record(self, run_id):
                return runs.get(run_id)

            def request_record(self, request_id):
                return requests.get(request_id)

            def stage_records(self, run_id):
                if run_id != parent_run_id:
                    return {}
                return {
                    "freeze": {
                        "stage": "freeze",
                        "state": "failed",
                        "receipt": {"error": "freeze refused for the child"},
                    }
                }

            def confirmed_stage(self, run_id, stage):
                return None

            def confirm_stage(self, **kwargs):
                confirmed.append(kwargs)

            def append_event(self, run_id, *, stage, kind, payload=None):
                return None

        class Stub:
            def authorized_intake_dir(self, handoff_path):
                return intake

        context = {
            "dag_run": SimpleNamespace(
                conf={
                    "parent_dag_run_id": parent_dag_run_id,
                    "resume_from": "freeze",
                    "handoff_path": str(intake),
                    "purpose": "engineering",
                    "target_hardware": "m3.0",
                    "change_summary": "freeze retry",
                },
                run_id=child_dag_run_id,
                triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
            ),
            "ti": SimpleNamespace(task_id="freeze", map_index=-1),
            "params": {"handoff_path": "", "main_assembly": ""},
        }
        with (
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(
                os_module.environ,
                {
                    "PLATFORM_CONFIG_DIGEST": "c" * 64,
                    "OPERATOR_STATE": str(store_root.parent),
                    "PIPELINE_STORE_ROOT": str(store_root),
                },
            ),
        ):
            request = dag.task_dict["freeze"].python_callable(**context)
        self.assertEqual(request["upload_sha256"], upload_sha)
        self.assertEqual(request["repository_slug"], "mimicverse/description")
        self.assertEqual(request["repository_base"], "main")
        self.assertNotIn("resume", request)
        self.assertIs(confirmed[-1]["state"], "completed")
        sealed_request = read_data(store_root / "sealed" / child_run_id / "freeze" / "input" / "request.json")
        self.assertEqual(sealed_request["handoff_digest"], upload_sha)
        # A grandchild whose provenance disagrees with the re-validated parent evidence refuses.
        requests[f"portal-{child_dag_run_id}"]["baseline"]["provenance"] = {**provenance, "sha256": "d" * 64}
        with (
            patch.object(module, "_endpoint", return_value=Stub()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(os_module.environ, {"PLATFORM_CONFIG_DIGEST": "c" * 64}),
            self.assertRaises(AirflowFailException),
        ):
            dag.task_dict["freeze"].python_callable(**context)
        self.assertTrue(any(row["state"] == "failed" for row in confirmed))

    def test_linked_publish_rebinds_routing_from_conf_over_null_parent_meta(self) -> None:
        from types import SimpleNamespace

        module = self._module()
        dag = module.dag
        import shutil
        import tempfile

        from description_pipeline.orchestration.airflow_client import native_run_id
        from description_pipeline.request_evidence import freeze_request_evidence

        store_root = Path(tempfile.mkdtemp(prefix="dag-linked-"))
        self.addCleanup(shutil.rmtree, store_root, ignore_errors=True)
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
            store_root=store_root,
            config_digest="e" * 64,
        )
        parent_run_id = native_run_id("portal-parent-2")
        freeze_request_evidence(
            store_root / "sealed" / parent_run_id / "freeze",
            purpose="engineering",
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

        started_child_runs: list[str] = []

        class _Platform(_InitialDispatchBindingDouble):
            def confirmed_stage(self, run_id, stage):
                return {"handoff_sha256": "d" * 64} if stage in {"freeze", "capture"} else None

            def stage_records(self, run_id):
                return {
                    "freeze": {"state": "completed", "checkpoint_uri": f"store://sealed/{run_id}/freeze"},
                    "capture": {"state": "completed", "checkpoint_uri": f"store://sealed/{run_id}/capture"},
                }

            def sealed_path(self, uri):
                return store_root / "sealed" / uri.removeprefix("store://sealed/")

            def start_run(self, run_id):
                started_child_runs.append(run_id)

        context = {
            "dag_run": SimpleNamespace(
                conf={
                    "parent_dag_run_id": "portal-parent-2",
                    "resume_from": "publish",
                    "target_hardware": "m3.0",
                    "change_summary": "rebound publish",
                    "purpose": "engineering",
                },
                run_id="portal-child-2",
                triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
            ),
            "ti": SimpleNamespace(task_id="freeze", map_index=-1),
            "params": {},
        }
        with (
            patch.object(module, "config_from_airflow_connection", return_value=config),
            patch.object(module, "_platform_binding", return_value=(_Platform(), store_root)),
            patch.object(module, "_store_only_platform_binding", return_value=(_Platform(), store_root)),
            patch.dict(
                os.environ,
                {"OPERATOR_STATE": str(store_root.parent), "PIPELINE_STORE_ROOT": str(store_root)},
            ),
        ):
            request = dag.task_dict["freeze"].python_callable(**context)
            self.assertTrue(request["linux_owned"])
            self.assertEqual(request["repository_slug"], "mimicverse/description")
            self.assertEqual(request["repository_base"], "main")
            self.assertEqual(started_child_runs, [request["run_id"]])
            forged = {
                **context,
                "dag_run": SimpleNamespace(
                    conf={**context["dag_run"].conf, "purpose": "inspection_preview"},
                    run_id="portal-child-2",
                    triggering_user_name=context["dag_run"].triggering_user_name,
                ),
            }
            from airflow.sdk.exceptions import AirflowFailException

            with self.assertRaisesRegex(AirflowFailException, "purpose"):
                dag.task_dict["freeze"].python_callable(**forged)
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
        self.registered: list[dict] = []
        self.claimed: list[dict] = []
        self.sequence: list[str] = []
        self.accepted_request: dict | None = None

        def register_operation(**payload):
            self.registered.append(payload)
            self.sequence.append("register")

        def claim_operation(**payload):
            self.claimed.append(payload)
            self.sequence.append("claim")
            return SimpleNamespace(granted=True, record=SimpleNamespace(state="running"))

        platform = SimpleNamespace(
            confirm_stage=lambda **payload: self.calls.append(payload),
            confirmed_stage=lambda *args, **kwargs: None,
            stage_records=lambda run_id: {},
            request_record=lambda request_id: self.accepted_request,
            start_run=lambda run_id: self.started.append(run_id),
            append_event=lambda run_id, *, stage, kind, payload=None: self.live_events.append(
                {"run_id": run_id, "stage": stage, "kind": kind, "payload": payload}
            ),
            register_operation=register_operation,
            claim_operation=claim_operation,
        )
        binding = patch.object(self.module, "_platform_binding", return_value=(platform, self.root))
        binding.start()
        self.addCleanup(binding.stop)

    def _settle_terminal_discover_failure(self, world, store, request: dict, dag_run_id: str) -> None:
        """Finish the exact task-level marker and DagRun-terminal settlement sequence."""

        from description_pipeline.platform import flow

        world.dag_runs[(self.module.DAG_ID, dag_run_id)] = "failed"
        for task_id, state, try_number in (
            ("freeze", "success", 1),
            ("discover", "failed", 1),
            ("capture", "upstream_failed", 0),
            ("generate", "upstream_failed", 0),
            ("verify", "upstream_failed", 0),
            ("publish", "upstream_failed", 0),
        ):
            world.task_instances[(self.module.DAG_ID, task_id, dag_run_id, -1)] = state
            world.task_try_numbers[(self.module.DAG_ID, task_id, dag_run_id, -1)] = try_number

        class TaskInstance:
            run_id = dag_run_id
            task_id = "discover"
            map_index = -1

            def __init__(self):
                self.values = {}

            def xcom_pull(self, *, task_ids, key=None):
                if task_ids == "freeze" and key is None:
                    return request
                return self.values.get((task_ids, key))

            def xcom_push(self, *, key, value):
                self.values[(self.task_id, key)] = value

        ti = TaskInstance()
        self.module._record_native_failure_binding({"ti": ti})
        ti.task_id = "publish"
        with (
            patch.object(self.module, "_work_paths", return_value=flow.RunPaths(self.root, request["run_id"])),
            patch.object(self.module, "_platform_binding", return_value=(store, self.root)),
        ):
            self.module._settle_native_owner_on_failure({"ti": ti})

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

    def test_the_publish_intent_is_registered_and_claimed_before_the_stage(self) -> None:
        tree = self._tree(stage="publish")
        receipt = {
            "run_id": "run-1",
            "state": "published",
            "passed": True,
            "subject_sha256": "a" * 64,
            "submission": {"state": "created"},
        }

        def run_stage(*_args, **_kwargs):
            self.sequence.append("stage")
            return receipt

        with patch.object(self.module.flow, "run_stage", side_effect=run_stage):
            result = self.module._portable_registered(
                "run-1",
                "publish",
                tree=tree,
                expected_subject="a" * 64,
                base="main",
                context={
                    "dag_run": SimpleNamespace(run_id="portal-publish-1"),
                    "ti": SimpleNamespace(task_id="publish", map_index=-1, xcom_push=lambda **kwargs: None),
                },
            )
        self.assertEqual(result, receipt)
        # The registered and claimed intent precedes the one external write.
        self.assertEqual(self.sequence, ["register", "claim", "stage"])
        self.assertEqual(
            [(call["stage"], call["kind"], call["operation_key"]) for call in self.registered],
            [("publish", "publish", "run-1/publish:0")],
        )
        self.assertRegex(self.registered[0]["params_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            self.registered[0]["dispatch"],
            {
                "dag_id": "solidworks_to_urdf",
                "dag_run_id": "portal-publish-1",
                "task_id": "publish",
                "map_index": -1,
            },
        )
        self.assertEqual(
            self.claimed,
            [{"run_id": "run-1", "stage": "publish", "operation_key": "run-1/publish:0"}],
        )

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

        run_dir = Path(self.temporary.name) / "runs" / "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "request.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        intake = Path(self.temporary.name) / "intake"
        (intake / "cad").mkdir(parents=True)
        (intake / "cad" / "part.sldprt").write_bytes(b"cad-bytes")
        (intake / "assembly.sldasm").write_bytes(b"assembly-bytes")
        upload_files = {
            "assembly.sldasm": file_digest(intake / "assembly.sldasm"),
            "cad/part.sldprt": file_digest(intake / "cad" / "part.sldprt"),
        }
        self.accepted_request = accepted_freeze_record(
            intake,
            upload_files,
            "c" * 64,
            "assembly.sldasm",
        )
        from description_pipeline.platform import flow

        paths = flow.RunPaths(run_dir.parent, "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d")
        stages = run_dir / "stages"
        self.module._register_freeze_stage(
            paths,
            "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d",
            request_id="portal-freeze",
            intake=intake,
            upload_files=upload_files,
            main_assembly="assembly.sldasm",
            config_digest="c" * 64,
        )
        call = self.calls[0]
        self.assertEqual(call["stage"], "freeze")
        self.assertEqual(call["state"], "completed")
        self.assertEqual(call["receipt"]["state"], "frozen")
        self.assertEqual(call["receipt"]["handoff_sha256"], digest(upload_files))
        tree = stages / "freeze"
        self.assertTrue((tree / "input" / "cad" / "part.sldprt").is_file())
        self.assertTrue((tree / "input" / "assembly.sldasm").is_file())
        self.assertTrue((tree / "input" / "request.json").is_file())
        manifest = read_delivery_manifest(tree)
        self.assertIn("input/assembly.sldasm", {record["path"] for record in manifest["subject"]["files"]})
        checks = [
            event["payload"]["check"]
            for event in self.live_events
            if isinstance(event["payload"], dict) and isinstance(event["payload"].get("check"), dict)
        ]
        self.assertEqual(
            [(item["boundary"], item["id"], item["state"]) for item in checks],
            [
                ("input", "handoff.admission", "passed"),
                ("output", "handoff.integrity", "passed"),
            ],
        )
        view = self.module.stage_view(
            {
                "run_id": "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d",
                "request": {"handoff_sha256": digest(upload_files)},
                "events": [
                    {"stage": event["stage"], **event["payload"]}
                    for event in self.live_events
                ],
            }
        )
        freeze = next(stage for stage in view["stages"] if stage["id"] == "freeze")
        self.assertEqual(freeze["state"], "completed")
        self.assertEqual(freeze["checks_passed"], 2)

    def test_freeze_refuses_missing_or_changed_original_bytes(self) -> None:
        from airflow.sdk.exceptions import AirflowFailException
        from description_pipeline.platform import flow

        run_id = "1f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
        intake = Path(self.temporary.name) / "intake-refusal"
        intake.mkdir()
        (intake / "assembly.sldasm").write_bytes(b"assembly-bytes")
        (intake / "part.sldprt").write_bytes(b"part-bytes")
        expected = {
            "assembly.sldasm": file_digest(intake / "assembly.sldasm"),
            "part.sldprt": file_digest(intake / "part.sldprt"),
        }
        self.accepted_request = accepted_freeze_record(
            intake,
            expected,
            "c" * 64,
            "assembly.sldasm",
        )
        run_dir = Path(self.temporary.name) / "runs" / run_id
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "request.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        paths = flow.RunPaths(run_dir.parent, run_id)

        with self.assertRaises(AirflowFailException):
            self.module._register_freeze_stage(
                paths,
                run_id,
                request_id="portal-freeze",
                intake=intake,
                upload_files={"assembly.sldasm": expected["assembly.sldasm"]},
                main_assembly="assembly.sldasm",
                config_digest="c" * 64,
            )
        self.assertFalse(any(call["state"] == "completed" for call in self.calls))

    def test_freeze_requires_every_admitted_request_fact(self) -> None:
        import copy

        from airflow.sdk.exceptions import AirflowFailException
        from description_pipeline.platform import flow

        run_id = "2f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
        intake = Path(self.temporary.name) / "intake-admission"
        intake.mkdir()
        (intake / "assembly.SLDASM").write_bytes(b"assembly-bytes")
        upload_files = {"assembly.SLDASM": file_digest(intake / "assembly.SLDASM")}
        accepted = accepted_freeze_record(intake, upload_files, "c" * 64, "assembly.SLDASM")
        run_dir = Path(self.temporary.name) / "runs" / run_id
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "request.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        paths = flow.RunPaths(run_dir.parent, run_id)
        mutations = {
            "handoff": lambda row: row["baseline"].__setitem__("handoff_sha256", "f" * 64),
            "files": lambda row: row["baseline"].__setitem__("files", 99),
            "bytes": lambda row: row["baseline"].__setitem__("bytes", row["baseline"]["bytes"] + 1),
            "main": lambda row: row.__setitem__("main_assembly", "other.SLDASM"),
            "target": lambda row: row.__setitem__("target_hardware", ""),
            "purpose": lambda row: row.__setitem__("purpose", "other"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.calls.clear()
                self.live_events.clear()
                self.accepted_request = copy.deepcopy(accepted)
                mutate(self.accepted_request)
                with self.assertRaises(AirflowFailException):
                    self.module._register_freeze_stage(
                        paths,
                        run_id,
                        request_id="portal-freeze",
                        intake=intake,
                        upload_files=upload_files,
                        main_assembly="assembly.SLDASM",
                        config_digest="c" * 64,
                    )
                self.assertFalse(any(call["state"] == "completed" for call in self.calls))

    def test_a_baseline_refusal_converges_fresh_and_linked_registration(self) -> None:
        from airflow.sdk.exceptions import AirflowFailException
        from description_pipeline.platform import flow
        from description_pipeline.platform.store import PlatformStore
        from tests.v1.test_platform_store import FakeWorld

        intake = Path(self.temporary.name) / "intake-baseline-refusal"
        intake.mkdir()
        (intake / "assembly.SLDASM").write_bytes(b"assembly-bytes")
        upload_files = {"assembly.SLDASM": file_digest(intake / "assembly.SLDASM")}

        def dispatch(run_id: str) -> dict:
            return {
                "stage": "freeze",
                "operation_key": f"{run_id}/freeze:0",
                "params_digest": "a" * 64,
                "dag_id": self.module.DAG_ID,
                "dag_run_id": f"portal-{run_id}",
                "task_id": "freeze",
                "map_index": -1,
            }

        cases = (
            ("fresh", None, None),
            ("linked", "8f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d", "8f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"),
        )
        for label, parent_run, parent_reference in cases:
            with self.subTest(path=label):
                run_id = (
                    "6f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
                    if parent_run is None
                    else "7f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
                )
                world = FakeWorld()
                store = PlatformStore(world.connect, sealed=lambda uri: self.root / uri)
                if parent_run is not None:
                    store.admit_run(
                        request_id=f"portal-{parent_run}",
                        run_id=parent_run,
                        principal="portal",
                        target_hardware="m3.0",
                        main_assembly="assembly.SLDASM",
                        configuration={"purpose": "engineering"},
                        baseline={
                            "platform_config_digest": "c" * 64,
                            "handoff_sha256": digest(upload_files),
                            "files": len(upload_files),
                            "bytes": (intake / "assembly.SLDASM").stat().st_size,
                            "folder": intake.name,
                        },
                        dispatch=dispatch(parent_run),
                    )
                    world.run_states[parent_run] = "failed"
                request_id = f"portal-{run_id}"
                store.admit_run(
                    request_id=request_id,
                    run_id=run_id,
                    principal="portal",
                    target_hardware="m3.0",
                    main_assembly="assembly.SLDASM",
                    configuration={"purpose": "engineering"},
                    baseline={
                        "platform_config_digest": "c" * 64,
                        "handoff_sha256": "f" * 64,
                        "files": len(upload_files),
                        "bytes": (intake / "assembly.SLDASM").stat().st_size,
                        "folder": intake.name,
                    },
                    dispatch=dispatch(run_id),
                    from_stage="freeze" if parent_reference is not None else None,
                    parent_run_id=parent_reference,
                )
                paths = flow.RunPaths(self.root, run_id)
                with (
                    patch.object(self.module, "_platform_binding", return_value=(store, self.root)),
                    self.assertRaises(AirflowFailException) as caught,
                ):
                    self.module._register_freeze_stage(
                        paths,
                        run_id,
                        request_id=request_id,
                        intake=intake,
                        upload_files=upload_files,
                        main_assembly="assembly.SLDASM",
                        config_digest="c" * 64,
                    )
                self.assertIn("different native handoff digest", str(caught.exception))
                records = store.stage_records(run_id)
                self.assertEqual(records["freeze"]["state"], "failed")
                self.assertIn("different native handoff digest", records["freeze"]["receipt"]["error"])
                self.assertEqual(
                    [records[stage]["state"] for stage in ("discover", "capture", "generate", "verify", "publish")],
                    ["blocked", "blocked", "blocked", "blocked", "blocked"],
                )
                self.assertEqual(store.run_record(run_id)["state"], "failed")
                self.assertEqual(world.operations[f"{run_id}/freeze:0"][1], "failed")

    def test_the_freeze_stage_starts_the_admitted_run(self) -> None:
        run_dir = Path(self.temporary.name) / "runs" / "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "request.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        intake = Path(self.temporary.name) / "intake-start"
        intake.mkdir()
        (intake / "assembly.sldasm").write_bytes(b"assembly-bytes")
        upload_files = {"assembly.sldasm": file_digest(intake / "assembly.sldasm")}
        self.accepted_request = accepted_freeze_record(
            intake,
            upload_files,
            "c" * 64,
            "assembly.sldasm",
        )
        from description_pipeline.platform import flow

        paths = flow.RunPaths(run_dir.parent, "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d")
        self.module._register_freeze_stage(
            paths,
            "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d",
            request_id="portal-freeze",
            intake=intake,
            upload_files=upload_files,
            main_assembly="assembly.sldasm",
            config_digest="c" * 64,
        )
        self.assertEqual(self.started, ["0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"])

    def test_an_early_freeze_refusal_converges_the_admitted_run(self) -> None:
        """A refusal before the first driver action still lands as a terminal failure: the
        admitted run is claimed once, freeze records its original error, every later stage
        records blocked - and no CAD or native action is ever triggered."""
        from airflow.sdk.exceptions import AirflowFailException

        from description_pipeline.orchestration.airflow_client import native_run_id

        intake = Path(self.temporary.name) / "early-intake"
        intake.mkdir()
        (intake / "assembly.SLDASM").write_bytes(b"<assembly/>")
        (intake / "second.SLDASM").write_bytes(b"<assembly/>")

        class IntakeDouble:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def authorized_intake_dir(self, path: str) -> Path:
                self.calls.append(f"authorized_intake_dir:{path}")
                return intake

        endpoint = IntakeDouble()
        admitted_request = {"value": "portal-early-digest"}
        bindings: list[dict] = []
        platform = SimpleNamespace(
            run_record=lambda run_id: {"request_id": admitted_request["value"]},
            bind_initial_dispatch=lambda **payload: bindings.append(payload),
            request_record=lambda request_id: {
                "target_hardware": "m3.0",
                "main_assembly": "",
                "purpose": "engineering",
                "configuration": {"purpose": "engineering"},
                "baseline": {"platform_config_digest": "a" * 64},
            },
            start_run=lambda run_id: self.started.append(run_id),
            confirm_stage=lambda **payload: self.calls.append(payload),
            stage_records=lambda run_id: {},
        )
        config = EndpointConfig(
            "http://127.0.0.1:1",
            "t" * 32,
            targets={"m3.0": {"repository_slug": "mimicverse/description", "base": "main"}},
            config_digest="a" * 64,
        )
        dag = self.module.dag

        def refusal(run_id: str, conf: dict, params: dict) -> str:
            self.started.clear()
            self.calls.clear()
            endpoint.calls.clear()
            bindings.clear()
            admitted_request["value"] = str(conf.get("request_id") or params.get("request_id") or "")
            with (
                patch.object(self.module, "_platform_binding", return_value=(platform, self.root)),
                patch.object(self.module, "_store_only_platform_binding", return_value=(platform, self.root)),
                patch.object(self.module, "_endpoint", return_value=endpoint),
                patch.object(self.module, "config_from_airflow_connection", return_value=config),
                self.assertRaises(AirflowFailException) as caught,
            ):
                dag.task_dict["freeze"].python_callable(
                    dag_run=SimpleNamespace(
                        conf=conf,
                        run_id=run_id,
                        triggering_user_name=build_actor_name("cli_smoke:smoke-tenant:ou_smoke", "smoke"),
                    ),
                    ti=SimpleNamespace(task_id="freeze", map_index=-1),
                    params=params,
                )
            uuid = native_run_id(run_id)
            self.assertEqual(
                bindings,
                [
                    {
                        "run_id": uuid,
                        "stage": "freeze",
                        "operation_key": f"portal-{run_id}/freeze:0",
                        "dag_id": "solidworks_to_urdf",
                        "dag_run_id": run_id,
                        "task_id": "freeze",
                        "map_index": -1,
                    }
                ],
                "the initial dispatch is bound before an early refusal converges",
            )
            self.assertEqual(self.started, [uuid], "the admitted run is claimed exactly once")
            failed = [call for call in self.calls if call["state"] == "failed"]
            self.assertEqual([call["stage"] for call in failed], ["freeze"])
            self.assertIn("AirflowFailException", failed[0]["receipt"]["error"])
            self.assertEqual(
                [call["stage"] for call in self.calls if call["state"] == "blocked"],
                ["discover", "capture", "generate", "verify", "publish"],
            )
            return str(caught.exception)

        base = {"target_hardware": "m3.0", "change_summary": "x", "request_id": "portal-early-digest"}
        message = refusal("portal-early-digest", dict(base), {"handoff_path": str(intake)})
        self.assertIn("Select a main assembly explicitly", message)
        self.assertEqual(endpoint.calls, [f"authorized_intake_dir:{intake}"])
        # An even earlier entry refusal (before any intake contact) converges identically.
        early = {"change_summary": "x", "request_id": "portal-early-target"}
        message = refusal("portal-early-target", early, {"handoff_path": str(intake)})
        self.assertIn("target_hardware is required", message)
        self.assertEqual(endpoint.calls, [])

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

    def test_a_capture_failure_builds_the_discover_stage_from_the_sealed_prepared_tree(self) -> None:
        import hashlib
        import json
        import zipfile

        from description_pipeline.io import digest, file_digest, read_data, write_json
        from description_pipeline.platform import flow
        from description_pipeline.schemas import TOOL_SCHEMA

        run_id = "cfa3e725-abc5-5b60-94ad-6ba838110408"
        run_dir = self.root / run_id
        derived = run_dir / "definition" / "derived"
        derived.mkdir(parents=True)
        (derived / "assembly.SLDASM").write_bytes(b"assembly")
        write_json(derived / "capture-plan.json", {"plan": "fixture"})
        write_json(derived / "source.json", {"source": "fixture"})
        (derived / "records").mkdir()
        write_json(derived / "records" / "arm.json", {"record": "fixture"})
        native_files = {"assembly.SLDASM": file_digest(derived / "assembly.SLDASM")}
        handoff = digest(native_files)
        full_upload = {**native_files, "excluded.SLDPRT": "d" * 64}
        full_upload_sha = digest(full_upload)
        from tests.sources.selection_support import sealed_selection

        discovery = {
            "native_files": native_files,
            "handoff_sha256": handoff,
            "raw": {
                "files": native_files,
                "source_selection": sealed_selection(
                    full_upload,
                    "assembly.SLDASM",
                    edges={"assembly.SLDASM": []},
                ),
            },
        }
        archive_path = run_dir / "definition" / "capture-request.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("discovery/native-discovery.json", json.dumps(discovery))
            archive.writestr("discovery/findings.json", "{}")
            archive.writestr("discovery/identity-resolution.json", "{}")
            archive.writestr("capture-plan.json", (derived / "capture-plan.json").read_bytes())
            archive.writestr("source.json", (derived / "source.json").read_bytes())
            archive.writestr("manifest.json", "{}")
        prepared = {
            "assembly.SLDASM": file_digest(derived / "assembly.SLDASM"),
            "capture-plan.json": file_digest(derived / "capture-plan.json"),
            "source.json": file_digest(derived / "source.json"),
            "records/arm.json": file_digest(derived / "records" / "arm.json"),
            "discovery/findings.json": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "discovery/identity-resolution.json": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "discovery/native-discovery.json": hashlib.sha256(json.dumps(discovery).encode()).hexdigest(),
        }
        native_tool = {
            "schema_version": TOOL_SCHEMA,
            "pipeline_id": "solidworks-to-urdf",
            "version": "1.5.0",
            "source_sha256": "a" * 64,
            "runtime": {"role": "native"},
        }
        portable_tool = {**native_tool, "runtime": {"role": "portable"}}
        from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes
        from tests.v1.test_native_recovery import stage_view

        request_evidence = request_evidence_bytes(
            build_request_evidence(
                purpose="inspection_preview",
                request_id="portal-recovery-fixture",
                target_hardware="m3.0",
                change_summary="recovery fixture",
                main_assembly="assembly.SLDASM",
                handoff_digest=full_upload_sha,
                platform_config_digest="c" * 64,
            )
        )
        (run_dir / "stages" / "freeze" / "input").mkdir(parents=True)
        (run_dir / "stages" / "freeze" / "input" / "request.json").write_bytes(request_evidence)
        write_json(run_dir / "stages" / "freeze" / "tool.json", portable_tool)
        job = {
            "run_id": run_id,
            "status": "failed",
            "request": {
                "run_id": run_id,
                "main_assembly": "assembly.SLDASM",
                "upload_files": full_upload,
                "upload_sha256": full_upload_sha,
            },
            "events": [
                {"stage": "discover", "state": "completed"},
                {"stage": "capture", "state": "failed"},
            ],
            "prepared_files": prepared,
            "capture_request": {"sha256": file_digest(archive_path)},
            "native_tool": native_tool,
            "handoff_sha256": handoff,
            "stages": stage_view(handoff_sha256=handoff, run_id=run_id),
        }
        request = {
            "run_id": run_id,
            "conn_id": "test",
            "platform_config_digest": "c" * 64,
            "upload_sha256": handoff,
            "main_assembly": "assembly.SLDASM",
        }
        with patch.object(self.module, "_work_paths", return_value=flow.RunPaths(self.root, run_id)):
            self.module._seal_native_discovery(request, job)
        tree = run_dir / "seal" / "native-discover"
        self.assertTrue((tree / "input" / "cad" / "assembly.SLDASM").is_file())
        self.assertTrue((tree / "input" / "records").is_dir())
        self.assertTrue((tree / "input" / "source.json").is_file())
        self.assertTrue((tree / "input" / "records" / "arm.json").is_file())
        self.assertTrue((tree / "evidence" / "discovery.json").is_file())
        self.assertTrue((tree / "evidence" / "capture-plan.json").is_file())
        self.assertTrue((tree / "reports" / "native-tool.json").is_file())
        handoff_record = read_data(tree / "input" / "handoff.json")
        self.assertEqual(handoff_record["input_receipt"]["inventory"], native_files)
        self.assertEqual(handoff_record["package_files"]["cad/assembly.SLDASM"], native_files["assembly.SLDASM"])
        self.assertEqual(
            handoff_record["package_files"]["records/arm.json"],
            file_digest(derived / "records" / "arm.json"),
        )
        transfer = read_data(tree / "transfer-manifest.json")
        self.assertEqual(transfer["handoff_sha256"], handoff)
        self.assertTrue((tree / "manifest.json").is_file())
        self.assertIs(self.calls[-1]["settle_operations"], False)
        self.assertEqual(self.calls[-1]["stage"], "discover")
        self.assertEqual(self.calls[-1]["state"], "completed")

    def test_native_failure_marker_binds_settlement_to_the_exact_failed_task(self) -> None:
        from types import SimpleNamespace

        module = self.module
        run_dir = Path(self.temporary.name) / "runs" / "native-marker"
        (run_dir / "reports").mkdir(parents=True)
        (run_dir / "reports" / "native-job-success.json").write_text("{}", encoding="utf-8")
        request = {
            "run_id": "cfa3e725-abc5-5b60-94ad-6ba838110408",
            "conn_id": "test",
            "platform_config_digest": "c" * 64,
        }

        class TaskInstance:
            run_id = "portal-marker-run"
            map_index = -1

            def __init__(self):
                self.task_id = "capture"
                self.values = {}

            def xcom_pull(self, *, task_ids, key=None):
                if task_ids == "freeze" and key is None:
                    return request
                return self.values.get((task_ids, key))

            def xcom_push(self, *, key, value):
                self.values[(self.task_id, key)] = value

        ti = TaskInstance()
        module._record_native_failure_binding({"ti": ti})
        marker = ti.values[("capture", module.NATIVE_FAILURE_MARKER)]
        self.assertEqual(marker["task_id"], "capture")
        self.assertEqual(marker["request_sha256"], digest(request))

        settled: list[tuple[str, Path]] = []
        ti.task_id = "publish"
        with (
            patch.object(
                module,
                "_work_paths",
                return_value=SimpleNamespace(run_dir=run_dir),
            ),
            patch.object(
                module,
                "_settle_native_owner_success_from_receipt",
                side_effect=lambda pulled, path: settled.append((pulled["run_id"], path)),
            ),
        ):
            module._settle_native_owner_on_failure({"ti": ti})
            self.assertEqual(settled, [(request["run_id"], run_dir / "reports" / "native-job-success.json")])
            ti.values[("discover", module.NATIVE_FAILURE_MARKER)] = {
                **marker,
                "task_id": "discover",
            }
            module._settle_native_owner_on_failure({"ti": ti})
        self.assertEqual(len(settled), 1, "ambiguous failure markers must never settle")

    def test_discover_task_marker_uses_the_receipt_terminal_stage(self) -> None:
        from types import SimpleNamespace

        from description_pipeline.orchestration.airflow_client import JOB_SCHEMA

        module = self.module
        run_dir = Path(self.temporary.name) / "runs" / "native-discover-marker"
        (run_dir / "reports").mkdir(parents=True)
        receipt = run_dir / "reports" / "native-job-failed.json"
        request = {
            "run_id": "cfa3e725-abc5-5b60-94ad-6ba838110408",
            "conn_id": "test",
            "platform_config_digest": "c" * 64,
        }

        class TaskInstance:
            run_id = "portal-discover-marker"
            map_index = -1
            task_id = "discover"

            def __init__(self):
                self.values = {}

            def xcom_pull(self, *, task_ids, key=None):
                if task_ids == "freeze" and key is None:
                    return request
                return self.values.get((task_ids, key))

            def xcom_push(self, *, key, value):
                self.values[(self.task_id, key)] = value

        for terminal_stage in ("discover", "capture"):
            with self.subTest(terminal_stage=terminal_stage):
                receipt.write_text(
                    json.dumps(
                        {
                            "schema_version": JOB_SCHEMA,
                            "pipeline_id": module.PIPELINE_ID,
                            "run_id": request["run_id"],
                            "request": {"run_id": request["run_id"]},
                            "status": "failed",
                            "events": [{"stage": terminal_stage, "state": "failed"}],
                        }
                    ),
                    encoding="utf-8",
                )
                ti = TaskInstance()
                module._record_native_failure_binding({"ti": ti})
                settled: list[tuple[dict, Path, str]] = []
                ti.task_id = "publish"
                with (
                    patch.object(module, "_work_paths", return_value=SimpleNamespace(run_dir=run_dir)),
                    patch.object(
                        module,
                        "_settle_native_owner_failure_from_receipt",
                        side_effect=lambda pulled, path, *, reported_stage, output=settled: output.append(
                            (pulled, path, reported_stage)
                        ),
                    ),
                ):
                    module._settle_native_owner_on_failure({"ti": ti})
                self.assertEqual(settled, [(request, receipt, terminal_stage)])

    def test_a_repeated_freeze_confirmation_consumes_the_confirmed_fact(self) -> None:
        run_dir = Path(self.temporary.name) / "runs" / "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "request.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        intake = Path(self.temporary.name) / "intake-retry"
        intake.mkdir()
        (intake / "assembly.sldasm").write_bytes(b"assembly-bytes")
        upload_files = {"assembly.sldasm": file_digest(intake / "assembly.sldasm")}
        from description_pipeline.platform import flow

        paths = flow.RunPaths(run_dir.parent, "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d")

        records: dict[tuple[str, str], dict] = {}
        confirmations: list[str] = []
        events: list[dict] = []

        class _RetainingPlatform:
            def start_run(self, run_id):
                return {"run_id": run_id, "state": "running"}

            def confirm_stage(
                self,
                *,
                run_id,
                stage,
                state,
                receipt,
                checkpoint_uri=None,
                original_executed_at=None,
                settle_operations=True,
            ):
                records[(run_id, stage)] = dict(receipt)
                confirmations.append(stage)

            def confirmed_stage(self, run_id, stage):
                return records.get((run_id, stage))

            def stage_records(self, run_id):
                return {
                    stage: {"state": "completed"}
                    for candidate, stage in records
                    if candidate == run_id
                }

            def append_event(self, run_id, *, stage, kind, payload=None):
                events.append({"run_id": run_id, "stage": stage, "kind": kind, "payload": payload})

            def request_record(self, request_id):
                return accepted_freeze_record(
                    intake,
                    upload_files,
                    "c" * 64,
                    "assembly.sldasm",
                )

        platform = _RetainingPlatform()
        with patch.object(self.module, "_platform_binding", return_value=(platform, self.root)):
            self.module._register_freeze_stage(
                paths,
                "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d",
                request_id="portal-freeze",
                intake=intake,
                upload_files=upload_files,
                main_assembly="assembly.sldasm",
                config_digest="c" * 64,
            )
            self.module._register_freeze_stage(
                paths,
                "0f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d",
                request_id="portal-freeze",
                intake=intake,
                upload_files=upload_files,
                main_assembly="assembly.sldasm",
                config_digest="c" * 64,
            )
        self.assertEqual(confirmations, ["freeze"])
        checks = [
            event["payload"]["check"]["id"]
            for event in events
            if isinstance(event["payload"], dict) and isinstance(event["payload"].get("check"), dict)
        ]
        self.assertEqual(checks, ["handoff.admission", "handoff.integrity"])

    def test_failed_native_stage_uses_the_terminal_operation_event(self) -> None:
        job = {
            "run_id": "run-1",
            "status": "failed",
            "events": [
                {"stage": "freeze", "state": "completed"},
                {"stage": "discover", "state": "running"},
                {"stage": "discover", "state": "failed"},
            ],
        }
        self.assertEqual(self.module._failed_native_stage(job), "discover")
        self.assertIsNone(self.module._failed_native_stage({"events": []}))
        self.assertIsNone(
            self.module._failed_native_stage(
                {"events": [{"stage": "discover", "state": "running"}]}
            )
        )

    def test_replayed_failed_stage_only_fills_missing_blocked_rows(self) -> None:
        calls: list[dict] = []

        class _TerminalPlatform:
            def stage_records(self, run_id):
                return {"discover": {"state": "failed", "receipt": {"error": "original"}}}

            def confirm_stage(self, **payload):
                calls.append(payload)

        self.module._converge_failed_stage(
            "5f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d",
            "discover",
            RuntimeError("replayed"),
            bound_platform=_TerminalPlatform(),
            bound_store_root=self.root,
        )
        self.assertEqual(
            [(row["stage"], row["state"]) for row in calls],
            [
                ("capture", "blocked"),
                ("generate", "blocked"),
                ("verify", "blocked"),
                ("publish", "blocked"),
            ],
        )

    def test_native_selection_failure_converges_discover_without_overwriting_freeze(self) -> None:
        from description_pipeline.orchestration.airflow_client import JOB_SCHEMA
        from description_pipeline.platform import flow
        from description_pipeline.platform.store import PlatformStore
        from tests.v1.test_platform_store import FakeWorld

        run_id = "4f5b8f2c-6a3d-4e7b-9c1d-2e3f4a5b6c7d"
        request_id = "portal-native-failure"
        config_digest = "c" * 64
        world = FakeWorld()
        store = PlatformStore(
            world.connect,
            sealed=lambda uri: self.root / "sealed" / uri.removeprefix("store://sealed/"),
        )
        intake = Path(self.temporary.name) / "native-failure-intake"
        intake.mkdir()
        (intake / "assembly.SLDASM").write_bytes(b"assembly-bytes")
        upload_files = {"assembly.SLDASM": file_digest(intake / "assembly.SLDASM")}
        store.admit_run(
            request_id=request_id,
            run_id=run_id,
            principal="portal",
            target_hardware="m3.0",
            main_assembly="assembly.SLDASM",
            configuration={"purpose": "engineering"},
            baseline={
                "platform_config_digest": config_digest,
                "handoff_sha256": digest(upload_files),
                "files": len(upload_files),
                "bytes": (intake / "assembly.SLDASM").stat().st_size,
                "folder": intake.name,
            },
            dispatch={
                "stage": "freeze",
                "operation_key": f"{run_id}/freeze:0",
                "params_digest": "a" * 64,
                "dag_id": self.module.DAG_ID,
                "dag_run_id": "portal-native-failure",
                "task_id": "freeze",
                "map_index": -1,
            },
        )
        run_dir = self.root / run_id
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "request.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        with patch.object(self.module, "_platform_binding", return_value=(store, self.root)):
            self.module._register_freeze_stage(
                flow.RunPaths(self.root, run_id),
                run_id,
                request_id=request_id,
                intake=intake,
                upload_files=upload_files,
                main_assembly="assembly.SLDASM",
                config_digest=config_digest,
            )
        operation_key = f"{run_id}/native:0"
        dispatch = {
            "dag_id": self.module.DAG_ID,
            "dag_run_id": "portal-native-failure",
            "task_id": "discover",
            "map_index": -1,
        }
        store.register_operation(
            run_id=run_id,
            stage="discover",
            kind="native",
            operation_key=operation_key,
            params_digest="b" * 64,
            dispatch=dispatch,
        )
        claimed = store.claim_operation(run_id=run_id, stage="discover", operation_key=operation_key)
        self.assertTrue(claimed.granted)

        job = {
            "schema_version": JOB_SCHEMA,
            "pipeline_id": self.module.PIPELINE_ID,
            "request": {
                "run_id": run_id,
                "upload_files": upload_files,
                "upload_sha256": digest(upload_files),
                "main_assembly": "assembly.SLDASM",
            },
            "run_id": run_id,
            "status": "failed",
            "error": "SelectionError: the staged main opened with CAD-reported errors",
            "error_code": "selection_open_errors",
            "selection": {
                "state": "reading",
                "staged_files": upload_files,
                "upload_sha256": digest(upload_files),
            },
            "detail": {
                "open_status": {"already_open": False, "errors": 2, "warnings": 1, "open_exception": None},
                "problems": [{"code": "selection_open_errors", "document": "assembly.SLDASM"}],
            },
            "events": [
                {"stage": "freeze", "state": "completed"},
                {"stage": "discover", "state": "running"},
                {"stage": "discover", "state": "failed"},
            ],
        }
        request = {
            "run_id": run_id,
            "upload_files": upload_files,
            "upload_sha256": digest(upload_files),
            "main_assembly": "assembly.SLDASM",
            "conn_id": "test",
            "platform_config_digest": config_digest,
        }
        endpoint = SimpleNamespace(get_job=lambda value: job)
        with (
            patch.object(self.module, "_platform_binding", return_value=(store, self.root)),
            patch.object(
                self.module,
                "_work_paths",
                return_value=flow.RunPaths(self.root, run_id),
            ),
        ):
            failure = self.module._converge_terminal_native_failure(endpoint, request)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.code, "selection_open_errors")
        self.assertEqual(failure.detail, job["detail"])
        self.assertTrue(failure.native_terminal_failure_converged)

        records = store.stage_records(run_id)
        self.assertEqual(records["freeze"]["state"], "completed")
        self.assertEqual(records["discover"]["state"], "failed")
        self.assertEqual(records["discover"]["receipt"]["error_code"], "selection_open_errors")
        self.assertEqual(records["discover"]["receipt"]["detail"], failure.detail)
        self.assertEqual(
            [records[stage]["state"] for stage in ("capture", "generate", "verify", "publish")],
            ["blocked", "blocked", "blocked", "blocked"],
        )
        self.assertEqual(store.run_record(run_id)["state"], "failed")
        self._settle_terminal_discover_failure(world, store, request, "portal-native-failure")
        settled = store.register_operation(
            run_id=run_id,
            stage="discover",
            kind="native",
            operation_key=operation_key,
            params_digest="b" * 64,
            dispatch=dispatch,
        )
        self.assertEqual(settled.state, "failed")

    def test_definition_derivation_failure_converges_the_real_e2e_shape(self) -> None:
        from airflow.sdk.exceptions import AirflowFailException
        from description_pipeline.orchestration.airflow_client import JOB_SCHEMA
        from description_pipeline.platform import flow
        from description_pipeline.platform.store import PlatformStore
        from tests.v1.test_platform_store import FakeWorld

        run_id = "c5d1a559-4a25-52eb-a6dc-8855f9b0654d"
        request_id = "portal-portal-20261019T140447-01d47dba"
        config_digest = "c" * 64
        world = FakeWorld()
        store = PlatformStore(
            world.connect,
            sealed=lambda uri: self.root / "sealed" / uri.removeprefix("store://sealed/"),
        )
        intake = Path(self.temporary.name) / "definition-failure-intake"
        intake.mkdir()
        (intake / "M3.0.3_20261016.SLDASM").write_bytes(b"assembly-bytes")
        upload_files = {"M3.0.3_20261016.SLDASM": file_digest(intake / "M3.0.3_20261016.SLDASM")}
        store.admit_run(
            request_id=request_id,
            run_id=run_id,
            principal="portal",
            target_hardware="m3.0",
            main_assembly="M3.0.3_20261016.SLDASM",
            configuration={"purpose": "inspection_preview"},
            baseline={
                "platform_config_digest": config_digest,
                "handoff_sha256": digest(upload_files),
                "files": len(upload_files),
                "bytes": (intake / "M3.0.3_20261016.SLDASM").stat().st_size,
                "folder": intake.name,
            },
            dispatch={
                "stage": "freeze",
                "operation_key": f"{request_id}/freeze:0",
                "params_digest": "a" * 64,
                "dag_id": self.module.DAG_ID,
                "dag_run_id": request_id,
                "task_id": "freeze",
                "map_index": -1,
            },
        )
        run_dir = self.root / run_id
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "request.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        with patch.object(self.module, "_platform_binding", return_value=(store, self.root)):
            self.module._register_freeze_stage(
                flow.RunPaths(self.root, run_id),
                run_id,
                request_id=request_id,
                intake=intake,
                upload_files=upload_files,
                main_assembly="M3.0.3_20261016.SLDASM",
                config_digest=config_digest,
            )
        operation_key = f"{run_id}/native:0"
        dispatch = {
            "dag_id": self.module.DAG_ID,
            "dag_run_id": request_id,
            "task_id": "discover",
            "map_index": -1,
        }
        store.register_operation(
            run_id=run_id,
            stage="discover",
            kind="native",
            operation_key=operation_key,
            params_digest="b" * 64,
            dispatch=dispatch,
        )
        claimed = store.claim_operation(run_id=run_id, stage="discover", operation_key=operation_key)
        self.assertTrue(claimed.granted)

        detail = {
            "discovery": {
                "discovery_sha256": "749bc6dbefa630051ee21016d3f5a5bcda9fb5622a3bc0b1864c5fd8f405f0fa",
                "findings": [
                    {
                        "blocking": False,
                        "code": "discovery.revision_missing",
                        "detail": {"publication_blocker": "revision"},
                        "message": "structural revision is not recorded in the CAD records",
                        "object": "assembly",
                        "schema_version": "solidworks-to-urdf.discovery-finding/v1",
                    },
                    {
                        "blocking": True,
                        "code": "discovery.configuration_unproven",
                        "detail": {
                            "configuration": "Default",
                            "document_state": {
                                "after": {
                                    "configuration": "Default",
                                    "configuration_needs_rebuild": False,
                                    "needs_rebuild2_value": 0,
                                    "read_only": True,
                                    "save_flag": True,
                                },
                                "before": {
                                    "configuration": "Default",
                                    "configuration_needs_rebuild": False,
                                    "needs_rebuild2_value": 1,
                                    "read_only": True,
                                    "save_flag": False,
                                },
                                "used_api": (
                                    "IModelDoc2.GetSaveFlag/IModelDocExtension.NeedsRebuild2/"
                                    "IConfiguration.Name/IConfiguration.NeedsRebuild"
                                ),
                            },
                        },
                        "message": (
                            "the delivery configuration is not observed as the saved active configuration; "
                            "a recorded name alone is not evidence"
                        ),
                        "object": "assembly",
                        "schema_version": "solidworks-to-urdf.discovery-finding/v1",
                    },
                    {
                        "blocking": True,
                        "code": "discovery.exporter_binding_mismatch",
                        "detail": {
                            "configuration": "Default",
                            "document": "M3.0.3_20261016.SLDASM",
                        },
                        "message": (
                            "saved declaration does not bind the observed main document "
                            "and delivery configuration"
                        ),
                        "object": "assembly",
                        "schema_version": "solidworks-to-urdf.discovery-finding/v1",
                    },
                ],
                "hardware_id": "m3.0",
                "passed": False,
                "revision": None,
            },
            "work_dir": f"/home/andy/operator/runs/{run_id}/definition",
        }
        job = {
            "schema_version": JOB_SCHEMA,
            "pipeline_id": self.module.PIPELINE_ID,
            "request": {
                "main_assembly": "M3.0.3_20261016.SLDASM",
                "run_id": run_id,
                "upload_files": upload_files,
                "upload_sha256": digest(upload_files),
            },
            "run_id": run_id,
            "status": "failed",
            "error": "the derivation did not pass; its findings must be resolved before capture",
            "error_code": "definition_derivation_failed",
            "selection": {
                "state": "reading",
                "staged_files": upload_files,
                "upload_sha256": digest(upload_files),
            },
            "detail": detail,
            "completed_at": "2026-10-19T14:07:27.086715+00:00",
            "failure_diagnostics": {
                "path": "diagnostic.json",
                "sha256": "b303544fa0507fa984c0fe1e46d485cc6aad5e2bb4077137d2ae448eb9539d51",
            },
            "events": [
                {
                    "at": "2026-10-19T14:05:15.376225+00:00",
                    "stage": "discover",
                    "state": "running",
                },
                {
                    "at": "2026-10-19T14:07:27.079428+00:00",
                    "error": "the derivation did not pass; its findings must be resolved before capture",
                    "stage": "discover",
                    "state": "failed",
                },
            ],
        }
        request = {
            "run_id": run_id,
            "upload_files": upload_files,
            "upload_sha256": digest(upload_files),
            "main_assembly": "M3.0.3_20261016.SLDASM",
            "conn_id": "test",
            "platform_config_digest": config_digest,
        }
        original = AirflowFailException(
            "definition derivation failed: the derivation did not pass; "
            "its findings must be resolved before capture"
        )
        endpoint = SimpleNamespace(get_job=lambda value: job)
        with (
            patch.object(self.module, "_endpoint", return_value=endpoint),
            patch.object(self.module, "_platform_binding", return_value=(store, self.root)),
            patch.object(
                self.module,
                "_work_paths",
                return_value=flow.RunPaths(self.root, run_id),
            ),
            patch.object(self.module, "_poke", side_effect=original),
            self.assertRaises(AirflowFailException) as caught,
        ):
            self.module.dag.task_dict["discover"].python_callable(
                request,
                dag_run=SimpleNamespace(run_id=request_id),
                ti=SimpleNamespace(task_id="discover", map_index=-1),
            )

        self.assertIs(caught.exception.__cause__, original)
        self.assertEqual(caught.exception.code, "definition_derivation_failed")
        self.assertEqual(caught.exception.detail, detail)
        self.assertTrue(caught.exception.native_terminal_failure_converged)
        records = store.stage_records(run_id)
        self.assertEqual(records["freeze"]["state"], "completed")
        self.assertEqual(records["discover"]["state"], "failed")
        self.assertEqual(records["discover"]["receipt"]["error_code"], "definition_derivation_failed")
        self.assertEqual(records["discover"]["receipt"]["detail"], detail)
        self.assertEqual(
            [records[stage]["state"] for stage in ("capture", "generate", "verify", "publish")],
            ["blocked", "blocked", "blocked", "blocked"],
        )
        self.assertEqual(store.run_record(run_id)["state"], "failed")
        self._settle_terminal_discover_failure(world, store, request, request_id)
        settled = store.register_operation(
            run_id=run_id,
            stage="discover",
            kind="native",
            operation_key=operation_key,
            params_digest="b" * 64,
            dispatch=dispatch,
        )
        self.assertEqual(settled.state, "failed")

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
                context={
                    "dag_run": SimpleNamespace(run_id=f"portal-{run_id}"),
                    "ti": SimpleNamespace(task_id=stage, map_index=-1, xcom_push=lambda **kwargs: None),
                },
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

    def test_a_confirmed_publication_replays_its_registered_receipt(self) -> None:
        self._registered_parent()
        self._admit(self.CHILD, parent=self.PARENT, from_stage="verify")
        self._linked_stage(self.CHILD, "verify", source=self.PARENT, seeds=[], gates=[], subject="b" * 64)
        receipt = self._linked_stage(
            self.CHILD,
            "publish",
            source=self.PARENT,
            seeds=[],
            gates=[],
            subject="c" * 64,
            repository=self.root / "model-repository",
        )
        self.assertEqual(receipt["state"], "published")
        sealed = self.store.confirmed_stage(self.CHILD, "publish")
        self.assertIsNotNone(sealed)
        # A retry of the exact confirmed publication replays the registered receipt and
        # never re-sends the side effect.
        tree = self.module.flow.RunPaths(self.root, self.CHILD).stage_output("publish")
        with (
            patch.object(self.module, "_platform_binding", return_value=(self.store, self.root)),
            patch.object(self.module.flow, "run_stage", side_effect=AssertionError("publication re-sent")),
        ):
            replayed = self.module._portable_registered(
                self.CHILD,
                "publish",
                tree=tree,
                expected_subject="c" * 64,
                repository=self.root / "model-repository",
                base="main",
                source_run_id=self.PARENT,
            )
        self.assertEqual(replayed, sealed, "the replay returns the registered sealed receipt")
        self.assertEqual(replayed["submission"]["state"], sealed["submission"]["state"])
        self.assertEqual(self.store.confirmed_stage(self.CHILD, "publish"), sealed)

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
