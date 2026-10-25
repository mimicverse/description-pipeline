"""Fixtures for source-adapter tests; the backend never touches live CAD."""

from __future__ import annotations

import copy
import hashlib
import os
import struct
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from description_pipeline.geometry.native_u import (
    NATIVE_CURVE_DOMAIN_RULE,
    NATIVE_CURVE_ENDPOINT_OPERATION_RULE,
    NATIVE_IDENTITY_POLICY_RULE,
    NATIVE_PERSISTENT_IDENTITY_RULE,
    NATIVE_POINT_ROUNDTRIP_RULE,
    NATIVE_TESSELLATION_EDGE_PROJECTION_RULE,
    NATIVE_TESSELLATION_EDGE_PROJECTION_METRIC,
    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
    NATIVE_U_CLOSEST_POINT_PARAMETER_RULE,
    NATIVE_U_IDENTITY_RULE,
    NATIVE_U_RESOLUTION_RULE,
    native_curve_domain_transform,
    native_curve_endpoint_operation_tolerance,
    native_curve_parameter_endpoints,
    native_curve_topology_endpoint_set_assignment,
    native_evaluate2_evidence,
    native_point_error,
)
from description_pipeline.geometry.native_planar_brep import NATIVE_PLANAR_BREP_SCHEMA
from description_pipeline.io import file_digest
from description_pipeline.schemas import (
    NATIVE_ID_ARRAY_SOURCE_FLAT,
    NATIVE_TESSELLATION_SCHEMA,
)
from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.sources.solidworks.protocol import CadBackend, Matrix, RawComponent, RawScene

NATIVE_TESSELLATION_PROPERTIES = {
    "improved_quality": True,
    "match_type": 2,
    "need_error_list": True,
    "need_face_facet_map": True,
    "need_edge_fin_map": True,
    "need_vertex_params": True,
}


def native_tessellation_request() -> dict[str, Any]:
    return {
        "api": "IBody2.GetTessellation",
        "argument_count": 1,
        "face_list": {
            "scope": "whole_body",
            "variant_type": "VT_EMPTY",
            "value": None,
        },
    }


