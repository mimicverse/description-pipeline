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
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.sdk import Param, dag, task
from airflow.sdk.exceptions import AirflowFailException

from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    EndpointError,
    EndpointNotFound,
    EndpointProtocolError,
    WindowsEndpoint,
    cache_release_manifest,
    cache_release_request,
    check_result,
    config_from_airflow_connection,
    drive_staging,
    native_run_id,
    normalize_change_summary,
    resolved_routing,
    submission_identity,
    validate_main_assembly,
)
from description_pipeline.orchestration.linux_store import LinuxStore
from description_pipeline.orchestration.stage_transfer import CAPTURE_MANIFEST
from description_pipeline.platform import capture as native_capture
from description_pipeline.platform import flow
from description_pipeline.delivery import (
    BUNDLE_SCHEMA,
    PIPELINE_ID,
    read_delivery_manifest,
    write_delivery_manifest,
)
from description_pipeline.interface_records import (
    CACHE_IMPORT_CONFIRMATION_SCHEMA,
    CHECK_POLICY_SCHEMA,
    cache_import_confirmation_digest,
    cache_release_params_digest,
    check_policy_digest,
)
from description_pipeline.platform.storage import seal_stage_confirmation, sealed_resolver
from description_pipeline.platform.store import PlatformStore
from description_pipeline.runtime import tool_record
from description_pipeline.stages import (
    CONFIRMED_STAGE_PASSED,
    CONFIRMED_STAGE_STATES,
    CONTRACT,
    CONTRACT_SHA256,
    DELIVERY_STAGES,
    STAGE_IDS,
    compact_view,
    contract_markdown,
    require_complete,
    stage_log,
    stage_view,
)
from description_pipeline.io import PipelineError, confined, digest, file_digest, inventory, read_data, write_json
from description_pipeline.request_evidence import carry_request_evidence, freeze_request_evidence, read_request_evidence
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.sources.solidworks.selection import validate_file_index

DAG_ID = "solidworks_to_urdf"
CONN_ID = os.environ.get("SOLIDWORKS_ENDPOINT_CONN_ID", "solidworks_windows")
SENSOR_MODE = os.environ.get("SOLIDWORKS_SENSOR_MODE", "reschedule")
POLL_INTERVAL = float(os.environ.get("SOLIDWORKS_POLL_INTERVAL", "10"))
POLL_TIMEOUT = float(os.environ.get("SOLIDWORKS_TIMEOUT", "3600"))
#: One native job at a time: the shared Windows CAD resource domain is serial
#: (contracts §资源所有权; the deploy installs this pool with capacity 1).
NATIVE_POOL = os.environ.get("SOLIDWORKS_NATIVE_POOL", "windows_native")
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


def _work_paths(conn_id: str, run_id: str) -> flow.RunPaths:
    """The in-flight working layout of one run; no state file is business authority."""
    config = config_from_airflow_connection(conn_id)
    if config.store_root is None:
        raise AirflowFailException(
            "pipeline.store_root / PIPELINE_STORE_ROOT is not configured; the Linux portable half cannot run"
        )
    return flow.RunPaths(config.store_root, run_id)


def _platform_binding() -> tuple[PlatformStore, Path]:
    """The platform registration binding: service DSN plus the sealed resolver over the store root."""
    config = config_from_airflow_connection(CONN_ID)
    if config.store_root is None:
        raise AirflowFailException(
            "pipeline.store_root / PIPELINE_STORE_ROOT is not configured; the platform registration cannot run"
        )
    return PlatformStore.from_environment(sealed=sealed_resolver(config.store_root)), Path(config.store_root)


