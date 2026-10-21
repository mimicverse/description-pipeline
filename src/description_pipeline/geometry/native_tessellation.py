"""Strict native face-span grammar shared by verification and report projection."""

from __future__ import annotations

import math
import re
import struct

from ..io import PipelineError
from ..schemas import NATIVE_TESSELLATION_SCHEMA

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
ITESSELLATION_IID = "{699C7492-DB56-4CC2-B7A2-9FFD56698434}"


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
    triangle_vertices: list[tuple[tuple[float, float, float], ...]] | None = None,
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
            "itessellation",
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
    face_spans_by_key = {}
    spans_by_body = {}
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
        face_spans_by_key[face_key] = span
        spans_by_body.setdefault(body_key, []).append(span)
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
        "fin_ids",
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
            and bool(edge["face_indices"])
            and edge["face_indices"] == sorted(set(edge["face_indices"]))
            and all(_integer(value) and value >= 0 for value in edge["face_indices"])
            and isinstance(edge["fin_ids"], list)
            and edge["fin_ids"] == sorted(set(edge["fin_ids"]))
            and all(_integer(value) and value >= 0 for value in edge["fin_ids"]),
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
    _parse_v3_itessellation(
        component,
        tessellation["itessellation"],
        spans_by_body,
        face_spans_by_key,
        native_edges,
        body_references,
        expected_bodies,
        triangles,
        triangle_vertices,
    )
    return spans


