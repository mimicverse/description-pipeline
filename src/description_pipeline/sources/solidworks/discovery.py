"""CAD-only semantic discovery: from native primitives to a prepared package.

The mechanical team supplies one immutable SolidWorks engineering directory.
This module reads the *raw* native record produced by an owned CAD session
(:meth:`CadBackend.discover_native`), derives rigid memberships, joints, axes,
limits and physical facts with explicit provenance, and writes an internal
prepared package: its generated content identity ``source.json``, the raw
discovery record and the findings list.  The engineering files are copied
through unmodified at the same relative paths.

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
    ``{name2, document, configuration, fixed, suppressed, lightweight,
    suppression_state}`` for every component occurrence; ``suppression_state``
    is the raw documented ``GetSuppression2`` integer (0 suppressed, 1
    lightweight, 2 fully resolved, 3 resolved, 4 fully lightweight).  State 5
    (internal-ID mismatch) and any missing, boolean or undocumented value is
    refused, and both emitted flags are required booleans that must agree with
    the state (``suppressed == (state == 0)``, ``lightweight == (state in
    {1, 4})``).
``mates``
    ``{name, type, entities: [{component, feature, face_index, cylinder}],
    alignment, limits: {lower, upper, unit}, suppressed}`` with each cylinder
    given in its component frame (``point``/``direction``/``radius``).
    A circular edge (``circle``: centre/normal/radius) carries the same axis or
    plane data as a cylinder/plane where a mate's semantics allow it.
    A mate entity may instead be the frozen top assembly's own frame:
    ``{component: "", assembly_frame: true, ...}`` with geometry recorded in the
    assembly frame.  Such mates only ground exactly one rigid cluster when they
    reach full rank, and only that cluster may become ``CS_base_link``.
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
import ntpath
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from collections.abc import Mapping, Sequence

from ...io import PipelineError, artifact_path_parts, confined, digest, inventory, parse_data, read_data
from ...interface_records import (
    PREVIEW_RANGE_KEYS,
    PREVIEW_RANGE_SCHEMA,
    PURPOSE_ENGINEERING,
    PURPOSE_INSPECTION_PREVIEW,
    preview_revolute_window_rad,
)
from ...request_evidence import read_request_evidence
from .jsonio import write_json
from .errors import CadError
from .exporter import ExporterDeclarationError, decode_exporter_v14
from .revision import package_inventory
from .session_artifacts import session_materialization_under_root, session_root_pid
from ...schemas import SOURCE_RECORD_SCHEMA, exporter_declaration_present

DISCOVERY_SCHEMA = "solidworks-to-urdf.native-discovery/v1"
PREPARED_SCHEMA = "solidworks-to-urdf.prepared-package/v1"
FINDING_SCHEMA = "solidworks-to-urdf.discovery-finding/v1"
SOURCE_FILE = "source.json"
DISCOVERY_FILE = "discovery/native-discovery.json"
FINDINGS_FILE = "discovery/findings.json"
IDENTITY_FILE = "discovery/identity-resolution.json"
PLAN_FILE = "capture-plan.json"
PLAN_SCHEMA = "solidworks-to-urdf.capture-plan/v1"
IDENTITY_SCHEMA = "solidworks-to-urdf.identity-resolution/v1"
RECORD_DIR = "records"
#: The one native contract: a fixed property namespace and one record version.
NAMESPACE = "dp"
CONTRACT = "native-discovery/v1"
GENERATOR = "native-discovery"
GENERATOR_VERSION = "1"
_DATE_ID = re.compile(r"[0-9]{8}\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")

#: The prepared package must satisfy the same joint geometry gate as an
#: authored package: a movable joint frame has to sit on the native shaft.
AXIS_OFFSET_TOL_M = 5e-5

CONTROL_SYSTEMS = ("git", "pdm", "handoff")

_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
_IDENTITY_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_GIT_COMMIT = re.compile(r"\b[0-9a-f]{40}(?:[0-9a-f]{24})?\b")

#: Datum name spaces the spec recognises: link frames, tool/sensor interfaces
#: and joint frames.  A datum is ``<PREFIX>_<snake_case>``: the suffix must be
#: exact snake_case before any transformation, so ``TCP_Tool`` blocks instead
#: of silently becoming ``tcp_tool``.  Any owned ``CS_*`` datum that is not the
#: body's link datum is a named interface frame.
INTERFACE_PREFIXES = ("CS_", "TCP_", "SCS_")
JCS_PREFIX = "JCS_"
_TOL = 1e-6

#: Mate types whose constraint rows this module can reconstruct from recorded
#: entity geometry. Unknown types never establish rigid membership; declaration
#: mode retains their rejection as a diagnostic without vetoing the declared tree.
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
    """The platform-controlled inputs: records, published names, the entry and the purpose.

    ``purpose`` mirrors the frozen request solely as in-flight derivation input
    (the persisted ``input/request.json`` remains the single purpose authority):
    it selects which joint parameters may be proven missing and whether the
    policy preview window or a bound preview declaration supplies them.  No
    consumer may persist it as a second authority.
    """

    record_roots: tuple[Path, ...] = ()
    frozen_names: Mapping[str, str] = field(default_factory=dict)
    main_assembly: str | None = None
    purpose: str = PURPOSE_ENGINEERING


@dataclass(frozen=True)
class SubmissionIdentity:
    """Administrative identity supplied by the portal submission.

    Used only to fill CAD-absent fields; CAD-carried ``dp.*`` values always win,
    and a differing submitted hardware identity blocks publication.  ``as_of``
    is the submission's UTC date (``yyyymmdd``) and ``inventory_digest`` the
    frozen CAD inventory digest used for the deterministic revision id.
    """

    hardware_id: str | None = None
    owner: str | None = None
    change_summary: str | None = None
    parent_revision: str | None = None
    control_reference: str | None = None
    as_of: str | None = None
    inventory_digest: str | None = None


@dataclass(frozen=True)
class PreparedPackage:
    """Internal prepared inputs bound to immutable CAD plus raw discovery evidence."""

    schema_version: str
    run_id: str
    package: Path
    handoff_sha256: str  # original frozen native input inventory
    prepared_sha256: str  # generated package identity
    hardware_id: str | None
    revision: str | None
    discovery_path: Path
    discovery_sha256: str
    findings: tuple[dict, ...]
    passed: bool


def _text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _authored_text(value: Any) -> bool:
    """Non-empty text without control characters, as the revision manifest requires."""

    return isinstance(value, str) and value.strip() != "" and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _authored_multiline_text(value: Any) -> bool:
    """Non-empty text whose only control character is the U+000A line break."""

    return (
        isinstance(value, str)
        and value.strip() != ""
        and not any((ord(c) < 32 and c != "\n") or ord(c) == 127 for c in value)
    )


def _saved_configuration_observed(record: dict, name: str) -> bool:
    """Prove the delivery configuration from immutable bytes and native state.

    The sealed source-selection inventory is the pre-read authority; the native
    record's file inventory is the post-read authority.  Both must agree, and
    both document-state readings must come from a read-only document whose
    active configuration is the requested name.  ``NeedsRebuild2`` is the
    documented rebuild-status enum, not a boolean: the initial reading may be
    non-frozen (1) or frozen-feature dirty (2), but the final reading must be
    fully rebuilt (0).  ``GetSaveFlag`` is diagnostic only because benign
    native reads may make an in-memory document dirty.
    """

    state = record.get("document_state")
    if not isinstance(state, dict):
        return False
    selection = record.get("source_selection")
    files = record.get("files")
    if (
        not isinstance(selection, dict)
        or not isinstance(files, dict)
        or selection.get("files") != files
    ):
        return False
    readings = (state.get("before"), state.get("after"))
    if not all(isinstance(reading, dict) for reading in readings):
        return False

    def proven(reading: dict) -> bool:
        rebuild = reading.get("needs_rebuild2_value")
        return (
            reading.get("read_only") is True
            and reading.get("configuration_needs_rebuild") is False
            and isinstance(reading.get("configuration"), str)
            and reading["configuration"].strip() == name
            and type(rebuild) is int
            and rebuild in {0, 1, 2}
        )

    before, after = readings
    return proven(before) and proven(after) and after["needs_rebuild2_value"] == 0


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


#: The frozen assembly's own frame: identity at the assembly origin.  A mate entity
#: marked ``assembly_frame`` binds here (component ``""``) instead of to a component.
_ASSEMBLY_FRAME = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]


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


#: Orientation agreement bound for evidence cross-checks: the quality contract's
#: 0.05 degrees; positions use the same contract's 0.05 mm (``AXIS_OFFSET_TOL_M``).
_ORIENTATION_TOL_RAD = math.radians(0.05)
#: Flat face-evidence kinds the record may carry; a flat point is a bare 3-vector by API.
_FLAT_FACE_KINDS = ("plane", "cylinder", "circle")
#: Comparable localized kinds; a localized cylinder is only ever cross-checked, never created.
_LOCALIZED_GEOMETRY_FIELDS = {
    "point": ("point",),
    "line": ("point", "direction"),
    "plane": ("point", "normal"),
    "cylinder": ("point", "direction", "radius"),
}


def _finite_vector(value: Any, length: int) -> list[float] | None:
    """A finite numeric vector of the exact length; booleans, NaN and Infinity fail."""
    if isinstance(value, (str, bytes, dict)) or value is None:
        return None
    try:
        items = list(value)
    except TypeError:
        return None
    if len(items) != length:
        return None
    numbers: list[float] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        numbers.append(number)
    return numbers


def _unit_vector(value: Any) -> list[float] | None:
    numbers = _finite_vector(value, 3)
    if numbers is None:
        return None
    norm = math.sqrt(sum(number * number for number in numbers))
    if norm <= 0 or not math.isfinite(norm):
        return None
    return [number / norm for number in numbers]


def _undirected_angle(first, second) -> float | None:
    """Angle between two undirected directions, in radians, or ``None`` when unusable."""
    dot = sum(one * two for one, two in zip(first, second, strict=True))
    return math.acos(min(1.0, abs(dot)))


def _same_geometry(kind: str, recorded: dict, localized: dict) -> bool:
    """Agreement between flat face evidence and localized provenance within contract bounds.

    The quality numerical contract's position (0.05 mm) and orientation (0.05 degrees) bounds
    are the cross-check bounds.  Planes and axes are undirected: a flipped normal or direction
    describes the same reference; a plane is compared by orientation and offset rather than by
    arbitrary point equality.
    """

    def plane_offset(p, o, n) -> float:
        return abs(sum((p[index] - o[index]) * n[index] for index in range(3)))

    def axis_offset(p, o, d) -> float:
        offset = [p[index] - o[index] for index in range(3)]
        cross = _cross(offset, d)
        return math.sqrt(sum(value * value for value in cross))

    if kind == "plane":
        recorded_normal, localized_normal = _unit_vector(recorded.get("normal")), _unit_vector(localized.get("normal"))
        recorded_point, localized_point = (
            _finite_vector(recorded.get("point"), 3),
            _finite_vector(localized.get("point"), 3),
        )
        if None in (recorded_normal, localized_normal, recorded_point, localized_point):
            return False
        angle = _undirected_angle(recorded_normal, localized_normal)
        if angle is None or angle > _ORIENTATION_TOL_RAD:
            return False
        return plane_offset(localized_point, recorded_point, recorded_normal) <= AXIS_OFFSET_TOL_M
    if kind in {"line", "cylinder"}:
        recorded_direction, localized_direction = (
            _unit_vector(recorded.get("direction")),
            _unit_vector(localized.get("direction")),
        )
        recorded_point, localized_point = (
            _finite_vector(recorded.get("point"), 3),
            _finite_vector(localized.get("point"), 3),
        )
        if None in (recorded_direction, localized_direction, recorded_point, localized_point):
            return False
        angle = _undirected_angle(recorded_direction, localized_direction)
        if angle is None or angle > _ORIENTATION_TOL_RAD:
            return False
        if kind == "cylinder":
            recorded_radius, localized_radius = recorded.get("radius"), localized.get("radius")
            if (
                isinstance(recorded_radius, bool)
                or isinstance(localized_radius, bool)
                or not isinstance(recorded_radius, (int, float))
                or not isinstance(localized_radius, (int, float))
                or not math.isfinite(float(recorded_radius))
                or not math.isfinite(float(localized_radius))
                or abs(float(recorded_radius) - float(localized_radius)) > AXIS_OFFSET_TOL_M
            ):
                return False
        return (
            axis_offset(localized_point, recorded_point, recorded_direction) <= AXIS_OFFSET_TOL_M
            and axis_offset(recorded_point, localized_point, localized_direction) <= AXIS_OFFSET_TOL_M
        )
    return False


def _entity_geometry_view(entity: dict, findings: list[dict], obj: str) -> dict | None:
    """One mate entity with its validated geometry, or ``None`` when it must block.

    Recorded face evidence (a flat plane/cylinder/circle, or the bare-list point) stays
    primary: missing, unverifiable or irrelevant localized provenance never blocks it, while a
    comparable localized reference that contradicts the face evidence does.  When no face
    evidence exists, a component-local localized point/line/plane is validated and surfaced as
    the fallback; a needed reference that is malformed, non-component-local or of any other
    kind blocks, and a localized cylinder is never turned into face evidence.
    """

    view = dict(entity)
    # A top-level ``line`` key is not part of the recorded face-evidence schema (the reader
    # never emits one and the independent oracle never treats it as evidence); dropping it
    # keeps the validated reference single on both sides instead of letting a stray key win.
    view.pop("line", None)
    flat: dict[str, dict] = {}
    malformed: list[str] = []
    for face_kind in _FLAT_FACE_KINDS:
        value = entity.get(face_kind)
        if value is None:
            continue
        if isinstance(value, dict):
            flat[face_kind] = value
        else:
            malformed.append(face_kind)
    raw_point = entity.get("point")
    flat_point = None
    if raw_point is not None:
        flat_point = _finite_vector(raw_point, 3)
        if flat_point is None:
            malformed.append("point")
    if malformed:
        # A recorded primary that is malformed is refused, never silently substituted by
        # provenance; the same evidence rule applies to the independent oracle.
        findings.append(
            _finding(
                "discovery.mate_entities_unsupported",
                obj,
                "recorded mate entity geometry is malformed",
                {"kinds": sorted(set(malformed))},
            )
        )
        return None
    usable_flat = bool(flat) or flat_point is not None
    reference = entity.get("mate_entity_reference")
    if reference is None:
        return view
    if not isinstance(reference, dict):
        if usable_flat:
            return view
        findings.append(_finding("discovery.mate_entities_unsupported", obj, "mate entity reference is not an object"))
        return None
    if reference.get("error") is not None:
        # A failed decode cannot create new evidence; recorded face evidence stays usable.
        if usable_flat:
            return view
        findings.append(
            _finding(
                "discovery.mate_entities_unsupported",
                obj,
                "localized mate entity geometry failed to decode",
                {"error": str(reference.get("error"))[:200]},
            )
        )
        return None
    geometry = reference.get("geometry")
    if not isinstance(geometry, dict):
        # No localized provenance to validate: the branches report missing geometry themselves.
        return view
    kind = str(geometry.get("kind") or "")
    if geometry.get("frame") != "component-local":
        if usable_flat:
            return view
        findings.append(
            _finding(
                "discovery.mate_entities_unsupported",
                obj,
                "localized mate entity geometry lacks component-local provenance",
            )
        )
        return None
    if kind == "cylinder" and "cylinder" not in flat:
        # A shaft is face evidence or it does not exist; localization never fabricates one.
        findings.append(
            _finding(
                "discovery.mate_entities_unsupported",
                obj,
                "localized cylinder geometry has no recorded face evidence",
                {"feature": entity.get("feature")},
            )
        )
        return None
    if kind not in _LOCALIZED_GEOMETRY_FIELDS:
        if usable_flat:
            return view
        findings.append(
            _finding(
                "discovery.mate_entities_unsupported",
                obj,
                "localized mate entity geometry carries an unsupported kind",
                {"kind": kind},
            )
        )
        return None
    values: dict[str, Any] = {}
    for part in _LOCALIZED_GEOMETRY_FIELDS[kind]:
        raw_value = geometry.get(part)
        if part == "radius":
            if (
                isinstance(raw_value, bool)
                or not isinstance(raw_value, (int, float))
                or not math.isfinite(float(raw_value))
                or float(raw_value) <= 0
            ):
                break
            values[part] = float(raw_value)
            continue
        numbers = _finite_vector(raw_value, 3)
        if numbers is None or (part in ("direction", "normal") and not any(number != 0.0 for number in numbers)):
            break
        values[part] = numbers
    if set(values) != set(_LOCALIZED_GEOMETRY_FIELDS[kind]):
        if usable_flat:
            return view
        findings.append(
            _finding(
                "discovery.mate_entities_unsupported",
                obj,
                "localized mate entity geometry is malformed",
                {"kind": kind},
            )
        )
        return None
    if kind == "point":
        if flat_point is not None:
            distance = math.sqrt(sum((flat_point[index] - values["point"][index]) ** 2 for index in range(3)))
            if distance > AXIS_OFFSET_TOL_M:
                findings.append(
                    _finding(
                        "discovery.mate_entities_unsupported",
                        obj,
                        "localized mate entity geometry contradicts recorded face evidence",
                        {"kind": kind},
                    )
                )
                return None
            return view
        if usable_flat:
            # A localized point cannot coexist with recorded face evidence of another kind:
            # the shapes contradict each other, and merging them would invent a reference.
            findings.append(
                _finding(
                    "discovery.mate_entities_unsupported",
                    obj,
                    "localized mate entity geometry contradicts recorded face evidence",
                    {"kind": kind},
                )
            )
            return None
        # A recorded point is a bare 3-vector, matching ``point_of`` and the existing API.
        view["point"] = values["point"]
        return view
    if kind in flat:
        if not _same_geometry(kind, flat[kind], values):
            findings.append(
                _finding(
                    "discovery.mate_entities_unsupported",
                    obj,
                    "localized mate entity geometry contradicts recorded face evidence",
                    {"kind": kind},
                )
            )
            return None
        return view
    if usable_flat:
        # A different comparable geometry than the recorded face evidence is a contradiction.
        findings.append(
            _finding(
                "discovery.mate_entities_unsupported",
                obj,
                "localized mate entity geometry contradicts recorded face evidence",
                {"kind": kind},
            )
        )
        return None
    view[kind] = values
    return view


def _mate_rows(mate: dict, frames: dict, findings: list[dict], obj: str) -> dict | None:
    """Reconstruct a mate's constraints as twists at the assembly origin.

    Entity geometry is recorded in its component frame and transformed here
    with that component's occurrence transform, so translation and rotation
    are coupled through the real entity points. ``None`` means the mate supplies
    no usable rows: an unknown mate must not become a rigid connection.
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
    first = _entity_geometry_view(entities[0], findings, obj)
    second = _entity_geometry_view(entities[1], findings, obj)
    if first is None or second is None:
        return None
    # A limits reading is an observation, not a default.  Only an intact reading
    # (an explicit null, or a mapping) may claim anything; a missing key or a
    # non-mapping value refuses before either purpose may treat it as absence.
    if "limits" not in mate:
        findings.append(
            _finding(
                "discovery.mate_limits_observation_incomplete",
                obj,
                "the mate observation carries no limits reading; a missing reading cannot prove absence",
                {"mate": mate.get("name")},
            )
        )
        return None
    raw_limits = mate["limits"]
    if raw_limits is not None and not isinstance(raw_limits, dict):
        findings.append(
            _finding(
                "discovery.mate_limits_observation_invalid",
                obj,
                "the mate limits reading is not a usable observation",
                {"mate": mate.get("name"), "value_type": type(raw_limits).__name__},
            )
        )
        return None
    limits = raw_limits if isinstance(raw_limits, dict) else None

    def fail(code: str, message: str, detail: Any = None) -> None:
        findings.append(_finding(code, obj, message, detail))
        return None

    def frame_of(entity: dict):
        if entity.get("component") == "" and entity.get("assembly_frame") is True:
            # The owning assembly's own frame: a real constraint against the assembly
            # origin (the global frame for the frozen root), not a component identity.
            return _ASSEMBLY_FRAME
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

    def line_of(entity: dict):
        """A recorded line/axis reference as (point, unit direction) in the assembly frame."""

        frame = frame_of(entity)
        line = entity.get("line")
        if frame is None or not isinstance(line, dict):
            return None
        direction = _unit(_vector(line.get("direction") or (), frame))
        try:
            point = _point([float(value) for value in line.get("point") or ()], frame)
        except (TypeError, ValueError):
            point = None
        if direction is None or point is None or len(point) != 3:
            fail(
                "discovery.mate_entities_unsupported",
                "recorded line is not usable",
                {"feature": entity.get("feature")},
            )
            return None
        return point, direction

    def circle_of(entity: dict):
        """A recorded circular edge as (centre, unit normal) in the assembly frame."""

        frame = frame_of(entity)
        circle = entity.get("circle")
        if frame is None or not isinstance(circle, dict):
            return None
        try:
            normal = _unit(_vector(circle.get("normal") or (), frame))
        except (TypeError, ValueError):
            normal = None
        try:
            centre = _point([float(value) for value in circle.get("center") or ()], frame)
        except (TypeError, ValueError):
            centre = None
        if normal is None or centre is None or len(centre) != 3:
            fail(
                "discovery.mate_entities_unsupported",
                "recorded circular edge is not usable",
                {"feature": entity.get("feature")},
            )
            return None
        return centre, normal

    def axial_geometry_of(entity: dict):
        """A cylinder face or circular edge as (point, unit direction)."""

        if isinstance(entity.get("cylinder"), dict):
            return cylinder_of(entity)
        return circle_of(entity)

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

    def concentric_point_of(entity: dict):
        """A *pure* point reference for a point-on-axis concentric mate, or ``None``.

        Only a validated point view qualifies — a bare recorded point or a point
        that already passed the component-local localized-provenance checks.
        Planes, lines, cylinders and circles never do, so the axis of the relation
        can only ever come from the other entity's recorded cylinder/circular-edge
        evidence; the point's own ``EntityParams`` contribute no direction and no
        radius.  An unusable point frame keeps failing closed through ``point_of``.
        """

        if any(isinstance(entity.get(shape), dict) for shape in ("plane", "cylinder", "circle", "line")):
            return None
        if not isinstance(entity.get("point"), (list, tuple)):
            return None
        return point_of(entity)

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
        left = axial_geometry_of(first)
        right = axial_geometry_of(second)
        if left is None or right is None:
            if (left is None) != (right is None):
                # One recorded axis plus one pure point reference is a point-on-axis
                # relation, not an axis alignment: the solved state must already place
                # the point on the axis, and the relation removes exactly the two
                # translations perpendicular to the axis.  The constraint rows are the
                # point's velocity projections applied AT the recorded point, so the
                # angular moment arms (point x direction) stay in the twist convention
                # used by this module ([translation, rotation] at the assembly origin)
                # — a displaced point is never reduced to origin translations.
                axis_geometry = right if left is None else left
                point_entity = first if left is None else second
                point = concentric_point_of(point_entity)
                if point is not None:
                    offset = [point[index] - axis_geometry[0][index] for index in range(3)]
                    along = _dot(offset, axis_geometry[1])
                    radial = [offset[index] - along * axis_geometry[1][index] for index in range(3)]
                    radial_gap = math.sqrt(_dot(radial, radial))
                    if radial_gap > AXIS_OFFSET_TOL_M:
                        return fail(
                            "discovery.mate_geometry_mismatch",
                            "solved concentric point is off the recorded axis",
                            {"radial_gap_m": radial_gap, "tolerance_m": AXIS_OFFSET_TOL_M},
                        )
                    rows = [_translation_row(direction, point) for direction in _orthogonal_basis(axis_geometry[1])]
                    # No shaft selector and no joint: point-on-axis is a two-row
                    # relation, never a four-row axis alignment.
                    return {"rows": rows, "limits": limits, "axis": None, "point": point, "entity": None}
            return fail(
                "discovery.mate_entities_unsupported",
                "concentric mate needs two recorded cylinders or circular edges",
            )
        if abs(abs(_dot(left[1], right[1])) - 1.0) > _TOL:
            return fail("discovery.mate_geometry_mismatch", "concentric mate axes are not parallel in the solved state")
        delta = [right[0][index] - left[0][index] for index in range(3)]
        along = _dot(delta, left[1])
        radial = [delta[index] - along * left[1][index] for index in range(3)]
        radial_gap = math.sqrt(_dot(radial, radial))
        if radial_gap > AXIS_OFFSET_TOL_M:
            return fail(
                "discovery.joint_axis_misaligned",
                "concentric mate references are parallel but radially displaced",
                {"radial_gap_m": radial_gap, "tolerance_m": AXIS_OFFSET_TOL_M},
            )
        axis = left[1]
        basis = _orthogonal_basis(axis)
        rows = [_translation_row(direction, left[0]) for direction in basis]
        rows += [_rotation_row(direction) for direction in basis]
        # The documented concentric rotation lock removes the spin only.
        # Axial translation remains free until another native mate constrains it.
        if (mate.get("lock_rotation") is True
                and mate.get("lock_rotation_api") == "IConcentricMateFeatureData.LockRotation"):
            rows.append(_rotation_row(axis))
        # Rows may be reconstructed from a circular edge, but shaft evidence is
        # only ever an actual cylindrical face: a circle normal can be flipped
        # against the vendor axis, and a circle carries no cylinder contract.
        shaft = next(
            (
                (entity, geometry)
                for entity, geometry in ((first, left), (second, right))
                if isinstance(entity.get("cylinder"), dict)
            ),
            None,
        )
        if shaft is None:
            return {"rows": rows, "limits": limits, "axis": None, "point": None, "entity": None}
        shaft_entity, shaft_geometry = shaft
        return {
            "rows": rows,
            "limits": limits,
            "axis": shaft_geometry[1],
            "point": shaft_geometry[0],
            "entity": shaft_entity,
        }
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

        def circle_plane_rows(circle_entity: dict, plane_entity: dict):
            # A circular edge coincident with a plane lies in that plane: the
            # row model locks the same alignment and position as plane-plane.
            geometry = circle_of(circle_entity)
            plane = plane_of(plane_entity)
            if geometry is None or plane is None:
                return None
            centre, normal = geometry
            if abs(abs(_dot(normal, plane[1])) - 1.0) > _TOL:
                return fail("discovery.mate_geometry_mismatch", "solved coincident circle and plane are not parallel")
            direction = normal if _dot(normal, plane[1]) >= 0 else [-value for value in normal]
            separation = abs(_dot([centre[index] - plane[0][index] for index in range(3)], plane[1]))
            if separation > AXIS_OFFSET_TOL_M:
                return fail(
                    "discovery.mate_geometry_mismatch",
                    "solved coincident circle and plane are not co-located",
                    {"separation_m": separation},
                )
            rows = [_translation_row(direction, centre)]
            rows += [_rotation_row(item) for item in _orthogonal_basis(direction)]
            return {"rows": rows, "limits": limits, "axis": None, "point": centre, "entity": None}

        if isinstance(first.get("circle"), dict) and isinstance(second.get("plane"), dict):
            return circle_plane_rows(first, second)
        if isinstance(first.get("plane"), dict) and isinstance(second.get("circle"), dict):
            return circle_plane_rows(second, first)

        def line_plane_rows(line_entity: dict, plane_entity: dict):
            # A line coincident with a plane lies in that plane: the recorded solved state
            # must already show the direction perpendicular to the normal and the point on
            # the plane; only then are exactly those two constraint rows emitted.
            line = line_of(line_entity)
            plane = plane_of(plane_entity)
            if line is None or plane is None:
                return None
            point, direction = line
            normal = plane[1]
            if abs(_dot(direction, normal)) > _TOL:
                return fail("discovery.mate_geometry_mismatch", "solved coincident line and plane are not coplanar")
            separation = abs(_dot([point[index] - plane[0][index] for index in range(3)], normal))
            if separation > AXIS_OFFSET_TOL_M:
                return fail(
                    "discovery.mate_geometry_mismatch",
                    "solved coincident line and plane are not co-located",
                    {"separation_m": separation},
                )
            pivot = _unit(_cross(direction, normal))
            rows = [_rotation_row(pivot)] if pivot is not None else []
            rows.append(_translation_row(normal, point))
            return {"rows": rows, "limits": limits, "axis": None, "point": point, "entity": None}

        if isinstance(first.get("line"), dict) and isinstance(second.get("plane"), dict):
            return line_plane_rows(first, second)
        if isinstance(first.get("plane"), dict) and isinstance(second.get("line"), dict):
            return line_plane_rows(second, first)
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
        if axis is not None:
            rows = [_rotation_row(direction) for direction in _orthogonal_basis(axis)]
            return {"rows": rows, "limits": limits, "axis": None, "point": None, "entity": None}
        first_is_line = isinstance(first.get("line"), dict)
        second_is_line = isinstance(second.get("line"), dict)
        if first_is_line and second_is_line:
            # Two axes: their directions must be parallel (two rotational constraints).
            line, other = line_of(first), line_of(second)
            if line is None or other is None:
                return fail("discovery.mate_entities_unsupported", "parallel mate entities carry no direction")
            if abs(abs(_dot(line[1], other[1])) - 1.0) > _TOL:
                return fail(
                    "discovery.mate_geometry_mismatch", "solved parallel mate does not record parallel directions"
                )
            rows = [_rotation_row(direction) for direction in _orthogonal_basis(line[1])]
            return {"rows": rows, "limits": limits, "axis": None, "point": None, "entity": None}
        if first_is_line != second_is_line:
            # One axis and one plane: parallel means the axis lies parallel to the plane, which
            # removes exactly one rotational freedom (axis perpendicular to the plane normal) --
            # never the two a parallel axis pair removes.  Validate the solved state first.
            line_entity, plane_entity = (first, second) if first_is_line else (second, first)
            line, plane = line_of(line_entity), plane_of(plane_entity)
            if line is None or plane is None:
                return fail("discovery.mate_entities_unsupported", "parallel mate entities carry no direction")
            direction, normal = line[1], plane[1]
            if abs(_dot(direction, normal)) > _TOL:
                return fail("discovery.mate_geometry_mismatch", "solved parallel line and plane are not parallel")
            pivot = _unit(_cross(direction, normal))
            if pivot is None:
                return fail("discovery.mate_geometry_mismatch", "solved parallel line and plane are not parallel")
            return {"rows": [_rotation_row(pivot)], "limits": limits, "axis": None, "point": None, "entity": None}
        plane = plane_of(first)
        other = plane_of(second)
        if plane is None or other is None:
            return fail("discovery.mate_entities_unsupported", "parallel mate entities carry no direction")
        if abs(abs(_dot(plane[1], other[1])) - 1.0) > _TOL:
            return fail("discovery.mate_geometry_mismatch", "solved parallel mate does not record parallel directions")
        rows = [_rotation_row(direction) for direction in _orthogonal_basis(plane[1])]
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


