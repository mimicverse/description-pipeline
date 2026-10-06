"""Project the canonical model into URDF and its local mesh assets."""

from __future__ import annotations

import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

from ..io import PipelineError, confined
from ..model import Robot


def _numbers(values) -> str:
    return " ".join(format(float(value), ".17g") for value in values)


def _origin(parent: ET.Element, data: dict) -> None:
    ET.SubElement(parent, "origin", xyz=_numbers(data["xyz"]), rpy=_numbers(data["rpy"]))


def generate_urdf(robot: Robot, source: Path, destination: Path, *, name: str) -> Path:
    """Generate only the URDF delivery; no consumer-specific conversions.

    Input model data is copied by ``Robot.to_dict``. Original evidence meshes
    remain untouched. Geometry and inertia use their explicitly defined link
    frames; this writer does not repair names, axes, units or physical values.
    """

    data = robot.to_dict()
    if data["constraints"]:
        raise PipelineError("URDF v1 does not support closed-loop constraints")
    if data.get("actuators") or data.get("sensors"):
        raise PipelineError("URDF v1 requires explicit extensions for actuator or sensor definitions")
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
                    for key, value in zip(
                        ("ixx", "ixy", "ixz", "iyy", "iyz", "izz"), inertia["inertia"], strict=True
                    )
                },
            )
        for role in ("visuals", "collisions"):
            tag = "visual" if role == "visuals" else "collision"
            for index, shape in enumerate(link[role]):
                geometry = ET.SubElement(node, tag)
                _origin(geometry, shape)
                element = ET.SubElement(geometry, "geometry")
                kind = shape["kind"]
                if kind == "mesh":
                    original = confined(source, shape["filename"])
                    if original.suffix.lower() != ".stl":
                        raise PipelineError("SolidWorks-to-URDF v1 supports native STL meshes")
                    relative = f"meshes/{tag}/{link['name']}_{index}.stl"
                    target = confined(destination, relative, exists=False)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, target)
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
            ET.SubElement(
                node, "dynamics", attrib={key: _numbers([value]) for key, value in joint["dynamics"].items()}
            )
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
