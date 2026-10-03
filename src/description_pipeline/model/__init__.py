"""Canonical semantics with explicit units, tensor frames and identities."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np

from ..io import PipelineError, read_data


@dataclass(frozen=True)
class Robot:
    """The internal JSON is private; callers receive copies rather than shared mutation."""

    _data: dict

    @classmethod
    def from_dict(cls, data: dict) -> Robot:
        validate_scene_schema(data)
        robot = cls(copy.deepcopy(data))
        robot._validate()
        return robot

    def to_dict(self) -> dict:
        return copy.deepcopy(self._data)

    def _validate(self) -> None:
        data = self._data
        for key in ("links", "joints", "frames", "actuators", "sensors"):
            for field in ("id", "name"):
                values = [obj[field] for obj in data[key]]
                if len(values) != len(set(values)):
                    raise PipelineError(f"Duplicate {key}.{field}")
        links = {link["name"]: link for link in data["links"]}
        frame_names = {frame["name"] for frame in data["frames"]}
        joint_names = {joint["name"] for joint in data["joints"]}
        if frame_names & links.keys() or {name + "_fixed" for name in frame_names} & joint_names:
            raise PipelineError("Frame names collide with generated URDF links/joints")
        children = set()
        for link in links.values():
            for geometry in link["visuals"] + link["collisions"]:
                for field in ("size", "scale"):
                    if field in geometry and any(value <= 0 for value in geometry[field]):
                        raise PipelineError(f"Geometry {field} must be positive")
                if any(not 0 <= value <= 1 for value in geometry.get("rgba", [])):
                    raise PipelineError("RGBA must be in [0, 1]")
            inertial = link["inertial"]
            if inertial is None:
                if link["provenance"].get("kind") != "reference_frame" or link["visuals"] or link["collisions"]:
                    raise PipelineError(f"Missing physical inertia is not a massless reference frame: {link['name']}")
                continue
            eigen = np.linalg.eigvalsh(tensor(inertial["inertia"]))
            if eigen[0] <= 0 or eigen[2] > eigen[0] + eigen[1] + 1e-10 * eigen[2]:
                raise PipelineError(f"Physically impossible COM inertia: {link['name']}")
        graph: dict[str, list[str]] = {name: [] for name in links}
        for joint in data["joints"]:
            if joint["parent"] not in links or joint["child"] not in links:
                raise PipelineError(f"Unknown joint endpoint: {joint['name']}")
            if joint["child"] in children:
                raise PipelineError(f"Multiple parent joints: {joint['child']}; use explicit constraints")
            children.add(joint["child"])
            graph[joint["parent"]].append(joint["child"])
            if joint["type"] != "fixed":
                if "axis" not in joint or np.linalg.norm(joint["axis"]) < 1e-12:
                    raise PipelineError(f"Missing or zero joint axis: {joint['name']}")
                if not np.isclose(np.linalg.norm(joint["axis"]), 1, atol=1e-8):
                    raise PipelineError(f"Joint axis must be normalized explicitly: {joint['name']}")
                if joint["type"] in {"revolute", "prismatic"}:
                    limits = joint.get("limits", {})
                    if "lower" not in limits or "upper" not in limits or limits["lower"] >= limits["upper"]:
                        raise PipelineError(f"Missing or invalid position limits: {joint['name']}")
        roots = set(links) - children
        if len(roots) != 1:
            raise PipelineError(f"Expected one kinematic root, got {sorted(roots)}")
        visited = set()

        def walk(name: str) -> None:
            if name in visited:
                raise PipelineError(f"Cycle in kinematic tree: {name}")
            visited.add(name)
            for child in graph[name]:
                walk(child)

        walk(next(iter(roots)))
        if visited != set(links):
            raise PipelineError("Disconnected or cyclic links")
        moving = {joint["name"] for joint in data["joints"] if joint["type"] != "fixed"}
        mimic = {joint["name"]: joint["mimic"]["joint"] for joint in data["joints"] if joint.get("mimic")}
        for name, reference in mimic.items():
            if reference not in moving or name not in moving:
                raise PipelineError("Mimic must reference movable joints")
            seen = {name}
            while reference in mimic:
                if reference in seen:
                    raise PipelineError("Cyclic mimic dependency")
                seen.add(reference)
                reference = mimic[reference]
        for actuator in data["actuators"]:
            if actuator["joint"] not in moving:
                raise PipelineError(f"Unknown actuator joint: {actuator['joint']}")
            if actuator["gear"] == 0 or actuator["control_range"][0] >= actuator["control_range"][1]:
                raise PipelineError("Actuator requires nonzero gear and an ordered control range")
        drives = data.get("mechanical_drives")
        if drives is not None:
            if set(drives) != moving:
                raise PipelineError("Mechanical drives require all movable canonical joint names, not source ids")
            identities = [drive["id"] for drive in drives.values() if drive["kind"] == "active"]
            if len(identities) != len(set(identities)):
                raise PipelineError("Duplicate mechanical drive identity")
            if any(drives[item["joint"]]["kind"] == "passive" for item in data["actuators"]):
                raise PipelineError("A passive mechanical joint cannot declare a simulated actuator")
            joints_by_name = {joint["name"]: joint for joint in data["joints"]}
            for name, drive in drives.items():
                if drive["kind"] != "active":
                    continue
                joint = joints_by_name[name]
                stator, rotor = set(drive["stator"]), set(drive["rotor"])
                parent = set(links[joint["parent"]]["provenance"].get("source_entities", []))
                child = set(links[joint["child"]]["provenance"].get("source_entities", []))
                if stator & rotor or not stator <= parent or not rotor <= child:
                    raise PipelineError(
                        f"Mechanical drive {name}: stator/rotor must belong to parent/child source instances"
                    )
        for frame in data["frames"]:
            if frame["parent"] not in links:
                raise PipelineError(f"Unknown frame parent: {frame['parent']}")
        for sensor in data["sensors"]:
            if sensor["frame"] not in frame_names:
                raise PipelineError(f"Unknown sensor frame: {sensor['frame']}")
        for pair in data["contact_excludes"]:
            if pair["body1"] not in links or pair["body2"] not in links:
                raise PipelineError("Unknown body in contact exclusion")


def validate_scene_schema(data: dict) -> None:
    """Frozen CAD assemblies may be forests; canonical Robot additionally enforces a tree."""
    schema = read_data(Path(__file__).with_name("schema.json"))
    try:
        jsonschema.Draft202012Validator(schema).validate(data)
    except jsonschema.ValidationError as error:
        raise PipelineError(f"Model contract {list(error.absolute_path)}: {error.message}") from error
    _finite(data)


def _finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise PipelineError("Nonfinite model value")
    if isinstance(value, dict):
        for child in value.values():
            _finite(child)
    elif isinstance(value, list):
        for child in value:
            _finite(child)


def tensor(values: list[float]) -> np.ndarray:
    xx, xy, xz, yy, yz, zz = values
    return np.array([[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]], dtype=float)


def rotation(rpy: list[float]) -> np.ndarray:
    r, p, y = rpy
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )
