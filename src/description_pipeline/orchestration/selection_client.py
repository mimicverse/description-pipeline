"""Bounded ZIP batches for the selection round transport (Linux side).

The supported flow sends only the files a native dependency request names, and
only when the upload index authorizes exactly those bytes.  This module is that
single building block: it archives exactly ``requested ∩ expected_upload`` with
equal sha256 on both sides and on disk, rejects everything else, and never
walks a folder.  The full-package handoff transport keeps its own route in
``sources.solidworks.handoff``.

Digest vocabulary fixed by the contract:

* ``upload_sha256`` - digest of the full intake index (operator intent);
* ``handoff_sha256`` - digest of the selected files (admitted native package);
* ``selection_sha256`` - digest of the manifest excluding its own seal.

There is no legacy or full-folder route here; the Airflow round loop wires this
helper once the endpoint contract lands.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from ..io import PipelineError, _filesystem_path, artifact_path_parts, confined
from ..sources.solidworks.handoff import MAX_HANDOFF_BYTES, MAX_HANDOFF_FILES

#: Per-batch caps; the shared round validator may tighten these.
MAX_BATCH_FILES = MAX_HANDOFF_FILES
MAX_BATCH_BYTES = MAX_HANDOFF_BYTES
_READ_CHUNK = 1 << 20

__all__ = ["MAX_BATCH_BYTES", "MAX_BATCH_FILES", "prepare_selection_batch"]


def _canonical_index(entries: object, label: str) -> dict[str, str]:
    """One path->sha256 mapping with canonical portable names and no casefold aliases."""

    if not isinstance(entries, dict) or not entries:
        raise PipelineError(f"{label} must be a nonempty path->sha256 mapping")
    canonical: dict[str, str] = {}
    folded: set[str] = set()
    for name, digest in entries.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise PipelineError(f"{label} entries must be path->sha256 strings")
        try:
            parts = artifact_path_parts(name)
        except PipelineError as error:
            raise PipelineError(f"{label} contains an inadmissible path: {error}") from error
        if "/".join(parts) != name:
            raise PipelineError(f"{label} contains a noncanonical path: {name!r}")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise PipelineError(f"{label} entry {name!r} has no lowercase sha256")
        if name.casefold() in folded:
            raise PipelineError(f"{label} contains a casefold duplicate: {name!r}")
        folded.add(name.casefold())
        canonical[name] = digest
    return canonical


def _disk_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(_filesystem_path(path), "rb") as stream:
        for block in iter(lambda: stream.read(_READ_CHUNK), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _write_member(output: zipfile.ZipFile, path: Path, name: str) -> tuple[int, str]:
    """Write one member while hashing the exact stream that enters the archive."""

    hasher = hashlib.sha256()
    written = 0
    with open(_filesystem_path(path), "rb") as stream, output.open(name, "w", force_zip64=True) as member:
        for block in iter(lambda: stream.read(_READ_CHUNK), b""):
            member.write(block)
            hasher.update(block)
            written += len(block)
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
    sha256, must be a real file under ``source`` (symlinks are refused), and its
    bytes are verified before, during and after archiving, including a read-back
    of the sealed archive.  Only the requested names are written; unrelated
    files in the source are never read.  Returns ``{"files": {name: sha256},
    "bytes": n}`` where ``bytes`` is the sum of member sizes (ZIP_STORED, so
    member bytes equal the original file bytes).  A failed call removes the
    partial archive.
    """

    source = Path(source)
    archive = Path(archive)
    if source.is_symlink() or source.is_junction() or not source.is_dir():
        raise PipelineError("Selection batch source must be a real directory")
    upload = _canonical_index(expected_upload, "expected_upload")
    asked = _canonical_index(requested, "requested")
    if len(asked) > MAX_BATCH_FILES:
        raise PipelineError("Selection batch exceeds the file cap")
    outside = sorted(name for name in asked if name not in upload)
    if outside:
        raise PipelineError("Selection batch requests paths outside the upload index", {"paths": outside[:10]})
    mismatch = sorted(name for name in asked if asked[name] != upload[name])
    if mismatch:
        raise PipelineError("Selection batch hashes disagree with the upload index", {"paths": mismatch[:10]})
    if archive.resolve().is_relative_to(source.resolve()):
        raise PipelineError("The selection batch archive must be outside the source tree")

    planned: dict[str, Path] = {}
    total = 0
    for name in sorted(asked):
        path = Path(_filesystem_path(confined(source, name)))
        if path.is_symlink():
            raise PipelineError(f"Symlink is not an immutable artifact: {name!r}")
        if _disk_digest(path) != asked[name]:
            raise PipelineError(f"Selection batch member changed before archiving: {name!r}")
        total += path.stat().st_size
        planned[name] = path
    if total > MAX_BATCH_BYTES:
        raise PipelineError("Selection batch exceeds the byte cap")

    files: dict[str, str] = {}
    written_bytes = 0
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
            for name in sorted(asked):
                size, digest = _write_member(output, planned[name], name)
                if digest != asked[name]:
                    raise PipelineError(f"Selection batch member changed while archiving: {name!r}")
                files[name] = digest
                written_bytes += size
        if written_bytes != total:
            raise PipelineError("Selection batch member changed while archiving: size drift")
        # Any mutation of the source during archiving fails the batch instead of
        # shipping mixed bytes.
        for name in sorted(asked):
            if _disk_digest(planned[name]) != asked[name]:
                raise PipelineError(f"Selection batch member changed while archiving: {name!r}")
        # Prove the sealed bytes are the requested bytes.
        with zipfile.ZipFile(archive) as sealed:
            if sorted(sealed.namelist()) != sorted(asked):
                raise PipelineError("Selection batch archive does not match the requested names")
            for name in sorted(asked):
                hasher = hashlib.sha256()
                with sealed.open(name) as member:
                    for block in iter(lambda: member.read(_READ_CHUNK), b""):
                        hasher.update(block)
                if hasher.hexdigest() != asked[name]:
                    raise PipelineError(f"Selection batch archive bytes differ from the request: {name!r}")
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return {"files": files, "bytes": written_bytes}
