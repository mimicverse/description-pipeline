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
INTERFACE_PREFIXES = ("CS_", "TCP_", "SCS_")
INTERFACE_SUFFIXES = ("_mount", "_frame", "_datum", "_tcp", "_scs", "_sensor", "_tool")
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


def _frames(record: dict) -> dict[str, list[list[float]] | None]:
    frames: dict[str, list[list[float]] | None] = {}
    for item in record.get("components") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name2")
        if not isinstance(name, str) or item.get("suppressed"):
            continue
        try:
            numbers = [float(value) for value in item.get("transform") or ()]
        except (TypeError, ValueError):
            numbers = []
        frames[name] = [numbers[0:4], numbers[4:8], numbers[8:12], numbers[12:16]] if len(numbers) == 16 else None
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


def _geometry(entity: dict, frames):
    frame = frames.get(str(entity.get("component")))
    if frame is None:
        return None, None
    if isinstance(entity.get("cylinder"), dict):
        direction = _unit_vector(_apply_vector(entity["cylinder"].get("direction") or (), frame))
        point = _apply_point(entity["cylinder"].get("point") or (), frame)
        return point, direction
    if isinstance(entity.get("plane"), dict):
        direction = _unit_vector(_apply_vector(entity["plane"].get("normal") or (), frame))
        point = _apply_point(entity["plane"].get("point") or (), frame)
        return point, direction
    if isinstance(entity.get("point"), (list, tuple)):
        return _apply_point(entity["point"], frame), None
    return None, None


