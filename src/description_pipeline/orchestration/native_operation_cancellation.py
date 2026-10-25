"""Resolve and cancel one exact native operation from its sealed effect."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .native_operation_client import (
    SealedNativeOperationRequest,
    NativeOperationClient,
    NativeOperationClientConflict,
    NativeOperationProtocolError,
    read_sealed_native_operation_request,
)
from .native_operation_runtime import NativeOperationView

if TYPE_CHECKING:
    from ..artifacts import ArtifactStore
    from ..pipeline.contracts import CancellationRequest, StageCommand
    from ..pipeline.effects import EffectIntent
    from ..platform.facts import FactStore

NATIVE_OPERATION_EFFECT_KIND = "native_operation"


@dataclass(frozen=True, slots=True)
class NativeOperationCancellationAdapter:
    """Read the sealed target identity and POST its exact cancellation."""

    effect_kind = NATIVE_OPERATION_EFFECT_KIND

    client: NativeOperationClient
    facts: FactStore
    artifacts: ArtifactStore

    def __post_init__(self) -> None:
        if not isinstance(self.client, NativeOperationClient):
            raise TypeError("client must be a NativeOperationClient")
        if not callable(getattr(self.facts, "effect_settlement", None)):
            raise TypeError("facts must provide effect_settlement")
        if not callable(getattr(self.artifacts, "materialize", None)):
            raise TypeError("artifacts must provide materialize")
        if not callable(getattr(self.artifacts, "verify", None)):
            raise TypeError("artifacts must provide verify")

    def send(
        self,
        command: StageCommand,
        target_command: StageCommand,
        target_effect: EffectIntent,
        request: CancellationRequest,
    ) -> Mapping[str, Any]:
        if command.kind != "cancel" or command.stage is not None:
            raise NativeOperationProtocolError(
                "native cancellation requires a cancel command"
            )
        if (
            request.target_command_id != target_command.command_id
            or request.target_run_id != target_command.run_id
            or request.target_stage != target_command.stage
            or request.target_request_digest != target_command.request_digest
            or target_effect.command_id != target_command.command_id
            or target_effect.effect_kind != NATIVE_OPERATION_EFFECT_KIND
        ):
            raise NativeOperationProtocolError(
                "native cancellation does not bind the exact target"
            )
        settlement = self.facts.effect_settlement(
            target_effect.effect_id,
            expected_command_id=target_command.command_id,
        )
        outbound_artifact = settlement.outbound_artifact
        if outbound_artifact is None:
            raise NativeOperationProtocolError(
                "native cancellation has no sealed original outbound"
            )
        if (
            outbound_artifact.set_sha256 != target_effect.outbound_digest
            or outbound_artifact.artifact_id
            != target_effect.outbound_artifact_id
        ):
            raise NativeOperationClientConflict(
                "native cancellation outbound differs from the effect identity",
                status=409,
                code="native_operation_conflict",
            )
        self.artifacts.verify(outbound_artifact)
        with tempfile.TemporaryDirectory(
            prefix="native-cancel-outbound-"
        ) as directory:
            outbound_root = self.artifacts.materialize(
                outbound_artifact,
                Path(directory) / "outbound",
            )
            identity = read_sealed_native_operation_request(
                target_command,
                target_effect,
                outbound_root,
            )

        declared_operation_id = target_effect.target.get("operation_id")
        if (
            declared_operation_id is not None
            and declared_operation_id != identity.operation_id
        ):
            raise NativeOperationClientConflict(
                "native cancellation derives another operation identity",
                status=409,
                code="native_operation_conflict",
            )

        view = self.client.cancel_operation(identity.operation_id)
        self._validate_view(view, identity)
        return {
            "accepted": True,
            "cancel_command_id": command.command_id,
            "target_effect_id": target_effect.effect_id,
            "operation_id": identity.operation_id,
            "operation_payload_sha256": view.payload_sha256,
            "operation_request_sha256": view.request_sha256,
            "original_outbound_sha256": outbound_artifact.set_sha256,
            "operation": view.to_record(),
        }

    @staticmethod
    def _validate_view(
        view: NativeOperationView,
        identity: SealedNativeOperationRequest,
    ) -> None:
        if (
            view.operation_id != identity.operation_id
            or view.kind != identity.kind
            or view.request_sha256 != identity.request_sha256
            or view.native_tool_requirement_sha256
            != identity.native_tool_requirement_sha256
            or (
                identity.payload_sha256 is not None
                and view.payload_sha256 != identity.payload_sha256
            )
        ):
            raise NativeOperationClientConflict(
                "native cancellation returned another operation",
                status=409,
                code="native_operation_conflict",
            )

def native_operation_cancellation_adapter(
    client: NativeOperationClient,
    *,
    facts: FactStore,
    artifacts: ArtifactStore,
) -> NativeOperationCancellationAdapter:
    """Construct the one credential-bearing native cancellation transport."""

    return NativeOperationCancellationAdapter(
        client=client,
        facts=facts,
        artifacts=artifacts,
    )


__all__ = [
    "NATIVE_OPERATION_EFFECT_KIND",
    "NativeOperationCancellationAdapter",
    "native_operation_cancellation_adapter",
]
