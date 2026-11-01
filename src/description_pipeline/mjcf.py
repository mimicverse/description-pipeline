"""Project one verified URDF reference closure into private MJCF stage files.

The URDF and the referenced meshes are the only source of mechanical facts; the
simulation configuration owns base mode, collision roles, contact parameters,
sites and the optional scene.  The projection writes round-trip exact numbers,
rotates each inertia tensor into the link frame and stores it as principal
values with a right-handed principal pose computed by the locked NumPy
``eigh``. The stage engine owns immutable publication after an independent
checker recomputes a passing report from the written bytes.
"""

from __future__ import annotations

import hashlib
import math
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from . import mjcf_contract as contract
from .io import PipelineError, artifact_path_parts, confined, format_number, parse_data

_STL_RECORD = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")])
_JOINT_TYPES = ("fixed", "revolute", "continuous", "prismatic")
_SUPPORT_BLOCK_ELEMENTS = 1024 * 1024


# --------------------------------------------------------------------------
# Strict URDF reading
# --------------------------------------------------------------------------


def _unknown_attributes(element: ET.Element, allowed: set[str], where: str) -> None:
    unknown = sorted(set(element.attrib) - allowed)
    if unknown:
        raise PipelineError(f"{where}: unsupported attribute {unknown[0]!r}")


def _child_elements(element: ET.Element, allowed: set[str], where: str) -> list[ET.Element]:
    result = []
    for child in element:
        if child.tag is ET.Comment:
            continue
        if not isinstance(child.tag, str) or child.tag not in allowed:
            raise PipelineError(f"{where}: unsupported element <{child.tag!r}>")
        result.append(child)
    return result


def _sole(element: ET.Element, tag: str, where: str, *, required: bool = False) -> ET.Element | None:
    found = [child for child in element if isinstance(child.tag, str) and child.tag == tag]
    if len(found) > 1:
        raise PipelineError(f"{where}: multiple <{tag}> elements")
    if not found:
        if required:
            raise PipelineError(f"{where}: missing <{tag}>")
        return None
    return found[0]


