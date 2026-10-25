"""The single production application service for the six engineering stages."""

from __future__ import annotations

import os
import json
import stat
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, get_args

from ..artifacts import ArtifactStore
from ..interface_records import validate_config_snapshot
from ..io import PipelineError, digest, read_data
from ..platform.facts import FactConflict, FactStore
from ..orchestration.native_operation_client import (
    NativeOperationClient,
    NativeOperationDiscoverPort,
    NativeOperationEffectObserver,
    NativeOperationEndpoint,
    NativeOperationToolProvider,
    NativeRemoteDiscoverAdapter,
    capture_handler_from_native_operation,
)
from ..repository.publish import REPOSITORY_SNAPSHOT_METADATA
from ..runtime import tool_record
from ..schemas import AIRFLOW_DISPATCH_CONF_SCHEMA
from .admission import (
    AdmissionSpec,
    VerifiedArtifact,
    VerifiedDiscoverResult,
    VerifiedPredecessor,
    VerifiedPublishIntent,
    VerifiedRepositorySnapshot,
    VerifiedRuleset,
    VerifiedVerifyResult,
    build_capture_command,
    build_discover_command,
    build_freeze_command,
    build_generate_command,
    build_publish_command,
    build_run_admission,
    build_verify_command,
    validate_run_admission,
    validate_stage_command,
)
from .contracts import (
    ArtifactRef,
    ExecutionClaim,
    DiagnosticReplayScope,
    DispatchCommand,
    DispatchConf,
    EffectKind,
    FailureEnvelope,
    NativeTargetIdentity,
    RunAdmission,
    StageCommand,
    StageDecision,
    StageEffectWait,
    StageId,
    StagePending,
    StageResult,
)
from .discover import DiscoverDomainAdapter, DiscoverHandler
from .dispatch import DispatchAdapter, DispatchResult, TechnicalDispatchService
from .effects import EffectIntent
from .engine import StageAttemptResult, StageHandler, StageServices, execute_stage
from .freeze import FreezeHandler
from .native_target import load_registered_native_target
from .services import CanonicalStageServices, ClaimPolicy
from .reconciliation import (
    EffectReconciliationAdapter,
    EffectReconciliationResult,
    EffectReconciliationService,
)
from .stages.generate import GenerateHandler
from .stages.publish import PublishHandler
from .verify import VerifyHandler
from .verify_contract import CERTIFICATE_FILENAME

STAGES: tuple[StageId, ...] = (
    "freeze",
    "discover",
    "capture",
    "generate",
    "verify",
    "publish",
)
_EFFECT_KINDS = frozenset(get_args(EffectKind))
_REQUIRED_RECONCILIATION_KINDS = (
    "native_operation",
    "git_ref_update",
    "pr_upsert",
)


class ApplicationError(PipelineError):
    def __init__(self, code: str, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})


class ApplicationPortMissing(ApplicationError):
    """The production composition lacks a required stage capability."""


@dataclass(frozen=True, slots=True)
class AirflowTaskBinding:
    """The exact Airflow task currently executing one stage command."""

    dag_id: str
    dag_run_id: str
    task_id: str
    map_index: int

    def __post_init__(self) -> None:
        for name in ("dag_id", "dag_run_id", "task_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ApplicationError(
                    "application.airflow_binding_invalid",
                    f"Airflow task binding has an invalid {name}",
                )
        if type(self.map_index) is not int or self.map_index < -1:
            raise ApplicationError(
                "application.airflow_binding_invalid",
                "Airflow map_index must be -1 for an unmapped task or a non-negative index",
            )


@dataclass(frozen=True, slots=True)
class EffectWakeupHint:
    """One non-authoritative wake-up observation for a canonical effect wait."""

    wait_digest: str
    observed_at: datetime
    changed: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.wait_digest, str)
            or len(self.wait_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.wait_digest)
        ):
            raise ApplicationError(
                "application.effect_wakeup_invalid",
                "Effect wake-up digest is invalid",
            )
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise ApplicationError(
                "application.effect_wakeup_invalid",
                "Effect wake-up time must be timezone-aware",
            )
        if type(self.changed) is not bool:
            raise ApplicationError(
                "application.effect_wakeup_invalid",
                "Effect wake-up changed flag must be boolean",
            )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _portable_executor_sha256() -> str:
    record = tool_record(role="portable")
    value = record.get("source_sha256")
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ApplicationError(
            "application.executor_invalid",
            "Portable runtime record carries no source identity",
        )
    return value


def _native_client_from_environment(
    source: Mapping[str, str],
    *,
    target_id: str,
) -> NativeOperationClient:
    """Build the loopback client without contacting the Windows endpoint."""

    base_url = source.get("SOLIDWORKS_ENDPOINT_URL")
    if not base_url:
        port_text = source.get("TUNNEL_LOOPBACK_PORT")
        if not port_text:
            raise ApplicationError(
                "application.native_endpoint_missing",
                "SOLIDWORKS_ENDPOINT_URL or TUNNEL_LOOPBACK_PORT is required",
            )
        try:
            port = int(port_text)
        except ValueError as error:
            raise ApplicationError(
                "application.native_endpoint_invalid",
                "TUNNEL_LOOPBACK_PORT must be a decimal port",
            ) from error
        if not 1 <= port <= 65535:
            raise ApplicationError(
                "application.native_endpoint_invalid",
                "TUNNEL_LOOPBACK_PORT is outside the port range",
            )
        base_url = f"http://127.0.0.1:{port}"

    token_value = source.get("ENDPOINT_TOKEN_FILE")
    if not token_value:
        raise ApplicationError(
            "application.native_token_missing",
            "ENDPOINT_TOKEN_FILE is required",
        )
    token_path = Path(token_value)
    if not token_path.is_absolute():
        raise ApplicationError(
            "application.native_token_invalid",
            "ENDPOINT_TOKEN_FILE must be absolute",
        )
    if token_path.is_symlink() or not token_path.is_file():
        raise ApplicationError(
            "application.native_token_invalid",
            "ENDPOINT_TOKEN_FILE must be a regular file",
        )
    if os.name != "nt" and stat.S_IMODE(token_path.stat().st_mode) & 0o077:
        raise ApplicationError(
            "application.native_token_invalid",
            "ENDPOINT_TOKEN_FILE must not grant group or other access",
        )
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        endpoint = NativeOperationEndpoint(base_url=base_url, token=token)
    except (OSError, UnicodeError, ValueError) as error:
        raise ApplicationError(
            "application.native_endpoint_invalid",
            str(error),
        ) from error
    return NativeOperationClient(
        endpoint,
        target_id=target_id,
    )


