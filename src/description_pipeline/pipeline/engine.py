"""The single stage engine shared by production and isolated replay."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..io import canonical
from .contracts import (
    ExecutionClaim,
    FailureEnvelope,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageExecution,
    StageResult,
    StageFailure,
    effect_is_settled,
)
from .effects import EffectState, StageEffectPort


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
            not isinstance(key, str) or not key or not isinstance(value, Path) for key, value in self.inputs.items()
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

    def effect_states_for_command(self, command_id: str) -> Mapping[str, EffectState]: ...

    def effect_port(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
    ) -> StageEffectPort: ...

    def commit_decision(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
    ) -> StageDecision: ...

    def commit_failure(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        failure: StageFailure,
        *,
        workspace_root: str | Path,
    ) -> FailureEnvelope: ...

    def discard_workspace(self, workspace: StageWorkspace) -> None: ...


class StageHandler(Protocol):
    """One stage's domain implementation; it has no platform write port."""

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: StageEffectPort,
    ) -> StageExecution: ...

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
        *,
        effects: StageEffectPort,
    ) -> StageCandidate: ...


def _validate_result_identity(command: StageCommand, result: StageResult) -> None:
    if (
        result.command_id != command.command_id
        or result.run_id != command.run_id
        or result.stage != command.stage
        or result.request_digest != command.request_digest
        or result.actual_executor_sha256 != command.expected_executor_sha256
    ):
        raise ProtocolError("result.command_mismatch", {"command_id": command.command_id})


def _validate_effect_settlement(services: StageServices, command: StageCommand, result: StageResult) -> None:
    ledger_states = services.effect_states_for_command(command.command_id)
    declared = set(result.required_effect_ids)
    ledger = set(ledger_states)
    if declared != ledger:
        raise ProtocolError(
            "effect.identity_mismatch",
            {
                "command_id": command.command_id,
                "declared": sorted(declared),
                "ledger": sorted(ledger),
            },
        )
    states = tuple(ledger_states[effect_id] for effect_id in sorted(ledger_states))
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
) -> StageResult | FailureEnvelope:
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
        if confirmed.failure_envelope is not None:
            return confirmed.failure_envelope
        result = services.load_result(confirmed)
        _validate_result_identity(command, result)
        services.verify_decision(confirmed, result)
        return result

    claim = services.acquire_claim(command)
    if claim.command_id != command.command_id:
        raise ProtocolError("claim.command_mismatch", {"command_id": command.command_id})

    candidate = services.candidate_for(command, claim)
    workspace = services.materialize(command, claim, candidate=candidate)
    cleanup_workspace = False
    try:
        effects = services.effect_port(command, claim, workspace)
        try:
            if candidate is None:
                execution = handler.execute(command, claim, workspace, effects=effects)
                try:
                    candidate = services.seal_candidate(command, claim, execution)
                except Exception as error:
                    if execution.outcome not in {"failed", "cancelled"}:
                        raise
                    original = execution.failure
                    failure = StageFailure(
                        (original.reason_code if original is not None else None) or "failure_envelope_required",
                        str(original) if original is not None else f"{type(error).__name__}: {error}"[:4096],
                        outcome=execution.outcome,
                        evidence_artifacts=(original.evidence_artifacts if original is not None else ()),
                        unavailable_reason=(
                            f"diagnostic_or_result_seal_failed: {type(error).__name__}: {error}"[:4096]
                        ),
                        error_type=original.error_type if original is not None else None,
                        detail=original.detail if original is not None else None,
                        retained_locations=(
                            *(original.retained_locations if original is not None else ()),
                            str(Path(execution.candidate_root).resolve()),
                        ),
                    )
                    try:
                        return services.commit_failure(
                            command,
                            claim,
                            failure,
                            workspace_root=workspace.root,
                        )
                    except Exception as envelope_error:
                        raise failure from envelope_error
            candidate = handler.validate_candidate(command, claim, candidate, effects=effects)
            candidate = services.verify_candidate(command, claim, candidate)
        except StageFailure as failure:
            try:
                return services.commit_failure(
                    command,
                    claim,
                    failure,
                    workspace_root=workspace.root,
                )
            except Exception as error:
                raise failure from error
        _validate_result_identity(command, candidate.result)
        _validate_effect_settlement(services, command, candidate.result)
        decision = services.commit_decision(command, claim, candidate)
        if (
            decision.command_id != command.command_id
            or decision.run_id != command.run_id
            or decision.stage != command.stage
            or decision.result_digest != candidate.result.digest
            or decision.outcome != candidate.result.outcome
        ):
            raise ProtocolError("decision.command_mismatch", {"command_id": command.command_id})
        cleanup_workspace = candidate.result.outcome == "completed" and candidate.result.resources_settled is True
        return candidate.result
    except Exception as error:
        with (
            contextlib.suppress(Exception),
            (workspace.root / "engine-unhandled-failure.json").open("xb") as stream,
        ):
            stream.write(
                canonical(
                    {
                        "schema_version": "description-pipeline.unhandled-stage-failure/v1",
                        "command_id": command.command_id,
                        "run_id": command.run_id,
                        "stage": command.stage,
                        "claim_id": claim.claim_id,
                        "fence": claim.fence,
                        "error_type": type(error).__name__,
                        "error_message": str(error)[:4096],
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "workspace": str(workspace.root.resolve()),
                    }
                )
                + b"\n"
            )
        raise
    finally:
        if cleanup_workspace:
            services.discard_workspace(workspace)
