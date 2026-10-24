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


class ProtocolError(PipelineError):
    """A canonical pipeline fact violates the shared protocol."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


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
        _require_text(self.artifact_id, "artifact.invalid", "artifact_id")
        _require_text(self.kind, "artifact.invalid", "kind")
        _require_text(self.locator, "artifact.invalid", "locator")
        _require_digest(self.manifest_sha256, "artifact.invalid", "manifest_sha256")
        _require_digest(self.set_sha256, "artifact.invalid", "set_sha256")
        if self.producer_run_id is not None:
            _require_text(self.producer_run_id, "artifact.invalid", "producer_run_id")
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
        _require_text(self.command_id, "command.invalid", "command_id")
        _require_text(self.run_id, "command.invalid", "run_id")
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
    result_artifact: ArtifactRef
    actual_executor_sha256: str
    checks_sha256: str
    required_effect_ids: tuple[str, ...] = ()
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "description-pipeline.stage-result/v1":
            raise ProtocolError("result.schema_unsupported", {"value": self.schema_version})
        _require_text(self.command_id, "result.invalid", "command_id")
        _require_text(self.run_id, "result.invalid", "run_id")
        _require_stage(self.stage, "result.invalid", "stage")
        _require_digest(self.request_digest, "result.invalid", "request_digest")
        if self.outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("result.invalid", {"where": "outcome", "value": self.outcome})
        if not isinstance(self.result_artifact, ArtifactRef):
            raise ProtocolError("result.invalid", {"where": "result_artifact"})
        _require_digest(self.actual_executor_sha256, "result.invalid", "actual_executor_sha256")
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
            "result_artifact": self.result_artifact.to_record(),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "stage": self.stage,
        }

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
    lane_key: str | None
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.claim_id, "claim.invalid", "claim_id")
        _require_text(self.command_id, "claim.invalid", "command_id")
        if type(self.fence) is not int or self.fence <= 0:
            raise ProtocolError("claim.invalid", {"where": "fence"})
        _require_digest(self.owner_sha256, "claim.invalid", "owner_sha256")
        if self.lane_key is not None:
            _require_text(self.lane_key, "claim.invalid", "lane_key")
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise ProtocolError("claim.invalid", {"where": "expires_at"})


@dataclass(frozen=True, slots=True)
class StageExecution:
    """A domain handler result before its artifact closure is sealed."""

    outcome: Outcome
    candidate_root: str
    checks_sha256: str
    required_effect_ids: tuple[str, ...] = ()
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("execution.invalid", {"where": "outcome", "value": self.outcome})
        _require_text(self.candidate_root, "execution.invalid", "candidate_root")
        _require_digest(self.checks_sha256, "execution.invalid", "checks_sha256")
        if not isinstance(self.required_effect_ids, tuple) or not all(
            isinstance(value, str) and value for value in self.required_effect_ids
        ):
            raise ProtocolError("execution.invalid", {"where": "required_effect_ids"})
        if self.outcome in {"failed", "cancelled"} and not self.reason_code:
            raise ProtocolError("execution.invalid", {"where": "reason_code"})
        if self.outcome == "completed" and self.reason_code is not None:
            raise ProtocolError("execution.invalid", {"where": "reason_code", "reason": "completed"})


@dataclass(frozen=True, slots=True)
class StageCandidate:
    """A sealed stage result not yet committed as a decision."""

    result: StageResult

    def __post_init__(self) -> None:
        if not isinstance(self.result, StageResult):
            raise ProtocolError("candidate.invalid", {"where": "result"})


@dataclass(frozen=True, slots=True)
class StageDecision:
    """The one committed business decision for a run and stage."""

    decision_id: str
    run_id: str
    stage: StageId
    command_id: str
    result_artifact: ArtifactRef
    result_digest: str
    actual_executor_sha256: str
    outcome: Outcome
    original_producer_decision_id: str | None
    reason_code: str | None
    decided_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.decision_id, "decision.invalid", "decision_id")
        _require_text(self.run_id, "decision.invalid", "run_id")
        _require_stage(self.stage, "decision.invalid", "stage")
        _require_text(self.command_id, "decision.invalid", "command_id")
        if not isinstance(self.result_artifact, ArtifactRef):
            raise ProtocolError("decision.invalid", {"where": "result_artifact"})
        _require_digest(self.result_digest, "decision.invalid", "result_digest")
        _require_digest(self.actual_executor_sha256, "decision.invalid", "actual_executor_sha256")
        if self.outcome not in {"completed", "failed", "cancelled"}:
            raise ProtocolError("decision.invalid", {"where": "outcome", "value": self.outcome})
        if self.original_producer_decision_id is not None:
            _require_text(self.original_producer_decision_id, "decision.invalid", "original_producer_decision_id")
        if self.outcome in {"failed", "cancelled"} and not self.reason_code:
            raise ProtocolError("decision.invalid", {"where": "reason_code"})
        if not isinstance(self.decided_at, datetime) or self.decided_at.tzinfo is None:
            raise ProtocolError("decision.invalid", {"where": "decided_at"})


def effect_is_settled(state: object) -> bool:
    """Whether an effect may participate in a terminal stage decision."""

    return state in {"satisfied", "definitely_failed"}


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
