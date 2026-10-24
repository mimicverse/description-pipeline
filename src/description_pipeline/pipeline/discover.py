"""Discover domain handler: sealed native observation to pure fixed-rule derivation."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ..interface_records import REQUEST_PURPOSES
from ..io import PipelineError, artifact_path_parts, canonical, confined, digest, file_digest, read_data
from ..orchestration.discover_protocol import SOURCE_ARCHIVE, seal_source_archive
from ..schemas import (
    ARTIFACT_MANIFEST_SCHEMA,
    DISCOVER_CHECKS_SCHEMA,
    DISCOVER_REQUEST_SCHEMA,
    DISCOVER_RESULT_SCHEMA,
    NATIVE_DISCOVER_EFFECT_SCHEMA,
    NATIVE_DISCOVER_REQUEST_SCHEMA,
    RULESET_CATALOG_SCHEMA,
)
from ..sources.solidworks.discover_stage import (
    NativeDiscoverRefusal,
    NativeDiscoverRequest,
    NativeObservation,
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
    StageDeferral,
    StageEffectWait,
    StageExecution,
    StageFailure,
)
from .effects import EffectState, StageEffectPort
from .engine import StageWorkspace
from .freeze import (
    RulesetCatalog,
    configuration_snapshot_digest,
    read_ruleset_catalog,
    ruleset_record_roots,
)

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
        "expected_native_tool_record_sha256",
        "native_tool_requirement_artifact_id",
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


class DiscoverDomainAdapter(Protocol):
    """Native/domain transport used only after common effect authorization."""

    def submit(
        self,
        source_archive: Path,
        request: NativeDiscoverRequest,
        workspace: Path,
    ) -> NativeObservation | None: ...

    def resume_transfer(
        self,
        source_archive: Path,
        request: NativeDiscoverRequest,
        workspace: Path,
    ) -> NativeObservation | None: ...

    def materialize(
        self,
        artifact,
        request: NativeDiscoverRequest,
        destination: Path,
    ) -> NativeObservation: ...


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
    for field in (
        "ruleset_catalog_sha256",
        "platform_config_sha256",
        "expected_native_tool_sha256",
        "expected_native_tool_record_sha256",
    ):
        _sha(request[field], field)
    _text(
        request["native_tool_requirement_artifact_id"],
        "native_tool_requirement_artifact_id",
    )
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
    if (
        freeze.producer_stage != "freeze"
        or (
            command.diagnostic_scope is None
            and freeze.producer_run_id != command.run_id
        )
    ):
        raise NativeDiscoverRefusal("discover.freeze_binding_mismatch", "Freeze artifact belongs to another run")
    if bound["ruleset"].set_sha256 != request["ruleset_catalog_sha256"]:
        raise NativeDiscoverRefusal("discover.ruleset_binding_mismatch", "Ruleset artifact differs from the request")
    if command.config_snapshot_sha256 != request["platform_config_sha256"]:
        raise NativeDiscoverRefusal(
            "discover.configuration_binding_mismatch",
            "Configuration identity differs from the command snapshot",
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


def _ruleset_version(root: Path, expected: str) -> RulesetCatalog:
    try:
        catalog = read_ruleset_catalog(root)
    except (PipelineError, OSError, ValueError) as error:
        raise NativeDiscoverRefusal("discover.ruleset_invalid", "Ruleset catalog is missing or unreadable") from error
    if catalog.version != expected:
        raise NativeDiscoverRefusal("discover.ruleset_invalid", "Ruleset catalog is invalid or names another version")
    try:
        ruleset_record_roots(root, catalog)
    except PipelineError as error:
        raise NativeDiscoverRefusal(
            "discover.ruleset_invalid",
            "Ruleset roots differ from their sealed catalog",
        ) from error
    return catalog


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


class DiscoverHandler:
    """Run one Discover command through the command-bound runtime effect port."""

    def __init__(self, native: DiscoverDomainAdapter) -> None:
        self.native = native

    @staticmethod
    def _effect_target(
        command: StageCommand,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "operation_id": command.command_id,
            "main_assembly": request["main_assembly"],
            "native_tool_sha256": request["expected_native_tool_sha256"],
        }

    @staticmethod
    def _effect_expected_state(
        command: StageCommand,
        request: Mapping[str, Any],
        source_archive_sha256: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": NATIVE_DISCOVER_EFFECT_SCHEMA,
            "operation_id": command.command_id,
            "request_digest": command.request_digest,
            "source_inventory_sha256": request["source_inventory_sha256"],
            "source_archive_sha256": source_archive_sha256,
            "native_tool_sha256": request["expected_native_tool_sha256"],
            "native_tool_record_sha256": request[
                "expected_native_tool_record_sha256"
            ],
            "native_tool_requirement_artifact_id": request[
                "native_tool_requirement_artifact_id"
            ],
        }

    @staticmethod
    def _verify_resume_wait(
        *,
        command: StageCommand,
        resume: StageEffectWait,
        settlement,
        effect_key: str,
        expected_state: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> None:
        outbound = settlement.outbound_artifact
        if outbound is None:
            raise ProtocolError(
                "discover.resume_outbound_missing",
                {"effect_key": effect_key},
            )
        expected_state_digest = digest(dict(expected_state))
        identity_digest = digest(
            {
                "command_id": command.command_id,
                "effect_id": settlement.effect_id,
                "effect_key": effect_key,
                "effect_kind": "native_operation",
                "expected_state": dict(expected_state),
                "expected_state_digest": expected_state_digest,
                "outbound_artifact_id": outbound.artifact_id,
                "outbound_digest": outbound.set_sha256,
                "target": dict(target),
            }
        )
        if (
            resume.run_id != command.run_id
            or resume.stage != command.stage
            or resume.command_id != command.command_id
            or resume.request_digest != command.request_digest
            or resume.effect_id != settlement.effect_id
            or resume.effect_key != effect_key
            or resume.effect_kind != "native_operation"
            or resume.intent_identity_digest != identity_digest
            or resume.outbound_artifact_id != outbound.artifact_id
            or resume.outbound_digest != outbound.set_sha256
            or resume.expected_state_digest != expected_state_digest
        ):
            raise ProtocolError(
                "discover.resume_identity_mismatch",
                {"effect_id": settlement.effect_id},
            )

    def execute(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        *,
        effects: StageEffectPort,
        resume: StageEffectWait | None = None,
    ) -> StageExecution | StageDeferral:
        try:
            if command.stage != "discover" or command.kind != "stage":
                raise NativeDiscoverRefusal("discover.stage_invalid", "Command does not authorize Discover")
            if (
                command.predecessor_decision_id is None
                and command.diagnostic_scope is None
            ):
                raise NativeDiscoverRefusal("discover.predecessor_invalid", "Discover requires Freeze predecessor")
            if claim.command_id != command.command_id or claim.executor_sha256 != command.expected_executor_sha256:
                raise NativeDiscoverRefusal("discover.claim_invalid", "Claim does not own this Discover command")
            if resume is not None and (
                resume.run_id != command.run_id
                or resume.stage != command.stage
                or resume.command_id != command.command_id
                or resume.request_digest != command.request_digest
            ):
                raise ProtocolError(
                    "discover.resume_command_mismatch",
                    {"command_id": command.command_id},
                )
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
            configuration_files = _artifact_files(
                roots["configuration"],
                bound["configuration"],
                role="configuration",
            )
            if set(configuration_files) != {"config-snapshot.json"}:
                raise NativeDiscoverRefusal(
                    "discover.configuration_binding_mismatch",
                    "The configuration artifact must carry exactly one config snapshot",
                )
            try:
                configuration_snapshot_sha256 = configuration_snapshot_digest(
                    roots["configuration"]
                )
            except PipelineError as error:
                raise NativeDiscoverRefusal(
                    "discover.configuration_binding_mismatch",
                    str(error),
                ) from error
            if (
                configuration_snapshot_sha256
                != request["platform_config_sha256"]
            ):
                raise NativeDiscoverRefusal(
                    "discover.configuration_binding_mismatch",
                    "The configuration snapshot differs from the request",
                )
            _artifact_files(roots["freeze"], bound["freeze"], role="freeze")
            native_request = NativeDiscoverRequest(
                schema_version=NATIVE_DISCOVER_REQUEST_SCHEMA,
                operation_id=command.command_id,
                command_id=command.command_id,
                run_id=command.run_id,
                request_digest=command.request_digest,
                source_files=source_files,
                source_inventory_sha256=request["source_inventory_sha256"],
                main_assembly=request["main_assembly"],
                purpose=request["purpose"],
                expected_native_tool_sha256=request["expected_native_tool_sha256"],
                expected_native_tool_record_sha256=request[
                    "expected_native_tool_record_sha256"
                ],
                native_tool_requirement_artifact_id=request[
                    "native_tool_requirement_artifact_id"
                ],
            )
            obtained = self._obtain_observation(
                command,
                claim,
                workspace,
                native_request,
                roots["source_set"],
                effects,
                resume,
            )
            if isinstance(obtained, StageDeferral):
                return obtained
            effect_artifact, observation = obtained
            try:
                observation = admit_native_observation(
                    observation.root,
                    request=native_request,
                    artifact_ref=effect_artifact,
                    resources_settled=observation.resources_settled,
                )
            except NativeDiscoverRefusal as refusal:
                raise StageFailure(
                    refusal.code,
                    refusal.message,
                    evidence_artifacts=(effect_artifact,),
                ) from refusal
            except PipelineError as error:
                raise StageFailure(
                    "discover.observation_invalid",
                    str(error),
                    evidence_artifacts=(effect_artifact,),
                ) from error
            if (
                digest(dict(observation.native_tool))
                != request["expected_native_tool_record_sha256"]
            ):
                raise StageFailure(
                    "discover.native_tool_record_mismatch",
                    "Observed native tool record differs from the current frozen requirement",
                    evidence_artifacts=(effect_artifact,),
                )
            selection = dict(observation.selection)
            bindings_files = selection.get("files")
            if not isinstance(bindings_files, Mapping):
                raise StageFailure(
                    "discover.selection_invalid",
                    "Native observation carries no selection",
                    evidence_artifacts=(effect_artifact,),
                )
            selected_root = workspace.root / f"selected-{claim.fence}"
            _copy_selected(roots["source_set"], selected_root, bindings_files)
            output = workspace.root / f"discover-result-{claim.fence}"
            output.mkdir(parents=True, exist_ok=False)
            settings = DiscoverySettings(
                record_roots=ruleset_record_roots(
                    roots["ruleset"],
                    ruleset,
                ),
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
                raise StageFailure(
                    "discover.derivation_invalid",
                    str(error),
                    evidence_artifacts=(effect_artifact,),
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
                            "effect_artifact_id": effect_artifact.artifact_id,
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
                        "details": {
                            "version": ruleset.version,
                            "sha256": bound["ruleset"].set_sha256,
                            "roots": [root.to_record() for root in ruleset.roots],
                        },
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
                raise StageFailure(
                    "discover.findings",
                    "Discover derivation contains blocking findings",
                    evidence_artifacts=(effect_artifact,),
                )
            return StageExecution(
                outcome="completed",
                candidate_root=str(output),
                checks_sha256=digest(checks),
                resources_settled=observation.resources_settled,
            )
        except NativeDiscoverRefusal as refusal:
            raise StageFailure(refusal.code, refusal.message) from refusal

    def _obtain_observation(
        self,
        command: StageCommand,
        claim: ExecutionClaim,
        workspace: StageWorkspace,
        native_request: NativeDiscoverRequest,
        source_root: Path,
        effects: StageEffectPort,
        resume: StageEffectWait | None,
    ) -> tuple[ArtifactRef, NativeObservation] | StageDeferral:
        effect_key = f"native-discover:{command.command_id}"
        settlement = effects.effect_settlement_for_key(effect_key)
        state = None if settlement is None else settlement.state
        if resume is not None and settlement is None:
            raise ProtocolError(
                "discover.resume_effect_missing",
                {"command_id": command.command_id},
            )
        if resume is not None:
            _unused_outbound, source_archive_sha256 = self._write_outbound(
                workspace,
                claim,
                native_request,
                source_root,
            )
            self._verify_resume_wait(
                command=command,
                resume=resume,
                settlement=settlement,
                effect_key=effect_key,
                expected_state=self._effect_expected_state(
                    command,
                    native_request.to_record(),
                    source_archive_sha256,
                ),
                target=self._effect_target(
                    command,
                    native_request.to_record(),
                ),
            )
        if state in {EffectState.UNKNOWN, EffectState.CONFLICT}:
            raise ProtocolError(
                "effect.unresolved",
                {"effect_key": effect_key, "state": state.value},
            )
        if state is EffectState.DEFINITELY_FAILED:
            raise StageFailure(
                "discover.native_definitely_failed",
                "The native Discover effect is definitely failed and cannot be resent",
            )
        if state is EffectState.SATISFIED:
            if settlement is None or settlement.payload_artifact is None:
                raise ProtocolError("effect.payload_missing", {"effect_key": effect_key})
            observation = self.native.materialize(
                settlement.payload_artifact,
                native_request,
                workspace.root / f"discover-observation-{claim.fence}",
            )
            return settlement.payload_artifact, observation

        outbound_root, source_archive_sha256 = self._write_outbound(
            workspace,
            claim,
            native_request,
            source_root,
        )
        expected_state = self._effect_expected_state(
            command,
            native_request.to_record(),
            source_archive_sha256,
        )
        intent = effects.prepare_effect(
            effect_key=effect_key,
            effect_kind="native_operation",
            target=self._effect_target(
                command,
                native_request.to_record(),
            ),
            expected_state=expected_state,
            outbound_root=outbound_root,
        )
        if settlement is not None and settlement.effect_id != intent.effect_id:
            raise ProtocolError("effect.identity_conflict", {"effect_key": effect_key})
        effects.authorize_dispatch(intent.effect_id, source="linux_discover_handler")
        try:
            observation = self.native.submit(
                outbound_root / SOURCE_ARCHIVE,
                native_request,
                workspace.root / f"native-discover-{claim.fence}",
            )
        except NativeDiscoverRefusal as refusal:
            failure = StageFailure(refusal.code, refusal.message)
            failure_root = workspace.root / f"native-discover-failure-{claim.fence}"
            try:
                _write_canonical(
                    failure_root / "native-failure-evidence.json",
                    {
                        "reason_code": refusal.code,
                        "message": refusal.message,
                        "detail": refusal.detail,
                    },
                )
                effects.record_response(
                    intent.effect_id,
                    source="windows_discover",
                    response_root=failure_root,
                )
                effects.record_observation(
                    intent.effect_id,
                    source="windows_discover",
                    state_class="known",
                    observation_root=failure_root,
                )
                settled = effects.effect_settlement(intent.effect_id)
                if settled.payload_artifact is not None:
                    failure = StageFailure(
                        refusal.code,
                        refusal.message,
                        evidence_artifacts=(settled.payload_artifact,),
                    )
            except Exception:
                failure.unavailable_reason = "native_failure_evidence_unavailable"
            raise failure from refusal
        if observation is None:
            return StageDeferral(intent.effect_id)
        effects.record_response(
            intent.effect_id,
            source="windows_discover",
            response_root=observation.root,
        )
        effects.record_observation(
            intent.effect_id,
            source="windows_discover",
            state_class="known",
            observation_root=observation.root,
        )
        settled = effects.effect_settlement(intent.effect_id)
        if settled.state is not EffectState.SATISFIED or settled.payload_artifact is None:
            raise ProtocolError(
                "effect.not_satisfied",
                {"effect_id": intent.effect_id, "state": settled.state.value},
            )
        observation = self.native.materialize(
            settled.payload_artifact,
            native_request,
            workspace.root / f"discover-observation-{claim.fence}",
        )
        return settled.payload_artifact, observation

    @staticmethod
    def _write_outbound(
        workspace: StageWorkspace,
        claim: ExecutionClaim,
        request: NativeDiscoverRequest,
        source_root: Path,
    ) -> tuple[Path, str]:
        root = workspace.root / f"discover-outbound-{claim.fence}"
        if root.exists():
            raise ProtocolError("discover.workspace_invalid", {"reason": "outbound-exists", "root": str(root)})
        root.mkdir(parents=True)
        _write_canonical(root / "request.json", request.to_record())
        _write_canonical(
            root / "source.json",
            {
                "source_files": dict(request.source_files),
                "source_inventory_sha256": request.source_inventory_sha256,
            },
        )
        archive = seal_source_archive(source_root, request.source_files, root / SOURCE_ARCHIVE)
        return root, file_digest(archive)

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
            or result.stage != "discover"
            or result.request_digest != command.request_digest
        ):
            raise ProtocolError("discover.candidate_mismatch", {"command_id": command.command_id})
        if claim.command_id != command.command_id or claim.executor_sha256 != command.expected_executor_sha256:
            raise ProtocolError("discover.claim_mismatch", {"command_id": command.command_id})
        if result.actual_executor_sha256 != claim.executor_sha256:
            raise ProtocolError("discover.executor_mismatch", {"command_id": command.command_id})
        if result.outcome in {"failed", "cancelled"} and not (
            isinstance(result.reason_code, str) and result.reason_code.startswith("discover.")
        ):
            raise ProtocolError("discover.failure_reason_invalid", {"command_id": command.command_id})
        return candidate


__all__ = [
    "CONFIGURATION_KIND",
    "DISCOVER_CHECKS_SCHEMA",
    "DISCOVER_REQUEST_SCHEMA",
    "DISCOVER_RESULT_SCHEMA",
    "FREEZE_RESULT_KIND",
    "RULESET_CATALOG_SCHEMA",
    "RULESET_KIND",
    "SOURCE_SET_KIND",
    "DiscoverDomainAdapter",
    "DiscoverHandler",
]
