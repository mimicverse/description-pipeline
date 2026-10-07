"""Independent offline verification of a native-discovery prepared package.

This module shares no code with :mod:`description_pipeline.sources.solidworks.discovery`.
It re-derives rigid memberships, joints, axes, limits and names from the bound
raw native record and rejects any generated claim the record does not support.
It runs without Windows or SolidWorks: everything it needs (the raw record, the
embedded controlled records and the generated contract files) is inside the
package.

Only three facts are taken from the package as *claims*: ``robot.yaml``, the
``cad-revision.json`` seal and the discovery payload itself.  The payload is
bound by digest to the package's provenance block; every derived value is
recomputed here before it is accepted.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

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

_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
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


def _unit_vector(vector) -> list[float] | None:
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


def _direction(entity: dict) -> list[float] | None:
    if isinstance(entity.get("cylinder"), dict):
        return _unit_vector(entity["cylinder"].get("direction"))
    if isinstance(entity.get("plane"), dict):
        return _unit_vector(entity["plane"].get("normal"))
    return None


def _rows_for(mate: dict) -> dict | None:
    """The verifier's own constraint reconstruction; ``None`` means unsupported."""

    kind = str(mate.get("type") or "").strip().lower()
    if kind not in SUPPORTED_MATES:
        return None
    entities = _entities(mate)
    if len(entities) < 2:
        return None
    first, second = entities[0], entities[1]
    limits = mate.get("limits") if isinstance(mate.get("limits"), dict) else None
    if kind == "lock":
        basis = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        return {"T": basis, "R": basis, "limits": limits, "axis": None}
    if kind == "concentric":
        left = _unit_vector((first.get("cylinder") or {}).get("direction"))
        right = _unit_vector((second.get("cylinder") or {}).get("direction"))
        if left is None or right is None or abs(abs(_dot(left, right)) - 1.0) > TOL:
            return None
        basis = [
            _unit_vector(_cross(left, [1.0, 0.0, 0.0])),
            _unit_vector(_cross(left, [0.0, 1.0, 0.0])),
        ]
        basis = _span(basis)
        if len(basis) != 2:
            return None
        return {"T": basis, "R": basis, "limits": limits, "axis": left}
    if kind == "coincident":
        first_plane = first.get("plane") if isinstance(first.get("plane"), dict) else None
        second_plane = second.get("plane") if isinstance(second.get("plane"), dict) else None
        first_point = first.get("point") if isinstance(first.get("point"), (list, tuple)) else None
        second_point = second.get("point") if isinstance(second.get("point"), (list, tuple)) else None
        if first_plane is not None and second_plane is not None:
            left = _unit_vector(first_plane.get("normal"))
            right = _unit_vector(second_plane.get("normal"))
            if left is None or right is None or abs(abs(_dot(left, right)) - 1.0) > TOL:
                return None
            normal = left if _dot(left, right) >= 0 else [-value for value in left]
            plane = _span(
                [_unit_vector(_cross(normal, [1.0, 0.0, 0.0])), _unit_vector(_cross(normal, [0.0, 1.0, 0.0]))]
            )
            if len(plane) != 2:
                return None
            return {"T": [normal], "R": plane, "limits": limits, "axis": None}
        if first_point is not None and second_point is not None:
            return {"T": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], "R": [], "limits": limits, "axis": None}
        normal = None
        if first_plane is not None:
            normal = _unit_vector(first_plane.get("normal"))
        elif second_plane is not None:
            normal = _unit_vector(second_plane.get("normal"))
        if normal is None:
            return None
        plane = _span([_unit_vector(_cross(normal, [1.0, 0.0, 0.0])), _unit_vector(_cross(normal, [0.0, 1.0, 0.0]))])
        if len(plane) != 2:
            return None
        return {"T": [normal], "R": plane, "limits": limits, "axis": None}
    if kind in ("distance", "limitdistance"):
        direction = None
        first_point = first.get("point") if isinstance(first.get("point"), (list, tuple)) else None
        second_point = second.get("point") if isinstance(second.get("point"), (list, tuple)) else None
        first_plane = first.get("plane") if isinstance(first.get("plane"), dict) else None
        second_plane = second.get("plane") if isinstance(second.get("plane"), dict) else None
        if first_point is not None and second_point is not None:
            direction = _unit_vector([float(second_point[i]) - float(first_point[i]) for i in range(3)])
        elif first_plane is not None and second_plane is not None:
            left = _unit_vector(first_plane.get("normal"))
            right = _unit_vector(second_plane.get("normal"))
            if left is not None and right is not None and abs(abs(_dot(left, right)) - 1.0) <= TOL:
                direction = left
        elif first_point is not None and second_plane is not None:
            direction = _unit_vector(second_plane.get("normal"))
        elif first_plane is not None and second_point is not None:
            direction = _unit_vector(first_plane.get("normal"))
        if direction is None:
            return None
        return {"T": [direction], "R": [], "limits": limits, "axis": None}
    if kind == "parallel":
        left = _direction(first)
        right = _direction(second)
        if left is None or right is None or abs(abs(_dot(left, right)) - 1.0) > TOL:
            return None
        basis = _span([_unit_vector(_cross(left, [1.0, 0.0, 0.0])), _unit_vector(_cross(left, [0.0, 1.0, 0.0]))])
        if len(basis) != 2:
            return None
        return {"T": [], "R": basis, "limits": limits, "axis": None}
    left = _direction(first)
    right = _direction(second)
    if left is None or right is None:
        return None
    normal = _unit_vector(_cross(left, right))
    if normal is None:
        return None
    return {"T": [], "R": [normal], "limits": limits, "axis": None}


