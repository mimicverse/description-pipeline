"""Airflow DAG over the canonical class-based stage adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

from airflow.sdk import Param, dag

from description_pipeline.orchestration.airflow_stage_operator import (
    StageTaskOperator,
)
from description_pipeline.stages import STAGE_IDS, WINDOWS_STAGES

DAG_ID = "solidworks_to_urdf"


@dag(
    dag_id=DAG_ID,
    schedule=None,
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=8,
    max_active_tasks=8,
    params={"run_id": Param(None, type="string")},
    tags=["description-pipeline", "six-stage"],
)
def solidworks_to_urdf():
    stages = [
        StageTaskOperator(
            task_id=stage, stage=stage, retries=0,
            pool="windows_native" if stage in WINDOWS_STAGES else "default_pool",
        )
        for stage in STAGE_IDS
    ]
    for upstream, downstream in pairwise(stages):
        upstream >> downstream


solidworks_to_urdf_dag = solidworks_to_urdf()
