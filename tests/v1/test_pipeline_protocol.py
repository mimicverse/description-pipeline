"""Shared six-stage protocol: identity, effects, projection and idempotence."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from description_pipeline.io import digest
from description_pipeline.pipeline import (
    ArtifactRef,
    EffectFact,
    EffectIntent,
    EffectState,
    ExecutionClaim,
    RunAdmission,
    RunStatus,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageExecution,
    StageResult,
    StageStatus,
    StageWorkspace,
    execute_stage,
    project_effect_state,
    project_run_status,
    project_stage_status,
)
from description_pipeline.pipeline.contracts import ProtocolError


def artifact(name: str, *, stage="generate") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=name,
        kind="stage_result",
        locator=f"cas://{name}",
        manifest_sha256=digest({"manifest": name}),
        set_sha256=digest({"set": name}),
        producer_run_id="run-1",
        producer_stage=stage,
        producer_tool_sha256=digest({"tool": name}),
    )


def command() -> StageCommand:
    return StageCommand.create(
        command_id="command-1",
        run_id="run-1",
        stage="generate",
        idempotency_key="run-1/generate",
        request={"capture": "capture-1"},
        input_artifacts=(artifact("capture-1", stage="capture"),),
        config_snapshot_sha256=digest({"config": 1}),
        contract_sha256=digest({"contract": 1}),
        policy_sha256=digest({"policy": 1}),
        expected_executor_sha256=digest({"executor": "generate"}),
        predecessor_decision_id="capture-decision",
    )


class EffectProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.intent = EffectIntent(
            effect_id="effect-1",
            command_id="command-1",
            effect_key="git",
            effect_kind="git_ref_update",
            target={"repository": "owner/repo", "branch": "generated"},
            expected_state={"base": "a" * 40, "head": "b" * 40},
            expected_state_digest=digest({"base": "a" * 40, "head": "b" * 40}),
            outbound_artifact_id="outbound-1",
            outbound_digest=digest({"outbound": 1}),
            created_at=datetime(2026, 10, 25, tzinfo=UTC),
        )

    def fact(
        self,
        seq: int,
        fact_type: str,
        *,
        state_class: str | None = None,
    ) -> EffectFact:
        return EffectFact(
            fact_id=f"fact-{seq}",
            effect_id=self.intent.effect_id,
            seq=seq,
            claim_id="claim-1",
            fence=1,
            fact_type=fact_type,
            source="test",
            state_class=state_class,
            payload_artifact_id=None,
            payload_digest=digest({"fact": seq}),
            observed_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(seconds=seq),
        )

    def test_timeout_remains_unknown_until_exact_observation(self) -> None:
        self.assertEqual(project_effect_state(self.intent, ()), EffectState.PREPARED)
        authorized = (self.fact(1, "dispatch_authorized"),)
        self.assertEqual(project_effect_state(self.intent, authorized), EffectState.UNKNOWN)
        timed_out = (*authorized, self.fact(2, "timeout"))
        self.assertEqual(project_effect_state(self.intent, timed_out), EffectState.UNKNOWN)
        observed = (*timed_out, self.fact(3, "observation", state_class="known"))
        self.assertEqual(project_effect_state(self.intent, observed), EffectState.SATISFIED)

    def test_exact_absent_and_unobservable_are_distinct(self) -> None:
        authorized = (self.fact(1, "dispatch_authorized"),)
        absent = (*authorized, self.fact(2, "reconciled", state_class="absent"))
        unobservable = (*authorized, self.fact(2, "reconciled", state_class="unobservable"))
        self.assertEqual(project_effect_state(self.intent, absent), EffectState.DEFINITELY_FAILED)
        self.assertEqual(project_effect_state(self.intent, unobservable), EffectState.UNKNOWN)

    def test_conflict_has_precedence(self) -> None:
        facts = (
            self.fact(1, "dispatch_authorized"),
            self.fact(2, "observation", state_class="known"),
            self.fact(3, "conflict"),
        )
        self.assertEqual(project_effect_state(self.intent, facts), EffectState.CONFLICT)

    def test_fact_sequence_and_identity_are_strict(self) -> None:
        wrong = self.fact(2, "dispatch_authorized")
        with self.assertRaises(ProtocolError):
            project_effect_state(self.intent, (wrong,))
        other = EffectFact(
            fact_id="other",
            effect_id="effect-2",
            seq=1,
            claim_id=None,
            fence=None,
            fact_type="dispatch_authorized",
            source="test",
            state_class=None,
            payload_artifact_id=None,
            payload_digest=digest({"other": 1}),
            observed_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        with self.assertRaises(ProtocolError):
            project_effect_state(self.intent, (other,))


class DerivedStateTests(unittest.TestCase):
    def test_unknown_effect_precedes_decision(self) -> None:
        self.assertEqual(
            project_stage_status(
                decision_outcome="completed",
                effect_states=("unknown",),
                predecessor_completed=True,
                open_claim=True,
            ),
            StageStatus.UNRESOLVED,
        )
        self.assertEqual(
            project_run_status(
                stage_outcomes=("completed", "failed", None, None, None, None),
                effect_states=("conflict",),
            ),
            RunStatus.UNRESOLVED,
        )

    def test_stage_and_run_status_are_derived(self) -> None:
        self.assertEqual(
            project_stage_status(
                decision_outcome=None,
                effect_states=(),
                predecessor_completed=False,
                open_claim=True,
            ),
            StageStatus.BLOCKED,
        )
        self.assertEqual(
            project_stage_status(
                decision_outcome=None,
                effect_states=(),
                predecessor_completed=True,
                open_claim=True,
            ),
            StageStatus.ACTIVE,
        )
        self.assertEqual(
            project_run_status(stage_outcomes=("completed",) * 6, effect_states=("satisfied",)),
            RunStatus.SUCCEEDED,
        )
        self.assertEqual(
            project_run_status(
                stage_outcomes=("completed", "cancelled", None, None, None, None),
                effect_states=(),
            ),
            RunStatus.CANCELLED,
        )


class CommandIdentityTests(unittest.TestCase):
    def test_run_admission_is_immutable_and_self_contained(self) -> None:
        admission = RunAdmission.create(
            run_id="run-1",
            request_id="request-1",
            parent_run_id=None,
            from_stage=None,
            request={"source": {"files": [{"path": "robot.SLDASM"}]}},
            created_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        with self.assertRaises(TypeError):
            admission.request["source"] = {}  # type: ignore[index]
        self.assertEqual(admission.request_digest, digest(admission.identity_record()))

    def test_nested_request_values_are_frozen_but_serialize_as_json(self) -> None:
        value = StageCommand.create(
            command_id="command-nested",
            run_id="run-1",
            stage="freeze",
            idempotency_key="run-1/freeze",
            request={"nested": {"items": [{"name": "source"}]}},
            input_artifacts=(),
            config_snapshot_sha256=digest({"config": 1}),
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": 1}),
            expected_executor_sha256=digest({"executor": "freeze"}),
            predecessor_decision_id=None,
        )
        with self.assertRaises(TypeError):
            value.request["nested"]["items"] = ()  # type: ignore[index]
        record = value.to_record()
        self.assertEqual(record["request"], {"nested": {"items": [{"name": "source"}]}})


class _Handler:
    def __init__(self) -> None:
        self.executions = 0
        self.validations = 0

    def execute(self, command_value, claim, workspace):
        self.executions += 1
        return StageExecution(
            outcome="completed",
            candidate_root=str(workspace.root / "generated"),
            checks_sha256=digest({"checks": 1}),
            required_effect_ids=("native-effect",),
        )

    def validate_candidate(self, command_value, claim, candidate):
        self.validations += 1
        return candidate


class _Services:
    def __init__(self, root: Path, command_value: StageCommand) -> None:
        self.root = root
        self.command = command_value
        self.candidate = None
        self.decision = None
        self.seal_calls = 0
        self.commit_calls = 0
        self.effects = {"native-effect": EffectState.SATISFIED}
        self.claim = ExecutionClaim(
            claim_id="claim-1",
            command_id=command_value.command_id,
            fence=1,
            owner_sha256=command_value.expected_executor_sha256,
            lane_key=None,
            expires_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(minutes=5),
        )

    def load_command(self, command_id):
        if command_id != self.command.command_id:
            raise AssertionError(command_id)
        return self.command

    def decision_for(self, command_value):
        return self.decision

    def load_result(self, decision):
        return self.candidate.result

    def verify_decision(self, decision, result):
        self.decision = decision

    def acquire_claim(self, command_value):
        return self.claim

    def candidate_for(self, command_value, claim):
        return self.candidate

    def materialize(self, command_value, claim, *, candidate):
        root = self.root / f"fence-{claim.fence}"
        root.mkdir(parents=True, exist_ok=True)
        return StageWorkspace(root=root, inputs={})

    def seal_candidate(self, command_value, claim, execution):
        self.seal_calls += 1
        result_artifact = ArtifactRef(
            artifact_id="result-1",
            kind="stage_result",
            locator="cas://result-1",
            manifest_sha256=digest({"manifest": 1}),
            set_sha256=digest({"set": 1}),
            producer_run_id=command_value.run_id,
            producer_stage=command_value.stage,
            producer_tool_sha256=command_value.expected_executor_sha256,
        )
        result = StageResult(
            schema_version="description-pipeline.stage-result/v1",
            command_id=command_value.command_id,
            run_id=command_value.run_id,
            stage=command_value.stage,
            request_digest=command_value.request_digest,
            outcome=execution.outcome,
            result_artifact=result_artifact,
            actual_executor_sha256=command_value.expected_executor_sha256,
            checks_sha256=execution.checks_sha256,
            required_effect_ids=execution.required_effect_ids,
            reason_code=execution.reason_code,
        )
        self.candidate = StageCandidate(result=result)
        return self.candidate

    def verify_candidate(self, command_value, claim, candidate):
        return candidate

    def effect_state(self, effect_id):
        return self.effects[effect_id]

    def commit_decision(self, command_value, claim, candidate):
        self.commit_calls += 1
        self.decision = StageDecision(
            decision_id=f"decision-{self.commit_calls}",
            run_id=command_value.run_id,
            stage=command_value.stage,
            command_id=command_value.command_id,
            result_artifact=candidate.result.result_artifact,
            result_digest=candidate.result.digest,
            actual_executor_sha256=candidate.result.actual_executor_sha256,
            outcome=candidate.result.outcome,
            original_producer_decision_id=None,
            reason_code=candidate.result.reason_code,
            decided_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        return self.decision


class StageEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.command = command()
        self.services = _Services(Path(self.temporary.name), self.command)
        self.handler = _Handler()

    def test_execute_once_and_return_the_committed_result(self) -> None:
        first = execute_stage(
            command_id=self.command.command_id,
            handler=self.handler,
            services=self.services,
        )
        second = execute_stage(
            command_id=self.command.command_id,
            handler=self.handler,
            services=self.services,
        )
        self.assertEqual(first, second)
        self.assertEqual(self.handler.executions, 1)
        self.assertEqual(self.handler.validations, 0)
        self.assertEqual(self.services.seal_calls, 1)
        self.assertEqual(self.services.commit_calls, 1)

    def test_unknown_effect_blocks_a_terminal_decision(self) -> None:
        self.services.effects["native-effect"] = EffectState.UNKNOWN
        with self.assertRaises(ProtocolError) as context:
            execute_stage(
                command_id=self.command.command_id,
                handler=self.handler,
                services=self.services,
            )
        self.assertEqual(context.exception.code, "effect.unsettled")
        self.assertEqual(self.services.commit_calls, 0)
        self.assertIsNotNone(self.services.candidate)

    def test_existing_candidate_is_revalidated_without_rerunning_domain(self) -> None:
        execute_stage(
            command_id=self.command.command_id,
            handler=self.handler,
            services=self.services,
        )
        self.services.decision = None
        execute_stage(
            command_id=self.command.command_id,
            handler=self.handler,
            services=self.services,
        )
        self.assertEqual(self.handler.executions, 1)
        self.assertEqual(self.handler.validations, 1)
        self.assertEqual(self.services.commit_calls, 2)


if __name__ == "__main__":
    unittest.main()
