"""Independent re-derivation of the normalized robot from raw CAD readings.

The oracle deliberately shares no code with the normalization path: it reads the
snapshot's raw component transforms and mass tensors again and recomputes sum(m),
the combined COM and the full inertia tensor with plain numpy. Agreement with the
canonical model is therefore evidence about the numbers, not about the code path
that produced them.
"""

from __future__ import annotations

import math
from pathlib import Path
from collections.abc import Sequence
from typing import Any

import numpy as np

from ...geometry.native_transform import rigid_matrix_from_row_major
from .jsonio import read_json
from .native_limits import NativeLimitInvalidError, finite_number
from ...io import PipelineError
from ...schemas import EFFECTIVE_INSTANCES_SCHEMA as EFFECTIVE_SCHEMA

INERTIA_ORDER = ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")
# Same API, selection scopes measured on the analytic native fixture
# (2026-10-06, SolidWorks 34.0.0): part documents and component groups both
# answer in standard notation (see scene.SCOPE_CONVENTIONS; kept independent on
# purpose - this is the oracle).
_SCOPE_CONVENTIONS = {
    "part_document": "solidworks_standard",
    "assembly_component_group": "solidworks_standard",
    "assembly_document": "solidworks_standard",
}
EFFECTIVE_CODE_SHEET_ONLY = "cad_surface_only_effective_instance"
EFFECTIVE_CODE_INSTANCE = "cad_instance_override_effective"
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
    values = np.array(rigid_matrix_from_row_major(matrix), dtype=float)
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


#: Advisory thresholds for the capture-time mass closure (not gates).
CLOSURE_MASS_RTOL = 1e-6


def _finite_positive(value: Any, what: str) -> float:
    """A finite, positive number, or a ``ValueError`` naming the field."""

    try:
        number = finite_number(value)
    except NativeLimitInvalidError as error:
        raise ValueError(f"{what} is not a finite number") from error
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{what} is not finite and positive")
    return number


def _finite_non_negative(value: Any, what: str) -> float:
    """A finite, non-negative number, or a ``ValueError`` naming the field."""

    try:
        number = finite_number(value)
    except NativeLimitInvalidError as error:
        raise ValueError(f"{what} is not a finite number") from error
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{what} is not finite and non-negative")
    return number


def _finite_vector(value: Any, what: str) -> np.ndarray:
    """A finite 3-vector, or a ``ValueError`` naming the field."""

    try:
        array = np.asarray([finite_number(item) for item in value], dtype=float)
    except (NativeLimitInvalidError, TypeError) as error:
        raise ValueError(f"{what} is not a finite 3-vector") from error
    if array.shape != (3,):
        raise ValueError(f"{what} is not a 3-vector")
    if not np.isfinite(array).all():
        raise ValueError(f"{what} is not finite")
    return array


def _append_note(details: dict[str, Any], note: str) -> None:
    """Append one sentence to the single advisory string the CLI prints."""

    existing = details.get("advisory")
    details["advisory"] = f"{existing}  {note}" if existing else note


