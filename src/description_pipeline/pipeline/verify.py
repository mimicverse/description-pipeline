"""Independent Verify stage over paired Generate and Capture artifacts."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..artifacts.store import LOCATOR_PREFIX, MANIFEST_NAME, MANIFEST_SCHEMA
from ..delivery import write_delivery_manifest
from ..interface_records import (
    CHECK_POLICY_SCHEMA,
    PURPOSE_ENGINEERING,
    PURPOSE_INSPECTION_PREVIEW,
    check_policy_digest,
    validate_qualification,
)
from ..io import PipelineError, canonical, digest, file_digest, inventory, read_data, write_json
from ..request_evidence import read_request_evidence
from ..runtime import tool_record
from ..schemas import GENERATE_RESULT_SCHEMA
from ..stages import CONTRACT, CONTRACT_SHA256
from ..verification.solidworks_urdf import (
    evaluate_bundle,
    rebuilt_definition,
    require_qualified_report,
)
from .contracts import (
    ArtifactRef,
    DiagnosticReplayScope,
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageExecution,
    StageFailure,
)
from .effects import StageEffectPort
from .engine import StageWorkspace
from .generate_contract import (
    CAPTURE_ARTIFACT_KIND,
    GENERATE_ARTIFACT_KIND,
    GENERATE_RECEIPT,
)
from .verify_contract import (
    CERTIFICATE_FIELDS,
    CERTIFICATE_FILENAME,
    CERTIFICATE_SCHEMA,
    EXECUTION_RECEIPT_FILENAME,
    OUTPUT_CONTRACT,
    RECEIPT_SCHEMA,
    REQUEST_SCHEMA,
    validate_certificate_diagnostics,
    verification_diagnostics,
)
_PURPOSES = frozenset({PURPOSE_ENGINEERING, PURPOSE_INSPECTION_PREVIEW})
_VOLATILE_DETAIL_KEYS = frozenset({"stderr", "stdout", "returncode", "traceback"})

IdentityProvider = Callable[[], Mapping[str, Any]]
Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


def current_verifier_identity() -> dict[str, Any]:
    """Read the verifier code and runtime that execute this process."""

    return dict(tool_record(role="portable"))


def current_verifier_sha256(identity: Mapping[str, Any]) -> str:
    """Return the tool source identity carried by a verifier runtime record."""

    value = identity.get("source_sha256")
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PipelineError("Verifier identity carries no source SHA-256")
    return value


def _json_object(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(dict(value), sort_keys=True))
    except (TypeError, ValueError) as error:
        raise PipelineError(f"{label} is not canonical JSON") from error


def _certificate_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **check,
            "details": {
                key: value for key, value in (check.get("details") or {}).items()
                if key not in _VOLATILE_DETAIL_KEYS
            },
        }
        for check in checks
    ]


@dataclass(frozen=True, slots=True)
class VerifyDomainRequest:
    """The exact Generate/Capture pair and verifier policy for one evaluation."""

    generate_artifact: ArtifactRef
    capture_artifact: ArtifactRef
    expected_verifier_sha256: str
    contract_sha256: str
    policy_sha256: str
    config_snapshot_sha256: str
    purpose: str
    schema_version: str = REQUEST_SCHEMA
    output_contract: str = OUTPUT_CONTRACT

    def __post_init__(self) -> None:
        if self.schema_version != REQUEST_SCHEMA or self.output_contract != OUTPUT_CONTRACT:
            raise PipelineError("Unsupported Verify request contract")
        if self.generate_artifact.kind != GENERATE_ARTIFACT_KIND or self.generate_artifact.producer_stage != "generate":
            raise PipelineError("Verify generate artifact has the wrong kind")
        if self.capture_artifact.kind != CAPTURE_ARTIFACT_KIND or self.capture_artifact.producer_stage != "capture":
            raise PipelineError("Verify capture artifact has the wrong kind")
        if self.generate_artifact.artifact_id == self.capture_artifact.artifact_id:
            raise PipelineError("Verify generate and capture artifacts must be distinct")
        for name in (
            "expected_verifier_sha256",
            "contract_sha256",
            "policy_sha256",
            "config_snapshot_sha256",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise PipelineError(f"Verify request {name} is not a lowercase SHA-256")
        if self.purpose not in _PURPOSES:
            raise PipelineError(f"Unsupported Verify purpose: {self.purpose!r}")

    def to_record(self) -> dict[str, Any]:
        return {
            "capture_artifact": self.capture_artifact.to_record(),
            "config_snapshot_sha256": self.config_snapshot_sha256,
            "contract_sha256": self.contract_sha256,
            "expected_verifier_sha256": self.expected_verifier_sha256,
            "generate_artifact": self.generate_artifact.to_record(),
            "output_contract": self.output_contract,
            "policy_sha256": self.policy_sha256,
            "purpose": self.purpose,
            "schema_version": self.schema_version,
        }

    @property
    def sha256(self) -> str:
        return digest(self.to_record())


@dataclass(frozen=True, slots=True)
class VerificationResult:
    certificate: dict[str, Any]
    execution_receipt: dict[str, Any]
    candidate_root: Path

    @property
    def passed(self) -> bool:
        return self.certificate["decision"] == "PASS"


class NoExternalEffects:
    """Explicit effect port for isolated Verify replay; any use is a defect."""

    def __getattr__(self, name: str):
        def forbidden(**_kwargs):
            raise ProtocolError("verify.external_effect_forbidden", {"method": name})

        return forbidden


@dataclass(frozen=True, slots=True)
class _ArtifactInput:
    artifact: ArtifactRef
    root: Path
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PreparedInput:
    request: VerifyDomainRequest
    generate: _ArtifactInput
    capture: _ArtifactInput
    generate_receipt: dict[str, Any]
    verifier: dict[str, Any]
    verifier_sha256: str
    verifier_record_sha256: str
    closure_sha256: str


def _artifact_members(root: Path) -> list[dict[str, Any]]:
    members = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST_NAME:
            continue
        if path.is_symlink() or path.is_junction():
            raise PipelineError("Artifact inputs cannot contain symbolic links or junctions")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PipelineError("Artifact inputs may contain only regular files and directories")
        members.append(
            {
                "bytes": path.stat().st_size,
                "path": relative,
                "sha256": file_digest(path),
            }
        )
    return members


def _verify_artifact_tree(root: Path, artifact: ArtifactRef) -> _ArtifactInput:
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise PipelineError("Artifact input is not a real materialized directory")
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise PipelineError("Artifact input lacks its immutable manifest")
    manifest = read_data(manifest_path)
    required = {"artifact_id", "kind", "members", "producer", "schema_version", "set_sha256"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise PipelineError("Artifact input manifest fields are incomplete or unknown")
    _require_artifact(
        manifest.get("schema_version") == MANIFEST_SCHEMA,
        "Artifact input uses an unknown manifest schema",
    )
    producer = manifest.get("producer")
    if not isinstance(producer, dict) or set(producer) != {"run_id", "stage", "tool_sha256"}:
        raise PipelineError("Artifact input producer record is malformed")
    members = manifest.get("members")
    if (
        not isinstance(members, list)
        or not members
        or any(not isinstance(item, dict) or set(item) != {"bytes", "path", "sha256"} for item in members)
    ):
        raise PipelineError("Artifact input member manifest is malformed")
    actual_members = _artifact_members(root)
    if actual_members != members:
        raise PipelineError("Artifact input bytes differ from their immutable member manifest")
    set_sha256 = digest({"members": members})
    identity = {
        "artifact_id": digest(
            {
                "kind": manifest["kind"],
                "members": members,
                "producer": producer,
                "schema_version": manifest["schema_version"],
                "set_sha256": set_sha256,
            }
        ),
        "kind": manifest["kind"],
        "locator": f"{LOCATOR_PREFIX}{manifest['artifact_id']}",
        "manifest_sha256": file_digest(manifest_path),
        "producer_run_id": producer["run_id"],
        "producer_stage": producer["stage"],
        "producer_tool_sha256": producer["tool_sha256"],
        "set_sha256": set_sha256,
    }
    if identity != artifact.to_record() or manifest.get("set_sha256") != set_sha256:
        raise PipelineError("Artifact input differs from the registered ArtifactRef")
    return _ArtifactInput(artifact=artifact, root=root, manifest=manifest)


def _require_artifact(condition: bool, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _generate_receipt(
    root: Path,
    *,
    command: StageCommand,
    generate: _ArtifactInput,
    capture: _ArtifactInput,
) -> dict[str, Any]:
    receipt = read_data(root / GENERATE_RECEIPT)
    expected = {
        "capture_artifact",
        "config_snapshot_sha256",
        "contract_sha256",
        "actual_executor_sha256",
        "mesh_files",
        "model_sha256",
        "output_contract",
        "required_executor_sha256",
        "schema_version",
        "semantic_identity",
        "urdf_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected:
        raise PipelineError("Generate receipt fields are incomplete or unknown")
    if (
        receipt.get("schema_version") != GENERATE_RESULT_SCHEMA
        or receipt.get("output_contract") != "solidworks-to-urdf.generated-artifact/v1"
    ):
        raise PipelineError("Generate receipt uses an unknown contract")
    if receipt.get("capture_artifact") != capture.artifact.to_record():
        raise PipelineError("Generate receipt names a different Capture artifact")
    if (
        receipt.get("config_snapshot_sha256") != command.config_snapshot_sha256
        or receipt.get("contract_sha256") != command.contract_sha256
        or receipt.get("required_executor_sha256") != generate.artifact.producer_tool_sha256
        or receipt.get("actual_executor_sha256") != generate.artifact.producer_tool_sha256
    ):
        raise PipelineError("Generate receipt command identity differs from the paired artifacts")
    model = read_data(root / "model/robot.json")
    if (
        not isinstance(model, dict)
        or receipt.get("model_sha256") != file_digest(root / "model/robot.json")
        or receipt.get("urdf_sha256") != file_digest(root / "urdf/robot.urdf")
        or receipt.get("semantic_identity") != model.get("semantic_identity")
    ):
        raise PipelineError("Generate receipt does not bind its model and URDF bytes")
    mesh_files = [
        {"path": "meshes/" + path, "sha256": checksum}
        for path, checksum in sorted(
            (path.relative_to(root / "meshes").as_posix(), file_digest(path))
            for path in (root / "meshes").rglob("*")
            if path.is_file()
        )
    ]
    if receipt.get("mesh_files") != mesh_files:
        raise PipelineError("Generate receipt does not bind its mesh files")
    allowed = {"model/", "urdf/", "meshes/", "reports/generate.json", "tool.json"}
    for member in generate.manifest["members"]:
        path = member["path"]
        if not any(path == item or (item.endswith("/") and path.startswith(item)) for item in allowed):
            raise PipelineError(f"Generate artifact contains a member outside its output contract: {path}")
    return receipt


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise PipelineError(f"Verification input directory is unavailable: {source.name}")
    shutil.copytree(source, destination)


def _evaluation_view(
    destination: Path,
    *,
    generate: _ArtifactInput,
    capture: _ArtifactInput,
    contract_sha256: str,
    policy_sha256: str,
) -> Path:
    if destination.exists():
        raise PipelineError("Verify evaluation view must not already exist")
    destination.mkdir(parents=True)
    _copy_tree(capture.root / "input", destination / "input")
    _copy_tree(capture.root / "evidence", destination / "evidence")
    _copy_tree(generate.root / "model", destination / "model")
    _copy_tree(generate.root / "urdf", destination / "urdf")
    _copy_tree(generate.root / "meshes", destination / "meshes")
    shutil.copyfile(generate.root / "tool.json", destination / "tool.json")
    write_delivery_manifest(
        destination,
        contract={"schema_version": CONTRACT["schema_version"], "digest": contract_sha256},
        policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": policy_sha256},
    )
    return destination


def _pair_subject_digest(generate: _ArtifactInput, capture: _ArtifactInput) -> str:
    files = {
        **{"input/" + path: checksum for path, checksum in inventory(capture.root / "input").items()},
        **{"evidence/" + path: checksum for path, checksum in inventory(capture.root / "evidence").items()},
        **{"model/" + path: checksum for path, checksum in inventory(generate.root / "model").items()},
        **{"urdf/" + path: checksum for path, checksum in inventory(generate.root / "urdf").items()},
        **{"meshes/" + path: checksum for path, checksum in inventory(generate.root / "meshes").items()},
        "tool.json": file_digest(generate.root / "tool.json"),
    }
    return digest(files)


def _prepare(
    request: VerifyDomainRequest,
    *,
    workspace: StageWorkspace,
    command: StageCommand,
    identity_provider: IdentityProvider,
) -> _PreparedInput:
    generate_root = workspace.inputs.get(request.generate_artifact.artifact_id)
    capture_root = workspace.inputs.get(request.capture_artifact.artifact_id)
    if (
        set(workspace.inputs)
        != {
            request.generate_artifact.artifact_id,
            request.capture_artifact.artifact_id,
        }
        or generate_root is None
        or capture_root is None
    ):
        raise ProtocolError("verify.input_closure", {"command_id": command.command_id})
    generate = _verify_artifact_tree(Path(generate_root), request.generate_artifact)
    capture = _verify_artifact_tree(Path(capture_root), request.capture_artifact)
    receipt = _generate_receipt(
        generate.root,
        command=command,
        generate=generate,
        capture=capture,
    )
    request_record, _request_sha = read_request_evidence(capture.root / "input/request.json")
    if request_record.get("purpose") != request.purpose:
        raise PipelineError("Verify purpose differs from the frozen request")
    if request_record.get("platform_config_digest") != request.config_snapshot_sha256:
        raise PipelineError("Verify configuration closure differs from the frozen request")
    if request.contract_sha256 != CONTRACT_SHA256 or request.policy_sha256 != check_policy_digest():
        raise PipelineError("Verify contract or policy differs from the current verifier")
    verifier = _json_object(identity_provider(), "verifier identity")
    verifier_sha256 = current_verifier_sha256(verifier)
    verifier_record_sha256 = digest(verifier)
    if verifier_sha256 != request.expected_verifier_sha256:
        raise PipelineError("Actual verifier differs from the requested Verify executor")
    closure = digest(
        {
            "capture_artifact": capture.artifact.to_record(),
            "generate_artifact": generate.artifact.to_record(),
        }
    )
    return _PreparedInput(
        request=request,
        generate=generate,
        capture=capture,
        generate_receipt=receipt,
        verifier=verifier,
        verifier_sha256=verifier_sha256,
        verifier_record_sha256=verifier_record_sha256,
        closure_sha256=closure,
    )


def _certificate(prepared: _PreparedInput, report: Mapping[str, Any]) -> dict[str, Any]:
    request = prepared.request
    evaluation_subject = _pair_subject_digest(prepared.generate, prepared.capture)
    if report.get("subject_sha256") != evaluation_subject:
        raise PipelineError("Verifier report is bound to another Generate/Capture view")
    proof = {
        "actual_verifier": prepared.verifier,
        "actual_verifier_sha256": prepared.verifier_sha256,
        "capture_artifact": prepared.capture.artifact.to_record(),
        "checks": _certificate_checks(report.get("checks") or []),
        "closure_sha256": prepared.closure_sha256,
        "config_snapshot_sha256": request.config_snapshot_sha256,
        "contract_sha256": request.contract_sha256,
        "decision": "PASS" if report.get("passed") is True else "FAIL",
        "diagnostics": verification_diagnostics(report),
        "evaluation_subject_sha256": evaluation_subject,
        "generate_artifact": prepared.generate.artifact.to_record(),
        "generate_receipt_sha256": file_digest(prepared.generate.root / GENERATE_RECEIPT),
        "passed": report.get("passed") is True,
        "pipeline_id": report.get("pipeline_id"),
        "policy_sha256": request.policy_sha256,
        "purpose": request.purpose,
        "qualifications": report.get("qualifications") or [],
        "request": request.to_record(),
        "request_sha256": request.sha256,
        "required_checks": list(report.get("required_checks") or []),
        "schema_version": CERTIFICATE_SCHEMA,
        "subject_sha256": request.generate_artifact.set_sha256,
        "subject_status": report.get("subject_status"),
    }
    return {**proof, "verification_key": digest(proof)}


class VerifyHandler:
    """Verify one Generate artifact against its explicitly paired Capture artifact."""

    def __init__(
        self,
        *,
        identity_provider: IdentityProvider | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.identity_provider = identity_provider or current_verifier_identity
        self.clock = clock or _now

    def request_from_command(
        self,
        command: StageCommand,
        workspace: StageWorkspace,
    ) -> tuple[VerifyDomainRequest, dict[str, Path]]:
        if command.stage != "verify":
            raise ProtocolError("verify.command_stage", {"stage": command.stage})
        request = dict(command.request)
        required = {
            "capture_artifact_id",
            "generate_artifact_id",
            "output_contract",
            "purpose",
            "schema_version",
        }
        if set(request) != required:
            raise ProtocolError(
                "verify.request_shape",
                {"missing": sorted(required - set(request)), "unknown": sorted(set(request) - required)},
            )
        if request["schema_version"] != REQUEST_SCHEMA or request["output_contract"] != OUTPUT_CONTRACT:
            raise ProtocolError("verify.request_schema", {"command_id": command.command_id})
        by_id = {item.artifact_id: item for item in command.input_artifacts}
        if len(by_id) != len(command.input_artifacts) or len(by_id) != 2:
            raise ProtocolError("verify.input_closure", {"command_id": command.command_id})
        try:
            generate = by_id[request["generate_artifact_id"]]
            capture = by_id[request["capture_artifact_id"]]
        except (KeyError, TypeError) as error:
            raise ProtocolError("verify.input_closure", {"command_id": command.command_id}) from error
        if generate.kind != GENERATE_ARTIFACT_KIND or generate.producer_stage != "generate":
            raise ProtocolError("verify.generate_ref", {"artifact_id": generate.artifact_id})
        if capture.kind != CAPTURE_ARTIFACT_KIND or capture.producer_stage != "capture":
            raise ProtocolError("verify.capture_ref", {"artifact_id": capture.artifact_id})
        if set(workspace.inputs) != set(by_id):
            raise ProtocolError("verify.workspace_inputs", {"command_id": command.command_id})
        return (
            VerifyDomainRequest(
                generate_artifact=generate,
                capture_artifact=capture,
                expected_verifier_sha256=command.expected_executor_sha256,
                contract_sha256=command.contract_sha256,
                policy_sha256=command.policy_sha256,
                config_snapshot_sha256=command.config_snapshot_sha256,
                purpose=str(request["purpose"]),
            ),
            {artifact_id: Path(path) for artifact_id, path in workspace.inputs.items()},
        )

    def evaluate(
        self,
        request: VerifyDomainRequest,
        *,
        generate_root: Path,
        capture_root: Path,
        candidate_root: Path,
        evaluation_root: Path,
        command: StageCommand,
        claim: ExecutionClaim,
    ) -> VerificationResult:
        started = self.clock()
        workspace = StageWorkspace(
            root=evaluation_root.parent,
            inputs={
                request.generate_artifact.artifact_id: generate_root,
                request.capture_artifact.artifact_id: capture_root,
            },
        )
        prepared = _prepare(
            request,
            workspace=workspace,
            command=command,
            identity_provider=self.identity_provider,
        )
        view = _evaluation_view(
            evaluation_root,
            generate=prepared.generate,
            capture=prepared.capture,
            contract_sha256=request.contract_sha256,
            policy_sha256=request.policy_sha256,
        )
        report = evaluate_bundle(
            view,
            require_native_receipts=False,
        )
        if report.get("passed") is True:
            require_qualified_report(report, rebuilt_definition(view), root=view)

        _verify_artifact_tree(generate_root, request.generate_artifact)
        _verify_artifact_tree(capture_root, request.capture_artifact)
        receipt_again = _generate_receipt(
            generate_root,
            command=command,
            generate=prepared.generate,
            capture=prepared.capture,
        )
        current_closure = digest(
            {
                "capture_artifact": request.capture_artifact.to_record(),
                "generate_artifact": request.generate_artifact.to_record(),
            }
        )
        if current_closure != prepared.closure_sha256 or receipt_again != prepared.generate_receipt:
            raise PipelineError("Generate/Capture artifact pair changed during Verify")
        final_verifier_record = _json_object(self.identity_provider(), "verifier identity")
        if digest(final_verifier_record) != prepared.verifier_record_sha256:
            raise PipelineError("Verifier identity changed during Verify")

        certificate = _certificate(prepared, report)
        finished = self.clock()
        execution_receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "execution_id": str(uuid.uuid4()),
            "stage": "verify",
            "command_id": command.command_id,
            "run_id": command.run_id,
            "claim_id": claim.claim_id,
            "fence": claim.fence,
            "request": request.to_record(),
            "request_sha256": request.sha256,
            "closure_sha256": prepared.closure_sha256,
            "certificate_sha256": digest(certificate),
            "started_at": started.astimezone(UTC).isoformat(),
            "finished_at": finished.astimezone(UTC).isoformat(),
            "actual_verifier": prepared.verifier,
            "actual_verifier_sha256": prepared.verifier_sha256,
            "execution_details": {
                check["id"]: {
                    key: value for key, value in (check.get("details") or {}).items()
                    if key in _VOLATILE_DETAIL_KEYS
                }
                for check in report.get("checks") or []
                if any(key in (check.get("details") or {}) for key in _VOLATILE_DETAIL_KEYS)
            },
        }
        if candidate_root.exists():
            raise PipelineError("Verify candidate output must not already exist")
        candidate_root.mkdir(parents=True)
        write_json(candidate_root / CERTIFICATE_FILENAME, certificate)
        write_json(candidate_root / EXECUTION_RECEIPT_FILENAME, execution_receipt)
        return VerificationResult(
            certificate=certificate,
            execution_receipt=execution_receipt,
            candidate_root=candidate_root,
        )

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: StageEffectPort,
    ) -> StageExecution:
        del effects
        if (
            command.predecessor_decision_id is None
            and command.diagnostic_scope is None
        ):
            raise ProtocolError(
                "verify.predecessor_missing",
                {"command_id": command.command_id},
            )
        if claim.command_id != command.command_id:
            raise ProtocolError("verify.claim_mismatch", {"command_id": command.command_id})
        request, roots = self.request_from_command(command, workspace)
        if claim.executor_sha256 != request.expected_verifier_sha256:
            raise ProtocolError("verify.executor_mismatch", {"claim_executor": claim.executor_sha256})
        result = self.evaluate(
            request,
            generate_root=roots[request.generate_artifact.artifact_id],
            capture_root=roots[request.capture_artifact.artifact_id],
            candidate_root=workspace.root / "output",
            evaluation_root=workspace.root / "evaluation",
            command=command,
            claim=claim,
        )
        if result.passed:
            return StageExecution(
                outcome="completed",
                candidate_root=str(result.candidate_root),
                checks_sha256=digest(result.certificate),
                resources_settled=True,
            )
        failure = StageFailure(
            "verification_failed",
            "Independent verification failed",
            evidence_artifacts=(),
            retained_locations=(str(result.candidate_root),),
        )
        return StageExecution(
            outcome="failed",
            candidate_root=str(result.candidate_root),
            checks_sha256=digest(result.certificate),
            resources_settled=True,
            failure=failure,
        )

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
        *,
        effects: StageEffectPort,
    ) -> StageCandidate:
        del effects
        result = candidate.result
        if (
            command.stage != "verify"
            or claim.command_id != command.command_id
            or result.command_id != command.command_id
            or result.run_id != command.run_id
            or result.stage != "verify"
            or result.request_digest != command.request_digest
            or result.actual_executor_sha256 != claim.executor_sha256
        ):
            raise ProtocolError("verify.candidate_identity_mismatch", {"command_id": command.command_id})
        return candidate


def validate_certificate_structure(certificate: Mapping[str, Any]) -> None:
    """Validate the one certificate schema before any consumer trusts its decision."""

    required = CERTIFICATE_FIELDS
    if set(certificate) != required:
        raise PipelineError(
            "Verification certificate fields differ from the contract: "
            f"missing={sorted(required - set(certificate))}, "
            f"unknown={sorted(set(certificate) - required)}"
        )
    if certificate.get("schema_version") != CERTIFICATE_SCHEMA:
        raise PipelineError("Verification certificate uses an unknown schema")
    if certificate.get("decision") not in {"PASS", "FAIL"}:
        raise PipelineError("Verification certificate has an invalid decision")
    if (certificate.get("decision") == "PASS") != (certificate.get("passed") is True):
        raise PipelineError("Verification certificate decision conflicts with passed")
    validate_certificate_diagnostics(certificate)
    request = certificate.get("request")
    if not isinstance(request, dict):
        raise PipelineError("Verification certificate request must be an object")
    try:
        request_value = VerifyDomainRequest(
            generate_artifact=ArtifactRef.from_record(request["generate_artifact"]),
            capture_artifact=ArtifactRef.from_record(request["capture_artifact"]),
            expected_verifier_sha256=request["expected_verifier_sha256"],
            contract_sha256=request["contract_sha256"],
            policy_sha256=request["policy_sha256"],
            config_snapshot_sha256=request["config_snapshot_sha256"],
            purpose=request["purpose"],
            schema_version=request["schema_version"],
            output_contract=request["output_contract"],
        )
    except (KeyError, TypeError, PipelineError) as error:
        raise PipelineError("Verification certificate request is invalid") from error
    if request_value.sha256 != certificate.get("request_sha256"):
        raise PipelineError("Verification certificate request digest differs")
    if (
        request_value.generate_artifact.to_record() != certificate.get("generate_artifact")
        or request_value.capture_artifact.to_record() != certificate.get("capture_artifact")
        or request_value.config_snapshot_sha256 != certificate.get("config_snapshot_sha256")
        or request_value.contract_sha256 != certificate.get("contract_sha256")
        or request_value.policy_sha256 != certificate.get("policy_sha256")
        or request_value.purpose != certificate.get("purpose")
        or request_value.expected_verifier_sha256 != certificate.get("actual_verifier_sha256")
        or request_value.generate_artifact.set_sha256 != certificate.get("subject_sha256")
    ):
        raise PipelineError("Verification certificate pair binding differs")
    verifier = certificate.get("actual_verifier")
    if not isinstance(verifier, dict) or current_verifier_sha256(verifier) != certificate.get(
        "actual_verifier_sha256"
    ):
        raise PipelineError("Verification certificate verifier identity differs")
    proof = dict(certificate)
    verification_key = proof.pop("verification_key")
    if digest(proof) != verification_key:
        raise PipelineError("Verification certificate proof digest differs")
    checks = certificate.get("checks")
    required_checks = certificate.get("required_checks")
    if (
        not isinstance(checks, list)
        or not checks
        or not all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in checks)
        or not isinstance(required_checks, list)
        or not required_checks
        or not all(isinstance(value, str) for value in required_checks)
        or len({row["id"] for row in checks}) != len(checks)
        or {row["id"] for row in checks} != set(required_checks)
    ):
        raise PipelineError("Verification certificate check inventory is incomplete")
    if certificate.get("passed") is True and not all(
        row.get("state") == "passed" and row.get("passed") is True for row in checks
    ):
        raise PipelineError("Verification certificate PASS contains a non-passing check")
    qualifications = certificate.get("qualifications")
    if not isinstance(qualifications, list):
        raise PipelineError("Verification certificate qualifications must be a list")
    for item in qualifications:
        validate_qualification(item)
    qualification_ids = [(item.get("fact_id"), item.get("scope"), item.get("object_id")) for item in qualifications]
    if len(qualification_ids) != len(set(qualification_ids)):
        raise PipelineError("Verification certificate repeats a qualification identity")


def _qualification_members(
    qualifications: list[dict],
    *,
    generate_root: Path,
    capture_root: Path,
) -> None:
    for item in qualifications:
        for reference in item["evidence"]:
            relative = reference["record"]
            root = capture_root if relative.startswith(("input/", "evidence/")) else generate_root
            path = root / relative
            if not path.is_file() or file_digest(path) != reference["sha256"]:
                raise PipelineError("Verification certificate qualification evidence differs from the pair")


def validate_verification_certificate(
    certificate_root: str | Path,
    *,
    generate_root: str | Path,
    capture_root: str | Path,
    generate_artifact: ArtifactRef,
    capture_artifact: ArtifactRef,
    require_pass: bool = True,
) -> dict[str, Any]:
    """Consume a certificate by re-reading the exact pair without mechanical replay."""

    certificate_root = Path(certificate_root)
    certificate_path = certificate_root / CERTIFICATE_FILENAME
    payload = certificate_path.read_bytes()
    certificate = read_data(certificate_path)
    if not isinstance(certificate, dict):
        raise PipelineError("Verification certificate must be an object")
    if payload != canonical(certificate):
        raise PipelineError("Verification certificate is not canonically encoded")
    validate_certificate_structure(certificate)
    if (
        certificate["generate_artifact"] != generate_artifact.to_record()
        or certificate["capture_artifact"] != capture_artifact.to_record()
    ):
        raise PipelineError("Verification certificate names another artifact pair")
    generate = _verify_artifact_tree(Path(generate_root), generate_artifact)
    capture = _verify_artifact_tree(Path(capture_root), capture_artifact)
    command = StageCommand.create(
        command_id="certificate-consumer",
        run_id="certificate-consumer",
        stage="verify",
        idempotency_key="certificate-consumer",
        request={
            "capture_artifact_id": capture_artifact.artifact_id,
            "generate_artifact_id": generate_artifact.artifact_id,
            "output_contract": OUTPUT_CONTRACT,
            "purpose": certificate["purpose"],
            "schema_version": REQUEST_SCHEMA,
        },
        input_artifacts=(generate_artifact, capture_artifact),
        config_snapshot_sha256=certificate["config_snapshot_sha256"],
        contract_sha256=certificate["contract_sha256"],
        policy_sha256=certificate["policy_sha256"],
        expected_executor_sha256=certificate["actual_verifier_sha256"],
        predecessor_decision_id=None,
    )
    receipt = _generate_receipt(generate.root, command=command, generate=generate, capture=capture)
    closure = digest(
        {
            "capture_artifact": capture_artifact.to_record(),
            "generate_artifact": generate_artifact.to_record(),
        }
    )
    evaluation_subject = _pair_subject_digest(generate, capture)
    if (
        certificate["closure_sha256"] != closure
        or certificate["evaluation_subject_sha256"] != evaluation_subject
        or certificate["generate_receipt_sha256"] != file_digest(generate.root / GENERATE_RECEIPT)
        or receipt["capture_artifact"] != capture_artifact.to_record()
    ):
        raise PipelineError("Verification certificate is not bound to the current Generate/Capture pair")
    if certificate["decision"] == "PASS":
        _qualification_members(
            certificate["qualifications"],
            generate_root=generate.root,
            capture_root=capture.root,
        )
    if require_pass and certificate["decision"] != "PASS":
        raise PipelineError("Verification certificate does not establish PASS")
    return certificate


def replay_verification(
    *,
    generate_root: str | Path,
    capture_root: str | Path,
    generate_artifact: ArtifactRef,
    capture_artifact: ArtifactRef,
    identity_provider: IdentityProvider | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Explicitly rerun VerifyHandler and compare its deterministic certificate."""

    provider = identity_provider or current_verifier_identity
    verifier = _json_object(provider(), "verifier identity")
    frozen_request, _request_sha = read_request_evidence(Path(capture_root) / "input/request.json")
    artifacts = (generate_artifact, capture_artifact)
    command = StageCommand.create(
        command_id="verification-replay",
        run_id="verification-replay",
        stage="verify",
        idempotency_key="verification-replay",
        request={
            "capture_artifact_id": capture_artifact.artifact_id,
            "generate_artifact_id": generate_artifact.artifact_id,
            "output_contract": OUTPUT_CONTRACT,
            "purpose": str(frozen_request["purpose"]),
            "schema_version": REQUEST_SCHEMA,
        },
        input_artifacts=artifacts,
        config_snapshot_sha256=str(frozen_request["platform_config_digest"]),
        contract_sha256=CONTRACT_SHA256,
        policy_sha256=check_policy_digest(),
        expected_executor_sha256=current_verifier_sha256(verifier),
        predecessor_decision_id=None,
        diagnostic_scope=DiagnosticReplayScope.from_inputs(
            "verify",
            artifacts,
        ),
    )
    claim = ExecutionClaim(
        claim_id="verification-replay",
        command_id=command.command_id,
        fence=1,
        owner_sha256=digest({"process": "verification-replay"}),
        executor_sha256=current_verifier_sha256(verifier),
        lane_key=None,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    with tempfile.TemporaryDirectory(prefix="description-verify-replay-") as directory:
        root = Path(directory)
        result = VerifyHandler(
            identity_provider=lambda: verifier,
            clock=clock or _now,
        ).execute(
            command,
            claim,
            StageWorkspace(
                root=root,
                inputs={
                    generate_artifact.artifact_id: Path(generate_root),
                    capture_artifact.artifact_id: Path(capture_root),
                },
            ),
            effects=NoExternalEffects(),
        )
        certificate = read_data(Path(result.candidate_root) / CERTIFICATE_FILENAME)
    return certificate
