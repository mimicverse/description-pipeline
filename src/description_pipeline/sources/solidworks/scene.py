"""Turn raw CAD readings into the shared ``description.scene/v1`` structure.

The adapter owns the *source-side* derivation: component placements, per-part
mass properties and exported geometry are read from CAD, then expressed in the
link frames the source config declares.  No value is copied from a final URDF,
and nothing is invented when the definition is incomplete - a missing frame or
joint definition is an error, not a default.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Sequence

from .errors import ConfigError
from .jsonio import read_json

SCENE_SCHEMA = "description.scene/v1"

Matrix3 = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
Vector3 = tuple[float, float, float]


# -- small rigid-transform helpers ---------------------------------------


def _matrix_from_row_major(values: Sequence[float], offset: int) -> Matrix3:
    return (
        (float(values[offset]), float(values[offset + 1]), float(values[offset + 2])),
        (float(values[offset + 4]), float(values[offset + 5]), float(values[offset + 6])),
        (float(values[offset + 8]), float(values[offset + 9]), float(values[offset + 10])),
    )


def _translation_from_row_major(values: Sequence[float]) -> Vector3:
    return (float(values[3]), float(values[7]), float(values[11]))


def identity_matrix() -> Matrix3:
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _transpose(matrix: Matrix3) -> Matrix3:
    return tuple(tuple(matrix[j][i] for j in range(3)) for i in range(3))  # type: ignore[return-value]


def _matmul(first: Matrix3, second: Matrix3) -> Matrix3:
    return tuple(tuple(sum(first[i][k] * second[k][j] for k in range(3)) for j in range(3)) for i in range(3))  # type: ignore[return-value]


def _matvec(matrix: Matrix3, vector: Vector3) -> Vector3:
    return tuple(sum(matrix[i][k] * vector[k] for k in range(3)) for i in range(3))  # type: ignore[return-value]


def _rotate_inertia(matrix: Matrix3, inertia: Matrix3) -> Matrix3:
    return _matmul(_matmul(matrix, inertia), _transpose(matrix))


def _is_identity(matrix: Matrix3) -> bool:
    return all(abs(matrix[i][j] - (1.0 if i == j else 0.0)) < 1e-12 for i in range(3) for j in range(3))


def rpy_from_matrix(matrix: Matrix3) -> Vector3:
    """URDF fixed-axis roll-pitch-yaw (R = Rz(y) Ry(p) Rx(r)) for a rotation."""

    if _is_identity(matrix):
        return (0.0, 0.0, 0.0)
    pitch = math.asin(max(-1.0, min(1.0, -matrix[2][0])))
    if abs(math.cos(pitch)) > 1e-9:
        roll = math.atan2(matrix[2][1], matrix[2][2])
        yaw = math.atan2(matrix[1][0], matrix[0][0])
    else:  # gimbal lock: pick roll = 0 and put the rotation in yaw
        roll = 0.0
        yaw = math.atan2(-matrix[0][1], matrix[1][1])
    return (roll, pitch, yaw)


def _inertia6(inertia: Matrix3) -> list[float]:
    return [
        inertia[0][0],
        inertia[0][1],
        inertia[0][2],
        inertia[1][1],
        inertia[1][2],
        inertia[2][2],
    ]


def combine_mass_properties(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Combine per-part mass properties about the combined COM (parallel axis).

    The canonical layer owns the authoritative combination; this source-side
    result exists so a snapshot carries usable semantics, and its provenance
    points at the raw per-part readings it came from.
    """

    if not entries:
        raise ConfigError("a body has no mass properties to combine")
    total = sum(float(entry["mass"]) for entry in entries)
    if total <= 0.0 or not math.isfinite(total):
        raise ConfigError("combined mass must be positive and finite")
    com = [0.0, 0.0, 0.0]
    for entry in entries:
        mass = float(entry["mass"])
        for axis in range(3):
            com[axis] += mass * float(entry["com"][axis])
    com = [value / total for value in com]
    inertia = [[0.0] * 3 for _ in range(3)]
    for entry in entries:
        mass = float(entry["mass"])
        delta = [float(entry["com"][i]) - com[i] for i in range(3)]
        squared = sum(value * value for value in delta)
        for i in range(3):
            for j in range(3):
                shift = mass * ((squared if i == j else 0.0) - delta[i] * delta[j])
                inertia[i][j] += float(entry["inertia"][i][j]) + shift
    return {
        "mass": total,
        "com": (com[0], com[1], com[2]),
        "inertia": tuple(tuple(row) for row in inertia),
    }


