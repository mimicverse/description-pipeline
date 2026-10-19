"""Client for the bearer-authenticated Windows SolidWorks execution endpoint.

The endpoint contract is fixed and carries no shell or local paths:

* ``POST /v1/jobs`` with ``{run_id, upload_files, upload_sha256, main_assembly}`` starts one
  staging job (idempotent per run_id; a different payload for the same run_id is HTTP 409);
  an optional ``resume={parent_run, from_stage}`` inherits a parent's sealed dependency closure
  for a linked native rerun;
* ``POST /v1/jobs/<uuid>/files`` admits the one full indexed-folder archive (``application/zip``);
  the endpoint stages it provisionally, then resolves, verifies and seals the native dependency
  closure server-side;
* ``GET /v1/jobs/<uuid>`` returns status/events/result/error plus the selection block and, once
  sealed, the manifest; ``awaiting_definition`` exposes the raw-record artifact, and
  ``POST /v1/jobs/<uuid>/definition`` submits the sealed capture request carrying
  ``X-Prepared-SHA256``; ``POST /v1/jobs/<uuid>/abort`` reports one local derivation failure;
* ``POST /v1/jobs/<uuid>/cache/release`` releases one terminal run's staged cache once the
  Linux side confirms its closed imported inventory; the answer is an immutable receipt and
  refusals are ``409 cache_release_refused`` with a stable reason;
* ``GET /v1/jobs/<uuid>/artifacts/<name>`` streams one bound raw-record or native capture artifact;
* ``GET /health`` exposes only ``pipeline_id`` and readiness.

Plaintext HTTP is allowed only for loopback hosts; anything remote requires TLS.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import stat
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from ..delivery import PIPELINE_ID
from ..io import PipelineError, artifact_path_parts, digest, parse_data
from ..interface_records import (
    REQUEST_PURPOSES,
    cache_import_confirmation_digest,
    check_config_identity_file,
    require_active_config_identity,
    validate_config_snapshot,
    validate_cache_release_request,
    validate_connection_config_identity,
    validate_install_record,
)
from ..schemas import CACHE_IMPORT_CONFIRMATION_SCHEMA, CONFIG_SNAPSHOT_SCHEMA
from ..sources.solidworks.selection import validate_file_index
from ..stages import STAGE_IDS
from .selection_client import prepare_staging_archive
from .stage_transfer import CAPTURE_ARCHIVE, MAX_TRANSFER_BYTES

JOB_SCHEMA = "solidworks-to-urdf.job/v1"
#: One release ledger per run fixes the cache-release operation identity and
#: its full request, so retries resend the same operation, never new parameters.
NATIVE_RUN_NAMESPACE = "solidworks_to_urdf"
EVENT_KEYS = ("stage", "state", "at")
#: ``native_complete`` means the Windows half finished and the sealed capture archive
#: is ready for the Linux generate/verify/publish half; it is never final success.
#: ``awaiting_definition`` is the nonterminal hand-off where the native raw record
#: is available and the generated definition ZIP is still to be submitted.
RUN_STATES = {"queued", "selecting", "running", "awaiting_definition", "native_complete", "passed", "failed"}
#: Staging transport states of a ``selecting`` job (frozen contract).
SELECTION_STATES = ("awaiting_files", "reading", "sealed")
#: The fixed artifact name of the native raw-record bundle (mirrors discovery_transfer).
NATIVE_RECORD_ARCHIVE = "raw-record.zip"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PULL_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*\Z")
_REVIEW_BRANCH = re.compile(r"work/solidworks/[a-z0-9_.-]+/(?:engineering|inspection_preview)\Z")
_HARDWARE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
SUBMISSION_STATES = {"published", "updated", "noop"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _validate_activity_block(activity, label: str) -> None:
    if activity is None:
        return
    if not isinstance(activity, dict):
        raise EndpointProtocolError(f"job {label} must be an object when present")
    if activity.get("phase") not in STAGE_IDS or not isinstance(activity.get("action"), str):
        raise EndpointProtocolError(f"job {label} must carry a stage phase and an action")
    for field in ("at", "started_at", "updated_at"):
        if not isinstance(activity.get(field), str):
            raise EndpointProtocolError(f"job {label} must carry {field}")
    completed, total = activity.get("completed"), activity.get("total")
    if (completed is not None or total is not None) and (
        type(completed) is not int or type(total) is not int or completed < 0 or total < completed
    ):
        raise EndpointProtocolError(f"job {label} counts must be a non-negative completed/total pair")


def _validate_activity(job: dict) -> None:
    """The optional live-activity blocks must be shape-correct when present.

    Activity is observation, so the blocks are optional; when an endpoint sends
    them, the portal must be able to trust their shape before rendering.
    """

    _validate_activity_block(job.get("activity"), "activity")
    _validate_activity_block(job.get("activity_final"), "activity_final")
    history = job.get("activity_history")
    if history is not None and not isinstance(history, list):
        raise EndpointProtocolError("job activity_history must be a list when present")
    if isinstance(history, list):
        for entry in history:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("at"), str)
                or not isinstance(entry.get("code"), str)
            ):
                raise EndpointProtocolError("activity history entries must carry at and code")


class EndpointError(RuntimeError):
    """Transport or protocol failure while talking to the execution endpoint."""


class EndpointAuthError(EndpointError):
    """The endpoint rejected the bearer token."""


class EndpointConflict(EndpointError):
    """The run_id is already bound to a different request payload."""


class EndpointProtocolError(EndpointError):
    """The endpoint returned an unexpected shape or value."""


class EndpointNotFound(EndpointError):
    """The endpoint has no record of the requested run, job or route."""


class JobFailed(EndpointError):
    """The remote job reached the failed state."""

    def __init__(self, run_id: str, job: dict[str, Any]) -> None:
        super().__init__(f"job {run_id} failed: {job.get('error') or 'unknown error'}")
        self.run_id = run_id
        self.job = job


class ResultNotPublishable(EndpointError):
    """A passed job whose result is missing quality or PR evidence."""


def _error_payload(error: urlerror.HTTPError) -> dict:
    """The server's structured refusal body, when the endpoint provides one."""
    try:
        raw = error.read(64 * 1024)
    except OSError:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _http_refusal(error: urlerror.HTTPError, *, context: str | None = None) -> EndpointError:
    """One request refusal that keeps the server's structured body for callers."""
    payload = _error_payload(error)
    if error.code in {401, 403}:
        failure: EndpointError = EndpointAuthError(f"endpoint rejected the bearer token ({error.code})")
    elif error.code == 404:
        failure = EndpointNotFound(context or "endpoint has no record of the requested resource")
    elif error.code == 409:
        failure = EndpointConflict(f"request conflict ({error.code})")
    else:
        failure = EndpointError(f"endpoint returned HTTP {error.code}")
    message = payload.get("error")
    if isinstance(message, str) and message:
        failure.args = (f"{failure.args[0]}: {message}",)
    failure.status = error.code
    failure.payload = payload
    failure.code = payload.get("error_code") if isinstance(payload.get("error_code"), str) else None
    for field in ("error_code", "reason", "message", "earliest_required", "active_run_id", "detail"):
        setattr(failure, field, payload.get(field))
    return failure


def validate_run_id(value: str) -> str:
    try:
        canonical = str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as error:
        raise EndpointProtocolError(f"run_id is not a UUID: {value!r}") from error
    if str(value) != canonical:
        raise EndpointProtocolError(f"run_id must be the canonical lowercase UUID: {value!r}")
    return canonical


def native_run_id(dag_run_id: str) -> str:
    """The canonical native job UUID for one Airflow DAG run id (stable across retries)."""
    if not isinstance(dag_run_id, str) or not dag_run_id.strip():
        raise EndpointProtocolError("dag_run_id must be a non-empty string")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{NATIVE_RUN_NAMESPACE}:{dag_run_id}"))


