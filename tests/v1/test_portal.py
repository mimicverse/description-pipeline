"""End-to-end tests for the operator portal: Airflow auth, one-field start, verified preview."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import ClassVar
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from socketserver import ThreadingMixIn
from types import SimpleNamespace
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    PIPELINE_ID,
    WindowsEndpoint,
    EndpointError,
    native_run_id,
)
from description_pipeline.orchestration.feishu_oauth import TRIGGERING_USER_NAME_LIMIT
from description_pipeline.orchestration.portal import (
    _attempt_frame_digest,
    AirflowApi,
    AirflowApiError,
    DEFAULT_DAG_ID,
    PortalApp,
    PortalConfig,
    load_portal_config,
)

os.environ.setdefault("PLATFORM_CONFIG_DIGEST", "c" * 64)
from description_pipeline.orchestration.uploads import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES
from description_pipeline.io import PipelineError
from description_pipeline.platform.store import (
    AdmissionClosedError,
    OpenAttemptConflictError,
    PlatformStoreError,
)
from description_pipeline.sources.solidworks import discovery
from description_pipeline.stages import CONTRACT

from .protocol_support import protocol_events

AIRFLOW_TOKEN = "airflow-session-token"
FEISHU_NAME = "崔工"
FEISHU_PRINCIPAL = "cli_app:tenant-a:ou_worker"
VIEWER_TOKEN = "airflow-viewer-token"
VIEWER_NAME = "李工"
VIEWER_PRINCIPAL = "cli_app:tenant-a:ou_viewer"
ADMIN_TOKEN = "airflow-admin-token"
NO_NAME_TOKEN = "airflow-no-name-token"
NO_NAME_PRINCIPAL = "cli_app:tenant-a:ou_noname"
DAG_RUN_ID = "portal-20261007T000000-abcdef01"
RUN_ID = native_run_id(DAG_RUN_ID)
_DEFAULT_ACTOR = object()
TEST_TARGETS = {"m3.0": {"base": "main"}, "m3.1": {"base": "main"}}
TEST_TARGET = "m3.0"
TEST_SUMMARY = "control fixture change summary"
UPLOAD_CHOICES = [
    ("target_hardware", TEST_TARGET),
    ("change_summary", TEST_SUMMARY),
    ("purpose", "engineering"),
]


def _feishu_profile(name: str, principal: str, open_id: str) -> dict:
    app_id, tenant_key, _ = principal.split(":")
    return {
        "open_id": open_id,
        "app_id": app_id,
        "name": name,
        "avatar_url": "https://avatar/u",
        "tenant_key": tenant_key,
        "principal": principal,
        "role": "OPERATOR",
    }


def _actor_envelope(name: str, principal: str) -> str:
    """The auth-owned ``triggering_user_name`` envelope the Feishu auth manager stamps."""
    return f"{principal}|{json.dumps(name, ensure_ascii=False)}"


class MockAirflow:
    """Minimal Airflow 3.3.2 surface: the Feishu profile bridge plus the v2 DAG-run endpoints."""

    def __init__(self) -> None:
        self.dag_runs: dict[str, dict] = {}
        self.profiles: dict[str, dict] = {
            AIRFLOW_TOKEN: _feishu_profile(FEISHU_NAME, FEISHU_PRINCIPAL, "ou_worker"),
            VIEWER_TOKEN: _feishu_profile(VIEWER_NAME, VIEWER_PRINCIPAL, "ou_viewer"),
            # Simulates a stale/foreign token: the auth manager would refuse to mint one.
            NO_NAME_TOKEN: _feishu_profile("", NO_NAME_PRINCIPAL, "ou_noname"),
        }
        self.profiles[ADMIN_TOKEN] = {
            **_feishu_profile("平台管理员", "cli_app:tenant-a:ou_admin", "ou_admin"),
            "role": "ADMIN",
        }
        self.task_states: dict[str, list[dict]] = {}
        self.clear_requests: list[tuple[str, dict]] = []
        self.clear_selection: list[dict] | None = None
        self.conf: dict | None = None
        self.trigger_payloads: list[dict] = []
        self.hits = 0
        self.revoked = False
        self.deny_runs = False
        self.trigger_fail_times = 0
        self.trigger_fail_after_create = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                return

            def _reply(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _token(self) -> str:
                header = self.headers.get("Authorization") or ""
                return header[len("Bearer ") :] if header.startswith("Bearer ") else ""

            def _authorized(self) -> bool:
                if outer.revoked or self._token() not in outer.profiles:
                    self._reply(401, {"detail": "unauthorized"})
                    return False
                return True

            def do_POST(self) -> None:
                outer.hits += 1
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if not self._authorized():
                    return
                if self.path == "/api/v2/dags/~/dagRuns/list":
                    # Operators are not authorized on the wildcard DAG; the client must not use it.
                    self._reply(403, {"detail": "forbidden on wildcard"})
                    return
                if self.path == "/api/v2/dags/solidworks_to_urdf/dagRuns":
                    payload = json.loads(body or b"{}")
                    outer.trigger_payloads.append(payload)
                    if "logical_date" not in payload:
                        # Airflow 3.3.2 TriggerDAGRunPostBody requires this nullable key; the mock
                        # mirrors the strict model so a body-only regression cannot pass here.
                        self._reply(
                            422,
                            {"detail": [{"type": "missing", "loc": ["body", "logical_date"], "msg": "Field required"}]},
                        )
                        return
                    dag_run_id = payload.get("dag_run_id")
                    if outer.trigger_fail_times > 0 and not outer.trigger_fail_after_create:
                        outer.trigger_fail_times -= 1
                        self._reply(500, {"detail": "transient trigger failure"})
                        return
                    outer.conf = payload.get("conf")
                    outer.dag_runs[dag_run_id] = {
                        "dag_run_id": dag_run_id,
                        "dag_id": "solidworks_to_urdf",
                        "state": "running",
                        "conf": payload.get("conf"),
                        "triggering_user_name": _actor_envelope(
                            outer.profiles[self._token()]["name"],
                            outer.profiles[self._token()]["principal"],
                        ),
                        "start_date": "2026-10-07T00:00:00Z",
                        "end_date": None,
                    }
                    if outer.trigger_fail_times > 0 and outer.trigger_fail_after_create:
                        outer.trigger_fail_times -= 1
                        self._reply(500, {"detail": "trigger outcome unknown"})
                        return
                    self._reply(201, dict(outer.dag_runs[dag_run_id]))
                    return
                match = re.fullmatch(r"/api/v2/dags/solidworks_to_urdf/dagRuns/([^/]+)/clear", self.path)
                if match:
                    run_id = match.group(1)
                    payload = json.loads(body or b"{}")
                    outer.clear_requests.append((self._token(), payload))
                    selected = outer.clear_selection
                    if selected is None:
                        selected = [
                            row
                            for row in outer.task_states.get(run_id, [])
                            if row["state"] in {"failed", "upstream_failed"}
                        ]
                    if payload.get("dry_run"):
                        self._reply(200, {"task_instances": selected, "total_entries": len(selected)})
                    else:
                        outer.dag_runs[run_id]["state"] = "queued"
                        for row in outer.task_states[run_id]:
                            if row["state"] in {"failed", "upstream_failed"}:
                                row["state"] = None
                        self._reply(200, dict(outer.dag_runs[run_id]))
                    return
                self._reply(404, {"detail": "not found"})

            def do_GET(self) -> None:
                outer.hits += 1
                if self.path == "/auth/feishu/profile":
                    cookie = self.headers.get("Cookie") or ""
                    token = cookie[len("_token=") :] if cookie.startswith("_token=") else ""
                    profile = outer.profiles.get(token)
                    if profile is None or outer.revoked:
                        self._reply(401, {"detail": "not_signed_in"})
                        return
                    self._reply(200, dict(profile))
                    return
                if not self._authorized():
                    return
                if self.path == "/api/v2/dags/solidworks_to_urdf":
                    self._reply(200, {"dag_id": "solidworks_to_urdf", "is_paused": False})
                    return
                if self.path.startswith("/api/v2/dags/solidworks_to_urdf/dagRuns?"):
                    runs = sorted(outer.dag_runs.values(), key=lambda run: run.get("start_date") or "", reverse=True)
                    query = dict(urlparse.parse_qsl(urlparse.urlsplit(self.path).query))
                    offset = int(query.get("offset") or 0)
                    limit = int(query.get("limit") or 20)
                    self._reply(
                        200,
                        {"dag_runs": runs[offset : offset + limit], "total_entries": len(runs)},
                    )
                    return
                prefix = "/api/v2/dags/solidworks_to_urdf/dagRuns/"
                if outer.deny_runs and self.path.startswith(prefix):
                    self._reply(403, {"detail": "forbidden"})
                    return
                if self.path.startswith(prefix):
                    rest = self.path[len(prefix) :]
                    dag_run_id, _, suffix = rest.partition("/")
                    run = outer.dag_runs.get(dag_run_id)
                    if run is None:
                        self._reply(404, {"detail": "unknown dag run"})
                        return
                    if suffix == "taskInstances":
                        self._reply(
                            200,
                            {
                                "task_instances": outer.task_states.get(
                                    dag_run_id,
                                    [
                                        {"task_id": "freeze", "state": "success"},
                                        {"task_id": "discover", "state": "success"},
                                        {"task_id": "capture", "state": "success"},
                                        {"task_id": "generate", "state": "success"},
                                        {"task_id": "verify", "state": "success"},
                                        {"task_id": "publish", "state": "success"},
                                    ],
                                ),
                                "total_entries": 6,
                            },
                        )
                        return
                    if suffix == "":
                        self._reply(200, dict(run))
                        return
                self._reply(404, {"detail": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> MockAirflow:
        self.thread.start()
        return self

    def __exit__(self, *args) -> None:
        self.server.shutdown()
        self.server.server_close()


class RedirectAirflow:
    """Redirect every request to another origin; used to test token-leak protection."""

    def __init__(self, target: str) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                return

            def _redirect(self) -> None:
                self.send_response(302)
                self.send_header("Location", target + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                self._redirect()

            def do_POST(self) -> None:
                self._redirect()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> RedirectAirflow:
        self.thread.start()
        return self

    def __exit__(self, *args) -> None:
        self.server.shutdown()
        self.server.server_close()


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args) -> None:
        return


def _airflow_token_cookie(value: str) -> Cookie:
    """The Airflow SSO cookie a signed-in browser would carry back to the same origin."""
    return Cookie(
        version=0,
        name="_token",
        value=value,
        port=None,
        port_specified=False,
        domain="127.0.0.1",
        domain_specified=False,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


class PortalClient:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = CookieJar()
        # The portal tests talk to loopback fixtures; ambient HTTP(S)_PROXY must not route
        # them through an external proxy (the production internal opener is direct too).
        self.opener = urlrequest.build_opener(urlrequest.ProxyHandler({}), urlrequest.HTTPCookieProcessor(self.jar))
        self.csrf: str | None = None
        self.login_response = b""
        self.last_set_cookies: list[str] = []

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        request = urlrequest.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=15) as response:
                self.last_set_cookies = list(response.headers.get_all("Set-Cookie") or [])
                return response.status, dict(response.headers), response.read()
        except urlerror.HTTPError as error:
            self.last_set_cookies = list(error.headers.get_all("Set-Cookie") or [])
            return error.code, dict(error.headers), error.read()

    def login(self, token: str = AIRFLOW_TOKEN) -> None:
        """The browser already holds the Feishu SSO cookie; the portal adopts it server-side."""
        self.jar.set_cookie(_airflow_token_cookie(token))
        status, _, body = self.request("GET", "/api/session")
        assert status == 200, body
        self.login_response = body
        self.csrf = json.loads(body)["csrf_token"]

    def request_raw(self, method: str, path: str, body: bytes, content_type: str, headers: dict | None = None):
        headers = {"Accept": "application/json", "Content-Type": content_type, **(headers or {})}
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        request = urlrequest.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                self.last_set_cookies = list(response.headers.get_all("Set-Cookie") or [])
                return response.status, dict(response.headers), response.read()
        except urlerror.HTTPError as error:
            self.last_set_cookies = list(error.headers.get_all("Set-Cookie") or [])
            return error.code, dict(error.headers), error.read()


def _endpoint_files() -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in _endpoint_artifacts().items()}


def _endpoint_artifacts() -> dict[str, bytes]:
    return {
        "urdf/robot.urdf": b"<robot name='fixture'/>",
        "meshes/base.stl": b"solid base",
        "meshes/a b.stl": b"solid spaced",
    }


def _preview_payload() -> dict:
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "subject_sha256": "a" * 64,
        "urdf": "urdf/robot.urdf",
        "files": _endpoint_files(),
    }


def _upload_body(
    folder: str = "机器人工程",
    files: list[tuple[str, bytes]] | None = None,
    fields: list[tuple[str, str]] | None = None,
) -> bytes:
    """One browser-shaped multipart folder body (top folder included, ``/`` separators)."""
    if fields is None:
        fields = list(UPLOAD_CHOICES)
    entries = files or [("model.SLDASM", b"<assembly/>"), ("parts/p1.SLDPRT", b"<part1/>")]
    chunks = []
    for rel, data in entries:
        chunks.append(b"--portal-boundary\r\n")
        chunks.append(
            b'Content-Disposition: form-data; name="files"; filename="' + f"{folder}/{rel}".encode() + b'"\r\n'
        )
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(data + b"\r\n")
    for name, value in fields or ():
        chunks.append(b"--portal-boundary\r\n")
        chunks.append(b'Content-Disposition: form-data; name="' + name.encode() + b'"\r\n\r\n')
        chunks.append(value.encode() + b"\r\n")
    chunks.append(b"--portal-boundary--\r\n")
    return b"".join(chunks)


class FakeEndpoint:
    """Two-leg endpoint stand-in: job snapshots and rerun plans only, no preview route."""

    def __init__(self, token: str = "test-token") -> None:
        self.token = token
        self.jobs: dict[str, dict] = {}
        self.rerun_payloads: dict[str, dict] = {}
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                return

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                outer.requests.append(self.path)
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self._send(401, {"error": "unauthorized"})
                    return
                if not self.path.startswith("/v1/jobs/"):
                    self._send(404, {"error": "not found"})
                    return
                parts = self.path.split("/")
                run_id = parts[3] if len(parts) > 3 else ""
                if len(parts) >= 5 and parts[4] == "reruns":
                    payload = outer.rerun_payloads.get(run_id)
                    if payload is None:
                        self._send(404, {"error": "no rerun plan"})
                        return
                    self._send(200, payload)
                    return
                if len(parts) != 4:
                    self._send(404, {"error": "not found"})
                    return
                job = outer.jobs.get(run_id)
                if job is None:
                    self._send(404, {"error": "unknown run_id"})
                    return
                self._send(200, job)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeEndpoint:
        return self

    def __exit__(self, *args) -> None:
        self.close()


class _DigestStream:
    """Bounded faithful artifact stream: the bound digest is verified at EOF."""

    def __init__(self, data: bytes, expected: str) -> None:
        self._data = data
        self._expected = expected
        self._offset = 0
        self._verified = False

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._data):
            if not self._verified:
                self._verified = True
                if hashlib.sha256(self._data).hexdigest() != self._expected:
                    raise PipelineError("Delivery artifact failed its digest check")
            return b""
        end = len(self._data) if size is None or size < 0 else min(len(self._data), self._offset + size)
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk

    def close(self) -> None:
        return

    def __enter__(self) -> _DigestStream:
        return self

    def __exit__(self, *args) -> None:
        return


class PreviewStore:
    """Bounded faithful double of the LinuxStore preview/artifact ownership.

    It mirrors the exact surface the portal consumes (meta, delivery_dir, preview,
    open_artifact, merged_job); the real store integrity rules stay covered by the
    store's own tests.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.runs: dict[str, dict] = {}
        self.root = Path(root) if root is not None else Path("/bounded-store")

    def run_dir(self, run_id: str) -> Path:
        path = self.root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def add(
        self,
        run_id: str,
        *,
        meta: dict | None = None,
        delivery: bool = False,
        preview: dict | None = None,
        artifacts: dict[str, bytes] | None = None,
        checkpoints: tuple[str, ...] = (),
        merged: dict | None = None,
    ) -> None:
        self.runs[run_id] = {
            "meta": meta,
            "delivery": delivery,
            "preview": preview,
            "artifacts": dict(artifacts or {}),
            "checkpoints": tuple(checkpoints),
            "merged": dict(merged or {}),
        }

    def meta(self, run_id: str) -> dict | None:
        entry = self.runs.get(run_id)
        if entry is None or entry["meta"] is None:
            return None
        return dict(entry["meta"])

    def delivery_dir(self, run_id: str) -> Path | None:
        entry = self.runs.get(run_id)
        return self.root / run_id / "delivery" if entry and entry["delivery"] else None

    def preview(self, run_id: str) -> dict:
        entry = self.runs.get(run_id)
        if entry is None or entry["preview"] is None:
            raise PipelineError("The retained delivery has not passed independent verification")
        return dict(entry["preview"])

    def open_artifact(self, run_id: str, name: str, *, sha256: str):
        entry = self.runs.get(run_id) or {}
        preview = entry.get("preview") or {}
        expected = (preview.get("files") or {}).get(name)
        if expected is None:
            expected = (preview.get("report_files") or {}).get(name)
        if expected != sha256:
            raise PipelineError("The requested artifact is not part of the previewed delivery")
        data = (entry.get("artifacts") or {}).get(name)
        if data is None:
            raise PipelineError("The previewed delivery no longer carries this artifact")
        # The digest is verified on the stream at EOF, exactly like the real store: tampered
        # bytes surface as a stream failure, never as a missing binding.
        return _DigestStream(data, sha256), len(data)

    def checkpoint_dir(self, run_id: str, stage: str) -> Path:
        entry = self.runs.get(run_id)
        stages = (entry or {}).get("checkpoints") or ()
        if stage not in stages:
            raise PipelineError(f"The attempt retains no {stage} checkpoint")
        return Path("/bounded-store") / run_id / stage

    def merged_job(self, run_id: str, native: dict | None = None) -> dict:
        merged = (
            dict(native)
            if isinstance(native, dict)
            else {
                "schema_version": "solidworks-to-urdf.job/v1",
                "pipeline_id": PIPELINE_ID,
                "run_id": run_id,
                "events": [],
                "result": None,
                "error": None,
            }
        )
        entry = self.runs.get(run_id) or {}
        merged.update(entry.get("merged") or {})
        return merged


