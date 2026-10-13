"""Strict independent physics gates over the actual native capture protocol.

The reconstruction kernels belong to the independent source verifier, not the
scene builder. Part readings are rotated and translated into assembly space,
combined about their common COM, then expressed in the CAD link datum. The
whole assembly and component-context readings must close independently.
"""

from __future__ import annotations

import math
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from ..io import PipelineError, confined, read_data
from ..schemas import EFFECTIVE_INSTANCES_SCHEMA, LINK_GROUP_MASS_SCHEMA, MASS_CLOSURE_REPORT_SCHEMA
from ..sources.solidworks import verify as oracle

MASS_RTOL = 1e-6
MASS_CLOSURE_ATOL = 1e-12
COM_ATOL_M = 5e-5
#: The whole-assembly readout and the per-occurrence recombination are two
#: different SolidWorks computations.  The native9 closure probe (2026-10-12,
#: same prepared copy, every individual read reproducible at 0.0 m) measured an
#: intrinsic gap of 2.627e-4 m with an intersession readout drift of 3.6e-5 m,
#: and the same intrinsic difference carries the inertia tensor to 2.232e-3
#: relative (rebuilt scale 9.7e-3 kg m^2), so the closure reconciliation also
#: carries its own tensor bound.  Every other comparison (link COM/inertia,
#: readings, reported effective closure) keeps COM_ATOL_M / TENSOR_RTOL.
CLOSURE_COM_ATOL_M = 1e-3
CLOSURE_TENSOR_RTOL = 1e-2
TENSOR_RTOL = 1e-4
#: The direct link group readout (native one-shot IMassProperty2 per body) is the
#: authoritative link mass source; the model is generated from it by an exact
#: assembly->link transform, and the recorded two-read pair must repeat (probe v2
#: measured bit-identical repeats; the repeat bounds below stand until native12
#: measures otherwise).
LINK_DIRECT_SCHEMA = LINK_GROUP_MASS_SCHEMA
LINK_DIRECT_USED_API = "IMassProperty2 (SelectedItems = component set)"
LINK_DIRECT_SELECTION_RULE = "top_most_providers+uncovered_leaves"
LINK_DIRECT_MASS_RTOL = 1e-12
LINK_DIRECT_COM_ATOL_M = 1e-9
LINK_DIRECT_TENSOR_RTOL = 1e-9
LINK_REPEAT_MASS_ATOL_KG = 1e-9
LINK_REPEAT_COM_ATOL_M = 5e-7
LINK_REPEAT_TENSOR_RTOL = 1e-6
INSTANCE_MEASUREMENT_BASIS = "cad_assembly_instance_measurement"
INSTANCE_MEASUREMENT_CODE = "cad_instance_measurement_differs_from_part"
INSTANCE_MEASUREMENT_QUALIFICATION = (
    "cad_assembly_instance_measurement; cause_of_part_difference_unproven; "
    "not geometry-derived, not independently physically qualified"
)


def _require(passed, message):
    if not bool(passed):
        raise PipelineError(message)


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _finite_vector3(value: object) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_finite_number(item) for item in value)


def _finite_matrix3(value: object) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_finite_vector3(row) for row in value)


def _require_measured_reading(measured: dict, row: dict, reference: dict, name: str) -> None:
    """The recorded measurement must quote the captured component reading exactly."""

    scope = reference.get("scope")
    _require(scope in ("component", "part_document"), name + ": captured reading carries no valid native scope")
    _require(measured.get("scope") == scope, name + ": measured scope differs from the captured reading")
    mass = measured.get("mass_kg")
    _require(_finite_number(mass) and float(mass) > 0.0, name + ": measured mass is not finite and positive")
    _require(mass == row.get("mass"), name + ": measured mass differs from the captured reading")
    com = measured.get("center_of_mass_m")
    _require(_finite_vector3(com), name + ": measured center of mass is malformed")
    _require(com == row.get("com"), name + ": measured center of mass differs from the captured reading")
    inertia = measured.get("inertia_kg_m2")
    _require(_finite_matrix3(inertia), name + ": measured inertia is malformed")
    _require(inertia == row.get("inertia"), name + ": measured inertia differs from the captured reading")
    volume = measured.get("volume_m3")
    captured_volume = reference.get("volume_m3")
    _require(volume == captured_volume, name + ": measured volume differs from the captured reading")
    _require(
        captured_volume is None or (_finite_number(volume) and float(volume) > 0.0),
        name + ": measured volume is not finite and positive",
    )
    density = measured.get("density_kg_m3")
    captured_density = reference.get("density_kg_m3")
    _require(density == captured_density, name + ": measured density differs from the captured reading")
    _require(
        captured_density is None or (_finite_number(density) and float(density) > 0.0),
        name + ": measured density is not finite and positive",
    )


def _unassigned_mass_rows(evidence_root: Path, source: dict) -> list[dict]:
    """The measured masses of native leaves that no selected body claims."""

    scene = read_data(confined(evidence_root, "raw/scene_raw.json"))
    rows_scene = scene.get("components") if isinstance(scene, dict) else None
    _require(isinstance(rows_scene, list), "The raw scene carries no component inventory")
    _require(
        all(isinstance(row, dict) and isinstance(row.get("name"), str) for row in rows_scene),
        "The raw scene component inventory is malformed",
    )
    leaves = [str(row["name"]) for row in rows_scene]
    _require(len(set(leaves)) == len(leaves), "The raw scene component inventory carries duplicate names")
    payload = read_data(confined(evidence_root, "raw/mass_closure.json"))
    context = payload.get("component_context") if isinstance(payload, dict) else None
    instances = context.get("instances") if isinstance(context, dict) else None
    _require(isinstance(instances, list), "Unassigned accounting awaits the recorded component context")
    _require(
        all(isinstance(row, dict) and isinstance(row.get("name"), str) for row in instances),
        "The component context instance rows are malformed",
    )
    named = [(str(row["name"]), row) for row in instances]
    _require(len({name for name, _row in named}) == len(named), "The component context carries duplicate instances")
    by_name = dict(named)
    selected = {
        str(component) for body in source.get("bodies") or [] for component in body.get("components") or []
    }
    rows = []
    for name in sorted(set(leaves) - selected):
        row = by_name.get(name)
        _require(isinstance(row, dict), f"Unassigned native component {name!r} has no assembly-context reading")
        value = row.get("context_mass_kg")
        _require(
            type(value) in (int, float) and math.isfinite(value) and value > 0,
            f"Unassigned native component {name!r} has no valid measured mass",
        )
        rows.append({"component": name, "document": row.get("document"), "mass_kg": float(value)})
    return rows


def _link_direct_record(evidence_root: Path) -> dict:
    """The sealed direct link group readout record (one supported schema)."""

    try:
        payload = read_data(confined(evidence_root, "raw/link_group_mass_properties.json"))
    except (OSError, PipelineError) as error:
        raise PipelineError(f"The link group mass record is unreadable: {error}") from error
    _require(
        isinstance(payload, dict) and payload.get("schema_version") == LINK_DIRECT_SCHEMA,
        "The link group mass record schema changed",
    )
    _require(payload.get("used_api") == LINK_DIRECT_USED_API, "The link group mass record API changed")
    _require(
        payload.get("selection_rule") == LINK_DIRECT_SELECTION_RULE,
        "The link group mass record selection rule changed",
    )
    links = payload.get("links")
    _require(isinstance(links, dict) and bool(links), "The link group mass record carries no links")
    for name, entry in links.items():
        _require(isinstance(entry, dict), f"{name}: link group entry is not an object")
        selection = entry.get("selection")
        _require(
            isinstance(selection, list)
            and bool(selection)
            and all(isinstance(path, str) and bool(path) for path in selection),
            f"{name}: link group selection is malformed",
        )
        reads = entry.get("reads")
        _require(
            isinstance(reads, list) and len(reads) == 2 and all(isinstance(read, dict) for read in reads),
            f"{name}: link group reads must be exactly two objects",
        )
        _require(
            isinstance(entry.get("reproducibility"), dict),
            f"{name}: link group entry carries no reproducibility block",
        )
    return payload


