#!/usr/bin/env python3
"""Read-only candidate5 acceptance for one real M3 run directory.

Usage:
    cd /home/andy/Work/worktrees/description-pipeline-operator-integration-20261007
    PYTHONPATH=src /home/andy/.local/share/description-pipeline/airflow/venv/bin/python -B \
        /home/andy/Work/reviews/incident094339/m3-candidate5-acceptance.py <RUN> [BUNDLE]

Prints one JSON report and exits nonzero unless every expectation holds: independent
quality recompute equals the saved report, subject_digest binds both receipts,
selection == raw == acquired inventory (equal dicts including the staged NP hash,
count recorded rather than pinned to one candidate), all 25 movable
joints keep their authored zero limits with both motion warnings present, and the
selected-set accounting is pinned to the comparison-only PR279 reference identity 89d0499
(never fetched or used as input): 26 links, 25 joints, 367 selected occurrences, 368 visual
assets, selected 1.0391975 kg plus the unassigned washer 0.0000725 kg closing to native
1.0392700 kg within 1e-12 kg, exactly one unassigned washer warning plus the native mass-gap
warning, and the three precise sheet no-facet warnings must all appear in the automatic
Chinese report with exact recompute evidence, and
capture's recorded stale-reference skips (the CopyDesignTemp lowerbody entry) are
closure-validated, counted identically in the dependency gate details and absent from
the unresolved list. The sealed material provenance record must quote the raw mass
readings exactly - component, document, configuration, body indices, the verbatim
assignment, the warning message and the measured values including the native scope -
and the report must cover the raw-derived unverified occurrence set exactly; the
record's assembly binds the sealed capture identity, never the package-relative
archive name. The sealed effective-instance record (readings, scene-notes
mirror, component-context effective rows, per-code warnings) must bind every
CAD-authored effective measurement verbatim, the Linux mass-closure report must
carry the native record verbatim plus the derived diagnostics, and both totals
are recomputed here independently against the assembly reading.
"""

from __future__ import annotations

import json
import math
import ntpath
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from description_pipeline.delivery import subject_digest
from description_pipeline.structural_report import REPORT_SCHEMA, render_structural_markdown
from description_pipeline.verification.native_discovery import verify_discovery
from description_pipeline.verification.solidworks_urdf import (
    _stale_stored_reference_skips,
    check_bundle,
)

EXPECTED_STALE_SUFFIX = "copyDesigntemp\\lowerbody.sldasm"

