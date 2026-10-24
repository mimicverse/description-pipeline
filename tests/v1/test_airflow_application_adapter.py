"""Static assertion that the Airflow DAG is only a StageApplication adapter."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DAG = ROOT / "deploy/airflow/dags/solidworks_to_urdf.py"


class AirflowApplicationAdapterTests(unittest.TestCase):
    def test_dag_has_no_business_tasks_of_its_own(self):
        tree = ast.parse(DAG.read_text(encoding="utf-8"))
        function = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "solidworks_to_urdf"
            ),
            None,
        )
        self.assertIsNotNone(function)
        tasks = [
            node.name
            for node in function.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self.assertEqual(tasks, [])

    def test_legacy_business_lifecycle_is_absent(self):
        source = DAG.read_text(encoding="utf-8")
        for forbidden in (
            "_portable_registered",
            "_seal_and_confirm",
            "_register_native_stages",
            "_provide_definition",
            "_converge_failed_stage",
            "flow.run_stage",
            "solidworks.generate",
            "solidworks.verify",
            "solidworks.publish",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_dag_uses_the_class_operator_with_bounded_cross_run_concurrency(self):
        source = DAG.read_text(encoding="utf-8")
        self.assertIn("StageTaskOperator(task_id=stage, stage=stage", source)
        self.assertIn("max_active_runs=8", source)
        self.assertIn("max_active_tasks=8", source)
        self.assertNotIn("from airflow.sdk import Param, dag, task", source)
        self.assertNotIn("max_active_runs=1", source)


if __name__ == "__main__":
    unittest.main()
