"""URDF → MJCF converter for the downstream MuJoCo consumer (docs/mjcf.md).

One entry point (:func:`build`) owns the whole conversion: it freezes the source
reference closure, projects the immutable URDF straight into MuJoCo XML, calls the
complete independent check on a staged directory, and only then commits the new
delivery directory.  There is no second complete model and no persistent state;
the URDF tree lives only inside one build call.
"""

from __future__ import annotations

import math
import os
import posixpath
import shutil
import tempfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np

from .geometry import stl
from .io import PipelineError, artifact_path_parts, confined, file_digest, format_number, parse_data, write_json
from .mjcf_contract import (
    INPUT_PREFIX,
    MANIFEST_JSON,
    MUJOCO_VERSION,
    REPORT_JSON,
    ROBOT_XML,
    SCENE_XML,
    SIMULATION_JSON,
    build_manifest,
    geom_name,
    validate_config,
)

#: The URDF joint kinds this converter preserves; anything else is refused by name.
_SUPPORTED_JOINT_TYPES = ("revolute", "continuous", "prismatic", "fixed")
#: Axis directions must be unit vectors; the tolerance only absorbs decimal round-off.
_AXIS_UNIT_TOLERANCE = 1e-9
#: Inertia tensors must be physical; the slack only absorbs numeric round-off.
_INERTIA_TRIANGLE_TOLERANCE = 1e-9
#: The only child elements each URDF element may carry; anything else is refused.
_ALLOWED_CHILDREN: dict[str, frozenset[str]] = {
    "robot": frozenset({"link", "joint", "material"}),
    "link": frozenset({"inertial", "visual", "collision"}),
    "inertial": frozenset({"origin", "mass", "inertia"}),
    "visual": frozenset({"origin", "geometry", "material"}),
    "collision": frozenset({"origin", "geometry"}),
    "geometry": frozenset({"box", "cylinder", "sphere", "mesh"}),
    "joint": frozenset({"parent", "child", "origin", "axis", "limit", "dynamics"}),
    "material": frozenset({"color"}),
    "origin": frozenset(),
    "mass": frozenset(),
    "inertia": frozenset(),
    "color": frozenset(),
    "box": frozenset(),
    "cylinder": frozenset(),
    "sphere": frozenset(),
    "mesh": frozenset(),
    "parent": frozenset(),
    "child": frozenset(),
    "axis": frozenset(),
    "limit": frozenset(),
    "dynamics": frozenset(),
}


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _check_children(element: ET.Element, what: str) -> None:
    allowed = _ALLOWED_CHILDREN[_strip_namespace(element.tag)]
    for child in element:
        tag = _strip_namespace(child.tag)
        if tag not in allowed:
            raise PipelineError(f"{what}: unsupported element <{tag}>")


def _unique_child(element: ET.Element, tag: str, what: str) -> ET.Element | None:
    found = [child for child in element if _strip_namespace(child.tag) == tag]
    if len(found) > 1:
        raise PipelineError(f"{what}: repeated <{tag}> element")
    return found[0] if found else None


def _required_child(element: ET.Element, tag: str, what: str) -> ET.Element:
    child = _unique_child(element, tag, what)
    if child is None:
        raise PipelineError(f"{what}: missing <{tag}> element")
    return child


