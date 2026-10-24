"""Shared six-stage protocol: identity, effects, projection and idempotence."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from description_pipeline.io import digest
from description_pipeline.pipeline.contracts import (
    ArtifactRef,
    DispatchCommand,
    DispatchConf,
    ExecutionClaim,
    FailureEnvelope,
    ProtocolError,
    RunAdmission,
    RunStatus,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageEffectWait,
    StageExecution,
    StagePending,
    StageResult,
    StageStatus,
    project_run_status,
    project_stage_status,
)
from description_pipeline.pipeline.effects import (
    EffectFact,
    EffectIntent,
    EffectState,
    project_effect_state,
)
from description_pipeline.pipeline.engine import StageWorkspace, execute_stage
from description_pipeline.schemas import (
    AIRFLOW_DISPATCH_CONF_SCHEMA,
    FAILURE_ENVELOPE_SCHEMA,
    STAGE_EFFECT_WAIT_SCHEMA,
    STAGE_RESULT_SCHEMA,
)


def artifact(name: str, *, stage="generate") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=name,
        kind="stage_result",
        locator=f"cas://artifact/{name}",
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
        expected = {
            "schema_version": "solidworks-to-urdf.git-ref-update/v1",
            "repository": "owner/repo",
            "base_ref": "refs/heads/main",
            "expected_base_oid": "a" * 40,
            "branch_ref": "refs/heads/generated",
            "expected_head_oid": "b" * 40,
            "candidate_commit": "c" * 40,
            "candidate_tree": "d" * 40,
            "subject_sha256": digest({"subject": 1}),
        }
        self.intent = EffectIntent(
            effect_id="effect-1",
            command_id="command-1",
            effect_key="git",
            effect_kind="git_ref_update",
            target={"repository": "owner/repo", "branch_ref": "refs/heads/generated"},
            expected_state=expected,
            expected_state_digest=digest(expected),
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
            payload_artifact_id=f"artifact-{seq}" if state_class is not None else None,
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

    def test_late_fence_evidence_cannot_downgrade_conflict(self) -> None:
        facts = (
            self.fact(1, "dispatch_authorized"),
            self.fact(2, "observation", state_class="known"),
            self.fact(3, "conflict"),
            self.fact(4, "response"),
            self.fact(5, "observation", state_class="known"),
        )
        self.assertEqual(project_effect_state(self.intent, facts), EffectState.CONFLICT)

    def test_conflict_is_absorbing_until_evidence_backed_reconciliation(self) -> None:
        absorbing = (
            self.fact(1, "dispatch_authorized"),
            self.fact(2, "conflict"),
            self.fact(3, "dispatch_authorized"),
            self.fact(4, "observation", state_class="known"),
        )
        self.assertEqual(project_effect_state(self.intent, absorbing), EffectState.CONFLICT)
        unresolved = (
            *absorbing,
            self.fact(5, "reconciled", state_class="known"),
        )
        self.assertEqual(project_effect_state(self.intent, unresolved), EffectState.CONFLICT)
        resolved = (
            *absorbing,
            replace(
                self.fact(5, "reconciled", state_class="known"),
                metadata_artifact_id="reconciliation-metadata",
            ),
        )
        self.assertEqual(project_effect_state(self.intent, resolved), EffectState.SATISFIED)

    def test_new_dispatch_after_known_is_unknown(self) -> None:
        facts = (
            self.fact(1, "observation", state_class="known"),
            self.fact(2, "dispatch_authorized"),
        )
        self.assertEqual(project_effect_state(self.intent, facts), EffectState.UNKNOWN)

    def test_late_response_and_timeout_do_not_downgrade_a_terminal_observation(self) -> None:
        facts = (
            self.fact(1, "dispatch_authorized"),
            self.fact(2, "observation", state_class="known"),
            self.fact(3, "response"),
            self.fact(4, "timeout"),
        )
        settlement = project_effect_state(self.intent, facts)
        self.assertEqual(settlement, EffectState.SATISFIED)

    def test_late_observation_and_reconciliation_do_not_rewrite_a_terminal_effect(self) -> None:
        facts = (
            self.fact(1, "dispatch_authorized"),
            self.fact(2, "reconciled", state_class="known"),
            self.fact(3, "observation", state_class="unobservable"),
            self.fact(4, "observation", state_class="absent"),
            self.fact(5, "reconciled", state_class="absent"),
        )
        self.assertEqual(project_effect_state(self.intent, facts), EffectState.SATISFIED)

    def test_absent_observation_after_send_is_not_definite_failure(self) -> None:
        facts = (
            self.fact(1, "dispatch_authorized"),
            self.fact(2, "observation", state_class="absent"),
        )
        self.assertEqual(project_effect_state(self.intent, facts), EffectState.UNKNOWN)

    def test_fact_sequence_and_identity_are_strict(self) -> None:
        wrong = self.fact(2, "dispatch_authorized")
        with self.assertRaises(ProtocolError):
            project_effect_state(self.intent, (wrong,))
        other = EffectFact(
            fact_id="other",
            effect_id="effect-2",
            seq=1,
            claim_id="claim-2",
            fence=2,
            fact_type="dispatch_authorized",
            source="test",
            state_class=None,
            payload_artifact_id=None,
            payload_digest=digest({"other": 1}),
            observed_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        with self.assertRaises(ProtocolError):
            project_effect_state(self.intent, (other,))

    def test_git_and_pr_expected_states_are_independently_fixed(self) -> None:
        git = self.intent
        pr_expected = {
            "schema_version": "solidworks-to-urdf.pr-upsert/v1",
            "repository": "owner/repo",
            "base_ref": "refs/heads/main",
            "head_ref": "refs/heads/generated",
            "candidate_commit": "c" * 40,
            "subject_sha256": digest({"subject": 1}),
            "title_digest": digest({"title": 1}),
            "body_digest": digest({"body": 1}),
            "expected_pr": None,
        }
        pr = EffectIntent(
            effect_id="effect-2",
            command_id="command-1",
            effect_key="pr",
            effect_kind="pr_upsert",
            target={
                "repository": "owner/repo",
                "base_ref": "refs/heads/main",
                "head_ref": "refs/heads/generated",
            },
            expected_state=pr_expected,
            expected_state_digest=digest(pr_expected),
            outbound_artifact_id="outbound-2",
            outbound_digest=digest({"outbound": 2}),
            created_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        self.assertNotEqual(git.effect_id, pr.effect_id)
        changed_target = dict(pr.target, head_ref="refs/heads/other")
        with self.assertRaises(ProtocolError):
            EffectIntent(
                effect_id="effect-3",
                command_id="command-1",
                effect_key="pr-mismatch",
                effect_kind="pr_upsert",
                target=changed_target,
                expected_state=pr_expected,
                expected_state_digest=digest(pr_expected),
                outbound_artifact_id="outbound-3",
                outbound_digest=digest({"outbound": 3}),
                created_at=datetime(2026, 10, 25, tzinfo=UTC),
            )

    def test_effect_intent_identity_ignores_only_creation_time(self) -> None:
        replay = EffectIntent(
            effect_id=self.intent.effect_id,
            command_id=self.intent.command_id,
            effect_key=self.intent.effect_key,
            effect_kind=self.intent.effect_kind,
            target=self.intent.target,
            expected_state=self.intent.expected_state,
            expected_state_digest=self.intent.expected_state_digest,
            outbound_artifact_id=self.intent.outbound_artifact_id,
            outbound_digest=self.intent.outbound_digest,
            created_at=datetime(2026, 10, 26, tzinfo=UTC),
        )
        self.assertEqual(replay.identity_digest, self.intent.identity_digest)


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

    def test_completed_decisions_require_satisfied_effects(self) -> None:
        self.assertEqual(
            project_stage_status(
                decision_outcome="completed",
                effect_states=("definitely_failed",),
                predecessor_completed=True,
                open_claim=False,
            ),
            StageStatus.UNRESOLVED,
        )
        self.assertEqual(
            project_run_status(
                stage_outcomes=("completed",) * 6,
                effect_states=("definitely_failed",),
            ),
            RunStatus.UNRESOLVED,
        )

    def test_pending_effect_is_waiting_without_hiding_other_unknowns(self) -> None:
        self.assertEqual(
            project_stage_status(
                decision_outcome=None,
                effect_states=("unknown",),
                predecessor_completed=True,
                open_claim=False,
                pending_effect_states=("unknown",),
            ),
            StageStatus.WAITING,
        )
        self.assertEqual(
            project_run_status(
                stage_outcomes=("completed", None, None, None, None, None),
                effect_states=("unknown",),
                pending_effect_states=("unknown",),
            ),
            RunStatus.INCOMPLETE,
        )
        self.assertEqual(
            project_stage_status(
                decision_outcome=None,
                effect_states=("unknown", "unknown"),
                predecessor_completed=True,
                open_claim=False,
                pending_effect_states=("unknown",),
            ),
            StageStatus.UNRESOLVED,
        )


class CommandIdentityTests(unittest.TestCase):
    def test_stage_effect_wait_is_strict_non_secret_identity(self) -> None:
        value = StagePending(
            StageEffectWait(
                schema_version=STAGE_EFFECT_WAIT_SCHEMA,
                run_id="run-1",
                stage="capture",
                command_id="capture:command",
                request_digest=digest({"request": 1}),
                effect_id="effect-native",
                effect_key="native-capture",
                effect_kind="native_operation",
                intent_identity_digest=digest({"intent": 1}),
                outbound_artifact_id="outbound-1",
                outbound_digest=digest({"outbound": 1}),
                expected_state_digest=digest({"expected": 1}),
            )
        )
        record = value.wait.to_record()
        self.assertEqual(StageEffectWait.from_record(record), value.wait)
        self.assertEqual(len(value.wait.digest), 64)
        self.assertNotIn("locator", record)
        self.assertNotIn("claim_id", record)
        self.assertNotIn("fence", record)
        with self.assertRaises(ProtocolError):
            StageEffectWait.from_record({**record, "claim_id": "foreign"})

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

    def test_path_bearing_identifiers_and_locators_are_canonical(self) -> None:
        with self.assertRaises(ProtocolError):
            StageCommand.create(
                command_id="../escape",
                run_id="run-1",
                stage="freeze",
                idempotency_key="run-1/freeze",
                request={},
                input_artifacts=(),
                config_snapshot_sha256=digest({"config": 1}),
                contract_sha256=digest({"contract": 1}),
                policy_sha256=digest({"policy": 1}),
                expected_executor_sha256=digest({"executor": "freeze"}),
                predecessor_decision_id=None,
            )

    def test_completed_execution_requires_resource_termination(self) -> None:
        with self.assertRaises(ProtocolError):
            StageExecution(
                outcome="completed",
                candidate_root="candidate",
                checks_sha256=digest({"checks": 1}),
                resources_settled=False,
            )

    def test_artifact_locator_must_match_its_immutable_id(self) -> None:
        with self.assertRaises(ProtocolError):
            ArtifactRef(
                artifact_id="artifact-1",
                kind="input_set",
                locator="cas://artifact/../escape",
                manifest_sha256=digest({"manifest": 1}),
                set_sha256=digest({"set": 1}),
                producer_run_id="run-1",
                producer_stage="freeze",
                producer_tool_sha256=digest({"tool": 1}),
            )

    def test_technical_dispatch_round_trips_its_non_secret_fixed_identity(self) -> None:
        executor = digest({"tool": "dispatch"})
        conf_artifact = ArtifactRef(
            artifact_id="dispatch-conf",
            kind="airflow_dispatch_conf",
            locator="cas://artifact/dispatch-conf",
            manifest_sha256=digest({"manifest": "dispatch-conf"}),
            set_sha256=digest({"set": "dispatch-conf"}),
            producer_run_id="run-1",
            producer_stage=None,
            producer_tool_sha256=executor,
        )
        command = DispatchCommand.create(
            command_id="command-dispatch",
            run_id="run-1",
            freeze_command_id="command-freeze",
            idempotency_key="run-1/dispatch",
            dag_id="solidworks_to_urdf",
            dag_run_id="portal-run-1",
            conf_artifact=conf_artifact,
            expected_executor_sha256=executor,
        )
        self.assertEqual(DispatchCommand.from_record(command.to_record()), command)
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id="run-1",
            freeze_command_id="command-freeze",
        )
        self.assertEqual(DispatchConf.from_record(conf.to_record()), conf)
        forged = command.to_record()
        forged["dag_run_id"] = "another-run"
        with self.assertRaises(ProtocolError):
            DispatchCommand.from_record(forged)


class _Handler:
    def __init__(self) -> None:
        self.executions = 0
        self.validations = 0

    def execute(self, command_value, claim, workspace, *, effects):
        self.executions += 1
        return StageExecution(
            outcome="completed",
            candidate_root=str(workspace.root / "generated"),
            checks_sha256=digest({"checks": 1}),
            resources_settled=True,
        )

    def validate_candidate(self, command_value, claim, candidate, *, effects):
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
        self.cancel_command = None
        self.claim = ExecutionClaim(
            claim_id="claim-1",
            command_id=command_value.command_id,
            fence=1,
            owner_sha256=digest({"process": "test"}),
            executor_sha256=command_value.expected_executor_sha256,
            lane_key=None,
            expires_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(minutes=5),
        )

    def load_command(self, command_id):
        if command_id != self.command.command_id:
            raise AssertionError(command_id)
        return self.command

    def decision_for(self, command_value):
        return self.decision

    def cancellation_for(self, command_value):
        return self.cancel_command

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
        output_artifact = ArtifactRef(
            artifact_id="output-1",
            kind="stage_output",
            locator="cas://artifact/output-1",
            manifest_sha256=digest({"manifest": 1}),
            set_sha256=digest({"set": 1}),
            producer_run_id=command_value.run_id,
            producer_stage=command_value.stage,
            producer_tool_sha256=command_value.expected_executor_sha256,
        )
        result_record_artifact = ArtifactRef(
            artifact_id="result-1",
            kind="stage_result",
            locator="cas://artifact/result-1",
            manifest_sha256=digest({"manifest": "result"}),
            set_sha256=digest({"set": "result"}),
            producer_run_id=command_value.run_id,
            producer_stage=command_value.stage,
            producer_tool_sha256=command_value.expected_executor_sha256,
        )
        result = StageResult(
            schema_version=STAGE_RESULT_SCHEMA,
            command_id=command_value.command_id,
            run_id=command_value.run_id,
            stage=command_value.stage,
            request_digest=command_value.request_digest,
            outcome=execution.outcome,
            output_artifact=output_artifact,
            actual_executor_sha256=command_value.expected_executor_sha256,
            checks_sha256=execution.checks_sha256,
            resources_settled=execution.resources_settled,
            required_effect_ids=("native-effect",),
            reason_code=execution.reason_code,
        )
        self.candidate = StageCandidate(
            result=result,
            result_record_artifact=result_record_artifact,
            candidate_id=f"candidate-{result.digest}",
            claim_id=claim.claim_id,
            fence=claim.fence,
        )
        return self.candidate

    def verify_candidate(self, command_value, claim, candidate):
        return candidate

    def effect_states_for_command(self, command_id):
        return dict(self.effects)

    def pending_effect_for_command(self, command_id):
        return None

    def effect_port(self, command_value, claim, workspace):
        return object()

    def commit_decision(self, command_value, claim, candidate):
        self.commit_calls += 1
        self.decision = StageDecision(
            decision_id=f"decision-{self.commit_calls}",
            run_id=command_value.run_id,
            stage=command_value.stage,
            command_id=command_value.command_id,
            result_record_artifact=candidate.result_record_artifact,
            result_digest=candidate.result.digest,
            actual_executor_sha256=candidate.result.actual_executor_sha256,
            outcome=candidate.result.outcome,
            original_producer_decision_id=None,
            reason_code=candidate.result.reason_code,
            failure_envelope=None,
            decided_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        return self.decision

    def commit_failure(self, command_value, claim, failure, *, workspace_root):
        raise AssertionError("failure not expected")

    def discard_workspace(self, workspace):
        return None


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
        self.assertEqual(self.handler.validations, 1)
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
        self.assertEqual(self.handler.validations, 2)
        self.assertEqual(self.services.commit_calls, 2)

    def test_cached_completed_decision_fails_closed_when_its_effect_conflicts(self) -> None:
        execute_stage(
            command_id=self.command.command_id,
            handler=self.handler,
            services=self.services,
        )
        self.services.effects["native-effect"] = EffectState.CONFLICT
        with self.assertRaises(ProtocolError) as context:
            execute_stage(
                command_id=self.command.command_id,
                handler=self.handler,
                services=self.services,
            )
        self.assertEqual(context.exception.code, "effect.unsettled")
        self.assertEqual(self.handler.executions, 1)

    def test_cached_failure_requires_original_command_and_current_ledger(self) -> None:
        failure = FailureEnvelope(
            schema_version=FAILURE_ENVELOPE_SCHEMA,
            command_id=self.command.command_id,
            run_id=self.command.run_id,
            stage="generate",
            request_digest=self.command.request_digest,
            outcome="failed",
            actual_executor_sha256=self.command.expected_executor_sha256,
            reason_code="domain_failure",
            error_type="DomainFailure",
            error_message="domain failure",
            error_detail=None,
            effect_states=(("native-effect", "definitely_failed"),),
            evidence_artifacts=(),
            retained_locations=(),
            unavailable_reason=None,
            occurred_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        self.services.decision = StageDecision(
            decision_id="decision-failure",
            run_id=self.command.run_id,
            stage="generate",
            command_id=self.command.command_id,
            result_record_artifact=None,
            result_digest=None,
            actual_executor_sha256=self.command.expected_executor_sha256,
            outcome="failed",
            original_producer_decision_id=None,
            reason_code="domain_failure",
            failure_envelope=failure,
            decided_at=datetime(2026, 10, 25, tzinfo=UTC),
        )
        self.services.effects["native-effect"] = EffectState.CONFLICT
        with self.assertRaises(ProtocolError) as context:
            execute_stage(
                command_id=self.command.command_id,
                handler=self.handler,
                services=self.services,
            )
        self.assertEqual(context.exception.code, "decision.effect_changed")
        self.assertEqual(self.handler.executions, 0)

    def test_cached_decision_cannot_be_reused_by_another_command(self) -> None:
        execute_stage(
            command_id=self.command.command_id,
            handler=self.handler,
            services=self.services,
        )
        self.services.decision = replace(self.services.decision, command_id="other-command")
        with self.assertRaises(ProtocolError) as context:
            execute_stage(
                command_id=self.command.command_id,
                handler=self.handler,
                services=self.services,
            )
        self.assertEqual(context.exception.code, "decision.command_mismatch")
        self.assertEqual(self.handler.executions, 1)


if __name__ == "__main__":
    unittest.main()
