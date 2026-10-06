"""Immutable mechanical handoff identity, independent of a generated model."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ...io import PipelineError, digest, inventory, read_data

SCHEMA = "solidworks-to-urdf.cad-revision/v1"
FILENAME = "cad-revision.json"
NATIVE_SUFFIXES = {".sldasm", ".sldprt"}
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def package_inventory(package: Path) -> dict[str, str]:
    """Native document lock files are transient, never part of a handoff."""

    ignored = tuple(
        p.relative_to(package).as_posix()
        for p in package.rglob("~$*")
        if p.is_file() and p.suffix.lower() in NATIVE_SUFFIXES
    )
    return inventory(package, exclude=ignored)


def cad_inventory(package: Path) -> dict[str, str]:
    return {
        name: value
        for name, value in package_inventory(package).items()
        if Path(name).suffix.lower() in NATIVE_SUFFIXES
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _validate(data, files: dict[str, str], hardware_id: str | None) -> dict:
    _require(isinstance(data, dict), "CAD revision manifest must be an object")
    allowed = {
        "schema_version",
        "hardware_id",
        "revision",
        "parent_revision",
        "owner",
        "change_summary",
        "control",
        "cad_files",
    }
    _require(
        all(isinstance(k, str) for k in data) and not set(data) - allowed,
        "CAD revision manifest contains unknown fields",
    )
    _require(data.get("schema_version") == SCHEMA, f"CAD revision schema must be {SCHEMA}")
    for field in ("hardware_id", "revision"):
        value = data.get(field)
        _require(
            isinstance(value, str) and _ID.fullmatch(value) is not None,
            f"CAD revision {field} must be an ASCII identifier of at most 64 characters",
        )
    _require(
        hardware_id is None or hardware_id == data["hardware_id"], "CAD revision hardware_id differs from robot.yaml"
    )
    parent = data.get("parent_revision")
    _require(
        parent is None or (isinstance(parent, str) and _ID.fullmatch(parent) is not None),
        "parent_revision must be an ASCII revision identifier or null",
    )
    _require(parent != data["revision"], "A CAD revision cannot be its own parent")
    for field in ("owner", "change_summary"):
        _require(_text(data.get(field)), f"CAD revision {field} must be non-empty text")
    control = data.get("control")
    _require(
        isinstance(control, dict) and set(control) == {"system", "reference"},
        "CAD control must contain system and reference",
    )
    _require(
        isinstance(control["system"], str) and control["system"] in {"git", "pdm", "handoff"},
        "CAD control system must be git, pdm or handoff",
    )
    _require(_text(control["reference"]), "CAD control reference must identify the retained source revision")
    if control["system"] == "git":
        _require(
            re.search(r"\b[0-9a-f]{40}(?:[0-9a-f]{24})?\b", control["reference"]) is not None,
            "Git CAD control reference must contain the full commit hash",
        )
    declared = data.get("cad_files")
    _require(isinstance(declared, dict) and bool(declared), "CAD revision cad_files must be non-empty")
    _require(
        all(isinstance(k, str) and isinstance(v, str) and _SHA.fullmatch(v) for k, v in declared.items()),
        "CAD revision files need relative names and SHA-256 digests",
    )
    _require(
        bool(files) and any(Path(n).suffix.lower() == ".sldasm" for n in files),
        "A CAD handoff must contain a SolidWorks assembly",
    )
    _require(declared == files, "CAD revision inventory differs from package bytes; issue a new handoff revision")
    return data


def read_revision(package: Path, *, hardware_id: str | None = None) -> dict:
    """Prove the package still contains exactly its declared CAD revision."""

    package = Path(package)
    _require(not package.is_symlink() and not package.is_junction(), "A CAD handoff cannot be a symlink or junction")
    package = package.resolve()
    path = package / FILENAME
    _require(path.is_file(), f"Missing {FILENAME}; the mechanical team must seal the handoff revision")
    return _validate(read_data(path), cad_inventory(package), hardware_id)


def seal_revision(
    package: Path,
    *,
    hardware_id: str,
    revision: str,
    owner: str,
    system: str,
    reference: str,
    summary: str,
    parent_revision: str | None = None,
) -> dict:
    """Create a revision manifest once; never rewrite an existing handoff."""

    package = Path(package)
    _require(
        package.is_dir() and not package.is_symlink() and not package.is_junction(),
        "A CAD handoff must be an existing directory, not a link",
    )
    files = cad_inventory(package)
    data = _validate(
        {
            "schema_version": SCHEMA,
            "hardware_id": hardware_id,
            "revision": revision,
            "parent_revision": parent_revision,
            "owner": owner,
            "change_summary": summary,
            "control": {"system": system, "reference": reference},
            "cad_files": files,
        },
        files,
        hardware_id,
    )
    path = package / FILENAME
    if path.exists():
        existing = read_revision(package, hardware_id=hardware_id)
        _require(existing == data, "Handoff already sealed; create a new revision directory instead of overwriting it")
        return existing
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise PipelineError("Handoff was sealed by another process; inspect its revision") from error
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _require(
        cad_inventory(package) == files,
        "CAD files changed while sealing; discard this handoff and create a new revision",
    )
    return data


def check_successor(previous: dict, current: dict) -> None:
    """Publication cannot reuse a mechanical revision for changed CAD bytes."""

    _require(
        previous["hardware_id"] == current["hardware_id"], "CAD hardware identity changed across the review branch"
    )
    if previous["revision"] == current["revision"]:
        _require(
            digest(previous) == digest(current), "CAD revision reused for different content; create a new revision"
        )
    else:
        _require(
            current.get("parent_revision") == previous["revision"],
            "New CAD revision must name the previous published revision as its parent",
        )
