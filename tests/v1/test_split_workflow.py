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
from description_pipeline.io import write_json
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.orchestration.linux_store import LinuxStore

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"
SUBJECT = "a" * 64


class _RecordingTaskInstance:
    """Minimal task instance that captures the engineering_stages push in direct unit tests."""

    def __init__(self) -> None:
        self.values: dict[str, dict] = {}

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
            "branch": "work/solidworks/robot",
            "url": "https://github.com/example/robot/pull/1",
        },
        "events": protocol_events(subject=SUBJECT),
    }


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
            ("run_generate", "fetch_capture"),
            ("run_verify", "run_generate"),
            ("run_publish", "run_verify"),
            ("confirm_job", "run_publish"),
        ):
            with self.subTest(stage=task_id):
                task = dag.get_task(task_id)
                self.assertIn(predecessor, task.upstream_task_ids)
                self.assertTrue(task.doc_md)
        self.assertIsNone(dag.schedule)

    def test_native_completion_only_unblocks_capture_transfer(self):
        module = self.module()
        request = {"run_id": "protocol-control", "package": "control", "handoff_sha256": "b" * 64, "conn_id": "test"}
        job = {
            "request": {key: request[key] for key in ("run_id", "package", "handoff_sha256")},
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
        subject = "a" * 64
        request = {
            "run_id": run_id, "package": "control", "handoff_sha256": "b" * 64,
            "conn_id": "test", "linux_owned": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LinuxStore(Path(tmp))
            store.update_meta(
                run_id, state="published", subject_sha256=subject,
                repository_slug="example/robot", repository_base="feature/robot",
            )
            capture = store.capture_dir(run_id)
            write_json(capture / "input/robot.yaml", {"hardware_id": "robot"})
            write_json(capture / "input/cad-revision.json", {"revision": "r1"})
            output = store.output_dir(run_id)
            (output / "input").mkdir(parents=True)
            write_json(output / "reports/quality.json", {"passed": True, "subject_sha256": subject})
            store.append_events(run_id, protocol_events(subject=subject))
            confirm = module.dag.get_task("confirm_job").python_callable
            with (
                patch.object(module, "_linux_store", return_value=store),
                patch.object(module, "_endpoint", side_effect=AssertionError("Linux confirmation must not open CAD")),
            ):
                for state in ("published", "updated", "noop"):
                    with self.subTest(state=state):
                        write_json(output / "reports/pr.json", {
                            "state": state, "passed": True, "subject_sha256": subject,
                            "url": "https://github.com/example/robot/pull/1", "commit": "c" * 40,
                            "base": "feature/robot", "repository_slug": "example/robot",
                            "branch": "work/solidworks/robot",
                        })
                        result = confirm({"request": request})
                        self.assertEqual(result["pipeline_id"], PIPELINE_ID)
                        self.assertEqual(result["submission"]["state"], state)
                write_json(output / "reports/quality.json", {"passed": True, "subject_sha256": "d" * 64})
                self.assertFalse(store.merged_job(run_id)["result"]["passed"])
                from description_pipeline.orchestration.airflow_client import ResultNotPublishable

                with self.assertRaises(ResultNotPublishable):
                    confirm({"request": request})

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
            "request": {"run_id": RUN_ID, "conn_id": "test"},
            "generate": {"state": "generated", "subject_sha256": SUBJECT},
            "from_stage": None,
        }
        with (
            patch.object(module, "_linux_store", return_value=object()),
            patch.object(module, "run_portable_stage", return_value=_failed_verify_receipt()),
        ):
            run_verify = module.dag.get_task("run_verify").python_callable
            with self.assertRaises(AirflowFailException):
                run_verify(generated, ti=ti)
        pushed = ti.values["engineering_stages"]
        self.assertEqual(next(item for item in pushed["stages"] if item["id"] == "verify")["state"], "failed")

    def test_publish_task_returns_and_reports_the_passing_receipt(self):
        module = self.module()
        ti = _RecordingTaskInstance()
        verified = {
            "run_id": RUN_ID,
            "request": {"run_id": RUN_ID, "conn_id": "test"},
            "verify": {"state": "verified", "passed": True, "subject_sha256": SUBJECT},
            "repository_slug": "example/robot",
            "repository_base": "feature/robot",
            "from_stage": None,
        }
        config = SimpleNamespace(repositories={"example/robot": Path("/tmp/model-repository")})
        with (
            patch.object(module, "_linux_store", return_value=object()),
            patch.object(module, "run_portable_stage", return_value=_passing_publish_receipt()),
            patch.object(module, "config_from_airflow_connection", return_value=config),
        ):
            run_publish = module.dag.get_task("run_publish").python_callable
            result = run_publish(verified, ti=ti)
        self.assertEqual(result["publish"]["state"], "published")
        self.assertTrue(result["publish"]["passed"])
        self.assertEqual(result["publish"]["submission"]["url"], "https://github.com/example/robot/pull/1")
        pushed = ti.values["engineering_stages"]
        self.assertEqual(next(item for item in pushed["stages"] if item["id"] == "publish")["state"], "completed")


if __name__ == "__main__":
    unittest.main()
