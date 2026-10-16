"""Independent checks of a projected URDF-to-MJCF delivery.

Every expectation is re-derived from the frozen URDF, meshes and configuration
inside one bounded isolated child process (``python -I``), which loads the
actual ``robot.xml`` (and the optional ``scene.xml``) with the locked MuJoCo and
compares against ``MjModel``/``MjData``/NumPy arrays in place.  No engine facts
are serialized out of that process; it returns only the compact deterministic
report.  The parent process invents nothing: it bounds the child, checks the
delivery body before and after, collects the report and requires the delivered
saved report to equal the recomputation.  The converter module is never
imported here, and the module's top level imports no package module so the
child runs without the pipeline on ``sys.path``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

TIMEOUT_SECONDS = 120


class CheckError(ValueError):
    """A failed independent gate with a located expectation."""


def _fail(where: str, message: str):
    raise CheckError(f"{where}: {message}")


def _need(state: dict, key: str):
    if key not in state:
        raise CheckError(f"input preparation failed before this check ({key} unavailable)")
    return state[key]


def _close(actual, expected, atol: float) -> float:
    first = np.asarray(actual, dtype=float)
    second = np.asarray(expected, dtype=float)
    if not np.isfinite(first).all() or not np.isfinite(second).all():
        raise CheckError("a numeric comparison met a non-finite value")
    if first.size == 0 or second.size == 0:
        if first.size != second.size:
            raise CheckError("a numeric comparison met an unexpected empty array")
        return 0.0
    residual = float(np.max(np.abs(first - second)))
    if residual > atol:
        raise CheckError(f"residual {residual:.18g} exceeds the absolute tolerance {atol:g}")
    return residual


def _scaled_close(actual, expected, rtol: float, atol: float) -> float:
    first = np.asarray(actual, dtype=float)
    second = np.asarray(expected, dtype=float)
    if not np.isfinite(first).all() or not np.isfinite(second).all():
        raise CheckError("a numeric comparison met a non-finite value")
    if first.size == 0 or second.size == 0:
        if first.size != second.size:
            raise CheckError("a numeric comparison met an unexpected empty array")
        return 0.0
    residual = np.abs(first - second)
    allowed = atol + rtol * np.maximum(np.abs(first), np.abs(second))
    worst = float(np.max(residual - allowed))
    if worst > 0:
        index = int(np.argmax(residual - allowed))
        raise CheckError(
            f"residual {residual.reshape(-1)[index]:.18g} exceeds {atol:g} + {rtol:g} * scale "
            f"at component {index}"
        )
    return float(np.max(residual))


def _rpy_matrix(rpy) -> np.ndarray:
    """The URDF convention: fixed-axis Rz(yaw) Ry(pitch) Rx(roll)."""

    roll, pitch, yaw = (float(value) for value in rpy)
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


def _rotation_from_quat(quat) -> np.ndarray:
    w, x, y, z = (float(value) for value in quat)
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ]
    )


# --------------------------------------------------------------------------
# Delivery reading (inside the child)
# --------------------------------------------------------------------------


def _parse_json(content: bytes, label: str):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        _fail(label, f"not UTF-8: {error}")

    def unique(pairs):
        mapping = {}
        for key, value in pairs:
            if key in mapping:
                _fail(label, f"duplicate JSON key {key!r}")
            mapping[key] = value
        return mapping

    def constant(name):
        _fail(label, f"non-finite constant {name}")

    def finite(text_number):
        value = float(text_number)
        if not math.isfinite(value):
            _fail(label, f"non-finite number {text_number}")
        return value

    try:
        return json.loads(text, object_pairs_hook=unique, parse_constant=constant, parse_float=finite)
    except json.JSONDecodeError as error:
        _fail(label, f"invalid JSON: {error}")


def _confined(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    if not relative or "\\" in relative or any(part in ("", ".", "..") for part in parts):
        _fail("path", f"invalid relative delivery path {relative!r}")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            _fail("path", f"symlink in the delivery: {relative!r}")
    if not current.is_file():
        _fail("path", f"missing delivered file {relative!r}")
    return current


def _inventory(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    seen: dict[str, str] = {}
    for directory, directories, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in directories:
            path = directory_path / name
            if path.is_symlink():
                _fail("inventory", f"symlink in the delivery: {path.relative_to(root).as_posix()!r}")
        for name in filenames:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                _fail("inventory", f"not a regular delivered file: {relative!r}")
            key = relative.casefold()
            if key in seen and seen[key] != relative:
                _fail("inventory", f"case-colliding delivery paths: {seen[key]!r} and {relative!r}")
            seen[key] = relative
            parts = relative.split("/")
            for index in range(len(parts)):
                prefix = "/".join(parts[: index + 1])
                prefix_key = prefix.casefold()
                if prefix_key in seen and seen[prefix_key] != prefix:
                    _fail("inventory", f"case-colliding delivery paths: {seen[prefix_key]!r} and {prefix!r}")
                seen.setdefault(prefix_key, prefix)
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _canonical_digest(data) -> str:
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _read_manifest(root: Path, policy: dict) -> dict:
    data = _parse_json(_confined(root, policy["manifest_name"]).read_bytes(), policy["manifest_name"])
    if not isinstance(data, dict) or data.get("schema_version") != policy["manifest_schema"]:
        _fail("manifest", f"schema_version must be {policy['manifest_schema']!r}")
    if set(data) != {"schema_version", "source", "files", "tool", "engine"}:
        _fail("manifest", "unexpected key set")
    source = data["source"]
    if not isinstance(source, dict) or set(source) != {"urdf", "meshes"}:
        _fail("manifest", "source must hold 'urdf' and 'meshes'")
    if not isinstance(source["urdf"], str) or not source["urdf"]:
        _fail("manifest", "source.urdf must be a relative path")
    if not isinstance(source["meshes"], list) or any(not isinstance(item, str) for item in source["meshes"]):
        _fail("manifest", "source.meshes must be a list of relative paths")
    files = data["files"]
    if not isinstance(files, dict) or not files:
        _fail("manifest", "files must be a non-empty mapping")
    for relative, record in files.items():
        if not isinstance(relative, str) or any(part in ("", ".", "..") for part in relative.split("/")):
            _fail("manifest", f"invalid file path {relative!r}")
        if not isinstance(record, dict) or set(record) != {"bytes", "sha256"}:
            _fail("manifest", f"invalid record for {relative!r}")
        if isinstance(record["bytes"], bool) or not isinstance(record["bytes"], int) or record["bytes"] < 0:
            _fail("manifest", f"invalid byte length for {relative!r}")
        sha = record["sha256"]
        if not isinstance(sha, str) or len(sha) != 64 or any(character not in "0123456789abcdef" for character in sha):
            _fail("manifest", f"invalid digest for {relative!r}")
    if not isinstance(data["tool"], dict) or set(data["tool"]) != {"name", "version", "source_sha256"}:
        _fail("manifest", "tool identity shape")
    if not isinstance(data["engine"], dict) or set(data["engine"]) != {"mujoco"}:
        _fail("manifest", "engine identity shape")
    return data


def _number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(where, f"a finite number is required, got {value!r}")
    return float(value)


def _triple(value, where: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        _fail(where, "three finite numbers are required")
    return tuple(_number(item, where) for item in value)


def _friction(value, where: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        _fail(where, "[slide, torsion, roll] is required")
    result = tuple(_number(item, where) for item in value)
    if any(item < 0 for item in result):
        _fail(where, "coefficients must be non-negative")
    return result


def _read_simulation(content: bytes, policy: dict) -> dict:
    data = _parse_json(content, policy["config_name"])
    if not isinstance(data, dict):
        _fail("simulation", "the configuration must be an object")
    unknown = sorted(
        set(data) - {"schema_version", "base", "collision", "simulation", "sites", "support_sites", "floor"}
    )
    if unknown:
        _fail("simulation", f"unknown key {unknown[0]!r}")
    if isinstance(data.get("schema_version"), bool) or data.get("schema_version") != 1:
        _fail("simulation", "schema_version must be 1")
    base = data.get("base")
    if base not in policy["base_modes"]:
        _fail("simulation", f"base must be one of {policy['base_modes']}")
    collision = data.get("collision")
    if not isinstance(collision, dict):
        _fail("simulation", "collision must be an object")
    unknown = sorted(set(collision) - {"source", "self_collision", "friction"})
    if unknown:
        _fail("simulation", f"unknown key {unknown[0]!r} at collision")
    source = collision.get("source")
    if source not in policy["collision_sources"]:
        _fail("simulation", f"collision.source must be one of {policy['collision_sources']}")
    self_collision = collision.get("self_collision")
    if not isinstance(self_collision, bool):
        _fail("simulation", "collision.self_collision must be true or false")
    simulation = data.get("simulation")
    if not isinstance(simulation, dict):
        _fail("simulation", "simulation must be an object")
    unknown = sorted(set(simulation) - {"timestep", "gravity", "integrator"})
    if unknown:
        _fail("simulation", f"unknown key {unknown[0]!r} at simulation")
    timestep = _number(simulation.get("timestep"), "simulation.timestep")
    if timestep <= 0:
        _fail("simulation", "simulation.timestep must be positive")
    gravity = _triple(simulation.get("gravity"), "simulation.gravity")
    integrator = simulation.get("integrator")
    if integrator not in policy["integrators"]:
        _fail("simulation", f"simulation.integrator must be one of {policy['integrators']}")
    sites = []
    site_names = set()
    if "sites" in data:
        if not isinstance(data["sites"], list):
            _fail("simulation", "sites must be a list")
        for index, raw in enumerate(data["sites"]):
            where = f"sites[{index}]"
            if not isinstance(raw, dict):
                _fail("simulation", f"{where} must be an object")
            unknown = sorted(set(raw) - {"name", "link", "xyz", "rpy"})
            if unknown:
                _fail("simulation", f"unknown key {unknown[0]!r} at {where}")
            name, link = raw.get("name"), raw.get("link")
            if not isinstance(name, str) or not name or not isinstance(link, str) or not link:
                _fail("simulation", f"{where} needs a name and a link")
            if name in site_names:
                _fail("simulation", f"duplicate site name {name!r}")
            site_names.add(name)
            sites.append(
                {
                    "name": name,
                    "link": link,
                    "xyz": _triple(raw.get("xyz"), f"{where}.xyz"),
                    "rpy": _triple(raw.get("rpy"), f"{where}.rpy"),
                }
            )
    support_sites = []
    if "support_sites" in data:
        if not isinstance(data["support_sites"], list):
            _fail("simulation", "support_sites must be a list")
        for index, raw in enumerate(data["support_sites"]):
            where = f"support_sites[{index}]"
            if not isinstance(raw, dict):
                _fail("simulation", f"{where} must be an object")
            unknown = sorted(set(raw) - {"name", "link"})
            if unknown:
                _fail("simulation", f"unknown key {unknown[0]!r} at {where}")
            name, link = raw.get("name"), raw.get("link")
            if not isinstance(name, str) or not name or not isinstance(link, str) or not link:
                _fail("simulation", f"{where} needs a name and a link")
            if name in site_names:
                _fail("simulation", f"site name {name!r} appears twice")
            site_names.add(name)
            support_sites.append({"name": name, "link": link})
    if support_sites and source == "none":
        _fail("simulation", "support_sites require collision.source urdf or visual_convex")
    floor = None
    if "floor" in data:
        raw = data["floor"]
        if not isinstance(raw, dict):
            _fail("simulation", "floor must be an object")
        unknown = sorted(set(raw) - {"height", "friction"})
        if unknown:
            _fail("simulation", f"unknown key {unknown[0]!r} at floor")
        raw_height = raw.get("height")
        if raw_height == policy["floor_height_supports"]:
            if not support_sites:
                _fail("simulation", f"floor.height {raw_height!r} requires support_sites")
            height, supports = None, True
        else:
            height, supports = _number(raw_height, "floor.height"), False
        floor = {
            "height": height,
            "supports": supports,
            "friction": _friction(raw.get("friction"), "floor.friction"),
        }
    return {
        "base": base,
        "collision": {
            "source": source,
            "self_collision": self_collision,
            "friction": _friction(collision.get("friction"), "collision.friction"),
        },
        "simulation": {"timestep": timestep, "gravity": gravity, "integrator": integrator},
        "sites": sites,
        "support_sites": support_sites,
        "floor": floor,
    }


# --------------------------------------------------------------------------
# Independent URDF and mesh reading (inside the child)
# --------------------------------------------------------------------------


def _checked_attributes(element: ET.Element, allowed: frozenset, where: str) -> None:
    extra = sorted(set(element.attrib) - allowed)
    if extra:
        _fail(where, f"unsupported attribute {extra[0]!r}")


def _checked_children(element: ET.Element, allowed: frozenset, where: str) -> list[ET.Element]:
    found = []
    for child in element:
        if child.tag is ET.Comment:
            continue
        if not isinstance(child.tag, str) or child.tag not in allowed:
            _fail(where, f"unsupported element <{child.tag!r}>")
        found.append(child)
    return found


def _one(element: ET.Element, tag: str, where: str, *, required: bool = False) -> ET.Element | None:
    found = [child for child in element if isinstance(child.tag, str) and child.tag == tag]
    if len(found) > 1:
        _fail(where, f"multiple <{tag}> elements")
    if not found and required:
        _fail(where, f"missing <{tag}>")
    return found[0] if found else None


def _text_number(text: str | None, where: str) -> float:
    if text is None:
        _fail(where, "missing value")
    try:
        value = float(text)
    except ValueError:
        _fail(where, f"{text!r} is not a number")
    if not math.isfinite(value):
        _fail(where, f"{text!r} is not finite")
    return value


def _vector(text: str | None, count: int, where: str) -> tuple[float, ...]:
    if text is None:
        _fail(where, "missing value")
    fields = text.split()
    if len(fields) != count:
        _fail(where, f"expected {count} numbers, got {text!r}")
    return tuple(_text_number(field, where) for field in fields)


def _element_name(element: ET.Element, where: str) -> str:
    value = element.get("name")
    if not value or any(ord(character) < 32 for character in value) or value != value.strip():
        _fail(where, f"invalid name {value!r}")
    return value


def _origin_of(element: ET.Element, where: str) -> dict:
    origin = _one(element, "origin", where)
    if origin is None:
        return {"xyz": (0.0, 0.0, 0.0), "rpy": (0.0, 0.0, 0.0)}
    _checked_attributes(origin, frozenset({"xyz", "rpy"}), f"{where}/origin")
    xyz = origin.get("xyz")
    rpy = origin.get("rpy")
    return {
        "xyz": _vector(xyz, 3, f"{where}/origin@xyz") if xyz is not None else (0.0, 0.0, 0.0),
        "rpy": _vector(rpy, 3, f"{where}/origin@rpy") if rpy is not None else (0.0, 0.0, 0.0),
    }


def _geometry_of(element: ET.Element, where: str) -> dict:
    geometry = _one(element, "geometry", where, required=True)
    shapes = _checked_children(geometry, frozenset({"box", "sphere", "cylinder", "mesh"}), f"{where}/geometry")
    if len(shapes) != 1:
        _fail(f"{where}/geometry", "exactly one shape is required")
    shape = shapes[0]
    inner = f"{where}/geometry/{shape.tag}"
    if shape.tag == "box":
        size = _vector(shape.get("size"), 3, f"{inner}@size")
        if any(value <= 0 for value in size):
            _fail(inner, "box size must be positive")
        return {"kind": "box", "size": size}
    if shape.tag == "sphere":
        radius = _vector(shape.get("radius"), 1, f"{inner}@radius")[0]
        if radius <= 0:
            _fail(inner, "sphere radius must be positive")
        return {"kind": "sphere", "radius": radius}
    if shape.tag == "cylinder":
        radius = _vector(shape.get("radius"), 1, f"{inner}@radius")[0]
        length = _vector(shape.get("length"), 1, f"{inner}@length")[0]
        if radius <= 0 or length <= 0:
            _fail(inner, "cylinder radius and length must be positive")
        return {"kind": "cylinder", "radius": radius, "length": length}
    filename = shape.get("filename")
    if not filename:
        _fail(inner, "mesh@filename is required")
    scale = (1.0, 1.0, 1.0) if shape.get("scale") is None else _vector(shape.get("scale"), 3, f"{inner}@scale")
    if any(value <= 0 for value in scale):
        _fail(inner, "mesh scale must be positive")
    return {"kind": "mesh", "filename": filename, "scale": scale}


def _color_of(element: ET.Element | None, where: str, materials: dict) -> tuple | None:
    if element is None:
        return None
    _checked_attributes(element, frozenset({"name"}), where)
    inline = _one(element, "color", where)
    if inline is not None:
        _checked_attributes(inline, frozenset({"rgba"}), f"{where}/color")
        rgba = _vector(inline.get("rgba"), 4, f"{where}/color@rgba")
        if any(not 0.0 <= component <= 1.0 for component in rgba):
            _fail(f"{where}/color", "components must lie in [0, 1]")
        return rgba
    reference = element.get("name")
    if reference not in materials:
        _fail(where, f"unknown material reference {reference!r}")
    return materials[reference]


def _surface_of(element: ET.Element, where: str, materials: dict, *, allow_material: bool) -> dict:
    allowed = frozenset({"origin", "geometry", "material"}) if allow_material else frozenset({"origin", "geometry"})
    _checked_attributes(element, frozenset({"name"}), where)
    _checked_children(element, allowed, where)
    surface = {"origin": _origin_of(element, where), "geometry": _geometry_of(element, where)}
    if allow_material:
        surface["color"] = _color_of(_one(element, "material", where), f"{where}/material", materials)
    return surface


def _inertial_of(element: ET.Element | None, where: str, policy: dict) -> dict | None:
    if element is None:
        return None
    _checked_children(element, frozenset({"origin", "mass", "inertia"}), where)
    mass = _one(element, "mass", where, required=True)
    _checked_attributes(mass, frozenset({"value"}), f"{where}/mass")
    mass_value = _text_number(mass.get("value"), f"{where}/mass@value")
    if mass_value <= 0:
        _fail(f"{where}/mass", f"mass must be positive, got {mass_value!r}")
    inertia = _one(element, "inertia", where, required=True)
    _checked_attributes(inertia, frozenset({"ixx", "ixy", "ixz", "iyy", "iyz", "izz"}), f"{where}/inertia")
    values = {
        key: _text_number(inertia.get(key), f"{where}/inertia@{key}")
        for key in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")
    }
    tensor = np.array(
        [
            [values["ixx"], values["ixy"], values["ixz"]],
            [values["ixy"], values["iyy"], values["iyz"]],
            [values["ixz"], values["iyz"], values["izz"]],
        ]
    )
    moments = np.linalg.eigvalsh(tensor)
    if moments[0] <= 0:
        _fail(f"{where}/inertia", "the tensor is not positive definite")
    if moments[2] > (moments[0] + moments[1]) * (1 + policy["tolerances"]["inertia_triangle_rtol"]):
        _fail(f"{where}/inertia", "principal moments violate A + B >= C")
    return {"origin": _origin_of(element, where), "mass": mass_value, "tensor": tensor}


_JOINT_TYPES = frozenset({"fixed", "revolute", "continuous", "prismatic"})


def _read_urdf(content: bytes, label: str, policy: dict) -> dict:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        _fail(label, f"the URDF must be UTF-8: {error}")
    for marker in ("<!DOCTYPE", "<!ENTITY"):
        if marker in text:
            _fail(label, "DTDs and external entities are not supported")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        _fail(label, f"invalid XML: {error}")
    if root.tag != "robot":
        _fail(label, "the root element must be <robot>")
    _checked_attributes(root, frozenset({"name"}), f"{label}: robot")
    robot_name = root.get("name")
    if not robot_name:
        _fail(f"{label}: robot", "name is required")
    _checked_children(root, frozenset({"link", "joint", "material"}), f"{label}: robot")

    materials: dict = {}
    for element in root.findall("material"):
        where = f"{label}: robot/material"
        _checked_attributes(element, frozenset({"name"}), where)
        name = _element_name(element, where)
        color = _one(element, "color", where, required=True)
        _checked_attributes(color, frozenset({"rgba"}), f"{where}/color")
        rgba = _vector(color.get("rgba"), 4, f"{where}/color@rgba")
        if any(not 0.0 <= component <= 1.0 for component in rgba):
            _fail(f"{where}/color", "components must lie in [0, 1]")
        if name in materials:
            _fail(where, f"duplicate material {name!r}")
        materials[name] = rgba

    links: list[dict] = []
    names: set[str] = set()
    for element in root.findall("link"):
        where = f"{label}: link"
        _checked_attributes(element, frozenset({"name"}), where)
        name = _element_name(element, where)
        if name in names:
            _fail(where, f"duplicate link {name!r}")
        names.add(name)
        position = f"{label}: link {name!r}"
        _checked_children(element, frozenset({"inertial", "visual", "collision"}), position)
        links.append(
            {
                "name": name,
                "inertial": _inertial_of(_one(element, "inertial", position), position, policy),
                "visuals": [
                    _surface_of(visual, f"{position}/visual[{index}]", materials, allow_material=True)
                    for index, visual in enumerate(element.findall("visual"))
                ],
                "collisions": [
                    _surface_of(collision, f"{position}/collision[{index}]", materials, allow_material=False)
                    for index, collision in enumerate(element.findall("collision"))
                ],
            }
        )
    if not links:
        _fail(label, "at least one link is required")

    joints: list[dict] = []
    seen_joints: set[str] = set()
    for element in root.findall("joint"):
        where = f"{label}: joint"
        _checked_attributes(element, frozenset({"name", "type"}), where)
        name = _element_name(element, where)
        if name in seen_joints:
            _fail(where, f"duplicate joint {name!r}")
        seen_joints.add(name)
        position = f"{label}: joint {name!r}"
        joint_type = element.get("type")
        if joint_type not in _JOINT_TYPES:
            _fail(position, f"unsupported joint type {joint_type!r}")
        _checked_children(
            element, frozenset({"origin", "parent", "child", "axis", "limit", "dynamics"}), position
        )
        parent = _one(element, "parent", position, required=True)
        child = _one(element, "child", position, required=True)
        parent_link = parent.get("link")
        child_link = child.get("link")
        if not parent_link or not child_link:
            _fail(position, "parent and child links are required")
        axis_element = _one(element, "axis", position)
        axis = None
        if axis_element is not None:
            _checked_attributes(axis_element, frozenset({"xyz"}), f"{position}/axis")
            axis = _vector(axis_element.get("xyz"), 3, f"{position}/axis@xyz")
        if joint_type != "fixed":
            if axis is None:
                axis = (1.0, 0.0, 0.0)
            magnitude = math.sqrt(sum(component * component for component in axis))
            if magnitude == 0 or not math.isfinite(magnitude):
                _fail(f"{position}/axis", "the axis must be finite and nonzero")
            axis = tuple(component / magnitude for component in axis)
        limit_element = _one(element, "limit", position)
        limit = None
        if limit_element is not None:
            _checked_attributes(limit_element, frozenset({"lower", "upper", "effort", "velocity"}), f"{position}/limit")
            limit = {
                key: (
                    _text_number(limit_element.get(key), f"{position}/limit@{key}")
                    if limit_element.get(key) is not None
                    else None
                )
                for key in ("lower", "upper", "effort", "velocity")
            }
            if joint_type in ("revolute", "prismatic") and (limit["lower"] is None or limit["upper"] is None):
                _fail(f"{position}/limit", "lower and upper are required")
        if joint_type in ("revolute", "prismatic"):
            if limit is None:
                _fail(position, f"a {joint_type} joint requires <limit>")
            if not limit["lower"] < limit["upper"]:
                _fail(f"{position}/limit", "the range must be strictly increasing")
        dynamics_element = _one(element, "dynamics", position)
        dynamics = {"damping": 0.0, "friction": 0.0}
        if dynamics_element is not None:
            _checked_attributes(dynamics_element, frozenset({"damping", "friction"}), f"{position}/dynamics")
            for key in ("damping", "friction"):
                if dynamics_element.get(key) is not None:
                    dynamics[key] = _text_number(dynamics_element.get(key), f"{position}/dynamics@{key}")
                    if dynamics[key] < 0:
                        _fail(f"{position}/dynamics", "values must be non-negative")
        joints.append(
            {
                "name": name,
                "type": joint_type,
                "parent": parent_link,
                "child": child_link,
                "origin": _origin_of(element, position),
                "axis": axis,
                "limit": limit,
                "dynamics": dynamics,
            }
        )
    _check_tree(links, joints, label)
    return {"name": robot_name, "links": links, "joints": joints}


def _check_tree(links: list[dict], joints: list[dict], label: str) -> None:
    known = {link["name"] for link in links}
    parents: dict[str, str] = {}
    for joint in joints:
        if joint["parent"] not in known or joint["child"] not in known:
            _fail(label, f"joint {joint['name']!r} references an unknown link")
        if joint["parent"] == joint["child"]:
            _fail(label, f"joint {joint['name']!r} connects a link to itself")
        if joint["child"] in parents:
            _fail(label, f"link {joint['child']!r} has multiple parent joints")
        parents[joint["child"]] = joint["parent"]
    roots = [name for name in known if name not in parents]
    if len(roots) != 1:
        _fail(label, f"expected one root link, found {len(roots)}")
    seen = {roots[0]}
    pending = [roots[0]]
    edges: dict[str, list[str]] = {name: [] for name in known}
    for joint in joints:
        edges[joint["parent"]].append(joint["child"])
    while pending:
        for child in edges[pending.pop()]:
            if child in seen:
                _fail(label, f"link {child!r} is reachable twice")
            seen.add(child)
            pending.append(child)
    if seen != known:
        _fail(label, f"links unreachable from the root: {sorted(known - seen)}")


_STL_RECORD = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")])


def _read_binary_stl(content: bytes, label: str) -> np.ndarray:
    if len(content) < 84:
        _fail(label, "not a binary STL (shorter than its header)")
    count = struct.unpack_from("<I", content, 80)[0]
    if len(content) != 84 + 50 * count:
        _fail(label, f"not a binary STL or corrupt ({len(content)} bytes for {count} triangles)")
    if count == 0:
        _fail(label, "the mesh is empty")
    records = np.frombuffer(content, dtype=_STL_RECORD, count=count, offset=84)
    vertices = records["vertices"].reshape(-1, 3).astype(np.float64)
    if not np.isfinite(vertices).all():
        _fail(label, "non-finite mesh coordinates")
    return vertices


def _canonical_mesh_path(directory: str, reference: str, where: str) -> str:
    if "://" in reference:
        _fail(where, f"remote or package:// reference {reference!r}")
    if reference.startswith("/") or "\\" in reference:
        _fail(where, f"mesh reference must be relative and POSIX: {reference!r}")
    parts = [part for part in directory.split("/") if part]
    for part in reference.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                _fail(where, f"mesh reference escapes the root: {reference!r}")
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        _fail(where, "mesh reference does not name a file")
    return "/".join(parts)


def _mesh_surfaces(document: dict) -> list[dict]:
    result = []
    for link in document["links"]:
        for role in ("visuals", "collisions"):
            for surface in link[role]:
                if surface["geometry"]["kind"] == "mesh":
                    result.append(surface)
    return result


# --------------------------------------------------------------------------
# Expectation helpers
# --------------------------------------------------------------------------


def _fk_pose(document: dict, values: dict) -> dict[str, np.ndarray]:
    joints_by_child = {joint["child"]: joint for joint in document["joints"]}
    children: dict[str, list[str]] = {link["name"]: [] for link in document["links"]}
    for joint in document["joints"]:
        children[joint["parent"]].append(joint["child"])
    root = next(link["name"] for link in document["links"] if link["name"] not in joints_by_child)
    poses: dict[str, np.ndarray] = {}

    def visit(name: str, parent: np.ndarray) -> None:
        joint = joints_by_child.get(name)
        pose = parent
        if joint is not None:
            origin = np.eye(4)
            origin[:3, :3] = _rpy_matrix(joint["origin"]["rpy"])
            origin[:3, 3] = joint["origin"]["xyz"]
            motion = np.eye(4)
            quantity = float(values.get(joint["name"], 0.0))
            axis = np.asarray(joint["axis"], dtype=float)
            if joint["type"] in ("revolute", "continuous"):
                motion[:3, :3] = _rodrigues(axis, quantity)
            elif joint["type"] == "prismatic":
                motion[:3, 3] = axis * quantity
            pose = parent @ origin @ motion
        poses[name] = pose
        for child in children[name]:
            visit(child, pose)

    visit(root, np.eye(4))
    return poses


def _rodrigues(axis: np.ndarray, angle: float) -> np.ndarray:
    cross = np.array([[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]], [-axis[1], axis[0], 0.0]])
    return np.eye(3) + math.sin(angle) * cross + (1.0 - math.cos(angle)) * (cross @ cross)


def _sample_value(joint: dict, fraction: float, policy: dict) -> float:
    if joint["type"] == "continuous":
        return policy["continuous_sample_rad"] * (2 * fraction - 1)
    return joint["limit"]["lower"] + (joint["limit"]["upper"] - joint["limit"]["lower"]) * fraction


def _fk_samples(document: dict, policy: dict) -> tuple[list[dict], list[tuple[int, str, float]]]:
    movable = [joint for joint in document["joints"] if joint["type"] != "fixed"]
    requests: list[dict] = [{}]
    singles: list[tuple[int, str, float]] = []
    for fraction in policy["fk_fractions"]:
        for joint in movable:
            value = _sample_value(joint, fraction, policy)
            singles.append((len(requests), joint["name"], value))
            requests.append({joint["name"]: value})
    for fraction in policy["fk_fractions"]:
        requests.append({joint["name"]: _sample_value(joint, fraction, policy) for joint in movable})
    return requests, singles


def _expected_geoms(document: dict, config: dict, policy: dict) -> list[dict]:
    result = []
    for link in document["links"]:
        for index, visual in enumerate(link["visuals"]):
            result.append(
                {
                    "name": policy["names"]["visual"].format(link=link["name"], index=index),
                    "role": "visual",
                    "surface": visual,
                    "link": link["name"],
                }
            )
        source = config["collision"]["source"]
        if source == "urdf":
            for index, collision in enumerate(link["collisions"]):
                result.append(
                    {
                        "name": policy["names"]["collision"].format(link=link["name"], index=index),
                        "role": "collision",
                        "surface": collision,
                        "link": link["name"],
                    }
                )
        elif source == "visual_convex":
            for index, visual in enumerate(link["visuals"]):
                result.append(
                    {
                        "name": policy["names"]["convex"].format(link=link["name"], index=index),
                        "role": "collision",
                        "surface": visual,
                        "link": link["name"],
                    }
                )
    return result


def _geom_type_name(mujoco, kind: int) -> str:
    names = {
        int(getattr(mujoco.mjtGeom, name)): name
        for name in (
            "mjGEOM_PLANE",
            "mjGEOM_HFIELD",
            "mjGEOM_SPHERE",
            "mjGEOM_CAPSULE",
            "mjGEOM_ELLIPSOID",
            "mjGEOM_CYLINDER",
            "mjGEOM_BOX",
            "mjGEOM_MESH",
        )
    }
    return names.get(int(kind), f"unknown({kind})")


def _zero_pose(state: dict) -> None:
    """Return MjData to the URDF zero configuration before reading world facts."""

    import mujoco

    model = _need(state, "robot_model")
    data = _need(state, "robot_data")
    data.qpos[:] = model.qpos0
    mujoco.mj_forward(model, data)


# --------------------------------------------------------------------------
# Independent support-site derivation (own implementation, source geometry)
# --------------------------------------------------------------------------


def _origin_matrix(origin: dict) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, :3] = _rpy_matrix(origin["rpy"])
    matrix[:3, 3] = origin["xyz"]
    return matrix


def _support_geometry(surface: dict, link_pose: np.ndarray, meshes: dict, directory: str, where: str) -> dict:
    world = link_pose @ _origin_matrix(surface["origin"])
    geometry = surface["geometry"]
    if geometry["kind"] == "mesh":
        reference = _canonical_mesh_path(directory, geometry["filename"], where)
        raw = meshes[reference] * np.asarray(geometry["scale"], dtype=float)
        return {"kind": "mesh", "vertices": raw @ world[:3, :3].T + world[:3, 3]}
    if geometry["kind"] == "box":
        return {
            "kind": "box",
            "center": world[:3, 3].copy(),
            "rotation": world[:3, :3].copy(),
            "half": np.asarray(geometry["size"], dtype=float) / 2,
        }
    if geometry["kind"] == "sphere":
        return {"kind": "sphere", "center": world[:3, 3].copy(), "radius": float(geometry["radius"])}
    return {
        "kind": "cylinder",
        "center": world[:3, 3].copy(),
        "rotation": world[:3, :3].copy(),
        "radius": float(geometry["radius"]),
        "half_length": float(geometry["length"]) / 2,
    }


def _support_minimum(geometry: dict, direction: np.ndarray) -> float:
    direction = np.asarray(direction, dtype=float)
    kind = geometry["kind"]
    if kind == "mesh":
        return float(np.min(np.asarray(geometry["vertices"]) @ direction))
    centre = np.asarray(geometry["center"])
    reach = float(direction @ centre)
    if kind == "box":
        local = np.asarray(geometry["rotation"]).T @ direction
        return reach - float(np.sum(np.abs(local) * np.asarray(geometry["half"])))
    if kind == "sphere":
        return reach - float(geometry["radius"])
    axis = np.asarray(geometry["rotation"])[:, 2]
    along = float(direction @ axis)
    return (
        reach
        - float(geometry["half_length"]) * abs(along)
        - float(geometry["radius"]) * math.sqrt(max(0.0, 1.0 - along * along))
    )


def _support_minimum_height(geometry: dict) -> float:
    kind = geometry["kind"]
    if kind == "mesh":
        return float(np.min(np.asarray(geometry["vertices"])[:, 2]))
    centre = np.asarray(geometry["center"])
    if kind == "box":
        local = np.asarray(geometry["rotation"])[2, :]
        return float(centre[2] - np.sum(np.abs(local) * np.asarray(geometry["half"])))
    if kind == "sphere":
        return float(centre[2] - float(geometry["radius"]))
    axis = np.asarray(geometry["rotation"])[:, 2]
    along = float(axis[2])
    return float(
        centre[2]
        - float(geometry["half_length"]) * abs(along)
        - float(geometry["radius"]) * math.sqrt(max(0.0, 1.0 - along * along))
    )


def _hull_2d(points: np.ndarray) -> np.ndarray:
    order = np.lexsort((points[:, 1], points[:, 0]))
    ordered = points[order]
    stacked = [ordered[0]]
    for row in ordered[1:]:
        if row[0] != stacked[-1][0] or row[1] != stacked[-1][1]:
            stacked.append(row)
    if len(stacked) < 3:
        return np.array(stacked)

    def turn(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    epsilon = 1e-12
    lower: list = []
    for point in stacked:
        while len(lower) >= 2 and turn(lower[-2], lower[-1], point) <= epsilon:
            lower.pop()
        lower.append(point)
    upper: list = []
    for point in reversed(stacked):
        while len(upper) >= 2 and turn(upper[-2], upper[-1], point) <= epsilon:
            upper.pop()
        upper.append(point)
    return np.array(lower[:-1] + upper[:-1])


def _hull_metrics(polygon: np.ndarray) -> tuple[float, np.ndarray]:
    total = 0.0
    centroid = np.zeros(2)
    for index in range(len(polygon)):
        first = polygon[index]
        second = polygon[(index + 1) % len(polygon)]
        cross = first[0] * second[1] - second[0] * first[1]
        total += cross
        centroid += cross * (first + second)
    if abs(total) < 1e-12:
        return 0.0, np.zeros(2)
    return abs(total) / 2, centroid / (3 * total)


def _derive_support_frame(name: str, link: str, geometries: list[dict], policy: dict) -> tuple[np.ndarray, np.ndarray]:
    """Recompute the unique largest contact hull directly from the source triangles."""

    tolerances = policy["tolerances"]
    triangles = []
    for geometry in geometries:
        if geometry["kind"] == "mesh":
            triangles.append(np.asarray(geometry["vertices"]).reshape(-1, 3, 3))
    if not triangles:
        _fail(f"support site {name!r}", f"link {link!r} has no binary STL in the selected contact geometry")
    candidates: list[tuple[np.ndarray, float, np.ndarray]] = []
    for face in np.concatenate(triangles):
        raw = np.cross(face[1] - face[0], face[2] - face[0])
        length = float(np.linalg.norm(raw))
        if length <= tolerances["support_triangle_eps"]:
            continue
        normal = raw / length
        offset = float(normal @ face[0])
        if normal[2] < 0:
            normal, offset = -normal, -offset
        if normal[2] <= tolerances["support_normal_z_atol"]:
            continue
        candidates.append((normal, offset, face))
    if not candidates:
        _fail(f"support site {name!r}", f"link {link!r} has no upward support plane candidate")
    supporting = []
    for normal, offset, _ in candidates:
        allowed = tolerances["support_plane_atol_m"] + tolerances["support_plane_rtol"] * abs(offset)
        if all(_support_minimum(geometry, normal) >= offset - allowed for geometry in geometries):
            supporting.append((normal, offset))
    if not supporting:
        _fail(
            f"support site {name!r}",
            f"no triangle plane lies below the whole contact geometry of link {link!r}",
        )
    clusters: list[dict] = []
    for normal, offset in supporting:
        for cluster in clusters:
            if (
                float(np.max(np.abs(np.cross(cluster["normal"], normal)))) <= tolerances["support_merge_angle_atol"]
                and abs(cluster["offset"] - offset)
                <= tolerances["support_merge_offset_atol_m"] + tolerances["support_plane_rtol"] * abs(cluster["offset"])
            ):
                break
        else:
            clusters.append({"normal": normal, "offset": offset})
    contact_vertices = np.concatenate(
        [np.asarray(geometry["vertices"]) for geometry in geometries if geometry["kind"] == "mesh"]
    )
    for cluster in clusters:
        normal = cluster["normal"]
        allowed = tolerances["support_plane_atol_m"] + tolerances["support_plane_rtol"] * abs(cluster["offset"])
        points = contact_vertices[np.abs(contact_vertices @ normal - cluster["offset"]) <= allowed]
        helper = np.eye(3)[int(np.argmin(np.abs(normal)))]
        first_axis = np.cross(normal, helper)
        first_axis = first_axis / np.linalg.norm(first_axis)
        second_axis = np.cross(normal, first_axis)
        centred = points - cluster["offset"] * normal
        coordinates = np.column_stack([centred @ first_axis, centred @ second_axis])
        hull = _hull_2d(coordinates)
        area, centroid = _hull_metrics(hull)
        cluster["area"] = area
        cluster["centroid"] = centroid
        cluster["basis"] = (first_axis, second_axis)
    ranked = sorted(clusters, key=lambda cluster: -cluster["area"])
    if ranked[0]["area"] <= tolerances["support_hull_eps"]:
        _fail(f"support site {name!r}", f"the contact hull of link {link!r} is degenerate")
    if len(ranked) > 1 and ranked[0]["area"] - ranked[1]["area"] <= tolerances["support_area_rtol"] * ranked[0]["area"]:
        _fail(f"support site {name!r}", f"link {link!r} has several support planes with the same contact area")
    chosen = ranked[0]
    normal = chosen["normal"]
    world_x = np.array([1.0, 0.0, 0.0])
    projection = world_x - float(world_x @ normal) * normal
    length = float(np.linalg.norm(projection))
    if length <= tolerances["support_xaxis_parallel_atol"]:
        _fail(f"support site {name!r}", f"world X is parallel to the support plane of link {link!r}")
    x_axis = projection / length
    z_axis = normal
    y_axis = np.cross(z_axis, x_axis)
    first_axis, second_axis = chosen["basis"]
    origin = (
        chosen["offset"] * normal
        + chosen["centroid"][0] * first_axis
        + chosen["centroid"][1] * second_axis
    )
    return origin, np.column_stack([x_axis, y_axis, z_axis])


def _support_expectations(state: dict) -> dict:
    """Derived support frames and the supports-floor height, recomputed from source geometry."""

    if "support_expected" in state:
        return state["support_expected"]
    policy = state["policy"]
    config = _need(state, "config")
    document = _need(state, "document")
    meshes = _need(state, "meshes")
    directory = "/".join(_need(state, "urdf_relative").split("/")[:-1])
    result: dict = {"sites": [], "height": None}
    if config["support_sites"]:
        source = config["collision"]["source"]
        if source == "none":
            _fail("support_sites", "a contact geometry source is required")
        links = {link["name"]: link for link in document["links"]}
        poses = _fk_pose(document, {})
        height = math.inf
        for descriptor in config["support_sites"]:
            link = links.get(descriptor["link"])
            if link is None:
                _fail(f"support site {descriptor['name']!r}", f"unknown link {descriptor['link']!r}")
            surfaces = link["collisions"] if source == "urdf" else link["visuals"]
            if not surfaces:
                _fail(f"support site {descriptor['name']!r}", f"link {descriptor['link']!r} has no {source} geometry")
            where = f"support site {descriptor['name']!r}"
            geometries = [
                _support_geometry(surface, poses[link["name"]], meshes, directory, where) for surface in surfaces
            ]
            for geometry in geometries:
                height = min(height, _support_minimum_height(geometry))
            origin, rotation = _derive_support_frame(descriptor["name"], link["name"], geometries, policy)
            link_pose = poses[link["name"]]
            result["sites"].append(
                {
                    "name": descriptor["name"],
                    "link": descriptor["link"],
                    "xyz": link_pose[:3, :3].T @ (origin - link_pose[:3, 3]),
                    "rotation": link_pose[:3, :3].T @ rotation,
                }
            )
        if config["floor"] is not None and config["floor"]["supports"]:
            result["height"] = float(height)
    state["support_expected"] = result
    return result


# --------------------------------------------------------------------------
# Gates (inside the child)
# --------------------------------------------------------------------------


def _gate_identity_closure(state: dict) -> dict:
    policy = state["policy"]
    root = state["root"]
    manifest = _read_manifest(root, policy)
    state["manifest"] = manifest
    state["manifest_sha256"] = _canonical_digest(manifest)
    if manifest["tool"] != policy["tool"]:
        raise CheckError("the manifest names a different tool revision; rebuild the delivery")
    if manifest["engine"] != policy["engine"]:
        raise CheckError("the manifest names a different engine revision; rebuild the delivery")

    actual = set(_inventory(root))
    allowed = set(manifest["files"]) | {policy["manifest_name"], policy["report_path"]}
    required = set(manifest["files"]) | {policy["manifest_name"]}
    unregistered = sorted(actual - allowed)
    if unregistered:
        raise CheckError(f"unregistered files in the delivery: {unregistered}")
    if required - actual:
        raise CheckError(f"missing delivered files: {sorted(required - actual)}")
    for relative, record in sorted(manifest["files"].items()):
        content = _confined(root, relative).read_bytes()
        if len(content) != record["bytes"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise CheckError(f"the sealed record of {relative!r} does not match the delivered bytes")

    config = _read_simulation(_confined(root, policy["config_name"]).read_bytes(), policy)
    state["config"] = config
    urdf_relative = manifest["source"]["urdf"]
    state["urdf_relative"] = urdf_relative
    urdf_input = f"{policy['input_root']}/{urdf_relative}"
    if urdf_input not in manifest["files"]:
        raise CheckError(f"the manifest does not seal the source URDF {urdf_relative!r}")
    document = _read_urdf(_confined(root, urdf_input).read_bytes(), urdf_input, policy)
    state["document"] = document

    directory = "/".join(urdf_relative.split("/")[:-1])
    mesh_relative = sorted(
        {
            _canonical_mesh_path(directory, surface["geometry"]["filename"], "mesh reference")
            for surface in _mesh_surfaces(document)
        }
    )
    state["mesh_relative"] = mesh_relative
    expected_inputs = {f"{policy['input_root']}/{urdf_relative}"}
    expected_inputs.update(f"{policy['input_root']}/{path}" for path in mesh_relative)
    if expected_inputs != {key for key in manifest["files"] if key.startswith(f"{policy['input_root']}/")}:
        raise CheckError("the sealed input closure differs from the references of the frozen URDF")
    if sorted(manifest["source"]["meshes"]) != mesh_relative:
        raise CheckError("the manifest source list differs from the references of the frozen URDF")
    expected_files = set(expected_inputs) | {policy["config_name"], policy["robot_name"]}
    if config["floor"] is not None:
        expected_files.add(policy["scene_name"])
    if set(manifest["files"]) != expected_files:
        raise CheckError("the manifest file list is not the projected delivery file set")

    meshes = {}
    for path in mesh_relative:
        meshes[path] = _read_binary_stl(
            _confined(root, f"{policy['input_root']}/{path}").read_bytes(), path
        )
    state["meshes"] = meshes
    requests, singles = _fk_samples(document, policy)
    state["fk_requests"] = requests
    state["fk_singles"] = singles
    state["closure_ok"] = True
    return {
        "manifest_sha256": state["manifest_sha256"],
        "files": len(manifest["files"]),
        "meshes": len(mesh_relative),
        "links": len(document["links"]),
        "joints": len(document["joints"]),
        "samples": len(requests),
    }


def _gate_engine(state: dict) -> dict:
    import mujoco

    policy = state["policy"]
    if state.get("closure_ok") is not True:
        raise CheckError("the sealed input closure did not pass; refusing to read the delivery")
    manifest = _need(state, "manifest")
    if mujoco.__version__ != policy["engine"]["mujoco"]:
        raise CheckError(
            f"the loaded engine version {mujoco.__version__!r} differs from the locked "
            f"{policy['engine']['mujoco']!r}"
        )
    sealed = {key for key in manifest["files"] if key.startswith(policy["input_root"] + "/")}
    robot_xml = _xml_document(_confined(state["root"], policy["robot_name"]).read_bytes(), policy["robot_name"])
    _checked_syntax(robot_xml, policy, "robot", policy["robot_name"])
    _checked_asset_paths(robot_xml, policy["robot_name"], policy, sealed, state["root"])
    robot_path = state["root"] / policy["robot_name"]
    warnings: list[str] = []
    mujoco.set_mju_user_warning(lambda text: warnings.append(text) or 0)
    try:
        model = mujoco.MjModel.from_xml_path(str(robot_path))
    except Exception as error:  # noqa: BLE001 (the child reports engine failures verbatim)
        raise CheckError(f"loading the delivered robot.xml failed: {type(error).__name__}: {error}") from error
    finally:
        mujoco.set_mju_user_warning(None)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    state["robot_model"] = model
    state["robot_data"] = data
    scene_path = state["root"] / policy["scene_name"]
    if scene_path.exists():
        scene_xml = _xml_document(_confined(state["root"], policy["scene_name"]).read_bytes(), policy["scene_name"])
        _checked_syntax(scene_xml, policy, "scene", policy["scene_name"])
        includes = scene_xml.findall("include")
        if len(includes) != 1 or includes[0].get("file") != policy["robot_name"]:
            raise CheckError(f"scene.xml must include exactly {policy['robot_name']!r}")
        scene_warnings: list[str] = []
        mujoco.set_mju_user_warning(lambda text: scene_warnings.append(text) or 0)
        try:
            scene_model = mujoco.MjModel.from_xml_path(str(scene_path))
        except Exception as error:  # noqa: BLE001
            raise CheckError(f"loading the delivered scene.xml failed: {type(error).__name__}: {error}") from error
        finally:
            mujoco.set_mju_user_warning(None)
        state["scene_model"] = scene_model
        state["scene_data"] = mujoco.MjData(scene_model)
        mujoco.mj_forward(scene_model, state["scene_data"])
        state["scene_warnings"] = scene_warnings
    return {"engine": mujoco.__version__, "warnings": warnings}


def _gate_topology_entities(state: dict) -> dict:
    document = _need(state, "document")
    model = _need(state, "robot_model")
    links = [link["name"] for link in document["links"]]
    names = [model.body(index).name for index in range(model.nbody)]
    if not names or names[0] != "world":
        raise CheckError("the compiled model must start at the world body")
    if sorted(names[1:]) != sorted(links):
        raise CheckError("the compiled body set differs from the URDF link set")
    expected_parent = {link["name"]: "world" for link in document["links"]}
    for joint in document["joints"]:
        expected_parent[joint["child"]] = joint["parent"]
    for index in range(1, model.nbody):
        name = model.body(index).name
        parent = model.body(int(model.body_parentid[index])).name
        if parent != expected_parent[name]:
            raise CheckError(f"body {name!r} is attached to {parent!r} instead of {expected_parent[name]!r}")
    joint_names = [model.joint(index).name for index in range(model.njnt)]
    types = [int(model.jnt_type[index]) for index in range(model.njnt)]
    free = [index for index, kind in enumerate(types) if kind == 0]
    if any(not joint_names[index] for index, kind in enumerate(types) if kind != 0):
        raise CheckError("the compiled model holds an unnamed non-free joint")
    movable = [joint["name"] for joint in document["joints"] if joint["type"] != "fixed"]
    named = sorted(name for name in joint_names if name)
    if len(named) != len(set(named)):
        raise CheckError("the compiled model has duplicate joint names")
    if named != sorted(movable):
        raise CheckError("the compiled joint set differs from the URDF movable joints")
    if len(free) > 1:
        raise CheckError("more than one free joint was generated")
    state["free_joint"] = free[0] if free else None
    return {"bodies": int(model.nbody) - 1, "joints": int(model.njnt), "movable": len(movable)}


def _gate_topology_dof(state: dict) -> dict:
    policy = state["policy"]
    document = _need(state, "document")
    config = _need(state, "config")
    model = _need(state, "robot_model")
    movable = [joint for joint in document["joints"] if joint["type"] != "fixed"]
    free = state.get("free_joint")
    expected_nq = len(movable) + (7 if free is not None else 0)
    expected_nv = len(movable) + (6 if free is not None else 0)
    if config["base"] == "floating" and free is None:
        raise CheckError("a floating base requires exactly one free joint on the root body")
    if config["base"] == "fixed" and free is not None:
        raise CheckError("a fixed base must not carry a free joint")
    if free is not None:
        root_name = next(
            link["name"]
            for link in document["links"]
            if all(joint["child"] != link["name"] for joint in document["joints"])
        )
        if model.body(int(model.jnt_bodyid[free])).name != root_name:
            raise CheckError("the free joint must sit on the root link body")
    if int(model.nq) != expected_nq or int(model.nv) != expected_nv:
        raise CheckError(
            f"the compiled model has nq/nv {int(model.nq)}/{int(model.nv)}, expected {expected_nq}/{expected_nv}"
        )
    expected = np.zeros(expected_nq)
    if free is not None:
        expected[3] = 1.0
    residual = _close(model.qpos0, expected, policy["tolerances"]["qpos0_atol"])
    mapping = {"revolute": 3, "continuous": 3, "prismatic": 2}
    for joint in movable:
        index = next(index for index in range(model.njnt) if model.joint(index).name == joint["name"])
        if int(model.jnt_type[index]) != mapping[joint["type"]]:
            raise CheckError(
                f"joint {joint['name']!r} compiles as type {int(model.jnt_type[index])} "
                f"instead of {mapping[joint['type']]}"
            )
    return {"nq": int(model.nq), "nv": int(model.nv), "qpos0_residual": residual}


def _gate_kinematics_fk(state: dict) -> dict:
    import mujoco

    policy = state["policy"]
    document = _need(state, "document")
    model = _need(state, "robot_model")
    data = _need(state, "robot_data")
    requests = _need(state, "fk_requests")
    by_name = {model.joint(index).name: index for index in range(model.njnt) if model.joint(index).name}
    worst_position = 0.0
    worst_rotation = 0.0
    for values in requests:
        qpos = model.qpos0.copy()
        for joint_name, value in values.items():
            qpos[int(model.jnt_qposadr[by_name[joint_name]])] = value
        data.qpos[:] = qpos
        mujoco.mj_forward(model, data)
        poses = _fk_pose(document, values)
        for link_name, pose in poses.items():
            body = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, link_name)
            if body < 0:
                raise CheckError(f"body {link_name!r} is missing from the compiled model")
            position_residual = float(np.max(np.abs(np.asarray(data.xpos[body]) - pose[:3, 3])))
            rotation = np.asarray(data.xmat[body]).reshape(3, 3)
            rotation_residual = float(np.max(np.abs(rotation - pose[:3, :3])))
            worst_position = max(worst_position, position_residual)
            worst_rotation = max(worst_rotation, rotation_residual)
            if position_residual > policy["tolerances"]["position_atol_m"]:
                raise CheckError(f"link {link_name!r} sits {position_residual:.18g} m from the URDF pose")
            if rotation_residual > policy["tolerances"]["rotation_atol"]:
                raise CheckError(f"link {link_name!r} rotates {rotation_residual:.18g} from the URDF pose")
    return {
        "samples": len(requests),
        "max_position_residual_m": worst_position,
        "max_rotation_residual": worst_rotation,
    }


def _gate_kinematics_axes(state: dict) -> dict:
    import mujoco

    policy = state["policy"]
    document = _need(state, "document")
    model = _need(state, "robot_model")
    data = _need(state, "robot_data")
    requests = _need(state, "fk_requests")
    singles = _need(state, "fk_singles")
    by_name = {model.joint(index).name: index for index in range(model.njnt) if model.joint(index).name}
    worst = 0.0
    for index, joint_name, _ in singles:
        joint = next(item for item in document["joints"] if item["name"] == joint_name)
        compiled = by_name[joint_name]
        axis = np.asarray(model.jnt_axis[compiled])
        magnitude = float(np.linalg.norm(axis))
        if abs(magnitude - 1.0) > policy["tolerances"]["axis_unit_atol"]:
            raise CheckError(f"joint {joint_name!r} carries a non-unit axis (norm {magnitude:.18g})")
        worst = max(worst, _close(axis, joint["axis"], policy["tolerances"]["position_atol_m"]))
        qpos = model.qpos0.copy()
        for name, value in requests[index].items():
            qpos[int(model.jnt_qposadr[by_name[name]])] = value
        data.qpos[:] = qpos
        mujoco.mj_forward(model, data)
        child = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, joint["child"])
        poses = _fk_pose(document, requests[index])
        expected_world = poses[joint["child"]][:3, :3] @ np.asarray(joint["axis"], dtype=float)
        compiled_world = np.asarray(data.xmat[child]).reshape(3, 3) @ axis
        residual = float(np.max(np.abs(compiled_world - expected_world)))
        worst = max(worst, residual)
        if residual > policy["tolerances"]["rotation_atol"]:
            raise CheckError(f"joint {joint_name!r} axis points {residual:.18g} away from the URDF direction")
    return {"single_samples": len(singles), "max_axis_residual": worst}


def _gate_inertia_links(state: dict) -> dict:
    policy = state["policy"]
    document = _need(state, "document")
    model = _need(state, "robot_model")
    tolerances = policy["tolerances"]
    worst_mass = 0.0
    worst_tensor = 0.0
    worst_com = 0.0
    for link in document["links"]:
        index = next(index for index in range(model.nbody) if model.body(index).name == link["name"])
        inertial = link["inertial"]
        if inertial is None:
            residual = _scaled_close(model.body_mass[index], 0.0, tolerances["mass_rtol"], tolerances["mass_atol"])
            worst_mass = max(worst_mass, residual)
            continue
        worst_mass = max(
            worst_mass,
            _scaled_close(model.body_mass[index], inertial["mass"], tolerances["mass_rtol"], tolerances["mass_atol"]),
        )
        worst_com = max(
            worst_com, _close(model.body_ipos[index], inertial["origin"]["xyz"], tolerances["position_atol_m"])
        )
        rotation = _rpy_matrix(inertial["origin"]["rpy"])
        expected = rotation @ inertial["tensor"] @ rotation.T
        quat_rotation = _rotation_from_quat(model.body_iquat[index])
        recovered = quat_rotation @ np.diag(np.asarray(model.body_inertia[index])) @ quat_rotation.T
        worst_tensor = max(
            worst_tensor,
            _scaled_close(recovered, expected, tolerances["inertia_rtol"], tolerances["inertia_atol"]),
        )
    return {
        "max_mass_residual_kg": worst_mass,
        "max_com_residual_m": worst_com,
        "max_tensor_residual": worst_tensor,
    }


def _gate_inertia_total(state: dict) -> dict:
    policy = state["policy"]
    document = _need(state, "document")
    model = _need(state, "robot_model")
    _zero_pose(state)
    data = _need(state, "robot_data")
    tolerances = policy["tolerances"]
    poses = _fk_pose(document, {})
    total_mass = 0.0
    centre = np.zeros(3)
    for link in document["links"]:
        if link["inertial"] is None:
            continue
        mass = link["inertial"]["mass"]
        pose = poses[link["name"]]
        point = pose[:3, :3] @ np.asarray(link["inertial"]["origin"]["xyz"]) + pose[:3, 3]
        total_mass += mass
        centre += mass * point
    centre = centre / total_mass
    expected_tensor = np.zeros((3, 3))
    for link in document["links"]:
        if link["inertial"] is None:
            continue
        mass = link["inertial"]["mass"]
        pose = poses[link["name"]]
        point = pose[:3, :3] @ np.asarray(link["inertial"]["origin"]["xyz"]) + pose[:3, 3]
        rotation = _rpy_matrix(link["inertial"]["origin"]["rpy"])
        local = rotation @ link["inertial"]["tensor"] @ rotation.T
        world = pose[:3, :3] @ local @ pose[:3, :3].T
        expected_tensor += world + mass * (float(point @ point) * np.eye(3) - np.outer(point, point))

    compiled_mass = float(np.sum(model.body_mass))
    compiled_centre = np.zeros(3)
    compiled_tensor = np.zeros((3, 3))
    for index in range(1, model.nbody):
        mass = float(model.body_mass[index])
        principal_rotation = _rotation_from_quat(model.body_iquat[index])
        body_rotation = np.asarray(data.xmat[index]).reshape(3, 3)
        position = np.asarray(data.xpos[index])
        local = principal_rotation @ np.diag(np.asarray(model.body_inertia[index])) @ principal_rotation.T
        world = body_rotation @ local @ body_rotation.T
        point = position + body_rotation @ np.asarray(model.body_ipos[index])
        compiled_centre += mass * point
        compiled_tensor += world + mass * (float(point @ point) * np.eye(3) - np.outer(point, point))
    compiled_centre = compiled_centre / compiled_mass
    mass_residual = _scaled_close(compiled_mass, total_mass, tolerances["mass_rtol"], tolerances["mass_atol"])
    centre_residual = _close(compiled_centre, centre, tolerances["position_atol_m"])
    tensor_residual = _scaled_close(
        compiled_tensor, expected_tensor, tolerances["inertia_rtol"], tolerances["inertia_atol"]
    )
    return {
        "mass_kg": total_mass,
        "max_mass_residual_kg": mass_residual,
        "max_com_residual_m": centre_residual,
        "max_tensor_residual": tensor_residual,
    }


def _gate_geometry_shapes(state: dict) -> dict:
    policy = state["policy"]
    document = _need(state, "document")
    config = _need(state, "config")
    model = _need(state, "robot_model")
    expected = _expected_geoms(document, config, policy)
    compiled = {}
    for index in range(model.ngeom):
        name = model.geom(index).name
        if not name:
            raise CheckError("the compiled model holds an unnamed geom")
        if name in compiled:
            raise CheckError(f"duplicate compiled geom name {name!r}")
        compiled[name] = index
    expected_names = {item["name"] for item in expected}
    if sorted(compiled) != sorted(expected_names):
        missing = sorted(expected_names - set(compiled))
        extra = sorted(set(compiled) - expected_names)
        raise CheckError(f"the compiled geom set differs from the projection (missing {missing}, extra {extra})")
    contact = policy["contact"]["robot"] if config["collision"]["self_collision"] else policy["contact"]["solo"]
    friction = config["collision"]["friction"]
    for item in expected:
        index = compiled[item["name"]]
        group = int(model.geom_group[index])
        if group != policy["roles"][item["role"]]:
            raise CheckError(f"geom {item['name']!r} carries role group {group}")
        if item["role"] == "visual":
            if int(model.geom_contype[index]) != 0 or int(model.geom_conaffinity[index]) != 0:
                raise CheckError(f"visual geom {item['name']!r} participates in contact")
        else:
            mask = (int(model.geom_contype[index]), int(model.geom_conaffinity[index]))
            if mask != tuple(contact):
                raise CheckError(
                    f"collision geom {item['name']!r} carries contact mask {mask[0]}/{mask[1]} "
                    f"instead of {tuple(contact)}"
                )
            if int(model.geom_condim[index]) != policy["contact"]["condim"]:
                raise CheckError(f"collision geom {item['name']!r} has condim {int(model.geom_condim[index])}")
            _close(model.geom_friction[index], friction, policy["tolerances"]["friction_atol"])
    return {
        "visual_geoms": sum(1 for item in expected if item["role"] == "visual"),
        "collision_geoms": sum(1 for item in expected if item["role"] == "collision"),
        "collision_source": config["collision"]["source"],
    }


def _robot_assets(state: dict) -> dict:
    policy = state["policy"]
    text = _confined(state["root"], policy["robot_name"]).read_bytes()
    try:
        document = ET.fromstring(text)
    except ET.ParseError as error:
        raise CheckError(f"robot.xml is not well-formed XML: {error}") from error
    if document.tag != "mujoco":
        raise CheckError("robot.xml must have a <mujoco> root")
    assets = {}
    asset_element = document.find("asset")
    if asset_element is not None:
        for mesh in asset_element.findall("mesh"):
            name = mesh.get("name")
            file = mesh.get("file")
            if not name or not file:
                raise CheckError("a mesh asset is missing its name or file")
            if name in assets:
                raise CheckError(f"duplicate mesh asset {name!r}")
            scale = mesh.get("scale")
            assets[name] = {
                "file": file,
                "scale": (1.0, 1.0, 1.0) if scale is None else tuple(float(item) for item in scale.split()),
            }
    return assets


# --------------------------------------------------------------------------
# Closed generated-XML syntax, checked before the engine ever reads a file
# --------------------------------------------------------------------------


def _xml_document(content: bytes, label: str) -> ET.Element:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        _fail(label, f"the generated XML must be UTF-8: {error}")
    for marker in ("<!DOCTYPE", "<!ENTITY"):
        if marker in text:
            _fail(label, "DTDs and external entities are not supported")
    try:
        return ET.fromstring(content)
    except ET.ParseError as error:
        _fail(label, f"invalid XML: {error}")


def _checked_syntax(document: ET.Element, policy: dict, kind: str, label: str) -> None:
    """Only the elements and attributes the projection emits are legal.

    Comments and formatting are ignored; anything else (defaults, equality,
    includes inside the robot, gravcomp/armature/flag and friends) fails before
    the engine is allowed to read the file.
    """

    tables = policy["xml_syntax"][kind]
    attributes = {tag: set(names) for tag, names in tables["attributes"].items()}
    structure = {tag: set(names) for tag, names in tables["structure"].items()}
    numeric = set(tables["numeric"])
    if document.tag not in attributes:
        _fail(label, f"unsupported element <{document.tag}>")
    pending = [document]
    while pending:
        element = pending.pop()
        extra = sorted(set(element.attrib) - attributes[element.tag])
        if extra:
            _fail(f"{label}: <{element.tag}>", f"unsupported attribute {extra[0]!r}")
        for key, value in sorted(element.attrib.items()):
            if key not in numeric:
                continue
            for token in value.split():
                try:
                    number = float(token)
                except ValueError:
                    _fail(f"{label}: <{element.tag}>@{key}", f"{token!r} is not a number")
                if not math.isfinite(number):
                    _fail(f"{label}: <{element.tag}>@{key}", f"{token!r} is not finite")
        for child in element:
            if child.tag is ET.Comment or not isinstance(child.tag, str):
                continue
            if child.tag not in attributes:
                _fail(f"{label}: <{element.tag}>", f"unsupported element <{child.tag!r}>")
            if child.tag not in structure[element.tag]:
                _fail(f"{label}: <{element.tag}>", f"<{child.tag}> is not allowed here")
            pending.append(child)


def _checked_asset_paths(document: ET.Element, label: str, policy: dict, sealed: set, root: Path) -> None:
    for mesh in document.iter("mesh"):
        file = mesh.get("file")
        if not file or "://" in file or file.startswith("/") or "\\" in file:
            _fail(f"{label}: <mesh>", f"mesh file path {file!r} is not a bundle-relative POSIX path")
        parts: list[str] = []
        for part in file.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    _fail(f"{label}: <mesh>", f"mesh file {file!r} escapes the bundle")
                parts.pop()
            else:
                parts.append(part)
        canonical = "/".join(parts)
        if canonical not in sealed:
            _fail(f"{label}: <mesh>", f"mesh file {file!r} is not a sealed delivery input")
        _confined(root, canonical)


def _gate_geometry_instances(state: dict) -> dict:
    policy = state["policy"]
    document = _need(state, "document")
    config = _need(state, "config")
    model = _need(state, "robot_model")
    _zero_pose(state)
    data = _need(state, "robot_data")
    meshes = _need(state, "meshes")
    expected = _expected_geoms(document, config, policy)
    assets = _robot_assets(state)
    directory = "/".join(_need(state, "urdf_relative").split("/")[:-1])
    poses = _fk_pose(document, {})
    worst_mesh = 0.0
    referenced_assets = set()
    for item in expected:
        try:
            worst_mesh = _geometry_instance(
                item, model, data, assets, meshes, directory, poses, referenced_assets, worst_mesh, policy
            )
        except CheckError as error:
            raise CheckError(f"{item['name']}: {error}") from error
    if referenced_assets != set(assets):
        raise CheckError(f"unused mesh assets: {sorted(set(assets) - referenced_assets)}")
    return {
        "mesh_geoms": sum(1 for item in expected if item["surface"]["geometry"]["kind"] == "mesh"),
        "max_vertex_residual_m": worst_mesh,
    }


def _geometry_instance(item, model, data, assets, meshes, directory, poses, referenced_assets, worst_mesh, policy):
    import mujoco

    index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, item["name"])
    if index < 0:
        raise CheckError("the geom is missing from the compiled model")
    surface = item["surface"]
    origin = np.eye(4)
    origin[:3, :3] = _rpy_matrix(surface["origin"]["rpy"])
    origin[:3, 3] = surface["origin"]["xyz"]
    world = poses[item["link"]] @ origin
    kind = surface["geometry"]["kind"]
    if kind == "mesh":
        mesh_id = int(model.geom_dataid[index])
        asset_name = model.mesh(mesh_id).name
        if asset_name not in assets:
            raise CheckError(f"references undeclared mesh asset {asset_name!r}")
        referenced_assets.add(asset_name)
        asset = assets[asset_name]
        file = asset["file"]
        if "://" in file or file.startswith("/") or "\\" in file:
            raise CheckError(f"mesh asset {asset_name!r} uses a non-portable file path {file!r}")
        parts: list[str] = []
        for part in file.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise CheckError(f"mesh asset {asset_name!r} escapes the bundle")
                parts.pop()
            else:
                parts.append(part)
        canonical = "/".join(parts)
        reference = _canonical_mesh_path(directory, surface["geometry"]["filename"], f"mesh geom {item['name']!r}")
        expected_file = f"{policy['input_root']}/{reference}"
        if canonical != expected_file:
            raise CheckError(f"references {canonical!r} instead of {expected_file!r}")
        if asset["scale"] != tuple(surface["geometry"]["scale"]):
            raise CheckError(f"declares scale {asset['scale']} instead of {surface['geometry']['scale']}")
        start = int(model.mesh_vertadr[mesh_id])
        count = int(model.mesh_vertnum[mesh_id])
        stored = np.asarray(model.mesh_vert[start : start + count], dtype=float)
        rotation = np.asarray(data.geom_xmat[index]).reshape(3, 3)
        engine_world = stored @ rotation.T + np.asarray(data.geom_xpos[index])
        source_vertices = meshes[reference] * np.asarray(surface["geometry"]["scale"], dtype=float)
        expected_world = source_vertices @ world[:3, :3].T + world[:3, 3]
        worst_mesh = max(worst_mesh, _compare_vertex_sets(engine_world, expected_world, policy))
    else:
        expected_type = {"box": "mjGEOM_BOX", "sphere": "mjGEOM_SPHERE", "cylinder": "mjGEOM_CYLINDER"}[kind]
        compiled_type = _geom_type_name(mujoco, model.geom_type[index])
        if compiled_type != expected_type:
            raise CheckError(f"compiles as {compiled_type} instead of {expected_type}")
        size = np.asarray(model.geom_size[index], dtype=float)
        if kind == "box":
            _close(size, [value / 2 for value in surface["geometry"]["size"]], policy["tolerances"]["position_atol_m"])
        elif kind == "sphere":
            _close(size[0], surface["geometry"]["radius"], policy["tolerances"]["position_atol_m"])
        else:
            _close(
                size[:2],
                [surface["geometry"]["radius"], surface["geometry"]["length"] / 2],
                policy["tolerances"]["position_atol_m"],
            )
        _close(np.asarray(data.geom_xpos[index]), world[:3, 3], policy["tolerances"]["position_atol_m"])
        _close(
            np.asarray(data.geom_xmat[index]).reshape(3, 3),
            world[:3, :3],
            policy["tolerances"]["rotation_atol"],
        )
    expected_color = surface.get("color") or (0.5, 0.5, 0.5, 1.0)
    _close(model.geom_rgba[index], expected_color, policy["tolerances"]["rgba_atol"])
    return worst_mesh


def _compare_vertex_sets(engine: np.ndarray, expected: np.ndarray, policy: dict) -> float:
    """Welding, reordering and float32 rounding are representation details.

    The vertex sets must agree within the declared tolerance in both directions:
    every engine vertex needs an STL counterpart and every STL vertex needs an
    engine counterpart (welding may collapse coincident copies on either side).
    """

    tolerances = policy["tolerances"]
    atol, rtol = tolerances["mesh_atol_m"], tolerances["mesh_rtol"]
    first = np.unique(np.asarray(engine, dtype=float), axis=0)
    second = np.unique(np.asarray(expected, dtype=float), axis=0)
    if len(first) == 0 or len(second) == 0:
        if len(first) != len(second):
            raise CheckError(f"holds {len(first)} distinct vertices instead of {len(second)}")
        return 0.0
    if first.shape == second.shape:
        residual = np.abs(first - second)
        allowed = atol + rtol * np.maximum(np.abs(first), np.abs(second))
        if np.all(residual <= allowed):
            return float(np.max(residual))
    scale = max(float(np.max(np.abs(first))), float(np.max(np.abs(second))))
    cell = atol + rtol * scale
    return max(
        _matched_distance(first, second, cell, atol, rtol, "the delivered mesh"),
        _matched_distance(second, first, cell, atol, rtol, "the STL"),
    )


def _matched_distance(
    source: np.ndarray, target: np.ndarray, cell: float, atol: float, rtol: float, label: str
) -> float:
    """Worst distance of a bijective-in-existence match of source points into target points."""

    keys = np.floor(target / cell).astype(np.int64)
    origin = keys.min(axis=0)
    spans = (keys.max(axis=0) - origin) + 3
    codes = (keys[:, 0] - origin[0]) + spans[0] * ((keys[:, 1] - origin[1]) + spans[1] * (keys[:, 2] - origin[2]))
    order = np.argsort(codes, kind="stable")
    sorted_codes = codes[order]
    sorted_target = target[order]
    source_keys = np.floor(source / cell).astype(np.int64)
    matched = np.zeros(len(source), dtype=bool)
    worst = 0.0
    for offset in ((dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)):
        probe = (
            (source_keys[:, 0] + offset[0] - origin[0])
            + spans[0]
            * (
                (source_keys[:, 1] + offset[1] - origin[1])
                + spans[1] * (source_keys[:, 2] + offset[2] - origin[2])
            )
        )
        lower = np.searchsorted(sorted_codes, probe, side="left")
        upper = np.searchsorted(sorted_codes, probe, side="right")
        for index in np.nonzero(upper > lower)[0]:
            if matched[index]:
                continue
            candidates = sorted_target[lower[index] : upper[index]]
            delta = np.abs(candidates - source[index])
            limits = atol + rtol * np.maximum(np.abs(candidates), np.abs(source[index]))
            ok = np.all(delta <= limits, axis=1)
            if ok.any():
                matched[index] = True
                worst = max(worst, float(np.max(delta[ok])))
    if not matched.all():
        index = int(np.nonzero(~matched)[0][0])
        raise CheckError(f"{label}: vertex {source[index]} has no counterpart within tolerance")
    return worst


def _gate_joints_config(state: dict) -> dict:
    policy = state["policy"]
    document = _need(state, "document")
    model = _need(state, "robot_model")
    by_name = {model.joint(index).name: index for index in range(model.njnt) if model.joint(index).name}
    tolerances = policy["tolerances"]
    worst_range = 0.0
    worst_dynamics = 0.0
    for joint in document["joints"]:
        if joint["type"] == "fixed":
            continue
        index = by_name[joint["name"]]
        if joint["type"] == "continuous":
            if bool(model.jnt_limited[index]):
                raise CheckError(f"continuous joint {joint['name']!r} must stay unlimited")
        else:
            if not bool(model.jnt_limited[index]):
                raise CheckError(f"joint {joint['name']!r} is missing its limits")
            worst_range = max(
                worst_range,
                _close(
                    model.jnt_range[index],
                    [joint["limit"]["lower"], joint["limit"]["upper"]],
                    tolerances["position_atol_m"],
                ),
            )
        dof = int(model.jnt_dofadr[index])
        worst_dynamics = max(
            worst_dynamics,
            _scaled_close(
                model.dof_damping[dof],
                joint["dynamics"]["damping"],
                tolerances["dynamics_atol"],
                tolerances["dynamics_atol"],
            ),
            _scaled_close(
                model.dof_frictionloss[dof],
                joint["dynamics"]["friction"],
                tolerances["dynamics_atol"],
                tolerances["dynamics_atol"],
            ),
        )
    return {"max_range_residual": worst_range, "max_dynamics_residual": worst_dynamics}


def _gate_model_configuration(state: dict) -> dict:
    policy = state["policy"]
    config = _need(state, "config")
    model = _need(state, "robot_model")
    tolerances = policy["tolerances"]
    option = config["simulation"]
    _scaled_close(model.opt.timestep, option["timestep"], tolerances["option_atol"], tolerances["option_atol"])
    _close(model.opt.gravity, option["gravity"], tolerances["option_atol"])
    if int(model.opt.integrator) != policy["integrator_codes"][option["integrator"]]:
        raise CheckError(f"the compiled integrator {int(model.opt.integrator)} differs from the configuration")
    for key, expected in (
        ("nu", 0),
        ("neq", 0),
        ("nsensor", 0),
        ("ntendon", 0),
        ("nkey", 0),
        ("ncam", 0),
        ("nlight", 0),
        ("ntex", 0),
        ("nmat", 0),
    ):
        if int(getattr(model, key)) != expected:
            raise CheckError(f"the robot model carries unexpected {key}={int(getattr(model, key))}")
    for name in ("dof_armature", "body_gravcomp", "jnt_stiffness"):
        if bool(np.any(np.asarray(getattr(model, name)) != 0)):
            raise CheckError(f"the robot model carries non-zero {name} without an authored source")
    expected_sites = {site["name"]: site for site in config["sites"]}
    for derived in _support_expectations(state)["sites"]:
        expected_sites[derived["name"]] = {
            "name": derived["name"],
            "link": derived["link"],
            "xyz": tuple(derived["xyz"]),
            "rotation": derived["rotation"],
        }
    compiled_sites = {model.site(index).name: index for index in range(model.nsite)}
    if sorted(compiled_sites) != sorted(expected_sites):
        raise CheckError("the compiled site set differs from the configuration")
    worst_site = 0.0
    for name, site in expected_sites.items():
        index = compiled_sites[name]
        body = model.body(int(model.site_bodyid[index])).name
        if body != site["link"]:
            raise CheckError(f"site {name!r} sits on {body!r} instead of {site['link']!r}")
        worst_site = max(worst_site, _close(model.site_pos[index], site["xyz"], tolerances["position_atol_m"]))
        rotation = site.get("rotation")
        if rotation is None:
            rotation = _rpy_matrix(site["rpy"])
        worst_site = max(
            worst_site,
            _close(_rotation_from_quat(model.site_quat[index]), rotation, tolerances["rotation_atol"]),
        )
    return {
        "timestep": float(model.opt.timestep),
        "integrator": option["integrator"],
        "sites": len(expected_sites),
        "max_site_residual": worst_site,
    }


def _gate_scene(state: dict) -> dict:
    import mujoco

    policy = state["policy"]
    config = _need(state, "config")
    document = _need(state, "document")
    model = _need(state, "robot_model")
    if config["floor"] is None:
        if "scene_model" in state:
            raise CheckError("a scene appears although the configuration declares no floor")
        return {"floor": False}
    scene = _need(state, "scene_model")
    scene_data = _need(state, "scene_data")
    try:
        scene_document = ET.fromstring(_confined(state["root"], policy["scene_name"]).read_bytes())
    except ET.ParseError as error:
        raise CheckError(f"scene.xml is not well-formed XML: {error}") from error
    if scene_document.tag != "mujoco":
        raise CheckError("scene.xml must have a <mujoco> root")
    includes = scene_document.findall("include")
    if len(includes) != 1 or includes[0].get("file") != policy["robot_name"]:
        raise CheckError(f"scene.xml must include exactly {policy['robot_name']!r}")

    if int(scene.nbody) != int(model.nbody):
        raise CheckError("the scene robot body set differs from robot.xml")
    if int(scene.nq) != int(model.nq) or int(scene.nv) != int(model.nv):
        raise CheckError("the scene robot holds a different number of degrees of freedom than robot.xml")
    scene_joints = {scene.joint(index).name: int(scene.jnt_type[index]) for index in range(scene.njnt)}
    robot_joints = {model.joint(index).name: int(model.jnt_type[index]) for index in range(model.njnt)}
    if scene_joints != robot_joints:
        raise CheckError("the scene joints differ from robot.xml")
    poses = _fk_pose(document, {})
    tolerances = policy["tolerances"]
    worst_position = 0.0
    worst_rotation = 0.0
    for index in range(scene.nbody):
        name = scene.body(index).name
        robot_index = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)
        if robot_index < 0:
            raise CheckError(f"the scene holds a body that robot.xml does not: {name!r}")
        _scaled_close(
            scene.body_mass[index], model.body_mass[robot_index], tolerances["mass_rtol"], tolerances["mass_atol"]
        )
        if name == "world":
            continue
        pose = poses[name]
        position_residual = float(np.max(np.abs(np.asarray(scene_data.xpos[index]) - pose[:3, 3])))
        rotation_residual = float(
            np.max(np.abs(np.asarray(scene_data.xmat[index]).reshape(3, 3) - pose[:3, :3]))
        )
        worst_position = max(worst_position, position_residual)
        worst_rotation = max(worst_rotation, rotation_residual)
        if position_residual > tolerances["position_atol_m"] or rotation_residual > tolerances["rotation_atol"]:
            raise CheckError(
                f"the scene shifts link {name!r} by {position_residual:.18g} m / {rotation_residual:.18g}"
            )
    robot_geoms = sorted(model.geom(index).name for index in range(model.ngeom))
    scene_geoms = sorted(scene.geom(index).name for index in range(scene.ngeom))
    if scene_geoms != sorted([*robot_geoms, policy["floor_geom_name"]]):
        raise CheckError("the scene geom set is not the robot plus the configured floor")
    floor_id = mujoco.mj_name2id(scene, mujoco.mjtObj.mjOBJ_GEOM, policy["floor_geom_name"])
    if floor_id < 0:
        raise CheckError("the scene misses the configured floor")
    floor_height = config["floor"]["height"]
    if config["floor"]["supports"]:
        floor_height = _support_expectations(state)["height"]
    if int(scene.geom_type[floor_id]) != int(mujoco.mjtGeom.mjGEOM_PLANE):
        raise CheckError(f"the floor compiles as {_geom_type_name(mujoco, scene.geom_type[floor_id])}")
    _close(
        np.asarray(scene_data.geom_xpos[floor_id]),
        [0.0, 0.0, floor_height],
        tolerances["position_atol_m"],
    )
    _close(
        np.asarray(scene_data.geom_xmat[floor_id]).reshape(3, 3), np.eye(3), tolerances["rotation_atol"]
    )
    _close(scene.geom_friction[floor_id], config["floor"]["friction"], tolerances["friction_atol"])
    if int(scene.geom_condim[floor_id]) != policy["contact"]["condim"]:
        raise CheckError(f"the floor has condim {int(scene.geom_condim[floor_id])}")
    mask = (int(scene.geom_contype[floor_id]), int(scene.geom_conaffinity[floor_id]))
    if mask != tuple(policy["contact"]["floor"]):
        raise CheckError("the floor carries an unexpected contact mask")
    if int(scene.nlight) != 1:
        raise CheckError(f"the scene must hold exactly one light, found {int(scene.nlight)}")
    _scaled_close(
        scene.opt.timestep, config["simulation"]["timestep"], tolerances["option_atol"], tolerances["option_atol"]
    )
    _close(scene.opt.gravity, config["simulation"]["gravity"], tolerances["option_atol"])
    if int(scene.opt.integrator) != policy["integrator_codes"][config["simulation"]["integrator"]]:
        raise CheckError("the scene integrator differs from the configuration")
    return {
        "floor": True,
        "height": floor_height,
        "max_position_residual_m": worst_position,
        "max_rotation_residual": worst_rotation,
    }


def _runtime_of(model, steps: int) -> dict:
    import mujoco

    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    forward_finite = bool(
        np.isfinite(data.qpos).all() and np.isfinite(data.qvel).all() and np.isfinite(data.xpos).all()
    )
    steps_finite = True
    for _ in range(steps):
        mujoco.mj_step(model, data)
        if not (np.isfinite(data.qpos).all() and np.isfinite(data.qvel).all()):
            steps_finite = False
            break
    return {"forward_finite": forward_finite, "steps": steps, "steps_finite": steps_finite}


def _gate_runtime(state: dict) -> dict:
    policy = state["policy"]
    model = _need(state, "robot_model")
    runtime = _runtime_of(model, policy["runtime_steps"])
    if not runtime["forward_finite"] or not runtime["steps_finite"]:
        raise CheckError("the zero state produced non-finite values")
    details = {"steps": runtime["steps"], "robot_finite": True}
    if "scene_model" in state:
        scene_runtime = _runtime_of(state["scene_model"], policy["runtime_steps"])
        if not scene_runtime["forward_finite"] or not scene_runtime["steps_finite"]:
            raise CheckError("the scene zero state produced non-finite values")
        details["scene_finite"] = True
    return details


def _facts(state: dict) -> dict:
    facts: dict = {}
    document = state.get("document")
    config = state.get("config")
    policy = state["policy"]
    if document is not None:
        facts["robot"] = document["name"]
        facts["links"] = len(document["links"])
        facts["joints"] = len(document["joints"])
        facts["visuals"] = sum(len(link["visuals"]) for link in document["links"])
        facts["effort_velocity"] = [
            {
                "joint": joint["name"],
                "effort": None if joint["limit"] is None else joint["limit"]["effort"],
                "velocity": None if joint["limit"] is None else joint["limit"]["velocity"],
            }
            for joint in document["joints"]
        ]
    if config is not None:
        facts["base"] = config["base"]
        facts["collision_source"] = config["collision"]["source"]
        facts["sites"] = len(config["sites"])
        facts["support_sites"] = len(config["support_sites"])
        facts["floor"] = config["floor"] is not None
        if document is not None:
            facts["collision_geoms"] = sum(
                1 for item in _expected_geoms(document, config, policy) if item["role"] == "collision"
            )
    return facts


_GATES_BY_ID = {
    "identity.closure": _gate_identity_closure,
    "engine.load": _gate_engine,
    "topology.entities": _gate_topology_entities,
    "topology.dof": _gate_topology_dof,
    "kinematics.fk": _gate_kinematics_fk,
    "kinematics.axes": _gate_kinematics_axes,
    "inertia.links": _gate_inertia_links,
    "inertia.total": _gate_inertia_total,
    "geometry.shapes": _gate_geometry_shapes,
    "geometry.instances": _gate_geometry_instances,
    "joints.config": _gate_joints_config,
    "model.configuration": _gate_model_configuration,
    "scene.scene": _gate_scene,
    "runtime.steps": _gate_runtime,
}


# --------------------------------------------------------------------------
# The isolated child process
# --------------------------------------------------------------------------


def _child_report(request: dict) -> dict:
    policy = request["policy"]
    state = {"root": Path(request["root"]).resolve(), "policy": policy}
    checks = []
    for identifier in policy["check_ids"]:
        function = _GATES_BY_ID.get(identifier)
        if function is None:
            raise CheckError(f"the checker has no implementation for {identifier!r}")
        try:
            details = function(state) or {}
            checks.append({"id": identifier, "state": "passed", "passed": True, "details": details})
        except Exception as error:  # noqa: BLE001 (every gate reports its own failure)
            checks.append(
                {
                    "id": identifier,
                    "state": "failed",
                    "passed": False,
                    "details": {"error": f"{type(error).__name__}: {error}"},
                }
            )
    return {
        "schema_version": policy["report_schema"],
        "scope": policy["report_scope"],
        "manifest_sha256": state.get("manifest_sha256"),
        "facts": _facts(state),
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
    }


def _child_main() -> int:
    try:
        request = json.load(sys.stdin)
        report = _child_report(request)
    except Exception as error:  # noqa: BLE001 (the parent renders the message)
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        return 1
    try:
        print(json.dumps(report, allow_nan=False))
    except ValueError as error:
        print(json.dumps({"error": f"the checker produced non-finite values: {error}"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_child_main())


# --------------------------------------------------------------------------
# The parent process
# --------------------------------------------------------------------------


def _policy() -> dict:
    from .. import mjcf_contract as contract

    return {
        "check_ids": list(contract.CHECK_IDS),
        "report_schema": contract.REPORT_SCHEMA,
        "report_scope": contract.REPORT_SCOPE,
        "manifest_schema": contract.MANIFEST_SCHEMA,
        "input_root": contract.INPUT_ROOT,
        "config_name": contract.CONFIG_NAME,
        "robot_name": contract.ROBOT_NAME,
        "scene_name": contract.SCENE_NAME,
        "manifest_name": contract.MANIFEST_NAME,
        "report_path": contract.REPORT_PATH,
        "floor_geom_name": contract.FLOOR_GEOM_NAME,
        "floor_height_supports": contract.FLOOR_HEIGHT_SUPPORTS,
        "base_modes": list(contract.BASE_MODES),
        "collision_sources": list(contract.COLLISION_SOURCES),
        "integrators": list(contract.INTEGRATORS),
        "integrator_codes": dict(contract.INTEGRATOR_CODES),
        "names": {
            "visual": contract.VISUAL_GEOM_TEMPLATE,
            "collision": contract.COLLISION_GEOM_TEMPLATE,
            "convex": contract.CONVEX_GEOM_TEMPLATE,
        },
        "xml_syntax": {
            project: {
                "attributes": {
                    tag: list(names) for tag, names in getattr(contract, f"{project.upper()}_XML_SYNTAX").items()
                },
                "structure": {
                    tag: list(names) for tag, names in getattr(contract, f"{project.upper()}_XML_STRUCTURE").items()
                },
                "numeric": list(getattr(contract, f"{project.upper()}_XML_NUMERIC")),
            }
            for project in ("robot", "scene")
        },
        "roles": dict(contract.ROLE_GROUPS),
        "contact": {
            "robot": list(contract.ROBOT_CONTACT),
            "solo": list(contract.ROBOT_SOLO_CONTACT),
            "floor": list(contract.FLOOR_CONTACT),
            "condim": contract.CONTACT_CONDIM,
        },
        "tolerances": {
            "position_atol_m": contract.POSITION_ATOL_M,
            "rotation_atol": contract.ROTATION_ATOL,
            "mass_rtol": contract.MASS_REL_TOL,
            "mass_atol": contract.MASS_ATOL,
            "inertia_rtol": contract.INERTIA_RTOL,
            "inertia_atol": contract.INERTIA_ATOL,
            "inertia_triangle_rtol": contract.INERTIA_TRIANGLE_RTOL,
            "mesh_atol_m": contract.MESH_ATOL_M,
            "mesh_rtol": contract.MESH_RTOL,
            "axis_unit_atol": contract.AXIS_UNIT_ATOL,
            "rgba_atol": contract.RGBA_ATOL,
            "friction_atol": contract.FRICTION_ATOL,
            "dynamics_atol": contract.DYNAMICS_ATOL,
            "qpos0_atol": contract.QPOS0_ATOL,
            "option_atol": contract.OPTION_ATOL,
            "support_plane_atol_m": contract.SUPPORT_PLANE_ATOL_M,
            "support_plane_rtol": contract.SUPPORT_PLANE_RTOL,
            "support_merge_angle_atol": contract.SUPPORT_MERGE_ANGLE_ATOL,
            "support_merge_offset_atol_m": contract.SUPPORT_MERGE_OFFSET_ATOL_M,
            "support_area_rtol": contract.SUPPORT_AREA_RTOL,
            "support_normal_z_atol": contract.SUPPORT_NORMAL_Z_ATOL,
            "support_xaxis_parallel_atol": contract.SUPPORT_XAXIS_PARALLEL_ATOL,
            "support_triangle_eps": contract.SUPPORT_TRIANGLE_EPS,
            "support_hull_eps": contract.SUPPORT_HULL_EPS,
        },
        "fk_fractions": list(contract.FK_FRACTIONS),
        "continuous_sample_rad": contract.CONTINUOUS_SAMPLE_RAD,
        "runtime_steps": contract.RUNTIME_STEPS,
        "engine": contract.engine_identity(),
        "tool": contract.tool_identity(),
    }


def _run_child(root: Path) -> dict:
    from .. import mjcf_contract as contract

    command = [sys.executable, "-I", str(Path(__file__).resolve())]
    request = json.dumps({"root": str(root), "policy": _policy()})
    try:
        completed = subprocess.run(
            command,
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise CheckError(f"the checker process timed out after {TIMEOUT_SECONDS} seconds") from error
    if not completed.stdout.strip():
        raise CheckError(f"the checker process produced no report: {completed.stderr.strip()[-2000:]}")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as error:
        raise CheckError(f"the checker process returned invalid JSON: {completed.stdout[:400]!r}") from error
    if isinstance(payload, dict) and "error" in payload and "schema_version" not in payload:
        raise CheckError(f"the checker process failed: {payload['error']}")
    if not isinstance(payload, dict) or payload.get("schema_version") != contract.REPORT_SCHEMA:
        raise CheckError("the checker process returned an unexpected report schema")
    checks = payload.get("checks")
    if not isinstance(checks, list) or [check.get("id") for check in checks] != list(contract.CHECK_IDS):
        raise CheckError("the checker process did not report every gate exactly once")
    for check in checks:
        if (
            check.get("state") not in ("passed", "failed")
            or not isinstance(check.get("passed"), bool)
            or not isinstance(check.get("details"), dict)
        ):
            raise CheckError("the checker process returned a malformed gate record")
    if not isinstance(payload.get("facts"), dict):
        raise CheckError("the checker process returned malformed facts")
    payload["passed"] = all(check["passed"] for check in checks)
    return payload


def _broken_report(message: str) -> dict:
    from .. import mjcf_contract as contract

    return {
        "schema_version": contract.REPORT_SCHEMA,
        "scope": contract.REPORT_SCOPE,
        "manifest_sha256": None,
        "facts": {},
        "checks": [{"id": "engine.process", "state": "failed", "passed": False, "details": {"error": message}}],
        "passed": False,
    }


def evaluate_bundle(root: Path) -> dict:
    """Run the bounded isolated checker and return its recomputed report."""

    try:
        return _run_child(Path(root))
    except CheckError as error:
        return _broken_report(str(error))


def check_bundle(root: Path) -> dict:
    """Evaluate the delivery, require an unchanged body and an exactly bound saved report."""

    from ..io import PipelineError, confined, inventory, read_data

    root = Path(root)
    inventory_error = None
    try:
        before = inventory(root)
    except (PipelineError, OSError) as error:
        before, inventory_error = None, str(error)
    report = evaluate_bundle(root)
    if inventory_error is not None:
        report["checks"].append(
            {
                "id": "identity.stability",
                "state": "failed",
                "passed": False,
                "details": {"error": f"the delivery cannot be inventoried: {inventory_error}"},
            }
        )
        report["passed"] = False
    else:
        try:
            if inventory(root) != before:
                raise CheckError("the delivery changed while it was checked")
        except (PipelineError, OSError) as error:
            report["checks"].append(
                {"id": "identity.stability", "state": "failed", "passed": False, "details": {"error": str(error)}}
            )
            report["passed"] = False
    try:
        saved = read_data(confined(root, "reports/quality.json"))
        if saved != json.loads(json.dumps(report)):
            raise CheckError("the saved quality report differs from the recomputation")
    except (OSError, ValueError) as error:
        report["checks"].append(
            {"id": "report.binding", "state": "failed", "passed": False, "details": {"error": str(error)}}
        )
        report["passed"] = False
    return report
