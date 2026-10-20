"""Shared recovery for a native job whose capture stage failed.

The prepared package is built on Linux before the native job starts.  When the
job fails inside capture, the discover engineering stage is already complete
but its platform row may never have been written.  This module reconstructs
that row from the exact prepared members and the original failed job; the DAG
and the maintenance entry share the same tree, receipt and store call.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path

from ..delivery import BUNDLE_SCHEMA, PIPELINE_ID, read_delivery_manifest, write_delivery_manifest
from ..io import PipelineError, artifact_path_parts, canonical, confined, digest, parse_data, read_data, write_json
from ..interface_records import check_policy_digest, validate_request_record, CHECK_POLICY_SCHEMA
from ..schemas import TOOL_SCHEMA
from ..sources.solidworks.selection import selection_bindings
from ..stages import (
    CONFIRMED_STAGE_PASSED,
    CONFIRMED_STAGE_STATES,
    CONTRACT,
    CONTRACT_SHA256,
    VIEW_SCHEMA,
)
from .storage import seal_stage_confirmation

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def validate_native_discovery_completed(events: object) -> int:
    """Require one completed discover event and return its position."""

    rows = _event_rows(events)
    completed = [
        index
        for index, event in enumerate(rows)
        if event.get("stage") == "discover" and event.get("state") == "completed"
    ]
    if len(completed) != 1:
        raise PipelineError("The native job does not carry exactly one completed discovery")
    if any(event.get("stage") == "discover" and event.get("state") == "failed" for event in rows):
        raise PipelineError("The native job carries a failed discovery alongside a completed discovery")
    return completed[0]


def validate_capture_failure_events(events: object) -> int:
    """Require one ordered capture-failed terminal event after the discovery."""

    rows = _event_rows(events)
    discovery_index = validate_native_discovery_completed(rows)
    index = validate_native_failed_terminal(rows)
    if rows[index].get("state") != "failed" or rows[index].get("stage") != "capture":
        raise PipelineError("The native terminal event is not a capture failure")
    if index <= discovery_index:
        raise PipelineError("The native capture failure precedes the completed discovery")
    if index != len(rows) - 1:
        raise PipelineError("The native capture failure is not the last event")
    return index


def validate_native_failed_terminal(events: object) -> int:
    """Require one failed terminal event at the end of the native event list."""

    rows = _event_rows(events)
    if any(event.get("state") == "passed" for event in rows):
        raise PipelineError("The native failure carries a passed event")
    failed = [index for index, event in enumerate(rows) if event.get("state") == "failed"]
    if len(failed) != 1:
        raise PipelineError("The native failure must carry exactly one failed terminal event")
    if failed[0] != len(rows) - 1:
        raise PipelineError("The native failed terminal event is not the last event")
    return failed[0]


def _event_rows(events: object) -> list[dict]:
    """Reject a malformed event stream before any ordering conclusion."""

    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise PipelineError("The native job event stream is not a list of objects")
    return events


def validate_native_discovery_stage(
    native_stages: object,
    *,
    run_id: str,
    handoff_sha256: str,
    capture_state: str = "failed",
) -> dict:
    """Require the native job's own completed discover and terminal capture facts."""

    if not isinstance(native_stages, dict):
        raise PipelineError("The native capture failure carries no stage view")
    if (
        native_stages.get("schema_version") != VIEW_SCHEMA
        or native_stages.get("pipeline_id") != PIPELINE_ID
        or native_stages.get("contract_sha256") != CONTRACT_SHA256
        or native_stages.get("run_id") != run_id
        or native_stages.get("handoff_sha256") != handoff_sha256
        or not _valid_native_execution_scope(native_stages.get("execution_scope"))
    ):
        raise PipelineError("The native capture failure carries another stage contract or run identity")
    rows = native_stages.get("stages")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PipelineError("The native capture failure carries no readable stage rows")
    expected_ids = [stage["id"] for stage in CONTRACT["stages"]]
    if [row.get("id") for row in rows] != expected_ids:
        raise PipelineError("The native stage view does not cover the installed contract exactly once")
    by_id = {row["id"]: row for row in rows}
    definition = next(stage for stage in CONTRACT["stages"] if stage["id"] == "discover")
    discover = by_id["discover"]
    if discover.get("state") != "completed" or discover.get("in_scope") is not True:
        raise PipelineError("The native stage view does not prove completed discovery")
    for boundary in ("input_qc", "output_qc"):
        expected = [item["id"] for item in definition[boundary]]
        rows_for_boundary = discover.get(boundary)
        if (
            not isinstance(rows_for_boundary, list)
            or [row.get("id") for row in rows_for_boundary if isinstance(row, dict)] != expected
            or any(
                not isinstance(row, dict)
                or row.get("state") != "passed"
                or (isinstance(row.get("details"), dict) and row["details"].get("passed") is False)
                for row in rows_for_boundary
            )
        ):
            raise PipelineError(f"The native discover {boundary} is not completely passed")
    capture = by_id["capture"]
    if capture.get("state") != capture_state or capture.get("in_scope") is not True:
        raise PipelineError(f"The native stage view does not prove capture {capture_state}")
    return native_stages


