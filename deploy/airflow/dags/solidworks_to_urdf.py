"""Airflow adapter for the canonical six-stage StageApplication service."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from airflow.sdk import Param, dag, task
from airflow.sdk.exceptions import AirflowFailException

from description_pipeline.pipeline.application import (
    ApplicationError,
    StageApplication,
    application_from_environment,
)
from description_pipeline.pipeline.contracts import FailureEnvelope, StageResult
from description_pipeline.pipeline.contracts import StageId

DAG_ID = "solidworks_to_urdf"


def _run_id(context) -> str:
    conf = getattr(context.get("dag_run"), "conf", None)
    conf = conf if isinstance(conf, dict) else {}
    run_id = conf.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise AirflowFailException("dag_run.conf.run_id is required")
    return run_id


def _application() -> StageApplication:
    try:
        return application_from_environment()
    except ApplicationError as error:
        raise AirflowFailException(f"{error.code}: {error}") from error


def _run_stage(stage: StageId, context) -> dict:
    application = _application()
    try:
        result = application.run_stage(_run_id(context), stage)
    except ApplicationError as error:
        raise AirflowFailException(
            json.dumps(
                {"code": error.code, "message": str(error), "detail": error.detail},
                ensure_ascii=False,
                sort_keys=True,
            )
        ) from error
    if isinstance(result, FailureEnvelope):
        raise AirflowFailException(
            json.dumps(result.to_record(), ensure_ascii=False, sort_keys=True)
        )
    if not isinstance(result, StageResult) or result.outcome != "completed":
        raise AirflowFailException("The stage did not return one completed StageResult")
    # This summary is emitted only for observability; the next task re-reads the
    # exact command/result facts from the platform and never consumes this XCom.
    return {
        "stage": result.stage,
        "outcome": result.outcome,
        "result_digest": result.digest,
        "output_artifact": result.output_artifact.to_record(),
        "actual_executor_sha256": result.actual_executor_sha256,
    }


@dag(
    dag_id=DAG_ID,
    schedule=None,
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    params={"run_id": Param(None, type="string")},
    tags=["description-pipeline", "six-stage"],
)
def solidworks_to_urdf():
    @task(retries=0)
    def freeze(**context):
        return _run_stage("freeze", context)

    @task(retries=0)
    def discover(**context):
        return _run_stage("discover", context)

    @task(retries=0)
    def capture(**context):
        return _run_stage("capture", context)

    @task(retries=0)
    def generate(**context):
        return _run_stage("generate", context)

    @task(retries=0)
    def verify(**context):
        return _run_stage("verify", context)

    @task(retries=0)
    def publish(**context):
        return _run_stage("publish", context)

    freeze() >> discover() >> capture() >> generate() >> verify() >> publish()


solidworks_to_urdf_dag = solidworks_to_urdf()
