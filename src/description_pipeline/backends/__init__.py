"""URDF and MJCF projections of the same canonical model."""

from __future__ import annotations

import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

from ..io import PipelineError, confined, write_json
from ..model import Robot, rotation, tensor


def numbers(values) -> str:
    return " ".join(format(float(value), ".17g") for value in values)


def origin(parent: ET.Element, data: dict) -> None:
    ET.SubElement(parent, "origin", xyz=numbers(data["xyz"]), rpy=numbers(data["rpy"]))


def write_xml(path: Path, element: ET.Element) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(element)
    # ``ElementTree.write`` opens the target in text mode and therefore translates "\n" to "\r\n" on
    # Windows: the same model then delivered different bytes per platform, which broke the
    # content-addressed identity the rest of the pipeline relies on.  Serialize in memory and write
    # the bytes ourselves so every delivered XML stays LF, like the JSON and mesh paths.
    path.write_bytes(ET.tostring(element, encoding="utf-8", xml_declaration=True))


def generate(robot: Robot, source: Path, destination: Path, profile: dict) -> dict:
    data = robot.to_dict()
    (destination / "meshes").mkdir(parents=True, exist_ok=True)
    if data["constraints"]:
        raise PipelineError("Explicit closed-loop/other constraints are preserved but unsupported by these backends")
    mapping: dict = {"links": {}, "joints": {}, "frames": {}, "actuators": {}, "sensors": {}}
    assets = {}
    for link in data["links"]:
        mapping["links"][link["id"]] = link["name"]
        for role in ("visuals", "collisions"):
            for index, geom in enumerate(link[role]):
                if geom["kind"] != "mesh":
                    continue
                original = confined(source, geom["filename"])
                if original.suffix.lower() not in {".stl", ".obj"}:
                    raise PipelineError(f"Unsupported mesh format: {original.suffix}")
                folder = "visual" if role == "visuals" else "collision"
                relative = f"meshes/{folder}/{link['name']}_{index}{original.suffix.lower()}"
                target = confined(destination, relative, exists=False)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
                geom["filename"] = relative
                assets[(link["name"], role, index)] = f"{link['name']}_{role}_{index}"
    urdf = ET.Element("robot", name="robot")
    for link in data["links"]:
        node = ET.SubElement(urdf, "link", name=link["name"])
        inertia = link["inertial"]
        if inertia:
            inertial = ET.SubElement(node, "inertial")
            origin(inertial, inertia)
            ET.SubElement(inertial, "mass", value=numbers([inertia["mass"]]))
            ET.SubElement(
                inertial,
                "inertia",
                attrib=dict(
                    zip(
                        ["ixx", "ixy", "ixz", "iyy", "iyz", "izz"],
                        [numbers([value]) for value in inertia["inertia"]],
                        strict=True,
                    )
                ),
            )
        for role in ("visuals", "collisions"):
            for geom in link[role]:
                shape = ET.SubElement(node, "visual" if role == "visuals" else "collision")
                origin(shape, geom)
                geometry = ET.SubElement(shape, "geometry")
                kind = geom["kind"]
                if kind == "mesh":
                    ET.SubElement(geometry, kind, filename="../" + geom["filename"], scale=numbers(geom["scale"]))
                elif kind == "box":
                    ET.SubElement(geometry, kind, size=numbers(geom["size"]))
                elif kind == "sphere":
                    ET.SubElement(geometry, kind, radius=numbers([geom["radius"]]))
                else:
                    ET.SubElement(geometry, kind, radius=numbers([geom["radius"]]), length=numbers([geom["length"]]))
                if role == "visuals" and "rgba" in geom:
                    mat = ET.SubElement(shape, "material", name=f"{link['name']}_color")
                    ET.SubElement(mat, "color", rgba=numbers(geom["rgba"]))
    for joint in data["joints"]:
        mapping["joints"][joint["id"]] = joint["name"]
        node = ET.SubElement(urdf, "joint", name=joint["name"], type=joint["type"])
        ET.SubElement(node, "parent", link=joint["parent"])
        ET.SubElement(node, "child", link=joint["child"])
        origin(node, joint)
        if joint["type"] != "fixed":
            ET.SubElement(node, "axis", xyz=numbers(joint["axis"]))
        if joint.get("limits"):
            ET.SubElement(node, "limit", attrib={key: numbers([value]) for key, value in joint["limits"].items()})
        if joint.get("dynamics"):
            ET.SubElement(node, "dynamics", attrib={key: numbers([value]) for key, value in joint["dynamics"].items()})
        if joint.get("mimic"):
            mimic = joint["mimic"]
            ET.SubElement(
                node,
                "mimic",
                joint=mimic["joint"],
                multiplier=numbers([mimic["multiplier"]]),
                offset=numbers([mimic["offset"]]),
            )
    for frame in data["frames"]:
        mapping["frames"][frame["id"]] = frame["name"]
        ET.SubElement(urdf, "link", name=frame["name"])
        node = ET.SubElement(urdf, "joint", name=frame["name"] + "_fixed", type="fixed")
        ET.SubElement(node, "parent", link=frame["parent"])
        ET.SubElement(node, "child", link=frame["name"])
        origin(node, frame)
    write_xml(destination / "urdf/robot.urdf", urdf)

    mjcf = ET.Element("mujoco", model="robot")
    ET.SubElement(mjcf, "compiler", angle="radian", eulerseq="XYZ", inertiafromgeom="false", fusestatic="false")
    ET.SubElement(
        mjcf,
        "option",
        timestep=str(profile.get("timestep", 0.002)),
        gravity=numbers(profile.get("gravity", [0, 0, -9.81])),
    )
    asset = ET.SubElement(mjcf, "asset")
    for link in data["links"]:
        for role in ("visuals", "collisions"):
            for index, geom in enumerate(link[role]):
                if geom["kind"] == "mesh":
                    ET.SubElement(
                        asset,
                        "mesh",
                        name=assets[(link["name"], role, index)],
                        file="../" + geom["filename"],
                        scale=numbers(geom["scale"]),
                    )
    world = ET.SubElement(mjcf, "worldbody")
    links = {link["name"]: link for link in data["links"]}
    child_joint = {joint["child"]: joint for joint in data["joints"]}
    root = next(name for name in links if name not in child_joint)

    def body(name: str, parent: ET.Element) -> None:
        link = links[name]
        joint = child_joint.get(name)
        pose = joint or {"xyz": [0, 0, 0], "rpy": [0, 0, 0]}
        node = ET.SubElement(parent, "body", name=name, pos=numbers(pose["xyz"]), euler=numbers(pose["rpy"]))
        if name == root and profile.get("root_mode", "fixed") == "floating":
            ET.SubElement(node, "freejoint", name="root_free_joint")
        if joint and joint["type"] != "fixed":
            attrs = {
                "name": joint["name"],
                "type": "slide" if joint["type"] == "prismatic" else "hinge",
                "axis": numbers(joint["axis"]),
            }
            if joint["type"] == "continuous":
                attrs["limited"] = "false"
            else:
                attrs.update(limited="true", range=numbers([joint["limits"]["lower"], joint["limits"]["upper"]]))
            effort = joint.get("limits", {}).get("effort")
            if effort is not None and effort > 0:
                attrs.update(actuatorfrclimited="true", actuatorfrcrange=numbers([-effort, effort]))
            for field, target in (("damping", "damping"), ("friction", "frictionloss")):
                if field in joint.get("dynamics", {}):
                    attrs[target] = numbers([joint["dynamics"][field]])
            ET.SubElement(node, "joint", **attrs)
        inertial = link["inertial"]
        if inertial:
            rot = rotation(inertial["rpy"])
            matrix = rot @ tensor(inertial["inertia"]) @ rot.T
            ET.SubElement(
                node,
                "inertial",
                mass=numbers([inertial["mass"]]),
                pos=numbers(inertial["xyz"]),
                fullinertia=numbers(
                    [matrix[0, 0], matrix[1, 1], matrix[2, 2], matrix[0, 1], matrix[0, 2], matrix[1, 2]]
                ),
            )
        for role in ("visuals", "collisions"):
            for index, geom in enumerate(link[role]):
                visual = role == "visuals"
                attrs = {
                    "name": f"{name}_{role}_{index}",
                    "pos": numbers(geom["xyz"]),
                    "euler": numbers(geom["rpy"]),
                    "group": "1" if visual else "3",
                    "contype": "0" if visual else "1",
                    "conaffinity": "0" if visual else "1",
                    "density": "0",
                }
                kind = geom["kind"]
                attrs["type"] = kind
                if kind == "mesh":
                    attrs["mesh"] = assets[(name, role, index)]
                elif kind == "box":
                    attrs["size"] = numbers([value / 2 for value in geom["size"]])
                elif kind == "sphere":
                    attrs["size"] = numbers([geom["radius"]])
                else:
                    attrs["size"] = numbers([geom["radius"], geom["length"] / 2])
                if "rgba" in geom:
                    attrs["rgba"] = numbers(geom["rgba"])
                if not visual and profile.get("contact"):
                    for key, value in profile["contact"].items():
                        attrs[key] = numbers(value if isinstance(value, list) else [value])
                ET.SubElement(node, "geom", **attrs)
        for frame in data["frames"]:
            if frame["parent"] == name:
                ET.SubElement(
                    node,
                    "site",
                    name=frame["name"],
                    pos=numbers(frame["xyz"]),
                    euler=numbers(frame["rpy"]),
                    size="0.001",
                )
        for candidate in data["joints"]:
            if candidate["parent"] == name:
                body(candidate["child"], node)

    body(root, world)
    if data["contact_excludes"]:
        contact = ET.SubElement(mjcf, "contact")
        for pair in data["contact_excludes"]:
            ET.SubElement(contact, "exclude", body1=pair["body1"], body2=pair["body2"])
    mimics = [joint for joint in data["joints"] if joint.get("mimic")]
    if mimics:
        equality = ET.SubElement(mjcf, "equality")
        for joint in mimics:
            mimic = joint["mimic"]
            ET.SubElement(
                equality,
                "joint",
                joint1=joint["name"],
                joint2=mimic["joint"],
                polycoef=numbers([mimic["offset"], mimic["multiplier"], 0, 0, 0]),
            )
    if data["actuators"]:
        actuators = ET.SubElement(mjcf, "actuator")
        for actuator in data["actuators"]:
            mapping["actuators"][actuator["id"]] = actuator["name"]
            ET.SubElement(
                actuators,
                actuator["type"],
                name=actuator["name"],
                joint=actuator["joint"],
                gear=numbers([actuator["gear"]]),
                ctrllimited="true",
                ctrlrange=numbers(actuator["control_range"]),
            )
    if data["sensors"]:
        sensors = ET.SubElement(mjcf, "sensor")
        for sensor in data["sensors"]:
            mapping["sensors"][sensor["id"]] = sensor["name"]
            attrs = {"name": sensor["name"]}
            if sensor["type"] in {"framepos", "framequat"}:
                attrs.update(objtype="site", objname=sensor["frame"])
            else:
                attrs["site"] = sensor["frame"]
            ET.SubElement(sensors, sensor["type"], **attrs)
    write_xml(destination / "mjcf/robot.xml", mjcf)
    scene = ET.Element("mujoco", model="robot")
    ET.SubElement(scene, "include", file="robot.xml")
    if profile.get("ground", False):
        attrs = {
            key: numbers(value if isinstance(value, list) else [value])
            for key, value in (profile.get("contact") or {}).items()
        }
        ET.SubElement(ET.SubElement(scene, "worldbody"), "geom", name="ground", type="plane", size="0 0 0.1", **attrs)
    write_xml(destination / "mjcf/scene.xml", scene)
    data["schema_version"] = "description.robot/v1"
    write_json(destination / "model/robot.json", data)
    write_json(destination / "model/mapping.json", mapping)
    write_json(
        destination / "config/consumer.json",
        {
            "frames": data["frames"],
            "actuators": data["actuators"],
            "sensors": data["sensors"],
            "control": data["control"],
            "contact_excludes": data["contact_excludes"],
            "profile": profile,
        },
    )
    return data