def _free_direction(rows) -> list[float] | None:
    basis = _span(rows)
    if not basis:
        return None
    if len(basis) >= 2:
        return _unit_vector(_cross(basis[0], basis[1]))
    helper = [1.0, 0.0, 0.0] if abs(basis[0][0]) <= 0.9 else [0.0, 1.0, 0.0]
    return _unit_vector(_cross(basis[0], helper))


def _find(nodes: list[str], parent: dict[str, str], node: str) -> str:
    while parent[node] != node:
        node = parent[node]
    return node


def _independent_clusters(record: dict) -> tuple[dict[str, list[str]], dict[tuple[str, str], dict]]:
    components = {
        str(item.get("name2")): item
        for item in record.get("components") or []
        if isinstance(item, dict) and isinstance(item.get("name2"), str) and not item.get("suppressed")
    }
    pairs: dict[tuple[str, str], dict] = {}
    for index, mate in enumerate(record.get("mates") or []):
        if not isinstance(mate, dict) or mate.get("suppressed"):
            continue
        rows = _rows_for(mate)
        names = _names(mate)
        for left_index in range(len(names)):
            for right_index in range(left_index + 1, len(names)):
                left, right = names[left_index], names[right_index]
                if left not in components or right not in components:
                    continue
                key = tuple(sorted((left, right)))
                group = pairs.setdefault(
                    key, {"T": [], "R": [], "mates": [], "unresolved": False, "limits": None, "axis": None}
                )
                group["mates"].append(index)
                if rows is None:
                    group["unresolved"] = True
                    continue
                group["T"].extend(rows["T"])
                group["R"].extend(rows["R"])
                if group["limits"] is None and isinstance(rows.get("limits"), dict):
                    group["limits"] = rows["limits"]
                if group["axis"] is None and isinstance(rows.get("axis"), list):
                    group["axis"] = rows["axis"]
    for group in pairs.values():
        group["rank"] = (len(_span(group["T"])), len(_span(group["R"])))
    parent = {name: name for name in components}
    union = [key for key, group in sorted(pairs.items()) if not group["unresolved"] and group["rank"] == (3, 3)]
    fixed = [name for name, item in components.items() if item.get("fixed")]
    if fixed:
        union.extend((fixed[0], name) for name in fixed[1:])
    for left, right in union:
        root_left, root_right = _find(components, parent, left), _find(components, parent, right)
        if root_left != root_right:
            parent[root_right] = root_left
    markers: dict[str, list[str]] = {}
    properties = (record.get("properties") or {}).get("components") or {}
    for name in components:
        values = properties.get(name) or {}
        marker = values.get("dp.body_marker")
        if isinstance(marker, str) and marker.strip():
            markers.setdefault(marker.strip(), []).append(name)
    for group in markers.values():
        for other in group[1:]:
            root_left, root_right = _find(components, parent, group[0]), _find(components, parent, other)
            if root_left != root_right:
                parent[root_right] = root_left
    members: dict[str, list[str]] = {}
    for name in components:
        members.setdefault(_find(components, parent, name), []).append(name)
    return {root: sorted(value) for root, value in members.items()}, pairs


