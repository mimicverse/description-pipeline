"""Single-transfer Windows Discover protocol over the existing native domain."""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ..build.archive import ZIP_EPOCH
from ..io import PipelineError, artifact_path_parts, confined, digest, file_digest, read_data
from ..schemas import ARTIFACT_MANIFEST_SCHEMA
from ..sources.solidworks.discover_stage import (
    OBSERVATION_MANIFEST,
    _validated_tool,
    NativeDiscoverRefusal,
    NativeDiscoverRequest,
    NativeObservation,
    admit_native_observation,
    seal_native_observation,
)
from ..sources.solidworks.handoff import MAX_HANDOFF_BYTES
from ..sources.solidworks.discovery import DiscoverySettings, read_native_record
from ..sources.solidworks.native_selection import project_native_closure
from ..sources.solidworks.revision import package_inventory
from .selection_transport import StagedSelection

if TYPE_CHECKING:
    from ..artifacts import ArtifactStore

SOURCE_ARCHIVE = "source-set.zip"
OBSERVATION_ARCHIVE = "native-observation.zip"
MAX_OBSERVATION_BYTES = 512 * 1024 * 1024
_READ_CHUNK = 1024 * 1024


def seal_source_archive(source_root: Path, files: Mapping[str, str], destination: Path) -> Path:
    """Seal the complete SourceSet once as one deterministic transfer archive."""

    root = Path(source_root)
    destination = Path(destination)
    expected = dict(files)
    actual = package_inventory(root)
    manifest_path = root / "manifest.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            manifest = read_data(manifest_path)
        except (PipelineError, OSError, ValueError):
            manifest = None
        if (
            isinstance(manifest, dict)
            and manifest.get("schema_version") == ARTIFACT_MANIFEST_SCHEMA
            and isinstance(manifest.get("members"), list)
        ):
            members = manifest["members"]
            member_files = {
                str(item.get("path")): str(item.get("sha256"))
                for item in members
                if isinstance(item, dict)
            }
            if (
                len(member_files) == len(members)
                and digest({"members": members}) == manifest.get("set_sha256")
                and {name: value for name, value in actual.items() if name != "manifest.json"} == member_files
            ):
                actual.pop("manifest.json", None)
    if actual != expected:
        raise NativeDiscoverRefusal(
            "discover.source_set_changed",
            "The full SourceSet differs from the registered discover request",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(expected):
                source = confined(root, name)
                info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o644 << 16
                hasher = hashlib.sha256()
                size = 0
                with source.open("rb") as input_stream, archive.open(info, "w") as output_stream:
                    while block := input_stream.read(_READ_CHUNK):
                        size += len(block)
                        total += len(block)
                        if total > MAX_HANDOFF_BYTES:
                            raise NativeDiscoverRefusal(
                                "discover.source_set_too_large",
                                "The SourceSet exceeds the transfer limit",
                            )
                        hasher.update(block)
                        output_stream.write(block)
                if size != source.stat().st_size or hasher.hexdigest() != expected[name]:
                    raise NativeDiscoverRefusal(
                        "discover.source_set_changed",
                        "A SourceSet member changed while its transfer was sealed",
                        {"path": name},
                    )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def seal_observation_bundle(observation: NativeObservation, destination: Path) -> Path:
    """Seal one verified observation for its one Windows-to-Linux return."""

    admitted = admit_native_observation(observation.root, request=_request_for_observation(observation))
    files = admitted.manifest["files"]
    names = set(files) | {OBSERVATION_MANIFEST}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(names):
                source = confined(admitted.root, name)
                if name in files and file_digest(source) != files[name]:
                    raise NativeDiscoverRefusal(
                        "discover.observation_changed",
                        "Native observation changed while its return bundle was sealed",
                        {"path": name},
                    )
                total += source.stat().st_size
                if total > MAX_OBSERVATION_BYTES:
                    raise NativeDiscoverRefusal(
                        "discover.observation_too_large",
                        "Native observation exceeds the return-bundle limit",
                    )
                info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o644 << 16
                with source.open("rb") as input_stream, archive.open(info, "w") as output_stream:
                    while block := input_stream.read(_READ_CHUNK):
                        output_stream.write(block)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def admit_observation_bundle(
    archive: Path,
    destination: Path,
    *,
    request: NativeDiscoverRequest,
) -> NativeObservation:
    """Admit and extract exactly one sealed Windows observation bundle."""

    archive = Path(archive)
    destination = Path(destination)
    if destination.exists():
        raise NativeDiscoverRefusal("discover.observation_exists", "Observation extraction destination already exists")
    if not archive.is_file():
        raise NativeDiscoverRefusal("discover.observation_invalid", "Native observation bundle is missing")
    try:
        with zipfile.ZipFile(archive) as source:
            infos = source.infolist()
            names = [item.filename for item in infos]
            if (
                len(names) != len(set(names))
                or OBSERVATION_MANIFEST not in names
                or any(item.is_dir() or item.flag_bits & 0x1 for item in infos)
                or sum(item.file_size for item in infos) > MAX_OBSERVATION_BYTES
            ):
                raise NativeDiscoverRefusal("discover.observation_invalid", "Observation bundle members are invalid")
            for name in names:
                if "/".join(artifact_path_parts(name)) != name:
                    raise NativeDiscoverRefusal(
                        "discover.observation_invalid",
                        "Observation bundle carries a noncanonical member name",
                    )
            destination.mkdir(parents=True)
            for item in infos:
                target = confined(destination, item.filename, exists=False)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as input_stream, target.open("xb") as output_stream:
                    while block := input_stream.read(_READ_CHUNK):
                        output_stream.write(block)
            return admit_native_observation(
                destination,
                request=request,
                resources_settled=True,
            )
    except BaseException:
        if destination.exists():
            import shutil

            shutil.rmtree(destination)
        raise


def _request_for_observation(observation: NativeObservation) -> NativeDiscoverRequest:
    record = read_data(observation.root / "request.json")
    return NativeDiscoverRequest.from_record(record)


def _default_backend(cad_ownership_file: Path | None):
    from ..sources.solidworks.native import SolidWorksBackend

    return SolidWorksBackend(cad_ownership_file=cad_ownership_file)


def run_windows_discover(
    source_archive: Path,
    request: NativeDiscoverRequest,
    workspace: Path,
    *,
    native_tool: Mapping[str, object],
    backend_factory: Callable | None = None,
    cad_ownership_file: Path | None = None,
    on_activity=None,
) -> NativeObservation:
    """Run one native Discover operation without waiting for Linux or Capture.

    The archive is accepted exactly once. Native closure and raw observation
    are sealed before this function returns.
    """

    workspace = Path(workspace)
    if workspace.exists() and any(workspace.iterdir()):
        raise NativeDiscoverRefusal(
            "discover.workspace_invalid",
            "The Windows Discover workspace must be absent or empty",
        )
    workspace.mkdir(parents=True, exist_ok=True)
    staged = StagedSelection(
        workspace / "staged",
        dict(request.source_files),
        request.main_assembly,
        reader_sha256=request.expected_native_tool_sha256,
    )
    accepted = staged.accept_batch(Path(source_archive))
    if accepted is not True:
        raise NativeDiscoverRefusal(
            "discover.source_transfer_repeated",
            "A Discover operation must transfer its complete SourceSet exactly once",
        )
    _validated_tool(native_tool, request.expected_native_tool_sha256, request.expected_native_tool_record_sha256)
    factory = backend_factory or (lambda root, files: _default_backend(cad_ownership_file))
    backend = factory(staged.stage, request.source_files)
    try:
        record = read_native_record(
            staged.stage, DiscoverySettings(main_assembly=request.main_assembly),
            backend=backend, on_activity=on_activity,
        )
        evidence = project_native_closure(
            staged.stage, request.main_assembly, request.source_files, record,
            reader_sha256=request.expected_native_tool_sha256,
        )
        selection = staged.seal_closure(evidence)
        return seal_native_observation(
            staged.stage,
            workspace / "native-observation",
            request,
            selection=selection,
            native_tool=native_tool,
            source_archive_sha256=file_digest(Path(source_archive)),
            record=record,
        )
    except NativeDiscoverRefusal:
        raise
    except PipelineError as error:
        raise NativeDiscoverRefusal(
            str(getattr(error, "code", "discover.native_refused")),
            str(error),
            dict(getattr(error, "detail", {}) or {}),
        ) from error


class ArchiveDiscoverAdapter:
    """In-process native adapter; authorization is owned by ``StageEffectPort``.

    The production HTTP adapter must accept the same sealed source archive once
    and invoke :func:`run_windows_discover`; it must not add a definition
    callback, selection round or Capture continuation.
    """

    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        native_tool: Mapping[str, object],
            backend_factory: Callable | None = None,
        cad_ownership_file: Path | None = None,
        on_activity=None,
    ) -> None:
        self.artifacts = artifacts
        self.native_tool = dict(native_tool)
        self.backend_factory = backend_factory
        self.cad_ownership_file = cad_ownership_file
        self.on_activity = on_activity
        self.invocations: list[dict] = []

    def submit(
        self,
        source_archive: Path,
        request: NativeDiscoverRequest,
        target,
        workspace: Path,
    ) -> NativeObservation:
        del target
        self.invocations.append(
            {
                "operation_id": request.operation_id,
                "archive_sha256": file_digest(Path(source_archive)),
                "source_inventory_sha256": request.source_inventory_sha256,
            }
        )
        return run_windows_discover(
            source_archive,
            request,
            Path(workspace),
            native_tool=self.native_tool,
            backend_factory=self.backend_factory,
            cad_ownership_file=self.cad_ownership_file,
            on_activity=self.on_activity,
        )

    def resume_transfer(
        self,
        source_archive: Path,
        request: NativeDiscoverRequest,
        target,
        workspace: Path,
    ) -> NativeObservation | None:
        return self.submit(source_archive, request, target, workspace)

    def materialize(
        self,
        artifact,
        request: NativeDiscoverRequest,
        destination: Path,
    ) -> NativeObservation:
        root = self.artifacts.materialize(artifact, destination)
        return admit_native_observation(
            root,
            request=request,
            artifact_ref=artifact,
            resources_settled=True,
        )


__all__ = [
    "MAX_OBSERVATION_BYTES",
    "OBSERVATION_ARCHIVE",
    "SOURCE_ARCHIVE",
    "ArchiveDiscoverAdapter",
    "admit_observation_bundle",
    "run_windows_discover",
    "seal_observation_bundle",
    "seal_source_archive",
]
