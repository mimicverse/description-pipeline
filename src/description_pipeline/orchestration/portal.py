"""Minimal authenticated operator portal for one SolidWorks engineering folder.

The portal is a dependency-free WSGI application:

* the operator signs in once with Feishu SSO at ``/auth/feishu/login``; the Airflow ``_token``
  cookie that Airflow issues is validated server-side through ``/auth/feishu/profile`` and its
  value is kept in a portal session, never echoed to the browser;
* the portal triggers the ``solidworks_to_urdf`` DAG with the canonical
  ``DispatchConf(run_id, entry_command_id)`` and reports the Airflow stage progress, native
  findings and the published pull request;
* every submission is admitted in the platform store first (request, run and the initial
  dispatch intent in one short transaction) and is refused while the admission window is
  closed or the platform store is unreachable;
* the Windows endpoint bearer token also stays server-side; the portal proxies the delivery
  preview and only serves URDF/mesh artifacts whose bytes match the digest-bound preview;
* the bundled viewer renders the actual verified URDF with joint and limit controls.

Run ``python -m description_pipeline.orchestration.portal --help`` for the configuration surface.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from collections.abc import Callable, Iterable, Mapping
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from ..pipeline.verify_contract import validate_certificate_diagnostics
from ..stages import (
    CONTRACT,
    CONTRACT_FILE_SHA256,
    CONTRACT_SHA256,
    VIEW_SCHEMA,
    activity_view,
    stage_display_state,
    stage_view,
)

from ..artifacts import ArtifactStore
from .. import mjcf_contract
from ..pipeline.stages.mjcf import (
    source_refs as mjcf_source_refs, certificate_from_bytes as mjcf_certificate_from_bytes,
)
from .airflow_client import (
    EndpointProtocolError,
    ResultNotPublishable,
    _linux_repositories,
    _linux_targets,
    check_result,
    native_run_id,
    normalize_change_summary,
    validate_artifact_name,
    verified_result,
)
from ..io import PipelineError, canonical_json_value, digest
from ..interface_records import REQUEST_PURPOSES
from ..delivery import PIPELINE_ID, SUBJECT_DIRECTORIES, PROJECTION_DIRECTORIES, SUBJECT_FILES
from ..platform.queries import read_run_projection
from ..schemas import PUBLICATION_RECEIPT_SCHEMA, RUN_ACTIVITY_SCHEMA
from ..pipeline.contracts import ArtifactRef, StageStatus, StageCommand
from ..pipeline.dispatch import DispatchObservation
from ..pipeline.effects import EffectState
from ..pipeline.verify import (
    CERTIFICATE_FILENAME,
    EXECUTION_RECEIPT_FILENAME,
    validate_certificate_structure,
)
from ..platform.facts import FactStoreError, RunNotAdmittedError
from ..ports import PORTAL_HTTP_PORT
from .feishu_oauth import (
    TRIGGERING_USER_NAME_DELIMITER,
    TRIGGERING_USER_NAME_LIMIT,
    build_actor_name,
    recorded_display_name,
    sanitize_display_name,
    valid_actor_principal,
)
from .run_ownership import (
    AMBIGUOUS_TASK_STATE,
    RECOVERY_ENGINEERING_TERMINAL,
    RECOVERY_UNKNOWN_PUBLICATION,
    RecoveryAssessment,
    classify_recovery,
    registered_recovery_block as _registered_recovery_block,
    recorded_actor_principal,
)
from .report_view import (
    _display_check_details, _display_value, display_stage_view,
    warning_summaries,
    text_warning_inventory,
    build_report,
    merge_warnings,
    registered_discovery_record,
    registered_discovery_findings,
    registered_handoff_warnings,
)
from .uploads import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    UploadGate,
    UploadRejected,
    drop_received_folder,
    receive_folder,
)

log = logging.getLogger(__name__)

DEFAULT_DAG_ID = "solidworks_to_urdf"
DEFAULT_API_ROOT = "/api/v2"
DEFAULT_SESSION_COOKIE = "solidworks_portal_session"
#: The Airflow SSO cookie the portal adopts after server-side validation.
AIRFLOW_TOKEN_COOKIE = "_token"
_AIRFLOW_TIMEOUT = 20.0
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,250}\Z")
_STAGE_IDS = tuple(stage["id"] for stage in CONTRACT["stages"])
_STAGE_NAMES = {stage["id"]: stage.get("name") or stage["id"] for stage in CONTRACT["stages"]}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
#: Row overlay when a run has no platform display row (treated read-only).
_EMPTY_OVERLAY = {"title": None, "deleted": False, "deleted_at": None, "deleted_by": None}

MAX_TITLE = 120


class TitleError(ValueError):
    """One rejected display title."""


def normalise_title(value: object) -> str | None:
    """One display title: printable text up to ``MAX_TITLE``; ``None`` clears it."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TitleError("Run title must be text")
    text = value.strip()
    if not text:
        raise TitleError("Run title cannot be empty; pass null to restore the default")
    if len(text) > MAX_TITLE:
        raise TitleError(f"Run title cannot exceed {MAX_TITLE} characters")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise TitleError("Run title contains invisible control characters")
    return text


def _display_overlay(display: object) -> dict:
    """One platform run-display row as the portal's overlay surface.

    The platform owns the display state - the name and the reversible
    ``hidden_at``/``hidden_reason`` tombstone; the portal only shapes it.
    """
    return {
        "title": display.display_name,
        "deleted": display.hidden_at is not None,
        "deleted_at": display.hidden_at.isoformat() if display.hidden_at is not None else None,
        "deleted_by": display.hidden_reason,
    }


#: Task states that still own execution; an unknown (None) state stays a refusal too.
_ACTIVE_TASK_STATES = {"running", "queued", "scheduled", "up_for_retry", "up_for_reschedule", "deferred"}
#: Task states that are terminal; anything else (including new Airflow states) fails closed.
_TERMINAL_TASK_STATES = {"success", "failed", "upstream_failed", "skipped"}
_ARTIFACT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".urdf": "application/xml",
    ".xml": "application/xml",
    ".stl": "model/stl",
    ".obj": "text/plain",
    ".dae": "model/vnd.collada+xml",
    ".json": "application/json",
    ".png": "image/png",
}
#: Bounded preview payloads: totals always report the untruncated count.
_PREVIEW_LIMIT = 50
_SHA256_TEXT = re.compile(r"\A[0-9a-f]{64}\Z")


def _member_class(path: str) -> str:
    """The member class projected from its path: reports are never subject members."""

    if path in SUBJECT_FILES or any(
        path == directory or path.startswith(directory + "/")
        for directory in (*SUBJECT_DIRECTORIES, *PROJECTION_DIRECTORIES)
    ):
        return "subject"
    return "report"


def _failing_checks(report: dict) -> list[dict]:
    """The exact failed checks of the one registered quality report.

    The one convention both writers share: a row is failed when it carries the
    boolean ``passed`` false OR the recorded ``state`` is ``"failed"``.  A
    registered report whose consumed ``checks`` field has the wrong shape
    refuses instead of silently reading as zero failures.
    """

    checks: list[dict] = []
    for row in _registered_source_rows(report, "checks"):
        if not (row.get("passed") is False or row.get("state") == "failed"):
            continue
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        checks.append(
            {
                "id": str(row.get("id") or ""),
                "state": str(row.get("state") or "failed"),
                "message": str(details.get("error") or details.get("reason") or "Automatic check failed"),
            }
        )
    return checks


def _parse_registered_json(data: bytes) -> dict:
    """Parse one registered diagnostic source; unreadable content refuses, never empty.

    These bytes were already verified against the registration, so a parse
    failure is a defect of the registered content itself: the preview refuses
    with the fixed 502 class instead of silently reporting no findings or
    warnings.
    """

    if not isinstance(data, (bytes, bytearray)):
        raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered diagnostic content cannot be parsed")
    try:
        parsed = json.loads(bytes(data).decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as error:
        raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered diagnostic content cannot be parsed") from error
    if not isinstance(parsed, dict):
        raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered diagnostic content cannot be parsed")
    return parsed


def _registered_source_rows(report: dict, field: str, *, code_required: bool = False) -> list[dict]:
    """The consumed rows of one registered list field.

    An absent optional field reads as no rows; a present field that is not a
    list of usable records is a shape error and refuses (502): a declared,
    registered diagnostic source can never silently contribute zero.
    """

    if field not in report:
        return []
    rows = report[field]
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or (code_required and (not isinstance(row.get("code"), str) or not row["code"]))
        for row in rows
    ):
        raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered diagnostic content has an invalid structure")
    return rows


def _delivery_warnings(members: dict) -> list[dict]:
    """Input warnings and non-blocking discovery findings, from the same registered members."""

    warnings: list[dict] = []
    handoff = members.get("input/handoff.json")
    discovery = members.get("input/discovery/native-discovery.json")
    try:
        if handoff is not None:
            for warning in registered_handoff_warnings(
                _parse_registered_json(handoff).get("warnings")
            ):
                warning.setdefault("source", "input")
                warnings.append(warning)
        if discovery is not None:
            record = registered_discovery_record(_parse_registered_json(discovery))
            for item in record["findings"]:
                if item["blocking"]:
                    continue
                warning = dict(item)
                warning.setdefault("source", "discovery")
                warnings.append(warning)
    except PipelineError as error:
        raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
    return warnings


def _quality_warnings(report: dict | None) -> list[dict]:
    """The displayable non-blocking diagnostics of the registered quality report."""

    warnings: list[dict] = []
    diagnostics = _registered_source_rows(
        report or {},
        "diagnostics",
        code_required=True,
    )
    try:
        validate_certificate_diagnostics(report or {})
    except PipelineError as error:
        raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
    for item in diagnostics:
        if item.get("blocking") is not False:
            # Only an explicit non-blocking diagnostic is a warning.  A
            # missing or unknown flag must never be downgraded silently.
            continue
        warning = dict(item)
        warning.setdefault("source", "verification")
        warnings.append(warning)
    return warnings


def _preview_warnings(*groups: list[dict]) -> list[dict]:
    """Merge recorded warnings with the shared semantic identity."""

    return merge_warnings(*groups)


_VERIFICATION_REPORT_LABELS = {
    CERTIFICATE_FILENAME: "Verification certificate",
    EXECUTION_RECEIPT_FILENAME: "Verification execution receipt",
    mjcf_contract.REPORT_PATH: "MJCF verification certificate",
}


def _run_state(projection: dict | None) -> str:
    """Expose only the canonical business state, including missing evidence."""
    if not isinstance(projection, dict):
        return "unresolved"
    return {
        "succeeded": "success",
        "failed": "failed",
        "cancelled": "cancelled",
        "unresolved": "unresolved",
        "running": "running",
    }.get(str(projection.get("state")), "unresolved")
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
}