def _delivery_manifest_arguments() -> tuple[dict, dict]:
    return (
        {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
        {"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
    )


def _ensure_subject_layout(tree: Path) -> None:
    """Every sealed stage tree carries the delivery subject layout; missing directories stay empty."""
    for name in ("input", "evidence", "model", "urdf", "meshes"):
        (tree / name).mkdir(parents=True, exist_ok=True)
    if not (tree / "tool.json").is_file():
        write_json(tree / "tool.json", tool_record(role="portable"))


def _platform_receipt(run_id: str, stage: str, receipt: dict, tree: str | Path, *, state: str | None = None) -> dict:
    """The one platform receipt for one stage tree: every binding is read back from the sealed bytes.

    A completion must bind the sealed handoff inventory; a real failure
    registers its actual products with or without a successful handoff and
    never loses the reports its tree already carries.
    """
    tree = Path(tree)
    manifest = read_delivery_manifest(tree)
    if state is None:
        if stage == "publish":
            state = (receipt.get("submission") or {}).get("state")
        else:
            confirmed = CONFIRMED_STAGE_STATES[stage]
            if len(confirmed) != 1:  # pragma: no cover - the contract pins one state per stage
                raise AirflowFailException(f"The {stage!r} stage carries no single confirmed state")
            state = next(iter(confirmed))
    handoff_path = tree / "input" / "handoff.json"
    handoff = None
    if handoff_path.is_file():
        try:
            handoff = read_data(handoff_path)
        except (PipelineError, OSError, ValueError):
            # A real failure byte-binds whatever the tree carries, malformed
            # inputs included; only a completion must parse its handoff index.
            if state == "completed":
                raise
    package_files = handoff.get("package_files") if isinstance(handoff, dict) else None
    if not isinstance(package_files, dict) or not package_files:
        if state == "completed":
            raise AirflowFailException(f"The {stage!r} stage tree carries no input/handoff.json inventory")
        package_files = None
    payload = {
        "schema_version": BUNDLE_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "stage": stage,
        "state": state,
        "passed": False if state == "failed" else CONFIRMED_STAGE_PASSED[stage],
        "subject_sha256": (manifest.get("subject") or {}).get("digest"),
        "artifacts": [dict(record) for record in (manifest.get("subject") or {}).get("files") or []],
    }
    if package_files is not None:
        payload["handoff_sha256"] = digest(package_files)
    for key in ("error", "tool", "native_tool", "quality", "submission"):
        if receipt.get(key) is not None:
            payload[key] = receipt[key]
    return payload


def _seal_and_confirm(
    platform: PlatformStore,
    store_root: Path,
    run_id: str,
    stage: str,
    tree: str | Path,
    receipt: dict,
    *,
    state: str = "completed",
) -> None:
    """Seal the stage tree through the one writer primitive, then register the stage record."""
    sealed_receipt = _platform_receipt(run_id, stage, receipt, tree, state=None if state == "completed" else "failed")
    uri, completed = seal_stage_confirmation(store_root, run_id, stage, Path(tree), sealed_receipt)
    executed_at = datetime.now(UTC) if state == "completed" else None
    if state == "completed":
        confirmed = platform.confirmed_stage(run_id, stage)
        if confirmed is not None:
            # The store is immutable: a retry consumes the confirmed fact instead
            # of overwriting it, and a different identity still refuses.
            if confirmed == completed:
                log.info("stage already confirmed with the same identity run=%s stage=%s", run_id, stage)
                return
            raise AirflowFailException(
                f"The {stage!r} stage of run {run_id!r} is already confirmed with another identity"
            )
    platform.confirm_stage(
        run_id=run_id,
        stage=stage,
        state=state,
        receipt=completed,
        checkpoint_uri=uri,
        original_executed_at=executed_at,
    )


def _register_failed_stage(
    platform: PlatformStore, store_root: Path, run_id: str, stage: str, error: Exception, tree: Path | None = None
) -> None:
    """Converge one real failure into the platform: the stage row, its run terminal state and one event.

    A stage tree that exists is sealed with the byte-bound actual products; a
    failure that produced no tree registers its shape only - a real failure is
    never blocked on success products and never grants completion.
    """
    receipt = {
        "schema_version": BUNDLE_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "stage": stage,
        "state": "failed",
        "passed": False,
        "error": f"{type(error).__name__}: {error}",
    }
    if tree is not None and (Path(tree) / "manifest.json").is_file():
        try:
            _seal_and_confirm(platform, store_root, run_id, stage, tree, receipt, state="failed")
            return
        except Exception:  # pragma: no cover - diagnostics must never mask the stage's own failure
            log.exception("failed-stage sealing unavailable run=%s stage=%s", run_id, stage)
    platform.confirm_stage(run_id=run_id, stage=stage, state="failed", receipt=receipt)


def _failure_tree(run_dir: Path) -> Path:
    """One snapshot of the attempt's actual products and reports for a failed registration."""
    tree = run_dir / "seal" / "failed"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    for name in ("input", "reports", "evidence"):
        source = run_dir / name
        if source.is_dir():
            shutil.copytree(source, tree / name)
    _ensure_subject_layout(tree)
    contract, policy = _delivery_manifest_arguments()
    write_delivery_manifest(tree, contract=contract, policy=policy)
    return tree


def _converge_failed_stage(
    run_id: str, stage: str, error: Exception, tree: Path | None = None, *, products: bool = False
) -> None:
    """Best-effort failure convergence; the original stage failure is never masked.

    ``products`` snapshots the attempt's actual files (its reports included)
    so a real failure registers what it actually produced, never less.
    """
    try:
        platform, store_root = _platform_binding()
        if tree is None and products:
            try:
                tree = _failure_tree(flow.RunPaths(store_root, run_id).run_dir)
            except Exception:  # pragma: no cover - the shape-only registration stands
                log.exception("failure-product snapshot unavailable run=%s stage=%s", run_id, stage)
                tree = None
        _register_failed_stage(platform, store_root, run_id, stage, error, tree=tree)
    except Exception:  # pragma: no cover - the stage failure stays the primary signal
        log.exception("failed-stage registration unavailable run=%s stage=%s", run_id, stage)


def _register_completed_stage(run_id: str, stage: str, tree: str | Path, receipt: dict) -> None:
    """The completed-stage registration: platform binding, sealed write, stage confirmation."""
    platform, store_root = _platform_binding()
    _seal_and_confirm(platform, store_root, run_id, stage, tree, receipt, state="completed")


def _materialize_linked_attempt(platform: PlatformStore, store_root: Path, run_id: str, source_run_id: str) -> None:
    """Materialize a linked attempt from its parent's registered rows and sealed trees.

    Every stage the attempt resumes past is confirmed from the parent's
    registered receipt and checkpoint; the receipt and its original execution
    instant travel verbatim, so the original producer identity stays visible
    instead of being rewritten for this attempt.  The attempt's own capture
    tree and frozen request are materialized from the sealed source trees -
    never from a mutable parent work directory - so a missing or damaged
    source refuses instead of falling back.
    """
    boundary = (platform.run_record(run_id) or {}).get("from_stage")
    if boundary not in DELIVERY_STAGES:
        return
    existing = platform.stage_records(run_id)
    source_rows = platform.stage_records(source_run_id)
    for stage in STAGE_IDS[: STAGE_IDS.index(boundary)]:
        if (existing.get(stage) or {}).get("state") == "completed":
            continue
        source = source_rows.get(stage) or {}
        if source.get("state") != "completed" or not source.get("checkpoint_uri"):
            raise AirflowFailException(
                f"The linked attempt resumes past {stage!r} without a confirmed source stage to reuse"
            )
        platform.confirm_stage(
            run_id=run_id,
            stage=stage,
            state="completed",
            receipt=source.get("receipt"),
            checkpoint_uri=source["checkpoint_uri"],
            reused_from_run=source_run_id,
            original_executed_at=source.get("original_executed_at"),
        )
    paths = flow.RunPaths(store_root, run_id)
    frozen = paths.run_dir / "input" / "request.json"
    if paths.capture_dir.is_dir() and frozen.is_file():
        return
    rows = platform.stage_records(run_id)
    capture_uri = (rows.get("capture") or {}).get("checkpoint_uri")
    freeze_uri = (rows.get("freeze") or {}).get("checkpoint_uri")
    if not capture_uri or not freeze_uri:
        raise AirflowFailException("The linked attempt has no reusable capture and frozen request to materialize")
    sealed_request = platform.sealed_path(freeze_uri) / "input" / "request.json"
    if not sealed_request.is_file():
        raise AirflowFailException("The linked attempt's frozen request is missing from the registered freeze tree")
    if paths.capture_dir.exists():
        shutil.rmtree(paths.capture_dir)
    shutil.copytree(platform.sealed_path(capture_uri), paths.capture_dir)
    carried = paths.capture_dir / "input" / "request.json"
    if not carried.is_file():
        carried.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sealed_request, carried)
    frozen.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sealed_request, frozen)