def _valid_native_execution_scope(scope: object) -> bool:
    """The native half is exactly discover/capture, optionally led by freeze."""

    return scope in (["discover", "capture"], ["freeze", "discover", "capture"])


def _request_evidence(
    request_evidence: object,
    *,
    request_upload_sha256: str,
    main_assembly: str,
) -> dict:
    """Validate the frozen request bytes and their native handoff/main binding."""

    if not isinstance(request_evidence, bytes) or not request_evidence:
        raise PipelineError("The native recovery carries no frozen request evidence")
    try:
        record = parse_data(request_evidence, Path("input/request.json"))
        validate_request_record(record)
    except (TypeError, ValueError) as error:
        raise PipelineError("The native recovery request evidence is not the frozen request record") from error
    if canonical(record) != request_evidence:
        raise PipelineError("The native recovery request evidence is not in canonical frozen form")
    if record.get("handoff_digest") != request_upload_sha256 or record.get("main_assembly") != main_assembly:
        raise PipelineError("The frozen request evidence does not bind the admitted upload and main assembly")
    return record


def _portable_tool(portable_tool: object, native_tool: Mapping[str, object]) -> dict:
    """Require the original portable tool identity from the same release."""

    portable_runtime = portable_tool.get("runtime") if isinstance(portable_tool, dict) else None
    if (
        not isinstance(portable_tool, dict)
        or portable_tool.get("schema_version") != TOOL_SCHEMA
        or portable_tool.get("pipeline_id") != PIPELINE_ID
        or not isinstance(portable_runtime, dict)
        or portable_runtime.get("role") != "portable"
        or portable_tool.get("version") != native_tool.get("version")
        or portable_tool.get("source_sha256") != native_tool.get("source_sha256")
    ):
        raise PipelineError("The native recovery carries no matching portable tool identity")
    return portable_tool


