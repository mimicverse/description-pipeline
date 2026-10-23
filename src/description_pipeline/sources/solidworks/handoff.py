"""Freeze a specified mechanical handoff for transport and native execution."""

from __future__ import annotations

import os
from pathlib import Path

from ...io import PipelineError, confined, digest
from .revision import package_inventory

HANDOFF_SCHEMA = "solidworks-to-urdf.handoff/v1"
MAX_HANDOFF_BYTES = 16 * 1024**3
MAX_HANDOFF_FILES = 100_000


def _directory(path: Path, *, exists: bool = True) -> Path:
    """Check links before resolving them, including linked parent directories."""
    path = Path(path).absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise PipelineError("A handoff directory and its parents must not be links or junctions")
    if (exists or path.exists()) and not path.is_dir():
        raise PipelineError(f"Mechanical handoff folder does not exist: {path}")
    return path.resolve()


def validate_handoff_roots(values) -> tuple[Path, ...]:
    """Require dedicated source directories, separate from service credentials."""
    if not isinstance(values, (list, tuple)) or not values:
        raise PipelineError("Configure at least one dedicated handoff root")
    roots = []
    broad = {Path.home(), Path.home().parent}
    if os.name == "posix":
        broad.update(
            Path(value)
            for value in ("/home", "/root", "/etc", "/usr", "/var", "/tmp", "/opt", "/srv", "/proc", "/sys", "/dev")
        )
    for value in values:
        path = Path(value)
        if not path.is_absolute():
            raise PipelineError("Handoff roots must be absolute directories")
        path = _directory(path)
        if path == Path(path.anchor) or path in broad:
            raise PipelineError("Handoff roots must be dedicated engineering directories")
        if path in roots:
            raise PipelineError("Duplicate handoff root")
        roots.append(path)
    return tuple(roots)


def authorize_handoff(source: Path, roots) -> Path:
    """Check the source boundary before inspecting or archiving any files."""
    roots = validate_handoff_roots(roots)
    source = Path(source)
    if not source.is_absolute():
        raise PipelineError("Handoff source must be absolute")
    source = _directory(source)
    if not any(source.is_relative_to(root) for root in roots):
        raise PipelineError("Handoff source is outside the configured engineering roots")
    return source


def describe_handoff(source: Path) -> dict:
    """Bind native engineering bytes without requiring generated definitions.

    Admission proves portable file identity and a nonempty assembly, not CAD
    validity. Native semantics and destination routing are resolved by the
    serialized Windows job after this immutable copy has been admitted.
    """
    source = _directory(source)
    files = package_inventory(source)
    if not files or len(files) > MAX_HANDOFF_FILES:
        raise PipelineError("Mechanical handoff has an empty or excessive file inventory")
    if sum(confined(source, name).stat().st_size for name in files) > MAX_HANDOFF_BYTES:
        raise PipelineError("Mechanical handoff exceeds the 16 GiB input limit")
    generated = {name for name in files if Path(name).name.casefold() in {"robot.yaml", "cad-revision.json"}}
    if generated:
        raise PipelineError("Native handoffs contain SolidWorks engineering only; remove generated pipeline inputs")
    assemblies = [name for name in files if Path(name).suffix.casefold() == ".sldasm"]
    if not assemblies or any(confined(source, name).stat().st_size == 0 for name in assemblies):
        raise PipelineError("A native handoff must contain a nonempty SolidWorks assembly")
    if package_inventory(source) != files:
        raise PipelineError("Mechanical handoff changed during inspection; select a saved immutable revision")
    return {
        "handoff_sha256": digest(files),
        "files": files,
    }
