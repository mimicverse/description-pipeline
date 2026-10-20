"""Turn raw CAD readings into the shared ``solidworks-to-urdf.scene/v1`` structure.

The adapter owns the *source-side* derivation: component placements, per-part
mass properties and exported geometry are read from CAD, then expressed in the
link frames the source config declares.  No value is copied from a final URDF,
and nothing is invented when the definition is incomplete - a missing frame or
joint definition is an error, not a default.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .jsonio import read_json
from .naming import asset_namespace, material_name, mesh_path, visual_name
from .native_limits import PREVIEW_POLICY_ID
from ...schemas import LINK_GROUP_MASS_SCHEMA as LINK_GROUP_SCHEMA

SCENE_SCHEMA = "solidworks-to-urdf.scene/v1"


def scene_schema_supported(value: object) -> bool:
    """Whether a scene declares the one current schema id."""

    return value == SCENE_SCHEMA


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


#: Row-major identity placement for readings already expressed in assembly axes.
IDENTITY_PLACEMENT = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)


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
    # The magnitude of the projected x axis is the cosine of pitch without
    # passing through asin.  Using hypot keeps the branch decision stable for
    # a matrix that is already at the gimbal boundary, while atan2 keeps the
    # pitch finite and signed on either side of it.
    cos_pitch = math.hypot(matrix[0][0], matrix[1][0])
    pitch = math.atan2(-matrix[2][0], cos_pitch)
    if cos_pitch > 1e-9:
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
    result derives usable semantics from the snapshot, and its provenance
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


PRODUCT_CONVENTIONS = ("solidworks_standard",)
# The native analytic fixture (SolidWorks 34.0.0, 2026-10-06) verifies rotated
# boxes and a solid cylinder with nonzero cross terms. Part tensors agree with
# the standard signed tensor within 1.5e-19 kg*m^2; the full assembly agrees
# within 8.7e-19. Other conventions are rejected.
SCOPE_CONVENTIONS = {
    "part_document": "solidworks_standard",
    "assembly_component_group": "solidworks_standard",
}


def tensor_from_raw(raw: Any, reference: Any, *, where: str) -> Matrix3:
    """Preserve the measured signed 3×3 tensor and reject other conventions."""

    rows = [[float(value) for value in row] for row in raw]
    if len(rows) != 3 or any(len(row) != 3 for row in rows):
        raise ConfigError("raw inertia must be a 3x3 matrix", {"where": where, "rows": len(rows)})
    scale = max(1.0, max(abs(value) for row in rows for value in row))
    for i, j in ((0, 1), (0, 2), (1, 2)):
        if abs(rows[i][j] - rows[j][i]) > 1e-12 * scale:
            raise ConfigError("raw inertia matrix must be symmetric", {"where": where, "pair": [i, j]})
    convention = (reference or {}).get("product_convention")
    scope = (reference or {}).get("scope")
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
        reference = mass.get("reference") or {}
        raw_inertia = tensor_from_raw(mass["inertia"], reference, where=f"component {name}")
        self.product_convention = "solidworks_standard"
        if str(reference.get("scope") or "part_document") == "assembly_component_group":
            # Effective CAD-authored instance measurement (sheet-only fallback
            # or overridden instance): the CoM and tensor are recorded about
            # the CoM in assembly document coordinates and are used verbatim
            # in the assembly frame.  No part coordinates are invented and
            # nothing is rescaled; the independent verifier derives the same
            # assembly-frame values from the raw row and the recorded
            # placement.
            self.assembly_com = tuple(float(value) for value in mass["com"])
            self.assembly_inertia = raw_inertia
        else:
            part_com = tuple(float(value) for value in mass["com"])
            # part frame -> assembly frame
            assembly_com = _matvec(self.rotation, part_com)  # type: ignore[arg-type]
            self.assembly_com = tuple(assembly_com[i] + self.translation[i] for i in range(3))
            self.assembly_inertia = _rotate_inertia(self.rotation, raw_inertia)
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


def map_from_row_major(values: Sequence[float]) -> tuple[Matrix3, Vector3]:
    """Split a row-major 4x4 into (rotation, translation)."""

    numbers = [float(value) for value in values]
    if len(numbers) != 16:
        raise ConfigError("a coordinate system must have 16 row-major values", {"values": len(numbers)})
    return _matrix_from_row_major(numbers, 0), _translation_from_row_major(numbers)


def _link_frame(body: dict[str, Any], coordinate_systems: dict[str, Sequence[float]]) -> tuple[Matrix3, Vector3]:
    """Read the body frame exclusively from its captured native datum."""

    frame = body.get("frame")
    if not isinstance(frame, dict) or set(frame) != {"coordinate_system"}:
        raise ConfigError("every body frame must name one native CAD datum", {"body": body.get("name")})
    reference = frame["coordinate_system"]
    if not isinstance(reference, str) or not reference:
        raise ConfigError("body coordinate_system must be a nonempty name", {"body": body.get("name")})
    matrix = coordinate_systems.get(reference)
    if matrix is None:
        raise ConfigError(
            "body frame references a coordinate system that the capture did not read",
            {"body": body.get("name"), "coordinate_system": reference, "available": sorted(coordinate_systems)},
        )
    return map_from_row_major(matrix)


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
    """Whether a raw reading explicitly records that its material is unverified.

    Real captures write ``reference.material_assignment``: a complete assignment is
    a per-entity material reading, while a missing assignment carries an
    ``unverified_reason`` such as ``cad_material_provenance_missing``. SolidWorks
    default density can produce a plausible-looking CAD mass, so such a reading is
    not verified mass: in CAD mode ``build_scene`` preserves the value and records
    a non-blocking warning; in documented-table mode the declared table must
    replace it. Fixtures and historical readings without this block return False,
    but declaration coverage rules still apply.
    """

    reference = payload.get("reference") if isinstance(payload, dict) else None
    assignment = reference.get("material_assignment") if isinstance(reference, dict) else None
    return isinstance(assignment, dict) and bool(assignment.get("unverified_reason"))


def check_mass_contract(cfg: dict[str, Any], included: Iterable[str], masses: dict[str, Any]) -> list[str]:
    """Validate the mass-source contract against the components actually included.

    * ``documented_table`` requires a declared mass for every included component.
    * Every mode refuses declared entries for components outside the included set;
      silently ignoring a misspelled or renamed key would remove its effect.
    * In ``cad`` mode an unverified material is a warning-level defect. Readings and
      geometry remain unchanged, and the component names are returned for a
      non-blocking warning that independent verification requires downstream.
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
        return []
    return sorted(name for name in included_set if material_unverified(masses.get(name)))


