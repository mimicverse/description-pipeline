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
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from socketserver import ThreadingMixIn
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
    AirflowApi,
    AirflowApiError,
    PortalApp,
    PortalConfig,
    load_portal_config,
)
from description_pipeline.orchestration.uploads import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES
from description_pipeline.stages import CONTRACT
from tests.v1.test_airflow_client import RUN_ID, MockEndpoint

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
_DEFAULT_ACTOR = object()


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
                                        {"task_id": "resolve_handoff", "state": "success"},
                                        {"task_id": "start_job", "state": "success"},
                                        {"task_id": "wait_for_job", "state": "success"},
                                        {"task_id": "confirm_job", "state": "success"},
                                    ],
                                ),
                                "total_entries": 4,
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
        self.opener = urlrequest.build_opener(urlrequest.HTTPCookieProcessor(self.jar))
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
    artifacts = {
        "urdf/robot.urdf": b"<robot name='fixture'/>",
        "meshes/base.stl": b"solid base",
        "meshes/a b.stl": b"solid spaced",
    }
    return {name: hashlib.sha256(data).hexdigest() for name, data in artifacts.items()}


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
) -> bytes:
    """One browser-shaped multipart folder body (top folder included, ``/`` separators)."""
    entries = files or [("model.SLDASM", b"<assembly/>"), ("parts/p1.SLDPRT", b"<part1/>")]
    chunks = []
    for rel, data in entries:
        chunks.append(b"--portal-boundary\r\n")
        chunks.append(
            b'Content-Disposition: form-data; name="files"; filename="' + f"{folder}/{rel}".encode() + b'"\r\n'
        )
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(data + b"\r\n")
    chunks.append(b"--portal-boundary--\r\n")
    return b"".join(chunks)


class PortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.airflow = MockAirflow()
        self.airflow.__enter__()
        self.addCleanup(self.airflow.__exit__, None, None, None)
        self.upload_root = Path(tempfile.mkdtemp(prefix="portal-upload-"))
        self.addCleanup(shutil.rmtree, self.upload_root, ignore_errors=True)
        self.endpoint_server = MockEndpoint(
            preview_payload=_preview_payload(),
        )
        self.endpoint_server.__enter__()
        self.addCleanup(self.endpoint_server.__exit__, None, None, None)
        self.endpoint = WindowsEndpoint(EndpointConfig(base_url=self.endpoint_server.url, token="test-token"))
        self.static_dir = Path(__file__).resolve().parents[2] / "src/description_pipeline/orchestration/static"
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url),
            endpoint=lambda: self.endpoint,
            static_dir=self.static_dir,
            upload_root=self.upload_root,
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
        self.endpoint.start_job(run_id=run_id, resolution=self.endpoint.resolve_handoff("handoff/m3.0"))
        self.endpoint.get_job(run_id)
        self.endpoint.get_job(run_id)
        return dag_run_id

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
            "conf": {"handoff_path": "/srv/robot-cell"} if conf is None else conf,
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
        self.airflow.dag_runs[DAG_RUN_ID]["state"] = "failed"
        self.airflow.task_states[DAG_RUN_ID] = [
            {"task_id": "resolve_handoff", "state": "success"},
            {"task_id": "start_job", "state": "success"},
            {"task_id": "wait_for_job", "state": "failed"},
            {"task_id": "confirm_job", "state": "upstream_failed"},
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
                self.assertEqual(detail["retry"], {"eligible": True, "reason": "transport_recovery"})
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
                    {"dag_run_id": DAG_RUN_ID, "state": "queued", "cleared_tasks": ["confirm_job", "wait_for_job"]},
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
        self.endpoint_server.fail_job = True
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["retry"], {"eligible": False, "reason": "native_terminal_failure"})
        self.assertEqual(self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})[0], 409)
        with patch.object(self.endpoint, "get_job", side_effect=EndpointError("offline")):
            status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["retry"], {"eligible": False, "reason": "endpoint_evidence_unavailable"})
            self.assertEqual(self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})[0], 409)
        self.assertEqual(self.airflow.clear_requests, [])

    def test_retry_refuses_unsafe_or_changed_dry_run_task_selection(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        for selected in [
            [{"task_id": "resolve_handoff", "state": "failed"}],
            [{"task_id": "wait_for_job", "state": "success"}],
            [{"task_id": "wait_for_job", "state": "failed"}],
            [{"task_id": "wait_for_job", "state": "failed", "dag_run_id": "another-run"}],
            [{"task_id": "wait_for_job", "state": "failed", "map_index": 0}],
            [{"task_id": "wait_for_job", "state": "failed"}] * 2,
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
        self.assertEqual(json.loads(body)["retry"], {"eligible": False, "reason": "endpoint_evidence_unavailable"})
        self.airflow.task_states[DAG_RUN_ID][1]["state"] = "failed"
        self.airflow.task_states[DAG_RUN_ID][2]["state"] = "upstream_failed"
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/retry", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["cleared_tasks"], ["confirm_job", "start_job", "wait_for_job"])
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
            "POST", "/api/runs", _upload_body(files=files), "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 201, body)
        payload = json.loads(body)
        dag_run_id = payload["dag_run_id"]
        self.assertTrue(dag_run_id.startswith("portal-"))
        self.assertEqual(payload["folder"], "机器人工程")
        self.assertEqual(payload["files"], 2)
        self.assertEqual(payload["bytes"], sum(len(data) for _, data in files))
        stored = self.upload_root / dag_run_id / "机器人工程"
        self.assertTrue((stored / "model.SLDASM").is_file())
        self.assertEqual(self.airflow.conf, {"handoff_path": str(stored)})
        self.assertEqual(
            self.airflow.trigger_payloads[-1],
            {"dag_run_id": dag_run_id, "logical_date": None, "conf": {"handoff_path": str(stored)}},
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
            "POST", "/api/runs", _upload_body(), "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 201, body)
        first = json.loads(body)["dag_run_id"]
        self.assertEqual(list(self.airflow.dag_runs), [first])
        self.assertEqual(len(self.airflow.trigger_payloads), 1)
        # (b) The run was not created: one reconcile + one retry with the same id.
        self.airflow.trigger_fail_after_create = False
        self.airflow.trigger_fail_times = 1
        status, _, body = self.client.request_raw(
            "POST", "/api/runs", _upload_body(), "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 201, body)
        second = json.loads(body)["dag_run_id"]
        self.assertIn(second, self.airflow.dag_runs)
        self.assertEqual(len(self.airflow.dag_runs), 2)
        # (c) Never confirmed: 502 carries the run id, the upload stays, and no run exists.
        self.airflow.trigger_fail_times = 10
        status, _, body = self.client.request_raw(
            "POST", "/api/runs", _upload_body(), "multipart/form-data; boundary=portal-boundary"
        )
        self.assertEqual(status, 502, body)
        payload = json.loads(body)
        self.assertEqual(payload["trigger"], "absent")
        self.assertIn("不要重复提交", payload["error"])
        unresolved = payload["dag_run_id"]
        self.assertNotIn(unresolved, self.airflow.dag_runs)
        self.assertTrue((self.upload_root / unresolved / "机器人工程" / "model.SLDASM").is_file())

    def test_upload_without_configured_intake_root_fails_closed(self) -> None:
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url), endpoint=lambda: self.endpoint, static_dir=self.static_dir
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
            "POST", "/api/runs", _upload_body(), "multipart/form-data; boundary=portal-boundary"
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
        self.assertTrue(any("干涉与间隙" in text for text in coverage["automatic"]["unsupported"]))
        self.assertEqual(payload["pr"]["url"], "https://github.com/example/m3.0/pull/1")
        self.assertEqual(
            [task["task_id"] for task in payload["tasks"]],
            ["resolve_handoff", "start_job", "wait_for_job", "confirm_job"],
        )
        self.assertEqual(payload["job"]["status"], "passed")

    def test_failed_job_reports_findings_and_no_success(self) -> None:
        self.client.login()
        self._seed_airflow_run(state="failed")
        run_id = native_run_id(DAG_RUN_ID)
        with MockEndpoint(fail_job=True) as failing:
            failing_endpoint = WindowsEndpoint(EndpointConfig(base_url=failing.url, token="test-token"))
            failing_endpoint.start_job(run_id=run_id, resolution=failing_endpoint.resolve_handoff("handoff/m3.0"))
            failing_endpoint.get_job(run_id)
            self.endpoint = failing_endpoint
            status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
            self.assertEqual(status, 200, body)
            payload = json.loads(body)
            self.assertEqual(payload["automatic"]["state"], "failed")
            self.assertEqual(payload["report"]["overall"]["state"], "failed")
            self.assertEqual(payload["report"]["failure"]["stage"], "discover")
            blocked = next(stage for stage in payload["report"]["stages"] if stage["id"] == "capture")
            self.assertEqual(blocked["state_zh"], "上游阶段失败（未执行）")
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
        self.endpoint_server.artifacts["urdf/robot.urdf"] = b"tampered"
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/artifacts/urdf/robot.urdf")
        self.assertEqual(status, 502)
        self.assertNotIn(b"tampered", body)

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
        run_id = native_run_id(DAG_RUN_ID)
        self.endpoint.start_job(run_id=run_id, resolution=self.endpoint.resolve_handoff("handoff/m3.0"))
        status, _, _ = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}/preview")
        self.assertEqual(status, 404)
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["automatic"]["state"], "passed")

    def test_verified_model_without_pull_request_is_still_previewable(self) -> None:
        self.client.login()
        self._seed_airflow_run(state="failed")
        with MockEndpoint(omit_submission=True, preview_payload=_preview_payload()) as server:
            endpoint = WindowsEndpoint(EndpointConfig(base_url=server.url, token="test-token"))
            run_id = native_run_id(DAG_RUN_ID)
            endpoint.start_job(run_id=run_id, resolution=endpoint.resolve_handoff("handoff/m3.0"))
            endpoint.get_job(run_id)
            endpoint.get_job(run_id)
            self.endpoint = endpoint
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
        with MockEndpoint(publish_failed=True, preview_payload=_preview_payload()) as server:
            endpoint = WindowsEndpoint(EndpointConfig(base_url=server.url, token="test-token"))
            run_id = native_run_id(DAG_RUN_ID)
            endpoint.start_job(run_id=run_id, resolution=endpoint.resolve_handoff("handoff/m3.0"))
            endpoint.get_job(run_id)
            self.endpoint = endpoint
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
        with MockEndpoint(publish_failed=True, quality_mismatch=True, preview_payload=_preview_payload()) as server:
            endpoint = WindowsEndpoint(EndpointConfig(base_url=server.url, token="test-token"))
            run_id = native_run_id(DAG_RUN_ID)
            endpoint.start_job(run_id=run_id, resolution=endpoint.resolve_handoff("handoff/m3.0"))
            endpoint.get_job(run_id)
            self.endpoint = endpoint
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
            "freeze": "冻结输入",
            "discover": "解析结构",
            "capture": "采集证据",
            "generate": "生成 URDF",
            "verify": "独立验证",
            "publish": "提交评审 PR",
        }
        chosen = overrides or {}
        rows = []
        for stage, name in names.items():
            eligible, reason, earliest = chosen.get(stage, (True, "ok", None))
            rows.append(
                {
                    "stage": stage,
                    "name_zh": name,
                    "eligible": eligible,
                    "reason": reason,
                    "reason_zh": "" if eligible else "上游检查点不可复用，请从更早步骤重新执行",
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
        for body in ({"stage": "bogus"}, {}, {"stage": "generate", "handoff_path": "/elsewhere"}):
            with self.subTest(body=body):
                status, _, _ = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", body)
                self.assertEqual(status, 400)

    def test_rerun_attempt_creates_linked_child_from_authoritative_parent(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows()
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
        self.assertEqual(payload["resume_from_name_zh"], "生成 URDF")
        self.assertEqual(payload["state"], "queued")
        self.assertEqual(self.airflow.trigger_payloads[-1]["dag_run_id"], child)
        self.assertEqual(
            self.airflow.trigger_payloads[-1]["conf"],
            {"handoff_path": "/srv/robot-cell", "parent_dag_run_id": DAG_RUN_ID, "resume_from": "generate"},
        )
        # The original run, its conf and the native job stay immutable.
        self.assertEqual(self.airflow.dag_runs[DAG_RUN_ID]["conf"], parent_conf)
        self.assertEqual(self.airflow.dag_runs[DAG_RUN_ID]["state"], "failed")
        self.assertEqual(set(self.endpoint_server.jobs), jobs_before)

    def test_rerun_attempt_same_stage_is_idempotent_and_other_stage_conflicts(self) -> None:
        self._seed_retryable_run()
        child = "portal-20261009T000000-deadbeef"
        self._seed_airflow_run(
            child,
            state="queued",
            conf={"handoff_path": "/srv/robot-cell", "parent_dag_run_id": DAG_RUN_ID, "resume_from": "generate"},
        )
        self.client.login()
        rows = self._rerun_rows()
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            before = len(self.airflow.trigger_payloads)
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
            self.assertEqual(status, 202, body)
            self.assertEqual(json.loads(body)["dag_run_id"], child)
            self.assertEqual(len(self.airflow.trigger_payloads), before)
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "capture"})
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertEqual(payload["reason"], "already_active")
        self.assertEqual(payload["active_dag_run_id"], child)
        self.assertTrue(payload["reason_zh"])

    def test_rerun_attempt_refuses_ineligible_stage_with_earliest_required(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows({"verify": (False, "dependency_changed", "discover")})
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "verify"})
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertEqual(payload["reason"], "dependency_changed")
        self.assertEqual(payload["earliest_required"], "discover")
        self.assertTrue(payload["reason_zh"])
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_rerun_attempt_without_planner_refuses_without_creating(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        with patch.object(self.endpoint, "rerun_plan", create=True, side_effect=EndpointError("planner offline")):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "endpoint_evidence_unavailable")
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_rerun_attempt_reconciles_ambiguous_trigger(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows()
        self.airflow.trigger_fail_after_create = True
        self.airflow.trigger_fail_times = 1
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "freeze"})
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
        self.assertEqual(list(self.airflow.dag_runs), [DAG_RUN_ID])

    def test_list_runs_exposes_linked_attempt_lineage(self) -> None:
        self._seed_retryable_run()
        child = "portal-20261009T000000-cafebabe"
        self._seed_airflow_run(
            child,
            state="queued",
            conf={"handoff_path": "/srv/robot-cell", "parent_dag_run_id": DAG_RUN_ID, "resume_from": "verify"},
        )
        self.client.login()
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        runs = {item["dag_run_id"]: item for item in json.loads(body)["runs"]}
        self.assertEqual(runs[child]["parent_dag_run_id"], DAG_RUN_ID)
        self.assertEqual(runs[child]["resume_from"], "verify")
        self.assertEqual(runs[child]["resume_from_name_zh"], "独立验证")
        self.assertIsNone(runs[DAG_RUN_ID]["parent_dag_run_id"])
        self.assertIsNone(runs[DAG_RUN_ID]["resume_from"])

    def test_run_detail_stage_reruns_passthrough_and_omission(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        with patch.object(self.endpoint, "rerun_plan", create=True, side_effect=EndpointError("planner offline")):
            status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        self.assertNotIn("stage_reruns", json.loads(body))
        rows = self._rerun_rows()
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["stage_reruns"], rows)

    def _write_rerun_reservation(self, attempt_id: str, stage: str) -> Path:
        folder = self.upload_root / ".rerun-attempts"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{DAG_RUN_ID}.json"
        path.write_text(
            json.dumps({"attempt_id": attempt_id, "stage": stage, "state": "attempting"}),
            encoding="utf-8",
        )
        return path

    def test_rerun_attempt_scan_covers_runs_beyond_the_old_page_limit(self) -> None:
        self._seed_retryable_run()
        child = "portal-20261005T000000-11112222"
        self._seed_airflow_run(
            child,
            state="queued",
            conf={"handoff_path": "/srv/robot-cell", "parent_dag_run_id": DAG_RUN_ID, "resume_from": "verify"},
        )
        self.airflow.dag_runs[child]["start_date"] = "2026-10-05T00:00:00Z"
        for index in range(25):
            newer = f"portal-20261008T0000{index:02d}-bbbb{index:04d}"
            self._seed_airflow_run(newer, state="success", conf={})
            self.airflow.dag_runs[newer]["start_date"] = f"2026-10-08T00:{index:02d}:00Z"
        self.client.login()
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertEqual(payload["reason"], "already_active")
        self.assertEqual(payload["active_dag_run_id"], child)
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "verify"})
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["dag_run_id"], child)

    def test_rerun_attempt_refuses_when_active_scan_fails(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        with patch.object(AirflowApi, "list_dag_runs", side_effect=AirflowApiError("scan down")):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "unresolved_outcome")
        self.assertEqual(self.airflow.trigger_payloads, [])

    def test_rerun_attempt_scan_cap_refuses_instead_of_failing_open(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        filler = [{"dag_run_id": f"filler-{index}", "conf": {}, "state": "success"} for index in range(100)]
        with patch.object(AirflowApi, "list_dag_runs", return_value=filler):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "unresolved_outcome")
        self.assertEqual(self.airflow.trigger_payloads, [])

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
        self.assertEqual(self.airflow.trigger_payloads[0]["dag_run_id"], child)

    def test_corrupt_rerun_reservation_refuses_without_creating_another_run(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        path = self._write_rerun_reservation("portal-original-reservation", "generate")
        for raw in ("{broken", "{}", '{"attempt_id":"../escape","stage":"generate"}'):
            with self.subTest(raw=raw):
                path.write_text(raw, encoding="utf-8")
                status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
                self.assertEqual(status, 409, body)
                self.assertEqual(json.loads(body)["reason"], "unresolved_outcome")
                self.assertEqual(self.airflow.trigger_payloads, [])
                self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_rerun_attempt_reuses_persisted_reservation_after_restart(self) -> None:
        self._seed_retryable_run()
        self.client.login()
        rows = self._rerun_rows()
        reserved = "portal-20261009T000000-aaaabbbb"
        path = self._write_rerun_reservation(reserved, "generate")
        with patch.object(self.endpoint, "rerun_plan", create=True, return_value={"stage_reruns": rows}):
            status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["dag_run_id"], reserved)
        self.assertEqual(self.airflow.trigger_payloads[-1]["dag_run_id"], reserved)
        self.assertFalse(path.exists())
        self.assertIn(reserved, self.airflow.dag_runs)

    def test_rerun_attempt_reservation_sees_child_created_before_restart(self) -> None:
        self._seed_retryable_run()
        reserved = "portal-20261009T000000-ccccdddd"
        self._seed_airflow_run(
            reserved,
            state="queued",
            conf={"handoff_path": "/srv/robot-cell", "parent_dag_run_id": DAG_RUN_ID, "resume_from": "generate"},
        )
        path = self._write_rerun_reservation(reserved, "generate")
        self.client.login()
        before = len(self.airflow.trigger_payloads)
        status, _, body = self.client.request("POST", f"/api/runs/{DAG_RUN_ID}/attempts", {"stage": "generate"})
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["dag_run_id"], reserved)
        self.assertEqual(len(self.airflow.trigger_payloads), before)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