def _parse_v3_itessellation(
    component: str,
    value: object,
    spans_by_body: dict,
    face_spans_by_key: dict,
    native_edges: list[dict],
    body_references: dict,
    expected_bodies: set,
    triangles: int,
    triangle_vertices: list[tuple[tuple[float, float, float], ...]] | None,
) -> None:
    """Validate body-local ITessellation facet/fin evidence fail closed."""

    _require(
        isinstance(value, dict)
        and set(value) == {"interface_iid", "properties", "bodies", "vertices"},
        component + ": native tessellation v3 ITessellation evidence is missing",
    )
    _require(
        value["interface_iid"] == ITESSELLATION_IID,
        component + ": native tessellation v3 ITessellation interface is unsupported",
    )
    properties = value["properties"]
    expected_properties = (
        "need_face_facet_map",
        "need_edge_fin_map",
        "need_vertex_params",
    )
    _require(
        isinstance(properties, dict)
        and set(properties) == set(expected_properties)
        and all(
            type(properties[key]) is bool and properties[key]
            for key in expected_properties
        ),
        component + ": native tessellation v3 ITessellation properties are incomplete",
    )
    if triangle_vertices is not None:
        _require(
            isinstance(triangle_vertices, list)
            and len(triangle_vertices) == triangles
            and all(
                isinstance(triangle, (list, tuple))
                and len(triangle) == 3
                and all(
                    isinstance(point, (list, tuple))
                    and len(point) == 3
                    and all(
                        type(coordinate) in (int, float)
                        and math.isfinite(coordinate)
                        for coordinate in point
                    )
                    for point in triangle
                )
                for triangle in triangle_vertices
            ),
            component + ": native STL triangle vertices are unavailable for v3 binding",
        )
    raw_bodies = value["bodies"]
    _require(
        isinstance(raw_bodies, list) and bool(raw_bodies),
        component + ": native tessellation v3 body evidence is missing",
    )

    body_evidence = {}
    body_counts = {}
    previous_body = None
    for index, body in enumerate(raw_bodies):
        _require(
            isinstance(body, dict)
            and set(body)
            == {
                "body_type",
                "body_index",
                "body_reference_sha256",
                "tessellate_result",
                "facet_count",
                "fin_count",
                "vertex_count",
                "faces",
                "facets",
                "fins",
            },
            component + f": native tessellation v3 body {index} has an unknown shape",
        )
        body_key = (body["body_type"], body["body_index"])
        _require(
            _integer(body_key[0])
            and body_key[0] in (0, 1)
            and _integer(body_key[1])
            and body_key[1] >= 0
            and body_key in expected_bodies,
            component + f": native tessellation v3 body {index} is unknown",
        )
        _require(
            previous_body is None or body_key > previous_body,
            component + f": native tessellation v3 body {index} is duplicated or unordered",
        )
        previous_body = body_key
        _require(
            body["body_reference_sha256"] == body_references[body_key]
            and body["tessellate_result"] is True
            and _integer(body["facet_count"])
            and body["facet_count"] >= 0
            and _integer(body["fin_count"])
            and body["fin_count"] >= 0
            and _integer(body["vertex_count"])
            and body["vertex_count"] >= 0
            and isinstance(body["faces"], list)
            and isinstance(body["facets"], list)
            and isinstance(body["fins"], list),
            component + f": native tessellation v3 body {body_key} is incomplete",
        )
        body_evidence[body_key] = _parse_itessellation_body(
            component,
            body,
            spans_by_body[body_key],
            face_spans_by_key,
        )
        body_counts[body_key] = {
            "facet_count": body["facet_count"],
            "fin_count": body["fin_count"],
            "vertex_count": body["vertex_count"],
        }
    _require(
        set(body_evidence) == expected_bodies,
        component + ": native tessellation v3 evidence does not cover every body",
    )

    raw_vertices = value["vertices"]
    _require(
        isinstance(raw_vertices, list),
        component + ": native tessellation v3 vertex point cache is missing",
    )
    vertices = {}
    previous_vertex = None
    for index, vertex in enumerate(raw_vertices):
        _require(
            isinstance(vertex, dict)
            and set(vertex) == {"body_type", "body_index", "vertex_id", "point_m"},
            component + f": native tessellation v3 vertex {index} has an unknown shape",
        )
        body_key = (vertex["body_type"], vertex["body_index"])
        vertex_id = vertex["vertex_id"]
        point = vertex["point_m"]
        _require(
            body_key in body_evidence
            and _integer(vertex_id)
            and vertex_id >= 0
            and isinstance(point, list)
            and len(point) == 3
            and all(
                type(coordinate) in (int, float) and math.isfinite(coordinate)
                for coordinate in point
            ),
            component + f": native tessellation v3 vertex {index} is malformed",
        )
        order = (*body_key, vertex_id)
        _require(
            previous_vertex is None or order > previous_vertex,
            component + f": native tessellation v3 vertex {index} is duplicated or unordered",
        )
        previous_vertex = order
        vertices[(body_key, vertex_id)] = tuple(point)

    for body_key, evidence in body_evidence.items():
        _require(
            len(evidence["facets"]) == body_counts[body_key]["facet_count"],
            component + f": native tessellation v3 facet count differs for body {body_key}",
        )
        _require(
            len(evidence["fins"]) == body_counts[body_key]["fin_count"],
            component + f": native tessellation v3 fin count differs for body {body_key}",
        )
        cached = {vertex_id for owner, vertex_id in vertices if owner == body_key}
        referenced = {
            vertex_id
            for fin in evidence["fins"].values()
            for vertex_id in fin["vertex_ids"]
        }
        _require(
            len(cached) == body_counts[body_key]["vertex_count"],
            component + f": native tessellation v3 vertex count differs for body {body_key}",
        )
        _require(
            cached == referenced,
            component + ": native tessellation v3 vertex cache differs from fin vertices",
        )
        cycles = _validate_facet_side_coverage(component, body_key, evidence)
        if triangle_vertices is not None:
            _validate_facet_stl_binding(
                component,
                body_key,
                evidence,
                cycles,
                vertices,
                triangle_vertices,
            )

    edges_by_body = {}
    for edge in native_edges:
        edges_by_body.setdefault((edge["body_type"], edge["body_index"]), {})[
            edge["edge_reference_sha256"]
        ] = edge
    for body_key, evidence in body_evidence.items():
        fins_by_edge_reference = {}
        for fin in evidence["fins"].values():
            reference = fin["edge_reference_sha256"]
            if reference is not None:
                fins_by_edge_reference.setdefault(reference, set()).add(fin["fin_id"])
        body_edges = edges_by_body.get(body_key, {})
        _require(
            set(fins_by_edge_reference) <= set(body_edges),
            component + f": native tessellation v3 edge references differ for body {body_key}",
        )
        for reference, edge in body_edges.items():
            declared = set(edge["fin_ids"])
            actual = fins_by_edge_reference.get(reference, set())
            _require(
                declared == actual,
                component
                + f": native tessellation v3 edge {reference} fin_ids are not bidirectional",
            )
            fin_faces = {
                evidence["fins"][fin_id]["face_index"] for fin_id in actual
            }
            active_faces = {
                face_index
                for face_index in edge["face_indices"]
                if face_spans_by_key[(*body_key, face_index)]["triangle_count"] > 0
            }
            _require(
                fin_faces == active_faces,
                component
                + f": native tessellation v3 edge {reference} does not cover its tessellated faces",
            )
            _require(
                bool(active_faces)
                or all(
                    face_spans_by_key[(*body_key, face_index)]["triangle_count"] == 0
                    for face_index in edge["face_indices"]
                ),
                component
                + f": native tessellation v3 edge {reference} has no fin without an untessellated face",
            )


