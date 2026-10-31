"""Independent native capture receipt and payload semantics.

The archive producer, Linux admission, and publication verifier use this one
validator over immutable receipt bytes and actual payload files. No scheduler,
platform store, CAD generator, or derived robot model is consulted.
"""

from __future__ import annotations

import hashlib
import ntpath
import uuid
from datetime import datetime
from pathlib import Path

from packaging.utils import canonicalize_name

from . import __version__
from .delivery import PIPELINE_ID, TRANSFER_FILES, CAPTURE_MANIFEST
from .interface_records import record_archive_path, validate_source_record
from .io import (
    PipelineError,
    _physical_path,
    artifact_path_parts,
    confined,
    digest,
    file_digest,
    inventory,
    parse_data,
    read_data,
)
from .request_evidence import read_request_evidence
from .runtime import RUNTIME_HOSTS, RUNTIME_VERSIONS, python_version_is_supported, required_packages
from .schemas import TOOL_SCHEMA, NATIVE_DISCOVERY_SCHEMA, NATIVE_CAPTURE_FACTS
from .sources.snapshot import verify_snapshot
from .sources.solidworks.capture_projection import (
    load_native_capture_observation,
    validate_native_capture_plan,
)
from .sources.solidworks.handoff import MAX_HANDOFF_BYTES, MAX_HANDOFF_FILES
from .sources.solidworks.revision import package_inventory
from .stages import CONTRACT, CONTRACT_FILE_SHA256, CONTRACT_SHA256, STAGE_IDS, VIEW_SCHEMA, stage_view

CAPTURE_TRANSFER_SCHEMA = "solidworks-to-urdf.capture-transfer/v4"


NATIVE_CAPTURE_SCOPE = ("capture",)


MAX_TRANSFER_FILES = MAX_HANDOFF_FILES


MAX_TRANSFER_BYTES = MAX_HANDOFF_BYTES


MAX_MANIFEST_BYTES = 64 * 1024**2


_REQUIRED_REPORTS = (
    "input/handoff.json",
    "input/source.json",
    "reports/native-tool.json",
    "reports/native-stages.json",
)


_LIVE_REPORTS = ("reports/stages.json",)


_TOOL_SCHEMA = TOOL_SCHEMA


_STAGE_DEFINITIONS = {stage["id"]: stage for stage in CONTRACT["stages"]}




def _require(condition, message: str) -> None:
    if not condition:
        raise PipelineError(message)


_CANONICAL_EVIDENCE = frozenset(
    {
        "evidence/manifest.json",
        "evidence/native-runtime.json",
        "evidence/raw/dependency_closure.json",
        f"evidence/{NATIVE_CAPTURE_FACTS}",
    }
)


def _is_canonical_evidence(name: str) -> bool:
    if name in _CANONICAL_EVIDENCE:
        return True
    if not name.startswith("evidence/raw/triangles/"):
        return False
    relative = name.removeprefix("evidence/")
    try:
        parts = artifact_path_parts(relative)
    except PipelineError:
        return False
    return (
        len(parts) == 3
        and parts[:2] == ("raw", "triangles")
        and parts[2].endswith(".triangles")
        and _is_sha256(parts[2][: -len(".triangles")])
    )


def _allowed_payload(name: str) -> bool:
    """Only archived input, canonical evidence and sealed reports are payload."""

    return (
        name.startswith("input/")
        or _is_canonical_evidence(name)
        or name in _REQUIRED_REPORTS
    )


def _payload_candidate(name: str) -> bool:
    """Names that can belong to the native transfer contract."""

    return (
        name.startswith(("input/", "evidence/"))
        or name in _REQUIRED_REPORTS
        or name in _LIVE_REPORTS
    )


def _is_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _capture_upstream(value) -> list[dict]:
    """Validate the exact Freeze/Discover producer references of one Capture."""

    from .pipeline.contracts import ArtifactRef

    _require(
        isinstance(value, (list, tuple)) and len(value) == 2,
        "Capture upstream artifacts must contain exactly Freeze and Discover",
    )
    refs = [ArtifactRef.from_record(row) for row in value]
    by_stage = {ref.producer_stage: ref for ref in refs}
    _require(
        len(by_stage) == 2 and set(by_stage) == {"freeze", "discover"},
        "Capture upstream artifacts must contain one Freeze and one Discover producer",
    )
    expected_kinds = {
        "freeze": "stage_output:freeze",
        "discover": "stage_output:discover",
    }
    _require(
        all(by_stage[stage].kind == kind for stage, kind in expected_kinds.items()),
        "Capture upstream artifacts have invalid stage-output kinds",
    )
    _require(
        all(ref.producer_run_id for ref in refs),
        "Capture upstream artifacts must retain their producer run identities",
    )
    return [
        ref.to_record()
        for ref in sorted(
            refs,
            key=lambda item: (
                {"freeze": 0, "discover": 1}[item.producer_stage],
                item.artifact_id,
            ),
        )
    ]


def _capture_execution(value, *, run_id: str) -> dict:
    """Validate the executor identity carried beside upstream producer identity."""

    required = {
        "claim_id",
        "command_id",
        "effect_key",
        "executor_sha256",
        "fence",
        "native_tool_sha256",
        "owner_sha256",
        "run_id",
    }
    _require(
        isinstance(value, dict) and set(value) == required,
        "Capture execution fields are incomplete or unknown",
    )
    _require(
        value.get("run_id") == run_id,
        "Capture execution run differs from the transfer executor",
    )
    _require(
        all(
            isinstance(value.get(name), str) and bool(value[name])
            for name in ("claim_id", "command_id", "effect_key")
        ),
        "Capture execution lacks its command, claim or effect identity",
    )
    _require(type(value.get("fence")) is int and value["fence"] > 0, "Capture execution fence is invalid")
    _require(_is_sha256(value.get("owner_sha256")), "Capture execution owner digest is invalid")
    _require(_is_sha256(value.get("executor_sha256")), "Capture execution executor digest is invalid")
    _require(
        _is_sha256(value.get("native_tool_sha256")),
        "Capture execution native tool digest is invalid",
    )
    _require(
        value["owner_sha256"] != value["executor_sha256"],
        "Capture execution owner and executor identities are mixed",
    )
    return dict(value)


def _document_basename(value) -> str:
    return ntpath.basename(str(value or "").replace("/", "\\"))


def _normalize_document_path(value) -> str:
    return ntpath.normpath(str(value or "").replace("/", "\\")).casefold()


def _expected_prepared_assembly(prepared_dir: str, main_assembly: str) -> str:
    root = str(prepared_dir or "").replace("/", "\\")
    relative = str(main_assembly or "").replace("/", "\\")
    _require(bool(root) and bool(relative), "Native prepared root and main assembly are required")
    artifact_path_parts(main_assembly)
    _require(
        ".." not in root.split("\\") and ".." not in relative.split("\\"),
        "Native prepared source path contains traversal",
    )
    expected = ntpath.normpath(ntpath.join(root, relative))
    _require(
        ntpath.isabs(root) and ntpath.isabs(expected),
        "Native prepared root is not an absolute path",
    )
    return _normalize_document_path(expected)