@dataclass(frozen=True)
class _FrameAttachment:
    """The proven frozen top assembly-frame attachment.

    ``members`` is the complete occurrence set of the one rigid cluster all
    frame-attached mates ground; only that cluster may act as the base body.
    """

    members: frozenset[str]
    components: tuple[str, ...]
    mates: tuple[str, ...]
    rank: int


@dataclass
class _Clusters:
    members: dict[str, list[str]]
    of: dict[str, str]
    #: Per component pair: the intersection of its mates' allowed motions and
    #: the mate indices that take part in it.
    pairs: dict[tuple[str, str], dict]
    #: The proven top assembly-frame attachment, or ``None`` when absent, nested
    #: or incomplete; only the proven cluster may bind top-assembly datums.
    frame: _FrameAttachment | None = None


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
    frame_rows: list[list[float]] = []
    frame_endpoints: list[str] = []
    frame_mates: list[str] = []
    frame_ok = True

    def frame_problem(message: str, detail: Any = None, *, obj: str = "assembly") -> None:
        nonlocal frame_ok
        frame_ok = False
        findings.append(_finding("discovery.frame_attachment", obj, message, detail))

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
        entities = _mate_entities(mate)
        if any(entity.get("assembly_frame") is True for entity in entities):
            # A mate to the frozen top assembly's own frame grounds exactly one
            # rigid cluster; it is never a component pair, invented grounding or
            # a mass.  Only a complete top-scope mate with one identity frame
            # entity and one recorded occurrence can take part.
            scope = mate.get("scope")
            flagged = [entity for entity in entities if entity.get("assembly_frame") is True]
            nested_self = scope != "" and len(flagged) == 1 and flagged[0].get("component") == scope
            if scope == "":
                frame_mates.append(obj)
            elif nested_self:
                # A nested sub-assembly's own frame is a definition-internal relation.
                # The reader binds its component to the mate's scope, validates that the
                # scope occurrence and document match at capture time, and localizes the
                # parameters with inv(F_scope) x F_scope, so the recorded scope-local
                # geometry is mapped back exactly by the recorded occurrence transform.
                # It is therefore reconstructed by the ordinary pair machinery below:
                # it can never ground anything and never vetoes the frozen top aggregate.
                # The non-frame endpoint must be a recorded occurrence under the mate's
                # scope: the reader records every in-scope component as ``scope/...``,
                # so any other name is an unjustified scope cross-link and is rejected
                # rather than guessed at.
                others = [entity for entity in entities if entity.get("assembly_frame") is not True]
                endpoints = [str(entity.get("component") or "") for entity in others]
                raw_entities = mate.get("entities")
                if (
                    not isinstance(raw_entities, list)
                    or len(raw_entities) != 2
                    or any(not isinstance(item, dict) for item in raw_entities)
                    or len(endpoints) != 1
                    or not endpoints[0]
                    or not endpoints[0].startswith(scope + "/")
                    or endpoints[0] not in components
                    or scope not in components
                ):
                    findings.append(
                        _finding(
                            "discovery.frame_attachment",
                            obj,
                            "a frame-attached mate outside the frozen top assembly is not supported",
                            {
                                "mate": mate.get("name"),
                                "scope": scope,
                                "components": [str(entity.get("component")) for entity in entities],
                            },
                        )
                    )
                    continue
            else:
                # Ambiguous or foreign frame identity: fail closed with its own finding,
                # scope-isolated from the frozen top aggregate gate.
                findings.append(
                    _finding(
                        "discovery.frame_attachment",
                        obj,
                        "a frame-attached mate outside the frozen top assembly is not supported",
                        {
                            "mate": mate.get("name"),
                            "scope": scope,
                            "components": [str(entity.get("component")) for entity in entities],
                        },
                    )
                )
                continue
            if scope == "":
                occurrence = [entity for entity in entities if entity.get("assembly_frame") is not True]
                if not occurrence:
                    frame_problem(
                        "a frame-attached mate has no occurrence endpoint", {"mate": mate.get("name")}, obj=obj
                    )
                    continue
                raw_entities = mate.get("entities")
                if not isinstance(raw_entities, list) or len(raw_entities) != 2 or len(entities) != 2:
                    frame_problem(
                        "a frame-attached mate must record exactly one frozen top-frame entity and one occurrence",
                        {"mate": mate.get("name"), "entities": len(entities), "frame_entities": len(flagged)},
                        obj=obj,
                    )
                    continue
                if flagged[0].get("component") != "":
                    frame_problem(
                        "a frame entity must carry the frozen top-scope identity",
                        {"mate": mate.get("name"), "component": flagged[0].get("component")},
                        obj=obj,
                    )
                    continue
                component = occurrence[0].get("component")
                if not isinstance(component, str) or component == "" or component not in components:
                    frame_problem(
                        "a frame-attached mate names an unknown occurrence",
                        {"mate": mate.get("name"), "component": component, "scope": scope},
                        obj=obj,
                    )
                    continue
                rows = _mate_rows(mate, frames, findings, obj)
                if rows is None:
                    frame_problem(
                        "a frame-attached mate cannot be reconstructed from its recorded entities",
                        {"mate": mate.get("name"), "type": mate.get("type"), "scope": scope},
                        obj=obj,
                    )
                    continue
                frame_rows.extend(rows["rows"])
                frame_endpoints.append(component)
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
    attachment: _FrameAttachment | None = None
    if frame_mates and frame_ok:
        rank = _rank(frame_rows)
        if rank != 6:
            frame_problem(
                "the aggregate frame attachment does not reach full rank; the attachment is incomplete",
                {"rank": rank, "mates": list(frame_mates), "components": sorted(set(frame_endpoints))},
            )
        else:
            keys = sorted({mapping[name] for name in frame_endpoints})
            if len(keys) != 1:
                frame_problem(
                    "frame-attached components must belong to exactly one rigid cluster",
                    {"components": sorted(set(frame_endpoints)), "clusters": [sorted(members[key]) for key in keys]},
                )
            else:
                attachment = _FrameAttachment(
                    members=frozenset(members[keys[0]]),
                    components=tuple(sorted(set(frame_endpoints))),
                    mates=tuple(frame_mates),
                    rank=rank,
                )
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
        frame=attachment,
    )
    return clusters


def _resolve_record(settings: DiscoverySettings, reference: str, findings: list[dict], obj: str) -> dict | None:
    relative, _, key = str(reference).partition("#")
    matches: dict[Path, tuple[Path, int]] = {}
    for index, root in enumerate(settings.record_roots):
        try:
            path = confined(Path(root), relative)
        except PipelineError:
            continue
        matches.setdefault(path.resolve(), (path, index))
    if not matches:
        findings.append(
            _finding(
                "discovery.record_missing", obj, "referenced controlled record was not found", {"reference": reference}
            )
        )
        return None
    if len(matches) > 1:
        findings.append(
            _finding(
                "discovery.record_ambiguous",
                obj,
                "controlled reference matches more than one specification file",
                {
                    "reference": reference,
                    "candidates": [{"root_index": index, "file": relative} for _path, index in matches.values()],
                },
            )
        )
        return None
    path, _index = next(iter(matches.values()))
    try:
        content = path.read_bytes()
        data = parse_data(content, path)
        json.dumps(data, allow_nan=False)
    except (PipelineError, OSError) as error:
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
            _finding("discovery.record_shape", obj, "record entry must be an object", {"file": relative, "key": key})
        )
        return None
    return {
        "reference": str(reference),
        "file": relative.replace("\\", "/"),
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "key": key,
        "value": node,
        "text": content.decode("utf-8-sig"),
        "content": content,
    }


