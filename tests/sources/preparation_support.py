"""Test-only composition of native reading and portable derivation."""

from pathlib import Path

from description_pipeline.sources.solidworks.discovery import (
    DiscoverySettings,
    PreparedPackage,
    _check_authored_files,
    _check_derivation_paths,
    derive_native_package,
    read_native_record,
)
from description_pipeline.sources.solidworks.revision import package_inventory


def prepare_native_package(
    frozen_source: Path,
    output: Path,
    run_id: str,
    *,
    backend=None,
    settings: DiscoverySettings | None = None,
    configuration: str | None = None,
    assembly: str | None = None,
    on_event=None,
    on_activity=None,
) -> PreparedPackage:
    """Compose the production leaf interfaces for recorded-backend tests."""

    settings = settings or DiscoverySettings()
    _check_derivation_paths(Path(frozen_source), Path(output))
    _check_authored_files(package_inventory(Path(frozen_source)))
    record = read_native_record(frozen_source, settings, backend=backend, on_activity=on_activity)
    return derive_native_package(
        frozen_source,
        output,
        run_id,
        record,
        settings=settings,
        configuration=configuration,
        assembly=assembly,
        on_event=on_event,
    )
