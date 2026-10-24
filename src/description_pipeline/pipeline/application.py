"""The single production application service for the six engineering stages."""

from __future__ import annotations

import os
import json
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from ..artifacts import ArtifactStore
from ..io import PipelineError, digest
from ..platform.facts import FactStore
from ..runtime import tool_record
from .admission import (
    AdmissionSpec,
    VerifiedArtifact,
    VerifiedPredecessor,
    VerifiedRuleset,
    build_discover_command,
    build_freeze_command,
    build_run_admission,
    validate_run_admission,
    validate_stage_command,
)
from .contracts import (
    FailureEnvelope,
    RunAdmission,
    StageCommand,
    StageDecision,
    StageId,
    StageResult,
)
from .discover import DiscoverDomainAdapter, DiscoverHandler
from .engine import StageHandler, StageServices, execute_stage
from .freeze import FreezeHandler
from .services import CanonicalStageServices, ClaimPolicy
from .stages.generate import GenerateHandler
from .verify import VerifyHandler

STAGES: tuple[StageId, ...] = (
    "freeze",
    "discover",
    "capture",
    "generate",
    "verify",
    "publish",
)


class ApplicationError(PipelineError):
    def __init__(self, code: str, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})


class ApplicationPortMissing(ApplicationError):
    """The production composition lacks a required stage capability."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class AdmissionFactPort(Protocol):
    def load_admission(self, run_id: str) -> RunAdmission | None: ...

    def admit_run(self, admission: RunAdmission, command: StageCommand) -> StageCommand: ...

    def load_command(self, command_id: str) -> StageCommand: ...

    def create_command(self, command: StageCommand) -> StageCommand: ...

    def decision_for(self, command: StageCommand) -> StageDecision | None: ...

    def load_result(self, decision: StageDecision) -> StageResult: ...


@dataclass(frozen=True, slots=True)
class CommandBuildContext:
    """All verified facts a deterministic successor builder may consume."""

    admission: AdmissionSpec
    stage: StageId
    predecessors: Mapping[StageId, VerifiedPredecessor]

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
    }


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


def _stage_result_record(value: StageResult | FailureEnvelope) -> tuple[str, dict[str, Any]]:
    if isinstance(value, StageResult):
        return value.outcome, value.to_record()
    if isinstance(value, FailureEnvelope):
        return value.outcome, value.to_record()
    raise ApplicationError("application.result_invalid", "The common engine returned an unknown result")


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
    ) -> None:
        if getattr(services, "facts", facts) is not facts:
            raise ApplicationError("application.store_identity_mismatch", "Stage services use another fact store")
        if getattr(services, "artifacts", artifacts) is not artifacts:
            raise ApplicationError("application.artifact_store_mismatch", "Stage services use another artifact store")
        self.services = services
        self.facts = facts
        self.artifacts = artifacts
        self.clock = clock
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
        }

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

    def admit(self, spec: AdmissionSpec) -> StageCommand:
        """Atomically register the immutable admission and its initial Freeze command."""

        if not isinstance(spec, AdmissionSpec):
            raise ApplicationError("application.admission_required", "AdmissionSpec is required")
        if spec.from_stage not in {None, "freeze"}:
            raise ApplicationError(
                "application.linked_admission_builder_missing",
                "The linked-run admission builder is not installed",
                detail={"from_stage": spec.from_stage},
            )
        self._verify_admission_artifacts(spec)
        admission = build_run_admission(spec)
        command = build_freeze_command(spec)
        stored = self.facts.admit_run(admission, command)
        if stored != command:
            raise ApplicationError("application.admission_conflict", "Stored Freeze command differs")
        return stored

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
    ) -> StageCommand:
        builder = self._require_builder(stage)
        command = builder(CommandBuildContext(spec, stage, predecessors))
        if (
            command.run_id != spec.run_id
            or command.stage != stage
            or command.kind != "stage"
            or command.contract_sha256 != spec.contract_sha256
            or command.policy_sha256 != spec.policy_sha256
            or command.config_snapshot_sha256 != spec.configuration.ref.set_sha256
            or command.expected_executor_sha256 != spec.expected_executor_sha256
        ):
            raise ApplicationError(
                "application.command_builder_invalid",
                f"The {stage} builder produced a command outside the admission identity",
                detail={"stage": stage, "command_id": command.command_id},
            )
        if stage in {"freeze", "discover"}:
            validate_stage_command(
                spec,
                command,
                predecessor=predecessors.get(STAGES[STAGES.index(stage) - 1])
                if stage != "freeze"
                else None,
            )
        return command

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

    def execute_registered_command(self, command_id: str) -> StageResult | FailureEnvelope:
        """Execute one already-registered command through the one common engine."""

        command = self.facts.load_command(command_id)
        if command.kind != "stage" or command.stage is None:
            raise ApplicationError("application.command_not_stage", "Only stage commands can execute")
        handler = self._require_handler(command.stage)
        return execute_stage(command_id=command_id, handler=handler, services=self.services)

    def replay(self, command_id: str) -> StageResult | FailureEnvelope:
        """Replay an already-admitted sealed command with no alternate execution path."""

        command = self.facts.load_command(command_id)
        scope = getattr(command, "scope", None)
        if not isinstance(scope, Mapping) or scope.get("mode") != "diagnostic":
            raise ApplicationError(
                "application.replay_scope_required",
                "Replay requires the common command scope to establish diagnostic qualification",
            )
        if scope.get("target_stage") != command.stage:
            raise ApplicationError(
                "application.replay_scope_invalid",
                "Diagnostic target stage differs from the registered command",
            )
        return self.execute_registered_command(command_id)

    def run_stage(self, run_id: str, stage: StageId) -> StageResult | FailureEnvelope:
        """Execute or return one stage after proving every prior decision in the admitted run."""

        if stage not in STAGES:
            raise ApplicationError("application.stage_invalid", f"Unknown stage {stage!r}")
        spec, _admission = self._load_spec(run_id)
        self._require_handler(stage)
        if spec.from_stage not in {None, "freeze"}:
            raise ApplicationError(
                "application.linked_run_not_supported",
                "This application composition has no linked-run predecessor builder",
                detail={"from_stage": spec.from_stage},
            )
        predecessors: dict[StageId, VerifiedPredecessor] = {}
        target_index = STAGES.index(stage)
        for current_stage in STAGES[: target_index + 1]:
            command = self._build_command(spec, current_stage, predecessors)
            if current_stage == "freeze":
                stored = self.facts.load_command(command.command_id)
                if stored != command:
                    raise ApplicationError(
                        "application.admission_command_mismatch",
                        "Stored Freeze command differs from its deterministic admission projection",
                    )
            else:
                stored = self.facts.create_command(command)
                if stored != command:
                    raise ApplicationError(
                        "application.command_conflict",
                        "Stored successor command differs from its deterministic projection",
                        detail={"stage": current_stage},
                    )
            predecessor = self._completed_predecessor(stored)
            if predecessor is not None:
                predecessors[current_stage] = predecessor
                continue
            if current_stage != stage:
                raise ApplicationError(
                    "application.upstream_incomplete",
                    f"Stage {current_stage!r} has no completed decision",
                    detail={"run_id": run_id, "stage": current_stage, "target_stage": stage},
                )
            return self.execute_registered_command(stored.command_id)
        raise ApplicationError("application.stage_plan_invalid", "The stage plan ended without a target")

    def execute_run(self, run_id: str, *, through: StageId = "publish") -> ApplicationOutcome:
        """Run stages one at a time from one admission; no downstream work is hidden in a handler."""

        if through not in STAGES:
            raise ApplicationError("application.stage_invalid", f"Unknown stage {through!r}")
        last = None
        completed_stage = None
        for stage in STAGES[: STAGES.index(through) + 1]:
            last = self.run_stage(run_id, stage)
            outcome, payload = _stage_result_record(last)
            if outcome != "completed":
                return ApplicationOutcome(
                    run_id=run_id,
                    stage=stage,
                    outcome=outcome,
                    payload=payload,
                    missing_capabilities=self.missing_capabilities(),
                )
            completed_stage = stage
        return ApplicationOutcome(
            run_id=run_id,
            stage=completed_stage,
            outcome="completed",
            payload=last.to_record() if last is not None else {},
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
) -> StageApplication:
    """Construct one immutable application from explicit ports; no hidden fallback exists."""

    return StageApplication(
        services=services,
        facts=facts,
        artifacts=artifacts,
        handlers=handlers,
        builders=builders or {},
        clock=clock,
    )


def application_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    discover_adapter: DiscoverDomainAdapter | None = None,
    capture_handler: StageHandler | None = None,
    publish_handler: StageHandler | None = None,
    handlers: Mapping[StageId, StageHandler] | None = None,
    builders: Mapping[StageId, CommandBuilder] | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> StageApplication:
    """Compose implemented handlers; missing runtime capabilities remain explicit."""

    source = os.environ if environ is None else environ
    artifacts = ArtifactStore.from_environment(source)
    facts = FactStore.from_environment(source, result_reader=artifacts)
    work_root = source.get("PIPELINE_WORK_ROOT")
    if not work_root:
        raise ApplicationError("application.work_root_missing", "PIPELINE_WORK_ROOT is not configured")
    executor_sha256 = digest(tool_record(role="portable"))
    owner_sha256 = digest({"process": str(uuid.uuid4()), "pid": os.getpid()})
    lease_seconds = float(source.get("PIPELINE_CLAIM_LEASE_SECONDS", "3600"))
    services = CanonicalStageServices(
        facts=facts,
        artifacts=artifacts,
        work_root=work_root,
        claim_policy=ClaimPolicy(
            owner_sha256=owner_sha256,
            executor_sha256=executor_sha256,
            lease=timedelta(seconds=lease_seconds),
        ),
        clock=clock,
    )
    installed: dict[StageId, StageHandler] = {
        "freeze": FreezeHandler(),
        "generate": GenerateHandler(artifacts),
        "verify": VerifyHandler(),
    }
    if discover_adapter is not None:
        installed["discover"] = DiscoverHandler(discover_adapter)
    if capture_handler is not None:
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
    )
__all__ = [
    "STAGES",
    "ApplicationError",
    "ApplicationOutcome",
    "ApplicationPortMissing",
    "CommandBuildContext",
    "CommandBuilder",
    "StageApplication",
    "application_from_environment",
    "build_stage_application",
]