class PortalError(RuntimeError):
    """An operator-facing failure with an HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = message


def _verified_admission_config_digest(provider: Callable[[], str] | None = None) -> str:
    """The snapshot identity every newly admitted request records."""

    if provider is None:
        from .airflow_client import config_from_airflow_connection

        try:
            value = config_from_airflow_connection("solidworks_windows").config_digest
        except Exception as error:
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Platform configuration identity is unavailable; submission was not admitted",
            ) from error
    else:
        value = str(provider()).strip()
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PortalError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Platform configuration identity is unavailable; submission was not admitted",
        )
    return value


class AirflowApiError(RuntimeError):
    """The Airflow API rejected a request or returned an unexpected shape."""


class AirflowAuthError(AirflowApiError):
    """The Airflow credentials or token were rejected."""


def _validate_http_url(url: str) -> str:
    try:
        parsed = urlparse.urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise AirflowApiError("invalid Airflow address") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AirflowApiError("Airflow address must be an absolute HTTP or HTTPS URL")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK:
        raise AirflowApiError("plaintext HTTP is only allowed for loopback Airflow addresses")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AirflowApiError("Airflow address cannot embed credentials, a query or a fragment")
    return url.rstrip("/")


class _SameOriginRedirect(urlrequest.HTTPRedirectHandler):
    """Never forward the Airflow bearer token across origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        try:
            old = urlparse.urlsplit(req.full_url)
            new = urlparse.urlsplit(newurl)
            same_origin = (old.scheme, old.hostname, old.port) == (new.scheme, new.hostname, new.port)
        except ValueError:
            same_origin = False
        if not same_origin:
            raise urlerror.HTTPError(newurl, code, "cross-origin redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Internal service calls use direct connections, independent of ambient proxy settings.
_AIRFLOW_OPENER = urlrequest.build_opener(urlrequest.ProxyHandler({}), _SameOriginRedirect())


@dataclass
class PortalSession:
    session_id: str
    user: str
    avatar_url: str
    principal: str
    token: str
    csrf_token: str
    created_at: float
    last_seen: float


class SessionStore:
    """Server-side sessions; browsers only ever see an opaque cookie value."""

    def __init__(self, ttl: float) -> None:
        if ttl <= 0:
            raise ValueError("session ttl must be positive")
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._sessions: dict[str, PortalSession] = {}

    def create(self, user: str, token: str, *, avatar_url: str = "", principal: str = "") -> PortalSession:
        now = time.time()
        session = PortalSession(
            session_id=secrets.token_urlsafe(32),
            user=user,
            avatar_url=avatar_url,
            principal=principal,
            token=token,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_seen=now,
        )
        with self._lock:
            self._sessions = {key: value for key, value in self._sessions.items() if self._fresh(value, now)}
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> PortalSession | None:
        if not session_id:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if not self._fresh(session, now):
                self._sessions.pop(session_id, None)
                return None
            session.last_seen = now
            return session

    def drop(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    def _fresh(self, session: PortalSession, now: float) -> bool:
        return now - session.last_seen <= self.ttl


class AirflowApi:
    """Thin client for the Airflow 3 stable REST API, used only with the operator's SSO token."""

    def __init__(
        self,
        base_url: str,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = _validate_http_url(base_url)
        self.api_root = DEFAULT_API_ROOT
        self.timeout = _AIRFLOW_TIMEOUT
        self._opener = opener or _AIRFLOW_OPENER.open

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        token: str | None = None,
        cookie_token: str | None = None,
    ) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if cookie_token:
            headers["Cookie"] = f"{AIRFLOW_TOKEN_COOKIE}={cookie_token}"
        request = urlrequest.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read()
        except urlerror.HTTPError as error:
            if error.code in {401, 403}:
                raise AirflowAuthError("Airflow rejected the operator credentials") from error
            raise AirflowApiError(f"Airflow returned HTTP {error.code}") from error
        except urlerror.URLError as error:
            raise AirflowApiError(f"Airflow unreachable: {error.reason}") from error
        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AirflowApiError("Airflow returned invalid JSON") from error
        if not isinstance(parsed, dict):
            raise AirflowApiError("Airflow returned a non-object payload")
        return parsed

    def profile(self, token: str) -> dict:
        """Validate one browser's Airflow SSO cookie and return its Feishu identity."""
        return self._request("GET", "/auth/feishu/profile", cookie_token=token)

    def trigger_dag_run(self, token: str, dag_id: str, dag_run_id: str, conf: dict) -> dict:
        # Airflow 3.3.2's TriggerDAGRunPostBody requires ``logical_date`` to be present (nullable);
        # this workflow is schedule=None, so the run intentionally carries no logical date.
        return self._request(
            "POST",
            f"{self.api_root}/dags/{urlparse.quote(dag_id)}/dagRuns",
            {"dag_run_id": dag_run_id, "logical_date": None, "conf": conf},
            token=token,
        )

    def dag_run(self, token: str, dag_id: str, dag_run_id: str) -> dict:
        return self._request(
            "GET",
            f"{self.api_root}/dags/{urlparse.quote(dag_id)}/dagRuns/{urlparse.quote(dag_run_id)}",
            token=token,
        )

    def task_instances(self, token: str, dag_id: str, dag_run_id: str) -> list[dict]:
        payload = self._request(
            "GET",
            f"{self.api_root}/dags/{urlparse.quote(dag_id)}/dagRuns/{urlparse.quote(dag_run_id)}/taskInstances",
            token=token,
        )
        instances = payload.get("task_instances")
        if not isinstance(instances, list):
            raise AirflowApiError("Airflow returned no task instance list")
        return [item for item in instances if isinstance(item, dict)]

    def clear_dag_run(self, token: str, dag_id: str, dag_run_id: str, *, dry_run: bool) -> dict:
        """Retry failed transport tasks on the original DAG version and frozen input."""
        return self._request(
            "POST",
            f"{self.api_root}/dags/{urlparse.quote(dag_id)}/dagRuns/{urlparse.quote(dag_run_id)}/clear",
            {"dry_run": dry_run, "only_failed": True, "only_new": False, "run_on_latest_version": False},
            token=token,
        )


@dataclass(frozen=True, slots=True)
class AirflowDispatchAdapter:
    """Bind one authenticated portal session to the common dispatch service."""

    airflow: AirflowApi
    token: str

    def trigger(self, command, conf) -> Mapping[str, Any] | None:
        return self.airflow.trigger_dag_run(
            self.token,
            command.dag_id,
            command.dag_run_id,
            conf.to_record(),
        )

    def observe(self, command, conf) -> DispatchObservation:
        try:
            payload = self.airflow.dag_run(
                self.token,
                command.dag_id,
                command.dag_run_id,
            )
        except AirflowApiError as error:
            if (
                isinstance(error.__cause__, urlerror.HTTPError)
                and error.__cause__.code == 404
            ):
                return DispatchObservation(
                    "absent",
                    {
                        "dag_id": command.dag_id,
                        "dag_run_id": command.dag_run_id,
                    },
                )
            raise
        if (
            payload.get("dag_id") != command.dag_id
            or payload.get("dag_run_id") != command.dag_run_id
            or payload.get("conf") != conf.to_record()
        ):
            raise AirflowApiError(
                "Airflow returned a different dag run or dispatch conf identity"
            )
        return DispatchObservation(
            "known",
            {
                "dag_id": command.dag_id,
                "dag_run_id": command.dag_run_id,
                "state": payload.get("state"),
            },
        )


@dataclass(frozen=True)
class PortalConfig:
    airflow: AirflowApi
    dag_id: str = DEFAULT_DAG_ID
    static_dir: Path = Path(__file__).resolve().parent / "static"
    host: str = "127.0.0.1"
    port: int = PORTAL_HTTP_PORT
    session_cookie: str = DEFAULT_SESSION_COOKIE
    session_ttl: float = 12 * 3600.0
    artifact_limit: int = 64 * 1024 * 1024
    max_body_bytes: int = 64 * 1024
    upload_root: Path | None = None
    upload_concurrency: int = 2
    simulation: dict | None = None
    #: Split-pipeline routing: the model-repository checkouts.
    pipeline_repositories: dict[str, Path] | None = None
    #: Configured publication targets used by the upload selector and conf validation.
    pipeline_targets: dict[str, dict] | None = None
    #: Canonical run control: admitted-run reads and display metadata only.
    run_control_store: Any | None = None
    #: Test/embedding seam for the already-validated active snapshot digest. Production
    #: loading leaves this unset so admission resolves the real Connection contract.
    config_identity_provider: Callable[[], str] | None = None
    #: Test/embedding seam for the one read-only fact projection. Production leaves
    #: this unset and reads `PLATFORM_SERVICE_DSN` plus `PLATFORM_ARTIFACT_ROOT`.
    run_projection_factory: Callable[[str], dict | None] | None = None
    artifact_store: ArtifactStore | None = None
    #: One production ingestion closure over the shared StageApplication, artifact
    #: store, native operation client and immutable config snapshot.
    production_admission_loader: Callable[[Any], Any] | None = None
    production_stage_rerun_loader: Callable[[Any], Any] | None = None
    production_application: Any | None = None
    production_native_client: Any | None = None
    production_config_snapshot: Path | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9_]{1,64}", self.session_cookie) is None:
            raise ValueError("Portal session cookie name is invalid")
        if self.run_projection_factory is None:
            projection = getattr(self.run_control_store, "fact_projection", None)
            if callable(projection):
                object.__setattr__(self, "run_projection_factory", projection)
        if self.artifact_store is None:
            artifacts = getattr(self.run_control_store, "projection_artifacts", None)
            if artifacts is None and self.production_application is not None:
                artifacts = getattr(self.production_application, "artifacts", None)
            if artifacts is not None:
                object.__setattr__(self, "artifact_store", artifacts)


_CONFIG_KEYS = {
    "airflow": {"url"},
    "endpoint": {"url", "token_file"},
    "portal": {"host", "port", "upload_root", "upload_concurrency", "simulation"},
    "pipeline": {"repositories", "targets", "discovery"},
}


def _read_config(path: Path) -> dict:
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AirflowApiError(f"cannot read portal config {path}: {error}") from error
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise AirflowApiError(f"portal config {path} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise AirflowApiError("portal config must be one JSON object")
    return data


def _section(data: dict, name: str) -> dict:
    section = data.get(name) or {}
    if not isinstance(section, dict):
        raise AirflowApiError(f"portal config section [{name}] must be a table/object")
    unknown = set(section) - _CONFIG_KEYS[name]
    if unknown:
        raise AirflowApiError(f"portal config section [{name}] has unknown keys: {sorted(unknown)}")
    return section


def _production_admission_runtime(
    endpoint_section: dict,
    environ: Mapping[str, str],
) -> tuple[
    Callable[[Any], Any] | None,
    Callable[[Any], Any] | None,
    Any | None,
    Any | None,
    Path | None,
]:
    """Compose the one production admission application from controlled inputs."""

    snapshot_value = environ.get("PLATFORM_CONFIG_SNAPSHOT")
    if not snapshot_value:
        raise AirflowApiError(
            "portal production admission requires PLATFORM_CONFIG_SNAPSHOT"
        )
    required = (
        "PLATFORM_CONFIG_SNAPSHOT",
        "PLATFORM_ARTIFACT_ROOT",
        "PLATFORM_SERVICE_DSN",
        "PIPELINE_WORK_ROOT",
    )
    missing = [name for name in required if not environ.get(name)]
    if missing:
        raise AirflowApiError(
            f"production admission environment is incomplete: {missing}"
        )
    snapshot = Path(snapshot_value).resolve()
    work_root = Path(environ["PIPELINE_WORK_ROOT"]).resolve()
    artifact_root = Path(environ["PLATFORM_ARTIFACT_ROOT"]).resolve()
    for path, label, real_directory in (
        (snapshot, "PLATFORM_CONFIG_SNAPSHOT", False),
        (work_root, "PIPELINE_WORK_ROOT", True),
        (artifact_root, "PLATFORM_ARTIFACT_ROOT", True),
    ):
        if (
            path.is_symlink()
            or (not path.is_file() if not real_directory else not path.is_dir())
        ):
            raise AirflowApiError(f"{label} is not a safe existing path")
    url = endpoint_section.get("url")
    token_file = endpoint_section.get("token_file")
    if not isinstance(url, str) or not isinstance(token_file, str):
        raise AirflowApiError("production admission needs endpoint.url and endpoint.token_file")
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
        from ..pipeline.application import application_from_environment

        from .native_operation_client import (
            NativeOperationClient,
            NativeOperationEndpoint,
            NativeOperationToolProvider,
        )
        from ..pipeline.ingestion import load_production_admission
        from ..pipeline.ingestion import load_production_stage_rerun
        from ..pipeline.native_target import (
            load_registered_native_target,
        )

        native_target = load_registered_native_target(snapshot)
        client = NativeOperationClient(
            NativeOperationEndpoint(base_url=url, token=token),
            target_id=native_target.target_id,
        )
        runtime_environment = {
            **dict(environ),
            "SOLIDWORKS_ENDPOINT_URL": url,
            "ENDPOINT_TOKEN_FILE": token_file,
        }
        application = application_from_environment(
            runtime_environment,
            native_operation_client=client,
            native_target_identity=native_target,
        )
        artifacts = application.artifacts
        tool_provider = NativeOperationToolProvider(client)
    except Exception as error:
        raise AirflowApiError(
            f"production admission composition failed: {error}"
        ) from error

    def load(request):
        return load_production_admission(
            application,
            request,
            artifacts=artifacts,
            platform_snapshot=snapshot,
            native_tool_provider=tool_provider,
            work_root=work_root,
        )

    def load_stage_rerun(request):
        return load_production_stage_rerun(
            application, request, artifacts=artifacts, platform_snapshot=snapshot,
            native_tool_provider=tool_provider, work_root=work_root,
        )

    return load, load_stage_rerun, application, client, snapshot


def load_portal_config(path: Path) -> PortalConfig:
    """Load the deployed portal configuration rendered by the operator install.

    The install renders ``portal.json`` from ``deploy/operator/portal.json.template``
    with the resolved generation's values; this parser is the only reader of the
    rendered file (this package ships no example copy of it).
    """
    data = _read_config(path)
    unknown = set(data) - set(_CONFIG_KEYS)
    if unknown:
        raise AirflowApiError(f"portal config has unknown sections: {sorted(unknown)}")
    airflow_section = _section(data, "airflow")
    portal_section = _section(data, "portal")
    endpoint_section = _section(data, "endpoint")
    pipeline_section = _section(data, "pipeline")
    url = airflow_section.get("url")
    if not isinstance(url, str) or not url.strip():
        raise AirflowApiError("portal config needs airflow.url")
    try:
        pipeline_repositories = _linux_repositories(pipeline_section.get("repositories") or {})
        pipeline_targets = _linux_targets(pipeline_section.get("targets") or {})
    except EndpointProtocolError as error:
        raise AirflowApiError(f"portal config [pipeline]: {error}") from error
    for hardware, target in pipeline_targets.items():
        if target["repository_slug"] not in (pipeline_repositories or {}):
            raise AirflowApiError(
                f"portal config [pipeline]: target {hardware!r} names an unconfigured repository "
                f"{target['repository_slug']!r}"
            )
    (
        production_loader,
        production_stage_rerun_loader,
        production_application,
        production_native_client,
        production_snapshot,
    ) = _production_admission_runtime(endpoint_section, os.environ)
    return PortalConfig(
        airflow=AirflowApi(url.strip()),
        host=str(portal_section.get("host") or "127.0.0.1"),
        port=int(portal_section.get("port", PORTAL_HTTP_PORT)),
        upload_root=_upload_root(portal_section),
        upload_concurrency=_upload_concurrency(portal_section),
        simulation=_simulation_config(portal_section),
        pipeline_repositories=pipeline_repositories,
        pipeline_targets=pipeline_targets,
        production_admission_loader=production_loader,
        production_stage_rerun_loader=production_stage_rerun_loader,
        production_application=production_application,
        production_native_client=production_native_client,
        production_config_snapshot=production_snapshot,
    )


def _simulation_config(section: dict) -> dict | None:
    value = section.get("simulation")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"root", "python", "capacity"}:
        raise AirflowApiError("portal.simulation requires root, python and capacity")
    if type(value["capacity"]) is not int or not 1 <= value["capacity"] <= 8:
        raise AirflowApiError("Simulation capacity must be between 1 and 8")
    for key in ("root", "python"):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise AirflowApiError("Simulation paths must be absolute")
    return value


def _upload_root(section: dict) -> Path | None:
    """One absolute, existing, non-symlink intake directory; unset stays fail-closed."""
    value = section.get("upload_root")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AirflowApiError("portal.upload_root must be an absolute directory path")
    path = Path(value.strip())
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise AirflowApiError("portal.upload_root must be an existing absolute directory")
    return path


def _upload_concurrency(section: dict) -> int:
    try:
        value = int(section.get("upload_concurrency", 2))
    except (TypeError, ValueError) as error:
        raise AirflowApiError("portal.upload_concurrency must be a positive integer") from error
    if value < 1 or value > 16:
        raise AirflowApiError("portal.upload_concurrency must be between 1 and 16")
    return value


def _actor_identity(actor: object) -> tuple[str | None, str | None]:
    """Split one Airflow actor value into its stable principal and recorded display name.

    The Feishu auth manager mints ``<principal>|<json name>`` in ``FeishuUser.get_name()``, so
    ``triggering_user_name`` carries both the durable identity and the Feishu-verified display
    name as one unforgeable, server-derived value. A value that is not that shape (legacy rows,
    other trigger sources, a damaged name) yields no name; a value whose principal part is not a
    compound identity keeps that raw value as the principal, so the operator page honestly says
    the name was not recorded.
    """
    if not isinstance(actor, str):
        return None, None
    text = actor.strip()
    if not text:
        return None, None
    if len(text) > TRIGGERING_USER_NAME_LIMIT:
        # An actor value the metadata column could never hold is damaged, not a name source.
        return text, None
    principal, delimiter, encoded = text.partition(TRIGGERING_USER_NAME_DELIMITER)
    if delimiter and valid_actor_principal(principal):
        try:
            name = sanitize_display_name(json.loads(encoded))
        except ValueError:
            name = ""
        # A well-formed principal plus a damaged name keeps the principal and records no name.
        return principal, name or None
    return text, None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _stage_name(value: object) -> str | None:
    stage = _optional_str(value)
    return _STAGE_NAMES.get(stage) if stage is not None else None


def suggest_target(candidates: list[str], targets: dict[str, dict]) -> str | None:
    """The unique configured hardware identity contained in the candidate names."""

    hits = [
        hardware
        for hardware in targets
        if any(hardware.casefold() in str(candidate).casefold() for candidate in candidates if str(candidate).strip())
    ]
    return hits[0] if len(hits) == 1 else None


def _page_integer(query: dict, name: str, *, default: int, minimum: int, maximum: int) -> int:
    """One bounded integer list parameter; invalid values are refused, never clamped silently."""
    raw = str((query.get(name) or [""])[0])
    if raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise PortalError(HTTPStatus.BAD_REQUEST, f"Pagination parameter {name} is invalid") from error
    if value < minimum or value > maximum:
        raise PortalError(HTTPStatus.BAD_REQUEST, f"Pagination parameter {name} is out of range")
    return value


def _discovery_evidence(detail: object, digest: str) -> dict:
    """One finding's diagnostic payload, always bound to the raw discovery record digest."""
    evidence: dict = {}
    if detail not in (None, "", [], {}):
        location = {"stage": "discover", "record": "input/discovery/native-discovery.json"}
        evidence["detail"] = (
            _display_check_details(detail, location) if isinstance(detail, dict)
            else _display_value(detail, location)
        )
    if digest:
        evidence["discovery_sha256"] = digest
    return evidence


def _registered_error_stage(job: dict) -> str:
    """The stage a registered error belongs to: the earliest failed stage of the registration.

    The registered failure carries its own stage and is never guessed from event order - the
    convergence appends later blocked stages whose events must not claim the error.  Only an
    unregistered native job, which has no registration to attribute from, keeps the native
    observation's last event stage.
    """
    stage = job.get("failed_stage")
    if isinstance(stage, str) and stage:
        return stage
    return str((job.get("events") or [{}])[-1].get("stage", ""))


def _findings(job: dict | None) -> list[dict]:
    """Native finding identities and explicit evidence excerpts over sealed source records.

    The endpoint's ``discovery.findings`` entries are
    ``{schema_version, code, object, message, detail, blocking}``; ``detail`` is the diagnostic
    evidence and ``discovery_sha256`` identifies the raw record it came from.
    """
    findings: list[dict] = []
    if not isinstance(job, dict):
        return findings
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    try:
        discovery_findings = registered_discovery_findings(job)
    except PipelineError as error:
        raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
    discovery = job.get("discovery") if isinstance(job.get("discovery"), dict) else {}
    digest = str(discovery.get("discovery_sha256") or "")
    for item in discovery_findings:
        findings.append(
            {
                "id": item["code"],
                "severity": "warning" if item["blocking"] is False else "error",
                "blocking": item["blocking"],
                "stage": "discover",
                "object": str(item.get("object") or ""),
                "message": str(item.get("message") or "native discovery finding"),
                "evidence": _discovery_evidence(item.get("detail"), digest),
            }
        )
    if job.get("error"):
        findings.append(
            {
                "id": "",
                "severity": "error",
                "stage": _registered_error_stage(job),
                "object": "",
                "message": str(job["error"]),
                "evidence": job.get("detail") or result.get("detail") or {},
            }
        )
    native_error = job.get("native_error")
    if isinstance(native_error, str) and native_error != job.get("error"):
        findings.append(
            {
                "id": "native.observation",
                "severity": "error",
                "stage": _registered_error_stage(job),
                "object": "",
                "message": native_error,
                "evidence": {"source": "native_observation"},
            }
        )
    quality = result.get("quality")
    if isinstance(quality, dict):
        for check in quality.get("checks") or []:
            if isinstance(check, dict) and (
                check.get("state") == "failed" or check.get("passed") is False
            ):
                findings.append(
                    {
                        "id": str(check.get("id") or ""),
                        "severity": "error",
                        "stage": "verify",
                        "object": str(check.get("object") or ""),
                        "message": str(
                            (check.get("details") or {}).get("error")
                            or (check.get("details") or {}).get("reason")
                            or "Automatic check failed"
                        ),
                        "evidence": check.get("details") or {},
                    }
                )
    return findings


def _waiting_for_effect_results(projection: dict, now: datetime) -> bool:
    """Describe authorized or yielded waits without changing their UNKNOWN safety state."""
    technical = projection.get("technical") or {}
    effects = [
        row
        for row in technical.get("effects", ())
        if isinstance(row, Mapping) and row.get("state") in {"unknown", "conflict"}
    ]
    if not effects:
        return False
    claims = {
        row.get("claim_id"): row
        for row in technical.get("claims", ())
        if isinstance(row, Mapping)
    }
    for effect in effects:
        if effect.get("state") != "unknown":
            return False
        fact = effect.get("decisive_fact") or {}
        if fact.get("fact_type") not in {"dispatch_authorized", "resume_authorized"}:
            return False
        claim = claims.get(fact.get("claim_id")) or {}
        if (
            claim.get("command_id") != effect.get("command_id")
            or claim.get("fence") != fact.get("fence")
        ):
            return False
        if claim.get("closed_at") is not None:
            if (
                claim.get("closure_code") == "yielded"
                and claim.get("yield_effect_id") == effect.get("effect_id")
            ):
                continue
            return False
        expiry = claim.get("expires_at")
        if isinstance(expiry, str):
            try:
                expiry = datetime.fromisoformat(expiry)
            except ValueError:
                return False
        if (
            not isinstance(expiry, datetime)
            or expiry.tzinfo is None
            or expiry <= now
        ):
            return False
    return True


def _automatic_summary(
    job: dict | None,
    projection: dict | None,
    *,
    now: datetime | None = None,
) -> dict:
    """Summarize recorded checks without turning missing verification into failure."""
    if not isinstance(job, dict):
        return {
            "state": "pending",
            "job_state": "pending",
            "checks": [],
        }
    projection = projection if isinstance(projection, dict) else {}
    stages = projection.get("stages") or {}
    verify = stages.get("verify") or {}
    verification_not_started = (
        not verify.get("decision")
        and verify.get("state") not in {"active", "unresolved"}
    )
    status = str(job.get("status") or "pending")
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    discovery = job.get("discovery") if isinstance(job.get("discovery"), dict) else {}
    quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
    checks = [
        {**check, "details": _display_check_details(check.get("details") or {}, {
            "stage": "verify", "record": "reports/verification-certificate.json", "check": check.get("id"),
        })}
        for check in quality.get("checks") or [] if isinstance(check, dict)
    ]
    if status == "unresolved":
        waiting = _waiting_for_effect_results(projection, now or datetime.now(UTC))
        message = (
            "An authorized execution is awaiting its recorded external result"
            if waiting
            else "External results are unresolved; reconcile the recorded effects before retrying"
        )
        if verification_not_started:
            message += "; independent verification has not started"
        return {
            "state": "waiting" if waiting else "unresolved",
            "job_state": status,
            "checks": checks,
            "message": message,
        }
    mjcf_row = stages.get("mjcf") or {}
    if mjcf_row.get("state") == "failed":
        return {
            "state": "failed", "job_state": status, "checks": checks,
            "message": ("MJCF conversion or independent checks failed; "
                        "no combined delivery is qualified. The URDF result remains separately available."),
        }
    try:
        verified_result(result)
    except ResultNotPublishable:
        pass
    else:
        if result.get("scope_kind") != "diagnostic" and result.get("formal_eligible") is not False:
            # A verified model stays previewable even when the PR service failed afterwards.
            return {
                "state": "passed",
                "job_state": status,
                "checks": checks,
            }
    if discovery.get("passed") is False:
        digest = str(discovery.get("discovery_sha256") or "")
        discovery_checks = [
            {
                "id": str(item.get("code") or ""),
                "passed": False,
                "object": str(item.get("object") or ""),
                "message": str(item.get("message") or ""),
                "evidence": _discovery_evidence(item.get("detail"), digest),
            }
            for item in discovery.get("findings") or []
            if isinstance(item, dict) and item.get("blocking", True)
        ]
        return {
            "state": "failed",
            "job_state": status,
            "checks": discovery_checks,
            "message": "Native CAD discovery failed; independent verification has not started",
        }
    if status == "failed":
        message = str(job.get("error") or "A recorded automatic check failed")
        if verification_not_started:
            message += "; independent verification has not started"
        return {
            "state": "failed",
            "job_state": status,
            "checks": checks,
            "message": message,
        }
    if status == "cancelled":
        return {
            "state": "cancelled",
            "job_state": status,
            "checks": checks,
            "message": "The run was cancelled; no completed independent verification result is available",
        }
    if quality.get("passed") is True:
        return {
            "state": "unverified",
            "job_state": status,
            "checks": checks,
            "message": "The mechanical verification result has no formal checkpoint qualification",
        }
    if status in {"pending", "queued", "running", "native_complete"}:
        active = any(
            row.get("state") == "active"
            for row in stages.values()
            if isinstance(row, Mapping)
        )
        if status == "queued":
            state = "queued"
        elif active or (not stages and status in {"running", "native_complete"}):
            state = "running"
        else:
            state = "pending"
        return {
            "state": state,
            "job_state": status,
            "checks": checks,
            "message": (
                "Independent verification has not started"
                if verification_not_started
                else "Independent verification is in progress"
            ),
        }
    return {
        "state": "unverified",
        "job_state": status,
        "checks": checks,
        "message": "No formal independent verification result is available",
    }


def _activity_summary(job: dict | None, automatic: dict) -> dict:
    """Keep recorded telemetry visible during a fact-backed authorized wait."""
    if (
        isinstance(job, dict)
        and job.get("status") == "unresolved"
        and automatic.get("state") == "waiting"
    ):
        job = {**job, "status": "running"}
    return activity_view(job)


def _canonical_native_status(projection: dict | None) -> str | None:
    """Derive retry evidence from stage decisions, never a retired native job row."""

    if not isinstance(projection, dict):
        return None
    stages = projection.get("stages")
    stages = stages if isinstance(stages, Mapping) else {}
    capture = stages.get("capture")
    capture = capture if isinstance(capture, Mapping) else {}
    if capture.get("state") == StageStatus.COMPLETED.value:
        return "satisfied"
    if capture.get("state") == StageStatus.FAILED.value:
        return "failed"
    if projection.get("state") == "running":
        return "running"
    if projection.get("state") in {"succeeded", "failed", "cancelled"}:
        return projection.get("state")
    return None


_NATIVE_JOB_STATUSES = frozenset(
    {"queued", "running", "passed", "satisfied", "native_complete", "failed"}
)
_RETRY_REFUSED_MESSAGES = {
    RECOVERY_UNKNOWN_PUBLICATION: (
        "Publication response is unknown. To prevent duplicate publication, this run cannot be "
        "retried in place; reconcile the result first and, if needed, start a linked run from a "
        "confirmed checkpoint"
    ),
    RECOVERY_ENGINEERING_TERMINAL: (
        "The engineering stage failed terminally; fix the findings and start a linked run. "
        "This run cannot be retried in place."
    ),
}


def _retry_assessment(run: dict, tasks: list[dict], job: dict | None, endpoint_error: str | None) -> RecoveryAssessment:
    states: dict[str, object] = {}
    for task in tasks:
        name = task.get("task_id")
        if not isinstance(name, str) or not name:
            return RecoveryAssessment(False, "", "unknown_failed_task", ())
        states[name] = AMBIGUOUS_TASK_STATE if name in states or task.get("map_index", -1) != -1 else task.get("state")
    # The recovery classification consumes the observed native status - the
    # registered outcome is display authority, never a stand-in for the
    # endpoint observation the classification and its availability gate need.
    status = None
    if isinstance(job, dict):
        observed = job.get("native_status", job.get("status"))
        status = observed if isinstance(observed, str) else None
    classification_status = {
        "native_complete": "satisfied",
        "passed": "satisfied",
    }.get(status, status)
    assessment = classify_recovery(run.get("state"), states, classification_status)
    if not assessment.eligible:
        return assessment
    if endpoint_error is not None and not (
        isinstance(job, dict) and isinstance(job.get("native_status"), str)
    ):
        return RecoveryAssessment(
            False,
            assessment.classification,
            "endpoint_evidence_unavailable",
            (),
        )
    if job is None:
        # The endpoint holds no job for this run: only the submission poke may
        # resume; a capture import without a job is unprovable, and an endpoint
        # error means even the absence is unconfirmed.  In the seven-stage
        # vocabulary the submission poke is the frozen admission and the native
        # discovery that carries it.
        if endpoint_error is not None:
            return RecoveryAssessment(False, assessment.classification, "endpoint_evidence_unavailable", ())
        real_failed = {name for name, value in states.items() if value == "failed"}
        if real_failed - {"freeze", "discover"}:
            return RecoveryAssessment(False, assessment.classification, "endpoint_evidence_unavailable", ())
        return assessment
    if status is None or status not in _NATIVE_JOB_STATUSES:
        return RecoveryAssessment(False, assessment.classification, "endpoint_evidence_unavailable", ())
    return assessment


def _coverage_report(job: dict | None, report: dict) -> dict:
    """Canonical check state plus the external review scope bound to this run's identity."""
    job = job if isinstance(job, dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    discovery = job.get("discovery") if isinstance(job.get("discovery"), dict) else {}
    verified = report["overall"]["engineering_state"] == "external_review"
    return {
        "structure": {
            "dag_run_id": str(job.get("dag_run_id") or ""),
            "hardware_id": str(job.get("hardware_id") or discovery.get("hardware_id") or ""),
            "revision": str(job.get("revision") or discovery.get("revision") or ""),
            "subject_sha256": str(result.get("subject_sha256") or ""),
            "discovery_sha256": str(discovery.get("discovery_sha256") or ""),
        },
        "automatic": {
            "state": report["overall"]["state"],
            "unsupported": [item["label"] for item in CONTRACT["unsupported"]],
            "message": (
                "See the per-stage reports for automatic verification; uncovered categories belong to external review."
            ),
        },
        "engineering": {
            "state": "external_review" if verified else "not_ready",
            "tracking": "external",
            "items": [
                {
                    "id": item.get("id"),
                    "label": item.get("label"),
                    "scope": item.get("scope"),
                    "automatic_exclusion": item.get("automatic_exclusion"),
                    "review_stage": item.get("review_stage"),
                    "reference": item.get("reference"),
                    "subject_sha256": result.get("subject_sha256"),
                }
                for item in CONTRACT["confirmations"]
            ],
            "message": (
                "External review tracking: the platform neither reads nor manages approvals. "
                "Unchanged facts may retain approval on rerun; changed items require confirmation."
            ),
        },
    }


class PortalApp:
    """WSGI application serving the operator page, its API and digest-verified artifacts."""

    def __init__(self, config: PortalConfig) -> None:
        self.config = config
        self.sessions = SessionStore(config.session_ttl)
        self._upload_gate = UploadGate(config.upload_concurrency)
        self._rerun_lock = threading.Lock()
        self._run_control_store = config.run_control_store
        self._platform_lock = threading.Lock()
        self._artifacts_cache = config.artifact_store

    def _run_control(self):
        """The canonical admission-control and display-metadata surface."""

        with self._platform_lock:
            if self._run_control_store is None:
                application = self.config.production_application
                facts = getattr(application, "facts", None)
                if facts is not None:
                    self._run_control_store = facts
            if self._run_control_store is None:
                raise PortalError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "The canonical run-control store is unavailable",
                )
            return self._run_control_store

    def _admission_config_digest(self) -> str:
        return _verified_admission_config_digest(self.config.config_identity_provider)

    def _artifact_store(self) -> ArtifactStore:
        if self._artifacts_cache is None:
            self._artifacts_cache = ArtifactStore.from_environment(os.environ)
        return self._artifacts_cache

    def _dispatch_technical(
        self,
        session: PortalSession,
        command_id: str,
        *,
        retry_absent: bool = False,
    ):
        application = self.config.production_application
        dispatch = getattr(application, "dispatch", None)
        if not callable(dispatch):
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "The production dispatch service is not installed",
            )
        return dispatch(
            command_id,
            AirflowDispatchAdapter(self.config.airflow, session.token),
            retry_absent=retry_absent,
        )

    def _read_fact_projection(self, run_id: str) -> dict | None:
        if self.config.run_projection_factory is not None:
            return self.config.run_projection_factory(run_id)
        dsn = os.environ.get("PLATFORM_SERVICE_DSN")
        if not dsn:
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Run facts are unavailable because PLATFORM_SERVICE_DSN is not configured",
            )
        try:
            import psycopg2

            connection = psycopg2.connect(dsn)
            try:
                return read_run_projection(
                    connection,
                    run_id,
                    artifacts=self._artifact_store(),
                )
            finally:
                connection.close()
        except RunNotAdmittedError:
            return None
        except PipelineError as error:
            raise PortalError(
                HTTPStatus.BAD_GATEWAY,
                f"Registered run evidence cannot be verified: {error}",
            ) from error
        except Exception as error:
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Run facts are temporarily unavailable; retry later",
            ) from error

    # ------------------------------------------------------------------ WSGI
    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        try:
            return self._dispatch(environ, start_response)
        except PortalError as error:
            return _json_response(start_response, error.status, {"error": error.message}, self.config)
        except Exception:  # noqa: BLE001 - a portal must fail closed, not leak internals
            log.exception("portal request failed")
            return _json_response(start_response, 500, {"error": "internal portal error"}, self.config)

    def _dispatch(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO") or "/")
        if path == "/" and method == "GET":
            return self._static(start_response, "index.html")
        if path.startswith("/static/") and method == "GET":
            return self._static(start_response, path[len("/static/") :])
        if path == "/api/simulation-capability" and method == "GET":
            self._session(environ)
            return _json_response(start_response, 200, {"available": self.config.simulation is not None}, self.config)
        if path == "/api/targets" and method == "GET":
            return self._targets(environ, start_response)
        if path == "/api/session":
            if method == "GET":
                return self._session_info(environ, start_response)
            if method == "POST":
                raise PortalError(HTTPStatus.METHOD_NOT_ALLOWED, "Request method is not allowed")
            if method == "DELETE":
                return self._logout(environ, start_response)
        if path == "/api/runs":
            if method == "GET":
                return self._list_runs(environ, start_response)
            if method == "POST":
                return self._start_run(environ, start_response)
        match = re.fullmatch(r"/api/runs/([^/]+)", path)
        if match:
            if method == "GET":
                return self._run_status(environ, start_response, match.group(1))
            if method == "PATCH":
                return self._rename_run(environ, start_response, match.group(1))
            if method == "DELETE":
                return self._delete_run(environ, start_response, match.group(1))
        match = re.fullmatch(r"/api/runs/([^/]+)/restore", path)
        if match and method == "POST":
            return self._restore_run(environ, start_response, match.group(1))
        match = re.fullmatch(r"/api/runs/([^/]+)/retry", path)
        if match and method == "POST":
            return self._retry_run(environ, start_response, match.group(1))
        match = re.fullmatch(r"/api/runs/([^/]+)/attempts", path)
        if match and method == "POST":
            return self._rerun_attempt(environ, start_response, match.group(1))
        match = re.fullmatch(r"/api/runs/([^/]+)/preview", path)
        if match and method == "GET":
            return self._preview(environ, start_response, match.group(1))
        match = re.fullmatch(r"/api/runs/([^/]+)/artifacts/(.+)", path)
        if match and method == "GET":
            return self._artifact(environ, start_response, match.group(1), match.group(2))
        raise PortalError(HTTPStatus.NOT_FOUND, "unknown portal route")

    # ------------------------------------------------------------- sessions
    def _session(self, environ: dict, *, csrf: bool = False) -> PortalSession:
        cookies = _parse_cookies(environ.get("HTTP_COOKIE"))
        session = self.sessions.get(cookies.get(self.config.session_cookie))
        if session is None:
            raise PortalError(HTTPStatus.UNAUTHORIZED, "Sign in with Feishu")
        if csrf:
            supplied = str(environ.get("HTTP_X_CSRF_TOKEN") or "")
            if not supplied or not hmac.compare_digest(supplied, session.csrf_token):
                raise PortalError(HTTPStatus.FORBIDDEN, "Request validation failed; refresh the page and retry")
        return session

    def _adopt_airflow_session(self, token: str | None) -> PortalSession | None:
        """Turn the browser's Airflow SSO cookie into a portal session after Airflow validates it.

        The JWT is never decoded or trusted here: Airflow answers with the Feishu identity only
        when the signature, expiry and tenant allowlist all pass.
        """
        if not token:
            return None
        try:
            profile = self.config.airflow.profile(token)
        except AirflowAuthError as error:
            raise PortalError(HTTPStatus.UNAUTHORIZED, "Feishu session expired; sign in again") from error
        except AirflowApiError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        open_id = profile.get("open_id")
        name = profile.get("name")
        principal = profile.get("principal")
        # The auth manager only mints sessions with a verified, recordable display name (and the
        # actor value must fit the metadata column); anything else is refused rather than shown
        # as an identifier or the viewing operator.
        if not isinstance(open_id, str) or not open_id.strip():
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Airflow returned no valid Feishu identity")
        if not isinstance(principal, str) or not principal.strip():
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Airflow returned no stable Feishu identity")
        try:
            recorded = recorded_display_name(name)
            build_actor_name(principal.strip(), recorded)
        except ValueError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Airflow returned no recordable Feishu identity") from error
        avatar = profile.get("avatar_url")
        return self.sessions.create(
            recorded,
            token,
            avatar_url=avatar.strip() if isinstance(avatar, str) else "",
            principal=principal.strip(),
        )

    def _viewer_identity(self, session: PortalSession) -> tuple[str, bool]:
        """One live profile recheck for a whole request: (principal, is_admin)."""
        try:
            profile = self.config.airflow.profile(session.token)
        except AirflowAuthError as error:
            self.sessions.drop(session.session_id)
            raise PortalError(HTTPStatus.UNAUTHORIZED, "Feishu session expired; sign in again") from error
        except AirflowApiError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        principal = profile.get("principal")
        try:
            build_actor_name(principal, recorded_display_name(profile.get("name")))
        except ValueError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Airflow returned no recordable Feishu identity") from error
        components = [profile.get(key) for key in ("app_id", "tenant_key", "open_id")]
        if (
            not all(isinstance(value, str) and value for value in components)
            or principal != ":".join(components)
            or principal != session.principal
        ):
            self.sessions.drop(session.session_id)
            raise PortalError(HTTPStatus.UNAUTHORIZED, "Feishu identity changed; sign in again")
        return principal, profile.get("role") == "ADMIN"

    def _can_manage(self, session: PortalSession, run: dict) -> bool:
        """Recheck the caller's current profile and the authoritative run actor."""
        principal, is_admin = self._viewer_identity(session)
        return is_admin or recorded_actor_principal(run.get("triggering_user_name")) == principal

    def _session_info(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        cookies = _parse_cookies(environ.get("HTTP_COOKIE"))
        session = self.sessions.get(cookies.get(self.config.session_cookie))
        headers: list[tuple[str, str]] = []
        if session is None:
            # First request after the Feishu callback: adopt the Airflow cookie and remember the
            # session in the browser with the portal's own opaque cookie.
            session = self._adopt_airflow_session(cookies.get(AIRFLOW_TOKEN_COOKIE))
            if session is None:
                raise PortalError(HTTPStatus.UNAUTHORIZED, "Sign in with Feishu")
            headers.append(("Set-Cookie", _session_cookie(session.session_id, environ, self.config)))
        return _json_response(
            start_response,
            200,
            {
                "authenticated": True,
                "user": session.user,
                "avatar_url": session.avatar_url,
                "csrf_token": session.csrf_token,
                "upload_limits": {
                    "max_files": MAX_FILES,
                    "max_bytes": MAX_TOTAL_BYTES,
                    "max_file_bytes": MAX_FILE_BYTES,
                },
            },
            self.config,
            headers,
        )

    def _logout(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        session = self._session(environ, csrf=True)
        self.sessions.drop(session.session_id)
        headers = [
            ("Set-Cookie", _session_cookie("", environ, self.config, clear=True)),
            ("Set-Cookie", _airflow_cookie_clear(environ, self.config)),
        ]
        return _json_response(start_response, 200, {"authenticated": False}, self.config, headers)

    # ------------------------------------------------------------------ runs
    def _targets(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        """The configured publication targets and the unique name-based suggestion."""
        self._session(environ)
        from urllib.parse import parse_qs

        query = parse_qs(str(environ.get("QUERY_STRING") or ""))
        folder = str((query.get("folder") or [""])[0]).strip()
        assembly = str((query.get("main_assembly") or [""])[0]).strip()
        targets = self.config.pipeline_targets or {}
        suggestion = suggest_target([folder, assembly], targets) if (folder or assembly) else None
        return _json_response(
            start_response,
            200,
            {
                "targets": [{"hardware_id": hardware, **target} for hardware, target in sorted(targets.items())],
                "suggestion": suggestion,
                "rule": "casefold_containment_unique",
                "configured": bool(targets),
            },
            self.config,
        )

    @staticmethod
    def _registered_owner(projection: dict) -> str:
        request = projection.get("request")
        owner = request.get("principal") if isinstance(request, Mapping) else None
        if not valid_actor_principal(owner):
            raise PortalError(HTTPStatus.BAD_GATEWAY, "The admitted initiator cannot be verified")
        return owner

    @staticmethod
    def _dispatch_state(projection: dict) -> str | None:
        technical = projection.get("technical")
        effects = technical.get("effects") if isinstance(technical, Mapping) else None
        matches = [
            effect for effect in effects or ()
            if isinstance(effect, Mapping) and effect.get("effect_kind") == "airflow_dispatch"
        ]
        if len(matches) != 1:
            return None
        return matches[0].get("state")

    def _list_runs(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        session = self._session(environ)
        query = urlparse.parse_qs(str(environ.get("QUERY_STRING") or ""), keep_blank_values=True)
        limit = _page_integer(query, "limit", default=20, minimum=1, maximum=100)
        offset = _page_integer(query, "offset", default=0, minimum=0, maximum=100_000)
        include_deleted = str((query.get("include_deleted") or [""])[0]).lower() in {"1", "true"}
        principal, is_admin = self._viewer_identity(session)
        metadata = self._metadata_snapshot()
        try:
            admitted = self._run_control().list_admitted_runs()
        except FactStoreError as error:
            raise PortalError(HTTPStatus.SERVICE_UNAVAILABLE, "Admitted runs are temporarily unavailable") from error
        visible = [
            item for item in admitted
            if include_deleted or not (metadata.get(item.run_id) or _EMPTY_OVERLAY)["deleted"]
        ]
        runs = []
        for item in visible[offset : offset + limit]:
            dag_run_id = self._dag_run_id_from_request_id(item.request_id)
            projection = self._read_projection(item.run_id)
            if dag_run_id is None or projection is None:
                raise PortalError(HTTPStatus.BAD_GATEWAY, "An admitted run identity cannot be verified")
            owner = self._registered_owner(projection)
            request = projection["request"]
            display_name = None
            airflow_run = {}
            try:
                airflow_run = self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
            except AirflowAuthError as error:
                raise PortalError(HTTPStatus.UNAUTHORIZED, str(error)) from error
            except AirflowApiError as error:
                if not (isinstance(error.__cause__, urlerror.HTTPError) and error.__cause__.code == 404):
                    raise PortalError(
                        HTTPStatus.SERVICE_UNAVAILABLE, "Run observations are temporarily unavailable"
                    ) from error
            if _actor_identity(airflow_run.get("triggering_user_name"))[0] == owner:
                display_name = _actor_identity(airflow_run.get("triggering_user_name"))[1]
            parent = (
                self._read_projection(item.parent_run_id)
                if item.parent_run_id else None
            )
            runs.append({
                "dag_run_id": dag_run_id,
                "handoff_path": None,
                "main_assembly": _optional_str(request.get("main_assembly")),
                "user": display_name,
                "principal": owner,
                "state": _run_state(projection),
                "automatic": {"state": "waiting"} if (
                    projection.get("state") == "unresolved"
                    and _waiting_for_effect_results(projection, datetime.now(UTC))
                ) else None,
                "started_at": airflow_run.get("start_date") or item.created_at.isoformat(),
                "parent_dag_run_id": (
                    self._dag_run_id_from_request_id(parent.get("request_id"))
                    if isinstance(parent, dict) else None
                ),
                "resume_from": item.from_stage,
                "resume_from_name": _stage_name(item.from_stage),
                "can_manage": is_admin or owner == principal,
                **(metadata.get(item.run_id) or _EMPTY_OVERLAY),
            })
        return _json_response(start_response, 200, {
            "runs": runs,
            "next_offset": offset + limit if len(visible) > offset + limit else None,
        }, self.config)

    def _start_run(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        """One uploaded SolidWorks folder starts one run; manual platform paths are gone."""
        session = self._session(environ, csrf=True)
        content_type = str(environ.get("CONTENT_TYPE") or "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise PortalError(
                HTTPStatus.BAD_REQUEST, "Upload an engineering folder with the page control before starting a run"
            )
        if self.config.upload_root is None:
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "The server has no configured upload directory; contact an administrator",
            )
        if (
            self.config.production_admission_loader is None
            or self.config.production_application is None
        ):
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "The production admission and dispatch composition is unavailable",
            )
        # The submitter identity is the strict live profile principal only - never a
        # cached session value, username fallback, or client text. Recheck it before
        # reading a single byte of a possibly multi-gigabyte upload body.
        principal, _ = self._viewer_identity(session)
        submission_key = str(environ.get("HTTP_IDEMPOTENCY_KEY") or "").strip()
        if submission_key and (
            _RUN_ID.fullmatch(submission_key) is None
            or len(submission_key) > 110
        ):
            raise PortalError(
                HTTPStatus.BAD_REQUEST,
                "Idempotency-Key must be a non-empty safe identifier of at most 110 characters",
            )
        dag_run_id = (
            f"portal-{submission_key}"
            if submission_key
            else f"portal-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(4)}"
        )
        run_id = native_run_id(dag_run_id)
        existing_outcome = None
        if (
            submission_key
            and self.config.production_application is not None
            and callable(
                getattr(
                    self.config.production_application,
                    "load_admission_outcome",
                    None,
                )
            )
        ):
            try:
                existing_outcome = (
                    self.config.production_application.load_admission_outcome(run_id)
                )
            except Exception as error:
                log.error("portal admission replay lookup failed: %s", error)
                return _json_response(
                    start_response,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": "The existing admission cannot be verified; retry later"
                    },
                    self.config,
                )
        try:
            admission = self._run_control().admission()
        except FactStoreError as error:
            log.error("admission control read failed: %s", error)
            return _json_response(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "Platform service is temporarily unavailable; retry later"},
                self.config,
            )
        if admission.state != "open" and existing_outcome is None:
            return _json_response(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": (
                        "Platform admission is closed; new run submissions "
                        "are not accepted"
                    )
                },
                self.config,
            )
        intake_run_id = (
            f"{dag_run_id}-retry-{secrets.token_hex(8)}"
            if existing_outcome is not None
            else dag_run_id
        )
        try:
            receipt = receive_folder(
                environ,
                intake_root=self.config.upload_root,
                run_id=intake_run_id,
                principal=principal,
                gate=self._upload_gate,
            )
        except UploadRejected as error:
            if error.status == 429:
                return _json_response(
                    start_response,
                    error.status,
                    {"error": error.message},
                    self.config,
                    [("Retry-After", "5")],
                )
            raise PortalError(error.status, error.message) from error
        try:
            target_hardware = receipt.target_hardware.strip() if isinstance(receipt.target_hardware, str) else ""
            if not target_hardware:
                raise PortalError(HTTPStatus.BAD_REQUEST, "Publication target is required; select one and retry")
            if target_hardware not in (self.config.pipeline_targets or {}):
                raise PortalError(HTTPStatus.BAD_REQUEST, "Publication target is not configured; select another target")
            change_summary = (
                normalize_change_summary(receipt.change_summary).strip()
                if isinstance(receipt.change_summary, str)
                else ""
            )
            if not change_summary:
                raise PortalError(HTTPStatus.BAD_REQUEST, "Change summary is required")
            purpose = receipt.purpose.strip() if isinstance(receipt.purpose, str) else ""
            if not purpose:
                raise PortalError(HTTPStatus.BAD_REQUEST, "Purpose is required; select one on the page")
            if purpose not in REQUEST_PURPOSES:
                raise PortalError(HTTPStatus.BAD_REQUEST, "Purpose is not configured; select another purpose")
        except PortalError:
            drop_received_folder(self.config.upload_root / intake_run_id)
            raise
        try:
            from ..pipeline.ingestion import (
                ProductionAdmissionRequest,
                ReceivedUploadReceipt,
            )

            production_outcome = self.config.production_admission_loader(
                ProductionAdmissionRequest(
                    run_id=run_id,
                    request_id=dag_run_id,
                    upload_dir=self.config.upload_root / intake_run_id,
                    receipt=ReceivedUploadReceipt(
                        folder=receipt.folder,
                        files=receipt.files,
                        bytes=receipt.bytes,
                        handoff_sha256=receipt.handoff_sha256,
                        main_assembly=receipt.main_assembly or "",
                        target_hardware=receipt.target_hardware or "",
                        change_summary=change_summary,
                        purpose=purpose,
                    ),
                    main_assembly=receipt.main_assembly or "",
                    target_hardware=receipt.target_hardware or "",
                    purpose=purpose,
                    change_summary=change_summary,
                    principal=principal,
                    from_stage="freeze",
                )
            )
        except Exception as error:
            drop_received_folder(self.config.upload_root / intake_run_id)
            log.error("production admission failed: %s", error)
            return _json_response(
                start_response,
                HTTPStatus.CONFLICT
                if getattr(error, "code", "") == "admission_ingestion.intent_conflict"
                else HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": "Submission admission was refused",
                    "detail": str(error)[:512],
                    "dag_run_id": dag_run_id,
                },
                self.config,
            )
        dispatch_command = getattr(production_outcome, "dispatch_command", None)
        if (
            dispatch_command is None
            or getattr(dispatch_command, "run_id", None) != run_id
            or getattr(dispatch_command, "dag_run_id", None) != dag_run_id
        ):
            return _json_response(
                start_response,
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "Admission returned a different dispatch identity",
                    "dag_run_id": dag_run_id,
                },
                self.config,
            )
        if intake_run_id != dag_run_id:
            drop_received_folder(self.config.upload_root / intake_run_id)
        try:
            dispatch_result = self._dispatch_technical(
                session,
                dispatch_command.command_id,
                retry_absent=existing_outcome is not None,
            )
        except AirflowAuthError:
            return _json_response(
                start_response,
                HTTPStatus.UNAUTHORIZED,
                {"error": "Feishu session expired; sign in again and retry", "dag_run_id": dag_run_id},
                self.config,
            )
        except (AirflowApiError, PipelineError, ValueError) as error:
            log.warning("portal upload dispatch failed dag_run_id=%s error=%s", dag_run_id, error)
            return _json_response(
                start_response,
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "Run start state is unconfirmed; the upload was retained",
                    "dag_run_id": dag_run_id,
                },
                self.config,
            )
        if dispatch_result.state is not EffectState.SATISFIED:
            log.warning(
                "portal upload trigger unresolved dag_run_id=%s trigger=%s",
                dag_run_id,
                dispatch_result.state.value,
            )
            return _json_response(
                start_response,
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": (
                        "Run start state is unconfirmed; the upload was retained. Check the run list "
                        "or contact an administrator; do not submit again."
                    ),
                    "dag_run_id": dag_run_id,
                    "trigger": dispatch_result.state.value,
                },
                self.config,
            )
        log.info(
            "portal started dag_run_id=%s user=%s principal=%s files=%d bytes=%d digest=%s",
            dag_run_id,
            session.user,
            session.principal,
            receipt.files,
            receipt.bytes,
            receipt.handoff_sha256,
        )
        payload = {
            "dag_run_id": dag_run_id,
            "folder": receipt.folder,
            "files": receipt.files,
            "bytes": receipt.bytes,
            "target_hardware": target_hardware,
            "change_summary": change_summary,
        }
        if production_outcome is not None:
            payload["request_id"] = dag_run_id
            payload["idempotent"] = existing_outcome is not None
        if receipt.main_assembly is not None:
            payload["main_assembly"] = receipt.main_assembly
        return _json_response(start_response, 201, payload, self.config)

    # ------------------------------------------------------- run metadata
    def _metadata_snapshot(self) -> dict[str, dict]:
        """Every display overlay with one platform read; failures fail closed."""
        try:
            displays = self._run_control().list_run_display_metadata()
        except FactStoreError as error:
            log.error("run display state unavailable: %s", error)
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Run metadata is temporarily unavailable; retry later"
            ) from error
        return {run_id: _display_overlay(display) for run_id, display in displays.items()}

    def _metadata_overlay(self, dag_run_id: str) -> dict:
        try:
            display = self._run_control().get_run_display_metadata(
                native_run_id(dag_run_id)
            )
        except FactStoreError as error:
            log.error("run display state unavailable: %s", error)
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Run metadata is temporarily unavailable; retry later"
            ) from error
        return _display_overlay(display) if display is not None else dict(_EMPTY_OVERLAY)

    def _require_run(self, session: PortalSession, dag_run_id: str) -> dict:
        """One authoritative Airflow run, or the existing 401/404/502 mapping."""
        try:
            return self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
        except AirflowAuthError as error:
            raise PortalError(HTTPStatus.UNAUTHORIZED, str(error)) from error
        except AirflowApiError as error:
            missing = isinstance(error.__cause__, urlerror.HTTPError) and error.__cause__.code == 404
            raise PortalError(HTTPStatus.NOT_FOUND if missing else HTTPStatus.BAD_GATEWAY, str(error)) from error

    def _actor_label(self, session: PortalSession, dag_run_id: str, run: dict) -> str:
        principal, name = _actor_identity(run.get("triggering_user_name"))
        return name or principal or session.principal

    @staticmethod
    def _dag_run_id_from_request_id(request_id: object) -> str | None:
        if not isinstance(request_id, str):
            return None
        return request_id if _RUN_ID.fullmatch(request_id) is not None else None

    def _rename_run(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        """Display-only rename; works for deleted runs and never touches native state."""
        session = self._session(environ, csrf=True)
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Run identifier is invalid")
        body = self._body(environ)
        if set(body) != {"title"}:
            raise PortalError(
                HTTPStatus.BAD_REQUEST, "Rename accepts only the title field; pass null to restore the default"
            )
        run = self._require_run(session, dag_run_id)
        if not self._can_manage(session, run):
            raise PortalError(
                HTTPStatus.FORBIDDEN, "Only the initiator or a platform administrator can rename this run"
            )
        try:
            title = normalise_title(body["title"])
        except TitleError as error:
            raise PortalError(HTTPStatus.BAD_REQUEST, str(error)) from error
        try:
            display = self._run_control().rename_run(native_run_id(dag_run_id), title)
        except RunNotAdmittedError as error:
            raise PortalError(
                HTTPStatus.NOT_FOUND, "This run has no platform registration and cannot be renamed"
            ) from error
        except FactStoreError as error:
            log.error("run display rename failed: %s", error)
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Run metadata is temporarily unavailable; retry later"
            ) from error
        return _json_response(
            start_response,
            200,
            {"dag_run_id": dag_run_id, **_display_overlay(display), "updated_at": display.updated_at.isoformat()},
            self.config,
        )

    def _delete_run(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        """Reversible tombstone: the record moves to the shared Deleted view, nothing else changes."""
        session = self._session(environ, csrf=True)
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Run identifier is invalid")
        run = self._require_run(session, dag_run_id)
        if not self._can_manage(session, run):
            raise PortalError(
                HTTPStatus.FORBIDDEN, "Only the initiator or a platform administrator can delete this run"
            )
        # Deleting must serialize with retry/rerun so a tombstone can never hide active work:
        # whoever holds this lock completes its state change before the other checks it.
        with self._rerun_lock:
            overlay = self._metadata_overlay(dag_run_id)
            if overlay["deleted"]:
                return _json_response(start_response, 200, {"dag_run_id": dag_run_id, **overlay}, self.config)
            refusal = self._delete_refusal(session, run, dag_run_id)
            if refusal is not None:
                reason, message = refusal
                return _json_response(
                    start_response, HTTPStatus.CONFLICT, {"error": message, "reason": reason}, self.config
                )
            try:
                display = self._run_control().hide_run(
                    native_run_id(dag_run_id), actor=self._actor_label(session, dag_run_id, run)
                )
            except RunNotAdmittedError as error:
                raise PortalError(
                    HTTPStatus.NOT_FOUND, "This run has no platform registration and cannot be deleted"
                ) from error
            except FactStoreError as error:
                log.error("run display hide failed: %s", error)
                raise PortalError(
                    HTTPStatus.SERVICE_UNAVAILABLE, "Run metadata is temporarily unavailable; retry later"
                ) from error
            overlay = _display_overlay(display)
        log.info("portal deleted dag_run_id=%s principal=%s", dag_run_id, session.principal)
        return _json_response(start_response, 200, {"dag_run_id": dag_run_id, **overlay}, self.config)

    def _delete_refusal(self, session: PortalSession, run: dict, dag_run_id: str) -> tuple[str, str] | None:
        """Any active or unknown execution state refuses deletion (fail closed)."""
        state = str(run.get("state") or "")
        if state in {"queued", "running"}:
            return "run_active", "The run is still executing and can be deleted only after it ends"
        if state not in {"success", "failed"}:
            return "unconfirmed", "Cannot confirm that the run ended; retry later"
        try:
            tasks = self.config.airflow.task_instances(session.token, self.config.dag_id, dag_run_id)
        except AirflowAuthError as error:
            raise PortalError(HTTPStatus.UNAUTHORIZED, str(error)) from error
        except AirflowApiError:
            return "unconfirmed", "Cannot confirm active task states; retry later"
        for task in tasks:
            task_state = task.get("state")
            if task_state in _ACTIVE_TASK_STATES:
                return "run_active", "The run still has unfinished tasks and can be deleted only after they end"
            if task_state not in _TERMINAL_TASK_STATES:
                return "unconfirmed", "Cannot confirm task states; retry later"
        projection = self._read_fact_projection(dag_run_id)
        if projection is None:
            projection = self._read_fact_projection(native_run_id(dag_run_id))
        if projection is None:
            return "registration_missing", "The run has no canonical admission facts; retry later"
        projection_state = projection.get("state")
        if projection_state == "unresolved":
            return (
                "reconciliation_required",
                "Unresolved external effects must be reconciled before deletion",
            )
        if projection_state == "running":
            return "run_active", "Canonical facts still report active work"
        technical = projection.get("technical")
        claims = technical.get("claims") if isinstance(technical, Mapping) else None
        if not isinstance(claims, list) or any(
            isinstance(claim, Mapping) and claim.get("closed_at") is None
            for claim in claims
        ):
            return "run_active", "A canonical execution claim is still open"
        if projection_state not in {"succeeded", "failed", "cancelled"}:
            return "unconfirmed", "Cannot confirm the canonical run state; retry later"
        return None

    def _restore_run(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        session = self._session(environ, csrf=True)
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Run identifier is invalid")
        run = self._require_run(session, dag_run_id)
        if not self._can_manage(session, run):
            raise PortalError(
                HTTPStatus.FORBIDDEN, "Only the initiator or a platform administrator can restore this run"
            )
        try:
            overlay = _display_overlay(
                self._run_control().restore_run(native_run_id(dag_run_id))
            )
        except RunNotAdmittedError as error:
            raise PortalError(
                HTTPStatus.NOT_FOUND, "This run has no platform registration and cannot be restored"
            ) from error
        except FactStoreError as error:
            log.error("run display restore failed: %s", error)
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Run metadata is temporarily unavailable; retry later"
            ) from error
        log.info("portal restored dag_run_id=%s principal=%s", dag_run_id, session.principal)
        return _json_response(start_response, 200, {"dag_run_id": dag_run_id, **overlay}, self.config)

    def _run_status(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        session = self._session(environ)
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Run identifier is invalid")
        viewer, is_admin = self._viewer_identity(session)
        run_id = native_run_id(dag_run_id)
        projection = self._read_projection(run_id)
        if projection is None:
            raise PortalError(HTTPStatus.NOT_FOUND, "The run has no canonical admission")
        principal = self._registered_owner(projection)
        can_manage = is_admin or viewer == principal
        airflow_run = {}
        tasks = []
        endpoint_error = None
        try:
            airflow_run = self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
        except AirflowAuthError as error:
            raise PortalError(HTTPStatus.UNAUTHORIZED, str(error)) from error
        except AirflowApiError as error:
            if not (isinstance(error.__cause__, urlerror.HTTPError) and error.__cause__.code == 404):
                raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
            endpoint_error = str(error)
        else:
            try:
                tasks = self.config.airflow.task_instances(session.token, self.config.dag_id, dag_run_id)
            except AirflowAuthError as error:
                raise PortalError(HTTPStatus.UNAUTHORIZED, str(error)) from error
            except AirflowApiError as error:
                raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        operator = (
            _actor_identity(airflow_run.get("triggering_user_name"))[1]
            if _actor_identity(airflow_run.get("triggering_user_name"))[0] == principal else None
        )
        job = self._fact_display_job(projection)
        retry_job = dict(job or {})
        retry_job["native_status"] = _canonical_native_status(projection)
        retry = _retry_assessment(airflow_run, tasks, retry_job, endpoint_error)
        recovery_block = _registered_recovery_block(projection)
        if recovery_block is not None:
            reason, classification = recovery_block
            retry = RecoveryAssessment(False, classification, reason, ())
        if self._dispatch_state(projection) in {"prepared", "unknown"}:
            retry = RecoveryAssessment(
                can_manage, "temporary_disconnection",
                "Reconcile the original Airflow dispatch; resend only after exact absence is observed", (),
            )
        pr = None
        if isinstance(job, dict) and isinstance(job.get("pr"), dict):
            pr = dict(job["pr"])
        elif isinstance(job, dict):
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            try:
                submission = check_result(
                    result,
                    expected_slug=str(job.get("repository_slug") or ""),
                    expected_base=str(job.get("repository_base") or ""),
                )["submission"]
                pr = {
                    "url": submission["url"],
                    "state": submission["state"],
                    "commit": submission["commit"],
                    "base": submission["base"],
                }
            except ResultNotPublishable:
                pr = None
        # The purpose authority of both pages is the registered freeze request: a
        # mutable conf echo never decides the display, and an unfinished
        # registration reads as "not recorded".
        stage_view_payload = self._registered_stage_view(job, projection)
        display_job = job
        try:
            report = build_report(display_job or {}, view=stage_view_payload)
        except PipelineError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        purpose = projection.get("purpose") if projection is not None else None
        request = projection.get("request") if projection is not None else None
        request = request if isinstance(request, dict) else {}
        parent_native_id = (
            projection.get("parent_run_id")
            if isinstance(projection, dict)
            else None
        )
        parent_projection = (
            self._read_fact_projection(parent_native_id)
            if isinstance(parent_native_id, str) and parent_native_id
            else None
        )
        parent_dag_run_id = (
            self._dag_run_id_from_request_id(parent_projection.get("request_id"))
            if isinstance(parent_projection, dict)
            else None
        )
        automatic = _automatic_summary(display_job, projection)
        payload = {
            "dag_run_id": dag_run_id,
            "state": _run_state(projection),
            "scope": projection.get("scope") if isinstance(projection, dict) else None,
            "formal_eligible": bool(
                isinstance(projection, dict)
                and projection.get("formal_eligible") is True
            ),
            "handoff_path": None,
            "main_assembly": _optional_str(request.get("main_assembly")),
            "purpose": purpose,
            "operator": operator,
            "principal": principal,
            "can_manage": can_manage,
            # Linked rerun lineage from canonical admission, same fields as the list rows.
            "parent_dag_run_id": parent_dag_run_id,
            "resume_from": _optional_str(
                request.get("from_stage") or (projection or {}).get("from_stage")
            ),
            "resume_from_name": _stage_name(
                request.get("from_stage") or (projection or {}).get("from_stage")
            ),
            "retry": {
                "eligible": retry.eligible,
                "reason": retry.reason,
                "classification": retry.classification,
            },
            "started_at": airflow_run.get("start_date") or projection.get("created_at"),
            "ended_at": airflow_run.get("end_date"),
            "tasks": [
                {
                    "task_id": str(task.get("task_id") or ""),
                    "state": task.get("state"),
                    "start_date": task.get("start_date"),
                    "end_date": task.get("end_date"),
                }
                for task in tasks
            ],
            "job": None
            if job is None
            else {
                "status": job.get("status"),
                "error": job.get("error") or job.get("native_error"),
                # The registered failure is the readout authority: its stable code
                # and detail travel with the same projection; a shape-only legacy
                # failure stays null instead of fabricating fields.
                "error_code": job.get("error_code"),
                "detail": job.get("detail"),
                "diagnostic_path": job.get("diagnostic_path"),
                "failure_diagnostics": job.get("failure_diagnostics"),
            },
            "discovery": None
            if not isinstance(display_job, dict) or not isinstance(display_job.get("discovery"), dict)
            else {
                "passed": display_job["discovery"].get("passed"),
                "hardware_id": display_job["discovery"].get("hardware_id"),
                "revision": display_job["discovery"].get("revision"),
                "discovery_sha256": display_job["discovery"].get("discovery_sha256"),
            },
            "stage_view": display_stage_view(stage_view_payload),
            "report": report,
            "verification_reports": self._verification_report_links(dag_run_id, projection),
            "findings": _findings(display_job),
            "activity": _activity_summary(display_job, automatic),
            "automatic": automatic,
            "coverage": _coverage_report(display_job, report),
            "pr": pr,
            "endpoint_error": endpoint_error,
            # Display rename / reversible tombstone; an unreadable ledger fails closed above.
            **self._metadata_overlay(dag_run_id),
        }
        if isinstance(projection, dict):
            payload["stage_reruns"] = self._stage_rerun_rows(projection["run_id"], principal)
            payload["execution_scope"] = projection.get("execution_scope")
            payload["delivery_complete"] = projection.get("delivery_complete") is True
        return _json_response(start_response, 200, payload, self.config)

    def _retry_run(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        session = self._session(environ, csrf=True)
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Run identifier is invalid")
        if self._body(environ):
            raise PortalError(HTTPStatus.BAD_REQUEST, "Retry does not accept run parameters")
        principal, is_admin = self._viewer_identity(session)
        run_id = native_run_id(dag_run_id)
        projection = self._read_projection(run_id)
        if projection is None:
            raise PortalError(HTTPStatus.NOT_FOUND, "The run has no canonical admission")
        if not is_admin and self._registered_owner(projection) != principal:
            raise PortalError(HTTPStatus.FORBIDDEN, "Only the initiator or a platform administrator can retry this run")
        if self._dispatch_state(projection) in {"prepared", "unknown"}:
            with self._rerun_lock:
                if self._metadata_overlay(dag_run_id)["deleted"]:
                    raise PortalError(HTTPStatus.CONFLICT, "Deleted runs cannot be retried; restore first")
                application = self.config.production_application
                loader = getattr(application, "load_admission_outcome", None)
                if not callable(loader):
                    raise PortalError(HTTPStatus.SERVICE_UNAVAILABLE, "The production admission reader is unavailable")
                outcome = loader(run_id)
                if outcome is None or outcome.dispatch_command.dag_run_id != dag_run_id:
                    raise PortalError(HTTPStatus.CONFLICT, "The original Airflow dispatch cannot be verified")
                try:
                    result = self._dispatch_technical(
                        session, outcome.dispatch_command.command_id, retry_absent=True,
                    )
                except FactStoreError as error:
                    raise PortalError(HTTPStatus.CONFLICT, str(error)) from error
                settled = result.state is EffectState.SATISFIED
                return _json_response(start_response, 200 if settled else 409, {
                    "dag_run_id": dag_run_id,
                    "state": "queued" if settled else "unresolved",
                    "trigger": result.state.value,
                    "cleared_tasks": [],
                    **({} if settled else {
                        "error": "The original dispatch remains unresolved; no new run was created"
                    }),
                }, self.config)
        try:
            run = self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
            if not self._can_manage(session, run):
                raise PortalError(
                    HTTPStatus.FORBIDDEN, "Only the initiator or a platform administrator can retry this run"
                )
            # Deleted-record refusal and the retry state change are atomic against deletion.
            with self._rerun_lock:
                if self._metadata_overlay(dag_run_id)["deleted"]:
                    return _json_response(
                        start_response,
                        HTTPStatus.CONFLICT,
                        {"error": "Deleted runs cannot be retried; restore first", "reason": "deleted"},
                        self.config,
                    )
                # Only a known live admitted registration opens the scheduling
                # recovery: decided terminal outcomes, unresolved results and
                # unproven registrations refuse BEFORE any Airflow clear
                # (dry-run included) - a stale native observation never
                # substitutes for the platform admission.
                run_id = native_run_id(dag_run_id)
                projection = self._read_projection(run_id)
                block = _registered_recovery_block(projection)
                if block is not None:
                    reason, _classification = block
                    return _json_response(
                        start_response,
                        HTTPStatus.CONFLICT,
                        {
                            "error": (
                                "This run has no platform registration that can be recovered in place "
                                "(missing, unknown, terminal, or unresolved). Clear-and-retry is not allowed. "
                                "Reconcile unresolved results; otherwise fix the registered result "
                                "and start a linked run."
                            ),
                            "reason": reason,
                        },
                        self.config,
                    )
                tasks = self.config.airflow.task_instances(session.token, self.config.dag_id, dag_run_id)
                job = self._fact_display_job(projection)
                retry_job = dict(job or {})
                retry_job["native_status"] = _canonical_native_status(projection)
                assessment = _retry_assessment(run, tasks, retry_job, None)
                if not assessment.eligible:
                    raise PortalError(
                        HTTPStatus.CONFLICT,
                        _RETRY_REFUSED_MESSAGES.get(
                            assessment.classification,
                            "This run cannot be retried; review the check results and start a new run if needed",
                        ),
                    )
                preview = self.config.airflow.clear_dag_run(session.token, self.config.dag_id, dag_run_id, dry_run=True)
                selected = preview.get("task_instances")
                if (
                    not isinstance(selected, list)
                    or type(preview.get("total_entries")) is not int
                    or preview.get("total_entries") != len(selected)
                    or any(not isinstance(task, dict) for task in selected)
                ):
                    raise PortalError(HTTPStatus.CONFLICT, "Airflow returned no valid retry-task check")
                selected_states = {}
                for task in selected:
                    name = task.get("task_id")
                    if (
                        not isinstance(name, str)
                        or name in selected_states
                        or task.get("map_index", -1) != -1
                        or task.get("state") not in {"failed", "upstream_failed"}
                        or task.get("dag_id", self.config.dag_id) != self.config.dag_id
                        or task.get("dag_run_id", dag_run_id) != dag_run_id
                    ):
                        raise PortalError(HTTPStatus.CONFLICT, "Retry tasks do not match the original run")
                    selected_states[name] = task.get("state")
                if tuple(sorted(selected_states)) != assessment.cleared_tasks:
                    raise PortalError(HTTPStatus.CONFLICT, "Run state changed; refresh and retry")
                if any(
                    selected_states[task["task_id"]] != task.get("state")
                    for task in tasks
                    if task.get("task_id") in selected_states
                ):
                    raise PortalError(HTTPStatus.CONFLICT, "Run state changed; refresh and retry")
                result = self.config.airflow.clear_dag_run(session.token, self.config.dag_id, dag_run_id, dry_run=False)
                if result.get("dag_run_id") != dag_run_id or result.get("dag_id") != self.config.dag_id:
                    raise PortalError(HTTPStatus.BAD_GATEWAY, "Airflow returned an inconsistent retry-run identity")
        except AirflowAuthError as error:
            forbidden = isinstance(error.__cause__, urlerror.HTTPError) and error.__cause__.code == 403
            raise PortalError(HTTPStatus.FORBIDDEN if forbidden else HTTPStatus.UNAUTHORIZED, str(error)) from error
        except AirflowApiError as error:
            code = error.__cause__.code if isinstance(error.__cause__, urlerror.HTTPError) else None
            status = code if code in {404, 409} else HTTPStatus.BAD_GATEWAY
            raise PortalError(status, str(error)) from error
        log.info(
            "portal retried dag_run_id=%s principal=%s tasks=%s",
            dag_run_id,
            session.principal,
            assessment.cleared_tasks,
        )
        return _json_response(
            start_response,
            200,
            {
                "dag_run_id": dag_run_id,
                "state": result.get("state"),
                "cleared_tasks": list(assessment.cleared_tasks),
            },
            self.config,
        )

    # ------------------------------------------------------------- rerun
    def _stage_rerun_rows(self, source_run_id: str, principal: str) -> list[dict]:
        application = self.config.production_application
        if application is None:
            return [
                {"stage": stage, "name": _STAGE_NAMES[stage], "eligible": False,
                 "reason": "stage_rerun_unavailable", "message": "Stage rerun service is unavailable."}
                for stage in _STAGE_IDS
            ]
        return application.stage_rerun_options(source_run_id, principal)

    def _rerun_refusal(self, start_response, reason: str, message: str) -> Iterable[bytes]:
        return _json_response(start_response, HTTPStatus.CONFLICT, {
            "error": message, "reason": reason, "message": message,
        }, self.config)

    def _rerun_attempt(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        """Admit a new immutable execution range over verified sealed inputs."""
        session = self._session(environ, csrf=True)
        principal = session.principal or ""
        if not principal:
            raise PortalError(HTTPStatus.UNAUTHORIZED, "Feishu session expired; sign in again")
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Run identifier is invalid")
        submission_key = str(environ.get("HTTP_IDEMPOTENCY_KEY") or "").strip()
        if not submission_key or _RUN_ID.fullmatch(submission_key) is None or len(submission_key) > 110:
            raise PortalError(
                HTTPStatus.BAD_REQUEST, "Idempotency-Key must be a safe identifier of at most 110 characters"
            )
        body = self._body(environ)
        if set(body) - {"from_stage", "through_stage", "change_summary", "main_assembly", "simulation"}:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Stage rerun parameters are invalid")
        stage = body.get("from_stage")
        through = body.get("through_stage", stage)
        if stage not in _STAGE_IDS or through not in _STAGE_IDS or _STAGE_IDS.index(through) < _STAGE_IDS.index(stage):
            raise PortalError(HTTPStatus.BAD_REQUEST, "Stage rerun execution range is invalid")
        for name in ("change_summary", "main_assembly", "simulation"):
            if name in body and not isinstance(body[name], str):
                raise PortalError(HTTPStatus.BAD_REQUEST, "Stage rerun parameters must be text")
        try:
            airflow_run = self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
        except AirflowAuthError as error:
            raise PortalError(HTTPStatus.UNAUTHORIZED, "Feishu session expired; sign in again") from error
        except AirflowApiError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        if not self._can_manage(session, airflow_run):
            raise PortalError(HTTPStatus.FORBIDDEN, "Only the initiator or a platform administrator can rerun a stage")
        loader = self.config.production_stage_rerun_loader
        application = self.config.production_application
        if loader is None or application is None:
            raise PortalError(HTTPStatus.SERVICE_UNAVAILABLE, "Stage rerun composition is unavailable")
        with self._rerun_lock:
            if self._metadata_overlay(dag_run_id)["deleted"]:
                return self._rerun_refusal(start_response, "deleted", "Restore the delivery before rerunning a stage")
            source_id = native_run_id(dag_run_id)
            projection = self._read_projection(source_id)
            if projection is None:
                return self._rerun_refusal(
                    start_response, "registration_missing", "Canonical source facts are unavailable"
                )
            new_dag_run_id = "portal-" + stage + "-" + submission_key
            new_run_id = native_run_id(new_dag_run_id)
            from ..pipeline.ingestion import ProductionStageRerunRequest

            try:
                existing = application.load_admission_outcome(new_run_id)
                outcome = loader(ProductionStageRerunRequest(
                    run_id=new_run_id, request_id=new_dag_run_id, parent_run_id=source_id,
                    principal=principal, from_stage=stage, through_stage=through,
                    change_summary=body.get("change_summary", projection["request"]["change_summary"]),
                    main_assembly=body.get("main_assembly"), simulation=body.get("simulation"),
                ))
            except (PipelineError, ValueError) as error:
                return self._rerun_refusal(
                    start_response, str(getattr(error, "code", "admission_refused")), str(error)[:512]
                )
            command = outcome.dispatch_command
            if command.run_id != new_run_id or command.dag_run_id != new_dag_run_id:
                return self._rerun_refusal(
                    start_response, "admission_identity_mismatch", "Stage dispatch identity differs"
                )
            try:
                dispatched = self._dispatch_technical(session, command.command_id, retry_absent=existing is not None)
            except AirflowAuthError as error:
                raise PortalError(HTTPStatus.UNAUTHORIZED, "Feishu session expired; sign in again") from error
            except (AirflowApiError, PipelineError, ValueError) as error:
                return _json_response(start_response, HTTPStatus.BAD_GATEWAY, {
                    "error": str(error)[:512], "dag_run_id": new_dag_run_id,
                    "reason": "dispatch_start_unconfirmed",
                }, self.config)
            settled = dispatched.state is EffectState.SATISFIED
        return _json_response(start_response, HTTPStatus.ACCEPTED if settled else HTTPStatus.BAD_GATEWAY, {
            "dag_run_id": new_dag_run_id, "parent_dag_run_id": dag_run_id,
            "resume_from": stage, "resume_from_name": _STAGE_NAMES[stage],
            "through_stage": through, "state": "queued" if settled else "unresolved",
            "trigger": dispatched.state.value,
            **({} if settled else {
                "error": "Start state is unconfirmed; retry this same immutable request to reconcile it."
            }),
        }, self.config)

    # --------------------------------------------------------------- preview
    def _authorize_run(self, session: PortalSession, dag_run_id: str) -> str:
        """Every preview and every artifact re-authorizes against Airflow first.

        An expired or revoked session, a deleted tenant or a run the operator
        may not read can never be served artifact or preview bytes.
        """

        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Run identifier is invalid")
        try:
            self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
        except AirflowAuthError as error:
            raise PortalError(HTTPStatus.UNAUTHORIZED, "Feishu session expired; sign in again") from error
        except AirflowApiError as error:
            raise PortalError(HTTPStatus.NOT_FOUND, "Run does not exist or access is denied") from error
        return native_run_id(dag_run_id)

    def _read_projection(self, run_id: str) -> dict | None:
        """Return the one fact projection with stage rows keyed by stage."""

        projection = self._read_fact_projection(run_id)
        if not isinstance(projection, dict):
            return None
        stages = {
            row["stage"]: ({**row["source"], "in_scope": row["in_scope"], "reuse": row["reuse"]}
                           if row.get("reuse") else row)
            for row in projection.get("stages", ())
            if isinstance(row, dict) and isinstance(row.get("stage"), str)
        }
        if not stages:
            return None
        result = {
            **projection,
            "run": {
                "run_id": projection.get("run_id"),
                "request_id": projection.get("request_id"),
                "parent_run_id": projection.get("parent_run_id"),
                "from_stage": projection.get("from_stage"),
                "state": projection.get("state"),
            },
            "stages": stages,
            "purpose": projection.get("purpose"),
        }
        return self._mjcf_input_members(result)

    @staticmethod
    def _registered_producer_run(projection: dict, row: dict) -> str:
        reuse = row.get("reuse")
        if not reuse:
            return projection["run_id"]
        command = row.get("command") or {}
        producer = reuse.get("source_run")
        if (
            reuse.get("reused") is not True or not isinstance(producer, str) or not producer
            or command.get("run_id") != producer
            or command.get("command_id") != reuse.get("source_command_id")
        ):
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered reused stage producer is inconsistent")
        return producer

    def _mjcf_input_members(self, projection: dict) -> dict:
        """Expose only the registered MJCF command's exact source closure."""
        stages = projection["stages"]
        row = stages.get("mjcf") or {}
        request = projection.get("request") or {}
        command_record = row.get("command")
        if not isinstance(command_record, dict):
            if request.get("from_stage") == "mjcf":
                raise PortalError(HTTPStatus.BAD_GATEWAY, "MJCF successor has no registered entry command")
            return projection
        try:
            command = StageCommand.from_record(command_record)
            producer_run = self._registered_producer_run(projection, row)
            if command.run_id != producer_run:
                raise PipelineError("MJCF command differs from its registered source producer")
            refs = mjcf_source_refs(command)
            by_stage = dict(projection.get("by_stage") or {})
            members = dict(projection.get("members") or {})
            for stage, ref in refs.items():
                existing = by_stage.get(stage) or {}
                if existing and self._stage_artifact_ref(existing, stage=stage) != ref:
                    raise PipelineError("MJCF source differs from the registered stage artifact")
                if existing:
                    continue
                name = {"generate": "reports/generate.json", "capture": "input/request.json",
                        "verify": CERTIFICATE_FILENAME}[stage]
                _content, _declared, manifest = self._artifact_store().read_metadata_member(
                    ref, name, max_bytes=self.config.artifact_limit,
                )
                source_members = {
                    item["path"]: {"artifact": ref.to_record(), "bytes": item["bytes"], "sha256": item["sha256"],
                                   "stage": stage, "source_run_id": ref.producer_run_id}
                    for item in manifest["members"]
                }
                by_stage[stage] = source_members
                members.update(source_members)
            if command.request["verify_certificate_sha256"] != by_stage["verify"][CERTIFICATE_FILENAME]["sha256"]:
                raise PipelineError("MJCF command names another URDF verification certificate")
            return {**projection, "stages": stages, "by_stage": by_stage, "members": members}
        except (PipelineError, KeyError, TypeError, ValueError) as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered MJCF source binding is invalid") from error

    def _fact_display_job(self, projection: dict | None) -> dict | None:
        """Build the display snapshot from StageDecision and sealed artifact facts."""

        if not isinstance(projection, dict):
            return None
        stored_activity = projection.get("activity")
        activity_record = None
        activity_history = []
        activity_final = None
        if (
            isinstance(stored_activity, Mapping)
            and stored_activity.get("schema_version") == RUN_ACTIVITY_SCHEMA
        ):
            current = stored_activity.get("current")
            activity_record = dict(current) if isinstance(current, Mapping) else None
            history = stored_activity.get("history")
            activity_history = (
                [dict(item) for item in history if isinstance(item, Mapping)]
                if isinstance(history, list)
                else []
            )
            final = stored_activity.get("final")
            activity_final = dict(final) if isinstance(final, Mapping) else None
        request = projection.get("request")
        request = request if isinstance(request, dict) else {}
        stages = projection.get("stages")
        stages = stages if isinstance(stages, dict) else {}
        failed = next(
            (
                row
                for row in stages.values()
                if isinstance(row, dict)
                and isinstance(row.get("decision"), dict)
                and row["decision"].get("outcome") != "completed"
            ),
            None,
        )
        failed_receipt = failed.get("receipt") if isinstance(failed, dict) else None
        failed_receipt = failed_receipt if isinstance(failed_receipt, dict) else {}
        failure = failed_receipt.get("failure")
        if not isinstance(failure, dict):
            failure = failed_receipt
        failure = failure if isinstance(failure, dict) else {}
        failure_message = failure.get("error_message") or failure.get("error")
        failure_code = failure.get("reason_code") or failure.get("error_code") or (
            (failed.get("decision") or {}).get("reason_code") if isinstance(failed, dict) else None
        )
        failure_detail = failure.get("error_detail") or failure.get("detail")
        failure_diagnostic_path = (
            failure.get("diagnostic_path") or failed_receipt.get("diagnostic_path")
        )
        failure_diagnostics = (
            failure.get("failure_diagnostics")
            or failed_receipt.get("failure_diagnostics")
        )
        by_stage = projection.get("by_stage")
        by_stage = by_stage if isinstance(by_stage, dict) else {}
        native_failure_member = (by_stage.get(failed.get("stage")) or {}).get("native-result.json") if failed else None
        if not failure_message and isinstance(native_failure_member, dict):
            native_result = _parse_registered_json(
                self._registered_member_bytes("native-result.json", native_failure_member)
            )
            native_failure = native_result.get("failure")
            if (
                not isinstance(native_failure, dict)
                or native_result.get("kind") != failed.get("stage")
                or native_result.get("state") != failed.get("state")
                or native_failure.get("code") != failure_code
                or not isinstance(native_failure.get("message"), str)
                or not native_failure["message"]
            ):
                raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered native failure conflicts with its stage decision")
            failure_message = native_failure["message"]
            failure_detail = native_result
        verify_row = stages.get("verify")
        verify_row = verify_row if isinstance(verify_row, dict) else {}
        qualification = verify_row.get("qualification")
        qualification = qualification if isinstance(qualification, dict) else {}
        formal_eligible = bool(
            verify_row.get("formal_eligible") is True
            and qualification.get("formal_eligible") is True
            and qualification.get("scope_kind") == "formal"
            and qualification.get("outcome") == "completed"
            and projection.get("state") != "unresolved"
        )
        scope_kind = qualification.get("scope_kind", verify_row.get("scope_kind"))
        certificate_member = (by_stage.get("verify") or {}).get(CERTIFICATE_FILENAME)
        if verify_row.get("state") == "completed" and not isinstance(certificate_member, dict):
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered completed Verify certificate is missing")
        certificate = None
        if isinstance(certificate_member, dict):
            certificate = _parse_registered_json(
                self._registered_member_bytes(CERTIFICATE_FILENAME, certificate_member)
            )
        certificate = certificate if isinstance(certificate, dict) else {}
        if isinstance(certificate_member, dict):
            try:
                self._bind_verification_certificate(projection, verify_row, certificate)
            except PipelineError as error:
                raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        formal_eligible = (
            formal_eligible and certificate.get("decision") == "PASS"
            and certificate.get("passed") is True
        )
        publish_member = (by_stage.get("publish") or {}).get("publication-receipt.json")
        if (stages.get("publish") or {}).get("state") == "completed" and not isinstance(publish_member, dict):
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered completed Publish receipt is missing")
        publication = None
        if isinstance(publish_member, dict):
            publication = _parse_registered_json(
                self._registered_member_bytes("publication-receipt.json", publish_member)
            )
            publish_row = stages.get("publish") or {}
            publish_command = publish_row.get("command") or {}
            if (
                digest(publication) != (publish_row.get("receipt") or {}).get("checks_sha256")
                or publication.get("schema_version") != PUBLICATION_RECEIPT_SCHEMA
                or not isinstance(publish_command.get("command_id"), str)
                or not isinstance(publish_command.get("request_digest"), str)
                or publication.get("command_id") != publish_command["command_id"]
                or publication.get("request_digest") != publish_command["request_digest"]
                or publication.get("run_id") != projection.get("run_id")
            ):
                raise PortalError(HTTPStatus.BAD_GATEWAY, "Publication receipt differs from its canonical stage result")
        publication = publication if isinstance(publication, dict) else {}
        pr_observation = (
            (publication.get("pr_effect") or {}).get("observation")
            if isinstance(publication.get("pr_effect"), dict)
            else None
        )
        pr_observation = pr_observation if isinstance(pr_observation, dict) else {}
        subject = certificate.get("subject_sha256")
        quality = {
            "schema_version": certificate.get("schema_version"),
            "subject_sha256": subject,
            "passed": certificate.get("passed") is True,
            "checks": certificate.get("checks") or [],
            "diagnostics": certificate.get("diagnostics") or [],
        }
        submission = None
        if (
            publication
            and isinstance(pr_observation.get("url"), str)
            and isinstance(pr_observation.get("head_oid"), str)
        ):
            submission = {
                "passed": projection.get("state") == "succeeded",
                "state": "published"
                if (publication.get("pr_effect") or {}).get("observation", {}).get("number")
                and publication.get("candidate_parents")
                else "updated",
                "url": pr_observation["url"],
                "commit": pr_observation["head_oid"],
                "base": publication.get("base_ref"),
                "branch": publication.get("branch_ref"),
                "repository_slug": publication.get("repository"),
                "subject_sha256": publication.get("publication_subject_sha256"),
            }
        result = {
            "schema_version": "solidworks-to-urdf.result/v1",
            "pipeline_id": PIPELINE_ID,
            "passed": formal_eligible and certificate.get("passed") is True,
            "subject_sha256": subject,
            "quality": quality,
            "scope_kind": scope_kind,
            "formal_eligible": formal_eligible,
        }
        if submission is not None:
            result["submission"] = submission
        events = [
            dict(event)
            for event in projection.get("events", ())
            if isinstance(event, dict)
        ]
        if failed is not None:
            events.append(
                {
                    "stage": failed.get("stage"),
                    "state": failed.get("state"),
                    **(
                        {"at": failed["updated_at"]}
                        if isinstance(failed.get("updated_at"), str)
                        else {}
                    ),
                    "error": failure_message,
                    "error_code": failure_code,
                    "detail": failure_detail,
                }
            )
        return {
            "run_id": projection.get("run_id"),
            "status": "passed" if projection.get("state") == "succeeded" else str(projection.get("state")),
            "scope": projection.get("scope"),
            "formal_eligible": formal_eligible,
            "events": events,
            "activity": activity_record,
            "activity_history": activity_history,
            "activity_final": activity_final,
            "result": result,
            "hardware_id": request.get("target_hardware"),
            "main_assembly": request.get("main_assembly"),
            "revision": None,
            "repository_slug": submission.get("repository_slug") if submission else None,
            "repository_base": submission.get("base") if submission else None,
            "pr": None
            if submission is None
            else {
                "url": submission["url"],
                "state": pr_observation.get("state"),
                "commit": submission["commit"],
                "base": submission["base"],
            },
            "error": failure_message,
            "error_code": failure_code,
            "detail": failure_detail,
            "failed_stage": failed.get("stage") if isinstance(failed, dict) else None,
            "diagnostic_path": failure_diagnostic_path,
            "failure_diagnostics": failure_diagnostics,
        }

    def _registered_stage_report(
        self,
        projection: dict,
        *,
        stage: str,
    ) -> dict | None:
        """Read a registered native-stage report and verify its sealed artifact binding."""

        stages = projection.get("stages")
        row = stages.get(stage) if isinstance(stages, dict) else None
        if not isinstance(row, dict):
            return None
        members = projection.get("by_stage", {}).get(stage)
        if not isinstance(members, dict) or not members:
            return None
        artifact = self._registered_stage_artifact(
            members,
            stage=stage,
        )
        source_run = artifact.producer_run_id
        member = members.get("reports/native-stages.json")
        if isinstance(member, dict):
            data = self._registered_member_bytes("reports/native-stages.json", member)
        else:
            return None
        report = _parse_registered_json(data)
        if (
            report.get("schema_version") != VIEW_SCHEMA
            or report.get("pipeline_id") != PIPELINE_ID
            or report.get("run_id") != source_run
            or report.get("contract_sha256") != CONTRACT_SHA256
            or report.get("contract_file_sha256") != CONTRACT_FILE_SHA256
        ):
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered stage report conflicts with its run or contract")
        return report

    @staticmethod
    def _registered_stage_artifact(
        members: dict,
        *,
        stage: str,
    ) -> ArtifactRef:
        """The immutable stage artifact that owns one registered report."""

        artifact = PortalApp._stage_artifact_ref(members, stage=stage)
        source_run = artifact.producer_run_id
        if not isinstance(source_run, str) or not source_run:
            raise PortalError(
                HTTPStatus.BAD_GATEWAY,
                "Registered stage artifact has no producer run",
            )
        return artifact

    def _registered_discovery_projection(
        self,
        projection: dict,
    ) -> dict | None:
        """Read discovery facts from their original sealed Discover output."""

        by_stage = projection.get("by_stage")
        discover = by_stage.get("discover") if isinstance(by_stage, dict) else None
        if not isinstance(discover, dict):
            return None
        path = "prepared/discovery/native-discovery.json"
        member = discover.get(path)
        if not isinstance(member, dict):
            if ((projection.get("stages") or {}).get("discover") or {}).get("state") == "completed":
                raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered completed Discover source evidence is missing")
            return None
        try:
            source = self._registered_stage_artifact(discover, stage="discover")
            data = self._registered_member_bytes(
                path,
                member,
            )
            record = registered_discovery_record(
                _parse_registered_json(data)
            )
            findings = registered_discovery_findings({"discovery": record})
            if record["run_id"] != source.producer_run_id:
                raise PipelineError(
                    "Registered discovery record belongs to another producer run"
                )
        except PipelineError as error:
            raise PortalError(
                HTTPStatus.BAD_GATEWAY,
                str(error),
            ) from error
        identity = record["identity"]
        return {
            "findings": findings,
            "passed": not any(item["blocking"] for item in findings),
            "hardware_id": identity.get("hardware_id"),
            "revision": identity.get("revision"),
            "discovery_sha256": hashlib.sha256(data).hexdigest(),
        }

    def _registered_stage_view(self, job: dict | None, projection: dict | None) -> dict:
        """Project committed stage decisions and their sealed evidence without re-deciding outcomes."""

        view = stage_view()
        if not isinstance(projection, dict):
            return view
        job = job if isinstance(job, dict) else {}
        run_id = projection.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered run identity is missing")
        result = job.get("result") or {}
        view.update(run_id=run_id, subject_sha256=result.get("subject_sha256"))
        stages = projection.get("stages") or {}
        by_stage = projection.get("by_stage") or {}
        for stage in view["stages"]:
            stage_id = stage["id"]
            row = stages.get(stage_id) or {}
            state = stage_display_state(row.get("state", "waiting"))
            at = row.get("updated_at") or row.get("original_executed_at")
            if hasattr(at, "isoformat"):
                at = at.isoformat()
            stage.update(
                state=state, at=at, in_scope=row.get("in_scope") is not False,
                input_qc=[], output_qc=[], inputs=[], outputs=[], findings=[],
            )
            if row.get("reuse"):
                stage["reuse"] = row["reuse"]
            command = row.get("command") or {}
            stage["inputs"] = [{
                "path": artifact["locator"], "label": artifact["kind"], "availability": "recorded",
            } for artifact in command.get("input_artifacts", [])]
            members = by_stage.get(stage_id) or {}
            if members:
                artifact = self._registered_stage_artifact(members, stage=stage_id)
                if artifact.producer_run_id != self._registered_producer_run(projection, row):
                    raise PortalError(
                        HTTPStatus.BAD_GATEWAY, "Registered stage artifact differs from its proven producer"
                    )
                stage["outputs"] = [{
                    "path": ".", "label": "Sealed stage output", "availability": "recorded",
                    "files": {name: member["sha256"] for name, member in members.items()},
                }]
            if state == "completed" and not members:
                raise PortalError(HTTPStatus.BAD_GATEWAY, f"Registered {stage_id} output evidence is missing")
            if stage_id in {"freeze", "discover"} and members:
                member = members.get("checks.json")
                if not isinstance(member, dict):
                    raise PortalError(HTTPStatus.BAD_GATEWAY, f"Registered {stage_id} checks are missing")
                checks = _parse_registered_json(self._registered_member_bytes("checks.json", member))
                receipt = row.get("receipt") or {}
                if (
                    not isinstance(command.get("command_id"), str)
                    or not isinstance(command.get("request_digest"), str)
                    or checks.get("command_id") != command.get("command_id")
                    or checks.get("request_digest") != command.get("request_digest")
                    or digest(checks) != receipt.get("checks_sha256")
                ):
                    raise PortalError(HTTPStatus.BAD_GATEWAY, f"Registered {stage_id} checks conflict with its result")
                recorded = checks.get("checks")
                if not isinstance(recorded, list) or not recorded:
                    raise PortalError(HTTPStatus.BAD_GATEWAY, f"Registered {stage_id} check inventory is malformed")
                identifiers = set()
                for check in recorded:
                    if (
                        not isinstance(check, dict) or not isinstance(check.get("id"), str)
                        or not check["id"] or check["id"] in identifiers
                        or check.get("state") not in {"passed", "failed"}
                        or not isinstance(check.get("details"), dict)
                    ):
                        raise PortalError(
                            HTTPStatus.BAD_GATEWAY, f"Registered {stage_id} check observation is malformed"
                        )
                    identifiers.add(check["id"])
                    stage["output_qc"].append({**check, "boundary": "stage"})
                if stage_id == "freeze":
                    inventory_check = next((item for item in recorded if item["id"] == "handoff.integrity"), {})
                    files = (inventory_check.get("details") or {}).get("files")
                    if isinstance(files, dict):
                        stage["input_inventory_count"] = len(files)
                        view["handoff_sha256"] = (inventory_check.get("details") or {}).get("handoff_sha256")
            elif stage_id == "capture" and state == "completed":
                # The native report remains detailed Capture evidence, never a stage-state authority.
                report = self._registered_stage_report(projection, stage=stage_id)
                if not isinstance(report, dict):
                    raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered native Capture observations are missing")
                captured = next((item for item in report.get("stages", []) if item.get("id") == stage_id), None)
                if not isinstance(captured, dict):
                    raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered native Capture observations are malformed")
                for boundary in ("input", "output"):
                    stage[f"{boundary}_qc"] = [dict(item) for item in captured.get(f"{boundary}_qc", [])]
                handoff_member = members.get("input/handoff.json")
                if isinstance(handoff_member, dict):
                    handoff = _parse_registered_json(
                        self._registered_member_bytes("input/handoff.json", handoff_member)
                    )
                    try:
                        warnings = registered_handoff_warnings(handoff.get("warnings"))
                    except PipelineError as error:
                        raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
                    for check in stage["input_qc"]:
                        if check.get("id") == "input.valid":
                            check["details"] = {**check.get("details", {}), "warnings": warnings}
            if stage_id == "mjcf" and mjcf_contract.REPORT_PATH in members:
                certificate = mjcf_certificate_from_bytes(
                    self._registered_member_bytes(mjcf_contract.REPORT_PATH, members[mjcf_contract.REPORT_PATH]),
                    StageCommand.from_record(command), require_pass=state == "completed",
                )
                if digest(certificate) != (row.get("receipt") or {}).get("checks_sha256"):
                    raise PortalError(HTTPStatus.BAD_GATEWAY, "MJCF check evidence differs from the stage result")
                stage["output_qc"] = [{
                    **item, "boundary": "stage", "state": "passed" if item["passed"] else "failed",
                } for item in certificate["report"]["checks"]]
            checks = stage["input_qc"] + stage["output_qc"]
            stage.update(checks_total=len(checks), checks_passed=sum(item.get("state") == "passed" for item in checks))
            if state == "failed":
                decision = row.get("decision") or {}
                stage["error_code"] = decision.get("reason_code")
                if job.get("failed_stage") == stage_id:
                    stage.update(error=job.get("error"), diagnostic=job.get("detail"))
        view["execution_scope"] = [stage["id"] for stage in view["stages"] if stage["in_scope"]]
        discovery = self._registered_discovery_projection(projection)
        if discovery is not None:
            job["discovery"] = discovery
            job["hardware_id"] = discovery["hardware_id"]
            job["revision"] = discovery["revision"]
        if any(stage["id"] == "verify" and stage["state"] == "completed" for stage in view["stages"]):
            for stage in view["stages"]:
                for confirmation in stage["confirmations"]:
                    confirmation["state"] = "external_review"
        return view

    @staticmethod
    def _registered_member_fields(member: dict) -> tuple[str, int, dict]:
        """The digest, byte length and immutable artifact of one registered member."""

        expected = member.get("sha256")
        length = member.get("bytes")
        artifact = member.get("artifact")
        if not (
            isinstance(expected, str)
            and _SHA256_TEXT.fullmatch(expected)
            and type(length) is int
            and length >= 0
            and isinstance(artifact, dict)
        ):
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Sealed-file registration is incomplete")
        return expected, length, artifact

    def _registered_member_bytes(self, path: str, member: dict) -> bytes:
        """One registered member's sealed bytes, verified against its registration.

        The one check both read surfaces share: the artifact download and the
        preview's evidence parsing (quality report, warnings) consume the same
        registered digest, length and checkpoint, so replaced, missing or
        incompletely registered bytes refuse identically with the fixed 502
        classes - the preview can never self-sign a serving decision or degrade
        to empty findings.
        """

        expected, length, artifact_record = self._registered_member_fields(member)
        try:
            artifact = ArtifactRef.from_record(artifact_record)
            data, _declared, _manifest = self._artifact_store().read_metadata_member(
                artifact, path, max_bytes=length,
            )
        except (PipelineError, OSError, TypeError, ValueError) as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Delivery artifact digest verification failed") from error
        if len(data) != length or hashlib.sha256(data).hexdigest() != expected:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Delivery artifact digest verification failed")
        return data

    @staticmethod
    def _stage_artifact_ref(members: dict, *, stage: str) -> ArtifactRef:
        """The one sealed output artifact behind a stage projection."""

        records = []
        for member in members.values():
            artifact = member.get("artifact") if isinstance(member, dict) else None
            if not isinstance(artifact, dict):
                raise PipelineError(f"The {stage} stage registration has no artifact identity")
            records.append(ArtifactRef.from_record(artifact).to_record())
        unique = {canonical_json_value(record) for record in records}
        if len(unique) != 1:
            raise PipelineError(f"The {stage} stage registration spans multiple artifacts")
        artifact = ArtifactRef.from_record(records[0])
        expected_kind = f"stage_output:{stage}"
        if artifact.kind != expected_kind or artifact.producer_stage != stage:
            raise PipelineError(f"The {stage} stage artifact has the wrong identity")
        return artifact

    @staticmethod
    def _verification_input_pair(
        verify_row: dict,
        by_stage: dict,
    ) -> tuple[ArtifactRef, ArtifactRef]:
        """The Generate/Capture pair named by Verify, independent of copied members."""

        generate_members = by_stage.get("generate")
        capture_members = by_stage.get("capture")
        if isinstance(generate_members, dict) and generate_members:
            generate = PortalApp._stage_artifact_ref(generate_members, stage="generate")
        else:
            generate = None
        if isinstance(capture_members, dict) and capture_members:
            capture = PortalApp._stage_artifact_ref(capture_members, stage="capture")
        else:
            capture = None
        command = verify_row.get("command")
        inputs = command.get("input_artifacts") if isinstance(command, dict) else None
        if isinstance(inputs, list):
            refs = [ArtifactRef.from_record(item) for item in inputs]
            named_generate = [item for item in refs if item.kind == "stage_output:generate"]
            named_capture = [item for item in refs if item.kind == "stage_output:capture"]
            if len(named_generate) == 1:
                if generate is not None and generate != named_generate[0]:
                    raise PipelineError("The Verify command names another Generate artifact")
                generate = named_generate[0]
            if len(named_capture) == 1:
                if capture is not None and capture != named_capture[0]:
                    raise PipelineError("The Verify command names another Capture artifact")
                capture = named_capture[0]
        if generate is None or capture is None or generate.artifact_id == capture.artifact_id:
            raise PipelineError("The Verify command has no distinct Generate/Capture input pair")
        return generate, capture

    @staticmethod
    def _pair_subject_from_members(
        generate_members: dict,
        capture_members: dict,
    ) -> str | None:
        """Recompute the Verify evaluation subject from registered member digests."""

        if not generate_members or not capture_members:
            return None
        files: dict[str, str] = {}
        for path, member in {**capture_members, **generate_members}.items():
            if not (
                path == "tool.json"
                or path.startswith(
                    ("input/", "evidence/", "model/", "urdf/", "meshes/")
                )
            ):
                continue
            sha256 = member.get("sha256") if isinstance(member, dict) else None
            if not isinstance(sha256, str) or _SHA256_TEXT.fullmatch(sha256) is None:
                raise PipelineError("The Verify pair carries a malformed member digest")
            files[path] = sha256
        if not files:
            raise PipelineError("The Verify pair carries no subject members")
        return digest(files)

    def _bind_verification_certificate(
        self,
        projection: dict,
        verify_row: dict,
        certificate: dict,
    ) -> tuple[ArtifactRef, ArtifactRef, str | None]:
        """Bind a certificate to this projection's exact Generate/Capture pair."""

        try:
            validate_certificate_structure(certificate)
        except PipelineError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        if digest(certificate) != (verify_row.get("receipt") or {}).get("checks_sha256"):
            raise PipelineError("The verification certificate differs from its stage result")
        by_stage = projection.get("by_stage")
        by_stage = by_stage if isinstance(by_stage, dict) else {}
        generate, capture = self._verification_input_pair(verify_row, by_stage)
        certificate_generate = ArtifactRef.from_record(certificate.get("generate_artifact"))
        certificate_capture = ArtifactRef.from_record(certificate.get("capture_artifact"))
        if certificate_generate != generate or certificate_capture != capture:
            raise PipelineError("The verification certificate names another Generate/Capture pair")
        if certificate.get("subject_sha256") != generate.set_sha256:
            raise PipelineError("The verification certificate subject differs from Generate")
        request = certificate.get("request")
        if not isinstance(request, dict) or (
            request.get("generate_artifact") != generate.to_record()
            or request.get("capture_artifact") != capture.to_record()
        ):
            raise PipelineError("The verification request differs from the Generate/Capture pair")

        generate_members = by_stage.get("generate")
        generate_members = generate_members if isinstance(generate_members, dict) else {}
        capture_members = by_stage.get("capture")
        capture_members = capture_members if isinstance(capture_members, dict) else {}
        verify_members = by_stage.get("verify")
        verify_members = verify_members if isinstance(verify_members, dict) else {}
        if not generate_members:
            generate_members = {
                path: member
                for path, member in verify_members.items()
                if path == "tool.json"
                or path.startswith(("model/", "urdf/", "meshes/"))
            }
        if not capture_members:
            capture_members = {
                path: member
                for path, member in verify_members.items()
                if path.startswith(("input/", "evidence/"))
            }
        evaluation_subject = self._pair_subject_from_members(generate_members, capture_members)
        if (
            evaluation_subject is not None
            and certificate.get("evaluation_subject_sha256") != evaluation_subject
        ):
            raise PipelineError("The verification certificate evaluation subject differs")

        receipt_member = generate_members.get(
            "reports/generate.json",
            verify_members.get("reports/generate.json"),
        )
        if isinstance(receipt_member, dict):
            receipt_bytes = self._registered_member_bytes("reports/generate.json", receipt_member)
            if certificate.get("generate_receipt_sha256") != hashlib.sha256(receipt_bytes).hexdigest():
                raise PipelineError("The verification certificate names another Generate receipt")
            receipt = _parse_registered_json(receipt_bytes)
            if (
                not isinstance(receipt, dict)
                or receipt.get("capture_artifact") != capture.to_record()
                or receipt.get("config_snapshot_sha256") != certificate.get("config_snapshot_sha256")
                or receipt.get("contract_sha256") != certificate.get("contract_sha256")
                or receipt.get("actual_executor_sha256") != generate.producer_tool_sha256
                or receipt.get("required_executor_sha256") != generate.producer_tool_sha256
            ):
                raise PipelineError("The Generate receipt differs from the verification pair")
        return generate, capture, evaluation_subject

    def _mjcf_preview_from_projection(self, projection: dict) -> dict | None:
        """Bind the actual checked MJCF bytes without replaying native compilation."""
        row = (projection.get("stages") or {}).get("mjcf") or {}
        if row.get("state") != "completed":
            return None
        by_stage = projection.get("by_stage") or {}
        output = by_stage.get("mjcf") or {}
        member = output.get(mjcf_contract.REPORT_PATH)
        if not isinstance(member, dict):
            raise PipelineError("Completed MJCF stage has no registered certificate")
        command = StageCommand.from_record(row.get("command"))
        certificate = mjcf_certificate_from_bytes(
            self._registered_member_bytes(mjcf_contract.REPORT_PATH, member), command,
        )
        if digest(certificate) != (row.get("receipt") or {}).get("checks_sha256"):
            raise PipelineError("MJCF certificate differs from its registered stage result")
        output_ref = self._stage_artifact_ref(output, stage="mjcf")
        if (output_ref.producer_run_id != command.run_id
                or output_ref.producer_tool_sha256 != command.expected_executor_sha256):
            raise PipelineError("MJCF artifact belongs to another producer")
        for stage, ref in mjcf_source_refs(command).items():
            if ref != self._stage_artifact_ref(by_stage.get(stage) or {}, stage=stage):
                raise PipelineError("MJCF preview names another source artifact")
        files = {}
        for name, source in (by_stage.get("generate") or {}).items():
            if not name.startswith(("urdf/", "meshes/")) and name != mjcf_contract.COMPONENT_SOURCE:
                continue
            sha, size, _ref = self._registered_member_fields(source)
            files[name] = {"sha256": sha, "bytes": size}
        for name, source in output.items():
            if name == mjcf_contract.REPORT_PATH:
                continue
            if not name.startswith("mjcf/"):
                raise PipelineError("MJCF output has a member outside its projection contract")
            sha, size, _ref = self._registered_member_fields(source)
            files[name] = {"sha256": sha, "bytes": size}
        components = mjcf_contract.COMPONENT_SOURCE if mjcf_contract.COMPONENT_SOURCE in files else None
        actual_binding = mjcf_contract.binding_for_files(files, components=components)
        if actual_binding != certificate["input_binding"]:
            raise PipelineError("MJCF preview file inventory or engine differs from its certificate")
        projected = {name: record for name, record in files.items() if name.startswith("mjcf/")}
        if digest(projected) != certificate["projection_sha256"]:
            raise PipelineError("MJCF preview subject differs from its certificate")
        if self._registered_member_bytes(mjcf_contract.CONFIG_NAME, output[mjcf_contract.CONFIG_NAME]) \
                != command.request["simulation"].encode("utf-8"):
            raise PipelineError("MJCF preview simulation configuration differs from its request")
        qualification = row.get("qualification") or {}
        formal = (row.get("formal_eligible") is True and qualification.get("formal_eligible") is True
                  and qualification.get("scope_kind") == "formal" and qualification.get("outcome") == "completed")
        diagnostic = (qualification.get("scope_kind") == "diagnostic"
                      and qualification.get("outcome") == "completed"
                      and (projection.get("scope") or {}).get("kind") == "diagnostic")
        if not formal and not diagnostic:
            raise PipelineError("Completed MJCF output has no matching stage qualification")
        verified = formal and projection.get("state") != "unresolved"
        report_sha, report_bytes, report_ref = self._registered_member_fields(member)
        warning_inventory = text_warning_inventory(mjcf_contract.projection_warnings(certificate), {
            "artifact": report_ref.to_record(), "path": mjcf_contract.REPORT_PATH,
            "bytes": report_bytes, "sha256": report_sha,
        })
        return {
            "entry": mjcf_contract.SCENE_NAME if mjcf_contract.SCENE_NAME in files else mjcf_contract.ROBOT_NAME,
            "files": {name: record["sha256"] for name, record in files.items()},
            "bytes": sum(record["bytes"] for record in files.values()),
            "engine": actual_binding["engine"], "subject_sha256": certificate["projection_sha256"],
            "certificate": {"path": mjcf_contract.REPORT_PATH, "sha256": member["sha256"]},
            "verified": verified, "formal_eligible": verified, "mechanical_passed": True,
            "state": "verified" if verified else "unverified",
            "verification_scope": qualification["scope_kind"],
            "warning_inventory": warning_inventory,
            "collision": next((item["details"] for item in certificate["report"]["checks"]
                               if item["id"] == "collision.hulls"), {}),
            "facts": certificate["report"]["facts"], "scope": certificate["report"]["scope"],
        }

    def _preview_from_projection(self, run_id: str, projection: dict) -> dict | None:
        payload = self._urdf_preview_from_projection(run_id, projection)
        mjcf = self._mjcf_preview_from_projection(projection)
        if payload is None:
            if mjcf is None:
                return None
            payload = {
                "schema_version": "solidworks-to-urdf.preview/v2", "pipeline_id": PIPELINE_ID,
                "run_id": run_id, "purpose": projection.get("purpose"), "urdf": None,
                "files": mjcf["files"], "report_files": {mjcf["certificate"]["path"]: mjcf["certificate"]["sha256"]},
                "subject_sha256": mjcf["subject_sha256"], "verified": mjcf["verified"],
                "formal_eligible": mjcf["formal_eligible"], "mechanical_passed": True,
                "state": mjcf["state"], "verification_scope": mjcf["verification_scope"],
                "warnings": [], "warnings_total": 0, "warning_categories_total": 0,
                "reason": "diagnostic_scope" if mjcf["verification_scope"] == "diagnostic" else "run_unresolved",
                "error": "MJCF structural checks passed; formal delivery eligibility is not established",
            }
        payload["mjcf"] = mjcf
        if mjcf is not None:
            payload["report_files"][mjcf["certificate"]["path"]] = mjcf["certificate"]["sha256"]
        return payload

    def _urdf_preview_from_projection(self, run_id: str, projection: dict) -> dict | None:
        """One preview payload over the registered members of this attempt.

        A completed verify stage serves the verified preview; a failed verify
        stage with registered members serves the explicitly unverified
        diagnostic preview; anything else returns ``None`` (readiness).
        """

        stages = projection.get("stages")
        verify = stages.get("verify") if isinstance(stages, dict) else None
        state = verify.get("state") if isinstance(verify, dict) else None
        by_stage = projection.get("by_stage")
        by_stage = by_stage if isinstance(by_stage, dict) else {}
        generate_members = by_stage.get("generate") or {}
        capture_members = by_stage.get("capture") or {}
        verify_members = by_stage.get("verify") or {}
        if state != "completed" and not (state == "failed" and verify_members):
            return None
        members = {**capture_members, **generate_members, **verify_members}
        certificate_member = verify_members.get(CERTIFICATE_FILENAME)
        if not isinstance(certificate_member, dict):
            raise PipelineError("The Verify artifact carries no verification certificate")
        certificate = _parse_registered_json(
            self._registered_member_bytes(CERTIFICATE_FILENAME, certificate_member)
        )
        if not isinstance(certificate, dict):
            raise PipelineError("Verification certificate must be an object")
        generate_artifact, capture_artifact, evaluation_subject = self._bind_verification_certificate(
            projection,
            verify,
            certificate,
        )
        mechanical_passed = (
            certificate.get("decision") == "PASS"
            and certificate.get("passed") is True
        )
        qualification = verify.get("qualification")
        qualification = qualification if isinstance(qualification, dict) else {}
        scope_kind = qualification.get("scope_kind", verify.get("scope_kind"))
        run_unresolved = projection.get("state") == "unresolved"
        formal_eligible = bool(
            state == "completed"
            and mechanical_passed
            and qualification.get("formal_eligible") is True
            and scope_kind == "formal"
            and verify.get("formal_eligible") is True
            and not run_unresolved
        )
        if state == "completed" and mechanical_passed and qualification.get("formal_eligible") is not True:
            run_scope = projection.get("scope")
            if not (
                isinstance(run_scope, dict)
                and run_scope.get("kind") == "diagnostic"
                and scope_kind == "diagnostic"
            ):
                raise PipelineError("The completed Verify PASS has no formal qualification")
        verified = formal_eligible
        # Every advertised member must be completely registered before any byte is
        # served or parsed: an incomplete registration refuses outright, never a
        # half-signed projection.
        for member in members.values():
            self._registered_member_fields(member)
        files = {
            path: member["sha256"]
            for path, member in members.items()
            if _member_class(path) == "subject"
            and (
                path == "tool.json"
                or path.startswith(("model/", "urdf/", "meshes/"))
            )
        }
        report_files = {
            path: member["sha256"]
            for path, member in members.items()
            if _member_class(path) == "report"
            or path in {CERTIFICATE_FILENAME, EXECUTION_RECEIPT_FILENAME}
        }
        if "urdf/robot.urdf" not in files:
            raise PipelineError("The registered delivery carries no urdf/robot.urdf member")
        payload = {
            "schema_version": "solidworks-to-urdf.preview/v2",
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "purpose": self._registered_purpose(projection),
            "subject_sha256": certificate.get("subject_sha256"),
            "urdf": "urdf/robot.urdf",
            "files": files,
            "report_files": report_files,
            "verified": verified,
            "state": "verified" if verified else "unverified",
            "mechanical_passed": mechanical_passed,
            "verification_scope": scope_kind,
            "formal_eligible": formal_eligible,
            "qualification_digest": qualification.get("qualification_digest"),
            "generate_artifact_id": generate_artifact.artifact_id,
            "capture_artifact_id": capture_artifact.artifact_id,
            "evaluation_subject_sha256": evaluation_subject or certificate.get("evaluation_subject_sha256"),
        }
        if verified:
            warnings = _preview_warnings(_quality_warnings(certificate))
            warning_groups = warning_summaries(warnings, certificate=certificate)
            payload.update(
                {
                    "warnings": warning_groups,
                    "warnings_total": len(warnings),
                    "warning_categories_total": len(warning_groups),
                }
            )
        else:
            if state == "failed" and mechanical_passed:
                # A failed-verify registration whose report claims success contradicts
                # the registered stage outcome: the diagnostic is refused, while the
                # failed products stay registered and downloadable.
                raise PortalError(
                    HTTPStatus.BAD_GATEWAY, "Registered diagnostic report conflicts with the failed-stage registration"
                )
            failing = _failing_checks(certificate)
            warnings = _preview_warnings(
                _quality_warnings(certificate),
                _delivery_warnings(
                    {
                        path: self._registered_member_bytes(path, member)
                        for path, member in members.items()
                        if path
                        in {
                            "input/handoff.json",
                            "input/discovery/native-discovery.json",
                        }
                    }
                ),
            )
            warning_groups = warning_summaries(warnings, certificate=certificate)
            payload.update(
                {
                    "reason": (
                        "run_unresolved"
                        if run_unresolved
                        else (
                            "diagnostic_scope"
                            if state == "completed" and mechanical_passed
                            else "verification_failed"
                        )
                    ),
                    "error": (
                        "The run has unresolved external effects and cannot establish a formal checkpoint"
                        if run_unresolved
                        else (
                            "This diagnostic replay passed mechanical verification but is not eligible "
                            "for a formal checkpoint or publication"
                        )
                        if state == "completed" and mechanical_passed
                        else str(
                            (verify.get("receipt") or {}).get("error")
                            or certificate.get("diagnostics")
                            or ""
                        )
                    ),
                    "failing_checks": failing[:_PREVIEW_LIMIT],
                    "failing_checks_total": len(failing),
                    "warnings": warning_groups,
                    "warnings_total": len(warnings),
                    "warning_categories_total": len(warning_groups),
                }
            )
        return payload

    def _registered_purpose(self, projection: dict) -> str | None:
        """The declared purpose of the registered freeze request, for display only.

        The value is read from the registered ``input/request.json`` member (the
        same store-side request record the freeze stage sealed); a missing,
        unreadable or undeclared purpose yields ``None`` so the display layer
        states "not recorded" instead of guessing.  D's sourcing and validation stay
        untouched - this is a read for the run/preview payloads.
        """

        purpose = projection.get("purpose")
        return purpose if purpose in REQUEST_PURPOSES else None

    def _preview_payload(self, session: PortalSession, dag_run_id: str, *, missing_ok: bool = False) -> dict | None:
        run_id = self._authorize_run(session, dag_run_id)
        projection = self._read_projection(run_id)
        preview = None
        failure: PipelineError | None = None
        if projection is not None:
            try:
                preview = self._preview_from_projection(run_id, projection)
            except PipelineError as error:
                failure = error
        if preview is None:
            if missing_ok:
                return None
            if projection is not None and projection["members"]:
                detail = f": {failure}" if failure is not None else ""
                raise PortalError(HTTPStatus.CONFLICT, f"The model has not passed independent verification{detail}")
            raise PortalError(HTTPStatus.NOT_FOUND, "This run has no verified delivery to display")
        return preview

    def _preview(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        session = self._session(environ)
        return _json_response(start_response, 200, self._preview_payload(session, dag_run_id), self.config)

    def _artifact(self, environ: dict, start_response: Callable, dag_run_id: str, name: str) -> Iterable[bytes]:
        session = self._session(environ)
        try:
            artifact = validate_artifact_name(name)
        except EndpointProtocolError as error:
            raise PortalError(HTTPStatus.BAD_REQUEST, str(error)) from error
        run_id = self._authorize_run(session, dag_run_id)
        projection = self._read_projection(run_id)
        member = projection["members"].get(artifact) if projection is not None else None
        if member is None:
            raise PortalError(HTTPStatus.NOT_FOUND, "This file is not registered as a sealed artifact of the run")
        data = self._registered_member_bytes(artifact, member)
        content_type = _ARTIFACT_TYPES.get(Path(artifact).suffix.lower(), "application/octet-stream")
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "private, max-age=300"),
        ]
        start_response("200 OK", _common_headers(headers, self.config))
        return [data]

    def _verification_report_links(self, dag_run_id: str, projection: dict | None) -> list[dict]:
        """Downloadable entries of the registered verification evidence.

        The run page links these directly through the authorized artifact route; the
        same resolver backs the download so a link can never point at a missing member.
        """

        if _RUN_ID.fullmatch(dag_run_id) is None:
            return []
        resolved = projection["members"] if projection is not None else {}
        return [
            {
                "path": artifact,
                "label": _VERIFICATION_REPORT_LABELS[artifact],
                "url": f"/api/runs/{dag_run_id}/artifacts/{artifact}",
            }
            for artifact in _VERIFICATION_REPORT_LABELS
            if artifact in resolved
        ]

    # ---------------------------------------------------------------- static
    def _static(self, start_response: Callable, name: str) -> Iterable[bytes]:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or name.startswith("/"):
            raise PortalError(HTTPStatus.NOT_FOUND, "unknown static asset")
        root = self.config.static_dir.resolve()
        target = (root / relative).resolve()
        if not target.is_file() or root not in target.parents:
            raise PortalError(HTTPStatus.NOT_FOUND, "unknown static asset")
        content_type = _STATIC_TYPES.get(target.suffix.lower())
        if content_type is None:
            raise PortalError(HTTPStatus.NOT_FOUND, "unknown static asset")
        data = target.read_bytes()
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "no-cache"),
        ]
        start_response("200 OK", _common_headers(headers, self.config, worker=name == "mjcf_worker.js"))
        return [data]

    # ---------------------------------------------------------------- helpers
    def _body(self, environ: dict) -> dict:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError) as error:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Request length is invalid") from error
        if length < 0 or length > self.config.max_body_bytes:
            raise PortalError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body is too large")
        raw = environ["wsgi.input"].read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Request body is not valid JSON") from error
        if not isinstance(payload, dict):
            raise PortalError(HTTPStatus.BAD_REQUEST, "Request body is not a JSON object")
        return payload


