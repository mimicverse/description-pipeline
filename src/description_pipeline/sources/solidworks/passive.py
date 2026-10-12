"""Portable completion of an undeclared, exactly one-axis passive CAD leaf.

This recognizes only coaxial circle/cylinder and perpendicular plane mates.
It never locks an unconstrained rotation or supplies a drive specification.
"""

from __future__ import annotations

import hashlib
import math

from ...io import PipelineError


def _key(mate):
    return str(mate.get("scope") or ""), str(mate.get("name") or "")


def _geometry(entity, frames, shape):
    from .discovery import _entity_geometry_view, _point, _unit, _vector

    diagnostics = []
    view = _entity_geometry_view(entity, diagnostics, "passive")
    if view is None or diagnostics:
        raise PipelineError("Passive mate has invalid native entity evidence")
    geometry = view.get(shape)
    frame = frames.get(entity.get("component"))
    if not isinstance(geometry, dict) or frame is None:
        raise PipelineError("Passive mate lacks its recorded native geometry or placement")
    if shape in {"circle", "cylinder"} and (
        type(geometry.get("radius")) not in (int, float)
        or not math.isfinite(geometry["radius"]) or geometry["radius"] <= 0
    ):
        raise PipelineError("Passive mate lacks a finite positive native radius")
    point = geometry.get("center") if shape == "circle" else geometry.get("point")
    direction = geometry.get("direction") if shape == "cylinder" else geometry.get("normal")
    if not isinstance(point, (list, tuple)) or len(point) != 3:
        raise PipelineError("Passive mate lacks a native point")
    if not isinstance(direction, (list, tuple)) or len(direction) != 3:
        raise PipelineError("Passive mate lacks a native direction")
    point = [float(v) for v in point]
    local_direction = _unit([float(v) for v in direction])
    if local_direction is None or not all(math.isfinite(v) for v in point):
        raise PipelineError("Passive mate has nonfinite or degenerate geometry")
    return point, local_direction, _point(point, frame), _unit(_vector(local_direction, frame))


def _axis(entity, frames):
    if isinstance(entity.get("circle"), dict):
        return _geometry(entity, frames, "circle")
    if isinstance(entity.get("cylinder"), dict):
        return _geometry(entity, frames, "cylinder")
    raise PipelineError("Passive concentric completion requires native circles or cylinders")


def resolve_mate_frame(record, selector):
    """Resolve an exact selector to a frame from the leaf placement and axis point."""
    from .discovery import _component_frames, _mate_entities

    component = selector.get("component")
    frames = _component_frames(record, [])
    frame = frames.get(component)
    if selector.get("kind") != "mate_axis" or frame is None:
        raise PipelineError("Passive frame has no captured component placement")
    names = [(item.get("scope"), item.get("name")) for item in selector.get("mates", [])]
    if not names or names != sorted(set(names)):
        raise PipelineError("Passive mate selector must be a sorted unique exact scope/name set")
    mates = []
    for key in names:
        matches = [mate for mate in record.get("mates", []) if _key(mate) == key]
        if len(matches) != 1:
            raise PipelineError("Passive mate selector does not resolve uniquely")
        mates.append(matches[0])
    for mate in mates:
        if str(mate.get("type") or "").lower() != "concentric":
            continue
        entries = [entity for entity in _mate_entities(mate) if entity.get("component") == component]
        if len(entries) != 1:
            raise PipelineError("Passive axis does not name exactly one leaf entity")
        local_point, local_axis, point, direction = _axis(entries[0], frames)
        result = [list(row) for row in frame]
        for index in range(3):
            result[index][3] = point[index]
        return {
            "matrix": [value for row in result for value in row],
            "local_point": local_point,
            "local_axis": local_axis,
            "point": point,
            "direction": direction,
        }
    raise PipelineError("Passive frame selector contains no native concentric axis")


