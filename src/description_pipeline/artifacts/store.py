"""Content-addressed immutable artifact trees."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from ..io import PipelineError, canonical, digest, file_digest, read_data
from ..pipeline.contracts import ArtifactRef, DispatchConf, StageId, StageResult
from ..schemas import ARTIFACT_MANIFEST_SCHEMA

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = ARTIFACT_MANIFEST_SCHEMA
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

    def _manifest_at(self, target: Path, *, artifact_id: str | None = None) -> dict:
        if target.is_symlink():
            raise ArtifactStoreError("An artifact object path may not be a symbolic link")
        manifest_path = target / MANIFEST_NAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ArtifactStoreError("The artifact manifest is missing")
        record = read_data(manifest_path)
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != MANIFEST_SCHEMA
            or record.get("artifact_id") != (artifact_id or target.name)
            or not isinstance(record.get("members"), list)
        ):
            raise ArtifactStoreError("The artifact manifest is not canonical")
        return record

    def _manifest(self, artifact_id: str) -> dict:
        return self._manifest_at(self.objects / artifact_id, artifact_id=artifact_id)

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

    def _verify_root(self, artifact: ArtifactRef, target: Path) -> dict:
        record = self._manifest_at(target, artifact_id=artifact.artifact_id)
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

    def verify(self, artifact: ArtifactRef) -> dict:
        if artifact.locator != f"{LOCATOR_PREFIX}{artifact.artifact_id}":
            raise ArtifactStoreError("The artifact locator differs from its immutable id")
        return self._verify_root(artifact, self.objects / artifact.artifact_id)

    def import_object(self, source_store: ArtifactStore, artifact: ArtifactRef) -> ArtifactRef:
        """Copy one verified source object without changing its manifest or producer identity."""

        if not isinstance(source_store, ArtifactStore):
            raise ArtifactStoreError("A source ArtifactStore is required")
        source_record = source_store.verify(artifact)
        if source_store.root.resolve() == self.root.resolve():
            return artifact
        target = self.objects / artifact.artifact_id
        if target.exists():
            if self.verify(artifact) != source_record:
                raise ArtifactStoreError("The destination artifact differs from the source manifest")
            return artifact
        source = source_store.objects / artifact.artifact_id
        temporary = Path(tempfile.mkdtemp(prefix=f".import-{artifact.artifact_id}.", dir=self.objects))
        try:
            shutil.copytree(source, temporary, dirs_exist_ok=True, symlinks=True)
            imported_record = self._verify_root(artifact, temporary)
            if imported_record != source_record:
                raise ArtifactStoreError("The imported artifact manifest differs from the source")
            for path in sorted(
                (item for item in temporary.rglob("*") if item.is_file()),
                key=lambda item: item.relative_to(temporary).as_posix(),
            ):
                _fsync_path(path)
            for directory in sorted(
                (item for item in temporary.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                _fsync_dir(directory)
            _fsync_dir(temporary)
            os.replace(temporary, target)
            _fsync_dir(self.objects)
            self.verify(artifact)
            return artifact
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

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
            try:
                os.replace(temporary, target)
            except OSError as error:
                if not target.exists():
                    raise
                published = self.verify(
                    self._ref(
                        self._manifest(artifact_id),
                        target / MANIFEST_NAME,
                    )
                )
                if published != record:
                    raise ArtifactStoreError(
                        "A concurrently published artifact differs from its source"
                    ) from error
                shutil.rmtree(temporary, ignore_errors=True)
                return self._ref(
                    self._manifest(artifact_id),
                    target / MANIFEST_NAME,
                )
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

    def seal_dispatch_conf(
        self,
        conf: DispatchConf,
        *,
        producer_run_id: str,
        producer_tool_sha256: str,
    ) -> ArtifactRef:
        if not isinstance(conf, DispatchConf) or conf.run_id != producer_run_id:
            raise ArtifactStoreError("The dispatch conf differs from its producer run")
        with tempfile.TemporaryDirectory(prefix="airflow-dispatch-conf-") as directory:
            root = Path(directory)
            (root / "conf.json").write_bytes(canonical(conf.to_record()) + b"\n")
            _fsync_path(root / "conf.json")
            _fsync_dir(root)
            return self.seal_tree(
                root,
                kind="airflow_dispatch_conf",
                producer_run_id=producer_run_id,
                producer_stage=None,
                producer_tool_sha256=producer_tool_sha256,
            )

    def read_dispatch_conf(self, artifact: ArtifactRef) -> DispatchConf:
        if artifact.kind != "airflow_dispatch_conf" or artifact.producer_stage is not None:
            raise ArtifactStoreError("The requested artifact is not an Airflow dispatch conf")
        record = self.verify(artifact)
        members = record["members"]
        if not isinstance(members, list) or len(members) != 1 or members[0].get("path") != "conf.json":
            raise ArtifactStoreError("The Airflow dispatch conf has an unexpected member set")
        conf = DispatchConf.from_record(read_data(self.objects / artifact.artifact_id / "conf.json"))
        if conf.run_id != artifact.producer_run_id:
            raise ArtifactStoreError("The Airflow dispatch conf differs from its producer")
        return conf