def _component_context_findings(
    payload: dict[str, Any],
    assembly_mass: float | None,
    scene_masses: dict[str, float] | None,
    covered: set[str] | None = None,
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
            "the component mass context could not be read",
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
    covered_names = {str(name) for name in (covered or set())}
    if any_overridden and not set(any_overridden) <= covered_names:
        return details, (
            "the CAD assembly carries component-level overrides "
            f"({len(any_overridden)} instance(s): {len(mass_overridden)} mass, "
            f"{len(com_overridden)} center of mass, {len(inertia_overridden)} inertia) that the "
            "part-document readings do not include; a CAD model cannot represent them. "
            "Correct the effective CAD material or override readings"
        )

    def _explained_by_cover(name: str) -> bool:
        """An overridden/effective instance at, inside or above the node explains its divergence."""

        return any(
            covered == name or name.startswith(covered + "/") or covered.startswith(name + "/")
            for covered in covered_names
        )

    mismatches = [row for row in equivalence if not _explained_by_cover(str(row.get("name")))]
    if isinstance(coverage, dict) and mismatches:
        examples = mismatches[:3]
        return details, (
            "the assembly context's effective mass differs from the selected part-document "
            f"reading for {len(mismatches)} node(s) with no recorded override "
            f"(examples: {examples}); the CAD model cannot claim source equivalence; "
            "correct the effective CAD material/override readings"
        )
    if incomplete:
        return details, (
            "the component mass context is incomplete (" + "; ".join(incomplete) + "), so a "
            "CAD model cannot prove its part-document masses are the effective ones"
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
            f"for {len(equivalence)} node(s); no basis is chosen and no mass is changed automatically",
        )
    if incomplete:
        _append_note(details, "component mass context is incomplete: " + "; ".join(incomplete))
    return details, None


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


def _link_frame(body: dict, raw_scene: dict) -> tuple[np.ndarray, np.ndarray]:
    frame = body.get("frame")
    if not isinstance(frame, dict) or set(frame) != {"coordinate_system"}:
        raise ValueError(f"body {body.get('name')!r} has no native datum binding")
    datums = raw_scene.get("coordinate_systems") or {}
    reference = frame["coordinate_system"]
    if reference not in datums:
        raise ValueError(f"raw native datum {reference!r} is missing")
    return _transform(datums[reference])


def _raw_tensor(component: str, payload: dict) -> np.ndarray:
    """Read the signed native tensor independently of the generator."""

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
            raise ValueError(f"component {component} has an unknown inertia measurement scope: {scope!r}")
        if convention is not None and str(convention) != expected:
            raise ValueError(
                f"component {component} inertia scope and product_convention disagree: {scope!r} vs {convention!r}"
            )
        convention = expected
    # Fail-closed backstop for the post-2026-10-15 layering: the capture
    # normalizes at the single evidence throat; a record that still carries
    # any other convention never passed normalization and must not be consumed.
    if convention != "solidworks_standard":
        raise ValueError(f"component {component} has an unsupported product_convention: {convention!r}")
    return matrix


def _component_mass(component: str, payload: dict) -> tuple[float, np.ndarray, np.ndarray, dict]:
    """Return (mass, com_part, inertia_part, provenance) independently of the builder."""

    mass = float(payload["mass"])
    com = np.array([float(value) for value in payload["com"]], dtype=float)
    inertia = _raw_tensor(component, payload)
    note = {"component": component, "raw_mass_kg": mass, "used_mass_kg": mass, "scale": 1.0}
    return mass, com, inertia, note


def load_effective_instances(evidence_root) -> dict[str, dict]:
    """The sealed effective-instance warnings, keyed by occurrence name.

    Both the sheet-only leaf fallback and non-leaf (group) native overrides are
    recorded here; the raw math stays in ``mass_properties.json`` rows and
    ``scene_raw.notes.effective_instances``, and this record binds them with
    verbatim measured values.  The record lives in the delivery capture block
    ``effective_instances``; the caller may pass the delivery root or its
    ``evidence/`` directory.
    """

    root = Path(evidence_root)
    base = root.parent if root.name == "evidence" else root
    capture_path = base / "evidence" / "capture.json"
    if not capture_path.is_file():
        raise PipelineError("the CAD capture has no capture record")
    capture = read_json(capture_path)
    payload = capture.get("effective_instances") if isinstance(capture, dict) else None
    if not isinstance(payload, dict):
        raise PipelineError("the CAD capture has no effective instance record")
    if payload.get("schema_version") != EFFECTIVE_SCHEMA:
        raise PipelineError("effective instance record schema changed")
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        raise PipelineError("effective instance record has no warnings list")
    entries: dict[tuple[str, str], dict] = {}
    for entry in warnings:
        if not isinstance(entry, dict):
            raise PipelineError("effective instance record carries a malformed warning")
        component = entry.get("component")
        code = entry.get("code")
        if not isinstance(component, str) or not component or not isinstance(code, str) or not code:
            raise PipelineError("effective instance record names an unknown or repeated occurrence")
        if (component, code) in entries:
            raise PipelineError("effective instance record names an unknown or repeated occurrence")
        entries[(component, code)] = entry
    return entries


def _notes_effective(raw_scene: dict) -> dict[str, dict]:
    notes = raw_scene.get("notes")
    entries = notes.get("effective_instances") if isinstance(notes, dict) else None
    if entries is None:
        return {}
    if not isinstance(entries, dict):
        raise ValueError("scene_raw effective instances are malformed")
    return {str(name): entry for name, entry in entries.items()}


def effective_math_map(masses: dict, raw_scene: dict) -> dict[str, dict]:
    """Verbatim effective measurements (assembly axes) keyed by occurrence.

    Leaf rows carry their values inline; non-leaf occurrences are recorded in
    ``scene_raw.notes.effective_instances``. Nothing is transformed here - the
    conversion uses the recorded placement independently in each consumer.
    """

    math: dict[str, dict] = {}
    for name, row in masses.items():
        if isinstance(row, dict) and row.get("mode") == "effective_instance":
            math[str(name)] = {"mass": row["mass"], "com": row["com"], "inertia": row["inertia"]}
    for name, entry in _notes_effective(raw_scene).items():
        if not isinstance(entry, dict) or not {"mass", "com", "inertia"} <= set(entry):
            raise ValueError(f"effective instance {name!r} is malformed")
        values = {"mass": entry["mass"], "com": entry["com"], "inertia": entry["inertia"]}
        if name in math:
            if math[name] != values:
                raise ValueError(f"effective instance {name!r} disagrees with its captured row")
            continue
        math[name] = values
    return math


def _effective_ancestors(name: str) -> list[str]:
    parts = name.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]


