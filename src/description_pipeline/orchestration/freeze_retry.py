"""The one resolved freeze-retry identity: registered evidence to a sealed intake.

Why this module exists (complexity justification): a terminal freeze failure must be
re-runnable on Linux from the SAME sealed originals with no second upload and no claim
written on the parent.  New-shape parents carry the sealed upload identity on their
request baseline; the original (old-shape) parents predate that field, so the identity
is resolved from their OWN immutable registered evidence - the freeze stage receipt's
bound Airflow failure log (JSONL) and its parent-bound selection event - and then
re-verified against the present sealed intake.  One resolver type serves the portal
eligibility, the child admission and the DAG freeze so all three sites agree
byte-for-byte; there is deliberately no permissive fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..io import PipelineError, digest
from ..platform.store import PlatformStore
from ..sources.solidworks.revision import package_inventory
from ..sources.solidworks.selection import validate_file_index

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SELECTION_EVENT = re.compile(
    r"^selection intake=(?P<intake>.+?) files=(?P<files>[0-9]+) "
    r"upload_sha256=(?P<handoff>[0-9a-f]{64}) main_assembly=(?P<main>.+?) "
    r"endpoint_conn=(?P<conn>\S+)$"
)


class FreezeRetryRefusal(Exception):
    """One canonical refusal; reasons stay inside the existing stop vocabulary."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ResolvedFreezeIdentity:
    """One verified identity of the sealed originals a freeze retry re-commits."""

    source: str
    handoff_sha256: str
    files: int
    bytes: int
    folder: str
    intake_path: Path
    main_assembly: str
    purpose: str | None
    upload_files: dict[str, str]
    provenance: dict | None


def _identity_refusal(detail: str) -> FreezeRetryRefusal:
    return FreezeRetryRefusal("originals_identity_missing", detail)


def _validated_provenance(value: object) -> dict:
    """The legitimate immutable provenance marker carried by an evidence-resolved parent."""

    if not isinstance(value, dict) or set(value) != {"source", "path", "sha256", "parent_run_id"}:
        raise _identity_refusal(
            "Upload identity has a missing or invalid provenance marker; Freeze inputs cannot be rerun"
        )
    path = value.get("path")
    sha256 = value.get("sha256")
    parent_run_id = value.get("parent_run_id")
    if not (
        value.get("source") == "parent_freeze_failure_log"
        and isinstance(path, str)
        and path
        and Path(path).is_absolute()
        and isinstance(sha256, str)
        and _SHA256.fullmatch(sha256) is not None
        and isinstance(parent_run_id, str)
        and parent_run_id
    ):
        raise _identity_refusal("Upload identity has an invalid provenance field; Freeze inputs cannot be rerun")
    anchor = Path(path)
    if anchor.is_symlink() or not anchor.is_file():
        raise _identity_refusal(
            "Provenance evidence is unavailable (missing, linked, or not a regular file); Freeze inputs cannot be rerun"
        )
    try:
        anchor_bytes = anchor.read_bytes()
    except OSError as error:
        raise _identity_refusal("Provenance evidence cannot be read; Freeze inputs cannot be rerun") from error
    if hashlib.sha256(anchor_bytes).hexdigest() != sha256:
        raise _identity_refusal("Provenance evidence does not match the recorded digest; Freeze inputs cannot be rerun")
    return {"source": "parent_freeze_failure_log", "path": path, "sha256": sha256, "parent_run_id": parent_run_id}


def _safe_intake(candidate: str, intake_root: Path | None, *, context_text: str) -> Path:
    """One portable, non-symlinked intake path, confined to the upload root when given."""

    path = Path(candidate)
    if not path.is_absolute() or ".." in path.parts:
        raise _identity_refusal(f"{context_text} path is unsafe; rerun refused")
    if path.is_symlink():
        raise _identity_refusal(f"{context_text} is a symbolic link; rerun refused")
    if intake_root is not None:
        root = Path(intake_root)
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise _identity_refusal(f"{context_text} is outside the platform upload root; rerun refused") from error
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise _identity_refusal(f"{context_text} contains a symbolic-link path; rerun refused")
    return path