@contextmanager
def _shared_maintenance_gate(snapshot_path: Path):
    """Reject transfer while deployment maintenance holds its exact exclusive lock."""

    if (
        not snapshot_path.is_absolute()
        or not snapshot_path.is_file()
        or snapshot_path.is_symlink()
        or any(parent.is_symlink() for parent in snapshot_path.parents)
    ):
        raise ApplicationPortMissing(
            "application.maintenance_gate_source_missing",
            "PLATFORM_CONFIG_SNAPSHOT must name the active regular snapshot",
        )
    snapshot = validate_config_snapshot(read_data(snapshot_path))
    state_root = Path(snapshot["resolved"]["storage"]["state_root"])
    lock_directory = state_root / "locks"
    lock_path = lock_directory / "maintenance.lock"
    if (
        not state_root.is_dir()
        or state_root.is_symlink()
        or any(parent.is_symlink() for parent in state_root.parents)
        or not lock_directory.is_dir()
        or lock_directory.is_symlink()
        or not lock_path.is_file()
        or lock_path.is_symlink()
    ):
        raise ApplicationPortMissing(
            "application.maintenance_gate_missing",
            "The active deployment maintenance lock is not installed",
        )
    try:
        import fcntl
    except ImportError as error:
        raise ApplicationPortMissing(
            "application.maintenance_gate_unsupported",
            "Transfer continuation requires POSIX advisory locking",
        ) from error
    descriptor = os.open(
        lock_path,
        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        try:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_SH | fcntl.LOCK_NB,
            )
        except BlockingIOError as error:
            raise ApplicationPortMissing(
                "application.maintenance_gate_busy",
                "Deployment maintenance currently owns the exclusive lock",
            ) from error
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _transfer_resume_gate_from_environment(
    source: Mapping[str, str],
):
    snapshot_value = source.get("PLATFORM_CONFIG_SNAPSHOT")
    snapshot_path = Path(snapshot_value) if snapshot_value else None

    def gate(_command: StageCommand, _claim: ExecutionClaim):
        if snapshot_path is None:
            raise ApplicationPortMissing(
                "application.maintenance_gate_source_missing",
                "PLATFORM_CONFIG_SNAPSHOT is required for transfer continuation",
            )
        return _shared_maintenance_gate(snapshot_path)

    return gate


def _registered_native_target_from_environment(
    source: Mapping[str, str],
) -> NativeTargetIdentity:
    snapshot_value = source.get("PLATFORM_CONFIG_SNAPSHOT")
    if not snapshot_value:
        raise ApplicationPortMissing(
            "application.native_target_missing",
            "PLATFORM_CONFIG_SNAPSHOT is required to load the registered native target",
        )
    try:
        return load_registered_native_target(snapshot_value)
    except PipelineError as error:
        raise ApplicationError(
            "application.native_target_invalid",
            str(error),
        ) from error


class AdmissionFactPort(Protocol):
    def load_admission(self, run_id: str) -> RunAdmission | None: ...

    def load_admission_by_request_id(self, request_id: str) -> RunAdmission | None: ...

    def admit_run(
        self,
        admission: RunAdmission,
        command: StageCommand,
        dispatch_command: DispatchCommand,
        dispatch_outbound_artifact: ArtifactRef,
    ) -> tuple[StageCommand, DispatchCommand, EffectIntent]: ...

    def load_command(self, command_id: str) -> StageCommand: ...

    def load_dispatch_command(self, command_id: str) -> DispatchCommand: ...

    def create_command(
        self,
        command: StageCommand,
        *,
        admission: RunAdmission,
        predecessor: VerifiedPredecessor,
        supporting_predecessors: tuple[VerifiedPredecessor, ...] = (),
        discover_result: VerifiedDiscoverResult | None = None,
        verify_result: VerifiedVerifyResult | None = None,
        publish_intent: VerifiedPublishIntent | None = None,
    ) -> StageCommand: ...

    def decision_for(self, command: StageCommand) -> StageDecision | None: ...

    def load_result(self, decision: StageDecision) -> StageResult: ...

    def effect_intent_for_key(self, command_id: str, effect_key: str) -> EffectIntent | None: ...


class CancellationControlPort(Protocol):
    def send(self, *, cancel_command_id: str, target_effect_id: str): ...


@dataclass(frozen=True, slots=True)
class CommandBuildContext:
    """All verified facts a deterministic successor builder may consume."""

    admission: AdmissionSpec
    stage: StageId
    predecessors: Mapping[StageId, VerifiedPredecessor]
    discover_result: VerifiedDiscoverResult | None = None
    verify_result: VerifiedVerifyResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "predecessors", MappingProxyType(dict(self.predecessors)))

    def predecessor(self, stage: StageId) -> VerifiedPredecessor:
        value = self.predecessors.get(stage)
        if value is None:
            raise ApplicationError(
                "application.predecessor_missing",
                f"{self.stage} requires completed {stage}",
                detail={"stage": self.stage, "predecessor": stage},
            )
        return value


CommandBuilder = Callable[[CommandBuildContext], StageCommand]


def _default_builders() -> dict[StageId, CommandBuilder]:
    return {
        "freeze": lambda context: build_freeze_command(context.admission),
        "discover": lambda context: build_discover_command(
            context.admission,
            context.predecessor("freeze"),
        ),
        "capture": lambda context: build_capture_command(
            context.admission,
            context.predecessor("freeze"),
            context.predecessor("discover"),
            context.discover_result
            if context.discover_result is not None
            else _missing_discover_result(context.stage),
        ),
        "generate": lambda context: build_generate_command(
            context.admission,
            context.predecessor("capture"),
        ),
        "verify": lambda context: build_verify_command(
            context.admission,
            context.predecessor("generate"),
            context.predecessor("capture"),
        ),
        "publish": lambda context: build_publish_command(
            context.admission,
            context.verify_result
            if context.verify_result is not None
            else _missing_verify_result(context.stage),
            context.admission.publish_intent,
        ),
    }


def _missing_discover_result(stage: StageId) -> VerifiedDiscoverResult:
    raise ApplicationError(
        "application.discover_result_missing",
        "Capture requires the verified Discover result",
        detail={"stage": stage},
    )


def _missing_verify_result(stage: StageId) -> VerifiedVerifyResult:
    raise ApplicationError(
        "application.verify_result_missing",
        "Publish requires a verified certificate",
        detail={"stage": stage},
    )


