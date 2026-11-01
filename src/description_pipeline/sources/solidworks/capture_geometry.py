"""Portable raw-triangle sealing and Linux-side STL materialization."""

from __future__ import annotations

import copy
import hashlib
import math
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ...geometry.native_scope import native_scope_identity_sha256
from ...geometry.native_tessellation import (
    VerifiedNativeTriangleStream,
    build_native_tessellation,
    inspect_native_triangle_stream,
    materialize_native_stl,
    native_zero_triangle_evidence_is_valid,
    native_stl_relative_path,
    native_triangle_relative_path,
    parse_native_tessellation,
    write_native_triangle_stream,
)
from ...io import PipelineError, _physical_path, file_digest
from .errors import CadError


USED_API = (
    "IComponent2.GetBodies3/IBody2.GetFaces/"
    "IFace2.GetTessTriangles(True)"
)

RAW_GEOMETRY_KEYS = frozenset(
    {
        "stream_path",
        "stream_sha256",
        "triangle_count",
        "body_inventory",
        "face_inventory",
        "body_counts",
        "used_api",
        "source_scope_sha256",
        "appearance_raw",
    }
)


def _require(condition: object, message: str, detail=None) -> None:
    if not condition:
        raise CadError("cad_mesh_export_failed", message, detail)


def _publish_if_absent(temporary: Path, target: Path, expected_sha256: str) -> None:
    temporary = Path(_physical_path(temporary))
    target = Path(_physical_path(target))
    if target.is_file():
        _require(
            file_digest(target) == expected_sha256,
            "two different geometry payloads resolved to one SHA-256 path",
            {"temporary": str(temporary), "target": str(target)},
        )
        temporary.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, target)


def _geometry_facts(component: str, facts: object) -> dict:
    _require(
        isinstance(facts, Mapping),
        "prepared geometry facts are not a mapping",
        {"component": component, "returned": type(facts).__name__},
    )
    _require(
        facts.get("component") == component,
        "prepared geometry facts name another component",
        {"component": component, "reported": facts.get("component")},
    )
    _require(
        facts.get("state") == "observed"
        and not facts.get("errors"),
        "prepared geometry is incomplete",
        {
            "component": component,
            "state": facts.get("state"),
            "errors": copy.deepcopy(facts.get("errors") or []),
        },
    )
    _require(
        "appearance_raw" in facts,
        "prepared appearance facts are missing",
        {"component": component},
    )
    triangle_values = facts.get("triangle_values")
    _require(
        isinstance(triangle_values, list)
        and bool(triangle_values)
        and len(triangle_values) % 9 == 0
        and all(
            type(value) in (int, float)
            and math.isfinite(float(value))
            for value in triangle_values
        ),
        "prepared geometry triangle values are malformed",
        {"component": component},
    )
    body_counts = facts.get("bodies")
    _require(
        isinstance(body_counts, dict)
        and set(body_counts) == {"solid", "sheet"}
        and all(type(value) is int and value >= 0 for value in body_counts.values())
        and sum(body_counts.values()) > 0,
        "prepared geometry body counts are malformed",
        {"component": component},
    )
    _require(
        facts.get("used_api") == USED_API,
        "prepared geometry uses an unsupported primary triangle API",
        {"component": component, "used_api": facts.get("used_api")},
    )
    body_inventory = facts.get("body_inventory")
    face_inventory = facts.get("face_inventory")
    _require(
        isinstance(body_inventory, list) and bool(body_inventory),
        "prepared body inventory is missing",
        {"component": component},
    )
    _require(
        isinstance(face_inventory, list) and bool(face_inventory),
        "prepared face inventory is missing",
        {"component": component},
    )
    return {
        "triangle_values": [float(value) for value in triangle_values],
        "body_inventory": copy.deepcopy(body_inventory),
        "face_inventory": copy.deepcopy(face_inventory),
        "body_counts": dict(body_counts),
        "used_api": USED_API,
    }


