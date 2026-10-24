"""Offline regressions for single-resource Windows native operations."""

from __future__ import annotations

import asyncio
import copy
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib import error as urlerror
from urllib import request as urlrequest

from description_pipeline.io import digest, file_digest, write_json
from description_pipeline.native_transfer import CAPTURE_TRANSFER_SCHEMA
from description_pipeline.orchestration.native_operation_client import (
    NativeOperationCapturePort,
    NativeOperationClient,
    NativeOperationClientConflict,
    NativeOperationClientError,
    NativeOperationDiscoverPort,
    NativeOperationEffectObserver,
    NativeOperationEndpoint,
    NativeOperationNotFound,
    NativeOperationProtocolError,
    NativeOperationStatusExpectation,
    NativeOperationToolProvider,
    capture_handler_from_native_operation,
    discover_adapter_from_native_operation,
    native_capture_operation_id,
    native_operation_input_was_admitted,
)
from description_pipeline.orchestration.discover_protocol import seal_source_archive
from description_pipeline.orchestration.native_operation_runtime import (
    CAPTURE_KIND,
    CAPTURE_OPERATION_SCHEMA,
    CaptureOperationRequest,
    CaptureOperationExecutor,
    DISCOVER_KIND,
    DiscoverOperationExecutor,
    NativeOperationFailure,
    NativeOperationCreate,
    NativeOperationResult,
    NativeOperationRuntime,
    NativeOperationUnresolved,
    build_operation_record,
)
from description_pipeline.pipeline.capture import CaptureEffectDispatch
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.pipeline.reconciliation import EffectReconciliationEvidence
from description_pipeline.request_evidence import build_request_evidence
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.orchestration.stage_transfer import CAPTURE_ARCHIVE
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
OUTPUT_CONTRACT = CAPTURE_TRANSFER_SCHEMA
REQUEST_EVIDENCE = build_request_evidence(
    purpose="engineering",
    request_id="native-operation-request",
    target_hardware="native_operation_test",
    change_summary="Exercise the native operation transport",
    main_assembly="cad/robot.SLDASM",
    handoff_digest="c" * 64,
    platform_config_digest="c" * 64,
)


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
    package_files: dict[str, str] | None = None,
) -> dict:
    if request is None:
        if kind == CAPTURE_KIND:
            package_files = package_files or {"cad/robot.SLDASM": "a" * 64}
            execution = {
                "claim_id": f"claim-{operation_id}",
                "command_id": operation_id,
                "effect_key": "capture-native",
                "executor_sha256": digest({"executor": operation_id}),
                "fence": 1,
                "native_tool_sha256": digest(tool),
                "owner_sha256": digest({"owner": operation_id}),
                "run_id": operation_id,
            }
            request = {
                "schema_version": CAPTURE_OPERATION_SCHEMA,
                "command_id": operation_id,
                "run_id": operation_id,
                "request_digest": digest({"request": operation_id}),
                "effect_key": "capture-native",
                "handoff_sha256": "b" * 64,
                "upload_sha256": "c" * 64,
                "prepared_sha256": digest(package_files),
                "main_assembly": "cad/robot.SLDASM",
                "package_files": package_files,
                "upstream_artifacts": [
                    _artifact(stage, operation_id)
                    for stage in ("freeze", "discover")
                ],
                "execution": execution,
                "native_tool_source_sha256": tool["source_sha256"],
                "request_evidence": REQUEST_EVIDENCE,
            }
        else:
            request = {"marker": operation_id}
    return build_operation_record(
        operation_id=operation_id,
        kind=kind,
        request=request,
        input_archive_sha256=file_digest(archive),
        input_archive_bytes=archive.stat().st_size,
        native_tool_requirement_sha256=digest(tool),
        output_contract=output_contract,
    )


def _artifact(stage: str, operation_id: str) -> dict:
    return _artifact_ref(stage, operation_id).to_record()