@dataclass(frozen=True, slots=True)
class ApplicationOutcome:
    """JSON-safe projection returned by the application adapters."""

    run_id: str
    stage: StageId | None
    outcome: str
    payload: dict[str, Any]
    missing_capabilities: dict[str, list[str]]

    def to_record(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "stage": self.stage,
            "outcome": self.outcome,
            "payload": self.payload,
            "missing_capabilities": self.missing_capabilities,
        }


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    """The atomically registered run and its Freeze/dispatch commands."""

    admission: RunAdmission
    freeze_command: StageCommand
    dispatch_command: DispatchCommand

    def to_record(self) -> dict[str, Any]:
        return {
            "admission": {
                "created_at": self.admission.created_at.isoformat(),
                "from_stage": self.admission.from_stage,
                "parent_run_id": self.admission.parent_run_id,
                "request": _plain(self.admission.request),
                "request_digest": self.admission.request_digest,
                "request_id": self.admission.request_id,
                "run_id": self.admission.run_id,
                "schema_version": self.admission.schema_version,
            },
            "dispatch_command": self.dispatch_command.to_record(),
            "freeze_command": self.freeze_command.to_record(),
        }


def _stage_result_record(value: StageResult | FailureEnvelope) -> tuple[str, dict[str, Any]]:
    if isinstance(value, StageResult):
        return value.outcome, value.to_record()
    if isinstance(value, FailureEnvelope):
        return value.outcome, value.to_record()
    raise ApplicationError("application.result_invalid", "The common engine returned an unknown result")


def _stage_attempt_record(value: StageAttemptResult) -> tuple[str, dict[str, Any]]:
    if isinstance(value, StagePending):
        return "pending", {"wait": value.wait.to_record()}
    return _stage_result_record(value)


