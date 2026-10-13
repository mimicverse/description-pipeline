"""Immutable native SolidWorks evidence and neutral test snapshots."""

from __future__ import annotations

from pathlib import Path

from ..io import PipelineError, confined, inventory, read_data, write_json

SCHEMA = "description.source/v1"


def write_manifest(root: Path, *, kind: str, identity: dict, evidence_class: str) -> dict:
    if evidence_class not in {"cad", "fixture", "imported"}:
        raise PipelineError(f"Unknown evidence class: {evidence_class}")
    if not kind or not identity:
        raise PipelineError("Snapshot requires source kind and identity")
    scene = "raw/scene_raw.json" if kind == "solidworks" else "scene.json"
    confined(root, scene)
    if kind == "solidworks":
        confined(root, "raw/source_config.json")
    manifest = {
        "schema_version": SCHEMA,
        "kind": kind,
        "identity": identity,
        "evidence_class": evidence_class,
        "scene": scene,
        "files": inventory(root, exclude=("manifest.json",)),
    }
    if kind == "solidworks":
        manifest["source_config"] = "raw/source_config.json"
    write_json(root / "manifest.json", manifest)
    return verify_snapshot(root)


def verify_snapshot(root: Path) -> dict:
    manifest = read_data(confined(root, "manifest.json"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA:
        raise PipelineError("Unsupported source manifest schema")
    if manifest.get("evidence_class") not in {"cad", "fixture", "imported"}:
        raise PipelineError("Snapshot evidence class is missing or invalid")
    if manifest.get("evidence_class") == "cad" and manifest.get("kind") != "solidworks":
        raise PipelineError("CAD evidence requires the native SolidWorks source kind")
    if not manifest.get("kind") or not isinstance(manifest.get("identity"), dict) or not manifest["identity"]:
        raise PipelineError("Snapshot source identity is missing")
    declared = manifest.get("files")
    if not isinstance(declared, dict) or not declared:
        raise PipelineError("Snapshot has no declared files")
    for name in declared:
        confined(root, name)
    actual = inventory(root, exclude=("manifest.json",))
    if actual != declared:
        changed = sorted(
            set(actual) ^ set(declared) | {key for key in actual.keys() & declared if actual[key] != declared[key]}
        )
        raise PipelineError(f"Source snapshot is incomplete or changed: {changed}")
    if manifest.get("scene") not in declared:
        raise PipelineError("Source scene is not bound by manifest")
    if manifest.get("kind") == "solidworks" and (
        manifest.get("scene") != "raw/scene_raw.json"
        or manifest.get("source_config") != "raw/source_config.json"
        or manifest["source_config"] not in declared
        or "scene.json" in declared
    ):
        raise PipelineError("SolidWorks snapshots must bind raw readings and source configuration only")
    return manifest


def load_scene(root: Path) -> dict:
    manifest = verify_snapshot(root)
    if manifest["kind"] == "solidworks":
        from .solidworks.captured import derive_scene

        return derive_scene(root, manifest)
    scene = read_data(confined(root, manifest["scene"]))
    if not isinstance(scene, dict):
        raise PipelineError("Source scene must be an object")
    return scene
