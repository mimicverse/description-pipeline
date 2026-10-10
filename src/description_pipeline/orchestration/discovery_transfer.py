"""Two-leg discovery transfer between the native Windows host and the Linux pipeline.

Leg 1 (Windows -> Linux) seals the exact raw discovery record into one bounded
ZIP; Linux admits it against the independently supplied run id, sealed selection
manifest and native tool record, then derives the prepared package from the
frozen selected bytes alone (no native runtime, no COM, no CAD session).

Leg 2 (Linux -> Windows) seals only the generated text members of the prepared
package; the Windows host admits them beside its already staged selected CAD,
rebuilds the identical prepared package and hands it to the existing native
capture.  A derivation refusal always keeps its findings and can never be sealed
as a capture request.

Every admission re-checks exact member names and bytes, the run id, the full
sealed selection manifest with its sealed digests, the raw-record digest and
size, and the native tool record.  The final native-evidence.zip and the
independent Linux verification are untouched by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..build.archive import ZIP_EPOCH
from ..io import PipelineError, artifact_path_parts, canonical, confined, digest, inventory
from ..sources.solidworks.discovery import (
    DISCOVERY_FILE,
    DISCOVERY_SCHEMA,
    FINDINGS_FILE,
    NAMESPACE,
    PREPARED_SCHEMA,
    RECORD_DIR,
    REVISION_FILE,
    ROBOT_FILE,
    DiscoverySettings,
    PreparedPackage,
    derive_native_package,
)
from ..sources.solidworks.input import load_package
from ..sources.solidworks.revision import package_inventory
from ..sources.solidworks.selection import SELECTION_SCHEMA, validate_file_index, verify_selection

__all__ = [
    "CAPTURE_REQUEST_ARCHIVE",
    "CAPTURE_REQUEST_SCHEMA",
    "MANIFEST_MEMBER",
    "MAX_RECORD_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_REQUEST_FILES",
    "RAW_RECORD_ARCHIVE",
    "RECORD_BUNDLE_SCHEMA",
    "RECORD_MEMBER",
    "RawRecordAdmission",
    "admit_capture_request",
    "admit_raw_record_bundle",
    "derive_from_raw_bundle",
    "seal_capture_request",
    "seal_raw_record_bundle",
    "stage_capture_request",
]

_READ_CHUNK = 1024 * 1024

#: Fixed names for the raw-record artifact and the generated-members request.
RAW_RECORD_ARCHIVE = "raw-record.zip"
CAPTURE_REQUEST_ARCHIVE = "capture-request.zip"
RECORD_MEMBER = "record.json"
MANIFEST_MEMBER = "manifest.json"
RECORD_BUNDLE_SCHEMA = "solidworks-to-urdf.raw-record/v1"
CAPTURE_REQUEST_SCHEMA = "solidworks-to-urdf.capture-request/v1"

#: Enforced transfer bounds.  The raw record and the generated members are JSON
#: text; CAD and imported sources are never part of either bundle.
MAX_RECORD_BYTES = 512 * 1024 * 1024
MAX_REQUEST_FILES = 10_000
MAX_REQUEST_BYTES = 512 * 1024 * 1024

#: A capture request only carries the actual derive outputs: the generated
#: contract files plus the embedded controlled records (``records/<sha8>_<stem>.txt``).
_EMBEDDED_RECORD_MEMBER = re.compile(rf"{re.escape(RECORD_DIR)}/[0-9a-f]{{8}}_[0-9A-Za-z._-]{{1,48}}\.txt")
_GENERATED_MEMBERS = frozenset({ROBOT_FILE, REVISION_FILE, DISCOVERY_FILE, FINDINGS_FILE})
_REQUIRED_GENERATED = frozenset({ROBOT_FILE, DISCOVERY_FILE, FINDINGS_FILE})

_RECORD_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "main_assembly",
        "main_assembly_sha256",
        "selection",
        "selection_sha256",
        "upload_sha256",
        "handoff_sha256",
        "record_sha256",
        "record_bytes",
        "native_tool",
        "native_tool_sha256",
        "files",
        "file_count",
        "total_bytes",
    }
)
_REQUEST_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "main_assembly",
        "main_assembly_sha256",
        "selection_sha256",
        "upload_sha256",
        "handoff_sha256",
        "record_sha256",
        "record_bytes",
        "native_tool",
        "native_tool_sha256",
        "prepared_sha256",
        "files",
        "file_count",
        "total_bytes",
    }
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _parse_exact_json(raw: bytes, member: str) -> dict:
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise PipelineError(f"{member} is not readable JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise PipelineError(f"{member} must be a JSON object")
    try:
        expected_bytes = canonical(parsed)
    except ValueError as error:
        raise PipelineError(f"{member} carries a value that cannot be canonicalized: {error}") from error
    if expected_bytes != raw:
        raise PipelineError(f"{member} is not in canonical form")
    return parsed


@dataclass(frozen=True)
class RawRecordAdmission:
    """One admitted raw-record bundle: the exact record plus its sealed manifest."""

    record: dict
    manifest: dict


def _selection_bindings(selection: Any) -> dict:
    """Validate one sealed selection manifest via the shared verifier.

    The full intake index is reconstructed from the selected and excluded files,
    and the shared ``verify_selection`` re-seals the manifest from that index so
    ``selection_sha256`` keeps its exact shared definition (digest of the manifest
    without its own digest) instead of any locally invented hash.
    """

    if not isinstance(selection, Mapping):
        raise PipelineError("selection manifest must be an object")
    manifest = dict(selection)
    if manifest.get("schema_version") != SELECTION_SCHEMA:
        raise PipelineError("selection manifest uses an unknown schema")
    files = manifest.get("files")
    excluded = manifest.get("excluded_files")
    if not isinstance(files, Mapping) or not files:
        raise PipelineError("selection manifest must carry its selected files")
    if not isinstance(excluded, Mapping):
        raise PipelineError("selection manifest must carry its excluded files")
    upload = {**dict(excluded), **dict(files)}
    verify_selection(manifest, upload, files, reader_sha256=manifest.get("reader_sha256"))
    for key in ("selection_sha256", "upload_sha256", "handoff_sha256"):
        if not _is_sha256(manifest.get(key)):
            raise PipelineError(f"selection manifest lacks a usable {key}")
    # The digests are distinct concepts, not distinct values: a full closure
    # without exclusions legitimately has upload_sha256 == handoff_sha256.
    selected = validate_file_index(files)
    main = manifest.get("main_assembly")
    if not isinstance(main, str) or not main or not main.lower().endswith(".sldasm") or main not in selected:
        raise PipelineError("selection manifest lacks its selected main assembly")
    return {
        "selection": manifest,
        "files": selected,
        "main_assembly": main,
        "main_assembly_sha256": selected[main],
        "selection_sha256": manifest["selection_sha256"],
        "upload_sha256": manifest["upload_sha256"],
        "handoff_sha256": manifest["handoff_sha256"],
    }


def _recorded_main(record: Mapping[str, Any]) -> str | None:
    identity = record.get("identity")
    value = identity.get("main_assembly") if isinstance(identity, Mapping) else None
    if not _text(value):
        properties = record.get("properties")
        document = properties.get("document") if isinstance(properties, Mapping) else None
        if isinstance(document, Mapping):
            value = document.get(f"{NAMESPACE}.main_assembly")
    if not _text(value):
        return None
    return str(value).replace("\\", "/")


def _record_bindings(record: Any, files: Mapping[str, str], main: str) -> dict:
    """Prove the record's file inventory against the selected staged bytes."""

    if not isinstance(record, Mapping):
        raise PipelineError("native record must be an object")
    record = dict(record)
    if record.get("schema_version") != DISCOVERY_SCHEMA:
        raise PipelineError("native record uses an unknown schema")
    record_files = validate_file_index(record.get("files"))
    for name, checksum in record_files.items():
        if files.get(name) != checksum:
            raise PipelineError(f"recorded file is not part of the selected staged bytes: {name!r}")
    recorded_main = _recorded_main(record)
    if recorded_main is None:
        raise PipelineError("native record does not name its main assembly")
    if recorded_main != main:
        raise PipelineError("native record did not read the selected main assembly")
    if recorded_main not in record_files:
        raise PipelineError("the recorded main assembly is not bound by the record file inventory")
    return record_files


