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
from ..sources.solidworks import verify as oracle

MASS_RTOL = 1e-6
MASS_CLOSURE_ATOL = 1e-12
COM_ATOL_M = 5e-5
TENSOR_RTOL = 1e-4


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


def verify_urdf_mass_equality(bundle_root: Path) -> dict:
    """Mandatory whole-CAD equality for the delivered URDF.

    The actual delivered URDF XML inertial mass sum must equal the bound
    ``evidence/raw/mass_closure.json`` whole-assembly mass with an absolute
    tolerance of 1e-12 kg and no relative tolerance. Missing, malformed or
    non-recorded whole evidence fails; masses are never normalized or adjusted.
    This gate is independent of the broader per-link mass/COM/tensor accuracy
    checks, which keep their own relative tolerances.
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
    delta = total - whole
    _require(
        abs(delta) <= MASS_CLOSURE_ATOL,
        f"URDF mass sum {total!r} differs from the whole-CAD reading {whole!r} (delta {delta!r} kg)",
    )
    return {
        "urdf_mass_kg": total,
        "whole_cad_mass_kg": whole,
        "delta_kg": delta,
        "atol_kg": MASS_CLOSURE_ATOL,
        "rtol": 0.0,
        "inertials": len(values),
    }


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


def _compare(actual, expected, label):
    _require(np.isclose(actual[0], expected[0], atol=1e-12, rtol=MASS_RTOL), label + ": mass differs")
    _require(np.allclose(actual[1], expected[1], atol=COM_ATOL_M, rtol=0), label + ": COM differs")
    scale = max(float(np.max(np.abs(expected[2]))), 1e-20)
    _require(
        float(np.max(np.abs(actual[2] - expected[2]))) <= TENSOR_RTOL * scale + 1e-15, label + ": full tensor differs"
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
            and record.get("schema_version") == "solidworks-to-urdf.effective-instances/v1",
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
        expected = set(leaf_rows) | overridden
        for name in sorted(overridden):
            row = instances[name]
            _require(
                isinstance(row.get("effective"), dict) and not row.get("effective_error"),
                name + ": native overrides carry no usable effective measurement",
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
                code in (oracle.EFFECTIVE_CODE_SHEET_ONLY, oracle.EFFECTIVE_CODE_INSTANCE),
                name + ": effective warning carries another code",
            )
            _require(entry.get("blocking") is False, name + ": effective warning must be explicitly non-blocking")
            _require(entry.get("qualification") == qualification, name + ": effective qualification statement changed")
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
            _require(any(flags.values()), name + ": effective reading without an active override")
            _require(
                flags == (instances.get(name) or {}).get("overrides"),
                name + ": effective flags differ from the native context",
            )
            _require(entry.get("overrides") == flags, name + ": warning flags differ from the sealed reading")
            _require(
                reference.get("effective_instance") is True
                and reference.get("measurement_basis") == "cad_authored_effective_instance",
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
            else:
                row = leaf_rows.get(name)
                _require(isinstance(row, dict), name + ": surface warning has no captured reading row")
                row_reference = row.get("reference")
                _require(isinstance(row_reference, dict), name + ": effective row has no reference block")
                for field in core_reference_fields:
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
                    measurement.get("measurement_basis")
                    == row_reference.get("measurement_basis")
                    == "cad_authored_effective_instance",
                    name + ": effective measurement basis changed",
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
    for body in source["bodies"]:

        def reconstruct(body=body):
            name = body["name"]
            world = oracle._raw_world(body, raw, masses, declared)
            rotation, origin = oracle._transform(datums[body["frame"]["coordinate_system"]])
            expected = (world["mass"], rotation.T @ (world["com"] - origin), rotation.T @ world["inertia"] @ rotation)
            actual = oracle._canonical_in_link_frame(links[name])
            _compare(actual, expected, name)
            return {
                "mass_kg": expected[0],
                "com_m": expected[1].tolist(),
                "inertia_kg_m2": oracle._tensor6(expected[2]),
            }

        gate("physics.link." + body["name"], reconstruct)

    def closure():
        payload = read_data(confined(root, "raw/mass_closure.json"))
        _require(
            payload["status"] == "recorded" and payload["mode"] == "full",
            "Full whole-assembly physics evidence is required",
        )
        top = _reading("whole assembly", payload["top_level"], "assembly_document")
        context = payload["component_context"]
        _require(context["status"] == "recorded" and not context["errors"], "Incomplete component-context evidence")
        entries = oracle.load_effective_instances(root)
        entry_names = sorted({name for name, _code in entries})
        overridden = {
            str(row["name"]): row
            for row in context["instances"]
            if any((row.get("overrides") or {}).values())
        }
        _require(
            entry_names == sorted(overridden),
            "Effective instance record does not cover the native overrides exactly",
        )
        for name, row in overridden.items():
            _require(
                entries[(name, oracle.EFFECTIVE_CODE_INSTANCE)].get("overrides") == row["overrides"],
                name + ": effective flags differ from the native context",
            )
        body = {"id": "assembly", "components": [row["name"] for row in raw["components"]]}
        rebuilt = oracle._raw_world(body, raw, masses, {})
        expected = (rebuilt["mass"], rebuilt["com"], rebuilt["inertia"])
        _compare(top, expected, "Whole assembly / effective instance sum")
        leaf = payload["leaf_total"]
        if oracle.effective_math_map(masses, raw):
            recorded = payload.get("effective_total")
            _require(isinstance(recorded, dict), "Effective total is missing")
            _compare(
                (float(recorded["mass"]), np.asarray(recorded["com"]), np.asarray(recorded["inertia"])),
                expected,
                "Captured effective closure",
            )
            plain = oracle._raw_world(body, raw, masses, {}, use_effective=False)
            _compare(
                (float(leaf["mass"]), np.asarray(leaf["com"]), np.asarray(leaf["inertia"])),
                (plain["mass"], plain["com"], plain["inertia"]),
                "Captured leaf closure",
            )
        else:
            _compare(
                (float(leaf["mass"]), np.asarray(leaf["com"]), np.asarray(leaf["inertia"])),
                expected,
                "Captured leaf closure",
            )
        raw_masses = {name: float(row["mass"]) for name, row in masses.items()}
        details, error = oracle._component_context_findings(
            payload, "cad", top[0], raw_masses, covered=set(entry_names)
        )
        _require(error is None, "Component context: " + str(error))
        return {
            "assembly_mass_kg": top[0],
            "independent_mass_kg": expected[0],
            "effective_instances": sorted(entries),
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
        reference.get("measurement_basis") == "cad_authored_effective_instance",
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
        "measurement_basis": reference["measurement_basis"],
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
