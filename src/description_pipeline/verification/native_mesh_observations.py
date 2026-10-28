"""Independently verify native triangle facts and their derived STL bytes."""

from __future__ import annotations

import hashlib
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
from ..io import PipelineError, confined
from ..sources.snapshot import verify_snapshot
from . import capture_evidence as evidence

_USED_API = "IComponent2.GetBodies3/IBody2.GetFaces/IFace2.GetTessTriangles(True)"


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
