"""Real PostgreSQL composition tests for the unified stage runtime.

The test is skipped unless the separately installed ``pgserver`` and
``psycopg2`` packages are on ``PYTHONPATH``. When present it starts an isolated
throwaway cluster and never contacts an existing platform or Airflow service.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import digest
from description_pipeline.pipeline import (
    EffectState,
    ExecutionClaim,
    RunAdmission,
    StageCommand,
    StageExecution,
    StageFailure,
    StageResult,
    execute_stage,
)
from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.platform.facts import FactConflict, FactStore

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = Path(__file__).resolve().parents[2] / "deploy" / "platform" / "schema.sql"


class _EffectHandler:
    def __init__(self) -> None:
        self.external_calls = 0
        self.validations = 0

    def execute(self, command, claim, workspace, *, effects):
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"operation": "native"}\n', encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "local-test", "operation": command.command_id},
            expected_state={"operation": command.command_id, "state": "complete"},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="runtime-test")
        self.external_calls += 1

        response = workspace.root / "response"
        response.mkdir()
        (response / "response.json").write_text('{"state": "complete"}\n', encoding="utf-8")
        effects.record_response(intent.effect_id, source="runtime-test", response_root=response)

        observation = workspace.root / "observation"
        observation.mkdir()
        (observation / "observation.json").write_text('{"state": "complete"}\n', encoding="utf-8")
        effects.record_observation(
            intent.effect_id,
            source="runtime-test",
            state_class="known",
            observation_root=observation,
        )

        output = workspace.root / "output"
        output.mkdir()
        (output / "model.txt").write_text("verified output\n", encoding="utf-8")
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256=digest({"checks": "passed"}),
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        self.validations += 1
        return candidate


class _FailureHandler:
    def execute(self, command, claim, workspace, *, effects):
        raise StageFailure(
            "domain_input_invalid",
            "The domain input is definitely invalid",
            unavailable_reason="diagnostic sealing deliberately unavailable",
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        raise AssertionError("A failing execution must not validate a candidate")


class _UnsealedFailureHandler:
    def execute(self, command, claim, workspace, *, effects):
        return StageExecution(
            outcome="failed",
            candidate_root=str(workspace.root / "missing-diagnostics"),
            checks_sha256=digest({"checks": "failed"}),
            reason_code="domain_failed",
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        raise AssertionError("An unsealed failure has no candidate")


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class RuntimeCandidatePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="runtime-candidate-postgres-")
        cls.server = pgserver.get_server(Path(cls.server_root.name) / "pgdata", cleanup_mode="delete")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="runtime-candidate-cas-")
        self.addCleanup(self.directory.cleanup)
        self.schema = "runtime_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{self.schema}"',
        )
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
            cursor.execute(schema_sql)
            cursor.execute(f'UPDATE "{self.schema}".admission SET state = %s WHERE id = 1', ("open",))

        self.now = datetime(2026, 10, 25, tzinfo=UTC)
        self.executor = digest({"tool": "runtime-candidate"})
        self.owner = digest({"process": str(uuid.uuid4())})
        self.artifacts = ArtifactStore(Path(self.directory.name) / "cas")
        self.facts = FactStore(self._connect, self.artifacts)
        self.admission = RunAdmission.create(
            run_id="run-1",
            request_id="request-1",
            parent_run_id=None,
            from_stage=None,
            request={"target": "m3", "source": "fixture"},
            created_at=self.now,
        )
        self.command = StageCommand.create(
            command_id="command-freeze",
            run_id="run-1",
            stage="freeze",
            idempotency_key="run-1/freeze",
            request={"source": "fixture"},
            input_artifacts=(),
            config_snapshot_sha256=digest({"config": 1}),
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": 1}),
            expected_executor_sha256=self.executor,
            predecessor_decision_id=None,
        )
        self.facts.admit_run(self.admission, self.command)
        self.services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "work",
            claim_policy=ClaimPolicy(
                owner_sha256=self.owner,
                executor_sha256=self.executor,
                lease=timedelta(minutes=5),
                claim_id_factory=lambda: "claim-" + str(uuid.uuid4()),
            ),
            clock=lambda: self.now,
        )

    def _connect(self):
        connection = psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )
        return connection

    def _execute(self, handler):
        return execute_stage(
            command_id=self.command.command_id,
            handler=handler,
            services=self.services,
        )

    def _query(self, statement, parameters=()):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def test_real_artifact_effect_claim_and_decision_chain(self) -> None:
        handler = _EffectHandler()
        result = self._execute(handler)

        self.assertIsInstance(result, StageResult)
        self.assertEqual(handler.external_calls, 1)
        self.assertEqual(handler.validations, 1)
        decision = self.facts.decision_for(self.command)
        self.assertIsNotNone(decision)
        self.assertNotEqual(
            result.output_artifact.artifact_id,
            decision.result_record_artifact.artifact_id,
        )
        self.assertEqual(result.output_artifact.producer_tool_sha256, self.executor)
        self.assertEqual(decision.actual_executor_sha256, self.executor)
        self.assertEqual(self.artifacts.read_result(decision.result_record_artifact), result)
        self.artifacts.verify(result.output_artifact)
        effect_ids = self.facts.effect_ids_for_command(self.command.command_id)
        self.assertEqual(len(effect_ids), 1)
        self.assertEqual(self.facts.effect_state(effect_ids[0]), EffectState.SATISFIED)
        settlement = self.facts.effect_settlement(effect_ids[0])
        self.assertEqual(settlement.state, EffectState.SATISFIED)
        self.assertIsNotNone(settlement.payload_artifact)
        self.artifacts.verify(settlement.payload_artifact)
        rows = self._query(
            "SELECT c.owner_sha256, c.executor_sha256, p.output_artifact_id, d.result_record_artifact_id "
            "FROM execution_claim c "
            "JOIN stage_candidate p ON p.command_id = c.command_id "
            "JOIN stage_decision d ON d.command_id = c.command_id "
            "WHERE c.command_id = %s",
            (self.command.command_id,),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], self.owner)
        self.assertEqual(rows[0][1], self.executor)
        self.assertNotEqual(rows[0][2], rows[0][3])
        command_root = self.services.work_root / self.command.command_id
        self.assertEqual(list(command_root.iterdir()), [])

    def test_unknown_effect_refuses_dispatch_before_external_callback(self) -> None:
        claim = self.services.acquire_claim(self.command)
        workspace = self.services.materialize(self.command, claim, candidate=None)
        effects = self.services.effect_port(self.command, claim, workspace)
        self.addCleanup(self.services.discard_workspace, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text("{}\n", encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "local-test"},
            expected_state={"state": "complete"},
            outbound_root=outbound,
        )
        replay = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "local-test"},
            expected_state={"state": "complete"},
            outbound_root=outbound,
        )
        self.assertEqual(replay.effect_id, intent.effect_id)
        changed = workspace.root / "changed-outbound"
        changed.mkdir()
        (changed / "request.json").write_text('{"changed": true}\n', encoding="utf-8")
        with self.assertRaises(FactConflict):
            effects.prepare_effect(
                effect_key="native",
                effect_kind="native_operation",
                target={"endpoint": "local-test"},
                expected_state={"state": "different"},
                outbound_root=changed,
            )
        effects.authorize_dispatch(intent.effect_id, source="runtime-test")
        effects.record_timeout(intent.effect_id, source="runtime-test")
        stale = ExecutionClaim(
            claim_id=claim.claim_id,
            command_id=claim.command_id,
            fence=claim.fence + 1,
            owner_sha256=claim.owner_sha256,
            executor_sha256=claim.executor_sha256,
            lane_key=claim.lane_key,
            expires_at=claim.expires_at,
        )
        with self.assertRaises(FactConflict):
            self.facts.authorize_effect_dispatch(
                effect_id=intent.effect_id,
                claim=stale,
                source="stale",
                payload_digest=digest({"stale": 1}),
                observed_at=self.now,
                fact_id="fact-stale",
            )
        self.assertIsNone(effects.effect_settlement_for_key("absent"))
        query_count = 1
        settlement = effects.effect_settlement_for_key("native")
        self.assertEqual(settlement.state, EffectState.UNKNOWN)
        callback_count = 0
        with self.assertRaises(FactConflict):
            effects.authorize_dispatch(intent.effect_id, source="runtime-test")
            callback_count += 1
        self.assertEqual(callback_count, 0)
        self.assertEqual(query_count, 1)
        self.assertEqual(effects.effect_state(intent.effect_id), EffectState.UNKNOWN)
        with self.assertRaises(ProtocolError):
            self.services.commit_failure(
                self.command,
                claim,
                StageFailure("native_unknown", "The native result remains unknown"),
            )
        decision_count = self._query(
            "SELECT count(*) FROM stage_decision WHERE command_id = %s",
            (self.command.command_id,),
        )[0][0]
        self.assertEqual(decision_count, 0)

        expired_outbound = workspace.root / "expired-outbound"
        expired_outbound.mkdir()
        (expired_outbound / "request.json").write_text("{}\n", encoding="utf-8")
        expired_effect = effects.prepare_effect(
            effect_key="expired",
            effect_kind="native_operation",
            target={"endpoint": "local-test"},
            expected_state={"state": "complete"},
            outbound_root=expired_outbound,
        )
        with self.assertRaises(FactConflict):
            self.facts.authorize_effect_dispatch(
                effect_id=expired_effect.effect_id,
                claim=claim,
                source="expired",
                payload_digest=digest({"expired": 1}),
                observed_at=self.now + timedelta(minutes=6),
                fact_id="fact-expired",
            )
        self.assertEqual(effects.effect_state(expired_effect.effect_id), EffectState.PREPARED)

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE execution_claim SET closed_at = %s, closure_code = 'operator_closed' WHERE claim_id = %s",
                (self.now, claim.claim_id),
            )
        with self.assertRaises(FactConflict):
            self.facts.acquire_claim(
                self.command.command_id,
                claim_id="claim-after-close",
                owner_sha256=digest({"process": "replacement"}),
                executor_sha256=self.executor,
                now=self.now,
                expires_at=self.now + timedelta(minutes=5),
            )

        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (outside / "payload.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ProtocolError):
            effects.prepare_effect(
                effect_key="outside",
                effect_kind="native_operation",
                target={"endpoint": "local-test"},
                expected_state={"state": "complete"},
                outbound_root=outside,
            )

    def test_satisfied_effect_returns_the_same_sealed_observation_without_resend(self) -> None:
        claim = self.services.acquire_claim(self.command)
        workspace = self.services.materialize(self.command, claim, candidate=None)
        self.addCleanup(self.services.discard_workspace, workspace)
        effects = self.services.effect_port(self.command, claim, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text("{}\n", encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="discover",
            effect_kind="native_operation",
            target={"endpoint": "local-test", "operation": "discover"},
            expected_state={"schema": "native-discover/v1", "members": ["observation.json"]},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="runtime-test")
        call_count = 1
        observation = workspace.root / "observation"
        observation.mkdir()
        (observation / "observation.json").write_text('{"objects": []}\n', encoding="utf-8")
        observation_fact = effects.record_observation(
            intent.effect_id,
            source="runtime-test",
            state_class="known",
            observation_root=observation,
        )

        restarted_effects = self.services.effect_port(self.command, claim, workspace)
        settlement = restarted_effects.effect_settlement_for_key("discover")
        self.assertEqual(settlement.state, EffectState.SATISFIED)
        self.assertIsNotNone(settlement.payload_artifact)
        self.assertEqual(settlement.payload_artifact.artifact_id, observation_fact.payload_artifact_id)
        self.artifacts.verify(settlement.payload_artifact)
        repeated = restarted_effects.effect_settlement_for_key("discover")
        self.assertEqual(repeated, settlement)
        with self.assertRaises(FactConflict):
            restarted_effects.authorize_dispatch(intent.effect_id, source="must-not-send")
            call_count += 1
        self.assertEqual(call_count, 1)

    def test_concurrent_claims_have_one_owner_and_same_tool_is_not_owner_identity(self) -> None:
        barrier = threading.Barrier(2)
        same_owner = digest({"process": "same-entry"})

        def acquire():
            barrier.wait(timeout=5)
            try:
                return self.facts.acquire_claim(
                    self.command.command_id,
                    claim_id="claim-" + str(uuid.uuid4()),
                    owner_sha256=same_owner,
                    executor_sha256=self.executor,
                    now=self.now,
                    expires_at=self.now + timedelta(minutes=5),
                )
            except FactConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _item: acquire(), range(2)))
        winners = [item for item in outcomes if not isinstance(item, FactConflict)]
        conflicts = [item for item in outcomes if isinstance(item, FactConflict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(winners[0].owner_sha256, same_owner)
        self.assertNotEqual(winners[0].owner_sha256, winners[0].executor_sha256)
        with self.assertRaises(FactConflict):
            self.facts.acquire_claim(
                self.command.command_id,
                claim_id="claim-" + str(uuid.uuid4()),
                owner_sha256=digest({"process": "other-entry"}),
                executor_sha256=self.executor,
                now=self.now,
                expires_at=self.now + timedelta(minutes=5),
            )
        count = self._query(
            "SELECT count(*) FROM execution_claim WHERE command_id = %s AND closed_at IS NULL",
            (self.command.command_id,),
        )[0][0]
        self.assertEqual(count, 1)

    def test_decision_requires_the_complete_effect_ledger(self) -> None:
        claim = self.services.acquire_claim(self.command)
        workspace = self.services.materialize(self.command, claim, candidate=None)
        self.addCleanup(self.services.discard_workspace, workspace)
        effects = self.services.effect_port(self.command, claim, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text("{}\n", encoding="utf-8")
        effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "local-test"},
            expected_state={"state": "complete"},
            outbound_root=outbound,
        )

        output_root = workspace.root / "manual-output"
        output_root.mkdir()
        (output_root / "model.txt").write_text("{}\n", encoding="utf-8")
        output = self.artifacts.seal_tree(
            output_root,
            kind="stage_output:freeze",
            producer_run_id=self.command.run_id,
            producer_stage=self.command.stage,
            producer_tool_sha256=self.executor,
        )
        result = StageResult(
            schema_version="description-pipeline.stage-result/v1",
            command_id=self.command.command_id,
            run_id=self.command.run_id,
            stage=self.command.stage,
            request_digest=self.command.request_digest,
            outcome="completed",
            output_artifact=output,
            actual_executor_sha256=self.executor,
            checks_sha256=digest({"checks": 1}),
            required_effect_ids=(),
        )
        record = self.artifacts.seal_stage_result(result)
        candidate = self.facts.register_candidate(
            candidate_id="candidate-" + result.digest,
            claim=claim,
            output_artifact=output,
            result_record_artifact=record,
            result=result,
            created_at=self.now,
        )
        with self.assertRaises(FactConflict):
            self.facts.commit_decision(
                decision_id="decision-forged",
                command=self.command,
                claim=claim,
                candidate=candidate,
                decided_at=self.now,
            )
        count = self._query(
            "SELECT count(*) FROM stage_decision WHERE command_id = %s",
            (self.command.command_id,),
        )[0][0]
        self.assertEqual(count, 0)

    def test_minimal_failure_envelope_does_not_require_diagnostic_seal(self) -> None:
        result = self._execute(_FailureHandler())
        self.assertNotIsInstance(result, StageResult)
        decision = self.facts.decision_for(self.command)
        self.assertIsNotNone(decision)
        self.assertIsNone(decision.result_record_artifact)
        self.assertIsNotNone(decision.failure_envelope)
        self.assertEqual(decision.failure_envelope.reason_code, "domain_input_invalid")
        self.assertEqual(decision.failure_envelope.unavailable_reason, "diagnostic sealing deliberately unavailable")
        rows = self._query(
            "SELECT result_record_artifact_id, failure_envelope, failure_digest "
            "FROM stage_decision WHERE command_id = %s",
            (self.command.command_id,),
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0][0])
        self.assertIsNotNone(rows[0][1])
        self.assertIsNotNone(rows[0][2])

    def test_unsealable_failed_execution_falls_back_to_failure_envelope(self) -> None:
        result = self._execute(_UnsealedFailureHandler())
        self.assertNotIsInstance(result, StageResult)
        self.assertEqual(result.reason_code, "domain_failed")
        self.assertEqual(result.unavailable_reason, "diagnostic_or_result_seal_failed")
        decision = self.facts.decision_for(self.command)
        self.assertIsNone(decision.result_record_artifact)
        self.assertEqual(decision.failure_envelope, result)

    def test_expected_executor_drift_stops_before_handler_execution(self) -> None:
        wrong_tool = digest({"tool": "other-release"})
        wrong_services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "wrong-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"process": "wrong-entry"}),
                executor_sha256=wrong_tool,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now,
        )
        handler = _EffectHandler()
        with self.assertRaises(ProtocolError):
            execute_stage(
                command_id=self.command.command_id,
                handler=handler,
                services=wrong_services,
            )
        self.assertEqual(handler.external_calls, 0)


if __name__ == "__main__":
    unittest.main()
