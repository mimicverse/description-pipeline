"""Turn raw CAD readings into the single engineering-model input structure.

The adapter owns the *source-side* derivation: component placements, per-part
mass properties and exported geometry are read from CAD, then expressed in the
link frames the source config declares.  No value is copied from a final URDF,
and nothing is invented when the definition is incomplete - a missing frame or
joint definition is an error, not a default.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .naming import (
    asset_namespace,
    fixed_joint_name,
    material_name,
    mesh_path,
    visual_name,
)
from .native_limits import ENGINEERING_DEFAULT_POLICY_ID, PREVIEW_POLICY_ID
from ...schemas import ENGINEERING_MODEL_SCHEMA, LINK_GROUP_MASS_SCHEMA as LINK_GROUP_SCHEMA


def engineering_model_schema_supported(value: object) -> bool:
    """Whether a payload declares the one current engineering model id."""

    return value == ENGINEERING_MODEL_SCHEMA


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

    Every non-effective part mass reading carries
    ``reference.material_assignment``: a complete assignment is a per-entity
    material reading, while an explicitly missing assignment carries
    ``cad_material_provenance_missing``. Effective instance readings carry their
    own measurement basis and make no part-material claim, so they are the only
    records exempt from this block. Any other missing or malformed evidence fails
    before scene generation.
    """

    if not isinstance(payload, dict):
        raise ConfigError("CAD mass reading must be an object")
    if payload.get("mode") == "effective_instance":
        return False
    reference = payload.get("reference")
    if not isinstance(reference, dict):
        raise ConfigError("CAD part mass reading has no reference block")
    assignment = reference.get("material_assignment")
    if not isinstance(assignment, dict):
        raise ConfigError("CAD part mass reading has no material assignment")
    if "unverified_reason" in assignment:
        if assignment["unverified_reason"] != "cad_material_provenance_missing":
            raise ConfigError(
                "CAD part material assignment has an unsupported unverified reason",
                {"unverified_reason": assignment["unverified_reason"]},
            )
        return True
    bodies = assignment.get("bodies")
    if not isinstance(bodies, list) or not bodies:
        raise ConfigError("CAD part material assignment has no body coverage")
    return False


