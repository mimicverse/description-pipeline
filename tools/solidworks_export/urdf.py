"""Deterministic URDF serialization for the bridge exporter."""

from __future__ import annotations

from typing import Sequence

from .model import RobotJoint, RobotLink, RobotModel


def format_number(value: float) -> str:
    """Deterministic, human-readable number formatting (no -0, 12 sig digits)."""

    number = float(value)
    if abs(number) < 5e-13:
        return "0"
    text = f"{number:.12g}"
    if text.startswith("-0.") and float(text) == 0.0:
        return "0"
    return text


def _vec3(values: Sequence[float]) -> str:
    return " ".join(format_number(v) for v in values)


def _link_xml(link: RobotLink) -> str:
    if link.is_frame:
        # A frame link has no solid: URDF allows a link without inertial,
        # visual or collision entries, and consumers treat it as a frame.
        return f'  <link name="{link.name}"/>'
    ixx, ixy, ixz, iyy, iyz, izz = link.inertia6
    inertia_values = tuple(format_number(value) for value in (ixx, ixy, ixz, iyy, iyz, izz))
    return "\n".join(
        [
            f'  <link name="{link.name}">',
            "    <inertial>",
            f'      <origin xyz="{_vec3(link.com)}" rpy="{_vec3(link.inertial_rpy)}"/>',
            f'      <mass value="{format_number(link.mass)}"/>',
            f'      <inertia ixx="{inertia_values[0]}" ixy="{inertia_values[1]}" '
            f'ixz="{inertia_values[2]}" iyy="{inertia_values[3]}" '
            f'iyz="{inertia_values[4]}" izz="{inertia_values[5]}"/>',
            "    </inertial>",
            "    <visual>",
            "      <geometry>",
            f'        <mesh filename="{link.mesh}"/>',
            "      </geometry>",
            "    </visual>",
            "    <collision>",
            "      <geometry>",
            f'        <mesh filename="{link.mesh}"/>',
            "      </geometry>",
            "    </collision>",
            "  </link>",
        ]
    )


def _joint_xml(joint: RobotJoint) -> str:
    lines = [
        f'  <joint name="{joint.name}" type="{joint.type}">',
        f'    <origin xyz="{_vec3(joint.xyz)}" rpy="{_vec3(joint.rpy)}"/>',
        f'    <parent link="{joint.parent}"/>',
        f'    <child link="{joint.child}"/>',
    ]
    if joint.type != "fixed" and joint.axis is not None:
        lines.append(f'    <axis xyz="{_vec3(joint.axis)}"/>')
    if joint.limit:
        limit = joint.limit
        parts = []
        for key in ("lower", "upper", "effort", "velocity"):
            if key in limit:
                parts.append(f'{key}="{format_number(limit[key])}"')
        lines.append(f"    <limit {' '.join(parts)}/>")
    if joint.dynamics and (joint.dynamics.get("damping") or joint.dynamics.get("friction")):
        parts = []
        for key in ("damping", "friction"):
            if key in joint.dynamics:
                parts.append(f'{key}="{format_number(joint.dynamics[key])}"')
        lines.append(f"    <dynamics {' '.join(parts)}/>")
    lines.append("  </joint>")
    return "\n".join(lines)


def build_urdf(model: RobotModel) -> str:
    """Serialize a RobotModel; identical input always yields identical text."""

    blocks = ['<?xml version="1.0"?>', f'<robot name="{model.name}">']
    blocks.extend(_link_xml(link) for link in model.links)
    blocks.extend(_joint_xml(joint) for joint in model.joints)
    blocks.append("</robot>")
    return "\n".join(blocks) + "\n"


def write_urdf(path: str, model: RobotModel) -> str:
    text = build_urdf(model)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return text