def _read_bound_selection(log_path: str, bound_sha256: str, *, parent_dag_run_id: str, expected_dag_id: str) -> dict:
    """The single parent-bound freeze selection record of one byte-verified JSONL log."""

    path = Path(log_path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise _identity_refusal(
            "Freeze-failure evidence is unavailable (missing, linked, or not a regular file); rerun refused"
        )
    try:
        evidence_bytes = path.read_bytes()
    except OSError as error:
        raise _identity_refusal("Freeze-failure evidence cannot be read; rerun refused") from error
    if hashlib.sha256(evidence_bytes).hexdigest() != bound_sha256:
        raise _identity_refusal("Freeze-failure evidence does not match the recorded digest; rerun refused")
    try:
        raw = evidence_bytes.decode("utf-8")
    except UnicodeError as error:
        raise _identity_refusal("Freeze-failure evidence cannot be decoded; rerun refused") from error
    selections: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("event"), str):
            continue
        if not record["event"].startswith("selection "):
            continue
        if (
            record.get("run_id") != parent_dag_run_id
            or record.get("task_id") != "freeze"
            or record.get("dag_id") != expected_dag_id
        ):
            continue
        match = _SELECTION_EVENT.fullmatch(record["event"])
        if match is None:
            raise _identity_refusal("Freeze-failure evidence contains an unreadable selection record; rerun refused")
        selections.append(match.groupdict())
    if not selections:
        raise _identity_refusal("Freeze-failure evidence is not bound to this run's input selection; rerun refused")
    if len(selections) != 1:
        raise _identity_refusal("Freeze-failure evidence has duplicate or ambiguous selection records; rerun refused")
    fields = selections[0]
    return {
        "intake": fields["intake"],
        "files": int(fields["files"]),
        "handoff_sha256": fields["handoff"],
        "main_assembly": fields["main"],
        "path": str(path),
        "sha256": bound_sha256,
    }


