"""把 URDF/MJCF 读成中立数据结构；只解析，不判断对错（判断在 rules.py）。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from xml.etree import ElementTree as ET

ROBOT_TYPES = ("revolute", "continuous", "prismatic", "fixed", "floating", "planar")


class ModelError(ValueError):
    pass


@dataclass
class MeshRef:
    uri: str
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rpy: tuple[float, float, float] = (0.0, 0.0, 0.0)
    path: Path | None = None  # 能解析成文件时给出
    kind: str = "visual"  # visual / collision


@dataclass
class Inertial:
    mass: float
    origin: tuple[float, float, float]
    rpy: tuple[float, float, float]
    inertia: tuple[float, float, float, float, float, float]  # ixx ixy ixz iyy iyz izz

    def matrix(self) -> tuple[tuple[float, float, float], ...]:
        ixx, ixy, ixz, iyy, iyz, izz = self.inertia
        return ((ixx, ixy, ixz), (ixy, iyy, iyz), (ixz, iyz, izz))

    def finite(self) -> bool:
        return all(math.isfinite(value) for value in (self.mass, *self.origin, *self.rpy, *self.inertia))


@dataclass
class Link:
    name: str
    inertial: Inertial | None = None
    meshes: list[MeshRef] = field(default_factory=list)
    visuals: int = 0
    collisions: int = 0


@dataclass
class Joint:
    name: str
    type: str
    parent: str
    child: str
    axis: tuple[float, float, float] | None = None
    limits: dict[str, float] | None = None
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rpy: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def moveable(self) -> bool:
        return self.type in ("revolute", "continuous", "prismatic")


@dataclass
class UrdfModel:
    path: Path
    name: str
    links: dict[str, Link] = field(default_factory=dict)
    joints: dict[str, Joint] = field(default_factory=dict)
    duplicate_links: list[str] = field(default_factory=list)
    duplicate_joints: list[str] = field(default_factory=list)

    def mesh_references(self) -> list[MeshRef]:
        return [mesh for link in self.links.values() for mesh in link.meshes]


def _numbers(text: str | None, count: int, *, what: str, default: float = 0.0) -> tuple[float, ...]:
    if text is None:
        return tuple(default for _ in range(count))
    fields = text.replace(",", " ").split()
    if len(fields) != count:
        raise ModelError(f"{what} 需要 {count} 个数，实际是 {text!r}")
    try:
        return tuple(float(field) for field in fields)
    except ValueError as error:
        raise ModelError(f"{what} 不是数字：{text!r}") from error


def _origin(element: ET.Element | None) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if element is None:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    return (
        _numbers(element.get("xyz"), 3, what="origin xyz"),
        _numbers(element.get("rpy"), 3, what="origin rpy"),
    )


def rpy_matrix(rpy: tuple[float, ...]) -> tuple[tuple[float, float, float], ...]:
    """URDF 固定轴约定：R = Rz(yaw) · Ry(pitch) · Rx(roll)。"""

    roll, pitch, yaw = rpy
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def load_urdf(path: Path) -> UrdfModel:
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise ModelError(f"XML 解析失败：{error}") from error
    except OSError as error:
        raise ModelError(f"无法读取 {path}：{error}") from error
    if root.tag != "robot":
        raise ModelError(f"根标签必须是 <robot>，实际是 <{root.tag}>")

    model = UrdfModel(path=path, name=root.get("name", ""))
    for element in root.findall("link"):
        name = element.get("name", "")
        link = Link(name=name)
        if name in model.links:
            model.duplicate_links.append(name)
        inertial = element.find("inertial")
        if inertial is not None:
            mass_element = inertial.find("mass")
            inertia_element = inertial.find("inertia")
            mass_text = mass_element.get("value") if mass_element is not None else None
            try:
                mass = float(mass_text) if mass_text is not None else float("nan")
                tensor = (
                    tuple(float(inertia_element.get(key, "nan")) for key in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz"))
                    if inertia_element is not None
                    else (float("nan"),) * 6
                )
            except ValueError as error:
                raise ModelError(f"link {name} 的 inertial 不是数字") from error
            xyz, rpy = _origin(inertial.find("origin"))
            link.inertial = Inertial(
                mass,
                cast(tuple[float, float, float], xyz),
                cast(tuple[float, float, float], rpy),
                cast(tuple[float, float, float, float, float, float], tensor),
            )
        for kind in ("visual", "collision"):
            for element_geometry in element.findall(kind):
                if kind == "visual":
                    link.visuals += 1
                else:
                    link.collisions += 1
                mesh = element_geometry.find("geometry/mesh")
                if mesh is None:
                    continue
                xyz, rpy = _origin(element_geometry.find("origin"))
                scale = _numbers(mesh.get("scale"), 3, what="mesh scale", default=1.0)
                link.meshes.append(
                    MeshRef(
                        uri=mesh.get("filename", ""),
                        scale=cast(tuple[float, float, float], scale),
                        origin=cast(tuple[float, float, float], xyz),
                        rpy=cast(tuple[float, float, float], rpy),
                        path=_resolve_mesh(path, mesh.get("filename", "")),
                        kind=kind,
                    )
                )
        model.links[name] = link

    for element in root.findall("joint"):
        name = element.get("name", "")
        parent_element = element.find("parent")
        child_element = element.find("child")
        joint = Joint(
            name=name,
            type=element.get("type", ""),
            parent=(parent_element.get("link", "") if parent_element is not None else ""),
            child=(child_element.get("link", "") if child_element is not None else ""),
        )
        if name in model.joints:
            model.duplicate_joints.append(name)
        axis = element.find("axis")
        if axis is not None and axis.get("xyz") is not None:
            joint.axis = cast(
                tuple[float, float, float],
                _numbers(axis.get("xyz"), 3, what=f"joint {name} axis"),
            )
        limit = element.find("limit")
        if limit is not None:
            joint.limits = {}
            for key in ("lower", "upper", "effort", "velocity"):
                if limit.get(key) is not None:
                    try:
                        joint.limits[key] = float(limit.get(key, ""))
                    except ValueError as error:
                        raise ModelError(f"joint {name} 的 limit {key} 不是数字") from error
        origin_xyz, origin_rpy = _origin(element.find("origin"))
        joint.origin = cast(tuple[float, float, float], origin_xyz)
        joint.rpy = cast(tuple[float, float, float], origin_rpy)
        model.joints[name] = joint
    return model


def _resolve_mesh(urdf_path: Path, uri: str) -> Path | None:
    """相对引用按 URDF 文件所在目录解析；绝对路径与 package:// 交给规则报错。"""

    if not uri or uri.startswith(("package://", "file://", "http://", "https://")):
        return None
    candidate = Path(uri)
    if candidate.is_absolute():
        return None
    resolved = (urdf_path.parent / candidate).resolve()
    return resolved if resolved.is_file() else None


