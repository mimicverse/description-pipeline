"""Independent numeric oracles for the URDF -> MJCF module.

Complements ``tests/v1/test_mjcf.py``: that suite owns the CLI boundaries,
configuration rejections and the reseal/escape surfaces, while this file owns
the independent numeric evidence recomputed from the original URDF into the
actual ``MjModel`` - mass/COM/full inertia, FK samples, world vertex sets with
the mesh-compensation semantics - plus rotated near-degenerate and tiny-scale
tensors, the support-site mathematics (including the all-vertices contact
hull), and the counterexamples that separate vertex sets from bounding boxes
and site orientation/centroid semantics from containment.

Run from the repository root with the locked environment::

    python -B -m unittest tests.v1.test_mjcf_oracles -v
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

try:
    import mujoco
except ImportError:  # pragma: no cover - environment guard
    mujoco = None


REPO_ROOT = Path(__file__).resolve().parents[2]
URDF_REL = "urdf/robot.urdf"

TOL_POSE = 1e-10
TOL_TENSOR_ABS = 1e-12
TOL_TENSOR_REL = 1e-10
TOL_MASS_ABS = 1e-12
TOL_MASS_REL = 1e-10
TOL_STL_ABS = 1e-6
TOL_STL_REL = 1e-6

SUPPORT_EPS_SUPPORT = 1e-9
SUPPORT_EPS_CONTACT = 1e-9
SUPPORT_EPS_COPLANAR = 1e-9
SUPPORT_EPS_AREA = 1e-12
SUPPORT_EPS_TIE = 1e-9


# --------------------------------------------------------------------- utils

def _fmt(value: float) -> str:
    return format(float(value), ".17g")


def _fvec(values) -> str:
    return " ".join(_fmt(v) for v in values)


def _unit(values):
    array = np.asarray(values, dtype=float)
    return array / np.linalg.norm(array)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rpy_matrix(r: float, p: float, y: float) -> np.ndarray:
    """URDF fixed-axis roll-pitch-yaw: R = Rz(y) @ Ry(p) @ Rx(r)."""
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])


def axis_angle(axis, angle: float) -> np.ndarray:
    a = np.asarray(axis, dtype=float)
    a = a / np.linalg.norm(a)
    k = np.array([[0.0, -a[2], a[1]], [a[2], 0.0, -a[0]], [-a[1], a[0], 0.0]])
    return np.eye(3) + math.sin(angle) * k + (1.0 - math.cos(angle)) * (k @ k)


def quat_to_mat(q) -> np.ndarray:
    w, x, y, z = np.asarray(q, dtype=float) / np.linalg.norm(q)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def close_matrix(actual, expected, tol: float) -> bool:
    return float(np.max(np.abs(np.asarray(actual, dtype=float)
                               - np.asarray(expected, dtype=float)))) <= tol


# ------------------------------------------------------------------- STL I/O

def binary_stl(triangles) -> bytes:
    records = b"".join(
        struct.pack("<12fH", 0.0, 0.0, 0.0, *[float(x) for x in tri], 0)
        for tri in triangles
    )
    header = b"mjcf-oracle".ljust(80, b" ")
    return header + struct.pack("<I", len(triangles)) + records


def box_triangles(hx: float, hy: float, hz: float):
    v = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
         (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]
    faces = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 4, 5), (0, 5, 1),
             (1, 5, 6), (1, 6, 2), (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0)]
    return [tuple(coord for index in face for coord in v[index]) for face in faces]


def wedge_triangles(a: float, b: float, c: float):
    """Asymmetric solid with an origin-symmetric bbox (bbox-invariant tamper)."""
    v0 = (-a, -b, -c)
    v1 = (a, -b, -c)
    v2 = (a, b, -c)
    v3 = (-a, b, -c)
    apex = (a / 2.0, b / 2.0, c)
    return [
        (*v0, *v1, *v2), (*v0, *v2, *v3),
        (*v0, *v1, *apex), (*v1, *v2, *apex), (*v2, *v3, *apex), (*v3, *v0, *apex),
    ]


def read_stl_vertices(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    count = struct.unpack_from("<I", raw, 80)[0]
    dtype = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
    return np.frombuffer(raw, dtype=dtype, count=count, offset=84)["vertices"].reshape(-1, 3)


# ------------------------------------------------------------- URDF parsing

class Spec:
    def __init__(self):
        self.name = ""
        self.links = {}
        self.joints = {}
        self.root = None
        self.children = {}


def _parse_origin(element):
    if element is None:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    xyz = tuple(float(x) for x in element.get("xyz", "0 0 0").split())
    rpy = tuple(float(x) for x in element.get("rpy", "0 0 0").split())
    return xyz, rpy


def _parse_geometry(geometry):
    mesh = geometry.find("mesh")
    if mesh is not None:
        scale = mesh.get("scale")
        return {"kind": "mesh", "filename": mesh.get("filename"),
                "scale": tuple(float(x) for x in scale.split()) if scale else (1.0, 1.0, 1.0)}
    box = geometry.find("box")
    if box is not None:
        return {"kind": "box", "size": tuple(float(x) * 0.5 for x in box.get("size").split())}
    sphere = geometry.find("sphere")
    if sphere is not None:
        return {"kind": "sphere", "radius": float(sphere.get("radius"))}
    cylinder = geometry.find("cylinder")
    if cylinder is not None:
        return {"kind": "cylinder", "radius": float(cylinder.get("radius")),
                "half_length": 0.5 * float(cylinder.get("length"))}
    raise AssertionError("unsupported fixture geometry")


def parse_urdf(path: Path) -> Spec:
    spec = Spec()
    root = ET.parse(path).getroot()
    spec.name = root.get("name")
    for link in root.findall("link"):
        name = link.get("name")
        entry = {"name": name, "visuals": [], "collisions": []}
        inertial = link.find("inertial")
        if inertial is not None:
            com, rot = _parse_origin(inertial.find("origin"))
            tensor = inertial.find("inertia")
            values = [float(tensor.get(k)) for k in
                      ("ixx", "iyy", "izz", "ixy", "ixz", "iyz")]
            entry["mass"] = float(inertial.find("mass").get("value"))
            entry["com"] = com
            entry["inertial_rpy"] = rot
            entry["inertia"] = np.array([
                [values[0], values[3], values[4]],
                [values[3], values[1], values[5]],
                [values[4], values[5], values[2]],
            ])
        else:
            entry["mass"] = None
        for visual in link.findall("visual"):
            xyz, rpy = _parse_origin(visual.find("origin"))
            entry["visuals"].append({"geometry": _parse_geometry(visual.find("geometry")),
                                     "xyz": xyz, "rpy": rpy})
        for collision in link.findall("collision"):
            xyz, rpy = _parse_origin(collision.find("origin"))
            entry["collisions"].append({"geometry": _parse_geometry(collision.find("geometry")),
                                        "xyz": xyz, "rpy": rpy})
        spec.links[name] = entry
    for joint in root.findall("joint"):
        name = joint.get("name")
        axis = joint.find("axis")
        limit = joint.find("limit")
        dynamics = joint.find("dynamics")
        spec.joints[name] = {
            "name": name, "type": joint.get("type"),
            "parent": joint.find("parent").get("link"),
            "child": joint.find("child").get("link"),
            "xyz": _parse_origin(joint.find("origin"))[0],
            "rpy": _parse_origin(joint.find("origin"))[1],
            "axis": tuple(float(x) for x in axis.get("xyz").split())
                    if axis is not None else (1.0, 0.0, 0.0),
            "lower": float(limit.get("lower")) if limit is not None
                     and limit.get("lower") is not None else None,
            "upper": float(limit.get("upper")) if limit is not None
                     and limit.get("upper") is not None else None,
            "damping": float(dynamics.get("damping", "0")) if dynamics is not None else 0.0,
            "friction": float(dynamics.get("friction", "0")) if dynamics is not None else 0.0,
        }
    child_links = {joint["child"] for joint in spec.joints.values()}
    roots = [name for name in spec.links if name not in child_links]
    assert len(roots) == 1, f"fixture must be a single tree, roots={roots}"
    spec.root = roots[0]
    for name in spec.links:
        spec.children[name] = []
    for name, joint in spec.joints.items():
        spec.children[joint["parent"]].append((name, joint["child"]))
    return spec


def fk_all(spec: Spec, q: dict) -> dict:
    out = {}

    def walk(link, rotation, translation):
        out[link] = (rotation.copy(), translation.copy())
        for joint_name, child in spec.children[link]:
            joint = spec.joints[joint_name]
            rj = rpy_matrix(*joint["rpy"])
            tj = np.asarray(joint["xyz"], dtype=float)
            value = float(q.get(joint_name, 0.0))
            if joint["type"] in ("revolute", "continuous"):
                r_child, t_child = rj @ axis_angle(joint["axis"], value), tj
            elif joint["type"] == "prismatic":
                r_child = rj
                t_child = tj + rj @ (np.asarray(joint["axis"], dtype=float) * value)
            else:
                r_child, t_child = rj, tj
            walk(child, rotation @ r_child, translation + rotation @ t_child)

    walk(spec.root, np.eye(3), np.zeros(3))
    return out


# ------------------------------------------------------------------ fixtures

TORSO_MASS = 1.3123456789012
TORSO_RANGE_HIGH = 1.123456789
BASE_MASS = 2.1234567890123
AXIS_TORSO = _unit((1.0, 2.0, 0.0))
AXIS_FOREARM = _unit((0.0, 1.0, 3.0))
AXIS_SLIDER = _unit((3.0, -2.0, 1.0))


def _link(name: str, *, mass=None, com=(0.0, 0.0, 0.0), rpy=(0.0, 0.0, 0.0),
          inertia=("0.01", "0.01", "0.01", "0", "0", "0"),
          visuals=(), collisions=()) -> str:
    lines = [f'  <link name="{name}">']
    if mass is not None:
        lines.append("    <inertial>")
        lines.append(f'      <origin xyz="{_fvec(com)}" rpy="{_fvec(rpy)}"/>')
        lines.append(f'      <mass value="{_fmt(mass)}"/>')
        lines.append(
            f'      <inertia ixx="{inertia[0]}" iyy="{inertia[1]}" izz="{inertia[2]}"'
            f' ixy="{inertia[3]}" ixz="{inertia[4]}" iyz="{inertia[5]}"/>')
        lines.append("    </inertial>")
    for visual in visuals:
        lines.append("    <visual>")
        lines.append(f'      <origin xyz="{_fvec(visual[0])}" rpy="{_fvec(visual[1])}"/>')
        lines.append(f"      {visual[2]}")
        lines.append("    </visual>")
    for collision in collisions:
        lines.append("    <collision>")
        lines.append(f'      <origin xyz="{_fvec(collision[0])}" rpy="{_fvec(collision[1])}"/>')
        lines.append(f"      {collision[2]}")
        lines.append("    </collision>")
    lines.append("  </link>")
    return "\n".join(lines)


def _joint(name: str, jtype: str, parent: str, child: str, xyz, rpy, axis=None,
           lower=None, upper=None, effort=None, velocity=None, dynamics=None) -> str:
    lines = [f'  <joint name="{name}" type="{jtype}">']
    lines.append(f'    <origin xyz="{_fvec(xyz)}" rpy="{_fvec(rpy)}"/>')
    lines.append(f'    <parent link="{parent}"/>')
    lines.append(f'    <child link="{child}"/>')
    if axis is not None:
        lines.append(f'    <axis xyz="{_fvec(axis)}"/>')
    limit_attrs = []
    if lower is not None:
        limit_attrs.append(f'lower="{_fmt(lower)}" upper="{_fmt(upper)}"')
    if effort is not None:
        limit_attrs.append(f'effort="{_fmt(effort)}" velocity="{_fmt(velocity)}"')
    if limit_attrs:
        lines.append(f'    <limit {" ".join(limit_attrs)}/>')
    if dynamics is not None:
        lines.append(
            f'    <dynamics damping="{_fmt(dynamics[0])}" friction="{_fmt(dynamics[1])}"/>')
    lines.append("  </joint>")
    return "\n".join(lines)


def _fixture_urdf_text() -> str:
    mesh_a = "../geometry/primary.stl"
    mesh_b = "../geometry/secondary.stl"
    mesh_w = "../geometry/wedge.stl"
    parts = ['<?xml version="1.0"?>',
             '<robot name="qa_bot">',
             _link("base_link", mass=BASE_MASS, com=(0.01, -0.02, 0.03),
                   rpy=(0.123456789012, -0.234567891234, 0.345678912345),
                   inertia=("0.20123456789012", "0.30123456789012", "0.40123456789012",
                            "0.0112345678901", "-0.0212345678901", "0.0312345678901"),
                   visuals=(((0.0, 0.0, 0.05), (0.0, 0.0, 0.0),
                             f'<geometry><mesh filename="{mesh_a}"/></geometry>'),
                            ((0.0, 0.0, 0.09), (0.0, 0.0, 0.0),
                             f'<geometry><mesh filename="{mesh_w}"/></geometry>'))),
             _link("torso_link", mass=TORSO_MASS, com=(0.02, 0.0, 0.04),
                   inertia=("0.08", "0.09", "0.07", "0.0012345678901", "0", "0"),
                   visuals=(((0.02, 0.0, 0.05), (0.0, 0.0, 0.0),
                             '<geometry><sphere radius="0.04"/></geometry>'),
                            ((0.0, 0.03, 0.0), (0.0, 0.3, 0.0),
                             f'<geometry><mesh filename="{mesh_b}" scale="0.5 1.0 2.0"/></geometry>')),
                   collisions=(((0.0, 0.0, 0.05), (0.0, 0.0, 0.0),
                                '<geometry><cylinder radius="0.05" length="0.12"/></geometry>'),)),
             _link("forearm_link", mass=0.7123456789012, com=(0.05, 0.0, 0.02),
                   inertia=("0.02", "0.025", "0.02", "0", "0", "0.0023456789012"),
                   visuals=(((0.1, 0.0, 0.0), (0.15, 0.0, 0.2),
                             f'<geometry><mesh filename="{mesh_b}" scale="0.5 1.0 2.0"/></geometry>'),)),
             _link("slider_link", mass=0.3512345678901, com=(0.0, 0.0, 0.01),
                   inertia=("0.0011", "0.002", "0.0015", "0", "0", "0"),
                   visuals=(((0.02, 0.0, 0.0), (0.0, 0.0, 0.1),
                             '<geometry><box size="0.06 0.03 0.04"/></geometry>'),),
                   collisions=(((0.0, 0.0, 0.0), (0.0, 0.0, 0.0),
                                f'<geometry><mesh filename="{mesh_a}" scale="0.4 0.4 0.4"/></geometry>'),)),
             _link("tool_link", mass=0.1123456789012, com=(0.005, 0.0, 0.0),
                   inertia=("0.0004", "0.0005", "0.0006", "0", "0", "0")),
             _joint("torso_joint", "revolute", "base_link", "torso_link",
                    (0.0, 0.0, 0.2), (0.1, 0.0, 0.2), axis=AXIS_TORSO,
                    lower=-0.4, upper=TORSO_RANGE_HIGH, effort=5.0, velocity=2.0,
                    dynamics=(0.12, 0.03)),
             _joint("forearm_joint", "continuous", "torso_link", "forearm_link",
                    (0.12, -0.03, 0.15), (0.0, 0.25, 0.0), axis=AXIS_FOREARM,
                    effort=3.0, velocity=1.5),
             _joint("slider_joint", "prismatic", "forearm_link", "slider_link",
                    (0.18, 0.02, 0.0), (0.0, 0.0, -0.3), axis=AXIS_SLIDER,
                    lower=0.01, upper=0.25, effort=2.0, velocity=0.3),
             _joint("tool_joint", "fixed", "slider_link", "tool_link",
                    (0.03, 0.0, 0.02), (0.2, 0.1, -0.3)),
             "</robot>", ""]
    return "\n".join(parts)


def write_fixture_source(root: Path) -> dict:
    geometry = root / "geometry"
    geometry.mkdir(parents=True, exist_ok=True)
    (geometry / "primary.stl").write_bytes(binary_stl(box_triangles(0.03, 0.05, 0.02)))
    (geometry / "secondary.stl").write_bytes(binary_stl(box_triangles(0.04, 0.012, 0.02)))
    (geometry / "wedge.stl").write_bytes(binary_stl(wedge_triangles(0.03, 0.02, 0.015)))
    urdf = root / URDF_REL
    urdf.parent.mkdir(parents=True, exist_ok=True)
    urdf.write_text(_fixture_urdf_text(), encoding="utf-8")
    return {"root": root, "urdf": urdf}


def write_single_body_source(root: Path, *, ixx, iyy, izz, ixy, ixz, iyz, rpy) -> Path:
    urdf = root / URDF_REL
    urdf.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join([
        '<?xml version="1.0"?>',
        '<robot name="qa_edge">',
        _link("base_link", mass=1.0, com=(0.0, 0.0, 0.0), rpy=rpy,
              inertia=(_fmt(ixx), _fmt(iyy), _fmt(izz), _fmt(ixy), _fmt(ixz), _fmt(iyz))),
        "</robot>", "",
    ])
    urdf.write_text(text, encoding="utf-8")
    return urdf


def write_config(path: Path, *, base="floating", source="visual_convex",
                 self_collision=True, floor=True) -> Path:
    config = {
        "schema_version": 1,
        "base": base,
        "collision": {"source": source, "self_collision": self_collision,
                      "friction": [0.7, 0.004, 0.0002]},
        "simulation": {"timestep": 0.00071, "gravity": [0.0, 0.0, -9.81],
                       "integrator": "implicitfast"},
        "sites": [],
    }
    if floor:
        config["floor"] = {"height": 0.037123456789,
                           "friction": [0.7, 0.004, 0.0002]}
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- CLI runner

def run_cli(args, *, expect_ok=True, timeout=900):
    env = os.environ.copy()
    entries = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    if env.get("PYTHONPATH"):
        entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(entries)
    command = [sys.executable, "-B", "-m", "description_pipeline", *args]
    result = subprocess.run(command, cwd=str(REPO_ROOT), env=env, timeout=timeout,
                            capture_output=True, text=True)
    if expect_ok and result.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def build_delivery(source_root: Path, urdf_rel: str, config: Path, output: Path):
    return run_cli(["mjcf", "build", str(source_root), "--urdf", urdf_rel,
                    "--config", str(config), "--output", str(output)])


def check_delivery(delivery: Path, *, expect_ok=True):
    return run_cli(["mjcf", "check", str(delivery)], expect_ok=expect_ok)


# ------------------------------------------------------------- model helpers

def load_model(path: Path):
    return mujoco.MjModel.from_xml_path(str(path))


def body_id(model, name: str) -> int:
    index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)
    assert index >= 0, f"body {name!r} not found"
    return index


def joint_id(model, name: str) -> int:
    index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
    assert index >= 0, f"joint {name!r} not found"
    return index


def site_id(model, name: str) -> int:
    index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, name)
    assert index >= 0, f"site {name!r} not found"
    return index


def zero_kinematics(model):
    data = mujoco.MjData(model)
    data.qpos[:] = model.qpos0
    mujoco.mj_kinematics(model, data)
    return data


def set_joint_values(data, model, values: dict) -> None:
    for name, value in values.items():
        jid = joint_id(model, name)
        data.qpos[model.jnt_qposadr[jid]] = value


def _geom_role(model, geom: int) -> str:
    contype = np.asarray(model.geom_contype)
    conaffinity = np.asarray(model.geom_conaffinity)
    ct = int(model.geom_contype[geom])
    ca = int(model.geom_conaffinity[geom])
    pair_mask = (ct & conaffinity) | (contype & ca)
    return "collision" if bool(np.any(pair_mask)) else "visual"


# ----------------------------------------------------------- numeric oracles

def expected_tensor_link(spec: Spec, link: str) -> np.ndarray:
    entry = spec.links[link]
    rotation = rpy_matrix(*entry["inertial_rpy"])
    return rotation @ entry["inertia"] @ rotation.T


def model_tensor_world(model, data, body: int) -> np.ndarray:
    """Spec reconstruction: I = R(q) diag(d) R(q)^T (forward convention)."""
    diag = np.array(model.body_inertia[body], dtype=float)
    rot = quat_to_mat(model.body_iquat[body])
    local = rot @ np.diag(diag) @ rot.T
    body_rot = np.array(data.xmat[body], dtype=float).reshape(3, 3)
    return body_rot @ local @ body_rot.T


def check_inertia(spec: Spec, model, failures: list, tag: str) -> None:
    data = zero_kinematics(model)
    poses = fk_all(spec, {})
    total = 0.0
    for link, entry in spec.links.items():
        if entry["mass"] is None:
            continue
        body = body_id(model, link)
        mass = float(model.body_mass[body])
        expected_mass = float(entry["mass"])
        if abs(mass - expected_mass) > TOL_MASS_ABS + TOL_MASS_REL * abs(expected_mass):
            failures.append(f"{tag}: {link} mass {mass!r} != {expected_mass!r}")
        total += expected_mass
        rotation, translation = poses[link]
        expected_com_world = translation + rotation @ np.asarray(entry["com"])
        com_world = (np.array(data.xpos[body], dtype=float)
                     + np.array(data.xmat[body], dtype=float).reshape(3, 3)
                     @ np.array(model.body_ipos[body], dtype=float))
        if not close_matrix(com_world, expected_com_world, TOL_POSE):
            failures.append(f"{tag}: {link} COM world mismatch")
        expected_local = expected_tensor_link(spec, link)
        expected_world = rotation @ expected_local @ rotation.T
        actual_world = model_tensor_world(model, data, body)
        scale = max(float(np.max(np.abs(expected_world))), 1e-20)
        if float(np.max(np.abs(actual_world - expected_world))) > TOL_TENSOR_ABS + TOL_TENSOR_REL * scale:
            failures.append(f"{tag}: {link} tensor exceeds the 1e-10 gate")
    total_model = float(np.sum(model.body_mass[1:]))
    if abs(total_model - total) > TOL_MASS_ABS + TOL_MASS_REL * total:
        failures.append(f"{tag}: total mass {total_model} != {total}")


def check_kinematics(spec: Spec, model, failures: list, tag: str) -> None:
    movable = [joint for joint in spec.joints.values() if joint["type"] != "fixed"]

    def sample_value(joint, fraction: float) -> float:
        if joint["type"] == "continuous":
            return 0.75 * (2.0 * fraction - 1.0)
        return joint["lower"] + (joint["upper"] - joint["lower"]) * fraction

    samples: list[dict] = [{}]
    for fraction in (3.0 / 8.0, 5.0 / 8.0):
        for joint in movable:
            samples.append({joint["name"]: sample_value(joint, fraction)})
    for fraction in (3.0 / 8.0, 5.0 / 8.0):
        samples.append({joint["name"]: sample_value(joint, fraction) for joint in movable})
    for sample in samples:
        data = mujoco.MjData(model)
        data.qpos[:] = model.qpos0
        set_joint_values(data, model, sample)
        mujoco.mj_kinematics(model, data)
        poses = fk_all(spec, sample)
        for link, (rotation, translation) in poses.items():
            body = body_id(model, link)
            if not close_matrix(data.xpos[body], translation, TOL_POSE):
                failures.append(f"{tag}: FK sample {sample} position mismatch on {link}")
            if not close_matrix(np.array(data.xmat[body], dtype=float).reshape(3, 3),
                                rotation, TOL_POSE):
                failures.append(f"{tag}: FK sample {sample} rotation mismatch on {link}")


def _expected_entries(spec: Spec, source: str) -> dict:
    out = {link: [] for link in spec.links}
    for link, entry in spec.links.items():
        for visual in entry["visuals"]:
            out[link].append(("visual", dict(visual["geometry"],
                                             origin=(visual["xyz"], visual["rpy"]))))
        if source == "visual_convex":
            for visual in entry["visuals"]:
                out[link].append(("collision", dict(visual["geometry"],
                                                    origin=(visual["xyz"], visual["rpy"]))))
        elif source == "urdf":
            for collision in entry["collisions"]:
                out[link].append(("collision", dict(collision["geometry"],
                                                    origin=(collision["xyz"], collision["rpy"]))))
    return out


def _shape_world_frame(shape: dict, link_pose):
    rotation, translation = link_pose
    origin_xyz, origin_rpy = shape["origin"]
    geom_rot = rotation @ rpy_matrix(*origin_rpy)
    center = translation + rotation @ np.asarray(origin_xyz, dtype=float)
    return geom_rot, center


def _compiled_mesh_world_vertices(model, data, geom: int) -> np.ndarray:
    """stored @ geom_xmat.T + geom_xpos (MuJoCo folds mesh preprocessing into
    the geom frame; tetra probe: 4.9e-9 m, double-compensation 0.13 m)."""
    mesh = model.geom_dataid[geom]
    begin = model.mesh_vertadr[mesh]
    vertices = np.array(model.mesh_vert[begin:begin + model.mesh_vertnum[mesh]], dtype=float)
    rot = np.array(data.geom_xmat[geom], dtype=float).reshape(3, 3)
    return vertices @ rot.T + np.array(data.geom_xpos[geom], dtype=float)


def _expected_mesh_world_vertices(shape: dict, link_pose, source_root: Path) -> np.ndarray:
    geom_rot, center = _shape_world_frame(shape, link_pose)
    mesh_path = (source_root / URDF_REL).parent / shape["filename"]
    vertices = read_stl_vertices(mesh_path) * np.array(shape["scale"], dtype=float)
    return vertices @ geom_rot.T + center


def _vertex_sets_match(expected: np.ndarray, compiled: np.ndarray) -> bool:
    a = np.unique(np.round(np.asarray(expected, dtype=float), 9), axis=0)
    b = np.unique(np.round(np.asarray(compiled, dtype=float), 9), axis=0)
    if a.size == 0 or b.size == 0:
        return a.size == b.size
    scale = max(float(np.max(np.abs(a))), float(np.max(np.abs(b))), 1.0)
    tol = TOL_STL_ABS + TOL_STL_REL * scale
    if a.shape == b.shape:
        order_a = np.lexsort((a[:, 2], a[:, 1], a[:, 0]))
        order_b = np.lexsort((b[:, 2], b[:, 1], b[:, 0]))
        if bool(np.all(np.abs(a[order_a] - b[order_b]) <= tol)):
            return True
    for source, target in ((a, b), (b, a)):
        order = np.argsort(target[:, 0], kind="stable")
        target_sorted = target[order]
        xs = target_sorted[:, 0]
        for point in source:
            low = int(np.searchsorted(xs, point[0] - tol, side="left"))
            high = int(np.searchsorted(xs, point[0] + tol, side="right"))
            if high <= low:
                return False
            window = target_sorted[low:high]
            if not bool(np.any(np.max(np.abs(window - point), axis=1) <= tol)):
                return False
    return True


def check_shapes(spec: Spec, source: str, source_root: Path, model, failures: list, tag: str) -> None:
    data = zero_kinematics(model)
    poses = fk_all(spec, {})
    for link, entries in _expected_entries(spec, source).items():
        if not entries:
            continue
        body = body_id(model, link)
        pools = {
            role: [g for g in range(model.ngeom)
                   if int(model.geom_bodyid[g]) == body and _geom_role(model, g) == role]
            for role in ("visual", "collision")
        }
        for role, shape in entries:
            expected_type = {
                "mesh": mujoco.mjtGeom.mjGEOM_MESH,
                "box": mujoco.mjtGeom.mjGEOM_BOX,
                "sphere": mujoco.mjtGeom.mjGEOM_SPHERE,
                "cylinder": mujoco.mjtGeom.mjGEOM_CYLINDER,
            }[shape["kind"]]
            geom_rot, center = _shape_world_frame(shape, poses[link])
            matched = None
            for geom in list(pools[role]):
                if model.geom_type[geom] != expected_type:
                    continue
                if shape["kind"] == "mesh":
                    ok = _vertex_sets_match(
                        _expected_mesh_world_vertices(shape, poses[link], source_root),
                        _compiled_mesh_world_vertices(model, data, geom))
                else:  # primitives compare centre/orientation/size only
                    size = np.asarray(model.geom_size[geom], dtype=float)
                    if shape["kind"] == "box":
                        corners = (np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
                                             [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], dtype=float)
                                   * size)
                        got = (corners @ np.array(data.geom_xmat[geom], dtype=float).reshape(3, 3).T
                               + np.array(data.geom_xpos[geom], dtype=float))
                        want = ((np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
                                           [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], dtype=float)
                                 * np.asarray(shape["size"], dtype=float))
                                @ geom_rot.T + center)
                        ok = _vertex_sets_match(want, got)
                    elif shape["kind"] == "sphere":
                        ok = (close_matrix(data.geom_xpos[geom], center, TOL_POSE)
                              and abs(float(size[0]) - shape["radius"]) <= 1e-12)
                    else:
                        want_axis = geom_rot[:, 2]
                        got_axis = np.array(data.geom_xmat[geom], dtype=float).reshape(3, 3)[:, 2]
                        ok = (close_matrix(data.geom_xpos[geom], center, TOL_POSE)
                              and abs(float(np.dot(got_axis, want_axis))) >= 1.0 - 1e-9
                              and abs(float(size[0]) - shape["radius"]) <= 1e-12
                              and abs(float(size[1]) - shape["half_length"]) <= 1e-12)
                if ok:
                    matched = geom
                    pools[role].remove(geom)
                    break
            if matched is None:
                failures.append(f"{tag}: no {role} geom on {link} matches {shape['kind']}")


# ---------------------------------------------------------------- resealing

def reseal_delivery(delivery: Path, replacements: dict, length_updates: dict | None = None) -> None:
    manifest_path = delivery / "manifest.json"
    report_path = delivery / "reports/quality.json"
    old_manifest_sha = _sha256_file(manifest_path)
    manifest_text = manifest_path.read_text(encoding="utf-8")
    report_text = report_path.read_text(encoding="utf-8")

    def apply_common(text: str) -> str:
        for old_len, new_len in (length_updates or {}).items():
            pattern = re.compile(rf"(?<![0-9a-f]){old_len}(?!\d)")
            if len(pattern.findall(text)) > 1:
                raise AssertionError(f"byte length {old_len} is ambiguous in the sealed records")
            text = pattern.sub(str(new_len), text)
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    manifest_text = apply_common(manifest_text)
    report_text = apply_common(report_text)
    manifest_path.write_text(manifest_text, encoding="utf-8")
    report_text = report_text.replace(old_manifest_sha, _sha256_file(manifest_path))
    report_path.write_text(report_text, encoding="utf-8")


def _tag_with(text: str, token: str):
    if text.count(token) != 1:
        raise AssertionError(f"token {token!r} not unique; calibration needed")
    index = text.find(token)
    start = text.rfind("<", 0, index)
    end = text.find(">", index)
    return start, end, text[start:end + 1]


def _set_attribute(text: str, token: str, attribute: str, value: str) -> str:
    start, end, tag = _tag_with(text, token)
    pattern = re.compile(rf'{attribute}="[^"]*"')
    if not pattern.search(tag):
        raise AssertionError(f"{attribute} missing on the anchored tag; calibration needed")
    new_tag = pattern.sub(f'{attribute}="{value}"', tag, count=1)
    return text[:start] + new_tag + text[end + 1:]


def _rotate_geom_about_z180(text: str, token: str) -> str:
    start, end, tag = _tag_with(text, token)
    match = re.search(r'quat="([^"]*)"', tag)
    if match:
        w, x, y, z = (float(v) for v in match.group(1).split())
        new_quat = (-z, y, -x, w)
        if new_quat[0] < 0:
            new_quat = tuple(-v for v in new_quat)
        replacement = 'quat="' + " ".join(repr(float(v)) for v in new_quat) + '"'
        new_tag = tag[:match.start()] + replacement + tag[match.end():]
    else:
        insertion = ' quat="0 0 0 1"'
        new_tag = tag[:-2] + insertion + "/>" if tag.endswith("/>") else tag[:-1] + insertion + ">"
    return text[:start] + new_tag + text[end + 1:]


# ------------------------------------------------- support-site mathematics

class SupportDerivationError(Exception):
    pass


def convex_hull_2d(points: np.ndarray) -> np.ndarray:
    pts = sorted({(float(x), float(y)) for x, y in points})
    if len(pts) < 3:
        return np.array(pts)

    def half(sequence):
        out = []
        for point in sequence:
            while len(out) >= 2:
                (x1, y1), (x2, y2) = out[-2], out[-1]
                if (x2 - x1) * (point[1] - y1) - (y2 - y1) * (point[0] - x1) <= 0:
                    out.pop()
                else:
                    break
            out.append(point)
        return out

    lower = half(pts)
    upper = half(list(reversed(pts)))
    return np.array(lower[:-1] + upper[:-1])


def polygon_area_centroid(hull: np.ndarray):
    x, y = hull[:, 0], hull[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    cross = x * y2 - x2 * y
    area2 = float(np.sum(cross))
    if abs(area2) <= 1e-18:
        raise SupportDerivationError("degenerate contact hull")
    cx = float(np.sum((x + x2) * cross)) / (3.0 * area2)
    cy = float(np.sum((y + y2) * cross)) / (3.0 * area2)
    return abs(area2) / 2.0, np.array([cx, cy])


def derive_support(triangles: np.ndarray):
    """Candidate planes from source triangles; contact hull over ALL selected
    vertices within the plane tolerance (ruling 2026-10-16)."""
    vertices = triangles.reshape(-1, 3)
    candidates = []
    for triangle in triangles:
        normal = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
        length = float(np.linalg.norm(normal))
        if length <= 1e-16:
            continue
        normal = normal / length
        if normal[2] < 0:
            normal = -normal
        if normal[2] <= 0:
            continue
        offset = float(np.dot(normal, triangle[0]))
        if float(np.min(vertices @ normal - offset)) < -SUPPORT_EPS_SUPPORT:
            continue
        candidates.append((normal, offset))
    if not candidates:
        raise SupportDerivationError("no supporting triangle plane")
    faces = []
    for normal, offset in candidates:
        for face in faces:
            if (abs(float(np.dot(face["n"], normal)) - 1.0) <= SUPPORT_EPS_COPLANAR
                    and abs(face["c"] - offset) <= SUPPORT_EPS_COPLANAR):
                break
        else:
            faces.append({"n": normal, "c": offset})
    valid = []
    for face in faces:
        distances = vertices @ face["n"] - face["c"]
        contact = vertices[np.abs(distances) <= SUPPORT_EPS_CONTACT]
        if len(contact) < 3:
            continue
        basis_u = np.cross(face["n"], np.array([0.0, 0.0, 1.0]))
        if float(np.linalg.norm(basis_u)) < 1e-12:
            basis_u = np.cross(face["n"], np.array([1.0, 0.0, 0.0]))
        basis_u = basis_u / np.linalg.norm(basis_u)
        basis_v = np.cross(face["n"], basis_u)
        base = contact[0]
        planar = np.column_stack(((contact - base) @ basis_u, (contact - base) @ basis_v))
        try:
            area, centroid2 = polygon_area_centroid(convex_hull_2d(planar))
        except SupportDerivationError:
            continue
        if area <= SUPPORT_EPS_AREA:
            continue
        centroid = base + centroid2[0] * basis_u + centroid2[1] * basis_v
        valid.append({"area": area, "centroid": centroid, "n": face["n"]})
    if not valid:
        raise SupportDerivationError("degenerate or empty contact face")
    valid.sort(key=lambda face: face["area"], reverse=True)
    best = valid[0]
    if len(valid) > 1:
        second = valid[1]
        if second["area"] >= best["area"] * (1.0 - SUPPORT_EPS_TIE) - SUPPORT_EPS_AREA:
            raise SupportDerivationError("ambiguous support planes (area tie)")
    x_world = np.array([1.0, 0.0, 0.0]) - best["n"][0] * best["n"]
    x_world = x_world / np.linalg.norm(x_world)
    return best["centroid"], best["n"], x_world


def support_geometry_points(spec: Spec, link: str, source: str, source_root: Path) -> np.ndarray:
    items = (spec.links[link]["collisions"] if source == "urdf" else spec.links[link]["visuals"])
    rotation, translation = fk_all(spec, {})[link]
    clouds = []
    for item in items:
        geometry = item["geometry"]
        if geometry["kind"] != "mesh":
            continue
        mesh_path = (source_root / URDF_REL).parent / geometry["filename"]
        vertices = read_stl_vertices(mesh_path) * np.array(geometry["scale"], dtype=float)
        origin_rot = rpy_matrix(*item["rpy"])
        clouds.append((vertices @ origin_rot.T + np.asarray(item["xyz"], dtype=float))
                      @ rotation.T + translation)
    if not clouds:
        raise SupportDerivationError("no triangle mesh in the selected geometry")
    return np.concatenate(clouds)


def _quad(corners):
    a, b, c, d = (np.asarray(point, dtype=float) for point in corners)
    return [(*a, *b, *c), (*a, *c, *d)]


def _frame_box_triangles(center, basis_u, basis_v, basis_n, half):
    hx, hy, hz = half
    corners = []
    for ix in (0, 1):
        for iy in (0, 1):
            for iz in (0, 1):
                corners.append(np.asarray(center, dtype=float)
                               + (2 * ix - 1) * hx * np.asarray(basis_u, dtype=float)
                               + (2 * iy - 1) * hy * np.asarray(basis_v, dtype=float)
                               + (2 * iz - 1) * hz * np.asarray(basis_n, dtype=float))

    def index(ix, iy, iz):
        return (ix << 2) | (iy << 1) | iz

    faces = [
        (index(0, 0, 0), index(1, 0, 0), index(1, 1, 0)),
        (index(0, 0, 0), index(1, 1, 0), index(0, 1, 0)),
        (index(0, 0, 1), index(0, 1, 1), index(1, 1, 1)),
        (index(0, 0, 1), index(1, 1, 1), index(1, 0, 1)),
        (index(0, 0, 0), index(0, 0, 1), index(1, 0, 1)),
        (index(0, 0, 0), index(1, 0, 1), index(1, 0, 0)),
        (index(1, 0, 0), index(1, 0, 1), index(1, 1, 1)),
        (index(1, 0, 0), index(1, 1, 1), index(1, 1, 0)),
        (index(1, 1, 0), index(1, 1, 1), index(0, 1, 1)),
        (index(1, 1, 0), index(0, 1, 1), index(0, 1, 0)),
        (index(0, 1, 0), index(0, 1, 1), index(0, 0, 1)),
        (index(0, 1, 0), index(0, 0, 1), index(0, 0, 0)),
    ]
    triangles = []
    for face in faces:
        points = [corners[i] for i in face]
        triangles.append(tuple(float(c) for point in points for c in point))
    return triangles


def write_support_source(root: Path, meshes: dict, *, link_xyz, link_rpy,
                         visuals, collisions) -> Path:
    geometry = root / "geometry"
    geometry.mkdir(parents=True, exist_ok=True)
    for filename, triangles in meshes.items():
        (geometry / filename).write_bytes(binary_stl(triangles))
    urdf = root / URDF_REL
    urdf.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        '<?xml version="1.0"?>',
        '<robot name="qa_support">',
        _link("base_link", mass=1.0, inertia=("0.02", "0.02", "0.02", "0", "0", "0")),
        _link("foot_link", mass=0.5, com=(0.0, 0.0, 0.02),
              inertia=("0.004", "0.005", "0.004", "0", "0", "0"),
              visuals=tuple(visuals), collisions=tuple(collisions)),
        _joint("foot_joint", "fixed", "base_link", "foot_link", link_xyz, link_rpy),
        "</robot>", "",
    ]
    urdf.write_text("\n".join(parts), encoding="utf-8")
    return urdf


def write_support_config(path: Path, *, source, support_sites, floor_height="supports") -> Path:
    config = {
        "schema_version": 1,
        "base": "floating",
        "collision": {"source": source, "self_collision": False,
                      "friction": [0.7, 0.004, 0.0002]},
        "simulation": {"timestep": 0.00071, "gravity": [0.0, 0.0, -9.81],
                       "integrator": "implicitfast"},
        "sites": [],
        "support_sites": support_sites,
        "floor": {"height": floor_height, "friction": [0.7, 0.004, 0.0002]},
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ fixtures

FIX = {}


def setUpModule():
    if mujoco is None:
        raise unittest.SkipTest("mujoco is not available")
    tmp = tempfile.TemporaryDirectory(prefix="mjcf-oracles-")
    FIX["tmp"] = tmp
    base = Path(tmp.name)
    FIX["base"] = base
    source_root = base / "source"
    source_root.mkdir()
    FIX.update(write_fixture_source(source_root))
    config = write_config(base / "config-a.json", base="floating", source="visual_convex",
                          self_collision=True, floor=True)
    delivery = base / "delivery-a"
    build_delivery(FIX["root"], URDF_REL, config, delivery)
    FIX["delivery"] = delivery
    FIX["spec"] = parse_urdf(FIX["urdf"])


def tearDownModule():
    tmp = FIX.pop("tmp", None)
    if tmp is not None:
        tmp.cleanup()


class NumericOracleTest(unittest.TestCase):
    """URDF -> actual MjModel numeric oracles (inertia/FK/mesh vertices)."""

    def test_inertia_gate_mass_com_tensor(self):
        model = load_model(FIX["delivery"] / "robot.xml")
        failures = []
        check_inertia(FIX["spec"], model, failures, "inertia")
        self.assertEqual([], failures)

    def test_kinematics_samples_match_urdf_fk(self):
        model = load_model(FIX["delivery"] / "robot.xml")
        failures = []
        check_kinematics(FIX["spec"], model, failures, "fk")
        self.assertEqual([], failures)

    def test_shapes_world_vertex_sets(self):
        model = load_model(FIX["delivery"] / "robot.xml")
        failures = []
        check_shapes(FIX["spec"], "visual_convex", FIX["root"], model, failures, "shapes")
        self.assertEqual([], failures)

    def test_tamper_geom_orientation_bbox_invariant_resealed_rejected(self):
        target = FIX["base"] / "tamper-rot180"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(FIX["delivery"], target)
        path = target / "robot.xml"
        old_len = path.stat().st_size
        old_sha = _sha256_file(path)
        text = _rotate_geom_about_z180(path.read_text(encoding="utf-8"), '"base_link__visual_1"')
        path.write_text(text, encoding="utf-8")
        reseal_delivery(target, {old_sha: _sha256_file(path)},
                        {old_len: path.stat().st_size})
        result = check_delivery(target, expect_ok=False)
        self.assertNotEqual(0, result.returncode,
                            "bbox-invariant vertex-set change must be rejected")


class InertiaEdgeTest(unittest.TestCase):
    """Rotated near-degenerate and tiny-scale full tensors through the pipeline."""

    @classmethod
    def setUpClass(cls):
        cls.base = FIX["base"]
        cls.cases = {}
        variants = {
            "near_degenerate": {
                "ixx": 0.30, "iyy": 0.300123456, "izz": 0.5,
                "ixy": 0.01123456789, "ixz": -0.02123456789, "iyz": 0.03123456789,
                "rpy": (0.31, -0.27, 0.44)},
            "tiny_scale": {
                "ixx": 3.0e-7, "iyy": 3.00123456e-7, "izz": 5.0e-7,
                "ixy": 1.123456789e-8, "ixz": -2.123456789e-8, "iyz": 3.123456789e-8,
                "rpy": (0.31, -0.27, 0.44)},
            "isotropic_control": {
                "ixx": 0.37, "iyy": 0.37, "izz": 0.37,
                "ixy": 0.0, "ixz": 0.0, "iyz": 0.0,
                "rpy": (0.31, -0.27, 0.44)},
        }
        for name, values in variants.items():
            root = cls.base / f"edge-{name}"
            root.mkdir(parents=True, exist_ok=True)
            write_single_body_source(root, **values)
            config = write_config(cls.base / f"edge-{name}.json", base="floating",
                                  source="none", self_collision=False, floor=False)
            delivery = cls.base / f"edge-delivery-{name}"
            build_delivery(root, URDF_REL, config, delivery)
            cls.cases[name] = {"root": root, "delivery": delivery}

    def _check(self, name):
        entry = self.cases[name]
        spec = parse_urdf(entry["root"] / URDF_REL)
        model = load_model(entry["delivery"] / "robot.xml")
        failures = []
        check_inertia(spec, model, failures, f"edge-{name}")
        self.assertEqual([], failures)

    def test_near_degenerate_tensor(self):
        self._check("near_degenerate")

    def test_tiny_scale_tensor(self):
        self._check("tiny_scale")

    def test_isotropic_control(self):
        self._check("isotropic_control")


class SupportOracleTest(unittest.TestCase):
    """Support-site mathematics and resealed site-frame counterexamples."""

    @classmethod
    def setUpClass(cls):
        base = FIX["base"] / "support"
        base.mkdir(exist_ok=True)
        cls.base = base
        cls.positive = {}
        cls.negative = {}

        def build_case(name, meshes, *, link_xyz=(0.0, 0.0, 0.1), link_rpy=(0.0, 0.0, 0.0),
                       visuals, source="visual_convex", support_sites,
                       floor_height="supports", expect_ok=True):
            root = base / name
            write_support_source(root, meshes, link_xyz=link_xyz, link_rpy=link_rpy,
                                 visuals=visuals, collisions=())
            config = write_support_config(base / f"{name}.json", source=source,
                                          support_sites=support_sites,
                                          floor_height=floor_height)
            delivery = base / f"{name}-delivery"
            result = run_cli(["mjcf", "build", str(root), "--urdf", URDF_REL,
                              "--config", str(config), "--output", str(delivery)],
                             expect_ok=expect_ok)
            if expect_ok:
                cls.positive[name] = {"root": root, "delivery": delivery,
                                      "config": json.loads(config.read_text())}
            else:
                cls.negative[name] = result.returncode

        def mesh_entry(filename, xyz=(0.0, 0.0, 0.0), rpy=(0.0, 0.0, 0.0)):
            return (xyz, rpy, f'<geometry><mesh filename="../geometry/{filename}"/></geometry>')

        unit_x, unit_y, unit_z = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)

        tilt = _unit((0.12, -0.18, 1.0))
        axis_u = _unit(np.cross(tilt, (0.0, 0.0, 1.0)))
        axis_v = np.cross(tilt, axis_u)
        center = np.array([0.01, -0.005, 0.0])
        slab = _frame_box_triangles(center + 0.004 * tilt, axis_u, axis_v, tilt,
                                    (0.06, 0.035, 0.004))
        heel = (0.0, 0.0, 0.06, 0.02, 0.0, 0.06, 0.0, 0.02, 0.06)
        tilted_mesh = {"sole.stl": [*slab, heel]}
        build_case("tilted", tilted_mesh, link_xyz=(0.0, 0.0, 0.12),
                   link_rpy=(0.35, -0.2, 0.4), visuals=[mesh_entry("sole.stl")],
                   support_sites=[{"name": "sole_ref", "link": "foot_link"}])

        plate1 = _frame_box_triangles((-0.04, 0.0, 0.003), unit_x, unit_y, unit_z,
                                      (0.02, 0.03, 0.003))
        plate2 = _frame_box_triangles((0.04, 0.0, 0.003), unit_x, unit_y, unit_z,
                                      (0.02, 0.03, 0.003))
        build_case("multipart", {"plate1.stl": plate1, "plate2.stl": plate2},
                   link_xyz=(0.0, 0.0, 0.1), link_rpy=(0.2, 0.1, -0.15),
                   visuals=[mesh_entry("plate1.stl"), mesh_entry("plate2.stl")],
                   support_sites=[{"name": "sole_ref", "link": "foot_link"}])

        pad = _quad([(-0.02, -0.02, 0.0), (0.02, -0.02, 0.0),
                     (0.02, 0.02, 0.0), (-0.02, 0.02, 0.0)])
        flap = _quad([(0.10, -0.03, 0.05), (0.16, -0.03, 0.068),
                      (0.16, 0.03, 0.068), (0.10, 0.03, 0.05)])
        build_case("overhang", {"shoe.stl": [*pad, *flap]},
                   link_xyz=(0.0, 0.0, 0.08), visuals=[mesh_entry("shoe.stl")],
                   support_sites=[{"name": "sole_ref", "link": "foot_link"}])

        pad7 = _frame_box_triangles((0.0, 0.0, 0.003), unit_x, unit_y, unit_z,
                                    (0.03, 0.02, 0.003))
        apex7 = (0.10, 0.0, 0.0)
        base_a, base_b, base_c = (0.09, -0.01, 0.04), (0.11, -0.01, 0.04), (0.10, 0.01, 0.04)
        spike7 = [(*base_a, *base_b, *base_c),
                  (*base_a, *apex7, *base_b),
                  (*base_b, *apex7, *base_c),
                  (*base_c, *apex7, *base_a)]
        build_case("touching_spike", {"spike.stl": [*pad7, *spike7]},
                   link_xyz=(0.0, 0.0, 0.07), visuals=[mesh_entry("spike.stl")],
                   support_sites=[{"name": "sole_ref", "link": "foot_link"}])

        ridge = (_quad([(0.0, -0.03, 0.0), (0.0, 0.03, 0.0),
                        (-0.05, 0.03, 0.03), (-0.05, -0.03, 0.03)])
                 + _quad([(0.0, -0.03, 0.0), (0.05, -0.03, 0.03),
                          (0.05, 0.03, 0.03), (0.0, 0.03, 0.0)]))
        build_case("ambiguous_planes", {"ridge.stl": ridge},
                   visuals=[mesh_entry("ridge.stl")],
                   support_sites=[{"name": "sole_ref", "link": "foot_link"}],
                   expect_ok=False)

        plate = _quad([(-0.06, -0.04, 0.0), (0.06, -0.04, 0.0),
                       (0.06, 0.04, 0.0), (-0.06, 0.04, 0.0)])
        spike = [(0.09, -0.005, 0.0, 0.09, -0.005, -0.02, 0.09, 0.005, -0.02),
                 (0.09, -0.005, 0.0, 0.09, 0.005, -0.02, 0.09, 0.005, 0.0)]
        build_case("no_support", {"spiked.stl": [*plate, *spike]},
                   visuals=[mesh_entry("spiked.stl")],
                   support_sites=[{"name": "sole_ref", "link": "foot_link"}],
                   expect_ok=False)

        build_case("degenerate", {"line.stl": [(0.0, 0.0, 0.0, 0.1, 0.0, 0.0, 0.2, 0.0, 0.0)]},
                   visuals=[mesh_entry("line.stl")],
                   support_sites=[{"name": "sole_ref", "link": "foot_link"}],
                   expect_ok=False)

    def _verify_positive(self, name):
        entry = self.positive[name]
        spec = parse_urdf(entry["root"] / URDF_REL)
        delivery = entry["delivery"]
        self.assertEqual(0, check_delivery(delivery).returncode)
        triangles = support_geometry_points(
            spec, "foot_link", "visual_convex", entry["root"]).reshape(-1, 3, 3)
        centroid, z_world, x_world = derive_support(triangles)
        model = load_model(delivery / "robot.xml")
        data = zero_kinematics(model)
        sid = site_id(model, "sole_ref")
        self.assertTrue(close_matrix(data.site_xpos[sid], centroid, 1e-9),
                        f"{name}: site position != independent derivation")
        expected_rot = np.column_stack([x_world, np.cross(z_world, x_world), z_world])
        got_rot = np.array(data.site_xmat[sid], dtype=float).reshape(3, 3)
        self.assertTrue(close_matrix(got_rot, expected_rot, 1e-9),
                        f"{name}: site orientation != independent derivation")
        if entry["config"]["floor"]["height"] == "supports":
            points = support_geometry_points(spec, "foot_link", "visual_convex", entry["root"])
            scene = load_model(delivery / "scene.xml")
            planes = [g for g in range(scene.ngeom)
                      if scene.geom_type[g] == mujoco.mjtGeom.mjGEOM_PLANE]
            self.assertEqual(1, len(planes))
            self.assertAlmostEqual(float(scene.geom_pos[planes[0]][2]),
                                   float(np.min(points[:, 2])), delta=1e-12)

    def test_tilted_sole_rotated_zero_pose(self):
        self._verify_positive("tilted")

    def test_multipart_coplanar_merge(self):
        self._verify_positive("multipart")

    def test_largest_facet_not_supporting_is_skipped(self):
        self._verify_positive("overhang")

    def test_touching_far_apex_included_in_contact_hull(self):
        entry = self.positive["touching_spike"]
        spec = parse_urdf(entry["root"] / URDF_REL)
        triangles = support_geometry_points(
            spec, "foot_link", "visual_convex", entry["root"]).reshape(-1, 3, 3)
        centroid, _, _ = derive_support(triangles)
        self.assertGreater(abs(float(centroid[0])), 0.01,
                           "oracle: the far touching apex must extend the hull")
        self._verify_positive("touching_spike")

    def test_support_negatives_rejected(self):
        for name, returncode in self.negative.items():
            with self.subTest(case=name):
                self.assertNotEqual(0, returncode, f"{name} must be rejected at build time")

    def _copy(self, name: str) -> Path:
        """Tilted fixture: its site frame is non-trivial, so forcing an identity
        quaternion (orientation-only) or a link-frame position stays inside any
        bounding box while violating the derived plane/centroid semantics."""
        target = self.base / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(self.positive["tilted"]["delivery"], target)
        return target

    def test_tamper_site_orientation_resealed_rejected(self):
        target = self._copy("tamper-site-quat")
        path = target / "robot.xml"
        old_len = path.stat().st_size
        old_sha = _sha256_file(path)
        text = _set_attribute(path.read_text(encoding="utf-8"), '"sole_ref"', "quat", "0 0 0 1")
        path.write_text(text, encoding="utf-8")
        reseal_delivery(target, {old_sha: _sha256_file(path)},
                        {old_len: path.stat().st_size})
        result = check_delivery(target, expect_ok=False)
        self.assertNotEqual(0, result.returncode,
                            "orientation-only support-site tamper not rejected")

    def test_tamper_site_position_resealed_rejected(self):
        target = self._copy("tamper-site-pos")
        path = target / "robot.xml"
        old_len = path.stat().st_size
        old_sha = _sha256_file(path)
        text = _set_attribute(path.read_text(encoding="utf-8"), '"sole_ref"', "pos", "0.01 0 0")
        path.write_text(text, encoding="utf-8")
        reseal_delivery(target, {old_sha: _sha256_file(path)},
                        {old_len: path.stat().st_size})
        result = check_delivery(target, expect_ok=False)
        self.assertNotEqual(0, result.returncode,
                            "centroid-shift support-site tamper not rejected")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
