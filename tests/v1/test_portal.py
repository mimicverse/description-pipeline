"""End-to-end tests for the operator portal: Airflow auth, one-field start, verified preview."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib import error as urlerror
from urllib import request as urlrequest
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    PIPELINE_ID,
    WindowsEndpoint,
    native_run_id,
)
from description_pipeline.orchestration.portal import (
    AirflowApi,
    AirflowApiError,
    PortalApp,
    PortalConfig,
    load_portal_config,
)
from tests.v1.test_airflow_client import RUN_ID, MockEndpoint

AIRFLOW_TOKEN = "airflow-session-token"
FEISHU_NAME = "崔工"
FEISHU_PRINCIPAL = "cli_app:tenant-a:ou_worker"
DAG_RUN_ID = "portal-20261007T000000-abcdef01"


class MockAirflow:
    """Minimal Airflow 3.3.2 surface: the Feishu profile bridge plus the v2 DAG-run endpoints."""

    def __init__(self) -> None:
        self.dag_runs: dict[str, dict] = {}
        self.conf: dict | None = None
        self.trigger_payloads: list[dict] = []
        self.hits = 0
        self.revoked = False
        self.deny_runs = False
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

            def _authorized(self) -> bool:
                if outer.revoked or self.headers.get("Authorization") != f"Bearer {AIRFLOW_TOKEN}":
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
                    outer.conf = payload.get("conf")
                    outer.dag_runs[dag_run_id] = {
                        "dag_run_id": dag_run_id,
                        "dag_id": "solidworks_to_urdf",
                        "state": "running",
                        "conf": payload.get("conf"),
                        "triggering_user_name": FEISHU_PRINCIPAL,
                        "start_date": "2026-10-07T00:00:00Z",
                        "end_date": None,
                    }
                    self._reply(201, dict(outer.dag_runs[dag_run_id]))
                    return
                self._reply(404, {"detail": "not found"})

            def do_GET(self) -> None:
                outer.hits += 1
                if self.path == "/auth/feishu/profile":
                    if self.headers.get("Cookie") != f"_token={AIRFLOW_TOKEN}":
                        self._reply(401, {"detail": "not_signed_in"})
                        return
                    self._reply(
                        200,
                        {
                            "open_id": "ou_worker",
                            "app_id": "cli_app",
                            "name": FEISHU_NAME,
                            "avatar_url": "https://avatar/u",
                            "tenant_key": "tenant-a",
                            "principal": FEISHU_PRINCIPAL,
                            "role": "OPERATOR",
                        },
                    )
                    return
                if not self._authorized():
                    return
                if self.path == "/api/v2/dags/solidworks_to_urdf":
                    self._reply(200, {"dag_id": "solidworks_to_urdf", "is_paused": False})
                    return
                if self.path.startswith("/api/v2/dags/solidworks_to_urdf/dagRuns?"):
                    runs = sorted(outer.dag_runs.values(), key=lambda run: run.get("start_date") or "", reverse=True)
                    self._reply(200, {"dag_runs": runs, "total_entries": len(runs)})
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
                                "task_instances": [
                                    {"task_id": "resolve_handoff", "state": "success"},
                                    {"task_id": "start_job", "state": "success"},
                                    {"task_id": "wait_for_job", "state": "success"},
                                    {"task_id": "confirm_job", "state": "success"},
                                ],
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

    def login(self) -> None:
        """The browser already holds the Feishu SSO cookie; the portal adopts it server-side."""
        self.jar.set_cookie(_airflow_token_cookie(AIRFLOW_TOKEN))
        status, _, body = self.request("GET", "/api/session")
        assert status == 200, body
        self.login_response = body
        self.csrf = json.loads(body)["csrf_token"]


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


class PortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.airflow = MockAirflow()
        self.airflow.__enter__()
        self.addCleanup(self.airflow.__exit__, None, None, None)
        self.endpoint_server = MockEndpoint(
            preview_payload=_preview_payload(),
        )
        self.endpoint_server.__enter__()
        self.addCleanup(self.endpoint_server.__exit__, None, None, None)
        self.endpoint = WindowsEndpoint(EndpointConfig(base_url=self.endpoint_server.url, token="test-token"))
        self.static_dir = Path(__file__).resolve().parents[2] / "src/description_pipeline/orchestration/static"
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url), endpoint=lambda: self.endpoint, static_dir=self.static_dir
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

    def _seed_passed_job(self, dag_run_id: str = DAG_RUN_ID) -> str:
        self._seed_airflow_run(dag_run_id, state="success")
        run_id = native_run_id(dag_run_id)
        self.endpoint.start_job(run_id=run_id, resolution=self.endpoint.resolve_handoff("handoff/m3.0"))
        self.endpoint.get_job(run_id)
        self.endpoint.get_job(run_id)
        return dag_run_id

    def _seed_airflow_run(self, dag_run_id: str = DAG_RUN_ID, *, state: str = "running") -> None:
        self.airflow.dag_runs[dag_run_id] = {
            "dag_run_id": dag_run_id,
            "dag_id": "solidworks_to_urdf",
            "state": state,
            "conf": {"handoff_path": "/srv/robot-cell"},
            "triggering_user_name": FEISHU_PRINCIPAL,
            "start_date": "2026-10-07T00:00:00Z",
            "end_date": "2026-10-07T00:05:00Z" if state != "running" else None,
        }

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

    def test_start_run_carries_one_folder_field(self) -> None:
        self.client.login()
        status, _, body = self.client.request("POST", "/api/runs", {"handoff_path": "/srv/robot-cell"})
        self.assertEqual(status, 201, body)
        dag_run_id = json.loads(body)["dag_run_id"]
        self.assertTrue(dag_run_id.startswith("portal-"))
        self.assertEqual(self.airflow.conf, {"handoff_path": "/srv/robot-cell"})
        self.assertEqual(
            self.airflow.trigger_payloads[-1],
            {"dag_run_id": dag_run_id, "logical_date": None, "conf": {"handoff_path": "/srv/robot-cell"}},
        )
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 200)
        listed = json.loads(body)["runs"][0]
        self.assertEqual(listed["dag_run_id"], dag_run_id)
        self.assertEqual(listed["handoff_path"], "/srv/robot-cell")
        self.assertEqual(listed["state"], "running")
        self.assertEqual(listed["user"], FEISHU_NAME)
        self.assertEqual(listed["principal"], FEISHU_PRINCIPAL)

    def test_run_identity_survives_a_portal_restart(self) -> None:
        """The operator page reads who triggered a run from Airflow, not from its own memory."""
        self._seed_airflow_run("portal-20261007T010000-abcdefff")
        self.client.login()
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 200, body)
        listed = json.loads(body)["runs"][0]
        self.assertEqual(listed["dag_run_id"], "portal-20261007T010000-abcdefff")
        self.assertEqual(listed["principal"], FEISHU_PRINCIPAL)

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
        status, _, _ = self.client.request("POST", "/api/runs", {"handoff_path": "/srv/robot-cell", "target": "hidden"})
        self.assertEqual(status, 400)
        self.client.csrf = None
        status, _, _ = self.client.request("POST", "/api/runs", {"handoff_path": "/srv/robot-cell"})
        self.assertEqual(status, 403)

    def test_status_separates_automatic_results_from_pending_confirmations(self) -> None:
        self.client.login()
        self._seed_passed_job()
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["automatic"]["state"], "passed")
        coverage = payload["coverage"]
        self.assertEqual(coverage["engineering"]["state"], "pending")
        self.assertEqual(len(coverage["engineering"]["items"]), 16)
        self.assertTrue(all(item["state"] == "pending" for item in coverage["engineering"]["items"]))
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

    def test_engineer_items_stay_pending_even_if_a_job_claims_confirmations(self) -> None:
        self.client.login()
        self._seed_passed_job()
        run_id = native_run_id(DAG_RUN_ID)
        self.endpoint_server.jobs[run_id]["confirmations"] = [{"id": "范围与身份", "state": "confirmed"}]
        status, _, body = self.client.request("GET", f"/api/runs/{DAG_RUN_ID}")
        self.assertEqual(status, 200, body)
        coverage = json.loads(body)["coverage"]
        self.assertTrue(all(item["state"] == "pending" for item in coverage["engineering"]["items"]))

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


if __name__ == "__main__":
    unittest.main()
