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
# Same API, different selection scope, different sign convention: see
# scene.SCOPE_CONVENTIONS (kept independent on purpose - this is the oracle).
_SCOPE_CONVENTIONS = {
    "part_document": "solidworks_positive",
    "assembly_component_group": "solidworks_standard",
}
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


def _finite_positive(value: Any, what: str) -> float:
    """A finite, positive number, or a ``ValueError`` naming the field."""

    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{what} is not finite and positive")
    return number


def _finite_non_negative(value: Any, what: str) -> float:
    """A finite, non-negative number, or a ``ValueError`` naming the field."""

    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{what} is not finite and non-negative")
    return number


def _finite_vector(value: Any, what: str) -> np.ndarray:
    """A finite 3-vector, or a ``ValueError`` naming the field."""

    array = np.asarray([float(item) for item in value], dtype=float)
    if array.shape != (3,):
        raise ValueError(f"{what} is not a 3-vector")
    if not np.isfinite(array).all():
        raise ValueError(f"{what} is not finite")
    return array


def _finite_tensor(value: Any, what: str) -> np.ndarray:
    """A finite 3x3 tensor, or a ``ValueError`` naming the field."""

    array = np.asarray([[float(item) for item in row] for row in value], dtype=float)
    if array.shape != (3, 3):
        raise ValueError(f"{what} is not a 3x3 tensor")
    if not np.isfinite(array).all():
        raise ValueError(f"{what} is not finite")
    return array


def _append_note(details: dict[str, Any], note: str) -> None:
    """Append one sentence to the single advisory string the CLI prints."""

    existing = details.get("advisory")
    details["advisory"] = f"{existing}  {note}" if existing else note


