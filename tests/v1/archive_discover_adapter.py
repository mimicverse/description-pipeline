"""Local adapter seam for Discover tests; production uses NativeOperationDiscoverPort."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import file_digest
from description_pipeline.orchestration.discover_protocol import (
    admit_native_observation,
    run_windows_discover,
)
from description_pipeline.sources.solidworks.discover_stage import (
    NativeDiscoverRequest,
    NativeObservation,
)


class ArchiveDiscoverAdapter:
    """Test-only in-process native adapter."""

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

    def _run(
        self,
        source_archive: Path,
        request: NativeDiscoverRequest,
        workspace: Path,
    ) -> NativeObservation:
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

    def submit(
        self,
        source_archive: Path,
        request: NativeDiscoverRequest,
        target,
        workspace: Path,
    ) -> NativeObservation:
        del target
        return self._run(source_archive, request, workspace)

    def resume_transfer(
        self,
        source_archive: Path,
        request: NativeDiscoverRequest,
        target,
        workspace: Path,
    ) -> NativeObservation:
        del target
        return self._run(source_archive, request, workspace)

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
