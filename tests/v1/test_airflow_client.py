"""Neutral mocked-HTTP tests for the Windows endpoint client (auth, retries, failure semantics)."""

from __future__ import annotations

import json
import hashlib
import io
import sys
import tempfile
import threading
import types
import uuid
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib import parse as urlparse

from description_pipeline.orchestration.airflow_client import (
    EndpointAuthError,
    EndpointConfig,
    EndpointConflict,
    EndpointError,
    EndpointProtocolError,
    HandoffResolution,
    HANDOFF_SCHEMA,
    JOB_SCHEMA,
    JobFailed,
    PIPELINE_ID,
    ResultNotPublishable,
    WindowsEndpoint,
    check_result,
    config_from_airflow_connection,
    native_run_id,
    resolved_routing,
    validate_artifact_name,
    validate_package,
    validate_run_id,
)

TOKEN = "test-token"
RUN_ID = "b3f1c2d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
SHA = "a" * 64
PREVIEW = {
    "pipeline_id": PIPELINE_ID,
    "run_id": RUN_ID,
    "subject_sha256": SHA,
    "urdf": "urdf/robot.urdf",
    "files": {"urdf/robot.urdf": SHA, "meshes/base.stl": "d" * 64},
}
NATIVE_HANDOFF = {
    "schema_version": HANDOFF_SCHEMA,
    "pipeline_id": PIPELINE_ID,
    "package": "handoff/m3.0",
    "handoff_sha256": "b" * 64,
}


def native_resolution(**overrides) -> HandoffResolution:
    values = {"package": "handoff/m3.0", "handoff_sha256": "b" * 64}
    values.update(overrides)
    return HandoffResolution(**values)


