"""The single portable run flow: consume registered sealed trees, produce one stage tree.

The platform store is the only authority: the capture consumption reads the
registered freeze record and the confirmed capture registration, every seed is
the sealed tree of the registered predecessor checkpoint, and the live events
stream through the caller's sink.  The working directories hold only in-flight
bytes - no state file is business authority.
"""

from __future__ import annotations

from pathlib import Path
import logging

from packaging.utils import canonicalize_name

from .. import solidworks
from ..delivery import PIPELINE_ID
from ..io import PipelineError, file_digest, read_data
from ..orchestration.airflow_client import JOB_SCHEMA, validate_run_id
from ..native_transfer import CAPTURE_MANIFEST, TEAM_REPORTS, verify_transfer
from ..runtime import RUNTIME_VERSIONS, required_packages, tool_record
from ..stages import (
    STAGE_IDS,
    canonical_freeze_timestamp,
    publication_succeeded,
)
from .store import PlatformStore

log = logging.getLogger(__name__)

_PORTABLE_STAGES = frozenset({"generate", "verify"})
_SEED_STAGE = {"generate": "capture", "verify": "generate"}
#: Registered run states projected onto the job-status vocabulary the run page reads.
_JOB_STATUS_BY_RUN_STATE = {"succeeded": "passed", "cancelled": "failed", "unresolved": "failed"}


def _registered_failure(rows: dict[str, dict]) -> dict:
    """The registered failure authority of one attempt: the failed stage's own receipt facts.

    The earliest failed engineering stage carries the original error (and its
    diagnostic identity when recorded); a stale native observation can never
    replace it because it lives in the registration, not in the endpoint.
    """
    for stage in STAGE_IDS:
        row = rows.get(stage) or {}
        if row.get("state") != "failed":
            continue
        receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
        return {
            "stage": stage,
            "error": receipt.get("error"),
            "error_code": receipt.get("error_code"),
            "detail": receipt.get("detail"),
            "diagnostic_path": receipt.get("diagnostic_path"),
            "failure_diagnostics": receipt.get("failure_diagnostics"),
        }
    return dict.fromkeys(("stage", "error", "error_code", "detail", "diagnostic_path", "failure_diagnostics"))


class RunPaths:
    """The in-flight working layout of one run; nothing here is authority."""

    def __init__(self, root: str | Path, run_id: str):
        self.root = Path(root)
        self.run_id = validate_run_id(run_id)

    @property
    def run_dir(self) -> Path:
        return self.root / self.run_id

    @property
    def capture_dir(self) -> Path:
        return self.run_dir / "capture"

    def stage_dir(self, stage: str) -> Path:
        if stage not in STAGE_IDS:
            raise PipelineError(f"Unknown engineering stage: {stage!r}")
        return self.run_dir / "stages" / stage

    @property
    def output_dir(self) -> Path:
        return self.run_dir / "output"

    def stage_output(self, stage: str) -> Path:
        return self.output_dir if stage == "publish" else self.stage_dir(stage)


def registered_checkpoint(platform: PlatformStore, run_id: str, stage: str) -> Path:
    """The sealed tree of one run's registered stage checkpoint."""
    row = platform.stage_records(run_id).get(stage)
    if row is None or row.get("state") != "completed" or not row.get("checkpoint_uri"):
        raise PipelineError(f"The {stage!r} stage of run {run_id!r} has no registered checkpoint")
    return platform.sealed_path(row["checkpoint_uri"])


def registered_capture(platform: PlatformStore, run_id: str) -> dict:
    """The confirmed capture receipt of one run - the native identity authority."""
    receipt = platform.confirmed_stage(run_id, "capture")
    if receipt is None:
        raise PipelineError("This run has no admitted native capture; fetch the transfer first")
    return receipt


