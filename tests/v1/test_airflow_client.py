"""Shared endpoint observation, configuration and result validation tests."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from description_pipeline.orchestration.airflow_client import (
    EndpointAuthError,
    EndpointConfig,
    EndpointConflict,
    EndpointError,
    EndpointProtocolError,
    JOB_SCHEMA,
    PIPELINE_ID,
    ResultNotPublishable,
    WindowsEndpoint,
    check_result,
    native_run_id,
    validate_artifact_name,
    validate_run_id,
    verified_result,
)
from description_pipeline.stages import STAGE_IDS

TOKEN = "test-token"
RUN_ID = "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
SHA = "a" * 64


def rerun_plan_payload(run_id: str, status: str = "failed") -> dict:
    rows = [
        {
            "stage": stage,
            "name": stage,
            "eligible": True,
            "reason": "ok",
            "message": "",
            "recomputes": [stage],
            "retains": [],
            "prerequisites": {
                "inputs": "ok",
                "tool": "ok",
                "dependency": "ok",
                "target": "ok",
                "receipt": "ok",
                "earliest_required": None,
            },
            "target_changed": False,
        }
        for stage in STAGE_IDS
    ]
    return {"run_id": run_id, "status": status, "stage_reruns": rows}


def job_snapshot(*, status: str = "passed", events: list[dict] | None = None) -> dict:
    return {
        "schema_version": JOB_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "status": status,
        "events": events
        if events is not None
        else [{"stage": "freeze", "state": "completed", "at": "2026-10-25T00:00:00Z"}],
        "result": None,
        "error": None,
    }


class MockEndpoint:
    """Read-only endpoint double for the retained job observation routes."""

    def __init__(
        self,
        *,
        token: str = TOKEN,
        redirect_to: str | None = None,
        rerun_payload: dict | None = None,
    ) -> None:
        self.token = token
        self.redirect_to = redirect_to
        self.rerun_payload = rerun_payload
        self.jobs: dict[str, dict] = {}
        self.hits = 0
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
                outer.hits += 1
                if outer.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to + self.path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self._send(401, {"error": "unauthorized"})
                    return
                parts = self.path.split("/")
                run_id = parts[3] if len(parts) > 3 else ""
                if len(parts) == 5 and parts[4] == "reruns":
                    if run_id not in outer.jobs:
                        self._send(404, {"error": "unknown run_id"})
                        return
                    self._send(
                        200,
                        outer.rerun_payload
                        or rerun_plan_payload(run_id, outer.jobs[run_id]["status"]),
                    )
                    return
                if len(parts) == 4:
                    job = outer.jobs.get(run_id)
                    if job is None:
                        self._send(404, {"error": "unknown run_id"})
                        return
                    self._send(200, job)
                    return
                self._send(404, {"error": "not found"})

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
    def endpoint(self, server: MockEndpoint, *, token: str = TOKEN, roots=()) -> WindowsEndpoint:
        return WindowsEndpoint(
            EndpointConfig(
                base_url=server.url,
                token=token,
                timeout=5,
                handoff_roots=roots,
            )
        )

    def test_get_job_reads_the_retained_contract_and_honors_auth(self) -> None:
        with MockEndpoint() as server:
            server.jobs[RUN_ID] = job_snapshot()
            job = self.endpoint(server).get_job(RUN_ID)
            self.assertEqual(job["run_id"], RUN_ID)
            self.assertEqual(job["status"], "passed")
            with self.assertRaises(EndpointAuthError):
                self.endpoint(server, token="wrong").get_job(RUN_ID)

    def test_get_job_rejects_invalid_events_and_empty_routing(self) -> None:
        with MockEndpoint() as server:
            endpoint = self.endpoint(server)
            server.jobs[RUN_ID] = job_snapshot()
            endpoint.get_job(RUN_ID)
            server.jobs[RUN_ID]["repository_slug"] = " "
            with self.assertRaises(EndpointProtocolError):
                endpoint.get_job(RUN_ID)
            server.jobs[RUN_ID].pop("repository_slug")
            server.jobs[RUN_ID]["events"] = [{"phase": "job", "state": "passed"}]
            with self.assertRaises(EndpointProtocolError):
                endpoint.get_job(RUN_ID)

    def test_rerun_plan_round_trips_and_refuses_partial_coverage(self) -> None:
        with MockEndpoint() as server:
            server.jobs[RUN_ID] = job_snapshot(status="failed")
            plan = self.endpoint(server).rerun_plan(RUN_ID)
            self.assertEqual(plan["run_id"], RUN_ID)
            self.assertEqual([row["stage"] for row in plan["stage_reruns"]], list(STAGE_IDS))
            broken = rerun_plan_payload(RUN_ID)
            broken["stage_reruns"] = broken["stage_reruns"][:-1]
            with MockEndpoint(rerun_payload=broken) as bad_server:
                bad_server.jobs[RUN_ID] = job_snapshot(status="failed")
                with self.assertRaises(EndpointProtocolError):
                    self.endpoint(bad_server).rerun_plan(RUN_ID)

    def test_filesystem_root_and_relative_roots_are_refused(self) -> None:
        for roots in ("/", ["relative/root"], [123]):
            with self.subTest(roots=roots), self.assertRaises(EndpointProtocolError):
                EndpointConfig(base_url="http://127.0.0.1", token=TOKEN, handoff_roots=roots)

    def test_discovery_settings_are_validated_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            config = EndpointConfig(
                base_url="http://127.0.0.1",
                token=TOKEN,
                discovery={"record_roots": [str(records)]},
            )
            self.assertEqual(config.discovery["record_roots"], (records,))
            for bad in (
                {"record_roots": ["relative/root"]},
                {"record_roots": [str(root / "missing")]},
                {"frozen_names_file": str(records)},
                {"record_roots": "records"},
                {"unknown": []},
                "records",
            ):
                with self.subTest(bad=str(bad)), self.assertRaises(EndpointProtocolError):
                    EndpointConfig(base_url="http://127.0.0.1", token=TOKEN, discovery=bad)
            link = root / "linked"
            link.symlink_to(records, target_is_directory=True)
            with self.assertRaises(EndpointProtocolError):
                EndpointConfig(base_url="http://127.0.0.1", token=TOKEN, discovery={"record_roots": [str(link)]})

    def test_discovery_record_paths_reject_root_and_dotdot_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            nested = root / "sub"
            nested.mkdir()
            for value in (
                str(records / ".." / "records"),
                str(nested / ".." / "records"),
                str(nested / ".." / "missing"),
                str(nested) + "/../records",
                str(nested) + "/..",
                str(nested) + "//records",
                "/" + str(records),
                "/",
            ):
                with self.subTest(record_root=value):
                    with self.assertRaises(EndpointProtocolError) as caught:
                        EndpointConfig(
                            base_url="http://127.0.0.1",
                            token=TOKEN,
                            discovery={"record_roots": [value]},
                        )
                    self.assertIn(
                        "discovery record root must be a dedicated absolute path",
                        str(caught.exception),
                    )

    def test_linux_routing_map_and_store_root_are_validated(self) -> None:
        config = EndpointConfig(
            base_url="http://127.0.0.1",
            token=TOKEN,
            repositories={"mimicverse/description": "/home/andy/Work/description"},
            store_root="/home/andy/operator/runs",
        )
        self.assertEqual(
            config.repositories,
            {"mimicverse/description": Path("/home/andy/Work/description")},
        )
        self.assertEqual(config.store_root, Path("/home/andy/operator/runs"))
        for kwargs in (
            {"repositories": {"noslug": "/srv/repo"}},
            {"repositories": {"owner/repo": "relative/repo"}},
            {"repositories": ["owner/repo"]},
            {"store_root": "relative/store"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(EndpointProtocolError):
                EndpointConfig(base_url="http://127.0.0.1", token=TOKEN, **kwargs)

    def test_native_run_id_is_canonical_and_stable(self) -> None:
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, f"solidworks_to_urdf:{RUN_ID}"))
        self.assertEqual(native_run_id(RUN_ID), expected)
        self.assertEqual(native_run_id(RUN_ID), native_run_id(RUN_ID))
        with self.assertRaises(EndpointProtocolError):
            native_run_id("")

    def test_publication_failure_never_hides_or_weakens_the_verified_model(self) -> None:
        result = {
            "passed": False,
            "pipeline_id": PIPELINE_ID,
            "stage": "publish",
            "state": "failed",
            "subject_sha256": SHA,
            "quality": {
                "passed": True,
                "subject_sha256": SHA,
                "checks": [{"id": "x", "state": "passed"}],
            },
            "submission": {
                "passed": False,
                "subject_sha256": SHA,
                "state": "failed",
                "error": "git_failed",
                "commit": "",
                "base": "feature/m3.0",
                "branch": "work/description/m3.0/0123456789abcdef01234567",
                "repository_slug": "example/m3.0",
            },
        }
        self.assertIs(verified_result(result), result)
        with self.assertRaises(ResultNotPublishable) as raised:
            check_result(
                result,
                expected_slug="example/m3.0",
                expected_base="feature/m3.0",
            )
        self.assertEqual(str(raised.exception), "result.passed is not true")
        for bad in (
            None,
            {**result, "pipeline_id": "other-pipeline"},
            {**result, "subject_sha256": "not-a-digest"},
            {key: value for key, value in result.items() if key != "quality"},
            {**result, "quality": None},
            {**result, "quality": {**result["quality"], "passed": False}},
            {
                **result,
                "quality": {**result["quality"], "subject_sha256": "b" * 64},
            },
        ):
            with self.subTest(bad=bad), self.assertRaises(ResultNotPublishable):
                verified_result(bad)

    def test_internal_opener_ignores_ambient_proxies(self) -> None:
        root = Path(__file__).resolve().parents[2]
        environment = {**os.environ, "PYTHONPATH": str(root / "src")}
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "proxy_probe.py"),
                "airflow_client",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
            cwd=str(root),
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")

    def test_input_validation(self) -> None:
        with self.assertRaises(EndpointProtocolError):
            EndpointConfig(base_url="http://example.com", token=TOKEN)
        with self.assertRaises(EndpointProtocolError):
            validate_run_id("not-a-uuid")
        with self.assertRaises(EndpointProtocolError):
            validate_run_id(RUN_ID.upper())
        self.assertEqual(validate_artifact_name("urdf/robot.urdf"), "urdf/robot.urdf")
        with self.assertRaises(EndpointProtocolError):
            validate_artifact_name("../escape.urdf")

    def test_cross_host_redirect_never_forwards_token(self) -> None:
        with MockEndpoint() as target, MockEndpoint(redirect_to=target.url) as redirector:
            target.jobs[RUN_ID] = job_snapshot()
            endpoint = self.endpoint(redirector)
            with self.assertRaises(EndpointError):
                endpoint.get_job(RUN_ID)
            self.assertEqual(target.hits, 0)

    def test_refusal_bodies_reach_callers(self) -> None:
        body = {
            "error": "selection is bound to another run",
            "error_code": "already_active",
            "active_run_id": RUN_ID,
            "earliest_required": "2026-10-11T00:00:00Z",
            "detail": {"stage": "capture"},
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                return

            def do_GET(self) -> None:
                encoded = json.dumps(body).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = WindowsEndpoint(
            EndpointConfig(
                base_url=f"http://127.0.0.1:{server.server_port}",
                token=TOKEN,
            )
        )
        with self.assertRaises(EndpointConflict) as caught:
            endpoint.get_job(RUN_ID)
        failure = caught.exception
        self.assertEqual(failure.status, 409)
        self.assertEqual(failure.code, "already_active")
        self.assertEqual(failure.error_code, "already_active")
        self.assertEqual(failure.active_run_id, RUN_ID)
        self.assertEqual(failure.earliest_required, "2026-10-11T00:00:00Z")
        self.assertEqual(failure.detail, {"stage": "capture"})
        self.assertIn("request conflict (409)", str(failure))
        self.assertIn("selection is bound to another run", str(failure))

    def test_invalid_url_timeout_and_redirect_fail_closed(self) -> None:
        for address in (
            "http://127.0.0.1:invalid",
            "http://127.0.0.1?token=x",
            "http://127.0.0.1#fragment",
        ):
            with self.subTest(address=address), self.assertRaises(EndpointProtocolError):
                EndpointConfig(base_url=address, token=TOKEN)
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(EndpointProtocolError):
                EndpointConfig(
                    base_url="http://127.0.0.1",
                    token=TOKEN,
                    timeout=timeout,
                )

    def test_invalid_utf8_response_is_a_protocol_failure(self) -> None:
        endpoint = WindowsEndpoint(
            EndpointConfig(base_url="http://127.0.0.1", token=TOKEN),
            opener=lambda *_args, **_kwargs: io.BytesIO(b"\xff"),
        )
        with self.assertRaisesRegex(EndpointProtocolError, "invalid UTF-8"):
            endpoint.get_job(RUN_ID)


if __name__ == "__main__":
    unittest.main()
