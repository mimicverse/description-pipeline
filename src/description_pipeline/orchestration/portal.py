"""Minimal authenticated operator portal for one SolidWorks engineering folder.

The portal is a dependency-free WSGI application:

* the operator signs in once with Feishu SSO at ``/auth/feishu/login``; the Airflow ``_token``
  cookie that Airflow issues is validated server-side through ``/auth/feishu/profile`` and its
  value is kept in a portal session, never echoed to the browser;
* the portal triggers the ``solidworks_to_urdf`` DAG with exactly one value, ``handoff_path``, and
  reports the Airflow stage progress, native findings and the published pull request;
* the Windows endpoint bearer token also stays server-side; the portal proxies the delivery
  preview and only serves URDF/mesh artifacts whose bytes match the digest-bound preview;
* the bundled viewer renders the actual verified URDF with joint and limit controls.

Run ``python -m description_pipeline.orchestration.portal --help`` for the configuration surface.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
import time
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

from ..stages import CONTRACT, stage_view
from .airflow_client import (
    EndpointConfig,
    EndpointError,
    EndpointNotFound,
    EndpointProtocolError,
    ResultNotPublishable,
    WindowsEndpoint,
    check_result,
    native_run_id,
    validate_artifact_name,
    validate_handoff_path,
    verified_result,
)
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
    RetryAssessment,
    classify_transport_retry,
    recorded_actor_principal,
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
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_ARTIFACT_TYPES = {
    ".urdf": "application/xml",
    ".xml": "application/xml",
    ".stl": "model/stl",
    ".obj": "text/plain",
    ".dae": "model/vnd.collada+xml",
    ".json": "application/json",
    ".png": "image/png",
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


_AIRFLOW_OPENER = urlrequest.build_opener(_SameOriginRedirect())


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

    def list_dag_runs(self, token: str, dag_id: str, *, limit: int = 20) -> list[dict]:
        """Recent runs of one DAG, so the operator page survives portal restarts.

        The per-DAG GET is authorized as a read of that DAG; the POST wildcard list would ask for
        authorization on ``~`` and is deliberately not used.
        """
        query = urlparse.urlencode({"limit": int(limit), "order_by": "-start_date"})
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
    port: int = 8780
    session_ttl: float = 12 * 3600.0
    artifact_limit: int = 64 * 1024 * 1024
    preview_ttl: float = 60.0
    max_body_bytes: int = 64 * 1024


_CONFIG_KEYS = {
    "airflow": {"url"},
    "endpoint": {"url", "token_file"},
    "portal": {"host", "port"},
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
    """Load the one JSON config file; see ``portal.example.json`` in this package."""
    data = _read_config(path)
    unknown = set(data) - set(_CONFIG_KEYS)
    if unknown:
        raise AirflowApiError(f"portal config has unknown sections: {sorted(unknown)}")
    airflow_section = _section(data, "airflow")
    portal_section = _section(data, "portal")
    endpoint_section = _section(data, "endpoint")
    url = airflow_section.get("url")
    if not isinstance(url, str) or not url.strip():
        raise AirflowApiError("portal config needs airflow.url")
    return PortalConfig(
        airflow=AirflowApi(url.strip()),
        endpoint=_endpoint_from_config(endpoint_section),
        host=str(portal_section.get("host") or "127.0.0.1"),
        port=int(portal_section.get("port", 8780)),
    )


@dataclass
class PortalRun:
    dag_run_id: str
    handoff_path: str
    principal: str
    started_at: float


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


def _run_identity(actor: object, record: PortalRun | None) -> tuple[str | None, str | None]:
    """One run's stable principal and recorded Feishu name, from Airflow's own record only.

    The in-memory record may only supply a missing principal for a run this process just
    started; a display name is never taken from portal memory.
    """
    principal, name = _actor_identity(actor)
    if principal is None and record is not None:
        return record.principal, None
    return principal, name


def _discovery_evidence(detail: object, digest: str) -> dict:
    """One finding's diagnostic payload, always bound to the raw discovery record digest."""
    evidence: dict = {}
    if detail not in (None, "", [], {}):
        evidence["detail"] = detail
    if digest:
        evidence["discovery_sha256"] = digest
    return evidence


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
                "stage": (job.get("events") or [{}])[-1].get("stage", ""),
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
                            or "自动检查未通过"
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
            "message": "原生 CAD 解析未通过；请按逐项发现修正工程源",
        }
    if status == "failed":
        return {"state": "failed", "job_state": status, "checks": checks, "message": message}
    if status in {"queued", "running"}:
        return {"state": status, "job_state": status, "checks": checks}
    return {"state": "unverified", "job_state": status, "checks": checks, "message": message}


