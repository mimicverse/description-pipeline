"""Independent checks of a projected URDF-to-MJCF delivery.

Every expectation is re-derived from the frozen URDF, meshes and configuration
inside one bounded isolated child process (``python -I``), which loads the
actual ``robot.xml`` (and the optional ``scene.xml``) with the locked MuJoCo and
compares against ``MjModel``/``MjData``/NumPy arrays in place.  No engine facts
are serialized out of that process; it returns only the compact deterministic
report.  The parent process invents nothing: it bounds the child, checks the
bound files after execution and returns the report for the stage-owned
certificate. Input and output identities come from the canonical stage boundary.  The converter module is never
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


def _read_binding(data: dict, policy: dict) -> dict:
    if not isinstance(data, dict) or data.get("schema_version") != policy["input_binding_schema"]:
        _fail("manifest", f"schema_version must be {policy['input_binding_schema']!r}")
    if set(data) != {"schema_version", "source", "files", "tool", "engine"}:
        _fail("manifest", "unexpected key set")
    source = data["source"]
    if not isinstance(source, dict) or set(source) != {"urdf", "meshes", "components"}:
        _fail("manifest", "source must hold 'urdf' and 'meshes'")
    if not isinstance(source["urdf"], str) or not source["urdf"]:
        _fail("manifest", "source.urdf must be a relative path")
    if not isinstance(source["meshes"], list) or any(not isinstance(item, str) for item in source["meshes"]):
        _fail("manifest", "source.meshes must be a list of relative paths")
    if source["components"] not in (None, policy["component_source"]):
        _fail("input binding", "source.components names an unsupported provenance file")
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


def _default(mapping: dict, key: str, fallback, where: str):
    """Omitted fields fall back to the policy default; an explicit null never does."""

    if key not in mapping:
        return fallback
    value = mapping[key]
    if value is None:
        _fail("simulation", f"{where}.{key} cannot be null; omit it to use the default policy")
    return value


def _read_simulation(content: bytes, policy: dict) -> dict:
    defaults = policy["defaults"]
    data = _parse_json(content, policy["config_name"])
    if not isinstance(data, dict):
        _fail("simulation", "the configuration must be an object")
    unknown = sorted(
        set(data) - {"schema_version", "base", "collision", "simulation", "sites", "support_sites", "floor"}
    )
    if unknown:
        _fail("simulation", f"unknown key {unknown[0]!r}")
    version = _default(data, "schema_version", defaults["schema_version"], "root")
    if isinstance(version, bool) or version != defaults["schema_version"]:
        _fail("simulation", f"schema_version must be {defaults['schema_version']}")
    base = _default(data, "base", defaults["base"], "root")
    if base not in policy["base_modes"]:
        _fail("simulation", f"base must be one of {policy['base_modes']}")
    collision = _default(data, "collision", {}, "root")
    if not isinstance(collision, dict):
        _fail("simulation", "collision must be an object")
    unknown = sorted(set(collision) - {"source", "self_collision", "friction", "max_vertices", "max_error_m"})
    if unknown:
        _fail("simulation", f"unknown key {unknown[0]!r} at collision")
    source = _default(collision, "source", defaults["collision"]["source"], "collision")
    if source not in policy["collision_sources"]:
        _fail("simulation", f"collision.source must be one of {policy['collision_sources']}")
    self_collision = _default(collision, "self_collision", defaults["collision"]["self_collision"], "collision")
    if not isinstance(self_collision, bool):
        _fail("simulation", "collision.self_collision must be true or false")
    contact_friction = _friction(
        _default(collision, "friction", defaults["collision"]["friction"], "collision"), "collision.friction"
    )
    max_vertices = _default(collision, "max_vertices", defaults["collision"]["max_vertices"], "collision")
    if type(max_vertices) is not int or not 4 <= max_vertices <= 4096:
        _fail("simulation", "collision.max_vertices must be an integer in [4, 4096]")
    max_error = _number(_default(collision, "max_error_m", defaults["collision"]["max_error_m"], "collision"),
                        "collision.max_error_m")
    if max_error <= 0:
        _fail("simulation", "collision.max_error_m must be positive")
    simulation = _default(data, "simulation", {}, "root")
    if not isinstance(simulation, dict):
        _fail("simulation", "simulation must be an object")
    unknown = sorted(set(simulation) - {"timestep", "gravity", "integrator"})
    if unknown:
        _fail("simulation", f"unknown key {unknown[0]!r} at simulation")
    timestep = _number(
        _default(simulation, "timestep", defaults["simulation"]["timestep"], "simulation"), "simulation.timestep"
    )
    if timestep <= 0:
        _fail("simulation", "simulation.timestep must be positive")
    gravity = _triple(
        _default(simulation, "gravity", defaults["simulation"]["gravity"], "simulation"), "simulation.gravity"
    )
    integrator = _default(simulation, "integrator", defaults["simulation"]["integrator"], "simulation")
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
                    "xyz": _triple(_default(raw, "xyz", (0.0, 0.0, 0.0), where), f"{where}.xyz"),
                    "rpy": _triple(_default(raw, "rpy", (0.0, 0.0, 0.0), where), f"{where}.rpy"),
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
            link = raw.get("link")
            if not isinstance(link, str) or not link:
                _fail("simulation", f"{where} needs a link")
            name = _default(raw, "name", policy["names"]["support"].format(link=link), where)
            if not isinstance(name, str) or not name:
                _fail("simulation", f"{where}.name must be a non-empty string")
            if name in site_names:
                _fail("simulation", f"site name {name!r} is used by more than one site")
            site_names.add(name)
            support_sites.append({"name": name, "link": link})
    if support_sites and source == "none":
        _fail("simulation", "support_sites require collision.source urdf or visual_convex")
    floor = None
    if "floor" in data:
        raw = data["floor"]
        if raw is None:
            _fail("simulation", "floor cannot be null; omit it to deliver the robot without a scene")
        if not isinstance(raw, dict):
            _fail("simulation", "floor must be an object")
        unknown = sorted(set(raw) - {"height", "friction"})
        if unknown:
            _fail("simulation", f"unknown key {unknown[0]!r} at floor")
        default_height = policy["floor_height_supports"] if support_sites else defaults["floor"]["height"]
        raw_height = _default(raw, "height", default_height, "floor")
        if raw_height == policy["floor_height_supports"]:
            if not support_sites:
                _fail("simulation", f"floor.height {raw_height!r} requires support_sites")
            height, supports = None, True
        else:
            height, supports = _number(raw_height, "floor.height"), False
        floor = {
            "height": height,
            "supports": supports,
            "friction": _friction(_default(raw, "friction", contact_friction, "floor"), "floor.friction"),
        }
    return {
        "base": base,
        "collision": {
            "source": source,
            "max_vertices": max_vertices,
            "max_error_m": max_error,
            "self_collision": self_collision,
            "friction": contact_friction,
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
    surface = {"name": element.get("name"), "origin": _origin_of(element, where),
               "geometry": _geometry_of(element, where)}
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


def _expected_collisions(state: dict) -> set[str]:
    """Rebuild grouping from the bound provenance, never from emitted proxies."""
    component_path = state["manifest"]["source"]["components"]
    lookup = {}
    if component_path is not None:
        model = _parse_json(_confined(state["root"], component_path).read_bytes(), component_path)
        if not isinstance(model, dict) or not isinstance(model.get("links"), list):
            raise CheckError("component provenance has no link list")
        for link in model["links"]:
            for visual in link.get("visuals", []):
                name, component = visual.get("name"), visual.get("provenance", {}).get("native_component")
                if not isinstance(name, str) or not isinstance(component, str) or not component:
                    raise CheckError("native visual component provenance is missing")
                identity = (link["name"], name)
                if identity in lookup and lookup[identity] != component:
                    raise CheckError("native visual identity conflicts across component records")
                lookup[identity] = component
    proxies = set()
    for link in state["document"]["links"]:
        source = state["config"]["collision"]["source"]
        if source == "auto":
            source = "urdf" if link["collisions"] else "visual_convex"
        projected = []
        if source == "urdf":
            projected = [{**surface, "source_surfaces": [surface]} for surface in link["collisions"]]
        elif source == "visual_convex":
            groups = {}
            for index, visual in enumerate(link["visuals"]):
                component = f"authored-visual:{index}"
                if component_path is not None:
                    component = lookup.get((link["name"], visual["name"]))
                    if component is None:
                        raise CheckError(f"visual {visual['name']!r} has no native component binding")
                groups.setdefault(component, []).append(visual)
            for component, surfaces in groups.items():
                if len(surfaces) == 1:
                    projected.append({**surfaces[0], "source_surfaces": surfaces})
                    continue
                if any(surface["geometry"]["kind"] != "mesh" for surface in surfaces):
                    raise CheckError("native component aggregation requires mesh surfaces")
                path = "mjcf/collision/" + _canonical_digest({"link": link["name"], "component": component}) + ".obj"
                proxies.add(path)
                projected.append({"origin": {"xyz": (0., 0., 0.), "rpy": (0., 0., 0.)},
                                  "geometry": {"kind": "mesh", "filename": "../" + path, "scale": (1., 1., 1.)},
                                  "source_surfaces": surfaces})
        link["projection_collisions"] = projected
        link["collision_role"] = source
    return proxies


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
        source = link["collision_role"]
        family = "collision" if source == "urdf" else "convex"
        for index, surface in enumerate(link["projection_collisions"]):
            result.append({"name": policy["names"][family].format(link=link["name"], index=index),
                           "role": "collision", "surface": surface, "link": link["name"],
                           "source_surfaces": surface["source_surfaces"]})
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
            surfaces = (link["collisions"] if source == "urdf" or (source == "auto" and link["collisions"])
                    else link["visuals"])
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
    manifest = _read_binding(state["input_binding"], policy)
    state["manifest"] = manifest
    state["input_binding_sha256"] = _canonical_digest(manifest)
    if manifest["tool"] != policy["tool"]:
        raise CheckError("the manifest names a different tool revision; rebuild the delivery")
    if manifest["engine"] != policy["engine"]:
        raise CheckError("the manifest names a different engine revision; rebuild the delivery")

    actual = {path for path in _inventory(root)
              if path.startswith(("urdf/", "meshes/", "mjcf/")) or path == policy["component_source"]}
    allowed = required = set(manifest["files"])
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
    urdf_input = urdf_relative
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
    expected_inputs = {urdf_relative}
    expected_inputs.update(mesh_relative)
    if expected_inputs != {key for key in manifest["files"] if key.startswith(("urdf/", "meshes/"))}:
        raise CheckError("the sealed input closure differs from the references of the frozen URDF")
    if sorted(manifest["source"]["meshes"]) != mesh_relative:
        raise CheckError("the manifest source list differs from the references of the frozen URDF")
    components = manifest["source"]["components"]
    proxies = _expected_collisions(state)
    expected_files = set(expected_inputs) | {policy["config_name"], policy["robot_name"]} | proxies
    if components is not None:
        expected_files.add(components)
    if config["floor"] is not None:
        expected_files.add(policy["scene_name"])
    if set(manifest["files"]) != expected_files:
        raise CheckError("the manifest file list is not the projected delivery file set")

    meshes = {}
    for path in mesh_relative:
        meshes[path] = _read_binary_stl(
            _confined(root, path).read_bytes(), path
        )
    state["meshes"] = meshes
    requests, singles = _fk_samples(document, policy)
    state["fk_requests"] = requests
    state["fk_singles"] = singles
    state["closure_ok"] = True
    return {
        "input_binding_sha256": state["input_binding_sha256"],
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
    sealed = {key for key in manifest["files"] if key.startswith(("meshes/", "mjcf/collision/"))}
    robot_xml = _xml_document(_confined(state["root"], policy["robot_name"]).read_bytes(), policy["robot_name"])
    _checked_syntax(robot_xml, policy, "robot", policy["robot_name"])
    _checked_asset_paths(robot_xml, policy["robot_name"], policy, sealed, state["root"])
    _robot_assets(state)
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
        if len(includes) != 1 or includes[0].get("file") != Path(policy["robot_name"]).name:
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
            if mesh.get("inertia") != "shell":
                raise CheckError(f"mesh asset {name!r} lacks the declared shell preprocessing policy")
            if mesh.get("maxhullvert") != str(state["config"]["collision"]["max_vertices"]):
                raise CheckError(f"mesh asset {name!r} differs from the collision vertex budget")
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
        parts: list[str] = policy["robot_name"].split("/")[:-1]
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
        asset_path = _confined(root, canonical)
        if canonical.startswith("mjcf/collision/"):
            _checked_proxy_obj(asset_path.read_bytes(), canonical)


def _checked_proxy_obj(content: bytes, label: str) -> None:
    """Reject resource directives before the engine can interpret an OBJ file."""
    vertices, faces = 0, []
    for line in content.decode("ascii").splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if fields[0] == "v" and len(fields) == 4:
            values = [float(value) for value in fields[1:]]
            if not all(math.isfinite(value) for value in values):
                raise CheckError(f"{label}: non-finite proxy vertex")
            vertices += 1
        elif fields[0] == "f" and len(fields) == 4:
            faces.append(tuple(int(value) for value in fields[1:]))
        else:
            raise CheckError(f"{label}: unsupported OBJ directive")
    if vertices < 4 or not faces or any(index < 1 or index > vertices for face in faces for index in face):
        raise CheckError(f"{label}: empty or invalid proxy topology")


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
    expected_body = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, item["link"])
    actual_body = int(model.geom_bodyid[index])
    if expected_body < 0 or actual_body != expected_body:
        raise CheckError(
            f"the geom belongs to body {model.body(actual_body).name!r} instead of {item['link']!r}"
        )
    surface = item["surface"]
    origin = np.eye(4)
    origin[:3, :3] = _rpy_matrix(surface["origin"]["rpy"])
    origin[:3, 3] = surface["origin"]["xyz"]
    world = poses[item["link"]] @ origin
    kind = surface["geometry"]["kind"]
    expected_color = surface.get("color") or (0.5, 0.5, 0.5, 1.0)
    _close(model.geom_rgba[index], expected_color, policy["tolerances"]["rgba_atol"])
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
        parts: list[str] = policy["robot_name"].split("/")[:-1]
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
        expected_file = reference
        if canonical != expected_file:
            raise CheckError(f"references {canonical!r} instead of {expected_file!r}")
        if asset["scale"] != tuple(surface["geometry"]["scale"]):
            raise CheckError(f"declares scale {asset['scale']} instead of {surface['geometry']['scale']}")
        start = int(model.mesh_vertadr[mesh_id])
        count = int(model.mesh_vertnum[mesh_id])
        stored = np.asarray(model.mesh_vert[start : start + count], dtype=float)
        rotation = np.asarray(data.geom_xmat[index]).reshape(3, 3)
        engine_world = stored @ rotation.T + np.asarray(data.geom_xpos[index])
        if item["role"] == "collision":
            return worst_mesh
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


def _gate_joint_restoring(state: dict) -> dict:
    """Exercise actual limit constraints independently of contact and gravity."""
    import mujoco

    model, document = _need(state, "robot_model"), _need(state, "document")
    gravity, flags = model.opt.gravity.copy(), int(model.opt.disableflags)
    rows = []
    try:
        model.opt.gravity[:] = 0
        model.opt.disableflags |= int(mujoco.mjtDisableBit.mjDSBL_CONTACT)
        neutral = model.qpos0.copy()
        for joint in document["joints"]:
            if joint["type"] not in ("revolute", "prismatic"):
                continue
            index = int(model.joint(joint["name"]).id)
            bounds = joint["limit"]
            neutral[int(model.jnt_qposadr[index])] = (bounds["lower"] + bounds["upper"]) / 2
        for joint in document["joints"]:
            if joint["type"] not in ("revolute", "prismatic"):
                continue
            index = int(model.joint(joint["name"]).id)
            address, dof = int(model.jnt_qposadr[index]), int(model.jnt_dofadr[index])
            forces = []
            for field, sign in (("lower", 1), ("upper", -1)):
                data = mujoco.MjData(model)
                data.qpos[:] = neutral
                data.qpos[address] = joint["limit"][field] - sign * state["policy"]["limit_probe_offset"]
                mujoco.mj_forward(model, data)
                force = float(data.qfrc_constraint[dof])
                if not math.isfinite(force) or sign * force <= 0:
                    raise CheckError(f"joint {joint['name']!r} has no restoring {field} limit force")
                forces.append(force)
            rows.append({"joint": joint["name"], "lower_force": forces[0], "upper_force": forces[1]})
    finally:
        model.opt.gravity[:] = gravity
        model.opt.disableflags = flags
    return {"joints": rows, "offset": state["policy"]["limit_probe_offset"],
            "scope": "Isolated numerical limit response; no hardware or full-travel qualification"}


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
    if len(includes) != 1 or includes[0].get("file") != Path(policy["robot_name"]).name:
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


def _gate_support_contacts(state: dict) -> dict:
    """Probe declared lowest supports with an equivalent upward floor displacement."""
    import mujoco

    config = _need(state, "config")
    if config["floor"] is None or not config["floor"]["supports"]:
        return {"applicable": False, "reason": "No source-derived support floor was requested"}
    model = _need(state, "scene_model")
    policy, document = state["policy"], _need(state, "document")
    poses = _fk_pose(document, {})
    directory = "/".join(state["urdf_relative"].split("/")[:-1])
    floor_height = _support_expectations(state)["height"]
    tolerance = policy["tolerances"]["support_plane_atol_m"]
    expected = set()
    support_links = {item["link"] for item in config["support_sites"]}
    for link in document["links"]:
        if link["name"] not in support_links:
            continue
        surfaces = (link["collisions"] if link["collision_role"] == "urdf" else link["visuals"])
        minimum = min(_support_minimum_height(_support_geometry(
            surface, poses[link["name"]], state["meshes"], directory, link["name"]
        )) for surface in surfaces)
        if abs(minimum - floor_height) <= tolerance:
            expected.add(link["name"])
    floor_id = int(model.geom(policy["floor_geom_name"]).id)
    depth = config["collision"]["max_error_m"] + policy["contact_probe_depth_m"] + tolerance
    observed, contacts = set(), []
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    # World geoms are compiled as static: editing model.geom_pos after compile
    # does not move their runtime pose. Probe a private data pose explicitly.
    data.geom_xpos[floor_id, 2] += depth
    mujoco.mj_collision(model, data)
    _close(data.geom_xpos[floor_id], [0., 0., floor_height + depth],
           policy["tolerances"]["position_atol_m"])
    for contact in data.contact:
        pair = (int(contact.geom1), int(contact.geom2))
        if floor_id not in pair or float(contact.dist) >= 0:
            continue
        geom = pair[1] if pair[0] == floor_id else pair[0]
        body = model.body(int(model.geom_bodyid[geom])).name
        observed.add(body)
        contacts.append({"geom": model.geom(geom).name, "link": body, "distance_m": float(contact.dist)})
    if not expected or not expected <= observed:
        raise CheckError(f"declared lowest supports have no ground response: {sorted(expected - observed)}")
    return {"applicable": True, "expected_support_links": sorted(expected),
            "observed_links": sorted(observed), "contacts": contacts, "floor_displacement_m": depth,
            "probe_floor_height_m": float(data.geom_xpos[floor_id, 2]),
            "scope": "Local contact probe at the source-derived support floor; no standing qualification"}


def _runtime_of(model, steps: int, time_tolerance: float) -> dict:
    import mujoco

    warnings: list[str] = []
    data = mujoco.MjData(model)
    mujoco.set_mju_user_warning(lambda text: warnings.append(text) or 0)
    try:
        mujoco.mj_forward(model, data)
        finite = bool(
            np.isfinite(data.qpos).all()
            and np.isfinite(data.qvel).all()
            and np.isfinite(data.qacc).all()
            and np.isfinite(data.xpos).all()
        )
        for _ in range(steps):
            mujoco.mj_step(model, data)
            # An implicit instability reset restores finite state and would fake
            # a passing run; the engine warning is the reliable signal.
            if warnings:
                finite = False
                break
            if not (
                np.isfinite(data.qpos).all() and np.isfinite(data.qvel).all() and np.isfinite(data.qacc).all()
            ):
                finite = False
                break
        counters = [int(entry.number) for entry in data.warning]
        expected = steps * float(model.opt.timestep)
        advanced = abs(float(data.time) - expected) <= time_tolerance * max(1.0, abs(expected))
        return {
            "steps": steps,
            "finite": finite,
            "time": float(data.time),
            "time_advanced": advanced,
            "warnings": warnings[:3],
            "warning_counts": counters,
        }
    finally:
        mujoco.set_mju_user_warning(None)


def _gate_runtime(state: dict) -> dict:
    policy = state["policy"]
    model = _need(state, "robot_model")
    runtime = _runtime_of(model, policy["runtime_steps"], policy["tolerances"]["option_atol"])
    if runtime["warnings"]:
        raise CheckError(f"the engine reported an unstable runtime: {runtime['warnings'][0]}")
    if any(runtime["warning_counts"]):
        raise CheckError(f"the engine counted instability warnings: {runtime['warning_counts']}")
    if not runtime["finite"]:
        raise CheckError("the zero state produced non-finite values")
    if not runtime["time_advanced"]:
        raise CheckError("the simulation time did not advance as expected; the state was implicitly reset")
    details = {"steps": runtime["steps"], "time": runtime["time"], "robot_finite": True}
    if "scene_model" in state:
        scene_runtime = _runtime_of(state["scene_model"], policy["runtime_steps"], policy["tolerances"]["option_atol"])
        if scene_runtime["warnings"]:
            raise CheckError(f"the scene reported an unstable runtime: {scene_runtime['warnings'][0]}")
        if any(scene_runtime["warning_counts"]):
            raise CheckError(f"the scene counted instability warnings: {scene_runtime['warning_counts']}")
        if not scene_runtime["finite"]:
            raise CheckError("the scene zero state produced non-finite values")
        if not scene_runtime["time_advanced"]:
            raise CheckError("the scene simulation time did not advance as expected")
        details["scene_finite"] = True
    return details


def _effective_projection(config: dict, policy: dict) -> dict:
    """The resolved options in the same shape as an external configuration."""

    projection = {
        "schema_version": policy["defaults"]["schema_version"],
        "base": config["base"],
        "collision": {
            "source": config["collision"]["source"],
            "max_vertices": config["collision"]["max_vertices"],
            "max_error_m": config["collision"]["max_error_m"],
            "self_collision": config["collision"]["self_collision"],
            "friction": list(config["collision"]["friction"]),
        },
        "simulation": {
            "timestep": config["simulation"]["timestep"],
            "gravity": list(config["simulation"]["gravity"]),
            "integrator": config["simulation"]["integrator"],
        },
        "sites": [
            {"name": site["name"], "link": site["link"], "xyz": list(site["xyz"]), "rpy": list(site["rpy"])}
            for site in config["sites"]
        ],
        "support_sites": [{"name": site["name"], "link": site["link"]} for site in config["support_sites"]],
    }
    if config["floor"] is not None:
        height = policy["floor_height_supports"] if config["floor"]["supports"] else config["floor"]["height"]
        projection["floor"] = {"height": height, "friction": list(config["floor"]["friction"])}
    return projection


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
        facts["effective_simulation"] = _effective_projection(config, policy)
        if document is not None and all("projection_collisions" in link for link in document["links"]):
            facts["collision_geoms"] = sum(
                1 for item in _expected_geoms(document, config, policy) if item["role"] == "collision"
            )
    return facts



def _compiled_hull(model, mesh_id: int) -> tuple[np.ndarray, np.ndarray]:
    """Read the actual convex graph, preserving the compiler's mesh-frame transform."""
    address = int(model.mesh_graphadr[mesh_id])
    if address < 0:
        raise CheckError("a collision mesh has no compiled convex graph")
    count, faces = map(int, model.mesh_graph[address:address + 2])
    start = address + 2 + 3 * count + 3 * faces
    indices = np.asarray(model.mesh_graph[start:start + 3 * faces], dtype=int).reshape(-1, 3)
    begin = int(model.mesh_vertadr[mesh_id])
    end = begin + int(model.mesh_vertnum[mesh_id])
    points = np.asarray(model.mesh_vert[begin:end], dtype=float)
    points = points @ _rotation_from_quat(model.mesh_quat[mesh_id]).T + model.mesh_pos[mesh_id]
    if not np.isfinite(points).all() or len(indices) == 0:
        raise CheckError("a collision hull is empty or non-finite")
    return points, indices


