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
from tests.v1.protocol_support import protocol_events
from description_pipeline.io import write_json
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.orchestration.linux_store import LinuxStore

ROOT = Path(__file__).resolve().parents[2]


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


if __name__ == "__main__":
    unittest.main()
