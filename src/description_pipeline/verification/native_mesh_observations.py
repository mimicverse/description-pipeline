"""Independently verify native triangle facts and their derived STL bytes."""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path

from ..geometry.native_tessellation import (
    HEX64,
    native_stl_relative_path,
    native_triangle_stl_warning,
    native_triangle_relative_path,
    parse_native_tessellation,
    read_native_triangle_count,
    read_native_triangle_stream,
    summarize_native_topology,
)
from ..geometry.native_scope import native_binary64_unit_vector
from ..geometry.native_transform import transform_from_solidworks
from ..io import PipelineError, confined
from ..sources.snapshot import verify_snapshot
from . import capture_evidence as evidence

_GEOMETRY_KEYS = {
    "component",
    "raw_path",
    "raw_sha",
    "path",
    "sha",
    "transform_raw",
    "triangles",
    "bodies",
    "used_api",
    "tessellation",
}
_USED_API = "IComponent2.GetBodies3/IBody2.GetFaces/IFace2.GetTessTriangles(True)"
_STL_TRIANGLE_SIZE = 50


def _require(condition: object, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _integer(value: object) -> bool:
    return type(value) is int


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _evidence_file(
    bundle: Path,
    manifest: dict,
    relative: object,
    expected_sha256: object,
    label: str,
) -> Path:
    _require(
        isinstance(relative, str) and isinstance(expected_sha256, str) and bool(HEX64.fullmatch(expected_sha256)),
        f"{label}: file identity is missing",
    )
    _require(
        manifest.get("files", {}).get(relative) == expected_sha256,
        f"{label}: file is not bound by the evidence manifest",
    )
    path = confined(Path(bundle) / "evidence", relative)
    _require(path.is_file(), f"{label}: file is missing")
    _require(_digest(path) == expected_sha256, f"{label}: file digest differs from its record")
    return path


def _verify_scene_binding(
    component: str,
    occurrence: dict,
    transform_raw: object,
    source_scope: dict,
) -> None:
    _require(occurrence.get("document_type") == "part", f"{component}: geometry owner is not a part occurrence")
    try:
        offline_transform = transform_from_solidworks(transform_raw)
    except (TypeError, ValueError) as error:
        raise PipelineError(f"{component}: raw occurrence transform is invalid: {error}") from error
    transform = occurrence.get("transform")
    _require(
        isinstance(transform, list)
        and len(transform) == 16
        and all(type(value) in (int, float) and math.isfinite(float(value)) for value in transform),
        f"{component}: occurrence transform is missing or non-finite",
    )
    _require(
        tuple(float(value) for value in transform) == offline_transform,
        f"{component}: offline occurrence transform differs from GetTotalTransform(False)",
    )
    _require(
        source_scope["occurrence_scope"] == component
        and source_scope["component"] == component
        and isinstance(source_scope["referenced_configuration"], str)
        and bool(source_scope["referenced_configuration"]),
        f"{component}: source scope does not bind the occurrence and configuration",
    )


def _verify_stl(
    component: str,
    path: Path,
    raw_path: Path,
    raw_triangles: list,
    triangles: int,
    spans: list[dict],
) -> None:
    data = path.read_bytes()
    _require(
        len(data) == 84 + _STL_TRIANGLE_SIZE * triangles and struct.unpack_from("<I", data, 80)[0] == triangles,
        f"{component}: derived STL size or count is inconsistent",
    )
    owner_by_triangle = []
    for span in spans:
        owner_by_triangle.extend([span["face_owner_id"]] * span["triangle_count"])
    _require(len(owner_by_triangle) == triangles, f"{component}: STL owner stream is incomplete")
    _require(
        read_native_triangle_count(raw_path) == triangles,
        f"{component}: raw triangle count differs from the geometry record",
    )
    for triangle_index, triangle in enumerate(raw_triangles):
        first = tuple(triangle[1][axis] - triangle[0][axis] for axis in range(3))
        second = tuple(triangle[2][axis] - triangle[0][axis] for axis in range(3))
        cross = (
            first[1] * second[2] - first[2] * second[1],
            first[2] * second[0] - first[0] * second[2],
            first[0] * second[1] - first[1] * second[0],
        )
        _require(
            any(cross) and all(math.isfinite(value) for value in cross),
            f"{component}: degenerate native triangle",
        )
        expected = struct.Struct("<12fH").pack(
            *native_binary64_unit_vector(cross),
            *(value for point in triangle for value in point),
            owner_by_triangle[triangle_index],
        )
        offset = 84 + _STL_TRIANGLE_SIZE * triangle_index
        _require(
            data[offset : offset + _STL_TRIANGLE_SIZE] == expected,
            f"{component}: STL record {triangle_index} does not bind its raw triangle",
        )


def verify_native_mesh_observations(bundle: Path) -> dict:
    bundle = Path(bundle)
    manifest = verify_snapshot(bundle / "evidence")
    scene = evidence.scene(bundle)
    scene_components = scene.get("components")
    _require(isinstance(scene_components, list), "native scene component inventory is missing")
    occurrences = {
        row["name"]: row for row in scene_components if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    _require(len(occurrences) == len(scene_components), "native scene component identities are missing or duplicated")
    source_scopes = evidence.source_scopes(bundle)
    _require(isinstance(source_scopes, dict) and bool(source_scopes), "native source-scope inventory is missing")
    rows = evidence.geometry(bundle)
    _require(isinstance(rows, list) and bool(rows), "native geometry inventory is missing")
    row_components = [row.get("component") for row in rows if isinstance(row, dict)]
    _require(
        len(row_components) == len(rows)
        and len(set(row_components)) == len(row_components)
        and set(row_components) == set(source_scopes),
        "native geometry does not cover the exact source-scope occurrence set",
    )
    warnings: list[dict] = []
    topologies = []
    for index, entry in enumerate(rows):
        _require(
            isinstance(entry, dict) and set(entry) == _GEOMETRY_KEYS,
            f"native geometry row {index} has an unknown shape",
        )
        component = entry["component"]
        _require(isinstance(component, str) and bool(component), f"native geometry row {index} has no component")
        _require(entry["used_api"] == _USED_API, f"{component}: unsupported primary triangle API")
        _require(
            entry["raw_path"] == native_triangle_relative_path(entry["raw_sha"]),
            f"{component}: raw triangle path is not the canonical payload digest",
        )
        _require(
            entry["path"] == native_stl_relative_path(entry["sha"]),
            f"{component}: STL path is not the canonical content digest",
        )
        source_scope = source_scopes.get(component)
        _require(isinstance(source_scope, dict), f"{component}: native source scope is missing")
        occurrence = occurrences.get(component)
        _require(isinstance(occurrence, dict), f"{component}: native occurrence binding is missing")
        _verify_scene_binding(
            component,
            occurrence,
            entry["transform_raw"],
            source_scope,
        )
        raw_path = _evidence_file(
            bundle,
            manifest,
            entry["raw_path"],
            entry["raw_sha"],
            f"{component}: raw triangle stream",
        )
        _require(
            read_native_triangle_count(raw_path) == entry["triangles"],
            f"{component}: raw triangle stream count differs from its record",
        )
        raw_triangles = read_native_triangle_stream(raw_path, expected_count=entry["triangles"])
        component_warnings: list[dict] = []
        spans = parse_native_tessellation(
            component,
            entry["tessellation"],
            entry["bodies"],
            entry["triangles"],
            source_scope=source_scope,
            warnings=component_warnings,
        )
        warnings.extend(component_warnings)
        stl_path = _evidence_file(
            bundle,
            manifest,
            entry["path"],
            entry["sha"],
            f"{component}: derived STL",
        )
        _verify_stl(component, stl_path, raw_path, raw_triangles, entry["triangles"], spans)
        topology = summarize_native_topology(spans, raw_triangles)
        topologies.append({"component": component, **topology})
        warnings.append(
            native_triangle_stl_warning(
                component,
                raw_path=entry["raw_path"],
                raw_sha=entry["raw_sha"],
                path=entry["path"],
                sha=entry["sha"],
                triangles=entry["triangles"],
            )
        )
    return {
        "components": len(rows),
        "warnings": warnings,
        "topologies": topologies,
    }