def _material_warnings(unverified: Sequence[str], masses: dict[str, Any]) -> list[dict[str, Any]]:
    """Emit non-blocking warnings for CAD mass backed by unverified material."""

    reasons = sorted(
        {
            str(
                (((masses.get(name) or {}).get("reference") or {}).get("material_assignment") or {}).get(
                    "unverified_reason"
                )
                or ""
            )
            for name in unverified
        }
        - {""}
    )
    return [
        {
            "code": "cad_material_provenance_missing",
            "message": (
                "CAD mass was measured from solids without an explicit physical material; "
                "the document's default density is not evidence, and the values are kept "
                "exactly as measured"
            ),
            "components": [str(name) for name in unverified],
            "unverified_reasons": reasons,
            "blocking": False,
        }
    ]


def _tensor_delta(actual: Matrix3, reference: Matrix3) -> tuple[float, float]:
    worst = 0.0
    scale = 0.0
    for row in range(3):
        for column in range(3):
            worst = max(worst, abs(float(actual[row][column]) - float(reference[row][column])))
            scale = max(scale, abs(float(reference[row][column])))
    return worst, worst / max(scale, 1e-30)


def _direct_link_reads(
    raw_scene: Any, bodies: Sequence[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate the recorded direct link group reads; read 1 is authoritative."""

    record = getattr(raw_scene, "link_group_mass_properties", None)
    if not isinstance(record, dict) or record.get("schema_version") != LINK_GROUP_SCHEMA:
        raise ConfigError(
            "the capture carries no direct link group mass properties record",
            {"required": "raw/link_group_mass_properties.json", "schema": LINK_GROUP_SCHEMA},
        )
    links = record.get("links")
    expected = {str(body.get("name") or body.get("id") or "") for body in bodies}
    record_names = {
        str(body.get("name") or body.get("id") or ""): str(
            body.get("source_name") or body.get("name") or body.get("id") or ""
        )
        for body in bodies
    }
    if not isinstance(links, dict) or set(links) != set(record_names.values()):
        raise ConfigError(
            "the link group record does not cover exactly the configured bodies",
            {"expected": sorted(record_names.values()), "recorded": sorted(links or [])},
        )
    meta = {
        "assembly": str(record.get("assembly") or ""),
        "configuration": str(record.get("configuration") or ""),
        "used_api": str(record.get("used_api") or ""),
        "selection_rule": str(record.get("selection_rule") or ""),
    }
    reads: dict[str, dict[str, Any]] = {}
    for name in sorted(expected):
        entry = links[record_names[name]]
        selection = entry.get("selection") if isinstance(entry, dict) else None
        rows = entry.get("reads") if isinstance(entry, dict) else None
        if (
            not isinstance(selection, list)
            or not selection
            or any(not isinstance(item, str) or not item for item in selection)
        ):
            raise ConfigError("a link group selection is malformed", {"body": name})
        if not isinstance(rows, list) or len(rows) != 2:
            raise ConfigError("a link group record must carry two reads", {"body": name})
        read = rows[0]
        if not isinstance(read, dict):
            raise ConfigError("a direct link read is malformed", {"body": name})
        mass = read.get("mass_kg")
        com = read.get("com_m")
        tensor = read.get("inertia_tensor_about_com")
        if type(mass) not in (int, float) or not math.isfinite(float(mass)) or float(mass) <= 0.0:
            raise ConfigError("a direct link read has an invalid mass", {"body": name})
        if (
            not isinstance(com, list)
            or len(com) != 3
            or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in com)
        ):
            raise ConfigError("a direct link read has an invalid COM", {"body": name})
        if (
            not isinstance(tensor, list)
            or len(tensor) != 3
            or any(
                not isinstance(row, list)
                or len(row) != 3
                or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in row)
                for row in tensor
            )
        ):
            raise ConfigError("a direct link read has an invalid inertia tensor", {"body": name})
        reads[name] = {"selection": list(selection), "read": read}
    return meta, reads


def build_scene(
    cfg: dict[str, Any],
    raw_scene: Any,
    geometry_entries: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the portable scene from sealed CAD readings and source definition."""

    placements = {component.name: list(component.transform) for component in raw_scene.components}
    masses = {name: dict(payload) for name, payload in (raw_scene.mass_properties or {}).items()}
    geometry_by_component = {entry["component"]: entry for entry in geometry_entries}
    bodies = list(cfg.get("bodies") or [])
    if not bodies:
        raise ConfigError("native discovery must define the rigid bodies")
    mesh_namespace = asset_namespace(
        Path(str(cfg.get("assembly") or cfg.get("robot_name") or "robot")).name
    )

    # Recheck the mass-mode contract against actual readings: documented_table must
    # cover every included component; CAD readings remain unchanged but carry warnings.
    included = {str(item) for body in bodies for item in (body.get("components") or [])}
    unverified_material = check_mass_contract(cfg, included, masses)
    # The direct one-shot link reads are the authoritative link inertials.  The
    # recombination below stays only as an audit block; a capture without the
    # record fails closed (one supported schema, no fallback path).
    direct_meta, direct_links = _direct_link_reads(raw_scene, bodies)

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
            # This initial scene holds what CAD read. Declared masses are applied later,
            # by load_scene, and only there do they become "used" values.
            contextual = ((getattr(raw_scene, "notes", None) or {}).get("effective_instances") or {}).get(component)
            if (
                isinstance(contextual, dict)
                and (contextual.get("reference") or {}).get("measurement_basis") == "cad_assembly_instance_measurement"
            ):
                payload = contextual
            payload = dict(payload, mass_source="cad")
            readings.append(_PartReading(component, placements[component], payload, link_rotation, link_translation))
        captured_effective = (getattr(raw_scene, "notes", None) or {}).get("effective_instances") or {}
        effective_instances = {
            str(instance): dict(reading)
            for instance, reading in captured_effective.items()
            if isinstance(reading, dict)
        }
        all_leaves = {str(component.name) for component in (raw_scene.components or [])}
        member_set = set(components)
        candidates = []
        for instance, reading in sorted(effective_instances.items()):
            if str(reading.get("document_type") or "") != "assembly":
                continue
            full_descendants = {leaf for leaf in all_leaves if leaf == instance or leaf.startswith(instance + "/")}
            local = full_descendants & member_set
            if not local or full_descendants != local:
                # A parent substitutes only when its ENTIRE descendant leaf
                # set lies in this rigid body; otherwise the leaf readings
                # stay and the closure records the explicit blocker.
                continue
            candidates.append((instance, local, reading))
        top_most = [
            (instance, local, reading)
            for instance, local, reading in candidates
            if not any(
                other != instance and (instance == other or instance.startswith(other + "/"))
                for other, _local, _reading in candidates
            )
        ]
        covered: set[str] = set()
        entries = []
        for instance, local, reading in top_most:
            covered |= local
            # The shallowest overridden ancestor is recorded in assembly document
            # axes (scope assembly_component_group), so it must enter the link
            # frame exactly like a leaf reading: with an identity placement. Using
            # the component's own transform here would map assembly coordinates a
            # second time and displace the delivered COM of every rotating link.
            substituted = _PartReading(str(instance), IDENTITY_PLACEMENT, reading, link_rotation, link_translation)
            entries.append(substituted.mass_entry())
        entries.extend(reading.mass_entry() for reading in readings if reading.name not in covered)
        combined = combine_mass_properties(entries)
        direct = direct_links[name]
        read = direct["read"]
        link_rotation_t = _transpose(link_rotation)
        direct_mass = float(read["mass_kg"])
        direct_com_link = _matvec(
            link_rotation_t,
            tuple(float(value) - link_translation[index] for index, value in enumerate(read["com_m"])),
        )
        direct_inertia_link = _rotate_inertia(
            link_rotation_t, [[float(value) for value in row] for row in read["inertia_tensor_about_com"]]
        )
        tensor_delta_max_abs, tensor_rel = _tensor_delta(combined["inertia"], direct_inertia_link)
        visuals = []
        for reading in readings:
            entry = geometry_by_component.get(reading.name)
            if entry is None:
                continue
            visual = {
                "kind": "mesh",
                "name": visual_name(reading.name, link=name),
                "filename": entry["path"],
                "sha256": entry.get("sha256"),
                "mesh_name": mesh_path(
                    str(entry.get("sha256") or ""),
                    namespace=mesh_namespace,
                ),
                "material": material_name(reading.name, link=name),
                "scale": [1.0, 1.0, 1.0],
                "xyz": list(reading.relative_translation),
                "rpy": list(rpy_from_matrix(reading.relative_rotation)),
                "provenance": {"native_component": reading.name},
            }
            visuals.append(visual)
        links.append(
            {
                "id": body_id,
                "name": name,
                "inertial": {
                    "mass": direct_mass,
                    "xyz": list(direct_com_link),
                    "rpy": [0.0, 0.0, 0.0],
                    "inertia": _inertia6(direct_inertia_link),
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
                    "link_frame": "cad_coordinate_system:" + body["frame"]["coordinate_system"],
                    "kinematics": "native_derived",
                    "mass_properties": {
                        "direct": {
                            "selection": direct["selection"],
                            "selection_rule": direct_meta["selection_rule"],
                            "used_api": direct_meta["used_api"],
                            "source": "raw/link_group_mass_properties.json",
                        },
                        "recombination": {
                            "mass_kg": float(combined["mass"]),
                            "com_link_m": [float(value) for value in combined["com"]],
                            "inertia_link": _inertia6(combined["inertia"]),
                        },
                        "direct_vs_recombination": {
                            "mass_delta_kg": float(combined["mass"]) - direct_mass,
                            "com_delta_m": math.dist(
                                [float(value) for value in combined["com"]],
                                [float(value) for value in direct_com_link],
                            ),
                            "tensor_delta_max_abs": tensor_delta_max_abs,
                            "tensor_rel": tensor_rel,
                        },
                    },
                },
            }
        )
        used_context = {
            component
            for component in components
            if component not in covered
            and ((effective_instances.get(component) or {}).get("reference") or {}).get("measurement_basis")
            == "cad_assembly_instance_measurement"
        }
        effective_names = {instance for instance, _local, _reading in top_most} | used_context
        if effective_names:
            links[-1]["provenance"]["effective_instances"] = sorted(effective_names)
            links[-1]["provenance"]["instance_measurements"] = "raw/effective_instances.json"
            links[-1]["provenance"]["combination"] = "parallel_axis_from_cad_part_and_instance_readings"

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
                "source": f"cad_coordinate_system:{reference}",
            }
    scene_provenance: dict[str, Any] = {
        "provider": "solidworks",
        "assembly": cfg.get("assembly"),
        "configuration": cfg.get("configuration"),
        "bodies_source": "native_derived",
        "coordinate_systems_read": sorted((raw_scene.coordinate_systems or {}).keys()),
        # every native entity that a consumer must account for; the common
        # layer checks that the links below cover exactly this set
        "expected_entities": sorted(included),
        "native_entities": sorted(placements),
        "unassigned_entities": sorted(set(placements) - included),
        "world_from_root": world_from_root,
    }
    value_warnings = _value_warnings(cfg, bodies, links, joint_entries, frames)
    if value_warnings:
        scene_provenance["value_warnings"] = value_warnings
    if unverified_material:
        scene_provenance["warnings"] = _material_warnings(unverified_material, masses)
    return {
        "schema_version": SCENE_SCHEMA,
        "name": cfg.get("robot_name", "robot"),
        "units": "SI",
        "links": links,
        "joints": joint_entries,
        "frames": frames,
        "provenance": scene_provenance,
    }


def _build_joints(
    cfg: dict[str, Any],
    link_names: Iterable[str],
    link_frames: dict[str, tuple[Matrix3, Vector3, str | None]] | None = None,
) -> list[dict[str, Any]]:
    """Derive joint origins from captured parent and child CAD datums."""

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
        if "xyz" in joint or "rpy" in joint:
            raise ConfigError("joint origins must be derived from CAD datums", {"joint": joint_id})
        frames = link_frames or {}
        if parent not in frames or child not in frames:
            raise ConfigError(
                "joint geometry requires captured parent and child CAD datums",
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
        geometry_detail = {"parent_frame": parent_datum, "child_frame": child_datum}
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
            entry["provenance"]["axis_reference"] = (
                dict(axis_reference) if isinstance(axis_reference, dict) else str(axis_reference)
            )
        limit_evidence = joint.get("limit_evidence")
        if isinstance(limit_evidence, dict):
            entry["provenance"]["limits_evidence"] = {str(key): str(value) for key, value in limit_evidence.items()}
        limit_source = joint.get("limit_source")
        if isinstance(limit_source, str) and limit_source:
            entry["limit_source"] = limit_source
        preview_policy = joint.get("preview_policy")
        if isinstance(preview_policy, dict):
            entry["provenance"]["preview_policy"] = dict(preview_policy)
        if movable:
            entry["axis"] = [float(value) for value in axis]
            axis_provenance = joint.get("axis_provenance")
            if isinstance(axis_provenance, dict):
                for key in (
                    "source",
                    "native_direction",
                    "parent_direction",
                    "parent_axis",
                    "assembly_direction",
                    "delivered_local_axis",
                    "direction_relation",
                    "motion",
                    "classification_source",
                    "alignment",
                    "rule_id",
                    "rule_version",
                ):
                    if key in axis_provenance:
                        entry["provenance"][f"axis.{key}"] = axis_provenance[key]
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


def _value_warnings(
    cfg: dict[str, Any],
    bodies: Sequence[dict[str, Any]],
    links: Sequence[dict[str, Any]],
    joints: Sequence[dict[str, Any]],
    frames: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Describe every delivered value whose provenance is not a direct native read."""

    warnings: list[dict[str, Any]] = []
    mappings = []
    motion_unresolved = []
    direction_unresolved = []
    sign_reversals = []
    preview_limits = []
    visuals = []
    materials = []
    mesh_paths = []
    for body in bodies:
        if body.get("source_name") and body["source_name"] != body.get("name"):
            rule = body.get("name_rule") if isinstance(body.get("name_rule"), dict) else {}
            mappings.append(
                {
                    "object_type": "link",
                    "object": body["name"],
                    "field": "name",
                    "actual": body["name"],
                    "source": f"CAD-derived interface naming for {body['source_name']}",
                    "original": body["source_name"],
                    "rule_id": rule.get("rule_id", "naming.interface"),
                    "rule_version": rule.get("rule_version", "naming-v1"),
                    "reason": rule.get("reason", "CAD datum formatting"),
                }
            )
    source_joints = cfg.get("joints") or []
    for index, joint in enumerate(joints):
        source_joint = source_joints[index] if index < len(source_joints) else {}
        source_name = str(source_joint.get("source_name") or source_joint.get("name") or joint["name"])
        if source_name != joint["name"]:
            rule = source_joint.get("name_rule") if isinstance(source_joint.get("name_rule"), dict) else {}
            mappings.append(
                {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": "name",
                    "actual": joint["name"],
                    "source": f"CAD-derived interface naming for {source_name}",
                    "original": source_name,
                    "rule_id": rule.get("rule_id", "naming.interface"),
                    "rule_version": rule.get("rule_version", "naming-v1"),
                    "reason": rule.get("reason", "CAD reference-axis formatting"),
                }
            )
        provenance = source_joint.get("axis_provenance")
        if isinstance(provenance, dict):
            native_axis = provenance.get("native_direction")
            output_axis = provenance.get("direction")
            if (
                provenance.get("direction_relation") == "author_sign_applied"
                and isinstance(native_axis, list)
                and isinstance(output_axis, list)
                and len(native_axis) == 3
                and len(output_axis) == 3
                and all(
                    abs(float(before) + float(after)) <= 1e-12
                    for before, after in zip(native_axis, output_axis, strict=True)
                )
            ):
                sign_reversals.append(
                    {
                        "object_type": "joint",
                        "object": joint["name"],
                        "field": "axis",
                        "actual": joint["axis"],
                        "source": "CAD author dp.joint.axis_sign",
                        "original": native_axis,
                        "rule_id": provenance.get("rule_id", "direction.native_axis_sign"),
                        "rule_version": provenance.get("rule_version", "v1"),
                        "reason": (
                            "CAD author axis_sign selects the positive direction of the "
                            "undirected shaft; q and ranges remain in author q"
                        ),
                        "native_joint_identity": source_name,
                        "native_sign": provenance.get("native_sign"),
                        "sign_reversal": provenance.get("sign_reversal"),
                        "sign_warning": provenance.get("sign_warning"),
                        "original_axis_assembly": native_axis,
                        "output_axis_assembly": output_axis,
                        "actual_child_axis": joint["axis"],
                        "original_frame": provenance.get("original_frame", "assembly"),
                        "output_frame": provenance.get("output_frame", "assembly"),
                        "model_frame": "child",
                        "q_sign": 1,
                        "range_transform": "not_applied",
                        "limits_before": source_joint.get("limits"),
                        "limits_after": source_joint.get("limits"),
                        "drive_before": source_joint.get("drive"),
                        "drive_after": source_joint.get("drive"),
                        "warning_does_not_authorize": True,
                        "warning_does_not_replace_evidence": True,
                    }
                )
            direction = {
                "object_type": "joint",
                "object": joint["name"],
                "field": "axis",
                "actual": joint["axis"],
                "source": "CAD signed axis and versioned positive-motion rule",
                "original": provenance.get("parent_axis"),
                "rule_id": provenance.get("rule_id", "direction.positive_semantics"),
                "rule_version": provenance.get("rule_version", "v3"),
                "reason": provenance.get(
                    "reason", provenance.get("direction_relation", provenance.get("status"))
                ),
                "motion": provenance.get("motion"),
            }
            if provenance.get("status", provenance.get("direction_relation")) != "local_class_aligned":
                direction_unresolved.append(direction)
        if source_joint.get("type") != "fixed" and not any(
            word in {"roll", "pitch", "yaw"} for word in joint["name"].split("_")
        ):
            motion_unresolved.append(
                {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": "name",
                    "actual": joint["name"],
                    "source": "neutral joint naming",
                    "original": source_name,
                    "rule_id": "naming.joint_motion",
                    "rule_version": "naming-v1",
                    "reason": "motion classification unavailable",
                }
            )
        if source_joint.get("limit_source") == "preview_policy":
            policy = source_joint.get("preview_policy") or {}
            preview_limits.append(
                {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": "limits",
                    "actual": joint["limits"],
                    "source": "inspection preview full-turn policy",
                    "original": policy.get("author_limits"),
                    "rule_id": PREVIEW_POLICY_ID,
                    "rule_version": "v1",
                    "reason": "inspection preview window; no native mechanical range is claimed",
                    "native_cad_ranges": False,
                    "hardware_qualified": False,
                }
            )
    for frame in cfg.get("frames") or []:
        source_name = str(frame.get("source_name") or "")
        native_suffix = str(frame.get("coordinate_system") or "").partition("_")[2]
        if source_name and native_suffix != frame.get("name"):
            mappings.append(
                {
                    "object_type": "frame",
                    "object": frame.get("name"),
                    "field": "name",
                    "actual": frame.get("name"),
                    "source": f"CAD interface datum formatting for {frame.get('coordinate_system')}",
                    "original": source_name,
                    "rule_id": "naming.frame",
                    "rule_version": "naming-v1",
                    "reason": "CAD interface datum formatting",
                }
            )
    for link in links:
        for visual in link.get("visuals") or []:
            visuals.append(
                {
                    "object_type": "visual",
                    "object": visual["name"],
                    "field": "name",
                    "actual": visual["name"],
                    "source": "native component identity formatting",
                    "original": visual.get("provenance", {}).get("native_component"),
                    "rule_id": "naming.visual",
                    "rule_version": "naming-v1",
                    "reason": "native component identity formatting",
                }
            )
            mesh_paths.append(
                {
                    "object_type": "visual",
                    "object": visual["name"],
                    "field": "mesh filename",
                    "actual": visual["mesh_name"],
                    "source": "current capture content addressing",
                    "original": visual.get("sha256"),
                    "rule_id": "naming.mesh",
                    "rule_version": "naming-v1",
                    "reason": "current capture content addressing",
                }
            )
            if visual.get("material"):
                materials.append(
                    {
                        "object_type": "visual",
                        "object": visual["name"],
                        "field": "material",
                        "actual": visual["material"],
                        "source": "native component identity formatting",
                        "original": visual.get("provenance", {}).get("native_component"),
                        "rule_id": "naming.material",
                        "rule_version": "naming-v1",
                        "reason": "native component identity formatting",
                    }
                )
    if mappings:
        warnings.append(
            {
                "code": "naming.formatted_interface",
                "message": "Link, joint, and frame names are deterministic formatting of native CAD identities.",
                "objects": mappings,
            }
        )
    if motion_unresolved:
        warnings.append(
            {
                "code": "naming.motion_unresolved",
                "message": "Joint names without a CAD motion classification retain a neutral name.",
                "objects": motion_unresolved,
            }
        )
    if direction_unresolved:
        warnings.append(
            {
                "code": "direction.semantics_unresolved",
                "message": "Authored CAD axes are preserved; functional positive motion is not proven.",
                "objects": direction_unresolved,
            }
        )
    if sign_reversals:
        warnings.append(
            {
                "code": "direction.author_sign_applied",
                "message": (
                    "CAD author axis_sign selects the positive direction of the undirected "
                    "shaft; q and ranges remain in author q."
                ),
                "objects": sign_reversals,
            }
        )
    if preview_limits:
        warnings.append(
            {
                "code": "limits.preview_policy",
                "message": (
                    "Inspection preview uses a versioned full-turn rotation window; "
                    "no native mechanical range is claimed."
                ),
                "objects": preview_limits,
            }
        )
    if visuals:
        warnings.append(
            {
                "code": "naming.derived_visual",
                "message": "Visual names are derived from the exact native component occurrence identity.",
                "objects": visuals,
            }
        )
    if mesh_paths:
        warnings.append(
            {
                "code": "naming.content_addressed_mesh",
                "message": "Mesh paths are derived from the current captured STL content digest.",
                "objects": mesh_paths,
            }
        )
    if materials:
        warnings.append(
            {
                "code": "naming.derived_material",
                "message": "Material names are derived from the owner link and native component identity.",
                "objects": materials,
            }
        )
    if cfg.get("robot_name_source") == "cad_filename_derived":
        source_name = str(cfg.get("assembly") or "").replace("\\", "/").rsplit("/", 1)[-1]
        warnings.append(
            {
                "code": "naming.formatted_robot_name",
                "message": "The robot name is formatted from the native top-assembly filename.",
                "objects": [
                    {
                        "object_type": "robot",
                        "object": cfg.get("robot_name"),
                        "field": "name",
                        "actual": cfg.get("robot_name"),
                        "source": "native top assembly filename formatting",
                        "original": source_name,
                        "rule_id": "naming.robot",
                        "rule_version": "naming-v1",
                        "reason": "native top assembly filename formatting",
                    }
                ],
            }
        )
    geometry_objects = [
        {
            "object_type": "link",
            "object": link["name"],
            "fields": ["visual.xyz", "visual.rpy", "visual.filename", "visual.scale"],
            "actual": "derived from native occurrence transforms and capture geometry",
            "source": "native_derived",
        }
        for link in links
    ]
    geometry_objects.extend(
        {
            "object_type": "joint",
            "object": joint["name"],
            "fields": ["xyz", "rpy", "axis"],
            "actual": "derived from native coordinate systems and axis declarations",
            "source": "native_derived",
        }
        for joint in joints
    )
    geometry_objects.extend(
        {
            "object_type": "frame",
            "object": frame["name"],
            "fields": ["xyz", "rpy"],
            "actual": "derived from native coordinate systems",
            "source": "native_derived",
        }
        for frame in frames
    )
    if geometry_objects:
        warnings.append(
            {
                "code": "geometry.derived_from_native",
                "message": "Geometry, frame transforms and signed joint axes are derived from native evidence.",
                "objects": geometry_objects,
            }
        )
    warnings.append(
        {
            "code": "units.si_conversion",
            "message": "Delivered values use SI units after conversion from native CAD units.",
            "objects": [
                {
                    "object_type": "robot",
                    "object": cfg.get("robot_name"),
                    "field": "units",
                    "actual": "SI",
                    "source": "native_derived",
                }
            ],
        }
    )
    physics_objects = [
        {
            "object_type": "link",
            "object": link["name"],
            "fields": ["inertial.mass", "inertial.xyz", "inertial.rpy", "inertial.inertia"],
            "actual": "transformed or recombined from native readings",
            "source": "native_derived",
        }
        for link in links
        if link.get("inertial") is not None
    ]
    if physics_objects:
        warnings.append(
            {
                "code": "physics.derived_from_native",
                "message": "Link mass properties are transformed, translated or recombined from native readings.",
                "objects": physics_objects,
            }
        )
    constants = []
    for link in links:
        for visual in link.get("visuals") or []:
            fields = ["scale"]
            actual = {"scale": visual.get("scale")}
            if visual.get("rgba"):
                fields.append("material.rgba")
                actual["rgba"] = visual["rgba"]
            constants.append(
                {
                    "object_type": "visual",
                    "object": visual["name"],
                    "fields": fields,
                    "actual": actual,
                    "source": "display constant",
                }
            )
    if constants:
        warnings.append(
            {
                "code": "display.constant",
                "message": "Display scale and material color are platform constants, not CAD readings.",
                "objects": constants,
            }
        )
    return warnings


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
        if not reference or "xyz" in frame or "rpy" in frame:
            raise ConfigError("every interface frame must name one native CAD datum", {"frame": frame_id})
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
    """Verify a frozen snapshot and derive its scene on the portable host.

    The snapshot itself stays raw.  Declared author decisions - currently the
    documented mass table - are applied *here*: every applied value is recorded
    per link with its raw reading, used value, reason and evidence, so a reader
    can always tell a CAD reading from a declared replacement.
    """

    from ..snapshot import verify_snapshot

    root = Path(snapshot_root).resolve()
    manifest = verify_snapshot(root)
    from .captured import derive_scene

    scene = derive_scene(root, manifest)
    if not isinstance(scene, dict) or not scene_schema_supported(scene.get("schema_version")):
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
    """Replace declared component masses and scale their inertia consistently."""

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
                "Declared mass scales the CAD uniform-density tensor by used/raw mass; "
                "this preserves the geometric shape assumption and is not measured inertia. "
                "It is only an order-of-magnitude estimate for printed parts and does not "
                "hold for catalog parts such as motors, batteries, or electronics."
            )
        provenance["mass_sources"] = sorted(set(sources))
        link["provenance"] = provenance
