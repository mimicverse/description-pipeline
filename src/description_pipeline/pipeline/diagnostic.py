"""Canonical single-stage diagnostic admission without fabricated predecessors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..io import digest
from ..schemas import DIAGNOSTIC_ADMISSION_SCHEMA
from .contracts import (
    ArtifactRef,
    DiagnosticReplayScope,
    ProtocolError,
    RunAdmission,
    StageCommand,
    StageId,
    _freeze_mapping,
    _json_value,
)

_HEX64 = frozenset("0123456789abcdef")


def _digest(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX64 for character in value)
    ):
        raise ProtocolError("diagnostic.invalid", {"where": where})
    return value


@dataclass(frozen=True, slots=True)
class DiagnosticAdmissionSpec:
    """The one authority for a private single-stage replay run."""

    schema_version: str
    run_id: str
    request_id: str
    target_stage: StageId
    request: Mapping[str, Any]
    input_artifacts: tuple[ArtifactRef, ...]
    expected_executor_sha256: str
    config_snapshot_sha256: str
    contract_sha256: str
    policy_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != DIAGNOSTIC_ADMISSION_SCHEMA:
            raise ProtocolError("diagnostic.schema_unsupported", {"value": self.schema_version})
        for field in ("run_id", "request_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value:
                raise ProtocolError("diagnostic.invalid", {"where": field})
        if self.target_stage not in {"freeze", "discover", "capture", "generate", "verify", "publish"}:
            raise ProtocolError("diagnostic.invalid", {"where": "target_stage"})
        object.__setattr__(
            self,
            "request",
            _freeze_mapping(self.request, "diagnostic.invalid", "request"),
        )
        if (
            not isinstance(self.input_artifacts, tuple)
            or not self.input_artifacts
            or not all(isinstance(item, ArtifactRef) for item in self.input_artifacts)
        ):
            raise ProtocolError("diagnostic.invalid", {"where": "input_artifacts"})
        if len({item.artifact_id for item in self.input_artifacts}) != len(self.input_artifacts):
            raise ProtocolError("diagnostic.invalid", {"where": "input_artifacts", "reason": "duplicate"})
        for field in (
            "expected_executor_sha256",
            "config_snapshot_sha256",
            "contract_sha256",
            "policy_sha256",
        ):
            _digest(getattr(self, field), field)
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ProtocolError("diagnostic.invalid", {"where": "created_at"})

    def identity_record(self) -> dict[str, Any]:
        return {
            "config_snapshot_sha256": self.config_snapshot_sha256,
            "contract_sha256": self.contract_sha256,
            "created_at": self.created_at.isoformat(),
            "expected_executor_sha256": self.expected_executor_sha256,
            "input_artifacts": [item.to_record() for item in self.input_artifacts],
            "policy_sha256": self.policy_sha256,
            "request": _json_value(self.request),
            "request_id": self.request_id,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "target_stage": self.target_stage,
        }

    @property
    def admission_sha256(self) -> str:
        return digest(self.identity_record())

    def to_record(self) -> dict[str, Any]:
        return {**self.identity_record(), "admission_sha256": self.admission_sha256}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> DiagnosticAdmissionSpec:
        expected = {
            "admission_sha256",
            "config_snapshot_sha256",
            "contract_sha256",
            "created_at",
            "expected_executor_sha256",
            "input_artifacts",
            "policy_sha256",
            "request",
            "request_id",
            "run_id",
            "schema_version",
            "target_stage",
        }
        if not isinstance(record, Mapping) or set(record) != expected:
            raise ProtocolError("diagnostic.invalid", {"where": "record"})
        raw_artifacts = record["input_artifacts"]
        if not isinstance(raw_artifacts, (list, tuple)):
            raise ProtocolError("diagnostic.invalid", {"where": "input_artifacts"})
        value = cls(
            schema_version=record["schema_version"],
            run_id=record["run_id"],
            request_id=record["request_id"],
            target_stage=record["target_stage"],
            request=record["request"],
            input_artifacts=tuple(ArtifactRef.from_record(item) for item in raw_artifacts),
            expected_executor_sha256=record["expected_executor_sha256"],
            config_snapshot_sha256=record["config_snapshot_sha256"],
            contract_sha256=record["contract_sha256"],
            policy_sha256=record["policy_sha256"],
            created_at=datetime.fromisoformat(record["created_at"].replace("Z", "+00:00")),
        )
        if value.admission_sha256 != record["admission_sha256"]:
            raise ProtocolError("diagnostic.digest_mismatch", {"run_id": value.run_id})
        return value


def build_diagnostic_run_admission(spec: DiagnosticAdmissionSpec) -> RunAdmission:
    if not isinstance(spec, DiagnosticAdmissionSpec):
        raise ProtocolError("diagnostic.admission_required")
    return RunAdmission.create(
        run_id=spec.run_id,
        request_id=spec.request_id,
        parent_run_id=None,
        from_stage=spec.target_stage,
        request=spec.to_record(),
        created_at=spec.created_at,
    )


def build_diagnostic_command(spec: DiagnosticAdmissionSpec) -> StageCommand:
    if not isinstance(spec, DiagnosticAdmissionSpec):
        raise ProtocolError("diagnostic.admission_required")
    scope = DiagnosticReplayScope.from_inputs(spec.target_stage, spec.input_artifacts)
    command_identity = digest(
        {
            "admission_sha256": spec.admission_sha256,
            "input_closure_sha256": scope.input_closure_sha256,
            "target_stage": spec.target_stage,
        }
    )
    return StageCommand.create(
        command_id=f"diagnostic:{command_identity}",
        run_id=spec.run_id,
        stage=spec.target_stage,
        idempotency_key=f"{spec.run_id}/diagnostic/{spec.target_stage}",
        request=_json_value(spec.request),
        input_artifacts=spec.input_artifacts,
        config_snapshot_sha256=spec.config_snapshot_sha256,
        contract_sha256=spec.contract_sha256,
        policy_sha256=spec.policy_sha256,
        expected_executor_sha256=spec.expected_executor_sha256,
        predecessor_decision_id=None,
        diagnostic_scope=scope,
    )


def validate_diagnostic_command(
    spec: DiagnosticAdmissionSpec,
    admission: RunAdmission,
    command: StageCommand,
) -> StageCommand:
    if admission != build_diagnostic_run_admission(spec):
        raise ProtocolError("diagnostic.admission_mismatch", {"run_id": spec.run_id})
    expected = build_diagnostic_command(spec)
    if command != expected:
        raise ProtocolError("diagnostic.command_mismatch", {"command_id": command.command_id})
    if command.kind != "stage" or command.predecessor_decision_id is not None:
        raise ProtocolError("diagnostic.command_invalid", {"command_id": command.command_id})
    if command.diagnostic_scope is None:
        raise ProtocolError("diagnostic.scope_required", {"command_id": command.command_id})
    command.diagnostic_scope.validate_for(command.stage, command.input_artifacts)
    return command


__all__ = [
    "DIAGNOSTIC_ADMISSION_SCHEMA",
    "DiagnosticAdmissionSpec",
    "build_diagnostic_command",
    "build_diagnostic_run_admission",
    "validate_diagnostic_command",
]
