"""Canonical Generate stage over accepted capture artifacts."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ...artifacts import ArtifactStore
from ...io import PipelineError, digest, inventory, read_data, write_json
from ...model import EngineeringModel, compile, emit
from ...runtime import tool_record
from ...sources.snapshot import verify_snapshot
from ...sources.solidworks.captured import derive_engineering_input
from ...sources.solidworks.generation_warnings import generation_value_warnings
from ..contracts import (
    ArtifactRef,
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageExecution,
)
from ..engine import StageWorkspace

GENERATE_REQUEST_SCHEMA = "description-pipeline.generate-request/v1"
GENERATE_OUTPUT_CONTRACT = "solidworks-to-urdf.generated-artifact/v1"
CAPTURE_ARTIFACT_KIND = "stage_output:capture"
GENERATE_RECEIPT = "reports/generate.json"


@dataclass(frozen=True, slots=True)
class _CaptureInput:
    artifact: ArtifactRef
    root: Path


def _request_capture(command: StageCommand, workspace: StageWorkspace) -> _CaptureInput:
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
    if set(workspace.inputs) != {artifact_id}:
        raise ProtocolError("generate.input_closure", {"artifact_id": artifact_id})
    root = Path(workspace.inputs[artifact_id])
    if not root.is_dir() or root.is_symlink():
        raise ProtocolError("generate.input_missing", {"artifact_id": artifact_id})
    return _CaptureInput(artifact=artifact, root=root)


def _compile_capture(capture: _CaptureInput) -> EngineeringModel:
    """Compile directly from one accepted capture artifact without legacy adapters."""

    evidence_root = capture.root / "evidence"
    manifest = verify_snapshot(evidence_root)
    model_input = derive_engineering_input(evidence_root, manifest)
    warnings = generation_value_warnings(capture.root)
    return compile(model_input, native_warnings=warnings)


def _receipt(
    *,
    command: StageCommand,
    capture: _CaptureInput,
    model: EngineeringModel,
    emission,
) -> dict:
    return {
        "schema_version": "description-pipeline.generate-result/v1",
        "capture_artifact": capture.artifact.to_record(),
        "config_snapshot_sha256": command.config_snapshot_sha256,
        "contract_sha256": command.contract_sha256,
        "executor_sha256": command.expected_executor_sha256,
        "output_contract": GENERATE_OUTPUT_CONTRACT,
        "semantic_identity": model.to_dict()["semantic_identity"],
        "model_sha256": emission.model_sha256,
        "urdf_sha256": emission.urdf_sha256,
        "mesh_files": list(emission.mesh_files),
    }


def _write_output(
    *,
    command: StageCommand,
    capture: _CaptureInput,
    destination: Path,
) -> tuple[str, EngineeringModel]:
    model = _compile_capture(capture)
    if destination.exists():
        raise PipelineError(f"Generate output already exists: {destination}")
    destination.mkdir(parents=True)
    emission = emit(model, evidence_root=capture.root / "evidence", destination=destination)
    write_json(destination / "tool.json", tool_record(role="portable"))
    receipt = _receipt(command=command, capture=capture, model=model, emission=emission)
    write_json(destination / GENERATE_RECEIPT, receipt)
    return digest({"receipt": receipt, "files": inventory(destination)}), model


class GenerateHandler:
    """Compile and emit one model from a registered capture artifact."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
    ) -> StageExecution:
        if command.stage != "generate":
            raise ProtocolError("generate.stage_mismatch", {"stage": command.stage})
        if command.predecessor_decision_id is None:
            raise ProtocolError("generate.predecessor_missing", {"command_id": command.command_id})
        if claim.command_id != command.command_id:
            raise ProtocolError("generate.claim_mismatch", {"command_id": command.command_id})

        capture = _request_capture(command, workspace)
        self.artifacts.verify(capture.artifact)
        output = workspace.root / "output"
        checks_sha256, _model = _write_output(command=command, capture=capture, destination=output)
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256=checks_sha256,
        )

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
    ) -> StageCandidate:
        """Replay deterministic compilation and compare the sealed candidate."""

        if (
            candidate.result.stage != "generate"
            or candidate.result.command_id != command.command_id
            or candidate.result.outcome != "completed"
        ):
            raise ProtocolError("generate.candidate_identity", {"command_id": command.command_id})
        if claim.command_id != command.command_id:
            raise ProtocolError("generate.claim_mismatch", {"command_id": command.command_id})
        with tempfile.TemporaryDirectory(prefix="generate-candidate-") as temporary:
            root = Path(temporary)
            inputs = root / "inputs"
            inputs.mkdir()
            materialized = {
                item.artifact_id: self.artifacts.materialize(item, inputs / item.artifact_id)
                for item in command.input_artifacts
            }
            candidate_root = self.artifacts.materialize(
                candidate.result.result_artifact,
                root / "candidate-output",
            )
            checks_sha256 = validate_candidate_output(
                command=command,
                workspace=StageWorkspace(root=root, inputs=materialized),
                candidate_root=candidate_root,
            )
            if checks_sha256 != candidate.result.checks_sha256:
                raise ProtocolError("generate.candidate_checks", {"command_id": command.command_id})
        return candidate


def validate_candidate_output(
    *,
    command: StageCommand,
    workspace: StageWorkspace,
    candidate_root: Path,
) -> str:
    """Compare an existing candidate with a fresh deterministic producer replay."""

    capture = _request_capture(command, workspace)
    expected_root = Path(tempfile.mkdtemp(prefix="generate-replay-", dir=workspace.root))
    shutil.rmtree(expected_root)
    try:
        checks_sha256, _model = _write_output(
            command=command,
            capture=capture,
            destination=expected_root,
        )
        candidate_files = inventory(Path(candidate_root), exclude=("manifest.json",))
        if inventory(expected_root) != candidate_files:
            raise ProtocolError("generate.candidate_mismatch", {"command_id": command.command_id})
        receipt = read_data(Path(candidate_root) / GENERATE_RECEIPT)
        if not isinstance(receipt, dict) or digest({"receipt": receipt, "files": candidate_files}) != checks_sha256:
            raise ProtocolError("generate.candidate_checks", {"command_id": command.command_id})
        EngineeringModel.from_dict(read_data(Path(candidate_root) / "model/robot.json"))
        return checks_sha256
    finally:
        shutil.rmtree(expected_root, ignore_errors=True)