def _portable_registered(run_id: str, stage: str, *, tree: Path, context: dict | None = None, **kwargs) -> dict:
    """Run one portable stage, report its boundary, then register it through the sealed chain.

    The platform binding is required before the stage runs - an unavailable
    platform refuses before any side effect - and every recorded stage event
    is mirrored into the platform's live event stream.  The stage itself is
    produced by the registered flow from the sealed predecessor trees.
    """
    platform, store_root = _platform_binding()
    source_run_id = kwargs.get("source_run_id")
    if source_run_id:
        _materialize_linked_attempt(platform, store_root, run_id, source_run_id)

    def live(event: dict) -> None:
        try:
            platform.append_event(run_id, stage=stage, kind="stage", payload=event)
        except Exception:  # pragma: no cover - live events are best-effort
            log.exception("live event write failed run=%s stage=%s", run_id, stage)

    kwargs.setdefault("on_event", live)
    try:
        receipt = flow.run_stage(platform, store_root, run_id, stage, **kwargs)
        _portable_report(receipt, stage=stage, context=context)
        _portable_summary(receipt, stage=stage)  # a non-confirmed receipt never reaches registration
    except Exception as error:
        _converge_failed_stage(
            run_id, stage, error, tree=tree if Path(tree).is_dir() else None, products=True
        )
        raise
    try:
        _register_completed_stage(run_id, stage, tree, receipt)
    except Exception as error:
        _converge_failed_stage(
            run_id, stage, error, tree=tree if (Path(tree) / "manifest.json").is_file() else None
        )
        raise
    return receipt


def _register_freeze_stage(store: LinuxStore, run_id: str, *, intake: Path, upload_files: dict[str, str]) -> None:
    """Seal the admitted originals with the frozen request evidence and confirm the freeze stage."""
    platform, store_root = _platform_binding()
    platform.start_run(run_id)  # the first driver action claims the admitted run
    tree = store.stage_dir(run_id, "freeze")
    try:
        if not (tree / "manifest.json").is_file():
            _ensure_subject_layout(tree)
            for name in upload_files:
                source = confined(intake, name)
                target = confined(tree, f"input/{name}", exists=False)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            request = store.run_dir(run_id) / "input" / "request.json"
            if not request.is_file():
                raise AirflowFailException("The freeze stage carries no frozen input/request.json")
            shutil.copy2(request, tree / "input" / "request.json")
            write_json(
                tree / "input" / "handoff.json", {"passed": True, "package_files": dict(sorted(upload_files.items()))}
            )
            contract, policy = _delivery_manifest_arguments()
            write_delivery_manifest(tree, contract=contract, policy=policy)
        receipt = {"tool": read_data(tree / "tool.json")}
        _seal_and_confirm(platform, store_root, run_id, "freeze", tree, receipt, state="completed")
    except Exception as error:
        _converge_failed_stage(run_id, "freeze", error, tree=tree if (tree / "manifest.json").is_file() else None)
        raise


