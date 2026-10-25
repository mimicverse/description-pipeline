"""Shared native edge-fin and cofin topology validation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from .native_u import (
    native_point_error,
    native_point_roundtrip_tolerance,
    native_u_equal,
    native_u_domain_is_resolvable,
    native_u_in_closed_interval,
)


class NativeFinRelationError(ValueError):
    """One reproducible native fin relation refusal."""

    def __init__(self, message, detail):
        super().__init__(message)
        self.message = message
        self.detail = dict(detail)


def _require(condition: bool, message: str, **detail) -> None:
    if not condition:
        raise NativeFinRelationError(message, detail)


def _write_only_sense_error(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("com_error:")
        and "(61836," in value
    )


def validate_native_edge_face_senses(
    *, component: str, face_indices: Sequence[int], face_senses: object,
) -> dict[int, bool]:
    """Validate already observed face orientations before provider selection."""

    face_sense_rows = face_senses
    incidence = len(face_indices)
    _require(
        isinstance(face_sense_rows, list)
        and len(face_sense_rows) == incidence
        and [
            row.get("face_index") if isinstance(row, Mapping) else None
            for row in face_sense_rows
        ]
        == list(face_indices),
        f"{component}: a native edge face-sense inventory is inconsistent",
        component=component,
        face_indices=list(face_indices),
    )
    face_sense = {}
    for row in face_sense_rows:
        _require(
            isinstance(row, Mapping)
            and set(row)
            == {
                "face_index",
                "EdgeInFaceSense",
                "IEdgeInFaceSense2",
            },
            f"{component}: a native edge face-sense row is malformed",
            component=component,
            face_index=row.get("face_index") if isinstance(row, Mapping) else None,
        )
        _require(
            type(row["face_index"]) is int,
            f"{component}: a native edge face-sense row is malformed",
            component=component,
            face_index=row["face_index"],
        )
        primary = row["EdgeInFaceSense"]
        _require(
            isinstance(primary, Mapping)
            and set(primary) == {"value", "error"}
            and primary["error"] is None
            and type(primary["value"]) is bool,
            f"{component}: a native edge primary face sense is unreadable",
            component=component,
            face_index=row["face_index"],
        )
        secondary = row["IEdgeInFaceSense2"]
        _require(
            isinstance(secondary, Mapping)
            and set(secondary) == {"value", "error"}
            and (
                (
                    secondary["error"] is None
                    and type(secondary["value"]) is bool
                    and secondary["value"] is primary["value"]
                )
                or (
                    _write_only_sense_error(secondary["error"])
                    and secondary["value"] is None
                )
            ),
            f"{component}: a native edge secondary face sense is invalid",
            component=component,
            face_index=row["face_index"],
        )
        face_sense[row["face_index"]] = primary["value"]

    values = list(face_sense.values())
    _require(
        len(values) == 1 or (len(values) == 2 and values[0] is not values[1]),
        f"{component}: a native edge cofaces have the same orientation",
        component=component,
        face_indices=list(face_indices),
    )
    return face_sense


def validate_native_edge_fin_relations(
    *,
    component: str,
    edge: Mapping,
    edge_fins: Sequence[int],
    incident_face_facet_counts: Sequence[int],
    fins_by_id: Mapping[int, Mapping],
    vertices_by_id: Mapping[int, Sequence[float]],
) -> dict:
    """Validate the fin set, including an explicitly untessellated sheet edge."""

    _require(
        type(edge["body_type"]) is int
        and edge["body_type"] in (0, 1),
        f"{component}: a native edge has an invalid body type",
        component=component,
        body_type=edge["body_type"],
    )
    fin_ids = tuple(edge_fins)
    _require(
        all(type(fin_id) is int and fin_id >= 0 for fin_id in fin_ids),
        f"{component}: a native edge fin set has an invalid ID type",
        component=component,
        fin_ids=list(fin_ids),
    )
    normalized_ids = tuple(sorted(set(fin_ids)))
    _require(
        len(fin_ids) == len(normalized_ids),
        f"{component}: a native edge repeats a fin ID",
        component=component,
        fin_ids=list(fin_ids),
    )
    declared = tuple(edge["fin_ids"])
    _require(
        all(type(fin_id) is int and fin_id >= 0 for fin_id in declared)
        and len(declared) == len(set(declared))
        and declared == normalized_ids,
        f"{component}: native edge fin_ids are not bidirectional",
        component=component,
        declared_fin_ids=list(declared),
        actual_fin_ids=list(normalized_ids),
    )
    face_indices = tuple(edge["face_indices"])
    incidence = len(face_indices)
    _require(
        incidence in {1, 2}
        and all(type(value) is int and value >= 0 for value in face_indices)
        and face_indices == tuple(sorted(set(face_indices))),
        f"{component}: a native edge has non-manifold face incidence",
        component=component,
        face_indices=list(face_indices),
    )
    facet_counts = tuple(incident_face_facet_counts)
    _require(
        len(facet_counts) == incidence
        and all(type(count) is int and count >= 0 for count in facet_counts),
        f"{component}: a native edge has invalid incident face facet counts",
        component=component,
        face_indices=list(face_indices),
        incident_face_facet_counts=list(facet_counts),
    )
    _require(
        bool(normalized_ids)
        or (
            edge["body_type"] == 1
            and all(count == 0 for count in facet_counts)
        ),
        f"{component}: a native edge did not return any fins",
        component=component,
        fin_ids=list(fin_ids),
        face_indices=list(face_indices),
        incident_face_facet_counts=list(facet_counts),
    )

    face_sense = validate_native_edge_face_senses(
        component=component, face_indices=face_indices, face_senses=edge["face_senses"],
    )

    rows = {}
    for fin_id in normalized_ids:
        fin = fins_by_id.get(fin_id)
        _require(
            isinstance(fin, Mapping),
            f"{component}: a native edge names a missing fin",
            component=component,
            fin_id=fin_id,
        )
        face_index = fin["face_index"]
        facet_id = fin.get("facet_id")
        raw_cofin = fin["cofin_raw_id"]
        cofin_id = fin["cofin_id"]
        _require(
            type(face_index) is int and face_index >= 0,
            f"{component}: a native fin has an invalid face identity",
            component=component,
            fin_id=fin_id,
            face_index=face_index,
        )
        _require(
            facet_id is None
            or (type(facet_id) is int and facet_id >= 0),
            f"{component}: a native fin has an invalid facet identity",
            component=component,
            fin_id=fin_id,
            facet_id=facet_id,
        )
        _require(
            face_index in face_indices,
            f"{component}: a native edge fin names an unrelated face",
            component=component,
            fin_id=fin_id,
            face_index=face_index,
            face_indices=list(face_indices),
        )
        _require(
            type(raw_cofin) is int and raw_cofin >= -1,
            f"{component}: GetFinCoFin returned an invalid raw value",
            component=component,
            fin_id=fin_id,
            cofin_raw_id=raw_cofin,
        )
        _require(
            (
                raw_cofin == -1
                and cofin_id is None
            )
            or (
                raw_cofin >= 0
                and type(cofin_id) is int
                and cofin_id == raw_cofin
            ),
            f"{component}: native cofin raw and normalized IDs differ",
            component=component,
            fin_id=fin_id,
            cofin_raw_id=raw_cofin,
            cofin_id=cofin_id,
        )
        rows[fin_id] = fin

    if incidence == 1:
        _require(
            edge["body_type"] == 1,
            f"{component}: an open edge is valid only on a sheet body",
            component=component,
            body_type=edge["body_type"],
        )
        unique_face = face_indices[0]
        for fin_id, fin in rows.items():
            _require(
                fin["face_index"] == unique_face,
                f"{component}: an open boundary fin names another face",
                component=component,
                fin_id=fin_id,
                face_index=fin["face_index"],
                unique_face=unique_face,
            )
            _require(
                fin["cofin_raw_id"] == -1 and fin["cofin_id"] is None,
                f"{component}: an open edge contains a non-boundary fin",
                component=component,
                fin_id=fin_id,
                cofin_raw_id=fin["cofin_raw_id"],
                cofin_id=fin["cofin_id"],
            )
        return {"fin_ids": normalized_ids, "collapsed_pairs": []}

    for fin_id, fin in rows.items():
        cofin_id = fin["cofin_id"]
        _require(
            fin["cofin_raw_id"] >= 0,
            f"{component}: a shared edge contains a boundary fin",
            component=component,
            fin_id=fin_id,
        )
        _require(
            cofin_id in rows and cofin_id != fin_id,
            f"{component}: a shared-edge cofin is outside the same edge",
            component=component,
            fin_id=fin_id,
            cofin_id=cofin_id,
            fin_ids=list(normalized_ids),
        )

    collapsed_pairs = []
    for fin_id in normalized_ids:
        fin = rows[fin_id]
        cofin_id = fin["cofin_id"]
        cofin = rows[cofin_id]
        _require(
            cofin["cofin_id"] == fin_id
            and cofin["cofin_raw_id"] == fin_id
            and fin["cofin_raw_id"] == cofin_id,
            f"{component}: native cofins are not reciprocal",
            component=component,
            fin_id=fin_id,
            cofin_id=cofin_id,
        )
        _require(
            cofin["face_index"] != fin["face_index"],
            f"{component}: reciprocal cofins name the same face",
            component=component,
            fin_id=fin_id,
            cofin_id=cofin_id,
            face_index=fin["face_index"],
        )
        _require(
            cofin["face_index"] in face_sense
            and fin["face_index"] in face_sense
            and face_sense[fin["face_index"]]
            is not face_sense[cofin["face_index"]],
            f"{component}: reciprocal cofins have inconsistent face sense",
            component=component,
            fin_id=fin_id,
            cofin_id=cofin_id,
        )
        for name in ("start_u", "end_u"):
            _require(
                type(fin[name]) in (int, float)
                and math.isfinite(fin[name])
                and type(cofin[name]) in (int, float)
                and math.isfinite(cofin[name]),
                f"{component}: reciprocal cofins have invalid U values",
                component=component,
                fin_id=fin_id,
                cofin_id=cofin_id,
            )
        fin_collapsed = native_u_equal(fin["start_u"], fin["end_u"])
        cofin_collapsed = native_u_equal(cofin["start_u"], cofin["end_u"])
        _require(
            fin_collapsed is cofin_collapsed,
            f"{component}: reciprocal cofins have partial U collapse",
            component=component,
            fin_id=fin_id,
            cofin_id=cofin_id,
        )
        if fin_collapsed:
            _require(
                native_u_equal(fin["start_u"], fin["end_u"])
                and native_u_equal(cofin["start_u"], cofin["end_u"])
                and native_u_equal(fin["start_u"], cofin["start_u"]),
                f"{component}: a collapsed cofin pair has inconsistent U",
                component=component,
                fin_id=fin_id,
                cofin_id=cofin_id,
            )
            collapsed_pairs.append(tuple(sorted((fin_id, cofin_id))))
        else:
            _require(
                native_u_equal(fin["start_u"], cofin["end_u"])
                and native_u_equal(fin["end_u"], cofin["start_u"])
                and (
                    (fin["end_u"] - fin["start_u"])
                    * (cofin["end_u"] - cofin["start_u"])
                    < 0.0
                ),
                f"{component}: reciprocal cofin U intervals do not correspond",
                component=component,
                fin_id=fin_id,
                cofin_id=cofin_id,
            )
        fin_start = vertices_by_id[fin["vertex_ids"][0]]
        fin_end = vertices_by_id[fin["vertex_ids"][1]]
        cofin_start = vertices_by_id[cofin["vertex_ids"][0]]
        cofin_end = vertices_by_id[cofin["vertex_ids"][1]]
        _require(
            native_point_error(fin_start, cofin_end)
            <= native_point_roundtrip_tolerance(fin_start, cofin_end)
            and native_point_error(fin_end, cofin_start)
            <= native_point_roundtrip_tolerance(fin_end, cofin_start),
            f"{component}: reciprocal cofin endpoints do not correspond",
            component=component,
            fin_id=fin_id,
            cofin_id=cofin_id,
        )
    return {
        "fin_ids": normalized_ids,
        "collapsed_pairs": sorted(set(collapsed_pairs)),
    }


def closed_edge_fin_partition(*, component: str, fins: Mapping[int, Mapping], lower: float, upper: float) -> dict:
    """Derive a closed edge partition from its complete undirected fin cycle.

    Parameter adjacency, not U length or an assumed raw fin orientation,
    identifies each segment. The two domain endpoints represent one seam node.
    Topological vertices remain distinct even when their parameters collapse.
    """
    _require(native_u_domain_is_resolvable(lower, upper),
             f"{component}: closed edge parameter domain is unresolved")
    adjacency = {}
    parameters = {}
    for fin_id, row in sorted(fins.items()):
        pair = row["vertex_ids"]
        _require(len(pair) == 2 and pair[0] != pair[1],
                 f"{component}: closed edge fin has invalid vertices", fin_id=fin_id)
        for vertex, raw_u in zip(pair, (row["start_u"], row["end_u"]), strict=True):
            _require(native_u_in_closed_interval(raw_u, lower, upper) and lower <= raw_u <= upper,
                     f"{component}: closed edge fin parameter is outside its domain", fin_id=fin_id)
            value = lower if native_u_equal(raw_u, upper) or native_u_equal(raw_u, lower) else raw_u
            observations = parameters.setdefault(vertex, [])
            _require(all(native_u_equal(value, previous) for previous in observations),
                     f"{component}: one closed edge vertex has conflicting parameters", vertex_id=vertex)
            observations.append(value)
            adjacency.setdefault(vertex, []).append(fin_id)
    _require(len(adjacency) >= 3 and all(len(rows) == 2 for rows in adjacency.values()),
             f"{component}: closed edge fins do not form an unambiguous cycle")
    first = min(adjacency)
    vertex_cycle = [first]
    fin_cycle = []
    vertex = first
    while len(fin_cycle) < len(fins):
        choices = sorted(fin for fin in adjacency[vertex] if fin not in fin_cycle)
        _require(bool(choices), f"{component}: closed edge fin cycle is disconnected")
        fin_id = choices[0]
        fin_cycle.append(fin_id)
        pair = fins[fin_id]["vertex_ids"]
        vertex = pair[1] if pair[0] == vertex else pair[0]
        vertex_cycle.append(vertex)
        _require(vertex == first or vertex not in vertex_cycle[:-1],
                 f"{component}: closed edge fin cycle repeats a vertex")
        if vertex == first:
            break
    _require(vertex == first and len(fin_cycle) == len(fins),
             f"{component}: closed edge fins contain disconnected cycles")

    values = sorted({value for rows in parameters.values() for value in rows})
    groups = []
    for value in values:
        if not groups or not native_u_equal(value, groups[-1][-1]):
            groups.append([value])
        else:
            _require(all(native_u_equal(value, previous) for previous in groups[-1]),
                     f"{component}: closed edge parameter identity is non-transitive")
            groups[-1].append(value)
    nodes = [group[0] for group in groups]
    _require(len(nodes) >= 3, f"{component}: closed edge has insufficient unique parameter nodes")
    node_by_value = {value: index for index, group in enumerate(groups) for value in group}
    node_by_vertex = {}
    for vertex, observations in parameters.items():
        matches = {node_by_value[value] for value in observations}
        _require(len(matches) == 1, f"{component}: closed edge parameter identity is ambiguous", vertex_id=vertex)
        node_by_vertex[vertex] = matches.pop()
    owners = {}
    segments = []
    for fin_id, row in sorted(fins.items()):
        start, end = (node_by_vertex[vertex] for vertex in row["vertex_ids"])
        _require((start == end) == native_u_equal(row["start_u"], row["end_u"]),
                 f"{component}: closed edge parameter identity changes a fin's nonzero span", fin_id=fin_id)
        if start == end:
            segments.append({"fin_id": fin_id, "intervals": []})
            continue
        if (start + 1) % len(nodes) == end:
            segment = start
        elif (end + 1) % len(nodes) == start:
            segment = end
        else:
            raise NativeFinRelationError(f"{component}: closed edge fin skips a parameter node", {"fin_id": fin_id})
        _require(segment not in owners, f"{component}: closed edge parameter segment is covered more than once",
                 fin_id=fin_id)
        owners[segment] = fin_id
        if segment + 1 < len(nodes):
            intervals = [[nodes[segment], nodes[segment + 1]]]
        else:
            intervals = [[nodes[-1], upper]]
            if nodes[0] > lower:
                intervals.append([lower, nodes[0]])
        segments.append({"fin_id": fin_id, "intervals": intervals})
    _require(len(owners) == len(nodes), f"{component}: closed edge parameter partition is incomplete")
    return {
        "rule_version": "native-closed-edge-fin-partition-v1",
        "domain": [lower, upper],
        "vertex_cycle": vertex_cycle,
        "fin_cycle": fin_cycle,
        "parameter_node_count": len(nodes),
        "segments": segments,
    }
