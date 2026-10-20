"""Minimal authenticated operator portal for one SolidWorks engineering folder.

The portal is a dependency-free WSGI application:

* the operator signs in once with Feishu SSO at ``/auth/feishu/login``; the Airflow ``_token``
  cookie that Airflow issues is validated server-side through ``/auth/feishu/profile`` and its
  value is kept in a portal session, never echoed to the browser;
* the portal triggers the ``solidworks_to_urdf`` DAG with ``handoff_path`` and ``main_assembly``,
  and reports the Airflow stage progress, native findings and the published pull request;
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
from collections.abc import Callable, Iterable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from ..stages import (
    CONTRACT,
    CONTRACT_FILE_SHA256,
    CONTRACT_SHA256,
    VIEW_SCHEMA,
    activity_view,
    stage_view,
)
from .airflow_client import (
    EndpointConfig,
    EndpointError,
    EndpointNotFound,
    EndpointProtocolError,
    ResultNotPublishable,
    WindowsEndpoint,
    _linux_repositories,
    _linux_targets,
    check_result,
    native_run_id,
    normalize_change_summary,
    validate_artifact_name,
    validate_main_assembly,
    verified_result,
)
from ..io import PipelineError, digest
from ..interface_records import REQUEST_PURPOSES
from ..delivery import PIPELINE_ID
from ..platform import flow
from ..platform.store import (
    AdmissionClosedError,
    OpenAttemptConflictError,
    PlatformStore,
    PlatformStoreError,
    RunNotAdmittedError,
)
from ..platform.storage import SEALED_URI_PREFIX, parse_sealed_uri
from ..ports import PORTAL_HTTP_PORT
from .stage_transfer import TEAM_REPORTS
from .freeze_retry import FreezeRetryRefusal, resolve_freeze_retry_identity
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
    RECOVERY_TEMPORARY_DISCONNECTION,
    RECOVERY_UNKNOWN_PUBLICATION,
    RecoveryAssessment,
    classify_recovery,
    recorded_actor_principal,
)
from .report_view import build_report
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
#: Read precedence across the registered stages: the first registration of a member
#: path wins, so a later stage's sealed copy is never shadowed by an earlier one.
_STAGE_READ_ORDER = ("publish", "verify", "generate", "capture", "discover", "freeze")
#: The one native half of a registered snapshot: live observations that never
#: carry run/delivery authority (outcome, identity, submission stay registered).
_OBSERVATION_ONLY_JOB_FIELDS = ("activity", "activity_final", "activity_history", "discovery", "error")


def _member_class(path: str) -> str:
    """The member class projected from its path: reports are never subject members."""

    return "report" if path.startswith("reports/") else "subject"


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
        not isinstance(row, dict) or (code_required and not row.get("code")) for row in rows
    ):
        raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered diagnostic content has an invalid structure")
    return rows


def _delivery_warnings(members: dict) -> list[dict]:
    """Input warnings and non-blocking discovery findings, from the same registered members."""

    warnings: list[dict] = []
    handoff = members.get("input/handoff.json")
    if handoff is not None:
        for item in _registered_source_rows(_parse_registered_json(handoff), "warnings", code_required=True):
            warnings.append(
                {
                    "source": "input",
                    "code": str(item["code"]),
                    "object": "",
                    "message": str(item.get("message") or ""),
                    "detail": item.get("detail"),
                }
            )
    discovery = members.get("evidence/discovery.json")
    if discovery is not None:
        findings = _registered_source_rows(_parse_registered_json(discovery), "findings", code_required=True)
        for item in findings:
            if item.get("blocking") is True:
                continue
            warnings.append(
                {
                    "source": "discovery",
                    "code": str(item["code"]),
                    "object": str(item.get("object") or ""),
                    "message": str(item.get("message") or ""),
                    "detail": item.get("detail"),
                }
            )
    return warnings


def _quality_warnings(report: dict | None) -> list[dict]:
    """The displayable non-blocking diagnostics of the registered quality report."""

    warnings: list[dict] = []
    for item in _registered_source_rows(report or {}, "diagnostics", code_required=True):
        if item.get("blocking") is True:
            continue
        warning = {
            "source": "quality",
            "code": str(item["code"]),
            "object": str(item.get("object") or ""),
            "message": str(item.get("message") or ""),
            "detail": item.get("detail"),
        }
        for key in ("blocking", "objects"):
            if key in item:
                warning[key] = item[key]
        warnings.append(warning)
    return warnings


def _canonical_warning_value(value: object) -> str:
    """One stable comparison form for a JSON-shaped warning field."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _preview_warnings(*groups: list[dict]) -> list[dict]:
    """Merge warning rows by display identity, then order them deterministically."""

    unique: dict[tuple[str, str, str, str, str], dict] = {}
    for row in (item for group in groups for item in group):
        key = (
            str(row.get("code") or ""),
            str(row.get("object") or ""),
            str(row.get("message") or ""),
            _canonical_warning_value(row.get("detail")),
            _canonical_warning_value(row.get("objects")),
        )
        unique.setdefault(key, row)

    def order(row: dict) -> tuple[str, str, str, str, str, str]:
        return (
            str(row.get("code") or ""),
            str(row.get("object") or ""),
            str(row.get("message") or ""),
            _canonical_warning_value(row.get("detail")),
            _canonical_warning_value(row.get("objects")),
            str(row.get("source") or ""),
        )

    return sorted(unique.values(), key=order)


#: Readable labels for the automatically retained structural-team reports.
_STRUCTURAL_REPORT_LABELS = {
    "reports/structural-team.md": "Structural-team report (readable Markdown)",
    "reports/structural-team.json": "Structural-team report (structured findings and parameters)",
}
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
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


class RerunUncertainty(RuntimeError):
    """The authoritative linked-attempt state could not be verified; never fail open."""


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

    def dag(self, token: str, dag_id: str) -> dict:
        return self._request("GET", f"{self.api_root}/dags/{urlparse.quote(dag_id)}", token=token)

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

    def list_dag_runs(self, token: str, dag_id: str, *, limit: int = 20, offset: int = 0) -> list[dict]:
        """Recent runs of one DAG, so the operator page survives portal restarts.

        The per-DAG GET is authorized as a read of that DAG; the POST wildcard list would ask for
        authorization on ``~`` and is deliberately not used.
        """
        query = urlparse.urlencode({"limit": int(limit), "offset": int(offset), "order_by": "-start_date"})
        payload = self._request(
            "GET",
            f"{self.api_root}/dags/{urlparse.quote(dag_id)}/dagRuns?{query}",
            token=token,
        )
        runs = payload.get("dag_runs")
        if not isinstance(runs, list):
            raise AirflowApiError("Airflow returned no dag run list")
        return [item for item in runs if isinstance(item, dict)]


@dataclass(frozen=True)
class PortalConfig:
    airflow: AirflowApi
    endpoint: WindowsEndpoint | Callable[[], WindowsEndpoint]
    dag_id: str = DEFAULT_DAG_ID
    static_dir: Path = Path(__file__).resolve().parent / "static"
    host: str = "127.0.0.1"
    port: int = PORTAL_HTTP_PORT
    session_ttl: float = 12 * 3600.0
    artifact_limit: int = 64 * 1024 * 1024
    preview_ttl: float = 60.0
    max_body_bytes: int = 64 * 1024
    upload_root: Path | None = None
    upload_concurrency: int = 2
    #: Split-pipeline routing: the model-repository checkouts.
    pipeline_repositories: dict[str, Path] | None = None
    #: Configured publication targets used by the upload selector and conf validation.
    pipeline_targets: dict[str, dict] | None = None
    #: The platform admission store; injected in tests, derived from the deploy
    #: environment (``PLATFORM_SERVICE_DSN``) otherwise.  Submissions are refused
    #: while the store cannot be reached - never silently ungated.
    platform_store: PlatformStore | None = None
    #: Test/embedding seam for the already-validated active snapshot digest. Production
    #: loading leaves this unset so admission resolves the real Connection contract.
    config_identity_provider: Callable[[], str] | None = None


