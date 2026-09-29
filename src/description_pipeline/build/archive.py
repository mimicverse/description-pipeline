"""Deterministic archives for release artifacts.

A release is only as verifiable as its bytes: with the same commit and the same lock file, two
independent builds must produce identical artifacts so a reviewer can rebuild and compare instead of
trusting an upload.  The builders upstream are not deterministic — setuptools stamps wheels with the
time of the build, and ``tar`` records ownership and mtimes for every sdist member — so every
distribution artifact is rewritten here with fixed metadata before it is published or packed into an
offline bundle.

The rewrite only changes container metadata.  Member names, order and contents (including the wheel
``RECORD``) are preserved, so the normalized artifacts install and verify exactly like the originals.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

#: ZIP cannot encode a timestamp before 1980.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: Tarball and gzip timestamps are normalized to the Unix epoch.
TAR_EPOCH = 0


def write_zip(path: Path, entries: Iterable[tuple[str, bytes]]) -> None:
    """Write ``entries`` as a ZIP with sorted names and fixed metadata."""

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries, key=lambda item: item[0]):
            info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            # Mark the entry as coming from Unix so extraction uses the same predictable mode.
            info.create_system = 3
            mode = 0o755 if name.endswith(".sh") else 0o644
            info.external_attr = mode << 16
            archive.writestr(info, data)


def normalize_zip(path: Path) -> None:
    """Rewrite an existing ZIP (a built wheel) with fixed metadata."""

    with zipfile.ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info)) for info in archive.infolist() if not info.is_dir()]
    write_zip(path, entries)


def normalize_sdist(path: Path) -> None:
    """Rewrite a ``.tar.gz`` sdist with fixed ownership, modes and timestamps."""

    with tarfile.open(path, "r:gz") as archive:
        members = []
        for info in archive.getmembers():
            payload_stream = archive.extractfile(info) if info.isreg() else None
            members.append((info, payload_stream.read() if payload_stream is not None else None))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for info, data in sorted(members, key=lambda item: item[0].name):
            clone = tarfile.TarInfo(info.name)
            clone.type = info.type
            clone.linkname = info.linkname
            clone.size = len(data) if data is not None else 0
            # Keep only the executable bit: release archives are read, not shared, by their owner.
            clone.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
            clone.mtime = TAR_EPOCH
            clone.uid = clone.gid = 0
            clone.uname = clone.gname = ""
            archive.addfile(clone, io.BytesIO(data) if data is not None else None)
    # ``filename=""`` keeps the original file name out of the gzip header; without it ``GzipFile``
    # derives it from the open file object and the artifact name leaks into the bytes.
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=TAR_EPOCH) as compressed,
    ):
        compressed.write(buffer.getvalue())
