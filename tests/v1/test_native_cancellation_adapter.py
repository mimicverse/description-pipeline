"""Real PostgreSQL and loopback proof for native operation cancellation."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.delivery import PIPELINE_ID
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
from description_pipeline.pipeline.contracts import (
    FailureEnvelope,
    NativeTargetIdentity,
    ProtocolError,
    StageCommand,
)
from description_pipeline.pipeline.engine import execute_cancellation
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.reconciliation import (
    reconciliation_metadata_document,
)
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
CANCEL_TARGET = NativeTargetIdentity.create(
    install_id="win-" + "3" * 32,
    pipeline_id=PIPELINE_ID,
)


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
                self.send_header(
                    "X-Native-Target-ID",
                    CANCEL_TARGET.target_id,
                )
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


def _client(loopback: _CancelServer) -> NativeOperationClient:
    client = NativeOperationClient(
        loopback.endpoint,
        target_id=CANCEL_TARGET.target_id,
    )
    client._target_verified = True
    return client


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
        transfer_target = {
            "operation_id": request.operation_id,
            "main_assembly": request.main_assembly,
            "native_target_identity": CANCEL_TARGET.to_record(),
            "native_target_id": CANCEL_TARGET.target_id,
            "native_tool_sha256": request.expected_native_tool_record_sha256,
        }
        transfer_target["target_binding_digest"] = digest(transfer_target)
        effect = effects.prepare_effect(
            effect_key=f"native-discover:{target_command.command_id}",
            effect_kind="native_operation",
            target=transfer_target,
            expected_state={
                "schema_version": NATIVE_OBSERVATION_SCHEMA,
                "operation_id": request.operation_id,
                "request_digest": target_command.request_digest,
                "source_inventory_sha256": request.source_inventory_sha256,
                "source_archive_sha256": file_digest(archive),
                "native_target_id": CANCEL_TARGET.target_id,
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
            target_id=CANCEL_TARGET.target_id,
        )
        timestamp = self.now.isoformat()
        view = NativeOperationView(
            schema_version=OPERATION_VIEW_SCHEMA,
            operation_id=request.operation_id,
            kind=DISCOVER_KIND,
            target_id=CANCEL_TARGET.target_id,
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
        return target_command, effect, cancel_command, view, services, claim

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
        target, effect, cancel_command, view, services, claim = self._target()
        loopback = _CancelServer(
            operation_id=view.operation_id,
            view=view,
            mode="success",
        )
        self.addCleanup(loopback.close)
        client = _client(loopback)
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

        observation_root = Path(self.directory.name) / "observed-result"
        metadata_root = Path(self.directory.name) / "observed-metadata"
        observation_root.mkdir()
        metadata_root.mkdir()
        write_json(observation_root / "result.json", {"complete": True})
        observer_tool = digest({"observer": "fixture"})
        observed_at = self.now + timedelta(minutes=2)
        payload = self.artifacts.seal_tree(
            observation_root,
            kind="effect_observation",
            producer_run_id=target.run_id,
            producer_stage=target.stage,
            producer_tool_sha256=observer_tool,
        )
        settlement = self.facts.effect_settlement(
            effect.effect_id,
            expected_command_id=target.command_id,
        )
        self.assertIsNotNone(settlement.outbound_artifact)
        write_json(
            metadata_root / "metadata.json",
            reconciliation_metadata_document(
                command=target,
                intent=effect,
                fact_type="reconciled",
                state_class="known",
                metadata={"source": "fixture"},
                outbound_artifact=settlement.outbound_artifact,
                payload_artifact=payload,
                observer_tool_sha256=observer_tool,
                observed_at=observed_at,
            ),
        )
        metadata = self.artifacts.seal_tree(
            metadata_root,
            kind="effect_reconciliation_metadata",
            producer_run_id=target.run_id,
            producer_stage=target.stage,
            producer_tool_sha256=observer_tool,
        )
        self.facts.append_reconciliation_fact(
            effect_id=effect.effect_id,
            fact_type="reconciled",
            source="effect-reconciler:" + observer_tool,
            state_class="known",
            payload_artifact=payload,
            metadata_artifact=metadata,
            payload_digest=payload.set_sha256,
            observed_at=observed_at,
            fact_id="known-" + effect.effect_id,
            expected_command_id=target.command_id,
        )
        self.facts.close_open_claim(
            claim,
            closure_code="completed",
            closed_at=self.now + timedelta(minutes=2),
        )
        completed = execute_cancellation(
            command_id=target.command_id,
            services=services,
        )
        self.assertIsInstance(completed, FailureEnvelope)
        self.assertEqual(completed.outcome, "cancelled")
        self.assertEqual(
            dict(completed.effect_states),
            {effect.effect_id: "satisfied"},
        )
        self.assertEqual(
            self.facts.effect_state(effect.effect_id),
            EffectState.SATISFIED,
        )

    def _assert_unresolved_without_retry(self, mode: str) -> None:
        target, effect, cancel_command, view, _services, _claim = self._target()
        loopback = _CancelServer(
            operation_id=view.operation_id,
            view=view,
            mode=mode,
        )
        self.addCleanup(loopback.close)
        adapter = native_operation_cancellation_adapter(
            _client(loopback),
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

    def test_mismatched_ack_identity_is_not_accepted(self) -> None:
        target, effect, cancel_command, view, _services, _claim = self._target()
        changed = replace(
            view,
            payload_sha256=digest({"operation": "other"}),
        )
        loopback = _CancelServer(
            operation_id=view.operation_id,
            view=changed,
            mode="success",
        )
        self.addCleanup(loopback.close)
        adapter = native_operation_cancellation_adapter(
            _client(loopback),
            facts=self.facts,
            artifacts=self.artifacts,
        )
        result = self._control(adapter).send(
            cancel_command_id=cancel_command.command_id,
            target_effect_id=effect.effect_id,
        )
        self.assertEqual(result.cancel_effect_state, EffectState.UNKNOWN)
        self.assertEqual(len(loopback.post_paths), 1)
        self.assertEqual(
            self.facts.effect_state(effect.effect_id),
            EffectState.UNKNOWN,
        )
        self.assertEqual(
            target.command_id,
            cancel_command.request["target_command_id"],
        )


if __name__ == "__main__":
    unittest.main()
