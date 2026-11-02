"""Static assertion that the Airflow DAG is only a StageApplication adapter."""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
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
        self.assertIn("StageTaskOperator(", source)
        self.assertIn("max_active_runs=8", source)
        self.assertIn("max_active_tasks=8", source)
        self.assertNotIn("from airflow.sdk import Param, dag, task", source)
        self.assertNotIn("max_active_runs=1", source)

    @unittest.skipUnless(importlib.util.find_spec("airflow"), "Airflow is not installed")
    def test_real_dag_and_pool_preserve_deferred_native_capacity(self):
        script = textwrap.dedent("""
            import importlib.util
            import os
            from contextlib import contextmanager
            from pathlib import Path
            from sqlalchemy import create_engine
            from sqlalchemy.orm import Session
            from airflow.models.pool import Pool
            from unittest.mock import patch
            from description_pipeline.stages import STAGE_IDS, WINDOWS_STAGES

            def load(name, path):
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module

            dag = load("pool_test_dag", Path("deploy/airflow/dags/solidworks_to_urdf.py")).solidworks_to_urdf_dag
            assert tuple(dag.task_ids) == STAGE_IDS
            for stage in STAGE_IDS:
                task = dag.get_task(stage)
                assert task.pool == ("windows_native" if stage in WINDOWS_STAGES else "default_pool")
                assert task.pool_slots == 1
            for previous, following in zip(STAGE_IDS, STAGE_IDS[1:]):
                assert dag.get_task(following).upstream_task_ids == {previous}

            engine = create_engine("sqlite://")
            Pool.__table__.create(engine)
            @contextmanager
            def session_factory():
                with Session(engine) as session:
                    yield session
            module = load("pool_test_installer", Path("deploy/airflow/scripts/ensure_pool.py"))
            for existing in (False, True):
                if existing:
                    with Session(engine) as session:
                        pool = session.query(Pool).filter(Pool.pool == "windows_native").one()
                        pool.slots = 7
                        pool.include_deferred = False
                        session.commit()
                module.ensure_pool("windows_native", 1, "native", session_factory=session_factory, pool_class=Pool)
                with Session(engine) as session:
                    pool = session.query(Pool).filter(Pool.pool == "windows_native").one()
                    assert pool.slots == 1 and pool.include_deferred is True
            from description_pipeline.dynamics_assessment.contracts import POOL_ID
            optional = load("assessment_pool_dag", Path("deploy/airflow/dags/upper_body_dynamics.py"))
            assert optional.upper_body_dynamics_dag.get_task("assess").pool == POOL_ID
            assert len(module.configured_pools({})) == 1
            pools = module.configured_pools({"ASSESSMENT_CONFIG_JSON": '{"enabled":true,"capacity":3}'})
            assert [name for name, _, _ in pools] == ["windows_native", POOL_ID]
            for name, slots, description in pools:
                module.ensure_pool(name, slots, description, session_factory=session_factory, pool_class=Pool)
            with Session(engine) as session:
                module.require_pool(session, Pool, POOL_ID, 3)
                try:
                    module.require_pool(session, Pool, POOL_ID, 2)
                except SystemExit:
                    pass
                else:
                    raise AssertionError("A health check accepted another resource capacity")
                assert session.query(Pool).filter(Pool.pool == POOL_ID).one().slots == 3
            with (
                patch.dict(os.environ, {"ASSESSMENT_CONFIG_JSON": '{"enabled":true,"capacity":2}'}),
                patch("airflow.utils.session.create_session", session_factory),
            ):
                assert module.main([]) == 0
                assert module.main(["--check"]) == 0
                with Session(engine) as session:
                    assert session.query(Pool).filter(Pool.pool == POOL_ID).one().slots == 2
        """)
        with tempfile.TemporaryDirectory() as home:
            env = {key: value for key, value in os.environ.items() if not key.startswith("AIRFLOW")}
            env.update(AIRFLOW_HOME=home, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == "__main__":
    unittest.main()