def _require_tool(native_tool: Any) -> None:
    if not isinstance(native_tool, dict) or not native_tool:
        raise PipelineError("native tool record must be a nonempty object")


def _require_run_id(run_id: Any) -> None:
    if not isinstance(run_id, str) or not run_id.strip():
        raise PipelineError("run_id must be a nonempty string")


def _require_generated_members(names: Iterable[str]) -> None:
    """Only the actual derive outputs may travel as capture-request members."""

    present = set(names)
    for name in sorted(present):
        if name in _GENERATED_MEMBERS or _EMBEDDED_RECORD_MEMBER.fullmatch(name):
            continue
        raise PipelineError(f"capture request carries an unexpected generated member: {name!r}")
    missing = sorted(_REQUIRED_GENERATED - present)
    _require(not missing, f"capture request lacks required generated members: {missing}")


def _write_member(target: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    target.writestr(info, data)


def _seal_memory_archive(archive: Path, members: Mapping[str, bytes], manifest_bytes: bytes, *, limit: int) -> None:
    """Write members already held in memory with an exclusive-create archive."""

    archive.parent.mkdir(parents=True, exist_ok=True)
    total = len(manifest_bytes)
    try:
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as target:
            for name in sorted(members):
                data = members[name]
                total += len(data)
                _require(total <= limit, "transfer exceeds the archive size limit while sealing")
                _write_member(target, name, data)
            _write_member(target, MANIFEST_MEMBER, manifest_bytes)
    except FileExistsError as error:
        raise PipelineError(f"Transfer archive already exists: {archive}") from error
    except BaseException:
        archive.unlink(missing_ok=True)
        raise


def _seal_file_archive(
    archive: Path, root: Path, files: Mapping[str, str], manifest_bytes: bytes, *, limit: int
) -> None:
    """Stream package members into an exclusive archive, re-hashing every byte."""

    archive.parent.mkdir(parents=True, exist_ok=True)
    total = len(manifest_bytes)
    try:
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as target:
            for name in sorted(files):
                source = confined(root, name)
                expected_size = source.stat().st_size
                hasher = hashlib.sha256()
                size = 0
                info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o644 << 16
                with open(source, "rb") as handle, target.open(info, "w") as sink:
                    while True:
                        chunk = handle.read(_READ_CHUNK)
                        if not chunk:
                            break
                        size += len(chunk)
                        total += len(chunk)
                        _require(total <= limit, "capture request exceeds the archive size limit while sealing")
                        hasher.update(chunk)
                        sink.write(chunk)
                _require(size == expected_size, f"prepared member size changed while sealing: {name!r}")
                _require(hasher.hexdigest() == files[name], f"prepared member changed while sealing: {name!r}")
            _write_member(target, MANIFEST_MEMBER, manifest_bytes)
    except FileExistsError as error:
        raise PipelineError(f"Transfer archive already exists: {archive}") from error
    except BaseException:
        archive.unlink(missing_ok=True)
        raise


def _sealed(archive: Path, manifest: dict, name: str) -> dict:
    """The seal result: the manifest plus the served artifact metadata."""

    return {
        "manifest": manifest,
        "artifact": {
            "name": name,
            "path": str(archive),
            "sha256": _file_sha256(archive),
            "bytes": archive.stat().st_size,
        },
    }


def _read_archive(
    archive: Path, *, limit_files: int, limit_bytes: int, expected_names: frozenset[str] | None = None
) -> dict[str, bytes]:
    """Read one strict transfer archive: exact members, canonical names, bounded bytes."""

    if not archive.is_file():
        raise PipelineError(f"Transfer archive is missing: {archive}")
    try:
        with zipfile.ZipFile(archive) as source:
            infos = source.infolist()
            _require(bool(infos), "Transfer archive is empty")
            seen: set[str] = set()
            for info in infos:
                _require(not info.is_dir(), f"Transfer member is a directory: {info.filename!r}")
                _require(
                    not (info.flag_bits & 0x1),
                    f"Transfer member is encrypted: {info.filename!r}",
                )
                _require(
                    info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    f"Transfer member uses an unsupported compression: {info.filename!r}",
                )
                parts = artifact_path_parts(info.filename)
                _require(
                    info.filename == "/".join(parts),
                    f"Transfer member name is not canonical: {info.filename!r}",
                )
                _require(info.filename not in seen, f"Transfer member repeats: {info.filename!r}")
                seen.add(info.filename)
            if expected_names is not None:
                _require(
                    seen == set(expected_names),
                    f"Transfer member set is not exactly {sorted(expected_names)}",
                )
            _require(len(infos) <= limit_files, "Transfer archive exceeds the member limit")
            _require(
                sum(info.file_size for info in infos) <= limit_bytes,
                "Transfer archive exceeds the size limit",
            )
            members: dict[str, bytes] = {}
            total = 0
            for info in infos:
                chunks: list[bytes] = []
                with source.open(info) as handle:
                    while True:
                        chunk = handle.read(_READ_CHUNK)
                        if not chunk:
                            break
                        total += len(chunk)
                        _require(total <= limit_bytes, "Transfer archive exceeds the size limit while reading")
                        chunks.append(chunk)
                data = b"".join(chunks)
                _require(
                    len(data) == info.file_size,
                    f"Transfer member size differs from its declaration: {info.filename!r}",
                )
                members[info.filename] = data
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError) as error:
        raise PipelineError(f"Transfer archive is not a readable ZIP: {error}") from error
    return members