def _register_native_stages(
    platform: PlatformStore, store_root: Path, run_id: str, capture: Path, run_dir: Path
) -> None:
    """Seal one snapshot of the imported capture tree and confirm discover, then capture.

    The native members stay byte-exact in the run's capture tree; the sealed
    snapshot adds only the Linux-side subject layout and the delivery manifest.
    """
    snapshot = run_dir / "seal" / "native"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(capture, snapshot)
    _ensure_subject_layout(snapshot)
    if not (snapshot / "reports" / "native-tool.json").is_file():
        raise AirflowFailException("The admitted capture carries no reports/native-tool.json to register")
    if not (snapshot / "manifest.json").is_file():
        contract, policy = _delivery_manifest_arguments()
        write_delivery_manifest(snapshot, contract=contract, policy=policy)
    native_tool = read_data(snapshot / "reports" / "native-tool.json")
    for stage in ("discover", "capture"):
        _seal_and_confirm(
            platform, store_root, run_id, stage, snapshot, {"native_tool": native_tool}, state="completed"
        )


def _confirmed_at_text(value) -> str | None:
    """The platform-stored confirmation instant as the canonical second-resolution text."""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        stamped = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return stamped.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _release_registered_cache(
    platform: PlatformStore,
    endpoint: WindowsEndpoint,
    run_id: str,
    capture_dir: Path,
    *,
    intake: Path,
    upload_files: dict[str, str],
) -> None:
    """The IF-10 release through the platform operation family: intent -> claim -> settle.

    The confirmation instant comes from the platform's own capture record, so
    the import block - and with it the registered parameter digest - is stable
    across every retry; the block is persisted with the intent (``dispatch``)
    as the one stored request identity.  Only the claim that performs
    queued->running executes a new side effect; once claimed, a lost response
    is reconciled by replaying the same operation id, which the endpoint's own
    release journal answers with its original receipt or a safe resume, and
    the settle closes that same DB identity - never a new one, never a file
    fallback.  A deferred or failed release never fails the engineering stage.
    """
    try:
        records = platform.stage_records(run_id)
        confirmed_at = _confirmed_at_text((records.get("capture") or {}).get("original_executed_at"))
        if confirmed_at is None:
            log.warning("cache release deferred run=%s: the capture stage records no instant", run_id)
            return
        manifest = cache_release_manifest(intake, upload_files)
        sealed = file_digest(capture_dir / CAPTURE_MANIFEST)
        production = {"run_id": run_id, "operation_id": f"{run_id}:native"}
        confirmation = {
            "schema_version": CACHE_IMPORT_CONFIRMATION_SCHEMA,
            "imported": [dict(entry) for entry in manifest],
            "production": dict(production),
            "sealed": {"confirmed_at": confirmed_at, "receipt_sha256": sealed},
            "digest": "",
        }
        confirmation["digest"] = cache_import_confirmation_digest(confirmation)
        import_block = {"linux_run_id": run_id, "production": dict(production), "confirmation": confirmation}
        operation_key = f"{run_id}/cache:0"
        record = platform.register_operation(
            run_id=run_id,
            stage="capture",
            kind="cache_release",
            operation_key=operation_key,
            params_digest=cache_release_params_digest(import_block),
        )
        if record.state == "succeeded":
            return
        claim = platform.claim_operation(run_id=run_id, stage="capture", operation_key=operation_key)
        if not claim.granted and claim.record.state != "running":
            return
        request = cache_release_request(
            run_id=run_id,
            linux_run_id=run_id,
            production_operation_id=production["operation_id"],
            operation_id=record.operation_id,
            health=endpoint.health(),
            imported=manifest,
            sealed_receipt_sha256=sealed,
            import_block=import_block,
        )
        receipt = endpoint.release_cache(run_id, request)
        platform.settle_cache_release(run_id=run_id, stage="capture", operation_key=operation_key, receipt=receipt)
    except Exception as error:  # noqa: BLE001 - a deferred release never fails the stage
        log.warning("cache release deferred run=%s error=%s", run_id, error)


def _failed_native_stage(job: dict) -> str:
    """The engineering stage a terminal native failure belongs to (fallback: capture)."""
    try:
        view = stage_view(job)
    except Exception:  # pragma: no cover - a malformed view never hides the failure
        return "capture"
    for row in view.get("stages") or []:
        if row.get("id") in STAGE_IDS and row.get("state") == "failed":
            return row["id"]
    return "capture"


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


def _parent_subject(platform: PlatformStore, parent_run: str, stage: str) -> str:
    """The recorded subject of a parent portable checkpoint a linked rerun reuses."""
    row = platform.stage_records(parent_run).get(stage) or {}
    if row.get("state") != "completed" or not row.get("checkpoint_uri"):
        raise AirflowFailException(f"parent attempt {parent_run} has no {stage} checkpoint to reuse")
    subject = (row.get("receipt") or {}).get("subject_sha256")
    if not isinstance(subject, str) or not subject:
        raise AirflowFailException(f"parent attempt {parent_run} {stage} checkpoint carries no subject")
    return subject