PRODUCT_CONVENTIONS = ("solidworks_positive", "solidworks_standard")
# The scope label names *what was selected*; it is authoritative and must agree
# with any declared convention, so a reading is never re-interpreted by
# silently relabelling historical measurements.
#
# Measured on an analytic native fixture (2026-10-06, SolidWorks 34.0.0,
# C:\m30-fixture-20261006): two rotated boxes with known geometry, known COM and
# non-zero off-diagonals.  The part-document raw matrix equals the analytic
# STANDARD tensor to 4e-20 / 1e-19 (the positive-product hypothesis is off by
# 3.5e-5), and a two-instance group equals the analytic standard parallel-axis
# combination to 6.5e-19 absolute / 2.6e-16 relative (the positive-product
# hypothesis is off by 2.2e-3 / 0.87).  This build therefore answers in
# standard notation for every measured scope; the earlier positive-product
# claim is NOT reproduced and historical readings that declare
# ``solidworks_positive`` keep their own (negating) interpretation.
SCOPE_CONVENTIONS = {
    "part_document": "solidworks_standard",
    "assembly_component_group": "solidworks_standard",
}
FIXTURE_API_MARKERS = ("fixture",)


def tensor_from_raw(raw: Any, reference: Any, *, where: str) -> Matrix3:
    """原始 9 个数（3×3）→ 标准惯性张量，按读数里声明的**惯性积约定**转换。

    ``IMassProperty2.GetMomentOfInertia(0)`` 在 ``solidworks_positive`` 记法下返回
    "正惯性积"：布局是

    ``[[Ixx, Ixy, Izx], [Ixy, Iyy, Iyz], [Izx, Iyz, Izz]]``，其中交叉项是
    ``∫xy dm / ∫zx dm / ∫yz dm``；标准惯性张量的非对角项正是它们的**相反数**。
    这里只转换**推导值**，raw 里的原始读数原样保留。

    缺约定时不猜：夹具读数（``used_api == "fixture"``）按其合同本来就可当标准张量；
    原生 CAD 读数缺 ``product_convention`` 属于无法解释的数据，直接失败。
    """

    rows = [[float(value) for value in row] for row in raw]
    if len(rows) != 3 or any(len(row) != 3 for row in rows):
        raise ConfigError("raw inertia must be a 3x3 matrix", {"where": where, "rows": len(rows)})
    scale = max(1.0, max(abs(value) for row in rows for value in row))
    for i, j in ((0, 1), (0, 2), (1, 2)):
        if abs(rows[i][j] - rows[j][i]) > 1e-12 * scale:
            raise ConfigError("raw inertia matrix must be symmetric", {"where": where, "pair": [i, j]})
    convention = (reference or {}).get("product_convention")
    scope = (reference or {}).get("scope")
    if convention is None and scope is None:
        used_api = str((reference or {}).get("used_api") or "")
        if used_api not in FIXTURE_API_MARKERS:
            raise ConfigError(
                "raw CAD inertia has no product_convention; refusing to guess the sign convention",
                {"where": where, "used_api": used_api},
            )
        return tuple(tuple(row) for row in rows)  # type: ignore[return-value]
    if scope is not None:
        expected = SCOPE_CONVENTIONS.get(str(scope))
        if expected is None:
            raise ConfigError(
                "raw CAD inertia declares an unknown measurement scope",
                {"where": where, "scope": scope, "supported": sorted(SCOPE_CONVENTIONS)},
            )
        if convention is not None and str(convention) != expected:
            raise ConfigError(
                "inertia scope and product_convention disagree",
                {"where": where, "scope": scope, "product_convention": convention, "expected": expected},
            )
        convention = expected
    if convention not in PRODUCT_CONVENTIONS:
        raise ConfigError(
            "raw CAD inertia declares an unsupported product_convention",
            {"where": where, "product_convention": convention, "supported": list(PRODUCT_CONVENTIONS)},
        )
    if convention == "solidworks_standard":
        return tuple(tuple(row) for row in rows)  # type: ignore[return-value]
    for i, j in ((0, 1), (1, 0), (0, 2), (2, 0), (1, 2), (2, 1)):
        rows[i][j] = -rows[i][j]
    return tuple(tuple(row) for row in rows)  # type: ignore[return-value]


