"""Resolve and cancel one exact native operation from its sealed effect."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..io import digest, file_digest, read_data
from ..sources.solidworks.discover_stage import (
    NATIVE_OBSERVATION_SCHEMA,
    NativeDiscoverRequest,
)
from .discover_protocol import SOURCE_ARCHIVE
from .native_operation_client import (
    NativeOperationClient,
    NativeOperationClientConflict,
    NativeOperationClientError,
    NativeOperationProtocolError,
    native_capture_operation_id,
)
from .native_operation_runtime import (
    CAPTURE_KIND,
    CAPTURE_OPERATION_SCHEMA,
    DISCOVER_KIND,
    CaptureOperationRequest,
    NativeOperationView,
    build_operation_record,
)

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
            identity = self._operation_identity(
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
    def _operation_identity(
        command: StageCommand,
        effect: EffectIntent,
        outbound_root: Path,
    ) -> _OperationIdentity:
        if command.stage == "capture":
            from ..pipeline.capture import (
                CaptureRequest,
                validate_capture_effect_outbound,
            )

            outbound = validate_capture_effect_outbound(outbound_root, command)
            request = CaptureRequest.from_command(command)
            native_request = CaptureOperationRequest.from_record(
                {
                    "schema_version": CAPTURE_OPERATION_SCHEMA,
                    "command_id": command.command_id,
                    "run_id": command.run_id,
                    "request_digest": command.request_digest,
                    "effect_key": request.effect_key,
                    "handoff_sha256": outbound["handoff_sha256"],
                    "upload_sha256": outbound["upload_sha256"],
                    "prepared_sha256": request.prepared_sha256,
                    "main_assembly": outbound["main_assembly"],
                    "package_files": outbound["prepared_files"],
                    "upstream_artifacts": outbound["upstream_artifacts"],
                    "execution": outbound["execution"],
                    "native_tool_source_sha256": (
                        request.native_tool_source_sha256
                    ),
                    "request_evidence": outbound["request_evidence"],
                }
            )
            operation_id = native_capture_operation_id(
                run_id=command.run_id,
                command_id=command.command_id,
                effect_key=request.effect_key,
            )
            if (
                effect.target.get("run_id") != command.run_id
                or effect.target.get("stage") != command.stage
                or outbound["effect_key"] != request.effect_key
            ):
                raise NativeOperationProtocolError(
                    "Capture cancellation does not bind the sealed operation"
                )
            return _OperationIdentity(
                operation_id=operation_id,
                kind=CAPTURE_KIND,
                payload_sha256=None,
                request_sha256=digest(native_request.to_record()),
                native_tool_requirement_sha256=(
                    request.native_tool_record_sha256
                ),
            )

        if command.stage == "discover":
            request = NativeDiscoverRequest.from_record(
                read_data(outbound_root / "request.json")
            )
            source = read_data(outbound_root / "source.json")
            archive = outbound_root / SOURCE_ARCHIVE
            if (
                source
                != {
                    "source_files": dict(request.source_files),
                    "source_inventory_sha256": (
                        request.source_inventory_sha256
                    ),
                }
                or request.command_id != command.command_id
                or request.run_id != command.run_id
                or request.request_digest != command.request_digest
                or command.request.get("expected_native_tool_sha256")
                != request.expected_native_tool_sha256
                or command.request.get(
                    "expected_native_tool_record_sha256"
                )
                != request.expected_native_tool_record_sha256
                or command.request.get(
                    "native_tool_requirement_artifact_id"
                )
                != request.native_tool_requirement_artifact_id
                or not archive.is_file()
                or archive.is_symlink()
            ):
                raise NativeOperationProtocolError(
                    "Discover cancellation does not bind the sealed operation"
                )
            record = build_operation_record(
                operation_id=request.operation_id,
                kind=DISCOVER_KIND,
                request=request.to_record(),
                input_archive_sha256=file_digest(archive),
                input_archive_bytes=archive.stat().st_size,
                native_tool_requirement_sha256=(
                    request.expected_native_tool_record_sha256
                ),
                output_contract=NATIVE_OBSERVATION_SCHEMA,
            )
            return _OperationIdentity(
                operation_id=request.operation_id,
                kind=DISCOVER_KIND,
                payload_sha256=record["payload_sha256"],
                request_sha256=digest(request.to_record()),
                native_tool_requirement_sha256=(
                    request.expected_native_tool_record_sha256
                ),
            )

        raise NativeOperationClientError(
            "native cancellation supports only Discover and Capture",
            code="native_operation_cancel_unsupported",
        )

    @staticmethod
    def _validate_view(
        view: NativeOperationView,
        identity: _OperationIdentity,
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


@dataclass(frozen=True, slots=True)
class _OperationIdentity:
    operation_id: str
    kind: str
    payload_sha256: str | None
    request_sha256: str
    native_tool_requirement_sha256: str


def native_operation_cancellation_adapter(
    client: NativeOperationClient,
    *,
    facts: FactStore,
    artifacts: ArtifactStore,
) -> NativeOperationCancellationAdapter:
    """Construct the one read-only native cancellation transport."""

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