def _parse_cookies(header: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in str(header or "").split(";"):
        name, _, value = chunk.strip().partition("=")
        if name:
            cookies[name] = value
    return cookies


def _request_is_secure(environ: dict) -> bool:
    if str(environ.get("HTTP_X_FORWARDED_PROTO") or "").lower() == "https":
        return True
    if str(environ.get("HTTP_X_FORWARDED_SSL") or "").lower() == "on":
        return True
    return str(environ.get("wsgi.url_scheme") or "") == "https"


def _is_loopback_request(environ: dict) -> bool:
    host = str(environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "")
    hostname = host.rsplit(":", 1)[0].strip("[]").lower()
    return hostname in _LOOPBACK


def _session_cookie(value: str, environ: dict, config: PortalConfig, *, clear: bool = False) -> str:
    secure = _request_is_secure(environ)
    if not secure and not _is_loopback_request(environ):
        secure = True  # never hand an insecure session cookie to a remote client
    parts = [
        f"{config.session_cookie}={value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
    ]
    if secure:
        parts.append("Secure")
    if clear:
        parts.extend(["Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"])
    return "; ".join(parts)


def _airflow_cookie_clear(environ: dict, config: PortalConfig) -> str:
    """Clear the Airflow ``_token`` cookie on logout; no Airflow route is exposed publicly."""
    secure = _request_is_secure(environ)
    if not secure and not _is_loopback_request(environ):
        secure = True
    parts = [
        f"{AIRFLOW_TOKEN_COOKIE}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=0",
        "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _common_headers(
    headers: list[tuple[str, str]], config: PortalConfig, *, worker: bool = False,
) -> list[tuple[str, str]]:
    script_sources = "'self' 'unsafe-eval'" if worker else "'self'"
    csp = (
        f"default-src 'self'; script-src {script_sources}; worker-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    return [
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy", csp),
        *headers,
    ]


def _json_response(
    start_response: Callable,
    status: int,
    payload: dict,
    config: PortalConfig,
    headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response_headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        *list(headers or []),
    ]
    start_response(f"{int(status)} {HTTPStatus(int(status)).phrase}", _common_headers(response_headers, config))
    return [body]


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("%s - %s", self.address_string(), format % args)


def serve(config: PortalConfig) -> None:
    """Serve the portal until interrupted (deployment owns the reverse proxy and TLS)."""
    app = PortalApp(config)
    if config.simulation is not None:
        import uvicorn
        from .pico_portal import create_app
        from ..simulation.service import SimulationService
        from ..simulation.store import SimulationStore
        from ..platform.facts import AdmissionControlStore

        connection_factory = AdmissionControlStore.from_environment(os.environ)._connect
        settings = config.simulation
        service = SimulationService(SimulationStore(connection_factory), app._artifact_store(),
                                    Path(settings["root"]), Path(settings["python"]), capacity=settings["capacity"])
        uvicorn.run(create_app(app, service), host=config.host, port=config.port, workers=1,
                    ws_max_size=128 * 1024, log_level="info")
        return
    with make_server(
        config.host, config.port, app, server_class=_ThreadingWSGIServer, handler_class=_QuietHandler
    ) as server:
        log.info("operator portal listening on http://%s:%s", config.host, server.server_address[1])
        server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="SolidWorks-to-URDF operator portal")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="JSON config with airflow.url, endpoint.url and endpoint.token_file",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve(load_portal_config(args.config))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by deployment
    raise SystemExit(main())
