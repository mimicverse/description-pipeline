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
    if len(values) != 3:
        return None
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return None
    return [value / norm for value in values]


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


def _null_direction(rows: Sequence[Sequence[float]]) -> list[float] | None:
    """The single direction orthogonal to one or two independent constraint rows."""

    basis: list[list[float]] = []
    for row in rows:
        vector = [float(value) for value in row]
        for other in basis:
            projection = _dot(vector, other)
            vector = [one - projection * two for one, two in zip(vector, other, strict=True)]
        norm = math.sqrt(_dot(vector, vector))
        if norm > _TOL:
            basis.append([value / norm for value in vector])
    if len(basis) == 0:
        return None
    if len(basis) >= 2:
        return _unit(_cross(basis[0], basis[1]))
    helper = [1.0, 0.0, 0.0]
    if abs(basis[0][0]) > 0.9:
        helper = [0.0, 1.0, 0.0]
    return _unit(_cross(basis[0], helper))


def _entity_direction(entity: dict) -> list[float] | None:
    cylinder = entity.get("cylinder")
    if isinstance(cylinder, dict):
        return _unit(cylinder.get("direction"))
    plane = entity.get("plane")
    if isinstance(plane, dict):
        return _unit(plane.get("normal"))
    return None


def _mate_rows(mate: dict, findings: list[dict], obj: str) -> dict | None:
    """Reconstruct a mate feature's constraint rows from recorded entity geometry.

    ``T`` holds blocked relative-translation directions, ``R`` blocked relative
    rotation directions, both as unit vectors in the assembly frame.  Returning
    ``None`` means the mate is outside the supported scope and is blocked, never
    guessed: an unknown mate must not become a rigid connection.
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

    if kind == "lock":
        basis = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        return {"T": basis, "R": basis, "limits": limits, "axis": None}
    if kind == "concentric":
        left = _unit((first.get("cylinder") or {}).get("direction"))
        right = _unit((second.get("cylinder") or {}).get("direction"))
        if left is None or right is None:
            return fail(
                "discovery.mate_entities_unsupported",
                "concentric mate needs two recorded cylindrical faces",
                {"entities": [first.get("feature"), second.get("feature")]},
            )
        if abs(abs(_dot(left, right)) - 1.0) > _TOL:
            return fail(
                "discovery.mate_geometry_mismatch", "recorded cylinder axes are not parallel in the solved state"
            )
        basis = _orthogonal_basis(left)
        return {"T": basis, "R": basis, "limits": limits, "axis": left}
    if kind == "coincident":
        first_plane = first.get("plane") if isinstance(first.get("plane"), dict) else None
        second_plane = second.get("plane") if isinstance(second.get("plane"), dict) else None
        first_point = first.get("point") if isinstance(first.get("point"), (list, tuple)) else None
        second_point = second.get("point") if isinstance(second.get("point"), (list, tuple)) else None
        if first_plane is not None and second_plane is not None:
            left = _unit(first_plane.get("normal"))
            right = _unit(second_plane.get("normal"))
            if left is None or right is None:
                return fail("discovery.mate_entities_unsupported", "a coincident plane has no usable normal")
            if abs(abs(_dot(left, right)) - 1.0) > _TOL:
                return fail("discovery.mate_geometry_mismatch", "solved coincident planes are not parallel")
            normal = left if _dot(left, right) >= 0.0 else [-value for value in left]
            return {"T": [normal], "R": _orthogonal_basis(normal), "limits": limits, "axis": None}
        if first_point is not None and second_point is not None:
            return {
                "T": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                "R": [],
                "limits": limits,
                "axis": None,
            }
        if first_plane is not None and second_point is not None:
            normal = _unit(first_plane.get("normal"))
        elif first_point is not None and second_plane is not None:
            normal = _unit(second_plane.get("normal"))
        else:
            return fail(
                "discovery.mate_entities_unsupported", "coincident mate entities carry no plane or point geometry"
            )
        if normal is None:
            return fail("discovery.mate_entities_unsupported", "coincident plane has no usable normal")
        return {"T": [normal], "R": _orthogonal_basis(normal), "limits": limits, "axis": None}
    if kind in ("distance", "limitdistance"):
        direction = None
        first_point = first.get("point") if isinstance(first.get("point"), (list, tuple)) else None
        second_point = second.get("point") if isinstance(second.get("point"), (list, tuple)) else None
        first_plane = first.get("plane") if isinstance(first.get("plane"), dict) else None
        second_plane = second.get("plane") if isinstance(second.get("plane"), dict) else None
        if first_point is not None and second_point is not None:
            delta = [float(second_point[index]) - float(first_point[index]) for index in range(3)]
            direction = _unit(delta)
        elif first_plane is not None and second_plane is not None:
            left = _unit(first_plane.get("normal"))
            right = _unit(second_plane.get("normal"))
            if left is not None and right is not None and abs(abs(_dot(left, right)) - 1.0) <= _TOL:
                direction = left
        elif first_point is not None and second_plane is not None:
            direction = _unit(second_plane.get("normal"))
        elif first_plane is not None and second_point is not None:
            direction = _unit(first_plane.get("normal"))
        if direction is None:
            return fail(
                "discovery.mate_entities_unsupported", "distance mate has no usable direction between its entities"
            )
        return {"T": [direction], "R": [], "limits": limits, "axis": None}
    if kind == "parallel":
        left = _entity_direction(first)
        right = _entity_direction(second)
        if left is None or right is None:
            return fail("discovery.mate_entities_unsupported", "parallel mate entities carry no direction")
        if abs(abs(_dot(left, right)) - 1.0) > _TOL:
            return fail("discovery.mate_geometry_mismatch", "solved parallel mate does not record parallel directions")
        return {"T": [], "R": _orthogonal_basis(left), "limits": limits, "axis": None}
    # perpendicular, angle, limitangle: only rotation about the common normal
    # leaves the recorded angle between the directions unchanged.
    left = _entity_direction(first)
    right = _entity_direction(second)
    if left is None or right is None:
        return fail("discovery.mate_entities_unsupported", "angle mate entities carry no direction")
    normal = _unit(_cross(left, right))
    if normal is None:
        return fail("discovery.mate_geometry_mismatch", "recorded directions are parallel; the angle is not defined")
    return {"T": [], "R": [normal], "limits": limits, "axis": None}


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


def _clusters(record: dict, findings: list[dict], namespace: str) -> _Clusters:
    components = {
        str(item.get("name2")): item
        for item in record.get("components") or []
        if isinstance(item, dict) and _text(item.get("name2")) and not item.get("suppressed")
    }
    names = sorted(components)
    pairs: dict[tuple[str, str], dict] = {}
    for index, mate in enumerate(record.get("mates") or []):
        if not isinstance(mate, dict) or mate.get("suppressed"):
            continue
        obj = f"mate:{mate.get('name') or index}"
        rows = _mate_rows(mate, findings, obj)
        for left, right in _mate_pairs(mate):
            if left not in components or right not in components:
                continue
            key = tuple(sorted((left, right)))
            bucket = pairs.setdefault(
                key, {"T": [], "R": [], "mates": [], "unresolved": False, "limits": None, "axis": None}
            )
            bucket["mates"].append(index)
            if rows is None:
                bucket["unresolved"] = True
                continue
            bucket["T"].extend(rows["T"])
            bucket["R"].extend(rows["R"])
            if bucket["limits"] is None and isinstance(rows.get("limits"), dict):
                bucket["limits"] = rows["limits"]
            if bucket["axis"] is None and isinstance(rows.get("axis"), list):
                bucket["axis"] = rows["axis"]
    for group in pairs.values():
        group["rank"] = (_rank(group["T"]), _rank(group["R"]))
    rigid = [
        (left, right)
        for (left, right), group in sorted(pairs.items())
        if not group["unresolved"] and group["rank"] == (3, 3)
    ]
    fixed = [name for name, item in components.items() if item.get("fixed")]
    if fixed:
        ground = fixed[0]
        rigid.extend((ground, name) for name in fixed[1:])
    properties = (record.get("properties") or {}).get("components") or {}
    markers: dict[str, str] = {}
    for name in names:
        value = (properties.get(name) or {}).get(f"{namespace}.body_marker")
        if _text(value):
            markers[name] = str(value).strip()
    mapping = _union_find(rigid, names)
    by_marker: dict[str, list[str]] = {}
    for node, marker in markers.items():
        by_marker.setdefault(marker, []).append(node)
    marker_pairs: list[tuple[str, str]] = []
    for group in by_marker.values():
        for other in group[1:]:
            marker_pairs.append((group[0], other))
    if marker_pairs:
        mapping = _union_find(rigid + marker_pairs, names)
    members: dict[str, list[str]] = {}
    for node, root in mapping.items():
        members.setdefault(root, []).append(node)
    clusters = _Clusters(
        members={key: sorted(value) for key, value in members.items()},
        of=mapping,
        pairs={
            key: {
                "T": tuple(group["T"]),
                "R": tuple(group["R"]),
                "rank": tuple(group["rank"]),
                "mates": tuple(group["mates"]),
                "unresolved": group["unresolved"],
                "limits": group["limits"],
                "axis": group["axis"],
            }
            for key, group in pairs.items()
        },
    )
    cluster_markers: dict[str, set[str]] = {}
    for node, marker in markers.items():
        cluster_markers.setdefault(mapping[node], set()).add(marker)
    for root, values in sorted(cluster_markers.items()):
        if len(values) > 1:
            findings.append(
                _finding(
                    "discovery.body_marker_conflict",
                    f"body:{root}",
                    "one rigid body carries conflicting scalar body markers",
                    {"markers": sorted(values), "components": clusters.members[root]},
                )
            )
    for (left, right), group in sorted(pairs.items()):
        if not group["unresolved"] and group["rank"] == (3, 3):
            continue
        if left in mapping and right in mapping and mapping[left] == mapping[right]:
            mate = (record.get("mates") or [None])[group["mates"][0]] if group["mates"] else None
            findings.append(
                _finding(
                    "discovery.body_marker_conflicts_mate",
                    f"mate:{(mate or {}).get('name') or group['mates'][0]}",
                    "a scalar marker merged components that a movable mate keeps apart",
                    {"components": [left, right], "mate_type": (mate or {}).get("type")},
                )
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
        except PipelineError as error:
            findings.append(_finding("discovery.record_unreadable", obj, str(error), {"file": relative}))
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


def _datum(record: dict, name: str | None) -> dict | None:
    for datum in record.get("datums") or []:
        if isinstance(datum, dict) and str(datum.get("name")) == str(name):
            return datum
    return None


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


def _joint_facts(record: dict, clusters: _Clusters, settings: DiscoverySettings, findings: list[dict]) -> list[dict]:
    namespace = NAMESPACE
    mate_properties = (record.get("properties") or {}).get("mates") or {}
    raw_mates = record.get("mates") or []
    joints: list[dict] = []
    for (left, right), group in sorted(clusters.pairs.items()):
        if left not in clusters.of or right not in clusters.of or clusters.of[left] == clusters.of[right]:
            continue
        rank_t, rank_r = group["rank"]
        if group["unresolved"]:
            continue
        if (rank_t, rank_r) == (3, 3):
            # The reconstructed constraints leave no relative motion: one body.
            continue
        mates = [raw_mates[index] for index in group["mates"] if 0 <= index < len(raw_mates)]
        if not mates:
            continue
        properties: dict = {}
        for item in mates:
            for key, value in (mate_properties.get(str(item.get("name") or "")) or {}).items():
                properties.setdefault(key, value)
        mate = _primary_mate(mates, properties)
        name = str(mate.get("name") or "")
        obj = f"mate:{name or group['mates'][0]}"
        hint = str(properties.get(f"{namespace}.joint.type") or "").strip().lower()
        if (rank_t, rank_r) == (3, 2):
            joint_type = "continuous" if hint == "continuous" else "revolute"
            if hint not in ("", "revolute", "continuous"):
                findings.append(
                    _finding(
                        "discovery.joint_type_conflict",
                        obj,
                        "the scalar joint type annotation contradicts the reconstructed freedom",
                        {"annotation": hint, "derived": joint_type},
                    )
                )
                continue
            free_axis = _null_direction(group["R"])
        elif (rank_t, rank_r) == (2, 3):
            joint_type = "prismatic"
            if hint not in ("", "prismatic"):
                findings.append(
                    _finding(
                        "discovery.joint_type_conflict",
                        obj,
                        "the scalar joint type annotation contradicts the reconstructed freedom",
                        {"annotation": hint, "derived": "prismatic"},
                    )
                )
                continue
            free_axis = _null_direction(group["T"])
        else:
            findings.append(
                _finding(
                    "discovery.joint_unsupported_pattern",
                    obj,
                    "the reconstructed mate constraints do not leave one supported joint motion",
                    {"translation_rank": rank_t, "rotation_rank": rank_r, "components": [left, right]},
                )
            )
            continue
        if hint and hint not in {"revolute", "prismatic", "continuous", ""}:
            findings.append(
                _finding(
                    "discovery.joint_type_unsupported", obj, "joint type annotation is not supported", {"type": hint}
                )
            )
            continue
        if free_axis is None:
            findings.append(_finding("discovery.joint_axis_missing", obj, "the reconstructed freedom has no free axis"))
            continue
        shaft = group["axis"]
        if shaft is not None and abs(abs(_dot(shaft, free_axis)) - 1.0) > 1e-4:
            findings.append(
                _finding(
                    "discovery.joint_axis_mismatch",
                    obj,
                    "the recorded shaft does not follow the reconstructed freedom",
                    {"shaft": shaft, "freedom": free_axis},
                )
            )
            continue
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
                    "no cylindrical mate entity carries a feature name or face index for the capture to re-read",
                )
            )
            continue
        cylinder = cylinder_entity["cylinder"]
        direction = _unit(shaft) if shaft is not None else free_axis
        if direction is None:
            direction = free_axis
        axis = {
            "point": [float(value) for value in cylinder.get("point") or ()],
            "direction": [float(value) for value in direction],
            "source": "mate",
        }
        axis_reference = {"component": str(cylinder_entity.get("component")), "body_type": "solid"}
        if _text(cylinder_entity.get("feature")):
            axis_reference["feature_name"] = str(cylinder_entity["feature"])
        else:
            axis_reference["face_index"] = int(cylinder_entity["face_index"])
        sign = properties.get(f"{namespace}.joint.axis_sign")
        if sign in (-1, "-1"):
            axis["direction"] = [-value for value in axis["direction"]]
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
        else:
            record_ref = properties.get(f"{namespace}.joint.limits_record")
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
        drive_ref = properties.get(f"{namespace}.joint.drive_record")
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
            velocity = float(spec["velocity"])
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
        if not (math.isfinite(effort) and math.isfinite(velocity)) or effort <= 0.0 or velocity <= 0.0:
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
                "components": [left, right],
                "axis": axis,
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
    namespace = NAMESPACE
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
        anchor = members[0]
        documents = sorted({str(components.get(name, {}).get("document") or "") for name in members} - {""})
        identity = documents[0] if documents else anchor
        candidates: dict[str, str] = {}
        for member in members:
            value = (properties.get(member) or {}).get(f"{namespace}.body_name")
            if _text(value):
                candidates["property"] = str(value).strip()
                break
        if identity in frozen:
            candidates["frozen"] = str(frozen[identity])
        if len(set(candidates.values())) > 1:
            findings.append(
                _finding(
                    "discovery.body_name_conflict",
                    f"body:{root}",
                    "frozen name and CAD annotation disagree for one body",
                    {"identity": identity, "candidates": candidates},
                )
            )
            continue
        if candidates:
            name = next(iter(candidates.values()))
            source = next(iter(candidates))
            stem_name = _snake(PurePosixPath(documents[0]).stem) if documents else None
        else:
            stem = PurePosixPath(documents[0]).stem if documents else anchor
            name = _snake(stem) or _snake(f"{anchor}_body")
            source = "derived"
            stem_name = name
            if len(documents) > 1:
                findings.append(
                    _finding(
                        "discovery.body_name_derived",
                        f"body:{root}",
                        "name derives from one document of a multi-document rigid body; a frozen name should pin it",
                        {"identity": identity, "documents": documents},
                        blocking=False,
                    )
                )
        if not _text(name) or _SNAKE.fullmatch(str(name)) is None:
            findings.append(
                _finding("discovery.body_name_invalid", f"body:{root}", "body name is not snake_case", {"name": name})
            )
            continue
        datum_name: str | None = None
        explicit = None
        for member in members:
            value = (properties.get(member) or {}).get(f"{namespace}.body_datum")
            if _text(value):
                explicit = str(value).strip()
                break
        if explicit is not None:
            if _datum(record, explicit) is None:
                findings.append(
                    _finding(
                        "discovery.body_datum_missing",
                        f"body:{root}",
                        "body_datum annotation names no recorded coordinate system",
                        {"datum": explicit},
                    )
                )
                continue
            datum_name = explicit
        else:
            owned = sorted({str(item["name"]) for item in datums if str(item.get("owner") or "") in members})
            shared = sorted({str(item["name"]) for item in datums if not str(item.get("owner") or "")})
            accepted = {str(name)}
            if stem_name:
                accepted.add(str(stem_name))
            accepted |= {f"cs_{value}" for value in list(accepted)}
            matches = sorted({str(item["name"]) for item in datums if _snake(item["name"]) in accepted})
            if len(owned) == 1:
                datum_name = owned[0]
            elif len(matches) == 1:
                datum_name = matches[0]
            elif owned or shared:
                findings.append(
                    _finding(
                        "discovery.body_datum_ambiguous",
                        f"body:{root}",
                        "several coordinate systems could frame this body; annotate dp.body_datum",
                        {"owned": owned, "assembly": shared, "name_matches": matches},
                    )
                )
                continue
            else:
                findings.append(
                    _finding(
                        "discovery.body_datum_missing",
                        f"body:{root}",
                        "no native coordinate system is bound to this body",
                    )
                )
                continue
        bodies.append(
            {
                "root": root,
                "components": members,
                "name": str(name),
                "source": source,
                "identity": identity,
                "datum": str(datum_name),
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


def _root_body(
    record: dict, clusters: _Clusters, bodies: list[dict], namespace: str, findings: list[dict]
) -> dict | None:
    fixed_roots = {
        clusters.of[str(item.get("name2"))]
        for item in record.get("components") or []
        if isinstance(item, dict)
        and item.get("fixed")
        and not item.get("suppressed")
        and str(item.get("name2")) in clusters.of
    }
    if len(fixed_roots) == 1:
        root = next(iter(fixed_roots))
        return next((body for body in bodies if body["root"] == root), None)
    properties = (record.get("properties") or {}).get("components") or {}
    annotated = {
        clusters.of[name]
        for name, values in properties.items()
        if isinstance(values, dict)
        and str(values.get(f"{namespace}.body_root") or "").strip().lower() in {"1", "true", "yes"}
        and name in clusters.of
    }
    if len(annotated) == 1:
        root = next(iter(annotated))
        return next((body for body in bodies if body["root"] == root), None)
    findings.append(
        _finding(
            "discovery.root_ambiguous",
            "assembly",
            "no fixed component or dp.body_root annotation identifies the base body",
            {"fixed_clusters": sorted(fixed_roots)},
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
    namespace = NAMESPACE
    frozen = settings.frozen_names or {}
    assigned: dict[str, str] = {}
    taken: dict[str, str] = {}
    for joint in joints:
        if "parent" not in joint:
            continue
        raw = joint["mate"]
        identity = raw or f"{joint['parent']}:{joint['child']}"
        candidates: dict[str, str] = {}
        value = joint["properties"].get(f"{namespace}.joint.name")
        if _text(value):
            candidates["property"] = str(value).strip()
        if identity in frozen:
            candidates["frozen"] = str(frozen[identity])
        if len(set(candidates.values())) > 1:
            findings.append(
                _finding(
                    "discovery.joint_name_conflict",
                    f"mate:{raw}",
                    "frozen name and CAD annotation disagree for one joint",
                    {"identity": identity, "candidates": candidates},
                )
            )
            continue
        if candidates:
            name = next(iter(candidates.values()))
        else:
            name = _snake(raw) or _snake(f"{joint['parent']}_{joint['child']}")
        if not _text(name) or _SNAKE.fullmatch(str(name)) is None:
            findings.append(
                _finding("discovery.joint_name_invalid", f"mate:{raw}", "joint name is not snake_case", {"name": name})
            )
            continue
        name = str(name)
        if name in taken:
            findings.append(
                _finding(
                    "discovery.joint_name_duplicate",
                    f"mate:{raw}",
                    "two joints resolve to the same name",
                    {"name": name, "other": taken[name]},
                )
            )
            continue
        taken[name] = f"mate:{raw}"
        joint["name"] = name
        if identity in frozen:
            assigned[identity] = name
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
        datum = _datum(record, body["datum"]) if body else None
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

        backend = SolidWorksBackend()
    record = backend.discover_native(frozen_source, {"namespace": NAMESPACE, "contract": CONTRACT})
    if not isinstance(record, dict) or record.get("schema_version") != DISCOVERY_SCHEMA:
        raise PipelineError("native discovery backend returned an unexpected record schema")
    findings: list[dict] = []
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
    clusters = _clusters(record, findings, NAMESPACE)
    bodies, by_root = _body_records(record, clusters, settings, findings)
    joints = _joint_facts(record, clusters, settings, findings)
    root = _root_body(record, clusters, bodies, NAMESPACE, findings)
    if root is not None:
        owner = by_root[root["root"]]
        if owner["name"] != "base_link":
            findings.append(
                _finding(
                    "discovery.root_name_pinned",
                    f"body:{owner['root']}",
                    "the base body keeps the contract name base_link",
                    {"derived": owner["name"]},
                    blocking=False,
                )
            )
        owner["name"] = "base_link"
    else:
        bodies.append(
            {
                "root": "__unresolved__",
                "components": [],
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
        identity, bodies, joints, record, settings, records, checks, run_id, discovery_sha256, handoff_sha256
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
    for joint in sorted(joints, key=lambda item: item.get("name") or ""):
        if "parent" not in joint or "name" not in joint:
            continue
        body = by_name[joint["child"]]
        datum = _datum(record, body["datum"])
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