def _preview_number(value: Any) -> float:
    """A finite real declaration number; booleans are not numeric facts."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return math.nan
    number = float(value)
    return number if math.isfinite(number) else math.nan


def _declared_preview_range(
    record: dict,
    settings: DiscoverySettings,
    findings: list[dict],
    obj: str,
    *,
    joint: str,
    instances: Sequence[str],
) -> tuple[dict | None, dict | None]:
    """Resolve one prismatic joint's bound finite preview declaration.

    The declaration reuses the controlled-record reference: the resolved file's
    digest and the parsed exact semantics (joint, instance, configuration, unit,
    zero, direction, purpose and a finite ordered range) are all required - a
    file that merely exists, or an anchor substring that merely matches, never
    passes.  ``source_sha256`` is not part of the shape: the controlled-record
    entry already owns and verifies the resolved file digest, so a second inner
    digest claim would be provenance theatre.

    The declared window is expressed in the declaration's own measure.  ``zero``
    is the declared value that coincides with the joint's canonical zero and
    ``direction`` (``+1``/``-1``) orients the declared positive sense against
    the canonical positive axis, so the canonical window is
    ``direction * (declared - zero)`` applied to both endpoints and ordered.
    The mapping is always applied - a descriptor is never silently dropped, and
    different zero/direction values yield different canonical bounds.
    """

    reference = (record.get("properties") or {}).get(f"{NAMESPACE}.joint.preview_range_record")
    if not _text(reference):
        findings.append(
            _finding(
                "discovery.joint_preview_range_missing",
                obj,
                "a prismatic joint with a proven-missing engineering range requires a bound finite "
                "preview declaration",
            )
        )
        return None, None
    resolved = _resolve_record(settings, str(reference), findings, obj)
    if resolved is None:
        return None, None
    value = resolved["value"]
    if not isinstance(value, dict) or set(value) != PREVIEW_RANGE_KEYS:
        findings.append(
            _finding(
                "discovery.joint_preview_range_invalid",
                obj,
                "preview declaration must carry exactly the bound finite semantics",
                {"file": resolved["file"]},
            )
        )
        return None, None
    lower = _preview_number(value["lower"])
    upper = _preview_number(value["upper"])
    zero = _preview_number(value["zero"])
    direction = value["direction"]
    sign = 1.0 if direction == "+1" else -1.0 if direction == "-1" else math.nan
    first, second = sign * (lower - zero), sign * (upper - zero)
    configuration = str(record.get("saved_configuration") or "")
    if (
        value["schema_version"] != PREVIEW_RANGE_SCHEMA
        or str(value["joint"]) != joint
        or str(value["instance"]) not in {str(member) for member in instances}
        or str(value["configuration"]) != configuration
        or value["unit"] != "m"
        or not math.isfinite(sign)
        or value["purpose"] != PURPOSE_INSPECTION_PREVIEW
        or not (math.isfinite(lower) and math.isfinite(upper) and math.isfinite(zero))
        or not lower < upper
        # The canonical endpoints the mapping actually produces must stay finite
        # and strictly ordered: an overflowing or collapsing transform is refused.
        or not (math.isfinite(first) and math.isfinite(second) and first != second)
    ):
        findings.append(
            _finding(
                "discovery.joint_preview_range_invalid",
                obj,
                "preview declaration does not bind this joint, instance, configuration, unit, zero, "
                "direction, purpose and a finite ordered range",
                {"file": resolved["file"]},
            )
        )
        return None, None
    return {"lower": min(first, second), "upper": max(first, second)}, resolved


def _datum(record: dict, name: str | None, owners: Sequence[str]) -> dict | None:
    """The single datum of that name owned by one of ``owners``.

    Ownership compares the recorded owner exactly: an absent or non-string
    owner never aliases into a component or the frozen top assembly scope.
    """

    matches = [
        datum
        for datum in record.get("datums") or []
        if isinstance(datum, dict) and str(datum.get("name")) == str(name) and datum.get("owner") in owners
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


def _declaration_joint_name(mates: list[dict], settings: DiscoverySettings, fallback: str) -> str:
    """The canonical joint name a preview declaration must bind.

    The independent rebuild derives it from the shared ``<joint>__<role>`` mate
    head (with the frozen-name override); the derivation binds the same name so
    the record can never attach to one naming and be read under another.
    """

    names = [str(item.get("name") or "") for item in mates]
    heads: list[str] = []
    for item in names:
        head, separator, role = item.partition("__")
        if not separator or not role:
            heads = []
            break
        heads.append(head)
    name = heads[0] if heads and len(set(heads)) == 1 else fallback
    frozen = settings.frozen_names or {}
    return str(frozen.get(names[0], name)) if names else fallback


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
        limit_source = None
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
            limit_source = "native"
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
                if resolved is None:
                    continue
                try:
                    lower = float(resolved["value"]["lower"])
                    upper = float(resolved["value"]["upper"])
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
                if not (math.isfinite(lower) and math.isfinite(upper)):
                    findings.append(
                        _finding("discovery.joint_limits_invalid", obj, "controlled position limits must be finite")
                    )
                    continue
                limits = {"lower": lower, "upper": upper}
                limit_evidence = resolved
                limit_source = "controlled"
            elif settings.purpose == PURPOSE_INSPECTION_PREVIEW and joint_type == "revolute":
                radius = preview_revolute_window_rad()
                limits = {"lower": -radius, "upper": radius}
                limit_source = "policy_preview_window"
            elif settings.purpose == PURPOSE_INSPECTION_PREVIEW and joint_type == "prismatic":
                limits, limit_evidence = _declared_preview_range(
                    {"properties": properties, "saved_configuration": record.get("saved_configuration")},
                    settings,
                    findings,
                    obj,
                    joint=_declaration_joint_name(mates, settings, name),
                    instances=(str(left), str(right)),
                )
                if limits is None:
                    continue
                limit_source = "declared_preview_window"
        if joint_type != "continuous" and limits is None:
            if settings.purpose == PURPOSE_INSPECTION_PREVIEW and joint_type == "prismatic":
                findings.append(
                    _finding(
                        "discovery.joint_preview_range_missing",
                        obj,
                        "a prismatic joint with a proven-missing engineering range requires a bound finite "
                        "preview declaration",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "discovery.joint_limits_missing", obj, "no native or controlled position range for this joint"
                    )
                )
            continue
        drive = None
        drive_source = "absent"
        drive_ref = properties.get(f"{NAMESPACE}.joint.drive_record")
        if _text(drive_ref):
            drive = _resolve_record(settings, str(drive_ref), findings, obj)
            if drive is None:
                continue
            drive_source = "controlled"
        elif settings.purpose != PURPOSE_INSPECTION_PREVIEW:
            findings.append(
                _finding("discovery.joint_drive_missing", obj, "no controlled drive specification for this joint")
            )
            continue
        if drive is not None:
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
                "limit_source": limit_source,
                "drive": drive,
                "drive_source": drive_source,
                "properties": dict(properties),
            }
        )
    return joints


def _exporter_declaration(record: dict, identity: dict, findings: list[dict]) -> dict | None:
    """Bind the raw saved declaration to the same observed assembly/configuration."""

    block = record.get("exporter")
    if not isinstance(block, dict):
        findings.append(_finding("discovery.exporter_unread", "assembly", "native exporter evidence is unread"))
        return None
    state = block.get("state")
    if state == "absent":
        return None
    if state != "extracted":
        findings.append(
            _finding("discovery.exporter_unread", "assembly", "native exporter evidence is unread", block.get("error"))
        )
        return None
    xml = block.get("raw_xml")
    if not isinstance(xml, str) or hashlib.sha256(xml.encode("utf-8")).hexdigest() != block.get("raw_xml_sha256"):
        findings.append(
            _finding(
                "discovery.exporter_digest_mismatch",
                "assembly",
                "saved declaration XML digest does not match its raw text",
            )
        )
        return None
    if (
        not _text(block.get("attribute_name"))
        or block.get("document") != identity.get("main_assembly")
        or not _text(block.get("configuration"))
        or block.get("configuration") != record.get("saved_configuration")
        or block.get("configuration") != identity.get("delivery_configuration")
    ):
        findings.append(
            _finding(
                "discovery.exporter_binding_mismatch",
                "assembly",
                "saved declaration does not bind the observed main document and delivery configuration",
                {"document": block.get("document"), "configuration": block.get("configuration")},
            )
        )
        return None
    try:
        return decode_exporter_v14(xml, block.get("exporter_version"))
    except ExporterDeclarationError as exc:
        findings.append(_finding(exc.code, "assembly", str(exc), exc.detail))
        return None


def _exporter_memberships(record: dict, declaration: dict, findings: list[dict]) -> tuple[list[dict], _Clusters]:
    """Expand exact selected occurrences, retaining independently selected subtrees."""

    components: dict[str, dict] = {}
    for item in record.get("components") or []:
        name = item.get("name2") if isinstance(item, dict) else None
        if not _text(name) or name in components:
            findings.append(
                _finding(
                    "discovery.component_identity_invalid",
                    f"component:{name}",
                    "native occurrence names must be nonempty and unique",
                )
            )
            continue
        components[name] = item
    active = {
        name: item
        for name, item in components.items()
        if not item.get("suppressed")
        and not any(
            name.startswith(parent + "/") and candidate.get("suppressed") for parent, candidate in components.items()
        )
    }
    bindings: dict[str, dict] = {}
    for binding in (record.get("exporter") or {}).get("pid_bindings") or []:
        if not isinstance(binding, dict):
            findings.append(
                _finding("discovery.exporter_pid_invalid", "assembly", "a native PID binding is not an object")
            )
            continue
        pid = binding.get("pid_hex")
        name = binding.get("name2")
        item = active.get(name) if isinstance(name, str) else None
        if not isinstance(pid, str) or re.fullmatch(r"(?:[0-9a-f]{2})+", pid) is None or pid in bindings:
            findings.append(
                _finding(
                    "discovery.exporter_pid_invalid",
                    "assembly",
                    "PID bindings must use unique exact lowercase byte hex",
                    {"pid_hex": pid},
                )
            )
            continue
        bindings[pid] = binding
        if (
            type(binding.get("error_code")) is not int
            or binding["error_code"] != 0
            or item is None
            or binding.get("document") != item.get("document")
            or binding.get("configuration") != item.get("configuration")
        ):
            findings.append(
                _finding(
                    "discovery.exporter_pid_unresolved",
                    f"component:{name}",
                    "selected PID did not resolve to the exact active recorded occurrence",
                    binding,
                )
            )
    selections: dict[str, set[str]] = {}
    per_link: dict[str, list[str]] = {}
    for link in declaration["links"]:
        name = link["name"]
        selected = []
        for pid in link["component_pids"]:
            binding = bindings.get(pid)
            if binding is None:
                findings.append(
                    _finding(
                        "discovery.exporter_pid_unresolved",
                        f"body:{name}",
                        "a selected component PID has no native byte binding",
                        {"pid_hex": pid},
                    )
                )
                continue
            occurrence = binding.get("name2")
            if occurrence in active:
                selected.append(occurrence)
                selections.setdefault(occurrence, set()).add(name)
        per_link[name] = selected
        if link["incomplete"] is not False:
            findings.append(
                _finding(
                    "discovery.exporter_link_incomplete",
                    f"body:{name}",
                    "saved exporter link is incomplete or its completion state is unread",
                )
            )
    for occurrence, owners in selections.items():
        if len(owners) > 1:
            findings.append(
                _finding(
                    "discovery.exporter_membership_overlap",
                    f"component:{occurrence}",
                    "one exact selection is declared by several links",
                    {"links": sorted(owners)},
                )
            )

    def owner_of(occurrence: str) -> str | None:
        ancestors = [name for name in selections if occurrence == name or occurrence.startswith(name + "/")]
        if not ancestors:
            return None
        selected = max(ancestors, key=len)
        owners = selections[selected]
        return next(iter(owners)) if len(owners) == 1 else None

    material = {
        name for name, item in active.items() if not str(item.get("document") or "").lower().endswith(".sldasm")
    }
    assigned = {name: owner_of(name) for name in active}
    pairs = _exporter_native_pairs(record, findings)
    rigid = [
        pair
        for pair, group in pairs.items()
        if not group["unresolved"] and group["rank"] == 6 and all(endpoint in active for endpoint in pair)
    ]
    rigid_mapping = _union_find(rigid, sorted(active))
    rigid_members: dict[str, list[str]] = {}
    for occurrence, root in rigid_mapping.items():
        rigid_members.setdefault(root, []).append(occurrence)
    attachments: dict[str, dict] = {}
    for members in rigid_members.values():
        owners = {assigned[member] for member in members if assigned[member] is not None}
        if len(owners) != 1:
            if len(owners) > 1:
                findings.append(
                    _finding(
                        "discovery.exporter_rigidity_conflict",
                        "assembly",
                        "saved-pose native rigidity connects several declared links; their declared memberships "
                        "remain separate and this group cannot assign unselected occurrences",
                        {"links": sorted(owners), "components": sorted(members)},
                        blocking=False,
                    )
                )
            continue
        owner = next(iter(owners))
        proof = sorted({mate for pair in rigid if set(pair).issubset(members) for mate in pairs[pair]["mates"]})
        anchors = sorted(member for member in members if assigned[member] == owner)
        for member in members:
            if assigned[member] is None:
                assigned[member] = owner
                attachments[member] = {"link": owner, "anchors": anchors, "mates": proof, "rank": 6}
                findings.append(
                    _finding(
                        "discovery.exporter_rigid_attachment",
                        f"component:{member}",
                        "native rank-six rigidity attaches this unselected occurrence to exactly one declared link",
                        attachments[member],
                        blocking=False,
                    )
                )
    uncovered = sorted(name for name in material if assigned[name] is None)
    if uncovered:
        findings.append(
            _finding(
                "discovery.exporter_membership_uncovered",
                "assembly",
                "active material occurrences remain unassigned; capture retains their CAD geometry and mass, "
                "but complete material coverage is not qualified",
                {"components": uncovered, "qualification_blocker": "complete_material_coverage"},
                blocking=False,
            )
        )
    bodies = []
    for link in declaration["links"]:
        name = link["name"]
        members = sorted(member for member in material if assigned[member] == name)
        owners = sorted(member for member in active if assigned[member] == name)
        if not members:
            findings.append(
                _finding(
                    "discovery.exporter_reference_frame_unsupported"
                    if link["fixed_frame"]
                    else "discovery.exporter_membership_empty",
                    f"body:{name}",
                    "declared link contains no supported material body",
                    {"selections": per_link[name]},
                )
            )
        main_pid = link["main_component_pid"]
        main = bindings.get(main_pid) if main_pid is not None else None
        if main_pid is not None and (main is None or main.get("name2") not in owners):
            findings.append(
                _finding(
                    "discovery.exporter_main_component_mismatch",
                    f"body:{name}",
                    "SWMainComponentPID is unresolved or belongs to another declared link",
                    {"pid_hex": main_pid},
                )
            )
        joint = link["joint"] or {}
        datum_selector = _exporter_reference(joint.get("coordinate_system"), findings, f"body:{name}")
        datum = (
            None
            if datum_selector is None
            else _datum(record, datum_selector["feature_name"], [datum_selector["component"]])
        )
        if (
            datum_selector
            and sum(
                item.get("name") == datum_selector["feature_name"]
                for item in record.get("datums") or []
                if isinstance(item, dict)
            )
            != 1
        ):
            findings.append(
                _finding(
                    "discovery.exporter_frame_ambiguous",
                    f"body:{name}",
                    "the current capture contract requires a unique coordinate-system name across occurrence scopes",
                    datum_selector,
                )
            )
        if datum is None or _finite_vector(datum.get("array"), 16) is None:
            findings.append(
                _finding(
                    "discovery.exporter_frame_missing",
                    f"body:{name}",
                    "declared coordinate system does not bind one finite native assembly-frame datum",
                    {"coordinate_system": joint.get("coordinate_system")},
                )
            )
        if datum_selector and datum_selector["component"] and datum_selector["component"] not in owners:
            findings.append(
                _finding(
                    "discovery.exporter_frame_owner_mismatch",
                    f"body:{name}",
                    "declared coordinate system belongs to another link",
                    datum_selector,
                )
            )
        bodies.append(
            {
                "root": name,
                "name": name,
                "source": "exporter",
                "identity": f"exporter:{name}",
                "components": members,
                "owners": owners,
                "selections": per_link[name],
                "datum": datum_selector["feature_name"] if datum_selector else "",
                "datum_owner": datum_selector["component"] if datum_selector else None,
                "declaration": link,
                "rigid_attachments": {member: attachments[member] for member in members if member in attachments},
            }
        )
    clusters = _Clusters(
        members={body["name"]: body["owners"] for body in bodies},
        of={member: owner for member, owner in assigned.items() if owner is not None},
        pairs=pairs,
    )
    return bodies, clusters


def _exporter_reference(value: Any, findings: list[dict], obj: str) -> dict | None:
    """The exporter encodes nested reference scope as 'Feature <exact Name2>'."""

    if not _text(value) or value in ("Automatically Generate", "None"):
        findings.append(
            _finding(
                "discovery.exporter_reference_missing",
                obj,
                "saved declaration has no named native reference",
                {"value": value},
            )
        )
        return None
    if "<" in value or ">" in value:
        match = re.fullmatch(r"([^<>]+?) <([^<>]+)>", value)
        if match is None:
            findings.append(
                _finding(
                    "discovery.exporter_reference_invalid",
                    obj,
                    "nested native reference does not contain an exact occurrence scope",
                    {"value": value},
                )
            )
            return None
        feature, component = match.groups()
    else:
        feature, component = value, ""
    return {"kind": "reference_axis", "component": component, "feature_name": feature}


def _body_datum(record: dict, body: dict) -> dict | None:
    owners = [body["datum_owner"]] if "datum_owner" in body else body["owners"]
    return _datum(record, body["datum"], owners)


def _exporter_native_pairs(record: dict, findings: list[dict]) -> dict[tuple[str, str], dict]:
    """Retain usable saved-pose constraints without vetoing the declared tree.

    Incomplete mate evidence is diagnostic in declaration mode. It contributes
    no constraint rows and leaves its pair unresolved for membership completion.
    """

    frames = _component_frames(record, findings)
    pairs: dict[tuple[str, str], dict] = {}
    for index, mate in enumerate(record.get("mates") or []):
        if not isinstance(mate, dict) or mate.get("suppressed"):
            continue
        name = str(mate.get("name") or index)
        obj = f"mate:{name}"
        entities = _mate_entities(mate)
        endpoints = tuple(sorted(str(entity.get("component")) for entity in entities))
        detail = {
            "mate": name,
            "scope": mate.get("scope"),
            "mate_type": mate.get("type"),
            "components": list(endpoints),
        }
        if len(entities) != 2 or len(mate.get("entities") or []) != 2:
            findings.append(
                _finding(
                    "discovery.mate_entities_unsupported",
                    obj,
                    "native constraint cannot be checked because it does not have exactly two raw endpoints",
                    detail,
                    blocking=False,
                )
            )
            continue
        if any(endpoint not in frames and endpoint != "" for endpoint in endpoints):
            findings.append(
                _finding(
                    "discovery.mate_component_missing",
                    obj,
                    "native constraint refers to an unknown occurrence",
                    detail,
                    blocking=False,
                )
            )
            continue
        group = pairs.setdefault(endpoints, {"rows": [], "mates": [], "unresolved": False, "constraints": []})
        group["mates"].append(name)
        if type(mate.get("error_code")) is not int or mate["error_code"] != 0:
            findings.append(
                _finding(
                    "discovery.mate_error_state",
                    obj,
                    "saved mate reports an error or unreadable solve state",
                    {**detail, "error_code": mate.get("error_code")},
                    blocking=False,
                )
            )
            group["unresolved"] = True
            continue
        diagnostics: list[dict] = []
        kind = str(mate.get("type") or "").strip().lower()
        if kind in SUPPORTED_MATES and kind not in {"lock", "limitangle"}:
            geometry = [_entity_geometry_view(entity, diagnostics, obj) for entity in entities]
            if all(entity is not None for entity in geometry) and any(
                not any(entity.get(shape) is not None for shape in ("point", "plane", "cylinder", "circle", "line"))
                for entity in geometry
            ):
                diagnostics.append(
                    _finding(
                        "discovery.mate_entities_unsupported",
                        obj,
                        "native constraint lacks recorded geometry for both endpoints",
                    )
                )
        view = None if diagnostics else _mate_rows(mate, frames, diagnostics, obj)
        for finding in diagnostics:
            findings.append({**finding, "detail": {**detail, **(finding.get("detail") or {})}, "blocking": False})
        if view is None or any(finding["blocking"] for finding in diagnostics):
            group["unresolved"] = True
        else:
            group["rows"].extend(view["rows"])
            group["constraints"].append({**detail, "rows": view["rows"], "limits": view.get("limits")})
    for group in pairs.values():
        group["rank"] = _rank(group["rows"])
    return pairs


def _exporter_constraint_checks(
    declaration: dict, clusters: _Clusters, joints: list[dict], findings: list[dict]
) -> None:
    descendants: dict[str, set[str]] = {link["name"]: {link["name"]} for link in declaration["links"]}
    for link in reversed(declaration["links"]):
        if link["parent"] is not None:
            descendants[link["parent"]].update(descendants[link["name"]])
    for (left, right), group in clusters.pairs.items():
        owners = [declaration["root"] if component == "" else clusters.of.get(component) for component in (left, right)]
        if None in owners or owners[0] == owners[1]:
            continue
        for joint in joints:
            if joint["type"] == "fixed":
                continue
            child_tree = descendants[joint["child"]]
            if (owners[0] in child_tree) == (owners[1] in child_tree):
                continue
            direction = joint["axis"].get("direction")
            point = joint["axis"].get("point")
            if direction is None or point is None:
                continue
            twist = (
                [*direction, 0.0, 0.0, 0.0] if joint["type"] == "prismatic" else [*_cross(point, direction), *direction]
            )
            for constraint in group["constraints"]:
                raw_residuals = [_dot(row, twist) for row in constraint["rows"]]
                normalized_residuals = [
                    abs(value) / max(math.hypot(*row), 1e-12)
                    for value, row in zip(raw_residuals, constraint["rows"], strict=True)
                ]
                residual = max(normalized_residuals, default=0.0)
                detail = {
                    "joint": joint["name"],
                    "mate": constraint["mate"],
                    "scope": constraint["scope"],
                    "mate_type": constraint["mate_type"],
                    "components": [left, right],
                    "links": owners,
                }
                if residual > 1e-6:
                    findings.append(
                        _finding(
                            "discovery.exporter_saved_pose_restriction",
                            f"joint:{joint['name']}",
                            "native mate restricts the declared joint motion at the saved CAD pose; "
                            "the saved declaration remains the kinematic authority",
                            {
                                **detail,
                                "rows": constraint["rows"],
                                "declared_twist": twist,
                                "raw_residuals": raw_residuals,
                                "max_abs_residual": max((abs(value) for value in raw_residuals), default=0.0),
                                "normalized_residuals": normalized_residuals,
                                "max_normalized_residual": residual,
                                "threshold": 1e-6,
                            },
                            blocking=False,
                        )
                    )
                limits = constraint["limits"]
                if not isinstance(limits, dict):
                    continue
                expected = "m" if joint["type"] == "prismatic" else "rad"
                declared = joint.get("limits") or {}
                if limits.get("unit") != expected or any(
                    limits.get(key) != declared.get(key) for key in ("lower", "upper")
                ):
                    findings.append(
                        _finding(
                            "discovery.joint_limits_conflict",
                            f"joint:{joint['name']}",
                            "native mate position limits disagree with the saved joint declaration",
                            {**detail, "native": limits, "declared": declared},
                        )
                    )


def _exporter_axis(record: dict, declared: dict, body: dict, findings: list[dict]) -> tuple[dict, dict | None]:
    obj = f"joint:{declared['name']}"
    selector = _exporter_reference(declared.get("axis_name"), findings, obj)
    axis = {"point": None, "direction": None, "source": "exporter"}
    if selector is None:
        return axis, None
    matches = [
        item
        for item in record.get("reference_axes") or []
        if isinstance(item, dict) and item.get("selector") == selector
    ]
    if len(matches) != 1:
        findings.append(
            _finding(
                "discovery.exporter_axis_missing",
                obj,
                "declared axis does not bind exactly one native named reference axis",
                selector,
            )
        )
        return axis, selector
    native = matches[0]
    endpoints = _finite_vector(native.get("raw_endpoints_m"), 6)
    point = _finite_vector(native.get("axis_point_m"), 3)
    direction = _unit_vector(native.get("axis_direction"))
    endpoint_direction = _unit([endpoints[i + 3] - endpoints[i] for i in range(3)]) if endpoints else None
    if (
        native.get("kind") != "reference_axis"
        or native.get("component") != selector["component"]
        or native.get("direction_semantics") != "undirected_axis_line"
        or not _text(native.get("used_api"))
        or point is None
        or direction is None
        or endpoint_direction is None
        or abs(abs(_dot(direction, endpoint_direction)) - 1.0) > 1e-8
        or _axis_offset(point, endpoints[:3], endpoint_direction) > 1e-8
    ):
        findings.append(
            _finding(
                "discovery.exporter_axis_invalid",
                obj,
                "native reference-axis geometry lacks consistent endpoint evidence",
                selector,
            )
        )
        return axis, selector
    if native.get("coordinate_frame") == "component_local" and selector["component"]:
        component = next(
            (item for item in record.get("components") or [] if item.get("name2") == selector["component"]), None
        )
        transform = _finite_vector(component.get("transform"), 16) if component else None
        if transform is None:
            findings.append(
                _finding(
                    "discovery.exporter_axis_invalid",
                    obj,
                    "reference axis has no finite occurrence transform",
                    selector,
                )
            )
            return axis, selector
        frame = [transform[index : index + 4] for index in range(0, 16, 4)]
        point, direction = _point(point, frame), _unit(_vector(direction, frame))
    elif native.get("coordinate_frame") != "assembly":
        findings.append(
            _finding("discovery.exporter_axis_invalid", obj, "reference axis has an invalid coordinate frame", selector)
        )
        return axis, selector
    datum = _body_datum(record, body)
    transform = _finite_vector(datum.get("array"), 16) if datum else None
    local = _unit_vector(declared.get("axis"))
    if local is None or transform is None:
        findings.append(
            _finding(
                "discovery.exporter_axis_invalid",
                obj,
                "declared signed axis requires a unit child-frame vector and native child datum",
            )
        )
        return axis, selector
    frame = [transform[index : index + 4] for index in range(0, 16, 4)]
    signed = _unit(_vector(local, frame))
    if signed is None or abs(abs(_dot(signed, direction)) - 1.0) > 1e-8:
        findings.append(
            _finding(
                "discovery.exporter_axis_mismatch",
                obj,
                "declared child-frame axis disagrees with the native reference-axis line",
                {"declared": signed, "native": direction},
            )
        )
    origin = [transform[3], transform[7], transform[11]]
    offset = _axis_offset(point, origin, direction)
    if offset > AXIS_OFFSET_TOL_M:
        findings.append(
            _finding(
                "discovery.joint_frame_off_axis",
                obj,
                "declared child datum does not lie on the native reference axis",
                {"offset_m": offset},
            )
        )
    axis.update(point=point, direction=signed)
    return axis, selector


def _exporter_origin(declared: dict, child: dict, parent: dict, record: dict, findings: list[dict]) -> None:
    origin = declared.get("origin")
    if not isinstance(origin, dict) or origin.get("customized") is None:
        findings.append(
            _finding(
                "discovery.exporter_origin_invalid",
                f"joint:{declared['name']}",
                "saved joint origin customization state is unread",
            )
        )
        return
    if origin["customized"] is False:
        # The exporter recalculates these rounded output values from named CAD
        # coordinate systems. Fresh geometry, not this serialized cache, wins.
        return
    xyz, rpy = _finite_vector(origin.get("xyz"), 3), _finite_vector(origin.get("rpy"), 3)
    parent_datum, child_datum = _body_datum(record, parent), _body_datum(record, child)
    left = _finite_vector(parent_datum.get("array"), 16) if parent_datum else None
    right = _finite_vector(child_datum.get("array"), 16) if child_datum else None
    if xyz is None or rpy is None or left is None or right is None:
        findings.append(
            _finding(
                "discovery.exporter_origin_invalid",
                f"joint:{declared['name']}",
                "customized joint origin lacks native frames or finite xyz/rpy",
            )
        )
        return
    cr, cp, cy = [math.cos(value) for value in rpy]
    sr, sp, sy = [math.sin(value) for value in rpy]
    rotation = [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]
    relative_xyz = [
        sum(left[row * 4 + col] * (right[row * 4 + 3] - left[row * 4 + 3]) for row in range(3)) for col in range(3)
    ]
    relative_rotation = [
        [sum(left[row * 4 + col] * right[row * 4 + axis] for row in range(3)) for axis in range(3)] for col in range(3)
    ]
    if any(abs(a - b) > 1e-6 for a, b in zip(xyz, relative_xyz, strict=True)) or any(
        abs(rotation[row][col] - relative_rotation[row][col]) > 1e-6 for row in range(3) for col in range(3)
    ):
        findings.append(
            _finding(
                "discovery.exporter_origin_mismatch",
                f"joint:{declared['name']}",
                "customized joint origin contradicts the current native parent/child coordinate systems",
            )
        )


def _exporter_joint_facts(record: dict, declaration: dict, bodies: list[dict], findings: list[dict]) -> list[dict]:
    by_name = {body["name"]: body for body in bodies}
    joints = []
    for link in declaration["links"]:
        if link["parent"] is None:
            continue
        declared = link["joint"]
        obj = f"joint:{declared['name']}"
        kind = declared["type"]
        if kind not in {"fixed", "revolute", "continuous", "prismatic"}:
            findings.append(
                _finding(
                    "discovery.joint_type_unsupported",
                    obj,
                    "saved declaration has an unsupported joint type",
                    {"type": kind},
                )
            )
        if any(value is not None for values in declared["optional"].values() for value in values.values()):
            findings.append(
                _finding(
                    "discovery.exporter_joint_fields_unsupported",
                    obj,
                    "saved joint contains optional engineering declarations the current contract cannot carry",
                    declared["optional"],
                )
            )
        child, parent = by_name[link["name"]], by_name[link["parent"]]
        _exporter_origin(declared, child, parent, record, findings)
        axis, selector = ({"point": None, "direction": None, "source": "exporter"}, None)
        if kind != "fixed":
            axis, selector = _exporter_axis(record, declared, child, findings)
        limits = declared["limits"]
        if kind != "fixed":

            def valid(value):
                return type(value) in (int, float) and math.isfinite(value)

            if kind != "continuous" and (
                not isinstance(limits, dict)
                or not all(valid(limits.get(key)) for key in ("lower", "upper"))
                or limits["lower"] > limits["upper"]
            ):
                findings.append(
                    _finding(
                        "discovery.joint_limits_invalid",
                        obj,
                        "saved position limits must be finite and ordered",
                        limits,
                    )
                )
            elif kind != "continuous" and limits["lower"] == limits["upper"]:
                findings.append(
                    _finding(
                        "discovery.joint_limits_degenerate",
                        obj,
                        "equal authored position bounds are retained for capture and generation; "
                        "a usable joint range is not qualified",
                        {"limits": limits, "qualification_blocker": "usable_joint_range"},
                        blocking=False,
                    )
                )
            if not isinstance(limits, dict) or not all(
                valid(limits.get(key)) and limits[key] >= 0 for key in ("effort", "velocity")
            ):
                findings.append(
                    _finding(
                        "discovery.joint_drive_invalid",
                        obj,
                        "saved effort and velocity must be finite and nonnegative",
                        limits,
                    )
                )
            elif any(limits[key] == 0 for key in ("effort", "velocity")):
                findings.append(
                    _finding(
                        "discovery.joint_drive_unspecified",
                        obj,
                        "authored zero effort or velocity is retained for capture and generation; "
                        "a usable drive specification is not qualified",
                        {"limits": limits, "qualification_blocker": "usable_joint_drive"},
                        blocking=False,
                    )
                )
            if (
                kind == "continuous"
                and isinstance(limits, dict)
                and any(limits.get(key) is not None for key in ("lower", "upper"))
            ):
                findings.append(
                    _finding(
                        "discovery.joint_limits_unexpected", obj, "a continuous joint declares position limits", limits
                    )
                )
        declared_axis = None
        raw_axis = declared.get("axis")
        if (
            isinstance(raw_axis, (list, tuple))
            and len(raw_axis) == 3
            and all(type(value) in (int, float) and math.isfinite(value) for value in raw_axis)
        ):
            declared_axis = [float(value) for value in raw_axis]
        joints.append(
            {
                "mate": f"exporter:{declared['name']}",
                "mates": [],
                "name": declared["name"],
                "index": len(joints),
                "type": kind,
                "parent": link["parent"],
                "child": link["name"],
                "components": [link["parent"], link["name"]],
                "axis": axis,
                "declared_axis": declared_axis,
                "axis_reference": selector,
                "limits": limits,
                "limit_evidence": None,
                "drive": None,
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
    attached = clusters.frame.members if clusters.frame is not None else None
    bodies: list[dict] = []
    for root, members in sorted(clusters.members.items()):
        proven = attached is not None and frozenset(members) == attached
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
        # The frozen top assembly's own CS_base_link may name this body only when
        # the frame-attachment proof selected exactly this cluster as the base.
        ownerless = (
            [datum for datum in datums if datum.get("owner") == "" and str(datum.get("name")) == "CS_base_link"]
            if proven
            else []
        )
        if explicit is not None:
            scope = [*members, *([""] if proven and explicit == "CS_base_link" else [])]
            candidate = _datum(record, explicit, scope)
            if candidate is None:
                foreign = [
                    datum for datum in datums if str(datum.get("name")) == explicit and datum.get("owner") not in scope
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
        elif owned:
            findings.append(
                _finding(
                    "discovery.link_name_conflict",
                    f"body:{root}",
                    "several CS_<link> coordinate systems are owned by one body",
                    {"datums": sorted(str(datum["name"]) for datum in owned)},
                )
            )
            continue
        elif len(ownerless) == 1:
            datum = ownerless[0]
        elif ownerless:
            findings.append(
                _finding(
                    "discovery.link_name_conflict",
                    f"body:{root}",
                    "several CS_<link> coordinate systems are owned by one body",
                    {"datums": sorted(str(datum["name"]) for datum in ownerless)},
                )
            )
            continue
        else:
            detail: dict = {"components": members}
            observations = {}
            for member in members:
                item = components.get(member)
                if isinstance(item, dict) and isinstance(item.get("solver_observation"), dict):
                    observations[member] = item["solver_observation"]
            if observations:
                detail["solver_observations"] = observations
            findings.append(
                _finding(
                    "discovery.link_name_missing",
                    f"body:{root}",
                    "no CS_<link> coordinate system is owned by this body",
                    detail,
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
    base: dict | None,
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
    link_datums.update((body["datum_owner"], body["datum"]) for body in bodies if "datum_owner" in body)
    if base is not None and not any(
        str(datum.get("name")) == str(base.get("datum")) and str(datum.get("owner") or "") in base["owners"]
        for datum in record.get("datums") or []
        if isinstance(datum, dict)
    ):
        # The proven base's own CS_base_link sits at the frozen top assembly scope.
        link_datums.add(("", str(base.get("datum"))))
    frames: list[dict] = []
    seen: dict[str, str] = {}
    for datum in record.get("datums") or []:
        if not isinstance(datum, dict):
            continue
        name = str(datum.get("name") or "")
        if not name.startswith(INTERFACE_PREFIXES):
            continue
        owner = datum.get("owner")
        if isinstance(owner, str) and (owner, name) in link_datums:
            continue
        body = body_of_component.get(str(owner or ""))
        if body is None and base is not None and owner == "":
            # Only the proven base binds the frozen top assembly's own datums.
            body = base
        if body is None:
            findings.append(
                _finding(
                    "discovery.interface_unowned",
                    f"datum:{name}",
                    "recognised interface datum is not owned by any body",
                    {"owner": str(owner or "")},
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


def _jcs_check(
    record: dict, joints: list[dict], by_name: dict[str, dict], base: dict | None, findings: list[dict]
) -> None:
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
        owners: list[str] | None = None
        if child is not None:
            owners = [*child["owners"], *([""] if base is not None and child is base else [])]
            if datum.get("owner") not in owners:
                findings.append(
                    _finding(
                        "discovery.jcs_owner_mismatch",
                        f"datum:{name}",
                        "JCS_ datum is not owned by the child body's components",
                        {"owner": datum.get("owner"), "child": joint["child"]},
                    )
                )
                continue
        child_datum = _datum(record, child["datum"], owners) if child is not None and owners is not None else None
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


def _attached_base(clusters: _Clusters, bodies: list[dict], root: dict | None) -> dict | None:
    """The proven base body the frozen top assembly's ownerless datums may use.

    The frame attachment, the resolved ``CS_base_link`` body and the base body
    must be the same cluster: an attachment on any other rigid body never gains
    the assembly scope's ownerless datum channel.
    """

    frame = clusters.frame
    if frame is None or root is None:
        return None
    body = next((item for item in bodies if frozenset(item["owners"]) == frame.members), None)
    if body is None or root.get("root") != body.get("root") or str(body.get("datum")) != "CS_base_link":
        return None
    return body


def _frame_attachment_findings(record: dict, clusters: _Clusters, bodies: list[dict], findings: list[dict]) -> None:
    """A proven attachment must become the published ``CS_base_link`` body."""

    frame = clusters.frame
    if frame is None:
        return
    components = {str(item.get("name2")): item for item in record.get("components") or [] if isinstance(item, dict)}
    material = [
        name
        for name in sorted(frame.members)
        if not str((components.get(name) or {}).get("document") or "").lower().endswith(".sldasm")
    ]
    if not material:
        findings.append(
            _finding(
                "discovery.frame_attachment",
                "assembly",
                "the frame-attached cluster has no material body",
                {"components": sorted(frame.members)},
            )
        )
        return
    body = next((item for item in bodies if frozenset(item["owners"]) == frame.members), None)
    if body is None:
        findings.append(
            _finding(
                "discovery.frame_attachment",
                "assembly",
                "the frame-attached cluster has no published body",
                {"components": material},
            )
        )
        return
    if str(body.get("datum")) != "CS_base_link":
        findings.append(
            _finding(
                "discovery.frame_attachment",
                f"body:{body['root']}",
                "the frame-attached cluster must become the CS_base_link body",
                {"body": body.get("name"), "datum": body.get("datum")},
            )
        )


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


def _frame_checks(
    joints: list[dict], by_name: dict[str, dict], record: dict, base: dict | None, findings: list[dict]
) -> None:
    for joint in joints:
        if "child" not in joint or joint["axis"].get("source") != "mate":
            continue
        body = by_name.get(joint["child"])
        owners: list[str] | None = None
        if body is not None:
            owners = [*body["owners"], *([""] if base is not None and body is base else [])]
        datum = _datum(record, body["datum"], owners) if body is not None and owners is not None else None
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
    """Resolve an explicitly referenced design budget/measurement record.

    Exported CAD mass and bounding extents can never budget themselves.  The
    CAD may carry one scalar reference to a versioned design budget record.
    Its authored intervals are acceptance checks when provided; without a
    reference no intervals are constructed. An explicit reference that cannot
    be resolved or validated still blocks the package.
    """

    reference = _scalar(record, NAMESPACE, "design_budget_record")
    if reference is None or (isinstance(reference, str) and not reference.strip()):
        findings.append(
            _finding(
                "discovery.design_budget_unspecified",
                "assembly",
                "no design budget is specified; no optional mass or extent acceptance ranges are applied",
                blocking=False,
            )
        )
        return {}, None
    if not _text(reference):
        findings.append(
            _finding("discovery.design_budget_invalid", "assembly", "design budget reference must be a nonempty string")
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
    submission: SubmissionIdentity | None = None,
    handoff_sha256: str | None = None,
) -> tuple[dict, dict]:
    """Read authored identity, filling CAD-absent fields from the portal submission.

    Absent hardware, revision, owner, change summary, control or robot name is a
    publication-readiness gap: it is recorded as a nonblocking finding carrying
    ``detail.publication_blocker`` and the raw record keeps the absence.  When a
    submission identity is supplied, CAD-absent fields are filled from it (and
    recorded as ``discovery.identity_derived``); CAD-carried values always win
    and a differing submitted hardware identity blocks.  Malformed values block.
    Returns ``(identity, sources)`` where ``sources`` names the origin of every
    administrative field for the identity-resolution evidence file.
    """

    namespace = NAMESPACE
    sources: dict[str, str] = {}
    derived: list[str] = []

    def deferred(code: str, field: str, message: str, detail: dict | None = None) -> None:
        payload = {"publication_blocker": field}
        if detail:
            payload.update(detail)
        findings.append(_finding(code, "assembly", message, payload, blocking=False))

    def submitted(value) -> str | None:
        return str(value).strip() if _text(value) else None

    sub_hardware = submitted(submission.hardware_id) if submission is not None else None
    sub_summary = submitted(submission.change_summary) if submission is not None else None

    hardware = _identity_value(record, namespace, "hardware_id")
    hardware = str(hardware).strip() if _text(hardware) else None
    if hardware is None:
        if sub_hardware is not None:
            hardware = sub_hardware
            # Confirmed-missing CAD identity adopts the configured target (the
            # admission gate already refused an unread property set), and the
            # origin stays explicit - never recorded as a CAD value.
            sources["hardware_id"] = "platform_configuration"
            derived.append("hardware_id")
        else:
            sources["hardware_id"] = "absent"
            deferred(
                "discovery.identity_missing",
                "hardware_id",
                "hardware identity is not recorded in the CAD records and the submission selected no "
                "configured target; publication readiness requires it",
            )
    else:
        sources["hardware_id"] = "cad"
        if sub_hardware is not None and sub_hardware != hardware:
            findings.append(
                _finding(
                    "discovery.identity_conflict",
                    "assembly",
                    "the submitted hardware identity differs from the CAD-carried identity; publication stays blocked",
                    {"cad": hardware, "submission": sub_hardware},
                )
            )
    if hardware is not None and _IDENTITY_ID.fullmatch(hardware) is None:
        findings.append(
            _finding(
                "discovery.identity_invalid",
                "assembly",
                "recorded hardware identity must be an ASCII identifier of at most 64 characters",
                {"value": hardware},
            )
        )
    revision = _identity_value(record, namespace, "revision")
    revision = str(revision).strip() if _text(revision) else None
    if revision is not None:
        sources["revision"] = "cad"
    else:
        # An absent engineering revision stays absent: it is never derived from a
        # date, a digest, the authenticated submitter or any platform value.
        sources["revision"] = "absent"
        deferred(
            "discovery.revision_missing",
            "revision",
            "structural revision is not recorded in the CAD records; it stays missing and the review "
            "shows the gap",
        )
    if revision is not None and _IDENTITY_ID.fullmatch(revision) is None:
        findings.append(
            _finding(
                "discovery.revision_invalid",
                "assembly",
                "recorded structural revision must be an ASCII identifier of at most 64 characters",
                {"value": revision},
            )
        )
    owner = _identity_value(record, namespace, "owner")
    owner = str(owner).strip() if _text(owner) else None
    if owner is not None:
        sources["owner"] = "cad"
    else:
        # The authenticated submitter is never substituted for an engineering owner.
        sources["owner"] = "absent"
        deferred(
            "discovery.owner_missing",
            "owner",
            "revision owner is not recorded in the CAD records; it stays missing and the review shows "
            "the gap",
        )
    if owner is not None and not _authored_text(owner):
        findings.append(
            _finding(
                "discovery.owner_invalid",
                "assembly",
                "recorded revision owner must be non-empty text without control characters",
                {"value": owner},
            )
        )
    summary = _identity_value(record, namespace, "change_summary")
    summary = str(summary).strip() if _text(summary) else None
    if summary is not None:
        sources["change_summary"] = "cad"
        # One line-ending standard with the independent read: a CAD-authored
        # summary folds CRLF and a lone CR to LF before it is validated and
        # recorded, while every other control character still refuses.  The
        # submission path stays LF-only (the portal already normalizes it).
        summary = summary.replace("\r\n", "\n").replace("\r", "\n").strip()
    elif sub_summary is not None:
        summary = sub_summary
        sources["change_summary"] = "submission"
        derived.append("change_summary")
    else:
        sources["change_summary"] = "absent"
        deferred(
            "discovery.summary_missing",
            "change_summary",
            "change summary is not recorded in the CAD records and the submission carries none; "
            "publication readiness requires it",
        )
    if summary is not None and not _authored_multiline_text(summary):
        findings.append(
            _finding(
                "discovery.summary_invalid",
                "assembly",
                "recorded change summary must be non-empty text without control characters other than line breaks",
                {"value": summary},
            )
        )
    control = (record.get("identity") or {}).get("control")
    if not isinstance(control, dict):
        control = {
            "system": _scalar(record, namespace, "control.system"),
            "reference": _scalar(record, namespace, "control.reference"),
        }
    system = str(control["system"]).strip() if _text(control.get("system")) else None
    reference = str(control["reference"]).strip() if _text(control.get("reference")) else None
    if system is None or reference is None:
        # A platform handoff reference is never substituted for a CAD control record.
        sources["control"] = "absent"
        deferred(
            "discovery.control_missing",
            "control",
            "CAD control system/reference are not recorded; they stay missing and the review shows "
            "the gap",
        )
        control = None
    elif system not in CONTROL_SYSTEMS:
        sources["control"] = "cad"
        findings.append(
            _finding(
                "discovery.control_invalid",
                "assembly",
                "CAD control system must be git, pdm or handoff",
                {"system": system},
            )
        )
        control = None
    elif system == "git" and _GIT_COMMIT.search(reference) is None:
        sources["control"] = "cad"
        findings.append(
            _finding(
                "discovery.control_invalid",
                "assembly",
                "Git CAD control reference must contain the full commit hash",
                {"reference": reference},
            )
        )
        control = None
    else:
        control = {"system": system, "reference": reference}
        sources["control"] = "cad"
    delivery = (
        configuration
        or _identity_value(record, namespace, "delivery_configuration")
        or record.get("saved_configuration")
    )
    if not _text(delivery):
        findings.append(
            _finding("discovery.configuration_missing", "assembly", "no saved delivery configuration is recorded")
        )
    delivery = str(delivery).strip() if _text(delivery) else None
    if delivery is not None and not _saved_configuration_observed(record, delivery):
        findings.append(
            _finding(
                "discovery.configuration_unproven",
                "assembly",
                "the delivery configuration and its read-only rebuild state are not proven "
                "by the native before/after readings",
                {"configuration": delivery, "document_state": record.get("document_state")},
            )
        )
        delivery = None
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
    authored_name = _identity_value(record, namespace, "robot_name")
    if _text(authored_name):
        robot_name = str(authored_name).strip()
        if _SNAKE.fullmatch(robot_name) is None:
            findings.append(
                _finding(
                    "discovery.robot_name_invalid", "assembly", "robot name is not snake_case", {"name": robot_name}
                )
            )
        technical_name = None
    else:
        technical = _snake(hardware) if hardware else None
        technical_name = technical if technical and _SNAKE.fullmatch(technical) else "robot"
        robot_name = None
        findings.append(
            _finding(
                "discovery.robot_name_missing",
                "assembly",
                f"no authored robot name is recorded; the technical label {technical_name!r} is used for "
                "generation and the native identity stays null",
                {"technical_name": technical_name, "authored": False},
                blocking=False,
            )
        )
    parent = _identity_value(record, namespace, "parent_revision")
    if _text(parent):
        parent_value = str(parent).strip()
        sources["parent_revision"] = "cad"
    else:
        # An administrative parent linkage is never an engineering parent revision.
        parent_value = None
        sources["parent_revision"] = "absent"
    if derived:
        findings.append(
            _finding(
                "discovery.identity_derived",
                "assembly",
                "administrative identity fields were taken from the configured target or the "
                "submission; CAD-carried values win",
                {"fields": sorted(derived)},
                blocking=False,
            )
        )
    return (
        {
            "hardware_id": hardware,
            "revision": revision,
            "parent_revision": parent_value,
            "owner": owner,
            "change_summary": summary,
            "control": control,
            "delivery_configuration": delivery,
            "main_assembly": main,
            "robot_name": robot_name,
            "robot_name_technical": technical_name,
        },
        sources,
    )


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
        if existing["file"] == relative:
            if existing["source_sha256"] != resolved["sha256"]:
                raise PipelineError(f"controlled record {relative!r} changed between references")
            if existing["key"] == str(resolved.get("key") or ""):
                return existing
    source = resolved.get("path")
    if not source:
        raise PipelineError(f"controlled record {relative!r} was not resolved from a record root")
    stem = re.sub(r"[^0-9A-Za-z._-]", "_", PurePosixPath(relative).stem)[:48] or "record"
    target_name = f"{RECORD_DIR}/{str(resolved['sha256'])[:8]}_{stem}.txt"
    target = confined(output, target_name, exists=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(resolved["content"])
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    if checksum != resolved["sha256"]:
        raise PipelineError(f"embedded controlled record {relative!r} differs from its captured bytes")
    entry = {
        "reference": str(resolved.get("reference") or relative),
        "file": relative,
        "key": str(resolved.get("key") or ""),
        "package_file": target_name,
        "sha256": checksum,
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


def _guarded_sink(on_activity):
    """The observer guard: activity callbacks can never alter or fail the capture."""

    if on_activity is None:
        return None

    def guarded(record):
        try:
            on_activity(record)
        except Exception:  # noqa: BLE001 - an observer can never fail the capture
            return

    return guarded


def _check_derivation_paths(frozen_source: Path, output: Path) -> None:
    if frozen_source.is_symlink() or frozen_source.is_junction() or not frozen_source.is_dir():
        raise PipelineError("frozen_source must be an existing native engineering directory, not a link")
    if output.exists() and any(output.iterdir()):
        raise PipelineError("prepared output directory must be empty")


def _check_authored_files(native_files: Mapping[str, str]) -> None:
    authored = sorted(
        name for name in native_files if name == SOURCE_FILE or name.startswith("discovery/")
    )
    if authored:
        raise PipelineError(f"frozen_source already contains authored package files: {authored[:5]}")


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
    on_activity=None,
) -> PreparedPackage:
    """Read one immutable engineering directory natively, then derive purely.

    The native-reading adapter is the only step that needs Windows/COM; the
    derivation below is pure and can run wherever the raw record is available.
    ``on_activity`` observes the native read and can never change the derived
    record, the returned package or any failure path.
    """

    settings = settings or DiscoverySettings()
    _check_derivation_paths(Path(frozen_source), Path(output))
    _check_authored_files(package_inventory(Path(frozen_source)))
    record = read_native_record(frozen_source, settings, backend=backend, on_activity=on_activity)
    return derive_native_package(
        frozen_source,
        output,
        run_id,
        record,
        settings=settings,
        configuration=configuration,
        assembly=assembly,
        on_event=on_event,
    )


def read_native_record(frozen_source: Path, settings: DiscoverySettings, *, backend=None, on_activity=None) -> dict:
    """Native-reading adapter: the only step that needs Windows/COM and a CAD session."""

    if backend is None:
        from .native import SolidWorksBackend
        from ...runtime import native_readiness

        native_readiness()
        backend = SolidWorksBackend()
    native_settings = {"namespace": NAMESPACE, "contract": CONTRACT}
    if settings.main_assembly:
        native_settings["main_assembly"] = settings.main_assembly
    record = backend.discover_native(Path(frozen_source), native_settings, on_activity=_guarded_sink(on_activity))
    if not isinstance(record, dict) or record.get("schema_version") != DISCOVERY_SCHEMA:
        raise PipelineError("native discovery backend returned an unexpected record schema")
    return record


def sealed_embedded_virtual_document(
    component: dict, name: str, native_files: Mapping[str, str]
) -> bool:
    """Whether the sealed record proves an embedded virtual session document."""

    provenance = component.get("source_provenance")
    if not isinstance(provenance, dict):
        return False
    if (
        provenance.get("kind") != "native_embedded_virtual_component"
        or provenance.get("state") != "embedded_virtual_component"
        or provenance.get("is_virtual") is not True
        or provenance.get("occurrence") != name
        or not isinstance(provenance.get("owner"), str)
        or not provenance["owner"]
        or not isinstance(provenance.get("owner_sha256"), str)
        or _SHA256_HEX.fullmatch(provenance["owner_sha256"]) is None
        or native_files.get(provenance["owner"]) != provenance["owner_sha256"]
        or not isinstance(provenance.get("session_document_path"), str)
        or not provenance["session_document_path"]
        or not isinstance(provenance.get("session_temp_root"), str)
        or not provenance["session_temp_root"]
        or type(provenance.get("native_api_pid")) is not int
        or provenance["native_api_pid"] <= 0
        or not isinstance(provenance.get("used_api"), str)
        or provenance["used_api"] != "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual"
        or provenance.get("author_imported_path") is not None
        or provenance.get("author_imported_error") is not None
        or provenance.get("document") is not None
        or not isinstance(component.get("configuration"), str)
        or not component["configuration"]
    ):
        return False
    session_path = provenance["session_document_path"].replace("\\", "/")
    session_root = provenance["session_temp_root"].replace("\\", "/").rstrip("/")
    root_name = session_root.rsplit("/", 1)[-1]
    if not re.fullmatch(r"swx[0-9]+", root_name):
        return False
    if int(root_name[3:]) != provenance["native_api_pid"]:
        return False
    if not session_path.casefold().startswith(session_root.casefold() + "/"):
        return False
    if session_path.casefold().count("/vc~~/") != 1:
        return False
    for key in ("native_document_path", "materialization_path"):
        value = provenance.get(key)
        if (
            not isinstance(value, str)
            or value.replace("\\", "/").casefold() != session_path.casefold()
        ):
            return False
    document = component.get("document")
    return (
        isinstance(document, str)
        and bool(document)
        and document.replace("\\", "/").casefold() == session_path.casefold()
    )


def sealed_cached_import_materialization(
    component: object,
    name: str,
    native_files: Mapping[str, str],
    source_selection: object,
) -> bool:
    """Whether raw and sealed closure prove a portable IC~~ import occurrence."""

    if not isinstance(component, dict):
        return False
    provenance = component.get("source_provenance")
    if not isinstance(provenance, dict) or (
        provenance.get("state") != "cached_materialization"
        or provenance.get("kind") != "native_import_materialization"
        or provenance.get("is_virtual") is not False
        or provenance.get("occurrence") != name
        or not isinstance(provenance.get("owner"), str)
        or not provenance["owner"]
        or not isinstance(provenance.get("owner_sha256"), str)
        or _SHA256_HEX.fullmatch(provenance["owner_sha256"]) is None
        or native_files.get(provenance["owner"]) != provenance["owner_sha256"]
        or not isinstance(provenance.get("materialization_path"), str)
        or not provenance["materialization_path"]
        or not isinstance(provenance.get("session_temp_root"), str)
        or not provenance["session_temp_root"]
        or type(provenance.get("native_api_pid")) is not int
        or provenance["native_api_pid"] <= 0
        or provenance.get("used_api") != "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual"
        or provenance.get("error") is not None
        or "author_imported_error" not in provenance
        or provenance.get("author_imported_error") is not None
        or not isinstance(component.get("configuration"), str)
        or not component["configuration"]
        or type(component.get("suppression_state")) is not int
        or component.get("suppression_state") not in {0, 1, 2, 3, 4}
    ):
        return False
    session_path = provenance["materialization_path"].replace("\\", "/")
    session_root = provenance["session_temp_root"].replace("\\", "/").rstrip("/")
    root_name = session_root.rsplit("/", 1)[-1]
    if (
        not re.fullmatch(r"swx[0-9]+", root_name)
        or int(root_name[3:]) != provenance["native_api_pid"]
        or not session_path.casefold().startswith(session_root.casefold() + "/")
        or session_path.casefold().count("/ic~~/") != 1
    ):
        return False
    for key in ("session_document_path", "native_document_path"):
        value = provenance.get(key)
        if (
            not isinstance(value, str)
            or value.replace("\\", "/").casefold() != session_path.casefold()
        ):
            return False
    document = component.get("document")
    if (
        not isinstance(document, str)
        or not document
        or document.replace("\\", "/").casefold() != session_path.casefold()
        or provenance.get("author_imported_in_source_root") is not True
        or not _portable_relative(provenance.get("author_imported_relative"))
        or not isinstance(provenance.get("author_imported_path"), str)
        or not isinstance(provenance.get("source_root"), str)
    ):
        return False
    author_relative = _relative_under_windows(
        provenance["author_imported_path"],
        provenance["source_root"],
    )
    if (
        author_relative is None
        or author_relative.casefold() != provenance["author_imported_relative"].casefold()
    ):
        return False
    if not isinstance(source_selection, dict):
        return False
    files = source_selection.get("files")
    closure = source_selection.get("closure")
    if not isinstance(files, dict) or not isinstance(closure, dict):
        return False
    imports = closure.get("imports")
    matches = [
        entry
        for entry in (imports or [])
        if isinstance(entry, dict)
        and entry.get("instance") == name
        and entry.get("state") == "cached_materialization"
    ]
    if len(matches) != 1:
        return False
    entry = matches[0]
    bound_source = entry.get("bound_source")
    stage_root = closure.get("stage_root")
    slot_author_relative = _relative_under_windows(entry.get("author_imported_path"), stage_root)
    if (
        entry.get("binding") != "author_metadata_only"
        or entry.get("is_virtual") is not False
        or str(entry.get("owner") or "").replace("\\", "/").casefold()
        != provenance["owner"].replace("\\", "/").casefold()
        or entry.get("owner_sha256") != provenance["owner_sha256"]
        or (entry.get("owner_anchor") or None) != (provenance.get("defining_anchor") or None)
        or entry.get("configuration") != component["configuration"]
        or type(entry.get("suppression_state")) is not int
        or entry.get("suppression_state") != component.get("suppression_state")
        or entry.get("author_metadata_in_stage") is not True
        or not _portable_relative(bound_source)
        or bound_source not in files
        or bound_source not in native_files
        or files.get(bound_source) != native_files.get(bound_source)
        or str(bound_source).replace("\\", "/").casefold()
        != provenance["author_imported_relative"].replace("\\", "/").casefold()
        or not isinstance(stage_root, str)
        or slot_author_relative is None
        or slot_author_relative.casefold() != bound_source.casefold()
    ):
        return False
    slot_root = entry.get("session_temp_root")
    slot_pid = entry.get("native_api_pid")
    slot_path = entry.get("materialization")
    if (
        type(slot_pid) is not int
        or slot_pid <= 0
        or not isinstance(slot_root, str)
        or not isinstance(slot_path, str)
        or session_root_pid(slot_root) != slot_pid
        or not session_materialization_under_root(slot_path, slot_root)
    ):
        return False
    graph = closure.get("graph")
    documents = closure.get("documents")
    if not isinstance(graph, dict) or not isinstance(documents, dict):
        return False
    owner = provenance["owner"]
    owner_graph = graph.get(owner)
    source_graph = graph.get(bound_source)
    owner_document = documents.get(owner)
    if (
        not isinstance(owner_graph, dict)
        or owner_graph.get("sha256") != provenance["owner_sha256"]
        or bound_source not in (owner_graph.get("references") or [])
        or not isinstance(source_graph, dict)
        or source_graph.get("sha256") != files[bound_source]
        or not isinstance(owner_document, dict)
    ):
        return False
    resolved = [
        edge
        for edge in owner_document.get("resolved_edges") or []
        if isinstance(edge, dict)
        and edge.get("kind") == "native_imported_document"
        and edge.get("instance") == name
        and edge.get("resolved") == bound_source
        and edge.get("configuration") == component["configuration"]
        and type(edge.get("suppression_state")) is int
        and edge.get("suppression_state") == component.get("suppression_state")
    ]
    return len(resolved) == 1


def _portable_relative(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return "/".join(artifact_path_parts(value)) == value
    except PipelineError:
        return False


def _relative_under_windows(path: object, root: object) -> str | None:
    if not isinstance(path, str) or not isinstance(root, str) or not path or not root:
        return None
    normalized = ntpath.normcase(ntpath.normpath(path))
    base = ntpath.normcase(ntpath.normpath(root))
    if not ntpath.isabs(normalized) or not ntpath.isabs(base):
        return None
    try:
        relative = ntpath.relpath(normalized, base)
    except ValueError:
        return None
    if relative == ".." or relative.startswith(".." + ntpath.sep) or ntpath.isabs(relative):
        return None
    return relative.replace("\\", "/")


def _capture_plan_selector(
    component: dict,
    native_files: Mapping[str, str],
    source_selection: object = None,
) -> dict | None:
    """Build one portable native selector for the sealed capture plan.

    Fully sealed embedded-virtual and cached-import occurrences keep their
    absolute session path in the raw diagnostic record only.  Their occurrence
    name and suppression state are the portable selector; consumer-side closure
    binding proves that an IC~~ occurrence can be re-materialized from a bound
    staged source.
    A delivered document must remain a portable staged path, so any other
    non-portable value is refused instead of being silently removed.
    """

    if not isinstance(component, dict) or component.get("suppressed"):
        return None
    name = component.get("instance_id") or component.get("name2")
    if not isinstance(name, str) or not name:
        return None
    selector = {"name": name}
    document = component.get("document")
    session_document = sealed_embedded_virtual_document(
        component, name, native_files
    ) or sealed_cached_import_materialization(
        component,
        name,
        native_files,
        source_selection,
    )
    if not session_document and (not isinstance(document, str) or not document):
        raise PipelineError("A non-virtual native component carries no document selector")
    if isinstance(document, str) and document and not session_document:
        normalized = document.replace("\\", "/")
        try:
            artifact_path_parts(normalized)
        except PipelineError as error:
            raise PipelineError(
                "A non-session native component carries a document outside the portable staged inventory"
            ) from error
        selector["document"] = normalized
    state = component.get("suppression_state")
    if type(state) is not int or state not in {0, 1, 2, 3, 4}:
        raise PipelineError("A native capture selector carries no truthful suppression state")
    selector["state"] = state
    return selector


def derive_native_package(
    frozen_source: Path,
    output: Path,
    run_id: str,
    record: dict,
    *,
    settings: DiscoverySettings | None = None,
    configuration: str | None = None,
    assembly: str | None = None,
    submission_identity: SubmissionIdentity | None = None,
    on_event=None,
) -> PreparedPackage:
    """Derive a prepared package purely from a raw native record.

    No backend, COM or session import happens here: the record is validated,
    its file bindings are proven against the frozen bytes, and the robot
    document, revision and findings follow from that evidence.  A submission
    identity fills CAD-absent administrative fields (CAD-carried values win and
    a conflicting hardware identity blocks); its resolution is recorded in
    ``discovery/identity-resolution.json``.
    """

    settings = settings or DiscoverySettings()
    _check_derivation_paths(Path(frozen_source), Path(output))
    if not isinstance(record, dict) or record.get("schema_version") != DISCOVERY_SCHEMA:
        raise PipelineError("native discovery record has an unexpected schema")
    frozen_source = Path(frozen_source)
    output = Path(output)
    native_files = package_inventory(frozen_source)
    handoff_sha256 = digest(native_files)
    _check_authored_files(native_files)
    _validate_native_record(record)
    record_files = record.get("files")
    if not isinstance(record_files, dict) or not record_files:
        raise PipelineError(
            "native discovery record carries no bound file inventory; the frozen bytes cannot be tied to the record"
        )
    for name, recorded_sha in record_files.items():
        if not isinstance(recorded_sha, str) or native_files.get(str(name)) != recorded_sha:
            raise PipelineError(f"native discovery record file binding does not match the frozen directory: {name!r}")
    identity_block = record.get("identity") if isinstance(record.get("identity"), dict) else {}
    recorded_main = identity_block.get("main_assembly")
    if not _text(recorded_main):
        recorded_main = _identity_value(record, NAMESPACE, "main_assembly")
    recorded_main = str(recorded_main).replace("\\", "/") if _text(recorded_main) else None
    if recorded_main is None or recorded_main not in record_files:
        raise PipelineError("the recorded main assembly is not bound by the record file inventory")
    if settings.main_assembly and recorded_main != settings.main_assembly:
        raise PipelineError(
            "Native discovery did not open the selected main assembly "
            f"({recorded_main!r} != {settings.main_assembly!r})"
        )
    declared_configuration = _identity_value(record, NAMESPACE, "delivery_configuration")
    saved_configuration = record.get("saved_configuration")
    if (
        _text(declared_configuration)
        and _text(saved_configuration)
        and str(declared_configuration) != str(saved_configuration)
    ):
        raise PipelineError("the recorded delivery configuration disagrees with the saved assembly configuration")
    recorded_configuration = saved_configuration if _text(saved_configuration) else declared_configuration
    if configuration is not None:
        if not _text(recorded_configuration):
            raise PipelineError("an explicit configuration cannot be bound: the record carries no usable configuration")
        if str(configuration) != str(recorded_configuration):
            raise PipelineError("the requested configuration does not match the recorded delivery configuration")
    source_selection = record.get("source_selection") if isinstance(record.get("source_selection"), dict) else {}
    upload_digest = source_selection.get("upload_sha256")
    upload_handoff_digest = (
        upload_digest
        if _SHA256_HEX.fullmatch(str(upload_digest or ""))
        else None
    )
    findings: list[dict] = []
    components = record.get("components")
    if not isinstance(components, list):
        findings.append(
            _finding(
                "discovery.suppression_state_invalid",
                "assembly",
                "the native record carries no component list to classify",
                {"components": type(components).__name__},
            )
        )
    for item in components if isinstance(components, list) else []:
        if not isinstance(item, dict):
            findings.append(
                _finding(
                    "discovery.suppression_state_invalid",
                    "assembly",
                    "a raw component entry is not an object",
                    {"entry": repr(item)[:200]},
                )
            )
            continue
        name = str(item.get("name2") or "?")
        state = item.get("suppression_state")
        if type(state) is not int or state not in {0, 1, 2, 3, 4}:
            findings.append(
                _finding(
                    "discovery.suppression_state_invalid",
                    f"component:{name}",
                    "every component must carry the documented raw suppression state "
                    "(integer 0..4); state 5 and missing, boolean or undocumented values are refused",
                    {"suppression_state": state},
                )
            )
            continue
        suppressed = item.get("suppressed")
        lightweight = item.get("lightweight")
        if type(suppressed) is not bool or type(lightweight) is not bool:
            findings.append(
                _finding(
                    "discovery.suppression_state_invalid",
                    f"component:{name}",
                    "every component must carry boolean suppressed and lightweight flags",
                    {"suppressed": suppressed, "lightweight": lightweight},
                )
            )
            continue
        if suppressed != (state == 0) or lightweight != (state in {1, 4}):
            findings.append(
                _finding(
                    "discovery.suppression_state_incoherent",
                    f"component:{name}",
                    "the emitted suppression flags must agree with the raw suppression state",
                    {
                        "suppression_state": state,
                        "suppressed": suppressed,
                        "lightweight": lightweight,
                    },
                )
            )
    known_occurrences = {
        str(item.get("name2"))
        for item in record.get("components") or []
        if isinstance(item, dict) and _text(item.get("name2"))
    }
    datum_ids: set[tuple[str, str]] = set()
    for datum in record.get("datums") or []:
        if not isinstance(datum, dict):
            continue
        name = str(datum.get("name") or "")
        owner = datum.get("owner")
        if not isinstance(owner, str) or (owner and owner not in known_occurrences):
            findings.append(
                _finding(
                    "discovery.datum_owner_invalid",
                    f"datum:{name}",
                    "a datum owner must be a known occurrence name or the frozen top assembly",
                    {"owner": owner},
                )
            )
        key = (str(datum.get("owner") or ""), name)
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
    identity, identity_sources = _identity(
        record,
        settings,
        findings,
        configuration=configuration,
        assembly=assembly,
        native_files=native_files,
        submission=submission_identity,
        handoff_sha256=handoff_sha256,
    )
    if submission_identity is not None:
        write_json(
            output / IDENTITY_FILE,
            {
                "schema_version": IDENTITY_SCHEMA,
                "run_id": run_id,
                "handoff_sha256": handoff_sha256,
                "sources": identity_sources,
                "hardware_id": identity["hardware_id"],
                "revision": identity["revision"],
                "parent_revision": identity["parent_revision"],
            },
        )
    checks, budget_entry = _budgets(record, settings, findings)
    bodies, joints, frames, derived_names = _derive_carriers(record, identity, settings, findings)
    blocking = [item for item in findings if item["blocking"]]
    output.mkdir(parents=True, exist_ok=True)
    discovery_path = output / DISCOVERY_FILE
    discovery_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": DISCOVERY_SCHEMA,
        "contract": CONTRACT,
        "namespace": NAMESPACE,
        "run_id": run_id,
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "handoff_sha256": handoff_sha256,
        "native_files": native_files,
        "derivation": {
            "frozen_names": dict(settings.frozen_names),
            "record_roots": [str(root) for root in settings.record_roots],
            "main_assembly": recorded_main,
            "configuration": configuration,
        },
        "identity": identity,
        "records": [],
        "frozen_names": derived_names,
        "raw": record,
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
            hardware_id=identity["hardware_id"],
            revision=identity["revision"],
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
    # A partial content identity cannot name the frozen revision.  The prepared
    # package still proceeds so measurement and inspection continue; publication
    # readiness stays deferred with the nonblocking findings above.
    # The minimal pre-acquisition plan: selectors, selection, datums and the
    # requested evidence items, bound to the sealed discovery bytes.  It is the
    # only plan traveling to Windows; the full engineering representation is
    # never copied into it.
    from .selection import selection_bindings

    source_selection = record.get("source_selection")
    selection = selection_bindings(source_selection)
    if selection["files"] != native_files or selection["main_assembly"] != recorded_main:
        raise PipelineError("The sealed source selection differs from the frozen native package")
    plan_objects = []
    for component in record.get("components") or ():
        selector = _capture_plan_selector(component, native_files, source_selection)
        if selector is not None:
            plan_objects.append(selector)
    plan_objects.sort(key=lambda item: item["name"])
    write_json(
        output / PLAN_FILE,
        {
            "schema_version": PLAN_SCHEMA,
            "run_id": str(run_id),
            "handoff_sha256": handoff_sha256,
            "discovery_sha256": discovery_sha256,
            "main_assembly": recorded_main,
            "configuration": recorded_configuration,
            "identity": {"hardware_id": identity.get("hardware_id"), "revision": identity.get("revision")},
            "selection": {"files": {str(key): str(value) for key, value in sorted(native_files.items())}},
            "objects": plan_objects,
            "groups": [
                {
                    "name": str(body["name"]),
                    "components": sorted(str(member) for member in (body.get("components") or [])),
                }
                for body in bodies
            ],
            "axis_requests": [
                {"name": str(joint["name"]), "axis_reference": joint["axis_reference"]}
                for joint in joints
                if isinstance(joint.get("axis_reference"), dict)
            ],
            "datums": sorted(
                {str(item["datum"]) for item in bodies if item.get("datum")}
                | {
                    str(item["coordinate_system"])
                    for item in frames
                    if isinstance(item, dict) and item.get("coordinate_system")
                }
            ),
            "evidence_requests": [
                "evidence/capture.json",
                "evidence/geometry/",
                "evidence/native-runtime.json",
            ],
        },
    )
    source_record = _source_record(identity, identity_sources, upload_handoff_digest, records)
    if source_record is not None:
        write_json(output / SOURCE_FILE, source_record)
    return PreparedPackage(
        schema_version=PREPARED_SCHEMA,
        run_id=run_id,
        package=output,
        handoff_sha256=handoff_sha256,
        prepared_sha256=digest(inventory(output)),
        hardware_id=identity["hardware_id"],
        revision=identity["revision"],
        discovery_path=discovery_path,
        discovery_sha256=discovery_sha256,
        findings=tuple(findings),
        passed=True,
    )


def _derive_carriers(
    record: dict, identity: dict, settings: DiscoverySettings, findings: list[dict]
) -> tuple[list, list, list, dict]:
    """Derive the in-memory source carriers: bodies, joints, frames and frozen names.

    Shared by the discover stage and the capture-side definition rebuild; the
    carriers are always derived from the raw record alone, never read from a
    sealed definition.
    """

    if exporter_declaration_present(record):
        declaration = _exporter_declaration(record, identity, findings)
        bodies, joints, derived_names = [], [], {}
        frames: list = []
        if declaration is not None:
            bodies, clusters = _exporter_memberships(record, declaration, findings)
            joints = _exporter_joint_facts(record, declaration, bodies, findings)
            root = next(body for body in bodies if body["name"] == declaration["root"])
            if root["name"] != "base_link":
                findings.append(
                    _finding(
                        "discovery.root_name_missing",
                        f"body:{root['name']}",
                        "the declared root link must be named base_link",
                    )
                )
            _exporter_constraint_checks(declaration, clusters, joints, findings)
            frames = _interface_frames(record, clusters, bodies, root, findings)
            for body in bodies:
                if settings.frozen_names.get(body["identity"], body["name"]) != body["name"]:
                    findings.append(
                        _finding(
                            "discovery.name_frozen_mismatch",
                            f"body:{body['name']}",
                            "saved declaration differs from the frozen published name",
                        )
                    )
        return bodies, joints, frames, derived_names
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
    base = _attached_base(clusters, bodies, root)
    _frame_attachment_findings(record, clusters, bodies, findings)
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
    _frame_checks(joints, by_name, record, base, findings)
    frames = _interface_frames(record, clusters, bodies, base, findings)
    _jcs_check(record, joints, by_name, base, findings)
    return bodies, joints, frames, derived_names


def source_definition(
    package: Path,
    *,
    discovery_name: str = DISCOVERY_FILE,
    cad_prefix: str = "",
    records_prefix: str = "",
) -> dict:
    """Rebuild one prepared package's in-memory source definition (the freeze/scene ``source``).

    The frozen raw record, the recorded derivation settings and the embedded
    controlled records are the only inputs: nothing is read from a sealed
    definition and nothing is written.  A rebuild whose findings block would
    have blocked discovery, so the same rules failing here refuse the capture.
    """

    import tempfile

    package = Path(package)
    payload = read_data(package / discovery_name)
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
    derivation = payload.get("derivation") if isinstance(payload.get("derivation"), dict) else {}
    records = [item for item in payload.get("records") or [] if isinstance(item, dict)]
    with tempfile.TemporaryDirectory(prefix="source-definition-") as temporary:
        root = Path(temporary)
        for entry in records:
            relative = str(entry.get("file") or "").replace("\\", "/")
            if not relative:
                continue
            target = confined(root, relative, exists=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(confined(package, records_prefix + str(entry.get("package_file") or "")), target)
        request_record, _request_digest = read_request_evidence(package / "input" / "request.json")
        settings = DiscoverySettings(
            record_roots=(root,),
            frozen_names=dict(derivation.get("frozen_names") or {}),
            main_assembly=derivation.get("main_assembly") or None,
            purpose=str(request_record["purpose"]),
        )
        findings: list[dict] = []
        view = {
            "main_assembly": identity.get("main_assembly") or derivation.get("main_assembly"),
            "delivery_configuration": identity.get("delivery_configuration") or derivation.get("configuration"),
        }
        checks, _budget_entry = _budgets(raw, settings, findings)
        bodies, joints, frames, _names = _derive_carriers(raw, view, settings, findings)
    blocking = [item for item in findings if item.get("blocking")]
    if blocking:
        raise PipelineError(
            f"the frozen discovery carriers cannot be rebuilt: {[item['code'] for item in blocking][:5]}"
        )
    discovery_sha256 = hashlib.sha256((package / discovery_name).read_bytes()).hexdigest()
    by_name = {body["name"]: body for body in bodies}
    joints_out: list[dict] = []
    for joint in sorted(joints, key=lambda item: item.get("name") or ""):
        if "parent" not in joint or "name" not in joint:
            continue
        body = by_name[joint["child"]]
        datum = _body_datum(raw, body)
        if joint["type"] == "fixed":
            joints_out.append(
                {
                    "id": joint["name"],
                    "name": joint["name"],
                    "type": "fixed",
                    "parent": joint["parent"],
                    "child": joint["child"],
                }
            )
            continue
        axis = _axis_in_child(joint, datum) if datum is not None else None
        if axis is None:
            raise PipelineError(f"joint {joint['name']!r} has no usable child frame")
        if joint.get("axis", {}).get("source") == "exporter":
            # The authored declaration is the source claim once the native geometry
            # and its sign were proved; the reprojected native line would only add
            # float noise the independent declaration check rejects.
            declared_axis = joint.get("declared_axis")
            if (
                not isinstance(declared_axis, (list, tuple))
                or len(declared_axis) != 3
                or not all(type(value) in (int, float) and math.isfinite(value) for value in declared_axis)
            ):
                raise PipelineError(f"joint {joint['name']!r} has no exact authored axis")
            axis = [float(value) for value in declared_axis]
        entry: dict[str, Any] = {
            "id": joint["name"],
            "name": joint["name"],
            "type": joint["type"],
            "parent": joint["parent"],
            "child": joint["child"],
            "axis": axis,
            "axis_reference": joint["axis_reference"],
        }
        if joint["axis"].get("source") == "exporter":
            entry["limits"] = {
                key: float(joint["limits"][key])
                for key in (
                    ("effort", "velocity")
                    if joint["type"] == "continuous"
                    else ("lower", "upper", "effort", "velocity")
                )
            }
            entry["limit_evidence"] = {
                "file": DISCOVERY_FILE,
                "sha256": discovery_sha256,
                "anchor": raw["exporter"]["raw_xml_sha256"],
            }
        elif joint["type"] == "continuous":
            if joint.get("drive") is not None:
                entry["limits"] = {
                    "effort": float(joint["drive"]["value"]["effort"]),
                    "velocity": float(joint["drive"]["value"]["velocity"]),
                }
        else:
            entry["limits"] = {
                "lower": float(joint["limits"]["lower"]),
                "upper": float(joint["limits"]["upper"]),
            }
            if joint.get("drive") is not None:
                entry["limits"]["effort"] = float(joint["drive"]["value"]["effort"])
                entry["limits"]["velocity"] = float(joint["drive"]["value"]["velocity"])
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
        if joint.get("limit_source") is not None:
            entry["limit_source"] = str(joint["limit_source"])
        joints_out.append(entry)
    coordinate_systems = sorted(
        {str(body["datum"]) for body in bodies if body.get("datum")}
        | {str(frame["coordinate_system"]) for frame in frames if frame.get("coordinate_system")}
    )
    source: dict[str, Any] = {
        "provider": "solidworks",
        "robot_name": identity.get("robot_name") or identity.get("robot_name_technical") or "robot",
        "assembly": str(confined(package.resolve(), cad_prefix + str(view["main_assembly"]))),
        "configuration": view["delivery_configuration"],
        "allowed_roots": [str(package.resolve())],
        "document_suffixes": [".sldasm", ".sldprt"],
        "geometry": {"enabled": True, "format": "stl_binary"},
        "coordinate_systems": coordinate_systems,
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
        "joints": joints_out,
        "frames": [
            {
                "id": frame["id"],
                "name": frame["name"],
                "parent": frame["parent"],
                "coordinate_system": frame["coordinate_system"],
            }
            for frame in sorted(frames, key=lambda item: item["name"])
        ],
        "material_source": "cad",
        "documented_masses": {},
        "mass_evidence": None,
        "geometry_exclusions": {},
        "expected_mass_kg": (checks or {}).get("expected_mass_kg"),
        "expected_extent_m": (checks or {}).get("expected_extent_m"),
        "require_saved": True,
    }
    return source


def _source_record(
    identity: dict,
    identity_sources: dict,
    handoff_digest: str | None,
    records: list[dict],
) -> dict | None:
    """The archived content identity (IF-06) that replaces the retired generated definition documents.

    Written only once the content identity is nameable - hardware, main assembly
    and configuration; anything less keeps the package unnamed and publication
    deferred with the findings already recorded above.
    """

    from ...interface_records import validate_source_record

    hardware = identity.get("hardware_id")
    assembly = identity.get("main_assembly")
    configuration = identity.get("delivery_configuration")
    if not (_text(hardware) and _text(assembly) and _text(configuration) and _text(handoff_digest)):
        return None
    references: dict[str, str] = {}
    for entry in records:
        reference = str(entry.get("reference") or "")
        checksum = str(entry.get("sha256") or "")
        if reference and checksum:
            references[reference] = checksum
    content = {
        "kind": "source-revision",
        "handoff_digest": str(handoff_digest),
        "main_assembly": str(assembly),
        "configuration": str(configuration),
        "hardware_id": str(hardware),
        "records": [
            {"reference": reference, "file_sha256": references[reference]} for reference in sorted(references)
        ],
    }
    return validate_source_record(
        {
            "schema_version": SOURCE_RECORD_SCHEMA,
            "source_revision_id": digest(content),
            "identity": content,
            "selection": {
                "main_assembly": content["main_assembly"],
                "configuration": content["configuration"],
                "hardware_id": content["hardware_id"],
                "hardware_source": "platform-config"
                if identity_sources.get("hardware_id") == "platform_configuration"
                else "cad",
            },
            "metadata": {
                "revision": identity.get("revision"),
                "owner": identity.get("owner"),
                "control": identity.get("control"),
                "change_summary": identity.get("change_summary"),
                "change_summary_source": {
                    "cad": "cad",
                    "submission": "submission",
                }.get(identity_sources.get("change_summary"), "generated"),
            },
        }
    )
