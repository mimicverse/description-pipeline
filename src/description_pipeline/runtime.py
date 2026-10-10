"""Installed tool integrity and the exact runtime dependency closure."""

from __future__ import annotations

import importlib.metadata
import importlib
import platform
import sys
from pathlib import Path

from . import __version__
from .delivery import PIPELINE_ID
from .io import PipelineError, digest, file_digest, read_data

RUNTIME_PACKAGES = ("numpy", "PyYAML", "jsonschema", "packaging", "python-multipart")
RUNTIME_VERSIONS = {
    "numpy": "2.5.3",
    "PyYAML": "6.0.3",
    "jsonschema": "4.26.0",
    "packaging": "26.3",
    "python-multipart": "0.0.32",
    "pywin32": "311",
    "mujoco": "3.13.0",
}


def runtime_role(role: str | None = None) -> str:
    role = role or ("native" if sys.platform == "win32" else "portable")
    if role not in {"native", "portable"}:
        raise PipelineError(f"Unknown runtime role: {role}")
    return role


def required_packages(role: str | None = None) -> tuple[str, ...]:
    return RUNTIME_PACKAGES + (("pywin32",) if runtime_role(role) == "native" else ("mujoco",))


def native_readiness() -> dict:
    """Check native prerequisites without opening CAD or importing a consumer."""
    if sys.platform != "win32":
        raise PipelineError("Native CAD capture requires Windows with licensed SolidWorks")
    if sys.version_info[:2] != (3, 12):
        raise PipelineError("Native CAD capture requires Python 3.12")
    if importlib.metadata.version("pywin32") != RUNTIME_VERSIONS["pywin32"]:
        raise PipelineError("Reinstall the pinned Windows runtime: pywin32 version differs")
    importlib.import_module("pythoncom")
    importlib.import_module("win32com.client")
    from .sources.solidworks.isolation import registered_executable

    return {
        "role": "native",
        "solidworks_executable": registered_executable(),
        "scope": "Native prerequisites; the capture session checks CAD access and document readiness",
    }


def tool_record(role: str | None = None) -> dict:
    """Identify the code and runtime actually used, without a mutable Git ref."""

    role = runtime_role(role)
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
            "role": role,
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "packages": runtime_packages(role),
        },
    }


def runtime_packages(role: str | None = None) -> dict:
    """Record runtime dependency closure, excluding unrelated developer tools."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pending = [(name, frozenset()) for name in required_packages(role)]
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
