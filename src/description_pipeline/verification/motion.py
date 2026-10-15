"""Non-zero per-leaf motion and interface assertions (the motion.{joint} owner).

Re-derives, from the declared joint evidence alone, where every rigid group's
leaves must be under any pose: the child group rotates about the joint's
evidenced axis line (a prismatic child translates along that axis and never
rotates), fixed joints are rigid welds, groups welded to a moving child
follow through the full chain, and the interface remains coaxial.  Evidence
defects are fail-closed (unordered limits, missing drive, anti-parallel
stored direction, poses outside the authored limits), and a statically green
candidate graph that keeps zero-pose leaf worlds while its parents, axis
line or leaf ownership are wrong diverges from the re-derived worlds as soon
as a pose is non-zero.
"""

from __future__ import annotations

import math

import numpy as np

from ..io import PipelineError

DEFAULT_TOLERANCE = {
    "position_m": 1e-9,
    "rotation_rad": 1e-9,
    "interface_line_m": 1e-9,
    "parallelism_1m": 1e-6,
}


def _pose(value) -> np.ndarray:
    matrix = np.asarray(value, dtype=float).reshape(4, 4)
    if not np.all(np.isfinite(matrix)):
        raise PipelineError("A motion pose carries a non-finite value")
    return matrix


def axis_rotation(axis, angle: float) -> np.ndarray:
    """The 3x3 rotation by ``angle`` about a unit ``axis`` through the origin."""

    unit = np.asarray(axis, dtype=float)
    norm = float(np.linalg.norm(unit))
    if norm == 0 or not math.isfinite(norm):
        raise PipelineError("A joint axis direction is not a finite vector")
    unit = unit / norm
    skew = np.array([[0.0, -unit[2], unit[1]], [unit[2], 0.0, -unit[0]], [-unit[1], unit[0], 0.0]])
    return np.eye(3) + math.sin(angle) * skew + (1 - math.cos(angle)) * (skew @ skew)


def line_rotation(point, direction, angle: float) -> np.ndarray:
    """Rotation by ``angle`` about the line through ``point`` along ``direction``."""

    rotation = axis_rotation(direction, angle)
    anchor = np.asarray(point, dtype=float)
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = anchor - rotation @ anchor
    return matrix


def line_translation(direction, distance: float) -> np.ndarray:
    """The 4x4 pure translation by ``distance`` along a ``direction``.

    A prismatic joint moves its child along the evidenced axis and never
    rotates it; the direction is normalised defensively because only the
    direction is meaningful for a slide.
    """

    unit = np.asarray(direction, dtype=float)
    norm = float(np.linalg.norm(unit)) if unit.shape == (3,) else 0.0
    if norm == 0 or not math.isfinite(norm):
        raise PipelineError("A prismatic axis has no usable direction")
    value = np.eye(4)
    value[:3, 3] = unit / norm * float(distance)
    return value


