"""Real PostgreSQL and loopback proof for native operation cancellation."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.io import digest, file_digest, write_json
from description_pipeline.orchestration.discover_protocol import (
    SOURCE_ARCHIVE,
    seal_source_archive,
)
from description_pipeline.orchestration.native_operation_cancellation import (
    native_operation_cancellation_adapter,
)
from description_pipeline.orchestration.native_operation_client import (
    NativeOperationClient,
    NativeOperationEndpoint,
)
from description_pipeline.orchestration.native_operation_runtime import (
    DISCOVER_KIND,
    NativeFailureView,
    NativeOperationView,
    OPERATION_VIEW_SCHEMA,
    build_operation_record,
)
from description_pipeline.pipeline.cancellation import (
    CancellationControlService,
)
from description_pipeline.pipeline.contracts import ProtocolError, StageCommand
from description_pipeline.pipeline.engine import execute_cancellation
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.services import (
    CanonicalStageServices,
    ClaimPolicy,
)
from description_pipeline.platform.facts import FactStore
from description_pipeline.schemas import (
    NATIVE_DISCOVER_REQUEST_SCHEMA,
    NATIVE_OBSERVATION_SCHEMA,
)
from description_pipeline.sources.solidworks.discover_stage import (
    NativeDiscoverRequest,
)
from tests.v1.test_pipeline_admission import AdmissionHarness

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = Path(__file__).resolve().parents[2] / "deploy" / "platform" / "schema.sql"
TOKEN = "n" * 32


class _CancelServer:
    def __init__(self, *, operation_id: str, view: NativeOperationView, mode: str) -> None:
        self.operation_id = operation_id
        self.view = view
        self.mode = mode
        self.post_paths: list[str] = []
        self.get_count = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                owner.get_count += 1
                self.send_error(404)

            def do_POST(self):
                owner.post_paths.append(self.path)
                expected = f"/operations/{owner.operation_id}/cancel"
                if self.path != expected:
                    self.send_error(409)
                    return
                if owner.mode == "lost":
                    self.close_connection = True
                    return
                if owner.mode == "not-found":
                    self.send_error(404)
                    return
                payload = json.dumps(
                    owner.view.to_record(),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    @property
    def endpoint(self) -> NativeOperationEndpoint:
        host, port = self.server.server_address
        return NativeOperationEndpoint(
            f"http://{host}:{port}",
            TOKEN,
            timeout=1,
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise AssertionError("cancel loopback server did not stop")


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class NativeCancellationAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(
            prefix="native-cancel-postgres-"
        )
        cls.server = pgserver.get_server(
            Path(cls.server_root.name) / "pgdata",
            cleanup_mode="delete",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(
            prefix="native-cancel-runtime-"
        )
        self.addCleanup(self.directory.cleanup)
        self.schema = "native_cancel_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{self.schema}"',
        )
        with (
            psycopg2.connect(self.server.get_uri()) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
            cursor.execute(schema_sql)
            cursor.execute(
                f'UPDATE "{self.schema}".admission SET state = %s WHERE id = 1',
                ("open",),
            )

        self.harness = AdmissionHarness(
            Path(self.directory.name) / "admission"
        )
        self.artifacts = self.harness.store
        self.facts = FactStore(self._connect, self.artifacts)
        self.now = datetime(2026, 10, 25, 8, tzinfo=UTC)
        self.cancel_executor = digest({"transport": "native-cancel"})

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _target(self):
        from description_pipeline.pipeline.admission import (
            build_freeze_command,
            build_run_admission,
        )
        from description_pipeline.pipeline.contracts import (
            DispatchCommand,
            DispatchConf,
        )
        from description_pipeline.schemas import AIRFLOW_DISPATCH_CONF_SCHEMA

        admission = build_run_admission(self.harness.spec)
        freeze = build_freeze_command(self.harness.spec)
        dispatch_executor = digest({"transport": "airflow"})
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=admission.run_id,
            freeze_command_id=freeze.command_id,
        )
        conf_artifact = self.artifacts.seal_dispatch_conf(
            conf,
            producer_run_id=admission.run_id,
            producer_tool_sha256=dispatch_executor,
        )
        dispatch = DispatchCommand.create(
            command_id="dispatch-" + admission.run_id,
            run_id=admission.run_id,
            freeze_command_id=freeze.command_id,
            idempotency_key=f"{admission.run_id}/dispatch",
            dag_id="solidworks_to_urdf",
            dag_run_id=admission.request_id,
            conf_artifact=conf_artifact,
            expected_executor_sha256=dispatch_executor,
        )
        _, _, _ = self.facts.admit_run(
            admission,
            freeze,
            dispatch,
            conf_artifact,
        )
        target_command = StageCommand.create(
            command_id="discover-" + digest({"target": admission.run_id}),
            run_id=admission.run_id,
            stage="discover",
            idempotency_key=f"{admission.run_id}/native-cancel",
            request={
                "expected_native_tool_sha256": (
                    self.harness.spec.native_tool_source_sha256
                ),
                "expected_native_tool_record_sha256": (
                    self.harness.spec.native_tool_record_sha256
                ),
                "native_tool_requirement_artifact_id": (
                    self.harness.spec.native_tool_requirement.ref.artifact_id
                ),
            },
            input_artifacts=(
                self.harness.spec.originals.ref,
                self.harness.spec.ruleset.artifact.ref,
                self.harness.spec.configuration.ref,
            ),
            config_snapshot_sha256=self.harness.spec.platform_config_sha256,
            contract_sha256=self.harness.spec.contract_sha256,
            policy_sha256=self.harness.spec.policy_sha256,
            expected_executor_sha256=(
                self.harness.spec.executors.discover
            ),
            predecessor_decision_id=None,
        )
        with self.facts._transaction() as connection:
            self.facts._insert_command_cursor(
                connection.cursor(),
                target_command,
            )

        services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "target-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "target"}),
                executor_sha256=self.harness.spec.executors.discover,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now,
        )
        claim = services.acquire_claim(target_command)
        workspace = services.materialize(
            target_command,
            claim,
            candidate=None,
        )
        self.addCleanup(services.discard_workspace, workspace)
        source_root = workspace.inputs[self.harness.spec.originals.ref.artifact_id]
        request = NativeDiscoverRequest(
            schema_version=NATIVE_DISCOVER_REQUEST_SCHEMA,
            operation_id=target_command.command_id,
            command_id=target_command.command_id,
            run_id=target_command.run_id,
            request_digest=target_command.request_digest,
            source_files=self.harness.spec.originals.inventory,
            source_inventory_sha256=(
                self.harness.spec.originals.inventory_sha256
            ),
            main_assembly=self.harness.spec.main_assembly,
            purpose=self.harness.spec.purpose,
            expected_native_tool_sha256=(
                self.harness.spec.native_tool_source_sha256
            ),
            expected_native_tool_record_sha256=(
                self.harness.spec.native_tool_record_sha256
            ),
            native_tool_requirement_artifact_id=(
                self.harness.spec.native_tool_requirement.ref.artifact_id
            ),
        )
        outbound = workspace.root / "native-outbound"
        outbound.mkdir()
        write_json(outbound / "request.json", request.to_record())
        write_json(
            outbound / "source.json",
            {
                "source_files": dict(request.source_files),
                "source_inventory_sha256": request.source_inventory_sha256,
            },
        )
        archive = seal_source_archive(
            source_root,
            request.source_files,
            outbound / SOURCE_ARCHIVE,
        )
        effects = services.effect_port(target_command, claim, workspace)
        effect = effects.prepare_effect(
            effect_key=f"native-discover:{target_command.command_id}",
            effect_kind="native_operation",
            target={
                "operation_id": request.operation_id,
                "main_assembly": request.main_assembly,
            },
            expected_state={
                "schema_version": NATIVE_OBSERVATION_SCHEMA,
                "operation_id": request.operation_id,
                "request_digest": target_command.request_digest,
                "source_inventory_sha256": request.source_inventory_sha256,
                "source_archive_sha256": file_digest(archive),
                "native_tool_sha256": request.expected_native_tool_sha256,
                "native_tool_record_sha256": (
                    request.expected_native_tool_record_sha256
                ),
                "native_tool_requirement_artifact_id": (
                    request.native_tool_requirement_artifact_id
                ),
            },
            outbound_root=outbound,
        )
        effects.authorize_dispatch(effect.effect_id, source="fixture")
        cancel_command = self.facts.request_cancel(
            target_command_id=target_command.command_id,
            cancel_executor_sha256=self.cancel_executor,
            reason_code="operator_cancelled",
            reason="Operator requested cancellation",
            requested_by="cli:tenant:ou_fixture",
            requested_at=self.now + timedelta(seconds=1),
        )

        operation = build_operation_record(
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
        timestamp = self.now.isoformat()
        view = NativeOperationView(
            schema_version=OPERATION_VIEW_SCHEMA,
            operation_id=request.operation_id,
            kind=DISCOVER_KIND,
            payload_sha256=operation["payload_sha256"],
            state="unknown",
            request_sha256=digest(request.to_record()),
            input_archive_sha256=file_digest(archive),
            native_tool_requirement_sha256=(
                request.expected_native_tool_record_sha256
            ),
            actual_native_tool_sha256=(
                request.expected_native_tool_record_sha256
            ),
            owner_sha256=claim.owner_sha256,
            executor_sha256=claim.executor_sha256,
            resource_terminated=False,
            artifact=None,
            failure=NativeFailureView(
                code="native_cancel_after_send",
                message="Cancellation was accepted after execution started",
            ),
            created_at=timestamp,
            started_at=timestamp,
            finished_at=timestamp,
        )
        return target_command, effect, cancel_command, view, services

    def _control(self, adapter):
        return CancellationControlService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "cancel-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "cancel"}),
                executor_sha256=self.cancel_executor,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now + timedelta(minutes=1),
            adapter=adapter,
        )

    def test_ack_reports_one_exact_post_and_does_not_finish_target(self) -> None:
        target, effect, cancel_command, view, services = self._target()
        loopback = _CancelServer(
            operation_id=view.operation_id,
            view=view,
            mode="success",
        )
        self.addCleanup(loopback.close)
        client = NativeOperationClient(loopback.endpoint)
        adapter = native_operation_cancellation_adapter(
            client,
            facts=self.facts,
            artifacts=self.artifacts,
        )
        control = self._control(adapter)

        result = control.send(
            cancel_command_id=cancel_command.command_id,
            target_effect_id=effect.effect_id,
        )

        self.assertTrue(result.sent)
        self.assertEqual(result.target_effect_state, EffectState.UNKNOWN)
        self.assertEqual(result.cancel_effect_state, EffectState.SATISFIED)
        self.assertEqual(
            loopback.post_paths,
            [f"/operations/{view.operation_id}/cancel"],
        )
        self.assertEqual(loopback.get_count, 0)
        self.assertEqual(
            self.facts.effect_state(effect.effect_id),
            EffectState.UNKNOWN,
        )
        replay = control.send(
            cancel_command_id=cancel_command.command_id,
            target_effect_id=effect.effect_id,
        )
        self.assertFalse(replay.sent)
        self.assertEqual(len(loopback.post_paths), 1)
        with self.assertRaises(ProtocolError) as unresolved:
            execute_cancellation(
                command_id=target.command_id,
                services=services,
            )
        self.assertEqual(unresolved.exception.code, "cancel.effect_unresolved")

    def _assert_unresolved_without_retry(self, mode: str) -> None:
        target, effect, cancel_command, view, _services = self._target()
        loopback = _CancelServer(
            operation_id=view.operation_id,
            view=view,
            mode=mode,
        )
        self.addCleanup(loopback.close)
        adapter = native_operation_cancellation_adapter(
            NativeOperationClient(loopback.endpoint),
            facts=self.facts,
            artifacts=self.artifacts,
        )
        control = self._control(adapter)
        result = control.send(
            cancel_command_id=cancel_command.command_id,
            target_effect_id=effect.effect_id,
        )
        self.assertTrue(result.sent)
        self.assertEqual(result.target_effect_state, EffectState.UNKNOWN)
        self.assertEqual(result.cancel_effect_state, EffectState.UNKNOWN)
        self.assertEqual(len(loopback.post_paths), 1)
        replay = control.send(
            cancel_command_id=cancel_command.command_id,
            target_effect_id=effect.effect_id,
        )
        self.assertFalse(replay.sent)
        self.assertEqual(len(loopback.post_paths), 1)
        self.assertEqual(
            target.command_id,
            cancel_command.request["target_command_id"],
        )

    def test_not_found_remains_unknown_without_retry(self) -> None:
        self._assert_unresolved_without_retry("not-found")

    def test_lost_response_remains_unknown_without_retry(self) -> None:
        self._assert_unresolved_without_retry("lost")


if __name__ == "__main__":
    unittest.main()
