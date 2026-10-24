"""Discover domain handler: sealed native observation to pure fixed-rule derivation."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ..interface_records import REQUEST_PURPOSES
from ..io import PipelineError, artifact_path_parts, canonical, confined, digest, file_digest, read_data
from ..orchestration.discover_protocol import NativeDiscoverExecution, NativeDiscoverFailure
from ..sources.solidworks.discover_stage import (
    NativeDiscoverRefusal,
    NativeDiscoverRequest,
    admit_native_observation,
)
from ..sources.solidworks.discovery import (
    DISCOVERY_FILE,
    FINDINGS_FILE,
    DiscoverySettings,
    SubmissionIdentity,
    derive_native_package,
)
from ..sources.solidworks.revision import package_inventory
from .contracts import (
    ArtifactRef,
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageExecution,
)
from .engine import StageWorkspace

DISCOVER_REQUEST_SCHEMA = "description-pipeline.discover-request/v1"
DISCOVER_RESULT_SCHEMA = "description-pipeline.discover-result/v1"
DISCOVER_CHECKS_SCHEMA = "description-pipeline.discover-checks/v1"
DISCOVER_FAILURE_SCHEMA = "description-pipeline.discover-failure/v1"
RULESET_CATALOG_SCHEMA = "description-pipeline.ruleset-catalog/v1"
ARTIFACT_MANIFEST_SCHEMA = "description-pipeline.artifact-manifest/v1"

FREEZE_RESULT_KIND = "stage_output:freeze"
SOURCE_SET_KIND = "freeze_originals"
RULESET_KIND = "freeze_ruleset"
CONFIGURATION_KIND = "platform_config"

_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "freeze_artifact_id",
        "source_set_artifact_id",
        "ruleset_artifact_id",
        "configuration_artifact_id",
        "source_files",
        "source_inventory_sha256",
        "main_assembly",
        "purpose",
        "ruleset_version",
        "ruleset_catalog_sha256",
        "platform_config_sha256",
        "submission_identity",
        "expected_native_tool_sha256",
    }
)
_SUBMISSION_FIELDS = frozenset(
    {
        "hardware_id",
        "owner",
        "change_summary",
        "parent_revision",
        "control_reference",
        "as_of",
        "inventory_digest",
    }
)
_ARTIFACT_FIELDS = (
    ("freeze", FREEZE_RESULT_KIND, "freeze_artifact_id"),
    ("source_set", SOURCE_SET_KIND, "source_set_artifact_id"),
    ("ruleset", RULESET_KIND, "ruleset_artifact_id"),
    ("configuration", CONFIGURATION_KIND, "configuration_artifact_id"),
)


class DiscoverNativePort(Protocol):
    """Runtime-owned native effect port.

    Implementations must authorize the complete outbound through the common
    effect journal before issuing the remote call. An unknown existing effect
    must be reconciled or refused without invoking the native operation.
    """

    def discover(
        self,
        request: NativeDiscoverRequest,
        source_root: Path,
        workspace: Path,
    ) -> NativeDiscoverExecution: ...


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise NativeDiscoverRefusal("discover.request_invalid", "Request keys must be text")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _sha(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise NativeDiscoverRefusal("discover.request_invalid", f"{field} is not a SHA-256 digest", {"field": field})
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise NativeDiscoverRefusal("discover.request_invalid", f"{field} is invalid", {"field": field})
    return value


def _validate_request(command: StageCommand) -> dict[str, Any]:
    request = _plain(command.request)
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise NativeDiscoverRefusal(
            "discover.request_invalid",
            "Discover request fields are incomplete or unknown",
            {
                "missing": sorted(_REQUEST_FIELDS - set(request or {})),
                "unknown": sorted(set(request or {}) - _REQUEST_FIELDS),
            },
        )
    if request["schema_version"] != DISCOVER_REQUEST_SCHEMA:
        raise NativeDiscoverRefusal("discover.request_invalid", "Discover request schema is unsupported")
    for field in (
        "freeze_artifact_id",
        "source_set_artifact_id",
        "ruleset_artifact_id",
        "configuration_artifact_id",
        "main_assembly",
        "ruleset_version",
    ):
        _text(request[field], field)
    if request["purpose"] not in REQUEST_PURPOSES:
        raise NativeDiscoverRefusal("discover.request_invalid", "Discover purpose is unsupported")
    try:
        parts = artifact_path_parts(request["main_assembly"])
    except PipelineError as error:
        raise NativeDiscoverRefusal("discover.request_invalid", str(error), {"field": "main_assembly"}) from error
    if not parts or Path(request["main_assembly"]).suffix.casefold() != ".sldasm":
        raise NativeDiscoverRefusal("discover.request_invalid", "Main assembly must name an SLDASM file")
    source_files = request["source_files"]
    if not isinstance(source_files, dict) or not source_files:
        raise NativeDiscoverRefusal("discover.request_invalid", "Discover request carries no SourceSet")
    if request["main_assembly"] not in source_files:
        raise NativeDiscoverRefusal("discover.main_assembly_invalid", "Main assembly is absent from the SourceSet")
    if digest(source_files) != _sha(request["source_inventory_sha256"], "source_inventory_sha256"):
        raise NativeDiscoverRefusal(
            "discover.source_inventory_mismatch",
            "SourceSet inventory differs from its request digest",
        )
    for field in ("ruleset_catalog_sha256", "platform_config_sha256", "expected_native_tool_sha256"):
        _sha(request[field], field)
    submission = request["submission_identity"]
    if not isinstance(submission, dict) or set(submission) != _SUBMISSION_FIELDS:
        raise NativeDiscoverRefusal("discover.request_invalid", "Submission identity fields are invalid")
    if any(value is not None and not isinstance(value, str) for value in submission.values()):
        raise NativeDiscoverRefusal("discover.request_invalid", "Submission identity values must be text or null")
    return request


def _bound_artifacts(command: StageCommand, request: Mapping[str, Any]) -> dict[str, ArtifactRef]:
    expected_kinds = {kind for _, kind, _ in _ARTIFACT_FIELDS}
    by_kind: dict[str, ArtifactRef] = {}
    for artifact in command.input_artifacts:
        if artifact.kind not in expected_kinds or artifact.kind in by_kind:
            raise NativeDiscoverRefusal(
                "discover.artifacts_invalid",
                "Discover requires exactly one freeze result, SourceSet, ruleset and configuration artifact",
                {"kind": artifact.kind},
            )
        by_kind[artifact.kind] = artifact
    bound = {}
    for role, kind, id_field in _ARTIFACT_FIELDS:
        artifact = by_kind.get(kind)
        if artifact is None or artifact.artifact_id != request[id_field]:
            raise NativeDiscoverRefusal(
                "discover.artifact_binding_mismatch",
                "Discover request names a different immutable artifact",
                {"role": role},
            )
        bound[role] = artifact
    freeze = bound["freeze"]
    if freeze.producer_run_id != command.run_id or freeze.producer_stage != "freeze":
        raise NativeDiscoverRefusal("discover.freeze_binding_mismatch", "Freeze artifact belongs to another run")
    if bound["ruleset"].set_sha256 != request["ruleset_catalog_sha256"]:
        raise NativeDiscoverRefusal("discover.ruleset_binding_mismatch", "Ruleset artifact differs from the request")
    if (
        bound["configuration"].set_sha256 != request["platform_config_sha256"]
        or command.config_snapshot_sha256 != request["platform_config_sha256"]
    ):
        raise NativeDiscoverRefusal(
            "discover.configuration_binding_mismatch",
            "Configuration artifact differs from the command snapshot",
        )
    return bound


def _artifact_files(root: Path, artifact: ArtifactRef, *, role: str) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise NativeDiscoverRefusal(
            "discover.artifact_invalid",
            "Discover input artifacts must materialize as real directories",
            {"role": role},
        )
    files: dict[str, str] = {}
    reserved_manifest = False
    manifest_path = root / "manifest.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            manifest = read_data(manifest_path)
        except (PipelineError, OSError, ValueError):
            manifest = None
        if isinstance(manifest, dict) and manifest.get("schema_version") == ARTIFACT_MANIFEST_SCHEMA:
            if manifest.get("artifact_id") != artifact.artifact_id:
                raise NativeDiscoverRefusal(
                    "discover.artifact_binding_mismatch",
                    "Materialized artifact names another artifact",
                    {"role": role},
                )
            reserved_manifest = True
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if reserved_manifest and relative == "manifest.json":
            continue
        if path.is_symlink() or path.is_junction():
            raise NativeDiscoverRefusal("discover.artifact_invalid", "Artifacts may not carry links", {"role": role})
        if path.is_dir():
            continue
        if not path.is_file():
            raise NativeDiscoverRefusal("discover.artifact_invalid", "Artifacts require regular files", {"role": role})
        files[relative] = file_digest(path)
    if not files:
        raise NativeDiscoverRefusal("discover.artifact_invalid", "Discover input artifact is empty", {"role": role})
    members = [
        {"path": path, "bytes": confined(root, path).stat().st_size, "sha256": checksum}
        for path, checksum in files.items()
    ]
    if digest({"members": members}) != artifact.set_sha256:
        raise NativeDiscoverRefusal(
            "discover.artifact_changed",
            "Materialized artifact differs from its registered identity",
            {"role": role},
        )
    return files


def _input_roots(command: StageCommand, workspace: StageWorkspace, bound: Mapping[str, ArtifactRef]) -> dict[str, Path]:
    expected = {artifact.artifact_id for artifact in bound.values()}
    if set(workspace.inputs) != expected:
        raise NativeDiscoverRefusal(
            "discover.workspace_invalid",
            "Claim workspace does not carry exactly the command artifacts",
            {
                "missing": sorted(expected - set(workspace.inputs)),
                "unknown": sorted(set(workspace.inputs) - expected),
            },
        )
    root = workspace.root.resolve()
    result = {}
    for role, artifact in bound.items():
        path = Path(workspace.inputs[artifact.artifact_id])
        if not path.is_absolute() or not path.resolve().is_relative_to(root):
            raise NativeDiscoverRefusal(
                "discover.workspace_invalid",
                "Artifact materialization must remain inside the claim workspace",
                {"role": role},
            )
        result[role] = path.resolve()
    return result


def _ruleset_version(root: Path, expected: str) -> dict:
    try:
        record = read_data(confined(root, "catalog.json"))
    except (PipelineError, OSError, ValueError) as error:
        raise NativeDiscoverRefusal("discover.ruleset_invalid", "Ruleset catalog is missing or unreadable") from error
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != RULESET_CATALOG_SCHEMA
        or record.get("version") != expected
        or not isinstance(record.get("records"), dict)
    ):
        raise NativeDiscoverRefusal("discover.ruleset_invalid", "Ruleset catalog is invalid or names another version")
    return record


def _submission_identity(value: Mapping[str, Any]) -> SubmissionIdentity:
    return SubmissionIdentity(**dict(value))


def _copy_selected(source: Path, destination: Path, files: Mapping[str, str]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for name, checksum in files.items():
        target = confined(destination, name, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(confined(source, name), target)
        if file_digest(target) != checksum:
            raise NativeDiscoverRefusal(
                "discover.source_set_changed",
                "Selected source changed while preparing pure derivation",
                {"path": name},
            )


def _write_canonical(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


def _failure_execution(
    command: StageCommand,
    claim: ExecutionClaim,
    workspace: StageWorkspace,
    refusal: NativeDiscoverRefusal,
    *,
    effect_id: str | None = None,
) -> StageExecution:
    failure = {
        "schema_version": DISCOVER_FAILURE_SCHEMA,
        "stage": "discover",
        "command_id": command.command_id,
        "idempotency_key": command.idempotency_key,
        "request_digest": command.request_digest,
        "claim_id": claim.claim_id,
        "fence": claim.fence,
        "claimed_executor_sha256": claim.owner_sha256,
        "reason_code": refusal.code,
        "message": refusal.message,
        "detail": refusal.detail,
        "effects": {
            "required": [] if effect_id is None else [effect_id],
            "known": [] if effect_id is None else [effect_id],
        },
        "evidence": {
            "success_artifact_available": False,
            "reason": "Discover refused before a complete result artifact existed",
        },
        "observed_at": datetime.now(UTC).isoformat(),
    }
    root = workspace.root / f"discover-failure-{claim.fence}"
    if root.exists():
        raise ProtocolError("discover.candidate_conflict", {"candidate": str(root)})
    _write_canonical(root / "failure.json", failure)
    return StageExecution(
        outcome="failed",
        candidate_root=str(root),
        checks_sha256=digest(failure),
        required_effect_ids=() if effect_id is None else (effect_id,),
        reason_code=refusal.code,
    )


class DiscoverHandler:
    """Run one Discover command through a runtime-authorized native effect."""

    def __init__(self, native: DiscoverNativePort) -> None:
        self.native = native

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
    ) -> StageExecution:
        try:
            if command.stage != "discover" or command.kind != "stage":
                raise NativeDiscoverRefusal("discover.stage_invalid", "Command does not authorize Discover")
            if command.predecessor_decision_id is None:
                raise NativeDiscoverRefusal("discover.predecessor_invalid", "Discover requires Freeze predecessor")
            if claim.command_id != command.command_id or claim.owner_sha256 != command.expected_executor_sha256:
                raise NativeDiscoverRefusal("discover.claim_invalid", "Claim does not own this Discover command")
            request = _validate_request(command)
            bound = _bound_artifacts(command, request)
            roots = _input_roots(command, workspace, bound)
            source_files = _artifact_files(roots["source_set"], bound["source_set"], role="source_set")
            if source_files != request["source_files"] or digest(source_files) != request["source_inventory_sha256"]:
                raise NativeDiscoverRefusal(
                    "discover.source_inventory_mismatch",
                    "Immutable SourceSet differs from the complete Discover request",
                )
            _artifact_files(roots["ruleset"], bound["ruleset"], role="ruleset")
            ruleset = _ruleset_version(roots["ruleset"], request["ruleset_version"])
            _artifact_files(roots["configuration"], bound["configuration"], role="configuration")
            _artifact_files(roots["freeze"], bound["freeze"], role="freeze")
            native_request = NativeDiscoverRequest(
                schema_version="description-pipeline.native-discover-request/v1",
                operation_id=command.command_id,
                command_id=command.command_id,
                run_id=command.run_id,
                request_digest=command.request_digest,
                source_files=source_files,
                source_inventory_sha256=request["source_inventory_sha256"],
                main_assembly=request["main_assembly"],
                purpose=request["purpose"],
                expected_native_tool_sha256=request["expected_native_tool_sha256"],
            )
            execution = self.native.discover(
                native_request,
                roots["source_set"],
                workspace.root / f"native-discover-{claim.fence}",
            )
            try:
                observation = admit_native_observation(execution.observation.root, request=native_request)
            except NativeDiscoverRefusal as refusal:
                raise NativeDiscoverFailure(
                    execution.effect_id,
                    refusal.code,
                    refusal.message,
                    refusal.detail,
                ) from refusal
            except PipelineError as error:
                raise NativeDiscoverFailure(
                    execution.effect_id,
                    "discover.observation_invalid",
                    str(error),
                ) from error
            selection = dict(observation.selection)
            bindings_files = selection.get("files")
            if not isinstance(bindings_files, Mapping):
                raise NativeDiscoverRefusal("discover.selection_invalid", "Native observation carries no selection")
            selected_root = workspace.root / f"selected-{claim.fence}"
            _copy_selected(roots["source_set"], selected_root, bindings_files)
            output = workspace.root / f"discover-result-{claim.fence}"
            output.mkdir(parents=True, exist_ok=False)
            settings = DiscoverySettings(
                record_roots=(roots["ruleset"],),
                main_assembly=request["main_assembly"],
                purpose=request["purpose"],
            )
            try:
                prepared = derive_native_package(
                    selected_root,
                    output / "prepared",
                    command.run_id,
                    dict(observation.record),
                    settings=settings,
                    configuration=None,
                    assembly=None,
                    submission_identity=_submission_identity(request["submission_identity"]),
                )
            except PipelineError as error:
                raise NativeDiscoverFailure(
                    execution.effect_id,
                    "discover.derivation_invalid",
                    str(error),
                ) from error
            shutil.copytree(observation.root, output / "native-observation")
            checks = {
                "schema_version": DISCOVER_CHECKS_SCHEMA,
                "command_id": command.command_id,
                "request_digest": command.request_digest,
                "claim_id": claim.claim_id,
                "fence": claim.fence,
                "observed_at": datetime.now(UTC).isoformat(),
                "checks": [
                    {"id": "source_set.closure", "state": "passed", "details": {"source_files": source_files}},
                    {
                        "id": "native.observation",
                        "state": "passed",
                        "details": {
                            "observation_sha256": observation.observation_sha256,
                            "native_tool_sha256": observation.manifest["native_tool_sha256"],
                        },
                    },
                    {
                        "id": "selection.closure",
                        "state": "passed",
                        "details": {
                            "selection_sha256": observation.manifest["selection_sha256"],
                            "selected_files_sha256": observation.manifest["selected_files_sha256"],
                        },
                    },
                    {
                        "id": "ruleset.derivation",
                        "state": "passed",
                        "details": {"version": ruleset["version"], "sha256": bound["ruleset"].set_sha256},
                    },
                    {
                        "id": "derivation.findings",
                        "state": "passed" if prepared.passed else "failed",
                        "details": {"findings": [dict(item) for item in prepared.findings]},
                    },
                ],
            }
            result = {
                "schema_version": DISCOVER_RESULT_SCHEMA,
                "command_id": command.command_id,
                "run_id": command.run_id,
                "request_digest": command.request_digest,
                "source_set_artifact": bound["source_set"].to_record(),
                "ruleset_artifact": bound["ruleset"].to_record(),
                "configuration_artifact": bound["configuration"].to_record(),
                "native_observation_sha256": observation.observation_sha256,
                "native_tool": dict(observation.native_tool),
                "selection": selection,
                "prepared_sha256": prepared.prepared_sha256,
                "prepared_files": package_inventory(prepared.package),
                "findings": [dict(item) for item in prepared.findings],
                "passed": prepared.passed,
                "discovery_file": DISCOVERY_FILE,
                "findings_file": FINDINGS_FILE,
            }
            _write_canonical(output / "request.json", request)
            _write_canonical(output / "checks.json", checks)
            _write_canonical(output / "result.json", result)
            if not prepared.passed:
                failure = {
                    "schema_version": DISCOVER_FAILURE_SCHEMA,
                    "stage": "discover",
                    "command_id": command.command_id,
                    "request_digest": command.request_digest,
                    "reason_code": "discover.findings",
                    "message": "Discover derivation contains blocking findings",
                    "findings": [dict(item) for item in prepared.findings],
                    "effects": {"required": [execution.effect_id], "known": [execution.effect_id]},
                    "observed_at": datetime.now(UTC).isoformat(),
                }
                _write_canonical(output / "failure.json", failure)
            return StageExecution(
                outcome="completed" if prepared.passed else "failed",
                candidate_root=str(output),
                checks_sha256=digest(checks),
                required_effect_ids=(execution.effect_id,),
                reason_code=None if prepared.passed else "discover.findings",
            )
        except NativeDiscoverFailure as refusal:
            return _failure_execution(
                command,
                claim,
                workspace,
                refusal,
                effect_id=refusal.effect_id,
            )
        except NativeDiscoverRefusal as refusal:
            return _failure_execution(command, claim, workspace, refusal)

    def validate_candidate(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
    ) -> StageCandidate:
        result = candidate.result
        if (
            result.command_id != command.command_id
            or result.run_id != command.run_id
            or result.stage != "discover"
            or result.request_digest != command.request_digest
        ):
            raise ProtocolError("discover.candidate_mismatch", {"command_id": command.command_id})
        if claim.command_id != command.command_id:
            raise ProtocolError("discover.claim_mismatch", {"command_id": command.command_id})
        if result.actual_executor_sha256 != command.expected_executor_sha256:
            raise ProtocolError("discover.executor_mismatch", {"command_id": command.command_id})
        if not result.required_effect_ids:
            raise ProtocolError("discover.effect_missing", {"command_id": command.command_id})
        if result.outcome in {"failed", "cancelled"} and not (
            isinstance(result.reason_code, str) and result.reason_code.startswith("discover.")
        ):
            raise ProtocolError("discover.failure_reason_invalid", {"command_id": command.command_id})
        return candidate


__all__ = [
    "CONFIGURATION_KIND",
    "DISCOVER_CHECKS_SCHEMA",
    "DISCOVER_FAILURE_SCHEMA",
    "DISCOVER_REQUEST_SCHEMA",
    "DISCOVER_RESULT_SCHEMA",
    "FREEZE_RESULT_KIND",
    "RULESET_CATALOG_SCHEMA",
    "RULESET_KIND",
    "SOURCE_SET_KIND",
    "DiscoverHandler",
    "DiscoverNativePort",
]
