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

from ..delivery import PIPELINE_ID
from ..interface_records import validate_source_record
from ..io import PipelineError, read_data
from ..orchestration.airflow_client import JOB_SCHEMA, validate_run_id
from ..stages import (
    STAGE_IDS,
    canonical_freeze_timestamp,
    publication_succeeded,
)
from .store import PlatformStore

log = logging.getLogger(__name__)

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


def source_identity(capture: Path) -> tuple[str, str | None]:
    """Read one admitted capture's frozen hardware and revision identity."""

    source_path = capture / "input/source.json"
    if not source_path.is_file():
        raise PipelineError("The admitted capture carries no input/source.json identity")
    payload = validate_source_record(read_data(source_path))
    identity = payload["identity"]
    metadata = payload["metadata"]
    hardware = identity["hardware_id"]
    revision = metadata.get("revision")
    return hardware, revision if isinstance(revision, str) and revision else None


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
