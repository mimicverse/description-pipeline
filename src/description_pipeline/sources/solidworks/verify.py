"""Independent re-derivation of the normalized robot from raw CAD readings.

The oracle deliberately shares no code with the normalization path: it reads the
snapshot's raw component transforms and mass tensors again, applies the declared
mass table itself, and recomputes sum(m), the combined COM and the full inertia
tensor with plain numpy.  Agreement with the canonical model is therefore
evidence about the numbers, not about the code path that produced them.
"""

from __future__ import annotations

import contextlib
import math
from pathlib import Path
from collections.abc import Sequence
from typing import Any, cast

import numpy as np

from .jsonio import read_json
from ...io import PipelineError, confined, file_digest

INERTIA_ORDER = ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")
MASS_ATOL = 1e-12
MASS_RTOL = 1e-9
COM_ATOL = 1e-9
COM_RTOL = 1e-7
INERTIA_ATOL = 1e-12
INERTIA_RTOL = 1e-6


def _result(code: str, passed: bool, *, expected=(), checked=(), details=None, status=None) -> dict:
    """The shared verification result shape, built without importing it."""

    missing = sorted(set(expected) - set(checked))
    return {
        "id": code,
        "version": 1,
        "status": status or ("passed" if passed and not missing else "failed"),
        "expected": sorted(expected),
        "checked": sorted(checked),
        "missing": missing,
        "details": {} if details is None else details,
    }


def _rpy_matrix(rpy: Sequence[float] | np.ndarray) -> np.ndarray:
    roll, pitch, yaw = (float(value) for value in np.asarray(rpy, dtype=float).reshape(3))
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ],
        dtype=float,
    )


