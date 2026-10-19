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
import zipfile
from collections.abc import Mapping
from pathlib import Path

from ..delivery import BUNDLE_SCHEMA, PIPELINE_ID, read_delivery_manifest, write_delivery_manifest
from ..io import PipelineError, artifact_path_parts, confined, digest, read_data, write_json
from ..interface_records import check_policy_digest, CHECK_POLICY_SCHEMA
from ..runtime import tool_record
from ..stages import CONFIRMED_STAGE_PASSED, CONFIRMED_STAGE_STATES, CONTRACT, CONTRACT_SHA256
from .storage import seal_stage_confirmation


def validate_native_discovery_completed(events: object) -> int:
    """Require one completed discover event and return its position."""

    rows = [event for event in events or () if isinstance(event, dict)]
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

    rows = [event for event in events or () if isinstance(event, dict)]
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

    rows = [event for event in events or () if isinstance(event, dict)]
    if any(event.get("state") == "passed" for event in rows):
        raise PipelineError("The native failure carries a passed event")
    failed = [index for index, event in enumerate(rows) if event.get("state") == "failed"]
    if len(failed) != 1:
        raise PipelineError("The native failure must carry exactly one failed terminal event")
    if failed[0] != len(rows) - 1:
        raise PipelineError("The native failed terminal event is not the last event")
    return failed[0]


def build_discovery_tree_from_prepared(
    *,
    tree: Path,
    derived: Path,
    archive_path: Path,
    prepared: Mapping[str, str],
    native_tool: dict,
    run_id: str,
    main_assembly: str,
    native_stages: dict | None = None,
    request_evidence: bytes | None = None,
    portable_tool: dict | None = None,
) -> dict:
    """Reconstruct the completed discover tree from the prepared package.

    Only the prepared members named by the failed job are copied.  The native
    originals move under ``input/cad``, the discovery record and capture plan
    move to their declared evidence paths, and every member is hashed before
    and after the copy.  No native or CAD operation occurs here.
    """

    if not prepared:
        raise PipelineError("The native prepared inventory is empty")
    if "source.json" not in prepared:
        raise PipelineError("The native prepared inventory carries no source identity")
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
            elif name in native_names:
                relative = f"input/cad/{name}"
            elif name.startswith("records/"):
                relative = f"input/{name}"
            else:
                relative = name if name.startswith("discovery/") else None
                if relative is None:
                    raise PipelineError(f"The prepared package carries an unknown member class: {name!r}")
            target = confined(tree, relative, exists=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    native_handoff = digest(native_inventory)
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
    if isinstance(native_stages, dict):
        write_json(tree / "reports" / "native-stages.json", native_stages)
    if isinstance(request_evidence, bytes):
        (tree / "input" / "request.json").write_bytes(request_evidence)
    write_json(tree / "tool.json", portable_tool or tool_record(role="portable"))
    write_delivery_manifest(
        tree,
        contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
        policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
    )
    return {
        "native_handoff": native_handoff,
        "native_inventory": native_inventory,
        "native_tool": native_tool,
        "native_stages_sha256": digest(native_stages) if isinstance(native_stages, dict) else None,
    }


def discovery_receipt(
    *,
    run_id: str,
    tree: Path,
    native_tool: dict,
    native_handoff: str,
    native_stages_sha256: str | None = None,
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
    }


def seal_discovery_stage(
    platform,
    store_root: Path,
    run_id: str,
    tree: Path,
    *,
    native_tool: dict,
    native_handoff: str,
    native_stages_sha256: str | None = None,
    settle_operations: bool = False,
) -> str:
    """Seal the reconstructed discover tree without settling the native owner."""

    receipt = discovery_receipt(
        run_id=run_id,
        tree=tree,
        native_tool=native_tool,
        native_handoff=native_handoff,
        native_stages_sha256=native_stages_sha256,
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
