"""Capture domain handler for the shared six-stage engine."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..delivery import BUNDLE_SCHEMA, PIPELINE_ID, writable_output
from ..interface_records import validate_request_record
from ..io import (
    PipelineError,
    acquire_process_lock,
    canonical,
    confined,
    digest,
    file_digest,
    inventory,
    read_data,
    write_json,
)
from .discover import (
    ARTIFACT_MANIFEST_SCHEMA,
    CONFIGURATION_KIND,
    DISCOVER_CHECKS_SCHEMA,
    DISCOVER_REQUEST_SCHEMA,
    DISCOVER_RESULT_SCHEMA,
    RULESET_KIND,
    SOURCE_SET_KIND,
)
from ..sources.solidworks.discovery import DISCOVERY_FILE, native_component_identity
from ..sources.solidworks.capture_package import consume_sealed_definition
from ..sources.solidworks.capture_transaction import capture_native_package
from ..sources.solidworks.revision import package_inventory
from ..sources.solidworks.selection import selection_bindings
from ..schemas import (
    CAPTURE_EFFECT_FAILURE_SCHEMA,
    NATIVE_CAPTURE_EFFECT_SCHEMA,
    NATIVE_TOOL_REQUIREMENT_KIND,
    TOOL_SCHEMA,
)
from ..stages import stage_view
from .contracts import (
    ArtifactRef,
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageExecution,
    StageFailure,
)
from .effects import EffectSettlement, EffectState, StageEffectPort
from .engine import StageWorkspace

if TYPE_CHECKING:
    from ..artifacts import ArtifactStore


NativeOutcome = Literal["completed", "failed", "cancelled", "unknown"]


class CaptureUnresolved(PipelineError):
    """The native operation has a possible external effect with no safe terminal fact."""


_CAPTURE_OUTBOUND_FIELDS = frozenset(
    {
        "command_id",
        "effect_key",
        "execution",
        "handoff_sha256",
        "main_assembly",
        "native_tool_record_sha256",
        "native_tool_source_sha256",
        "object_set_sha256",
        "objects",
        "prepared_files",
        "prepared_sha256",
        "request_digest",
        "request_evidence",
        "run_id",
        "schema_version",
        "upstream_artifacts",
        "upload_sha256",
    }
)


def _outbound_tree_sha256(record: Mapping[str, Any]) -> str:
    payload = canonical(dict(record))
    return digest(
        {
            "members": [
                {
                    "bytes": len(payload),
                    "path": "outbound.json",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ]
        }
    )


def validate_capture_effect_outbound(
    source: Path,
    command: StageCommand,
) -> dict[str, Any]:
    """Recompute one Capture effect closure without the prepared package."""

    source = Path(source)
    path = source / "outbound.json" if source.is_dir() else source
    record = read_data(path)
    if (
        not isinstance(record, dict)
        or set(record) != _CAPTURE_OUTBOUND_FIELDS
        or path.read_bytes() != canonical(record)
    ):
        raise ProtocolError("capture.effect_outbound", {"where": "outbound.json"})
    request = CaptureRequest.from_command(command)
    if (
        record.get("schema_version") != NATIVE_CAPTURE_EFFECT_SCHEMA
        or record.get("command_id") != command.command_id
        or record.get("run_id") != command.run_id
        or record.get("request_digest") != command.request_digest
        or record.get("effect_key") != request.effect_key
        or record.get("handoff_sha256") != request.handoff_sha256
        or record.get("upload_sha256") != request.upload_sha256
        or record.get("main_assembly") != request.main_assembly
        or record.get("prepared_sha256") != request.prepared_sha256
        or record.get("native_tool_source_sha256")
        != request.native_tool_source_sha256
        or record.get("native_tool_record_sha256")
        != request.native_tool_record_sha256
    ):
        raise ProtocolError("capture.effect_outbound", {"where": "request"})
    prepared_files = record.get("prepared_files")
    if (
        not isinstance(prepared_files, dict)
        or digest(prepared_files) != request.prepared_sha256
    ):
        raise ProtocolError("capture.effect_outbound", {"where": "prepared_files"})
    objects = record.get("objects")
    if (
        not isinstance(objects, list)
        or any(not isinstance(item, str) or not item for item in objects)
        or objects != sorted(objects)
        or len(set(objects)) != len(objects)
        or record.get("object_set_sha256") != digest(objects)
    ):
        raise ProtocolError("capture.effect_outbound", {"where": "objects"})
    upstream = record.get("upstream_artifacts")
    if not isinstance(upstream, list) or len(upstream) != 2:
        raise ProtocolError("capture.effect_outbound", {"where": "upstream_artifacts"})
    try:
        refs = [ArtifactRef.from_record(item) for item in upstream]
    except (PipelineError, TypeError, ValueError) as error:
        raise ProtocolError(
            "capture.effect_outbound",
            {"where": "upstream_artifacts"},
        ) from error
    command_refs = {
        item.artifact_id: item
        for item in command.input_artifacts
        if item.producer_stage in {"freeze", "discover"}
    }
    if (
        {item.producer_stage for item in refs} != {"freeze", "discover"}
        or any(command_refs.get(item.artifact_id) != item for item in refs)
    ):
        raise ProtocolError(
            "capture.effect_outbound",
            {"where": "upstream_artifacts"},
        )
    request_evidence = record.get("request_evidence")
    try:
        validate_request_record(request_evidence)
    except PipelineError as error:
        raise ProtocolError(
            "capture.effect_outbound",
            {"where": "request_evidence"},
        ) from error
    if (
        request_evidence.get("main_assembly") != request.main_assembly
        or request_evidence.get("handoff_digest") != request.upload_sha256
    ):
        raise ProtocolError(
            "capture.effect_outbound",
            {"where": "request_evidence"},
        )
    execution = record.get("execution")
    if (
        not isinstance(execution, dict)
        or execution.get("command_id") != command.command_id
        or execution.get("run_id") != command.run_id
        or execution.get("effect_key") != request.effect_key
        or execution.get("executor_sha256")
        != command.expected_executor_sha256
        or execution.get("native_tool_sha256")
        != request.native_tool_record_sha256
    ):
        raise ProtocolError("capture.effect_outbound", {"where": "execution"})
    return record


def _artifact_files(root: Path, artifact: ArtifactRef, *, role: str) -> dict[str, str]:
    """Validate one materialized ArtifactStore root and return its subject files."""

    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise ProtocolError("capture.artifact_invalid", {"role": role})
    manifest_path = root / "manifest.json"
    manifest = read_data(manifest_path) if manifest_path.is_file() else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != ARTIFACT_MANIFEST_SCHEMA
        or manifest.get("artifact_id") != artifact.artifact_id
        or manifest.get("kind") != artifact.kind
        or manifest.get("set_sha256") != artifact.set_sha256
    ):
        raise ProtocolError("capture.artifact_manifest", {"role": role})
    if manifest.get("producer") != {
        "run_id": artifact.producer_run_id,
        "stage": artifact.producer_stage,
        "tool_sha256": artifact.producer_tool_sha256,
    }:
        raise ProtocolError("capture.artifact_producer", {"role": role})
    files = {
        path.relative_to(root).as_posix(): file_digest(path)
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        if path.is_file() and path.relative_to(root).as_posix() != "manifest.json"
    }
    members = [
        {
            "bytes": confined(root, path).stat().st_size,
            "path": path,
            "sha256": checksum,
        }
        for path, checksum in files.items()
    ]
    if not files or digest({"members": members}) != artifact.set_sha256:
        raise ProtocolError("capture.artifact_changed", {"role": role})
    return files


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    """The Capture-specific part of one immutable StageCommand request."""

    native_tool_source_sha256: str
    native_tool_record_sha256: str
    native_tool_requirement_artifact_id: str
    handoff_sha256: str
    upload_sha256: str
    prepared_sha256: str
    main_assembly: str
    effect_key: str

    @classmethod
    def from_command(cls, command: StageCommand) -> CaptureRequest:
        if command.stage != "capture":
            raise ProtocolError("capture.command_stage", {"stage": command.stage})
        request = command.request
        required = {
            "effect_key",
            "handoff_sha256",
            "main_assembly",
            "native_tool_source_sha256",
            "native_tool_record_sha256",
            "native_tool_requirement_artifact_id",
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
        for key in (
            "handoff_sha256",
            "native_tool_source_sha256",
            "native_tool_record_sha256",
            "prepared_sha256",
            "upload_sha256",
        ):
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
    request_evidence: Mapping[str, Any]
    historical_native_tool: Mapping[str, Any]
    current_native_tool: Mapping[str, Any]
    native_tool_requirement: ArtifactRef

    @classmethod
    def resolve(
        cls,
        command: StageCommand,
        workspace: StageWorkspace,
        request: CaptureRequest,
    ) -> CaptureInputs:
        by_stage: dict[str, ArtifactRef] = {}
        requirements: list[ArtifactRef] = []
        for artifact in command.input_artifacts:
            if artifact.kind == NATIVE_TOOL_REQUIREMENT_KIND:
                requirements.append(artifact)
                continue
            if artifact.producer_stage not in {"freeze", "discover"}:
                raise ProtocolError("capture.input_stage", {"producer_stage": artifact.producer_stage})
            if artifact.producer_stage in by_stage:
                raise ProtocolError("capture.input_duplicate", {"producer_stage": artifact.producer_stage})
            by_stage[artifact.producer_stage] = artifact
        if len(requirements) != 1:
            raise ProtocolError(
                "capture.native_requirement_artifact",
                {"count": len(requirements)},
            )
        if set(by_stage) != {"freeze", "discover"}:
            raise ProtocolError("capture.input_incomplete", {"stages": sorted(by_stage)})
        freeze = by_stage["freeze"]
        discover = by_stage["discover"]
        requirement = requirements[0]
        if (
            requirement.producer_run_id is not None
            or requirement.producer_stage is not None
            or requirement.artifact_id
            != request.native_tool_requirement_artifact_id
        ):
            raise ProtocolError(
                "capture.native_requirement_binding",
                {
                    "artifact_id": request.native_tool_requirement_artifact_id,
                },
            )
        if freeze.producer_run_id is None or discover.producer_run_id is None:
            raise ProtocolError(
                "capture.producer_missing",
                {"artifact_ids": [freeze.artifact_id, discover.artifact_id]},
            )
        if freeze.producer_stage != "freeze" or discover.producer_stage != "discover":
            raise ProtocolError(
                "capture.producer_stage",
                {
                    "freeze": freeze.producer_stage,
                    "discover": discover.producer_stage,
                },
            )

        roots = {}
        input_artifacts = (freeze, discover, requirement)
        for artifact in input_artifacts:
            root = workspace.inputs.get(artifact.artifact_id)
            if not isinstance(root, Path):
                raise ProtocolError("capture.input_not_materialized", {"artifact_id": artifact.artifact_id})
            if root.is_symlink() or root.is_junction() or not root.is_dir():
                raise ProtocolError("capture.input_root_invalid", {"artifact_id": artifact.artifact_id})
            roots[artifact.artifact_id] = root
        expected_input_ids = {artifact.artifact_id for artifact in input_artifacts}
        if set(workspace.inputs) != expected_input_ids:
            raise ProtocolError(
                "capture.workspace_invalid",
                {
                    "missing": sorted(expected_input_ids - set(workspace.inputs)),
                    "unknown": sorted(set(workspace.inputs) - expected_input_ids),
                },
            )

        freeze_files = _artifact_files(roots[freeze.artifact_id], freeze, role="freeze")
        discover_files = _artifact_files(roots[discover.artifact_id], discover, role="discover")
        requirement_files = _artifact_files(
            roots[requirement.artifact_id],
            requirement,
            role="native-requirement",
        )
        if set(requirement_files) != {"tool.json"}:
            raise ProtocolError(
                "capture.native_requirement_artifact",
                {"files": sorted(requirement_files)},
            )
        for name in (
            "request.json",
            "stage-request.json",
            "inputs.json",
            "checks.json",
        ):
            if name not in freeze_files:
                raise ProtocolError("capture.freeze_incomplete", {"missing": name})
        for name in ("request.json", "result.json", "checks.json", "native-observation/observation.json"):
            if name not in discover_files:
                raise ProtocolError("capture.discover_incomplete", {"missing": name})

        tool_path = roots[requirement.artifact_id] / "tool.json"
        if tool_path.is_symlink() or tool_path.is_junction() or not tool_path.is_file():
            raise ProtocolError(
                "capture.native_requirement_artifact",
                {"where": "tool.json"},
            )
        current_native_tool = read_data(tool_path)
        if (
            not isinstance(current_native_tool, dict)
            or canonical(current_native_tool) != tool_path.read_bytes()
            or current_native_tool.get("schema_version") != TOOL_SCHEMA
            or current_native_tool.get("source_sha256")
            != request.native_tool_source_sha256
            or digest(current_native_tool)
            != request.native_tool_record_sha256
        ):
            raise ProtocolError(
                "capture.native_requirement_artifact",
                {"where": "tool.json"},
            )

        freeze_request = read_data(
            roots[freeze.artifact_id] / "stage-request.json"
        )
        request_evidence = read_data(
            roots[freeze.artifact_id] / "request.json"
        )
        freeze_inputs = read_data(roots[freeze.artifact_id] / "inputs.json")
        discover_request = read_data(roots[discover.artifact_id] / "request.json")
        discover_checks = read_data(roots[discover.artifact_id] / "checks.json")
        discover_result = read_data(roots[discover.artifact_id] / "result.json")
        if not all(
            isinstance(record, dict)
            for record in (
                freeze_request,
                request_evidence,
                freeze_inputs,
                discover_request,
                discover_checks,
                discover_result,
            )
        ):
            raise ProtocolError("capture.adjacent_record_invalid", {"where": "freeze-or-discover"})
        try:
            validate_request_record(request_evidence)
        except PipelineError as error:
            raise ProtocolError(
                "capture.request_evidence_invalid",
                {"where": "freeze/request.json"},
            ) from error
        if discover_request.get("schema_version") != DISCOVER_REQUEST_SCHEMA:
            raise ProtocolError("capture.discover_request_schema", {"where": "request.json"})
        if discover_checks.get("schema_version") != DISCOVER_CHECKS_SCHEMA:
            raise ProtocolError("capture.discover_checks_schema", {"where": "checks.json"})
        if (
            discover_result.get("schema_version") != DISCOVER_RESULT_SCHEMA
            or discover_result.get("run_id") != discover.producer_run_id
            or discover_result.get("command_id") != discover_checks.get("command_id")
            or discover_result.get("request_digest") != discover_checks.get("request_digest")
        ):
            raise ProtocolError("capture.discover_result_identity", {"where": "result.json"})
        native_tool = discover_result.get("native_tool")
        historical_source_sha256 = (
            native_tool.get("source_sha256")
            if isinstance(native_tool, dict)
            else None
        )
        if (
            discover_request.get("expected_native_tool_sha256") != historical_source_sha256
            or not isinstance(native_tool, dict)
            or not isinstance(historical_source_sha256, str)
        ):
            raise ProtocolError(
                "capture.native_requirement_binding",
                {
                    "historical_source": historical_source_sha256,
                },
            )
        native_check = next(
            (
                check
                for check in discover_checks.get("checks", ())
                if isinstance(check, dict)
                and check.get("id") == "native.observation"
            ),
            None,
        )
        native_details = (
            native_check.get("details")
            if isinstance(native_check, dict)
            else None
        )
        if (
            not isinstance(native_check, dict)
            or native_check.get("state") != "passed"
            or not isinstance(native_details, dict)
            or native_details.get("native_tool_sha256") != digest(native_tool)
            or native_details.get("observation_sha256")
            != discover_result.get("native_observation_sha256")
        ):
            raise ProtocolError(
                "capture.native_history_binding",
                {"where": "native.observation"},
            )
        if command.diagnostic_scope is None and (
            historical_source_sha256 != request.native_tool_source_sha256
            or digest(native_tool) != request.native_tool_record_sha256
        ):
            raise ProtocolError(
                "capture.native_requirement_binding",
                {
                    "source": request.native_tool_source_sha256,
                    "record": request.native_tool_record_sha256,
                },
            )
        if (
            discover_request.get("freeze_artifact_id") != freeze.artifact_id
            or discover_request.get("main_assembly") != request.main_assembly
            or discover_request.get("source_inventory_sha256") != request.upload_sha256
        ):
            raise ProtocolError("capture.freeze_discover_binding", {"where": "discover request"})
        if (
            freeze_request.get("main_assembly") != request.main_assembly
            or freeze_request.get("upload_inventory_sha256") != request.upload_sha256
            or request_evidence.get("main_assembly")
            != request.main_assembly
            or request_evidence.get("handoff_digest")
            != request.upload_sha256
            or request_evidence.get("request_id")
            != freeze_request.get("request_id")
            or request_evidence.get("purpose")
            != freeze_request.get("purpose")
            or request_evidence.get("target_hardware")
            != freeze_request.get("target_hardware")
            or request_evidence.get("platform_config_digest")
            != freeze_request.get("platform_config_sha256")
        ):
            raise ProtocolError("capture.freeze_request_binding", {"where": "freeze request"})
        freeze_artifacts = freeze_inputs.get("artifacts")
        if not isinstance(freeze_artifacts, dict) or set(freeze_artifacts) != {
            "originals",
            "ruleset",
            "configuration",
        }:
            raise ProtocolError("capture.freeze_inputs_invalid", {"where": "artifacts"})
        if (
            freeze_request.get("originals_artifact_id") != freeze_artifacts["originals"].get("artifact_id")
            or freeze_request.get("ruleset_artifact_id") != freeze_artifacts["ruleset"].get("artifact_id")
            or freeze_request.get("configuration_artifact_id")
            != freeze_artifacts["configuration"].get("artifact_id")
        ):
            raise ProtocolError("capture.freeze_request_binding", {"where": "freeze request"})
        for role, kind, result_key in (
            ("originals", SOURCE_SET_KIND, "source_set_artifact"),
            ("ruleset", RULESET_KIND, "ruleset_artifact"),
            ("configuration", CONFIGURATION_KIND, "configuration_artifact"),
        ):
            freeze_artifact = freeze_artifacts[role]
            discover_artifact = discover_result.get(result_key)
            if freeze_artifact != discover_artifact:
                raise ProtocolError(
                    "capture.freeze_discover_binding",
                    {"where": result_key},
                )
            try:
                bound_artifact = ArtifactRef.from_record(discover_artifact)
            except (PipelineError, TypeError, ValueError) as error:
                raise ProtocolError("capture.discover_artifact_invalid", {"where": result_key}) from error
            if bound_artifact.kind != kind:
                raise ProtocolError("capture.discover_artifact_kind", {"where": result_key})

        selection = discover_result.get("selection")
        if not isinstance(selection, dict):
            raise ProtocolError("capture.discover_selection", {"where": "selection"})
        try:
            selection_binding = selection_bindings(selection)
        except PipelineError as error:
            raise ProtocolError("capture.discover_selection", {"where": "selection"}) from error
        selected_files = {str(name): str(value) for name, value in selection_binding["files"].items()}
        if (
            digest(selected_files) != request.handoff_sha256
            or selection_binding["upload_sha256"] != request.upload_sha256
        ):
            raise ProtocolError("capture.handoff_mismatch", {"handoff_sha256": request.handoff_sha256})
        upload_inventory = freeze_inputs.get("upload_inventory")
        if not isinstance(upload_inventory, dict) or any(
            upload_inventory.get(name) != checksum for name, checksum in selected_files.items()
        ):
            raise ProtocolError("capture.selection_source_mismatch", {"where": "freeze upload inventory"})

        if discover_result.get("prepared_sha256") != request.prepared_sha256 or not isinstance(
            discover_result.get("prepared_files"),
            dict,
        ):
            raise ProtocolError("capture.prepared_binding", {"prepared_sha256": request.prepared_sha256})
        if discover_result.get("native_observation_sha256") != file_digest(
            roots[discover.artifact_id] / "native-observation/observation.json"
        ):
            raise ProtocolError("capture.native_observation_changed", {"where": "observation.json"})

        package = roots[discover.artifact_id] / "prepared"
        if not package.is_dir() or package.is_symlink() or package.is_junction():
            raise ProtocolError("capture.prepared_missing", {"path": str(package)})
        prepared_files = package_inventory(package)
        if (
            prepared_files != {str(name): str(value) for name, value in discover_result["prepared_files"].items()}
            or digest(prepared_files) != request.prepared_sha256
        ):
            raise ProtocolError("capture.prepared_changed", {"prepared_sha256": request.prepared_sha256})

        _source, raw_plan = consume_sealed_definition(package)
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
            request_evidence=dict(request_evidence),
            historical_native_tool=dict(native_tool),
            current_native_tool=dict(current_native_tool),
            native_tool_requirement=requirement,
        )


@dataclass(frozen=True, slots=True)
class NativeCaptureResult:
    """Native operation outcome before the common engine seals a StageResult."""

    outcome: NativeOutcome
    output: Path | None
    archive: Path | None
    receipt: Mapping[str, Any]
    resources_settled: bool = False
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "failed", "cancelled", "unknown"}:
            raise ProtocolError("capture.native_outcome", {"outcome": self.outcome})
        if self.outcome in {"failed", "cancelled"} and not self.reason_code:
            raise ProtocolError("capture.native_reason", {"outcome": self.outcome})
        if type(self.resources_settled) is not bool:
            raise ProtocolError("capture.native_resources", {"outcome": self.outcome})
        if self.outcome == "completed" and self.resources_settled is not True:
            raise ProtocolError("capture.native_resources", {"reason": "completed-requires-exit-proof"})


def _execution_record(
    command: StageCommand,
    claim: ExecutionClaim,
    request: CaptureRequest,
    native_tool: Mapping[str, Any],
) -> dict[str, Any]:
    if claim.command_id != command.command_id or claim.executor_sha256 != command.expected_executor_sha256:
        raise ProtocolError("capture.claim_mismatch", {"command_id": command.command_id})
    if (
        native_tool.get("source_sha256") != request.native_tool_source_sha256
        or digest(native_tool) != request.native_tool_record_sha256
    ):
        raise ProtocolError(
            "capture.native_tool_mismatch",
            {
                "expected_source": request.native_tool_source_sha256,
                "expected_record": request.native_tool_record_sha256,
                "actual": native_tool.get("source_sha256"),
            },
        )
    return {
        "claim_id": claim.claim_id,
        "command_id": command.command_id,
        "effect_key": request.effect_key,
        "executor_sha256": claim.executor_sha256,
        "fence": claim.fence,
        "native_tool_sha256": digest(native_tool),
        "owner_sha256": claim.owner_sha256,
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


@contextmanager
def _capture_output_lock(output: Path):
    """One attempt owns the destination lock for the complete native transaction."""

    if output.is_symlink() or output.is_junction():
        raise PipelineError("Output cannot be a symlink or junction")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.parent / f".{output.name}.description.lock"
    try:
        handle = acquire_process_lock(lock_path)
    except OSError as error:
        raise PipelineError(f"Another run owns {lock_path} or its ownership lock is unavailable") from error
    try:
        yield output
    finally:
        os.close(handle)


def _capture_receipt(
    run_id: str,
    prior_events,
    *,
    resume: Mapping[str, Any] | None,
    handoff_sha256: str,
    execution_scope: tuple[str, ...],
) -> dict[str, Any]:
    """Create a native-only receipt; prior events are accepted only by the legacy adapter."""

    events = [event for event in (prior_events or ()) if isinstance(event, dict)]
    receipt = {
        "schema_version": BUNDLE_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id or str(uuid.uuid4()),
        "handoff_sha256": handoff_sha256,
        "state": "failed",
        "events": events,
        "execution_scope": list(execution_scope),
    }
    if resume:
        receipt["resume"] = {
            "parent_run": resume.get("parent_run"),
            "from_stage": resume.get("from_stage"),
        }
    return receipt


def _capture_event_sink(receipt: dict[str, Any], on_event=None):
    def event(item):
        entry = {"at": datetime.now(UTC).isoformat(), **item}
        receipt.setdefault("events", []).append(entry)
        receipt["stage"] = item["stage"]
        if on_event is not None:
            on_event({**entry, "pipeline_id": PIPELINE_ID, "run_id": receipt["run_id"]})

    return event


def _install_capture_tree(staging: Path, target: Path) -> None:
    if not writable_output(target):
        raise PipelineError(f"Output must be absent or empty: {target}")
    backup = Path(tempfile.mkdtemp(prefix=f".{target.name}-previous-", dir=target.parent))
    backup.rmdir()
    moved = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved = True
        os.replace(staging, target)
    except BaseException:
        if moved and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        if backup.exists() and target.exists():
            shutil.rmtree(backup)


def _stamp_capture(root: Path, receipt: dict[str, Any]) -> None:
    receipt["artifacts"] = inventory(root, exclude=("reports/stages.json",))
    write_json(root / "reports/stages.json", stage_view(receipt))
    receipt["stage_report"] = {
        "path": "reports/stages.json",
        "sha256": file_digest(root / "reports/stages.json"),
    }


def _keep_capture_diagnostic(staging: Path, output: Path, receipt: dict[str, Any]) -> Path:
    _stamp_capture(staging, receipt)
    failed = output.with_name(output.name + ".failed")
    if not writable_output(failed):
        failed = Path(tempfile.mkdtemp(prefix=output.name + ".failed-", dir=output.parent))
        failed.rmdir()
    _install_capture_tree(staging, failed)
    return failed


def _failed_capture(staging, output, receipt, event, error):
    phase = receipt.get("stage") or "capture"
    if not receipt.get("events") or receipt["events"][-1]["state"] != "failed":
        with contextlib.suppress(Exception):
            event({"stage": phase, "state": "failed"})
    receipt.update(passed=False, state="failed", stage=phase, error=str(error))
    receipt["native_complete"] = False
    if hasattr(error, "code"):
        receipt["error_code"] = error.code
    detail = getattr(error, "detail", None) or getattr(error, "details", None)
    if detail is not None:
        receipt["detail"] = detail
    has_staging = staging is not None and staging.exists()
    root = staging if has_staging else output
    quality_path = root / "reports/quality.json"
    if quality_path.is_file():
        receipt["quality"] = read_data(quality_path)
        receipt["subject_sha256"] = receipt["quality"].get("subject_sha256")
    if has_staging:
        receipt["diagnostic_path"] = str(_keep_capture_diagnostic(staging, output, receipt))
    else:
        _stamp_capture(output, receipt)
        receipt["diagnostic_path"] = str(output)
    return receipt


def _seal_native_capture_artifact(
    staging: Path,
    output: Path,
    receipt: dict[str, Any],
    *,
    run_id: str,
    handoff_sha256: str,
    upload_sha256: str | None,
    main_assembly: str,
    upstream_artifacts=None,
    execution=None,
    native_tool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Seal the native artifact and atomically install the completed Capture tree."""

    from ..native_transfer import expected_native_check_details
    from ..orchestration.stage_transfer import CAPTURE_ARCHIVE, CAPTURE_MANIFEST, seal_capture

    upload_sha256 = upload_sha256 or handoff_sha256
    tool = native_tool or _native_tool_record()
    write_json(staging / "reports/native-tool.json", tool)
    receipt.update(
        state="native_complete",
        passed=False,
        native_complete=True,
        native_tool=tool,
        stage="capture",
        resource_settlement={
            "settled": True,
            "reason": "owned_native_sessions_released",
        },
    )
    discovery = read_data(staging / "evidence/discovery.json")
    native_files = discovery.get("native_files") if isinstance(discovery, dict) else None
    if not isinstance(native_files, dict) or not native_files:
        raise PipelineError("Native discovery has no file inventory")
    expected_details = expected_native_check_details(
        staging,
        handoff_sha256=handoff_sha256,
        main_assembly=main_assembly,
        main_assembly_sha256=file_digest(staging / "input/cad" / main_assembly),
        native_files=native_files,
    )
    for event in receipt["events"]:
        check = event.get("check") if isinstance(event, dict) else None
        if isinstance(check, dict) and check.get("id") in expected_details:
            check["details"] = expected_details[check["id"]]
    _stamp_capture(staging, receipt)
    native_stages = read_data(staging / "reports/stages.json")
    native_stages["events"] = list(receipt["events"])
    write_json(staging / "reports/native-stages.json", native_stages)
    archive = staging / CAPTURE_ARCHIVE
    manifest = seal_capture(
        staging,
        archive,
        run_id=run_id,
        handoff_sha256=handoff_sha256,
        upload_sha256=upload_sha256,
        main_assembly=main_assembly,
        native_tool=tool,
        upstream_artifacts=upstream_artifacts,
        execution=execution,
    )
    receipt["upload_sha256"] = upload_sha256
    write_json(staging / CAPTURE_MANIFEST, manifest)
    receipt["capture_archive"] = {
        "name": CAPTURE_ARCHIVE,
        "sha256": file_digest(archive),
        "size": archive.stat().st_size,
        "manifest_sha256": file_digest(staging / CAPTURE_MANIFEST),
    }
    _stamp_capture(staging, receipt)
    _install_capture_tree(staging, output)
    return {**receipt, "output": str(output)}


