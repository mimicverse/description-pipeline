"""The one frozen request evidence: ``input/request.json`` (schema ``solidworks-to-urdf.request/v1``).

``freeze`` forms this record before the derivation or generator runs, and every
later stage carries its original bytes verbatim; the freeze receipt binds the
file digest.  The record binds only intake-visible facts: the authenticated
request id, the verified selected target, the LF-normalized submission summary,
the explicit main assembly, the full upload index digest and the actual platform
config snapshot identity - never secrets or credentials.  A delivery that lacks
the file is refused; there is no fallback to any earlier representation.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .io import PipelineError, canonical

REQUEST_SCHEMA = "solidworks-to-urdf.request/v1"
REQUEST_RELATIVE = "input/request.json"
REQUEST_FIELDS = (
    "schema_version",
    "request_id",
    "target_hardware",
    "change_summary",
    "main_assembly",
    "handoff_digest",
    "platform_config_digest",
)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
#: LF stays legal inside the summary; every other control character is refused.
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def normalize_change_summary(value: str) -> str:
    """Fold CRLF (and a lone CR) into LF before the evidence is frozen."""

    return value.replace("\r\n", "\n").replace("\r", "\n")


def build_request_evidence(
    *,
    request_id,
    target_hardware,
    change_summary,
    main_assembly,
    handoff_digest,
    platform_config_digest,
) -> dict:
    """The validated seven-field record, minus every secret the platform holds."""

    record = {
        "schema_version": REQUEST_SCHEMA,
        "request_id": _identifier(request_id, "request_id"),
        "target_hardware": _identifier(target_hardware, "target_hardware"),
        "change_summary": _summary(change_summary),
        "main_assembly": _identifier(main_assembly, "main_assembly"),
        "handoff_digest": _digest(handoff_digest, "handoff_digest"),
        "platform_config_digest": _digest(platform_config_digest, "platform_config_digest"),
    }
    return record


def request_evidence_bytes(record: dict) -> bytes:
    """The canonical bytes every consumer must carry verbatim."""

    _validate(record)
    return canonical(record)


def write_request_evidence(path: str | Path, record: dict) -> str:
    """Freeze the record and return the sha256 of exactly the written bytes."""

    payload = request_evidence_bytes(record)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def read_request_evidence(path: str | Path) -> tuple[dict, str]:
    """Read and validate the frozen record; a missing file is an explicit refusal."""

    source = Path(path)
    if not source.is_file():
        raise PipelineError("The delivery carries no input/request.json; the frozen request evidence is required")
    payload = source.read_bytes()
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise PipelineError("The frozen request evidence is not readable JSON") from error
    _validate(record)
    return record, hashlib.sha256(payload).hexdigest()


def verify_request_evidence(path: str | Path, *, expected_sha256: str) -> dict:
    """The receipt's binding check: the carried bytes must equal the frozen digest."""

    record, actual = read_request_evidence(path)
    if actual != expected_sha256:
        raise PipelineError("The frozen request evidence bytes do not match the receipt binding")
    return record


def _identifier(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PipelineError(f"The frozen request evidence lacks its {field}")
    text = value.strip()
    if _CONTROL.search(text):
        raise PipelineError(f"The frozen request evidence carries control characters in {field}")
    return text


def _summary(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PipelineError("The frozen request evidence lacks its change summary")
    text = normalize_change_summary(value)
    if _CONTROL.search(text):
        raise PipelineError("The frozen request evidence carries control characters in change_summary")
    return text


def _digest(value, field: str) -> str:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise PipelineError(f"The frozen request evidence lacks a sha256 {field}")
    return value


def _validate(record: dict) -> None:
    if not isinstance(record, dict) or set(record) != set(REQUEST_FIELDS):
        raise PipelineError("The frozen request evidence must carry exactly the seven schema fields")
    if record.get("schema_version") != REQUEST_SCHEMA:
        raise PipelineError(f"The frozen request evidence uses an unknown schema: {record.get('schema_version')!r}")
    rebuilt = build_request_evidence(
        request_id=record["request_id"],
        target_hardware=record["target_hardware"],
        change_summary=record["change_summary"],
        main_assembly=record["main_assembly"],
        handoff_digest=record["handoff_digest"],
        platform_config_digest=record["platform_config_digest"],
    )
    if rebuilt != record:
        raise PipelineError("The frozen request evidence is not the canonical normalized form")