def _finite(value: str, what: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise PipelineError(f"{what}: {value!r} is not a number") from error
    if not math.isfinite(number):
        raise PipelineError(f"{what}: {value!r} is not finite")
    return number


def _numbers(element: ET.Element, attribute: str, count: int, what: str) -> tuple[float, ...]:
    text = element.get(attribute)
    if text is None:
        raise PipelineError(f"{what}: missing {attribute!r} attribute")
    fields = text.split()
    if len(fields) != count:
        raise PipelineError(f"{what}: {attribute!r} must hold {count} numbers, found {text!r}")
    return tuple(_finite(field, f"{what} {attribute}") for field in fields)


def _optional_numbers(element: ET.Element, attribute: str, count: int, what: str) -> tuple[float, ...] | None:
    return None if element.get(attribute) is None else _numbers(element, attribute, count, what)


def _optional_number(element: ET.Element, attribute: str, what: str) -> float | None:
    return None if element.get(attribute) is None else _finite(element.get(attribute), f"{what} {attribute}")


def _element_name(element: ET.Element, what: str) -> str:
    name = element.get("name")
    if not name:
        raise PipelineError(f"{what}: a name attribute is required")
    if any(character.isspace() or ord(character) < 32 for character in name):
        raise PipelineError(f"{what}: name {name!r} contains whitespace or control characters")
    return name


# --------------------------------------------------------------------------- #
# URDF parsing
# --------------------------------------------------------------------------- #


def _parse_origin(element: ET.Element, what: str) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    origin = _unique_child(element, "origin", what)
    if origin is None:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    _check_children(origin, f"{what} origin")
    xyz = _optional_numbers(origin, "xyz", 3, f"{what} origin")
    rpy = _optional_numbers(origin, "rpy", 3, f"{what} origin")
    return (
        xyz if xyz is not None else (0.0, 0.0, 0.0),
        rpy if rpy is not None else (0.0, 0.0, 0.0),
    )


def _positive(value: float, what: str) -> float:
    if value <= 0:
        raise PipelineError(f"{what}: must be positive, found {format_number(value)}")
    return value


def _parse_geometry(element: ET.Element, what: str) -> dict[str, Any]:
    geometry = _required_child(element, "geometry", what)
    _check_children(geometry, f"{what} geometry")
    shapes = list(geometry)
    if len(shapes) != 1:
        raise PipelineError(f"{what} geometry: exactly one shape is required")
    shape = shapes[0]
    tag = _strip_namespace(shape.tag)
    _check_children(shape, f"{what} <{tag}>")
    if tag == "box":
        size = _numbers(shape, "size", 3, f"{what} box")
        return {"kind": "box", "size": tuple(_positive(value, f"{what} box size") for value in size)}
    if tag == "cylinder":
        radius = _positive(_numbers(shape, "radius", 1, f"{what} cylinder")[0], f"{what} cylinder radius")
        length = _positive(_numbers(shape, "length", 1, f"{what} cylinder")[0], f"{what} cylinder length")
        return {"kind": "cylinder", "radius": radius, "length": length}
    if tag == "sphere":
        radius = _positive(_numbers(shape, "radius", 1, f"{what} sphere")[0], f"{what} sphere radius")
        return {"kind": "sphere", "radius": radius}
    filename = shape.get("filename")
    if not filename:
        raise PipelineError(f"{what} mesh: a filename attribute is required")
    scale = _optional_numbers(shape, "scale", 3, f"{what} mesh") or (1.0, 1.0, 1.0)
    return {
        "kind": "mesh",
        "filename": filename,
        "relative": None,
        "scale": tuple(_positive(value, f"{what} mesh scale") for value in scale),
    }


def _principal_eigenvalues(ixx: float, ixy: float, ixz: float, iyy: float, iyz: float, izz: float):
    """Closed-form eigenvalues of the symmetric 3×3 inertia tensor."""

    first = ixy * ixy + ixz * ixz + iyz * iyz
    mean = (ixx + iyy + izz) / 3
    spread = (ixx - mean) ** 2 + (iyy - mean) ** 2 + (izz - mean) ** 2 + 2 * first
    if spread <= 0:
        return (mean, mean, mean)
    radius = math.sqrt(spread / 6)
    bxx, bxy, bxz = (ixx - mean) / radius, ixy / radius, ixz / radius
    byy, byz, bzz = (iyy - mean) / radius, iyz / radius, (izz - mean) / radius
    determinant = bxx * (byy * bzz - byz * byz) - bxy * (bxy * bzz - byz * bxz) + bxz * (bxy * byz - byy * bxz)
    angle = math.acos(max(-1.0, min(1.0, determinant / 2))) / 3
    largest = mean + 2 * radius * math.cos(angle)
    smallest = mean + 2 * radius * math.cos(angle + 2 * math.pi / 3)
    middle = 3 * mean - largest - smallest
    return tuple(sorted((smallest, middle, largest)))


def _parse_inertial(element: ET.Element, what: str) -> dict[str, Any] | None:
    inertial = _unique_child(element, "inertial", what)
    if inertial is None:
        return None
    label = f"{what} inertial"
    _check_children(inertial, label)
    xyz, rpy = _parse_origin(inertial, label)
    mass = _positive(
        _numbers(_required_child(inertial, "mass", label), "value", 1, f"{label} mass")[0], f"{label} mass"
    )
    inertia = _required_child(inertial, "inertia", label)
    _check_children(inertia, f"{label} tensor")
    values = dict.fromkeys(("ixx", "ixy", "ixz", "iyy", "iyz", "izz"), 0.0)
    for name in values:
        value = _optional_number(inertia, name, f"{label} tensor")
        if value is not None:
            values[name] = value
    low, middle, high = _principal_eigenvalues(
        values["ixx"], values["ixy"], values["ixz"], values["iyy"], values["iyz"], values["izz"]
    )
    if low <= 0:
        raise PipelineError(f"{label}: the inertia tensor is not positive definite")
    if high > low + middle + _INERTIA_TRIANGLE_TOLERANCE * high:
        raise PipelineError(f"{label}: the inertia tensor violates the physical triangle condition")
    return {"mass": mass, "com": xyz, "rpy": rpy, "inertia": values}


def _parse_element(element: ET.Element, what: str) -> dict[str, Any]:
    _check_children(element, what)
    xyz, rpy = _parse_origin(element, what)
    geometry = _parse_geometry(element, what)
    rgba: tuple[float, float, float, float] | None = None
    if _strip_namespace(element.tag) == "visual":
        material = _unique_child(element, "material", what)
        if material is not None:
            _check_children(material, f"{what} material")
            color = _unique_child(material, "color", f"{what} material")
            if color is not None:
                _check_children(color, f"{what} color")
                rgba = tuple(_numbers(color, "rgba", 4, f"{what} material color"))
    return {"origin": (xyz, rpy), "shape": geometry, "rgba": rgba, "material_index": None}


def _parse_joint(element: ET.Element, links: set[str]) -> dict[str, Any]:
    name = _element_name(element, "joint")
    what = f"joint {name!r}"
    _check_children(element, what)
    kind = element.get("type")
    if kind not in _SUPPORTED_JOINT_TYPES:
        raise PipelineError(f"{what}: unsupported joint type {kind!r}; supported: {list(_SUPPORTED_JOINT_TYPES)}")
    parent = _required_child(element, "parent", what).get("link")
    child = _required_child(element, "child", what).get("link")
    for side, reference in (("parent", parent), ("child", child)):
        if not reference or reference not in links:
            raise PipelineError(f"{what}: {side} link {reference!r} does not exist")
    if parent == child:
        raise PipelineError(f"{what}: parent and child must differ")
    xyz, rpy = _parse_origin(element, what)
    axis: tuple[float, float, float] | None = None
    lower = upper = effort = velocity = None
    if kind in ("revolute", "continuous", "prismatic"):
        axis_element = _required_child(element, "axis", what)
        axis_vector = _numbers(axis_element, "xyz", 3, f"{what} axis")
        norm = math.sqrt(sum(value * value for value in axis_vector))
        if norm == 0 or abs(norm - 1.0) > _AXIS_UNIT_TOLERANCE:
            raise PipelineError(f"{what}: axis must be a unit vector, found {norm!r}")
        axis = axis_vector
    limit = _unique_child(element, "limit", what)
    if kind in ("revolute", "prismatic"):
        if limit is None:
            raise PipelineError(f"{what}: {kind} joints require a limit with lower and upper")
        lower = _numbers(limit, "lower", 1, f"{what} limit")[0]
        upper = _numbers(limit, "upper", 1, f"{what} limit")[0]
        if not lower < upper:
            raise PipelineError(f"{what}: limit lower must be below upper")
    elif kind == "continuous":
        if limit is not None and (limit.get("lower") or limit.get("upper")):
            raise PipelineError(f"{what}: continuous joints cannot carry a bounded range")
    elif limit is not None:
        raise PipelineError(f"{what}: fixed joints cannot carry a limit")
    if limit is not None:
        effort = _optional_number(limit, "effort", f"{what} limit")
        velocity = _optional_number(limit, "velocity", f"{what} limit")
        for label, value in (("effort", effort), ("velocity", velocity)):
            if value is not None and value < 0:
                raise PipelineError(f"{what}: limit {label} must not be negative")
    damping = frictionloss = 0.0
    dynamics = _unique_child(element, "dynamics", what)
    if dynamics is not None:
        if kind == "fixed":
            raise PipelineError(f"{what}: fixed joints cannot carry dynamics")
        damping = _optional_number(dynamics, "damping", f"{what} dynamics") or 0.0
        frictionloss = _optional_number(dynamics, "friction", f"{what} dynamics") or 0.0
        if damping < 0 or frictionloss < 0:
            raise PipelineError(f"{what}: dynamics values must not be negative")
    return {
        "name": name,
        "kind": kind,
        "parent": parent,
        "child": child,
        "origin": (xyz, rpy),
        "axis": axis,
        "lower": lower,
        "upper": upper,
        "damping": damping,
        "frictionloss": frictionloss,
    }


def _parse_material(element: ET.Element, what: str) -> tuple[str, tuple[float, float, float, float] | None]:
    name = _element_name(element, what)
    _check_children(element, f"{what} {name!r}")
    color = _unique_child(element, "color", f"{what} {name!r}")
    rgba = None
    if color is not None:
        _check_children(color, f"{what} {name!r} color")
        rgba = tuple(_numbers(color, "rgba", 4, f"{what} {name!r} color"))
    return name, rgba


def _parse_urdf(document: ET.Element, urdf_relative: str) -> dict[str, Any]:
    if _strip_namespace(document.tag) != "robot":
        raise PipelineError(f"{urdf_relative}: root element must be <robot>")
    _check_children(document, f"{urdf_relative}")
    robot_name = document.get("name") or "robot"
    if any(character.isspace() or ord(character) < 32 for character in robot_name):
        raise PipelineError(f"{urdf_relative}: robot name {robot_name!r} cannot be used by MuJoCo")
    links: dict[str, dict[str, Any]] = {}
    for element in document.findall("link"):
        name = _element_name(element, "link")
        if name in links:
            raise PipelineError(f"{urdf_relative}: duplicate link name {name!r}")
        what = f"link {name!r}"
        _check_children(element, what)
        visuals = [
            _parse_element(child, f"{what} visual {index}") for index, child in enumerate(element.findall("visual"))
        ]
        collisions = [
            _parse_element(child, f"{what} collision {index}")
            for index, child in enumerate(element.findall("collision"))
        ]
        links[name] = {"visuals": visuals, "collisions": collisions, "inertial": _parse_inertial(element, what)}
    materials: dict[str, tuple[float, float, float, float] | None] = {}
    for element in document.findall("material"):
        name, rgba = _parse_material(element, "material")
        if name in materials:
            raise PipelineError(f"{urdf_relative}: duplicate material name {name!r}")
        materials[name] = rgba
    joints = [_parse_joint(element, set(links)) for element in document.findall("joint")]
    joint_names: set[str] = set()
    for joint in joints:
        if joint["name"] in joint_names:
            raise PipelineError(f"{urdf_relative}: duplicate joint name {joint['name']!r}")
        joint_names.add(joint["name"])
    parent_of = {joint["child"]: joint for joint in joints}
    if len(parent_of) != len(joints):
        repeated = sorted(
            joint["child"] for joint in joints if [item["child"] for item in joints].count(joint["child"]) > 1
        )
        raise PipelineError(f"{urdf_relative}: links with multiple parent joints: {sorted(set(repeated))}")
    roots = [name for name in links if name not in parent_of]
    if len(roots) != 1:
        raise PipelineError(f"{urdf_relative}: the mechanism needs exactly one root link, found {sorted(roots)}")
    children: dict[str, list[dict[str, Any]]] = {name: [] for name in links}
    for joint in joints:
        children[joint["parent"]].append(joint)
    reachable: list[str] = []
    frontier = [roots[0]]
    while frontier:
        link = frontier.pop()
        reachable.append(link)
        frontier.extend(joint["child"] for joint in children[link])
    if len(reachable) != len(links):
        missing = sorted(set(links) - set(reachable))
        raise PipelineError(f"{urdf_relative}: unreachable links or a closed loop: {missing}")
    for visual in (element for link in links.values() for element in (*link["visuals"], *link["collisions"])):
        shape = visual["shape"]
        if shape["kind"] == "mesh":
            shape["relative"] = _resolve_in_root(urdf_relative, shape["filename"], f"mesh {shape['filename']!r}")
        if visual["rgba"] is None:
            material = None
            source = visual.get("material_name")
            if source is not None:
                if source not in materials:
                    raise PipelineError(f"{urdf_relative}: unknown material reference {source!r}")
                material = materials[source]
            visual["rgba"] = material
    incoming = {joint["child"]: joint for joint in joints}
    for name, link in links.items():
        joint = incoming.get(name)
        if joint is not None and joint["kind"] != "fixed" and link["inertial"] is None:
            raise PipelineError(f"{urdf_relative}: movable link {name!r} carries no inertial; mass is never invented")
    return {"name": robot_name, "links": links, "joints": joints, "children": children, "root": roots[0]}


def _resolve_in_root(urdf_relative: str, filename: str, what: str) -> str:
    """Normalize one mesh reference to an in-root POSIX path or refuse it."""

    if "\\" in filename or "://" in filename:
        raise PipelineError(f"{what}: must be a relative in-root path, found {filename!r}")
    if posixpath.isabs(filename) or (len(filename) > 1 and filename[1] == ":"):
        raise PipelineError(f"{what}: must be relative to the source root, found {filename!r}")
    parts: list[str] = []
    for part in posixpath.join(posixpath.dirname(urdf_relative), filename).split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise PipelineError(f"{what}: escapes the source root: {filename!r}")
            parts.pop()
            continue
        parts.append(part)
    relative = "/".join(parts)
    if not relative:
        raise PipelineError(f"{what}: empty mesh path")
    artifact_path_parts(relative)
    return relative


def _validate_mesh(source: Path, relative: str) -> None:
    path = confined(source, relative)
    try:
        stl.read(path)
    except stl.StlError as error:
        raise PipelineError(f"mesh {relative}: {error}") from error


def _freeze_file(source: Path, relative: str, destination: Path) -> str:
    origin = confined(source, relative)
    before = file_digest(origin)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origin, destination)
    if file_digest(origin) != before:
        raise PipelineError(f"{relative} changed while it was frozen")
    if file_digest(destination) != before:
        raise PipelineError(f"{relative} was not frozen byte-for-byte")
    return before


