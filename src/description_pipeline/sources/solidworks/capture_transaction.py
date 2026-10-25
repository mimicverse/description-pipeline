"""Native Capture transaction over one prepared package."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

from ... import __version__
from ...interface_records import (
    record_archive_path,
    validate_source_record,
)
from ...io import (
    PipelineError,
    confined,
    digest,
    file_digest,
    inventory,
    read_data,
    write_json,
)
from ...sources.snapshot import verify_snapshot
from ...stages import checked
from .capture_package import (
    CAPTURE_EVIDENCE_REQUESTS,
    consume_sealed_definition,
)
from .discovery import (
    DISCOVERY_FILE,
    FINDINGS_FILE,
    IDENTITY_FILE,
    PLAN_FILE,
    native_component_identity,
)
from .freeze import capture_prepared
from .revision import package_inventory
from .selection import verify_selection


def _state(emit, stage: str, state: str, **details) -> None:
    if emit is not None:
        emit({"stage": stage, "state": state, **details})


def _require(condition: object, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def inspect_prepared_package(package: Path) -> dict:
    """Validate the prepared package's source identity without changing it."""

    package = Path(package)
    report = {
        "passed": True,
        "errors": [],
        "warnings": [],
        "input": None,
        "resolved": None,
        "input_receipt": None,
        "cad_revision": None,
    }
    path = package / "source.json"
    if not path.is_file():
        report["warnings"].append(
            {
                "code": "input.source_identity_pending",
                "message": (
                    "CAD administrative identity is incomplete; "
                    "publication is unavailable"
                ),
                "detail": {"publication_blocker": "source_identity"},
            }
        )
        return report
    try:
        source = validate_source_record(read_data(path))
        record_path = package / DISCOVERY_FILE
        if record_path.is_file():
            record = read_data(record_path)
            receipt = {
                str(name): str(value)
                for name, value in (record.get("native_files") or {}).items()
            }
        elif (package / "cad").is_dir():
            receipt = dict(inventory(package / "cad"))
        else:
            receipt = dict(package_inventory(package))
    except (PipelineError, OSError, ValueError) as error:
        report["passed"] = False
        report["errors"].append(
            {
                "code": "input.source_identity",
                "message": str(error),
                "detail": None,
            }
        )
        return report
    identity = source["identity"]
    metadata = source["metadata"]
    report["main_assembly"] = identity["main_assembly"]
    report["configuration"] = identity["configuration"]
    report["input"] = {
        "hardware_id": identity["hardware_id"],
        "source": {
            "assembly": identity["main_assembly"],
            "configuration": identity["configuration"],
        },
    }
    report["cad_revision"] = {
        "hardware_id": identity["hardware_id"],
        "revision": metadata["revision"],
        "owner": metadata["owner"],
        "control": metadata["control"],
        "change_summary": metadata["change_summary"],
    }
    report["input_receipt"] = {"inventory": dict(sorted(receipt.items()))}
    return report


