"""Freeze domain handler over the canonical six-stage protocol."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..interface_records import REQUEST_PURPOSES
from ..io import (
    PipelineError,
    artifact_path_parts,
    canonical,
    confined,
    digest,
    file_digest,
    read_data,
)
from ..sources.solidworks.handoff import describe_handoff
from ..sources.solidworks.revision import package_inventory
from .contracts import (
    ArtifactRef,
    ExecutionClaim,
    ProtocolError,
    StageCandidate,
    StageCommand,
    StageExecution,
    StageFailure,
)
from .effects import StageEffectPort
from .engine import StageWorkspace

FREEZE_REQUEST_SCHEMA = "description-pipeline.freeze-request/v1"
FREEZE_CHECKS_SCHEMA = "description-pipeline.freeze-checks/v1"
RULESET_CATALOG_SCHEMA = "description-pipeline.ruleset-catalog/v1"
ARTIFACT_MANIFEST_SCHEMA = "description-pipeline.artifact-manifest/v1"

ORIGINALS_KIND = "freeze_originals"
RULESET_KIND = "freeze_ruleset"
CONFIGURATION_KIND = "platform_config"

_REQUEST_FIELDS = {
    "change_summary",
    "configuration_artifact_id",
    "main_assembly",
    "originals_artifact_id",
    "platform_config_sha256",
    "principal",
    "purpose",
    "request_id",
    "ruleset_artifact_id",
    "ruleset_catalog_sha256",
    "ruleset_version",
    "schema_version",
    "target_hardware",
    "upload_inventory_sha256",
}
_ARTIFACT_FIELDS = (
    ("originals", ORIGINALS_KIND, "originals_artifact_id"),
    ("ruleset", RULESET_KIND, "ruleset_artifact_id"),
    ("configuration", CONFIGURATION_KIND, "configuration_artifact_id"),
)
_TERMINAL_REASON_PREFIX = "freeze."


class FreezeRefusal(PipelineError):
    """A deterministic Freeze refusal with a stable machine code."""

    def __init__(self, code: str, message: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = dict(detail or {})


def _text(value: object, code: str, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise FreezeRefusal(code, f"{field} must be non-empty text", {"field": field})
    return value


def _sha256(value: object, code: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FreezeRefusal(code, f"{field} must be a lowercase SHA-256 digest", {"field": field})
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise FreezeRefusal("freeze.request_invalid", "Freeze request keys must be text")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _validate_request(command: StageCommand) -> dict[str, Any]:
    request = _plain(command.request)
    if not isinstance(request, dict):
        raise FreezeRefusal("freeze.request_invalid", "Freeze request must be an object")
    if set(request) != _REQUEST_FIELDS:
        raise FreezeRefusal(
            "freeze.request_invalid",
            "Freeze request fields are incomplete or unknown",
            {"missing": sorted(_REQUEST_FIELDS - set(request)), "unknown": sorted(set(request) - _REQUEST_FIELDS)},
        )
    if request["schema_version"] != FREEZE_REQUEST_SCHEMA:
        raise FreezeRefusal("freeze.request_invalid", "Freeze request schema is unsupported")
    for field in ("request_id", "principal", "target_hardware", "ruleset_version"):
        _text(request[field], "freeze.request_invalid", field)
    if request["purpose"] not in REQUEST_PURPOSES:
        raise FreezeRefusal("freeze.request_invalid", "Freeze purpose is unsupported", {"field": "purpose"})
    summary = request["change_summary"]
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or any((ord(character) < 32 and character != "\n") or ord(character) == 127 for character in summary)
    ):
        raise FreezeRefusal("freeze.request_invalid", "Change summary is empty or unsafe", {"field": "change_summary"})
    main = _text(request["main_assembly"], "freeze.request_invalid", "main_assembly")
    try:
        parts = artifact_path_parts(main)
    except PipelineError as error:
        raise FreezeRefusal("freeze.request_invalid", str(error), {"field": "main_assembly"}) from error
    if not parts or Path(main).suffix.casefold() != ".sldasm":
        raise FreezeRefusal("freeze.request_invalid", "Main assembly must name an SLDASM file")
    for field in ("upload_inventory_sha256", "ruleset_catalog_sha256", "platform_config_sha256"):
        _sha256(request[field], "freeze.request_invalid", field)
    return request


def _bound_artifacts(command: StageCommand, request: Mapping[str, Any]) -> dict[str, ArtifactRef]:
    by_kind: dict[str, ArtifactRef] = {}
    for artifact in command.input_artifacts:
        if artifact.kind not in {kind for _, kind, _ in _ARTIFACT_FIELDS} or artifact.kind in by_kind:
            raise FreezeRefusal(
                "freeze.artifacts_invalid",
                "Freeze requires exactly one originals, ruleset and configuration artifact",
                {"kind": artifact.kind},
            )
        by_kind[artifact.kind] = artifact
    bound: dict[str, ArtifactRef] = {}
    for role, kind, id_field in _ARTIFACT_FIELDS:
        artifact = by_kind.get(kind)
        if artifact is None:
            raise FreezeRefusal("freeze.artifacts_invalid", "A required Freeze artifact is absent", {"role": role})
        if artifact.artifact_id != request[id_field]:
            raise FreezeRefusal(
                "freeze.artifact_binding_mismatch",
                "The complete request names a different immutable artifact",
                {"role": role},
            )
        bound[role] = artifact
    if command.config_snapshot_sha256 != request["platform_config_sha256"]:
        raise FreezeRefusal(
            "freeze.configuration_binding_mismatch",
            "The command configuration identity differs from the frozen request",
        )
    if bound["configuration"].set_sha256 != request["platform_config_sha256"]:
        raise FreezeRefusal(
            "freeze.configuration_binding_mismatch",
            "The configuration artifact content differs from the frozen request",
        )
    if bound["ruleset"].set_sha256 != request["ruleset_catalog_sha256"]:
        raise FreezeRefusal(
            "freeze.ruleset_binding_mismatch",
            "The ruleset artifact content differs from the frozen request",
        )
    return bound


def _artifact_files(root: Path, artifact: ArtifactRef, *, role: str) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise FreezeRefusal(
            "freeze.artifact_invalid",
            "A Freeze input artifact must materialize as a real directory",
            {"role": role},
        )
    files: dict[str, str] = {}
    manifest_path = root / "manifest.json"
    reserved_manifest = False
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            manifest = read_data(manifest_path)
        except (PipelineError, OSError, ValueError):
            manifest = None
        if isinstance(manifest, dict) and manifest.get("schema_version") == ARTIFACT_MANIFEST_SCHEMA:
            if manifest.get("artifact_id") != artifact.artifact_id:
                raise FreezeRefusal(
                    "freeze.artifact_binding_mismatch",
                    "The materialized artifact manifest names another artifact",
                    {"role": role},
                )
            reserved_manifest = True
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if reserved_manifest and relative == "manifest.json":
            continue
        if path.is_symlink() or path.is_junction():
            raise FreezeRefusal("freeze.artifact_invalid", "An artifact may not contain links", {"role": role})
        if path.is_dir():
            continue
        if not path.is_file():
            raise FreezeRefusal("freeze.artifact_invalid", "An artifact may contain only regular files", {"role": role})
        files[relative] = file_digest(path)
    if not files:
        raise FreezeRefusal("freeze.artifact_invalid", "A Freeze input artifact is empty", {"role": role})
    members = [
        {"path": path, "bytes": confined(root, path).stat().st_size, "sha256": checksum}
        for path, checksum in files.items()
    ]
    if digest({"members": members}) != artifact.set_sha256:
        raise FreezeRefusal(
            "freeze.artifact_changed",
            "Materialized artifact bytes differ from the registered content identity",
            {"role": role},
        )
    return files


def _input_roots(command: StageCommand, workspace: StageWorkspace, bound: Mapping[str, ArtifactRef]) -> dict[str, Path]:
    expected = {artifact.artifact_id for artifact in bound.values()}
    if set(workspace.inputs) != expected:
        raise FreezeRefusal(
            "freeze.workspace_invalid",
            "The claim workspace does not carry exactly the command input artifacts",
            {
                "missing": sorted(expected - set(workspace.inputs)),
                "unknown": sorted(set(workspace.inputs) - expected),
            },
        )
    workspace_root = workspace.root.resolve()
    roots: dict[str, Path] = {}
    for role, artifact in bound.items():
        path = Path(workspace.inputs[artifact.artifact_id])
        if not path.is_absolute():
            raise FreezeRefusal("freeze.workspace_invalid", "Artifact paths must be absolute", {"role": role})
        resolved = path.resolve()
        if not resolved.is_relative_to(workspace_root):
            raise FreezeRefusal(
                "freeze.workspace_invalid",
                "Artifact materialization must stay inside the claim workspace",
                {"role": role},
            )
        roots[role] = resolved
    return roots


def _copy_original_payload(files: Mapping[str, str], source: Path, destination: Path) -> None:
    if destination.exists():
        raise FreezeRefusal("freeze.workspace_invalid", "The Freeze validation directory already exists")
    destination.mkdir(parents=True)
    for relative, checksum in files.items():
        source_path = confined(source, relative)
        target_path = confined(destination, relative, exists=False)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)
        if file_digest(target_path) != checksum:
            raise FreezeRefusal(
                "freeze.artifact_changed",
                "Original bytes changed while the Freeze closure was materialized",
                {"path": relative},
            )


def _ruleset_record(root: Path, expected_version: str) -> dict[str, Any]:
    path = confined(root, "catalog.json")
    try:
        record = read_data(path)
    except (PipelineError, OSError, ValueError) as error:
        raise FreezeRefusal("freeze.ruleset_invalid", "Ruleset catalog metadata is missing or unreadable") from error
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != RULESET_CATALOG_SCHEMA
        or record.get("version") != expected_version
        or not isinstance(record.get("records"), dict)
    ):
        raise FreezeRefusal("freeze.ruleset_invalid", "Ruleset catalog metadata is invalid or names another version")
    return record


def _write_canonical(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value) + b"\n")


class FreezeHandler:
    """Bind a complete Freeze command to immutable input and produce one decision."""

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: StageEffectPort,
    ) -> StageExecution:
        del effects
        try:
            if command.stage != "freeze" or command.kind != "stage":
                raise FreezeRefusal("freeze.stage_invalid", "The command does not authorize the Freeze stage")
            if command.predecessor_decision_id is not None:
                raise FreezeRefusal("freeze.predecessor_invalid", "Freeze is a root stage and has no predecessor")
            if claim.command_id != command.command_id or claim.executor_sha256 != command.expected_executor_sha256:
                raise FreezeRefusal("freeze.claim_invalid", "The execution claim does not own this Freeze command")
            request = _validate_request(command)
            bound = _bound_artifacts(command, request)
            roots = _input_roots(command, workspace, bound)
            originals = _artifact_files(roots["originals"], bound["originals"], role="originals")
            _artifact_files(roots["ruleset"], bound["ruleset"], role="ruleset")
            _artifact_files(roots["configuration"], bound["configuration"], role="configuration")
            ruleset = _ruleset_record(roots["ruleset"], request["ruleset_version"])

            validation = workspace.root / f"freeze-originals-{claim.fence}"
            _copy_original_payload(originals, roots["originals"], validation)
            identity = describe_handoff(validation)
            actual = package_inventory(validation)
            if actual != originals or identity["handoff_sha256"] != request["upload_inventory_sha256"]:
                raise FreezeRefusal(
                    "freeze.original_binding_mismatch",
                    "Original bytes differ from the complete Freeze request",
                )
            main = request["main_assembly"]
            if main not in actual:
                candidates = sorted(name for name in actual if name.casefold() == main.casefold())
                detail = {"main_assembly": main, "case_match": candidates[0] if candidates else None}
                raise FreezeRefusal("freeze.main_assembly_invalid", "The selected main assembly is not exact", detail)
            if confined(validation, main).stat().st_size == 0:
                raise FreezeRefusal("freeze.main_assembly_invalid", "The selected main assembly is empty")

            checks = {
                "schema_version": FREEZE_CHECKS_SCHEMA,
                "stage": "freeze",
                "command_id": command.command_id,
                "request_digest": command.request_digest,
                "claim_id": claim.claim_id,
                "fence": claim.fence,
                "observed_at": datetime.now(UTC).isoformat(),
                "checks": [
                    {"id": "command.binding", "state": "passed", "details": {}},
                    {
                        "id": "artifact.closure",
                        "state": "passed",
                        "details": {
                            "artifacts": {role: artifact.to_record() for role, artifact in bound.items()},
                        },
                    },
                    {
                        "id": "handoff.integrity",
                        "state": "passed",
                        "details": {"handoff_sha256": identity["handoff_sha256"], "files": actual},
                    },
                    {
                        "id": "main_assembly",
                        "state": "passed",
                        "details": {"main_assembly": main, "sha256": actual[main]},
                    },
                    {
                        "id": "ruleset.catalog",
                        "state": "passed",
                        "details": {"version": ruleset["version"], "set_sha256": bound["ruleset"].set_sha256},
                    },
                    {
                        "id": "configuration.snapshot",
                        "state": "passed",
                        "details": {"set_sha256": bound["configuration"].set_sha256},
                    },
                ],
            }
            output = workspace.root / f"freeze-result-{claim.fence}"
            if output.exists():
                raise ProtocolError("freeze.candidate_conflict", {"candidate": str(output)})
            _write_canonical(output / "request.json", request)
            _write_canonical(
                output / "inputs.json",
                {
                    "artifacts": {role: artifact.to_record() for role, artifact in bound.items()},
                    "upload_inventory": actual,
                },
            )
            _write_canonical(
                output / "ruleset.json",
                {"artifact": bound["ruleset"].to_record(), "catalog": ruleset},
            )
            _write_canonical(
                output / "configuration.json",
                {"artifact": bound["configuration"].to_record()},
            )
            _write_canonical(output / "checks.json", checks)
            return StageExecution(
                outcome="completed",
                candidate_root=str(output),
                checks_sha256=digest(checks),
                resources_settled=True,
            )
        except FreezeRefusal as refusal:
            raise StageFailure(refusal.code, refusal.message) from refusal
        except PipelineError as error:
            raise StageFailure("freeze.input_invalid", str(error)) from error

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
            result.command_id != command.command_id
            or result.run_id != command.run_id
            or result.stage != "freeze"
            or result.request_digest != command.request_digest
        ):
            raise ProtocolError("freeze.candidate_mismatch", {"command_id": command.command_id})
        if claim.command_id != command.command_id:
            raise ProtocolError("freeze.claim_mismatch", {"command_id": command.command_id})
        if result.actual_executor_sha256 != command.expected_executor_sha256:
            raise ProtocolError("freeze.executor_mismatch", {"command_id": command.command_id})
        if result.outcome in {"failed", "cancelled"} and not (
            isinstance(result.reason_code, str) and result.reason_code.startswith(_TERMINAL_REASON_PREFIX)
        ):
            raise ProtocolError("freeze.failure_reason_invalid", {"command_id": command.command_id})
        return candidate


__all__ = [
    "CONFIGURATION_KIND",
    "FREEZE_CHECKS_SCHEMA",
    "FREEZE_REQUEST_SCHEMA",
    "ORIGINALS_KIND",
    "RULESET_CATALOG_SCHEMA",
    "RULESET_KIND",
    "FreezeHandler",
    "FreezeRefusal",
]