def _component_context_findings(
    payload: dict[str, Any],
    material_source: str,
    assembly_mass: float | None,
    scene_masses: dict[str, float] | None,
) -> tuple[dict[str, Any], str | None]:
    """Read the recorded component-context evidence; return ``(details, error)``.

    The section records, per component instance, the mass the *assembly context* uses next to the
    part-document basis and the three override flags.  Totals use only the disjoint depth-0 rows;
    nested rows exist to detect overrides a clean parent would otherwise hide.

    A pure-CAD model reads part documents, so it cannot represent any instance override (mass, COM
    or inertia), and it cannot claim equivalence while the evidence is unavailable or incomplete.
    A documented table may legitimately choose either basis, so those findings are notes, never
    enforcement.  Malformed rows and inconsistent status/error pairs are a defect, exactly like a
    malformed closure record.
    """

    context = payload.get("component_context")
    if context is None:
        return {}, None
    if not isinstance(context, dict):
        return {}, "component mass context is not an object"
    status = str(context.get("status") or "")
    if status == "unavailable":
        return (
            {
                "component_context": {
                    "status": "unavailable",
                    "reason": str(context.get("reason") or "unknown"),
                    "message": str(context.get("message") or ""),
                }
            },
            "the component mass context could not be read" if material_source != "documented_table" else None,
        )
    if status not in {"recorded", "partial"}:
        return {}, f"component mass context status is {status!r}"
    rows = context.get("instances")
    if not isinstance(rows, list) or not rows:
        return {}, "component mass context has no instances"
    row_errors = context.get("errors")
    if not isinstance(row_errors, list):
        return {}, "component mass context errors are not a list"
    if status == "recorded" and row_errors:
        return {}, "component mass context is recorded but carries row errors"
    if status == "partial" and not row_errors:
        return {}, "component mass context is partial but carries no row errors"
    entries: list[dict[str, Any]] = []
    try:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("component mass context row is not an object")
            name = str(row.get("name") or "").strip()
            if not name:
                raise ValueError("component mass context row has no name")
            raw_depth = row.get("depth")
            if not isinstance(raw_depth, int) or isinstance(raw_depth, bool):
                raise ValueError(f"component mass context row for {name} has no integer depth")
            depth = raw_depth
            expected_depth = name.count("/")
            if depth != expected_depth:
                raise ValueError(f"component mass context row for {name} has depth {depth}, expected {expected_depth}")
            parent = name.rsplit("/", 1)[0] if "/" in name else None
            if row.get("parent") != parent:
                raise ValueError(
                    f"component mass context row for {name} has parent {row.get('parent')!r}, expected {parent!r}"
                )
            overrides = row.get("overrides")
            if not isinstance(overrides, dict):
                raise ValueError(f"component mass context row for {name} has no override flags")
            if not {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"} <= set(overrides):
                raise ValueError(f"component mass context row for {name} is missing override flags")
            flags: dict[str, bool] = {}
            for key, value in overrides.items():
                if not isinstance(value, bool):
                    raise ValueError(f"component mass context row for {name} has a non-boolean flag {key!r}")
                flags[str(key)] = value
            document_mass = row.get("document_basis_mass_kg")
            volume = row.get("context_volume_m3")
            entries.append(
                {
                    "name": name,
                    "depth": depth,
                    "parent": parent,
                    "document_type": str(row.get("document_type") or ""),
                    "context_mass_kg": _finite_positive(
                        row["context_mass_kg"], f"component mass context mass for {name}"
                    ),
                    "document_basis_mass_kg": (
                        None
                        if document_mass is None
                        else _finite_non_negative(document_mass, f"component document mass for {name}")
                    ),
                    "context_volume_m3": (
                        None if volume is None else _finite_non_negative(volume, f"component volume for {name}")
                    ),
                    "overrides": flags,
                }
            )
    except (KeyError, TypeError, ValueError) as exc:
        return {}, f"malformed component mass context: {exc}"
    names = [entry["name"] for entry in entries]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        return {}, f"malformed component mass context: duplicated instance rows {duplicates[:5]}"
    top_level = [entry for entry in entries if entry["depth"] == 0]
    if not top_level:
        return {}, "malformed component mass context: no top-level rows"
    mass_overridden = [entry["name"] for entry in entries if entry["overrides"]["OverrideMass"]]
    com_overridden = [entry["name"] for entry in entries if entry["overrides"]["OverrideCenterOfMass"]]
    inertia_overridden = [entry["name"] for entry in entries if entry["overrides"]["OverrideMomentsOfInertia"]]
    any_overridden = [entry["name"] for entry in entries if any(entry["overrides"].values())]
    context_total = sum(entry["context_mass_kg"] for entry in top_level)
    recorded_document_total = sum(
        entry["document_basis_mass_kg"] or 0.0 for entry in top_level if entry["document_basis_mass_kg"] is not None
    )
    missing_document_basis = [entry["name"] for entry in top_level if entry["document_basis_mass_kg"] is None]
    leaf_rows = [entry["name"] for entry in entries if entry["document_type"] == "part"]
    if scene_masses is not None:
        expected_nodes = _expected_instance_nodes(scene_masses)
        node_names = {entry["name"] for entry in entries}
        missing_nodes = sorted(expected_nodes - node_names)[:20]
        unexpected_nodes = sorted(node_names - expected_nodes)[:20]
        missing_parents = sorted(
            entry["name"] for entry in entries if entry["parent"] is not None and entry["parent"] not in node_names
        )[:20]
        wrong_types = sorted(
            entry["name"] for entry in entries if entry["name"] in scene_masses if entry["document_type"] != "part"
        ) or sorted(
            entry["name"]
            for entry in entries
            if entry["name"] not in scene_masses and entry["document_type"] != "assembly"
        )
        if wrong_types:
            return {}, f"malformed component mass context: invalid node types {wrong_types[:5]}"
        equivalence = []
        cached_mismatches = []
        basis_by_node: dict[str, float] = {}
        for entry in entries:
            basis = sum(
                mass
                for leaf, mass in scene_masses.items()
                if leaf == entry["name"] or leaf.startswith(entry["name"] + "/")
            )
            basis_by_node[entry["name"]] = basis
            recorded_basis = entry["document_basis_mass_kg"]
            if recorded_basis is not None and not _mass_close(recorded_basis, basis):
                cached_mismatches.append(
                    {
                        "name": entry["name"],
                        "recorded_document_basis_mass_kg": recorded_basis,
                        "recomputed_document_basis_mass_kg": basis,
                    }
                )
            if not _mass_close(entry["context_mass_kg"], basis):
                equivalence.append(
                    {
                        "name": entry["name"],
                        "context_mass_kg": entry["context_mass_kg"],
                        "document_basis_mass_kg": basis,
                    }
                )
        if cached_mismatches:
            return {}, (
                "malformed component mass context: the cached document basis contradicts the raw "
                f"readings for {len(cached_mismatches)} node(s) (examples: {cached_mismatches[:3]})"
            )
        coverage: dict[str, Any] = {
            "known": True,
            "expected_nodes": len(expected_nodes),
            "recorded_nodes": len(node_names),
            "missing_nodes": missing_nodes,
            "unexpected_nodes": unexpected_nodes,
            "missing_parent_rows": missing_parents,
            "effective_vs_document_mismatches": equivalence[:20],
        }
        recomputed_document_total: float | None = sum(basis_by_node.get(entry["name"], 0.0) for entry in top_level)
    else:
        coverage = {"known": False}
        equivalence = []
        recomputed_document_total = None
    recorded_assembly = context.get("assembly_mass_kg")
    details = {
        "component_context": {
            "status": status,
            "instances": len(entries),
            "top_level_instances": len(top_level),
            "leaf_instances": len(leaf_rows),
            "mass_overridden": len(mass_overridden),
            "mass_overridden_examples": mass_overridden[:5],
            "com_overridden": len(com_overridden),
            "inertia_overridden": len(inertia_overridden),
            "any_override": len(any_overridden),
            "document_total_kg": recomputed_document_total,
            "recorded_document_total_kg": recorded_document_total,
            "context_total_kg": context_total,
            "context_minus_document_kg": (
                None if recomputed_document_total is None else context_total - recomputed_document_total
            ),
            "assembly_mass_kg": assembly_mass,
            "context_minus_assembly_kg": None if assembly_mass is None else context_total - assembly_mass,
            "recorded_assembly_mass_kg": recorded_assembly,
            "top_level_instances_without_document_basis": missing_document_basis[:20],
            "coverage": coverage,
            "effective_vs_document_mismatches": len(equivalence),
            "row_errors": row_errors,
        }
    }
    incomplete: list[str] = []
    if status == "partial" or row_errors:
        incomplete.append(f"{len(row_errors)} row error(s)")
    if scene_masses is None:
        incomplete.append("no raw scene and mass readings to prove node coverage against")
    else:
        if coverage["missing_nodes"]:
            incomplete.append(f"{len(coverage['missing_nodes'])} expected instance node(s) not recorded")
        if coverage["unexpected_nodes"]:
            incomplete.append(f"{len(coverage['unexpected_nodes'])} unexpected node row(s)")
        if coverage["missing_parent_rows"]:
            incomplete.append(f"{len(coverage['missing_parent_rows'])} node row(s) whose parent row is absent")
    if material_source != "documented_table":
        if any_overridden:
            return details, (
                "the CAD assembly carries component-level overrides "
                f"({len(any_overridden)} instance(s): {len(mass_overridden)} mass, "
                f"{len(com_overridden)} center of mass, {len(inertia_overridden)} inertia) that the "
                "part-document readings do not include; a pure-CAD (material_source=cad) model "
                "cannot represent them.  Record the effective properties with matching evidence — "
                "a documented mass only rescales the CAD tensor at an unchanged center of mass — or "
                "remove the overrides in CAD"
            )
        if isinstance(coverage, dict) and coverage.get("effective_vs_document_mismatches"):
            examples = coverage["effective_vs_document_mismatches"][:3]
            return details, (
                "the assembly context's effective mass differs from the selected part-document "
                f"reading for {len(equivalence)} node(s) with no recorded override "
                f"(examples: {examples}); a pure-CAD (material_source=cad) model cannot claim "
                "source equivalence — record the effective masses through source.documented_masses"
            )
        if incomplete:
            return details, (
                "the component mass context is incomplete (" + "; ".join(incomplete) + "), so a "
                "pure-CAD (material_source=cad) model cannot prove its part-document masses are the "
                "effective ones"
            )
    if any_overridden:
        top_level_overridden = sum(1 for entry in top_level if any(entry["overrides"].values()))
        _append_note(
            details,
            "the assembly context carries overrides on "
            f"{len(any_overridden)} of {len(entries)} recorded component instances "
            f"({len(mass_overridden)} mass, {len(com_overridden)} COM, {len(inertia_overridden)} "
            f"inertia; {top_level_overridden} at the top level); the assembly reading uses the "
            "context values while the leaf readings use part-document values — no cause is inferred "
            "and no mass is distributed automatically",
        )
    if equivalence:
        _append_note(
            details,
            "the assembly-context effective mass differs from the recomputed part-document basis "
            f"for {len(equivalence)} node(s); the documented table may choose either basis — no mass "
            "is changed automatically",
        )
    if incomplete:
        _append_note(details, "component mass context is incomplete: " + "; ".join(incomplete))
    return details, None


def _scene_leaf_masses(snapshot: Path) -> dict[str, float] | None:
    """Leaf instance name to raw document mass, from the snapshot's own readings.

    The context guard recomputes every node's document basis from these values; it never uses a
    recorded sum.  A snapshot without both raw files cannot support the source-equivalence claim.
    """

    scene_path = Path(snapshot) / "raw" / "scene_raw.json"
    masses_path = Path(snapshot) / "raw" / "mass_properties.json"
    if not scene_path.is_file() or not masses_path.is_file():
        return None
    try:
        scene_raw = read_json(scene_path)
        masses = read_json(masses_path)
    except (OSError, ValueError, PipelineError):
        return None
    components = scene_raw.get("components") if isinstance(scene_raw, dict) else None
    if not isinstance(components, list) or not isinstance(masses, dict):
        return None
    found: dict[str, float] = {}
    for entry in components:
        if not isinstance(entry, dict) or not entry.get("name"):
            return None
        name = str(entry["name"])
        payload = masses.get(name)
        if not isinstance(payload, dict):
            return None
        try:
            mass = _finite_positive(payload["mass"], f"scene leaf mass for {name}")
        except (KeyError, TypeError, ValueError):
            return None
        found[name] = mass
    return found or None


def _expected_instance_nodes(scene_masses: dict[str, float]) -> set[str]:
    """Every ancestor prefix of every scene leaf instance, the full expected node coverage."""

    nodes: set[str] = set()
    for leaf in scene_masses:
        segments = leaf.split("/")
        nodes.update("/".join(segments[: index + 1]) for index in range(len(segments)))
    return nodes


def _mass_close(first: float, second: float) -> bool:
    """Source-equivalence tolerance for an effective mass against its document basis."""

    return abs(first - second) <= max(1e-12, CLOSURE_MASS_RTOL * max(abs(first), abs(second)))


def _mass_only_closure_check(
    path: Path, payload: dict[str, Any], material_source: str, scene_masses: dict[str, float] | None
) -> dict:
    """Evaluate the mass-only closure the legacy assembly API produced.

    ``Extension.GetMassProperties2`` answers with a vector whose mass the M3.0 recovery reports and
    the 2026-09-29 native pairing both corroborate; its COM and inertia are not trusted per
    document, so the capture marks them ``not_inferred`` and this check reads nothing but the two
    masses (the recorded volumes stay visible as context).  The comparison is therefore an advisory
    on the mass ratio only — but a mass that is absent, non-numeric or not finite is corrupt
    evidence, exactly like a malformed full record, and fails.
    """

    try:
        top = payload.get("top_level")
        leaf = payload.get("leaf_total")
        if not isinstance(top, dict) or not isinstance(leaf, dict):
            raise ValueError("mass closure readings are not objects")
        top_mass = _finite_positive(cast("dict[str, Any]", top)["mass"], "mass closure top mass")
        leaf_mass = _finite_positive(cast("dict[str, Any]", leaf)["mass"], "mass closure leaf mass")
    except (KeyError, TypeError, ValueError, OSError, PipelineError) as exc:
        return _result(
            "source.normalization.mass_closure",
            False,
            details={"error": str(exc), "record": str(path), "mode": "mass_only"},
        )
    mass_rel = abs(top_mass - leaf_mass) / max(abs(top_mass), abs(leaf_mass), 1e-12)
    details: dict[str, Any] = {
        "mode": "mass_only",
        "top_level_mass_kg": top_mass,
        "leaf_total_mass_kg": leaf_mass,
        "delta": {"mass_rel": mass_rel},
    }
    for name, source in (("top_level_volume_m3", top), ("leaf_total_volume_m3", leaf)):
        with contextlib.suppress(KeyError, TypeError, ValueError):
            volume = float(source["volume_m3"])
            if math.isfinite(volume):
                details[name] = volume
    if mass_rel > CLOSURE_MASS_RTOL:
        details["advisory"] = (
            "the assembly document's own mass disagrees with the recombined leaf readings "
            f"(mass {top_mass:.9g} kg vs {leaf_mass:.9g} kg, {mass_rel:.3g} relative); no cause is "
            "inferred here — review the CAD and any declared masses (this capture's legacy API "
            "reported mass only, so COM and inertia were not compared)"
        )
    context_details, context_error = _component_context_findings(payload, material_source, top_mass, scene_masses)
    details.update(context_details)
    if context_error is not None:
        return _result(
            "source.normalization.mass_closure",
            False,
            details={**details, "error": context_error, "mode": "mass_only"},
        )
    return _result("source.normalization.mass_closure", True, details=details)


def _mass_closure_check(snapshot: Path, material_source: str = "cad") -> dict:
    """Assembly-versus-leaf mass closure, plus the component-context source guard.

    The record is capture-time evidence: the assembly document's own mass properties next to the
    parallel-axis combination of the leaf readings.  The whole-assembly delta is an *advisory*:
    a mismatch lands in ``details.advisory`` and the model still qualifies, and snapshots without
    the record are ``not_applicable``.  Invalid or unsupported *source policy* is a blocker: a
    recorded component instance override, or an effective mass the selected part documents cannot
    explain under ``material_source: cad``, or malformed/incomplete evidence, fails the check.  A
    ``mass_only`` record (from a build whose legacy assembly API reports mass alone) is evaluated
    by :func:`_mass_only_closure_check` on that mass only, with the same source guard.
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
        scene_masses = _scene_leaf_masses(snapshot)
        if payload.get("status") == "unavailable":
            evidence: dict[str, Any] = {
                "reason": "the capture could not read the assembly mass properties",
                "unavailable": str(payload.get("reason") or "unknown"),
                "message": str(payload.get("message") or ""),
            }
            # The override evidence stands on its own: an unavailable assembly reading must not
            # let a pure-CAD model earn equivalence by omission.
            context_details, context_error = _component_context_findings(payload, material_source, None, scene_masses)
            evidence.update(context_details)
            if context_error is not None:
                return _result(
                    "source.normalization.mass_closure",
                    False,
                    details={**evidence, "error": context_error},
                )
            return _result(
                "source.normalization.mass_closure",
                True,
                status="not_applicable",
                details=evidence,
            )
        if str(payload.get("mode") or "full") == "mass_only":
            return _mass_only_closure_check(path, payload, material_source, scene_masses)
        top = payload.get("top_level")
        leaf = payload.get("leaf_total")
        if not isinstance(top, dict) or not isinstance(leaf, dict):
            raise ValueError("mass closure readings are not objects")
        top = cast("dict[str, Any]", top)
        leaf = cast("dict[str, Any]", leaf)
        top_mass = _finite_positive(top["mass"], "mass closure top mass")
        leaf_mass = _finite_positive(leaf["mass"], "mass closure leaf mass")
        top_com = _finite_vector(top["com"], "mass closure top COM")
        leaf_com = _finite_vector(leaf["com"], "mass closure leaf COM")
        top_inertia = _finite_tensor(top["inertia"], "mass closure top inertia")
        leaf_inertia = _finite_tensor(leaf["inertia"], "mass closure leaf inertia")
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
            f"(mass {top_mass:.9g} kg vs {leaf_mass:.9g} kg, {mass_rel:.3g} relative); no cause is "
            "inferred here — review the CAD and any declared masses"
        )
    context_details, context_error = _component_context_findings(payload, material_source, top_mass, scene_masses)
    details.update(context_details)
    if context_error is not None:
        return _result(
            "source.normalization.mass_closure",
            False,
            details={**details, "error": context_error},
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
    标准张量的非对角项是它们的相反数。``solidworks_standard`` 是部件组选择的记法：读数
    本身就是标准张量（2026-10-06 组审计）。读数带 scope 时以 scope 为准，且必须与显式
    约定一致；原始读数保持原样，缺约定的原生数据不猜。
    """

    matrix = np.array([[float(value) for value in row] for row in payload["inertia"]], dtype=float)
    if matrix.shape != (3, 3):
        raise ValueError(f"component {component} has a non-3x3 inertia matrix")
    scale = max(1.0, float(np.abs(matrix).max()))
    if not np.allclose(matrix, matrix.T, atol=1e-12 * scale, rtol=0.0):
        raise ValueError(f"component {component} has a non-symmetric inertia matrix")
    reference = payload.get("reference") or {}
    convention = reference.get("product_convention")
    scope = reference.get("scope")
    if scope is not None:
        expected = _SCOPE_CONVENTIONS.get(str(scope))
        if expected is None:
            raise ValueError(
                f"component {component} has an unknown inertia measurement scope: {scope!r}"
            )
        if convention is not None and str(convention) != expected:
            raise ValueError(
                f"component {component} inertia scope and product_convention disagree: "
                f"{scope!r} vs {convention!r}"
            )
        convention = expected
    if convention == "solidworks_positive":
        tensor = matrix.copy()
        off_diagonal = ~np.eye(3, dtype=bool)
        tensor[off_diagonal] = -matrix[off_diagonal]
        return tensor
    if convention == "solidworks_standard":
        return matrix
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
    results.append(_mass_closure_check(snapshot, material_source))

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