def _validate_native_tool(native_tool) -> None:
    """A native tool record must pin its release and the Windows-native runtime closure."""

    tool_fields = {
        "schema_version",
        "pipeline_id",
        "version",
        "source_sha256",
        "release",
        "runtime",
    }
    _require(
        isinstance(native_tool, dict) and set(native_tool) == tool_fields,
        "Native tool record must carry its exact fields",
    )
    _require(native_tool.get("schema_version") == _TOOL_SCHEMA, "Native tool record uses an unknown schema")
    _require(native_tool.get("pipeline_id") == PIPELINE_ID, "Native tool record belongs to another pipeline")
    _require(native_tool.get("version") == __version__, "Native tool record is from another release")
    _require(_is_sha256(native_tool.get("source_sha256")), "Native tool record lacks its source digest")
    runtime = native_tool.get("runtime")
    _require(
        isinstance(runtime, dict)
        and set(runtime) == {"role", "system", "python", "machine", "packages"},
        "Native tool record lacks its exact runtime closure",
    )
    _require(
        runtime.get("role") == "native",
        "Native tool record must declare runtime.role = 'native'",
    )
    native_host = RUNTIME_HOSTS["native"]
    _require(
        runtime.get("system") == native_host["os"],
        "Native tool record must come from the Windows host",
    )
    _require(
        python_version_is_supported(runtime.get("python")),
        "Native tool record must pin Python 3.12",
    )
    machine = runtime.get("machine")
    _require(
        native_host["arch"] == "x86_64"
        and isinstance(machine, str)
        and machine.casefold() in {"amd64", "x86_64"},
        "Native tool record must pin a Windows x86_64 runtime",
    )
    packages = runtime.get("packages")
    _require(isinstance(packages, dict) and packages, "Native tool record lacks its package pins")
    pins = {}
    for name, version in packages.items():
        _require(
            isinstance(name, str)
            and bool(name)
            and isinstance(version, str)
            and bool(version),
            "Native tool package pins are malformed",
        )
        key = canonicalize_name(name)
        _require(key not in pins, f"Native tool record repeats package {key}")
        pins[key] = version
    required_native = {canonicalize_name(name) for name in required_packages("native")}
    _require(
        set(pins) == required_native,
        f"Native tool record must pin exactly the native package closure: {sorted(required_native)}",
    )
    for name in required_native:
        expected = next(
            version
            for package, version in RUNTIME_VERSIONS.items()
            if canonicalize_name(package) == name
        )
        _require(
            pins[name] == expected,
            f"Native tool record must pin {name} to the release version",
        )
    claimed_release = native_tool["release"]
    if claimed_release is not None:
        from .pipeline.ingestion import _formal_release  # noqa: PLC0415

        _formal_release(native_tool, role="native")
    release = Path(__file__).resolve().parent / "tool-release.json"
    if release.is_file():
        identity = read_data(release)
        _require(
            claimed_release == identity,
            "Native tool record is from another release than this portable runtime",
        )


