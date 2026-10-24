"""Pure admission facts and deterministic stage-command projection."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..interface_records import REQUEST_PURPOSES
from ..io import PipelineError, artifact_path_parts, digest, parse_data
from .contracts import (
    ArtifactRef,
    ProtocolError,
    RunAdmission,
    StageCommand,
    StageDecision,
    StageId,
    StageResult,
)
from .discover import DISCOVER_REQUEST_SCHEMA
from .freeze import (
    CONFIGURATION_KIND,
    FREEZE_REQUEST_SCHEMA,
    ORIGINALS_KIND,
    RULESET_CATALOG_SCHEMA,
    RULESET_KIND,
)

ADMISSION_SPEC_SCHEMA = "description-pipeline.admission-spec/v1"
ARTIFACT_MANIFEST_SCHEMA = "description-pipeline.artifact-manifest/v1"
SUPPORTED_PROJECTION_STAGES = ("freeze", "discover")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_ADMISSION_FIELDS = frozenset(
    {
        "contract_sha256",
        "created_at",
        "expected_executor_sha256",
        "expected_native_tool_sha256",
        "from_stage",
        "main_assembly",
        "originals",
        "parent_run_id",
        "policy_sha256",
        "principal",
        "purpose",
        "request_id",
        "ruleset",
        "run_id",
        "schema_version",
        "target_hardware",
        "change_summary",
        "configuration",
    }
)


def _require_text(value: object, code: str, where: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProtocolError(code, {"where": where})
    return value


def _require_digest(value: object, code: str, where: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProtocolError(code, {"where": where})
    return value


def _require_safe_multiline(value: object, code: str, where: str) -> str:
    text = _require_text(value, code, where)
    if not text.strip() or any((ord(char) < 32 and char != "\n") or ord(char) == 127 for char in text):
        raise ProtocolError(code, {"where": where})
    return text


@dataclass(frozen=True, slots=True)
class ArtifactMember:
    """One member of an already verified immutable artifact."""

    path: str
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _require_text(self.path, "artifact.member_invalid", "path")
        try:
            parts = artifact_path_parts(self.path)
        except PipelineError as error:
            raise ProtocolError("artifact.member_invalid", {"where": "path", "reason": str(error)}) from error
        if not parts or "/".join(parts) != self.path:
            raise ProtocolError("artifact.member_invalid", {"where": "path"})
        if type(self.bytes) is not int or self.bytes < 0:
            raise ProtocolError("artifact.member_invalid", {"where": "bytes"})
        _require_digest(self.sha256, "artifact.member_invalid", "sha256")

    def to_record(self) -> dict[str, Any]:
        return {"bytes": self.bytes, "path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    """A typed snapshot of bytes already checked by the shared ArtifactStore."""

    ref: ArtifactRef
    members: tuple[ArtifactMember, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ArtifactRef):
            raise ProtocolError("artifact.invalid", {"where": "ref"})
        if (
            not isinstance(self.members, tuple)
            or not self.members
            or not all(isinstance(item, ArtifactMember) for item in self.members)
        ):
            raise ProtocolError("artifact.invalid", {"where": "members"})
        ordered = tuple(sorted(self.members, key=lambda item: item.path))
        if ordered != self.members:
            raise ProtocolError("artifact.invalid", {"where": "members", "reason": "not-sorted"})
        folded: dict[str, str] = {}
        for member in self.members:
            key = member.path.casefold()
            if key in folded:
                raise ProtocolError(
                    "artifact.invalid",
                    {"where": "members", "reason": "case-collision", "paths": [folded[key], member.path]},
                )
            folded[key] = member.path
        paths = set(folded)
        for member in self.members:
            parts = member.path.split("/")
            for index in range(1, len(parts)):
                if "/".join(parts[:index]).casefold() in paths:
                    raise ProtocolError(
                        "artifact.invalid",
                        {"where": "members", "reason": "file-directory-collision", "path": member.path},
                    )
        if digest({"members": [item.to_record() for item in self.members]}) != self.ref.set_sha256:
            raise ProtocolError("artifact.invalid", {"where": "set_sha256"})

    @classmethod
    def from_store_record(cls, ref: ArtifactRef, record: Mapping[str, Any]) -> VerifiedArtifact:
        """Convert one successful ``ArtifactStore.verify`` result into immutable facts."""

        if not isinstance(record, Mapping) or set(record) != {
            "artifact_id",
            "kind",
            "members",
            "producer",
            "schema_version",
            "set_sha256",
        }:
            raise ProtocolError("artifact.invalid", {"where": "manifest"})
        producer = record.get("producer")
        if not isinstance(producer, Mapping) or set(producer) != {"run_id", "stage", "tool_sha256"}:
            raise ProtocolError("artifact.invalid", {"where": "producer"})
        if (
            record["schema_version"] != ARTIFACT_MANIFEST_SCHEMA
            or record["artifact_id"] != ref.artifact_id
            or record["kind"] != ref.kind
            or record["set_sha256"] != ref.set_sha256
            or producer["run_id"] != ref.producer_run_id
            or producer["stage"] != ref.producer_stage
            or producer["tool_sha256"] != ref.producer_tool_sha256
        ):
            raise ProtocolError("artifact.invalid", {"where": "manifest", "reason": "ref-mismatch"})
        raw_members = record["members"]
        if not isinstance(raw_members, list):
            raise ProtocolError("artifact.invalid", {"where": "members"})
        members = tuple(
            sorted(
                (
                    ArtifactMember(
                        path=item.get("path"),
                        bytes=item.get("bytes"),
                        sha256=item.get("sha256"),
                    )
                    for item in raw_members
                    if isinstance(item, Mapping) and set(item) == {"path", "bytes", "sha256"}
                ),
                key=lambda item: item.path,
            )
        )
        if len(members) != len(raw_members):
            raise ProtocolError("artifact.invalid", {"where": "members"})
        return cls(ref=ref, members=members)

    @property
    def inventory(self) -> dict[str, str]:
        return {member.path: member.sha256 for member in self.members}

    @property
    def inventory_sha256(self) -> str:
        return digest(self.inventory)

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact": self.ref.to_record(),
            "members": [member.to_record() for member in self.members],
        }


@dataclass(frozen=True, slots=True)
class VerifiedRuleset:
    """One ruleset artifact whose version came from its verified catalog bytes."""

    artifact: VerifiedArtifact
    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, VerifiedArtifact) or self.artifact.ref.kind != RULESET_KIND:
            raise ProtocolError("admission.invalid", {"where": "ruleset"})
        _require_text(self.version, "admission.invalid", "ruleset.version")

    @classmethod
    def from_catalog_bytes(
        cls,
        artifact: VerifiedArtifact,
        catalog_bytes: bytes,
    ) -> VerifiedRuleset:
        if not isinstance(catalog_bytes, bytes) or not catalog_bytes:
            raise ProtocolError("admission.ruleset_invalid", {"where": "catalog.json"})
        expected = artifact.inventory.get("catalog.json")
        if expected is None or hashlib.sha256(catalog_bytes).hexdigest() != expected:
            raise ProtocolError("admission.ruleset_invalid", {"where": "catalog.json", "reason": "bytes-mismatch"})
        try:
            record = parse_data(catalog_bytes, Path("catalog.json"))
        except PipelineError as error:
            raise ProtocolError("admission.ruleset_invalid", {"reason": str(error)}) from error
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != RULESET_CATALOG_SCHEMA
            or not isinstance(record.get("version"), str)
            or not record["version"]
            or not isinstance(record.get("records"), dict)
        ):
            raise ProtocolError("admission.ruleset_invalid", {"where": "catalog.json"})
        return cls(artifact=artifact, version=record["version"])

    def to_record(self) -> dict[str, Any]:
        return {"artifact": self.artifact.to_record(), "version": self.version}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> VerifiedRuleset:
        if not isinstance(record, Mapping) or set(record) != {"artifact", "version"}:
            raise ProtocolError("admission.ruleset_invalid", {"where": "record"})
        return cls(
            artifact=_verified_from_record(record["artifact"]),
            version=record["version"],
        )


@dataclass(frozen=True, slots=True)
class AdmissionSpec:
    """The one immutable administrative and input-closure fact for a run."""

    schema_version: str
    run_id: str
    request_id: str
    parent_run_id: str | None
    from_stage: StageId | None
    principal: str
    target_hardware: str
    purpose: str
    change_summary: str
    main_assembly: str
    originals: VerifiedArtifact
    ruleset: VerifiedRuleset
    configuration: VerifiedArtifact
    expected_native_tool_sha256: str
    contract_sha256: str
    policy_sha256: str
    expected_executor_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.schema_version != ADMISSION_SPEC_SCHEMA:
            raise ProtocolError("admission.schema_unsupported", {"value": self.schema_version})
        for field in ("run_id", "request_id", "principal", "target_hardware"):
            _require_text(getattr(self, field), "admission.invalid", field)
        if self.parent_run_id is not None:
            _require_text(self.parent_run_id, "admission.invalid", "parent_run_id")
            if self.parent_run_id == self.run_id:
                raise ProtocolError("admission.invalid", {"where": "parent_run_id", "reason": "self-reference"})
        if self.from_stage not in {None, "freeze", "discover"}:
            raise ProtocolError("admission.invalid", {"where": "from_stage", "value": self.from_stage})
        if self.purpose not in REQUEST_PURPOSES:
            raise ProtocolError("admission.invalid", {"where": "purpose"})
        _require_safe_multiline(self.change_summary, "admission.invalid", "change_summary")
        _require_text(self.main_assembly, "admission.invalid", "main_assembly")
        if not isinstance(self.originals, VerifiedArtifact) or self.originals.ref.kind != ORIGINALS_KIND:
            raise ProtocolError("admission.invalid", {"where": "originals"})
        if not isinstance(self.ruleset, VerifiedRuleset):
            raise ProtocolError("admission.invalid", {"where": "ruleset"})
        if not isinstance(self.configuration, VerifiedArtifact) or self.configuration.ref.kind != CONFIGURATION_KIND:
            raise ProtocolError("admission.invalid", {"where": "configuration"})
        if self.main_assembly not in self.originals.inventory:
            raise ProtocolError(
                "admission.invalid",
                {"where": "main_assembly", "reason": "absent-from-originals"},
            )
        for field in (
            "expected_native_tool_sha256",
            "contract_sha256",
            "policy_sha256",
            "expected_executor_sha256",
        ):
            _require_digest(getattr(self, field), "admission.invalid", field)
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ProtocolError("admission.invalid", {"where": "created_at"})

    @property
    def admission_sha256(self) -> str:
        return digest(self.identity_record())

    def identity_record(self) -> dict[str, Any]:
        return {
            "change_summary": self.change_summary,
            "configuration": self.configuration.to_record(),
            "contract_sha256": self.contract_sha256,
            "created_at": self.created_at.isoformat(),
            "expected_executor_sha256": self.expected_executor_sha256,
            "expected_native_tool_sha256": self.expected_native_tool_sha256,
            "from_stage": self.from_stage,
            "main_assembly": self.main_assembly,
            "originals": self.originals.to_record(),
            "parent_run_id": self.parent_run_id,
            "policy_sha256": self.policy_sha256,
            "principal": self.principal,
            "purpose": self.purpose,
            "request_id": self.request_id,
            "ruleset": self.ruleset.to_record(),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "target_hardware": self.target_hardware,
        }

    def to_record(self) -> dict[str, Any]:
        return {
            **{
                key: value
                for key, value in self.identity_record().items()
                if key not in {"configuration", "originals", "ruleset"}
            },
            "originals": self.originals.to_record(),
            "ruleset": self.ruleset.to_record(),
            "configuration": self.configuration.to_record(),
            "created_at": self.created_at.isoformat(),
            "admission_sha256": self.admission_sha256,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> AdmissionSpec:
        if not isinstance(record, Mapping) or set(record) != _ADMISSION_FIELDS | {"admission_sha256"}:
            raise ProtocolError("admission.invalid", {"where": "record"})
        originals = _verified_from_record(record["originals"])
        ruleset = VerifiedRuleset.from_record(record["ruleset"])
        configuration = _verified_from_record(record["configuration"])
        value = cls(
            schema_version=record["schema_version"],
            run_id=record["run_id"],
            request_id=record["request_id"],
            parent_run_id=record["parent_run_id"],
            from_stage=record["from_stage"],
            principal=record["principal"],
            target_hardware=record["target_hardware"],
            purpose=record["purpose"],
            change_summary=record["change_summary"],
            main_assembly=record["main_assembly"],
            originals=originals,
            ruleset=ruleset,
            configuration=configuration,
            expected_native_tool_sha256=record["expected_native_tool_sha256"],
            contract_sha256=record["contract_sha256"],
            policy_sha256=record["policy_sha256"],
            expected_executor_sha256=record["expected_executor_sha256"],
            created_at=datetime.fromisoformat(record["created_at"].replace("Z", "+00:00")),
        )
        if value.admission_sha256 != record["admission_sha256"]:
            raise ProtocolError("admission.digest_mismatch", {"run_id": value.run_id})
        return value


def _verified_from_record(record: object) -> VerifiedArtifact:
    if not isinstance(record, Mapping) or set(record) != {"artifact", "members"}:
        raise ProtocolError("artifact.invalid", {"where": "verified_record"})
    ref = ArtifactRef.from_record(record["artifact"])
    raw_members = record["members"]
    if not isinstance(raw_members, list):
        raise ProtocolError("artifact.invalid", {"where": "members"})
    members = tuple(
        ArtifactMember(path=item.get("path"), bytes=item.get("bytes"), sha256=item.get("sha256"))
        for item in raw_members
        if isinstance(item, Mapping) and set(item) == {"path", "bytes", "sha256"}
    )
    if len(members) != len(raw_members):
        raise ProtocolError("artifact.invalid", {"where": "members"})
    return VerifiedArtifact(ref=ref, members=members)


def build_run_admission(spec: AdmissionSpec) -> RunAdmission:
    """Project the canonical RunAdmission from the typed admission fact."""

    if not isinstance(spec, AdmissionSpec):
        raise ProtocolError("admission.spec_required", {"stage": "run"})
    return RunAdmission.create(
        run_id=spec.run_id,
        request_id=spec.request_id,
        parent_run_id=spec.parent_run_id,
        from_stage=spec.from_stage,
        request=spec.to_record(),
        created_at=spec.created_at,
    )


def validate_run_admission(spec: AdmissionSpec, admission: RunAdmission) -> RunAdmission:
    if not isinstance(admission, RunAdmission):
        raise ProtocolError("admission.run_required")
    expected = build_run_admission(spec)
    if admission != expected:
        raise ProtocolError(
            "admission.run_mismatch",
            {
                "run_id": spec.run_id,
                "fields": [
                    field
                    for field in (
                        "run_id",
                        "request_id",
                        "parent_run_id",
                        "from_stage",
                        "request",
                        "request_digest",
                        "created_at",
                    )
                    if getattr(admission, field) != getattr(expected, field)
                ],
            },
        )
    return admission


@dataclass(frozen=True, slots=True)
class VerifiedPredecessor:
    """One committed predecessor command, result and decision as one fact."""

    stage: StageId
    command: StageCommand
    result: StageResult
    decision: StageDecision

    def __post_init__(self) -> None:
        if (
            self.command.stage != self.stage
            or self.result.stage != self.stage
            or self.decision.stage != self.stage
            or self.command.command_id != self.result.command_id
            or self.command.command_id != self.decision.command_id
            or self.command.run_id != self.result.run_id
            or self.command.run_id != self.decision.run_id
            or self.command.request_digest != self.result.request_digest
            or self.result.digest != self.decision.result_digest
            or self.decision.result_record_artifact is None
            or self.result.output_artifact.kind == "stage_result"
            or self.result.actual_executor_sha256 != self.decision.actual_executor_sha256
            or self.result.outcome != "completed"
            or self.decision.outcome != "completed"
            or self.decision.failure_envelope is not None
        ):
            raise ProtocolError("admission.predecessor_invalid", {"stage": self.stage})


def _base_command(
    spec: AdmissionSpec,
    *,
    stage: StageId,
    request: Mapping[str, Any],
    input_artifacts: tuple[ArtifactRef, ...],
    predecessor_decision_id: str | None,
) -> StageCommand:
    command_identity = digest(
        {
            "admission_sha256": spec.admission_sha256,
            "predecessor_decision_id": predecessor_decision_id,
            "stage": stage,
        }
    )
    return StageCommand.create(
        command_id=f"{stage}:{command_identity}",
        run_id=spec.run_id,
        stage=stage,
        idempotency_key=f"{spec.run_id}/{stage}",
        request=request,
        input_artifacts=input_artifacts,
        config_snapshot_sha256=spec.configuration.ref.set_sha256,
        contract_sha256=spec.contract_sha256,
        policy_sha256=spec.policy_sha256,
        expected_executor_sha256=spec.expected_executor_sha256,
        predecessor_decision_id=predecessor_decision_id,
    )


def build_freeze_command(spec: AdmissionSpec) -> StageCommand:
    """Project the one Freeze command from admission facts; no separate parameters."""

    if not isinstance(spec, AdmissionSpec):
        raise ProtocolError("admission.spec_required", {"stage": "freeze"})
    if spec.from_stage not in {None, "freeze"}:
        raise ProtocolError("admission.stage_mismatch", {"stage": "freeze", "from_stage": spec.from_stage})
    request = {
        "schema_version": FREEZE_REQUEST_SCHEMA,
        "request_id": spec.request_id,
        "principal": spec.principal,
        "target_hardware": spec.target_hardware,
        "purpose": spec.purpose,
        "change_summary": spec.change_summary,
        "main_assembly": spec.main_assembly,
        "ruleset_version": spec.ruleset.version,
        "originals_artifact_id": spec.originals.ref.artifact_id,
        "ruleset_artifact_id": spec.ruleset.artifact.ref.artifact_id,
        "configuration_artifact_id": spec.configuration.ref.artifact_id,
        "upload_inventory_sha256": spec.originals.inventory_sha256,
        "ruleset_catalog_sha256": spec.ruleset.artifact.ref.set_sha256,
        "platform_config_sha256": spec.configuration.ref.set_sha256,
    }
    return _base_command(
        spec,
        stage="freeze",
        request=request,
        input_artifacts=(spec.originals.ref, spec.ruleset.artifact.ref, spec.configuration.ref),
        predecessor_decision_id=None,
    )


def _submission_identity(spec: AdmissionSpec) -> dict[str, str | None]:
    return {
        "hardware_id": spec.target_hardware,
        "owner": spec.principal,
        "change_summary": spec.change_summary,
        "parent_revision": None,
        "control_reference": spec.originals.inventory_sha256,
        "as_of": spec.created_at.strftime("%Y%m%d"),
        "inventory_digest": spec.originals.inventory_sha256,
    }


def build_discover_command(spec: AdmissionSpec, predecessor: VerifiedPredecessor) -> StageCommand:
    """Project Discover from admission plus one verified committed Freeze fact."""

    if not isinstance(spec, AdmissionSpec):
        raise ProtocolError("admission.spec_required", {"stage": "discover"})
    if spec.from_stage not in {None, "freeze", "discover"}:
        raise ProtocolError("admission.stage_mismatch", {"stage": "discover", "from_stage": spec.from_stage})
    if not isinstance(predecessor, VerifiedPredecessor) or predecessor.stage != "freeze":
        raise ProtocolError("admission.predecessor_invalid", {"stage": "discover"})
    if predecessor.command.run_id != spec.run_id or predecessor.decision.run_id != spec.run_id:
        raise ProtocolError("admission.predecessor_invalid", {"reason": "run-mismatch"})
    request = {
        "schema_version": DISCOVER_REQUEST_SCHEMA,
        "freeze_artifact_id": predecessor.result.output_artifact.artifact_id,
        "source_set_artifact_id": spec.originals.ref.artifact_id,
        "ruleset_artifact_id": spec.ruleset.artifact.ref.artifact_id,
        "configuration_artifact_id": spec.configuration.ref.artifact_id,
        "source_files": spec.originals.inventory,
        "source_inventory_sha256": spec.originals.inventory_sha256,
        "main_assembly": spec.main_assembly,
        "purpose": spec.purpose,
        "ruleset_version": spec.ruleset.version,
        "ruleset_catalog_sha256": spec.ruleset.artifact.ref.set_sha256,
        "platform_config_sha256": spec.configuration.ref.set_sha256,
        "submission_identity": _submission_identity(spec),
        "expected_native_tool_sha256": spec.expected_native_tool_sha256,
    }
    return _base_command(
        spec,
        stage="discover",
        request=request,
        input_artifacts=(
            predecessor.result.output_artifact,
            spec.originals.ref,
            spec.ruleset.artifact.ref,
            spec.configuration.ref,
        ),
        predecessor_decision_id=predecessor.decision.decision_id,
    )


def _command_differences(expected: StageCommand, actual: StageCommand) -> list[str]:
    fields = []
    for field in (
        "schema_version",
        "command_id",
        "run_id",
        "kind",
        "stage",
        "idempotency_key",
        "request_digest",
        "input_artifacts",
        "config_snapshot_sha256",
        "contract_sha256",
        "policy_sha256",
        "expected_executor_sha256",
        "predecessor_decision_id",
    ):
        if getattr(expected, field) != getattr(actual, field):
            fields.append(field)
    if expected.request != actual.request:
        expected_keys = set(expected.request)
        actual_keys = set(actual.request)
        if expected_keys != actual_keys:
            fields.extend(["request.fields"])
        fields.extend(
            f"request.{key}"
            for key in sorted(expected_keys & actual_keys)
            if expected.request[key] != actual.request[key]
        )
    return fields


def _validate(expected: StageCommand, command: StageCommand) -> StageCommand:
    if not isinstance(command, StageCommand):
        raise ProtocolError("admission.command_required", {"stage": expected.stage})
    if command != expected:
        raise ProtocolError(
            "admission.command_mismatch",
            {"stage": expected.stage, "fields": _command_differences(expected, command)},
        )
    return command


def validate_freeze_command(spec: AdmissionSpec, command: StageCommand) -> StageCommand:
    return _validate(build_freeze_command(spec), command)


def validate_discover_command(
    spec: AdmissionSpec,
    predecessor: VerifiedPredecessor,
    command: StageCommand,
) -> StageCommand:
    return _validate(build_discover_command(spec, predecessor), command)


def validate_stage_command(
    spec: AdmissionSpec,
    command: StageCommand,
    *,
    predecessor: VerifiedPredecessor | None = None,
) -> StageCommand:
    """The pure Store transaction hook; unsupported stages are explicit."""

    if command.stage == "freeze":
        if predecessor is not None:
            raise ProtocolError("admission.predecessor_invalid", {"stage": "freeze"})
        return validate_freeze_command(spec, command)
    if command.stage == "discover":
        if predecessor is None:
            raise ProtocolError("admission.predecessor_required", {"stage": "discover"})
        return validate_discover_command(spec, predecessor, command)
    raise ProtocolError("admission.stage_not_supported", {"stage": command.stage})


def validate_store_command(
    spec: AdmissionSpec,
    admission: RunAdmission,
    command: StageCommand,
    *,
    predecessor: VerifiedPredecessor | None = None,
) -> StageCommand:
    """Pure transaction hook for the common FactStore integration."""

    validate_run_admission(spec, admission)
    return validate_stage_command(spec, command, predecessor=predecessor)


__all__ = [
    "ADMISSION_SPEC_SCHEMA",
    "SUPPORTED_PROJECTION_STAGES",
    "AdmissionSpec",
    "ArtifactMember",
    "VerifiedArtifact",
    "VerifiedPredecessor",
    "VerifiedRuleset",
    "build_discover_command",
    "build_freeze_command",
    "build_run_admission",
    "validate_discover_command",
    "validate_freeze_command",
    "validate_run_admission",
    "validate_stage_command",
    "validate_store_command",
]
