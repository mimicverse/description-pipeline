"""Airflow ordering and the native/portable execution boundary (real DAG parser)."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.v1.test_airflow_dag import AIRFLOW_AVAILABLE
from description_pipeline.stages import CONTRACT, STAGE_IDS
from tests.v1.protocol_support import protocol_events
from description_pipeline.delivery import PIPELINE_ID

from .portal_read_support import RegisteredPlatform, stub_definition_rebuild

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"
SUBJECT = "a" * 64


class _RecordingTaskInstance:
    """Minimal task instance that captures the engineering_stages push in direct unit tests."""

    def __init__(self, *, task_id: str = "publish", map_index: int = -1) -> None:
        self.values: dict[str, dict] = {}
        self.task_id = task_id
        self.map_index = map_index

    def xcom_pull(self, *, task_ids, key):
        return self.values.get(key)

    def xcom_push(self, *, key, value):
        self.values[key] = value


def _failed_verify_receipt() -> dict:
    """The real shape of a verification rejection receipt: recorded checks, no fake passes."""
    events = protocol_events(stages=("freeze", "discover", "capture", "generate"), subject=SUBJECT)
    events = [*events, *protocol_events(stages=("verify",), subject=SUBJECT, failed_stage="verify")]
    return {
        "run_id": RUN_ID,
        "state": "failed",
        "passed": False,
        "subject_sha256": SUBJECT,
        "error": "URDF verification failed or required checks were not executed; see reports/quality.json",
        "events": events,
    }


def _passing_publish_receipt() -> dict:
    return {
        "run_id": RUN_ID,
        "state": "published",
        "passed": True,
        "subject_sha256": SUBJECT,
        "submission": {
            "passed": True,
            "state": "published",
            "subject_sha256": SUBJECT,
            "commit": "c" * 40,
            "base": "feature/robot",
            "repository_slug": "example/robot",
            "branch": "work/solidworks/robot/engineering",
            "url": "https://github.com/example/robot/pull/1",
        },
        "events": protocol_events(subject=SUBJECT),
    }


def _registered_delivery(
    platform: RegisteredPlatform, *, submission_state: str = "published", confirm_publish: bool = True
) -> str:
    """Register the six-stage records of one passing delivery over the platform store.

    The confirmation consumes exactly these records: the run row, the six stage
    receipts, the recorded check events and the sealed capture tree that binds
    hardware/revision.  The capture and publication trees are genuinely sealed
    through the production writer and confirmed through the store, so a
    confirmed-stage read-back re-verifies the same bytes production would.  The
    returned subject is the sealed delivery's own digest the events bind.
    """
    platform.admit(RUN_ID)
    platform.register_capture(RUN_ID)
    platform.conclude(RUN_ID, "succeeded")
    for stage in ("freeze", "discover", "generate"):
        platform.stage(RUN_ID, stage)
    subject = SUBJECT
    if confirm_publish:
        subject = platform.register_publish(RUN_ID, submission_state=submission_state)
    platform.stage(
        RUN_ID,
        "verify",
        receipt={
            "subject_sha256": subject,
            "handoff_sha256": "b" * 64,
            "quality": {"passed": True, "subject_sha256": subject},
        },
    )
    platform.events(RUN_ID, protocol_events(subject=subject))
    return subject


@unittest.skipUnless(AIRFLOW_AVAILABLE, "Airflow is not installed in this interpreter")
class SplitWorkflowTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.spec_from_file_location(
            "split_workflow_acceptance", ROOT / "deploy/airflow/dags/solidworks_to_urdf.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        return module

    def test_publication_depends_on_linux_verification_and_generation(self):
        from airflow.models import DagBag

        bag = DagBag(str(ROOT / "deploy/airflow/dags"))
        self.assertFalse(bag.import_errors, bag.import_errors)
        dag = bag.dags["solidworks_to_urdf"]
        for task_id, predecessor in (
            ("generate", "capture"),
            ("verify", "generate"),
            ("publish", "verify"),
        ):
            with self.subTest(stage=task_id):
                task = dag.get_task(task_id)
                self.assertIn(predecessor, task.upstream_task_ids)
                self.assertTrue(task.doc_md)
        self.assertIsNone(dag.schedule)

    def test_native_completion_only_unblocks_capture_transfer(self):
        module = self.module()
        from description_pipeline.io import digest

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
            "status": "native_complete",
            "events": protocol_events(stages=("freeze", "discover", "capture")),
            "result": {"native_complete": True, "passed": False},
        }
        with patch.object(module, "_endpoint", return_value=SimpleNamespace(get_job=lambda _: job)):
            self.assertTrue(module._poke(request, ti=None))
        from description_pipeline.stages import require_complete, stage_view
        from description_pipeline.io import PipelineError

        with self.assertRaises(PipelineError):
            require_complete(stage_view(job))

    def test_confirmation_consumes_bound_linux_results_for_every_publication_outcome(self):
        module = self.module()
        run_id = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"
        request = {
            "run_id": run_id,
            "package": "control",
            "handoff_sha256": "b" * 64,
            "conn_id": "test",
            "platform_config_digest": "c" * 64,
            "linux_owned": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            platform = RegisteredPlatform(Path(tmp) / "platform")
            subject = _registered_delivery(platform)
            # The run's bound routing rides the chain; hardware/revision keep the
            # registered capture identity as their authority.
            published = {
                "request": request,
                "hardware_id": "chain-value",
                "revision": "chain-value",
                "repository_slug": "example/robot",
                "repository_base": "feature/robot",
            }
            confirm = module._confirm_delivery
            with (
                patch.object(module, "_platform_binding", return_value=(platform.store, platform.root)),
                patch.object(module, "_endpoint", side_effect=AssertionError("Linux confirmation must not open CAD")),
            ):
                for state in ("published", "updated", "noop"):
                    with self.subTest(state=state):
                        receipt = _passing_publish_receipt()
                        receipt["handoff_sha256"] = "b" * 64
                        receipt["subject_sha256"] = subject
                        receipt["quality"] = {"passed": True, "subject_sha256": subject}
                        receipt["submission"] = {
                            **receipt["submission"],
                            "state": state,
                            "subject_sha256": subject,
                        }
                        platform.stage(run_id, "publish", receipt=receipt)
                        result = confirm(published)
                        self.assertEqual(result["pipeline_id"], PIPELINE_ID)
                        self.assertEqual(result["submission"]["state"], state)
                        # The registered capture identity outranks the chain claim.
                        self.assertEqual(result["handoff"]["hardware_id"], "robot")
                        self.assertEqual(result["handoff"]["revision"], "r1")
                receipt = _passing_publish_receipt()
                receipt["handoff_sha256"] = "b" * 64
                receipt["subject_sha256"] = subject
                receipt["quality"] = {"passed": True, "subject_sha256": "d" * 64}
                receipt["submission"] = {**receipt["submission"], "subject_sha256": subject}
                platform.stage(run_id, "publish", receipt=receipt)
                from description_pipeline.platform import flow as flow_module

                self.assertFalse(flow_module.registered_delivery_snapshot(platform.store, run_id)["result"]["passed"])
                from description_pipeline.orchestration.airflow_client import ResultNotPublishable

                with self.assertRaises(ResultNotPublishable):
                    confirm(published)
                # The registered run outcome is authoritative: a failed run never
                # confirms, however passing its recorded receipt is.
                from airflow.sdk.exceptions import AirflowFailException

                platform.conclude(run_id, "failed")
                with self.assertRaises(AirflowFailException):
                    confirm(published)

    def test_engineering_failure_fails_its_airflow_task_even_with_a_retained_subject(self):
        from airflow.sdk.exceptions import AirflowFailException

        module = self.module()
        failed = {"state": "failed", "passed": False, "subject_sha256": "a" * 64, "error": "quality gate failed"}
        for stage in ("generate", "verify", "publish"):
            with self.subTest(stage=stage), self.assertRaisesRegex(AirflowFailException, "quality gate failed"):
                module._portable_summary(failed, stage=stage)
        generated = module._portable_summary(
            {"state": "generated", "passed": False, "subject_sha256": "b" * 64}, stage="generate"
        )
        self.assertEqual(generated["state"], "generated")

    def test_portable_report_renders_recorded_checks_without_fabricating_qc(self):
        module = self.module()
        receipt = _failed_verify_receipt()
        ti = _RecordingTaskInstance()
        with self.assertLogs(module.log.name, level="INFO") as captured:
            stages = module._portable_report(receipt, stage="verify", context={"ti": ti})
            # Direct unit tests call the reporting helper without an Airflow context.
            module._portable_report(receipt, stage="verify")
        rows = {stage["id"]: stage for stage in stages["stages"]}
        verify = rows["verify"]
        definition = next(item for item in CONTRACT["stages"] if item["id"] == "verify")
        self.assertEqual(verify["state"], "failed")
        self.assertEqual(verify["error"], receipt["error"])
        self.assertTrue(all(item["state"] == "passed" for item in verify["input_qc"]))
        output_states = {item["id"]: item["state"] for item in verify["output_qc"]}
        self.assertEqual(output_states[definition["output_qc"][0]["id"]], "failed")
        self.assertIn("not_run", set(output_states.values()))
        self.assertNotIn("passed", set(output_states.values()))
        self.assertEqual(rows["publish"]["state"], "blocked")
        self.assertTrue(any("portable result=" in line and '"id": "verify"' in line for line in captured.output))
        self.assertTrue(any("portable stage=verify failure=" in line for line in captured.output))

        # The bounded XCom keeps the native and portable recorded states, without check details.
        pushed = ti.values["engineering_stages"]
        self.assertEqual([item["id"] for item in pushed["stages"]], list(STAGE_IDS))
        states = {item["id"]: item["state"] for item in pushed["stages"]}
        for native_stage in ("freeze", "discover", "capture"):
            self.assertEqual(states[native_stage], "completed")
        self.assertEqual(states["verify"], "failed")
        for stage in pushed["stages"]:
            for key in ("input_qc", "output_qc"):
                for item in stage[key]:
                    self.assertEqual(set(item), {"id", "state"})

    def test_portable_report_pushes_the_passing_summary_with_external_review_scope(self):
        module = self.module()
        ti = _RecordingTaskInstance()
        with self.assertLogs(module.log.name, level="INFO") as captured:
            stages = module._portable_report(_passing_publish_receipt(), stage="publish", context={"ti": ti})
        rows = {stage["id"]: stage for stage in stages["stages"]}
        self.assertEqual(rows["publish"]["state"], "completed")
        self.assertEqual(rows["verify"]["state"], "completed")
        # After verification the view reports the external review scope only: no pending claim.
        confirmations = [item for stage in stages["stages"] for item in stage["confirmations"]]
        self.assertTrue(confirmations)
        self.assertEqual({item["state"] for item in confirmations}, {"external_review"})
        self.assertFalse(any("failure=" in line for line in captured.output))
        pushed = ti.values["engineering_stages"]
        self.assertEqual(pushed["subject_sha256"], SUBJECT)
        self.assertEqual(next(item for item in pushed["stages"] if item["id"] == "publish")["state"], "completed")

    def test_verify_task_reports_the_failed_receipt_before_it_raises(self):
        from airflow.sdk.exceptions import AirflowFailException

        module = self.module()
        ti = _RecordingTaskInstance()
        generated = {
            "run_id": RUN_ID,
            "request": {"run_id": RUN_ID, "conn_id": "test", "platform_config_digest": "c" * 64},
            "generate": {"state": "generated", "subject_sha256": SUBJECT},
            "from_stage": None,
        }
        with (
            patch.object(module.flow, "run_stage", return_value=_failed_verify_receipt()),
            patch.object(
                module,
                "config_from_airflow_connection",
                return_value=SimpleNamespace(store_root=Path("/nonexistent-work")),
            ),
            patch.object(
                module,
                "_platform_binding",
                return_value=(
                    SimpleNamespace(
                        append_event=lambda *args, **kwargs: None,
                        stage_records=lambda _run_id: {},
                        confirm_stage=lambda **_payload: None,
                    ),
                    Path("/nonexistent"),
                ),
            ),
        ):
            verify = module.dag.get_task("verify").python_callable
            with self.assertRaises(AirflowFailException):
                verify(generated, ti=ti)
        pushed = ti.values["engineering_stages"]
        self.assertEqual(next(item for item in pushed["stages"] if item["id"] == "verify")["state"], "failed")

    def test_publish_task_returns_and_reports_the_passing_receipt(self):
        module = self.module()
        ti = _RecordingTaskInstance()
        verified = {
            "run_id": RUN_ID,
            "request": {
                "run_id": RUN_ID,
                "package": "control",
                "handoff_sha256": "b" * 64,
                "conn_id": "test",
                "linux_owned": True,
                "platform_config_digest": "c" * 64,
            },
            "verify": {"state": "verified", "passed": True, "subject_sha256": SUBJECT},
            "repository_slug": "example/robot",
            "repository_base": "feature/robot",
            "from_stage": None,
        }
        config = SimpleNamespace(
            repositories={"example/robot": Path("/tmp/model-repository")},
            store_root=Path("/nonexistent-work"),
            config_digest="c" * 64,
        )
        with tempfile.TemporaryDirectory() as tmp:
            platform = RegisteredPlatform(Path(tmp) / "platform")
            _registered_delivery(platform)
            with (
                stub_definition_rebuild(),
                patch.object(module.flow, "run_stage", side_effect=AssertionError("publication re-sent")),
                patch.object(module, "_platform_binding", return_value=(platform.store, platform.root)),
                patch.object(module, "config_from_airflow_connection", return_value=config),
            ):
                registered = platform.store.confirmed_stage(RUN_ID, "publish")
                before = dict(platform.world.operations)
                publish = module.dag.get_task("publish").python_callable
                result = publish(verified, ti=ti)
        self.assertIsNotNone(registered)
        # The confirmed publication is replayed: the registered sealed receipt is the
        # result, the stage never re-runs and nothing new is written.  The task's
        # stage report belongs to the original attempt; the replay returns the
        # delivery summary from the registered receipt alone.
        self.assertEqual(result["pipeline_id"], PIPELINE_ID)
        self.assertEqual(result["submission"]["state"], registered["submission"]["state"])
        self.assertEqual(result["submission"]["url"], "https://github.com/example/robot/pull/1")
        self.assertEqual(platform.world.operations, before, "the replay writes no new operation")

    def test_publish_on_a_terminal_run_without_a_registration_is_refused(self):
        from description_pipeline.platform.store import PlatformStoreError

        module = self.module()
        ti = _RecordingTaskInstance()
        verified = {
            "run_id": RUN_ID,
            "request": {
                "run_id": RUN_ID,
                "package": "control",
                "handoff_sha256": "b" * 64,
                "conn_id": "test",
                "linux_owned": True,
                "platform_config_digest": "c" * 64,
            },
            "verify": {"state": "verified", "passed": True, "subject_sha256": SUBJECT},
            "repository_slug": "example/robot",
            "repository_base": "feature/robot",
            "from_stage": None,
        }
        config = SimpleNamespace(
            repositories={"example/robot": Path("/tmp/model-repository")},
            store_root=Path("/nonexistent-work"),
            config_digest="c" * 64,
        )
        with tempfile.TemporaryDirectory() as tmp:
            platform = RegisteredPlatform(Path(tmp) / "platform")
            _registered_delivery(platform, confirm_publish=False)
            before = dict(platform.world.operations)
            with (
                patch.object(module.flow, "run_stage", side_effect=AssertionError("publication re-sent")),
                patch.object(module, "_platform_binding", return_value=(platform.store, platform.root)),
                patch.object(module, "config_from_airflow_connection", return_value=config),
                self.assertRaisesRegex(PlatformStoreError, "terminal"),
            ):
                module.dag.get_task("publish").python_callable(
                    verified, ti=ti, dag_run=SimpleNamespace(run_id=RUN_ID)
                )
            # Nothing was written: the terminal run refuses even the new intent.
            self.assertEqual(platform.world.operations, before)


if __name__ == "__main__":
    unittest.main()
