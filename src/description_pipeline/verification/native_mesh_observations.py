"""Validate native tessellation accounting without silently completing surfaces."""

import math
from pathlib import Path

from ..io import PipelineError, read_data


def _require(condition, message):
    if not condition:
        raise PipelineError(message)


def verify_native_mesh_observations(bundle: Path) -> dict:
    root = Path(bundle) / "evidence"
    manifest = read_data(root / "manifest.json")
    native = manifest.get("identity", {}).get("capture", {}).get("evidence_class") == "cad"
    rows = read_data(root / "raw/geometry.json")
    warnings = []
    observed = 0
    for entry in rows:
        tessellation = entry.get("tessellation")
        if tessellation is None and not native:
            continue
        component = entry["component"]
        _require(isinstance(tessellation, dict), component + ": native tessellation accounting is missing")
        _require(set(tessellation) == {"faces", "no_facets_faces"}, component + ": unknown tessellation accounting")
        faces = tessellation["faces"]
        omitted = tessellation["no_facets_faces"]
        _require(type(faces) is int and faces > 0, component + ": invalid native face count")
        _require(isinstance(omitted, list) and len(omitted) < faces, component + ": no tessellated native faces remain")
        bodies = entry.get("bodies")
        _require(isinstance(bodies, dict) and set(bodies) == {"solid", "sheet"}, component + ": body counts missing")
        _require(all(type(count) is int and count >= 0 for count in bodies.values()),
                 component + ": invalid body counts")
        _require(sum(bodies.values()) > 0, component + ": no native bodies recorded")
        seen = set()
        for face in omitted:
            _require(isinstance(face, dict) and set(face) == {
                "body_type", "body_index", "face_index", "triangle_count", "facet_count",
                "area_m2", "tessellate_result", "used_api",
            }, component + ": malformed untessellated-face observation")
            _require(type(face["body_type"]) is int and face["body_type"] == 1,
                     component + ": only native sheet faces may lack tessellation")
            _require(type(face["body_index"]) is int and 0 <= face["body_index"] < bodies["sheet"],
                     component + ": untessellated body is outside the native sheet inventory")
            _require(type(face["face_index"]) is int and face["face_index"] >= 0,
                     component + ": invalid native face index")
            key = (face["body_type"], face["body_index"], face["face_index"])
            _require(key not in seen, component + ": duplicate untessellated face")
            seen.add(key)
            _require(type(face["triangle_count"]) is int and face["triangle_count"] == 0
                     and type(face["facet_count"]) is int and face["facet_count"] == 0,
                     component + ": native triangles or facets cannot be omitted")
            _require(type(face["area_m2"]) in (int, float) and math.isfinite(face["area_m2"])
                     and face["area_m2"] >= 0, component + ": invalid untessellated native area")
            _require(type(face["tessellate_result"]) is bool,
                     component + ": native tessellation result is unreadable")
            _require(face["used_api"] == "IBody2.GetTessellation/ITessellation.Tessellate/GetFaceFacets",
                     component + ": unsupported untessellated-face evidence API")
            warnings.append({
                "code": "cad_sheet_face_untessellated", "component": component, **face,
                "qualification": "native_facets_absent; complete_surface_geometry_unqualified",
            })
        observed += 1
    return {"components": observed, "warnings": warnings,
            "untessellated_native_area_m2": math.fsum(row["area_m2"] for row in warnings)}