def _name_value(value: str | None, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise PipelineError(f"{where}: a non-empty name is required")
    if any(ord(character) < 32 for character in value) or value != value.strip():
        raise PipelineError(f"{where}: the name {value!r} is not portable")
    return value


def _numbers(text: str | None, count: int, where: str) -> tuple[float, ...]:
    if text is None:
        raise PipelineError(f"{where}: missing value")
    fields = text.split()
    if len(fields) != count:
        raise PipelineError(f"{where}: expected {count} numbers, got {text!r}")
    values = []
    for field in fields:
        try:
            value = float(field)
        except ValueError as error:
            raise PipelineError(f"{where}: {field!r} is not a number") from error
        if not math.isfinite(value):
            raise PipelineError(f"{where}: {field!r} is not finite")
        values.append(value)
    return tuple(values)


def _origin(element: ET.Element | None, where: str) -> dict:
    if element is None:
        return {"xyz": (0.0, 0.0, 0.0), "rpy": (0.0, 0.0, 0.0)}
    _unknown_attributes(element, {"xyz", "rpy"}, f"{where}/origin")
    xyz = element.get("xyz")
    rpy = element.get("rpy")
    return {
        "xyz": _numbers(xyz, 3, f"{where}/origin@xyz") if xyz is not None else (0.0, 0.0, 0.0),
        "rpy": _numbers(rpy, 3, f"{where}/origin@rpy") if rpy is not None else (0.0, 0.0, 0.0),
    }


def _rgba(text: str | None, where: str) -> tuple[float, float, float, float]:
    values = _numbers(text, 4, where)
    if any(not 0.0 <= value <= 1.0 for value in values):
        raise PipelineError(f"{where}: colour components must lie in [0, 1]")
    return values  # type: ignore[return-value]


def _geometry(element: ET.Element | None, where: str) -> dict:
    if element is None:
        raise PipelineError(f"{where}: missing <geometry>")
    children = _child_elements(element, {"box", "sphere", "cylinder", "mesh"}, where)
    if len(children) != 1:
        raise PipelineError(f"{where}: <geometry> must contain exactly one shape")
    shape = children[0]
    tag = f"{where}/{shape.tag}"
    if shape.tag == "box":
        _unknown_attributes(shape, {"size"}, tag)
        size = _numbers(shape.get("size"), 3, f"{tag}@size")
        if any(value <= 0 for value in size):
            raise PipelineError(f"{tag}: box size must be positive")
        return {"kind": "box", "size": size}
    if shape.tag == "sphere":
        _unknown_attributes(shape, {"radius"}, tag)
        radius = _numbers(shape.get("radius"), 1, f"{tag}@radius")[0]
        if radius <= 0:
            raise PipelineError(f"{tag}: sphere radius must be positive")
        return {"kind": "sphere", "radius": radius}
    if shape.tag == "cylinder":
        _unknown_attributes(shape, {"radius", "length"}, tag)
        radius = _numbers(shape.get("radius"), 1, f"{tag}@radius")[0]
        length = _numbers(shape.get("length"), 1, f"{tag}@length")[0]
        if radius <= 0 or length <= 0:
            raise PipelineError(f"{tag}: cylinder radius and length must be positive")
        return {"kind": "cylinder", "radius": radius, "length": length}
    _unknown_attributes(shape, {"filename", "scale"}, tag)
    filename = shape.get("filename")
    if not filename:
        raise PipelineError(f"{tag}: mesh@filename is required")
    scale = (1.0, 1.0, 1.0) if shape.get("scale") is None else _numbers(shape.get("scale"), 3, f"{tag}@scale")
    if any(value <= 0 for value in scale):
        raise PipelineError(f"{tag}: mesh scale must be positive")
    return {"kind": "mesh", "filename": filename, "scale": scale}


def _visual_surface(element: ET.Element, where: str, materials: dict) -> dict:
    _unknown_attributes(element, {"name"}, where)
    _child_elements(element, {"origin", "geometry", "material"}, where)
    origin = _origin(_sole(element, "origin", where), where)
    geometry = _geometry(_sole(element, "geometry", where, required=True), where)
    material = _sole(element, "material", where)
    color = None
    if material is not None:
        _unknown_attributes(material, {"name"}, f"{where}/material")
        _child_elements(material, {"color"}, f"{where}/material")
        inline = _sole(material, "color", f"{where}/material")
        if inline is not None:
            _unknown_attributes(inline, {"rgba"}, f"{where}/material/color")
            color = _rgba(inline.get("rgba"), f"{where}/material/color@rgba")
        else:
            reference = material.get("name")
            if reference is None or reference not in materials:
                raise PipelineError(f"{where}/material: unknown material reference {reference!r}")
            color = materials[reference]
    return {"name": element.get("name"), "origin": origin, "geometry": geometry, "color": color}


def _collision_surface(element: ET.Element, where: str) -> dict:
    _unknown_attributes(element, {"name"}, where)
    _child_elements(element, {"origin", "geometry"}, where)
    return {
        "origin": _origin(_sole(element, "origin", where), where),
        "geometry": _geometry(_sole(element, "geometry", where, required=True), where),
    }


def _inertial(element: ET.Element | None, where: str) -> dict | None:
    if element is None:
        return None
    _child_elements(element, {"origin", "mass", "inertia"}, where)
    origin = _origin(_sole(element, "origin", where), where)
    mass_element = _sole(element, "mass", where, required=True)
    _unknown_attributes(mass_element, {"value"}, f"{where}/mass")
    mass = _numbers(mass_element.get("value"), 1, f"{where}/mass@value")[0]
    if mass <= 0:
        raise PipelineError(f"{where}/mass: mass must be positive, got {mass!r}")
    inertia_element = _sole(element, "inertia", where, required=True)
    _unknown_attributes(inertia_element, {"ixx", "ixy", "ixz", "iyy", "iyz", "izz"}, f"{where}/inertia")
    ixx = _numbers(inertia_element.get("ixx"), 1, f"{where}/inertia@ixx")[0]
    ixy = _numbers(inertia_element.get("ixy"), 1, f"{where}/inertia@ixy")[0]
    ixz = _numbers(inertia_element.get("ixz"), 1, f"{where}/inertia@ixz")[0]
    iyy = _numbers(inertia_element.get("iyy"), 1, f"{where}/inertia@iyy")[0]
    iyz = _numbers(inertia_element.get("iyz"), 1, f"{where}/inertia@iyz")[0]
    izz = _numbers(inertia_element.get("izz"), 1, f"{where}/inertia@izz")[0]
    tensor = np.array([[ixx, ixy, ixz], [ixy, iyy, iyz], [ixz, iyz, izz]])
    values = np.linalg.eigvalsh(tensor)
    if values[0] <= 0:
        raise PipelineError(f"{where}/inertia: the tensor is not positive definite")
    if values[2] > (values[0] + values[1]) * (1 + contract.INERTIA_TRIANGLE_RTOL):
        raise PipelineError(f"{where}/inertia: principal moments violate A + B >= C")
    return {"origin": origin, "mass": mass, "inertia": (ixx, ixy, ixz, iyy, iyz, izz)}


def _limited(element: ET.Element | None, where: str, joint_type: str) -> dict:
    if element is None:
        if joint_type in ("revolute", "prismatic"):
            raise PipelineError(f"{where}: a {joint_type} joint requires <limit>")
        return {"lower": None, "upper": None, "effort": None, "velocity": None}
    _unknown_attributes(element, {"lower", "upper", "effort", "velocity"}, f"{where}/limit")
    def optional(key):
        text = element.get(key)
        return _numbers(text, 1, f"{where}/limit@{key}")[0] if text is not None else None

    lower, upper = optional("lower"), optional("upper")
    effort, velocity = optional("effort"), optional("velocity")
    for label, value in (("effort", effort), ("velocity", velocity)):
        if value is not None and value < 0:
            raise PipelineError(f"{where}/limit@{label}: must be non-negative, got {value!r}")
    if joint_type in ("revolute", "prismatic"):
        if lower is None or upper is None:
            raise PipelineError(f"{where}/limit: lower and upper are required for a {joint_type} joint")
        if lower == upper:
            raise PipelineError(
                f"{where}/limit: the locked MJCF projection cannot encode a zero-width range "
                f"[{lower}, {upper}]; the source range is preserved and no replacement is inferred"
            )
        if lower > upper:
            raise PipelineError(
                f"{where}/limit: the range must be finite and strictly increasing, got [{lower}, {upper}]"
            )
    return {"lower": lower, "upper": upper, "effort": effort, "velocity": velocity}


def _dynamics(element: ET.Element | None, where: str) -> dict:
    if element is None:
        return {"damping": 0.0, "friction": 0.0}
    _unknown_attributes(element, {"damping", "friction"}, f"{where}/dynamics")
    damping = (
        _numbers(element.get("damping"), 1, f"{where}/dynamics@damping")[0]
        if element.get("damping") is not None
        else 0.0
    )
    friction = (
        _numbers(element.get("friction"), 1, f"{where}/dynamics@friction")[0]
        if element.get("friction") is not None
        else 0.0
    )
    if damping < 0 or friction < 0:
        raise PipelineError(f"{where}/dynamics: damping and friction must be non-negative")
    return {"damping": damping, "friction": friction}


def _normalized_axis(axis: tuple[float, ...], where: str) -> tuple[float, float, float]:
    """Finite nonzero axes keep their direction; only the magnitude is normalised."""

    norm = math.sqrt(sum(value * value for value in axis))
    if norm == 0:
        raise PipelineError(f"{where}: the joint axis must be nonzero")
    if not math.isfinite(norm):
        raise PipelineError(f"{where}: the joint axis must be finite")
    return (axis[0] / norm, axis[1] / norm, axis[2] / norm)


def _read_urdf(content: bytes, label: str) -> dict:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PipelineError(f"{label}: the URDF must be UTF-8: {error}") from error
    for marker in ("<!DOCTYPE", "<!ENTITY"):
        if marker in text:
            raise PipelineError(f"{label}: DTDs and external entities are not supported")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise PipelineError(f"{label}: invalid XML: {error}") from error
    if root.tag != "robot":
        raise PipelineError(f"{label}: the root element must be <robot>")
    _unknown_attributes(root, {"name"}, f"{label}: robot")
    robot_name = _name_value(root.get("name"), f"{label}: robot@name")
    _child_elements(root, {"link", "joint", "material"}, f"{label}: robot")

    materials: dict[str, tuple] = {}
    for element in root.findall("material"):
        where = f"{label}: robot/material"
        _unknown_attributes(element, {"name"}, where)
        name = _name_value(element.get("name"), f"{where}@name")
        _child_elements(element, {"color"}, where)
        color_element = _sole(element, "color", where, required=True)
        _unknown_attributes(color_element, {"rgba"}, f"{where}/color")
        if name in materials:
            raise PipelineError(f"{where}: duplicate material name {name!r}")
        materials[name] = _rgba(color_element.get("rgba"), f"{where}/color@rgba")

    links: list[dict] = []
    names: set[str] = set()
    for element in root.findall("link"):
        where = f"{label}: link"
        _unknown_attributes(element, {"name"}, where)
        name = _name_value(element.get("name"), f"{where}@name")
        if name in names:
            raise PipelineError(f"{where}: duplicate link name {name!r}")
        names.add(name)
        position = f"{label}: link {name!r}"
        _child_elements(element, {"inertial", "visual", "collision"}, position)
        links.append(
            {
                "name": name,
                "inertial": _inertial(_sole(element, "inertial", position), position),
                "visuals": [
                    _visual_surface(visual, f"{position}/visual[{index}]", materials)
                    for index, visual in enumerate(element.findall("visual"))
                ],
                "collisions": [
                    _collision_surface(collision, f"{position}/collision[{index}]")
                    for index, collision in enumerate(element.findall("collision"))
                ],
            }
        )
    if not links:
        raise PipelineError(f"{label}: the URDF must declare at least one link")

    joints: list[dict] = []
    joint_names: set[str] = set()
    for element in root.findall("joint"):
        where = f"{label}: joint"
        _unknown_attributes(element, {"name", "type"}, where)
        name = _name_value(element.get("name"), f"{where}@name")
        if name in joint_names:
            raise PipelineError(f"{where}: duplicate joint name {name!r}")
        joint_names.add(name)
        joint_type = element.get("type")
        position = f"{label}: joint {name!r}"
        if joint_type not in _JOINT_TYPES:
            raise PipelineError(f"{position}: unsupported joint type {joint_type!r}")
        _child_elements(element, {"origin", "parent", "child", "axis", "limit", "dynamics"}, position)
        parent = _sole(element, "parent", position, required=True)
        child = _sole(element, "child", position, required=True)
        _unknown_attributes(parent, {"link"}, f"{position}/parent")
        _unknown_attributes(child, {"link"}, f"{position}/child")
        parent_name = _name_value(parent.get("link"), f"{position}/parent@link")
        child_name = _name_value(child.get("link"), f"{position}/child@link")
        axis_element = _sole(element, "axis", position)
        axis = None
        if axis_element is not None:
            _unknown_attributes(axis_element, {"xyz"}, f"{position}/axis")
            axis = _numbers(axis_element.get("xyz"), 3, f"{position}/axis@xyz")
        if joint_type != "fixed" and axis is None:
            axis = (1.0, 0.0, 0.0)
        if joint_type != "fixed":
            axis = _normalized_axis(axis, f"{position}/axis")
        joints.append(
            {
                "name": name,
                "type": joint_type,
                "parent": parent_name,
                "child": child_name,
                "origin": _origin(_sole(element, "origin", position), position),
                "axis": axis,
                "limit": _limited(_sole(element, "limit", position), position, joint_type),
                "dynamics": _dynamics(_sole(element, "dynamics", position), position),
            }
        )

    _validate_tree(links, joints, label)
    return {"name": robot_name, "links": links, "joints": joints}


def _validate_tree(links: list[dict], joints: list[dict], label: str) -> None:
    known = {link["name"] for link in links}
    children: dict[str, str] = {}
    for joint in joints:
        if joint["parent"] not in known or joint["child"] not in known:
            raise PipelineError(f"{label}: joint {joint['name']!r} references an unknown link")
        if joint["parent"] == joint["child"]:
            raise PipelineError(f"{label}: joint {joint['name']!r} connects a link to itself")
        if joint["child"] in children:
            raise PipelineError(
                f"{label}: link {joint['child']!r} has multiple parent joints (closed loop or duplicate child)"
            )
        children[joint["child"]] = joint["name"]
    roots = [link["name"] for link in links if link["name"] not in children]
    if len(roots) != 1:
        raise PipelineError(f"{label}: the URDF must form a single connected tree, found {len(roots)} root links")
    adjacency: dict[str, list[str]] = {link["name"]: [] for link in links}
    for joint in joints:
        adjacency[joint["parent"]].append(joint["child"])
    visited = {roots[0]}
    pending = [roots[0]]
    while pending:
        for child in adjacency[pending.pop()]:
            if child in visited:
                raise PipelineError(f"{label}: the URDF is not an acyclic tree (link {child!r} is reachable twice)")
            visited.add(child)
            pending.append(child)
    if visited != known:
        missing = sorted(known - visited)
        raise PipelineError(f"{label}: links unreachable from the root: {missing}")


def _validate_mobility(links: list[dict], joints: list[dict], base: str, label: str) -> None:
    """Movable rigid bodies must carry a physical inertial; fixed frames may stay massless."""

    by_name = {link["name"]: link for link in links}
    movable = {joint["child"] for joint in joints if joint["type"] != "fixed"}
    root = next(link["name"] for link in links if all(joint["child"] != link["name"] for joint in joints))
    required = set(movable) | ({root} if base == "floating" else set())
    for name in sorted(required):
        if by_name[name]["inertial"] is None:
            raise PipelineError(
                f"{label}: link {name!r} can move but declares no <inertial>; "
                "massless frames are only supported for statically attached links"
            )


# --------------------------------------------------------------------------
# Mesh closure
# --------------------------------------------------------------------------


def _read_binary_stl(content: bytes, label: str) -> np.ndarray:
    if len(content) < 84:
        raise PipelineError(f"{label}: not a binary STL (shorter than its 84-byte header)")
    count = struct.unpack_from("<I", content, 80)[0]
    expected = 84 + 50 * count
    if len(content) != expected:
        raise PipelineError(
            f"{label}: not a binary STL or corrupt (expected {expected} bytes for {count} triangles, "
            f"found {len(content)}; ASCII STL is not supported)"
        )
    if count == 0:
        raise PipelineError(f"{label}: the mesh is empty (no triangles)")
    records = np.frombuffer(content, dtype=_STL_RECORD, count=count, offset=84)
    vertices = records["vertices"].reshape(-1, 3).astype(np.float64)
    if not np.isfinite(vertices).all():
        raise PipelineError(f"{label}: the mesh carries non-finite coordinates")
    return vertices


def _canonical_reference(directory_parts: list[str], reference: str, where: str) -> str:
    if "://" in reference:
        raise PipelineError(f"{where}: remote and package:// mesh references are not supported: {reference!r}")
    if reference.startswith("/") or "\\" in reference:
        raise PipelineError(f"{where}: the mesh reference must be a relative POSIX path: {reference!r}")
    stack = list(directory_parts)
    for part in reference.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                raise PipelineError(f"{where}: the mesh reference escapes the input root: {reference!r}")
            stack.pop()
        else:
            stack.append(part)
    if not stack:
        raise PipelineError(f"{where}: the mesh reference does not name a file: {reference!r}")
    canonical = "/".join(stack)
    artifact_path_parts(canonical)
    return canonical


def _check_case_conflicts(paths: list[str]) -> None:
    seen: dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        for index in range(len(parts)):
            prefix = "/".join(parts[: index + 1])
            key = prefix.casefold()
            prior = seen.get(key)
            if prior is None:
                seen[key] = prefix
            elif prior != prefix:
                raise PipelineError(f"case-conflicting input paths: {prior!r} and {prefix!r}")


def _resolve_meshes(
    document: dict, root: Path, urdf_relative: str
) -> tuple[dict[str, bytes], dict[str, np.ndarray]]:
    urdf_directory = urdf_relative.split("/")[:-1]
    resolved: dict[str, str] = {}

    def surfaces():
        for link in document["links"]:
            for role in ("visuals", "collisions"):
                yield from link[role]

    for surface in surfaces():
        geometry = surface["geometry"]
        if geometry["kind"] != "mesh":
            continue
        where = f"mesh reference {geometry['filename']!r}"
        canonical = _canonical_reference(urdf_directory, geometry["filename"], where)
        if canonical == urdf_relative:
            raise PipelineError(f"{where}: the URDF itself is not a mesh")
        geometry["path"] = canonical
        resolved[canonical] = geometry["filename"]
    paths = sorted(resolved)
    _check_case_conflicts([urdf_relative, *paths])

    contents: dict[str, bytes] = {}
    meshes: dict[str, np.ndarray] = {}
    for canonical in paths:
        content = confined(root, canonical).read_bytes()
        contents[canonical] = content
        meshes[canonical] = _read_binary_stl(content, canonical)
    return contents, meshes


def _encode_meshes(document: dict, contents: dict, meshes: dict) -> tuple[dict, dict, list]:
    """Encode only engine-consumed source meshes without changing their geometry or instances."""
    used = {surface["geometry"]["path"]
            for link in document["links"]
            for surface in (*link["visuals"], *link["projection_collisions"])
            if surface["geometry"]["kind"] == "mesh" and surface["geometry"]["path"] in contents}
    files, paths, warnings = {}, {}, []
    for source in sorted(used):
        sha = hashlib.sha256(contents[source]).hexdigest()
        target = contract.MESH_ENCODING_PREFIX + sha + ".obj"
        paths[source] = target
        if target not in files:
            vertices, indices = np.unique(meshes[source], axis=0, return_inverse=True)
            # Float64 round trips retain the exact decoded float32 coordinates.
            lines = ["v " + " ".join(repr(float(value)) for value in vertex) for vertex in vertices]
            lines.extend("f " + " ".join(str(int(index) + 1) for index in face)
                         for face in indices.reshape(-1, 3))
            files[target] = ("\n".join(lines) + "\n").encode("ascii")
        warnings.append(contract.mesh_encoding_warning({
            "source_file": source, "source_sha256": sha, "target_file": target,
            "target_sha256": hashlib.sha256(files[target]).hexdigest(),
            "triangles": len(meshes[source]) // 3, "rule": contract.MESH_ENCODING,
            "scope": contract.MESH_ENCODING_SCOPE,
        }))
    return files, paths, warnings


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def _component_groups(document: dict, metadata: bytes | None) -> None:
    """Read source identities only; URDF retains all mechanical and geometric values."""
    names = {}
    if metadata is not None:
        model = parse_data(metadata, Path(contract.COMPONENT_SOURCE))
        if not isinstance(model, dict) or not isinstance(model.get("links"), list):
            raise PipelineError("Component provenance has no link inventory")
        for link in model["links"]:
            for visual in link.get("visuals", []):
                name = visual.get("name")
                component = visual.get("provenance", {}).get("native_component")
                if not isinstance(name, str) or not isinstance(component, str) or not component:
                    raise PipelineError("A native visual lacks its component provenance")
                key = (link["name"], name)
                if key in names and names[key] != component:
                    raise PipelineError("A visual name has conflicting native component identities")
                names[key] = component
    for link in document["links"]:
        groups = {}
        for index, visual in enumerate(link["visuals"]):
            if metadata is None:
                component = f"authored-visual:{index}"
            else:
                component = names.get((link["name"], visual["name"]))
                if component is None:
                    raise PipelineError(f"Visual {visual['name']!r} has no verified component binding")
            groups.setdefault(component, []).append(index)
        link["visual_groups"] = list(groups.items())


def _proxy_obj(points: np.ndarray, budget: int) -> bytes:
    """Use the locked engine to construct a bounded convex proxy in link coordinates."""
    import mujoco

    if mujoco.__version__ != contract.engine_identity()["mujoco"]:
        raise PipelineError("Collision projection requires the locked MuJoCo engine")
    xml = ET.Element("mujoco")
    ET.SubElement(xml, "compiler", inertiafromgeom="false")
    values = " ".join(_fmt(value) for value in np.unique(points, axis=0).flat)
    ET.SubElement(ET.SubElement(xml, "asset"), "mesh", name="component", vertex=values,
                  inertia="shell", maxhullvert=str(budget))
    ET.SubElement(ET.SubElement(xml, "worldbody"), "geom", type="mesh", mesh="component")
    model = mujoco.MjModel.from_xml_string(ET.tostring(xml, encoding="unicode"))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    address = int(model.mesh_graphadr[0])
    if address < 0:
        raise PipelineError("The component has no valid convex collision hull")
    count, faces = map(int, model.mesh_graph[address:address + 2])
    start = address + 2 + 3 * count + 3 * faces
    triangles = np.asarray(model.mesh_graph[start:start + 3 * faces], dtype=int).reshape(-1, 3)
    cloud = np.asarray(model.mesh_vert, dtype=float) @ data.geom_xmat[0].reshape(3, 3).T + data.geom_xpos[0]
    selected = np.unique(triangles)
    remap = {int(old): index + 1 for index, old in enumerate(selected)}
    lines = ["# Derived convex component proxy; source bindings are in the MJCF certificate."]
    lines.extend("v " + _fmt3(cloud[index]) for index in selected)
    lines.extend("f " + " ".join(str(remap[int(index)]) for index in face) for face in triangles)
    return ("\n".join(lines) + "\n").encode("ascii")


def _prepare_collisions(document: dict, config: dict, meshes: dict) -> dict[str, bytes]:
    outputs = {}
    for link in document["links"]:
        role = config["collision"]["source"]
        if role == "auto":
            role = "urdf" if link["collisions"] else "visual_convex"
        projected = []
        if role == "urdf":
            projected = list(link["collisions"])
        elif role == "visual_convex":
            for component, indices in link["visual_groups"]:
                if len(indices) == 1:
                    projected.append(link["visuals"][indices[0]])
                    continue
                cloud = []
                for index in indices:
                    surface = link["visuals"][index]
                    geometry = surface["geometry"]
                    if geometry["kind"] != "mesh":
                        raise PipelineError("Native component aggregation requires mesh surfaces")
                    transform = _origin_matrix(surface["origin"])
                    points = meshes[geometry["path"]] * np.asarray(geometry["scale"])
                    cloud.append(points @ transform[:3, :3].T + transform[:3, 3])
                path = contract.component_proxy_path(link["name"], component)
                outputs[path] = _proxy_obj(np.concatenate(cloud), config["collision"]["max_vertices"])
                projected.append({"origin": {"xyz": (0., 0., 0.), "rpy": (0., 0., 0.)},
                                  "geometry": {"kind": "mesh", "path": path, "scale": (1., 1., 1.)}})
        link["projection_collisions"] = projected
        link["collision_role"] = role
    return outputs


def _fmt(value: float) -> str:
    return format_number(value)


def _fmt3(values) -> str:
    return " ".join(_fmt(value) for value in values)


# --------------------------------------------------------------------------
# Support-site derivation: explicit link selection, geometry-only decision.
# --------------------------------------------------------------------------


def _origin_matrix(origin: dict) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, :3] = np.array(contract.rpy_matrix(origin["rpy"]))
    matrix[:3, 3] = origin["xyz"]
    return matrix


