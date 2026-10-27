"""End-to-end tests for the operator portal: Airflow auth, one-field start, verified preview."""

from __future__ import annotations

import contextlib
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
from datetime import UTC, datetime
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
    PIPELINE_ID,
    native_run_id,
)
from description_pipeline.orchestration.feishu_oauth import TRIGGERING_USER_NAME_LIMIT
from description_pipeline.orchestration.portal import (
    AirflowDispatchAdapter,
    AirflowApi,
    AirflowApiError,
    DEFAULT_DAG_ID,
    PortalApp,
    PortalConfig,
    load_portal_config,
)

os.environ.setdefault("PLATFORM_CONFIG_DIGEST", "c" * 64)
from description_pipeline.orchestration.uploads import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES
from description_pipeline.io import PipelineError, digest
from description_pipeline.pipeline.contracts import DispatchConf
from description_pipeline.pipeline.dispatch import DispatchResult
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.platform.facts import FactStoreError, RunNotAdmittedError
from description_pipeline.schemas import (
    AIRFLOW_DISPATCH_CONF_SCHEMA,
    VERIFICATION_CERTIFICATE_SCHEMA,
    VERIFY_REQUEST_SCHEMA,
)
from description_pipeline.sources.solidworks import discovery
from description_pipeline.stages import CONTRACT, stage_view

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


class ProjectionArtifactStore:
    """In-memory materializer for complete test-side fact projections."""

    def __init__(self, platform: FakeRunControl) -> None:
        self.platform = platform
        self.objects: dict[str, dict[str, bytes]] = {}
        self.refs: dict[str, dict] = {}

    def register(self, run_id: str, stage: str, files: dict[str, bytes]) -> dict:
        artifact_id = digest({"projection_run": run_id, "stage": stage})
        ordered = sorted(files)
        set_sha256 = digest(
            {
                "members": [
                    {
                        "path": path,
                        "bytes": len(files[path]),
                        "sha256": hashlib.sha256(files[path]).hexdigest(),
                    }
                    for path in ordered
                ]
            }
        )
        record = {
            "artifact_id": artifact_id,
            "kind": f"stage_output:{stage}",
            "locator": f"cas://artifact/{artifact_id}",
            "manifest_sha256": digest({"manifest": artifact_id}),
            "set_sha256": set_sha256,
            "producer_run_id": run_id,
            "producer_stage": stage,
            "producer_tool_sha256": digest({"tool": stage}),
        }
        self.objects[artifact_id] = dict(files)
        self.refs[artifact_id] = record
        return record

    def materialize(self, artifact, destination: str | Path) -> Path:
        root = Path(destination)
        files = self.objects.get(artifact.artifact_id)
        if files is None:
            raise PipelineError("The projected artifact is unavailable")
        root.mkdir(parents=True)
        for name, data in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return root


