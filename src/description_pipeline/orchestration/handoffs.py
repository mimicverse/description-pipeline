"""Freeze a specified mechanical handoff for transport and native execution."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from ..io import PipelineError, _filesystem_path, artifact_path_parts, confined, digest
from ..sources.solidworks.revision import package_inventory

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


def freeze_handoff(source: Path, store: Path) -> tuple[Path, dict]:
    """Copy validated bytes into a content-addressed, reusable managed folder."""
    source = _directory(source)
    identity = describe_handoff(source)
    store = _directory(Path(store), exists=False)
    if store.is_relative_to(source):
        raise PipelineError("The managed import store must be separate from the engineering source directory")
    store.mkdir(parents=True, exist_ok=True)
    store = _directory(store)
    target = store / identity["handoff_sha256"]
    if target.exists():
        if describe_handoff(target) != identity:
            raise PipelineError("Managed handoff inventory was modified; retain its diagnostics and repair the store")
        return target, identity
    with tempfile.TemporaryDirectory(prefix=".handoff-", dir=store) as temporary:
        staging = Path(temporary) / "package"
        staging.mkdir()
        for name in identity["files"]:
            original = confined(source, name)
            copied = confined(staging, name, exists=False)
            physical = Path(_filesystem_path(copied))
            physical.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_filesystem_path(original), physical)
        if package_inventory(source) != identity["files"] or describe_handoff(staging) != identity:
            raise PipelineError("Mechanical handoff changed during import; no native job was started")
        try:
            os.rename(staging, target)
        except OSError:
            # Another import may have installed these same bytes concurrently.
            if not target.is_dir() or describe_handoff(target) != identity:
                raise
    return target, identity


def prepare_archive(source: Path, archive: Path) -> dict:
    """Create a bounded transport ZIP without including lock files or secrets."""
    source = _directory(source)
    identity = describe_handoff(source)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
        for name in sorted(identity["files"]):
            output.write(_filesystem_path(confined(source, name)), arcname=name)
    if package_inventory(source) != identity["files"]:
        Path(archive).unlink(missing_ok=True)
        raise PipelineError("Mechanical handoff changed while preparing transport")
    if Path(archive).stat().st_size > MAX_HANDOFF_BYTES:
        Path(archive).unlink(missing_ok=True)
        raise PipelineError("Mechanical handoff transport exceeds the 16 GiB limit")
    return identity


def import_archive(archive: Path, store: Path) -> tuple[Path, dict]:
    """Extract only regular, portable files; validate before installing a package."""
    if Path(archive).stat().st_size > MAX_HANDOFF_BYTES:
        raise PipelineError("Mechanical handoff transport exceeds the 16 GiB limit")
    store = _directory(Path(store), exists=False)
    store.mkdir(parents=True, exist_ok=True)
    store = _directory(store)
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=store) as temporary:
        package = Path(temporary)
        try:
            with zipfile.ZipFile(archive) as incoming:
                members = incoming.infolist()
                if not members or len(members) > MAX_HANDOFF_FILES:
                    raise PipelineError("Handoff ZIP has an empty or excessive inventory")
                if sum(member.file_size for member in members) > MAX_HANDOFF_BYTES:
                    raise PipelineError("Expanded handoff ZIP exceeds the 16 GiB limit")
                names = set()
                for member in members:
                    name = member.filename
                    parts = artifact_path_parts(name)
                    if not parts or "/".join(parts) != name or name.casefold() in names:
                        raise PipelineError("Handoff ZIP contains duplicate or noncanonical paths")
                    names.add(name.casefold())
                    mode = stat.S_IFMT(member.external_attr >> 16)
                    if member.is_dir() or mode not in {0, stat.S_IFREG} or member.flag_bits & 1:
                        raise PipelineError("Handoff ZIP may contain only unencrypted regular files")
                    if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                        raise PipelineError("Unsupported handoff ZIP compression")
                for member in members:
                    target = Path(_filesystem_path(confined(package, member.filename, exists=False)))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with incoming.open(member) as original, target.open("xb") as output:
                        shutil.copyfileobj(original, output, length=1024 * 1024)
        except (zipfile.BadZipFile, EOFError, RuntimeError) as error:
            raise PipelineError(f"Invalid handoff ZIP: {error}") from error
        return freeze_handoff(package, store)
