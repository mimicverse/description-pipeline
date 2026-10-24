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
from ..repository.publish import CommitSpec
from ..schemas import (
    ADMISSION_SPEC_SCHEMA,
    ARTIFACT_MANIFEST_SCHEMA,
    DISCOVER_RESULT_SCHEMA,
    GENERATE_REQUEST_SCHEMA,
    TOOL_SCHEMA,
)
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
from .publish_contract import (
    PUBLISH_OUTPUT_CONTRACT,
    PUBLISH_REQUEST_SCHEMA,
    REPOSITORY_SNAPSHOT_FILE,
    REPOSITORY_SNAPSHOT_KIND,
    REPOSITORY_SNAPSHOT_METADATA,
    REPOSITORY_SNAPSHOT_SCHEMA,
)
from .verify_contract import (
    CERTIFICATE_FILENAME,
    CERTIFICATE_SCHEMA,
    EXECUTION_RECEIPT_FILENAME,
    OUTPUT_CONTRACT as VERIFY_OUTPUT_CONTRACT,
    REQUEST_SCHEMA as VERIFY_REQUEST_SCHEMA,
)

CAPTURE_ARTIFACT_KIND = "stage_output:capture"
GENERATE_ARTIFACT_KIND = "stage_output:generate"
GENERATE_OUTPUT_CONTRACT = "solidworks-to-urdf.generated-artifact/v1"
SUPPORTED_PROJECTION_STAGES = ("freeze", "discover", "capture", "generate", "verify", "publish")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_OID40 = re.compile(r"\A[0-9a-f]{40}\Z")
_ADMISSION_FIELDS = frozenset(
    {
        "contract_sha256",
        "created_at",
        "executors",
        "from_stage",
        "main_assembly",
        "native_tool_source_sha256",
        "native_tool_record_sha256",
        "native_tool",
        "originals",
        "parent_run_id",
        "policy_sha256",
        "platform_config_sha256",
        "principal",
        "publish_intent",
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


def _plain_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_value(item) for item in value]
    return value


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
class StageExecutors:
    """The exact tool identity required by each canonical stage."""

    freeze: str
    discover: str
    capture: str
    generate: str
    verify: str
    publish: str

    def __post_init__(self) -> None:
        for name in ("freeze", "discover", "capture", "generate", "verify", "publish"):
            _require_digest(getattr(self, name), "admission.invalid", f"executors.{name}")

    def for_stage(self, stage: StageId) -> str:
        if stage not in {"freeze", "discover", "capture", "generate", "verify", "publish"}:
            raise ProtocolError("admission.invalid", {"where": "executors.stage", "value": stage})
        return str(getattr(self, stage))

    def to_record(self) -> dict[str, str]:
        return {
            "capture": self.capture,
            "discover": self.discover,
            "freeze": self.freeze,
            "generate": self.generate,
            "publish": self.publish,
            "verify": self.verify,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> StageExecutors:
        expected = {"freeze", "discover", "capture", "generate", "verify", "publish"}
        if not isinstance(record, Mapping) or set(record) != expected:
            raise ProtocolError("admission.invalid", {"where": "executors"})
        return cls(**dict(record))


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
    executors: StageExecutors
    native_tool_source_sha256: str
    native_tool_record_sha256: str
    native_tool: Mapping[str, Any]
    contract_sha256: str
    policy_sha256: str
    platform_config_sha256: str
    publish_intent: VerifiedPublishIntent
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
        if self.from_stage not in {None, "freeze", "discover", "capture", "generate", "verify", "publish"}:
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
        if not isinstance(self.executors, StageExecutors):
            raise ProtocolError("admission.invalid", {"where": "executors"})
        for field in (
            "native_tool_source_sha256",
            "native_tool_record_sha256",
            "contract_sha256",
            "policy_sha256",
            "platform_config_sha256",
        ):
            _require_digest(getattr(self, field), "admission.invalid", field)
        native_tool = _plain_value(self.native_tool)
        if not isinstance(native_tool, dict):
            raise ProtocolError("admission.invalid", {"where": "native_tool"})
        if (
            native_tool.get("schema_version") != TOOL_SCHEMA
            or native_tool.get("source_sha256") != self.native_tool_source_sha256
            or digest(native_tool) != self.native_tool_record_sha256
        ):
            raise ProtocolError(
                "admission.invalid",
                {"where": "native_tool", "reason": "identity-mismatch"},
            )
        object.__setattr__(self, "native_tool", native_tool)
        if not isinstance(self.publish_intent, VerifiedPublishIntent):
            raise ProtocolError("admission.invalid", {"where": "publish_intent"})
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
            "executors": self.executors.to_record(),
            "from_stage": self.from_stage,
            "main_assembly": self.main_assembly,
            "native_tool_source_sha256": self.native_tool_source_sha256,
            "native_tool_record_sha256": self.native_tool_record_sha256,
            "native_tool": _plain_value(self.native_tool),
            "originals": self.originals.to_record(),
            "parent_run_id": self.parent_run_id,
            "policy_sha256": self.policy_sha256,
            "platform_config_sha256": self.platform_config_sha256,
            "principal": self.principal,
            "publish_intent": self.publish_intent.to_record(),
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
            executors=StageExecutors.from_record(record["executors"]),
            native_tool_source_sha256=record["native_tool_source_sha256"],
            native_tool_record_sha256=record["native_tool_record_sha256"],
            native_tool=record["native_tool"],
            contract_sha256=record["contract_sha256"],
            policy_sha256=record["policy_sha256"],
            platform_config_sha256=record["platform_config_sha256"],
            publish_intent=VerifiedPublishIntent.from_record(record["publish_intent"]),
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
    if not isinstance(raw_members, (list, tuple)):
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
            or self.command.diagnostic_scope is not None
            or self.result.diagnostic_scope is not None
        ):
            raise ProtocolError("admission.predecessor_invalid", {"stage": self.stage})


@dataclass(frozen=True, slots=True)
class VerifiedDiscoverResult:
    """The sealed result fields required to project Capture."""

    predecessor: VerifiedPredecessor
    prepared_sha256: str
    selected_files_sha256: str
    upload_inventory_sha256: str
    main_assembly: str
    native_observation_sha256: str
    native_tool_source_sha256: str
    native_tool_record_sha256: str
    source_set_artifact: ArtifactRef
    ruleset_artifact: ArtifactRef
    configuration_artifact: ArtifactRef

    def __post_init__(self) -> None:
        if not isinstance(self.predecessor, VerifiedPredecessor) or self.predecessor.stage != "discover":
            raise ProtocolError("admission.discover_result_invalid", {"where": "predecessor"})
        for field in (
            "prepared_sha256",
            "selected_files_sha256",
            "upload_inventory_sha256",
            "native_observation_sha256",
            "native_tool_source_sha256",
            "native_tool_record_sha256",
        ):
            _require_digest(getattr(self, field), "admission.discover_result_invalid", field)
        _require_text(self.main_assembly, "admission.discover_result_invalid", "main_assembly")
        if (
            not isinstance(self.source_set_artifact, ArtifactRef)
            or self.source_set_artifact.kind != ORIGINALS_KIND
            or not isinstance(self.ruleset_artifact, ArtifactRef)
            or self.ruleset_artifact.kind != RULESET_KIND
            or not isinstance(self.configuration_artifact, ArtifactRef)
            or self.configuration_artifact.kind != CONFIGURATION_KIND
        ):
            raise ProtocolError("admission.discover_result_invalid", {"where": "source-artifacts"})

    @classmethod
    def from_record(cls, predecessor: VerifiedPredecessor, record: Mapping[str, Any]) -> VerifiedDiscoverResult:
        required = {
            "schema_version",
            "command_id",
            "run_id",
            "request_digest",
            "selection",
            "prepared_sha256",
            "native_observation_sha256",
            "passed",
            "source_set_artifact",
            "ruleset_artifact",
            "configuration_artifact",
            "native_tool",
        }
        if not isinstance(record, Mapping) or not required <= set(record):
            raise ProtocolError("admission.discover_result_invalid", {"where": "record"})
        if (
            record["schema_version"] != DISCOVER_RESULT_SCHEMA
            or record["command_id"] != predecessor.command.command_id
            or record["run_id"] != predecessor.result.run_id
            or record["request_digest"] != predecessor.command.request_digest
            or record["passed"] is not True
        ):
            raise ProtocolError("admission.discover_result_invalid", {"where": "identity"})
        selection = record["selection"]
        if not isinstance(selection, Mapping):
            raise ProtocolError("admission.discover_result_invalid", {"where": "selection"})
        files = selection.get("files")
        upload = selection.get("upload_sha256")
        main = selection.get("main_assembly")
        if not isinstance(files, Mapping) or not files:
            raise ProtocolError("admission.discover_result_invalid", {"where": "selection.files"})
        selected_files_sha256 = digest({str(path): str(checksum) for path, checksum in files.items()})
        if selection.get("handoff_sha256") != selected_files_sha256:
            raise ProtocolError("admission.discover_result_invalid", {"where": "selection.handoff_sha256"})
        _require_digest(upload, "admission.discover_result_invalid", "selection.upload_sha256")
        _require_text(main, "admission.discover_result_invalid", "selection.main_assembly")
        native_tool = record["native_tool"]
        if not isinstance(native_tool, Mapping) or not native_tool:
            raise ProtocolError("admission.discover_result_invalid", {"where": "native_tool"})
        native_tool_source_sha256 = native_tool.get("source_sha256")
        _require_digest(
            native_tool_source_sha256,
            "admission.discover_result_invalid",
            "native_tool.source_sha256",
        )
        return cls(
            predecessor=predecessor,
            prepared_sha256=record["prepared_sha256"],
            selected_files_sha256=selected_files_sha256,
            upload_inventory_sha256=upload,
            main_assembly=main,
            native_observation_sha256=record["native_observation_sha256"],
            native_tool_source_sha256=native_tool_source_sha256,
            native_tool_record_sha256=digest(dict(native_tool)),
            source_set_artifact=ArtifactRef.from_record(record["source_set_artifact"]),
            ruleset_artifact=ArtifactRef.from_record(record["ruleset_artifact"]),
            configuration_artifact=ArtifactRef.from_record(record["configuration_artifact"]),
        )


@dataclass(frozen=True, slots=True)
class VerifiedVerifyResult:
    """The sealed Verify certificate projection required by Publish."""

    predecessor: VerifiedPredecessor
    artifact: VerifiedArtifact
    generate_artifact: ArtifactRef
    capture_artifact: ArtifactRef
    verified_subject_sha256: str
    certificate_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.predecessor, VerifiedPredecessor) or self.predecessor.stage != "verify":
            raise ProtocolError("admission.verify_result_invalid", {"where": "predecessor"})
        if (
            not isinstance(self.artifact, VerifiedArtifact)
            or self.artifact.ref != self.predecessor.result.output_artifact
            or set(self.artifact.inventory) != {CERTIFICATE_FILENAME, EXECUTION_RECEIPT_FILENAME}
        ):
            raise ProtocolError("admission.verify_result_invalid", {"where": "artifact"})
        if (
            not isinstance(self.generate_artifact, ArtifactRef)
            or self.generate_artifact.kind != GENERATE_ARTIFACT_KIND
            or self.generate_artifact.producer_stage != "generate"
            or not isinstance(self.capture_artifact, ArtifactRef)
            or self.capture_artifact.kind != CAPTURE_ARTIFACT_KIND
            or self.capture_artifact.producer_stage != "capture"
            or self.generate_artifact.artifact_id == self.capture_artifact.artifact_id
        ):
            raise ProtocolError("admission.verify_result_invalid", {"where": "artifact-pair"})
        _require_digest(
            self.verified_subject_sha256,
            "admission.verify_result_invalid",
            "verified_subject_sha256",
        )
        _require_digest(
            self.certificate_sha256,
            "admission.verify_result_invalid",
            "certificate_sha256",
        )
        if self.artifact.inventory[CERTIFICATE_FILENAME] != self.certificate_sha256:
            raise ProtocolError("admission.verify_result_invalid", {"where": "certificate_sha256"})
        request = self.predecessor.command.request
        if (
            request.get("generate_artifact_id") != self.generate_artifact.artifact_id
            or request.get("capture_artifact_id") != self.capture_artifact.artifact_id
            or self.verified_subject_sha256 != self.generate_artifact.set_sha256
            or self.predecessor.result.run_id != self.generate_artifact.producer_run_id
            or self.predecessor.result.run_id != self.capture_artifact.producer_run_id
        ):
            raise ProtocolError("admission.verify_result_invalid", {"where": "lineage"})

    @classmethod
    def from_artifact_bytes(
        cls,
        predecessor: VerifiedPredecessor,
        artifact: VerifiedArtifact,
        certificate_bytes: bytes,
    ) -> VerifiedVerifyResult:
        if not isinstance(predecessor, VerifiedPredecessor) or predecessor.stage != "verify":
            raise ProtocolError("admission.verify_result_invalid", {"where": "predecessor"})
        if not isinstance(artifact, VerifiedArtifact):
            raise ProtocolError("admission.verify_result_invalid", {"where": "artifact"})
        if (
            artifact.ref != predecessor.result.output_artifact
            or set(artifact.inventory) != {CERTIFICATE_FILENAME, EXECUTION_RECEIPT_FILENAME}
            or not isinstance(certificate_bytes, bytes)
            or not certificate_bytes
            or hashlib.sha256(certificate_bytes).hexdigest()
            != artifact.inventory[CERTIFICATE_FILENAME]
        ):
            raise ProtocolError("admission.verify_result_invalid", {"where": "certificate-bytes"})
        try:
            certificate = parse_data(certificate_bytes, Path(CERTIFICATE_FILENAME))
        except PipelineError as error:
            raise ProtocolError("admission.verify_result_invalid", {"where": "certificate"}) from error
        required = {
            "actual_verifier",
            "actual_verifier_sha256",
            "capture_artifact",
            "config_snapshot_sha256",
            "contract_sha256",
            "decision",
            "generate_artifact",
            "passed",
            "policy_sha256",
            "purpose",
            "request",
            "request_sha256",
            "schema_version",
            "subject_sha256",
            "verification_key",
        }
        if not isinstance(certificate, Mapping) or not required <= set(certificate):
            raise ProtocolError("admission.verify_result_invalid", {"where": "certificate"})
        proof = {key: certificate[key] for key in certificate if key != "verification_key"}
        if (
            certificate.get("schema_version") != CERTIFICATE_SCHEMA
            or certificate.get("decision") != "PASS"
            or certificate.get("passed") is not True
            or digest(proof) != certificate.get("verification_key")
            or not isinstance(certificate.get("actual_verifier"), Mapping)
            or digest(certificate["actual_verifier"]) != certificate.get("actual_verifier_sha256")
            or certificate.get("actual_verifier_sha256") != predecessor.result.actual_executor_sha256
            or certificate.get("actual_verifier_sha256") != predecessor.decision.actual_executor_sha256
        ):
            raise ProtocolError("admission.verify_result_invalid", {"where": "certificate-identity"})
        try:
            generate = ArtifactRef.from_record(certificate["generate_artifact"])
            capture = ArtifactRef.from_record(certificate["capture_artifact"])
        except (KeyError, TypeError, PipelineError) as error:
            raise ProtocolError("admission.verify_result_invalid", {"where": "certificate-artifacts"}) from error
        request = certificate["request"]
        command = predecessor.command
        if (
            not isinstance(request, Mapping)
            or digest(request) != certificate["request_sha256"]
            or request.get("generate_artifact") != generate.to_record()
            or request.get("capture_artifact") != capture.to_record()
            or request.get("config_snapshot_sha256") != command.config_snapshot_sha256
            or request.get("contract_sha256") != command.contract_sha256
            or request.get("expected_verifier_sha256") != command.expected_executor_sha256
            or request.get("output_contract") != VERIFY_OUTPUT_CONTRACT
            or request.get("policy_sha256") != command.policy_sha256
            or request.get("purpose") != command.request.get("purpose")
            or request.get("schema_version") != VERIFY_REQUEST_SCHEMA
            or command.request.get("generate_artifact_id") != generate.artifact_id
            or command.request.get("capture_artifact_id") != capture.artifact_id
            or certificate["contract_sha256"] != command.contract_sha256
            or certificate["policy_sha256"] != command.policy_sha256
            or certificate["config_snapshot_sha256"] != command.config_snapshot_sha256
            or certificate["purpose"] != command.request.get("purpose")
            or certificate["subject_sha256"] != generate.set_sha256
        ):
            raise ProtocolError("admission.verify_result_invalid", {"where": "certificate-lineage"})
        return cls(
            predecessor=predecessor,
            artifact=artifact,
            generate_artifact=generate,
            capture_artifact=capture,
            verified_subject_sha256=generate.set_sha256,
            certificate_sha256=hashlib.sha256(certificate_bytes).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class VerifiedRepositorySnapshot:
    """One verified repository bundle and the exact refs it can materialize."""

    artifact: VerifiedArtifact
    repository: str
    origin_url: str
    base_ref: str
    expected_base_oid: str
    branch_ref: str
    expected_head_oid: str | None
    bundle_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.artifact, VerifiedArtifact)
            or self.artifact.ref.kind != REPOSITORY_SNAPSHOT_KIND
            or set(self.artifact.inventory) != {REPOSITORY_SNAPSHOT_FILE, REPOSITORY_SNAPSHOT_METADATA}
        ):
            raise ProtocolError("admission.publish_snapshot_invalid", {"where": "artifact"})
        _require_text(self.repository, "admission.publish_snapshot_invalid", "repository")
        _require_text(self.origin_url, "admission.publish_snapshot_invalid", "origin_url")
        if "/" not in self.repository:
            raise ProtocolError("admission.publish_snapshot_invalid", {"where": "repository"})
        for field in ("base_ref", "branch_ref"):
            value = _require_text(
                getattr(self, field),
                "admission.publish_snapshot_invalid",
                field,
            )
            if value.startswith("-") or ".." in value or value.endswith(".lock"):
                raise ProtocolError("admission.publish_snapshot_invalid", {"where": field})
        if _OID40.fullmatch(self.expected_base_oid) is None:
            raise ProtocolError("admission.publish_snapshot_invalid", {"where": "expected_base_oid"})
        if self.expected_head_oid is not None and _OID40.fullmatch(self.expected_head_oid) is None:
            raise ProtocolError("admission.publish_snapshot_invalid", {"where": "expected_head_oid"})
        _require_digest(
            self.bundle_sha256,
            "admission.publish_snapshot_invalid",
            "bundle_sha256",
        )
        if self.artifact.inventory[REPOSITORY_SNAPSHOT_FILE] != self.bundle_sha256:
            raise ProtocolError("admission.publish_snapshot_invalid", {"where": "bundle_sha256"})

    @classmethod
    def from_metadata_bytes(
        cls,
        artifact: VerifiedArtifact,
        metadata_bytes: bytes,
    ) -> VerifiedRepositorySnapshot:
        if not isinstance(artifact, VerifiedArtifact):
            raise ProtocolError("admission.publish_snapshot_invalid", {"where": "artifact"})
        if not isinstance(metadata_bytes, bytes) or not metadata_bytes:
            raise ProtocolError("admission.publish_snapshot_invalid", {"where": "metadata"})
        expected_sha256 = artifact.inventory.get(REPOSITORY_SNAPSHOT_METADATA)
        if expected_sha256 is None or hashlib.sha256(metadata_bytes).hexdigest() != expected_sha256:
            raise ProtocolError(
                "admission.publish_snapshot_invalid",
                {"where": "metadata", "reason": "bytes-mismatch"},
            )
        try:
            record = parse_data(metadata_bytes, Path(REPOSITORY_SNAPSHOT_METADATA))
        except PipelineError as error:
            raise ProtocolError("admission.publish_snapshot_invalid", {"where": "metadata"}) from error
        required = {
            "schema_version",
            "repository",
            "origin_url",
            "base_ref",
            "base_oid",
            "branch_ref",
            "head_oid",
            "bundle_sha256",
        }
        if (
            not isinstance(record, dict)
            or set(record) != required
            or record["schema_version"] != REPOSITORY_SNAPSHOT_SCHEMA
        ):
            raise ProtocolError("admission.publish_snapshot_invalid", {"where": "metadata-fields"})
        return cls(
            artifact=artifact,
            repository=record["repository"],
            origin_url=record["origin_url"],
            base_ref=record["base_ref"],
            expected_base_oid=record["base_oid"],
            branch_ref=record["branch_ref"],
            expected_head_oid=record["head_oid"],
            bundle_sha256=record["bundle_sha256"],
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact.to_record(),
            "base_ref": self.base_ref,
            "branch_ref": self.branch_ref,
            "bundle_sha256": self.bundle_sha256,
            "expected_base_oid": self.expected_base_oid,
            "expected_head_oid": self.expected_head_oid,
            "origin_url": self.origin_url,
            "repository": self.repository,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> VerifiedRepositorySnapshot:
        required = {
            "artifact",
            "base_ref",
            "branch_ref",
            "bundle_sha256",
            "expected_base_oid",
            "expected_head_oid",
            "origin_url",
            "repository",
        }
        if not isinstance(record, Mapping) or set(record) != required:
            raise ProtocolError("admission.publish_snapshot_invalid", {"where": "record"})
        return cls(
            artifact=_verified_from_record(record["artifact"]),
            repository=record["repository"],
            origin_url=record["origin_url"],
            base_ref=record["base_ref"],
            expected_base_oid=record["expected_base_oid"],
            branch_ref=record["branch_ref"],
            expected_head_oid=record["expected_head_oid"],
            bundle_sha256=record["bundle_sha256"],
        )


@dataclass(frozen=True, slots=True)
class VerifiedPublishIntent:
    """The immutable repository target and deterministic commit metadata."""

    repository: VerifiedRepositorySnapshot
    commit: CommitSpec

    def __post_init__(self) -> None:
        if not isinstance(self.repository, VerifiedRepositorySnapshot) or not isinstance(self.commit, CommitSpec):
            raise ProtocolError("admission.publish_intent_invalid", {"where": "components"})

    def to_record(self) -> dict[str, Any]:
        return {
            "commit": _commit_record(self.commit),
            "repository": self.repository.to_record(),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> VerifiedPublishIntent:
        if not isinstance(record, Mapping) or set(record) != {"commit", "repository"}:
            raise ProtocolError("admission.publish_intent_invalid", {"where": "record"})
        commit = record["commit"]
        commit_fields = {
            "author_email",
            "author_name",
            "authored_at",
            "body",
            "committed_at",
            "committer_email",
            "committer_name",
            "subject",
        }
        if not isinstance(commit, Mapping) or set(commit) != commit_fields:
            raise ProtocolError("admission.publish_intent_invalid", {"where": "commit"})
        try:
            value = CommitSpec(**dict(commit))
        except PipelineError as error:
            raise ProtocolError("admission.publish_intent_invalid", {"where": "commit"}) from error
        return cls(
            repository=VerifiedRepositorySnapshot.from_record(record["repository"]),
            commit=value,
        )


def _commit_record(commit: CommitSpec) -> dict[str, Any]:
    return {
        "author_email": commit.author_email,
        "author_name": commit.author_name,
        "authored_at": commit.authored_at,
        "body": commit.body,
        "committed_at": commit.committed_at,
        "committer_email": commit.committer_email,
        "committer_name": commit.committer_name,
        "subject": commit.subject,
    }


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
        config_snapshot_sha256=spec.platform_config_sha256,
        contract_sha256=spec.contract_sha256,
        policy_sha256=spec.policy_sha256,
        expected_executor_sha256=spec.executors.for_stage(stage),
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
        "platform_config_sha256": spec.platform_config_sha256,
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
        "platform_config_sha256": spec.platform_config_sha256,
        "submission_identity": _submission_identity(spec),
        "expected_native_tool_sha256": spec.native_tool_source_sha256,
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


def _require_predecessor(
    predecessor: VerifiedPredecessor,
    *,
    stage: StageId,
    run_id: str,
) -> VerifiedPredecessor:
    if not isinstance(predecessor, VerifiedPredecessor) or predecessor.stage != stage:
        raise ProtocolError("admission.predecessor_invalid", {"required_stage": stage})
    if predecessor.command.run_id != run_id or predecessor.decision.run_id != run_id:
        raise ProtocolError("admission.predecessor_invalid", {"reason": "run-mismatch", "stage": stage})
    return predecessor


def build_capture_command(
    spec: AdmissionSpec,
    freeze: VerifiedPredecessor,
    discover: VerifiedPredecessor,
    discover_result: VerifiedDiscoverResult,
) -> StageCommand:
    """Project Capture from the verified Freeze/Discover lineage and result."""

    freeze = _require_predecessor(freeze, stage="freeze", run_id=spec.run_id)
    discover = _require_predecessor(discover, stage="discover", run_id=spec.run_id)
    if (
        discover_result.predecessor != discover
        or discover.command.request.get("freeze_artifact_id") != freeze.result.output_artifact.artifact_id
        or discover_result.upload_inventory_sha256 != spec.originals.inventory_sha256
        or discover_result.main_assembly != spec.main_assembly
        or discover_result.source_set_artifact != spec.originals.ref
        or discover_result.ruleset_artifact != spec.ruleset.artifact.ref
        or discover_result.configuration_artifact != spec.configuration.ref
        or discover_result.native_tool_source_sha256 != spec.native_tool_source_sha256
        or discover_result.native_tool_record_sha256
        != spec.native_tool_record_sha256
    ):
        raise ProtocolError("admission.lineage_mismatch", {"stage": "capture"})
    effect_key = "native-capture:" + digest(
        {
            "admission_sha256": spec.admission_sha256,
            "discover_decision_id": discover.decision.decision_id,
            "freeze_artifact_id": freeze.result.output_artifact.artifact_id,
        }
    )
    request = {
        "effect_key": effect_key,
        "handoff_sha256": discover_result.selected_files_sha256,
        "upload_sha256": discover_result.upload_inventory_sha256,
        "prepared_sha256": discover_result.prepared_sha256,
        "main_assembly": discover_result.main_assembly,
        "native_tool_source_sha256": discover_result.native_tool_source_sha256,
        "native_tool_record_sha256": discover_result.native_tool_record_sha256,
    }
    return _base_command(
        spec,
        stage="capture",
        request=request,
        input_artifacts=(freeze.result.output_artifact, discover.result.output_artifact),
        predecessor_decision_id=discover.decision.decision_id,
    )


def build_generate_command(spec: AdmissionSpec, capture: VerifiedPredecessor) -> StageCommand:
    """Project Generate from the exact committed Capture output."""

    capture = _require_predecessor(capture, stage="capture", run_id=spec.run_id)
    request = {
        "schema_version": GENERATE_REQUEST_SCHEMA,
        "capture_artifact_id": capture.result.output_artifact.artifact_id,
        "output_contract": GENERATE_OUTPUT_CONTRACT,
    }
    return _base_command(
        spec,
        stage="generate",
        request=request,
        input_artifacts=(capture.result.output_artifact,),
        predecessor_decision_id=capture.decision.decision_id,
    )


def build_verify_command(
    spec: AdmissionSpec,
    generate: VerifiedPredecessor,
    capture: VerifiedPredecessor,
) -> StageCommand:
    """Project Verify from the exact Generate/Capture artifact pair."""

    generate = _require_predecessor(generate, stage="generate", run_id=spec.run_id)
    capture = _require_predecessor(capture, stage="capture", run_id=spec.run_id)
    if generate.command.request.get("capture_artifact_id") != capture.result.output_artifact.artifact_id:
        raise ProtocolError("admission.lineage_mismatch", {"stage": "verify"})
    request = {
        "capture_artifact_id": capture.result.output_artifact.artifact_id,
        "generate_artifact_id": generate.result.output_artifact.artifact_id,
        "output_contract": VERIFY_OUTPUT_CONTRACT,
        "purpose": spec.purpose,
        "schema_version": VERIFY_REQUEST_SCHEMA,
    }
    return _base_command(
        spec,
        stage="verify",
        request=request,
        input_artifacts=(generate.result.output_artifact, capture.result.output_artifact),
        predecessor_decision_id=generate.decision.decision_id,
    )


def build_publish_command(
    spec: AdmissionSpec,
    verify_result: VerifiedVerifyResult,
    publish_intent: VerifiedPublishIntent,
) -> StageCommand:
    """Project Publish from one verified certificate and sealed repository target."""

    if not isinstance(verify_result, VerifiedVerifyResult):
        raise ProtocolError("admission.verify_result_invalid", {"stage": "publish"})
    if not isinstance(publish_intent, VerifiedPublishIntent):
        raise ProtocolError("admission.publish_intent_invalid", {"stage": "publish"})
    if publish_intent != spec.publish_intent:
        raise ProtocolError("admission.publish_intent_invalid", {"where": "admission-binding"})
    verify = _require_predecessor(verify_result.predecessor, stage="verify", run_id=spec.run_id)
    repository = publish_intent.repository
    verified = verify_result.predecessor.result.output_artifact
    input_artifacts = (
        verify_result.generate_artifact,
        verify_result.capture_artifact,
        verified,
        repository.artifact.ref,
    )
    if len({item.artifact_id for item in input_artifacts}) != len(input_artifacts):
        raise ProtocolError("admission.publish_intent_invalid", {"where": "duplicate-artifact"})
    request = {
        "base_ref": repository.base_ref,
        "branch_ref": repository.branch_ref,
        "capture_artifact_id": verify_result.capture_artifact.artifact_id,
        "commit": _commit_record(publish_intent.commit),
        "expected_base_oid": repository.expected_base_oid,
        "expected_head_oid": repository.expected_head_oid,
        "generate_artifact_id": verify_result.generate_artifact.artifact_id,
        "origin_url": repository.origin_url,
        "output_contract": PUBLISH_OUTPUT_CONTRACT,
        "repository_slug": repository.repository,
        "repository_snapshot_artifact_id": repository.artifact.ref.artifact_id,
        "schema_version": PUBLISH_REQUEST_SCHEMA,
        "verified_subject_sha256": verify_result.verified_subject_sha256,
        "verify_artifact_id": verified.artifact_id,
    }
    return _base_command(
        spec,
        stage="publish",
        request=request,
        input_artifacts=input_artifacts,
        predecessor_decision_id=verify.decision.decision_id,
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


def validate_capture_command(
    spec: AdmissionSpec,
    freeze: VerifiedPredecessor,
    discover: VerifiedPredecessor,
    discover_result: VerifiedDiscoverResult,
    command: StageCommand,
) -> StageCommand:
    return _validate(build_capture_command(spec, freeze, discover, discover_result), command)


def validate_generate_command(
    spec: AdmissionSpec,
    capture: VerifiedPredecessor,
    command: StageCommand,
) -> StageCommand:
    return _validate(build_generate_command(spec, capture), command)


def validate_verify_command(
    spec: AdmissionSpec,
    generate: VerifiedPredecessor,
    capture: VerifiedPredecessor,
    command: StageCommand,
) -> StageCommand:
    return _validate(build_verify_command(spec, generate, capture), command)


def validate_publish_command(
    spec: AdmissionSpec,
    verify_result: VerifiedVerifyResult,
    publish_intent: VerifiedPublishIntent,
    command: StageCommand,
) -> StageCommand:
    return _validate(
        build_publish_command(spec, verify_result, publish_intent),
        command,
    )


def validate_stage_command(
    spec: AdmissionSpec,
    command: StageCommand,
    *,
    predecessor: VerifiedPredecessor | None = None,
    supporting_predecessors: tuple[VerifiedPredecessor, ...] = (),
    discover_result: VerifiedDiscoverResult | None = None,
    verify_result: VerifiedVerifyResult | None = None,
    publish_intent: VerifiedPublishIntent | None = None,
) -> StageCommand:
    """The pure Store transaction hook; unsupported stages are explicit."""

    if command.stage == "freeze":
        if (
            predecessor is not None
            or supporting_predecessors
            or discover_result is not None
            or verify_result is not None
            or publish_intent is not None
        ):
            raise ProtocolError("admission.predecessor_invalid", {"stage": "freeze"})
        return validate_freeze_command(spec, command)
    if command.stage == "discover":
        if (
            predecessor is None
            or supporting_predecessors
            or discover_result is not None
            or verify_result is not None
            or publish_intent is not None
        ):
            raise ProtocolError("admission.predecessor_required", {"stage": "discover"})
        return validate_discover_command(spec, predecessor, command)
    if command.stage == "capture":
        if (
            predecessor is None
            or discover_result is None
            or len(supporting_predecessors) != 1
            or verify_result is not None
            or publish_intent is not None
        ):
            raise ProtocolError("admission.predecessor_required", {"stage": "capture"})
        return validate_capture_command(
            spec,
            supporting_predecessors[0],
            predecessor,
            discover_result,
            command,
        )
    if command.stage == "generate":
        if (
            predecessor is None
            or supporting_predecessors
            or discover_result is not None
            or verify_result is not None
            or publish_intent is not None
        ):
            raise ProtocolError("admission.predecessor_required", {"stage": "generate"})
        return validate_generate_command(spec, predecessor, command)
    if command.stage == "verify":
        if (
            predecessor is None
            or len(supporting_predecessors) != 1
            or discover_result is not None
            or verify_result is not None
            or publish_intent is not None
        ):
            raise ProtocolError("admission.predecessor_required", {"stage": "verify"})
        return validate_verify_command(spec, predecessor, supporting_predecessors[0], command)
    if command.stage == "publish":
        if (
            predecessor is None
            or verify_result is None
            or predecessor != verify_result.predecessor
            or supporting_predecessors
            or discover_result is not None
            or publish_intent is None
        ):
            raise ProtocolError("admission.predecessor_required", {"stage": "publish"})
        return validate_publish_command(spec, verify_result, publish_intent, command)
    raise ProtocolError("admission.stage_not_supported", {"stage": command.stage})


def validate_store_command(
    spec: AdmissionSpec,
    admission: RunAdmission,
    command: StageCommand,
    *,
    predecessor: VerifiedPredecessor | None = None,
    supporting_predecessors: tuple[VerifiedPredecessor, ...] = (),
    discover_result: VerifiedDiscoverResult | None = None,
    verify_result: VerifiedVerifyResult | None = None,
    publish_intent: VerifiedPublishIntent | None = None,
) -> StageCommand:
    """Pure transaction hook for the common FactStore integration."""

    validate_run_admission(spec, admission)
    return validate_stage_command(
        spec,
        command,
        predecessor=predecessor,
        supporting_predecessors=supporting_predecessors,
        discover_result=discover_result,
        verify_result=verify_result,
        publish_intent=publish_intent,
    )


__all__ = [
    "ADMISSION_SPEC_SCHEMA",
    "CAPTURE_ARTIFACT_KIND",
    "GENERATE_ARTIFACT_KIND",
    "GENERATE_OUTPUT_CONTRACT",
    "GENERATE_REQUEST_SCHEMA",
    "SUPPORTED_PROJECTION_STAGES",
    "VERIFY_OUTPUT_CONTRACT",
    "VERIFY_REQUEST_SCHEMA",
    "AdmissionSpec",
    "ArtifactMember",
    "StageExecutors",
    "VerifiedArtifact",
    "VerifiedDiscoverResult",
    "VerifiedPredecessor",
    "VerifiedPublishIntent",
    "VerifiedRepositorySnapshot",
    "VerifiedRuleset",
    "VerifiedVerifyResult",
    "build_capture_command",
    "build_discover_command",
    "build_freeze_command",
    "build_generate_command",
    "build_publish_command",
    "build_run_admission",
    "build_verify_command",
    "validate_capture_command",
    "validate_discover_command",
    "validate_freeze_command",
    "validate_generate_command",
    "validate_publish_command",
    "validate_run_admission",
    "validate_stage_command",
    "validate_store_command",
    "validate_verify_command",
]
