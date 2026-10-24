"""The single stage engine shared by production and isolated replay."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .contracts import (
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageExecution,
    StageResult,
    effect_is_settled,
)
from .effects import EffectState


@dataclass(frozen=True, slots=True)
class StageWorkspace:
    """One claim-owned materialization of registered immutable inputs."""

    root: Path
    inputs: Mapping[str, Path]

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise ProtocolError("workspace.invalid", {"where": "root"})
        if not isinstance(self.inputs, Mapping):
            raise ProtocolError("workspace.invalid", {"where": "inputs"})
        if any(
            not isinstance(key, str) or not key or not isinstance(value, Path)
            for key, value in self.inputs.items()
        ):
            raise ProtocolError("workspace.invalid", {"where": "inputs"})


class StageServices(Protocol):
    """Persistence, artifact and effect ports used by the stage engine."""

    def load_command(self, command_id: str) -> StageCommand: ...

    def decision_for(self, command: StageCommand) -> StageDecision | None: ...

    def load_result(self, decision: StageDecision) -> StageResult: ...

    def verify_decision(self, decision: StageDecision, result: StageResult) -> None: ...

    def acquire_claim(self, command: StageCommand) -> ExecutionClaim: ...

    def candidate_for(self, command: StageCommand, claim: ExecutionClaim) -> StageCandidate | None: ...

    def materialize(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        *,
        candidate: StageCandidate | None,
    ) -> StageWorkspace: ...

    def seal_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        execution: StageExecution,
    ) -> StageCandidate: ...

    def verify_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
    ) -> StageCandidate: ...

    def effect_state(self, effect_id: str) -> EffectState: ...

    def commit_decision(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
    ) -> StageDecision: ...


class StageHandler(Protocol):
    """One stage's domain implementation; it has no platform write port."""

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
    ) -> StageExecution: ...

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
    ) -> StageCandidate: ...


def _validate_result_identity(command: StageCommand, result: StageResult) -> None:
    if (
        result.command_id != command.command_id
        or result.run_id != command.run_id
        or result.stage != command.stage
        or result.request_digest != command.request_digest
    ):
        raise ProtocolError("result.command_mismatch", {"command_id": command.command_id})


def _validate_effect_settlement(services: StageServices, result: StageResult) -> None:
    states = tuple(services.effect_state(effect_id) for effect_id in result.required_effect_ids)
    if any(not effect_is_settled(state) for state in states):
        raise ProtocolError(
            "effect.unsettled",
            {
                "command_id": result.command_id,
                "effect_states": [state.value for state in states],
            },
        )
    if result.outcome == "completed" and any(state is not EffectState.SATISFIED for state in states):
        raise ProtocolError(
            "effect.not_satisfied",
            {
                "command_id": result.command_id,
                "effect_states": [state.value for state in states],
            },
        )


def execute_stage(
    *,
    command_id: str,
    handler: StageHandler,
    services: StageServices,
) -> StageResult:
    """Execute one stage to a unique decision or return its existing decision.

    Unhandled domain or infrastructure exceptions intentionally propagate. A
    process failure is not evidence of a definite stage failure and must remain
    associated with the open claim for reconciliation.
    """

    command = services.load_command(command_id)
    if command.command_id != command_id:
        raise ProtocolError("command.identity_mismatch", {"command_id": command_id})
    if command.kind != "stage" or command.stage is None:
        raise ProtocolError("command.not_stage", {"command_id": command_id})

    confirmed = services.decision_for(command)
    if confirmed is not None:
        result = services.load_result(confirmed)
        _validate_result_identity(command, result)
        services.verify_decision(confirmed, result)
        return result

    claim = services.acquire_claim(command)
    if claim.command_id != command.command_id:
        raise ProtocolError("claim.command_mismatch", {"command_id": command.command_id})

    candidate = services.candidate_for(command, claim)
    workspace = services.materialize(command, claim, candidate=candidate)
    if candidate is None:
        execution = handler.execute(command, claim, workspace)
        candidate = services.seal_candidate(command, claim, execution)
    else:
        candidate = handler.validate_candidate(command, claim, candidate)
        candidate = services.verify_candidate(command, claim, candidate)

    _validate_result_identity(command, candidate.result)
    _validate_effect_settlement(services, candidate.result)
    decision = services.commit_decision(command, claim, candidate)
    if (
        decision.command_id != command.command_id
        or decision.run_id != command.run_id
        or decision.stage != command.stage
        or decision.result_digest != candidate.result.digest
        or decision.outcome != candidate.result.outcome
    ):
        raise ProtocolError("decision.command_mismatch", {"command_id": command.command_id})
    return candidate.result
