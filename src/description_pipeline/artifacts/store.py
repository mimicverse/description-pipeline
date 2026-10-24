"""Content-addressed immutable artifact trees."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from ..io import PipelineError, canonical, digest, file_digest, read_data
from ..pipeline.contracts import ArtifactRef, StageId, StageResult

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "description-pipeline.artifact-manifest/v1"
LOCATOR_PREFIX = "cas://artifact/"


class ArtifactStoreError(PipelineError):
    """An immutable artifact cannot be read, sealed or materialized safely."""


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ArtifactStore:
    """A local CAS whose manifests bind every byte in an artifact tree."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        if self.root.exists() and self.root.is_symlink():
            raise ArtifactStoreError("The artifact store root may not be a symbolic link")
        self.root.mkdir(parents=True, exist_ok=True)
        if any(item.is_symlink() for item in self.root.parents):
            raise ArtifactStoreError("The artifact store root may not traverse a symbolic link")
        self.objects = self.root / "objects"
        self.objects.mkdir(exist_ok=True)
        if self.objects.is_symlink():
            raise ArtifactStoreError("The artifact store object directory may not be a symbolic link")

    @classmethod
    def from_environment(cls, environ) -> ArtifactStore:
        root = environ.get("PLATFORM_ARTIFACT_ROOT")
        if not root:
            raise ArtifactStoreError("PLATFORM_ARTIFACT_ROOT is not configured")
        return cls(root)

    def _members(self, root: Path, *, allow_manifest: bool = False) -> list[dict]:
        if not root.is_dir() or root.is_symlink():
            raise ArtifactStoreError("An artifact source must be a real directory")
        members = []
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
                raise ArtifactStoreError("An artifact member path escapes its root")
            if relative == MANIFEST_NAME:
                if allow_manifest:
                    continue
                raise ArtifactStoreError("An artifact source may not carry the reserved manifest file")
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or path.is_symlink() or path.is_junction():
                raise ArtifactStoreError("An artifact may not contain symbolic links or junctions")
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ArtifactStoreError("An artifact may contain only regular files and directories")
            members.append(
                {
                    "bytes": path.stat().st_size,
                    "path": relative,
                    "sha256": file_digest(path),
                }
            )
        if not members:
            raise ArtifactStoreError("An artifact must contain at least one member")
        return members

    def _descriptor(
        self,
        *,
        kind: str,
        producer_run_id: str | None,
        producer_stage: StageId | None,
        producer_tool_sha256: str,
        members: list[dict],
    ) -> dict:
        set_sha256 = digest({"members": members})
        base = {
            "schema_version": MANIFEST_SCHEMA,
            "kind": kind,
            "producer": {
                "run_id": producer_run_id,
                "stage": producer_stage,
                "tool_sha256": producer_tool_sha256,
            },
            "set_sha256": set_sha256,
            "members": members,
        }
        artifact_id = digest(base)
        return {**base, "artifact_id": artifact_id}

    def _manifest(self, artifact_id: str) -> dict:
        target = self.objects / artifact_id
        if target.is_symlink():
            raise ArtifactStoreError("An artifact object path may not be a symbolic link")
        manifest_path = target / MANIFEST_NAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ArtifactStoreError("The artifact manifest is missing")
        record = read_data(manifest_path)
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != MANIFEST_SCHEMA
            or record.get("artifact_id") != artifact_id
            or not isinstance(record.get("members"), list)
        ):
            raise ArtifactStoreError("The artifact manifest is not canonical")
        return record

    def _ref(self, record: dict, manifest_path: Path) -> ArtifactRef:
        producer = record.get("producer")
        if not isinstance(producer, dict):
            raise ArtifactStoreError("The artifact manifest carries no producer")
        return ArtifactRef(
            artifact_id=record["artifact_id"],
            kind=record["kind"],
            locator=f"{LOCATOR_PREFIX}{record['artifact_id']}",
            manifest_sha256=file_digest(manifest_path),
            set_sha256=record["set_sha256"],
            producer_run_id=producer.get("run_id"),
            producer_stage=producer.get("stage"),
            producer_tool_sha256=producer.get("tool_sha256"),
        )

    def verify(self, artifact: ArtifactRef) -> dict:
        if artifact.locator != f"{LOCATOR_PREFIX}{artifact.artifact_id}":
            raise ArtifactStoreError("The artifact locator differs from its immutable id")
        target = self.objects / artifact.artifact_id
        record = self._manifest(artifact.artifact_id)
        manifest_path = target / MANIFEST_NAME
        stored_ref = self._ref(record, manifest_path)
        if stored_ref != artifact:
            raise ArtifactStoreError("The registered ArtifactRef differs from its manifest")
        members = self._members(target, allow_manifest=True)
        if members != record["members"]:
            raise ArtifactStoreError("The artifact bytes differ from their manifest")
        if digest({"members": members}) != record["set_sha256"]:
            raise ArtifactStoreError("The artifact member-set digest is invalid")
        actual_files = {item.relative_to(target).as_posix() for item in target.rglob("*") if item.is_file()}
        expected_files = {MANIFEST_NAME, *(item["path"] for item in members)}
        if actual_files != expected_files:
            raise ArtifactStoreError("The artifact tree carries an unregistered member")
        return record

    def seal_tree(
        self,
        source: str | Path,
        *,
        kind: str,
        producer_run_id: str | None,
        producer_stage: StageId | None,
        producer_tool_sha256: str,
    ) -> ArtifactRef:
        supplied = Path(source)
        if supplied.is_symlink() or supplied.is_junction():
            raise ArtifactStoreError("An artifact source root may not be a symbolic link")
        origin = supplied.resolve()
        if any(parent.is_symlink() for parent in origin.parents):
            raise ArtifactStoreError("An artifact source may not traverse a symbolic link")
        first = self._members(origin)
        record = self._descriptor(
            kind=kind,
            producer_run_id=producer_run_id,
            producer_stage=producer_stage,
            producer_tool_sha256=producer_tool_sha256,
            members=first,
        )
        artifact_id = record["artifact_id"]
        target = self.objects / artifact_id
        if target.exists():
            self.verify(self._ref(self._manifest(artifact_id), target / MANIFEST_NAME))
            return self._ref(self._manifest(artifact_id), target / MANIFEST_NAME)

        temporary = Path(tempfile.mkdtemp(prefix=f".{artifact_id}.", dir=self.objects))
        try:
            for item in first:
                source_path = origin / item["path"]
                destination = temporary / item["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, destination)
                _fsync_path(destination)
            copied = self._members(temporary)
            if copied != first:
                raise ArtifactStoreError("The artifact source changed while it was being sealed")
            manifest_bytes = canonical(record) + b"\n"
            (temporary / MANIFEST_NAME).write_bytes(manifest_bytes)
            _fsync_path(temporary / MANIFEST_NAME)
            for directory in sorted(
                (path for path in temporary.rglob("*") if path.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                _fsync_dir(directory)
            _fsync_dir(temporary)
            os.replace(temporary, target)
            _fsync_dir(self.objects)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return self._ref(record, target / MANIFEST_NAME)

    def materialize(self, artifact: ArtifactRef, destination: str | Path) -> Path:
        self.verify(artifact)
        target = Path(destination)
        if target.exists():
            raise ArtifactStoreError("An artifact destination must not already exist")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{artifact.artifact_id}.", dir=target.parent))
        shutil.rmtree(temporary)
        source = self.objects / artifact.artifact_id
        try:
            shutil.copytree(source, temporary)
            if self._members(temporary, allow_manifest=True) != self._manifest(artifact.artifact_id)["members"]:
                raise ArtifactStoreError("The materialized artifact differs from its manifest")
            os.replace(temporary, target)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return target

    def seal_stage_result(
        self,
        result: StageResult,
    ) -> ArtifactRef:
        with tempfile.TemporaryDirectory(prefix="stage-result-") as directory:
            root = Path(directory)
            (root / "result.json").write_bytes(canonical(result.to_record()) + b"\n")
            _fsync_path(root / "result.json")
            _fsync_dir(root)
            return self.seal_tree(
                root,
                kind="stage_result",
                producer_run_id=result.run_id,
                producer_stage=result.stage,
                producer_tool_sha256=result.actual_executor_sha256,
            )

    def read_result(self, artifact: ArtifactRef) -> StageResult:
        if artifact.kind != "stage_result":
            raise ArtifactStoreError("The requested artifact is not a StageResult record")
        self.verify(artifact)
        record = read_data(self.objects / artifact.artifact_id / "result.json")
        result = StageResult.from_record(record)
        if (
            result.run_id != artifact.producer_run_id
            or result.stage != artifact.producer_stage
            or result.actual_executor_sha256 != artifact.producer_tool_sha256
        ):
            raise ArtifactStoreError("The StageResult record differs from its artifact producer")
        return result
