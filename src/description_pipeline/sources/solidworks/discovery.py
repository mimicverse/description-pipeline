"""CAD-only semantic discovery: from native primitives to a prepared package.

The mechanical team supplies one immutable SolidWorks engineering directory.
This module reads the *raw* native record produced by an owned CAD session
(:meth:`CadBackend.discover_native`), derives rigid memberships, joints, axes,
limits and physical facts with explicit provenance, and writes an internal
prepared package: a generated ``robot.yaml`` (marked with a ``provenance``
block), a generated ``cad-revision.json``, the raw discovery record and the
findings list.  The engineering files are copied through unmodified at the
same relative paths.

Nothing here invents a mechanical fact.  Facts that cannot be derived from the
native record, a versioned controlled record, or a scalar CAD annotation become
object-specific findings and block the prepared package; no geometry default,
name prefix or parameter default is ever substituted.

Raw record shape (``solidworks-to-urdf.native-discovery/v1``), as returned by
the backend and re-read by the independent verifier:

``identity``
    Scalars read from the saved documents: ``hardware_id``, ``revision``,
    ``parent_revision``, ``owner``, ``change_summary``, ``control``
    (``system``/``reference``), ``delivery_configuration``, ``main_assembly``
    (package-relative), ``robot_name``.
``components``
    ``{name2, document, configuration, fixed, suppressed, lightweight}`` for
    every component occurrence.
``mates``
    ``{name, type, entities: [{component, feature, face_index, cylinder}],
    alignment, limits: {lower, upper, unit}, suppressed}`` with each cylinder
    given in its component frame (``point``/``direction``/``radius``).
``datums``
    ``{name, owner, array: [16 floats]}`` coordinate systems with the
    transform expressed in the assembly frame (owner ``""`` is assembly
    scope).
``masses``
    ``{component, mass_kg, material}`` per component read from CAD.
``properties``
    ``{document: {..}, components: {name2: {..}}, mates: {mate: {..}}}`` of
    scalar ``dp.*`` custom properties - the only channel for facts SolidWorks
    cannot express natively, and never a place for member maps.
``files``
    Relative path -> SHA-256 of every native document consulted.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from collections.abc import Mapping, Sequence

import yaml

from ...io import PipelineError, confined, digest, inventory, read_data
from .jsonio import write_json
from .errors import CadError
from .revision import package_inventory, seal_revision

DISCOVERY_SCHEMA = "solidworks-to-urdf.native-discovery/v1"
PREPARED_SCHEMA = "solidworks-to-urdf.prepared-package/v1"
FINDING_SCHEMA = "solidworks-to-urdf.discovery-finding/v1"
INPUT_SCHEMA = "solidworks-to-urdf.input/v1"
ROBOT_FILE = "robot.yaml"
REVISION_FILE = "cad-revision.json"
DISCOVERY_FILE = "discovery/native-discovery.json"
FINDINGS_FILE = "discovery/findings.json"
RECORD_DIR = "records"
#: The one native contract: a fixed property namespace and one record version.
NAMESPACE = "dp"
CONTRACT = "native-discovery/v1"
GENERATOR = "native-discovery"
GENERATOR_VERSION = "1"

#: The prepared package must satisfy the same joint geometry gate as an
#: authored package: a movable joint frame has to sit on the native shaft.
AXIS_OFFSET_TOL_M = 5e-5

CONTROL_SYSTEMS = ("git", "pdm", "handoff")

_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Datum name spaces the spec recognises: link frames, tool/sensor interfaces
#: and joint frames.  A datum is ``<PREFIX>_<snake_case>``: the suffix must be
#: exact snake_case before any transformation, so ``TCP_Tool`` blocks instead
#: of silently becoming ``tcp_tool``.  Any owned ``CS_*`` datum that is not the
#: body's link datum is a named interface frame.
INTERFACE_PREFIXES = ("CS_", "TCP_", "SCS_")
JCS_PREFIX = "JCS_"
_TOL = 1e-6

#: Mate types whose constraint rows this module can reconstruct from recorded
#: entity geometry.  Everything else blocks with an object finding; a mate type
#: this table does not support is never turned into a rigid connection.
SUPPORTED_MATES = (
    "coincident",
    "concentric",
    "distance",
    "limitdistance",
    "parallel",
    "perpendicular",
    "angle",
    "limitangle",
    "lock",
)


def _finding(code: str, obj: str, message: str, detail: Any = None, *, blocking: bool = True) -> dict:
    return {
        "schema_version": FINDING_SCHEMA,
        "code": code,
        "object": obj,
        "message": message,
        "detail": detail,
        "blocking": blocking,
    }


@dataclass(frozen=True)
class DiscoverySettings:
    """The two platform-controlled inputs: records and published names."""

    record_roots: tuple[Path, ...] = ()
    frozen_names: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedPackage:
    """Internal prepared inputs bound to immutable CAD plus raw discovery evidence."""

    schema_version: str
    run_id: str
    package: Path
    handoff_sha256: str  # original frozen native input inventory
    prepared_sha256: str  # generated package identity
    revision_sha256: str
    hardware_id: str | None
    revision: str | None
    source: dict | None
    discovery_path: Path
    discovery_sha256: str
    findings: tuple[dict, ...]
    passed: bool


def _text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _snake(value: Any) -> str | None:
    """Return a stable snake_case identifier, or ``None`` when nothing remains."""

    if not _text(value):
        return None
    folded = unicodedata.normalize("NFKD", str(value))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = re.sub(r"[^0-9A-Za-z]+", "_", folded).strip("_").lower()
    folded = re.sub(r"_{2,}", "_", folded)
    if not folded:
        return None
    if folded[0].isdigit():
        folded = "n_" + folded
    return folded


def _property(record: dict, section: str, key: str) -> Any:
    properties = record.get("properties") or {}
    bucket = properties.get(section) or {}
    return bucket.get(key)


def _scalar(record: dict, namespace: str, key: str, *, section: str = "document") -> Any:
    return _property(record, section, f"{namespace}.{key}")


def _identity_value(record: dict, namespace: str, key: str) -> Any:
    value = (record.get("identity") or {}).get(key)
    if value is None or value == "":
        value = _scalar(record, namespace, key)
    return value


def _unit(vector) -> list[float] | None:
    try:
        values = [float(value) for value in vector or ()]
    except (TypeError, ValueError):
        return None
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        return None
    norm = math.hypot(*values)
    if norm == 0:
        return None
    return [value / norm for value in values]


def _validate_native_record(value: Any, path: str = "raw") -> None:
    """Reject invalid producer evidence before derivation or serialization."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CadError(
                    "cad_native_record_invalid",
                    "a native record key is not a string",
                    {"field": path, "key": repr(key)},
                )
            _validate_native_record(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_native_record(item, f"{path}[{index}]")
        return
    raise CadError(
        "cad_native_record_invalid",
        "native evidence contains a nonfinite or non-JSON value",
        {"field": path, "value": repr(value)},
    )


def _cross(left: Sequence[float], right: Sequence[float]) -> list[float]:
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(one * two for one, two in zip(left, right, strict=True))


def _orthogonal_basis(direction: Sequence[float]) -> list[list[float]]:
    """Two independent unit vectors spanning the plane orthogonal to ``direction``."""

    helper = [1.0, 0.0, 0.0]
    if abs(direction[0]) > 0.9:
        helper = [0.0, 1.0, 0.0]
    first = _unit(_cross(direction, helper))
    second = _unit(_cross(direction, first))
    return [first, second]


def _rank(rows: Sequence[Sequence[float]]) -> int:
    """Row rank by Gram-Schmidt: how many independent constraints are recorded."""

    basis: list[list[float]] = []
    for row in rows:
        vector = [float(value) for value in row]
        for other in basis:
            projection = _dot(vector, other)
            vector = [one - projection * two for one, two in zip(vector, other, strict=True)]
        norm = math.sqrt(_dot(vector, vector))
        if norm > _TOL:
            basis.append([value / norm for value in vector])
    return len(basis)


def _component_frames(record: dict, findings: list[dict]) -> dict[str, list[list[float]] | None]:
    """Every component's 4x4 assembly transform; missing transforms block."""

    frames: dict[str, list[list[float]] | None] = {}
    for item in record.get("components") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name2") or "")
        if not name or item.get("suppressed"):
            continue
        try:
            values = [float(value) for value in item.get("transform") or ()]
        except (TypeError, ValueError):
            values = []
        if len(values) != 16 or not all(math.isfinite(value) for value in values):
            frames[name] = None
            findings.append(
                _finding(
                    "discovery.component_transform_missing",
                    f"component:{name}",
                    "component has no finite recorded 4x4 assembly transform",
                )
            )
        else:
            frames[name] = [values[0:4], values[4:8], values[8:12], values[12:16]]
    return frames


def _point(row, frame) -> list[float]:
    x, y, z = (float(value) for value in row)
    return [
        frame[0][0] * x + frame[0][1] * y + frame[0][2] * z + frame[0][3],
        frame[1][0] * x + frame[1][1] * y + frame[1][2] * z + frame[1][3],
        frame[2][0] * x + frame[2][1] * y + frame[2][2] * z + frame[2][3],
    ]


def _vector(row, frame) -> list[float]:
    x, y, z = (float(value) for value in row)
    return [
        frame[0][0] * x + frame[0][1] * y + frame[0][2] * z,
        frame[1][0] * x + frame[1][1] * y + frame[1][2] * z,
        frame[2][0] * x + frame[2][1] * y + frame[2][2] * z,
    ]


def _translation_row(direction, point) -> list[float]:
    moment = _cross(point, direction)
    return [float(direction[0]), float(direction[1]), float(direction[2]), moment[0], moment[1], moment[2]]


def _rotation_row(direction) -> list[float]:
    return [0.0, 0.0, 0.0, float(direction[0]), float(direction[1]), float(direction[2])]


def _null_space(rows: Sequence[Sequence[float]], dim: int = 6) -> list[list[float]]:
    """Basis of the twist space the constraint rows leave free."""

    matrix = [[float(value) for value in row] for row in rows]
    pivots: list[int] = []
    row_index = 0
    for column in range(dim):
        pivot = next((index for index in range(row_index, len(matrix)) if abs(matrix[index][column]) > _TOL), None)
        if pivot is None:
            continue
        matrix[row_index], matrix[pivot] = matrix[pivot], matrix[row_index]
        scale = matrix[row_index][column]
        matrix[row_index] = [value / scale for value in matrix[row_index]]
        for index in range(len(matrix)):
            if index != row_index and abs(matrix[index][column]) > _TOL:
                factor = matrix[index][column]
                matrix[index] = [one - factor * two for one, two in zip(matrix[index], matrix[row_index], strict=True)]
        pivots.append(column)
        row_index += 1
        if row_index == len(matrix):
            break
    free = [column for column in range(dim) if column not in pivots]
    basis: list[list[float]] = []
    for column in free:
        vector = [0.0] * dim
        vector[column] = 1.0
        for index, pivot in enumerate(pivots):
            vector[pivot] = -matrix[index][column]
        norm = math.sqrt(sum(value * value for value in vector))
        basis.append([value / norm for value in vector])
    return basis


def _mate_rows(mate: dict, frames: dict, findings: list[dict], obj: str) -> dict | None:
    """Reconstruct a mate's constraints as twists at the assembly origin.

    Entity geometry is recorded in its component frame and transformed here
    with that component's occurrence transform, so translation and rotation
    are coupled through the real entity points.  ``None`` means the mate is
    outside the supported scope and is blocked, never guessed: an unknown mate
    must not become a rigid connection.
    """

    kind = str(mate.get("type") or "").strip().lower()
    if kind not in SUPPORTED_MATES:
        findings.append(
            _finding(
                "discovery.mate_unsupported",
                obj,
                "mate type is outside the supported native constraint scope",
                {"type": kind},
            )
        )
        return None
    entities = _mate_entities(mate)
    if len(entities) < 2:
        findings.append(_finding("discovery.mate_entities_unsupported", obj, "mate has fewer than two entity records"))
        return None
    first, second = entities[0], entities[1]
    limits = mate.get("limits") if isinstance(mate.get("limits"), dict) else None

    def fail(code: str, message: str, detail: Any = None) -> None:
        findings.append(_finding(code, obj, message, detail))
        return None

    def frame_of(entity: dict):
        frame = frames.get(str(entity.get("component")))
        if frame is None:
            fail(
                "discovery.component_transform_missing",
                "mate entity component has no recorded assembly transform",
                {"component": entity.get("component")},
            )
            return None
        return frame

    def plane_of(entity: dict):
        frame = frame_of(entity)
        plane = entity.get("plane")
        if frame is None or not isinstance(plane, dict):
            return None
        direction = _unit(_vector(plane.get("normal") or (), frame))
        try:
            point = _point([float(value) for value in plane.get("point") or ()], frame)
        except (TypeError, ValueError):
            point = None
        if direction is None or point is None or len(point) != 3:
            fail(
                "discovery.mate_entities_unsupported",
                "recorded plane is not usable",
                {"feature": entity.get("feature")},
            )
            return None
        return point, direction

    def cylinder_of(entity: dict):
        frame = frame_of(entity)
        cylinder = entity.get("cylinder")
        if frame is None or not isinstance(cylinder, dict):
            return None
        direction = _unit(_vector(cylinder.get("direction") or cylinder.get("normal") or (), frame))
        try:
            point = _point([float(value) for value in (cylinder.get("point") or cylinder.get("center")) or ()], frame)
        except (TypeError, ValueError):
            point = None
        if direction is None or point is None or len(point) != 3:
            fail(
                "discovery.mate_entities_unsupported",
                "recorded cylinder is not usable",
                {"feature": entity.get("feature")},
            )
            return None
        return point, direction

    def point_of(entity: dict):
        frame = frame_of(entity)
        if frame is None:
            return None
        try:
            return _point([float(value) for value in entity.get("point") or ()], frame)
        except (TypeError, ValueError):
            fail(
                "discovery.mate_entities_unsupported",
                "recorded point is not usable",
                {"feature": entity.get("feature")},
            )
            return None

    basis_x = [1.0, 0.0, 0.0]
    basis_y = [0.0, 1.0, 0.0]
    basis_z = [0.0, 0.0, 1.0]
    if kind == "lock":
        first_frame, second_frame = frame_of(first), frame_of(second)
        if first_frame is None or second_frame is None:
            return None
        # A solved lock removes all six relative freedoms. Its constraint
        # basis can be expressed at the captured occurrence origin without
        # inventing a face, shaft or mechanical interface point.
        point = [first_frame[index][3] for index in range(3)]
        rows = [_translation_row(axis, point) for axis in (basis_x, basis_y, basis_z)]
        rows += [_rotation_row(axis) for axis in (basis_x, basis_y, basis_z)]
        return {"rows": rows, "limits": limits, "axis": None, "point": point, "entity": None}
    if kind == "concentric":
        left = cylinder_of(first)
        right = cylinder_of(second)
        if left is None or right is None:
            return fail("discovery.mate_entities_unsupported", "concentric mate needs two recorded cylindrical faces")
        if abs(abs(_dot(left[1], right[1])) - 1.0) > _TOL:
            return fail(
                "discovery.mate_geometry_mismatch", "recorded cylinder axes are not parallel in the solved state"
            )
        delta = [right[0][index] - left[0][index] for index in range(3)]
        along = _dot(delta, left[1])
        radial = [delta[index] - along * left[1][index] for index in range(3)]
        radial_gap = math.sqrt(_dot(radial, radial))
        if radial_gap > AXIS_OFFSET_TOL_M:
            return fail(
                "discovery.joint_axis_misaligned",
                "concentric mate cylinders are parallel but radially displaced",
                {"radial_gap_m": radial_gap, "tolerance_m": AXIS_OFFSET_TOL_M},
            )
        axis = left[1]
        basis = _orthogonal_basis(axis)
        rows = [_translation_row(direction, left[0]) for direction in basis]
        rows += [_rotation_row(direction) for direction in basis]
        return {"rows": rows, "limits": limits, "axis": axis, "point": left[0], "entity": first}
    if kind == "coincident":
        left_plane = plane_of(first)
        right_plane = plane_of(second)
        left_point = point_of(first) if first.get("point") is not None else None
        right_point = point_of(second) if second.get("point") is not None else None
        if left_plane is not None and right_plane is not None:
            if abs(abs(_dot(left_plane[1], right_plane[1])) - 1.0) > _TOL:
                return fail("discovery.mate_geometry_mismatch", "solved coincident planes are not parallel")
            normal = left_plane[1] if _dot(left_plane[1], right_plane[1]) >= 0 else [-value for value in left_plane[1]]
            rows = [_translation_row(normal, left_plane[0])]
            rows += [_rotation_row(direction) for direction in _orthogonal_basis(normal)]
            return {"rows": rows, "limits": limits, "axis": None, "point": left_plane[0], "entity": None}
        if isinstance(first.get("circle"), dict) and isinstance(second.get("circle"), dict):
            frame_left, frame_right = frame_of(first), frame_of(second)
            if frame_left is None or frame_right is None:
                return None
            left_circle = first["circle"]
            right_circle = second["circle"]
            normal = _unit(_vector(left_circle.get("normal") or (), frame_left))
            other = _unit(_vector(right_circle.get("normal") or (), frame_right))
            if normal is None or other is None or abs(abs(_dot(normal, other)) - 1.0) > _TOL:
                return fail("discovery.mate_geometry_mismatch", "solved coincident circles are not parallel")
            try:
                center = _point([float(value) for value in left_circle.get("center") or ()], frame_left)
                other_center = _point([float(value) for value in right_circle.get("center") or ()], frame_right)
            except (TypeError, ValueError):
                return fail("discovery.mate_entities_unsupported", "recorded circle centre is not usable")
            separation = math.sqrt(sum((center[i] - other_center[i]) ** 2 for i in range(3)))
            if separation > AXIS_OFFSET_TOL_M:
                return fail(
                    "discovery.mate_geometry_mismatch",
                    "solved coincident circles are not co-located",
                    {"separation_m": separation},
                )
            return {
                "rows": [_translation_row(normal, center)],
                "limits": limits,
                "axis": None,
                "point": center,
                "entity": None,
            }
        if left_point is not None and right_point is not None:
            rows = [_translation_row(axis, left_point) for axis in (basis_x, basis_y, basis_z)]
            return {"rows": rows, "limits": limits, "axis": None, "point": left_point, "entity": None}
        if left_plane is not None and right_point is not None:
            normal = left_plane[1]
            point = right_point
        elif left_point is not None and right_plane is not None:
            normal = right_plane[1]
            point = left_point
        else:
            return fail(
                "discovery.mate_entities_unsupported", "coincident mate entities carry no plane or point geometry"
            )
        # A vertex on a face removes exactly one translation; it does not
        # constrain the relative orientation.
        return {
            "rows": [_translation_row(normal, point)],
            "limits": limits,
            "axis": None,
            "point": point,
            "entity": None,
        }
    if kind == "limitangle":
        # A bounded angle travels inside its range: no bilateral constraint.
        return {"rows": [], "limits": limits, "axis": None, "point": None, "entity": None}
    if kind == "limitdistance":
        # A bounded distance keeps its travel; parallel planes still keep
        # their normal alignment.
        left_plane = plane_of(first) if isinstance(first.get("plane"), dict) else None
        right_plane = plane_of(second) if isinstance(second.get("plane"), dict) else None
        if left_plane is not None and right_plane is not None:
            if abs(abs(_dot(left_plane[1], right_plane[1])) - 1.0) > _TOL:
                return fail("discovery.mate_geometry_mismatch", "limit distance planes are not parallel")
            normal = left_plane[1] if _dot(left_plane[1], right_plane[1]) >= 0 else [-value for value in left_plane[1]]
            return {
                "rows": [_rotation_row(direction) for direction in _orthogonal_basis(normal)],
                "limits": limits,
                "axis": None,
                "point": left_plane[0],
                "entity": None,
            }
        return {"rows": [], "limits": limits, "axis": None, "point": None, "entity": None}
    if kind == "distance":
        left_point = point_of(first) if first.get("point") is not None else None
        right_point = point_of(second) if second.get("point") is not None else None
        left_plane = plane_of(first) if left_point is None else None
        right_plane = plane_of(second) if right_point is None else None
        if left_point is not None and right_point is not None:
            delta = [right_point[index] - left_point[index] for index in range(3)]
            direction = _unit(delta)
            point = [(left_point[index] + right_point[index]) / 2.0 for index in range(3)]
        elif left_plane is not None and right_plane is not None:
            if abs(abs(_dot(left_plane[1], right_plane[1])) - 1.0) > _TOL:
                return fail("discovery.mate_geometry_mismatch", "distance mate planes are not parallel")
            direction = left_plane[1]
            point = left_plane[0]
        elif left_point is not None and right_plane is not None:
            direction = right_plane[1]
            point = left_point
        elif left_plane is not None and right_point is not None:
            direction = left_plane[1]
            point = right_point
        else:
            return fail(
                "discovery.mate_entities_unsupported", "distance mate has no usable direction between its entities"
            )
        if direction is None:
            return fail(
                "discovery.mate_entities_unsupported", "distance mate has no usable direction between its entities"
            )
        return {
            "rows": [_translation_row(direction, point)],
            "limits": limits,
            "axis": None,
            "point": point,
            "entity": None,
        }
    if kind == "parallel":
        axis = None
        for entity in (first, second):
            if isinstance(entity.get("cylinder"), dict):
                cylinder = cylinder_of(entity)
                axis = cylinder[1] if cylinder is not None else None
                break
        if axis is None:
            plane = plane_of(first)
            other = plane_of(second)
            if plane is None or other is None:
                return fail("discovery.mate_entities_unsupported", "parallel mate entities carry no direction")
            if abs(abs(_dot(plane[1], other[1])) - 1.0) > _TOL:
                return fail(
                    "discovery.mate_geometry_mismatch", "solved parallel mate does not record parallel directions"
                )
            axis = plane[1]
        rows = [_rotation_row(direction) for direction in _orthogonal_basis(axis)]
        return {"rows": rows, "limits": limits, "axis": None, "point": None, "entity": None}
    directions = []
    for entity in (first, second):
        if isinstance(entity.get("cylinder"), dict):
            cylinder = cylinder_of(entity)
            directions.append(cylinder[1] if cylinder is not None else None)
        else:
            plane = plane_of(entity)
            directions.append(plane[1] if plane is not None else None)
    if directions[0] is None or directions[1] is None:
        return fail("discovery.mate_entities_unsupported", "angle mate entities carry no direction")
    normal = _unit(_cross(directions[0], directions[1]))
    if normal is None:
        return fail("discovery.mate_geometry_mismatch", "recorded directions are parallel; the angle is not defined")
    return {"rows": [_rotation_row(normal)], "limits": limits, "axis": None, "point": None, "entity": None}


def _mate_entities(mate: dict) -> list[dict]:
    return [item for item in mate.get("entities") or [] if isinstance(item, dict)]


def _mate_pairs(mate: dict) -> list[tuple[str, str]]:
    names: list[str] = []
    for item in _mate_entities(mate):
        name = str(item.get("component") or "")
        if name and name not in names:
            names.append(name)
    return [(names[left], names[right]) for left in range(len(names)) for right in range(left + 1, len(names))]


def _union_find(pairs: Sequence[tuple[str, str]], nodes: Sequence[str]) -> dict[str, str]:
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in pairs:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left
    return {node: find(node) for node in nodes}


@dataclass
class _Clusters:
    members: dict[str, list[str]]
    of: dict[str, str]
    #: Per component pair: the intersection of its mates' allowed motions and
    #: the mate indices that take part in it.
    pairs: dict[tuple[str, str], dict]


def _clusters(record: dict, findings: list[dict]) -> _Clusters:
    occurrences: set[str] = set()
    for item in record.get("components") or []:
        name = item.get("name2") if isinstance(item, dict) else None
        if not _text(name) or name in occurrences:
            findings.append(
                _finding(
                    "discovery.component_identity_invalid",
                    f"component:{name}",
                    "native occurrence names must be nonempty and unique",
                )
            )
        if _text(name):
            occurrences.add(name)
    components = {
        str(item.get("name2")): item
        for item in record.get("components") or []
        if isinstance(item, dict) and _text(item.get("name2")) and not item.get("suppressed")
    }
    names = sorted(components)
    frames = _component_frames(record, findings)
    pairs: dict[tuple[str, str], dict] = {}
    for index, mate in enumerate(record.get("mates") or []):
        if not isinstance(mate, dict) or mate.get("suppressed"):
            continue
        obj = f"mate:{mate.get('name') or index}"
        if mate.get("error_code") != 0:
            findings.append(
                _finding(
                    "discovery.mate_error_state",
                    obj,
                    "the saved mate reports a native error or an unreadable solve state",
                    {"error_code": mate.get("error_code")},
                )
            )
            continue
        rows = _mate_rows(mate, frames, findings, obj)
        for left, right in _mate_pairs(mate):
            if left not in components or right not in components:
                continue
            key = tuple(sorted((left, right)))
            bucket = pairs.setdefault(
                key,
                {"rows": [], "mates": [], "unresolved": False, "limits": None, "axis": None, "point": None},
            )
            bucket["mates"].append(index)
            if rows is None:
                bucket["unresolved"] = True
                continue
            bucket["rows"].extend(rows["rows"])
            if bucket["limits"] is None and isinstance(rows.get("limits"), dict):
                bucket["limits"] = rows["limits"]
            if bucket["axis"] is None and isinstance(rows.get("axis"), list):
                bucket["axis"] = rows["axis"]
                bucket["point"] = rows.get("point")
    for group in pairs.values():
        group["rank"] = _rank(group["rows"])
    rigid = [
        (left, right)
        for (left, right), group in sorted(pairs.items())
        if not group["unresolved"] and group["rank"] == 6
    ]
    properties = (record.get("properties") or {}).get("components") or {}
    for name in names:
        value = (properties.get(name) or {}).get(f"{NAMESPACE}.body_marker")
        if _text(value):
            findings.append(
                _finding(
                    "discovery.body_marker_unsupported",
                    f"component:{name}",
                    "authored body markers are not an accepted membership channel; remove them",
                    {"value": str(value)},
                )
            )
    mapping = _union_find(rigid, names)
    members: dict[str, list[str]] = {}
    for node, root in mapping.items():
        members.setdefault(root, []).append(node)
    clusters = _Clusters(
        members={key: sorted(value) for key, value in members.items()},
        of=mapping,
        pairs={
            key: {
                "rows": tuple(group["rows"]),
                "rank": group["rank"],
                "mates": tuple(group["mates"]),
                "unresolved": group["unresolved"],
                "limits": group["limits"],
                "axis": group["axis"],
                "point": group["point"],
            }
            for key, group in pairs.items()
        },
    )
    return clusters


def _resolve_record(settings: DiscoverySettings, reference: str, findings: list[dict], obj: str) -> dict | None:
    relative, _, key = str(reference).partition("#")
    for root in settings.record_roots:
        try:
            path = confined(Path(root), relative)
        except PipelineError:
            continue
        if not path.is_file():
            continue
        try:
            data = read_data(path)
            json.dumps(data, allow_nan=False)
        except PipelineError as error:
            findings.append(_finding("discovery.record_unreadable", obj, str(error), {"file": relative}))
            return None
        except (TypeError, ValueError):
            findings.append(
                _finding(
                    "discovery.record_unreadable",
                    obj,
                    "controlled evidence contains a nonfinite or non-JSON value",
                    {"file": relative},
                )
            )
            return None
        node: Any = data
        for part in [piece for piece in key.split(".") if piece]:
            if not isinstance(node, dict) or part not in node:
                findings.append(
                    _finding(
                        "discovery.record_key_missing",
                        obj,
                        "record reference has no such key",
                        {"file": relative, "key": key},
                    )
                )
                return None
            node = node[part]
        if not isinstance(node, dict):
            findings.append(
                _finding(
                    "discovery.record_shape", obj, "record entry must be an object", {"file": relative, "key": key}
                )
            )
            return None
        return {
            "reference": str(reference),
            "file": relative.replace("\\", "/"),
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "key": key,
            "value": node,
            "text": path.read_text(encoding="utf-8", errors="replace"),
        }
    findings.append(
        _finding(
            "discovery.record_missing", obj, "referenced controlled record was not found", {"reference": reference}
        )
    )
    return None


def _datum(record: dict, name: str | None, owners: Sequence[str]) -> dict | None:
    matches = [
        datum
        for datum in record.get("datums") or []
        if isinstance(datum, dict)
        and str(datum.get("name")) == str(name)
        and str(datum.get("owner") or "") in owners
    ]
    return matches[0] if len(matches) == 1 else None


def _axis_offset(point: Sequence[float], origin: Sequence[float], direction: Sequence[float]) -> float:
    delta = [float(point[index]) - float(origin[index]) for index in range(3)]
    along = sum(delta[index] * direction[index] for index in range(3))
    residual = [delta[index] - along * direction[index] for index in range(3)]
    return math.sqrt(sum(value * value for value in residual))


def _primary_mate(mates: list[dict], properties: dict) -> dict:
    """The mate that names the joint: limits first, then the shaft, then order."""

    for item in mates:
        if isinstance(item.get("limits"), dict):
            return item
    for item in mates:
        if any(isinstance(entity.get("cylinder"), dict) for entity in _mate_entities(item)):
            return item
    return mates[0]


def _merged_joint_properties(
    mate_properties: dict,
    document_properties: dict,
    mates: list[dict],
    namespace: str,
    findings: list[dict],
    obj: str,
) -> dict:
    """Merge mate-level and document-level scalars; conflicts block.

    No source wins by order: when two native sources declare the same key with
    different values the package is blocked with an object-specific finding.
    """

    candidates: dict[str, list[tuple[str, object]]] = {}
    names = [str(item.get("name") or "") for item in mates]
    for name in names:
        for key, value in (mate_properties.get(name) or {}).items():
            candidates.setdefault(key, []).append((f"mate:{name}", value))
    heads = {name.split("__", 1)[0] for name in names if "__" in name}
    prefixes = [f"{namespace}.joint.{head}." for head in sorted(heads)]
    prefixes += [f"{namespace}.joint.{name}." for name in names if name]
    for key, value in (document_properties or {}).items():
        if not isinstance(key, str):
            continue
        for prefix in prefixes:
            if key.startswith(prefix):
                candidates.setdefault(f"{namespace}.joint.{key[len(prefix) :]}", []).append((f"document:{key}", value))
    merged: dict = {}
    for key, values in candidates.items():
        texts = {str(value) for _, value in values}
        if len(texts) > 1:
            findings.append(
                _finding(
                    "discovery.joint_property_conflict",
                    obj,
                    "two native sources declare the same joint scalar with different values",
                    {"key": key, "sources": [source for source, _ in values], "values": sorted(texts)},
                )
            )
            continue
        merged[key] = values[0][1]
    return merged


def _joint_facts(record: dict, clusters: _Clusters, settings: DiscoverySettings, findings: list[dict]) -> list[dict]:
    mate_properties = (record.get("properties") or {}).get("mates") or {}
    document_properties = (record.get("properties") or {}).get("document") or {}
    raw_mates = record.get("mates") or []
    joints: list[dict] = []
    for (left, right), group in sorted(clusters.pairs.items()):
        if left not in clusters.of or right not in clusters.of or clusters.of[left] == clusters.of[right]:
            continue
        if group["unresolved"]:
            continue
        rank = group["rank"]
        if rank == 6:
            # The reconstructed constraints leave no relative motion: one body.
            continue
        mates = [raw_mates[index] for index in group["mates"] if 0 <= index < len(raw_mates)]
        if not mates:
            continue
        obj = f"mate:{mates[0].get('name') or group['mates'][0]}"
        properties = _merged_joint_properties(mate_properties, document_properties, mates, NAMESPACE, findings, obj)
        mate = _primary_mate(mates, properties)
        name = str(mate.get("name") or "")
        hint = str(properties.get(f"{NAMESPACE}.joint.type") or "").strip().lower()
        nullity = 6 - rank
        if nullity != 1:
            findings.append(
                _finding(
                    "discovery.joint_unsupported_pattern",
                    obj,
                    "the reconstructed mate constraints do not leave exactly one joint motion",
                    {"rank": rank, "nullity": nullity, "components": [left, right]},
                )
            )
            continue
        twist = _null_space(group["rows"], 6)[0]
        velocity, omega = twist[:3], twist[3:]
        w = math.sqrt(_dot(omega, omega))
        v = math.sqrt(_dot(velocity, velocity))
        if w <= 1e-9:
            joint_type = "prismatic"
            direction = [value / v for value in velocity] if v > 0 else None
            axis_point = None
        else:
            if v > 1e-9 and abs(_dot(velocity, omega)) > 1e-6 * max(v, w) ** 2:
                findings.append(
                    _finding(
                        "discovery.joint_unsupported_screw",
                        obj,
                        "the reconstructed motion is a screw, which no v1 joint type carries",
                        {"pitch_term": _dot(velocity, omega)},
                    )
                )
                continue
            joint_type = "continuous" if hint == "continuous" else "revolute"
            direction = [value / w for value in omega]
            axis_point = [value / (w * w) for value in _cross(omega, velocity)]
        if direction is None:
            findings.append(
                _finding("discovery.joint_unsupported_pattern", obj, "the reconstructed motion has no axis")
            )
            continue
        if hint and hint not in {"revolute", "prismatic", "continuous"}:
            findings.append(
                _finding(
                    "discovery.joint_type_unsupported", obj, "joint type annotation is not supported", {"type": hint}
                )
            )
            continue
        if joint_type == "prismatic" and hint not in ("", "prismatic"):
            findings.append(
                _finding(
                    "discovery.joint_type_conflict",
                    obj,
                    "the scalar joint type annotation contradicts the reconstructed freedom",
                    {"annotation": hint, "derived": "prismatic"},
                )
            )
            continue
        shaft = group["axis"]
        if shaft is None:
            findings.append(
                _finding(
                    "discovery.joint_axis_selector_missing",
                    obj,
                    "no cylindrical mate entity carries the joint shaft for the capture to re-read",
                )
            )
            continue
        if abs(abs(_dot(shaft, direction)) - 1.0) > 1e-4:
            findings.append(
                _finding(
                    "discovery.joint_axis_mismatch",
                    obj,
                    "the recorded shaft does not follow the reconstructed freedom",
                    {"shaft": shaft, "freedom": direction},
                )
            )
            continue
        if axis_point is not None and group.get("point") is not None:
            offset = _axis_offset(group["point"], axis_point, direction)
            if offset > AXIS_OFFSET_TOL_M:
                findings.append(
                    _finding(
                        "discovery.joint_axis_mismatch",
                        obj,
                        "the recorded shaft misses the reconstructed joint axis",
                        {"offset_m": offset},
                    )
                )
                continue
        sign_value = properties.get(f"{NAMESPACE}.joint.axis_sign")
        if (type(sign_value) is int and sign_value == 1) or (type(sign_value) is str and sign_value in ("+1", "1")):
            sign = 1
        elif (type(sign_value) is int and sign_value == -1) or (type(sign_value) is str and sign_value == "-1"):
            sign = -1
        else:
            findings.append(
                _finding(
                    "discovery.joint_axis_sign_missing",
                    obj,
                    "dp.joint.axis_sign must be exactly +1 or -1; a vendor cylinder direction is not a positive motion",
                    {"value": sign_value},
                )
            )
            continue
        native_direction = [float(value) for value in shaft]
        if sign == -1:
            native_direction = [-value for value in native_direction]
        cylinder_entity = next(
            (
                item
                for mate_item in mates
                for item in _mate_entities(mate_item)
                if isinstance(item.get("cylinder"), dict)
                and _text(item.get("component"))
                and (_text(item.get("feature")) or isinstance(item.get("face_index"), int))
            ),
            None,
        )
        if cylinder_entity is None:
            findings.append(
                _finding(
                    "discovery.joint_axis_selector_missing",
                    obj,
                    "the cylindrical mate entity carries no feature name or face index for the capture",
                )
            )
            continue
        axis_reference = {"component": str(cylinder_entity.get("component")), "body_type": "solid"}
        if _text(cylinder_entity.get("feature")):
            axis_reference["feature_name"] = str(cylinder_entity["feature"])
        else:
            axis_reference["face_index"] = int(cylinder_entity["face_index"])
        limits = None
        limit_evidence = None
        native = group["limits"]
        if joint_type == "continuous":
            if isinstance(native, dict):
                findings.append(
                    _finding(
                        "discovery.joint_limits_unexpected", obj, "a continuous joint carries native position limits"
                    )
                )
                continue
        elif isinstance(native, dict):
            unit = native.get("unit")
            expected = "m" if joint_type == "prismatic" else "rad"
            if unit != expected:
                findings.append(
                    _finding(
                        "discovery.joint_limits_unit",
                        obj,
                        "native mate limits are not recorded in SI units for this joint type",
                        {"unit": unit, "expected": expected},
                    )
                )
                continue
            try:
                limits = {"lower": float(native["lower"]), "upper": float(native["upper"])}
            except (KeyError, TypeError, ValueError):
                findings.append(
                    _finding("discovery.joint_limits_invalid", obj, "native mate limits are not finite numbers")
                )
                continue
            native_record_ref = properties.get(f"{NAMESPACE}.joint.limits_record")
            if _text(native_record_ref):
                declared = _resolve_record(settings, str(native_record_ref), findings, obj)
                if declared is not None:
                    try:
                        lower = float(declared["value"]["lower"])
                        upper = float(declared["value"]["upper"])
                    except (KeyError, TypeError, ValueError):
                        findings.append(
                            _finding(
                                "discovery.joint_limits_invalid",
                                obj,
                                "controlled record lacks finite lower/upper limits",
                                {"file": declared["file"]},
                            )
                        )
                        continue
                    if not (math.isfinite(lower) and math.isfinite(upper)):
                        findings.append(
                            _finding("discovery.joint_limits_invalid", obj, "controlled position limits must be finite")
                        )
                        continue
                    if abs(lower - limits["lower"]) > 1e-12 or abs(upper - limits["upper"]) > 1e-12:
                        findings.append(
                            _finding(
                                "discovery.joint_limits_conflict",
                                obj,
                                "native mate limits and the controlled record disagree",
                                {"native": limits, "record": {"lower": lower, "upper": upper}},
                            )
                        )
                        continue
                    limit_evidence = declared
        else:
            record_ref = properties.get(f"{NAMESPACE}.joint.limits_record")
            if _text(record_ref):
                resolved = _resolve_record(settings, str(record_ref), findings, obj)
                if resolved is not None:
                    try:
                        limits = {
                            "lower": float(resolved["value"]["lower"]),
                            "upper": float(resolved["value"]["upper"]),
                        }
                    except (KeyError, TypeError, ValueError):
                        findings.append(
                            _finding(
                                "discovery.joint_limits_invalid",
                                obj,
                                "controlled record lacks finite lower/upper limits",
                                {"file": resolved["file"]},
                            )
                        )
                        continue
                    limit_evidence = resolved
        if joint_type != "continuous" and limits is None:
            findings.append(
                _finding("discovery.joint_limits_missing", obj, "no native or controlled position range for this joint")
            )
            continue
        drive = None
        drive_ref = properties.get(f"{NAMESPACE}.joint.drive_record")
        if _text(drive_ref):
            drive = _resolve_record(settings, str(drive_ref), findings, obj)
        if drive is None:
            findings.append(
                _finding("discovery.joint_drive_missing", obj, "no controlled drive specification for this joint")
            )
            continue
        spec = drive["value"]
        try:
            effort = float(spec["effort"])
            velocity_value = float(spec["velocity"])
        except (KeyError, TypeError, ValueError):
            findings.append(
                _finding(
                    "discovery.joint_drive_invalid",
                    obj,
                    "drive record lacks finite effort/velocity",
                    {"file": drive["file"]},
                )
            )
            continue
        if not (math.isfinite(effort) and math.isfinite(velocity_value)) or effort <= 0.0 or velocity_value <= 0.0:
            findings.append(
                _finding(
                    "discovery.joint_drive_invalid",
                    obj,
                    "drive record effort/velocity must be finite and positive",
                    {"file": drive["file"]},
                )
            )
            continue
        joints.append(
            {
                "mate": name,
                "index": group["mates"][0],
                "mates": [str(item.get("name") or "") for item in mates],
                "type": joint_type,
                "components": [clusters.of[left], clusters.of[right]],
                "axis": {
                    "point": [float(value) for value in (group["point"] or [0.0, 0.0, 0.0])],
                    "direction": native_direction,
                    "source": "mate",
                },
                "axis_reference": axis_reference,
                "limits": limits,
                "limit_evidence": limit_evidence,
                "drive": drive,
                "properties": dict(properties),
            }
        )
    return joints


def _body_records(
    record: dict,
    clusters: _Clusters,
    settings: DiscoverySettings,
    findings: list[dict],
) -> tuple[list[dict], dict[str, dict]]:
    """Link names come from the body's own ``CS_<link>`` coordinate system.

    No filename, folder or sanitized fallback naming: the native datum name is
    the interface name.  A published name in the frozen registry wins, keyed by
    the occurrence-aware component identity.
    """

    components = {
        str(item.get("name2")): item
        for item in record.get("components") or []
        if isinstance(item, dict) and _text(item.get("name2"))
    }
    properties = (record.get("properties") or {}).get("components") or {}
    frozen = settings.frozen_names or {}
    datums = [item for item in record.get("datums") or [] if isinstance(item, dict) and _text(item.get("name"))]
    bodies: list[dict] = []
    for root, members in sorted(clusters.members.items()):
        material = [
            member
            for member in members
            if not str(components[member].get("document") or "").lower().endswith(".sldasm")
        ]
        if not material:
            continue
        anchor = material[0]
        item = components.get(anchor, {})
        identity = str(item.get("instance_id") or f"{item.get('document') or ''}#{anchor}")
        declared = {
            str(value).strip()
            for member in members
            if _text(value := (properties.get(member) or {}).get(f"{NAMESPACE}.body_datum"))
        }
        if len(declared) > 1:
            findings.append(
                _finding(
                    "discovery.body_datum_conflict",
                    f"body:{root}",
                    "one body declares conflicting body datum names",
                    {"datums": sorted(declared)},
                )
            )
            continue
        explicit = next(iter(declared), None)
        owned = [
            datum
            for datum in datums
            if str(datum.get("owner") or "") in members and str(datum.get("name")).startswith("CS_")
        ]
        if explicit is not None:
            candidate = _datum(record, explicit, members)
            if candidate is None:
                foreign = [
                    datum
                    for datum in datums
                    if str(datum.get("name")) == explicit and str(datum.get("owner") or "") not in members
                ]
                findings.append(
                    _finding(
                        "discovery.body_datum_owner_mismatch" if foreign else "discovery.body_datum_missing",
                        f"body:{root}",
                        "body_datum does not bind exactly one coordinate system owned by this body",
                        {"datum": explicit},
                    )
                )
                continue
            datum = candidate
        elif len(owned) == 1:
            datum = owned[0]
        elif not owned:
            findings.append(
                _finding(
                    "discovery.link_name_missing",
                    f"body:{root}",
                    "no CS_<link> coordinate system is owned by this body",
                    {"components": members},
                )
            )
            continue
        else:
            findings.append(
                _finding(
                    "discovery.link_name_conflict",
                    f"body:{root}",
                    "several CS_<link> coordinate systems are owned by one body",
                    {"datums": sorted(str(datum["name"]) for datum in owned)},
                )
            )
            continue
        datum_name = str(datum["name"])
        if not datum_name.startswith("CS_"):
            findings.append(
                _finding(
                    "discovery.link_name_invalid",
                    f"body:{root}",
                    "the body datum must be named CS_<link>",
                    {"datum": datum_name},
                )
            )
            continue
        name = datum_name[3:]
        if _SNAKE.fullmatch(name) is None:
            findings.append(
                _finding(
                    "discovery.link_name_invalid",
                    f"body:{root}",
                    "the CS_<link> suffix must be the exact snake_case interface name",
                    {"datum": datum_name},
                )
            )
            continue
        source = "datum"
        if identity in frozen:
            published = str(frozen[identity])
            if _SNAKE.fullmatch(published) is None:
                findings.append(
                    _finding(
                        "discovery.link_name_invalid",
                        f"body:{root}",
                        "frozen published name is not snake_case",
                        {"identity": identity, "name": published},
                    )
                )
                continue
            if published != name:
                findings.append(
                    _finding(
                        "discovery.name_frozen_mismatch",
                        f"body:{root}",
                        "the frozen name differs from the current native CS_<link>; review the interface",
                        {"identity": identity, "native": name, "published": published},
                    )
                )
                continue
            source = "frozen"
        bodies.append(
            {
                "root": root,
                "components": material,
                "owners": members,
                "name": name,
                "source": source,
                "identity": identity,
                "datum": datum_name,
            }
        )
    by_root = {item["root"]: item for item in bodies}
    seen: dict[str, str] = {}
    for body in bodies:
        if body["name"] in seen:
            findings.append(
                _finding(
                    "discovery.body_name_duplicate",
                    f"body:{body['root']}",
                    "two rigid bodies resolve to the same name",
                    {"name": body["name"], "other": seen[body["name"]]},
                )
            )
        else:
            seen[body["name"]] = body["root"]
    return bodies, by_root


def _interface_frames(
    record: dict,
    clusters: _Clusters,
    bodies: list[dict],
    findings: list[dict],
) -> list[dict]:
    """Every recognised named native interface becomes a source frame.

    A recognised datum is never dropped silently: an interface that cannot be
    attributed to a body, or whose derived name is not an exact snake_case
    name, blocks the package.
    """

    body_of_component: dict[str, dict] = {}
    for body in bodies:
        for component in body["owners"]:
            body_of_component[component] = body
    link_datums = {(owner, str(body.get("datum"))) for body in bodies for owner in body["owners"]}
    frames: list[dict] = []
    seen: dict[str, str] = {}
    for datum in record.get("datums") or []:
        if not isinstance(datum, dict):
            continue
        name = str(datum.get("name") or "")
        if not name.startswith(INTERFACE_PREFIXES):
            continue
        owner = str(datum.get("owner") or "")
        if (owner, name) in link_datums:
            continue
        body = body_of_component.get(owner)
        if body is None:
            findings.append(
                _finding(
                    "discovery.interface_unowned",
                    f"datum:{name}",
                    "recognised interface datum is not owned by any body",
                    {"owner": owner},
                )
            )
            continue
        _prefix, _, suffix = name.partition("_")
        if _SNAKE.fullmatch(suffix) is None:
            findings.append(
                _finding(
                    "discovery.interface_name_invalid",
                    f"datum:{name}",
                    "interface datum suffix must already be exact snake_case",
                )
            )
            continue
        frame_name = suffix
        if frame_name in seen:
            findings.append(
                _finding(
                    "discovery.interface_name_duplicate",
                    f"datum:{name}",
                    "two interface datums derive the same frame name",
                    {"other": seen[frame_name]},
                )
            )
            continue
        if frame_name in {str(body.get("name")) for body in bodies}:
            findings.append(
                _finding(
                    "discovery.interface_name_duplicate",
                    f"datum:{name}",
                    "an interface frame collides with a body name under the shared name contract",
                    {"name": frame_name},
                )
            )
            continue
        seen[frame_name] = name
        frames.append(
            {
                "id": frame_name,
                "name": frame_name,
                "parent": body["name"],
                "coordinate_system": name,
            }
        )
    return frames


def _jcs_check(record: dict, joints: list[dict], by_name: dict[str, dict], findings: list[dict]) -> None:
    """``JCS_<joint>`` must alias the child body datum the compiler uses.

    The current compiler places the joint frame at the child body's datum; a
    JCS that disagrees would be silently ignored, so it blocks instead.
    """

    named = {str(joint.get("name")): joint for joint in joints if joint.get("name")}
    for datum in record.get("datums") or []:
        if not isinstance(datum, dict):
            continue
        name = str(datum.get("name") or "")
        if not name.startswith(JCS_PREFIX):
            continue
        joint_name = name[len(JCS_PREFIX) :]
        joint = named.get(joint_name)
        if joint is None or "child" not in joint:
            findings.append(
                _finding(
                    "discovery.jcs_unmatched",
                    f"datum:{name}",
                    "JCS_<joint> names no discovered joint",
                )
            )
            continue
        child = by_name.get(str(joint["child"]))
        if child is not None and str(datum.get("owner") or "") not in child["owners"]:
            findings.append(
                _finding(
                    "discovery.jcs_owner_mismatch",
                    f"datum:{name}",
                    "JCS_ datum is not owned by the child body's components",
                    {"owner": datum.get("owner"), "child": joint["child"]},
                )
            )
            continue
        child_datum = _datum(record, child["datum"], child["owners"]) if child else None
        alias = [float(value) for value in datum.get("array") or ()]
        reference = [float(value) for value in (child_datum or {}).get("array") or ()]
        if (
            len(alias) != 16
            or len(reference) != 16
            or any(abs(one - two) > 1e-6 for one, two in zip(alias, reference, strict=True))
        ):
            findings.append(
                _finding(
                    "discovery.jcs_mismatch",
                    f"datum:{name}",
                    "JCS_ frame differs from the child body datum the compiler places the joint at",
                )
            )
        else:
            findings.append(
                _finding(
                    "discovery.jcs_alias",
                    f"datum:{name}",
                    "JCS_ frame aliases the child body datum",
                    blocking=False,
                )
            )


def _root_body(
    record: dict, clusters: _Clusters, bodies: list[dict], namespace: str, findings: list[dict]
) -> dict | None:
    """The base body is the one the CAD itself names: CS_base_link.

    A temporary ``IsFixed`` flag never proves a root or a rigid connection.
    """

    named = [body for body in bodies if str(body.get("datum")) == "CS_base_link"]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        findings.append(
            _finding(
                "discovery.root_conflict",
                "assembly",
                "several bodies own a CS_base_link coordinate system",
                {"bodies": [body["name"] for body in named]},
            )
        )
        return None
    findings.append(
        _finding(
            "discovery.root_missing",
            "assembly",
            "no body owns CS_base_link; a temporary IsFixed flag cannot prove the base",
        )
    )
    return None


def _orient(
    joints: list[dict],
    bodies: list[dict],
    by_root: dict[str, dict],
    root: dict | None,
    findings: list[dict],
) -> None:
    """Orient the joint graph away from the base body and reject loops and islands."""

    known = set(by_root)
    adjacency: dict[str, list[tuple[str, dict]]] = {}
    for joint in joints:
        components = joint["components"]
        if len(components) < 2 or components[0] not in known or components[1] not in known:
            continue
        adjacency.setdefault(components[0], []).append((components[1], joint))
        adjacency.setdefault(components[1], []).append((components[0], joint))
    if root is None:
        return
    order: dict[str, int] = {root["root"]: 0}
    queue = [root["root"]]
    consumed: set[int] = set()
    edges: set[frozenset] = set()
    while queue:
        current = queue.pop(0)
        for neighbour, joint in sorted(adjacency.get(current, []), key=lambda item: item[1]["index"]):
            if joint["index"] in consumed:
                continue
            edge = frozenset((current, neighbour))
            if edge in edges:
                consumed.add(joint["index"])
                findings.append(
                    _finding(
                        "discovery.topology_parallel",
                        f"mate:{joint['mate'] or joint['index']}",
                        "two movable mates connect the same pair of bodies; one reduction is ambiguous",
                        {"components": joint["components"]},
                    )
                )
                continue
            consumed.add(joint["index"])
            edges.add(edge)
            joint["parent_root"] = current
            joint["child_root"] = neighbour
            if neighbour not in order:
                order[neighbour] = order[current] + 1
                queue.append(neighbour)
    for joint in joints:
        if joint["index"] not in consumed:
            findings.append(
                _finding(
                    "discovery.topology_loop",
                    f"mate:{joint['mate'] or joint['index']}",
                    "this movable mate closes a loop; the reduced tree cannot carry it",
                    {"components": joint["components"]},
                )
            )
            continue
        joint["parent"] = by_root[joint["parent_root"]]["name"]
        joint["child"] = by_root[joint["child_root"]]["name"]
    unreached = sorted(body["root"] for body in bodies if body["root"] not in order)
    if unreached:
        findings.append(
            _finding(
                "discovery.tree_disconnected",
                "assembly",
                "some rigid bodies have no movable-mate path to the base body",
                {"bodies": unreached},
            )
        )


def _joint_names(
    joints: list[dict],
    settings: DiscoverySettings,
    findings: list[dict],
) -> dict[str, str]:
    """Joint names come from named mate groups ``<joint>__<role>``.

    Every mate of one pair must share the same snake_case group name; missing
    or conflicting groups block.  A frozen published name, keyed by the
    occurrence-aware primary mate name, wins.
    """

    frozen = settings.frozen_names or {}
    assigned: dict[str, str] = {}
    taken: dict[str, str] = {}
    for joint in joints:
        if "parent" not in joint:
            continue
        names = [name for name in joint.get("mates") or [] if name]
        heads: list[str] = []
        for mate_name in names:
            head, separator, role = mate_name.partition("__")
            if not separator or not role or _SNAKE.fullmatch(head) is None:
                heads = []
                break
            heads.append(head)
        if not heads:
            findings.append(
                _finding(
                    "discovery.joint_name_missing",
                    f"mate:{joint['mate'] or joint['index']}",
                    "mate names must form a named group <joint>__<role>",
                    {"mates": names},
                )
            )
            continue
        if len(set(heads)) != 1:
            findings.append(
                _finding(
                    "discovery.joint_name_conflict",
                    f"mate:{joint['mate'] or joint['index']}",
                    "the mates of one pair carry different joint group names",
                    {"mates": names},
                )
            )
            continue
        name = heads[0]
        identity = names[0]
        if identity in frozen:
            published = str(frozen[identity])
            if _SNAKE.fullmatch(published) is None:
                findings.append(
                    _finding(
                        "discovery.joint_name_invalid",
                        f"mate:{identity}",
                        "frozen published joint name is not snake_case",
                        {"identity": identity, "name": published},
                    )
                )
                continue
            if published != name:
                findings.append(
                    _finding(
                        "discovery.joint_name_frozen_mismatch",
                        f"mate:{identity}",
                        "the frozen name differs from the named mate group; review the interface change",
                        {"identity": identity, "native": name, "published": published},
                    )
                )
                continue
            assigned[identity] = name
        if name in taken:
            findings.append(
                _finding(
                    "discovery.joint_name_duplicate",
                    f"mate:{identity}",
                    "two joints resolve to the same name",
                    {"name": name, "other": taken[name]},
                )
            )
            continue
        taken[name] = f"mate:{identity}"
        joint["name"] = name
    return assigned


def _axis_in_child(joint: dict, datum: dict) -> list[float] | None:
    try:
        values = [float(value) for value in datum.get("array") or ()]
    except (TypeError, ValueError):
        return None
    if len(values) != 16:
        return None
    basis = (
        (values[0], values[1], values[2]),
        (values[4], values[5], values[6]),
        (values[8], values[9], values[10]),
    )
    axis = [float(value) for value in joint["axis"]["direction"]]
    # The child frame's basis columns are the datum's local axes in the
    # assembly frame, so the local axis is the transpose applied to the
    # assembly axis.
    local = [
        basis[0][0] * axis[0] + basis[1][0] * axis[1] + basis[2][0] * axis[2],
        basis[0][1] * axis[0] + basis[1][1] * axis[1] + basis[2][1] * axis[2],
        basis[0][2] * axis[0] + basis[1][2] * axis[1] + basis[2][2] * axis[2],
    ]
    norm = math.sqrt(sum(value * value for value in local))
    if norm == 0:
        return None
    return [value / norm for value in local]


def _frame_checks(joints: list[dict], by_name: dict[str, dict], record: dict, findings: list[dict]) -> None:
    for joint in joints:
        if "child" not in joint or joint["axis"].get("source") != "mate":
            continue
        body = by_name.get(joint["child"])
        datum = _datum(record, body["datum"], body["owners"]) if body else None
        if datum is None:
            continue
        try:
            values = [float(value) for value in (datum or {}).get("array") or ()]
        except (TypeError, ValueError):
            continue
        if len(values) != 16:
            continue
        origin = [values[3], values[7], values[11]]
        offset = _axis_offset(joint["axis"]["point"], origin, joint["axis"]["direction"])
        if offset > AXIS_OFFSET_TOL_M:
            findings.append(
                _finding(
                    "discovery.joint_frame_off_axis",
                    f"mate:{joint['mate']}",
                    "the child body frame does not lie on the native joint axis",
                    {"offset_m": offset, "body": joint["child"], "datum": body["datum"]},
                )
            )


def _budgets(
    record: dict,
    settings: DiscoverySettings,
    findings: list[dict],
) -> tuple[dict | None, dict | None]:
    """Resolve the versioned design budget/measurement record.

    Exported CAD mass and bounding extents can never budget themselves.  The
    CAD carries one scalar reference to a versioned design budget record; the
    intervals it declares are the only accepted capture windows, and a missing
    or malformed record blocks the package.
    """

    reference = _scalar(record, NAMESPACE, "design_budget_record")
    if not _text(reference):
        findings.append(
            _finding(
                "discovery.design_budget_missing",
                "assembly",
                "no versioned design budget record is referenced; exported CAD readings cannot budget themselves",
            )
        )
        return None, None
    resolved = _resolve_record(settings, str(reference), findings, "assembly")
    if resolved is None:
        return None, None
    value = resolved["value"]
    checks: dict[str, list[float]] = {}
    for key in ("expected_mass_kg", "expected_extent_m"):
        item = value.get(key)
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            findings.append(
                _finding(
                    "discovery.design_budget_invalid",
                    "assembly",
                    f"design budget record lacks a two-element {key} interval",
                    {"file": resolved["file"]},
                )
            )
            return None, None
        try:
            low, high = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            findings.append(
                _finding(
                    "discovery.design_budget_invalid",
                    "assembly",
                    f"design budget {key} is not numeric",
                    {"file": resolved["file"]},
                )
            )
            return None, None
        if not (math.isfinite(low) and math.isfinite(high)) or low <= 0.0 or high <= low:
            findings.append(
                _finding(
                    "discovery.design_budget_invalid",
                    "assembly",
                    f"design budget {key} must be a finite increasing positive interval",
                    {"file": resolved["file"], "value": [low, high]},
                )
            )
            return None, None
        checks[key] = [low, high]
    return checks, resolved


def _identity(
    record: dict,
    settings: DiscoverySettings,
    findings: list[dict],
    *,
    configuration: str | None,
    assembly: str | None,
    native_files: Mapping[str, str],
) -> dict:
    namespace = NAMESPACE
    hardware = _identity_value(record, namespace, "hardware_id")
    if not _text(hardware):
        findings.append(
            _finding("discovery.identity_missing", "assembly", "hardware identity is not recorded in the CAD records")
        )
    hardware = str(hardware).strip() if _text(hardware) else None
    revision = _identity_value(record, namespace, "revision")
    if not _text(revision):
        findings.append(
            _finding("discovery.revision_missing", "assembly", "structural revision is not recorded in the CAD records")
        )
    revision = str(revision).strip() if _text(revision) else None
    owner = _identity_value(record, namespace, "owner")
    if not _text(owner):
        findings.append(
            _finding("discovery.owner_missing", "assembly", "revision owner is not recorded in the CAD records")
        )
    summary = _identity_value(record, namespace, "change_summary")
    if not _text(summary):
        findings.append(
            _finding("discovery.summary_missing", "assembly", "change summary is not recorded in the CAD records")
        )
    control = (record.get("identity") or {}).get("control")
    if not isinstance(control, dict):
        control = {
            "system": _scalar(record, namespace, "control.system"),
            "reference": _scalar(record, namespace, "control.reference"),
        }
    if not _text(control.get("system")) or not _text(control.get("reference")):
        findings.append(
            _finding("discovery.control_missing", "assembly", "CAD control system/reference are not recorded")
        )
        control = None
    elif str(control["system"]) not in CONTROL_SYSTEMS:
        findings.append(
            _finding(
                "discovery.control_invalid",
                "assembly",
                "CAD control system must be git, pdm or handoff",
                {"system": control["system"]},
            )
        )
        control = None
    delivery = configuration or _identity_value(record, namespace, "delivery_configuration")
    if not _text(delivery):
        findings.append(
            _finding("discovery.configuration_missing", "assembly", "no saved delivery configuration is recorded")
        )
    delivery = str(delivery).strip() if _text(delivery) else None
    main = assembly or _identity_value(record, namespace, "main_assembly")
    main = str(main).replace("\\", "/") if _text(main) else None
    if main is None or main not in native_files:
        findings.append(
            _finding(
                "discovery.main_assembly_ambiguous",
                "assembly",
                "no unique saved main assembly is identifiable inside the frozen directory",
                {"assembly": main},
            )
        )
    robot_name = _identity_value(record, namespace, "robot_name")
    robot_name = str(robot_name).strip() if _text(robot_name) else _snake(hardware)
    if not _text(robot_name) or _SNAKE.fullmatch(str(robot_name)) is None:
        findings.append(
            _finding("discovery.robot_name_invalid", "assembly", "robot name is not snake_case", {"name": robot_name})
        )
    parent = _identity_value(record, namespace, "parent_revision")
    return {
        "hardware_id": hardware,
        "revision": revision,
        "parent_revision": str(parent).strip() if _text(parent) else None,
        "owner": str(owner).strip() if _text(owner) else None,
        "change_summary": str(summary).strip() if _text(summary) else None,
        "control": {"system": str(control["system"]), "reference": str(control["reference"])} if control else None,
        "delivery_configuration": delivery,
        "main_assembly": main,
        "robot_name": str(robot_name) if _text(robot_name) else None,
    }


def _anchor_for(text: str, resolved: dict) -> str | None:
    value = resolved.get("value") or {}
    candidates: list[str] = []
    if isinstance(value.get("anchor"), str) and value["anchor"].strip():
        candidates.append(value["anchor"].strip())
    last = [piece for piece in str(resolved.get("key") or "").split(".") if piece]
    if last:
        candidates.append(json.dumps(last[-1]))
        candidates.append(last[-1])
    if resolved.get("reference"):
        candidates.append(str(resolved["reference"]))
    for item in candidates:
        if item and item in text:
            return item
    return None


def _embed_record(output: Path, resolved: dict, records: list[dict]) -> dict:
    """Copy a controlled record into the package once and bind its bytes."""

    relative = str(resolved["file"]).replace("\\", "/")
    for existing in records:
        if existing["file"] == relative and existing["key"] == str(resolved.get("key") or ""):
            return existing
    source = resolved.get("path")
    if not source:
        raise PipelineError(f"controlled record {relative!r} was not resolved from a record root")
    stem = re.sub(r"[^0-9A-Za-z._-]", "_", PurePosixPath(relative).stem)[:48] or "record"
    target_name = f"{RECORD_DIR}/{str(resolved['sha256'])[:8]}_{stem}.txt"
    target = confined(output, target_name, exists=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(source, target)
    entry = {
        "reference": str(resolved.get("reference") or relative),
        "file": relative,
        "key": str(resolved.get("key") or ""),
        "package_file": target_name,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "source_sha256": str(resolved["sha256"]),
        "anchor": _anchor_for(target.read_text(encoding="utf-8", errors="replace"), resolved),
    }
    records.append(entry)
    return entry


def _copy_tree(source: Path, output: Path, files: Mapping[str, str]) -> None:
    for name in sorted(files):
        original = confined(source, name)
        target = confined(output, name, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)


def _write_yaml(path: Path, document: dict) -> None:
    text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.write_text(text, encoding="utf-8", newline="\n")


def prepare_native_package(
    frozen_source: Path,
    output: Path,
    run_id: str,
    *,
    backend=None,
    settings: DiscoverySettings | None = None,
    configuration: str | None = None,
    assembly: str | None = None,
    on_event=None,
) -> PreparedPackage:
    """Derive a prepared package from one immutable native engineering directory."""

    settings = settings or DiscoverySettings()
    frozen_source = Path(frozen_source)
    output = Path(output)
    if frozen_source.is_symlink() or frozen_source.is_junction() or not frozen_source.is_dir():
        raise PipelineError("frozen_source must be an existing native engineering directory, not a link")
    if output.exists() and any(output.iterdir()):
        raise PipelineError("prepared output directory must be empty")
    native_files = package_inventory(frozen_source)
    handoff_sha256 = digest(native_files)
    authored = sorted(
        name for name in native_files if name in {ROBOT_FILE, REVISION_FILE} or name.startswith("discovery/")
    )
    if authored:
        raise PipelineError(f"frozen_source already contains authored package files: {authored[:5]}")
    if backend is None:
        from .native import SolidWorksBackend
        from ...verification.consumer import readiness

        readiness()
        backend = SolidWorksBackend()
    record = backend.discover_native(frozen_source, {"namespace": NAMESPACE, "contract": CONTRACT})
    if not isinstance(record, dict) or record.get("schema_version") != DISCOVERY_SCHEMA:
        raise PipelineError("native discovery backend returned an unexpected record schema")
    _validate_native_record(record)
    findings: list[dict] = []
    datum_ids: set[tuple[str, str]] = set()
    for datum in record.get("datums") or []:
        if not isinstance(datum, dict):
            continue
        key = (str(datum.get("owner") or ""), str(datum.get("name") or ""))
        if key in datum_ids:
            findings.append(
                _finding(
                    "discovery.datum_identity_duplicate",
                    f"datum:{key[1]}",
                    "the same occurrence records one datum name more than once",
                    {"owner": key[0]},
                )
            )
        datum_ids.add(key)
    record_namespace = record.get("namespace")
    if record_namespace is not None and str(record_namespace) != NAMESPACE:
        findings.append(
            _finding(
                "discovery.namespace_mismatch",
                "assembly",
                "native record was read with another property namespace",
                {"record": record_namespace, "expected": NAMESPACE},
            )
        )
    record_contract = record.get("contract")
    if record_contract is not None and str(record_contract) != CONTRACT:
        findings.append(
            _finding(
                "discovery.contract_mismatch",
                "assembly",
                "native record declares another discovery contract",
                {"record": record_contract, "expected": CONTRACT},
            )
        )
    identity = _identity(
        record, settings, findings, configuration=configuration, assembly=assembly, native_files=native_files
    )
    checks, budget_entry = _budgets(record, settings, findings)
    clusters = _clusters(record, findings)
    bodies, by_root = _body_records(record, clusters, settings, findings)
    joints = _joint_facts(record, clusters, settings, findings)
    root = _root_body(record, clusters, bodies, NAMESPACE, findings)
    if root is not None:
        if root["name"] != "base_link":
            findings.append(
                _finding(
                    "discovery.root_name_missing",
                    f"body:{root['root']}",
                    "the fixed body must own a CS_base_link coordinate system",
                    {"name": root["name"], "datum": root["datum"]},
                )
            )
    else:
        bodies.append(
            {
                "root": "__unresolved__",
                "components": [],
                "owners": [],
                "name": "base_link",
                "source": "placeholder",
                "identity": "assembly",
                "datum": "",
            }
        )
        by_root = {item["root"]: item for item in bodies}
    _orient(joints, bodies, by_root, root, findings)
    derived_names = _joint_names(joints, settings, findings)
    for body in bodies:
        if body["source"] == "frozen":
            derived_names[body["identity"]] = body["name"]
    seen_names: dict[str, str] = {}
    for body in bodies:
        if body["name"] in seen_names:
            findings.append(
                _finding(
                    "discovery.body_name_duplicate",
                    f"body:{body['root']}",
                    "two rigid bodies resolve to the same name",
                    {"name": body["name"], "other": seen_names[body["name"]]},
                )
            )
        else:
            seen_names[body["name"]] = body["root"]
    by_name = {body["name"]: body for body in bodies}
    _frame_checks(joints, by_name, record, findings)
    frames = _interface_frames(record, clusters, bodies, findings)
    _jcs_check(record, joints, by_name, findings)
    blocking = [item for item in findings if item["blocking"]]
    output.mkdir(parents=True, exist_ok=True)
    discovery_path = output / DISCOVERY_FILE
    discovery_path.parent.mkdir(parents=True, exist_ok=True)
    derived = {
        "bodies": [
            {"name": body["name"], "components": body["components"], "frame": {"coordinate_system": body["datum"]}}
            for body in sorted(bodies, key=lambda item: item["name"])
        ],
        "joints": [
            {
                "mate": joint["mate"],
                "name": joint.get("name"),
                "type": joint["type"],
                "parent": joint.get("parent"),
                "child": joint.get("child"),
                "axis": joint["axis"],
                "limits": joint["limits"],
            }
            for joint in joints
        ],
        "frames": frames,
    }
    payload = {
        "schema_version": DISCOVERY_SCHEMA,
        "contract": CONTRACT,
        "namespace": NAMESPACE,
        "run_id": run_id,
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "handoff_sha256": handoff_sha256,
        "native_files": native_files,
        "identity": identity,
        "records": [],
        "frozen_names": derived_names,
        "raw": record,
        "derived": derived,
        "findings": findings,
    }
    write_json(discovery_path, payload)
    discovery_sha256 = hashlib.sha256(discovery_path.read_bytes()).hexdigest()
    write_json(output / FINDINGS_FILE, {"schema_version": FINDING_SCHEMA, "findings": findings})
    if blocking:
        return PreparedPackage(
            schema_version=PREPARED_SCHEMA,
            run_id=run_id,
            package=output,
            handoff_sha256=handoff_sha256,
            prepared_sha256=digest(inventory(output)),
            revision_sha256="",
            hardware_id=identity["hardware_id"],
            revision=identity["revision"],
            source=None,
            discovery_path=discovery_path,
            discovery_sha256=discovery_sha256,
            findings=tuple(findings),
            passed=False,
        )
    # Passing package: copy the native engineering tree through unmodified, then
    # write the generated contract files beside it.
    _copy_tree(frozen_source, output, native_files)
    records: list[dict] = []
    for joint in joints:
        if joint.get("limit_evidence") is not None:
            _embed_record(output, joint["limit_evidence"], records)
        if joint.get("drive") is not None:
            _embed_record(output, joint["drive"], records)
    budget_record = _embed_record(output, budget_entry, records) if budget_entry is not None else None
    if records or checks is not None:
        payload["records"] = records
        if budget_record is not None:
            payload["budget"] = {
                "reference": str(budget_entry.get("reference") or ""),
                "key": str(budget_entry.get("key") or ""),
                "package_file": budget_record["package_file"],
                "sha256": budget_record["sha256"],
            }
        write_json(discovery_path, payload)
        discovery_sha256 = hashlib.sha256(discovery_path.read_bytes()).hexdigest()
    document = _robot_document(
        identity, bodies, joints, record, settings, records, checks, frames, run_id, discovery_sha256, handoff_sha256
    )
    _write_yaml(output / ROBOT_FILE, document)
    seal_revision(
        output,
        hardware_id=identity["hardware_id"],
        revision=identity["revision"],
        owner=identity["owner"],
        system=identity["control"]["system"],
        reference=identity["control"]["reference"],
        summary=identity["change_summary"],
        parent_revision=identity["parent_revision"],
    )
    from .input import load_package

    loaded = load_package(output)
    return PreparedPackage(
        schema_version=PREPARED_SCHEMA,
        run_id=run_id,
        package=output,
        handoff_sha256=handoff_sha256,
        prepared_sha256=digest(inventory(output)),
        revision_sha256=hashlib.sha256((output / REVISION_FILE).read_bytes()).hexdigest(),
        hardware_id=identity["hardware_id"],
        revision=identity["revision"],
        source=loaded,
        discovery_path=discovery_path,
        discovery_sha256=discovery_sha256,
        findings=tuple(findings),
        passed=True,
    )


def _robot_document(
    identity: dict,
    bodies: list[dict],
    joints: list[dict],
    record: dict,
    settings: DiscoverySettings,
    records: list[dict],
    checks: dict | None,
    frames: list[dict],
    run_id: str,
    discovery_sha256: str,
    handoff_sha256: str,
) -> dict:
    by_name = {body["name"]: body for body in bodies}
    source: dict[str, Any] = {
        "provider": "solidworks",
        "robot_name": identity["robot_name"],
        "assembly": identity["main_assembly"],
        "configuration": identity["delivery_configuration"],
        "bodies": [
            {
                "id": body["name"],
                "name": body["name"],
                "components": body["components"],
                "frame": {"coordinate_system": body["datum"]},
            }
            for body in sorted(bodies, key=lambda item: item["name"])
            if body["components"]
        ],
        "joints": [],
        "material_source": "cad",
    }
    if frames:
        source["frames"] = [
            {
                "id": frame["id"],
                "name": frame["name"],
                "parent": frame["parent"],
                "coordinate_system": frame["coordinate_system"],
            }
            for frame in sorted(frames, key=lambda item: item["name"])
        ]
    for joint in sorted(joints, key=lambda item: item.get("name") or ""):
        if "parent" not in joint or "name" not in joint:
            continue
        body = by_name[joint["child"]]
        datum = _datum(record, body["datum"], body["owners"])
        axis = _axis_in_child(joint, datum) if datum is not None else None
        if axis is None:
            raise PipelineError(f"joint {joint['name']!r} has no usable child frame")
        entry: dict[str, Any] = {
            "id": joint["name"],
            "name": joint["name"],
            "type": joint["type"],
            "parent": joint["parent"],
            "child": joint["child"],
            "axis": axis,
            "axis_reference": joint["axis_reference"],
        }
        if joint["type"] == "continuous":
            entry["limits"] = {
                "effort": float(joint["drive"]["value"]["effort"]),
                "velocity": float(joint["drive"]["value"]["velocity"]),
            }
        else:
            entry["limits"] = {
                "lower": float(joint["limits"]["lower"]),
                "upper": float(joint["limits"]["upper"]),
                "effort": float(joint["drive"]["value"]["effort"]),
                "velocity": float(joint["drive"]["value"]["velocity"]),
            }
        if joint.get("limit_evidence") is not None:
            embedded = next(
                (
                    item
                    for item in records
                    if item["file"] == str(joint["limit_evidence"]["file"]).replace("\\", "/")
                    and item["key"] == str(joint["limit_evidence"].get("key") or "")
                ),
                None,
            )
            if embedded is not None and embedded.get("anchor"):
                entry["limit_evidence"] = {
                    "file": embedded["package_file"],
                    "sha256": embedded["sha256"],
                    "anchor": embedded["anchor"],
                }
        source["joints"].append(entry)
    document: dict[str, Any] = {
        "schema_version": INPUT_SCHEMA,
        "hardware_id": identity["hardware_id"],
        "source": source,
        "provenance": {
            "generator": GENERATOR,
            "generator_version": GENERATOR_VERSION,
            "contract": CONTRACT,
            "discovery_sha256": discovery_sha256,
            "native_inventory_sha256": handoff_sha256,
            "run_id": run_id,
        },
    }
    if checks:
        document["checks"] = checks
    return document
