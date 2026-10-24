"""Capture domain handler for the shared six-stage engine."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..io import PipelineError, digest, read_data
from ..sources.solidworks.discovery import DISCOVERY_FILE, native_component_identity
from ..sources.solidworks.revision import package_inventory
from .contracts import ArtifactRef, ExecutionClaim, ProtocolError, StageCandidate, StageCommand, StageExecution
from .engine import StageWorkspace


NativeOutcome = Literal["completed", "failed", "cancelled", "unknown"]


class CaptureUnresolved(PipelineError):
    """The native operation has a possible external effect with no safe terminal fact."""


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    """The Capture-specific part of one immutable StageCommand request."""

    handoff_sha256: str
    upload_sha256: str
    prepared_sha256: str
    main_assembly: str
    effect_id: str

    @classmethod
    def from_command(cls, command: StageCommand) -> CaptureRequest:
        if command.stage != "capture":
            raise ProtocolError("capture.command_stage", {"stage": command.stage})
        request = command.request
        required = {
            "effect_id",
            "handoff_sha256",
            "main_assembly",
            "prepared_sha256",
            "upload_sha256",
        }
        if set(request) != required:
            raise ProtocolError(
                "capture.request_fields",
                {"missing": sorted(required - set(request)), "unknown": sorted(set(request) - required)},
            )
        values = {key: request[key] for key in required}
        if not all(isinstance(value, str) and value and "\x00" not in value for value in values.values()):
            raise ProtocolError("capture.request_invalid", {"where": "text-fields"})
        for key in ("handoff_sha256", "prepared_sha256", "upload_sha256"):
            value = values[key]
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ProtocolError("capture.request_invalid", {"where": key})
        return cls(**values)


@dataclass(frozen=True, slots=True)
class CaptureInputs:
    """Materialized provenance references and the one Capture package."""

    freeze: ArtifactRef
    discover: ArtifactRef
    freeze_root: Path
    package: Path
    prepared_files: Mapping[str, str]
    plan: Mapping[str, Any]
    object_names: tuple[str, ...]

    @classmethod
    def resolve(
        cls,
        command: StageCommand,
        workspace: StageWorkspace,
        request: CaptureRequest,
    ) -> CaptureInputs:
        by_stage: dict[str, ArtifactRef] = {}
        for artifact in command.input_artifacts:
            if artifact.producer_stage not in {"freeze", "discover"}:
                raise ProtocolError("capture.input_stage", {"producer_stage": artifact.producer_stage})
            if artifact.producer_stage in by_stage:
                raise ProtocolError("capture.input_duplicate", {"producer_stage": artifact.producer_stage})
            by_stage[artifact.producer_stage] = artifact
        if set(by_stage) != {"freeze", "discover"}:
            raise ProtocolError("capture.input_incomplete", {"stages": sorted(by_stage)})
        freeze = by_stage["freeze"]
        discover = by_stage["discover"]
        if freeze.producer_run_id is None or discover.producer_run_id is None:
            raise ProtocolError(
                "capture.producer_missing",
                {"artifact_ids": [freeze.artifact_id, discover.artifact_id]},
            )

        roots = {}
        for artifact in (freeze, discover):
            root = workspace.inputs.get(artifact.artifact_id)
            if not isinstance(root, Path):
                raise ProtocolError("capture.input_not_materialized", {"artifact_id": artifact.artifact_id})
            if root.is_symlink() or root.is_junction() or not root.is_dir():
                raise ProtocolError("capture.input_root_invalid", {"artifact_id": artifact.artifact_id})
            roots[artifact.artifact_id] = root

        package = roots[discover.artifact_id]
        prepared_files = package_inventory(package)
        if digest(prepared_files) != request.prepared_sha256:
            raise ProtocolError("capture.prepared_changed", {"prepared_sha256": request.prepared_sha256})

        from .. import steps

        _source, raw_plan = steps._consume_sealed_definition(package)
        plan = dict(raw_plan)
        discovery = read_data(package / DISCOVERY_FILE)
        if not isinstance(discovery, dict) or discovery.get("run_id") != discover.producer_run_id:
            raise ProtocolError(
                "capture.producer_mismatch",
                {"artifact_run_id": discover.producer_run_id, "discovery_run_id": discovery.get("run_id")},
            )
        if plan.get("run_id") != discover.producer_run_id:
            raise ProtocolError("capture.plan_producer_mismatch", {"plan_run_id": plan.get("run_id")})
        if plan.get("handoff_sha256") != request.handoff_sha256:
            raise ProtocolError("capture.handoff_mismatch", {"handoff_sha256": request.handoff_sha256})
        if plan.get("main_assembly") != request.main_assembly:
            raise ProtocolError("capture.main_mismatch", {"main_assembly": request.main_assembly})

        record = discovery.get("raw") if isinstance(discovery.get("raw"), dict) else {}
        components = [row for row in (record.get("components") or []) if isinstance(row, dict)]
        active = [native_component_identity(row) for row in components if not row.get("suppressed")]
        planned = [str(row.get("name") or "") for row in plan.get("objects") or [] if isinstance(row, dict)]
        if not planned or any(not name for name in planned):
            raise ProtocolError("capture.objects_invalid", {"where": "plan.objects"})
        if len(set(active)) != len(active) or len(set(planned)) != len(planned):
            raise ProtocolError("capture.objects_duplicate", {"where": "active-or-plan"})
        if set(active) != set(planned):
            raise ProtocolError(
                "capture.objects_incomplete",
                {"missing": sorted(set(active) - set(planned)), "extra": sorted(set(planned) - set(active))},
            )

        return cls(
            freeze=freeze,
            discover=discover,
            freeze_root=roots[freeze.artifact_id],
            package=package,
            prepared_files=prepared_files,
            plan=plan,
            object_names=tuple(sorted(active)),
        )


@dataclass(frozen=True, slots=True)
class NativeCaptureResult:
    """Native operation outcome before the common engine seals a StageResult."""

    outcome: NativeOutcome
    output: Path | None
    archive: Path | None
    receipt: Mapping[str, Any]
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "failed", "cancelled", "unknown"}:
            raise ProtocolError("capture.native_outcome", {"outcome": self.outcome})
        if self.outcome in {"failed", "cancelled"} and not self.reason_code:
            raise ProtocolError("capture.native_reason", {"outcome": self.outcome})


def _execution_record(command: StageCommand, claim: ExecutionClaim, request: CaptureRequest) -> dict[str, Any]:
    if claim.command_id != command.command_id or claim.owner_sha256 != command.expected_executor_sha256:
        raise ProtocolError("capture.claim_mismatch", {"command_id": command.command_id})
    return {
        "claim_id": claim.claim_id,
        "command_id": command.command_id,
        "effect_id": request.effect_id,
        "executor_sha256": claim.owner_sha256,
        "fence": claim.fence,
        "run_id": command.run_id,
    }


def _upstream_records(inputs: CaptureInputs) -> tuple[dict[str, Any], ...]:
    return tuple(
        artifact.to_record()
        for artifact in sorted(
            (inputs.freeze, inputs.discover),
            key=lambda item: (str(item.producer_stage), item.artifact_id),
        )
    )


def _native_tool_record() -> dict[str, Any]:
    from ..runtime import tool_record

    return tool_record(role="native")


def capture_native_transaction(
    package: Path,
    output: Path,
    *,
    backend=None,
    run_id: str,
    on_event=None,
    on_activity=None,
    prior_events=None,
    expected_inputs: Mapping[str, str] | None = None,
    handoff_sha256: str,
    upload_sha256: str | None = None,
    main_assembly: str,
    resume: Mapping[str, Any] | None = None,
    upstream_artifacts: tuple[dict[str, Any], ...] | None = None,
    execution: Mapping[str, Any] | None = None,
    native_tool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the real native Capture transaction and seal its artifact.

    This is the shared implementation behind both the staged handler and the
    endpoint adapter used by the currently deployed integration.
    """

    from .. import solidworks
    from ..delivery import writable_output
    from ..steps import capture_evidence

    package = Path(package)
    if package.is_symlink() or package.is_junction():
        raise PipelineError("The input package cannot be a symlink or junction")
    package = package.resolve()
    output = Path(output)
    if output.resolve().is_relative_to(package) or package.is_relative_to(output.resolve()):
        raise PipelineError("Input and output directories must be separate")
    with solidworks.output_lock(output) as output:
        if not writable_output(output):
            raise PipelineError(f"Output must be absent or empty: {output}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-capture-", dir=output.parent))
        receipt = solidworks._stage_receipt(
            "capture",
            run_id,
            prior_events,
            resume=dict(resume) if resume is not None else None,
            handoff_sha256=handoff_sha256,
        )
        event = solidworks._event_sink(receipt, on_event)
        try:
            _, input_report = capture_evidence(
                package,
                staging,
                backend=backend,
                on_event=event,
                on_activity=on_activity,
                expected_inputs=dict(expected_inputs) if expected_inputs is not None else None,
                handoff_sha256=handoff_sha256,
            )
            receipt["cad_revision"] = (input_report["cad_revision"] or {}).get("revision")
            return solidworks._finish_native_capture(
                staging,
                output,
                receipt,
                run_id=receipt["run_id"],
                handoff_sha256=handoff_sha256,
                upload_sha256=upload_sha256,
                main_assembly=main_assembly,
                upstream_artifacts=upstream_artifacts,
                execution=dict(execution) if execution is not None else None,
                native_tool=native_tool,
            )
        except Exception as error:
            return solidworks._failed_run(staging, output, receipt, event, error)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _invoke_native_operation(
    inputs: CaptureInputs,
    output: Path,
    *,
    backend,
    run_id: str,
    handoff_sha256: str,
    upload_sha256: str,
    main_assembly: str,
    upstream_artifacts: tuple[dict[str, Any], ...],
    execution: dict[str, Any],
    native_tool: dict[str, Any],
) -> NativeCaptureResult:
    """Call the production native reader, sealer and receipt writer."""

    result = capture_native_transaction(
        inputs.package,
        output,
        backend=backend,
        run_id=run_id,
        handoff_sha256=handoff_sha256,
        upload_sha256=upload_sha256,
        main_assembly=main_assembly,
        expected_inputs=inputs.prepared_files,
        prior_events=(),
        upstream_artifacts=upstream_artifacts,
        execution=execution,
        native_tool=native_tool,
    )
    if result.get("native_complete") is True:
        archive_receipt = result.get("capture_archive")
        archive = output / str((archive_receipt or {}).get("name") or "")
        return NativeCaptureResult("completed", output, archive, result)
    if result.get("state") == "failed":
        reason = str(result.get("error_code") or "capture.native_failed")
        if reason in {"cad_process_cleanup_failed", "cad_session_cleanup_failed"}:
            return NativeCaptureResult("unknown", None, None, result, reason)
        return NativeCaptureResult("failed", Path(result["diagnostic_path"]), None, result, reason)
    return NativeCaptureResult("unknown", None, None, result, "capture.native_unknown")


