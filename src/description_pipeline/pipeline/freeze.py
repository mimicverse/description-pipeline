"""Freeze domain handler over the canonical six-stage protocol."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
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
    inventory,
    read_data,
    parse_data,
)
from ..schemas import (
    ARTIFACT_MANIFEST_SCHEMA,
    FREEZE_CHECKS_SCHEMA,
    FREEZE_REQUEST_SCHEMA,
    RULESET_CATALOG_SCHEMA,
)
from ..request_evidence import (
    build_request_evidence,
    request_evidence_bytes,
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

_RULESET_CATALOG_FIELDS = frozenset({"schema_version", "version", "records", "roots"})
_RULESET_ROOT_FIELDS = frozenset({"path", "members"})

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


@dataclass(frozen=True, slots=True)
class RulesetRoot:
    """One portable logical record root inside a sealed ruleset artifact."""

    path: str
    members: Mapping[str, str]

    def __post_init__(self) -> None:
        try:
            parts = artifact_path_parts(self.path)
        except PipelineError as error:
            raise ValueError("ruleset root path is not canonical") from error
        if not parts or "/".join(parts) != self.path or self.path == "catalog.json":
            raise ValueError("ruleset root path is not canonical")
        if not isinstance(self.members, Mapping):
            raise ValueError("ruleset root members must be an object")
        members = {}
        folded = set()
        for relative, checksum in self.members.items():
            if not isinstance(relative, str) or not isinstance(checksum, str):
                raise ValueError("ruleset members must map text paths to digests")
            try:
                member_parts = artifact_path_parts(relative)
            except PipelineError as error:
                raise ValueError("ruleset member path is not canonical") from error
            if not member_parts or "/".join(member_parts) != relative:
                raise ValueError("ruleset member path is not canonical")
            if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
                raise ValueError("ruleset member digest is invalid")
            key = relative.casefold()
            if key in folded:
                raise ValueError("ruleset member paths collide")
            folded.add(key)
            members[relative] = checksum
        object.__setattr__(self, "members", dict(sorted(members.items())))

    def to_record(self) -> dict[str, Any]:
        return {"path": self.path, "members": dict(self.members)}

    @classmethod
    def from_record(cls, record: object) -> RulesetRoot:
        if not isinstance(record, Mapping) or set(record) != _RULESET_ROOT_FIELDS:
            raise ValueError("ruleset root fields are invalid")
        return cls(path=record["path"], members=record["members"])


@dataclass(frozen=True, slots=True)
class RulesetCatalog:
    """The ordered logical roots and closed member inventory of one ruleset."""

    version: str
    roots: tuple[RulesetRoot, ...]
    records: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("ruleset version is invalid")
        if (
            not isinstance(self.roots, tuple)
            or not self.roots
            or not all(isinstance(root, RulesetRoot) for root in self.roots)
        ):
            raise ValueError("ruleset roots are invalid")
        root_paths = [root.path.casefold() for root in self.roots]
        if len(root_paths) != len(set(root_paths)):
            raise ValueError("ruleset roots collide")
        for index, root in enumerate(self.roots):
            root_parts = tuple(part.casefold() for part in root.path.split("/"))
            for other in self.roots[index + 1 :]:
                other_parts = tuple(part.casefold() for part in other.path.split("/"))
                shortest = min(len(root_parts), len(other_parts))
                if root_parts[:shortest] == other_parts[:shortest]:
                    raise ValueError("ruleset roots overlap")
        records: dict[str, str] = {}
        for root in self.roots:
            for relative, checksum in root.members.items():
                path = f"{root.path}/{relative}"
                if path in records:
                    raise ValueError("ruleset records collide")
                records[path] = checksum
        if not isinstance(self.records, Mapping) or dict(self.records) != records:
            raise ValueError("ruleset records differ from their logical roots")
        expected_version = "records-" + digest([root.to_record() for root in self.roots])
        if self.version != expected_version:
            raise ValueError("ruleset version differs from its logical roots")
        object.__setattr__(self, "records", dict(sorted(records.items())))

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": RULESET_CATALOG_SCHEMA,
            "version": self.version,
            "records": dict(self.records),
            "roots": [root.to_record() for root in self.roots],
        }

    @classmethod
    def from_record(cls, record: object) -> RulesetCatalog:
        if not isinstance(record, Mapping) or set(record) != _RULESET_CATALOG_FIELDS:
            raise ValueError("ruleset catalog fields are invalid")
        if record.get("schema_version") != RULESET_CATALOG_SCHEMA:
            raise ValueError("ruleset catalog schema is unsupported")
        roots = record.get("roots")
        if not isinstance(roots, list):
            raise ValueError("ruleset roots must be a list")
        return cls(
            version=record["version"],
            roots=tuple(RulesetRoot.from_record(item) for item in roots),
            records=record["records"],
        )


def read_ruleset_catalog(root: Path) -> RulesetCatalog:
    path = confined(Path(root), "catalog.json")
    try:
        record = parse_data(path.read_bytes(), path)
        return RulesetCatalog.from_record(record)
    except (PipelineError, OSError, TypeError, ValueError) as error:
        raise PipelineError(f"Ruleset catalog is invalid: {error}") from error


def ruleset_record_roots(root: Path, catalog: RulesetCatalog) -> tuple[Path, ...]:
    """Resolve sealed logical roots and verify every cataloged byte."""

    root = Path(root)
    result = []
    for item in catalog.roots:
        directory = confined(root, item.path, exists=False)
        if not directory.is_dir() or directory.is_symlink() or directory.is_junction():
            raise PipelineError(f"Ruleset root is missing or unsafe: {item.path}")
        if inventory(directory) != dict(item.members):
            raise PipelineError(f"Ruleset root differs from its catalog: {item.path}")
        result.append(directory)
    return tuple(result)


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


def _ruleset_record(root: Path, expected_version: str) -> RulesetCatalog:
    try:
        catalog = read_ruleset_catalog(root)
    except (PipelineError, OSError, TypeError, ValueError) as error:
        raise FreezeRefusal("freeze.ruleset_invalid", "Ruleset catalog metadata is missing or unreadable") from error
    if catalog.version != expected_version:
        raise FreezeRefusal("freeze.ruleset_invalid", "Ruleset catalog metadata is invalid or names another version")
    try:
        ruleset_record_roots(root, catalog)
    except PipelineError as error:
        raise FreezeRefusal(
            "freeze.ruleset_invalid",
            "Ruleset roots differ from their sealed catalog",
        ) from error
    return catalog


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
                        "details": {
                            "catalog": ruleset.to_record(),
                            "set_sha256": bound["ruleset"].set_sha256,
                        },
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
            _write_canonical(output / "stage-request.json", request)
            output.mkdir(parents=True, exist_ok=True)
            request_evidence = build_request_evidence(
                purpose=request["purpose"],
                request_id=request["request_id"],
                target_hardware=request["target_hardware"],
                change_summary=request["change_summary"],
                main_assembly=request["main_assembly"],
                handoff_digest=request["upload_inventory_sha256"],
                platform_config_digest=request["platform_config_sha256"],
            )
            (output / "request.json").write_bytes(
                request_evidence_bytes(request_evidence)
            )
            _write_canonical(
                output / "inputs.json",
                {
                    "artifacts": {role: artifact.to_record() for role, artifact in bound.items()},
                    "upload_inventory": actual,
                },
            )
            _write_canonical(
                output / "ruleset.json",
                {
                    "artifact": bound["ruleset"].to_record(),
                    "catalog": ruleset.to_record(),
                },
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
        if result.actual_executor_sha256 != claim.executor_sha256:
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
    "RulesetCatalog",
    "RulesetRoot",
    "read_ruleset_catalog",
    "ruleset_record_roots",
]