def _sealed_stage_tree(platform: PlatformStore, run_id: str, stage: str) -> Path:
    """The sealed tree of one confirmed stage, byte- and scope-qualified by the store.

    The row's completed flag and locator are never trusted alone: the store's
    own confirmation query re-verifies the sealed bytes, the locator scope and
    the receipt before the tree is handed out, and a missing registration
    refuses loudly.
    """
    if platform.confirmed_stage(run_id, stage) is None:
        raise AirflowFailException(f"the run {run_id} has no confirmed {stage!r} to read")
    row = platform.stage_records(run_id).get(stage) or {}
    if not row.get("checkpoint_uri"):
        raise AirflowFailException(f"the run {run_id} has no confirmed {stage!r} to read")
    return platform.sealed_path(row["checkpoint_uri"])


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


def _submission_frame(context) -> tuple[dict, dict]:
    """The submission identity and configured target routing of one trigger.

    The publish target and the change summary are required (conf first, then
    params): a missing or blank field and an unconfigured target fail the run
    here - never a deferred publish.  The parent revision is read best-effort
    from the model checkout so the derived revision keeps its successor
    linkage; ``routing`` is the same config resolution a linked publish
    rebinds ``repository_slug``/``repository_base`` from.
    """

    conf = getattr(context["dag_run"], "conf", None)
    conf = conf if isinstance(conf, dict) else {}
    target = str(conf.get("target_hardware") or context["params"].get("target_hardware") or "").strip()
    summary = normalize_change_summary(
        str(conf.get("change_summary") or context["params"].get("change_summary") or "")
    ).strip()
    if not target:
        raise AirflowFailException(
            "target_hardware is required: the run carries no configured publication target to publish to"
        )
    if not summary:
        raise AirflowFailException("change_summary is required: the run carries no nonempty change summary")
    principal = conf.get("submission_principal")
    if not isinstance(principal, str) or not principal.strip():
        principal = str(getattr(context["dag_run"], "triggering_user_name", "") or "").strip()
    config = config_from_airflow_connection(CONN_ID)
    routing = config.targets.get(target)
    if routing is None:
        raise AirflowFailException(f"target_hardware {target!r} is not a configured publication target")
    from description_pipeline.repository.urdf_pr import published_revision

    try:
        identity = submission_identity(
            target_hardware=target,
            change_summary=summary,
            principal=principal,
            config=config,
            published_revision=published_revision,
            warn=lambda message: log.warning("%s", message),
        )
    except EndpointProtocolError as error:
        raise AirflowFailException(str(error)) from error
    if identity is None:
        raise AirflowFailException(f"target_hardware {target!r} produced no publication identity")
    return identity, routing


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


def _store_failure_report(endpoint: WindowsEndpoint, request: dict, job: dict) -> None:
    """Best-effort Linux-rendered structural-team report of one failed native job.

    The endpoint seals raw diagnostics only; the report is rendered here, from
    the hash-bound sealed receipt and evidence, so the native host never writes
    Markdown.  A fetch or render failure is logged and never masks the run
    failure itself.
    """

    from description_pipeline.structural_report import write_structural_report

    downloader = getattr(endpoint, "download_failure_diagnostics", None)
    if downloader is None:
        return
    run_id = request["run_id"]
    try:
        store = _linux_store(request["conn_id"])
        run_dir = store.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".failure-diagnostics-", dir=run_dir))
        try:
            downloader(run_id, job, staging)
            receipt = read_data(staging / "receipt.json")
            if not isinstance(receipt, dict) or receipt.get("run_id") != run_id:
                raise PipelineError("sealed failure diagnostics do not belong to this run")
            written = write_structural_report(run_dir, receipt, evidence_root=staging / "evidence")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        log.info(
            "structural report run_id=%s markdown=%s json=%s",
            run_id,
            written.get("markdown"),
            written.get("json"),
        )
    except EndpointNotFound:
        log.info("structural report run_id=%s absent", run_id)
    except (AirflowFailException, EndpointError, EndpointProtocolError, PipelineError, OSError, ValueError) as error:
        log.warning("structural report unavailable run_id=%s error=%s", run_id, error)


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
        identity_block = request.get("identity") if isinstance(request.get("identity"), dict) else None
        submission = None
        if identity_block is not None:
            from description_pipeline.sources.solidworks.discovery import SubmissionIdentity

            handoff_digest = manifest.get("handoff_sha256")
            submission = SubmissionIdentity(
                hardware_id=identity_block.get("hardware_id"),
                owner=identity_block.get("owner"),
                change_summary=identity_block.get("change_summary"),
                parent_revision=identity_block.get("parent_revision"),
                control_reference=(
                    handoff_digest if isinstance(handoff_digest, str) else identity_block.get("control_reference")
                ),
            )
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
            submission_identity=submission,
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
    if ti is None or ti.xcom_pull(task_ids="discover", key="engineering_progress") != progress:
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
        _store_failure_report(endpoint, request, job)
        failure = AirflowFailException(f"SolidWorks job {request['run_id']} failed: {job.get('error')}")
        _converge_failed_stage(request["run_id"], _failed_native_stage(job), failure, products=True)
        raise failure
    return job["status"] in {"passed", "native_complete"}


