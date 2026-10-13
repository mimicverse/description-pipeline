"""Independent offline verification of a native-discovery prepared package.

This module shares no code with :mod:`description_pipeline.sources.solidworks.discovery`.
It re-derives rigid memberships, joints, axes, limits and names from the bound
raw native record and rejects any generated claim the record does not support.
It runs without Windows or SolidWorks: everything it needs (the raw record, the
embedded controlled records and the generated contract files) is inside the
package.

Only three facts are taken from the package as *claims*: ``robot.yaml``, a supplied
``cad-revision.json`` seal and the discovery payload itself.  The payload is
bound by digest to the package's provenance block; every derived value is
recomputed here before it is accepted.

Two verification regimes are supported by design, selected by the same shared
predicate as the producer (``sources/solidworks/discovery.py`` ``exporter_mode``):
an exporter-declaration capture keeps the declaration suite, while a capture whose
exporter state is a proven ``absent`` runs the mate-inference suite.  Gate ownership
(VER-01):
  * both regimes: ``discovery.binding``, ``suppression_state``, ``selection``,
    ``files``, ``imported_sources``, ``identity``, ``revision``, ``budgets``,
    ``masses``;
  * mate-inference only: ``graph``, ``frame_attachment``, ``bodies``, ``names``,
    ``joints``, ``tree``, ``frames``;
  * exporter-declaration only: ``discovery.declarations``
    (``native_declarations.py``, ``v14_declarations.py``);
  * conditional: budgets drop when no evidence is supplied, the revision
    not-sealed note stays a nonblocking diagnostic, and a binding failure stubs
    its dependents as ``not_run``.
"""

from __future__ import annotations

import hashlib
import json
import math
import ntpath
import re
from pathlib import Path, PurePosixPath

import yaml

VERIFICATION_SCHEMA = "solidworks-to-urdf.native-discovery-verification/v1"
DISCOVERY_SCHEMA = "solidworks-to-urdf.native-discovery/v1"
INPUT_SCHEMA = "solidworks-to-urdf.input/v1"
ROBOT_FILE = "robot.yaml"
REVISION_FILE = "cad-revision.json"
DISCOVERY_FILE = "discovery/native-discovery.json"

SUPPORTED_MATES = {
    "coincident",
    "concentric",
    "distance",
    "limitdistance",
    "parallel",
    "perpendicular",
    "angle",
    "limitangle",
    "lock",
}

AXIS_OFFSET_TOL_M = 5e-5
TOL = 1e-6
#: The documented native property that may carry a concentric rotation lock.
LOCK_ROTATION_API = "IConcentricMateFeatureData.LockRotation"
#: Conflict tolerances of the delivery contract: 0.05 mm and 0.05 degrees.
_CONFLICT_POS_TOL_M = 5e-5
_CONFLICT_ANGLE_EPS = 1.0 - math.cos(math.radians(0.05))
#: Kinds a localized fallback may surface; a cylinder must exist as recorded face evidence.
_LOCALIZED_KINDS = ("point", "line", "plane")

_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
INTERFACE_PREFIXES = ("CS_", "TCP_", "SCS_")
JCS_PREFIX = "JCS_"
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")


