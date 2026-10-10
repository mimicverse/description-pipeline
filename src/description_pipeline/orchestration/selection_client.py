"""Bounded ZIP batches for selection rounds (Linux side).

``prepare_selection_batch`` archives exactly the requested names that the upload
index authorizes: canonical portable paths with identical sha256, real files
under the source, verified before, during and after archiving.  Only requested
names are read, size limits are enforced from recorded sizes and while streaming,
and every refusal removes the archive this call created.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from ..io import PipelineError, _filesystem_path, confined
from ..sources.solidworks.handoff import MAX_HANDOFF_BYTES, _directory
from ..sources.solidworks.selection import MAX_SELECTION_FILES, validate_file_index

#: Per-batch caps; the shared round validator may tighten these.
MAX_BATCH_FILES = MAX_SELECTION_FILES
MAX_BATCH_BYTES = MAX_HANDOFF_BYTES
_READ_CHUNK = 1 << 20

__all__ = ["MAX_BATCH_BYTES", "MAX_BATCH_FILES", "prepare_selection_batch"]


def _disk_digest(path: Path, *, limit: int) -> str:
    """Hash at most ``limit`` bytes; growth beyond the recorded size aborts."""

    hasher = hashlib.sha256()
    read = 0
    with open(_filesystem_path(path), "rb") as stream:
        for block in iter(lambda: stream.read(_READ_CHUNK), b""):
            read += len(block)
            if read > limit:
                raise PipelineError(f"Selection batch member exceeds its recorded size: {path.name!r}")
            hasher.update(block)
    return hasher.hexdigest()


def _write_member(output: zipfile.ZipFile, path: Path, name: str, *, limit: int) -> tuple[int, str]:
    """Write one member bounded by ``limit``, hashing the stream that enters the archive."""

    hasher = hashlib.sha256()
    written = 0
    with open(_filesystem_path(path), "rb") as stream, output.open(name, "w", force_zip64=True) as member:
        for block in iter(lambda: stream.read(_READ_CHUNK), b""):
            written += len(block)
            if written > limit:
                raise PipelineError(f"Selection batch member exceeds its recorded size: {name!r}")
            member.write(block)
            hasher.update(block)
    return written, hasher.hexdigest()


def prepare_selection_batch(
    source: Path,
    archive: Path,
    requested: dict[str, str],
    *,
    expected_upload: dict[str, str],
) -> dict:
    """Archive exactly ``requested ∩ expected_upload`` into one bounded ZIP.

    Every requested name must appear in the upload index with the identical
    sha256, must be a real file under ``source`` (symlinks and linked parents
    are refused), and its bytes are verified before, during and after
    archiving, including a read-back of the sealed archive.  Only the requested
    names are read.  Sizes are enforced from recorded file sizes before any
    content is read and while streaming.  Returns ``{"files": {name: sha256},
    "bytes": n}`` where ``bytes`` is the sum of member sizes.  The archive is
    created exclusively - an existing file is never replaced - and a failed
    call removes only the archive this call created.
    """

    source = _directory(source)
    archive = Path(archive)
    upload = validate_file_index(expected_upload)
    asked = validate_file_index(requested)
    if len(asked) > MAX_BATCH_FILES:
        raise PipelineError("Selection batch exceeds the file cap")
    outside = sorted(name for name in asked if name not in upload)
    if outside:
        raise PipelineError("Selection batch requests paths outside the upload index", {"paths": outside[:10]})
    mismatch = sorted(name for name in asked if asked[name] != upload[name])
    if mismatch:
        raise PipelineError("Selection batch hashes disagree with the upload index", {"paths": mismatch[:10]})
    if archive.resolve().is_relative_to(source):
        raise PipelineError("The selection batch archive must be outside the source tree")

    # Preflight from recorded sizes: missing members, links and cumulative caps
    # are decided before any file content is read.
    planned: dict[str, tuple[Path, int]] = {}
    total = 0
    for name in sorted(asked):
        path = Path(_filesystem_path(confined(source, name)))
        if path.is_symlink():
            raise PipelineError(f"Symlink is not an immutable artifact: {name!r}")
        size = path.stat().st_size
        total += size
        if total > MAX_BATCH_BYTES:
            raise PipelineError("Selection batch exceeds the byte cap")
        planned[name] = (path, size)

    files: dict[str, str] = {}
    written_bytes = 0
    try:
        output = zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_STORED, allowZip64=True)
    except FileExistsError as error:
        raise PipelineError("Selection batch archive already exists", {"path": str(archive)}) from error
    except FileNotFoundError as error:
        raise PipelineError("Selection batch archive directory does not exist", {"path": str(archive)}) from error
    try:
        with output:
            for name in sorted(asked):
                path, size = planned[name]
                if _disk_digest(path, limit=size) != asked[name]:
                    raise PipelineError(f"Selection batch member changed before archiving: {name!r}")
                written, digest = _write_member(output, path, name, limit=size)
                if digest != asked[name]:
                    raise PipelineError(f"Selection batch member changed while archiving: {name!r}")
                files[name] = digest
                written_bytes += written
        if written_bytes != total:
            raise PipelineError("Selection batch member changed while archiving: size drift")
        # Any mutation of the source during archiving fails the batch instead of
        # shipping mixed bytes.
        for name in sorted(asked):
            path, size = planned[name]
            if _disk_digest(path, limit=size) != asked[name]:
                raise PipelineError(f"Selection batch member changed while archiving: {name!r}")
        # Prove the sealed bytes are the requested bytes.
        with zipfile.ZipFile(archive) as sealed:
            if sorted(sealed.namelist()) != sorted(asked):
                raise PipelineError("Selection batch archive does not match the requested names")
            for name in sorted(asked):
                size = planned[name][1]
                hasher = hashlib.sha256()
                read = 0
                with sealed.open(name) as member:
                    for block in iter(lambda: member.read(_READ_CHUNK), b""):
                        read += len(block)
                        if read > size:
                            raise PipelineError(f"Selection batch archive member exceeds its recorded size: {name!r}")
                        hasher.update(block)
                if hasher.hexdigest() != asked[name]:
                    raise PipelineError(f"Selection batch archive bytes differ from the request: {name!r}")
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return {"files": files, "bytes": written_bytes}
