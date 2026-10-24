#!/usr/bin/env python3
"""Migrate one complete legacy Capture input closure into an isolated diagnostic artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from description_pipeline.artifacts import ArtifactStore  # noqa: E402
from description_pipeline.interface_records import check_policy_digest  # noqa: E402
from description_pipeline.io import (  # noqa: E402
    PipelineError,
    digest,
    file_digest,
    inventory,
    read_data,
    write_json,
)
from description_pipeline.pipeline.diagnostic import (  # noqa: E402
    DiagnosticAdmissionSpec,
    build_diagnostic_command,
)
from description_pipeline.pipeline.discover import (  # noqa: E402
    CONFIGURATION_KIND,
    DISCOVER_CHECKS_SCHEMA,
    DISCOVER_REQUEST_SCHEMA,
    DISCOVER_RESULT_SCHEMA,
    RULESET_KIND,
    SOURCE_SET_KIND,
)
from description_pipeline.pipeline.freeze import (  # noqa: E402
    FREEZE_CHECKS_SCHEMA,
    FREEZE_REQUEST_SCHEMA,
)
from description_pipeline.runtime import tool_record  # noqa: E402
from description_pipeline.schemas import (  # noqa: E402
    DIAGNOSTIC_ADMISSION_SCHEMA,
    NATIVE_TOOL_REQUIREMENT_KIND,
    TOOL_SCHEMA,
)
from description_pipeline.sources.solidworks.capture_package import (  # noqa: E402
    consume_sealed_definition,
)
from description_pipeline.sources.solidworks.discovery import (  # noqa: E402
    DISCOVERY_FILE,
    PLAN_FILE,
    PLAN_SCHEMA,
    SOURCE_FILE,
)
from description_pipeline.stages import CONTRACT_SHA256  # noqa: E402

MIGRATION_SCHEMA = "solidworks-to-urdf.historical-capture-diagnostic/v1"
MIGRATION_KIND = "historical_capture_diagnostic_input"
RECEIPT_SCHEMA = "solidworks-to-urdf.historical-capture-migration-receipt/v1"
_SHA256_LENGTH = 64


class HistoricalMigrationError(PipelineError):
    """The legacy closure is incomplete, drifted or incorrectly labelled."""

    def __init__(self, code: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = dict(detail or {})


@dataclass(frozen=True, slots=True)
class MigrationInputs:
    prepared: Path
    native_record: Path
    source_request: Path
    capture_request: Path


def _sha256(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise HistoricalMigrationError(
            "historical_capture.invalid_digest",
            {"where": where},
        )
    return value


def _regular_directory(value: Path, where: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise HistoricalMigrationError(
            "historical_capture.invalid_path",
            {"where": where, "path": str(path)},
        )
    return path.resolve()


def _regular_file(value: Path, where: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise HistoricalMigrationError(
            "historical_capture.invalid_path",
            {"where": where, "path": str(path)},
        )
    return path.resolve()


def _plain_inventory(root: Path) -> dict[str, str]:
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise HistoricalMigrationError(
                "historical_capture.unsafe_member",
                {"path": path.relative_to(root).as_posix()},
            )
        if not stat.S_ISREG(path.stat().st_mode) and not path.is_dir():
            raise HistoricalMigrationError(
                "historical_capture.unsafe_member",
                {"path": path.relative_to(root).as_posix()},
            )
    return inventory(root)


def _zip_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise HistoricalMigrationError(
            "historical_capture.unsafe_archive_member",
            {"path": value},
        )
    return value


def _capture_request_binding(
    archive: Path,
    prepared: Path,
    prepared_files: Mapping[str, str],
) -> dict[str, Any]:
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            name = _zip_name(entry.filename)
            if name in members:
                raise HistoricalMigrationError(
                    "historical_capture.archive_duplicate",
                    {"path": name},
                )
            members[name] = source.read(name)
    required = {
        "manifest.json",
        PLAN_FILE,
        SOURCE_FILE,
        "discovery/findings.json",
        "discovery/identity-resolution.json",
        DISCOVERY_FILE,
    }
    if set(members) != required:
        raise HistoricalMigrationError(
            "historical_capture.archive_inventory",
            {
                "missing": sorted(required - set(members)),
                "unknown": sorted(set(members) - required),
            },
        )
    manifest = json.loads(members.pop("manifest.json"))
    if not isinstance(manifest, dict):
        raise HistoricalMigrationError("historical_capture.archive_manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or files != {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in members.items()
    }:
        raise HistoricalMigrationError(
            "historical_capture.archive_manifest_mismatch",
            {"where": "files"},
        )
    for name, payload in members.items():
        source = prepared.joinpath(*PurePosixPath(name).parts)
        if (
            not source.is_file()
            or source.is_symlink()
            or source.read_bytes() != payload
            or prepared_files.get(name) != files[name]
        ):
            raise HistoricalMigrationError(
                "historical_capture.archive_prepared_mismatch",
                {"path": name},
            )
    prepared_sha256 = digest(dict(prepared_files))
    if manifest.get("prepared_sha256") != prepared_sha256:
        raise HistoricalMigrationError(
            "historical_capture.prepared_digest_mismatch",
            {
                "recorded": manifest.get("prepared_sha256"),
                "actual": prepared_sha256,
            },
        )
    return manifest


def _validate_identity(
    *,
    plan: Mapping[str, Any],
    source: Mapping[str, Any],
    resolution: Mapping[str, Any],
    source_request: Mapping[str, Any],
) -> dict[str, Any]:
    plan_identity = plan.get("identity")
    source_identity = source.get("identity")
    if not isinstance(plan_identity, dict) or not isinstance(source_identity, dict):
        raise HistoricalMigrationError(
            "historical_capture.identity_missing",
            {"where": "plan-or-source"},
        )
    for field in ("hardware_id", "revision"):
        if (
            plan_identity.get(field) != source_identity.get(field)
            or plan_identity.get(field) != resolution.get(field)
        ):
            raise HistoricalMigrationError(
                "historical_capture.identity_mismatch",
                {"field": field},
            )
    if (
        plan.get("run_id") != resolution.get("run_id")
        or plan.get("handoff_sha256") != resolution.get("handoff_sha256")
        or plan.get("main_assembly") != source_identity.get("main_assembly")
        or plan.get("main_assembly") != source_request.get("main_assembly")
        or plan_identity.get("hardware_id")
        != source_request.get("target_hardware")
        or source_identity.get("handoff_digest")
        != source_request.get("handoff_digest")
    ):
        raise HistoricalMigrationError(
            "historical_capture.identity_binding",
            {"run_id": plan.get("run_id")},
        )
    return {
        "hardware_id": plan_identity["hardware_id"],
        "revision": plan_identity["revision"],
        "main_assembly": plan["main_assembly"],
        "handoff_sha256": plan["handoff_sha256"],
        "upload_sha256": source_identity["handoff_digest"],
        "run_id": plan["run_id"],
    }


def validate_legacy_inputs(inputs: MigrationInputs) -> dict[str, Any]:
    prepared = _regular_directory(inputs.prepared, "prepared")
    native_record_path = _regular_file(inputs.native_record, "native_record")
    source_request_path = _regular_file(inputs.source_request, "source_request")
    capture_request_path = _regular_file(
        inputs.capture_request,
        "capture_request",
    )
    files = _plain_inventory(prepared)
    plan_path = prepared / PLAN_FILE
    source_path = prepared / SOURCE_FILE
    discovery_path = prepared / DISCOVERY_FILE
    identity_path = prepared / "discovery/identity-resolution.json"
    for path, role in (
        (plan_path, "plan"),
        (source_path, "source"),
        (discovery_path, "discovery"),
        (identity_path, "identity_resolution"),
    ):
        if not path.is_file() or path.is_symlink():
            raise HistoricalMigrationError(
                "historical_capture.member_missing",
                {"role": role, "path": str(path)},
            )
    plan = read_data(plan_path)
    source = read_data(source_path)
    discovery = read_data(discovery_path)
    resolution = read_data(identity_path)
    source_request = read_data(source_request_path)
    native_record = read_data(native_record_path)
    if not all(
        isinstance(value, dict)
        for value in (
            plan,
            source,
            discovery,
            resolution,
            source_request,
            native_record,
        )
    ):
        raise HistoricalMigrationError(
            "historical_capture.record_invalid",
            {"where": "json"},
        )
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise HistoricalMigrationError(
            "historical_capture.plan_schema",
            {"value": plan.get("schema_version")},
        )
    selection = plan.get("selection")
    selected = selection.get("files") if isinstance(selection, dict) else None
    if not isinstance(selected, dict) or not selected:
        raise HistoricalMigrationError(
            "historical_capture.plan_selection",
            {"where": "selection.files"},
        )
    actual_cad = {
        name: checksum
        for name, checksum in files.items()
        if name.casefold().endswith((".sldasm", ".sldprt"))
    }
    for name in selected:
        _zip_name(name)
    if actual_cad != selected:
        raise HistoricalMigrationError(
            "historical_capture.plan_prepared_mismatch",
            {
                "missing": sorted(set(selected) - set(actual_cad)),
                "unknown": sorted(set(actual_cad) - set(selected)),
                "changed": sorted(
                    name
                    for name in set(selected) & set(actual_cad)
                    if selected[name] != actual_cad[name]
                ),
            },
        )
    if (
        plan.get("handoff_sha256") != digest(selected)
        or plan.get("discovery_sha256") != file_digest(discovery_path)
        or plan.get("run_id") != discovery.get("run_id")
        or plan.get("handoff_sha256") != discovery.get("handoff_sha256")
    ):
        raise HistoricalMigrationError(
            "historical_capture.plan_snapshot_mismatch",
            {"run_id": plan.get("run_id")},
        )
    try:
        consume_sealed_definition(prepared)
    except PipelineError as error:
        raise HistoricalMigrationError(
            "historical_capture.legacy_contract",
            {"reason": str(error)[:1024]},
        ) from error
    identity = _validate_identity(
        plan=plan,
        source=source,
        resolution=resolution,
        source_request=source_request,
    )
    request_manifest = _capture_request_binding(
        capture_request_path,
        prepared,
        files,
    )
    native_record_files = native_record.get("files")
    source_selection = native_record.get("source_selection")
    native_tool = request_manifest.get("native_tool")
    if not isinstance(native_tool, dict):
        raise HistoricalMigrationError(
            "historical_capture.producer_binding",
            {"where": "native-tool"},
        )
    if request_manifest.get("native_tool_sha256") != digest(native_tool):
        raise HistoricalMigrationError(
            "historical_capture.producer_binding",
            {"where": "native-tool-digest"},
        )
    if (
        request_manifest.get("run_id") != plan.get("run_id")
        or request_manifest.get("handoff_sha256") != plan.get("handoff_sha256")
        or request_manifest.get("main_assembly") != plan.get("main_assembly")
        or request_manifest.get("upload_sha256") != identity["upload_sha256"]
        or request_manifest.get("prepared_sha256") != digest(files)
        or native_record_files != selected
        or not isinstance(source_selection, dict)
        or source_selection.get("reader_sha256")
        != native_tool.get("source_sha256")
        or native_record.get("generator") != "solidworks-native-reader"
    ):
        raise HistoricalMigrationError(
            "historical_capture.producer_binding",
            {"run_id": plan.get("run_id")},
        )
    return {
        "prepared": prepared,
        "prepared_files": files,
        "plan": plan,
        "source": source,
        "discovery": discovery,
        "resolution": resolution,
        "source_request": source_request,
        "native_record": native_record,
        "capture_request_manifest": request_manifest,
        "native_hashes": {
            "plan": file_digest(plan_path),
            "source": file_digest(source_path),
            "discovery": file_digest(discovery_path),
            "identity_resolution": file_digest(identity_path),
            "native_record": file_digest(native_record_path),
            "source_request": file_digest(source_request_path),
            "capture_request": file_digest(capture_request_path),
            "prepared": digest(files),
        },
        "identity": identity,
    }


def _seal_current_diagnostic(
    *,
    store: ArtifactStore,
    root: Path,
    validated: Mapping[str, Any],
    migration_tool: Mapping[str, Any],
    native_tool: Mapping[str, Any],
    created_at: datetime,
) -> tuple[DiagnosticAdmissionSpec, dict[str, Any]]:
    """Build the current Capture contract around the immutable legacy bytes."""

    plan = validated["plan"]
    prepared = validated["prepared"]
    prepared_files = dict(validated["prepared_files"])
    selected = dict(plan["selection"]["files"])
    old_tool = dict(
        validated["capture_request_manifest"]["native_tool"]
    )
    source_request = dict(validated["source_request"])
    migration_source = migration_tool["source_sha256"]

    originals_root = root / "originals"
    for name, checksum in selected.items():
        source = prepared.joinpath(*PurePosixPath(name).parts)
        target = originals_root.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if file_digest(target) != checksum:
            raise HistoricalMigrationError(
                "historical_capture.originals_changed",
                {"path": name},
            )
    originals = store.seal_tree(
        originals_root,
        kind=SOURCE_SET_KIND,
        producer_run_id=plan["run_id"],
        producer_stage="freeze",
        producer_tool_sha256=migration_source,
    )

    ruleset_root = root / "ruleset"
    write_json(
        ruleset_root / "catalog.json",
        {
            "schema_version": "solidworks-to-urdf.historical-migration/v1",
            "source_label": plan["run_id"].rsplit("-", 1)[0],
        },
    )
    ruleset = store.seal_tree(
        ruleset_root,
        kind=RULESET_KIND,
        producer_run_id=plan["run_id"],
        producer_stage="freeze",
        producer_tool_sha256=migration_source,
    )
    configuration_root = root / "configuration"
    write_json(
        configuration_root / "config-snapshot.json",
        {
            "schema_version": "solidworks-to-urdf.historical-migration/v1",
            "platform_config_sha256": source_request[
                "platform_config_digest"
            ],
        },
    )
    configuration = store.seal_tree(
        configuration_root,
        kind=CONFIGURATION_KIND,
        producer_run_id=plan["run_id"],
        producer_stage="freeze",
        producer_tool_sha256=migration_source,
    )
    current_tool = dict(native_tool)
    if (
        current_tool.get("schema_version") != TOOL_SCHEMA
        or not isinstance(current_tool.get("runtime"), Mapping)
        or current_tool["runtime"].get("role") != "native"
    ):
        raise HistoricalMigrationError(
            "historical_capture.current_native_tool",
            {"where": "identity"},
        )
    current_tool_source = _sha256(
        current_tool.get("source_sha256"),
        "current_native_tool.source_sha256",
    )
    current_tool_record = digest(current_tool)
    requirement_root = root / "native-tool-requirement"
    requirement_root.mkdir()
    (requirement_root / "tool.json").write_bytes(
        json.dumps(
            current_tool,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    requirement = store.seal_tree(
        requirement_root,
        kind=NATIVE_TOOL_REQUIREMENT_KIND,
        producer_run_id=None,
        producer_stage=None,
        producer_tool_sha256=current_tool_source,
    )

    freeze_root = root / "freeze"
    freeze_request = {
        "schema_version": FREEZE_REQUEST_SCHEMA,
        "request_id": source_request["request_id"],
        "principal": "diagnostic:historical-capture",
        "target_hardware": plan["identity"]["hardware_id"],
        "purpose": source_request["purpose"],
        "change_summary": source_request["change_summary"],
        "main_assembly": plan["main_assembly"],
        "ruleset_version": "historical-migration-v1",
        "originals_artifact_id": originals.artifact_id,
        "ruleset_artifact_id": ruleset.artifact_id,
        "configuration_artifact_id": configuration.artifact_id,
        "upload_inventory_sha256": validated["identity"]["upload_sha256"],
        "ruleset_catalog_sha256": ruleset.set_sha256,
        "platform_config_sha256": source_request[
            "platform_config_digest"
        ],
    }
    freeze_request_digest = digest(freeze_request)
    write_json(freeze_root / "stage-request.json", freeze_request)
    write_json(freeze_root / "request.json", source_request)
    write_json(
        freeze_root / "inputs.json",
        {
            "artifacts": {
                "configuration": configuration.to_record(),
                "originals": originals.to_record(),
                "ruleset": ruleset.to_record(),
            },
            "upload_inventory": selected,
        },
    )
    write_json(
        freeze_root / "checks.json",
        {
            "schema_version": FREEZE_CHECKS_SCHEMA,
            "request_digest": freeze_request_digest,
            "checks": [
                {
                    "id": "historical.migration",
                    "state": "passed",
                    "details": {
                        "diagnostic_only": True,
                        "legacy_run_id": plan["run_id"],
                    },
                }
            ],
        },
    )
    freeze = store.seal_tree(
        freeze_root,
        kind="stage_output:freeze",
        producer_run_id=plan["run_id"],
        producer_stage="freeze",
        producer_tool_sha256=migration_source,
    )

    discover_root = root / "discover"
    shutil.copytree(
        prepared,
        discover_root / "prepared",
        symlinks=False,
    )
    observation = (
        prepared / DISCOVERY_FILE
    ).read_bytes()
    (discover_root / "native-observation").mkdir()
    (discover_root / "native-observation/observation.json").write_bytes(
        observation
    )
    observation_sha256 = hashlib.sha256(observation).hexdigest()
    discover_request = {
        "schema_version": DISCOVER_REQUEST_SCHEMA,
        "freeze_artifact_id": freeze.artifact_id,
        "source_set_artifact_id": originals.artifact_id,
        "ruleset_artifact_id": ruleset.artifact_id,
        "configuration_artifact_id": configuration.artifact_id,
        "source_files": selected,
        "source_inventory_sha256": validated["identity"][
            "upload_sha256"
        ],
        "main_assembly": plan["main_assembly"],
        "purpose": source_request["purpose"],
        "ruleset_version": freeze_request["ruleset_version"],
        "ruleset_catalog_sha256": ruleset.set_sha256,
        "platform_config_sha256": source_request[
            "platform_config_digest"
        ],
        "submission_identity": {
            "hardware_id": plan["identity"]["hardware_id"],
            "owner": None,
            "change_summary": source_request["change_summary"],
            "parent_revision": None,
            "control_reference": None,
            "as_of": None,
            "inventory_digest": None,
        },
        "expected_native_tool_sha256": old_tool["source_sha256"],
        "expected_native_tool_record_sha256": digest(old_tool),
        "native_tool_requirement_artifact_id": requirement.artifact_id,
    }
    discover_request_digest = digest(discover_request)
    discover_command_id = "historical-discover-" + discover_request_digest
    write_json(discover_root / "request.json", discover_request)
    write_json(
        discover_root / "checks.json",
        {
            "schema_version": DISCOVER_CHECKS_SCHEMA,
            "command_id": discover_command_id,
            "request_digest": discover_request_digest,
            "checks": [
                {
                    "id": "native.observation",
                    "state": "passed",
                    "details": {
                        "native_tool_sha256": digest(old_tool),
                        "observation_sha256": observation_sha256,
                    },
                }
            ],
        },
    )
    write_json(
        discover_root / "result.json",
        {
            "schema_version": DISCOVER_RESULT_SCHEMA,
            "run_id": plan["run_id"],
            "command_id": discover_command_id,
            "request_digest": discover_request_digest,
            "passed": True,
            "native_tool": old_tool,
            "native_observation_sha256": observation_sha256,
            "prepared_sha256": digest(prepared_files),
            "prepared_files": prepared_files,
            "selection": validated["native_record"]["source_selection"],
            "source_set_artifact": originals.to_record(),
            "ruleset_artifact": ruleset.to_record(),
            "configuration_artifact": configuration.to_record(),
        },
    )
    discover = store.seal_tree(
        discover_root,
        kind="stage_output:discover",
        producer_run_id=plan["run_id"],
        producer_stage="discover",
        producer_tool_sha256=migration_source,
    )

    capture_request = {
        "effect_key": "capture-native",
        "handoff_sha256": plan["handoff_sha256"],
        "main_assembly": plan["main_assembly"],
        "native_tool_source_sha256": current_tool_source,
        "native_tool_record_sha256": current_tool_record,
        "native_tool_requirement_artifact_id": requirement.artifact_id,
        "prepared_sha256": digest(prepared_files),
        "upload_sha256": validated["identity"]["upload_sha256"],
    }
    variables = {
        "config_snapshot_sha256": source_request[
            "platform_config_digest"
        ],
        "contract_sha256": CONTRACT_SHA256,
        "created_at": created_at,
        "expected_executor_sha256": migration_source,
        "input_artifacts": (freeze, discover, requirement),
        "policy_sha256": check_policy_digest(),
        "request": capture_request,
        "request_id": "historical-capture-" + plan["run_id"],
        "run_id": plan["run_id"],
        "schema_version": DIAGNOSTIC_ADMISSION_SCHEMA,
        "target_stage": "capture",
    }
    admission = DiagnosticAdmissionSpec(**variables)
    build_diagnostic_command(admission)
    return admission, {
        "configuration": configuration.to_record(),
        "freeze": freeze.to_record(),
        "discover": discover.to_record(),
        "native_tool_requirement": requirement.to_record(),
        "originals": originals.to_record(),
        "ruleset": ruleset.to_record(),
    }


def migrate_historical_capture_input(
    inputs: MigrationInputs,
    *,
    artifact_store: Path,
    work_root: Path,
    migration_tool: Mapping[str, Any] | None = None,
    native_tool: Mapping[str, Any] | None = None,
    expected_run_id: str | None = None,
    expected_prepared_sha256: str | None = None,
    diagnostic_created_at: datetime | None = None,
) -> dict[str, Any]:
    validated = validate_legacy_inputs(inputs)
    plan = validated["plan"]
    run_id = plan["run_id"]
    if expected_run_id is not None and run_id != expected_run_id:
        raise HistoricalMigrationError(
            "historical_capture.source_label_mismatch",
            {"expected": expected_run_id, "actual": run_id},
        )
    prepared_sha256 = validated["native_hashes"]["prepared"]
    if (
        expected_prepared_sha256 is not None
        and prepared_sha256 != _sha256(
            expected_prepared_sha256,
            "expected_prepared_sha256",
        )
    ):
        raise HistoricalMigrationError(
            "historical_capture.prepared_label_mismatch",
            {"expected": expected_prepared_sha256, "actual": prepared_sha256},
        )
    tool = dict(migration_tool or tool_record(role="portable"))
    if tool.get("schema_version") != TOOL_SCHEMA:
        raise HistoricalMigrationError(
            "historical_capture.tool_invalid",
            {"where": "schema_version"},
        )
    tool_source_sha256 = _sha256(
        tool.get("source_sha256"),
        "migration_tool.source_sha256",
    )
    current_native_tool = dict(
        native_tool or tool_record(role="native")
    )
    created_at = diagnostic_created_at or datetime.now(UTC)
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise HistoricalMigrationError(
            "historical_capture.diagnostic_time",
            {"where": "created_at"},
        )
    store = ArtifactStore(artifact_store)
    work_root = _regular_directory(work_root, "work_root")
    if work_root.is_relative_to(store.root) or store.root.is_relative_to(work_root):
        raise HistoricalMigrationError(
            "historical_capture.work_root_overlap",
            {"work_root": str(work_root), "artifact_root": str(store.root)},
        )
    with tempfile.TemporaryDirectory(
        prefix="historical-capture-migration-",
        dir=work_root,
    ) as temporary:
        staging = Path(temporary) / "input"
        shutil.copytree(
            validated["prepared"],
            staging / "legacy/prepared",
            symlinks=False,
        )
        shutil.copyfile(
            inputs.native_record.resolve(),
            staging / "legacy/native-record.json",
        )
        shutil.copyfile(
            inputs.source_request.resolve(),
            staging / "legacy/source-request.json",
        )
        shutil.copyfile(
            inputs.capture_request.resolve(),
            staging / "legacy/capture-request.zip",
        )
        native_record = validated["native_record"]
        source_selection = native_record["source_selection"]
        migration = {
            "schema_version": MIGRATION_SCHEMA,
            "diagnostic_only": True,
            "execution_authorized": False,
            "formal_checkpoint_eligible": False,
            "source_label": run_id.rsplit("-", 1)[0],
            "run_id": run_id,
            "identity": validated["identity"],
            "hashes": validated["native_hashes"],
            "plan": {
                "schema_version": plan["schema_version"],
                "identity": plan["identity"],
                "main_assembly": plan["main_assembly"],
                "handoff_sha256": plan["handoff_sha256"],
                "discovery_sha256": plan["discovery_sha256"],
            },
            "original_producer": {
                "kind": native_record["generator"],
                "schema_version": native_record.get("schema_version"),
                "reader_sha256": source_selection["reader_sha256"],
                "record_sha256": validated["native_hashes"]["native_record"],
                "generator_version": native_record.get("generator_version"),
                "release": native_record.get("release"),
            },
            "migration_tool": {
                "source_sha256": tool_source_sha256,
                "record_sha256": digest(tool),
                "runtime": dict(tool.get("runtime") or {}),
                "release": tool.get("release"),
            },
        }
        write_json(staging / "migration.json", migration)
        admission, current_inputs = _seal_current_diagnostic(
            store=store,
            root=Path(temporary) / "current",
            validated=validated,
            migration_tool=tool,
            native_tool=current_native_tool,
            created_at=created_at,
        )
        write_json(
            staging / "diagnostic-admission.json",
            admission.to_record(),
        )
        artifact = store.seal_tree(
            staging,
            kind=MIGRATION_KIND,
            producer_run_id=run_id,
            producer_stage="capture",
            producer_tool_sha256=tool_source_sha256,
        )
    return {
        "schema_version": RECEIPT_SCHEMA,
        "created_at": datetime.now().astimezone().isoformat(),
        "artifact": artifact.to_record(),
        "migration": migration,
        "diagnostic_admission": admission.to_record(),
        "diagnostic_command": build_diagnostic_command(admission).to_record(),
        "current_inputs": current_inputs,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--native-record", type=Path, required=True)
    parser.add_argument("--source-request", type=Path, required=True)
    parser.add_argument("--capture-request", type=Path, required=True)
    parser.add_argument("--artifact-store", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--tool-record", type=Path)
    parser.add_argument("--current-native-tool", type=Path)
    parser.add_argument("--expected-run-id")
    parser.add_argument("--expected-prepared-sha256")
    parser.add_argument("--diagnostic-created-at", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tool = read_data(args.tool_record) if args.tool_record is not None else None
        current_native_tool = (
            read_data(args.current_native_tool)
            if args.current_native_tool is not None
            else None
        )
        receipt = migrate_historical_capture_input(
            MigrationInputs(
                prepared=args.prepared,
                native_record=args.native_record,
                source_request=args.source_request,
                capture_request=args.capture_request,
            ),
            artifact_store=args.artifact_store,
            work_root=args.work_root,
            migration_tool=tool,
            native_tool=current_native_tool,
            expected_run_id=args.expected_run_id,
            expected_prepared_sha256=args.expected_prepared_sha256,
            diagnostic_created_at=datetime.fromisoformat(
                args.diagnostic_created_at.replace("Z", "+00:00")
            ),
        )
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.receipt, receipt)
    except (
        HistoricalMigrationError,
        OSError,
        UnicodeError,
        ValueError,
        zipfile.BadZipFile,
    ) as error:
        code = getattr(error, "code", "historical_capture.migration_failed")
        print(f"{code}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