def write_binary_stl(
    path: Path,
    triangles: Sequence[Sequence[Sequence[float]]],
    *,
    attributes: Sequence[int] | None = None,
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    attributes = list(attributes or [0] * len(triangles))
    if len(attributes) != len(triangles):
        raise ValueError("binary STL attributes must match the triangle count")
    header = b"fixture stl".ljust(80, b" ")
    payload = bytearray()
    for triangle, attribute in zip(triangles, attributes, strict=True):
        first, second, third = triangle
        payload += struct.pack("<3f", 0.0, 0.0, 1.0)
        for vertex in (first, second, third):
            payload += struct.pack("<3f", float(vertex[0]), float(vertex[1]), float(vertex[2]))
        payload += struct.pack("<H", attribute)
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(struct.pack("<I", len(triangles)))
        handle.write(bytes(payload))
    return len(triangles)


def cube_triangles(
    half: float = 0.02,
) -> tuple[list[tuple[tuple[float, float, float], ...]], tuple[int, ...]]:
    corners = [(x, y, z) for x in (-half, half) for y in (-half, half) for z in (-half, half)]
    faces = [
        (0, 2, 3),
        (0, 3, 1),
        (4, 5, 7),
        (4, 7, 6),
        (0, 1, 5),
        (0, 5, 4),
        (2, 6, 7),
        (2, 7, 3),
        (0, 4, 6),
        (0, 6, 2),
        (1, 3, 7),
        (1, 7, 5),
    ]
    return (
        [(corners[a], corners[b], corners[c]) for a, b, c in faces],
        (1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6),
    )


def cube_stl(path: Path, half: float = 0.02) -> int:
    triangles, attributes = cube_triangles(half)
    return write_binary_stl(path, triangles, attributes=attributes)


def cube_native_edges(half: float = 0.02) -> tuple[dict[int, list[str]], list[dict]]:
    """Return the cube's twelve native edges and the references on each owner face."""

    corners = [(x, y, z) for x in (-half, half) for y in (-half, half) for z in (-half, half)]
    face_corners = (
        (0, 1, 2, 3),
        (4, 5, 6, 7),
        (0, 1, 4, 5),
        (2, 3, 6, 7),
        (0, 2, 4, 6),
        (1, 3, 5, 7),
    )
    face_edges: dict[int, list[str]] = {face_index: [] for face_index in range(len(face_corners))}
    native_edges = []
    for start, end in (
        (0, 1),
        (0, 2),
        (0, 4),
        (1, 3),
        (1, 5),
        (2, 3),
        (2, 6),
        (3, 7),
        (4, 5),
        (4, 6),
        (5, 7),
        (6, 7),
    ):
        face_indices = [
            face_index
            for face_index, owners in enumerate(face_corners)
            if start in owners and end in owners
        ]
        if len(face_indices) != 2:
            raise AssertionError(f"cube edge {start}-{end} must have two owner faces")
        reference = hashlib.sha256(f"fixture-cube-edge-{start}-{end}".encode()).hexdigest()
        for face_index in face_indices:
            face_edges[face_index].append(reference)
        native_edges.append(
            {
                "body_type": 0,
                "body_index": 0,
                "edge_reference_sha256": reference,
                "start_vertex_reference_sha256": hashlib.sha256(
                    f"fixture-cube-vertex-{start}".encode()
                ).hexdigest(),
                "end_vertex_reference_sha256": hashlib.sha256(
                    f"fixture-cube-vertex-{end}".encode()
                ).hexdigest(),
                "start_point_m": list(corners[start]),
                "end_point_m": list(corners[end]),
                "face_indices": face_indices,
            }
        )
    for references in face_edges.values():
        references.sort()
    return face_edges, native_edges


ITESSELLATION_IID = "{699C7492-DB56-4CC2-B7A2-9FFD56698434}"
NATIVE_VT_BYREF_R8 = 0x4005
NATIVE_VT_BYREF_BOOL = 0x400B


class NativeTessellationV3Fixture(dict):
    """A v3 envelope with the exact STL bytes generated from its facet graph."""

    def __init__(
        self,
        envelope: dict[str, Any],
        *,
        stl_bytes: bytes,
        triangle_vertices: Sequence[Sequence[Sequence[float]]],
    ):
        super().__init__(envelope)
        self.stl_bytes = stl_bytes
        self.triangle_vertices = [
            tuple(tuple(float(value) for value in point) for point in triangle)
            for triangle in triangle_vertices
        ]


def _binary32_point(point: Sequence[float]) -> tuple[float, float, float]:
    values = tuple(float(value) for value in point)
    if len(values) != 3:
        raise ValueError("native tessellation point must have three coordinates")
    return struct.unpack("<3f", struct.pack("<3f", *values))


def _packed_evaluate2_success() -> float:
    return struct.unpack("<d", struct.pack("<Q", 1))[0]


def _evaluate2_evidence(
    point: tuple[float, float, float],
) -> tuple[list[float], dict[str, Any]]:
    raw = [*point, 0.0, 0.0, 0.0, _packed_evaluate2_success()]
    evidence = native_evaluate2_evidence(raw)
    if (
        evidence is None
        or evidence["packed_status"]["parsed_success"] is not True
    ):
        raise ValueError("native Evaluate2 fixture status is not successful")
    return evidence["payload"], evidence["packed_status"]


def _u_evidence(
    u_parameter: float,
    point: tuple[float, float, float],
    *,
    projected_point_m: Sequence[float] | None = None,
    evaluated_point_m: Sequence[float] | None = None,
) -> dict[str, Any]:
    projected_point = (
        point
        if projected_point_m is None
        else tuple(float(value) for value in projected_point_m)
    )
    evaluated_source = (
        point
        if evaluated_point_m is None
        else tuple(float(value) for value in evaluated_point_m)
    )
    payload, packed_status = _evaluate2_evidence(evaluated_source)
    evaluated_point = tuple(payload[:3])
    raw_evaluate2 = [
        *payload,
        struct.unpack("<d", struct.pack("<Q", 1))[0],
    ]
    return {
        "u": u_parameter,
        "u_parameter_rule": NATIVE_U_CLOSEST_POINT_PARAMETER_RULE,
        "rule_version": NATIVE_U_IDENTITY_RULE,
        "projection_rule": NATIVE_TESSELLATION_EDGE_PROJECTION_RULE,
        "projection_metric": NATIVE_TESSELLATION_EDGE_PROJECTION_METRIC,
        "projection_tolerance_m": (
            NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
        ),
        "source_point_m": list(point),
        "closest_point_on_raw": [*projected_point, u_parameter],
        "evaluate2_raw": raw_evaluate2,
        "evaluate_payload": payload,
        "evaluate_packed_status": packed_status,
        "projection_errors_m": {
            "source_to_closest": native_point_error(
                point,
                projected_point,
            ),
            "closest_to_evaluate": native_point_error(
                projected_point,
                evaluated_point,
            ),
            "source_to_evaluate": native_point_error(
                point,
                evaluated_point,
            ),
        },
    }


def _curve_endpoint_evaluation(
    u_parameter: float,
    point: Sequence[float],
    opposite_point: Sequence[float],
) -> dict[str, Any]:
    expected = _binary32_point(point)
    opposite = _binary32_point(opposite_point)
    payload, packed_status = _evaluate2_evidence(expected)
    evaluated = tuple(payload[:3])
    return {
        "u": u_parameter,
        "point_roundtrip_rule": NATIVE_CURVE_ENDPOINT_OPERATION_RULE,
        "payload": payload,
        "packed_status": packed_status,
        "error": None,
        "point_tolerance_m": native_curve_endpoint_operation_tolerance(
            expected,
            evaluated,
            opposite,
        ),
        "point_error_m": max(
            abs(expected[index] - evaluated[index])
            for index in range(3)
        ),
    }


def replace_curve_endpoint_evaluation(
    evaluation: dict[str, Any],
    point: Sequence[float],
    opposite_point: Sequence[float],
) -> None:
    """Rebind one fixture evaluation to a new expected native endpoint."""

    evaluation.update(
        _curve_endpoint_evaluation(
            evaluation["u"],
            point,
            opposite_point,
        )
    )


def replace_u_evidence(
    evidence: dict[str, Any],
    point: Sequence[float],
) -> None:
    """Rebind one fin endpoint proof to a changed native vertex point."""

    evidence.update(_u_evidence(evidence["u"], _binary32_point(point)))


def _curve_end_params(
    start: float,
    end: float,
    *,
    is_closed: bool,
    is_periodic: bool,
) -> dict[str, Any]:
    return {
        "call_form": "pywin32-byref-variant",
        "success": True,
        "error": None,
        "start": start,
        "end": end,
        "is_closed": is_closed,
        "is_periodic": is_periodic,
        "raw_outputs": [
            {"value_type": NATIVE_VT_BYREF_R8, "value": start},
            {"value_type": NATIVE_VT_BYREF_R8, "value": end},
            {"value_type": NATIVE_VT_BYREF_BOOL, "value": is_closed},
            {"value_type": NATIVE_VT_BYREF_BOOL, "value": is_periodic},
        ],
    }


def native_tessellation_fixture(
    face_spans: Sequence[dict[str, Any]],
    native_edges: Sequence[dict[str, Any]],
    no_facets_faces: Sequence[dict[str, Any]] = (),
    *,
    triangle_vertices: Sequence[Sequence[Sequence[float]]] | None = None,
) -> NativeTessellationV3Fixture:
    """Build one closed v3 graph and its byte-identical binary STL projection."""

    spans = copy.deepcopy(list(face_spans))
    input_edges = copy.deepcopy(list(native_edges))
    normalized_no_facets = copy.deepcopy(list(no_facets_faces))
    for row in normalized_no_facets:
        row.setdefault("source_rule", NATIVE_ID_ARRAY_SOURCE_FLAT)
        row.setdefault("warnings", [])
        row.setdefault("derived_empty_edges", [])
    for span in spans:
        span.setdefault(
            "native_body_reference_sha256",
            span["body_reference_sha256"],
        )
        span.setdefault(
            "native_face_reference_sha256",
            span["face_reference_sha256"],
        )
        span.setdefault(
            "face_identity_mode",
            NATIVE_PERSISTENT_IDENTITY_RULE,
        )
        span.setdefault("face_identity", None)
    for edge in input_edges:
        edge.setdefault(
            "native_edge_reference_sha256",
            edge["edge_reference_sha256"],
        )
        edge.setdefault(
            "edge_identity_mode",
            NATIVE_PERSISTENT_IDENTITY_RULE,
        )
        edge.setdefault("edge_identity", None)
        edge.setdefault("mapping_source_edge_index", None)
        edge.setdefault(
            "mapping_source_edge_persistent_reference_sha256",
            None,
        )
    fixture_triangles = []
    body_keys = sorted({(span["body_type"], span["body_index"]) for span in spans})
    evidence_by_body: dict[tuple[int, int], dict[str, Any]] = {}
    body_rows = []
    vertex_rows = []
    next_facet_id = 1
    next_fin_id = 1
    next_vertex_id = 1

    for body_type, body_index in body_keys:
        body_key = (body_type, body_index)
        body_spans = sorted(
            (
                span
                for span in spans
                if (span["body_type"], span["body_index"]) == body_key
            ),
            key=lambda span: span["face_index"],
        )
        faces = []
        facets = []
        fins = []
        vertex_ids_by_point = {}
        points_by_vertex_id = {}
        vertex_ids_by_face = {}
        fins_by_face = {}
        for span in body_spans:
            facet_ids = []
            for local_index in range(span["triangle_count"]):
                triangle_record_index = span["triangle_start"] + local_index
                if triangle_vertices is None:
                    points = [
                        [float(triangle_record_index), float(vertex_index), 0.0]
                        for vertex_index in range(3)
                    ]
                else:
                    points = triangle_vertices[triangle_record_index]
                    if len(points) != 3:
                        raise ValueError("native tessellation triangle must have three vertices")
                facet_id = next_facet_id
                next_facet_id += 1
                vertex_ids = []
                for point in points:
                    point_key = _binary32_point(point)
                    vertex_id = vertex_ids_by_point.get(point_key)
                    if vertex_id is None:
                        vertex_id = next_vertex_id
                        next_vertex_id += 1
                        vertex_ids_by_point[point_key] = vertex_id
                        points_by_vertex_id[vertex_id] = point_key
                        vertex_rows.append(
                            {
                                "body_type": body_type,
                                "body_index": body_index,
                                "vertex_id": vertex_id,
                                "point_m": list(point_key),
                            }
                        )
                    vertex_ids.append(vertex_id)
                if len(set(vertex_ids)) != 3:
                    raise ValueError("native tessellation facet needs three distinct binary32 vertices")
                fixture_triangles.append(
                    tuple(_binary32_point(point) for point in points)
                )
                fin_ids = [next_fin_id, next_fin_id + 1, next_fin_id + 2]
                next_fin_id += 3
                for fin_id, side in zip(
                    fin_ids,
                    (
                        (vertex_ids[0], vertex_ids[1]),
                        (vertex_ids[1], vertex_ids[2]),
                        (vertex_ids[2], vertex_ids[0]),
                    ),
                    strict=True,
                ):
                    fins.append(
                        {
                            "fin_id": fin_id,
                            "facet_id": facet_id,
                            "face_index": span["face_index"],
                            "vertex_ids": list(side),
                            "edge_reference_sha256": None,
                            "classification": "internal_only",
                            "start_u": None,
                            "end_u": None,
                            "start_evidence": None,
                            "end_evidence": None,
                            "cofin_raw_id": None,
                            "cofin_id": None,
                        }
                    )
                facets.append(
                    {
                        "facet_id": facet_id,
                        "face_index": span["face_index"],
                        "fin_ids": fin_ids,
                        "vertex_ids": sorted(vertex_ids),
                        "triangle_record_index": triangle_record_index,
                    }
                )
                fins_by_face.setdefault(span["face_index"], []).extend(fin_ids)
                vertex_ids_by_face.setdefault(span["face_index"], set()).update(
                    vertex_ids
                )
                facet_ids.append(facet_id)
            faces.append(
                {
                    "face_index": span["face_index"],
                    "facet_ids": facet_ids,
                    "facet_count": len(facet_ids),
                    "facet_ids_source": NATIVE_ID_ARRAY_SOURCE_FLAT,
                    "raw_facet_ids": list(facet_ids),
                    "warnings": [],
                    "face_identity_sha256": span[
                        "face_reference_sha256"
                    ],
                }
            )
        evidence_by_body[body_key] = {
            "spans": body_spans,
            "faces": faces,
            "facets": facets,
            "fins": fins,
            "fins_by_face": fins_by_face,
            "vertex_ids_by_face": vertex_ids_by_face,
            "vertex_ids_by_point": vertex_ids_by_point,
            "points_by_vertex_id": points_by_vertex_id,
            "assigned_fin_ids": set(),
        }

    span_by_face = {
        (span["body_type"], span["body_index"], span["face_index"]): span
        for span in spans
    }

    def directed_path(
        body: dict[str, Any],
        face_index: int,
        source: int,
        target: int,
        allowed_vertices: set[int],
    ) -> list[dict[str, Any]] | None:
        fins_by_id = {fin["fin_id"]: fin for fin in body["fins"]}
        adjacency: dict[int, list[dict[str, Any]]] = {}
        for fin_id in body["fins_by_face"].get(face_index, []):
            if fin_id in body["assigned_fin_ids"]:
                continue
            fin = fins_by_id[fin_id]
            if not set(fin["vertex_ids"]) <= allowed_vertices:
                continue
            adjacency.setdefault(fin["vertex_ids"][0], []).append(fin)
        queue = deque([(source, ())])
        while queue:
            current, path = queue.popleft()
            if current == target and path:
                return list(path)
            visited = {source, *(fin["vertex_ids"][1] for fin in path)}
            for fin in adjacency.get(current, []):
                destination = fin["vertex_ids"][1]
                if destination not in visited:
                    queue.append((destination, (*path, fin)))
        return None

    edge_rows = []
    for edge in input_edges:
        body_key = (edge["body_type"], edge["body_index"])
        body = evidence_by_body[body_key]
        face_indices = sorted(edge["face_indices"])
        active_faces = [
            face_index
            for face_index in face_indices
            if span_by_face[(*body_key, face_index)]["triangle_count"] > 0
        ]
        paths_by_face = {}
        if active_faces:
            if len(active_faces) not in (1, 2):
                raise ValueError("native edge fixture requires one or two active cofaces")
            source = body["vertex_ids_by_point"].get(_binary32_point(edge["start_point_m"]))
            target = body["vertex_ids_by_point"].get(_binary32_point(edge["end_point_m"]))
            if source is None or target is None or source == target:
                raise ValueError("native edge endpoints are not shared body vertices")
            shared_vertices = set.intersection(
                *(body["vertex_ids_by_face"][face_index] for face_index in active_faces)
            )
            fins_by_id = {fin["fin_id"]: fin for fin in body["fins"]}
            for face_index in active_faces:
                direct = [
                    fin
                    for fin_id in body["fins_by_face"].get(face_index, [])
                    for fin in (fins_by_id[fin_id],)
                    if frozenset(fin["vertex_ids"]) == frozenset((source, target))
                    and fin_id not in body["assigned_fin_ids"]
                ]
                if len(direct) == 1:
                    path = direct
                else:
                    path = directed_path(
                        body,
                        face_index,
                        source,
                        target,
                        shared_vertices,
                    )
                    if path is None:
                        path = directed_path(
                            body,
                            face_index,
                            target,
                            source,
                            shared_vertices,
                        )
                if not path:
                    raise ValueError(f"native edge endpoint path is missing on face {face_index}")
                paths_by_face[face_index] = path

        u_min = float(edge.get("u_min", 0.0))
        u_max = float(edge.get("u_max", 1.0))
        if not u_min < u_max:
            raise ValueError("native edge fixture U domain must be increasing")
        edge_fin_ids = []
        path_senses = {}
        if active_faces:
            first_face = active_faces[0]
            second_face = active_faces[1] if len(active_faces) == 2 else None
            source = body["vertex_ids_by_point"][_binary32_point(edge["start_point_m"])]
            first_path = paths_by_face[first_face]
            if first_path[0]["vertex_ids"][0] != source:
                first_path = list(reversed(first_path))
                path_senses[first_face] = False
            else:
                path_senses[first_face] = True
            canonical_segments = []
            current = source
            for fin in first_path:
                if fin["vertex_ids"][0] == current:
                    segment = tuple(fin["vertex_ids"])
                elif fin["vertex_ids"][1] == current:
                    segment = tuple(reversed(fin["vertex_ids"]))
                else:
                    raise ValueError("native edge face path is disconnected")
                canonical_segments.append((segment, fin))
                current = segment[1]
            if current != body["vertex_ids_by_point"][_binary32_point(edge["end_point_m"])]:
                raise ValueError("native edge face path does not reach its endpoint")
            if len(active_faces) == 1:
                step = (u_max - u_min) / len(canonical_segments)
                for index, (segment, first_fin) in enumerate(
                    canonical_segments
                ):
                    low = u_min + step * index
                    high = u_min + step * (index + 1)
                    first_reversed = (
                        first_fin["vertex_ids"][0] != segment[0]
                    )
                    first_start_u = high if first_reversed else low
                    first_end_u = low if first_reversed else high
                    first_fin.update(
                        {
                            "edge_reference_sha256": edge[
                                "edge_reference_sha256"
                            ],
                            "classification": "native_edge",
                            "start_u": first_start_u,
                            "end_u": first_end_u,
                            "start_evidence": _u_evidence(
                                first_start_u,
                                body["points_by_vertex_id"][
                                    first_fin["vertex_ids"][0]
                                ],
                            ),
                            "end_evidence": _u_evidence(
                                first_end_u,
                                body["points_by_vertex_id"][
                                    first_fin["vertex_ids"][1]
                                ],
                            ),
                            "cofin_raw_id": -1,
                            "cofin_id": None,
                        }
                    )
                    body["assigned_fin_ids"].add(first_fin["fin_id"])
                    edge_fin_ids.append(first_fin["fin_id"])
            else:
                second_path = paths_by_face[second_face]
                path_senses[second_face] = second_path[0]["vertex_ids"][0] == body[
                    "vertex_ids_by_point"
                ][_binary32_point(edge["start_point_m"])]
                second_by_segment = {}
                for fin in second_path:
                    key = frozenset(fin["vertex_ids"])
                    if key in second_by_segment:
                        raise ValueError("native edge coface has duplicate segment fins")
                    second_by_segment[key] = fin
                if len(canonical_segments) != len(second_path):
                    raise ValueError("native edge cofaces have different segment counts")
                step = (u_max - u_min) / len(canonical_segments)
                for index, (segment, first_fin) in enumerate(canonical_segments):
                    second_fin = second_by_segment.get(frozenset(segment))
                    if second_fin is None:
                        raise ValueError("native edge cofaces do not share segment endpoints")
                    if tuple(second_fin["vertex_ids"]) != tuple(
                        reversed(first_fin["vertex_ids"])
                    ):
                        raise ValueError("native edge coface fins run in the same direction")
                    low = u_min + step * index
                    high = u_min + step * (index + 1)
                    first_reversed = (
                        first_fin["vertex_ids"][0] != segment[0]
                    )
                    second_reversed = second_fin["vertex_ids"][0] != segment[0]
                    first_start_u = high if first_reversed else low
                    first_end_u = low if first_reversed else high
                    second_start_u = high if second_reversed else low
                    second_end_u = low if second_reversed else high
                    first_fin.update(
                        {
                            "edge_reference_sha256": edge["edge_reference_sha256"],
                            "classification": "native_edge",
                            "start_u": first_start_u,
                            "end_u": first_end_u,
                            "start_evidence": _u_evidence(
                                first_start_u,
                                body["points_by_vertex_id"][first_fin["vertex_ids"][0]],
                            ),
                            "end_evidence": _u_evidence(
                                first_end_u,
                                body["points_by_vertex_id"][first_fin["vertex_ids"][1]],
                            ),
                            "cofin_raw_id": second_fin["fin_id"],
                            "cofin_id": second_fin["fin_id"],
                        }
                    )
                    second_fin.update(
                        {
                            "edge_reference_sha256": edge["edge_reference_sha256"],
                            "classification": "native_edge",
                            "start_u": second_start_u,
                            "end_u": second_end_u,
                            "start_evidence": _u_evidence(
                                second_start_u,
                                body["points_by_vertex_id"][second_fin["vertex_ids"][0]],
                            ),
                            "end_evidence": _u_evidence(
                                second_end_u,
                                body["points_by_vertex_id"][second_fin["vertex_ids"][1]],
                            ),
                            "cofin_raw_id": first_fin["fin_id"],
                            "cofin_id": first_fin["fin_id"],
                        }
                    )
                    body["assigned_fin_ids"].update(
                        {first_fin["fin_id"], second_fin["fin_id"]}
                    )
                    edge_fin_ids.extend((first_fin["fin_id"], second_fin["fin_id"]))

        senses = {
            face_index: path_senses.get(face_index, index % 2 == 1)
            for index, face_index in enumerate(face_indices)
        }
        curve_sense = edge.get("curve_sense", True)
        curve_closed = edge.get("curve_closed", False)
        curve_periodic = edge.get("curve_periodic", False)
        if type(curve_sense) is not bool or type(curve_closed) is not bool or type(
            curve_periodic
        ) is not bool:
            raise ValueError("native edge curve flags must be booleans")
        curve_start_point = _binary32_point(edge["start_point_m"])
        curve_end_point = _binary32_point(edge["end_point_m"])
        curve_domain_transform = native_curve_domain_transform(
            u_min,
            u_max,
            u_min,
            u_max,
            curve_sense,
            curve_periodic,
        )
        if curve_domain_transform["rule_version"] != NATIVE_CURVE_DOMAIN_RULE:
            raise ValueError("native curve domain rule differs")
        topology_endpoints = native_curve_parameter_endpoints(
            curve_start_point,
            curve_end_point,
        )
        topology_assignment = native_curve_topology_endpoint_set_assignment(
            topology_endpoints[0],
            topology_endpoints[1],
            edge["start_point_m"],
            edge["end_point_m"],
            shared_vertex=(edge.get("start_vertex_reference_sha256") is not None
                           and edge.get("start_vertex_reference_sha256") == edge.get("end_vertex_reference_sha256")),
        )
        edge_rows.append(
            {
                "body_type": body_key[0],
                "body_index": body_key[1],
                "edge_reference_sha256": edge["edge_reference_sha256"],
                "native_edge_reference_sha256": edge[
                    "native_edge_reference_sha256"
                ],
                "edge_identity_mode": edge["edge_identity_mode"],
                "edge_identity": edge["edge_identity"],
                "mapping_source_edge_index": edge.get(
                    "mapping_source_edge_index"
                ),
                "mapping_source_edge_persistent_reference_sha256":
                    edge.get(
                        "mapping_source_edge_persistent_reference_sha256"
                    ),
                "start_vertex_reference_sha256": edge["start_vertex_reference_sha256"],
                "end_vertex_reference_sha256": edge["end_vertex_reference_sha256"],
                "start_point_m": [float(value) for value in edge["start_point_m"]],
                "end_point_m": [float(value) for value in edge["end_point_m"]],
                "start_vertex_error": None,
                "end_vertex_error": None,
                "start_point_error": None,
                "end_point_error": None,
                "start_vertex_state": "present",
                "end_vertex_state": "present",
                "start_reference_error": None,
                "end_reference_error": None,
                "face_indices": face_indices,
                "fin_ids": sorted(edge_fin_ids),
                "fin_ids_source": NATIVE_ID_ARRAY_SOURCE_FLAT,
                "raw_fin_ids": sorted(edge_fin_ids),
                "warnings": [],
                "u_min": u_min,
                "u_max": u_max,
                "curve_start_point_m": list(curve_start_point),
                "curve_end_point_m": list(curve_end_point),
                "curve_sense": curve_sense,
                "curve_type": edge.get("curve_type", 0),
                "curve_closed": curve_closed,
                "curve_periodic": curve_periodic,
                "curve_end_params": _curve_end_params(
                    u_min,
                    u_max,
                    is_closed=curve_closed,
                    is_periodic=curve_periodic,
                ),
                "curve_domain_transform": curve_domain_transform,
                "curve_endpoint_evaluations": {
                    "start": _curve_endpoint_evaluation(
                        u_min,
                        curve_start_point,
                        curve_end_point,
                    ),
                    "end": _curve_endpoint_evaluation(
                        u_max,
                        curve_end_point,
                        curve_start_point,
                    ),
                },
                "topology_endpoint_assignment": topology_assignment,
                "closed_fin_cycles": [],
                "endpoint_points_derived_from_curve": False,
                "face_senses": [
                    {
                        "face_index": face_index,
                        "EdgeInFaceSense": {
                            "value": senses[face_index],
                            "error": None,
                        },
                        "IEdgeInFaceSense2": {
                            "value": senses[face_index],
                            "error": None,
                        },
                    }
                    for face_index in face_indices
                ],
            }
        )

    for body_type, body_index in body_keys:
        body_key = (body_type, body_index)
        body = evidence_by_body[body_key]
        body_rows.append(
            {
                "body_type": body_type,
                "body_index": body_index,
                "body_reference_sha256": body["spans"][0]["body_reference_sha256"],
                "tessellate_result": True,
                "request": native_tessellation_request(),
                "property_requests": copy.deepcopy(
                    NATIVE_TESSELLATION_PROPERTIES
                ),
                "properties": copy.deepcopy(NATIVE_TESSELLATION_PROPERTIES),
                "facet_count": len(body["facets"]),
                "fin_count": len(body["fins"]),
                "vertex_count": len(body["vertex_ids_by_point"]),
                "faces": body["faces"],
                "facets": body["facets"],
                "fins": body["fins"],
            }
        )

    vertex_rows.sort(
        key=lambda row: (row["body_type"], row["body_index"], row["vertex_id"])
    )
    native_fin_count = sum(
        fin["classification"] == "native_edge"
        for body in body_rows
        for fin in body["fins"]
    )
    u_observations = native_fin_count * 2
    solidworks_vertex_identities = []
    vertex_identity_by_key = {}
    for edge in input_edges:
        body_key = (edge["body_type"], edge["body_index"])
        for endpoint in ("start", "end"):
            reference = edge[f"{endpoint}_vertex_reference_sha256"]
            point = edge[f"{endpoint}_point_m"]
            if reference is None or point is None:
                continue
            key = (
                *body_key,
                reference,
                *(
                    struct.pack("<d", float(value)).hex()
                    for value in point
                ),
            )
            previous = vertex_identity_by_key.get(key)
            if previous is not None:
                continue
            vertex_identity_by_key[key] = point
    next_vertex_index = {}
    for body_key, reference, _point_bits, point in sorted(
        (
            (key[:2], key[2], key[3:], value)
            for key, value in vertex_identity_by_key.items()
        ),
        key=lambda item: (item[0], item[1], item[2]),
    ):
        vertex_index = next_vertex_index.get(body_key, 0)
        next_vertex_index[body_key] = vertex_index + 1
        solidworks_vertex_identities.append(
            {
                "body_type": body_key[0],
                "body_index": body_key[1],
                "vertex_index": vertex_index,
                "point_m": list(point),
                "identity_mode": NATIVE_PERSISTENT_IDENTITY_RULE,
                "native_reference_sha256": reference,
                "identity": None,
                "identity_sha256": reference,
            }
        )
    expected_triangles = sum(span["triangle_count"] for span in spans)
    if len(fixture_triangles) != expected_triangles:
        raise ValueError("native tessellation triangle projection is incomplete")
    stl_payload = bytearray(
        b"native-tessellation-v3 fixture".ljust(80, b" ")
        + struct.pack("<I", len(fixture_triangles))
    )
    cursor = 0
    for face_index, span in enumerate(spans):
        owner_id = span.get("face_owner_id")
        if owner_id is None:
            owner_id = face_index + 1
            span["face_owner_id"] = owner_id
        record_start = len(stl_payload)
        for triangle in fixture_triangles[
            cursor : cursor + span["triangle_count"]
        ]:
            stl_payload.extend(
                struct.pack(
                    "<12fH",
                    0.0,
                    0.0,
                    0.0,
                    *(coordinate for point in triangle for coordinate in point),
                    owner_id,
                )
            )
        span["triangle_records_sha256"] = hashlib.sha256(
            bytes(stl_payload[record_start:])
        ).hexdigest()
        cursor += span["triangle_count"]
    stl_bytes = bytes(stl_payload)
    envelope = {
        "schema_version": NATIVE_TESSELLATION_SCHEMA,
        "faces": len(spans),
        "face_spans": spans,
        "native_edges": edge_rows,
        "no_facets_faces": normalized_no_facets,
        "planar_brep": {
            "schema_version": NATIVE_PLANAR_BREP_SCHEMA,
            "bodies": [],
        },
        "itessellation": {
            "interface_iid": ITESSELLATION_IID,
            "u_rule_version": NATIVE_U_IDENTITY_RULE,
            "u_resolution_rule": NATIVE_U_RESOLUTION_RULE,
            "identity_rule": NATIVE_IDENTITY_POLICY_RULE,
            "point_roundtrip_rule": NATIVE_POINT_ROUNDTRIP_RULE,
            "u_observation_stats": {
                "unique_observations": u_observations,
                "cache_hits": 0,
                "get_closest_point_on_calls": u_observations,
                "evaluate2_calls": u_observations,
            },
            "property_requests": copy.deepcopy(
                NATIVE_TESSELLATION_PROPERTIES
            ),
            "properties": copy.deepcopy(NATIVE_TESSELLATION_PROPERTIES),
            "reference_alias_audit": {
                "body": [],
                "face": [],
                "edge": [],
                "vertex": [],
            },
            "cross_kind_raw_aliases": [],
            "bodies": body_rows,
            "vertices": vertex_rows,
            "solidworks_vertex_identities": solidworks_vertex_identities,
        },
    }
    return NativeTessellationV3Fixture(
        envelope,
        stl_bytes=stl_bytes,
        triangle_vertices=fixture_triangles,
    )


def refresh_topology_endpoint_assignment(tessellation: dict[str, Any]) -> None:
    """Recompute the topology audit after a test mutates curve or endpoint evidence."""

    for edge in tessellation["native_edges"]:
        if edge["start_point_m"] is None or edge["end_point_m"] is None:
            edge["topology_endpoint_assignment"] = None
            continue
        topology_endpoints = native_curve_parameter_endpoints(
            edge["curve_start_point_m"],
            edge["curve_end_point_m"],
        )
        edge["topology_endpoint_assignment"] = (
            native_curve_topology_endpoint_set_assignment(
                topology_endpoints[0],
                topology_endpoints[1],
                edge["start_point_m"],
                edge["end_point_m"],
                shared_vertex=(edge["start_vertex_reference_sha256"] is not None
                               and edge["start_vertex_reference_sha256"] == edge["end_vertex_reference_sha256"]),
            )
        )


def _matrix16(values: Sequence[float]) -> Matrix:
    """Coerce a sequence to the 4x4 row-major matrix shape the protocol declares."""

    numbers = [float(value) for value in values]
    if len(numbers) != 16:
        raise ValueError(f"expected 16 matrix values, got {len(numbers)}")
    return cast(Matrix, tuple(numbers))


def _identity() -> list[float]:
    return [
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def placement(xyz: Sequence[float] = (0.0, 0.0, 0.0), rpy: Sequence[float] = (0.0, 0.0, 0.0)) -> list[float]:
    """Row-major 4x4 for a URDF-style xyz/rpy placement."""

    from description_pipeline.sources.solidworks.scene import _rotation_from_rpy

    rotation = _rotation_from_rpy((float(rpy[0]), float(rpy[1]), float(rpy[2])))
    return [
        rotation[0][0],
        rotation[0][1],
        rotation[0][2],
        float(xyz[0]),
        rotation[1][0],
        rotation[1][1],
        rotation[1][2],
        float(xyz[1]),
        rotation[2][0],
        rotation[2][1],
        rotation[2][2],
        float(xyz[2]),
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def mass_payload(
    mass: float,
    com: Sequence[float] = (0.0, 0.0, 0.0),
    inertia: Sequence[Sequence[float]] | None = None,
) -> dict[str, Any]:
    tensor = [list(row) for row in (inertia or [[0.001, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]])]
    return {
        "mass": float(mass),
        "com": [float(value) for value in com],
        "inertia": tensor,
        "reference": {"used_api": "fixture", "product_convention": "solidworks_standard"},
    }


def compose_fixture_link_reads(
    components: Sequence[dict[str, Any]],
    effective: dict[str, dict[str, Any]],
    selections: dict[str, Sequence[str]],
    masses: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fixture 'CAD' direct reads: the assembly-frame recombination of a selection.

    Used by the fixture backend (so capture and scene agree exactly) and by
    scene tests that need a valid link group record; divergence cases inject
    their own read values instead.  The math is convention-free on purpose:
    the native reader returns raw assembly numbers, and product-convention
    refusals belong to the part rows, not to this read.
    """

    from description_pipeline.sources.solidworks.scene import combine_mass_properties  # noqa: PLC0415

    def placed(transform: Sequence[float], com: Sequence[float]) -> list[float]:
        rows = [list(transform[index : index + 3]) for index in (0, 4, 8)]
        translation = [transform[3], transform[7], transform[11]]
        return [
            sum(rows[row][column] * float(com[column]) for column in range(3)) + translation[row] for row in range(3)
        ]

    def rotated(transform: Sequence[float], inertia: Sequence[Sequence[float]]) -> list[list[float]]:
        rows = [list(transform[index : index + 3]) for index in (0, 4, 8)]
        rotated_rows = [
            [sum(rows[row][k] * float(inertia[k][column]) for k in range(3)) for column in range(3)] for row in range(3)
        ]
        return [
            [sum(rotated_rows[row][k] * rows[column][k] for k in range(3)) for column in range(3)] for row in range(3)
        ]

    def entry_for(name: str, payload: dict[str, Any], transform: Sequence[float]) -> dict[str, Any]:
        scope = (payload.get("reference") or {}).get("scope") or "part_document"
        com = [float(value) for value in payload["com"]]
        inertia = [[float(value) for value in row] for row in payload["inertia"]]
        if scope == "assembly_component_group":
            return {"mass": float(payload["mass"]), "com": com, "inertia": inertia}
        return {
            "mass": float(payload["mass"]),
            "com": placed(transform, com),
            "inertia": rotated(transform, inertia),
        }

    by_name: dict[str, tuple[Any, dict[str, Any]]] = {}
    for entry in components:
        if isinstance(entry, dict):
            by_name[str(entry["name"])] = (entry.get("transform") or _identity(), entry["mass"])
        else:
            mass = getattr(entry, "mass", None)
            if mass is None and masses is not None:
                mass = masses[str(entry.name)]
            by_name[str(entry.name)] = (getattr(entry, "transform", None) or _identity(), mass)
    links: dict[str, dict[str, Any]] = {}
    for body, names in selections.items():
        entries = []
        claimed: set[str] = set()
        for name in names:
            parts = str(name).split("/")
            ancestors = ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]
            provider = next((ancestor for ancestor in ancestors if ancestor in effective), None)
            if provider is not None:
                if provider in claimed:
                    continue
                claimed.add(provider)
                payload = dict(effective[provider], mass_source="cad")
                transform = by_name.get(provider, (_identity(), None))[0]
                entries.append(entry_for(provider, payload, transform))
            else:
                transform, mass = by_name[str(name)]
                entries.append(entry_for(str(name), mass, _matrix16(transform)))
        combined = combine_mass_properties(entries)
        mass = float(combined["mass"])
        com = [float(value) for value in combined["com"]]
        tensor = [[float(value) for value in row] for row in combined["inertia"]]
        # The second recording mirrors GetMomentOfInertia(1): the same tensor
        # about the assembly origin, related to the about-COM tensor by the
        # parallel-axis identity in the standard product convention.  The
        # capture-side convention proof reads exactly this pair.
        norm = sum(value * value for value in com)
        origin = [
            [
                tensor[row][column]
                + mass * ((norm - com[row] * com[column]) if row == column else -com[row] * com[column])
                for column in range(3)
            ]
            for row in range(3)
        ]
        read = {
            "mass_kg": mass,
            "volume_m3": None,
            "density_kg_m3": None,
            "com_m": com,
            "inertia_tensor_about_com": tensor,
            "inertia_values_about_origin_api": [origin[row][column] for row in range(3) for column in range(3)],
            "overrides": {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            },
        }
        links[str(body)] = {
            "selection": [str(name) for name in names],
            "reads": [read, copy.deepcopy(read)],
            "reproducibility": {"mass_delta_kg": 0.0, "com_delta_norm_m": 0.0, "tensor_delta_max_abs": 0.0},
        }
    return links


class FixtureCadBackend(CadBackend):
    """A CAD host with deterministic content and no SolidWorks."""

    name = "fixture"

    def __init__(
        self,
        document: Path,
        components: Sequence[dict[str, Any]],
        *,
        saved: bool = True,
        configurations: Sequence[str] = ("Default",),
        dependencies: Sequence[Path] | None = None,
        coordinate_systems: dict[str, Sequence[float]] | None = None,
    ) -> None:
        self.document = Path(document)
        self.components = [dict(entry) for entry in components]
        self.saved = saved
        self.configurations = list(configurations)
        self.open_documents: list[str] = []
        self.collected: list[str] = []
        self.exported: list[str] = []
        # Explicit neutral native datums for the shared two-body analytic fixture.
        datums = (
            {
                "base_datum": placement(),
                "arm_datum": placement((0.0, 0.0, 0.2)),
                "imu_datum": placement((0.01, 0.0, 0.03)),
            }
            if coordinate_systems is None
            else coordinate_systems
        )
        self.coordinate_system_matrices: dict[str, Sequence[float]] = {
            name: list(matrix) for name, matrix in datums.items()
        }
        self._dependencies = [Path(path) for path in (dependencies or [])]
        # failure-injection knobs for the closure contract
        self.pack_drop_files: set[str] = set()
        self.copy_unresolved = 0
        self.copy_extra_components = 0
        self.resolve_returns_empty = False
        self.copy_configuration: str | None = None
        self.copy_referenced_configuration_change = False
        self.copy_missing_files = False
        self.copy_outside_documents = False
        self.copy_duplicate_instances = False
        self.copy_rewrites_bytes = False
        self.resolve_only_existing = False
        self.collected_scene_calls: list[str] = []
        self.scene_document_override: str | None = None
        self.originals_change_during_capture = False
        self.state_calls: list[str] = []
        self.unsaved_paths: set[str] = set()
        self.drift_configuration_after_reading = False
        self._drifted = False
        self._original_paths = {str(self.document), *(str(path) for path in self._dependencies)}
        # The fixture models the two owned processes used by the real freeze:
        # collection/rewrite and the post-boundary capture session.
        self.session_pid = 100
        self.capture_session_pid: int | None = None
        self.retired_session_pids: list[int] = []

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "backend": self.name,
            "sw_version": "fixture-2026",
            "active_document": str(self.document),
        }

    def list_documents(self) -> list[str]:
        # a real session has the assembly *and* its references open
        return [str(self.document), *(str(path) for path in self._dependencies)]

    def open_document(self, path: str) -> dict[str, Any]:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("open_document")
        self.capture_session_pid = self.session_pid
        self.open_documents.append(str(path))
        return {
            "opened": Path(path).name,
            "path": str(path),
            "read_only": True,
            "errors": 0,
            "warnings": 0,
        }

    def close_document(self, name: str, confirm: bool = False) -> dict[str, Any]:
        return {"closed": name}

    def document_state(self, path: str) -> dict[str, Any]:
        self.state_calls.append(str(path))
        active = self.configurations[0]
        if self._drifted and str(path) in self._original_paths:
            # an edit made in the session during the capture: the file on disk is
            # untouched, only the in-memory state moved
            active = "Other"
        saved = bool(self.saved and str(path) not in self.unsaved_paths)
        return {
            "path": str(path),
            "title": Path(path).name,
            "save_flag": not saved,
            "saved": saved,
            "active_configuration": active,
            "configurations": list(self.configurations),
            "read_only": True,
            "lightweight": None,
            "unsupported": ["lightweight"],
        }

    def save_flag_documents(self) -> list[str]:
        """The working-tree documents the fake reports as needing a save."""

        if not self.saved:
            return sorted(
                {
                    str(self.document),
                    *(str(path) for path in self._dependencies),
                    *(str(path) for path in self.unsaved_paths),
                }
            )
        return sorted(str(path) for path in self.unsaved_paths)

    def list_dependencies(self, path: str) -> list[dict[str, Any]]:
        return [
            {"path": str(item), "exists": Path(item).is_file(), "type": Path(item).suffix.lower()}
            for item in self._dependencies
        ]

    def collect_dependencies(
        self,
        path: str,
        destination_dir: str,
        *,
        skip_missing=(),
        excluded_paths=(),
        required_names=(),
        admitted_roots=(),
        closure_documents=None,
        required_paths=(),
    ) -> dict[str, Any]:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("collect_dependencies")
        self.collect_skips = {
            "skip_missing": sorted(str(item) for item in skip_missing),
            "excluded_paths": sorted(str(item) for item in excluded_paths),
            "required_names": sorted(str(item) for item in required_names),
            "admitted_roots": [str(item) for item in admitted_roots],
            "closure_documents": (
                sorted((closure_documents or {}).keys()) if isinstance(closure_documents, dict) else None
            ),
            "required_paths": sorted(str(item) for item in required_paths),
        }
        target = Path(destination_dir)
        target.mkdir(parents=True, exist_ok=True)
        self.collected = []
        excluded = {os.path.normcase(os.path.abspath(str(item))) for item in excluded_paths}
        for item in [self.document, *self._dependencies]:
            source = Path(item)
            if os.path.normcase(os.path.abspath(source)) in excluded:
                continue
            if not source.is_file() or source.name in self.pack_drop_files:
                continue
            destination = target / source.name
            payload = source.read_bytes()
            if self.copy_rewrites_bytes and source.resolve() == self.document.resolve():
                # an assembly carries its references, so Native reference collection legitimately
                # rewrites it: the copy is not byte-identical to the source
                payload = payload + b" (rewritten references)"
            destination.write_bytes(payload)
            self.collected.append(str(destination))
        top_level = target / Path(self.document).name
        if self.copy_missing_files:
            # the collector reported the document but its bytes never landed
            for path in list(self.collected):
                if Path(path) != top_level:
                    Path(path).unlink(missing_ok=True)
        return {
            "method": "fixture_reference_copy",
            "destination": str(target),
            "source_document": str(self.document),
            "top_level": str(top_level),
            "configured": ["SetSaveToName(True, destination)"],
            "statuses": {
                "count": len(self.collected),
                "values": [0] * len(self.collected),
                "failures": [],
                "ok": bool(self.collected),
            },
            "files": list(self.collected),
            "skipped_missing": self.collect_skips["skip_missing"],
            "skipped_excluded": self.collect_skips["excluded_paths"],
            "mapping": {
                str(item): str(target / Path(item).name)
                for item in [self.document, *self._dependencies]
                if str(target / Path(item).name) in self.collected
            },
        }

    def inspect_copy(self, assembly_path: str) -> dict[str, Any]:
        """Report the component instances a re-opened document resolves (fixture).

        Every failure knob describes the *copy* only: the original is the baseline
        the copy is compared against, so injecting there would just make the
        source look broken.
        """

        target = Path(assembly_path)
        base = target.parent
        is_copy = target.resolve() != Path(self.document).resolve()
        extra = self.copy_extra_components if is_copy else 0
        unresolved_count = self.copy_unresolved if is_copy else 0
        entries = [dict(entry) for entry in self.components]
        entries.extend({"name": f"extra-{index}", "path": str(self.document)} for index in range(extra))
        instances = []
        unresolved = []
        suppressed = []
        for position, entry in enumerate(entries):
            name = str(entry["name"])
            parent = str(entry.get("parent") or "")
            # Name2 carries the assembly instance path, not the document path
            instance = f"{parent}/{name}" if parent else str(entry.get("instance") or name)
            if is_copy and self.copy_duplicate_instances and position == 1 and len(entries) > 1:
                instance = str(entries[0].get("instance") or entries[0]["name"])
            is_suppressed = bool(entry.get("suppressed"))
            document: Path | None = base / Path(entry.get("path") or self.document).name
            if position >= len(entries) - unresolved_count:
                document = None
            elif is_copy and self.copy_outside_documents:
                # the realistic capture defect: the copy still resolves the
                # same-named document from the working tree it was collected from
                document = Path(self.document).parent / Path(entry.get("path") or self.document).name
            configuration = str(entry.get("configuration") or "Default")
            if is_copy and self.copy_referenced_configuration_change:
                configuration = "Other"
            if is_suppressed:
                # A suppressed instance loads no model at all, which is not an
                # escape as long as the copy is suppressed in the same places.
                document = None
            # The reported document is what the host resolved, whether or not the
            # bytes are on disk; the closure check is what proves the file exists.
            if document is None:
                if is_suppressed:
                    suppressed.append(instance)
                else:
                    unresolved.append(instance)
            instances.append(
                {
                    "instance": instance,
                    "name": name,
                    "document": str(document) if document is not None else None,
                    "document_name": document.name.lower() if document is not None else None,
                    "configuration": configuration,
                    "defining": str(Path(assembly_path)),
                    "defining_anchor": None,
                    "virtual": False,
                    "imported_path": None,
                    "imported_error": None,
                    "suppressed": is_suppressed,
                    "depth": 0,
                }
            )
        active = self.configurations[0]
        if is_copy and self.copy_configuration:
            active = self.copy_configuration
        return {
            "document": str(target),
            "configuration": active,
            "components": len(instances),
            "unresolved": unresolved,
            "suppressed": suppressed,
            "instances": instances,
        }

    def resolve_dependencies(self, path: str) -> list[dict[str, Any]]:
        if self.resolve_returns_empty:
            return []
        target = Path(path).parent
        entries = [
            {
                "path": str(target / Path(item).name),
                "exists": (target / Path(item).name).is_file(),
                "type": Path(item).suffix.lower(),
            }
            for item in [self.document, *self._dependencies]
        ]
        if self.resolve_only_existing:
            # a build that lists only what it actually resolved, so a missing
            # instance document has to be caught by the re-opened copy
            entries = [entry for entry in entries if entry["exists"]]
        return entries

    def collect_scene(
        self,
        doc_path: str,
        coordinate_systems: Sequence[str],
        on_activity=None,
        require_material: bool = True,
    ) -> RawScene:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("collect_scene")
        self.collected_scene_calls.append(str(doc_path))
        if self.originals_change_during_capture:
            Path(self.document).write_bytes(b"fixture assembly edited during capture")
        if self.drift_configuration_after_reading:
            self._drifted = True
        scene = RawScene(document=str(self.scene_document_override or doc_path))
        for entry in self.components:
            scene.components.append(
                RawComponent(
                    name=str(entry["name"]),
                    path=str(entry.get("path") or self.document),
                    transform=_matrix16(entry.get("transform") or _identity()),
                    is_fixed=bool(entry.get("is_fixed", False)),
                    document_type="part",
                )
            )
        scene.coordinate_systems = {name: _matrix16(matrix) for name, matrix in self.coordinate_system_matrices.items()}
        scene.mass_properties = {str(entry["name"]): dict(entry["mass"]) for entry in self.components}
        scene.notes = {"backend": self.name}
        if on_activity is not None:
            total = len(self.components)
            for index, entry in enumerate(self.components, start=1):
                on_activity(
                    {
                        "phase": "capture",
                        "action": "instance_reads",
                        "current_object": str(entry["name"]),
                        "completed": index,
                        "total": total,
                    }
                )
        return scene

    def export_component_meshes(self, destinations: dict[str, str], on_activity=None) -> dict[str, dict[str, Any]]:
        entries = {}
        total = len(destinations)
        for index, (component, dest_path) in enumerate(destinations.items(), start=1):
            if on_activity is not None:
                on_activity(
                    {
                        "phase": "capture",
                        "action": "mesh_export",
                        "current_object": component,
                        "completed": index,
                        "total": total,
                    }
                )
            face_edges, native_edges = cube_native_edges()
            mesh_triangles, _attributes = cube_triangles()
            spans = []
            start = 0
            for face_index in range(6):
                count = 2
                spans.append(
                    {
                        "body_type": 0,
                        "body_index": 0,
                        "body_name": "fixture-solid",
                        "body_reference_sha256": hashlib.sha256(b"fixture-body").hexdigest(),
                        "face_index": face_index,
                        "face_reference_sha256": hashlib.sha256(
                            f"fixture-face-{face_index}".encode()
                        ).hexdigest(),
                        "face_owner_id": face_index + 1,
                        "edge_references": face_edges[face_index],
                        "triangle_start": start,
                        "triangle_count": count,
                        "triangle_records_sha256": "0" * 64,
                    }
                )
                start += count
            tessellation = native_tessellation_fixture(
                spans,
                native_edges,
                triangle_vertices=mesh_triangles,
            )
            Path(dest_path).write_bytes(tessellation.stl_bytes)
            self.exported.append(str(dest_path))
            entries[component] = {
                "component": component,
                "written": dest_path,
                "used_api": "fixture",
                "triangles": len(mesh_triangles),
                "bodies": {"solid": 1, "sheet": 0},
                "tessellation": tessellation,
            }
        return entries

    def link_group_mass_properties(self, path, selections):
        """Direct link reads for the fixture: the assembly-frame recombination."""

        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("link_group_read")
        links = compose_fixture_link_reads(self.components, getattr(self, "effective_readings", None) or {}, selections)
        override = getattr(self, "link_group_selection_override", None) or {}
        for body, names in override.items():
            if str(body) in links:
                links[str(body)]["selection"] = [str(name) for name in names]
        if getattr(self, "link_group_payload_nonfinite", False):
            links["base_link"]["reads"][0]["inertia_values_about_origin_api"] = {
                "error": "fixture",
                "values": [float("nan")] * 9,
            }
        return {
            "assembly": str(self.scene_document_override or path),
            "configuration": str(getattr(self, "configuration_name", "Default")),
            "used_api": "IMassProperty2 (SelectedItems = component set)",
            "selection_rule": "top_most_providers+uncovered_leaves",
            "links": links,
        }

    def close_all_documents(self):
        """Fixture close-all for the pristine readout window."""

        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("close_all_documents")
        if getattr(self, "close_refuses", False):
            raise CadError("cad_close_failed", "fixture refuses to close documents")
        self.open_documents.clear()
        return {"closed": True}

    def retire_session(self, role: str = "copy") -> dict[str, Any]:
        """Model the rewrite-to-capture owned-process boundary."""

        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("retire_session")
        if role != "copy":
            raise AssertionError(f"unexpected retired role: {role}")
        before = {
            "pid": self.session_pid,
            "state": "active",
            "role": role,
        }
        self.retired_session_pids.append(self.session_pid)
        self.open_documents.clear()
        self.session_pid += 1
        return {
            "role": role,
            "retired": True,
            "before": before,
            "after": {
                "pid": self.session_pid,
                "state": "active",
                "role": role,
            },
        }

    def verify_sources_unchanged(self) -> dict[str, Any]:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("verify_sources")
        files = [self.document, *self._dependencies]
        return {str(item): {"sha256": file_digest(Path(item))} for item in files if Path(item).is_file()}

    def environment(self) -> dict[str, Any]:
        order = getattr(self, "order", None)
        if order is None:
            order = self.order = []
        order.append("environment")
        return {
            "revision": "fixture-2026",
            "input_mode": "saved_documents",
            "build": "fixture-2026",
            "license": 0,
            "visible": True,
            "sessions": {
                "copy": {
                    "pid": self.session_pid,
                    "state": "active",
                    "role": "copy",
                }
            },
        }


def make_cad_tree(root: Path) -> Path:
    """Create a tiny on-disk 'CAD' tree: one assembly plus two parts."""

    root.mkdir(parents=True, exist_ok=True)
    assembly = root / "robot.SLDASM"
    assembly.write_bytes(b"fixture assembly")
    for index, name in enumerate(("base.SLDPRT", "arm.SLDPRT"), start=1):
        (root / name).write_bytes(f"fixture part {index}".encode())
    return assembly


def cleanup(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def write_link_group_mass_record(bundle: Path, model: dict, source: dict) -> None:
    """Synthesize the direct link group record the producer will emit (fixture stand-in).

    The record is derived from the bundle's sealed raw scene plus the model the
    fixtures just built, so the verifier's direct-readout binding can run before
    the native producer method lands.
    """

    import json as _json

    import numpy as _np

    from description_pipeline.sources.solidworks import verify as oracle

    bundle = Path(bundle)
    evidence = bundle / "evidence"
    raw = _json.loads((evidence / "raw/scene_raw.json").read_text())
    masses = _json.loads((evidence / "raw/mass_properties.json").read_text())
    datums = _json.loads((evidence / "raw/coordinate_systems.json").read_text())
    effective = oracle.effective_math_map(masses, raw)
    links = {str(link["name"]): link for link in model.get("links", [])}
    payload = {
        "schema_version": "solidworks-to-urdf.link-group-mass-properties/v1",
        "assembly": raw.get("document") or raw.get("source_document"),
        "configuration": str(source.get("configuration") or "Default"),
        "used_api": "IMassProperty2 (SelectedItems = component set)",
        "selection_rule": "top_most_providers+uncovered_leaves",
        "links": {},
    }
    for body in source.get("bodies") or []:
        name = str(body.get("name"))
        selection = sorted(
            str(note["component"])
            for *_rest, note in oracle._resolved_contributions(body, raw, masses, effective)
        )
        mass, com_link, tensor_link = oracle._canonical_in_link_frame(links[name])
        datum = _np.array(datums[body["frame"]["coordinate_system"]], dtype=float).reshape(4, 4)
        com_assembly = datum[:3, :3] @ com_link + datum[:3, 3]
        tensor_assembly = datum[:3, :3] @ tensor_link @ datum[:3, :3].T
        read = {
            "mass_kg": float(mass),
            "volume_m3": 0.001,
            "density_kg_m3": 1000.0,
            "com_m": com_assembly.tolist(),
            "inertia_tensor_about_com": tensor_assembly.tolist(),
            "inertia_values_about_origin_api": {"error": "fixture"},
            "overrides": {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            },
        }
        payload["links"][name] = {
            "selection": selection,
            "reads": [_json.loads(_json.dumps(read)), _json.loads(_json.dumps(read))],
            "reproducibility": {
                "mass_delta_kg": 0.0,
                "com_delta_norm_m": 0.0,
                "tensor_delta_max_abs": 0.0,
            },
        }
    (evidence / "raw/link_group_mass_properties.json").write_text(
        _json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def write_link_group_mass_record_from_recombination(bundle: Path, source: dict) -> None:
    """Seed the direct link record from the recombination (fixtures without a model yet).

    Used before ``load_scene``/manifest verification so a fixture bundle is
    shaped exactly like a native12 capture; the scene that consumes it later
    regenerates the same link inertials by construction.
    """

    import json as _json

    from description_pipeline.sources.solidworks import verify as oracle

    bundle = Path(bundle)
    evidence = bundle / "evidence"
    raw = _json.loads((evidence / "raw/scene_raw.json").read_text())
    masses = _json.loads((evidence / "raw/mass_properties.json").read_text())
    effective = oracle.effective_math_map(masses, raw)
    payload = {
        "schema_version": "solidworks-to-urdf.link-group-mass-properties/v1",
        "assembly": raw.get("document") or raw.get("source_document"),
        "configuration": str(source.get("configuration") or "Default"),
        "used_api": "IMassProperty2 (SelectedItems = component set)",
        "selection_rule": "top_most_providers+uncovered_leaves",
        "links": {},
    }
    for body in source.get("bodies") or []:
        name = str(body.get("name"))
        selection = sorted(
            str(note["component"])
            for *_rest, note in oracle._resolved_contributions(body, raw, masses, effective)
        )
        world = oracle._raw_world(body, raw, masses)
        read = {
            "mass_kg": float(world["mass"]),
            "volume_m3": None,
            "density_kg_m3": None,
            "com_m": [float(value) for value in world["com"]],
            "inertia_tensor_about_com": [[float(value) for value in row] for row in world["inertia"]],
            "inertia_values_about_origin_api": {"error": "fixture"},
            "overrides": {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            },
        }
        payload["links"][name] = {
            "selection": selection,
            "reads": [_json.loads(_json.dumps(read)), _json.loads(_json.dumps(read))],
            "reproducibility": {
                "mass_delta_kg": 0.0,
                "com_delta_norm_m": 0.0,
                "tensor_delta_max_abs": 0.0,
            },
        }
    (evidence / "raw/link_group_mass_properties.json").write_text(
        _json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def read_scene(snapshot: Path) -> dict:
    """Read the raw scene a snapshot carries (shared loader when available)."""

    from description_pipeline.sources.snapshot import load_scene

    return load_scene(Path(snapshot))


# ---------------------------------------------------------------------------
# Target-tree fixtures (IF-13): delivery roots with capture blocks and a bound
# manifest.  Shared by the migrated verification tests; block writes re-seal the
# manifest so semantic checks - not byte rebinding - are what the tests exercise.
# ---------------------------------------------------------------------------

CONTRACT_SCHEMA_VERSION = "solidworks-to-urdf.stage-contract/v1"


def saved_document_state(configuration: str = "Default") -> dict:
    """The native before/after document readings of one saved, active configuration."""

    observed = {
        "save_flag": False,
        "configuration": configuration,
        "needs_rebuild2_value": 0,
        "configuration_needs_rebuild": False,
        "read_only": True,
    }
    return {
        "before": copy.deepcopy(observed),
        "after": copy.deepcopy(observed),
        "used_api": (
            "IModelDoc2.GetSaveFlag/IModelDocExtension.NeedsRebuild2/"
            "IConfiguration.Name/IConfiguration.NeedsRebuild"
        ),
    }


def default_source_record(
    *,
    hardware_id: str = "m3.0",
    configuration: str = "Default",
    main_assembly: str = "M3.0.SLDASM",
    revision: str | None = "r1",
) -> dict:
    from description_pipeline.io import digest as canonical_digest
    from description_pipeline.schemas import SOURCE_RECORD_SCHEMA

    identity = {
        "kind": "source-revision",
        "handoff_digest": canonical_digest({"fixture": main_assembly}),
        "main_assembly": main_assembly,
        "configuration": configuration,
        "hardware_id": hardware_id,
        "records": [],
    }
    return {
        "schema_version": SOURCE_RECORD_SCHEMA,
        "source_revision_id": canonical_digest(identity),
        "identity": identity,
        "selection": {
            "main_assembly": main_assembly,
            "configuration": configuration,
            "hardware_id": hardware_id,
            "hardware_source": "cad",
        },
        "metadata": {
            "revision": revision,
            "owner": "fixture",
            "control": {"system": "handoff", "reference": "fixture"},
            "change_summary": "fixture",
            "change_summary_source": "submission",
        },
    }


def seal_delivery(root: Path) -> dict:
    from description_pipeline.delivery import write_delivery_manifest
    from description_pipeline.interface_records import check_policy_digest
    from description_pipeline.schemas import CHECK_POLICY_SCHEMA
    from description_pipeline.stages import CONTRACT_SHA256

    return write_delivery_manifest(
        Path(root),
        contract={"schema_version": CONTRACT_SCHEMA_VERSION, "digest": CONTRACT_SHA256},
        policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
    )


def write_target_delivery(
    root: Path,
    *,
    capture: dict,
    discovery: dict | None = None,
    source: dict | None = None,
    handoff: dict | None = None,
    runtime: dict | None = None,
    tool: dict | None = None,
    originals: dict[str, str] | None = None,
    records: dict[str, str] | None = None,
    request: dict | None = None,
) -> Path:
    """Create a minimal target-tree delivery holding the given capture blocks."""

    from description_pipeline.io import write_json
    from description_pipeline.schemas import TOOL_SCHEMA

    root = Path(root)
    for name in (
        "input",
        "input/cad",
        "input/records",
        "evidence",
        "evidence/raw",
        "model",
        "urdf",
        "meshes",
        "reports",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
    source = source or default_source_record()
    selection = source["selection"]
    if handoff is None:
        handoff = {
            "package_files": {},
            "input_receipt": {"inventory": {}},
            "main_assembly": selection["main_assembly"],
            "configuration": selection["configuration"],
        }
    write_json(root / "input/handoff.json", handoff)
    write_json(root / "input/source.json", source)
    if request is None:
        payload_handoff = (discovery or {}).get("handoff_sha256") if isinstance(discovery, dict) else None
        raw_selection = (
            ((discovery or {}).get("raw") or {}).get("source_selection")
            if isinstance(discovery, dict)
            else None
        )
        upload_handoff = raw_selection.get("upload_sha256") if isinstance(raw_selection, dict) else None
        request = {
            "schema_version": "solidworks-to-urdf.request/v2",
            "purpose": "engineering",
            "request_id": "00000000-0000-4000-8000-000000000000",
            "target_hardware": str(selection["hardware_id"]),
            "change_summary": str((source.get("metadata") or {}).get("change_summary") or "fixture"),
            "main_assembly": str(selection["main_assembly"]),
            "handoff_digest": (
                upload_handoff
                if isinstance(upload_handoff, str)
                else payload_handoff if isinstance(payload_handoff, str) else "0" * 64
            ),
            "platform_config_digest": "0" * 64,
        }
    write_json(root / "input/request.json", request)
    for name, text in (originals or {}).items():
        target = root / "input/cad" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for name, text in (records or {}).items():
        relative = name if name.replace("\\", "/").startswith("records/") else f"records/{name}"
        target = root / "input" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    write_json(root / "evidence/discovery.json", discovery or {})
    capture = dict(capture)
    if "material_provenance" not in capture:
        # The sealed capture block is the material authority the verifier reads
        # (the retired evidence/raw/source_config.json is no longer a carrier).
        capture["material_provenance"] = {
            "schema_version": "solidworks-to-urdf.material-provenance/v1",
            "assembly": str((root / "input/cad" / str(selection["main_assembly"])).resolve()),
            "configuration": str(selection["configuration"]),
            "measured_source": "cad_api",
            "warnings": [],
        }
    write_json(root / "evidence/capture.json", capture)
    write_json(root / "evidence/native-runtime.json", runtime or {})
    write_json(root / "model/robot.json", {})
    (root / "urdf/robot.urdf").write_text("<robot name='m3.0'/>\n", encoding="utf-8")
    (root / "meshes/part.stl").write_text("solid\n", encoding="utf-8")
    write_json(root / "tool.json", tool or {"schema_version": TOOL_SCHEMA})
    write_json(root / "reports/quality.json", {})
    seal_delivery(root)
    return root


def capture_file_for(root: Path) -> Path:
    root = Path(root)
    return (root.parent if root.name == "evidence" else root) / "evidence/capture.json"


def load_capture_block(root: Path, name: str):
    from description_pipeline.io import read_data

    return read_data(capture_file_for(root))[name]


def store_capture_block(root: Path, name: str, value) -> None:
    """Rewrite one capture block and re-seal the manifest so bytes stay bound."""

    from description_pipeline.io import read_data, write_json

    path = capture_file_for(root)
    capture = read_data(path)
    capture[name] = value
    write_json(path, capture)
    _refresh_snapshot_manifest(path.parent.parent)
    seal_delivery(path.parent.parent)


def delete_capture_block(root: Path, name: str) -> None:
    """Remove one capture block and re-seal the manifest."""

    from description_pipeline.io import read_data, write_json

    path = capture_file_for(root)
    capture = read_data(path)
    capture.pop(name, None)
    write_json(path, capture)
    _refresh_snapshot_manifest(path.parent.parent)
    seal_delivery(path.parent.parent)


def _refresh_snapshot_manifest(delivery_root: Path) -> None:
    """Rebuild the snapshot manifest inventory when the tree carries one.

    Trees that model the frozen delivery keep ``evidence/manifest.json`` (the
    capture identity and the file inventory the verifier re-checks); rewriting
    a capture block must keep that inventory true.
    """

    from description_pipeline.io import read_data
    from description_pipeline.sources.snapshot import write_manifest

    manifest_path = Path(delivery_root) / "evidence/manifest.json"
    if not manifest_path.is_file():
        return
    current = read_data(manifest_path)
    write_manifest(
        Path(delivery_root) / "evidence",
        kind=current["kind"],
        identity=current["identity"],
        evidence_class=current["evidence_class"],
    )


# ---------------------------------------------------------------------------
# Native discovery fixtures (IF-13): fold a prepared native package into one
# target-tree delivery.  This is the test-side preview of the producer-side
# conversion (worker A's a2): the record gains the frozen file inventory and
# robot carrier, the originals move under ``input/cad`` and the embedded
# records under ``input/records``.
# ---------------------------------------------------------------------------


def frozen_discovery_payload(
    record: dict,
    *,
    files: dict[str, str],
) -> dict:
    """The IF-13 discovery record: the native payload plus the frozen additions.

    ``files`` maps frozen-source-relative native paths to their digests; the
    record carries them as ``files[]`` plus the canonical
    ``native_inventory_sha256`` and the top-level ``robot_name`` resolved from
    the recorded identity (authored name, then its technical label).
    """

    from description_pipeline.io import digest

    payload = copy.deepcopy(record)
    rows = [{"path": path, "sha256": checksum} for path, checksum in sorted(files.items())]
    payload["files"] = rows
    payload["native_inventory_sha256"] = digest(rows)
    identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
    payload["robot_name"] = identity.get("robot_name") or identity.get("robot_name_technical") or "robot"
    return payload


def native_discovery_delivery(root: Path, prepared: Path, *, record: dict | None = None) -> Path:
    """Materialize one target-tree delivery from a prepared native package.

    The CAD originals move to ``input/cad`` at their frozen-source-relative
    paths; the record becomes ``evidence/discovery.json`` (frozen additions
    applied); the embedded records move under ``input/records`` keeping their
    recorded ``package_file`` names; ``input/source.json`` is the prepared
    package's archived content identity verbatim (the fixture record only
    backs packages that could not name one).  Returns the delivery root.
    """

    import hashlib

    from description_pipeline.io import read_data

    prepared = Path(prepared)
    if record is None:
        record = read_data(prepared / "discovery/native-discovery.json")
    raw = record.get("raw") or {}
    raw_files = raw.get("files") if isinstance(raw.get("files"), dict) else {}
    originals: dict[str, str] = {}
    files: dict[str, str] = {}
    for relative in sorted(raw_files):
        data = (prepared / str(relative)).read_bytes()
        files[str(relative)] = hashlib.sha256(data).hexdigest()
        originals[str(relative)] = data.decode("utf-8")
    records: dict[str, str] = {}
    references = list(record.get("records") or []) + ([record["budget"]] if record.get("budget") else [])
    for entry in references:
        if not isinstance(entry, dict):
            continue
        package_file = str(entry.get("package_file") or "")
        source = prepared / package_file
        if package_file and source.is_file():
            records[package_file] = source.read_text(encoding="utf-8")
    payload = frozen_discovery_payload(record, files=files)
    prepared_source = prepared / "source.json"
    source_record = read_data(prepared_source) if prepared_source.is_file() else source_record_for_robot(record)
    return write_target_delivery(
        root,
        capture={},
        discovery=payload,
        source=source_record,
        originals=originals,
        records=records,
    )


def source_record_for_robot(record: dict) -> dict:
    """The archived input/source.json identity for one prepared native package."""

    identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    revision = identity.get("revision")
    return default_source_record(
        hardware_id=str(identity.get("hardware_id") or "fixture"),
        configuration=str(identity.get("delivery_configuration") or "Default"),
        main_assembly=str(identity.get("main_assembly") or "robot.SLDASM"),
        revision=revision if isinstance(revision, str) and revision else None,
    )


def capture_requests(config: dict) -> dict:
    """Translate a fixture config's carriers into the minimal native request sets.

    The product consumes ``groups``/``axis_requests`` only; fixtures keep their
    bodies/joints/frames authoring structures and translate at the call
    boundary (interface frames come from the native ``TCP_``/``SCS_`` datums).
    No product code carries a compatibility fallback.
    """

    cfg = dict(config)
    bodies = cfg.pop("bodies", None)
    if bodies is not None and "groups" not in cfg:
        cfg["groups"] = [
            {
                "name": str(body.get("name") or body.get("id") or ""),
                "components": sorted(str(member) for member in (body.get("components") or [])),
            }
            for body in bodies
        ]
    joints = cfg.pop("joints", None)
    if joints is not None and "axis_requests" not in cfg:
        cfg["axis_requests"] = [
            {"name": str(joint.get("name") or joint.get("id") or ""), "axis_reference": joint["axis_reference"]}
            for joint in joints
            if isinstance(joint.get("axis_reference"), dict)
        ]
    cfg.pop("frames", None)
    return cfg


def _record_keys_for(
    record: dict,
    record_keys: dict[str, str | Sequence[str]] | None,
    relative: str,
) -> list[str]:
    """Every controlled key referenced by the raw claims for one fixture record."""

    keys: list[str] = []
    properties = record.get("properties") if isinstance(record.get("properties"), dict) else {}

    def visit(value) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        else:
            if not isinstance(value, str) or "#" not in value:
                return
            referenced, _, key = value.partition("#")
            if referenced.replace("\\", "/") == relative and key:
                keys.append(key)

    visit(properties)
    configured = (record_keys or {}).get(relative)
    if isinstance(configured, str):
        keys.append(configured)
    elif configured:
        keys.extend(str(key) for key in configured)
    return list(dict.fromkeys(keys))


def compose_delivery_inputs(
    delivery: Path,
    *,
    main_assembly: str,
    configuration: str,
    record: dict,
    cad_files: dict[str, Path],
    records: dict[str, str] | None = None,
    record_keys: dict[str, str | Sequence[str]] | None = None,
) -> None:
    """Compose the sealed inputs Linux consumers rebuild from (fixture plumbing).

    Copies the genuine CAD bytes into ``input/cad/<relative>``, writes the
    sealed discovery payload (with its raw native record) at
    ``evidence/discovery.json`` (including optional controlled-record files at
    ``input/records/<name>`` carrying their payload entries). Synthetic
    consumer-fixture inputs only: it must never feed the unchanged
    canonical/E2E producers, which produce their own evidence.  Existing
    fixture blocks (warnings and readouts included) are preserved, and the
    provenance identity binds to the record's captured identity rather than a
    relocated fixture path.  No consumer output is injected.
    """

    import shutil

    from description_pipeline.io import file_digest, write_json
    from description_pipeline.sources.solidworks.discovery import DISCOVERY_SCHEMA

    write_fixture_request(Path(delivery), main_assembly=main_assembly)
    delivery = Path(delivery)
    evidence = delivery / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    native_files: dict[str, str] = {}
    for relative, source in cad_files.items():
        target = delivery / "input" / "cad" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        native_files[str(relative)] = file_digest(target)
    identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    import json
    import hashlib as _hashlib

    payload_records: list[dict] = []
    for name, text in (records or {}).items():
        archive_name = "records/" + Path(name).name
        target = delivery / "input" / archive_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        common = {
            "file": str(name),
            "package_file": archive_name,
            "sha256": _hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        keys = _record_keys_for(record, record_keys, str(name))
        if keys:
            payload_records.extend(
                {
                    **common,
                    "key": str(key),
                    "reference": f"{name}#{key}",
                    "anchor": json.dumps(str(key)),
                }
                for key in keys
            )
        else:
            payload_records.append(common)
    write_json(
        evidence / "discovery.json",
        {
            "schema_version": DISCOVERY_SCHEMA,
            "run_id": str(record.get("run_id") or "fixture-run"),
            "derivation": {
                "main_assembly": main_assembly,
                "configuration": configuration,
                "record_roots": [],
            },
            "identity": {
                "main_assembly": main_assembly,
                "delivery_configuration": configuration,
                "robot_name": str(identity.get("robot_name") or "robot"),
            },
            "records": payload_records,
            "raw": record,
            "native_files": dict(sorted(native_files.items())),
        },
    )
# ---------------------------------------------------------------------------
# Fixture plumbing: the small shared producer for consumer-fixture inputs.
#
# The scenario facts - which occurrences, datums, mates and masses a module
# exercises - stay with the module that builds them; these builders own the
# record boilerplate and the sealing infrastructure only.  Synthetic
# test-only inputs: the unchanged canonical/E2E producers build their own
# evidence and never use any of this.  No consumer output is injected.
# ---------------------------------------------------------------------------


def _native_float(value: float | int) -> float:
    """Canonical record float: +0.0 zeros, all other values unchanged (fixture plumbing)."""

    return float(value) + 0.0


def native_component(
    name: str, *, document: str, transform: Sequence[float] | None = None, fixed: bool = True
) -> dict:
    """One recorded native occurrence entry (fixture plumbing).

    ``document`` is the scenario's own binding of the occurrence to its
    original part document; it is never derived from the occurrence name.
    """

    return {
        "name2": name,
        "lightweight": False,
        "suppression_state": 2,
        "instance_id": name,
        "document": str(document),
        "configuration": "Default",
        "fixed": fixed,
        "suppressed": False,
        "transform": [_native_float(value) for value in (placement() if transform is None else transform)],
    }


def native_datum(name: str, owner: str, transform: Sequence[float]) -> dict:
    """One recorded native coordinate system (fixture plumbing)."""

    return {"name": name, "owner": owner, "array": [_native_float(value) for value in transform]}


def native_hinge_mates(
    left: str,
    right: str,
    *,
    left_point: Sequence[float],
    right_point: Sequence[float],
    radius: float,
    name: str = "hinge_joint",
) -> list[dict]:
    """The solved concentric+coincident mate pair one movable hinge requires.

    Both axes point along +z in their component frames; each scenario keeps
    its own solved points and shaft radius.  A hinge axis must pass through
    the child body's datum origin (the joint frame check requires it).
    """

    def cylinder(component: str, feature: str, face_index: int, point: Sequence[float]) -> dict:
        return {
            "component": component,
            "feature": feature,
            "face_index": face_index,
            "cylinder": {
                "point": [float(value) for value in point],
                "direction": [0.0, 0.0, 1.0],
                "radius": float(radius),
            },
        }

    def plane(component: str, feature: str, point: Sequence[float], normal: float) -> dict:
        return {
            "component": component,
            "feature": feature,
            "plane": {"point": [float(value) for value in point], "normal": [0.0, 0.0, normal]},
        }

    return [
        {
            "name": f"{name}__coaxial",
            "type": "concentric",
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [cylinder(left, "Cyl1", 3, left_point), cylinder(right, "Cyl2", 7, right_point)],
        },
        {
            "name": f"{name}__locate",
            "type": "coincident",
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [plane(left, "Plane1", left_point, 1.0), plane(right, "Plane2", right_point, -1.0)],
        },
    ]


def native_limit_observation(
    type_index: int,
    *,
    nominal: float,
    lower: float,
    upper: float,
) -> dict:
    """One complete raw native-limit record for a limit angle/distance mate."""

    if type_index == 6:
        unit = "rad"
        interface = "IAngleMateFeatureData"
        iid = "{EF4303D7-B4FD-46E7-AD34-D942C420E096}"
        nominative, minimum, maximum = "Angle", "MinimumAngle", "MaximumAngle"
    elif type_index == 5:
        unit = "m"
        interface = "IDistanceMateFeatureData"
        iid = "{943EED8D-E13B-4459-BD59-EBCC3F60B915}"
        nominative, minimum, maximum = "Distance", "MinimumDistance", "MaximumDistance"
    else:
        raise ValueError("unsupported native limit type")
    return {
        "type_index": type_index,
        "definition_interface": interface,
        "definition_interface_iid": iid,
        "nominal_dimension": {"value": float(nominal), "api": f"{interface}.{nominative}", "unit": unit},
        "minimum_variation": {
            "value": float(lower) - nominal,
            "api": "IMate2.MinimumVariation",
            "unit": unit,
        },
        "maximum_variation": {
            "value": float(upper) - nominal,
            "api": "IMate2.MaximumVariation",
            "unit": unit,
        },
        "minimum_limit": {"value": float(lower), "api": f"{interface}.{minimum}", "unit": unit},
        "maximum_limit": {"value": float(upper), "api": f"{interface}.{maximum}", "unit": unit},
    }


def native_limit_mate(
    left: str,
    right: str,
    *,
    lower: float = -1.0,
    upper: float = 1.0,
    name: str = "hinge_joint",
    point: Sequence[float] = (0.0, 0.0, 0.0),
) -> dict:
    """One enabled angular limit mate beside a hinge's solved constraint pair."""

    native_limit = native_limit_observation(6, nominal=0.0, lower=lower, upper=upper)
    return {
        "name": f"{name}__limit",
        "type": "limitangle",
        "suppressed": False,
        "error_code": 0,
        "scope": "",
        "limits": {"lower": float(lower), "upper": float(upper), "unit": "rad"},
        "is_advanced_mate": True,
        "is_advanced_mate_error": None,
        "is_advanced_mate_api": "IAngleMateFeatureData.IsAdvancedMate",
        "native_limit": native_limit,
        "entities": [
            {
                "entity_index": 0,
                "component": str(left),
                "reference_configuration": "Default",
                "feature": "LimitPlane1",
                "face_index": None,
                "mate_entity_reference": {"error": None, "geometry": None},
                "plane": {"point": [float(value) for value in point], "normal": [0.0, 0.0, 1.0]},
            },
            {
                "entity_index": 1,
                "component": str(right),
                "reference_configuration": "Default",
                "feature": "LimitPlane2",
                "face_index": None,
                "mate_entity_reference": {"error": None, "geometry": None},
                "plane": {"point": [float(value) for value in point], "normal": [0.0, 0.0, 1.0]},
            },
        ],
    }


def native_lock(members: Sequence[str], *, name: str = "board_rigid") -> dict:
    """One solved lock mate merging the occurrences into a rigid body (fixture plumbing)."""

    return {
        "name": name,
        "type": "lock",
        "suppressed": False,
        "error_code": 0,
        "scope": "",
        "limits": None,
        "entities": [{"component": str(member)} for member in members],
    }


def native_joint_claims(*, record_file: str = "joints/hinge.json", mate: str = "hinge_joint__coaxial") -> dict:
    """The dp.joint.* claims a hinge's name and limits/drive travel through (fixture plumbing)."""

    return {
        mate: {
            "dp.joint.axis_sign": "+1",
            "dp.joint.limits_record": f"{record_file}#limits",
            "dp.joint.drive_record": f"{record_file}#drive",
        }
    }


def native_joint_record(*, lower: float = -1.0, upper: float = 1.0, effort: float = 2.0, velocity: float = 3.0) -> str:
    """The controlled joint record text the claims resolve against (fixture plumbing)."""

    import json

    return json.dumps({"limits": {"lower": lower, "upper": upper}, "drive": {"effort": effort, "velocity": velocity}})


def native_budget_record(record: dict, *, tolerance: float = 0.1) -> str:
    """The controlled design-budget text the record's budget property binds (fixture plumbing)."""

    import json

    mass = float(record["assembly_mass_kg"])
    return json.dumps(
        {"robot": {"expected_mass_kg": [mass - tolerance, mass + tolerance], "expected_extent_m": [0.2, 0.8]}}
    )


def native_files(cad_root: Path, names: Sequence[str]) -> dict[str, str]:
    """SHA-256 of the genuine CAD fixture bytes, keyed by relative name (fixture plumbing)."""

    import hashlib

    return {name: hashlib.sha256((Path(cad_root) / name).read_bytes()).hexdigest() for name in names}


def native_record(
    *,
    hardware_id: str,
    change_summary: str,
    control_reference: str,
    components: Sequence[dict],
    datums: Sequence[dict],
    masses: Sequence[dict],
    mates: Sequence[dict] = (),
    joint_claims: dict | None = None,
    budget: bool = True,
    assembly_mass_kg: float | None = None,
    assembly_extent_m: float = 0.5,
    files: dict[str, str] | None = None,
) -> dict:
    """The sealed native discovery record the consumer fixtures rebuild from.

    Owns the record boilerplate only: the scenario facts (which occurrences,
    datums, mates and masses) stay with the module that builds them.  A
    ``joint_claims`` block names the controlled limits/drive records a hinge
    travels through; ``budget`` binds the controlled design-budget record;
    when frozen ``files`` are given, the record carries their inventory and
    the sealed selection over the ``robot.SLDASM`` fixture tree.
    """

    from description_pipeline.sources.solidworks.discovery import CONTRACT, DISCOVERY_SCHEMA

    from .selection_support import sealed_selection

    properties: dict[str, Any] = {"components": {}, "mates": dict(joint_claims or {})}
    if budget:
        properties["document"] = {"dp.design_budget_record": "budget.json#robot"}
    payload: dict[str, Any] = {
        "schema_version": DISCOVERY_SCHEMA,
        "contract": CONTRACT,
        "namespace": "dp",
        "identity": {
            "hardware_id": hardware_id,
            "revision": "r1",
            "owner": "mechanical-team",
            "change_summary": change_summary,
            "control": {"system": "handoff", "reference": control_reference},
            "delivery_configuration": "Default",
            "main_assembly": "robot.SLDASM",
            "robot_name": "robot",
        },
        "saved_configuration": "Default",
        "document_state": saved_document_state(),
        "components": [dict(item) for item in components],
        "mates": [dict(item) for item in mates],
        "datums": [dict(item) for item in datums],
        "masses": [dict(item) for item in masses],
        "assembly_mass_kg": float(
            assembly_mass_kg if assembly_mass_kg is not None else sum(float(row["mass_kg"]) for row in masses)
        ),
        "assembly_extent_m": float(assembly_extent_m),
        "properties": properties,
    }
    if files is not None:
        payload["files"] = dict(files)
        parts = [name for name in files if name != "robot.SLDASM"]
        payload["source_selection"] = sealed_selection(
            dict(files),
            "robot.SLDASM",
            edges={"robot.SLDASM": parts, **{name: [] for name in parts}},
        )
    return payload


def seal_scene_inputs(
    destination: Path,
    *,
    cad_root: Path,
    cad_names: Sequence[str],
    record: dict,
    controlled: dict[str, str] | None = None,
    record_keys: dict[str, str | Sequence[str]] | None = None,
) -> None:
    """Fixture plumbing: seal the consumer inputs a Linux scene rebuild reads.

    Copies the genuine CAD bytes into ``input/cad`` and writes the sealed
    discovery payload (the raw native record and any controlled records) into
    a frozen snapshot, then re-seals the manifest so the added members stay
    bound by the verified inventory.
    """

    import json

    identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    compose_delivery_inputs(
        destination,
        main_assembly=str(identity.get("main_assembly") or "robot.SLDASM"),
        configuration=str(identity.get("delivery_configuration") or "Default"),
        record=record,
        cad_files={name: Path(cad_root) / name for name in cad_names},
        records=controlled,
        record_keys=record_keys,
    )
    runtime = Path(destination) / "native-runtime.json"
    environment = Path(destination) / "evidence/environment.json"
    if not runtime.is_file():
        runtime.write_bytes(environment.read_bytes())
    from packaging.utils import canonicalize_name

    from description_pipeline import __version__
    from description_pipeline.delivery import PIPELINE_ID
    from description_pipeline.io import read_data, write_json
    from description_pipeline.runtime import RUNTIME_VERSIONS, required_packages

    runtime_record = read_data(runtime)
    native_tool = {
        "schema_version": "solidworks-to-urdf.tool/v1",
        "pipeline_id": PIPELINE_ID,
        "version": __version__,
        "source_sha256": "f" * 64,
        "release": None,
        "runtime": {
            "role": "native",
            "system": "Windows",
            "python": "3.12.14",
            "machine": "AMD64",
            "packages": {
                canonicalize_name(name): RUNTIME_VERSIONS[name]
                for name in required_packages("native")
            },
        },
    }
    rewrite_path = Path(destination) / "evidence/rewrite-environment.json"
    rewrite = read_data(rewrite_path)
    runtime_copy_pid = runtime_record.get("solidworks", {}).get("sessions", {}).get("copy", {}).get("pid")
    rewrite_copy_pid = rewrite.get("solidworks", {}).get("sessions", {}).get("copy", {}).get("pid")
    runtime_copy_pid = runtime_copy_pid if type(runtime_copy_pid) is int and runtime_copy_pid > 0 else 101
    rewrite_copy_pid = rewrite_copy_pid if type(rewrite_copy_pid) is int and rewrite_copy_pid > 0 else 100
    source_pid = min(runtime_copy_pid, rewrite_copy_pid) - 1
    if source_pid <= 0:
        source_pid = max(runtime_copy_pid, rewrite_copy_pid) + 1
    runtime_sessions = {
        "source": {"pid": source_pid, "state": "closed"},
        "copy": {"pid": runtime_copy_pid, "state": "active"},
    }
    rewrite_sessions = {
        "source": {"pid": source_pid, "state": "closed"},
        "copy": {"pid": rewrite_copy_pid, "state": "active"},
    }
    runtime_record["platform"] = "Windows-11"
    runtime_record["toolchain"] = native_tool
    runtime_record["worker_version"] = str(native_tool["version"])
    runtime_record["solidworks"]["sessions"] = runtime_sessions
    boundary = runtime_record.get("session_boundary")
    if isinstance(boundary, dict) and isinstance(boundary.get("retirement"), dict):
        retirement = boundary["retirement"]
        for key, state in (("before", "active"), ("after", "closed")):
            identity = retirement.get(key)
            if isinstance(identity, dict):
                retirement[key] = {
                    "pid": rewrite_copy_pid,
                    "state": state,
                    **{
                        name: identity[name]
                        for name in ("executable", "ownership", "lease")
                        if name in identity
                    },
                }
    rewrite["platform"] = "Windows-11"
    rewrite["toolchain"] = native_tool
    rewrite["worker_version"] = str(native_tool["version"])
    rewrite["solidworks"]["sessions"] = rewrite_sessions
    write_json(runtime, runtime_record)
    write_json(rewrite_path, rewrite)
    write_json(Path(destination) / "reports/native-tool.json", native_tool)
    from description_pipeline.sources.snapshot import write_manifest

    manifest = json.loads((Path(destination) / "manifest.json").read_text(encoding="utf-8"))
    write_manifest(
        Path(destination),
        kind=manifest["kind"],
        identity=manifest["identity"],
        evidence_class=manifest["evidence_class"],
    )


def stage_discovery_inputs(
    root: Path,
    *,
    record: dict,
    controlled: dict[str, str] | None = None,
    record_keys: dict[str, str | Sequence[str]] | None = None,
) -> None:
    """Fixture plumbing: stage the discovery payload for hand-built producer roots.

    The controlled records land under ``input/records``, the payload binds
    them, and the capture's base occurrence is stubbed under ``input/cad`` -
    the raw-evidence sibling of ``compose_delivery_inputs`` for the fixture
    roots that carry no snapshot manifest (closure and its accounting
    consumer).
    """

    import hashlib
    import json

    from description_pipeline.io import write_json

    root = Path(root)
    payload_records = []
    for relative, text in (controlled or {}).items():
        archive_name = "records/" + Path(relative).name
        path = root / "input" / archive_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        common = {
            "file": relative,
            "package_file": archive_name,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        keys = _record_keys_for(record, record_keys, str(relative))
        if keys:
            payload_records.extend(
                {
                    **common,
                    "key": str(key),
                    "reference": f"{relative}#{key}",
                    "anchor": json.dumps(str(key)),
                }
                for key in keys
            )
        else:
            payload_records.append(common)
    write_json(
        root / "evidence" / "discovery.json",
        {
            "schema_version": "solidworks-to-urdf.native-discovery/v1",
            "run_id": "fixture-run",
            "derivation": {
                "main_assembly": "robot.SLDASM",
                "configuration": "Default",
                "record_roots": [],
            },
            "identity": {
                "main_assembly": "robot.SLDASM",
                "delivery_configuration": "Default",
                "robot_name": "robot",
            },
            "records": payload_records,
            "raw": record,
            "native_files": {},
        },
    )
    write_fixture_request(root, main_assembly="robot.SLDASM")
    assembly = root / "input" / "cad" / "robot.SLDASM"
    assembly.parent.mkdir(parents=True, exist_ok=True)
    assembly.write_bytes(b"fixture assembly")


def write_fixture_request(delivery: Path, *, main_assembly: str) -> None:
    """Fixture plumbing: the frozen v2 request record every fixture delivery carries.

    Synthetic fixture data only (``purpose=engineering``); the persisted request
    remains the single purpose authority every consumer reads.  An existing
    record is left untouched so fixtures may author their own.
    """

    from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes

    target = Path(delivery) / "input" / "request.json"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        request_evidence_bytes(
            build_request_evidence(
                purpose="engineering",
                request_id="fixture-run",
                target_hardware="fixture",
                change_summary="fixture frozen request",
                main_assembly=main_assembly,
                handoff_digest="0" * 64,
                platform_config_digest="0" * 64,
            )
        )
    )