def seal_raw_record_bundle(record: dict, archive: Path, *, run_id: str, selection: dict, native_tool: dict) -> dict:
    """Seal the exact native record for transport to the Linux side (Windows host).

    Members are exactly ``{record.json, manifest.json}``; the record member holds
    ``canonical(record)`` bytes.  Returns ``{"manifest": ..., "artifact": {...}}``
    where the artifact metadata names the ZIP to serve and its digest and size.
    """

    archive = Path(archive)
    _require_run_id(run_id)
    bindings = _selection_bindings(selection)
    _record_bindings(record, bindings["files"], bindings["main_assembly"])
    _require_tool(native_tool)
    record_bytes = canonical(record)
    _require(len(record_bytes) <= MAX_RECORD_BYTES, "Raw native record exceeds the record transfer limit")
    record_sha256 = hashlib.sha256(record_bytes).hexdigest()
    manifest = {
        "schema_version": RECORD_BUNDLE_SCHEMA,
        "run_id": run_id,
        "main_assembly": bindings["main_assembly"],
        "main_assembly_sha256": bindings["main_assembly_sha256"],
        "selection": bindings["selection"],
        "selection_sha256": bindings["selection_sha256"],
        "upload_sha256": bindings["upload_sha256"],
        "handoff_sha256": bindings["handoff_sha256"],
        "record_sha256": record_sha256,
        "record_bytes": len(record_bytes),
        "native_tool": native_tool,
        "native_tool_sha256": digest(native_tool),
        "files": {RECORD_MEMBER: record_sha256},
        "file_count": 1,
        "total_bytes": len(record_bytes),
    }
    _seal_memory_archive(archive, {RECORD_MEMBER: record_bytes}, canonical(manifest), limit=MAX_RECORD_BYTES)
    return _sealed(archive, manifest, RAW_RECORD_ARCHIVE)