@dataclass
class MjcfBody:
    name: str
    mass: float | None = None
    joints: list[dict] = field(default_factory=list)
    meshes: list[str] = field(default_factory=list)
    children: list[MjcfBody] = field(default_factory=list)


@dataclass
class MjcfModel:
    path: Path
    name: str
    meshdir: str
    bodies: dict[str, MjcfBody] = field(default_factory=dict)
    mesh_files: list[str] = field(default_factory=list)


def load_mjcf(path: Path) -> MjcfModel:
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise ModelError(f"XML 解析失败：{error}") from error
    except OSError as error:
        raise ModelError(f"无法读取 {path}：{error}") from error
    if root.tag != "mujoco":
        raise ModelError(f"根标签必须是 <mujoco>，实际是 <{root.tag}>")
    compiler = root.find("compiler")
    model = MjcfModel(
        path=path,
        name=root.get("model", ""),
        meshdir=(compiler.get("meshdir", "") if compiler is not None else ""),
    )
    asset = root.find("asset")
    if asset is not None:
        model.mesh_files = sorted(mesh.get("file", "") for mesh in asset.findall("mesh") if mesh.get("file"))

    def walk(element: ET.Element, parent: MjcfBody | None) -> None:
        name = element.get("name", "")
        body = MjcfBody(name=name)
        inertial = element.find("inertial")
        mass_text = inertial.get("mass") if inertial is not None else None
        if mass_text is not None:
            try:
                body.mass = float(mass_text)
            except ValueError:
                body.mass = float("nan")
        for joint in element.findall("joint"):
            body.joints.append(
                {
                    "name": joint.get("name", ""),
                    "type": joint.get("type", "hinge"),
                    "range": joint.get("range"),
                    "axis": joint.get("axis"),
                }
            )
        for geom in element.findall("geom"):
            mesh_name = geom.get("mesh")
            if mesh_name:
                body.meshes.append(mesh_name)
        if name:
            model.bodies[name] = body
        if parent is not None:
            parent.children.append(body)
        for child in element.findall("body"):
            walk(child, body)

    world = root.find("worldbody")
    if world is not None:
        for element in world.findall("body"):
            walk(element, None)
    return model


REPO_LINK = re.compile(r"^(?:left_|right_)?[a-z0-9][a-z0-9_]*_link$")
REPO_JOINT = re.compile(r"^(?:left_|right_)?[a-z0-9][a-z0-9_]*_joint$")
