"""MJCF stage over a passing, immutable URDF verification closure."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ... import mjcf, mjcf_contract as contract
from ...artifacts import ArtifactStore
from ...io import PipelineError, canonical, confined, digest, file_digest, inventory, read_data, write_json
from ...verification.mjcf import evaluate_projection
from ..contracts import (
    ArtifactRef,
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageExecution,
    StageFailure,
)
from ..effects import StageEffectPort
from ..engine import StageWorkspace
from ..verify import validate_verification_certificate
from ..verify_contract import CERTIFICATE_FILENAME, EXECUTION_RECEIPT_FILENAME


def source_refs(command: StageCommand) -> dict[str, ArtifactRef]:
    """Parse one closed request; admission separately establishes formal lineage."""
    request = command.request
    required = {
        "schema_version",
        "output_contract",
        "generate_artifact_id",
        "capture_artifact_id",
        "verify_artifact_id",
        "verify_certificate_sha256",
        "simulation",
    }
    if command.stage != "mjcf" or set(request) != required:
        raise ProtocolError("mjcf.request_shape")
    if (
        request["schema_version"] != contract.STAGE_REQUEST_SCHEMA
        or request["output_contract"] != contract.OUTPUT_CONTRACT
    ):
        raise ProtocolError("mjcf.request_contract")
    if not isinstance(request["simulation"], str):
        raise ProtocolError("mjcf.simulation_type")
    contract.load_simulation(request["simulation"].encode("utf-8"))
    refs = {}
    for stage in ("generate", "capture", "verify"):
        matches = [ref for ref in command.input_artifacts if ref.artifact_id == request[f"{stage}_artifact_id"]]
        if len(matches) != 1 or matches[0].kind != f"stage_output:{stage}" or matches[0].producer_stage != stage:
            raise ProtocolError("mjcf.source_ref", {"stage": stage})
        refs[stage] = matches[0]
    if len(command.input_artifacts) != 3 or len({ref.artifact_id for ref in refs.values()}) != 3:
        raise ProtocolError("mjcf.input_closure")
    return refs


def _source_certificate(command: StageCommand, refs: dict, roots: dict) -> dict:
    if set(inventory(roots["verify"], exclude=("manifest.json",))) != {
        CERTIFICATE_FILENAME,
        EXECUTION_RECEIPT_FILENAME,
    }:
        raise PipelineError("MJCF source Verify artifact has files outside its output contract")
    certificate = validate_verification_certificate(
        roots["verify"],
        generate_root=roots["generate"],
        capture_root=roots["capture"],
        generate_artifact=refs["generate"],
        capture_artifact=refs["capture"],
    )
    if (
        file_digest(roots["verify"] / CERTIFICATE_FILENAME) != command.request["verify_certificate_sha256"]
        or certificate["actual_verifier_sha256"] != refs["verify"].producer_tool_sha256
        or certificate["contract_sha256"] != command.contract_sha256
        or certificate["policy_sha256"] != command.policy_sha256
        or certificate["config_snapshot_sha256"] != command.config_snapshot_sha256
    ):
        raise PipelineError("MJCF source qualification differs from its admitted request")
    return certificate


def _view(destination: Path, generate: Path, projection: Path | None = None) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("model", "urdf", "meshes"):
        shutil.copytree(generate / name, destination / name)
    if projection is not None:
        shutil.copytree(projection / "mjcf", destination / "mjcf")
    return destination


def _actual_executor(command: StageCommand, claim: ExecutionClaim) -> str:
    actual = contract.tool_identity()["source_sha256"]
    if (
        claim.command_id != command.command_id
        or actual != claim.executor_sha256
        or actual != command.expected_executor_sha256
    ):
        raise ProtocolError("mjcf.executor_mismatch")
    return actual


_CERTIFICATE_FIELDS = {
    "command",
    "schema_version",
    "request_digest",
    "source_artifacts",
    "input_binding",
    "report",
    "actual_executor_sha256",
    "projection_sha256",
    "certificate_key",
}


def read_certificate(root: Path, command: StageCommand, *, require_pass: bool = True) -> dict:
    """Validate the certificate shape and exact stage output before consuming it."""
    path = confined(root, contract.REPORT_PATH)
    certificate = read_data(path)
    if (
        not isinstance(certificate, dict)
        or set(certificate) != _CERTIFICATE_FIELDS
        or path.read_bytes() != canonical(certificate)
    ):
        raise PipelineError("MJCF certificate has an invalid shape or encoding")
    proof = {key: value for key, value in certificate.items() if key != "certificate_key"}
    refs = source_refs(command)
    binding, report = certificate["input_binding"], certificate["report"]
    if (
        certificate["schema_version"] != contract.STAGE_CERTIFICATE_SCHEMA
        or certificate["command"] != command.to_record()
        or certificate["request_digest"] != command.request_digest
        or certificate["source_artifacts"] != {stage: ref.to_record() for stage, ref in refs.items()}
        or certificate["actual_executor_sha256"] != command.expected_executor_sha256
        or certificate["certificate_key"] != digest(proof)
        or not isinstance(binding, dict)
        or not isinstance(report, dict)
        or report.get("schema_version") != contract.REPORT_SCHEMA
        or type(report.get("passed")) is not bool
        or (
            require_pass and (report.get("passed") is not True or report.get("input_binding_sha256") != digest(binding))
        )
    ):
        raise PipelineError("MJCF certificate identity or passing proof differs")
    checks = report.get("checks")
    if (
        not isinstance(checks, list)
        or [row.get("id") for row in checks] not in ([*contract.CHECK_IDS, "identity.stability"], ["engine.process"])
        or any(
            type(row.get("passed")) is not bool
            or not isinstance(row.get("details"), dict)
            or row.get("state") != ("passed" if row["passed"] else "failed")
            for row in checks
        )
        or all(row["passed"] for row in checks) != report["passed"]
    ):
        raise PipelineError("MJCF certificate does not pass every required check")
    files = binding.get("files")
    if not isinstance(files, dict):
        raise PipelineError("MJCF certificate has no bound file inventory")
    projected = {path: row for path, row in files.items() if path.startswith("mjcf/")}
    actual = inventory(root, exclude=("manifest.json",))
    if (
        not projected
        or contract.ROBOT_NAME not in projected
        or contract.CONFIG_NAME not in projected
        or actual
        != {contract.REPORT_PATH: file_digest(path), **{name: row["sha256"] for name, row in projected.items()}}
        or any(confined(root, name).stat().st_size != row["bytes"] for name, row in projected.items())
        or certificate["projection_sha256"] != digest(projected)
        or confined(root, contract.CONFIG_NAME).read_bytes() != command.request["simulation"].encode("utf-8")
    ):
        raise PipelineError("MJCF stage output differs from its certificate")
    return certificate


def validate_certificate(
    root: Path,
    command: StageCommand,
    *,
    source_roots: dict[str, Path],
    require_pass: bool = True,
) -> dict:
    """Rebind a sealed certificate to its exact sources without running conversion."""
    certificate = read_certificate(root, command, require_pass=require_pass)
    refs = source_refs(command)
    _source_certificate(command, refs, source_roots)
    with tempfile.TemporaryDirectory(prefix="mjcf-certificate-") as temporary:
        view = _view(Path(temporary) / "view", source_roots["generate"], root)
        if contract.binding_for_projection(view) != certificate["input_binding"]:
            raise PipelineError("MJCF certificate input bytes or tool identity changed")
    return certificate


class MJCFHandler:
    """Project and independently check MJCF without writing any upstream artifact."""

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
        del effects
        actual = _actual_executor(command, claim)
        if command.predecessor_decision_id is None and command.diagnostic_scope is None:
            raise ProtocolError("mjcf.predecessor_missing")
        refs = source_refs(command)
        if set(workspace.inputs) != {ref.artifact_id for ref in refs.values()}:
            raise ProtocolError("mjcf.workspace_inputs")
        roots = {stage: Path(workspace.inputs[ref.artifact_id]) for stage, ref in refs.items()}
        for ref in refs.values():
            self.artifacts.verify(ref)
        try:
            _source_certificate(command, refs, roots)
            view = _view(workspace.root / "projection", roots["generate"])
            mjcf.project_isolated(view, command.request["simulation"].encode("utf-8"))
            binding = contract.binding_for_projection(view)
            report = evaluate_projection(view, binding)
            _source_certificate(command, refs, roots)
            _actual_executor(command, claim)
            certificate = {
                "command": command.to_record(),
                "schema_version": contract.STAGE_CERTIFICATE_SCHEMA,
                "request_digest": command.request_digest,
                "source_artifacts": {stage: ref.to_record() for stage, ref in refs.items()},
                "input_binding": binding,
                "report": report,
                "actual_executor_sha256": actual,
                "projection_sha256": digest(
                    {path: row for path, row in binding["files"].items() if path.startswith("mjcf/")}
                ),
            }
            certificate["certificate_key"] = digest(certificate)
            output = workspace.root / "output"
            output.mkdir(exist_ok=False)
            shutil.copytree(view / "mjcf", output / "mjcf")
            write_json(output / contract.REPORT_PATH, certificate)
            passed = report["passed"] is True
            return StageExecution(
                outcome="completed" if passed else "failed",
                candidate_root=str(output),
                checks_sha256=digest(certificate),
                resources_settled=True,
                failure=None if passed else StageFailure("mjcf.verification_failed", "Independent MJCF checks failed"),
            )
        except ProtocolError:
            raise
        except PipelineError as error:
            raise StageFailure("mjcf.local_contract_failed", str(error), error_type=type(error).__name__) from error

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
        *,
        effects: StageEffectPort,
    ) -> StageCandidate:
        del effects
        actual = _actual_executor(command, claim)
        result = candidate.result
        if (
            result.stage != "mjcf"
            or result.run_id != command.run_id
            or result.command_id != command.command_id
            or result.request_digest != command.request_digest
            or result.outcome not in {"completed", "failed"}
            or result.output_artifact.kind != contract.ARTIFACT_KIND
            or result.output_artifact.producer_run_id != command.run_id
            or result.output_artifact.producer_stage != "mjcf"
            or result.actual_executor_sha256 != actual
            or result.output_artifact.producer_tool_sha256 != actual
        ):
            raise ProtocolError("mjcf.candidate_identity")
        refs = source_refs(command)
        with tempfile.TemporaryDirectory(prefix="mjcf-candidate-") as temporary:
            directory = Path(temporary)
            roots = {stage: self.artifacts.materialize(ref, directory / stage) for stage, ref in refs.items()}
            root = self.artifacts.materialize(result.output_artifact, directory / "output")
            certificate = validate_certificate(
                root, command, source_roots=roots, require_pass=result.outcome == "completed"
            )
            if certificate["report"]["passed"] != (result.outcome == "completed"):
                raise ProtocolError("mjcf.candidate_outcome")
            if digest(certificate) != result.checks_sha256:
                raise ProtocolError("mjcf.candidate_checks")
        return candidate