def admit_raw_record_bundle(
    bundle: Path,
    *,
    expected_run_id: str,
    expected_selection: dict,
    expected_native_tool: dict,
    expected_archive_sha256: str | None = None,
) -> RawRecordAdmission:
    """Admit the raw-record bundle against independently supplied expectations (Linux)."""

    bundle = Path(bundle)
    _require_run_id(expected_run_id)
    _require_tool(expected_native_tool)
    _require(bundle.is_file(), f"Raw record bundle is missing: {bundle}")
    if expected_archive_sha256 is not None:
        _require(_is_sha256(expected_archive_sha256), "expected archive digest must be a sha256")
        _require(
            _file_sha256(bundle) == expected_archive_sha256,
            "Raw record bundle bytes differ from the transferred digest",
        )
    members = _read_archive(
        bundle,
        limit_files=2,
        limit_bytes=MAX_RECORD_BYTES,
        expected_names=frozenset({RECORD_MEMBER, MANIFEST_MEMBER}),
    )
    manifest = _parse_exact_json(members[MANIFEST_MEMBER], MANIFEST_MEMBER)
    _require(set(manifest) == _RECORD_MANIFEST_KEYS, "raw record manifest has an unexpected field set")
    _require(manifest.get("schema_version") == RECORD_BUNDLE_SCHEMA, "raw record manifest uses an unknown schema")
    carried = _selection_bindings(manifest.get("selection"))
    expected = _selection_bindings(expected_selection)
    _require(manifest["run_id"] == expected_run_id, "raw record bundle belongs to another run")
    _require(carried["selection"] == expected["selection"], "raw record bundle carries another selection manifest")
    _require(
        manifest["main_assembly"] == expected["main_assembly"]
        and manifest["main_assembly_sha256"] == expected["main_assembly_sha256"],
        "raw record bundle names another main assembly",
    )
    _require(
        manifest["selection_sha256"] == expected["selection_sha256"]
        and manifest["upload_sha256"] == expected["upload_sha256"]
        and manifest["handoff_sha256"] == expected["handoff_sha256"],
        "raw record bundle carries other selection digests",
    )
    _require(
        manifest["native_tool"] == expected_native_tool
        and manifest["native_tool_sha256"] == digest(expected_native_tool),
        "raw record bundle carries another native tool record",
    )
    raw_record = members[RECORD_MEMBER]
    record = _parse_exact_json(raw_record, RECORD_MEMBER)
    _require(
        manifest["record_sha256"] == hashlib.sha256(raw_record).hexdigest(),
        "raw record bytes do not match record_sha256",
    )
    _require(manifest["record_bytes"] == len(raw_record), "raw record size differs from its manifest")
    _require(
        manifest["files"] == {RECORD_MEMBER: manifest["record_sha256"]},
        "raw record bundle inventory is inconsistent",
    )
    _require(
        manifest["file_count"] == 1 and manifest["total_bytes"] == len(raw_record),
        "raw record bundle totals are inconsistent",
    )
    _record_bindings(record, expected["files"], expected["main_assembly"])
    return RawRecordAdmission(record=record, manifest=manifest)