class _Failure(Exception):
    def __init__(self, code: str, message: str, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def _require(condition: bool, code: str, message: str, detail=None) -> None:
    if not condition:
        raise _Failure(code, message, detail)


def _read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(isinstance(data, dict), "discovery.file", f"{path.name} must be a mapping", {"path": path.name})
    return data


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _authored_identity(raw: dict, namespace: str) -> dict:
    """Read optional authored metadata without inventing a publication identity."""
    identity = raw.get("identity")
    properties = (raw.get("properties") or {}).get("document") or {}
    _require(identity is None or isinstance(identity, dict), "discovery.identity", "raw identity is not an object")
    _require(isinstance(properties, dict), "discovery.identity", "document properties are not an object")
    identity = identity or {}
    control = identity.get("control")
    _require(
        control is None or (isinstance(control, dict) and not set(control) - {"system", "reference"}),
        "discovery.identity",
        "authored control metadata is malformed",
    )

    def value(key):
        direct = (control or {}).get(key.split(".")[1]) if key.startswith("control.") else identity.get(key)
        stored = properties.get(namespace + "." + key)
        normalized = []
        for candidate in (direct, stored):
            if candidate is None:
                continue
            _require(
                isinstance(candidate, str)
                and bool(candidate.strip())
                and not any(ord(char) < 32 or ord(char) == 127 for char in candidate),
                "discovery.identity",
                "supplied metadata must be non-empty text",
                {"field": key},
            )
            normalized.append(candidate.strip())
        _require(
            len(set(normalized)) <= 1,
            "discovery.identity",
            "native metadata readings disagree",
            {"field": key},
        )
        return normalized[0] if normalized else None

    fields = ("hardware_id", "revision", "parent_revision", "owner", "change_summary", "robot_name")
    authored = {key: value(key) for key in fields}
    for key in ("hardware_id", "revision", "parent_revision"):
        _require(
            authored[key] is None or _ID.fullmatch(authored[key]),
            "discovery.identity",
            "supplied metadata is not an ASCII identifier",
            {"field": key},
        )
    _require(
        authored["parent_revision"] is None or authored["parent_revision"] != authored["revision"],
        "discovery.identity",
        "a revision cannot name itself as its parent",
    )
    _require(
        authored["robot_name"] is None or _SNAKE.fullmatch(authored["robot_name"]),
        "discovery.identity",
        "authored robot name is not snake_case",
    )
    system, reference = value("control.system"), value("control.reference")
    _require(
        system is None or system in {"git", "pdm", "handoff"},
        "discovery.identity",
        "supplied control system is unsupported",
    )
    _require(
        system != "git" or reference is None or re.search(r"\b[0-9a-f]{40}(?:[0-9a-f]{24})?\b", reference),
        "discovery.identity",
        "Git control reference lacks a full commit hash",
    )
    authored["control"] = {"system": system, "reference": reference} if system and reference else None
    authored["missing"] = [
        key for key in ("hardware_id", "revision", "owner", "change_summary", "control") if authored[key] is None
    ]
    return authored


def publication_metadata_state(raw: dict, namespace: str) -> dict:
    """Authored publication metadata exactly as the verifier reads it (single contract)."""

    return _authored_identity(raw, namespace)


def _digest_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise _Failure("discovery.symlink", f"package contains a symlink: {path.relative_to(root)}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if path.name.startswith("~$") and path.suffix.lower() in {".sldasm", ".sldprt"}:
            continue
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _entities(mate: dict) -> list[dict]:
    return [item for item in mate.get("entities") or [] if isinstance(item, dict)]


def _names(mate: dict) -> list[str]:
    out: list[str] = []
    for item in _entities(mate):
        name = str(item.get("component") or "")
        if name and name not in out:
            out.append(name)
    return out


def _nested_self_frame_entity(mate: dict) -> dict | None:
    """The scope's own frame entity inside a well-formed nested self-frame relation.

    A mate whose scope is ``S`` may carry exactly two raw recorded entities: one
    assembly-frame entity with ``component == S`` (S's own reference geometry, expressed
    in S's own space) and one ordinary endpoint whose occurrence lives inside S's subtree
    (``S/...``).  In the parent record the occurrence ``S`` supplies the transform into
    parent coordinates, so the relation is an ordinary in-scope pair -- it never takes
    part in the frozen top attachment and never grounds anything.  A malformed raw list
    (non-dict entries or a wrong length) or an endpoint outside the scope subtree is not
    this relation and stays unsupported.
    """

    raw = mate.get("entities")
    if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(item, dict) for item in raw):
        return None
    scope = mate.get("scope")
    if not isinstance(scope, str) or scope == "":
        return None
    flagged = [entity for entity in raw if entity.get("assembly_frame") is True]
    if len(flagged) != 1 or flagged[0].get("component") != scope:
        return None
    endpoint = next(entity for entity in raw if entity is not flagged[0])
    component = endpoint.get("component")
    if not isinstance(component, str) or not component.startswith(scope + "/"):
        return None
    return flagged[0]


def _number(value) -> float | None:
    """A real finite JSON number; booleans, numeric strings and null are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _unit_vector(vector) -> list[float] | None:
    try:
        raw = list(vector or ())
    except TypeError:
        return None
    if len(raw) != 3:
        return None
    values = [_number(value) for value in raw]
    if any(value is None for value in values):
        return None
    numbers = [value for value in values if value is not None]
    # hypot keeps a large finite magnitude from overflowing the norm, which would otherwise
    # normalize the vector to zero and silently make an unusable direction look usable.
    norm = math.hypot(*numbers)
    if norm == 0.0:
        return None
    return [value / norm for value in numbers]


def _finite_triple(values) -> list[float] | None:
    """A plain three-number point or vector; non-finite or misshaped data never becomes geometry."""
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        return None
    numbers = [_number(value) for value in values]
    if any(value is None for value in numbers):
        return None
    return [value for value in numbers if value is not None]


def _finite_limit(value) -> bool:
    """A real finite number; booleans and strings are not limits."""
    return _number(value) is not None


def _valid_limits(limits) -> bool:
    """A bounded range: finite, matching-unit, strictly ordered lower < upper."""
    if not isinstance(limits, dict):
        return False
    lower, upper = limits.get("lower"), limits.get("upper")
    return (
        _finite_limit(lower)
        and _finite_limit(upper)
        and float(lower) < float(upper)
        and limits.get("unit") in {"m", "rad"}
    )


def _report_safe(value):
    """Diagnostics only: JSON-serializable without non-finite numbers or unordered sets.

    The evidence itself is never rewritten; this shapes what the report carries about it, so a
    corrupt raw payload (``Infinity`` parsed from JSON, for example) cannot break the report.
    """
    if isinstance(value, dict):
        return {str(key): _report_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_report_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value


def _cross(left, right) -> list[float]:
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def _dot(left, right) -> float:
    return sum(one * two for one, two in zip(left, right, strict=True))


def _span(rows) -> list[list[float]]:
    basis: list[list[float]] = []
    for row in rows:
        vector = [float(value) for value in row]
        for other in basis:
            projection = _dot(vector, other)
            vector = [one - projection * two for one, two in zip(vector, other, strict=True)]
        norm = math.sqrt(_dot(vector, vector))
        if norm > TOL:
            basis.append([value / norm for value in vector])
    return basis


#: The frozen top assembly's own frame: identity at the assembly origin.
_ASSEMBLY_FRAME = [
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 1.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
]


def _frame_for(entity: dict, frames):
    """The entity's frame; a proven assembly-frame entity binds the identity frame.

    Only an entity that carries ``assembly_frame: true`` together with the frozen
    top-scope identity ``""`` reads the assembly frame.  The identity must be exactly
    the empty string: a marked entity with a missing, null or numeric component stays
    unknown and fails closed.
    """

    component = entity.get("component")
    if entity.get("assembly_frame") is True:
        return _ASSEMBLY_FRAME if component == "" else None
    return frames.get(component) if isinstance(component, str) else None


def _cached_materialization_reason(
    component: dict,
    provenance: dict,
    slots: list[dict],
    used: set[int],
    carrier: object,
    native: dict,
) -> str | None:
    """Exact sealed-carrier evidence for one cached native materialization.

    The selection session and the discovery session are different native sessions, so
    their session temp roots differ and the two materialization paths can never be
    compared directly.  The stable identity is the ultimate native owner (with its
    frozen bytes), the scoped occurrence, the defining anchor and the recorded
    configuration, while each materialization path must be structurally a session
    temp document under its own recorded owned temp root.  Nothing is guessed from
    names or suffixes, and a present external file is never downgraded.
    """

    if not isinstance(carrier, dict) or not isinstance(carrier.get("closure"), dict):
        return "the sealed selection carrier is absent"
    owner = provenance.get("owner")
    owner_sha = provenance.get("owner_sha256")
    occurrence = provenance.get("occurrence")
    anchor = provenance.get("defining_anchor")
    materialization = (
        provenance.get("materialization_path")
        or provenance.get("materialization")
        or provenance.get("native_document_path")
    )
    temp_root = provenance.get("session_temp_root")
    author = provenance.get("author_imported_path")
    virtual = provenance.get("is_virtual")
    if not (isinstance(owner, str) and owner.strip()):
        return "the cached materialization evidence names no ultimate owner"
    if not (isinstance(occurrence, str) and occurrence.strip()):
        return "the cached materialization evidence names no occurrence"
    if anchor is not None and not isinstance(anchor, str):
        return "the cached materialization defining anchor is unreadable"
    if not isinstance(owner_sha, str) or _HEX.match(owner_sha) is None:
        return "the cached materialization owner bytes are unproven"
    if not (isinstance(materialization, str) and ntpath.isabs(materialization)):
        return "the cached materialization evidence carries no absolute session materialization"
    if not _session_temp_path(materialization, temp_root):
        if not (isinstance(temp_root, str) and ntpath.isabs(temp_root)):
            return "the cached materialization evidence carries no owned session temp root"
        parts = materialization.replace("\\", "/").split("/")
        if not any(part.casefold() == "ic~~" for part in parts if part):
            return "the recorded materialization is not a session temp materialization"
        return "the session materialization is outside its recorded owned session temp root"
    if author is not None and not isinstance(author, str):
        return "the cached materialization author metadata is unreadable"
    if type(virtual) is not bool:
        return "the cached materialization virtual state is unread"
    files = carrier.get("files")
    if not isinstance(files, dict) or files.get(owner) != owner_sha or native.get(owner) != owner_sha:
        return "the cached materialization owner bytes are not bound to the sealed selection"

    def folded(value: object) -> str:
        return str(value or "").replace("\\", "/").casefold()

    matches = []
    for index, slot in enumerate(slots):
        if index in used:
            continue
        slot_owner = slot.get("owner") if isinstance(slot.get("owner"), str) and slot.get("owner") else None
        slot_owner = slot_owner or slot.get("document")
        if slot_owner is None or folded(slot_owner) != folded(owner):
            continue
        if str(slot.get("instance") or "") != occurrence:
            continue
        if slot.get("owner_anchor") != anchor:
            continue
        if slot.get("owner_sha256") != owner_sha:
            continue
        slot_configuration = slot.get("configuration")
        if slot_configuration is not None and component.get("configuration") != slot_configuration:
            continue
        if not _session_temp_path(slot.get("materialization"), slot.get("session_temp_root")):
            continue
        if provenance.get("author_imported_in_source_root") is True:
            # In-root author sources are re-resolved by the engine to whichever root
            # holds the bytes, so the absolute author path is relocation-dependent on
            # each side.  Match the recorded root-relative name and the sealed bytes,
            # with each side's containment proven independently.
            relative = provenance.get("author_imported_relative")
            source_root = provenance.get("source_root")
            bound = slot.get("bound_source")
            stage_root = carrier["closure"].get("stage_root")
            if not (
                slot.get("author_metadata_in_stage") is True
                and isinstance(relative, str)
                and relative.strip()
                and isinstance(source_root, str)
                and ntpath.isabs(source_root)
                and isinstance(author, str)
                and ntpath.isabs(author)
                and _owned_relative_match(author, source_root, relative)
                and isinstance(bound, str)
                and bound.strip()
                and isinstance(stage_root, str)
                and ntpath.isabs(stage_root)
                and _owned_relative_match(slot.get("author_imported_path"), stage_root, bound)
                and bound.replace("\\", "/").casefold() == relative.replace("\\", "/").casefold()
            ):
                continue
            sealed, recorded = files.get(bound), native.get(relative)
            if not (isinstance(sealed, str) and sealed and sealed == recorded):
                continue
        else:
            slot_author = slot.get("author_imported_path")
            if (slot_author is None) != (author is None):
                continue
            if slot_author is not None and ntpath.normcase(slot_author) != ntpath.normcase(author):
                continue
        slot_state = slot.get("suppression_state")
        if isinstance(slot_state, int) and slot_state != component.get("suppression_state"):
            continue
        if "is_virtual" in slot and slot.get("is_virtual") is not virtual:
            continue
        matches.append(index)
    if not matches:
        return "no sealed cached materialization matches this occurrence exactly"
    if len(matches) > 1:
        return "the sealed carrier records ambiguous cached materializations for this occurrence"
    used.add(matches[0])
    return None


def _session_temp_path(value: object, root: object) -> bool:
    """True when ``value`` is an absolute session temp document under ``root``."""

    if not (isinstance(value, str) and ntpath.isabs(value)):
        return False
    if not (isinstance(root, str) and ntpath.isabs(root)):
        return False
    if not any(part.casefold() == "ic~~" for part in value.replace("\\", "/").split("/") if part):
        return False
    try:
        relative = ntpath.relpath(value, root)
    except ValueError:
        return False
    return relative != ".." and not relative.startswith("..\\") and not ntpath.isabs(relative)


def _owned_relative_match(value: object, root: object, relative: str) -> bool:
    """True when ``value`` is inside ``root`` at exactly the ``relative`` location."""

    if not (isinstance(value, str) and isinstance(root, str) and isinstance(relative, str)):
        return False
    try:
        computed = ntpath.relpath(value, root)
    except ValueError:
        return False
    if computed == ".." or computed.startswith("..\\") or ntpath.isabs(computed):
        return False
    return computed.replace("\\", "/").casefold() == relative.replace("\\", "/").casefold()


def _require_suppression_states(record: dict) -> None:
    """Every component must carry the documented raw suppression state.

    The raw ``GetSuppression2`` integer is authoritative: 0 suppressed, 1
    lightweight, 2 fully resolved, 3 resolved, 4 fully lightweight.  State 5
    (internal-ID mismatch), missing, boolean, undocumented and incoherent
    values are refused, and a lightweight occurrence (1/4) cannot be
    independently measured, so it is refused instead of being counted as
    suppressed.  Both emitted flags are required booleans that must agree with
    the state; the legacy raw ``IsSuppressed`` boolean is never consulted.
    """

    _require(
        isinstance(record, dict),
        "discovery.suppression_state",
        "the raw record is not an object",
        {"record": type(record).__name__},
    )
    components = record.get("components")
    _require(
        isinstance(components, list),
        "discovery.suppression_state",
        "the raw record carries no component list",
        {"components": type(components).__name__},
    )
    for item in components:
        _require(
            isinstance(item, dict),
            "discovery.suppression_state",
            "a raw component entry is not an object",
            {"entry": repr(item)[:200]},
        )
        name = str(item.get("name2") or "?")
        state = item.get("suppression_state")
        _require(
            type(state) is int and state in {0, 1, 2, 3, 4},
            "discovery.suppression_state",
            "a component does not carry the documented raw suppression state (integer 0..4)",
            {"component": name, "suppression_state": state},
        )
        suppressed = item.get("suppressed")
        lightweight = item.get("lightweight")
        _require(
            type(suppressed) is bool and type(lightweight) is bool,
            "discovery.suppression_state",
            "a component must carry boolean suppressed and lightweight flags",
            {"component": name, "suppressed": suppressed, "lightweight": lightweight},
        )
        _require(
            suppressed == (state == 0) and lightweight == (state in {1, 4}),
            "discovery.suppression_state",
            "the emitted suppression flags do not agree with the raw suppression state",
            {
                "component": name,
                "suppression_state": state,
                "suppressed": suppressed,
                "lightweight": lightweight,
            },
        )
        if state in {1, 4}:
            _require(
                False,
                "discovery.suppression_lightweight",
                "a lightweight component cannot be independently measured; the native capture must fully load it",
                {"component": name, "suppression_state": state},
            )


def _require_source_selection(payload: dict) -> None:
    """Independently recompute the sealed source selection carried by the raw record.

    The proof is mandatory: the one supported native capture carries
    ``raw.source_selection``, and a bundle without it is not verifiable.  The manifest
    must recompute exactly, every selected source must be bound by the raw native file
    inventory with a matching hash, and the raw main choice must agree.
    """

    from ..sources.solidworks.selection import verify_selection

    raw = payload.get("raw") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        raise _Failure(
            "discovery.selection",
            "the native capture carries no raw record with the sealed source selection",
            None,
        )
    manifest = raw.get("source_selection")
    if manifest is None:
        raise _Failure(
            "discovery.selection",
            "raw.source_selection is required: a capture without the sealed closure proof is not verifiable",
            None,
        )
    if not isinstance(manifest, dict) or manifest.get("scope") != "native_staged_folder_closure":
        raise _Failure(
            "discovery.selection",
            "raw.source_selection is not a sealed closure selection manifest",
            None,
        )
    files = manifest.get("files")
    excluded = manifest.get("excluded_files")
    if not isinstance(files, dict) or not isinstance(excluded, dict):
        raise _Failure("discovery.selection", "sealed selection carries no file inventories", None)
    upload = {**excluded, **files}
    try:
        verify_selection(manifest, upload, files, reader_sha256=manifest.get("reader_sha256"))
    except Exception as error:  # noqa: BLE001 - reported as a structured verifier failure
        raise _Failure("discovery.selection", f"sealed selection does not recompute: {error}", None) from error
    native_files = raw.get("files")
    if not isinstance(native_files, dict) or not native_files:
        raise _Failure("discovery.selection", "the raw record carries no native file inventory", None)
    for name, checksum in files.items():
        if native_files.get(name) != checksum:
            raise _Failure(
                "discovery.selection",
                f"selected source {name!r} is not bound by the raw record",
                {"document": name},
            )
    raw_selection = raw.get("selection")
    if isinstance(raw_selection, dict):
        chosen = raw_selection.get("main_assembly")
        if chosen is not None and str(chosen).replace("\\", "/") != manifest.get("main_assembly"):
            raise _Failure("discovery.selection", "the raw main choice differs from the sealed selection", None)


def _frames(record: dict) -> dict[str, list[list[float]] | None]:
    _require_suppression_states(record)
    frames: dict[str, list[list[float]] | None] = {}
    for item in record.get("components") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name2")
        suppressed = item.get("suppressed")
        if not isinstance(name, str) or not isinstance(suppressed, bool) or suppressed:
            continue
        values = [_number(value) for value in item.get("transform") or ()]
        numbers = [value for value in values if value is not None]
        frames[name] = (
            [numbers[0:4], numbers[4:8], numbers[8:12], numbers[12:16]]
            if len(values) == 16 and len(numbers) == 16
            else None
        )
    return frames


def _apply_point(point, frame):
    x, y, z = (float(value) for value in point)
    return [
        frame[0][0] * x + frame[0][1] * y + frame[0][2] * z + frame[0][3],
        frame[1][0] * x + frame[1][1] * y + frame[1][2] * z + frame[1][3],
        frame[2][0] * x + frame[2][1] * y + frame[2][2] * z + frame[2][3],
    ]


def _apply_vector(vector, frame):
    x, y, z = (float(value) for value in vector)
    return [
        frame[0][0] * x + frame[0][1] * y + frame[0][2] * z,
        frame[1][0] * x + frame[1][1] * y + frame[1][2] * z,
        frame[2][0] * x + frame[2][1] * y + frame[2][2] * z,
    ]


def _plane_basis(axis: list[float]) -> list[list[float]]:
    """Two orthonormal directions spanning the plane orthogonal to the axis."""

    helper = [1.0, 0.0, 0.0] if abs(axis[0]) <= 0.9 else [0.0, 1.0, 0.0]
    first = _unit_vector(_cross(axis, helper))
    second = _unit_vector(_cross(axis, first)) if first is not None else None
    return [value for value in (first, second) if value is not None]


def _translation_row(direction, point):
    moment = _cross(point, direction)
    return [direction[0], direction[1], direction[2], moment[0], moment[1], moment[2]]


def _rotation_row(direction):
    return [0.0, 0.0, 0.0, direction[0], direction[1], direction[2]]


def _null_space(rows, dim: int = 6) -> list[list[float]]:
    """The oracle's own elimination: twists the rows leave free."""

    matrix = [[float(value) for value in row] for row in rows]
    pivot_columns: list[int] = []
    index = 0
    for column in range(dim):
        chosen = None
        for candidate in range(index, len(matrix)):
            if abs(matrix[candidate][column]) > TOL:
                chosen = candidate
                break
        if chosen is None:
            continue
        matrix[index], matrix[chosen] = matrix[chosen], matrix[index]
        factor = matrix[index][column]
        matrix[index] = [value / factor for value in matrix[index]]
        for other in range(len(matrix)):
            if other != index and abs(matrix[other][column]) > TOL:
                weight = matrix[other][column]
                matrix[other] = [one - weight * two for one, two in zip(matrix[other], matrix[index], strict=True)]
        pivot_columns.append(column)
        index += 1
        if index == len(matrix):
            break
    basis: list[list[float]] = []
    for column in [value for value in range(dim) if value not in pivot_columns]:
        vector = [0.0] * dim
        vector[column] = 1.0
        for row, pivot in enumerate(pivot_columns):
            vector[pivot] = -matrix[row][column]
        norm = math.sqrt(sum(value * value for value in vector))
        basis.append([value / norm for value in vector])
    return basis


def _axis_entity(entity: dict) -> bool:
    """An entity that defines an axis: a cylindrical face or a circular edge."""

    return isinstance(entity.get("cylinder"), dict) or isinstance(entity.get("circle"), dict)


def _malformed_recorded(entity: dict) -> bool:
    """A present recorded primary that is unusable refuses the entity outright.

    Recorded primaries are the flat faces (``plane``/``cylinder``/``circle``) and the
    bare-list ``point``; ``None`` means absent.  A present value of any other shape is
    malformed evidence: it must fail closed and never be silently substituted by
    localized provenance.  Line references have no flat record by design.
    """

    for name in ("plane", "cylinder", "circle"):
        value = entity.get(name)
        if value is not None and not isinstance(value, dict):
            return True
    point = entity.get("point")
    return point is not None and (not isinstance(point, (list, tuple)) or _finite_triple(point) is None)


def _kind(entity: dict) -> str | None:
    """The entity's effective kind: recorded face first, then a licensed localization.

    Mirrors :func:`_geometry`'s resolution order so branch selection and geometry always
    agree; a failed localization (``mate_entity_reference.error``) never supplies a kind,
    and a present-but-unusable recorded primary refuses the entity.
    """

    if _malformed_recorded(entity):
        return None
    for known in ("cylinder", "circle", "plane"):
        if isinstance(entity.get(known), dict):
            return known
    if isinstance(entity.get("point"), (list, tuple)):
        return "point"
    reference = entity.get("mate_entity_reference")
    if isinstance(reference, dict) and reference.get("error") is None:
        geometry = reference.get("geometry")
        if isinstance(geometry, dict):
            if geometry.get("frame") == "component-local":
                kind = str(geometry.get("kind") or "")
                if kind in _LOCALIZED_KINDS or kind == "cylinder":
                    return kind
            return None
        if isinstance(entity.get("line"), dict):
            # No localized geometry to validate: a stray flat line key remains
            # last-resort evidence, exactly as the source's view consumes it.
            return "line"
        return None
    if reference is None and isinstance(entity.get("line"), dict):
        return "line"
    return None


def _recorded_geometry(entity: dict, frame):
    """Recorded face evidence in the assembly frame as ``(kind, point, direction)``.

    ``(None, None, None)`` means no recorded kind exists.  ``("invalid", None, None)``
    means recorded evidence exists but is unusable: the caller must fail closed rather
    than silently falling back to localized provenance.  Lines are never recorded flat.
    """

    def finish(kind: str, source, direction):
        point = _apply_point(source, frame) if source is not None else None
        if point is None or direction is None or not all(math.isfinite(value) for value in point):
            return "invalid", None, None
        return kind, point, direction

    if _malformed_recorded(entity):
        return "invalid", None, None
    if isinstance(entity.get("cylinder"), dict):
        source = _finite_triple(entity["cylinder"].get("point"))
        return finish(
            "cylinder",
            source,
            _unit_vector(_apply_vector(entity["cylinder"].get("direction") or (), frame)),
        )
    if isinstance(entity.get("circle"), dict):
        source = _finite_triple(entity["circle"].get("center"))
        return finish(
            "circle",
            source,
            _unit_vector(_apply_vector(entity["circle"].get("normal") or (), frame)),
        )
    if isinstance(entity.get("plane"), dict):
        source = _finite_triple(entity["plane"].get("point"))
        return finish(
            "plane",
            source,
            _unit_vector(_apply_vector(entity["plane"].get("normal") or (), frame)),
        )
    if isinstance(entity.get("point"), (list, tuple)):
        source = _finite_triple(entity["point"])
        point = _apply_point(source, frame) if source is not None else None
        if point is None or not all(math.isfinite(value) for value in point):
            return "invalid", None, None
        return "point", point, None
    return None, None, None


def _geometry_agrees(kind: str, recorded: dict, localized: dict) -> bool:
    """Undirected agreement between recorded face evidence and localized provenance.

    Planes, lines and cylinders are undirected geometry: a flipped normal or direction
    describes the same reference, while a different plane/axis, a moved point or a
    changed cylinder radius blocks.
    """

    def point(value):
        return _finite_triple(value or ())

    def flat_distance(value, origin, direction) -> float:
        offset = [value[index] - origin[index] for index in range(3)]
        cross = _cross(offset, direction)
        return math.sqrt(sum(item * item for item in cross))

    if kind == "plane":
        recorded_normal = _unit_vector(recorded.get("normal") or ())
        localized_normal = _unit_vector(localized.get("normal") or ())
        recorded_point, localized_point = point(recorded.get("point")), point(localized.get("point"))
        if None in (recorded_normal, localized_normal, recorded_point, localized_point):
            return False
        if abs(abs(_dot(recorded_normal, localized_normal)) - 1.0) > _CONFLICT_ANGLE_EPS:
            return False
        return (
            abs(sum((localized_point[index] - recorded_point[index]) * recorded_normal[index] for index in range(3)))
            <= _CONFLICT_POS_TOL_M
        )
    if kind == "line":
        recorded_direction = _unit_vector(recorded.get("direction") or ())
        localized_direction = _unit_vector(localized.get("direction") or ())
        recorded_point, localized_point = point(recorded.get("point")), point(localized.get("point"))
        if None in (recorded_direction, localized_direction, recorded_point, localized_point):
            return False
        if abs(abs(_dot(recorded_direction, localized_direction)) - 1.0) > _CONFLICT_ANGLE_EPS:
            return False
        return (
            flat_distance(localized_point, recorded_point, recorded_direction) <= _CONFLICT_POS_TOL_M
            and flat_distance(recorded_point, localized_point, localized_direction) <= _CONFLICT_POS_TOL_M
        )
    if kind == "cylinder":
        recorded_direction = _unit_vector(recorded.get("direction") or recorded.get("normal") or ())
        localized_direction = _unit_vector(localized.get("direction") or ())
        recorded_point = point(recorded.get("point") or recorded.get("center"))
        localized_point = point(localized.get("point"))
        recorded_radius, localized_radius = recorded.get("radius"), localized.get("radius")
        if None in (recorded_direction, localized_direction, recorded_point, localized_point):
            return False
        if not isinstance(recorded_radius, (int, float)) or not isinstance(localized_radius, (int, float)):
            return False
        if abs(float(recorded_radius) - float(localized_radius)) > _CONFLICT_POS_TOL_M:
            return False
        if abs(abs(_dot(recorded_direction, localized_direction)) - 1.0) > _CONFLICT_ANGLE_EPS:
            return False
        return (
            flat_distance(localized_point, recorded_point, recorded_direction) <= _CONFLICT_POS_TOL_M
            and flat_distance(recorded_point, localized_point, localized_direction) <= _CONFLICT_POS_TOL_M
        )
    return False


def _localized_values(geometry):
    """One localized shape in component coordinates, or ``None`` when it must block.

    The provenance must be component-local, the kind must be a supported fallback kind
    (a localized cylinder is carried only for agreement with recorded face evidence)
    and the shape must be complete and finite.
    """

    def strict_triple(values):
        if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
            return None
        items = list(values)
        if len(items) != 3 or any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in items):
            return None
        numbers = [float(item) for item in items]
        if not all(math.isfinite(item) for item in numbers):
            return None
        return numbers

    def strict_unit(values):
        numbers = strict_triple(values)
        return _unit_vector(numbers) if numbers is not None else None

    if not isinstance(geometry, dict) or geometry.get("frame") != "component-local":
        return None
    kind = str(geometry.get("kind") or "")
    if kind not in _LOCALIZED_KINDS and kind != "cylinder":
        return None
    values = {"kind": kind}
    if kind == "point":
        point = strict_triple(geometry.get("point") or ())
        if point is None:
            return None
        values["point"] = point
        return values
    if kind == "line":
        point, direction = strict_triple(geometry.get("point") or ()), strict_unit(geometry.get("direction") or ())
    elif kind == "plane":
        point, direction = strict_triple(geometry.get("point") or ()), strict_unit(geometry.get("normal") or ())
    else:  # cylinder
        radius = geometry.get("radius")
        if not isinstance(radius, (int, float)) or isinstance(radius, bool) or not math.isfinite(float(radius)):
            return None
        values["radius"] = float(radius)
        point, direction = strict_triple(geometry.get("point") or ()), strict_unit(geometry.get("direction") or ())
    if point is None or direction is None:
        return None
    values["point"], values["direction"] = point, direction
    if kind == "plane":
        # Canonical plane field: agreement checks compare the localized normal explicitly.
        values["normal"] = direction
    return values


