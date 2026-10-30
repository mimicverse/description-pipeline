"""Strictly bind actual URDF names, definitions, references, and materials.

This reader treats the delivered XML as an untrusted encoding. It compares that
encoding with the canonical model claim while the rest of verification proves
the claim against sealed native evidence. The current emitter uses inline
materials only; global material definitions and textures fail closed until they
have an explicit model and provenance contract.
"""

from __future__ import annotations

import hashlib
import math
import posixpath
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from ..io import PipelineError, canonical_json_value, confined
from ..sources.solidworks.naming import fixed_joint_name

_NAME = re.compile(r"\A[a-z][a-z0-9_]*\Z")
_LINK_CHILDREN = frozenset({"inertial", "visual", "collision"})
_GEOMETRY_CHILDREN = frozenset({"mesh", "box", "sphere", "cylinder"})
_MATERIAL_CHILDREN = frozenset({"color", "texture"})
_JOINT_CHILDREN = frozenset({"parent", "child", "origin", "axis", "limit", "dynamics", "mimic"})


def _require(condition, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _attributes(element: ET.Element, *, required=(), optional=(), label: str) -> None:
    keys = set(element.attrib)
    required = set(required)
    allowed = required | set(optional)
    _require(required <= keys, f"{label} lacks required attributes: {sorted(required - keys)}")
    _require(keys <= allowed, f"{label} carries unknown attributes: {sorted(keys - allowed)}")


def _child(parent: ET.Element, tag: str, *, required: bool = True, label: str) -> ET.Element | None:
    nodes = parent.findall(tag)
    _require(len(nodes) <= 1, f"{label} repeats {tag}")
    if required:
        _require(len(nodes) == 1, f"{label} lacks {tag}")
        return nodes[0]
    return nodes[0] if nodes else None


def _leaf(element: ET.Element, label: str) -> None:
    _require(len(element) == 0 and not (element.text or "").strip(), f"{label} must be an empty element")


def _numbers(value, count: int, label: str) -> list[float]:
    _require(isinstance(value, str), f"{label} must be a whitespace list")
    fields = value.split()
    _require(len(fields) == count, f"{label} must contain {count} numbers")
    try:
        numbers = [float(field) for field in fields]
    except ValueError as error:
        raise PipelineError(f"{label} contains a non-number") from error
    _require(all(math.isfinite(number) for number in numbers), f"{label} contains a non-finite number")
    return numbers


def _same_numbers(actual, expected, label: str, *, tolerance: float = 1e-12) -> None:
    _require(
        isinstance(expected, (list, tuple))
        and len(expected) == len(actual)
        and all(type(value) in (int, float) and math.isfinite(value) for value in expected),
        f"{label} has no finite canonical value",
    )
    _require(
        all(abs(left - right) <= tolerance for left, right in zip(actual, expected, strict=True)),
        f"{label} differs from the canonical model",
    )


def _name(value, label: str) -> str:
    _require(isinstance(value, str) and bool(_NAME.fullmatch(value)), f"{label} is not snake_case")
    return value


def _shape_name(shape: dict, label: str) -> str:
    return _name(shape.get("name"), label)


def _validate_origin(parent: ET.Element, expected: dict, label: str) -> None:
    origin = _child(parent, "origin", label=label)
    _attributes(origin, required={"xyz", "rpy"}, label=f"{label}.origin")
    _leaf(origin, f"{label}.origin")
    _same_numbers(_numbers(origin.get("xyz"), 3, f"{label}.origin.xyz"), expected.get("xyz"), f"{label}.xyz")
    _same_numbers(_numbers(origin.get("rpy"), 3, f"{label}.origin.rpy"), expected.get("rpy"), f"{label}.rpy")


def _mesh_path(root: Path, value: str) -> str:
    _require(isinstance(value, str) and value.startswith("../meshes/"), "Mesh URI must reference delivered meshes")
    relative = value.removeprefix("../meshes/")
    normalized = posixpath.normpath(relative)
    parts = normalized.split("/")
    _require(
        bool(relative)
        and relative == normalized
        and not value.startswith("/")
        and "\\" not in value
        and ":" not in value
        and "?" not in value
        and "#" not in value
        and all(part not in {"", ".", ".."} for part in parts)
        and normalized.lower().endswith(".stl"),
        "Mesh URI is not a confined STL member path",
    )
    path = confined(root, "meshes/" + normalized)
    _require(
        path.is_file() and not path.is_symlink() and not path.is_junction(),
        "Mesh URI is not a regular delivery file",
    )
    return "meshes/" + normalized


def _validate_geometry(root: Path, visual: ET.Element, shape: dict, label: str) -> str | None:
    geometry = _child(visual, "geometry", label=label)
    nodes = [node for node in geometry if not isinstance(node.tag, str) or not str(node.tag).startswith("!")]
    _require(len(nodes) == 1, f"{label}.geometry must contain exactly one geometry element")
    node = nodes[0]
    _require(node.tag in _GEOMETRY_CHILDREN, f"{label}.geometry uses an unknown shape: {node.tag!r}")
    _require(node.tag == shape.get("kind"), f"{label}.geometry kind differs from the canonical model")
    _leaf(node, f"{label}.geometry.{node.tag}")
    mesh: str | None = None
    if node.tag == "mesh":
        _attributes(node, required={"filename", "scale"}, label=f"{label}.mesh")
        _same_numbers(_numbers(node.get("scale"), 3, f"{label}.mesh.scale"), shape.get("scale"), f"{label}.scale")
        mesh = _mesh_path(root, node.get("filename"))
    elif node.tag == "box":
        _attributes(node, required={"size"}, label=f"{label}.box")
        _same_numbers(_numbers(node.get("size"), 3, f"{label}.box.size"), shape.get("size"), f"{label}.size")
    elif node.tag == "sphere":
        _attributes(node, required={"radius"}, label=f"{label}.sphere")
        _same_numbers(
            _numbers(node.get("radius"), 1, f"{label}.sphere.radius"),
            [shape.get("radius")],
            f"{label}.radius",
        )
    else:
        _attributes(node, required={"radius", "length"}, label=f"{label}.cylinder")
        _same_numbers(
            _numbers(node.get("radius"), 1, f"{label}.cylinder.radius"),
            [shape.get("radius")],
            f"{label}.radius",
        )
        _same_numbers(
            _numbers(node.get("length"), 1, f"{label}.cylinder.length"),
            [shape.get("length")],
            f"{label}.length",
        )
    return mesh


def _validate_material(visual: ET.Element, shape: dict, label: str) -> str | None:
    material = _child(visual, "material", required=False, label=label)
    rgba = shape.get("rgba")
    if rgba is None:
        _require(material is None, f"{label} carries an undeclared material")
        return None
    _require(material is not None, f"{label} lacks its canonical material")
    _attributes(material, required={"name"}, label=f"{label}.material")
    expected_name = _name(shape.get("material"), f"{label}.material model name")
    _require(material.get("name") == expected_name, f"{label}.material name differs from the canonical model")
    children = list(material)
    _require(
        all(isinstance(child.tag, str) and child.tag in _MATERIAL_CHILDREN for child in children),
        f"{label}.material carries an unknown appearance element",
    )
    _require(
        [child.tag for child in children] == ["color"],
        f"{label}.material must use exactly one color; texture is not qualified",
    )
    color = children[0]
    _attributes(color, required={"rgba"}, label=f"{label}.material.color")
    _leaf(color, f"{label}.material.color")
    actual_rgba = _numbers(color.get("rgba"), 4, f"{label}.material.color.rgba")
    _require(all(0.0 <= value <= 1.0 for value in actual_rgba), f"{label}.material.rgba is outside [0, 1]")
    _same_numbers(actual_rgba, rgba, f"{label}.material.rgba")
    return expected_name


def _validate_shape_group(
    root: Path,
    nodes: list[ET.Element],
    shape: dict,
    *,
    role: str,
    label: str,
) -> tuple[str | None, str | None]:
    _require(bool(nodes), f"{label} has no delivered {role}")
    meshes = []
    materials = set()
    for index, node in enumerate(nodes):
        item = f"{label}.{role}[{index}]"
        _attributes(node, required={"name"}, label=item)
        _require(node.get("name") == _shape_name(shape, f"{item}.model name"), f"{item} name differs from the model")
        allowed = {"origin", "geometry", "material"} if role == "visual" else {"origin", "geometry"}
        _require(
            all(isinstance(child.tag, str) and child.tag in allowed for child in node),
            f"{item} carries an unknown child element",
        )
        _validate_origin(node, shape, item)
        mesh = _validate_geometry(root, node, shape, item)
        if mesh is not None:
            meshes.append(mesh)
        if role == "visual":
            material = _validate_material(node, shape, item)
            materials.add(material)
        else:
            _require(node.find("material") is None, f"{item} collision carries a material")
    _require(len(materials) <= 1, f"{label}.{role} chunks carry different materials")
    return (meshes[0] if len(set(meshes)) == 1 else None), next(iter(materials), None)


def _validate_shapes(root: Path, node: ET.Element, model_link: dict, label: str) -> dict:
    result = {"visuals": 0, "collisions": 0, "materials": 0}
    for role, key in (("visual", "visuals"), ("collision", "collisions")):
        model_shapes = model_link.get(key) or []
        _require(isinstance(model_shapes, list), f"{label}.{key} must be a model list")
        expected = {}
        for index, shape in enumerate(model_shapes):
            _require(isinstance(shape, dict), f"{label}.{key}[{index}] must be an object")
            name = _shape_name(shape, f"{label}.{key}[{index}].name")
            _require(name not in expected, f"{label}.{key} repeats a canonical shape name: {name}")
            expected[name] = shape
        actual = node.findall(role)
        for item in actual:
            name = _name(item.get("name"), f"{label}.{role}.name")
            _require(name in expected, f"{label}.{role} {name!r} has no canonical model definition")
        actual_names = {item.get("name") for item in actual}
        _require(actual_names == set(expected), f"{label}.{key} definitions differ from the canonical model")
        for name, shape in expected.items():
            group = [item for item in actual if item.get("name") == name]
            _, material = _validate_shape_group(
                root,
                group,
                shape,
                role=role,
                label=label,
            )
            if material is not None:
                result["materials"] += 1
        result[key] = len(actual)
    return result


def _validate_inertial(link: ET.Element, model_link: dict, label: str) -> None:
    inertial = _child(link, "inertial", required=False, label=label)
    if model_link.get("inertial") is None:
        _require(inertial is None, f"{label} carries an undeclared inertial")
        return
    _require(inertial is not None, f"{label} lacks its canonical inertial")
    _require(
        all(isinstance(child.tag, str) and child.tag in {"origin", "mass", "inertia"} for child in inertial),
        f"{label}.inertial carries an unknown child element",
    )
    origin = _child(inertial, "origin", label=f"{label}.inertial")
    _attributes(origin, required={"xyz", "rpy"}, label=f"{label}.inertial.origin")
    _leaf(origin, f"{label}.inertial.origin")
    for tag, attrs in (
        ("mass", {"value"}),
        ("inertia", {"ixx", "ixy", "ixz", "iyy", "iyz", "izz"}),
    ):
        node = _child(inertial, tag, label=f"{label}.inertial")
        _attributes(node, required=attrs, label=f"{label}.inertial.{tag}")
        _leaf(node, f"{label}.inertial.{tag}")


def _validate_joint_children(node: ET.Element, joint: dict, label: str, link_names: set[str]) -> None:
    _require(
        all(isinstance(child.tag, str) and child.tag in _JOINT_CHILDREN for child in node),
        f"{label} carries an unknown child element",
    )
    parent = _child(node, "parent", label=label)
    child = _child(node, "child", label=label)
    for end, expected in (("parent", joint["parent"]), ("child", joint["child"])):
        endpoint = parent if end == "parent" else child
        _attributes(endpoint, required={"link"}, label=f"{label}.{end}")
        _leaf(endpoint, f"{label}.{end}")
        _require(endpoint.get("link") in link_names, f"{label}.{end} references an undefined link")
        _require(endpoint.get("link") == expected, f"{label}.{end} differs from the canonical model")
    _validate_origin(node, joint, label)
    if joint["type"] == "fixed":
        _require(node.find("axis") is None, f"{label} fixed joint carries an axis")
    else:
        axis = _child(node, "axis", label=label)
        _attributes(axis, required={"xyz"}, label=f"{label}.axis")
        _leaf(axis, f"{label}.axis")
        _same_numbers(_numbers(axis.get("xyz"), 3, f"{label}.axis.xyz"), joint.get("axis"), f"{label}.axis")

    expected_limits = joint.get("limits") if "limits" in joint else None
    limit = _child(node, "limit", required=expected_limits is not None, label=label)
    if expected_limits is None:
        _require(limit is None, f"{label} carries undeclared limits")
    else:
        _attributes(limit, required=set(expected_limits), label=f"{label}.limit")
        _leaf(limit, f"{label}.limit")
        for key, value in expected_limits.items():
            _same_numbers(_numbers(limit.get(key), 1, f"{label}.limit.{key}"), [value], f"{label}.limit.{key}")

    expected_dynamics = joint.get("dynamics")
    dynamics = _child(node, "dynamics", required=expected_dynamics is not None, label=label)
    if expected_dynamics is None:
        _require(dynamics is None, f"{label} carries undeclared dynamics")
    else:
        _attributes(dynamics, required=set(expected_dynamics), label=f"{label}.dynamics")
        _leaf(dynamics, f"{label}.dynamics")
        for key, value in expected_dynamics.items():
            _same_numbers(
                _numbers(dynamics.get(key), 1, f"{label}.dynamics.{key}"),
                [value],
                f"{label}.dynamics.{key}",
            )

    expected_mimic = joint.get("mimic")
    mimic = _child(node, "mimic", required=expected_mimic is not None, label=label)
    if expected_mimic is None:
        _require(mimic is None, f"{label} carries undeclared mimic")
    else:
        _attributes(
            mimic,
            required={"joint", "multiplier", "offset"},
            label=f"{label}.mimic",
        )
        _leaf(mimic, f"{label}.mimic")
        _name(mimic.get("joint"), f"{label}.mimic.joint")
        _require(mimic.get("joint") == expected_mimic.get("joint"), f"{label}.mimic joint differs from the model")
        for key in ("multiplier", "offset"):
            _same_numbers(
                _numbers(mimic.get(key), 1, f"{label}.mimic.{key}"),
                [expected_mimic.get(key)],
                f"{label}.mimic.{key}",
            )


def _warning_comments(model: dict) -> list[str]:
    expected = []
    for item in (model.get("provenance") or {}).get("value_warnings") or []:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        objects = item.get("objects")
        for subject in objects if isinstance(objects, list) and objects else [{}]:
            detail = {"message": item.get("message", "")}
            if item.get("detail") is not None:
                detail["detail"] = item["detail"]
            detail["provenance"] = subject
            payload = canonical_json_value(detail, ensure_ascii=True).replace("--", r"\u002d\u002d")
            code = str(item["code"]).replace("--", "- -")
            expected.append(f"WARNING {code}: {payload} See reports/verification-certificate.json.")
    return expected


def _validate_document(document: ET.Element, root: Path, definition: dict, model: dict) -> dict:
    robot_name = (definition.get("source") or {}).get("robot_name")
    _require(isinstance(robot_name, str) and bool(robot_name), "The input definition carries no robot label")
    _require(document.tag == "robot", "The URDF root is not robot")
    _attributes(document, required={"name"}, label="urdf.robot")
    _require(document.get("name") == robot_name == model.get("name"), "URDF robot identity differs from the model")
    expected_comments = _warning_comments(model)
    comments = [child for child in document if child.tag is ET.Comment]
    _require(
        [child.text for child in comments] == expected_comments,
        "URDF value-provenance warnings differ from the model",
    )
    top = [child for child in document if child.tag is not ET.Comment]
    _require(
        all(isinstance(child.tag, str) and child.tag in {"link", "joint"} for child in top),
        "Unknown top-level URDF definition",
    )

    model_links = {link["name"]: link for link in model.get("links") or []}
    model_frames = {frame["name"]: frame for frame in model.get("frames") or []}
    _require(set(model_links).isdisjoint(model_frames), "Canonical links and frames repeat a name")
    for role in ("visuals", "collisions"):
        shape_names = [
            shape.get("name")
            for link in model_links.values()
            for shape in link.get(role) or []
        ]
        _require(len(shape_names) == len(set(shape_names)), f"Canonical {role} repeat a name across links")
    expected_links = set(model_links) | set(model_frames)
    actual_links = document.findall("link")
    link_names = [item.get("name") for item in actual_links]
    _require(len(link_names) == len(set(link_names)), "URDF repeats a link name")
    _require(all(_NAME.fullmatch(name or "") for name in link_names), "URDF link names must be snake_case")
    _require(set(link_names) == expected_links, "URDF link definitions differ from the canonical model")
    links = {item.get("name"): item for item in actual_links}

    counts = {"links": len(actual_links), "visuals": 0, "collisions": 0, "materials": 0}
    for name, model_link in model_links.items():
        node = links[name]
        _attributes(node, required={"name"}, label=f"link[{name}]")
        _require(
            all(isinstance(child.tag, str) and child.tag in _LINK_CHILDREN for child in node),
            f"link[{name}] carries an unknown child element",
        )
        _validate_inertial(node, model_link, f"link[{name}]")
        shape_counts = _validate_shapes(root, node, model_link, f"link[{name}]")
        for key in ("visuals", "collisions", "materials"):
            counts[key] += shape_counts[key]
    for name in model_frames:
        _attributes(links[name], required={"name"}, label=f"frame[{name}]")
        _require(len(links[name]) == 0, f"frame[{name}] must be an empty link")

    model_joints = {joint["name"]: joint for joint in model.get("joints") or []}
    frame_joints = {
        fixed_joint_name(str(frame["parent"]), str(frame["name"])): frame
        for frame in model_frames.values()
    }
    _require(set(model_joints).isdisjoint(frame_joints), "Canonical and frame joints repeat a name")
    _require(set(model_joints).isdisjoint(links), "A joint name repeats a URDF link name")
    _require(set(frame_joints).isdisjoint(links), "A frame joint name repeats a URDF link name")
    expected_joints = set(model_joints) | set(frame_joints)
    actual_joints = document.findall("joint")
    joint_names = [item.get("name") for item in actual_joints]
    _require(len(joint_names) == len(set(joint_names)), "URDF repeats a joint name")
    _require(all(_NAME.fullmatch(name or "") for name in joint_names), "URDF joint names must be snake_case")
    _require(set(joint_names) == expected_joints, "URDF joint definitions differ from the canonical model")
    joints = {item.get("name"): item for item in actual_joints}
    for name, joint in model_joints.items():
        node = joints[name]
        _attributes(node, required={"name", "type"}, label=f"joint[{name}]")
        _require(node.get("type") == joint.get("type"), f"joint[{name}] type differs from the canonical model")
        _validate_joint_children(node, joint, f"joint[{name}]", set(link_names))
    for name, frame in frame_joints.items():
        node = joints[name]
        _attributes(node, required={"name", "type"}, label=f"joint[{name}]")
        _require(node.get("type") == "fixed", f"frame joint {name} is not fixed")
        expected = {
            "parent": frame["parent"],
            "child": frame["name"],
            "type": "fixed",
            "xyz": frame["xyz"],
            "rpy": frame["rpy"],
        }
        _validate_joint_children(node, expected, f"joint[{name}]", set(link_names))
    mimic_targets = {}
    for name, node in joints.items():
        mimic = node.find("mimic")
        if mimic is None:
            continue
        target = mimic.get("joint")
        _require(target in joints, f"joint[{name}] mimic references an undefined joint")
        _require(target != name, f"joint[{name}] mimic references itself")
        mimic_targets[name] = target
    for start in mimic_targets:
        seen = set()
        cursor = start
        while cursor in mimic_targets:
            _require(cursor not in seen, "URDF mimic references form a cycle")
            seen.add(cursor)
            cursor = mimic_targets[cursor]
    counts["joints"] = len(actual_joints)
    return counts


def read_and_validate_actual_urdf(root, definition: dict, model: dict) -> tuple[ET.Element, dict]:
    """Read, validate, and re-read the exact actual URDF before passing the gate."""

    root = Path(root)
    path = confined(root, "urdf/robot.urdf")
    _require(
        path.is_file() and not path.is_symlink() and not path.is_junction(),
        "URDF must be a regular delivery file",
    )
    payload = path.read_bytes()
    _require(
        b"<!DOCTYPE" not in payload.upper() and b"<!ENTITY" not in payload.upper(),
        "XML declarations are forbidden",
    )
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        document = ET.fromstring(payload, parser=parser)
    except ET.ParseError as error:
        raise PipelineError(f"URDF is not parseable XML: {error}") from error
    details = _validate_document(document, root, definition, model)
    _require(path.read_bytes() == payload, "URDF changed while actual definitions were binding")
    return document, {**details, "xml_sha256": hashlib.sha256(payload).hexdigest()}