def _direct_link_sum(record: dict, source: dict) -> tuple[float, np.ndarray, np.ndarray]:
    """Parallel-axis sum of the authoritative direct link reads in assembly axes."""

    entries = []
    for body in source.get("bodies") or []:
        name = str(body.get("name"))
        entry = (record.get("links") or {}).get(name)
        _require(isinstance(entry, dict), name + ": the link group mass record carries no entry")
        read = entry["reads"][0]
        mass = oracle._finite_positive(read.get("mass_kg"), name + " link group mass")
        com = np.array(oracle._finite_vector(read.get("com_m"), name + " link group COM"), dtype=float)
        tensor = oracle._raw_tensor(
            name,
            {"inertia": read.get("inertia_tensor_about_com"),
             "reference": {"product_convention": "solidworks_standard"}},
        )
        _require(np.isfinite(tensor).all(), name + ": nonfinite link group tensor")
        entries.append((mass, com, tensor))
    _require(bool(entries), "The direct link sum carries no bodies")
    total = float(sum(mass for mass, _com, _tensor in entries))
    center = sum((mass * com for mass, com, _tensor in entries), np.zeros(3)) / total
    inertia = np.zeros((3, 3))
    for mass, com, tensor in entries:
        offset = com - center
        inertia += tensor + mass * (float(offset @ offset) * np.eye(3) - np.outer(offset, offset))
    return total, center, inertia


def verify_urdf_mass_equality(bundle_root: Path, source: dict | None = None) -> dict:
    """Mandatory whole-CAD equality for the delivered URDF.

    The actual delivered URDF XML inertial mass sum, plus the independently
    measured mass of every native occurrence that no selected body claims,
    must equal the bound ``evidence/raw/mass_closure.json`` whole-assembly
    mass with an absolute tolerance of 1e-12 kg and no relative tolerance.
    Accounting unassigned occurrences this way records ``whole_mass_qualified``
    false together with an exact per-component warning; the readings
    themselves are never normalized, redistributed or adjusted. Missing,
    malformed or non-recorded evidence fails, and an unexplained gap always
    fails. This gate is independent of the broader per-link mass/COM/tensor
    accuracy checks, which keep their own relative tolerances.
    """

    root = Path(bundle_root)
    try:
        payload = read_data(confined(root / "evidence", "raw/mass_closure.json"))
    except (OSError, PipelineError) as error:
        raise PipelineError(f"Whole-CAD mass evidence is unreadable: {error}") from error
    _require(
        isinstance(payload, dict) and payload.get("status") == "recorded" and payload.get("mode") == "full",
        "Whole-CAD mass evidence is not a full recorded reading",
    )
    top = payload.get("top_level")
    _require(isinstance(top, dict), "Whole-CAD mass evidence has no top-level reading")
    whole = top.get("mass")
    _require(
        isinstance(whole, (int, float)) and not isinstance(whole, bool),
        "Whole-CAD mass evidence is missing a numeric mass",
    )
    whole = float(whole)
    _require(math.isfinite(whole) and whole > 0.0, "Whole-CAD mass is not a finite positive value")
    try:
        tree = ET.parse(confined(root, "urdf/robot.urdf")).getroot()
    except (OSError, ET.ParseError) as error:
        raise PipelineError(f"Delivered URDF is unreadable: {error}") from error
    _require(tree.tag == "robot", "Delivered URDF has no robot root")
    values = []
    for link in tree.findall("link"):
        inertials = link.findall("inertial")
        _require(len(inertials) <= 1, "URDF link carries multiple inertial definitions")
        if not inertials:  # Fixed reference frames may be massless.
            continue
        masses = inertials[0].findall("mass")
        _require(
            len(masses) == 1 and masses[0].get("value") is not None,
            "URDF inertial element is missing its mass value",
        )
        try:
            value = float(masses[0].get("value"))
        except (ValueError, OverflowError) as error:
            raise PipelineError("URDF inertial mass is not numeric") from error
        _require(math.isfinite(value) and value > 0.0, "URDF inertial mass is not a finite positive value")
        values.append(value)
    _require(values, "Delivered URDF carries no inertial masses")
    total = math.fsum(values)
    unassigned = _unassigned_mass_rows(root / "evidence", source) if source is not None else []
    allowance = math.fsum(item["mass_kg"] for item in unassigned)
    accounted = total + allowance
    delta = accounted - whole
    _require(
        abs(delta) <= MASS_CLOSURE_ATOL,
        f"URDF mass sum {total!r} plus measured unassigned native mass {allowance!r} kg differs "
        f"from the whole-CAD reading {whole!r} (delta {delta!r} kg)",
    )
    details = {
        "urdf_mass_kg": total,
        "unassigned_mass_kg": allowance,
        "accounted_mass_kg": accounted,
        "whole_cad_mass_kg": whole,
        "delta_kg": delta,
        "atol_kg": MASS_CLOSURE_ATOL,
        "rtol": 0.0,
        "inertials": len(values),
        "unassigned": unassigned,
        "whole_mass_qualified": not unassigned,
    }
    if unassigned:
        details["warnings"] = [
            {
                "code": "urdf.unassigned_native_mass",
                "component": item["component"],
                "mass_kg": item["mass_kg"],
                "message": (
                    f"native component {item['component']!r} is not selected into any body; its measured "
                    f"{item['mass_kg']!r} kg is excluded from the delivered URDF and accounted explicitly, "
                    "so whole-assembly mass qualification is withheld"
                ),
            }
            for item in unassigned
        ]
    return details