def _transform(matrix: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    values = np.array([float(value) for value in matrix], dtype=float)
    if values.size != 16:
        raise ValueError("a component transform must have 16 row-major values")
    grid = values.reshape(4, 4)
    return grid[:3, :3], grid[:3, 3]


def _tensor6(tensor: np.ndarray) -> list[float]:
    return [
        float(tensor[0, 0]),
        float(tensor[0, 1]),
        float(tensor[0, 2]),
        float(tensor[1, 1]),
        float(tensor[1, 2]),
        float(tensor[2, 2]),
    ]


def _declared_table(definition: dict, snapshot_root: Path | None) -> dict[str, dict]:
    table: dict[str, dict] = {}
    source = (definition or {}).get("source") or {}
    raw = source.get("documented_masses") or {}
    if isinstance(raw, dict):
        for name, entry in raw.items():
            if isinstance(entry, dict) and "mass_kg" in entry:
                table[str(name)] = dict(entry)
            elif isinstance(entry, (int, float)) and not isinstance(entry, bool):
                table[str(name)] = {"mass_kg": float(entry)}
    if not table and snapshot_root is not None:
        path = Path(snapshot_root) / "raw" / "declared_masses.json"
        if path.is_file():
            payload = read_json(path)
            if isinstance(payload, dict):
                for name, entry in (payload.get("items") or {}).items():
                    if isinstance(entry, dict) and "mass_kg" in entry:
                        table[str(name)] = dict(entry)
    return table


def _model_root(snapshot_root: Path) -> Path | None:
    """从快照目录向上找到模型仓库根（含 ``config/robot.yaml`` 的那一层）。

    快照固定在 ``<model>/sources/<id>/`` 下（``build.freeze`` 就是这么建的），
    归档证据文件属于模型输入，按模型仓库相对路径引用。
    """

    candidate = Path(snapshot_root).resolve()
    for folder in (candidate, *candidate.parents):
        if (folder / "config" / "robot.yaml").is_file():
            return folder
    return None


#: Advisory thresholds for the capture-time mass closure (not gates).
CLOSURE_MASS_RTOL = 1e-6
CLOSURE_COM_ATOL_M = 1e-6
CLOSURE_INERTIA_RTOL = 1e-4


def _mass_closure_check(snapshot: Path) -> dict:
    """Assembly-versus-leaf mass closure, reported as an advisory — never a blocker.

    The record is capture-time evidence: the assembly document's own mass properties next to the
    parallel-axis combination of the leaf readings.  Snapshots without it are ``not_applicable``; a
    mismatch is surfaced in ``details.advisory`` so `description check` prints it as a note while the
    model still qualifies; a malformed record is a defect.
    """

    path = Path(snapshot) / "raw" / "mass_closure.json"
    if not path.is_file():
        return _result(
            "source.normalization.mass_closure",
            True,
            status="not_applicable",
            details={"reason": "snapshot carries no assembly mass closure record"},
        )
    try:
        payload = read_json(path)
        if not isinstance(payload, dict):
            raise ValueError("mass closure record is not an object")
        if payload.get("status") == "unavailable":
            return _result(
                "source.normalization.mass_closure",
                True,
                status="not_applicable",
                details={
                    "reason": "the capture could not read the assembly mass properties",
                    "unavailable": str(payload.get("reason") or "unknown"),
                    "message": str(payload.get("message") or ""),
                },
            )
        top = payload.get("top_level")
        leaf = payload.get("leaf_total")
        if not isinstance(top, dict) or not isinstance(leaf, dict):
            raise ValueError("mass closure readings are not objects")
        top = cast("dict[str, Any]", top)
        leaf = cast("dict[str, Any]", leaf)
        top_mass = float(top["mass"])
        leaf_mass = float(leaf["mass"])
        top_com = np.asarray([float(value) for value in top["com"]], dtype=float)
        leaf_com = np.asarray([float(value) for value in leaf["com"]], dtype=float)
        top_inertia = np.asarray([[float(value) for value in row] for row in top["inertia"]], dtype=float)
        leaf_inertia = np.asarray([[float(value) for value in row] for row in leaf["inertia"]], dtype=float)
        if top_com.shape != (3,) or leaf_com.shape != (3,):
            raise ValueError("mass closure COM is not a 3-vector")
        if top_inertia.shape != (3, 3) or leaf_inertia.shape != (3, 3):
            raise ValueError("mass closure inertia is not a 3x3 tensor")
    except (KeyError, TypeError, ValueError, OSError, PipelineError) as exc:
        return _result(
            "source.normalization.mass_closure",
            False,
            details={"error": str(exc), "record": str(path)},
        )
    mass_rel = abs(top_mass - leaf_mass) / max(abs(top_mass), abs(leaf_mass), 1e-12)
    com_abs = float(np.max(np.abs(top_com - leaf_com)))
    scale = max(float(np.max(np.abs(top_inertia))), 1e-12)
    inertia_rel = float(np.max(np.abs(top_inertia - leaf_inertia))) / scale
    details = {
        "top_level_mass_kg": top_mass,
        "leaf_total_mass_kg": leaf_mass,
        "delta": {"mass_rel": mass_rel, "com_abs_max_m": com_abs, "inertia_rel": inertia_rel},
    }
    if mass_rel > CLOSURE_MASS_RTOL or com_abs > CLOSURE_COM_ATOL_M or inertia_rel > CLOSURE_INERTIA_RTOL:
        details["advisory"] = (
            "the assembly document's own mass properties disagree with the recombined leaf readings "
            f"(mass {top_mass:.9g} kg vs {leaf_mass:.9g} kg, {mass_rel:.3g} relative); the CAD tree "
            "may have been repaired or re-materialed after the leaf files were written — review the "
            "CAD and any declared masses"
        )
    return _result("source.normalization.mass_closure", True, details=details)


def _mass_evidence_check(
    definition: dict,
    snapshot_root: Path,
    included: Sequence[str],
    declared: dict[str, dict],
) -> dict | None:
    """独立核对"声明质量 → 归档证据文件"这条链：文件缺失/越界/摘要变化/锚点不匹配都拒绝。"""

    if not declared:
        return None
    binding = ((definition or {}).get("source") or {}).get("mass_evidence")
    problems: list[str] = []
    details: dict[str, object] = {
        "reference": binding.get("reference") if isinstance(binding, dict) else None,
        "file": binding.get("file") if isinstance(binding, dict) else None,
        "claimed_sha256": binding.get("sha256") if isinstance(binding, dict) else None,
        "anchors": {},
    }
    if not isinstance(binding, dict) or not binding.get("file") or not binding.get("sha256"):
        problems.append("evidence_not_bound")
        details["problems"] = problems
        return _result(
            "source.normalization.mass_evidence",
            False,
            expected=sorted(declared),
            checked=[],
            details=details,
        )

    root = _model_root(snapshot_root)
    if root is None:
        problems.append("evidence_root_not_found")
        details["problems"] = problems
        return _result(
            "source.normalization.mass_evidence",
            False,
            expected=sorted(declared),
            checked=[],
            details=details,
        )
    details["model_root"] = str(root)
    try:
        path = confined(root, str(binding["file"]), exists=False)
    except (PipelineError, ValueError) as exc:
        problems.append("evidence_path_escape")
        details["problems"] = problems
        details["error"] = str(exc)
        return _result(
            "source.normalization.mass_evidence",
            False,
            expected=sorted(declared),
            checked=[],
            details=details,
        )
    if not path.is_file():
        problems.append("evidence_file_missing")
        details["problems"] = problems
        return _result(
            "source.normalization.mass_evidence",
            False,
            expected=sorted(declared),
            checked=[],
            details=details,
        )
    actual = file_digest(path)
    details["actual_sha256"] = actual
    if actual != str(binding["sha256"]):
        problems.append("evidence_digest_mismatch")
    text = path.read_bytes().decode("utf-8", errors="replace")
    anchors: dict[str, object] = {}
    for name, entry in sorted(declared.items()):
        anchor = entry.get("evidence")
        if not isinstance(anchor, str) or not anchor:
            anchors[name] = "missing_anchor"
            problems.append(f"evidence_anchor_missing:{name}")
            continue
        if anchor in text:
            anchors[name] = "found"
        else:
            anchors[name] = "not_found"
            problems.append(f"evidence_anchor_not_found:{name}")
    uncovered = sorted(name for name in included if name not in declared)
    if uncovered:
        problems.extend(f"uncovered_component:{name}" for name in uncovered)
    details["anchors"] = anchors
    details["uncovered_components"] = uncovered
    details["problems"] = problems
    return _result(
        "source.normalization.mass_evidence",
        not problems,
        expected=sorted(declared),
        checked=sorted(name for name, state in anchors.items() if state == "found"),
        details=details,
    )


def _raw_inputs(snapshot_root: Path) -> tuple[dict, dict]:
    root = Path(snapshot_root)
    scene_raw = read_json(root / "raw" / "scene_raw.json")
    masses = read_json(root / "raw" / "mass_properties.json")
    if not isinstance(scene_raw, dict) or not isinstance(masses, dict):
        raise ValueError("snapshot is missing raw/scene_raw.json or raw/mass_properties.json")
    return scene_raw, masses


def _link_frame(body: dict) -> tuple[np.ndarray, np.ndarray]:
    frame = body.get("frame") or {}
    if not frame:
        return np.eye(3), np.zeros(3)
    xyz = np.array([float(value) for value in frame.get("xyz", (0.0, 0.0, 0.0))], dtype=float)
    rpy = np.array([float(value) for value in frame.get("rpy", (0.0, 0.0, 0.0))], dtype=float)
    return _rpy_matrix(rpy), xyz


def _raw_tensor(component: str, payload: dict) -> np.ndarray:
    """原始 9 个数 → 标准惯性张量；独立实现，刻意不引用生成侧的转换代码。

    ``solidworks_positive`` 是正惯性积记法：交叉项是 ``∫xy dm`` / ``∫zx dm`` / ``∫yz dm``，
    标准张量的非对角项是它们的相反数。原始读数保持原样；缺约定的原生数据不猜。
    """

    matrix = np.array([[float(value) for value in row] for row in payload["inertia"]], dtype=float)
    if matrix.shape != (3, 3):
        raise ValueError(f"component {component} has a non-3x3 inertia matrix")
    scale = max(1.0, float(np.abs(matrix).max()))
    if not np.allclose(matrix, matrix.T, atol=1e-12 * scale, rtol=0.0):
        raise ValueError(f"component {component} has a non-symmetric inertia matrix")
    reference = payload.get("reference") or {}
    convention = reference.get("product_convention")
    if convention == "solidworks_positive":
        tensor = matrix.copy()
        off_diagonal = ~np.eye(3, dtype=bool)
        tensor[off_diagonal] = -matrix[off_diagonal]
        return tensor
    if convention is None:
        used_api = str(reference.get("used_api") or "")
        if used_api == "fixture":
            return matrix
        raise ValueError(
            f"component {component} has no product_convention; refusing to guess the inertia sign convention"
        )
    raise ValueError(f"component {component} has an unsupported product_convention: {convention!r}")


def _component_mass(
    component: str, payload: dict, declared: dict[str, dict]
) -> tuple[float, np.ndarray, np.ndarray, dict]:
    """Return (mass, com_part, inertia_part, provenance) independently of the builder."""

    mass = float(payload["mass"])
    com = np.array([float(value) for value in payload["com"]], dtype=float)
    inertia = _raw_tensor(component, payload)
    note = {"component": component, "raw_mass_kg": mass, "used_mass_kg": mass, "scale": 1.0}
    entry = declared.get(component)
    if entry is not None:
        used = float(entry["mass_kg"])
        if mass <= 0.0:
            raise ValueError(f"component {component} has no positive CAD mass to scale from")
        scale = used / mass
        inertia = inertia * scale
        note.update(
            {"used_mass_kg": used, "scale": scale, "reason": entry.get("reason"), "evidence": entry.get("evidence")}
        )
        mass = used
    return mass, com, inertia, note


def _recompute_link(body: dict, components: dict[str, dict], raw_scene: dict, masses: dict, declared: dict) -> dict:
    placements = {entry["name"]: entry["transform"] for entry in raw_scene.get("components") or []}
    rotation_link, translation_link = _link_frame(body)
    total_mass = 0.0
    weighted = np.zeros(3)
    per_component = []
    for component in body.get("components") or []:
        if component not in placements or component not in masses:
            raise ValueError(f"body {body.get('id')!r} references a component without raw readings: {component}")
        rotation, translation = _transform(placements[component])
        mass, com_part, inertia_part, note = _component_mass(component, masses[component], declared)
        com_assembly = rotation @ com_part + translation
        inertia_assembly = rotation @ inertia_part @ rotation.T
        total_mass += mass
        weighted += mass * com_assembly
        per_component.append((mass, com_assembly, inertia_assembly, note))
    if total_mass <= 0.0:
        raise ValueError(f"body {body.get('id')!r} has no positive mass")
    com_assembly = weighted / total_mass
    inertia_assembly = np.zeros((3, 3))
    for mass, com, inertia, _note in per_component:
        delta = com - com_assembly
        inertia_assembly += inertia + mass * (float(delta @ delta) * np.eye(3) - np.outer(delta, delta))
    com_link = rotation_link.T @ (com_assembly - translation_link)
    inertia_link = rotation_link.T @ inertia_assembly @ rotation_link
    return {
        "mass": total_mass,
        "com": com_link,
        "inertia": inertia_link,
        "components": [note for _m, _c, _i, note in per_component],
    }


def _canonical_in_link_frame(link: dict) -> tuple[float, np.ndarray, np.ndarray]:
    inertial = link.get("inertial")
    if inertial is None:
        raise ValueError(f"link {link.get('name')!r} has no inertial block")
    rotation = _rpy_matrix(inertial.get("rpy") or (0.0, 0.0, 0.0))
    tensor = np.array(
        [
            [inertial["inertia"][0], inertial["inertia"][1], inertial["inertia"][2]],
            [inertial["inertia"][1], inertial["inertia"][3], inertial["inertia"][4]],
            [inertial["inertia"][2], inertial["inertia"][4], inertial["inertia"][5]],
        ],
        dtype=float,
    )
    return (
        float(inertial["mass"]),
        np.array([float(v) for v in inertial["xyz"]], dtype=float),
        rotation @ tensor @ rotation.T,
    )


def _raw_world(body: dict, raw_scene: dict, masses: dict, declared: dict) -> dict:
    """COM and tensor in *assembly* axes, computed without any link frame.

    This is the frame-independent view: it uses only the raw component
    placements and part tensors, so it catches a wrong link frame that a
    link-local comparison would happily agree with.
    """

    placements = {entry["name"]: entry["transform"] for entry in raw_scene.get("components") or []}
    total_mass = 0.0
    weighted = np.zeros(3)
    parts = []
    for component in body.get("components") or []:
        if component not in placements or component not in masses:
            raise ValueError(f"body {body.get('id')!r} references a component without raw readings: {component}")
        rotation, translation = _transform(placements[component])
        mass, com_part, inertia_part, _note = _component_mass(component, masses[component], declared)
        com_world = rotation @ com_part + translation
        inertia_world = rotation @ inertia_part @ rotation.T
        total_mass += mass
        weighted += mass * com_world
        parts.append((mass, com_world, inertia_world))
    if total_mass <= 0.0:
        raise ValueError(f"body {body.get('id')!r} has no positive mass")
    com_world = weighted / total_mass
    inertia_world = np.zeros((3, 3))
    for mass, com, inertia in parts:
        delta = com - com_world
        inertia_world += inertia + mass * (float(delta @ delta) * np.eye(3) - np.outer(delta, delta))
    return {"mass": total_mass, "com": com_world, "inertia": inertia_world}


def _root_pose(definition: dict, snapshot_root: Path) -> dict | None:
    """The root link frame, re-derived from the definition (and raw CAD when bound).

    Reading it here - instead of from the canonical model's own provenance -
    keeps the world check independent: the oracle applies the author's root pose
    to the canonical chain and compares the result with raw assembly readings.
    """

    source = (definition or {}).get("source") or {}
    bodies = list(source.get("bodies") or [])
    children = {str(joint.get("child")) for joint in source.get("joints") or []}
    roots = [body for body in bodies if str(body.get("name") or body.get("id")) not in children]
    if len(roots) != 1:
        return None
    frame = roots[0].get("frame") or {}
    reference = frame.get("coordinate_system")
    if reference:
        path = Path(snapshot_root) / "raw" / "coordinate_systems.json"
        if not path.is_file():
            raise ValueError("snapshot has no raw/coordinate_systems.json for the root frame")
        payload = read_json(path)
        matrix = (payload or {}).get(str(reference)) if isinstance(payload, dict) else None
        if matrix is None:
            raise ValueError(f"raw coordinate system {reference!r} is missing from the snapshot")
        rotation, translation = _transform(matrix)
        return {
            "xyz": translation,
            "rpy": np.array(_rpy_to_rpy_list(rotation), dtype=float),
            "source": f"cad_coordinate_system:{reference}",
        }
    xyz = np.array([float(value) for value in frame.get("xyz") or (0.0, 0.0, 0.0)], dtype=float)
    rpy = np.array([float(value) for value in frame.get("rpy") or (0.0, 0.0, 0.0)], dtype=float)
    return {"xyz": xyz, "rpy": rpy, "source": "author_declared" if frame else "identity"}


def _rpy_to_rpy_list(rotation: np.ndarray) -> list[float]:
    """Inverse of :func:`_rpy_matrix` for the URDF fixed-axis convention."""

    pitch = math.asin(max(-1.0, min(1.0, -float(rotation[2, 0]))))
    if abs(math.cos(pitch)) > 1e-9:
        roll = math.atan2(float(rotation[2, 1]), float(rotation[2, 2]))
        yaw = math.atan2(float(rotation[1, 0]), float(rotation[0, 0]))
    else:
        roll = 0.0
        yaw = math.atan2(-float(rotation[0, 1]), float(rotation[1, 1]))
    return [roll, pitch, yaw]


def _fk_world(canonical: dict, root_pose: dict | None = None) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """World pose of every link from the canonical chain at q=0."""

    links = {link["name"]: link for link in canonical.get("links") or []}
    children: dict[str, list[dict]] = {name: [] for name in links}
    incoming: set[str] = set()
    for joint in canonical.get("joints") or []:
        parent = joint.get("parent")
        child = joint.get("child")
        if parent in links and child in links:
            children[parent].append(joint)
            incoming.add(child)
    roots = sorted(set(links) - incoming)
    if len(roots) != 1:
        raise ValueError(f"canonical model must have exactly one kinematic root, found {roots}")
    root_name = roots[0]
    provenance = canonical.get("provenance") or {}
    root_pose = provenance.get("world_from_root") or {}
    rotation_root = _rpy_matrix(root_pose.get("rpy") or (0.0, 0.0, 0.0))
    translation_root = np.array([float(value) for value in (root_pose.get("xyz") or (0.0, 0.0, 0.0))], dtype=float)
    poses: dict[str, tuple[np.ndarray, np.ndarray]] = {root_name: (rotation_root, translation_root)}
    stack = [root_name]
    while stack:
        name = stack.pop()
        rotation_parent, translation_parent = poses[name]
        for joint in children[name]:
            rotation_joint = _rpy_matrix(joint.get("rpy") or (0.0, 0.0, 0.0))
            translation_joint = np.array([float(value) for value in (joint.get("xyz") or (0.0, 0.0, 0.0))], dtype=float)
            poses[joint["child"]] = (
                rotation_parent @ rotation_joint,
                rotation_parent @ translation_joint + translation_parent,
            )
            stack.append(joint["child"])
    return poses


def _close(actual, expected, *, atol: float, rtol: float) -> bool:
    return bool(np.allclose(np.asarray(actual, dtype=float), np.asarray(expected, dtype=float), atol=atol, rtol=rtol))


def verify_normalization(
    raw_scene: dict,
    definition: dict,
    snapshot_root: Path,
    canonical: dict,
) -> list[dict]:
    """Recompute every solid link from raw CAD data and compare with the model."""

    snapshot = Path(snapshot_root)
    bodies = list(((definition or {}).get("source") or {}).get("bodies") or [])
    declared = _declared_table(definition, snapshot)
    try:
        scene_raw, masses = _raw_inputs(snapshot)
    except (OSError, ValueError) as exc:
        return [
            _result(
                "source.normalization.raw_readings",
                False,
                details={"error": str(exc), "snapshot": str(snapshot)},
            )
        ]

    canonical_links = {link.get("name"): link for link in canonical.get("links") or []}
    results: list[dict] = []
    defined_entities: set[str] = set()
    checked_entities: list[str] = []
    world_pairs: list[tuple[str, dict]] = []
    for body in bodies:
        name = str(body.get("name") or body.get("id"))
        defined_entities.update(str(item) for item in body.get("components") or [])
        try:
            recomputed = _recompute_link(body, {}, scene_raw, masses, declared)
        except (KeyError, TypeError, ValueError) as exc:
            results.append(
                _result(f"source.normalization.{name}", False, expected=[name], checked=[], details={"error": str(exc)})
            )
            continue
        checked_entities.extend(note["component"] for note in recomputed["components"])
        with contextlib.suppress(KeyError, TypeError, ValueError):
            # frame-independent view: assembly axes, no link frame involved
            world_pairs.append((name, _raw_world(body, scene_raw, masses, declared)))
        link = canonical_links.get(name)
        if link is None:
            results.append(
                _result(
                    f"source.normalization.{name}",
                    False,
                    expected=[name],
                    checked=[],
                    details={"error": "canonical model has no such link"},
                )
            )
            continue
        try:
            mass, com, inertia = _canonical_in_link_frame(link)
        except (KeyError, TypeError, ValueError) as exc:
            results.append(
                _result(f"source.normalization.{name}", False, expected=[name], checked=[], details={"error": str(exc)})
            )
            continue
        mass_ok = _close([recomputed["mass"]], [mass], atol=MASS_ATOL, rtol=MASS_RTOL)
        com_ok = _close(recomputed["com"], com, atol=COM_ATOL, rtol=COM_RTOL)
        inertia_ok = _close(_tensor6(recomputed["inertia"]), _tensor6(inertia), atol=INERTIA_ATOL, rtol=INERTIA_RTOL)
        passed = mass_ok and com_ok and inertia_ok
        results.append(
            _result(
                f"source.normalization.{name}",
                passed,
                expected=[name],
                checked=[name],
                details={
                    "recomputed": {
                        "mass_kg": recomputed["mass"],
                        "com_m": recomputed["com"].tolist(),
                        "inertia": _tensor6(recomputed["inertia"]),
                    },
                    "canonical": {"mass_kg": mass, "com_m": com.tolist(), "inertia": _tensor6(inertia)},
                    "mass_ok": mass_ok,
                    "com_ok": com_ok,
                    "inertia_ok": inertia_ok,
                    "components": recomputed["components"],
                },
            )
        )

    # F1: the expected entity set comes from the raw component list, not from the
    # derived scene, so an instance that both the freeze path and the author list
    # dropped is still caught.
    raw_entities = {str(entry.get("name")) for entry in scene_raw.get("components") or [] if entry.get("name")}
    counts = {entity: checked_entities.count(entity) for entity in set(checked_entities)}
    duplicated = sorted(entity for entity, count in counts.items() if count > 1)
    results.append(
        _result(
            "source.normalization.entities",
            bool(raw_entities)
            and set(checked_entities) == raw_entities
            and defined_entities == raw_entities
            and not duplicated,
            expected=raw_entities,
            checked=checked_entities,
            details={
                "duplicated": duplicated,
                "unexpected": sorted(raw_entities - defined_entities),
                "undefined": sorted(defined_entities - raw_entities),
            },
        )
    )

    # F3: every applied author mass must carry evidence for its value.
    if declared:
        missing_evidence = sorted(name for name, entry in declared.items() if not entry.get("evidence"))
        results.append(
            _result(
                "source.normalization.declared_masses",
                not missing_evidence,
                expected=sorted(declared),
                checked=sorted(name for name in declared if name not in missing_evidence),
                details={"without_evidence": missing_evidence},
            )
        )

    # F1/F2: rediscover the mass-source contract from raw inputs, independently of
    # whatever the generator reported.  Two failure modes are invisible to the
    # per-link comparison because both sides would use the same wrong mass:
    #  * a component that is included but has no declared mass (documented_table),
    #    so its CAD default-density placeholder silently becomes the model mass;
    #  * declared entries that match no component (typo/renamed instance);
    #  * in strict CAD mode, a component whose raw reading says its material is
    #    unverified, so its CAD mass is a default-density placeholder.
    material_source = str(((definition or {}).get("source") or {}).get("material_source") or "cad")
    included = sorted(defined_entities)
    missing_declared = (
        sorted(name for name in included if name not in declared) if material_source == "documented_table" else []
    )
    unknown_declared = sorted(name for name in declared if name not in defined_entities)
    unverified_cad: list[str] = []
    if material_source != "documented_table":
        for name in included:
            payload = masses.get(name)
            reference = payload.get("reference") if isinstance(payload, dict) else None
            assignment = reference.get("material_assignment") if isinstance(reference, dict) else None
            if name not in declared and isinstance(assignment, dict) and assignment.get("unverified_reason"):
                unverified_cad.append(name)
    mass_provenance_ok = not (missing_declared or unknown_declared or unverified_cad)
    mass_details: dict[str, object] = {
        "material_source": material_source,
        "included": included,
        "declared": sorted(declared),
        "missing_declared": missing_declared,
        "unknown_declared": unknown_declared,
        "cad_mass_without_verified_material": unverified_cad,
    }
    if declared:
        mass_details["inertia_model"] = "scaled_cad_uniform_density"
        mass_details["inertia_model_scope"] = (
            "declared mass scales the CAD uniform-density tensor (used/raw); it is a shape-preserving "
            "estimate, not a measured inertia - valid as an order-of-magnitude model for printed parts, "
            "not for catalogue parts whose mass distribution differs from their geometry"
        )
        mass_details["scaled_components"] = sorted(name for name in included if name in declared)
    results.append(
        _result(
            "source.normalization.mass_provenance",
            mass_provenance_ok and bool(included),
            expected=included,
            checked=sorted(set(included) - set(missing_declared) - set(unverified_cad)),
            details=mass_details,
        )
    )
    evidence_check = _mass_evidence_check(definition, snapshot, included, declared)
    if evidence_check is not None:
        results.append(evidence_check)
    results.append(_mass_closure_check(snapshot))

    # F4: reconcile the raw assembly-frame quantities with the canonical chain at
    # q=0.  This is what catches a body frame that disagrees with the joint
    # origins - a link-local comparison cannot see it.
    try:
        derived_root = _root_pose(definition, snapshot)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        results.append(_result("source.normalization.world", False, details={"error": str(exc)}))
        return results
    canonical_root = (canonical.get("provenance") or {}).get("world_from_root")
    if derived_root is not None and canonical_root is not None:
        same_xyz = _close(canonical_root.get("xyz") or [], derived_root["xyz"], atol=COM_ATOL, rtol=COM_RTOL)
        same_rpy = _close(canonical_root.get("rpy") or [], derived_root["rpy"], atol=1e-9, rtol=1e-7)
        if not (same_xyz and same_rpy):
            results.append(
                _result(
                    "source.normalization.root_frame",
                    False,
                    expected=[derived_root["source"]],
                    checked=[str(canonical_root.get("source"))],
                    details={
                        "derived": {
                            "xyz": np.asarray(derived_root["xyz"]).tolist(),
                            "rpy": np.asarray(derived_root["rpy"]).tolist(),
                            "source": derived_root["source"],
                        },
                        "canonical": canonical_root,
                    },
                )
            )
            return results
    try:
        poses = _fk_world(canonical, derived_root)
    except (KeyError, TypeError, ValueError) as exc:
        results.append(_result("source.normalization.world", False, details={"error": str(exc)}))
        return results
    world_details: dict[str, dict] = {}
    world_ok = True
    for name, raw_world in world_pairs:
        pose = poses.get(name)
        link = canonical_links.get(name)
        if pose is None or link is None:
            world_details[name] = {"error": "canonical model has no such link"}
            world_ok = False
            continue
        rotation_link, translation_link = pose
        try:
            _mass, com_link, inertia_link = _canonical_in_link_frame(link)
        except (KeyError, TypeError, ValueError) as exc:
            world_details[name] = {"error": str(exc)}
            world_ok = False
            continue
        com_world = rotation_link @ com_link + translation_link
        inertia_world = rotation_link @ inertia_link @ rotation_link.T
        com_ok = _close(com_world, raw_world["com"], atol=COM_ATOL, rtol=COM_RTOL)
        inertia_ok = _close(
            _tensor6(inertia_world), _tensor6(raw_world["inertia"]), atol=INERTIA_ATOL, rtol=INERTIA_RTOL
        )
        world_ok = world_ok and com_ok and inertia_ok
        world_details[name] = {
            "com_ok": com_ok,
            "inertia_ok": inertia_ok,
            "raw_com_m": raw_world["com"].tolist(),
            "canonical_com_world_m": com_world.tolist(),
            "raw_inertia": _tensor6(raw_world["inertia"]),
            "canonical_inertia_world": _tensor6(inertia_world),
        }
    results.append(
        _result(
            "source.normalization.world",
            bool(world_details) and world_ok,
            expected=sorted(world_details),
            checked=sorted(world_details),
            details={
                "root_pose": (
                    None
                    if derived_root is None
                    else {
                        "xyz": np.asarray(derived_root["xyz"]).tolist(),
                        "rpy": np.asarray(derived_root["rpy"]).tolist(),
                        "source": derived_root["source"],
                    }
                ),
                "links": world_details,
            },
        )
    )
    return results