def _artifact_ref(stage: str, operation_id: str) -> ArtifactRef:
    artifact_id = digest({"artifact": stage, "operation_id": operation_id})
    return ArtifactRef(
        artifact_id=artifact_id,
        kind=f"stage_output:{stage}",
        locator=f"cas://artifact/{artifact_id}",
        manifest_sha256=digest({"manifest": artifact_id}),
        set_sha256=digest({"set": artifact_id}),
        producer_run_id=operation_id,
        producer_stage=stage,
        producer_tool_sha256=digest({"producer": stage}),
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
        claim_id = next(
            event["payload"]["claim_id"]
            for event in runtime._events("operation-roundtrip")
            if event["type"] == "claimed"
        )
        _wait_for(
            lambda: not (
                runtime.workspace_root / "operation-roundtrip" / claim_id
            ).exists()
        )

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

    def test_submit_operation_returns_the_current_state_without_polling_or_resend(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def blocking_executor(request, source_archive, workspace, native_tool):
            nonlocal calls
            del request, source_archive, native_tool
            calls += 1
            entered.set()
            if not release.wait(5):
                raise AssertionError("test executor was not released")
            artifact = Path(workspace) / "result.zip"
            artifact.write_bytes(b"completed after deferred wait")
            return NativeOperationResult(
                artifact_path=artifact,
                artifact_name="result.zip",
                output_contract=OUTPUT_CONTRACT,
                resource_terminated=True,
            )

        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: blocking_executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        sleeper = mock.Mock()
        client = NativeOperationClient(
            NativeOperationEndpoint(server.base_url, TOKEN, timeout=5.0),
            sleeper=sleeper,
        )
        envelope = _operation("operation-submit-once", self.archive)

        submitted = client.submit_operation(envelope, self.archive)

        self.assertIn(submitted.state, {"awaiting_input", "running"})
        self.assertTrue(entered.wait(5))
        self.assertTrue(native_operation_input_was_admitted(submitted))
        self.assertEqual(calls, 1)
        sleeper.assert_not_called()

        self.archive.unlink()
        repeated = client.submit_operation(envelope, self.archive)
        self.assertEqual(calls, 1)
        self.assertIn(repeated.state, {"awaiting_input", "running"})

        release.set()
        finished = client.wait_for_terminal("operation-submit-once", timeout=5)
        self.assertEqual(finished.state, "satisfied")
        terminal = client.submit_operation(envelope, self.archive)
        self.assertEqual(terminal, finished)
        self.assertEqual(calls, 1)

    def test_accepted_operation_can_resume_the_same_input_transfer(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "accepted-transfer-state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: self.executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        envelope = _operation("operation-accepted-transfer", self.archive)
        accepted = client.create_operation(envelope)
        self.assertEqual(accepted.state, "accepted")
        self.assertNotIn(
            "input_admitted",
            {event["type"] for event in runtime._events(envelope["operation_id"])},
        )

        corrupt = self.root / "corrupt-input.zip"
        corrupt.write_bytes(b"different archive")
        with self.assertRaises(NativeOperationClientError):
            client.upload_input(envelope["operation_id"], corrupt)
        rejected = client.get_operation(envelope["operation_id"])
        self.assertEqual(rejected.state, "accepted")
        self.assertNotIn(
            "input_admitted",
            {event["type"] for event in runtime._events(envelope["operation_id"])},
        )

        client.create_operation = mock.Mock(
            wraps=client.create_operation
        )
        submitted = client.resume_input_transfer(envelope, self.archive)
        self.assertIsNotNone(submitted)
        self.assertIn(
            submitted.state,
            {"awaiting_input", "running", "satisfied"},
        )
        finished = client.wait_for_terminal(
            envelope["operation_id"],
            timeout=5,
        )

        self.assertEqual(finished.state, "satisfied")
        self.assertEqual(self.calls, 1)
        client.create_operation.assert_not_called()
        events = runtime._events(envelope["operation_id"])
        self.assertEqual(
            sum(event["type"] == "accepted" for event in events),
            1,
        )
        self.assertEqual(
            sum(event["type"] == "input_admitted" for event in events),
            1,
        )

    def test_input_transfer_resume_never_creates_a_missing_operation(self) -> None:
        client = NativeOperationClient(
            NativeOperationEndpoint("http://127.0.0.1:1", TOKEN)
        )
        envelope = _operation("operation-missing-transfer", self.archive)
        client.get_operation = mock.Mock(
            side_effect=NativeOperationNotFound("missing", status=404)
        )
        client.create_operation = mock.Mock(
            side_effect=AssertionError(
                "resume transfer must not create another operation"
            )
        )
        client.upload_input = mock.Mock(
            side_effect=AssertionError(
                "missing operation has no accepted input slot"
            )
        )

        self.assertIsNone(
            client.resume_input_transfer(envelope, self.archive)
        )
        client.get_operation.assert_called_once_with(
            "operation-missing-transfer"
        )
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()

    def test_input_transfer_resume_observes_admitted_operation_without_reupload(
        self,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_executor(
            request,
            source_archive,
            workspace,
            native_tool,
        ):
            del request, source_archive, native_tool
            entered.set()
            if not release.wait(5):
                raise AssertionError("native operation was not released")
            artifact = Path(workspace) / "result.zip"
            artifact.write_bytes(b"admitted operation result")
            return NativeOperationResult(
                artifact_path=artifact,
                artifact_name="result.zip",
                output_contract=OUTPUT_CONTRACT,
                resource_terminated=True,
            )

        runtime = NativeOperationRuntime(
            state_root=self.root / "admitted-transfer-state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: blocking_executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        envelope = _operation("operation-admitted-transfer", self.archive)
        client.create_operation(envelope)
        client.create_operation = mock.Mock(
            wraps=client.create_operation
        )
        client.upload_input = mock.Mock(
            wraps=client.upload_input
        )

        first = client.resume_input_transfer(envelope, self.archive)
        self.assertIsNotNone(first)
        self.assertTrue(entered.wait(5))
        self.assertIn(
            first.state,
            {"awaiting_input", "running"},
        )
        second = client.resume_input_transfer(envelope, self.archive)
        self.assertIn(second.state, {"running", "satisfied"})
        self.assertEqual(client.create_operation.call_count, 0)
        self.assertEqual(client.upload_input.call_count, 1)
        release.set()
        self.assertEqual(
            client.wait_for_terminal(
                envelope["operation_id"],
                timeout=5,
            ).state,
            "satisfied",
        )

    def test_async_status_probe_is_one_read_and_does_not_block_the_event_loop(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: self.executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        envelope = _operation("operation-status-probe", self.archive)
        view = client.submit_operation(envelope, self.archive)
        expected = NativeOperationStatusExpectation.from_create(
            NativeOperationCreate.from_record(envelope)
        )
        def slow_read(_operation_id):
            time.sleep(0.15)
            return view

        slow_read = mock.Mock(side_effect=slow_read)
        client.get_operation = slow_read
        client.create_operation = mock.Mock(
            side_effect=AssertionError("status probe must not create")
        )
        client.upload_input = mock.Mock(
            side_effect=AssertionError("status probe must not upload")
        )
        client.cancel_operation = mock.Mock(
            side_effect=AssertionError("status probe must not cancel")
        )

        async def exercise():
            task = asyncio.create_task(
                client.probe_operation_status_async(expected)
            )
            ticks = 0
            while not task.done():
                ticks += 1
                await asyncio.sleep(0.005)
            return await task, ticks

        observed, ticks = asyncio.run(exercise())

        self.assertEqual(observed, view)
        self.assertGreater(ticks, 2)
        slow_read.assert_called_once_with("operation-status-probe")
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()
        client.cancel_operation.assert_not_called()

        with self.assertRaises(NativeOperationClientConflict):
            asyncio.run(
                client.probe_operation_status_async(
                    replace(expected, request_sha256=digest({"foreign": True}))
                )
            )

        running = replace(
            observed,
            state="running",
            actual_native_tool_sha256=expected.native_tool_requirement_sha256,
            executor_sha256=expected.native_tool_requirement_sha256,
        )
        client.get_operation = mock.Mock(
            return_value=replace(
                running,
                executor_sha256=digest({"foreign": "executor"}),
            )
        )
        with self.assertRaises(NativeOperationClientConflict) as raised:
            asyncio.run(client.probe_operation_status_async(expected))
        self.assertEqual(raised.exception.code, "native_executor_mismatch")

    def test_async_status_probe_reports_missing_without_other_calls(self) -> None:
        client = NativeOperationClient(
            NativeOperationEndpoint("http://127.0.0.1:1", TOKEN)
        )
        expected = NativeOperationStatusExpectation(
            operation_id="operation-missing",
            kind=CAPTURE_KIND,
            request_sha256=digest({"request": "missing"}),
            native_tool_requirement_sha256=digest(NATIVE_TOOL),
            input_archive_sha256=file_digest(self.archive),
            payload_sha256=digest({"payload": "missing"}),
        )
        client.get_operation = mock.Mock(
            side_effect=NativeOperationNotFound("not found", status=404)
        )
        client.create_operation = mock.Mock(
            side_effect=AssertionError("status probe must not create")
        )
        client.upload_input = mock.Mock(
            side_effect=AssertionError("status probe must not upload")
        )
        client.cancel_operation = mock.Mock(
            side_effect=AssertionError("status probe must not cancel")
        )

        observed = asyncio.run(client.probe_operation_status_async(expected))

        self.assertIsNone(observed)
        client.get_operation.assert_called_once_with("operation-missing")
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()
        client.cancel_operation.assert_not_called()

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

    def test_full_native_tool_provider_reads_windows_once(self) -> None:
        reads = 0

        def tool_factory():
            nonlocal reads
            reads += 1
            return dict(NATIVE_TOOL)

        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=tool_factory,
            executors={},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        provider = NativeOperationToolProvider(server.client())
        self.assertEqual(provider(), NATIVE_TOOL)
        self.assertEqual(provider(), NATIVE_TOOL)
        self.assertEqual(reads, 1)
        self.assertEqual(provider.identity.tool_sha256, digest(NATIVE_TOOL))
        adapter = discover_adapter_from_native_operation(
            server.client(),
            provider,
            SimpleNamespace(materialize=lambda *args: None),
        )
        self.assertEqual(adapter.port.native_tool, NATIVE_TOOL)
        self.assertEqual(reads, 1)

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

    def test_capture_executor_calls_the_shared_native_transaction(self) -> None:
        package = self.root / "prepared"
        files = {
            "cad/robot.SLDASM": b"prepared assembly",
            "source.json": b"prepared source",
        }
        for name, payload in files.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        prepared_files = package_inventory(package)
        archive = seal_source_archive(
            package,
            prepared_files,
            self.root / "prepared.zip",
        )
        captured = {}

        def transaction(
            transaction_package,
            output,
            **kwargs,
        ):
            captured["package_inventory"] = package_inventory(transaction_package)
            captured["output"] = Path(output)
            captured["kwargs"] = kwargs
            Path(output).mkdir(parents=True)
            (Path(output) / CAPTURE_ARCHIVE).write_bytes(b"sealed capture")
            return {
                "native_complete": True,
                "capture_archive": {"name": CAPTURE_ARCHIVE},
                "resource_settlement": {"settled": True},
                "output": str(output),
            }

        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: CaptureOperationExecutor()},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        envelope = _operation(
            "operation-capture-transaction",
            archive,
            package_files=prepared_files,
        )
        with mock.patch(
            "description_pipeline.pipeline.capture._capture_native_transaction",
            side_effect=transaction,
        ) as native_transaction:
            client.create_operation(envelope)
            client.upload_input("operation-capture-transaction", archive)
            result = client.wait_for_terminal("operation-capture-transaction")
        self.assertEqual(result.state, "satisfied")
        native_transaction.assert_called_once()
        self.assertEqual(captured["package_inventory"], prepared_files)
        self.assertEqual(
            captured["kwargs"]["execution_scope"],
            ("capture",),
        )
        self.assertEqual(
            captured["kwargs"]["native_tool_factory"](),
            NATIVE_TOOL,
        )

    def test_linux_capture_adapter_observes_exact_identity_and_calls_windows_once(self) -> None:
        package = self.root / "prepared-adapter"
        files = {
            "cad/robot.SLDASM": b"adapter assembly",
            "source.json": b"adapter source",
        }
        for name, payload in files.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        prepared_files = package_inventory(package)
        run_id = "run-capture-adapter"
        command_id = "command-capture-adapter"
        effect_key = "capture-native"
        operation_id = native_capture_operation_id(
            run_id=run_id,
            command_id=command_id,
            effect_key=effect_key,
        )
        execution = {
            "claim_id": "claim-capture-adapter",
            "command_id": command_id,
            "effect_key": effect_key,
            "executor_sha256": digest({"executor": "capture-adapter"}),
            "fence": 1,
            "native_tool_sha256": digest(NATIVE_TOOL),
            "owner_sha256": digest({"owner": "capture-adapter"}),
            "run_id": run_id,
        }
        freeze = _artifact_ref("freeze", run_id)
        discover = _artifact_ref("discover", run_id)
        inputs = SimpleNamespace(
            package=package,
            prepared_files=prepared_files,
            request_evidence=REQUEST_EVIDENCE,
            historical_native_tool={
                "schema_version": TOOL_SCHEMA,
                "pipeline_id": "historical-native-operation-test",
                "source_sha256": "ef" * 32,
                "runtime": {"role": "native"},
            },
        )
        calls = 0
        transaction_kwargs = {}

        def transaction(transaction_package, output, **kwargs):
            nonlocal calls, transaction_kwargs
            calls += 1
            del transaction_package
            transaction_kwargs = kwargs
            Path(output).mkdir(parents=True)
            (Path(output) / CAPTURE_ARCHIVE).write_bytes(b"remote capture")
            return {
                "native_complete": True,
                "capture_archive": {"name": CAPTURE_ARCHIVE},
                "resource_settlement": {"settled": True},
                "output": str(output),
            }

        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: CaptureOperationExecutor()},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        port = NativeOperationCapturePort(
            client,
            timeout=5,
            poll_interval=0.01,
        )
        input_archive = seal_source_archive(
            package,
            prepared_files,
            self.root / "capture-adapter-input.zip",
        )
        request_digest = digest({"request": run_id})
        handoff_sha256 = "b" * 64
        upload_sha256 = "c" * 64
        main_assembly = "cad/robot.SLDASM"
        native_request = CaptureOperationRequest.from_record(
            {
                "schema_version": CAPTURE_OPERATION_SCHEMA,
                "command_id": command_id,
                "run_id": run_id,
                "request_digest": request_digest,
                "effect_key": effect_key,
                "handoff_sha256": handoff_sha256,
                "upload_sha256": upload_sha256,
                "prepared_sha256": digest(prepared_files),
                "main_assembly": main_assembly,
                "package_files": prepared_files,
                "upstream_artifacts": [
                    freeze.to_record(),
                    discover.to_record(),
                ],
                "execution": execution,
                "native_tool_source_sha256": NATIVE_TOOL[
                    "source_sha256"
                ],
                "request_evidence": REQUEST_EVIDENCE,
            }
        )
        operation = build_operation_record(
            operation_id=operation_id,
            kind=CAPTURE_KIND,
            request=native_request.to_record(),
            input_archive_sha256=file_digest(input_archive),
            input_archive_bytes=input_archive.stat().st_size,
            native_tool_requirement_sha256=digest(NATIVE_TOOL),
            output_contract=CAPTURE_TRANSFER_SCHEMA,
        )
        dispatch = CaptureEffectDispatch(
            effect_id="effect-capture-adapter",
            settlement=None,
            outbound_root=self.root,
            outbound={
                "operation_id": operation_id,
                "payload_sha256": operation["payload_sha256"],
                "input_archive_sha256": operation["input"][
                    "archive_sha256"
                ],
                "input_archive_bytes": operation["input"][
                    "archive_bytes"
                ],
                "execution": execution,
            },
            operation=operation,
            input_archive=input_archive,
            execution=execution,
        )
        self.assertIsNone(
            port.observe(
                run_id=run_id,
                command_id=command_id,
                effect_key=effect_key,
            )
        )
        with self.assertRaises(NativeOperationNotFound):
            client.get_operation(operation_id)
        self.assertEqual(runtime.stats()["queued"], 0)

        with mock.patch(
            "description_pipeline.pipeline.capture._capture_native_transaction",
            side_effect=transaction,
        ):
            first = port(
                inputs,
                self.root / "linux-output",
                backend=None,
                run_id=run_id,
                request_digest=digest({"request": run_id}),
                handoff_sha256=handoff_sha256,
                upload_sha256=upload_sha256,
                main_assembly=main_assembly,
                upstream_artifacts=(freeze.to_record(), discover.to_record()),
                native_tool=NATIVE_TOOL,
                sealed_dispatch=dispatch,
            )
            second = port(
                inputs,
                self.root / "linux-output",
                backend=None,
                run_id=run_id,
                request_digest=digest({"request": run_id}),
                handoff_sha256=handoff_sha256,
                upload_sha256=upload_sha256,
                main_assembly=main_assembly,
                upstream_artifacts=(freeze.to_record(), discover.to_record()),
                native_tool=NATIVE_TOOL,
                sealed_dispatch=dispatch,
            )
        self.assertEqual(first.outcome, "completed")
        self.assertEqual(second.outcome, "completed")
        self.assertEqual(calls, 1)
        self.assertEqual(
            transaction_kwargs["request_evidence"],
            REQUEST_EVIDENCE,
        )
        remote = port.observe(
            run_id=run_id,
            command_id=command_id,
            effect_key=effect_key,
        )
        self.assertEqual(remote.state, "satisfied")
        self.assertEqual(remote.actual_native_tool_sha256, digest(NATIVE_TOOL))
        handler = capture_handler_from_native_operation(client)
        self.assertEqual(handler._native_tool_factory(), NATIVE_TOOL)

    def test_sealed_capture_dispatch_is_sent_once_without_rebuilding(self) -> None:
        archive = self.root / "sealed-capture-input.zip"
        archive.write_bytes(b"sealed capture input archive")
        run_id = "run-sealed-capture"
        command_id = "command-sealed-capture"
        effect_key = "capture-native"
        operation_id = native_capture_operation_id(
            run_id=run_id,
            command_id=command_id,
            effect_key=effect_key,
        )
        sealed_execution = {
            "claim_id": "claim-original",
            "command_id": command_id,
            "effect_key": effect_key,
            "executor_sha256": digest({"executor": "original-platform"}),
            "fence": 1,
            "native_tool_sha256": digest(NATIVE_TOOL),
            "owner_sha256": digest({"owner": "original-platform"}),
            "run_id": run_id,
        }
        request = {
            "schema_version": CAPTURE_OPERATION_SCHEMA,
            "command_id": command_id,
            "run_id": run_id,
            "request_digest": digest({"request": run_id}),
            "effect_key": effect_key,
            "handoff_sha256": "b" * 64,
            "upload_sha256": "c" * 64,
            "prepared_sha256": digest(
                {"cad/robot.SLDASM": "a" * 64}
            ),
            "main_assembly": "cad/robot.SLDASM",
            "package_files": {"cad/robot.SLDASM": "a" * 64},
            "upstream_artifacts": [
                _artifact(stage, run_id)
                for stage in ("freeze", "discover")
            ],
            "execution": sealed_execution,
            "native_tool_source_sha256": NATIVE_TOOL["source_sha256"],
            "request_evidence": REQUEST_EVIDENCE,
        }
        envelope = _operation(
            operation_id,
            archive,
            request=request,
        )
        sealed_dispatch = SimpleNamespace(
            operation=envelope,
            input_archive=archive,
            execution=sealed_execution,
        )
        observed = []

        def executor(
            received_request,
            source_archive,
            workspace,
            native_tool,
        ):
            del native_tool
            observed.append(
                (
                    copy.deepcopy(received_request),
                    Path(source_archive).read_bytes(),
                )
            )
            artifact = Path(workspace) / CAPTURE_ARCHIVE
            artifact.write_bytes(b"sealed remote capture")
            return NativeOperationResult(
                artifact_path=artifact,
                artifact_name=CAPTURE_ARCHIVE,
                output_contract=CAPTURE_TRANSFER_SCHEMA,
                resource_terminated=True,
            )

        runtime = NativeOperationRuntime(
            state_root=self.root / "sealed-state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        client.create_operation = mock.Mock(
            wraps=client.create_operation
        )
        client.upload_input = mock.Mock(
            wraps=client.upload_input
        )
        port = NativeOperationCapturePort(
            client,
            timeout=5,
            poll_interval=0.01,
        )
        common = {
            "backend": None,
            "run_id": run_id,
            "request_digest": request["request_digest"],
            "handoff_sha256": request["handoff_sha256"],
            "upload_sha256": request["upload_sha256"],
            "main_assembly": request["main_assembly"],
            "upstream_artifacts": tuple(request["upstream_artifacts"]),
            "native_tool": NATIVE_TOOL,
            "sealed_dispatch": sealed_dispatch,
        }

        first = port(
            None,
            self.root / "sealed-output-1",
            **common,
        )
        second = port(
            None,
            self.root / "sealed-output-2",
            **common,
        )

        self.assertEqual(first.outcome, "completed")
        self.assertEqual(second.outcome, "completed")
        self.assertEqual(observed, [(request, archive.read_bytes())])
        self.assertEqual(client.create_operation.call_count, 1)
        self.assertEqual(client.upload_input.call_count, 1)
        stored_intent = runtime._intent(operation_id)[1]
        self.assertEqual(stored_intent.to_record(), envelope)

    def test_unknown_sealed_capture_dispatch_is_never_resent(self) -> None:
        archive = self.root / "unknown-capture-input.zip"
        archive.write_bytes(b"unknown capture input archive")
        run_id = "run-unknown-sealed"
        command_id = "command-unknown-sealed"
        effect_key = "capture-native"
        operation_id = native_capture_operation_id(
            run_id=run_id,
            command_id=command_id,
            effect_key=effect_key,
        )
        sealed_execution = {
            "claim_id": "claim-unknown-original",
            "command_id": command_id,
            "effect_key": effect_key,
            "executor_sha256": digest({"executor": "original-platform"}),
            "fence": 1,
            "native_tool_sha256": digest(NATIVE_TOOL),
            "owner_sha256": digest({"owner": "original-platform"}),
            "run_id": run_id,
        }
        request = {
            "schema_version": CAPTURE_OPERATION_SCHEMA,
            "command_id": command_id,
            "run_id": run_id,
            "request_digest": digest({"request": run_id}),
            "effect_key": effect_key,
            "handoff_sha256": "b" * 64,
            "upload_sha256": "c" * 64,
            "prepared_sha256": digest(
                {"cad/robot.SLDASM": "a" * 64}
            ),
            "main_assembly": "cad/robot.SLDASM",
            "package_files": {"cad/robot.SLDASM": "a" * 64},
            "upstream_artifacts": [
                _artifact(stage, run_id)
                for stage in ("freeze", "discover")
            ],
            "execution": sealed_execution,
            "native_tool_source_sha256": NATIVE_TOOL["source_sha256"],
            "request_evidence": REQUEST_EVIDENCE,
        }

        def executor(request, source_archive, workspace, native_tool):
            del request, source_archive, workspace, native_tool
            raise NativeOperationUnresolved(
                "capture.native_unknown",
                "native Capture result is unknown",
                resource_terminated=True,
            )

        envelope = _operation(operation_id, archive, request=request)
        sealed_dispatch = SimpleNamespace(
            operation=envelope,
            input_archive=archive,
            execution=sealed_execution,
        )
        runtime = NativeOperationRuntime(
            state_root=self.root / "unknown-sealed-state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        client.create_operation = mock.Mock(
            wraps=client.create_operation
        )
        client.upload_input = mock.Mock(
            wraps=client.upload_input
        )
        port = NativeOperationCapturePort(
            client,
            timeout=5,
            poll_interval=0.01,
        )
        common = {
            "backend": None,
            "run_id": run_id,
            "request_digest": request["request_digest"],
            "handoff_sha256": request["handoff_sha256"],
            "upload_sha256": request["upload_sha256"],
            "main_assembly": request["main_assembly"],
            "upstream_artifacts": tuple(request["upstream_artifacts"]),
            "native_tool": NATIVE_TOOL,
            "sealed_dispatch": sealed_dispatch,
        }

        first = port(
            None,
            self.root / "unknown-sealed-output-1",
            **common,
        )
        second = port(
            None,
            self.root / "unknown-sealed-output-2",
            **common,
        )

        self.assertEqual(first.outcome, "unknown")
        self.assertEqual(second.outcome, "unknown")
        self.assertEqual(client.create_operation.call_count, 1)
        self.assertEqual(client.upload_input.call_count, 1)

    def test_capture_transaction_failure_retains_diagnostic_bytes(self) -> None:
        package = self.root / "prepared-failure"
        files = {
            "cad/robot.SLDASM": b"failure assembly",
            "source.json": b"failure source",
        }
        for name, payload in files.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        prepared_files = package_inventory(package)
        archive = seal_source_archive(
            package,
            prepared_files,
            self.root / "prepared-failure.zip",
        )

        def transaction(transaction_package, output, **kwargs):
            del transaction_package, kwargs
            diagnostic = Path(output)
            diagnostic.mkdir(parents=True)
            (diagnostic / "raw-failure.bin").write_bytes(b"native failure bytes")
            return {
                "state": "failed",
                "error": "capture failed",
                "error_code": "capture.native_failed",
                "diagnostic_path": str(diagnostic),
                "resource_settlement": {"settled": True},
            }

        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: CaptureOperationExecutor()},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        with mock.patch(
            "description_pipeline.pipeline.capture._capture_native_transaction",
            side_effect=transaction,
        ):
            client.create_operation(
                _operation(
                    "operation-capture-failure",
                    archive,
                    package_files=prepared_files,
                )
            )
            client.upload_input("operation-capture-failure", archive)
            failed = client.wait_for_terminal("operation-capture-failure")
        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.failure.code, "capture.native_failed")
        self.assertTrue(failed.resource_terminated)
        claim_id = next(
            event["payload"]["claim_id"]
            for event in runtime._events("operation-capture-failure")
            if event["type"] == "claimed"
        )
        evidence = (
            runtime.workspace_root
            / "operation-capture-failure"
            / claim_id
            / "capture-output"
            / "raw-failure.bin"
        )
        self.assertEqual(evidence.read_bytes(), b"native failure bytes")

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

    def test_full_current_tool_record_mismatch_never_calls_cad(self) -> None:
        current_variant = copy.deepcopy(NATIVE_TOOL)
        current_variant["runtime"]["execution_variant"] = "different-windows-record"
        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: current_variant,
            executors={CAPTURE_KIND: self.executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        client.create_operation(_operation("operation-full-tool-mismatch", self.archive))
        client.upload_input("operation-full-tool-mismatch", self.archive)

        failed = client.wait_for_terminal("operation-full-tool-mismatch")

        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.failure.code, "native_tool_mismatch")
        self.assertEqual(
            failed.actual_native_tool_sha256,
            digest(current_variant),
        )
        self.assertNotEqual(
            failed.actual_native_tool_sha256,
            digest(NATIVE_TOOL),
        )
        self.assertEqual(self.calls, 0)

    def test_definite_native_failure_preserves_workspace_bytes(self) -> None:
        def failing_executor(request, source_archive, workspace, native_tool):
            del request, source_archive, native_tool
            evidence = Path(workspace) / "partial-native.bin"
            evidence.write_bytes(b"unsealed failure evidence")
            raise NativeOperationFailure(
                "capture.native_failed",
                "native capture failed after writing evidence",
                resource_terminated=True,
            )

        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: failing_executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        client.create_operation(_operation("operation-failed-evidence", self.archive))
        client.upload_input("operation-failed-evidence", self.archive)
        failed = client.wait_for_terminal("operation-failed-evidence")
        self.assertEqual(failed.state, "failed")
        claim_id = next(
            event["payload"]["claim_id"]
            for event in runtime._events("operation-failed-evidence")
            if event["type"] == "claimed"
        )
        evidence = (
            runtime.workspace_root
            / "operation-failed-evidence"
            / claim_id
            / "partial-native.bin"
        )
        self.assertEqual(evidence.read_bytes(), b"unsealed failure evidence")

    def test_artifact_seal_failure_preserves_unsealed_workspace(self) -> None:
        def returning(request, source_archive, workspace, native_tool):
            del request, source_archive, native_tool
            (Path(workspace) / "unsealed.bin").write_bytes(b"must survive")
            artifact = Path(workspace) / "result.zip"
            artifact.write_bytes(b"candidate artifact")
            return NativeOperationResult(
                artifact_path=artifact,
                artifact_name="result.zip",
                output_contract=OUTPUT_CONTRACT,
                resource_terminated=True,
            )

        runtime = NativeOperationRuntime(
            state_root=self.root / "state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={CAPTURE_KIND: returning},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        with mock.patch.object(
            runtime,
            "_install_artifact",
            side_effect=RuntimeError("forced seal failure"),
        ):
            client.create_operation(_operation("operation-seal-failure", self.archive))
            client.upload_input("operation-seal-failure", self.archive)
            failed = client.wait_for_terminal("operation-seal-failure")
        self.assertEqual(failed.state, "unknown")
        self.assertTrue(failed.resource_terminated)
        self.assertEqual(failed.failure.code, "native_artifact_seal_failed")
        claim_id = next(
            event["payload"]["claim_id"]
            for event in runtime._events("operation-seal-failure")
            if event["type"] == "claimed"
        )
        evidence = (
            runtime.workspace_root
            / "operation-seal-failure"
            / claim_id
            / "unsealed.bin"
        )
        self.assertEqual(evidence.read_bytes(), b"must survive")

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
            expected_native_tool_record_sha256=digest(NATIVE_TOOL),
            native_tool_requirement_artifact_id=(
                "native-tool-requirement-fixture"
            ),
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

    def submit_and_wait(
        self,
        port: NativeOperationDiscoverPort,
        client: NativeOperationClient,
        archive: Path,
        request: NativeDiscoverRequest,
        workspace: Path,
    ):
        observation = port.submit(archive, request, workspace)
        if observation is None:
            client.wait_for_terminal(request.operation_id, timeout=5)
            observation = port.submit(archive, request, workspace)
        self.assertIsNotNone(observation)
        return observation

    def observer_fixture(
        self,
        operation_id: str,
        workspace: Path,
        *,
        archive: Path | None = None,
    ):
        if archive is None:
            archive = seal_source_archive(
                self.source,
                self.inventory,
                workspace / "input-source-set.zip",
            )
        request = self.request(operation_id)
        outbound = workspace / "outbound"
        outbound.mkdir()
        write_json(outbound / "request.json", request.to_record())
        write_json(
            outbound / "source.json",
            {
                "source_files": dict(request.source_files),
                "source_inventory_sha256": request.source_inventory_sha256,
            },
        )
        (outbound / "source-set.zip").write_bytes(archive.read_bytes())
        command = SimpleNamespace(
            stage="discover",
            command_id=operation_id,
            run_id=request.run_id,
            request_digest=request.request_digest,
            request=request.to_record(),
        )
        intent = SimpleNamespace(
            effect_kind="native_operation",
            command_id=operation_id,
            effect_key=f"native-discover:{operation_id}",
            target={"operation_id": operation_id},
            expected_state={
                "request_digest": request.request_digest,
                "source_archive_sha256": file_digest(archive),
                "native_tool_record_sha256": (
                    request.expected_native_tool_record_sha256
                ),
                "native_tool_requirement_artifact_id": (
                    request.native_tool_requirement_artifact_id
                ),
            },
        )
        return command, intent, outbound

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
        workspace = self.root / "workspace-single"
        archive = seal_source_archive(
            self.source,
            self.inventory,
            workspace / "input-source-set.zip",
        )
        request = self.request("discover-operation-single")
        self.assertIsNone(port.observe(request))

        with mock.patch(
            "description_pipeline.pipeline.capture._capture_native_transaction",
            capture,
        ):
            observation = self.submit_and_wait(
                port,
                client,
                archive,
                request,
                workspace,
            )

        self.assertEqual(reader.calls, 1)
        self.assertEqual(backend.calls, 1)
        capture.assert_not_called()
        self.assertEqual(
            set(observation.selection["files"]),
            {self.MAIN, self.SELECTED},
        )
        self.assertNotIn(self.DECOY, observation.selection["files"])

        remote = client.get_operation("discover-operation-single")
        self.assertEqual(remote.state, "satisfied")
        self.assertTrue(remote.resource_terminated)
        self.assertEqual(remote.kind, DISCOVER_KIND)
        self.assertEqual(remote.artifact.media_type, "application/zip")
        repeated = port.submit(
            archive,
            request,
            workspace,
        )
        self.assertIsNotNone(repeated)
        self.assertEqual(
            repeated.observation_sha256,
            observation.observation_sha256,
        )
        self.assertEqual(reader.calls, 1)
        self.assertEqual(backend.calls, 1)

    def test_discover_submit_returns_without_waiting_and_reuses_operation(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class GatedBackend(_CountingBackend):
            def discover_native(
                self,
                frozen_source,
                settings,
                *,
                on_activity=None,
            ):
                del frozen_source, settings, on_activity
                self.calls += 1
                entered.set()
                if not release.wait(5):
                    raise AssertionError("native Discover was not released")
                return copy.deepcopy(self.payload)

        reader = _ClosureReader(self.inventory, self.MAIN, self.edges)
        backend = GatedBackend(copy.deepcopy(self.native_record))
        client = self.runtime(reader, backend)
        client.create_operation = mock.Mock(
            wraps=client.create_operation
        )
        client.upload_input = mock.Mock(
            wraps=client.upload_input
        )
        port = NativeOperationDiscoverPort(
            client,
            NATIVE_TOOL,
            timeout=5,
            poll_interval=0.01,
        )
        workspace = self.root / "workspace-nonblocking-submit"
        archive = seal_source_archive(
            self.source,
            self.inventory,
            workspace / "input-source-set.zip",
        )
        request = self.request("discover-operation-nonblocking")

        self.assertIsNone(
            port.submit(archive, request, workspace / "operation")
        )
        self.assertTrue(entered.wait(5))
        self.assertEqual(backend.calls, 1)
        self.assertEqual(client.create_operation.call_count, 1)
        self.assertEqual(client.upload_input.call_count, 1)

        release.set()
        client.wait_for_terminal(request.operation_id, timeout=5)
        observation = port.submit(
            archive,
            request,
            workspace / "operation",
        )

        self.assertIsNotNone(observation)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(client.create_operation.call_count, 1)
        self.assertEqual(client.upload_input.call_count, 1)

    def test_effect_observer_downloads_satisfied_discover_without_posting(self) -> None:
        reader = _ClosureReader(self.inventory, self.MAIN, self.edges)
        backend = _CountingBackend(copy.deepcopy(self.native_record))
        client = self.runtime(reader, backend)
        port = NativeOperationDiscoverPort(
            client,
            NATIVE_TOOL,
            timeout=5,
            poll_interval=0.01,
        )
        workspace = self.root / "workspace-observer"
        archive = seal_source_archive(
            self.source,
            self.inventory,
            workspace / "input-source-set.zip",
        )
        request = self.request("discover-operation-observer")
        self.submit_and_wait(
            port,
            client,
            archive,
            request,
            workspace / "operation",
        )
        command, intent, outbound = self.observer_fixture(
            request.operation_id,
            workspace,
            archive=archive,
        )
        client.create_operation = mock.Mock(
            side_effect=AssertionError("observer must not create")
        )
        client.upload_input = mock.Mock(
            side_effect=AssertionError("observer must not upload")
        )
        client.cancel_operation = mock.Mock(
            side_effect=AssertionError("observer must not cancel")
        )

        evidence = NativeOperationEffectObserver(client).observe(
            command=command,
            intent=intent,
            outbound_root=outbound,
            workspace_root=workspace,
        )

        self.assertIsInstance(evidence, EffectReconciliationEvidence)
        self.assertEqual(evidence.fact_type, "reconciled")
        self.assertEqual(evidence.state_class, "known")
        self.assertIsNotNone(evidence.payload_root)
        self.assertEqual(
            {path.name for path in evidence.payload_root.iterdir()},
            {
                "observation.json",
                "record.json",
                "request.json",
                "selection.json",
                "source.json",
                "tool.json",
            },
        )
        self.assertEqual(
            evidence.metadata["actual_native_tool_sha256"],
            digest(NATIVE_TOOL),
        )
        identity = evidence.metadata["identity"]
        self.assertIsNone(identity["platform_origin"])
        self.assertEqual(
            identity["native_runtime"]["owner_binding"],
            "native_claim_projection",
        )
        self.assertEqual(
            identity["native_runtime"]["executor_sha256"],
            digest(NATIVE_TOOL),
        )
        self.assertEqual(
            identity["native_runtime"]["executor_binding"],
            "tool_observed_and_archive",
        )
        self.assertEqual(reader.calls, 1)
        self.assertEqual(backend.calls, 1)
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()
        client.cancel_operation.assert_not_called()

    def test_effect_observer_rejects_foreign_discover_executor(self) -> None:
        reader = _ClosureReader(self.inventory, self.MAIN, self.edges)
        backend = _CountingBackend(copy.deepcopy(self.native_record))
        client = self.runtime(reader, backend)
        port = NativeOperationDiscoverPort(
            client,
            NATIVE_TOOL,
            timeout=5,
            poll_interval=0.01,
        )
        workspace = self.root / "workspace-observer-foreign-executor"
        archive = seal_source_archive(
            self.source,
            self.inventory,
            workspace / "input-source-set.zip",
        )
        request = self.request("discover-operation-foreign-executor")
        self.submit_and_wait(
            port,
            client,
            archive,
            request,
            workspace / "operation",
        )
        command, intent, outbound = self.observer_fixture(
            request.operation_id,
            workspace,
            archive=archive,
        )
        observed = client.get_operation(request.operation_id)
        client.get_operation = mock.Mock(
            return_value=replace(
                observed,
                executor_sha256=digest({"foreign": "discover-executor"}),
            )
        )
        client.download_artifact = mock.Mock(
            side_effect=AssertionError(
                "executor mismatch must be rejected before artifact download"
            )
        )

        with self.assertRaises(NativeOperationClientConflict) as raised:
            NativeOperationEffectObserver(client).observe(
                command=command,
                intent=intent,
                outbound_root=outbound,
                workspace_root=workspace,
            )

        self.assertEqual(raised.exception.code, "native_executor_mismatch")
        client.download_artifact.assert_not_called()

    def test_download_identity_recheck_rejects_a_changed_operation(self) -> None:
        reader = _ClosureReader(self.inventory, self.MAIN, self.edges)
        backend = _CountingBackend(copy.deepcopy(self.native_record))
        client = self.runtime(reader, backend)
        port = NativeOperationDiscoverPort(
            client,
            NATIVE_TOOL,
            timeout=5,
            poll_interval=0.01,
        )
        workspace = self.root / "workspace-observer-changed"
        archive = seal_source_archive(
            self.source,
            self.inventory,
            workspace / "input-source-set.zip",
        )
        request = self.request("discover-operation-observer-changed")
        self.submit_and_wait(
            port,
            client,
            archive,
            request,
            workspace / "operation",
        )
        command, intent, outbound = self.observer_fixture(
            request.operation_id,
            workspace,
            archive=archive,
        )
        observed = client.get_operation(request.operation_id)
        changed = replace(observed, payload_sha256=digest({"changed": True}))
        client.get_operation = mock.Mock(
            side_effect=[observed, observed, changed]
        )

        with self.assertRaisesRegex(
            NativeOperationClientConflict,
            "changed while its artifact was observed",
        ):
            NativeOperationEffectObserver(client).observe(
                command=command,
                intent=intent,
                outbound_root=outbound,
                workspace_root=workspace,
            )

    def test_cancel_race_uses_complete_artifact_and_resource_release(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class GatedBackend(_CountingBackend):
            def discover_native(self, frozen_source, settings, *, on_activity=None):
                del frozen_source, settings, on_activity
                entered.set()
                if not release.wait(5):
                    raise AssertionError("gated backend was not released")
                return copy.deepcopy(self.payload)

        reader = _ClosureReader(self.inventory, self.MAIN, self.edges)
        backend = GatedBackend(copy.deepcopy(self.native_record))
        client = self.runtime(reader, backend)
        request = self.request("discover-operation-cancel-race")
        workspace = self.root / "cancel-race"
        archive = seal_source_archive(
            self.source,
            self.inventory,
            workspace / "input-source-set.zip",
        )
        client.create_operation(
            _operation(
                request.operation_id,
                archive,
                kind=DISCOVER_KIND,
                request=request.to_record(),
                output_contract=NATIVE_OBSERVATION_SCHEMA,
            )
        )
        client.upload_input(request.operation_id, archive)
        self.assertTrue(entered.wait(5))
        cancelled = client.cancel_operation(request.operation_id)
        self.assertEqual(cancelled.state, "unknown")
        release.set()
        settled = _wait_for(
            lambda: (
                current
                if (
                    current := client.get_operation(request.operation_id)
                ).artifact
                is not None
                and current.resource_terminated
                else None
            )
        )
        self.assertEqual(settled.state, "unknown")
        (workspace / "observer").mkdir()
        command, intent, outbound = self.observer_fixture(
            request.operation_id,
            workspace / "observer",
            archive=archive,
        )
        client.create_operation = mock.Mock(
            side_effect=AssertionError("observer must not create")
        )
        client.upload_input = mock.Mock(
            side_effect=AssertionError("observer must not upload")
        )
        client.cancel_operation = mock.Mock(
            side_effect=AssertionError("observer must not cancel")
        )

        evidence = NativeOperationEffectObserver(client).observe(
            command=command,
            intent=intent,
            outbound_root=outbound,
            workspace_root=workspace / "observer",
        )

        self.assertEqual(evidence.fact_type, "reconciled")
        self.assertEqual(evidence.state_class, "known")
        self.assertEqual(evidence.metadata["operation_state"], "unknown")
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()
        client.cancel_operation.assert_not_called()

    def test_missing_or_unreachable_operation_stays_an_observation(self) -> None:
        runtime = NativeOperationRuntime(
            state_root=self.root / "missing-state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        missing_workspace = self.root / "missing-workspace"
        missing_workspace.mkdir()
        missing_command, missing_intent, missing_outbound = (
            self.observer_fixture(
                "discover-operation-missing",
                missing_workspace,
            )
        )
        missing_client = server.client()
        missing = NativeOperationEffectObserver(missing_client).observe(
            command=missing_command,
            intent=missing_intent,
            outbound_root=missing_outbound,
            workspace_root=missing_workspace,
        )
        self.assertEqual(missing.fact_type, "observation")
        self.assertEqual(missing.state_class, "absent")
        self.assertEqual(missing.metadata["http_status"], 404)

        timeout_workspace = self.root / "timeout-workspace"
        timeout_workspace.mkdir()
        timeout_command, timeout_intent, timeout_outbound = (
            self.observer_fixture(
                "discover-operation-timeout",
                timeout_workspace,
            )
        )

        def timeout_opener(_request, *, timeout):
            del timeout
            raise urlerror.URLError(TimeoutError("timeout"))

        timeout_client = NativeOperationClient(
            NativeOperationEndpoint("http://127.0.0.1:1", TOKEN),
            opener=timeout_opener,
        )
        unobservable = NativeOperationEffectObserver(timeout_client).observe(
            command=timeout_command,
            intent=timeout_intent,
            outbound_root=timeout_outbound,
            workspace_root=timeout_workspace,
        )
        self.assertEqual(unobservable.fact_type, "observation")
        self.assertEqual(unobservable.state_class, "unobservable")

    def test_only_verified_resource_termination_without_output_is_absent(self) -> None:
        def unknown_executor(request, source_archive, workspace, native_tool):
            del request, source_archive, workspace, native_tool
            raise RuntimeError("native result is unknown")

        runtime = NativeOperationRuntime(
            state_root=self.root / "terminal-state",
            native_tool_factory=lambda: NATIVE_TOOL,
            executors={DISCOVER_KIND: unknown_executor},
        )
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        workspace = self.root / "terminal-workspace"
        workspace.mkdir()
        command, intent, outbound = self.observer_fixture(
            "discover-operation-terminal",
            workspace,
        )
        request = NativeDiscoverRequest.from_record(command.request)
        archive = outbound / "source-set.zip"
        client = server.client()
        envelope = build_operation_record(
            operation_id=request.operation_id,
            kind=DISCOVER_KIND,
            request=request.to_record(),
            input_archive_sha256=file_digest(archive),
            input_archive_bytes=archive.stat().st_size,
            native_tool_requirement_sha256=(
                request.expected_native_tool_record_sha256
            ),
            output_contract=NATIVE_OBSERVATION_SCHEMA,
        )
        client.create_operation(envelope)
        client.upload_input(request.operation_id, archive)
        operation = client.wait_for_terminal(request.operation_id)
        self.assertEqual(operation.state, "unknown")
        self.assertFalse(operation.resource_terminated)
        observer = NativeOperationEffectObserver(client)
        unresolved = observer.observe(
            command=command,
            intent=intent,
            outbound_root=outbound,
            workspace_root=workspace,
        )
        self.assertEqual(unresolved.fact_type, "observation")
        self.assertEqual(unresolved.state_class, "unobservable")

        runtime.reconcile_resource_termination(
            request.operation_id,
            evidence_sha256=digest({"process": "terminated"}),
            reason="native_external_process_gone",
        )
        reconciled = observer.observe(
            command=command,
            intent=intent,
            outbound_root=outbound,
            workspace_root=workspace,
        )
        self.assertEqual(reconciled.fact_type, "reconciled")
        self.assertEqual(reconciled.state_class, "absent")

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