def derive_from_raw_bundle(
    bundle: Path,
    frozen_source: Path,
    output: Path,
    *,
    expected_run_id: str,
    expected_selection: dict,
    expected_native_tool: dict,
    expected_archive_sha256: str | None = None,
    settings: DiscoverySettings | None = None,
    configuration: str | None = None,
    assembly: str | None = None,
    on_event: Any = None,
) -> PreparedPackage:
    """Admit the bundle, then derive the prepared package from the frozen selected CAD (Linux)."""

    admission = admit_raw_record_bundle(
        bundle,
        expected_run_id=expected_run_id,
        expected_selection=expected_selection,
        expected_native_tool=expected_native_tool,
        expected_archive_sha256=expected_archive_sha256,
    )
    expected = _selection_bindings(expected_selection)
    frozen = Path(frozen_source)
    _require(
        package_inventory(frozen) == dict(expected["files"]),
        "Frozen source differs from the selected staged files",
    )
    return derive_native_package(
        frozen,
        Path(output),
        expected_run_id,
        admission.record,
        settings=settings,
        configuration=configuration,
        assembly=assembly,
        on_event=on_event,
    )


def seal_capture_request(
    prepared: PreparedPackage, archive: Path, *, record: dict, selection: dict, native_tool: dict
) -> dict:
    """Seal the generated text members of one passed derivation (Linux host).

    Only the actual derive outputs outside the selected staged files are
    transported (the generated contract files and the embedded controlled
    records); the CAD tree and any imported sources stay on the Windows side.
    A failed derivation retains its findings and is refused here, so a refusal
    can never become a valid capture request.  Returns
    ``{"manifest": ..., "artifact": {...}}``.
    """

    archive = Path(archive)
    _require(
        getattr(prepared, "passed", None) is True,
        "the derivation did not pass; its findings must be resolved before a capture request is sealed",
    )
    bindings = _selection_bindings(selection)
    _record_bindings(record, bindings["files"], bindings["main_assembly"])
    _require_tool(native_tool)
    package = Path(prepared.package)
    _require(package.is_dir(), "the derived prepared package is missing")
    package_files = inventory(package)
    _require(
        digest(package_files) == prepared.prepared_sha256,
        "the prepared package changed after derivation",
    )
    _require(
        prepared.handoff_sha256 == bindings["handoff_sha256"],
        "the prepared package is bound to another selection handoff",
    )
    native = {name: checksum for name, checksum in package_files.items() if name in bindings["files"]}
    _require(native == dict(bindings["files"]), "the prepared CAD tree differs from the selected staged files")
    members = {name: checksum for name, checksum in package_files.items() if name not in bindings["files"]}
    _require_generated_members(members)
    _require(len(members) <= MAX_REQUEST_FILES, "capture request exceeds the member limit")
    total = sum(confined(package, name).stat().st_size for name in members)
    _require(total <= MAX_REQUEST_BYTES, "capture request exceeds the transfer size limit")
    record_bytes = canonical(record)
    manifest = {
        "schema_version": CAPTURE_REQUEST_SCHEMA,
        "run_id": prepared.run_id,
        "main_assembly": bindings["main_assembly"],
        "main_assembly_sha256": bindings["main_assembly_sha256"],
        "selection_sha256": bindings["selection_sha256"],
        "upload_sha256": bindings["upload_sha256"],
        "handoff_sha256": bindings["handoff_sha256"],
        "record_sha256": hashlib.sha256(record_bytes).hexdigest(),
        "record_bytes": len(record_bytes),
        "native_tool": native_tool,
        "native_tool_sha256": digest(native_tool),
        "prepared_sha256": prepared.prepared_sha256,
        "files": dict(sorted(members.items())),
        "file_count": len(members),
        "total_bytes": total,
    }
    _seal_file_archive(archive, package, members, canonical(manifest), limit=MAX_REQUEST_BYTES)
    return _sealed(archive, manifest, CAPTURE_REQUEST_ARCHIVE)