def _geometry(entity: dict, frames):
    frame = _frame_for(entity, frames)
    if frame is None:
        return None, None
    recorded_kind, point, direction = _recorded_geometry(entity, frame)
    if recorded_kind == "invalid":
        return None, None

    def raw_line():
        # A flat line key is not recorded evidence (records carry line references only
        # through localization), so it can never override validated provenance; it is
        # consumed as-is only when no localized geometry exists to validate.
        value = entity.get("line")
        if not isinstance(value, dict):
            return None, None
        source = _finite_triple(value.get("point"))
        surface = _apply_point(source, frame) if source is not None else None
        heading = _unit_vector(_apply_vector(value.get("direction") or (), frame))
        if surface is None or heading is None or not all(math.isfinite(value) for value in surface):
            return None, None
        return surface, heading

    reference = entity.get("mate_entity_reference")
    if reference is None:
        return (point, direction) if recorded_kind is not None else raw_line()
    if not isinstance(reference, dict) or reference.get("error") is not None:
        # Malformed or failed provenance never blocks recorded face evidence; without
        # recorded evidence the entity fails closed.
        return (point, direction) if recorded_kind is not None else (None, None)
    geometry = reference.get("geometry")
    if not isinstance(geometry, dict):
        # No localized geometry to validate: recorded evidence stays the source, and a
        # stray flat line key remains last-resort evidence.
        return (point, direction) if recorded_kind is not None else raw_line()
    values = _localized_values(geometry)
    if values is None:
        # Missing or malformed provenance never blocks recorded face evidence.
        return (point, direction) if recorded_kind is not None else (None, None)
    kind = values["kind"]
    if kind == "cylinder":
        # A shaft is face evidence or it does not exist; localization never fabricates one.
        if recorded_kind != "cylinder" or not _geometry_agrees("cylinder", entity["cylinder"], values):
            return None, None
        return point, direction
    if kind == "point":
        if recorded_kind == "point":
            stored = _finite_triple(entity.get("point") or ())
            if stored is None or any(
                abs(left - right) > _CONFLICT_POS_TOL_M for left, right in zip(stored, values["point"], strict=True)
            ):
                return None, None
            return point, None
        if recorded_kind is None:
            surface = _apply_point(values["point"], frame)
            if not all(math.isfinite(value) for value in surface):
                return None, None
            return surface, None
        return point, direction
    if recorded_kind == kind:
        if not _geometry_agrees(kind, entity[kind], values):
            return None, None
        return point, direction
    if recorded_kind is None:
        surface_point = _apply_point(values["point"], frame)
        surface_direction = _unit_vector(_apply_vector(values["direction"], frame))
        if surface_direction is None or not all(math.isfinite(value) for value in surface_point):
            return None, None
        return surface_point, surface_direction
    # A comparable localized reference that contradicts the recorded primary is refused.
    return None, None


def _rejected_entity(entity: dict, frames) -> bool:
    """The source refuses an entity whose evidence cannot be validated; mirror that gate.

    A present-but-unusable recorded primary, a comparable contradiction, or a required
    localization that cannot be validated blocks the whole mate before branch selection,
    so branches that never read geometry (lock, limitangle) cannot accept the mate.
    """

    if _malformed_recorded(entity):
        return True
    point, direction = _geometry(entity, frames)
    if point is not None or direction is not None:
        return False
    reference = entity.get("mate_entity_reference")
    if reference is None:
        return False
    if isinstance(reference, dict) and reference.get("error") is None:
        geometry = reference.get("geometry")
        if not isinstance(geometry, dict):
            return False
    return True


def _rows_for(mate: dict, frames) -> dict | None:
    """The oracle's own constraint reconstruction; ``None`` means unsupported."""

    kind = str(mate.get("type") or "").strip().lower()
    if kind not in SUPPORTED_MATES:
        return None
    entities = _entities(mate)
    nested = _nested_self_frame_entity(mate)
    if nested is not None:
        # The scope's own frame entity resolves through the scope occurrence transform,
        # exactly like the same relation written as an ordinary in-scope pair.
        entities = [{**entity, "assembly_frame": False} if entity is nested else entity for entity in entities]
    if len(entities) < 2:
        return None
    first, second = entities[0], entities[1]
    for entity in (first, second):
        if _rejected_entity(entity, frames):
            return None
    limits = mate.get("limits") if isinstance(mate.get("limits"), dict) else None
    axes = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    if kind == "lock":
        first_frame = _frame_for(first, frames)
        second_frame = _frame_for(second, frames)
        if first_frame is None or second_frame is None:
            return None
        # A solved lock removes all six relative freedoms; the constraint basis can be stated at
        # the first captured occurrence origin, which needs no face, shaft or vertex evidence.
        point = [first_frame[index][3] for index in range(3)]
        rows = [_translation_row(axis, point) for axis in axes] + [_rotation_row(axis) for axis in axes]
        return {"rows": rows, "limits": limits, "axis": None, "point": point}
    if kind == "concentric":
        # Generic point-on-axis form: one bare recorded point (no direction of its own)
        # against one axis entity (cylindrical face or circular edge).  The point carries
        # only its position, so a generic entity parameter is never promoted into a fake
        # axis, and only the axis entity may define the line.  Solved-state consistency is
        # required before the two point-on-line rows are emitted.
        left_point, left_axis = _geometry(first, frames)
        right_point, right_axis = _geometry(second, frames)
        point_candidates = [
            point
            for entity, point, _axis in ((first, left_point, left_axis), (second, right_point, right_axis))
            if _kind(entity) == "point" and point is not None
        ]
        axis_candidates = [
            (point, axis)
            for entity, point, axis in ((first, left_point, left_axis), (second, right_point, right_axis))
            if _axis_entity(entity) and point is not None and axis is not None
        ]
        if len(point_candidates) == 1 and len(axis_candidates) == 1:
            point_point = point_candidates[0]
            axis_point, axis_direction = axis_candidates[0]
            delta = [point_point[index] - axis_point[index] for index in range(3)]
            along = _dot(delta, axis_direction)
            radial = [delta[index] - along * axis_direction[index] for index in range(3)]
            if math.sqrt(_dot(radial, radial)) > AXIS_OFFSET_TOL_M:
                return None
            plane = _plane_basis(axis_direction)
            if len(plane) != 2:
                return None
            # Point-on-line rows live at the recorded point P: [n, P x n] for two
            # independent directions n orthogonal to the axis.  The point evidences
            # neither a shaft/joint identity nor a ground binding.
            rows = [_translation_row(direction, point_point) for direction in plane]
            return {"rows": rows, "limits": limits, "axis": None, "point": None}
        if not _axis_entity(first) or not _axis_entity(second):
            return None
        if (
            left_point is None
            or right_point is None
            or left_axis is None
            or right_axis is None
            or abs(abs(_dot(left_axis, right_axis)) - 1.0) > TOL
        ):
            return None
        # Solved-state consistency: the recorded axes must be coaxial, not merely
        # parallel, exactly as the constraint requires of the solved assembly.
        delta = [right_point[index] - left_point[index] for index in range(3)]
        along = _dot(delta, left_axis)
        radial = [delta[index] - along * left_axis[index] for index in range(3)]
        if math.sqrt(_dot(radial, radial)) > AXIS_OFFSET_TOL_M:
            return None
        plane = _plane_basis(left_axis)
        if len(plane) != 2:
            return None
        rows = [_translation_row(direction, left_point) for direction in plane] + [
            _rotation_row(direction) for direction in plane
        ]
        # The documented concentric rotation lock removes the spin only: one axial
        # rotation row, and only from an exact native boolean true.  A missing,
        # false or non-boolean value leaves the revolute freedom intact.
        if mate.get("lock_rotation") is True and mate.get("lock_rotation_api") == LOCK_ROTATION_API:
            rows.append(_rotation_row(left_axis))
        # Circle axes reconstruct the constraint but never evidence the shaft: the
        # independent interface gate must keep requiring a real cylindrical entity.
        shaft = None
        shaft_point = None
        for entity, point, axis in ((first, left_point, left_axis), (second, right_point, right_axis)):
            if isinstance(entity.get("cylinder"), dict):
                shaft, shaft_point = axis, point
                break
        return {"rows": rows, "limits": limits, "axis": shaft, "point": shaft_point}
    if kind == "coincident":
        left_point, left_axis = _geometry(first, frames)
        right_point, right_axis = _geometry(second, frames)
        if left_axis is not None and right_axis is not None and _kind(first) == "plane" and _kind(second) == "plane":
            if abs(abs(_dot(left_axis, right_axis)) - 1.0) > TOL:
                return None
            normal = left_axis if _dot(left_axis, right_axis) >= 0 else [-value for value in left_axis]
            plane = _plane_basis(normal)
            if len(plane) != 2:
                return None
            rows = [_translation_row(normal, left_point)] + [_rotation_row(direction) for direction in plane]
            return {"rows": rows, "limits": limits, "axis": None, "point": left_point}
        if _kind(first) == "circle" and _kind(second) == "circle":
            if left_axis is None or right_axis is None or abs(abs(_dot(left_axis, right_axis)) - 1.0) > TOL:
                return None
            separation = math.sqrt(sum((left_point[i] - right_point[i]) ** 2 for i in range(3)))
            if separation > AXIS_OFFSET_TOL_M:
                return None
            return {
                "rows": [_translation_row(left_axis, left_point)],
                "limits": limits,
                "axis": None,
                "point": left_point,
            }
        if (_kind(first) == "circle" and _kind(second) == "plane") or (
            _kind(second) == "circle" and _kind(first) == "plane"
        ):
            # A circular edge coincident with a plane: the circle's plane is the
            # plane (two tilts locked) and its centre lies in it (one translation
            # locked); sliding and spinning in the plane stay free.
            circle_entity, plane_entity = (first, second) if _kind(first) == "circle" else (second, first)
            circle_point, circle_axis = _geometry(circle_entity, frames)
            plane_point, plane_axis = _geometry(plane_entity, frames)
            if (
                circle_point is None
                or plane_point is None
                or circle_axis is None
                or plane_axis is None
                or abs(abs(_dot(circle_axis, plane_axis)) - 1.0) > TOL
            ):
                return None
            normal = plane_axis if _dot(circle_axis, plane_axis) >= 0 else [-value for value in plane_axis]
            offset = abs(sum((circle_point[index] - plane_point[index]) * normal[index] for index in range(3)))
            if offset > AXIS_OFFSET_TOL_M:
                return None
            plane = _plane_basis(normal)
            if len(plane) != 2:
                return None
            rows = [_translation_row(normal, circle_point)] + [_rotation_row(direction) for direction in plane]
            return {"rows": rows, "limits": limits, "axis": None, "point": circle_point}

        def line_plane_rows(line_entity: dict, plane_entity: dict):
            # A line coincident with a plane lies in that plane: the recorded solved state
            # must already show the direction perpendicular to the normal and the line's
            # point on the plane; only then are exactly those two constraint rows emitted.
            line_point, line_direction = _geometry(line_entity, frames)
            plane_point, plane_normal = _geometry(plane_entity, frames)
            if None in (line_point, line_direction, plane_point, plane_normal):
                return None
            if abs(_dot(line_direction, plane_normal)) > TOL:
                return None
            separation = abs(sum((line_point[index] - plane_point[index]) * plane_normal[index] for index in range(3)))
            if separation > AXIS_OFFSET_TOL_M:
                return None
            pivot = _unit_vector(_cross(line_direction, plane_normal))
            rows = ([_rotation_row(pivot)] if pivot is not None else []) + [_translation_row(plane_normal, line_point)]
            return {"rows": rows, "limits": limits, "axis": None, "point": line_point}

        if _kind(first) == "line" and _kind(second) == "plane":
            return line_plane_rows(first, second)
        if _kind(first) == "plane" and _kind(second) == "line":
            return line_plane_rows(second, first)
        if _kind(first) == "point" and _kind(second) == "point":
            rows = [_translation_row(axis, left_point) for axis in axes]
            return {"rows": rows, "limits": limits, "axis": None, "point": left_point}
        # Only the explicit plane-plus-vertex form remains: the face side must be a
        # recorded plane and the other side an explicit point.  A missing entity, a
        # cylinder, a circle, or any unrelated geometry is never treated as a vertex.
        if _kind(first) == "plane" and left_axis is not None and _kind(second) == "point" and right_point is not None:
            normal, point = left_axis, right_point
        elif _kind(second) == "plane" and right_axis is not None and _kind(first) == "point" and left_point is not None:
            normal, point = right_axis, left_point
        else:
            return None
        # A vertex on a face removes one translation only.
        return {"rows": [_translation_row(normal, point)], "limits": limits, "axis": None, "point": point}
    if kind == "limitangle":
        return {"rows": [], "limits": limits, "axis": None, "point": None}
    if kind == "limitdistance":
        _first_point, first_axis = _geometry(first, frames)
        _second_point, second_axis = _geometry(second, frames)
        if (
            _kind(first) == "plane"
            and _kind(second) == "plane"
            and first_axis is not None
            and second_axis is not None
            and abs(abs(_dot(first_axis, second_axis)) - 1.0) <= TOL
        ):
            normal = first_axis if _dot(first_axis, second_axis) >= 0 else [-value for value in first_axis]
            plane = _plane_basis(normal)
            if len(plane) != 2:
                return None
            return {
                "rows": [_rotation_row(direction) for direction in plane],
                "limits": limits,
                "axis": None,
                "point": None,
            }
        return {"rows": [], "limits": limits, "axis": None, "point": None}
    if kind == "distance":
        left_point, left_axis = _geometry(first, frames)
        right_point, right_axis = _geometry(second, frames)
        if _kind(first) == "point" and _kind(second) == "point":
            direction = _unit_vector([right_point[i] - left_point[i] for i in range(3)])
            point = [(left_point[i] + right_point[i]) / 2.0 for i in range(3)]
        else:
            direction = left_axis or right_axis
            point = left_point if left_axis is not None else right_point
        if direction is None or point is None:
            return None
        return {"rows": [_translation_row(direction, point)], "limits": limits, "axis": None, "point": point}
    if kind == "parallel":
        kinds = [_kind(first), _kind(second)]
        if "line" in kinds and "plane" in kinds:
            # A line parallel to a plane is one rotational freedom: the solved state must
            # already show the line direction perpendicular to the plane normal within
            # the module's solved-state tolerance (the 0.05-degree bound belongs to the
            # face-evidence crosscheck only).
            line_entity, plane_entity = (first, second) if kinds[0] == "line" else (second, first)
            _line_point, line_direction = _geometry(line_entity, frames)
            _plane_point, plane_normal = _geometry(plane_entity, frames)
            if line_direction is None or plane_normal is None:
                return None
            if abs(_dot(line_direction, plane_normal)) > TOL:
                return None
            pivot = _unit_vector(_cross(line_direction, plane_normal))
            if pivot is None:
                return None
            return {"rows": [_rotation_row(pivot)], "limits": limits, "axis": None, "point": None}
        if kinds.count("line") == 1:
            # A line paired with a cylinder or point stays outside the supported scope.
            return None
        axis = None
        for entity in (first, second):
            _point, direction = _geometry(entity, frames)
            if isinstance(entity.get("cylinder"), dict) and direction is not None:
                axis = direction
                break
        if axis is None:
            _first_point, first_axis = _geometry(first, frames)
            _second_point, second_axis = _geometry(second, frames)
            if first_axis is None or second_axis is None or abs(abs(_dot(first_axis, second_axis)) - 1.0) > TOL:
                return None
            axis = first_axis
        plane = _plane_basis(axis)
        if len(plane) != 2:
            return None
        return {
            "rows": [_rotation_row(direction) for direction in plane],
            "limits": limits,
            "axis": None,
            "point": None,
        }
    for entity in (first, second):
        if _kind(entity) == "line":
            # Lines stay outside the angle/perpendicular supported scope.
            return None
    _first_point, first_axis = _geometry(first, frames)
    _second_point, second_axis = _geometry(second, frames)
    if first_axis is None or second_axis is None:
        return None
    normal = _unit_vector(_cross(first_axis, second_axis))
    if normal is None:
        return None
    return {"rows": [_rotation_row(normal)], "limits": limits, "axis": None, "point": None}