def portable_identity_gate(platform: PlatformStore, run_id: str) -> dict:
    """Fail closed unless the portable runtime matches the registered native identity."""
    native_tool = registered_capture(platform, run_id).get("native_tool")
    if not isinstance(native_tool, dict) or not native_tool:
        raise PipelineError("The admitted capture carries no native tool record")
    portable = tool_record(role="portable")
    runtime = portable.get("runtime") or {}
    if runtime.get("role") != "portable":
        raise PipelineError("The portable runtime did not identify itself as role=portable")
    if runtime.get("system") != "Linux":
        raise PipelineError("The portable runtime must execute on Linux")
    if not str(runtime.get("python") or "").startswith("3.12"):
        raise PipelineError("The portable runtime must execute on the pinned Python 3.12")
    for key in ("pipeline_id", "version", "source_sha256"):
        if portable.get(key) != native_tool.get(key):
            raise PipelineError(f"The portable runtime differs from the native capture in {key}; refusing to continue")
    if portable.get("release") != native_tool.get("release"):
        raise PipelineError("The portable release identity differs from the native capture; refusing to continue")
    packages = {canonicalize_name(str(name)): value for name, value in (runtime.get("packages") or {}).items()}
    for name in required_packages("portable"):
        if packages.get(canonicalize_name(name)) != RUNTIME_VERSIONS.get(name):
            raise PipelineError(f"The portable runtime does not carry the pinned {name}")
    return portable


def _registered_request_sha256(platform: PlatformStore, run_id: str) -> str:
    """The registered frozen-original digest, from the platform authority.

    There is no file fallback: a missing freeze registration and a
    registration whose sealed bytes no longer rebind both refuse.
    """
    freeze = platform.confirmed_stage(run_id, "freeze")
    if freeze is None:
        raise PipelineError("The run has no registered frozen original to bind input/request.json")
    record = next(
        (item for item in freeze.get("artifacts") or [] if item.get("path") == "input/request.json"), None
    )
    if record is None or not isinstance(record.get("sha256"), str):
        raise PipelineError("The registered freeze receipt binds no input/request.json")
    return record["sha256"]


def revalidate_capture(platform: PlatformStore, run_id: str, capture: Path, frozen: Path) -> None:
    """Revalidate the in-flight capture tree against its registered records.

    The frozen original is anchored to the registered freeze record - both
    mutable copies must exist and equal the registered digest.  The native
    members are checked record-for-record against the sealed transfer
    manifest, and the handoff binding and the native tool bytes must equal
    the confirmed capture registration (read from the sealed tree the
    platform itself re-verifies).  No file fallback exists.
    """
    authority = _registered_request_sha256(platform, run_id)
    if not frozen.is_file():
        raise PipelineError("The run is missing the registered frozen input/request.json")
    if file_digest(frozen) != authority:
        raise PipelineError("The frozen input/request.json differs from the registered original")
    carried = capture / "input" / "request.json"
    if not carried.is_file():
        raise PipelineError("The admitted capture is missing the registered frozen input/request.json")
    if file_digest(carried) != authority:
        raise PipelineError("The carried input/request.json differs from the registered original")
    receipt = registered_capture(platform, run_id)
    upload_sha256 = receipt.get("upload_sha256")
    prepared_dir = receipt.get("native_prepared_dir")
    prepared_files = receipt.get("native_prepared_files")
    if (
        not isinstance(upload_sha256, str)
        or len(upload_sha256) != 64
        or any(character not in "0123456789abcdef" for character in upload_sha256)
    ):
        raise PipelineError("The recorded capture carries no full-upload digest")
    if not isinstance(prepared_dir, str) or not prepared_dir:
        raise PipelineError("The recorded capture carries no platform prepared source root")
    if not isinstance(prepared_files, dict) or not prepared_files:
        raise PipelineError("The recorded capture carries no platform prepared source inventory")
    summary = verify_transfer(
        capture,
        carried={"input/request.json": authority},
        expected_upload_sha256=upload_sha256,
        native_prepared_dir=prepared_dir,
        native_prepared_files=prepared_files,
    )
    if not isinstance(summary, dict):
        raise PipelineError("The admitted capture carries no sealed transfer to revalidate")
    if summary.get("run_id") != receipt.get("run_id"):
        # The transfer belongs to the original native producer; a linked
        # attempt's materialized copy must still present that producer's own
        # summary, not the direct parent attempt's identity.
        raise PipelineError("Admitted capture provenance differs from the recorded admission: run_id")
    if summary.get("handoff_sha256") != receipt.get("handoff_sha256"):
        raise PipelineError("Admitted capture provenance differs from the recorded admission: handoff_sha256")
    sealed_root = registered_checkpoint(platform, run_id, "capture")
    sealed_manifest = read_data(sealed_root / CAPTURE_MANIFEST)
    manifest = read_data(capture / CAPTURE_MANIFEST)
    if not isinstance(manifest, dict) or not isinstance(sealed_manifest, dict):
        raise PipelineError("Admitted capture manifest differs from the recorded admission")
    for key in ("files", "file_count", "total_bytes"):
        if manifest.get(key) != sealed_manifest.get(key):
            raise PipelineError(f"Admitted capture manifest differs from the recorded admission: {key}")
    if read_data(capture / "reports/native-tool.json") != receipt.get("native_tool"):
        raise PipelineError("Admitted capture native tool record differs from the recorded admission")