def _validate_raw_inventory(
    component: str,
    body_inventory: list,
    face_inventory: list,
    body_counts: dict,
    triangle_count: int,
) -> None:
    """Validate raw body/face structure without deriving source-scope identity."""

    body_keys = {
        "body_type",
        "body_index",
        "body_name",
        "face_count",
    }
    body_index = {}
    counts = {0: 0, 1: 0}
    faces_by_body = {}
    for row in body_inventory:
        _require(
            isinstance(row, dict) and set(row) == body_keys,
            "raw body inventory has an unknown shape",
            {"component": component},
        )
        body_type = row["body_type"]
        index = row["body_index"]
        _require(
            type(body_type) is int
            and body_type in {0, 1}
            and type(index) is int
            and index >= 0
            and (
                row["body_name"] is None
                or isinstance(row["body_name"], str)
            )
            and type(row["face_count"]) is int
            and row["face_count"] > 0,
            "raw body inventory row is malformed",
            {"component": component},
        )
        key = (body_type, index)
        _require(
            key not in body_index,
            "raw body inventory repeats an identity",
            {"component": component, "body_type": body_type, "body_index": index},
        )
        body_index[key] = row
        counts[body_type] += 1
        faces_by_body[key] = 0
    _require(
        counts == {0: body_counts["solid"], 1: body_counts["sheet"]},
        "raw body counts differ from the body inventory",
        {"component": component},
    )
    face_keys = {
        "body_type",
        "body_index",
        "face_index",
        "triangle_start",
        "triangle_count",
    }
    next_triangle = 0
    seen_faces = set()
    for row in face_inventory:
        _require(
            isinstance(row, dict) and set(row) == face_keys
            | ({"zero_triangle_evidence"} if row.get("triangle_count") == 0 else set()),
            "raw face inventory has an unknown shape",
            {"component": component},
        )
        body_type = row["body_type"]
        body_id = row["body_index"]
        face_index = row["face_index"]
        start = row["triangle_start"]
        count = row["triangle_count"]
        key = (body_type, body_id)
        _require(
            key in body_index
            and type(face_index) is int
            and face_index >= 0
            and type(start) is int
            and type(count) is int
            and start == next_triangle
            and count >= 0
            and (count > 0 or native_zero_triangle_evidence_is_valid(
                body_type, row.get("zero_triangle_evidence"),
            )),
            "raw face inventory is not contiguous",
            {"component": component},
        )
        face_key = (*key, face_index)
        _require(
            face_key not in seen_faces,
            "raw face inventory repeats an identity",
            {"component": component},
        )
        seen_faces.add(face_key)
        faces_by_body[key] += 1
        next_triangle += count
    _require(
        next_triangle == triangle_count,
        "raw face inventory does not cover the triangle stream",
        {"component": component},
    )
    _require(
        all(
            row["face_count"] == faces_by_body[key]
            for key, row in body_index.items()
        ),
        "raw body face counts differ from the face inventory",
        {"component": component},
    )


def _compact_appearance(
    component: str,
    source_scope: Mapping[str, object],
    appearance: object,
) -> dict | None:
    if appearance is None:
        return None
    _require(
        isinstance(appearance, Mapping),
        "prepared appearance facts are missing",
        {"component": component},
    )
    _require(
        appearance.get("component") == component,
        "prepared appearance facts name another component",
        {"component": component, "reported": appearance.get("component")},
    )
    _require(
        appearance.get("source_document_path")
        == source_scope.get("source_document_path"),
        "prepared appearance source differs from the source scope",
        {"component": component},
    )
    _require(
        appearance.get("referenced_configuration")
        == source_scope.get("referenced_configuration"),
        "prepared appearance configuration differs from the source scope",
        {"component": component},
    )
    compact = copy.deepcopy(dict(appearance))
    compact.pop("component", None)
    compact.pop("source_document_path", None)
    compact.pop("referenced_configuration", None)
    return compact


def seal_native_geometry(
    raw_geometry: Mapping[str, Mapping],
    source_scopes: Mapping[str, Mapping],
    evidence_root: Path,
) -> dict[str, dict]:
    """Write exact triangle streams and return only raw geometry metadata.

    ``triangle_values`` are serialized with the existing binary64 native stream
    format.  The manifest binds those serialized bytes; this function does not
    claim byte identity with the COM SAFEARRAY representation.
    """

    _require(
        isinstance(raw_geometry, Mapping)
        and isinstance(source_scopes, Mapping),
        "geometry serialization requires component mappings",
    )
    _require(
        set(raw_geometry) == set(source_scopes),
        "geometry and source-scope component sets differ",
        {
            "geometry": sorted(raw_geometry),
            "source_scopes": sorted(source_scopes),
        },
    )
    root = Path(_physical_path(evidence_root))
    raw_directory = root / "raw" / "triangles"
    records = {}
    for component in sorted(raw_geometry):
        facts = _geometry_facts(component, raw_geometry[component])
        source_scope = source_scopes[component]
        _require(
            isinstance(source_scope, Mapping),
            "prepared geometry source scope is malformed",
            {"component": component},
        )
        triangle_count = len(facts["triangle_values"]) // 9
        _validate_raw_inventory(
            component,
            facts["body_inventory"],
            facts["face_inventory"],
            facts["body_counts"],
            triangle_count,
        )
        component_key = hashlib.sha256(component.encode("utf-8")).hexdigest()
        temporary = raw_directory / f".{component_key}.partial.triangles"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        written_count = write_native_triangle_stream(
            temporary,
            facts["triangle_values"],
        )
        _require(
            written_count == triangle_count,
            "native triangle stream count differs while sealing",
            {"component": component},
        )
        stream_sha256 = file_digest(temporary)
        stream_path = native_triangle_relative_path(stream_sha256)
        target = root / stream_path
        _publish_if_absent(temporary, target, stream_sha256)
        records[component] = {
            "stream_path": stream_path,
            "stream_sha256": stream_sha256,
            "triangle_count": triangle_count,
            "body_inventory": facts["body_inventory"],
            "face_inventory": facts["face_inventory"],
            "body_counts": facts["body_counts"],
            "used_api": facts["used_api"],
            "source_scope_sha256": native_scope_identity_sha256(source_scope),
            "appearance_raw": _compact_appearance(
                component,
                source_scope,
                raw_geometry[component].get("appearance_raw"),
            ),
        }
    return records


