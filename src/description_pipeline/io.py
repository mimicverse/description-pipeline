"""Deterministic data and confined artifact IO, shared at format boundaries."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml


class PipelineError(ValueError):
    """An explicit invalid input or unmet pipeline requirement."""

    #: Set when the failure preserved diagnostics the operator should inspect.
    diagnostic_path: str | None = None


def pin_utf8_streams() -> None:
    """Declare that this process exchanges UTF-8 on its standard streams.

    Everything the pipeline prints is consumed by another program — a launcher, a capture, a report
    reader — and the messages contain non-ASCII text.  Windows otherwise encodes redirected streams
    with the active ANSI code page (cp936 on the tested host), so a caller that reads UTF-8 cannot
    decode them and ``subprocess.run(text=True, encoding="utf-8")`` fails outright.  Pinning the
    streams here keeps every entry point on the same contract; the Windows ``description.cmd`` shim
    sets the console code page to match.
    """

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def canonical(data: Any) -> bytes:
    return (json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()


def digest(data: Any) -> str:
    return hashlib.sha256(canonical(data)).hexdigest()


def _windows_extended_path(value: str, *, force: bool = False) -> str:
    """Normalize a Windows path and add the extended prefix only when needed."""

    value = ntpath.abspath(value)
    if value.startswith("\\\\?\\") or (not force and len(value) < 240):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    if len(value) >= 2 and value[1] == ":":
        return "\\\\?\\" + value
    return value


def _filesystem_path(path: Path) -> str | Path:
    """Return an extended Windows path only when ordinary Win32 paths are too long.

    Manifest keys and all paths crossing the pipeline boundary remain ordinary
    relative POSIX strings.  The prefix is used only for local Python file I/O;
    consumers such as MuJoCo receive the normal short paths they support.
    """

    if os.name != "nt":
        return path
    return _windows_extended_path(os.fspath(path))


def _physical_path(path: Path) -> str | Path:
    """Use the extended namespace for directory traversal on Windows.

    ``os.walk`` can yield an ordinary path for a child whose *parent* is short;
    that child then crosses ``MAX_PATH`` even though the walk root did not.  A
    forced extended root makes every descendant returned by the walk usable.
    The prefix remains an internal filesystem detail and never enters a
    manifest or other serialized boundary.
    """

    if os.name != "nt":
        return path
    return _windows_extended_path(os.fspath(path), force=True)


def file_digest(path: Path) -> str:
    with open(_filesystem_path(path), "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _unique_mapping(pairs) -> dict:
    mapping = {}
    for key, value in pairs:
        try:
            if key in mapping:
                raise PipelineError(f"Duplicate input key: {key!r}")
            mapping[key] = value
        except TypeError as error:
            raise PipelineError("Input mapping keys must be scalar values") from error
    return mapping


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        return _unique_mapping(
            (self.construct_object(key, deep=deep), self.construct_object(value, deep=deep))
            for key, value in node.value
        )


def read_data(path: Path) -> Any:
    # Windows PowerShell 5.1 writes a BOM for Set-Content -Encoding UTF8.
    with open(_filesystem_path(path), encoding="utf-8-sig") as stream:
        try:
            if path.suffix in {".yaml", ".yml"}:
                return yaml.load(stream, Loader=_UniqueLoader)
            return json.load(stream, object_pairs_hook=_unique_mapping)
        except (PipelineError, yaml.YAMLError, json.JSONDecodeError) as error:
            raise PipelineError(f"Invalid input {path}: {error}") from error


def write_json(path: Path, data: Any) -> None:
    path = Path(_filesystem_path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def artifact_path_parts(relative: str) -> tuple[str, ...]:
    if not relative or "\\" in relative or Path(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise PipelineError(f"Invalid relative artifact path: {relative!r}")
    parts = Path(relative).parts
    if any(part in {"..", ".git"} for part in parts):
        raise PipelineError(f"Escaping artifact path: {relative!r}")
    if any(
        part.endswith((".", " "))
        or ":" in part
        or PureWindowsPath(part).is_reserved()
        or any(ord(character) < 32 for character in part)
        for part in parts
    ):
        raise PipelineError(f"Nonportable artifact path: {relative!r}")
    return parts


def confined(root: Path, relative: str, *, exists: bool = True) -> Path:
    root = root.resolve()
    parts = artifact_path_parts(relative)
    path = root.joinpath(*parts)
    physical_root = Path(_physical_path(root))
    physical_path = physical_root.joinpath(*parts)
    for parent in (physical_path, *physical_path.parents):
        if parent == physical_root:
            break
        if parent.is_symlink() or parent.is_junction():
            raise PipelineError(f"Symlink is not an immutable artifact: {relative}")
    if not physical_path.resolve().is_relative_to(physical_root.resolve()):
        raise PipelineError(f"Escaping artifact path: {relative!r}")
    if exists and not physical_path.is_file():
        raise PipelineError(f"Missing artifact: {relative}")
    return path


def inventory(root: Path, *, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    root = Path(_physical_path(root))
    if not root.exists():
        return {}
    if root.is_symlink() or root.is_junction():
        raise PipelineError(f"Symlink in artifact: {root}")
    files = {}
    names = set()

    def scan_error(error: OSError) -> None:
        raise PipelineError(f"Cannot scan artifact directory {error.filename}: {error}") from error

    for directory, directories, filenames in os.walk(root, topdown=True, followlinks=False, onerror=scan_error):
        directory_path = Path(directory)
        for name in directories:
            path = directory_path / name
            if path.is_symlink() or path.is_junction():
                relative = path.relative_to(root).as_posix()
                raise PipelineError(f"Symlink in artifact: {relative}")
        for name in filenames:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if relative in exclude:
                continue
            if relative.casefold() in names:
                raise PipelineError(f"Case-colliding artifact paths: {relative}")
            names.add(relative.casefold())
            if path.is_symlink() or path.is_junction():
                raise PipelineError(f"Symlink in artifact: {relative}")
            if not path.is_file():
                raise PipelineError(f"Artifact is not a regular file: {relative}")
            files[relative] = file_digest(confined(root, relative))
    return files
