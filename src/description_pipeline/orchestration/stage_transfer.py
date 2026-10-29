"""Seal, safely extract, and atomically admit one native capture archive.

Receipt semantics live in native_transfer and are shared by every on-disk and
in-memory consumer. This module owns only transport and the filesystem commit.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from ..build.archive import ZIP_EPOCH
from ..io import (
    PipelineError,
    _physical_path,
    artifact_path_parts,
    canonical,
    confined,
    read_data,
)
from .. import native_transfer
from ..native_transfer import CAPTURE_MANIFEST, MAX_MANIFEST_BYTES, MAX_TRANSFER_BYTES, MAX_TRANSFER_FILES


CAPTURE_ARCHIVE = "native-evidence.zip"
_READ_CHUNK = 1024 * 1024


def _require(condition, message):
    if not condition:
        raise PipelineError(message)


def seal_capture(
    root: Path,
    archive: Path,
    *,
    run_id: str,
    handoff_sha256: str,
    upload_sha256: str,
    main_assembly: str,
    native_tool: dict,
    upstream_artifacts,
    execution,
) -> dict:
    """Seal a completed Windows capture into one closed, self-describing archive."""

    root = Path(_physical_path(root))
    archive = Path(_physical_path(archive))
    _require(not archive.exists(), f"Transfer archive already exists: {archive}")
    manifest = native_transfer.capture_manifest(
        root,
        run_id=run_id,
        handoff_sha256=handoff_sha256,
        upload_sha256=upload_sha256,
        main_assembly=main_assembly,
        native_tool=native_tool,
        upstream_artifacts=upstream_artifacts,
        execution=execution,
    )
    files = manifest["files"]
    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_streaming_zip(archive, root, files, canonical(manifest))
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return manifest


def _write_streaming_zip(archive: Path, root: Path, files: dict[str, str], manifest_bytes: bytes) -> None:
    """Write the payload with fixed metadata, streaming member bytes and re-hashing them.

    The inventory is verified again while streaming, so a capture that changes between the
    inventory scan and the write is refused instead of being sealed inconsistently.
    """

    archive = Path(_physical_path(archive))
    root = Path(_physical_path(root))
    total = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as target:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o755 if name.endswith(".sh") else 0o644) << 16
            source_path = confined(root, name)
            expected_size = source_path.stat().st_size
            hasher = hashlib.sha256()
            size = 0
            with open(source_path, "rb") as source, target.open(info, "w") as sink:
                while True:
                    chunk = source.read(_READ_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    _require(
                        total <= MAX_TRANSFER_BYTES,
                        "Capture exceeds the transfer size limit while sealing",
                    )
                    hasher.update(chunk)
                    sink.write(chunk)
            _require(size == expected_size, f"Capture member size changed while sealing: {name}")
            _require(hasher.hexdigest() == files[name], f"Capture member changed while sealing: {name}")
        manifest_info = zipfile.ZipInfo(CAPTURE_MANIFEST, date_time=ZIP_EPOCH)
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.create_system = 3
        manifest_info.external_attr = 0o644 << 16
        target.writestr(manifest_info, manifest_bytes)


def _extract(archive: Path, staging: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as source:
            infos = source.infolist()
            _require(infos, "Transfer archive is empty")
            _require(len(infos) <= MAX_TRANSFER_FILES + 1, "Transfer archive exceeds the file limit")
            seen: set[str] = set()
            for info in infos:
                _require(not info.is_dir(), f"Transfer member is a directory: {info.filename!r}")
                parts = artifact_path_parts(info.filename)
                _require(
                    info.filename == "/".join(parts),
                    f"Transfer member name is not canonical: {info.filename!r}",
                )
                _require(
                    info.filename.casefold() not in seen,
                    f"Duplicate transfer member: {info.filename!r}",
                )
                seen.add(info.filename.casefold())
                mode = info.external_attr >> 16
                file_type = mode & 0o170000
                if info.create_system == 3 and file_type not in {0, stat.S_IFREG}:
                    _require(
                        False,
                        f"Transfer member is not a regular file: {info.filename!r}",
                    )
            _require(
                sum(info.file_size for info in infos) <= MAX_TRANSFER_BYTES,
                "Transfer archive exceeds the size limit",
            )
            names = source.namelist()
            _require(CAPTURE_MANIFEST in names, "Transfer archive lacks transfer-manifest.json")
            manifest_info = source.getinfo(CAPTURE_MANIFEST)
            _require(manifest_info.file_size <= MAX_MANIFEST_BYTES, "Transfer manifest exceeds the size limit")
            total_written = 0
            for info in infos:
                target = staging.joinpath(*artifact_path_parts(info.filename))
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with source.open(info) as stream, open(target, "wb") as sink:
                    while True:
                        chunk = stream.read(_READ_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        total_written += len(chunk)
                        _require(
                            total_written <= MAX_TRANSFER_BYTES,
                            "Transfer archive exceeds the size limit while extracting",
                        )
                        sink.write(chunk)
                _require(written == info.file_size, f"Transfer member size changed while reading: {info.filename}")
    except zipfile.BadZipFile as error:
        raise PipelineError(f"Transfer archive is not a readable zip: {error}") from error


def admit_capture(
    archive: Path,
    destination: Path,
    *,
    expected_run_id: str,
    expected_handoff_sha256: str,
    expected_upload_sha256: str,
    expected_main_assembly: str,
    expected_native_tool: dict,
    expected_upstream_artifacts,
    expected_execution,
    expected_prepared_dir: str | None = None,
) -> dict:
    """Admit one transferred capture: validate in private staging, then install atomically."""

    archive = Path(archive)
    destination = Path(destination)
    _require(archive.is_file(), f"Transfer archive is missing: {archive}")
    _require(not destination.exists(), f"Transfer destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="transfer-", dir=destination.parent))
    installed = False
    try:
        _extract(archive, staging)
        native_transfer.verify_transfer(
            staging,
            expected_run_id=expected_run_id,
            expected_handoff_sha256=expected_handoff_sha256,
            expected_upload_sha256=expected_upload_sha256,
            expected_main_assembly=expected_main_assembly,
            expected_native_tool=expected_native_tool,
            native_prepared_dir=expected_prepared_dir,
            expected_upstream_artifacts=expected_upstream_artifacts,
            expected_execution=expected_execution,
        )
        manifest = read_data(staging / CAPTURE_MANIFEST)
        os.replace(staging, destination)
        installed = True
        return manifest
    finally:
        if not installed:
            shutil.rmtree(staging, ignore_errors=True)
