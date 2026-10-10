"""Airflow ordering and the native/portable execution boundary (real DAG parser)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.v1.test_airflow_dag import AIRFLOW_AVAILABLE
from tests.v1.protocol_support import protocol_events

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


if __name__ == "__main__":
    unittest.main()