class _PartReading:
    """One component's placement and mass properties, mapped into the link frame."""

    product_convention: str

    def __init__(
        self, name: str, placement: Sequence[float], mass: dict[str, Any], link: Matrix3, link_xyz: Vector3
    ) -> None:
        self.name = name
        self.rotation = _matrix_from_row_major(placement, 0)
        self.translation = _translation_from_row_major(placement)
        self.link_rotation = link
        self.link_translation = link_xyz
        self.mass = float(mass["mass"])
        self.cad_mass_kg = float(mass.get("cad_mass_kg", mass["mass"]))
        self.mass_scale = float(mass.get("scale", 1.0))
        part_com = tuple(float(value) for value in mass["com"])
        reference = mass.get("reference") or {}
        self.product_convention = str(reference.get("product_convention") or "fixture_tensor")
        part_inertia = tensor_from_raw(mass["inertia"], reference, where=f"component {name}")
        # part frame -> assembly frame
        assembly_com = _matvec(self.rotation, part_com)  # type: ignore[arg-type]
        self.assembly_com = tuple(assembly_com[i] + self.translation[i] for i in range(3))
        self.assembly_inertia = _rotate_inertia(self.rotation, part_inertia)
        # assembly frame -> link frame
        link_rotation_t = _transpose(link)
        relative = tuple(self.assembly_com[i] - self.link_translation[i] for i in range(3))
        self.link_com = _matvec(link_rotation_t, relative)  # type: ignore[arg-type]
        self.link_inertia = _rotate_inertia(link_rotation_t, self.assembly_inertia)
        self.relative_rotation = _matmul(link_rotation_t, self.rotation)
        self.relative_translation = _matvec(
            link_rotation_t,
            tuple(self.translation[i] - self.link_translation[i] for i in range(3)),  # type: ignore[arg-type]
        )

    def mass_entry(self) -> dict[str, Any]:
        return {"mass": self.mass, "com": self.link_com, "inertia": self.link_inertia}


