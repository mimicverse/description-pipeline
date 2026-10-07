"""Submit one engineering folder to the Windows SolidWorks execution endpoint.

The DAG carries one operator value, ``handoff_path``. It sends only the resolved native package
and its digest; hardware, revision and repository routing resolve inside the serialized Windows
job after CAD discovery and are confirmed here before publication.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from airflow.providers.standard.sensors.python import PythonSensor
from airflow.sdk import Param, dag, task
from airflow.sdk.exceptions import AirflowFailException

from description_pipeline.orchestration.airflow_client import (
    HandoffResolution,
    WindowsEndpoint,
    check_result,
    config_from_airflow_connection,
    native_run_id,
    resolved_routing,
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


def _resolution(request: dict) -> HandoffResolution:
    return HandoffResolution(
        package=request["package"],
        handoff_sha256=request["handoff_sha256"],
    )


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
    expected = {key: request[key] for key in ("run_id", "package", "handoff_sha256")}
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
            title="Engineering folder path",
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
            "resolved handoff_path=%s package=%s handoff_sha256=%s endpoint_conn=%s",
            handoff_path,
            resolved.package,
            resolved.handoff_sha256,
            CONN_ID,
        )
        return {
            "run_id": run_id,
            "package": resolved.package,
            "handoff_sha256": resolved.handoff_sha256,
            "conn_id": CONN_ID,
        }

    @task
    def start_job(request: dict) -> dict:
        job = _endpoint(request["conn_id"]).start_job(
            run_id=request["run_id"],
            resolution=_resolution(request),
        )
        log.info("started run_id=%s status=%s", job["run_id"], job["status"])
        return {**request, "status": job["status"]}

    @task
    def confirm_job(request: dict) -> dict:
        job = _endpoint(request["conn_id"]).get_job(request["run_id"])
        _same_request(job, request)
        if job["status"] != "passed":
            raise AirflowFailException(f"job {request['run_id']} is {job['status']}, not passed")
        routing = resolved_routing(job)
        handoff = {
            "package": request["package"],
            "handoff_sha256": request["handoff_sha256"],
            "hardware_id": routing["hardware_id"],
            "revision": routing["revision"],
            "target": routing["target"],
            "repository_slug": routing["repository_slug"],
            "base": routing["repository_base"],
        }
        result = check_result(
            job.get("result"),
            expected_slug=routing["repository_slug"],
            expected_base=routing["repository_base"],
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
            "handoff": handoff,
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
