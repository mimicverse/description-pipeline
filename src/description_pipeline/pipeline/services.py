"""Production composition of the stage engine, fact store and artifact store."""

from __future__ import annotations

import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..artifacts import ArtifactStore
from ..io import digest
from ..platform.facts import FactStore
from .contracts import (
    ArtifactRef,
    ExecutionClaim,
    FailureEnvelope,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageExecution,
    StageFailure,
    StageResult,
)
from .effects import EffectFact, EffectIntent, EffectKind, EffectSettlement, EffectState, StageEffectPort
from .engine import StageWorkspace


@dataclass(frozen=True, slots=True)
class ClaimPolicy:
    #: One concrete process/execution-owner instance. It must never be reused by a
    #: concurrent entry or identified with the tool release.
    owner_sha256: str
    #: The actual tool/distribution identity expected by the stage command.
    executor_sha256: str
    lease: timedelta
    lane_key: str | None = None
    dag_id: str | None = None
    dag_run_id: str | None = None
    task_id: str | None = None
    map_index: int | None = None
    claim_id_factory: Callable[[], str] = lambda: str(uuid.uuid4())

    def __post_init__(self) -> None:
        for name in ("owner_sha256", "executor_sha256"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ProtocolError("claim_policy.invalid", {"where": name})
        if self.owner_sha256 == self.executor_sha256:
            raise ProtocolError("claim_policy.invalid", {"reason": "owner-tool-identity-collision"})
        if not isinstance(self.lease, timedelta) or self.lease <= timedelta(0):
            raise ProtocolError("claim_policy.invalid", {"where": "lease"})
        for name in ("lane_key", "dag_id", "dag_run_id", "task_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ProtocolError("claim_policy.invalid", {"where": name})
        if self.map_index is not None and type(self.map_index) is not int:
            raise ProtocolError("claim_policy.invalid", {"where": "map_index"})


class CanonicalStageServices:
    """The concrete StageServices port for one executor and scratch root."""

    def __init__(
        self,
        *,
        facts: FactStore,
        artifacts: ArtifactStore,
        work_root: str | Path,
        claim_policy: ClaimPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        self.facts = facts
        self.artifacts = artifacts
        supplied_root = Path(work_root)
        if supplied_root.exists() and (supplied_root.is_symlink() or supplied_root.is_junction()):
            raise ProtocolError("workspace.invalid", {"root": str(supplied_root)})
        supplied_root.mkdir(parents=True, exist_ok=True)
        self.work_root = supplied_root.resolve()
        if any(parent.is_symlink() for parent in self.work_root.parents):
            raise ProtocolError("workspace.invalid", {"root": str(self.work_root), "reason": "symlink-parent"})
        self.claim_policy = claim_policy
        self.clock = clock
        self._workspace_lock = threading.RLock()
        self._owned_workspaces: dict[Path, tuple[str, str, int]] = {}

    def load_command(self, command_id: str) -> StageCommand:
        return self.facts.load_command(command_id)

    def decision_for(self, command: StageCommand) -> StageDecision | None:
        return self.facts.decision_for(command)

    def load_result(self, decision: StageDecision) -> StageResult:
        return self.facts.load_result(decision)

    def verify_decision(self, decision: StageDecision, result: StageResult) -> None:
        recorded = self.facts.load_result(decision)
        self.artifacts.verify(result.output_artifact)
        self.artifacts.verify(decision.result_record_artifact)
        if recorded != result:
            raise ProtocolError("decision.result_mismatch", {"decision_id": decision.decision_id})

    def acquire_claim(self, command: StageCommand) -> ExecutionClaim:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ProtocolError("clock.invalid", {"where": "acquire_claim"})
        policy = self.claim_policy
        if policy.executor_sha256 != command.expected_executor_sha256:
            raise ProtocolError(
                "claim.executor_mismatch",
                {
                    "expected": command.expected_executor_sha256,
                    "actual": policy.executor_sha256,
                },
            )
        return self.facts.acquire_claim(
            command.command_id,
            claim_id=policy.claim_id_factory(),
            owner_sha256=policy.owner_sha256,
            executor_sha256=policy.executor_sha256,
            now=now,
            expires_at=now + policy.lease,
            lane_key=policy.lane_key,
            dag_id=policy.dag_id,
            dag_run_id=policy.dag_run_id,
            task_id=policy.task_id,
            map_index=policy.map_index,
        )

    def candidate_for(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
    ) -> StageCandidate | None:
        if claim.command_id != command.command_id:
            raise ProtocolError("claim.command_mismatch", {"command_id": command.command_id})
        return self.facts.candidate_for(command)

    def materialize(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        *,
        candidate: StageCandidate | None,
    ) -> StageWorkspace:
        command_root = self.work_root / command.command_id
        if command_root.exists() and (command_root.is_symlink() or command_root.is_junction()):
            raise ProtocolError("workspace.escape", {"root": str(command_root)})
        command_root.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix=f"{claim.fence}-", dir=command_root))
        with self._workspace_lock:
            self._owned_workspaces[root] = (command.command_id, claim.claim_id, claim.fence)
        try:
            inputs = root / "inputs"
            inputs.mkdir()
            materialized: dict[str, Path] = {}
            for artifact in command.input_artifacts:
                materialized[artifact.artifact_id] = self.artifacts.materialize(
                    artifact,
                    inputs / artifact.artifact_id,
                )
            if candidate is not None:
                self.artifacts.materialize(
                    candidate.result.output_artifact,
                    root / "candidate-output",
                )
                if candidate.result_record_artifact is not None:
                    self.artifacts.materialize(
                        candidate.result_record_artifact,
                        root / "candidate-result-record",
                    )
            return StageWorkspace(root=root, inputs=materialized)
        except BaseException:
            with self._workspace_lock:
                self._owned_workspaces.pop(root, None)
            shutil.rmtree(root, ignore_errors=True)
            raise

    def seal_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        execution: StageExecution,
    ) -> StageCandidate:
        if command.stage is None:
            raise ProtocolError("candidate.stage_required", {"command_id": command.command_id})
        supplied = Path(execution.candidate_root)
        if supplied.is_symlink() or supplied.is_junction():
            raise ProtocolError("candidate.path_invalid", {"reason": "symlink"})
        candidate_root = supplied.resolve()
        with self._workspace_lock:
            owner = next(
                (
                    (root, identity)
                    for root, identity in self._owned_workspaces.items()
                    if candidate_root.is_relative_to(root)
                ),
                None,
            )
        if owner is None:
            raise ProtocolError("candidate.path_invalid", {"reason": "outside-owned-workspace"})
        _root, identity = owner
        if identity != (command.command_id, claim.claim_id, claim.fence):
            raise ProtocolError("candidate.path_invalid", {"reason": "foreign-claim-workspace"})
        output = self.artifacts.seal_tree(
            execution.candidate_root,
            kind=f"stage_output:{command.stage}",
            producer_run_id=command.run_id,
            producer_stage=command.stage,
            producer_tool_sha256=claim.executor_sha256,
        )
        result = StageResult(
            schema_version="description-pipeline.stage-result/v1",
            command_id=command.command_id,
            run_id=command.run_id,
            stage=command.stage,
            request_digest=command.request_digest,
            outcome=execution.outcome,
            output_artifact=output,
            actual_executor_sha256=claim.executor_sha256,
            checks_sha256=execution.checks_sha256,
            resources_settled=execution.resources_settled,
            required_effect_ids=self.facts.effect_ids_for_command(command.command_id),
            reason_code=execution.reason_code,
        )
        result_record = self.artifacts.seal_stage_result(result)
        candidate_id = f"candidate-{result.digest}"
        return self.facts.register_candidate(
            candidate_id=candidate_id,
            claim=claim,
            output_artifact=output,
            result_record_artifact=result_record,
            result=result,
            created_at=self.clock(),
        )

    def verify_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
    ) -> StageCandidate:
        if candidate.candidate_id is None:
            raise ProtocolError("candidate.not_persisted", {"command_id": command.command_id})
        recorded = self.facts.candidate_for(command)
        if recorded is None or recorded != candidate:
            raise ProtocolError("candidate.changed", {"command_id": command.command_id})
        self.artifacts.verify(candidate.result.output_artifact)
        if candidate.result_record_artifact is None:
            raise ProtocolError("candidate.record_artifact_missing", {"command_id": command.command_id})
        self.artifacts.verify(candidate.result_record_artifact)
        if self.artifacts.read_result(candidate.result_record_artifact) != candidate.result:
            raise ProtocolError("candidate.result_mismatch", {"command_id": command.command_id})
        return candidate

    def effect_states_for_command(self, command_id: str) -> dict[str, EffectState]:
        return self.facts.effect_states_for_command(command_id)

    def commit_decision(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
    ) -> StageDecision:
        if candidate.candidate_id is None:
            raise ProtocolError("candidate.not_persisted", {"command_id": command.command_id})
        return self.facts.commit_decision(
            decision_id=f"decision-{command.command_id}",
            command=command,
            claim=claim,
            candidate=candidate,
            decided_at=self.clock(),
        )

    def commit_failure(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        failure: StageFailure,
        *,
        workspace_root: str | Path,
    ) -> FailureEnvelope:
        states = self.facts.effect_states_for_command(command.command_id)
        if any(state in {EffectState.UNKNOWN, EffectState.CONFLICT} for state in states.values()):
            raise ProtocolError(
                "failure.effect_unresolved",
                {
                    "command_id": command.command_id,
                    "effect_states": {effect_id: state.value for effect_id, state in sorted(states.items())},
                },
            )
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ProtocolError("clock.invalid", {"where": "commit_failure"})
        workspace = Path(workspace_root).resolve()
        retained_locations = [str(workspace)]
        for locator in failure.retained_locations:
            path = Path(locator).resolve()
            if not path.is_relative_to(workspace):
                raise ProtocolError("failure.retained_path_escape", {"locator": locator})
            retained_locations.append(str(path))
        verified_evidence = []
        unavailable_reason = failure.unavailable_reason
        for artifact in failure.evidence_artifacts:
            try:
                self.artifacts.verify(artifact)
            except Exception:
                unavailable_reason = "failure_evidence_artifact_unavailable"
                continue
            verified_evidence.append(artifact)
        envelope = FailureEnvelope(
            schema_version="description-pipeline.failure-envelope/v1",
            command_id=command.command_id,
            run_id=command.run_id,
            stage=command.stage,
            request_digest=command.request_digest,
            outcome=failure.outcome,
            actual_executor_sha256=claim.executor_sha256,
            reason_code=failure.reason_code,
            error_type=failure.error_type,
            error_message=str(failure)[:4096],
            error_detail=failure.detail,
            effect_states=tuple((effect_id, state.value) for effect_id, state in sorted(states.items())),
            evidence_artifacts=tuple(verified_evidence),
            retained_locations=tuple(dict.fromkeys(retained_locations)),
            unavailable_reason=unavailable_reason,
            occurred_at=now,
        )
        return self.facts.commit_failure_envelope(
            decision_id=f"decision-{command.command_id}",
            command=command,
            claim=claim,
            failure=envelope,
        )

    def effect_port(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
    ) -> StageEffectPort:
        if claim.command_id != command.command_id or claim.executor_sha256 != command.expected_executor_sha256:
            raise ProtocolError("claim.command_mismatch", {"command_id": command.command_id})
        with self._workspace_lock:
            identity = self._owned_workspaces.get(workspace.root)
        if identity != (command.command_id, claim.claim_id, claim.fence):
            raise ProtocolError("workspace.foreign_claim", {"command_id": command.command_id})
        return BoundStageEffectPort(
            facts=self.facts,
            artifacts=self.artifacts,
            command=command,
            claim=claim,
            workspace_root=workspace.root,
            clock=self.clock,
        )

    def discard_workspace(self, workspace: StageWorkspace) -> None:
        """Delete only a workspace created by this service; it is never authority."""

        root = workspace.root.resolve()
        if not root.is_relative_to(self.work_root.resolve()) or root == self.work_root.resolve():
            raise ProtocolError("workspace.escape", {"root": str(root)})
        with self._workspace_lock:
            self._owned_workspaces.pop(root, None)
        shutil.rmtree(root, ignore_errors=True)