def _directed_convex_distance(points: np.ndarray, vertices: np.ndarray, faces: np.ndarray) -> float:
    """Enumerate convex vertices against the other solid's complete triangular boundary.

    Distance to a convex set is convex; its maximum over a polytope is attained
    at a vertex. Interior points have zero solid distance. This does not bound
    the difference from the original concave CAD solid.
    """
    triangles = vertices[faces]
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    ab, ac = b - a, c - a
    normal = np.cross(ab, ac)
    lengths = np.linalg.norm(normal, axis=1)
    if not np.isfinite(lengths).all() or bool(np.any(lengths <= 0)):
        raise CheckError("a collision hull has degenerate triangular faces")
    normal = normal / lengths[:, None]
    center = vertices[np.unique(faces)].mean(axis=0)
    normal = np.where(((center - a) * normal).sum(axis=1)[:, None] > 0, -normal, normal)
    d00, d01, d11 = (ab * ab).sum(axis=1), (ab * ac).sum(axis=1), (ac * ac).sum(axis=1)
    denominator = lengths * lengths
    if bool(np.any(denominator <= 0)):
        raise CheckError("a collision hull has numerically singular faces")
    worst = 0.0
    for offset in range(0, len(points), 32):
        sample = points[offset:offset + 32]
        ap = sample[:, None, :] - a
        plane = (ap * normal).sum(axis=2)
        interior = (plane <= 0).all(axis=1)
        d20, d21 = (ap * ab).sum(axis=2), (ap * ac).sum(axis=2)
        u = (d11 * d20 - d01 * d21) / denominator
        v = (d00 * d21 - d01 * d20) / denominator
        projected = (u >= 0) & (v >= 0) & (u + v <= 1)
        distances = np.where(projected, plane * plane, np.inf)
        for left, right in ((a, b), (b, c), (c, a)):
            edge = right - left
            vector = sample[:, None, :] - left
            t = np.clip((vector * edge).sum(axis=2) / (edge * edge).sum(axis=1), 0, 1)
            residual = vector - t[:, :, None] * edge
            distances = np.minimum(distances, (residual * residual).sum(axis=2))
        nearest = np.sqrt(distances.min(axis=1))
        nearest[interior] = 0
        if not np.isfinite(nearest).all():
            raise CheckError("a collision distance is non-finite")
        worst = max(worst, float(nearest.max(initial=0)))
    return worst