def validate_resume(value: object, *, run_id: str | None = None) -> dict:
    """One linked-run binding: the parent native UUID and the canonical restart stage."""
    if not isinstance(value, dict) or set(value) != {"parent_run", "from_stage"}:
        raise EndpointProtocolError("resume must carry exactly parent_run and from_stage")
    parent = validate_run_id(value.get("parent_run"))
    stage = value.get("from_stage")
    if stage not in STAGE_IDS:
        raise EndpointProtocolError(f"from_stage is not a canonical engineering stage: {stage!r}")
    if run_id is not None and parent == run_id:
        raise EndpointProtocolError("a linked run cannot reuse itself")
    return {"parent_run": parent, "from_stage": stage}


def _validate_capture_archive(archive) -> dict:
    """The exact native capture-archive receipt the sealing contract produces."""
    expected = {"name", "sha256", "size", "manifest_sha256"}
    if not isinstance(archive, dict) or set(archive) != expected:
        raise EndpointProtocolError("capture_archive must carry exactly name, sha256, size and manifest_sha256")
    if archive.get("name") != CAPTURE_ARCHIVE:
        raise EndpointProtocolError("capture_archive name differs from the transfer contract")
    for key in ("sha256", "manifest_sha256"):
        if not isinstance(archive.get(key), str) or _SHA256.fullmatch(archive[key]) is None:
            raise EndpointProtocolError(f"capture_archive {key} must be lowercase SHA-256")
    size = archive.get("size")
    if type(size) is not int or size <= 0 or size > MAX_TRANSFER_BYTES:
        raise EndpointProtocolError("capture_archive size must be a positive integer within the transfer bound")
    return dict(archive)


def capture_archive_metadata(job: dict) -> dict:
    """The validated capture-archive receipt of one ``native_complete`` job."""
    if not isinstance(job, dict) or job.get("status") != "native_complete":
        raise EndpointProtocolError("the job is not native_complete")
    result = job.get("result")
    if not isinstance(result, dict) or result.get("native_complete") is not True:
        raise EndpointProtocolError("native_complete result must declare native_complete = true")
    return _validate_capture_archive(result.get("capture_archive"))