def recorded_events(platform: PlatformStore, run_id: str) -> list[dict]:
    """Every recorded event row of one run: the one bounded-paging read facility.

    The store pages forward with its own cursor; a fixed single page would
    truncate a genuine long history, so callers iterate here instead of
    reading one page each.
    """
    run_id = validate_run_id(run_id)
    rows: list[dict] = []
    after_seq = 0
    while True:
        page = platform.run_events(run_id, after_seq=after_seq)
        rows.extend(page)
        if len(page) < 200:
            return rows
        after_seq = page[-1].get("seq", after_seq + len(page))


def reuse_events(platform: PlatformStore, run_id: str, owner: str, stage: str) -> None:
    """Copy the parent's recorded events for the stages this attempt resumes past, marked reused."""
    prefix = set(STAGE_IDS[: STAGE_IDS.index(stage)])
    for event in recorded_events(platform, owner):
        if not isinstance(event, dict) or event.get("stage") not in prefix:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        marked = {
            **payload,
            "stage": event.get("stage"),
            "reuse": {
                "parent_run": owner,
                "source_run": (payload.get("reuse") or {}).get("source_run", owner),
                "reused": True,
            },
        }
        platform.append_event(
            run_id, stage=str(event.get("stage") or stage), kind=str(event.get("kind") or "stage"), payload=marked
        )


def _prior_events(platform: PlatformStore, run_id: str) -> list[dict]:
    payloads = []
    for row in recorded_events(platform, run_id):
        payload = row.get("payload") if isinstance(row, dict) else None
        payloads.append(payload if isinstance(payload, dict) else row)
    return payloads


def _write_failure_products(
    platform: PlatformStore, paths: RunPaths, run_id: str, stage: str, error: Exception
) -> None:
    from ..structural_report import write_structural_report

    receipt = {
        "run_id": run_id,
        "state": "failed",
        "passed": False,
        "stage": stage,
        "error": f"{type(error).__name__}: {error}",
        "error_code": getattr(error, "code", None),
        "detail": getattr(error, "detail", None) or getattr(error, "details", None),
        "events": recorded_events(platform, run_id),
    }
    receipt["structural_report"] = write_structural_report(paths.run_dir, receipt)