def _admit_capture(
    archive: Path,
    *,
    expected_run_id: str,
    expected_selection: dict,
    expected_record: dict,
    expected_native_tool: dict,
    expected_prepared_sha256: str,
    expected_archive_sha256: str | None,
) -> tuple[dict, dict[str, bytes]]:
    """Validate one capture request against independently supplied expectations.

    ``expected_prepared_sha256`` is the caller-requested prepared identity (the
    authenticated Linux request's ``X-Prepared-SHA256`` binding).  It is verified
    against the actual rebuilt combined inventory on staging, but it is not an
    independent Linux-side qualification; the raw-record and selected-source
    byte checks plus the existing capture and verification remain the proof.
    """

    archive = Path(archive)
    _require_run_id(expected_run_id)
    _require_tool(expected_native_tool)
    _require(archive.is_file(), f"Capture request archive is missing: {archive}")
    _require(
        isinstance(expected_record, Mapping),
        "expected raw record must be the admitted record object",
    )
    expected_record = dict(expected_record)
    _require(_is_sha256(expected_prepared_sha256), "expected prepared digest must be a sha256")
    expected = _selection_bindings(expected_selection)
    _record_bindings(expected_record, expected["files"], expected["main_assembly"])
    if expected_archive_sha256 is not None:
        _require(_is_sha256(expected_archive_sha256), "expected archive digest must be a sha256")
        _require(
            _file_sha256(archive) == expected_archive_sha256,
            "Capture request bytes differ from the transferred digest",
        )
    members = _read_archive(archive, limit_files=MAX_REQUEST_FILES + 1, limit_bytes=MAX_REQUEST_BYTES)
    _require(MANIFEST_MEMBER in members, "capture request lacks its manifest")
    manifest = _parse_exact_json(members[MANIFEST_MEMBER], MANIFEST_MEMBER)
    _require(set(manifest) == _REQUEST_MANIFEST_KEYS, "capture request manifest has an unexpected field set")
    _require(manifest.get("schema_version") == CAPTURE_REQUEST_SCHEMA, "capture request uses an unknown schema")
    files = validate_file_index(manifest.get("files"))
    _require(MANIFEST_MEMBER not in files, "capture request manifest may not list itself")
    _require(
        set(members) == set(files) | {MANIFEST_MEMBER},
        "capture request member set differs from its manifest",
    )
    _require_generated_members(files)
    for name, checksum in files.items():
        _require(
            hashlib.sha256(members[name]).hexdigest() == checksum,
            f"capture request member differs from its digest: {name!r}",
        )
    _require(manifest["run_id"] == expected_run_id, "capture request belongs to another run")
    _require(
        manifest["selection_sha256"] == expected["selection_sha256"]
        and manifest["upload_sha256"] == expected["upload_sha256"]
        and manifest["handoff_sha256"] == expected["handoff_sha256"],
        "capture request carries other selection digests",
    )
    _require(
        manifest["main_assembly"] == expected["main_assembly"]
        and manifest["main_assembly_sha256"] == expected["main_assembly_sha256"],
        "capture request names another main assembly",
    )
    record_bytes = canonical(expected_record)
    _require(
        manifest["record_sha256"] == hashlib.sha256(record_bytes).hexdigest()
        and manifest["record_bytes"] == len(record_bytes),
        "capture request is bound to another raw record",
    )
    _require(
        manifest["native_tool"] == expected_native_tool
        and manifest["native_tool_sha256"] == digest(expected_native_tool),
        "capture request carries another native tool record",
    )
    _require(
        manifest["prepared_sha256"] == expected_prepared_sha256,
        "capture request prepared identity differs from the expected one",
    )
    _require(
        manifest["file_count"] == len(files) and manifest["total_bytes"] == sum(len(members[name]) for name in files),
        "capture request totals are inconsistent",
    )
    payload = {name: members[name] for name in files}
    return manifest, payload


