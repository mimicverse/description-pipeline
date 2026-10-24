"""Admit the sealed native capture into one attempt: verified bytes, no file authority.

The download is byte-verified against the native receipt, the capture tree is
installed by the transfer admission, the archived native files are re-bound
to the sealed selection, and a committed identical retry reuses the installed
tree only after it re-verifies against the registered freeze request and its
own transfer manifest.  The platform store carries the admission facts; no
file-side state is consulted.
"""

from __future__ import annotations

from pathlib import Path
import copy
import hashlib
import shutil
import zipfile

from ..interface_records import validate_source_record
from ..io import PipelineError, digest, file_digest, read_data
from ..sources.solidworks.selection import validate_file_index, verify_selection
from ..orchestration.airflow_client import capture_archive_metadata, validate_run_id
from ..orchestration.stage_transfer import (
    CAPTURE_ARCHIVE,
    CAPTURE_MANIFEST,
    MAX_MANIFEST_BYTES,
    admit_capture,
)
from ..native_transfer import verify_transfer
from ..stages import canonical_freeze_timestamp, validate_freeze_events
from . import flow
from .flow import RunPaths, _registered_request_sha256
from .store import PlatformStore


def _capture_selection(job: dict, run_id: str, native_tool: dict) -> dict:
    """Bind the capture to the selected closure, distinct from the full upload."""
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    if job.get("run_id") != run_id or request.get("run_id") != run_id:
        raise PipelineError("The native capture belongs to another requested run")
    if job.get("native_tool") != native_tool:
        raise PipelineError("The native capture tool differs from the selection reader")
    upload = validate_file_index(request.get("upload_files"))
    if request.get("upload_sha256") != digest(upload):
        raise PipelineError("The native capture upload index differs from its digest")
    selection = job.get("selection")
    if not isinstance(selection, dict) or selection.get("state") != "sealed":
        raise PipelineError("The native capture requires a sealed source selection")
    manifest = verify_selection(
        selection.get("manifest"),
        upload,
        job.get("package_files"),
        reader_sha256=native_tool.get("source_sha256"),
    )
    if (
        selection.get("staged_files") != upload
        or selection.get("reader_sha256") != manifest["reader_sha256"]
        or selection.get("upload_sha256") != manifest["upload_sha256"]
        or job.get("handoff_sha256") != manifest["handoff_sha256"]
        or request.get("main_assembly") != manifest["main_assembly"]
        or job.get("main_assembly") != manifest["main_assembly"]
    ):
        raise PipelineError("The native capture differs from its sealed source selection")
    return {
        "upload_files": upload,
        "upload_sha256": manifest["upload_sha256"],
        "selection_sha256": manifest["selection_sha256"],
        "selected_files": manifest["files"],
        "selection": manifest,
    }


def _check_selected_capture(capture: Path, selection: dict) -> None:
    """Require the archived native files to be the selected bytes."""
    record = read_data(capture / "evidence/discovery.json")
    if record.get("native_files") != selection["files"]:
        raise PipelineError("The capture native inventory differs from the sealed selection")
    for name, checksum in selection["files"].items():
        if file_digest(capture / "input/cad" / name) != checksum:
            raise PipelineError(f"The captured source differs from the selected bytes: {name}")


def registered_freeze_events(platform: PlatformStore, run_id: str) -> list[dict]:
    """Read the one registered Linux freeze block from platform rows."""

    events = []
    for row in flow.recorded_events(platform, run_id):
        if (
            not isinstance(row, dict)
            or row.get("stage") != "freeze"
            or row.get("kind") != "stage"
        ):
            continue
        payload = row.get("payload")
        if payload == {"state": "completed"}:
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("stage") != "freeze"
        ):
            raise PipelineError("A freeze-stage row is not a raw protocol event")
        events.append({"at": canonical_freeze_timestamp(row.get("at")), **copy.deepcopy(payload)})
    return validate_freeze_events(events)


