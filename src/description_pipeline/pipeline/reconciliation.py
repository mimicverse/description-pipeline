"""Claim-free, read-only reconciliation of one unresolved external effect."""

from __future__ import annotations

import contextlib
import tempfile
import shutil
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ..io import canonical, process_error_record, read_data, write_json
from ..schemas import (
    EFFECT_RECONCILIATION_FAILURE_SCHEMA,
    EFFECT_RECONCILIATION_SCHEMA,
)
from .contracts import ArtifactRef, ProtocolError, StageCommand
from .effects import (
    EffectFact,
    EffectIntent,
    EffectResumePhase,
    EffectSettlement,
    EffectState,
    native_transfer_target_digest,
    require_native_transfer_target,
)

if TYPE_CHECKING:
    from ..artifacts import ArtifactStore
    from ..platform.facts import FactStore

_STATE_CLASSES = frozenset({"known", "absent", "unobservable"})


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ProtocolError("reconciliation.invalid", {"where": "payload.keys"})
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def reconciliation_metadata_document(
    *,
    command: StageCommand,
    intent: EffectIntent,
    fact_type: str,
    state_class: str,
    metadata: Mapping[str, Any] | None,
    outbound_artifact: ArtifactRef,
    payload_artifact: ArtifactRef,
    observer_tool_sha256: str,
    observed_at: datetime,
    resume_phase: EffectResumePhase | None = None,
    resume_target_digest: str | None = None,
) -> dict[str, Any]:
    """Construct the one immutable reconciliation metadata closure."""

    record = {
        "schema_version": EFFECT_RECONCILIATION_SCHEMA,
        "command_id": command.command_id,
        "effect_id": intent.effect_id,
        "effect_key": intent.effect_key,
        "fact_type": fact_type,
        "state_class": state_class,
        "metadata": _plain(metadata or {}),
        "outbound_artifact": outbound_artifact.to_record(),
        "payload_artifact": payload_artifact.to_record(),
        "observer_tool_sha256": observer_tool_sha256,
        "observed_at": observed_at.isoformat(),
    }
    if resume_phase is not None:
        record["resume_phase"] = resume_phase.value
        record["resume_target_digest"] = resume_target_digest
    return record


def read_reconciliation_metadata(
    artifacts,
    metadata_artifact: ArtifactRef,
    *,
    command: StageCommand,
    intent: EffectIntent,
    fact_type: str,
    state_class: str,
    outbound_artifact: ArtifactRef,
    payload_artifact: ArtifactRef,
    observed_at: datetime,
    resume_phase: EffectResumePhase | None = None,
    resume_target_digest: str | None = None,
) -> dict[str, Any]:
    """Read and exactly bind reconciliation metadata to one effect closure."""

    if metadata_artifact.kind != "effect_reconciliation_metadata":
        raise ProtocolError("reconciliation.metadata_invalid", {"where": "kind"})
    if getattr(artifacts, "verify", None) is None or getattr(artifacts, "materialize", None) is None:
        raise ProtocolError("reconciliation.metadata_invalid", {"where": "artifact-reader"})
    artifacts.verify(metadata_artifact)
    with tempfile.TemporaryDirectory(prefix="effect-reconciliation-metadata-") as directory:
        root = artifacts.materialize(
            metadata_artifact,
            Path(directory) / "metadata",
        )
        files = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }
        if files != {"manifest.json", "metadata.json"} or any(path.is_symlink() for path in root.rglob("*")):
            raise ProtocolError("reconciliation.metadata_invalid", {"where": "members"})
        record = read_data(root / "metadata.json")
    expected = reconciliation_metadata_document(
        command=command,
        intent=intent,
        fact_type=fact_type,
        state_class=state_class,
        metadata=record.get("metadata") if isinstance(record, dict) else None,
        outbound_artifact=outbound_artifact,
        payload_artifact=payload_artifact,
        observer_tool_sha256=metadata_artifact.producer_tool_sha256,
        observed_at=observed_at,
        resume_phase=resume_phase,
        resume_target_digest=resume_target_digest,
    )
    expected_keys = {
        "schema_version",
        "command_id",
        "effect_id",
        "effect_key",
        "fact_type",
        "state_class",
        "metadata",
        "outbound_artifact",
        "payload_artifact",
        "observer_tool_sha256",
        "observed_at",
    }
    if resume_phase is not None:
        expected_keys.update({"resume_phase", "resume_target_digest"})
    if (
        not isinstance(record, dict)
        or set(record) != expected_keys
        or record != expected
        or metadata_artifact.producer_run_id != command.run_id
        or metadata_artifact.producer_stage != command.stage
        or len(metadata_artifact.producer_tool_sha256) != 64
    ):
        raise ProtocolError("reconciliation.metadata_mismatch", {"effect_id": intent.effect_id})
    return record