class CaptureHandler:
    """The sole Capture domain handler used by the shared stage engine."""

    def __init__(
        self,
        *,
        backend_factory: Callable[[Path], object] | None = None,
        native_operation: Callable[..., NativeCaptureResult] | None = None,
        admitter: Callable[..., dict[str, Any]] | None = None,
        native_tool_factory: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._backend_factory = backend_factory
        self._native_operation = native_operation or _invoke_native_operation
        self._native_tool_factory = native_tool_factory or _native_tool_record
        if admitter is None:
            from ..orchestration.stage_transfer import admit_capture

            admitter = admit_capture
        self._admitter = admitter

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
    ) -> StageExecution:
        request = CaptureRequest.from_command(command)
        inputs = CaptureInputs.resolve(command, workspace, request)
        execution = _execution_record(command, claim, request)
        upstream = _upstream_records(inputs)
        tool = self._native_tool_factory()
        backend = self._backend_factory(inputs.package) if self._backend_factory is not None else None
        output = workspace.root / "capture-native"
        native = self._native_operation(
            inputs,
            output,
            backend=backend,
            run_id=command.run_id,
            handoff_sha256=request.handoff_sha256,
            upload_sha256=request.upload_sha256,
            main_assembly=request.main_assembly,
            upstream_artifacts=upstream,
            execution=execution,
            native_tool=tool,
        )
        required_effects = (request.effect_id,)
        if native.outcome == "unknown":
            raise CaptureUnresolved(native.reason_code or "capture.native_unknown")
        if native.outcome in {"failed", "cancelled"}:
            if native.output is None or not native.output.exists():
                raise ProtocolError("capture.failure_evidence_missing", {"outcome": native.outcome})
            checks = digest(
                {
                    "effect_id": request.effect_id,
                    "objects": inputs.object_names,
                    "outcome": native.outcome,
                    "reason_code": native.reason_code,
                }
            )
            return StageExecution(
                outcome=native.outcome,
                candidate_root=str(native.output),
                checks_sha256=checks,
                required_effect_ids=required_effects,
                reason_code=native.reason_code,
            )
        if native.output is None or native.archive is None or not native.archive.is_file():
            raise ProtocolError("capture.native_artifact_missing", {"output": str(native.output)})

        admitted = workspace.root / "capture-admitted"
        manifest = self._admitter(
            native.archive,
            admitted,
            expected_run_id=command.run_id,
            expected_handoff_sha256=request.handoff_sha256,
            expected_upload_sha256=request.upload_sha256,
            expected_main_assembly=request.main_assembly,
            expected_native_tool=tool,
            expected_prepared_dir=str(inputs.package),
            expected_prepared_files=dict(inputs.prepared_files),
            expected_upstream_artifacts=upstream,
            expected_execution=execution,
        )
        checks = digest(
            {
                "effect_id": request.effect_id,
                "execution": execution,
                "manifest": manifest,
                "objects": inputs.object_names,
                "upstream_artifacts": upstream,
            }
        )
        return StageExecution(
            outcome="completed",
            candidate_root=str(admitted),
            checks_sha256=checks,
            required_effect_ids=required_effects,
        )

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
    ) -> StageCandidate:
        request = CaptureRequest.from_command(command)
        result = candidate.result
        if (
            result.command_id != command.command_id
            or result.run_id != command.run_id
            or result.stage != "capture"
            or result.request_digest != command.request_digest
            or result.actual_executor_sha256 != claim.owner_sha256
        ):
            raise ProtocolError("capture.candidate_identity", {"command_id": command.command_id})
        if request.effect_id not in result.required_effect_ids:
            raise ProtocolError("capture.effect_missing", {"effect_id": request.effect_id})
        artifact = result.result_artifact
        if (
            artifact.producer_run_id != command.run_id
            or artifact.producer_stage != "capture"
            or artifact.producer_tool_sha256 != result.actual_executor_sha256
        ):
            raise ProtocolError("capture.candidate_artifact", {"artifact_id": artifact.artifact_id})
        return candidate