def _event_key(event: dict, *, stage=None, kind="stage", at=None) -> tuple:
    """One event identity independent of whether its instant is a row or payload field."""

    payload = copy.deepcopy(event)
    timestamp = payload.pop("at", None)
    if timestamp is None:
        timestamp = at
    return (
        stage or payload.get("stage"),
        kind,
        canonical_freeze_timestamp(timestamp),
        digest(payload),
    )


def _append_native_events(
    platform: PlatformStore,
    run_id: str,
    capture: Path,
    expected_freeze_events,
) -> None:
    """Validate the carried Freeze block once, then mirror only new native events."""

    expected_freeze = validate_freeze_events(expected_freeze_events)
    if registered_freeze_events(platform, run_id) != expected_freeze:
        raise PipelineError("The native capture Freeze block differs from the registered platform facts")
    receipt = read_data(capture / "reports/native-stages.json")
    receipt_events = (receipt or {}).get("events") or []
    if not isinstance(receipt_events, list):
        raise PipelineError("The native stage receipt has no event list")
    receipt_freeze = [
        event
        for event in receipt_events
        if isinstance(event, dict) and event.get("stage") == "freeze"
    ]
    if validate_freeze_events(receipt_freeze) != expected_freeze:
        raise PipelineError("The native stage receipt changed its carried Freeze block")
    remaining = {
        _event_key(
            payload if isinstance(payload, dict) else {},
            stage=row.get("stage"),
            kind=row.get("kind"),
            at=row.get("at"),
        )
        for row in flow.recorded_events(platform, run_id)
        for payload in [row.get("payload") if isinstance(row, dict) else None]
    }
    receipt_keys = set()
    for event in receipt_events:
        if not isinstance(event, dict) or event.get("stage") == "freeze":
            continue
        key = _event_key(event)
        if key in receipt_keys:
            raise PipelineError("The native stage receipt repeats an event")
        receipt_keys.add(key)
        if key in remaining:
            # An earlier attempt already mirrored this event; recovery never duplicates it.
            remaining.remove(key)
            continue
        platform.append_event(
            run_id,
            stage=str(event.get("stage") or "capture"),
            kind=str(event.get("kind") or "stage"),
            payload=event,
        )


def source_identity(capture: Path) -> tuple[str, str | None]:
    """Hardware and revision of one admitted capture tree, read from its frozen source."""
    source_path = capture / "input/source.json"
    if not source_path.is_file():
        raise PipelineError("The admitted capture carries no input/source.json identity")
    payload = validate_source_record(read_data(source_path))
    identity = payload["identity"]
    metadata = payload["metadata"]
    hardware = identity["hardware_id"]
    revision = metadata.get("revision")
    return hardware, revision if isinstance(revision, str) and revision else None