@dataclass(frozen=True, slots=True)
class EffectReconciliationEvidence:
    """One complete domain evidence tree or an already registered payload artifact."""

    fact_type: Literal["observation", "reconciled"]
    state_class: Literal["known", "absent", "unobservable"]
    payload_root: Path | None = None
    payload_artifact: ArtifactRef | None = None
    metadata: Mapping[str, Any] | None = None
    resume_phase: EffectResumePhase | None = None
    resume_target_digest: str | None = None

    def __post_init__(self) -> None:
        if self.fact_type not in {"observation", "reconciled"}:
            raise ProtocolError("reconciliation.invalid", {"where": "fact_type"})
        if self.state_class not in _STATE_CLASSES:
            raise ProtocolError("reconciliation.invalid", {"where": "state_class"})
        if self.resume_phase is not None:
            if not isinstance(self.resume_phase, EffectResumePhase):
                raise ProtocolError("reconciliation.invalid", {"where": "resume_phase"})
            if self.fact_type != "observation" or self.state_class != "unobservable":
                raise ProtocolError(
                    "reconciliation.invalid",
                    {"where": "resume_phase", "reason": "nonterminal-observation-required"},
                )
            if (
                not isinstance(self.resume_target_digest, str)
                or len(self.resume_target_digest) != 64
                or any(character not in "0123456789abcdef" for character in self.resume_target_digest)
            ):
                raise ProtocolError("reconciliation.invalid", {"where": "resume_target_digest"})
        elif self.resume_target_digest is not None:
            raise ProtocolError(
                "reconciliation.invalid",
                {"where": "resume_target_digest", "reason": "phase-required"},
            )
        if (self.payload_root is None) == (self.payload_artifact is None):
            raise ProtocolError("reconciliation.invalid", {"reason": "one-payload-required"})
        if self.payload_root is not None and not isinstance(self.payload_root, Path):
            raise ProtocolError("reconciliation.invalid", {"where": "payload_root"})
        if self.payload_artifact is not None and not isinstance(self.payload_artifact, ArtifactRef):
            raise ProtocolError("reconciliation.invalid", {"where": "payload_artifact"})
        if self.metadata is not None and not isinstance(self.metadata, Mapping):
            raise ProtocolError("reconciliation.invalid", {"where": "metadata"})
        try:
            canonical(dict(self.metadata or {}))
        except (TypeError, ValueError) as error:
            raise ProtocolError("reconciliation.invalid", {"where": "metadata"}) from error


class EffectReconciliationAdapter(Protocol):
    """A read-only domain view of the original external target."""

    def observe(
        self,
        *,
        command: StageCommand,
        intent: EffectIntent,
        outbound_root: Path,
        workspace_root: Path,
    ) -> EffectReconciliationEvidence: ...


@dataclass(frozen=True, slots=True)
class EffectReconciliationResult:
    command_id: str
    effect_id: str
    effect_key: str
    fact: EffectFact | None
    settlement: EffectSettlement
    resume_phase: EffectResumePhase | None = None


