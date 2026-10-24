"""Canonical service composition with real artifact bytes and a fact-store port."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import digest
from description_pipeline.pipeline import (
    ExecutionClaim,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageExecution,
    execute_stage,
)
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy


class _Facts:
    def __init__(self, command: StageCommand, claim: ExecutionClaim, artifacts: ArtifactStore) -> None:
        self.command = command
        self.claim = claim
        self.artifacts = artifacts
        self.candidate: StageCandidate | None = None
        self.decision: StageDecision | None = None
        self.effects = {"effect-1": EffectState.SATISFIED}
        self.commit_count = 0
        self.candidate_writes = 0

    def load_command(self, command_id):
        if command_id != self.command.command_id:
            raise AssertionError(command_id)
        return self.command

    def decision_for(self, command):
        return self.decision

    def load_result(self, decision):
        return self.artifacts.read_result(decision.result_record_artifact)

    def acquire_claim(self, command_id, **kwargs):
        if command_id != self.command.command_id:
            raise AssertionError(command_id)
        return self.claim

    def candidate_for(self, command):
        return self.candidate

    def register_candidate(
        self,
        *,
        candidate_id,
        claim,
        output_artifact,
        result_record_artifact,
        result,
        created_at,
    ):
        if result.output_artifact != output_artifact:
            raise AssertionError("output artifact mismatch")
        self.candidate_writes += 1
        self.candidate = StageCandidate(
            result=result,
            result_record_artifact=result_record_artifact,
            candidate_id=candidate_id,
            claim_id=claim.claim_id,
            fence=claim.fence,
        )
        return self.candidate

    def effect_ids_for_command(self, command_id):
        if command_id != self.command.command_id:
            raise AssertionError(command_id)
        return tuple(sorted(self.effects))

    def effect_states_for_command(self, command_id):
        if command_id != self.command.command_id:
            raise AssertionError(command_id)
        return dict(self.effects)

    def commit_decision(
        self,
        *,
        decision_id,
        command,
        claim,
        candidate,
        decided_at,
        original_producer_decision_id=None,
    ):
        self.commit_count += 1
        self.decision = StageDecision(
            decision_id=decision_id,
            run_id=command.run_id,
            stage=command.stage,
            command_id=command.command_id,
            result_record_artifact=candidate.result_record_artifact,
            result_digest=candidate.result.digest,
            actual_executor_sha256=candidate.result.actual_executor_sha256,
            outcome=candidate.result.outcome,
            original_producer_decision_id=original_producer_decision_id,
            reason_code=candidate.result.reason_code,
            failure_envelope=None,
            decided_at=decided_at,
        )
        return self.decision

    def commit_failure(self, command, claim, failure):
        raise AssertionError("failure not expected")


class _Handler:
    def __init__(self) -> None:
        self.executions = 0
        self.validations = 0

    def execute(self, command, claim, workspace, *, effects):
        self.executions += 1
        output = workspace.root / "output"
        output.mkdir()
        (output / "model.txt").write_text("generated\n", encoding="utf-8")
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256=digest({"checks": 1}),
            resources_settled=True,
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        self.validations += 1
        return candidate


def command() -> StageCommand:
    return StageCommand.create(
        command_id="command-1",
        run_id="run-1",
        stage="generate",
        idempotency_key="run-1/generate",
        request={"capture": "capture-1"},
        input_artifacts=(),
        config_snapshot_sha256=digest({"config": 1}),
        contract_sha256=digest({"contract": 1}),
        policy_sha256=digest({"policy": 1}),
        expected_executor_sha256=digest({"executor": "generate"}),
        predecessor_decision_id="capture-decision",
    )


class CanonicalStageServicesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.command = command()
        self.owner = digest({"process": 1})
        self.executor = self.command.expected_executor_sha256
        self.now = datetime(2026, 10, 25, tzinfo=UTC)
        self.claim = ExecutionClaim(
            claim_id="claim-1",
            command_id=self.command.command_id,
            fence=1,
            owner_sha256=self.owner,
            executor_sha256=self.executor,
            lane_key=None,
            expires_at=self.now + timedelta(minutes=5),
        )
        self.artifacts = ArtifactStore(self.root / "cas")
        self.facts = _Facts(self.command, self.claim, self.artifacts)
        self.services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=self.root / "work",
            claim_policy=ClaimPolicy(
                owner_sha256=self.owner,
                executor_sha256=self.executor,
                lease=timedelta(minutes=5),
                claim_id_factory=lambda: self.claim.claim_id,
            ),
            clock=lambda: self.now,
        )
        self.handler = _Handler()

    def execute(self):
        return execute_stage(
            command_id=self.command.command_id,
            handler=self.handler,
            services=self.services,
        )

    def test_domain_output_and_result_record_are_separate_immutable_artifacts(self) -> None:
        result = self.execute()
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(self.facts.candidate_writes, 1)
        self.assertEqual(self.facts.commit_count, 1)
        self.assertNotEqual(
            result.output_artifact.artifact_id,
            self.facts.decision.result_record_artifact.artifact_id,
        )
        self.assertEqual(
            self.artifacts.read_result(self.facts.decision.result_record_artifact),
            result,
        )
        output = self.artifacts.materialize(result.output_artifact, self.root / "verified-output")
        self.assertEqual((output / "model.txt").read_text(encoding="utf-8"), "generated\n")

    def test_existing_decision_returns_without_new_domain_execution(self) -> None:
        first = self.execute()
        second = self.execute()
        self.assertEqual(first, second)
        self.assertEqual(self.handler.executions, 1)
        self.assertEqual(self.facts.candidate_writes, 1)
        self.assertEqual(self.facts.commit_count, 1)

    def test_sealed_candidate_resume_validates_without_rerunning_domain(self) -> None:
        self.execute()
        self.facts.decision = None
        result = self.execute()
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(self.handler.executions, 1)
        self.assertEqual(self.handler.validations, 2)
        self.assertEqual(self.facts.candidate_writes, 1)
        self.assertEqual(self.facts.commit_count, 2)


if __name__ == "__main__":
    unittest.main()
