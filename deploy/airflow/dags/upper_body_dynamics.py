"""Default-paused auxiliary assessment DAG with no engineering-stage dependency."""

from datetime import UTC, datetime

from airflow.sdk import DAG

from description_pipeline.dynamics_assessment.airflow import DynamicsAssessmentOperator
from description_pipeline.dynamics_assessment.contracts import DAG_ID, POOL_ID

with DAG(
    dag_id=DAG_ID,
    schedule=None,
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    is_paused_upon_creation=True,
    max_active_runs=2,
    max_active_tasks=2,
    tags=["robot-model-studio", "optional", "dynamics"],
) as upper_body_dynamics_dag:
    DynamicsAssessmentOperator(task_id="assess", retries=0, pool=POOL_ID)