def materialize_native_geometry(
    geometry_raw: Mapping[str, Mapping],
    source_scopes: Mapping[str, Mapping],
    *,
    evidence_root: Path,
    destination_root: Path,
    verified_streams: Mapping[
        str,
        VerifiedNativeTriangleStream,
    ]
    | None = None,
    warnings: list[dict] | None = None,
) -> dict[str, dict]:
    """Materialize STL bytes after Capture admission from the sealed raw streams."""

    _require(
        isinstance(geometry_raw, Mapping)
        and isinstance(source_scopes, Mapping)
        and set(geometry_raw) == set(source_scopes),
        "raw geometry and source scopes must cover the same components",
    )
    evidence_root = Path(evidence_root)
    destination_root = Path(destination_root)
    records = {}
    for component in sorted(geometry_raw):
        raw = geometry_raw[component]
        _require(
            isinstance(raw, Mapping) and set(raw) == RAW_GEOMETRY_KEYS,
            "raw geometry metadata has an unknown shape",
            {"component": component},
        )
        source_scope = source_scopes[component]
        _require(
            isinstance(source_scope, Mapping)
            and raw["source_scope_sha256"]
            == native_scope_identity_sha256(source_scope),
            "raw geometry source-scope binding differs",
            {"component": component},
        )
        raw_path = evidence_root / str(raw["stream_path"])
        _require(
            raw_path.is_file()
            and raw["stream_path"]
            == native_triangle_relative_path(str(raw["stream_sha256"])),
            "raw triangle stream is missing or not canonical",
            {"component": component},
        )
        verified = (
            verified_streams.get(component)
            if isinstance(verified_streams, Mapping)
            else None
        )
        if verified is None:
            try:
                verified = inspect_native_triangle_stream(
                    raw_path,
                    expected_sha256=str(raw["stream_sha256"]),
                    expected_count=raw["triangle_count"],
                )
            except PipelineError as error:
                raise CadError(
                    "cad_mesh_export_failed",
                    str(error),
                    {"component": component},
                ) from error
        _require(
            verified.path.resolve() == raw_path.resolve()
            and verified.sha256 == raw["stream_sha256"]
            and verified.triangle_count == raw["triangle_count"],
            "verified raw triangle stream differs from its record",
            {"component": component},
        )
        triangle_count = verified.triangle_count
        component_warnings: list[dict] = []
        tessellation = build_native_tessellation(
            component,
            raw["body_inventory"],
            raw["face_inventory"],
            source_scope=source_scope,
        )
        spans = parse_native_tessellation(
            component,
            tessellation,
            raw["body_counts"],
            triangle_count,
            source_scope=source_scope,
            warnings=component_warnings,
        )
        if warnings is not None:
            warnings.extend(component_warnings)
        stl_directory = destination_root / "geometry"
        stl_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".stl-", dir=stl_directory) as temporary_root:
            temporary = Path(temporary_root) / "mesh.stl"
            materialize_native_stl(
                raw_path,
                temporary,
                spans,
                triangle_count=triangle_count,
            )
            stl_sha256 = file_digest(temporary)
            stl_path = native_stl_relative_path(stl_sha256)
            _publish_if_absent(
                temporary,
                destination_root / stl_path,
                stl_sha256,
            )
        records[component] = {
            "component": component,
            "raw_path": raw["stream_path"],
            "raw_sha": raw["stream_sha256"],
            "path": stl_path,
            "sha": stl_sha256,
            "triangles": triangle_count,
            "bodies": copy.deepcopy(raw["body_counts"]),
            "used_api": raw["used_api"],
            "tessellation": tessellation,
        }
    return records


__all__ = [
    "RAW_GEOMETRY_KEYS",
    "USED_API",
    "materialize_native_geometry",
    "seal_native_geometry",
]