def import_native_capture(
    platform: PlatformStore, paths: RunPaths, run_id: str, job: dict, endpoint
) -> dict:
    """Download the sealed native capture and admit it into the attempt."""
    run_id = validate_run_id(run_id)
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    native_request = job.get("request") if isinstance(job.get("request"), dict) else {}
    freeze_events = validate_freeze_events(native_request.get("freeze_events"))
    if native_request.get("freeze_events_sha256") != digest(freeze_events):
        raise PipelineError("The native capture Freeze block differs from its bound digest")
    native_tool = result.get("native_tool")
    if not isinstance(native_tool, dict) or not native_tool:
        raise PipelineError("The native job carries no tool record; the transfer cannot be verified")
    selection_binding = _capture_selection(job, run_id, native_tool)
    selection = selection_binding["selection"]
    handoff = selection["handoff_sha256"]
    upload_sha256 = selection_binding["upload_sha256"]
    main_assembly = selection["main_assembly"]
    prepared_dir = job.get("prepared_dir")
    prepared_files = job.get("prepared_files")
    if not isinstance(prepared_dir, str) or not prepared_dir:
        raise PipelineError("The native job carries no platform prepared source root")
    if (
        not isinstance(prepared_files, dict)
        or not prepared_files
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(checksum, str)
            or not checksum
            for name, checksum in prepared_files.items()
        )
    ):
        raise PipelineError("The native job carries no platform prepared source inventory")
    archive = capture_archive_metadata(job)
    capture = paths.capture_dir
    admitted = (
        capture.is_dir()
        and (capture / CAPTURE_MANIFEST).is_file()
        and file_digest(capture / CAPTURE_MANIFEST) == archive["manifest_sha256"]
    )
    if admitted:
        # A committed identical admission is reused only after the installed
        # tree re-verifies against the registered freeze request and its own
        # transfer manifest - a mutated or resealed tree never passes.
        authority = _registered_request_sha256(platform, run_id)
        frozen = capture / "input" / "request.json"
        if not frozen.is_file():
            # Interrupted between the raw install and the DAG's carry: recover the
            # registered frozen request from its sealed source - the same bytes the
            # registration bound, never weaker.
            freeze_row = platform.stage_records(run_id).get("freeze") or {}
            freeze_uri = freeze_row.get("checkpoint_uri")
            sealed_request = (
                platform.sealed_path(freeze_uri) / "input" / "request.json" if freeze_uri else None
            )
            if sealed_request is None or not sealed_request.is_file():
                raise PipelineError("The registered frozen request is missing from its sealed freeze tree")
            if file_digest(sealed_request) != authority:
                raise PipelineError("The registered frozen request differs from its sealed binding")
            frozen.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sealed_request, frozen)
        if file_digest(frozen) != authority:
            raise PipelineError("The admitted capture is missing the registered frozen input/request.json")
        summary = verify_transfer(
            capture,
            carried={"input/request.json": authority},
            expected_upload_sha256=upload_sha256,
            native_prepared_dir=prepared_dir,
            native_prepared_files=prepared_files,
        )
        if not isinstance(summary, dict) or summary.get("handoff_sha256") != handoff:
            raise PipelineError("The admitted capture differs from its native manifest receipt")
        _check_selected_capture(capture, selection)
        _append_native_events(platform, run_id, capture, freeze_events)
    else:
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        archive_path = paths.run_dir / CAPTURE_ARCHIVE
        endpoint.stream_capture_archive(run_id, archive, archive_path)
        if file_digest(archive_path) != archive["sha256"] or archive_path.stat().st_size != archive["size"]:
            raise PipelineError("The downloaded capture archive does not match its declared receipt")
        try:
            with zipfile.ZipFile(archive_path) as packed, packed.open(CAPTURE_MANIFEST) as manifest:
                total, checksum = 0, hashlib.sha256()
                while chunk := manifest.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_MANIFEST_BYTES:
                        raise PipelineError("The native manifest exceeds the transfer size limit")
                    checksum.update(chunk)
                if checksum.hexdigest() != archive["manifest_sha256"]:
                    raise PipelineError("The capture manifest does not match its native receipt")
        except (zipfile.BadZipFile, KeyError) as error:
            raise PipelineError("The capture archive contains no readable manifest") from error
        admit_capture(
            archive_path,
            capture,
            expected_run_id=run_id,
            expected_handoff_sha256=handoff,
            expected_upload_sha256=upload_sha256,
            expected_main_assembly=main_assembly,
            expected_native_tool=native_tool,
            expected_prepared_dir=prepared_dir,
            expected_prepared_files=prepared_files,
        )
        _check_selected_capture(capture, selection)
        _append_native_events(platform, run_id, capture, freeze_events)
    hardware, revision = source_identity(capture)
    return {
        "run_id": run_id,
        "handoff_sha256": handoff,
        "main_assembly": main_assembly,
        "state": "capture_admitted",
        "hardware_id": hardware,
        "revision": revision,
        "upload_sha256": upload_sha256,
        "native_prepared_dir": prepared_dir,
        "native_prepared_files": dict(prepared_files),
        **selection_binding,
    }