def _zero_poses(document: dict) -> dict[str, np.ndarray]:
    joints_by_child = {joint["child"]: joint for joint in document["joints"]}
    children: dict[str, list[str]] = {link["name"]: [] for link in document["links"]}
    for joint in document["joints"]:
        children[joint["parent"]].append(joint["child"])
    root = next(link["name"] for link in document["links"] if link["name"] not in joints_by_child)
    poses: dict[str, np.ndarray] = {}

    def visit(name: str, parent: np.ndarray) -> None:
        joint = joints_by_child.get(name)
        pose = parent if joint is None else parent @ _origin_matrix(joint["origin"])
        poses[name] = pose
        for child in children[name]:
            visit(child, pose)

    visit(root, np.eye(4))
    return poses


def _surface_geometry(surface: dict, link_pose: np.ndarray, meshes: dict[str, np.ndarray]) -> dict:
    world = link_pose @ _origin_matrix(surface["origin"])
    geometry = surface["geometry"]
    if geometry["kind"] == "mesh":
        raw = meshes[geometry["path"]] * np.asarray(geometry["scale"], dtype=float)
        return {"kind": "mesh", "vertices": raw @ world[:3, :3].T + world[:3, 3]}
    if geometry["kind"] == "box":
        return {
            "kind": "box",
            "center": world[:3, 3],
            "rotation": world[:3, :3],
            "half": np.asarray(geometry["size"], dtype=float) / 2,
        }
    if geometry["kind"] == "sphere":
        return {"kind": "sphere", "center": world[:3, 3], "radius": geometry["radius"]}
    return {
        "kind": "cylinder",
        "center": world[:3, 3],
        "rotation": world[:3, :3],
        "radius": geometry["radius"],
        "half_length": geometry["length"] / 2,
    }