class EffectReconciliationFailure(ProtocolError):
    """A failed read-only reconciliation retained its complete working tree."""

    def __init__(self, cause: Exception, retained_workspace: Path) -> None:
        diagnostic = process_error_record(cause, operation="effect.reconciliation")
        super().__init__(
            "reconciliation.failed",
            {
                "error_type": type(cause).__name__,
                "error_message": diagnostic["message"],
                "retained_workspace": str(retained_workspace),
            },
        )
        self.cause = cause
        self.retained_workspace = retained_workspace


class EffectReconciliationService:
    """Append read-only evidence so the original engine can acquire a new claim."""

    def __init__(
        self,
        *,
        facts: FactStore,
        artifacts: ArtifactStore,
        work_root: str | Path,
        observer_tool_sha256: str,
        clock: Callable[[], datetime],
    ) -> None:
        if (
            not isinstance(observer_tool_sha256, str)
            or len(observer_tool_sha256) != 64
            or any(character not in "0123456789abcdef" for character in observer_tool_sha256)
        ):
            raise ProtocolError("reconciliation.invalid", {"where": "observer_tool_sha256"})
        root = Path(work_root)
        if root.exists() and (root.is_symlink() or root.is_junction()):
            raise ProtocolError("reconciliation.workspace_invalid", {"root": str(root)})
        root.mkdir(parents=True, exist_ok=True)
        self.facts = facts
        self.artifacts = artifacts
        self.work_root = root.resolve()
        self.observer_tool_sha256 = observer_tool_sha256
        self.clock = clock

    def reconcile(
        self,
        *,
        command_id: str,
        effect_key: str,
        adapter: EffectReconciliationAdapter,
    ) -> EffectReconciliationResult:
        command = self.facts.load_command(command_id)
        settlement = self.facts.effect_settlement_for_key(command_id, effect_key)
        intent = self.facts.effect_intent_for_key(command_id, effect_key)
        if settlement is None or intent is None:
            raise ProtocolError("reconciliation.effect_missing", {"command_id": command_id, "effect_key": effect_key})
        if settlement.state in {
            EffectState.SATISFIED,
            EffectState.DEFINITELY_FAILED,
        }:
            return EffectReconciliationResult(
                command_id=command_id,
                effect_id=intent.effect_id,
                effect_key=effect_key,
                fact=None,
                settlement=settlement,
            )
        if settlement.state not in {EffectState.UNKNOWN, EffectState.CONFLICT}:
            raise ProtocolError(
                "reconciliation.not_required",
                {"state": settlement.state.value, "effect_id": intent.effect_id},
            )
        if settlement.outbound_artifact is None:
            raise ProtocolError("reconciliation.outbound_missing", {"effect_id": intent.effect_id})
        self.artifacts.verify(settlement.outbound_artifact)
        workspace = Path(tempfile.mkdtemp(prefix="effect-reconcile-", dir=self.work_root))
        persisted = False
        try:
            outbound = self.artifacts.materialize(
                settlement.outbound_artifact,
                workspace / "outbound",
            )
            evidence = adapter.observe(
                command=command,
                intent=intent,
                outbound_root=outbound,
                workspace_root=workspace,
            )
            if not isinstance(evidence, EffectReconciliationEvidence):
                raise ProtocolError("reconciliation.invalid", {"where": "adapter-result"})
            if evidence.resume_phase is not None and intent.effect_kind != "native_operation":
                raise ProtocolError(
                    "reconciliation.phase_not_allowed",
                    {"effect_id": intent.effect_id, "effect_kind": intent.effect_kind},
                )
            if evidence.resume_phase is not None:
                require_native_transfer_target(intent.target)
            if (
                evidence.resume_phase is not None
                and evidence.resume_target_digest != native_transfer_target_digest(intent.target)
            ):
                raise ProtocolError(
                    "reconciliation.target_mismatch",
                    {"effect_id": intent.effect_id},
                )
            if settlement.state is EffectState.CONFLICT and (
                evidence.fact_type != "reconciled"
                or evidence.state_class not in {"known", "absent"}
            ):
                raise ProtocolError(
                    "reconciliation.conflict_unresolved",
                    {
                        "effect_id": intent.effect_id,
                        "fact_type": evidence.fact_type,
                        "state_class": evidence.state_class,
                    },
                )
            observed_at = self._now()
            if evidence.payload_artifact is not None:
                self.artifacts.verify(evidence.payload_artifact)
                payload = evidence.payload_artifact
            else:
                payload_root = evidence.payload_root.resolve()
                if (
                    not payload_root.is_dir()
                    or payload_root.is_symlink()
                    or not payload_root.is_relative_to(workspace)
                ):
                    raise ProtocolError(
                        "reconciliation.invalid",
                        {"where": "payload_root", "reason": "outside-service-workspace"},
                    )
                payload = self.artifacts.seal_tree(
                    payload_root,
                    kind="effect_observation",
                    producer_run_id=command.run_id,
                    producer_stage=command.stage,
                    producer_tool_sha256=self.observer_tool_sha256,
                )
            metadata_root = workspace / "reconciliation-metadata"
            write_json(
                metadata_root / "metadata.json",
                reconciliation_metadata_document(
                    command=command,
                    intent=intent,
                    fact_type=evidence.fact_type,
                    state_class=evidence.state_class,
                    metadata=evidence.metadata,
                    outbound_artifact=settlement.outbound_artifact,
                    payload_artifact=payload,
                    observer_tool_sha256=self.observer_tool_sha256,
                    observed_at=observed_at,
                resume_phase=evidence.resume_phase,
                resume_target_digest=evidence.resume_target_digest,
                ),
            )
            metadata = self.artifacts.seal_tree(
                metadata_root,
                kind="effect_reconciliation_metadata",
                producer_run_id=command.run_id,
                producer_stage=command.stage,
                producer_tool_sha256=self.observer_tool_sha256,
            )
            fact = self.facts.append_reconciliation_fact(
                effect_id=intent.effect_id,
                fact_type=evidence.fact_type,
                source="effect-reconciler:" + self.observer_tool_sha256,
                state_class=evidence.state_class,
                payload_artifact=payload,
                metadata_artifact=metadata,
                payload_digest=payload.set_sha256,
                observed_at=observed_at,
                fact_id="fact-" + str(uuid.uuid4()),
                expected_command_id=command.command_id,
                resume_phase=evidence.resume_phase,
                resume_target_digest=evidence.resume_target_digest,
            )
            persisted = True
            final = self.facts.effect_settlement(
                intent.effect_id,
                expected_command_id=command.command_id,
            )
        except Exception as error:
            if not persisted:
                self._retain_failure(workspace, error)
                raise EffectReconciliationFailure(error, workspace) from None
            raise
        finally:
            if persisted:
                shutil.rmtree(workspace, ignore_errors=True)
        return EffectReconciliationResult(
            command_id=command_id,
            effect_id=intent.effect_id,
            effect_key=effect_key,
            fact=fact,
            settlement=final,
            resume_phase=final.resume_phase,
        )

    @staticmethod
    def _retain_failure(workspace: Path, error: Exception) -> None:
        with contextlib.suppress(Exception):
            diagnostic = process_error_record(error, operation="effect.reconciliation")
            write_json(
                workspace / "reconciliation-failure.json",
                {
                    "schema_version": EFFECT_RECONCILIATION_FAILURE_SCHEMA,
                    "error_type": type(error).__name__,
                    "error_message": diagnostic["message"],
                    "error_detail": diagnostic,
                    "workspace": str(workspace),
                },
            )

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ProtocolError("clock.invalid", {"where": "effect_reconciliation"})
        return value


__all__ = [
    "EffectReconciliationAdapter",
    "EffectReconciliationEvidence",
    "EffectReconciliationFailure",
    "EffectReconciliationResult",
    "EffectReconciliationService",
    "read_reconciliation_metadata",
    "reconciliation_metadata_document",
]