def _rows_for(mate: dict, frames) -> dict | None:
    """The oracle's own constraint reconstruction; ``None`` means unsupported."""

    kind = str(mate.get("type") or "").strip().lower()
    if kind not in SUPPORTED_MATES:
        return None
    entities = _entities(mate)
    if len(entities) < 2:
        return None
    first, second = entities[0], entities[1]
    limits = mate.get("limits") if isinstance(mate.get("limits"), dict) else None
    axes = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    if kind == "lock":
        point, _ = _geometry(first, frames)
        if point is None:
            return None
        rows = [_translation_row(axis, point) for axis in axes] + [_rotation_row(axis) for axis in axes]
        return {"rows": rows, "limits": limits, "axis": None, "point": point}
    if kind == "concentric":
        left_point, left_axis = _geometry(first, frames)
        right_point, right_axis = _geometry(second, frames)
        if left_axis is None or right_axis is None or abs(abs(_dot(left_axis, right_axis)) - 1.0) > TOL:
            return None
        plane = _plane_basis(left_axis)
        if len(plane) != 2:
            return None
        rows = [_translation_row(direction, left_point) for direction in plane] + [
            _rotation_row(direction) for direction in plane
        ]
        return {"rows": rows, "limits": limits, "axis": left_axis, "point": left_point}
    if kind == "coincident":
        left_point, left_axis = _geometry(first, frames)
        right_point, right_axis = _geometry(second, frames)
        if (
            left_axis is not None
            and right_axis is not None
            and isinstance(first.get("plane"), dict)
            and isinstance(second.get("plane"), dict)
        ):
            if abs(abs(_dot(left_axis, right_axis)) - 1.0) > TOL:
                return None
            normal = left_axis if _dot(left_axis, right_axis) >= 0 else [-value for value in left_axis]
            plane = _plane_basis(normal)
            if len(plane) != 2:
                return None
            rows = [_translation_row(normal, left_point)] + [_rotation_row(direction) for direction in plane]
            return {"rows": rows, "limits": limits, "axis": None, "point": left_point}
        if isinstance(first.get("point"), (list, tuple)) and isinstance(second.get("point"), (list, tuple)):
            rows = [_translation_row(axis, left_point) for axis in axes]
            return {"rows": rows, "limits": limits, "axis": None, "point": left_point}
        normal = left_axis or right_axis
        point = left_point if left_axis is not None else right_point
        if normal is None or point is None:
            return None
        # A vertex on a face removes one translation only.
        return {"rows": [_translation_row(normal, point)], "limits": limits, "axis": None, "point": point}
    if kind == "limitangle":
        return {"rows": [], "limits": limits, "axis": None, "point": None}
    if kind == "limitdistance":
        _first_point, first_axis = _geometry(first, frames)
        _second_point, second_axis = _geometry(second, frames)
        if (
            isinstance(first.get("plane"), dict)
            and isinstance(second.get("plane"), dict)
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
        if isinstance(first.get("point"), (list, tuple)) and isinstance(second.get("point"), (list, tuple)):
            direction = _unit_vector([right_point[i] - left_point[i] for i in range(3)])
            point = [(left_point[i] + right_point[i]) / 2.0 for i in range(3)]
        else:
            direction = left_axis or right_axis
            point = left_point if left_axis is not None else right_point
        if direction is None or point is None:
            return None
        return {"rows": [_translation_row(direction, point)], "limits": limits, "axis": None, "point": point}
    if kind == "parallel":
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
    markers: dict[str, list[str]] = {}
    properties = (record.get("properties") or {}).get("components") or {}
    for name in components:
        marker = (properties.get(name) or {}).get("dp.body_marker")
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
        frames = _frames(raw)
        for mate in raw.get("mates") or []:
            _require(isinstance(mate, dict), "discovery.graph", "a mate entry is not an object")
            if mate.get("suppressed"):
                continue
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
        bodies = source.get("bodies") or []
        joints = source.get("joints") or []
        members, pairs = _independent_clusters(raw)
        components = {str(item.get("name2")): item for item in raw.get("components") or [] if isinstance(item, dict)}
        datums = [item for item in raw.get("datums") or [] if isinstance(item, dict)]
        identities: dict[str, str] = {}
        for group in members.values():
            body = next(
                (item for item in bodies if {str(value) for value in (item.get("components") or [])} == set(group)),
                None,
            )
            _require(body is not None, "discovery.names", "a rigid body has no robot.yaml body", {"components": group})
            name = str(body.get("name"))
            datum_name = str((body.get("frame") or {}).get("coordinate_system"))
            owned = [
                item for item in datums if str(item.get("owner") or "") in group and str(item.get("name")) == datum_name
            ]
            _require(
                owned,
                "discovery.names",
                "a body frame datum is not owned by its components",
                {"body": name, "datum": datum_name},
            )
            item = components.get(group[0], {})
            identity = str(item.get("instance_id") or f"{item.get('document') or ''}#{group[0]}")
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
                explicit = {(components_properties.get(component) or {}).get("dp.body_datum") for component in group}
                if datum_name not in explicit:
                    _require(
                        not any(str(datum_name).endswith(suffix) for suffix in INTERFACE_SUFFIXES),
                        "discovery.names",
                        "a suffix-qualified interface datum cannot be the link frame",
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
        source = robot.get("source") or {}
        source_joints = source.get("joints") or []
        bodies = {
            frozenset(str(value) for value in (body.get("components") or [])): body
            for body in source.get("bodies") or []
        }
        component_body: dict[str, str] = {}
        for components, body in bodies.items():
            for component in components:
                component_body[component] = str(body.get("name"))
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
            child_datum = _datum(raw, child["frame"]["coordinate_system"])
            frame = _frame(child_datum.get("array"))
            native = [float(value) for value in shaft]
            if str(properties.get("dp.joint.axis_sign")) == "-1":
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
        bodies = source.get("bodies") or []
        component_body: dict[str, str] = {}
        link_datums: set[str] = set()
        body_of: dict[str, dict] = {}
        for body in bodies:
            body_of[str(body.get("name"))] = body
            link_datums.add(str((body.get("frame") or {}).get("coordinate_system")))
            for component in body.get("components") or []:
                component_body[str(component)] = str(body.get("name"))
        expected: dict[str, tuple[str, str]] = {}
        for datum in raw.get("datums") or []:
            if not isinstance(datum, dict):
                continue
            name = str(datum.get("name") or "")
            if not name.startswith(INTERFACE_PREFIXES) or name in link_datums:
                continue
            owner = str(datum.get("owner") or "")
            body = component_body.get(owner)
            _require(
                body is not None,
                "discovery.frames",
                "a recognised interface datum is not owned by any body",
                {"datum": name, "owner": owner},
            )
            frame_name = name.lower()
            _require(
                _SNAKE.match(frame_name) is not None,
                "discovery.frames",
                "an interface datum does not derive an exact snake_case frame name",
                {"datum": name},
            )
            _require(
                frame_name not in expected,
                "discovery.frames",
                "two interface datums derive the same frame name",
                {"datum": name},
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
            reference = _datum(raw, (child.get("frame") or {}).get("coordinate_system"))
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