def _source_convex_hull(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    import mujoco

    vertices = " ".join(format(float(value), ".17g") for value in np.unique(points, axis=0).flat)
    source = ET.Element("mujoco")
    ET.SubElement(source, "compiler", inertiafromgeom="false")
    ET.SubElement(ET.SubElement(source, "asset"), "mesh", name="source", vertex=vertices, inertia="shell")
    ET.SubElement(ET.SubElement(source, "worldbody"), "geom", type="mesh", mesh="source")
    model = mujoco.MjModel.from_xml_string(ET.tostring(source, encoding="unicode"))
    return _compiled_hull(model, 0)


def _gate_collision_hulls(state: dict) -> dict:
    model = _need(state, "robot_model")
    document = _need(state, "document")
    config, policy = state["config"], state["policy"]
    _zero_pose(state)
    data = state["robot_data"]
    poses = _fk_pose(document, {})
    budget = config["collision"]["max_vertices"]
    limit = config["collision"]["max_error_m"]
    directory = "/".join(state["urdf_relative"].split("/")[:-1])
    rows = []
    for item in _expected_geoms(document, config, policy):
        if item["role"] != "collision" or item["surface"]["geometry"]["kind"] != "mesh":
            continue
        geom_id = int(model.geom(item["name"]).id)
        mesh_id = int(model.geom_dataid[geom_id])
        expected, references = [], []
        for surface in item["source_surfaces"]:
            geometry = surface["geometry"]
            reference = _canonical_mesh_path(directory, geometry["filename"], item["name"])
            references.append(reference)
            points = state["meshes"][reference] * np.asarray(geometry["scale"], dtype=float)
            transform = _rpy_matrix(surface["origin"]["rpy"])
            expected.append(points @ transform.T + np.asarray(surface["origin"]["xyz"]))
        source, source_faces = _source_convex_hull(np.concatenate(expected))
        _, actual_faces = _compiled_hull(model, mesh_id)
        begin = int(model.mesh_vertadr[mesh_id])
        end = begin + int(model.mesh_vertnum[mesh_id])
        vertices = np.asarray(model.mesh_vert[begin:end], dtype=float)
        actual = vertices @ np.asarray(data.geom_xmat[geom_id]).reshape(3, 3).T + data.geom_xpos[geom_id]
        frame = poses[item["link"]]
        actual = (actual - frame[:3, 3]) @ frame[:3, :3]
        source_points, actual_points = source[np.unique(source_faces)], actual[np.unique(actual_faces)]
        if len(actual_points) > budget:
            raise CheckError(f"collision {item['name']!r} exceeds the declared convex vertex budget")
        distance = max(_directed_convex_distance(source_points, actual, actual_faces),
                       _directed_convex_distance(actual_points, source, source_faces))
        if distance > limit + policy["tolerances"]["collision_distance_atol_m"]:
            raise CheckError(f"collision {item['name']!r} approximation distance {distance:.17g} m "
                             f"exceeds {limit:.17g} m for sources {references!r}")
        rows.append({"geom": item["name"], "link": item["link"],
                     "source_meshes": references, "source_convex_vertices": len(source_points),
                     "actual_vertices": len(actual_points), "complete_convex_vertex_distance_m": distance})
    return {"mesh_proxies": rows, "vertex_budget": budget, "max_error_m": limit,
            "scope": "Convex-solid distance relative to source component hulls; no concave-surface equivalence claim"}


def _derivation_records(state: dict) -> list[dict]:
    """Locate every encoded object and field in its source or explicit policy."""
    policy, document = state["policy"], state["document"]
    geoms = {row["name"]: row for row in _expected_geoms(document, state["config"], policy)}
    links = {row["name"]: row for row in document["links"]}
    incoming = {row["child"]: row for row in document["joints"]}
    urdf, records = state["urdf_relative"], []
    config_source = {"record": policy["config_name"], "fields": ["/base", "/collision", "/simulation", "/sites",
                                                                  "/support_sites", "/floor"]}
    policy_source = {"tool_sha256": state["manifest"]["tool"]["source_sha256"],
                     "module": "description_pipeline.mjcf_contract",
                     "fields": ["DEFAULT_POLICY", "ROLE_GROUPS", "CONTACT_CONDIM", "ROBOT_XML_SYNTAX",
                                "SCENE_XML_SYNTAX", "VISUAL_GEOM_TEMPLATE", "COLLISION_GEOM_TEMPLATE",
                                "CONVEX_GEOM_TEMPLATE", "FLOOR_RENDER_SIZE", "LIGHT_POS", "LIGHT_DIR"],
                     "default_policy_schema": policy["defaults"]["schema_version"]}

    def source(entity, name, fields):
        return {"record": urdf, "entity": entity, "name": name, "fields": fields}

    def visit(node, file, path, link=None):
        if not isinstance(node.tag, str):
            return
        if node.tag == "body":
            link = node.get("name")
        name = node.get("name")
        sources = [policy_source]
        rule = "versioned XML representation"
        if node.tag == "mujoco" and file == policy["robot_name"]:
            sources.append(source("robot", document["name"], ["name"]))
        elif node.tag == "body":
            sources.append(source("link", link, ["name"]))
            if link in incoming:
                sources.append(source("joint", incoming[link]["name"], ["origin", "parent", "child"]))
            rule = "URDF link tree and fixed origin encoding"
        elif node.tag == "inertial":
            sources.append(source("link", link, ["inertial.origin", "inertial.mass", "inertial.inertia"]))
            rule = "URDF tensor rotation and right-handed principal inertia encoding"
        elif node.tag == "joint":
            sources.append(source("joint", name, ["name", "type", "axis", "limit", "dynamics"]))
            rule = "URDF joint, directed axis, limits and authored dynamics encoding"
        elif node.tag == "geom" and name in geoms:
            row = geoms[name]
            native_role = "collisions" if row["role"] == "collision" and links[link]["collision_role"] == "urdf" \
                else "visuals"
            selected = row.get("source_surfaces", [row["surface"]])
            indices = [links[link][native_role].index(surface) for surface in selected]
            fields = ("origin", "geometry", "material") if native_role == "visuals" else ("origin", "geometry")
            sources.append(source("link", link, ["name", *[f"{native_role}[{index}].{field}" for index in indices
                                                           for field in fields]]))
            sources.append(config_source)
            if row["role"] == "collision" and native_role == "visuals" and state["manifest"]["source"]["components"]:
                sources.append({"record": state["manifest"]["source"]["components"], "entity": "link", "name": link,
                                "fields": [f"visuals.{surface['name']}.provenance.native_component"
                                           for surface in selected]})
            rule = "URDF surface encoding and declared convex contact approximation"
        elif node.tag == "mesh":
            filename = _canonical_mesh_path("/".join(file.split("/")[:-1]), node.get("file"), name)
            sources.extend([{"record": filename, "fields": ["vertices", "faces"]}, config_source])
            directory = "/".join(urdf.split("/")[:-1])
            originals = set()
            for row in geoms.values():
                geometry = row["surface"]["geometry"]
                if (geometry["kind"] != "mesh"
                        or _canonical_mesh_path(directory, geometry["filename"], name) != filename):
                    continue
                for surface in row.get("source_surfaces", [row["surface"]]):
                    originals.add(_canonical_mesh_path(directory, surface["geometry"]["filename"], name))
            sources.extend({"record": original, "fields": ["vertices"]} for original in sorted(originals))
            rule = "Mesh asset reference, scale and bounded convex preprocessing"
        elif node.tag in {"freejoint", "option", "site", "geom", "light"}:
            sources.append(config_source)
            for support in state["config"]["support_sites"]:
                role = "collisions" if links[support["link"]]["collision_role"] == "urdf" else "visuals"
                sources.append(source("link", support["link"], [f"{role}.geometry", f"{role}.origin"]))
            rule = "Explicit simulation settings, policy constants or source-derived support frame"
        elif node.tag == "include":
            sources.append({"record": policy["robot_name"], "fields": ["document"]})
        records.append({"code": "mjcf.derived_fields", "blocking": False,
                        "target": {"file": file, "path": path, "tag": node.tag, "name": name, "link": link,
                                   "fields": sorted(node.attrib) or ["element"]},
                        "sources": sources, "rule": rule,
                        "scope": "Derived encoding and policy values, not direct native CAD measurements"})
        for index, child in enumerate(node):
            visit(child, file, [*path, index], link)

    for file in (policy["robot_name"], policy["scene_name"]):
        if file in state["manifest"]["files"]:
            visit(ET.fromstring(_confined(state["root"], file).read_bytes()), file, [])
    return records


def _gate_provenance_warnings(state: dict) -> dict:
    """Compare the actual source warning comments with the delivered encoding."""
    _need(state, "document")
    def warnings(path):
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        root = ET.fromstring(_confined(state["root"], path).read_bytes(), parser=parser)
        return [item.text for item in root.iter()
                if item.tag is ET.Comment and (item.text or "").startswith("WARNING ")]
    source = warnings(state["urdf_relative"])
    delivered = warnings(state["policy"]["robot_name"])
    policy, config = state["policy"], state["config"]
    expected = [*source, policy["simulation_warning"], policy["format_warning"]]
    for link in state["document"]["links"]:
        role = config["collision"]["source"]
        if role == "auto":
            role = "urdf" if link["collisions"] else "visual_convex"
        selected = link["collisions"] if role == "urdf" else link["visuals"]
        indices = [index for index, surface in enumerate(selected) if surface["geometry"]["kind"] == "mesh"]
        if role == "none" or not indices:
            continue
        payload = {"link": link["name"], "source_file": state["urdf_relative"],
                   "source_role": "collision" if role == "urdf" else "visual",
                   "surface_indices": indices, "max_vertices": config["collision"]["max_vertices"],
                   "max_error_m": config["collision"]["max_error_m"], "scope": policy["collision_scope"]}
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        expected.append(policy["collision_warning_prefix"] + encoded.replace("--", r"\u002d\u002d"))
    if delivered != expected:
        raise CheckError("MJCF warning comments differ from the source and simulation policy")
    return {"source_warning_count": len(source), "warning_sha256": _canonical_digest(expected),
            "warning_comments": expected, "derivations": _derivation_records(state)}


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
    "collision.hulls": _gate_collision_hulls,
    "joints.config": _gate_joints_config,
    "joints.restoring": _gate_joint_restoring,
    "model.configuration": _gate_model_configuration,
    "scene.scene": _gate_scene,
    "contacts.supports": _gate_support_contacts,
    "runtime.steps": _gate_runtime,
    "provenance.warnings": _gate_provenance_warnings,
}


