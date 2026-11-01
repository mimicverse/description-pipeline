"""One optional Airflow task, outside the engineering StageTaskOperator graph."""

from __future__ import annotations

from airflow.models import BaseOperator
from airflow.sdk.exceptions import AirflowFailException

from .composition import assessment_executor_from_environment
from .contracts import DAG_ID, auxiliary_dag_run_id, object_record


class DynamicsAssessmentOperator(BaseOperator):
    template_fields = ()
    ui_color = "#e2eaf3"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._executor = None
        self._attempt_id = None

    def execute(self, context):
        dag_run = context.get("dag_run")
        task = context.get("ti")
        try:
            conf = object_record(
                getattr(dag_run, "conf", None), {"request_id", "attempt_id", "request_sha256"}, "Assessment DagRun conf"
            )
            if (
                self.dag_id != DAG_ID
                or getattr(dag_run, "dag_id", None) != DAG_ID
                or getattr(dag_run, "run_id", None) != auxiliary_dag_run_id(conf["attempt_id"])
                or getattr(task, "run_id", None) != dag_run.run_id
                or getattr(task, "map_index", -1) != -1
                or getattr(task, "task_id", None) != self.task_id
            ):
                raise ValueError("Assessment task does not belong to the registered auxiliary DagRun")
            self._executor = assessment_executor_from_environment()
            if self._executor is None:
                raise ValueError("Optional dynamics assessment is disabled")
            self._attempt_id = conf["attempt_id"]
            return self._executor.execute(self._attempt_id, conf)
        except Exception as error:
            raise AirflowFailException(str(error)) from error

    def on_kill(self):
        if self._executor is not None and self._attempt_id is not None:
            self._executor.cancel_owned(self._attempt_id)