def _parse_itessellation_body(
    component: str,
    body: dict,
    spans: list[dict],
    face_spans_by_key: dict,
) -> dict:
    body_key = (body["body_type"], body["body_index"])
    face_spans = {span["face_index"]: span for span in spans}
    faces = {}
    previous_face = None
    for index, face in enumerate(body["faces"]):
        _require(
            isinstance(face, dict)
            and set(face) == {"face_index", "facet_ids"},
            component + f": native tessellation v3 face {index} has an unknown shape",
        )
        face_index = face["face_index"]
        facet_ids = face["facet_ids"]
        _require(
            _integer(face_index)
            and face_index in face_spans
            and isinstance(facet_ids, list)
            and facet_ids == sorted(set(facet_ids))
            and all(_integer(facet_id) and facet_id >= 0 for facet_id in facet_ids)
            and (previous_face is None or face_index > previous_face),
            component + f": native tessellation v3 face {index} is malformed",
        )
        previous_face = face_index
        faces[face_index] = set(facet_ids)
    _require(
        set(faces) == set(face_spans),
        component + f": native tessellation v3 body {body_key} does not cover every face",
    )

    facets = {}
    previous_facet = None
    used_triangle_records = set()
    for index, facet in enumerate(body["facets"]):
        _require(
            isinstance(facet, dict)
            and set(facet)
            == {
                "facet_id",
                "face_index",
                "fin_ids",
                "vertex_ids",
                "triangle_record_index",
            },
            component + f": native tessellation v3 facet {index} has an unknown shape",
        )
        facet_id = facet["facet_id"]
        face_index = facet["face_index"]
        fin_ids = facet["fin_ids"]
        vertex_ids = facet["vertex_ids"]
        triangle_record_index = facet["triangle_record_index"]
        _require(
            _integer(facet_id)
            and facet_id >= 0
            and (previous_facet is None or facet_id > previous_facet)
            and face_index in faces
            and facet_id in faces[face_index]
            and isinstance(fin_ids, list)
            and len(fin_ids) == 3
            and fin_ids == sorted(set(fin_ids))
            and all(_integer(fin_id) and fin_id >= 0 for fin_id in fin_ids)
            and isinstance(vertex_ids, list)
            and len(vertex_ids) == 3
            and vertex_ids == sorted(set(vertex_ids))
            and all(_integer(vertex_id) and vertex_id >= 0 for vertex_id in vertex_ids)
            and _integer(triangle_record_index)
            and triangle_record_index >= 0,
            component + f": native tessellation v3 facet {index} is malformed",
        )
        previous_facet = facet_id
        span = face_spans_by_key[(*body_key, face_index)]
        expected_records = range(
            span["triangle_start"],
            span["triangle_start"] + span["triangle_count"],
        )
        _require(
            triangle_record_index in expected_records
            and triangle_record_index not in used_triangle_records,
            component
            + f": native tessellation v3 facet {facet_id} has duplicate or misplaced triangle index",
        )
        used_triangle_records.add(triangle_record_index)
        facets[facet_id] = {
            "facet_id": facet_id,
            "face_index": face_index,
            "fin_ids": tuple(fin_ids),
            "vertex_ids": tuple(vertex_ids),
            "triangle_record_index": triangle_record_index,
        }

    expected_triangle_records = {
        triangle_index
        for span in spans
        for triangle_index in range(
            span["triangle_start"],
            span["triangle_start"] + span["triangle_count"],
        )
    }
    _require(
        used_triangle_records == expected_triangle_records,
        component + f": native tessellation v3 body {body_key} does not cover every triangle record",
    )
    _require(
        {
            facet_id
            for facet_ids in faces.values()
            for facet_id in facet_ids
        }
        == set(facets),
        component + f": native tessellation v3 body {body_key} face-to-facet map is not closed",
    )
    for face_index, facet_ids in faces.items():
        _require(
            facet_ids
            == {
                facet_id
                for facet_id, facet in facets.items()
                if facet["face_index"] == face_index
            },
            component
            + f": native tessellation v3 body {body_key} face {face_index} has misowned facets",
        )

    fins = {}
    previous_fin = None
    facet_fin_membership = {}
    for index, fin in enumerate(body["fins"]):
        _require(
            isinstance(fin, dict)
            and set(fin)
            == {
                "fin_id",
                "facet_id",
                "face_index",
                "vertex_ids",
                "edge_reference_sha256",
                "classification",
            },
            component + f": native tessellation v3 fin {index} has an unknown shape",
        )
        fin_id = fin["fin_id"]
        facet_id = fin["facet_id"]
        face_index = fin["face_index"]
        vertex_ids = fin["vertex_ids"]
        reference = fin["edge_reference_sha256"]
        classification = fin["classification"]
        _require(
            _integer(fin_id)
            and fin_id >= 0
            and (previous_fin is None or fin_id > previous_fin)
            and facet_id in facets
            and face_index == facets[facet_id]["face_index"]
            and isinstance(vertex_ids, list)
            and len(vertex_ids) == 2
            and len(set(vertex_ids)) == 2
            and all(_integer(vertex_id) and vertex_id >= 0 for vertex_id in vertex_ids)
            and (
                (reference is None and classification == "internal_only")
                or (
                    isinstance(reference, str)
                    and bool(HEX64.fullmatch(reference))
                    and classification == "native_edge"
                )
            ),
            component + f": native tessellation v3 fin {index} is malformed or unclassified",
        )
        previous_fin = fin_id
        _require(
            fin_id in facets[facet_id]["fin_ids"],
            component + f": native tessellation v3 fin {fin_id} is absent from its facet",
        )
        previous = facet_fin_membership.setdefault(fin_id, facet_id)
        _require(
            previous == facet_id,
            component + f": native tessellation v3 fin {fin_id} belongs to multiple facets",
        )
        fins[fin_id] = {
            "fin_id": fin_id,
            "facet_id": facet_id,
            "face_index": face_index,
            "vertex_ids": tuple(vertex_ids),
            "edge_reference_sha256": reference,
            "classification": classification,
        }
    _require(
        set(facet_fin_membership)
        == {
            fin_id
            for facet in facets.values()
            for fin_id in facet["fin_ids"]
        },
        component + f": native tessellation v3 body {body_key} facet-to-fin map is not closed",
    )
    return {"faces": faces, "facets": facets, "fins": fins}