class FakeRunControl:
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
        self.projection_artifacts = ProjectionArtifactStore(self)

    def fact_projection(self, run_id: str) -> dict | None:
        """Project the registered preview fixture through immutable test artifacts."""

        if self.fixtures is None:
            return None
        entry = self.fixtures.runs.get(run_id)
        if entry is None:
            if not any(row.get("run_id") == run_id for row in self.admits):
                return None
            entry = {
                "meta": None,
                "delivery": False,
                "preview": None,
                "artifacts": {},
                "checkpoints": (),
                "merged": {},
            }
        preview = entry.get("preview") if isinstance(entry.get("preview"), dict) else {}
        current = entry.get("artifacts") or {}
        merged = entry.get("merged") if isinstance(entry.get("merged"), dict) else {}
        stage_report = current.get("reports/native-stages.json")
        if not isinstance(stage_report, (bytes, bytearray)) and isinstance(
            merged.get("events"),
            list,
        ):
            stage_report = json.dumps(
                stage_view({**merged, "run_id": run_id}),
                sort_keys=True,
            ).encode("utf-8")
        files = {
            **(preview.get("files") or {}),
            **(preview.get("report_files") or {}),
        }
        event_states: dict[str, str] = {}
        for event in merged.get("events") or ():
            if (
                isinstance(event, dict)
                and isinstance(event.get("stage"), str)
                and event.get("check") is None
                and event.get("state") in {"completed", "failed", "cancelled"}
            ):
                event_states[event["stage"]] = event["state"]
        failed_index = next(
            (
                index
                for index, name in enumerate(
                    ("freeze", "discover", "capture", "generate", "verify", "publish")
                )
                if event_states.get(name) == "failed"
            ),
            None,
        )
        stages: dict[str, dict] = {}
        by_stage: dict[str, dict] = {}
        members: dict[str, dict] = {}
        generate_receipt = None
        generate_receipt_bytes = None
        stage_order = ("freeze", "discover", "capture", "generate", "verify", "publish")
        for stage_index, stage in enumerate(stage_order):
            selected = {
                path: bytes(current.get(path, b""))
                for path in files
                if (
                    (stage == "capture" and path.startswith(("input/", "evidence/")))
                    or (
                        stage == "generate"
                        and path.startswith(("model/", "urdf/", "meshes/"))
                    )
                    or (stage == "generate" and path == "tool.json")
                    or (
                        stage == "verify"
                        and path.startswith("reports/")
                    )
                )
            }
            if preview and stage == "capture" and not selected:
                selected["input/handoff.json"] = b'{"fixture":true}\n'
            if preview and stage == "generate":
                if not selected:
                    selected["proof/placeholder.json"] = b"{}\n"
                capture_member = next(iter((by_stage.get("capture") or {}).values()), None)
                capture_artifact = (
                    capture_member.get("artifact")
                    if isinstance(capture_member, dict)
                    else None
                )
                if isinstance(capture_artifact, dict):
                    generate_tool = digest({"tool": "generate"})
                    generate_receipt = {
                        "actual_executor_sha256": generate_tool,
                        "capture_artifact": capture_artifact,
                        "config_snapshot_sha256": "e" * 64,
                        "contract_sha256": "f" * 64,
                        "required_executor_sha256": generate_tool,
                    }
                    selected["reports/generate.json"] = (
                        json.dumps(generate_receipt, sort_keys=True).encode("utf-8") + b"\n"
                    )
                    generate_receipt_bytes = selected["reports/generate.json"]
            if stage == "verify" and preview:
                quality_payload = current.get("reports/quality.json")
                quality_record = {}
                if isinstance(quality_payload, (bytes, bytearray)):
                    try:
                        parsed = json.loads(bytes(quality_payload))
                    except ValueError:
                        parsed = {}
                    quality_record = parsed if isinstance(parsed, dict) else {}
                generate_member = next(
                    iter((by_stage.get("generate") or {}).values()),
                    None,
                )
                capture_member = next(
                    iter((by_stage.get("capture") or {}).values()),
                    None,
                )
                if not isinstance(generate_member, dict):
                    fallback = self.projection_artifacts.register(
                        run_id,
                        "generate",
                        {"proof/placeholder.json": b"{}\n"},
                    )
                    generate_member = {
                        "artifact": fallback,
                        "sha256": hashlib.sha256(b"{}\n").hexdigest(),
                    }
                if not isinstance(capture_member, dict):
                    fallback = self.projection_artifacts.register(
                        run_id,
                        "capture",
                        {"input/handoff.json": b"{}\n"},
                    )
                    capture_member = {
                        "artifact": fallback,
                        "sha256": hashlib.sha256(b"{}\n").hexdigest(),
                    }
                if generate_receipt_bytes is None:
                    generate_receipt_bytes = b"{}\n"
                generate_artifact = generate_member["artifact"]
                capture_artifact = capture_member["artifact"]
                pair_files = {
                    path: member["sha256"]
                    for path, member in {
                        **by_stage["capture"],
                        **by_stage["generate"],
                    }.items()
                    if (
                        path in {"tool.json"}
                        or path.startswith(("input/", "evidence/", "model/", "urdf/", "meshes/"))
                    )
                }
                request = {
                    "capture_artifact": capture_artifact,
                    "config_snapshot_sha256": "e" * 64,
                    "contract_sha256": "f" * 64,
                    "expected_verifier_sha256": generate_artifact["producer_tool_sha256"],
                    "generate_artifact": generate_artifact,
                    "output_contract": "solidworks-to-urdf.verification-artifact/v1",
                    "policy_sha256": "1" * 64,
                    "purpose": preview.get("purpose") or "engineering",
                    "schema_version": VERIFY_REQUEST_SCHEMA,
                }
                certificate = {
                    "schema_version": VERIFICATION_CERTIFICATE_SCHEMA,
                    "decision": "PASS"
                    if preview.get("verified") is not False
                    else "FAIL",
                    "passed": preview.get("verified") is not False,
                    "subject_sha256": generate_artifact["set_sha256"],
                    "checks": quality_record.get("checks") or preview.get("checks") or [],
                    "diagnostics": quality_record.get("diagnostics")
                    or preview.get("warnings")
                    or [],
                    "actual_verifier": {"executor": "verify"},
                    "actual_verifier_sha256": generate_artifact["producer_tool_sha256"],
                    "capture_artifact": capture_artifact,
                    "config_snapshot_sha256": "e" * 64,
                    "contract_sha256": "f" * 64,
                    "evaluation_subject_sha256": digest(pair_files),
                    "generate_artifact": generate_artifact,
                    "generate_receipt_sha256": hashlib.sha256(
                        generate_receipt_bytes
                    ).hexdigest(),
                    "policy_sha256": "1" * 64,
                    "purpose": preview.get("purpose") or "engineering",
                    "request": request,
                    "request_sha256": digest(request),
                }
                certificate.update(entry.get("certificate_override") or {})
                certificate["verification_key"] = digest(certificate)
                selected["verification-certificate.json"] = (
                    json.dumps(certificate, sort_keys=True).encode("utf-8") + b"\n"
                )
                selected["verification-execution.json"] = b"{}\n"
            if (
                not selected
                and "freeze" in (entry.get("checkpoints") or ())
                and isinstance(current.get("input/request.json"), (bytes, bytearray))
            ):
                selected["request.json"] = bytes(current["input/request.json"])
            if stage_report is not None and (
                stage in (entry.get("checkpoints") or ()) or preview
            ):
                selected["reports/native-stages.json"] = stage_report
            merged_status = str((entry.get("merged") or {}).get("status") or "")
            state = "completed" if stage in (entry.get("checkpoints") or ()) else None
            if preview and stage in {"freeze", "discover", "capture", "generate"}:
                state = "completed"
            if stage == "verify" and preview:
                state = "completed" if preview.get("verified") is not False else "failed"
            if (
                stage == "publish"
                and preview.get("verified") is not False
                and merged_status in {"passed", "succeeded"}
                and isinstance((entry.get("merged") or {}).get("result"), dict)
            ):
                state = "completed"
                result = (entry.get("merged") or {}).get("result") or {}
                submission = result.get("submission") if isinstance(result, dict) else None
                if isinstance(submission, dict):
                    selected["publication-receipt.json"] = (
                        json.dumps(
                            {
                                "base_ref": submission.get("base"),
                                "branch_ref": submission.get("branch"),
                                "candidate_parents": ["0" * 40],
                                "pr_effect": {
                                    "observation": {
                                        "head_oid": submission.get("commit"),
                                        "number": 1,
                                        "state": "OPEN",
                                        "url": submission.get("url"),
                                    }
                                },
                                "publication_subject_sha256": submission.get(
                                    "subject_sha256"
                                ),
                                "repository": submission.get("repository_slug"),
                            },
                            sort_keys=True,
                        ).encode("utf-8")
                        + b"\n"
                    )
            if failed_index is not None:
                if stage_index < failed_index:
                    state = "completed"
                elif stage_index == failed_index:
                    state = "failed"
                else:
                    state = None
            if state is None:
                stages[stage] = {
                    "stage": stage,
                    "state": "waiting",
                    "decision": None,
                    "receipt": {"artifacts": []},
                    "updated_at": None,
                }
                by_stage[stage] = {}
                continue
            artifact = self.projection_artifacts.register(run_id, stage, selected)
            rows = {}
            artifacts = []
            for path, data in sorted(selected.items()):
                expected = files[path] if path in files else hashlib.sha256(data).hexdigest()
                member = {
                    "bytes": len(data),
                    "sha256": expected,
                    "artifact": artifact,
                }
                rows[path] = member
                artifacts.append({"path": path, **member})
                members[path] = member
            decision = {
                "command_id": f"{run_id}-{stage}",
                "outcome": state,
                "reason_code": None,
            }
            failure = None
            if state != "completed":
                failure = {
                    "reason_code": "verification_failed",
                    "error_message": preview.get("error") or "verification failed",
                }
            stages[stage] = {
                "stage": stage,
                "state": state,
                "decision": decision,
                "receipt": {
                    "artifacts": artifacts,
                    "failure": failure,
                },
                "updated_at": None,
            }
            by_stage[stage] = rows
        effect_states = [
            item
            for item in (entry.get("effect_states") or [])
            if isinstance(item, dict) and isinstance(item.get("state"), str)
        ]
        status = str((entry.get("merged") or {}).get("status") or "passed")
        run_state = {
            "passed": "succeeded",
            "succeeded": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "unresolved": "unresolved",
            "running": "running",
        }.get(status, "running")
        if any(
            item["state"] in {"unknown", "conflict"} for item in effect_states
        ):
            run_state = "unresolved"
        override = self.stages.get(run_id)
        if override is not None:
            for stage in stage_order:
                if stage not in override:
                    stages[stage] = {
                        "stage": stage,
                        "state": "waiting",
                        "decision": None,
                        "receipt": {"artifacts": []},
                        "updated_at": None,
                    }
                    by_stage[stage] = {}
        for stage, row in (override or {}).items():
            if stage not in stages or not isinstance(row, dict):
                continue
            stage_state = row.get("state")
            if not isinstance(stage_state, str):
                continue
            stages[stage]["state"] = stage_state
            stages[stage]["receipt"] = row.get("receipt") or stages[stage]["receipt"]
            stages[stage]["decision"] = {
                "command_id": row.get("command_id", f"{run_id}-{stage}"),
                "outcome": stage_state,
                "reason_code": (row.get("receipt") or {}).get("error_code"),
            }
        scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else None
        scope_kind = str((scope or {}).get("kind") or "formal")
        target_stage = (scope or {}).get("target_stage")
        for stage, row in stages.items():
            decision = row.get("decision")
            outcome = decision.get("outcome") if isinstance(decision, dict) else None
            stage_scope = (
                "diagnostic"
                if scope_kind == "diagnostic" and stage == target_stage
                else "formal"
            )
            formal = outcome == "completed" and stage_scope == "formal"
            qualification = (
                {
                    "outcome": outcome,
                    "scope_kind": stage_scope,
                    "formal_eligible": formal,
                    "qualification_digest": digest(
                        {
                            "run_id": run_id,
                            "stage": stage,
                            "outcome": outcome,
                            "scope_kind": stage_scope,
                        }
                    ),
                }
                if isinstance(outcome, str)
                else None
            )
            row["scope_kind"] = stage_scope
            row["formal_eligible"] = formal
            row["qualification"] = qualification
            row["in_scope"] = scope_kind != "diagnostic" or stage == target_stage
            if stage == "verify":
                row["receipt"]["effect_states"] = effect_states
                if any(
                    item["state"] in {"unknown", "conflict"}
                    for item in effect_states
                ):
                    row["formal_eligible"] = False
            qualification_overrides = entry.get("qualification_overrides")
            override_qualification = (
                qualification_overrides.get(stage)
                if isinstance(qualification_overrides, dict)
                else None
            )
            if isinstance(override_qualification, dict) and qualification is not None:
                qualification.update(override_qualification)
                row["formal_eligible"] = qualification.get("formal_eligible") is True
        members = {
            path: member
            for stage in stage_order
            for path, member in by_stage.get(stage, {}).items()
        }
        admitted = next(
            (
                row
                for row in reversed(self.admits)
                if row.get("run_id") == run_id
            ),
            {},
        )
        configuration = admitted.get("configuration")
        configuration = configuration if isinstance(configuration, dict) else {}
        run_scope = (
            {
                "kind": "diagnostic",
                "target_stage": target_stage,
                "formal_eligible": False,
            }
            if scope_kind == "diagnostic"
            else {
                "kind": "formal",
                "target_stage": None,
                "formal_eligible": run_state == "succeeded",
            }
        )
        request_payload = (
            dict(entry["admission_request"])
            if isinstance(entry.get("admission_request"), dict)
            else {
                "main_assembly": admitted.get("main_assembly") or "robot.SLDASM",
                "target_hardware": admitted.get("target_hardware") or TEST_TARGET,
            }
        )
        return {
            "run_id": run_id,
            "request_id": entry.get("request_id") or admitted.get("request_id"),
            "parent_run_id": entry.get("parent_run_id") or admitted.get("parent_run_id"),
            "from_stage": entry.get("from_stage") or admitted.get("from_stage"),
            "request": request_payload,
            "purpose": (
                preview.get("purpose")
                or self._freeze_purpose(current.get("input/request.json"))
                or configuration.get("purpose")
                or "engineering"
            ),
            "scope": run_scope,
            "formal_eligible": run_scope["formal_eligible"],
            "state": run_state,
            "events": list(merged.get("events") or ()),
            "stages": list(stages.values()),
            "by_stage": by_stage,
            "members": members,
            "technical": {
                "commands": [],
                "claims": [],
                "effects": effect_states,
            },
        }

    @staticmethod
    def _freeze_purpose(payload: object) -> str | None:
        if not isinstance(payload, (bytes, bytearray)):
            return None
        try:
            record = json.loads(bytes(payload))
        except (UnicodeDecodeError, ValueError):
            return None
        purpose = record.get("purpose") if isinstance(record, dict) else None
        return purpose if isinstance(purpose, str) else None

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

    def _stage_report_bytes(self, run_id: str) -> bytes | None:
        entry = self._entry(run_id) or {}
        merged = entry.get("merged")
        if not isinstance(merged, dict) or not isinstance(merged.get("events"), list):
            return None
        return json.dumps(stage_view({**merged, "run_id": run_id}), sort_keys=True).encode("utf-8")

    def _stage_report_reference(self, run_id: str) -> dict | None:
        data = self._stage_report_bytes(run_id)
        if data is None:
            return None
        return {"path": "reports/stages.json", "sha256": hashlib.sha256(data).hexdigest()}

    def stage_records(self, run_id: str) -> dict[str, dict]:
        override = self.stages.get(run_id)
        if override is not None:
            return {stage: dict(row) for stage, row in override.items()}
        entry = self._entry(run_id)
        if entry is None:
            return {}
        stage_report = self._stage_report_reference(run_id)
        stages: dict[str, dict] = {
            stage: {
                "stage": stage,
                "state": "completed",
                "receipt": {
                    "subject_sha256": None,
                    "artifacts": [],
                    **({"stage_report": stage_report} if stage_report else {}),
                },
                "original_executed_at": None,
                "updated_at": None,
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
                        **({"stage_report": stage_report} if stage_report else {}),
                    },
                    "original_executed_at": None,
                    "updated_at": None,
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
                    **({"stage_report": stage_report} if stage_report else {}),
                },
                "original_executed_at": None,
                "updated_at": None,
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
                    **({"stage_report": stage_report} if stage_report else {}),
                },
                "original_executed_at": None,
                "updated_at": None,
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
                    "kind": event.get("kind") if isinstance(event.get("kind"), str) else "stage",
                    "stage": event.get("stage"),
                    "operation_id": None,
                    "payload": event,
                }
            )
        return [row for row in rows if row["seq"] > after_seq][:limit]

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

    def list_run_display_metadata(self) -> dict:
        return dict(self.displays)

    def get_run_display_metadata(self, run_id: str):
        return self.displays.get(run_id)

    def rename_run(self, run_id: str, display_name: str | None):
        if run_id not in self.runs:
            raise RunNotAdmittedError("The run is not admitted")
        display = self.displays.get(run_id) or SimpleNamespace(
            display_name=None,
            hidden_at=None,
            hidden_reason=None,
            updated_at=datetime.now(UTC),
        )
        display.display_name = display_name
        display.updated_at = datetime.now(UTC)
        self.displays[run_id] = display
        return display

    def hide_run(self, run_id: str, *, actor: str):
        if run_id not in self.runs:
            raise RunNotAdmittedError("The run is not admitted")
        now = datetime.now(UTC)
        display = self.displays.get(run_id) or SimpleNamespace(
            display_name=None,
            hidden_at=None,
            hidden_reason=None,
            updated_at=now,
        )
        display.hidden_at = display.hidden_at or now
        display.hidden_reason = display.hidden_reason or actor
        display.updated_at = now
        self.displays[run_id] = display
        return display

    def restore_run(self, run_id: str):
        if run_id not in self.runs:
            raise RunNotAdmittedError("The run is not admitted")
        display = self.displays.get(run_id) or SimpleNamespace(
            display_name=None,
            hidden_at=None,
            hidden_reason=None,
            updated_at=datetime.now(UTC),
        )
        display.hidden_at = None
        display.hidden_reason = None
        display.updated_at = datetime.now(UTC)
        self.displays[run_id] = display
        return display


