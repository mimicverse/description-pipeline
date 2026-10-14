"""Native document inventory of one frozen engineering handoff."""

from __future__ import annotations

from pathlib import Path

from ...io import inventory

NATIVE_SUFFIXES = {".sldasm", ".sldprt"}


def package_inventory(package: Path) -> dict[str, str]:
    """Native document lock files are transient, never part of a handoff."""

    ignored = tuple(
        p.relative_to(package).as_posix()
        for p in package.rglob("~$*")
        if p.is_file() and p.suffix.lower() in NATIVE_SUFFIXES
    )
    return inventory(package, exclude=ignored)