# --------------------------------------------------------------------------- #
# XML projection
# --------------------------------------------------------------------------- #


def _rotation_matrix(rpy: tuple[float, float, float]):
    roll, pitch, yaw = rpy
    cos_roll, sin_roll = math.cos(roll), math.sin(roll)
    cos_pitch, sin_pitch = math.cos(pitch), math.sin(pitch)
    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    return (
        (
            cos_yaw * cos_pitch,
            cos_yaw * sin_pitch * sin_roll - sin_yaw * cos_roll,
            cos_yaw * sin_pitch * cos_roll + sin_yaw * sin_roll,
        ),
        (
            sin_yaw * cos_pitch,
            sin_yaw * sin_pitch * sin_roll + cos_yaw * cos_roll,
            sin_yaw * sin_pitch * cos_roll - cos_yaw * sin_roll,
        ),
        (-sin_pitch, cos_pitch * sin_roll, cos_pitch * cos_roll),
    )


def _quat_from_rpy(rpy: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """Quaternion (w, x, y, z) of the URDF rotation R = Rz(yaw) · Ry(pitch) · Rx(roll)."""

    def axis(angle: float, unit: tuple[float, float, float]):
        half = angle / 2
        return (math.cos(half), unit[0] * math.sin(half), unit[1] * math.sin(half), unit[2] * math.sin(half))

    def product(a, b):
        aw, ax, ay, az = a
        bw, bx, by, bz = b
        return (
            aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
        )

    roll, pitch, yaw = rpy
    return product(product(axis(yaw, (0.0, 0.0, 1.0)), axis(pitch, (0.0, 1.0, 0.0))), axis(roll, (1.0, 0.0, 0.0)))


def _rotate_inertia(inertia: dict[str, float], rpy: tuple[float, float, float]) -> tuple[float, ...]:
    """Rotate the complete tensor into link coordinates; MuJoCo's fullinertia order."""

    matrix = (
        (inertia["ixx"], inertia["ixy"], inertia["ixz"]),
        (inertia["ixy"], inertia["iyy"], inertia["iyz"]),
        (inertia["ixz"], inertia["iyz"], inertia["izz"]),
    )
    rotation = _rotation_matrix(rpy)
    rotated: list[list[float]] = [[0.0, 0.0, 0.0] for _ in range(3)]
    for row in range(3):
        for column in range(3):
            total = 0.0
            for left in range(3):
                for right in range(3):
                    total += rotation[row][left] * matrix[left][right] * rotation[column][right]
            rotated[row][column] = total
    return (
        rotated[0][0],
        rotated[1][1],
        rotated[2][2],
        rotated[0][1],
        rotated[0][2],
        rotated[1][2],
    )


def _quat_from_matrix(matrix: np.ndarray) -> tuple[float, float, float, float]:
    """The (w, x, y, z) quaternion of a rotation matrix, made sign-deterministic."""

    trace = float(matrix[0, 0] + matrix[1, 1] + matrix[2, 2])
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        values = [
            0.25 * scale,
            (matrix[2, 1] - matrix[1, 2]) / scale,
            (matrix[0, 2] - matrix[2, 0]) / scale,
            (matrix[1, 0] - matrix[0, 1]) / scale,
        ]
    elif matrix[0, 0] >= matrix[1, 1] and matrix[0, 0] >= matrix[2, 2]:
        scale = math.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
        values = [
            (matrix[2, 1] - matrix[1, 2]) / scale,
            0.25 * scale,
            (matrix[0, 1] + matrix[1, 0]) / scale,
            (matrix[0, 2] + matrix[2, 0]) / scale,
        ]
    elif matrix[1, 1] >= matrix[2, 2]:
        scale = math.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
        values = [
            (matrix[0, 2] - matrix[2, 0]) / scale,
            (matrix[0, 1] + matrix[1, 0]) / scale,
            0.25 * scale,
            (matrix[1, 2] + matrix[2, 1]) / scale,
        ]
    else:
        scale = math.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
        values = [
            (matrix[1, 0] - matrix[0, 1]) / scale,
            (matrix[0, 2] + matrix[2, 0]) / scale,
            (matrix[1, 2] + matrix[2, 1]) / scale,
            0.25 * scale,
        ]
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        raise PipelineError("a rotation degenerated into a zero quaternion")
    values = [value / norm for value in values]
    if values[0] < 0.0:
        values = [-value for value in values]
    return values[0], values[1], values[2], values[3]


def _principal_axes(
    inertia: dict[str, float], rpy: tuple[float, float, float]
) -> tuple[list[float], tuple[float, float, float, float]]:
    """Lossless re-parametrisation of the link-frame tensor into principal values and a frame.

    MuJoCo re-decomposes a ``fullinertia`` with ``mju_eig3``, whose eigenvectors are only good to
    about 2.4e-8; a delivered tensor would then not survive the declared inertia tolerance.  Doing
    the decomposition here, at full double precision, keeps it recoverable to about 1e-15.  Nothing
    is dropped, rescaled or repaired: the off-diagonal terms live in the returned frame.
    """

    local = np.array(
        [
            [inertia["ixx"], inertia["ixy"], inertia["ixz"]],
            [inertia["ixy"], inertia["iyy"], inertia["iyz"]],
            [inertia["ixz"], inertia["iyz"], inertia["izz"]],
        ],
        dtype=np.float64,
    )
    rotation = np.array(_rotation_matrix(rpy), dtype=np.float64)
    tensor = rotation @ local @ rotation.T
    values, vectors = np.linalg.eigh(tensor)
    order = np.argsort(values)[::-1]
    values = values[order]
    vectors = vectors[:, order]
    if float(np.linalg.det(vectors)) < 0.0:
        vectors[:, 2] = -vectors[:, 2]
    return [float(value) for value in values], _quat_from_matrix(vectors)


def _vector(values) -> str:
    return " ".join(format_number(value) for value in values)


def _nonzero(values) -> bool:
    return any(value != 0.0 for value in values)


def _contact_masks(config: dict[str, Any]) -> tuple[str, str]:
    return ("1", "1") if config["collision"]["self_collision"] else ("1", "2")


def _emit_geom(parent: ET.Element, element: dict[str, Any], attributes: dict[str, str]) -> None:
    xyz, rpy = element["origin"]
    shape = element["shape"]
    if _nonzero(xyz):
        attributes["pos"] = _vector(xyz)
    if _nonzero(rpy):
        attributes["quat"] = _vector(_quat_from_rpy(rpy))
    if shape["kind"] == "box":
        attributes["type"] = "box"
        attributes["size"] = _vector(value / 2 for value in shape["size"])
    elif shape["kind"] == "cylinder":
        attributes["type"] = "cylinder"
        attributes["size"] = _vector((shape["radius"], shape["length"] / 2))
    elif shape["kind"] == "sphere":
        attributes["type"] = "sphere"
        attributes["size"] = format_number(shape["radius"])
    else:
        attributes["type"] = "mesh"
        attributes["mesh"] = f"mesh_{shape['mesh_index']}"
    ET.SubElement(parent, "geom", **attributes)


def _collect_assets(model: dict[str, Any]) -> tuple[list[tuple[float, float, float, float]], list[tuple[str, tuple]]]:
    materials: list[tuple[float, float, float, float]] = []
    material_index: dict[tuple, int] = {}
    meshes: list[tuple[str, tuple]] = []
    mesh_index: dict[tuple, int] = {}
    for link in model["links"].values():
        for element in (*link["visuals"], *link["collisions"]):
            rgba = element["rgba"]
            if rgba is not None:
                index = material_index.get(rgba)
                if index is None:
                    index = len(materials)
                    material_index[rgba] = index
                    materials.append(rgba)
                element["material_index"] = index
            shape = element["shape"]
            if shape["kind"] == "mesh":
                key = (shape["relative"], shape["scale"])
                index = mesh_index.get(key)
                if index is None:
                    index = len(meshes)
                    mesh_index[key] = index
                    meshes.append(key)
                shape["mesh_index"] = index
    return materials, meshes


def _emit_body(
    parent: ET.Element,
    model: dict[str, Any],
    link_name: str,
    joint: dict[str, Any] | None,
    config: dict[str, Any],
    sites: dict[str, list[dict[str, Any]]],
) -> None:
    link = model["links"][link_name]
    attributes = {"name": link_name}
    if joint is not None:
        xyz, rpy = joint["origin"]
        if _nonzero(xyz):
            attributes["pos"] = _vector(xyz)
        if _nonzero(rpy):
            attributes["quat"] = _vector(_quat_from_rpy(rpy))
    body = ET.SubElement(parent, "body", **attributes)
    if joint is None and config["base"] == "floating":
        ET.SubElement(body, "freejoint")
    inertial = link["inertial"]
    if inertial is not None:
        principal, principal_frame = _principal_axes(inertial["inertia"], inertial["rpy"])
        ET.SubElement(
            body,
            "inertial",
            pos=_vector(inertial["com"]),
            mass=format_number(inertial["mass"]),
            quat=_vector(principal_frame),
            diaginertia=_vector(principal),
        )
    if joint is not None and joint["kind"] != "fixed":
        joint_attributes = {
            "name": joint["name"],
            "type": "hinge" if joint["kind"] in ("revolute", "continuous") else "slide",
            "axis": _vector(joint["axis"]),
        }
        if joint["kind"] != "continuous":
            joint_attributes["range"] = f"{format_number(joint['lower'])} {format_number(joint['upper'])}"
        if joint["damping"] != 0.0:
            joint_attributes["damping"] = format_number(joint["damping"])
        if joint["frictionloss"] != 0.0:
            joint_attributes["frictionloss"] = format_number(joint["frictionloss"])
        ET.SubElement(body, "joint", **joint_attributes)
    contact = _contact_masks(config)
    friction = _vector(config["collision"]["friction"])
    source = config["collision"]["source"]

    def visual_attributes(index: int, element: dict[str, Any], contype: str, conaffinity: str) -> dict[str, str]:
        attributes = {"name": geom_name(link_name, "visual", index), "contype": contype, "conaffinity": conaffinity}
        if source != "urdf" or contype != "0":
            attributes["friction"] = friction
        if element["material_index"] is not None:
            attributes["material"] = f"material_{element['material_index']}"
        return attributes

    if source == "visual_convex":
        for index, element in enumerate(link["visuals"]):
            _emit_geom(body, element, visual_attributes(index, element, contact[0], contact[1]))
    else:
        for index, element in enumerate(link["visuals"]):
            _emit_geom(body, element, visual_attributes(index, element, "0", "0"))
        if source == "urdf":
            for index, element in enumerate(link["collisions"]):
                attributes = {
                    "name": geom_name(link_name, "collision", index),
                    "contype": contact[0],
                    "conaffinity": contact[1],
                    "friction": friction,
                }
                _emit_geom(body, element, attributes)
    for site in sites.get(link_name, []):
        site_attributes = {"name": site["name"]}
        if _nonzero(site["xyz"]):
            site_attributes["pos"] = _vector(site["xyz"])
        if _nonzero(site["rpy"]):
            site_attributes["quat"] = _vector(_quat_from_rpy(site["rpy"]))
        ET.SubElement(body, "site", **site_attributes)
    for child in model["children"][link_name]:
        _emit_body(body, model, child["child"], child, config, sites)


def _serialize(document: ET.Element) -> bytes:
    ET.indent(document)
    return ET.tostring(document, encoding="utf-8", xml_declaration=True) + b"\n"


def _project(
    model: dict[str, Any],
    config: dict[str, Any],
    materials: list[tuple[float, float, float, float]],
    meshes: list[tuple[str, tuple]],
) -> tuple[bytes, bytes | None]:
    robot = ET.Element("mujoco", model=model["name"])
    ET.SubElement(
        robot,
        "compiler",
        angle="radian",
        fusestatic="false",
        strippath="false",
        inertiafromgeom="false",
        balanceinertia="false",
    )
    ET.SubElement(
        robot,
        "option",
        timestep=format_number(config["simulation"]["timestep"]),
        gravity=_vector(config["simulation"]["gravity"]),
        integrator=config["simulation"]["integrator"],
    )
    asset = ET.SubElement(robot, "asset")
    for index, rgba in enumerate(materials):
        ET.SubElement(asset, "material", name=f"material_{index}", rgba=_vector(rgba))
    for index, (relative, scale) in enumerate(meshes):
        attributes = {"name": f"mesh_{index}", "file": f"{INPUT_PREFIX}/{relative}"}
        if scale != (1.0, 1.0, 1.0):
            attributes["scale"] = _vector(scale)
        ET.SubElement(asset, "mesh", **attributes)
    sites: dict[str, list[dict[str, Any]]] = {}
    for site in config["sites"]:
        sites.setdefault(site["link"], []).append(site)
    worldbody = ET.SubElement(robot, "worldbody")
    _emit_body(worldbody, model, model["root"], None, config, sites)
    scene: bytes | None = None
    floor = config["floor"]
    if floor is not None:
        document = ET.Element("mujoco")
        ET.SubElement(document, "include", file=ROBOT_XML)
        scene_worldbody = ET.SubElement(document, "worldbody")
        ET.SubElement(
            scene_worldbody,
            "geom",
            name="floor",
            type="plane",
            pos=f"0 0 {format_number(floor['height'])}",
            size="0 0 0.05",
            friction=_vector(floor["friction"]),
            contype="2",
            conaffinity="1",
        )
        ET.SubElement(scene_worldbody, "light", name="key", directional="true", pos="0 0 3", dir="0 0 -1")
        scene = _serialize(document)
    return _serialize(robot), scene


# --------------------------------------------------------------------------- #
# Build entry point
# --------------------------------------------------------------------------- #


def _load_mujoco():
    import importlib.metadata

    try:
        installed = importlib.metadata.version("mujoco")
    except importlib.metadata.PackageNotFoundError as error:
        raise PipelineError("the URDF → MJCF pipeline requires the locked MuJoCo runtime (verify extra)") from error
    if installed != MUJOCO_VERSION:
        raise PipelineError(f"the locked MuJoCo runtime is {MUJOCO_VERSION}, found {installed}")
    import mujoco

    return mujoco


def _compile(directory: Path, names) -> None:
    mujoco = _load_mujoco()
    for name in names:
        try:
            mujoco.MjModel.from_xml_path(str(Path(directory) / name))
        except Exception as error:  # noqa: BLE001 - MuJoCo raises its own fatal error type
            raise PipelineError(f"MuJoCo refused {name}: {error}") from error


def _read_configuration(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        content = Path(path).read_bytes()
    except OSError as error:
        raise PipelineError(f"cannot read the simulation configuration {path}: {error}") from error
    return content, validate_config(parse_data(content, Path(path)))


def _normalized_urdf_path(value: str) -> str:
    if "\\" in value or "://" in value:
        raise PipelineError(f"the URDF path must be relative and in-root: {value!r}")
    return "/".join(artifact_path_parts(value))


def build(source: Path, urdf: str | Path, config: Path, output: Path) -> dict[str, Any]:
    """Convert one immutable URDF into a checked, self-contained MJCF delivery."""

    source = Path(source)
    output = Path(output)
    urdf_relative = _normalized_urdf_path(str(urdf))
    if output.is_symlink() or output.exists():
        raise PipelineError(f"the output directory already exists: {output}")
    config_bytes, config_data = _read_configuration(config)
    urdf_path = confined(source, urdf_relative)
    try:
        document = ET.fromstring(urdf_path.read_bytes())
    except OSError as error:
        raise PipelineError(f"cannot read {urdf_relative}: {error}") from error
    except ET.ParseError as error:
        raise PipelineError(f"{urdf_relative} is not well-formed XML: {error}") from error
    model = _parse_urdf(document, urdf_relative)
    for index, site in enumerate(config_data["sites"]):
        if site["link"] not in model["links"]:
            raise PipelineError(f"sites[{index}].link {site['link']!r} is not a link of this URDF")
    materials, meshes = _collect_assets(model)
    mesh_files = sorted({relative for relative, _ in meshes})
    for relative in mesh_files:
        _validate_mesh(source, relative)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".mjcf-build-", dir=output.parent))
    try:
        sources = {urdf_relative: _freeze_file(source, urdf_relative, temporary / INPUT_PREFIX / urdf_relative)}
        for relative in mesh_files:
            sources[relative] = _freeze_file(source, relative, temporary / INPUT_PREFIX / relative)
        (temporary / SIMULATION_JSON).write_bytes(config_bytes)
        robot, scene = _project(model, config_data, materials, meshes)
        (temporary / ROBOT_XML).write_bytes(robot)
        compiled = [ROBOT_XML]
        if scene is not None:
            (temporary / SCENE_XML).write_bytes(scene)
            compiled.append(SCENE_XML)
        _compile(temporary, compiled)
        write_json(temporary / MANIFEST_JSON, build_manifest(temporary, urdf=urdf_relative, sources=sources))
        from .verification.mjcf import check_bundle

        report = check_bundle(temporary, compare_saved=False)
        if report.get("passed") is not True:
            raise PipelineError(
                f"the independent check refused the staged delivery: {report.get('error', 'unreported failure')}"
            )
        write_json(temporary / REPORT_JSON, report)
        confirmed = check_bundle(temporary, compare_saved=True)
        if confirmed.get("passed") is not True:
            raise PipelineError(
                f"the saved delivery report does not reproduce: {confirmed.get('error', 'unreported failure')}"
            )
        try:
            os.replace(temporary, output)
        except OSError as error:
            raise PipelineError(f"cannot commit the delivery to {output}: {error}") from error
        return report
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