class MockEndpoint:
    """Minimal stand-in for the Windows runner; state advances on each GET."""

    def __init__(
        self,
        *,
        token: str = TOKEN,
        fail_job: bool = False,
        invalid_events: bool = False,
        omit_quality: bool = False,
        omit_submission: bool = False,
        redirect_to: str | None = None,
        handoff_response: dict | None = None,
        preview_payload: dict | None = None,
        artifact_redirect_to: str | None = None,
    ) -> None:
        self.token = token
        self.fail_job = fail_job
        self.invalid_events = invalid_events
        self.omit_quality = omit_quality
        self.omit_submission = omit_submission
        self.redirect_to = redirect_to
        self.handoff_response = handoff_response
        self.preview_payload = preview_payload
        self.artifact_redirect_to = artifact_redirect_to
        self.hits = 0
        self.jobs: dict[str, dict] = {}
        self.resolved_paths: list[str] = []
        self.imports: list[dict] = []
        self.artifact_paths: list[str] = []
        self.artifacts = {
            "urdf/robot.urdf": b"<robot name='fixture'/>",
            "meshes/base.stl": b"solid base",
            "meshes/a b.stl": b"solid spaced",
        }
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
                    parts = self.path.split("/")
                    run_id = parts[3] if len(parts) > 3 else ""
                    if len(parts) >= 5 and parts[4] == "preview":
                        job = outer.jobs.get(run_id)
                        if job is None or job.get("status") != "passed":
                            self._send(404, {"error": "no passed delivery"})
                            return
                        payload = dict(outer.preview_payload or PREVIEW)
                        payload["run_id"] = run_id
                        self._send(200, payload)
                        return
                    if len(parts) >= 6 and parts[4] == "artifacts":
                        if outer.artifact_redirect_to:
                            self.send_response(302)
                            self.send_header("Location", outer.artifact_redirect_to)
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        name = urlparse.unquote("/".join(parts[5:]))
                        outer.artifact_paths.append(self.path)
                        data = outer.artifacts.get(name)
                        if data is None:
                            self._send(404, {"error": "unknown artifact"})
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    job = outer.jobs.get(run_id)
                    if job is None:
                        self._send(404, {"error": "unknown run_id"})
                        return
                    if "hardware_id" not in job:
                        job.update(
                            hardware_id="m3.0",
                            revision="r1",
                            repository_slug="example/m3.0",
                            repository_base="feature/m3.0",
                        )
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
                        if outer.omit_submission:
                            result["submission"] = {"passed": False, "subject_sha256": SHA}
                        job["result"] = result
                    else:
                        job["status"] = "running"
                    event = (
                        {"phase": "job", "state": job["status"], "time": "t0"}
                        if outer.invalid_events
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
                body = self.rfile.read(length)
                if self.path in {"/v1/handoffs/resolve", "/v1/handoffs/import"}:
                    if self.path == "/v1/handoffs/resolve":
                        outer.resolved_paths.append(json.loads(body or b"{}").get("handoff_path"))
                    else:
                        outer.imports.append(
                            {
                                "size": len(body),
                                "bytes": body,
                                "content_type": self.headers.get("Content-Type"),
                                "content_length": self.headers.get("Content-Length"),
                            }
                        )
                    self._send(200, dict(outer.handoff_response or NATIVE_HANDOFF))
                    return
                payload = json.loads(body or b"{}")
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
    def endpoint(self, server: MockEndpoint, token: str = TOKEN, roots=()) -> WindowsEndpoint:
        return WindowsEndpoint(EndpointConfig(base_url=server.url, token=token, timeout=5, handoff_roots=roots))

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
            first = endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())
            second = endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(len(server.jobs), 1)
            with self.assertRaises(EndpointConflict):
                endpoint.start_job(run_id=RUN_ID, resolution=native_resolution(handoff_sha256="c" * 64))

    def test_native_job_binds_only_package_and_digest(self) -> None:
        with MockEndpoint() as server:
            endpoint = self.endpoint(server)
            resolved = endpoint.resolve_handoff("handoff/m3.0")
            self.assertEqual(set(vars(resolved)), {"package", "handoff_sha256"})
            job = endpoint.start_job(run_id=RUN_ID, resolution=resolved)
            self.assertEqual(job["request"], {"run_id": RUN_ID, "package": "handoff/m3.0", "handoff_sha256": "b" * 64})
            self.assertNotIn("repository_slug", job)
            with self.assertRaises(EndpointProtocolError):
                resolved_routing(job)
            with self.assertRaises(EndpointConflict):
                endpoint.start_job(run_id=RUN_ID, resolution=native_resolution(handoff_sha256="c" * 64))

    def test_native_job_resolves_routing_after_cad_discovery(self) -> None:
        with MockEndpoint() as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, resolution=endpoint.resolve_handoff("handoff/m3.0"))
            routing = resolved_routing(endpoint.get_job(RUN_ID))
            self.assertEqual(routing["hardware_id"], "m3.0")
            self.assertEqual(routing["repository_slug"], "example/m3.0")
            self.assertEqual(routing["repository_base"], "feature/m3.0")
            passed = endpoint.wait(RUN_ID, interval=0.05, timeout=5)
            check_result(
                passed["result"],
                expected_slug=routing["repository_slug"],
                expected_base=routing["repository_base"],
            )

    def test_resolve_handoff_uses_resolver_for_managed_paths(self) -> None:
        with MockEndpoint() as server:
            endpoint = self.endpoint(server)
            resolved = endpoint.resolve_handoff("handoff/m3.0")
            self.assertEqual(resolved.package, "handoff/m3.0")
            self.assertEqual(resolved.handoff_sha256, "b" * 64)
            endpoint.resolve_handoff(r"C:\handoffs\m3.0")
            self.assertEqual(server.resolved_paths, ["handoff/m3.0", r"C:\handoffs\m3.0"])
            self.assertEqual(server.imports, [])

    def test_native_resolution_fails_closed_on_bad_payload(self) -> None:
        for bad in (
            {k: v for k, v in NATIVE_HANDOFF.items() if k != "handoff_sha256"},
            {k: v for k, v in NATIVE_HANDOFF.items() if k != "package"},
            {**NATIVE_HANDOFF, "schema_version": "solidworks-to-urdf.handoff/v2"},
            {**NATIVE_HANDOFF, "pipeline_id": "other-pipeline"},
            {**NATIVE_HANDOFF, "kind": "native"},
            {**NATIVE_HANDOFF, "package": "../escape"},
            {**NATIVE_HANDOFF, "handoff_sha256": "nope"},
        ):
            with (
                self.subTest(bad=bad),
                MockEndpoint(handoff_response=bad) as server,
                self.assertRaises(EndpointProtocolError),
            ):
                self.endpoint(server).resolve_handoff("handoff/m3.0")

    def test_get_job_accepts_unresolved_routing_but_rejects_empty_values(self) -> None:
        with MockEndpoint() as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, resolution=endpoint.resolve_handoff("handoff/m3.0"))
            self.assertNotIn("repository_slug", server.jobs[RUN_ID])
            endpoint.get_job(RUN_ID)
            server.jobs[RUN_ID]["repository_slug"] = " "
            with self.assertRaises(EndpointProtocolError):
                endpoint.get_job(RUN_ID)
            server.jobs[RUN_ID]["repository_slug"] = "example/m3.0"
            server.jobs[RUN_ID]["hardware_id"] = "m3.0 左"
            with self.assertRaises(EndpointProtocolError):
                resolved_routing(server.jobs[RUN_ID])
            server.jobs[RUN_ID]["hardware_id"] = "9m3"
            with self.assertRaises(EndpointProtocolError):
                resolved_routing(server.jobs[RUN_ID])
            server.jobs[RUN_ID]["hardware_id"] = "A" * 65
            with self.assertRaises(EndpointProtocolError):
                resolved_routing(server.jobs[RUN_ID])
            server.jobs[RUN_ID]["hardware_id"] = "M3.0"
            self.assertEqual(resolved_routing(server.jobs[RUN_ID])["hardware_id"], "M3.0")

    def test_read_artifact_verifies_digest_and_limit(self) -> None:
        urdf = b"<robot name='fixture'/>"
        stl = b"solid base"
        preview = {
            "pipeline_id": PIPELINE_ID,
            "run_id": RUN_ID,
            "subject_sha256": SHA,
            "urdf": "urdf/robot.urdf",
            "files": {
                "urdf/robot.urdf": hashlib.sha256(urdf).hexdigest(),
                "meshes/base.stl": hashlib.sha256(stl).hexdigest(),
            },
        }
        with MockEndpoint(preview_payload=preview) as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())
            endpoint.wait(RUN_ID, interval=0.05, timeout=5)
            served = endpoint.get_preview(RUN_ID)
            self.assertEqual(
                endpoint.read_artifact(RUN_ID, "urdf/robot.urdf", sha256=served["files"]["urdf/robot.urdf"]),
                urdf,
            )
            self.assertEqual(
                endpoint.read_artifact(RUN_ID, "meshes/base.stl", sha256=served["files"]["meshes/base.stl"]),
                stl,
            )
            with self.assertRaises(EndpointProtocolError):
                endpoint.read_artifact(RUN_ID, "urdf/robot.urdf", sha256="c" * 64)
            with self.assertRaises(EndpointProtocolError):
                endpoint.read_artifact(RUN_ID, "urdf/robot.urdf", sha256=SHA, limit=4)

    def _stub_archive(self, digest: str = "b" * 64):
        from description_pipeline.orchestration import handoffs as handoffs_module

        self.archived_sources: list[Path] = []

        def prepare_archive(source: Path, archive: Path) -> dict:
            self.assertTrue(Path(source).is_dir(), source)
            self.archived_sources.append(Path(source).resolve())
            Path(archive).write_bytes(b"PK\x03\x04stub-archive")
            return {"handoff_sha256": digest, "files": {"cad-revision.json": "x"}}

        return mock.patch.object(handoffs_module, "prepare_archive", prepare_archive)

    def test_absolute_posix_path_is_zipped_and_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "handoffs"
            source = root / "handoff"
            source.mkdir(parents=True)
            (source / "cad-revision.json").write_text("{}", encoding="utf-8")
            with MockEndpoint() as server, self._stub_archive():
                resolved = self.endpoint(server, roots=[root]).resolve_handoff(str(source))
            self.assertEqual(resolved.handoff_sha256, "b" * 64)
            self.assertEqual(self.archived_sources, [source.resolve()])
            self.assertEqual(server.resolved_paths, [])
            self.assertEqual(len(server.imports), 1)
            upload = server.imports[0]
            self.assertEqual(upload["bytes"], b"PK\x03\x04stub-archive")
            self.assertEqual(upload["content_type"], "application/zip")
            self.assertEqual(upload["content_length"], str(upload["size"]))

    def test_import_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "handoffs"
            source = root / "handoff"
            source.mkdir(parents=True)
            with (
                MockEndpoint() as server,
                self._stub_archive(digest="c" * 64),
                self.assertRaises(EndpointProtocolError),
            ):
                self.endpoint(server, roots=[root]).resolve_handoff(str(source))

    def test_absolute_handoff_requires_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "handoff"
            source.mkdir()
            with MockEndpoint() as server:
                with self.assertRaises(EndpointProtocolError):
                    self.endpoint(server).resolve_handoff(str(source))
                self.assertEqual(server.hits, 0)
                self.assertEqual(server.imports, [])

    def test_absolute_handoff_outside_roots_is_rejected_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "handoffs"
            root.mkdir()
            outside = Path(tmp) / "private"
            outside.mkdir()
            with MockEndpoint() as server:
                with self.assertRaises(EndpointProtocolError):
                    self.endpoint(server, roots=[root]).resolve_handoff(str(outside))
                self.assertEqual(server.hits, 0)
                self.assertEqual(server.imports, [])

    def test_nested_folder_inside_root_is_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "handoffs"
            nested = root / "team-a" / "robot-cell"
            nested.mkdir(parents=True)
            (nested / "part.SLDPRT").write_text("solid", encoding="utf-8")
            with MockEndpoint() as server, self._stub_archive():
                resolved = self.endpoint(server, roots=[root]).resolve_handoff(str(nested))
                self.assertEqual(resolved.handoff_sha256, "b" * 64)
                self.assertEqual(self.archived_sources, [nested.resolve()])
                self.assertEqual(len(server.imports), 1)

    def test_linked_handoff_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "handoffs"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (root / "linked").symlink_to(outside, target_is_directory=True)
            with MockEndpoint() as server:
                with self.assertRaises(EndpointProtocolError):
                    self.endpoint(server, roots=[root]).resolve_handoff(str(root / "linked" / "cell"))
                self.assertEqual(server.hits, 0)
                with self.assertRaises(EndpointProtocolError):
                    self.endpoint(server, roots=[root / "linked"])

    def test_filesystem_root_and_relative_roots_are_refused(self) -> None:
        for roots in ("/", ["relative/root"], [123]):
            with self.subTest(roots=roots), self.assertRaises(EndpointProtocolError):
                EndpointConfig(base_url="http://127.0.0.1", token=TOKEN, handoff_roots=roots)

    def test_connection_extra_supplies_handoff_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "handoffs"
            root.mkdir()

            class FakeConnection:
                host = "127.0.0.1"
                port = 18765
                password = "token"

                def __init__(self) -> None:
                    self.extra_dejson = {"timeout": 5, "handoff_roots": [str(root)]}

            class FakeBaseHook:
                @staticmethod
                def get_connection(conn_id):
                    return FakeConnection()

            airflow = types.ModuleType("airflow")
            hooks = types.ModuleType("airflow.hooks")
            base = types.ModuleType("airflow.hooks.base")
            base.BaseHook = FakeBaseHook
            hooks.base = base
            airflow.hooks = hooks
            with mock.patch.dict(sys.modules, {"airflow": airflow, "airflow.hooks": hooks, "airflow.hooks.base": base}):
                config = config_from_airflow_connection("solidworks_windows")
            self.assertEqual(config.handoff_roots, (root.resolve(),))
            self.assertEqual(config.base_url, "http://127.0.0.1:18765")

    def test_native_run_id_is_canonical_and_stable(self) -> None:
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, f"solidworks_to_urdf:{RUN_ID}"))
        self.assertEqual(native_run_id(RUN_ID), expected)
        self.assertEqual(native_run_id(RUN_ID), native_run_id(RUN_ID))
        with self.assertRaises(EndpointProtocolError):
            native_run_id("")

    def test_preview_requires_a_passed_delivery(self) -> None:
        with MockEndpoint() as server:
            endpoint = self.endpoint(server)
            with self.assertRaises(EndpointError):
                endpoint.get_preview(RUN_ID)
            endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())
            endpoint.wait(RUN_ID, interval=0.05, timeout=5)
            preview = endpoint.get_preview(RUN_ID)
            self.assertEqual(preview["run_id"], RUN_ID)
            self.assertEqual(preview["urdf"], "urdf/robot.urdf")
            self.assertIn(preview["urdf"], preview["files"])

    def test_preview_fails_closed_on_bad_payload(self) -> None:
        for bad in (
            {**PREVIEW, "pipeline_id": "other"},
            {**PREVIEW, "subject_sha256": "nope"},
            {**PREVIEW, "urdf": "../escape.urdf"},
            {**PREVIEW, "files": {"urdf/robot.urdf": "nope"}},
            {**PREVIEW, "files": {"meshes/base.stl": "d" * 64}},
        ):
            with self.subTest(bad=bad), MockEndpoint(preview_payload=bad) as server:
                endpoint = self.endpoint(server)
                endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())
                endpoint.wait(RUN_ID, interval=0.05, timeout=5)
                with self.assertRaises(EndpointProtocolError):
                    endpoint.get_preview(RUN_ID)

    def test_open_artifact_streams_verified_bytes(self) -> None:
        with MockEndpoint() as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())
            endpoint.wait(RUN_ID, interval=0.05, timeout=5)
            with endpoint.open_artifact(RUN_ID, "urdf/robot.urdf") as response:
                self.assertEqual(response.read(), b"<robot name='fixture'/>")
            with endpoint.open_artifact(RUN_ID, "meshes/a b.stl") as response:
                self.assertEqual(response.read(), b"solid spaced")
            self.assertEqual(
                server.artifact_paths,
                [f"/v1/jobs/{RUN_ID}/artifacts/urdf/robot.urdf", f"/v1/jobs/{RUN_ID}/artifacts/meshes/a%20b.stl"],
            )
            with self.assertRaises(EndpointError):
                endpoint.open_artifact(RUN_ID, "meshes/missing.stl")
            for bad in ("../escape", "/etc/passwd", "a\\b", "", "."):
                with self.subTest(bad=bad), self.assertRaises(EndpointProtocolError):
                    endpoint.open_artifact(RUN_ID, bad)
            self.assertEqual(len(server.artifact_paths), 3)

    def test_open_artifact_refuses_cross_host_redirect(self) -> None:
        with MockEndpoint(artifact_redirect_to="http://127.0.0.1:1/urdf/robot.urdf") as server:
            endpoint = self.endpoint(server)
            with self.assertRaises(EndpointError):
                endpoint.open_artifact(RUN_ID, "urdf/robot.urdf")

    def test_wait_passes_and_fails_closed_on_result(self) -> None:
        with MockEndpoint() as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())
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
            endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())
            with self.assertRaises(ResultNotPublishable):
                check_result(
                    endpoint.wait(RUN_ID, interval=0.05, timeout=5)["result"],
                    expected_slug="example/m3.0",
                    expected_base="feature/m3.0",
                )

    def test_failed_job_raises(self) -> None:
        with MockEndpoint(fail_job=True) as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())
            with self.assertRaises(JobFailed):
                endpoint.wait(RUN_ID, interval=0.05, timeout=5)

    def test_invalid_event_contract_is_refused(self) -> None:
        with MockEndpoint(invalid_events=True) as server:
            endpoint = self.endpoint(server)
            endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())
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
            validate_run_id("not-a-uuid")
        with self.assertRaises(EndpointProtocolError):
            validate_run_id(RUN_ID.upper())
        for bad in ("pkg/./x", "pkg//x", "pkg/", "/abs", "CON/x"):
            with self.assertRaises(EndpointProtocolError):
                validate_package(bad)
        self.assertEqual(validate_package("手臂 r1/cad"), "手臂 r1/cad")
        self.assertEqual(validate_package(".hidden/x"), ".hidden/x")
        self.assertEqual(validate_artifact_name("urdf/robot.urdf"), "urdf/robot.urdf")
        with self.assertRaises(EndpointProtocolError):
            validate_artifact_name("../escape.urdf")

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
            "request": {"run_id": RUN_ID, "package": "other/r1", "handoff_sha256": "c" * 64},
        }
        endpoint = WindowsEndpoint(
            EndpointConfig(base_url="http://127.0.0.1", token=TOKEN),
            opener=lambda *_args, **_kwargs: io.BytesIO(json.dumps(response).encode()),
        )
        with self.assertRaisesRegex(EndpointProtocolError, "different mechanical handoff"):
            endpoint.start_job(run_id=RUN_ID, resolution=native_resolution())

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
