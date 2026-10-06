"""Submit one configured CAD package to the Windows SolidWorks execution endpoint."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta

from airflow.providers.standard.sensors.python import PythonSensor
from airflow.sdk import Param, dag, task
from airflow.sdk.exceptions import AirflowFailException

from description_pipeline.orchestration.airflow_client import (
    WindowsEndpoint,
    check_result,
    config_from_airflow_connection,
    validate_package,
    validate_revision_sha,
)

DAG_ID = "solidworks_to_urdf"
SENSOR_MODE = os.environ.get("SOLIDWORKS_SENSOR_MODE", "reschedule")
POLL_INTERVAL = float(os.environ.get("SOLIDWORKS_POLL_INTERVAL", "10"))
POLL_TIMEOUT = float(os.environ.get("SOLIDWORKS_TIMEOUT", "3600"))
log = logging.getLogger(__name__)


def _endpoint(conn_id: str) -> WindowsEndpoint:
    return WindowsEndpoint(config_from_airflow_connection(conn_id))


def _run_uuid(context) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{DAG_ID}:{context['dag_run'].run_id}"))


def _poke(request: dict) -> bool:
    job = _endpoint(request["conn_id"]).get_job(request["run_id"])
    _same_request(job, request)
    latest = job["events"][-1] if job["events"] else {}
    log.info(
        "native run_id=%s status=%s stage=%s stage_state=%s",
        request["run_id"],
        job["status"],
        latest.get("stage"),
        latest.get("state"),
    )
    if job["status"] == "failed":
        result = job.get("result") or {}
        log.error("native diagnostics=%s events=%s", result.get("diagnostic_path"), job["events"])
        raise AirflowFailException(f"SolidWorks job {request['run_id']} failed: {job.get('error')}")
    return job["status"] == "passed"


def _same_request(job: dict, request: dict) -> None:
    expected = {key: request[key] for key in ("run_id", "package", "revision_sha256", "target")}
    if job.get("request") != expected:
        raise AirflowFailException("Endpoint job differs from the requested mechanical handoff")


@dag(
    dag_id=DAG_ID,
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["solidworks", "urdf", "windows"],
    default_args={"retries": 2, "retry_delay": timedelta(seconds=15)},
    params={
        "package": Param("", type="string", description="POSIX relative package path"),
        "revision_sha256": Param("", type="string", description="sealed cad-revision.json digest"),
        "target": Param("", type="string", description="configured repository alias"),
        "repository_slug": Param("", type="string", description="expected origin owner/repo"),
        "base": Param("", type="string", description="expected base branch feature/<hardware>"),
        "conn_id": Param("solidworks_windows", type="string", description="Airflow connection"),
    },
)
def solidworks_to_urdf():
    @task
    def validate_request(**context) -> dict:
        params = context["params"]
        package = validate_package(params["package"])
        revision_sha256 = validate_revision_sha(params["revision_sha256"])
        target = str(params["target"]).strip()
        if not target:
            raise AirflowFailException("target repository alias is required")
        repository_slug = str(params["repository_slug"]).strip()
        base = str(params["base"]).strip()
        if not repository_slug or "/" not in repository_slug or not base.startswith("feature/"):
            raise AirflowFailException("repository_slug and feature/<hardware> base are required")
        run_id = _run_uuid(context)
        log.info("queue run_id=%s package=%s target=%s endpoint_conn=%s", run_id, package, target, params["conn_id"])
        return {
            "run_id": run_id,
            "package": package,
            "revision_sha256": revision_sha256,
            "target": target,
            "repository_slug": repository_slug,
            "base": base,
            "conn_id": params["conn_id"],
        }

    @task
    def start_job(request: dict) -> dict:
        job = _endpoint(request["conn_id"]).start_job(
            run_id=request["run_id"],
            package=request["package"],
            revision_sha256=request["revision_sha256"],
            target=request["target"],
        )
        log.info("started run_id=%s status=%s", job["run_id"], job["status"])
        return {**request, "status": job["status"]}

    @task
    def confirm_job(request: dict) -> dict:
        job = _endpoint(request["conn_id"]).get_job(request["run_id"])
        _same_request(job, request)
        if job["status"] != "passed":
            raise AirflowFailException(f"job {request['run_id']} is {job['status']}, not passed")
        if job.get("repository_slug") != request["repository_slug"]:
            raise AirflowFailException("job repository_slug differs from the requested origin")
        if job.get("repository_base") != request["base"]:
            raise AirflowFailException("job repository_base differs from the requested base")
        result = check_result(
            job.get("result"), expected_slug=request["repository_slug"], expected_base=request["base"]
        )
        log.info(
            "published run_id=%s quality=%s submission=%s",
            request["run_id"],
            result.get("quality"),
            result.get("submission"),
        )
        return {
            "run_id": request["run_id"],
            "pipeline_id": result["pipeline_id"],
            "events": job["events"],
            "quality": result["quality"],
            "submission": result["submission"],
        }

    request = validate_request()
    started = start_job(request)
    wait_for_job = PythonSensor(
        task_id="wait_for_job",
        python_callable=_poke,
        op_kwargs={"request": started},
        mode=SENSOR_MODE,
        poke_interval=POLL_INTERVAL,
        timeout=POLL_TIMEOUT,
    )
    confirm = confirm_job(started)
    wait_for_job >> confirm


dag = solidworks_to_urdf()