def _resolved_contributions(body, raw_scene, masses, effective):
    """One contribution per authored occurrence, after top-down substitution.

    A native effective instance replaces its whole subtree (SolidWorks uses the
    overridden component value in its parent). The subtree must travel inside
    exactly this body: an effective delta is never distributed across links.
    """

    placements = {entry["name"]: entry["transform"] for entry in raw_scene.get("components") or []}
    leaves = [entry["name"] for entry in raw_scene.get("components") or []]
    owned = set(body.get("components") or [])
    contributions = []
    claimed: set[str] = set()
    for component in body.get("components") or []:
        if component not in placements or component not in masses:
            raise ValueError(f"body {body.get('id')!r} references a component without raw readings: {component}")
        provider = next((ancestor for ancestor in _effective_ancestors(component) if ancestor in effective), None)
        if provider is None:
            rotation, translation = _transform(placements[component])
            mass, com_part, inertia_part, note = _component_mass(component, masses[component])
            contributions.append((rotation, translation, mass, com_part, inertia_part, note))
            continue
        if provider in claimed:
            continue
        claimed.add(provider)
        subtree = [leaf for leaf in leaves if leaf == provider or leaf.startswith(provider + "/")]
        if not set(subtree) <= owned:
            raise ValueError(
                f"effective instance {provider!r} spans several bodies; its delta cannot be distributed"
            )
        payload = effective[provider]
        mass = float(payload["mass"])
        placement = placements.get(provider)
        if placement is None:
            # A group occurrence keeps its recorded assembly-axes values verbatim.
            rotation, translation = np.eye(3), np.zeros(3)
            com_part = np.array(payload["com"], dtype=float)
            inertia_part = np.array(payload["inertia"], dtype=float)
        else:
            # A leaf fallback converts with its recorded placement on both sides.
            rotation, translation = _transform(placement)
            if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-9):
                raise ValueError(f"effective instance {provider!r} has a non-orthonormal placement")
            com_part = rotation.T @ (np.array(payload["com"], dtype=float) - translation)
            inertia_part = rotation.T @ np.array(payload["inertia"], dtype=float) @ rotation
        note = {
            "component": provider,
            "raw_mass_kg": mass,
            "used_mass_kg": mass,
            "scale": 1.0,
            "effective_instance": True,
        }
        contributions.append((rotation, translation, mass, com_part, inertia_part, note))
    return contributions


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


def _raw_world(body: dict, raw_scene: dict, masses: dict, *, use_effective: bool = True) -> dict:
    """COM and tensor in *assembly* axes, computed without any link frame.

    This is the frame-independent view: it uses only the raw component
    placements and part tensors, so it catches a wrong link frame that a
    link-local comparison would happily agree with.
    """

    effective = effective_math_map(masses, raw_scene)
    if not use_effective:
        effective = {
            name: payload
            for name, payload in effective.items()
            if name in masses and isinstance(masses[name], dict) and masses[name].get("mode") == "effective_instance"
        }
    total_mass = 0.0
    weighted = np.zeros(3)
    parts = []
    for rotation, translation, mass, com_part, inertia_part, _note in _resolved_contributions(
        body, raw_scene, masses, effective
    ):
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