def build_discovery_tree_from_prepared(
    *,
    tree: Path,
    derived: Path,
    archive_path: Path,
    prepared: Mapping[str, str],
    native_tool: dict,
    run_id: str,
    main_assembly: str,
    native_stages: object,
    request_evidence: object,
    portable_tool: object,
    request_upload_files: object,
    request_upload_sha256: object,
    capture_state: str = "failed",
) -> dict:
    """Reconstruct the completed discover tree from the prepared package.

    Only the prepared members named by the failed job are copied.  The native
    originals move under ``input/cad``, the discovery record and capture plan
    move to their declared evidence paths, and every member is hashed before
    and after the copy.  No native or CAD operation occurs here.
    """

    if capture_state not in {"completed", "failed"}:
        raise PipelineError("The native recovery carries an unsupported expected capture state")
    if not prepared:
        raise PipelineError("The native prepared inventory is empty")
    if "source.json" not in prepared:
        raise PipelineError("The native prepared inventory carries no source identity")
    if (
        not isinstance(request_upload_files, dict)
        or not request_upload_files
        or any(not isinstance(name, str) or not _is_sha256(value) for name, value in request_upload_files.items())
        or not _is_sha256(request_upload_sha256)
        or digest(request_upload_files) != request_upload_sha256
    ):
        raise PipelineError("The native recovery does not bind its full request upload inventory")
    if not archive_path.is_file():
        raise PipelineError("The sealed capture-request archive is missing")
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "input" / "cad").mkdir(parents=True)
    (tree / "input" / "records").mkdir()
    (tree / "evidence").mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        try:
            discovery = json.loads(archive.read("discovery/native-discovery.json"))
        except (KeyError, ValueError) as error:
            raise PipelineError("The capture-request archive carries no readable discovery record") from error
        native_inventory = discovery.get("native_files") if isinstance(discovery, dict) else None
        if not isinstance(native_inventory, dict) or not native_inventory:
            raise PipelineError("The prepared discovery record carries no native inventory")
        for name, checksum in native_inventory.items():
            if not isinstance(name, str) or prepared.get(name) != checksum:
                raise PipelineError("The prepared discovery inventory differs from the prepared package")
        native_names = set(native_inventory)
        for name, expected in prepared.items():
            if not isinstance(name, str) or not isinstance(expected, str):
                raise PipelineError("The native prepared inventory is not a path-to-digest map")
            try:
                artifact_path_parts(name)
            except PipelineError as error:
                raise PipelineError(f"The native prepared inventory carries an unsafe member: {name!r}") from error
            if name.startswith("discovery/"):
                try:
                    data = archive.read(name)
                except KeyError as error:
                    raise PipelineError(f"The capture-request archive is missing {name!r}") from error
            else:
                try:
                    source = confined(derived, name)
                except PipelineError as error:
                    raise PipelineError(f"The derived prepared package carries an unsafe member: {name!r}") from error
                data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise PipelineError(f"The prepared member {name!r} differs from the native job")
            if name == "source.json":
                relative = "input/source.json"
            elif name == "capture-plan.json":
                relative = "evidence/capture-plan.json"
            elif name == "discovery/native-discovery.json":
                relative = "evidence/discovery.json"
            elif name.startswith("discovery/"):
                relative = f"evidence/discovery/{name.removeprefix('discovery/')}"
            elif name in native_names:
                relative = f"input/cad/{name}"
            elif name.startswith("records/"):
                relative = f"input/{name}"
            else:
                raise PipelineError(f"The prepared package carries an unknown member class: {name!r}")
            target = confined(tree, relative, exists=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    native_handoff = digest(native_inventory)
    raw_record = discovery.get("raw") if isinstance(discovery, dict) else None
    if not isinstance(raw_record, dict):
        raise PipelineError("The prepared discovery record carries no raw record")
    bindings = selection_bindings(raw_record.get("source_selection"))
    selected = bindings["selection"]["files"]
    excluded = bindings["selection"]["excluded_files"]
    if (
        bindings["files"] != native_inventory
        or raw_record.get("files") != bindings["files"]
        or bindings["handoff_sha256"] != native_handoff
        or bindings["upload_sha256"] != request_upload_sha256
        or bindings["main_assembly"] != main_assembly
        or {**excluded, **selected} != request_upload_files
    ):
        raise PipelineError("The prepared discovery selection does not bind the admitted and native inventories")
    native_runtime = native_tool.get("runtime") if isinstance(native_tool, Mapping) else None
    if (
        not isinstance(native_tool, dict)
        or native_tool.get("schema_version") != TOOL_SCHEMA
        or native_tool.get("pipeline_id") != PIPELINE_ID
        or not isinstance(native_runtime, dict)
        or native_runtime.get("role") != "native"
    ):
        raise PipelineError("The native recovery carries no native tool identity")
    validated_stages = validate_native_discovery_stage(
        native_stages,
        run_id=run_id,
        handoff_sha256=native_handoff,
        capture_state=capture_state,
    )
    request_record = _request_evidence(
        request_evidence,
        request_upload_sha256=request_upload_sha256,
        main_assembly=main_assembly,
    )
    portable = _portable_tool(portable_tool, native_tool)
    discovery = read_data(tree / "evidence" / "discovery.json")
    if (
        not isinstance(discovery, dict)
        or discovery.get("native_files") != native_inventory
        or discovery.get("handoff_sha256") != native_handoff
    ):
        raise PipelineError("The prepared discovery record does not bind the native inventory")
    package_files = {"source.json": prepared["source.json"]}
    package_files.update({f"cad/{name}": prepared[name] for name in native_names})
    package_files.update(
        {
            name: prepared[name]
            for name in prepared
            if name.startswith("records/")
        }
    )
    write_json(
        tree / "input" / "handoff.json",
        {
            "passed": True,
            "package_files": package_files,
            "input_receipt": {"inventory": native_inventory},
        },
    )
    write_json(
        tree / "transfer-manifest.json",
        {
            "schema_version": "solidworks-to-urdf.transfer/v1",
            "run_id": run_id,
            "handoff_sha256": native_handoff,
            "main_assembly": main_assembly,
            "native_tool": native_tool,
        },
    )
    for name in ("model", "urdf", "meshes"):
        (tree / name).mkdir()
    (tree / "reports").mkdir()
    write_json(tree / "reports" / "native-tool.json", native_tool)
    write_json(tree / "reports" / "native-stages.json", validated_stages)
    (tree / "input" / "request.json").write_bytes(request_evidence)
    write_json(tree / "tool.json", portable)
    write_delivery_manifest(
        tree,
        contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
        policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
    )
    return {
        "native_handoff": native_handoff,
        "native_inventory": native_inventory,
        "native_tool": native_tool,
        "native_stages_sha256": digest(validated_stages),
        "request_evidence_sha256": hashlib.sha256(request_evidence).hexdigest(),
        "portable_tool_sha256": digest(portable),
        "request_id": request_record["request_id"],
        "platform_config_digest": request_record["platform_config_digest"],
    }


def discovery_receipt(
    *,
    run_id: str,
    tree: Path,
    native_tool: dict,
    native_handoff: str,
    native_stages_sha256: str,
    request_evidence_sha256: str,
    portable_tool_sha256: str,
) -> dict:
    """The canonical completed-discover receipt for the reconstructed tree."""

    manifest = read_delivery_manifest(tree)
    subject = manifest.get("subject") or {}
    artifacts = [dict(record) for record in subject.get("files") or []]
    return {
        "schema_version": BUNDLE_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "stage": "discover",
        "state": next(iter(CONFIRMED_STAGE_STATES["discover"])),
        "passed": CONFIRMED_STAGE_PASSED["discover"],
        "subject_sha256": subject.get("digest"),
        "artifacts": artifacts,
        "handoff_sha256": native_handoff,
        "native_tool": native_tool,
        "native_stages_sha256": native_stages_sha256,
        "request_evidence_sha256": request_evidence_sha256,
        "portable_tool_sha256": portable_tool_sha256,
    }


def seal_discovery_stage(
    platform,
    store_root: Path,
    run_id: str,
    tree: Path,
    *,
    native_tool: dict,
    native_handoff: str,
    native_stages_sha256: str,
    request_evidence_sha256: str,
    portable_tool_sha256: str,
    settle_operations: bool = False,
) -> str:
    """Seal the reconstructed discover tree without settling the native owner."""

    receipt = discovery_receipt(
        run_id=run_id,
        tree=tree,
        native_tool=native_tool,
        native_handoff=native_handoff,
        native_stages_sha256=native_stages_sha256,
        request_evidence_sha256=request_evidence_sha256,
        portable_tool_sha256=portable_tool_sha256,
    )
    uri, completed = seal_stage_confirmation(store_root, run_id, "discover", tree, receipt)
    confirmed = platform.confirmed_stage(run_id, "discover")
    if confirmed is not None:
        if confirmed == completed:
            return str(uri)
        raise PipelineError("The discover stage is already confirmed with another identity")
    platform.confirm_stage(
        run_id=run_id,
        stage="discover",
        state="completed",
        receipt=completed,
        checkpoint_uri=uri,
        original_executed_at=None,
        settle_operations=settle_operations,
    )
    return str(uri)
