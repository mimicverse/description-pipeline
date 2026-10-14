"""Project the canonical model into URDF and its local mesh assets."""

from __future__ import annotations

import shutil
import struct
from pathlib import Path
from xml.etree import ElementTree as ET

from ..io import PipelineError, confined, format_number
from ..geometry.stl import MESH_PART_TRIANGLES_MAX
from ..model import Robot

#: Consumers reject a single display mesh above this many triangles, so larger
#: native meshes are delivered as deterministic, lossless parts.
MESH_PART_TRIANGLE_LIMIT = MESH_PART_TRIANGLES_MAX
_STL_HEADER_BYTES = 80
_STL_PREFIX_BYTES = 84
_STL_TRIANGLE_BYTES = 50


def _numbers(values) -> str:
    return " ".join(format_number(value) for value in values)


def _origin(parent: ET.Element, data: dict) -> None:
    ET.SubElement(parent, "origin", xyz=_numbers(data["xyz"]), rpy=_numbers(data["rpy"]))


def _binary_stl_triangle_count(original: Path, raw: bytes) -> int:
    """Validate an exact binary STL payload and return its declared triangle count."""
    if len(raw) >= _STL_PREFIX_BYTES:
        count = struct.unpack_from("<I", raw, 80)[0]
        if len(raw) == _STL_PREFIX_BYTES + count * _STL_TRIANGLE_BYTES:
            return count
    raise PipelineError(f"Native STL mesh is not an exact binary STL: {original.name}")


def _export_mesh(original: Path, destination: Path, relative: str) -> list[str]:
    """Deliver one native STL, partitioning only when it exceeds the consumer limit.

    Every part repeats the native 80-byte header, declares its own triangle count
    and copies whole native triangle records in their original order, so the
    concatenated part payloads reproduce the source records byte for byte.
    """
    raw = original.read_bytes()
    count = _binary_stl_triangle_count(original, raw)
    target = confined(destination, relative, exists=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    if count <= MESH_PART_TRIANGLE_LIMIT:
        shutil.copyfile(original, target)
        return [relative]
    stem = relative[: -len(".stl")]
    payload = raw[_STL_PREFIX_BYTES:]
    names = []
    for index, start in enumerate(range(0, count, MESH_PART_TRIANGLE_LIMIT)):
        end = min(start + MESH_PART_TRIANGLE_LIMIT, count)
        name = f"{stem}_part_{index}.stl"
        part = confined(destination, name, exists=False)
        part.write_bytes(
            raw[:_STL_HEADER_BYTES]
            + struct.pack("<I", end - start)
            + payload[start * _STL_TRIANGLE_BYTES : end * _STL_TRIANGLE_BYTES]
        )
        names.append(name)
    return names


def generate_urdf(robot: Robot, source: Path, destination: Path, *, name: str) -> Path:
    """Generate only the URDF delivery; no consumer-specific conversions.

    Input model data is copied by ``Robot.to_dict``. Original evidence meshes
    remain untouched. Geometry and inertia use their explicitly defined link
    frames; this writer does not repair names, axes, units or physical values.
    A mesh above ``MESH_PART_TRIANGLE_LIMIT`` is delivered as lossless parts
    with one identically placed visual or collision element per part.
    """

    data = robot.to_dict()
    document = ET.Element("robot", name=name)
    for link in data["links"]:
        node = ET.SubElement(document, "link", name=link["name"])
        inertia = link["inertial"]
        if inertia is not None:
            inertial = ET.SubElement(node, "inertial")
            _origin(inertial, inertia)
            ET.SubElement(inertial, "mass", value=_numbers([inertia["mass"]]))
            ET.SubElement(
                inertial,
                "inertia",
                attrib={
                    key: _numbers([value])
                    for key, value in zip(("ixx", "ixy", "ixz", "iyy", "iyz", "izz"), inertia["inertia"], strict=True)
                },
            )
        for role in ("visuals", "collisions"):
            tag = "visual" if role == "visuals" else "collision"
            for index, shape in enumerate(link[role]):
                kind = shape["kind"]
                if kind == "mesh":
                    original = confined(source, shape["filename"])
                    if original.suffix.lower() != ".stl":
                        raise PipelineError("SolidWorks-to-URDF v1 supports native STL meshes")
                    meshes = _export_mesh(original, destination, f"meshes/{tag}/{link['name']}_{index}.stl")
                else:
                    meshes = [None]
                for relative in meshes:
                    geometry = ET.SubElement(node, tag)
                    _origin(geometry, shape)
                    element = ET.SubElement(geometry, "geometry")
                    if relative is not None:
                        ET.SubElement(element, "mesh", filename="../" + relative, scale=_numbers(shape["scale"]))
                    elif kind == "box":
                        ET.SubElement(element, kind, size=_numbers(shape["size"]))
                    elif kind == "sphere":
                        ET.SubElement(element, kind, radius=_numbers([shape["radius"]]))
                    elif kind == "cylinder":
                        ET.SubElement(
                            element, kind, radius=_numbers([shape["radius"]]), length=_numbers([shape["length"]])
                        )
                    else:
                        raise PipelineError(f"Unsupported URDF geometry: {kind}")
                    if tag == "visual" and "rgba" in shape:
                        material = ET.SubElement(geometry, "material", name=f"{link['name']}_color_{index}")
                        ET.SubElement(material, "color", rgba=_numbers(shape["rgba"]))
    for joint in data["joints"]:
        node = ET.SubElement(document, "joint", name=joint["name"], type=joint["type"])
        ET.SubElement(node, "parent", link=joint["parent"])
        ET.SubElement(node, "child", link=joint["child"])
        _origin(node, joint)
        if joint["type"] != "fixed":
            ET.SubElement(node, "axis", xyz=_numbers(joint["axis"]))
        if "limits" in joint:
            ET.SubElement(node, "limit", attrib={key: _numbers([value]) for key, value in joint["limits"].items()})
        if joint.get("dynamics"):
            ET.SubElement(node, "dynamics", attrib={key: _numbers([value]) for key, value in joint["dynamics"].items()})
        if joint.get("mimic"):
            mimic = joint["mimic"]
            ET.SubElement(
                node,
                "mimic",
                joint=mimic["joint"],
                multiplier=_numbers([mimic["multiplier"]]),
                offset=_numbers([mimic["offset"]]),
            )
    for frame in data["frames"]:
        ET.SubElement(document, "link", name=frame["name"])
        node = ET.SubElement(document, "joint", name=frame["name"] + "_fixed", type="fixed")
        ET.SubElement(node, "parent", link=frame["parent"])
        ET.SubElement(node, "child", link=frame["name"])
        _origin(node, frame)
    output = destination / "urdf/robot.urdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    (destination / "meshes").mkdir(exist_ok=True)
    ET.indent(document)
    output.write_bytes(ET.tostring(document, encoding="utf-8", xml_declaration=True) + b"\n")
    return output
