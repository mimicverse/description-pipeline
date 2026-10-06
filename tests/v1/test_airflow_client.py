"""Neutral mocked-HTTP tests for the Windows endpoint client (auth, retries, failure semantics)."""

from __future__ import annotations

import json
import io
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from description_pipeline.orchestration.airflow_client import (
    EndpointAuthError,
    EndpointConfig,
    EndpointConflict,
    EndpointError,
    EndpointProtocolError,
    JOB_SCHEMA,
    JobFailed,
    PIPELINE_ID,
    ResultNotPublishable,
    WindowsEndpoint,
    check_result,
    validate_package,
    validate_revision_sha,
    validate_run_id,
)

TOKEN = "test-token"
RUN_ID = "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
SHA = "a" * 64


class MockEndpoint:
    """Minimal stand-in for the Windows runner; state advances on each GET."""

    def __init__(
        self,
        *,
        token: str = TOKEN,
        fail_job: bool = False,
        legacy_events: bool = False,
        omit_quality: bool = False,
        redirect_to: str | None = None,
    ) -> None:
        self.token = token
        self.fail_job = fail_job
        self.legacy_events = legacy_events
        self.omit_quality = omit_quality
        self.redirect_to = redirect_to
        self.hits = 0
        self.jobs: dict[str, dict] = {}
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

            def _authorized(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self) -> None:
                outer.hits += 1
                if self.path == "/health" and outer.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to + "/health")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not self._authorized():
                    return
                if self.path == "/health":
                    self._send(200, {"pipeline_id": PIPELINE_ID, "ready": True})
                    return
                if self.path.startswith("/v1/jobs/"):
                    run_id = self.path.rsplit("/", 1)[-1]
                    job = outer.jobs.get(run_id)
                    if job is None:
                        self._send(404, {"error": "unknown run_id"})
                        return
                    job["pokes"] += 1
                    if outer.fail_job:
                        job["status"] = "failed"
                        job["error"] = "cad capture failed"
                        job["result"] = None
                    elif job["pokes"] >= 2:
                        job["status"] = "passed"
                        result = {
                            "passed": True,
                            "pipeline_id": PIPELINE_ID,
                            "output": "build/out",
                            "subject_sha256": SHA,
                            "submission": {
                                "passed": True,
                                "subject_sha256": SHA,
                                "base": "feature/m3.0",
                                "branch": "work/solidworks/m3.0",
                                "state": "published",
                                "commit": "a" * 40,
                                "repository_slug": "example/m3.0",
                                "url": "https://github.com/example/m3.0/pull/1",
                            },
                        }
                        if not outer.omit_quality:
                            result["quality"] = {"passed": True, "subject_sha256": SHA, "checks": [{"id": "x"}]}
                        job["result"] = result
                    else:
                        job["status"] = "running"
                    event = (
                        {"phase": "job", "state": job["status"], "time": "t0"}
                        if outer.legacy_events
                        else {"stage": "job", "state": job["status"], "at": "t0"}
                    )
                    job["events"].append(event)
                    self._send(200, job)
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                outer.hits += 1
                if not self._authorized():
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path != "/v1/jobs":
                    self._send(404, {"error": "not found"})
                    return
                run_id = payload.get("run_id", "")
                if run_id in outer.jobs:
                    if outer.jobs[run_id]["request"] != payload:
                        self._send(409, {"error": "run_id bound to a different request"})
                        return
                    self._send(200, outer.jobs[run_id])
                    return
                job = {
                    "schema_version": JOB_SCHEMA,
                    "pipeline_id": PIPELINE_ID,
                    "run_id": run_id,
                    "repository_slug": "example/m3.0",
                    "repository_base": "feature/m3.0",
                    "status": "queued",
                    "events": [{"stage": "submit", "state": "queued", "at": "t0"}],
                    "result": None,
                    "error": None,
                    "pokes": 0,
                    "request": payload,
                }
                outer.jobs[run_id] = job
                self._send(202, {k: v for k, v in job.items() if k != "pokes"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> MockEndpoint:
        self.thread.start()
        return self

    def __exit__(self, *args) -> None:
        self.server.shutdown()
        self.server.server_close()


class ClientTests(unittest.TestCase):
    def endpoint(self, server: MockEndpoint, token: str = TOKEN) -> WindowsEndpoint:
        return WindowsEndpoint(EndpointConfig(base_url=server.url, token=token, timeout=5))

    def test_health_and_auth(self) -> None:
        with MockEndpoint() as server:
            health = self.endpoint(server).health()
            self.assertEqual(health["pipeline_id"], PIPELINE_ID)
            self.assertTrue(health["ready"])
            with self.assertRaises(EndpointAuthError):
                self.endpoint(server, token="wrong").health()

    def test_start_is_idempotent_and_conflicts_on_mismatch(self) -> None:
        with MockEndpoint() as server:
            endpoint = self.endpoint(server)
            first = endpoint.start_job(run_id=RUN_ID, package="handoff/m3.0", revision_sha256=SHA, target="m3")
            second = endpoint.start_job(run_id=RUN_ID, package="handoff/m3.0", revision_sha256=SHA, target="m3")
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(len(server.jobs), 1)
            with self.assertRaises(EndpointConflict):
                endpoint.start_job(run_id=RUN_ID, package="handoff/other", revision_sha256=SHA, target="m3")

    def test_wait_passes_and_fails_closed_on_result(self) -> None:
        with MockEndpoint() as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, package="handoff/m3.0", revision_sha256=SHA, target="m3")
            job = endpoint.wait(RUN_ID, interval=0.05, timeout=5)
            self.assertEqual(job["status"], "passed")
            self.assertEqual(
                check_result(job["result"], expected_slug="example/m3.0", expected_base="feature/m3.0")["submission"][
                    "url"
                ],
                "https://github.com/example/m3.0/pull/1",
            )
            with self.assertRaises(ResultNotPublishable):
                check_result(
                    {
                        "passed": True,
                        "pipeline_id": PIPELINE_ID,
                        "subject_sha256": SHA,
                        "quality": {"passed": True, "subject_sha256": SHA},
                        "submission": {
                            "passed": True,
                            "subject_sha256": SHA,
                            "base": "feature/m3.0",
                            "branch": "work/solidworks/m3.0",
                            "state": "published",
                            "commit": "a" * 40,
                            "url": "https://example.test/pr/1",
                        },
                    },
                    expected_slug="example/m3.0",
                    expected_base="feature/m3.0",
                )
            good = job["result"]
            for bad in (
                {**good, "subject_sha256": "b" * 64},
                {**good, "quality": {**good["quality"], "subject_sha256": "b" * 64}},
                {**good, "submission": {**good["submission"], "url": "https://example.test/pr/1"}},
                {**good, "submission": {**good["submission"], "commit": "xyz"}},
                {**good, "submission": {**good["submission"], "state": "queued"}},
                {**good, "submission": {**good["submission"], "repository_slug": "attacker/repo"}},
                {**good, "submission": {**good["submission"], "base": "feature/other"}},
                {**good, "submission": {**good["submission"], "branch": "main"}},
            ):
                with self.assertRaises(ResultNotPublishable):
                    check_result(bad, expected_slug="example/m3.0", expected_base="feature/m3.0")
        with MockEndpoint(omit_quality=True) as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, package="handoff/m3.0", revision_sha256=SHA, target="m3")
            with self.assertRaises(ResultNotPublishable):
                check_result(
                    endpoint.wait(RUN_ID, interval=0.05, timeout=5)["result"],
                    expected_slug="example/m3.0",
                    expected_base="feature/m3.0",
                )

    def test_failed_job_raises(self) -> None:
        with MockEndpoint(fail_job=True) as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, package="handoff/m3.0", revision_sha256=SHA, target="m3")
            with self.assertRaises(JobFailed):
                endpoint.wait(RUN_ID, interval=0.05, timeout=5)

    def test_event_contract_enforced(self) -> None:
        with MockEndpoint(legacy_events=True) as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, package="handoff/m3.0", revision_sha256=SHA, target="m3")
            with self.assertRaises(EndpointProtocolError):
                endpoint.get_job(RUN_ID)

    def test_input_validation(self) -> None:
        with self.assertRaises(EndpointProtocolError):
            EndpointConfig(base_url="http://example.com", token=TOKEN)
        with self.assertRaises(EndpointProtocolError):
            validate_package("/etc/passwd")
        with self.assertRaises(EndpointProtocolError):
            validate_package("a/../../b")
        with self.assertRaises(EndpointProtocolError):
            validate_revision_sha("not-a-sha")
        with self.assertRaises(EndpointProtocolError):
            validate_run_id("not-a-uuid")
        with self.assertRaises(EndpointProtocolError):
            validate_run_id(RUN_ID.upper())
        for bad in ("pkg/./x", "pkg//x", "pkg/", "/abs", "CON/x"):
            with self.assertRaises(EndpointProtocolError):
                validate_package(bad)
        self.assertEqual(validate_package("手臂 r1/cad"), "手臂 r1/cad")
        self.assertEqual(validate_package(".hidden/x"), ".hidden/x")

    def test_cross_host_redirect_never_forwards_token(self) -> None:
        with MockEndpoint() as target, MockEndpoint(redirect_to=target.url) as redirector:
            endpoint = WindowsEndpoint(EndpointConfig(base_url=redirector.url, token=TOKEN, timeout=5))
            with self.assertRaises(EndpointError):
                endpoint.health()
            self.assertEqual(target.hits, 0)

    def test_start_rejects_a_different_echoed_handoff(self) -> None:
        response = {
            "run_id": RUN_ID,
            "status": "queued",
            "request": {"run_id": RUN_ID, "package": "other/r1", "revision_sha256": SHA, "target": "m3"},
        }
        endpoint = WindowsEndpoint(
            EndpointConfig(base_url="http://127.0.0.1", token=TOKEN),
            opener=lambda *_args, **_kwargs: io.BytesIO(json.dumps(response).encode()),
        )
        with self.assertRaisesRegex(EndpointProtocolError, "different mechanical handoff"):
            endpoint.start_job(run_id=RUN_ID, package="handoff/m3.0", revision_sha256=SHA, target="m3")

    def test_invalid_url_timeout_and_redirect_fail_closed(self) -> None:
        for address in ("http://127.0.0.1:invalid", "http://127.0.0.1?token=x", "http://127.0.0.1#fragment"):
            with self.subTest(address=address), self.assertRaises(EndpointProtocolError):
                EndpointConfig(base_url=address, token=TOKEN)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(EndpointProtocolError):
                EndpointConfig(base_url="http://127.0.0.1", token=TOKEN, timeout=timeout)
        with (
            MockEndpoint(redirect_to="http://127.0.0.1:invalid") as redirector,
            self.assertRaises(EndpointError),
        ):
            self.endpoint(redirector).health()

    def test_invalid_utf8_response_is_a_protocol_failure(self) -> None:
        endpoint = WindowsEndpoint(
            EndpointConfig(base_url="http://127.0.0.1", token=TOKEN),
            opener=lambda *_args, **_kwargs: io.BytesIO(b"\xff"),
        )
        with self.assertRaisesRegex(EndpointProtocolError, "invalid UTF-8"):
            endpoint.health()


if __name__ == "__main__":
    unittest.main()
