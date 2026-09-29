"""Compare effective engine geometry against independently parsed URDF geometry."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from ..geometry.stl import vertices
from ..io import PipelineError
from .urdf_quality.model import rpy_matrix


def geometry_evidence(root: Path, engine, data, mujoco, profile: dict) -> list[dict]:
    xml = ET.parse(root / "urdf/robot.urdf").getroot()
    evidence = []
    for link in xml.findall("link"):
        name = link.get("name")
        body_id = mujoco.mj_name2id(engine, mujoco.mjtObj.mjOBJ_BODY, name)
        if body_id < 0:
            continue
        body_rotation = data.xmat[body_id].reshape(3, 3)
        for tag, role in (("visual", "visuals"), ("collision", "collisions")):
            for index, node in enumerate(link.findall(tag)):
                identifier = f"{name}_{role}_{index}"
                geom_id = mujoco.mj_name2id(engine, mujoco.mjtObj.mjOBJ_GEOM, identifier)
                if geom_id < 0:
                    evidence.append({"object": identifier, "passed": False, "reason": "missing"})
                    continue
                origin = node.find("origin")
                xyz = (
                    np.array([float(v) for v in origin.get("xyz", "0 0 0").split()])
                    if origin is not None
                    else np.zeros(3)
                )
                rpy = [float(v) for v in origin.get("rpy", "0 0 0").split()] if origin is not None else [0, 0, 0]
                expected_rotation = body_rotation @ np.asarray(rpy_matrix(tuple(rpy)), dtype=float)
                expected_position = data.xpos[body_id] + body_rotation @ xyz
                geometry = node.find("geometry")
                if geometry is None or len(geometry) != 1:
                    raise PipelineError("Missing or ambiguous URDF geometry")
                shape = geometry[0]
                expected_kind = {
                    "box": mujoco.mjtGeom.mjGEOM_BOX,
                    "sphere": mujoco.mjtGeom.mjGEOM_SPHERE,
                    "cylinder": mujoco.mjtGeom.mjGEOM_CYLINDER,
                    "mesh": mujoco.mjtGeom.mjGEOM_MESH,
                }[shape.tag]
                valid = engine.geom_type[geom_id] == expected_kind
                valid &= bool(engine.geom_contype[geom_id] == (0 if tag == "visual" else 1))
                valid &= bool(engine.geom_conaffinity[geom_id] == (0 if tag == "visual" else 1))
                position_error = 0.0
                if shape.tag == "mesh":
                    path = (root / "urdf" / shape.attrib["filename"]).resolve()
                    if not path.is_relative_to(root.resolve()):
                        raise PipelineError("Escaped URDF geometry path")
                    if path.suffix.lower() == ".stl":
                        points = vertices(path)
                    else:
                        points = np.array(
                            [
                                [float(v) for v in fields[1:4]]
                                for line in path.read_text(encoding="utf-8").splitlines()
                                if len(fields := line.split()) >= 4 and fields[0] == "v"
                            ]
                        )
                    points *= np.array([float(v) for v in shape.get("scale", "1 1 1").split()])
                    expected = points @ expected_rotation.T + expected_position
                    mesh_id = int(engine.geom_dataid[geom_id])
                    start = int(engine.mesh_vertadr[mesh_id])
                    count = int(engine.mesh_vertnum[mesh_id])
                    actual = (
                        engine.mesh_vert[start : start + count] @ data.geom_xmat[geom_id].reshape(3, 3).T
                        + data.geom_xpos[geom_id]
                    )
                    for reducer in (np.min, np.max):
                        position_error = max(
                            position_error, float(np.linalg.norm(reducer(expected, axis=0) - reducer(actual, axis=0)))
                        )
                    valid &= position_error <= max(profile["position_atol"], 1e-6)
                else:
                    position_error = float(np.linalg.norm(data.geom_xpos[geom_id] - expected_position))
                    valid &= position_error <= profile["position_atol"]
                    valid &= bool(
                        np.allclose(
                            data.geom_xmat[geom_id].reshape(3, 3),
                            expected_rotation,
                            rtol=0,
                            atol=profile["rotation_atol"],
                        )
                    )
                    if shape.tag == "box":
                        size = np.array([float(v) / 2 for v in shape.attrib["size"].split()])
                    elif shape.tag == "sphere":
                        size = np.array([float(shape.attrib["radius"])])
                    else:
                        size = np.array([float(shape.attrib["radius"]), float(shape.attrib["length"]) / 2])
                    valid &= bool(np.allclose(engine.geom_size[geom_id][: len(size)], size, atol=1e-9, rtol=1e-7))
                evidence.append(
                    {"object": identifier, "passed": bool(valid), "position_or_bounds_error_m": position_error}
                )
    expected_count = sum(len(link.findall(tag)) for link in xml.findall("link") for tag in ("visual", "collision"))
    if len(evidence) != expected_count or engine.ngeom != expected_count:
        evidence.append(
            {
                "object": "coverage",
                "passed": False,
                "expected": expected_count,
                "engine": int(engine.ngeom),
                "checked": len(evidence),
            }
        )
    return evidence
