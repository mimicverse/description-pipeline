"""Independently verify native triangle facts and their derived STL bytes."""

from __future__ import annotations

import hashlib
import math
import struct
import tempfile
from pathlib import Path

from ..geometry.native_tessellation import (
    HEX64,
    build_native_tessellation,
    materialize_native_stl,
    native_stl_relative_path,
    native_stl_subset,
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


def verify_native_mesh_observations(
    bundle: Path,
    model: dict,
) -> dict:
    bundle = Path(bundle)
    manifest = verify_snapshot(bundle / "evidence")
    raw_facts = evidence.native_capture_facts(bundle)
    geometry_raw = raw_facts.get("geometry_raw")
    source_scopes = evidence.source_scopes(bundle)
    _require(
        isinstance(geometry_raw, dict)
        and bool(geometry_raw)
        and isinstance(source_scopes, dict)
        and set(geometry_raw) == set(source_scopes),
        "native raw geometry does not cover the exact source scopes",
    )
    shapes_by_source: dict[str, list[dict]] = {}
    for link in model.get("links") or []:
        _require(isinstance(link, dict), "delivered model link is malformed")
        for role in ("visuals", "collisions"):
            for shape in link.get(role) or []:
                _require(
                    isinstance(shape, dict)
                    and isinstance(shape.get("filename"), str),
                    "delivered model mesh shape is malformed",
                )
                shapes_by_source.setdefault(
                    shape["filename"],
                    [],
                ).append(shape)
    warnings: list[dict] = []
    topologies = []
    with tempfile.TemporaryDirectory(
        prefix="verify-native-geometry-"
    ) as directory:
        scratch = Path(directory)
        for index, (component, entry) in enumerate(
            sorted(geometry_raw.items())
        ):
            _require(
                isinstance(entry, dict),
                f"native geometry row {index} is malformed",
            )
            _require(
                isinstance(component, str) and bool(component),
                f"native geometry row {index} has no component",
            )
            _require(
                entry.get("used_api") == _USED_API,
                f"{component}: unsupported primary triangle API",
            )
            raw_sha = str(entry.get("stream_sha256"))
            _require(
                entry.get("stream_path")
                == native_triangle_relative_path(raw_sha),
                f"{component}: raw triangle path is not canonical",
            )
            source_scope = source_scopes.get(component)
            _require(
                isinstance(source_scope, dict),
                f"{component}: native source scope is missing",
            )
            raw_path = _evidence_file(
                bundle,
                manifest,
                entry["stream_path"],
                entry["stream_sha256"],
                f"{component}: raw triangle stream",
            )
            _require(
                read_native_triangle_count(raw_path)
                == entry["triangle_count"],
                f"{component}: raw triangle stream count differs",
            )
            raw_triangles = read_native_triangle_stream(
                raw_path,
                expected_count=entry["triangle_count"],
            )
            tessellation = build_native_tessellation(
                component,
                entry["body_inventory"],
                entry["face_inventory"],
                source_scope=source_scope,
            )
            component_warnings: list[dict] = []
            spans = parse_native_tessellation(
                component,
                tessellation,
                entry["body_counts"],
                entry["triangle_count"],
                source_scope=source_scope,
                warnings=component_warnings,
            )
            warnings.extend(component_warnings)
            source_stl = scratch / f"{index}.stl"
            materialize_native_stl(raw_path, source_stl, spans)
            source_bytes = source_stl.read_bytes()
            source_sha = hashlib.sha256(source_bytes).hexdigest()
            source_path = native_stl_relative_path(source_sha)
            for shape in shapes_by_source.get(source_path, []):
                ranges = shape.get("triangle_ranges")
                if ranges is None:
                    _require(
                        "mesh_sha256" not in shape,
                        f"{component}: whole mesh carries a subset hash",
                    )
                else:
                    subset, count = native_stl_subset(
                        source_bytes,
                        ranges,
                    )
                    _require(
                        count > 0
                        and shape.get("mesh_sha256")
                        == hashlib.sha256(subset).hexdigest(),
                        f"{component}: appearance mesh differs from raw triangles",
                    )
                _require(
                    shape.get("sha256") == source_sha,
                    f"{component}: model source mesh differs from raw triangles",
                )
            topology = summarize_native_topology(spans, raw_triangles)
            topologies.append({"component": component, **topology})
            warnings.append(
                native_triangle_stl_warning(
                    component,
                    raw_path=entry["stream_path"],
                    raw_sha=raw_sha,
                    path=source_path,
                    sha=source_sha,
                    triangles=entry["triangle_count"],
                )
            )
    return {
        "components": len(geometry_raw),
        "warnings": warnings,
        "topologies": topologies,
    }