#: Canonical responsibility rows of docs/mechanical-handoff-spec.md §11.1. The automatic column
#: below mirrors only the spec's explicit coverage statements; every engineering item stays
#: pending until the review PR/native controlled records carry the confirmation for this version.
def _retry_assessment(run: dict, tasks: list[dict], job: dict | None, endpoint_error: str | None) -> RetryAssessment:
    states: dict[str, object] = {}
    for task in tasks:
        name = task.get("task_id")
        if not isinstance(name, str) or not name:
            return RetryAssessment(False, "unknown_failed_task", ())
        states[name] = AMBIGUOUS_TASK_STATE if name in states or task.get("map_index", -1) != -1 else task.get("state")
    assessment = classify_transport_retry(run.get("state"), states)
    if not assessment.eligible:
        return assessment
    if endpoint_error:
        return RetryAssessment(False, "endpoint_evidence_unavailable", ())
    if job is None:
        # An authoritative 404 permits the first submission to resume from the saved resolution.
        return (
            assessment
            if states.get("start_job") == "failed"
            else RetryAssessment(False, "endpoint_evidence_unavailable", ())
        )
    if job.get("status") == "failed":
        return RetryAssessment(False, "native_terminal_failure", ())
    if job.get("status") not in {"queued", "running", "passed"}:
        return RetryAssessment(False, "endpoint_evidence_unavailable", ())
    return assessment