# --------------------------------------------------------------------------
# The isolated child process
# --------------------------------------------------------------------------


def _child_report(request: dict) -> dict:
    policy = request["policy"]
    state = {"root": Path(request["root"]).resolve(), "policy": policy, "input_binding": request["input_binding"]}
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
        "input_binding_sha256": state.get("input_binding_sha256"),
        "facts": _facts(state),
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
    }


def _child_main() -> int:
    try:
        import resource

        request = json.load(sys.stdin)
        limits = request["policy"]["process_limits"]
        resource.setrlimit(resource.RLIMIT_CPU, (limits["cpu_seconds"], limits["cpu_seconds"] + 5))
        resource.setrlimit(resource.RLIMIT_AS, (limits["memory_bytes"], limits["memory_bytes"]))
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
        "simulation_warning": contract.SIMULATION_WARNING,
        "format_warning": contract.FORMAT_WARNING,
        "collision_warning_prefix": contract.COLLISION_WARNING_PREFIX,
        "collision_scope": contract.COLLISION_SCOPE,
        "process_limits": {"cpu_seconds": contract.VERIFIER_CPU_SECONDS,
                           "memory_bytes": contract.VERIFIER_MEMORY_BYTES},
        "input_binding_schema": contract.INPUT_BINDING_SCHEMA,
        "component_source": contract.COMPONENT_SOURCE,
        "config_name": contract.CONFIG_NAME,
        "robot_name": contract.ROBOT_NAME,
        "scene_name": contract.SCENE_NAME,
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
            "support": contract.SUPPORT_SITE_TEMPLATE,
        },
        "defaults": contract.DEFAULT_POLICY,
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
            "collision_distance_atol_m": contract.COLLISION_DISTANCE_ATOL_M,
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
        "limit_probe_offset": contract.LIMIT_PROBE_OFFSET,
        "contact_probe_depth_m": contract.CONTACT_PROBE_DEPTH_M,
        "engine": contract.engine_identity(),
        "tool": contract.tool_identity(),
    }


