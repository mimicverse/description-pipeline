"""Real engine proof that Generate and Publish accept only scoped predecessorless replay."""

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

from description_pipeline.io import digest, read_data
from description_pipeline.artifacts import ArtifactStore
from description_pipeline.pipeline import (
    DiagnosticAdmissionSpec,
    build_diagnostic_command,
    execute_stage,
    qualify_stage,
)
from description_pipeline.pipeline.contracts import ExecutionClaim, ProtocolError, StageCommand
from description_pipeline.pipeline.engine import StageWorkspace
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.pipeline.stages.generate import GenerateHandler
from description_pipeline.pipeline.stages.publish import PublishHandler
from description_pipeline.platform.diagnostic import DiagnosticRuntimeConfig, open_diagnostic_stores
from description_pipeline.stages import STAGE_IDS

from tests.v1 import test_generate_handler as generate_fixtures
from tests.v1.test_pipeline_publish import PublishFixture, _PullRequests

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = Path(__file__).resolve().parents[2] / "deploy" / "platform" / "schema.sql"


class _StageCounter:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls = dict.fromkeys(STAGE_IDS, 0)

    def execute(self, command, claim, workspace, *, effects):
        self.calls[command.stage] += 1
        return self.handler.execute(command, claim, workspace, effects=effects)

    def validate_candidate(self, command, claim, candidate, *, effects):
        return self.handler.validate_candidate(command, claim, candidate, effects=effects)