def _reading(name, record, scope):
    reference = record["reference"]
    _require(reference["used_api"] == "IMassProperty2.GetMomentOfInertia(0)", f"{name}: unqualified inertia API")
    _require(reference["product_convention"] == "solidworks_standard", f"{name}: unqualified inertia convention")
    _require(
        reference.get("scope") == scope,
        f"{name}: inertia scope must be {scope}",
    )
    axes = "part_document_axes" if scope == "part_document" else "assembly_document_axes"
    _require(reference.get("axes") == axes, f"{name}: undeclared inertia axes")
    _require(reference.get("reference_point") == "center_of_mass", f"{name}: tensor must be about COM")
    _require(reference.get("use_system_units") is True, f"{name}: native SI units are required")
    overrides = reference.get("overrides")
    flags = {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"}
    _require(isinstance(overrides, dict) and flags <= set(overrides), f"{name}: missing override evidence")
    _require(all(value is False for value in overrides.values()), f"{name}: unsupported native override")
    mass = oracle._finite_positive(record["mass"], name + " mass")
    com = oracle._finite_vector(record["com"], name + " COM")
    tensor = oracle._raw_tensor(name, record)
    _require(np.isfinite(tensor).all(), f"{name}: nonfinite tensor")
    eigenvalues = np.linalg.eigvalsh(tensor)
    _require(
        eigenvalues[0] > 0 and eigenvalues[-1] <= sum(eigenvalues[:2]) + 1e-10 * eigenvalues[-1],
        f"{name}: nonphysical tensor",
    )
    return mass, com, tensor


def _compare(actual, expected, label, *, com_atol=COM_ATOL_M, tensor_rtol=TENSOR_RTOL):
    _require(np.isclose(actual[0], expected[0], atol=1e-12, rtol=MASS_RTOL), label + ": mass differs")
    _require(np.allclose(actual[1], expected[1], atol=com_atol, rtol=0), label + ": COM differs")
    scale = max(float(np.max(np.abs(expected[2]))), 1e-20)
    _require(
        float(np.max(np.abs(actual[2] - expected[2]))) <= tensor_rtol * scale + 1e-15,
        label + ": full tensor differs",
    )


def verify_physics(evidence_root: Path, source: dict, model: dict) -> list[dict]:
    checks = []

    def gate(identifier, callback):
        try:
            details = callback() or {}
            checks.append({"id": identifier, "passed": True, "details": details})
            return details
        except Exception as error:
            checks.append({"id": identifier, "passed": False, "details": {"error": f"{type(error).__name__}: {error}"}})
            return None

    root = Path(evidence_root)
    try:
        raw = read_data(confined(root, "raw/scene_raw.json"))
        masses = read_data(confined(root, "raw/mass_properties.json"))
        datums = read_data(confined(root, "raw/coordinate_systems.json"))
        _require(masses == raw["mass_properties"] and datums == raw["coordinate_systems"], "Raw reading files disagree")
        _require(isinstance(masses, dict) and bool(masses), "No component mass readings")
    except (OSError, ValueError, TypeError, KeyError) as error:
        return [{"id": "physics.raw_inputs", "passed": False, "details": {"error": str(error)}}]
    gate("physics.raw_inputs", lambda: {"occurrences": len(masses)})
    for name, record in masses.items():
        gate("physics.reading." + name, lambda n=name, r=record: _reading_summary(n, r))
    declared = source.get("documented_masses") or {}

    def authority():
        recorded = read_data(confined(root, "raw/declared_masses.json"))
        _require(
            recorded["items"] == declared and recorded["evidence"] == source.get("mass_evidence"),
            "Captured mass decisions differ from author inputs",
        )
        mode = source["material_source"]
        _require(mode in {"cad", "documented_table"}, "Unknown mass authority")
        if mode == "documented_table":
            _require(set(declared) == set(masses), "Documented masses must cover every occurrence exactly")
            for name, entry in declared.items():
                oracle._finite_positive(entry["mass_kg"], name + " declared mass")
                _require(bool(entry["reason"]) and bool(entry["evidence"]), "Missing mass authority reason/evidence")
        else:
            _require(not declared, "CAD authority cannot carry replacement masses")
            unverified: list[str] = []
            for name, record in masses.items():
                if record.get("mode") == "effective_instance":
                    # An effective-instance reading (surface-only fallback or a native
                    # override) carries no physical material claim; it is gated by
                    # physics.effective_instances instead.
                    continue
                assignment = record["reference"]["material_assignment"]
                _require(isinstance(assignment, dict), name + ": material assignment is not an object")
                if "unverified_reason" in assignment:
                    # A missing explicit physical assignment is a warnable engineering
                    # deficiency: the native-measured mass properties stay authoritative
                    # and the condition is recorded, never silently claimed as verified.
                    # Only the recorded deficiency code is warnable; any other value is
                    # an unreadable or hand-written reading and fails closed.
                    _require(
                        assignment.get("unverified_reason") == "cad_material_provenance_missing",
                        name + ": unknown unverified material reason",
                    )
                    unverified.append(name)
                    continue
                _require(bool(assignment.get("bodies")), name + ": unverified material")
                for body in assignment["bodies"]:
                    selected = body["material"] if body["effective_source"] == "body" else assignment["part"]
                    _require(
                        bool(selected["name"]) and bool(selected["database"]), name + ": missing material assignment"
                    )
        return {
            "mode": mode,
            "inertia_model": "scaled_cad_uniform_density" if declared else "cad_mass_properties",
            "unverified_material": sorted(unverified) if mode == "cad" else [],
        }

    gate("physics.authority", authority)

    def material_provenance():
        path = root / "raw/material_provenance.json"
        if source.get("material_source") != "cad":
            # Only CAD-sourced masses depend on material provenance; documented
            # tables carry their own bound evidence.
            return {"recorded": path.is_file(), "unverified_material": []}
        affected = sorted(
            name
            for name, item in masses.items()
            if isinstance(item.get("reference"), dict)
            and isinstance(item["reference"].get("material_assignment"), dict)
            and "unverified_reason" in item["reference"]["material_assignment"]
        )
        _require(path.is_file(), "Material provenance record is missing from the CAD capture")
        record = read_data(path)
        _require(isinstance(record, dict), "Material provenance record is not an object")
        _require(
            record.get("schema_version") == "solidworks-to-urdf.material-provenance/v1",
            "Material provenance schema changed",
        )
        _require(
            record.get("material_source") == source.get("material_source"),
            "Material provenance does not describe this capture's material source",
        )
        _require(record.get("measured_source") == "cad_api", "Material provenance measured source changed")
        # The record quotes the assembly path of the capture session; the archived
        # definition names the same assembly package-relative. The sealed snapshot
        # identity (manifest, kept equal to the collection identity) is the
        # legitimate binding, never the relative archive path.
        sealed = read_data(confined(root, "manifest.json"))
        identity = sealed.get("identity") if isinstance(sealed, dict) else None
        _require(isinstance(identity, dict), "Sealed capture identity is missing")
        _require(
            isinstance(identity.get("assembly"), str) and bool(identity["assembly"]),
            "Sealed capture identity carries no assembly",
        )
        _require(
            record.get("assembly") == identity["assembly"],
            "Material provenance assembly differs from the sealed capture identity",
        )
        _require(
            record.get("configuration") == str(source.get("configuration") or ""),
            "Material provenance configuration differs from the captured input",
        )
        warnings = record.get("warnings")
        _require(isinstance(warnings, list), "Material provenance warnings must be a list")
        recorded: dict[str, dict] = {}
        for entry in warnings:
            _require(isinstance(entry, dict), "Material provenance warning is malformed")
            _require(
                entry.get("code") == "cad_material_provenance_missing",
                "Material provenance warning carries another code",
            )
            _require(entry.get("blocking") is False, "Material provenance warning must be explicitly non-blocking")
            name = entry.get("component")
            _require(
                isinstance(name, str) and name in masses, "Material provenance warning names an unknown occurrence"
            )
            _require(name not in recorded, "Material provenance repeats an occurrence")
            reference = masses[name].get("reference")
            _require(isinstance(reference, dict), name + ": captured reading has no reference block")
            assignment = reference.get("material_assignment")
            _require(
                isinstance(assignment, dict)
                and assignment.get("unverified_reason") == "cad_material_provenance_missing",
                name + ": captured reading does not carry the recorded material deficiency",
            )
            _require(
                entry.get("material_assignment") == assignment,
                name + ": material provenance differs from the captured reading",
            )
            _require(
                entry.get("message") == str(assignment.get("message") or "material assignment is not verified"),
                name + ": material warning message differs from the captured reading",
            )
            _require(
                entry.get("document") == reference.get("part_document"),
                name + ": material provenance document differs from the captured reading",
            )
            _require(
                entry.get("configuration") == (reference.get("configuration") or assignment.get("configuration")),
                name + ": material provenance configuration differs from the captured reading",
            )
            _require(
                entry.get("missing_body_indices") == assignment.get("missing_body_indices"),
                name + ": material provenance body indices differ from the captured reading",
            )
            measured = entry.get("measured")
            _require(
                isinstance(measured, dict) and measured.get("source") == "cad_api",
                name + ": measured material evidence is missing",
            )
            _require_measured_reading(measured, masses[name], reference, name)
            recorded[name] = {**entry, "blocking": False}
        _require(
            sorted(recorded) == affected,
            "Material provenance does not cover the unverified materials exactly",
        )
        return {
            "recorded": True,
            "unverified_material": affected,
            "warnings": list(recorded.values()),
        }

    gate("physics.material_provenance", material_provenance)

    def effective_instances():
        path = root / "raw/effective_instances.json"
        if source.get("material_source") != "cad":
            return {"recorded": path.is_file(), "effective": []}
        _require(path.is_file(), "Effective instance record is missing from the CAD capture")
        record = read_data(path)
        sealed = read_data(confined(root, "manifest.json"))
        identity = sealed.get("identity") if isinstance(sealed, dict) else None
        _require(
            isinstance(identity, dict) and isinstance(identity.get("assembly"), str) and identity["assembly"],
            "Sealed capture identity carries no assembly",
        )
        _require(
            isinstance(record, dict)
            and record.get("schema_version") == EFFECTIVE_INSTANCES_SCHEMA,
            "Effective instance schema changed",
        )
        _require(
            record.get("assembly") == identity["assembly"],
            "Effective instance record belongs to another assembly",
        )
        _require(
            record.get("configuration") == str(source.get("configuration") or ""),
            "Effective instance record configuration differs from the captured input",
        )
        readings = record.get("readings")
        _require(isinstance(readings, dict), "Effective instance readings must be an object")
        warnings = record.get("warnings")
        _require(isinstance(warnings, list), "Effective instance warnings must be a list")
        notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
        mirrored = notes.get("effective_instances") if isinstance(notes.get("effective_instances"), dict) else {}
        _require(mirrored == readings, "Effective instance readings differ from the captured scene notes")
        closure = read_data(confined(root, "raw/mass_closure.json"))
        context = closure.get("component_context") if isinstance(closure, dict) else None
        _require(isinstance(context, dict), "Effective instances need the component context")
        instances = {str(row["name"]): row for row in context.get("instances") or [] if isinstance(row, dict)}
        leaf_rows = {
            name: row
            for name, row in masses.items()
            if isinstance(row, dict) and row.get("mode") == "effective_instance"
        }
        overridden = {name for name, row in instances.items() if any((row.get("overrides") or {}).values())}
        divergent = _instance_measurement_required(masses, instances)
        expected = set(leaf_rows) | overridden | divergent
        _require(
            set(readings) == expected,
            "Effective instance readings do not cover the native effective instances exactly",
        )
        for name in sorted(overridden | divergent):
            row = instances.get(name) or {}
            _require(
                isinstance(row.get("effective"), dict) and not row.get("effective_error"),
                name + ": native instances carry no usable effective measurement",
            )
            _require(
                row["effective"] == readings.get(name),
                name + ": component context effective reading differs from the sealed record",
            )
        flag_keys = {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"}
        qualification = (
            "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
        )
        capture_document = raw.get("document")
        _require(isinstance(capture_document, str) and capture_document, "Captured scene names no document")
        expected_pairs = sorted(
            [(name, oracle.EFFECTIVE_CODE_SHEET_ONLY) for name in leaf_rows]
            + [(name, oracle.EFFECTIVE_CODE_INSTANCE) for name in overridden]
            + [(name, INSTANCE_MEASUREMENT_CODE) for name in divergent]
        )
        core_reference_fields = (
            "scope",
            "axes",
            "used_api",
            "selection_api",
            "override_api",
            "effective_instance",
            "measurement_basis",
            "selected_components",
            "assembly_document",
            "assembly_configuration",
            "part_document",
            "configuration",
            "overrides",
        )
        entries: dict[tuple[str, str], dict] = {}
        for entry in warnings:
            _require(isinstance(entry, dict), "Effective instance warning is malformed")
            name = entry.get("component")
            code = entry.get("code")
            _require(
                isinstance(name, str) and name and isinstance(code, str),
                "Effective instance warning names an unknown occurrence",
            )
            _require((name, code) not in entries, "Effective instance warning is repeated")
            _require(
                code
                in (
                    oracle.EFFECTIVE_CODE_SHEET_ONLY,
                    oracle.EFFECTIVE_CODE_INSTANCE,
                    INSTANCE_MEASUREMENT_CODE,
                ),
                name + ": effective warning carries another code",
            )
            _require(entry.get("blocking") is False, name + ": effective warning must be explicitly non-blocking")
            expected_qualification = (
                INSTANCE_MEASUREMENT_QUALIFICATION if code == INSTANCE_MEASUREMENT_CODE else qualification
            )
            _require(
                entry.get("qualification") == expected_qualification,
                name + ": effective qualification statement changed",
            )
            reading = readings.get(name)
            _require(isinstance(reading, dict), name + ": effective warning has no sealed reading")
            reference = reading.get("reference")
            _require(isinstance(reference, dict), name + ": effective reading has no reference block")
            flags = reference.get("overrides")
            _require(
                isinstance(flags, dict)
                and set(flags) == flag_keys
                and all(isinstance(value, bool) for value in flags.values()),
                name + ": effective override flags are malformed",
            )
            _require(
                any(flags.values()) or code == INSTANCE_MEASUREMENT_CODE,
                name + ": effective reading without an active override",
            )
            if code == INSTANCE_MEASUREMENT_CODE:
                _require(
                    not any(flags.values()),
                    name + ": instance measurement must carry all-false overrides",
                )
            _require(
                flags == (instances.get(name) or {}).get("overrides"),
                name + ": effective flags differ from the native context",
            )
            _require(entry.get("overrides") == flags, name + ": warning flags differ from the sealed reading")
            expected_basis = (
                INSTANCE_MEASUREMENT_BASIS
                if code == INSTANCE_MEASUREMENT_CODE
                else "cad_authored_effective_instance"
            )
            _require(
                reference.get("effective_instance") is True
                and reference.get("measurement_basis") == expected_basis,
                name + ": effective basis marker changed",
            )
            _require(
                reference.get("selected_components") == [name]
                and entry.get("selected_components") == reference.get("selected_components"),
                name + ": effective selection must be exactly this occurrence",
            )
            _require(
                reference.get("assembly_document") == capture_document
                and entry.get("assembly_document") == reference.get("assembly_document"),
                name + ": effective assembly document differs from the captured copy",
            )
            _require(
                reference.get("assembly_configuration") == str(source.get("configuration") or "")
                and entry.get("assembly_configuration") == reference.get("assembly_configuration"),
                name + ": effective assembly configuration differs",
            )
            measurement = entry.get("measurement")
            _require(isinstance(measurement, dict), name + ": warning measurement block is missing")
            for field in ("scope", "axes", "used_api", "selection_api", "override_api"):
                _require(
                    measurement.get(field) == reference.get(field),
                    name + ": warning measurement " + field + " differs from the sealed reading",
                )
            _require(
                (measurement.get("mass_kg"), measurement.get("center_of_mass_m"), measurement.get("inertia_kg_m2"))
                == (reading.get("mass"), reading.get("com"), reading.get("inertia")),
                name + ": warning measured values differ from the sealed reading",
            )
            _require(
                (measurement.get("volume_m3"), measurement.get("density_kg_m3"))
                == (reference.get("volume_m3"), reference.get("density_kg_m3")),
                name + ": warning volume/density differ from the sealed reading",
            )
            if code == oracle.EFFECTIVE_CODE_INSTANCE:
                _require(name in overridden, name + ": override warning without an active override")
                _require(
                    entry.get("role") == reading.get("role")
                    and entry.get("document_type") == reading.get("document_type"),
                    name + ": override warning role differs from the sealed reading",
                )
                _require(
                    entry.get("configuration") == reference.get("configuration")
                    and entry.get("part_document") == reference.get("part_document"),
                    name + ": override warning part identity differs from the sealed reading",
                )
            elif code == oracle.EFFECTIVE_CODE_SHEET_ONLY:
                row = leaf_rows.get(name)
                _require(isinstance(row, dict), name + ": surface warning has no captured reading row")
                row_reference = row.get("reference")
                _require(isinstance(row_reference, dict), name + ": effective row has no reference block")
                for field in core_reference_fields:
                    if field == "measurement_basis":
                        # The ratified sheet-only row carries the basis at the ROW
                        # level (native.py `_sheet_only_instance_fallback`); the
                        # explicit check below binds it to the sealed reading.
                        continue
                    _require(
                        row_reference.get(field) == reference.get(field),
                        name + ": captured row " + field + " differs from the sealed reading",
                    )
                _require(
                    reading.get("role") == "leaf" and reading.get("document_type") == "part",
                    name + ": surface reading must be a leaf part",
                )
                _require(row.get("state") == "sheet_only", name + ": effective row state changed")
                _require(
                    (row.get("mass"), row.get("com"), row.get("inertia"))
                    == (reading.get("mass"), reading.get("com"), reading.get("inertia")),
                    name + ": captured row values differ from the sealed reading",
                )
                _require(
                    entry.get("document") == reference.get("part_document")
                    and entry.get("configuration") == reference.get("configuration"),
                    name + ": surface warning part identity differs from the sealed reading",
                )
                _require(
                    entry.get("part_document_read") == "unavailable",
                    name + ": surface fallback must record the unreadable part document",
                )
                _require(
                    entry.get("fallback_reason")
                    == row_reference.get("fallback_reason")
                    == "cad_part_document_mass_unavailable",
                    name + ": effective fallback reason marker changed",
                )
                _require(
                    entry.get("fallback_error_code") == row_reference.get("fallback_error_code") == "cad_empty_model",
                    name + ": effective fallback reason changed",
                )
                counts = notes.get("bodies:" + name)
                _require(
                    isinstance(counts, dict)
                    and entry.get("solid_bodies") == row_reference.get("solid_bodies") == counts.get("solid") == 0
                    and entry.get("sheet_bodies") == row_reference.get("sheet_bodies") == counts.get("sheet"),
                    name + ": effective body counts differ from the captured reading",
                )
                _require(
                    measurement.get("state") == row.get("state"),
                    name + ": warning state differs from the captured row",
                )
                _require(
                    row.get("measurement_basis") == "cad_authored_effective_instance"
                    and row_reference.get("measurement_basis") in (None, "cad_authored_effective_instance")
                    and measurement.get("measurement_basis") == "cad_authored_effective_instance",
                    name + ": effective measurement basis changed",
                )
            else:
                _require(name in divergent, name + ": instance measurement outside the divergent set")
                row = masses.get(name)
                _require(isinstance(row, dict), name + ": instance measurement has no captured part row")
                row_reference = row.get("reference")
                _require(isinstance(row_reference, dict), name + ": part row has no reference block")
                _require(
                    reading.get("role") == "leaf" and reading.get("document_type") == "part",
                    name + ": instance measurement must be a leaf part",
                )
                _require(
                    reference.get("part_document") == row_reference.get("part_document")
                    and reference.get("configuration") == row_reference.get("configuration"),
                    name + ": instance measurement part identity differs from the captured row",
                )
                oracle._finite_positive(reading.get("mass"), name + " instance mass")
                oracle._finite_vector(reading.get("com"), name + " instance COM")
                tensor_value = oracle._raw_tensor(name, reading)
                eigenvalues = np.linalg.eigvalsh(tensor_value)
                _require(
                    np.isfinite(tensor_value).all()
                    and eigenvalues[0] > 0
                    and eigenvalues[-1] <= sum(eigenvalues[:2]) + 1e-10 * eigenvalues[-1],
                    name + ": nonphysical instance tensor",
                )
                part = reading.get("part_measurement")
                _require(isinstance(part, dict), name + ": instance measurement carries no part diagnostic")
                _require(
                    part.get("mass_kg") == row.get("mass")
                    and part.get("center_of_mass_m") == row.get("com")
                    and part.get("inertia_kg_m2") == row.get("inertia"),
                    name + ": part diagnostic differs from the captured part row",
                )
                _require(
                    part.get("scope") == "part_document"
                    and part.get("used_api") == row_reference.get("used_api")
                    and part.get("product_convention") == "solidworks_standard",
                    name + ": part diagnostic basis changed",
                )
                for field in ("volume_m3", "density_kg_m3"):
                    if field in part:
                        _require(
                            part.get(field) == row_reference.get(field),
                            name + ": part diagnostic " + field + " differs from the captured row",
                        )
                _require(
                    entry.get("part_measurement") == part,
                    name + ": warning part diagnostic differs from the sealed reading",
                )
                _require(
                    entry.get("role") == reading.get("role")
                    and entry.get("document_type") == reading.get("document_type"),
                    name + ": instance warning role differs from the sealed reading",
                )
                _require(
                    entry.get("configuration") == reference.get("configuration")
                    and entry.get("part_document") == reference.get("part_document"),
                    name + ": instance warning part identity differs from the sealed reading",
                )
            entries[(name, code)] = entry
        _require(
            sorted(entries) == expected_pairs,
            "Effective instance record does not cover the native overrides exactly",
        )
        return {
            "recorded": True,
            "effective": sorted({name for name, _code in entries}),
            "warnings": list(entries.values()),
        }

    gate("physics.effective_instances", effective_instances)
    links = {row["name"]: row for row in model.get("links", [])}
    effective = oracle.effective_math_map(masses, raw)
    direct: dict = {"record": None}

    def direct_record():
        direct["record"] = _link_direct_record(root)
        bodies = {str(body["name"]) for body in source["bodies"]}
        _require(
            set(direct["record"]["links"]) == bodies,
            "The link group mass record does not cover the selected bodies exactly",
        )
        return {"bodies": len(direct["record"]["links"])}

    gate("physics.link_direct_record", direct_record)
    partition_audit: dict[str, dict] = {}
    for body in source["bodies"]:

        def reconstruct(body=body):
            name = body["name"]
            entry = ((direct["record"] or {}).get("links") or {}).get(name)
            _require(isinstance(entry, dict), name + ": the link group mass record carries no entry")
            contributions = oracle._resolved_contributions(body, raw, masses, declared, effective)
            expected_selection = sorted(str(note["component"]) for *_rest, note in contributions)
            _require(
                entry.get("selection") == expected_selection,
                name + ": the recorded selection differs from the independently recomputed set",
            )
            reads = entry.get("reads")
            _require(
                isinstance(reads, list) and len(reads) == 2 and all(isinstance(read, dict) for read in reads),
                name + ": the link group mass record needs exactly two reads",
            )
            flag_keys = {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"}
            tensors = []
            object_override_notes: list[dict] = []
            for read in reads:
                oracle._finite_positive(read.get("mass_kg"), name + " link group mass")
                oracle._finite_vector(read.get("com_m"), name + " link group COM")
                tensor = oracle._raw_tensor(
                    name,
                    {"inertia": read.get("inertia_tensor_about_com"),
                     "reference": {"product_convention": "solidworks_standard"}},
                )
                _require(np.isfinite(tensor).all(), name + ": nonfinite link group tensor")
                overrides = read.get("overrides")
                _require(
                    isinstance(overrides, dict)
                    and set(overrides) == flag_keys
                    and all(isinstance(value, bool) for value in overrides.values()),
                    name + ": missing or malformed link group override evidence",
                )
                if any(overrides.values()):
                    # A single-occurrence selection surfaces the instance's
                    # authored override as an object-level flag in the pristine
                    # reopened window; instance overrides are part of the CAD
                    # value, so the active flags are recorded verbatim instead
                    # of refused (whole-assembly and per-instance readers keep
                    # their own refusals).
                    object_override_notes.append(
                        {"read": len(tensors), "active": sorted(key for key, value in overrides.items() if value)}
                    )
                tensors.append(tensor)
            first, second = reads
            mass_delta = abs(float(first["mass_kg"]) - float(second["mass_kg"]))
            com_delta_norm = float(np.linalg.norm(np.subtract(first["com_m"], second["com_m"])))
            tensor_delta = float(np.max(np.abs(tensors[0] - tensors[1])))
            repeat_scale = max(float(np.max(np.abs(tensors[0]))), 1e-20)
            _require(
                mass_delta <= LINK_REPEAT_MASS_ATOL_KG
                and com_delta_norm <= LINK_REPEAT_COM_ATOL_M
                and tensor_delta <= LINK_REPEAT_TENSOR_RTOL * repeat_scale,
                name + ": the recorded link group reads do not repeat within the evidence bound",
            )
            recorded_repeat = entry.get("reproducibility")
            _require(
                isinstance(recorded_repeat, dict),
                name + ": the link group record carries no reproducibility block",
            )
            for key, measured in (
                ("mass_delta_kg", mass_delta),
                ("com_delta_norm_m", com_delta_norm),
                ("tensor_delta_max_abs", tensor_delta),
            ):
                value = recorded_repeat.get(key)
                _require(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and abs(float(value) - measured) <= 1e-12 + 1e-9 * abs(measured),
                    name + ": recorded reproducibility " + key + " differs from the recomputed pair",
                )
            rotation, origin = oracle._transform(datums[body["frame"]["coordinate_system"]])
            expected_mass = float(first["mass_kg"])
            expected_com = rotation.T @ (np.array(first["com_m"], dtype=float) - origin)
            expected_tensor = rotation.T @ tensors[0] @ rotation
            actual_mass, actual_com, actual_tensor = oracle._canonical_in_link_frame(links[name])
            _require(
                np.isclose(actual_mass, expected_mass, rtol=LINK_DIRECT_MASS_RTOL, atol=1e-15),
                name + ": model mass differs from the direct link group read",
            )
            _require(
                np.allclose(actual_com, expected_com, atol=LINK_DIRECT_COM_ATOL_M, rtol=0),
                name + ": model COM differs from the direct link group read",
            )
            _require(
                np.allclose(actual_tensor, expected_tensor, rtol=LINK_DIRECT_TENSOR_RTOL, atol=1e-15),
                name + ": model inertia differs from the direct link group read",
            )
            world = oracle._raw_world(body, raw, masses, declared)
            recombination_com = rotation.T @ (world["com"] - origin)
            recombination_tensor = rotation.T @ world["inertia"] @ rotation
            recombination_delta = float(np.max(np.abs(expected_tensor - recombination_tensor)))
            partition_audit[name] = {
                "mass_delta_kg": expected_mass - float(world["mass"]),
                "com_delta_m": [
                    float(a) - float(b) for a, b in zip(expected_com, recombination_com, strict=True)
                ],
                "com_delta_norm_m": float(np.linalg.norm(np.subtract(expected_com, recombination_com))),
                "tensor_delta_max_abs": recombination_delta,
                "tensor_rel": recombination_delta
                / max(float(np.max(np.abs(recombination_tensor))), 1e-20),
            }
            details = {
                "mass_kg": expected_mass,
                "selection": expected_selection,
                "com_m": expected_com.tolist(),
                "inertia_kg_m2": oracle._tensor6(expected_tensor),
            }
            if object_override_notes:
                details["object_overrides"] = object_override_notes
                details["object_overrides_note"] = (
                    "single-occurrence selections surface the instance's authored overrides as "
                    "object-level flags in the pristine window; recorded verbatim, not refused"
                )
            return details

        gate("physics.link." + body["name"], reconstruct)

    gate(
        "physics.link_partition_audit",
        lambda: {
            "note": "non-blocking: direct link group read vs the per-instance recombination",
            "links": partition_audit,
        },
    )

    def closure():
        payload = read_data(confined(root, "raw/mass_closure.json"))
        report_path = Path(root).parent / "reports" / "mass-closure.json"
        report = None
        if report_path.is_file():
            report = read_data(report_path)
            _require(
                isinstance(report, dict)
                and report.get("schema_version") == MASS_CLOSURE_REPORT_SCHEMA,
                "Mass closure report schema changed",
            )
            _require(
                report.get("native") == payload,
                "Mass closure report native section differs from the captured record",
            )
        elif source.get("material_source") == "cad":
            _require(False, "The Linux mass closure report is missing")
        _require(
            payload["status"] == "recorded" and payload["mode"] == "full",
            "Full whole-assembly physics evidence is required",
        )
        top = _reading("whole assembly", payload["top_level"], "assembly_document")
        context = payload["component_context"]
        _require(isinstance(context, dict), "Component mass context is not an object")
        _require(context["status"] == "recorded" and not context["errors"], "Incomplete component-context evidence")
        entries = oracle.load_effective_instances(root)
        entry_names = sorted({name for name, _code in entries})
        overridden = {
            str(row["name"]): row
            for row in context["instances"]
            if any((row.get("overrides") or {}).values())
        }
        instances_by_name = {
            str(row["name"]): row for row in context["instances"] if isinstance(row, dict)
        }
        divergent = sorted(_instance_measurement_required(masses, instances_by_name))
        _require(
            entry_names == sorted(set(overridden) | set(divergent)),
            "Effective instance record does not cover the native effective instances exactly",
        )
        for name, row in overridden.items():
            _require(
                entries[(name, oracle.EFFECTIVE_CODE_INSTANCE)].get("overrides") == row["overrides"],
                name + ": effective flags differ from the native context",
            )
        for name in divergent:
            _require(
                entries[(name, INSTANCE_MEASUREMENT_CODE)].get("overrides")
                == (instances_by_name.get(name) or {}).get("overrides")
                == {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False},
                name + ": instance measurement flags differ from the native context",
            )
        derived = report.get("derived") if isinstance(report, dict) else None
        if report is not None:
            _require(isinstance(derived, dict), "Mass closure report carries no derived block")
        mine = _derive_closure(payload, raw, masses, source)
        if derived is not None:
            _compare_derived(derived, mine, "derived", _require)
            _require(not derived.get("blockers"), "Effective substitution blockers were recorded")
        _require(not mine["blockers"], "Effective substitution spans bodies")
        body = {"id": "assembly", "components": [row["name"] for row in raw["components"]]}
        recombination = oracle._raw_world(body, raw, masses, {})
        expected_recombination = (recombination["mass"], recombination["com"], recombination["inertia"])
        _require(
            isinstance(direct["record"], dict),
            "The link group mass record is required for the closure ledger",
        )
        direct_sum = _direct_link_sum(direct["record"], source)
        unassigned = _unassigned_mass_rows(root, source)
        unassigned_mass = math.fsum(item["mass_kg"] for item in unassigned)
        accounted_mass = float(direct_sum[0]) + unassigned_mass
        _require(
            abs(accounted_mass - float(top[0])) <= 1e-12,
            "Whole assembly / direct link sum: mass differs",
        )
        reconciliation = {
            "recombination": {
                "mass_delta_kg": float(top[0]) - float(expected_recombination[0]),
                "com_delta_m": [
                    float(a) - float(b) for a, b in zip(top[1], expected_recombination[1], strict=True)
                ],
                "com_delta_norm_m": float(np.linalg.norm(np.subtract(top[1], expected_recombination[1]))),
                "tensor_delta_max_abs_kg_m2": float(
                    np.max(np.abs(np.subtract(top[2], expected_recombination[2])))
                ),
                "tensor_scale_kg_m2": float(max(np.max(np.abs(expected_recombination[2])), 1e-20)),
            },
            "direct_sum": {
                "unassigned_mass_kg": unassigned_mass,
                "accounted_mass_kg": accounted_mass,
                "mass_delta_kg": float(top[0]) - accounted_mass,
                "com_delta_m": [float(a) - float(b) for a, b in zip(top[1], direct_sum[1], strict=True)],
                "com_delta_norm_m": float(np.linalg.norm(np.subtract(top[1], direct_sum[1]))),
                "tensor_delta_max_abs_kg_m2": float(np.max(np.abs(np.subtract(top[2], direct_sum[2])))),
                "tensor_scale_kg_m2": float(max(np.max(np.abs(direct_sum[2])), 1e-20)),
            },
            "com_atol_m": CLOSURE_COM_ATOL_M,
            "tensor_rtol": CLOSURE_TENSOR_RTOL,
        }
        _compare(
            top,
            (accounted_mass, direct_sum[1], direct_sum[2]),
            "Whole assembly / direct link sum",
            com_atol=CLOSURE_COM_ATOL_M,
            tensor_rtol=CLOSURE_TENSOR_RTOL,
        )
        if derived is not None:
            _compare(
                (
                    float(derived["effective_total"]["mass"]),
                    np.asarray(derived["effective_total"]["com"]),
                    np.asarray(derived["effective_total"]["inertia"]),
                ),
                expected_recombination,
                "Reported effective closure",
            )
        enriched_rows = []
        for row in context["instances"]:
            if not isinstance(row, dict):
                enriched_rows.append(row)
                continue
            name = str(row.get("name"))
            enriched_rows.append(
                {
                    **row,
                    "document_basis_mass_kg": mine["instances"].get(name, {}).get("document_basis_mass_kg"),
                    "delta_kg": mine["instances"].get(name, {}).get("delta_kg"),
                }
            )
        enriched = {"component_context": {**context, "instances": enriched_rows}}
        raw_masses = {name: float(row["mass"]) for name, row in masses.items()}
        details, error = oracle._component_context_findings(
            enriched, "cad", top[0], raw_masses, covered=set(entry_names)
        )
        _require(error is None, "Component context: " + str(error))
        return {
            "assembly_mass_kg": top[0],
            "independent_mass_kg": expected_recombination[0],
            "direct_sum_mass_kg": direct_sum[0],
            "reconciliation": reconciliation,
            # JSON model types only: the report schema is a document, and tuple
            # keys would not survive the saved-report round trip.
            "effective_instances": [[name, code] for name, code in sorted(entries)],
            "component_context": details,
        }

    gate("physics.closure", closure)
    return checks


def _effective_reading_summary(name, record):
    """A surface-only fallback or native override measured in the assembly context."""

    reference = record["reference"]
    _require(reference["used_api"] == "IMassProperty2.GetMomentOfInertia(0)", f"{name}: unqualified inertia API")
    _require(
        reference.get("scope") == "assembly_component_group",
        f"{name}: effective instance scope must be assembly_component_group",
    )
    _require(reference.get("product_convention") == "solidworks_standard", f"{name}: unqualified inertia convention")
    _require(reference.get("reference_point") == "center_of_mass", f"{name}: effective tensor must be about COM")
    _require(reference.get("use_system_units") is True, f"{name}: native SI units are required")
    _require(bool(reference.get("selection_api")), f"{name}: effective selection API is unrecorded")
    _require(bool(reference.get("override_api")), f"{name}: effective override API is unrecorded")
    _require(
        record.get("measurement_basis") == "cad_authored_effective_instance"
        and reference.get("measurement_basis") in (None, "cad_authored_effective_instance"),
        f"{name}: effective measurement basis changed",
    )
    _require(record.get("mode") == "effective_instance", f"{name}: effective mode changed")
    _require(record.get("state") == "sheet_only", f"{name}: effective state changed")
    _require(
        reference.get("selected_components") == [name],
        f"{name}: effective instance must select exactly this occurrence",
    )
    flags = reference.get("overrides")
    flag_keys = {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"}
    _require(
        isinstance(flags, dict)
        and set(flags) == flag_keys
        and all(isinstance(value, bool) for value in flags.values()),
        f"{name}: missing override evidence",
    )
    _require(any(flags.values()), f"{name}: authored effective basis without an active override")
    mass = oracle._finite_positive(record["mass"], name + " mass")
    com = oracle._finite_vector(record["com"], name + " COM")
    tensor = oracle._raw_tensor(name, record)
    _require(np.isfinite(tensor).all(), f"{name}: nonfinite tensor")
    eigenvalues = np.linalg.eigvalsh(tensor)
    _require(
        eigenvalues[0] > 0 and eigenvalues[-1] <= sum(eigenvalues[:2]) + 1e-10 * eigenvalues[-1],
        f"{name}: nonphysical tensor",
    )
    return {
        "mass_kg": mass,
        "com_m": com.tolist(),
        "inertia_kg_m2": oracle._tensor6(tensor),
        "api": reference["used_api"],
        "scope": reference["scope"],
        # Ratified producer shape: the sheet-only row carries the basis itself.
        "measurement_basis": record.get("measurement_basis"),
    }


def _reading_summary(name, record):
    if isinstance(record, dict) and record.get("mode") == "effective_instance":
        return _effective_reading_summary(name, record)
    mass, com, tensor = _reading(name, record, "part_document")
    return {
        "mass_kg": mass,
        "com_m": com.tolist(),
        "inertia_kg_m2": oracle._tensor6(tensor),
        "api": record["reference"]["used_api"],
        "scope": record["reference"].get("scope"),
    }


def _closure_delta(top_level: dict, leaf_total: dict) -> dict:
    """The verifier's own copy of the assembly-minus-leaves diagnostics."""

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


def _instance_measurement_required(masses: dict, instances: dict) -> set[str]:
    """Flagless leaf instances whose native singleton mass differs from the part row.

    These are the only occurrences allowed (and required) to carry the
    cad_assembly_instance_measurement basis; descendants of overridden
    instances stay diagnostic because the substitution replaces them.
    """

    overridden = {name for name, row in instances.items() if any((row.get("overrides") or {}).values())}
    required = set()
    for name, row in masses.items():
        if not isinstance(row, dict) or name in overridden or row.get("mode") == "effective_instance":
            continue
        if any(name == ancestor or name.startswith(ancestor + "/") for ancestor in overridden):
            continue
        context_row = instances.get(name)
        if not isinstance(context_row, dict) or any((context_row.get("overrides") or {}).values()):
            continue
        context_mass = float(context_row.get("context_mass_kg") or 0.0)
        part_mass = float(row.get("mass") or 0.0)
        if abs(context_mass - part_mass) > max(1e-12, 1e-9 * max(abs(context_mass), abs(part_mass))):
            required.add(name)
    return required


def _derive_closure(native: dict, raw_scene: dict, masses: dict, source: dict) -> dict:
    """Rebuild every derived closure finding from the sealed raw facts alone.

    This never reads the Linux report: the report is a receipt to compare against.
    """

    context = native.get("component_context")
    if not isinstance(context, dict):
        raise ValueError("the native mass closure carries no component context")
    rows = context.get("instances")
    if not isinstance(rows, list) or not rows:
        raise ValueError("the native component context carries no instance rows")
    leaves = [row["name"] for row in raw_scene.get("components") or []]
    body = {"id": "assembly", "components": leaves}
    try:
        leaf_total = oracle._raw_world(body, raw_scene, masses, {}, use_effective=False)
    except ValueError:
        leaf_total = None
    effective_total = oracle._raw_world(body, raw_scene, masses, {})
    parents = sorted(
        name
        for name, value in oracle._notes_effective(raw_scene).items()
        if str(value.get("document_type") or value.get("role") or "") == "assembly"
        and any(leaf == name or leaf.startswith(name + "/") for leaf in leaves)
    )
    top_most = [
        name
        for name in parents
        if not any(other != name and (name == other or name.startswith(other + "/")) for other in parents)
    ]
    body_members: dict[str, str] = {}
    for entry in source.get("bodies") or []:
        if not isinstance(entry, dict):
            continue
        owner = str(entry.get("name") or entry.get("id") or "")
        for member in entry.get("components") or []:
            body_members[str(member)] = owner
    blockers = []
    for name in parents:
        descendants = {leaf for leaf in leaves if leaf == name or leaf.startswith(name + "/")}
        owners = sorted({body_members.get(leaf, "") for leaf in descendants} - {""})
        if not owners or len(owners) != 1:
            blockers.append(
                {
                    "code": "effective_instance_spans_bodies",
                    "component": name,
                    "bodies": owners,
                    "descendants": sorted(descendants)[:50],
                }
            )
    parsed = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError("the native component context carries a malformed instance row")
        parsed.append((row["name"], row))
    if len({name for name, _entry in parsed}) != len(parsed):
        raise ValueError("the native component context carries duplicate instance rows")
    masses_by_name = {
        name: float((payload or {}).get("mass") or 0.0)
        for name, payload in masses.items()
        if isinstance(payload, dict)
    }
    basis_by_node = {
        name: sum(
            mass for leaf, mass in masses_by_name.items() if leaf == name or leaf.startswith(name + "/")
        )
        for name, _row in parsed
    }
    instances = {}
    for name, row in parsed:
        basis = basis_by_node[name]
        context_mass = float(row["context_mass_kg"])
        instances[name] = {"document_basis_mass_kg": basis, "delta_kg": context_mass - basis}
    top_rows = [row for name, row in parsed if row.get("depth") == 0]
    if not top_rows:
        raise ValueError("the native component context has no top-level rows")
    context_kg = sum(float(row["context_mass_kg"]) for row in top_rows)
    document_kg = sum(basis_by_node[name] for name, row in parsed if row.get("depth") == 0)
    assembly_kg = context.get("assembly_mass_kg")
    assembly_kg = None if assembly_kg is None else float(assembly_kg)
    part_rows = {name for name, row in parsed if str(row.get("document_type") or "") == "part"}
    covered_documents = {leaf for leaf in leaves if leaf in part_rows}
    overridden = {
        "top_level_instances": len(top_rows),
        "mass": sum(1 for _name, row in parsed if (row.get("overrides") or {}).get("OverrideMass")),
        "com": sum(1 for _name, row in parsed if (row.get("overrides") or {}).get("OverrideCenterOfMass")),
        "inertia": sum(1 for _name, row in parsed if (row.get("overrides") or {}).get("OverrideMomentsOfInertia")),
        "any": sum(1 for _name, row in parsed if any((row.get("overrides") or {}).values())),
        "top_level_overridden": sum(1 for row in top_rows if any((row.get("overrides") or {}).values())),
        "examples": [
            name for name, row in parsed if (row.get("overrides") or {}).get("OverrideMass")
        ][:50],
    }
    leaf_payload = (
        None
        if leaf_total is None
        else {
            "mass": leaf_total["mass"],
            "com": leaf_total["com"].tolist(),
            "inertia": leaf_total["inertia"].tolist(),
        }
    )
    top_level = native.get("top_level")
    delta = _closure_delta(top_level, leaf_total) if isinstance(top_level, dict) and leaf_total else None
    by_instance = dict(parsed)
    unassigned = []
    for name in sorted(set(leaves) - set(body_members)):
        row = by_instance.get(name)
        if not isinstance(row, dict):
            raise ValueError("unassigned occurrence has no native assembly-context mass: " + name)
        value = row.get("context_mass_kg")
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("unassigned occurrence has an invalid native mass: " + name)
        reference = (masses.get(name) or {}).get("reference") or {}
        unassigned.append(
            {
                "component": name,
                "document": row.get("document"),
                "configuration": reference.get("configuration"),
                "parent": row.get("parent"),
                "mass_kg": float(value),
            }
        )
    unassigned_mass = math.fsum(entry["mass_kg"] for entry in unassigned)
    native_mass = float(top_level["mass"]) if isinstance(top_level, dict) and "mass" in top_level else None
    full_mass = float(effective_total["mass"])
    selection = {
        "native_mass_kg": native_mass,
        "selected_mass_kg": full_mass - unassigned_mass,
        "unassigned_mass_kg": unassigned_mass,
        "unassigned": unassigned,
        "accounting_delta_kg": None if native_mass is None else native_mass - full_mass,
        "whole_mass_qualified": (
            not unassigned and native_mass is not None and abs(native_mass - full_mass) <= 1e-12
        ),
    }
    return {
        "leaf_total": leaf_payload,
        "effective_total": {
            "mass": effective_total["mass"],
            "com": effective_total["com"].tolist(),
            "inertia": effective_total["inertia"].tolist(),
            "substituted": top_most,
        },
        "blockers": blockers,
        "delta": delta,
        "totals": {
            "context_kg": context_kg,
            "document_kg": document_kg,
            "assembly_kg": assembly_kg,
            "context_minus_document_kg": context_kg - document_kg,
            "context_minus_assembly_kg": None if assembly_kg is None else context_kg - assembly_kg,
        },
        "overridden": overridden,
        "coverage": {
            "leaf_documents": len(leaves),
            "leaf_documents_covered": len(covered_documents),
            "leaf_documents_uncovered": sorted(set(leaves) - covered_documents)[:50],
        },
        "instances": {
            name: {"name": name, **instances[name]} for name, _row in parsed
        },
        "selection": selection,
    }


def _compare_derived(recorded, mine, label: str, _require) -> None:
    """Bind the Linux report's derived block to the verifier's own recomputation."""

    if isinstance(mine, dict):
        _require(isinstance(recorded, dict) and set(recorded) == set(mine), label + ": derived keys changed")
        for key in sorted(mine):
            _compare_derived(recorded[key], mine[key], label + "." + key, _require)
        return
    if isinstance(mine, list):
        _require(isinstance(recorded, list) and len(recorded) == len(mine), label + ": derived list changed")
        left = list(recorded)
        right = list(mine)
        if right and all(isinstance(item, str) for item in right):
            left, right = sorted(left), sorted(right)
        elif right and all(isinstance(item, dict) and "name" in item for item in right):
            left = sorted(left, key=lambda item: item.get("name"))
            right = sorted(right, key=lambda item: item.get("name"))
        for index, (recorded_item, mine_item) in enumerate(zip(left, right, strict=True)):
            _compare_derived(recorded_item, mine_item, f"{label}[{index}]", _require)
        return
    if isinstance(mine, bool) or mine is None or isinstance(mine, str):
        _require(recorded == mine, label + ": derived value changed")
        return
    if isinstance(mine, (int, float)):
        _require(
            isinstance(recorded, (int, float))
            and not isinstance(recorded, bool)
            and abs(float(recorded) - float(mine)) <= max(1e-12, 1e-6 * abs(float(mine))),
            label + ": derived number changed",
        )
        return
    _require(recorded == mine, label + ": derived value changed")