def _archive_input(package: Path, staging: Path) -> None:
    """Archive identity, CAD originals and controlled records once."""

    payload = read_data(package / DISCOVERY_FILE)
    native_files = (
        payload.get("native_files")
        if isinstance(payload.get("native_files"), dict)
        else {}
    )
    destination = staging / "input"
    (destination / "cad").mkdir(parents=True, exist_ok=True)
    (destination / "records").mkdir(parents=True, exist_ok=True)
    archived: dict[str, str] = {}
    for relative in sorted(native_files):
        target = confined(
            destination / "cad",
            relative,
            exists=False,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(confined(package, relative), target)
        archived[relative] = file_digest(target)
    if archived != {
        name: str(value) for name, value in native_files.items()
    }:
        raise PipelineError(
            "Archived CAD originals differ from the frozen inventory"
        )
    entries = list(payload.get("records") or [])
    if isinstance(payload.get("budget"), dict):
        entries.append(payload["budget"])
    copied_records: dict[str, tuple[str, str]] = {}
    for entry in entries:
        package_file = record_archive_path(
            str(entry.get("package_file") or "")
        )
        casefolded = package_file.casefold()
        source = confined(package, package_file)
        checksum = file_digest(source)
        existing = copied_records.get(casefolded)
        if existing is not None:
            if existing != (package_file, checksum):
                raise PipelineError(
                    f"Controlled record archive repeats a path: {package_file}"
                )
            continue
        copied_records[casefolded] = (package_file, checksum)
        target = confined(
            destination,
            package_file,
            exists=False,
        )
        if target.exists():
            raise PipelineError(
                "Controlled record archive would overwrite a file: "
                f"{package_file}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    source_path = package / "source.json"
    if source_path.is_file():
        shutil.copyfile(source_path, destination / "source.json")
    sidecars = [DISCOVERY_FILE, FINDINGS_FILE]
    if confined(package, IDENTITY_FILE, exists=False).is_file():
        sidecars.append(IDENTITY_FILE)
    for relative in sidecars:
        target = confined(destination, relative, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(confined(package, relative), target)


def _rehome_evidence(staging: Path, package: Path) -> None:
    """Project one native snapshot into the delivery evidence layout."""

    evidence = staging / "evidence"
    blocks = {
        "scene": "raw/scene_raw.json",
        "mass_closure": "raw/mass_closure.json",
        "material_provenance": "raw/material_provenance.json",
        "dependency_closure": "raw/dependency_closure.json",
        "link_group_mass_properties": (
            "raw/link_group_mass_properties.json"
        ),
        "effective_instances": "raw/effective_instances.json",
        "geometry": "raw/geometry.json",
        "axes": "raw/axis_references.json",
        "native_capture_facts": "raw/native_capture_facts.json",
        "collection": "evidence/collection.json",
    }
    capture = {
        name: read_data(evidence / relative)
        for name, relative in blocks.items()
    }
    capture["components"] = [
        {"name": str(name), "mass_properties": value}
        for name, value in sorted(
            (read_data(evidence / "raw/mass_properties.json") or {}).items()
        )
    ]
    capture["datums"] = [
        {"name": str(name), "matrix": matrix}
        for name, matrix in sorted(
            (
                read_data(
                    evidence / "raw/coordinate_systems.json"
                )
                or {}
            ).items()
        )
    ]
    (evidence / PLAN_FILE).write_bytes((package / PLAN_FILE).read_bytes())
    write_json(evidence / "capture.json", capture)
    write_json(
        evidence / "native-runtime.json",
        read_data(evidence / "evidence/environment.json"),
    )
    payload = read_data(package / DISCOVERY_FILE)
    identity = (
        payload.get("identity")
        if isinstance(payload.get("identity"), dict)
        else {}
    )
    rows = [
        {"path": name, "sha256": checksum}
        for name, checksum in sorted(
            (payload.get("native_files") or {}).items()
        )
    ]
    write_json(
        evidence / "discovery.json",
        {
            **payload,
            "files": rows,
            "native_inventory_sha256": digest(rows),
            "robot_name": (
                identity.get("robot_name")
                or identity.get("robot_name_technical")
                or "robot"
            ),
        },
    )
    manifest_path = evidence / "manifest.json"
    manifest = read_data(manifest_path)
    files = dict(manifest.get("files") or {})
    for name in (
        PLAN_FILE,
        "capture.json",
        "discovery.json",
        "native-runtime.json",
    ):
        files[name] = file_digest(evidence / name)
    manifest["files"] = dict(sorted(files.items()))
    write_json(manifest_path, manifest)


def _snapshot_details(staging: Path, manifest: dict) -> dict:
    manifest_path = staging / "evidence/manifest.json"
    raw = manifest_path.read_bytes()
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise PipelineError(
            f"Evidence manifest is not readable JSON: {error}"
        ) from error
    if parsed != manifest:
        raise PipelineError(
            "Evidence manifest changed after verification"
        )
    manifest_hash = hashlib.sha256(raw).hexdigest()
    return {
        "manifest_sha256": manifest_hash,
        "files": {
            "evidence/manifest.json": manifest_hash,
            **{
                "evidence/" + path: checksum
                for path, checksum in manifest["files"].items()
            },
        },
    }


def _require_plan_against_capture(
    staging: Path,
    plan: dict,
) -> None:
    """Require planned objects, datums and evidence in the actual capture."""

    evidence = staging / "evidence"
    captured = read_data(evidence / "raw" / "mass_properties.json")
    captured_names = {str(name) for name in (captured or {})}
    discovery = read_data(evidence / "discovery.json")
    raw_discovery = (
        discovery.get("raw")
        if isinstance(discovery, dict)
        else None
    )
    native_components = (
        raw_discovery.get("components")
        if isinstance(raw_discovery, dict)
        else None
    )
    if (
        not isinstance(native_components, list)
        or any(
            not isinstance(item, dict)
            for item in native_components
        )
    ):
        raise PipelineError(
            "The capture carries no valid sealed native component inventory"
        )
    all_native_list = [
        native_component_identity(item)
        for item in native_components
    ]
    if len(set(all_native_list)) != len(all_native_list):
        raise PipelineError(
            "The capture repeats a sealed native instance identity"
        )
    all_native_names = set(all_native_list)
    active_native = [
        item for item in native_components if not item.get("suppressed")
    ]
    planned_list = [str(item["name"]) for item in plan["objects"]]
    native_list = [
        native_component_identity(item) for item in active_native
    ]
    if (
        len(set(planned_list)) != len(planned_list)
        or len(set(native_list)) != len(native_list)
    ):
        raise PipelineError(
            "The capture repeats a sealed native instance identity"
        )
    planned_names = set(planned_list)
    native_names = set(native_list)
    native_documents = {
        native_component_identity(item): str(
            item.get("document") or ""
        )
        for item in active_native
    }
    container_names = {
        name
        for name in native_names
        if native_documents[name].lower().endswith(".sldasm")
        or any(
            other != name and other.startswith(name + "/")
            for other in all_native_names
        )
    }
    planned_mass_names = {
        name for name in native_names if name not in container_names
    }
    scene = read_data(evidence / "raw" / "scene_raw.json")
    scene_components = (
        scene.get("components") if isinstance(scene, dict) else None
    )
    if (
        not isinstance(scene_components, list)
        or any(
            not isinstance(item, dict)
            for item in scene_components
        )
    ):
        raise PipelineError(
            "The capture scene has no valid native component inventory"
        )
    scene_list = [str(item.get("name")) for item in scene_components]
    if len(set(scene_list)) != len(scene_list):
        raise PipelineError(
            "The capture scene repeats a native component identity"
        )
    scene_types = {
        str(item.get("name")): item.get("document_type")
        for item in scene_components
    }
    if (
        planned_names != native_names
        or captured_names != planned_mass_names
        or set(scene_types) != planned_mass_names
        or any(kind != "part" for kind in scene_types.values())
    ):
        raise PipelineError(
            "The capture did not read every planned native instance"
        )
    if plan["datums"]:
        systems = read_data(
            evidence / "raw" / "coordinate_systems.json"
        )
        known = {str(name) for name in (systems or {})}
        if not set(map(str, plan["datums"])) <= known:
            raise PipelineError(
                "The capture did not read every planned datum"
            )
    for requested in plan["evidence_requests"]:
        requested = str(requested)
        target = evidence / requested.removeprefix("evidence/")
        if requested.endswith("/"):
            if not target.is_dir():
                raise PipelineError(
                    "The capture did not produce a planned evidence item: "
                    f"{requested}"
                )
        elif not target.is_file():
            raise PipelineError(
                "The capture did not produce a planned evidence item: "
                f"{requested}"
            )


def capture_native_package(
    package: Path,
    staging: Path,
    *,
    expected_inputs=None,
    handoff_sha256=None,
    backend=None,
    on_event=None,
    on_activity=None,
):
    """Inspect, archive and capture one complete prepared package."""

    _state(on_event, "capture", "running")

    def inspect_and_archive():
        if (
            expected_inputs is not None
            and package_inventory(package) != expected_inputs
        ):
            raise PipelineError(
                "Prepared inputs changed after discovery"
            )
        report = inspect_prepared_package(package)
        if report.get("passed") is not True:
            write_json(staging / "input/handoff.json", report)
            error = PipelineError(
                "The CAD package does not meet the input specification"
            )
            error.details = {"errors": report.get("errors", [])}
            raise error
        _sealed_source, plan = consume_sealed_definition(package)
        main = str(plan["main_assembly"] or "")
        assembly = package / main
        if not assembly.is_file():
            raise PipelineError(
                "The sealed capture plan names no staged main assembly"
            )
        configuration = plan.get("configuration")
        if not isinstance(configuration, str) or not configuration:
            raise PipelineError(
                "The sealed capture plan carries no saved configuration"
            )
        discovery = read_data(package / DISCOVERY_FILE)
        native_files = (
            discovery.get("native_files")
            if isinstance(discovery, dict)
            else None
        )
        if (
            not isinstance(native_files, dict)
            or not native_files
            or any(
                not isinstance(name, str)
                or not isinstance(checksum, str)
                for name, checksum in native_files.items()
            )
        ):
            raise PipelineError(
                "The sealed discovery record carries no CAD file inventory"
            )
        raw = discovery.get("raw") if isinstance(discovery, dict) else None
        source_selection = (
            raw.get("source_selection")
            if isinstance(raw, dict)
            else None
        )
        if not isinstance(source_selection, dict):
            raise PipelineError(
                "The sealed discovery record carries no source selection"
            )
        selected = source_selection.get("files")
        excluded = source_selection.get("excluded_files")
        if (
            not isinstance(selected, dict)
            or not isinstance(excluded, dict)
            or set(selected) & set(excluded)
            or selected != native_files
            or selected != raw.get("files")
        ):
            raise PipelineError(
                "The sealed discovery source selection differs from its file inventory"
            )
        verify_selection(
            source_selection,
            {**excluded, **selected},
            selected,
            reader_sha256=source_selection.get("reader_sha256"),
        )
        prepared_files = package_inventory(package)
        _archive_input(package, staging)
        report["package_files"] = {
            name: checksum
            for name, checksum in sorted(
                inventory(staging / "input").items()
            )
            if name != "request.json"
        }
        source = {
            "provider": "solidworks",
            "assembly": str(assembly.resolve()),
            "configuration": configuration,
            "geometry": {"enabled": True, "format": "stl_binary"},
            "coordinate_systems": [
                str(name) for name in plan["datums"]
            ],
            "groups": plan["groups"],
            "axis_requests": plan["axis_requests"],
        }
        definition = {
            "source": source,
            "plan": plan,
            "native_files": dict(native_files),
        }
        write_json(staging / "input/handoff.json", report)
        return definition, report, prepared_files

    definition, input_report, original_files = checked(
        on_event,
        "capture",
        "input",
        "input.valid",
        inspect_and_archive,
        describe=lambda value: {
            "passed": value[1]["passed"],
            "handoff_sha256": handoff_sha256,
            "files_sha256": digest(value[1]["package_files"]),
            "cad_revision": value[1]["cad_revision"],
            "warnings": value[1].get("warnings", []),
            "files": {
                "input/" + path: checksum
                for path, checksum in value[1]["package_files"].items()
            },
        },
    )

    def ready():
        if backend is None and sys.platform != "win32":
            raise PipelineError(
                "Native SLDASM/SLDPRT capture requires Windows with licensed SolidWorks"
            )
        if backend is None:
            from ...runtime import native_readiness

            return native_readiness()
        reader = getattr(backend, "native_ready", None)
        if callable(reader):
            return reader()
        return {
            "role": "native",
            "scope": (
                "provided CAD backend; native qualification is "
                "recorded in the capture evidence"
            ),
        }

    checked(
        on_event,
        "capture",
        "input",
        "runtime.ready",
        ready,
    )
    _require(
        package_inventory(package) == original_files,
        "Prepared inputs changed before native capture",
    )
    capture_prepared(
        definition["source"],
        staging / "evidence",
        package=package,
        prepared_root=staging / "input/cad",
        native_files=definition["native_files"],
        backend=backend,
        worker_version=__version__,
        on_activity=on_activity,
    )
    checked(
        on_event,
        "capture",
        "output",
        "capture.integrity",
        lambda: verify_snapshot(staging / "evidence"),
        describe=lambda value: _snapshot_details(staging, value),
    )
    _rehome_evidence(staging, package)
    _require_plan_against_capture(staging, definition["plan"])

    def stable_inputs():
        if package_inventory(package) != original_files:
            raise PipelineError(
                "The supplied package changed during capture; save it and rerun"
            )
        return {
            "files": {
                "input/" + path: checksum
                for path, checksum in original_files.items()
            }
        }

    checked(
        on_event,
        "capture",
        "output",
        "capture.input_stability",
        stable_inputs,
    )
    _state(on_event, "capture", "completed")
    return definition, input_report


__all__ = [
    "CAPTURE_EVIDENCE_REQUESTS",
    "capture_native_package",
    "inspect_prepared_package",
]
