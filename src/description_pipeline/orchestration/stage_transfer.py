"""Seal one completed Windows capture for transport and admit it on the Linux side.

One supported workflow: the Windows host runs freeze/discover/capture, seals the completed
capture into a single closed archive, and the Linux host admits it before generate/verify/
publish.  The manifest binds run, handoff, main assembly, native tool and the closed file
inventory; admission re-hashes every member and re-reads the input report, evidence snapshot
and native stage receipts from the extracted bytes.  Native completion is never final
qualified success: verification still runs on Linux.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from ..build.archive import write_zip
from ..delivery import PIPELINE_ID
from ..io import PipelineError, artifact_path_parts, canonical, confined, digest, file_digest, inventory, read_data
from ..sources.snapshot import verify_snapshot
from ..sources.solidworks.revision import package_inventory
from ..stages import CONTRACT_FILE_SHA256, CONTRACT_SHA256, STAGE_IDS, VIEW_SCHEMA

CAPTURE_ARCHIVE = "native-evidence.zip"
CAPTURE_MANIFEST = "transfer-manifest.json"
TRANSFER_SCHEMA = "solidworks-to-urdf.transfer/v1"

#: The stages the Windows host executes; the remaining stages run on Linux after admission.
NATIVE_STAGE_SCOPE = ("freeze", "discover", "capture")

MAX_TRANSFER_FILES = 100_000
MAX_TRANSFER_BYTES = 16 * 1024**3
MAX_MANIFEST_BYTES = 64 * 1024**2

_REQUIRED_REPORTS = ("reports/input.json", "reports/native-tool.json", "reports/stages.json")
_READ_CHUNK = 1024 * 1024


def _require(condition, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _is_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _validate_native_tool(native_tool) -> None:
    _require(isinstance(native_tool, dict) and native_tool, "Native tool record must be a nonempty object")
    runtime = native_tool.get("runtime")
    _require(
        isinstance(runtime, dict) and runtime.get("role") == "native",
        "Native tool record must declare runtime.role = 'native'",
    )


def _validate_stage_receipts(view, *, run_id: str, handoff_sha256: str) -> None:
    _require(isinstance(view, dict), "Stage receipts must be an object")
    _require(view.get("schema_version") == VIEW_SCHEMA, "Stage receipts use another schema")
    _require(view.get("pipeline_id") == PIPELINE_ID, "Stage receipts belong to another pipeline")
    _require(
        view.get("contract_sha256") == CONTRACT_SHA256 and view.get("contract_file_sha256") == CONTRACT_FILE_SHA256,
        "Stage receipt contract differs from this release",
    )
    _require(view.get("run_id") == run_id, "Stage receipts belong to another run")
    _require(view.get("handoff_sha256") == handoff_sha256, "Stage receipts bind another handoff")
    _require(
        tuple(view.get("execution_scope") or ()) == NATIVE_STAGE_SCOPE,
        "Stage receipts must declare the native execution scope freeze/discover/capture",
    )
    stages = view.get("stages")
    _require(
        isinstance(stages, list)
        and all(isinstance(stage, dict) for stage in stages)
        and len(stages) == len(STAGE_IDS)
        and {stage.get("id") for stage in stages} == set(STAGE_IDS),
        "Stage receipts must list all six engineering stages exactly once",
    )
    by_id = {stage["id"]: stage for stage in stages}
    for stage_id in NATIVE_STAGE_SCOPE:
        stage = by_id[stage_id]
        checks = [*(stage.get("input_qc") or []), *(stage.get("output_qc") or [])]
        _require(stage.get("in_scope") is True, f"Native stage {stage_id} is not in scope")
        _require(stage.get("state") == "completed", f"Native stage {stage_id} is not completed")
        _require(
            checks and all(isinstance(check, dict) for check in checks),
            f"Native stage {stage_id} has no recorded boundary checks",
        )
        _require(
            all(check.get("state") == "passed" for check in checks),
            f"Native stage {stage_id} has a failed or unrun boundary check",
        )
        _require(
            stage.get("checks_total") == len(checks) and stage.get("checks_passed") == stage.get("checks_total"),
            f"Native stage {stage_id} receipts are incomplete",
        )
    for stage_id in STAGE_IDS:
        if stage_id in NATIVE_STAGE_SCOPE:
            continue
        stage = by_id[stage_id]
        _require(stage.get("in_scope") is False, f"Non-native stage {stage_id} is declared in the native scope")
        _require(
            stage.get("state") != "completed",
            f"Non-native stage {stage_id} is claimed complete in the native transfer",
        )


def _validate_input(root: Path, report, main_assembly: str) -> str:
    _require(isinstance(report, dict), "Input report must be an object")
    _require(report.get("passed") is True, "Input report did not pass")
    package_files = report.get("package_files")
    _require(isinstance(package_files, dict) and package_files, "Input report lacks its package file inventory")
    actual = package_inventory(root / "input")
    _require(actual == package_files, "Archived input differs from reports/input.json")
    if main_assembly not in package_files:
        near = sorted(name for name in package_files if name.casefold() == main_assembly.casefold())
        if near:
            raise PipelineError(
                f"The selected main assembly case does not match the capture input: {main_assembly!r} vs {near[0]!r}"
            )
        raise PipelineError(f"The selected main assembly is not part of the capture input: {main_assembly!r}")
    return package_files[main_assembly]


def _validate_evidence(root: Path, main_assembly: str) -> None:
    evidence = Path(root) / "evidence"
    manifest = verify_snapshot(evidence)
    scene = manifest.get("scene")
    _require(
        isinstance(scene, str) and scene in (manifest.get("files") or {}),
        "Evidence snapshot does not bind its scene",
    )
    collection = read_data(confined(evidence, "collection.json"))
    _require(isinstance(collection, dict), "Evidence collection must be an object")
    identity = collection.get("identity") if isinstance(collection.get("identity"), dict) else {}
    _require(
        isinstance(identity.get("dependency_digest"), str) and identity["dependency_digest"],
        "Evidence identity lacks the native dependency digest",
    )
    capture = collection.get("capture") if isinstance(collection.get("capture"), dict) else {}
    _require(capture.get("originals_unchanged") is True, "Evidence does not prove the CAD bytes were unchanged")
    source_hashes = capture.get("source_hashes")
    _require(isinstance(source_hashes, dict) and source_hashes, "Evidence lacks native source hashes")
    assembly = identity.get("assembly")
    _require(isinstance(assembly, str) and assembly.strip(), "Evidence identity lacks the assembly name")
    wanted = {main_assembly.casefold(), Path(main_assembly).stem.casefold()}
    _require(assembly.casefold() in wanted, "Evidence identity names another assembly")


def _validate_capture(root: Path, *, run_id: str, handoff_sha256: str, main_assembly: str, native_tool) -> dict:
    """Re-read a capture root (sealed or freshly extracted) against the transfer contract."""

    _validate_native_tool(native_tool)
    recorded = read_data(confined(root, "reports/native-tool.json"))
    _require(recorded == native_tool, "Native tool report differs from the supplied native tool record")
    _validate_stage_receipts(
        read_data(confined(root, "reports/stages.json")),
        run_id=run_id,
        handoff_sha256=handoff_sha256,
    )
    main_assembly_sha256 = _validate_input(root, read_data(confined(root, "reports/input.json")), main_assembly)
    _validate_evidence(root, main_assembly)
    return {"main_assembly_sha256": main_assembly_sha256}


def seal_capture(
    root: Path,
    archive: Path,
    *,
    run_id: str,
    handoff_sha256: str,
    main_assembly: str,
    native_tool: dict,
) -> dict:
    """Seal a completed Windows capture into one closed, self-describing archive."""

    root = Path(root)
    archive = Path(archive)
    _require(isinstance(run_id, str) and run_id.strip(), "run_id must be a nonempty string")
    _require(_is_sha256(handoff_sha256), "handoff_sha256 must be a sha256 digest")
    _require(isinstance(main_assembly, str) and main_assembly.strip(), "main_assembly must be a relative file name")
    artifact_path_parts(main_assembly)
    _require(not archive.exists(), f"Transfer archive already exists: {archive}")

    files = inventory(root)
    _require(files, "Capture root contains no files to seal")
    _require(len(files) <= MAX_TRANSFER_FILES, "Capture exceeds the transfer file limit")
    total_bytes = sum(confined(root, name).stat().st_size for name in files)
    _require(total_bytes <= MAX_TRANSFER_BYTES, "Capture exceeds the transfer size limit")
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
        main_assembly=main_assembly,
        native_tool=native_tool,
    )
    manifest = {
        "schema_version": TRANSFER_SCHEMA,
        "run_id": run_id,
        "handoff_sha256": handoff_sha256,
        "main_assembly": main_assembly,
        "main_assembly_sha256": validated["main_assembly_sha256"],
        "native_tool": native_tool,
        "native_tool_sha256": digest(native_tool),
        "files": files,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "native_stage_scope": list(NATIVE_STAGE_SCOPE),
    }
    entries = [(name, confined(root, name).read_bytes()) for name in sorted(files)]
    entries.append((CAPTURE_MANIFEST, canonical(manifest)))
    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_zip(archive, entries)
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return manifest


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
            try:
                manifest = json.loads(source.read(manifest_info).decode("utf-8"))
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
            return manifest
    except zipfile.BadZipFile as error:
        raise PipelineError(f"Transfer archive is not a readable zip: {error}") from error


def admit_capture(
    archive: Path,
    destination: Path,
    *,
    expected_run_id: str,
    expected_handoff_sha256: str,
    expected_main_assembly: str,
    expected_native_tool: dict,
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
        _require(manifest.get("main_assembly") == expected_main_assembly, "Transfer binds another main assembly")
        _require(
            manifest.get("native_tool") == expected_native_tool,
            "Transfer carries another native tool record",
        )
        validated = _validate_capture(
            staging,
            run_id=expected_run_id,
            handoff_sha256=expected_handoff_sha256,
            main_assembly=expected_main_assembly,
            native_tool=expected_native_tool,
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
