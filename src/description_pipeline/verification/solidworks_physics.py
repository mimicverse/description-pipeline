"""Strict independent physics gates over the actual native capture protocol.

The reconstruction kernels belong to the independent source verifier, not the
scene builder. Part readings are rotated and translated into assembly space,
combined about their common COM, then expressed in the CAD link datum. The
whole assembly and component-context readings must close independently.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..io import PipelineError, confined, read_data
from ..sources.solidworks import verify as oracle

MASS_RTOL = 1e-6
COM_ATOL_M = 5e-5
TENSOR_RTOL = 1e-4


def _require(passed, message):
    if not bool(passed):
        raise PipelineError(message)


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
            for name, record in masses.items():
                assignment = record["reference"]["material_assignment"]
                _require(
                    not assignment.get("unverified_reason") and bool(assignment.get("bodies")),
                    name + ": unverified material",
                )
                for body in assignment["bodies"]:
                    selected = body["material"] if body["effective_source"] == "body" else assignment["part"]
                    _require(
                        bool(selected["name"]) and bool(selected["database"]), name + ": missing material assignment"
                    )
        return {"mode": mode, "inertia_model": "scaled_cad_uniform_density" if declared else "cad_mass_properties"}

    gate("physics.authority", authority)
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
        body = {"id": "assembly", "components": [row["name"] for row in raw["components"]]}
        rebuilt = oracle._raw_world(body, raw, masses, {})
        expected = (rebuilt["mass"], rebuilt["com"], rebuilt["inertia"])
        _compare(top, expected, "Whole assembly / independent part sum")
        leaf = payload["leaf_total"]
        _compare(
            (float(leaf["mass"]), np.asarray(leaf["com"]), np.asarray(leaf["inertia"])),
            expected,
            "Captured leaf closure",
        )
        context = payload["component_context"]
        _require(context["status"] == "recorded" and not context["errors"], "Incomplete component-context evidence")
        raw_masses = {name: float(row["mass"]) for name, row in masses.items()}
        details, error = oracle._component_context_findings(payload, "cad", top[0], raw_masses)
        _require(error is None, "Component context: " + str(error))
        # v1 refuses effective mass/COM/tensor overrides even when a table is supplied.
        # A later input contract must explicitly model them before they can qualify.
        for row in context["instances"]:
            _require(not any(row["overrides"].values()), "Unsupported native instance override: " + row["name"])
        return {"assembly_mass_kg": top[0], "independent_mass_kg": expected[0], "component_context": details}

    gate("physics.closure", closure)
    return checks


def _reading_summary(name, record):
    mass, com, tensor = _reading(name, record, "part_document")
    return {
        "mass_kg": mass,
        "com_m": com.tolist(),
        "inertia_kg_m2": oracle._tensor6(tensor),
        "api": record["reference"]["used_api"],
        "scope": record["reference"].get("scope"),
    }