def _capture_native_transaction(
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
    native_tool_factory: Callable[[], dict[str, Any]] | None = None,
    execution_scope: tuple[str, ...] = ("capture",),
    request_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the real native Capture transaction and seal its artifact.

    This is the shared implementation behind both the staged handler and the
    endpoint adapter used by the currently deployed integration.
    """

    package = Path(package)
    if package.is_symlink() or package.is_junction():
        raise PipelineError("The input package cannot be a symlink or junction")
    package = package.resolve()
    output = Path(output)
    if output.resolve().is_relative_to(package) or package.is_relative_to(output.resolve()):
        raise PipelineError("Input and output directories must be separate")
    with _capture_output_lock(output) as output:
        if not writable_output(output):
            raise PipelineError(f"Output must be absent or empty: {output}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-capture-", dir=output.parent))
        receipt = _capture_receipt(
            run_id,
            prior_events,
            resume=dict(resume) if resume is not None else None,
            handoff_sha256=handoff_sha256,
            execution_scope=execution_scope,
        )
        event = _capture_event_sink(receipt, on_event)
        try:
            if request_evidence is not None:
                validate_request_record(request_evidence)
                request_path = staging / "input/request.json"
                request_path.parent.mkdir(parents=True, exist_ok=True)
                request_path.write_bytes(
                    canonical(dict(request_evidence))
                )
            _, input_report = capture_native_package(
                package,
                staging,
                backend=backend,
                on_event=event,
                on_activity=on_activity,
                expected_inputs=dict(expected_inputs) if expected_inputs is not None else None,
                handoff_sha256=handoff_sha256,
            )
            receipt["cad_revision"] = (input_report["cad_revision"] or {}).get("revision")
            tool = (native_tool_factory or _native_tool_record)()
            return _seal_native_capture_artifact(
                staging,
                output,
                receipt,
                run_id=receipt["run_id"],
                handoff_sha256=handoff_sha256,
                upload_sha256=upload_sha256,
                main_assembly=main_assembly,
                upstream_artifacts=upstream_artifacts,
                execution=dict(execution) if execution is not None else None,
                native_tool=tool,
            )
        except Exception as error:
            return _failed_capture(staging, output, receipt, event, error)


def _invoke_native_operation(
    inputs: CaptureInputs,
    output: Path,
    *,
    backend,
    run_id: str,
    request_digest: str,
    handoff_sha256: str,
    upload_sha256: str,
    main_assembly: str,
    upstream_artifacts: tuple[dict[str, Any], ...],
    execution: dict[str, Any],
    native_tool: dict[str, Any],
) -> NativeCaptureResult:
    """Call the production native reader, sealer and receipt writer."""

    del request_digest
    result = _capture_native_transaction(
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
        native_tool_factory=lambda: native_tool,
        request_evidence=inputs.request_evidence,
    )
    if result.get("native_complete") is True:
        archive_receipt = result.get("capture_archive")
        archive = output / str((archive_receipt or {}).get("name") or "")
        return NativeCaptureResult(
            "completed",
            output,
            archive,
            result,
            result.get("resource_settlement", {}).get("settled") is True,
        )
    if result.get("state") == "failed":
        reason = str(result.get("error_code") or "capture.native_failed")
        if reason in {"cad_process_cleanup_failed", "cad_session_cleanup_failed"}:
            return NativeCaptureResult("unknown", None, None, result, False, reason)
        return NativeCaptureResult(
            "failed",
            Path(result["diagnostic_path"]),
            None,
            result,
            False,
            reason,
        )
    return NativeCaptureResult("unknown", None, None, result, False, "capture.native_unknown")


class CaptureHandler:
    """The sole Capture domain handler used by the shared stage engine."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore | None = None,
        backend_factory: Callable[[Path], object] | None = None,
        native_operation: Callable[..., NativeCaptureResult] | None = None,
        admitter: Callable[..., dict[str, Any]] | None = None,
        native_tool_factory: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._backend_factory = backend_factory
        self._native_operation = native_operation or _invoke_native_operation
        self._native_tool_factory = native_tool_factory
        if admitter is None:
            from ..orchestration.stage_transfer import admit_capture

            admitter = admit_capture
        self._admitter = admitter

    @staticmethod
    def _outbound_record(
        command: StageCommand,
        request: CaptureRequest,
        inputs: CaptureInputs,
        execution: Mapping[str, Any],
    ) -> dict[str, Any]:
        objects = list(inputs.object_names)
        prepared_files = dict(sorted(inputs.prepared_files.items()))
        return {
            "schema_version": NATIVE_CAPTURE_EFFECT_SCHEMA,
            "command_id": command.command_id,
            "effect_key": request.effect_key,
            "execution": dict(execution),
            "handoff_sha256": request.handoff_sha256,
            "main_assembly": request.main_assembly,
            "native_tool_record_sha256": (
                request.native_tool_record_sha256
            ),
            "native_tool_source_sha256": (
                request.native_tool_source_sha256
            ),
            "object_set_sha256": digest(objects),
            "objects": objects,
            "prepared_files": prepared_files,
            "prepared_sha256": request.prepared_sha256,
            "request_digest": command.request_digest,
            "request_evidence": dict(inputs.request_evidence),
            "run_id": command.run_id,
            "upstream_artifacts": list(_upstream_records(inputs)),
            "upload_sha256": request.upload_sha256,
        }

    def _effect_state(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        request: CaptureRequest,
        inputs: CaptureInputs,
        tool: Mapping[str, Any],
        effects: StageEffectPort,
    ) -> tuple[str, EffectSettlement | None]:
        execution = _execution_record(command, claim, request, tool)
        outbound = self._outbound_record(
            command,
            request,
            inputs,
            execution,
        )
        outbound_sha256 = _outbound_tree_sha256(outbound)
        expected_state = {
            "command_id": command.command_id,
            "effect_key": request.effect_key,
            "handoff_sha256": request.handoff_sha256,
            "main_assembly": request.main_assembly,
            "native_tool_record_sha256": (
                request.native_tool_record_sha256
            ),
            "native_tool_source_sha256": (
                request.native_tool_source_sha256
            ),
            "object_set_sha256": outbound["object_set_sha256"],
            "outbound_sha256": outbound_sha256,
            "prepared_sha256": request.prepared_sha256,
            "request_digest": command.request_digest,
            "schema_version": NATIVE_CAPTURE_EFFECT_SCHEMA,
            "upload_sha256": request.upload_sha256,
        }
        existing = effects.effect_settlement_for_key(request.effect_key)
        if existing is not None:
            if (
                self._artifact_store is None
                or existing.outbound_artifact is None
            ):
                raise ProtocolError(
                    "capture.effect_outbound_unavailable",
                    {"effect_key": request.effect_key},
                )
            with tempfile.TemporaryDirectory(
                prefix="capture-existing-outbound-",
                dir=workspace.root,
            ) as directory:
                existing_root = self._artifact_store.materialize(
                    existing.outbound_artifact,
                    Path(directory) / "artifact",
                )
                existing_outbound = validate_capture_effect_outbound(
                    existing_root,
                    command,
                )
            stable_keys = _CAPTURE_OUTBOUND_FIELDS - {"execution"}
            if digest(
                {
                    key: existing_outbound[key]
                    for key in stable_keys
                }
            ) != digest(
                {key: outbound[key] for key in stable_keys}
            ):
                raise ProtocolError(
                    "capture.effect_identity_conflict",
                    {"effect_key": request.effect_key},
                )
            if existing.state == EffectState.SATISFIED:
                return existing.effect_id, existing
            if existing.state in {EffectState.UNKNOWN, EffectState.CONFLICT}:
                raise CaptureUnresolved(f"capture.effect_{existing.state.value}")
            if existing.state == EffectState.DEFINITELY_FAILED:
                return existing.effect_id, existing
            if existing.state != EffectState.PREPARED:
                raise ProtocolError("capture.effect_state", {"state": existing.state.value})
            effects.authorize_dispatch(existing.effect_id, source="capture-handler")
            return existing.effect_id, existing

        outbound_root = workspace.root / "capture-effect-outbound"
        outbound_root.mkdir(parents=True, exist_ok=False)
        write_json(outbound_root / "outbound.json", outbound)
        intent = effects.prepare_effect(
            effect_key=request.effect_key,
            effect_kind="native_operation",
            target={
                "command_id": command.command_id,
                "host": "windows",
                "run_id": command.run_id,
                "stage": "capture",
            },
            expected_state=expected_state,
            outbound_root=outbound_root,
        )
        effects.authorize_dispatch(intent.effect_id, source="capture-handler")
        return intent.effect_id, None

    def _admit_effect_payload(
        self,
        *,
        payload_root: Path,
        command: StageCommand,
        claim: ExecutionClaim,
        request: CaptureRequest,
        inputs: CaptureInputs,
    ) -> StageExecution:
        execution_record = read_data(payload_root / "execution.json")
        tool = read_data(payload_root / "native-tool.json")
        archive = payload_root / "native-evidence.zip"
        if not isinstance(execution_record, dict) or not isinstance(tool, dict) or not archive.is_file():
            raise ProtocolError("capture.effect_payload", {"effect_key": request.effect_key})
        if (
            execution_record.get("command_id") != command.command_id
            or execution_record.get("run_id") != command.run_id
            or execution_record.get("executor_sha256") != claim.executor_sha256
            or tool.get("source_sha256") != request.native_tool_source_sha256
            or digest(tool) != request.native_tool_record_sha256
            or execution_record.get("native_tool_sha256") != digest(tool)
        ):
            raise ProtocolError("capture.effect_identity", {"effect_key": request.effect_key})
        admitted = payload_root / "admitted"
        manifest = self._admitter(
            archive,
            admitted,
            expected_run_id=command.run_id,
            expected_handoff_sha256=request.handoff_sha256,
            expected_upload_sha256=request.upload_sha256,
            expected_main_assembly=request.main_assembly,
            expected_native_tool=tool,
            expected_prepared_dir=None,
            expected_prepared_files=dict(inputs.prepared_files),
            expected_upstream_artifacts=_upstream_records(inputs),
            expected_execution=execution_record,
        )
        checks = digest(
            {
                "effect_key": request.effect_key,
                "manifest": manifest,
                "object_set_sha256": digest(list(inputs.object_names)),
                "upstream_artifacts": _upstream_records(inputs),
            }
        )
        return StageExecution(
            outcome="completed",
            candidate_root=str(admitted),
            checks_sha256=checks,
            resources_settled=True,
        )

    def _reuse_satisfied_effect(
        self,
        *,
        settlement: EffectSettlement,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        request: CaptureRequest,
        inputs: CaptureInputs,
    ) -> StageExecution:
        if self._artifact_store is None or settlement.payload_artifact is None:
            raise ProtocolError("capture.effect_payload_unavailable", {"effect_key": request.effect_key})
        payload_root = self._artifact_store.materialize(
            settlement.payload_artifact,
            workspace.root / "satisfied-effect-payload",
        )
        return self._admit_effect_payload(
            payload_root=payload_root,
            command=command,
            claim=claim,
            request=request,
            inputs=inputs,
        )

    @staticmethod
    def _effect_failure_root(
        workspace: StageWorkspace,
        settlement: EffectSettlement,
        reason_code: str,
    ) -> Path:
        root = workspace.root / "capture-effect-failure"
        root.mkdir(parents=True, exist_ok=False)
        payload = {
            "effect_id": settlement.effect_id,
            "reason_code": reason_code,
            "schema_version": CAPTURE_EFFECT_FAILURE_SCHEMA,
            "state": settlement.state.value,
        }
        write_json(root / "failure.json", payload)
        return root

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: StageEffectPort,
    ) -> StageExecution:
        request = CaptureRequest.from_command(command)
        inputs = CaptureInputs.resolve(command, workspace, request)
        upstream = _upstream_records(inputs)
        if self._native_tool_factory is None:
            raise ProtocolError(
                "capture.native_tool_provider_missing",
                {"command_id": command.command_id},
            )
        tool = self._native_tool_factory()
        if dict(tool) != dict(inputs.current_native_tool):
            raise ProtocolError(
                "capture.native_tool_mismatch",
                {"where": "current-native-tool"},
            )
        execution = _execution_record(command, claim, request, tool)
        effect_id, settlement = self._effect_state(
            command,
            claim,
            workspace,
            request,
            inputs,
            tool,
            effects,
        )
        if settlement is not None and settlement.state == EffectState.SATISFIED:
            return self._reuse_satisfied_effect(
                settlement=settlement,
                command=command,
                claim=claim,
                workspace=workspace,
                request=request,
                inputs=inputs,
            )
        if settlement is not None and settlement.state == EffectState.DEFINITELY_FAILED:
            root = self._effect_failure_root(workspace, settlement, "capture.effect_definitely_failed")
            return StageExecution(
                outcome="failed",
                candidate_root=str(root),
                checks_sha256=digest(read_data(root / "failure.json")),
                resources_settled=True,
                failure=StageFailure(
                    "capture.effect_definitely_failed",
                    "The Capture effect is definitely failed and will not be resent",
                ),
            )
        backend = self._backend_factory(inputs.package) if self._backend_factory is not None else None
        output = workspace.root / "capture-native"
        native = self._native_operation(
            inputs,
            output,
            backend=backend,
            run_id=command.run_id,
            request_digest=command.request_digest,
            handoff_sha256=request.handoff_sha256,
            upload_sha256=request.upload_sha256,
            main_assembly=request.main_assembly,
            upstream_artifacts=upstream,
            execution=execution,
            native_tool=tool,
        )
        if native.outcome == "unknown":
            timeout_root = workspace.root / "capture-effect-unknown"
            timeout_root.mkdir(parents=True, exist_ok=False)
            write_json(timeout_root / "native-result.json", dict(native.receipt))
            effects.record_timeout(effect_id, source="capture-handler", detail_root=timeout_root)
            effects.record_observation(
                effect_id,
                source="capture-handler",
                state_class="unobservable",
                observation_root=timeout_root,
            )
            raise CaptureUnresolved(native.reason_code or "capture.native_unknown")
        if native.outcome in {"failed", "cancelled"}:
            if native.output is None or not native.output.exists():
                raise ProtocolError("capture.failure_evidence_missing", {"outcome": native.outcome})
            effects.record_response(effect_id, source="capture-handler", response_root=native.output)
            if not native.resources_settled:
                effects.record_observation(
                    effect_id,
                    source="capture-handler",
                    state_class="unobservable",
                    observation_root=native.output,
                )
                raise CaptureUnresolved(native.reason_code or "capture.native_resources_unknown")
            effects.reconcile_effect(
                effect_id,
                source="capture-handler",
                state_class="absent",
                evidence_root=native.output,
            )
            checks = digest(
                {
                    "effect_id": effect_id,
                    "objects": inputs.object_names,
                    "outcome": native.outcome,
                    "reason_code": native.reason_code,
                }
            )
            return StageExecution(
                outcome=native.outcome,
                candidate_root=str(native.output),
                checks_sha256=checks,
                resources_settled=native.resources_settled,
                failure=StageFailure(
                    native.reason_code or "capture.native_failed",
                    f"Native Capture ended with {native.outcome}",
                    outcome=native.outcome,
                    retained_locations=(str(native.output),),
                ),
            )
        if native.output is None or native.archive is None or not native.archive.is_file():
            raise ProtocolError("capture.native_artifact_missing", {"output": str(native.output)})
        effect_payload = workspace.root / "capture-effect-payload"
        effect_payload.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(native.archive, effect_payload / "native-evidence.zip")
        write_json(effect_payload / "execution.json", execution)
        write_json(effect_payload / "native-tool.json", tool)
        effects.record_response(effect_id, source="capture-handler", response_root=effect_payload)
        effects.record_observation(
            effect_id,
            source="capture-handler",
            state_class="known",
            observation_root=effect_payload,
        )
        return self._admit_effect_payload(
            payload_root=effect_payload,
            command=command,
            claim=claim,
            request=request,
            inputs=inputs,
        )

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
        *,
        effects: StageEffectPort,
    ) -> StageCandidate:
        request = CaptureRequest.from_command(command)
        result = candidate.result
        if (
            result.command_id != command.command_id
            or result.run_id != command.run_id
            or result.stage != "capture"
            or result.request_digest != command.request_digest
            or result.actual_executor_sha256 != claim.executor_sha256
        ):
            raise ProtocolError("capture.candidate_identity", {"command_id": command.command_id})
        settlement = effects.effect_settlement_for_key(request.effect_key)
        expected_state = (
            EffectState.SATISFIED
            if result.outcome == "completed"
            else EffectState.DEFINITELY_FAILED
        )
        if settlement is None or settlement.state != expected_state:
            raise ProtocolError("capture.candidate_effect", {"effect_key": request.effect_key})
        artifact = result.output_artifact
        if (
            artifact.producer_run_id != command.run_id
            or artifact.producer_stage != "capture"
            or artifact.producer_tool_sha256 != result.actual_executor_sha256
        ):
            raise ProtocolError("capture.candidate_artifact", {"artifact_id": artifact.artifact_id})
        return candidate