def _validate_stage_receipts(
    view,
    *,
    run_id: str,
    handoff_sha256: str,
) -> None:
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
        tuple(view.get("execution_scope") or ()) == NATIVE_CAPTURE_SCOPE,
        "Stage receipts must declare the capture execution scope",
    )
    stages = view.get("stages")
    _require(
        isinstance(stages, list)
        and all(isinstance(stage, dict) for stage in stages)
        and len(stages) == len(STAGE_IDS)
        and [stage.get("id") for stage in stages] == list(STAGE_IDS),
        "Stage receipts must list the seven engineering stages once in contract order",
    )
    by_id = {stage["id"]: stage for stage in stages}
    for stage_id in NATIVE_CAPTURE_SCOPE:
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
        if stage_id in NATIVE_CAPTURE_SCOPE:
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
    previous_at = None
    stage_blocks = []
    for event in events:
        _require(isinstance(event, dict), "A native protocol event must be an object")
        _require(
            event.get("stage") in NATIVE_CAPTURE_SCOPE,
            "Native stage receipts contain an event from an out-of-scope stage",
        )
        timestamp = event.get("at")
        _require(isinstance(timestamp, str) and timestamp, "A native protocol event lacks its timestamp")
        try:
            parsed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as error:
            raise PipelineError(f"A native protocol event has an invalid timestamp: {timestamp!r}") from error
        _require(parsed_at.tzinfo is not None, "A native protocol event timestamp lacks its timezone")
        _require(
            previous_at is None or parsed_at >= previous_at,
            "Native protocol event timestamps are not monotonic",
        )
        previous_at = parsed_at
        _require(
            event.get("state") in {"running", "completed", "failed"},
            "A native protocol event has an unknown state",
        )
        _require(event["state"] != "failed", f"Native stage {event['stage']} contains a failed event")
        check = event.get("check")
        if check is not None:
            _require(
                isinstance(check, dict) and check.get("state") in {"passed", "failed"},
                "A native protocol check event is malformed",
            )
        if not stage_blocks or stage_blocks[-1][0] != event["stage"]:
            stage_blocks.append((event["stage"], []))
        stage_blocks[-1][1].append(event)
    _require(
        tuple(stage_id for stage_id, _block in stage_blocks)
        == NATIVE_CAPTURE_SCOPE,
        "Native protocol stage blocks are reordered, missing or interleaved",
    )
    for stage_id, block in stage_blocks:
        _require(
            block[0].get("state") == "running",
            f"Native stage {stage_id} does not start with a running event",
        )
        _require(
            sum(event["state"] == "running" for event in block) >= 1,
            f"Native stage {stage_id} carries no running event",
        )
        _require(
            sum(event["state"] == "completed" for event in block) == 1,
            f"Native stage {stage_id} does not carry exactly one completed event",
        )
        _require(
            block[-1]["state"] == "completed" and block[-1].get("check") is None,
            f"Native stage {stage_id} does not end with its completed event",
        )
        _require(
            all(event["state"] == "running" for event in block[1:-1]),
            f"Native stage {stage_id} carries an event outside its running interval",
        )
    try:
        recomputed = stage_view(
            {
                "run_id": run_id,
                "request": {"handoff_sha256": handoff_sha256},
                "result": {"execution_scope": list(NATIVE_CAPTURE_SCOPE)},
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
    for stage_id in NATIVE_CAPTURE_SCOPE:
        stage = by_id.get(stage_id) or {}
        checks = {}
        for boundary in ("input", "output"):
            for item in stage.get(f"{boundary}_qc") or []:
                if isinstance(item, dict):
                    checks[(boundary, item.get("id"))] = item.get("state")
        projection[stage_id] = {
            "at": stage.get("at"),
            "input_qc": stage.get("input_qc"),
            "output_qc": stage.get("output_qc"),
            "state": stage.get("state"),
            "in_scope": stage.get("in_scope"),
            "checks_passed": stage.get("checks_passed"),
            "checks_total": stage.get("checks_total"),
            "checks": checks,
        }
    return projection


def _casefold_inventory(value, label: str) -> dict[str, tuple[str, str]]:
    _require(isinstance(value, dict) and value, f"{label} must be a nonempty inventory")
    rows = {}
    for name, checksum in value.items():
        _require(isinstance(name, str) and name and _is_sha256(checksum), f"{label} has a malformed row")
        key = name.casefold()
        _require(key not in rows, f"{label} collides case-insensitively: {name!r}")
        rows[key] = (name, checksum)
    return rows


def _source_relative_inventory(native_files, main_assembly: str) -> dict[str, str]:
    """Map native package paths into the capture's assembly-relative namespace."""

    native = _casefold_inventory(native_files, "Native discovery file inventory")
    _require(isinstance(main_assembly, str) and main_assembly, "Main assembly identity is missing")
    artifact_path_parts(main_assembly)
    main_key = main_assembly.casefold()
    _require(main_key in native, "Native discovery lacks the selected main assembly")
    root = ntpath.dirname(main_assembly.replace("/", "\\"))
    rows: dict[str, str] = {}
    for _key, (name, checksum) in sorted(native.items()):
        artifact_path_parts(name)
        normalized = name.replace("/", "\\")
        relative = ntpath.relpath(normalized, root) if root else normalized
        relative_name = relative.replace("\\", "/").casefold()
        _require(
            relative_name not in rows,
            f"Native discovery source paths collide case-insensitively: {relative_name}",
        )
        rows[relative_name] = checksum
    return rows


def _validate_input(root: Path, report, main_assembly: str) -> tuple[dict[str, str], str, str]:
    _require(isinstance(report, dict), "Input report must be an object")
    _require(report.get("passed") is True, "Input report did not pass")
    package_files = report.get("package_files")
    _require(isinstance(package_files, dict) and package_files, "Input report lacks its package file inventory")
    actual = package_inventory(root / "input" / "cad")
    recorded = report.get("input_receipt", {}).get("inventory")
    actual_casefold = _casefold_inventory(actual, "Archived CAD inventory")
    recorded_casefold = _casefold_inventory(recorded, "Handoff CAD inventory")
    _require(
        {key: value[1] for key, value in actual_casefold.items()}
        == {key: value[1] for key, value in recorded_casefold.items()},
        "Archived input differs from the frozen handoff inventory",
    )
    main_row = actual_casefold.get(main_assembly.casefold())
    if main_row is None:
        raise PipelineError(f"The selected main assembly is not part of the capture input: {main_assembly!r}")
    return actual, main_row[0], main_row[1]


def _validate_evidence(
    root: Path,
    *,
    handoff_sha256: str,
    main_assembly: str,
    configuration: str,
    native_files: dict[str, str],
    native_tool: dict,
    native_prepared_dir: str | None = None,
    discovery_record: dict | None = None,
) -> tuple[dict, dict]:
    evidence = Path(root) / "evidence"
    manifest = verify_snapshot(evidence)
    _require(
        manifest.get("kind") == "solidworks" and manifest.get("evidence_class") == "cad",
        "Evidence snapshot is not native SolidWorks CAD evidence",
    )
    scene = manifest.get("scene")
    _require(
        isinstance(scene, str) and scene in (manifest.get("files") or {}),
        "Evidence snapshot does not bind its scene",
    )
    identity = (
        manifest.get("identity")
        if isinstance(manifest.get("identity"), dict)
        else {}
    )
    observation = load_native_capture_observation(root)
    plan = read_data(confined(root, "input/capture-plan.json"))
    validate_native_capture_plan(observation, plan)
    triangle_files = {
        name
        for name in manifest["files"]
        if name.startswith("raw/triangles/")
    }
    referenced_triangles = {
        str(row["stream_path"])
        for row in observation["geometry_raw"].values()
    }
    _require(
        triangle_files == referenced_triangles,
        "Evidence triangle inventory differs from raw geometry references",
    )
    raw_closure = read_data(confined(evidence, "raw/dependency_closure.json"))
    _require(isinstance(raw_closure, dict), "Dependency closure must be an object")
    _require(identity.get("provider") == "solidworks", "Evidence identity has the wrong provider")
    _require(identity.get("configuration") == configuration, "Evidence identity uses another configuration")
    _require(
        identity.get("worker_version") == native_tool.get("version"),
        "Evidence worker version differs from the native tool",
    )
    runtime = read_data(confined(evidence, "native-runtime.json"))
    _require(isinstance(runtime, dict), "Native runtime evidence must be an object")
    _require(
        identity.get("solidworks_revision") == (runtime.get("solidworks") or {}).get("revision"),
        "Evidence SolidWorks revision differs from the native runtime",
    )
    _require(runtime.get("toolchain") == native_tool, "Native runtime toolchain differs from the native tool")
    for name in ("assembly", "document_id"):
        value = identity.get(name)
        _require(
            isinstance(value, str)
            and _document_basename(value).casefold() == _document_basename(main_assembly).casefold(),
            f"Evidence identity {name} names another main assembly",
        )
    input_stability = observation.get("input_stability")
    _require(
        isinstance(input_stability, dict),
        "Raw Capture observation has no input stability record",
    )
    _require(
        set(input_stability) == {"originals_unchanged", "source_hashes"},
        "Raw Capture input stability fields are incomplete or unknown",
    )
    unchanged = input_stability.get("originals_unchanged")
    _require(
        isinstance(unchanged, dict),
        "Evidence does not prove the CAD bytes were unchanged",
    )
    _require(
        set(unchanged) == {"files_checked"},
        "Raw Capture input stability fields are incomplete or unknown",
    )
    files_checked = unchanged["files_checked"]
    _require(
        type(files_checked) is int and files_checked > 0,
        "Input stability files_checked must be a positive integer",
    )
    source_hashes = input_stability.get("source_hashes")
    _require(isinstance(source_hashes, dict) and source_hashes, "Evidence lacks native source hashes")
    native_source_inputs = _source_relative_inventory(native_files, main_assembly)
    source_inputs = {}
    for name, row in source_hashes.items():
        _require(
            isinstance(name, str)
            and name
            and isinstance(row, dict)
            and set(row) == {"sha256"}
            and _is_sha256(row["sha256"]),
            "Evidence source hash row is malformed",
        )
        key = name.casefold()
        _require(key not in source_inputs, "Evidence repeats a source identity case-insensitively")
        source_inputs[key] = row["sha256"]
    _require(
        source_inputs == native_source_inputs,
        "Evidence source hashes differ from the native discovery inventory",
    )
    # The producer counts every rehashed prepared CAD file; this exact comparison uses the
    # existing canonical source map and does not create another inventory.
    _require(
        files_checked == len(native_source_inputs),
        "Input stability files_checked differs from the complete native source inventory",
    )
    _require(
        identity.get("dependency_digest") == digest(sorted(source_inputs.items())),
        "Evidence dependency digest does not recompute from its source hashes",
    )
    collected_document = raw_closure.get("top_level")
    source_assembly = _normalize_document_path(identity.get("assembly"))
    source_document = _normalize_document_path(identity.get("document_id"))
    collected_source = _normalize_document_path(collected_document)
    closure_source = _normalize_document_path(raw_closure.get("top_level"))
    expected_source_suffix = "\\" + _normalize_document_path(main_assembly)
    if discovery_record is None:
        discovery_record = read_data(
            confined(root, "input/discovery/native-discovery.json")
        )
    discovery_identity = (
        discovery_record.get("identity")
        if isinstance(discovery_record, dict)
        else None
    )
    _require(
        isinstance(discovery_identity, dict)
        and plan.get("identity")
        == {
            "hardware_id": discovery_identity.get("hardware_id"),
            "revision": discovery_identity.get("revision"),
        },
        "Capture plan identity differs from sealed discovery",
    )
    discovery_main = discovery_identity.get("main_assembly") if isinstance(discovery_identity, dict) else None
    _require(
        isinstance(discovery_main, str) and bool(discovery_main),
        "Evidence discovery record names no unique source assembly",
    )
    discovery_source_suffix = "\\" + _normalize_document_path(discovery_main)
    _require(
        source_assembly == source_document
        and source_assembly.endswith(expected_source_suffix)
        and source_assembly.endswith(discovery_source_suffix),
        "Evidence source paths disagree or do not bind the selected main assembly",
    )
    if native_prepared_dir is not None:
        prepared_source = _expected_prepared_assembly(
            native_prepared_dir,
            main_assembly,
        )
        _require(
            source_assembly == source_document == prepared_source,
            "Evidence source paths differ from the platform prepared source",
        )
    _require(
        collected_source == closure_source
        and collected_source.endswith("\\" + _document_basename(main_assembly).casefold()),
        "Evidence collected document differs from the dependency closure top level",
    )
    _source_name, source_sha256 = next(
        (
            (name, checksum)
            for name, checksum in native_files.items()
            if _normalize_document_path(name) == _normalize_document_path(main_assembly)
        ),
        (None, None),
    )
    _require(source_sha256 is not None, "Native discovery lacks the selected main assembly")
    original_files = raw_closure.get("original_files")
    _require(
        isinstance(original_files, dict)
        and any(
            _normalize_document_path(path) == source_assembly and checksum == source_sha256
            for path, checksum in original_files.items()
        ),
        "Evidence dependency closure does not bind the selected source assembly",
    )
    mapping = raw_closure.get("mapping")
    _require(
        isinstance(mapping, list)
        and any(
            isinstance(row, dict)
            and _normalize_document_path(row.get("source")) == source_assembly
            and row.get("source_sha256") == source_sha256
            for row in mapping
        ),
        "Evidence dependency mapping does not bind the selected source assembly",
    )
    _require(
        isinstance(collected_document, str)
        and _document_basename(collected_document).casefold() == _document_basename(main_assembly).casefold(),
        "Evidence capture names another collected main assembly",
    )
    return identity, manifest


def _stage_checks(view: dict) -> dict[str, dict]:
    checks = {}
    for event in view.get("events") or []:
        check = event.get("check") if isinstance(event, dict) else None
        if not isinstance(check, dict):
            continue
        identifier = check.get("id")
        _require(isinstance(identifier, str) and identifier, "A native check has no identifier")
        _require(identifier not in checks, f"A native check is duplicated: {identifier}")
        checks[identifier] = check
    return checks


def _prepared_input_inventory(
    root: Path, native_files: dict[str, str], archived: dict[str, str], package_files
) -> dict[str, str]:
    """Reconstruct producer-relative inputs from the existing archived-input layout."""

    root = Path(root)
    archived_rows = _casefold_inventory(archived, "Archived input files")
    package_rows = _casefold_inventory(package_files, "Frozen package files")
    _require(
        set(package_rows) == set(archived_rows),
        "Frozen package file inventory differs from the archived input",
    )
    for key, (_name, checksum) in package_rows.items():
        _require(
            archived_rows[key][1] == checksum,
            f"Frozen package file differs from its archived bytes: {archived_rows[key][0]}",
        )
    native_rows = {}
    for _key, (name, checksum) in sorted(_casefold_inventory(native_files, "Native discovery file inventory").items()):
        artifact_path_parts(name)
        native_rows[name.casefold()] = checksum
    archived_cad = {
        key.removeprefix("cad/"): value[1]
        for key, value in archived_rows.items()
        if key.startswith("cad/")
    }
    _require(
        archived_cad == native_rows,
        "Archived CAD inventory differs from the native discovery inventory",
    )
    prepared = {}
    prepared_rows = set()
    for name, checksum in archived.items():
        relative = name.removeprefix("cad/") if name.startswith("cad/") else name
        artifact_path_parts(relative)
        folded = relative.casefold()
        _require(folded not in prepared_rows, f"Prepared input paths collide case-insensitively: {relative}")
        prepared_rows.add(folded)
        prepared[f"input/{relative}"] = checksum
    return prepared


def expected_native_check_details(
    root: Path,
    *,
    handoff_sha256: str,
    main_assembly: str,
    native_files: dict[str, str],
    discovery_record: dict | None = None,
    evidence_manifest: dict | None = None,
) -> dict[str, dict]:
    """Recompute every stage detail that is derivable from the final capture tree."""

    input_root = Path(root) / "input"
    input_inventory = {
        name: checksum
        for name, checksum in inventory(input_root).items()
        if name not in {"handoff.json", "request.json"}
    }
    handoff_record = read_data(confined(root, "input/handoff.json"))
    package_files = handoff_record.get("package_files")
    prepared_inputs = _prepared_input_inventory(root, native_files, input_inventory, package_files)
    expected_input = {f"input/{name}": checksum for name, checksum in input_inventory.items()}
    if discovery_record is None:
        discovery_record = read_data(
            confined(root, "input/discovery/native-discovery.json")
        )
    derivation = discovery_record.get("derivation") if isinstance(discovery_record, dict) else None
    record_roots = derivation.get("record_roots") if isinstance(derivation, dict) else []
    _require(
        isinstance(record_roots, list) and all(isinstance(value, str) and value for value in record_roots),
        "Native discovery derivation carries invalid controlled-record roots",
    )
    evidence_root = Path(root) / "evidence"
    if evidence_manifest is None:
        evidence_manifest = verify_snapshot(evidence_root)
    expected_evidence = {
        "evidence/manifest.json": file_digest(evidence_root / "manifest.json"),
        **{
            f"evidence/{name}": checksum
            for name, checksum in evidence_manifest["files"].items()
        },
    }
    return {
        "discovery.inputs": {
            "files_sha256": handoff_sha256,
            "handoff_sha256": handoff_sha256,
            "main_assembly": main_assembly,
            "record_roots": record_roots,
        },
        "input.valid": {
            "files": expected_input,
            "files_sha256": digest(package_files),
            "handoff_sha256": handoff_sha256,
            "passed": True,
            "warnings": list(handoff_record.get("warnings") or []),
        },
        "capture.integrity": {
            "files": expected_evidence,
            "manifest_sha256": file_digest(evidence_root / "manifest.json"),
        },
        "capture.input_stability": {
            "files": prepared_inputs,
        },
    }


def _validate_stage_details(
    root: Path,
    view: dict,
    *,
    handoff_sha256: str,
    main_assembly: str,
    native_files: dict[str, str],
    discovery_record: dict | None = None,
    evidence_manifest: dict | None = None,
) -> None:
    checks = _stage_checks(view)
    expected = expected_native_check_details(
        root,
        handoff_sha256=handoff_sha256,
        main_assembly=main_assembly,
        native_files=native_files,
        discovery_record=discovery_record,
        evidence_manifest=evidence_manifest,
    )
    required_identifiers = {
        "input.valid",
        "capture.integrity",
        "capture.input_stability",
    }
    _require(
        required_identifiers <= set(checks),
        "Native stage receipts lack required details: "
        f"{sorted(required_identifiers - set(checks))}",
    )
    for identifier in required_identifiers:
        _require(
            checks[identifier].get("details") == expected[identifier],
            f"Native stage detail differs from its captured bytes: {identifier}",
        )


def _validate_source_identity(
    root: Path,
    *,
    upload_sha256: str,
    main_assembly: str,
    configuration: str,
    discovery_record: dict | None = None,
) -> dict:
    source = validate_source_record(read_data(confined(root, "input/source.json")))
    identity = source["identity"]
    selection = source["selection"]
    handoff = read_data(confined(root, "input/handoff.json"))
    _require(isinstance(handoff, dict), "Frozen handoff must be an object")
    input_identity = handoff.get("input") if isinstance(handoff.get("input"), dict) else {}
    hardware = input_identity.get("hardware_id")
    _require(isinstance(hardware, str) and hardware, "Frozen handoff lacks its hardware identity")
    input_source = input_identity.get("source") if isinstance(input_identity.get("source"), dict) else {}
    _require(
        handoff.get("main_assembly") == main_assembly
        and handoff.get("configuration") == configuration
        and input_source.get("assembly") == main_assembly
        and input_source.get("configuration") == configuration,
        "Frozen handoff main assembly or configuration differs from the transfer",
    )
    _require(
        identity.get("handoff_digest") == upload_sha256
        and identity.get("main_assembly") == main_assembly
        and identity.get("configuration") == configuration
        and identity.get("hardware_id") == hardware
        and selection.get("main_assembly") == main_assembly
        and selection.get("configuration") == configuration
        and selection.get("hardware_id") == hardware,
        "Source record identity differs from the frozen handoff or transfer",
    )
    discovery = (
        discovery_record
        if discovery_record is not None
        else read_data(
            confined(root, "input/discovery/native-discovery.json")
        )
    )
    discovery_identity = discovery.get("identity") if isinstance(discovery, dict) else {}
    _require(
        discovery_identity.get("main_assembly") == main_assembly
        and (discovery_identity.get("delivery_configuration") or configuration) == configuration
        and discovery_identity.get("hardware_id") == hardware,
        "Native discovery identity differs from the source record",
    )
    discovery_records = discovery.get("records")
    _require(isinstance(discovery_records, list), "Native discovery controlled-record list is missing")
    by_reference = {}
    for item in discovery_records:
        _require(isinstance(item, dict), "Native discovery controlled record is malformed")
        reference = item.get("reference")
        _require(
            isinstance(reference, str) and bool(reference) and reference not in by_reference,
            "Native discovery repeats a controlled-record reference",
        )
        by_reference[reference] = item
    source_records = identity["records"]
    _require(
        {item["reference"] for item in source_records} == set(by_reference),
        "Source record controlled references differ from native discovery",
    )
    for item in source_records:
        expected = by_reference[item["reference"]]
        _require(
            set(expected)
            == {
                "reference",
                "file",
                "key",
                "package_file",
                "sha256",
                "source_sha256",
                "anchor",
            },
            f"Native controlled record has unknown or missing fields: {item['reference']}",
        )
        package_file = record_archive_path(str(expected.get("package_file") or ""))
        handoff_files = handoff.get("package_files")
        source_sha256 = expected.get("source_sha256")
        record_sha256 = expected.get("sha256")
        source_file = expected.get("file")
        record_key = expected.get("key")
        anchor = expected.get("anchor")
        reference = item["reference"]
        reference_path, marker, reference_key = reference.partition("#")
        _require(
            marker in {"", "#"} and reference.count("#") <= 1 and bool(reference_path),
            f"Controlled record reference is not canonical: {reference}",
        )
        canonical_reference_path = reference_path.replace("\\", "/")
        _require(
            reference_path == canonical_reference_path
            and canonical_reference_path == str(source_file or "").replace("\\", "/")
            and reference_key == str(record_key or ""),
            f"Controlled record reference disagrees with its file or key: {reference}",
        )
        _require(
            _is_sha256(source_sha256)
            and record_sha256 == source_sha256
            and source_sha256 == item["file_sha256"]
            and isinstance(source_file, str)
            and bool(source_file)
            and isinstance(record_key, str)
            and (anchor is None or (isinstance(anchor, str) and bool(anchor)))
            and isinstance(handoff_files, dict),
            f"Controlled record path differs from the source record: {item['reference']}",
        )
        artifact_path_parts(source_file)
        archived_record = next(
            (
                name
                for name, checksum in handoff_files.items()
                if name.replace("\\", "/").casefold() == package_file.casefold() and checksum == source_sha256
            ),
            None,
        )
        _require(
            archived_record is not None and file_digest(confined(root, "input/" + archived_record)) == source_sha256,
            f"Controlled record bytes differ from the source record: {item['reference']}",
        )
        if anchor is not None:
            try:
                content = confined(root, "input/" + archived_record).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise PipelineError(f"Controlled record anchor is unreadable: {item['reference']}") from error
            _require(
                anchor in content,
                f"Controlled record anchor is not present in its bound bytes: {item['reference']}",
            )
    request_path = confined(root, "input/request.json", exists=False)
    if request_path.is_file():
        request, _request_sha256 = read_request_evidence(request_path)
        _require(
            request.get("main_assembly") == main_assembly
            and request.get("target_hardware") == hardware
            and request.get("handoff_digest") == upload_sha256,
            "Frozen request differs from the source and handoff identity",
        )
    return source


def _validate_native_discovery(
    root: Path,
    *,
    producer_run_id: str,
    main_assembly: str,
    handoff_sha256: str,
    actual_inputs: dict[str, str],
) -> tuple[dict[str, str], str, dict]:
    """Bind the main assembly through the real input source and the native discovery record.

    The packaged discovery record names the entry the reader opened, the handoff it was bound to,
    and the native file inventory it hashed; the archived input must carry the same bytes.
    """

    record = read_data(
        confined(root, "input/discovery/native-discovery.json")
    )
    _require(isinstance(record, dict), "Native discovery record must be an object")
    _require(
        record.get("schema_version") == NATIVE_DISCOVERY_SCHEMA,
        "Native discovery record uses an unknown schema",
    )
    recorded_run_id = record.get("run_id")
    try:
        canonical_discovery_run = str(uuid.UUID(str(recorded_run_id)))
    except (ValueError, AttributeError, TypeError) as error:
        raise PipelineError("Native discovery run_id is not a UUID") from error
    _require(
        recorded_run_id == canonical_discovery_run == producer_run_id,
        "Native discovery record belongs to another producer run",
    )
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
    native_casefold = _casefold_inventory(native_files, "Native discovery file inventory")
    actual_casefold = _casefold_inventory(actual_inputs, "Archived capture input inventory")
    _require(
        {key: value[1] for key, value in native_casefold.items()}
        == {key: value[1] for key, value in actual_casefold.items()},
        "Native discovery inventory differs from the archived capture input",
    )
    _require(
        digest(native_files) == handoff_sha256,
        "The native discovery file inventory does not digest to its handoff",
    )
    main = native_casefold.get(main_assembly.casefold())
    _require(main is not None, f"The native discovery record does not inventory the main assembly: {main_assembly!r}")
    _require(
        main[1] == file_digest(confined(root, "input/cad/" + main[0])),
        "The archived main assembly differs from the native file inventory",
    )
    return native_files, main[1], record


def _validate_capture(
    root: Path,
    *,
    run_id: str,
    handoff_sha256: str,
    upload_sha256: str,
    main_assembly: str,
    native_tool,
    native_stages=None,
    native_tool_record=None,
    native_prepared_dir: str | None = None,
    discovery_producer_run_id: str | None = None,
) -> dict:
    """Re-read a capture root (sealed or freshly extracted) against the transfer contract."""

    _validate_native_tool(native_tool)
    recorded = (
        native_tool_record if native_tool_record is not None else read_data(confined(root, "reports/native-tool.json"))
    )
    _require(recorded == native_tool, "Native tool report differs from the supplied native tool record")
    view = native_stages if native_stages is not None else read_data(confined(root, "reports/native-stages.json"))
    _validate_stage_receipts(
        view,
        run_id=run_id,
        handoff_sha256=handoff_sha256,
    )
    handoff = read_data(confined(root, "input/handoff.json"))
    actual_inputs, input_main, main_assembly_sha256 = _validate_input(root, handoff, main_assembly)
    native_files, native_sha256, discovery_record = _validate_native_discovery(
        root,
        producer_run_id=discovery_producer_run_id or run_id,
        main_assembly=main_assembly,
        handoff_sha256=handoff_sha256,
        actual_inputs=actual_inputs,
    )
    _require(
        native_sha256 == main_assembly_sha256,
        "The archived main assembly differs between the input report and the native inventory",
    )
    _require(
        _document_basename(input_main).casefold() == _document_basename(main_assembly).casefold(),
        "Input and transfer main assembly identities differ",
    )
    configuration = str(handoff.get("configuration") or "")
    _require(configuration, "Frozen handoff lacks its configuration")
    source = _validate_source_identity(
        root,
        upload_sha256=upload_sha256,
        main_assembly=main_assembly,
        configuration=configuration,
        discovery_record=discovery_record,
    )
    _identity, evidence_manifest = _validate_evidence(
        root,
        handoff_sha256=handoff_sha256,
        main_assembly=main_assembly,
        configuration=configuration,
        native_files=native_files,
        native_tool=native_tool,
        native_prepared_dir=native_prepared_dir,
        discovery_record=discovery_record,
    )
    _validate_stage_details(
        root,
        view,
        handoff_sha256=handoff_sha256,
        main_assembly=main_assembly,
        native_files=native_files,
        discovery_record=discovery_record,
        evidence_manifest=evidence_manifest,
    )
    return {
        "main_assembly_sha256": main_assembly_sha256,
        "source_revision_id": source["source_revision_id"],
        "hardware_id": source["identity"]["hardware_id"],
    }


def capture_manifest(
    root: Path,
    *,
    run_id: str,
    handoff_sha256: str,
    upload_sha256: str,
    main_assembly: str,
    native_tool: dict,
    upstream_artifacts,
    execution,
) -> dict:
    """Build and validate one closed native payload with the shared verifier."""

    root = Path(root)
    _require(isinstance(run_id, str) and run_id.strip(), "run_id must be a nonempty string")
    _require(_is_sha256(handoff_sha256), "handoff_sha256 must be a sha256 digest")
    _require(_is_sha256(upload_sha256), "upload_sha256 must be a sha256 digest")
    _require(isinstance(main_assembly, str) and main_assembly.strip(), "main_assembly must be a relative file name")
    artifact_path_parts(main_assembly)

    files = inventory(root)
    # Sealing outputs (the archive, the optional sidecar manifest) and the live stage view are
    # never payload: Linux rewrites the live view after admission.
    ignored = {CAPTURE_MANIFEST, *_LIVE_REPORTS}
    files = {name: checksum for name, checksum in files.items() if name not in ignored}
    _require(files, "Capture root contains no files to seal")
    _require(len(files) <= MAX_TRANSFER_FILES, "Capture exceeds the transfer file limit")
    total_bytes = sum(Path(_physical_path(confined(root, name))).stat().st_size for name in files)
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

    normalized_upstream = _capture_upstream(upstream_artifacts)
    normalized_execution = _capture_execution(execution, run_id=run_id)
    manifest = {
        "schema_version": CAPTURE_TRANSFER_SCHEMA,
        "run_id": run_id,
        "handoff_sha256": handoff_sha256,
        "upload_sha256": upload_sha256,
        "main_assembly": main_assembly,
        "main_assembly_sha256": file_digest(confined(root, "input/cad/" + main_assembly)),
        "native_tool": native_tool,
        "native_tool_sha256": digest(native_tool),
        "files": files,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "upstream_artifacts": normalized_upstream,
        "execution": normalized_execution,
    }
    verify_transfer(
        root,
        transfer_manifest=manifest,
        expected_upstream_artifacts=manifest.get("upstream_artifacts"),
        expected_execution=manifest.get("execution"),
    )
    return manifest


def verify_transfer(
    bundle: Path,
    *,
    carried: dict[str, str] | None = None,
    native_receipts: dict | None = None,
    transfer_manifest: dict | None = None,
    expected_upstream_artifacts,
    expected_execution,
    expected_run_id: str | None = None,
    expected_handoff_sha256: str | None = None,
    expected_main_assembly: str | None = None,
    expected_native_tool: dict | None = None,
    expected_upload_sha256: str | None = None,
    native_prepared_dir: str | None = None,
) -> dict | None:
    """Revalidate transferred native provenance inside a delivery bundle.

    Returns ``None`` for a neutral delivery that carries no transfer.  A partial triplet is
    refused; a complete one re-hashes the sealed payload and re-reads the native tool record,
    stage receipts, evidence/discovery main-assembly binding and evidence snapshot, so a resealed
    but semantically invalid manifest cannot reach publication.

    ``carried`` names the Linux-side originals the freeze stage registered from their own
    bytes (``input/request.json``), mapped to the frozen binding of this attempt.  A carried
    original may also be listed by the native manifest; either classification must bind the
    same delivered bytes.  It never exempts anything else: unknown extras still refuse.
    """

    bundle = Path(bundle)
    supplied = {}
    _require(
        native_receipts is None or transfer_manifest is None,
        "Transfer manifest cannot have two receipt sources",
    )
    if native_receipts is not None:
        _require(isinstance(native_receipts, dict), "Native receipts must be supplied as an object")
        for name in ("native_tool_bytes", "native_stages_bytes", "transfer_manifest_bytes"):
            blob = native_receipts.get(name)
            _require(
                isinstance(blob, (bytes, bytearray)) and bool(blob),
                f"Native receipt {name} is missing",
            )
            supplied[name] = bytes(blob)
        try:
            receipt_manifest = parse_data(
                supplied["transfer_manifest_bytes"],
                Path("transfer_manifest_bytes.json"),
            )
            receipt_native_tool = parse_data(
                supplied["native_tool_bytes"],
                Path("native_tool_bytes.json"),
            )
            receipt_stages = parse_data(
                supplied["native_stages_bytes"],
                Path("native_stages_bytes.json"),
            )
        except PipelineError as error:
            raise PipelineError("Native receipts are not UTF-8 JSON") from error
    elif transfer_manifest is not None:
        receipt_manifest = transfer_manifest
        receipt_native_tool = receipt_stages = None
    else:
        present = [name for name in TRANSFER_FILES if (bundle / name).is_file()]
        if not present:
            return None
        missing = sorted(set(TRANSFER_FILES) - set(present))
        _require(not missing, f"Transferred capture provenance is incomplete; missing: {missing}")
        receipt_manifest = receipt_native_tool = receipt_stages = None
    carried_bindings = dict(carried or {})
    manifest = receipt_manifest if receipt_manifest is not None else read_data(confined(bundle, CAPTURE_MANIFEST))
    _require(isinstance(manifest, dict), "Transfer manifest must be an object")
    manifest_schema = manifest.get("schema_version")
    expected_fields = {
        "schema_version",
        "run_id",
        "handoff_sha256",
        "upload_sha256",
        "main_assembly",
        "main_assembly_sha256",
        "native_tool",
        "native_tool_sha256",
        "files",
        "file_count",
        "total_bytes",
        "upstream_artifacts",
        "execution",
    }
    _require(
        set(manifest) == expected_fields,
        "Transfer manifest fields are incomplete or unknown",
    )
    _require(
        manifest_schema == CAPTURE_TRANSFER_SCHEMA,
        "Transfer manifest uses an unknown schema",
    )
    run_id = manifest.get("run_id")
    try:
        canonical_run_id = str(uuid.UUID(run_id))
    except (ValueError, AttributeError, TypeError) as error:
        raise PipelineError("Transfer run_id is not a UUID") from error
    _require(
        run_id == canonical_run_id,
        "Transfer run_id is not the canonical lowercase UUID",
    )
    upstream_artifacts = _capture_upstream(manifest.get("upstream_artifacts"))
    execution = _capture_execution(
        manifest.get("execution"),
        run_id=run_id,
    )
    _require(
        upstream_artifacts == _capture_upstream(expected_upstream_artifacts),
        "Capture transfer binds different upstream artifacts",
    )
    _require(
        execution
        == _capture_execution(
            expected_execution,
            run_id=run_id,
        ),
        "Capture transfer binds another execution identity",
    )
    files = manifest.get("files")
    _require(isinstance(files, dict) and files, "Transfer manifest declares no files")
    _require(
        set(_REQUIRED_REPORTS) <= set(files),
        "Transfer manifest omits required capture reports: "
        f"{sorted(set(_REQUIRED_REPORTS) - set(files))}",
    )
    carried_not_native = set(carried_bindings) - set(files)
    skip = carried_not_native
    ignored = {CAPTURE_MANIFEST}
    if transfer_manifest is not None:
        ignored.update(_LIVE_REPORTS)
    actual_members = set(inventory(bundle))
    if receipt_manifest is not None:
        actual_members = {
            name for name in actual_members if _payload_candidate(name)
        }
    actual = actual_members - ignored - skip
    expected_members = set(files)
    _require(
        actual == expected_members,
        "Transfer archive inventory differs from its manifest: "
        f"missing={sorted(expected_members - actual)[:5]}, "
        f"extra={sorted(actual - expected_members)[:5]}",
    )
    _require(len(files) <= MAX_TRANSFER_FILES, "Capture exceeds the transfer file limit")
    _require(
        type(manifest.get("file_count")) is int and manifest["file_count"] == len(files),
        "Transfer manifest file count differs from its inventory",
    )
    _require(
        type(manifest.get("total_bytes")) is int and manifest["total_bytes"] >= 0,
        "Transfer manifest total bytes are invalid",
    )
    sized_members = {}
    for name, checksum in files.items():
        artifact_path_parts(name)
        _require(
            _allowed_payload(name),
            f"Transfer manifest lists a member outside the capture payload: {name!r}",
        )
        _require(_is_sha256(checksum), f"Transfer manifest hash is not a sha256: {name}")
        if name == "reports/native-tool.json" and receipt_native_tool is not None:
            actual = hashlib.sha256(supplied["native_tool_bytes"]).hexdigest()
            disk_path = confined(bundle, name, exists=False)
            if disk_path.is_file():
                _require(
                    file_digest(disk_path) == checksum,
                    f"Native disk receipt differs from the transfer manifest: {name}",
                )
        elif name == "reports/native-stages.json" and receipt_stages is not None:
            actual = hashlib.sha256(supplied["native_stages_bytes"]).hexdigest()
            disk_path = confined(bundle, name, exists=False)
            if disk_path.is_file():
                _require(
                    file_digest(disk_path) == checksum,
                    f"Native disk receipt differs from the transfer manifest: {name}",
                )
        else:
            actual = file_digest(confined(bundle, name))
        _require(
            actual == checksum,
            f"Delivered member differs from the transfer manifest: {name}",
        )
        if name == "reports/native-tool.json" and receipt_native_tool is not None:
            sized_members[name] = len(supplied["native_tool_bytes"])
        elif name == "reports/native-stages.json" and receipt_stages is not None:
            sized_members[name] = len(supplied["native_stages_bytes"])
        else:
            sized_members[name] = confined(bundle, name).stat().st_size
    actual_payload = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file()
        and _payload_candidate(path.relative_to(bundle).as_posix())
        and path.relative_to(bundle).as_posix() not in ignored
        and path.relative_to(bundle).as_posix() not in skip
    }
    receipt_members = set()
    if receipt_native_tool is not None:
        receipt_members = {"reports/native-tool.json", "reports/native-stages.json"}
    absent = sorted(set(files) - receipt_members - actual_payload)
    extra = sorted(actual_payload - set(files))
    _require(
        not absent and not extra,
        f"Delivered capture inventory differs from the transfer manifest: missing={absent[:5]}, extra={extra[:5]}",
    )
    for name, checksum in carried_bindings.items():
        _require(_allowed_payload(name), f"Carried original lies outside the capture payload: {name!r}")
        _require(_is_sha256(checksum), f"Carried original binding is not a sha256: {name}")
        if name in files:
            _require(
                files[name] == checksum,
                f"Carried original differs from the native manifest binding: {name}",
            )
        _require(
            file_digest(confined(bundle, name)) == checksum,
            f"Delivered carried original differs from its frozen bytes: {name}",
        )
    _require(
        manifest.get("total_bytes") == sum(sized_members.values()),
        "Transfer manifest size differs from its members",
    )
    _require(
        manifest.get("native_tool_sha256") == digest(manifest.get("native_tool")),
        "Transfer manifest native tool digest differs from its record",
    )
    _validate_native_tool(manifest.get("native_tool"))
    _require(
        execution["native_tool_sha256"] == manifest.get("native_tool_sha256"),
        "Capture execution native tool differs from the transfer tool",
    )
    if receipt_native_tool is not None:
        _require(
            receipt_native_tool == manifest.get("native_tool"),
            "Native tool receipt differs from the transfer manifest",
        )
    else:
        _require(
            read_data(confined(bundle, "reports/native-tool.json")) == manifest.get("native_tool"),
            "Native tool report differs from the transfer manifest",
        )
    handoff = manifest.get("handoff_sha256")
    upload = manifest.get("upload_sha256")
    main_assembly = manifest.get("main_assembly")
    _require(
        _is_sha256(handoff)
        and _is_sha256(upload)
        and isinstance(main_assembly, str)
        and main_assembly,
        "Transfer manifest lacks its handoff, upload or main assembly",
    )
    if expected_upload_sha256 is not None:
        _require(upload == expected_upload_sha256, "Transfer binds another upload")
    if expected_run_id is not None:
        _require(run_id == expected_run_id, "Transfer belongs to another run")
    if expected_handoff_sha256 is not None:
        _require(handoff == expected_handoff_sha256, "Transfer binds another handoff")
    if expected_main_assembly is not None:
        _require(main_assembly == expected_main_assembly, "Transfer binds another main assembly")
    if expected_native_tool is not None:
        _require(manifest.get("native_tool") == expected_native_tool, "Transfer carries another native tool record")
    validated = _validate_capture(
        bundle,
        run_id=run_id,
        handoff_sha256=handoff,
        upload_sha256=upload,
        main_assembly=main_assembly,
        native_tool=manifest.get("native_tool"),
        native_stages=receipt_stages,
        native_tool_record=receipt_native_tool,
        native_prepared_dir=native_prepared_dir,
        discovery_producer_run_id=next(
            (
                ref["producer_run_id"]
                for ref in upstream_artifacts or ()
                if ref.get("producer_stage") == "discover"
            ),
            None,
        ),
    )
    _require(
        validated["main_assembly_sha256"] == manifest.get("main_assembly_sha256"),
        "The delivered main assembly differs from the transfer manifest",
    )
    return {
        "run_id": run_id,
        "handoff_sha256": handoff,
        "upload_sha256": upload,
        "main_assembly": main_assembly,
        "main_assembly_sha256": validated["main_assembly_sha256"],
        "files": len(files),
        "source_revision_id": validated["source_revision_id"],
        "hardware_id": validated["hardware_id"],
    }


def validate_native_receipts(
    root: Path,
    receipts: dict,
    *,
    expected_upload_sha256: str | None = None,
    native_prepared_dir: str | None = None,
) -> dict:
    """Validate an in-memory native receipt triplet against one delivery root.

    This is the publisher path: native report files are intentionally absent
    from the model repository, so their immutable bytes are supplied by the
    platform. This is a thin transport adapter over the one on-disk transfer
    validator; the manifest, stage, input, evidence, and identity semantics are
    never duplicated.
    """

    _require(isinstance(receipts, dict), "Native receipts must be supplied as an object")
    blobs = {}
    receipt_names = (
        "native_tool_bytes",
        "native_stages_bytes",
        "transfer_manifest_bytes",
    )
    for name in receipt_names:
        blob = receipts.get(name)
        _require(isinstance(blob, (bytes, bytearray)) and bool(blob), f"Native receipt {name} is missing")
        blobs[name] = bytes(blob)
    try:
        transfer = parse_data(blobs["transfer_manifest_bytes"], Path("transfer_manifest_bytes.json"))
        native_tool = parse_data(blobs["native_tool_bytes"], Path("native_tool_bytes.json"))
        stages = parse_data(blobs["native_stages_bytes"], Path("native_stages_bytes.json"))
    except PipelineError as error:
        raise PipelineError("Native receipts are not UTF-8 JSON") from error
    request_path = confined(root, "input/request.json", exists=False)
    carried = {"input/request.json": file_digest(request_path)} if request_path.is_file() else {}
    summary = verify_transfer(
        root,
        carried=carried,
        native_receipts=blobs,
        expected_upstream_artifacts=transfer.get("upstream_artifacts"),
        expected_execution=transfer.get("execution"),
        expected_upload_sha256=expected_upload_sha256,
        native_prepared_dir=native_prepared_dir,
    )
    _require(isinstance(summary, dict), "Native receipts do not bind a transfer")
    return {
        "digests": {name: hashlib.sha256(blob).hexdigest() for name, blob in blobs.items()},
        "native_tool": native_tool,
        "native_tool_sha256": hashlib.sha256(blobs["native_tool_bytes"]).hexdigest(),
        "native_stages": stages,
        "transfer_manifest": transfer,
        **summary,
    }


def load_native_receipts(root: Path) -> dict[str, bytes] | None:
    """Load the complete native receipt triplet from one delivery boundary."""

    names = {
        "native_tool_bytes": "reports/native-tool.json",
        "native_stages_bytes": "reports/native-stages.json",
        "transfer_manifest_bytes": "transfer-manifest.json",
    }
    paths = {
        name: confined(root, relative, exists=False)
        for name, relative in names.items()
    }
    required = ("native_tool_bytes", "native_stages_bytes", "transfer_manifest_bytes")
    present = [paths[name].is_file() for name in required]
    if not any(present):
        return None
    _require(all(present), "Native receipt triplet is incomplete")
    return {
        name: path.read_bytes()
        for name, path in paths.items()
        if path.is_file()
    }