class FakePlatformStore:
    """The portal-side admission surface: open by default, recording every admit."""

    def __init__(self) -> None:
        self.state = "open"
        self.read_error: Exception | None = None
        self.admit_error: Exception | None = None
        self.admits: list[dict] = []
        #: Admitted run rows as the read side sees them: the DB reservation authority.
        self.runs: dict[str, dict] = {}
        #: Admitted request rows as the read side sees them (the immutable frame).
        self.requests: dict[str, dict] = {}
        #: Explicit stage-row overrides (terminal freeze failures) keyed by run id.
        self.stages: dict[str, dict] = {}
        self.displays: dict[str, object] = {}
        #: The one preview fixture this double projects registered reads from.
        self.fixtures: PreviewStore | None = None

    # ------------------------------------------- registered read projection (IF-13)
    def _entry(self, run_id: str) -> dict | None:
        if self.fixtures is None:
            return None
        entry = self.fixtures.runs.get(run_id)
        return entry if isinstance(entry, dict) and entry.get("meta") is not None else None

    def run_record(self, run_id: str) -> dict | None:
        if run_id in self.runs:
            return dict(self.runs[run_id])
        entry = self._entry(run_id)
        if entry is None:
            return None
        merged = entry.get("merged") or {}
        state = merged.get("status")
        return {
            "run_id": run_id,
            "state": str(state) if isinstance(state, str) and state else None,
            "request_id": None,
            "parent_run_id": None,
            "from_stage": None,
            "tool_identity": None,
        }

    def confirmed_stage(self, run_id: str, stage: str):
        """The validated reuse read the flow consumes: the completed row's receipt."""
        row = self.stage_records(run_id).get(stage)
        if not isinstance(row, dict) or row.get("state") != "completed":
            return None
        return row.get("receipt")

    def _stage_members(self, run_id: str) -> list[dict]:
        entry = self._entry(run_id) or {}
        preview = entry.get("preview")
        if not isinstance(preview, dict):
            return []
        files = {**(preview.get("files") or {}), **(preview.get("report_files") or {})}
        artifacts = entry.get("artifacts") or {}
        rows = []
        for path in sorted(files):
            data = artifacts.get(path)
            rows.append(
                {
                    "path": path,
                    "bytes": len(data) if isinstance(data, (bytes, bytearray)) else 0,
                    "sha256": files[path],
                }
            )
        return rows

    def stage_records(self, run_id: str) -> dict[str, dict]:
        override = self.stages.get(run_id)
        if override is not None:
            return {stage: dict(row) for stage, row in override.items()}
        entry = self._entry(run_id)
        if entry is None:
            return {}
        stages: dict[str, dict] = {
            stage: {
                "stage": stage,
                "state": "completed",
                "receipt": {"subject_sha256": None, "artifacts": []},
                "checkpoint_uri": f"store://sealed/{run_id}/{stage}",
                "reused_from_run": None,
                "original_executed_at": None,
            }
            for stage in entry.get("checkpoints") or ()
        }
        if isinstance(entry.get("preview"), dict) and "freeze" in (entry.get("checkpoints") or ()):
            request_digest = (entry["preview"].get("files") or {}).get("input/request.json")
            if isinstance(request_digest, str):
                request_bytes = (entry.get("artifacts") or {}).get("input/request.json")
                stages["freeze"] = {
                    "stage": "freeze",
                    "state": "completed",
                    "receipt": {
                        "subject_sha256": None,
                        "artifacts": [
                            {
                                "path": "input/request.json",
                                "bytes": len(request_bytes) if isinstance(request_bytes, (bytes, bytearray)) else 0,
                                "sha256": request_digest,
                            }
                        ],
                    },
                    "checkpoint_uri": f"store://sealed/{run_id}/freeze",
                    "reused_from_run": None,
                    "original_executed_at": None,
                }
        preview = entry.get("preview")
        if isinstance(preview, dict):
            verified = preview.get("verified") is not False
            stages["verify"] = {
                "stage": "verify",
                "state": "completed" if verified else "failed",
                "receipt": {
                    "subject_sha256": preview.get("subject_sha256"),
                    "artifacts": self._stage_members(run_id),
                    "error": preview.get("error"),
                },
                "checkpoint_uri": f"store://sealed/{run_id}/verify",
                "reused_from_run": None,
                "original_executed_at": None,
            }
        merged = entry.get("merged") or {}
        result = merged.get("result") if isinstance(merged.get("result"), dict) else None
        if result:
            # The publish registration carries what the publish stage produced:
            # the delivered quality and submission, exactly as _platform_receipt
            # copies them from the stage receipt.
            stages["publish"] = {
                "stage": "publish",
                "state": "completed",
                "receipt": {
                    "subject_sha256": result.get("subject_sha256"),
                    "handoff_sha256": (entry.get("meta") or {}).get("handoff_sha256"),
                    "quality": result.get("quality"),
                    "submission": result.get("submission"),
                },
                "checkpoint_uri": f"store://sealed/{run_id}/publish",
                "reused_from_run": None,
                "original_executed_at": None,
            }
        return stages

    def run_events(self, run_id: str, *, after_seq: int = 0, limit: int = 200) -> list[dict]:
        """One bounded page of recorded events in their database row shape."""
        entry = self._entry(run_id) or {}
        events = (entry.get("merged") or {}).get("events")
        rows = []
        for index, event in enumerate(events if isinstance(events, list) else [], start=1):
            event = event if isinstance(event, dict) else {}
            rows.append(
                {
                    "seq": index,
                    "at": event.get("at"),
                    "kind": "stage",
                    "stage": event.get("stage"),
                    "operation_id": None,
                    "payload": event,
                }
            )
        return [row for row in rows if row["seq"] > after_seq][:limit]

    def sealed_path(self, checkpoint_uri: str):
        """Materialize the registered checkpoint members and return their root."""

        prefix = "store://sealed/"
        if not isinstance(checkpoint_uri, str) or not checkpoint_uri.startswith(prefix):
            raise PlatformStoreError("invalid sealed reference")
        parts = checkpoint_uri.removeprefix(prefix).split("/")
        if len(parts) != 2 or not all(parts) or self.fixtures is None:
            raise PlatformStoreError("invalid sealed reference")
        run_id, stage = parts
        entry = self._entry(run_id)
        if entry is None:
            raise PlatformStoreError("the run is not registered")
        base = self.fixtures.root / "sealed" / run_id / stage
        preview = entry.get("preview")
        files = {}
        if isinstance(preview, dict):
            files = {**(preview.get("files") or {}), **(preview.get("report_files") or {})}
        artifacts = entry.get("artifacts") or {}
        for path, _digest in files.items():
            data = artifacts.get(path)
            if not isinstance(data, (bytes, bytearray)):
                continue
            target = base / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(data))
        return base

    def admission(self):
        if self.read_error is not None:
            raise self.read_error
        return SimpleNamespace(state=self.state)

    def admit_run(self, **kwargs) -> None:
        if self.admit_error is not None:
            raise self.admit_error
        self.admits.append(kwargs)
        native = kwargs.get("run_id")
        if isinstance(native, str) and native:
            self.runs[native] = {
                "run_id": native,
                "state": "queued",
                "request_id": kwargs.get("request_id"),
                "parent_run_id": kwargs.get("parent_run_id"),
                "from_stage": kwargs.get("from_stage"),
                "tool_identity": None,
            }
            request_id = kwargs.get("request_id")
            if isinstance(request_id, str) and request_id:
                configuration = kwargs.get("configuration")
                self.requests[request_id] = {
                    "request_id": request_id,
                    "principal": kwargs.get("principal"),
                    "target_hardware": kwargs.get("target_hardware"),
                    "main_assembly": kwargs.get("main_assembly"),
                    "sealed_at": None,
                    "dispatch_digest": (kwargs.get("dispatch") or {}).get("params_digest"),
                    "purpose": (configuration or {}).get("purpose") if isinstance(configuration, dict) else None,
                }

    def seed_open_child(
        self,
        parent_run_id: str,
        dag_run_id: str,
        *,
        from_stage: str = "generate",
        state: str = "queued",
        principal: str = FEISHU_PRINCIPAL,
        target_hardware: str = TEST_TARGET,
        main_assembly: str | None = None,
        change_summary: str = TEST_SUMMARY,
        purpose: str = "engineering",
        handoff_path: str = "/srv/robot-cell",
        dispatch_digest: str | None = None,
    ) -> str:
        """One already-admitted child exactly as the read side finds it (no file state)."""
        native = native_run_id(dag_run_id)
        self.runs[native] = {
            "run_id": native,
            "state": state,
            "request_id": f"portal-{dag_run_id}",
            "parent_run_id": parent_run_id,
            "from_stage": from_stage,
            "tool_identity": None,
        }
        self.requests[f"portal-{dag_run_id}"] = {
            "request_id": f"portal-{dag_run_id}",
            "principal": principal,
            "target_hardware": target_hardware,
            "main_assembly": main_assembly or "",
            "sealed_at": None,
            "purpose": purpose,
            "dispatch_digest": (
                dispatch_digest
                if dispatch_digest is not None
                else _attempt_frame_digest(
                    handoff_path=handoff_path,
                    stage=from_stage,
                    target_hardware=target_hardware,
                    change_summary=change_summary,
                    parent_assembly=main_assembly,
                    purpose=purpose,
                )
            ),
        }
        return native

    def request_record(self, request_id: str) -> dict | None:
        record = self.requests.get(request_id)
        return dict(record) if isinstance(record, dict) else None

    def lifecycle_rows(self) -> list[dict]:
        buckets = {"queued": "in_flight", "running": "in_flight", "unresolved": "unresolved"}
        return [
            {
                "object_kind": "run",
                "object_id": run_id,
                "run_id": run_id,
                "state": record["state"],
                "bucket": buckets.get(str(record["state"]), "terminal"),
                "updated_at": None,
            }
            for run_id, record in self.runs.items()
        ]

    def run_display(self, run_id: str):
        return self.displays.get(run_id)

    def run_displays(self) -> dict:
        return dict(self.displays)


class PortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.airflow = MockAirflow()
        self.airflow.__enter__()
        self.addCleanup(self.airflow.__exit__, None, None, None)
        self.platform = FakePlatformStore()
        self.upload_root = Path(tempfile.mkdtemp(prefix="portal-upload-"))
        self.addCleanup(shutil.rmtree, self.upload_root, ignore_errors=True)
        self.endpoint_server = FakeEndpoint()
        self.endpoint_server.__enter__()
        self.addCleanup(self.endpoint_server.__exit__, None, None, None)
        self.endpoint = WindowsEndpoint(EndpointConfig(base_url=self.endpoint_server.url, token="test-token"))
        self.store_root = Path(tempfile.mkdtemp(prefix="portal-store-"))
        self.addCleanup(shutil.rmtree, self.store_root, ignore_errors=True)
        self.store = PreviewStore(self.store_root)
        self.platform.fixtures = self.store
        self.static_dir = Path(__file__).resolve().parents[2] / "src/description_pipeline/orchestration/static"
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url),
            endpoint=lambda: self.endpoint,
            static_dir=self.static_dir,
            upload_root=self.upload_root,
            platform_store=self.platform,
            pipeline_targets=TEST_TARGETS,
            config_identity_provider=lambda: "c" * 64,
        )
        self.server = make_server(
            "127.0.0.1",
            0,
            PortalApp(config),
            server_class=_ThreadingWSGIServer,
            handler_class=_QuietHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.client = PortalClient(f"http://127.0.0.1:{self.server.server_address[1]}")

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _start_extra_portal(self):
        """A restarted portal process: same Airflow, brand-new (empty) session store."""
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url),
            endpoint=lambda: self.endpoint,
            static_dir=self.static_dir,
            upload_root=self.upload_root,
            platform_store=self.platform,
            pipeline_targets=TEST_TARGETS,
            config_identity_provider=lambda: "c" * 64,
        )
        server = make_server(
            "127.0.0.1", 0, PortalApp(config), server_class=_ThreadingWSGIServer, handler_class=_QuietHandler
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(self._stop_extra_portal, server)
        return server

    def _stop_extra_portal(self, server) -> None:
        server.shutdown()
        server.server_close()

    def _seed_passed_job(self, dag_run_id: str = DAG_RUN_ID) -> str:
        self._seed_airflow_run(dag_run_id, state="success")
        run_id = native_run_id(dag_run_id)
        self._seed_job(dag_run_id, status="passed", result=self._passed_result())
        self.store.add(
            run_id,
            meta={"handoff_sha256": "d" * 64},
            delivery=True,
            preview=_preview_payload(),
            artifacts=_endpoint_artifacts(),
            checkpoints=("capture", "generate", "verify"),
        )
        self._record_fixture_events(run_id)
        return dag_run_id

    #: The registered run vocabulary beside the native job's status words.
    _STORE_RUN_STATE: ClassVar[dict[str, str]] = {"passed": "succeeded", "native_complete": "running"}

    def _record_fixture_events(self, run_id: str) -> None:
        """Mirror the seeded job's own facts into the registered side.

        The runner records the observed stage/check events and the run outcome
        in the platform store, and the publish stage's receipt carries the
        delivered quality and submission - the read authority for the run
        page. The fixture models that by copying the seeded job's facts in.
        """

        job = self.endpoint_server.jobs.get(run_id) or {}
        entry = self.store.runs[run_id]
        merged = {**(entry.get("merged") or {}), "events": job.get("events") or []}
        status = job.get("status")
        state = self._STORE_RUN_STATE.get(str(status), status)
        if isinstance(state, str) and state:
            merged["status"] = state
        if isinstance(job.get("result"), dict):
            merged["result"] = job["result"]
        entry["merged"] = merged
        hardware = job.get("hardware_id")
        if isinstance(hardware, str) and hardware:
            # The registered capture tree carries the frozen source identity the
            # run page reads its hardware/revision from.
            capture = self.store.root / "sealed" / run_id / "capture" / "input"
            capture.mkdir(parents=True, exist_ok=True)
            (capture / "source.json").write_text(
                json.dumps({"identity": {"hardware_id": hardware}, "metadata": {"revision": job.get("revision")}}),
                encoding="utf-8",
            )

    def _rebind_attempt_member(self, run_id: str, path: str, payload: bytes) -> None:
        """Re-register one member with new bytes: the fixture keeps the registration
        consistent, so only the parse/shape handling of the registered content is
        under test (mirroring a producer that registered what it actually wrote)."""

        entry = self.store.runs[run_id]
        entry["artifacts"][path] = payload
        digest = hashlib.sha256(payload).hexdigest()
        for key in ("files", "report_files"):
            if path in (entry["preview"].get(key) or {}):
                entry["preview"][key][path] = digest

    def _passed_result(self, *, subject: str = "a" * 64, quality_subject: str | None = None) -> dict:
        return {
            "passed": True,
            "pipeline_id": PIPELINE_ID,
            "output": "build/out",
            "subject_sha256": subject,
            "quality": {
                "passed": True,
                "subject_sha256": quality_subject or subject,
                "checks": [{"id": "x", "state": "passed"}],
            },
            "submission": {
                "passed": True,
                "subject_sha256": subject,
                "base": "feature/m3.0",
                "branch": "work/solidworks/m3.0/engineering",
                "state": "published",
                "commit": "c" * 40,
                "repository_slug": "example/m3.0",
                "url": "https://github.com/example/m3.0/pull/1",
            },
        }

    def _seed_job(self, dag_run_id: str = DAG_RUN_ID, **overrides) -> dict:
        """Direct fixture seeding: the two-leg job snapshot the portal reads, no endpoint API."""

        run_id = native_run_id(dag_run_id)
        job = {
            "schema_version": "solidworks-to-urdf.job/v1",
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "request": {
                "run_id": run_id,
                "upload_files": {"cad/robot.SLDASM": "a" * 64},
                "upload_sha256": "b" * 64,
                "main_assembly": "cad/robot.SLDASM",
            },
            "status": "passed",
            "events": protocol_events(),
            "result": None,
            "error": None,
            "hardware_id": "m3.0",
            "revision": "r1",
            "repository_slug": "example/m3.0",
            "repository_base": "feature/m3.0",
        }
        job.update(overrides)
        self.endpoint_server.jobs[run_id] = job
        return job

    def _fail_job(self, dag_run_id: str = DAG_RUN_ID) -> None:
        job = self.endpoint_server.jobs[native_run_id(dag_run_id)]
        job.update(
            status="failed",
            error="cad capture failed",
            result=None,
            discovery={
                "passed": False,
                "findings": [
                    {
                        "schema_version": "native-discovery/finding/v1",
                        "code": "discovery.axis_unresolved",
                        "object": "mate:elbow-1",
                        "message": "elbow axis cannot be resolved",
                        "detail": {"route": "native"},
                        "blocking": True,
                    }
                ],
                "hardware_id": "m3.0",
                "revision": "r1",
                "discovery_sha256": "d" * 64,
            },
            events=protocol_events(failed_stage="discover"),
        )
        run_id = native_run_id(dag_run_id)
        if run_id in self.store.runs:
            self._record_fixture_events(run_id)

    def _register_attempt(self, run_id: str, members: dict[str, bytes], *, verified: bool) -> None:
        """One registered attempt: the fixture members are the only servable bytes."""

        files = {
            path: hashlib.sha256(data).hexdigest()
            for path, data in members.items()
            if not path.startswith("reports/") and path != "manifest.json"
        }
        report_files = {
            path: hashlib.sha256(data).hexdigest() for path, data in members.items() if path.startswith("reports/")
        }
        self.store.add(
            run_id,
            meta={"handoff_sha256": "d" * 64},
            delivery=True,
            preview={
                "schema_version": "solidworks-to-urdf.preview/v1",
                "pipeline_id": PIPELINE_ID,
                "run_id": run_id,
                "subject_sha256": "d" * 64,
                "urdf": "urdf/robot.urdf",
                "files": files,
                "report_files": report_files,
                "verified": verified,
                "state": "verified" if verified else "unverified",
                "reason": "verification_failed",
            },
            artifacts=dict(members),
        )
        self._record_fixture_events(run_id)

    def test_run_and_preview_read_the_registered_freeze_purpose_not_the_conf(self) -> None:
        import hashlib

        from description_pipeline import request_evidence

        payload_bytes = request_evidence.request_evidence_bytes(
            request_evidence.build_request_evidence(
                purpose="inspection_preview",
                request_id="fixture-freeze",
                target_hardware="m3.0",
                change_summary="fixture",
                main_assembly="assembly.SLDASM",
                handoff_digest="a" * 64,
                platform_config_digest="c" * 64,
            )
        )
        digest = hashlib.sha256(payload_bytes).hexdigest()
        self.store.add(
            native_run_id(DAG_RUN_ID),
            meta={"handoff_sha256": "e" * 64},
            checkpoints=("freeze",),
            preview={
                "subject_sha256": "d" * 64,
                "verified": True,
                "files": {"urdf/robot.urdf": "f" * 64, "input/request.json": digest},
                "report_files": {},
            },
            artifacts={"urdf/robot.urdf": b"<robot/>", "input/request.json": payload_bytes},
        )
        # A stale/mutable conf echo must never decide either page: the registered
        # freeze request carries inspection_preview while conf says engineering.
        self._seed_airflow_run(
            DAG_RUN_ID,
            state="success",
            conf={
                "handoff_path": "/incoming/folder",
                "target_hardware": "m3.0",
                "change_summary": "x",
                "purpose": "engineering",
            },
        )
        self.client.login()
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["purpose"], "inspection_preview")
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["purpose"], "inspection_preview")
        # An unfinished registration reads as 未记录 on both pages - the conf echo
        # does not leak into the run page.
        self.store.runs[native_run_id(DAG_RUN_ID)]["checkpoints"] = ()
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["purpose"])

    def test_registered_purpose_reads_the_freeze_request_and_never_guesses(self) -> None:
        from description_pipeline import request_evidence

        app = PortalApp.__new__(PortalApp)
        payload = request_evidence.request_evidence_bytes(
            request_evidence.build_request_evidence(
                purpose="inspection_preview",
                request_id="fixture",
                target_hardware="m3.0",
                change_summary="fixture",
                main_assembly="assembly.SLDASM",
                handoff_digest="a" * 64,
                platform_config_digest="c" * 64,
            )
        )
        app._registered_member_bytes = lambda path, member: payload
        projection = {"by_stage": {"freeze": {"input/request.json": {"sha256": "b" * 64}}}}
        self.assertEqual(PortalApp._registered_purpose(app, projection), "inspection_preview")
        self.assertIsNone(PortalApp._registered_purpose(app, {"by_stage": {}}))

        def boom(path, member):
            raise PipelineError("broken member")

        app._registered_member_bytes = boom
        self.assertIsNone(PortalApp._registered_purpose(app, projection))
        app._registered_member_bytes = lambda path, member: b'{"purpose": "unknown"}'
        self.assertIsNone(PortalApp._registered_purpose(app, projection))

    def _seed_airflow_run(
        self,
        dag_run_id: str = DAG_RUN_ID,
        *,
        state: str = "running",
        conf: dict | None = None,
        triggering_user_name: object = _DEFAULT_ACTOR,
    ) -> None:
        self.airflow.dag_runs[dag_run_id] = {
            "dag_run_id": dag_run_id,
            "dag_id": "solidworks_to_urdf",
            "state": state,
            "conf": (
                {
                    "handoff_path": "/srv/robot-cell",
                    "target_hardware": TEST_TARGET,
                    "change_summary": TEST_SUMMARY,
                    "purpose": "engineering",
                }
                if conf is None
                else conf
            ),
            "triggering_user_name": (
                _actor_envelope(FEISHU_NAME, FEISHU_PRINCIPAL)
                if triggering_user_name is _DEFAULT_ACTOR
                else triggering_user_name
            ),
            "start_date": "2026-10-07T00:00:00Z",
            "end_date": "2026-10-07T00:05:00Z" if state != "running" else None,
        }

    def _seed_retryable_run(self) -> None:
        self._seed_passed_job()
        # A retryable attempt is interrupted, not delivered: its registration is
        # non-terminal (the terminal immutability gate owns everything else).
        merged = self.store.runs[native_run_id(DAG_RUN_ID)]["merged"]
        merged["status"] = "running"
        merged.pop("result", None)
        self.airflow.dag_runs[DAG_RUN_ID]["state"] = "failed"
        self.airflow.task_states[DAG_RUN_ID] = [
            {"task_id": "freeze", "state": "success"},
            {"task_id": "discover", "state": "success"},
            {"task_id": "capture", "state": "failed"},
            {"task_id": "generate", "state": "upstream_failed"},
        ]

    def test_retry_capability_uses_current_profile_and_recorded_actor(self) -> None:
        self._seed_retryable_run()
        for token, can_manage in [(AIRFLOW_TOKEN, True), (ADMIN_TOKEN, True), (VIEWER_TOKEN, False)]:
            with self.subTest(token=token):
                client = PortalClient(self.client.base)
                client.login(token)
                status, _, body = client.request("GET", f"/api/runs/{DAG_RUN_ID}")
                self.assertEqual(status, 200, body)
                detail = json.loads(body)
                self.assertIs(detail["can_manage"], can_manage)
                self.assertEqual(
                    detail["retry"],
                    {
                        "eligible": True,
                        "reason": "sealed_evidence_reimport",
                        "classification": "evidence_corruption",
                    },
                )
        self.client.login()
        self.airflow.dag_runs[DAG_RUN_ID]["triggering_user_name"] = f"{FEISHU_PRINCIPAL}|broken"
        self.assertFalse(json.loads(self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")[2])["can_manage"])

    def test_owner_and_admin_retry_preserve_frozen_job_and_successful_tasks(self) -> None:
        for token in [AIRFLOW_TOKEN, ADMIN_TOKEN]:
            with self.subTest(token=token):
                self._seed_retryable_run()
                client = PortalClient(self.client.base)
                client.login(token)
                jobs_before = set(self.endpoint_server.jobs)
                request_before = dict(self.endpoint_server.jobs[native_run_id(DAG_RUN_ID)]["request"])
                conf_before = dict(self.airflow.dag_runs[DAG_RUN_ID]["conf"])
                status, _, body = client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})
                self.assertEqual(status, 200, body)
                self.assertEqual(
                    json.loads(body),
                    {"dag_run_id": DAG_RUN_ID, "state": "queued", "cleared_tasks": ["capture", "generate"]},
                )
                self.assertEqual(self.airflow.dag_runs[DAG_RUN_ID]["conf"], conf_before)
                self.assertEqual(set(self.endpoint_server.jobs), jobs_before)
                self.assertEqual(self.endpoint_server.jobs[native_run_id(DAG_RUN_ID)]["request"], request_before)
                self.assertEqual(
                    [row["state"] for row in self.airflow.task_states[DAG_RUN_ID][:2]], ["success", "success"]
                )
                for dry_run, (caller, payload) in zip([True, False], self.airflow.clear_requests[-2:], strict=True):
                    self.assertEqual(caller, token)
                    self.assertEqual(
                        payload,
                        {"dry_run": dry_run, "only_failed": True, "only_new": False, "run_on_latest_version": False},
                    )

    def test_retry_denies_other_users_missing_csrf_and_request_parameters(self) -> None:
        self._seed_retryable_run()
        viewer = PortalClient(self.client.base)
        viewer.login(VIEWER_TOKEN)
        self.assertEqual(viewer.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})[0], 403)
        self.client.login()
        self.client.csrf = None
        self.assertEqual(self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})[0], 403)
        self.client.login()
        self.assertEqual(self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {"only_failed": False})[0], 400)
        self.assertEqual(self.airflow.clear_requests, [])

    def test_retry_rechecks_admin_role_and_expired_identity(self) -> None:
        self._seed_retryable_run()
        self.client.login(ADMIN_TOKEN)
        self.airflow.profiles[ADMIN_TOKEN]["role"] = "OPERATOR"
        self.assertEqual(self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})[0], 403)
        self.airflow.revoked = True
        self.assertEqual(self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})[0], 401)
        self.assertEqual(self.airflow.clear_requests, [])

    def test_retry_native_failure_and_unavailable_evidence_are_diagnostics(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        self._fail_job()
        # The registered terminal outcome owns the displayed retry advice exactly
        # as it owns the POST decision: the run page never offers the refused action.
        # (The native-side diagnostics stay covered by the classification test.)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        self.assertEqual(
            json.loads(body)["retry"],
            {"eligible": False, "reason": "registered_failed", "classification": "engineering_terminal_failure"},
        )
        self.assertEqual(self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})[0], 409)
        with patch.object(self.endpoint, "get_job", side_effect=EndpointError("offline")):
            status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
            self.assertEqual(status, 200, body)
            self.assertEqual(
                json.loads(body)["retry"],
                {
                    "eligible": False,
                    "reason": "registered_failed",
                    "classification": "engineering_terminal_failure",
                },
            )
            self.assertEqual(self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})[0], 409)
        self.assertEqual(self.airflow.clear_requests, [])

    def test_retry_refuses_unsafe_or_changed_dry_run_task_selection(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        for selected in [
            [{"task_id": "freeze", "state": "failed"}],
            [{"task_id": "capture", "state": "success"}],
            [{"task_id": "capture", "state": "failed"}],
            [{"task_id": "capture", "state": "failed", "dag_run_id": "another-run"}],
            [{"task_id": "capture", "state": "failed", "map_index": 0}],
            [{"task_id": "capture", "state": "failed"}] * 2,
        ]:
            with self.subTest(selected=selected):
                self.airflow.clear_selection = selected
                status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})
                self.assertEqual(status, 409, body)
        self.assertTrue(all(payload["dry_run"] for _, payload in self.airflow.clear_requests))

    def test_retry_classifies_missing_jobs_and_failed_resolution_conservatively(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        self.endpoint_server.jobs.clear()
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        self.assertEqual(
            json.loads(body)["retry"],
            {
                "eligible": False,
                "reason": "endpoint_evidence_unavailable",
                "classification": "temporary_disconnection",
            },
        )
        self.airflow.task_states[DAG_RUN_ID][1]["state"] = "failed"
        self.airflow.task_states[DAG_RUN_ID][2]["state"] = "upstream_failed"
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["cleared_tasks"], ["capture", "discover", "generate"])
        self._seed_retryable_run()
        self.airflow.task_states[DAG_RUN_ID][0]["state"] = "failed"
        self.assertEqual(self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})[0], 409)

    def test_retry_rejects_changed_profile_identity(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        self.airflow.profiles[AIRFLOW_TOKEN]["principal"] = VIEWER_PRINCIPAL
        self.assertEqual(self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})[0], 401)
        self.assertEqual(self.airflow.clear_requests, [])

    def test_login_is_required_and_tokens_stay_server_side(self) -> None:
        status, _, _ = self.client.request("GET", "/api/session")
        self.assertEqual(status, 401)
        status, _, _ = self.client.request("POST", "/api/session", {"username": "operator", "password": "x"})
        self.assertEqual(status, 405)
        self.client.login()
        status, headers, body = self.client.request("GET", "/api/session")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["user"], FEISHU_NAME)
        self.assertEqual(payload["avatar_url"], "https://avatar/u")
        self.assertIn("csrf_token", payload)
        for secret in (AIRFLOW_TOKEN, "test-token"):
            self.assertNotIn(secret.encode(), body)
            self.assertNotIn(secret.encode(), self.client.login_response)
        self.assertNotIn("Secure", headers.get("Set-Cookie", ""))

    def test_a_forged_or_stale_sso_cookie_is_refused(self) -> None:
        self.client.jar.set_cookie(_airflow_token_cookie("forged"))
        status, _, body = self.client.request("GET", "/api/session")
        self.assertEqual(status, 401, body)

    def test_uploaded_folder_starts_exactly_one_run(self) -> None:
        self.client.login()
        files = [("model.SLDASM", b"<assembly/>"), ("parts/p1.SLDPRT", b"<part1/>")]
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(files=files, fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 201, body)
        payload = json.loads(body)
        dag_run_id = payload["dag_run_id"]
        self.assertTrue(dag_run_id.startswith("portal-"))
        self.assertEqual(payload["folder"], "机器人工程")
        self.assertEqual(payload["files"], 2)
        self.assertEqual(payload["bytes"], sum(len(data) for _, data in files))
        self.assertEqual(payload["target_hardware"], TEST_TARGET)
        self.assertEqual(payload["change_summary"], TEST_SUMMARY)
        stored = self.upload_root / dag_run_id / "机器人工程"
        self.assertTrue((stored / "model.SLDASM").is_file())
        expected_conf = {
            "handoff_path": str(stored),
            "main_assembly": "model.SLDASM",
            "purpose": "engineering",
            "request_id": f"portal-{dag_run_id}",
            "target_hardware": TEST_TARGET,
            "change_summary": TEST_SUMMARY,
        }
        self.assertEqual(self.airflow.conf, expected_conf)
        self.assertEqual(
            self.airflow.trigger_payloads[-1],
            {
                "dag_run_id": dag_run_id,
                "logical_date": None,
                "conf": expected_conf,
            },
        )
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 200)
        listed = json.loads(body)["runs"][0]
        self.assertEqual(listed["dag_run_id"], dag_run_id)
        self.assertEqual(listed["handoff_path"], str(stored))
        self.assertEqual(listed["state"], "running")
        self.assertEqual(listed["user"], FEISHU_NAME)
        self.assertEqual(listed["principal"], FEISHU_PRINCIPAL)
        staging = self.upload_root / ".staging"
        self.assertTrue(not staging.exists() or not any(staging.iterdir()))

    def test_a_closed_admission_gate_refuses_before_uploading_or_triggering(self) -> None:
        self.client.login()
        self.platform.state = "closed"
        files = [("model.SLDASM", b"<assembly/>")]
        status, _, body = self.client.request_raw(
            "POST", "/api/runs", _upload_body(files=files), "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 503, body)
        self.assertIn("Platform admission is closed", json.loads(body)["error"])
        self.assertEqual(self.platform.admits, [])
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual([entry.name for entry in self.upload_root.iterdir() if entry.name != ".staging"], [])

    def test_the_submission_admits_the_run_with_its_dispatch_intent(self) -> None:
        self.client.login()
        files = [("model.SLDASM", b"<assembly/>"), ("parts/p1.SLDPRT", b"<part1/>")]
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(files=files, fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 201, body)
        dag_run_id = json.loads(body)["dag_run_id"]
        self.assertEqual(len(self.platform.admits), 1)
        admit = self.platform.admits[0]
        self.assertEqual(admit["request_id"], f"portal-{dag_run_id}")
        self.assertEqual(admit["run_id"], native_run_id(dag_run_id))
        self.assertEqual(admit["principal"], FEISHU_PRINCIPAL)
        self.assertEqual(admit["main_assembly"], "model.SLDASM")
        self.assertEqual(admit["configuration"], {"purpose": "engineering"})
        dispatch = admit["dispatch"]
        self.assertEqual(dispatch["stage"], "capture")
        self.assertEqual(dispatch["operation_key"], f"portal-{dag_run_id}/capture:0")
        self.assertEqual(dispatch["dag_run_id"], dag_run_id)
        self.assertEqual(dispatch["dag_id"], DEFAULT_DAG_ID)
        self.assertRegex(dispatch["params_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(self.airflow.trigger_payloads), 1)
        self.assertEqual(self.airflow.trigger_payloads[0]["dag_run_id"], dag_run_id)
        self.assertEqual(self.airflow.trigger_payloads[0]["conf"]["purpose"], "engineering")

    def test_upload_requires_a_known_purpose(self) -> None:
        self.client.login()
        files = [("model.SLDASM", b"<assembly/>")]
        for label, fields in (
            ("missing", [("target_hardware", TEST_TARGET), ("change_summary", TEST_SUMMARY)]),
            (
                "unknown",
                [("target_hardware", TEST_TARGET), ("change_summary", TEST_SUMMARY), ("purpose", "ghost")],
            ),
        ):
            with self.subTest(case=label):
                status, _, body = self.client.request_raw(
                    "POST",
                    "/api/runs",
                    _upload_body(files=files, fields=fields),
                    "multipart/form-data; boundary=portal-boundary",
                )
                self.assertEqual(status, 400, body)
        self.assertEqual(self.platform.admits, [])
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_an_admission_that_closes_after_upload_removes_the_intake(self) -> None:
        self.client.login()
        self.platform.admit_error = AdmissionClosedError("the gate closed mid-upload")
        files = [("model.SLDASM", b"<assembly/>")]
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(files=files, fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 503, body)
        self.assertIn("was not admitted", json.loads(body)["error"])
        self.assertEqual(self.platform.admits, [])
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual([entry.name for entry in self.upload_root.iterdir() if entry.name != ".staging"], [])

    def test_an_unavailable_platform_refuses_the_submission(self) -> None:
        self.client.login()
        self.platform.read_error = PlatformStoreError("PLATFORM_SERVICE_DSN is not configured")
        files = [("model.SLDASM", b"<assembly/>")]
        status, _, body = self.client.request_raw(
            "POST", "/api/runs", _upload_body(files=files), "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 503, body)
        self.assertIn("temporarily unavailable", json.loads(body)["error"])
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual([entry.name for entry in self.upload_root.iterdir() if entry.name != ".staging"], [])

    def test_submission_requires_a_configured_target_and_a_change_summary(self) -> None:
        self.client.login()
        cases = {
            "missing_both": {},
            "missing_target": {"change_summary": TEST_SUMMARY},
            "whitespace_target": {"target_hardware": "   ", "change_summary": TEST_SUMMARY},
            "unknown_target": {"target_hardware": "ghost", "change_summary": TEST_SUMMARY},
            "missing_summary": {"target_hardware": TEST_TARGET},
            "whitespace_summary": {"target_hardware": TEST_TARGET, "change_summary": " \t "},
            "whitespace_lines_summary": {"target_hardware": TEST_TARGET, "change_summary": " \n\t "},
        }
        for label, choices in cases.items():
            with self.subTest(case=label):
                status, _, body = self.client.request_raw(
                    "POST",
                    "/api/runs",
                    _upload_body(fields=[(name, value) for name, value in choices.items()]),
                    "multipart/form-data; boundary=portal-boundary",
                )
                self.assertEqual(status, 400, body)
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual(self.airflow.dag_runs, {})

    def test_multiline_change_summary_normalizes_line_breaks_and_reaches_discovery(self) -> None:
        self.client.login()
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(
                fields=[
                    ("target_hardware", TEST_TARGET),
                    ("change_summary", "第一行\r\n第二行\r第三行\n第四行"),
                    ("purpose", "engineering"),
                ]
            ),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 201, body)
        stored = "第一行\n第二行\n第三行\n第四行"
        payload = json.loads(body)
        self.assertEqual(payload["change_summary"], stored)
        self.assertEqual(self.airflow.conf["change_summary"], stored)
        self.assertEqual(self.airflow.trigger_payloads[-1]["conf"]["change_summary"], stored)
        # End-to-end smoke: the recorded summary passes the discovery summary check without blocking.
        findings: list[dict] = []
        identity, _sources = discovery._identity(
            {"identity": {}, "properties": {"document": {}}},
            discovery.DiscoverySettings(),
            findings,
            configuration=None,
            assembly=None,
            native_files={},
            submission=discovery.SubmissionIdentity(
                hardware_id=TEST_TARGET,
                change_summary=stored,
                as_of="20261015",
                inventory_digest="a" * 64,
            ),
            handoff_sha256=None,
        )
        self.assertEqual(identity["change_summary"], stored)
        self.assertNotIn("discovery.summary_invalid", [finding["code"] for finding in findings])

    def test_submission_conf_always_carries_target_and_summary(self) -> None:
        self.client.login()
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 201, body)
        stored = self.upload_root / json.loads(body)["dag_run_id"] / "机器人工程"
        self.assertEqual(
            self.airflow.conf,
            {
                "handoff_path": str(stored),
                "main_assembly": "model.SLDASM",
                "purpose": "engineering",
                "request_id": f"portal-{json.loads(body)['dag_run_id']}",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
            },
        )
        # The recorded submitter is the run's stored actor, never a create-body claim.
        self.assertNotIn("submission_principal", self.airflow.conf)

    def test_unknown_target_hardware_is_rejected(self) -> None:
        self.client.login()
        files = [("model.SLDASM", b"<assembly/>")]
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(files=files, fields=[("target_hardware", "ghost")]),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 400, body)
        self.assertIn("Publication target is not configured", json.loads(body)["error"])

    def test_main_assembly_selection_is_validated_and_persisted(self) -> None:
        self.client.login()
        files = [
            ("model.SLDASM", b"<assembly/>"),
            ("parts/arm.SLDASM", b"<arm/>"),
            ("parts/p1.SLDPRT", b"<p1/>"),
        ]
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(files=files, fields=[("main_assembly", "parts/arm.SLDASM"), *UPLOAD_CHOICES]),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 201, body)
        payload = json.loads(body)
        dag_run_id = payload["dag_run_id"]
        self.assertEqual(payload["main_assembly"], "parts/arm.SLDASM")
        stored = self.upload_root / dag_run_id / "机器人工程"
        self.assertEqual(
            self.airflow.conf,
            {
                "handoff_path": str(stored),
                "main_assembly": "parts/arm.SLDASM",
                "purpose": "engineering",
                "request_id": f"portal-{dag_run_id}",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
            },
        )
        status, _, body = self.client.request("GET", f"/api/runs/{dag_run_id}")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["main_assembly"], "parts/arm.SLDASM")
        cases = (
            ("unknown", "nope.SLDASM"),
            ("traversal", "../arm.SLDASM"),
            ("not_assembly", "parts/p1.SLDPRT"),
            ("case_variant", "Parts/Arm.SLDASM"),
            ("empty", ""),
            ("padded", " parts/arm.SLDASM"),
        )
        for label, value in cases:
            with self.subTest(case=label):
                status, _, response = self.client.request_raw(
                    "POST",
                    "/api/runs",
                    _upload_body(files=files, fields=[("main_assembly", value), *UPLOAD_CHOICES]),
                    "multipart/form-data; boundary=portal-boundary",
                )
                self.assertEqual(status, 400, response)
        duplicated = _upload_body(
            files=files,
            fields=[("main_assembly", "model.SLDASM"), ("main_assembly", "model.SLDASM"), *UPLOAD_CHOICES],
        )
        status, _, response = self.client.request_raw(
            "POST", "/api/runs", duplicated, "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 400, response)
        self.assertEqual(len(self.airflow.trigger_payloads), 1)
        staging = self.upload_root / ".staging"
        self.assertTrue(not staging.exists() or not any(staging.iterdir()))

    def test_multiple_assemblies_require_a_choice_before_any_trigger(self) -> None:
        self.client.login()
        files = [("left/model.SLDASM", b"left"), ("right/model.SLDASM", b"right")]
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(files=files, fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(self.airflow.trigger_payloads, [])
        staging = self.upload_root / ".staging"
        self.assertTrue(not staging.exists() or not any(staging.iterdir()))

    def test_unicode_selection_matches_normalized_uploaded_path(self) -> None:
        self.client.login()
        chosen = "子装配/re\u0301vision.SLDASM"
        files = [("main.SLDASM", b"main"), (chosen, b"selected")]
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(files=files, fields=[("main_assembly", chosen), *UPLOAD_CHOICES]),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(json.loads(body)["main_assembly"], "子装配/révision.SLDASM")
        self.assertEqual(self.airflow.conf["main_assembly"], "子装配/révision.SLDASM")

    def test_manual_paths_and_unconfirmed_sessions_are_refused_before_writes(self) -> None:
        self.client.login()
        status, _, body = self.client.request("POST", "/api/runs", {"handoff_path": "/srv/robot-cell"})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.airflow.trigger_payloads, [])
        anonymous = PortalClient(self.client.base)
        status, _, _ = anonymous.request_raw(
            "POST", "/api/runs", _upload_body(), "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 401)
        self.client.login()
        self.airflow.revoked = True
        status, _, body = self.client.request_raw(
            "POST", "/api/runs", _upload_body(), "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 401, body)
        staging = self.upload_root / ".staging"
        self.assertTrue(not staging.exists() or not any(staging.iterdir()))
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_upload_rejections_leave_no_run_and_no_staging(self) -> None:
        self.client.login()
        cases = (
            ("duplicate", _upload_body(files=[("a.SLDASM", b"x"), ("a.SLDASM", b"x")])),
            ("case_alias", _upload_body(files=[("a.SLDASM", b"x"), ("A.sldasm", b"x")])),
            ("traversal", _upload_body(files=[("a/../x.SLDASM", b"x"), ("ok.SLDASM", b"x")])),
            ("transient", _upload_body(files=[("~$a.SLDASM", b"x"), ("ok.SLDASM", b"x")])),
            ("no_assembly", _upload_body(files=[("only.SLDPRT", b"x")])),
            ("empty", b"--portal-boundary--\r\n"),
        )
        for label, body in cases:
            with self.subTest(case=label):
                status, _, response = self.client.request_raw(
                    "POST", "/api/runs", body, "multipart/form-data; boundary=portal-boundary"
                )
                self.assertEqual(status, 400, response)
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual(list(self.upload_root.glob("portal-*")), [])
        staging = self.upload_root / ".staging"
        self.assertTrue(not staging.exists() or not any(staging.iterdir()))

    def test_ambiguous_trigger_outcomes_reconcile_on_the_recorded_run_id(self) -> None:
        self.client.login()
        # (a) The run was created but the response was lost: one GET finds it; no duplicate trigger.
        self.airflow.trigger_fail_after_create = True
        self.airflow.trigger_fail_times = 1
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 201, body)
        first = json.loads(body)["dag_run_id"]
        self.assertEqual(list(self.airflow.dag_runs), [first])
        self.assertEqual(len(self.airflow.trigger_payloads), 1)
        # (b) The run was not created: one reconcile + one retry with the same id.
        self.airflow.trigger_fail_after_create = False
        self.airflow.trigger_fail_times = 1
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 201, body)
        second = json.loads(body)["dag_run_id"]
        self.assertIn(second, self.airflow.dag_runs)
        self.assertEqual(len(self.airflow.dag_runs), 2)
        # (c) Never confirmed: 502 carries the run id, the upload stays, and no run exists.
        self.airflow.trigger_fail_times = 10
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 502, body)
        payload = json.loads(body)
        self.assertEqual(payload["trigger"], "absent")
        self.assertIn("do not submit again", payload["error"])
        unresolved = payload["dag_run_id"]
        self.assertNotIn(unresolved, self.airflow.dag_runs)
        self.assertTrue((self.upload_root / unresolved / "机器人工程" / "model.SLDASM").is_file())

    def test_upload_without_configured_intake_root_fails_closed(self) -> None:
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url),
            endpoint=lambda: self.endpoint,
            static_dir=self.static_dir,
            config_identity_provider=lambda: "c" * 64,
        )
        server = make_server(
            "127.0.0.1", 0, PortalApp(config), server_class=_ThreadingWSGIServer, handler_class=_QuietHandler
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def stop() -> None:
            server.shutdown()
            server.server_close()

        self.addCleanup(stop)
        client = PortalClient(f"http://127.0.0.1:{server.server_address[1]}")
        client.login()
        status, _, _ = client.request_raw(
            "POST", "/api/runs", _upload_body(), "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 503)
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_session_exposes_authoritative_upload_limits(self) -> None:
        self.client.login()
        payload = json.loads(self.client.login_response)
        self.assertEqual(
            payload["upload_limits"],
            {"max_files": MAX_FILES, "max_bytes": MAX_TOTAL_BYTES, "max_file_bytes": MAX_FILE_BYTES},
        )
        self.assertEqual(payload["upload_limits"]["max_files"], 4096)
        self.assertEqual(payload["upload_limits"]["max_bytes"], 2 * 1024**3)
        self.assertEqual(payload["upload_limits"]["max_file_bytes"], 512 * 1024**2)

    def test_run_identity_survives_a_portal_restart(self) -> None:
        """The operator page reads who triggered a run from Airflow, not from its own memory."""
        self._seed_airflow_run("portal-20261007T010000-abcdefff")
        self.client.login()
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        listed = json.loads(body)["runs"][0]
        self.assertEqual(listed["dag_run_id"], "portal-20261007T010000-abcdefff")
        self.assertEqual(listed["principal"], FEISHU_PRINCIPAL)
        self.assertEqual(listed["user"], FEISHU_NAME)

    def test_submitter_name_survives_restart_and_another_viewer(self) -> None:
        """The displayed submitter is the original initiator, never the current viewer."""
        self.client.login()
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 201, body)
        dag_run_id = json.loads(body)["dag_run_id"]
        # Restart the portal (new process, empty session store); a different operator signs in.
        restarted = self._start_extra_portal()
        viewer = PortalClient(f"http://127.0.0.1:{restarted.server_address[1]}")
        viewer.login(VIEWER_TOKEN)
        status, _, body = viewer.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        listed = next(run for run in json.loads(body)["runs"] if run["dag_run_id"] == dag_run_id)
        self.assertEqual(listed["user"], FEISHU_NAME)
        self.assertEqual(listed["principal"], FEISHU_PRINCIPAL)
        status, _, body = viewer.request("GET", f"/api/runs/{dag_run_id}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["operator"], FEISHU_NAME)
        self.assertEqual(payload["principal"], FEISHU_PRINCIPAL)
        self.assertNotEqual(payload["operator"], VIEWER_NAME)

    def test_browser_supplied_identity_fields_are_refused(self) -> None:
        self.client.login()
        for field in ("initiator", "user", "operator", "principal", "open_id", "name"):
            status, _, _ = self.client.request(
                "POST",
                "/api/runs",
                {"handoff_path": "/srv/robot-cell", field: {"principal": "cli_forged", "name": "冒充"}},
            )
            self.assertEqual(status, 400, field)
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_direct_airflow_trigger_cannot_forge_a_display_name(self) -> None:
        """Airflow's auth-owned actor envelope is the only name source; conf is ignored."""
        forged = "portal-20261007T040000-forged"
        self._seed_airflow_run(
            forged,
            conf={
                "handoff_path": "/srv/robot-cell",
                # A direct API triggerer echoing their own real principal with an arbitrary name.
                "initiator": {"principal": FEISHU_PRINCIPAL, "name": "冒名者"},
            },
        )
        self.client.login()
        status, _, body = self.client.request("GET", f"/api/runs/{forged}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["operator"], FEISHU_NAME)
        self.assertEqual(payload["principal"], FEISHU_PRINCIPAL)
        self.assertNotIn("冒名者", body.decode("utf-8"))
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        listed = next(run for run in json.loads(body)["runs"] if run["dag_run_id"] == forged)
        self.assertEqual(listed["user"], FEISHU_NAME)

    def test_unrecorded_or_damaged_names_never_guess(self) -> None:
        raw_damaged = f"{FEISHU_PRINCIPAL}|not-json"
        self._seed_airflow_run("portal-20261007T050000-legacy", triggering_user_name=FEISHU_PRINCIPAL)
        self._seed_airflow_run("portal-20261007T050001-damaged", triggering_user_name=raw_damaged)
        self._seed_airflow_run(
            "portal-20261007T050002-control",
            triggering_user_name=f"{FEISHU_PRINCIPAL}|{json.dumps('bad\nname')}",
        )
        self._seed_airflow_run(
            "portal-20261007T050003-bidi",
            triggering_user_name=f"{FEISHU_PRINCIPAL}|{json.dumps('evil\u202egniht')}",
        )
        self._seed_airflow_run("portal-20261007T050004-nouser", triggering_user_name=None)
        self._seed_airflow_run(
            "portal-20261007T050005-structural",
            triggering_user_name=f"cli:租户:ou|{json.dumps('名字', ensure_ascii=False)}",
        )
        self._seed_airflow_run(
            "portal-20261007T050006-twopart",
            triggering_user_name=f"cli:ou|{json.dumps('名字', ensure_ascii=False)}",
        )
        self._seed_airflow_run(
            "portal-20261007T050007-overlong",
            triggering_user_name=f"{FEISHU_PRINCIPAL}|{json.dumps('A' * 600)}",
        )
        self.client.login()
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        runs = {run["dag_run_id"]: run for run in json.loads(body)["runs"]}
        legacy = runs["portal-20261007T050000-legacy"]
        self.assertIsNone(legacy["user"])
        self.assertEqual(legacy["principal"], FEISHU_PRINCIPAL)
        damaged = runs["portal-20261007T050001-damaged"]
        self.assertIsNone(damaged["user"])
        self.assertEqual(damaged["principal"], FEISHU_PRINCIPAL)
        self.assertIsNone(runs["portal-20261007T050002-control"]["user"])
        self.assertEqual(runs["portal-20261007T050002-control"]["principal"], FEISHU_PRINCIPAL)
        self.assertIsNone(runs["portal-20261007T050003-bidi"]["user"])
        # Compound structure is validated without invented identifier charsets.
        structural = runs["portal-20261007T050005-structural"]
        self.assertEqual(structural["user"], "名字")
        self.assertEqual(structural["principal"], "cli:租户:ou")
        two_part = runs["portal-20261007T050006-twopart"]
        self.assertIsNone(two_part["user"])
        self.assertEqual(two_part["principal"], f"cli:ou|{json.dumps('名字', ensure_ascii=False)}")
        overlong = runs["portal-20261007T050007-overlong"]
        self.assertIsNone(overlong["user"])
        self.assertGreater(len(overlong["principal"]), TRIGGERING_USER_NAME_LIMIT)
        missing = runs["portal-20261007T050004-nouser"]
        self.assertIsNone(missing["user"])
        self.assertIsNone(missing["principal"])

    def test_session_adoption_enforces_the_recorded_name_budget(self) -> None:
        principal = "cli_app:tenant-a:ou_boundary"
        boundary = TRIGGERING_USER_NAME_LIMIT - len(principal) - 1 - 2
        for token, name, expected in (
            ("airflow-boundary-ok", "A" * boundary, 200),
            ("airflow-boundary-over", "A" * (boundary + 1), 502),
            ("airflow-boundary-type", 123, 502),
        ):
            with self.subTest(token=token):
                self.airflow.profiles[token] = _feishu_profile(name, principal, "ou_boundary")
                client = PortalClient(self.client.base)
                client.jar.set_cookie(_airflow_token_cookie(token))
                status, _, body = client.request("GET", "/api/session")
                self.assertEqual(status, expected, body)
                if expected == 200:
                    self.assertEqual(json.loads(body)["user"], "A" * boundary)
        for malformed in ("cli:tenant:ou:worker", "cli:tenant:ou worker", "not-a-principal"):
            with self.subTest(principal=malformed):
                token = "airflow-malformed-principal"
                profile = _feishu_profile("崔工", principal, "ou_boundary")
                profile["principal"] = malformed
                self.airflow.profiles[token] = profile
                client = PortalClient(self.client.base)
                client.jar.set_cookie(_airflow_token_cookie(token))
                status, _, body = client.request("GET", "/api/session")
                self.assertEqual(status, 502, body)

    def test_recorded_name_round_trips_unicode_and_delimiter(self) -> None:
        name = "崔|工🙂"
        self._seed_airflow_run(
            "portal-20261007T070000-unicode",
            triggering_user_name=_actor_envelope(name, FEISHU_PRINCIPAL),
        )
        self.client.login()
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        listed = next(run for run in json.loads(body)["runs"] if run["dag_run_id"] == "portal-20261007T070000-unicode")
        self.assertEqual(listed["user"], name)
        status, _, body = self.client.request("GET", "/api/runs/portal-20261007T070000-unicode")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["operator"], name)
        self.assertEqual(payload["principal"], FEISHU_PRINCIPAL)

    def test_session_without_a_verified_name_is_refused(self) -> None:
        """The auth manager only mints named sessions; anything else fails closed."""
        client = PortalClient(self.client.base)
        client.jar.set_cookie(_airflow_token_cookie(NO_NAME_TOKEN))
        status, _, body = client.request("GET", "/api/session")
        self.assertEqual(status, 502, body)
        self.assertNotIn("ou_noname", body.decode("utf-8"))

    def test_revoked_session_cannot_read_run_identity(self) -> None:
        self._seed_airflow_run("portal-20261007T060000-secret")
        self.client.login()
        self.airflow.revoked = True
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 401)
        self.assertNotIn(FEISHU_NAME.encode("utf-8"), body)
        status, _, body = self.client.request("GET", "/api/runs/portal-20261007T060000-secret")
        self.assertEqual(status, 401)
        self.assertNotIn(FEISHU_NAME.encode("utf-8"), body)
        fresh = PortalClient(self.client.base)
        status, _, body = fresh.request("GET", "/api/session")
        self.assertEqual(status, 401)
        self.assertNotIn(FEISHU_NAME.encode("utf-8"), body)

    def test_preview_and_artifacts_reauthorize_against_airflow(self) -> None:
        """A revoked session or a denied run must never be served from the preview cache."""
        self.client.login()
        self._seed_passed_job()
        preview = f"/api/runs/{DAG_RUN_ID}/preview"
        artifact = f"/api/runs/{DAG_RUN_ID}/artifacts/urdf/robot.urdf"
        self.assertEqual(self.client.request("GET", preview)[0], 200)
        self.assertEqual(self.client.request("GET", artifact)[0], 200)
        self.airflow.deny_runs = True
        self.assertEqual(self.client.request("GET", preview)[0], 401)
        self.assertEqual(self.client.request("GET", artifact)[0], 401)
        self.airflow.deny_runs = False
        self.airflow.revoked = True
        self.assertEqual(self.client.request("GET", preview)[0], 401)
        self.assertEqual(self.client.request("GET", artifact)[0], 401)
        status, _, _ = self.client.request("POST", "/api/runs", {"handoff_path": "/srv/robot-cell"})
        self.assertEqual(status, 400)
        self.client.csrf = None
        status, _, _ = self.client.request_raw(
            "POST", "/api/runs", _upload_body(), "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 403)

    def test_status_separates_automatic_results_from_pending_confirmations(self) -> None:
        self.client.login()
        self._seed_passed_job()
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["automatic"]["state"], "passed")
        self.assertEqual(payload["report"]["schema_version"], "solidworks-to-urdf.report/v1")
        self.assertEqual(payload["report"]["overall"]["state"], "passed")
        coverage = payload["coverage"]
        self.assertEqual(coverage["engineering"]["state"], "external_review")
        self.assertEqual(coverage["engineering"]["tracking"], "external")
        self.assertEqual(len(coverage["engineering"]["items"]), len(CONTRACT["confirmations"]))
        self.assertTrue(
            all("scope" in item and "automatic_exclusion" in item for item in coverage["engineering"]["items"])
        )
        self.assertEqual(coverage["automatic"]["state"], "passed")
        self.assertEqual(coverage["structure"]["hardware_id"], "m3.0")
        self.assertEqual(coverage["structure"]["revision"], "r1")
        self.assertTrue(any("Interference and clearance" in text for text in coverage["automatic"]["unsupported"]))
        self.assertEqual(payload["pr"]["url"], "https://github.com/example/m3.0/pull/1")
        self.assertEqual(
            [task["task_id"] for task in payload["tasks"]],
            ["freeze", "discover", "capture", "generate", "verify", "publish"],
        )
        self.assertEqual(payload["job"]["status"], "passed")

    def test_failed_job_reports_findings_and_no_success(self) -> None:
        self.client.login()
        self._seed_airflow_run(state="failed")
        self._seed_job()
        self._fail_job()
        run_id = native_run_id(DAG_RUN_ID)
        # A registered attempt without the urdf member stays unqualified: the preview refuses.
        self._register_attempt(run_id, {"meshes/only.stl": b"member\n"}, verified=True)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["automatic"]["state"], "failed")
        self.assertEqual(payload["report"]["overall"]["state"], "failed")
        self.assertEqual(payload["report"]["failure"]["stage"], "discover")
        blocked = next(stage for stage in payload["report"]["stages"] if stage["id"] == "capture")
        self.assertEqual(blocked["state_label"], "Blocked by upstream failure")
        self.assertTrue(any(check["id"] == "discovery.axis_unresolved" for check in payload["automatic"]["checks"]))
        self.assertEqual(payload["coverage"]["structure"]["hardware_id"], "m3.0")
        self.assertFalse(payload["discovery"]["passed"])
        self.assertIsNone(payload["pr"])
        self.assertTrue(any("cad capture failed" in finding["message"] for finding in payload["findings"]))
        self.assertTrue(any(finding["object"] == "mate:elbow-1" for finding in payload["findings"]))
        self.assertTrue(
            any(
                finding.get("evidence", {}).get("detail", {}).get("route") == "native"
                and finding["evidence"]["discovery_sha256"] == "d" * 64
                for finding in payload["findings"]
            )
        )
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 409)

    def test_coverage_external_scope_ignores_job_confirmation_claims(self) -> None:
        self.client.login()
        self._seed_passed_job()
        run_id = native_run_id(DAG_RUN_ID)
        self.endpoint_server.jobs[run_id]["confirmations"] = [{"id": "范围与身份", "state": "confirmed"}]
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        coverage = json.loads(body)["coverage"]
        self.assertEqual(coverage["engineering"]["state"], "external_review")
        self.assertEqual(coverage["engineering"]["tracking"], "external")
        self.assertTrue(all("state" not in item for item in coverage["engineering"]["items"]))
        self.assertFalse(any("confirmed" in str(item.get("id", "")) for item in coverage["engineering"]["items"]))

    def test_preview_and_artifacts_are_digest_bound(self) -> None:
        self.client.login()
        self._seed_passed_job()
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 200, body)
        preview = json.loads(body)
        self.assertEqual(preview["urdf"], "urdf/robot.urdf")
        status, headers, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/urdf/robot.urdf")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<robot name='fixture'/>")
        self.assertEqual(headers.get("Content-Type"), "application/xml")
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/meshes/missing.stl")
        self.assertEqual(status, 404)
        self.store.runs[RUN_ID]["artifacts"]["urdf/robot.urdf"] = b"tampered"
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/urdf/robot.urdf")
        self.assertEqual(status, 502)
        self.assertNotIn(b"tampered", body)
        # Previews and artifacts are store-owned; the endpoint is never consulted for them.
        self.assertFalse(any("preview" in path for path in self.endpoint_server.requests))

    def test_failed_linux_run_serves_the_retained_structural_report(self) -> None:
        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        document = "# 结构团队报告\n\n阻塞项与警告见结构化证据。\n".encode()
        self._register_attempt(run_id, {"reports/structural-team.md": document}, verified=False)

        status, headers, body = self.client.request(
            "GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/structural-team.md"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body, document)
        self.assertEqual(headers.get("Content-Type"), "text/markdown; charset=utf-8")
        # The run page itself links the retained report; no URL hunting required.
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        links = json.loads(body)["structural_report"]
        self.assertEqual([entry["path"] for entry in links], ["reports/structural-team.md"])
        self.assertEqual(links[0]["url"], f"/api/runs/{DAG_RUN_ID}/artifacts/reports/structural-team.md")
        self.assertTrue(links[0]["label"])

    def test_failed_native_run_serves_the_fetched_report_and_refuses_absent(self) -> None:
        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        markdown = b"native capture blocked\n"
        self._register_attempt(run_id, {"reports/structural-team.md": markdown}, verified=False)

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/structural-team.md")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, markdown)

        # A member the run never retained is a clear 404, never a substitute report.
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/structural-team.json")
        self.assertEqual(status, 404)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        links = json.loads(body)["structural_report"]
        self.assertEqual([entry["path"] for entry in links], ["reports/structural-team.md"])

    def test_verified_but_unpublished_run_serves_the_report_from_the_bound_delivery(self) -> None:
        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        # Verify green, publish unroutable: the registered verify checkpoint still
        # carries the report as a bound member.
        document = b"verify checkpoint report\n"
        self._register_attempt(
            run_id,
            {"urdf/robot.urdf": b"<robot name='fixture'/>", "reports/structural-team.md": document},
            verified=True,
        )

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/structural-team.md")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, document)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        links = json.loads(body)["structural_report"]
        self.assertEqual([entry["path"] for entry in links], ["reports/structural-team.md"])
        # Authorization is unchanged: without a session the route refuses outright.
        anonymous = PortalClient(self.client.base)
        status, _, _ = anonymous.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/structural-team.md")
        self.assertEqual(status, 401)

    def test_failed_run_serves_the_bound_diagnostic_quality_document(self) -> None:
        """The unverified preview's report member is served under the same digest rule."""

        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        urdf = b"<robot name='fixture'/>"
        document = json.dumps({"passed": False, "subject_sha256": "d" * 64, "checks": []}).encode()
        digest = hashlib.sha256(document).hexdigest()
        self.store.add(
            run_id,
            meta={"handoff_sha256": "d" * 64},
            delivery=True,
            preview={
                "schema_version": "solidworks-to-urdf.preview/v1",
                "pipeline_id": PIPELINE_ID,
                "run_id": run_id,
                "subject_sha256": "d" * 64,
                "urdf": "urdf/robot.urdf",
                "files": {"urdf/robot.urdf": hashlib.sha256(urdf).hexdigest()},
                "report_files": {"reports/quality.json": digest},
                "verified": False,
                "state": "unverified",
                "reason": "verification_failed",
            },
            artifacts={"urdf/robot.urdf": urdf, "reports/quality.json": document},
        )
        status, headers, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/quality.json")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, document)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        # The digest rule is unchanged: tampered bytes surface as a stream failure.
        self.store.runs[run_id]["artifacts"]["reports/quality.json"] = b"tampered"
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/quality.json")
        self.assertEqual(status, 502)
        self.assertNotIn(b"tampered", body)
        # A member outside the bound member maps stays a clear 404.
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/other.json")
        self.assertEqual(status, 404)

    def test_verified_preview_does_not_expose_report_members(self) -> None:
        self.client.login()
        self._seed_passed_job()
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/quality.json")
        self.assertEqual(status, 404)

    def test_registered_member_defects_refuse_with_502(self) -> None:
        """Registration defects and byte edits never self-sign a serving decision."""

        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        document = json.dumps({"passed": False, "subject_sha256": "d" * 64, "checks": []}).encode()
        self._register_attempt(
            run_id,
            {"urdf/robot.urdf": b"<robot name='fixture'/>", "reports/quality.json": document},
            verified=False,
        )
        route = f"/api/runs/{DAG_RUN_ID}/artifacts/reports/quality.json"
        # Bytes edited before the first read never re-sign the registered digest.
        self.store.runs[run_id]["artifacts"]["reports/quality.json"] = b"edited before first read\n"
        status, _, body = self.client.request("GET", route)
        self.assertEqual(status, 502)
        self.assertNotIn(b"edited", body)
        # A malformed registration (no digest) refuses with the incomplete-registration 502.
        self.store.runs[run_id]["artifacts"]["reports/quality.json"] = document
        preview = self.store.runs[run_id]["preview"]
        preview["report_files"]["reports/quality.json"] = None
        status, _, body = self.client.request("GET", route)
        self.assertEqual(status, 502)
        self.assertNotIn(document, body)
        # A member with no registration at all stays a clear 404.
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/other.json")
        self.assertEqual(status, 404)

    def test_edited_evidence_refuses_the_preview_like_the_download(self) -> None:
        """The diagnostic preview parses the registered bytes: an edit before the first
        read refuses with the same 502 class as the download, never empty findings."""

        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        quality = json.dumps({"passed": False, "checks": [{"id": "fixture.required", "state": "failed"}]}).encode()
        self._register_attempt(
            run_id,
            {"urdf/robot.urdf": b"<robot name='fixture'/>", "reports/quality.json": quality},
            verified=False,
        )
        # Replaced before the first read: the preview refuses exactly like the download.
        self.store.runs[run_id]["artifacts"]["reports/quality.json"] = b'{"checks":[]}'
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 502, body)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/quality.json")
        self.assertEqual(status, 502, body)

    def test_incomplete_evidence_registration_refuses_the_preview(self) -> None:
        """A member whose digest is missing is an incomplete registration: the preview
        refuses before any parse instead of reporting zero failing checks."""

        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        quality = json.dumps({"passed": False, "checks": [{"id": "fixture.required", "state": "failed"}]}).encode()
        self._register_attempt(
            run_id,
            {"urdf/robot.urdf": b"<robot name='fixture'/>", "reports/quality.json": quality},
            verified=False,
        )
        self.store.runs[run_id]["preview"]["report_files"]["reports/quality.json"] = None
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 502, body)

    def test_registered_report_parse_defects_refuse_the_preview(self) -> None:
        """A registered report that cannot be consumed as an object refuses (502) instead
        of silently reporting zero failing checks; its bytes still download unchanged."""

        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        quality = json.dumps({"passed": False, "checks": [{"id": "fixture.required", "state": "failed"}]}).encode()
        self._register_attempt(
            run_id,
            {"urdf/robot.urdf": b"<robot name='fixture'/>", "reports/quality.json": quality},
            verified=False,
        )
        route = f"/api/runs/{DAG_RUN_ID}/artifacts/reports/quality.json"
        for payload in (b"{", b"[]", b'{"passed":false,"checks":"invalid"}'):
            with self.subTest(payload=payload):
                self._rebind_attempt_member(run_id, "reports/quality.json", payload)
                status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
                self.assertEqual(status, 502, body)
                # The changed bytes are correctly registered: the refusal is the parse side.
                status, _, body = self.client.request("GET", route)
                self.assertEqual(status, 200)
                self.assertEqual(body, payload)

    def test_registered_warning_source_parse_defect_refuses_the_preview(self) -> None:
        """A registered warning source that cannot be parsed refuses (502), never a
        silently empty warning list."""

        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        quality = json.dumps({"passed": False, "checks": [{"id": "fixture.required", "state": "failed"}]}).encode()
        handoff = json.dumps({"warnings": [{"code": "fixture_warning", "message": "kept"}]}).encode()
        self._register_attempt(
            run_id,
            {
                "urdf/robot.urdf": b"<robot name='fixture'/>",
                "reports/quality.json": quality,
                "input/handoff.json": handoff,
            },
            verified=False,
        )
        self._rebind_attempt_member(run_id, "input/handoff.json", b"{")
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 502, body)
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/input/handoff.json")
        self.assertEqual(status, 200)

    def test_undeclared_optional_warning_source_is_not_an_error(self) -> None:
        """A warning source the registration does not carry stays absent: no error, and
        the registered report still serves its failing-check count."""

        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        quality = json.dumps({"passed": False, "checks": [{"id": "fixture.required", "state": "failed"}]}).encode()
        self._register_attempt(
            run_id,
            {"urdf/robot.urdf": b"<robot name='fixture'/>", "reports/quality.json": quality},
            verified=False,
        )
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["failing_checks_total"], 1)
        self.assertEqual(payload["warnings_total"], 0)

    def test_registered_run_state_and_events_outrank_the_native_job(self) -> None:
        """The DB registration owns the run state and the recorded events; the endpoint
        only supplements display detail and never overrides them."""

        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID, status="running", events=[{"stage": "capture", "state": "running", "at": "t0"}])
        run_id = native_run_id(DAG_RUN_ID)
        recorded = protocol_events(failed_stage="verify", subject="a" * 64)
        self.store.add(
            run_id,
            meta={"handoff_sha256": "d" * 64},
            merged={"status": "failed", "events": recorded},
        )
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        # The registered run state outranks the native job's "running".
        self.assertEqual(payload["job"]["status"], "failed")
        # The recorded events - not the native-only capture event - drive the stage view.
        self.assertEqual(payload["report"]["failure"]["stage"], "verify")

    def test_registered_diagnostics_are_the_only_servable_copy(self) -> None:
        self.client.login()
        self._seed_airflow_run(DAG_RUN_ID, state="failed")
        self._seed_job(DAG_RUN_ID)
        self._fail_job(DAG_RUN_ID)
        run_id = native_run_id(DAG_RUN_ID)
        newest = b"newest failed publish diagnostics\n"
        self._register_attempt(run_id, {"reports/structural-team.md": newest}, verified=False)
        # An older copy on disk is never a substitute: without a registration it
        # establishes no member identity.
        older = self.store.run_dir(run_id) / "output.failed/reports/structural-team.md"
        older.parent.mkdir(parents=True, exist_ok=True)
        older.write_bytes(b"older delivered copy\n")

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/structural-team.md")
        self.assertEqual(status, 200, body)
        self.assertEqual(body, newest)
        self.assertNotEqual(body, older.read_bytes())

    def test_stage_view_exposes_contract_evidence_and_only_verified_assets_are_linkable(self) -> None:
        from description_pipeline.stages import CONTRACT, STAGE_IDS

        self.client.login()
        self._seed_passed_job()
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        view = json.loads(body)["stage_view"]
        self.assertEqual([stage["id"] for stage in view["stages"]], list(STAGE_IDS))
        definitions = {stage["id"]: stage for stage in CONTRACT["stages"]}
        for stage in view["stages"]:
            self.assertTrue(stage["evidence"], stage["id"])
            self.assertEqual(
                [item["id"] for item in stage["input_qc"]],
                [item["id"] for item in definitions[stage["id"]]["input_qc"]],
            )
            self.assertEqual(
                [item["id"] for item in stage["output_qc"]],
                [item["id"] for item in definitions[stage["id"]]["output_qc"]],
            )
            expected_unsupported = [item for item in CONTRACT["unsupported"] if item["stage"] == stage["id"]]
            self.assertEqual(
                [item["label"] for item in stage["unsupported"]], [item["label"] for item in expected_unsupported]
            )
            self.assertTrue(all(item["state"] == "unsupported" for item in stage["unsupported"]))
        unsupported_stages = {stage["id"] for stage in view["stages"] if stage["unsupported"]}
        self.assertEqual(unsupported_stages, {item["stage"] for item in CONTRACT["unsupported"]})
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 200, body)
        preview = json.loads(body)
        self.assertTrue(preview["files"])
        for name in preview["files"]:
            self.assertTrue(name.startswith(("urdf/", "meshes/")), name)

    def test_preview_waits_for_verification(self) -> None:
        self.client.login()
        self._seed_airflow_run()
        self._seed_job(
            status="native_complete",
            result={
                "native_complete": True,
                "capture_archive": {
                    "name": "native-evidence.zip",
                    "sha256": "a" * 64,
                    "size": 1024,
                    "manifest_sha256": "b" * 64,
                },
            },
        )
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 404)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        automatic = json.loads(body)["automatic"]
        # native_complete ends the native half only: no verified store delivery exists yet.
        self.assertEqual(automatic["state"], "running")
        self.assertEqual(automatic["job_state"], "native_complete")

    def test_verified_model_without_pull_request_is_still_previewable(self) -> None:
        self.client.login()
        self._seed_airflow_run(state="failed")
        result = self._passed_result()
        result["submission"] = {"passed": False, "subject_sha256": "a" * 64}
        self._seed_job(status="failed", result=result, error="Incomplete or mismatched publication receipt")
        self.store.add(
            native_run_id(DAG_RUN_ID),
            meta={"handoff_sha256": "d" * 64},
            delivery=True,
            preview=_preview_payload(),
            artifacts=_endpoint_artifacts(),
        )
        self._record_fixture_events(native_run_id(DAG_RUN_ID))
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["automatic"]["state"], "passed")
        self.assertIsNone(payload["pr"])
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 200)

    def test_publication_failure_preserves_the_verified_preview(self) -> None:
        # Acceptance 8: a PR-service failure keeps the verified preview; the automatic summary
        # reflects the model qualification, not the overall job or publication state.
        self.client.login()
        self._seed_airflow_run(state="failed")
        subject = "a" * 64
        self._seed_job(
            status="failed",
            error="Incomplete or mismatched publication receipt",
            result={
                "passed": False,
                "pipeline_id": PIPELINE_ID,
                "output": "build/out",
                "stage": "publish",
                "state": "failed",
                "subject_sha256": subject,
                "quality": {
                    "passed": True,
                    "subject_sha256": subject,
                    "checks": [{"id": "physics.expected_mass", "state": "passed"}],
                },
                "submission": {
                    "passed": False,
                    "subject_sha256": subject,
                    "base": "feature/m3.0",
                    "branch": "work/solidworks/m3.0/engineering",
                    "state": "failed",
                    "error": "git_failed",
                    "commit": "",
                    "repository_slug": "example/m3.0",
                },
            },
            events=protocol_events(subject=subject, failed_stage="publish"),
        )
        self.store.add(
            native_run_id(DAG_RUN_ID),
            meta={"handoff_sha256": "d" * 64},
            delivery=True,
            preview=_preview_payload(),
            artifacts=_endpoint_artifacts(),
        )
        self._record_fixture_events(native_run_id(DAG_RUN_ID))
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["automatic"]["state"], "passed")
        self.assertEqual(payload["job"]["status"], "failed")
        self.assertIsNone(payload["pr"])
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["urdf"], "urdf/robot.urdf")

    def test_publication_failure_with_mismatched_quality_is_still_blocked(self) -> None:
        self.client.login()
        self._seed_airflow_run(state="failed")
        subject = "a" * 64
        self._seed_job(
            status="failed",
            error="Incomplete or mismatched publication receipt",
            result={
                "passed": False,
                "pipeline_id": PIPELINE_ID,
                "output": "build/out",
                "stage": "publish",
                "state": "failed",
                "subject_sha256": subject,
                "quality": {
                    "passed": True,
                    "subject_sha256": "b" * 64,
                    "checks": [{"id": "physics.expected_mass", "state": "passed"}],
                },
                "submission": {
                    "passed": False,
                    "subject_sha256": subject,
                    "base": "feature/m3.0",
                    "branch": "work/solidworks/m3.0/engineering",
                    "state": "failed",
                    "error": "git_failed",
                    "commit": "",
                    "repository_slug": "example/m3.0",
                },
            },
            events=protocol_events(subject=subject, failed_stage="publish"),
        )
        # Registered members exist but carry no qualified robot member: the preview refuses.
        self._register_attempt(native_run_id(DAG_RUN_ID), {"meshes/only.stl": b"member\n"}, verified=True)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["automatic"]["state"], "failed")
        self.assertIn("subject_sha256 differs", payload["automatic"]["message"])
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 409)

    def test_static_assets_are_local_and_traversal_is_refused(self) -> None:
        client = PortalClient(self.client.base)
        status, _, body = client.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/static/app.js", body)
        self.assertNotIn(b"http://", body)
        self.assertNotIn(b"https://", body)
        status, headers, body = client.request("GET", "/static/vendor/three.module.min.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertGreater(len(body), 100000)
        status, _, _ = client.request("GET", "/static/%2e%2e/portal.py")
        self.assertEqual(status, 404)
        viewer = (self.static_dir / "viewer.js").read_text(encoding="utf-8")
        self.assertIn('from "/static/vendor/three.module.min.js"', viewer)
        self.assertIn('querySelector("limit")', viewer)
        self.assertIn("<!doctype", viewer.lower())
        for name in ("index.html", "app.js", "viewer.js", "style.css"):
            source = (self.static_dir / name).read_text(encoding="utf-8")
            self.assertNotIn("http://", source)
            self.assertNotIn("https://", source)

    def test_browser_module_dependencies_are_served_locally(self) -> None:
        """Follow the module graph, including vendored imports needed to boot the page."""
        pending = ["/static/app.js"]
        visited = set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            status, headers, body = self.client.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("javascript", headers.get("Content-Type", ""), path)
            source = body.decode("utf-8")
            for dependency in re.findall(r"\bfrom\s*[\"']([^\"']+)[\"']", source):
                self.assertTrue(dependency.startswith(("/static/", "./", "../")), dependency)
                target = posixpath.normpath(posixpath.join(posixpath.dirname(path), dependency))
                self.assertTrue(target.startswith("/static/"), target)
                pending.append(target)

    def test_config_file_drives_the_portal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "endpoint.token"
            token_path.write_text("test-token", encoding="utf-8")
            config_path = Path(tmp) / "portal.json"
            config_path.write_text(
                json.dumps(
                    {
                        "airflow": {"url": self.airflow.url},
                        "endpoint": {"url": self.endpoint_server.url, "token_file": str(token_path)},
                        "portal": {"host": "127.0.0.1", "port": 18780},
                    }
                ),
                encoding="utf-8",
            )
            config = load_portal_config(config_path)
            self.assertEqual(config.dag_id, "solidworks_to_urdf")
            self.assertEqual(config.host, "127.0.0.1")
            self.assertEqual(config.port, 18780)
            self.assertEqual(config.airflow.base_url, self.airflow.url)
            self.assertEqual(config.endpoint.config.base_url, self.endpoint_server.url)
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({"portal": {"unknown_key": 1}}), encoding="utf-8")
            with self.assertRaises(AirflowApiError):
                load_portal_config(bad)
            missing = Path(tmp) / "missing.json"
            missing.write_text(
                json.dumps({"airflow": {"url": self.airflow.url}, "endpoint": {"url": self.endpoint_server.url}}),
                encoding="utf-8",
            )
            with self.assertRaises(AirflowApiError):
                load_portal_config(missing)

    def test_airflow_redirect_never_forwards_credentials(self) -> None:
        target = MockAirflow()
        target.__enter__()
        self.addCleanup(target.__exit__, None, None, None)
        with RedirectAirflow(target.url) as redirector:
            with self.assertRaises(AirflowApiError):
                AirflowApi(redirector.url).profile("airflow-session-token")
            self.assertEqual(target.hits, 0)

    def test_internal_airflow_calls_ignore_ambient_proxies(self) -> None:
        # The child imports the Airflow opener under a hostile proxy environment; only a
        # direct connection passes and no request may reach the fake proxy.
        root = Path(__file__).resolve().parents[2]
        environment = {**os.environ, "PYTHONPATH": str(root / "src")}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "proxy_probe.py"), "portal"],
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
            cwd=str(root),
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")

    def test_no_password_path_survives(self) -> None:
        self.assertFalse(hasattr(AirflowApi, "login"))
        page = (self.static_dir / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("password", page.lower())
        self.assertIn("/auth/feishu/login", page)
        status, _, _ = self.client.request("POST", "/api/session", {"username": "operator", "password": "x"})
        self.assertEqual(status, 405)

    def test_logout_clears_the_portal_and_airflow_cookies(self) -> None:
        self.client.login()
        status, _headers, body = self.client.request("DELETE", "/api/session")
        self.assertEqual(status, 200, body)
        cookies = self.client.last_set_cookies
        self.assertTrue(any(cookie.startswith("solidworks_portal_session=") for cookie in cookies), cookies)
        self.assertTrue(any(cookie.startswith("_token=") and "Max-Age=0" in cookie for cookie in cookies), cookies)
        self.assertEqual(self.client.request("GET", "/api/session")[0], 401)

    def test_entry_point_boots_from_config(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "endpoint.token"
            token_path.write_text("test-token", encoding="utf-8")
            config_path = Path(tmp) / "portal.json"
            config_path.write_text(
                json.dumps(
                    {
                        "airflow": {"url": self.airflow.url},
                        "endpoint": {"url": self.endpoint_server.url, "token_file": str(token_path)},
                        "portal": {"host": "127.0.0.1", "port": port},
                    }
                ),
                encoding="utf-8",
            )
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
            process = subprocess.Popen(
                [sys.executable, "-m", "description_pipeline.orchestration.portal", "--config", str(config_path)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.time() + 20
                while True:
                    try:
                        with urlrequest.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                            self.assertEqual(response.status, 200)
                            self.assertIn(b"/static/app.js", response.read())
                        break
                    except (urlerror.URLError, ConnectionError, TimeoutError) as error:
                        if time.time() >= deadline:
                            output = process.stdout.read().decode("utf-8", "replace") if process.stdout else ""
                            self.fail(f"portal did not start: {error}; output={output[-2000:]}")
                        time.sleep(0.2)
                with self.assertRaises(urlerror.HTTPError) as raised:
                    urlrequest.urlopen(f"http://127.0.0.1:{port}/api/session", timeout=2)
                self.assertEqual(raised.exception.code, 401)
                raised.exception.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                if process.stdout:
                    process.stdout.close()

    # ------------------------------------------------------- linked reruns
    @staticmethod
    def _rerun_rows(overrides: dict[str, tuple[bool, str, str | None]] | None = None) -> list[dict]:
        names = {
            "freeze": "Freeze inputs",
            "discover": "Discover structure",
            "capture": "Capture evidence",
            "generate": "Generate URDF",
            "verify": "Independent verification",
            "publish": "Publish review PR",
        }
        chosen = overrides or {}
        rows = []
        for stage, name in names.items():
            eligible, reason, earliest = chosen.get(stage, (True, "ok", None))
            rows.append(
                {
                    "stage": stage,
                    "name": name,
                    "eligible": eligible,
                    "reason": reason,
                    "message": "" if eligible else "Upstream checkpoint cannot be reused; restart from an earlier step",
                    "recomputes": [stage],
                    "retains": [],
                    "prerequisites": {
                        "inputs": "ok",
                        "tool": "ok",
                        "dependency": "ok",
                        "target": "ok",
                        "receipt": "ok",
                        "earliest_required": earliest,
                    },
                }
            )
        return rows

    def test_rerun_attempt_requires_owner_and_csrf_and_valid_stage(self) -> None:
        self._seed_passed_job()
        viewer = PortalClient(self.client.base)
        viewer.login(VIEWER_TOKEN)
        status, _, _ = viewer.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 403)
        self.client.login()
        self.client.csrf = None
        status, _, _ = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 403)
        self.client.login()
        for body in (
            {"stage": "bogus"},
            {},
            {"stage": "generate", "handoff_path": "/elsewhere"},
            {"stage": "generate", "submission_principal": "cli_forged:tenant-a:ou_attacker"},
        ):
            with self.subTest(body=body):
                status, _, _ = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", body)
                self.assertEqual(status, 400)

    def test_rerun_attempt_creates_linked_child_from_authoritative_parent(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows()
        self.airflow.dag_runs[DAG_RUN_ID]["conf"]["main_assembly"] = "parts/arm.SLDASM"
        parent_conf = dict(self.airflow.dag_runs[DAG_RUN_ID]["conf"])
        jobs_before = set(self.endpoint_server.jobs)
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        child = payload["dag_run_id"]
        self.assertNotEqual(child, DAG_RUN_ID)
        self.assertEqual(payload["parent_dag_run_id"], DAG_RUN_ID)
        self.assertEqual(payload["resume_from"], "generate")
        self.assertEqual(payload["resume_from_name"], "Generate URDF")
        self.assertEqual(payload["state"], "queued")
        self.assertEqual(self.airflow.trigger_payloads[-1]["dag_run_id"], child)
        # The admission is the durable reservation: canonical native id, the parent
        # linkage, the resume stage and the strict profile principal, all before the
        # Airflow binding; nothing lands in a state file.
        self.assertEqual(len(self.platform.admits), 1)
        admit = self.platform.admits[0]
        self.assertEqual(admit["request_id"], f"portal-{child}")
        self.assertEqual(admit["run_id"], native_run_id(child))
        self.assertEqual(admit["parent_run_id"], native_run_id(DAG_RUN_ID))
        self.assertEqual(admit["from_stage"], "generate")
        self.assertEqual(admit["principal"], FEISHU_PRINCIPAL)
        self.assertEqual(admit["dispatch"]["dag_run_id"], child)
        self.assertEqual(admit["dispatch"]["operation_key"], f"portal-{child}/generate:0")
        self.assertFalse((self.upload_root / ".rerun-attempts").exists())
        self.assertEqual(
            self.airflow.trigger_payloads[-1]["conf"],
            {
                "handoff_path": "/srv/robot-cell",
                "parent_dag_run_id": DAG_RUN_ID,
                "resume_from": "generate",
                "purpose": "engineering",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
                "main_assembly": "parts/arm.SLDASM",
            },
        )
        # The original run, its conf and the native job stay immutable.
        self.assertEqual(self.airflow.dag_runs[DAG_RUN_ID]["conf"], parent_conf)
        self.assertEqual(self.airflow.dag_runs[DAG_RUN_ID]["state"], "failed")
        self.assertEqual(set(self.endpoint_server.jobs), jobs_before)

    def test_rerun_attempt_never_copies_the_parent_submitter_identity(self) -> None:
        self._seed_retryable_run()
        # Even a parent conf that carries a submitter claim is never copied: the child's
        # initiator is the authenticated actor of THIS request, and the original submitter
        # stays lineage via the parent linkage - an administrator re-running someone else's
        # parent can never impersonate the original user.
        self.airflow.dag_runs[DAG_RUN_ID]["conf"]["submission_principal"] = VIEWER_PRINCIPAL
        self.client.login()
        rows = self._rerun_rows()
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 202, body)
        self.assertEqual(
            self.airflow.trigger_payloads[-1]["conf"],
            {
                "handoff_path": "/srv/robot-cell",
                "parent_dag_run_id": DAG_RUN_ID,
                "resume_from": "generate",
                "purpose": "engineering",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
            },
        )

    def test_rerun_attempt_rebinds_the_publish_target_and_summary(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows()
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request(
                "POST",
                f"/api/runs/{DAG_RUN_ID}/attempts",
                {"stage": "generate", "target_hardware": "m3.1", "change_summary": "rebound\r\nsummary"},
            )
        self.assertEqual(status, 202, body)
        self.assertEqual(
            self.airflow.trigger_payloads[-1]["conf"],
            {
                "handoff_path": "/srv/robot-cell",
                "parent_dag_run_id": DAG_RUN_ID,
                "resume_from": "generate",
                "purpose": "engineering",
                "target_hardware": "m3.1",
                "change_summary": "rebound\nsummary",
            },
        )

    def test_rerun_attempt_requires_a_configured_target_and_summary(self) -> None:
        self._seed_retryable_run()
        self.airflow.dag_runs[DAG_RUN_ID]["conf"] = {"handoff_path": "/srv/robot-cell"}
        self.client.login()
        rows = self._rerun_rows()
        cases = (
            {"stage": "generate"},
            {"stage": "generate", "target_hardware": "ghost", "change_summary": "control fixture"},
            {"stage": "generate", "target_hardware": TEST_TARGET},
            {"stage": "generate", "target_hardware": TEST_TARGET, "change_summary": "   "},
        )
        for case in cases:
            with self.subTest(case=sorted(case)):
                before = len(self.airflow.trigger_payloads)
                with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
                    status, _, _ = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", case)
                self.assertEqual(status, 400)
                self.assertEqual(len(self.airflow.trigger_payloads), before)

    def test_rerun_attempt_rejects_a_crlf_only_parent_summary(self) -> None:
        self._seed_retryable_run()
        self.airflow.dag_runs[DAG_RUN_ID]["conf"]["change_summary"] = "\r\n"
        self.client.login()
        rows = self._rerun_rows()
        before = len(self.airflow.trigger_payloads)
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 400, body)
        self.assertEqual(len(self.airflow.trigger_payloads), before)

    def test_rerun_attempt_same_stage_is_idempotent_and_other_stage_conflicts(self) -> None:
        self._seed_retryable_run()
        child = "portal-20261009T000000-deadbeef"
        self.platform.seed_open_child(native_run_id(DAG_RUN_ID), child, from_stage="generate")
        self._seed_airflow_run(
            child,
            state="queued",
            conf={
                "handoff_path": "/srv/robot-cell",
                "parent_dag_run_id": DAG_RUN_ID,
                "resume_from": "generate",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
                "purpose": "engineering",
            },
        )
        self.client.login()
        rows = self._rerun_rows()
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            before = len(self.airflow.trigger_payloads)
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
            self.assertEqual(status, 202, body)
            self.assertEqual(json.loads(body)["dag_run_id"], child)
            self.assertEqual(len(self.airflow.trigger_payloads), before)
            self.assertEqual(self.platform.admits, [])
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "capture"})
            self.assertEqual(status, 409, body)
            payload = json.loads(body)
            self.assertEqual(payload["reason"], "already_active")
            self.assertEqual(payload["active_dag_run_id"], child)
            self.assertTrue(payload["message"])
            # A changed frame (another target) may never ride the existing attempt.
            status, _, body = self.client.request(
                "POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate", "target_hardware": "m3.1"}
            )
            self.assertEqual(status, 409, body)
            payload = json.loads(body)
            self.assertEqual(payload["reason"], "attempt_mismatch", body)
            self.assertEqual(payload["active_dag_run_id"], child)
            self.assertEqual(len(self.airflow.trigger_payloads), before)
            self.assertEqual(self.platform.admits, [])

    def test_rerun_attempt_refuses_ineligible_stage_with_earliest_required(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows({"capture": (False, "dependency_changed", "discover")})
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "capture"})
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertEqual(payload["reason"], "dependency_changed")
        self.assertEqual(payload["earliest_required"], "discover")
        self.assertTrue(payload["message"])
        # A portable stage whose Linux checkpoint is missing refuses with the store's own reason.
        self.store.runs[RUN_ID]["checkpoints"] = ("capture",)
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": self._rerun_rows()}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "verify"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "linux_checkpoint_missing")
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_rerun_attempt_without_planner_refuses_without_creating(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        # Without an admitted Linux attempt the Windows plan is the only restart evidence;
        # its absence refuses instead of creating a linked run.
        self.store.runs.pop(RUN_ID)
        with patch.object(self.endpoint, "rerun_plan", create=True, side_effect=EndpointError("planner offline")):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "endpoint_evidence_unavailable")
        self.assertEqual(self.airflow.trigger_payloads, [])
        # With the admitted Linux attempt the store owns the restart surface: the linked
        # portable rerun is still created while the Windows plan stays offline.
        self._seed_retryable_run()
        with patch.object(self.endpoint, "rerun_plan", create=True, side_effect=EndpointError("planner offline")):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["resume_from"], "generate")

    def test_rerun_attempt_reconciles_ambiguous_trigger(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows()
        self.airflow.trigger_fail_after_create = True
        self.airflow.trigger_fail_times = 1
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "capture"})
        self.assertEqual(status, 202, body)
        self.assertIn(json.loads(body)["dag_run_id"], self.airflow.dag_runs)

    def test_rerun_attempt_reports_unconfirmed_start_when_absent(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows()
        self.airflow.trigger_fail_times = 10
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "capture"})
        self.assertEqual(status, 502, body)
        self.assertTrue(json.loads(body)["dag_run_id"])
        self.assertEqual(len(self.platform.admits), 1)
        self.assertEqual(list(self.airflow.dag_runs), [DAG_RUN_ID])

    # ------------------------------------------------- Linux freeze retry
    def _seed_freeze_failed_parent(self) -> Path:
        """A terminal freeze-failed parent with a sealed intake and its recorded identity."""
        from description_pipeline.io import digest as pipeline_digest
        from description_pipeline.sources.solidworks.revision import package_inventory
        from description_pipeline.sources.solidworks.selection import validate_file_index

        self._seed_retryable_run()
        self.endpoint_server.jobs.pop(native_run_id(DAG_RUN_ID), None)
        intake = self.upload_root / DAG_RUN_ID / "M3.0.3_20261016"
        (intake / "parts").mkdir(parents=True, exist_ok=True)
        (intake / "3.0 总装.SLDASM").write_bytes(b"assembly-bytes")
        (intake / "parts" / "bracket.SLDPRT").write_bytes(b"part-bytes")
        upload_files = validate_file_index(package_inventory(intake))
        self.upload_files = upload_files
        self.upload_digest = pipeline_digest(upload_files)
        native = native_run_id(DAG_RUN_ID)
        request_id = f"portal-{DAG_RUN_ID}"
        conf = self.airflow.dag_runs[DAG_RUN_ID]["conf"]
        conf["handoff_path"] = str(intake)
        conf["main_assembly"] = "3.0 总装.SLDASM"
        self.platform.runs[native] = {
            "run_id": native,
            "state": "failed",
            "request_id": request_id,
            "parent_run_id": None,
            "from_stage": None,
            "tool_identity": None,
        }
        self.platform.requests[request_id] = {
            "request_id": request_id,
            "principal": FEISHU_PRINCIPAL,
            "target_hardware": TEST_TARGET,
            "main_assembly": "3.0 总装.SLDASM",
            "configuration": {"purpose": "engineering"},
            "baseline": {
                "handoff_sha256": self.upload_digest,
                "files": len(upload_files),
                "bytes": sum(path.stat().st_size for path in intake.rglob("*") if path.is_file()),
                "folder": "M3.0.3_20261016",
            },
            "sealed_at": "2026-10-16 21:51:07.308311+08",
        }
        self.platform.stages[native] = {
            "freeze": {"stage": "freeze", "state": "failed", "receipt": {"error": "freeze prelude refused"}}
        }
        return intake

    def test_freeze_retry_creates_a_linked_child_without_a_native_job(self) -> None:
        intake = self._seed_freeze_failed_parent()
        self.client.login()
        jobs_before = set(self.endpoint_server.jobs)
        parent_conf = dict(self.airflow.dag_runs[DAG_RUN_ID]["conf"])
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        rows = {row["stage"]: row for row in json.loads(body)["stage_reruns"]}
        self.assertIs(rows["freeze"]["eligible"], True)
        self.assertEqual(rows["freeze"]["reason"], "linux_ready")
        self.assertTrue(rows["freeze"]["message"])
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        child = payload["dag_run_id"]
        self.assertEqual(payload["resume_from"], "freeze")
        self.assertEqual(payload["resume_from_name"], "Freeze inputs")
        self.assertEqual(len(self.platform.admits), 1)
        admit = self.platform.admits[0]
        self.assertEqual(admit["request_id"], f"portal-{child}")
        self.assertEqual(admit["run_id"], native_run_id(child))
        self.assertEqual(admit["parent_run_id"], native_run_id(DAG_RUN_ID))
        self.assertEqual(admit["from_stage"], "freeze")
        self.assertEqual(admit["dispatch"]["stage"], "freeze")
        self.assertEqual(admit["dispatch"]["operation_key"], f"portal-{child}/freeze:0")
        self.assertEqual(admit["baseline"]["handoff_sha256"], self.upload_digest)
        self.assertEqual(
            self.airflow.trigger_payloads[-1]["conf"],
            {
                "handoff_path": str(intake),
                "parent_dag_run_id": DAG_RUN_ID,
                "resume_from": "freeze",
                "purpose": "engineering",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
                "main_assembly": "3.0 总装.SLDASM",
            },
        )
        self.assertEqual(set(self.endpoint_server.jobs), jobs_before)
        self.assertEqual(self.airflow.dag_runs[DAG_RUN_ID]["conf"], parent_conf)

    def test_freeze_retry_refuses_missing_or_tampered_originals(self) -> None:
        intake = self._seed_freeze_failed_parent()
        self.client.login()
        target = intake / "parts" / "bracket.SLDPRT"
        content = target.read_bytes()
        target.unlink()
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "originals_missing")
        target.write_bytes(b"tampered-part")
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "originals_mismatch")
        self.assertEqual(self.platform.admits, [])
        self.assertEqual(self.airflow.trigger_payloads, [])
        target.write_bytes(content)

    def test_freeze_retry_requires_a_registered_identity_and_terminal_failure(self) -> None:
        self._seed_freeze_failed_parent()
        self.client.login()
        request_id = f"portal-{DAG_RUN_ID}"
        baseline = self.platform.requests[request_id].pop("baseline")
        with patch.object(self.endpoint, "rerun_plan", create=True, side_effect=EndpointError("no native job")):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "originals_identity_missing")
        self.platform.requests[request_id]["baseline"] = baseline
        self.platform.runs[native_run_id(DAG_RUN_ID)]["state"] = "running"
        with patch.object(self.endpoint, "rerun_plan", create=True, side_effect=EndpointError("no native job")):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "linux_owned")
        self.platform.runs[native_run_id(DAG_RUN_ID)]["state"] = "failed"
        self.platform.stages[native_run_id(DAG_RUN_ID)] = {
            "freeze": {"stage": "freeze", "state": "completed", "receipt": {}}
        }
        with patch.object(self.endpoint, "rerun_plan", create=True, side_effect=EndpointError("no native job")):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "linux_owned")
        self.assertEqual(self.platform.admits, [])
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_freeze_retry_reuses_the_open_attempt_and_refuses_a_conflict(self) -> None:
        from description_pipeline.platform.store import OpenAttemptConflictError

        intake = self._seed_freeze_failed_parent()
        child = "portal-20261009T000000-feedface"
        self.platform.seed_open_child(native_run_id(DAG_RUN_ID), child, from_stage="freeze")
        self._seed_airflow_run(
            child,
            state="queued",
            conf={
                "handoff_path": str(intake),
                "parent_dag_run_id": DAG_RUN_ID,
                "resume_from": "freeze",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
                "purpose": "engineering",
                "main_assembly": "3.0 总装.SLDASM",
            },
        )
        self.client.login()
        before = len(self.airflow.trigger_payloads)
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["dag_run_id"], child)
        self.assertEqual(len(self.airflow.trigger_payloads), before)
        self.assertEqual(self.platform.admits, [])
        status, _, body = self.client.request(
            "POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze", "target_hardware": "m3.1"}
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "attempt_mismatch")
        self.platform.runs.pop(native_run_id(child), None)
        other = "20261009T000001-aaaaaaaa"
        self.platform.admit_error = OpenAttemptConflictError(
            native_run_id(DAG_RUN_ID), native_run_id(child), f"portal-{other}"
        )
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertEqual(payload["reason"], "already_active")
        self.assertEqual(payload["active_dag_run_id"], other)

    @staticmethod
    def _freeze_log_bytes(
        *,
        dag_run_id: str,
        intake: str,
        files: int,
        upload_sha256: str,
        main_assembly: str,
        selections: int = 1,
    ) -> bytes:
        """One JSONL freeze task log in the recorded production envelope shape."""
        lines = [
            {
                "timestamp": "2026-10-16T13:51:08.177140Z",
                "level": "info",
                "event": "::group::Pre Execute",
                "logger": "task",
                "filename": "task_runner.py",
                "lineno": 2412,
            }
        ]
        for attempt in range(1, selections + 1):
            lines.append(
                {
                    "timestamp": "2026-10-16T13:51:10.237754Z",
                    "level": "info",
                    "event": (
                        f"selection intake={intake} files={files} upload_sha256={upload_sha256} "
                        f"main_assembly={main_assembly} endpoint_conn=solidworks_windows"
                    ),
                    "logger": "unusual_prefix_solidworks_to_urdf",
                    "try_number": attempt,
                    "run_id": dag_run_id,
                    "ti_id": "01a144fb-1252-77b2-b363-12c7beb80fad",
                    "task_id": "freeze",
                    "dag_id": "solidworks_to_urdf",
                    "map_index": -1,
                    "filename": "solidworks_to_urdf.py",
                    "lineno": 1505,
                }
            )
        return ("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n").encode()

    def _seed_old_shape_parent(
        self,
        *,
        selections: int = 1,
        tamper_log: bool = False,
        symlink_log: bool = False,
        receipt: bool = True,
        bind_main: bool = True,
        intake_override: str | None = None,
        baseline: dict | None = None,
    ) -> Path:
        """The real pre-baseline shape: admitted request WITHOUT baseline + a bound freeze failure log."""
        from description_pipeline.io import digest as pipeline_digest
        from description_pipeline.sources.solidworks.revision import package_inventory
        from description_pipeline.sources.solidworks.selection import validate_file_index

        self._seed_retryable_run()
        self.endpoint_server.jobs.pop(native_run_id(DAG_RUN_ID), None)
        intake = self.upload_root / DAG_RUN_ID / "M3.0.3_20261016"
        (intake / "parts").mkdir(parents=True, exist_ok=True)
        (intake / "3.0 总装.SLDASM").write_bytes(b"assembly-bytes")
        (intake / "parts" / "bracket.SLDPRT").write_bytes(b"part-bytes")
        upload_files = validate_file_index(package_inventory(intake))
        self.upload_files = upload_files
        self.upload_digest = pipeline_digest(upload_files)
        conf = self.airflow.dag_runs[DAG_RUN_ID]["conf"]
        conf["handoff_path"] = str(intake)
        conf["main_assembly"] = "3.0 总装.SLDASM"
        log_dir = Path(tempfile.mkdtemp(prefix="freeze-log-"))
        self.addCleanup(shutil.rmtree, log_dir, ignore_errors=True)
        log_bytes = self._freeze_log_bytes(
            dag_run_id=DAG_RUN_ID,
            intake=intake_override or str(intake),
            files=len(upload_files),
            upload_sha256=self.upload_digest,
            main_assembly="3.0 总装.SLDASM" if bind_main else "other.SLDASM",
            selections=selections,
        )
        log_path = log_dir / "freeze-attempt-1.log"
        log_path.write_bytes(log_bytes)
        self.log_sha = hashlib.sha256(log_bytes).hexdigest()
        bound_path = log_path
        if tamper_log:
            log_path.write_bytes(log_bytes + b"tampered")
        if symlink_log:
            link = log_dir / "freeze-link.log"
            link.symlink_to(log_path)
            bound_path = link
        self.log_path = bound_path
        native = native_run_id(DAG_RUN_ID)
        request_id = f"portal-{DAG_RUN_ID}"
        self.platform.runs[native] = {
            "run_id": native,
            "state": "failed",
            "request_id": request_id,
            "parent_run_id": None,
            "from_stage": None,
            "tool_identity": None,
        }
        self.platform.requests[request_id] = {
            "request_id": request_id,
            "principal": FEISHU_PRINCIPAL,
            "target_hardware": TEST_TARGET,
            "main_assembly": "3.0 总装.SLDASM",
            "configuration": {"purpose": "engineering"},
            "baseline": baseline,
            "sealed_at": "2026-10-16 21:51:07.308311+08",
        }
        stage_receipt = (
            {
                "error": "AirflowFailException: PLATFORM_CONFIG_DIGEST is unavailable",
                "airflow_failure": {"path": str(bound_path), "sha256": self.log_sha},
                "admitted_before_dispatch": True,
            }
            if receipt
            else {"error": "shape-only legacy failure"}
        )
        self.platform.stages[native] = {"freeze": {"stage": "freeze", "state": "failed", "receipt": stage_receipt}}
        return intake

    def test_freeze_retry_old_shape_resolves_the_bound_failure_evidence(self) -> None:
        intake = self._seed_old_shape_parent()
        self.client.login()
        jobs_before = set(self.endpoint_server.jobs)
        parent_conf = dict(self.airflow.dag_runs[DAG_RUN_ID]["conf"])
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        rows = {row["stage"]: row for row in json.loads(body)["stage_reruns"]}
        self.assertIs(rows["freeze"]["eligible"], True)
        self.assertEqual(rows["freeze"]["reason"], "linux_ready")
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 202, body)
        child = json.loads(body)["dag_run_id"]
        self.assertEqual(len(self.platform.admits), 1)
        admit = self.platform.admits[0]
        baseline = admit["baseline"]
        self.assertEqual(baseline["handoff_sha256"], self.upload_digest)
        self.assertEqual(baseline["files"], len(self.upload_files))
        self.assertTrue(baseline["folder"])
        self.assertEqual(
            baseline["provenance"],
            {
                "source": "parent_freeze_failure_log",
                "path": str(self.log_path),
                "sha256": self.log_sha,
                "parent_run_id": native_run_id(DAG_RUN_ID),
            },
        )
        self.assertEqual(admit["from_stage"], "freeze")
        self.assertEqual(
            self.airflow.trigger_payloads[-1]["conf"],
            {
                "handoff_path": str(intake),
                "parent_dag_run_id": DAG_RUN_ID,
                "resume_from": "freeze",
                "purpose": "engineering",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
                "main_assembly": "3.0 总装.SLDASM",
            },
        )
        # The same request rides the two reservation rails: the open child first,
        # and a lost admission never opens a second child.
        status, _, again = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 202, again)
        self.assertEqual(json.loads(again)["dag_run_id"], child)
        self.assertEqual(len(self.platform.admits), 1)
        self.assertEqual(set(self.endpoint_server.jobs), jobs_before)
        self.assertEqual(self.airflow.dag_runs[DAG_RUN_ID]["conf"], parent_conf)

    def test_freeze_retry_old_shape_refuses_unprovable_or_changed_evidence(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="outside-intake-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        self.client.login()
        cases = (
            ("missing_evidence", {"unlink_log": True}, "originals_identity_missing"),
            ("tampered_log", {"tamper_log": True}, "originals_identity_missing"),
            ("ambiguous_selections", {"selections": 2}, "originals_identity_missing"),
            ("receipt_without_binding", {"receipt": False}, "originals_identity_missing"),
            ("main_binding_mismatch", {"bind_main": False}, "originals_identity_missing"),
            ("symlinked_log", {"symlink_log": True}, "originals_identity_missing"),
            ("intake_outside_root", {"intake_override": str(outside)}, "originals_identity_missing"),
            ("intake_changed", {}, "originals_mismatch"),
            ("intake_missing", {}, "originals_missing"),
        )
        for label, options, reason in cases:
            with self.subTest(case=label):
                intake = self._seed_old_shape_parent(**{k: v for k, v in options.items() if k != "unlink_log"})
                self.platform.admits.clear()
                self.airflow.trigger_payloads.clear()
                if options.get("unlink_log"):
                    self.log_path.unlink()
                elif label == "intake_changed":
                    (intake / "3.0 总装.SLDASM").write_bytes(b"tampered-bytes")
                elif label == "intake_missing":
                    (intake / "parts" / "bracket.SLDPRT").unlink()
                status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
                self.assertEqual(status, 409, body)
                self.assertEqual(json.loads(body)["reason"], reason)
                self.assertEqual(self.platform.admits, [])
                self.assertEqual(self.airflow.trigger_payloads, [])

    def test_freeze_retry_malformed_baseline_never_falls_back_to_evidence(self) -> None:
        self.client.login()
        valid = {
            "handoff_sha256": None,
            "files": None,
            "bytes": 64,
            "folder": "M3.0.3_20261016",
        }
        probe = self._seed_old_shape_parent()
        valid["handoff_sha256"] = self.upload_digest
        valid["files"] = len(self.upload_files)
        cases = (
            ("corrupt_handoff", {**valid, "handoff_sha256": "f" * 63}),
            ("wrong_typed_files", {**valid, "files": "71"}),
            ("empty_folder", {**valid, "folder": ""}),
            ("provenance_present", {**valid, "provenance": {"source": "anything"}}),
            ("empty", {}),
        )
        self.assertIsNotNone(probe)
        for label, malformed in cases:
            with self.subTest(case=label):
                self._seed_old_shape_parent(baseline=malformed)
                self.platform.admits.clear()
                self.airflow.trigger_payloads.clear()
                status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
                self.assertEqual(status, 409, body)
                self.assertEqual(json.loads(body)["reason"], "originals_identity_missing")
                self.assertEqual(self.platform.admits, [])
                self.assertEqual(self.airflow.trigger_payloads, [])

    def test_freeze_retry_preserved_provenance_carries_to_the_grandchild(self) -> None:
        self._seed_old_shape_parent()
        self.client.login()
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 202, body)
        child = json.loads(body)["dag_run_id"]
        child_baseline = self.platform.admits[0]["baseline"]
        self.assertEqual(child_baseline["provenance"]["sha256"], self.log_sha)
        # The child fails at freeze and becomes the next retry's terminal parent.
        parent_native = native_run_id(DAG_RUN_ID)
        child_native = native_run_id(child)
        self.platform.runs[child_native]["state"] = "failed"
        self.platform.requests[f"portal-{child}"]["baseline"] = dict(child_baseline)
        self.platform.stages[child_native] = {
            "freeze": {"stage": "freeze", "state": "failed", "receipt": {"error": "freeze refused for the child"}}
        }
        self.airflow.dag_runs[child]["state"] = "failed"
        rows_before = {key: dict(self.platform.runs[key]) for key in (parent_native, child_native)}
        child_stage_before = dict(self.platform.stages[child_native])
        before_admits = len(self.platform.admits)
        status, _, body = self.client.request("POST", f"/api/runs/{child}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 202, body)
        grandchild = json.loads(body)["dag_run_id"]
        self.assertNotEqual(grandchild, child)
        self.assertEqual(len(self.platform.admits), before_admits + 1)
        admit = self.platform.admits[-1]
        self.assertEqual(admit["parent_run_id"], child_native)
        self.assertEqual(admit["from_stage"], "freeze")
        # The preserved provenance flows into the grandchild admission verbatim.
        self.assertEqual(admit["baseline"], child_baseline)
        status, _, again = self.client.request("POST", f"/api/runs/{child}/attempts", {"stage": "freeze"})
        self.assertEqual(status, 202, again)
        self.assertEqual(json.loads(again)["dag_run_id"], grandchild)
        self.assertEqual(len(self.platform.admits), before_admits + 1)
        # Parent and child rows stay byte-unchanged.
        self.assertEqual({key: dict(self.platform.runs[key]) for key in rows_before}, rows_before)
        self.assertEqual(dict(self.platform.stages[child_native]), child_stage_before)

    def test_list_runs_exposes_linked_attempt_lineage(self) -> None:
        self._seed_retryable_run()
        child = "portal-20261009T000000-cafebabe"
        self._seed_airflow_run(
            child,
            state="queued",
            conf={
                "handoff_path": "/srv/robot-cell",
                "parent_dag_run_id": DAG_RUN_ID,
                "resume_from": "verify",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
                "purpose": "engineering",
            },
        )
        self.client.login()
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        runs = {item["dag_run_id"]: item for item in json.loads(body)["runs"]}
        self.assertEqual(runs[child]["parent_dag_run_id"], DAG_RUN_ID)
        self.assertEqual(runs[child]["resume_from"], "verify")
        self.assertEqual(runs[child]["resume_from_name"], "Independent verification")
        self.assertIsNone(runs[DAG_RUN_ID]["parent_dag_run_id"])
        self.assertIsNone(runs[DAG_RUN_ID]["resume_from"])

    def test_run_detail_stage_reruns_passthrough_and_omission(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        # Without an admitted Linux attempt and without a Windows plan the key is omitted.
        self.store.runs.pop(native_run_id(DAG_RUN_ID))
        with patch.object(self.endpoint, "rerun_plan", create=True, side_effect=EndpointError("planner offline")):
            status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        self.assertNotIn("stage_reruns", json.loads(body))
        # An admitted Linux attempt keeps its portable restart rows even without a Windows plan.
        self._seed_retryable_run()
        with patch.object(self.endpoint, "rerun_plan", create=True, side_effect=EndpointError("planner offline")):
            status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        linked = json.loads(body)["stage_reruns"]
        self.assertEqual([row["stage"] for row in linked], [stage["id"] for stage in CONTRACT["stages"]])
        portable = {row["stage"]: row for row in linked if row["stage"] in {"generate", "verify", "publish"}}
        self.assertEqual({row["reason"] for row in portable.values()}, {"linux_ready"})
        self.assertTrue(all(row["eligible"] for row in portable.values()))
        # A Windows plan passes through with the Linux checkpoint overlay on the portable stages.
        rows = [dict(row) for row in self._rerun_rows()]
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        served = json.loads(body)["stage_reruns"]
        self.assertEqual([row["stage"] for row in served], [row["stage"] for row in rows])
        self.assertTrue(all(row["eligible"] for row in served if row["stage"] in {"generate", "verify", "publish"}))

    def test_rerun_attempt_sees_the_admitted_child_despite_a_paged_run_list(self) -> None:
        self._seed_retryable_run()
        child = "portal-20261005T000000-11112222"
        self.platform.seed_open_child(native_run_id(DAG_RUN_ID), child, from_stage="verify")
        self._seed_airflow_run(
            child,
            state="queued",
            conf={
                "handoff_path": "/srv/robot-cell",
                "parent_dag_run_id": DAG_RUN_ID,
                "resume_from": "verify",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
                "purpose": "engineering",
            },
        )
        self.airflow.dag_runs[child]["start_date"] = "2026-10-05T00:00:00Z"
        for index in range(25):
            newer = f"portal-20261008T0000{index:02d}-bbbb{index:04d}"
            self._seed_airflow_run(newer, state="success", conf={})
            self.airflow.dag_runs[newer]["start_date"] = f"2026-10-08T00:{index:02d}:00Z"
        self.client.login()
        before = len(self.airflow.trigger_payloads)
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertEqual(payload["reason"], "already_active")
        self.assertEqual(payload["active_dag_run_id"], child)
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "verify"})
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["dag_run_id"], child)
        self.assertEqual(len(self.airflow.trigger_payloads), before)
        self.assertEqual(self.platform.admits, [])

    def test_rerun_attempt_refuses_when_the_platform_read_fails(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        with patch.object(FakePlatformStore, "lifecycle_rows", side_effect=PlatformStoreError("store down")):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "unresolved_outcome")
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_rerun_attempt_refuses_an_unresolvable_open_set(self) -> None:
        self._seed_retryable_run()
        parent_native = native_run_id(DAG_RUN_ID)
        self.platform.seed_open_child(parent_native, "portal-20261004T000000-aaaa0001")
        self.platform.seed_open_child(parent_native, "portal-20261004T000000-aaaa0002", from_stage="verify")
        self.client.login()
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "unresolved_outcome")
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual(self.platform.admits, [])

    def test_rerun_attempt_concurrent_clicks_create_one_child(self) -> None:
        self._seed_retryable_run()
        rows = self._rerun_rows()
        clients = [PortalClient(self.client.base), PortalClient(self.client.base)]
        for client in clients:
            client.login()
        barrier = threading.Barrier(2)
        results: dict[int, tuple[int, dict, bytes]] = {}

        def click(index: int) -> None:
            barrier.wait()
            results[index] = clients[index].request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})

        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            threads = [threading.Thread(target=click, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        statuses = sorted(status for status, _, _ in results.values())
        self.assertEqual(statuses, [202, 202])
        child_ids = {json.loads(body)["dag_run_id"] for _, _, body in results.values()}
        self.assertEqual(len(child_ids), 1)
        child = child_ids.pop()
        self.assertNotEqual(child, DAG_RUN_ID)
        self.assertEqual(len(self.airflow.trigger_payloads), 1)
        self.assertEqual(len(self.platform.admits), 1)
        self.assertEqual(self.airflow.trigger_payloads[0]["dag_run_id"], child)

    def test_rerun_attempt_refuses_an_unreadable_open_child(self) -> None:
        self._seed_retryable_run()
        native = self.platform.seed_open_child(native_run_id(DAG_RUN_ID), "portal-20261009T000000-eeff0011")
        self.platform.runs[native]["request_id"] = "not-a-portal-identity"
        self.client.login()
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "unresolved_outcome")
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual(self.platform.admits, [])

    def test_rerun_attempt_reuses_the_admitted_reservation_after_restart(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows()
        reserved = "portal-20261009T000000-aaaabbbb"
        self.platform.seed_open_child(native_run_id(DAG_RUN_ID), reserved, from_stage="generate")
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["dag_run_id"], reserved)
        self.assertEqual(self.airflow.trigger_payloads[-1]["dag_run_id"], reserved)
        # The reuse never re-admits and never reads or writes a state file.
        self.assertEqual(self.platform.admits, [])
        self.assertFalse((self.upload_root / ".rerun-attempts").exists())
        self.assertIn(reserved, self.airflow.dag_runs)

    def test_a_rerun_parent_without_a_purpose_refuses(self) -> None:
        self._seed_retryable_run()
        self.airflow.dag_runs[DAG_RUN_ID]["conf"].pop("purpose")
        self.client.login()
        before = len(self.airflow.trigger_payloads)
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "incomplete")
        self.assertEqual(len(self.airflow.trigger_payloads), before)
        self.assertEqual(self.platform.admits, [])

    def test_rerun_attempt_rejects_a_purpose_change_in_the_body(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        status, _, body = self.client.request(
            "POST",
            f"/api/runs/{DAG_RUN_ID}/attempts",
            {"stage": "generate", "purpose": "inspection_preview"},
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(self.platform.admits, [])
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_rerun_attempt_refuses_a_changed_frame_of_an_admitted_reservation(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows()
        reserved = "portal-20261009T000000-dddd1111"
        self.platform.seed_open_child(native_run_id(DAG_RUN_ID), reserved, from_stage="generate")
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            before = len(self.airflow.trigger_payloads)
            for changed in (
                {"stage": "generate", "target_hardware": "m3.1"},
                {"stage": "generate", "change_summary": "another summary"},
            ):
                with self.subTest(changed=sorted(changed)):
                    status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", changed)
                    self.assertEqual(status, 409, body)
                    self.assertEqual(json.loads(body)["reason"], "attempt_mismatch")
                    self.assertEqual(json.loads(body)["active_dag_run_id"], reserved)
            self.assertEqual(len(self.airflow.trigger_payloads), before)
            self.assertEqual(self.platform.admits, [])
            # The exact admitted request still reuses the reservation.
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
            self.assertEqual(status, 202, body)
            self.assertEqual(json.loads(body)["dag_run_id"], reserved)
            self.assertEqual(self.airflow.trigger_payloads[-1]["dag_run_id"], reserved)

    def test_rerun_attempt_refuses_a_cross_session_retry_of_an_admitted_reservation(self) -> None:
        self._seed_retryable_run()
        admin = PortalClient(self.client.base)
        admin.login(ADMIN_TOKEN)
        rows = self._rerun_rows()
        reserved = "portal-20261009T000000-dddd2222"
        self.platform.seed_open_child(native_run_id(DAG_RUN_ID), reserved, from_stage="generate")
        before = len(self.airflow.trigger_payloads)
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = admin.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "attempt_mismatch")
        self.assertEqual(json.loads(body)["active_dag_run_id"], reserved)
        self.assertEqual(len(self.airflow.trigger_payloads), before)
        self.assertEqual(self.platform.admits, [])

    def test_rerun_attempt_answers_a_concurrent_reservation_with_the_winner(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows()
        winner = "portal-20261009T000000-dddd3333"
        self.platform.admit_error = OpenAttemptConflictError(
            native_run_id(DAG_RUN_ID), native_run_id(winner), f"portal-{winner}"
        )
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertEqual(payload["reason"], "already_active")
        self.assertEqual(payload["active_dag_run_id"], winner)
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_rerun_attempt_reuses_the_child_the_scheduler_already_knows(self) -> None:
        self._seed_retryable_run()
        reserved = "portal-20261009T000000-ccccdddd"
        self.platform.seed_open_child(native_run_id(DAG_RUN_ID), reserved, from_stage="generate")
        self._seed_airflow_run(
            reserved,
            state="queued",
            conf={
                "handoff_path": "/srv/robot-cell",
                "parent_dag_run_id": DAG_RUN_ID,
                "resume_from": "generate",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
                "purpose": "engineering",
            },
        )
        self.client.login()
        before = len(self.airflow.trigger_payloads)
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["dag_run_id"], reserved)
        self.assertEqual(len(self.airflow.trigger_payloads), before)
        self.assertEqual(self.platform.admits, [])

    def test_rerun_attempt_closed_admission_refuses_before_any_trigger(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        self.platform.admit_error = AdmissionClosedError("review closed admission")
        rows = self._rerun_rows()
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 503, body)
        self.assertIn("Platform admission is closed", json.loads(body)["error"])
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual(self.platform.admits, [])


if __name__ == "__main__":
    unittest.main()
