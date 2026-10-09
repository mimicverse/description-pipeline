"""Submit one engineering folder to the Windows SolidWorks execution endpoint.

The DAG carries one operator value, ``handoff_path``. A linked attempt (``parent_dag_run_id``
and ``resume_from``) derives the retained package and digest from the parent native job itself.
Hardware, revision and repository routing resolve inside the serialized Windows job after CAD
discovery and are confirmed here before publication.
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
from description_pipeline.stages import (
    STAGE_IDS,
    compact_view,
    contract_markdown,
    require_complete,
    stage_log,
    stage_view,
)
from description_pipeline.io import digest

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


def _linked_conf(context) -> dict | None:
    """The linked-attempt binding of one trigger, or None for an ordinary upload."""
    conf = getattr(context["dag_run"], "conf", None)
    conf = conf if isinstance(conf, dict) else {}
    parent = conf.get("parent_dag_run_id")
    stage = conf.get("resume_from")
    if parent is None and stage is None:
        return None
    if not (isinstance(parent, str) and parent.strip() and isinstance(stage, str) and stage in STAGE_IDS):
        raise AirflowFailException("A linked attempt requires parent_dag_run_id and a canonical resume_from stage")
    return {"parent_run": native_run_id(parent.strip()), "from_stage": stage}


def _resolution(request: dict) -> HandoffResolution:
    return HandoffResolution(
        package=request["package"],
        handoff_sha256=request["handoff_sha256"],
    )


def _poke(request: dict, **context) -> bool:
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
    stages = stage_view(job)
    ti = context.get("ti")
    progress = digest(compact_view(stages))
    if ti is None or ti.xcom_pull(task_ids="wait_for_job", key="engineering_progress") != progress:
        for stage in stages["stages"]:
            log.info(
                "engineering stage=%s state=%s input_qc=%s output_qc=%s",
                stage["id"],
                stage["state"],
                [(item["id"], item["state"]) for item in stage["input_qc"]],
                [(item["id"], item["state"]) for item in stage["output_qc"]],
            )
        if ti is not None:
            ti.xcom_push(key="engineering_progress", value=progress)
    if job["status"] in {"passed", "failed"}:
        for row in stage_log(stages):
            log.info("engineering result=%s", row)
        if ti is not None:
            ti.xcom_push(key="engineering_stages", value=compact_view(stages))
        else:
            log.warning("Terminal engineering summary has no task instance; XCom was not stored")
    if job["status"] == "failed":
        result = job.get("result") or {}
        log.error("native diagnostics=%s events=%s", result.get("diagnostic_path"), job["events"])
        raise AirflowFailException(f"SolidWorks job {request['run_id']} failed: {job.get('error')}")
    return job["status"] == "passed"


def _same_request(job: dict, request: dict) -> None:
    expected = {key: request[key] for key in ("run_id", "package", "handoff_sha256")}
    if request.get("resume") is not None:
        expected["resume"] = request["resume"]
    if job.get("request") != expected:
        raise AirflowFailException("Endpoint job differs from the requested mechanical handoff")


@dag(
    dag_id=DAG_ID,
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["solidworks", "urdf", "windows"],
    default_args={"retries": 2, "retry_delay": timedelta(seconds=15)},
    doc_md=(
        "# SolidWorks to URDF\n\nSix engineering stages execute in one serialized Windows job. "
        "The graph below transports that job; polling is not an engineering stage.\n\n"
        + contract_markdown()
        + "\n\nRun results: `wait_for_job` logs every stage and terminal QC details, including failures. "
        "Its `engineering_stages` XCom contains the terminal summary. The operator page shows "
        "inputs, checks, outputs and evidence per stage; `reports/stages.json` retains the detailed receipt. "
        "Engineering confirmations remain pending until approved in the bound review records."
    ),
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
    @task(doc_md="Collect the admitted native folder and bind its file inventory for the queued engineering job.")
    def resolve_handoff(**context) -> dict:
        linked = _linked_conf(context)
        if linked is not None:
            # The parent native job is the only authority for the retained upload;
            # client-supplied package or digest values are never trusted.
            parent = _endpoint(CONN_ID).get_job(linked["parent_run"])
            retained = parent.get("request") if isinstance(parent.get("request"), dict) else {}
            package, digest_value = retained.get("package"), retained.get("handoff_sha256")
            if not isinstance(package, str) or not package or not isinstance(digest_value, str) or not digest_value:
                raise AirflowFailException(f"Parent job {linked['parent_run']} has no retained mechanical handoff")
            log.info(
                "linked attempt parent_run=%s from_stage=%s package=%s endpoint_conn=%s",
                linked["parent_run"],
                linked["from_stage"],
                package,
                CONN_ID,
            )
            return {
                "run_id": _run_uuid(context),
                "package": package,
                "handoff_sha256": digest_value,
                "conn_id": CONN_ID,
                "resume": linked,
            }
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

    @task(doc_md="Submit the same UUID and frozen handoff to the serial Windows queue; retries never replay CAD.")
    def start_job(request: dict) -> dict:
        job = _endpoint(request["conn_id"]).start_job(
            run_id=request["run_id"],
            resolution=_resolution(request),
            resume=request.get("resume"),
        )
        log.info("started run_id=%s status=%s", job["run_id"], job["status"])
        return {**request, "status": job["status"]}

    @task(doc_md="Confirm all six engineering stages and the verified candidate PR; return the bound stage summary.")
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
            "repository_slug": routing["repository_slug"],
            "base": routing["repository_base"],
        }
        result = check_result(
            job.get("result"),
            expected_slug=routing["repository_slug"],
            expected_base=routing["repository_base"],
        )
        stages = stage_view(job)
        require_complete(stages)
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
            "stages": compact_view(stages),
            "quality": {key: result["quality"].get(key) for key in ("passed", "subject_sha256")},
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
        doc_md=(
            "Transport polling only. Engineering stage/QC results are in these logs and the engineering_stages XCom."
        ),
    )
    confirm = confirm_job(started)
    wait_for_job >> confirm


dag = solidworks_to_urdf()