class GeneratePublishDiagnosticBoundaryTests(unittest.TestCase):
    def test_formal_generate_and_publish_still_require_a_predecessor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="diagnostic-domain-boundary-") as directory:
            root = Path(directory)
            store = ArtifactStore(root / "cas")
            workspace = StageWorkspace(root=root / "work", inputs={})
            workspace.root.mkdir()
            now = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
            cases = (
                ("generate", GenerateHandler(store), "generate.predecessor_missing"),
                ("publish", PublishHandler(store), "publish.command"),
            )
            for index, (stage, handler, error_code) in enumerate(cases, start=1):
                command = StageCommand.create(
                    command_id=f"formal-{stage}",
                    run_id=f"formal-{stage}-run",
                    stage=stage,
                    idempotency_key=f"formal-{stage}-run/{stage}",
                    request={},
                    input_artifacts=(),
                    config_snapshot_sha256=digest({"config": 1}),
                    contract_sha256=digest({"contract": 1}),
                    policy_sha256=digest({"policy": 1}),
                    expected_executor_sha256=digest({"tool": stage}),
                    predecessor_decision_id=None,
                )
                claim = ExecutionClaim(
                    claim_id=f"formal-{stage}-claim",
                    command_id=command.command_id,
                    fence=index,
                    owner_sha256=digest({"process": stage}),
                    executor_sha256=command.expected_executor_sha256,
                    lane_key=None,
                    expires_at=now + timedelta(minutes=5),
                )
                with self.assertRaises(ProtocolError) as raised:
                    handler.execute(command, claim, workspace, effects=object())
                self.assertEqual(raised.exception.code, error_code)


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class GeneratePublishDiagnosticPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="generate-publish-diagnostic-postgres-")
        cls.server = pgserver.get_server(
            Path(cls.server_root.name) / "pgdata",
            cleanup_mode="delete",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="generate-publish-diagnostic-")
        self.addCleanup(self.directory.cleanup)
        self.schema = "diagnostic_domain_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{self.schema}"',
        )
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
            cursor.execute(schema_sql)
            cursor.execute(f'UPDATE "{self.schema}".admission SET state = %s WHERE id = 1', ("open",))
        config = DiagnosticRuntimeConfig(
            database_dsn=self.server.get_uri(),
            artifact_root=Path(self.directory.name) / "diagnostic-artifacts",
            database_options=f"-c search_path={self.schema},public",
        )
        stores = open_diagnostic_stores(config)
        self.artifacts = stores.artifacts
        self.facts = stores.facts
        self.now = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _services(self, command, executor_sha256: str) -> CanonicalStageServices:
        return CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "work" / command.run_id,
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"process": uuid.uuid4().hex}),
                executor_sha256=executor_sha256,
                lease=timedelta(minutes=5),
                claim_id_factory=lambda: "diagnostic-claim-" + uuid.uuid4().hex,
            ),
            clock=lambda: self.now,
        )

    def _spec(self, *, source_command, imported_inputs, expected_executor: str):
        return DiagnosticAdmissionSpec(
            schema_version="description-pipeline.diagnostic-admission-spec/v1",
            run_id="diagnostic-" + source_command.stage,
            request_id="diagnostic-request-" + source_command.stage,
            target_stage=source_command.stage,
            request=source_command.to_record()["request"],
            input_artifacts=tuple(imported_inputs),
            expected_executor_sha256=expected_executor,
            config_snapshot_sha256=source_command.config_snapshot_sha256,
            contract_sha256=source_command.contract_sha256,
            policy_sha256=source_command.policy_sha256,
            created_at=self.now,
        )

    def _assert_only_target_committed(self, command) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT stage, count(*) FROM command WHERE run_id = %s GROUP BY stage",
                (command.run_id,),
            )
            self.assertEqual(cursor.fetchall(), [(command.stage, 1)])
            cursor.execute(
                "SELECT stage, count(*) FROM stage_decision WHERE run_id = %s GROUP BY stage",
                (command.run_id,),
            )
            self.assertEqual(cursor.fetchall(), [(command.stage, 1)])

    def test_generate_runs_one_scoped_diagnostic_stage_without_predecessor(self) -> None:
        source = generate_fixtures.GenerateHandlerTests(methodName="runTest")
        source.setUp()
        self.addCleanup(source.doCleanups)
        imported = tuple(self.artifacts.import_object(source.store, item) for item in source.command.input_artifacts)
        spec = self._spec(
            source_command=source.command,
            imported_inputs=imported,
            expected_executor=source.executor,
        )
        command = build_diagnostic_command(spec)
        self.facts.admit_diagnostic(spec, command)
        self.assertIsNone(command.predecessor_decision_id)
        self.assertEqual(command.diagnostic_scope.target_stage, "generate")
        self.assertEqual(command.input_artifacts, imported)

        counter = _StageCounter(GenerateHandler(self.artifacts))
        result = execute_stage(
            command_id=command.command_id,
            handler=counter,
            services=self._services(command, source.executor),
        )
        self.assertEqual(counter.calls, {stage: int(stage == "generate") for stage in STAGE_IDS})
        self.assertEqual(result.diagnostic_scope, command.diagnostic_scope)
        self.assertEqual(result.output_artifact.producer_run_id, command.run_id)
        qualification = qualify_stage(
            self.facts.load_admission(command.run_id),
            command,
            result=result,
        )
        self.assertFalse(qualification.formal_eligible)
        self._assert_only_target_committed(command)

    def test_publish_runs_one_scoped_diagnostic_stage_without_predecessor(self) -> None:
        source = PublishFixture()
        self.addCleanup(source.close)
        imported = tuple(self.artifacts.import_object(source.store, item) for item in source.command.input_artifacts)
        spec = self._spec(
            source_command=source.command,
            imported_inputs=imported,
            expected_executor=source.executor,
        )
        command = build_diagnostic_command(spec)
        self.facts.admit_diagnostic(spec, command)
        self.assertIsNone(command.predecessor_decision_id)
        self.assertEqual(command.diagnostic_scope.target_stage, "publish")
        self.assertEqual(command.input_artifacts, imported)

        pull_requests = _PullRequests()
        counter = _StageCounter(PublishHandler(self.artifacts, pull_requests=pull_requests))
        result = execute_stage(
            command_id=command.command_id,
            handler=counter,
            services=self._services(command, source.executor),
        )
        self.assertEqual(counter.calls, {stage: int(stage == "publish") for stage in STAGE_IDS})
        self.assertEqual(result.diagnostic_scope, command.diagnostic_scope)
        self.assertEqual(result.output_artifact.producer_run_id, command.run_id)
        output = self.artifacts.materialize(
            result.output_artifact,
            Path(self.directory.name) / "diagnostic-publish-output",
        )
        receipt = read_data(Path(output) / "publication-receipt.json")
        self.assertEqual(receipt["run_id"], command.run_id)
        self.assertEqual(receipt["branch_ref"], command.request["branch_ref"])
        self.assertEqual(len(pull_requests.rows), 1)
        qualification = qualify_stage(
            self.facts.load_admission(command.run_id),
            command,
            result=result,
        )
        self.assertFalse(qualification.formal_eligible)
        self._assert_only_target_committed(command)


if __name__ == "__main__":
    unittest.main()