def _find(nodes: list[str], parent: dict[str, str], node: str) -> str:
    while parent[node] != node:
        node = parent[node]
    return node


def _independent_clusters(record: dict):
    _require_suppression_states(record)
    components = {
        str(item.get("name2")): item
        for item in record.get("components") or []
        if isinstance(item, dict) and isinstance(item.get("name2"), str) and not item.get("suppressed")
    }
    frames = _frames(record)
    pairs: dict[tuple[str, str], dict] = {}
    for index, mate in enumerate(record.get("mates") or []):
        if not isinstance(mate, dict) or mate.get("suppressed"):
            continue
        rows = _rows_for(mate, frames)
        names = _names(mate)
        for left_index in range(len(names)):
            for right_index in range(left_index + 1, len(names)):
                left, right = names[left_index], names[right_index]
                if left not in components or right not in components:
                    continue
                key = tuple(sorted((left, right)))
                group = pairs.setdefault(
                    key, {"rows": [], "mates": [], "unresolved": False, "limits": None, "axis": None, "point": None}
                )
                group["mates"].append(index)
                if rows is None:
                    group["unresolved"] = True
                    continue
                group["rows"].extend(rows["rows"])
                if group["limits"] is None and isinstance(rows.get("limits"), dict):
                    group["limits"] = rows["limits"]
                if group["axis"] is None and isinstance(rows.get("axis"), list):
                    group["axis"] = rows["axis"]
                    group["point"] = rows.get("point")
    for group in pairs.values():
        group["rank"] = len(_span(group["rows"]))
    parent = {name: name for name in components}
    union = [key for key, group in sorted(pairs.items()) if not group["unresolved"] and group["rank"] == 6]
    for left, right in union:
        root_left, root_right = _find(components, parent, left), _find(components, parent, right)
        if root_left != root_right:
            parent[root_right] = root_left
    members: dict[str, list[str]] = {}
    for name in components:
        members.setdefault(_find(components, parent, name), []).append(name)
    return {root: sorted(value) for root, value in members.items()}, pairs


def _owned_datum(record: dict, name, owners) -> dict | None:
    """The single datum of that name owned by one of ``owners``.

    A name that two datums of the same body share is ambiguous, exactly as a name that no
    component of the body owns is unusable: both answer ``None`` so the caller blocks.
    """
    wanted = {str(owner) for owner in owners}
    candidates = [
        datum
        for datum in record.get("datums") or []
        if isinstance(datum, dict) and str(datum.get("name")) == str(name) and str(datum.get("owner") or "") in wanted
    ]
    return candidates[0] if len(candidates) == 1 else None


def _components_by_name(record: dict) -> dict[str, dict]:
    return {
        str(item.get("name2")): item
        for item in record.get("components") or []
        if isinstance(item, dict) and isinstance(item.get("name2"), str)
    }


def _is_container(component: dict | None) -> bool:
    """An assembly-document occurrence is a container, never a physical member."""
    if not isinstance(component, dict):
        return False
    return str(component.get("document") or "").strip().lower().endswith(".sldasm")


def _cluster_bindings(
    record: dict,
) -> tuple[dict[frozenset[str], frozenset[str]], dict[str, frozenset[str]]]:
    """Map each material body to its full rigid cluster, and each bound occurrence to it.

    A container joins a cluster only through the same solved rank-6 evidence as any other
    occurrence, so a datum owned by a flexible sub-assembly's container never becomes a
    descendant body's frame merely because the path is nested below it.
    """
    by_name = _components_by_name(record)
    members, _pairs = _independent_clusters(record)
    by_material: dict[frozenset[str], frozenset[str]] = {}
    by_component: dict[str, frozenset[str]] = {}
    for group in members.values():
        material = frozenset(name for name in group if not _is_container(by_name.get(name)))
        if not material:
            continue
        full = frozenset(group)
        by_material[material] = full
        for name in full:
            by_component[name] = full
    return by_material, by_component


def _frame_attachment(record: dict) -> dict:
    """Re-derive the frozen top assembly's frame attachment from the record alone.

    An assembly-frame entity is ``{component: "", assembly_frame: true}``: geometry of
    the assembly's own reference geometry, recorded in the top assembly frame.  It may
    only appear in an unsuppressed top-scope mate together with at least one occurrence
    endpoint.  The aggregate of every such mate's reconstructed rows must reach rank 6
    and every occurrence endpoint must belong to exactly one rigid cluster; that
    cluster is the only claim the frame attachment can carry.  Nested, incomplete or
    ambiguous attachments are recorded as problems, never guessed.

    A nested self-frame mate (scope ``S`` with its own flagged entity ``component == S``)
    is an ordinary in-scope pair: it is not part of this aggregate.  Problems on such
    nested relations -- or on any mate that is not a well-formed top-scope attachment --
    are scope-isolated diagnostics: they are reported, but only top-scope problems veto
    the frozen-top attachment.
    """

    problems: list[dict] = []
    top_problems: list[dict] = []
    scope_problems: list[dict] = []
    seen = 0
    attached: list[str] = []
    rows_all: list[list[float]] = []
    known = _components_by_name(record)
    frames = _frames(record)

    def problem(target: list[dict], message: str, detail: dict | None = None) -> None:
        item = {"code": "discovery.frame_attachment", "message": message, "detail": detail or {}}
        problems.append(item)
        target.append(item)

    for index, mate in enumerate(record.get("mates") or []):
        if not isinstance(mate, dict) or mate.get("suppressed"):
            continue
        entities = _entities(mate)
        flagged = [entity for entity in entities if entity.get("assembly_frame") is True]
        if not flagged:
            continue
        name = str(mate.get("name") or index)
        scope = mate.get("scope")
        nested = _nested_self_frame_entity(mate)
        if nested is not None:
            others = [entity for entity in entities if entity is not nested]
            endpoint = others[0].get("component") if len(others) == 1 else None
            if scope not in known or not isinstance(endpoint, str) or endpoint not in known:
                problem(
                    scope_problems,
                    "a frame-attached mate outside the frozen top assembly is not supported",
                    {"mate": name, "scope": scope, "endpoint": endpoint, "entities": len(entities)},
                )
            continue
        seen += 1
        if scope != "":
            # Missing, null or numeric scopes are as unsupported as a nested one: the
            # frozen top scope must be exactly the empty string.  Scope-isolated: this
            # never vetoes the frozen-top aggregate.
            problem(
                scope_problems,
                "a frame-attached mate outside the frozen top assembly is not supported",
                {"mate": name, "scope": scope},
            )
            continue
        others = [entity for entity in entities if entity.get("assembly_frame") is not True]
        raw_entities = mate.get("entities")
        if (
            not isinstance(raw_entities, list)
            or len(raw_entities) != 2
            or len(entities) != 2
            or len(flagged) != 1
            or len(others) != 1
        ):
            problem(
                top_problems,
                "a frame-attached mate must carry exactly one frame entity and one occurrence entity",
                {"mate": name, "entities": len(entities)},
            )
            continue
        for entity in flagged:
            if entity.get("component") != "":
                problem(
                    top_problems,
                    "a frame entity must carry the frozen top-scope identity",
                    {"mate": name, "component": entity.get("component")},
                )
        rows = _rows_for(mate, frames)
        if rows is None:
            problem(
                top_problems,
                "a frame-attached mate cannot be reconstructed from its recorded entities",
                {"mate": name, "type": mate.get("type")},
            )
            continue
        rows_all.extend(rows["rows"])
        for entity in others:
            component = entity.get("component")
            if not isinstance(component, str) or not component:
                problem(
                    top_problems,
                    "a frame-attached mate names an unknown occurrence",
                    {"mate": name, "component": component},
                )
                continue
            attached.append(component)
    result = {
        "mates": seen,
        "components": sorted(set(attached)),
        "members": None,
        "rank": None,
        "problems": problems,
        "top_problems": top_problems,
        "scope_problems": scope_problems,
    }
    if top_problems:
        return result
    if not seen:
        result["mates"] = 0
        result["components"] = []
        return result
    unknown = sorted(name for name in set(attached) if name not in known)
    if unknown:
        problem(top_problems, "a frame-attached mate names an unknown occurrence", {"components": unknown})
    members, _pairs = _independent_clusters(record)
    cluster_of = {name: key for key, group in members.items() for name in group}
    keys = sorted({cluster_of[name] for name in set(attached) if name in cluster_of})
    if len(keys) != 1:
        problem(
            top_problems,
            "frame-attached components must belong to exactly one rigid cluster",
            {"components": sorted(set(attached)), "clusters": [sorted(members[key]) for key in keys]},
        )
    rank = len(_span(rows_all))
    if rank != 6:
        problem(
            top_problems,
            "the aggregate frame attachment does not reach full rank; the attachment is incomplete",
            {"rank": rank},
        )
    full = frozenset(members[keys[0]]) if len(keys) == 1 else None
    result.update({"members": full, "rank": rank})
    return result