def _validate_facet_side_coverage(
    component: str,
    body_key: tuple[int, int],
    evidence: dict,
) -> dict[int, tuple[int, int, int]]:
    """Require each facet's directed fins to form one unique three-cycle."""

    cycles = {}
    for facet in evidence["facets"].values():
        facet_vertices = set(facet["vertex_ids"])
        fin_ids = facet["fin_ids"]
        fins = [evidence["fins"][fin_id] for fin_id in fin_ids]
        starts = [fin["vertex_ids"][0] for fin in fins]
        ends = [fin["vertex_ids"][1] for fin in fins]
        _require(
            set(starts) == facet_vertices
            and set(ends) == facet_vertices
            and len(set(starts)) == 3
            and len(set(ends)) == 3,
            component
            + f": native tessellation v3 facet {facet['facet_id']} is not a directed three-cycle",
        )
        cycle = [fins[0]["vertex_ids"][0]]
        current = fins[0]
        used = set()
        for _ in range(3):
            _require(
                current["fin_id"] not in used,
                component
                + f": native tessellation v3 facet {facet['facet_id']} repeats a directed fin",
            )
            used.add(current["fin_id"])
            cycle.append(current["vertex_ids"][1])
            matches = [
                fin
                for fin in fins
                if fin["vertex_ids"][0] == current["vertex_ids"][1]
            ]
            _require(
                len(matches) == 1,
                component
                + f": native tessellation v3 facet {facet['facet_id']} has an ambiguous directed cycle",
            )
            current = matches[0]
        _require(
            current["fin_id"] == fins[0]["fin_id"]
            and cycle[0] == cycle[-1]
            and len(set(cycle[:-1])) == 3,
            component
            + f": native tessellation v3 facet {facet['facet_id']} does not form one directed three-cycle",
        )
        cycles[facet["facet_id"]] = (cycle[0], cycle[1], cycle[2])
    return cycles


def _float32(value: float) -> float:
    """Apply the manifest's public binary32 rule with round-to-nearest-even."""

    return struct.unpack("<f", struct.pack("<f", value))[0]


def _validate_facet_stl_binding(
    component: str,
    body_key: tuple[int, int],
    evidence: dict,
    cycles: dict[int, tuple[int, int, int]],
    vertices: dict,
    triangle_vertices: list,
) -> None:
    """Bind the directed native facet cycle to its exact binary STL record."""

    for facet in evidence["facets"].values():
        native = [
            tuple(_float32(coordinate) for coordinate in vertices[(body_key, vertex_id)])
            for vertex_id in cycles[facet["facet_id"]]
        ]
        record = triangle_vertices[facet["triangle_record_index"]]
        stl = [tuple(float(coordinate) for coordinate in point) for point in record]
        _require(
            any(native == stl[offset:] + stl[:offset] for offset in range(3)),
            component
            + f": native tessellation v3 facet {facet['facet_id']} does not bind "
            + f"triangle record {facet['triangle_record_index']}",
        )


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
