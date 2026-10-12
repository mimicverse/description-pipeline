"""Linux-side recombination of the sealed raw mass-closure evidence.

The native capture records observations only: the assembly's own reading and
the per-instance component context (identity, context mass/volume, override
flags, effective readings).  Everything derived - document basis and deltas,
totals, coverage, the leaf recombination with the shallowest-overridden-
ancestor substitution, and the explicit substitution blockers - is rebuilt
here from the sealed snapshot so the Windows boundary stays raw.  This
projection is producer evidence for comparison and reporting; the independent
verifier keeps its own oracle math and never trusts it.  The function only
reads files: the snapshot bytes are never modified.
"""

from __future__ import annotations

import types
import math
from pathlib import Path

from .jsonio import read_json
from .errors import ConfigError
from .scene import _PartReading, assembly_leaf_total, closure_delta, combine_mass_properties, identity_matrix

#: The Linux report schema; the native section is the ENTIRE
#: ``raw/mass_closure.json`` object verbatim and the derived section carries
#: the comparison evidence rebuilt on Linux.
REPORT_SCHEMA = "solidworks-to-urdf.mass-closure-report/v1"


def _read(root: Path, relative: str):
    path = root / relative
    if not path.is_file():
        raise FileNotFoundError(f"closure evidence is missing {relative} under {root}")
    return read_json(path)


def derive_mass_closure(evidence_root) -> dict:
    """Rebuild the derived mass-closure diagnostics from the sealed snapshot."""

    root = Path(evidence_root)
    native = _read(root, "raw/mass_closure.json")
    scene_raw = _read(root, "raw/scene_raw.json")
    config = _read(root, "raw/source_config.json")
    if not isinstance(native, dict) or not isinstance(scene_raw, dict) or not isinstance(config, dict):
        raise ValueError("closure evidence files must contain JSON objects")
    return {
        "schema_version": REPORT_SCHEMA,
        "native": native,
        "derived": _derive(native, scene_raw, config),
    }


