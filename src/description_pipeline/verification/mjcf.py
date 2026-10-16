"""Independent verification of a delivered URDF → MJCF bundle.

The verifier never imports the converter.  It re-derives every expectation from the frozen
``input/`` URDF, the referenced meshes and ``simulation.json``, then loads the *actual*
``robot.xml`` / ``scene.xml`` in this process with the locked MuJoCo and compares semantics.
Delivered bytes are never the source of truth, and opening a model successfully is not evidence
of physics.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np

from .. import mjcf_contract as contract
from ..io import PipelineError, canonical, read_data, write_json

_MOVING = {"revolute": "hinge", "continuous": "hinge", "prismatic": "slide"}
_DEFAULT_RGBA = (0.5, 0.5, 0.5, 1.0)


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #


def _numbers(text: str | None, count: int, what: str) -> list[float]:
    if text is None:
        raise PipelineError(f"{what} is missing")
    parts = text.split()
    if len(parts) != count:
        raise PipelineError(f"{what} must hold {count} numbers, got {text!r}")
    values = [float(part) for part in parts]
    if not all(math.isfinite(value) for value in values):
        raise PipelineError(f"{what} must be finite, got {text!r}")
    return values


def _rotation(rpy: tuple[float, float, float]) -> np.ndarray:
    roll, pitch, yaw = rpy
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )


def _quat_matrix(quat: Any) -> np.ndarray:
    w, x, y, z = (float(value) for value in quat)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def _axis_rotation(axis: Any, angle: float) -> np.ndarray:
    x, y, z = (float(value) for value in axis)
    c, s = math.cos(angle), math.sin(angle)
    one = 1.0 - c
    return np.array(
        [
            [c + x * x * one, x * y * one - z * s, x * z * one + y * s],
            [y * x * one + z * s, c + y * y * one, y * z * one - x * s],
            [z * x * one - y * s, z * y * one + x * s, c + z * z * one],
        ]
    )


def _transform(rotation: np.ndarray, translation: Any) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    matrix[:3, 3] = [float(value) for value in translation]
    return matrix


def _origin(element: ET.Element | None) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if element is None:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    node = element.find("origin")
    if node is None:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    xyz = _numbers(node.get("xyz"), 3, "an origin xyz") if node.get("xyz") else [0.0, 0.0, 0.0]
    rpy = _numbers(node.get("rpy"), 3, "an origin rpy") if node.get("rpy") else [0.0, 0.0, 0.0]
    return (xyz[0], xyz[1], xyz[2]), (rpy[0], rpy[1], rpy[2])


# --------------------------------------------------------------------------- #
# Independent URDF and STL reading
# --------------------------------------------------------------------------- #


def _stl(path: Path) -> np.ndarray:
    payload = path.read_bytes()
    if len(payload) < 84:
        raise PipelineError(f"The frozen mesh is too short to be a binary STL: {path.name}")
    count = struct.unpack("<I", payload[80:84])[0]
    if len(payload) != 84 + count * 50:
        raise PipelineError(f"The frozen mesh is not a binary STL of {count} triangles: {path.name}")
    vertices = np.frombuffer(payload, dtype=np.uint8, count=count * 50, offset=84).reshape(count, 50)
    points = vertices[:, 12:48].copy().view("<f4").reshape(count, 3, 3)
    flat = points.reshape(-1, 3).astype(np.float64)
    if not np.all(np.isfinite(flat)):
        raise PipelineError(f"The frozen mesh carries non-finite vertices: {path.name}")
    return flat


def _geometry(element: ET.Element, base: Path, closure: Path, what: str) -> dict[str, Any]:
    node = element.find("geometry")
    if node is None or len(node) != 1:
        raise PipelineError(f"{what} carries no single supported geometry")
    shape = node[0]
    origin = _origin(element)
    material = element.find("material")
    colour = _DEFAULT_RGBA
    if material is not None and material.find("color") is not None and material.find("color").get("rgba"):
        colour = tuple(_numbers(material.find("color").get("rgba"), 4, "a material colour"))
    entry: dict[str, Any] = {"origin": origin, "rgba": colour}
    if shape.tag == "box":
        size = _numbers(shape.get("size"), 3, f"{what} box size")
        entry.update(kind="box", size=[value / 2.0 for value in size])
    elif shape.tag == "sphere":
        entry.update(kind="sphere", size=[float(shape.get("radius"))])
    elif shape.tag == "cylinder":
        entry.update(kind="cylinder", size=[float(shape.get("radius")), float(shape.get("length")) / 2.0])
    elif shape.tag == "mesh":
        relative = (base / shape.get("filename", "")).resolve()
        if not relative.is_relative_to(closure.resolve()):
            raise PipelineError(f"{what} references a mesh outside the frozen closure")
        scale = _numbers(shape.get("scale"), 3, f"{what} mesh scale") if shape.get("scale") else [1.0, 1.0, 1.0]
        entry.update(kind="mesh", size=[], scale=scale, vertices=_stl(relative) * np.array(scale))
    else:
        raise PipelineError(f"{what} carries an unsupported geometry <{shape.tag}>")
    return entry


def _read_robot(path: Path, closure: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    base = path.parent
    links: dict[str, Any] = {}
    joints: dict[str, Any] = {}
    for link in root.findall("link"):
        name = link.get("name")
        inertial = link.find("inertial")
        mass = None
        com = (0.0, 0.0, 0.0)
        tensor = None
        if inertial is not None:
            mass = float(inertial.find("mass").get("value"))
            com = _origin(inertial)[0]
            _, rpy = _origin(inertial)
            node = inertial.find("inertia")
            local = np.array(
                [
                    [float(node.get("ixx")), float(node.get("ixy")), float(node.get("ixz"))],
                    [float(node.get("ixy")), float(node.get("iyy")), float(node.get("iyz"))],
                    [float(node.get("ixz")), float(node.get("iyz")), float(node.get("izz"))],
                ]
            )
            rotation = _rotation(rpy)
            tensor = rotation @ local @ rotation.T
        links[name] = {
            "mass": mass,
            "com": com,
            "tensor": tensor,
            "visuals": [
                _geometry(element, base, closure, f"Link {name!r} visual[{index}]")
                for index, element in enumerate(link.findall("visual"))
            ],
            "collisions": [
                _geometry(element, base, closure, f"Link {name!r} collision[{index}]")
                for index, element in enumerate(link.findall("collision"))
            ],
        }
    for index, joint in enumerate(root.findall("joint")):
        name = joint.get("name")
        kind = joint.get("type")
        axis_node = joint.find("axis")
        axis = (
            tuple(_numbers(axis_node.get("xyz"), 3, f"Joint {name!r} axis"))
            if axis_node is not None
            else (0.0, 0.0, 0.0)
        )
        limit = joint.find("limit")
        limits = None
        effort = velocity = None
        if limit is not None:
            if kind in ("revolute", "prismatic"):
                limits = (float(limit.get("lower")), float(limit.get("upper")))
            effort = float(limit.get("effort")) if limit.get("effort") else None
            velocity = float(limit.get("velocity")) if limit.get("velocity") else None
        damping = friction = 0.0
        dynamics = joint.find("dynamics")
        if dynamics is not None:
            damping = float(dynamics.get("damping") or 0.0)
            friction = float(dynamics.get("friction") or 0.0)
        joints[name] = {
            "kind": kind,
            "parent": joint.find("parent").get("link"),
            "child": joint.find("child").get("link"),
            "origin": _origin(joint),
            "axis": axis,
            "limits": limits,
            "damping": damping,
            "friction": friction,
            "effort": effort,
            "velocity": velocity,
            "index": index,
        }
    roots = [name for name in links if name not in {joint["child"] for joint in joints.values()}]
    if len(roots) != 1:
        raise PipelineError(f"The frozen URDF does not describe one rooted tree: roots={sorted(roots)}")
    return {"links": links, "joints": joints, "root": roots[0]}


def _forward(robot: dict[str, Any], angles: dict[str, float]) -> dict[str, np.ndarray]:
    """Independent URDF forward kinematics, the reference the compiled model must reproduce."""

    children: dict[str, list[tuple[str, dict[str, Any]]]] = {name: [] for name in robot["links"]}
    for name, joint in robot["joints"].items():
        children[joint["parent"]].append((name, joint))
    poses: dict[str, np.ndarray] = {}

    def walk(link: str, pose: np.ndarray) -> None:
        poses[link] = pose
        for name, joint in children[link]:
            translation, rpy = joint["origin"]
            local = _transform(_rotation(rpy), translation)
            if joint["kind"] in _MOVING:
                angle = angles.get(name, 0.0)
                if joint["kind"] == "prismatic":
                    local = local @ _transform(np.eye(3), np.array(joint["axis"]) * angle)
                else:
                    local = local @ _transform(_axis_rotation(joint["axis"], angle), (0.0, 0.0, 0.0))
            walk(joint["child"], pose @ local)

    walk(robot["root"], np.eye(4))
    return poses


# --------------------------------------------------------------------------- #
# Bundle-level checks
# --------------------------------------------------------------------------- #


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _close(actual: Any, expected: Any, absolute: float, what: str) -> float:
    residual = float(np.max(np.abs(np.asarray(actual, dtype=np.float64) - np.asarray(expected, dtype=np.float64))))
    if not residual <= absolute:
        raise PipelineError(f"{what} differs by {residual:.3e} (limit {absolute:.1e})")
    return residual


def _rotation_residual(actual: Any, expected: Any) -> float:
    return float(np.max(np.abs(np.asarray(actual, dtype=np.float64) - np.asarray(expected, dtype=np.float64))))


def _vertex_residual(actual: np.ndarray, expected: np.ndarray, what: str) -> float:
    left = np.unique(np.asarray(actual, dtype=np.float64).T, axis=0)
    right = np.unique(np.asarray(expected, dtype=np.float64).T, axis=0)
    if left.shape != right.shape:
        raise PipelineError(f"{what} delivers {left.shape[0]} distinct world vertices, expected {right.shape[0]}")
    order = lambda cloud: cloud[np.lexsort((cloud[:, 2], cloud[:, 1], cloud[:, 0]))]  # noqa: E731
    residual = float(np.max(np.abs(order(left) - order(right))))
    if not residual <= contract.MESH_ABS:
        raise PipelineError(f"{what} world vertices differ by {residual:.3e} (limit {contract.MESH_ABS:.1e})")
    return residual


def check_bundle(bundle: Path, *, compare_saved: bool = True, write_report: bool = False) -> dict:
    """Recompute every check against the delivered bytes and return the deterministic report."""

    import mujoco

    bundle = Path(bundle)
    if not bundle.is_dir():
        raise PipelineError(f"The delivery is not a directory: {bundle}")
    payload = contract.inventory(bundle)
    manifest = contract.verify_subject(read_data(bundle / contract.MANIFEST_JSON))
    _require(
        manifest.get("engine") == contract.engine_identity(),
        "The manifest engine identity does not match the locked consumer",
    )
    _require(manifest.get("files") == payload, "The manifest file table does not match the delivered files")
    sources = manifest.get("sources")
    _require(isinstance(sources, dict) and sources, "The manifest records no frozen sources")
    frozen = {name: value for name, value in payload.items() if name.startswith(f"{contract.INPUT_PREFIX}/")}
    expected_frozen = {f"{contract.INPUT_PREFIX}/{name}": value for name, value in sources.items()}
    _require(frozen == expected_frozen, "The frozen input closure differs from the recorded sources")
    urdf_relative = manifest.get("urdf")
    _require(
        isinstance(urdf_relative, str) and f"{contract.INPUT_PREFIX}/{urdf_relative}" in payload,
        "The manifest does not bind a frozen URDF",
    )
    for name in (contract.SIMULATION_JSON, contract.ROBOT_XML, contract.REPORT_JSON):
        _require(name in payload or name == contract.REPORT_JSON, f"The delivery is missing {name}")
    settings = contract.read_config(bundle / contract.SIMULATION_JSON)
    checks: dict[str, Any] = {"identity": {"files": len(payload), "sources": len(sources)}}
    robot = _read_robot(bundle / contract.INPUT_PREFIX / urdf_relative, bundle / contract.INPUT_PREFIX)
    _check_topology(robot, settings, bundle, mujoco, checks)
    _check_inertia(robot, bundle, mujoco, checks)
    _check_geometry(robot, settings, bundle, mujoco, checks)
    _check_configuration(robot, settings, bundle, mujoco, checks)
    _check_scene(settings, bundle, mujoco, checks)
    _check_runtime(bundle, mujoco, checks)
    _require(contract.inventory(bundle) == payload, "The delivery body changed while it was checked")
    report = {
        "passed": True,
        "kind": contract.MODULE_ID,
        "schema_version": contract.SCHEMA_VERSION,
        "engine": contract.engine_identity(),
        "subject_sha256": manifest["subject_sha256"],
        "checks": checks,
    }
    if write_report:
        write_json(bundle / contract.REPORT_JSON, report)
    if compare_saved:
        saved = bundle / contract.REPORT_JSON
        if not saved.is_file():
            raise PipelineError("The delivery carries no saved quality report to compare")
        if canonical(read_data(saved)) != canonical(report):
            raise PipelineError("The saved quality report differs from the recomputed report")
    return report


def _check_topology(
    robot: dict[str, Any], settings: dict[str, Any], bundle: Path, mujoco: Any, checks: dict[str, Any]
) -> None:
    model = mujoco.MjModel.from_xml_path(str(bundle / contract.ROBOT_XML))
    links = robot["links"]
    _require(model.nbody == len(links) + 1, f"The compiled model has {model.nbody} bodies, expected {len(links) + 1}")
    for name in links:
        _require(mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name) > 0, f"Link {name!r} has no delivered body")
    joints = robot["joints"]
    moving = [name for name, joint in joints.items() if joint["kind"] in _MOVING]
    expected = (7 if settings["base"] == "floating" else 0) + len(moving)
    velocity = (6 if settings["base"] == "floating" else 0) + len(moving)
    _require(model.nq == expected, f"The compiled model has {model.nq} position dofs, expected {expected}")
    _require(model.nv == velocity, f"The compiled model has {model.nv} velocity dofs, expected {velocity}")
    _require(model.nu == 0, f"The compiled model carries {model.nu} implicit actuators")
    for name, joint in joints.items():
        identifier = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        if joint["kind"] == "fixed":
            _require(identifier == -1, f"Fixed joint {name!r} was projected as an articulated joint")
            continue
        _require(identifier >= 0, f"Joint {name!r} has no delivered joint")
        kind = model.jnt_type[identifier]
        expected_kind = mujoco.mjtJoint.mjJNT_HINGE if joint["kind"] != "prismatic" else mujoco.mjtJoint.mjJNT_SLIDE
        _require(kind == expected_kind, f"Joint {name!r} is not projected as a {_MOVING[joint['kind']]}")
    checks["topology"] = {"bodies": model.nbody, "nq": model.nq, "nv": model.nv, "nu": model.nu}
    _check_kinematics(robot, model, mujoco, checks)


def _check_kinematics(robot: dict[str, Any], model: Any, mujoco: Any, checks: dict[str, Any]) -> None:
    data = mujoco.MjData(model)
    worst_position = worst_rotation = worst_axis = 0.0
    samples: list[dict[str, float]] = [{}]
    for name, joint in robot["joints"].items():
        if joint["kind"] not in _MOVING:
            continue
        samples.append({name: 0.5 if joint["kind"] == "prismatic" else 0.4})
        samples.append({name: -0.3 if joint["kind"] == "prismatic" else -0.6})
    combined = {
        name: (0.2 if joint["kind"] == "prismatic" else 0.3)
        for name, joint in robot["joints"].items()
        if joint["kind"] in _MOVING
    }
    if combined:
        samples.append(combined)
    moving_axes = 0
    for name, joint in robot["joints"].items():
        if joint["kind"] in _MOVING:
            identifier = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
            residual = _close(model.jnt_axis[identifier], joint["axis"], contract.ROTATION_ABS, f"Joint {name!r} axis")
            worst_axis = max(worst_axis, residual)
            moving_axes += 1
    for angles in samples:
        data.qpos[:] = 0.0
        for name, angle in angles.items():
            identifier = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
            data.qpos[model.jnt_qposadr[identifier]] = angle
        mujoco.mj_forward(model, data)
        poses = _forward(robot, angles)
        for link, pose in poses.items():
            identifier = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, link)
            worst_position = max(
                worst_position,
                _close(data.xpos[identifier], pose[:3, 3], contract.POSITION_ABS, f"Link {link!r} world origin"),
            )
            worst_rotation = max(worst_rotation, _rotation_residual(data.xmat[identifier].reshape(3, 3), pose[:3, :3]))
    _require(
        worst_rotation <= contract.ROTATION_ABS,
        f"Link world orientation differs by {worst_rotation:.3e} (limit {contract.ROTATION_ABS:.1e})",
    )
    checks["kinematics"] = {
        "samples": len(samples),
        "moving_axes": moving_axes,
        "position_residual": worst_position,
        "rotation_residual": worst_rotation,
        "axis_residual": worst_axis,
    }


def _check_inertia(robot: dict[str, Any], bundle: Path, mujoco: Any, checks: dict[str, Any]) -> None:
    model = mujoco.MjModel.from_xml_path(str(bundle / contract.ROBOT_XML))
    worst_mass = worst_com = worst_tensor = worst_composite = 0.0
    total_mass = 0.0
    total_tensor = np.zeros((3, 3))
    for name, link in robot["links"].items():
        identifier = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)
        if link["mass"] is None:
            _require(
                float(model.body_mass[identifier]) == 0.0,
                f"Link {name!r} carries no URDF inertia but the delivered body has mass",
            )
            continue
        worst_mass = max(
            worst_mass, _close(model.body_mass[identifier], link["mass"], contract.INERTIA_ABS, f"Link {name!r} mass")
        )
        worst_com = max(
            worst_com,
            _close(model.body_ipos[identifier], link["com"], contract.POSITION_ABS, f"Link {name!r} centre of mass"),
        )
        recovered = (
            _quat_matrix(model.body_iquat[identifier])
            @ np.diag(model.body_inertia[identifier])
            @ _quat_matrix(model.body_iquat[identifier]).T
        )
        scale = max(contract.INERTIA_ABS, float(np.max(np.abs(link["tensor"]))) * contract.INERTIA_REL)
        residual = float(np.max(np.abs(recovered - link["tensor"])))
        if not residual <= scale:
            raise PipelineError(f"Link {name!r} inertia tensor differs by {residual:.3e} (limit {scale:.1e})")
        worst_tensor = max(worst_tensor, residual)
        total_mass += link["mass"]
        offset = np.array(link["com"], dtype=np.float64)
        total_tensor += link["tensor"] + link["mass"] * (float(offset @ offset) * np.eye(3) - np.outer(offset, offset))
    if total_mass:
        compiled_mass = float(np.sum(model.body_mass))
        _require(
            abs(compiled_mass - total_mass) <= contract.INERTIA_ABS * max(1.0, total_mass),
            f"The compiled total mass {compiled_mass!r} differs from the URDF total {total_mass!r}",
        )
        poses = _forward(robot, {})
        world_com = np.zeros(3)
        for name, link in robot["links"].items():
            if link["mass"] is None:
                continue
            world_com += link["mass"] * (poses[name][:3, :3] @ np.array(link["com"]) + poses[name][:3, 3])
        world_com /= total_mass
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        worst_composite = _close(
            data.subtree_com[0], world_com, contract.POSITION_ABS, "The compiled whole-robot centre of mass"
        )
    checks["inertia"] = {
        "links": len(robot["links"]),
        "total_mass": total_mass,
        "mass_residual": worst_mass,
        "com_residual": worst_com,
        "composite_com_residual": worst_composite,
        "tensor_residual": worst_tensor,
        "parallel_axis_trace": float(np.trace(total_tensor)),
    }


def _check_geometry(
    robot: dict[str, Any], settings: dict[str, Any], bundle: Path, mujoco: Any, checks: dict[str, Any]
) -> None:
    model = mujoco.MjModel.from_xml_path(str(bundle / contract.ROBOT_XML))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    source = settings["collision"]["source"]
    masks = (1, 1) if settings["collision"]["self_collision"] else (1, 2)
    expected: list[tuple[str, str, int, dict[str, Any], tuple[int, int]]] = []
    for link, entry in robot["links"].items():
        for index, geometry in enumerate(entry["visuals"]):
            role_mask = (0, 0) if source == "urdf" else ((0, 0) if source == "none" else masks)
            expected.append((link, contract.VISUAL_ROLE, index, geometry, role_mask))
        if source == "urdf":
            for index, geometry in enumerate(entry["collisions"]):
                expected.append((link, contract.COLLISION_ROLE, index, geometry, masks))
    _require(model.ngeom == len(expected), f"The compiled model has {model.ngeom} geoms, expected {len(expected)}")
    worst_position = worst_rotation = worst_vertex = worst_size = 0.0
    meshes = 0
    for link, role, index, geometry, mask in expected:
        name = contract.geom_name(link, role, index)
        identifier = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, name)
        _require(identifier >= 0, f"Delivered geom {name!r} is missing")
        poses = _forward(robot, {})[link]
        translation, rpy = geometry["origin"]
        world = poses @ _transform(_rotation(rpy), translation)
        delivered_mask = (int(model.geom_contype[identifier]), int(model.geom_conaffinity[identifier]))
        _require(
            delivered_mask == mask,
            f"Geom {name!r} carries contact mask {delivered_mask}, expected {mask}",
        )
        if geometry["kind"] == "mesh":
            meshes += 1
            cloud = model.mesh_vert[
                model.mesh_vertadr[model.geom_dataid[identifier]] : model.mesh_vertadr[model.geom_dataid[identifier]]
                + model.mesh_vertnum[model.geom_dataid[identifier]]
            ]
            world_vertices = data.geom_xpos[identifier][:, None] + data.geom_xmat[identifier].reshape(3, 3) @ cloud.T
            expected_vertices = world[:3, 3][:, None] + world[:3, :3] @ geometry["vertices"].T
            worst_vertex = max(worst_vertex, _vertex_residual(world_vertices, expected_vertices, f"Geom {name!r}"))
        else:
            # A mesh geom's local frame is MuJoCo's recentred mesh frame, so only primitives keep the
            # URDF origin verbatim; a mesh is pinned by its world vertices instead.
            worst_position = max(
                worst_position,
                _close(data.geom_xpos[identifier], world[:3, 3], contract.POSITION_ABS, f"Geom {name!r} world origin"),
            )
            worst_rotation = max(
                worst_rotation, _rotation_residual(data.geom_xmat[identifier].reshape(3, 3), world[:3, :3])
            )
            worst_size = max(
                worst_size,
                _close(
                    model.geom_size[identifier][: len(geometry["size"])],
                    geometry["size"],
                    contract.MESH_ABS,
                    f"Geom {name!r} size",
                ),
            )
    checks["geometry"] = {
        "geoms": model.ngeom,
        "meshes": meshes,
        "position_residual": worst_position,
        "rotation_residual": worst_rotation,
        "size_residual": worst_size,
        "vertex_residual": worst_vertex,
    }
    _require(worst_rotation <= contract.ROTATION_ABS, f"A delivered geom orientation differs by {worst_rotation:.3e}")


def _check_configuration(
    robot: dict[str, Any], settings: dict[str, Any], bundle: Path, mujoco: Any, checks: dict[str, Any]
) -> None:
    document = ET.parse(bundle / contract.ROBOT_XML).getroot()
    compiler = document.find("compiler")
    _require(compiler is not None, "The delivered robot carries no <compiler> element")
    _require(compiler.get("angle") == "radian", "The delivered compiler angle must be radian; MJCF defaults to degrees")
    _require(compiler.get("inertiafromgeom") == "false", "The delivered compiler must not invent inertia from geometry")
    _require(compiler.get("balanceinertia") == "false", "The delivered compiler must not silently rebalance inertia")
    model = mujoco.MjModel.from_xml_path(str(bundle / contract.ROBOT_XML))
    worst_range = worst_damping = worst_friction = 0.0
    for name, joint in robot["joints"].items():
        if joint["kind"] not in _MOVING:
            continue
        identifier = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        if joint["limits"] is None:
            _require(int(model.jnt_limited[identifier]) == 0, f"Continuous joint {name!r} was given a finite range")
            continue
        _require(int(model.jnt_limited[identifier]) == 1, f"Joint {name!r} lost its bounded range")
        worst_range = max(
            worst_range,
            _close(model.jnt_range[identifier], joint["limits"], contract.POSITION_ABS, f"Joint {name!r} range"),
        )
        worst_damping = max(
            worst_damping,
            _close(
                model.dof_damping[model.jnt_dofadr[identifier]],
                joint["damping"],
                contract.INERTIA_ABS,
                f"Joint {name!r} damping",
            ),
        )
        worst_friction = max(
            worst_friction,
            _close(
                model.dof_frictionloss[model.jnt_dofadr[identifier]],
                joint["friction"],
                contract.INERTIA_ABS,
                f"Joint {name!r} frictionloss",
            ),
        )
    for site in settings["sites"]:
        identifier = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, site["name"])
        _require(identifier >= 0, f"Configured site {site['name']!r} is missing from the delivery")
        # A site lives in its link's frame, which the delivery keeps identical to the URDF link frame.
        _close(model.site_pos[identifier], site["xyz"], contract.POSITION_ABS, f"Site {site['name']!r} position")
        _require(
            _rotation_residual(_quat_matrix(model.site_quat[identifier]), _rotation(tuple(site["rpy"])))
            <= contract.ROTATION_ABS,
            f"Site {site['name']!r} orientation does not match its configuration",
        )
        owner = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, site["link"])
        _require(
            owner > 0 and int(model.site_bodyid[identifier]) == owner,
            f"Site {site['name']!r} is not attached to link {site['link']!r}",
        )
    checks["configuration"] = {
        "sites": len(settings["sites"]),
        "range_residual": worst_range,
        "damping_residual": worst_damping,
        "frictionloss_residual": worst_friction,
        "effort_velocity": {
            name: {"effort": joint["effort"], "velocity": joint["velocity"]}
            for name, joint in sorted(robot["joints"].items())
            if joint["effort"] is not None or joint["velocity"] is not None
        },
    }


def _check_scene(settings: dict[str, Any], bundle: Path, mujoco: Any, checks: dict[str, Any]) -> None:
    scene = bundle / contract.SCENE_XML
    if settings["floor"] is None:
        _require(not scene.exists(), "A scene was delivered although the configuration declares no floor")
        checks["scene"] = {"floor": None}
        return
    _require(scene.is_file(), "The configuration declares a floor but no scene was delivered")
    document = ET.parse(scene).getroot()
    include = document.find("include")
    _require(
        include is not None and include.get("file") == contract.ROBOT_XML,
        "The scene does not include the delivered robot",
    )
    _require(any(element.tag == "light" for element in document.iter()), "The scene carries no light")
    model = mujoco.MjModel.from_xml_path(str(scene))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    planes = [index for index in range(model.ngeom) if model.geom_type[index] == mujoco.mjtGeom.mjGEOM_PLANE]
    _require(len(planes) == 1, f"The scene carries {len(planes)} planes, expected exactly one floor")
    floor = planes[0]
    expected_height = settings["floor"]["height"]
    _close(data.geom_xpos[floor], (0.0, 0.0, expected_height), contract.POSITION_ABS, "The scene floor position")
    normal = data.geom_xmat[floor].reshape(3, 3)[:, 2]
    _require(
        _rotation_residual(normal, (0.0, 0.0, 1.0)) <= contract.ROTATION_ABS, "The scene floor normal is not vertical"
    )
    _close(model.geom_friction[floor], settings["floor"]["friction"], contract.INERTIA_ABS, "The scene floor friction")
    robot = mujoco.MjModel.from_xml_path(str(bundle / contract.ROBOT_XML))
    _require(
        model.nq == robot.nq and model.nv == robot.nv and model.nbody == robot.nbody,
        "The scene does not carry the delivered robot unchanged",
    )
    checks["scene"] = {"floor": settings["floor"], "light": True, "geoms": model.ngeom}


def _check_runtime(bundle: Path, mujoco: Any, checks: dict[str, Any]) -> None:
    model = mujoco.MjModel.from_xml_path(str(bundle / contract.ROBOT_XML))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    _require(
        bool(np.all(np.isfinite(data.qpos))) and bool(np.all(np.isfinite(data.qvel))), "The zero state is not finite"
    )
    for _ in range(10):
        mujoco.mj_step(model, data)
    _require(
        bool(np.all(np.isfinite(data.qpos))) and bool(np.all(np.isfinite(data.qvel))),
        "A short zero-drive run produced a non-finite state",
    )
    again = mujoco.MjModel.from_xml_path(str(bundle / contract.ROBOT_XML))
    _require(
        again.nq == model.nq and again.nv == model.nv and again.nbody == model.nbody,
        "Reloading the saved XML does not reproduce the model",
    )
    _require(
        bool(np.allclose(again.body_mass, model.body_mass, atol=0.0, rtol=0.0)),
        "Reloading the saved XML changes the delivered masses",
    )
    checks["runtime"] = {"steps": 10, "reload": True}