class FakeProductionApplication:
    """Test boundary for the production application's admission and dispatch API."""

    def __init__(self) -> None:
        self.outcomes = {}
        self.dispatch_records = {}
        self.dispatch_calls = []
        self.dispatched = set()
        self.admission_requests = []
        self.freeze_successor_requests = []

    def load_admission_outcome(self, run_id: str):
        return self.outcomes.get(run_id)

    def register(self, outcome, conf: DispatchConf) -> None:
        self.outcomes[outcome.admission.run_id] = outcome
        self.dispatch_records[outcome.dispatch_command.command_id] = (
            outcome.dispatch_command,
            conf,
        )

    def dispatch(self, command_id: str, adapter) -> DispatchResult:
        self.dispatch_calls.append(command_id)
        command, conf = self.dispatch_records[command_id]
        response = None
        if command_id not in self.dispatched:
            self.dispatched.add(command_id)
            with contextlib.suppress(AirflowApiError):
                response = adapter.trigger(command, conf)
        observation = adapter.observe(command, conf)
        state = (
            EffectState.SATISFIED
            if observation.state_class == "known"
            else EffectState.UNKNOWN
        )
        return DispatchResult(
            command_id=command_id,
            effect_id="dispatch-effect-" + digest(response or {}),
            state=state,
            observation=observation,
        )


class PortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.airflow = MockAirflow()
        self.airflow.__enter__()
        self.addCleanup(self.airflow.__exit__, None, None, None)
        self.platform = FakeRunControl()
        self.upload_root = Path(tempfile.mkdtemp(prefix="portal-upload-"))
        self.addCleanup(shutil.rmtree, self.upload_root, ignore_errors=True)
        self.endpoint_server = FakeEndpoint()
        self.endpoint_server.__enter__()
        self.addCleanup(self.endpoint_server.__exit__, None, None, None)
        self.store_root = Path(tempfile.mkdtemp(prefix="portal-store-"))
        self.addCleanup(shutil.rmtree, self.store_root, ignore_errors=True)
        self.store = PreviewStore(self.store_root)
        self.platform.fixtures = self.store
        self.production_application = FakeProductionApplication()
        self.static_dir = Path(__file__).resolve().parents[2] / "src/description_pipeline/orchestration/static"
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url),
            static_dir=self.static_dir,
            upload_root=self.upload_root,
            run_control_store=self.platform,
            pipeline_targets=TEST_TARGETS,
            config_identity_provider=lambda: "c" * 64,
            run_projection_factory=self.platform.fact_projection,
            artifact_store=self.platform.projection_artifacts,
            production_admission_loader=self._test_admission_loader,
            production_freeze_successor_loader=self._test_freeze_successor_loader,
            production_application=self.production_application,
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

    def _test_admission_loader(self, request):
        self.production_application.admission_requests.append(request)
        existing = self.production_application.load_admission_outcome(request.run_id)
        if existing is not None:
            return existing
        admission = self.platform.admission()
        if admission.state != "open":
            raise FactStoreError("The platform admission is closed")
        if self.platform.admit_error is not None:
            raise self.platform.admit_error
        dag_run_id = request.request_id.removeprefix("portal-")
        dispatch = {
            "dag_id": DEFAULT_DAG_ID,
            "dag_run_id": dag_run_id,
            "operation_key": f"{request.request_id}/freeze:0",
            "params_digest": digest(
                {
                    "change_summary": request.change_summary,
                    "main_assembly": request.main_assembly,
                    "target_hardware": request.target_hardware,
                }
            ),
            "stage": "freeze",
        }
        self.platform.admit_run(
            request_id=request.request_id,
            run_id=request.run_id,
            principal=request.principal,
            target_hardware=request.target_hardware,
            main_assembly=request.main_assembly,
            configuration={"purpose": request.purpose},
            baseline={
                "bytes": request.receipt.bytes,
                "files": request.receipt.files,
                "folder": request.receipt.folder,
                "handoff_sha256": request.receipt.handoff_sha256,
                "platform_config_digest": "c" * 64,
            },
            dispatch=dispatch,
            from_stage=request.from_stage,
            parent_run_id=None,
        )
        command = SimpleNamespace(
            command_id="dispatch:" + digest(
                {
                    "request_id": request.request_id,
                    "run_id": request.run_id,
                }
            ),
            run_id=request.run_id,
            dag_id=DEFAULT_DAG_ID,
            dag_run_id=dag_run_id,
        )
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=request.run_id,
            freeze_command_id="freeze:" + digest(request.run_id),
        )
        outcome = SimpleNamespace(
            admission=SimpleNamespace(
                run_id=request.run_id,
                request_id=request.request_id,
            ),
            dispatch_command=command,
        )
        self.production_application.register(outcome, conf)
        return outcome

    def _test_freeze_successor_loader(self, request):
        self.production_application.freeze_successor_requests.append(request)
        existing = self.production_application.load_admission_outcome(
            request.run_id
        )
        if existing is not None:
            return existing
        dag_run_id = request.request_id.removeprefix("portal-")
        command = SimpleNamespace(
            command_id="dispatch:" + digest(
                {
                    "request_id": request.request_id,
                    "run_id": request.run_id,
                }
            ),
            run_id=request.run_id,
            dag_id=DEFAULT_DAG_ID,
            dag_run_id=dag_run_id,
        )
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=request.run_id,
            freeze_command_id="freeze:" + digest(request.run_id),
        )
        outcome = SimpleNamespace(
            admission=SimpleNamespace(
                run_id=request.run_id,
                parent_run_id=request.parent_run_id,
                request_id=request.request_id,
            ),
            dispatch_command=command,
        )
        self.production_application.register(outcome, conf)
        return outcome

    def test_dispatch_observer_binds_the_full_conf_record(self) -> None:
        command = SimpleNamespace(
            dag_id=DEFAULT_DAG_ID,
            dag_run_id=DAG_RUN_ID,
        )
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=RUN_ID,
            freeze_command_id="freeze:" + digest(RUN_ID),
        )
        adapter = AirflowDispatchAdapter(
            AirflowApi(self.airflow.url),
            AIRFLOW_TOKEN,
        )
        self.airflow.dag_runs[DAG_RUN_ID] = {
            "dag_id": DEFAULT_DAG_ID,
            "dag_run_id": DAG_RUN_ID,
            "conf": conf.to_record(),
            "state": "queued",
        }
        observation = adapter.observe(command, conf)
        self.assertEqual(observation.state_class, "known")

        self.airflow.dag_runs[DAG_RUN_ID]["conf"] = {
            **conf.to_record(),
            "freeze_command_id": "freeze:another-command",
        }
        with self.assertRaisesRegex(
            AirflowApiError,
            "dispatch conf identity",
        ):
            adapter.observe(command, conf)

    def _start_extra_portal(
        self,
        *,
        production_admission_loader=None,
        production_freeze_successor_loader=None,
        production_application=None,
    ):
        """A restarted portal process: same Airflow, brand-new (empty) session store."""
        production_admission_loader = (
            production_admission_loader
            or self._test_admission_loader
        )
        production_freeze_successor_loader = (
            production_freeze_successor_loader
            or self._test_freeze_successor_loader
        )
        production_application = (
            production_application
            or self.production_application
        )
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url),
            static_dir=self.static_dir,
            upload_root=self.upload_root,
            run_control_store=self.platform,
            pipeline_targets=TEST_TARGETS,
            config_identity_provider=lambda: "c" * 64,
            run_projection_factory=self.platform.fact_projection,
            artifact_store=self.platform.projection_artifacts,
            production_admission_loader=production_admission_loader,
            production_freeze_successor_loader=production_freeze_successor_loader,
            production_application=production_application,
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
            from tests.v1.source_record_support import source_record

            # The registered capture tree carries the frozen source identity the
            # run page reads its hardware/revision from.
            capture = self.store.root / "sealed" / run_id / "capture" / "input"
            capture.mkdir(parents=True, exist_ok=True)
            (capture / "source.json").write_text(
                json.dumps(source_record(
                    hardware_id=hardware,
                    handoff_digest="a" * 64,
                    main_assembly=job.get("main_assembly") or "robot.SLDASM",
                    revision=job.get("revision"),
                )),
                encoding="utf-8",
            )

    def _seed_7b_stage_display(self) -> str:
        """Register the failed 7b-shaped run, including its bound native stage report."""
        from description_pipeline.stages import CONTRACT_FILE_SHA256, CONTRACT_SHA256, STAGE_IDS

        self._seed_airflow_run(
            DAG_RUN_ID,
            state="failed",
            conf={
                "handoff_path": "/home/andy/cad-handoffs/M3.0.3_20261016",
                "main_assembly": "M3.0.3_20261016.SLDASM",
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
                "purpose": "inspection_preview",
            },
        )
        run_id = native_run_id(DAG_RUN_ID)
        states = {
            "freeze": "completed",
            "discover": "completed",
            "capture": "failed",
            "generate": "blocked",
            "verify": "blocked",
            "publish": "blocked",
        }
        report_stages = []
        for definition in CONTRACT["stages"]:
            stage_id = definition["id"]
            boundary_rows = {}
            for boundary in ("input", "output"):
                rows = []
                for item in definition[f"{boundary}_qc"]:
                    passed = (
                        (stage_id in {"freeze", "discover"} and states[stage_id] == "completed")
                        or (stage_id == "capture" and boundary == "input")
                    )
                    rows.append(
                        {
                            "id": item["id"],
                            "label": item["label"],
                            "state": "passed" if passed else "not_run",
                            "details": {"passed": True} if passed else {"reason": "No recorded check result"},
                        }
                    )
                boundary_rows[boundary] = rows
            checks = boundary_rows["input"] + boundary_rows["output"]
            report_stages.append(
                {
                    "id": stage_id,
                    "name": definition["name"],
                    "state": states[stage_id],
                    "checks_passed": sum(row["state"] == "passed" for row in checks),
                    "checks_total": len(checks),
                    "input_qc": boundary_rows["input"],
                    "output_qc": boundary_rows["output"],
                }
            )
        handoff = "d" * 64
        native_report = json.dumps(
            {
                "schema_version": "solidworks-to-urdf.stages/v1",
                "pipeline_id": PIPELINE_ID,
                "contract_sha256": CONTRACT_SHA256,
                "contract_file_sha256": CONTRACT_FILE_SHA256,
                "run_id": run_id,
                "execution_scope": list(STAGE_IDS),
                "subject_sha256": None,
                "handoff_sha256": handoff,
                "release_approval": "required",
                "stages": report_stages,
                "events": [],
                "unsupported": [],
            },
            sort_keys=True,
        ).encode("utf-8")
        native_report_digest = hashlib.sha256(native_report).hexdigest()
        native_stages_member = {
            "path": "reports/native-stages.json",
            "bytes": len(native_report),
            "sha256": native_report_digest,
        }
        failure = {
            "error": "the link group payload echoes another selection",
            "error_code": "cad_link_group_payload_invalid",
            "detail": {
                "code": "cad_link_group_payload_invalid",
                "message": "the link group payload echoes another selection",
                "body": "r_thigh_yaw",
            },
        }
        self.platform.stages[run_id] = {
            stage_id: {
                "stage": stage_id,
                "state": state,
                "receipt": {
                    "subject_sha256": None,
                    "artifacts": [native_stages_member] if stage_id == "discover" else [],
                    "handoff_sha256": handoff,
                    **(failure if stage_id == "capture" else {}),
                },
                "original_executed_at": None,
            }
            for stage_id, state in states.items()
        }
        self.platform.runs[run_id] = {
            "run_id": run_id,
            "state": "failed",
            "request_id": f"request-{DAG_RUN_ID}",
            "parent_run_id": None,
            "from_stage": None,
            "tool_identity": None,
        }
        events = [
            {"stage": "freeze", "state": "running", "at": "2026-10-19T18:37:14Z"},
            {
                "stage": "freeze",
                "state": "running",
                "at": "2026-10-19T18:37:14Z",
                "check": {
                    "id": "handoff.admission",
                    "boundary": "input",
                    "state": "passed",
                    "details": {"handoff_sha256": handoff},
                },
            },
            {
                "stage": "freeze",
                "state": "running",
                "at": "2026-10-19T18:37:14Z",
                "check": {
                    "id": "handoff.integrity",
                    "boundary": "output",
                    "state": "passed",
                    "details": {"handoff_sha256": handoff},
                },
            },
            {"stage": "freeze", "state": "completed", "at": "2026-10-19T18:37:14Z"},
            # The registered event stream carries discovery's completion without
            # check rows; the sealed report is the only place those rows survive.
            {"stage": "discover", "state": "completed", "at": "2026-10-19T18:49:40Z"},
            {
                "stage": "capture",
                "state": "running",
                "at": "2026-10-19T18:49:41Z",
                "check": {
                    "id": "input.valid",
                    "boundary": "input",
                    "state": "passed",
                    "details": {"passed": True},
                },
            },
            {"stage": "capture", "state": "failed", "at": "2026-10-19T18:49:35Z"},
            {"stage": "generate", "state": "blocked", "at": "2026-10-19T18:49:41Z"},
            {"stage": "verify", "state": "blocked", "at": "2026-10-19T18:49:41Z"},
            {"stage": "publish", "state": "blocked", "at": "2026-10-19T18:49:41Z"},
            {
                "kind": "binding",
                "stage": "capture",
                "at": "2026-10-19T18:49:47Z",
                "dag_id": DEFAULT_DAG_ID,
                "task_id": "discover",
                "actual_stage": "capture",
                "intent_stage": "discover",
                "closure_shape": "bound_native_capture_failed",
                "native_run_id": run_id,
                "operation_key": f"{run_id}/native:0",
                "reported_stage": "capture",
                "reconciled_stage": "discover",
                "reconciled_state": "failed",
            },
        ]
        self._seed_job(
            DAG_RUN_ID,
            status="failed",
            error="the link group payload echoes another selection",
            result=None,
            events=events,
        )
        self.store.add(
            run_id,
            meta={"handoff_sha256": handoff},
            preview={
                "schema_version": "solidworks-to-urdf.preview/v1",
                "pipeline_id": PIPELINE_ID,
                "run_id": run_id,
                "subject_sha256": None,
                "urdf": "urdf/robot.urdf",
                "files": {},
                "report_files": {"reports/native-stages.json": native_report_digest},
                "verified": False,
                "state": "unverified",
            },
            artifacts={"reports/native-stages.json": native_report},
            merged={"status": "failed", "events": events},
        )
        return run_id

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
                "branch": "work/description/m3.0/0123456789abcdef01234567",
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

        job = self.endpoint_server.jobs.get(run_id) or {}
        files = {
            path: hashlib.sha256(data).hexdigest()
            for path, data in members.items()
            if not path.startswith("reports/") and path != "manifest.json"
        }
        report_files = {
            path: hashlib.sha256(data).hexdigest() for path, data in members.items() if path.startswith("reports/")
        }
        quality_payload = members.get("reports/quality.json")
        quality = {}
        if isinstance(quality_payload, (bytes, bytearray)):
            try:
                parsed = json.loads(bytes(quality_payload))
            except ValueError:
                parsed = {}
            quality = parsed if isinstance(parsed, dict) else {}
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
                "checks": quality.get("checks") or [],
                "warnings": quality.get("diagnostics") or [],
            },
            artifacts=dict(members),
            merged={
                key: job[key]
                for key in ("discovery", "events", "result")
                if key in job
            }
            | (
                {"status": self._STORE_RUN_STATE.get(str(job.get("status")), job.get("status"))}
                if job.get("status") is not None
                else {}
            ),
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
        # The immutable admission owns purpose independently of the mutable conf echo.
        self.store.runs[native_run_id(DAG_RUN_ID)]["checkpoints"] = ()
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["purpose"], "inspection_preview")

    def test_registered_purpose_reads_the_freeze_request_and_never_guesses(self) -> None:
        app = PortalApp.__new__(PortalApp)
        projection = {"purpose": "inspection_preview"}
        self.assertEqual(PortalApp._registered_purpose(app, projection), "inspection_preview")
        self.assertIsNone(PortalApp._registered_purpose(app, {}))

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
        entry = self.store.runs[native_run_id(DAG_RUN_ID)]
        entry["request_id"] = f"portal-{DAG_RUN_ID}"
        merged = entry["merged"]
        merged["status"] = "running"
        merged.pop("result", None)
        entry["admission_request"] = {
            "change_summary": TEST_SUMMARY,
            "main_assembly": "model.SLDASM",
            "originals": {
                "members": [
                    {"path": "model.SLDASM"},
                    {"path": "parts/arm.SLDASM"},
                    {"path": "parts/p1.SLDPRT"},
                ]
            },
            "purpose": "engineering",
            "target_hardware": TEST_TARGET,
        }
        self.airflow.dag_runs[DAG_RUN_ID]["state"] = "failed"
        self.airflow.task_states[DAG_RUN_ID] = [
            {"task_id": "freeze", "state": "success"},
            {"task_id": "discover", "state": "success"},
            {"task_id": "capture", "state": "failed"},
            {"task_id": "generate", "state": "upstream_failed"},
        ]

    def test_freeze_successor_creates_one_current_generation_request(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        body = {
            "stage": "freeze",
            "target_hardware": "m3.1",
            "change_summary": "Recreate\r\ncurrent generation",
            "main_assembly": "parts/arm.SLDASM",
        }
        status, _, response = self.client.request(
            "POST",
            f"/api/runs/{DAG_RUN_ID}/attempts",
            body,
        )
        self.assertEqual(status, 202, response)
        first = json.loads(response)
        child = first["dag_run_id"]
        self.assertTrue(child.startswith("portal-freeze-"))
        self.assertEqual(first["parent_dag_run_id"], DAG_RUN_ID)
        self.assertEqual(first["resume_from"], "freeze")
        successor = self.production_application.freeze_successor_requests[-1]
        self.assertEqual(successor.parent_run_id, native_run_id(DAG_RUN_ID))
        self.assertEqual(successor.main_assembly, "parts/arm.SLDASM")
        self.assertEqual(successor.target_hardware, "m3.1")
        self.assertEqual(successor.change_summary, "Recreate\ncurrent generation")
        self.assertEqual(successor.purpose, "engineering")
        self.assertEqual(successor.principal, FEISHU_PRINCIPAL)
        self.assertEqual(self.platform.admits, [])
        self.assertEqual(len(self.airflow.trigger_payloads), 1)
        self.assertEqual(
            self.airflow.trigger_payloads[0]["conf"]["run_id"],
            native_run_id(child),
        )

        status, _, response = self.client.request(
            "POST",
            f"/api/runs/{DAG_RUN_ID}/attempts",
            body,
        )
        self.assertEqual(status, 202, response)
        self.assertEqual(json.loads(response)["dag_run_id"], child)
        self.assertEqual(len(self.airflow.trigger_payloads), 1)

    def test_attempt_rejects_non_freeze_resume_and_invalid_originals(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        status, _, body = self.client.request(
            "POST",
            f"/api/runs/{DAG_RUN_ID}/attempts",
            {"stage": "generate"},
        )
        self.assertEqual(status, 400, body)
        self.assertIn("start from Freeze", json.loads(body)["error"])
        status, _, body = self.client.request(
            "POST",
            f"/api/runs/{DAG_RUN_ID}/attempts",
            {"stage": "freeze", "main_assembly": "missing.SLDASM"},
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual(self.production_application.freeze_successor_requests, [])

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

    def test_retry_uses_canonical_evidence_without_a_native_job(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        self.endpoint_server.jobs.clear()
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        self.assertEqual(
            json.loads(body)["retry"],
            {
                "eligible": True,
                "reason": "sealed_evidence_reimport",
                "classification": "evidence_corruption",
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
        admitted = self.production_application.admission_requests[-1]
        self.assertEqual(admitted.main_assembly, "model.SLDASM")
        self.assertEqual(admitted.target_hardware, TEST_TARGET)
        self.assertEqual(admitted.change_summary, TEST_SUMMARY)
        self.assertEqual(admitted.purpose, "engineering")
        expected_conf = {
            "schema_version": AIRFLOW_DISPATCH_CONF_SCHEMA,
            "run_id": native_run_id(dag_run_id),
            "freeze_command_id": next(
                iter(self.production_application.dispatch_records.values())
            )[1].freeze_command_id,
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
        self.assertIsNone(listed["handoff_path"])
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
        self.assertEqual(dispatch["stage"], "freeze")
        self.assertEqual(dispatch["operation_key"], f"portal-{dag_run_id}/freeze:0")
        self.assertEqual(dispatch["dag_run_id"], dag_run_id)
        self.assertEqual(dispatch["dag_id"], DEFAULT_DAG_ID)
        self.assertRegex(dispatch["params_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(self.airflow.trigger_payloads), 1)
        self.assertEqual(self.airflow.trigger_payloads[0]["dag_run_id"], dag_run_id)
        self.assertEqual(
            self.airflow.trigger_payloads[0]["conf"]["run_id"],
            native_run_id(dag_run_id),
        )
        recorded_conf = next(
            iter(self.production_application.dispatch_records.values())
        )[1]
        self.assertEqual(
            self.airflow.trigger_payloads[0]["conf"]["freeze_command_id"],
            recorded_conf.freeze_command_id,
        )

    def test_production_admission_reuses_one_idempotent_dispatch(self) -> None:
        from types import SimpleNamespace

        calls = []

        class ProductionApplication:
            outcome = None

            def __init__(self):
                self.dispatched = set()

            def load_admission_outcome(self, run_id):
                if self.outcome is not None and self.outcome.admission.run_id == run_id:
                    return self.outcome
                return None

            def dispatch(self, command_id, adapter):
                command = self.outcome.dispatch_command
                if command_id not in self.dispatched:
                    adapter.trigger(
                        command,
                        DispatchConf(
                            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
                            run_id=command.run_id,
                            freeze_command_id="freeze:" + command.run_id,
                        ),
                    )
                    self.dispatched.add(command_id)
                return SimpleNamespace(state=EffectState.SATISFIED)

        production_application = ProductionApplication()

        def load(request):
            calls.append(request)
            dag_run_id = request.request_id.removeprefix("portal-")
            outcome = SimpleNamespace(
                admission=SimpleNamespace(
                    run_id=request.run_id,
                    request_id=request.request_id,
                ),
                dispatch_command=SimpleNamespace(
                    command_id="dispatch:" + request.run_id,
                    run_id=request.run_id,
                    dag_id=DEFAULT_DAG_ID,
                    dag_run_id=dag_run_id,
                ),
            )
            production_application.outcome = outcome
            return outcome

        server = self._start_extra_portal(
            production_admission_loader=load,
            production_application=production_application,
        )
        client = PortalClient(f"http://127.0.0.1:{server.server_address[1]}")
        client.login()
        body = _upload_body(
            files=[("model.SLDASM", b"<assembly/>")],
            fields=list(UPLOAD_CHOICES),
        )
        headers = {
            "Idempotency-Key": "submission-20261025-stable-0001",
        }

        status, _, response = client.request_raw(
            "POST",
            "/api/runs",
            body,
            "multipart/form-data; boundary=portal-boundary",
            headers,
        )
        self.assertEqual(status, 201, response)
        first = json.loads(response)
        self.assertEqual(
            first["dag_run_id"],
            "portal-submission-20261025-stable-0001",
        )
        self.assertEqual(first["request_id"], f"portal-{first['dag_run_id']}")
        self.assertFalse(first["idempotent"])

        status, _, response = client.request_raw(
            "POST",
            "/api/runs",
            body,
            "multipart/form-data; boundary=portal-boundary",
            headers,
        )
        self.assertEqual(status, 201, response)
        replay = json.loads(response)
        self.assertEqual(replay["dag_run_id"], first["dag_run_id"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.platform.admits, [])
        self.assertEqual(len(self.airflow.trigger_payloads), 1)
        self.assertEqual(
            [path.name for path in self.upload_root.iterdir() if path.name != ".staging"],
            [first["dag_run_id"]],
        )

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
        self.platform.admit_error = FactStoreError("the gate closed mid-upload")
        files = [("model.SLDASM", b"<assembly/>")]
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(files=files, fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 503, body)
        self.assertEqual("Submission admission was refused", json.loads(body)["error"])
        self.assertEqual(self.platform.admits, [])
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual([entry.name for entry in self.upload_root.iterdir() if entry.name != ".staging"], [])

    def test_an_unavailable_platform_refuses_the_submission(self) -> None:
        self.client.login()
        self.platform.read_error = FactStoreError("PLATFORM_SERVICE_DSN is not configured")
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
        self.assertEqual(
            self.production_application.admission_requests[-1].change_summary,
            stored,
        )
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
        admitted = self.production_application.admission_requests[-1]
        self.assertEqual(
            (
                admitted.main_assembly,
                admitted.purpose,
                admitted.target_hardware,
                admitted.change_summary,
            ),
            (
                "model.SLDASM",
                "engineering",
                TEST_TARGET,
                TEST_SUMMARY,
            ),
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
        admitted = self.production_application.admission_requests[-1]
        self.assertEqual(
            admitted.main_assembly,
            "parts/arm.SLDASM",
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
        self.assertEqual(
            self.production_application.admission_requests[-1].main_assembly,
            "子装配/révision.SLDASM",
        )

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
        # (b) The run was not created: the sent effect stays UNKNOWN and is never resent.
        self.airflow.trigger_fail_after_create = False
        self.airflow.trigger_fail_times = 1
        triggers_before = len(self.airflow.trigger_payloads)
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 502, body)
        second = json.loads(body)["dag_run_id"]
        self.assertNotIn(second, self.airflow.dag_runs)
        self.assertEqual(len(self.airflow.trigger_payloads), triggers_before + 1)
        # (c) Repeated unavailable observations still never add a blind POST.
        self.airflow.trigger_fail_times = 10
        triggers_before = len(self.airflow.trigger_payloads)
        status, _, body = self.client.request_raw(
            "POST",
            "/api/runs",
            _upload_body(fields=list(UPLOAD_CHOICES)),
            "multipart/form-data; boundary=portal-boundary",
        )
        self.assertEqual(status, 502, body)
        payload = json.loads(body)
        self.assertEqual(payload["trigger"], "unknown")
        self.assertIn("do not submit again", payload["error"])
        self.assertEqual(len(self.airflow.trigger_payloads), triggers_before + 1)
        unresolved = payload["dag_run_id"]
        self.assertNotIn(unresolved, self.airflow.dag_runs)
        self.assertTrue((self.upload_root / unresolved / "机器人工程" / "model.SLDASM").is_file())

    def test_upload_without_configured_intake_root_fails_closed(self) -> None:
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url),
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
        self.assertTrue(any("Interference and clearance" in text for text in coverage["automatic"]["unsupported"]))
        self.assertEqual(payload["pr"]["url"], "https://github.com/example/m3.0/pull/1")
        self.assertEqual(
            [task["task_id"] for task in payload["tasks"]],
            ["freeze", "discover", "capture", "generate", "verify", "publish"],
        )
        self.assertEqual(payload["job"]["status"], "passed")

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

    def test_edited_quality_is_not_a_preview_authority(self) -> None:
        """Quality bytes are display evidence; the Verify certificate owns preview state."""

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
        # Replaced before the first read: the certificate-backed preview remains
        # available, while the mismatched report download is refused.
        self.store.runs[run_id]["artifacts"]["reports/quality.json"] = b'{"checks":[]}'
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["failing_checks_total"], 1)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/reports/quality.json")
        self.assertEqual(status, 502, body)

    def test_diagnostic_verify_pass_is_not_a_formal_preview_or_checkpoint(self) -> None:
        self.client.login()
        self._seed_airflow_run(state="success")
        self._seed_job(status="passed", result=self._passed_result())
        run_id = native_run_id(DAG_RUN_ID)
        self._register_attempt(
            run_id,
            {
                "urdf/robot.urdf": b"<robot name='fixture'/>",
                "reports/quality.json": b'{"passed":true,"checks":[]}\n',
            },
            verified=True,
        )
        self.store.runs[run_id]["scope"] = {
            "kind": "diagnostic",
            "target_stage": "verify",
        }

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")

        self.assertEqual(status, 200, body)
        preview = json.loads(body)
        self.assertFalse(preview["verified"])
        self.assertEqual(preview["state"], "unverified")
        self.assertEqual(preview["reason"], "diagnostic_scope")
        self.assertTrue(preview["mechanical_passed"])
        self.assertEqual(preview["verification_scope"], "diagnostic")
        self.assertFalse(preview["formal_eligible"])
        self.assertIn("not eligible for a formal checkpoint", preview["error"])

    def test_formal_verify_pass_without_qualification_is_refused(self) -> None:
        self.client.login()
        self._seed_airflow_run(state="success")
        self._seed_job(status="passed", result=self._passed_result())
        run_id = native_run_id(DAG_RUN_ID)
        self._register_attempt(
            run_id,
            {
                "urdf/robot.urdf": b"<robot name='fixture'/>",
                "reports/quality.json": b'{"passed":true,"checks":[]}\n',
            },
            verified=True,
        )
        self.store.runs[run_id]["qualification_overrides"] = {
            "verify": {
                "scope_kind": "formal",
                "formal_eligible": False,
            }
        }

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")

        self.assertEqual(status, 409, body)
        self.assertIn("no formal qualification", json.loads(body)["error"])

    def test_certificate_cannot_name_a_different_generate_artifact(self) -> None:
        self.client.login()
        self._seed_airflow_run(state="success")
        self._seed_job(status="passed", result=self._passed_result())
        run_id = native_run_id(DAG_RUN_ID)
        self._register_attempt(
            run_id,
            {
                "urdf/robot.urdf": b"<robot name='fixture'/>",
                "reports/quality.json": b'{"passed":true,"checks":[]}\n',
            },
            verified=True,
        )
        projection = self.platform.fact_projection(run_id)
        certificate_member = projection["by_stage"]["verify"]["verification-certificate.json"]
        certificate = json.loads(
            self.platform.projection_artifacts.objects[
                certificate_member["artifact"]["artifact_id"]
            ]["verification-certificate.json"]
        )
        self.store.runs[run_id]["certificate_override"] = {
            "generate_artifact": certificate["capture_artifact"],
        }

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")

        self.assertEqual(status, 409, body)
        self.assertIn("another Generate/Capture pair", json.loads(body)["error"])

    def test_conflicting_effect_rejects_current_formal_status_and_preview(self) -> None:
        self.client.login()
        self._seed_airflow_run(state="success")
        self._seed_job(status="passed", result=self._passed_result())
        run_id = native_run_id(DAG_RUN_ID)
        self._register_attempt(
            run_id,
            {
                "urdf/robot.urdf": b"<robot name='fixture'/>",
                "reports/quality.json": b'{"passed":true,"checks":[]}\n',
            },
            verified=True,
        )
        self.store.runs[run_id]["effect_states"] = [
            {
                "effect_id": "effect-after-verify",
                "command_id": f"{run_id}-verify",
                "effect_key": "late-observation",
                "effect_kind": "native_operation",
                "state": "conflict",
            }
        ]

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        run = json.loads(body)
        self.assertEqual(run["state"], "unresolved")
        self.assertFalse(run["formal_eligible"])
        self.assertEqual(run["automatic"]["state"], "unverified")

        status, _, body = self.client.request(
            "GET",
            f"/api/runs/{DAG_RUN_ID}/preview",
        )
        self.assertEqual(status, 200, body)
        preview = json.loads(body)
        self.assertFalse(preview["verified"])
        self.assertFalse(preview["formal_eligible"])
        self.assertEqual(preview["reason"], "run_unresolved")
        self.assertIn("unresolved external effects", preview["error"])

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

    def test_registered_stage_report_parse_defects_refuse_the_run_page(self) -> None:
        """A malformed registered stage report cannot silently become zero checks."""

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
        route = f"/api/runs/{DAG_RUN_ID}/artifacts/reports/native-stages.json"
        for payload in (b"{", b"[]", b'{"passed":false,"checks":"invalid"}'):
            with self.subTest(payload=payload):
                self._rebind_attempt_member(run_id, "reports/native-stages.json", payload)
                status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
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

    def test_run_page_uses_registered_stage_states_and_bound_native_checks_for_7b_shape(self) -> None:
        """A completed platform row and its sealed checks survive a bare event stream."""

        self.client.login()
        run_id = self._seed_7b_stage_display()
        self.assertEqual(run_id, native_run_id(DAG_RUN_ID))
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["job"]["status"], "failed")

        stages = {stage["id"]: stage for stage in payload["stage_view"]["stages"]}
        self.assertEqual(stages["freeze"]["state"], "completed")
        self.assertEqual(stages["freeze"]["checks_passed"], 2)
        self.assertEqual(stages["freeze"]["checks_total"], 2)
        self.assertEqual(stages["discover"]["state"], "completed")
        self.assertEqual(stages["discover"]["checks_passed"], 3)
        self.assertEqual(stages["discover"]["checks_total"], 3)
        self.assertTrue(
            all(row["state"] == "passed" for row in stages["discover"]["input_qc"] + stages["discover"]["output_qc"])
        )
        self.assertEqual(stages["capture"]["state"], "failed")
        for downstream in ("generate", "verify", "publish"):
            with self.subTest(stage=downstream):
                self.assertEqual(stages[downstream]["state"], "blocked")

        report = payload["report"]
        report_stages = {stage["id"]: stage for stage in report["stages"]}
        self.assertEqual(report_stages["discover"]["state"], "completed")
        self.assertEqual(
            report_stages["discover"]["counts"]["boundary"],
            {"passed": 3, "executed": 3, "total": 3},
        )
        self.assertEqual(report_stages["capture"]["state"], "failed")
        for downstream in ("generate", "verify", "publish"):
            with self.subTest(report_stage=downstream):
                self.assertEqual(report_stages[downstream]["state"], "blocked")
        self.assertEqual(report["failure"]["stage"], "capture")
        self.assertEqual(report["failure"]["error_code"], "cad_link_group_payload_invalid")
        self.assertEqual(report["failure"]["title"], "Native link-group mass read rejected")
        self.assertEqual(report["overall"]["headline"], "Capture evidence stage failed; downstream stages did not run")

    def test_run_page_refuses_a_stage_report_bound_to_another_run(self) -> None:
        self.client.login()
        run_id = self._seed_7b_stage_display()
        entry = self.store.runs[run_id]
        report = json.loads(entry["artifacts"]["reports/native-stages.json"])
        report["run_id"] = "00000000-0000-0000-0000-000000000000"
        payload = json.dumps(report, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        entry["artifacts"]["reports/native-stages.json"] = payload
        entry["preview"]["report_files"]["reports/native-stages.json"] = digest
        for stage in ("discover",):
            member = self.platform.stages[run_id][stage]["receipt"]["artifacts"][0]
            member.update(bytes=len(payload), sha256=digest)

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")

        self.assertEqual(status, 502, body)
        self.assertIn("conflicts with its run", json.loads(body)["error"])

    def test_run_page_refuses_a_digest_mismatched_stage_report(self) -> None:
        self.client.login()
        run_id = self._seed_7b_stage_display()
        self.store.runs[run_id]["artifacts"]["reports/native-stages.json"] = b'{"tampered":true}'

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")

        self.assertEqual(status, 502, body)
        self.assertIn("digest verification failed", json.loads(body)["error"])

    def test_run_page_keeps_the_registered_failure_before_native_settlement(self) -> None:
        self.client.login()
        run_id = self._seed_7b_stage_display()
        self.platform.stages[run_id] = {
            stage: self.platform.stages[run_id][stage] for stage in ("freeze", "discover")
        }
        self.endpoint_server.jobs[run_id]["events"] = []

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")

        self.assertEqual(status, 200, body)
        report = json.loads(body)["report"]
        self.assertEqual(report["overall"]["state"], "failed")
        self.assertEqual(report["overall"]["headline"], "Job failed; see the raw diagnostic")
        self.assertEqual(
            {stage["id"]: stage["state"] for stage in report["stages"]},
            {
                "freeze": "completed",
                "discover": "completed",
                "capture": "not_run",
                "generate": "not_run",
                "verify": "not_run",
                "publish": "not_run",
            },
        )

    def test_run_page_refuses_a_later_malformed_known_check_state(self) -> None:
        self.client.login()
        run_id = self._seed_7b_stage_display()
        self.store.runs[run_id]["merged"]["events"].append(
            {
                "stage": "freeze",
                "state": "running",
                "check": {
                    "id": "handoff.admission",
                    "boundary": "input",
                    "state": "not_run",
                    "details": {"handoff_sha256": "d" * 64},
                },
            }
        )

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")

        self.assertEqual(status, 502, body)
        self.assertIn("check observation is malformed", json.loads(body)["error"])

    def test_run_page_refuses_later_malformed_known_check_details(self) -> None:
        self.client.login()
        run_id = self._seed_7b_stage_display()
        self.store.runs[run_id]["merged"]["events"].append(
            {
                "stage": "freeze",
                "state": "running",
                "check": {
                    "id": "handoff.integrity",
                    "boundary": "output",
                    "state": "passed",
                    "details": "malformed",
                },
            }
        )

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")

        self.assertEqual(status, 502, body)
        self.assertIn("check observation is malformed", json.loads(body)["error"])

    def test_run_page_uses_the_latest_registered_check_state(self) -> None:
        self.client.login()
        run_id = self._seed_7b_stage_display()
        report = json.loads(self.store.runs[run_id]["artifacts"]["reports/native-stages.json"])
        freeze = next(row for row in report["stages"] if row["id"] == "freeze")
        admission = next(
            row for row in freeze["input_qc"] if row["id"] == "handoff.admission"
        )
        admission["state"] = "failed"
        admission["details"] = {"error": "later registered failure"}
        payload = json.dumps(report, sort_keys=True).encode("utf-8")
        self._rebind_attempt_member(run_id, "reports/native-stages.json", payload)

        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")

        self.assertEqual(status, 200, body)
        stages = {stage["id"]: stage for stage in json.loads(body)["stage_view"]["stages"]}
        self.assertEqual(stages["freeze"]["state"], "failed")
        admission = next(row for row in stages["freeze"]["input_qc"] if row["id"] == "handoff.admission")
        self.assertEqual(admission["state"], "failed")
        self.assertEqual(admission["details"], {"error": "later registered failure"})

    def test_preview_waits_for_verification(self) -> None:
        self.client.login()
        self._seed_airflow_run()
        self.store.add(
            native_run_id(DAG_RUN_ID),
            meta={},
            checkpoints=("freeze", "discover"),
            merged={"status": "running"},
        )
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 404)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        automatic = json.loads(body)["automatic"]
        self.assertEqual(automatic["state"], "running")
        self.assertEqual(automatic["job_state"], "running")

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
                    "branch": "work/description/m3.0/0123456789abcdef01234567",
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
                    "branch": "work/description/m3.0/0123456789abcdef01234567",
                    "state": "failed",
                    "error": "git_failed",
                    "commit": "",
                    "repository_slug": "example/m3.0",
                },
            },
            events=protocol_events(subject=subject, failed_stage="publish"),
        )
        # Registered members exist but carry no qualified robot member: the preview refuses.
        self._register_attempt(native_run_id(DAG_RUN_ID), {"meshes/only.stl": b"member\n"}, verified=False)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["automatic"]["state"], "failed")
        self.assertIn("quality is missing or not passed", payload["automatic"]["message"])
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
            token_path.write_text("t" * 40, encoding="utf-8")
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
            with patch(
                "description_pipeline.orchestration.portal._production_admission_runtime",
                return_value=(
                    lambda _request: None,
                    lambda _request: None,
                    object(),
                    object(),
                    None,
                ),
            ):
                config = load_portal_config(config_path)
            self.assertEqual(config.dag_id, "solidworks_to_urdf")
            self.assertEqual(config.host, "127.0.0.1")
            self.assertEqual(config.port, 18780)
            self.assertEqual(config.airflow.base_url, self.airflow.url)
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
            with (
                patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(
                    AirflowApiError,
                    "PLATFORM_CONFIG_SNAPSHOT",
                ),
            ):
                load_portal_config(config_path)

    def test_config_loader_installs_the_production_admission_runtime(self) -> None:
        from types import SimpleNamespace
        from description_pipeline.pipeline.contracts import NativeTargetIdentity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_path = root / "endpoint.token"
            token_path.write_text("t" * 40, encoding="utf-8")
            snapshot = root / "config-snapshot.json"
            snapshot.write_text("{}\n", encoding="utf-8")
            artifacts = root / "artifacts"
            artifacts.mkdir()
            work = root / "work"
            work.mkdir()
            config_path = root / "portal.json"
            config_path.write_text(
                json.dumps(
                    {
                        "airflow": {"url": self.airflow.url},
                        "endpoint": {
                            "url": "http://127.0.0.1:18765",
                            "token_file": str(token_path),
                        },
                        "portal": {"host": "127.0.0.1", "port": 18780},
                    }
                ),
                encoding="utf-8",
            )
            fake_application = SimpleNamespace(artifacts=object())
            native_target = NativeTargetIdentity.create(
                install_id="win-" + "a" * 32,
                pipeline_id="solidworks-to-urdf",
            )
            environment = {
                "PLATFORM_CONFIG_SNAPSHOT": str(snapshot),
                "PLATFORM_ARTIFACT_ROOT": str(artifacts),
                "PLATFORM_SERVICE_DSN": "postgresql://fixture/unused",
                "PIPELINE_WORK_ROOT": str(work),
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch(
                    "description_pipeline.pipeline.application.application_from_environment",
                    return_value=fake_application,
                ) as application_factory,
                patch(
                    "description_pipeline.pipeline.native_target."
                    "load_registered_native_target",
                    return_value=native_target,
                ),
            ):
                config = load_portal_config(config_path)

            application_factory.assert_called_once()
            self.assertEqual(
                application_factory.call_args.kwargs[
                    "native_target_identity"
                ],
                native_target,
            )
            self.assertIs(config.production_application, fake_application)
            self.assertIsNotNone(config.production_native_client)
            self.assertEqual(config.production_config_snapshot, snapshot.resolve())
            self.assertTrue(callable(config.production_admission_loader))
            self.assertTrue(
                callable(config.production_freeze_successor_loader)
            )

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
        from description_pipeline.interface_records import (
            INSTALL_RECORD_SCHEMA,
        )
        from description_pipeline.io import digest, write_json
        from description_pipeline.pipeline.contracts import NativeTargetIdentity
        from tests.v1.test_pipeline_discover import DiscoverHarness

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = DiscoverHarness(root / "fixture")
            token_path = Path(tmp) / "endpoint.token"
            token_path.write_text("t" * 40, encoding="utf-8")
            snapshot = root / "config-snapshot.json"
            snapshot.write_bytes(
                (fixture.configuration / "config-snapshot.json").read_bytes()
            )
            snapshot_sha256 = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            target = NativeTargetIdentity.create(
                install_id="win-" + "a" * 32,
                pipeline_id="solidworks-to-urdf",
            )
            release_source_sha256 = digest({"release": "fixture"})
            release = {
                "version": "1.0.0",
                "source_commit": "a" * 40,
                "source_sha256": release_source_sha256,
                "artifacts": [
                    {
                        "name": "fixture.whl",
                        "sha256": digest({"artifact": "fixture"}),
                    }
                ],
            }
            write_json(
                fixture.state / "install-record.json",
                {
                    "schema_version": INSTALL_RECORD_SCHEMA,
                    "install_id": "install-fixture",
                    "timestamp": "2026-10-25T00:00:00Z",
                    "snapshot": {
                        "path": str(snapshot),
                        "digest": snapshot_sha256,
                        "install_id": "install-fixture",
                    },
                    "release": release,
                    "services": ["description-postgres"],
                    "paths": {"state_root": str(fixture.state)},
                    "dag": {
                        "id": "solidworks_to_urdf",
                        "path": "/srv/description/dags/solidworks_to_urdf.py",
                        "digest": digest({"dag": "fixture"}),
                        "tasks": 6,
                    },
                    "ends": {
                        "linux": {
                            "install_id": "install-fixture",
                            "config_digest": snapshot_sha256,
                            "release": {
                                "version": release["version"],
                                "source_commit": release["source_commit"],
                                "source_sha256": release["source_sha256"],
                                "role": "portable",
                            },
                        },
                        "windows": {
                            "install_id": target.install_id,
                            "config_digest": digest({"windows": "config"}),
                            "release": {
                                "version": release["version"],
                                "source_commit": release["source_commit"],
                                "source_sha256": release["source_sha256"],
                                "role": "native",
                            },
                            "target_identity": target.to_record(),
                        },
                    },
                },
            )
            artifacts = root / "artifacts"
            artifacts.mkdir()
            work = root / "work"
            work.mkdir()
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
            env = dict(
                os.environ,
                PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
                PLATFORM_CONFIG_SNAPSHOT=str(snapshot),
                PLATFORM_ARTIFACT_ROOT=str(artifacts),
                PLATFORM_SERVICE_DSN="postgresql://fixture/unused",
                PIPELINE_WORK_ROOT=str(work),
            )
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
    def test_attempt_endpoint_rejects_old_downstream_resume(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        for stage_id in ("discover", "capture", "generate", "verify", "publish"):
            with self.subTest(stage=stage_id):
                status, _, body = self.client.request(
                    "POST",
                    f"/api/runs/{DAG_RUN_ID}/attempts",
                    {"stage": stage_id},
                )
                self.assertEqual(status, 400, body)
                self.assertIn("start from Freeze", json.loads(body)["error"])
        self.assertEqual(self.airflow.trigger_payloads, [])
        self.assertEqual(self.platform.admits, [])
        self.assertEqual(
            self.production_application.freeze_successor_requests,
            [],
        )

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
        child_run = native_run_id(child)
        self.store.add(child_run, meta={})
        self.store.runs[child_run].update(
            parent_run_id=native_run_id(DAG_RUN_ID),
            request_id=f"portal-{child}",
            from_stage="verify",
            admission_request={
                "change_summary": TEST_SUMMARY,
                "main_assembly": "model.SLDASM",
                "purpose": "engineering",
                "target_hardware": TEST_TARGET,
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

    def test_run_detail_exposes_only_a_freeze_successor_when_facts_exist(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        # Without canonical admission facts the key is omitted.
        self.store.runs.pop(native_run_id(DAG_RUN_ID))
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        self.assertNotIn("stage_reruns", json.loads(body))
        # A completed Freeze exposes one new-request option derived from sealed facts.
        self._seed_retryable_run()
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        rows = json.loads(body)["stage_reruns"]
        self.assertEqual([row["stage"] for row in rows], ["freeze"])
        self.assertTrue(rows[0]["eligible"])
        self.assertEqual(
            rows[0]["main_assembly_options"],
            ["model.SLDASM", "parts/arm.SLDASM"],
        )

if __name__ == "__main__":
    unittest.main()
