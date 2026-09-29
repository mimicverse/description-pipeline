"""Turn a RawScene plus an ExportConfig into a RobotModel and CAD evidence.

Frame and sign conventions:

* Component mass properties are read in the component's own (part) frame --
  that is what ``IMassProperty2`` on the component document returns.
* The link frame is defined by the joint coordinate system when the link has a
  parent joint that declares one; otherwise by the link's frame component.
  Inertia, COM and mesh all use this same frame, so the URDF child frame
  (which coincides with the joint frame) is consistent with the mesh and
  inertia data.
* SolidWorks reports products of inertia in the positive-products notation.
  The exporter negates the three cross terms **in the original part axes
  first** and only then rotates into the link frame (docs, section 2:
  "交叉项取反必须发生在原始坐标轴中，再旋转到 link 轴").
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple, cast

from .config import ExportConfig
from .errors import CadError
from .model import RawScene, RobotJoint, RobotLink, RobotModel
from .physics import (
    combine_mass_properties,
    inertia6_of,
    inertia_rotate,
    physics_conditions,
)
from .transform import (
    apply_point,
    between,
    from_xyz_rpy,
    inverse_rigid,
    matmul,
    rotation_3x3_of,
    xyz_rpy_of,
)

UNIT_FACTOR = {"m": 1.0, "mm": 1e-3}
PRODUCT_CONVENTIONS = ("solidworks_positive", "standard_negative")


def _scaled(transform, factor: float):
    values = list(transform)
    for row in range(3):
        values[row * 4 + 3] *= factor
    return tuple(values)


def _mesh_scale(transform, factor: float):
    """Scale the linear part so document-unit (e.g. mm) meshes land in metres.

    The link frame itself is already scaled to metres; mesh vertices come from
    the CAD document and therefore need the same factor applied to their
    coordinates (translation stays untouched).
    """

    if factor == 1.0:
        return transform
    values = list(transform)
    for row in range(3):
        for col in range(3):
            values[row * 4 + col] *= factor
    return tuple(values)


def _flip_products(inertia: Sequence[Sequence[float]]):
    """Positive-products (SolidWorks) tensor -> standard inertia tensor."""

    return (
        (float(inertia[0][0]), -float(inertia[0][1]), -float(inertia[0][2])),
        (-float(inertia[1][0]), float(inertia[1][1]), -float(inertia[1][2])),
        (-float(inertia[2][0]), -float(inertia[2][1]), float(inertia[2][2])),
    )


def _standard_inertia(inertia: Sequence[Sequence[float]], convention: str):
    if convention == "solidworks_positive":
        return _flip_products(inertia)
    return tuple(tuple(float(value) for value in row) for row in inertia)


def _lookup_transform(cfg: ExportConfig, scene: RawScene, feature_name: str, notes: Dict[str, str]):
    override = cfg.coordinate_system_transforms.get(feature_name)
    if override is not None:
        notes[f"coordinate_system:{feature_name}"] = "config_override"
        return tuple(float(v) for v in override), "config_override"
    scene_transform = scene.coordinate_systems.get(feature_name)
    if scene_transform is None:
        raise CadError(
            "cad_missing_coordinate_system",
            f"assembly has no coordinate system named {feature_name!r}",
            {"available": sorted(scene.coordinate_systems.keys())},
        )
    notes[f"coordinate_system:{feature_name}"] = "solidworks_api"
    return scene_transform, "solidworks_api"


def build_robot_model(cfg: ExportConfig, scene: RawScene):
    """Return ``(RobotModel, evidence, mesh_transforms)`` from raw CAD values.

    ``mesh_transforms`` maps component name -> 4x4 transform that brings the
    component's part-frame mesh into its link frame (same frame as the inertia).
    """

    factor = UNIT_FACTOR[cfg.source_length_unit]
    notes: Dict[str, str] = dict(scene.notes)
    component_names = [component.name for component in scene.components]
    duplicates = sorted({name for name in component_names if component_names.count(name) > 1})
    if duplicates:
        raise CadError(
            "cad_duplicate_component_name",
            "assembly contains components with duplicate names",
            {
                "duplicates": duplicates,
                "hint": "rename the instances in SolidWorks (stable, unique names) "
                "or split them into distinct components before export",
            },
        )
    components = {component.name: component for component in scene.components}
    for spec in cfg.links:
        if spec.is_frame:
            continue
        missing = [name for name in spec.components if name not in components]
        if missing:
            raise CadError(
                "cad_missing_component",
                "assembly does not contain configured components",
                {"link": spec.name, "missing": missing, "available": sorted(components.keys())},
            )

    # ------------------------------------------------------------------ frames
    joint_by_child = {joint.child: joint for joint in cfg.joints}
    link_frames: Dict[str, tuple] = {}
    link_frame_sources: Dict[str, str] = {}
    for spec in cfg.links:
        parent_joint = joint_by_child.get(spec.name)
        if spec.frame:
            link_frames[spec.name] = _scaled(from_xyz_rpy(tuple(spec.frame["xyz"]), tuple(spec.frame["rpy"])), factor)
            link_frame_sources[spec.name] = "link_config_frame"
        elif parent_joint is not None and parent_joint.origin:
            xyz = parent_joint.origin["xyz"]
            rpy = parent_joint.origin["rpy"]
            link_frames[spec.name] = _scaled(from_xyz_rpy(tuple(xyz), tuple(rpy)), factor)
            link_frame_sources[spec.name] = f"joint_config_origin:{parent_joint.name}"
        elif parent_joint is not None and parent_joint.coordinate_system:
            transform, _source = _lookup_transform(cfg, scene, parent_joint.coordinate_system, notes)
            link_frames[spec.name] = _scaled(transform, factor)
            link_frame_sources[spec.name] = f"joint_coordinate_system:{parent_joint.coordinate_system}"
        else:
            frame_name = spec.frame_component or (spec.components[0] if spec.components else None)
            if frame_name is None:
                raise CadError(
                    "cad_frame_without_component",
                    f"frame link {spec.name!r} needs a frame_component",
                    {"link": spec.name},
                )
            link_frames[spec.name] = _scaled(components[frame_name].transform, factor)
            link_frame_sources[spec.name] = f"frame_component:{frame_name}"

    evidence: Dict[str, Any] = {"document": scene.document, "links": {}, "joints": {}}
    mesh_transforms: Dict[str, tuple] = {}

    # ------------------------------------------------------------------- links
    links: List[RobotLink] = []
    for spec in cfg.links:
        if spec.is_frame:
            # Frames carry no CAD solid: no mass, no inertia, no mesh, and no
            # invented numbers. kinematics only.
            links.append(
                RobotLink(
                    name=spec.name,
                    mass=0.0,
                    com=(0.0, 0.0, 0.0),
                    inertia6=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                    inertial_rpy=(0.0, 0.0, 0.0),
                    mesh=None,
                    is_frame=True,
                )
            )
            evidence["links"][spec.name] = {
                "kind": "frame",
                "frame_component": spec.frame_component,
                "link_frame_source": link_frame_sources[spec.name],
                "components": [],
                "mass_properties": "not_applicable",
                "note": "massless frame link; no CAD mass properties are claimed",
            }
            continue
        link_frame = link_frames[spec.name]
        inverse_link = inverse_rigid(link_frame)
        entries = []
        raw_evidence = {}
        component_frames = {}
        for name in spec.components:
            component = components[name]
            placement = _scaled(component.transform, factor)
            local = matmul(inverse_link, placement)
            mesh_transforms[name] = _mesh_scale(local, factor)

            props = scene.mass_properties.get(name)
            if props is None:
                raise CadError(
                    "cad_missing_mass_properties",
                    f"no mass properties recorded for component {name!r}",
                    {"link": spec.name},
                )
            raw_inertia = props["inertia"]
            standard_local = _standard_inertia(raw_inertia, cfg.inertia_product_convention)
            if factor != 1.0:
                standard_local = tuple(tuple(value * factor * factor for value in row) for row in standard_local)
            rotation_local = rotation_3x3_of(local)
            inertia_link = inertia_rotate(standard_local, rotation_local)
            com_local = tuple(float(v) * factor for v in props["com"])
            com_link = apply_point(local, com_local)
            mass_value = float(props["mass"])
            documented = cfg.component_masses.get(name)
            if documented:
                # Documented mass (e.g. printed-part effective density or a datasheet
                # value) replaces the CAD density: geometry keeps the shape, so the
                # inertia scales by the mass ratio and the COM stays where CAD put it.
                scale = documented / mass_value if mass_value else 0.0
                inertia_link = tuple(tuple(value * scale for value in row) for row in inertia_link)
                mass_value = documented
            entries.append({"mass": mass_value, "com": com_link, "inertia": inertia_link})
            xyz_placement, rpy_placement = xyz_rpy_of(local)
            component_frames[name] = {
                "xyz_m": list(xyz_placement),
                "rpy_rad": list(rpy_placement),
            }
            raw_evidence[name] = {
                "mass": float(props["mass"]),
                "documented_mass": documented if documented else None,
                "mass_source": "documented" if documented else "cad",
                "com": [float(v) for v in props["com"]],
                "inertia": [[float(v) for v in row] for row in raw_inertia],
                "raw_frame": "component_part_frame",
                "product_convention": cfg.inertia_product_convention,
                "reference": props.get("reference", {}),
            }
        combined = combine_mass_properties(entries)
        conditions = physics_conditions(combined["inertia"])
        links.append(
            RobotLink(
                name=spec.name,
                mass=combined["mass"],
                com=tuple(combined["com"]),
                inertia6=inertia6_of(combined["inertia"]),
                inertial_rpy=(0.0, 0.0, 0.0),
                mesh=f"{cfg.mesh_path_prefix}{spec.name}.STL",
            )
        )
        evidence["links"][spec.name] = {
            "frame_component": spec.frame_component or spec.components[0],
            "link_frame_source": link_frame_sources[spec.name],
            "components": list(spec.components),
            "component_placement_in_link": component_frames,
            "raw": raw_evidence,
            "combined": {
                "mass": combined["mass"],
                "com": list(combined["com"]),
                "inertia": [[float(v) for v in row] for row in combined["inertia"]],
                "standard_convention": "negative_products",
            },
            "conditions": conditions,
            "source_length_unit": cfg.source_length_unit,
            "inertia_product_convention_declared": cfg.inertia_product_convention,
            "inertia_convention_confirmed_on_hardware": False,
        }

    # ------------------------------------------------------------------ joints
    joints: List[RobotJoint] = []
    for joint_spec in cfg.joints:
        if joint_spec.name and joint_spec.child not in link_frames:
            raise CadError(
                "cad_missing_link_frame",
                "no frame for child link",
                {"joint": joint_spec.name, "child": joint_spec.child},
            )
        parent_frame = link_frames[joint_spec.parent]
        child_frame = link_frames[joint_spec.child]
        if joint_spec.origin is not None:
            # The configuration already states the URDF joint origin (documented
            # reference kinematics); do not re-derive it from CAD placements.
            joint_xyz = tuple(float(v) * 1.0 for v in joint_spec.origin["xyz"])
            joint_rpy = tuple(float(v) for v in joint_spec.origin["rpy"])
        else:
            relative = between(parent_frame, child_frame)
            joint_xyz, joint_rpy = xyz_rpy_of(relative)
        joints.append(
            RobotJoint(
                name=joint_spec.name,
                type=joint_spec.type,
                parent=joint_spec.parent,
                child=joint_spec.child,
                xyz=cast(Tuple[float, float, float], joint_xyz),
                rpy=cast(Tuple[float, float, float], joint_rpy),
                axis=joint_spec.axis,
                limit=joint_spec.limits,
                dynamics=joint_spec.dynamics,
            )
        )
        evidence["joints"][joint_spec.name] = {
            "parent": joint_spec.parent,
            "child": joint_spec.child,
            "type": joint_spec.type,
            "coordinate_system": joint_spec.coordinate_system,
            "child_link_frame_source": link_frame_sources[joint_spec.child],
            "origin_xyz": list(joint_xyz),
            "origin_rpy": list(joint_rpy),
            "axis_source": "explicit_export_configuration",
            "physical_axis_reference_verified": False,
        }

    model = RobotModel(name=cfg.model, links=links, joints=joints)
    evidence["notes"] = notes
    return model, evidence, mesh_transforms
