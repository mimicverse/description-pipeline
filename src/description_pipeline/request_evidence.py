"""The one frozen request evidence: ``input/request.json`` (schema ``solidworks-to-urdf.request/v2``).

``freeze`` forms this record before the derivation or generator runs, and every
later stage carries its original bytes verbatim; the freeze receipt binds the
file digest.  The record binds only intake-visible facts: the declared purpose
(the single purpose authority every stage and verifier consumes), the
authenticated request id, the verified selected target, the LF-normalized
submission summary, the explicit main assembly, the full upload index digest and
the actual platform config snapshot identity - never secrets or credentials.
Writers and readers carry the purpose explicitly and never default it; a v1
record refuses on its version field (no lenient or mixed parsing).  A delivery
that lacks the file is refused; there is no fallback to any earlier
representation.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .interface_records import REQUEST_RECORD_SCHEMA, validate_request_record
from .io import PipelineError, canonical, read_data

REQUEST_SCHEMA = REQUEST_RECORD_SCHEMA
REQUEST_RELATIVE = "input/request.json"
REQUEST_FIELDS = (
    "schema_version",
    "purpose",
    "request_id",
    "target_hardware",
    "change_summary",
    "main_assembly",
    "handoff_digest",
    "platform_config_digest",
)


def normalize_change_summary(value: str) -> str:
    """Fold CRLF (and a lone CR) into LF before the evidence is frozen."""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def build_request_evidence(
    *,
    purpose,
    request_id,
    target_hardware,
    change_summary,
    main_assembly,
    handoff_digest,
    platform_config_digest,
) -> dict:
    """The validated eight-field v2 record, minus every secret the platform holds."""

    record = {
        "schema_version": REQUEST_SCHEMA,
        "purpose": purpose.strip() if isinstance(purpose, str) else purpose,
        "request_id": request_id.strip() if isinstance(request_id, str) else request_id,
        "target_hardware": target_hardware.strip() if isinstance(target_hardware, str) else target_hardware,
        "change_summary": (
            normalize_change_summary(change_summary) if isinstance(change_summary, str) else change_summary
        ),
        "main_assembly": main_assembly.strip() if isinstance(main_assembly, str) else main_assembly,
        "handoff_digest": handoff_digest,
        "platform_config_digest": platform_config_digest,
    }
    return validate_request_record(record)


def request_evidence_bytes(record: dict) -> bytes:
    """The canonical bytes every consumer must carry verbatim."""

    validate_request_record(record)
    return canonical(record)


def write_request_evidence(path: str | Path, record: dict) -> str:
    """Freeze the record and return the sha256 of exactly the written bytes."""

    payload = request_evidence_bytes(record)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def read_request_evidence(path: str | Path) -> tuple[dict, str]:
    """Read and validate the frozen record through the shared strict parser.

    A missing file is an explicit refusal, and the shared ``io`` entry refuses
    duplicate keys, non-finite numbers and malformed JSON before this module's
    own eight-field validation runs - there is no lenient reading path.
    """

    source = Path(path)
    if not source.is_file():
        raise PipelineError("The delivery carries no input/request.json; the frozen request evidence is required")
    payload = source.read_bytes()
    try:
        record = read_data(source)
    except PipelineError as error:
        raise PipelineError("The frozen request evidence is not the canonical strict JSON record") from error
    validate_request_record(record)
    return record, hashlib.sha256(payload).hexdigest()


def freeze_request_evidence(
    run_dir: str | Path,
    *,
    purpose,
    request_id,
    target_hardware,
    change_summary,
    main_assembly,
    handoff_digest,
    platform_config_digest,
) -> Path:
    """Form ``<run>/input/request.json`` before the derivation or generator runs.

    Writing here, at freeze, is the timing contract: the frozen bytes are in
    place before any later stage exists, and every later tree carries this
    exact file (see :func:`carry_request_evidence`).  The freeze receipt binds
    the file through its artifact inventory - not through a parallel field.
    """

    record = build_request_evidence(
        purpose=purpose,
        request_id=request_id,
        target_hardware=target_hardware,
        change_summary=change_summary,
        main_assembly=main_assembly,
        handoff_digest=handoff_digest,
        platform_config_digest=platform_config_digest,
    )
    target = Path(run_dir) / REQUEST_RELATIVE
    write_request_evidence(target, record)
    return target


def carry_request_evidence(run_dir: str | Path, tree_dir: str | Path) -> Path:
    """Copy the frozen evidence into a stage tree byte-for-byte; absence refuses."""

    source = Path(run_dir) / REQUEST_RELATIVE
    record, _ = read_request_evidence(source)
    if record["schema_version"] != REQUEST_SCHEMA:  # pragma: no cover - _validate already refuses
        raise PipelineError("The frozen request evidence uses an unknown schema")
    target = Path(tree_dir) / REQUEST_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    if target.read_bytes() != source.read_bytes():  # pragma: no cover - write or refuse
        raise PipelineError("The carried request evidence differs from the frozen bytes")
    return target