def admit_capture_request(
    archive: Path,
    staging: Path,
    *,
    expected_run_id: str,
    expected_selection: dict,
    expected_record: dict,
    expected_native_tool: dict,
    expected_prepared_sha256: str,
    expected_archive_sha256: str | None = None,
) -> dict:
    """Validate the capture request and extract its generated members (Windows host).

    Members are written exclusively into a freshly created staging directory and
    the directory is removed again if any write fails.  ``expected_prepared_sha256``
    is the caller-requested prepared identity from the authenticated Linux
    request (recorded as a transport binding, not an independent qualification).
    Returns the manifest.
    """

    staging = Path(staging)
    manifest, payload = _admit_capture(
        archive,
        expected_run_id=expected_run_id,
        expected_selection=expected_selection,
        expected_record=expected_record,
        expected_native_tool=expected_native_tool,
        expected_prepared_sha256=expected_prepared_sha256,
        expected_archive_sha256=expected_archive_sha256,
    )
    if staging.exists():
        raise PipelineError(f"Capture request staging already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        for name, data in sorted(payload.items()):
            target = confined(staging, name, exists=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(data)
            _require(
                hashlib.sha256(target.read_bytes()).hexdigest() == manifest["files"][name],
                f"capture request member changed while staging: {name!r}",
            )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _build_prepared(destination: Path, manifest: dict, expected_record: Mapping[str, Any]) -> PreparedPackage:
    """Rebuild the prepared package identity from the staged generated members."""

    discovery_path = destination / DISCOVERY_FILE
    _require(discovery_path.is_file(), "the capture request carries no native discovery evidence")
    try:
        payload = json.loads(discovery_path.read_bytes())
        findings_document = json.loads((destination / FINDINGS_FILE).read_bytes())
    except (OSError, ValueError) as error:
        raise PipelineError(f"prepared discovery evidence is unreadable: {error}") from error
    _require(isinstance(payload, dict), "prepared discovery evidence must be an object")
    _require(payload.get("schema_version") == DISCOVERY_SCHEMA, "prepared discovery evidence uses an unknown schema")
    _require(payload.get("run_id") == manifest["run_id"], "prepared discovery belongs to another run")
    _require(
        payload.get("handoff_sha256") == manifest["handoff_sha256"],
        "prepared discovery is bound to another handoff",
    )
    _require(payload.get("raw") == dict(expected_record), "prepared discovery carries another raw record")
    _require(
        isinstance(payload.get("native_files"), dict) and digest(payload["native_files"]) == manifest["handoff_sha256"],
        "prepared discovery carries another selected file inventory",
    )
    identity = payload.get("identity") if isinstance(payload.get("identity"), Mapping) else {}
    _require(
        identity.get("main_assembly") == manifest["main_assembly"], "prepared discovery names another main assembly"
    )
    findings = findings_document.get("findings") if isinstance(findings_document, Mapping) else None
    _require(
        isinstance(findings, list) and all(isinstance(item, dict) for item in findings),
        "prepared findings are unreadable",
    )
    _require(payload.get("findings") == findings, "prepared findings disagree with the discovery evidence")
    _require(not any(item.get("blocking", True) for item in findings), "prepared discovery contains blocking findings")
    revision_path = destination / REVISION_FILE
    revision_sha256 = hashlib.sha256(revision_path.read_bytes()).hexdigest() if revision_path.is_file() else ""
    return PreparedPackage(
        schema_version=PREPARED_SCHEMA,
        run_id=manifest["run_id"],
        package=destination,
        handoff_sha256=manifest["handoff_sha256"],
        prepared_sha256=manifest["prepared_sha256"],
        revision_sha256=revision_sha256,
        hardware_id=identity.get("hardware_id"),
        revision=identity.get("revision"),
        source=load_package(destination),
        discovery_path=discovery_path,
        discovery_sha256=hashlib.sha256(discovery_path.read_bytes()).hexdigest(),
        findings=tuple(findings),
        passed=True,
    )


def stage_capture_request(
    archive: Path,
    destination: Path,
    *,
    expected_run_id: str,
    expected_selection: dict,
    expected_record: dict,
    expected_native_tool: dict,
    expected_prepared_sha256: str,
    expected_archive_sha256: str | None = None,
) -> PreparedPackage:
    """Rebuild the prepared package beside the staged selected CAD (Windows host).

    ``destination`` is the owned prepared directory already holding the staged
    selected CAD tree; only the generated text members are written into it,
    exclusively and digest-checked, and the full package digest must equal the
    caller-requested prepared identity (``X-Prepared-SHA256`` recorded by the
    authenticated Linux request; not an independent Linux-side qualification)
    before a PreparedPackage is returned.
    """

    destination = Path(destination)
    _require(destination.is_dir(), f"prepared destination is missing: {destination}")
    manifest, payload = _admit_capture(
        archive,
        expected_run_id=expected_run_id,
        expected_selection=expected_selection,
        expected_record=expected_record,
        expected_native_tool=expected_native_tool,
        expected_prepared_sha256=expected_prepared_sha256,
        expected_archive_sha256=expected_archive_sha256,
    )
    written: list[str] = []
    try:
        for name, data in sorted(payload.items()):
            target = confined(destination, name, exists=False)
            if target.exists():
                raise PipelineError(f"the prepared destination already contains {name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(data)
            written.append(name)
            _require(
                hashlib.sha256(data).hexdigest() == manifest["files"][name],
                f"capture request member changed while staging: {name!r}",
            )
        _require(
            digest(inventory(destination)) == manifest["prepared_sha256"],
            "the rebuilt prepared package differs from the derived prepared identity",
        )
        return _build_prepared(destination, manifest, expected_record)
    except BaseException:
        for name in written:
            with suppress(OSError, PipelineError):
                confined(destination, name).unlink()
        raise