class BoundStageEffectPort:
    """The command/claim-bound capability passed to one stage handler."""

    def __init__(
        self,
        *,
        facts: FactStore,
        artifacts: ArtifactStore,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace_root: Path,
        clock: Callable[[], datetime],
    ) -> None:
        self.facts = facts
        self.artifacts = artifacts
        self.command = command
        self.claim = claim
        self.workspace_root = workspace_root.resolve()
        self.clock = clock

    def prepare_effect(
        self,
        *,
        effect_key: str,
        effect_kind: EffectKind,
        target: Mapping[str, object],
        expected_state: Mapping[str, object],
        outbound_root: str | Path,
    ) -> EffectIntent:
        outbound = self._seal_payload(outbound_root, "effect_payload")
        target_value = self._plain(target)
        expected_value = self._plain(expected_state)
        expected_digest = digest(expected_value)
        identity = {
            "command_id": self.command.command_id,
            "effect_key": effect_key,
            "effect_kind": effect_kind,
            "expected_state_digest": expected_digest,
            "outbound_artifact_id": outbound.artifact_id,
            "target": target_value,
        }
        intent = EffectIntent(
            effect_id="effect-" + digest(identity),
            command_id=self.command.command_id,
            effect_key=effect_key,
            effect_kind=effect_kind,
            target=target_value,
            expected_state=expected_value,
            expected_state_digest=expected_digest,
            outbound_artifact_id=outbound.artifact_id,
            outbound_digest=outbound.set_sha256,
            created_at=self._now(),
        )
        return self.facts.register_effect_intent(
            intent,
            claim=self.claim,
            outbound_artifact=outbound,
        )

    def authorize_dispatch(self, effect_id: str, *, source: str) -> EffectFact:
        return self.facts.authorize_effect_dispatch(
            effect_id=effect_id,
            claim=self.claim,
            source=source,
            payload_digest=digest(
                {
                    "effect_id": effect_id,
                    "claim_id": self.claim.claim_id,
                    "fence": self.claim.fence,
                }
            ),
            observed_at=self._now(),
            fact_id="fact-" + str(uuid.uuid4()),
        )

    def record_response(
        self,
        effect_id: str,
        *,
        source: str,
        response_root: str | Path,
    ) -> EffectFact:
        payload = self._seal_payload(response_root, "effect_response")
        return self._append(effect_id, "response", source=source, payload=payload)

    def record_timeout(
        self,
        effect_id: str,
        *,
        source: str,
        detail_root: str | Path | None = None,
    ) -> EffectFact:
        payload = self._seal_payload(detail_root, "effect_timeout") if detail_root is not None else None
        return self._append(effect_id, "timeout", source=source, payload=payload)

    def record_observation(
        self,
        effect_id: str,
        *,
        source: str,
        state_class: str,
        observation_root: str | Path,
    ) -> EffectFact:
        payload = self._seal_payload(observation_root, "effect_observation")
        return self._append(
            effect_id,
            "observation",
            source=source,
            payload=payload,
            state_class=state_class,
            bound_claim=False,
        )

    def reconcile_effect(
        self,
        effect_id: str,
        *,
        source: str,
        state_class: str,
        evidence_root: str | Path,
    ) -> EffectFact:
        payload = self._seal_payload(evidence_root, "effect_reconciliation")
        return self._append(
            effect_id,
            "reconciled",
            source=source,
            payload=payload,
            state_class=state_class,
            bound_claim=False,
        )

    def effect_state(self, effect_id: str) -> EffectState:
        return self.facts.effect_state(effect_id, expected_command_id=self.command.command_id)

    def effect_settlement(self, effect_id: str) -> EffectSettlement:
        return self.facts.effect_settlement(
            effect_id,
            expected_command_id=self.command.command_id,
        )

    def effect_settlement_for_key(self, effect_key: str) -> EffectSettlement | None:
        return self.facts.effect_settlement_for_key(self.command.command_id, effect_key)

    def _append(
        self,
        effect_id: str,
        fact_type: str,
        *,
        source: str,
        payload: ArtifactRef | None,
        state_class: str | None = None,
        bound_claim: bool = True,
    ) -> EffectFact:
        return self.facts.append_effect_fact(
            effect_id=effect_id,
            claim=self.claim if bound_claim else None,
            fact_type=fact_type,
            source=source,
            state_class=state_class,
            payload_artifact=payload,
            payload_digest=(
                payload.set_sha256
                if payload is not None
                else digest(
                    {
                        "effect_id": effect_id,
                        "fact_type": fact_type,
                        "source": source,
                    }
                )
            ),
            observed_at=self._now(),
            fact_id="fact-" + str(uuid.uuid4()),
            expected_command_id=self.command.command_id,
        )

    def _seal_payload(self, path: str | Path, kind: str) -> ArtifactRef:
        supplied = Path(path)
        if supplied.is_symlink() or supplied.is_junction():
            raise ProtocolError("effect.payload_invalid", {"reason": "symlink"})
        origin = supplied.resolve()
        if not origin.is_relative_to(self.workspace_root):
            raise ProtocolError("effect.payload_invalid", {"reason": "outside-workspace"})
        return self.artifacts.seal_tree(
            origin,
            kind=kind,
            producer_run_id=self.command.run_id,
            producer_stage=self.command.stage,
            producer_tool_sha256=self.claim.executor_sha256,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ProtocolError("clock.invalid", {"where": "effect_port"})
        return value

    def _plain(self, value):
        if isinstance(value, Mapping):
            return {key: self._plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._plain(item) for item in value]
        return value