def run_stage(
    platform: PlatformStore,
    root: str | Path,
    run_id: str,
    stage: str,
    *,
    expected_subject: str | None = None,
    source_run_id: str | None = None,
    on_event=None,
) -> dict:
    """Run one portable stage to its boundary from registered inputs.

    A registered completion is returned as the confirmed receipt; otherwise
    the stage is produced from the registered sealed predecessor, and its
    events stream through ``on_event``.  The caller owns sealing and
    registration; a failure leaves the structural report in the work tree for
    the failure registration to carry.

    The capture/input authority is explicit: a portable resume binds the
    parent attempt named by the caller, while a native-boundary attempt
    (whose own capture was just imported) binds this attempt itself - the
    registered parent is never a silent fallback.  The first executed stage
    seeds from the parent's registered predecessor; every later stage seeds
    from this attempt's completed registered checkpoint.
    """
    run_id = validate_run_id(run_id)
    if stage not in _PORTABLE_STAGES:
        raise PipelineError(f"stage must be one of {tuple(sorted(_PORTABLE_STAGES))}: {stage!r}")
    paths = RunPaths(root, run_id)
    owner = validate_run_id(source_run_id) if source_run_id else run_id
    confirmed = platform.confirmed_stage(run_id, stage)
    if confirmed is not None:
        revalidate_capture(platform, owner, paths.capture_dir, paths.run_dir / "input" / "request.json")
        portable_identity_gate(platform, owner)
        return confirmed
    record = platform.run_record(run_id)
    if record is None:
        raise PipelineError("This run is not admitted; fetch the transfer first")
    boundary = record.get("from_stage")
    output = paths.stage_output(stage)
    with solidworks.output_lock(paths.run_dir):
        try:
            capture_receipt = registered_capture(platform, owner)
            revalidate_capture(platform, owner, paths.capture_dir, paths.run_dir / "input" / "request.json")
            portable_identity_gate(platform, owner)
            if stage == "generate" and expected_subject is not None:
                raise PipelineError("generate does not take an expected subject")
            if stage == "verify" and not expected_subject:
                raise PipelineError(f"{stage} requires the recorded subject of the previous checkpoint")
            seed_owner = owner if boundary == stage else run_id
            seed = registered_checkpoint(platform, seed_owner, _SEED_STAGE[stage])
            if stage == "verify":
                upload_sha256 = capture_receipt.get("upload_sha256")
                prepared_dir = capture_receipt.get("native_prepared_dir")
                prepared_files = capture_receipt.get("native_prepared_files")
                if (
                    not isinstance(upload_sha256, str)
                    or not isinstance(prepared_dir, str)
                    or not isinstance(prepared_files, dict)
                ):
                    raise PipelineError("The registered capture lacks its native source binding")
                summary = verify_transfer(
                    seed,
                    regenerated=TEAM_REPORTS,
                    carried={"input/request.json": _registered_request_sha256(platform, owner)},
                    expected_upload_sha256=upload_sha256,
                    native_prepared_dir=prepared_dir,
                    native_prepared_files=prepared_files,
                )
                if not isinstance(summary, dict):
                    raise PipelineError(f"{stage} seed carries no complete native receipt triplet")
            resume = None
            if owner != run_id:
                resume = {"parent_run": owner, "from_stage": boundary or stage}
                if boundary == stage:
                    # Only the boundary stage resumes past the parent's
                    # upstream work; later stages are this attempt's own.
                    reuse_events(platform, run_id, owner, stage)
            common = {
                "run_id": run_id,
                "prior_events": _prior_events(platform, run_id),
                "resume": resume,
                "handoff_sha256": capture_receipt.get("handoff_sha256"),
                "on_event": on_event,
            }
            if stage == "generate":
                receipt = solidworks.generate(seed, output, **common)
            else:
                receipt = solidworks.verify(seed, output, expected_subject=expected_subject, **common)
        except Exception as error:
            _write_failure_products(platform, paths, run_id, stage, error)
            raise
    return receipt


def _event_instant(value) -> str | None:
    """The recorded instant as canonical UTC text; malformed shapes stay absent."""

    try:
        return canonical_freeze_timestamp(value)
    except PipelineError:
        return None


