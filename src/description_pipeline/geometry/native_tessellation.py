"""Strict native face-span grammar shared by verification and report projection."""

from __future__ import annotations

import math
import re

from ..io import PipelineError
from ..schemas import NATIVE_TESSELLATION_SCHEMA

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _integer(value) -> bool:
    return type(value) is int


def parse_native_tessellation(
    component: str,
    tessellation: object,
    bodies: dict,
    triangles: int,
) -> list[dict]:
    """Validate the one current tessellation schema and return its face spans."""

    _require(
        isinstance(tessellation, dict)
        and set(tessellation) == {
            "schema_version",
            "faces",
            "face_spans",
            "native_edges",
            "no_facets_faces",
        },
        component + ": unknown or legacy tessellation accounting",
    )
    _require(
        tessellation["schema_version"] == NATIVE_TESSELLATION_SCHEMA,
        component + ": unsupported native tessellation schema",
    )
    spans = tessellation["face_spans"]
    faces = tessellation["faces"]
    _require(
        _integer(faces)
        and faces > 0
        and isinstance(spans, list)
        and len(spans) == faces,
        component + ": native face-span count is inconsistent",
    )
    required = {
        "body_type",
        "body_index",
        "body_name",
        "body_reference_sha256",
        "face_index",
        "face_reference_sha256",
        "face_owner_id",
        "edge_references",
        "triangle_start",
        "triangle_count",
        "triangle_records_sha256",
    }
    previous = None
    next_start = 0
    body_names = {}
    body_references = {}
    face_references = set()
    face_owner_ids = set()
    face_keys = set()
    bodies_by_face = {}
    face_edge_references = {}
    for index, span in enumerate(spans):
        _require(
            isinstance(span, dict) and set(span) == required,
            component + f": face span {index} has an unknown shape",
        )
        body_type = span["body_type"]
        body_index = span["body_index"]
        face_index = span["face_index"]
        start = span["triangle_start"]
        count = span["triangle_count"]
        body_name = span["body_name"]
        body_reference = span["body_reference_sha256"]
        face_reference = span["face_reference_sha256"]
        face_owner_id = span["face_owner_id"]
        edge_references = span["edge_references"]
        records_digest = span["triangle_records_sha256"]
        _require(
            _integer(body_type)
            and body_type in (0, 1)
            and _integer(body_index)
            and body_index >= 0
            and _integer(face_index)
            and face_index >= 0
            and _integer(start)
            and _integer(count)
            and count >= 0
            and isinstance(body_name, str)
            and bool(body_name)
            and isinstance(body_reference, str)
            and bool(HEX64.fullmatch(body_reference))
            and isinstance(face_reference, str)
            and bool(HEX64.fullmatch(face_reference))
            and _integer(face_owner_id)
            and 0 < face_owner_id <= 0xFFFF
            and isinstance(edge_references, list)
            and len(edge_references) == len(set(edge_references))
            and all(
                isinstance(reference, str) and bool(HEX64.fullmatch(reference))
                for reference in edge_references
            )
            and isinstance(records_digest, str)
            and bool(HEX64.fullmatch(records_digest)),
            component + f": face span {index} is malformed",
        )
        body_count = bodies["solid" if body_type == 0 else "sheet"]
        _require(
            body_index < body_count,
            component + f": face span {index} names a body outside its native inventory",
        )
        order = (body_type, body_index, face_index)
        _require(
            previous is None or order > previous,
            component + f": face span {index} is duplicated or out of canonical order",
        )
        _require(
            start == next_start,
            component + f": face span {index} leaves a gap or overlaps the triangle stream",
        )
        body_key = (body_type, body_index)
        _require(
            body_names.setdefault(body_key, body_name) == body_name,
            component + f": native body {body_key} changes name across its faces",
        )
        _require(
            body_references.setdefault(body_key, body_reference) == body_reference,
            component + f": native body {body_key} changes persistent reference across its faces",
        )
        _require(
            face_reference not in face_references,
            component + f": native face reference repeats at span {index}",
        )
        face_references.add(face_reference)
        _require(
            face_owner_id not in face_owner_ids,
            component + f": face owner id repeats at span {index}",
        )
        face_owner_ids.add(face_owner_id)
        face_key = (*body_key, face_index)
        _require(face_key not in face_keys, component + f": duplicate native face {face_key}")
        face_keys.add(face_key)
        bodies_by_face.setdefault(body_key, []).append(face_index)
        face_edge_references[face_key] = set(edge_references)
        previous = order
        next_start += count
    _require(
        _integer(triangles) and next_start == triangles,
        component + ": face spans do not cover the complete native STL triangle stream",
    )
    expected_bodies = {
        (body_type, body_index)
        for body_type, count in ((0, bodies["solid"]), (1, bodies["sheet"]))
        for body_index in range(count)
    }
    _require(
        set(bodies_by_face) == expected_bodies,
        component + ": face spans do not cover every native body",
    )
    _require(
        all(indices == list(range(len(indices))) for indices in bodies_by_face.values()),
        component + ": each native body must expose a contiguous face-index sequence",
    )
    _require(
        len(set(body_references.values())) == len(expected_bodies),
        component + ": a persistent body reference is missing or reused",
    )
    native_edges = tessellation["native_edges"]
    _require(isinstance(native_edges, list), component + ": native edge evidence is missing")
    edge_required = {
        "body_type",
        "body_index",
        "edge_reference_sha256",
        "start_vertex_reference_sha256",
        "end_vertex_reference_sha256",
        "start_point_m",
        "end_point_m",
        "face_indices",
    }
    edge_owners = {}
    seen_edges = set()
    vertex_points = {}
    for index, edge in enumerate(native_edges):
        _require(
            isinstance(edge, dict) and set(edge) == edge_required,
            component + f": native edge {index} has an unknown shape",
        )
        reference = edge["edge_reference_sha256"]
        _require(
            _integer(edge["body_type"])
            and edge["body_type"] in (0, 1)
            and _integer(edge["body_index"])
            and edge["body_index"] >= 0
            and isinstance(reference, str)
            and bool(HEX64.fullmatch(reference))
            and isinstance(edge["start_vertex_reference_sha256"], str)
            and bool(HEX64.fullmatch(edge["start_vertex_reference_sha256"]))
            and isinstance(edge["end_vertex_reference_sha256"], str)
            and bool(HEX64.fullmatch(edge["end_vertex_reference_sha256"]))
            and isinstance(edge["start_point_m"], list)
            and len(edge["start_point_m"]) == 3
            and all(
                type(value) in (int, float) and math.isfinite(value)
                for value in edge["start_point_m"]
            )
            and isinstance(edge["end_point_m"], list)
            and len(edge["end_point_m"]) == 3
            and all(
                type(value) in (int, float) and math.isfinite(value)
                for value in edge["end_point_m"]
            )
            and isinstance(edge["face_indices"], list)
            and edge["face_indices"] == sorted(set(edge["face_indices"]))
            and all(_integer(value) and value >= 0 for value in edge["face_indices"]),
            component + f": native edge {index} is malformed",
        )
        body_key = (edge["body_type"], edge["body_index"])
        _require(body_key in expected_bodies, component + f": native edge {index} names an unknown body")
        _require(reference not in seen_edges, component + f": native edge {index} is duplicated")
        seen_edges.add(reference)
        for vertex_reference, point in (
            (edge["start_vertex_reference_sha256"], edge["start_point_m"]),
            (edge["end_vertex_reference_sha256"], edge["end_point_m"]),
        ):
            observed = vertex_points.setdefault(vertex_reference, tuple(point))
            _require(
                observed == tuple(point),
                component + f": native vertex {vertex_reference} binds multiple coordinates",
            )
        edge_owners[reference] = (body_key, set(edge["face_indices"]))
        _require(
            all((*body_key, face_index) in face_edge_references for face_index in edge_owners[reference][1]),
            component + f": native edge {index} names an unknown face",
        )
    for face_key, references in face_edge_references.items():
        body_key = face_key[:2]
        for reference in references:
            owner = edge_owners.get(reference)
            _require(
                owner is not None
                and owner[0] == body_key
                and face_key[2] in owner[1],
                component + f": face {face_key} has no matching native edge incidence for {reference}",
            )
    for reference, (body_key, face_indices) in edge_owners.items():
        _require(
            all(
                reference in face_edge_references[(*body_key, face_index)]
                for face_index in face_indices
            ),
            component + ": native edge incidence is not bound to every declared face",
        )
    _require(
        not (
            set(body_references.values())
            & face_references
            or set(body_references.values()) & seen_edges
            or face_references & seen_edges
            or set(body_references.values()) & set(vertex_points)
            or face_references & set(vertex_points)
            or seen_edges & set(vertex_points)
        ),
        component + ": body, face, edge and vertex identities must not alias",
    )
    return spans