def resolve_freeze_retry_identity(
    platform: PlatformStore,
    parent_run_id: str,
    *,
    parent_dag_run_id: str,
    expected_dag_id: str,
    intake_path: Path,
    intake_root: Path | None = None,
) -> ResolvedFreezeIdentity:
    """Resolve and fully verify the one identity a freeze retry may re-commit.

    A registered baseline wins verbatim (the strict new-shape path); without it the
    identity comes from the freeze stage receipt's bound failure evidence, validated
    against this parent (run, task, dag bindings, registered main assembly) and
    against the trigger's intake path.  In both shapes the present sealed intake is
    recomputed in full and must equal the resolved identity byte-for-byte.
    """

    record = platform.run_record(parent_run_id)
    if not isinstance(record, dict):
        raise FreezeRetryRefusal(
            "not_ready", "The original run is not registered yet and cannot be rerun from this step"
        )
    if record.get("state") not in {"failed", "succeeded", "cancelled"}:
        raise FreezeRetryRefusal(
            "not_ready", "The original run is not terminal and cannot provide sealed originals yet"
        )
    stages = platform.stage_records(parent_run_id)
    freeze_row = stages.get("freeze") if isinstance(stages, dict) else None
    if not (isinstance(freeze_row, dict) and freeze_row.get("state") in {"failed", "completed"}):
        raise FreezeRetryRefusal(
            "linux_owned", "The run has no registered freeze to provide sealed originals"
        )
    request_id = record.get("request_id")
    request = platform.request_record(request_id) if isinstance(request_id, str) else None
    if not isinstance(request, dict):
        raise _identity_refusal("The original run has no registered request identity; Freeze inputs cannot be rerun")
    main = request.get("main_assembly")
    if not (isinstance(main, str) and main):
        raise _identity_refusal("The original run has no registered main assembly; Freeze inputs cannot be rerun")
    purpose = request.get("purpose") if isinstance(request.get("purpose"), str) else None
    baseline = request.get("baseline")
    given = _safe_intake(str(intake_path), intake_root, context_text="originals directory supplied by this trigger")
    stated_bytes: int | None = None
    if baseline is None:
        if freeze_row.get("state") != "failed":
            raise _identity_refusal(
                "The completed freeze has no registered upload baseline; a new request cannot be created"
            )
        receipt = freeze_row.get("receipt")
        failure = receipt.get("airflow_failure") if isinstance(receipt, dict) else None
        evidence_path = failure.get("path") if isinstance(failure, dict) else None
        evidence_sha256 = failure.get("sha256") if isinstance(failure, dict) else None
        if not (
            isinstance(evidence_path, str)
            and evidence_path
            and isinstance(evidence_sha256, str)
            and _SHA256.fullmatch(evidence_sha256) is not None
        ):
            raise _identity_refusal("The freeze-failure record lacks evidence binding; Freeze inputs cannot be rerun")
        selection = _read_bound_selection(
            evidence_path,
            evidence_sha256,
            parent_dag_run_id=parent_dag_run_id,
            expected_dag_id=expected_dag_id,
        )
        evidence_intake = _safe_intake(
            selection["intake"], intake_root, context_text="originals directory in freeze-failure evidence"
        )
        if os.path.normpath(str(evidence_intake)) != os.path.normpath(str(given)):
            raise _identity_refusal(
                "The originals directory in freeze-failure evidence differs from this trigger; rerun refused"
            )
        if selection["main_assembly"] != main:
            raise _identity_refusal(
                "The main assembly in freeze-failure evidence differs from the registered one; rerun refused"
            )
        source = "parent_freeze_failure_log"
        bound_handoff = selection["handoff_sha256"]
        expected_files = selection["files"]
        folder = evidence_intake.name
        provenance = {
            "source": source,
            "path": selection["path"],
            "sha256": selection["sha256"],
            "parent_run_id": parent_run_id,
        }
    else:
        if not isinstance(baseline, dict):
            raise _identity_refusal(
                "The registered upload identity is damaged (baseline is not an object); Freeze inputs cannot be rerun"
            )
        handoff_value = baseline.get("handoff_sha256")
        files_value = baseline.get("files")
        bytes_value = baseline.get("bytes")
        folder_value = baseline.get("folder")
        if not (
            isinstance(handoff_value, str)
            and _SHA256.fullmatch(handoff_value) is not None
            and isinstance(files_value, int)
            and not isinstance(files_value, bool)
            and files_value > 0
            and isinstance(bytes_value, int)
            and not isinstance(bytes_value, bool)
            and bytes_value >= 0
            and isinstance(folder_value, str)
            and folder_value
        ):
            raise _identity_refusal(
                "The registered upload identity is damaged (missing or invalid fields); Freeze inputs cannot be rerun"
            )
        provenance = _validated_provenance(baseline["provenance"]) if "provenance" in baseline else None
        source = "baseline"
        bound_handoff = handoff_value
        expected_files = files_value
        stated_bytes = bytes_value
        folder = folder_value
    try:
        upload_files = validate_file_index(package_inventory(given))
    except (PipelineError, OSError, ValueError) as error:
        raise FreezeRetryRefusal(
            "originals_missing", "Original uploaded files are missing or unreadable; rerun refused"
        ) from error
    if isinstance(expected_files, int) and len(upload_files) != expected_files:
        raise FreezeRetryRefusal(
            "originals_missing", "Original upload file count differs from the registered identity; rerun refused"
        )
    if digest(upload_files) != bound_handoff:
        raise FreezeRetryRefusal(
            "originals_mismatch", "Original upload bytes differ from the registered identity; reuse refused"
        )
    if main not in upload_files:
        raise FreezeRetryRefusal(
            "originals_missing", "The main assembly is not in the originals inventory; rerun refused"
        )
    bytes_total = (
        stated_bytes
        if stated_bytes is not None
        else sum(entry.stat().st_size for entry in given.rglob("*") if entry.is_file())
    )
    return ResolvedFreezeIdentity(
        source=source,
        handoff_sha256=bound_handoff,
        files=len(upload_files),
        bytes=bytes_total,
        folder=folder,
        intake_path=given,
        main_assembly=main,
        purpose=purpose,
        upload_files=upload_files,
        provenance=provenance,
    )
