"""Technical cancellation transport for an already sent operation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..io import write_json
from ..schemas import OPERATION_CANCEL_EFFECT_SCHEMA
from .contracts import CancellationRequest, ProtocolError, StageCommand
from .effects import EffectIntent, EffectState
from .services import CanonicalStageServices, ClaimPolicy

if TYPE_CHECKING:
    from ..artifacts import ArtifactStore
    from ..platform.facts import FactStore


class CancellationAdapter(Protocol):
    """Credential-owning transport for one exact native cancellation request."""

    def send(
        self,
        command: StageCommand,
        target_command: StageCommand,
        target_effect: EffectIntent,
        request: CancellationRequest,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CancellationControlResult:
    cancel_command_id: str
    target_command_id: str
    target_effect_id: str
    cancel_effect_id: str | None
    target_effect_state: EffectState
    cancel_effect_state: EffectState | None
    sent: bool
    retained_workspace: str | None


class CancellationControlService:
    """Send the cancellation command's exact effect without deciding the target stage."""

    def __init__(
        self,
        *,
        facts: FactStore,
        artifacts: ArtifactStore,
        work_root: str | Path,
        claim_policy: ClaimPolicy,
        clock: Callable[[], datetime],
        adapter: CancellationAdapter,
    ) -> None:
        self.facts = facts
        self.artifacts = artifacts
        self.services = CanonicalStageServices(
            facts=facts,
            artifacts=artifacts,
            work_root=work_root,
            claim_policy=claim_policy,
            clock=clock,
        )
        self.clock = clock
        self.adapter = adapter

    def send(self, *, cancel_command_id: str, target_effect_id: str) -> CancellationControlResult:
        cancel_command = self.facts.load_command(cancel_command_id)
        request = CancellationRequest.from_command(cancel_command)
        target_command = self.facts.load_command(request.target_command_id)
        if target_command.kind != "stage" or target_command.stage != request.target_stage:
            raise ProtocolError("cancel.target_mismatch", {"command_id": target_command.command_id})
        target_effect = self.facts.effect_intent(
            target_effect_id,
            expected_command_id=target_command.command_id,
        )
        if target_effect.effect_kind != "native_operation":
            raise ProtocolError(
                "cancel.effect_unsupported",
                {"effect_kind": target_effect.effect_kind},
            )
        target_state = self.facts.effect_state(
            target_effect.effect_id,
            expected_command_id=target_command.command_id,
        )
        effect_key = f"cancel:{target_effect.effect_id}"
        existing = self.facts.effect_settlement_for_key(cancel_command.command_id, effect_key)
        if target_state is not EffectState.UNKNOWN:
            return CancellationControlResult(
                cancel_command_id=cancel_command.command_id,
                target_command_id=target_command.command_id,
                target_effect_id=target_effect.effect_id,
                cancel_effect_id=None if existing is None else existing.effect_id,
                target_effect_state=target_state,
                cancel_effect_state=None if existing is None else existing.state,
                sent=False,
                retained_workspace=None,
            )
        if existing is not None and existing.state is not EffectState.PREPARED:
            return CancellationControlResult(
                cancel_command_id=cancel_command.command_id,
                target_command_id=target_command.command_id,
                target_effect_id=target_effect.effect_id,
                cancel_effect_id=existing.effect_id,
                target_effect_state=target_state,
                cancel_effect_state=existing.state,
                sent=False,
                retained_workspace=None,
            )

        claim = self.services.acquire_claim(cancel_command)
        workspace = self.services.materialize(cancel_command, claim, candidate=None)
        effects = self.services.effect_port(cancel_command, claim, workspace)
        outbound = workspace.root / "cancel-request"
        outbound.mkdir()
        write_json(
            outbound / "request.json",
            {
                "cancel_request": request.to_record(),
                "schema_version": OPERATION_CANCEL_EFFECT_SCHEMA,
                "target_command": target_command.to_record(),
                "target_effect": target_effect.identity_record(),
            },
        )
        expected_state = {
            "schema_version": OPERATION_CANCEL_EFFECT_SCHEMA,
            "cancel_request_accepted": True,
            "target_command_id": target_command.command_id,
            "target_effect_id": target_effect.effect_id,
            "target_effect_digest": target_effect.identity_digest,
        }
        intent = effects.prepare_effect(
            effect_key=effect_key,
            effect_kind="native_operation",
            target={
                "operation": "cancel",
                "target_command_id": target_command.command_id,
                "target_effect_id": target_effect.effect_id,
            },
            expected_state=expected_state,
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="operation-cancel")
        try:
            response = self.adapter.send(
                cancel_command,
                target_command,
                target_effect,
                request,
            )
        except Exception as error:
            response_root = workspace.root / "cancel-timeout"
            response_root.mkdir()
            write_json(
                response_root / "error.json",
                {
                    "error_message": str(error)[:4096],
                    "error_type": type(error).__name__,
                },
            )
            effects.record_timeout(
                intent.effect_id,
                source="operation-cancel",
                detail_root=response_root,
            )
            settlement = self.facts.effect_settlement(
                intent.effect_id,
                expected_command_id=cancel_command.command_id,
            )
            return CancellationControlResult(
                cancel_command_id=cancel_command.command_id,
                target_command_id=target_command.command_id,
                target_effect_id=target_effect.effect_id,
                cancel_effect_id=intent.effect_id,
                target_effect_state=target_state,
                cancel_effect_state=settlement.state,
                sent=True,
                retained_workspace=str(workspace.root),
            )
        if not isinstance(response, Mapping):
            raw_root = workspace.root / "cancel-raw-response"
            raw_root.mkdir()
            if isinstance(response, (bytes, bytearray, memoryview)):
                (raw_root / "response.bin").write_bytes(bytes(response))
            else:
                write_json(
                    raw_root / "response.json",
                    {
                        "representation": repr(response)[:1_048_576],
                        "type": type(response).__name__,
                    },
                )
            raise ProtocolError("cancel.response_invalid", {"command_id": cancel_command.command_id})
        response_root = workspace.root / "cancel-response"
        response_root.mkdir()
        try:
            write_json(response_root / "response.json", dict(response))
        except Exception:
            write_json(
                response_root / "response.repr.json",
                {
                    "representation": repr(response)[:1_048_576],
                    "type": type(response).__name__,
                },
            )
            raise
        effects.record_response(
            intent.effect_id,
            source="operation-cancel",
            response_root=response_root,
        )
        accepted = (
            response.get("accepted") is True
            and response.get("cancel_command_id") == cancel_command.command_id
            and response.get("target_effect_id") == target_effect.effect_id
        )
        effects.record_observation(
            intent.effect_id,
            source="operation-cancel",
            state_class="known" if accepted else "unobservable",
            observation_root=response_root,
        )
        settlement = self.facts.effect_settlement(
            intent.effect_id,
            expected_command_id=cancel_command.command_id,
        )
        if settlement.state is EffectState.UNKNOWN:
            return CancellationControlResult(
                cancel_command_id=cancel_command.command_id,
                target_command_id=target_command.command_id,
                target_effect_id=target_effect.effect_id,
                cancel_effect_id=intent.effect_id,
                target_effect_state=target_state,
                cancel_effect_state=settlement.state,
                sent=True,
                retained_workspace=str(workspace.root),
            )
        if settlement.state is not EffectState.SATISFIED:
            raise ProtocolError(
                "cancel.response_unresolved",
                {"command_id": cancel_command.command_id, "state": settlement.state.value},
            )
        self.facts.close_open_claim(
            claim,
            closure_code="completed",
            closed_at=self._now(),
        )
        self.services.discard_workspace(workspace)
        return CancellationControlResult(
            cancel_command_id=cancel_command.command_id,
            target_command_id=target_command.command_id,
            target_effect_id=target_effect.effect_id,
            cancel_effect_id=intent.effect_id,
            target_effect_state=target_state,
            cancel_effect_state=settlement.state,
            sent=True,
            retained_workspace=None,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ProtocolError("clock.invalid", {"where": "cancellation_control"})
        return value


__all__ = [
    "CancellationAdapter",
    "CancellationControlResult",
    "CancellationControlService",
]