def evaluate_motion(*, bodies, joints, interface, poses, tolerance=None) -> dict:
    """Run the motion assertions of one pose sample over the declared evidence.

    ``bodies``: ``[{"id", "group", "frame", "leaves": [{"id", "world"}]}]`` with
    the zero-pose frames and leaf worlds; ``joints``: ``[{"id", "type", "parent",
    "child", "axis_point", "axis_dir", "stored_direction", "limits", "drive",
    "origin_parent_child"}]``; ``interface``: ``{"name", "on_parent", "on_child",
    "axis_point", "axis_dir"}``; ``poses``: joint id to angle.  Evidence defects
    raise; a sound input returns the re-derived leaf worlds, the interface
    continuity values and the intra-group relatives.
    """

    tolerance = {**DEFAULT_TOLERANCE, **(tolerance or {})}
    groups = {}
    group_body = {}
    leaves = {}
    for body in bodies:
        _pose(body["frame"])
        groups[body["id"]] = body["group"]
        if body["group"] in group_body and group_body[body["group"]] != body["id"]:
            raise PipelineError(f"Rigid group {body['group']!r} spans more than one body")
        group_body[body["group"]] = body["id"]
        for leaf in body["leaves"]:
            leaves[leaf["id"]] = (body["id"], _pose(leaf["world"]))
    by_id = {joint["id"]: joint for joint in joints}
    if len(by_id) != len(joints):
        raise PipelineError("Duplicate joint id in the motion evidence")

    def evidence(joint):
        if joint.get("type") == "fixed":
            # A fixed joint is a rigid weld: no drive, no limits, no pose.
            return None
        if joint.get("type") not in {"revolute", "prismatic", "continuous"}:
            raise PipelineError(f"Joint {joint['id']} is not a moving joint")
        limits = joint.get("limits") or {}
        drive = joint.get("drive")
        if not isinstance(drive, dict) or not all(
            isinstance(drive.get(key), (int, float)) and math.isfinite(drive[key]) and drive[key] >= 0
            for key in ("effort", "velocity")
        ):
            raise PipelineError(f"Joint {joint['id']} lacks its drive evidence")
        lower, upper = limits.get("lower"), limits.get("upper")
        if joint["type"] != "continuous" and not (
                isinstance(lower, (int, float))
                and isinstance(upper, (int, float))
                and math.isfinite(lower)
                and math.isfinite(upper)
                and lower <= upper
        ):
            raise PipelineError(f"Joint {joint['id']} has no usable ordered limits")
        stored, axis = np.asarray(joint["stored_direction"], float), np.asarray(joint["axis_dir"], float)
        if float(np.linalg.norm(stored)) == 0 or float(np.dot(stored, axis)) <= 0:
            raise PipelineError(f"Joint {joint['id']} stored direction is anti-parallel to its axis")
        return limits

    limits = {joint["id"]: evidence(joint) for joint in joints}
    for joint_id, pose in poses.items():
        if joint_id not in by_id:
            raise PipelineError(f"Pose names unknown joint {joint_id!r}")
        if by_id[joint_id]["type"] == "fixed":
            raise PipelineError(f"Joint {joint_id} is fixed and cannot carry a pose")
        bound = limits[joint_id]
        if by_id[joint_id]["type"] != "continuous" and not (bound["lower"] <= pose <= bound["upper"]):
            raise PipelineError(f"Pose for joint {joint_id} leaves its authored limits")

    # Group transforms: each driven child group inherits its parent group's
    # transform composed with the rotation about its own evidenced axis line.
    group_world = {}
    driven = {groups.get(joint["child"]) for joint in joints}
    for body in bodies:
        if body["group"] not in driven:
            group_world.setdefault(body["group"], np.eye(4))
    pending = list(joints)
    while pending:
        progressed = False
        for joint in list(pending):
            parent, child = groups.get(joint["parent"]), groups.get(joint["child"])
            if parent is None or child is None or parent not in group_world:
                continue
            if joint["type"] == "fixed":
                group_world[child] = group_world[parent]
            elif joint["type"] == "prismatic":
                group_world[child] = group_world[parent] @ line_translation(
                    joint["axis_dir"], float(poses.get(joint["id"], 0.0))
                )
            else:
                group_world[child] = group_world[parent] @ line_rotation(
                    joint["axis_point"], joint["axis_dir"], float(poses.get(joint["id"], 0.0))
                )
            pending.remove(joint)
            progressed = True
        if not progressed:
            raise PipelineError("The joint graph does not form a chain from a rooted group")
    for body in bodies:
        group_world.setdefault(body["group"], np.eye(4))

    # The group chain transform applies to the zero-pose worlds directly: for a
    # child frame L the composite rotation about the evidenced line reproduces
    # ``T(θ)·L``, whose frame factors cancel against the recorded zero worlds.
    world = {}
    for leaf, (owner, zero) in leaves.items():
        world[leaf] = group_world[groups[owner]] @ zero

    def line(body, point, direction):
        matrix = group_world[groups[body]]
        return (
            (matrix @ np.append(np.asarray(point, float), 1.0))[:3],
            matrix[:3, :3] @ np.asarray(direction, float),
        )

    parent_point, parent_direction = line(interface["on_parent"], interface["axis_point"], interface["axis_dir"])
    child_point, child_direction = line(interface["on_child"], interface["axis_point"], interface["axis_dir"])
    parent_direction = parent_direction / np.linalg.norm(parent_direction)
    child_direction = child_direction / np.linalg.norm(child_direction)
    continuity = {
        "line_distance_m": float(np.linalg.norm(np.cross(child_point - parent_point, parent_direction))),
        "parallelism": float(abs(np.dot(parent_direction, child_direction))),
    }
    intra = {}
    for group, body in group_body.items():
        members = [leaf for leaf, (owner, _zero) in leaves.items() if owner == body]
        for index, first in enumerate(members):
            for second in members[index + 1 :]:
                zero = np.linalg.inv(leaves[first][1]) @ leaves[second][1]
                now = np.linalg.inv(world[first]) @ world[second]
                intra[group] = max(intra.get(group, 0.0), float(np.max(np.abs(zero - now))))
    return {
        "group_world": {group: matrix.tolist() for group, matrix in group_world.items()},
        "world": {leaf: matrix.tolist() for leaf, matrix in world.items()},
        "interface": continuity,
        "intra_group_relative": intra,
    }