def _minimum_support(geometry: dict, directions: np.ndarray) -> np.ndarray:
    """The realised minimum of direction . point over one whole surface."""

    if geometry["kind"] == "mesh":
        vertices = geometry["vertices"]
        directions = np.asarray(directions)
        if not len(vertices):
            raise PipelineError("Mesh support requires a nonempty vertex set")
        minima = np.full(len(directions), np.inf)
        direction_step = min(256, _SUPPORT_BLOCK_ELEMENTS)
        for start in range(0, len(directions), direction_step):
            block = directions[start:start + direction_step]
            vertex_step = max(1, _SUPPORT_BLOCK_ELEMENTS // len(block))
            current = minima[start:start + len(block)]
            for offset in range(0, len(vertices), vertex_step):
                values = vertices[offset:offset + vertex_step] @ block.T
                np.minimum(current, np.min(values, axis=0), out=current)
        return minima
    projection = np.asarray(directions) @ geometry["center"]
    if geometry["kind"] == "box":
        local = np.asarray(directions) @ geometry["rotation"]
        return projection - np.sum(np.abs(local) * geometry["half"], axis=1)
    if geometry["kind"] == "sphere":
        return projection - geometry["radius"]
    axis = geometry["rotation"][:, 2]
    cosine = np.asarray(directions) @ axis
    return (
        projection
        - geometry["half_length"] * np.abs(cosine)
        - geometry["radius"] * np.sqrt(np.maximum(0.0, 1.0 - cosine * cosine))
    )


def _minimum_height(geometry: dict) -> float:
    if geometry["kind"] == "mesh":
        return float(np.min(geometry["vertices"][:, 2]))
    if geometry["kind"] == "box":
        return float(geometry["center"][2] - np.sum(np.abs(geometry["rotation"][2, :]) * geometry["half"]))
    if geometry["kind"] == "sphere":
        return float(geometry["center"][2] - geometry["radius"])
    axis_z = float(geometry["rotation"][2, 2])
    return float(
        geometry["center"][2]
        - geometry["half_length"] * abs(axis_z)
        - geometry["radius"] * math.sqrt(max(0.0, 1.0 - axis_z * axis_z))
    )


def _convex_hull_2d(points: np.ndarray) -> np.ndarray:
    order = np.lexsort((points[:, 1], points[:, 0]))
    ordered = points[order]
    unique = [ordered[0]]
    for row in ordered[1:]:
        if row[0] != unique[-1][0] or row[1] != unique[-1][1]:
            unique.append(row)
    if len(unique) < 3:
        return np.array(unique)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= contract.SUPPORT_HULL_EPS:
            lower.pop()
        lower.append(point)
    upper: list = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= contract.SUPPORT_HULL_EPS:
            upper.pop()
        upper.append(point)
    return np.array(lower[:-1] + upper[:-1])


def _polygon_centroid(polygon: np.ndarray) -> tuple[float, np.ndarray]:
    twice_area = 0.0
    centroid = np.zeros(2)
    for index in range(len(polygon)):
        first = polygon[index]
        second = polygon[(index + 1) % len(polygon)]
        factor = first[0] * second[1] - second[0] * first[1]
        twice_area += factor
        centroid += factor * (first + second)
    area = twice_area / 2
    if abs(twice_area) < contract.SUPPORT_HULL_EPS:
        return 0.0, np.zeros(2)
    return abs(area), centroid / (3 * twice_area)


def _support_site_frame(name: str, link: str, geometries: list[dict]) -> tuple[tuple, tuple]:
    """The unique largest contact hull of a supporting plane, expressed in the link frame."""

    triangles = [geometry["vertices"].reshape(-1, 3, 3) for geometry in geometries if geometry["kind"] == "mesh"]
    if not triangles:
        raise PipelineError(
            f"support site {name!r}: link {link!r} has no binary STL in the selected contact geometry"
        )
    faces = np.concatenate(triangles)
    normals = np.cross(faces[:, 1] - faces[:, 0], faces[:, 2] - faces[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    keep = lengths > contract.SUPPORT_TRIANGLE_EPS
    faces, normals = faces[keep], normals[keep] / lengths[keep, None]
    if not len(faces):
        raise PipelineError(f"support site {name!r}: every triangle of link {link!r} is degenerate")
    offsets = np.einsum("ij,ij->i", normals, faces[:, 0])
    flip = normals[:, 2] < 0
    normals[flip] = -normals[flip]
    offsets[flip] = -offsets[flip]
    keep = normals[:, 2] > contract.SUPPORT_NORMAL_Z_ATOL
    faces, normals, offsets = faces[keep], normals[keep], offsets[keep]
    if not len(faces):
        raise PipelineError(f"support site {name!r}: no upward-facing support plane candidate for link {link!r}")
    minima = np.full(len(faces), np.inf)
    for geometry in geometries:
        minima = np.minimum(minima, _minimum_support(geometry, normals))
    allowed = contract.SUPPORT_PLANE_ATOL_M + contract.SUPPORT_PLANE_RTOL * np.abs(offsets)
    candidates = np.nonzero(minima >= offsets - allowed)[0]
    if not len(candidates):
        raise PipelineError(
            f"support site {name!r}: no triangle plane lies below the whole contact geometry of link {link!r}"
        )
    clusters: list[dict] = []
    for index in candidates:
        normal, offset = normals[index], offsets[index]
        for cluster in clusters:
            if (
                np.max(np.abs(np.cross(cluster["normal"], normal))) <= contract.SUPPORT_MERGE_ANGLE_ATOL
                and abs(cluster["offset"] - offset)
                <= contract.SUPPORT_MERGE_OFFSET_ATOL_M + contract.SUPPORT_PLANE_RTOL * abs(cluster["offset"])
            ):
                break
        else:
            clusters.append({"normal": normal, "offset": offset})
    # The contact hull is the hull of every STL vertex of the selected link
    # within the plane tolerance, not only of the candidate triangles: a sharp
    # contact point elsewhere on the same plane belongs to the support region.
    contact_vertices = np.concatenate(
        [geometry["vertices"] for geometry in geometries if geometry["kind"] == "mesh"]
    )
    for cluster in clusters:
        normal = cluster["normal"]
        allowed = contract.SUPPORT_PLANE_ATOL_M + contract.SUPPORT_PLANE_RTOL * abs(cluster["offset"])
        distances = contact_vertices @ normal
        points = contact_vertices[np.abs(distances - cluster["offset"]) <= allowed]
        helper = np.eye(3)[int(np.argmin(np.abs(normal)))]
        first_axis = np.cross(normal, helper)
        first_axis = first_axis / np.linalg.norm(first_axis)
        second_axis = np.cross(normal, first_axis)
        centred = points - cluster["offset"] * normal
        coordinates = np.column_stack([centred @ first_axis, centred @ second_axis])
        hull = _convex_hull_2d(coordinates)
        area, centroid = _polygon_centroid(hull)
        cluster["area"] = area
        cluster["centroid"] = centroid
        cluster["basis"] = (first_axis, second_axis)
    ordered = sorted(clusters, key=lambda cluster: -cluster["area"])
    if ordered[0]["area"] <= contract.SUPPORT_HULL_EPS:
        raise PipelineError(f"support site {name!r}: the contact hull of link {link!r} is degenerate")
    if len(ordered) > 1 and ordered[0]["area"] - ordered[1]["area"] <= contract.SUPPORT_AREA_RTOL * ordered[0]["area"]:
        raise PipelineError(
            f"support site {name!r}: link {link!r} has several support planes with the same contact area"
        )
    chosen = ordered[0]
    normal = chosen["normal"]
    world_x = np.array([1.0, 0.0, 0.0])
    projection = world_x - float(world_x @ normal) * normal
    length = float(np.linalg.norm(projection))
    if length <= contract.SUPPORT_XAXIS_PARALLEL_ATOL:
        raise PipelineError(f"support site {name!r}: world X is parallel to the support plane of link {link!r}")
    x_axis = projection / length
    z_axis = normal
    y_axis = np.cross(z_axis, x_axis)
    basis_first, basis_second = chosen["basis"]
    origin = (
        chosen["offset"] * normal
        + chosen["centroid"][0] * basis_first
        + chosen["centroid"][1] * basis_second
    )
    return origin, np.column_stack([x_axis, y_axis, z_axis])


def _derive_support_sites(document: dict, config: dict, meshes: dict[str, np.ndarray]) -> dict:
    """Derived support frames per selected link, plus the supports-floor height."""

    if not config["support_sites"]:
        return {"sites": [], "height": None}
    source = config["collision"]["source"]
    if source == "none":
        raise PipelineError("support_sites require collision.source urdf or visual_convex")
    links = {link["name"]: link for link in document["links"]}
    poses = _zero_poses(document)
    derived: list[dict] = []
    height = math.inf
    for site in config["support_sites"]:
        link = links.get(site["link"])
        if link is None:
            raise PipelineError(f"Support site {site['name']!r} references unknown link {site['link']!r}")
        surfaces = (link["collisions"] if source == "urdf" or (source == "auto" and link["collisions"])
                    else link["visuals"])
        if not surfaces:
            raise PipelineError(
                f"support site {site['name']!r}: link {site['link']!r} has no {source} geometry"
            )
        geometries = [_surface_geometry(surface, poses[link["name"]], meshes) for surface in surfaces]
        for geometry in geometries:
            height = min(height, _minimum_height(geometry))
        origin, rotation = _support_site_frame(site["name"], link["name"], geometries)
        link_pose = poses[link["name"]]
        origin_local = link_pose[:3, :3].T @ (origin - link_pose[:3, 3])
        rotation_local = link_pose[:3, :3].T @ rotation
        derived.append(
            {
                "name": site["name"],
                "link": site["link"],
                "xyz": tuple(float(value) for value in origin_local),
                "quat": contract.quaternion_from_matrix(rotation_local),
            }
        )
    return {"sites": derived, "height": float(height) if config["floor"] and config["floor"]["supports"] else None}


def _principal_inertial(inertial: dict) -> tuple[tuple, tuple, float, tuple]:
    """Rotate the tensor into the link frame and diagonalise with the locked NumPy eigh."""

    rotation = np.array(contract.rpy_matrix(inertial["origin"]["rpy"]))
    ixx, ixy, ixz, iyy, iyz, izz = inertial["inertia"]
    tensor = np.array([[ixx, ixy, ixz], [ixy, iyy, iyz], [ixz, iyz, izz]])
    values, vectors = np.linalg.eigh(rotation @ tensor @ rotation.T)  # ascending moments
    for column in range(3):
        eigenvector = vectors[:, column]
        dominant = int(np.argmax(np.abs(eigenvector)))
        if eigenvector[dominant] < 0:
            vectors[:, column] = -eigenvector
    if np.linalg.det(vectors) < 0:
        vectors[:, 2] = -vectors[:, 2]
    import mujoco

    quat = np.zeros(4)
    mujoco.mju_mat2Quat(quat, vectors.reshape(9))
    quat = (float(quat[0]), float(quat[1]), float(quat[2]), float(quat[3]))
    if quat[0] < 0 or (quat[0] == 0 and next((item for item in quat[1:] if item != 0), 0.0) < 0):
        quat = (-quat[0], -quat[1], -quat[2], -quat[3])
    return inertial["origin"]["xyz"], quat, inertial["mass"], (float(values[0]), float(values[1]), float(values[2]))


class _MeshAssets:
    def __init__(self) -> None:
        self.keys: list[tuple[str, tuple]] = []
        self.by_key: dict[tuple[str, tuple], str] = {}

    def name_for(self, path: str, scale: tuple) -> str:
        key = (path, scale)
        if key not in self.by_key:
            self.by_key[key] = contract.mesh_asset_name(len(self.keys))
            self.keys.append(key)
        return self.by_key[key]


def _geometry_attributes(element: ET.Element, geometry: dict, assets: _MeshAssets) -> str:
    kind = geometry["kind"]
    if kind == "box":
        element.set("type", "box")
        element.set("size", _fmt3(value / 2 for value in geometry["size"]))
    elif kind == "sphere":
        element.set("type", "sphere")
        element.set("size", _fmt(geometry["radius"]))
    elif kind == "cylinder":
        element.set("type", "cylinder")
        element.set("size", f"{_fmt(geometry['radius'])} {_fmt(geometry['length'] / 2)}")
    else:
        element.set("type", "mesh")
        element.set("mesh", assets.name_for(geometry["path"], geometry["scale"]))
    return kind


def _add_geom(
    parent: ET.Element,
    *,
    name: str,
    surface: dict,
    role: str,
    assets: _MeshAssets,
    contact: tuple[int, int] | None,
    friction: tuple | None,
) -> None:
    element = ET.SubElement(parent, "geom")
    element.set("name", name)
    _geometry_attributes(element, surface["geometry"], assets)
    if surface["origin"]["xyz"] != (0.0, 0.0, 0.0):
        element.set("pos", _fmt3(surface["origin"]["xyz"]))
    quat = contract.quaternion_from_matrix(contract.rpy_matrix(surface["origin"]["rpy"]))
    if quat != (1.0, 0.0, 0.0, 0.0):
        element.set("quat", _fmt3(quat))
    if surface.get("color") is not None:
        element.set("rgba", _fmt3(surface["color"]))
    element.set("group", str(contract.ROLE_GROUPS[role]))
    if contact is None:
        element.set("contype", "0")
        element.set("conaffinity", "0")
    else:
        element.set("contype", str(contact[0]))
        element.set("conaffinity", str(contact[1]))
        element.set("condim", str(contract.CONTACT_CONDIM))
        element.set("friction", _fmt3(friction))


def _add_body(
    parent: ET.Element,
    link: dict,
    joints_by_parent: dict,
    config: dict,
    assets: _MeshAssets,
    derived_sites: dict,
    *,
    free: bool = False,
) -> ET.Element:
    body = ET.SubElement(parent, "body")
    body.set("name", link["name"])
    joint = joints_by_parent.get(link["name"])
    if joint is not None:
        origin = joint["origin"]
        if origin["xyz"] != (0.0, 0.0, 0.0):
            body.set("pos", _fmt3(origin["xyz"]))
        quat = contract.quaternion_from_matrix(contract.rpy_matrix(origin["rpy"]))
        if quat != (1.0, 0.0, 0.0, 0.0):
            body.set("quat", _fmt3(quat))
    if free:
        ET.SubElement(body, "freejoint")
    if joint is not None and joint["type"] != "fixed":
        element = ET.SubElement(body, "joint")
        element.set("name", joint["name"])
        element.set("type", "hinge" if joint["type"] in ("revolute", "continuous") else "slide")
        element.set("axis", _fmt3(joint["axis"]))
        if joint["type"] == "continuous":
            element.set("limited", "false")
        else:
            element.set("range", f"{_fmt(joint['limit']['lower'])} {_fmt(joint['limit']['upper'])}")
            element.set("limited", "true")
        if joint["dynamics"]["damping"] != 0:
            element.set("damping", _fmt(joint["dynamics"]["damping"]))
        if joint["dynamics"]["friction"] != 0:
            element.set("frictionloss", _fmt(joint["dynamics"]["friction"]))
    if link["inertial"] is not None:
        position, quat, mass, moments = _principal_inertial(link["inertial"])
        element = ET.SubElement(body, "inertial")
        element.set("pos", _fmt3(position))
        element.set("quat", _fmt3(quat))
        element.set("mass", _fmt(mass))
        element.set("diaginertia", _fmt3(moments))
    source = config["collision"]["source"]
    if source == "auto":
        source = "urdf" if link["collisions"] else "visual_convex"
    self_collision = config["collision"]["self_collision"]
    contact = contract.ROBOT_CONTACT if self_collision else contract.ROBOT_SOLO_CONTACT
    friction = config["collision"]["friction"]
    for index, visual in enumerate(link["visuals"]):
        _add_geom(
            body,
            name=contract.visual_geom_name(link["name"], index),
            surface=visual,
            role="visual",
            assets=assets,
            contact=None,
            friction=None,
        )
    for index, surface in enumerate(link["projection_collisions"]):
        name = (contract.collision_geom_name(link["name"], index) if source == "urdf"
                else contract.convex_geom_name(link["name"], index))
        _add_geom(body, name=name, surface=surface, role="collision", assets=assets,
                  contact=contact, friction=friction)
    for site in config["sites"]:
        if site["link"] != link["name"]:
            continue
        quat = contract.quaternion_from_matrix(contract.rpy_matrix(site["rpy"]))
        _add_site(body, site["name"], site["xyz"], quat)
    for site in derived_sites.get(link["name"], []):
        _add_site(body, site["name"], site["xyz"], site["quat"])
    return body


def _add_site(body: ET.Element, name: str, position, quat) -> None:
    element = ET.SubElement(body, "site")
    element.set("name", name)
    if tuple(position) != (0.0, 0.0, 0.0):
        element.set("pos", _fmt3(position))
    if tuple(quat) != (1.0, 0.0, 0.0, 0.0):
        element.set("quat", _fmt3(quat))


def _xml_bytes(root: ET.Element) -> bytes:
    ET.indent(root, space="  ")
    text = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{text}\n'.encode()


def _project_robot(document: dict, config: dict, derived_sites: list[dict], encoded_paths: dict) -> bytes:
    root = ET.Element("mujoco")
    root.set("model", document["name"])
    compiler = ET.SubElement(root, "compiler")
    compiler.set("angle", "radian")
    compiler.set("fusestatic", "false")
    compiler.set("inertiafromgeom", "false")
    option = ET.SubElement(root, "option")
    option.set("timestep", _fmt(config["simulation"]["timestep"]))
    option.set("gravity", _fmt3(config["simulation"]["gravity"]))
    option.set("integrator", config["simulation"]["integrator"])

    assets = _MeshAssets()
    for link in document["links"]:
        used = [*link["visuals"], *link["projection_collisions"]]
        for surface in used:
            geometry = surface["geometry"]
            if geometry["kind"] == "mesh":
                assets.name_for(geometry["path"], geometry["scale"])
    asset = ET.SubElement(root, "asset")
    for path, scale in assets.keys:
        mesh = ET.SubElement(asset, "mesh")
        mesh.set("name", assets.name_for(path, scale))
        mesh.set("file", f"{contract.ASSET_PREFIX}{encoded_paths.get(path, path)}")
        mesh.set("inertia", "shell")
        mesh.set("maxhullvert", str(config["collision"]["max_vertices"]))
        if tuple(scale) != contract.UNIT_SCALE:
            mesh.set("scale", _fmt3(scale))

    worldbody = ET.SubElement(root, "worldbody")
    joints_by_child = {joint["child"]: joint for joint in document["joints"]}
    links_by_name = {link["name"]: link for link in document["links"]}
    children: dict[str, list[str]] = {link["name"]: [] for link in document["links"]}
    for joint in document["joints"]:
        children[joint["parent"]].append(joint["child"])
    root_name = next(link["name"] for link in document["links"] if link["name"] not in joints_by_child)

    sites_by_link: dict[str, list[dict]] = {}
    for site in derived_sites:
        sites_by_link.setdefault(site["link"], []).append(site)

    def emit(parent: ET.Element, link: dict, *, free: bool = False) -> None:
        body = _add_body(parent, link, joints_by_child, config, assets, sites_by_link, free=free)
        for child_name in children[link["name"]]:
            emit(body, links_by_name[child_name])

    emit(worldbody, links_by_name[root_name], free=config["base"] == "floating")
    return _xml_bytes(root)


def _project_scene(config: dict, height: float) -> bytes:
    root = ET.Element("mujoco")
    include = ET.SubElement(root, "include")
    include.set("file", Path(contract.ROBOT_NAME).name)
    worldbody = ET.SubElement(root, "worldbody")
    element = ET.SubElement(worldbody, "geom")
    element.set("name", contract.FLOOR_GEOM_NAME)
    element.set("type", "plane")
    element.set("size", contract.FLOOR_RENDER_SIZE)
    element.set("pos", f"0 0 {_fmt(height)}")
    element.set("contype", str(contract.FLOOR_CONTACT[0]))
    element.set("conaffinity", str(contract.FLOOR_CONTACT[1]))
    element.set("condim", str(contract.CONTACT_CONDIM))
    element.set("friction", _fmt3(config["floor"]["friction"]))
    light = ET.SubElement(worldbody, "light")
    light.set("name", contract.LIGHT_NAME)
    light.set("pos", contract.LIGHT_POS)
    light.set("dir", contract.LIGHT_DIR)
    light.set("directional", "true")
    return _xml_bytes(root)


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def project(root: Path, simulation: bytes = contract.EMPTY_CONFIG) -> dict[str, str]:
    """Emit MJCF in a private Generate workspace, reusing its URDF and mesh bytes.

    The stage engine owns publication and failure preservation. This function
    writes no evidence, manifest, secondary robot model or stage decision.
    """
    root = Path(root)
    config = contract.load_simulation(simulation)
    source = confined(root, contract.URDF_NAME)
    urdf_bytes = source.read_bytes()
    document = _read_urdf(urdf_bytes, contract.URDF_NAME)
    _validate_mobility(document["links"], document["joints"], config["base"], contract.URDF_NAME)
    mesh_bytes, meshes = _resolve_meshes(document, root, contract.URDF_NAME)
    known = {link["name"] for link in document["links"]}
    for site in (*config["sites"], *config["support_sites"]):
        if site["link"] not in known:
            raise PipelineError(f"Simulation site {site['name']!r} references unknown link {site['link']!r}")
    metadata = (confined(root, contract.COMPONENT_SOURCE).read_bytes()
                if (root / contract.COMPONENT_SOURCE).exists() else None)
    _component_groups(document, metadata)
    proxies = _prepare_collisions(document, config, meshes)
    encoded, encoded_paths, encoding_warnings = _encode_meshes(document, mesh_bytes, meshes)
    derived = _derive_support_sites(document, config, meshes)
    robot_bytes = _project_robot(document, config, derived["sites"], encoded_paths)
    # Preserve the exact upstream warning comments in both delivered encodings.
    warnings = ET.fromstring(urdf_bytes, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    robot = ET.fromstring(robot_bytes)
    comments = [item for item in warnings.iter() if item.tag is ET.Comment]
    for comment in reversed(comments):
        robot.insert(0, comment)
    derived_warnings = [contract.SIMULATION_WARNING, contract.FORMAT_WARNING, *encoding_warnings]
    for link in document["links"]:
        source_role = config["collision"]["source"]
        if source_role == "auto":
            source_role = "urdf" if link["collisions"] else "visual_convex"
        surfaces = link["collisions"] if source_role == "urdf" else link["visuals"]
        indices = [index for index, surface in enumerate(surfaces) if surface["geometry"]["kind"] == "mesh"]
        if source_role != "none" and indices:
            derived_warnings.append(contract.collision_warning({
                "link": link["name"], "source_file": contract.URDF_NAME,
                "source_role": "collision" if source_role == "urdf" else "visual",
                "surface_indices": indices, "max_vertices": config["collision"]["max_vertices"],
                "max_error_m": config["collision"]["max_error_m"], "scope": contract.COLLISION_SCOPE,
            }))
    for index, warning in enumerate(derived_warnings, len(comments)):
        robot.insert(index, ET.Comment(warning))
    robot_bytes = _xml_bytes(robot)
    files = {contract.CONFIG_NAME: simulation, contract.ROBOT_NAME: robot_bytes, **encoded, **proxies}
    if config["floor"] is not None:
        height = derived["height"] if config["floor"]["supports"] else config["floor"]["height"]
        files[contract.SCENE_NAME] = _project_scene(config, height)
    destination = root / "mjcf"
    destination.mkdir(exist_ok=False)
    for name, content in files.items():
        target = confined(root, name, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    # Recheck actual source bytes before returning the stage candidate.
    for name, content in {contract.URDF_NAME: urdf_bytes, **mesh_bytes}.items():
        if confined(root, name).read_bytes() != content:
            raise PipelineError(f"The source {name} changed during MJCF projection")
    if metadata is not None and confined(root, contract.COMPONENT_SOURCE).read_bytes() != metadata:
        raise PipelineError("Component provenance changed during MJCF projection")
    return {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}


def project_isolated(root: Path, simulation: bytes = contract.EMPTY_CONFIG) -> dict[str, str]:
    """Run native hull construction in one bounded process over a private view."""
    from .io import inventory

    try:
        result = subprocess.run(
            [sys.executable, "-m", "description_pipeline.mjcf", str(Path(root).resolve())],
            input=simulation, capture_output=True, timeout=contract.VERIFIER_TIMEOUT_SECONDS,
            env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"},
        )
    except subprocess.TimeoutExpired as error:
        raise PipelineError("The MJCF projection process timed out") from error
    if result.returncode != 0:
        raise PipelineError(f"The MJCF projection process exited with status {result.returncode}: "
                            f"{result.stderr.decode('utf-8', errors='replace')[-2000:]}")
    try:
        outputs = json.loads(result.stdout)
    except ValueError as error:
        raise PipelineError("The MJCF projection process returned invalid JSON") from error
    actual = {"mjcf/" + name: sha for name, sha in inventory(root / "mjcf").items()}
    if not isinstance(outputs, dict) or not outputs or actual != outputs:
        raise PipelineError("The MJCF projection process output differs from the written files")
    return outputs


def _projection_main() -> int:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (contract.VERIFIER_CPU_SECONDS, contract.VERIFIER_CPU_SECONDS + 5))
    resource.setrlimit(resource.RLIMIT_AS, (contract.VERIFIER_MEMORY_BYTES, contract.VERIFIER_MEMORY_BYTES))
    try:
        outputs = project(Path(sys.argv[1]), sys.stdin.buffer.read())
    except Exception as error:  # noqa: BLE001 (the parent preserves process failure)
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(outputs, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_projection_main())