def assembly_leaf_total(components: Sequence[Any], readings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Combine the raw per-component readings about the assembly COM (parallel axis).

    This is what the assembly *should* weigh if the leaf files on disk are the ones SolidWorks
    measured.  The capture records it next to the assembly document's own reading so a repaired or
    re-materialed leaf (a file that no longer matches the tree the assembly was built from) shows up
    as a difference instead of an invisible undercount.
    """

    entries = []
    for component in components:
        payload = readings.get(str(component.name))
        if payload is None:
            raise ConfigError("a component has no mass property reading", {"component": str(component.name)})
        # The identity link frame keeps the reading in assembly coordinates.
        part = _PartReading(str(component.name), component.transform, payload, identity_matrix(), (0.0, 0.0, 0.0))
        entries.append(part.mass_entry())
    combined = combine_mass_properties(entries)
    return {
        "mass": float(combined["mass"]),
        "com": [float(value) for value in combined["com"]],
        "inertia": [[float(value) for value in row] for row in combined["inertia"]],
    }


def closure_delta(top_level: dict[str, Any], leaf_total: dict[str, Any]) -> dict[str, float]:
    """Difference between the assembly's own reading and the recombined leaf readings."""

    top_mass = float(top_level["mass"])
    leaf_mass = float(leaf_total["mass"])
    com_top = [float(value) for value in top_level["com"]]
    com_leaf = [float(value) for value in leaf_total["com"]]
    inertia_top = [[float(value) for value in row] for row in top_level["inertia"]]
    inertia_leaf = [[float(value) for value in row] for row in leaf_total["inertia"]]
    scale = max(max(abs(value) for row in inertia_top for value in row), 1e-12)
    worst = max(abs(inertia_top[i][j] - inertia_leaf[i][j]) for i in range(3) for j in range(3))
    return {
        "mass_abs": abs(top_mass - leaf_mass),
        "mass_rel": abs(top_mass - leaf_mass) / max(abs(top_mass), abs(leaf_mass), 1e-12),
        "com_abs_max": max(abs(com_top[i] - com_leaf[i]) for i in range(3)),
        "inertia_abs_max": worst,
        "inertia_rel": worst / scale,
    }


FRAME_KEYS = {"xyz", "rpy", "coordinate_system"}


def map_from_row_major(values: Sequence[float]) -> tuple[Matrix3, Vector3]:
    """Split a row-major 4x4 into (rotation, translation)."""

    numbers = [float(value) for value in values]
    if len(numbers) != 16:
        raise ConfigError("a coordinate system must have 16 row-major values", {"values": len(numbers)})
    return _matrix_from_row_major(numbers, 0), _translation_from_row_major(numbers)


def _link_frame(body: dict[str, Any], coordinate_systems: dict[str, Sequence[float]]) -> tuple[Matrix3, Vector3]:
    """The link frame: declared, or re-derived from a CAD coordinate system.

    A frame bound to ``coordinate_system`` is read from the assembly readings, so
    the pipeline and the independent oracle both work from raw CAD rather than
    from an author-supplied number.  A declared xyz/rpy next to it must agree.
    """

    frame = body.get("frame") or {}
    if not frame:
        return identity_matrix(), (0.0, 0.0, 0.0)
    unknown = sorted(set(frame) - FRAME_KEYS)
    if unknown:
        raise ConfigError("a body frame has unknown fields", {"body": body.get("name"), "fields": unknown})
    declared_xyz = frame.get("xyz")
    declared_rpy = frame.get("rpy")
    reference = frame.get("coordinate_system")
    if reference:
        matrix = coordinate_systems.get(str(reference))
        if matrix is None:
            raise ConfigError(
                "body frame references a coordinate system that the capture did not read",
                {"body": body.get("name"), "coordinate_system": reference, "available": sorted(coordinate_systems)},
            )
        rotation, xyz = map_from_row_major(matrix)
        rpy = rpy_from_matrix(rotation)
        if declared_xyz is not None:
            given = tuple(float(value) for value in declared_xyz)
            if any(abs(given[i] - xyz[i]) > 1e-9 for i in range(3)):
                raise ConfigError(
                    "declared frame xyz disagrees with the CAD coordinate system",
                    {"body": body.get("name"), "declared": list(given), "cad": list(xyz)},
                )
        if declared_rpy is not None:
            given_rpy = tuple(float(value) for value in declared_rpy)
            if any(abs(given_rpy[i] - rpy[i]) > 1e-6 for i in range(3)):
                raise ConfigError(
                    "declared frame rpy disagrees with the CAD coordinate system",
                    {"body": body.get("name"), "declared": list(given_rpy), "cad": list(rpy)},
                )
        return rotation, xyz
    xyz_values = tuple(float(value) for value in (declared_xyz or (0.0, 0.0, 0.0)))
    rpy_values = tuple(float(value) for value in (declared_rpy or (0.0, 0.0, 0.0)))
    if len(xyz_values) != 3 or len(rpy_values) != 3:
        raise ConfigError("a body frame needs xyz and rpy", {"body": body.get("name")})
    return _rotation_from_rpy(rpy_values), xyz_values


def _rotation_from_rpy(rpy: Vector3) -> Matrix3:
    roll, pitch, yaw = rpy
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _kinematic_root(cfg: dict[str, Any], link_names: Sequence[str]) -> str | None:
    """The one link that is nobody's child, or ``None`` when that is not unique."""

    children = {str(joint.get("child")) for joint in cfg.get("joints") or []}
    roots = [name for name in link_names if name not in children]
    return roots[0] if len(roots) == 1 else None


def material_unverified(payload: Any) -> bool:
    """原始读数是否明确记录了"这块实体没有经过验证的材料"。

    真实采集会写 ``reference.material_assignment``：材料齐全时是逐实体材料读数，
    没有材料时带 ``unverified_reason``（如 ``cad_material_provenance_missing``）。
    SolidWorks 的默认密度会给出一个"看起来正常"的 CAD 质量，所以这类组件的质量
    只有在 ``documented_table`` 里被显式声明过才可用。夹具/历史读数完全没有这一段时
    这里返回 False：无法证伪就不擅自判失败，但仍由下文的声明覆盖规则约束。
    """

    reference = payload.get("reference") if isinstance(payload, dict) else None
    assignment = reference.get("material_assignment") if isinstance(reference, dict) else None
    return isinstance(assignment, dict) and bool(assignment.get("unverified_reason"))


def check_mass_contract(cfg: dict[str, Any], included: Iterable[str], masses: dict[str, Any]) -> None:
    """质量来源的模式合同（生成侧，基于实际纳入的组件）。

    * ``documented_table``：每个被纳入的组件都必须有声明质量；缺一个即拒绝，
      不能让某个组件悄悄落回 CAD 的默认密度占位值。
    * 任何模式：声明表里出现未被纳入的组件（拼错/改名）即拒绝——静默忽略等于质量没生效。
    * ``cad``：质量只能来自**已验证材料**的读数；原始读数明确记录了材料未验证时拒绝。
    """

    documented = dict(cfg.get("documented_masses") or {})
    included_set = {str(name) for name in included}
    unknown = sorted(set(documented) - included_set)
    if unknown:
        raise ConfigError(
            "documented_masses contains components that are not included in any body",
            {"unknown": unknown[:20]},
        )
    mode = str(cfg.get("material_source") or "cad")
    if mode == "documented_table":
        missing = sorted(included_set - set(documented))
        if missing:
            raise ConfigError(
                "documented_table requires a mass for every included component",
                {"missing": missing[:20]},
            )
        return
    unverified = sorted(name for name in included_set if material_unverified(masses.get(name)))
    if unverified:
        raise ConfigError(
            "CAD mass came from an unverified material (default density is not evidence); "
            "assign materials or declare documented masses",
            {"unverified_material": unverified[:20], "material_source": mode},
        )


def build_scene(
    cfg: dict[str, Any],
    raw_scene: Any,
    geometry_entries: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble ``scene.json`` from the CAD readings and the source definition."""

    placements = {component.name: list(component.transform) for component in raw_scene.components}
    masses = {name: dict(payload) for name, payload in (raw_scene.mass_properties or {}).items()}
    geometry_by_component = {entry["component"]: entry for entry in geometry_entries}
    bodies = list(cfg.get("bodies") or [])
    if not bodies:
        bodies = [
            {"id": component.name, "name": component.name, "components": [component.name]}
            for component in raw_scene.components
        ]
        auto_bodies = True
    else:
        auto_bodies = False

    # 质量模式合同用**实际读数**复核（配置层只能查到声明层）：documented_table 必须覆盖
    # 每个被纳入的组件；任何模式都不允许把"未验证材料"的 CAD 质量当作可用读数。
    included = {str(item) for body in bodies for item in (body.get("components") or [])}
    check_mass_contract(cfg, included, masses)

    links: list[dict[str, Any]] = []
    link_frames: dict[str, tuple[Matrix3, Vector3, str | None]] = {}
    for body in bodies:
        body_id = str(body.get("id") or body.get("name") or "")
        if not body_id:
            raise ConfigError("every body needs an id", {"body": body})
        name = str(body.get("name") or body_id)
        components = [str(item) for item in body.get("components") or []]
        if not components:
            raise ConfigError("every solid body needs at least one component", {"body": body_id})
        unknown = [item for item in components if item not in placements]
        if unknown:
            raise ConfigError(
                "body references components that are not in the assembly",
                {"body": body_id, "unknown": unknown[:10]},
            )
        link_rotation, link_translation = _link_frame(body, raw_scene.coordinate_systems or {})
        link_frames[str(name)] = (
            link_rotation,
            link_translation,
            (body.get("frame") or {}).get("coordinate_system"),
        )
        readings = []
        for component in components:
            payload = masses.get(component)
            if payload is None:
                raise ConfigError(
                    "component has no CAD mass properties; capture materials or declare the body mass explicitly",
                    {"body": body_id, "component": component},
                )
            # scene.json holds what CAD read.  Declared masses are applied later,
            # by load_scene, and only there do they become "used" values.
            payload = dict(payload, mass_source="cad")
            readings.append(_PartReading(component, placements[component], payload, link_rotation, link_translation))
        combined = combine_mass_properties([reading.mass_entry() for reading in readings])
        visuals = []
        for reading in readings:
            entry = geometry_by_component.get(reading.name)
            if entry is None:
                continue
            visuals.append(
                {
                    "kind": "mesh",
                    "filename": entry["path"],
                    "scale": [1.0, 1.0, 1.0],
                    "xyz": list(reading.relative_translation),
                    "rpy": list(rpy_from_matrix(reading.relative_rotation)),
                }
            )
        links.append(
            {
                "id": body_id,
                "name": name,
                "inertial": {
                    "mass": combined["mass"],
                    "xyz": list(combined["com"]),
                    "rpy": [0.0, 0.0, 0.0],
                    "inertia": _inertia6(combined["inertia"]),
                },
                "visuals": visuals,
                "collisions": [],
                "provenance": {
                    "source_entities": components,
                    "mass": "raw/mass_properties.json",
                    "parts": [
                        {
                            "id": reading.name,
                            "mass_kg": reading.cad_mass_kg,
                            "com_link_m": list(reading.link_com),
                            "inertia_link": _inertia6(reading.link_inertia),
                            "product_convention": reading.product_convention,
                        }
                        for reading in readings
                    ],
                    "combination": "parallel_axis_from_part_readings",
                    "link_frame": (
                        "assembly"
                        if _is_identity(link_rotation)
                        else ("cad_coordinate_system:" + str((body.get("frame") or {}).get("coordinate_system")))
                        if (body.get("frame") or {}).get("coordinate_system")
                        else "author_declared"
                    ),
                    "kinematics": "not_defined" if auto_bodies else "declared",
                },
            }
        )

    joint_entries = _build_joints(cfg, {link["name"] for link in links}, link_frames)
    frames = _build_frames(cfg, {link["name"] for link in links}, raw_scene, link_frames)
    # The root link's frame is where the model's world coordinates start.  The
    # world-frame oracle needs it to evaluate the q=0 chain, so it is written
    # here instead of being reconstructed by whoever reads the snapshot.
    world_from_root: dict[str, Any] | None = None
    root_name = _kinematic_root(cfg, [link["name"] for link in links])
    if root_name is not None:
        root_body = next((body for body in bodies if str(body.get("name") or body.get("id")) == root_name), None)
        if root_body is not None:
            rotation_root, translation_root = _link_frame(root_body, raw_scene.coordinate_systems or {})
            reference = (root_body.get("frame") or {}).get("coordinate_system")
            world_from_root = {
                "xyz": [float(value) for value in translation_root],
                "rpy": [float(value) for value in rpy_from_matrix(rotation_root)],
                "source": (
                    f"cad_coordinate_system:{reference}"
                    if reference
                    else ("author_declared" if root_body.get("frame") else "identity")
                ),
            }
    return {
        "schema_version": SCENE_SCHEMA,
        "name": cfg.get("robot_name", "robot"),
        "units": "SI",
        "links": links,
        "joints": joint_entries,
        "frames": frames,
        "actuators": list(cfg.get("actuators") or []),
        "sensors": list(cfg.get("sensors") or []),
        "constraints": list(cfg.get("constraints") or []),
        "control": dict(cfg.get("control") or {}),
        "contact_excludes": list(cfg.get("contact_excludes") or []),
        "provenance": {
            "provider": "solidworks",
            "assembly": cfg.get("assembly"),
            "configuration": cfg.get("configuration"),
            "bodies_source": "auto_from_top_level_components" if auto_bodies else "source.bodies",
            "coordinate_systems_read": sorted((raw_scene.coordinate_systems or {}).keys()),
            # every native entity that a consumer must account for; the common
            # layer checks that the links below cover exactly this set
            "expected_entities": sorted(placements),
            "world_from_root": world_from_root,
        },
    }


def _build_joints(
    cfg: dict[str, Any],
    link_names: Iterable[str],
    link_frames: dict[str, tuple[Matrix3, Vector3, str | None]] | None = None,
) -> list[dict[str, Any]]:
    """Build canonical joints.

    v1 joints carry no authored origin numbers: the joint frame is the child
    body's named CAD datum at zero, so the relative origin/RPY is derived from
    the raw parent and child link frames.  The legacy explicit ``xyz``/``rpy``
    form stays available for retained sources and is still validated exactly.
    """

    known = set(link_names)
    joints: list[dict[str, Any]] = []
    for joint in cfg.get("joints") or []:
        joint_id = str(joint.get("id") or joint.get("name") or "")
        if not joint_id:
            raise ConfigError("every joint needs an id", {"joint": joint})
        parent = str(joint.get("parent") or "")
        child = str(joint.get("child") or "")
        if parent not in known or child not in known:
            raise ConfigError("joint references an unknown link", {"joint": joint_id, "parent": parent, "child": child})
        joint_type = str(joint.get("type") or "")
        if joint_type not in ("revolute", "prismatic", "fixed", "continuous"):
            raise ConfigError("joint.type must be revolute, prismatic, continuous or fixed", {"joint": joint_id})
        declared_xyz = joint.get("xyz")
        declared_rpy = joint.get("rpy")
        if declared_xyz is None and declared_rpy is None:
            frames = link_frames or {}
            if parent not in frames or child not in frames:
                raise ConfigError(
                    "joint geometry needs either explicit xyz/rpy or CAD-bound body frames",
                    {"joint": joint_id, "parent": parent, "child": child},
                )
            parent_rotation, parent_translation, parent_datum = frames[parent]
            child_rotation, child_translation, child_datum = frames[child]
            inverse_parent = _transpose(parent_rotation)
            relative_rotation = _matmul(inverse_parent, child_rotation)
            relative_translation = _matvec(
                inverse_parent,
                (
                    child_translation[0] - parent_translation[0],
                    child_translation[1] - parent_translation[1],
                    child_translation[2] - parent_translation[2],
                ),
            )
            entry_xyz = [float(value) for value in relative_translation]
            entry_rpy = [float(value) for value in rpy_from_matrix(relative_rotation)]
            geometry_source = "cad_body_frames"
            geometry_detail: dict[str, Any] = {"parent_frame": parent_datum, "child_frame": child_datum}
        else:
            for key, value in (("xyz", declared_xyz), ("rpy", declared_rpy)):
                if not isinstance(value, (list, tuple)) or len(value) != 3:
                    raise ConfigError(
                        "joint geometry must be declared explicitly as xyz/rpy in the parent link frame",
                        {"joint": joint_id, "field": key},
                    )
            entry_xyz = [float(value) for value in declared_xyz]
            entry_rpy = [float(value) for value in declared_rpy]
            geometry_source = "source.joints"
            geometry_detail = {}
        movable = joint_type != "fixed"
        axis = joint.get("axis")
        if movable and (not isinstance(axis, (list, tuple)) or len(axis) != 3):
            raise ConfigError("movable joints need an explicit axis", {"joint": joint_id})
        entry: dict[str, Any] = {
            "id": joint_id,
            "name": str(joint.get("name") or joint_id),
            "type": joint_type,
            "parent": parent,
            "child": child,
            "xyz": entry_xyz,
            "rpy": entry_rpy,
            "provenance": {"geometry": geometry_source, **geometry_detail},
        }
        axis_reference = joint.get("axis_reference")
        if axis_reference is not None:
            entry["provenance"]["axis_reference"] = str(axis_reference)
        limit_evidence = joint.get("limit_evidence")
        if isinstance(limit_evidence, dict):
            entry["provenance"]["limits_evidence"] = {
                str(key): str(value) for key, value in limit_evidence.items()
            }
        if movable:
            entry["axis"] = [float(value) for value in axis]
        limits = joint.get("limits")
        if limits:
            entry["limits"] = {str(key): float(value) for key, value in limits.items()}
        elif movable:
            raise ConfigError("movable joints need explicit limits; none are invented", {"joint": joint_id})
        dynamics = joint.get("dynamics")
        if dynamics:
            entry["dynamics"] = {str(key): float(value) for key, value in dynamics.items()}
        joints.append(entry)
    return joints


def _build_frames(
    cfg: dict[str, Any],
    link_names: Iterable[str],
    raw_scene: Any,
    link_frames: dict[str, tuple[Matrix3, Vector3, str | None]] | None = None,
) -> list[dict[str, Any]]:
    """Named frames are native *world* datums; URDF frames are parent-relative."""

    known = set(link_names)
    frames: list[dict[str, Any]] = []
    for frame in cfg.get("frames") or []:
        frame_id = str(frame.get("id") or frame.get("name") or "")
        parent = str(frame.get("parent") or "")
        if not frame_id or parent not in known:
            raise ConfigError("every frame needs an id and a known parent link", {"frame": frame})
        reference = frame.get("coordinate_system")
        if reference:
            matrix = (raw_scene.coordinate_systems or {}).get(str(reference))
            if matrix is None:
                raise ConfigError(
                    "frame references a coordinate system that the capture did not read",
                    {"frame": frame_id, "coordinate_system": reference},
                )
            rotation, translation = map_from_row_major(matrix)
            frames_map = link_frames or {}
            if parent not in frames_map:
                raise ConfigError(
                    "frame parent has no captured link frame to be expressed against",
                    {"frame": frame_id, "parent": parent},
                )
            parent_rotation, parent_translation, _parent_datum = frames_map[parent]
            inverse_parent = _transpose(parent_rotation)
            rotation = _matmul(inverse_parent, rotation)
            translation = _matvec(
                inverse_parent,
                (
                    translation[0] - parent_translation[0],
                    translation[1] - parent_translation[1],
                    translation[2] - parent_translation[2],
                ),
            )
            xyz = [float(value) for value in translation]
            rpy = [float(value) for value in rpy_from_matrix(rotation)]
            geometry = f"cad_coordinate_system:{reference}"
        else:
            xyz = [float(value) for value in frame.get("xyz", (0.0, 0.0, 0.0))]
            rpy = [float(value) for value in frame.get("rpy", (0.0, 0.0, 0.0))]
            geometry = "source.frames"
        frames.append(
            {
                "id": frame_id,
                "name": str(frame.get("name") or frame_id),
                "parent": parent,
                "xyz": xyz,
                "rpy": rpy,
                "provenance": {"geometry": geometry},
            }
        )
    return frames


def load_scene(snapshot_root: Path) -> dict[str, Any]:
    """Read a frozen snapshot and return its scene, after verifying the snapshot.

    The snapshot itself stays raw.  Declared author decisions - currently the
    documented mass table - are applied *here*: every applied value is recorded
    per link with its raw reading, used value, reason and evidence, so a reader
    can always tell a CAD reading from a declared replacement.
    """

    from ..snapshot import verify_snapshot

    root = Path(snapshot_root).resolve()
    manifest = verify_snapshot(root)
    scene_path = root / str(manifest.get("scene") or "scene.json")
    if not scene_path.is_file():
        raise ConfigError("snapshot has no scene.json", {"snapshot": str(root)})
    scene = read_json(scene_path)
    if not isinstance(scene, dict) or scene.get("schema_version") != SCENE_SCHEMA:
        raise ConfigError("unexpected scene schema", {"snapshot": str(root)})
    if scene.get("units") != "SI":
        raise ConfigError("only SI scenes are supported", {"units": scene.get("units")})
    declared = _read_declared_masses(root)
    if declared:
        _apply_declared_masses(scene, declared)
    provenance = dict(scene.get("provenance") or {})
    provenance["snapshot"] = {
        "kind": manifest.get("kind"),
        "evidence_class": manifest.get("evidence_class"),
        "identity": manifest.get("identity"),
        "files": len(manifest.get("files") or {}),
    }
    scene["provenance"] = provenance
    return scene


def _read_declared_masses(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "raw" / "declared_masses.json"
    if not path.is_file():
        return {}
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ConfigError("declared mass table must be an object", {"path": str(path)})
    items = payload.get("items") or {}
    if not isinstance(items, dict):
        raise ConfigError("declared mass table items must be an object", {"path": str(path)})
    table: dict[str, dict[str, Any]] = {}
    for name, entry in items.items():
        if not isinstance(entry, dict) or "mass_kg" not in entry:
            raise ConfigError("declared mass entries need mass_kg", {"component": name})
        table[str(name)] = entry
    return table


def _apply_declared_masses(scene: dict[str, Any], declared: dict[str, dict[str, Any]]) -> None:
    """Replace declared component masses and scale their inertia accordingly.

    惯量是**按质量整体缩放的 CAD 张量**：保留"几何均匀密度"的质量分布形状，
    只改总量级。这里把该假设写进 provenance（``inertia_model``），避免下游把它当实测；
    目录件（电机/电池/电子）的真实质量分布与几何形状不同，缩放值只作量级估计。
    """

    if declared:
        known_parts = {
            str(part.get("id"))
            for link in scene.get("links") or []
            for part in (link.get("provenance") or {}).get("parts") or []
            if isinstance(part, dict)
        }
        unknown = sorted(set(declared) - known_parts)
        if unknown:
            raise ConfigError(
                "declared mass table has entries that match no component (typo or renamed instance)",
                {"unknown": unknown[:20]},
            )
    for link in scene.get("links") or []:
        provenance = dict(link.get("provenance") or {})
        if provenance.get("mass") == "source.documented_masses":
            # Already normalized: applying the table twice must not scale twice.
            continue
        parts = provenance.get("parts")
        if not isinstance(parts, list) or not parts:
            continue
        applied: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        sources: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            entry = declared.get(str(part.get("id")))
            raw_mass = float(part.get("mass_kg", 0.0))
            scale = 1.0
            used_mass = raw_mass
            sources.append("documented" if entry is not None else "cad")
            if entry is not None:
                used_mass = float(entry["mass_kg"])
                if raw_mass <= 0.0:
                    raise ConfigError(
                        "cannot scale inertia for a component whose CAD mass is not positive",
                        {"component": part.get("id"), "raw_mass_kg": raw_mass},
                    )
                scale = used_mass / raw_mass
                records.append(
                    {
                        "component": part.get("id"),
                        "raw_mass_kg": raw_mass,
                        "used_mass_kg": used_mass,
                        "scale": scale,
                        "reason": str(entry.get("reason") or "declared by the model author"),
                        "evidence": entry.get("evidence"),
                    }
                )
            inertia = [
                [
                    float(part["inertia_link"][0]) * scale,
                    float(part["inertia_link"][1]) * scale,
                    float(part["inertia_link"][2]) * scale,
                ],
                [
                    float(part["inertia_link"][1]) * scale,
                    float(part["inertia_link"][3]) * scale,
                    float(part["inertia_link"][4]) * scale,
                ],
                [
                    float(part["inertia_link"][2]) * scale,
                    float(part["inertia_link"][4]) * scale,
                    float(part["inertia_link"][5]) * scale,
                ],
            ]
            applied.append({"mass": used_mass, "com": part.get("com_link_m"), "inertia": inertia})
        if not applied:
            continue
        combined = combine_mass_properties(applied)
        link["inertial"] = {
            "mass": combined["mass"],
            "xyz": list(combined["com"]),
            "rpy": [0.0, 0.0, 0.0],
            "inertia": _inertia6(combined["inertia"]),
        }
        if records:
            provenance["mass"] = "source.documented_masses"
            provenance["declared_masses"] = records
            provenance["inertia_model"] = "scaled_cad_uniform_density"
            provenance["inertia_model_scope"] = (
                "声明质量按 used/raw 整体缩放 CAD 的均匀密度张量：保留几何形状假设，"
                "不等于实测惯量；打印件可作量级估计，目录件（电机/电池/电子）不成立"
            )
        provenance["mass_sources"] = sorted(set(sources))
        link["provenance"] = provenance


def normalize_scene(
    scene: dict[str, Any],
    definition: dict[str, Any] | None = None,
    snapshot_root: Path | None = None,
) -> dict[str, Any]:
    """Pipeline hook: apply declared author decisions to a *raw* scene.

    The shared pipeline reads ``scene.json`` with the common loader (raw CAD
    readings) and calls this hook during normalization.  The hook is pure - the
    caller's raw scene is not modified - and idempotent, and every applied value
    is recorded per link with raw / used / reason / evidence.
    """

    import copy

    data = copy.deepcopy(scene)
    declared_from_definition: dict[str, dict[str, Any]] = {}
    source = (definition or {}).get("source") or {}
    raw_table = source.get("documented_masses") or {}
    if isinstance(raw_table, dict):
        for name, entry in raw_table.items():
            if isinstance(entry, dict) and "mass_kg" in entry:
                declared_from_definition[str(name)] = entry
            elif isinstance(entry, (int, float)) and not isinstance(entry, bool):
                declared_from_definition[str(name)] = {
                    "mass_kg": float(entry),
                    "reason": "declared by the model author",
                    "evidence": None,
                }
    declared = declared_from_definition
    if not declared and snapshot_root is not None:
        declared = _read_declared_masses(Path(snapshot_root))
    if declared:
        _apply_declared_masses(data, declared)
    return data
