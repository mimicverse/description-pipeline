"""Deterministic data and confined artifact IO, shared at format boundaries."""

from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any


class PipelineError(ValueError):
    """An explicit invalid input or unmet pipeline requirement."""

    #: Set when the failure preserved diagnostics the operator should inspect.
    diagnostic_path: str | None = None


def _lock_contention(error: OSError) -> bool:
    """Whether this platform's lock call failed BECAUSE another owner holds it.

    Only the platform's documented contention result counts, by ACTUAL errno:
    a POSIX non-blocking ``flock`` reports EAGAIN/EWOULDBLOCK, while
    ``msvcrt.locking`` reports a held region as EACCES (EDEADLK on a
    deadlock).  The exception class alone is not enough on POSIX -
    ``EINPROGRESS`` and ``EALREADY`` are also ``BlockingIOError`` instances
    but are not contention.  Every other lock-call failure is an observation
    error.
    """

    import errno

    if os.name == "nt":
        return error.errno in (errno.EACCES, errno.EDEADLK)
    return error.errno in (errno.EAGAIN, errno.EWOULDBLOCK)


def try_acquire_process_lock(path: Path) -> int | None:
    """Try to hold a kernel lock; ``None`` means another actor holds it now.

    Opening failures (missing rights, IO errors, a symlinked lock) stay loud
    and propagate, and so does every lock-call failure other than a proven
    platform contention result: they are observation errors, never evidence
    of a lock holder.  On success the lock file keeps this process's pid for
    diagnostics, and the caller owns closing the returned descriptor.
    """
    if path.is_symlink() or path.is_junction():
        raise PipelineError("Ownership lock must be a real file")
    handle = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(handle)
        if not _lock_contention(error):
            raise
        return None
    try:
        owner = str(os.getpid()).encode("ascii")
        os.write(handle, owner)
        os.ftruncate(handle, len(owner))
    except BaseException:
        os.close(handle)
        raise
    return handle


def acquire_process_lock(path: Path) -> int:
    """Hold a kernel lock until its descriptor closes, including on process exit.

    Competitors refuse loudly (``OSError``) instead of waiting.  Keep the lock
    file in place: unlinking would let another process lock a different inode
    while the first owner still runs. Its PID is diagnostic.
    """

    handle = try_acquire_process_lock(path)
    if handle is None:
        raise OSError(f"the ownership lock is already held by another process: {path}")
    return handle


def pin_utf8_streams() -> None:
    """Declare that this process exchanges UTF-8 on its standard streams.

    Everything the pipeline prints is consumed by another program — a launcher, a capture, a report
    reader — and the messages contain non-ASCII text.  Windows otherwise encodes redirected streams
    with the active ANSI code page (cp936 on the tested host), so a caller that reads UTF-8 cannot
    decode them and ``subprocess.run(text=True, encoding="utf-8")`` fails outright.  Pinning the
    streams here keeps every entry point on the same contract.
    """

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _canonical_value(value: Any) -> Any:
    """Normalize the canonical number rule: unified zero, no NaN/Infinity survived."""

    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("canonical encoding forbids NaN/Infinity")
        return 0.0 if value == 0.0 else value
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def canonical_json_value(value: object) -> str:
    """One stable JSON text form for warning identity, ordering, and display."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical(data: Any) -> bytes:
    """The single canonical encoding (contracts §122): UTF-8, sorted keys, compact
    separators (no extra whitespace), NaN/Infinity refused, unified zero
    (``-0.0`` encodes as ``0.0``).  Digests are taken over exactly these bytes.
    """

    return json.dumps(
        _canonical_value(data),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def format_number(value: Any) -> str:
    """The single numeric write format for URDF values: the shortest decimal that
    round-trips the exact binary64 value, locale-independent, unified zero
    (``-0.0`` and ``0.0`` both render ``0``); non-finite values are refused.
    """

    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise PipelineError(f"Cannot format a non-finite number: {value!r}")
    if number == 0.0:
        return "0"
    return repr(number)


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


def _load_yaml(text: str) -> Any:
    """Parse YAML with duplicate-key rejection; the only PyYAML-dependent surface.

    The import is deferred so the contract closure (schemas/io/delivery/stages/
    interface_records) loads in bootstrap interpreters without ambient packages:
    PyYAML is required only when a capture actually carries a YAML document.
    """
    try:
        import yaml  # noqa: PLC0415  (deferred on purpose: the bootstrap closure must import without PyYAML)
    except ModuleNotFoundError as error:
        raise PipelineError(f"the YAML parser (PyYAML) is required to read this input ({error.name})") from error

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            return _unique_mapping(
                (self.construct_object(key, deep=deep), self.construct_object(value, deep=deep))
                for key, value in node.value
            )

    try:
        return yaml.load(text, Loader=UniqueLoader)
    except yaml.YAMLError as error:
        raise PipelineError(str(error)) from error


def _parse_constant(name: str) -> Any:
    """Reject the non-finite JSON constants in the single foundation reader."""

    raise PipelineError(f"the JSON document carries the non-finite constant {name}")


def _parse_float(text: str) -> float:
    """Parse one JSON number, refusing values that overflow binary64 to non-finite."""

    value = float(text)
    if not math.isfinite(value):
        raise PipelineError(f"the JSON number {text} is not finite")
    return value


def parse_data(content: bytes, path: Path) -> Any:
    """Parse one captured byte sequence, retaining its filename for diagnostics."""
    try:
        # Windows PowerShell 5.1 writes a BOM for Set-Content -Encoding UTF8.
        text = content.decode("utf-8-sig")
        if path.suffix in {".yaml", ".yml"}:
            return _load_yaml(text)
        return json.loads(
            text,
            object_pairs_hook=_unique_mapping,
            parse_constant=_parse_constant,
            parse_float=_parse_float,
        )
    except (PipelineError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise PipelineError(f"Invalid input {path}: {error}") from error


def read_data(path: Path) -> Any:
    with open(_filesystem_path(path), "rb") as stream:
        return parse_data(stream.read(), path)


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
        # Windows-forbidden characters must be rejected before any transfer, even when the
        # inspection or archive runs on Linux: "< > " | ? *" cannot survive the handoff.
        or any(character in part for character in '<>"|?*')
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
    directories_seen: dict[str, str] = {}

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
            parent_parts = Path(relative).parts[:-1]
            for index in range(len(parent_parts)):
                prefix = "/".join(parent_parts[: index + 1])
                prefix_key = prefix.casefold()
                prior = directories_seen.get(prefix_key)
                if prior is None:
                    directories_seen[prefix_key] = prefix
                elif prior != prefix:
                    raise PipelineError(f"Case-colliding artifact paths: {relative}")
            if path.is_symlink() or path.is_junction():
                raise PipelineError(f"Symlink in artifact: {relative}")
            if not path.is_file():
                raise PipelineError(f"Artifact is not a regular file: {relative}")
            files[relative] = file_digest(confined(root, relative))
    return files
