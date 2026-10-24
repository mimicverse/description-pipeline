"""Offline regressions for single-resource Windows native operations."""

from __future__ import annotations

import copy
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib import error as urlerror
from urllib import request as urlrequest

from description_pipeline.io import digest, file_digest
from description_pipeline.orchestration.native_operation_client import (
    NativeOperationClient,
    NativeOperationClientConflict,
    NativeOperationDiscoverPort,
    NativeOperationEndpoint,
    NativeOperationProtocolError,
)
from description_pipeline.orchestration.native_operation_runtime import (
    CAPTURE_KIND,
    DISCOVER_KIND,
    DiscoverOperationExecutor,
    NativeOperationResult,
    NativeOperationRuntime,
    build_operation_record,
)
from description_pipeline.orchestration.windows import handler as windows_handler
from description_pipeline.schemas import TOOL_SCHEMA
from description_pipeline.sources.solidworks.discover_stage import (
    NATIVE_OBSERVATION_SCHEMA,
    NATIVE_REQUEST_SCHEMA,
    NativeDiscoverRequest,
)
from tests.sources.selection_support import closure_evidence
from tests.sources.test_solidworks_discovery import FakeBackend, record

TOKEN = "t" * 32
TOOL_SHA256 = "ab" * 32
NATIVE_TOOL = {
    "schema_version": TOOL_SCHEMA,
    "pipeline_id": "native-operation-test",
    "source_sha256": TOOL_SHA256,
    "runtime": {"role": "native"},
}
OUTPUT_CONTRACT = "description-pipeline.test-artifact/v1"


