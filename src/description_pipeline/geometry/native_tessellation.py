"""Read, validate, and materialize native SolidWorks display triangles."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..io import PipelineError, _physical_path
from ..schemas import (
    NATIVE_TESSELLATION_SCHEMA,
    NATIVE_TRIANGLE_STREAM_MAGIC,
    NATIVE_TRIANGLE_STREAM_VERSION,
)
from .native_scope import (
    NATIVE_SCOPE_IDENTITY_RULE,
    NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
    NATIVE_TRIANGLE_STL_DERIVED,
    NATIVE_SHEET_DISPLAY_GAP,
    NATIVE_SHEET_DISPLAY_GAP_MESSAGE,
    native_binary64_unit_vector,
    native_scope_identity_sha256,
    native_source_scope_is_valid,
)

HEX64 = re.compile(r"[0-9a-f]{64}")
_RAW_HEADER = struct.Struct("<16sII")
_RAW_TRIANGLE = struct.Struct("<9d")
_STL_TRIANGLE = struct.Struct("<12fH")
_STL_HEADER = b"SolidWorks GetTessTriangles(True), metres".ljust(80, b" ")
_STL_BYTES_PER_TRIANGLE = 50


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _integer(value: object) -> bool:
    return type(value) is int


def _finite_float(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _binary32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def _binary32_point(point: Sequence[float]) -> tuple[float, float, float]:
    return tuple(_binary32(value) for value in point)  # type: ignore[return-value]


def native_zero_triangle_evidence_is_valid(body_type: object, evidence: object) -> bool:
    """Validate exact successful same-face API readings without an area threshold."""

    return (
        type(body_type) is int
        and body_type == 1
        and isinstance(evidence, dict)
        and set(evidence) == {
            "get_tess_triangles_return", "get_tess_triangle_count", "get_area_m2",
        }
        and evidence["get_tess_triangles_return"] in ("null", "empty_array")
        and type(evidence["get_tess_triangle_count"]) is int
        and evidence["get_tess_triangle_count"] == 0
        and _finite_float(evidence["get_area_m2"])
        and evidence["get_area_m2"] >= 0
    )


def native_display_gap_value_warnings(warnings: Sequence[dict]) -> list[dict]:
    """Project the verified face gaps into the existing model warning contract."""

    objects = [
        {"object": warning["object"], **warning["detail"]}
        for warning in warnings
        if warning.get("code") == NATIVE_SHEET_DISPLAY_GAP
    ]
    return ([{
        "code": NATIVE_SHEET_DISPLAY_GAP,
        "message": NATIVE_SHEET_DISPLAY_GAP_MESSAGE,
        "objects": objects,
    }] if objects else [])


def native_triangle_relative_path(raw_sha: str) -> str:
    _require(bool(HEX64.fullmatch(raw_sha)), "native triangle digest is invalid")
    return f"raw/triangles/{raw_sha}.triangles"


def native_stl_relative_path(stl_sha: str) -> str:
    _require(bool(HEX64.fullmatch(stl_sha)), "native STL digest is invalid")
    return f"geometry/{stl_sha}.stl"


def _scope_owner_key(
    source_scope_sha256: str,
    body_type: int,
    body_index: int,
) -> str:
    return native_scope_identity_sha256(
        {
            "source_scope_sha256": source_scope_sha256,
            "body_type": body_type,
            "body_index": body_index,
        }
    )


def _entity_identity(
    owner_scope_key: str,
    entity_kind: str,
    native_index: int,
) -> tuple[dict, str]:
    identity = {
        "rule_version": NATIVE_SCOPE_IDENTITY_RULE,
        "entity_kind": entity_kind,
        "persistent": False,
        "owner_scope_key": owner_scope_key,
        "native_index": native_index,
    }
    return identity, native_scope_identity_sha256(identity)


def native_scope_entity(
    source_scope: dict,
    entity_kind: str,
    body_type: int,
    body_index: int,
    native_index: int,
) -> tuple[dict, str]:
    """Return the one derived identity used by producer and verifier."""

    if (
        entity_kind not in {"body", "face"}
        or type(body_type) is not int
        or body_type not in {0, 1}
        or type(body_index) is not int
        or body_index < 0
        or type(native_index) is not int
        or native_index < 0
    ):
        raise ValueError("native scope entity coordinates are invalid")
    source_scope_sha256 = native_scope_identity_sha256(source_scope)
    owner_scope_key = _scope_owner_key(source_scope_sha256, body_type, body_index)
    return _entity_identity(owner_scope_key, entity_kind, native_index)


def write_native_triangle_stream(path: Path, values: Sequence[float]) -> int:
    """Write one component-local ``GetTessTriangles(True)`` result unchanged."""

    target = Path(_physical_path(path))
    _require(
        isinstance(values, Sequence)
        and len(values) > 0
        and len(values) % 9 == 0
        and all(_finite_float(value) for value in values),
        "native triangle stream is empty, malformed, or non-finite",
    )
    triangle_count = len(values) // 9
    _require(triangle_count <= 0xFFFFFFFF, "native triangle stream is too large")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(
            _RAW_HEADER.pack(
                NATIVE_TRIANGLE_STREAM_MAGIC,
                NATIVE_TRIANGLE_STREAM_VERSION,
                triangle_count,
            )
        )
        for offset in range(0, len(values), 9):
            stream.write(_RAW_TRIANGLE.pack(*map(float, values[offset : offset + 9])))
    return triangle_count


def read_native_triangle_count(path: Path) -> int:
    target = Path(_physical_path(path))
    _require(target.is_file(), f"native triangle stream is missing: {target.name}")
    with target.open("rb") as stream:
        header = stream.read(_RAW_HEADER.size)
    _require(len(header) == _RAW_HEADER.size, "native triangle stream header is truncated")
    magic, version, count = _RAW_HEADER.unpack(header)
    _require(magic == NATIVE_TRIANGLE_STREAM_MAGIC, "native triangle stream magic is unsupported")
    _require(version == NATIVE_TRIANGLE_STREAM_VERSION, "native triangle stream version is unsupported")
    expected_size = _RAW_HEADER.size + count * _RAW_TRIANGLE.size
    _require(target.stat().st_size == expected_size, "native triangle stream size is inconsistent")
    return count


@dataclass(frozen=True)
class VerifiedNativeTriangleStream:
    path: Path
    sha256: str
    triangle_count: int


def inspect_native_triangle_stream(
    path: Path,
    *,
    expected_sha256: str,
    expected_count: int,
) -> VerifiedNativeTriangleStream:
    """Hash and validate one stream in a single buffered pass."""

    target = Path(_physical_path(path))
    _require(target.is_file(), f"native triangle stream is missing: {target.name}")
    _require(
        isinstance(expected_sha256, str)
        and HEX64.fullmatch(expected_sha256) is not None,
        "native triangle stream expected digest is malformed",
    )
    _require(
        _integer(expected_count) and expected_count > 0,
        "native triangle stream expected count is malformed",
    )
    digest = hashlib.sha256()
    byte_count = 0
    with target.open("rb") as stream:
        header = stream.read(_RAW_HEADER.size)
        _require(len(header) == _RAW_HEADER.size, "native triangle stream header is truncated")
        magic, version, count = _RAW_HEADER.unpack(header)
        _require(magic == NATIVE_TRIANGLE_STREAM_MAGIC, "native triangle stream magic is unsupported")
        _require(version == NATIVE_TRIANGLE_STREAM_VERSION, "native triangle stream version is unsupported")
        _require(count == expected_count, "native triangle stream count differs from its record")
        digest.update(header)
        byte_count += len(header)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            byte_count += len(block)
    _require(digest.hexdigest() == expected_sha256, "native triangle stream digest differs")
    expected_size = _RAW_HEADER.size + count * _RAW_TRIANGLE.size
    _require(byte_count == expected_size, "native triangle stream size is inconsistent")
    return VerifiedNativeTriangleStream(
        path=target,
        sha256=expected_sha256,
        triangle_count=count,
    )


def iter_native_triangles(
    path: Path,
    *,
    expected_count: int | None = None,
) -> Iterator[tuple[tuple[float, float, float], ...]]:
    """Yield exact raw-order triangles without applying an occurrence transform."""

    target = Path(_physical_path(path))
    count = read_native_triangle_count(target) if expected_count is None else expected_count
    _require(_integer(count) and count > 0, "native triangle stream count is invalid")
    with target.open("rb") as stream:
        header = stream.read(_RAW_HEADER.size)
        _magic, _version, recorded_count = _RAW_HEADER.unpack(header)
        _require(
            recorded_count == count,
            "native triangle stream count differs from its record",
        )
        for triangle_index in range(count):
            payload = stream.read(_RAW_TRIANGLE.size)
            _require(
                len(payload) == _RAW_TRIANGLE.size,
                f"native triangle {triangle_index} is truncated",
            )
            values = _RAW_TRIANGLE.unpack(payload)
            _require(
                all(math.isfinite(value) for value in values),
                f"native triangle {triangle_index} contains a non-finite coordinate",
            )
            yield (
                (values[0], values[1], values[2]),
                (values[3], values[4], values[5]),
                (values[6], values[7], values[8]),
            )
        _require(stream.read(1) == b"", "native triangle stream has trailing bytes")


def read_native_triangle_stream(
    path: Path,
    *,
    expected_count: int | None = None,
) -> list[tuple[tuple[float, float, float], ...]]:
    """Materialize the strict raw stream into memory for one component."""

    return list(iter_native_triangles(Path(path), expected_count=expected_count))


def native_stl_subset(
    source_bytes: bytes,
    triangle_ranges: object,
) -> tuple[bytes, int]:
    """Return one binary STL containing only validated source triangle ranges."""

    _require(isinstance(source_bytes, bytes), "Native STL source is not bytes")
    _require(
        len(source_bytes) >= len(_STL_HEADER) + 4
        and len(source_bytes)
        == len(_STL_HEADER)
        + 4
        + _STL_BYTES_PER_TRIANGLE * struct.unpack_from("<I", source_bytes, 80)[0],
        "Native STL source is not an exact binary STL",
    )
    total = struct.unpack_from("<I", source_bytes, 80)[0]
    _require(
        isinstance(triangle_ranges, list) and bool(triangle_ranges),
        "Native appearance triangle ranges are missing",
    )
    records = []
    next_start = 0
    for item in triangle_ranges:
        _require(
            isinstance(item, dict)
            and set(item) == {"triangle_start", "triangle_count"}
            and _integer(item["triangle_start"])
            and _integer(item["triangle_count"])
            and item["triangle_start"] >= next_start
            and item["triangle_count"] > 0,
            "Native appearance triangle ranges are not positive, ordered, and non-overlapping",
        )
        start = item["triangle_start"]
        count = item["triangle_count"]
        _require(
            start + count <= total,
            "Native appearance triangle range exceeds the source STL",
        )
        offset = len(_STL_HEADER) + 4 + _STL_BYTES_PER_TRIANGLE * start
        records.append(
            source_bytes[offset : offset + _STL_BYTES_PER_TRIANGLE * count]
        )
        next_start = start + count
    payload = b"".join(records)
    count = len(payload) // _STL_BYTES_PER_TRIANGLE
    _require(
        count > 0 and len(payload) == count * _STL_BYTES_PER_TRIANGLE,
        "Native appearance subset is empty",
    )
    return (
        _STL_HEADER
        + struct.pack("<I", count)
        + payload,
        count,
    )


def materialize_native_stl(
    raw_path: Path,
    stl_path: Path,
    spans: Sequence[dict],
    *,
    triangle_count: int | None = None,
) -> int:
    """Convert one raw triangle stream to binary STL after the CAD session closes."""

    ordered = sorted(spans, key=lambda span: span["triangle_start"])
    _require(bool(ordered), "native triangle materialization has no face spans")
    triangle_count = (
        read_native_triangle_count(Path(raw_path))
        if triangle_count is None
        else triangle_count
    )
    next_start = 0
    for span in ordered:
        _require(
            span["triangle_start"] == next_start and _integer(span["triangle_count"])
            and span["triangle_count"] >= 0
            and (span["triangle_count"] > 0 or native_zero_triangle_evidence_is_valid(
                span.get("body_type"), span.get("zero_triangle_evidence"),
            )),
            "native face spans are not a complete contiguous triangle partition",
        )
        next_start += span["triangle_count"]
    _require(next_start == triangle_count, "native face spans do not cover the raw triangle stream")

    target = Path(_physical_path(stl_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = [span for span in ordered if span["triangle_count"] > 0]
    span_index = 0
    local_triangle = 0
    with target.open("xb") as stream:
        stream.write(_STL_HEADER)
        stream.write(struct.pack("<I", triangle_count))
        for triangle in iter_native_triangles(Path(raw_path), expected_count=triangle_count):
            while local_triangle >= ordered[span_index]["triangle_count"]:
                span_index += 1
                local_triangle = 0
            owner = ordered[span_index]["face_owner_id"]
            first = tuple(triangle[1][axis] - triangle[0][axis] for axis in range(3))
            second = tuple(triangle[2][axis] - triangle[0][axis] for axis in range(3))
            cross = (
                first[1] * second[2] - first[2] * second[1],
                first[2] * second[0] - first[0] * second[2],
                first[0] * second[1] - first[1] * second[0],
            )
            _require(all(math.isfinite(value) for value in cross) and any(cross), "native triangle is degenerate")
            stream.write(
                _STL_TRIANGLE.pack(
                    *native_binary64_unit_vector(cross),
                    *(value for point in triangle for value in point),
                    owner,
                )
            )
            local_triangle += 1
    return triangle_count


def native_triangle_stl_warning(
    component: str,
    *,
    raw_path: str,
    raw_sha: str,
    path: str,
    sha: str,
    triangles: int,
) -> dict:
    """Return the single warning for binary32 STL and normal derivation."""

    return {
        "code": NATIVE_TRIANGLE_STL_DERIVED,
        "blocking": False,
        "severity": "warning",
        "message": (
            "Binary STL vertices are the binary32 encoding of native "
            "GetTessTriangles(True) points; facet normals are derived from "
            "the native triangle winding."
        ),
        "object": f"{component}:geometry",
        "detail": {
            "component": component,
            "field": "geometry",
            "raw_path": raw_path,
            "raw_sha": raw_sha,
            "triangle_count": triangles,
            "stl_path": path,
            "stl_sha": sha,
            "normal_rule": "binary64-cross-product-unit-v1",
            "point_rule": "native-binary64-to-binary32-v1",
            "precision_qualification": "not_established",
        },
    }


def _scope_warning(
    *,
    component: str,
    entity_kind: str,
    body_type: int,
    body_index: int,
    object_index: int,
    source_scope_sha256: str,
    owner_scope_key: str,
    field: str,
) -> dict:
    return {
        "code": NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
        "blocking": False,
        "severity": "warning",
        "message": (f"Native {entity_kind} identity is derived from the sealed source scope and native array index."),
        "object": f"{component}:{entity_kind}:{body_type}/{body_index}",
        "detail": {
            "component": component,
            "entity_kind": entity_kind,
            "body_type": body_type,
            "body_index": body_index,
            "owner_scope_key": owner_scope_key,
            "persistent": False,
            "rule_version": NATIVE_SCOPE_IDENTITY_RULE,
            "object_indices": [object_index],
            "field": field,
            "source_scope_sha256": source_scope_sha256,
        },
    }


def build_native_tessellation(
    component: str,
    body_inventory: object,
    face_inventory: object,
    *,
    source_scope: object,
) -> dict:
    """Add post-release body/face references to raw native indices."""

    _require(
        native_source_scope_is_valid(source_scope, component),
        f"{component}: native source scope is invalid",
    )
    _require(
        isinstance(body_inventory, list) and bool(body_inventory),
        f"{component}: native body inventory is missing",
    )
    _require(
        isinstance(face_inventory, list) and bool(face_inventory),
        f"{component}: native face inventory is missing",
    )
    bodies = []
    for body in body_inventory:
        _require(
            isinstance(body, dict) and set(body) == {"body_type", "body_index", "body_name", "face_count"},
            f"{component}: raw native body inventory has an unknown shape",
        )
        body_type = body["body_type"]
        body_index = body["body_index"]
        _require(
            _integer(body_type) and body_type in {0, 1} and _integer(body_index) and body_index >= 0,
            f"{component}: raw native body identity is invalid",
        )
        reference = native_scope_entity(
            source_scope,
            "body",
            body_type,
            body_index,
            body_index,
        )[1]
        bodies.append({**body, "body_reference_sha256": reference})
    spans = []
    for span in face_inventory:
        _require(
            isinstance(span, dict)
            and set(span)
            == {
                "body_type",
                "body_index",
                "face_index",
                "triangle_start",
                "triangle_count",
            } | ({"zero_triangle_evidence"} if span.get("triangle_count") == 0 else set()),
            f"{component}: raw native face inventory has an unknown shape",
        )
        _require(
            _integer(span["body_type"])
            and span["body_type"] in {0, 1}
            and _integer(span["body_index"])
            and span["body_index"] >= 0
            and _integer(span["face_index"])
            and span["face_index"] >= 0,
            f"{component}: raw native face identity is invalid",
        )
        reference = native_scope_entity(
            source_scope,
            "face",
            span["body_type"],
            span["body_index"],
            span["face_index"],
        )[1]
        spans.append({**span, "face_reference_sha256": reference})
    tessellation = {
        "schema_version": NATIVE_TESSELLATION_SCHEMA,
        "faces": len(spans),
        "bodies": bodies,
        "face_spans": spans,
    }
    return tessellation


def parse_native_tessellation(
    component: str,
    tessellation: object,
    bodies: object,
    triangles: object,
    *,
    source_scope: object,
    warnings: list[dict],
) -> list[dict]:
    """Validate the one native-triangle schema and return exact face spans."""

    _require(isinstance(warnings, list) and not warnings, f"{component}: warning output must be empty")
    _require(
        isinstance(tessellation, dict) and set(tessellation) == {"schema_version", "faces", "bodies", "face_spans"},
        f"{component}: native triangle schema has unknown or legacy fields",
    )
    _require(
        tessellation["schema_version"] == NATIVE_TESSELLATION_SCHEMA,
        f"{component}: native triangle schema version is unsupported",
    )
    _require(
        isinstance(bodies, dict)
        and set(bodies) == {"solid", "sheet"}
        and all(_integer(value) and value >= 0 for value in bodies.values())
        and sum(bodies.values()) > 0,
        f"{component}: native body counts are invalid",
    )
    _require(_integer(triangles) and triangles > 0, f"{component}: native triangle count is invalid")
    _require(
        native_source_scope_is_valid(source_scope, component),
        f"{component}: native source scope is invalid",
    )
    source_scope_sha256 = native_scope_identity_sha256(source_scope)

    body_rows = tessellation["bodies"]
    _require(isinstance(body_rows, list), f"{component}: native body inventory is missing")
    expected_body_rows = bodies["solid"] + bodies["sheet"]
    _require(len(body_rows) == expected_body_rows, f"{component}: native body inventory count differs")
    body_by_key: dict[tuple[int, int], dict] = {}
    seen_body_keys = set()
    counts = {0: 0, 1: 0}
    for index, body in enumerate(body_rows):
        _require(
            isinstance(body, dict)
            and set(body)
            == {
                "body_type",
                "body_index",
                "body_name",
                "body_reference_sha256",
                "face_count",
            },
            f"{component}: native body {index} has an unknown shape",
        )
        body_type = body["body_type"]
        body_index = body["body_index"]
        _require(
            _integer(body_type)
            and body_type in (0, 1)
            and _integer(body_index)
            and body_index >= 0
            and (body["body_name"] is None or isinstance(body["body_name"], str))
            and _integer(body["face_count"])
            and body["face_count"] > 0,
            f"{component}: native body {index} is invalid",
        )
        key = (body_type, body_index)
        _require(key not in seen_body_keys, f"{component}: native body inventory repeats {key}")
        seen_body_keys.add(key)
        counts[body_type] += 1
        owner_scope_key = _scope_owner_key(source_scope_sha256, body_type, body_index)
        _identity, reference = _entity_identity(owner_scope_key, "body", body_index)
        _require(
            body["body_reference_sha256"] == reference,
            f"{component}: native body reference differs from its sealed scope",
        )
        body_by_key[key] = body
        warnings.append(
            _scope_warning(
                component=component,
                entity_kind="body",
                body_type=body_type,
                body_index=body_index,
                object_index=body_index,
                source_scope_sha256=source_scope_sha256,
                owner_scope_key=owner_scope_key,
                field="body_reference_sha256",
            )
        )
    _require(
        counts == {0: bodies["solid"], 1: bodies["sheet"]},
        f"{component}: native body types differ from the recorded inventories",
    )

    spans = tessellation["face_spans"]
    _require(
        _integer(tessellation["faces"])
        and tessellation["faces"] > 0
        and isinstance(spans, list)
        and len(spans) == tessellation["faces"],
        f"{component}: native face count and span inventory differ",
    )
    face_counts = dict.fromkeys(body_by_key, 0)
    next_start = 0
    result = []
    for span_index, span in enumerate(spans):
        _require(
            isinstance(span, dict)
            and set(span)
            == {
                "body_type",
                "body_index",
                "face_index",
                "face_reference_sha256",
                "triangle_start",
                "triangle_count",
            } | ({"zero_triangle_evidence"} if span.get("triangle_count") == 0 else set()),
            f"{component}: native face span {span_index} has an unknown shape",
        )
        key = (span["body_type"], span["body_index"])
        _require(key in body_by_key, f"{component}: native face span names an unknown body")
        _require(
            _integer(span["face_index"])
            and span["face_index"] == face_counts[key]
            and span["triangle_start"] == next_start
            and _integer(span["triangle_count"])
            and span["triangle_count"] >= 0
            and (span["triangle_count"] > 0 or native_zero_triangle_evidence_is_valid(
                span["body_type"], span.get("zero_triangle_evidence"),
            )),
            f"{component}: native face spans are not a complete ordered partition",
        )
        owner_scope_key = _scope_owner_key(source_scope_sha256, *key)
        _identity, reference = _entity_identity(owner_scope_key, "face", span["face_index"])
        _require(
            span["face_reference_sha256"] == reference,
            f"{component}: native face reference differs from its sealed scope",
        )
        if span["triangle_count"] == 0:
            warnings.append({
                "code": NATIVE_SHEET_DISPLAY_GAP,
                "blocking": False,
                "severity": "warning",
                "message": NATIVE_SHEET_DISPLAY_GAP_MESSAGE,
                "object": f"{component}:face:{key[0]}/{key[1]}/{span['face_index']}",
                "detail": {
                    "component": component,
                    "field": "geometry.tessellation.face_spans",
                    "body_type": key[0],
                    "body_index": key[1],
                    "face_index": span["face_index"],
                    "face_reference_sha256": reference,
                    "source_scope_sha256": source_scope_sha256,
                    "triangle_start": span["triangle_start"],
                    "triangle_count": 0,
                    "zero_triangle_evidence": dict(span["zero_triangle_evidence"]),
                },
            })
        face_counts[key] += 1
        next_start += span["triangle_count"]
        result.append({**span, "face_owner_id": span_index + 1})
        warnings.append(
            _scope_warning(
                component=component,
                entity_kind="face",
                body_type=key[0],
                body_index=key[1],
                object_index=span["face_index"],
                source_scope_sha256=source_scope_sha256,
                owner_scope_key=owner_scope_key,
                field="face_reference_sha256",
            )
        )
    _require(
        all(face_counts[key] == body_by_key[key]["face_count"] for key in body_by_key),
        f"{component}: native face spans do not match every body's face inventory",
    )
    _require(next_start == triangles, f"{component}: native face spans do not cover every triangle")
    _require(len(result) <= 0xFFFF, f"{component}: native face owners exceed binary STL capacity")
    return result


def summarize_native_topology(
    spans: Sequence[dict],
    triangles: Sequence[tuple[tuple[float, float, float], ...]],
) -> dict:
    """Derive exact triangle-edge incidence for each native body."""

    rows = {}
    edge_owners: dict[tuple[int, int], dict[tuple, list[int]]] = {}
    for span in spans:
        key = (span["body_type"], span["body_index"])
        row = rows.setdefault(
            key,
            {
                "body_type": key[0],
                "body_index": key[1],
                "faces": 0,
                "triangles": 0,
                "triangle_edges": 0,
                "open_edges": 0,
                "shared_edges": 0,
                "non_manifold_edges": 0,
            },
        )
        row["faces"] += 1
        row["triangles"] += span["triangle_count"]
        body_edges = edge_owners.setdefault(key, {})
        start = span["triangle_start"]
        for triangle in triangles[start : start + span["triangle_count"]]:
            points = tuple(_binary32_point(point) for point in triangle)
            for left, right in ((0, 1), (1, 2), (2, 0)):
                edge = tuple(sorted((points[left], points[right])))
                body_edges.setdefault(edge, []).append(span["face_owner_id"])
    for key, row in rows.items():
        incidences = edge_owners[key]
        row["triangle_edges"] = len(incidences)
        row["open_edges"] = sum(len(owners) == 1 for owners in incidences.values())
        row["shared_edges"] = sum(len(owners) == 2 for owners in incidences.values())
        row["non_manifold_edges"] = sum(len(owners) > 2 for owners in incidences.values())
    return {"bodies": [rows[key] for key in sorted(rows)]}
