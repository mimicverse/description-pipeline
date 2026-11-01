"""Actual Airflow parsing and operator execution with private native resources.

The composition factory is injected at its explicit seam. These checks do not
claim production scheduler, session, portal or Publish integration.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from description_pipeline.dynamics_assessment.contracts import dispatch_conf
from tests.v1.test_dynamics_assessment_contracts import AssessmentCase
from tests.v1.test_dynamics_assessment_native import Dispatch, build_native_case
from tests.v1.test_dynamics_assessment_postgres import PostgresCase

try:
    from airflow.dag_processing.dagbag import DagBag
    from airflow.sdk.exceptions import AirflowFailException
except ModuleNotFoundError as error:
    if error.name != "airflow":
        raise
    raise unittest.SkipTest("The installed Airflow runtime is required") from error

from description_pipeline.dynamics_assessment import airflow as assessment_airflow  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def load_dag():
    bag = DagBag(
        dag_folder=ROOT / "deploy/airflow/dags/upper_body_dynamics.py",
        known_pools={"default_pool", "dynamics_assessment"},
    )
    assert not bag.import_errors
    assert set(bag.dags) == {"upper_body_dynamics"}
    return bag.dags["upper_body_dynamics"]


class AirflowTests(AssessmentCase):
    def test_actual_dag_parse_is_paused_and_has_no_required_stage_dependencies(self):
        dag = load_dag()
        assert dag.is_paused_upon_creation is True
        assert dag.schedule is None
        assert dag.max_active_runs == 2
        assert dag.task_ids == ["assess"]
        task = dag.get_task("assess")
        assert task.pool == "dynamics_assessment" and task.retries == 0
        assert not task.upstream_task_ids and (not task.downstream_task_ids)

    def test_actual_operator_rejects_foreign_task_context_before_composition(self):

        def forbidden():
            unittest.TestCase().fail("A foreign task context must not compose the executor")

        self.enterContext(mock.patch.object(assessment_airflow, "assessment_executor_from_environment", forbidden))
        task = load_dag().get_task("assess")
        with self.assertRaisesRegex(AirflowFailException, "registered auxiliary DagRun"):
            task.execute(
                context={
                    "dag_run": SimpleNamespace(
                        dag_id=task.dag_id,
                        run_id="unregistered-run",
                        conf={"request_id": "a" * 64, "attempt_id": "b" * 64, "request_sha256": "c" * 64},
                    ),
                    "ti": SimpleNamespace(task_id="assess", run_id="unregistered-run", map_index=-1),
                }
            )


class AirflowDatabaseTests(PostgresCase):
    def test_actual_operator_runs_native_teleop_and_seals_private_result(self):
        service, executor, access, bundle, *_ = build_native_case(self.database)
        public = service.create(access, "source-dag-run", "actual-airflow-operator", bundle, Dispatch())
        attempt = public["attempt"]
        request = service.store.request(public["request_id"])
        conf = dispatch_conf(public["request_id"], attempt["attempt_id"], request)
        self.enterContext(
            mock.patch.object(assessment_airflow, "assessment_executor_from_environment", lambda: executor)
        )
        task = load_dag().get_task("assess")
        context = {
            "dag_run": SimpleNamespace(dag_id=task.dag_id, run_id=attempt["dag_run_id"], conf=conf),
            "ti": SimpleNamespace(task_id=task.task_id, run_id=attempt["dag_run_id"], map_index=-1),
        }
        result = task.execute(context=context)
        assert result["state"] == "completed"
        assert service.store.attempt(attempt["attempt_id"])["resources_settled"]
        assert service.read_result(access, attempt["attempt_id"], "verify.stdout.json")[0]
