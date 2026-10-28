"""Canonical types shared by the six stages.

The business outcome is a single immutable decision per stage. Execution rights,
scheduler activity and external effects are separate facts owned by dedicated
records. These types contain no filesystem or database side effects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from ..io import PipelineError, canonical, digest
from ..stages import STAGE_IDS
from ..schemas import (
    AIRFLOW_DISPATCH_CONF_SCHEMA,
    CANCEL_REQUEST_SCHEMA,
    DIAGNOSTIC_SCOPE_SCHEMA,
    DISPATCH_COMMAND_SCHEMA,
    FAILURE_ENVELOPE_SCHEMA,
    NATIVE_TARGET_IDENTITY_SCHEMA,
    RUN_ADMISSION_SCHEMA,
    STAGE_COMMAND_SCHEMA,
    STAGE_EFFECT_WAIT_SCHEMA,
    STAGE_QUALIFICATION_SCHEMA,
    STAGE_RESULT_SCHEMA,
)

StageId = Literal["freeze", "discover", "capture", "generate", "verify", "publish"]
Outcome = Literal["completed", "failed", "cancelled"]
EffectKind = Literal["airflow_dispatch", "native_operation", "git_ref_update", "pr_upsert"]
QUALIFICATION_SCHEMA = STAGE_QUALIFICATION_SCHEMA

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_LOCATOR_PREFIX = "cas://artifact/"


class ProtocolError(PipelineError):
    """A canonical pipeline fact violates the shared protocol."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True, slots=True)
class AdmissionControl:
    """The single administrative admission gate row."""

    state: Literal["open", "closed"]
    reason: str
    actor: str
    changed_at: datetime
    revision: int

    def __post_init__(self) -> None:
        if self.state not in {"open", "closed"}:
            raise ProtocolError("admission_control.invalid", {"where": "state"})
        for field in ("reason", "actor"):
            _require_text(getattr(self, field), "admission_control.invalid", field)
        if not isinstance(self.changed_at, datetime) or self.changed_at.tzinfo is None:
            raise ProtocolError("admission_control.invalid", {"where": "changed_at"})
        if type(self.revision) is not int or self.revision <= 0:
            raise ProtocolError("admission_control.invalid", {"where": "revision"})


@dataclass(frozen=True, slots=True)
class RunAdmission:
    """An immutable accepted run and its complete non-secret request."""

    schema_version: str
    run_id: str
    request_id: str
    parent_run_id: str | None
    from_stage: StageId | None
    request: Mapping[str, Any]
    request_digest: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != RUN_ADMISSION_SCHEMA:
            raise ProtocolError("admission.schema_unsupported", {"value": self.schema_version})
        _require_identifier(self.run_id, "admission.invalid", "run_id")
        _require_identifier(self.request_id, "admission.invalid", "request_id")
        if self.parent_run_id is not None:
            _require_identifier(self.parent_run_id, "admission.invalid", "parent_run_id")
            if self.parent_run_id == self.run_id:
                raise ProtocolError("admission.invalid", {"where": "parent_run_id", "reason": "self-reference"})
        if self.from_stage is not None:
            _require_stage(self.from_stage, "admission.invalid", "from_stage")
        object.__setattr__(self, "request", _freeze_mapping(self.request, "admission.invalid", "request"))
        _require_digest(self.request_digest, "admission.invalid", "request_digest")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ProtocolError("admission.invalid", {"where": "created_at"})
        if digest(self.identity_record()) != self.request_digest:
            raise ProtocolError("admission.digest_mismatch", {"run_id": self.run_id})

    def identity_record(self) -> dict[str, Any]:
        return {
            "from_stage": self.from_stage,
            "parent_run_id": self.parent_run_id,
            "request": _json_value(self.request),
            "request_id": self.request_id,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        request_id: str,
        parent_run_id: str | None,
        from_stage: StageId | None,
        request: Mapping[str, Any],
        created_at: datetime,
    ) -> RunAdmission:
        values = {
            "schema_version": RUN_ADMISSION_SCHEMA,
            "run_id": run_id,
            "request_id": request_id,
            "parent_run_id": parent_run_id,
            "from_stage": from_stage,
            "request": dict(request),
            "created_at": created_at,
        }
        request_digest = digest(
            {
                "from_stage": from_stage,
                "parent_run_id": parent_run_id,
                "request": dict(request),
                "request_id": request_id,
                "run_id": run_id,
                "schema_version": values["schema_version"],
            }
        )
        return cls(request_digest=request_digest, **values)


class StageStatus(StrEnum):
    WAITING = "waiting"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNRESOLVED = "unresolved"


class RunStatus(StrEnum):
    INCOMPLETE = "incomplete"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNRESOLVED = "unresolved"