def complete_passive_leaves(record, assigned, reserved_names):
    """Return proven passive leaves; unsupported/ambiguous leaves stay uncovered."""
    from .discovery import (
        AXIS_OFFSET_TOL_M, _component_frames, _cross, _dot, _mate_entities, _mate_rows, _rank,
    )

    components = {
        row["name2"]: row for row in record.get("components", [])
        if isinstance(row, dict) and isinstance(row.get("name2"), str) and row.get("suppressed") is False
    }
    frames = _component_frames(record, [])
    results = []
    for component, item in sorted(components.items()):
        if component in assigned or item.get("fixed") is not False:
            continue
        if str(item.get("document") or "").lower().endswith(".sldasm"):
            continue
        if any(name.startswith(component + "/") for name in components):
            continue
        mates = [
            mate for mate in record.get("mates", []) if isinstance(mate, dict)
            and mate.get("suppressed") is not True
            and any(entity.get("component") == component for entity in _mate_entities(mate))
        ]
        if not mates:
            continue
        try:
            keys = [_key(mate) for mate in mates]
            if any(not name for _, name in keys) or len(keys) != len(set(keys)):
                raise PipelineError("Passive completion has ambiguous mate identities")
            selector = {
                "kind": "mate_axis", "component": component,
                "mates": [{"scope": scope, "name": name} for scope, name in sorted(keys)],
            }
            axis = resolve_mate_frame(record, selector)
            point, direction = axis["point"], axis["direction"]
            rows, owners, kinds = [], set(), set()
            for mate in mates:
                kind = str(mate.get("type") or "").lower()
                entities = _mate_entities(mate)
                if (kind not in {"concentric", "coincident"} or len(entities) != 2
                        or mate.get("suppressed") is not False or type(mate.get("error_code")) is not int
                        or mate["error_code"] != 0 or mate.get("limits") is not None
                        or not isinstance(mate.get("scope"), str) or not isinstance(mate.get("name"), str)):
                    raise PipelineError("Passive completion has unsupported or unresolved active mates")
                peers = [entity.get("component") for entity in entities if entity.get("component") != component]
                if len(peers) != 1 or peers[0] not in assigned:
                    raise PipelineError("Passive completion has an unassigned peer")
                owners.add(assigned[peers[0]])
                diagnostics = []
                view = _mate_rows(mate, frames, diagnostics, "passive")
                if view is None or diagnostics:
                    raise PipelineError("Passive completion has invalid native constraints")
                rows.extend(view["rows"])
                kinds.add(kind)
                if kind == "concentric":
                    for entity in entities:
                        _, _, position, other = _axis(entity, frames)
                        offset = [position[i] - point[i] for i in range(3)]
                        if (abs(abs(_dot(direction, other)) - 1.0) > 1e-9
                                or math.hypot(*_cross(offset, direction)) > AXIS_OFFSET_TOL_M):
                            raise PipelineError("Passive completion contains another native axis")
                else:
                    planes = [_geometry(entity, frames, "plane") for entity in entities]
                    for _, _, _, normal in planes:
                        if abs(abs(_dot(direction, normal)) - 1.0) > 1e-9:
                            raise PipelineError("Passive plane is not perpendicular to its rotation axis")
                    separation = [planes[0][2][i] - planes[1][2][i] for i in range(3)]
                    if abs(_dot(separation, planes[0][3])) > AXIS_OFFSET_TOL_M:
                        raise PipelineError("Passive coincident planes are not coincident")
            twist = [*_cross(point, direction), *direction]
            if (kinds != {"concentric", "coincident"} or len(owners) != 1 or _rank(rows) != 5
                    or any(abs(_dot(row, twist)) / max(math.hypot(*row), 1e-12) > 1e-9 for row in rows)):
                raise PipelineError("Native constraints do not prove exactly one passive rotation")
            suffix = hashlib.sha256(component.encode("utf-8")).hexdigest()[:12]
            body, joint = "passive_" + suffix, "passive_joint_" + suffix
            if body in reserved_names or joint in reserved_names:
                raise PipelineError("Generated passive identity collides with a declared name")
            results.append({
                "component": component, "parent": next(iter(owners)), "body": body, "joint": joint,
                "axis_reference": selector, "axis": axis, "rank": 5,
                "qualification": "cad_derived_passive_dof; author_intent_and_drive_unqualified",
            })
        except (PipelineError, KeyError, TypeError, ValueError):
            # Remaining coverage is reported by the caller, never completed by guessing.
            continue
    return results
