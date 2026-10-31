"""The single stage engine shared by production and isolated replay."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..io import canonical
from ..schemas import UNHANDLED_STAGE_FAILURE_SCHEMA
from .contracts import (
    CancellationRequest,
    ExecutionClaim,
    FailureEnvelope,
    ProtocolError,
    StageCandidate,
    StageCancellationRequested,
    StageCommand,
    StageDecision,
    StageAttemptResult,
    StageDeferral,
    StageEffectWait,
    StageExecution,
    StagePending,
    StageResult,
    StageFailure,
    effect_is_settled,
)
from .effects import (
    EffectResumePhase,
    EffectState,
    StageEffectPort,
    TransferResumeEffectPort,
)
from .reconciliation import EffectReconciliationResult


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

    def cancellation_for(self, command: StageCommand) -> StageCommand | None: ...

    def load_result(self, decision: StageDecision) -> StageResult: ...

    def verify_decision(self, decision: StageDecision, result: StageResult) -> None: ...

    def acquire_claim(self, command: StageCommand) -> ExecutionClaim: ...

    def acquire_transfer_resume_claim(
        self,
        command: StageCommand,
        effect_id: str,
    ) -> ExecutionClaim: ...

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

    def pending_effect_for_command(self, command_id: str) -> StagePending | None: ...

    def verify_pending_effect(
        self,
        command: StageCommand,
        pending: StagePending,
    ) -> StagePending: ...

    def yield_claim_for_effect(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        effect_id: str,
    ) -> StagePending: ...

    def effect_port(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
    ) -> StageEffectPort: ...

    def transfer_effect_port(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
    ) -> TransferResumeEffectPort: ...

    def transfer_resume_gate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
    ) -> AbstractContextManager[None]: ...

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
    ) -> StageExecution | StageDeferral: ...

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
        *,
        effects: StageEffectPort,
    ) -> StageCandidate: ...


class ResumeStageHandler(Protocol):
    """Resume only from an already terminal canonical effect."""

    def resume_stage(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: StageEffectPort,
        resume: StageEffectWait,
    ) -> StageExecution | StageDeferral: ...


class TransferResumeHandler(Protocol):
    """Continue one existing native input transfer; never accept stage success."""

    def resume_transfer(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: TransferResumeEffectPort,
        resume: StageEffectWait,
    ) -> StageDeferral: ...


class EffectResumeReconciler(Protocol):
    """Read-only external-effect observer supplied by the production application."""

    def reconcile_effect(
        self,
        *,
        command_id: str,
        effect_key: str,
    ) -> EffectReconciliationResult: ...


def _validate_result_identity(command: StageCommand, result: StageResult) -> None:
    if (
        result.command_id != command.command_id
        or result.run_id != command.run_id
        or result.stage != command.stage
        or result.request_digest != command.request_digest
        or result.actual_executor_sha256 != command.expected_executor_sha256
        or result.diagnostic_scope != command.diagnostic_scope
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


def _validate_confirmed_decision(
    services: StageServices,
    command: StageCommand,
    decision: StageDecision,
) -> None:
    if (
        decision.command_id != command.command_id
        or decision.run_id != command.run_id
        or decision.stage != command.stage
    ):
        raise ProtocolError("decision.command_mismatch", {"command_id": command.command_id})
    if decision.failure_envelope is None:
        return
    current = services.effect_states_for_command(command.command_id)
    declared = dict(decision.failure_envelope.effect_states)
    actual = {effect_id: state.value for effect_id, state in current.items()}
    if declared != actual:
        raise ProtocolError(
            "decision.effect_changed",
            {"command_id": command.command_id, "declared": declared, "actual": actual},
        )
    if any(state in {EffectState.UNKNOWN, EffectState.CONFLICT} for state in current.values()):
        raise ProtocolError(
            "decision.effect_unresolved",
            {
                "command_id": command.command_id,
                "effect_states": {effect_id: state.value for effect_id, state in sorted(current.items())},
            },
        )


def _confirmed_decision_record(
    services: StageServices,
    command: StageCommand,
    decision: StageDecision,
) -> StageResult | FailureEnvelope:
    _validate_confirmed_decision(services, command, decision)
    if decision.failure_envelope is not None:
        return decision.failure_envelope
    result = services.load_result(decision)
    _validate_result_identity(command, result)
    _validate_effect_settlement(services, command, result)
    services.verify_decision(decision, result)
    return result


def _execute_requested_cancellation(
    command: StageCommand,
    cancel_command: StageCommand,
    services: StageServices,
) -> FailureEnvelope:
    request = CancellationRequest.from_command(cancel_command)
    if request.target_command_id != command.command_id:
        raise ProtocolError("cancel.target_mismatch", {"command_id": command.command_id})
    before = services.effect_states_for_command(command.command_id)
    if any(not effect_is_settled(state) for state in before.values()):
        raise ProtocolError(
            "cancel.effect_unresolved",
            {
                "command_id": command.command_id,
                "effect_states": {effect_id: state.value for effect_id, state in sorted(before.items())},
            },
        )
    claim = services.acquire_claim(command)
    if claim.command_id != command.command_id:
        raise ProtocolError("claim.command_mismatch", {"command_id": command.command_id})
    candidate = services.candidate_for(command, claim)
    workspace = services.materialize(command, claim, candidate=candidate)
    after = services.effect_states_for_command(command.command_id)
    if after != before:
        raise ProtocolError(
            "cancel.effect_changed",
            {
                "command_id": command.command_id,
                "before": {effect_id: state.value for effect_id, state in sorted(before.items())},
                "after": {effect_id: state.value for effect_id, state in sorted(after.items())},
            },
        )
    return _commit_owned_cancellation(command, cancel_command, claim, workspace, candidate, services)


def _commit_owned_cancellation(
    command: StageCommand,
    cancel_command: StageCommand,
    claim: ExecutionClaim,
    workspace: StageWorkspace,
    candidate: StageCandidate | None,
    services: StageServices,
    original_failure: StageFailure | None = None,
) -> FailureEnvelope:
    """Settle a finished attempt with its existing fence and preserve its evidence."""
    request = CancellationRequest.from_command(cancel_command)
    if request.target_command_id != command.command_id:
        raise ProtocolError("cancel.target_mismatch", {"command_id": command.command_id})
    evidence = list(original_failure.evidence_artifacts if original_failure is not None else ())
    detail = {
        "cancel_command_id": cancel_command.command_id,
        "requested_by": request.requested_by,
        "target_command_id": command.command_id,
    }
    if candidate is not None:
        evidence.append(candidate.result.output_artifact)
        if candidate.result_record_artifact is not None:
            evidence.append(candidate.result_record_artifact)
        detail["candidate_result_digest"] = candidate.result.digest
        detail["candidate_outcome"] = candidate.result.outcome
    if original_failure is not None:
        detail["original_failure"] = {
            "reason_code": original_failure.reason_code,
            "error_type": original_failure.error_type,
            "message": str(original_failure),
            "detail": original_failure.detail,
        }
    failure = StageFailure(
        request.reason_code,
        request.reason,
        outcome="cancelled",
        error_type="StageCancelled",
        detail=detail,
        evidence_artifacts=tuple({item.artifact_id: item for item in evidence}.values()),
        retained_locations=original_failure.retained_locations if original_failure is not None else (),
        unavailable_reason=original_failure.unavailable_reason if original_failure is not None else None,
    )
    return services.commit_failure(
        command,
        claim,
        failure,
        workspace_root=workspace.root,
    )


def execute_cancellation(
    *,
    command_id: str,
    services: StageServices,
) -> StageResult | FailureEnvelope:
    """Return the existing decision or form one cancelled result for the original executor."""

    command = services.load_command(command_id)
    if command.kind != "stage" or command.stage is None:
        raise ProtocolError("cancel.target_not_stage", {"command_id": command_id})
    confirmed = services.decision_for(command)
    if confirmed is not None:
        return _confirmed_decision_record(services, command, confirmed)
    cancel_command = services.cancellation_for(command)
    if cancel_command is None:
        raise ProtocolError("cancel.not_requested", {"command_id": command_id})
    return _execute_requested_cancellation(command, cancel_command, services)


def execute_stage(
    *,
    command_id: str,
    handler: StageHandler | ResumeStageHandler | TransferResumeHandler,
    services: StageServices,
    resume: StageEffectWait | None = None,
    resume_reconciler: EffectResumeReconciler | None = None,
) -> StageAttemptResult:
    """Execute one stage, resume a yielded effect, or return the canonical pending wait.

    Unhandled domain or infrastructure exceptions intentionally propagate. A
    process failure is not evidence of a definite stage failure and must remain
    associated with the open claim for reconciliation. A yielded effect closes
    its old fence atomically and is never another external-send authorization.
    """

    command = services.load_command(command_id)
    if command.command_id != command_id:
        raise ProtocolError("command.identity_mismatch", {"command_id": command_id})
    if command.kind != "stage" or command.stage is None:
        raise ProtocolError("command.not_stage", {"command_id": command_id})

    confirmed = services.decision_for(command)
    if confirmed is not None:
        return _confirmed_decision_record(services, command, confirmed)

    recovered_from_history = False
    transfer_resume = False
    if resume is None:
        recovered = services.pending_effect_for_command(command.command_id)
        if recovered is not None:
            states = services.effect_states_for_command(command.command_id)
            state = states.get(recovered.wait.effect_id)
            if state is EffectState.UNKNOWN:
                return recovered
            if state is EffectState.CONFLICT:
                raise ProtocolError(
                    "resume.effect_conflict",
                    {"command_id": command.command_id, "effect_id": recovered.wait.effect_id},
                )
            if state not in {EffectState.SATISFIED, EffectState.DEFINITELY_FAILED}:
                raise ProtocolError(
                    "resume.effect_state_invalid",
                    {
                        "command_id": command.command_id,
                        "effect_id": recovered.wait.effect_id,
                        "state": None if state is None else state.value,
                    },
                )
            resume = recovered.wait
            recovered_from_history = True

    if resume is not None:
        if resume.command_id != command.command_id:
            raise ProtocolError("resume.command_mismatch", {"command_id": command.command_id})
        pending = services.verify_pending_effect(
            command,
            StagePending(resume),
        )
        if not recovered_from_history:
            if resume_reconciler is None:
                raise ProtocolError("resume.reconciler_missing", {"command_id": command.command_id})
            reconciled = resume_reconciler.reconcile_effect(
                command_id=command.command_id,
                effect_key=resume.effect_key,
            )
            if reconciled.command_id != command.command_id or reconciled.effect_id != resume.effect_id:
                raise ProtocolError(
                    "resume.reconciliation_mismatch",
                    {"command_id": command.command_id, "effect_id": resume.effect_id},
                )
            if reconciled.resume_phase != reconciled.settlement.resume_phase:
                raise ProtocolError(
                    "resume.phase_mismatch",
                    {"command_id": command.command_id, "effect_id": resume.effect_id},
                )
            state = reconciled.settlement.state
            if (
                state is EffectState.UNKNOWN
                and reconciled.resume_phase is not EffectResumePhase.INPUT_TRANSFER_PENDING
            ):
                return pending
            if state is EffectState.CONFLICT:
                raise ProtocolError(
                    "resume.effect_conflict",
                    {"command_id": command.command_id, "effect_id": resume.effect_id},
                )
            transfer_resume = (
                state is EffectState.UNKNOWN
                and reconciled.resume_phase is EffectResumePhase.INPUT_TRANSFER_PENDING
            )

        if transfer_resume:
            if not callable(getattr(handler, "resume_transfer", None)):
                raise ProtocolError(
                    "resume.transfer_handler_missing",
                    {"command_id": command.command_id},
                )
        elif resume is not None and not callable(getattr(handler, "resume_stage", None)):
            raise ProtocolError(
                "resume.stage_handler_missing",
                {"command_id": command.command_id},
            )

    cancel_command = services.cancellation_for(command)
    if cancel_command is not None:
        return _execute_requested_cancellation(command, cancel_command, services)

    claim = (
        services.acquire_transfer_resume_claim(command, resume.effect_id)
        if transfer_resume and resume is not None
        else services.acquire_claim(command)
    )
    if claim.command_id != command.command_id:
        raise ProtocolError("claim.command_mismatch", {"command_id": command.command_id})

    candidate = None if transfer_resume else services.candidate_for(command, claim)
    workspace = services.materialize(command, claim, candidate=candidate)
    cleanup_workspace = False
    original_failure = None
    try:
        try:
            if transfer_resume:
                if resume is None:  # pragma: no cover - set only with resume
                    raise ProtocolError("resume.wait_missing", {"command_id": command.command_id})
                with services.transfer_resume_gate(command, claim):
                    transfer_effects = services.transfer_effect_port(command, claim, workspace)
                    execution = handler.resume_transfer(  # type: ignore[union-attr]
                        command,
                        claim,
                        workspace,
                        effects=transfer_effects,
                        resume=resume,
                    )
                    if not isinstance(execution, StageDeferral):
                        raise ProtocolError(
                            "resume.transfer_result_invalid",
                            {"command_id": command.command_id},
                        )
                    pending = services.yield_claim_for_effect(
                        command,
                        claim,
                        execution.effect_id,
                    )
                cleanup_workspace = True
                return pending

            effects = services.effect_port(command, claim, workspace)
            if candidate is None:
                if resume is None:
                    execution = handler.execute(command, claim, workspace, effects=effects)
                else:
                    execution = handler.resume_stage(  # type: ignore[union-attr]
                        command,
                        claim,
                        workspace,
                        effects=effects,
                        resume=resume,
                    )
                if isinstance(execution, StageDeferral):
                    pending = services.yield_claim_for_effect(
                        command,
                        claim,
                        execution.effect_id,
                    )
                    cleanup_workspace = True
                    return pending
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
                    original_failure = failure
                    try:
                        return services.commit_failure(
                            command,
                            claim,
                            failure,
                            workspace_root=workspace.root,
                        )
                    except StageCancellationRequested:
                        raise
                    except Exception as envelope_error:
                        raise failure from envelope_error
            candidate = handler.validate_candidate(command, claim, candidate, effects=effects)
            candidate = services.verify_candidate(command, claim, candidate)
        except StageFailure as failure:
            original_failure = failure
            try:
                return services.commit_failure(
                    command,
                    claim,
                    failure,
                    workspace_root=workspace.root,
                )
            except StageCancellationRequested:
                raise
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
    except StageCancellationRequested as cancellation:
        return _commit_owned_cancellation(
            command, cancellation.cancel_command, claim, workspace, candidate, services, original_failure,
        )
    except Exception as error:
        failure_record = {
            "schema_version": UNHANDLED_STAGE_FAILURE_SCHEMA,
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
        if command.diagnostic_scope is not None:
            failure_record["diagnostic_scope"] = command.diagnostic_scope.to_record()
        with (
            contextlib.suppress(Exception),
            (workspace.root / "engine-unhandled-failure.json").open("xb") as stream,
        ):
            stream.write(canonical(failure_record) + b"\n")
        raise
    finally:
        if cleanup_workspace:
            services.discard_workspace(workspace)
