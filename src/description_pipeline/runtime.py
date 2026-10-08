"""Installed tool integrity and the exact runtime dependency closure."""

from __future__ import annotations

import importlib.metadata
import platform
import sys
from pathlib import Path

from . import __version__
from .delivery import PIPELINE_ID
from .io import PipelineError, digest, file_digest, read_data

RUNTIME_PACKAGES = ("numpy", "PyYAML", "jsonschema", "packaging", "mujoco")


def tool_record() -> dict:
    """Identify the code and runtime actually used, without a mutable Git ref."""

    package = Path(__file__).resolve().parent
    files = {}
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.name != "tool-release.json":
            files[path.relative_to(package).as_posix()] = file_digest(path)
    release = package / "tool-release.json"
    identity = read_data(release) if release.is_file() else None
    source_hash = digest(files)
    if identity is not None and (
        identity.get("schema_version") != "solidworks-to-urdf.release/v1"
        or identity.get("pipeline_id") != PIPELINE_ID
        or identity.get("version") != __version__
        or identity.get("source_sha256") != source_hash
        or identity.get("package_files") != files
    ):
        raise PipelineError("Installed release files differ from the embedded source identity; reinstall the release")
    return {
        "schema_version": "solidworks-to-urdf.tool/v1",
        "pipeline_id": PIPELINE_ID,
        "version": __version__,
        "source_sha256": source_hash,
        "release": identity,
        "runtime": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "packages": runtime_packages(),
        },
    }



def runtime_packages() -> dict:
    """Record runtime dependency closure, excluding unrelated developer tools."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pending = [(name, frozenset()) for name in RUNTIME_PACKAGES + (("pywin32",) if sys.platform == "win32" else ())]
    versions = {}
    visited = set()
    while pending:
        requested, extras = pending.pop()
        distribution = importlib.metadata.distribution(requested)
        name = canonicalize_name(distribution.metadata["Name"])
        context = name, extras
        if context in visited:
            continue
        visited.add(context)
        versions[name] = distribution.version
        for value in distribution.requires or []:
            requirement = Requirement(value)
            if requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in {"", *extras}
            ):
                pending.append((requirement.name, frozenset(requirement.extras)))
    return dict(sorted(versions.items()))