def registered_delivery_events(platform: PlatformStore, run_id: str) -> list[dict]:
    """The recorded display events of one registered attempt, in recorded order.

    Every recorded payload that carries display meaning is kept; a stage
    confirmation row whose payload has no stage key renders as that stage's own
    state event (its recorded instant included), so the registered stage
    outcomes render without inventing anything the stream does not carry.
    """
    run_id = validate_run_id(run_id)
    events: list[dict] = []
    for row in recorded_events(platform, run_id):
        payload = row.get("payload") if isinstance(row, dict) else None
        event = dict(payload) if isinstance(payload, dict) else {}
        if not isinstance(event.get("stage"), str) or not event["stage"]:
            stage = row.get("stage") if isinstance(row, dict) else None
            if not isinstance(stage, str) or not stage:
                continue
            event["stage"] = stage
            if not isinstance(event.get("at"), str):
                instant = _event_instant(row.get("at"))
                if instant is not None:
                    event["at"] = instant
        events.append(event)
    return events


def _registered_capture_identity(
    platform: PlatformStore, run_id: str, rows: dict[str, dict]
) -> tuple[str | None, str | None]:
    """The hardware/revision identity read back from the re-bound registered capture tree.

    The capture identity is only consumed after the sealed rebinding validated
    the registration against the current bytes (the same validated read the
    reuse path uses): a damaged or forged tree never grants byte authority, so
    a raw stage projection can neither supply nor revive an identity.
    """
    row = rows.get("capture") or {}
    uri = row.get("checkpoint_uri")
    if row.get("state") != "completed" or not isinstance(uri, str) or not uri:
        return None, None
    try:
        confirmed = platform.confirmed_stage(run_id, "capture")
    except PipelineError:
        return None, None
    if not isinstance(confirmed, dict):
        return None, None
    from .capture import source_identity

    return source_identity(Path(platform.sealed_path(uri)))


def registered_delivery_snapshot(platform: PlatformStore, run_id: str) -> dict:
    """The job-shaped snapshot of one registered delivery, read from the platform records.

    The registered run row, the recorded event stream and the sealed stage
    receipts are the only sources: the registered run outcome is authoritative
    (a receipt can never flip a failed/cancelled/unresolved/running run into a
    pass), the delivery result is the publish receipt's quality and submission
    bound to the recorded subject, and hardware/revision are read back from the
    re-bound registered capture tree.  A run that never entered the register
    refuses - there is no working-directory or file fallback and no fabricated
    snapshot.
    """
    run_id = validate_run_id(run_id)
    record = platform.run_record(run_id)
    if record is None:
        raise PipelineError(f"The run {run_id!r} has no platform registration")
    rows = platform.stage_records(run_id)
    receipt = (rows.get("publish") or {}).get("receipt") or {}
    quality = receipt.get("quality") if isinstance(receipt.get("quality"), dict) else {}
    submission = receipt.get("submission") if isinstance(receipt.get("submission"), dict) else {}
    subject = receipt.get("subject_sha256")
    submission_bound = publication_succeeded(submission, subject)
    state = record.get("state")
    result = {
        "pipeline_id": PIPELINE_ID,
        "passed": bool(
            state == "succeeded"
            and submission_bound
            and quality.get("passed") is True
            and quality.get("subject_sha256") == subject
        ),
        "quality": quality,
        "subject_sha256": subject,
        "handoff_sha256": receipt.get("handoff_sha256"),
    }
    if submission:
        result["submission"] = submission
    hardware, revision = _registered_capture_identity(platform, run_id, rows)
    failure = _registered_failure(rows)
    return {
        "schema_version": JOB_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        # The registered outcome projects the status; a receipt never rewrites it.
        "status": _JOB_STATUS_BY_RUN_STATE.get(state, state) if isinstance(state, str) and state else "running",
        "request": {"handoff_sha256": receipt.get("handoff_sha256")},
        "events": registered_delivery_events(platform, run_id),
        "result": result,
        "error": failure["error"],
        "error_code": failure["error_code"],
        "detail": failure["detail"],
        "diagnostic_path": failure["diagnostic_path"],
        "failure_diagnostics": failure["failure_diagnostics"],
        # The canonical attribution of the error: the earliest failed stage of the
        # registration - consumers never guess it from event order.
        "failed_stage": failure["stage"],
        "hardware_id": hardware,
        "revision": revision,
    }
