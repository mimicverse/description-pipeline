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
    from .scene import build_scene

    raw = read_data(confined(root, manifest["scene"]))
    config = read_data(confined(root, manifest["source_config"]))
    if not isinstance(raw, dict) or not isinstance(config, dict):
        raise PipelineError("Captured readings and source configuration must be objects")
    if config.get("assembly") != manifest["identity"].get("assembly"):
        raise PipelineError("Captured source configuration names another assembly")
    if config.get("configuration") != manifest["identity"].get("configuration"):
        raise PipelineError("Captured source configuration names another configuration")
    masses = read_data(confined(root, "raw/mass_properties.json"))
    datums = read_data(confined(root, "raw/coordinate_systems.json"))
    if raw.get("mass_properties") != masses or raw.get("coordinate_systems") != datums:
        raise PipelineError("Captured scene readings differ from their dedicated raw records")
    components = raw.get("components")
    if not isinstance(components, list) or not all(isinstance(row, dict) for row in components):
        raise PipelineError("Captured component readings must be objects")
    mate_frames = {}
    selectors = [body.get("frame", {}).get("mate_axis") for body in config.get("bodies", [])]
    selectors = [selector for selector in selectors if isinstance(selector, dict)]
    if selectors:
        from .passive import resolve_mate_frame

        native = read_data(confined(root, "raw/native_discovery.json"))["raw"]
        observed = {row["name"]: row["transform"] for row in components}
        for selector in selectors:
            component = selector["component"]
            original = next(row["transform"] for row in native["components"] if row.get("name2") == component)
            if component not in observed or any(
                abs(a - b) > 1e-8 for a, b in zip(original, observed[component], strict=True)
            ):
                raise PipelineError("Passive mate placement differs between discovery and capture")
            mate_frames[component] = {"selector": selector, **resolve_mate_frame(native, selector)}
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
        native_mate_frames=mate_frames,
    )
    return build_scene(config, readings, geometry)
