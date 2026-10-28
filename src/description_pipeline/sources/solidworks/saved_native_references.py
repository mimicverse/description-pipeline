"""Bind saved native reference features for inspection, without mechanical claims.

The caller supplies the locally projected native record. This module proves
occurrences and geometry before returning an in-memory binding; it never reads
historical exports, writes CAD, or persists a second engineering model.
"""

from __future__ import annotations

import math
import ntpath
import re

from ...io import PipelineError
from ...schemas import (
    SAVED_NATIVE_REFERENCE_RULE as RULE_ID,
    SAVED_NATIVE_REFERENCE_POSITION_TOL_M as LINE_TOLERANCE_M,
    SAVED_NATIVE_REFERENCE_DOT_TOL as AXIS_DOT_TOLERANCE,
)
from .naming import token


class SavedNativeReferenceError(PipelineError):
    def __init__(self, message: str, **detail):
        super().__init__(message)
        self.details = detail


def _require(condition, message, **detail):
    if not condition:
        raise SavedNativeReferenceError(message, **detail)


def _vector(value, length):
    _require(
        isinstance(value, list) and len(value) == length
        and all(type(x) in (int, float) and math.isfinite(x) for x in value),
        "Native geometry requires a finite numeric array", length=length,
    )
    return [float(x) for x in value]


def _unit(value):
    vector = _vector(value, 3)
    length = math.hypot(*vector)
    _require(length > 0, "Native axis is degenerate")
    return [x / length for x in vector]


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right, strict=True))


def _line_distance(point, origin, direction):
    delta = [a - b for a, b in zip(point, origin, strict=True)]
    projection = _dot(delta, direction)
    return math.hypot(*(x - projection * d for x, d in zip(delta, direction, strict=True)))


def _document_key(value):
    return ntpath.normcase(ntpath.normpath(str(value).replace("/", "\\")))


def _semantic_token(value):
    try:
        return token(value)
    except ValueError as error:
        raise SavedNativeReferenceError(
            "Native reference feature cannot form a legal semantic identity", native_name=value,
        ) from error


def _component_tokens(component):
    # Revision dates and occurrence indices select name candidates only. A
    # candidate must independently match both current native interface axes.
    names = [ntpath.splitext(ntpath.basename(component["document"]))[0], component["name2"].rsplit("/", 1)[-1]]
    tokens = set()
    for name in names:
        name = re.sub(r"-\d+$", "", name)
        name = re.sub(r"_\d{8}$", "", name)
        try:
            tokens.add(token(name))
        except ValueError:
            continue
    return tokens


def _axial_reference(entity):
    reference = entity.get("mate_entity_reference")
    _require(isinstance(reference, dict), "Concentric endpoint has no native parameters")
    kind = reference.get("reference_type")
    _require(
        type(kind) is int and kind in {2, 4, 7}
        and reference.get("source") == "mate-entity-params"
        and reference.get("space") == "mate-assembly"
        and reference.get("error") is None,
        "Concentric endpoint is not a readable native line, cylinder or circle", reference_type=kind,
    )
    values = reference.get("params")
    _require(isinstance(values, list), "Concentric endpoint parameters are unreadable")
    values = _vector(values, len(values))
    _require(len(values) >= (6 if kind == 2 else 7), "Concentric endpoint parameters are incomplete")
    if kind in {4, 7}:
        _require(values[6] > 0, "Native axial reference radius must be positive")
    return values[:3], _unit(values[3:6])


def _unique_assignment(candidates, tokens):
    pending = {child: set(values) for child, values in candidates.items()}
    result = {}
    while pending:
        _require(all(pending.values()), "A native interface has no remaining feature binding")
        forced = {child: next(iter(values)) for child, values in pending.items() if len(values) == 1}
        for feature in tokens - set(result.values()):
            owners = [child for child, values in pending.items() if feature in values]
            _require(owners, "A native feature pair is unbound", feature=feature)
            if len(owners) == 1:
                child = owners[0]
                _require(
                    child not in forced or forced[child] == feature,
                    "Several feature pairs bind the same native interface", component=child,
                )
                forced[child] = feature
        _require(forced, "Native feature binding is ambiguous", candidates={k: sorted(v) for k, v in pending.items()})
        _require(len(set(forced.values())) == len(forced), "Several native interfaces bind the same feature pair")
        result.update(forced)
        for child in forced:
            pending.pop(child)
        for values in pending.values():
            values.difference_update(forced.values())
    _require(set(result.values()) == tokens, "Native reference features are not covered exactly once")
    return result