def _wait_for(predicate, *, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true before the deadline")


def _operation(
    operation_id: str,
    archive: Path,
    *,
    kind: str = CAPTURE_KIND,
    request: dict | None = None,
    output_contract: str = OUTPUT_CONTRACT,
    tool=NATIVE_TOOL,
) -> dict:
    return build_operation_record(
        operation_id=operation_id,
        kind=kind,
        request=request or {"marker": operation_id},
        input_archive_sha256=file_digest(archive),
        input_archive_bytes=archive.stat().st_size,
        native_tool_requirement_sha256=digest(tool),
        output_contract=output_contract,
    )


class _Server:
    def __init__(self, runtime: NativeOperationRuntime) -> None:
        self.runtime = runtime
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            windows_handler(runtime, TOKEN),
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    def client(self) -> NativeOperationClient:
        return NativeOperationClient(
            NativeOperationEndpoint(self.base_url, TOKEN, timeout=5.0)
        )

    def close(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


class NativeOperationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="native-operations-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "input.zip"
        self.archive.write_bytes(b"complete input archive")
        self.calls = 0

    def executor(self, request, source_archive, workspace, native_tool):
        del request, source_archive, native_tool
        self.calls += 1
        artifact = Path(workspace) / "result.zip"
        artifact.write_bytes(b"sealed native artifact")
        return NativeOperationResult(
            artifact_path=artifact,
            artifact_name="result.zip",
            output_contract=OUTPUT_CONTRACT,
            resource_terminated=True,
        )

    def test_http_roundtrip_is_idempotent_and_downloads_the_sealed_artifact(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: self.executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        envelope = _operation("operation-roundtrip", self.archive)

        accepted = client.create_operation(envelope)
        self.assertEqual(accepted.state, "accepted")
        admitted = client.upload_input("operation-roundtrip", self.archive)
        self.assertIn(admitted.state, {"awaiting_input", "running", "satisfied"})
        finished = client.wait_for_terminal("operation-roundtrip")
        self.assertEqual(finished.state, "satisfied")
        self.assertTrue(finished.resource_terminated)
        self.assertEqual(self.calls, 1)

        repeated = client.create_operation(envelope)
        self.assertEqual(repeated, finished)
        repeated_upload = client.upload_input("operation-roundtrip", self.archive)
        self.assertEqual(repeated_upload, finished)
        self.assertEqual(self.calls, 1)

        destination = self.root / "download/result.zip"
        downloaded = client.download_artifact("operation-roundtrip", destination)
        self.assertEqual(downloaded.path, destination)
        self.assertEqual(destination.read_bytes(), b"sealed native artifact")
        self.assertEqual(downloaded.sha256, file_digest(destination))

    def test_same_operation_id_with_changed_payload_is_conflict(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: self.executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        first = _operation("operation-conflict", self.archive)
        client.create_operation(first)

        other = self.root / "other.zip"
        other.write_bytes(b"different input archive")
        with self.assertRaises(NativeOperationClientConflict):
            client.create_operation(_operation("operation-conflict", other))
        self.assertEqual(self.calls, 0)

    def test_legacy_job_routes_are_absent_from_the_production_handler(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: self.executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        request = urlrequest.Request(
            server.base_url + "/v1/jobs",
            method="POST",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
            },
        )
        with self.assertRaises(urlerror.HTTPError) as raised:
            urlrequest.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 404)

    def test_changed_input_bytes_are_refused_before_execution(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: self.executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        envelope = _operation("operation-input-mismatch", self.archive)
        client.create_operation(envelope)
        other = self.root / "other.zip"
        other.write_bytes(b"different input archive")
        with self.assertRaises(NativeOperationClientConflict) as raised:
            client.upload_input("operation-input-mismatch", other)
        self.assertEqual(raised.exception.code, "input_archive_mismatch")
        self.assertEqual(self.calls, 0)

    def test_cache_hit_returns_original_result_without_executor_or_upload(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: self.executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        envelope = _operation("operation-cache", self.archive)
        client.create_operation(envelope)
        client.upload_input("operation-cache", self.archive)
        first = client.wait_for_terminal("operation-cache")
        self.assertEqual(self.calls, 1)
        self.archive.unlink()

        second = client.create_operation(envelope)
        self.assertEqual(second, first)
        self.assertEqual(self.calls, 1)

    def test_restart_after_execution_started_stays_unknown_and_blocks_next_resource(self) -> None:
        state = self.root / "state"
        first_runtime = NativeOperationRuntime(
            state_root=state,
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: self.executor},
        )
        first_runtime.create(_operation("operation-restart", self.archive))
        first_runtime.close()
        self.assertTrue(
            first_runtime._claim_and_start(
                "operation-restart",
                claim_id="claim-before-crash",
                fence=1,
                owner_sha256="1" * 64,
            )
        )

        second_runtime = NativeOperationRuntime(
            state_root=state,
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: self.executor},
        )
        self.addCleanup(second_runtime.close)
        interrupted = second_runtime.view("operation-restart")
        self.assertEqual(interrupted["state"], "unknown")
        self.assertFalse(interrupted["resource_terminated"])
        self.assertEqual(interrupted["failure"]["code"], "native_operation_restart_unknown")
        self.assertEqual(self.calls, 0)

        second_runtime.create(_operation("operation-blocked", self.archive))
        second_runtime.admit_input("operation-blocked", self.archive)
        _wait_for(
            lambda: second_runtime.view("operation-blocked")["actual_native_tool_sha256"]
        )
        blocked = second_runtime.view("operation-blocked")
        self.assertNotIn(blocked["state"], {"running", "satisfied"})
        event_types = {event["type"] for event in second_runtime._events("operation-blocked")}
        self.assertNotIn("claimed", event_types)
        self.assertEqual(self.calls, 0)
        with second_runtime._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT operation_id FROM native_resource_lease WHERE singleton=1"
                ).fetchone()[0],
                "operation-restart",
            )

    def test_pre_start_cancel_is_definite_and_rejects_later_input(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: self.executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        client.create_operation(_operation("operation-pre-cancel", self.archive))
        cancelled = client.cancel_operation("operation-pre-cancel")
        self.assertEqual(cancelled.state, "cancelled")
        with self.assertRaises(NativeOperationClientConflict):
            client.upload_input("operation-pre-cancel", self.archive)
        self.assertEqual(self.calls, 0)

    def test_post_start_cancel_is_unknown_and_never_restarts(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self_calls = 0

        def blocking(request, source_archive, workspace, native_tool):
            nonlocal self_calls
            del request, source_archive, native_tool
            self_calls += 1
            entered.set()
            if not release.wait(5):
                raise AssertionError("test executor was not released")
            artifact = Path(workspace) / "result.zip"
            artifact.write_bytes(b"completed after cancellation")
            return NativeOperationResult(
                artifact_path=artifact,
                artifact_name="result.zip",
                output_contract=OUTPUT_CONTRACT,
                resource_terminated=True,
            )

        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: blocking},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        client.create_operation(_operation("operation-post-cancel", self.archive))
        client.upload_input("operation-post-cancel", self.archive)
        self.assertTrue(entered.wait(5))
        cancelled = client.cancel_operation("operation-post-cancel")
        self.assertEqual(cancelled.state, "unknown")
        release.set()
        released = _wait_for(
            lambda: (
                current
                if (current := runtime.view("operation-post-cancel"))[
                    "resource_terminated"
                ]
                else None
            )
        )
        self.assertEqual(released["state"], "unknown")
        self.assertEqual(self_calls, 1)
        self.assertEqual(
            client.create_operation(_operation("operation-post-cancel", self.archive)).state,
            "unknown",
        )
        self.assertEqual(self_calls, 1)

    def test_tool_mismatch_is_definite_and_never_calls_executor(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: {**NATIVE_TOOL, "source_sha256": "cd" * 32},
            executors={CAPTURE_KIND: self.executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        client.create_operation(_operation("operation-tool-mismatch", self.archive))
        client.upload_input("operation-tool-mismatch", self.archive)
        failed = client.wait_for_terminal("operation-tool-mismatch")
        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.failure.code, "native_tool_mismatch")
        self.assertTrue(failed.resource_terminated)
        self.assertEqual(self.calls, 0)

    def test_missing_capture_executor_fails_without_loading_a_legacy_runner(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        client.create_operation(_operation("operation-no-capture", self.archive))
        client.upload_input("operation-no-capture", self.archive)
        failed = client.wait_for_terminal("operation-no-capture")
        self.assertEqual(failed.state, "failed")
        self.assertEqual(
            failed.failure.code,
            "native_operation_executor_unavailable",
        )
        self.assertTrue(failed.resource_terminated)
        self.assertEqual(self.calls, 0)

    def test_unknown_executor_exception_keeps_resource_and_blocks_next_operation(self) -> None:
        unknown_entered = threading.Event()
        calls = 0

        def unknown_executor(request, source_archive, workspace, native_tool):
            nonlocal calls
            del request, source_archive, workspace, native_tool
            calls += 1
            unknown_entered.set()
            raise RuntimeError("native process result is unknown\nsecond line")

        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: unknown_executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        client.create_operation(_operation("operation-unknown", self.archive))
        client.upload_input("operation-unknown", self.archive)
        self.assertTrue(unknown_entered.wait(5))
        unknown = client.wait_for_terminal("operation-unknown")
        self.assertEqual(unknown.state, "unknown")
        self.assertFalse(unknown.resource_terminated)
        self.assertNotIn("\n", unknown.failure.message)

        client.create_operation(_operation("operation-after-unknown", self.archive))
        client.upload_input("operation-after-unknown", self.archive)
        _wait_for(
            lambda: runtime.view("operation-after-unknown")[
                "actual_native_tool_sha256"
            ]
        )
        self.assertNotEqual(
            runtime.view("operation-after-unknown")["state"],
            "running",
        )
        self.assertEqual(calls, 1)
        reconciled = runtime.reconcile_resource_termination(
            "operation-unknown",
            evidence_sha256=digest({"external": "process gone"}),
            reason="native_external_process_gone",
        )
        self.assertEqual(reconciled["state"], "unknown")
        self.assertTrue(reconciled["resource_terminated"])
        self.assertEqual(
            client.wait_for_terminal("operation-after-unknown").state,
            "unknown",
        )
        self.assertEqual(calls, 2)


class _CountingBackend(FakeBackend):
    def __init__(self, payload: dict) -> None:
        super().__init__(payload)
        self.calls = 0

    def discover_native(self, frozen_source, settings, *, on_activity=None):
        self.calls += 1
        return super().discover_native(
            frozen_source,
            settings,
            on_activity=on_activity,
        )


class _ClosureReader:
    def __init__(
        self,
        files: dict[str, str],
        main: str,
        edges: dict,
        *,
        reader_sha256: str = TOOL_SHA256,
    ):
        self.files = files
        self.main = main
        self.edges = edges
        self.reader_sha256 = reader_sha256
        self.calls = 0

    def observe(self, stage, main_assembly, upload_files):
        self.calls += 1
        evidence = closure_evidence(
            upload_files,
            main_assembly,
            edges=self.edges,
            reader_sha256=self.reader_sha256,
            stage_root=str(stage),
        )
        return evidence


class NativeDiscoverOperationTests(unittest.TestCase):
    MAIN = "cad/robot.SLDASM"
    SELECTED = "cad/base.SLDPRT"
    DECOY = "spare/base.SLDPRT"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="native-discover-operation-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.files = {
            self.MAIN: b"main assembly bytes",
            self.SELECTED: b"selected same-basename part",
            self.DECOY: b"excluded same-basename part",
        }
        for name, payload in self.files.items():
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        self.inventory = {
            name: file_digest(self.source / name) for name in self.files
        }
        self.edges = {
            self.MAIN: [self.SELECTED],
            self.SELECTED: [],
            self.DECOY: [],
        }
        self.native_record = record()
        self.native_record["files"] = {
            self.MAIN: self.inventory[self.MAIN],
            self.SELECTED: self.inventory[self.SELECTED],
        }

    def request(
        self,
        operation_id: str,
        *,
        expected_native_tool_sha256: str = TOOL_SHA256,
    ) -> NativeDiscoverRequest:
        return NativeDiscoverRequest(
            schema_version=NATIVE_REQUEST_SCHEMA,
            operation_id=operation_id,
            command_id=operation_id,
            run_id="run-native-operation",
            request_digest=digest({"operation_id": operation_id}),
            source_files=self.inventory,
            source_inventory_sha256=digest(self.inventory),
            main_assembly=self.MAIN,
            purpose="engineering",
            expected_native_tool_sha256=expected_native_tool_sha256,
        )

    def runtime(self, reader, backend):
        executor = DiscoverOperationExecutor(
            selection_reader_factory=lambda: reader,
            backend_factory=lambda _root, _files: backend,
        )
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={DISCOVER_KIND: executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        return server.client()

    def test_single_discover_operation_selects_full_bound_archive_and_stops(self) -> None:
        reader = _ClosureReader(self.inventory, self.MAIN, self.edges)
        backend = _CountingBackend(copy.deepcopy(self.native_record))
        client = self.runtime(reader, backend)
        port = NativeOperationDiscoverPort(
            client,
            NATIVE_TOOL,
            timeout=5,
            poll_interval=0.01,
        )
        capture = mock.Mock(side_effect=AssertionError("Discover must not run Capture"))

        with mock.patch("description_pipeline.solidworks.capture", capture):
            execution = port.discover(
                self.request("discover-operation-single"),
                self.source,
                self.root / "workspace-single",
            )

        self.assertEqual(execution.effect_id, "native-discover:discover-operation-single")
        self.assertEqual(reader.calls, 1)
        self.assertEqual(backend.calls, 1)
        capture.assert_not_called()
        self.assertEqual(
            set(execution.observation.selection["files"]),
            {self.MAIN, self.SELECTED},
        )
        self.assertNotIn(self.DECOY, execution.observation.selection["files"])

        remote = client.get_operation("discover-operation-single")
        self.assertEqual(remote.state, "satisfied")
        self.assertTrue(remote.resource_terminated)
        self.assertEqual(remote.kind, DISCOVER_KIND)
        self.assertEqual(remote.artifact.media_type, "application/zip")
        repeated = port.discover(
            self.request("discover-operation-single"),
            self.source,
            self.root / "workspace-single",
        )
        self.assertEqual(repeated.effect_id, execution.effect_id)
        self.assertEqual(reader.calls, 1)
        self.assertEqual(backend.calls, 1)

    def test_tool_refusal_is_terminal_and_never_reads_native_record(self) -> None:
        refused_tool_sha256 = "cd" * 32
        reader = _ClosureReader(
            self.inventory,
            self.MAIN,
            self.edges,
            reader_sha256=refused_tool_sha256,
        )
        backend = _CountingBackend(copy.deepcopy(self.native_record))
        client = self.runtime(reader, backend)
        request = self.request(
            "discover-operation-tool-refusal",
            expected_native_tool_sha256=refused_tool_sha256,
        )
        source_archive = self.root / "source-set.zip"
        from description_pipeline.orchestration.discover_protocol import seal_source_archive

        seal_source_archive(self.source, request.source_files, source_archive)
        envelope = _operation(
            request.operation_id,
            source_archive,
            kind=DISCOVER_KIND,
            request=request.to_record(),
            output_contract=NATIVE_OBSERVATION_SCHEMA,
        )
        client.create_operation(envelope)
        client.upload_input(request.operation_id, source_archive)
        failed = client.wait_for_terminal(request.operation_id)
        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.failure.code, "discover.native_tool_mismatch")
        self.assertTrue(failed.resource_terminated)
        self.assertEqual(backend.calls, 0)

    def test_client_rejects_malformed_operation_view(self) -> None:
        with self.assertRaises(NativeOperationProtocolError):
            NativeOperationClient._view({"schema_version": "wrong"})


if __name__ == "__main__":
    unittest.main()
