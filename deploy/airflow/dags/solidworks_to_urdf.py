"""Submit one engineering folder to the Windows SolidWorks execution endpoint.

The DAG carries ``handoff_path`` and an optional ``main_assembly`` (the explicit delivered
assembly). A fresh run admits the sealed local intake, stages the full indexed native folder to
the endpoint, which resolves and seals the dependency closure before the native host reads the
raw discovery record; the prepared package is derived here on Linux over the sealed selection and returned as
the capture request, and a local derivation refusal aborts the waiting endpoint with a
structured failure. A linked attempt (``parent_dag_run_id`` and ``resume_from``) reuses the
parent native job's retained source selection - files, index digest and main assembly - and
refuses a supplied selection that differs. Hardware, revision and repository routing are
confirmed here before publication.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from airflow.sdk import Param, dag, task
from airflow.sdk.exceptions import AirflowFailException

from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    EndpointError,
    EndpointProtocolError,
    WindowsEndpoint,
    capture_archive_metadata,
    check_result,
    config_from_airflow_connection,
    drive_staging,
    native_run_id,
    resolved_routing,
    validate_main_assembly,
)
from description_pipeline.orchestration.linux_runner import fetch_capture as fetch_native_capture
from description_pipeline.orchestration.linux_runner import run_portable_stage
from description_pipeline.orchestration.linux_store import LinuxStore
from description_pipeline.stages import (
    STAGE_IDS,
    compact_view,
    contract_markdown,
    require_complete,
    stage_log,
    stage_view,
)
from description_pipeline.io import PipelineError, confined, digest, inventory, read_data
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.sources.solidworks.selection import validate_file_index

DAG_ID = "solidworks_to_urdf"
CONN_ID = os.environ.get("SOLIDWORKS_ENDPOINT_CONN_ID", "solidworks_windows")
#: Stages the Linux portable host owns; a linked attempt from one of these skips Windows.
PORTABLE_STAGES = ("generate", "verify", "publish")
SENSOR_MODE = os.environ.get("SOLIDWORKS_SENSOR_MODE", "reschedule")
POLL_INTERVAL = float(os.environ.get("SOLIDWORKS_POLL_INTERVAL", "10"))
POLL_TIMEOUT = float(os.environ.get("SOLIDWORKS_TIMEOUT", "3600"))
log = logging.getLogger(__name__)


def _endpoint(conn_id: str) -> WindowsEndpoint:
    return WindowsEndpoint(config_from_airflow_connection(conn_id))


def _linux_store(conn_id: str) -> LinuxStore:
    config = config_from_airflow_connection(conn_id)
    if config.store_root is None:
        raise AirflowFailException(
            "pipeline.store_root / PIPELINE_STORE_ROOT is not configured; the Linux portable half cannot run"
        )
    return LinuxStore(config.store_root)


def _portable_summary(receipt: dict, *, stage: str) -> dict:
    expected = {"generate": {"generated"}, "verify": {"verified"}, "publish": {"published", "updated", "noop"}}
    if receipt.get("state") not in expected[stage] or (stage != "generate" and receipt.get("passed") is not True):
        raise AirflowFailException(receipt.get("error") or f"{stage} did not complete its required checks")
    return {
        "state": receipt.get("state"),
        "passed": receipt.get("passed"),
        "subject_sha256": receipt.get("subject_sha256"),
        "error": receipt.get("error"),
        "submission": receipt.get("submission"),
    }


def _portable_report(receipt: dict, *, stage: str, context: dict | None = None) -> dict:
    """Log one Linux stage's recorded checks and retain its bounded stage summary.

    The summary renders only the receipt's own events, so a failing stage reports its actual
    boundary results and is pushed to ``engineering_stages`` before the domain failure is
    raised.  Unrecorded checks stay ``not_run``; nothing is invented for the log or the XCom.
    """
    stages = stage_view(
        {
            "run_id": receipt.get("run_id"),
            "events": [event for event in receipt.get("events") or [] if isinstance(event, dict)],
            "result": receipt,
        }
    )
    row = next((item for item in stages["stages"] if item["id"] == stage), None)
    if row is None:  # pragma: no cover - the installed contract always names all six stages
        return stages
    log.info(
        "portable stage=%s state=%s checks=%s/%s subject=%s",
        stage,
        row["state"],
        row["checks_passed"],
        row["checks_total"],
        stages.get("subject_sha256"),
    )
    for line in stage_log({"stages": [row]}):
        log.info("portable result=%s", line)
    if row["state"] == "failed":
        # Report an unexpected failure only from what the receipt actually carries.
        failure = {
            key: receipt[key]
            for key in ("error", "error_code", "detail", "diagnostic_path")
            if receipt.get(key) is not None
        }
        log.error("portable stage=%s failure=%s", stage, failure)
    ti = (context or {}).get("ti")
    if ti is not None:
        ti.xcom_push(key="engineering_stages", value=compact_view(stages))
    return stages


def _parent_subject(store: LinuxStore, parent_run: str, stage: str) -> str:
    """The recorded subject of a parent portable checkpoint a linked rerun reuses."""
    path = store.checkpoint_receipt(parent_run, stage)
    if not path.is_file():
        raise AirflowFailException(f"parent attempt {parent_run} has no {stage} checkpoint to reuse")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    subject = receipt.get("subject_sha256")
    if not isinstance(subject, str) or not subject:
        raise AirflowFailException(f"parent attempt {parent_run} {stage} checkpoint carries no subject")
    return subject


def _root_capture(store: LinuxStore, run_id: str):
    """The admitted capture directory of an attempt, following its source lineage."""
    try:
        return store.checkpoint_dir(run_id, "capture")
    except PipelineError:
        return None


def _root_identity(store: LinuxStore, run_id: str) -> tuple[str | None, str | None]:
    """Hardware and revision of the admitted delivery, read from its JSON revision record."""
    capture = _root_capture(store, run_id)
    if capture is None:
        return None, None
    path = capture / "input/cad-revision.json"
    if not path.is_file():
        return None, None
    payload = json.loads(path.read_text(encoding="utf-8"))
    hardware = payload.get("hardware_id")
    revision = payload.get("revision")
    return (
        hardware if isinstance(hardware, str) and hardware else None,
        revision if isinstance(revision, str) and revision else None,
    )


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


def _param_selection(context) -> str | None:
    """The explicit main assembly of this trigger, validated, or None when absent."""

    value = context["params"].get("main_assembly")
    if value is None or value == "":
        return None
    return validate_main_assembly(value)


def _selection_main(upload_files: dict[str, str], requested: str | None) -> str:
    """The explicit selection, or the sole ``.SLDASM`` of the sealed intake - never a scan."""

    if requested is not None:
        if requested not in upload_files:
            raise AirflowFailException(f"main assembly {requested!r} is not part of the sealed intake")
        return requested
    assemblies = sorted(name for name in upload_files if name.lower().endswith(".sldasm"))
    if len(assemblies) != 1:
        raise AirflowFailException(
            f"Select a main assembly explicitly; the sealed intake holds {len(assemblies)} .SLDASM files"
        )
    return assemblies[0]


def _linked_selection(parent_value, supplied) -> str | None:
    """A linked attempt's effective selection: inherit the parent, or an explicitly equal value.

    The parent job remains the single authority for its checkpoints; a supplied different
    selection would describe a run the retained discovery cannot serve, so it is refused
    rather than replaced by the parent's value (which would let the stored conf lie).
    """

    parent_selection = parent_value if isinstance(parent_value, str) and parent_value else None
    if supplied is None or supplied == "":
        return parent_selection
    supplied = validate_main_assembly(supplied)
    if supplied != parent_selection:
        raise AirflowFailException(
            "A linked attempt must keep the parent's main assembly selection "
            f"({parent_selection!r} != {supplied!r}); restore it or start a new run"
        )
    return supplied


def _linked_source(retained: object, supplied) -> tuple[dict[str, str], str, str]:
    """The retained source selection a linked native rerun reuses exactly.

    The endpoint compares the rerun's source fields against the parent's own request, so the
    client first re-validates the parent's echoed selection and refuses anything inconsistent.
    """

    request = retained if isinstance(retained, dict) else {}
    upload_files = request.get("upload_files")
    upload_sha256 = request.get("upload_sha256")
    if not isinstance(upload_files, dict) or not upload_files or not isinstance(upload_sha256, str):
        raise AirflowFailException("Parent native job carries no retained selection payload to resume")
    try:
        validated = validate_file_index(upload_files)
    except PipelineError as error:
        raise AirflowFailException(f"Parent native job's retained selection payload is invalid: {error}") from error
    if digest(validated) != upload_sha256:
        raise AirflowFailException("Parent native job's retained selection payload is inconsistent")
    main = _linked_selection(request.get("main_assembly"), supplied)
    if main is None or main not in validated:
        raise AirflowFailException("Parent native job's retained selection names no admissible main assembly")
    return validated, upload_sha256, main


def _materialize_selection(intake: Path, files: dict[str, str], target: Path) -> Path:
    """Copy exactly the sealed selection into one fresh run-owned directory."""

    intake, target = Path(intake), Path(target)
    if target.exists():
        if inventory(target) == files:
            return target
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name, checksum in files.items():
        source = Path(confined(intake, name))
        destination = Path(confined(target, name, exists=False))
        destination.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        with source.open("rb") as reader, destination.open("wb") as writer:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                writer.write(chunk)
        if hasher.hexdigest() != checksum:
            raise AirflowFailException(f"Selected source {name!r} no longer matches the sealed selection")
    if inventory(target) != files:
        raise AirflowFailException("The selected source copy differs from the sealed selection")
    return target


def _definition_settings(config: EndpointConfig, main_assembly: str):
    """The pinned Linux derivation settings of one definition hand-off."""

    from description_pipeline.sources.solidworks.discovery import DiscoverySettings

    configuration = config.discovery
    registry = configuration.get("frozen_names_file")
    names: dict = {}
    if registry:
        try:
            names = read_data(Path(str(registry)))
        except (OSError, PipelineError) as error:
            raise AirflowFailException(f"Frozen-name registry is not readable: {error}") from error
    if not isinstance(names, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in names.items()
    ):
        raise AirflowFailException("Frozen-name registry must map native identities to interface names")
    roots = configuration.get("record_roots", ())
    if not isinstance(roots, (list, tuple)):
        raise AirflowFailException("Controlled-record roots must be a list of paths")
    return DiscoverySettings(
        record_roots=tuple(Path(value) for value in roots),
        frozen_names=names,
        main_assembly=main_assembly,
    )


def _provide_definition(endpoint: WindowsEndpoint, request: dict) -> None:
    """Derive the prepared package from the transferred raw record and submit it.

    The derivation reads only the sealed selection copied out of the intake folder,
    never the full upload; a local refusal aborts the waiting endpoint so it cannot
    hang, while transport failures leave the job awaiting a retry.
    """

    from description_pipeline.orchestration.discovery_transfer import (
        admit_raw_record_bundle,
        derive_from_raw_bundle,
        seal_capture_request,
    )

    run_id = request["run_id"]
    snapshot = endpoint.definition_snapshot(run_id, upload_files=request["upload_files"])
    store = _linux_store(request["conn_id"])
    work = store.run_dir(run_id) / "definition"
    bundle = work / "raw-record.zip"
    derived = work / "derived"
    capture_request = work / "capture-request.zip"
    work.mkdir(parents=True, exist_ok=True)
    endpoint.stream_native_record(run_id, snapshot, bundle)
    prepared = None
    try:
        intake = request.get("intake_dir")
        if not isinstance(intake, str) or not intake:
            raise AirflowFailException(
                "Definition derivation requires the sealed intake folder of the fresh or linked source"
            )
        manifest = snapshot.selection.manifest
        main_assembly = manifest.get("main_assembly")
        if not isinstance(main_assembly, str) or not main_assembly:
            raise AirflowFailException("The sealed selection names no main assembly to derive")
        selected = manifest.get("files")
        if not isinstance(selected, dict) or not selected:
            raise AirflowFailException("The sealed selection lists no files to derive")
        settings = _definition_settings(endpoint.config, main_assembly)
        frozen = _materialize_selection(Path(intake), selected, work / "selected")
        admission = admit_raw_record_bundle(
            bundle,
            expected_run_id=run_id,
            expected_selection=manifest,
            expected_native_tool=snapshot.native_tool,
            expected_archive_sha256=snapshot.record_sha256,
        )
        if admission.manifest != snapshot.record_manifest:
            raise AirflowFailException("The admitted raw record differs from the endpoint declaration")
        if derived.exists():
            shutil.rmtree(derived)
        capture_request.unlink(missing_ok=True)
        prepared = derive_from_raw_bundle(
            bundle,
            frozen,
            derived,
            expected_run_id=run_id,
            expected_selection=manifest,
            expected_native_tool=snapshot.native_tool,
            expected_archive_sha256=snapshot.record_sha256,
            settings=settings,
        )
        if prepared.passed is not True:
            raise PipelineError("the derivation did not pass; its findings must be resolved before capture")
        seal_capture_request(
            prepared,
            capture_request,
            record=admission.record,
            selection=manifest,
            native_tool=snapshot.native_tool,
        )
    except (AirflowFailException, PipelineError, OSError) as error:
        error_code = getattr(error, "code", None) or "definition_derivation_failed"
        detail = dict(getattr(error, "detail", None) or {})
        diagnostic = getattr(error, "diagnostic_path", None)
        if diagnostic:
            detail.setdefault("diagnostic_path", str(diagnostic))
        if prepared is not None:
            detail.setdefault(
                "discovery",
                {
                    "passed": prepared.passed,
                    "findings": [dict(item) for item in prepared.findings],
                    "hardware_id": prepared.hardware_id,
                    "revision": prepared.revision,
                    "discovery_sha256": prepared.discovery_sha256,
                },
            )
        detail.setdefault("work_dir", str(work))
        try:
            endpoint.abort_job(
                run_id,
                error=str(error)[:2000] or type(error).__name__,
                error_code=str(error_code),
                detail=detail,
            )
        except (EndpointError, EndpointProtocolError) as abort_error:
            log.warning("definition abort unavailable run_id=%s error=%s", run_id, abort_error)
        raise AirflowFailException(f"definition derivation failed: {error}") from error
    endpoint.post_definition_archive(run_id, capture_request, prepared_sha256=prepared.prepared_sha256)
    log.info("definition run_id=%s prepared_sha256=%s", run_id, prepared.prepared_sha256)


def _poke(request: dict, **context) -> bool:
    if request.get("linux_owned"):
        # A linked portable attempt has no Windows job to poll.
        return True
    endpoint = _endpoint(request["conn_id"])
    job = endpoint.get_job(request["run_id"])
    _same_request(job, request)
    if request.get("upload_files") and job.get("status") == "selecting":
        intake = request.get("intake_dir")
        if not isinstance(intake, str) or not intake:
            raise AirflowFailException("Folder staging requires the sealed intake folder of a fresh source")
        try:
            outcome = drive_staging(
                endpoint,
                run_id=request["run_id"],
                upload_files=request["upload_files"],
                main_assembly=request["main_assembly"],
                intake_dir=Path(intake),
                batch_dir=Path(tempfile.gettempdir()) / f"staging-{request['run_id']}",
            )
        except (EndpointProtocolError, PipelineError) as error:
            raise AirflowFailException(f"folder staging failed: {error}") from error
        log.info(
            "staging run_id=%s action=%s state=%s sealed=%s",
            request["run_id"],
            outcome.get("action"),
            outcome.get("state"),
            outcome.get("sealed"),
        )
        return False
    if job.get("status") == "awaiting_definition":
        _provide_definition(endpoint, request)
        return False
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
    if job["status"] in {"passed", "failed", "native_complete"}:
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
    return job["status"] in {"passed", "native_complete"}


def _same_request(job: dict, request: dict) -> None:
    expected = {key: request[key] for key in ("run_id", "upload_files", "upload_sha256", "main_assembly")}
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
        "# SolidWorks to URDF\n\nLinux admits the sealed engineering folder and stages the assembly-derived "
        "dependency closure to the Windows endpoint; the native host reads the raw discovery record and, "
        "after Linux derives the prepared package from the sealed selection, captures the native evidence. "
        "Linux then generates the URDF, independently verifies it and publishes the PR. The graph "
        "coordinates these six engineering stages; transport and polling are orchestration tasks.\n\n"
        + contract_markdown()
        + "\n\nRun results: `wait_for_job` logs native stage checks; `run_generate`, `run_verify` and "
        "`run_publish` log Linux stage results. Their `engineering_stages` XComs retain stage summaries, "
        "including failures. The operator page shows "
        "inputs, checks, outputs and evidence per stage; `reports/stages.json` retains the detailed receipt. "
        "Engineering review scope stays with the bound review records; the platform reports it without "
        "claiming approvals are pending or complete."
    ),
    params={
        "handoff_path": Param(
            "",
            type="string",
            title="Engineering folder path",
            description=(
                "Sealed engineering folder on this Linux host whose dependency closure is staged to "
                "the Windows endpoint (an absolute folder inside the configured handoff roots)"
            ),
        ),
        "main_assembly": Param(
            "",
            type="string",
            title="Delivered main assembly",
            description=(
                "Optional explicit delivered assembly: a .SLDASM relative path inside the "
                "engineering folder (top folder excluded). Authoritative for entry selection "
                "when provided."
            ),
        ),
    },
)
def solidworks_to_urdf():
    @task(doc_md="Collect the admitted native folder and bind its file inventory for the queued engineering job.")
    def resolve_handoff(**context) -> dict:
        linked = _linked_conf(context)
        if linked is not None:
            if linked["from_stage"] in PORTABLE_STAGES:
                # A portable-linked rerun resumes from the parent's Linux attempt and never
                # contacts Windows: the parent store already holds the admitted capture.
                store = _linux_store(CONN_ID)
                parent_meta = store.meta(linked["parent_run"])
                parent_capture = _root_capture(store, linked["parent_run"])
                if not isinstance(parent_meta, dict) or not parent_meta.get("handoff_sha256") or parent_capture is None:
                    raise AirflowFailException(
                        f"Linked parent {linked['parent_run']} has no admitted Linux capture to resume"
                    )
                request = {
                    "run_id": _run_uuid(context),
                    "package": str(parent_capture),
                    "handoff_sha256": str(parent_meta["handoff_sha256"]),
                    "conn_id": CONN_ID,
                    "resume": linked,
                    "linux_owned": True,
                }
                selection = _linked_selection(parent_meta.get("main_assembly"), context["params"].get("main_assembly"))
                if selection is not None:
                    request["main_assembly"] = selection
                return request
            # The parent native job is the only authority for the retained source
            # selection; the rerun reuses its exact files, digest and main assembly.
            parent = _endpoint(CONN_ID).get_job(linked["parent_run"])
            retained = parent.get("request") if isinstance(parent.get("request"), dict) else {}
            upload_files, upload_sha256, main = _linked_source(retained, context["params"].get("main_assembly"))
            log.info(
                "linked attempt parent_run=%s from_stage=%s files=%d upload_sha256=%s main_assembly=%s conn=%s",
                linked["parent_run"],
                linked["from_stage"],
                len(upload_files),
                upload_sha256,
                main,
                CONN_ID,
            )
            request = {
                "run_id": _run_uuid(context),
                "upload_files": upload_files,
                "upload_sha256": upload_sha256,
                "main_assembly": main,
                "resume": linked,
                "conn_id": CONN_ID,
            }
            if linked["from_stage"] != "capture":
                # A freeze or discover rerun still serves the definition leg from the
                # original sealed intake; the local folder must match it exactly.
                handoff_path = str(context["params"].get("handoff_path") or "").strip()
                if not handoff_path:
                    raise AirflowFailException(
                        "A linked freeze or discover rerun needs handoff_path with the original sealed intake"
                    )
                intake = _endpoint(CONN_ID).authorized_intake_dir(handoff_path)
                if validate_file_index(package_inventory(intake)) != upload_files:
                    raise AirflowFailException("The linked intake no longer matches the parent's retained selection")
                request["intake_dir"] = str(intake)
            return request
        handoff_path = str(context["params"]["handoff_path"]).strip()
        if not handoff_path:
            raise AirflowFailException("handoff_path is required")
        selection = _param_selection(context)
        intake = _endpoint(CONN_ID).authorized_intake_dir(handoff_path)
        upload_files = validate_file_index(package_inventory(intake))
        main = _selection_main(upload_files, selection)
        run_id = _run_uuid(context)
        log.info(
            "selection intake=%s files=%d upload_sha256=%s main_assembly=%s endpoint_conn=%s",
            intake,
            len(upload_files),
            digest(upload_files),
            main,
            CONN_ID,
        )
        return {
            "run_id": run_id,
            "upload_files": upload_files,
            "upload_sha256": digest(upload_files),
            "main_assembly": main,
            "intake_dir": str(intake),
            "conn_id": CONN_ID,
        }

    @task(doc_md="Submit the same UUID and frozen handoff to the serial Windows queue; retries never replay CAD.")
    def start_job(request: dict) -> dict:
        if request.get("linux_owned"):
            log.info(
                "linked attempt run_id=%s from_stage=%s is linux-owned; no Windows job",
                request["run_id"],
                (request.get("resume") or {}).get("from_stage"),
            )
            return {**request, "status": "linux_owned"}
        job = _endpoint(request["conn_id"]).start_staging_job(
            run_id=request["run_id"],
            upload_files=request["upload_files"],
            main_assembly=request["main_assembly"],
            resume=request.get("resume"),
        )
        log.info("started run_id=%s status=%s", job["run_id"], job["status"])
        return {**request, "status": job["status"]}

    @task(doc_md="Import the sealed native capture into the Linux attempt store and bind routing.")
    def fetch_capture(request: dict) -> dict:
        store = _linux_store(request["conn_id"])
        run_id = request["run_id"]
        resume = request.get("resume") or {}
        if request.get("linux_owned"):
            parent_run = resume["parent_run"]
            parent_meta = store.meta(parent_run) or {}
            hardware, revision = _root_identity(store, parent_run)
            binding = {
                "run_id": run_id,
                "source_run_id": parent_run,
                "from_stage": resume.get("from_stage"),
                "handoff_sha256": parent_meta.get("handoff_sha256"),
                "main_assembly": parent_meta.get("main_assembly"),
                "repository_slug": parent_meta.get("repository_slug"),
                "repository_base": parent_meta.get("repository_base"),
                "hardware_id": hardware,
                "revision": revision,
                "state": "linked",
            }
        else:
            job = _endpoint(request["conn_id"]).get_job(run_id)
            _same_request(job, request)
            if job.get("status") != "native_complete":
                raise AirflowFailException(f"native job {run_id} is {job.get('status')}, not native_complete")
            capture_archive_metadata(job)
            binding = fetch_native_capture(store, _endpoint(request["conn_id"]), run_id, job)
            binding["repository_slug"] = job.get("repository_slug")
            binding["repository_base"] = job.get("repository_base")
            binding["hardware_id"], binding["revision"] = _root_identity(store, run_id)
            binding["source_run_id"] = None
            binding["from_stage"] = None
        log.info(
            "linux capture ready run_id=%s source=%s state=%s",
            run_id,
            binding.get("source_run_id"),
            binding.get("state"),
        )
        return {"request": request, **binding}

    @task(doc_md="Generate the model on Linux; this checkpoint is intentionally unverified.")
    def run_generate(binding: dict, **context) -> dict:
        if binding.get("from_stage") in {"verify", "publish"}:
            # The linked attempt resumes past generation: reuse the parent checkpoint.
            store = _linux_store(binding["request"]["conn_id"])
            subject = _parent_subject(store, binding["source_run_id"], "generate")
            return {**binding, "generate": {"state": "reused", "passed": True, "subject_sha256": subject}}
        store = _linux_store(binding["request"]["conn_id"])
        receipt = run_portable_stage(store, binding["run_id"], "generate", source_run_id=binding.get("source_run_id"))
        _portable_report(receipt, stage="generate", context=context)
        return {**binding, "generate": _portable_summary(receipt, stage="generate")}

    @task(doc_md="Independent verification on Linux, including the MuJoCo consumer.")
    def run_verify(generated: dict, **context) -> dict:
        if generated.get("from_stage") == "publish":
            # The linked attempt resumes past verification: reuse the parent checkpoint.
            store = _linux_store(generated["request"]["conn_id"])
            subject = _parent_subject(store, generated["source_run_id"], "verify")
            return {**generated, "verify": {"state": "reused", "passed": True, "subject_sha256": subject}}
        subject = (generated.get("generate") or {}).get("subject_sha256")
        if not subject:
            raise AirflowFailException("generate produced no subject to verify")
        store = _linux_store(generated["request"]["conn_id"])
        receipt = run_portable_stage(
            store,
            generated["run_id"],
            "verify",
            expected_subject=subject,
            source_run_id=generated.get("source_run_id"),
        )
        _portable_report(receipt, stage="verify", context=context)
        return {**generated, "verify": _portable_summary(receipt, stage="verify")}

    @task(doc_md="Publish the verified delivery from Linux; no repository work happens on Windows.")
    def run_publish(verified: dict, **context) -> dict:
        subject = (verified.get("verify") or {}).get("subject_sha256")
        if not subject:
            raise AirflowFailException("verify produced no subject to publish")
        slug, base = verified.get("repository_slug"), verified.get("repository_base")
        repository = config_from_airflow_connection(CONN_ID).repositories.get(str(slug or ""))
        if repository is None:
            raise AirflowFailException(f"no configured Linux checkout for model repository {slug!r}")
        store = _linux_store(verified["request"]["conn_id"])
        receipt = run_portable_stage(
            store,
            verified["run_id"],
            "publish",
            expected_subject=subject,
            repository=repository,
            base=base,
            source_run_id=verified.get("source_run_id"),
        )
        _portable_report(receipt, stage="publish", context=context)
        return {**verified, "publish": _portable_summary(receipt, stage="publish")}

    @task(doc_md="Confirm the six-stage receipt and the verified candidate PR from the Linux store.")
    def confirm_job(published: dict) -> dict:
        request = published["request"]
        run_id = request["run_id"]
        native = None
        if not request.get("linux_owned"):
            native = _endpoint(request["conn_id"]).get_job(run_id)
            _same_request(native, request)
            if native["status"] == "failed":
                raise AirflowFailException(f"native job {run_id} failed: {native.get('error')}")
        store = _linux_store(request["conn_id"])
        snapshot = store.merged_job(run_id, native)
        for key in ("hardware_id", "revision", "repository_slug", "repository_base"):
            if not isinstance(snapshot.get(key), str) or not snapshot.get(key):
                value = published.get(key)
                if isinstance(value, str) and value:
                    snapshot[key] = value
        view = stage_view(snapshot)
        require_complete(view)
        routing = resolved_routing(snapshot)
        if request.get("linux_owned"):
            source = {"package": request["package"], "handoff_sha256": request["handoff_sha256"]}
        else:
            selected = native.get("handoff_sha256")
            if not isinstance(selected, str) or not selected:
                raise AirflowFailException(f"native job {run_id} carries no sealed handoff digest")
            source = {"upload_sha256": request.get("upload_sha256"), "handoff_sha256": selected}
        handoff = {
            **source,
            "hardware_id": routing["hardware_id"],
            "revision": routing["revision"],
            "repository_slug": routing["repository_slug"],
            "base": routing["repository_base"],
        }
        result = check_result(
            snapshot.get("result"),
            expected_slug=routing["repository_slug"],
            expected_base=routing["repository_base"],
        )
        log.info(
            "published run_id=%s quality=%s submission=%s",
            run_id,
            result.get("quality"),
            result.get("submission"),
        )
        return {
            "run_id": run_id,
            "pipeline_id": result["pipeline_id"],
            "handoff": handoff,
            "stages": compact_view(view),
            "quality": {key: result["quality"].get(key) for key in ("passed", "subject_sha256")},
            "submission": result["submission"],
        }

    request = resolve_handoff()
    started = start_job(request)

    @task.sensor(
        mode=SENSOR_MODE,
        poke_interval=POLL_INTERVAL,
        timeout=POLL_TIMEOUT,
        doc_md=(
            "Transport polling and the definition exchange (folder staging, raw-record derivation, "
            "capture-request submission). Engineering stage/QC results are in these logs and the "
            "engineering_stages XCom."
        ),
    )
    def wait_for_job(request: dict, **context) -> bool:
        return _poke(request, **context)

    waited = wait_for_job(started)
    # The poll follows the submission even where op_kwargs XCom resolution is not
    # inferred as a dependency edge.
    fetched = fetch_capture(started)
    generated = run_generate(fetched)
    verified = run_verify(generated)
    published = run_publish(verified)
    confirm = confirm_job(published)
    waited >> fetched
    fetched >> generated >> verified >> published >> confirm


dag = solidworks_to_urdf()
