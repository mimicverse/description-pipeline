"""Derive the portable scene from an already verified immutable CAD snapshot.

This module never loads a native CAD library or modifies the capture. Windows
seals native observations and their source configuration; Linux constructs the
link-frame scene when generation consumes those observations.
"""

from pathlib import Path
from types import SimpleNamespace

from ...io import PipelineError, confined, read_data


def derive_scene(root: Path, manifest: dict) -> dict:
    """Construct a scene using only members of a verified SolidWorks snapshot."""
    from .evidence import (
        REWRITE_ENVIRONMENT_EVIDENCE,
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
        delivery, discovery_name="evidence/discovery.json", cad_prefix="input/cad/", records_prefix="input/records/"
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
    components = raw.get("components")
    if not isinstance(components, list) or not all(isinstance(row, dict) for row in components):
        raise PipelineError("Captured component readings must be objects")
    geometry = (
        read_data(confined(root, "raw/geometry.json"))
        if "raw/geometry.json" in manifest["files"]
        else []
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

    def evidence_source(delivery_path: str, snapshot_path: str) -> tuple[dict, str]:
        digest = files.get(snapshot_path)
        if not isinstance(digest, str):
            raise PipelineError(f"Captured evidence is not bound by the manifest: {delivery_path}")
        payload = read_data(confined(root, snapshot_path))
        if not isinstance(payload, dict):
            raise PipelineError(f"Captured evidence must be an object: {delivery_path}")
        return payload, digest

    runtime, runtime_sha256 = evidence_source(RUNTIME_EVIDENCE, "native-runtime.json")
    rewrite, rewrite_sha256 = evidence_source(
        REWRITE_ENVIRONMENT_EVIDENCE,
        "evidence/rewrite-environment.json",
    )
    closure = read_data(confined(root, "raw/dependency_closure.json"))
    if not isinstance(closure, dict):
        raise PipelineError("Captured dependency closure must be an object")
    metadata_warnings = metadata_value_warnings(
        runtime,
        rewrite,
        runtime_sha256=runtime_sha256,
        rewrite_sha256=rewrite_sha256,
        expected_configuration=str(config.get("configuration") or ""),
        closure_top_level=str(closure.get("top_level") or ""),
    )
    provenance = scene.setdefault("provenance", {})
    existing = provenance.get("value_warnings")
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        raise PipelineError("Constructed scene value warnings must be a list")
    if any(item.get("code") == metadata_warnings[0]["code"] for item in existing if isinstance(item, dict)):
        raise PipelineError("Metadata provenance warning was emitted twice")
    provenance["value_warnings"] = [*existing, *metadata_warnings]
    return scene