def _datum(record: dict, name) -> dict | None:
    for datum in record.get("datums") or []:
        if isinstance(datum, dict) and str(datum.get("name")) == str(name):
            return datum
    return None


def _frame(values) -> list[list[float]] | None:
    try:
        numbers = [float(value) for value in values or ()]
    except (TypeError, ValueError):
        return None
    if len(numbers) != 16:
        return None
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


def verify_discovery(package: Path) -> dict:
    """Recompute every native-discovery claim from the bound raw record."""

    package = Path(package)
    checks: list[dict] = []
    errors: list[dict] = []
    state: dict = {}

    def check(identifier, callback):
        try:
            details = callback() or {}
            checks.append({"id": identifier, "passed": True, "details": details})
        except _Failure as failure:
            checks.append(
                {
                    "id": identifier,
                    "passed": False,
                    "details": {"code": failure.code, "error": failure.message, **failure.detail},
                }
            )
            errors.append({"code": failure.code, "message": failure.message, "detail": failure.detail})
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
        _require(
            _ID.match(str(robot.get("hardware_id") or "")),
            "discovery.binding",
            "hardware_id is not an ASCII identifier",
        )
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

    def revision():
        robot = state["robot"]
        _require(revision_path.is_file(), "discovery.revision", "cad-revision.json is missing")
        revision = _read_json(revision_path)
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
        names = {str(item.get("name2")) for item in components if isinstance(item, dict)}
        for mate in raw.get("mates") or []:
            _require(isinstance(mate, dict), "discovery.graph", "a mate entry is not an object")
            if mate.get("suppressed"):
                continue
            _require(
                _rows_for(mate) is not None,
                "discovery.graph",
                "a mate is outside the supported constraint scope or lacks usable entities",
                {"type": mate.get("type"), "name": mate.get("name")},
            )
            for entity in _entities(mate):
                _require(
                    str(entity.get("component")) in names,
                    "discovery.graph",
                    "a mate entity names an unknown component",
                    {"component": entity.get("component")},
                )
                cylinder = entity.get("cylinder")
                if isinstance(cylinder, dict):
                    direction = _unit_vector(cylinder.get("direction"))
                    _require(direction is not None, "discovery.graph", "a cylinder axis is not usable")
                    radius = cylinder.get("radius")
                    _require(
                        isinstance(radius, (int, float)) and float(radius) > 0,
                        "discovery.graph",
                        "a cylinder radius is not positive",
                    )
        for datum in raw.get("datums") or []:
            _require(
                isinstance(datum, dict) and _frame(datum.get("array")) is not None,
                "discovery.graph",
                "a datum transform is not a 4x4 frame",
            )
        return {"components": len(names), "mates": len(raw.get("mates") or []), "datums": len(raw.get("datums") or [])}

    def bodies():
        robot = state["robot"]
        raw = state["payload"]["raw"]
        members, _pairs = _independent_clusters(raw)
        source_bodies = (robot.get("source") or {}).get("bodies")
        _require(isinstance(source_bodies, list) and source_bodies, "discovery.bodies", "robot.yaml declares no bodies")
        expected = {frozenset(value) for value in members.values()}
        observed = {frozenset(str(item) for item in (body.get("components") or [])) for body in source_bodies}
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
                _datum(raw, frame["coordinate_system"]) is not None,
                "discovery.bodies",
                "a body frame names no recorded datum",
                {"body": name},
            )
        _require("base_link" in seen, "discovery.bodies", "no base_link body")
        return {"bodies": len(source_bodies)}

    def names():
        robot = state["robot"]
        payload = state["payload"]
        raw = payload["raw"]
        frozen = payload.get("frozen_names") or {}
        _require(isinstance(frozen, dict), "discovery.names", "frozen_names must be an object")
        source = robot.get("source") or {}
        body_names = {str(body.get("name")) for body in source.get("bodies") or []}
        joint_names = {str(joint.get("name")) for joint in source.get("joints") or []}
        members, _pairs = _independent_clusters(raw)
        components = {str(item.get("name2")): item for item in raw.get("components") or [] if isinstance(item, dict)}
        for _root, group in sorted(members.items()):
            documents = sorted({str(components.get(name, {}).get("document") or "") for name in group} - {""})
            identity = documents[0] if documents else group[0]
            if identity in frozen:
                _require(
                    frozen[identity] in body_names,
                    "discovery.names",
                    "a frozen published body name was not preserved",
                    {"identity": identity, "expected": frozen[identity]},
                )
        pair_primary: dict[tuple[str, str], str] = {}
        raw_mates = raw.get("mates") or []
        for key, group in sorted(_pairs.items()):
            mates = [raw_mates[index] for index in group["mates"] if 0 <= index < len(raw_mates)]
            pair_primary[key] = _primary_name(mates)
        for key, primary in sorted(pair_primary.items()):
            identity = primary or f"{key[0]}:{key[1]}"
            if identity in frozen:
                _require(
                    frozen[identity] in joint_names,
                    "discovery.names",
                    "a frozen published joint name was not preserved",
                    {"identity": identity, "expected": frozen[identity]},
                )
        for identity, name in sorted(frozen.items()):
            _require(
                str(name) in body_names or str(name) in joint_names,
                "discovery.names",
                "a frozen name was dropped from the package",
                {"identity": identity, "name": name},
            )
        return {"frozen": len(frozen)}

    def joints():
        robot = state["robot"]
        payload = state["payload"]
        raw = payload["raw"]
        source = robot.get("source") or {}
        source_joints = source.get("joints") or []
        bodies = {
            frozenset(str(item) for item in (body.get("components") or [])): str(body.get("name"))
            for body in source.get("bodies") or []
        }
        component_body: dict[str, str] = {}
        for components, name in bodies.items():
            for component in components:
                component_body[component] = name
        _members, pairs = _independent_clusters(raw)
        seen: set[tuple[str, str]] = set()
        derived = 0
        for key, group in sorted(pairs.items()):
            _require(
                not group["unresolved"],
                "discovery.joints",
                "a mate pair could not be reconstructed",
                {"components": list(key)},
            )
            if group["rank"] == (3, 3):
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
            mates = [raw["mates"][index] for index in group["mates"] if 0 <= index < len(raw.get("mates") or [])]
            properties: dict = {}
            for mate in mates:
                values = ((raw.get("properties") or {}).get("mates") or {}).get(str(mate.get("name") or "")) or {}
                for name, value in values.items():
                    properties.setdefault(name, value)
            hint = str(properties.get("dp.joint.type") or "").strip().lower()
            annotation = str(joint.get("type"))
            rank_t, rank_r = group["rank"]
            if (rank_t, rank_r) == (3, 2):
                expected_type = "continuous" if hint == "continuous" else "revolute"
            elif (rank_t, rank_r) == (2, 3):
                expected_type = "prismatic"
            else:
                _require(
                    False,
                    "discovery.joints",
                    "an unsupported freedom pattern was reduced to a joint",
                    {"translation_rank": rank_t, "rotation_rank": rank_r, "components": list(key)},
                )
            _require(
                hint in {"", expected_type},
                "discovery.joints",
                "the joint type annotation contradicts the reconstructed freedom",
                {"annotation": hint, "derived": expected_type},
            )
            _require(
                annotation == expected_type,
                "discovery.joints",
                "the joint type differs from the reconstructed freedom",
                {"derived": expected_type, "type": annotation},
            )
            # Axis: the recorded shaft must follow the reconstructed freedom.
            free = _free_direction(group["R"] if rank_r == 2 else group["T"])
            _require(
                free is not None,
                "discovery.joints",
                "the reconstructed freedom has no free axis",
                {"components": list(key)},
            )
            cylinder_mate = next(
                (mate for mate in mates if any(isinstance(item.get("cylinder"), dict) for item in _entities(mate))),
                None,
            )
            _require(
                cylinder_mate is not None,
                "discovery.joints",
                "no cylindrical mate entity carries the joint shaft",
                {"joint": joint.get("name")},
            )
            cylinders = [
                item["cylinder"] for item in _entities(cylinder_mate) if isinstance(item.get("cylinder"), dict)
            ]
            point = [float(value) for value in cylinders[0].get("point") or ()]
            direction = _unit_vector(cylinders[0].get("direction"))
            _require(direction is not None and len(point) == 3, "discovery.joints", "the shaft reading is unusable")
            _require(
                abs(abs(_dot(direction, free)) - 1.0) <= 1e-4,
                "discovery.joints",
                "the recorded shaft does not follow the reconstructed freedom",
                {"joint": joint.get("name"), "shaft": direction, "freedom": free},
            )
            axis = {"point": point, "direction": direction}
            if str(properties.get("dp.joint.axis_sign")) in {"-1"}:
                axis["direction"] = [-value for value in axis["direction"]]
            child_name = str(joint.get("child"))
            child_body = next(body for body in source["bodies"] if str(body.get("name")) == child_name)
            child_datum = _datum(raw, child_body["frame"]["coordinate_system"])
            frame = _frame(child_datum.get("array"))
            local = _local_axis(frame, axis["direction"])
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
                    if str(entity.get("component")) == str(reference.get("component")) and isinstance(
                        entity.get("cylinder"), dict
                    ):
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
            # Limits and drive come from the native mate or the embedded record.
            native = next((mate.get("limits") for mate in mates if isinstance(mate.get("limits"), dict)), None)
            limits = joint.get("limits")
            _require(
                isinstance(limits, dict),
                "discovery.joints",
                "a movable joint has no limits",
                {"joint": joint.get("name")},
            )
            if annotation == "continuous":
                _require(
                    "lower" not in limits and "upper" not in limits,
                    "discovery.joints",
                    "a continuous joint carries position bounds",
                )
                _require(native is None, "discovery.joints", "a continuous joint has native position limits")
            elif native is not None:
                _require(
                    _close(
                        [limits.get("lower"), limits.get("upper")], [native.get("lower"), native.get("upper")], 1e-12
                    ),
                    "discovery.joints",
                    "the joint limits differ from the native mate",
                    {"joint": joint.get("name")},
                )
                unit = native.get("unit")
                _require(
                    unit == ("m" if annotation == "prismatic" else "rad"),
                    "discovery.joints",
                    "native limits are not in SI units",
                    {"unit": unit, "type": annotation},
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
                    _close([limits.get("lower"), limits.get("upper")], [value.get("lower"), value.get("upper")], 1e-12),
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
            # The child frame must sit on the shaft.
            if cylinder_mate is not None:
                origin = [frame[0][3], frame[1][3], frame[2][3]]
                offset = _offset(axis["point"], origin, axis["direction"])
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
        raw = state["payload"]["raw"]
        members, _pairs = _independent_clusters(raw)
        components = {str(item.get("name2")): item for item in raw.get("components") or [] if isinstance(item, dict)}
        fixed_roots = {
            root
            for root, group in members.items()
            if any(
                components.get(name, {}).get("fixed") and not components.get(name, {}).get("suppressed")
                for name in group
            )
        }
        _require(fixed_roots, "discovery.tree", "no fixed component identifies the base body")
        _require(len(fixed_roots) == 1, "discovery.tree", "several independent clusters claim the assembly ground")
        base_components = members[next(iter(fixed_roots))]
        owners = {
            str(body.get("name"))
            for body in source.get("bodies") or []
            for component in body.get("components") or []
            if str(component) in base_components
        }
        _require(
            owners == {"base_link"}, "discovery.tree", "base_link is not the fixed body", {"owners": sorted(owners)}
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
        source = robot.get("source") or {}
        checked = 0
        for joint in source.get("joints") or []:
            if str(joint.get("type")) == "fixed":
                continue
            children = [body for body in source["bodies"] if str(body.get("name")) == str(joint.get("child"))]
            _require(children, "discovery.frames", "a joint names no child body")
            child = children[0]
            datum = _datum(raw, child["frame"]["coordinate_system"])
            frame = _frame(datum.get("array"))
            authored = joint.get("axis")
            axis = _local_axis(frame, authored)
            norm = math.sqrt(sum(value * value for value in axis))
            _require(norm > 0, "discovery.frames", "a child frame collapses its joint axis")
            checked += 1
        _require(
            checked == len(source.get("joints") or []), "discovery.frames", "some joints carry no recomputed frame"
        )
        return {"moving_joints": checked}

    bound = check("discovery.binding", binding)
    dependent = (
        ("discovery.files", files),
        ("discovery.revision", revision),
        ("discovery.budgets", budgets),
        ("discovery.graph", graph),
        ("discovery.bodies", bodies),
        ("discovery.names", names),
        ("discovery.joints", joints),
        ("discovery.tree", tree),
        ("discovery.frames", frames),
    )
    if bound["passed"]:
        for identifier, callback in dependent:
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
    }
    if checks and checks[0]["passed"]:
        report["discovery_sha256"] = checks[0]["details"].get("discovery_sha256")
        report["bodies"] = next(
            (item["details"].get("bodies") for item in checks if item["id"] == "discovery.bodies"), None
        )
        report["joints"] = next(
            (item["details"].get("joints") for item in checks if item["id"] == "discovery.joints"), None
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
