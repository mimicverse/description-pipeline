"""Real PostgreSQL and loopback recovery for a lost native Capture response."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

import description_pipeline.pipeline.capture as capture_module
from description_pipeline.io import digest, file_digest, read_data
from description_pipeline.orchestration.discover_protocol import seal_source_archive
from description_pipeline.orchestration.native_operation_client import (
    NativeOperationEffectObserver,
)
from description_pipeline.orchestration.native_operation_runtime import (
    CAPTURE_KIND,
    CAPTURE_OPERATION_SCHEMA,
    CaptureOperationRequest,
    NativeOperationResult,
    NativeOperationRuntime,
    build_operation_record,
)
from description_pipeline.orchestration.stage_transfer import CAPTURE_ARCHIVE
from description_pipeline.pipeline.application import StageApplication
from description_pipeline.pipeline.capture import CaptureUnresolved, NativeCaptureResult
from description_pipeline.pipeline.diagnostic import (
    DiagnosticAdmissionSpec,
    build_diagnostic_command,
)
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.reconciliation import EffectReconciliationService
from description_pipeline.pipeline.services import (
    CanonicalStageServices,
    ClaimPolicy,
)
from description_pipeline.platform.facts import FactStore
from description_pipeline.schemas import DIAGNOSTIC_ADMISSION_SCHEMA

from . import test_capture_handler
from .test_native_operations import _Server

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "platform"
    / "schema.sql"
)


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class NativeObserverPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(
            prefix="native-observer-postgres-"
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
        self.fixture = test_capture_handler.CaptureHandlerTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.schema = "native_observer_" + uuid.uuid4().hex
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
                f'UPDATE "{self.schema}".admission '
                "SET state = %s WHERE id = 1",
                ("open",),
            )

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _admit_capture(self, facts: FactStore):
        now = datetime(2026, 10, 25, tzinfo=UTC)
        spec = DiagnosticAdmissionSpec(
            schema_version=DIAGNOSTIC_ADMISSION_SCHEMA,
            run_id=test_capture_handler.CHILD,
            request_id="native-observer-recovery",
            target_stage="capture",
            request=_plain(self.fixture.command.request),
            input_artifacts=self.fixture.command.input_artifacts,
            expected_executor_sha256=self.fixture.stage_executor,
            config_snapshot_sha256=self.fixture.command.config_snapshot_sha256,
            contract_sha256=self.fixture.command.contract_sha256,
            policy_sha256=self.fixture.command.policy_sha256,
            created_at=now,
        )
        command = build_diagnostic_command(spec)
        facts.admit_diagnostic(spec, command)
        return command

    def test_application_reconciles_downloaded_capture_then_replays_without_resend(
        self,
    ) -> None:
        facts = FactStore(self._connect, self.fixture.store)
        command = self._admit_capture(facts)
        now = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
        services = CanonicalStageServices(
            facts=facts,
            artifacts=self.fixture.store,
            work_root=Path(self.fixture.root) / "engine-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "native-observer-recovery"}),
                executor_sha256=self.fixture.stage_executor,
                lease=timedelta(minutes=10),
            ),
            clock=lambda: now,
        )
        recovered_archive = Path(self.fixture.root) / "recovered-native.zip"
        native_calls = 0

        def lost_response(inputs, output, **kwargs):
            nonlocal native_calls
            native_calls += 1
            completed = capture_module._invoke_native_operation(
                inputs,
                output,
                **kwargs,
            )
            self.assertEqual(
                completed.outcome,
                "completed",
                completed.receipt,
            )
            self.assertIsNotNone(completed.archive)
            shutil.copyfile(completed.archive, recovered_archive)
            return NativeCaptureResult(
                "unknown",
                None,
                None,
                completed.receipt,
                False,
                "capture.response_lost",
            )

        capture_handler = self.fixture.handler(
            native_operation=lost_response
        )
        runtime = NativeOperationRuntime(
            state_root=Path(self.fixture.root) / "native-state",
            native_tool_factory=lambda: dict(
                self.fixture.current_native_tool
            ),
            executors={CAPTURE_KIND: self._return_recovered_archive},
            install_id=test_capture_handler.CAPTURE_TARGET.install_id,
        )
        self._recovered_archive = recovered_archive
        self._executor_calls = 0
        server = _Server(runtime)
        self.addCleanup(server.close)
        self.addCleanup(runtime.close)
        client = server.client()
        observer = NativeOperationEffectObserver(client)
        reconciliation = EffectReconciliationService(
            facts=facts,
            artifacts=self.fixture.store,
            work_root=Path(self.fixture.root) / "reconciliation",
            observer_tool_sha256=digest({"tool": "native-observer"}),
            clock=lambda: now,
        )
        application = StageApplication(
            services=services,
            facts=facts,
            artifacts=self.fixture.store,
            handlers={"capture": capture_handler},
            builders={},
            clock=lambda: now,
            reconciler=reconciliation,
            reconciliation_adapters={"native_operation": observer},
        )

        with (
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=self.fixture.current_native_tool,
            ),
            self.assertRaises(CaptureUnresolved),
        ):
            application.execute_registered_command(command.command_id)
        self.assertEqual(native_calls, 1)
        settlement = facts.effect_settlement_for_key(
            command.command_id,
            "capture-native",
        )
        self.assertIsNotNone(settlement)
        self.assertEqual(settlement.state, EffectState.UNKNOWN)
        self.assertTrue(recovered_archive.is_file())

        outbound_root = Path(self.fixture.root) / "effect-outbound"
        self.fixture.store.materialize(
            settlement.outbound_artifact,
            outbound_root,
        )
        outbound = read_data(outbound_root / "outbound.json")
        native_request = CaptureOperationRequest.from_record(
            {
                "schema_version": CAPTURE_OPERATION_SCHEMA,
                "command_id": command.command_id,
                "run_id": command.run_id,
                "request_digest": command.request_digest,
                "effect_key": "capture-native",
                "handoff_sha256": outbound["handoff_sha256"],
                "upload_sha256": outbound["upload_sha256"],
                "prepared_sha256": outbound["prepared_sha256"],
                "main_assembly": outbound["main_assembly"],
                "package_files": outbound["prepared_files"],
                "upstream_artifacts": outbound["upstream_artifacts"],
                "execution": outbound["execution"],
                "native_tool_source_sha256": (
                    outbound["native_tool_source_sha256"]
                ),
                "request_evidence": outbound["request_evidence"],
            }
        )
        operation_id = "capture-" + digest(
            {
                "run_id": command.run_id,
                "command_id": command.command_id,
                "effect_key": "capture-native",
            }
        )
        source_archive = seal_source_archive(
            self.fixture.package,
            self.fixture.prepared_files,
            Path(self.fixture.root) / "capture-input.zip",
        )
        envelope = build_operation_record(
            operation_id=operation_id,
            kind=CAPTURE_KIND,
            request=native_request.to_record(),
            input_archive_sha256=file_digest(source_archive),
            input_archive_bytes=source_archive.stat().st_size,
            native_tool_requirement_sha256=digest(
                self.fixture.current_native_tool
            ),
            output_contract=(
                "solidworks-to-urdf.capture-transfer/v4"
            ),
            target_id=runtime.target_identity.target_id,
        )
        client.create_operation(envelope)
        client.upload_input(operation_id, source_archive)
        remote = client.wait_for_terminal(operation_id, timeout=5)
        self.assertEqual(remote.state, "satisfied")
        self.assertEqual(self._executor_calls, 1)

        reconciled = application.reconcile_effect(
            command_id=command.command_id,
            effect_key="capture-native",
        )
        self.assertEqual(reconciled.settlement.state, EffectState.SATISFIED)
        self.assertIsNotNone(reconciled.settlement.payload_artifact)
        self.fixture.store.verify(reconciled.settlement.payload_artifact)
        self.assertEqual(self._executor_calls, 1)

        recovery_time = now + timedelta(minutes=10)
        recovery_services = CanonicalStageServices(
            facts=facts,
            artifacts=self.fixture.store,
            work_root=Path(self.fixture.root) / "recovery-engine-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "native-observer-replay"}),
                executor_sha256=self.fixture.stage_executor,
                lease=timedelta(minutes=10),
            ),
            clock=lambda: recovery_time,
        )
        recovery_application = StageApplication(
            services=recovery_services,
            facts=facts,
            artifacts=self.fixture.store,
            handlers={"capture": capture_handler},
            builders={},
            clock=lambda: recovery_time,
            reconciler=reconciliation,
            reconciliation_adapters={"native_operation": observer},
        )
        replayed = recovery_application.execute_registered_command(
            command.command_id
        )
        self.assertEqual(replayed.outcome, "completed")
        self.assertEqual(native_calls, 1)
        self.assertEqual(self._executor_calls, 1)
        final = facts.effect_settlement_for_key(
            command.command_id,
            "capture-native",
        )
        self.assertEqual(final.state, EffectState.SATISFIED)
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT outcome FROM stage_decision "
                "WHERE command_id = %s",
                (command.command_id,),
            )
            self.assertEqual(cursor.fetchall(), [("completed",)])

    def _return_recovered_archive(
        self,
        request,
        source_archive,
        workspace,
        native_tool,
    ) -> NativeOperationResult:
        del request, source_archive, native_tool
        self._executor_calls += 1
        target = Path(workspace) / CAPTURE_ARCHIVE
        shutil.copyfile(self._recovered_archive, target)
        return NativeOperationResult(
            artifact_path=target,
            artifact_name=CAPTURE_ARCHIVE,
            output_contract="solidworks-to-urdf.capture-transfer/v4",
            resource_terminated=True,
        )


if __name__ == "__main__":
    unittest.main()
