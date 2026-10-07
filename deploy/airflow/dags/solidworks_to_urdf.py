"""Submit one configured CAD package to the Windows SolidWorks execution endpoint."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from airflow.providers.standard.sensors.python import PythonSensor
from airflow.sdk import Param, dag, task
from airflow.sdk.exceptions import AirflowFailException

from description_pipeline.orchestration.airflow_client import (
    WindowsEndpoint,
    check_result,
    config_from_airflow_connection,
    native_run_id,
)

DAG_ID = "solidworks_to_urdf"
CONN_ID = os.environ.get("SOLIDWORKS_ENDPOINT_CONN_ID", "solidworks_windows")
SENSOR_MODE = os.environ.get("SOLIDWORKS_SENSOR_MODE", "reschedule")
POLL_INTERVAL = float(os.environ.get("SOLIDWORKS_POLL_INTERVAL", "10"))
POLL_TIMEOUT = float(os.environ.get("SOLIDWORKS_TIMEOUT", "3600"))
log = logging.getLogger(__name__)


def _endpoint(conn_id: str) -> WindowsEndpoint:
    return WindowsEndpoint(config_from_airflow_connection(conn_id))


def _run_uuid(context) -> str:
    return native_run_id(context["dag_run"].run_id)


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
    keys = ["run_id", "package", "revision_sha256", "target"]
    if "handoff_sha256" in request:
        keys.append("handoff_sha256")
    expected = {key: request[key] for key in keys}
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
        "handoff_path": Param(
            "",
            type="string",
            title="Handoff folder path",
            description=(
                "Folder the Windows endpoint inspects (an absolute Linux folder is archived and "
                "imported before the run starts)"
            ),
        ),
    },
)
def solidworks_to_urdf():
    @task
    def resolve_handoff(**context) -> dict:
        handoff_path = str(context["params"]["handoff_path"]).strip()
        if not handoff_path:
            raise AirflowFailException("handoff_path is required")
        resolved = _endpoint(CONN_ID).resolve_handoff(handoff_path)
        run_id = _run_uuid(context)
        log.info(
            "resolved handoff_path=%s package=%s handoff_sha256=%s hardware_id=%s target=%s endpoint_conn=%s",
            handoff_path,
            resolved.package,
            resolved.handoff_sha256,
            resolved.hardware_id,
            resolved.target,
            CONN_ID,
        )
        return {
            "run_id": run_id,
            "package": resolved.package,
            "revision_sha256": resolved.revision_sha256,
            "handoff_sha256": resolved.handoff_sha256,
            "target": resolved.target,
            "repository_slug": resolved.repository_slug,
            "base": resolved.base,
            "hardware_id": resolved.hardware_id,
            "revision": resolved.revision,
            "conn_id": CONN_ID,
        }

    @task
    def start_job(request: dict) -> dict:
        job = _endpoint(request["conn_id"]).start_job(
            run_id=request["run_id"],
            package=request["package"],
            revision_sha256=request["revision_sha256"],
            target=request["target"],
            handoff_sha256=request.get("handoff_sha256"),
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
            "handoff": {
                "package": request["package"],
                "revision_sha256": request["revision_sha256"],
                "handoff_sha256": request["handoff_sha256"],
                "hardware_id": request["hardware_id"],
                "revision": request["revision"],
                "target": request["target"],
                "repository_slug": request["repository_slug"],
                "base": request["base"],
            },
            "events": job["events"],
            "quality": result["quality"],
            "submission": result["submission"],
        }

    request = resolve_handoff()
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