def _require_text(value: object, code: str, where: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProtocolError(code, {"where": where})
    return value


def _require_identifier(value: object, code: str, where: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ProtocolError(code, {"where": where, "reason": "unsafe-identifier"})
    return value


def _require_digest(value: object, code: str, where: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise ProtocolError(code, {"where": where})
    return value


def _require_stage(value: object, code: str, where: str) -> StageId:
    if value not in STAGE_IDS:
        raise ProtocolError(code, {"where": where, "value": value})
    return value  # type: ignore[return-value]


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _freeze_mapping(value: Mapping[str, Any], code: str, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(code, {"where": where})
    source = dict(value)
    # Canonical encoding is both the JSON-safety check and the digest identity.
    # Existing immutable contracts contain MappingProxyType values; their JSON
    # view is valid input when a StageFailure detail is promoted to an envelope.
    try:
        canonical(_json_value(source))
    except (TypeError, ValueError) as error:
        raise ProtocolError(code, {"where": where, "reason": str(error)}) from error

    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, (list, tuple)):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(source)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """One immutable artifact closure registered by the platform."""

    artifact_id: str
    kind: str
    locator: str
    manifest_sha256: str
    set_sha256: str
    producer_run_id: str | None
    producer_stage: StageId | None
    producer_tool_sha256: str

    def __post_init__(self) -> None:
        _require_identifier(self.artifact_id, "artifact.invalid", "artifact_id")
        _require_text(self.kind, "artifact.invalid", "kind")
        if self.locator != f"{_LOCATOR_PREFIX}{self.artifact_id}":
            raise ProtocolError(
                "artifact.invalid",
                {"where": "locator", "reason": "not-canonical-cas-locator"},
            )
        _require_digest(self.manifest_sha256, "artifact.invalid", "manifest_sha256")
        _require_digest(self.set_sha256, "artifact.invalid", "set_sha256")
        if self.producer_run_id is not None:
            _require_identifier(self.producer_run_id, "artifact.invalid", "producer_run_id")
        if self.producer_stage is not None:
            _require_stage(self.producer_stage, "artifact.invalid", "producer_stage")
        _require_digest(self.producer_tool_sha256, "artifact.invalid", "producer_tool_sha256")

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "locator": self.locator,
            "manifest_sha256": self.manifest_sha256,
            "producer_run_id": self.producer_run_id,
            "producer_stage": self.producer_stage,
            "producer_tool_sha256": self.producer_tool_sha256,
            "set_sha256": self.set_sha256,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ArtifactRef:
        if not isinstance(record, Mapping):
            raise ProtocolError("artifact.invalid", {"reason": "not-an-object"})
        expected = {
            "artifact_id",
            "kind",
            "locator",
            "manifest_sha256",
            "producer_run_id",
            "producer_stage",
            "producer_tool_sha256",
            "set_sha256",
        }
        if set(record) != expected:
            raise ProtocolError(
                "artifact.invalid",
                {"missing": sorted(expected - set(record)), "unknown": sorted(set(record) - expected)},
            )
        return cls(**dict(record))


@dataclass(frozen=True, slots=True)
class NativeTargetIdentity:
    """The stable runtime target, separate from transport locator and tool identity."""

    schema_version: str
    install_id: str
    pipeline_id: str
    target_id: str

    def __post_init__(self) -> None:
        if self.schema_version != NATIVE_TARGET_IDENTITY_SCHEMA:
            raise ProtocolError(
                "target_identity.schema_unsupported",
                {"value": self.schema_version},
            )
        _require_identifier(self.install_id, "target_identity.invalid", "install_id")
        _require_identifier(self.pipeline_id, "target_identity.invalid", "pipeline_id")
        _require_digest(self.target_id, "target_identity.invalid", "target_id")
        if digest(self.identity_record()) != self.target_id:
            raise ProtocolError(
                "target_identity.digest_mismatch",
                {"target_id": self.target_id},
            )

    def identity_record(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "install_id": self.install_id,
            "pipeline_id": self.pipeline_id,
        }

    def to_record(self) -> dict[str, str]:
        return {**self.identity_record(), "target_id": self.target_id}

    @classmethod
    def create(cls, *, install_id: str, pipeline_id: str) -> NativeTargetIdentity:
        identity = {
            "schema_version": NATIVE_TARGET_IDENTITY_SCHEMA,
            "install_id": install_id,
            "pipeline_id": pipeline_id,
        }
        return cls(target_id=digest(identity), **identity)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> NativeTargetIdentity:
        expected = {"schema_version", "install_id", "pipeline_id", "target_id"}
        if not isinstance(record, Mapping) or set(record) != expected:
            raise ProtocolError(
                "target_identity.invalid",
                {"reason": "record-shape"},
            )
        return cls(**dict(record))


def artifact_closure_sha256(artifacts: tuple[ArtifactRef, ...]) -> str:
    """Digest one ordered complete input artifact closure."""

    if not isinstance(artifacts, tuple):
        raise ProtocolError("artifact.closure_invalid", {"reason": "tuple-required"})
    if not all(isinstance(item, ArtifactRef) for item in artifacts):
        raise ProtocolError("artifact.closure_invalid", {"reason": "artifact-ref-required"})
    return digest([item.to_record() for item in artifacts])


@dataclass(frozen=True, slots=True)
class DiagnosticReplayScope:
    """The immutable single-stage diagnostic replay identity."""

    schema_version: str
    target_stage: StageId
    input_closure_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != DIAGNOSTIC_SCOPE_SCHEMA:
            raise ProtocolError("diagnostic.schema_unsupported", {"value": self.schema_version})
        _require_stage(self.target_stage, "diagnostic.invalid", "target_stage")
        _require_digest(self.input_closure_sha256, "diagnostic.invalid", "input_closure_sha256")

    @classmethod
    def from_inputs(
        cls,
        target_stage: StageId,
        input_artifacts: tuple[ArtifactRef, ...],
    ) -> DiagnosticReplayScope:
        return cls(
            schema_version=DIAGNOSTIC_SCOPE_SCHEMA,
            target_stage=target_stage,
            input_closure_sha256=artifact_closure_sha256(input_artifacts),
        )

    def validate_for(self, stage: StageId, input_artifacts: tuple[ArtifactRef, ...]) -> None:
        if self.target_stage != stage or self.input_closure_sha256 != artifact_closure_sha256(input_artifacts):
            raise ProtocolError("diagnostic.command_mismatch", {"stage": stage})

    def to_record(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "target_stage": self.target_stage,
            "input_closure_sha256": self.input_closure_sha256,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> DiagnosticReplayScope:
        if not isinstance(record, Mapping):
            raise ProtocolError("diagnostic.invalid", {"reason": "not-an-object"})
        expected = {"schema_version", "target_stage", "input_closure_sha256"}
        if set(record) != expected:
            raise ProtocolError(
                "diagnostic.invalid",
                {"missing": sorted(expected - set(record)), "unknown": sorted(set(record) - expected)},
            )
        return cls(**dict(record))

    @property
    def digest(self) -> str:
        return digest(self.to_record())


@dataclass(frozen=True, slots=True)
class CancellationRequest:
    """One immutable request to cancel an existing stage command."""

    schema_version: str
    target_command_id: str
    target_run_id: str
    target_stage: StageId
    target_request_digest: str
    reason_code: str
    reason: str
    requested_by: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != CANCEL_REQUEST_SCHEMA:
            raise ProtocolError("cancel.invalid", {"where": "schema_version"})
        for field in ("target_command_id", "target_run_id", "reason_code", "reason", "requested_by"):
            _require_text(getattr(self, field), "cancel.invalid", field)
        _require_stage(self.target_stage, "cancel.invalid", "target_stage")
        _require_digest(self.target_request_digest, "cancel.invalid", "target_request_digest")
        if not isinstance(self.requested_at, datetime) or self.requested_at.tzinfo is None:
            raise ProtocolError("cancel.invalid", {"where": "requested_at"})

    def to_record(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "reason_code": self.reason_code,
            "requested_at": self.requested_at.isoformat(),
            "requested_by": self.requested_by,
            "schema_version": self.schema_version,
            "target_command_id": self.target_command_id,
            "target_request_digest": self.target_request_digest,
            "target_run_id": self.target_run_id,
            "target_stage": self.target_stage,
        }

    def logical_identity(self) -> dict[str, Any]:
        """The retry identity of the request, excluding server receipt time."""

        record = self.to_record()
        record.pop("requested_at")
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> CancellationRequest:
        expected = {
            "reason",
            "reason_code",
            "requested_at",
            "requested_by",
            "schema_version",
            "target_command_id",
            "target_request_digest",
            "target_run_id",
            "target_stage",
        }
        if not isinstance(record, Mapping) or set(record) != expected:
            raise ProtocolError("cancel.invalid", {"where": "request"})
        if not isinstance(record["requested_at"], str):
            raise ProtocolError("cancel.invalid", {"where": "requested_at"})
        try:
            requested_at = datetime.fromisoformat(record["requested_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise ProtocolError("cancel.invalid", {"where": "requested_at"}) from error
        return cls(
            schema_version=record["schema_version"],
            target_command_id=record["target_command_id"],
            target_run_id=record["target_run_id"],
            target_stage=record["target_stage"],
            target_request_digest=record["target_request_digest"],
            reason_code=record["reason_code"],
            reason=record["reason"],
            requested_by=record["requested_by"],
            requested_at=requested_at,
        )

    @classmethod
    def from_command(cls, command: StageCommand) -> CancellationRequest:
        if command.kind != "cancel" or command.stage is not None:
            raise ProtocolError("cancel.invalid", {"where": "command"})
        request = cls.from_record(command.request)
        if request.target_run_id != command.run_id:
            raise ProtocolError("cancel.invalid", {"where": "target_run_id"})
        return request


@dataclass(frozen=True, slots=True)
class StageCommand:
    """The complete non-secret request that authorizes one stage execution."""

    schema_version: str
    command_id: str
    run_id: str
    kind: Literal["stage", "cancel", "reconcile"]
    stage: StageId | None
    idempotency_key: str
    request: Mapping[str, Any]
    request_digest: str
    input_artifacts: tuple[ArtifactRef, ...]
    config_snapshot_sha256: str
    contract_sha256: str
    policy_sha256: str
    expected_executor_sha256: str
    predecessor_decision_id: str | None
    diagnostic_scope: DiagnosticReplayScope | None = None

    def __post_init__(self) -> None:
        if self.schema_version != STAGE_COMMAND_SCHEMA:
            raise ProtocolError("command.schema_unsupported", {"value": self.schema_version})
        _require_identifier(self.command_id, "command.invalid", "command_id")
        _require_identifier(self.run_id, "command.invalid", "run_id")
        if self.kind not in {"stage", "cancel", "reconcile"}:
            raise ProtocolError("command.invalid", {"where": "kind", "value": self.kind})
        if self.kind == "stage" or self.stage is not None:
            _require_stage(self.stage, "command.invalid", "stage")
        if self.kind == "cancel":
            CancellationRequest.from_command(self)
        _require_text(self.idempotency_key, "command.invalid", "idempotency_key")
        object.__setattr__(self, "request", _freeze_mapping(self.request, "command.invalid", "request"))
        _require_digest(self.request_digest, "command.invalid", "request_digest")
        if not isinstance(self.input_artifacts, tuple) or not all(
            isinstance(item, ArtifactRef) for item in self.input_artifacts
        ):
            raise ProtocolError("command.invalid", {"where": "input_artifacts"})
        if len({item.artifact_id for item in self.input_artifacts}) != len(self.input_artifacts):
            raise ProtocolError("command.invalid", {"where": "input_artifacts", "reason": "duplicate"})
        _require_digest(self.config_snapshot_sha256, "command.invalid", "config_snapshot_sha256")
        _require_digest(self.contract_sha256, "command.invalid", "contract_sha256")
        _require_digest(self.policy_sha256, "command.invalid", "policy_sha256")
        _require_digest(self.expected_executor_sha256, "command.invalid", "expected_executor_sha256")
        if self.predecessor_decision_id is not None:
            _require_text(self.predecessor_decision_id, "command.invalid", "predecessor_decision_id")
        if self.diagnostic_scope is not None:
            if not isinstance(self.diagnostic_scope, DiagnosticReplayScope):
                raise ProtocolError("command.invalid", {"where": "diagnostic_scope"})
            self.diagnostic_scope.validate_for(self.stage, self.input_artifacts)
            if self.predecessor_decision_id is not None:
                raise ProtocolError(
                    "command.invalid",
                    {"where": "predecessor_decision_id", "reason": "diagnostic"},
                )
        if digest(self.identity_record()) != self.request_digest:
            raise ProtocolError("command.digest_mismatch", {"command_id": self.command_id})

    def identity_record(self) -> dict[str, Any]:
        record = {
            "command_id": self.command_id,
            "config_snapshot_sha256": self.config_snapshot_sha256,
            "contract_sha256": self.contract_sha256,
            "expected_executor_sha256": self.expected_executor_sha256,
            "idempotency_key": self.idempotency_key,
            "input_artifacts": [item.to_record() for item in self.input_artifacts],
            "kind": self.kind,
            "policy_sha256": self.policy_sha256,
            "predecessor_decision_id": self.predecessor_decision_id,
            "request": _json_value(self.request),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "stage": self.stage,
        }
        if self.diagnostic_scope is not None:
            record["diagnostic_scope"] = self.diagnostic_scope.to_record()
        return record

    @classmethod
    def create(
        cls,
        *,
        command_id: str,
        run_id: str,
        stage: StageId,
        idempotency_key: str,
        request: Mapping[str, Any],
        input_artifacts: tuple[ArtifactRef, ...],
        config_snapshot_sha256: str,
        contract_sha256: str,
        policy_sha256: str,
        expected_executor_sha256: str,
        predecessor_decision_id: str | None,
        diagnostic_scope: DiagnosticReplayScope | None = None,
    ) -> StageCommand:
        values = {
            "schema_version": STAGE_COMMAND_SCHEMA,
            "command_id": command_id,
            "run_id": run_id,
            "kind": "stage",
            "stage": stage,
            "idempotency_key": idempotency_key,
            "request": dict(request),
            "input_artifacts": input_artifacts,
            "config_snapshot_sha256": config_snapshot_sha256,
            "contract_sha256": contract_sha256,
            "policy_sha256": policy_sha256,
            "expected_executor_sha256": expected_executor_sha256,
            "predecessor_decision_id": predecessor_decision_id,
            "diagnostic_scope": diagnostic_scope,
        }
        identity = {
            "command_id": command_id,
            "idempotency_key": idempotency_key,
            "kind": values["kind"],
            "request": values["request"],
            "schema_version": values["schema_version"],
            "stage": values["stage"],
            "run_id": run_id,
            "input_artifacts": [item.to_record() for item in input_artifacts],
            "config_snapshot_sha256": config_snapshot_sha256,
            "contract_sha256": contract_sha256,
            "policy_sha256": policy_sha256,
            "expected_executor_sha256": expected_executor_sha256,
            "predecessor_decision_id": predecessor_decision_id,
        }
        if diagnostic_scope is not None:
            identity["diagnostic_scope"] = diagnostic_scope.to_record()
        request_digest = digest(identity)
        return cls(request_digest=request_digest, **values)

    @classmethod
    def create_cancel(
        cls,
        *,
        command_id: str,
        run_id: str,
        idempotency_key: str,
        request: CancellationRequest,
        config_snapshot_sha256: str,
        contract_sha256: str,
        policy_sha256: str,
        expected_executor_sha256: str,
    ) -> StageCommand:
        if not isinstance(request, CancellationRequest) or request.target_run_id != run_id:
            raise ProtocolError("cancel.invalid", {"where": "request"})
        request_record = request.to_record()
        values = {
            "schema_version": STAGE_COMMAND_SCHEMA,
            "command_id": command_id,
            "run_id": run_id,
            "kind": "cancel",
            "stage": None,
            "idempotency_key": idempotency_key,
            "request": request_record,
            "input_artifacts": (),
            "config_snapshot_sha256": config_snapshot_sha256,
            "contract_sha256": contract_sha256,
            "policy_sha256": policy_sha256,
            "expected_executor_sha256": expected_executor_sha256,
            "predecessor_decision_id": None,
            "diagnostic_scope": None,
        }
        identity = {
            key: values[key]
            for key in (
                "command_id",
                "idempotency_key",
                "kind",
                "request",
                "schema_version",
                "stage",
                "run_id",
                "input_artifacts",
                "config_snapshot_sha256",
                "contract_sha256",
                "policy_sha256",
                "expected_executor_sha256",
                "predecessor_decision_id",
            )
        }
        return cls(request_digest=digest(identity), **values)

    def to_record(self) -> dict[str, Any]:
        return {
            **self.identity_record(),
            "request_digest": self.request_digest,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> StageCommand:
        if not isinstance(record, Mapping):
            raise ProtocolError("command.invalid", {"reason": "not-an-object"})
        required = {
            "command_id",
            "config_snapshot_sha256",
            "contract_sha256",
            "expected_executor_sha256",
            "idempotency_key",
            "input_artifacts",
            "kind",
            "policy_sha256",
            "predecessor_decision_id",
            "request",
            "request_digest",
            "run_id",
            "schema_version",
            "stage",
        }
        expected = required | ({"diagnostic_scope"} if "diagnostic_scope" in record else set())
        if set(record) != expected:
            raise ProtocolError(
                "command.invalid",
                {"missing": sorted(required - set(record)), "unknown": sorted(set(record) - expected)},
            )
        inputs = record["input_artifacts"]
        if not isinstance(inputs, (list, tuple)):
            raise ProtocolError("command.invalid", {"where": "input_artifacts"})
        return cls(
            schema_version=record["schema_version"],
            command_id=record["command_id"],
            run_id=record["run_id"],
            kind=record["kind"],
            stage=record["stage"],
            idempotency_key=record["idempotency_key"],
            request=record["request"],
            request_digest=record["request_digest"],
            input_artifacts=tuple(ArtifactRef.from_record(item) for item in inputs),
            config_snapshot_sha256=record["config_snapshot_sha256"],
            contract_sha256=record["contract_sha256"],
            policy_sha256=record["policy_sha256"],
            expected_executor_sha256=record["expected_executor_sha256"],
            predecessor_decision_id=record["predecessor_decision_id"],
            diagnostic_scope=(
                DiagnosticReplayScope.from_record(record["diagnostic_scope"])
                if "diagnostic_scope" in record
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class DispatchConf:
    """The non-secret Airflow conf, containing references only."""

    schema_version: str
    run_id: str
    freeze_command_id: str

    def __post_init__(self) -> None:
        if self.schema_version != AIRFLOW_DISPATCH_CONF_SCHEMA:
            raise ProtocolError("dispatch_conf.schema_unsupported", {"value": self.schema_version})
        _require_identifier(self.run_id, "dispatch_conf.invalid", "run_id")
        _require_identifier(self.freeze_command_id, "dispatch_conf.invalid", "freeze_command_id")

    def to_record(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "freeze_command_id": self.freeze_command_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> DispatchConf:
        if not isinstance(record, Mapping):
            raise ProtocolError("dispatch_conf.invalid", {"reason": "not-an-object"})
        expected = {"schema_version", "run_id", "freeze_command_id"}
        if set(record) != expected:
            raise ProtocolError(
                "dispatch_conf.invalid",
                {"missing": sorted(expected - set(record)), "unknown": sorted(set(record) - expected)},
            )
        return cls(**dict(record))


@dataclass(frozen=True, slots=True)
class DispatchCommand:
    """One immutable technical Airflow dispatch bound to a run and Freeze command."""

    schema_version: str
    command_id: str
    run_id: str
    freeze_command_id: str
    idempotency_key: str
    dag_id: str
    dag_run_id: str
    conf_artifact: ArtifactRef
    request_digest: str
    expected_executor_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != DISPATCH_COMMAND_SCHEMA:
            raise ProtocolError("dispatch.schema_unsupported", {"value": self.schema_version})
        _require_identifier(self.command_id, "dispatch.invalid", "command_id")
        _require_identifier(self.run_id, "dispatch.invalid", "run_id")
        _require_identifier(self.freeze_command_id, "dispatch.invalid", "freeze_command_id")
        _require_text(self.idempotency_key, "dispatch.invalid", "idempotency_key")
        _require_identifier(self.dag_id, "dispatch.invalid", "dag_id")
        _require_identifier(self.dag_run_id, "dispatch.invalid", "dag_run_id")
        if not isinstance(self.conf_artifact, ArtifactRef):
            raise ProtocolError("dispatch.invalid", {"where": "conf_artifact"})
        if (
            self.conf_artifact.kind != "airflow_dispatch_conf"
            or self.conf_artifact.producer_run_id != self.run_id
            or self.conf_artifact.producer_stage is not None
            or self.conf_artifact.producer_tool_sha256 != self.expected_executor_sha256
        ):
            raise ProtocolError("dispatch.invalid", {"where": "conf_artifact", "reason": "binding"})
        _require_digest(self.request_digest, "dispatch.invalid", "request_digest")
        _require_digest(self.expected_executor_sha256, "dispatch.invalid", "expected_executor_sha256")
        if digest(self.identity_record()) != self.request_digest:
            raise ProtocolError("dispatch.digest_mismatch", {"command_id": self.command_id})

    @property
    def effect_key(self) -> str:
        return "airflow_dispatch"

    def identity_record(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "conf_artifact": self.conf_artifact.to_record(),
            "dag_id": self.dag_id,
            "dag_run_id": self.dag_run_id,
            "expected_executor_sha256": self.expected_executor_sha256,
            "freeze_command_id": self.freeze_command_id,
            "idempotency_key": self.idempotency_key,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def create(
        cls,
        *,
        command_id: str,
        run_id: str,
        freeze_command_id: str,
        idempotency_key: str,
        dag_id: str,
        dag_run_id: str,
        conf_artifact: ArtifactRef,
        expected_executor_sha256: str,
    ) -> DispatchCommand:
        values = {
            "schema_version": DISPATCH_COMMAND_SCHEMA,
            "command_id": command_id,
            "run_id": run_id,
            "freeze_command_id": freeze_command_id,
            "idempotency_key": idempotency_key,
            "dag_id": dag_id,
            "dag_run_id": dag_run_id,
            "conf_artifact": conf_artifact,
            "expected_executor_sha256": expected_executor_sha256,
        }
        identity = {
            **values,
            "conf_artifact": conf_artifact.to_record(),
        }
        return cls(request_digest=digest(identity), **values)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> DispatchCommand:
        if not isinstance(record, Mapping):
            raise ProtocolError("dispatch.invalid", {"reason": "not-an-object"})
        expected = {
            "command_id",
            "conf_artifact",
            "dag_id",
            "dag_run_id",
            "expected_executor_sha256",
            "freeze_command_id",
            "idempotency_key",
            "request_digest",
            "run_id",
            "schema_version",
        }
        if set(record) != expected:
            raise ProtocolError(
                "dispatch.invalid",
                {"missing": sorted(expected - set(record)), "unknown": sorted(set(record) - expected)},
            )
        return cls(
            schema_version=record["schema_version"],
            command_id=record["command_id"],
            run_id=record["run_id"],
            freeze_command_id=record["freeze_command_id"],
            idempotency_key=record["idempotency_key"],
            dag_id=record["dag_id"],
            dag_run_id=record["dag_run_id"],
            conf_artifact=ArtifactRef.from_record(record["conf_artifact"]),
            request_digest=record["request_digest"],
            expected_executor_sha256=record["expected_executor_sha256"],
        )

    def to_record(self) -> dict[str, Any]:
        return {**self.identity_record(), "request_digest": self.request_digest}


@dataclass(frozen=True, slots=True)
class StageResult:
    """One immutable business result for a stage command."""

    schema_version: str
    command_id: str
    run_id: str
    stage: StageId
    request_digest: str
    outcome: Outcome
    output_artifact: ArtifactRef
    actual_executor_sha256: str
    checks_sha256: str
    resources_settled: bool
    required_effect_ids: tuple[str, ...] = ()
    reason_code: str | None = None
    diagnostic_scope: DiagnosticReplayScope | None = None

    def __post_init__(self) -> None:
        if self.schema_version != STAGE_RESULT_SCHEMA:
            raise ProtocolError("result.schema_unsupported", {"value": self.schema_version})
        _require_identifier(self.command_id, "result.invalid", "command_id")
        _require_identifier(self.run_id, "result.invalid", "run_id")
        _require_stage(self.stage, "result.invalid", "stage")
        _require_digest(self.request_digest, "result.invalid", "request_digest")
        if self.outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("result.invalid", {"where": "outcome", "value": self.outcome})
        if not isinstance(self.output_artifact, ArtifactRef):
            raise ProtocolError("result.invalid", {"where": "output_artifact"})
        if self.output_artifact.kind == "stage_result":
            raise ProtocolError("result.invalid", {"where": "output_artifact", "reason": "record-is-not-output"})
        _require_digest(self.actual_executor_sha256, "result.invalid", "actual_executor_sha256")
        if (
            self.output_artifact.producer_run_id != self.run_id
            or self.output_artifact.producer_stage != self.stage
            or self.output_artifact.producer_tool_sha256 != self.actual_executor_sha256
        ):
            raise ProtocolError("result.invalid", {"where": "output_artifact", "reason": "producer-mismatch"})
        _require_digest(self.checks_sha256, "result.invalid", "checks_sha256")
        if type(self.resources_settled) is not bool:
            raise ProtocolError("result.invalid", {"where": "resources_settled"})
        if not isinstance(self.required_effect_ids, tuple) or not all(
            isinstance(value, str) and value for value in self.required_effect_ids
        ):
            raise ProtocolError("result.invalid", {"where": "required_effect_ids"})
        if len(set(self.required_effect_ids)) != len(self.required_effect_ids):
            raise ProtocolError("result.invalid", {"where": "required_effect_ids", "reason": "duplicate"})
        if self.outcome in {"failed", "cancelled"} and not self.reason_code:
            raise ProtocolError("result.invalid", {"where": "reason_code"})
        if self.outcome == "completed" and self.reason_code is not None:
            raise ProtocolError("result.invalid", {"where": "reason_code", "reason": "completed"})
        if self.outcome == "completed" and self.resources_settled is not True:
            raise ProtocolError("result.invalid", {"where": "resources_settled", "reason": "completed"})
        if self.diagnostic_scope is not None:
            if not isinstance(self.diagnostic_scope, DiagnosticReplayScope):
                raise ProtocolError("result.invalid", {"where": "diagnostic_scope"})
            if self.diagnostic_scope.target_stage != self.stage:
                raise ProtocolError("result.invalid", {"where": "diagnostic_scope", "reason": "stage"})

    def to_record(self) -> dict[str, Any]:
        record = {
            "actual_executor_sha256": self.actual_executor_sha256,
            "checks_sha256": self.checks_sha256,
            "command_id": self.command_id,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "request_digest": self.request_digest,
            "resources_settled": self.resources_settled,
            "required_effect_ids": list(self.required_effect_ids),
            "output_artifact": self.output_artifact.to_record(),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "stage": self.stage,
        }
        if self.diagnostic_scope is not None:
            record["diagnostic_scope"] = self.diagnostic_scope.to_record()
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> StageResult:
        if not isinstance(record, Mapping):
            raise ProtocolError("result.invalid", {"reason": "not-an-object"})
        required = {
            "actual_executor_sha256",
            "checks_sha256",
            "command_id",
            "outcome",
            "reason_code",
            "request_digest",
            "resources_settled",
            "required_effect_ids",
            "output_artifact",
            "run_id",
            "schema_version",
            "stage",
        }
        expected = required | ({"diagnostic_scope"} if "diagnostic_scope" in record else set())
        if set(record) != expected:
            raise ProtocolError(
                "result.invalid",
                {"missing": sorted(required - set(record)), "unknown": sorted(set(record) - expected)},
            )
        required_effects = record["required_effect_ids"]
        if not isinstance(required_effects, list):
            raise ProtocolError("result.invalid", {"where": "required_effect_ids"})
        return cls(
            schema_version=record["schema_version"],
            command_id=record["command_id"],
            run_id=record["run_id"],
            stage=record["stage"],
            request_digest=record["request_digest"],
            outcome=record["outcome"],
            output_artifact=ArtifactRef.from_record(record["output_artifact"]),
            actual_executor_sha256=record["actual_executor_sha256"],
            checks_sha256=record["checks_sha256"],
            resources_settled=record["resources_settled"],
            required_effect_ids=tuple(required_effects),
            reason_code=record["reason_code"],
            diagnostic_scope=(
                DiagnosticReplayScope.from_record(record["diagnostic_scope"])
                if "diagnostic_scope" in record
                else None
            ),
        )

    @property
    def digest(self) -> str:
        return digest(self.to_record())


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    """A fenced right to submit facts for one command."""

    claim_id: str
    command_id: str
    fence: int
    owner_sha256: str
    executor_sha256: str
    lane_key: str | None
    expires_at: datetime
    scope: Literal["execution", "transfer_resume"] = "execution"
    effect_scope_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.claim_id, "claim.invalid", "claim_id")
        _require_identifier(self.command_id, "claim.invalid", "command_id")
        if type(self.fence) is not int or self.fence <= 0:
            raise ProtocolError("claim.invalid", {"where": "fence"})
        _require_digest(self.owner_sha256, "claim.invalid", "owner_sha256")
        _require_digest(self.executor_sha256, "claim.invalid", "executor_sha256")
        if self.owner_sha256 == self.executor_sha256:
            raise ProtocolError("claim.invalid", {"reason": "owner-tool-identity-collision"})
        if self.lane_key is not None:
            _require_text(self.lane_key, "claim.invalid", "lane_key")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise ProtocolError("claim.invalid", {"where": "expires_at"})
        if self.scope not in {"execution", "transfer_resume"}:
            raise ProtocolError("claim.invalid", {"where": "scope"})
        if self.effect_scope_id is not None:
            _require_identifier(self.effect_scope_id, "claim.invalid", "effect_scope_id")
        if (self.scope == "transfer_resume") != (self.effect_scope_id is not None):
            raise ProtocolError(
                "claim.invalid",
                {"reason": "scope-effect-mismatch"},
            )


@dataclass(frozen=True, slots=True)
class StageDeferral:
    """A handler request to yield an execution claim for one unresolved effect."""

    effect_id: str

    def __post_init__(self) -> None:
        _require_identifier(self.effect_id, "deferral.invalid", "effect_id")


@dataclass(frozen=True, slots=True)
class StageEffectWait:
    """A non-secret immutable locator for one yielded stage effect."""

    schema_version: str
    run_id: str
    stage: StageId
    command_id: str
    request_digest: str
    effect_id: str
    effect_key: str
    effect_kind: EffectKind
    intent_identity_digest: str
    outbound_artifact_id: str
    outbound_digest: str
    expected_state_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != STAGE_EFFECT_WAIT_SCHEMA:
            raise ProtocolError("effect_wait.schema_unsupported", {"value": self.schema_version})
        _require_identifier(self.run_id, "effect_wait.invalid", "run_id")
        _require_stage(self.stage, "effect_wait.invalid", "stage")
        _require_identifier(self.command_id, "effect_wait.invalid", "command_id")
        _require_digest(self.request_digest, "effect_wait.invalid", "request_digest")
        _require_identifier(self.effect_id, "effect_wait.invalid", "effect_id")
        _require_text(self.effect_key, "effect_wait.invalid", "effect_key")
        if self.effect_kind not in {"airflow_dispatch", "native_operation", "git_ref_update", "pr_upsert"}:
            raise ProtocolError("effect_wait.invalid", {"where": "effect_kind"})
        _require_digest(self.intent_identity_digest, "effect_wait.invalid", "intent_identity_digest")
        _require_identifier(self.outbound_artifact_id, "effect_wait.invalid", "outbound_artifact_id")
        _require_digest(self.outbound_digest, "effect_wait.invalid", "outbound_digest")
        _require_digest(self.expected_state_digest, "effect_wait.invalid", "expected_state_digest")

    def to_record(self) -> dict[str, str]:
        return {
            "command_id": self.command_id,
            "effect_id": self.effect_id,
            "effect_key": self.effect_key,
            "effect_kind": self.effect_kind,
            "expected_state_digest": self.expected_state_digest,
            "intent_identity_digest": self.intent_identity_digest,
            "outbound_artifact_id": self.outbound_artifact_id,
            "outbound_digest": self.outbound_digest,
            "request_digest": self.request_digest,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "stage": self.stage,
        }

    @property
    def digest(self) -> str:
        return digest(self.to_record())

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> StageEffectWait:
        expected = {
            "command_id",
            "effect_id",
            "effect_key",
            "effect_kind",
            "expected_state_digest",
            "intent_identity_digest",
            "outbound_artifact_id",
            "outbound_digest",
            "request_digest",
            "run_id",
            "schema_version",
            "stage",
        }
        if not isinstance(record, Mapping) or set(record) != expected:
            raise ProtocolError(
                "effect_wait.invalid",
                {
                    "missing": sorted(expected - set(record)) if isinstance(record, Mapping) else [],
                    "unknown": sorted(set(record) - expected) if isinstance(record, Mapping) else [],
                },
            )
        return cls(**dict(record))


@dataclass(frozen=True, slots=True)
class StagePending:
    """A non-terminal attempt result that instructs the caller to defer."""

    wait: StageEffectWait

    def __post_init__(self) -> None:
        if not isinstance(self.wait, StageEffectWait):
            raise ProtocolError("stage_pending.invalid", {"where": "wait"})


class StageFailure(RuntimeError):
    """A trusted domain handler's definite failure or cancellation signal."""

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        outcome: Outcome = "failed",
        evidence_artifacts: tuple[ArtifactRef, ...] = (),
        unavailable_reason: str | None = None,
        error_type: str | None = None,
        detail: Mapping[str, Any] | None = None,
        retained_locations: tuple[str, ...] = (),
    ) -> None:
        _require_text(reason_code, "failure.invalid", "reason_code")
        _require_text(message, "failure.invalid", "message")
        if outcome not in {"failed", "cancelled"}:
            raise ProtocolError("failure.invalid", {"where": "outcome", "value": outcome})
        if not isinstance(evidence_artifacts, tuple) or not all(
            isinstance(item, ArtifactRef) for item in evidence_artifacts
        ):
            raise ProtocolError("failure.invalid", {"where": "evidence_artifacts"})
        if unavailable_reason is not None:
            _require_text(unavailable_reason, "failure.invalid", "unavailable_reason")
        error_type = error_type or type(self).__name__
        _require_text(error_type, "failure.invalid", "error_type")
        frozen_detail = None if detail is None else _freeze_mapping(detail, "failure.invalid", "detail")
        if not isinstance(retained_locations, tuple) or not all(
            isinstance(item, str) and item and "\x00" not in item for item in retained_locations
        ):
            raise ProtocolError("failure.invalid", {"where": "retained_locations"})
        super().__init__(message)
        self.reason_code = reason_code
        self.outcome = outcome
        self.evidence_artifacts = evidence_artifacts
        self.unavailable_reason = unavailable_reason
        self.error_type = error_type
        self.detail = frozen_detail
        self.retained_locations = retained_locations


@dataclass(frozen=True, slots=True)
class FailureEnvelope:
    """The minimal immutable record of a definite domain failure or cancellation."""

    schema_version: str
    command_id: str
    run_id: str
    stage: StageId
    request_digest: str
    outcome: Outcome
    actual_executor_sha256: str
    reason_code: str
    error_type: str
    error_message: str
    error_detail: Mapping[str, Any] | None
    effect_states: tuple[tuple[str, str], ...]
    evidence_artifacts: tuple[ArtifactRef, ...]
    retained_locations: tuple[str, ...]
    unavailable_reason: str | None
    occurred_at: datetime
    diagnostic_scope: DiagnosticReplayScope | None = None

    def __post_init__(self) -> None:
        if self.schema_version != FAILURE_ENVELOPE_SCHEMA:
            raise ProtocolError("failure.schema_unsupported", {"value": self.schema_version})
        _require_identifier(self.command_id, "failure.invalid", "command_id")
        _require_identifier(self.run_id, "failure.invalid", "run_id")
        _require_stage(self.stage, "failure.invalid", "stage")
        _require_digest(self.request_digest, "failure.invalid", "request_digest")
        if self.outcome not in {"failed", "cancelled"}:
            raise ProtocolError("failure.invalid", {"where": "outcome", "value": self.outcome})
        _require_digest(self.actual_executor_sha256, "failure.invalid", "actual_executor_sha256")
        _require_text(self.reason_code, "failure.invalid", "reason_code")
        _require_text(self.error_type, "failure.invalid", "error_type")
        _require_text(self.error_message, "failure.invalid", "error_message")
        if self.error_detail is not None:
            object.__setattr__(
                self,
                "error_detail",
                _freeze_mapping(self.error_detail, "failure.invalid", "error_detail"),
            )
        if len(self.error_message) > 4096:
            raise ProtocolError("failure.invalid", {"where": "error_message", "reason": "too-long"})
        if not isinstance(self.effect_states, tuple):
            raise ProtocolError("failure.invalid", {"where": "effect_states"})
        effect_ids = []
        for row in self.effect_states:
            if (
                not isinstance(row, tuple)
                or len(row) != 2
                or not isinstance(row[0], str)
                or not _SAFE_ID.fullmatch(row[0])
                or row[1] not in {"prepared", "unknown", "satisfied", "definitely_failed", "conflict"}
            ):
                raise ProtocolError("failure.invalid", {"where": "effect_states"})
            effect_ids.append(row[0])
        if len(effect_ids) != len(set(effect_ids)):
            raise ProtocolError("failure.invalid", {"where": "effect_states", "reason": "duplicate"})
        if not isinstance(self.evidence_artifacts, tuple) or not all(
            isinstance(item, ArtifactRef) for item in self.evidence_artifacts
        ):
            raise ProtocolError("failure.invalid", {"where": "evidence_artifacts"})
        if len({item.artifact_id for item in self.evidence_artifacts}) != len(self.evidence_artifacts):
            raise ProtocolError("failure.invalid", {"where": "evidence_artifacts", "reason": "duplicate"})
        if not isinstance(self.retained_locations, tuple) or not all(
            isinstance(item, str) and item and "\x00" not in item for item in self.retained_locations
        ):
            raise ProtocolError("failure.invalid", {"where": "retained_locations"})
        if len(set(self.retained_locations)) != len(self.retained_locations):
            raise ProtocolError("failure.invalid", {"where": "retained_locations", "reason": "duplicate"})
        if self.unavailable_reason is not None:
            _require_text(self.unavailable_reason, "failure.invalid", "unavailable_reason")
        if not isinstance(self.occurred_at, datetime) or self.occurred_at.tzinfo is None:
            raise ProtocolError("failure.invalid", {"where": "occurred_at"})
        if self.diagnostic_scope is not None:
            if not isinstance(self.diagnostic_scope, DiagnosticReplayScope):
                raise ProtocolError("failure.invalid", {"where": "diagnostic_scope"})
            if self.diagnostic_scope.target_stage != self.stage:
                raise ProtocolError("failure.invalid", {"where": "diagnostic_scope", "reason": "stage"})

    def to_record(self) -> dict[str, Any]:
        record = {
            "actual_executor_sha256": self.actual_executor_sha256,
            "command_id": self.command_id,
            "effect_states": [list(row) for row in self.effect_states],
            "error_message": self.error_message,
            "error_detail": _json_value(self.error_detail) if self.error_detail is not None else None,
            "error_type": self.error_type,
            "evidence_artifacts": [item.to_record() for item in self.evidence_artifacts],
            "occurred_at": self.occurred_at.isoformat(),
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "request_digest": self.request_digest,
            "retained_locations": list(self.retained_locations),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "stage": self.stage,
            "unavailable_reason": self.unavailable_reason,
        }
        if self.diagnostic_scope is not None:
            record["diagnostic_scope"] = self.diagnostic_scope.to_record()
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> FailureEnvelope:
        if not isinstance(record, Mapping):
            raise ProtocolError("failure.invalid", {"reason": "not-an-object"})
        required = {
            "actual_executor_sha256",
            "command_id",
            "effect_states",
            "error_message",
            "error_detail",
            "error_type",
            "evidence_artifacts",
            "occurred_at",
            "outcome",
            "reason_code",
            "request_digest",
            "retained_locations",
            "run_id",
            "schema_version",
            "stage",
            "unavailable_reason",
        }
        expected = required | ({"diagnostic_scope"} if "diagnostic_scope" in record else set())
        if set(record) != expected:
            raise ProtocolError(
                "failure.invalid",
                {"missing": sorted(required - set(record)), "unknown": sorted(set(record) - expected)},
            )
        effect_rows = record["effect_states"]
        evidence = record["evidence_artifacts"]
        retained = record["retained_locations"]
        if not isinstance(effect_rows, list) or not isinstance(evidence, list) or not isinstance(retained, list):
            raise ProtocolError("failure.invalid", {"where": "record-arrays"})
        occurred_raw = record["occurred_at"]
        if not isinstance(occurred_raw, str):
            raise ProtocolError("failure.invalid", {"where": "occurred_at"})
        try:
            occurred_at = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
        except ValueError as error:
            raise ProtocolError("failure.invalid", {"where": "occurred_at"}) from error
        return cls(
            schema_version=record["schema_version"],
            command_id=record["command_id"],
            run_id=record["run_id"],
            stage=record["stage"],
            request_digest=record["request_digest"],
            outcome=record["outcome"],
            actual_executor_sha256=record["actual_executor_sha256"],
            reason_code=record["reason_code"],
            error_type=record["error_type"],
            error_message=record["error_message"],
            error_detail=record["error_detail"],
            effect_states=tuple(tuple(row) for row in effect_rows),
            evidence_artifacts=tuple(ArtifactRef.from_record(item) for item in evidence),
            retained_locations=tuple(retained),
            unavailable_reason=record["unavailable_reason"],
            occurred_at=occurred_at,
            diagnostic_scope=(
                DiagnosticReplayScope.from_record(record["diagnostic_scope"])
                if "diagnostic_scope" in record
                else None
            ),
        )

    @property
    def digest(self) -> str:
        return digest(self.to_record())


StageAttemptResult = StageResult | FailureEnvelope | StagePending


@dataclass(frozen=True, slots=True)
class StageQualification:
    """The certificate-facing qualification derived from one committed record."""

    schema_version: str
    command_id: str
    run_id: str
    stage: StageId
    admission_request_digest: str
    request_digest: str
    command_digest: str
    record_digest: str
    outcome: Outcome
    diagnostic_scope: DiagnosticReplayScope | None

    def __post_init__(self) -> None:
        if self.schema_version != QUALIFICATION_SCHEMA:
            raise ProtocolError("qualification.schema_unsupported", {"value": self.schema_version})
        _require_identifier(self.command_id, "qualification.invalid", "command_id")
        _require_identifier(self.run_id, "qualification.invalid", "run_id")
        _require_stage(self.stage, "qualification.invalid", "stage")
        _require_digest(self.admission_request_digest, "qualification.invalid", "admission_request_digest")
        _require_digest(self.request_digest, "qualification.invalid", "request_digest")
        _require_digest(self.command_digest, "qualification.invalid", "command_digest")
        _require_digest(self.record_digest, "qualification.invalid", "record_digest")
        if self.outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("qualification.invalid", {"where": "outcome"})
        if self.diagnostic_scope is not None:
            if not isinstance(self.diagnostic_scope, DiagnosticReplayScope):
                raise ProtocolError("qualification.invalid", {"where": "diagnostic_scope"})
            if self.diagnostic_scope.target_stage != self.stage:
                raise ProtocolError("qualification.invalid", {"where": "diagnostic_scope", "reason": "stage"})

    @property
    def scope_kind(self) -> Literal["formal", "diagnostic"]:
        return "formal" if self.diagnostic_scope is None else "diagnostic"

    @property
    def formal_eligible(self) -> bool:
        return self.scope_kind == "formal" and self.outcome == "completed"

    def identity_record(self) -> dict[str, Any]:
        return {
            "admission_request_digest": self.admission_request_digest,
            "command_digest": self.command_digest,
            "command_id": self.command_id,
            "diagnostic_scope": self.diagnostic_scope.to_record() if self.diagnostic_scope is not None else None,
            "formal_eligible": self.formal_eligible,
            "outcome": self.outcome,
            "record_digest": self.record_digest,
            "request_digest": self.request_digest,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "scope_kind": self.scope_kind,
            "stage": self.stage,
        }

    @property
    def digest(self) -> str:
        return digest(self.identity_record())

    def to_record(self) -> dict[str, Any]:
        return {**self.identity_record(), "qualification_digest": self.digest}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> StageQualification:
        expected = {
            "admission_request_digest",
            "command_digest",
            "command_id",
            "diagnostic_scope",
            "formal_eligible",
            "outcome",
            "qualification_digest",
            "record_digest",
            "request_digest",
            "run_id",
            "schema_version",
            "scope_kind",
            "stage",
        }
        if not isinstance(record, Mapping) or set(record) != expected:
            raise ProtocolError("qualification.invalid", {"where": "record"})
        scope = (
            DiagnosticReplayScope.from_record(record["diagnostic_scope"])
            if record["diagnostic_scope"] is not None
            else None
        )
        value = cls(
            schema_version=record["schema_version"],
            command_id=record["command_id"],
            run_id=record["run_id"],
            stage=record["stage"],
            admission_request_digest=record["admission_request_digest"],
            request_digest=record["request_digest"],
            command_digest=record["command_digest"],
            record_digest=record["record_digest"],
            outcome=record["outcome"],
            diagnostic_scope=scope,
        )
        if (
            record["scope_kind"] != value.scope_kind
            or record["formal_eligible"] is not value.formal_eligible
            or record["qualification_digest"] != value.digest
        ):
            raise ProtocolError("qualification.invalid", {"reason": "derived-fields"})
        return value


def qualify_stage(
    admission: RunAdmission,
    command: StageCommand,
    *,
    result: StageResult | None = None,
    failure: FailureEnvelope | None = None,
) -> StageQualification:
    """Derive formal/diagnostic certificate eligibility from immutable records."""

    if command.stage is None:
        raise ProtocolError("qualification.invalid", {"where": "command.stage"})
    if not isinstance(admission, RunAdmission) or admission.run_id != command.run_id:
        raise ProtocolError("qualification.admission_mismatch", {"run_id": command.run_id})
    if command.diagnostic_scope is not None and admission.from_stage != command.stage:
        raise ProtocolError("qualification.admission_mismatch", {"where": "from_stage"})
    if (result is None) == (failure is None):
        raise ProtocolError("qualification.invalid", {"reason": "one-record-required"})
    record = result if result is not None else failure
    if (
        record.command_id != command.command_id
        or record.run_id != command.run_id
        or record.stage != command.stage
        or record.request_digest != command.request_digest
        or record.actual_executor_sha256 != command.expected_executor_sha256
        or record.diagnostic_scope != command.diagnostic_scope
    ):
        raise ProtocolError("qualification.record_mismatch", {"command_id": command.command_id})
    return StageQualification(
        schema_version=QUALIFICATION_SCHEMA,
        command_id=command.command_id,
        run_id=command.run_id,
        stage=command.stage,
        admission_request_digest=admission.request_digest,
        request_digest=command.request_digest,
        command_digest=digest(command.to_record()),
        record_digest=record.digest,
        outcome=record.outcome,
        diagnostic_scope=command.diagnostic_scope,
    )


def require_formal_qualification(
    qualification: StageQualification,
    admission: RunAdmission,
    command: StageCommand,
    *,
    result: StageResult | None = None,
    failure: FailureEnvelope | None = None,
) -> StageQualification:
    """Recompute and require one formal, same-admission, same-record qualification."""

    if not isinstance(qualification, StageQualification):
        raise ProtocolError("qualification.invalid", {"where": "qualification"})
    expected = qualify_stage(admission, command, result=result, failure=failure)
    if expected != qualification or not expected.formal_eligible:
        raise ProtocolError("qualification.formal_ineligible", {"command_id": command.command_id})
    return expected


@dataclass(frozen=True, slots=True)
class StageExecution:
    """A domain handler result before its artifact closure is sealed."""

    outcome: Outcome
    candidate_root: str
    checks_sha256: str
    resources_settled: bool
    failure: StageFailure | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("execution.invalid", {"where": "outcome", "value": self.outcome})
        _require_text(self.candidate_root, "execution.invalid", "candidate_root")
        _require_digest(self.checks_sha256, "execution.invalid", "checks_sha256")
        if type(self.resources_settled) is not bool:
            raise ProtocolError("execution.invalid", {"where": "resources_settled"})
        if self.outcome == "completed":
            if self.failure is not None:
                raise ProtocolError("execution.invalid", {"where": "failure", "reason": "completed"})
            if self.resources_settled is not True:
                raise ProtocolError("execution.invalid", {"where": "resources_settled", "reason": "completed"})
        elif not isinstance(self.failure, StageFailure) or self.failure.outcome != self.outcome:
            raise ProtocolError("execution.invalid", {"where": "failure", "reason": "required"})

    @property
    def reason_code(self) -> str | None:
        return None if self.failure is None else self.failure.reason_code


@dataclass(frozen=True, slots=True)
class StageCandidate:
    """A sealed stage result not yet committed as a decision."""

    result: StageResult
    result_record_artifact: ArtifactRef | None = None
    candidate_id: str | None = None
    claim_id: str | None = None
    fence: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.result, StageResult):
            raise ProtocolError("candidate.invalid", {"where": "result"})
        values = (self.result_record_artifact, self.candidate_id, self.claim_id, self.fence)
        if any(value is None for value in values) and not all(value is None for value in values):
            raise ProtocolError("candidate.invalid", {"reason": "partial-persisted-identity"})
        if self.result_record_artifact is not None:
            if not isinstance(self.result_record_artifact, ArtifactRef):
                raise ProtocolError("candidate.invalid", {"where": "result_record_artifact"})
            if self.result_record_artifact.kind != "stage_result":
                raise ProtocolError(
                    "candidate.invalid",
                    {"where": "result_record_artifact", "reason": "wrong-kind"},
                )
            _require_identifier(self.candidate_id, "candidate.invalid", "candidate_id")
            _require_identifier(self.claim_id, "candidate.invalid", "claim_id")
            if type(self.fence) is not int or self.fence <= 0:
                raise ProtocolError("candidate.invalid", {"where": "fence"})


@dataclass(frozen=True, slots=True)
class StageDecision:
    """The one committed business decision for a run and stage."""

    decision_id: str
    run_id: str
    stage: StageId
    command_id: str
    result_record_artifact: ArtifactRef | None
    result_digest: str | None
    actual_executor_sha256: str
    outcome: Outcome
    original_producer_decision_id: str | None
    reason_code: str | None
    failure_envelope: FailureEnvelope | None
    decided_at: datetime

    def __post_init__(self) -> None:
        _require_identifier(self.decision_id, "decision.invalid", "decision_id")
        _require_identifier(self.run_id, "decision.invalid", "run_id")
        _require_stage(self.stage, "decision.invalid", "stage")
        _require_identifier(self.command_id, "decision.invalid", "command_id")
        if self.result_record_artifact is not None and not isinstance(self.result_record_artifact, ArtifactRef):
            raise ProtocolError("decision.invalid", {"where": "result_record_artifact"})
        if self.result_record_artifact is not None and self.result_record_artifact.kind != "stage_result":
            raise ProtocolError("decision.invalid", {"where": "result_record_artifact", "reason": "wrong-kind"})
        if self.result_digest is not None:
            _require_digest(self.result_digest, "decision.invalid", "result_digest")
        _require_digest(self.actual_executor_sha256, "decision.invalid", "actual_executor_sha256")
        if self.outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("decision.invalid", {"where": "outcome", "value": self.outcome})
        if self.original_producer_decision_id is not None:
            _require_identifier(
                self.original_producer_decision_id,
                "decision.invalid",
                "original_producer_decision_id",
            )
        if self.outcome in {"failed", "cancelled"} and not self.reason_code:
            raise ProtocolError("decision.invalid", {"where": "reason_code"})
        if self.outcome == "completed":
            if self.result_record_artifact is None or self.result_digest is None or self.failure_envelope is not None:
                raise ProtocolError("decision.invalid", {"reason": "completed-result-required"})
        elif (self.result_record_artifact is None) == (self.failure_envelope is None):
            raise ProtocolError("decision.invalid", {"reason": "one-failure-representation-required"})
        elif self.result_record_artifact is None and self.result_digest is not None:
            raise ProtocolError("decision.invalid", {"reason": "orphan-result-digest"})
        elif self.result_record_artifact is not None and self.result_digest is None:
            raise ProtocolError("decision.invalid", {"reason": "missing-result-digest"})
        if self.failure_envelope is not None:
            if not isinstance(self.failure_envelope, FailureEnvelope):
                raise ProtocolError("decision.invalid", {"where": "failure_envelope"})
            if (
                self.failure_envelope.command_id != self.command_id
                or self.failure_envelope.run_id != self.run_id
                or self.failure_envelope.stage != self.stage
                or self.failure_envelope.outcome != self.outcome
                or self.failure_envelope.actual_executor_sha256 != self.actual_executor_sha256
                or self.failure_envelope.reason_code != self.reason_code
            ):
                raise ProtocolError("decision.invalid", {"where": "failure_envelope", "reason": "identity"})
        if not isinstance(self.decided_at, datetime) or self.decided_at.tzinfo is None:
            raise ProtocolError("decision.invalid", {"where": "decided_at"})


def effect_is_settled(state: object) -> bool:
    """Whether an effect may participate in a terminal stage decision."""

    # PREPARED proves that no send was authorized. It cannot support a
    # completed decision, but it is safe to record as not attempted beside a
    # definite failure.
    return state in {"prepared", "satisfied", "definitely_failed"}


def project_stage_status(
    *,
    decision_outcome: str | None,
    effect_states: tuple[str, ...],
    predecessor_completed: bool,
    open_claim: bool,
    pending_effect_states: tuple[str, ...] = (),
) -> StageStatus:
    """Derive the stage status; unknown external effects have precedence."""

    unresolved_effect_states = list(effect_states)
    for state in pending_effect_states:
        if state != "unknown" or "unknown" not in unresolved_effect_states:
            raise ProtocolError(
                "stage_status.invalid",
                {"where": "pending_effect_states", "state": state},
            )
        unresolved_effect_states.remove("unknown")
    if "unknown" in unresolved_effect_states or "conflict" in unresolved_effect_states:
        return StageStatus.UNRESOLVED
    if decision_outcome is not None:
        if decision_outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("stage_status.invalid", {"decision_outcome": decision_outcome})
        if decision_outcome == "completed" and any(state != "satisfied" for state in effect_states):
            return StageStatus.UNRESOLVED
        return StageStatus(decision_outcome)
    if not predecessor_completed:
        return StageStatus.BLOCKED
    if open_claim:
        return StageStatus.ACTIVE
    return StageStatus.WAITING


def project_run_status(
    *,
    stage_outcomes: tuple[str | None, ...],
    effect_states: tuple[str, ...],
    pending_effect_states: tuple[str, ...] = (),
) -> RunStatus:
    """Derive a run status from ordered stage decisions and effect facts."""

    if len(stage_outcomes) != len(STAGE_IDS):
        raise ProtocolError("run_status.invalid", {"reason": "six-stage-outcomes-required"})
    unresolved_effect_states = list(effect_states)
    for state in pending_effect_states:
        if state != "unknown" or "unknown" not in unresolved_effect_states:
            raise ProtocolError(
                "run_status.invalid",
                {"where": "pending_effect_states", "state": state},
            )
        unresolved_effect_states.remove("unknown")
    if "unknown" in unresolved_effect_states or "conflict" in unresolved_effect_states:
        return RunStatus.UNRESOLVED
    for outcome in stage_outcomes:
        if outcome is None:
            return RunStatus.INCOMPLETE
        if outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("run_status.invalid", {"stage_outcome": outcome})
        if outcome != "completed":
            return RunStatus(outcome)
    if any(state != "satisfied" for state in effect_states):
        return RunStatus.UNRESOLVED
    return RunStatus.SUCCEEDED