def unverified_mass_components(included: Iterable[str], masses: dict[str, Any]) -> list[str]:
    """Return CAD mass readings whose material assignment is explicitly unverified."""

    included_set = {str(name) for name in included}
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
    if not isinstance(links, dict) or set(links) != expected:
        raise ConfigError(
            "the link group record does not cover exactly the configured bodies",
            {"expected": sorted(expected), "recorded": sorted(links or [])},
        )
    meta = {
        "assembly": str(record.get("assembly") or ""),
        "configuration": str(record.get("configuration") or ""),
        "used_api": str(record.get("used_api") or ""),
        "selection_rule": str(record.get("selection_rule") or ""),
    }
    reads: dict[str, dict[str, Any]] = {}
    for name in sorted(expected):
        entry = links[name]
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

    # CAD readings remain unchanged and carry material-provenance warnings.
    included = {str(item) for body in bodies for item in (body.get("components") or [])}
    unverified_material = unverified_mass_components(included, masses)
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
                    "component has no CAD mass properties; recapture the native mass properties",
                    {"body": body_id, "component": component},
                )
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
            if (
                "rgba" in entry
                or "material" in entry
                or entry.get("appearance_groups") is not None
            ) and not isinstance(entry.get("appearance_groups"), list):
                raise ConfigError(
                    "This capture schema has no independently verified CAD appearance record",
                    {
                        "component": reading.name,
                        "material": entry.get("material"),
                        "rgba": entry.get("rgba"),
                    },
                )
            groups = entry.get("appearance_groups")
            if groups is not None:
                if not isinstance(groups, list) or not groups:
                    raise ConfigError(
                        "Native appearance groups are empty or malformed",
                        {"component": reading.name},
                    )
                for group in groups:
                    if (
                        not isinstance(group, dict)
                        or not isinstance(group.get("group_id"), str)
                        or len(group["group_id"]) != 16
                        or group.get("rgba") is None
                        or not isinstance(group.get("mesh_sha256"), str)
                        or len(group["mesh_sha256"]) != 64
                        or not isinstance(group.get("face_references"), list)
                        or not group["face_references"]
                        or not isinstance(group.get("triangle_ranges"), list)
                        or not group["triangle_ranges"]
                        or not isinstance(group.get("sources"), list)
                        or not group["sources"]
                    ):
                        raise ConfigError(
                            "Native appearance group is malformed",
                            {"component": reading.name},
                        )
                    identity = (
                        f"{reading.name}:appearance:{group['group_id']}"
                    )
                    visuals.append(
                        {
                            "kind": "mesh",
                            "name": visual_name(identity, link=name),
                            "filename": entry["path"],
                            "sha256": entry["sha"],
                            "mesh_sha256": group["mesh_sha256"],
                            "mesh_name": mesh_path(
                                group["mesh_sha256"],
                                namespace=mesh_namespace,
                            ),
                            "face_references": list(
                                group["face_references"]
                            ),
                            "triangle_ranges": copy.deepcopy(
                                group["triangle_ranges"]
                            ),
                            "rgba": list(group["rgba"]),
                            "material": material_name(
                                identity,
                                link=name,
                            ),
                            "scale": [1.0, 1.0, 1.0],
                            "xyz": list(reading.relative_translation),
                            "rpy": list(
                                rpy_from_matrix(reading.relative_rotation)
                            ),
                            "provenance": {
                                "native_component": reading.name,
                                "material_source": "native_appearance",
                                "appearance": {
                                    "group_id": group["group_id"],
                                    "face_references": list(
                                        group["face_references"]
                                    ),
                                    "sources": copy.deepcopy(group["sources"]),
                                },
                            },
                        }
                    )
            else:
                visual = {
                    "kind": "mesh",
                    "name": visual_name(reading.name, link=name),
                    "filename": entry["path"],
                    "sha256": entry["sha"],
                    "mesh_name": mesh_path(
                        entry["sha"],
                        namespace=mesh_namespace,
                    ),
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
        "schema_version": ENGINEERING_MODEL_SCHEMA,
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
            entry["provenance"]["limits_evidence"] = {
                str(key): dict(value) if isinstance(value, dict) else value
                for key, value in limit_evidence.items()
            }
        drive_evidence = joint.get("drive_evidence")
        if isinstance(drive_evidence, dict):
            entry["provenance"]["drive_evidence"] = {
                str(key): dict(value) if isinstance(value, dict) else value
                for key, value in drive_evidence.items()
            }
        limit_source = joint.get("limit_source")
        if isinstance(limit_source, str) and limit_source:
            entry["limit_source"] = limit_source
        preview_policy = joint.get("preview_policy")
        if isinstance(preview_policy, dict):
            entry["provenance"]["preview_policy"] = dict(preview_policy)
        default_policy = joint.get("default_policy")
        if isinstance(default_policy, dict):
            entry["provenance"]["default_policy"] = dict(default_policy)
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
    frame_joint_names = []
    motion_unresolved = []
    direction_unresolved = []
    sign_reversals = []
    preview_limits = []
    default_limits = []
    declared_author_limits = []
    controlled_drive = []
    unqualified_drive = []
    visuals = []
    mesh_paths = []
    native_appearances = []
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
                        "limits_status": "present" if source_joint.get("limits") is not None else "absent",
                        "drive_status": "present" if source_joint.get("drive") is not None else "absent",
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
            # The v3 audit never establishes author-positive semantics.
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
        elif source_joint.get("limit_source") == "default_policy":
            policy = source_joint.get("default_policy") or {}
            default_limits.append(
                {
                    "object_type": "joint",
                    "object": joint["name"],
                    "field": "limits",
                    "actual": joint["limits"],
                    "source": "engineering default rotation window policy",
                    "original": policy.get("author_limits"),
                    "rule_id": ENGINEERING_DEFAULT_POLICY_ID,
                    "rule_version": "v1",
                    "reason": (
                        "engineering analysis default window; no native mechanical range or engineering "
                        "position-range qualification is claimed"
                    ),
                    "native_cad_ranges": False,
                    "hardware_qualified": False,
                    "engineering_position_range_qualified": False,
                    "native_absence": policy.get("native_absence"),
                    "native_observation_errors": policy.get("native_observation_errors"),
                }
            )
        limit_evidence = source_joint.get("limit_evidence")
        drive_evidence = source_joint.get("drive_evidence")
        limit_evidence = limit_evidence if isinstance(limit_evidence, dict) else {}
        drive_evidence = drive_evidence if isinstance(drive_evidence, dict) else {}
        if source_joint.get("limit_source") == "declared_author" and source_joint.get("type") != "continuous":
            declared_values = limit_evidence.get("declared_values")
            if not isinstance(declared_values, dict) or not {"lower", "upper"} <= set(declared_values):
                raise ConfigError(
                    "declared author limits are missing their embedded-record evidence",
                    {"joint": joint["name"]},
                )
        if source_joint.get("drive_source") == "controlled" and not drive_evidence:
            raise ConfigError(
                "controlled drive is missing its embedded-record evidence",
                {"joint": joint["name"]},
            )
        if source_joint.get("limit_source") == "declared_author":
            declared_values = limit_evidence.get("declared_values")
            declared_values = declared_values if isinstance(declared_values, dict) else {}
            for field in ("lower", "upper"):
                if field not in declared_values or field not in joint["limits"]:
                    continue
                declared_author_limits.append(
                    {
                        "object_type": "joint",
                        "object": joint["name"],
                        "field": field,
                        "actual": joint["limits"][field],
                        "source": "controlled author dp.joint.limits_record",
                        "original": declared_values[field],
                        "file": limit_evidence.get("file"),
                        "sha256": limit_evidence.get("sha256"),
                        "anchor": limit_evidence.get("anchor"),
                        "key": limit_evidence.get("key"),
                        "reference": limit_evidence.get("reference"),
                        "native_observation_errors": limit_evidence.get("native_observation_errors") or [],
                        "rule_id": "limits.declared_author",
                        "rule_version": "v1",
                        "reason": "controlled author position range is not a native CAD reading",
                    }
                )
        declared_drive = drive_evidence.get("declared_values")
        declared_drive = declared_drive if isinstance(declared_drive, dict) else {}
        for field in ("effort", "velocity"):
            if field not in joint["limits"]:
                continue
            if field not in declared_drive:
                continue
            row = {
                "object_type": "joint",
                "object": joint["name"],
                "field": field,
                "actual": joint["limits"][field],
                "source": "controlled author dp.joint.drive_record",
                "original": declared_drive[field],
                "file": drive_evidence.get("file"),
                "sha256": drive_evidence.get("sha256"),
                "anchor": drive_evidence.get("anchor"),
                "key": drive_evidence.get("key"),
                "reference": drive_evidence.get("reference"),
                "rule_id": "drive.declared_author",
                "rule_version": "v1",
                "reason": "controlled author drive value is not a native CAD reading",
            }
            controlled_drive.append(row)
            if joint["limits"][field] == 0:
                unqualified_drive.append(
                    {
                        **row,
                        "rule_id": "drive.nonzero_required",
                        "reason": "zero effort or velocity is retained but does not qualify drive sizing",
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
        joint_name = fixed_joint_name(str(frame.get("parent") or ""), str(frame.get("name") or ""))
        frame_joint_names.append(
            {
                "object_type": "joint",
                "object": joint_name,
                "field": "name",
                "actual": joint_name,
                "source": "normalized parent and frame identities",
                "original": {"parent": frame.get("parent"), "child": frame.get("name")},
                "rule_id": "naming.fixed_endpoint_pair",
                "rule_version": "naming-v1",
                "reason": "fixed auxiliary joint derived from its normalized parent and child endpoints",
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
            if visual.get("provenance", {}).get("material_source") == "native_appearance":
                appearance = visual["provenance"].get("appearance") or {}
                native_appearances.append(
                    {
                        "object_type": "visual",
                        "object": visual["name"],
                        "fields": [
                            "material.name",
                            "material.color.rgba",
                            "mesh.filename",
                            "mesh.faces",
                        ],
                        "actual": {
                            "material": visual.get("material"),
                            "rgba": visual.get("rgba"),
                            "mesh": visual.get("mesh_name"),
                            "face_references": appearance.get("face_references"),
                        },
                        "sources": appearance.get("sources"),
                        "rule_id": "appearance.native_owner_state",
                        "rule_version": "v1",
                        "reason": (
                            "RGBA is resolved from the recorded native owner/display-state "
                            "appearance and grouped by exact delivered face spans"
                        ),
                    }
                )
            mesh_paths.append(
                {
                    "object_type": "visual",
                    "object": visual["name"],
                    "field": "mesh filename",
                    "actual": visual["mesh_name"],
                    "source": "current capture content addressing",
                    "original": visual.get(
                        "mesh_sha256",
                        visual.get("sha256"),
                    ),
                    "rule_id": "naming.mesh",
                    "rule_version": "naming-v1",
                    "reason": "current capture content addressing",
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
    if frame_joint_names:
        warnings.append(
            {
                "code": "naming.derived_frame_joint",
                "message": "Auxiliary frame joints derive their names from normalized parent and child identities.",
                "objects": frame_joint_names,
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
    if default_limits:
        warnings.append(
            {
                "code": "limits.default_policy",
                "message": (
                    "Engineering uses a versioned default rotation window because no usable native "
                    "or authored position range was read; the engineering position range is unqualified."
                ),
                "objects": default_limits,
            }
        )
    if declared_author_limits:
        warnings.append(
            {
                "code": "limits.declared_author",
                "message": (
                    "Joint position values come from a controlled author record; "
                    "they are not native CAD readings."
                ),
                "objects": declared_author_limits,
            }
        )
    if controlled_drive:
        warnings.append(
            {
                "code": "drive.declared_author",
                "message": (
                    "Joint drive values come from a controlled author record; "
                    "they are preserved but are not native CAD readings."
                ),
                "objects": controlled_drive,
            }
        )
    if unqualified_drive:
        warnings.append(
            {
                "code": "drive.unqualified",
                "message": (
                    "Controlled drive values of zero are retained for delivery but do not "
                    "qualify effort or velocity sizing."
                ),
                "objects": unqualified_drive,
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
    if native_appearances:
        warnings.append(
            {
                "code": "appearance.native_derived",
                "message": (
                    "Material colors are derived from recorded native appearance owners "
                    "and display-state assignments for the delivered faces."
                ),
                "objects": native_appearances,
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
            constants.append(
                {
                    "object_type": "visual",
                    "object": visual["name"],
                    "fields": ["scale"],
                    "actual": {"scale": visual.get("scale")},
                    "source": "display constant",
                }
            )
    if constants:
        warnings.append(
            {
                "code": "display.constant",
                "message": "Mesh display scale is a platform constant, not a CAD reading.",
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
                "provenance": {
                    "geometry": geometry,
                    "derived_joint_name": fixed_joint_name(parent, str(frame.get("name") or frame_id)),
                    "joint_name_rule": {
                        "rule_id": "naming.fixed_endpoint_pair",
                        "rule_version": "naming-v1",
                        "parent": parent,
                        "child": str(frame.get("name") or frame_id),
                    },
                },
            }
        )
    return frames


def load_scene(snapshot_root: Path) -> dict[str, Any]:
    """Legacy loader for replay/tests; Generate consumes derive_engineering_input."""

    from ..snapshot import verify_snapshot

    root = Path(snapshot_root).resolve()
    manifest = verify_snapshot(root)
    from .captured import derive_engineering_input

    scene = derive_engineering_input(root, manifest)
    if not isinstance(scene, dict) or not engineering_model_schema_supported(scene.get("schema_version")):
        raise ConfigError("unexpected scene schema", {"snapshot": str(root)})
    if scene.get("units") != "SI":
        raise ConfigError("only SI scenes are supported", {"units": scene.get("units")})
    provenance = dict(scene.get("provenance") or {})
    provenance["snapshot"] = {
        "kind": manifest.get("kind"),
        "evidence_class": manifest.get("evidence_class"),
        "identity": manifest.get("identity"),
        "files": len(manifest.get("files") or {}),
    }
    scene["provenance"] = provenance
    return scene
