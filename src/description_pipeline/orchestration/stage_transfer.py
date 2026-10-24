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
from contextlib import suppress
from pathlib import Path

from ..build.archive import ZIP_EPOCH
from ..io import (
    PipelineError,
    artifact_path_parts,
    canonical,
    confined,
    digest,
    file_digest,
    inventory,
    parse_data,
)
from ..native_transfer import (
    _LIVE_REPORTS,
    _READ_CHUNK,
    _REQUIRED_REPORTS,
    CAPTURE_MANIFEST,
    MAX_MANIFEST_BYTES,
    MAX_TRANSFER_BYTES,
    MAX_TRANSFER_FILES,
    NATIVE_STAGE_SCOPE,
    TRANSFER_SCHEMA,
    _allowed_payload,
    _is_sha256,
    _require,
    _validate_capture,
)

CAPTURE_ARCHIVE = "native-evidence.zip"


def seal_capture(
    root: Path,
    archive: Path,
    *,
    run_id: str,
    handoff_sha256: str,
    upload_sha256: str,
    main_assembly: str,
    native_tool: dict,
) -> dict:
    """Seal a completed Windows capture into one closed, self-describing archive."""

    root = Path(root)
    archive = Path(archive)
    _require(isinstance(run_id, str) and run_id.strip(), "run_id must be a nonempty string")
    _require(_is_sha256(handoff_sha256), "handoff_sha256 must be a sha256 digest")
    _require(_is_sha256(upload_sha256), "upload_sha256 must be a sha256 digest")
    _require(isinstance(main_assembly, str) and main_assembly.strip(), "main_assembly must be a relative file name")
    artifact_path_parts(main_assembly)
    _require(not archive.exists(), f"Transfer archive already exists: {archive}")

    files = inventory(root)
    # Sealing outputs (the archive, the optional sidecar manifest) and the live stage view are
    # never payload: Linux rewrites the live view after admission.
    ignored = {CAPTURE_MANIFEST, *_LIVE_REPORTS}
    with suppress(ValueError):
        ignored.add(archive.resolve().relative_to(root.resolve()).as_posix())
    files = {name: checksum for name, checksum in files.items() if name not in ignored}
    _require(files, "Capture root contains no files to seal")
    _require(len(files) <= MAX_TRANSFER_FILES, "Capture exceeds the transfer file limit")
    total_bytes = sum(confined(root, name).stat().st_size for name in files)
    _require(total_bytes <= MAX_TRANSFER_BYTES, "Capture exceeds the transfer size limit")
    stray = sorted(name for name in files if not _allowed_payload(name))
    _require(not stray, f"Capture contains files outside the transfer payload: {stray[:5]}")
    for name in _REQUIRED_REPORTS:
        _require(name in files, f"Capture lacks required report: {name}")
    _require(any(name == "input" or name.startswith("input/") for name in files), "Capture lacks the archived input")
    _require(
        any(name == "evidence" or name.startswith("evidence/") for name in files),
        "Capture lacks the evidence snapshot",
    )

    validated = _validate_capture(
        root,
        run_id=run_id,
        handoff_sha256=handoff_sha256,
        upload_sha256=upload_sha256,
        main_assembly=main_assembly,
        native_tool=native_tool,
    )
    manifest = {
        "schema_version": TRANSFER_SCHEMA,
        "run_id": run_id,
        "handoff_sha256": handoff_sha256,
        "upload_sha256": upload_sha256,
        "main_assembly": main_assembly,
        "main_assembly_sha256": validated["main_assembly_sha256"],
        "native_tool": native_tool,
        "native_tool_sha256": digest(native_tool),
        "files": files,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "native_stage_scope": list(NATIVE_STAGE_SCOPE),
    }
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


def _extract(archive: Path, staging: Path) -> dict:
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
            manifest_bytes = source.read(manifest_info)
            try:
                manifest = parse_data(manifest_bytes, Path(CAPTURE_MANIFEST))
            except (UnicodeDecodeError, ValueError) as error:
                raise PipelineError(f"Transfer manifest is not readable JSON: {error}") from error
            _require(isinstance(manifest, dict), "Transfer manifest must be an object")
            _require(manifest.get("schema_version") == TRANSFER_SCHEMA, "Transfer manifest uses an unknown schema")
            files = manifest.get("files")
            _require(isinstance(files, dict) and files, "Transfer manifest declares no files")
            members = {info.filename for info in infos if info.filename != CAPTURE_MANIFEST}
            missing = sorted(set(files) - members)
            extra = sorted(members - set(files))
            _require(
                not missing and not extra,
                f"Transfer archive inventory differs from its manifest: missing={missing[:5]}, extra={extra[:5]}",
            )
            stray = sorted(name for name in members if not _allowed_payload(name))
            _require(not stray, f"Transfer contains members outside the capture payload: {stray[:5]}")
            _require(
                manifest.get("file_count") == len(files),
                "Transfer manifest file count differs from its inventory",
            )
            _require(
                manifest.get("total_bytes") == sum(source.getinfo(name).file_size for name in files),
                "Transfer manifest size differs from its members",
            )
            _require(
                manifest.get("native_stage_scope") == list(NATIVE_STAGE_SCOPE),
                "Transfer manifest declares another native stage scope",
            )
            _require(_is_sha256(manifest.get("handoff_sha256")), "Transfer manifest handoff digest is not a sha256")
            _require(
                manifest.get("native_tool_sha256") == digest(manifest.get("native_tool")),
                "Transfer manifest native tool digest differs from its record",
            )
            for name, checksum in files.items():
                artifact_path_parts(name)
                _require(_is_sha256(checksum), f"Transfer manifest hash is not a sha256: {name}")

            total_written = 0
            for info in infos:
                if info.filename == CAPTURE_MANIFEST:
                    continue
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
            for name, checksum in files.items():
                _require(
                    file_digest(staging.joinpath(*artifact_path_parts(name))) == checksum,
                    f"Transfer member hash differs from its manifest: {name}",
                )
            # The admitted root must retain the exact canonical manifest bytes for downstream
            # seeding, subject binding and publication.
            (staging / CAPTURE_MANIFEST).write_bytes(manifest_bytes)
            return manifest
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
    expected_prepared_dir: str | None = None,
    expected_prepared_files: dict[str, str] | None = None,
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
        manifest = _extract(archive, staging)
        _require(manifest.get("run_id") == expected_run_id, "Transfer belongs to another run")
        _require(manifest.get("handoff_sha256") == expected_handoff_sha256, "Transfer binds another handoff")
        _require(manifest.get("upload_sha256") == expected_upload_sha256, "Transfer binds another upload")
        _require(manifest.get("main_assembly") == expected_main_assembly, "Transfer binds another main assembly")
        _require(
            manifest.get("native_tool") == expected_native_tool,
            "Transfer carries another native tool record",
        )
        validated = _validate_capture(
            staging,
            run_id=expected_run_id,
            handoff_sha256=expected_handoff_sha256,
            upload_sha256=expected_upload_sha256,
            main_assembly=expected_main_assembly,
            native_tool=expected_native_tool,
            native_prepared_dir=expected_prepared_dir,
            native_prepared_files=expected_prepared_files,
        )
        _require(
            manifest.get("main_assembly_sha256") == validated["main_assembly_sha256"],
            "Transfer main assembly hash differs from the capture input",
        )
        os.replace(staging, destination)
        installed = True
        return manifest
    finally:
        if not installed:
            shutil.rmtree(staging, ignore_errors=True)