def _run_child(root: Path, input_binding: dict) -> dict:
    from .. import mjcf_contract as contract

    command = [sys.executable, "-I", str(Path(__file__).resolve())]
    request = json.dumps({"root": str(root), "policy": _policy(), "input_binding": input_binding})
    try:
        completed = subprocess.run(
            command,
            input=request,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=contract.VERIFIER_TIMEOUT_SECONDS,
            env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
        )
    except subprocess.TimeoutExpired as error:
        raise CheckError(f"the checker process timed out after {contract.VERIFIER_TIMEOUT_SECONDS} seconds") from error
    if completed.returncode != 0:
        raise CheckError(f"the checker process exited with status {completed.returncode}: "
                         f"{completed.stderr.strip()[-2000:]}")
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
        "input_binding_sha256": None,
        "facts": {},
        "checks": [{"id": "engine.process", "state": "failed", "passed": False, "details": {"error": message}}],
        "passed": False,
    }


def evaluate_projection(root: Path, input_binding: dict) -> dict:
    """Independently evaluate a private view and rebind its bytes after the child exits."""
    from ..io import PipelineError, confined, inventory

    root = Path(root)
    try:
        report = _run_child(root, input_binding)
        expected = input_binding["files"]
        actual = {name: sha for name, sha in inventory(root).items()
                  if name.startswith(("urdf/", "meshes/", "mjcf/")) or name == input_binding["source"]["components"]}
        stable = actual == {name: row["sha256"] for name, row in expected.items()}
        stable = stable and all(confined(root, name).stat().st_size == row["bytes"]
                                for name, row in expected.items())
        report["checks"].append({
            "id": "identity.stability", "state": "passed" if stable else "failed",
            "passed": stable, "details": {"unchanged": stable},
        })
        report["passed"] = report["passed"] and stable
        return report
    except (CheckError, PipelineError, OSError, KeyError, TypeError, ValueError) as error:
        return _broken_report(str(error))
