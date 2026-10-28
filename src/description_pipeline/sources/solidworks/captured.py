"""Derive the Generate input from an already verified immutable CAD snapshot.

This module never loads a native CAD library or modifies the capture. Windows
seals native observations and their source configuration; Linux constructs the
link-frame scene when generation consumes those observations.
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace

from ...io import PipelineError, confined, digest, inventory, read_data


def _raw_scene_record(scene) -> dict:
    return {
        "document": scene.document,
        "components": [
            {
                "name": component.name,
                "path": component.path,
                "transform": list(component.transform),
                "is_fixed": component.is_fixed,
                "document_type": component.document_type,
            }
            for component in scene.components
        ],
        "coordinate_systems": {
            name: list(matrix)
            for name, matrix in scene.coordinate_systems.items()
        },
        "mass_properties": scene.mass_properties,
        "notes": scene.notes,
    }


def derive_engineering_input(
    root: Path,
    manifest: dict,
    *,
    mesh_root: Path,
) -> dict:
    """Construct Generate input using only a verified SolidWorks snapshot."""
    from .evidence import (
        RUNTIME_EVIDENCE,
        metadata_value_warnings,
    )
    from .capture_geometry import materialize_native_geometry
    from .capture_projection import (
        load_native_capture_observation,
        project_native_capture_observation,
    )
    from .saved_state import SAVED_STATE_WARNING_CODE, warning_from_capture
    from .scene import build_scene

    from .discovery import source_definition

    observation = load_native_capture_observation(root)
    delivery = Path(root)
    delivery = delivery.parent if delivery.name == "evidence" else delivery
    payload = read_data(
        confined(delivery, "input/discovery/native-discovery.json")
    )
    if not isinstance(payload, dict):
        raise PipelineError("Captured discovery record must be an object")
    main = str((payload.get("derivation") or {}).get("main_assembly") or "")
    config = source_definition(
        delivery,
        discovery_name="input/discovery/native-discovery.json",
        cad_prefix="input/cad/",
        records_prefix="input/",
    )
    if not isinstance(config, dict):
        raise PipelineError("Captured source configuration must be an object")
    projection_source = {
        **config,
        "groups": [
            {
                "name": str(body.get("name") or body.get("id") or ""),
                "components": list(body.get("components") or []),
            }
            for body in config.get("bodies") or []
            if isinstance(body, dict)
        ],
    }
    projected = project_native_capture_observation(
        observation,
        projection_source,
    )
    raw = _raw_scene_record(projected["scene"])
    if not main or config.get("assembly") != str(confined(delivery.resolve(), "input/cad/" + main)):
        raise PipelineError("Captured source configuration names another assembly")
    admitted = str(manifest["identity"].get("assembly") or "").replace("\\", "/")
    if not admitted.endswith("/" + main):
        raise PipelineError("Captured source configuration names another assembly")
    if config.get("configuration") != manifest["identity"].get("configuration"):
        raise PipelineError("Captured source configuration names another configuration")
    masses = projected["scene"].mass_properties
    datums = raw["coordinate_systems"]
    link_groups = projected["link_group_mass_properties"]
    geometry = list(
        materialize_native_geometry(
            observation["geometry_raw"],
            observation["source_scopes"],
            evidence_root=root,
            destination_root=mesh_root,
        ).values()
    )
    from .appearance import bind_capture_appearance

    raw, geometry = bind_capture_appearance(
        raw,
        geometry,
        observation,
    )
    components = raw.get("components")
    if not isinstance(components, list) or not all(isinstance(row, dict) for row in components):
        raise PipelineError("Captured component readings must be objects")
    if manifest.get("evidence_class") != "fixture":
        from .appearance import (
            UNRECORDED_APPEARANCE,
            appearance_groups,
            resolve_deliverable_appearance,
        )
        from ...geometry.native_tessellation import native_stl_subset

        components_by_name = {str(row.get("name") or ""): row for row in components}
        for entry in geometry:
            component = str(entry.get("component") or "")
            source_scope = entry.get("source_scope") or {}
            raw_component = components_by_name.get(component)
            tessellation = entry.get("tessellation")
            face_spans = (
                tessellation.get("face_spans")
                if isinstance(tessellation, dict)
                else None
            )
            if not isinstance(face_spans, list) or not face_spans:
                raise PipelineError(
                    f"Native appearance has no delivered face spans: {component}"
                )
            resolution = resolve_deliverable_appearance(
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
                face_spans=face_spans,
            )
            source_path = confined(
                mesh_root,
                str(entry.get("path") or ""),
            )
            source_bytes = source_path.read_bytes()
            if hashlib.sha256(source_bytes).hexdigest() != entry.get("sha"):
                raise PipelineError(
                    f"Native appearance source STL differs from its geometry record: {component}"
                )
            groups = []
            for group in appearance_groups(resolution.faces):
                subset, triangles = native_stl_subset(
                    source_bytes,
                    group["triangle_ranges"],
                )
                expected_triangles = sum(
                    row["triangle_count"]
                    for row in group["triangle_ranges"]
                )
                if triangles != expected_triangles:
                    raise PipelineError(
                        f"Native appearance subset count is inconsistent: {component}"
                    )
                groups.append(
                    {
                        **group,
                        "mesh_sha256": hashlib.sha256(subset).hexdigest(),
                    }
                )
            entry["appearance_groups"] = groups
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
    prepared_capture = runtime.get("prepared_capture")
    captured_open = (
        prepared_capture.get("opened")
        if isinstance(prepared_capture, dict)
        else None
    )
    saved_state_warning = warning_from_capture(
        (payload.get("raw") or {}).get("source_selection"),
        captured_open,
    )
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
    if saved_state_warning is not None and any(
        item.get("code") == SAVED_STATE_WARNING_CODE
        for item in existing
        if isinstance(item, dict)
    ):
        raise PipelineError("Saved-state source warning was emitted twice")
    provenance["value_warnings"] = [
        *existing,
        *metadata_warnings,
        *([saved_state_warning] if saved_state_warning is not None else []),
    ]
    return scene