class StageApplication:
    """Static production composition; handlers and builders cannot be mutated after construction."""

    def __init__(
        self,
        *,
        services: StageServices,
        facts: AdmissionFactPort,
        artifacts: ArtifactStore,
        handlers: Mapping[StageId, StageHandler],
        builders: Mapping[StageId, CommandBuilder],
        clock: Callable[[], datetime] = _utc_now,
        reconciler: EffectReconciliationService | None = None,
        reconciliation_adapters: Mapping[str, EffectReconciliationAdapter] | None = None,
        cancellation_controllers: Mapping[str, CancellationControlPort] | None = None,
        native_target_identity: NativeTargetIdentity | None = None,
    ) -> None:
        if getattr(services, "facts", facts) is not facts:
            raise ApplicationError("application.store_identity_mismatch", "Stage services use another fact store")
        if getattr(services, "artifacts", artifacts) is not artifacts:
            raise ApplicationError("application.artifact_store_mismatch", "Stage services use another artifact store")
        self.services = services
        self.facts = facts
        self.artifacts = artifacts
        self.clock = clock
        if native_target_identity is not None and not isinstance(
            native_target_identity,
            NativeTargetIdentity,
        ):
            raise ApplicationError(
                "application.native_target_invalid",
                "native_target_identity must be a NativeTargetIdentity",
            )
        self.native_target_identity = native_target_identity
        adapters = dict(reconciliation_adapters or {})
        unknown_kinds = set(adapters) - _EFFECT_KINDS
        if unknown_kinds:
            raise ApplicationError(
                "application.reconciliation_kind_invalid",
                "Unknown effect reconciliation adapter kind",
                detail={"kinds": sorted(unknown_kinds)},
            )
        if (reconciler is None) != (not adapters):
            raise ApplicationError(
                "application.reconciliation_incomplete",
                "Reconciliation requires both its service and read-only adapters",
            )
        if reconciler is not None and (
            reconciler.facts is not facts
            or reconciler.artifacts is not artifacts
        ):
            raise ApplicationError(
                "application.reconciliation_store_mismatch",
                "Reconciliation uses another fact or artifact store",
            )
        self.reconciler = reconciler
        self.reconciliation_adapters = MappingProxyType(adapters)
        controllers = dict(cancellation_controllers or {})
        unknown_cancellation_kinds = set(controllers) - _EFFECT_KINDS
        if unknown_cancellation_kinds:
            raise ApplicationError(
                "application.cancellation_kind_invalid",
                "Unknown cancellation controller kind",
                detail={"kinds": sorted(unknown_cancellation_kinds)},
            )
        self.cancellation_controllers = MappingProxyType(controllers)
        for name, values in (("handlers", handlers), ("builders", builders)):
            unknown = set(values) - set(STAGES)
            if unknown:
                raise ApplicationError(
                    "application.registry_invalid",
                    f"Unknown stage in {name} registry",
                    detail={"unknown": sorted(unknown)},
                )
        self.handlers = MappingProxyType(dict(handlers))
        merged_builders = _default_builders()
        merged_builders.update(builders)
        self.builders = MappingProxyType(merged_builders)

    def missing_capabilities(self) -> dict[str, list[str]]:
        return {
            "handlers": [stage for stage in STAGES if stage not in self.handlers],
            "builders": [stage for stage in STAGES if stage not in self.builders],
            "reconciliation": (
                [
                    kind
                    for kind in _REQUIRED_RECONCILIATION_KINDS
                    if kind not in self.reconciliation_adapters
                ]
                if self.reconciler is not None
                else list(_REQUIRED_RECONCILIATION_KINDS)
            ),
            "cancellation": (
                []
                if "native_operation" in self.cancellation_controllers
                else ["native_operation"]
            ),
        }

    def reconcile_effect(
        self,
        *,
        command_id: str,
        effect_key: str,
    ) -> EffectReconciliationResult:
        """Observe one unresolved effect without acquiring or resending it."""

        if self.reconciler is None or not self.reconciliation_adapters:
            raise ApplicationPortMissing(
                "application.reconciliation_missing",
                "The read-only effect observer is not installed",
            )
        intent = self.facts.effect_intent_for_key(command_id, effect_key)
        if intent is None:
            raise ApplicationError(
                "application.effect_missing",
                "The effect intent is not registered",
                detail={"command_id": command_id, "effect_key": effect_key},
            )
        adapter = self.reconciliation_adapters.get(intent.effect_kind)
        if adapter is None:
            raise ApplicationPortMissing(
                "application.reconciliation_kind_missing",
                f"The {intent.effect_kind} effect observer is not installed",
                detail={"effect_kind": intent.effect_kind},
            )
        return self.reconciler.reconcile(
            command_id=command_id,
            effect_key=effect_key,
            adapter=adapter,
        )

    def _require_handler(self, stage: StageId) -> StageHandler:
        handler = self.handlers.get(stage)
        if handler is None:
            raise ApplicationPortMissing(
                "application.handler_missing",
                f"The {stage} handler is not installed",
                detail={"stage": stage},
            )
        return handler

    def _require_builder(self, stage: StageId) -> CommandBuilder:
        builder = self.builders.get(stage)
        if builder is None:
            raise ApplicationPortMissing(
                "application.builder_missing",
                f"The {stage} command builder is not installed",
                detail={"stage": stage},
            )
        return builder

    def _verify_admission_artifacts(self, spec: AdmissionSpec) -> None:
        for expected, role in (
            (spec.originals, "originals"),
            (spec.configuration, "configuration"),
            (spec.native_tool_requirement, "native_tool_requirement"),
        ):
            actual = VerifiedArtifact.from_store_record(
                expected.ref,
                self.artifacts.verify(expected.ref),
            )
            if actual != expected:
                raise ApplicationError(
                    "application.admission_artifact_mismatch",
                    f"Admission {role} differs from the artifact store",
                    detail={"role": role, "artifact_id": expected.ref.artifact_id},
                )
        ruleset_artifact = VerifiedArtifact.from_store_record(
            spec.ruleset.artifact.ref,
            self.artifacts.verify(spec.ruleset.artifact.ref),
        )
        if ruleset_artifact != spec.ruleset.artifact:
            raise ApplicationError(
                "application.admission_artifact_mismatch",
                "Admission ruleset differs from the artifact store",
                detail={"artifact_id": spec.ruleset.artifact.ref.artifact_id},
            )
        with tempfile.TemporaryDirectory(prefix="description-admission-ruleset-") as directory:
            root = self.artifacts.materialize(spec.ruleset.artifact.ref, Path(directory) / "artifact")
            catalog = root / "catalog.json"
            if not catalog.is_file():
                raise ApplicationError("application.ruleset_invalid", "Ruleset catalog is absent")
            actual_ruleset = VerifiedRuleset.from_catalog_bytes(ruleset_artifact, catalog.read_bytes())
        if actual_ruleset != spec.ruleset:
            raise ApplicationError("application.ruleset_invalid", "Ruleset version differs from its catalog")
        repository_snapshot = spec.publish_intent.repository
        snapshot_artifact = VerifiedArtifact.from_store_record(
            repository_snapshot.artifact.ref,
            self.artifacts.verify(repository_snapshot.artifact.ref),
        )
        if snapshot_artifact != repository_snapshot.artifact:
            raise ApplicationError(
                "application.admission_artifact_mismatch",
                "Admission repository snapshot differs from the artifact store",
                detail={"artifact_id": snapshot_artifact.ref.artifact_id},
            )
        with tempfile.TemporaryDirectory(prefix="description-admission-repository-") as directory:
            root = self.artifacts.materialize(
                snapshot_artifact.ref,
                Path(directory) / "artifact",
            )
            metadata_path = root / REPOSITORY_SNAPSHOT_METADATA
            if not metadata_path.is_file():
                raise ApplicationError(
                    "application.repository_snapshot_invalid",
                    "Repository snapshot metadata is absent",
                )
            actual_snapshot = VerifiedRepositorySnapshot.from_metadata_bytes(
                snapshot_artifact,
                metadata_path.read_bytes(),
            )
        if actual_snapshot != repository_snapshot:
            raise ApplicationError(
                "application.repository_snapshot_invalid",
                "Repository snapshot metadata differs from AdmissionSpec",
            )

    def admit(
        self,
        spec: AdmissionSpec,
        *,
        dispatch_dag_id: str = "solidworks_to_urdf",
        dispatch_dag_run_id: str | None = None,
    ) -> AdmissionOutcome:
        """Atomically register admission, Freeze and technical Airflow dispatch."""

        if not isinstance(spec, AdmissionSpec):
            raise ApplicationError("application.admission_required", "AdmissionSpec is required")
        if self.native_target_identity is not None:
            admitted_target = spec.require_native_target_identity()
            if admitted_target != self.native_target_identity:
                raise ApplicationError(
                    "application.native_target_mismatch",
                    "The admission target differs from the registered target",
                    detail={
                        "application": self.native_target_identity.to_record(),
                        "admission": admitted_target.to_record(),
                    },
                )
        if spec.from_stage not in {None, "freeze"}:
            raise ApplicationError(
                "application.linked_admission_builder_missing",
                "The linked-run admission builder is not installed",
                detail={"from_stage": spec.from_stage},
            )
        self._verify_admission_artifacts(spec)
        admission = build_run_admission(spec)
        command = build_freeze_command(spec)
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=spec.run_id,
            freeze_command_id=command.command_id,
        )
        dispatch_executor_sha256 = spec.dispatch_executor_sha256
        conf_artifact = self.artifacts.seal_dispatch_conf(
            conf,
            producer_run_id=spec.run_id,
            producer_tool_sha256=dispatch_executor_sha256,
        )
        dispatch_command = self._project_dispatch_command(
            spec,
            command,
            conf_artifact,
            dispatch_dag_id=dispatch_dag_id,
            dispatch_dag_run_id=dispatch_dag_run_id,
        )
        try:
            stored_command, stored_dispatch, _intent = self.facts.admit_run(
                admission,
                command,
                dispatch_command,
                conf_artifact,
            )
        except FactConflict:
            existing = self.load_admission_outcome(spec.run_id)
            if existing is None:
                raise
            return existing
        if stored_command != command or stored_dispatch != dispatch_command:
            raise ApplicationError(
                "application.admission_conflict",
                "Stored admission commands differ from their deterministic projection",
            )
        return AdmissionOutcome(
            admission=admission,
            freeze_command=stored_command,
            dispatch_command=stored_dispatch,
        )

    @staticmethod
    def _dispatch_identity(
        spec: AdmissionSpec,
        command: StageCommand,
        *,
        dispatch_dag_id: str = "solidworks_to_urdf",
        dispatch_dag_run_id: str | None = None,
    ) -> dict[str, str]:
        return {
            "admission_sha256": spec.admission_sha256,
            "dag_id": dispatch_dag_id,
            "dag_run_id": dispatch_dag_run_id or spec.request_id,
            "freeze_command_id": command.command_id,
            "run_id": spec.run_id,
        }

    def _project_dispatch_command(
        self,
        spec: AdmissionSpec,
        command: StageCommand,
        conf_artifact: ArtifactRef,
        *,
        dispatch_dag_id: str = "solidworks_to_urdf",
        dispatch_dag_run_id: str | None = None,
    ) -> DispatchCommand:
        dag_run_id = dispatch_dag_run_id or spec.request_id
        dispatch_identity = self._dispatch_identity(
            spec,
            command,
            dispatch_dag_id=dispatch_dag_id,
            dispatch_dag_run_id=dag_run_id,
        )
        return DispatchCommand.create(
            command_id="dispatch:" + digest(dispatch_identity),
            run_id=spec.run_id,
            freeze_command_id=command.command_id,
            idempotency_key=f"{spec.run_id}/dispatch",
            dag_id=dispatch_dag_id,
            dag_run_id=dag_run_id,
            conf_artifact=conf_artifact,
            expected_executor_sha256=spec.dispatch_executor_sha256,
        )

    def load_admission_outcome(self, run_id: str) -> AdmissionOutcome | None:
        """Read and validate one admitted run without sealing or writing any input."""

        admission = self.facts.load_admission(run_id)
        if admission is None:
            return None
        if not isinstance(admission.request, Mapping):
            raise ApplicationError(
                "application.admission_invalid",
                "Run admission request is malformed",
            )
        request_record = json.loads(json.dumps(_plain(admission.request), sort_keys=True))
        spec = AdmissionSpec.from_record(request_record)
        validate_run_admission(spec, admission)
        self._verify_admission_artifacts(spec)
        freeze_command = build_freeze_command(spec)
        stored_freeze = self.facts.load_command(freeze_command.command_id)
        if stored_freeze != freeze_command:
            raise ApplicationError(
                "application.admission_command_mismatch",
                "Stored Freeze command differs from its deterministic admission projection",
            )
        dispatch_identity = self._dispatch_identity(spec, stored_freeze)
        dispatch_command_id = "dispatch:" + digest(dispatch_identity)
        stored_dispatch = self.facts.load_dispatch_command(dispatch_command_id)
        expected_dispatch = self._project_dispatch_command(
            spec,
            stored_freeze,
            stored_dispatch.conf_artifact,
        )
        if stored_dispatch != expected_dispatch:
            raise ApplicationError(
                "application.admission_command_mismatch",
                "Stored dispatch command differs from its deterministic projection",
            )
        conf = self.artifacts.read_dispatch_conf(stored_dispatch.conf_artifact)
        if (
            conf.run_id != spec.run_id
            or conf.freeze_command_id != stored_freeze.command_id
        ):
            raise ApplicationError(
                "application.admission_command_mismatch",
                "Stored dispatch conf names another run or Freeze command",
            )
        return AdmissionOutcome(
            admission=admission,
            freeze_command=stored_freeze,
            dispatch_command=stored_dispatch,
        )

    def load_verified_freeze_origins(self, run_id: str) -> VerifiedArtifact:
        """Read parent CAD originals only through one committed Freeze closure."""

        spec, admission = self._load_spec(run_id)
        freeze_command = build_freeze_command(spec)
        stored_command = self.facts.load_command(freeze_command.command_id)
        if stored_command != freeze_command:
            raise ApplicationError(
                "application.freeze_origin_mismatch",
                "Stored Freeze command differs from its admission projection",
            )
        predecessor = self._completed_predecessor(stored_command)
        if predecessor is None:
            raise ApplicationError(
                "application.freeze_origin_uncommitted",
                "The parent run has no completed Freeze decision",
            )
        result = predecessor.result
        if (
            result.stage != "freeze"
            or result.run_id != admission.run_id
            or result.command_id != stored_command.command_id
        ):
            raise ApplicationError(
                "application.freeze_origin_invalid",
                "The parent Freeze result has another identity",
            )
        self.artifacts.verify(result.output_artifact)
        with tempfile.TemporaryDirectory(prefix="description-freeze-origin-") as directory:
            root = self.artifacts.materialize(
                result.output_artifact,
                Path(directory) / "artifact",
            )
            inputs = read_data(root / "inputs.json")
        if (
            not isinstance(inputs, Mapping)
            or set(inputs) != {"artifacts", "upload_inventory"}
            or not isinstance(inputs.get("artifacts"), Mapping)
            or set(inputs["artifacts"])
            != {"configuration", "originals", "ruleset"}
            or inputs["artifacts"].get("originals")
            != spec.originals.ref.to_record()
            or inputs.get("upload_inventory") != spec.originals.inventory
        ):
            raise ApplicationError(
                "application.freeze_origin_invalid",
                "The parent Freeze closure differs from its admitted originals",
            )
        actual = VerifiedArtifact.from_store_record(
            spec.originals.ref,
            self.artifacts.verify(spec.originals.ref),
        )
        if actual != spec.originals:
            raise ApplicationError(
                "application.freeze_origin_invalid",
                "The parent originals differ from the artifact store",
            )
        return spec.originals

    def dispatch(
        self,
        command_id: str,
        adapter: DispatchAdapter,
    ) -> DispatchResult:
        """Drive one admitted dispatch command through the common effect runtime."""

        command = self.facts.load_dispatch_command(command_id)
        claim_policy = getattr(self.services, "claim_policy", None)
        work_root = getattr(self.services, "work_root", None)
        if (
            not isinstance(claim_policy, ClaimPolicy)
            or not isinstance(work_root, Path)
        ):
            raise ApplicationPortMissing(
                "application.dispatch_missing",
                "The production dispatch service is not installed",
            )
        service = TechnicalDispatchService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=work_root / "dispatch",
            claim_policy=ClaimPolicy(
                owner_sha256=claim_policy.owner_sha256,
                executor_sha256=command.expected_executor_sha256,
                lease=claim_policy.lease,
            ),
            clock=self.clock,
            adapter=adapter,
        )
        return service.dispatch(command_id)

    def cancel(
        self,
        *,
        effect_kind: str,
        cancel_command_id: str,
        target_effect_id: str,
    ):
        """Send one cancellation transport for its exact effect kind."""

        controller = self.cancellation_controllers.get(effect_kind)
        if controller is None:
            raise ApplicationPortMissing(
                "application.cancellation_missing",
                f"The {effect_kind} cancellation controller is not installed",
                detail={"effect_kind": effect_kind},
            )
        return controller.send(
            cancel_command_id=cancel_command_id,
            target_effect_id=target_effect_id,
        )

    def admission_by_request_id(self, request_id: str) -> RunAdmission | None:
        """Read one existing admission by its stable request key."""

        if not isinstance(request_id, str) or not request_id:
            raise ApplicationError(
                "application.request_id_invalid",
                "A non-empty request id is required",
            )
        loader = getattr(self.facts, "load_admission_by_request_id", None)
        if not callable(loader):
            raise ApplicationError(
                "application.request_lookup_missing",
                "The fact store cannot resolve request-id admission identity",
            )
        return loader(request_id)

    def _load_spec(self, run_id: str) -> tuple[AdmissionSpec, RunAdmission]:
        admission = self.facts.load_admission(run_id)
        if admission is None:
            raise ApplicationError("application.run_not_admitted", f"Run {run_id!r} is not admitted")
        if not isinstance(admission.request, Mapping):
            raise ApplicationError("application.admission_invalid", "Run admission request is malformed")
        request_record = json.loads(json.dumps(_plain(admission.request), sort_keys=True))
        spec = AdmissionSpec.from_record(request_record)
        validate_run_admission(spec, admission)
        return spec, admission

    def _build_command(
        self,
        spec: AdmissionSpec,
        stage: StageId,
        predecessors: Mapping[StageId, VerifiedPredecessor],
        discover_result: VerifiedDiscoverResult | None,
        verify_result: VerifiedVerifyResult | None,
    ) -> StageCommand:
        builder = self._require_builder(stage)
        command = builder(
            CommandBuildContext(
                spec,
                stage,
                predecessors,
                discover_result,
                verify_result,
            )
        )
        if (
            command.run_id != spec.run_id
            or command.stage != stage
            or command.kind != "stage"
            or command.contract_sha256 != spec.contract_sha256
            or command.policy_sha256 != spec.policy_sha256
            or command.config_snapshot_sha256 != spec.platform_config_sha256
            or command.expected_executor_sha256 != spec.executors.for_stage(stage)
        ):
            raise ApplicationError(
                "application.command_builder_invalid",
                f"The {stage} builder produced a command outside the admission identity",
                detail={"stage": stage, "command_id": command.command_id},
            )
        previous, supporting, command_discover_result = self._command_predecessors(
            stage,
            predecessors,
            discover_result,
        )
        validate_stage_command(
            spec,
            command,
            predecessor=previous,
            supporting_predecessors=supporting,
            discover_result=command_discover_result,
            verify_result=verify_result if stage == "publish" else None,
            publish_intent=spec.publish_intent if stage == "publish" else None,
        )
        return command

    @staticmethod
    def _command_predecessors(
        stage: StageId,
        predecessors: Mapping[StageId, VerifiedPredecessor],
        discover_result: VerifiedDiscoverResult | None,
    ) -> tuple[
        VerifiedPredecessor | None,
        tuple[VerifiedPredecessor, ...],
        VerifiedDiscoverResult | None,
    ]:
        index = STAGES.index(stage)
        previous = predecessors.get(STAGES[index - 1]) if index else None
        supporting: tuple[VerifiedPredecessor, ...] = ()
        if stage == "capture":
            supporting = (predecessors["freeze"],)
        elif stage == "verify":
            supporting = (predecessors["capture"],)
        return previous, supporting, discover_result if stage == "capture" else None

    def _completed_predecessor(
        self,
        command: StageCommand,
    ) -> VerifiedPredecessor | None:
        decision = self.facts.decision_for(command)
        if decision is None:
            return None
        if decision.outcome != "completed":
            return None
        result = self.facts.load_result(decision)
        return VerifiedPredecessor(
            stage=command.stage,
            command=command,
            result=result,
            decision=decision,
        )

    def _verified_discover_result(
        self,
        predecessor: VerifiedPredecessor,
    ) -> VerifiedDiscoverResult:
        if predecessor.stage != "discover":
            raise ApplicationError("application.discover_result_missing", "Discover predecessor is required")
        with tempfile.TemporaryDirectory(prefix="description-discover-result-") as directory:
            root = self.artifacts.materialize(
                predecessor.result.output_artifact,
                Path(directory) / "artifact",
            )
            record = read_data(root / "result.json")
        return VerifiedDiscoverResult.from_record(predecessor, record)

    def _verified_verify_result(
        self,
        predecessor: VerifiedPredecessor,
    ) -> VerifiedVerifyResult:
        if predecessor.stage != "verify":
            raise ApplicationError("application.verify_result_missing", "Verify predecessor is required")
        artifact = VerifiedArtifact.from_store_record(
            predecessor.result.output_artifact,
            self.artifacts.verify(predecessor.result.output_artifact),
        )
        with tempfile.TemporaryDirectory(prefix="description-verify-result-") as directory:
            root = self.artifacts.materialize(
                predecessor.result.output_artifact,
                Path(directory) / "artifact",
            )
            certificate = (root / CERTIFICATE_FILENAME).read_bytes()
        return VerifiedVerifyResult.from_artifact_bytes(predecessor, artifact, certificate)

    def execute_registered_command(
        self,
        command_id: str,
        *,
        resume: StageEffectWait | None = None,
    ) -> StageAttemptResult:
        """Execute one registered command through the one common engine."""

        command = self.facts.load_command(command_id)
        if command.kind != "stage" or command.stage is None:
            raise ApplicationError("application.command_not_stage", "Only stage commands can execute")
        handler = self._require_handler(command.stage)
        return execute_stage(
            command_id=command_id,
            handler=handler,
            services=self.services,
            resume=resume,
            resume_reconciler=self if resume is not None else None,
        )

    def replay(self, command_id: str) -> StageAttemptResult:
        """Replay an already-admitted sealed command with no alternate execution path."""

        command = self.facts.load_command(command_id)
        scope = command.diagnostic_scope
        if not isinstance(scope, DiagnosticReplayScope):
            raise ApplicationError(
                "application.replay_scope_required",
                "Replay requires the common command scope to establish diagnostic qualification",
            )
        if scope.target_stage != command.stage:
            raise ApplicationError(
                "application.replay_scope_invalid",
                "Diagnostic target stage differs from the registered command",
            )
        scope.validate_for(command.stage, command.input_artifacts)
        return self.execute_registered_command(command_id)

    def run_stage(self, run_id: str, stage: StageId) -> StageAttemptResult:
        """Execute one admitted stage or return its canonical pending wait."""

        if stage not in STAGES:
            raise ApplicationError("application.stage_invalid", f"Unknown stage {stage!r}")
        self._validate_airflow_task_binding(run_id, stage)
        spec, admission = self._load_spec(run_id)
        self._require_handler(stage)
        if spec.from_stage not in {None, "freeze"}:
            raise ApplicationError(
                "application.linked_run_not_supported",
                "This application composition has no linked-run predecessor builder",
                detail={"from_stage": spec.from_stage},
            )
        predecessors: dict[StageId, VerifiedPredecessor] = {}
        discover_result: VerifiedDiscoverResult | None = None
        verify_result: VerifiedVerifyResult | None = None
        target_index = STAGES.index(stage)
        for current_stage in STAGES[: target_index + 1]:
            command = self._build_command(
                spec,
                current_stage,
                predecessors,
                discover_result,
                verify_result,
            )
            if current_stage == "freeze":
                stored = self.facts.load_command(command.command_id)
                if stored != command:
                    raise ApplicationError(
                        "application.admission_command_mismatch",
                        "Stored Freeze command differs from its deterministic admission projection",
                    )
            else:
                previous, supporting, command_discover_result = self._command_predecessors(
                    current_stage,
                    predecessors,
                    discover_result,
                )
                if previous is None:
                    raise ApplicationError(
                        "application.predecessor_missing",
                        f"{current_stage} requires a completed predecessor",
                        detail={"stage": current_stage},
                    )
                stored = self.facts.create_command(
                    command,
                    admission=admission,
                    predecessor=previous,
                    supporting_predecessors=supporting,
                    discover_result=command_discover_result,
                    verify_result=verify_result if current_stage == "publish" else None,
                    publish_intent=spec.publish_intent if current_stage == "publish" else None,
                )
                if stored != command:
                    raise ApplicationError(
                        "application.command_conflict",
                        "Stored successor command differs from its deterministic projection",
                        detail={"stage": current_stage},
                    )
            predecessor = self._completed_predecessor(stored)
            if predecessor is not None:
                predecessors[current_stage] = predecessor
                if current_stage == "discover":
                    discover_result = self._verified_discover_result(predecessor)
                if current_stage == "verify":
                    verify_result = self._verified_verify_result(predecessor)
                if current_stage == stage:
                    return self.execute_registered_command(stored.command_id)
                continue
            if current_stage != stage:
                raise ApplicationError(
                    "application.upstream_incomplete",
                    f"Stage {current_stage!r} has no completed decision",
                    detail={"run_id": run_id, "stage": current_stage, "target_stage": stage},
                )
            return self.execute_registered_command(stored.command_id)
        raise ApplicationError("application.stage_plan_invalid", "The stage plan ended without a target")

    def resume_stage(
        self,
        wait: StageEffectWait,
        *,
        wakeup: EffectWakeupHint | None = None,
    ) -> StageAttemptResult:
        """Reconcile one exact yielded effect and resume its same canonical command."""

        if not isinstance(wait, StageEffectWait):
            raise ApplicationError(
                "application.effect_wait_invalid",
                "A canonical StageEffectWait is required for resume",
            )
        self._validate_airflow_task_binding(wait.run_id, wait.stage)
        if wakeup is not None:
            if not isinstance(wakeup, EffectWakeupHint):
                raise ApplicationError(
                    "application.effect_wakeup_invalid",
                    "The optional wake-up value must be an EffectWakeupHint",
                )
            if wakeup.wait_digest != wait.digest:
                raise ApplicationError(
                    "application.effect_wakeup_mismatch",
                    "The wake-up hint names another effect wait",
                    detail={
                        "expected": wait.digest,
                        "actual": wakeup.wait_digest,
                    },
                )
        return self.execute_registered_command(
            wait.command_id,
            resume=wait,
        )

    async def observe_pending(self, wait: StageEffectWait) -> EffectWakeupHint:
        """Perform one passive status observation without acquiring authority."""

        if not isinstance(wait, StageEffectWait):
            raise ApplicationError(
                "application.effect_wait_invalid",
                "A canonical StageEffectWait is required for observation",
            )
        from ..orchestration.airflow_effect_trigger import probe_effect_wait

        return await probe_effect_wait(wait)

    def _validate_airflow_task_binding(self, run_id: str, stage: StageId) -> None:
        """Reject a context binding that differs from the admitted dispatch identity."""

        policy = getattr(self.services, "claim_policy", None)
        if not isinstance(policy, ClaimPolicy):
            return
        values = (policy.dag_id, policy.dag_run_id, policy.task_id, policy.map_index)
        if all(value is None for value in values):
            return
        if any(value is None for value in values):
            raise ApplicationError(
                "application.airflow_binding_incomplete",
                "Airflow claims require dag_id, dag_run_id, task_id and map_index together",
            )
        binding = AirflowTaskBinding(
            dag_id=policy.dag_id,
            dag_run_id=policy.dag_run_id,
            task_id=policy.task_id,
            map_index=policy.map_index,
        )
        outcome = self.load_admission_outcome(run_id)
        if outcome is None:
            raise ApplicationError(
                "application.airflow_binding_run_missing",
                "Airflow task binding names an unadmitted run",
                detail={"run_id": run_id},
            )
        dispatch = outcome.dispatch_command
        if binding.task_id != stage:
            raise ApplicationError(
                "application.airflow_binding_stage_mismatch",
                "Airflow task_id differs from the requested stage",
                detail={"task_id": binding.task_id, "stage": stage},
            )
        if (
            binding.dag_id != dispatch.dag_id
            or binding.dag_run_id != dispatch.dag_run_id
        ):
            raise ApplicationError(
                "application.airflow_binding_dispatch_mismatch",
                "Airflow task binding differs from the admitted dispatch",
                detail={
                    "bound": {
                        "dag_id": binding.dag_id,
                        "dag_run_id": binding.dag_run_id,
                    },
                    "admitted": {
                        "dag_id": dispatch.dag_id,
                        "dag_run_id": dispatch.dag_run_id,
                    },
                },
            )

    def execute_run(self, run_id: str, *, through: StageId = "publish") -> ApplicationOutcome:
        """Run stages one at a time from one admission; no downstream work is hidden in a handler."""

        if through not in STAGES:
            raise ApplicationError("application.stage_invalid", f"Unknown stage {through!r}")
        last: StageAttemptResult | None = None
        completed_stage: StageId | None = None
        for stage in STAGES[: STAGES.index(through) + 1]:
            last = self.run_stage(run_id, stage)
            outcome, payload = _stage_attempt_record(last)
            if outcome != "completed":
                return ApplicationOutcome(
                    run_id=run_id,
                    stage=stage,
                    outcome=outcome,
                    payload=payload,
                    missing_capabilities=self.missing_capabilities(),
                )
            completed_stage = stage
        if not isinstance(last, StageResult):
            raise ApplicationError(
                "application.result_invalid",
                "The stage sequence ended without a completed StageResult",
            )
        return ApplicationOutcome(
            run_id=run_id,
            stage=completed_stage,
            outcome="completed",
            payload=last.to_record(),
            missing_capabilities=self.missing_capabilities(),
        )