def bind_saved_native_references(record: dict, *, purpose: str) -> dict:
    """Prove one saved inspection declaration from current native facts alone."""

    _require(purpose == "inspection_preview", "Saved native carriers require the frozen inspection purpose")
    rows = record.get("components")
    _require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows),
             "Native component inventory is unreadable")
    for index, row in enumerate(rows):
        _require(type(row.get("suppressed")) is bool, "Native suppression state is unreadable", index=index)
        if not row["suppressed"]:
            _require(all(isinstance(row.get(key), str) and bool(row[key].strip())
                         for key in ("name2", "document", "configuration"))
                     and isinstance(row.get("parent_occurrence"), str),
                     "An active native occurrence lacks its document, identity or configuration", index=index)
    components = {row["name2"]: row for row in rows if row.get("suppressed") is False}
    _require(len(components) == sum(row["suppressed"] is False for row in rows),
             "Native occurrence identities are duplicated")
    roots = {name: row for name, row in components.items() if row["parent_occurrence"] == ""}
    _require(roots, "The saved native declaration has no active root occurrences")
    identity = record.get("identity")
    configuration = record.get("saved_configuration")
    main = identity.get("main_assembly") if isinstance(identity, dict) else None
    _require(isinstance(configuration, str) and bool(configuration) and isinstance(main, str) and bool(main),
             "Native document and saved configuration are unreadable")
    exporter = record.get("exporter")
    _require(
        isinstance(exporter, dict) and exporter.get("state") == "absent"
        and exporter.get("configuration") == configuration
        and _document_key(exporter.get("document")) == _document_key(main)
        and exporter.get("error") is None and exporter.get("raw_xml") in (None, "")
        and exporter.get("registration") in (None, {}) and exporter.get("pid_bindings") in (None, [])
        and all(value is None for key, value in exporter.items() if key.endswith("_error")),
        "Saved native references require an explicitly absent exporter in the same native context",
    )
    for name, row in components.items():
        parent = row["parent_occurrence"]
        _require(not parent or parent in components, "An active native occurrence has no active parent", component=name)
        observation = row.get("solver_observation")
        _require(isinstance(observation, dict), "Native solver observation is unreadable", component=name)
        context = observation.get("context")
        defining = components[parent]["document"] if parent else main
        defining_configuration = components[parent]["configuration"] if parent else configuration
        _require(
            isinstance(context, dict) and context.get("scope") == parent
            and context.get("configuration") == defining_configuration
            and _document_key(context.get("document")) == _document_key(defining),
            "The native occurrence has inconsistent defining context", component=name,
        )
    for name, row in components.items():
        children = [item for item in components.values() if item["parent_occurrence"] == name]
        if row["document"].lower().endswith(".sldasm"):
            observation = row["solver_observation"].get("solving")
            _require(
                children and isinstance(observation, dict)
                and {"method", "family", "value", "error"} <= observation.keys()
                and observation.get("method") == "IComponent2.Solving"
                and observation.get("family") == "swComponentSolvingOption_e"
                and type(observation.get("value")) is int and observation["value"] == 0
                and observation.get("error") is None,
                "A native assembly carrier lacks its context-bound rigid solve declaration", component=name,
            )
        else:
            _require(not children, "A native part unexpectedly contains occurrence children", component=name)
    for name, row in roots.items():
        _require(type(row.get("fixed")) is bool and row.get("fixed_error") is None,
                 "The saved fixed-root observation is unreadable", component=name)
    fixed = [name for name, row in roots.items() if row["fixed"] is True]
    _require(len(fixed) == 1, "The saved inspection declaration requires one explicit fixed root", roots=fixed)
    root = fixed[0]
    owner = {}
    members = {name: [] for name in roots}
    for name in components:
        current = name
        while components[current]["parent_occurrence"]:
            current = components[current]["parent_occurrence"]
        _require(current in roots, "An occurrence has no declared root carrier", component=name)
        owner[name] = current
        members[current].append(name)

    coordinates, axes = {}, {}
    for row in record.get("datums") or []:
        _require(isinstance(row, dict) and isinstance(row.get("name"), str) and bool(row["name"]),
                 "A native coordinate system has no readable identity")
        if not row["name"].startswith("c_"):
            continue
        key = _semantic_token(row["name"][2:])
        _require(key not in coordinates and row.get("owner") == "" and row.get("configuration") == configuration,
                 "A native coordinate pair is duplicated or belongs to another scope", feature=row["name"])
        _require(
            isinstance(row.get("references"), dict)
            and row["references"].get("context") == {"document": main, "configuration": configuration, "scope": ""}
            and row.get("context", {"document": main, "configuration": configuration, "scope": ""})
            == {"document": main, "configuration": configuration, "scope": ""}
            and row.get("error") is None
            and all(value is None for field, value in row.items() if field.endswith("_error")),
            "A native coordinate pair has a conflicting context or read error", feature=row["name"],
        )
        _vector(row.get("array"), 16)
        coordinates[key] = row
    for row in record.get("reference_axes") or []:
        _require(isinstance(row, dict) and isinstance(row.get("selector"), dict),
                 "A native reference axis has no readable selector")
        selector = row.get("selector") or {}
        feature = selector.get("feature_name")
        if not isinstance(feature, str) or not feature.startswith("a_"):
            continue
        key = _semantic_token(feature[2:])
        _require(key not in axes and selector.get("component") == "" and row.get("component") == ""
                 and selector.get("kind") == "reference_axis" and row.get("kind") == "reference_axis"
                 and row.get("coordinate_frame") == "assembly"
                 and row.get("used_api") == "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams"
                 and row.get("direction_semantics") == "undirected_axis_line"
                 and row.get("configuration", configuration) == configuration
                 and row.get("context", {"document": main, "configuration": configuration, "scope": ""})
                 == {"document": main, "configuration": configuration, "scope": ""}
                 and row.get("error") is None
                 and all(value is None for field, value in row.items() if field.endswith("_error")),
                 "A native axis pair is duplicated or belongs to another scope", feature=feature)
        endpoints = _vector(row.get("raw_endpoints_m"), 6)
        direction = _unit([endpoints[i + 3] - endpoints[i] for i in range(3)])
        _require(abs(abs(_dot(direction, _unit(row.get("axis_direction")))) - 1) <= AXIS_DOT_TOLERANCE
                 and _line_distance(_vector(row.get("axis_point_m"), 3), endpoints[:3], direction) <= LINE_TOLERANCE_M,
                 "Native axis endpoints contradict the recorded line", feature=feature)
        axes[key] = {"record": row, "point": endpoints[:3], "direction": direction}
    _require(set(coordinates) == set(axes) and "base_link" in coordinates,
             "The saved native declaration has missing or partial coordinate/axis pairs")
    _require(len(coordinates) == len(roots), "Native feature pairs do not cover the declared rigid carriers")
    for key, coordinate in coordinates.items():
        matrix = coordinate["array"]
        _require(_line_distance([matrix[3], matrix[7], matrix[11]], axes[key]["point"], axes[key]["direction"])
                 <= LINE_TOLERANCE_M, "A native coordinate origin misses its paired reference axis", feature=key)

    edges = {}
    pose_mates = []
    mate_identities = set()
    for mate in record["mates"]:
        if mate.get("suppressed") is True:
            continue
        _require(mate.get("suppressed") is False and type(mate.get("error_code")) is int
                 and mate["error_code"] == 0, "A saved mate has an unreadable or erroneous native state",
                 mate=mate.get("name"))
        identity = (mate.get("scope"), mate.get("name"))
        _require(all(isinstance(value, str) for value in identity) and bool(identity[1])
                 and identity not in mate_identities
                 and isinstance(mate.get("type"), str) and bool(mate["type"])
                 and isinstance(mate.get("configuration"), str) and bool(mate["configuration"]),
                 "A saved mate has a missing or duplicate scoped identity", mate=mate.get("name"))
        mate_identities.add(identity)
        if mate["type"] == "inplace":
            _require(
                mate.get("native_feature_type") == "MateInPlace" and mate.get("native_interface") == "IMateInPlace"
                and isinstance(mate.get("inplace"), dict)
                and (identity[0] == "" or identity[0] in components)
                and mate["configuration"]
                == (components[identity[0]]["configuration"] if identity[0] else configuration),
                "An in-place observation lacks its exact native owner context", mate=mate["name"],
            )
            pose_mates.append({"mate": mate, "owners": [owner[identity[0]]] if identity[0] else []})
            continue
        entities = mate.get("entities")
        _require(isinstance(entities, list) and bool(entities)
                 and type(mate.get("entity_count")) is int and mate["entity_count"] == len(entities)
                 and all(isinstance(entity, dict) and type(entity.get("entity_index")) is int
                         and entity["entity_index"] == index
                         and isinstance(entity.get("mate_entity_reference"), dict)
                         and entity["mate_entity_reference"].get("error") is None
                         for index, entity in enumerate(entities)),
                 "A saved mate has unreadable native endpoints", mate=mate.get("name"))
        owners = set()
        for entity in entities:
            component = entity.get("component")
            if component == "" and entity.get("assembly_frame") is True:
                continue
            _require(isinstance(component, str) and component in owner,
                     "A saved mate endpoint has no bound occurrence", mate=mate.get("name"))
            owners.add(owner[component])
        _require(len(owners) <= 2, "A saved mate crosses several declared carrier interfaces", mate=mate.get("name"))
        if mate.get("type") == "concentric" and mate.get("scope") == "" and len(owners) == 2:
            _require(len(entities) == 2 and type(mate.get("entity_count")) is int
                     and mate["entity_count"] == 2 and mate.get("lock_rotation") is False
                     and mate.get("lock_rotation_api") == "IConcentricMateFeatureData.LockRotation"
                     and all((isinstance(entity.get("feature"), str) and bool(entity["feature"]))
                             or (type(entity.get("face_index")) is int and entity["face_index"] >= 0)
                             for entity in entities),
                     "A concentric interface is incomplete or explicitly rotation-locked", mate=mate.get("name"))
            key = tuple(sorted(owners))
            edges.setdefault(key, []).append({"mate": mate, "lines": [_axial_reference(e) for e in entities]})
        else:
            pose_mates.append({"mate": mate, "owners": sorted(owners)})
    _require(len(edges) == len(roots) - 1, "The saved concentric interfaces do not form a complete tree")
    parent = {root: None}
    frontier = [root]
    while frontier:
        current = frontier.pop()
        for left, right in sorted(edges):
            if current not in (left, right):
                continue
            child = right if current == left else left
            if child == parent[current]:
                continue
            _require(child not in parent, "The saved concentric interface graph contains a cycle")
            parent[child] = current
            frontier.append(child)
    _require(set(parent) == set(roots), "The saved concentric interface graph is disconnected")
    for item in pose_mates:
        _require(len(item["owners"]) != 2 or tuple(item["owners"]) in edges,
                 "A saved pose mate contradicts the declared interface topology", mate=item["mate"].get("name"))
    candidates = {}
    for child, ancestor in parent.items():
        if ancestor is None:
            continue
        witnesses = edges[tuple(sorted((child, ancestor)))]
        matches = set()
        for key in set(coordinates) - {"base_link"}:
            axis = axes[key]
            if all(abs(abs(_dot(direction, axis["direction"])) - 1) <= AXIS_DOT_TOLERANCE
                   and _line_distance(point, axis["point"], axis["direction"]) <= LINE_TOLERANCE_M
                   for witness in witnesses for point, direction in witness["lines"]):
                matches.add(key)
        named_candidates = {key for key in _component_tokens(roots[child]) & matches if not key.startswith("auto_")}
        candidates[child] = named_candidates or matches
    assignments = {root: "base_link", **_unique_assignment(candidates, set(coordinates) - {"base_link"})}
    return {"root": root, "roots": roots, "components": components, "members": members, "owner": owner,
            "parent": parent, "features": assignments, "coordinates": coordinates, "axes": axes,
            "edges": edges, "pose_mates": pose_mates, "rule_id": RULE_ID}
