"""Canonical Generate stage over accepted capture artifacts."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from ...artifacts import ArtifactStore
from ...io import PipelineError, confined, digest, file_digest, inventory, read_data, write_json
from ...model import EngineeringModel, compile, emit
from ...runtime import tool_record
from ...schemas import ENGINEERING_MODEL_SCHEMA, GENERATE_REQUEST_SCHEMA, GENERATE_RESULT_SCHEMA, TOOL_SCHEMA
from ...sources.snapshot import verify_snapshot
from ...sources.solidworks.captured import derive_engineering_input
from ..contracts import (
    ArtifactRef,
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageExecution,
    StageResult,
)
from ..effects import StageEffectPort
from ..engine import StageWorkspace
from ..generate_contract import (
    CAPTURE_ARTIFACT_KIND,
    GENERATE_OUTPUT_CONTRACT,
    GENERATE_RECEIPT,
)


@dataclass(frozen=True, slots=True)
class _CaptureInput:
    artifact: ArtifactRef
    root: Path


def _capture_ref(command: StageCommand) -> ArtifactRef:
    request = dict(command.request)
    if set(request) != {"schema_version", "capture_artifact_id", "output_contract"}:
        raise ProtocolError("generate.request_shape", {"command_id": command.command_id})
    if request["schema_version"] != GENERATE_REQUEST_SCHEMA:
        raise ProtocolError("generate.request_schema", {"value": request["schema_version"]})
    if request["output_contract"] != GENERATE_OUTPUT_CONTRACT:
        raise ProtocolError("generate.output_contract", {"value": request["output_contract"]})
    artifact_id = request["capture_artifact_id"]
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ProtocolError("generate.capture_ref", {"command_id": command.command_id})
    candidates = [item for item in command.input_artifacts if item.artifact_id == artifact_id]
    if len(candidates) != 1:
        raise ProtocolError("generate.capture_ref", {"capture_artifact_id": artifact_id})
    artifact = candidates[0]
    if artifact.kind != CAPTURE_ARTIFACT_KIND or artifact.producer_stage != "capture":
        raise ProtocolError("generate.capture_ref", {"artifact_id": artifact_id, "kind": artifact.kind})
    return artifact


def _capture_input(command: StageCommand, workspace: StageWorkspace) -> _CaptureInput:
    artifact = _capture_ref(command)
    if set(workspace.inputs) != {artifact.artifact_id}:
        raise ProtocolError("generate.input_closure", {"artifact_id": artifact.artifact_id})
    root = Path(workspace.inputs[artifact.artifact_id])
    if not root.is_dir() or root.is_symlink():
        raise ProtocolError("generate.input_missing", {"artifact_id": artifact.artifact_id})
    return _CaptureInput(artifact=artifact, root=root)


def _compile_capture(capture: _CaptureInput) -> EngineeringModel:
    """Compile directly from one accepted capture artifact without legacy adapters."""

    evidence_root = capture.root / "evidence"
    manifest = verify_snapshot(evidence_root)
    model_input = derive_engineering_input(evidence_root, manifest)
    return compile(model_input)


def _actual_tool(command: StageCommand, claim: ExecutionClaim) -> tuple[dict, str]:
    tool = tool_record(role="portable")
    actual = tool.get("source_sha256")
    if not isinstance(actual, str):
        raise ProtocolError("generate.executor_unavailable", {"command_id": command.command_id})
    if actual != claim.executor_sha256:
        raise ProtocolError(
            "generate.executor_mismatch",
            {"actual": actual, "claimed": claim.executor_sha256},
        )
    if actual != command.expected_executor_sha256:
        raise ProtocolError(
            "generate.executor_mismatch",
            {"actual": actual, "expected": command.expected_executor_sha256},
        )
    return tool, actual


def _receipt(
    *,
    command: StageCommand,
    capture: _CaptureInput,
    model: EngineeringModel,
    emission,
    actual_executor_sha256: str,
) -> dict:
    return {
        "schema_version": GENERATE_RESULT_SCHEMA,
        "capture_artifact": capture.artifact.to_record(),
        "config_snapshot_sha256": command.config_snapshot_sha256,
        "contract_sha256": command.contract_sha256,
        "required_executor_sha256": command.expected_executor_sha256,
        "actual_executor_sha256": actual_executor_sha256,
        "output_contract": GENERATE_OUTPUT_CONTRACT,
        "semantic_identity": model.to_dict()["semantic_identity"],
        "model_sha256": emission.model_sha256,
        "urdf_sha256": emission.urdf_sha256,
        "mesh_files": list(emission.mesh_files),
    }


def _write_output(
    *,
    command: StageCommand,
    claim: ExecutionClaim,
    capture: _CaptureInput,
    destination: Path,
) -> tuple[str, EngineeringModel]:
    tool, actual_executor = _actual_tool(command, claim)
    model = _compile_capture(capture)
    if destination.exists():
        raise PipelineError(f"Generate output already exists: {destination}")
    destination.mkdir(parents=True)
    emission = emit(model, evidence_root=capture.root / "evidence", destination=destination)
    write_json(destination / "tool.json", tool)
    receipt = _receipt(
        command=command,
        capture=capture,
        model=model,
        emission=emission,
        actual_executor_sha256=actual_executor,
    )
    write_json(destination / GENERATE_RECEIPT, receipt)
    return digest({"receipt": receipt, "files": inventory(destination)}), model


def _mesh_bindings(root: Path, receipt: dict) -> None:
    files = inventory(root)
    actual = {path: checksum for path, checksum in files.items() if path.startswith("meshes/")}
    declared_rows = receipt.get("mesh_files")
    if not isinstance(declared_rows, list) or any(
        not isinstance(item, dict) or set(item) != {"path", "sha256"} for item in declared_rows
    ):
        raise ProtocolError("generate.receipt_mesh_files", {"reason": "invalid-shape"})
    declared = {str(item["path"]): str(item["sha256"]) for item in declared_rows}
    if len(declared) != len(declared_rows) or declared != actual:
        raise ProtocolError("generate.receipt_mesh_files", {"reason": "digest-mismatch"})

    document = ET.parse(confined(root, "urdf/robot.urdf")).getroot()
    references = []
    for mesh in document.findall(".//mesh"):
        filename = mesh.get("filename")
        if not isinstance(filename, str) or not filename.startswith("../meshes/"):
            raise ProtocolError("generate.urdf_mesh_reference", {"value": filename})
        relative = PurePosixPath(filename.removeprefix("../"))
        references.append(relative.as_posix())
        if ".." in relative.parts or relative.as_posix() not in declared:
            raise ProtocolError("generate.urdf_mesh_reference", {"value": filename})
    if set(references) != set(declared):
        raise ProtocolError("generate.urdf_mesh_reference", {"reason": "incomplete-or-extra"})


def _validate_output_closure(
    *,
    root: Path,
    command: StageCommand,
    claim: ExecutionClaim,
    capture: ArtifactRef,
    result: StageResult,
) -> str:
    files = inventory(root, exclude=("manifest.json",))
    required = {"model/robot.json", "urdf/robot.urdf", "tool.json", GENERATE_RECEIPT}
    if not required <= set(files):
        raise ProtocolError("generate.output_incomplete", {"missing": sorted(required - set(files))})

    receipt = read_data(confined(root, GENERATE_RECEIPT))
    if not isinstance(receipt, dict) or receipt.get("schema_version") != GENERATE_RESULT_SCHEMA:
        raise ProtocolError("generate.receipt_invalid", {"command_id": command.command_id})
    tool = read_data(confined(root, "tool.json"))
    current_tool, actual_executor = _actual_tool(command, claim)
    if tool != current_tool or receipt.get("actual_executor_sha256") != actual_executor:
        raise ProtocolError("generate.actual_executor_mismatch", {"command_id": command.command_id})
    if (
        receipt.get("required_executor_sha256") != command.expected_executor_sha256
        or result.actual_executor_sha256 != actual_executor
        or result.output_artifact.producer_tool_sha256 != actual_executor
    ):
        raise ProtocolError("generate.executor_binding", {"command_id": command.command_id})
    if (
        receipt.get("capture_artifact") != capture.to_record()
        or receipt.get("config_snapshot_sha256") != command.config_snapshot_sha256
        or receipt.get("contract_sha256") != command.contract_sha256
        or receipt.get("output_contract") != GENERATE_OUTPUT_CONTRACT
    ):
        raise ProtocolError("generate.receipt_binding", {"command_id": command.command_id})

    model = EngineeringModel.from_dict(read_data(confined(root, "model/robot.json")))
    if receipt.get("semantic_identity") != model.to_dict().get("semantic_identity"):
        raise ProtocolError("generate.receipt_model", {"command_id": command.command_id})
    if receipt.get("model_sha256") != file_digest(confined(root, "model/robot.json")):
        raise ProtocolError("generate.receipt_model", {"reason": "digest"})
    if receipt.get("urdf_sha256") != file_digest(confined(root, "urdf/robot.urdf")):
        raise ProtocolError("generate.receipt_urdf", {"reason": "digest"})
    if tool.get("schema_version") != TOOL_SCHEMA or model.to_dict().get("schema_version") != ENGINEERING_MODEL_SCHEMA:
        raise ProtocolError("generate.output_schema", {"command_id": command.command_id})
    _mesh_bindings(root, receipt)

    checks_sha256 = digest({"receipt": receipt, "files": files})
    if checks_sha256 != result.checks_sha256:
        raise ProtocolError("generate.candidate_checks", {"command_id": command.command_id})
    return checks_sha256


class GenerateHandler:
    """Compile and emit one model from a registered capture artifact."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: StageEffectPort,
    ) -> StageExecution:
        if command.stage != "generate":
            raise ProtocolError("generate.stage_mismatch", {"stage": command.stage})
        if command.predecessor_decision_id is None and command.diagnostic_scope is None:
            raise ProtocolError("generate.predecessor_missing", {"command_id": command.command_id})
        if claim.command_id != command.command_id:
            raise ProtocolError("generate.claim_mismatch", {"command_id": command.command_id})

        capture = _capture_input(command, workspace)
        self.artifacts.verify(capture.artifact)
        output = workspace.root / "output"
        checks_sha256, _model = _write_output(
            command=command,
            claim=claim,
            capture=capture,
            destination=output,
        )
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256=checks_sha256,
            resources_settled=True,
        )

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
        *,
        effects: StageEffectPort,
    ) -> StageCandidate:
        """Validate sealed output structure and binding without recompiling it."""

        if (
            candidate.result.stage != "generate"
            or candidate.result.command_id != command.command_id
            or candidate.result.run_id != command.run_id
            or candidate.result.request_digest != command.request_digest
            or candidate.result.outcome != "completed"
            or candidate.result.output_artifact.kind != "stage_output:generate"
            or candidate.result.output_artifact.producer_run_id != command.run_id
            or candidate.result.output_artifact.producer_stage != "generate"
        ):
            raise ProtocolError("generate.candidate_identity", {"command_id": command.command_id})
        if claim.command_id != command.command_id:
            raise ProtocolError("generate.claim_mismatch", {"command_id": command.command_id})
        capture = _capture_ref(command)
        self.artifacts.verify(capture)
        self.artifacts.verify(candidate.result.output_artifact)
        with tempfile.TemporaryDirectory(prefix="generate-candidate-") as temporary:
            candidate_root = self.artifacts.materialize(
                candidate.result.output_artifact,
                Path(temporary) / "output",
            )
            _validate_output_closure(
                root=candidate_root,
                command=command,
                claim=claim,
                capture=capture,
                result=candidate.result,
            )
        return candidate
