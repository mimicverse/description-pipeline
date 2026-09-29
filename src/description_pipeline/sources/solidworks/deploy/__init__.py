"""Windows deployment resources for the SolidWorks collection worker.

The worker script, the host-config template and the Windows dependency lock are
package resources: a plain ``pip install`` of the pipeline already carries them,
and they are exported or packaged into the offline worker bundle from here
rather than from a checkout-relative ``deploy/`` directory.

    python -m description_pipeline.sources.solidworks.deploy export --out DIR
    python -m description_pipeline.sources.solidworks.deploy build-bundle \\
        --out description-worker-<version>.zip [--wheels DIR]
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

RESOURCE_PACKAGE = "description_pipeline.sources.solidworks.deploy"
WORKER_SCRIPT = "worker.ps1"
HOST_TEMPLATE = "worker-host.example.json"
SUBMIT_SCRIPT = "submit.ps1"
SUBMIT_TEMPLATE = "submit-host.example.json"
LOCK_FILE = "requirements/win-py312.lock"
BUNDLE_BUILDER = "build_bundle.py"


def resource_path(relative: str) -> Path:
    """Return the on-disk path of one packaged deployment resource."""

    candidate = Path(__file__).resolve().parent / relative
    if not candidate.is_file():
        # A zip-imported package has no real path; fall back to the resource API
        # so callers get a readable error instead of a silent empty export.
        traversable = resources.files(RESOURCE_PACKAGE).joinpath(relative)
        if not traversable.is_file():
            raise FileNotFoundError(f"deployment resource is missing from the package: {relative}")
        return Path(str(traversable))
    return candidate


def export_deploy_resources(destination: Path) -> list[Path]:
    """Copy ``worker.ps1``, the host template and the lock into ``destination``."""

    target = Path(destination)
    written: list[Path] = []
    for relative in (WORKER_SCRIPT, HOST_TEMPLATE, SUBMIT_SCRIPT, SUBMIT_TEMPLATE, LOCK_FILE, BUNDLE_BUILDER):
        source = resource_path(relative)
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output)
        written.append(output)
    return written


def lock_requirements() -> list[str]:
    """The pinned Windows dependency set, read from the packaged lock."""

    lines = []
    for line in resource_path(LOCK_FILE).read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            lines.append(entry)
    return lines


def build_worker_bundle(out: Path, *, version: str, source: Path, wheels: Path | None = None) -> dict:
    """Build the offline Windows bundle from the packaged resources."""

    from .build_bundle import build

    requirements = tuple(lock_requirements())
    return build(Path(source), Path(out), version, wheels, requirements)