def _frame_attached_members(record: dict) -> frozenset[str]:
    """Members of the proven top frame cluster; empty when the attachment is not proven.

    Proven means a well-formed top-scope attachment (rank 6, one rigid cluster) whose
    cluster itself binds ``CS_base_link``.  A rank-6 attachment on a moving cluster is
    not the base, so it earns no datum-channel allowance.
    """

    result = _frame_attachment(record)
    if result["top_problems"] or result["members"] is None:
        return frozenset()
    members = frozenset(result["members"])
    if _owned_datum(record, "CS_base_link", _datum_owners(members, members)) is None:
        return frozenset()
    return members


def _datum_owners(full, attached: frozenset[str]) -> set[str]:
    """Owners that may bind a cluster's frame datum.

    A cluster's own members always qualify.  The frozen top assembly's datum channel
    (owner ``""``) qualifies only for the proven frame-attached cluster: that cluster is
    rigidly the assembly frame, so its own datums are equivalent to top-owned ones.
    """

    owners = {str(value) for value in full}
    if attached and owners == set(attached):
        owners.add("")
    return owners


def _frame(values) -> list[list[float]] | None:
    try:
        raw = list(values or ())
    except TypeError:
        return None
    values_as_numbers = [_number(value) for value in raw]
    if len(values_as_numbers) != 16 or any(value is None for value in values_as_numbers):
        return None
    numbers = [value for value in values_as_numbers if value is not None]
    return [numbers[0:4], numbers[4:8], numbers[8:12], numbers[12:16]]


def _local_axis(frame: list[list[float]], axis: list[float]) -> list[float]:
    return [
        frame[0][0] * axis[0] + frame[1][0] * axis[1] + frame[2][0] * axis[2],
        frame[0][1] * axis[0] + frame[1][1] * axis[1] + frame[2][1] * axis[2],
        frame[0][2] * axis[0] + frame[1][2] * axis[1] + frame[2][2] * axis[2],
    ]


def _offset(point, origin, axis) -> float:
    delta = [float(point[index]) - float(origin[index]) for index in range(3)]
    along = sum(delta[index] * axis[index] for index in range(3))
    residual = [delta[index] - along * axis[index] for index in range(3)]
    return math.sqrt(sum(value * value for value in residual))


def _close(left, right, tolerance: float = 1e-9) -> bool:
    try:
        return all(abs(float(one) - float(two)) <= tolerance for one, two in zip(left, right, strict=True))
    except (TypeError, ValueError):
        return False


def _record_value(package: Path, entry: dict, key: str):
    target = package / str(entry.get("package_file") or "")
    _require(
        target.is_file(), "discovery.record", "embedded record file is missing", {"file": entry.get("package_file")}
    )
    payload = target.read_bytes()
    _require(
        hashlib.sha256(payload).hexdigest() == entry.get("sha256"),
        "discovery.record",
        "embedded record bytes changed",
        {"file": entry.get("package_file")},
    )
    text = payload.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = yaml.safe_load(text)
    node = data
    for part in [piece for piece in str(key or "").split(".") if piece]:
        _require(
            isinstance(node, dict) and part in node,
            "discovery.record",
            "embedded record has no such key",
            {"file": entry.get("package_file"), "key": key},
        )
        node = node[part]
    return node


def _primary_name(mates: list[dict]) -> str:
    """Mirror of the documented primary-mate rule: limits, then shaft, then order."""

    for mate in mates:
        if isinstance(mate.get("limits"), dict):
            return str(mate.get("name") or "")
    for mate in mates:
        if any(isinstance(item.get("cylinder"), dict) for item in _entities(mate)):
            return str(mate.get("name") or "")
    return str(mates[0].get("name") or "") if mates else ""


def _joint_properties(raw: dict, mates: list[dict]) -> tuple[dict, dict]:
    """Merged scalars plus every key two native sources declare differently."""

    candidates: dict[str, list[tuple[str, object]]] = {}
    mate_properties = (raw.get("properties") or {}).get("mates") or {}
    document = (raw.get("properties") or {}).get("document") or {}
    names = [str(mate.get("name") or "") for mate in mates]
    for name in names:
        for key, value in (mate_properties.get(name) or {}).items():
            candidates.setdefault(key, []).append((f"mate:{name}", value))
    heads = {name.split("__", 1)[0] for name in names if "__" in name}
    prefixes = [f"dp.joint.{head}." for head in sorted(heads)] + [f"dp.joint.{name}." for name in names if name]
    for key, value in document.items():
        if not isinstance(key, str):
            continue
        for prefix in prefixes:
            if key.startswith(prefix):
                candidates.setdefault(f"dp.joint.{key[len(prefix) :]}", []).append((f"document:{key}", value))
    merged: dict = {}
    conflicts: dict = {}
    for key, values in candidates.items():
        texts = {str(value) for _, value in values}
        if len(texts) > 1:
            conflicts[key] = {"sources": [source for source, _ in values], "values": sorted(texts)}
            continue
        merged[key] = values[0][1]
    return merged, conflicts


