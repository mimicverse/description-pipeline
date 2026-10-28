"""Shared endpoint configuration, result validation and config resolution."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path, PurePosixPath
from urllib import parse as urlparse

from ..delivery import PIPELINE_ID
from ..io import PipelineError, artifact_path_parts, parse_data
from ..repository.review_branch import is_review_branch
from ..interface_records import (
    check_config_identity_file,
    require_active_config_identity,
    validate_config_snapshot,
    validate_connection_config_identity,
    validate_install_record,
)
from ..schemas import CONFIG_SNAPSHOT_SCHEMA
from ..stages import publication_succeeded

#: Stable UUID namespace for the native observation derived from one DAG run.
NATIVE_RUN_NAMESPACE = "solidworks_to_urdf"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PULL_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*\Z")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class EndpointError(RuntimeError):
    """Transport or protocol failure while talking to the execution endpoint."""


class EndpointProtocolError(EndpointError):
    """The endpoint returned an unexpected shape or value."""


class ResultNotPublishable(EndpointError):
    """A passed job whose result is missing quality or PR evidence."""


def native_run_id(dag_run_id: str) -> str:
    """The canonical native job UUID for one Airflow DAG run id (stable across retries)."""
    if not isinstance(dag_run_id, str) or not dag_run_id.strip():
        raise EndpointProtocolError("dag_run_id must be a non-empty string")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{NATIVE_RUN_NAMESPACE}:{dag_run_id}"))


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


def normalize_change_summary(value: str) -> str:
    """Normalize a change summary's line endings to LF before it is recorded.

    The discovery contract admits U+000A inside a change summary and still rejects every
    other control character, so the portal and the DAG intake fold CRLF (and a lone CR)
    into LF instead of forwarding a carriage return into the records.
    """

    return value.replace("\r\n", "\n").replace("\r", "\n")


def validate_artifact_name(value: str) -> str:
    return _validate_relative_path(value, "artifact name")


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
    #: Linux-side routing: model-repository slug to local checkout.
    repositories: dict[str, Path] = dataclass_field(default_factory=dict)
    #: Linux-side derivation controls: controlled-record roots and interface-name inputs.
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
    """Validate the Linux derivation settings: controlled-record roots."""
    if values in (None, "", {}):
        return {}
    if not isinstance(values, dict) or not set(values) <= {"record_roots"}:
        raise EndpointProtocolError("discovery must carry only record_roots")
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
    if not publication_succeeded(submission, subject):
        raise ResultNotPublishable("result.submission is missing or not passed")
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
    if not is_review_branch(submission.get("branch")):
        raise ResultNotPublishable(
            "submission.branch must be the deterministic "
            "work/description/<hardware>/<request> branch"
        )
    return result


if __name__ == "__main__":
    raise SystemExit(config_reference_cli())
