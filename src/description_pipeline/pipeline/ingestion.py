"""Production admission ingestion and immutable run-spec construction."""

from __future__ import annotations

import copy
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ..artifacts import ArtifactStore
from ..interface_records import (
    REQUEST_PURPOSES,
    check_policy_digest,
    config_snapshot_digest,
    validate_config_snapshot,
)
from ..io import PipelineError, artifact_path_parts, canonical, digest, file_digest, parse_data, write_json
from ..request_evidence import normalize_change_summary
from ..repository.publish import (
    CommitSpec,
    materialize_repository_snapshot,
    seal_repository_snapshot,
)
from ..repository.review_branch import review_branch
from ..runtime import tool_record
from ..schemas import NATIVE_TOOL_REQUIREMENT_KIND, TOOL_SCHEMA
from ..sources.solidworks.handoff import describe_handoff
from ..stages import CONTRACT_SHA256
from .admission import (
    ADMISSION_SPEC_SCHEMA,
    AdmissionSpec,
    StageExecutors,
    VerifiedArtifact,
    VerifiedPublishIntent,
    VerifiedRepositorySnapshot,
    VerifiedRuleset,
    build_run_admission,
)
from .contracts import ProtocolError, RunAdmission
from .discover import CONFIGURATION_KIND, RULESET_KIND, SOURCE_SET_KIND
from .freeze import RULESET_CATALOG_SCHEMA
from .publish_contract import REPOSITORY_SNAPSHOT_KIND, REPOSITORY_SNAPSHOT_METADATA
from ..orchestration.feishu_oauth import valid_actor_principal

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TARGET = re.compile(r"\A[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_REF = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RECEIPT_FIELDS = frozenset(
    {
        "folder",
        "files",
        "bytes",
        "handoff_sha256",
        "main_assembly",
        "target_hardware",
        "change_summary",
        "purpose",
    }
)
_REQUEST_FIELDS = frozenset(
    {
        "run_id",
        "request_id",
        "upload_dir",
        "receipt",
        "main_assembly",
        "target_hardware",
        "purpose",
        "change_summary",
        "principal",
        "parent_run_id",
        "from_stage",
    }
)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or _CONTROL.search(value):
        raise ProtocolError("admission_ingestion.invalid", {"where": where})
    return value


def _multiline(value: object, where: str) -> str:
    normalized = (
        normalize_change_summary(value)
        if isinstance(value, str)
        else None
    )
    if (
        normalized is None
        or not normalized.strip()
        or any(
            (ord(character) < 32 and character != "\n")
            or ord(character) == 127
            for character in normalized
        )
    ):
        raise ProtocolError("admission_ingestion.invalid", {"where": where})
    return normalized