def _validate_relative_path(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise EndpointProtocolError(f"{field} must be a POSIX relative path")
    try:
        artifact_path_parts(value)
    except PipelineError as error:
        raise EndpointProtocolError(f"{field} must be a portable relative path") from error
    path = PurePosixPath(value)
    if path.is_absolute() or any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise EndpointProtocolError(f"{field} must stay inside the delivery: {value!r}")
    return path.as_posix()


#: One explicit delivered-assembly selection shares the member-path bound.
MAX_MAIN_ASSEMBLY = 1024


def validate_main_assembly(value: object) -> str:
    """One explicit delivered assembly: canonical POSIX path inside the handoff root.

    The value is the operator's authoritative entry selection (the top engineering
    folder is excluded). It names a saved SolidWorks assembly and must match a
    frozen file exactly; the endpoint re-verifies that before any CAD runs.
    """

    if not isinstance(value, str) or not value or len(value) > MAX_MAIN_ASSEMBLY:
        raise EndpointProtocolError("main_assembly must be a non-empty relative path")
    if value != value.strip():
        raise EndpointProtocolError("main_assembly must not carry leading or trailing whitespace")
    if unicodedata.normalize("NFC", value) != value:
        raise EndpointProtocolError("main_assembly must be NFC-normalized")
    _validate_relative_path(value, "main_assembly")
    if not value.casefold().endswith(".sldasm"):
        raise EndpointProtocolError("main_assembly must name a saved SolidWorks assembly (.SLDASM)")
    return value


def normalize_change_summary(value: str) -> str:
    """Normalize a change summary's line endings to LF before it is recorded.

    The discovery contract admits U+000A inside a change summary and still rejects every
    other control character, so the portal and the DAG intake fold CRLF (and a lone CR)
    into LF instead of forwarding a carriage return into the records.
    """

    return value.replace("\r\n", "\n").replace("\r", "\n")


def submission_identity(
    *,
    target_hardware,
    change_summary=None,
    principal=None,
    purpose,
    config: EndpointConfig,
    published_revision=None,
    warn=None,
) -> dict | None:
    """Build the validated submission identity carried beside the endpoint request.

    Returns ``None`` when no target was selected (today's deferred behavior).
    An unconfigured target fails closed; the previous published revision is read
    best-effort through the supplied reader (any failure only warns and leaves
    ``parent_revision`` absent).
    """

    target = str(target_hardware or "").strip()
    summary = str(change_summary or "").strip()
    if not target:
        return None
    declared_purpose = str(purpose or "").strip()
    if declared_purpose not in REQUEST_PURPOSES:
        raise EndpointProtocolError(f"purpose {declared_purpose!r} is not a declared submission purpose")
    routing = config.targets.get(target)
    if routing is None:
        raise EndpointProtocolError(f"target_hardware {target!r} is not a configured publication target")
    parent = None
    repository = config.repositories.get(str(routing["repository_slug"]))
    if repository is not None and published_revision is not None:
        try:
            previous = published_revision(repository, target, declared_purpose)
        except Exception as error:  # noqa: BLE001 - identity derivation stays best-effort
            if warn is not None:
                warn(f"previous revision unavailable for {target}: {error}")
        else:
            if isinstance(previous, dict) and isinstance(previous.get("revision"), str):
                parent = previous["revision"]
    owner = principal if isinstance(principal, str) and principal.strip() else None
    from ..interface_records import RESOURCE_POLICY_V1, resource_policy_v1_digest

    return {
        "hardware_id": target,
        "owner": owner,
        "change_summary": summary or None,
        "parent_revision": parent,
        "resource_policy": {
            "schema_version": RESOURCE_POLICY_V1["schema_version"],
            "digest": resource_policy_v1_digest(),
        },
    }


def validate_artifact_name(value: str) -> str:
    return _validate_relative_path(value, "artifact name")


def _validate_digest(value: str, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EndpointProtocolError(f"{field} must be a lowercase SHA-256 digest")
    return value


def validate_sha256(value: str) -> str:
    return _validate_digest(value, "sha256")


def validate_handoff_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EndpointProtocolError("handoff_path must be a non-empty path")
    path = value.strip()
    if _CONTROL.search(path):
        raise EndpointProtocolError("handoff_path must not contain control characters")
    return path


def cache_release_manifest(intake: Path, upload_files: dict[str, str]) -> list[dict]:
    """The closed staged-cache inventory of one frozen upload (path, bytes, SHA-256).

    The endpoint compares this inventory against the bytes it actually staged,
    so it is built from the same files the portal uploaded, byte for byte.
    """

    entries: list[dict] = []
    for relative in sorted(upload_files):
        path = Path(intake).joinpath(*artifact_path_parts(relative))
        if path.is_symlink() or not path.is_file():
            raise PipelineError(f"The uploaded handoff item is missing: {relative!r}")
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if checksum != upload_files[relative]:
            raise PipelineError(f"The uploaded handoff changed after its index was recorded: {relative!r}")
        entries.append({"path": relative, "bytes": path.stat().st_size, "sha256": checksum})
    return entries


def cache_release_request(
    *,
    run_id: str,
    linux_run_id: str,
    production_operation_id: str,
    operation_id: str,
    health: dict,
    imported: list[dict],
    sealed_receipt_sha256: str,
    import_block: dict | None = None,
) -> dict:
    """One frozen IF-10 release request, bound to the endpoint identity read live.

    Both binding blocks come from the endpoint's own ``/health`` answer (never
    hard-coded): the release triple and the contract digest the caller must
    bind.  ``sealed_receipt_sha256`` identifies the Linux sealed import
    confirmation (the admitted capture manifest digest); the endpoint keeps it
    opaque.  ``import_block`` reuses one already-frozen import block, so the
    registered parameter digest and the executed request bind the same
    confirmation bytes.  The request validates against the frozen record layer
    before it leaves the Linux side.
    """

    release = health.get("release") if isinstance(health.get("release"), dict) else None
    contract = health.get("contract") if isinstance(health.get("contract"), dict) else None
    if release is None or contract is None:
        raise EndpointProtocolError("the endpoint health carries no release identity or contract binding")
    if import_block is None:
        production = {"run_id": validate_run_id(run_id), "operation_id": production_operation_id}
        confirmation = {
            "schema_version": CACHE_IMPORT_CONFIRMATION_SCHEMA,
            "imported": [dict(entry) for entry in imported],
            "production": dict(production),
            "sealed": {
                "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "receipt_sha256": sealed_receipt_sha256,
            },
            "digest": "",
        }
        confirmation["digest"] = cache_import_confirmation_digest(confirmation)
        import_block = {
            "linux_run_id": linux_run_id,
            "production": dict(production),
            "confirmation": confirmation,
        }
    request = {
        "operation_id": operation_id,
        "binding": {
            "release": {key: release[key] for key in ("version", "source_commit", "source_sha256")},
            "contract": dict(contract),
        },
        "import": dict(import_block),
    }
    validate_cache_release_request(request)
    return request


def _validate_base_url(url: str) -> str:
    try:
        parsed = urlparse.urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise EndpointProtocolError("Invalid endpoint address") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EndpointProtocolError("Endpoint URL must use absolute HTTP or HTTPS")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK:
        raise EndpointProtocolError(
            "plaintext HTTP is only allowed for loopback; use TLS or an SSH tunnel for remote endpoints"
        )
    if parsed.username or parsed.password:
        raise EndpointProtocolError("credentials must not be embedded in the endpoint URL")
    if parsed.query or parsed.fragment:
        raise EndpointProtocolError("Endpoint URL cannot contain a query or fragment")
    return url.rstrip("/")


@dataclass(frozen=True)
class EndpointConfig:
    base_url: str
    token: str
    timeout: float = 30.0
    handoff_roots: tuple[Path, ...] = ()
    #: Linux-side routing: model-repository slug to local checkout, and the attempt store.
    repositories: dict[str, Path] = dataclass_field(default_factory=dict)
    store_root: Path | None = None
    #: Linux-side derivation controls: controlled-record roots and the frozen-name registry.
    discovery: dict = dataclass_field(default_factory=dict)
    #: Configured publication targets: hardware identity to repository slug and base branch.
    targets: dict[str, dict] = dataclass_field(default_factory=dict)
    #: Frozen IF-16 snapshot digest that produced this runtime configuration.
    config_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validate_base_url(self.base_url))
        if not self.token:
            raise EndpointProtocolError("a bearer token is required")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise EndpointProtocolError("Endpoint timeout must be finite and positive")
        object.__setattr__(self, "handoff_roots", _handoff_roots(self.handoff_roots))
        object.__setattr__(self, "repositories", _linux_repositories(self.repositories))
        object.__setattr__(self, "store_root", _linux_store_root(self.store_root))
        object.__setattr__(self, "discovery", _linux_discovery(self.discovery))
        object.__setattr__(self, "targets", _linux_targets(self.targets))
        if self.config_digest and _SHA256.fullmatch(self.config_digest) is None:
            raise EndpointProtocolError("config_digest must be a lowercase sha256 hex value")


def _handoff_roots(values) -> tuple[Path, ...]:
    """Validate the platform allowlist with the shared handoff boundary rules."""
    if values is None or values == () or values == "":
        return ()
    try:
        from ..sources.solidworks.handoff import validate_handoff_roots

        return validate_handoff_roots(values)
    except (ImportError, PipelineError, TypeError, ValueError, OSError) as error:
        raise EndpointProtocolError(str(error)) from error


_REPOSITORY_SLUG = re.compile(r"[^/\s]+/[^/\s]+\Z")


def _linux_repositories(values) -> dict[str, Path]:
    """Validate the Linux slug-to-checkout map used to publish without Windows."""
    if values in (None, "", {}):
        return {}
    if not isinstance(values, dict):
        raise EndpointProtocolError("repositories must be a slug-to-absolute-path mapping")
    resolved: dict[str, Path] = {}
    for slug, path in values.items():
        if not isinstance(slug, str) or _REPOSITORY_SLUG.fullmatch(slug) is None:
            raise EndpointProtocolError(f"repository slug must be owner/name: {slug!r}")
        candidate = Path(str(path))
        if not candidate.is_absolute():
            raise EndpointProtocolError(f"repository path for {slug!r} must be absolute")
        resolved[slug] = candidate
    return resolved


def _linux_store_root(value) -> Path | None:
    """Validate the absolute Linux attempt-store root; absent disables the portable half."""
    if value in (None, ""):
        return None
    candidate = Path(str(value))
    if not candidate.is_absolute():
        raise EndpointProtocolError("store_root must be an absolute Linux path")
    return candidate


_TARGET_HARDWARE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_TARGET_BASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


def _linux_targets(values) -> dict[str, dict]:
    """Validate the hardware-to-publication-target map used for identity resolution."""

    if values in (None, "", {}):
        return {}
    if not isinstance(values, dict):
        raise EndpointProtocolError("targets must map hardware identities to repository routing")
    resolved: dict[str, dict] = {}
    for hardware, target in values.items():
        if not isinstance(hardware, str) or _TARGET_HARDWARE.fullmatch(hardware) is None:
            raise EndpointProtocolError(f"target hardware identity must be an ASCII identifier: {hardware!r}")
        if not isinstance(target, dict) or set(target) != {"repository_slug", "base"}:
            raise EndpointProtocolError(f"target {hardware!r} must carry exactly repository_slug and base")
        slug, base = target["repository_slug"], target["base"]
        if not isinstance(slug, str) or _REPOSITORY_SLUG.fullmatch(slug) is None:
            raise EndpointProtocolError(f"target {hardware!r} repository slug must be owner/name")
        if (
            not isinstance(base, str)
            or _TARGET_BASE.fullmatch(base) is None
            or ".." in base
            or base.endswith(".lock")
            or base.startswith("-")
        ):
            raise EndpointProtocolError(f"target {hardware!r} base must be a well-formed git branch")
        resolved[hardware] = {"repository_slug": slug, "base": base}
    return resolved


def _discovery_path(value, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise EndpointProtocolError(f"discovery {label} must be an absolute path")
    text = str(value)
    candidate = Path(text)
    if not candidate.is_absolute():
        raise EndpointProtocolError(f"discovery {label} must be an absolute path")
    # The same dedicated-directory rule the handoff roots carry, judged on the RAW spelling:
    # ``Path`` collapses ``//`` before any parts check, so the segment check must see the
    # original text.  A filesystem root, a ``..`` segment or an empty segment (``a//b``,
    # ``/a/``) refuses; the text is never silently rewritten (no ``resolve()``).
    if any(segment in {"..", ""} for segment in text[1:].split("/")):
        raise EndpointProtocolError(f"discovery {label} must be a dedicated absolute path: {value}")
    if candidate.is_symlink() or candidate.is_junction():
        raise EndpointProtocolError(f"discovery {label} must not be a link")
    return candidate


def _linux_discovery(values) -> dict:
    """Validate the Linux derivation settings: controlled-record roots and the registry."""
    if values in (None, "", {}):
        return {}
    if not isinstance(values, dict) or not set(values) <= {"record_roots", "frozen_names_file"}:
        raise EndpointProtocolError("discovery must carry only record_roots and frozen_names_file")
    settings: dict = {}
    roots = values.get("record_roots", ())
    if not isinstance(roots, (list, tuple)):
        raise EndpointProtocolError("discovery record_roots must be a list of absolute directories")
    checked = []
    for value in roots:
        candidate = _discovery_path(value, "record root")
        if not candidate.is_dir():
            raise EndpointProtocolError(f"discovery record root is not a directory: {candidate}")
        checked.append(candidate)
    settings["record_roots"] = tuple(checked)
    if "frozen_names_file" in values:
        registry = _discovery_path(values["frozen_names_file"], "frozen-name registry")
        if not registry.is_file():
            raise EndpointProtocolError(f"discovery frozen-name registry is not a file: {registry}")
        settings["frozen_names_file"] = registry
    return settings


def _airflow_connection_document(connection) -> dict:
    """The exact IF-11 document consumed from one Airflow connection row."""

    return {
        "host": connection.host or "",
        "port": connection.port,
        "password": connection.password or "",
        "extra": dict(connection.extra_dejson or {}),
    }


def _require_private_text_file(path, *, label: str) -> str:
    """Read one configured 0600 regular file without following a final symlink."""

    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
        raise EndpointProtocolError(f"{label} must be an absolute regular non-link file")
    if stat.S_IMODE(candidate.stat().st_mode) != 0o600:
        raise EndpointProtocolError(f"{label} must have mode 0600")
    try:
        value = candidate.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise EndpointProtocolError(f"{label} is unreadable") from error
    if not value:
        raise EndpointProtocolError(f"{label} is empty")
    return value


def _runtime_state_root(state_root: Path | None) -> Path:
    """Resolve the one permitted state root from controlled configuration only."""

    if state_root is None:
        configured = os.environ.get("OPERATOR_STATE", "").strip()
        if not configured:
            raise EndpointProtocolError("OPERATOR_STATE or an explicit state_root is required")
        state_root = Path(configured)
    root = Path(state_root)
    if not root.is_absolute():
        raise EndpointProtocolError("state_root must be an absolute path")
    return root


def _read_install_record(state_root: Path) -> dict:
    path = state_root / "install-record.json"
    if not path.is_file():
        raise EndpointProtocolError(f"active install record is missing: {path}")
    try:
        return validate_install_record(parse_data(path.read_bytes(), path))
    except (OSError, PipelineError, ValueError) as error:
        raise EndpointProtocolError(f"active install record is invalid: {error}") from error


def _frozen_snapshot_reference(state_root: Path, config_digest: str) -> dict:
    """Resolve one already-admitted digest without consulting the current active pointer."""

    if _SHA256.fullmatch(config_digest) is None:
        raise EndpointProtocolError("frozen config digest must be a lowercase sha256 hex value")
    snapshots = state_root / "snapshots"
    matches = []
    for candidate in sorted(snapshots.glob("*.json")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            continue
        if actual == config_digest:
            matches.append(candidate)
    if len(matches) != 1:
        raise EndpointProtocolError(f"frozen config digest {config_digest} does not identify exactly one snapshot")
    return {
        "schema_version": CONFIG_SNAPSHOT_SCHEMA,
        "path": str(matches[0]),
        "sha256": config_digest,
    }


def _endpoint_config_from_snapshot(snapshot: dict, *, config_digest: str, timeout: float) -> EndpointConfig:
    """Map one immutable config snapshot to the runtime endpoint configuration."""

    resolved = snapshot["resolved"]
    token = _require_private_text_file(resolved["windows"]["token_file"], label="endpoint token file")
    return EndpointConfig(
        base_url=f"http://127.0.0.1:{resolved['windows']['tunnel_loopback_port']}",
        token=token,
        timeout=timeout,
        handoff_roots=(resolved["storage"]["handoff_root"],),
        repositories=resolved["derived"]["repositories"],
        store_root=resolved["derived"]["store_root"],
        discovery=resolved["derived"]["discovery"],
        targets=resolved["targets"],
        config_digest=config_digest,
    )


def config_from_airflow_connection(
    conn_id: str,
    *,
    timeout: float = 30.0,
    config_digest: str | None = None,
    state_root: Path | None = None,
) -> EndpointConfig:
    """Resolve the active or frozen immutable snapshot through the Airflow connection.

    A fresh admission (no ``config_digest``) must reference the exact active install
    snapshot. A frozen request supplies its recorded digest and continues to resolve
    that snapshot even after the active pointer moves. The Airflow row carries only
    the reference; endpoint, routing, token-file and target values come from the
    validated snapshot.
    """
    root = _runtime_state_root(state_root)
    if config_digest is None:
        from airflow.models.connection import Connection
        from airflow.settings import Session

        with Session() as session:
            connection = session.query(Connection).filter(Connection.conn_id == conn_id).one_or_none()
        if connection is None:
            raise EndpointProtocolError(f"Airflow connection {conn_id!r} is missing")
        try:
            reference = validate_connection_config_identity(_airflow_connection_document(connection))
        except PipelineError as error:
            raise EndpointProtocolError(f"Airflow connection config identity is invalid: {error}") from error
        record = _read_install_record(root)
        if record["paths"].get("state_root") != str(root):
            raise EndpointProtocolError("active install record belongs to another state root")
        if reference["path"] != record["snapshot"]["path"] or reference["sha256"] != record["snapshot"]["digest"]:
            raise EndpointProtocolError("Airflow connection does not reference the active config snapshot")
        try:
            require_active_config_identity(reference, record)
        except PipelineError as error:
            raise EndpointProtocolError(f"Airflow connection is not admitted for new work: {error}") from error
        selected = dict(reference)
    else:
        selected = _frozen_snapshot_reference(root, config_digest)
    try:
        payload = check_config_identity_file(selected, allowed_root=str(root))
        if hashlib.sha256(payload).hexdigest() != selected["sha256"]:
            raise EndpointProtocolError("config snapshot digest does not match its reference")
        snapshot = parse_data(payload, Path(selected["path"]))
        validate_config_snapshot(snapshot)
    except PipelineError as error:
        detail = getattr(error, "detail", None)
        suffix = f" {detail}" if detail else ""
        raise EndpointProtocolError(f"config snapshot is invalid: {error}{suffix}") from error
    if snapshot["resolved"]["storage"]["state_root"] != str(root):
        raise EndpointProtocolError("config snapshot belongs to another state root")
    return _endpoint_config_from_snapshot(
        snapshot,
        config_digest=selected["sha256"],
        timeout=float(timeout),
    )


def config_reference_cli(argv=None) -> int:
    """Health/operator entry for the active configuration reference."""

    import argparse

    parser = argparse.ArgumentParser(prog="python -m description_pipeline.orchestration.airflow_client")
    parser.add_argument("--conn-id", default="solidworks_windows")
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args(argv)
    config = config_from_airflow_connection(args.conn_id, state_root=args.state_root)
    print(f"config_digest={config.config_digest}")
    return 0


@dataclass(frozen=True, slots=True)
class SelectionSnapshot:
    """Validated selection block of one endpoint job snapshot."""

    state: str
    reader_sha256: str
    upload_sha256: str
    staged_files: dict[str, str]
    manifest: dict | None = None


def _selection_files(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(not isinstance(name, str) for name in value):
        raise EndpointProtocolError(f"selection {label} must be a path->sha256 object")
    files: dict[str, str] = {}
    for name, checksum in value.items():
        try:
            parts = artifact_path_parts(name)
        except PipelineError as error:
            raise EndpointProtocolError(f"selection {label} has an inadmissible path: {name!r}") from error
        if "/".join(parts) != name:
            raise EndpointProtocolError(f"selection {label} has a noncanonical path: {name!r}")
        if not isinstance(checksum, str) or _SHA256.fullmatch(checksum) is None:
            raise EndpointProtocolError(f"selection {label} entry {name!r} has no lowercase sha256")
        files[name] = checksum
    return files


def _selection_block(
    job: dict, *, upload_files: dict[str, str] | None = None, initial: bool = False
) -> SelectionSnapshot:
    """Parse and validate one job's staging block against the frozen contract."""

    tool = job.get("native_tool")
    if not isinstance(tool, dict):
        raise EndpointProtocolError("selection job carries no native tool record")
    reader = validate_sha256(tool.get("source_sha256"))
    block = job.get("selection")
    if not isinstance(block, dict):
        raise EndpointProtocolError("staging job carries no selection block")
    state = block.get("state")
    if state not in SELECTION_STATES:
        raise EndpointProtocolError(f"unexpected staging state: {state!r}")
    expected_keys = {"state", "reader_sha256", "upload_sha256", "staged_files"}
    if "manifest" in block:
        expected_keys.add("manifest")
    if set(block) != expected_keys:
        raise EndpointProtocolError("the staging block carries unexpected fields")
    if validate_sha256(block.get("reader_sha256")) != reader:
        raise EndpointProtocolError("staging reader identity differs from the native tool record")
    upload_sha256 = validate_sha256(block.get("upload_sha256"))
    staged = _selection_files(block.get("staged_files"), "staged_files")
    manifest = block.get("manifest")
    if manifest is not None and (not isinstance(manifest, dict) or not manifest):
        raise EndpointProtocolError("selection manifest must be a non-empty object")
    if state == "sealed" and manifest is None:
        raise EndpointProtocolError("a sealed selection must carry its manifest")
    if upload_files is not None:
        if upload_sha256 != digest(upload_files):
            raise EndpointProtocolError("staging upload digest differs from the upload index")
        for name, checksum in staged.items():
            if upload_files.get(name) != checksum:
                raise EndpointProtocolError(f"staging names a file outside the upload index: {name!r}")
        if state == "sealed" and staged != dict(upload_files):
            raise EndpointProtocolError("a sealed staging must hold the full upload index")
    if state == "awaiting_files" and (staged or manifest is not None):
        raise EndpointProtocolError("an awaiting staging carries no staged bytes")
    if initial and (state != "awaiting_files" or staged or manifest is not None):
        raise EndpointProtocolError("staging must start from an empty awaiting stage")
    return SelectionSnapshot(
        state=state,
        reader_sha256=reader,
        upload_sha256=upload_sha256,
        staged_files=staged,
        manifest=manifest,
    )


@dataclass(frozen=True, slots=True)
class AwaitingDefinition:
    """Validated ``awaiting_definition`` snapshot: raw-record receipt and sealed selection."""

    record_name: str
    record_sha256: str
    record_size: int
    record_manifest: dict
    native_tool: dict
    selection: SelectionSnapshot


def _native_record_block(record: object) -> dict:
    """The flat raw-record artifact receipt the frozen transport contract declares."""

    expected = {"name", "sha256", "size", "manifest_sha256", "manifest"}
    if not isinstance(record, dict) or set(record) != expected:
        raise EndpointProtocolError("native_record must carry exactly name, sha256, size, manifest_sha256 and manifest")
    name = validate_artifact_name(record.get("name"))
    if name != NATIVE_RECORD_ARCHIVE:
        raise EndpointProtocolError("native_record name differs from the transport contract")
    checksum = validate_sha256(record.get("sha256"))
    manifest_sha256 = validate_sha256(record.get("manifest_sha256"))
    size = record.get("size")
    if type(size) is not int or size <= 0 or size > MAX_TRANSFER_BYTES:
        raise EndpointProtocolError("native_record size must be a positive integer within the transfer bound")
    manifest = record.get("manifest")
    if not isinstance(manifest, dict) or not manifest:
        raise EndpointProtocolError("native_record carries no bundle manifest")
    if digest(manifest) != manifest_sha256:
        raise EndpointProtocolError("native_record manifest does not match its recorded digest")
    return {
        "name": name,
        "sha256": checksum,
        "size": size,
        "manifest_sha256": manifest_sha256,
        "manifest": manifest,
    }


def _record_block(record: AwaitingDefinition | dict) -> dict:
    """The validated flat raw-record receipt, from either carrier form."""

    if isinstance(record, AwaitingDefinition):
        record = {
            "name": record.record_name,
            "sha256": record.record_sha256,
            "size": record.record_size,
            "manifest_sha256": digest(record.record_manifest),
            "manifest": record.record_manifest,
        }
    return _native_record_block(record)


def _validate_failure_diagnostics(job: dict) -> dict | None:
    """The hash-bound raw failure-diagnostics receipt of one job, or None when absent."""

    value = job.get("failure_diagnostics")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"path", "sha256"} or value.get("path") != "diagnostic.json":
        raise EndpointProtocolError("failure_diagnostics must name diagnostic.json with its sha256")
    return {"path": "diagnostic.json", "sha256": validate_sha256(value.get("sha256"))}


def _sealed_diagnostic_member(relative: object) -> str | None:
    """One allowlisted sealed member name, or None when it is unsafe or unknown."""

    from ..structural_report import is_sealed_diagnostic_member

    if not isinstance(relative, str) or not relative or "\\" in relative or relative.startswith("/"):
        return None
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        return None
    return relative if is_sealed_diagnostic_member(relative) else None


def _awaiting_definition(job: dict, *, upload_files: dict[str, str] | None = None) -> AwaitingDefinition:
    """Parse and validate one job's definition hand-off against the frozen contract."""

    if not isinstance(job, dict) or job.get("status") != "awaiting_definition":
        raise EndpointProtocolError("the job does not await a definition")
    record = _native_record_block(job.get("native_record"))
    tool = job.get("native_tool")
    if not isinstance(tool, dict) or not tool:
        raise EndpointProtocolError("definition job carries no native tool record")
    selection = _selection_block(job, upload_files=upload_files)
    if selection.state != "sealed" or selection.manifest is None:
        raise EndpointProtocolError("the definition job carries no sealed selection manifest")
    return AwaitingDefinition(
        record_name=record["name"],
        record_sha256=record["sha256"],
        record_size=record["size"],
        record_manifest=record["manifest"],
        native_tool=tool,
        selection=selection,
    )


class WindowsEndpoint:
    """Small JSON client; injectable opener keeps it testable without network."""

    def __init__(
        self,
        config: EndpointConfig,
        *,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._opener = opener or _opener
        self._sleep = sleeper
        self._clock = clock

    def _exchange(self, request: urlrequest.Request) -> dict:
        try:
            with self._opener(request, timeout=self.config.timeout) as response:
                data = json.loads(response.read().decode("utf-8") or "{}")
        except urlerror.HTTPError as error:
            raise _http_refusal(error) from error
        except urlerror.URLError as error:
            raise EndpointError(f"endpoint unreachable: {error.reason}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EndpointProtocolError("Endpoint returned invalid UTF-8 JSON") from error
        if not isinstance(data, dict):
            raise EndpointProtocolError("endpoint response is not a JSON object")
        return data

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        return self._exchange(
            urlrequest.Request(
                self.config.base_url + path,
                data=body,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.config.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        )

    def _request_stream(
        self, method: str, path: str, source: Any, *, size: int, headers: dict[str, str] | None = None
    ) -> dict:
        """Stream one file body with a fixed Content-Length (no chunked upload)."""
        return self._exchange(
            urlrequest.Request(
                self.config.base_url + path,
                data=source,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.config.token}",
                    "Content-Type": "application/zip",
                    "Accept": "application/json",
                    "Content-Length": str(size),
                    **(headers or {}),
                },
            )
        )

    def _authorized_handoff_dir(self, path: str) -> Path:
        """Authorize one absolute Linux folder against the platform handoff_roots allowlist."""
        if not self.config.handoff_roots:
            raise EndpointProtocolError(
                "absolute Linux handoff folders require handoff_roots in the Airflow endpoint connection"
            )
        try:
            from ..sources.solidworks.handoff import authorize_handoff

            return authorize_handoff(Path(path), self.config.handoff_roots)
        except (ImportError, PipelineError, TypeError, ValueError, OSError) as error:
            raise EndpointProtocolError(str(error)) from error

    def health(self) -> dict:
        payload = self._request("GET", "/health")
        if payload.get("pipeline_id") != PIPELINE_ID:
            raise EndpointProtocolError(f"unexpected pipeline_id: {payload.get('pipeline_id')!r}")
        if "ready" not in payload and "readiness" not in payload:
            raise EndpointProtocolError("health response must include readiness")
        return payload

    def authorized_intake_dir(self, handoff_path: str) -> Path:
        """The local sealed intake folder for one operator path, inside ``handoff_roots``."""

        return self._authorized_handoff_dir(validate_handoff_path(handoff_path))

    def start_staging_job(
        self,
        *,
        run_id: str,
        upload_files: dict[str, str],
        main_assembly: str,
        resume: dict | None = None,
    ) -> dict:
        """Start one staging job: a fresh full-folder stage, or a linked rerun inheriting a sealed parent."""

        validated = validate_file_index(upload_files)
        main = validate_main_assembly(main_assembly)
        if main not in validated:
            raise EndpointProtocolError("the main assembly is not part of the upload index")
        payload = {
            "run_id": validate_run_id(run_id),
            "upload_files": dict(validated),
            "upload_sha256": digest(validated),
            "main_assembly": main,
        }
        if resume is not None:
            payload["resume"] = validate_resume(resume, run_id=payload["run_id"])
        response = self._request("POST", "/v1/jobs", payload)
        if response.get("run_id") != payload["run_id"] or response.get("request") != payload:
            raise EndpointProtocolError("endpoint accepted a different staging request")
        if response.get("status") not in RUN_STATES:
            raise EndpointProtocolError(f"unexpected job status: {response.get('status')!r}")
        snapshot = _selection_block(response, upload_files=validated, initial=resume is None)
        if resume is None:
            return response
        if snapshot.state != "sealed" or snapshot.manifest is None:
            raise EndpointProtocolError("a linked rerun must inherit the parent's sealed closure")
        if snapshot.manifest.get("main_assembly") != main:
            raise EndpointProtocolError("the inherited closure is bound to a different main assembly")
        sealed_files = snapshot.manifest.get("files")
        if not isinstance(sealed_files, dict) or not sealed_files:
            raise EndpointProtocolError("the inherited closure carries no resolved files")
        for name, checksum in sealed_files.items():
            if validated.get(name) != checksum:
                raise EndpointProtocolError(f"the inherited closure names a file outside the upload index: {name!r}")
        return response

    def staging_snapshot(self, run_id: str, *, upload_files: dict[str, str] | None = None) -> SelectionSnapshot:
        """Fetch and validate the staging block; never trusts a partial contract."""

        job = self.get_job(run_id)
        validated = None if upload_files is None else validate_file_index(upload_files)
        return _selection_block(job, upload_files=validated)

    def stage_folder_archive(self, run_id: str, archive: Path, *, upload_files: dict[str, str]) -> dict:
        """Push the one exact full-index folder archive; the endpoint stages it before resolving."""

        validated = validate_file_index(upload_files)
        archive = Path(archive)
        size = archive.stat().st_size
        if not 0 < size <= MAX_TRANSFER_BYTES:
            raise EndpointProtocolError("staging archive has an invalid size")
        with archive.open("rb") as handle:
            response = self._request_stream(
                "POST",
                f"/v1/jobs/{validate_run_id(run_id)}/files",
                handle,
                size=size,
            )
        snapshot = _selection_block(response, upload_files=validated)
        if snapshot.staged_files != validated:
            raise EndpointProtocolError("endpoint did not stage the full indexed folder")
        return response

    def definition_snapshot(self, run_id: str, *, upload_files: dict[str, str] | None = None) -> AwaitingDefinition:
        """Fetch and validate the definition hand-off; never trusts a partial contract."""

        job = self.get_job(run_id)
        validated = None if upload_files is None else validate_file_index(upload_files)
        return _awaiting_definition(job, upload_files=validated)

    def stream_native_record(
        self, run_id: str, record: AwaitingDefinition | dict, destination: Path, *, limit: int = MAX_TRANSFER_BYTES
    ) -> Path:
        """Stream the raw-record artifact to disk, requiring its exact size and digest."""

        metadata = _record_block(record)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise EndpointProtocolError("record size limit must be a positive integer")
        if metadata["size"] > limit:
            raise EndpointProtocolError("native record exceeds the configured size limit")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        total = 0
        with (
            self.open_artifact(run_id, metadata["name"]) as response,
            tempfile.NamedTemporaryFile(prefix=".native-record-", dir=destination.parent, delete=False) as handle,
        ):
            temporary = Path(handle.name)
            try:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise EndpointProtocolError("native record exceeds the configured size limit")
                    hasher.update(chunk)
                    handle.write(chunk)
            except BaseException:
                with suppress(OSError):
                    temporary.unlink()
                raise
        if total != metadata["size"] or hasher.hexdigest() != metadata["sha256"]:
            with suppress(OSError):
                temporary.unlink()
            raise EndpointProtocolError("native record does not match its recorded size or digest")
        os.replace(temporary, destination)
        return destination

    def post_definition_archive(self, run_id: str, archive: Path, *, prepared_sha256: str) -> dict:
        """Submit the sealed capture request; the endpoint rebuilds the prepared package."""

        prepared = validate_sha256(prepared_sha256)
        archive = Path(archive)
        size = archive.stat().st_size
        if not 0 < size <= MAX_TRANSFER_BYTES:
            raise EndpointProtocolError("definition archive has an invalid size")
        with archive.open("rb") as handle:
            response = self._request_stream(
                "POST",
                f"/v1/jobs/{validate_run_id(run_id)}/definition",
                handle,
                size=size,
                headers={"X-Prepared-SHA256": prepared},
            )
        if not isinstance(response, dict):
            raise EndpointProtocolError("definition acknowledgement must be a job object")
        if response.get("run_id", run_id) != run_id:
            raise EndpointProtocolError("endpoint acknowledged a different definition run")
        if "status" in response and response["status"] not in RUN_STATES:
            raise EndpointProtocolError(f"unexpected job status: {response.get('status')!r}")
        return response

    def download_failure_diagnostics(
        self, run_id: str, job: dict, destination: Path, *, limit: int = MAX_TRANSFER_BYTES
    ) -> dict:
        """Stream the hash-bound raw failure diagnostics of one failed job.

        The bundle carries the exact sealed receipt and supporting evidence files
        the Linux side renders the structural-team report from; the native host
        never writes the report itself.  ``limit`` bounds the whole bundle,
        manifest included.
        """

        from ..structural_report import DIAGNOSTIC_SCHEMA

        receipt = _validate_failure_diagnostics(job)
        if receipt is None:
            raise EndpointNotFound("this run carries no failure diagnostics")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise EndpointProtocolError("diagnostics size limit must be a positive integer")
        canonical = validate_run_id(run_id)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        manifest_bytes = self._read_artifact(canonical, receipt["path"], receipt["sha256"], limit=min(limit, 1 << 22))
        try:
            manifest = json.loads(manifest_bytes)
        except ValueError as error:
            raise EndpointProtocolError("failure diagnostics manifest is not JSON") from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != DIAGNOSTIC_SCHEMA
            or manifest.get("run_id") != canonical
        ):
            raise EndpointProtocolError("failure diagnostics manifest is invalid")
        receipt_entry = manifest.get("receipt")
        if not isinstance(receipt_entry, dict) or receipt_entry.get("path") != "receipt.json":
            raise EndpointProtocolError("failure diagnostics carry no receipt entry")
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise EndpointProtocolError("failure diagnostics carry no file list")
        members: dict[str, str] = {"receipt.json": validate_sha256(receipt_entry.get("sha256"))}
        for relative, checksum in files.items():
            safe = _sealed_diagnostic_member(relative)
            if safe is None:
                raise EndpointProtocolError(f"failure diagnostics name an unsafe or unknown member: {relative!r}")
            members[f"evidence/{safe}"] = validate_sha256(checksum)
        budget = limit - len(manifest_bytes)
        if budget < 0:
            raise EndpointProtocolError("failure diagnostics exceed the configured size limit")
        written: dict[str, str] = {}
        for name, expected in members.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            self._download_artifact(canonical, name, target, expected, limit=budget)
            size = target.stat().st_size
            budget -= size
            if budget < 0:
                raise EndpointProtocolError("failure diagnostics exceed the configured size limit")
            written[name] = str(target)
        try:
            sealed_receipt = json.loads((destination / "receipt.json").read_bytes())
        except ValueError as error:
            raise EndpointProtocolError("failure diagnostics receipt is not JSON") from error
        if not isinstance(sealed_receipt, dict) or sealed_receipt.get("run_id") != canonical:
            raise EndpointProtocolError("failure diagnostics receipt belongs to a different run")
        (destination / "diagnostic.json").write_bytes(manifest_bytes)
        written["diagnostic.json"] = str(destination / "diagnostic.json")
        return written

    def _read_artifact(self, run_id: str, name: str, expected_sha256: str, *, limit: int) -> bytes:
        """Read one bounded artifact and require its recorded digest."""

        hasher = hashlib.sha256()
        total = 0
        chunks: list[bytes] = []
        with self.open_artifact(run_id, name) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise EndpointProtocolError(f"artifact {name!r} exceeds the configured size limit")
                hasher.update(chunk)
                chunks.append(chunk)
        if hasher.hexdigest() != expected_sha256:
            raise EndpointProtocolError(f"artifact {name!r} does not match its recorded digest")
        return b"".join(chunks)

    def _download_artifact(self, run_id: str, name: str, target: Path, expected_sha256: str, *, limit: int) -> None:
        """Stream one bounded artifact to disk under its recorded digest."""

        hasher = hashlib.sha256()
        total = 0
        with (
            self.open_artifact(run_id, name) as response,
            tempfile.NamedTemporaryFile(prefix=f".{target.name}-", dir=target.parent, delete=False) as handle,
        ):
            temporary = Path(handle.name)
            try:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise EndpointProtocolError(f"artifact {name!r} exceeds the configured size limit")
                    hasher.update(chunk)
                    handle.write(chunk)
            except BaseException:
                with suppress(OSError):
                    temporary.unlink()
                raise
        if hasher.hexdigest() != expected_sha256:
            with suppress(OSError):
                temporary.unlink()
            raise EndpointProtocolError(f"artifact {name!r} does not match its recorded digest")
        os.replace(temporary, target)

    def abort_job(self, run_id: str, *, error: str, error_code: str, detail: dict | None = None) -> dict:
        """Abort one awaiting-definition job after a local derivation failure; never hangs the endpoint."""

        for field, value in (("error", error), ("error_code", error_code)):
            if not isinstance(value, str) or not value.strip():
                raise EndpointProtocolError(f"abort {field} must be a non-empty string")
        if detail is not None and not isinstance(detail, dict):
            raise EndpointProtocolError("abort detail must be an object of diagnostic fields")
        response = self._request(
            "POST",
            f"/v1/jobs/{validate_run_id(run_id)}/abort",
            {"error": error, "error_code": error_code, "detail": dict(detail) if detail else {}},
        )
        if not isinstance(response, dict):
            raise EndpointProtocolError("abort acknowledgement must be a job object")
        if response.get("run_id", run_id) != run_id:
            raise EndpointProtocolError("endpoint acknowledged a different abort run")
        if "status" in response and response["status"] not in RUN_STATES:
            raise EndpointProtocolError(f"unexpected job status: {response.get('status')!r}")
        return response

    def release_cache(self, run_id: str, request: dict) -> dict:
        """The sanctioned IF-10 release of one terminal run's staged cache."""

        return self._request("POST", f"/v1/jobs/{validate_run_id(run_id)}/cache/release", request)

    def rerun_plan(self, run_id: str) -> dict:
        """Authoritative per-stage restart plan of one terminal native job.

        The endpoint probes its own retained checkpoints, dependency snapshot and
        publication target; the portal must not recompute eligibility locally.
        """
        canonical = validate_run_id(run_id)
        payload = self._request("GET", f"/v1/jobs/{canonical}/reruns")
        if payload.get("run_id") != canonical:
            raise EndpointProtocolError("rerun plan returned a different run_id")
        if payload.get("status") not in RUN_STATES:
            raise EndpointProtocolError(f"unexpected rerun plan status: {payload.get('status')!r}")
        rows = payload.get("stage_reruns")
        if not isinstance(rows, list) or [row.get("stage") for row in rows if isinstance(row, dict)] != list(STAGE_IDS):
            raise EndpointProtocolError("rerun plan must cover every engineering stage exactly once")
        for row in rows:
            if not isinstance(row, dict) or any(
                key not in row
                for key in (
                    "stage",
                    "name",
                    "eligible",
                    "reason",
                    "message",
                    "recomputes",
                    "retains",
                    "prerequisites",
                )
            ):
                raise EndpointProtocolError("rerun plan row is incomplete")
            if not isinstance(row["eligible"], bool) or not isinstance(row["recomputes"], list):
                raise EndpointProtocolError("rerun plan row carries invalid eligibility or recompute stages")
        return payload

    def get_job(self, run_id: str) -> dict:
        job = self._request("GET", f"/v1/jobs/{validate_run_id(run_id)}")
        if job.get("run_id") != run_id:
            raise EndpointProtocolError("endpoint returned a different run_id")
        if job.get("schema_version") != JOB_SCHEMA:
            raise EndpointProtocolError(f"unexpected job schema: {job.get('schema_version')!r}")
        if job.get("pipeline_id") != PIPELINE_ID:
            raise EndpointProtocolError(f"unexpected pipeline_id: {job.get('pipeline_id')!r}")
        for field in ("repository_slug", "repository_base"):
            value = job.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise EndpointProtocolError(f"job {field} must be non-empty text when present")
        if job.get("status") not in RUN_STATES:
            raise EndpointProtocolError(f"unexpected job status: {job.get('status')!r}")
        if not isinstance(job.get("events"), list):
            raise EndpointProtocolError("job response must include an events list")
        for event in job["events"]:
            if not isinstance(event, dict) or any(key not in event for key in EVENT_KEYS):
                raise EndpointProtocolError("Events must carry stage, state and timestamp")
        _validate_activity(job)
        return job

    def open_artifact(self, run_id: str, name: str) -> Any:
        """Open one verified delivery artifact for streaming; the caller closes the response."""
        canonical = validate_run_id(run_id)
        artifact = validate_artifact_name(name)
        encoded = urlparse.quote(artifact, safe="/")
        request = urlrequest.Request(
            f"{self.config.base_url}/v1/jobs/{canonical}/artifacts/{encoded}",
            method="GET",
            headers={"Authorization": f"Bearer {self.config.token}", "Accept": "*/*"},
        )
        try:
            return self._opener(request, timeout=self.config.timeout)
        except urlerror.HTTPError as error:
            raise _http_refusal(error, context=f"artifact {artifact!r} is not part of run {canonical}") from error
        except urlerror.URLError as error:
            raise EndpointError(f"endpoint unreachable: {error.reason}") from error

    def stream_capture_archive(
        self, run_id: str, archive: dict, destination: Path, *, limit: int = MAX_TRANSFER_BYTES
    ) -> Path:
        """Stream the native capture archive to disk, requiring its exact size and digest."""
        metadata = _validate_capture_archive(archive)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise EndpointProtocolError("archive size limit must be a positive integer")
        if metadata["size"] > limit:
            raise EndpointProtocolError("capture archive exceeds the configured size limit")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        with (
            self.open_artifact(run_id, metadata["name"]) as response,
            tempfile.NamedTemporaryFile(prefix=".capture-archive-", dir=destination.parent, delete=False) as handle,
        ):
            temporary = Path(handle.name)
            try:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise EndpointProtocolError("capture archive exceeds the configured size limit")
                    digest.update(chunk)
                    handle.write(chunk)
            except BaseException:
                with suppress(OSError):
                    temporary.unlink()
                raise
        if total != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
            with suppress(OSError):
                temporary.unlink()
            raise EndpointProtocolError("capture archive does not match its recorded size or digest")
        os.replace(temporary, destination)
        return destination

    def wait(self, run_id: str, *, interval: float = 2.0, timeout: float = 600.0) -> dict:
        """Bounded polling; returns the job dict at the native terminal boundary.

        Sealed native evidence transfers to Linux at ``native_complete``.
        Qualification stays with the independent result gates.
        """
        deadline = self._clock() + timeout
        while True:
            job = self.get_job(run_id)
            status = job["status"]
            if status in {"passed", "native_complete"}:
                return job
            if status == "failed":
                raise JobFailed(run_id, job)
            if self._clock() >= deadline:
                raise EndpointError(f"job {run_id} did not finish within {timeout:.0f}s")
            self._sleep(interval)


def _require_closure(upload_files: dict[str, str], main_assembly: str, snapshot: SelectionSnapshot) -> dict:
    """The sealed closure manifest must bind to the local intake it was staged from."""

    manifest = snapshot.manifest
    if not isinstance(manifest, dict):
        raise EndpointProtocolError("a sealed staging carries no closure manifest")
    if manifest.get("upload_sha256") != digest(upload_files):
        raise EndpointProtocolError("the closure manifest does not bind the upload index digest")
    if manifest.get("main_assembly") != main_assembly:
        raise EndpointProtocolError("the closure manifest is bound to a different main assembly")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise EndpointProtocolError("the closure manifest carries no resolved files")
    for name, checksum in files.items():
        if upload_files.get(name) != checksum:
            raise EndpointProtocolError(f"the closure manifest names a file outside the upload index: {name!r}")
    if manifest.get("handoff_sha256") != digest(files):
        raise EndpointProtocolError("the closure manifest does not bind its resolved closure digest")
    return manifest


def drive_staging(
    endpoint: WindowsEndpoint,
    *,
    run_id: str,
    upload_files: dict[str, str],
    main_assembly: str,
    intake_dir: Path,
    batch_dir: Path,
) -> dict:
    """One bounded staging step: push the exact indexed folder once, wait, or verify the closure.

    The archive carries every indexed name with the exact recorded bytes; a lost
    acknowledgement is retried logically with the same member set and hashes, and
    the endpoint seals the native closure itself - the client never plans rounds,
    seals or transfers anything beyond the one full indexed folder.
    """

    validated = validate_file_index(upload_files)
    snapshot = endpoint.staging_snapshot(run_id, upload_files=validated)
    if snapshot.state == "awaiting_files":
        batch_dir = Path(batch_dir)
        batch_dir.mkdir(parents=True, exist_ok=True)
        archive = batch_dir / f"staging-{digest(validated)}.zip"
        try:
            metadata = prepare_staging_archive(Path(intake_dir), archive, validated)
            if metadata["files"] != validated:
                raise EndpointProtocolError("the prepared staging archive differs from the upload index")
            response = endpoint.stage_folder_archive(run_id, archive, upload_files=validated)
        finally:
            archive.unlink(missing_ok=True)
        staged = _selection_block(response, upload_files=validated)
        if staged.state not in {"reading", "sealed"} or staged.staged_files != validated:
            raise EndpointProtocolError("endpoint did not stage the full indexed folder")
        return {"action": "staged", "state": staged.state, "sealed": staged.state == "sealed"}
    if snapshot.state == "reading":
        return {"action": "wait", "state": "reading", "sealed": False}
    manifest = _require_closure(validated, main_assembly, snapshot)
    return {"action": "sealed", "state": "sealed", "sealed": True, "manifest": manifest}


def verified_result(result: dict | None) -> dict:
    """Fail closed unless the run carries an independently verified model result.

    Qualification covers the delivered model only: the pipeline identity, the run subject and a
    passing, subject-bound quality document. Whole-job and publication success are separate
    gates (:func:`check_result`); a PR service failure must not hide an otherwise verified
    delivery, and a failed or unverified model must never be shown.
    """
    if not isinstance(result, dict):
        raise ResultNotPublishable("job has no model result payload")
    if result.get("pipeline_id") != PIPELINE_ID:
        raise ResultNotPublishable(f"result.pipeline_id must be {PIPELINE_ID}")
    subject = result.get("subject_sha256")
    if not isinstance(subject, str) or _SHA256.fullmatch(subject) is None:
        raise ResultNotPublishable("result.subject_sha256 is missing")
    quality = result.get("quality")
    if not isinstance(quality, dict) or quality.get("passed") is not True:
        raise ResultNotPublishable("result.quality is missing or not passed")
    if quality.get("subject_sha256") != subject:
        raise ResultNotPublishable("result.quality.subject_sha256 differs from the run subject")
    return result


def check_result(result: dict | None, *, expected_slug: str, expected_base: str) -> dict:
    """Fail closed unless the passed job carries bound quality and publication evidence.

    The publication gate stays strict: it requires the whole job's ``passed`` flag in addition
    to the model qualification from :func:`verified_result` and a subject-bound, passing
    submission for the configured repository.
    """
    result = verified_result(result)
    if result.get("passed") is not True:
        raise ResultNotPublishable("result.passed is not true")
    subject = result["subject_sha256"]
    submission = result.get("submission")
    if not isinstance(submission, dict) or submission.get("passed") is not True:
        raise ResultNotPublishable("result.submission is missing or not passed")
    if submission.get("subject_sha256") != subject:
        raise ResultNotPublishable("result.submission.subject_sha256 differs from the run subject")
    if submission.get("state") not in SUBMISSION_STATES:
        raise ResultNotPublishable(f"result.submission.state must be one of {sorted(SUBMISSION_STATES)}")
    if not isinstance(submission.get("base"), str) or not submission["base"].strip():
        raise ResultNotPublishable("result.submission.base is missing")
    if not isinstance(submission.get("commit"), str) or _COMMIT.fullmatch(submission["commit"]) is None:
        raise ResultNotPublishable("result.submission.commit must be a hex commit")
    url = str(submission.get("url") or "")
    if _PULL_URL.fullmatch(url) is None:
        raise ResultNotPublishable("result.submission.url must be an exact GitHub pull URL")
    if not expected_slug or submission.get("repository_slug") != expected_slug:
        raise ResultNotPublishable("repository_slug does not match the expected origin slug")
    if not url.startswith(f"https://github.com/{expected_slug}/pull/"):
        raise ResultNotPublishable("pull URL does not match the configured repository slug")
    if submission.get("base") != expected_base:
        raise ResultNotPublishable("submission.base does not match the expected base branch")
    if not isinstance(submission.get("branch"), str) or _REVIEW_BRANCH.fullmatch(submission["branch"]) is None:
        raise ResultNotPublishable(
            "submission.branch must be the deterministic work/solidworks/<hardware>/<purpose> branch"
        )
    return result


def resolved_routing(job: dict) -> dict:
    """The routing a native job resolved inside the serialized Windows execution.

    A native handoff cannot name hardware, revision or destination before CAD discovery, so the
    passed job snapshot must carry the routing it resolved instead of the operator request. The
    repository target is keyed directly by the native hardware identity, so no alias is carried.
    """
    if not isinstance(job, dict):
        raise EndpointProtocolError("job is not a JSON object")
    routing = {}
    for field in ("hardware_id", "revision", "repository_slug", "repository_base"):
        value = job.get(field)
        if not isinstance(value, str) or not value.strip() or _CONTROL.search(value):
            raise EndpointProtocolError(f"native job has not resolved {field}")
        routing[field] = value.strip()
    if _HARDWARE_ID.fullmatch(routing["hardware_id"]) is None:
        raise EndpointProtocolError("native job resolved a non-ASCII hardware_id")
    if "/" not in routing["repository_slug"]:
        raise EndpointProtocolError("native job resolved a repository_slug without an owner")
    return routing


class _SameHostRedirect(urlrequest.HTTPRedirectHandler):
    """Never forward the bearer token across hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        try:
            old = urlparse.urlsplit(req.full_url)
            new = urlparse.urlsplit(newurl)
            same_origin = (old.scheme, old.hostname, old.port) == (new.scheme, new.hostname, new.port)
        except ValueError:
            same_origin = False
        if not same_origin:
            raise urlerror.HTTPError(newurl, code, "cross-host redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Internal service calls use direct connections, independent of ambient proxy settings.
_OPENER = urlrequest.build_opener(urlrequest.ProxyHandler({}), _SameHostRedirect())


def _opener(request: urlrequest.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


if __name__ == "__main__":
    raise SystemExit(config_reference_cli())
