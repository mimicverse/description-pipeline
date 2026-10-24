"""Single-transfer Windows Discover protocol over the existing native domain."""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..build.archive import ZIP_EPOCH
from ..io import PipelineError, artifact_path_parts, confined, file_digest, read_data
from ..sources.solidworks.discover_stage import (
    OBSERVATION_MANIFEST,
    NativeDiscoverRefusal,
    NativeDiscoverRequest,
    NativeObservation,
    admit_native_observation,
    observe_native_record,
)
from ..sources.solidworks.handoff import MAX_HANDOFF_BYTES
from ..sources.solidworks.revision import package_inventory
from .selection_transport import StagedSelection

SOURCE_ARCHIVE = "source-set.zip"
OBSERVATION_ARCHIVE = "native-observation.zip"
MAX_OBSERVATION_BYTES = 512 * 1024 * 1024
_READ_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class NativeDiscoverExecution:
    """One satisfied native effect and its sealed observation."""

    effect_id: str
    observation: NativeObservation


class NativeDiscoverFailure(NativeDiscoverRefusal):
    """A definite native refusal after the effect was authorized."""

    def __init__(self, effect_id: str, code: str, message: str, detail: Mapping[str, object] | None = None):
        super().__init__(code, message, detail)
        self.effect_id = effect_id


def seal_source_archive(source_root: Path, files: Mapping[str, str], destination: Path) -> Path:
    """Seal the complete SourceSet once as one deterministic transfer archive."""

    root = Path(source_root)
    destination = Path(destination)
    expected = dict(files)
    if package_inventory(root) != expected:
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
            return admit_native_observation(destination, request=request)
    except BaseException:
        if destination.exists():
            import shutil

            shutil.rmtree(destination)
        raise


def _request_for_observation(observation: NativeObservation) -> NativeDiscoverRequest:
    record = read_data(observation.root / "request.json")
    return NativeDiscoverRequest.from_record(record)


def _default_selection_reader(request: NativeDiscoverRequest, cad_ownership_file: Path | None):
    from ..sources.solidworks.native_selection import NativeSelectionReader

    return NativeSelectionReader(
        reader_sha256=request.expected_native_tool_sha256,
        cad_ownership_file=cad_ownership_file,
    )


def _default_backend(source_root: Path, files: Mapping[str, str], cad_ownership_file: Path | None):
    from ..sources.solidworks.native import SolidWorksBackend

    backend = SolidWorksBackend(cad_ownership_file=cad_ownership_file)
    backend.configure_staged_search(source_root)
    backend.seal_documents([confined(source_root, name) for name in files])
    return backend


def run_windows_discover(
    source_archive: Path,
    request: NativeDiscoverRequest,
    workspace: Path,
    *,
    native_tool: Mapping[str, object],
    selection_reader_factory: Callable | None = None,
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
    reader_factory = selection_reader_factory or (lambda: _default_selection_reader(request, cad_ownership_file))
    reader = reader_factory()
    try:
        evidence = reader.observe(staged.stage, request.main_assembly, dict(request.source_files))
        selection = staged.seal_closure(evidence)
    except NativeDiscoverRefusal:
        raise
    except PipelineError as error:
        raise NativeDiscoverRefusal(
            str(getattr(error, "code", "discover.native_refused")),
            str(error),
            dict(getattr(error, "detail", {}) or {}),
        ) from error
    factory = backend_factory or (lambda root, files: _default_backend(root, files, cad_ownership_file))
    backend = factory(staged.stage, selection["files"])
    try:
        return observe_native_record(
            staged.stage,
            workspace / "native-observation",
            request,
            selection=selection,
            native_tool=native_tool,
            source_archive_sha256=file_digest(Path(source_archive)),
            backend=backend,
            on_activity=on_activity,
        )
    except NativeDiscoverRefusal:
        raise
    except PipelineError as error:
        raise NativeDiscoverRefusal(
            str(getattr(error, "code", "discover.native_refused")),
            str(error),
            dict(getattr(error, "detail", {}) or {}),
        ) from error


class LocalArchiveDiscoverPort:
    """Adapter that exercises the exact Windows protocol without a live endpoint.

    The production HTTP adapter must accept the same sealed source archive once
    and invoke :func:`run_windows_discover`; it must not add a definition
    callback, selection round or Capture continuation.
    """

    def __init__(
        self,
        *,
        native_tool: Mapping[str, object],
        selection_reader_factory: Callable | None = None,
        backend_factory: Callable | None = None,
        cad_ownership_file: Path | None = None,
        on_activity=None,
    ) -> None:
        self.native_tool = dict(native_tool)
        self.selection_reader_factory = selection_reader_factory
        self.backend_factory = backend_factory
        self.cad_ownership_file = cad_ownership_file
        self.on_activity = on_activity
        self.transfers: list[dict] = []

    def discover(
        self,
        request: NativeDiscoverRequest,
        source_root: Path,
        workspace: Path,
    ) -> NativeDiscoverExecution:
        effect_id = f"native-discover:{request.operation_id}"
        archive = seal_source_archive(source_root, request.source_files, Path(workspace) / SOURCE_ARCHIVE)
        self.transfers.append(
            {
                "operation_id": request.operation_id,
                "archive_sha256": file_digest(archive),
                "source_inventory_sha256": request.source_inventory_sha256,
            }
        )
        try:
            observation = run_windows_discover(
                archive,
                request,
                Path(workspace) / "windows",
                native_tool=self.native_tool,
                selection_reader_factory=self.selection_reader_factory,
                backend_factory=self.backend_factory,
                cad_ownership_file=self.cad_ownership_file,
                on_activity=self.on_activity,
            )
        except NativeDiscoverRefusal as refusal:
            raise NativeDiscoverFailure(
                effect_id,
                refusal.code,
                refusal.message,
                refusal.detail,
            ) from refusal
        return NativeDiscoverExecution(
            effect_id=effect_id,
            observation=observation,
        )


__all__ = [
    "MAX_OBSERVATION_BYTES",
    "OBSERVATION_ARCHIVE",
    "SOURCE_ARCHIVE",
    "LocalArchiveDiscoverPort",
    "NativeDiscoverExecution",
    "NativeDiscoverFailure",
    "admit_observation_bundle",
    "run_windows_discover",
    "seal_observation_bundle",
    "seal_source_archive",
]