def summarize_native_topology(tessellation: dict, spans: list[dict]) -> dict:
    """Summarize native edge incidence without inventing geometric adjacency."""

    body_rows = {}
    for span in spans:
        key = (span["body_type"], span["body_index"])
        row = body_rows.setdefault(
            key,
            {
                "body_type": key[0],
                "body_index": key[1],
                "body_name": span["body_name"],
                "body_reference_sha256": span["body_reference_sha256"],
                "faces": 0,
                "triangles": 0,
                "native_edges": 0,
                "shared_edges": 0,
                "open_edges": 0,
                "nonmanifold_edges": 0,
            },
        )
        row["faces"] += 1
        row["triangles"] += span["triangle_count"]
    vertex_owners = {}
    for edge in tessellation["native_edges"]:
        key = (edge["body_type"], edge["body_index"])
        row = body_rows[key]
        row["native_edges"] += 1
        incidence = len(edge["face_indices"])
        if incidence == 1:
            row["open_edges"] += 1
        elif incidence == 2:
            row["shared_edges"] += 1
        else:
            row["nonmanifold_edges"] += 1
        for reference in (
            edge["start_vertex_reference_sha256"],
            edge["end_vertex_reference_sha256"],
        ):
            vertex_owners.setdefault(reference, set()).add(key)
    rows = [body_rows[key] for key in sorted(body_rows)]
    complete = (
        bool(rows)
        and all(row["native_edges"] > 0 and row["faces"] > 0 for row in rows)
    )
    return {
        "status": "evidence_complete" if complete else "unresolved",
        "qualification": (
            "native_edge_incidence_only; normalized_boundary_evidence_required"
        ),
        "cross_body_vertex_reference_count": sum(
            len(owners) > 1 for owners in vertex_owners.values()
        ),
        "bodies": rows,
    }
