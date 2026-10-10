"""Seal one completed Windows capture for transport and admit it on the Linux side.

One supported workflow: the Windows host runs freeze/discover/capture, seals the completed
capture into a single closed archive, and the Linux host admits it before generate/verify/
publish.  The manifest binds run, handoff, main assembly, native tool and the closed file
inventory; admission re-hashes every member and re-reads the input report, evidence snapshot
and native stage receipts from the extracted bytes.  Native completion is never final
qualified success: verification still runs on Linux.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from contextlib import suppress
from pathlib import Path

from packaging.utils import canonicalize_name

from .. import __version__
from ..build.archive import ZIP_EPOCH
from ..delivery import PIPELINE_ID, TRANSFER_FILES
from ..io import PipelineError, artifact_path_parts, canonical, confined, digest, file_digest, inventory, read_data
from ..runtime import RUNTIME_VERSIONS, required_packages
from ..sources.snapshot import verify_snapshot
from ..sources.solidworks.revision import package_inventory
from ..stages import CONTRACT, CONTRACT_FILE_SHA256, CONTRACT_SHA256, STAGE_IDS, VIEW_SCHEMA, stage_view

CAPTURE_ARCHIVE = "native-evidence.zip"
CAPTURE_MANIFEST = "transfer-manifest.json"
TRANSFER_SCHEMA = "solidworks-to-urdf.transfer/v1"

#: The stages the Windows host executes; the remaining stages run on Linux after admission.
NATIVE_STAGE_SCOPE = ("freeze", "discover", "capture")

MAX_TRANSFER_FILES = 100_000
MAX_TRANSFER_BYTES = 16 * 1024**3
MAX_MANIFEST_BYTES = 64 * 1024**2

#: The immutable native provenance reports; the live ``reports/stages.json`` view is a Linux-side
#: artifact and is deliberately excluded from the transfer payload.
_REQUIRED_REPORTS = ("reports/input.json", "reports/native-tool.json", "reports/native-stages.json")
#: Known mutable receipts stamped by the endpoint; sealing ignores them, never transfers them.
_LIVE_REPORTS = ("reports/stages.json", "reports/run.json")

_TOOL_SCHEMA = "solidworks-to-urdf.tool/v1"
_STAGE_DEFINITIONS = {stage["id"]: stage for stage in CONTRACT["stages"]}
_READ_CHUNK = 1024 * 1024


def _require(condition, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _allowed_payload(name: str) -> bool:
    """Only the archived input, the evidence snapshot and the three reports are payload."""

    return name.startswith(("input/", "evidence/")) or name in _REQUIRED_REPORTS


def _is_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _validate_native_tool(native_tool) -> None:
    """A native tool record must pin its release and the Windows-native runtime closure."""

    _require(isinstance(native_tool, dict) and native_tool, "Native tool record must be a nonempty object")
    _require(native_tool.get("schema_version") == _TOOL_SCHEMA, "Native tool record uses an unknown schema")
    _require(native_tool.get("pipeline_id") == PIPELINE_ID, "Native tool record belongs to another pipeline")
    _require(native_tool.get("version") == __version__, "Native tool record is from another release")
    _require(_is_sha256(native_tool.get("source_sha256")), "Native tool record lacks its source digest")
    runtime = native_tool.get("runtime")
    _require(isinstance(runtime, dict), "Native tool record lacks its runtime closure")
    _require(
        runtime.get("role") == "native",
        "Native tool record must declare runtime.role = 'native'",
    )
    _require(str(runtime.get("system") or "") == "Windows", "Native tool record must come from the Windows host")
    _require(str(runtime.get("python") or "").startswith("3.12"), "Native tool record must pin Python 3.12")
    packages = runtime.get("packages")
    _require(isinstance(packages, dict) and packages, "Native tool record lacks its package pins")
    pins = {canonicalize_name(str(name)): value for name, value in packages.items()}
    for name in required_packages("native"):
        _require(
            pins.get(canonicalize_name(name)) == RUNTIME_VERSIONS.get(name),
            f"Native tool record must pin {name} to the release version",
        )
    _require(
        canonicalize_name("mujoco") not in pins,
        "Native tool record must not include the MuJoCo consumer closure",
    )
    release = Path(__file__).resolve().parent.parent / "tool-release.json"
    if release.is_file():
        identity = read_data(release)
        _require(
            native_tool["source_sha256"] == identity.get("source_sha256"),
            "Native tool record is from another release than this portable runtime",
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
        definition = _STAGE_DEFINITIONS[stage_id]
        stage = by_id[stage_id]
        received_input = stage.get("input_qc")
        received_output = stage.get("output_qc")
        expected_input = [item["id"] for item in definition["input_qc"]]
        expected_output = [item["id"] for item in definition["output_qc"]]
        _require(
            isinstance(received_input, list)
            and all(isinstance(item, dict) for item in received_input)
            and [item.get("id") for item in received_input] == expected_input,
            f"Native stage {stage_id} input checks differ from the release contract",
        )
        _require(
            isinstance(received_output, list)
            and all(isinstance(item, dict) for item in received_output)
            and [item.get("id") for item in received_output] == expected_output,
            f"Native stage {stage_id} output checks differ from the release contract",
        )
        checks = [*received_input, *received_output]
        _require(stage.get("in_scope") is True, f"Native stage {stage_id} is not in scope")
        _require(stage.get("state") == "completed", f"Native stage {stage_id} is not completed")
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
    events = view.get("events")
    _require(
        isinstance(events, list) and events,
        "Native stage receipts must carry the raw protocol events",
    )
    for event in events:
        _require(isinstance(event, dict), "A native protocol event must be an object")
        _require(
            event.get("stage") in NATIVE_STAGE_SCOPE,
            "Native stage receipts must contain only the first three stages' events",
        )
        _require(isinstance(event.get("at"), str) and event["at"], "A native protocol event lacks its timestamp")
        _require(
            event.get("state") in {"running", "completed", "failed"},
            "A native protocol event has an unknown state",
        )
        check = event.get("check")
        if check is not None:
            _require(
                isinstance(check, dict) and check.get("state") in {"passed", "failed"},
                "A native protocol check event is malformed",
            )
    try:
        recomputed = stage_view(
            {
                "run_id": run_id,
                "request": {"handoff_sha256": handoff_sha256},
                "result": {"execution_scope": list(NATIVE_STAGE_SCOPE)},
                "events": events,
            }
        )
    except Exception as error:  # noqa: BLE001 - a receipt that cannot be rebuilt is not evidence
        raise PipelineError(f"Native stage receipts cannot be rebuilt from their events: {error}") from error
    _require(
        _receipt_projection(recomputed) == _receipt_projection(view),
        "Native stage receipts differ from the stage view rebuilt from their own events",
    )


def _receipt_projection(view) -> dict:
    """The native rows and boundary-check states a receipt must actually prove."""

    stages = view.get("stages")
    by_id = {stage.get("id"): stage for stage in stages or [] if isinstance(stage, dict)}
    projection = {}
    for stage_id in NATIVE_STAGE_SCOPE:
        stage = by_id.get(stage_id) or {}
        checks = {}
        for boundary in ("input", "output"):
            for item in stage.get(f"{boundary}_qc") or []:
                if isinstance(item, dict):
                    checks[(boundary, item.get("id"))] = item.get("state")
        projection[stage_id] = {
            "state": stage.get("state"),
            "in_scope": stage.get("in_scope"),
            "checks_passed": stage.get("checks_passed"),
            "checks_total": stage.get("checks_total"),
            "checks": checks,
        }
    return projection


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


def _validate_evidence(root: Path) -> None:
    evidence = Path(root) / "evidence"
    manifest = verify_snapshot(evidence)
    scene = manifest.get("scene")
    _require(
        isinstance(scene, str) and scene in (manifest.get("files") or {}),
        "Evidence snapshot does not bind its scene",
    )
    # ``freeze`` writes its evidence record inside the snapshot's own ``evidence/`` folder.
    collection = read_data(confined(evidence, "evidence/collection.json"))
    _require(isinstance(collection, dict), "Evidence collection must be an object")
    _require(
        collection.get("identity") == manifest.get("identity"),
        "Evidence collection and snapshot identities differ",
    )
    identity = collection.get("identity") if isinstance(collection.get("identity"), dict) else {}
    _require(
        isinstance(identity.get("dependency_digest"), str) and identity["dependency_digest"],
        "Evidence identity lacks the native dependency digest",
    )
    capture = collection.get("capture") if isinstance(collection.get("capture"), dict) else {}
    unchanged = capture.get("originals_unchanged")
    # ``freeze`` records a structured observation (files checked / states recorded) and raises on
    # any change; a boolean or empty claim is not that evidence.
    _require(
        isinstance(unchanged, dict) and int(unchanged.get("files_checked") or 0) >= 1,
        "Evidence does not prove the CAD bytes were unchanged",
    )
    source_hashes = capture.get("source_hashes")
    _require(isinstance(source_hashes, dict) and source_hashes, "Evidence lacks native source hashes")


def _validate_native_discovery(root: Path, *, main_assembly: str, handoff_sha256: str) -> str:
    """Bind the main assembly through the real input source and the native discovery record.

    The packaged discovery record names the entry the reader opened, the handoff it was bound to,
    and the native file inventory it hashed; the archived input must carry the same bytes.
    """

    record = read_data(confined(root, "input/discovery/native-discovery.json"))
    _require(isinstance(record, dict), "Native discovery record must be an object")
    identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    recorded = identity.get("main_assembly")
    _require(isinstance(recorded, str) and recorded, "Native discovery record names no main assembly")
    if recorded != main_assembly:
        hint = " (case differs)" if recorded.casefold() == main_assembly.casefold() else ""
        raise PipelineError(
            f"The native discovery record opened another main assembly{hint}: {recorded!r} vs {main_assembly!r}"
        )
    recorded_handoff = record.get("handoff_sha256")
    _require(_is_sha256(recorded_handoff), "Native discovery record lacks its handoff digest")
    _require(recorded_handoff == handoff_sha256, "The native discovery record is bound to another handoff")
    native_files = record.get("native_files")
    _require(
        isinstance(native_files, dict) and native_files,
        "Native discovery record lacks its native file inventory",
    )
    _require(
        main_assembly in native_files,
        f"The native discovery record does not inventory the main assembly: {main_assembly!r}",
    )
    digest_value = file_digest(confined(root, "input/" + main_assembly))
    _require(
        native_files[main_assembly] == digest_value,
        "The archived main assembly differs from the native file inventory",
    )
    return digest_value


def _validate_capture(root: Path, *, run_id: str, handoff_sha256: str, main_assembly: str, native_tool) -> dict:
    """Re-read a capture root (sealed or freshly extracted) against the transfer contract."""

    _validate_native_tool(native_tool)
    recorded = read_data(confined(root, "reports/native-tool.json"))
    _require(recorded == native_tool, "Native tool report differs from the supplied native tool record")
    _validate_stage_receipts(
        read_data(confined(root, "reports/native-stages.json")),
        run_id=run_id,
        handoff_sha256=handoff_sha256,
    )
    main_assembly_sha256 = _validate_input(root, read_data(confined(root, "reports/input.json")), main_assembly)
    native_sha256 = _validate_native_discovery(root, main_assembly=main_assembly, handoff_sha256=handoff_sha256)
    _require(
        native_sha256 == main_assembly_sha256,
        "The archived main assembly differs between the input report and the native inventory",
    )
    _validate_evidence(root)
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
                manifest = json.loads(manifest_bytes.decode("utf-8"))
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


def verify_transfer(bundle: Path) -> dict | None:
    """Revalidate transferred native provenance inside a delivery bundle.

    Returns ``None`` for a neutral delivery that carries no transfer.  A partial triplet is
    refused; a complete one re-hashes the sealed payload and re-reads the native tool record,
    stage receipts, input/discovery main-assembly binding and evidence snapshot, so a resealed
    but semantically invalid manifest cannot reach publication.
    """

    bundle = Path(bundle)
    present = [name for name in TRANSFER_FILES if (bundle / name).is_file()]
    if not present:
        return None
    missing = sorted(set(TRANSFER_FILES) - set(present))
    _require(not missing, f"Transferred capture provenance is incomplete; missing: {missing}")

    manifest = read_data(confined(bundle, CAPTURE_MANIFEST))
    _require(isinstance(manifest, dict), "Transfer manifest must be an object")
    _require(manifest.get("schema_version") == TRANSFER_SCHEMA, "Transfer manifest uses an unknown schema")
    files = manifest.get("files")
    _require(isinstance(files, dict) and files, "Transfer manifest declares no files")
    _require(
        manifest.get("native_stage_scope") == list(NATIVE_STAGE_SCOPE),
        "Transfer manifest declares another native stage scope",
    )
    _require(manifest.get("file_count") == len(files), "Transfer manifest file count differs from its inventory")
    for name, checksum in files.items():
        artifact_path_parts(name)
        _require(
            _allowed_payload(name),
            f"Transfer manifest lists a member outside the capture payload: {name!r}",
        )
        _require(_is_sha256(checksum), f"Transfer manifest hash is not a sha256: {name}")
        _require(
            file_digest(confined(bundle, name)) == checksum,
            f"Delivered member differs from the transfer manifest: {name}",
        )
    actual_payload = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and _allowed_payload(path.relative_to(bundle).as_posix())
    }
    absent = sorted(set(files) - actual_payload)
    extra = sorted(actual_payload - set(files))
    _require(
        not absent and not extra,
        f"Delivered capture inventory differs from the transfer manifest: missing={absent[:5]}, extra={extra[:5]}",
    )
    _require(
        manifest.get("total_bytes") == sum(confined(bundle, name).stat().st_size for name in files),
        "Transfer manifest size differs from its members",
    )
    _require(
        manifest.get("native_tool_sha256") == digest(manifest.get("native_tool")),
        "Transfer manifest native tool digest differs from its record",
    )
    _validate_native_tool(manifest.get("native_tool"))
    _require(
        read_data(confined(bundle, "reports/native-tool.json")) == manifest.get("native_tool"),
        "Native tool report differs from the transfer manifest",
    )
    run_id = manifest.get("run_id")
    handoff = manifest.get("handoff_sha256")
    main_assembly = manifest.get("main_assembly")
    _require(
        isinstance(run_id, str) and run_id and _is_sha256(handoff) and isinstance(main_assembly, str) and main_assembly,
        "Transfer manifest lacks its run, handoff or main assembly",
    )
    _validate_stage_receipts(
        read_data(confined(bundle, "reports/native-stages.json")),
        run_id=run_id,
        handoff_sha256=handoff,
    )
    main_sha = _validate_input(bundle, read_data(confined(bundle, "reports/input.json")), main_assembly)
    native_sha = _validate_native_discovery(bundle, main_assembly=main_assembly, handoff_sha256=handoff)
    _require(
        main_sha == native_sha == manifest.get("main_assembly_sha256"),
        "The delivered main assembly differs from the transfer manifest",
    )
    _validate_evidence(bundle)
    return {
        "run_id": run_id,
        "handoff_sha256": handoff,
        "main_assembly": main_assembly,
        "files": len(files),
    }