def _coverage_report(job: dict | None) -> dict:
    """Per-item canonical check state bound to the native structural identity of this run."""
    job = job if isinstance(job, dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    discovery = job.get("discovery") if isinstance(job.get("discovery"), dict) else {}
    return {
        "structure": {
            "dag_run_id": str(job.get("dag_run_id") or ""),
            "hardware_id": str(job.get("hardware_id") or discovery.get("hardware_id") or ""),
            "revision": str(job.get("revision") or discovery.get("revision") or ""),
            "subject_sha256": str(result.get("subject_sha256") or ""),
            "discovery_sha256": str(discovery.get("discovery_sha256") or ""),
        },
        "automatic": {
            "state": "unsupported-present",
            "unsupported": [item["label"] for item in CONTRACT["unsupported"]],
            "message": "其余检查项按已发布的自动校验执行；未覆盖项必须由工程师确认。",
        },
        "engineering": {
            "state": "pending",
            "items": [
                {
                    "id": item["label"],
                    "review_stage": item["review_stage"],
                    "state": "pending",
                    "subject_sha256": result.get("subject_sha256"),
                    "reference": item["reference"],
                }
                for item in CONTRACT["confirmations"]
            ],
            "message": "工程确认由结构负责人在本版本原生工程及评审 PR 中完成，本页只显示逐项待确认状态。",
        },
    }


class PortalApp:
    """WSGI application serving the operator page, its API and digest-verified artifacts."""

    def __init__(self, config: PortalConfig) -> None:
        self.config = config
        self.sessions = SessionStore(config.session_ttl)
        self._lock = threading.Lock()
        self._runs: dict[str, PortalRun] = {}
        self._previews: dict[str, tuple[float, dict]] = {}

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
        if path == "/api/session":
            if method == "GET":
                return self._session_info(environ, start_response)
            if method == "POST":
                raise PortalError(HTTPStatus.METHOD_NOT_ALLOWED, "请求方法不被允许")
            if method == "DELETE":
                return self._logout(environ, start_response)
        if path == "/api/runs":
            if method == "GET":
                return self._list_runs(environ, start_response)
            if method == "POST":
                return self._start_run(environ, start_response)
        match = re.fullmatch(r"/api/runs/([^/]+)", path)
        if match and method == "GET":
            return self._run_status(environ, start_response, match.group(1))
        match = re.fullmatch(r"/api/runs/([^/]+)/retry", path)
        if match and method == "POST":
            return self._retry_run(environ, start_response, match.group(1))
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
            raise PortalError(HTTPStatus.UNAUTHORIZED, "请使用飞书登录")
        if csrf:
            supplied = str(environ.get("HTTP_X_CSRF_TOKEN") or "")
            if not supplied or not hmac.compare_digest(supplied, session.csrf_token):
                raise PortalError(HTTPStatus.FORBIDDEN, "请求校验失败，请刷新页面重试")
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
            raise PortalError(HTTPStatus.UNAUTHORIZED, "飞书登录已过期，请重新登录") from error
        except AirflowApiError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        open_id = profile.get("open_id")
        name = profile.get("name")
        principal = profile.get("principal")
        # The auth manager only mints sessions with a verified, recordable display name (and the
        # actor value must fit the metadata column); anything else is refused rather than shown
        # as an identifier or the viewing operator.
        if not isinstance(open_id, str) or not open_id.strip():
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Airflow 未返回有效的飞书身份")
        if not isinstance(principal, str) or not principal.strip():
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Airflow 未返回稳定的飞书身份")
        try:
            recorded = recorded_display_name(name)
            build_actor_name(principal.strip(), recorded)
        except ValueError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Airflow 未返回可记录的飞书身份") from error
        avatar = profile.get("avatar_url")
        return self.sessions.create(
            recorded,
            token,
            avatar_url=avatar.strip() if isinstance(avatar, str) else "",
            principal=principal.strip(),
        )

    def _can_manage(self, session: PortalSession, run: dict) -> bool:
        """Recheck the caller's current profile and the authoritative run actor."""
        try:
            profile = self.config.airflow.profile(session.token)
        except AirflowAuthError as error:
            self.sessions.drop(session.session_id)
            raise PortalError(HTTPStatus.UNAUTHORIZED, "飞书登录已过期，请重新登录") from error
        except AirflowApiError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        principal = profile.get("principal")
        try:
            build_actor_name(principal, recorded_display_name(profile.get("name")))
        except ValueError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, "Airflow 未返回可记录的飞书身份") from error
        components = [profile.get(key) for key in ("app_id", "tenant_key", "open_id")]
        if (
            not all(isinstance(value, str) and value for value in components)
            or principal != ":".join(components)
            or principal != session.principal
        ):
            self.sessions.drop(session.session_id)
            raise PortalError(HTTPStatus.UNAUTHORIZED, "飞书身份已变更，请重新登录")
        return profile.get("role") == "ADMIN" or recorded_actor_principal(run.get("triggering_user_name")) == principal

    def _session_info(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        cookies = _parse_cookies(environ.get("HTTP_COOKIE"))
        session = self.sessions.get(cookies.get(DEFAULT_SESSION_COOKIE))
        headers: list[tuple[str, str]] = []
        if session is None:
            # First request after the Feishu callback: adopt the Airflow cookie and remember the
            # session in the browser with the portal's own opaque cookie.
            session = self._adopt_airflow_session(cookies.get(AIRFLOW_TOKEN_COOKIE))
            if session is None:
                raise PortalError(HTTPStatus.UNAUTHORIZED, "请使用飞书登录")
            headers.append(("Set-Cookie", _session_cookie(session.session_id, environ, self.config)))
        return _json_response(
            start_response,
            200,
            {
                "authenticated": True,
                "user": session.user,
                "avatar_url": session.avatar_url,
                "csrf_token": session.csrf_token,
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
    def _list_runs(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        session = self._session(environ)
        with self._lock:
            local = {run.dag_run_id: run for run in self._runs.values()}
        try:
            airflow_runs = self.config.airflow.list_dag_runs(session.token, self.config.dag_id, limit=20)
        except AirflowAuthError as error:
            raise PortalError(HTTPStatus.UNAUTHORIZED, str(error)) from error
        except AirflowApiError:
            airflow_runs = []
        runs = []
        seen = set()
        for item in airflow_runs:
            dag_run_id = str(item.get("dag_run_id") or "")
            if not dag_run_id or dag_run_id in seen:
                continue
            seen.add(dag_run_id)
            record = local.get(dag_run_id)
            conf = item.get("conf") if isinstance(item.get("conf"), dict) else {}
            principal, name = _run_identity(item.get("triggering_user_name"), record)
            runs.append(
                {
                    "dag_run_id": dag_run_id,
                    "handoff_path": conf.get("handoff_path") or (record.handoff_path if record else None),
                    # The recorded initiator, never the current viewer; missing names stay null.
                    "user": name,
                    # Airflow's own record outlives any portal restart.
                    "principal": principal,
                    "state": item.get("state"),
                    "started_at": item.get("start_date"),
                }
            )
        for record in sorted(local.values(), key=lambda item: item.started_at, reverse=True):
            if record.dag_run_id not in seen:
                runs.append(
                    {
                        "dag_run_id": record.dag_run_id,
                        "handoff_path": record.handoff_path,
                        # No in-memory name fallback: the authoritative run record is the only
                        # source for a displayed submitter.
                        "user": None,
                        "principal": record.principal,
                        "state": None,
                        "started_at": datetime.fromtimestamp(record.started_at, UTC).isoformat(),
                    }
                )
        return _json_response(
            start_response,
            200,
            {"runs": runs[:20]},
            self.config,
        )

    def _start_run(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        session = self._session(environ, csrf=True)
        payload = self._body(environ)
        if set(payload) - {"handoff_path"}:
            raise PortalError(HTTPStatus.BAD_REQUEST, "只允许提供工程文件夹路径，不接受其他运行参数")
        try:
            handoff_path = validate_handoff_path(payload.get("handoff_path"))
        except EndpointProtocolError as error:
            raise PortalError(HTTPStatus.BAD_REQUEST, str(error)) from error
        dag_run_id = f"portal-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(4)}"
        try:
            self.config.airflow.trigger_dag_run(
                session.token,
                self.config.dag_id,
                dag_run_id,
                {"handoff_path": handoff_path},
            )
        except AirflowAuthError as error:
            raise PortalError(HTTPStatus.UNAUTHORIZED, str(error)) from error
        except AirflowApiError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        with self._lock:
            self._runs[dag_run_id] = PortalRun(
                dag_run_id=dag_run_id,
                handoff_path=handoff_path,
                principal=session.principal,
                started_at=time.time(),
            )
        log.info("portal started dag_run_id=%s user=%s principal=%s", dag_run_id, session.user, session.principal)
        return _json_response(start_response, 201, {"dag_run_id": dag_run_id}, self.config)

    def _run_status(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        session = self._session(environ)
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "运行标识不合法")
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
            job = endpoint.get_job(run_id)
        except EndpointNotFound:
            job = None
        except EndpointError as error:
            endpoint_error = str(error)
        conf = airflow_run.get("conf") if isinstance(airflow_run.get("conf"), dict) else {}
        with self._lock:
            record = self._runs.get(dag_run_id)
        principal, operator = _run_identity(airflow_run.get("triggering_user_name"), record)
        retry = _retry_assessment(airflow_run, tasks, job, endpoint_error)
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
        return _json_response(
            start_response,
            200,
            {
                "dag_run_id": dag_run_id,
                "state": airflow_run.get("state"),
                "handoff_path": conf.get("handoff_path"),
                "operator": operator,
                "principal": principal,
                "can_manage": can_manage,
                "retry": {"eligible": retry.eligible, "reason": retry.reason},
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
                "job": None if job is None else {"status": job.get("status"), "error": job.get("error")},
                "discovery": None
                if not isinstance(job, dict) or not isinstance(job.get("discovery"), dict)
                else {
                    "passed": job["discovery"].get("passed"),
                    "hardware_id": job["discovery"].get("hardware_id"),
                    "revision": job["discovery"].get("revision"),
                    "discovery_sha256": job["discovery"].get("discovery_sha256"),
                },
                "stage_view": stage_view(job),
                "findings": _findings(job),
                "automatic": _automatic_summary(job),
                "coverage": _coverage_report(job),
                "pr": pr,
                "endpoint_error": endpoint_error,
            },
            self.config,
        )

    def _retry_run(self, environ: dict, start_response: Callable, dag_run_id: str) -> Iterable[bytes]:
        session = self._session(environ, csrf=True)
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "运行标识不合法")
        if self._body(environ):
            raise PortalError(HTTPStatus.BAD_REQUEST, "重试不接受运行参数")
        try:
            run = self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
            if not self._can_manage(session, run):
                raise PortalError(HTTPStatus.FORBIDDEN, "只有发起人或平台管理员可重试此运行")
            tasks = self.config.airflow.task_instances(session.token, self.config.dag_id, dag_run_id)
            job = None
            try:
                job = self._endpoint().get_job(native_run_id(dag_run_id))
            except EndpointNotFound:
                pass
            except EndpointError as error:
                raise PortalError(HTTPStatus.CONFLICT, "无法确认原作业状态，请稍后重试") from error
            assessment = _retry_assessment(run, tasks, job, None)
            if not assessment.eligible:
                raise PortalError(HTTPStatus.CONFLICT, "此运行无法重试，请查看检查结果并按需新建运行")
            preview = self.config.airflow.clear_dag_run(session.token, self.config.dag_id, dag_run_id, dry_run=True)
            selected = preview.get("task_instances")
            if (
                not isinstance(selected, list)
                or type(preview.get("total_entries")) is not int
                or preview.get("total_entries") != len(selected)
                or any(not isinstance(task, dict) for task in selected)
            ):
                raise PortalError(HTTPStatus.CONFLICT, "Airflow 未提供有效的重试任务检查结果")
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
                    raise PortalError(HTTPStatus.CONFLICT, "重试任务与原运行不一致")
                selected_states[name] = task.get("state")
            if tuple(sorted(selected_states)) != assessment.cleared_tasks:
                raise PortalError(HTTPStatus.CONFLICT, "运行状态已变化，请刷新后重试")
            if any(
                selected_states[task["task_id"]] != task.get("state")
                for task in tasks
                if task.get("task_id") in selected_states
            ):
                raise PortalError(HTTPStatus.CONFLICT, "运行状态已变化，请刷新后重试")
            result = self.config.airflow.clear_dag_run(session.token, self.config.dag_id, dag_run_id, dry_run=False)
            if result.get("dag_run_id") != dag_run_id or result.get("dag_id") != self.config.dag_id:
                raise PortalError(HTTPStatus.BAD_GATEWAY, "Airflow 返回的重试运行身份不一致")
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

    # --------------------------------------------------------------- preview
    def _preview_payload(self, session: PortalSession, dag_run_id: str) -> dict:
        if _RUN_ID.fullmatch(dag_run_id) is None:
            raise PortalError(HTTPStatus.BAD_REQUEST, "运行标识不合法")
        # Every preview and every artifact re-authorizes against Airflow first: an expired or
        # revoked session, a deleted tenant or a run the operator may not read can never be served
        # from this cache or from the endpoint.
        try:
            self.config.airflow.dag_run(session.token, self.config.dag_id, dag_run_id)
        except AirflowAuthError as error:
            raise PortalError(HTTPStatus.UNAUTHORIZED, "飞书登录已失效，请重新登录") from error
        except AirflowApiError as error:
            raise PortalError(HTTPStatus.NOT_FOUND, "该运行不存在或无权访问") from error
        run_id = native_run_id(dag_run_id)
        now = time.time()
        with self._lock:
            cached = self._previews.get(run_id)
        if cached is not None and now - cached[0] <= self.config.preview_ttl:
            return cached[1]
        endpoint = self._endpoint()
        try:
            job = endpoint.get_job(run_id)
            if str(job.get("status") or "") in {"queued", "running"}:
                raise PortalError(HTTPStatus.NOT_FOUND, "该运行还没有可展示的已验证交付")
            verified_result(job.get("result"))
            preview = endpoint.get_preview(run_id)
        except EndpointNotFound as error:
            raise PortalError(HTTPStatus.NOT_FOUND, "该运行还没有可展示的已验证交付") from error
        except ResultNotPublishable as error:
            raise PortalError(HTTPStatus.CONFLICT, f"模型尚未通过独立校验：{error}") from error
        except EndpointError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
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
        preview = self._preview_payload(session, dag_run_id)
        digest = preview["files"].get(artifact)
        if not isinstance(digest, str):
            raise PortalError(HTTPStatus.NOT_FOUND, "该文件不在已验证交付清单中")
        endpoint = self._endpoint()
        try:
            data = endpoint.read_artifact(
                native_run_id(dag_run_id),
                artifact,
                sha256=digest,
                limit=self.config.artifact_limit,
            )
        except EndpointNotFound as error:
            raise PortalError(HTTPStatus.NOT_FOUND, "该文件不在已验证交付清单中") from error
        except EndpointProtocolError as error:
            log.warning("artifact verification failed run=%s name=%s: %s", dag_run_id, artifact, error)
            raise PortalError(HTTPStatus.BAD_GATEWAY, "交付文件摘要校验失败，已拒绝提供") from error
        except EndpointError as error:
            raise PortalError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        content_type = _ARTIFACT_TYPES.get(Path(artifact).suffix.lower(), "application/octet-stream")
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(data))),
            ("Cache-Control", "private, max-age=300"),
        ]
        start_response("200 OK", _common_headers(headers, self.config))
        return [data]

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

    def _body(self, environ: dict) -> dict:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError) as error:
            raise PortalError(HTTPStatus.BAD_REQUEST, "请求长度不合法") from error
        if length < 0 or length > self.config.max_body_bytes:
            raise PortalError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求内容过大")
        raw = environ["wsgi.input"].read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PortalError(HTTPStatus.BAD_REQUEST, "请求内容不是合法 JSON") from error
        if not isinstance(payload, dict):
            raise PortalError(HTTPStatus.BAD_REQUEST, "请求内容不是 JSON 对象")
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
