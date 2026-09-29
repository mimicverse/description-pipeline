"""export_config.json: schema, loading and semantic validation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import ConfigError

SCHEMA_VERSION = "swbridge.export-config/v1"
MOVABLE_TYPES = ("revolute", "prismatic", "continuous")
ALLOWED_TYPES = (*MOVABLE_TYPES, "fixed")
MESH_PREFIX_RE = re.compile(r"(?:\.\./)?[A-Za-z0-9_.-]+/")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
UNITS = ("m", "mm")
PRODUCT_CONVENTIONS = ("solidworks_positive", "standard_negative")


@dataclass
class LinkSpec:
    name: str
    components: List[str]
    frame_component: Optional[str] = None
    is_frame: bool = False
    frame: Optional[Dict[str, List[float]]] = None


@dataclass
class JointSpec:
    name: str
    type: str
    parent: str
    child: str
    coordinate_system: Optional[str] = None
    origin: Optional[Dict[str, List[float]]] = None
    axis: Optional[Tuple[float, float, float]] = None
    limits: Optional[Dict[str, float]] = None
    dynamics: Optional[Dict[str, float]] = None


@dataclass
class ExportConfig:
    model: str
    links: List[LinkSpec] = field(default_factory=list)
    joints: List[JointSpec] = field(default_factory=list)
    mesh_merge: str = "per_link"
    mesh_path_prefix: str = "meshes/"
    source_length_unit: str = "m"
    inertia_product_convention: str = "solidworks_positive"
    coordinate_system_transforms: Dict[str, List[float]] = field(default_factory=dict)
    component_masses: Dict[str, float] = field(default_factory=dict)
    mass_provenance: Dict[str, str] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    notes: Dict[str, str] = field(default_factory=dict)


def _require(condition: object, message: str, detail=None) -> None:
    """条件为"真值"即通过；类型放宽到 object 以允许直接传可能为 None 的字段。"""

    if not condition:
        raise ConfigError(message, detail)


def _vec3(value, field_name: str) -> Tuple[float, float, float]:
    _require(isinstance(value, (list, tuple)) and len(value) == 3, f"{field_name} must be a list of 3 numbers", value)
    _require(not any(isinstance(v, bool) for v in value), f"{field_name} must contain numbers, not booleans")
    try:
        return (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must contain numbers", str(exc)) from exc


def _numbers(mapping: dict, field_name: str) -> Dict[str, float]:
    """Coerce a mapping of names to finite numbers; every failure is a ConfigError."""
    parsed: Dict[str, float] = {}
    for key, value in mapping.items():
        _require(not isinstance(value, bool), f"{field_name}[{key!r}] must be a number", value)
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{field_name}[{key!r}] must be a number", str(exc)) from exc
        _require(math.isfinite(number), f"{field_name}[{key!r}] must be finite", value)
        parsed[str(key)] = number
    return parsed


def config_from_dict(data: dict) -> ExportConfig:
    _require(isinstance(data, dict), "config must be a JSON object")
    schema = data.get("schema_version", SCHEMA_VERSION)
    _require(schema == SCHEMA_VERSION, f"unsupported schema_version: {schema!r}", {"expected": SCHEMA_VERSION})
    model = data.get("model")
    _require(
        isinstance(model, str) and NAME_RE.match(model) is not None, "config.model must be a stable ASCII name", model
    )
    assert isinstance(model, str)  # _require 已保证

    mesh = data.get("mesh", {}) or {}
    mesh_merge = mesh.get("merge", "per_link")
    _require(mesh_merge in ("per_link", "none"), "mesh.merge must be 'per_link' or 'none'", mesh_merge)
    _require(mesh.get("format", "stl_binary") == "stl_binary", "mesh.format must be 'stl_binary'", mesh.get("format"))
    # Package layout uses "meshes/", the description repository uses "../meshes/".
    mesh_path_prefix = str(mesh.get("path_prefix", "meshes/"))
    _require(
        MESH_PREFIX_RE.fullmatch(mesh_path_prefix) is not None and "\\" not in mesh_path_prefix,
        "mesh.path_prefix must be a relative directory ending in '/': 'meshes/' or '../meshes/'",
        mesh_path_prefix,
    )

    unit = data.get("source_length_unit", "m")
    _require(unit in UNITS, f"source_length_unit must be one of {UNITS}", unit)

    convention = data.get("inertia_product_convention", "solidworks_positive")
    _require(
        convention in PRODUCT_CONVENTIONS,
        f"inertia_product_convention must be one of {PRODUCT_CONVENTIONS}",
        convention,
    )

    links: List[LinkSpec] = []
    for raw in data.get("links", []):
        _require(isinstance(raw, dict), "each link must be an object")
        name = raw.get("name")
        _require(
            isinstance(name, str) and NAME_RE.match(name) is not None, "link.name must be a stable ASCII name", name
        )
        components = raw.get("components")
        frame = raw.get("frame_component")
        if components is None or components == []:
            # Massless sensor/foot frame: no CAD solid of its own, only a frame.
            _require(
                isinstance(frame, str) and frame,
                "a link without components must name the frame_component it sits on",
                name,
            )
            links.append(LinkSpec(name=name, components=[], frame_component=frame, is_frame=True))
            continue
        _require(isinstance(components, list) and components, "link.components must be a non-empty list", name)
        for component in components:
            _require(
                isinstance(component, str) and component, "link.components entries must be non-empty strings", name
            )
        if frame is not None:
            _require(frame in components, "link.frame_component must be one of the link components", name)
        explicit = raw.get("frame")
        parsed_frame = None
        if explicit is not None:
            _require(isinstance(explicit, dict), "link.frame must be an object", name)
            parsed_frame = {
                "xyz": list(_vec3(explicit.get("xyz"), "link.frame.xyz")),
                "rpy": list(_vec3(explicit.get("rpy"), "link.frame.rpy")),
            }
        links.append(LinkSpec(name=name, components=list(components), frame_component=frame, frame=parsed_frame))

    joints: List[JointSpec] = []
    for raw in data.get("joints", []):
        _require(isinstance(raw, dict), "each joint must be an object")
        name = raw.get("name")
        _require(
            isinstance(name, str) and NAME_RE.match(name) is not None, "joint.name must be a stable ASCII name", name
        )
        joint_type = raw.get("type")
        _require(joint_type in ALLOWED_TYPES, f"joint.type must be one of {ALLOWED_TYPES}", joint_type)
        parent, child = raw.get("parent"), raw.get("child")
        for value, label in ((parent, "parent"), (child, "child")):
            _require(
                isinstance(value, str) and NAME_RE.match(value) is not None, f"joint.{label} must be a link name", value
            )
        axis = _vec3(raw["axis"], "joint.axis") if raw.get("axis") is not None else None
        limits = raw.get("limits")
        if limits is not None:
            _require(isinstance(limits, dict), "joint.limits must be an object", name)
            limits = _numbers(limits, "joint.limits")
        dynamics = raw.get("dynamics")
        if dynamics is not None:
            _require(isinstance(dynamics, dict), "joint.dynamics must be an object", name)
            dynamics = _numbers(dynamics, "joint.dynamics")
        cs = raw.get("coordinate_system")
        if cs is not None:
            _require(isinstance(cs, str) and cs, "joint.coordinate_system must be a feature name", name)
        origin = raw.get("origin")
        parsed_origin = None
        if origin is not None:
            _require(isinstance(origin, dict), "joint.origin must be an object", name)
            parsed_origin = {
                "xyz": list(_vec3(origin.get("xyz"), "joint.origin.xyz")),
                "rpy": list(_vec3(origin.get("rpy"), "joint.origin.rpy")),
            }
        if joint_type in ("revolute", "prismatic"):
            _require(
                cs is not None or parsed_origin is not None,
                "a movable joint needs coordinate_system or an explicit origin",
                name,
            )
        joints.append(
            JointSpec(
                name=name,
                type=joint_type,
                parent=parent,
                child=child,
                coordinate_system=cs,
                origin=parsed_origin,
                axis=axis,
                limits=limits,
                dynamics=dynamics,
            )
        )

    overrides = data.get("coordinate_system_transforms", {}) or {}
    _require(isinstance(overrides, dict), "coordinate_system_transforms must be an object")
    parsed_overrides: Dict[str, List[float]] = {}
    for key, value in overrides.items():
        values = list(value) if isinstance(value, (list, tuple)) else None
        _require(
            values is not None and len(values) == 16,
            f"coordinate_system_transforms[{key!r}] must hold 16 numbers",
        )
        assert values is not None  # _require 已保证
        parsed = _numbers({str(i): v for i, v in enumerate(values)}, f"coordinate_system_transforms[{key!r}]")
        parsed_overrides[str(key)] = [parsed[str(i)] for i in range(len(values))]

    raw_masses = data.get("component_masses", {}) or {}
    _require(isinstance(raw_masses, dict), "component_masses must be an object")
    masses: Dict[str, float] = {}
    for key, value in raw_masses.items():
        _require(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
            and math.isfinite(float(value)),
            f"component_masses[{key!r}] must be a positive mass in kg",
        )
        masses[str(key)] = float(value)
    provenance = {str(k): str(v) for k, v in (data.get("mass_provenance", {}) or {}).items()}
    if masses:
        _require(
            provenance.get("kind") == "documented_source",
            "component_masses require mass_provenance.kind=documented_source",
            provenance,
        )

    cfg = ExportConfig(
        model=model,
        links=links,
        joints=joints,
        mesh_merge=mesh_merge,
        mesh_path_prefix=mesh_path_prefix,
        source_length_unit=unit,
        inertia_product_convention=convention,
        coordinate_system_transforms=parsed_overrides,
        component_masses=masses,
        mass_provenance=provenance,
        schema_version=SCHEMA_VERSION,
        notes={str(k): str(v) for k, v in (data.get("notes", {}) or {}).items()},
    )
    validate_config(cfg)
    return cfg


def load_config(path: str) -> ExportConfig:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read config: {exc}", {"path": path}) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid JSON: {exc}", {"path": path}) from exc
    return config_from_dict(data)


def validate_config(cfg: ExportConfig) -> None:
    _require(cfg.links, "config must declare at least one link")
    _require(cfg.joints or len(cfg.links) == 1, "a multi-link model must declare joints")

    names = [link.name for link in cfg.links]
    _require(len(names) == len(set(names)), "duplicate link names", names)
    joint_names = [joint.name for joint in cfg.joints]
    _require(len(joint_names) == len(set(joint_names)), "duplicate joint names", joint_names)
    # URDF allows a link and a joint to share a name (the reference Microban model
    # does exactly that, e.g. "head" is both); only within-category uniqueness matters.

    component_owner: Dict[str, str] = {}
    for link in cfg.links:
        for component in link.components:
            _require(
                component not in component_owner,
                f"component {component!r} is assigned to multiple links",
                {"links": [component_owner.get(component), link.name]},
            )
            component_owner[component] = link.name

    known = set(names)
    children: Dict[str, str] = {}
    for joint in cfg.joints:
        _require(
            joint.parent in known, "joint.parent is not a declared link", {"joint": joint.name, "parent": joint.parent}
        )
        _require(
            joint.child in known, "joint.child is not a declared link", {"joint": joint.name, "child": joint.child}
        )
        _require(joint.parent != joint.child, "joint parent and child must differ", joint.name)
        _require(
            joint.child not in children,
            f"link {joint.child!r} has more than one parent joint",
            {"joints": [children.get(joint.child), joint.name]},
        )
        children[joint.child] = joint.name
        if joint.type == "fixed":
            # A fixed joint carries no motion, so a zero (or absent) axis stays
            # zero instead of being rejected; reference models write "0 0 0".
            if joint.axis is not None and any(abs(v) > 0 for v in joint.axis):
                joint.axis = _normalize_axis(joint.axis, joint.name)
            elif joint.axis is not None:
                joint.axis = None
            continue
        if joint.type in ("revolute", "continuous"):
            _require(
                joint.limits is None
                or joint.type == "continuous"
                or ("lower" in joint.limits and "upper" in joint.limits),
                "revolute joint limits need lower and upper",
                joint.name,
            )
        if joint.type == "revolute" or joint.type == "prismatic":
            _require(joint.limits is not None, "joint requires explicit limits", joint.name)
        if joint.limits is not None:
            for key, value in joint.limits.items():
                _require(
                    isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
                    "joint limits must be finite",
                    {"joint": joint.name, "key": key},
                )
        _require(joint.axis is not None, "movable joint requires an explicit axis; no default is inferred", joint.name)
        axis = joint.axis
        assert axis is not None  # _require 已保证
        joint.axis = _normalize_axis(axis, joint.name)
        if joint.limits is not None and joint.type in ("revolute", "prismatic"):
            lower, upper = joint.limits.get("lower"), joint.limits.get("upper")
            _require(
                lower is not None and upper is not None and lower < upper,
                "joint limits need lower < upper",
                {"joint": joint.name, "lower": lower, "upper": upper},
            )
            _require(float(joint.limits.get("effort", 0.0)) > 0.0, "joint limits need positive effort", joint.name)
            _require(float(joint.limits.get("velocity", 0.0)) > 0.0, "joint limits need positive velocity", joint.name)

    roots = [name for name in names if name not in children]
    _require(len(roots) == 1, "the link tree must have exactly one root", roots)
    root = roots[0]
    for name in names:
        seen = set()
        current = name
        while current != root:
            _require(current not in seen, "cycle in the link tree", current)
            seen.add(current)
            parent_joint = children.get(current)
            _require(parent_joint is not None, "link is disconnected from the root", current)
            matching = [j for j in cfg.joints if j.name == parent_joint]
            current = matching[0].parent
    cfg.notes.setdefault("root_link", root)


def _normalize_axis(axis: Sequence[float], joint: str) -> Tuple[float, float, float]:
    values = tuple(float(v) for v in axis)
    _require(
        len(values) == 3 and all(math.isfinite(v) for v in values), "joint axis must have three finite values", joint
    )
    norm = math.hypot(*values)
    _require(math.isfinite(norm) and norm > 1e-12, "joint axis must be non-zero and have finite norm", joint)
    return (values[0] / norm, values[1] / norm, values[2] / norm)
