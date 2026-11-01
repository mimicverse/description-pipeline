"""Immutable source, execution bundle and runtime identities for assessment."""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from ..artifacts import ArtifactStore
from ..io import PipelineError, canonical, digest
from ..pipeline.contracts import ArtifactRef

SOURCE_SCHEMA = "description.assessment-source/v1"
REQUEST_SCHEMA = "description.dynamics-assessment-request/v1"
BUNDLE_SCHEMA = "description.dynamics-execution-bundle/v1"
RUNTIME_SCHEMA = "description.assessment-runtime/v1"
RESULT_SCHEMA = "description.dynamics-assessment-result/v1"
DAG_ID = "upper_body_dynamics"
POOL_ID = "dynamics_assessment"
BUNDLE_MEMBER = "assessment.json"
TERMINAL_STATES = frozenset({"completed", "unsupported", "failed", "cancelled", "timeout"})
ACTIVE_STATES = frozenset({"registered", "dispatching", "queued", "running", "verifying", "sealing", "unknown"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class AssessmentError(PipelineError):
    """An assessment input or immutable association is invalid."""


class AssessmentConflict(AssessmentError):
    """An existing identity or execution owner conflicts with this operation."""


class AssessmentDisabled(AssessmentError):
    """The optional assessment feature is disabled."""


class SourceNotReady(AssessmentError):
    """An authorized source has no complete engineering artifact/certificate pair yet."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssessmentError(message)


def project_attempt(record: dict) -> dict:
    """Project lease uncertainty without mutating the row or settling resources."""
    result = copy.deepcopy(record)
    require(result["state"] in ACTIVE_STATES | TERMINAL_STATES, "Assessment attempt state is invalid")
    if result["state"] in {"running", "verifying", "sealing"} and result["lease_expired"]:
        # A read projects uncertainty; it does not settle resources or grant takeover.
        result["detail"] = {**result["detail"], "reason": "owner_lease_expired", "previous_phase": result["state"]}
        result["state"] = "unknown"
    return result


def dispatch_was_accepted(attempt: dict) -> bool:
    """Acceptance is a monotonic attempt fact, independent of projected state."""
    detail = attempt.get("detail")
    require(isinstance(detail, dict), "Assessment attempt detail is invalid")
    if "dispatch_accepted" not in detail:
        return False
    require(detail["dispatch_accepted"] is True, "Assessment acceptance fact is invalid")
    return True


def text(value: Any, name: str) -> str:
    require(isinstance(value, str) and bool(value.strip()) and "\x00" not in value, f"{name} is invalid")
    return value


def sha(value: Any, name: str) -> str:
    require(isinstance(value, str) and _SHA.fullmatch(value) is not None, f"{name} must be a SHA256 digest")
    return value


def relative(value: Any, name: str) -> str:
    value = text(value, name)
    path = PurePosixPath(value)
    require(
        not path.is_absolute()
        and "\\" not in value
        and ":" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/")),
        f"{name} must be a confined relative path",
    )
    return value


def finite_json(value: Any) -> None:
    if isinstance(value, float):
        require(math.isfinite(value), "Non-finite JSON is not permitted")
    elif isinstance(value, dict):
        require(all(isinstance(key, str) for key in value), "JSON object keys must be strings")
        for item in value.values():
            finite_json(item)
    elif isinstance(value, list):
        for item in value:
            finite_json(item)
    else:
        require(value is None or isinstance(value, (str, int, bool)), "Value is not JSON data")


def object_record(value: Any, fields: set[str], name: str) -> dict:
    require(isinstance(value, dict) and set(value) == fields, f"{name} has missing or unknown fields")
    finite_json(value)
    return value


def parse_json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON keys are not permitted")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=pairs)
    except (UnicodeError, ValueError) as error:
        raise AssessmentError(f"Invalid assessment JSON: {error}") from error
    require(isinstance(value, dict), "Assessment JSON must be an object")
    finite_json(value)
    return value


@dataclass(frozen=True)
class MemberRef:
    """An exact member of an existing immutable ArtifactRef."""

    artifact: ArtifactRef
    path: str
    bytes: int
    sha256: str

    def __post_init__(self):
        require(isinstance(self.artifact, ArtifactRef), "Member artifact must be an ArtifactRef")
        relative(self.path, "Member path")
        require(type(self.bytes) is int and self.bytes >= 0, "Member byte count is invalid")
        sha(self.sha256, "Member digest")

    def to_record(self) -> dict:
        return {"artifact": self.artifact.to_record(), "path": self.path, "bytes": self.bytes, "sha256": self.sha256}

    @classmethod
    def from_record(cls, value: Any) -> MemberRef:
        value = object_record(value, {"artifact", "path", "bytes", "sha256"}, "MemberRef")
        return cls(ArtifactRef.from_record(value["artifact"]), value["path"], value["bytes"], value["sha256"])

    def read(self, artifacts: ArtifactStore) -> bytes:
        import hashlib

        data, member, _manifest = artifacts.read_metadata_member(self.artifact, self.path, max_bytes=self.bytes)
        require(
            member["bytes"] == self.bytes
            and member["sha256"] == self.sha256
            and len(data) == self.bytes
            and hashlib.sha256(data).hexdigest() == self.sha256,
            "Source member differs from the registered source identity",
        )
        return data


class _ImmutableRecord:
    """Keep the validated value as bytes; callers receive independent records."""

    __slots__ = ("_canonical",)

    def _freeze(self, value: dict) -> None:
        self._canonical = canonical(value)

    @property
    def record(self) -> dict:
        return parse_json(self._canonical)


class SourceBinding(_ImmutableRecord):
    """Source selected and certificate-bound by the authenticated portal adapter.

    The adapter must use the existing engineering certificate validators before
    constructing this record. This class verifies immutable bytes, not physics.
    """

    def __init__(self, record: dict):
        value = copy.deepcopy(record)
        object_record(
            value,
            {"schema", "run_id", "dag_run_id", "subject_sha256", "urdf", "mjcf", "certificates"},
            "Assessment source",
        )
        require(value["schema"] == SOURCE_SCHEMA, "Assessment source schema is unsupported")
        text(value["run_id"], "Source run")
        text(value["dag_run_id"], "Source DagRun")
        sha(value["subject_sha256"], "Source subject")
        for key in ("urdf", "mjcf"):
            MemberRef.from_record(value[key])
        certificates = object_record(value["certificates"], {"urdf", "mjcf"}, "Source certificates")
        for item in certificates.values():
            MemberRef.from_record(item)
        self._freeze(value)

    def to_record(self) -> dict:
        return copy.deepcopy(self.record)

    @property
    def sha256(self) -> str:
        return digest(self.record)

    def verify(self, artifacts: ArtifactStore) -> None:
        members = [self.record["urdf"], self.record["mjcf"], *self.record["certificates"].values()]
        verified = set()
        for member_record in members:
            member = MemberRef.from_record(member_record)
            if member.artifact.artifact_id not in verified:
                artifacts.verify(member.artifact)
                verified.add(member.artifact.artifact_id)
            member.read(artifacts)


class RuntimePin(_ImmutableRecord):
    """One approved installed Teleop distribution and dependency closure."""

    def __init__(self, record: dict):
        value = copy.deepcopy(record)
        object_record(
            value, {"schema", "source_commit", "wheel_sha256", "files", "dependency_versions"}, "Assessment runtime"
        )
        require(value["schema"] == RUNTIME_SCHEMA, "Assessment runtime schema is unsupported")
        require(
            isinstance(value["source_commit"], str) and _COMMIT.fullmatch(value["source_commit"]),
            "Runtime source commit is invalid",
        )
        sha(value["wheel_sha256"], "Runtime wheel")
        require(isinstance(value["files"], dict) and bool(value["files"]), "Runtime file closure is required")
        for name, value_digest in value["files"].items():
            relative(name, "Runtime file")
            require(name.startswith("teleop/"), "Runtime file must belong to Teleop")
            sha(value_digest, "Runtime file digest")
        versions = value["dependency_versions"]
        require(
            isinstance(versions, dict) and {"robot-teleop", "mujoco", "numpy"} <= set(versions),
            "Runtime dependency versions are incomplete",
        )
        for name, version in versions.items():
            text(name, "Dependency name")
            text(version, "Dependency version")
        self._freeze(value)

    def to_record(self) -> dict:
        return copy.deepcopy(self.record)

    @property
    def sha256(self) -> str:
        return digest(self.record)


class AssessmentRequest(_ImmutableRecord):
    """A source-authorized immutable request, never a required StageCommand."""

    def __init__(self, record: dict):
        value = copy.deepcopy(record)
        object_record(
            value, {"schema", "principal", "source", "bundle", "runtime", "executor_sha256"}, "Assessment request"
        )
        require(value["schema"] == REQUEST_SCHEMA, "Assessment request schema is unsupported")
        text(value["principal"], "Assessment principal")
        SourceBinding(value["source"])
        RuntimePin(value["runtime"])
        sha(value["executor_sha256"], "Assessment executor")
        if value["bundle"] is not None:
            bundle = ArtifactRef.from_record(value["bundle"])
            require(
                bundle.kind == "dynamics_execution_bundle" and bundle.producer_stage is None,
                "Execution bundle must be a separately sealed auxiliary artifact",
            )
        self._freeze(value)

    def to_record(self) -> dict:
        return copy.deepcopy(self.record)

    @property
    def sha256(self) -> str:
        return digest(self.record)

    @property
    def source(self) -> SourceBinding:
        return SourceBinding(self.record["source"])

    @property
    def runtime(self) -> RuntimePin:
        return RuntimePin(self.record["runtime"])

    @property
    def bundle(self) -> ArtifactRef | None:
        value = self.record["bundle"]
        return ArtifactRef.from_record(value) if value is not None else None


def dispatch_conf(request_id: str, attempt_id: str, request: AssessmentRequest) -> dict:
    return {
        "request_id": text(request_id, "Assessment request id"),
        "attempt_id": text(attempt_id, "Assessment attempt id"),
        "request_sha256": request.sha256,
    }


def auxiliary_dag_run_id(attempt_id: str) -> str:
    sha(attempt_id, "Assessment attempt id")
    return "assessment-" + attempt_id
