"""Real PostgreSQL proof for one imported, fully sealed diagnostic stage replay."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.artifacts import ArtifactStore, ArtifactStoreError
from description_pipeline.io import digest
from description_pipeline.pipeline.admission import VerifiedPredecessor
from description_pipeline.pipeline.application import StageApplication
from description_pipeline.pipeline.contracts import StageCommand, StageExecution, StageFailure
from description_pipeline.pipeline.diagnostic import DiagnosticAdmissionSpec, build_diagnostic_command
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.engine import execute_stage
from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.platform.diagnostic import DiagnosticRuntimeConfig, open_diagnostic_stores
from description_pipeline.platform.facts import FactConflict, FactStoreError
from description_pipeline.schemas import DIAGNOSTIC_ADMISSION_SCHEMA

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = Path(__file__).resolve().parents[2] / "deploy" / "platform" / "schema.sql"


class _DiagnosticSuccessHandler:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, command, claim, workspace, *, effects):
        self.calls += 1
        if command.diagnostic_scope is None or command.predecessor_decision_id is not None:
            raise AssertionError("The diagnostic command lost its scope")
        if set(workspace.inputs) != {artifact.artifact_id for artifact in command.input_artifacts}:
            raise AssertionError("The diagnostic workspace lost its sealed inputs")
        self.assert_absent_effect = effects.effect_settlement_for_key("not-prepared")
        input_path = next(iter(workspace.inputs.values())) / "payload.txt"
        output = workspace.root / "output"
        output.mkdir()
        (output / "result.txt").write_text(input_path.read_text(encoding="utf-8"), encoding="utf-8")
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256=digest({"checks": "diagnostic-success"}),
            resources_settled=True,
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        if candidate.result.diagnostic_scope != command.diagnostic_scope:
            raise ProtocolError("diagnostic.result_scope_mismatch")
        return candidate


class _DiagnosticUnknownHandler:
    def __init__(self) -> None:
        self.external_calls = 0

    def execute(self, command, claim, workspace, *, effects):
        existing = effects.effect_settlement_for_key("native")
        if existing is not None:
            raise AssertionError("An existing diagnostic effect was sent again")
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"operation":"diagnostic"}\n', encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "local-fixture"},
            expected_state={"operation": command.command_id, "state": "complete"},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="diagnostic-fixture")
        self.external_calls += 1
        effects.record_timeout(intent.effect_id, source="diagnostic-fixture")
        raise RuntimeError("simulated lost diagnostic response")

    def validate_candidate(self, command, claim, candidate, *, effects):
        raise AssertionError("An unknown diagnostic has no candidate")


class _DiagnosticFailureHandler:
    def execute(self, command, claim, workspace, *, effects):
        raise StageFailure(
            "diagnostic.fixture_failed",
            "The diagnostic fixture failed",
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        raise AssertionError("A definite diagnostic failure has no candidate")


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class DiagnosticStagePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="diagnostic-postgres-")
        cls.server = pgserver.get_server(Path(cls.server_root.name) / "pgdata", cleanup_mode="delete")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="diagnostic-runtime-")
        self.addCleanup(self.directory.cleanup)
        self.schema = "diagnostic_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{self.schema}"',
        )
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
            cursor.execute(schema_sql)
            cursor.execute(f'UPDATE "{self.schema}".admission SET state = %s WHERE id = 1', ("open",))

        self.source = ArtifactStore(Path(self.directory.name) / "historical-source")
        config = DiagnosticRuntimeConfig(
            database_dsn=self.server.get_uri(),
            artifact_root=Path(self.directory.name) / "private-diagnostic",
            database_options=f"-c search_path={self.schema},public",
        )
        stores = open_diagnostic_stores(config)
        self.artifacts = stores.artifacts
        self.facts = stores.facts
        self.now = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
        self.executor = digest({"tool": "diagnostic-handler"})
        self.owner = digest({"process": uuid.uuid4().hex})
        source_root = Path(self.directory.name) / "sealed-input"
        source_root.mkdir()
        (source_root / "payload.txt").write_text("historical sealed bytes\n", encoding="utf-8")
        self.historical = self.source.seal_tree(
            source_root,
            kind="fixture_input",
            producer_run_id="historical-run",
            producer_stage="capture",
            producer_tool_sha256=digest({"tool": "historical-producer"}),
        )
        self.imported = self.artifacts.import_object(self.source, self.historical)
        self.assertEqual(self.imported, self.historical)

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _query(self, statement, parameters=()):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def _spec(self, suffix: str, *, input_artifact=None, target_stage="generate"):
        return DiagnosticAdmissionSpec(
            schema_version=DIAGNOSTIC_ADMISSION_SCHEMA,
            run_id=f"diagnostic-{suffix}",
            request_id=f"diagnostic-request-{suffix}",
            target_stage=target_stage,
            request={"schema_version": "diagnostic-fixture/v1", "operation": "read-input"},
            input_artifacts=(input_artifact or self.imported,),
            expected_executor_sha256=self.executor,
            config_snapshot_sha256=digest({"config": 1}),
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": 1}),
            created_at=self.now,
        )

    def test_all_six_diagnostic_targets_execute_only_the_target_stage(self) -> None:
        handlers = {
            stage: _DiagnosticSuccessHandler()
            for stage in (
                "freeze",
                "discover",
                "capture",
                "generate",
                "verify",
                "publish",
            )
        }
        services = self._services(
            build_diagnostic_command(self._spec("target-route"))
        )
        application = StageApplication(
            services=services,
            facts=self.facts,
            artifacts=self.artifacts,
            handlers=handlers,
            builders={},
            clock=lambda: self.now,
        )

        for stage, target in handlers.items():
            with self.subTest(stage=stage):
                spec = self._spec(
                    f"target-{stage}",
                    target_stage=stage,
                )
                command = build_diagnostic_command(spec)
                self.facts.admit_diagnostic(spec, command)
                for handler in handlers.values():
                    handler.calls = 0
                result = application.execute_registered_command(
                    command.command_id
                )
                self.assertEqual(result.outcome, "completed")
                self.assertEqual(
                    result.diagnostic_scope,
                    command.diagnostic_scope,
                )
                self.assertEqual(target.calls, 1)
                self.assertEqual(
                    {
                        name: handler.calls
                        for name, handler in handlers.items()
                        if name != stage
                    },
                    dict.fromkeys(
                        (name for name in handlers if name != stage),
                        0,
                    ),
                )
                self.assertEqual(
                    self._query(
                        "SELECT stage FROM command WHERE run_id = %s",
                        (spec.run_id,),
                    ),
                    [(stage,)],
                )

    def _services(self, command: StageCommand) -> CanonicalStageServices:
        return CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "work" / command.run_id,
            claim_policy=ClaimPolicy(
                owner_sha256=self.owner,
                executor_sha256=self.executor,
                lease=timedelta(minutes=5),
                claim_id_factory=lambda: "diagnostic-claim-" + uuid.uuid4().hex,
            ),
            clock=lambda: self.now,
        )

    @staticmethod
    def _object_bytes(store: ArtifactStore, artifact) -> dict[str, bytes]:
        root = store.objects / artifact.artifact_id
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_imported_history_runs_one_diagnostic_stage_without_upstream_decisions(self) -> None:
        source_before = self._object_bytes(self.source, self.historical)
        spec = self._spec("success")
        command = build_diagnostic_command(spec)
        self.facts.admit_diagnostic(spec, command)
        self.facts.admit_diagnostic(spec, command)
        self.assertEqual(
            self._query("SELECT count(*) FROM run_admission WHERE run_id = %s", ("historical-run",))[0][0],
            0,
        )
        self.assertEqual(
            self._query(
                "SELECT producer_run_id, producer_stage, producer_tool_sha256 FROM artifact WHERE artifact_id = %s",
                (self.imported.artifact_id,),
            )[0],
            (
                self.imported.producer_run_id,
                self.imported.producer_stage,
                self.imported.producer_tool_sha256,
            ),
        )

        handler = _DiagnosticSuccessHandler()
        result = execute_stage(
            command_id=command.command_id,
            handler=handler,
            services=self._services(command),
        )
        self.assertEqual(handler.calls, 1)
        self.assertIsNone(handler.assert_absent_effect)
        self.assertEqual(result.diagnostic_scope, command.diagnostic_scope)
        decision = self.facts.decision_for(command)
        self.assertIsNotNone(decision)
        qualification = self.facts.qualification_for(decision)
        self.assertEqual(qualification.scope_kind, "diagnostic")
        self.assertFalse(qualification.formal_eligible)
        self.assertEqual(
            self._query(
                "SELECT stage, count(*) FROM command WHERE run_id = %s GROUP BY stage ORDER BY stage",
                (spec.run_id,),
            ),
            [("generate", 1)],
        )
        self.assertEqual(
            self._query(
                "SELECT stage, count(*) FROM stage_decision WHERE run_id = %s GROUP BY stage",
                (spec.run_id,),
            ),
            [("generate", 1)],
        )
        with self.assertRaises(ProtocolError):
            VerifiedPredecessor(
                stage="generate",
                command=self.facts.load_command(command.command_id),
                result=result,
                decision=decision,
            )
        self.assertEqual(self._object_bytes(self.source, self.historical), source_before)
        self.source.verify(self.historical)

    def test_missing_imported_input_and_wrong_command_executor_are_rejected_before_writes(self) -> None:
        missing_root = Path(self.directory.name) / "missing-input"
        missing_root.mkdir()
        (missing_root / "payload.txt").write_text("not imported\n", encoding="utf-8")
        missing = self.source.seal_tree(
            missing_root,
            kind="fixture_input",
            producer_run_id="historical-run",
            producer_stage="capture",
            producer_tool_sha256=digest({"tool": "historical-producer"}),
        )
        missing_spec = self._spec("missing", input_artifact=missing)
        missing_command = build_diagnostic_command(missing_spec)
        with self.assertRaises(ArtifactStoreError):
            self.facts.admit_diagnostic(missing_spec, missing_command)
        self.assertEqual(
            self._query("SELECT count(*) FROM run_admission WHERE run_id = %s", (missing_spec.run_id,))[0][0],
            0,
        )

        spec = self._spec("wrong-tool")
        valid = build_diagnostic_command(spec)
        forged = StageCommand.create(
            command_id=valid.command_id,
            run_id=valid.run_id,
            stage="generate",
            idempotency_key=valid.idempotency_key,
            request=valid.request,
            input_artifacts=valid.input_artifacts,
            config_snapshot_sha256=valid.config_snapshot_sha256,
            contract_sha256=valid.contract_sha256,
            policy_sha256=valid.policy_sha256,
            expected_executor_sha256=digest({"tool": "wrong-diagnostic-handler"}),
            predecessor_decision_id=None,
            diagnostic_scope=valid.diagnostic_scope,
        )
        with self.assertRaisesRegex(FactStoreError, "typed validation"):
            self.facts.admit_diagnostic(spec, forged)
        self.assertEqual(
            self._query("SELECT count(*) FROM run_admission WHERE run_id = %s", (spec.run_id,))[0][0],
            0,
        )

    def test_unknown_diagnostic_effect_is_not_sent_again(self) -> None:
        spec = self._spec("unknown")
        command = build_diagnostic_command(spec)
        self.facts.admit_diagnostic(spec, command)
        handler = _DiagnosticUnknownHandler()
        services = self._services(command)
        with self.assertRaisesRegex(RuntimeError, "lost diagnostic response"):
            execute_stage(command_id=command.command_id, handler=handler, services=services)
        effects = self.facts.effect_states_for_command(command.command_id)
        self.assertEqual(list(effects.values()), [EffectState.UNKNOWN])
        self.assertEqual(handler.external_calls, 1)
        with self.assertRaises(FactConflict):
            execute_stage(command_id=command.command_id, handler=handler, services=services)
        self.assertEqual(handler.external_calls, 1)

    def test_diagnostic_failure_record_retains_scope_and_cannot_be_formal(self) -> None:
        spec = self._spec("failure")
        command = build_diagnostic_command(spec)
        self.facts.admit_diagnostic(spec, command)
        failure = execute_stage(
            command_id=command.command_id,
            handler=_DiagnosticFailureHandler(),
            services=self._services(command),
        )
        self.assertEqual(failure.diagnostic_scope, command.diagnostic_scope)
        decision = self.facts.decision_for(command)
        self.assertIsNotNone(decision)
        qualification = self.facts.qualification_for(decision)
        self.assertEqual(qualification.scope_kind, "diagnostic")
        self.assertFalse(qualification.formal_eligible)
        self.assertEqual(
            self._query(
                "SELECT failure_envelope -> 'diagnostic_scope' -> 'target_stage' "
                "FROM stage_decision WHERE command_id = %s",
                (command.command_id,),
            )[0][0],
            "generate",
        )


if __name__ == "__main__":
    unittest.main()