def build_stage_application(
    *,
    services: StageServices,
    facts: AdmissionFactPort,
    artifacts: ArtifactStore,
    handlers: Mapping[StageId, StageHandler],
    builders: Mapping[StageId, CommandBuilder] | None = None,
    clock: Callable[[], datetime] = _utc_now,
    reconciler: EffectReconciliationService | None = None,
    reconciliation_adapters: Mapping[str, EffectReconciliationAdapter] | None = None,
    cancellation_controllers: Mapping[str, CancellationControlPort] | None = None,
    native_target_identity: NativeTargetIdentity | None = None,
) -> StageApplication:
    """Construct one immutable application from explicit ports; no hidden fallback exists."""

    return StageApplication(
        services=services,
        facts=facts,
        artifacts=artifacts,
        handlers=handlers,
        builders=builders or {},
        clock=clock,
        reconciler=reconciler,
        reconciliation_adapters=reconciliation_adapters,
        cancellation_controllers=cancellation_controllers,
        native_target_identity=native_target_identity,
    )


def application_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    discover_adapter: DiscoverDomainAdapter | None = None,
    capture_handler: StageHandler | None = None,
    publish_handler: StageHandler | None = None,
    handlers: Mapping[StageId, StageHandler] | None = None,
    builders: Mapping[StageId, CommandBuilder] | None = None,
    native_operation_client: NativeOperationClient | None = None,
    native_target_identity: NativeTargetIdentity | None = None,
    reconciliation_adapters: Mapping[str, EffectReconciliationAdapter] | None = None,
    cancellation_controllers: Mapping[str, CancellationControlPort] | None = None,
    airflow_task_binding: AirflowTaskBinding | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> StageApplication:
    """Compose implemented handlers; missing runtime capabilities remain explicit."""

    source = os.environ if environ is None else environ
    artifacts = ArtifactStore.from_environment(source)
    facts = FactStore.from_environment(source, result_reader=artifacts)
    work_root = source.get("PIPELINE_WORK_ROOT")
    if not work_root:
        raise ApplicationError("application.work_root_missing", "PIPELINE_WORK_ROOT is not configured")
    executor_sha256 = _portable_executor_sha256()
    registered_target = (
        native_target_identity
        if native_target_identity is not None
        else _registered_native_target_from_environment(source)
    )
    if not isinstance(registered_target, NativeTargetIdentity):
        raise ApplicationError(
            "application.native_target_invalid",
            "native_target_identity must be a NativeTargetIdentity",
        )
    if (
        native_operation_client is not None
        and getattr(native_operation_client, "target_id", None)
        != registered_target.target_id
    ):
        raise ApplicationError(
            "application.native_client_target_mismatch",
            "The injected native client differs from the registered target",
            detail={
                "registered": registered_target.target_id,
                "client": getattr(native_operation_client, "target_id", None),
            },
        )
    owner_sha256 = digest({"process": str(uuid.uuid4()), "pid": os.getpid()})
    lease_seconds = float(source.get("PIPELINE_CLAIM_LEASE_SECONDS", "3600"))
    if airflow_task_binding is None:
        claim_binding = {
            "dag_id": None,
            "dag_run_id": None,
            "task_id": None,
            "map_index": None,
        }
    elif isinstance(airflow_task_binding, AirflowTaskBinding):
        claim_binding = {
            "dag_id": airflow_task_binding.dag_id,
            "dag_run_id": airflow_task_binding.dag_run_id,
            "task_id": airflow_task_binding.task_id,
            "map_index": airflow_task_binding.map_index,
        }
    else:
        raise ApplicationError(
            "application.airflow_binding_invalid",
            "airflow_task_binding must be an AirflowTaskBinding",
        )
    services = CanonicalStageServices(
        facts=facts,
        artifacts=artifacts,
        work_root=work_root,
        claim_policy=ClaimPolicy(
            owner_sha256=owner_sha256,
            executor_sha256=executor_sha256,
            lease=timedelta(seconds=lease_seconds),
            **claim_binding,
        ),
        clock=clock,
        transfer_resume_gate=_transfer_resume_gate_from_environment(source),
    )
    client = native_operation_client
    if discover_adapter is None or capture_handler is None:
        client = (
            native_operation_client
            if native_operation_client is not None
            else _native_client_from_environment(
                source,
                target_id=registered_target.target_id,
            )
        )
        tool_provider = NativeOperationToolProvider(client)
        if discover_adapter is None:
            discover_adapter = NativeRemoteDiscoverAdapter(
                port=NativeOperationDiscoverPort(
                    client,
                    native_tool=None,
                    tool_provider=tool_provider,
                ),
                artifacts=artifacts,
            )
        if capture_handler is None:
            capture_handler = capture_handler_from_native_operation(
                client,
                artifact_store=artifacts,
                tool_provider=tool_provider,
            )
    adapters = dict(reconciliation_adapters or {})
    if client is not None:
        adapters.setdefault(
            "native_operation",
            NativeOperationEffectObserver(client),
        )
    from .stages import publish as publish_stage

    publish_observer = getattr(
        publish_stage,
        "PublishGitReconciliationAdapter",
        None,
    )
    if publish_observer is not None:
        adapters.setdefault("git_ref_update", publish_observer())
    pull_request_observer = getattr(
        publish_stage,
        "PublishPullRequestReconciliationAdapter",
        None,
    )
    if pull_request_observer is not None:
        from ..repository.publish import GitHubCliPullRequestClient

        pull_request_root = Path(work_root) / "publish-pr-observer"
        pull_request_root.mkdir(parents=True, exist_ok=True)
        adapters.setdefault(
            "pr_upsert",
            pull_request_observer(
                GitHubCliPullRequestClient(pull_request_root)
            ),
        )
    controllers = dict(cancellation_controllers or {})
    if client is not None:
        try:
            from .cancellation import CancellationControlService
            from ..orchestration.native_operation_cancellation import (
                NATIVE_OPERATION_EFFECT_KIND,
                native_operation_cancellation_adapter,
            )
        except ModuleNotFoundError as error:
            if error.name not in {
                "description_pipeline.pipeline.cancellation",
                "description_pipeline.orchestration.native_operation_cancellation",
            }:
                raise
        else:
            cancellation_adapter = native_operation_cancellation_adapter(
                client,
                facts=facts,
                artifacts=artifacts,
            )
            controllers.setdefault(
                NATIVE_OPERATION_EFFECT_KIND,
                CancellationControlService(
                    facts=facts,
                    artifacts=artifacts,
                    work_root=Path(work_root) / "cancellation",
                    claim_policy=services.claim_policy,
                    clock=clock,
                    adapter=cancellation_adapter,
                ),
            )
    reconciler = (
        EffectReconciliationService(
            facts=facts,
            artifacts=artifacts,
            work_root=Path(work_root) / "reconciliation",
            observer_tool_sha256=executor_sha256,
            clock=clock,
        )
        if adapters
        else None
    )
    installed: dict[StageId, StageHandler] = {
        "freeze": FreezeHandler(),
        "generate": GenerateHandler(artifacts),
        "publish": PublishHandler(artifacts),
        "verify": VerifyHandler(),
    }
    assert discover_adapter is not None
    assert capture_handler is not None
    installed["discover"] = DiscoverHandler(discover_adapter)
    installed["capture"] = capture_handler
    if publish_handler is not None:
        installed["publish"] = publish_handler
    for stage, handler in (handlers or {}).items():
        if stage in installed and installed[stage] != handler:
            raise ApplicationError(
                "application.duplicate_handler",
                f"Handler {stage!r} is supplied twice",
                detail={"stage": stage},
            )
        installed[stage] = handler
    return StageApplication(
        services=services,
        facts=facts,
        artifacts=artifacts,
        handlers=installed,
        builders=builders or {},
        clock=clock,
        reconciler=reconciler,
        reconciliation_adapters=adapters,
        cancellation_controllers=controllers,
        native_target_identity=registered_target,
    )
__all__ = [
    "STAGES",
    "AdmissionOutcome",
    "AirflowTaskBinding",
    "ApplicationError",
    "ApplicationOutcome",
    "ApplicationPortMissing",
    "CancellationControlPort",
    "CommandBuildContext",
    "CommandBuilder",
    "EffectWakeupHint",
    "StageApplication",
    "application_from_environment",
    "build_stage_application",
]