_CONFIG_KEYS = {
    "airflow": {"url"},
    "endpoint": {"url", "token_file"},
    "portal": {"host", "port", "upload_root", "upload_concurrency"},
    "pipeline": {"store_root", "repositories", "targets", "discovery"},
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


def _endpoint_from_config(section: dict) -> WindowsEndpoint:
    token_file = section.get("token_file")
    url = section.get("url")
    if not (isinstance(url, str) and url.strip()):
        raise AirflowApiError("portal config needs endpoint.url")
    if not (isinstance(token_file, str) and token_file.strip()):
        raise AirflowApiError("portal config needs endpoint.token_file")
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise AirflowApiError(f"cannot read endpoint token file {token_file}: {error}") from error
    if not token:
        raise AirflowApiError(f"endpoint token file {token_file} is empty")
    return WindowsEndpoint(EndpointConfig(base_url=url.strip(), token=token))


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
    return PortalConfig(
        airflow=AirflowApi(url.strip()),
        endpoint=_endpoint_from_config(endpoint_section),
        host=str(portal_section.get("host") or "127.0.0.1"),
        port=int(portal_section.get("port", PORTAL_HTTP_PORT)),
        upload_root=_upload_root(portal_section),
        upload_concurrency=_upload_concurrency(portal_section),
        pipeline_repositories=pipeline_repositories,
        pipeline_targets=pipeline_targets,
    )


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


def _attempt_conf_matches(
    conf: object,
    *,
    handoff_path: str,
    stage: str,
    target_hardware: str,
    change_summary: str,
    parent_assembly: str | None,
    purpose: str,
) -> bool:
    """Whether an existing attempt's recorded conf is exactly this request's frame.

    A retry may only ride an existing attempt when every frame field equals what
    that attempt recorded: a different target, summary, source or selection names
    a different request and must never be answered by the other attempt's run.
    """
    if not isinstance(conf, dict):
        return False
    recorded_summary = conf.get("change_summary")
    if isinstance(recorded_summary, str):
        recorded_summary = normalize_change_summary(recorded_summary).strip()
    return (
        conf.get("handoff_path") == handoff_path
        and conf.get("resume_from") == stage
        and conf.get("purpose") == purpose
        and _optional_str(conf.get("target_hardware")) == target_hardware
        and recorded_summary == change_summary
        and _optional_str(conf.get("main_assembly")) == parent_assembly
    )


def _attempt_frame_digest(
    *,
    handoff_path: str,
    stage: str,
    target_hardware: str,
    change_summary: str,
    parent_assembly: str | None,
    purpose: str,
) -> str:
    """The sealed frame digest of one linked attempt: the admission's own identity."""
    return digest(
        {
            "handoff_path": handoff_path,
            "main_assembly": parent_assembly,
            "purpose": purpose,
            "target_hardware": target_hardware,
            "resume_from": stage,
            "change_summary": change_summary,
        }
    )


def _initial_dispatch(
    *,
    dag_id: str,
    dag_run_id: str,
    stage: str,
    params_digest: str,
) -> dict:
    """The one Portal-owned initial dispatch payload for a new admitted run."""

    return {
        "stage": stage,
        "operation_key": f"portal-{dag_run_id}/{stage}:0",
        "params_digest": params_digest,
        "dag_id": dag_id,
        "dag_run_id": dag_run_id,
    }


def _admission_matches(
    admission: object,
    *,
    principal: str,
    target_hardware: str,
    parent_assembly: str | None,
    frame_digest: str,
    purpose: str,
) -> bool:
    """Whether an uncertain retry equals the immutable admitted request exactly.

    The retry may only ride the reservation when its authenticated principal,
    target, selection and sealed frame digest all equal the admission; a changed
    actor, target or summary names a different request and must never be
    answered by the other attempt's run.
    """
    if not isinstance(admission, dict):
        return False
    return (
        admission.get("principal") == principal
        and admission.get("purpose") == purpose
        and admission.get("target_hardware") == target_hardware
        and admission.get("main_assembly") == (parent_assembly or "")
        and admission.get("dispatch_digest") == frame_digest
    )


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
        evidence["detail"] = detail
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
    """Native findings with object/message/evidence, exactly as the endpoint persists them.

    The endpoint's ``discovery.findings`` entries are
    ``{schema_version, code, object, message, detail, blocking}``; ``detail`` is the diagnostic
    evidence and ``discovery_sha256`` identifies the raw record it came from.
    """
    findings: list[dict] = []
    if not isinstance(job, dict):
        return findings
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    discovery = job.get("discovery") if isinstance(job.get("discovery"), dict) else {}
    digest = str(discovery.get("discovery_sha256") or "")
    for item in discovery.get("findings") or []:
        if not isinstance(item, dict):
            continue
        findings.append(
            {
                "id": str(item.get("code") or ""),
                "severity": "error" if item.get("blocking", True) else "warning",
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
    quality = result.get("quality")
    if isinstance(quality, dict):
        for check in quality.get("checks") or []:
            if isinstance(check, dict) and check.get("passed") is False:
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


def _automatic_summary(job: dict | None) -> dict:
    if not isinstance(job, dict):
        return {"state": "pending", "job_state": "pending", "checks": []}
    status = str(job.get("status") or "pending")
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    discovery = job.get("discovery") if isinstance(job.get("discovery"), dict) else {}
    checks = []
    quality = result.get("quality")
    if isinstance(quality, dict):
        checks = [check for check in quality.get("checks") or [] if isinstance(check, dict)]
    try:
        verified_result(result)
    except ResultNotPublishable as error:
        message = str(error)
    else:
        # A verified model stays previewable even when the PR service failed afterwards.
        return {"state": "passed", "job_state": status, "checks": checks}
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
            "message": "Native CAD discovery failed; correct the engineering source for each finding",
        }
    if status == "failed":
        return {"state": "failed", "job_state": status, "checks": checks, "message": message}
    if status in {"queued", "selecting", "running", "awaiting_definition", "native_complete"}:
        # native_complete ends the native half only: the portable half is still the
        # pipeline's remaining work, so the automatic view stays in progress instead of
        # surfacing a terminal verdict or a raw protocol message.
        return {
            "state": "queued" if status == "queued" else "running",
            "job_state": status,
            "checks": checks,
        }
    return {"state": "unverified", "job_state": status, "checks": checks, "message": message}


_NATIVE_JOB_STATUSES = frozenset(
    {"queued", "selecting", "running", "awaiting_definition", "passed", "native_complete", "failed"}
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


#: The recovery classification of a registered outcome that blocks in-place retry.
#: An unresolved outcome is stage-neutral: it requires reconciliation, and only a
#: registration whose own evidence names the publication scope may say so.
RECOVERY_RECONCILIATION_REQUIRED = "reconciliation_required"
_REGISTERED_RECOVERY_CLASSIFICATION = {
    "failed": RECOVERY_ENGINEERING_TERMINAL,
    "cancelled": RECOVERY_ENGINEERING_TERMINAL,
    "succeeded": "run_succeeded",
    "unresolved": RECOVERY_RECONCILIATION_REQUIRED,
}


def _registered_recovery_block(record: dict | None) -> tuple[str, str] | None:
    """The (reason, classification) blocking in-place recovery, or None while it may proceed.

    Only a known live admitted registration (``queued``/``running``) opens the
    scheduling recovery: decided terminal outcomes refuse with their recorded
    reason, an unresolved outcome requires reconciliation, and a missing or
    unproven registration fails closed - Airflow or native facts never
    substitute for the platform admission.
    """
    state = record.get("state") if isinstance(record, dict) else None
    if state in {"queued", "running"}:
        return None
    if isinstance(state, str) and state in _REGISTERED_RECOVERY_CLASSIFICATION:
        return f"registered_{state}", _REGISTERED_RECOVERY_CLASSIFICATION[state]
    return "registration_missing", RECOVERY_TEMPORARY_DISCONNECTION


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
    assessment = classify_recovery(run.get("state"), states, status)
    if not assessment.eligible:
        return assessment
    if job is None:
        # The endpoint holds no job for this run: only the submission poke may
        # resume; a capture import without a job is unprovable, and an endpoint
        # error means even the absence is unconfirmed.  In the six-stage
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
        self._lock = threading.Lock()
        self._previews: dict[str, tuple[float, dict]] = {}
        self._upload_gate = UploadGate(config.upload_concurrency)
        self._rerun_lock = threading.Lock()
        self._platform_store = config.platform_store
        self._platform_lock = threading.Lock()

    def _platform(self) -> PlatformStore:
        """The admission store: the injected one, or the deploy-environment store.

        Built lazily so a portal process without the deploy environment still
        starts; its first submission then refuses instead of running ungated.
        """

        with self._platform_lock:
            if self._platform_store is None:
                self._platform_store = PlatformStore.from_environment(os.environ)
            return self._platform_store

    def _admission_config_digest(self) -> str:
        return _verified_admission_config_digest(self.config.config_identity_provider)

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
        session = self.sessions.get(cookies.get(DEFAULT_SESSION_COOKIE))
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
        session = self.sessions.get(cookies.get(DEFAULT_SESSION_COOKIE))
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

    def _list_runs(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        session = self._session(environ)
        query = urlparse.parse_qs(str(environ.get("QUERY_STRING") or ""), keep_blank_values=True)
        limit = _page_integer(query, "limit", default=20, minimum=1, maximum=100)
        offset = _page_integer(query, "offset", default=0, minimum=0, maximum=100_000)
        include_deleted = str((query.get("include_deleted") or [""])[0]).lower() in {"1", "true"}
        metadata = self._metadata_snapshot()
        principal, is_admin = self._viewer_identity(session)
        runs: list[dict] = []
        seen: set[str] = set()
        # Scan past recently deleted runs so tombstones never push older runs off the page.
        # The budget scales with the requested offset and a bounded extra scan; when the budget
        # runs out before the end of the history is proven, the list fails closed instead of
        # presenting a truncated result as the complete one.
        page_size = 100
        required = offset + limit + 1
        scan_budget = ((required + page_size - 1) // page_size) + 10
        pages = 0
        while True:
            if pages >= scan_budget:
                raise PortalError(
                    HTTPStatus.SERVICE_UNAVAILABLE, "Run history is too long to locate more records; retry later"
                )
            try:
                items = self.config.airflow.list_dag_runs(
                    session.token, self.config.dag_id, limit=page_size, offset=pages * page_size
                )
            except AirflowAuthError as error:
                raise PortalError(HTTPStatus.UNAUTHORIZED, str(error)) from error
            except AirflowApiError as error:
                # Never present a partial history as the complete one.
                raise PortalError(
                    HTTPStatus.SERVICE_UNAVAILABLE, "Run list is temporarily unavailable; retry later"
                ) from error
            pages += 1
            for item in items:
                dag_run_id = str(item.get("dag_run_id") or "")
                if not dag_run_id or dag_run_id in seen:
                    continue
                seen.add(dag_run_id)
                conf = item.get("conf") if isinstance(item.get("conf"), dict) else {}
                owner, name = _actor_identity(item.get("triggering_user_name"))
                overlay = metadata.get(native_run_id(dag_run_id)) or _EMPTY_OVERLAY
                if overlay["deleted"] and not include_deleted:
                    continue
                runs.append(
                    {
                        "dag_run_id": dag_run_id,
                        "handoff_path": conf.get("handoff_path"),
                        # The recorded initiator, never the current viewer; missing names stay null.
                        "user": name,
                        # Airflow's own record outlives any portal restart.
                        "principal": owner,
                        "state": item.get("state"),
                        "started_at": item.get("start_date"),
                        # Linked rerun lineage: derived from the child run's own conf only.
                        "parent_dag_run_id": _optional_str(conf.get("parent_dag_run_id")),
                        "resume_from": _optional_str(conf.get("resume_from")),
                        "resume_from_name": _stage_name(conf.get("resume_from")),
                        # Row-level action gate for the operator page (one profile check per list).
                        "can_manage": is_admin or owner == principal,
                        **overlay,
                    }
                )
            if len(runs) >= required or len(items) < page_size:
                break
        page_runs = runs[offset : offset + limit]
        return _json_response(
            start_response,
            200,
            {
                "runs": page_runs,
                "next_offset": offset + limit if len(runs) > offset + limit else None,
            },
            self.config,
        )

    def _start_run(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        """One uploaded SolidWorks folder starts one run; manual platform paths are gone."""
        session = self._session(environ, csrf=True)
        principal = session.principal or ""
        if not principal:
            # The submitter identity is the strict profile principal only - never a
            # username fallback and never client text.
            raise PortalError(HTTPStatus.UNAUTHORIZED, "Feishu session expired; sign in again")
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
        # A cached portal session can outlive the Airflow token behind it; prove the live
        # profile before reading a single byte of a possibly multi-gigabyte body.
        try:
            self.config.airflow.profile(session.token)
        except AirflowAuthError:
            return _json_response(
                start_response,
                HTTPStatus.UNAUTHORIZED,
                {"error": "Feishu session expired; sign in again and retry"},
                self.config,
            )
        except AirflowApiError:
            return _json_response(
                start_response,
                HTTPStatus.BAD_GATEWAY,
                {"error": "Airflow is temporarily unavailable; retry later"},
                self.config,
            )
        try:
            admission = self._platform().admission()
        except PlatformStoreError:
            return _json_response(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "Platform service is temporarily unavailable; retry later"},
                self.config,
            )
        if admission.state != "open":
            return _json_response(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "Platform admission is closed; new run submissions are not accepted"},
                self.config,
            )
        dag_run_id = f"portal-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(4)}"
        try:
            receipt = receive_folder(
                environ,
                intake_root=self.config.upload_root,
                run_id=dag_run_id,
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
        target_hardware = receipt.target_hardware.strip() if isinstance(receipt.target_hardware, str) else ""
        if not target_hardware:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Publication target is required; select one and retry")
        if target_hardware not in (self.config.pipeline_targets or {}):
            raise PortalError(HTTPStatus.BAD_REQUEST, "Publication target is not configured; select another target")
        change_summary = (
            normalize_change_summary(receipt.change_summary).strip() if isinstance(receipt.change_summary, str) else ""
        )
        if not change_summary:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Change summary is required")
        purpose = receipt.purpose.strip() if isinstance(receipt.purpose, str) else ""
        if not purpose:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Purpose is required; select one on the page")
        if purpose not in REQUEST_PURPOSES:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Purpose is not configured; select another purpose")
        handoff_path = str(self.config.upload_root / dag_run_id / receipt.folder)
        try:
            self._platform().admit_run(
                request_id=f"portal-{dag_run_id}",
                # The store's run identity is the same canonical native UUID the DAG
                # derives; the raw dag_run_id stays only as the Airflow binding.
                run_id=native_run_id(dag_run_id),
                principal=principal,
                target_hardware=receipt.target_hardware or "",
                main_assembly=receipt.main_assembly or "",
                configuration={"purpose": purpose},
                baseline={
                    "handoff_sha256": receipt.handoff_sha256,
                    "files": receipt.files,
                    "bytes": receipt.bytes,
                    "folder": receipt.folder,
                    "platform_config_digest": self._admission_config_digest(),
                },
                dispatch=_initial_dispatch(
                    dag_id=self.config.dag_id,
                    dag_run_id=dag_run_id,
                    stage="freeze",
                    params_digest=digest(
                        {
                            "handoff_path": handoff_path,
                            "main_assembly": receipt.main_assembly,
                            "target_hardware": receipt.target_hardware,
                        }
                    ),
                ),
            )
        except AdmissionClosedError:
            drop_received_folder(self.config.upload_root / dag_run_id)
            return _json_response(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "Platform admission is closed; submission was not admitted", "dag_run_id": dag_run_id},
                self.config,
            )
        except PlatformStoreError:
            drop_received_folder(self.config.upload_root / dag_run_id)
            return _json_response(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": "Platform service is temporarily unavailable; submission was not admitted",
                    "dag_run_id": dag_run_id,
                },
                self.config,
            )
        outcome = self._trigger_uploaded_run(
            session,
            dag_run_id,
            handoff_path,
            target_hardware,
            change_summary,
            receipt.main_assembly,
            purpose=purpose,
        )
        if outcome == "auth":
            return _json_response(
                start_response,
                HTTPStatus.UNAUTHORIZED,
                {"error": "Feishu session expired; sign in again and retry", "dag_run_id": dag_run_id},
                self.config,
            )
        if outcome != "created":
            log.warning("portal upload trigger unresolved dag_run_id=%s trigger=%s", dag_run_id, outcome)
            return _json_response(
                start_response,
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": (
                        "Run start state is unconfirmed; the upload was retained. Check the run list "
                        "or contact an administrator; do not submit again."
                    ),
                    "dag_run_id": dag_run_id,
                    "trigger": outcome,
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
        if receipt.main_assembly is not None:
            payload["main_assembly"] = receipt.main_assembly
        return _json_response(start_response, 201, payload, self.config)

    def _trigger_uploaded_run(
        self,
        session: PortalSession,
        dag_run_id: str,
        handoff_path: str,
        target_hardware: str,
        change_summary: str,
        main_assembly: str | None = None,
        *,
        purpose: str,
    ) -> str:
        """Trigger once; an ambiguous failure is reconciled on the exact run id, never blind-retried."""
        # The submitter of a run is its Airflow-authenticated actor, recorded on
        # the run itself; the create body never carries an identity claim - not
        # the acting operator's, and never a client-provided one.
        conf = {
            "handoff_path": handoff_path,
            "purpose": purpose,
            "request_id": f"portal-{dag_run_id}",
            "target_hardware": target_hardware,
            "change_summary": change_summary,
        }
        if main_assembly is not None:
            conf["main_assembly"] = main_assembly
        try:
            self.config.airflow.trigger_dag_run(session.token, self.config.dag_id, dag_run_id, conf)
            return "created"
        except AirflowAuthError:
            return "auth"
        except AirflowApiError:
            pass
        if self._uploaded_run_exists(session, dag_run_id):
            return "created"
        try:
            self.config.airflow.trigger_dag_run(session.token, self.config.dag_id, dag_run_id, conf)
            return "created"
        except AirflowAuthError:
            return "auth"
        except AirflowApiError:
            return "created" if self._uploaded_run_exists(session, dag_run_id) else "absent"

    def _uploaded_run_exists(self, session: PortalSession, dag_run_id: str) -> bool:
        try:
            self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
            return True
        except (AirflowApiError, AirflowAuthError):
            return False

    # ------------------------------------------------------- run metadata
    def _platform_read(self, callback):
        """One registered platform read; an unavailable store fails the surface closed."""

        try:
            return callback()
        except PlatformStoreError as error:
            log.error("run display state unavailable: %s", error)
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Run metadata is temporarily unavailable; retry later"
            ) from error

    def _metadata_snapshot(self) -> dict[str, dict]:
        """Every display overlay with one platform read; failures fail closed."""
        try:
            displays = self._platform().run_displays()
        except PlatformStoreError as error:
            log.error("run display state unavailable: %s", error)
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Run metadata is temporarily unavailable; retry later"
            ) from error
        return {run_id: _display_overlay(display) for run_id, display in displays.items()}

    def _metadata_overlay(self, dag_run_id: str) -> dict:
        try:
            display = self._platform().run_display(native_run_id(dag_run_id))
        except PlatformStoreError as error:
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
            display = self._platform().set_run_display(native_run_id(dag_run_id), title)
        except RunNotAdmittedError as error:
            raise PortalError(
                HTTPStatus.NOT_FOUND, "This run has no platform registration and cannot be renamed"
            ) from error
        except PlatformStoreError as error:
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
                display = self._platform().hide_run(
                    native_run_id(dag_run_id), actor=self._actor_label(session, dag_run_id, run)
                )
            except RunNotAdmittedError as error:
                raise PortalError(
                    HTTPStatus.NOT_FOUND, "This run has no platform registration and cannot be deleted"
                ) from error
            except PlatformStoreError as error:
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
        try:
            job = self._endpoint().get_job(native_run_id(dag_run_id))
        except EndpointNotFound:
            job = None
        except EndpointError:
            return "unconfirmed", "Cannot confirm native job state; retry later"
        if isinstance(job, dict):
            job_state = str(job.get("status") or "")
            if job_state in {"queued", "selecting", "running", "awaiting_definition"}:
                return "run_active", "The native job is still executing and can be deleted only after it ends"
            # native_complete is a definitive native state: the Windows half is done and only
            # the Linux portable half may still be running (checked above).
            if job_state not in {"passed", "failed", "native_complete"}:
                return "unconfirmed", "Cannot confirm native job state; retry later"
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
            overlay = _display_overlay(self._platform().restore_run(native_run_id(dag_run_id)))
        except RunNotAdmittedError as error:
            raise PortalError(
                HTTPStatus.NOT_FOUND, "This run has no platform registration and cannot be restored"
            ) from error
        except PlatformStoreError as error:
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
        try:
            airflow_run = self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
            tasks = self.config.airflow.task_instances(session.token, self.config.dag_id, dag_run_id)
        except AirflowAuthError as error:
            raise PortalError(HTTPStatus.UNAUTHORIZED, str(error)) from error
        except AirflowApiError as error:
            missing = isinstance(error.__cause__, urlerror.HTTPError) and error.__cause__.code == 404
            raise PortalError(HTTPStatus.NOT_FOUND if missing else HTTPStatus.BAD_GATEWAY, str(error)) from error
        can_manage = self._can_manage(session, airflow_run)
        endpoint = self._endpoint()
        run_id = native_run_id(dag_run_id)
        job: dict | None = None
        endpoint_error: str | None = None
        try:
            job = self._job_snapshot(run_id, endpoint)
        except EndpointError as error:
            endpoint_error = str(error)
        conf = airflow_run.get("conf") if isinstance(airflow_run.get("conf"), dict) else {}
        principal, operator = _actor_identity(airflow_run.get("triggering_user_name"))
        retry = _retry_assessment(airflow_run, tasks, job, endpoint_error)
        recovery = self._platform_read(lambda: self._platform().run_record(run_id))
        recovery_block = _registered_recovery_block(recovery)
        if recovery_block is not None:
            # The same admission criterion the retry route enforces: the displayed
            # advice never offers an action the POST would necessarily refuse.
            reason, classification = recovery_block
            retry = RecoveryAssessment(False, classification, reason, ())
        pr = None
        if isinstance(job, dict):
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
        projection = self._read_projection(run_id)
        display_job = self._registered_stage_job(job, projection)
        stage_view_payload = stage_view(display_job)
        report = build_report(job, view=stage_view_payload)
        purpose = self._registered_purpose(projection) if projection is not None else None
        payload = {
            "dag_run_id": dag_run_id,
            "state": airflow_run.get("state"),
            "handoff_path": conf.get("handoff_path"),
            "main_assembly": _optional_str(conf.get("main_assembly")),
            "purpose": purpose,
            "operator": operator,
            "principal": principal,
            "can_manage": can_manage,
            # Linked rerun lineage from the child run's own conf, same fields as the list rows.
            "parent_dag_run_id": _optional_str(conf.get("parent_dag_run_id")),
            "resume_from": _optional_str(conf.get("resume_from")),
            "resume_from_name": _stage_name(conf.get("resume_from")),
            "retry": {
                "eligible": retry.eligible,
                "reason": retry.reason,
                "classification": retry.classification,
            },
            "started_at": airflow_run.get("start_date"),
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
                "error": job.get("error"),
                # The registered failure is the readout authority: its stable code
                # and detail travel with the same projection; a shape-only legacy
                # failure stays null instead of fabricating fields.
                "error_code": job.get("error_code"),
                "detail": job.get("detail"),
            },
            "discovery": None
            if not isinstance(job, dict) or not isinstance(job.get("discovery"), dict)
            else {
                "passed": job["discovery"].get("passed"),
                "hardware_id": job["discovery"].get("hardware_id"),
                "revision": job["discovery"].get("revision"),
                "discovery_sha256": job["discovery"].get("discovery_sha256"),
            },
            "stage_view": stage_view_payload,
            "report": report,
            "structural_report": self._retained_report_links(dag_run_id),
            "findings": _findings(job),
            "activity": activity_view(job),
            "automatic": _automatic_summary(job),
            "coverage": _coverage_report(job, report),
            "pr": pr,
            "endpoint_error": endpoint_error,
            # Display rename / reversible tombstone; an unreadable ledger fails closed above.
            **self._metadata_overlay(dag_run_id),
        }
        rerun_rows = self._rerun_rows(run_id, conf)
        if rerun_rows is not None:
            payload["stage_reruns"] = rerun_rows
        return _json_response(start_response, 200, payload, self.config)

    def _retry_run(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        session = self._session(environ, csrf=True)
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Run identifier is invalid")
        if self._body(environ):
            raise PortalError(HTTPStatus.BAD_REQUEST, "Retry does not accept run parameters")
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
                record = self._platform_read(lambda: self._platform().run_record(run_id))
                block = _registered_recovery_block(record)
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
                job = None
                try:
                    job = self._endpoint().get_job(native_run_id(dag_run_id))
                except EndpointNotFound:
                    pass
                except EndpointError as error:
                    raise PortalError(
                        HTTPStatus.CONFLICT, "Cannot confirm the original job state; retry later"
                    ) from error
                assessment = _retry_assessment(run, tasks, job, None)
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
    # ------------------------------------------------- freeze retry stand
    def _freeze_retry_stand(
        self, native_id: str, conf: dict | None
    ) -> tuple[bool, str, str, list[str]] | None:
        """The Linux-owned freeze-retry stand of one parent, from registered facts only.

        ``None`` keeps the existing stage row untouched (the parent is not a
        terminal freeze failure).  Otherwise the verdict replaces the freeze row:
        the one resolver proves the sealed intake against the registered identity
        (the request baseline when present, the bound failure evidence otherwise)
        and any missing or changed original refuses instead of being silently
        resealed.
        """

        record = self._platform_read(lambda: self._platform().run_record(native_id))
        if not isinstance(record, dict) or record.get("state") not in {"failed", "cancelled", "succeeded"}:
            return None
        stages = self._platform_read(lambda: self._platform().stage_records(native_id))
        freeze_row = stages.get("freeze") if isinstance(stages, dict) else None
        if not (isinstance(freeze_row, dict) and freeze_row.get("state") in {"failed", "completed"}):
            return None
        parent_dag_run_id = self._admitted_dag_run_id(record.get("request_id"))
        if parent_dag_run_id is None:
            return (
                False,
                "originals_identity_missing",
                "The original run has no recoverable scheduling identity; Freeze inputs cannot be rerun",
                [],
            )
        handoff_path = _optional_str(conf.get("handoff_path")) if isinstance(conf, dict) else None
        if handoff_path is None:
            return (
                False,
                "incomplete",
                "The original run has no reusable upload directory and cannot be rerun from this step",
                [],
            )
        try:
            resolved = resolve_freeze_retry_identity(
                self._platform(),
                native_id,
                parent_dag_run_id=parent_dag_run_id,
                expected_dag_id=self.config.dag_id,
                intake_path=Path(handoff_path),
                intake_root=self.config.upload_root,
            )
        except FreezeRetryRefusal as refusal:
            return (False, refusal.reason, refusal.message, [])
        except PlatformStoreError as error:
            raise PortalError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Run metadata is temporarily unavailable; retry later"
            ) from error
        options = sorted(
            (
                name
                for name in resolved.upload_files
                if isinstance(name, str) and name.lower().endswith(".sldasm")
            ),
            key=lambda value: (value.lower(), value),
        )
        if not options:
            return (
                False,
                "originals_identity_missing",
                "The sealed originals carry no selectable main assembly; rerun refused",
                [],
            )
        if freeze_row.get("state") == "completed":
            return (
                True,
                "linux_new_request",
                "Create a new request from these sealed originals on Linux; freeze and all later stages rerun",
                options,
            )
        return (
            True,
            "linux_ready",
            "Freeze inputs can be rerun on Linux with the sealed originals",
            options,
        )

    def _rerun_rows(self, native_id: str, conf: dict | None = None) -> list[dict] | None:
        """Finished stage-rerun availability rows; unavailable data is omitted, never faked."""
        plan = getattr(self._endpoint(), "rerun_plan", None)
        rows = None
        if plan is not None:
            try:
                payload = plan(native_id)
                rows = payload.get("stage_reruns") if isinstance(payload, dict) else None
            except (EndpointNotFound, EndpointError):
                rows = None
        if not isinstance(rows, list) or not rows:
            rows = None
        record = self._platform_read(lambda: self._platform().run_record(native_id))
        if not isinstance(record, dict):
            return rows
        stages = self._platform_read(lambda: self._platform().stage_records(native_id))

        def checkpoint(stage: str) -> str | None:
            row = stages.get(stage)
            if isinstance(row, dict) and isinstance(row.get("checkpoint_uri"), str) and row["checkpoint_uri"]:
                return row["checkpoint_uri"]
            return None

        if rows is None:
            # A linked Linux attempt has no Windows plan of its own: the rows describe the
            # portable restart surface while the native stages remain reused evidence.
            rows = [
                {
                    "stage": stage["id"],
                    "name": stage["name"],
                    "eligible": False,
                    "reason": "linux_owned",
                    "message": "This stage is provided by the Windows native endpoint or was reused",
                    "recomputes": [stage["id"]],
                    "retains": [],
                    "prerequisites": [],
                    "target_changed": False,
                }
                for stage in CONTRACT["stages"]
            ]
        seeds = {"generate": checkpoint("capture"), "verify": checkpoint("generate"), "publish": checkpoint("verify")}
        for row in rows:
            stage = row.get("stage")
            if stage not in seeds:
                continue
            ready = seeds[stage] is not None
            row.update(
                eligible=bool(ready),
                reason="linux_ready" if ready else "linux_checkpoint_missing",
                message="Restartable on Linux" if ready else "No Linux-restartable checkpoint is available",
                # The native plan's prerequisite summary describes the Windows half only
                # (its receipt accounting cannot see post-capture evidence); for the
                # portable surface the verdict above is proven by the local checkpoints.
                # Clear it instead of inventing Linux receipt evidence.
                prerequisites=[],
            )
        verdict = self._freeze_retry_stand(native_id, conf) if conf is not None else None
        if verdict is not None:
            eligible, reason, message, main_assembly_options = verdict
            for row in rows:
                if row.get("stage") != "freeze":
                    continue
                row.update(
                    eligible=eligible,
                    reason=reason,
                    message=message,
                    recomputes=list(_STAGE_IDS) if eligible else [],
                    retains=[],
                    prerequisites={},
                    main_assembly_options=main_assembly_options,
                )
                break
        return rows

    def _child_run(self, session: PortalSession, dag_run_id: str) -> dict | None:
        """One authoritative Airflow run, or None on a clean 404; anything else is uncertain."""
        try:
            return self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
        except AirflowAuthError:
            raise
        except AirflowApiError as error:
            missing = isinstance(error.__cause__, urlerror.HTTPError) and error.__cause__.code == 404
            if missing:
                return None
            raise RerunUncertainty("linked run lookup failed") from error

    @staticmethod
    def _admitted_dag_run_id(request_id: object) -> str | None:
        """The Airflow id of an admitted run, recovered from the portal's request identity.

        The portal mints ``request_id = "portal-" + dag_run_id`` for every admitted run,
        so the read side recovers the exact Airflow binding without a second mapping.
        Anything else is unreadable and refuses upstream.
        """
        if not isinstance(request_id, str) or not request_id.startswith("portal-"):
            return None
        candidate = request_id[len("portal-") :]
        return candidate if _RUN_ID.fullmatch(candidate) is not None else None

    def _open_linked_child(self, parent_dag_run_id: str) -> tuple[str, str | None] | None:
        """The one open (in-flight or unresolved) admitted child of this parent, or ``None``.

        The platform run rows are the reservation authority: every attempt is admitted
        before its trigger, so an open admitted child IS the durable reservation.  No
        state file and no Airflow run-list scan is consulted, and more than one open
        child of a parent is unresolvable rather than guessed.
        """
        native_parent = native_run_id(parent_dag_run_id)
        try:
            rows = self._platform().lifecycle_rows()
            candidates: list[tuple[str, str | None]] = []
            for row in rows:
                if not isinstance(row, dict) or row.get("object_kind") != "run":
                    continue
                if row.get("bucket") not in {"in_flight", "unresolved"}:
                    continue
                native_child = row.get("run_id")
                if not isinstance(native_child, str) or not native_child:
                    continue
                record = self._platform().run_record(native_child)
                if not isinstance(record, dict) or record.get("parent_run_id") != native_parent:
                    continue
                child = self._admitted_dag_run_id(record.get("request_id"))
                if child is None:
                    raise RerunUncertainty("the open attempt carries no recoverable run identity")
                candidates.append((child, _optional_str(record.get("from_stage"))))
        except PlatformStoreError as error:
            raise RerunUncertainty("active-attempt read failed") from error
        if not candidates:
            return None
        if len(candidates) > 1:
            raise RerunUncertainty("several open attempts of one parent")
        return candidates[0]

    def _trigger_linked_attempt(self, session: PortalSession, dag_run_id: str, conf: dict) -> str:
        """Trigger once; an ambiguous failure is reconciled on the exact run id, never blind-retried."""
        try:
            self.config.airflow.trigger_dag_run(session.token, self.config.dag_id, dag_run_id, conf)
            return "created"
        except AirflowAuthError:
            return "auth"
        except AirflowApiError:
            pass
        if self._uploaded_run_exists(session, dag_run_id):
            return "created"
        try:
            self.config.airflow.trigger_dag_run(session.token, self.config.dag_id, dag_run_id, conf)
            return "created"
        except AirflowAuthError:
            return "auth"
        except AirflowApiError:
            return "created" if self._uploaded_run_exists(session, dag_run_id) else "absent"

    @staticmethod
    def _attempt_payload(new_run_id: str, parent_dag_run_id: str, stage: str, state: str) -> dict:
        return {
            "dag_run_id": new_run_id,
            "parent_dag_run_id": parent_dag_run_id,
            "resume_from": stage,
            "resume_from_name": _STAGE_NAMES.get(stage, stage),
            "state": state,
        }

    def _accepted_attempt(
        self, start_response: Callable, attempt_id: str, parent_dag_run_id: str, stage: str, state: object
    ) -> Iterable[bytes]:
        return _json_response(
            start_response,
            HTTPStatus.ACCEPTED,
            self._attempt_payload(attempt_id, parent_dag_run_id, stage, _optional_str(state) or "queued"),
            self.config,
        )

    def _rerun_refusal(
        self,
        start_response: Callable,
        reason: str,
        message: str,
        earliest_required: str | None,
        *,
        active_dag_run_id: str | None = None,
    ) -> Iterable[bytes]:
        payload = {
            "error": message,
            "reason": reason,
            "message": message,
            "earliest_required": earliest_required,
        }
        if active_dag_run_id is not None:
            payload["active_dag_run_id"] = active_dag_run_id
        return _json_response(start_response, HTTPStatus.CONFLICT, payload, self.config)

    def _rerun_attempt(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        """One linked attempt from a chosen stage; only the stage is client-provided."""
        session = self._session(environ, csrf=True)
        principal = session.principal or ""
        if not principal:
            raise PortalError(HTTPStatus.UNAUTHORIZED, "Feishu session expired; sign in again")
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "Run identifier is invalid")
        body = self._body(environ)
        stage = body.get("stage")
        keys = {"stage", "target_hardware", "change_summary", "main_assembly"}
        if set(body) - keys or not isinstance(stage, str) or stage not in _STAGE_IDS:
            raise PortalError(
                HTTPStatus.BAD_REQUEST, "Rerun accepts only a valid stage and publication-target parameters"
            )
        overrides: dict[str, str] = {}
        for key in ("target_hardware", "change_summary", "main_assembly"):
            if key in body:
                if not isinstance(body[key], str):
                    raise PortalError(
                        HTTPStatus.BAD_REQUEST, "Rerun accepts only a valid stage and publication-target parameters"
                    )
                overrides[key] = body[key].strip()
        if "main_assembly" in overrides and stage != "freeze":
            raise PortalError(HTTPStatus.BAD_REQUEST, "Only a freeze rerun may select a new main assembly")
        try:
            airflow_run = self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
            if not self._can_manage(session, airflow_run):
                raise PortalError(
                    HTTPStatus.FORBIDDEN, "Only the initiator or a platform administrator can rerun this run"
                )
            parent_conf = airflow_run.get("conf") if isinstance(airflow_run.get("conf"), dict) else {}
            handoff_path = _optional_str(parent_conf.get("handoff_path"))
            if handoff_path is None:
                return self._rerun_refusal(
                    start_response,
                    "incomplete",
                    "The original run has no reusable upload directory and cannot be rerun from this step",
                    None,
                )
            with self._rerun_lock:
                if self._metadata_overlay(dag_run_id)["deleted"]:
                    # Readable evidence, but not a valid starting point until restored.
                    return self._rerun_refusal(
                        start_response, "deleted", "Deleted runs cannot be rerun; restore first", None
                    )
                # This request's effective frame, validated once here and reused by the
                # existing-attempt comparison and by the trigger itself.
                target_hardware = (
                    overrides["target_hardware"]
                    if "target_hardware" in overrides
                    else _optional_str(parent_conf.get("target_hardware"))
                )
                if not target_hardware:
                    raise PortalError(HTTPStatus.BAD_REQUEST, "Publication target is required when rerunning")
                if target_hardware not in (self.config.pipeline_targets or {}):
                    raise PortalError(
                        HTTPStatus.BAD_REQUEST, "Publication target is not configured; select another target"
                    )
                change_summary = (
                    overrides["change_summary"]
                    if "change_summary" in overrides
                    else _optional_str(parent_conf.get("change_summary"))
                )
                change_summary = (
                    normalize_change_summary(change_summary).strip() if isinstance(change_summary, str) else ""
                )
                if not change_summary:
                    raise PortalError(HTTPStatus.BAD_REQUEST, "Change summary is required when rerunning")
                parent_assembly = _optional_str(parent_conf.get("main_assembly"))
                resolved_freeze = None
                selected_assembly = parent_assembly
                if stage == "freeze":
                    try:
                        resolved_freeze = resolve_freeze_retry_identity(
                            self._platform(),
                            native_run_id(dag_run_id),
                            parent_dag_run_id=dag_run_id,
                            expected_dag_id=self.config.dag_id,
                            intake_path=Path(handoff_path),
                            intake_root=self.config.upload_root,
                        )
                    except FreezeRetryRefusal as refusal:
                        return self._rerun_refusal(start_response, refusal.reason, refusal.message, None)
                    except PlatformStoreError:
                        return _json_response(
                            start_response,
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "Platform service is temporarily unavailable; rerun was not admitted"},
                            self.config,
                        )
                    requested_assembly = overrides.get("main_assembly", parent_assembly or "")
                    try:
                        selected_assembly = validate_main_assembly(requested_assembly)
                    except EndpointProtocolError as error:
                        raise PortalError(HTTPStatus.BAD_REQUEST, str(error)) from error
                    if selected_assembly not in resolved_freeze.upload_files:
                        raise PortalError(
                            HTTPStatus.BAD_REQUEST,
                            "The selected main assembly is not part of the sealed originals",
                        )
                # The linked attempt inherits the parent's declared purpose and never
                # accepts a new one; a parent without it cannot be re-run.
                purpose = _optional_str(parent_conf.get("purpose"))
                if purpose not in REQUEST_PURPOSES:
                    return self._rerun_refusal(
                        start_response,
                        "incomplete",
                        "The original run has no purpose identifier and cannot be rerun from this step",
                        None,
                    )
                # The platform admission of the child IS the reservation: an open admitted
                # child of this parent is the one existing attempt to ride, conflict with
                # or reconcile - there is no state file and no run-list scan.
                open_child = self._open_linked_child(dag_run_id)
                reserved_id: str | None = None
                if open_child is not None:
                    child_id, child_stage = open_child
                    child_run = self._child_run(session, child_id)
                    child_state = _optional_str(child_run.get("state")) if child_run is not None else None
                    if child_run is not None and (child_state is None or child_state in {"queued", "running"}):
                        if child_stage != stage:
                            return self._rerun_refusal(
                                start_response,
                                "already_active",
                                "A linked attempt is already running; wait for it to finish",
                                None,
                                active_dag_run_id=child_id,
                            )
                        if not _attempt_conf_matches(
                            child_run.get("conf"),
                            handoff_path=handoff_path,
                            stage=stage,
                            target_hardware=target_hardware,
                            change_summary=change_summary,
                            parent_assembly=selected_assembly,
                            purpose=purpose,
                        ):
                            return self._rerun_refusal(
                                start_response,
                                "attempt_mismatch",
                                "An existing linked attempt uses different parameters; wait for it "
                                "to finish or restore the original parameters",
                                None,
                                active_dag_run_id=child_id,
                            )
                        return self._accepted_attempt(start_response, child_id, dag_run_id, stage, child_state)
                    if child_run is not None:
                        # The scheduler says the attempt ended while the registration is
                        # still open: only reconciliation may decide, never a second run.
                        raise RerunUncertainty("the open attempt disagrees with the recorded scheduler state")
                    if child_stage is not None and child_stage != stage:
                        return self._rerun_refusal(
                            start_response,
                            "already_active",
                            "A linked attempt is already incomplete; retry later",
                            None,
                            active_dag_run_id=child_id,
                        )
                    # Same stage and the trigger outcome is still unknown: reuse the exact
                    # admitted id only when this retry equals the immutable admission.
                    try:
                        native_child = native_run_id(child_id)
                        child_record = self._platform().run_record(native_child)
                        admission = (
                            self._platform().request_record(child_record.get("request_id"))
                            if isinstance(child_record, dict)
                            else None
                        )
                    except PlatformStoreError as error:
                        raise RerunUncertainty("the admitted frame cannot be read") from error
                    if not _admission_matches(
                        admission,
                        principal=principal,
                        target_hardware=target_hardware,
                        parent_assembly=selected_assembly,
                        purpose=purpose,
                        frame_digest=_attempt_frame_digest(
                            handoff_path=handoff_path,
                            stage=stage,
                            target_hardware=target_hardware,
                            change_summary=change_summary,
                            parent_assembly=selected_assembly,
                            purpose=purpose,
                        ),
                    ):
                        return self._rerun_refusal(
                            start_response,
                            "attempt_mismatch",
                            "An existing linked attempt differs from this admission request; wait "
                            "for it to finish or restore the original request",
                            None,
                            active_dag_run_id=child_id,
                        )
                    reserved_id = child_id
                rows = self._rerun_rows(native_run_id(dag_run_id), parent_conf)
                if rows is None:
                    return self._rerun_refusal(
                        start_response,
                        "endpoint_evidence_unavailable",
                        "Cannot currently confirm a reusable checkpoint; retry later",
                        None,
                    )
                row = next((item for item in rows if isinstance(item, dict) and item.get("stage") == stage), None)
                if row is None or row.get("eligible") is not True:
                    prerequisites = (
                        row.get("prerequisites")
                        if isinstance(row, dict) and isinstance(row.get("prerequisites"), dict)
                        else {}
                    )
                    return self._rerun_refusal(
                        start_response,
                        str((row or {}).get("reason") or "not_ready") if isinstance(row, dict) else "not_ready",
                        str((row or {}).get("message") or "This step cannot currently be rerun"),
                        _optional_str(prerequisites.get("earliest_required")),
                    )
                new_run_id = reserved_id or f"portal-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(4)}"
                # The child's initiator is the authenticated actor of THIS request (Airflow
                # stamps it as the run's triggering user); the parent's original submitter
                # stays lineage via parent_dag_run_id and the parent's own record - it is
                # never copied into the create body, so an administrator re-running someone
                # else's parent can never impersonate the original user.
                conf = {
                    "handoff_path": handoff_path,
                    "parent_dag_run_id": dag_run_id,
                    "resume_from": stage,
                    "purpose": purpose,
                    "target_hardware": target_hardware,
                    "change_summary": change_summary,
                }
                if selected_assembly is not None:
                    conf["main_assembly"] = selected_assembly
                admit_baseline: dict | None = None
                if reserved_id is None and stage == "freeze":
                    resolved = resolved_freeze
                    if resolved is None:
                        raise PortalError(HTTPStatus.CONFLICT, "Freeze retry identity was not resolved")
                    admit_baseline = {
                        "handoff_sha256": resolved.handoff_sha256,
                        "files": resolved.files,
                        "bytes": resolved.bytes,
                        "folder": resolved.folder,
                        "platform_config_digest": self._admission_config_digest(),
                    }
                    if resolved.provenance is not None:
                        admit_baseline["provenance"] = resolved.provenance
                if reserved_id is None:
                    # The canonical admission registers the new request, its linked run and
                    # the initial dispatch intent in one gate-locked transaction - before
                    # any trigger; a closed gate refuses here with nothing left behind.
                    try:
                        self._platform().admit_run(
                            request_id=f"portal-{new_run_id}",
                            run_id=native_run_id(new_run_id),
                            principal=principal,
                            target_hardware=target_hardware,
                            main_assembly=selected_assembly or "",
                            configuration={"purpose": purpose},
                            baseline=admit_baseline,
                            dispatch=_initial_dispatch(
                                dag_id=self.config.dag_id,
                                dag_run_id=new_run_id,
                                stage=stage,
                                params_digest=_attempt_frame_digest(
                                    handoff_path=handoff_path,
                                    stage=stage,
                                    target_hardware=target_hardware,
                                    change_summary=change_summary,
                                    parent_assembly=selected_assembly,
                                    purpose=purpose,
                                ),
                            ),
                            from_stage=stage,
                            parent_run_id=native_run_id(dag_run_id),
                        )
                    except OpenAttemptConflictError as error:
                        # A concurrent submission won the reservation; answer with its attempt.
                        return self._rerun_refusal(
                            start_response,
                            "already_active",
                            "A concurrent linked attempt has started; refresh and retry",
                            None,
                            active_dag_run_id=self._admitted_dag_run_id(error.request_id),
                        )
                    except AdmissionClosedError:
                        return _json_response(
                            start_response,
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "Platform admission is closed; rerun was not admitted"},
                            self.config,
                        )
                    except PlatformStoreError:
                        return _json_response(
                            start_response,
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "Platform service is temporarily unavailable; rerun was not admitted"},
                            self.config,
                        )
                outcome = self._trigger_linked_attempt(session, new_run_id, conf)
                if outcome == "auth":
                    raise PortalError(HTTPStatus.UNAUTHORIZED, "Feishu session expired; sign in again")
                if outcome == "absent":
                    # The durable reservation is the platform admission itself; a retry
                    # reuses the exact admitted child id.
                    return _json_response(
                        start_response,
                        HTTPStatus.BAD_GATEWAY,
                        {
                            "error": (
                                "Start state is unconfirmed; check the run list for the new "
                                "attempt and do not submit again"
                            ),
                            "dag_run_id": new_run_id,
                        },
                        self.config,
                    )
        except RerunUncertainty:
            return self._rerun_refusal(
                start_response,
                "unresolved_outcome",
                "Cannot currently confirm run state; retry later",
                None,
            )
        except AirflowAuthError as error:
            forbidden = isinstance(error.__cause__, urlerror.HTTPError) and error.__cause__.code == 403
            raise PortalError(HTTPStatus.FORBIDDEN if forbidden else HTTPStatus.UNAUTHORIZED, str(error)) from error
        except AirflowApiError as error:
            code = error.__cause__.code if isinstance(error.__cause__, urlerror.HTTPError) else None
            status = code if code in {404, 409} else HTTPStatus.BAD_GATEWAY
            raise PortalError(status, str(error)) from error
        log.info(
            "portal rerun parent=%s child=%s stage=%s principal=%s",
            dag_run_id,
            new_run_id,
            stage,
            session.principal,
        )
        return _json_response(
            start_response,
            HTTPStatus.ACCEPTED,
            self._attempt_payload(new_run_id, dag_run_id, stage, "queued"),
            self.config,
        )

    # --------------------------------------------------------------- preview
    def _authorize_run(self, session: PortalSession, dag_run_id: str) -> str:
        """Every preview and every artifact re-authorizes against Airflow first.

        An expired or revoked session, a deleted tenant or a run the operator
        may not read can never be served from the preview cache or the endpoint.
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
        """The one DB read projection: registered run and stage rows of this attempt.

        Members are the registered stage ``receipt.artifacts`` rows
        (``{path, bytes, sha256}``) with their ``checkpoint_uri``; expected
        digests always come from that registration, never from current bytes.
        """

        record = self._platform_read(lambda: self._platform().run_record(run_id))
        if not isinstance(record, dict):
            return None
        stages = self._platform_read(lambda: self._platform().stage_records(run_id))
        stages = stages if isinstance(stages, dict) else {}
        by_stage: dict[str, dict] = {}
        for stage in _STAGE_READ_ORDER:
            row = stages.get(stage)
            if not isinstance(row, dict):
                continue
            receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
            members: dict[str, dict] = {}
            for artifact in receipt.get("artifacts") if isinstance(receipt.get("artifacts"), list) else []:
                if not isinstance(artifact, dict):
                    continue
                path = artifact.get("path")
                if not isinstance(path, str) or not path:
                    continue
                members.setdefault(
                    path,
                    {
                        "bytes": artifact.get("bytes"),
                        "sha256": artifact.get("sha256"),
                        "checkpoint_uri": row.get("checkpoint_uri"),
                        "stage": stage,
                        "state": row.get("state"),
                    },
                )
            by_stage[stage] = members
        members: dict[str, dict] = {}
        for stage in _STAGE_READ_ORDER:
            for path, member in by_stage.get(stage, {}).items():
                members.setdefault(path, member)
        return {"run": record, "stages": stages, "members": members, "by_stage": by_stage}

    def _registered_stage_report(
        self,
        projection: dict,
        *,
        run_id: str,
        stage: str,
    ) -> dict | None:
        """One stage's immutable report, bound by its platform receipt.

        Native stage reports are registered members when the receipt carries them.
        Portable receipts bind the report through ``stage_report`` instead. Both
        paths verify the registered bytes before parsing; a missing report leaves
        the display observation unchanged, while a present but conflicting report
        refuses rather than manufacturing check results.
        """

        stages = projection.get("stages")
        row = stages.get(stage) if isinstance(stages, dict) else None
        if not isinstance(row, dict):
            return None
        source_run = self._registered_checkpoint_origin(row, run_id=run_id, stage=stage)
        if source_run is None:
            if row.get("reused_from_run") is not None:
                raise PortalError(
                    HTTPStatus.BAD_GATEWAY,
                    "Registered stage report source cannot be verified",
                )
            source_run = run_id
        members = projection.get("by_stage", {}).get(stage)
        member = members.get("reports/native-stages.json") if isinstance(members, dict) else None
        if isinstance(member, dict):
            data = self._registered_member_bytes("reports/native-stages.json", member)
        else:
            receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
            reference = receipt.get("stage_report")
            if reference is None:
                return None
            if not isinstance(reference, dict):
                raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered stage report is incomplete")
            path = reference.get("path")
            expected = reference.get("sha256")
            checkpoint = row.get("checkpoint_uri")
            if (
                not isinstance(path, str)
                or not isinstance(expected, str)
                or _SHA256_TEXT.fullmatch(expected) is None
                or not isinstance(checkpoint, str)
                or not checkpoint
            ):
                raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered stage report is incomplete")
            try:
                validate_artifact_name(path)
                platform = self._platform_read(self._platform)
                root = Path(platform.sealed_path(checkpoint)).resolve()
                target = (root / path).resolve()
                if root not in target.parents:
                    raise OSError("stage report escapes the registered checkpoint")
                length = target.stat().st_size
            except (PlatformStoreError, PipelineError, EndpointProtocolError, OSError, ValueError) as error:
                raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered stage report is incomplete") from error
            data = self._registered_member_bytes(
                path,
                {
                    "bytes": length,
                    "sha256": expected,
                    "checkpoint_uri": checkpoint,
                },
            )
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
    def _registered_checkpoint_origin(row: dict, *, run_id: str, stage: str) -> str | None:
        """The proven sealed source of a registered stage checkpoint."""

        checkpoint = row.get("checkpoint_uri")
        if not isinstance(checkpoint, str) or not checkpoint:
            return None
        if not checkpoint.startswith(SEALED_URI_PREFIX):
            return run_id if row.get("reused_from_run") is None else None
        try:
            checkpoint_run, checkpoint_stage = parse_sealed_uri(checkpoint)
        except PipelineError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered stage checkpoint is malformed") from error
        if checkpoint_stage != stage:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Registered stage checkpoint names another stage")
        return checkpoint_run

    def _registered_stage_job(self, job: dict | None, projection: dict | None) -> dict | None:
        """A display snapshot whose stage states come from registered rows.

        A completed platform row can arrive as a bare live event after its check
        observations have already been sealed. Rebuild each registered stage from
        the authoritative row state and its receipt-bound report, then pass the
        result through the unchanged fail-closed ``stage_view``. Unregistered
        stages retain their live observations so in-progress runs remain visible.
        """

        if not isinstance(job, dict) or not isinstance(projection, dict):
            return job
        stages = projection.get("stages")
        if not isinstance(stages, dict) or not stages:
            return job
        registered_run = projection.get("run")
        run_id = registered_run.get("run_id") if isinstance(registered_run, dict) else None
        if not isinstance(run_id, str) or not run_id:
            run_id = job.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return job
        original_events = job.get("events") if isinstance(job.get("events"), list) else []
        events: list[dict] = []
        for definition in CONTRACT["stages"]:
            stage_id = definition["id"]
            row = stages.get(stage_id)
            if not isinstance(row, dict):
                events.extend(
                    event
                    for event in original_events
                    if isinstance(event, dict) and event.get("stage") == stage_id
                )
                continue
            original_stage_events = [
                event for event in original_events if isinstance(event, dict) and event.get("stage") == stage_id
            ]
            reused_from = row.get("reused_from_run")
            reuse = None
            if isinstance(reused_from, str) and reused_from:
                reuse = {"parent_run": reused_from, "reused": True}
                source_run = self._registered_checkpoint_origin(row, run_id=run_id, stage=stage_id)
                if source_run is not None:
                    reuse["source_run"] = source_run
            report = self._registered_stage_report(projection, run_id=run_id, stage=stage_id)
            reported = None
            if isinstance(report, dict):
                rows = report.get("stages")
                if (
                    not isinstance(rows, list)
                    or any(not isinstance(item, dict) for item in rows)
                    or [item.get("id") for item in rows] != [item["id"] for item in CONTRACT["stages"]]
                    or len(rows) != len(CONTRACT["stages"])
                ):
                    raise PortalError(
                        HTTPStatus.BAD_GATEWAY,
                        "Registered stage report check inventory is malformed",
                    )
                reported = next(item for item in rows if item.get("id") == stage_id)
            observed_at = {
                (
                    (event.get("check") or {}).get("boundary"),
                    (event.get("check") or {}).get("id"),
                ): event.get("at")
                for event in original_stage_events
                if isinstance(event.get("check"), dict)
                and isinstance(event.get("at"), str)
                and event.get("at")
            }
            if isinstance(reported, dict):
                for boundary in ("input", "output"):
                    rows = reported.get(f"{boundary}_qc")
                    expected = [item["id"] for item in definition[f"{boundary}_qc"]]
                    if rows is None:
                        continue
                    if (
                        not isinstance(rows, list)
                        or any(not isinstance(item, dict) for item in rows)
                        or [item.get("id") for item in rows] != expected
                    ):
                        raise PortalError(
                            HTTPStatus.BAD_GATEWAY,
                            "Registered stage report check inventory is malformed",
                        )
                    for item in rows:
                        check_state = item.get("state")
                        details = item.get("details")
                        if check_state not in {"passed", "failed"}:
                            continue
                        if not isinstance(details, dict):
                            raise PortalError(
                                HTTPStatus.BAD_GATEWAY,
                                "Registered stage report check details are malformed",
                            )
                        events.append(
                            {
                                "stage": stage_id,
                                "state": "running",
                                **(
                                    {"at": observed_at[(boundary, item["id"])]}
                                    if (boundary, item["id"]) in observed_at
                                    else {}
                                ),
                                **({"reuse": reuse} if reuse is not None else {}),
                                "check": {
                                    "id": item["id"],
                                    "boundary": boundary,
                                    "state": check_state,
                                    "details": details,
                                },
                            }
                        )
            else:
                expected = {
                    boundary: [item["id"] for item in definition[f"{boundary}_qc"]]
                    for boundary in ("input", "output")
                }
                for event in original_stage_events:
                    check = event.get("check")
                    if not isinstance(check, dict):
                        continue
                    boundary = check.get("boundary")
                    identifier = check.get("id")
                    if boundary not in expected or identifier not in expected[boundary]:
                        continue
                    if check.get("state") not in {"passed", "failed"} or not isinstance(check.get("details"), dict):
                        raise PortalError(
                            HTTPStatus.BAD_GATEWAY,
                            "Registered stage check observation is malformed",
                        )
                    events.append(
                        {
                            "stage": stage_id,
                            "state": "running",
                            **({"at": event["at"]} if isinstance(event.get("at"), str) else {}),
                            **({"reuse": reuse} if reuse is not None else {}),
                            "check": {
                                "id": identifier,
                                "boundary": boundary,
                                "state": check["state"],
                                "details": check["details"],
                            },
                        }
                    )
            platform_state = row.get("state")
            at = row.get("updated_at") or row.get("original_executed_at")
            if hasattr(at, "isoformat"):
                at = at.isoformat()
            if not isinstance(at, str) or not at:
                at = next(
                    (
                        event.get("at")
                        for event in reversed(original_stage_events)
                        if isinstance(event.get("at"), str) and event.get("at")
                    ),
                    None,
                )
            events.append(
                {
                    "stage": stage_id,
                    "state": (
                        platform_state
                        if platform_state in {"completed", "failed", "blocked", "running"}
                        else "not_run"
                    ),
                    **({"at": at} if at is not None else {}),
                    **({"reuse": reuse} if reuse is not None else {}),
                }
            )
        display = dict(job)
        display["events"] = events
        # Registered stage rows own the terminal partition. The mutable job's
        # coarse failure flag must not convert a final blocked row to failed.
        display.pop("status", None)
        return display

    @staticmethod
    def _registered_member_fields(member: dict) -> tuple[str, int, str]:
        """The digest, byte length and checkpoint of one completely registered member."""

        expected = member.get("sha256")
        length = member.get("bytes")
        checkpoint = member.get("checkpoint_uri")
        if not (
            isinstance(expected, str)
            and _SHA256_TEXT.fullmatch(expected)
            and type(length) is int
            and length >= 0
            and isinstance(checkpoint, str)
            and checkpoint
        ):
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Sealed-file registration is incomplete")
        return expected, length, checkpoint

    def _registered_member_bytes(self, path: str, member: dict) -> bytes:
        """One registered member's sealed bytes, verified against its registration.

        The one check both read surfaces share: the artifact download and the
        preview's evidence parsing (quality report, warnings) consume the same
        registered digest, length and checkpoint, so replaced, missing or
        incompletely registered bytes refuse identically with the fixed 502
        classes - the preview can never self-sign a serving decision or degrade
        to empty findings.
        """

        expected, length, checkpoint = self._registered_member_fields(member)
        platform = self._platform_read(self._platform)
        try:
            root = Path(platform.sealed_path(checkpoint)).resolve()
        except (PlatformStoreError, PipelineError, OSError, ValueError) as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Sealed-file registration is incomplete") from error
        try:
            target = (root / path).resolve()
            if root not in target.parents:
                raise OSError("member path escapes the registered checkpoint")
            data = target.read_bytes()
        except OSError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Delivery artifact digest verification failed") from error
        if len(data) != length or hashlib.sha256(data).hexdigest() != expected:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Delivery artifact digest verification failed")
        return data

    def _preview_from_projection(self, run_id: str, projection: dict) -> dict | None:
        """One preview payload over the registered members of this attempt.

        A completed verify stage serves the verified preview; a failed verify
        stage with registered members serves the explicitly unverified
        diagnostic preview; anything else returns ``None`` (readiness).
        """

        stages = projection.get("stages")
        verify = stages.get("verify") if isinstance(stages, dict) else None
        state = verify.get("state") if isinstance(verify, dict) else None
        members = projection.get("by_stage", {}).get("verify") or {}
        if state != "completed" and not (state == "failed" and members):
            return None
        receipt = verify.get("receipt") if isinstance(verify.get("receipt"), dict) else {}
        verified = state == "completed"
        # Every advertised member must be completely registered before any byte is
        # served or parsed: an incomplete registration refuses outright, never a
        # half-signed projection.
        for member in members.values():
            self._registered_member_fields(member)
        files = {
            path: member["sha256"]
            for path, member in members.items()
            if _member_class(path) == "subject" and path != "manifest.json"
        }
        report_files = {path: member["sha256"] for path, member in members.items() if _member_class(path) == "report"}
        if "urdf/robot.urdf" not in files:
            raise PipelineError("The registered delivery carries no urdf/robot.urdf member")
        payload = {
            "schema_version": "solidworks-to-urdf.preview/v1",
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "purpose": self._registered_purpose(projection),
            "subject_sha256": receipt.get("subject_sha256"),
            "urdf": "urdf/robot.urdf",
            "files": files,
            "report_files": report_files,
            "verified": verified,
            "state": "verified" if verified else "unverified",
        }
        quality_member = members.get("reports/quality.json")
        quality = (
            _parse_registered_json(self._registered_member_bytes("reports/quality.json", quality_member))
            if isinstance(quality_member, dict)
            else None
        )
        if verified:
            warnings = _preview_warnings(_quality_warnings(quality))
            payload.update(
                {
                    "warnings": warnings[:_PREVIEW_LIMIT],
                    "warnings_total": len(warnings),
                }
            )
        else:
            if quality is not None and quality.get("passed") is True:
                # A failed-verify registration whose report claims success contradicts
                # the registered stage outcome: the diagnostic is refused, while the
                # failed products stay registered and downloadable.
                raise PortalError(
                    HTTPStatus.BAD_GATEWAY, "Registered diagnostic report conflicts with the failed-stage registration"
                )
            failing = _failing_checks(quality or {})
            warnings = _preview_warnings(
                _quality_warnings(quality),
                _delivery_warnings(
                    {
                        path: self._registered_member_bytes(path, member)
                        for path, member in members.items()
                        if path in {"input/handoff.json", "evidence/discovery.json"}
                    }
                ),
            )
            payload.update(
                {
                    "reason": "verification_failed",
                    "error": str(receipt.get("error") or ""),
                    "failing_checks": failing[:_PREVIEW_LIMIT],
                    "failing_checks_total": len(failing),
                    "warnings": warnings[:_PREVIEW_LIMIT],
                    "warnings_total": len(warnings),
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

        member = (projection.get("by_stage", {}).get("freeze") or {}).get("input/request.json")
        if not isinstance(member, dict):
            return None
        try:
            record = _parse_registered_json(self._registered_member_bytes("input/request.json", member))
        except Exception:  # noqa: BLE001 - display read; served members keep their own validation
            return None
        purpose = record.get("purpose") if isinstance(record, dict) else None
        return purpose if purpose in REQUEST_PURPOSES else None

    def _preview_payload(self, session: PortalSession, dag_run_id: str, *, missing_ok: bool = False) -> dict | None:
        run_id = self._authorize_run(session, dag_run_id)
        now = time.time()
        with self._lock:
            cached = self._previews.get(run_id)
        if cached is not None and now - cached[0] <= self.config.preview_ttl:
            return cached[1]
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
        with self._lock:
            self._previews[run_id] = (now, preview)
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

    def _retained_report_links(self, dag_run_id: str) -> list[dict]:
        """Downloadable entries of the automatically retained structural-team report.

        The run page links these directly through the authorized artifact route; the
        same resolver backs the download so a link can never point at a missing member.
        """

        if _RUN_ID.fullmatch(dag_run_id) is None:
            return []
        projection = self._read_projection(native_run_id(dag_run_id))
        resolved = projection["members"] if projection is not None else {}
        return [
            {
                "path": artifact,
                "label": _STRUCTURAL_REPORT_LABELS.get(artifact, artifact),
                "url": f"/api/runs/{dag_run_id}/artifacts/{artifact}",
            }
            for artifact in TEAM_REPORTS
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
        start_response("200 OK", _common_headers(headers, self.config))
        return [data]

    # ---------------------------------------------------------------- helpers
    def _endpoint(self) -> WindowsEndpoint:
        endpoint = self.config.endpoint
        return endpoint() if callable(endpoint) else endpoint

    def _job_snapshot(self, run_id: str, endpoint: WindowsEndpoint) -> dict | None:
        """The run snapshot: the registered delivery is the authority once admitted.

        Once the attempt entered the platform store, its registered outcome,
        identity, events and publication submission (each byte-bound at
        registration and re-bound on the identity read) own the snapshot's
        authoritative fields; the native endpoint only supplements observation
        details (activity telemetry, native findings, error text) and can never
        override or resurrect the registered outcome/submission - a stale or
        synthetic native result is never displayed over the registered delivery.
        Without a registration the native job is served as-is.
        """
        native = None
        try:
            native = endpoint.get_job(run_id)
        except (EndpointNotFound, EndpointError):
            # An admitted attempt stays readable even when Windows is unreachable.
            native = None
        record = self._platform_read(lambda: self._platform().run_record(run_id))
        if not isinstance(record, dict):
            return native
        registered = self._platform_read(lambda: flow.registered_delivery_snapshot(self._platform(), run_id))
        observation = {
            key: native[key] for key in _OBSERVATION_ONLY_JOB_FIELDS if isinstance(native, dict) and key in native
        }
        observation["native_status"] = native.get("status") if isinstance(native, dict) else None
        snapshot = {**observation, **registered}
        if registered.get("error") is None and isinstance(observation.get("error"), str):
            # No registered failure: the native half's error text stays an observation.
            snapshot["error"] = observation["error"]
        # The registered publication carries its own repository identity; the
        # displayed PR is validated against exactly that, never the native claim.
        submission = registered.get("result", {}).get("submission") or {}
        if isinstance(submission.get("repository_slug"), str):
            snapshot["repository_slug"] = submission["repository_slug"]
        if isinstance(submission.get("base"), str):
            snapshot["repository_base"] = submission["base"]
        return snapshot

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
        f"{DEFAULT_SESSION_COOKIE}={value}",
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


def _common_headers(headers: list[tuple[str, str]], config: PortalConfig) -> list[tuple[str, str]]:
    csp = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
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