def _identifier(value: object, where: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ProtocolError("admission_ingestion.invalid", {"where": where})
    return value


def _sha256(value: object, where: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProtocolError("admission_ingestion.invalid", {"where": where})
    return value


def _real_directory(value: object, where: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ProtocolError("admission_ingestion.invalid", {"where": where})
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or path.is_junction() or not path.is_dir():
        raise ProtocolError("admission_ingestion.invalid", {"where": where})
    resolved = path.resolve()
    if any(parent.is_symlink() for parent in resolved.parents):
        raise ProtocolError("admission_ingestion.invalid", {"where": where, "reason": "symlink-parent"})
    return resolved


def _canonical_mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError("admission_ingestion.invalid", {"where": where})
    record = copy.deepcopy(dict(value))
    try:
        canonical(record)
    except (TypeError, ValueError) as error:
        raise ProtocolError(
            "admission_ingestion.invalid",
            {"where": where, "reason": "non-canonical-json"},
        ) from error
    return record


def _confined_path(root: Path, relative: str, where: str) -> Path:
    try:
        parts = artifact_path_parts(relative)
    except PipelineError as error:
        raise ProtocolError("admission_ingestion.invalid", {"where": where}) from error
    if not parts or "/".join(parts) != relative:
        raise ProtocolError("admission_ingestion.invalid", {"where": where})
    target = root.joinpath(*parts)
    if target.is_symlink() or target.is_junction():
        raise ProtocolError("admission_ingestion.invalid", {"where": where, "reason": "link"})
    return target


def _git(repository: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        capture_output=True,
    )
    if check and completed.returncode != 0:
        raise ProtocolError(
            "admission_ingestion.git_failed",
            {
                "args": list(args),
                "stderr": completed.stderr.decode("utf-8", "replace")[:1024],
            },
        )
    return completed.stdout.decode("utf-8").strip()


def _git_optional(repository: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.decode("utf-8").strip()
    return value or None


@dataclass(frozen=True, slots=True)
class ReceivedUploadReceipt:
    """The immutable receipt returned by the receiving upload boundary."""

    folder: str
    files: int
    bytes: int
    handoff_sha256: str
    main_assembly: str
    target_hardware: str
    change_summary: str
    purpose: str

    def __post_init__(self) -> None:
        _text(self.folder, "receipt.folder")
        if "/" in self.folder or "\\" in self.folder:
            raise ProtocolError("admission_ingestion.invalid", {"where": "receipt.folder"})
        if type(self.files) is not int or self.files <= 0:
            raise ProtocolError("admission_ingestion.invalid", {"where": "receipt.files"})
        if type(self.bytes) is not int or self.bytes <= 0:
            raise ProtocolError("admission_ingestion.invalid", {"where": "receipt.bytes"})
        _sha256(self.handoff_sha256, "receipt.handoff_sha256")
        _text(self.main_assembly, "receipt.main_assembly")
        _text(self.target_hardware, "receipt.target_hardware")
        object.__setattr__(
            self,
            "change_summary",
            _multiline(self.change_summary, "receipt.change_summary"),
        )
        _text(self.purpose, "receipt.purpose")

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ReceivedUploadReceipt:
        if not isinstance(record, Mapping) or set(record) != _RECEIPT_FIELDS:
            raise ProtocolError(
                "admission_ingestion.invalid",
                {"where": "receipt", "reason": "fields"},
            )
        return cls(**dict(record))

    def to_record(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "files": self.files,
            "bytes": self.bytes,
            "handoff_sha256": self.handoff_sha256,
            "main_assembly": self.main_assembly,
            "target_hardware": self.target_hardware,
            "change_summary": self.change_summary,
            "purpose": self.purpose,
        }


@dataclass(frozen=True, slots=True)
class ProductionAdmissionRequest:
    """All non-secret input required to build one run admission."""

    run_id: str
    request_id: str
    upload_dir: Path
    receipt: ReceivedUploadReceipt
    main_assembly: str
    target_hardware: str
    purpose: str
    change_summary: str
    principal: str
    parent_run_id: str | None = None
    from_stage: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _identifier(self.request_id, "request_id")
        if not isinstance(self.upload_dir, Path):
            raise ProtocolError("admission_ingestion.invalid", {"where": "upload_dir"})
        if not isinstance(self.receipt, ReceivedUploadReceipt):
            raise ProtocolError("admission_ingestion.invalid", {"where": "receipt"})
        _text(self.main_assembly, "main_assembly")
        if not _TARGET.fullmatch(self.target_hardware):
            raise ProtocolError("admission_ingestion.invalid", {"where": "target_hardware"})
        if self.purpose not in REQUEST_PURPOSES:
            raise ProtocolError("admission_ingestion.invalid", {"where": "purpose"})
        summary = _multiline(self.change_summary, "change_summary")
        object.__setattr__(self, "change_summary", summary)
        _text(self.principal, "principal")
        if not valid_actor_principal(self.principal):
            raise ProtocolError(
                "admission_ingestion.invalid",
                {"where": "principal", "reason": "feishu-principal"},
            )
        if self.parent_run_id is not None:
            _identifier(self.parent_run_id, "parent_run_id")
            if self.parent_run_id == self.run_id:
                raise ProtocolError(
                    "admission_ingestion.invalid",
                    {"where": "parent_run_id", "reason": "self-reference"},
                )
        if self.from_stage not in {
            None,
            "freeze",
            "discover",
            "capture",
            "generate",
            "verify",
            "publish",
        }:
            raise ProtocolError("admission_ingestion.invalid", {"where": "from_stage"})
        if (
            self.main_assembly != self.receipt.main_assembly
            or self.target_hardware != self.receipt.target_hardware
            or self.purpose != self.receipt.purpose
            or summary != self.receipt.change_summary
        ):
            raise ProtocolError(
                "admission_ingestion.invalid",
                {"where": "receipt", "reason": "request-mismatch"},
            )

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ProductionAdmissionRequest:
        if not isinstance(record, Mapping) or set(record) != _REQUEST_FIELDS:
            raise ProtocolError(
                "admission_ingestion.invalid",
                {"where": "request", "reason": "fields"},
            )
        receipt = ReceivedUploadReceipt.from_record(record["receipt"])
        upload_dir = record["upload_dir"]
        if not isinstance(upload_dir, (str, Path)):
            raise ProtocolError("admission_ingestion.invalid", {"where": "upload_dir"})
        return cls(
            run_id=record["run_id"],
            request_id=record["request_id"],
            upload_dir=Path(upload_dir),
            receipt=receipt,
            main_assembly=record["main_assembly"],
            target_hardware=record["target_hardware"],
            purpose=record["purpose"],
            change_summary=record["change_summary"],
            principal=record["principal"],
            parent_run_id=record["parent_run_id"],
            from_stage=record["from_stage"],
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "upload_dir": str(self.upload_dir),
            "receipt": self.receipt.to_record(),
            "main_assembly": self.main_assembly,
            "target_hardware": self.target_hardware,
            "purpose": self.purpose,
            "change_summary": self.change_summary,
            "principal": self.principal,
            "parent_run_id": self.parent_run_id,
            "from_stage": self.from_stage,
        }


class AdmissionApplication(Protocol):
    """The one atomic admission write consumed by the loader."""

    def admit(self, spec: AdmissionSpec) -> RunAdmission: ...


@dataclass(frozen=True, slots=True)
class PreparedAdmission:
    """One fully sealed admission waiting for its database transaction."""

    spec: AdmissionSpec
    run_admission: RunAdmission

    def register(self, application: AdmissionApplication) -> RunAdmission:
        if not callable(getattr(application, "admit", None)):
            raise TypeError("application must provide admit(spec)")
        actual = application.admit(self.spec)
        if actual != self.run_admission:
            raise ProtocolError(
                "admission_ingestion.run_mismatch",
                {"run_id": self.spec.run_id},
            )
        return actual


class ProductionAdmissionFactory:
    """Seal real production inputs and produce the one AdmissionSpec fact."""

    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        platform_snapshot: Path,
        native_tool_provider: Callable[[], Mapping[str, Any]],
        work_root: Path,
        clock: Callable[[], datetime] | None = None,
        portable_tool_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        if not isinstance(artifacts, ArtifactStore):
            raise TypeError("artifacts must be an ArtifactStore")
        if not callable(native_tool_provider):
            raise TypeError("native_tool_provider must be callable")
        self.artifacts = artifacts
        self.platform_snapshot = Path(platform_snapshot)
        self.native_tool_provider = native_tool_provider
        self.work_root = _real_directory(work_root, "work_root")
        artifact_root = self.artifacts.root.resolve()
        if (
            self.work_root.is_relative_to(artifact_root)
            or artifact_root.is_relative_to(self.work_root)
        ):
            raise ProtocolError(
                "admission_ingestion.invalid",
                {"where": "work_root", "reason": "artifact-root-overlap"},
            )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.portable_tool_provider = portable_tool_provider or (
            lambda: tool_record(role="portable")
        )

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ProtocolError("admission_ingestion.invalid", {"where": "clock"})
        return value.astimezone(UTC)

    def _load_platform(self) -> tuple[dict[str, Any], str, bytes]:
        if (
            not self.platform_snapshot.is_file()
            or self.platform_snapshot.is_symlink()
            or any(parent.is_symlink() for parent in self.platform_snapshot.resolve().parents)
        ):
            raise ProtocolError("admission_ingestion.config_missing")
        raw = self.platform_snapshot.read_bytes()
        try:
            snapshot = parse_data(raw, self.platform_snapshot)
            validated = validate_config_snapshot(snapshot)
        except PipelineError as error:
            raise ProtocolError(
                "admission_ingestion.config_invalid",
                {"reason": str(error)[:1024]},
            ) from error
        return validated["resolved"], config_snapshot_digest(validated), raw

    def _installed(self) -> tuple[StageExecutors, str]:
        portable = _canonical_mapping(
            self.portable_tool_provider(),
            "portable_tool",
        )
        if portable.get("schema_version") != TOOL_SCHEMA:
            raise ProtocolError("admission_ingestion.installation_invalid")
        source = _sha256(portable.get("source_sha256"), "portable_tool.source_sha256")
        executors = StageExecutors(
            freeze=source,
            discover=source,
            capture=source,
            generate=source,
            verify=source,
            publish=source,
        )
        ingestion = digest(
            {
                "kind": "admission_ingestion",
                "portable_tool_sha256": digest(portable),
            }
        )
        return executors, ingestion

    def _native_requirement(
        self,
        ingestion_tool_sha256: str,
    ) -> tuple[VerifiedArtifact, str, str]:
        tool = _canonical_mapping(
            self.native_tool_provider(),
            "native_tool",
        )
        if tool.get("schema_version") != TOOL_SCHEMA:
            raise ProtocolError("admission_ingestion.native_tool_invalid")
        source = _sha256(tool.get("source_sha256"), "native_tool.source_sha256")
        release = tool.get("release")
        if isinstance(release, Mapping) and release.get("source_sha256") not in {
            None,
            source,
        }:
            raise ProtocolError(
                "admission_ingestion.native_tool_invalid",
                {"reason": "release-source-mismatch"},
            )
        record_sha256 = digest(tool)
        with tempfile.TemporaryDirectory(
            prefix="admission-native-tool-",
            dir=self.work_root,
        ) as directory:
            root = Path(directory)
            (root / "tool.json").write_bytes(canonical(tool))
            artifact = self.artifacts.seal_tree(
                root,
                kind=NATIVE_TOOL_REQUIREMENT_KIND,
                producer_run_id=None,
                producer_stage=None,
                producer_tool_sha256=ingestion_tool_sha256,
            )
        requirement = self._verified(artifact)
        if requirement.inventory != {"tool.json": record_sha256}:
            raise ProtocolError(
                "admission_ingestion.native_tool_invalid",
                {"reason": "artifact-identity"},
            )
        return requirement, source, record_sha256

    def _verified(self, artifact) -> VerifiedArtifact:
        return VerifiedArtifact.from_store_record(
            artifact,
            self.artifacts.verify(artifact),
        )

    def _seal_upload(
        self,
        request: ProductionAdmissionRequest,
        resolved: Mapping[str, Any],
        ingestion_tool_sha256: str,
    ) -> VerifiedArtifact:
        handoff_root = _real_directory(
            resolved["storage"]["handoff_root"],
            "resolved.storage.handoff_root",
        )
        upload_dir = _real_directory(request.upload_dir, "upload_dir")
        if not upload_dir.is_relative_to(handoff_root):
            raise ProtocolError(
                "admission_ingestion.upload_outside_handoff_root",
                {"upload_dir": str(upload_dir)},
            )
        source = _confined_path(upload_dir, request.receipt.folder, "receipt.folder")
        source = _real_directory(source, "receipt.folder")
        identity = describe_handoff(source)
        files = dict(identity["files"])
        total = sum(
            _confined_path(source, name, f"files.{name}").stat().st_size
            for name in files
        )
        if (
            len(files) != request.receipt.files
            or total != request.receipt.bytes
            or identity["handoff_sha256"] != request.receipt.handoff_sha256
            or request.main_assembly not in files
        ):
            raise ProtocolError(
                "admission_ingestion.upload_mismatch",
                {"folder": request.receipt.folder},
            )
        artifact = self.artifacts.seal_tree(
            source,
            kind=SOURCE_SET_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=ingestion_tool_sha256,
        )
        verified = self._verified(artifact)
        if verified.inventory != files:
            raise ProtocolError("admission_ingestion.upload_artifact_mismatch")
        return verified

    def _seal_ruleset(
        self,
        resolved: Mapping[str, Any],
        ingestion_tool_sha256: str,
    ) -> VerifiedRuleset:
        roots = resolved["specifications"]["record_roots"]
        if not isinstance(roots, list) or not roots:
            raise ProtocolError("admission_ingestion.ruleset_empty")
        with tempfile.TemporaryDirectory(prefix="admission-rules-", dir=self.work_root) as directory:
            target = Path(directory)
            records: dict[str, str] = {}
            seen: set[str] = set()
            for index, root_value in enumerate(roots):
                root = _real_directory(root_value, f"record_roots[{index}]")
                for source in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
                    if source.is_symlink() or source.is_junction() or not stat.S_ISREG(source.stat().st_mode):
                        if source.is_dir() and not source.is_symlink() and not source.is_junction():
                            continue
                        raise ProtocolError(
                            "admission_ingestion.ruleset_invalid",
                            {"where": str(source)},
                        )
                    relative = source.relative_to(root).as_posix()
                    if "/".join(artifact_path_parts(relative)) != relative:
                        raise ProtocolError(
                            "admission_ingestion.ruleset_invalid",
                            {"where": relative},
                        )
                    name = f"root-{index:03d}/{relative}"
                    folded = name.casefold()
                    if folded in seen:
                        raise ProtocolError(
                            "admission_ingestion.ruleset_invalid",
                            {"reason": "case-collision", "path": name},
                        )
                    seen.add(folded)
                    destination = _confined_path(target, name, "ruleset.record")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    before = file_digest(source)
                    shutil.copyfile(source, destination)
                    copied = file_digest(destination)
                    if copied != before or file_digest(source) != before:
                        raise ProtocolError(
                            "admission_ingestion.ruleset_invalid",
                            {"reason": "changed", "path": name},
                        )
                    records[name] = copied
            if not records:
                raise ProtocolError("admission_ingestion.ruleset_empty")
            version = "records-" + digest(records)
            write_json(
                target / "catalog.json",
                {
                    "schema_version": RULESET_CATALOG_SCHEMA,
                    "version": version,
                    "records": records,
                },
            )
            artifact = self.artifacts.seal_tree(
                target,
                kind=RULESET_KIND,
                producer_run_id=None,
                producer_stage=None,
                producer_tool_sha256=ingestion_tool_sha256,
            )
        verified = self._verified(artifact)
        with tempfile.TemporaryDirectory(prefix="admission-ruleset-read-", dir=self.work_root) as directory:
            materialized = self.artifacts.materialize(
                artifact,
                Path(directory) / "artifact",
            )
            return VerifiedRuleset.from_catalog_bytes(
                verified,
                (materialized / "catalog.json").read_bytes(),
            )

    def _seal_configuration(
        self,
        resolved: Mapping[str, Any],
        snapshot_bytes: bytes,
        ingestion_tool_sha256: str,
    ) -> VerifiedArtifact:
        artifact_root = _real_directory(
            resolved["storage"]["artifact_root"],
            "resolved.storage.artifact_root",
        )
        if artifact_root != self.artifacts.root.resolve():
            raise ProtocolError(
                "admission_ingestion.config_mismatch",
                {"reason": "artifact-root", "configured": str(artifact_root)},
            )
        with tempfile.TemporaryDirectory(prefix="admission-config-", dir=self.work_root) as directory:
            target = Path(directory)
            (target / "config-snapshot.json").write_bytes(snapshot_bytes)
            artifact = self.artifacts.seal_tree(
                target,
                kind=CONFIGURATION_KIND,
                producer_run_id=None,
                producer_stage=None,
                producer_tool_sha256=ingestion_tool_sha256,
            )
        return self._verified(artifact)

    def _publication(
        self,
        request: ProductionAdmissionRequest,
        resolved: Mapping[str, Any],
        created_at: datetime,
        ingestion_tool_sha256: str,
    ) -> VerifiedPublishIntent:
        target = resolved["targets"].get(request.target_hardware)
        if not isinstance(target, Mapping):
            raise ProtocolError(
                "admission_ingestion.target_unknown",
                {"target_hardware": request.target_hardware},
            )
        repository_slug = _text(target["repository_slug"], "target.repository_slug")
        base_ref = _text(target["base"], "target.base")
        repositories = resolved["derived"]["repositories"]
        checkout = _real_directory(
            repositories.get(repository_slug),
            f"resolved.derived.repositories[{repository_slug}]",
        )
        origin_url = _text(
            _git(checkout, "remote", "get-url", "origin"),
            "repository.origin",
        )
        base_oid = _git_optional(
            checkout,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{base_ref}^{{commit}}",
        ) or _git_optional(
            checkout,
            "rev-parse",
            "--verify",
            f"refs/heads/{base_ref}^{{commit}}",
        )
        if base_oid is None:
            raise ProtocolError(
                "admission_ingestion.repository_base_missing",
                {"base_ref": base_ref},
            )
        branch_ref = review_branch(request.target_hardware, request.purpose)
        if _REF.fullmatch(branch_ref) is None:
            raise ProtocolError(
                "admission_ingestion.repository_branch_invalid",
                {"branch_ref": branch_ref},
            )
        head_oid = _git_optional(
            checkout,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{branch_ref}^{{commit}}",
        )
        actor_name = _git_optional(
            checkout,
            "config",
            "--local",
            "--get",
            "user.name",
        )
        actor_email = _git_optional(
            checkout,
            "config",
            "--local",
            "--get",
            "user.email",
        )
        if not actor_name or not actor_email:
            raise ProtocolError(
                "admission_ingestion.publication_actor_missing",
                {"repository": repository_slug},
            )
        if _CONTROL.search(actor_name) or _CONTROL.search(actor_email) or "@" not in actor_email:
            raise ProtocolError(
                "admission_ingestion.publication_actor_invalid",
                {"repository": repository_slug},
            )
        with tempfile.TemporaryDirectory(prefix="admission-repository-", dir=self.work_root) as directory:
            snapshot_root = Path(directory) / "payload"
            seal_repository_snapshot(
                checkout,
                snapshot_root,
                repository_slug=repository_slug,
                origin_url=origin_url,
                base_ref=base_ref,
                base_oid=base_oid,
                branch_ref=branch_ref,
                head_oid=head_oid,
            )
            artifact = self.artifacts.seal_tree(
                snapshot_root,
                kind=REPOSITORY_SNAPSHOT_KIND,
                producer_run_id=None,
                producer_stage=None,
                producer_tool_sha256=ingestion_tool_sha256,
            )
        verified = self._verified(artifact)
        with tempfile.TemporaryDirectory(prefix="admission-repository-read-", dir=self.work_root) as directory:
            materialized = self.artifacts.materialize(
                artifact,
                Path(directory) / "artifact",
            )
            repository = VerifiedRepositorySnapshot.from_metadata_bytes(
                verified,
                (materialized / REPOSITORY_SNAPSHOT_METADATA).read_bytes(),
            )
            materialize_repository_snapshot(
                materialized,
                Path(directory) / "verify-clone",
                expected_repository=repository.repository,
                expected_origin_url=repository.origin_url,
                expected_base_ref=repository.base_ref,
                expected_base_oid=repository.expected_base_oid,
                expected_branch_ref=repository.branch_ref,
                expected_head_oid=repository.expected_head_oid,
            )
        timestamp = created_at.isoformat()
        commit = CommitSpec(
            author_name=actor_name,
            author_email=actor_email,
            authored_at=timestamp,
            committer_name=actor_name,
            committer_email=actor_email,
            committed_at=timestamp,
            subject=f"feat({request.target_hardware}): publish URDF delivery",
            body=request.change_summary,
        )
        return VerifiedPublishIntent(repository=repository, commit=commit)

    def prepare(self, request: ProductionAdmissionRequest) -> PreparedAdmission:
        if not isinstance(request, ProductionAdmissionRequest):
            raise TypeError("request must be a ProductionAdmissionRequest")
        upload_root = request.upload_dir.resolve()
        if (
            self.work_root.is_relative_to(upload_root)
            or upload_root.is_relative_to(self.work_root)
        ):
            raise ProtocolError(
                "admission_ingestion.invalid",
                {"where": "work_root", "reason": "upload-root-overlap"},
            )
        resolved, platform_config_sha256, snapshot_bytes = self._load_platform()
        executors, ingestion_tool_sha256 = self._installed()
        native_tool_requirement, native_source_sha256, native_record_sha256 = (
            self._native_requirement(ingestion_tool_sha256)
        )
        created_at = self._now()
        originals = self._seal_upload(
            request,
            resolved,
            ingestion_tool_sha256,
        )
        ruleset = self._seal_ruleset(resolved, ingestion_tool_sha256)
        configuration = self._seal_configuration(
            resolved,
            snapshot_bytes,
            ingestion_tool_sha256,
        )
        publish_intent = self._publication(
            request,
            resolved,
            created_at,
            ingestion_tool_sha256,
        )
        spec = AdmissionSpec(
            schema_version=ADMISSION_SPEC_SCHEMA,
            run_id=request.run_id,
            request_id=request.request_id,
            parent_run_id=request.parent_run_id,
            from_stage=request.from_stage,
            principal=request.principal,
            target_hardware=request.target_hardware,
            purpose=request.purpose,
            change_summary=request.change_summary,
            main_assembly=request.main_assembly,
            originals=originals,
            ruleset=ruleset,
            configuration=configuration,
            executors=executors,
            native_tool_source_sha256=native_source_sha256,
            native_tool_record_sha256=native_record_sha256,
            native_tool_requirement=native_tool_requirement,
            contract_sha256=CONTRACT_SHA256,
            policy_sha256=check_policy_digest(),
            platform_config_sha256=platform_config_sha256,
            publish_intent=publish_intent,
            created_at=created_at,
        )
        return PreparedAdmission(
            spec=spec,
            run_admission=build_run_admission(spec),
        )

    def admit(
        self,
        application: AdmissionApplication,
        request: ProductionAdmissionRequest,
    ) -> RunAdmission:
        return self.prepare(request).register(application)


def load_production_admission(
    application: AdmissionApplication,
    request: ProductionAdmissionRequest,
    *,
    artifacts: ArtifactStore,
    platform_snapshot: Path,
    native_tool_provider: Callable[[], Mapping[str, Any]],
    work_root: Path,
    clock: Callable[[], datetime] | None = None,
    portable_tool_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> RunAdmission:
    """Minimal loader call for the production entry."""

    return ProductionAdmissionFactory(
        artifacts=artifacts,
        platform_snapshot=platform_snapshot,
        native_tool_provider=native_tool_provider,
        work_root=work_root,
        clock=clock,
        portable_tool_provider=portable_tool_provider,
    ).admit(application, request)


__all__ = [
    "AdmissionApplication",
    "PreparedAdmission",
    "ProductionAdmissionFactory",
    "ProductionAdmissionRequest",
    "ReceivedUploadReceipt",
    "load_production_admission",
]
