"""Production composition of the stage engine, fact store and artifact store."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..artifacts import ArtifactStore
from ..platform.facts import FactStore
from .contracts import (
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageExecution,
    StageResult,
)
from .effects import EffectState
from .engine import StageWorkspace


@dataclass(frozen=True, slots=True)
class ClaimPolicy:
    owner_sha256: str
    lease: timedelta
    lane_key: str | None = None
    dag_id: str | None = None
    dag_run_id: str | None = None
    task_id: str | None = None
    map_index: int | None = None
    claim_id_factory: Callable[[], str] = lambda: str(uuid.uuid4())

    def __post_init__(self) -> None:
        if (
            not isinstance(self.owner_sha256, str)
            or len(self.owner_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.owner_sha256)
        ):
            raise ProtocolError("claim_policy.invalid", {"where": "owner_sha256"})
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
        self.work_root = Path(work_root)
        if self.work_root.exists() and self.work_root.is_symlink():
            raise ProtocolError("workspace.invalid", {"root": str(self.work_root)})
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.claim_policy = claim_policy
        self.clock = clock

    def load_command(self, command_id: str) -> StageCommand:
        return self.facts.load_command(command_id)

    def decision_for(self, command: StageCommand) -> StageDecision | None:
        return self.facts.decision_for(command)

    def load_result(self, decision: StageDecision) -> StageResult:
        return self.facts.load_result(decision)

    def verify_decision(self, decision: StageDecision, result: StageResult) -> None:
        recorded = self.facts.load_result(decision)
        self.artifacts.verify(result.result_artifact)
        if recorded != result:
            raise ProtocolError("decision.result_mismatch", {"decision_id": decision.decision_id})

    def acquire_claim(self, command: StageCommand) -> ExecutionClaim:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ProtocolError("clock.invalid", {"where": "acquire_claim"})
        policy = self.claim_policy
        return self.facts.acquire_claim(
            command.command_id,
            claim_id=policy.claim_id_factory(),
            owner_sha256=policy.owner_sha256,
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
        command_root.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix=f"{claim.fence}-", dir=command_root))
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
                candidate.result.result_artifact,
                root / "candidate-output",
            )
            if candidate.result_record_artifact is not None:
                self.artifacts.materialize(
                    candidate.result_record_artifact,
                    root / "candidate-result-record",
                )
        return StageWorkspace(root=root, inputs=materialized)

    def seal_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        execution: StageExecution,
    ) -> StageCandidate:
        if command.stage is None:
            raise ProtocolError("candidate.stage_required", {"command_id": command.command_id})
        output = self.artifacts.seal_tree(
            execution.candidate_root,
            kind=f"stage_output:{command.stage}",
            producer_run_id=command.run_id,
            producer_stage=command.stage,
            producer_tool_sha256=claim.owner_sha256,
        )
        result = StageResult(
            schema_version="description-pipeline.stage-result/v1",
            command_id=command.command_id,
            run_id=command.run_id,
            stage=command.stage,
            request_digest=command.request_digest,
            outcome=execution.outcome,
            result_artifact=output,
            actual_executor_sha256=claim.owner_sha256,
            checks_sha256=execution.checks_sha256,
            required_effect_ids=execution.required_effect_ids,
            reason_code=execution.reason_code,
        )
        result_record = self.artifacts.seal_stage_result(result)
        candidate_id = f"candidate-{result.digest}"
        return self.facts.register_candidate(
            candidate_id=candidate_id,
            claim=claim,
            result_artifact=result_record,
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
        self.artifacts.verify(candidate.result.result_artifact)
        if candidate.result_record_artifact is None:
            raise ProtocolError("candidate.record_artifact_missing", {"command_id": command.command_id})
        self.artifacts.verify(candidate.result_record_artifact)
        if self.artifacts.read_result(candidate.result_record_artifact) != candidate.result:
            raise ProtocolError("candidate.result_mismatch", {"command_id": command.command_id})
        return candidate

    def effect_state(self, effect_id: str) -> EffectState:
        return self.facts.effect_state(effect_id)

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

    def discard_workspace(self, workspace: StageWorkspace) -> None:
        """Delete only a workspace created by this service; it is never authority."""

        root = workspace.root.resolve()
        if not root.is_relative_to(self.work_root.resolve()) or root == self.work_root.resolve():
            raise ProtocolError("workspace.escape", {"root": str(root)})
        shutil.rmtree(root, ignore_errors=True)