CANONICAL_MATERIAL_REASON = "cad_material_provenance_missing"
MATERIAL_FALLBACK_MESSAGE = "material assignment is not verified"
NATIVE_READING_SCOPES = ("component", "part_document")


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _bind_material_entry(name: str, entry: dict, row: dict, reference: dict, assignment: dict, errors: list) -> None:
    """Bind one sealed warning verbatim to the raw reading it claims to quote."""

    def want(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    want(entry.get("code") == CANONICAL_MATERIAL_REASON, name + ": warning code is not the canonical deficiency")
    want(entry.get("blocking") is False, name + ": warning is not explicitly non-blocking")
    want(
        entry.get("material_assignment") == assignment,
        name + ": warning assignment differs from the captured reading",
    )
    want(
        entry.get("message") == str(assignment.get("message") or MATERIAL_FALLBACK_MESSAGE),
        name + ": warning message differs from the captured reading",
    )
    want(
        entry.get("document") == reference.get("part_document"),
        name + ": warning document differs from the captured reading",
    )
    want(
        entry.get("configuration") == (reference.get("configuration") or assignment.get("configuration")),
        name + ": warning configuration differs from the captured reading",
    )
    want(
        entry.get("missing_body_indices") == assignment.get("missing_body_indices"),
        name + ": warning body indices differ from the captured reading",
    )
    measured = entry.get("measured")
    if not isinstance(measured, dict) or measured.get("source") != "cad_api":
        errors.append(name + ": measured evidence is missing or unqualified")
        return
    scope = reference.get("scope")
    want(scope in NATIVE_READING_SCOPES, name + ": captured reading carries no valid native scope")
    want(measured.get("scope") == scope, name + ": measured scope differs from the captured reading")
    mass = measured.get("mass_kg")
    want(_finite(mass) and float(mass) > 0.0, name + ": measured mass is not finite and positive")
    want(mass == row.get("mass"), name + ": measured mass differs from the captured reading")
    com = measured.get("center_of_mass_m")
    want(
        isinstance(com, list) and len(com) == 3 and all(_finite(item) for item in com),
        name + ": measured center of mass is malformed",
    )
    want(com == row.get("com"), name + ": measured center of mass differs from the captured reading")
    inertia = measured.get("inertia_kg_m2")
    want(
        isinstance(inertia, list)
        and len(inertia) == 3
        and all(isinstance(item, list) and len(item) == 3 and all(_finite(v) for v in item) for item in inertia),
        name + ": measured inertia is malformed",
    )
    want(inertia == row.get("inertia"), name + ": measured inertia differs from the captured reading")
    volume = measured.get("volume_m3")
    captured_volume = reference.get("volume_m3")
    want(volume == captured_volume, name + ": measured volume differs from the captured reading")
    want(
        captured_volume is None or (_finite(volume) and float(volume) > 0.0),
        name + ": measured volume is not finite and positive",
    )
    density = measured.get("density_kg_m3")
    captured_density = reference.get("density_kg_m3")
    want(density == captured_density, name + ": measured density differs from the captured reading")
    want(
        captured_density is None or (_finite(density) and float(density) > 0.0),
        name + ": measured density is not finite and positive",
    )


def _material_warning_acceptance(bundle: Path, quality: dict) -> dict:
    """Independently accept the sealed material warnings against the raw readings.

    The affected occurrences and every bound value are derived from the raw mass
    readings; the sealed record is then required to quote them exactly, and the
    quality report must cover the same exact occurrence set. The report's own
    pass/fail decision is recorded but never trusted for the derivation.
    """

    errors: list = []
    try:
        masses = json.loads((bundle / "evidence/raw/mass_properties.json").read_text())
    except (OSError, ValueError):
        masses = None
    try:
        record = json.loads((bundle / "evidence/raw/material_provenance.json").read_text())
    except (OSError, ValueError):
        record = None
    try:
        source = json.loads((bundle / "reports/input.json").read_text()).get("input", {}).get("source")
    except (OSError, ValueError, AttributeError):
        source = None
    try:
        manifest = json.loads((bundle / "evidence/manifest.json").read_text())
    except (OSError, ValueError):
        manifest = None
    identity = manifest.get("identity") if isinstance(manifest, dict) else None
    result: dict = {
        "raw_components": len(masses) if isinstance(masses, dict) else 0,
        "sealed_assembly": identity.get("assembly") if isinstance(identity, dict) else None,
        "record_present": isinstance(record, dict),
        "affected": [],
        "unknown_reasons": {},
        "gate_claims_pass": False,
        "gate_covers_affected": False,
        "authority_covers_affected": False,
        "passed": False,
    }
    if not isinstance(masses, dict) or not masses:
        result["errors"] = ["raw mass readings are missing"]
        return result
    if isinstance(source, dict) and source.get("material_source") != "cad":
        errors.append("the capture is not CAD-sourced")
    affected: list = []
    unknown: dict = {}
    for name, row in sorted(masses.items()):
        reference = row.get("reference") if isinstance(row, dict) else None
        assignment = reference.get("material_assignment") if isinstance(reference, dict) else None
        if isinstance(assignment, dict) and "unverified_reason" in assignment:
            affected.append(name)
            if assignment["unverified_reason"] != CANONICAL_MATERIAL_REASON:
                unknown[name] = assignment["unverified_reason"]
    result["affected"] = affected
    result["unknown_reasons"] = unknown
    if unknown:
        errors.append("unverified reasons other than the canonical deficiency: " + ", ".join(sorted(unknown)))
    if not isinstance(source, dict):
        errors.append("the archived input definition is unreadable")
    if not isinstance(record, dict):
        errors.append("the sealed material provenance record is missing")
    else:
        if record.get("schema_version") != "solidworks-to-urdf.material-provenance/v1":
            errors.append("material provenance schema changed")
        if isinstance(source, dict):
            if record.get("material_source") != source.get("material_source"):
                errors.append("material provenance does not describe this capture's material source")
            if record.get("configuration") != str(source.get("configuration") or ""):
                errors.append("material provenance configuration differs from the captured input")
        if not isinstance(identity, dict) or not str(identity.get("assembly") or ""):
            errors.append("the sealed capture identity carries no assembly")
        elif record.get("assembly") != identity["assembly"]:
            errors.append("material provenance assembly differs from the sealed capture identity")
        if record.get("measured_source") != "cad_api":
            errors.append("material provenance measured source changed")
        warnings = record.get("warnings")
        if not isinstance(warnings, list):
            errors.append("material provenance warnings are not a list")
        else:
            recorded: list = []
            for entry in warnings:
                if not isinstance(entry, dict):
                    errors.append("material provenance carries a malformed warning")
                    continue
                name = entry.get("component")
                if not isinstance(name, str) or name not in masses:
                    errors.append("material warning names an unknown occurrence: " + repr(name))
                    continue
                if name in recorded:
                    errors.append("material provenance repeats an occurrence: " + name)
                    continue
                recorded.append(name)
                row = masses[name]
                reference = row.get("reference") if isinstance(row, dict) else None
                assignment = reference.get("material_assignment") if isinstance(reference, dict) else None
                if not isinstance(reference, dict) or not isinstance(assignment, dict):
                    errors.append(name + ": captured reading carries no material deficiency to bind")
                    continue
                if assignment.get("unverified_reason") != CANONICAL_MATERIAL_REASON:
                    errors.append(name + ": captured reading does not carry the canonical material deficiency")
                    continue
                _bind_material_entry(name, entry, row, reference, assignment, errors)
            if sorted(recorded) != affected:
                errors.append("material warnings do not cover the unverified materials exactly")
    gates = {check.get("id"): check for check in quality.get("checks") or [] if isinstance(check, dict)}
    gate = gates.get("physics.material_provenance") or {}
    details = gate.get("details") or {}
    result["gate_claims_pass"] = gate.get("passed") is True
    gate_warnings = details.get("warnings")
    result["gate_covers_affected"] = bool(
        gate.get("passed") is True
        and details.get("recorded") is True
        and details.get("unverified_material") == affected
        and isinstance(gate_warnings, list)
        and len(gate_warnings) == len(affected)
        and sorted(str(entry.get("component")) for entry in gate_warnings if isinstance(entry, dict)) == affected
    )
    authority = (gates.get("physics.authority") or {}).get("details") or {}
    result["authority_covers_affected"] = bool(
        (gates.get("physics.authority") or {}).get("passed") is True
        and authority.get("unverified_material") == affected
    )
    if not result["gate_covers_affected"]:
        errors.append("the quality report does not cover the raw-derived warning set")
    if not result["authority_covers_affected"]:
        errors.append("physics.authority does not list the raw-derived unverified materials")
    result["errors"] = errors
    result["passed"] = not errors
    return result


EFFECTIVE_SCHEMA = "solidworks-to-urdf.effective-instances/v1"
EFFECTIVE_CODE_SHEET_ONLY = "cad_surface_only_effective_instance"
EFFECTIVE_CODE_INSTANCE = "cad_instance_override_effective"
EFFECTIVE_CODE_DIFFERENCE = "cad_instance_measurement_differs_from_part"
EFFECTIVE_QUALIFICATION_DIFFERENCE = (
    "cad_assembly_instance_measurement; cause_of_part_difference_unproven; "
    "not geometry-derived, not independently physically qualified"
)
PART_MEASUREMENT_KEYS = (
    "mass_kg",
    "center_of_mass_m",
    "inertia_kg_m2",
    "scope",
    "used_api",
    "product_convention",
)
EFFECTIVE_QUALIFICATION = (
    "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
)


def _closure_delta(top_level: dict, leaf_total: dict) -> dict:
    """The script's own copy of the assembly-minus-leaves diagnostics."""

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


def _combine(entries: list) -> dict:
    total = sum(entry[0] for entry in entries)
    if total <= 0.0:
        raise ValueError("recombination has no positive mass")
    weighted = sum((entry[0] * entry[1] for entry in entries), np.zeros(3))
    com = weighted / total
    inertia = np.zeros((3, 3))
    for mass, part_com, part_inertia in entries:
        delta = part_com - com
        inertia += part_inertia + mass * (float(delta @ delta) * np.eye(3) - np.outer(delta, delta))
    return {"mass": total, "com": com, "inertia": inertia}


def _derive_report(native: dict, scene: dict, masses: dict, source: dict) -> dict:
    """Independent rebuild of the Linux report's derived block."""

    components = [row for row in scene.get("components") or [] if isinstance(row, dict)]
    placements = {str(row["name"]): row["transform"] for row in components}
    leaves = [str(row["name"]) for row in components]
    masses_by_name = {
        str(name): float((payload or {}).get("mass") or 0.0)
        for name, payload in masses.items()
        if isinstance(payload, dict)
    }

    def basis(name: str) -> float:
        prefix = name + "/"
        return sum(mass for leaf, mass in masses_by_name.items() if leaf == name or leaf.startswith(prefix))

    context = native.get("component_context") if isinstance(native.get("component_context"), dict) else {}
    rows = [row for row in context.get("instances") or [] if isinstance(row, dict)]
    instance_rows = {str(row.get("name")): row for row in rows if row.get("name")}
    instances = {
        name: {
            "name": name,
            "document_basis_mass_kg": basis(name),
            "delta_kg": float(row.get("context_mass_kg") or 0.0) - basis(name),
        }
        for name, row in instance_rows.items()
    }
    top_rows = [row for row in rows if row.get("depth") == 0]
    context_kg = sum(float(row.get("context_mass_kg") or 0.0) for row in top_rows)
    document_kg = sum(
        instances[str(row.get("name"))]["document_basis_mass_kg"]
        for row in top_rows
        if str(row.get("name")) in instances
    )
    assembly_kg = context.get("assembly_mass_kg")
    assembly_kg = None if assembly_kg is None else float(assembly_kg)
    part_rows = {str(row.get("name")) for row in rows if str(row.get("document_type") or "") == "part"}
    covered_documents = {leaf for leaf in leaves if leaf in part_rows}

    def place(leaf: str) -> tuple:
        grid = np.array(placements[leaf], dtype=float).reshape(4, 4)
        rotation, translation = grid[:3, :3], grid[:3, 3]
        payload = masses[leaf]
        com = rotation @ np.array(payload["com"], dtype=float) + translation
        inertia = rotation @ np.array(payload["inertia"], dtype=float) @ rotation.T
        return float(payload["mass"]), com, inertia

    effective_readings: dict = {}

    def effective_place(leaf: str) -> tuple:
        payload = masses[leaf]
        if payload.get("mode") in {"effective_instance", "assembly_instance_measurement"}:
            reading = effective_readings.get(leaf)
            if isinstance(reading, dict) and reading.get("mass") is not None:
                return (
                    float(reading["mass"]),
                    np.array(reading["com"], dtype=float),
                    np.array(reading["inertia"], dtype=float),
                )
            return (
                float(payload["mass"]),
                np.array(payload["com"], dtype=float),
                np.array(payload["inertia"], dtype=float),
            )
        return place(leaf)

    top_level = native.get("top_level")
    try:
        leaf_total = _combine([effective_place(leaf) for leaf in leaves])
    except (KeyError, ValueError):
        leaf_total = None
    delta = _closure_delta(top_level, leaf_total) if isinstance(top_level, dict) and leaf_total else None
    effective_readings.update(
        {str(row.get("name")): row.get("effective") for row in rows if isinstance(row.get("effective"), dict)}
    )
    parent_readings = {
        name: value
        for name, value in effective_readings.items()
        if str(value.get("document_type") or value.get("role") or "") == "assembly"
    }
    body_members: dict = {}
    for body in source.get("bodies") or []:
        if not isinstance(body, dict):
            continue
        body_name = str(body.get("name") or body.get("id") or "")
        for member in body.get("components") or []:
            body_members[str(member)] = body_name
    blockers: list = []
    substitutable: list = []
    for name, value in sorted(parent_readings.items()):
        descendants = {leaf for leaf in leaves if leaf == name or leaf.startswith(name + "/")}
        if not descendants:
            continue
        owners = {body_members.get(leaf, "") for leaf in descendants}
        if len(owners) == 1 and next(iter(owners)):
            substitutable.append((name, descendants, value))
        else:
            blockers.append(
                {
                    "code": "effective_instance_spans_bodies",
                    "component": name,
                    "bodies": sorted(owner for owner in owners if owner),
                    "descendants": sorted(descendants)[:50],
                }
            )
    top_most = [
        entry
        for entry in substitutable
        if not any(
            other != entry[0] and (entry[0] == other or entry[0].startswith(other + "/"))
            for other, _descendants, _value in substitutable
        )
    ]
    covered_effective: set = set()
    entries: list = []
    for _name, descendants, value in top_most:
        covered_effective |= descendants
        entries.append(
            (float(value["mass"]), np.array(value["com"], dtype=float), np.array(value["inertia"], dtype=float))
        )
    for leaf in leaves:
        if leaf in covered_effective:
            continue
        if not isinstance(masses.get(leaf), dict) or "mass" not in masses[leaf]:
            continue
        if leaf not in placements:
            continue
        entries.append(effective_place(leaf))
    effective_total = None
    if entries:
        try:
            combined = _combine(entries)
        except ValueError:
            combined = None
        if combined is not None:
            effective_total = {
                "mass": combined["mass"],
                "com": combined["com"].tolist(),
                "inertia": combined["inertia"].tolist(),
                "substituted": sorted(name for name, _descendants, _value in top_most),
            }
    selected_members = sorted(
        {
            str(member)
            for body in (source or {}).get("bodies") or []
            if isinstance(body, dict)
            for member in body.get("components") or []
        }
    )
    selected_set = set(selected_members)
    raw_set = set(masses_by_name)
    unassigned_names = sorted(raw_set - selected_set)
    context_names = [str(row.get("name")) for row in rows if row.get("name")]
    if len(context_names) != len(set(context_names)):
        raise ValueError("component context repeats an instance name")
    if len(leaves) != len(set(leaves)):
        raise ValueError("scene components repeat a leaf name")
    native_mass = (
        float(top_level["mass"])
        if isinstance(top_level, dict) and top_level.get("mass") is not None
        else None
    )
    effective_mass = (
        float(effective_total["mass"])
        if isinstance(effective_total, dict) and effective_total.get("mass") is not None
        else None
    )
    collected: list = []
    unassigned_rows_payload = []
    for name in unassigned_names:
        row = instance_rows.get(name)
        if not isinstance(row, dict):
            raise ValueError(f"unassigned occurrence has no native context row: {name}")
        value = row.get("context_mass_kg")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise ValueError(f"unassigned occurrence has no positive finite context mass: {name}")
        mass_value = float(value)
        collected.append(mass_value)
        reference = {}
        if isinstance(masses.get(name), dict):
            reference = masses[name].get("reference") or {}
        unassigned_rows_payload.append(
            {
                "component": name,
                "document": row.get("document"),
                "configuration": (reference or {}).get("configuration"),
                "parent": row.get("parent"),
                "mass_kg": mass_value,
            }
        )
    unassigned_mass = math.fsum(collected) if collected else 0.0
    delta_kg = (
        None
        if native_mass is None or effective_mass is None
        else native_mass - effective_mass
    )
    selection_payload = {
        "native_mass_kg": native_mass,
        "selected_mass_kg": (
            None
            if effective_mass is None
            else effective_mass - unassigned_mass
        ),
        "unassigned_mass_kg": unassigned_mass,
        "unassigned": unassigned_rows_payload,
        "accounting_delta_kg": delta_kg,
        "whole_mass_qualified": (
            not unassigned_names
            and native_mass is not None
            and effective_mass is not None
            and delta_kg is not None
            and abs(delta_kg) <= 1e-12
        ),
    }
    return {
        "selection": selection_payload,
        "leaf_total": (
            None
            if leaf_total is None
            else {
                "mass": leaf_total["mass"],
                "com": leaf_total["com"].tolist(),
                "inertia": leaf_total["inertia"].tolist(),
            }
        ),
        "effective_total": effective_total,
        "blockers": blockers,
        "delta": delta,
        "totals": {
            "context_kg": context_kg,
            "document_kg": document_kg,
            "assembly_kg": assembly_kg,
            "context_minus_document_kg": context_kg - document_kg,
            "context_minus_assembly_kg": None if assembly_kg is None else context_kg - assembly_kg,
        },
        "overridden": {
            "top_level_instances": len(top_rows),
            "mass": sum(1 for row in rows if (row.get("overrides") or {}).get("OverrideMass")),
            "com": sum(1 for row in rows if (row.get("overrides") or {}).get("OverrideCenterOfMass")),
            "inertia": sum(1 for row in rows if (row.get("overrides") or {}).get("OverrideMomentsOfInertia")),
            "any": sum(1 for row in rows if any((row.get("overrides") or {}).values())),
            "top_level_overridden": sum(1 for row in top_rows if any((row.get("overrides") or {}).values())),
            "examples": [
                str(row.get("name")) for row in rows if (row.get("overrides") or {}).get("OverrideMass")
            ][:50],
        },
        "coverage": {
            "leaf_documents": len(leaves),
            "leaf_documents_covered": len(covered_documents),
            "leaf_documents_uncovered": sorted(set(leaves) - covered_documents)[:50],
        },
        "instances": instances,
    }


def _compare_receipt(recorded, mine, label: str, errors: list) -> None:
    """Bind the Linux report's derived block to this script's own recomputation."""

    if isinstance(mine, dict):
        if not isinstance(recorded, dict) or set(recorded) != set(mine):
            errors.append(label + ": derived keys changed")
            return
        for key in sorted(mine):
            _compare_receipt(recorded[key], mine[key], label + "." + key, errors)
        return
    if isinstance(mine, list):
        if not isinstance(recorded, list) or len(recorded) != len(mine):
            errors.append(label + ": derived list changed")
            return
        left, right = list(recorded), list(mine)
        if right and all(isinstance(item, str) for item in right):
            left, right = sorted(left), sorted(right)
        elif right and all(isinstance(item, dict) and "name" in item for item in right):
            left = sorted(left, key=lambda item: item.get("name"))
            right = sorted(right, key=lambda item: item.get("name"))
        for index, (recorded_item, mine_item) in enumerate(zip(left, right, strict=False)):
            _compare_receipt(recorded_item, mine_item, f"{label}[{index}]", errors)
        return
    if isinstance(mine, bool) or mine is None or isinstance(mine, str):
        if recorded != mine:
            errors.append(label + ": derived value changed")
        return
    if isinstance(mine, (int, float)):
        if (
            not isinstance(recorded, (int, float))
            or isinstance(recorded, bool)
            or abs(float(recorded) - float(mine)) > max(1e-12, 1e-6 * abs(float(mine)))
        ):
            errors.append(label + ": derived number changed")
        return
    if recorded != mine:
        errors.append(label + ": derived value changed")


def _direct_readout_comparison(
    bundle: Path, closure: dict, scene: dict, source: dict, link_record: dict, top_level: dict
) -> dict:
    """Recompute Σ direct link reads (+ unassigned) against the pristine top.

    The pristine-window workflow intentionally makes the whole-assembly reading
    and the direct link reads come from a reopened, never-touched session, while
    the walked per-instance recombination stays a recorded diagnostic.  This
    branch compares the two pristine values at the released closure constants
    (mass 1e-12 kg, COM 1e-3 m, tensor 1e-2 relative), mirroring verification's
    closure ledger, and reports the branch explicitly.
    """

    links = link_record.get("links") if isinstance(link_record, dict) else None
    if not isinstance(links, dict) or not links:
        raise ValueError("the direct link record carries no links")
    context_rows = ((closure or {}).get("component_context") or {}).get("instances") or []
    context_mass = {
        str(row.get("name")): float(row.get("context_mass_kg"))
        for row in context_rows
        if isinstance(row, dict) and row.get("name")
    }
    masses = json.loads((bundle / "evidence/raw/mass_properties.json").read_text(encoding="utf-8"))
    selected = {
        str(item) for body in source.get("bodies") or [] for item in body.get("components") or []
    }
    native_names = {str(row.get("name")) for row in (scene or {}).get("components") or []}
    unassigned_names = sorted(native_names - selected)
    unassigned_mass = math.fsum(
        context_mass.get(name, float(masses.get(name, {}).get("mass", 0.0))) for name in unassigned_names
    )
    entries = []
    for body in source.get("bodies") or []:
        name = str(body.get("name"))
        entry = links.get(name)
        reads = entry.get("reads") if isinstance(entry, dict) else None
        if not isinstance(reads, list) or not reads:
            raise ValueError(f"{name}: the direct link record carries no reads")
        read = reads[0]
        mass = float(read["mass_kg"])
        com = np.asarray(read["com_m"], dtype=float)
        tensor = np.asarray(read["inertia_tensor_about_com"], dtype=float)
        if mass <= 0.0 or com.shape != (3,) or tensor.shape != (3, 3):
            raise ValueError(f"{name}: malformed direct link read")
        entries.append((mass, com, tensor))
    total = float(sum(mass for mass, _com, _tensor in entries))
    center = sum((mass * com for mass, com, _tensor in entries), np.zeros(3)) / total
    inertia = np.zeros((3, 3))
    for mass, com, tensor in entries:
        offset = com - center
        inertia += tensor + mass * (float(offset @ offset) * np.eye(3) - np.outer(offset, offset))
    accounted_mass = total + unassigned_mass
    top_mass = float(top_level["mass"])
    top_com = np.asarray(top_level["com"], dtype=float)
    top_tensor = np.asarray(top_level["inertia"], dtype=float)
    com_delta = top_com - center
    tensor_scale = float(max(np.max(np.abs(inertia)), 1e-20))
    return {
        "mode": "pristine-reopen",
        "note": (
            "top and Σ direct reads come from the reopened pristine window; the walked "
            "recombination is kept as a recorded diagnostic, so the strict top-vs-recombination "
            "equality is replaced by the released closure constants"
        ),
        "constants": {"mass_kg": 1e-12, "com_m": 1e-3, "tensor_rtol": 1e-2},
        "direct_mass_kg": total,
        "unassigned_mass_kg": float(unassigned_mass),
        "unassigned": unassigned_names,
        "accounted_mass_kg": float(accounted_mass),
        "mass_delta_kg": abs(top_mass - accounted_mass),
        "com_delta_m": [float(value) for value in com_delta],
        "com_delta_norm_m": float(np.linalg.norm(com_delta)),
        "tensor_delta_max_abs_kg_m2": float(np.max(np.abs(top_tensor - inertia))),
        "tensor_scale_kg_m2": tensor_scale,
        "tensor_rel": float(np.max(np.abs(top_tensor - inertia)) / tensor_scale),
    }


def _effective_instance_acceptance(bundle: Path, quality: dict) -> dict:
    """Bind the sealed effective-instance record, its warnings and the totals."""

    errors: list = []

    def read(path: Path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    masses = read(bundle / "evidence/raw/mass_properties.json")
    scene = read(bundle / "evidence/raw/scene_raw.json")
    record = read(bundle / "evidence/raw/effective_instances.json")
    closure = read(bundle / "evidence/raw/mass_closure.json")
    report_file = read(bundle / "reports/mass-closure.json")
    manifest = read(bundle / "evidence/manifest.json")
    definition = read(bundle / "reports/input.json")
    result: dict = {
        "record_present": isinstance(record, dict),
        "effective": [],
        "cache_error": None,
        "blockers": None,
        "passed": False,
    }
    if not isinstance(masses, dict) or not isinstance(scene, dict) or not isinstance(closure, dict):
        result["errors"] = ["raw readings are missing"]
        return result
    source = (definition or {}).get("input", {}).get("source", {}) if isinstance(definition, dict) else {}
    identity = manifest.get("identity") if isinstance(manifest, dict) else None
    if not isinstance(identity, dict) or not str(identity.get("assembly") or ""):
        errors.append("the sealed capture identity carries no assembly")
    capture_document = scene.get("document")
    if not isinstance(capture_document, str) or not capture_document:
        errors.append("the captured scene names no read document")
    notes = scene.get("notes") if isinstance(scene.get("notes"), dict) else {}
    context = closure.get("component_context") if isinstance(closure.get("component_context"), dict) else {}
    instances = {
        str(row.get("name")): row
        for row in context.get("instances") or []
        if isinstance(row, dict) and row.get("name")
    }
    leaf_rows = {
        name: row
        for name, row in masses.items()
        if isinstance(row, dict) and row.get("mode") in {"effective_instance", "assembly_instance_measurement"}
    }
    overridden = {name for name, row in instances.items() if any((row.get("overrides") or {}).values())}
    effective_names = sorted(set(leaf_rows) | overridden)
    result["effective"] = effective_names
    result["blockers"] = None
    if not isinstance(report_file, dict):
        errors.append("the Linux mass closure report is missing")
    else:
        if report_file.get("schema_version") != "solidworks-to-urdf.mass-closure-report/v1":
            errors.append("mass closure report schema changed")
        if report_file.get("native") != closure:
            errors.append("mass closure report native section differs from the captured record")
        derived_receipt = report_file.get("derived")
        if not isinstance(derived_receipt, dict):
            errors.append("mass closure report carries no derived block")
        else:
            blockers = derived_receipt.get("blockers") or []
            result["blockers"] = blockers
            if blockers:
                errors.append(
                    "the producer recorded effective substitution blockers: " + json.dumps(blockers)[:300]
                )
    if not isinstance(record, dict):
        errors.append("the sealed effective instance record is missing")
        result["errors"] = errors
        return result
    result["cache_error"] = record.get("cache_error")
    if record.get("cache_error") and effective_names:
        errors.append("the readings cache failed while native overrides exist")
    if record.get("schema_version") != EFFECTIVE_SCHEMA:
        errors.append("effective instance schema changed")
    if isinstance(identity, dict) and record.get("assembly") != identity.get("assembly"):
        errors.append("effective instance record belongs to another assembly")
    if record.get("configuration") != str(source.get("configuration") or ""):
        errors.append("effective instance record configuration differs from the captured input")
    readings = record.get("readings") if isinstance(record.get("readings"), dict) else None
    warnings = record.get("warnings") if isinstance(record.get("warnings"), list) else None
    if readings is None or warnings is None:
        errors.append("effective instance readings or warnings are malformed")
        result["errors"] = errors
        return result
    mirrored = notes.get("effective_instances") if isinstance(notes.get("effective_instances"), dict) else {}
    if mirrored != readings:
        errors.append("effective instance readings differ from the captured scene notes")
    if set(readings) != set(effective_names):
        errors.append("effective instance readings do not cover the native overrides exactly")
    for name in sorted(overridden):
        row = instances[name]
        if not isinstance(row.get("effective"), dict) or row.get("effective_error"):
            errors.append(name + ": native overrides carry no usable effective measurement")
            continue
        if row["effective"] != readings.get(name):
            errors.append(name + ": component context effective reading differs from the sealed record")
    flag_keys = {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"}
    def _sheet_only_row(name: str) -> bool:
        ref = ((masses.get(name) or {}).get("reference") or {})
        solids = ref.get("solid_bodies")
        sheets = ref.get("sheet_bodies")
        return type(solids) is int and solids == 0 and type(sheets) is int and sheets > 0

    sheet_only = {name for name in leaf_rows if _sheet_only_row(name)}
    difference = {name for name in leaf_rows if name not in sheet_only and name not in overridden}
    expected_pairs = sorted(
        [(name, EFFECTIVE_CODE_SHEET_ONLY) for name in sheet_only]
        + [(name, EFFECTIVE_CODE_INSTANCE) for name in overridden]
        + [(name, EFFECTIVE_CODE_DIFFERENCE) for name in difference]
    )
    seen: list = []
    for entry in warnings:
        if not isinstance(entry, dict):
            errors.append("effective instance warning is malformed")
            continue
        name = entry.get("component")
        code = entry.get("code")
        if not isinstance(name, str) or not name or not isinstance(code, str):
            errors.append("effective instance warning names an unknown occurrence")
            continue
        seen.append((name, code))
        reading = readings.get(name)
        if not isinstance(reading, dict):
            errors.append(name + ": effective warning has no sealed reading")
            continue
        reference = reading.get("reference") if isinstance(reading.get("reference"), dict) else {}
        flags = reference.get("overrides")
        if entry.get("blocking") is not False:
            errors.append(name + ": effective warning must be explicitly non-blocking")
        expected_qualification = (
            EFFECTIVE_QUALIFICATION_DIFFERENCE
            if code == EFFECTIVE_CODE_DIFFERENCE
            else EFFECTIVE_QUALIFICATION
        )
        if entry.get("qualification") != expected_qualification:
            errors.append(name + ": effective qualification statement changed")
        if (
            not isinstance(flags, dict)
            or set(flags) != flag_keys
            or not all(isinstance(value, bool) for value in flags.values())
            or (code != EFFECTIVE_CODE_DIFFERENCE and not any(flags.values()))
            or (code == EFFECTIVE_CODE_DIFFERENCE and any(flags.values()))
        ):
            errors.append(name + ": effective override flags are malformed or inactive")
            flags = {}
        if flags != (instances.get(name) or {}).get("overrides"):
            errors.append(name + ": effective flags differ from the native context")
        if entry.get("overrides") != flags:
            errors.append(name + ": warning flags differ from the sealed reading")
        if reference.get("effective_instance") is not True:
            errors.append(name + ": effective instance marker changed")
        expected_basis = (
            "cad_assembly_instance_measurement"
            if code == EFFECTIVE_CODE_DIFFERENCE
            else "cad_authored_effective_instance"
        )
        if reference.get("measurement_basis") != expected_basis:
            errors.append(name + ": effective measurement basis changed")
        if code == EFFECTIVE_CODE_DIFFERENCE:
            part = entry.get("part_measurement")
            if not isinstance(part, dict) or any(key not in part for key in PART_MEASUREMENT_KEYS):
                errors.append(name + ": flagless difference warning lacks its part measurement block")
            else:
                raw_row = masses.get(name) if isinstance(masses.get(name), dict) else {}
                raw_reference = raw_row.get("reference") if isinstance(raw_row.get("reference"), dict) else {}
                raw_mass = raw_row.get("mass")
                if (
                    raw_mass is None
                    or abs(float(part.get("mass_kg", float("nan"))) - float(raw_mass)) > 1e-12
                    or not np.allclose(part.get("center_of_mass_m"), raw_row.get("com"), atol=1e-12)
                    or not np.allclose(part.get("inertia_kg_m2"), raw_row.get("inertia"), atol=1e-12)
                    or part.get("scope") != raw_reference.get("scope")
                    or part.get("used_api") != raw_reference.get("used_api")
                    or part.get("product_convention") != raw_reference.get("product_convention")
                ):
                    errors.append(name + ": flagless part measurement differs from the raw part row")
                if reading.get("part_measurement") != part:
                    errors.append(name + ": reading and warning part measurements differ")
        if reference.get("used_api") != "IMassProperty2.GetMomentOfInertia(0)":
            errors.append(name + ": effective inertia API changed")
        if reference.get("scope") != "assembly_component_group":
            errors.append(name + ": effective scope changed")
        if reference.get("axes") != "assembly_document_axes":
            errors.append(name + ": effective axes changed")
        if reference.get("product_convention") != "solidworks_standard":
            errors.append(name + ": effective convention changed")
        if reference.get("reference_point") != "center_of_mass":
            errors.append(name + ": effective reference point changed")
        if reference.get("selected_components") != [name] or entry.get("selected_components") != [name]:
            errors.append(name + ": effective selection must be exactly this occurrence")
        if reference.get("assembly_document") != capture_document or entry.get("assembly_document") != capture_document:
            errors.append(name + ": effective assembly document differs from the captured read")
        if (
            reference.get("assembly_configuration") != str(source.get("configuration") or "")
            or entry.get("assembly_configuration") != reference.get("assembly_configuration")
        ):
            errors.append(name + ": effective assembly configuration differs")
        measurement = entry.get("measurement")
        if not isinstance(measurement, dict):
            errors.append(name + ": warning measurement block is missing")
            continue
        for field in ("scope", "axes", "used_api", "selection_api", "override_api"):
            if measurement.get(field) != reference.get(field):
                errors.append(name + ": warning measurement " + field + " differs from the sealed reading")
        if (measurement.get("mass_kg"), measurement.get("center_of_mass_m"), measurement.get("inertia_kg_m2")) != (
            reading.get("mass"),
            reading.get("com"),
            reading.get("inertia"),
        ):
            errors.append(name + ": warning measured values differ from the sealed reading")
        if (measurement.get("volume_m3"), measurement.get("density_kg_m3")) != (
            reference.get("volume_m3"),
            reference.get("density_kg_m3"),
        ):
            errors.append(name + ": warning volume/density differ from the sealed reading")
        if code == EFFECTIVE_CODE_INSTANCE:
            if entry.get("role") != reading.get("role") or entry.get("document_type") != reading.get("document_type"):
                errors.append(name + ": override warning role differs from the sealed reading")
            if (
                entry.get("configuration") != reference.get("configuration")
                or entry.get("part_document") != reference.get("part_document")
            ):
                errors.append(name + ": override warning part identity differs from the sealed reading")
        elif code == EFFECTIVE_CODE_SHEET_ONLY:
            row = leaf_rows.get(name)
            if not isinstance(row, dict):
                errors.append(name + ": surface warning has no captured reading row")
                continue
            row_reference = row.get("reference") if isinstance(row.get("reference"), dict) else {}
            if row.get("state") != "sheet_only":
                errors.append(name + ": effective row state changed")
            if entry.get("document") != reference.get("part_document"):
                errors.append(name + ": surface warning document differs")
            if entry.get("part_document_read") != "unavailable":
                errors.append(name + ": surface fallback must record the unreadable part document")
            if entry.get("fallback_reason") != row_reference.get("fallback_reason") or entry.get(
                "fallback_reason"
            ) != "cad_part_document_mass_unavailable":
                errors.append(name + ": effective fallback reason marker changed")
            if (
                entry.get("fallback_error_code") != row_reference.get("fallback_error_code")
                or entry.get("fallback_error_code") != "cad_empty_model"
            ):
                errors.append(name + ": effective fallback error code changed")
            counts = notes.get("bodies:" + name) if isinstance(notes.get("bodies:" + name), dict) else {}
            if (
                entry.get("solid_bodies") != row_reference.get("solid_bodies")
                or row_reference.get("solid_bodies") != counts.get("solid")
                or entry.get("sheet_bodies") != row_reference.get("sheet_bodies")
                or row_reference.get("sheet_bodies") != counts.get("sheet")
            ):
                errors.append(name + ": effective body counts differ from the captured reading")
            if measurement.get("state") != row.get("state"):
                errors.append(name + ": warning state differs from the captured row")
            if measurement.get("measurement_basis") != "cad_authored_effective_instance":
                errors.append(name + ": warning measurement basis changed")
        elif code == EFFECTIVE_CODE_DIFFERENCE:
            if reading.get("role") != "leaf":
                errors.append(name + ": flagless difference warning is not a leaf reading")
        else:
            errors.append(name + ": effective warning carries another code")
    if sorted(seen) != expected_pairs:
        errors.append("effective warnings do not cover the native overrides exactly")
    if isinstance(report_file, dict) and isinstance(report_file.get("derived"), dict):
        try:
            mine = _derive_report(closure, scene, masses, source)
        except ValueError as error:
            errors.append("derived selection contract violated: " + str(error))
            mine = None
        if mine is not None:
            if mine["blockers"]:
                errors.append("effective substitution spans bodies: " + json.dumps(mine["blockers"])[:300])
            _compare_receipt(report_file["derived"], mine, "derived", errors)
        top_level = closure.get("top_level")
        expected = mine["effective_total"] if mine is not None else None
        if isinstance(top_level, dict):
            link_record = read(bundle / "evidence/raw/link_group_mass_properties.json")
            pristine = bool(
                isinstance(link_record, dict)
                and link_record.get("readout_window") == "pristine-reopen"
                and (top_level.get("reference") or {}).get("readout_window") == "pristine-reopen"
            )
            if pristine:
                try:
                    comparison = _direct_readout_comparison(bundle, closure, scene, source, link_record, top_level)
                except (KeyError, TypeError, ValueError) as error:
                    errors.append(f"pristine window comparison is unavailable: {error}")
                else:
                    if expected is not None:
                        comparison["walked_recombination_diagnostic"] = {
                            "mass_delta_kg": abs(float(top_level["mass"]) - float(expected["mass"])),
                            "com_delta_norm_m": float(
                                np.linalg.norm(
                                    np.asarray(top_level["com"], dtype=float)
                                    - np.asarray(expected["com"], dtype=float)
                                )
                            ),
                            "tensor_delta_max_abs_kg_m2": float(
                                np.max(
                                    np.abs(
                                        np.asarray(top_level["inertia"], dtype=float)
                                        - np.asarray(expected["inertia"], dtype=float)
                                    )
                                )
                            ),
                            "note": "recorded diagnostic only; the strict equality is not applied",
                        }
                    result["top_level_comparison"] = comparison
                    if comparison["mass_delta_kg"] > 1e-12:
                        errors.append(
                            "pristine window: whole assembly mass differs from Σ direct reads + "
                            f"unassigned by {comparison['mass_delta_kg']}"
                        )
                    if comparison["com_delta_norm_m"] > 1e-3:
                        errors.append(
                            "pristine window: whole assembly COM differs from Σ direct reads "
                            f"(norm {comparison['com_delta_norm_m']} m > 1e-3 m)"
                        )
                    if comparison["tensor_rel"] > 1e-2:
                        errors.append(
                            "pristine window: whole assembly tensor differs from Σ direct reads "
                            f"(rel {comparison['tensor_rel']} > 1e-2)"
                        )
            elif expected is None:
                errors.append("the independent effective recombination is unavailable")
            else:
                delta = abs(float(top_level["mass"]) - float(expected["mass"]))
                if delta > 1e-12:
                    errors.append(
                        f"whole assembly mass differs from the independent effective recombination by {delta}"
                    )
                if not np.allclose(top_level["com"], expected["com"], atol=1e-9, rtol=0):
                    errors.append("whole assembly COM differs from the independent effective recombination")
                if not np.allclose(top_level["inertia"], expected["inertia"], atol=1e-12, rtol=1e-6):
                    errors.append("whole assembly tensor differs from the independent effective recombination")
    result["errors"] = errors
    result["passed"] = not errors
    return result


# ---------------------------------------------------------------------------
# Native9 / PR279 acceptance pins (comparison-only reference identity 89d0499 —
# never fetched or used as pipeline input; naming aliases are not imported).
EXPECTED_LINKS = 26
EXPECTED_JOINTS = 25
EXPECTED_SELECTED_OCCURRENCES = 367
EXPECTED_NATIVE_RAW_ROWS = 368
EXPECTED_VISUAL_ASSETS = 368
EXPECTED_NATIVE_MASS_KG = 1.0392700
EXPECTED_SELECTED_MASS_KG = 1.0391975
EXPECTED_UNASSIGNED_MASS_KG = 0.0000725
EXPECTED_SHEET_NO_FACET_WARNINGS = 3
EXPECTED_UNASSIGNED_COMPONENT = "headM3.0-1/\u57ab\u72472-2"  # exact bound name, never a substring
MASS_TOL_KG = 1e-12
REFERENCE_PIN = "89d0499a10795fcdc1d1aa2f85c501025b7b3a30"
# ---------------------------------------------------------------------------


def _urdf_mass_sum(root) -> object:
    total = 0.0
    for link in root.findall("link"):
        inertial = link.find("inertial")
        mass_el = inertial.find("mass") if inertial is not None else None
        value = mass_el.get("value") if mass_el is not None else None
        if value is None:
            return None
        try:
            total += float(value)
        except (TypeError, ValueError):
            return None
    return total


def _selected_union(bundle: Path, masses_raw: object) -> set:
    """Selected occurrence set = union of components across the derived source bodies."""

    try:
        input_report = json.loads((bundle / "reports/input.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    source_block = ((input_report.get("input") or {}).get("source") or {}) if isinstance(input_report, dict) else {}
    return {
        str(member)
        for body in source_block.get("bodies") or []
        if isinstance(body, dict)
        for member in body.get("components") or []
    }


def _selected_union_size(bundle: Path, masses_raw: object) -> object:
    return len(_selected_union(bundle, masses_raw))


def _independent_selected_ok(bundle: Path, masses_raw: object) -> bool:
    if not isinstance(masses_raw, dict):
        return False
    selected = _selected_union(bundle, masses_raw)
    return bool(selected) and selected == set(masses_raw) - {EXPECTED_UNASSIGNED_COMPONENT}


def main() -> int:
    run = Path(sys.argv[1])
    bundle = Path(sys.argv[2]) if len(sys.argv) > 2 else run / "stages" / "verify"
    report: dict = {"run": str(run)}

    quality = check_bundle(bundle)
    try:
        saved_quality = json.loads((bundle / "reports/quality.json").read_text())
    except (OSError, ValueError):
        saved_quality = None
    report["quality_recompute_equals_saved"] = saved_quality is not None and saved_quality == quality
    report["quality_passed"] = quality["passed"]
    report["failed_checks"] = [c["id"] for c in quality["checks"] if not c.get("passed")]
    report["quality_diagnostics"] = sorted({d.get("code") for d in quality.get("diagnostics") or []})

    subject = subject_digest(bundle)
    receipt = json.loads((run / "receipts" / "verify.json").read_text())
    generate = json.loads((run / "receipts" / "generate.json").read_text())
    report["subject_matches"] = {
        "verify": subject == receipt.get("subject_sha256"),
        "generate": subject == generate.get("subject_sha256"),
    }

    discovery = verify_discovery(bundle / "input")
    report["discovery_passed"] = discovery["passed"]
    report["discovery_errors"] = [e["code"] for e in discovery["errors"]]
    report["discovery_diagnostics"] = sorted({d.get("code") for d in discovery.get("diagnostics") or []})

    record = json.loads((bundle / "input/discovery/native-discovery.json").read_text())
    raw = record.get("raw") or {}
    manifest = raw.get("source_selection") or {}
    selected = manifest.get("files") or {}
    walked = raw.get("files") or {}
    acquired = record.get("native_files") or {}
    report["inventory"] = {
        "selected": len(selected),
        "raw": len(walked),
        "acquired": len(acquired),
        "count": len(selected),
        "counts_48": len(selected) == len(walked) == len(acquired) == 48,
        "counts_equal": bool(selected) and len(selected) == len(walked) == len(acquired),
        "equal": selected == walked == acquired,
        "np_manifest_sha": selected.get("NP-F550.step"),
        "np_hash_consistent": (
            selected.get("NP-F550.step") is not None
            and selected.get("NP-F550.step") == walked.get("NP-F550.step") == acquired.get("NP-F550.step")
        ),
    }

    try:
        dependency_closure = json.loads((bundle / "evidence/raw/dependency_closure.json").read_text())
    except (OSError, ValueError):
        dependency_closure = None
    if isinstance(dependency_closure, dict):
        skips = dependency_closure.get("stale_stored_references") or []
        gate = next((c for c in quality["checks"] if c["id"] == "source.dependencies"), {})
        report["stale_skips"] = {
            "count": len(skips),
            "validated_count": _stale_stored_reference_skips(bundle, dependency_closure),
            "lowerbody_recorded": any(
                isinstance(item, dict)
                and str(item.get("location") or "").replace("/", "\\").casefold().endswith(EXPECTED_STALE_SUFFIX)
                for item in skips
            ),
            "declared_unresolved_empty": not dependency_closure.get("declared_unresolved"),
            "gate_passed": gate.get("passed") is True,
            "reported_in_gate": (gate.get("details") or {}).get("stale_stored_reference_skips"),
        }
    else:
        report["stale_skips"] = {
            "count": 0,
            "validated_count": None,
            "lowerbody_recorded": False,
            "declared_unresolved_empty": False,
            "gate_passed": False,
            "reported_in_gate": None,
        }
    if isinstance(dependency_closure, dict):
        rows = [row for row in dependency_closure.get("mapping") or [] if isinstance(row, dict)]
        originals = dependency_closure.get("original_files") or {}
        copies = dependency_closure.get("copy_files") or {}
        np_sha = selected.get("NP-F550.step")
        np_row = next(
            (
                row
                for row in rows
                if str(row.get("source") or "").replace("/", "\\").casefold().endswith("\\np-f550.step")
                and row.get("source_sha256") == np_sha
            ),
            None,
        )
        report["copied_coverage"] = {
            "original_files": len(originals),
            "mapping": len(rows),
            "copy_files": len(copies),
            "mapping_exact": {ntpath.normcase(str(path)) for path in originals}
            == {ntpath.normcase(str(row.get("source"))) for row in rows},
            "np_row_bound": np_row is not None,
            "seeded_required_leaves": len(dependency_closure.get("seeded_required_leaves") or []),
        }
    else:
        report["copied_coverage"] = {
            "original_files": 0,
            "mapping": 0,
            "copy_files": 0,
            "mapping_exact": False,
            "np_row_bound": False,
            "seeded_required_leaves": 0,
        }

    material = _material_warning_acceptance(bundle, quality)
    report["material_warnings"] = material
    effective = _effective_instance_acceptance(bundle, quality)
    report["effective_instances"] = effective

    report_root = None
    for candidate_root in (run, run / "capture", bundle):
        if (candidate_root / "reports/structural-team.json").is_file():
            report_root = candidate_root
            break
    structural: dict = {"found": report_root is not None}
    if report_root is not None:
        payload = json.loads((report_root / "reports/structural-team.json").read_text())
        markdown_disk = (report_root / "reports/structural-team.md").read_text()
        stale_findings = [
            item for item in payload.get("findings") or [] if item.get("code") == "native.stale_stored_reference"
        ]
        expected_pairs = {
            (
                str(item.get("document") or ""),
                str(item.get("location") or "").replace("/", "\\").casefold(),
            )
            for item in (dependency_closure or {}).get("stale_stored_references") or []
            if isinstance(item, dict)
        }
        visible_pairs = {
            (
                str((item.get("evidence") or {}).get("document") or ""),
                str((item.get("evidence") or {}).get("location") or "").replace("/", "\\").casefold(),
            )
            for item in stale_findings
        }
        structural = {
            "found": True,
            "root": str(report_root),
            "schema_ok": payload.get("schema_version") == REPORT_SCHEMA,
            "stale_findings": len(stale_findings),
            "stale_visible": expected_pairs == visible_pairs and len(stale_findings) == len(expected_pairs),
            "lowerbody_warning": any(location.endswith(EXPECTED_STALE_SUFFIX) for _, location in visible_pairs),
            "warning_severity": all(item.get("severity") == "warning" for item in stale_findings),
            "markdown_renderer_consistent": render_structural_markdown(payload) == markdown_disk,
            "urdf_link": "urdf/robot.urdf" in (payload.get("urdf_files") or []),
            "urdf_present": (bundle / "urdf/robot.urdf").is_file(),
        }
    report["structural_report"] = structural

    def _zero(value: object) -> bool:
        try:
            return float(value) == 0.0  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    root = ET.parse(bundle / "urdf/robot.urdf").getroot()
    joints = root.findall("joint")
    movable = [joint for joint in joints if joint.get("type") != "fixed"]
    zero_valued = [
        joint.get("name")
        for joint in movable
        if (limit := joint.find("limit")) is not None
        and _zero(limit.get("effort"))
        and _zero(limit.get("velocity"))
        and _zero(limit.get("lower"))
        and _zero(limit.get("upper"))
    ]
    report["joints"] = {"total": len(joints), "movable": len(movable), "zero_valued": len(zero_valued)}
    report["motion_warnings"] = {
        "urdf": sum(1 for d in quality.get("diagnostics") or [] if d.get("code") == "urdf.motion_unqualified"),
        "v14": sum(1 for d in discovery.get("diagnostics") or [] if d.get("code") == "v14.motion_unqualified"),
    }

    stale = report["stale_skips"]
    copied = report["copied_coverage"]
    selection_path = bundle / "reports/mass-closure.json"
    selection = None
    if selection_path.is_file():
        try:
            selection_report = json.loads(selection_path.read_text(encoding="utf-8"))
            derived = selection_report.get("derived") if isinstance(selection_report, dict) else None
            selection = derived.get("selection") if isinstance(derived, dict) else None
        except (OSError, ValueError):
            selection = None
    masses_raw = None
    masses_path = bundle / "evidence/raw/mass_properties.json"
    if masses_path.is_file():
        try:
            masses_raw = json.loads(masses_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            masses_raw = None
    findings = payload.get("findings") or []
    washer_rows = [
        row
        for row in ((selection or {}).get("unassigned") or [])
        if isinstance(row, dict) and row.get("component") == EXPECTED_UNASSIGNED_COMPONENT
    ]
    report["selection"] = {
        "linear": isinstance(selection, dict),
        "native_mass": (selection or {}).get("native_mass_kg"),
        "selected_mass": (selection or {}).get("selected_mass_kg"),
        "unassigned_mass": (selection or {}).get("unassigned_mass_kg"),
        "delta": (selection or {}).get("accounting_delta_kg"),
        "qualified": (selection or {}).get("whole_mass_qualified"),
        "washer_rows": len(washer_rows),
        "raw_rows": len(masses_raw) if isinstance(masses_raw, dict) else None,
        "selected_rows": _selected_union_size(bundle, masses_raw),
        "independent_selected_ok": _independent_selected_ok(bundle, masses_raw),
        "links": len(root.findall("link")),
        "visuals": sum(len(link.findall("visual")) for link in root.findall("link")),
        "sheet_warnings": sum(1 for item in findings if item.get("code") == "cad_sheet_face_untessellated"),
        "unassigned_warnings": sum(1 for item in findings if item.get("code") == "cad_unassigned_occurrence_mass"),
        "gap_warnings": sum(1 for item in findings if item.get("code") == "cad_native_mass_gap_unqualified"),
        "urdf_mass_kg": _urdf_mass_sum(root),
        "reference_pin": REFERENCE_PIN,
    }
    sel = report["selection"]

    link_record = None
    link_record_path = bundle / "evidence/raw/link_group_mass_properties.json"
    if link_record_path.is_file():
        try:
            link_record = json.loads(link_record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            link_record = None
    quality_checks = {check.get("id"): check for check in quality.get("checks") or []}
    closure_reconciliation = (
        (quality_checks.get("physics.closure") or {}).get("details") or {}
    ).get("reconciliation") or {}
    partition_audit = (
        (quality_checks.get("physics.link_partition_audit") or {}).get("details") or {}
    ).get("links") or {}
    record_links = link_record.get("links") if isinstance(link_record, dict) else None
    report["direct_link_readout"] = {
        "schema_ok": isinstance(link_record, dict)
        and link_record.get("schema_version") == "solidworks-to-urdf.link-group-mass-properties/v1",
        "links": len(record_links) if isinstance(record_links, dict) else None,
        "two_reads_each": bool(record_links)
        and all(
            isinstance(entry, dict)
            and isinstance(entry.get("reads"), list)
            and len(entry["reads"]) == 2
            for entry in record_links.values()
        ),
        "record_gate_passed": (quality_checks.get("physics.link_direct_record") or {}).get("passed") is True,
        "binding_failures": sorted(
            identifier
            for identifier, check in quality_checks.items()
            if check.get("passed") is not True
            and (
                identifier in {"physics.link_direct_record", "physics.closure"}
                or identifier.startswith("physics.link.")
            )
        ),
        "direct_sum": closure_reconciliation.get("direct_sum"),
        "partition_audit_links": len(partition_audit),
    }
    direct = report["direct_link_readout"]
    report["direct_link_ok"] = bool(
        direct["schema_ok"]
        and direct["links"] == EXPECTED_LINKS
        and direct["two_reads_each"]
        and direct["record_gate_passed"]
        and not direct["binding_failures"]
    )

    def _close(value: object, expected: float) -> bool:
        try:
            return abs(float(value) - expected) <= MASS_TOL_KG  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    report["selection_ok"] = bool(
        sel["linear"]
        and _close(sel["native_mass"], EXPECTED_NATIVE_MASS_KG)
        and _close(sel["selected_mass"], EXPECTED_SELECTED_MASS_KG)
        and _close(sel["unassigned_mass"], EXPECTED_UNASSIGNED_MASS_KG)
        and _close(sel["delta"], 0.0)
        and sel["qualified"] is False
        and sel["washer_rows"] == 1
        and sel["raw_rows"] == EXPECTED_NATIVE_RAW_ROWS
        and sel["selected_rows"] == EXPECTED_SELECTED_OCCURRENCES
        and sel["independent_selected_ok"] is True
        and isinstance(sel["urdf_mass_kg"], (int, float))
        and abs(float(sel["urdf_mass_kg"]) - EXPECTED_SELECTED_MASS_KG) <= MASS_TOL_KG
        and sel["links"] == EXPECTED_LINKS
        and sel["visuals"] == EXPECTED_VISUAL_ASSETS
        and sel["sheet_warnings"] == EXPECTED_SHEET_NO_FACET_WARNINGS
        and sel["unassigned_warnings"] == 1
        and sel["gap_warnings"] == 1
    )

    report["accepted"] = bool(
        quality["passed"]
        and report["quality_recompute_equals_saved"]
        and not report["failed_checks"]
        and all(report["subject_matches"].values())
        and discovery["passed"]
        and not discovery["errors"]
        and report["inventory"]["equal"]
        and report["inventory"]["counts_equal"]
        and report["inventory"]["np_hash_consistent"]
        and report["joints"]["movable"] == 25
        and report["joints"]["zero_valued"] == 25
        and report["motion_warnings"]["urdf"] == 25
        and report["motion_warnings"]["v14"] == 25
        and stale["count"] >= 1
        and stale["validated_count"] == stale["count"]
        and stale["lowerbody_recorded"]
        and stale["declared_unresolved_empty"]
        and stale["gate_passed"]
        and stale["reported_in_gate"] == stale["count"]
        and copied["mapping_exact"]
        and copied["np_row_bound"]
        and copied["original_files"] == copied["mapping"] == copied["copy_files"]
        and material["passed"]
        and effective["passed"]
        and structural.get("found")
        and structural.get("schema_ok")
        and structural.get("stale_visible")
        and structural.get("lowerbody_warning")
        and structural.get("warning_severity")
        and structural.get("markdown_renderer_consistent")
        and structural.get("urdf_link")
        and structural.get("urdf_present")
        and report["selection_ok"]
        and report["direct_link_ok"]
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
