"""Derive the Generate input from an already verified immutable CAD snapshot.

This module never loads a native CAD library or modifies the capture. Windows
seals native observations and their source configuration; Linux constructs the
link-frame scene when generation consumes those observations.
"""

from pathlib import Path
from types import SimpleNamespace

from ...io import PipelineError, confined, digest, inventory, read_data


def derive_engineering_input(root: Path, manifest: dict) -> dict:
    """Construct Generate input using only a verified SolidWorks snapshot."""
    from .evidence import (
        RUNTIME_EVIDENCE,
        metadata_value_warnings,
    )
    from .scene import build_scene

    raw = read_data(confined(root, manifest["scene"]))
    from .discovery import source_definition

    delivery = Path(root)
    delivery = delivery.parent if delivery.name == "evidence" else delivery
    payload = read_data(confined(delivery, "evidence/discovery.json"))
    main = str((payload.get("derivation") or {}).get("main_assembly") or "")
    config = source_definition(
        delivery,
        discovery_name="evidence/discovery.json",
        cad_prefix="input/cad/",
        records_prefix="input/",
    )
    if not isinstance(raw, dict) or not isinstance(config, dict):
        raise PipelineError("Captured readings and source configuration must be objects")
    if not main or config.get("assembly") != str(confined(delivery.resolve(), "input/cad/" + main)):
        raise PipelineError("Captured source configuration names another assembly")
    admitted = str(manifest["identity"].get("assembly") or "").replace("\\", "/")
    if not admitted.endswith("/" + main):
        raise PipelineError("Captured source configuration names another assembly")
    if config.get("configuration") != manifest["identity"].get("configuration"):
        raise PipelineError("Captured source configuration names another configuration")
    masses = read_data(confined(root, "raw/mass_properties.json"))
    datums = read_data(confined(root, "raw/coordinate_systems.json"))
    if raw.get("mass_properties") != masses or raw.get("coordinate_systems") != datums:
        raise PipelineError("Captured scene readings differ from their dedicated raw records")
    if "raw/link_group_mass_properties.json" not in manifest["files"]:
        raise PipelineError(
            "The capture carries no direct link group mass properties record; "
            "this workflow requires a capture made by the direct-link-readout pipeline"
        )
    link_groups = read_data(confined(root, "raw/link_group_mass_properties.json"))
    geometry = (
        read_data(confined(root, "raw/geometry.json"))
        if "raw/geometry.json" in manifest["files"]
        else []
    )
    if "raw/native_capture_facts.json" in manifest["files"]:
        from .appearance import bind_capture_appearance

        raw, geometry = bind_capture_appearance(
            raw,
            geometry,
            read_data(confined(root, "raw/native_capture_facts.json")),
        )
    components = raw.get("components")
    if not isinstance(components, list) or not all(isinstance(row, dict) for row in components):
        raise PipelineError("Captured component readings must be objects")
    if manifest.get("evidence_class") != "fixture":
        from .appearance import (
            UNRECORDED_APPEARANCE,
            require_deliverable_appearance,
        )

        components_by_name = {str(row.get("name") or ""): row for row in components}
        for entry in geometry:
            component = str(entry.get("component") or "")
            source_scope = entry.get("source_scope") or {}
            raw_component = components_by_name.get(component)
            require_deliverable_appearance(
                (
                    raw_component.get("appearance_raw", UNRECORDED_APPEARANCE)
                    if raw_component is not None
                    else UNRECORDED_APPEARANCE
                ),
                component=component,
                expected_document_path=source_scope.get("source_document_path"),
                expected_configuration=source_scope.get(
                    "referenced_configuration"
                ),
                expected_bodies=entry.get("body_inventory"),
            )
    readings = SimpleNamespace(
        components=[SimpleNamespace(**row) for row in components],
        coordinate_systems=datums,
        mass_properties=masses,
        notes=raw.get("notes", {}),
        document=raw.get("document"),
        link_group_mass_properties=link_groups,
    )
    scene = build_scene(config, readings, geometry)

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise PipelineError("Captured evidence manifest has no file inventory")
    assembly_member = "input/cad/" + main
    input_files = {f"input/{path}": checksum for path, checksum in inventory(delivery / "input").items()}
    if assembly_member not in input_files:
        raise PipelineError("Captured source assembly is not in the archived input closure")
    evidence_files = {f"evidence/{path}": checksum for path, checksum in files.items()}
    scene["semantic_identity"] = {
        "capture_set_sha256": digest({**input_files, **evidence_files}),
        "source_member": assembly_member,
        "source_sha256": input_files[assembly_member],
        "source_configuration": str(manifest["identity"].get("configuration") or ""),
    }

    def evidence_source(delivery_path: str, snapshot_path: str) -> tuple[dict, str]:
        digest = files.get(snapshot_path)
        if not isinstance(digest, str):
            raise PipelineError(f"Captured evidence is not bound by the manifest: {delivery_path}")
        payload = read_data(confined(root, snapshot_path))
        if not isinstance(payload, dict):
            raise PipelineError(f"Captured evidence must be an object: {delivery_path}")
        return payload, digest

    runtime, runtime_sha256 = evidence_source(RUNTIME_EVIDENCE, "native-runtime.json")
    native_tool_path = confined(delivery, "reports/native-tool.json")
    native_tool = read_data(native_tool_path)
    metadata_warnings = metadata_value_warnings(
        runtime,
        expected_tool=native_tool,
        runtime_sha256=runtime_sha256,
        expected_configuration=str(config.get("configuration") or ""),
    )
    provenance = scene.setdefault("provenance", {})
    # Native producer paths remain in raw evidence. The model retains only a
    # location-independent member reference so moving an identical artifact
    # cannot change its semantic identity.
    provenance["assembly"] = assembly_member
    existing = provenance.get("value_warnings")
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        raise PipelineError("Constructed scene value warnings must be a list")
    if any(item.get("code") == metadata_warnings[0]["code"] for item in existing if isinstance(item, dict)):
        raise PipelineError("Metadata provenance warning was emitted twice")
    provenance["value_warnings"] = [*existing, *metadata_warnings]
    return scene
