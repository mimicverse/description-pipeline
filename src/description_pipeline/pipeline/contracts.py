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

STAGES = ("freeze", "discover", "capture", "generate", "verify", "publish")
StageId = Literal["freeze", "discover", "capture", "generate", "verify", "publish"]
Outcome = Literal["completed", "failed", "cancelled"]
EffectKind = Literal["airflow_dispatch", "native_operation", "git_ref_update", "pr_upsert"]

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
        if self.schema_version != "description-pipeline.run-admission/v1":
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
            "schema_version": "description-pipeline.run-admission/v1",
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
    if value not in STAGES:
        raise ProtocolError(code, {"where": where, "value": value})
    return value  # type: ignore[return-value]


def _freeze_mapping(value: Mapping[str, Any], code: str, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(code, {"where": where})
    source = dict(value)
    # Canonical encoding is both the JSON-safety check and the digest identity.
    try:
        canonical(source)
    except (TypeError, ValueError) as error:
        raise ProtocolError(code, {"where": where, "reason": str(error)}) from error

    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, (list, tuple)):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(source)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


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

    def __post_init__(self) -> None:
        if self.schema_version != "description-pipeline.stage-command/v1":
            raise ProtocolError("command.schema_unsupported", {"value": self.schema_version})
        _require_identifier(self.command_id, "command.invalid", "command_id")
        _require_identifier(self.run_id, "command.invalid", "run_id")
        if self.kind not in {"stage", "cancel", "reconcile"}:
            raise ProtocolError("command.invalid", {"where": "kind", "value": self.kind})
        if self.kind == "stage" or self.stage is not None:
            _require_stage(self.stage, "command.invalid", "stage")
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
        if digest(self.identity_record()) != self.request_digest:
            raise ProtocolError("command.digest_mismatch", {"command_id": self.command_id})

    def identity_record(self) -> dict[str, Any]:
        return {
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
    ) -> StageCommand:
        values = {
            "schema_version": "description-pipeline.stage-command/v1",
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
        }
        request_digest = digest(
            {
                "command_id": command_id,
                "idempotency_key": idempotency_key,
                "kind": values["kind"],
                "request": values["request"],
                "schema_version": values["schema_version"],
                "stage": values["stage"],
                "run_id": values["run_id"],
                "input_artifacts": [item.to_record() for item in input_artifacts],
                "config_snapshot_sha256": config_snapshot_sha256,
                "contract_sha256": contract_sha256,
                "policy_sha256": policy_sha256,
                "expected_executor_sha256": expected_executor_sha256,
                "predecessor_decision_id": predecessor_decision_id,
            }
        )
        return cls(request_digest=request_digest, **values)

    def to_record(self) -> dict[str, Any]:
        return {
            **self.identity_record(),
            "request_digest": self.request_digest,
        }


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
    required_effect_ids: tuple[str, ...] = ()
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "description-pipeline.stage-result/v1":
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

    def to_record(self) -> dict[str, Any]:
        return {
            "actual_executor_sha256": self.actual_executor_sha256,
            "checks_sha256": self.checks_sha256,
            "command_id": self.command_id,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "request_digest": self.request_digest,
            "required_effect_ids": list(self.required_effect_ids),
            "output_artifact": self.output_artifact.to_record(),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "stage": self.stage,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> StageResult:
        if not isinstance(record, Mapping):
            raise ProtocolError("result.invalid", {"reason": "not-an-object"})
        expected = {
            "actual_executor_sha256",
            "checks_sha256",
            "command_id",
            "outcome",
            "reason_code",
            "request_digest",
            "required_effect_ids",
            "output_artifact",
            "run_id",
            "schema_version",
            "stage",
        }
        if set(record) != expected:
            raise ProtocolError(
                "result.invalid",
                {"missing": sorted(expected - set(record)), "unknown": sorted(set(record) - expected)},
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
            required_effect_ids=tuple(required_effects),
            reason_code=record["reason_code"],
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
        super().__init__(message)
        self.reason_code = reason_code
        self.outcome = outcome
        self.evidence_artifacts = evidence_artifacts
        self.unavailable_reason = unavailable_reason


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
    effect_states: tuple[tuple[str, str], ...]
    evidence_artifacts: tuple[ArtifactRef, ...]
    unavailable_reason: str | None
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != "description-pipeline.failure-envelope/v1":
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
        if self.unavailable_reason is not None:
            _require_text(self.unavailable_reason, "failure.invalid", "unavailable_reason")
        if not isinstance(self.occurred_at, datetime) or self.occurred_at.tzinfo is None:
            raise ProtocolError("failure.invalid", {"where": "occurred_at"})

    def to_record(self) -> dict[str, Any]:
        return {
            "actual_executor_sha256": self.actual_executor_sha256,
            "command_id": self.command_id,
            "effect_states": [list(row) for row in self.effect_states],
            "error_message": self.error_message,
            "error_type": self.error_type,
            "evidence_artifacts": [item.to_record() for item in self.evidence_artifacts],
            "occurred_at": self.occurred_at.isoformat(),
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "request_digest": self.request_digest,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "stage": self.stage,
            "unavailable_reason": self.unavailable_reason,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> FailureEnvelope:
        if not isinstance(record, Mapping):
            raise ProtocolError("failure.invalid", {"reason": "not-an-object"})
        expected = {
            "actual_executor_sha256",
            "command_id",
            "effect_states",
            "error_message",
            "error_type",
            "evidence_artifacts",
            "occurred_at",
            "outcome",
            "reason_code",
            "request_digest",
            "run_id",
            "schema_version",
            "stage",
            "unavailable_reason",
        }
        if set(record) != expected:
            raise ProtocolError(
                "failure.invalid",
                {"missing": sorted(expected - set(record)), "unknown": sorted(set(record) - expected)},
            )
        effect_rows = record["effect_states"]
        evidence = record["evidence_artifacts"]
        if not isinstance(effect_rows, list) or not isinstance(evidence, list):
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
            effect_states=tuple(tuple(row) for row in effect_rows),
            evidence_artifacts=tuple(ArtifactRef.from_record(item) for item in evidence),
            unavailable_reason=record["unavailable_reason"],
            occurred_at=occurred_at,
        )

    @property
    def digest(self) -> str:
        return digest(self.to_record())


@dataclass(frozen=True, slots=True)
class StageExecution:
    """A domain handler result before its artifact closure is sealed."""

    outcome: Outcome
    candidate_root: str
    checks_sha256: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("execution.invalid", {"where": "outcome", "value": self.outcome})
        _require_text(self.candidate_root, "execution.invalid", "candidate_root")
        _require_digest(self.checks_sha256, "execution.invalid", "checks_sha256")
        if self.outcome in {"failed", "cancelled"} and not self.reason_code:
            raise ProtocolError("execution.invalid", {"where": "reason_code"})
        if self.outcome == "completed" and self.reason_code is not None:
            raise ProtocolError("execution.invalid", {"where": "reason_code", "reason": "completed"})


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
) -> StageStatus:
    """Derive the stage status; unknown external effects have precedence."""

    if "unknown" in effect_states or "conflict" in effect_states:
        return StageStatus.UNRESOLVED
    if decision_outcome is not None:
        if decision_outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("stage_status.invalid", {"decision_outcome": decision_outcome})
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
) -> RunStatus:
    """Derive a run status from ordered stage decisions and effect facts."""

    if len(stage_outcomes) != len(STAGES):
        raise ProtocolError("run_status.invalid", {"reason": "six-stage-outcomes-required"})
    if "unknown" in effect_states or "conflict" in effect_states:
        return RunStatus.UNRESOLVED
    for outcome in stage_outcomes:
        if outcome is None:
            return RunStatus.INCOMPLETE
        if outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("run_status.invalid", {"stage_outcome": outcome})
        if outcome != "completed":
            return RunStatus(outcome)
    return RunStatus.SUCCEEDED