def _same_request(job: dict, request: dict) -> None:
    expected = {key: request[key] for key in ("run_id", "upload_files", "upload_sha256", "main_assembly")}
    if request.get("resume") is not None:
        expected["resume"] = request["resume"]
    if job.get("request") != expected:
        raise AirflowFailException("Endpoint job differs from the requested mechanical handoff")


def _confirm_delivery(published: dict) -> dict:
    """Confirm the six-stage receipt and report the delivery (PR, commit, subject)."""

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
    submission = result.get("submission") or {}
    log.info(
        "delivered run_id=%s hardware=%s revision=%s subject=%s state=%s",
        run_id,
        routing["hardware_id"],
        routing["revision"],
        result["quality"].get("subject_sha256"),
        submission.get("state"),
    )
    log.info(
        "delivered pr=%s commit=%s base=%s",
        submission.get("url"),
        submission.get("commit"),
        submission.get("base"),
    )
    return {
        "run_id": run_id,
        "pipeline_id": result["pipeline_id"],
        "handoff": handoff,
        "stages": compact_view(view),
        "quality": {key: result["quality"].get(key) for key in ("passed", "subject_sha256")},
        "submission": result["submission"],
    }


@dag(
    dag_id=DAG_ID,
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["solidworks", "urdf", "windows"],
    description="Engineering folder → native capture → verified URDF + review PR",
    default_args={"retries": 2, "retry_delay": timedelta(seconds=15)},
    doc_md=(
        "# SolidWorks → URDF\n\n"
        "One engineering folder becomes a verified URDF delivery and a review PR. Linux admits the sealed "
        "folder and stages it to the Windows endpoint; the native engine resolves the main assembly's "
        "references inside that staging and captures the native evidence; Linux then generates the URDF, "
        "independently verifies it and publishes the PR. Transport and polling are orchestration tasks.\n\n"
        "## Read a run in the grid\n\n"
        "| Task | Stage | What it means |\n"
        "|---|---|---|\n"
        "| `freeze` | freeze | Admitted folder, file inventory and handoff digest |\n"
        "| `discover` | discover | Windows native half: submission, reference resolution, definition "
        "exchange and capture; native stage checks appear in these logs |\n"
        "| `capture` | capture | Sealed native capture imported into the Linux attempt store |\n"
        "| `generate`, `verify`, `publish` | generate, verify, publish | Linux URDF generation, "
        "independent verification, review PR and the final six-stage delivery receipt |\n\n"
        "## Stage contract\n\n" + contract_markdown() + "\n\n## Where results live\n\n"
        "- Task logs: native stage checks in `discover`; Linux results "
        "(`portable stage=... state=... checks=n/m`) in `generate` / `verify` / `publish`; "
        "the delivery summary in `publish`.\n"
        "- Per-run XCom `engineering_stages`; detailed receipt at `reports/stages.json`; delivered or "
        "failed native runs also retain the structural-team report at `reports/structural-team.md` and "
        "`reports/structural-team.json`.\n"
        "- The operator page shows inputs, checks, outputs and evidence per stage. Engineering review "
        "scope stays with the bound review records; the platform reports it without claiming approvals "
        "are pending or complete.\n"
    ),
    params={
        "handoff_path": Param(
            "",
            type="string",
            title="Engineering folder path",
            description=(
                "Sealed engineering folder on this Linux host that is staged to the Windows endpoint "
                "for native reference resolution (an absolute folder inside the configured handoff roots)"
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
        "target_hardware": Param(
            "",
            type="string",
            title="Publication target hardware identity",
            description=(
                "Required configured hardware identity used to derive the publication identity; "
                "must name a configured target or the run fails before any handoff resolution"
            ),
        ),
        "change_summary": Param(
            "",
            type="string",
            title="Change summary",
            description="Required nonempty one-line change summary carried into the release submission",
        ),
    },
)
def solidworks_to_urdf():
    @task(
        doc_md=(
            "Freeze stage: admit the sealed folder, bind its file inventory and handoff digest for the engineering job."
        )
    )
    def freeze(**context) -> dict:
        identity, routing = _submission_frame(context)
        linked = _linked_conf(context)
        if linked is not None:
            if linked["from_stage"] in DELIVERY_STAGES:
                # A portable-linked rerun resumes from the parent's Linux attempt and never
                # contacts Windows: the parent's registered capture is the authority.
                platform, store_root = _platform_binding()
                receipt = platform.confirmed_stage(linked["parent_run"], "capture")
                if receipt is None or not receipt.get("handoff_sha256"):
                    raise AirflowFailException(
                        f"Linked parent {linked['parent_run']} has no admitted Linux capture to resume"
                    )
                request = {
                    "run_id": _run_uuid(context),
                    "package": str(_sealed_stage_tree(platform, linked["parent_run"], "capture")),
                    "handoff_sha256": str(receipt["handoff_sha256"]),
                    "conn_id": CONN_ID,
                    "resume": linked,
                    "linux_owned": True,
                    "repository_slug": routing["repository_slug"],
                    "repository_base": routing["base"],
                }
                parent_record, _ = read_request_evidence(
                    _sealed_stage_tree(platform, linked["parent_run"], "freeze") / "input" / "request.json"
                )
                carried = carry_request_evidence(
                    _sealed_stage_tree(platform, linked["parent_run"], "freeze"),
                    flow.RunPaths(store_root, request["run_id"]).run_dir,
                )
                request["request_evidence"] = str(carried)
                selection = _linked_selection(
                    parent_record.get("main_assembly"), context["params"].get("main_assembly")
                )
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
            platform, store_root = _platform_binding()
            carried = carry_request_evidence(
                _sealed_stage_tree(platform, linked["parent_run"], "freeze"),
                flow.RunPaths(store_root, request["run_id"]).run_dir,
            )
            request["request_evidence"] = str(carried)
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
            request["identity"] = {**identity, "control_reference": request["upload_sha256"]}
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
        request = {
            "run_id": run_id,
            "upload_files": upload_files,
            "upload_sha256": digest(upload_files),
            "main_assembly": main,
            "intake_dir": str(intake),
            "conn_id": CONN_ID,
        }
        request["identity"] = {**identity, "control_reference": request["upload_sha256"]}
        conf = getattr(context["dag_run"], "conf", None)
        conf = conf if isinstance(conf, dict) else {}
        request_id = str(conf.get("request_id") or context["params"].get("request_id") or "").strip()
        if not request_id:
            raise AirflowFailException("request_id is required: the run carries no authenticated request identity")
        config_digest = str(os.environ.get("PLATFORM_CONFIG_DIGEST") or "").strip()
        if not config_digest:
            raise AirflowFailException(
                "PLATFORM_CONFIG_DIGEST is unavailable: the controlled platform configuration identity is required"
            )
        conf_digest = str(conf.get("platform_config_digest") or "").strip()
        if conf_digest and conf_digest != config_digest:
            raise AirflowFailException(
                "conf platform_config_digest does not match the controlled platform configuration identity"
            )
        store = _linux_store(CONN_ID)
        evidence = freeze_request_evidence(
            store.run_dir(run_id),
            request_id=request_id,
            target_hardware=identity["hardware_id"],
            change_summary=identity.get("change_summary") or "",
            main_assembly=main,
            handoff_digest=request["upload_sha256"],
            platform_config_digest=config_digest,
        )
        request["request_evidence"] = str(evidence)
        request["request_id"] = request_id
        _register_freeze_stage(store, run_id, intake=intake, upload_files=upload_files)
        return request

    @task.sensor(
        mode=SENSOR_MODE,
        poke_interval=POLL_INTERVAL,
        timeout=POLL_TIMEOUT,
        pool=NATIVE_POOL,
        doc_md=(
            "Discover stage: submit the same UUID and frozen handoff to the serial Windows queue, "
            "then drive the transport/definition exchange until the native job is terminal; retries "
            "never replay CAD."
        ),
    )
    def discover(request: dict, **context) -> bool:
        if request.get("linux_owned"):
            log.info(
                "linked attempt run_id=%s from_stage=%s is linux-owned; no Windows job",
                request["run_id"],
                (request.get("resume") or {}).get("from_stage"),
            )
            return True
        endpoint = _endpoint(request["conn_id"])
        try:
            endpoint.get_job(request["run_id"])
        except EndpointNotFound:
            job = endpoint.start_staging_job(
                run_id=request["run_id"],
                upload_files=request["upload_files"],
                main_assembly=request["main_assembly"],
                resume=request.get("resume"),
            )
            log.info("started run_id=%s status=%s", job["run_id"], job["status"])
            return False  # the reschedule poll continues on the next poke
        return _poke(request, **context)

    @task(
        doc_md=("Capture stage: import the sealed native capture into the Linux store and bind routing for generation.")
    )
    def capture(request: dict) -> dict:
        run_id = request["run_id"]
        resume = request.get("resume") or {}
        if request.get("linux_owned"):
            parent_run = resume["parent_run"]
            platform, store_root = _platform_binding()
            receipt = platform.confirmed_stage(parent_run, "capture")
            if receipt is None or not receipt.get("handoff_sha256"):
                raise AirflowFailException(f"Linked parent {parent_run} has no admitted Linux capture to resume")
            parent_record, _ = read_request_evidence(
                _sealed_stage_tree(platform, parent_run, "freeze") / "input" / "request.json"
            )
            hardware, revision = native_capture.source_identity(
                _sealed_stage_tree(platform, parent_run, "capture")
            )
            binding = {
                "run_id": run_id,
                "source_run_id": parent_run,
                "from_stage": resume.get("from_stage"),
                "handoff_sha256": receipt.get("handoff_sha256"),
                "main_assembly": parent_record.get("main_assembly"),
                # The trigger's conf target and its configured routing are the authority for a
                # linked publish; the parent's stored record may carry nulls.
                "repository_slug": request.get("repository_slug"),
                "repository_base": request.get("repository_base"),
                "hardware_id": hardware,
                "revision": revision,
                "state": "linked",
            }
        else:
            try:
                endpoint = _endpoint(request["conn_id"])
                job = endpoint.get_job(run_id)
                _same_request(job, request)
                if job.get("status") != "native_complete":
                    raise AirflowFailException(
                        f"native job {run_id} is {job.get('status')}, not native_complete"
                    )
                platform, store_root = _platform_binding()
                paths = flow.RunPaths(store_root, run_id)
                binding = native_capture.import_native_capture(platform, paths, run_id, job, endpoint)
                # The frozen request evidence rides the delivery from the capture tree
                # onward; generation and verification consume it byte-verbatim.
                carry_request_evidence(paths.run_dir, paths.capture_dir)
                # The trigger's conf target and its configured routing are the authority;
                # the native job no longer carries publication routing.
                binding["repository_slug"] = request.get("repository_slug")
                binding["repository_base"] = request.get("repository_base")
                binding["source_run_id"] = None
                binding["from_stage"] = None
                # The two native stages are registered over one sealed snapshot of the
                # imported capture tree; the native members themselves stay byte-exact,
                # and the confirmed capture import is the predecessor the IF-10
                # release operation binds below through the platform operation family.
                _register_native_stages(platform, store_root, run_id, paths.capture_dir, paths.run_dir)
                _release_registered_cache(
                    platform,
                    endpoint,
                    run_id,
                    paths.capture_dir,
                    intake=Path(request["intake_dir"]),
                    upload_files=request["upload_files"],
                )
            except Exception as error:
                _converge_failed_stage(run_id, "capture", error, products=True)
                raise
        log.info(
            "linux capture ready run_id=%s source=%s state=%s",
            run_id,
            binding.get("source_run_id"),
            binding.get("state"),
        )
        return {"request": request, **binding}

    @task(doc_md="Generate stage: build the URDF on Linux; this checkpoint is intentionally unverified.")
    def generate(binding: dict, **context) -> dict:
        if binding.get("from_stage") in {"verify", "publish"}:
            # The linked attempt resumes past generation: reuse the parent checkpoint.
            platform, _root = _platform_binding()
            subject = _parent_subject(platform, binding["source_run_id"], "generate")
            return {**binding, "generate": {"state": "reused", "passed": True, "subject_sha256": subject}}
        paths = _work_paths(binding["request"]["conn_id"], binding["run_id"])
        receipt = _portable_registered(
            binding["run_id"],
            "generate",
            tree=paths.stage_dir("generate"),
            context=context,
            source_run_id=binding.get("source_run_id"),
        )
        return {**binding, "generate": _portable_summary(receipt, stage="generate")}

    @task(doc_md="Verify stage: independent verification on Linux, including the MuJoCo consumer.")
    def verify(generated: dict, **context) -> dict:
        if generated.get("from_stage") == "publish":
            # The linked attempt resumes past verification: reuse the parent checkpoint.
            platform, _root = _platform_binding()
            subject = _parent_subject(platform, generated["source_run_id"], "verify")
            return {**generated, "verify": {"state": "reused", "passed": True, "subject_sha256": subject}}
        subject = (generated.get("generate") or {}).get("subject_sha256")
        if not subject:
            raise AirflowFailException("generate produced no subject to verify")
        paths = _work_paths(generated["request"]["conn_id"], generated["run_id"])
        receipt = _portable_registered(
            generated["run_id"],
            "verify",
            tree=paths.stage_dir("verify"),
            context=context,
            expected_subject=subject,
            source_run_id=generated.get("source_run_id"),
        )
        return {**generated, "verify": _portable_summary(receipt, stage="verify")}

    @task(
        doc_md=(
            "Publish stage: publish the verified delivery as the review PR from Linux; no repository work on Windows."
        )
    )
    def publish(verified: dict, **context) -> dict:
        subject = (verified.get("verify") or {}).get("subject_sha256")
        if not subject:
            raise AirflowFailException("verify produced no subject to publish")
        slug, base = verified.get("repository_slug"), verified.get("repository_base")
        repository = config_from_airflow_connection(CONN_ID).repositories.get(str(slug or ""))
        if repository is None:
            raise AirflowFailException(f"no configured Linux checkout for model repository {slug!r}")
        paths = _work_paths(verified["request"]["conn_id"], verified["run_id"])
        receipt = _portable_registered(
            verified["run_id"],
            "publish",
            tree=paths.output_dir,
            context=context,
            expected_subject=subject,
            repository=repository,
            base=base,
            source_run_id=verified.get("source_run_id"),
        )
        published = {**verified, "publish": _portable_summary(receipt, stage="publish")}
        return _confirm_delivery(published)

    request = freeze()
    started = discover(request)
    # The poll follows the submission even where op_kwargs XCom resolution is not
    # inferred as a dependency edge.
    fetched = capture(request)
    generated = generate(fetched)
    verified = verify(generated)
    published = publish(verified)
    started >> fetched
    fetched >> generated >> verified >> published


dag = solidworks_to_urdf()