def _derive(native: dict, scene_raw: dict, config: dict) -> dict:
    components = [
        types.SimpleNamespace(
            name=str(entry.get("name") or ""),
            transform=list(entry.get("transform") or ()),
            path=str(entry.get("path") or ""),
            is_fixed=bool(entry.get("is_fixed")),
            document_type=str(entry.get("document_type") or ""),
        )
        for entry in scene_raw.get("components") or []
    ]
    mass_properties = dict(scene_raw.get("mass_properties") or {})
    leaves = [
        (str(component.name), float((mass_properties.get(str(component.name)) or {}).get("mass") or 0.0))
        for component in components
    ]
    leaf_names = {leaf for leaf, _mass in leaves}
    top_level = native.get("top_level")
    context = native.get("component_context") if isinstance(native.get("component_context"), dict) else {}
    native_instances = list(context.get("instances") or [])

    def document_basis(name: str) -> float:
        prefix = name + "/"
        return sum(mass for leaf, mass in leaves if leaf == name or leaf.startswith(prefix))

    derived_instances = []
    for entry in native_instances:
        name = str(entry.get("name") or "")
        if not name:
            continue
        basis = document_basis(name)
        derived_instances.append(
            {
                "name": name,
                "document_basis_mass_kg": basis,
                "delta_kg": float(entry.get("context_mass_kg") or 0.0) - basis,
            }
        )

    top_rows = [entry for entry in native_instances if entry.get("depth") == 0]
    context_total = sum(float(entry.get("context_mass_kg") or 0.0) for entry in top_rows)
    document_total = sum(
        float(entry.get("document_basis_mass_kg") or 0.0)
        for entry in derived_instances
        if any(row.get("name") == entry["name"] and row.get("depth") == 0 for row in native_instances)
    )
    assembly_kg = context.get("assembly_mass_kg")
    assembly_kg = None if assembly_kg is None else float(assembly_kg)
    part_rows = {
        str(entry.get("name"))
        for entry in native_instances
        if str(entry.get("document_type") or "") == "part"
    }
    covered = {leaf for leaf in leaf_names if leaf in part_rows}

    try:
        leaf_total = assembly_leaf_total(components, mass_properties)
    except ConfigError:
        # A snapshot with no mass-bearing components still yields a report:
        # the absence is recorded instead of fabricating a recombination.
        leaf_total = None
    delta = closure_delta(top_level, leaf_total) if isinstance(top_level, dict) and leaf_total else None

    effective_readings = {
        str(entry.get("name")): entry.get("effective")
        for entry in native_instances
        if isinstance(entry.get("effective"), dict)
    }
    body_members: dict[str, str] = {}
    for body in config.get("bodies") or []:
        if not isinstance(body, dict):
            continue
        body_name = str(body.get("name") or body.get("id") or "")
        for member in body.get("components") or []:
            body_members[str(member)] = body_name
    transforms = {str(component.name): component.transform for component in components}
    parent_readings = {
        name: value
        for name, value in effective_readings.items()
        if str(value.get("document_type") or value.get("role") or "") == "assembly"
    }
    blockers: list[dict] = []
    substitutable: list[tuple[str, set, dict]] = []
    for name, value in sorted(parent_readings.items()):
        descendants = {leaf for leaf, _mass in leaves if leaf == name or leaf.startswith(name + "/")}
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
        (name, descendants, value)
        for name, descendants, value in substitutable
        if not any(
            other != name and (name == other or name.startswith(other + "/"))
            for other, _descendants, _value in substitutable
        )
    ]
    effective_entries: list[dict] = []
    covered_effective: set[str] = set()
    for _name, descendants, value in top_most:
        covered_effective |= descendants
        effective_entries.append(
            {
                "mass": float(value["mass"]),
                "com": [float(item) for item in value["com"]],
                "inertia": [[float(item) for item in row] for row in value["inertia"]],
            }
        )
    for leaf, _mass in leaves:
        if leaf in covered_effective:
            continue
        payload = mass_properties.get(leaf)
        contextual = effective_readings.get(leaf)
        if (
            isinstance(contextual, dict)
            and (contextual.get("reference") or {}).get("measurement_basis") == "cad_assembly_instance_measurement"
        ):
            payload = contextual
        if not isinstance(payload, dict) or "mass" not in payload:
            continue
        transform = transforms.get(leaf)
        if transform is None:
            continue
        part = _PartReading(leaf, transform, payload, identity_matrix(), (0.0, 0.0, 0.0))
        effective_entries.append(part.mass_entry())
    effective_total = None
    if effective_entries:
        combined = combine_mass_properties(effective_entries)
        effective_total = {
            "mass": float(combined["mass"]),
            "com": [float(item) for item in combined["com"]],
            "inertia": [[float(item) for item in row] for row in combined["inertia"]],
            "substituted": sorted(name for name, _descendants, _value in top_most),
        }

    by_instance = {str(entry.get("name")): entry for entry in native_instances}
    unassigned = []
    for name in sorted(leaf_names - set(body_members)):
        entry = by_instance.get(name)
        if not isinstance(entry, dict):
            raise ConfigError("unassigned occurrence has no native assembly-context mass", {"component": name})
        value = entry.get("context_mass_kg")
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ConfigError("unassigned occurrence has an invalid native mass", {"component": name})
        reference = (mass_properties.get(name) or {}).get("reference") or {}
        unassigned.append({
            "component": name, "document": entry.get("document"),
            "configuration": reference.get("configuration"), "parent": entry.get("parent"),
            "mass_kg": float(value),
        })
    unassigned_mass = math.fsum(entry["mass_kg"] for entry in unassigned)
    native_mass = float(top_level["mass"]) if isinstance(top_level, dict) and "mass" in top_level else None
    full_mass = effective_total["mass"] if effective_total is not None else None
    selection = {
        "native_mass_kg": native_mass,
        "selected_mass_kg": None if full_mass is None else full_mass - unassigned_mass,
        "unassigned_mass_kg": unassigned_mass,
        "unassigned": unassigned,
        "accounting_delta_kg": None if native_mass is None or full_mass is None else native_mass - full_mass,
        "whole_mass_qualified": not unassigned and native_mass is not None and full_mass is not None
        and abs(native_mass - full_mass) <= 1e-12,
    }

    return {
        "leaf_total": leaf_total,
        "effective_total": effective_total,
        "blockers": blockers,
        "delta": delta,
        "totals": {
            "context_kg": context_total,
            "document_kg": document_total,
            "assembly_kg": assembly_kg,
            "context_minus_document_kg": context_total - document_total,
            "context_minus_assembly_kg": None if assembly_kg is None else context_total - assembly_kg,
        },
        "overridden": {
            "top_level_instances": len(top_rows),
            "mass": sum(1 for entry in native_instances if (entry.get("overrides") or {}).get("OverrideMass")),
            "com": sum(1 for entry in native_instances if (entry.get("overrides") or {}).get("OverrideCenterOfMass")),
            "inertia": sum(
                1 for entry in native_instances if (entry.get("overrides") or {}).get("OverrideMomentsOfInertia")
            ),
            "any": sum(1 for entry in native_instances if any((entry.get("overrides") or {}).values())),
            "top_level_overridden": sum(1 for entry in top_rows if any((entry.get("overrides") or {}).values())),
            "examples": [
                entry.get("name") for entry in native_instances if (entry.get("overrides") or {}).get("OverrideMass")
            ][:50],
        },
        "coverage": {
            "leaf_documents": len(leaf_names),
            "leaf_documents_covered": len(covered),
            "leaf_documents_uncovered": sorted(leaf_names - covered)[:50],
        },
        "instances": {entry["name"]: entry for entry in derived_instances},
        "selection": selection,
    }