def verify_discovery(package: Path) -> dict:
    """Recompute every native-discovery claim from the bound raw record."""

    package = Path(package)
    checks: list[dict] = []
    errors: list[dict] = []
    diagnostics: list[dict] = []
    state: dict = {}

    def check(identifier, callback):
        try:
            details = _report_safe(callback() or {})
            checks.append({"id": identifier, "passed": True, "details": details})
        except _Failure as failure:
            detail = _report_safe(failure.detail)
            if not isinstance(detail, dict):
                detail = {"detail": detail}
            checks.append(
                {
                    "id": identifier,
                    "passed": False,
                    "details": {"code": failure.code, "error": failure.message, **detail},
                }
            )
            errors.append({"code": failure.code, "message": failure.message, "detail": detail})
        except Exception as error:  # noqa: BLE001 - a verifier must not crash on hostile input
            checks.append(
                {
                    "id": identifier,
                    "passed": False,
                    "details": {"code": "discovery.internal", "error": f"{type(error).__name__}: {error}"},
                }
            )
            errors.append({"code": "discovery.internal", "message": str(error), "detail": {}})
        return checks[-1]

    robot_path = package / ROBOT_FILE
    revision_path = package / REVISION_FILE
    record_path = package / DISCOVERY_FILE

    def binding():
        _require(robot_path.is_file(), "discovery.file", "robot.yaml is missing")
        _require(record_path.is_file(), "discovery.file", "the discovery record is missing")
        robot = _read_yaml(robot_path)
        provenance = robot.get("provenance")
        _require(isinstance(provenance, dict), "discovery.binding", "robot.yaml carries no native-discovery provenance")
        _require(
            provenance.get("generator") == "native-discovery",
            "discovery.binding",
            "provenance.generator is not native-discovery",
        )
        for key in ("generator_version", "contract", "run_id"):
            _require(
                isinstance(provenance.get(key), str) and provenance[key].strip(),
                "discovery.binding",
                f"provenance.{key} is missing",
            )
        for key in ("discovery_sha256", "native_inventory_sha256"):
            _require(
                isinstance(provenance.get(key), str) and _HEX.match(provenance[key]),
                "discovery.binding",
                f"provenance.{key} is not a digest",
            )
        payload_bytes = record_path.read_bytes()
        _require(
            hashlib.sha256(payload_bytes).hexdigest() == provenance["discovery_sha256"],
            "discovery.binding",
            "the discovery record does not match provenance.discovery_sha256",
        )
        payload = json.loads(payload_bytes.decode("utf-8"))
        _require(payload.get("schema_version") == DISCOVERY_SCHEMA, "discovery.binding", "unexpected discovery schema")
        _require(
            payload.get("contract") == provenance.get("contract"),
            "discovery.binding",
            "record contract differs from provenance",
        )
        _require(
            payload.get("run_id") == provenance.get("run_id"),
            "discovery.binding",
            "record run_id differs from provenance",
        )
        native_files = payload.get("native_files")
        _require(
            isinstance(native_files, dict) and native_files,
            "discovery.binding",
            "record carries no native file inventory",
        )
        # The inventory digest mirrors the pipeline's canonical JSON encoding
        # (sorted keys, UTF-8, two-space indent, trailing newline).
        canonical = hashlib.sha256(
            (json.dumps(native_files, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
        ).hexdigest()
        _require(
            canonical == provenance["native_inventory_sha256"],
            "discovery.binding",
            "the native inventory digest does not match provenance",
        )
        _require(robot.get("schema_version") == INPUT_SCHEMA, "discovery.binding", "robot.yaml schema changed")
        state["robot"] = robot
        state["payload"] = payload
        state["provenance"] = provenance
        return {
            "discovery_sha256": provenance["discovery_sha256"],
            "native_inventory_sha256": provenance["native_inventory_sha256"],
            "run_id": provenance["run_id"],
            "contract": provenance.get("contract"),
        }

    def files():
        payload = state["payload"]
        inventory = _digest_files(package)
        native = payload["native_files"]
        for relative, checksum in sorted(native.items()):
            _require(
                relative in inventory,
                "discovery.files",
                "a recorded native file is missing from the package",
                {"file": relative},
            )
            _require(
                inventory[relative] == checksum,
                "discovery.files",
                "a native file differs from the recorded bytes",
                {"file": relative},
            )
        declared = {name for name in native if Path(name).suffix.lower() in {".sldasm", ".sldprt"}}
        observed = {name for name in inventory if Path(name).suffix.lower() in {".sldasm", ".sldprt"}}
        _require(
            declared == observed,
            "discovery.files",
            "the package contains CAD documents the record never read",
            {"extra": sorted(observed - declared)},
        )
        raw = payload.get("raw") or {}
        for relative, checksum in sorted((raw.get("files") or {}).items()):
            _require(
                inventory.get(relative) == checksum,
                "discovery.files",
                "a raw-recorded document does not match its package bytes",
                {"file": relative},
            )
        return {"native_files": len(native), "cad_files": len(observed)}

    def imported_sources():
        """Every recorded imported occurrence must bind to a frozen native source.

        Two classes are accepted.  A resolved native imported document whose
        GetImportedPath names the captured document inside the frozen root binds
        directly.  A cached native materialization (a session temp document whose author
        path is metadata) is accepted only through the mandatory sealed selection
        carrier: exact owner, owner bytes, occurrence identity, session-temp
        materialization inside the owned session temp area, author metadata and current
        native suppression/virtual readings must all match one recorded cached
        materialization, and every recorded cached materialization must be proven by a
        current occurrence.  Nothing is guessed from raw paths, names or suffixes, and a
        present external file is never downgraded; everything else yields a precise
        discovery.imported_source_unbound failure.
        """

        payload = state["payload"]
        raw = payload.get("raw") or {}
        native = payload.get("native_files") or {}
        raw_files = raw.get("files") or {}
        carrier = raw.get("source_selection")
        closure = carrier.get("closure") if isinstance(carrier, dict) else None
        cached_slots: list[dict] = []
        if isinstance(closure, dict) and isinstance(closure.get("imports"), list):
            cached_slots = [
                item
                for item in closure["imports"]
                if isinstance(item, dict) and item.get("state") == "cached_materialization"
            ]
        cached_used: set[int] = set()
        unbound: list[dict] = []
        observed = 0
        for component in raw.get("components") or []:
            if not isinstance(component, dict):
                continue
            provenance = component.get("source_provenance")
            if provenance is None:
                continue
            observed += 1
            document = component.get("document")
            observation = component.get("solver_observation")
            context = observation.get("context") if isinstance(observation, dict) else None
            entry = {
                "occurrence": str(component.get("name2") or ""),
                "document": document,
                "containing_document": context.get("document") if isinstance(context, dict) else None,
                "native_path": provenance.get("native_document_path") if isinstance(provenance, dict) else None,
                "imported_path": provenance.get("imported_path") if isinstance(provenance, dict) else None,
            }
            if not isinstance(provenance, dict):
                unbound.append({**entry, "state": None, "reason": "imported provenance is not an object"})
                continue
            if provenance.get("state") == "cached_materialization":
                reason = _cached_materialization_reason(
                    component, provenance, cached_slots, cached_used, carrier, native
                )
                if reason is not None:
                    unbound.append(
                        {**entry, "state": provenance.get("state"), "reason": reason, "error": provenance.get("error")}
                    )
                continue
            reason = None
            if provenance.get("state") != "resolved":
                reason = f"imported source state is {provenance.get('state') or 'unread'}"
            elif provenance.get("kind") != "native_imported_document":
                reason = "imported provenance is not a native imported document"
            elif (
                provenance.get("used_api")
                != "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual"
            ):
                reason = "imported provenance used an unexpected native API"
            elif provenance.get("document") != document:
                reason = "imported provenance document differs from the occurrence document"
            elif (
                not isinstance(document, str)
                or not document
                or PurePosixPath(document).is_absolute()
                or PurePosixPath(document).as_posix() != document
                or ".." in PurePosixPath(document).parts
                or "\\" in document
                or ":" in document
            ):
                reason = "the occurrence document is not a canonical confined package path"
            elif provenance.get("is_virtual") is not False:
                reason = (
                    "virtual imported documents are outside the handoff contract"
                    if provenance.get("is_virtual") is True
                    else "the imported virtual state is unread"
                )
            else:
                sha = provenance.get("sha256")
                recorded = raw_files.get(document)
                if not (
                    isinstance(sha, str)
                    and _HEX.match(sha)
                    and native.get(document) == sha
                    and (recorded is None or recorded == sha)
                ):
                    reason = "imported bytes are not bound to the frozen native inventory"
                else:
                    origin, root = provenance.get("imported_path"), provenance.get("source_root")
                    if not (
                        isinstance(origin, str)
                        and ntpath.isabs(origin)
                        and isinstance(root, str)
                        and ntpath.isabs(root)
                    ):
                        reason = "the imported-source confinement evidence is incomplete"
                    else:
                        try:
                            relative = ntpath.relpath(origin, root)
                        except ValueError:
                            reason = "imported path and frozen root are on different drives"
                        else:
                            if (
                                relative == ".."
                                or relative.startswith("..\\")
                                or ntpath.normcase(relative) != ntpath.normcase(document.replace("/", "\\"))
                            ):
                                reason = "GetImportedPath does not name the captured source inside the frozen root"
            if reason is not None:
                unbound.append(
                    {
                        **entry,
                        "state": provenance.get("state"),
                        "reason": reason,
                        "error": provenance.get("error"),
                    }
                )
        if unbound:
            _require(
                False,
                "discovery.imported_source_unbound",
                "recorded imported occurrences are not bound to frozen native sources",
                {
                    "unbound": len(unbound),
                    "observations": observed,
                    "occurrences": [item["occurrence"] for item in unbound],
                    "imported_paths": sorted({str(item["imported_path"]) for item in unbound if item["imported_path"]}),
                    "samples": unbound[:20],
                },
            )
        unmatched = [index for index in range(len(cached_slots)) if index not in cached_used]
        if unmatched:
            _require(
                False,
                "discovery.imported_source_unbound",
                "the sealed carrier records cached materializations that no current occurrence proves",
                {
                    "unmatched": [cached_slots[index] for index in unmatched[:20]],
                    "unmatched_count": len(unmatched),
                },
            )
        return {"imported_occurrences": observed, "cached_materializations": len(cached_used)}

    def identity():
        payload, robot = state["payload"], state["robot"]
        authored = _authored_identity(payload.get("raw") or {}, str(payload.get("namespace") or "dp"))
        state["authored_identity"] = authored
        claimed = payload.get("identity")
        _require(isinstance(claimed, dict), "discovery.identity", "generated identity claims are absent")
        for key in ("hardware_id", "revision", "parent_revision", "owner", "change_summary", "control"):
            _require(
                claimed.get(key) == authored[key],
                "discovery.identity",
                "generated metadata differs from native evidence",
                {"field": key},
            )
        _require(
            robot.get("hardware_id") == authored["hardware_id"],
            "discovery.identity",
            "robot.yaml hardware_id differs from authored native metadata",
        )
        technical_name = authored["robot_name"]
        label_source = "authored_robot_name"
        if technical_name is None:
            label_source = "hardware_id" if authored["hardware_id"] is not None else "technical_default"
            technical_name = re.sub(r"[^0-9A-Za-z]+", "_", authored["hardware_id"] or "robot").strip("_").lower()
        _require(
            (robot.get("source") or {}).get("robot_name") == technical_name,
            "discovery.identity",
            "generated robot label differs from its recorded naming source",
        )
        if authored["missing"]:
            diagnostics.append(
                {
                    "code": "discovery.publication_metadata_incomplete",
                    "blocking": False,
                    "message": "engineering evidence is inspectable; authored publication metadata is incomplete",
                    "detail": {"missing": authored["missing"]},
                }
            )
        return {
            "missing_publication_fields": authored["missing"],
            "technical_label": technical_name,
            "label_source": label_source,
        }

    def revision():
        robot = state["robot"]
        _require(not revision_path.is_symlink(), "discovery.revision", "cad-revision.json must be a regular file")
        _require(revision_path.is_file(), "discovery.revision", "cad-revision.json is missing")
        revision = _read_json(revision_path)
        fields = {
            "schema_version",
            "hardware_id",
            "revision",
            "parent_revision",
            "owner",
            "change_summary",
            "control",
            "cad_files",
        }
        _require(
            isinstance(revision, dict)
            and not set(revision) - fields
            and revision.get("schema_version") == "solidworks-to-urdf.cad-revision/v1",
            "discovery.revision",
            "the supplied revision manifest is malformed",
        )
        authored = state.get("authored_identity")
        _require(
            authored is not None and not authored["missing"],
            "discovery.revision",
            "the revision manifest lacks complete native author metadata",
        )
        for key in ("hardware_id", "revision", "parent_revision", "owner", "change_summary", "control"):
            _require(
                revision.get(key) == authored[key],
                "discovery.revision",
                "revision metadata differs from native author metadata",
                {"field": key},
            )
        _require(
            revision.get("hardware_id") == robot.get("hardware_id"),
            "discovery.revision",
            "revision hardware_id differs from robot.yaml",
        )
        inventory = _digest_files(package)
        observed = {
            name: checksum
            for name, checksum in inventory.items()
            if Path(name).suffix.lower() in {".sldasm", ".sldprt"}
        }
        _require(
            revision.get("cad_files") == observed,
            "discovery.revision",
            "the revision does not seal the package CAD bytes",
        )
        return {"cad_files": len(observed)}

    def budgets():
        robot = state["robot"]
        payload = state["payload"]
        entry = payload.get("budget")
        _require(isinstance(entry, dict), "discovery.budgets", "the package carries no design budget record binding")
        checks = robot.get("checks")
        _require(isinstance(checks, dict), "discovery.budgets", "robot.yaml carries no checks block")
        value = _record_value(
            package,
            {"package_file": entry.get("package_file"), "sha256": entry.get("sha256")},
            str(entry.get("key") or ""),
        )
        _require(isinstance(value, dict), "discovery.budgets", "the design budget record entry is not an object")
        for key in ("expected_mass_kg", "expected_extent_m"):
            item = value.get(key)
            declared = checks.get(key)
            _require(
                isinstance(item, (list, tuple))
                and isinstance(declared, (list, tuple))
                and len(item) == len(declared) == 2
                and all(_number(number) is not None for number in (*item, *declared))
                and 0 <= item[0] < item[1]
                and 0 <= declared[0] < declared[1]
                and _close([float(declared[0]), float(declared[1])], [float(item[0]), float(item[1])], 1e-12),
                "discovery.budgets",
                f"the {key} capture window differs from the design budget record",
                {"value": declared},
            )
        return {"budget_file": entry.get("package_file")}

    def graph():
        payload = state["payload"]
        raw = payload.get("raw")
        _require(isinstance(raw, dict), "discovery.graph", "the raw record is missing")
        components = raw.get("components")
        _require(isinstance(components, list) and components, "discovery.graph", "the record lists no components")
        component_names = [str(item.get("name2") or "") for item in components if isinstance(item, dict)]
        _require(
            all(component_names) and len(component_names) == len(set(component_names)),
            "discovery.graph",
            "component occurrence names must be unique and non-empty",
            {
                "duplicates": sorted({name for name in component_names if component_names.count(name) > 1}),
                "empty": sum(1 for name in component_names if not name),
            },
        )
        names = {str(item.get("name2")) for item in components if isinstance(item, dict)}
        frames = _frames(raw)
        for mate in raw.get("mates") or []:
            _require(isinstance(mate, dict), "discovery.graph", "a mate entry is not an object")
            suppressed = mate.get("suppressed")
            _require(
                isinstance(suppressed, bool),
                "discovery.graph",
                "a mate suppression flag is not a boolean",
                {"mate": mate.get("name"), "suppressed": suppressed},
            )
            if suppressed:
                continue
            error_code = mate.get("error_code")
            _require(
                isinstance(error_code, int) and not isinstance(error_code, bool) and error_code == 0,
                "discovery.graph",
                "the saved mate reports a native error or an unreadable solve state",
                {"mate": mate.get("name"), "error_code": error_code},
            )
            kind = str(mate.get("type") or "").strip().lower()
            limits = mate.get("limits")
            if kind in {"limitdistance", "limitangle"}:
                unit = "m" if kind == "limitdistance" else "rad"
                _require(
                    _valid_limits(limits) and limits.get("unit") == unit,
                    "discovery.graph",
                    f"a {kind} mate must carry finite lower < upper bounds in {unit}",
                    {"mate": mate.get("name"), "limits": limits},
                )
            elif limits is not None:
                _require(
                    _valid_limits(limits),
                    "discovery.graph",
                    "a bounded mate limit is not a finite ordered range with a unit",
                    {"mate": mate.get("name"), "limits": limits},
                )
            for entity in _entities(mate):
                frame_flag = entity.get("assembly_frame")
                _require(
                    frame_flag is None or frame_flag is True,
                    "discovery.graph",
                    "an assembly-frame flag must be exactly true when present",
                    {"mate": mate.get("name"), "assembly_frame": frame_flag},
                )
                component = entity.get("component")
                _require(
                    (isinstance(component, str) and component in names) or (frame_flag is True and component == ""),
                    "discovery.graph",
                    "a mate entity names an unknown component",
                    {"component": entity.get("component")},
                )
                cylinder = entity.get("cylinder")
                if isinstance(cylinder, dict):
                    _require(
                        _finite_triple(cylinder.get("point")) is not None,
                        "discovery.graph",
                        "a cylinder point is not finite",
                        {"mate": mate.get("name"), "component": entity.get("component")},
                    )
                    _require(
                        _unit_vector(cylinder.get("direction")) is not None,
                        "discovery.graph",
                        "a cylinder axis is not usable",
                    )
                    radius = cylinder.get("radius")
                    _require(
                        isinstance(radius, (int, float))
                        and not isinstance(radius, bool)
                        and math.isfinite(float(radius))
                        and float(radius) > 0,
                        "discovery.graph",
                        "a cylinder radius is not a finite positive number",
                    )
                plane = entity.get("plane")
                if isinstance(plane, dict):
                    _require(
                        _finite_triple(plane.get("point")) is not None
                        and _unit_vector(plane.get("normal")) is not None,
                        "discovery.graph",
                        "a plane entity is not a finite point and normal",
                    )
                if isinstance(entity.get("point"), (list, tuple)):
                    _require(
                        _finite_triple(entity.get("point")) is not None,
                        "discovery.graph",
                        "a point entity is not a finite point",
                    )
                circle = entity.get("circle")
                if isinstance(circle, dict):
                    radius = circle.get("radius")
                    _require(
                        _finite_triple(circle.get("center")) is not None
                        and _finite_triple(circle.get("normal")) is not None
                        and _unit_vector(circle.get("normal")) is not None
                        and _finite_limit(radius)
                        and float(radius) > 0,
                        "discovery.graph",
                        "a circle entity is not a finite circle with a usable normal",
                        {"mate": mate.get("name"), "component": entity.get("component")},
                    )
            _require(
                _rows_for(mate, frames) is not None,
                "discovery.graph",
                "a mate is outside the supported constraint scope or lacks usable entities",
                {"type": mate.get("type"), "name": mate.get("name")},
            )
            if str(mate.get("type") or "").strip().lower() == "concentric":
                geometry = [
                    _geometry(entity, frames) for entity in _entities(mate) if isinstance(entity.get("cylinder"), dict)
                ]
                if len(geometry) == 2 and geometry[0][1] is not None and geometry[1][1] is not None:
                    left_point, left_axis = geometry[0]
                    right_point, right_axis = geometry[1]
                    _require(
                        abs(abs(_dot(left_axis, right_axis)) - 1.0) <= TOL,
                        "discovery.graph",
                        "concentric mate cylinders are not parallel",
                        {"mate": mate.get("name")},
                    )
                    delta = [right_point[index] - left_point[index] for index in range(3)]
                    along = _dot(delta, left_axis)
                    radial = [delta[index] - along * left_axis[index] for index in range(3)]
                    gap = math.sqrt(_dot(radial, radial))
                    _require(
                        gap <= AXIS_OFFSET_TOL_M,
                        "discovery.graph",
                        "concentric mate cylinders are radially displaced",
                        {"mate": mate.get("name"), "radial_gap_m": gap},
                    )
        for datum in raw.get("datums") or []:
            _require(
                isinstance(datum, dict) and _frame(datum.get("array")) is not None,
                "discovery.graph",
                "a datum transform is not a 4x4 frame",
            )
            owner = datum.get("owner")
            _require(
                isinstance(owner, str) and (owner == "" or owner in names),
                "discovery.graph",
                "a datum owner must be a known occurrence name or the frozen top assembly",
                {"datum": datum.get("name"), "owner": owner},
            )
        component_properties = (raw.get("properties") or {}).get("components") or {}
        for name, values in component_properties.items():
            marker = values.get("dp.body_marker") if isinstance(values, dict) else None
            _require(
                not (isinstance(marker, str) and marker.strip()),
                "discovery.graph",
                "authored body markers are not an accepted membership channel",
                {"component": name},
            )
        return {"components": len(names), "mates": len(raw.get("mates") or []), "datums": len(raw.get("datums") or [])}

    def frame_attachment():
        raw = state["payload"]["raw"]
        result = _frame_attachment(raw)
        for item in result["problems"]:
            _require(False, item["code"], item["message"], item["detail"])
        members = result["members"]
        if members is None:
            return {"mates": result["mates"]}
        by_material, _by_component = _cluster_bindings(raw)
        material = next((group for group, full in by_material.items() if set(full) == set(members)), None)
        _require(
            material is not None,
            "discovery.frame_attachment",
            "the frame-attached cluster has no material body",
            {"components": sorted(members)},
        )
        source_bodies = (state["robot"].get("source") or {}).get("bodies") or []
        body = next(
            (
                item
                for item in source_bodies
                if {str(value) for value in (item.get("components") or [])} == set(material)
            ),
            None,
        )
        _require(
            body is not None,
            "discovery.frame_attachment",
            "the frame-attached cluster has no published body",
            {"components": sorted(material)},
        )
        datum = str((body.get("frame") or {}).get("coordinate_system") or "")
        _require(
            datum == "CS_base_link",
            "discovery.frame_attachment",
            "the frame-attached cluster must become the CS_base_link body",
            {"body": body.get("name"), "datum": datum},
        )
        return {
            "mates": result["mates"],
            "components": result["components"],
            "rank": result["rank"],
            "body": body.get("name"),
        }

    def bodies():
        robot = state["robot"]
        raw = state["payload"]["raw"]
        attached = _frame_attached_members(raw)
        by_name = _components_by_name(raw)
        source_bodies = (robot.get("source") or {}).get("bodies")
        _require(isinstance(source_bodies, list) and source_bodies, "discovery.bodies", "robot.yaml declares no bodies")
        # A body is a set of physical parts.  Assembly-document occurrences stay in the mate
        # graph as connectors but are never material members, so a rigid sub-assembly cannot
        # become a body of its own or double the material of its parts.
        full_by_material, _by_component = _cluster_bindings(raw)
        expected = set(full_by_material)
        observed: set[frozenset[str]] = set()
        for body in source_bodies:
            listed = [str(item) for item in (body.get("components") or [])]
            unknown = sorted(name for name in listed if name not in by_name)
            _require(not unknown, "discovery.bodies", "a body names an unknown component", {"components": unknown})
            containers = sorted(name for name in listed if _is_container(by_name.get(name)))
            _require(
                not containers,
                "discovery.bodies",
                "a body lists an assembly container as a material member",
                {"body": body.get("name"), "containers": containers},
            )
            observed.add(frozenset(listed))
        _require(
            observed == expected,
            "discovery.bodies",
            "body membership differs from the independent re-derivation",
            {
                "missing": sorted(sorted(group) for group in expected - observed),
                "extra": sorted(sorted(group) for group in observed - expected),
            },
        )
        seen: set[str] = set()
        for body in source_bodies:
            name = str(body.get("name"))
            _require(_SNAKE.match(name), "discovery.bodies", "a body name is not snake_case", {"name": name})
            _require(name not in seen, "discovery.bodies", "a body name repeats", {"name": name})
            seen.add(name)
            frame = body.get("frame")
            _require(
                isinstance(frame, dict)
                and isinstance(frame.get("coordinate_system"), str)
                and frame["coordinate_system"].strip(),
                "discovery.bodies",
                "a body has no native datum binding",
                {"body": name},
            )
            _require(
                _owned_datum(
                    raw,
                    frame["coordinate_system"],
                    _datum_owners(full_by_material.get(frozenset(body.get("components") or []), frozenset()), attached),
                )
                is not None,
                "discovery.bodies",
                "a body frame names no datum owned uniquely by that body",
                {"body": name, "datum": frame["coordinate_system"]},
            )
        _require("base_link" in seen, "discovery.bodies", "no base_link body")
        return {"bodies": len(source_bodies)}

    def masses():
        raw = state["payload"]["raw"]
        by_name = _components_by_name(raw)
        entries = raw.get("masses") or []
        _require(isinstance(entries, list), "discovery.masses", "the mass inventory is not a list")
        seen: set[str] = set()
        containers: list[str] = []
        material: set[str] = set()
        for entry in entries:
            _require(isinstance(entry, dict), "discovery.masses", "a mass entry is not an object")
            component = str(entry.get("component") or "")
            _require(
                component in by_name,
                "discovery.masses",
                "a mass entry names an unknown component",
                {"component": component},
            )
            _require(
                component not in seen,
                "discovery.masses",
                "a component appears twice in the mass inventory",
                {"component": component},
            )
            seen.add(component)
            _require(
                _finite_limit(entry.get("mass_kg")) and float(entry["mass_kg"]) > 0,
                "discovery.masses",
                "a recorded component mass is not finite and positive",
                {"component": component},
            )
            if _is_container(by_name[component]):
                containers.append(component)
            else:
                material.add(component)
        for container in containers:
            duplicated = sorted(name for name in material if name.startswith(container + "/"))
            _require(
                not duplicated,
                "discovery.masses",
                "an assembly container mass duplicates its material parts",
                {"container": container, "parts": duplicated[:8]},
            )
        return {"components": len(seen), "containers": len(containers)}

    def names():
        robot = state["robot"]
        payload = state["payload"]
        raw = payload["raw"]
        attached = _frame_attached_members(raw)
        frozen = payload.get("frozen_names") or {}
        _require(isinstance(frozen, dict), "discovery.names", "frozen_names must be an object")
        source = robot.get("source") or {}
        bodies = source.get("bodies") or []
        joints = source.get("joints") or []
        _members, pairs = _independent_clusters(raw)
        material_clusters, _by_component = _cluster_bindings(raw)
        components = {str(item.get("name2")): item for item in raw.get("components") or [] if isinstance(item, dict)}
        datums = [item for item in raw.get("datums") or [] if isinstance(item, dict)]
        identities: dict[str, str] = {}
        for group, full in sorted(material_clusters.items()):
            body = next(
                (item for item in bodies if {str(value) for value in (item.get("components") or [])} == set(group)),
                None,
            )
            _require(
                body is not None,
                "discovery.names",
                "a rigid body has no robot.yaml body",
                {"components": sorted(group)},
            )
            name = str(body.get("name"))
            datum_name = str((body.get("frame") or {}).get("coordinate_system"))
            owners = _datum_owners(full, attached)
            owned = [
                item
                for item in datums
                if str(item.get("owner") or "") in owners and str(item.get("name")) == datum_name
            ]
            _require(
                owned,
                "discovery.names",
                "a body frame datum is not owned by its components",
                {"body": name, "datum": datum_name},
            )
            _require(
                datum_name == f"CS_{name}",
                "discovery.names",
                "a frozen name must still match its native CS_<link> datum",
                {"body": name, "datum": datum_name},
            )
            first = sorted(group)[0]
            item = components.get(first, {})
            identity = str(item.get("instance_id") or f"{item.get('document') or ''}#{first}")
            identities[identity] = name
            if identity in frozen:
                _require(
                    str(frozen[identity]) == name,
                    "discovery.names",
                    "a frozen published name was not preserved",
                    {"identity": identity, "expected": frozen[identity], "observed": name},
                )
            else:
                _require(
                    datum_name == f"CS_{name}",
                    "discovery.names",
                    "the body datum must be CS_<link>",
                    {"body": name, "datum": datum_name},
                )
                components_properties = (raw.get("properties") or {}).get("components") or {}
                explicit = {(components_properties.get(component) or {}).get("dp.body_datum") for component in full}
                if datum_name in explicit:
                    _require(
                        _owned_datum(raw, datum_name, _datum_owners(full, attached)) is not None,
                        "discovery.names",
                        "body_datum names a datum no component of this body owns uniquely",
                        {"body": name, "datum": datum_name},
                    )
                else:
                    _require(
                        str(datum_name) == f"CS_{name}",
                        "discovery.names",
                        "the body datum must be CS_<link>",
                        {"body": name, "datum": datum_name},
                    )
        raw_mates = raw.get("mates") or []
        joint_identities: dict[str, str] = {}
        for _key, group in sorted(pairs.items()):
            if group["unresolved"] or group["rank"] == 6:
                continue
            mates = [raw_mates[index] for index in group["mates"] if 0 <= index < len(raw_mates)]
            _properties, conflicts = _joint_properties(raw, mates)
            _require(
                not conflicts,
                "discovery.names",
                "two native sources declare the same joint scalar differently",
                {"conflicts": conflicts},
            )
            heads = []
            for mate in mates:
                head, separator, role = str(mate.get("name") or "").partition("__")
                _require(
                    bool(separator) and bool(role) and _SNAKE.match(head) is not None,
                    "discovery.names",
                    "mate names must form <joint>__<role>",
                    {"mate": mate.get("name")},
                )
                heads.append(head)
            _require(
                len(set(heads)) == 1,
                "discovery.names",
                "the mates of one pair carry different group names",
                {"mates": [m.get("name") for m in mates]},
            )
            name = heads[0]
            joint_identities[str(mates[0].get("name") or "")] = name
            matching = [joint for joint in joints if str(joint.get("name")) == name]
            _require(matching, "discovery.names", "a named mate group has no joint", {"group": name})
        for identity, published in sorted(frozen.items()):
            observed = identities.get(identity) or joint_identities.get(identity)
            _require(
                observed is not None,
                "discovery.names",
                "a frozen identity matches no body or joint",
                {"identity": identity},
            )
            _require(
                str(published) == observed,
                "discovery.names",
                "a frozen published name was not preserved",
                {"identity": identity, "expected": published, "observed": observed},
            )
        return {"frozen": len(frozen)}

    def joints():
        robot = state["robot"]
        payload = state["payload"]
        raw = payload["raw"]
        attached = _frame_attached_members(raw)
        source = robot.get("source") or {}
        source_joints = source.get("joints") or []
        bodies = {
            frozenset(str(value) for value in (body.get("components") or [])): body
            for body in source.get("bodies") or []
        }
        by_material, _by_component = _cluster_bindings(raw)
        component_body: dict[str, str] = {}
        cluster_of_body: dict[str, frozenset[str]] = {}
        for material, full in by_material.items():
            body = bodies.get(material)
            _require(
                body is not None,
                "discovery.joints",
                "a rigid body has no robot.yaml body",
                {"components": sorted(material)},
            )
            name = str(body.get("name"))
            cluster_of_body[name] = full
            for component in full:
                component_body[component] = name
        _members, pairs = _independent_clusters(raw)
        raw_mates = raw.get("mates") or []
        seen: set[frozenset] = set()
        derived = 0
        for key, group in sorted(pairs.items()):
            _require(
                not group["unresolved"],
                "discovery.joints",
                "a mate pair could not be reconstructed",
                {"components": list(key)},
            )
            if group["rank"] == 6:
                continue
            derived += 1
            left, right = key
            _require(
                left in component_body and right in component_body,
                "discovery.joints",
                "a movable pair has no bodies",
                {"components": list(key)},
            )
            pair = frozenset((component_body[left], component_body[right]))
            matching = [
                joint
                for joint in source_joints
                if frozenset((str(joint.get("parent")), str(joint.get("child")))) == pair
            ]
            _require(
                len(matching) == 1,
                "discovery.joints",
                "a movable mate pair does not map to exactly one joint",
                {
                    "components": list(key),
                    "joints": [joint.get("name") for joint in matching],
                },
            )
            joint = matching[0]
            seen.add(pair)
            mates = [raw_mates[index] for index in group["mates"] if 0 <= index < len(raw_mates)]
            properties, conflicts = _joint_properties(raw, mates)
            _require(
                not conflicts,
                "discovery.joints",
                "two native sources declare the same joint scalar differently",
                {"conflicts": conflicts},
            )
            hint = str(properties.get("dp.joint.type") or "").strip().lower()
            nullity = 6 - group["rank"]
            _require(
                nullity == 1,
                "discovery.joints",
                "the mate pair does not leave exactly one motion",
                {
                    "rank": group["rank"],
                    "nullity": nullity,
                    "components": list(key),
                },
            )
            twist = _null_space(group["rows"], 6)[0]
            velocity, omega = twist[:3], twist[3:]
            w = math.sqrt(_dot(omega, omega))
            v = math.sqrt(_dot(velocity, velocity))
            if w <= 1e-9:
                expected_type = "prismatic"
                direction = [value / v for value in velocity]
                axis_point = None
            else:
                _require(
                    not (v > 1e-9 and abs(_dot(velocity, omega)) > 1e-6 * max(v, w) ** 2),
                    "discovery.joints",
                    "the reconstructed motion is a screw, not a supported joint",
                )
                expected_type = "continuous" if hint == "continuous" else "revolute"
                direction = [value / w for value in omega]
                axis_point = [value / (w * w) for value in _cross(omega, velocity)]
            _require(
                hint in {"", expected_type},
                "discovery.joints",
                "the joint type annotation contradicts the reconstructed freedom",
                {"annotation": hint, "derived": expected_type},
            )
            _require(
                str(joint.get("type")) == expected_type,
                "discovery.joints",
                "the joint type differs from the reconstructed freedom",
                {"derived": expected_type, "type": joint.get("type")},
            )
            shaft = group["axis"]
            _require(
                shaft is not None,
                "discovery.joints",
                "no cylindrical mate entity carries the joint shaft",
                {"joint": joint.get("name")},
            )
            _require(
                abs(abs(_dot(shaft, direction)) - 1.0) <= 1e-4,
                "discovery.joints",
                "the recorded shaft does not follow the reconstructed freedom",
                {"joint": joint.get("name")},
            )
            if axis_point is not None and group.get("point") is not None:
                offset = _offset(group["point"], axis_point, direction)
                _require(
                    offset <= AXIS_OFFSET_TOL_M,
                    "discovery.joints",
                    "the recorded shaft misses the reconstructed axis",
                    {"joint": joint.get("name"), "offset_m": offset},
                )
            child = next(body for body in source["bodies"] if str(body.get("name")) == str(joint.get("child")))
            child_datum = _owned_datum(
                raw,
                child["frame"]["coordinate_system"],
                _datum_owners(cluster_of_body.get(str(child.get("name")), frozenset()), attached),
            )
            _require(
                child_datum is not None,
                "discovery.joints",
                "the child body frame names no datum owned uniquely by that body",
                {"body": child.get("name"), "datum": child["frame"]["coordinate_system"]},
            )
            frame = _frame(child_datum.get("array"))
            sign_value = properties.get("dp.joint.axis_sign")
            _require(
                (type(sign_value) is int and sign_value in (1, -1))
                or (type(sign_value) is str and sign_value in ("+1", "1", "-1")),
                "discovery.joints",
                "dp.joint.axis_sign must be exactly +1 or -1",
                {"value": sign_value},
            )
            native = [float(value) for value in shaft]
            if sign_value in (-1, "-1"):
                native = [-value for value in native]
            local = _local_axis(frame, native)
            norm = math.sqrt(sum(value * value for value in local))
            _require(norm > 0, "discovery.joints", "the child frame collapses the joint axis")
            local = [value / norm for value in local]
            authored = joint.get("axis")
            _require(
                isinstance(authored, list) and len(authored) == 3 and _close(authored, local, 1e-9),
                "discovery.joints",
                "the authored axis is not the native shaft in the child frame",
                {"joint": joint.get("name"), "expected": local, "authored": authored},
            )
            reference = joint.get("axis_reference")
            _require(
                isinstance(reference, dict),
                "discovery.joints",
                "a movable joint has no structured shaft reference",
                {"joint": joint.get("name")},
            )
            selector = None
            for mate in mates:
                for entity in _entities(mate):
                    if str(entity.get("component")) != str(reference.get("component")) or not isinstance(
                        entity.get("cylinder"), dict
                    ):
                        continue
                    if reference.get("feature_name"):
                        if str(entity.get("feature")) == str(reference["feature_name"]):
                            selector = entity
                    elif entity.get("face_index") == reference.get("face_index"):
                        selector = entity
            _require(
                selector is not None,
                "discovery.joints",
                "the shaft selector does not match a mate entity",
                {"joint": joint.get("name")},
            )
            limits = joint.get("limits")
            _require(
                isinstance(limits, dict),
                "discovery.joints",
                "a movable joint has no limits",
                {"joint": joint.get("name")},
            )
            if expected_type == "continuous":
                _require(
                    "lower" not in limits and "upper" not in limits,
                    "discovery.joints",
                    "a continuous joint carries position bounds",
                )
                _require(group["limits"] is None, "discovery.joints", "a continuous joint has native position limits")
            elif isinstance(group["limits"], dict):
                unit = group["limits"].get("unit")
                _require(
                    unit == ("m" if expected_type == "prismatic" else "rad"),
                    "discovery.joints",
                    "native limits are not in SI units",
                    {"unit": unit, "type": expected_type},
                )
                _require(
                    _close(
                        [limits.get("lower"), limits.get("upper")],
                        [group["limits"].get("lower"), group["limits"].get("upper")],
                        1e-12,
                    ),
                    "discovery.joints",
                    "the joint limits differ from the native mate",
                    {"joint": joint.get("name")},
                )
                native_record = properties.get("dp.joint.limits_record")
                if isinstance(native_record, str) and native_record.strip():
                    entry = _find_record_entry(payload, native_record)
                    value = _record_value(package, entry["entry"], entry["key"])
                    _require(
                        _close(
                            [limits.get("lower"), limits.get("upper")],
                            [value.get("lower"), value.get("upper")],
                            1e-12,
                        ),
                        "discovery.joints",
                        "native mate limits and the controlled record disagree",
                        {"joint": joint.get("name")},
                    )
            else:
                reference_record = properties.get("dp.joint.limits_record")
                _require(
                    isinstance(reference_record, str) and reference_record.strip(),
                    "discovery.joints",
                    "the joint has no native or controlled position range",
                    {"joint": joint.get("name")},
                )
                entry = _find_record_entry(payload, reference_record)
                value = _record_value(package, entry["entry"], entry["key"])
                _require(
                    _close(
                        [limits.get("lower"), limits.get("upper")],
                        [value.get("lower"), value.get("upper")],
                        1e-12,
                    ),
                    "discovery.joints",
                    "the joint limits differ from the controlled record",
                    {"joint": joint.get("name")},
                )
            drive_reference = properties.get("dp.joint.drive_record")
            _require(
                isinstance(drive_reference, str) and drive_reference.strip(),
                "discovery.joints",
                "the joint has no controlled drive record",
                {"joint": joint.get("name")},
            )
            entry = _find_record_entry(payload, drive_reference)
            value = _record_value(package, entry["entry"], entry["key"])
            _require(
                _close(
                    [limits.get("effort"), limits.get("velocity")], [value.get("effort"), value.get("velocity")], 1e-12
                ),
                "discovery.joints",
                "the joint drive differs from the controlled record",
                {"joint": joint.get("name")},
            )
            origin = [frame[0][3], frame[1][3], frame[2][3]]
            offset = _offset(group["point"], origin, native)
            _require(
                offset <= AXIS_OFFSET_TOL_M,
                "discovery.joints",
                "the child body frame is off the native joint axis",
                {"joint": joint.get("name"), "offset_m": offset},
            )
        for joint in source_joints:
            pair = frozenset((str(joint.get("parent")), str(joint.get("child"))))
            _require(
                pair in seen,
                "discovery.joints",
                "robot.yaml contains a joint with no movable mate pair",
                {"joint": joint.get("name")},
            )
        return {"joints": derived}

    def tree():
        robot = state["robot"]
        source = robot.get("source") or {}
        bodies = [str(body.get("name")) for body in source.get("bodies") or []]
        _require("base_link" in bodies, "discovery.tree", "no base_link body")
        _require(
            any(
                str(body.get("name")) == "base_link"
                and str((body.get("frame") or {}).get("coordinate_system")) == "CS_base_link"
                for body in source.get("bodies") or []
            ),
            "discovery.tree",
            "no body owns CS_base_link; a temporary IsFixed flag cannot prove the base",
        )
        incoming: dict[str, str] = {}
        edges: dict[str, list[str]] = {}
        for joint in source.get("joints") or []:
            parent, child = str(joint.get("parent")), str(joint.get("child"))
            _require(
                parent in bodies and child in bodies,
                "discovery.tree",
                "a joint names an unknown body",
                {"joint": joint.get("name")},
            )
            _require(child not in incoming, "discovery.tree", "a body has two parents", {"body": child})
            incoming[child] = parent
            edges.setdefault(parent, []).append(child)
        roots = [name for name in bodies if name not in incoming]
        _require(
            roots == ["base_link"], "discovery.tree", "the joint tree must be rooted at base_link", {"roots": roots}
        )
        reached = {"base_link"}
        frontier = ["base_link"]
        while frontier:
            current = frontier.pop()
            for child in edges.get(current, []):
                _require(child not in reached, "discovery.tree", "the joint graph contains a cycle")
                reached.add(child)
                frontier.append(child)
        _require(
            reached == set(bodies),
            "discovery.tree",
            "some bodies are disconnected",
            {"unreached": sorted(set(bodies) - reached)},
        )
        return {"bodies": len(bodies), "joints": len(source.get("joints") or [])}

    def frames():
        robot = state["robot"]
        payload = state["payload"]
        raw = payload["raw"]
        attached = _frame_attached_members(raw)
        source = robot.get("source") or {}
        bodies = source.get("bodies") or []
        by_material, _by_component = _cluster_bindings(raw)
        component_body: dict[str, str] = {}
        link_datums: set[tuple[str, str]] = set()
        body_of: dict[str, dict] = {}
        cluster_of_body: dict[str, frozenset[str]] = {}
        for body in bodies:
            name = str(body.get("name"))
            body_of[name] = body
            material = frozenset(str(value) for value in (body.get("components") or []))
            full = by_material.get(material, material)
            cluster_of_body[name] = full
            for component in full:
                component_body[component] = name
        # A proven frame-attached cluster is rigidly the assembly frame, so the frozen
        # top assembly's ownerless datum channel maps to exactly that body: recognised
        # top-owned interfaces (TCP_/SCS_) resolve there or stay unowned and block.
        if attached:
            material = next((group for group, full in by_material.items() if set(full) == set(attached)), None)
            if material is not None:
                base = next(
                    (
                        str(item.get("name"))
                        for item in bodies
                        if {str(value) for value in (item.get("components") or [])} == set(material)
                    ),
                    None,
                )
                if base is not None:
                    component_body[""] = base
        for body in bodies:
            frame_name = str((body.get("frame") or {}).get("coordinate_system") or "")
            frame_datum = _owned_datum(
                raw,
                frame_name,
                _datum_owners(cluster_of_body.get(str(body.get("name")), frozenset()), attached),
            )
            if frame_datum is not None:
                link_datums.add((str(frame_datum.get("owner") or ""), str(frame_datum.get("name") or "")))
        expected: dict[str, tuple[str, str]] = {}
        for datum in raw.get("datums") or []:
            if not isinstance(datum, dict):
                continue
            name = str(datum.get("name") or "")
            owner = str(datum.get("owner") or "")
            # Only the exact (owner, name) pair a body uses as its frame is a link datum: a
            # foreign datum that merely shares the name stays an interface and cannot vanish.
            if not name.startswith(INTERFACE_PREFIXES) or (owner, name) in link_datums:
                continue
            body = component_body.get(owner)
            _require(
                body is not None,
                "discovery.frames",
                "a recognised interface datum is not owned by any body",
                {"datum": name, "owner": owner},
            )
            _prefix, _, suffix = name.partition("_")
            _require(
                _SNAKE.fullmatch(suffix) is not None,
                "discovery.frames",
                "an interface datum suffix is not exact snake_case",
                {"datum": name},
            )
            frame_name = suffix
            _require(
                frame_name not in expected,
                "discovery.frames",
                "two interface datums derive the same frame name",
                {"datum": name},
            )
            _require(
                frame_name not in {str(item.get("name")) for item in source.get("bodies") or []},
                "discovery.frames",
                "an interface frame collides with a body name",
                {"datum": name, "name": frame_name},
            )
            expected[frame_name] = (body, name)
        observed: dict[str, tuple[str, str]] = {}
        for frame in source.get("frames") or []:
            name = str(frame.get("name"))
            _require(name not in observed, "discovery.frames", "a frame name repeats", {"frame": name})
            observed[name] = (str(frame.get("parent")), str(frame.get("coordinate_system")))
        _require(
            observed == expected,
            "discovery.frames",
            "named native interfaces were dropped or invented",
            {
                "missing": sorted(set(expected) - set(observed)),
                "extra": sorted(set(observed) - set(expected)),
                "wrong": sorted(name for name in set(observed) & set(expected) if observed[name] != expected[name]),
            },
        )
        joints = {str(joint.get("name")): joint for joint in source.get("joints") or []}
        jcs_seen = 0
        for datum in raw.get("datums") or []:
            if not isinstance(datum, dict):
                continue
            name = str(datum.get("name") or "")
            if not name.startswith(JCS_PREFIX):
                continue
            jcs_seen += 1
            joint = joints.get(name[len(JCS_PREFIX) :])
            _require(joint is not None, "discovery.frames", "JCS_ names no discovered joint", {"datum": name})
            child = body_of.get(str(joint.get("child")))
            _require(child is not None, "discovery.frames", "JCS_ joint has no child body", {"datum": name})
            _require(
                str(datum.get("owner") or "")
                in cluster_of_body.get(
                    str(child.get("name")), frozenset(str(value) for value in child.get("components") or [])
                ),
                "discovery.frames",
                "JCS_ datum is not owned by the child body's rigid scope",
                {"datum": name, "owner": datum.get("owner")},
            )
            reference = _owned_datum(
                raw,
                (child.get("frame") or {}).get("coordinate_system"),
                _datum_owners(cluster_of_body.get(str(child.get("name")), frozenset()), attached),
            )
            _require(
                reference is not None,
                "discovery.frames",
                "a JCS_ child body has no datum owned uniquely by that body",
                {"datum": name, "body": child.get("name")},
            )
            alias = [float(value) for value in datum.get("array") or ()]
            target = [float(value) for value in (reference or {}).get("array") or ()]
            _require(
                len(alias) == 16
                and len(target) == 16
                and all(abs(one - two) <= 1e-6 for one, two in zip(alias, target, strict=True)),
                "discovery.frames",
                "JCS_ frame differs from the child body datum the compiler places the joint at",
                {"datum": name},
            )
        return {"frames": len(expected), "jcs_aliases": jcs_seen}

    def declarations():
        from .native_declarations import verify_declarations

        report = verify_declarations(state["payload"]["raw"], state["robot"].get("source"))
        _require(
            report["passed"],
            "discovery.declarations",
            "independent native declaration verification failed",
            {"failures": report["errors"], "checks": report["checks"], "diagnostics": report["diagnostics"]},
        )
        return {
            "bodies": len(report["members"]),
            "joints": len((state["robot"].get("source") or {}).get("joints") or []),
            "checks": report["checks"],
            "rigid_completion": report["completion"],
            "diagnostics": report["diagnostics"],
        }

    bound = check("discovery.binding", binding)

    def suppression():
        return _require_suppression_states((state.get("payload") or {}).get("raw") or {})

    def source_selection():
        return _require_source_selection(state.get("payload") or {})

    dependent = (
        ("discovery.suppression_state", suppression),
        ("discovery.selection", source_selection),
        ("discovery.files", files),
        ("discovery.imported_sources", imported_sources),
        ("discovery.identity", identity),
        ("discovery.revision", revision),
        ("discovery.budgets", budgets),
        ("discovery.graph", graph),
        ("discovery.frame_attachment", frame_attachment),
        ("discovery.bodies", bodies),
        ("discovery.masses", masses),
        ("discovery.names", names),
        ("discovery.joints", joints),
        ("discovery.tree", tree),
        ("discovery.frames", frames),
    )
    raw_block = (state.get("payload") or {}).get("raw")
    raw_block = raw_block if isinstance(raw_block, dict) else None
    # Shared predicate contract with the producer (``sources/solidworks/discovery.py``
    # ``exporter_mode``): the native capture writes the exporter block unconditionally,
    # and only a proven ``absent`` state means the CAD carries no v1.4 declaration. That
    # capture must run the mate-inference suite below; every other present state keeps
    # the declarations suite, which fails closed on unreadable declarations.
    exporter_block = raw_block.get("exporter") if raw_block is not None else None
    declaration_capture = (
        raw_block is not None
        and "exporter" in raw_block
        and not (isinstance(exporter_block, dict) and exporter_block.get("state") == "absent")
    )
    if bound["passed"] and declaration_capture:
        # Explicit native declarations do not require incomplete mate inference to
        # rediscover memberships or impose unrelated CS_/dp.* naming conventions.
        # Capture/file/mass binding remains mandatory. A declaration carries no
        # external design budget; never invent nominal mass or extent ranges.
        dependent = (
            ("discovery.suppression_state", suppression),
            ("discovery.selection", source_selection),
            ("discovery.files", files),
            ("discovery.imported_sources", imported_sources),
            ("discovery.identity", identity),
            ("discovery.revision", revision),
            ("discovery.budgets", budgets),
            ("discovery.masses", masses),
            ("discovery.declarations", declarations),
        )
    if bound["passed"]:
        payload = state["payload"]
        raw_block = payload.get("raw")
        raw_block = raw_block if isinstance(raw_block, dict) else {}
        document_properties = (raw_block.get("properties") or {}).get("document") or {}
        budget_reference = str(payload.get("namespace") or "dp") + ".design_budget_record"
        expectations = state["robot"].get("checks") or {}
        if (
            "budget" not in payload
            and budget_reference not in document_properties
            and not any(key in expectations for key in ("expected_mass_kg", "expected_extent_m"))
        ):
            dependent = tuple(item for item in dependent if item[0] != "discovery.budgets")
    if bound["passed"]:
        for identifier, callback in dependent:
            if identifier == "discovery.revision":
                authored = state.get("authored_identity") or {}
                if not revision_path.exists() and not revision_path.is_symlink() and authored.get("missing"):
                    # Native file/provenance checks still bind every captured byte. The
                    # absent publication seal is a readiness warning, never a passed check.
                    diagnostics.append(
                        {
                            "code": "discovery.revision_not_sealed",
                            "blocking": False,
                            "message": (
                                "cad-revision.json is absent while authored publication metadata is incomplete"
                            ),
                            "detail": {"state": "not_sealed", "missing": list(authored.get("missing") or [])},
                        }
                    )
                    continue
            check(identifier, callback)
    else:
        for identifier, _callback in dependent:
            checks.append(
                {
                    "id": identifier,
                    "passed": False,
                    "details": {"code": "discovery.binding", "error": "not evaluated: the discovery binding failed"},
                }
            )
    passed = bool(errors) is False and all(item["passed"] for item in checks)
    report = {
        "schema_version": VERIFICATION_SCHEMA,
        "passed": passed,
        "checks": checks,
        "errors": errors,
        "diagnostics": diagnostics,
    }
    if checks and checks[0]["passed"]:
        report["discovery_sha256"] = checks[0]["details"].get("discovery_sha256")
        report["bodies"] = next(
            (
                item["details"].get("bodies")
                for item in checks
                if item["id"] in {"discovery.bodies", "discovery.declarations"}
            ),
            None,
        )
        report["joints"] = next(
            (
                item["details"].get("joints")
                for item in checks
                if item["id"] in {"discovery.joints", "discovery.declarations"}
            ),
            None,
        )
    return report


def _find_record_entry(payload: dict, reference: str) -> dict:
    relative, _, key = str(reference).partition("#")
    for entry in payload.get("records") or []:
        if (
            isinstance(entry, dict)
            and str(entry.get("file")) == relative.replace("\\", "/")
            and str(entry.get("key") or "") == key
        ):
            return {"entry": entry, "key": key}
    raise _Failure(
        "discovery.record",
        "the referenced controlled record was not embedded in the package",
        {"reference": reference},
    )
