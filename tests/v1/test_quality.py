"""Independent analytic and adversarial checks; fixtures grant no native qualification."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from description_pipeline.io import PipelineError, canonical, write_json
from description_pipeline.sources.solidworks import verify as oracle
from description_pipeline.sources.solidworks.scene import closure_delta
from description_pipeline.verification import solidworks_urdf as quality
from description_pipeline.verification.solidworks_physics import verify_physics, verify_urdf_mass_equality


class PhysicsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bundle = Path(directory.name)
        self.root = self.bundle / "evidence"
        first = np.eye(4)
        first[:3, 3] = [0.1, 0.2, 0]
        second = np.array([[0, -1, 0, -0.2], [1, 0, 0, 0.1], [0, 0, 1, 0.3], [0, 0, 0, 1]], dtype=float)
        datum = np.array([[0, -1, 0, 0.05], [1, 0, 0, -0.03], [0, 0, 1, 0.1], [0, 0, 0, 1]], dtype=float)
        self.source = {
            "material_source": "cad",
            "assembly": "robot.SLDASM",
            "configuration": "Default",
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": ["a", "b"],
                    "frame": {"coordinate_system": "base_datum"},
                }
            ],
        }
        # The capture session seals its own assembly identity (absolute native
        # path); the archived definition keeps the package-relative name.
        self.captured_identity = {
            "assembly": "C:\\endpoint\\prepared\\robot.SLDASM",
            "configuration": "Default",
        }
        material = {
            "part": {"name": "Analytic control material", "database": "unit-test"},
            "bodies": [{"material": {}, "effective_source": "part"}],
        }
        reference = {
            "used_api": "IMassProperty2.GetMomentOfInertia(0)",
            "product_convention": "solidworks_standard",
            "scope": "part_document",
            "axes": "part_document_axes",
            "reference_point": "center_of_mass",
            "use_system_units": True,
            "configuration": "Default",
            "part_document": "C:\\fixture\\a.SLDPRT",
            "volume_m3": 0.001,
            "density_kg_m3": 1000.0,
            "overrides": {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False},
            "material_assignment": material,
        }
        self.masses = {
            "a": {
                "mass": 1.0,
                "com": [0, 0, 0],
                "inertia": np.diag([0.002, 0.003, 0.004]).tolist(),
                "reference": copy.deepcopy(reference),
            },
            "b": {
                "mass": 2.0,
                "com": [0, 0, 0],
                "inertia": np.diag([0.004, 0.006, 0.008]).tolist(),
                "reference": copy.deepcopy(reference),
            },
        }
        self.raw = {
            "document": "C:\\fixture\\captured.SLDASM",
            "components": [
                {"name": "a", "transform": first.ravel().tolist()},
                {"name": "b", "transform": second.ravel().tolist()},
            ],
            "coordinate_systems": {"base_datum": datum.ravel().tolist()},
            "mass_properties": self.masses,
        }
        # Closed-form two-point reduced-mass contribution, including all cross terms.
        delta = np.array([-0.3, -0.1, 0.3])
        tensor_world = np.diag([0.008, 0.007, 0.012]) + 2 / 3 * (
            float(delta @ delta) * np.eye(3) - np.outer(delta, delta)
        )
        com_world = np.array([-0.1, 0.4 / 3, 0.2])
        tensor_local = datum[:3, :3].T @ tensor_world @ datum[:3, :3]
        com_local = datum[:3, :3].T @ (com_world - datum[:3, 3])
        self.model = {
            "links": [
                {
                    "name": "base_link",
                    "inertial": {
                        "mass": 3.0,
                        "xyz": com_local.tolist(),
                        "rpy": [0, 0, 0],
                        "inertia": [
                            tensor_local[0, 0],
                            tensor_local[0, 1],
                            tensor_local[0, 2],
                            tensor_local[1, 1],
                            tensor_local[1, 2],
                            tensor_local[2, 2],
                        ],
                    },
                }
            ]
        }
        flags = {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}
        # The split native record: observations only, as the capture writes them.
        context = {
            "schema_version": "description-pipeline.solidworks-component-mass-context/v1",
            "status": "recorded",
            "assembly_mass_kg": 3.0,
            "instances": [
                {
                    "name": name,
                    "parent": None,
                    "depth": 0,
                    "document": "C:\\fixture\\" + name + ".SLDPRT",
                    "document_type": "part",
                    "context_mass_kg": value["mass"],
                    "context_volume_m3": 0.001,
                    "overrides": dict(flags),
                    "effective": None,
                    "effective_error": None,
                }
                for name, value in self.masses.items()
            ],
            "errors": [],
            "reference": {"method": "IMassProperty2 (SelectedItems = component instance)"},
        }
        world = {"mass": 3.0, "com": com_world.tolist(), "inertia": tensor_world.tolist()}
        self.closure = {
            "schema_version": "description-pipeline.solidworks-mass-closure/v1",
            "status": "recorded",
            "mode": "full",
            "top_level": {
                **world,
                "reference": {**reference, "scope": "assembly_document", "axes": "assembly_document_axes"},
            },
            "leaf_total": world,
            "component_context": context,
        }
        self.write()

    def write(self):
        self.raw["mass_properties"] = copy.deepcopy(self.masses)
        write_json(self.root / "raw/scene_raw.json", self.raw)
        write_json(self.root / "raw/mass_properties.json", self.masses)
        write_json(self.root / "raw/coordinate_systems.json", self.raw["coordinate_systems"])
        write_json(self.root / "raw/declared_masses.json", {"items": {}, "evidence": None})
        native = self._native_closure()
        write_json(self.root / "raw/mass_closure.json", native)
        write_json(
            self.bundle / "reports/mass-closure.json",
            {
                "schema_version": "solidworks-to-urdf.mass-closure-report/v1",
                "native": native,
                "derived": self._derived_closure(),
            },
        )
        write_json(self.root / "manifest.json", {"identity": copy.deepcopy(self.captured_identity)})

    def _native_closure(self):
        """The split native record: observations only, no derived math."""

        native = copy.deepcopy(self.closure)
        context = native.get("component_context")
        if isinstance(context, dict):
            for row in context.get("instances") or []:
                if isinstance(row, dict):
                    row.pop("document_basis_mass_kg", None)
                    row.pop("delta_kg", None)
            for key in (
                "totals",
                "overridden",
                "effective_total",
                "blockers",
                "leaf_documents",
                "leaf_documents_covered",
                "leaf_documents_uncovered",
            ):
                context.pop(key, None)
        for key in ("leaf_total", "leaf_components", "delta", "effective_total", "blockers"):
            native.pop(key, None)
        return native

    def _derived_closure(self):
        """The Linux-side derived diagnostics the report must carry."""

        context = self.closure["component_context"]
        rows = [row for row in context.get("instances") or [] if isinstance(row, dict)]
        leaves = [row["name"] for row in self.raw["components"]]
        masses_by_name = {name: float(row["mass"]) for name, row in self.masses.items()}
        basis = {
            row["name"]: sum(
                mass
                for leaf, mass in masses_by_name.items()
                if leaf == row["name"] or leaf.startswith(row["name"] + "/")
            )
            for row in rows
        }
        top_rows = [row for row in rows if row.get("depth") == 0]
        context_kg = sum(float(row["context_mass_kg"]) for row in top_rows)
        document_kg = sum(basis[row["name"]] for row in top_rows)
        assembly_kg = context.get("assembly_mass_kg")
        part_rows = {row["name"] for row in rows if row.get("document_type") == "part"}
        covered = {leaf for leaf in leaves if leaf in part_rows}
        effective = copy.deepcopy(self.closure.get("effective_total"))
        if effective is None:
            effective = {**copy.deepcopy(self.closure["leaf_total"]), "substituted": []}
        effective.setdefault("substituted", [])
        return {
            "leaf_total": copy.deepcopy(self.closure["leaf_total"]),
            "effective_total": effective,
            "blockers": copy.deepcopy(self.closure.get("blockers") or []),
            "delta": closure_delta(self.closure["top_level"], self.closure["leaf_total"]),
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
                "inertia": sum(
                    1 for row in rows if (row.get("overrides") or {}).get("OverrideMomentsOfInertia")
                ),
                "any": sum(1 for row in rows if any((row.get("overrides") or {}).values())),
                "top_level_overridden": sum(
                    1 for row in top_rows if any((row.get("overrides") or {}).values())
                ),
                "examples": [
                    row["name"] for row in rows if (row.get("overrides") or {}).get("OverrideMass")
                ][:50],
            },
            "coverage": {
                "leaf_documents": len(leaves),
                "leaf_documents_covered": len(covered),
                "leaf_documents_uncovered": sorted(set(leaves) - covered)[:50],
            },
            "instances": {
                row["name"]: {
                    "name": row["name"],
                    "document_basis_mass_kg": basis[row["name"]],
                    "delta_kg": float(row["context_mass_kg"]) - basis[row["name"]],
                }
                for row in rows
            },
        }

    def failures(self):
        self.write()
        checks = verify_physics(self.root, self.source, self.model)
        return [check["id"] for check in checks if not check["passed"]]

    def test_analytic_rotated_translated_parts_and_link_datum_match(self):
        self._record_clean_provenance()
        self.assertEqual([], self.failures())

    def test_translation_mutation_is_rejected_after_resealing_all_raw_copies(self):
        self.raw["components"][1]["transform"][3] += 0.01
        self.assertIn("physics.link.base_link", self.failures())
        self.assertIn("physics.closure", self.failures())

    def test_unknown_or_absent_convention_is_rejected(self):
        self.masses["a"]["reference"].pop("product_convention")
        self.assertIn("physics.reading.a", self.failures())

    def test_unqualified_fallback_is_rejected(self):
        self.masses["b"]["reference"]["used_api"] = "GetMassProperties2"
        self.assertIn("physics.reading.b", self.failures())

    def test_missing_or_wrong_reading_context_fails_even_when_values_match(self):
        for key, value in (
            ("scope", None),
            ("scope", "assembly_document"),
            ("axes", "assembly_document_axes"),
            ("reference_point", "origin"),
            ("use_system_units", False),
            ("overrides", {}),
        ):
            with self.subTest(key=key, value=value):
                original = copy.deepcopy(self.masses["a"]["reference"])
                self.masses["a"]["reference"][key] = value
                self.assertIn("physics.reading.a", self.failures())
                self.masses["a"]["reference"] = original

    def test_wrong_whole_scope_leaf_tensor_and_part_override_are_rejected(self):
        self.closure["top_level"]["reference"]["scope"] = "part_document"
        self.assertIn("physics.closure", self.failures())
        self.closure["top_level"]["reference"]["scope"] = "assembly_document"
        self.closure["leaf_total"]["inertia"][0][1] += 0.001
        self.assertIn("physics.closure", self.failures())
        self.masses["a"]["reference"]["overrides"]["OverrideMass"] = True
        self.assertIn("physics.reading.a", self.failures())

    def test_offdiagonal_sign_mutation_is_rejected(self):
        self.model["links"][0]["inertial"]["inertia"][1] *= -1
        self.assertIn("physics.link.base_link", self.failures())

    def test_link_datum_mutation_is_rejected(self):
        self.raw["coordinate_systems"]["base_datum"][7] += 0.01
        self.assertIn("physics.link.base_link", self.failures())

    def test_missing_material_is_rejected(self):
        self.masses["a"]["reference"]["material_assignment"].pop("part")
        self.assertIn("physics.authority", self.failures())

    def _record_clean_provenance(self) -> None:
        """The canonical records a current CAD capture seals with every material verified."""

        write_json(
            self.root / "raw/material_provenance.json",
            {
                "schema_version": "solidworks-to-urdf.material-provenance/v1",
                "assembly": self.captured_identity["assembly"],
                "configuration": self.source["configuration"],
                "material_source": "cad",
                "measured_source": "cad_api",
                "warnings": [],
            },
        )
        write_json(
            self.root / "raw/effective_instances.json",
            {
                "schema_version": "solidworks-to-urdf.effective-instances/v1",
                "assembly": self.captured_identity["assembly"],
                "configuration": self.source["configuration"],
                "readings": {},
                "warnings": [],
            },
        )

    def _mark_unverified_material(self) -> None:
        """Mark a's captured assignment with the recorded missing-material wrapper."""

        verbatim = {
            "schema_version": "swbridge.material-assignment/v1",
            "configuration": "Default",
            "query_configuration": "Default",
            "part_api": "IPartDoc.GetMaterialPropertyName2",
            "body_api": "IBody2.GetMaterialPropertyName",
            "part": {"name": "", "database": ""},
            "body_count": 1,
            "bodies": [
                {"index": 0, "name": "solid", "material": {"name": "", "database": ""}, "effective_source": "part"}
            ],
        }
        assignment = {
            "schema_version": "swbridge.material-assignment/v1",
            "configuration": "Default",
            "unverified_reason": "cad_material_provenance_missing",
            "message": "Every solid requires an explicit physical material; default density is not evidence",
            "missing_body_indices": [0],
            "material_assignment": copy.deepcopy(verbatim),
        }
        self.masses["a"]["reference"]["material_assignment"] = copy.deepcopy(assignment)
        self.write()

    def _record_unverified_material(self) -> None:
        self._mark_unverified_material()
        reference = self.masses["a"]["reference"]
        assignment = reference["material_assignment"]
        write_json(
            self.root / "raw/material_provenance.json",
            {
                "schema_version": "solidworks-to-urdf.material-provenance/v1",
                "assembly": self.captured_identity["assembly"],
                "configuration": self.source["configuration"],
                "material_source": "cad",
                "measured_source": "cad_api",
                "warnings": [
                    {
                        "code": "cad_material_provenance_missing",
                        "message": assignment["message"],
                        "blocking": False,
                        "component": "a",
                        "document": reference["part_document"],
                        "configuration": reference["configuration"],
                        "missing_body_indices": copy.deepcopy(assignment["missing_body_indices"]),
                        "material_assignment": copy.deepcopy(assignment),
                        "measured": {
                            "source": "cad_api",
                            "scope": reference.get("scope"),
                            "mass_kg": self.masses["a"]["mass"],
                            "center_of_mass_m": copy.deepcopy(self.masses["a"]["com"]),
                            "inertia_kg_m2": copy.deepcopy(self.masses["a"]["inertia"]),
                            "volume_m3": reference["volume_m3"],
                            "density_kg_m3": reference["density_kg_m3"],
                        },
                    }
                ],
            },
        )

    def test_unverified_material_is_a_warning_not_a_failure(self):
        self._record_unverified_material()
        checks = verify_physics(self.root, self.source, self.model)
        by_id = {check["id"]: check for check in checks}
        self.assertTrue(by_id["physics.authority"]["passed"], by_id["physics.authority"])
        self.assertEqual(by_id["physics.authority"]["details"]["unverified_material"], ["a"])
        provenance = by_id["physics.material_provenance"]
        self.assertTrue(provenance["passed"], provenance)
        self.assertTrue(provenance["details"]["recorded"])
        self.assertEqual(provenance["details"]["unverified_material"], ["a"])
        self.assertFalse(provenance["details"]["warnings"][0]["blocking"])

    def test_material_provenance_record_is_required_even_without_warnings(self):
        self._record_clean_provenance()
        (self.root / "raw/material_provenance.json").unlink()
        self.assertIn("physics.material_provenance", self.failures())

    def test_material_provenance_record_is_required_and_bound_to_the_readings(self):
        def load() -> dict:
            return json.loads((self.root / "raw/material_provenance.json").read_text())

        def store(payload: dict) -> None:
            write_json(self.root / "raw/material_provenance.json", payload)

        def warning(payload: dict) -> dict:
            return payload["warnings"][0]

        def silent_use() -> None:
            self._mark_unverified_material()

        def deletion_after_sealing() -> None:
            self._record_unverified_material()
            (self.root / "raw/material_provenance.json").unlink()

        def empty_warnings() -> None:
            self._record_unverified_material()
            payload = load()
            payload["warnings"] = []
            store(payload)

        def wrong_code() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["code"] = "cad_material_read_failed"
            store(payload)

        def changed_assignment() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["material_assignment"]["material_assignment"]["part"]["name"] = "Invented"
            store(payload)

        def changed_document() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["document"] = "C:\\elsewhere.SLDPRT"
            store(payload)

        def changed_configuration() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["configuration"] = "Other"
            store(payload)

        def changed_body_indices() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["missing_body_indices"] = [1]
            store(payload)

        def blocking_flag() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["blocking"] = True
            store(payload)

        def unknown_component() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["component"] = "zzz"
            store(payload)

        def uncovered_component() -> None:
            self._record_unverified_material()
            payload = load()
            entry = json.loads(json.dumps(warning(payload)))
            entry["component"] = "b"
            payload["warnings"].append(entry)
            store(payload)

        def repeated_component() -> None:
            self._record_unverified_material()
            payload = load()
            payload["warnings"].append(json.loads(json.dumps(warning(payload))))
            store(payload)

        def changed_measured_mass() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["measured"]["mass_kg"] = 0.5
            store(payload)

        def changed_measured_com() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["measured"]["center_of_mass_m"] = [0.0, 0.0, 1.0]
            store(payload)

        def changed_measured_inertia() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["measured"]["inertia_kg_m2"][0][0] = 0.02
            store(payload)

        def changed_measured_volume() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["measured"]["volume_m3"] = 0.002
            store(payload)

        def changed_measured_density() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["measured"]["density_kg_m3"] = 900.0
            store(payload)

        def negative_measured_mass() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["measured"]["mass_kg"] = -1.0
            store(payload)

        def deleted_blocking() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload).pop("blocking")
            store(payload)

        def changed_warning_message() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["message"] = "All physical materials are qualified"
            store(payload)

        def changed_measured_scope() -> None:
            self._record_unverified_material()
            payload = load()
            warning(payload)["measured"]["scope"] = "component"
            store(payload)

        def invented_scope() -> None:
            self._record_unverified_material()
            self.masses["a"]["reference"]["scope"] = "assembly_document"
            self.write()
            payload = load()
            warning(payload)["measured"]["scope"] = "assembly_document"
            store(payload)

        def changed_assembly() -> None:
            self._record_unverified_material()
            payload = load()
            payload["assembly"] = "C:\\elsewhere.SLDASM"
            store(payload)

        def changed_captured_assembly() -> None:
            self._record_unverified_material()
            self.captured_identity["assembly"] = "C:\\endpoint\\prepared\\other.SLDASM"
            self.write()

        def changed_record_configuration() -> None:
            self._record_unverified_material()
            payload = load()
            payload["configuration"] = "Other"
            store(payload)

        def changed_material_source() -> None:
            self._record_unverified_material()
            payload = load()
            payload["material_source"] = "documented_table"
            store(payload)

        def changed_measured_source() -> None:
            self._record_unverified_material()
            payload = load()
            payload["measured_source"] = "swbridge"
            store(payload)

        for label, mutate in (
            ("changed_warning_message", changed_warning_message),
            ("silent_use", silent_use),
            ("deletion_after_sealing", deletion_after_sealing),
            ("empty_warnings", empty_warnings),
            ("wrong_code", wrong_code),
            ("changed_assignment", changed_assignment),
            ("changed_document", changed_document),
            ("changed_configuration", changed_configuration),
            ("changed_body_indices", changed_body_indices),
            ("blocking_flag", blocking_flag),
            ("unknown_component", unknown_component),
            ("uncovered_component", uncovered_component),
            ("repeated_component", repeated_component),
            ("changed_measured_mass", changed_measured_mass),
            ("changed_measured_com", changed_measured_com),
            ("changed_measured_inertia", changed_measured_inertia),
            ("changed_measured_volume", changed_measured_volume),
            ("changed_measured_density", changed_measured_density),
            ("negative_measured_mass", negative_measured_mass),
            ("deleted_blocking", deleted_blocking),
            ("changed_measured_scope", changed_measured_scope),
            ("invented_scope", invented_scope),
            ("changed_assembly", changed_assembly),
            ("changed_captured_assembly", changed_captured_assembly),
            ("changed_record_configuration", changed_record_configuration),
            ("changed_material_source", changed_material_source),
            ("changed_measured_source", changed_measured_source),
        ):
            with self.subTest(case=label):
                self.setUp()
                mutate()
                self.assertIn("physics.material_provenance", self.failures())

    def _write_effective(self) -> None:
        """Seal the current a-row state as the canonical effective instance record."""

        reference = self.masses["a"]["reference"]
        reading = {
            "role": "leaf",
            "document_type": "part",
            "mass": self.masses["a"]["mass"],
            "com": copy.deepcopy(self.masses["a"]["com"]),
            "inertia": copy.deepcopy(self.masses["a"]["inertia"]),
            "reference": copy.deepcopy(reference),
        }
        context = self.closure["component_context"]
        row = next(entry for entry in context["instances"] if entry["name"] == "a")
        row["overrides"] = copy.deepcopy(reference["overrides"])
        row["effective"] = copy.deepcopy(reading)
        self.raw.setdefault("notes", {})["effective_instances"] = {"a": copy.deepcopy(reading)}
        self.closure["effective_total"] = {
            key: copy.deepcopy(self.closure["top_level"][key]) for key in ("mass", "com", "inertia")
        }
        self.closure["effective_total"]["substituted"] = ["a"]
        self.write()
        qualification = (
            "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
        )
        measurement = {
            "scope": reference["scope"],
            "axes": reference["axes"],
            "used_api": reference["used_api"],
            "selection_api": reference["selection_api"],
            "override_api": reference["override_api"],
            "mass_kg": self.masses["a"]["mass"],
            "center_of_mass_m": copy.deepcopy(self.masses["a"]["com"]),
            "inertia_kg_m2": copy.deepcopy(self.masses["a"]["inertia"]),
            "volume_m3": reference.get("volume_m3"),
            "density_kg_m3": reference.get("density_kg_m3"),
        }
        warnings = [
            {
                "code": "cad_instance_override_effective",
                "blocking": False,
                "component": "a",
                "role": reading["role"],
                "document_type": reading["document_type"],
                "configuration": reference["configuration"],
                "part_document": reference["part_document"],
                "assembly_document": reference["assembly_document"],
                "assembly_configuration": reference["assembly_configuration"],
                "selected_components": copy.deepcopy(reference["selected_components"]),
                "overrides": copy.deepcopy(reference["overrides"]),
                "qualification": qualification,
                "measurement": copy.deepcopy(measurement),
            },
            {
                "code": "cad_surface_only_effective_instance",
                "blocking": False,
                "component": "a",
                "document": reference["part_document"],
                "configuration": reference["configuration"],
                "assembly_document": reference["assembly_document"],
                "assembly_configuration": reference["assembly_configuration"],
                "selected_components": copy.deepcopy(reference["selected_components"]),
                "part_document_read": "unavailable",
                "fallback_reason": "cad_part_document_mass_unavailable",
                "fallback_error_code": reference["fallback_error_code"],
                "solid_bodies": reference["solid_bodies"],
                "sheet_bodies": reference["sheet_bodies"],
                "overrides": copy.deepcopy(reference["overrides"]),
                "qualification": qualification,
                "measurement": {**copy.deepcopy(measurement), "state": self.masses["a"]["state"],
                               "measurement_basis": reference["measurement_basis"]},
            },
        ]
        write_json(
            self.root / "raw/effective_instances.json",
            {
                "schema_version": "solidworks-to-urdf.effective-instances/v1",
                "assembly": self.captured_identity["assembly"],
                "configuration": self.source["configuration"],
                "readings": {"a": reading},
                "warnings": warnings,
            },
        )

    def _mark_effective_instance(self) -> None:
        """Replace a's reading with the sealed surface-only effective instance."""

        placement = next(row for row in self.raw["components"] if row["name"] == "a")
        grid = np.array(placement["transform"]).reshape(4, 4)
        rotation, translation = grid[:3, :3], grid[:3, 3]
        local_com = np.array(self.masses["a"]["com"], dtype=float)
        local_tensor = np.array(self.masses["a"]["inertia"], dtype=float)
        # The sealed effective reading is recorded verbatim in assembly axes.
        self.masses["a"]["com"] = (rotation @ local_com + translation).tolist()
        self.masses["a"]["inertia"] = (rotation @ local_tensor @ rotation.T).tolist()
        reference = self.masses["a"]["reference"]
        reference.pop("material_assignment", None)
        reference.update(
            {
                "used_api": "IMassProperty2.GetMomentOfInertia(0)",
                "selection_api": "IMassProperty2.SelectedItems = (IComponent2)",
                "override_api": "IMassProperty2.GetOverrideOptions",
                "scope": "assembly_component_group",
                "axes": "assembly_document_axes",
                "product_convention": "solidworks_standard",
                "reference_point": "center_of_mass",
                "use_system_units": True,
                "effective_instance": True,
                "measurement_basis": "cad_authored_effective_instance",
                "selected_components": ["a"],
                "assembly_document": self.raw["document"],
                "assembly_configuration": self.source["configuration"],
                "part_document": "C:\\fixture\\a.SLDPRT",
                "configuration": "Default",
                "solid_bodies": 0,
                "sheet_bodies": 2350,
                "fallback_reason": "cad_part_document_mass_unavailable",
                "fallback_error_code": "cad_empty_model",
                "overrides": {
                    "OverrideMass": True,
                    "OverrideCenterOfMass": True,
                    "OverrideMomentsOfInertia": True,
                },
            }
        )
        self.masses["a"]["mode"] = "effective_instance"
        self.masses["a"]["state"] = "sheet_only"
        self.raw.setdefault("notes", {})["bodies:a"] = {"solid": 0, "sheet": 2350}
        self._record_clean_provenance()
        self._write_effective()

    def test_effective_instance_is_accepted_and_bound(self):
        self._mark_effective_instance()
        checks = {check["id"]: check for check in verify_physics(self.root, self.source, self.model)}
        self.assertTrue(checks["physics.effective_instances"]["passed"], checks["physics.effective_instances"])
        self.assertTrue(checks["physics.reading.a"]["passed"], checks["physics.reading.a"])
        self.assertEqual([], self.failures())

    def test_effective_instance_evidence_is_required_and_bound(self):
        def load() -> dict:
            return json.loads((self.root / "raw/effective_instances.json").read_text())

        def store(payload: dict) -> None:
            write_json(self.root / "raw/effective_instances.json", payload)

        def removed_record() -> None:
            (self.root / "raw/effective_instances.json").unlink()

        def changed_flags() -> None:
            payload = load()
            warning = next(w for w in payload["warnings"] if w["code"] == "cad_instance_override_effective")
            warning["overrides"] = {
                "OverrideMass": True,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            }
            store(payload)

        def changed_measured_mass() -> None:
            payload = load()
            warning = next(w for w in payload["warnings"] if w["code"] == "cad_instance_override_effective")
            warning["measurement"]["mass_kg"] = 1.5
            store(payload)

        def changed_warning_blocking() -> None:
            payload = load()
            warning = next(w for w in payload["warnings"] if w["code"] == "cad_surface_only_effective_instance")
            warning["blocking"] = True
            store(payload)

        def changed_row_sheet_count() -> None:
            self.masses["a"]["reference"]["sheet_bodies"] = 999
            self._write_effective()

        def inactive_overrides() -> None:
            self.masses["a"]["reference"]["overrides"] = {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            }
            self._write_effective()

        def duplicated_selection() -> None:
            self.masses["a"]["reference"]["selected_components"] = ["a", "b"]
            self._write_effective()

        def changed_assembly_identity() -> None:
            self.masses["a"]["reference"]["assembly_document"] = "C:\\elsewhere.SLDASM"
            self._write_effective()

        for label, mutate in (
            ("removed_record", removed_record),
            ("changed_flags", changed_flags),
            ("changed_measured_mass", changed_measured_mass),
            ("changed_warning_blocking", changed_warning_blocking),
            ("changed_row_sheet_count", changed_row_sheet_count),
            ("inactive_overrides", inactive_overrides),
            ("duplicated_selection", duplicated_selection),
            ("changed_assembly_identity", changed_assembly_identity),
        ):
            with self.subTest(case=label):
                self.setUp()
                self._mark_effective_instance()
                mutate()
                self.assertIn("physics.effective_instances", self.failures())

    def test_effective_parent_substitution_requires_one_body(self):
        def transform(rotation, translation):
            matrix = np.eye(4)
            matrix[:3, :3] = rotation
            matrix[:3, 3] = translation
            return matrix.ravel().tolist()

        parent_rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
        parent = transform(parent_rotation, [-1.025, -1.066, -0.988])
        child_a = transform(parent_rotation, [-0.925, -1.166, -0.888])
        child_b = transform(parent_rotation, [-1.125, -0.966, -1.088])
        leaf_q = transform(np.eye(3), [0.5, -0.25, 0.75])
        raw = {
            "components": [
                {"name": "p/a", "transform": child_a},
                {"name": "p/b", "transform": child_b},
                {"name": "q", "transform": leaf_q},
            ],
            "coordinate_systems": copy.deepcopy(self.raw["coordinate_systems"]),
            "notes": {
                "effective_instances": {
                    "p": {
                        "mass": 4.0,
                        "com": [-1.0, -1.05, -0.9],
                        "inertia": np.diag([0.01, 0.02, 0.03]).tolist(),
                        "reference": {"selected_components": ["p"]},
                    }
                }
            },
        }
        masses = {
            "p/a": {"mass": 1.0, "com": [0.1, 0.0, 0.0], "inertia": np.diag([0.001, 0.002, 0.003]).tolist(),
                    "reference": {"scope": "part_document", "product_convention": "solidworks_standard"}},
            "p/b": {"mass": 2.0, "com": [0.0, 0.1, 0.0], "inertia": np.diag([0.001, 0.002, 0.003]).tolist(),
                    "reference": {"scope": "part_document", "product_convention": "solidworks_standard"}},
            "q": {"mass": 1.0, "com": [0.0, 0.0, 0.0], "inertia": np.diag([0.004, 0.005, 0.006]).tolist(),
                  "reference": {"scope": "part_document", "product_convention": "solidworks_standard"}},
        }
        world = oracle._raw_world({"id": "x", "components": ["p/a", "p/b", "q"]}, raw, masses, {})
        self.assertAlmostEqual(world["mass"], 5.0, places=12)
        expected_com = (4.0 * np.array([-1.0, -1.05, -0.9]) + 1.0 * np.array([0.5, -0.25, 0.75])) / 5.0
        self.assertTrue(np.allclose(world["com"], expected_com, atol=1e-12))
        with self.assertRaises(ValueError):
            oracle._raw_world({"id": "x", "components": ["p/a"]}, raw, masses, {})

    def test_unknown_unverified_reasons_are_not_warnable(self):
        def unknown_reason() -> None:
            self._record_unverified_material()
            self.masses["a"]["reference"]["material_assignment"]["unverified_reason"] = "cad_material_read_failed"

        def boolean_reason() -> None:
            self._record_unverified_material()
            self.masses["a"]["reference"]["material_assignment"]["unverified_reason"] = False

        for label, mutate in (("unknown_reason", unknown_reason), ("boolean_reason", boolean_reason)):
            with self.subTest(case=label):
                self.setUp()
                mutate()
                self.assertIn("physics.authority", self.failures())

    def test_override_and_incomplete_context_are_rejected(self):
        self.closure["component_context"]["instances"][0]["overrides"]["OverrideCenterOfMass"] = True
        self.assertIn("physics.closure", self.failures())
        self.closure["component_context"]["status"] = "partial"
        self.assertIn("physics.closure", self.failures())

    def test_missing_or_changed_assembly_reading_is_rejected(self):
        self.closure["top_level"]["mass"] = 3.1
        self.assertIn("physics.closure", self.failures())
        self.closure["mode"] = "mass_only"
        self.assertIn("physics.closure", self.failures())


class StructuralQualityTests(unittest.TestCase):
    def test_windows_pywin32_integer_version_is_a_valid_runtime_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "reports/tool.json",
                {
                    "schema_version": "solidworks-to-urdf.tool/v1",
                    "pipeline_id": "solidworks-to-urdf",
                    "source_sha256": "a" * 64,
                    "version": "1.0.0",
                    "runtime": {"python": "3.12.10", "packages": {"numpy": "2.5.3", "pywin32": "311"}},
                },
            )
            self.assertEqual("1.0.0", quality._tool(root)["version"])

    def test_captured_matrices_are_rowmajor_and_reflections_fail(self):
        transform = np.array([[0, -1, 0, 0.1], [1, 0, 0, 0.2], [0, 0, 1, 0.3], [0, 0, 0, 1]], dtype=float)
        np.testing.assert_array_equal(transform, quality._cad_pose(transform.ravel()))
        transform[0, 1] = 1
        with self.assertRaises(PipelineError):
            quality._cad_pose(transform.ravel())

    def test_rotated_native_shaft_line_uses_component_frame(self):
        transform = quality._pose([0.2, -0.1, 0.3], [0.4, 0.5, 0.6])
        child = transform @ quality._pose([0.01, 0, 0.1], [0, 0, 0])
        reference = {"component": "shaft", "face_index": 3, "body_type": "solid"}
        authored = {"axis_reference": reference, "axis": [0, 0, 1]}
        record = {
            **reference,
            "surface": "cylinder",
            "radius_m": 0.01,
            "coordinate_frame": "component_local",
            "axis_point_m": [0.01, 0, 0.1],
            "axis_direction": [0, 0, -1],
        }
        self.assertAlmostEqual(0, quality._axis(authored, record, {"shaft": transform}, child)["offset_m"])
        record["axis_point_m"][0] += 0.01
        with self.assertRaises(PipelineError):
            quality._axis(authored, record, {"shaft": transform}, child)

    def test_signed_axis_and_continuous_limits_match_author(self):
        authored = {
            "name": "joint",
            "parent": "base_link",
            "child": "arm_link",
            "type": "continuous",
            "axis": [0, 0, 1],
            "limits": {"effort": 1.0, "velocity": 2.0},
        }
        canonical_joint = {**authored, "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
        node = ET.fromstring(
            '<joint type="continuous"><parent link="base_link"/><child link="arm_link"/>'
            '<origin xyz="0 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>'
            '<limit effort="1" velocity="2"/></joint>'
        )
        frames = {"base_link": np.eye(4), "arm_link": np.eye(4)}
        diagnostics: list[dict] = []
        quality._joint(node, authored, canonical_joint, {}, frames, diagnostics)
        self.assertEqual(diagnostics, [])
        node.find("axis").set("xyz", "0 0 -1")
        with self.assertRaises(PipelineError):
            quality._joint(node, authored, canonical_joint, {}, frames, [])

    def test_authored_zero_limits_load_with_a_motion_warning(self):
        authored = {
            "name": "joint",
            "parent": "base_link",
            "child": "arm_link",
            "type": "continuous",
            "axis": [0, 0, 1],
            "limits": {"effort": 0.0, "velocity": 0.0},
        }
        canonical_joint = {**authored, "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
        node = ET.fromstring(
            '<joint type="continuous"><parent link="base_link"/><child link="arm_link"/>'
            '<origin xyz="0 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>'
            '<limit effort="0" velocity="0"/></joint>'
        )
        frames = {"base_link": np.eye(4), "arm_link": np.eye(4)}
        diagnostics: list[dict] = []
        result = quality._joint(node, authored, canonical_joint, {}, frames, diagnostics)
        self.assertEqual(result["limits"], {"effort": 0.0, "velocity": 0.0})
        self.assertEqual([item["code"] for item in diagnostics], ["urdf.motion_unqualified"])
        self.assertFalse(diagnostics[0]["blocking"])

        negative = {**authored, "limits": {"effort": -1.0, "velocity": 0.0}}
        canonical_negative = {**negative, "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
        node_negative = ET.fromstring(
            '<joint type="continuous"><parent link="base_link"/><child link="arm_link"/>'
            '<origin xyz="0 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>'
            '<limit effort="-1" velocity="0"/></joint>'
        )
        with self.assertRaises(PipelineError):
            quality._joint(node_negative, negative, canonical_negative, {}, frames, [])

    def test_incomplete_delivery_and_forged_green_report_fail_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "reports/quality.json", {"passed": True})
            report = quality.check_bundle(root)
            self.assertFalse(report["passed"])
            self.assertIn("report.binding", [check["id"] for check in report["checks"]])
            canonical(report)
            self.assertEqual(report["subject_status"], "unavailable")
            self.assertNotIn(str(root).encode(), canonical(report))


class UrdfMassEqualityTests(unittest.TestCase):
    """The delivered URDF must equal the bound whole-CAD mass exactly (atol 1e-12, rtol 0)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "urdf").mkdir()
        (self.root / "evidence/raw").mkdir(parents=True)
        self.masses = [1.0, 2.0]
        self.write()

    def write(self, whole=3.0, status="recorded", mode="full", top=True, write_closure=True):
        body = "".join(
            f'<link name="link_{index}"><inertial><origin xyz="0 0 0" rpy="0 0 0"/>'
            f'<mass value="{mass}"/>'
            '<inertia ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.001"/>'
            "</inertial></link>"
            for index, mass in enumerate(self.masses)
        )
        (self.root / "urdf/robot.urdf").write_text(f'<robot name="unit">{body}</robot>', encoding="utf-8")
        closure = self.root / "evidence/raw/mass_closure.json"
        if write_closure:
            payload = {"status": status, "mode": mode}
            if top:
                payload["top_level"] = {"mass": whole}
            write_json(closure, payload)
        elif closure.exists():
            closure.unlink()

    def test_exact_whole_cad_mass_passes(self):
        details = verify_urdf_mass_equality(self.root)
        self.assertEqual(3.0, details["urdf_mass_kg"])
        self.assertEqual(3.0, details["whole_cad_mass_kg"])
        self.assertEqual(0.0, details["delta_kg"])
        self.assertEqual(1e-12, details["atol_kg"])
        self.assertEqual(0.0, details["rtol"])
        self.assertEqual(2, details["inertials"])

    def test_mismatch_previously_hidden_by_relative_tolerance_fails(self):
        self.masses = [1.000001, 2.0]
        self.write(whole=3.0)
        # The broader accuracy comparison would have accepted this with rtol 1e-6.
        self.assertTrue(np.isclose(3.000001, 3.0, atol=1e-12, rtol=1e-6))
        with self.assertRaises(PipelineError):
            verify_urdf_mass_equality(self.root)

    def test_missing_or_malformed_whole_evidence_fails(self):
        for label, kwargs in (
            ("missing_file", {"write_closure": False}),
            ("failed_status", {"status": "failed"}),
            ("partial_mode", {"mode": "partial"}),
            ("no_top_level", {"top": False}),
            ("non_numeric_mass", {"whole": None}),
            ("non_positive_mass", {"whole": 0.0}),
        ):
            with self.subTest(label=label):
                self.masses = [1.0, 2.0]
                self.write(**kwargs)
                with self.assertRaises(PipelineError):
                    verify_urdf_mass_equality(self.root)

    def test_urdf_without_inertial_masses_fails(self):
        (self.root / "urdf/robot.urdf").write_text('<robot name="unit"><link name="link_0"/></robot>', encoding="utf-8")
        with self.assertRaises(PipelineError):
            verify_urdf_mass_equality(self.root)

    def test_only_actual_link_masses_count_and_fixed_reference_frames_are_allowed(self):
        path = self.root / "urdf/robot.urdf"
        xml = path.read_text().replace(
            "</robot>",
            '<link name="sensor_frame"/><extension><inertial><mass value="100"/></inertial></extension></robot>',
        )
        path.write_text(xml)
        self.assertEqual(3.0, verify_urdf_mass_equality(self.root)["urdf_mass_kg"])

    def test_malformed_or_ambiguous_link_mass_fails_with_a_pipeline_error(self):
        path = self.root / "urdf/robot.urdf"
        original = path.read_text()
        for replacement in ('<mass value="not-a-number"/>', '<mass value="1.0"/><mass value="2.0"/>'):
            with self.subTest(replacement=replacement):
                path.write_text(original.replace('<mass value="1.0"/>', replacement, 1))
                with self.assertRaises(PipelineError):
                    verify_urdf_mass_equality(self.root)


class OptionalDesignExpectationTests(unittest.TestCase):
    def test_absent_expectations_do_not_execute_or_require_a_passing_placeholder(self):
        definition = {"checks": {}}
        gates = quality._Gates()
        quality._author_expectations(gates, definition, {}, [])
        self.assertEqual(gates.checks, [])
        required = quality.required_checks(definition)
        self.assertNotIn("physics.expected_mass", required)
        self.assertNotIn("geometry.expected_extent", required)
        for core in [
            "physics.mass_closure_equality",
            "physics.independent",
            "model.schema",
            "geometry.coverage",
            "geometry.assets",
            "source.integrity",
        ]:
            self.assertIn(core, required)
        report = {
            "passed": True,
            "required_checks": required,
            "checks": [{"id": key, "state": "passed", "passed": True} for key in required],
        }
        quality.require_qualified_report(report)
        removed = copy.deepcopy(report)
        removed["checks"] = [row for row in removed["checks"] if row["id"] != "physics.mass_closure_equality"]
        removed["required_checks"].remove("physics.mass_closure_equality")
        with self.assertRaises(PipelineError):
            quality.require_qualified_report(removed)

    def test_supplied_expectations_are_required_evaluated_and_not_defaulted(self):
        model = {"links": [{"inertial": {"mass": 2.0}}]}
        points = [np.array([[0, 0, 0], [0.5, 0.2, 0.1]])]
        definition = {"checks": {"expected_mass_kg": [1, 3], "expected_extent_m": [0.4, 0.6]}}
        gates = quality._Gates()
        quality._author_expectations(gates, definition, model, points)
        self.assertEqual({row["id"] for row in gates.checks}, {"physics.expected_mass", "geometry.expected_extent"})
        self.assertTrue(all(row["passed"] for row in gates.checks))
        required = quality.required_checks(definition)
        self.assertIn("physics.expected_mass", required)
        self.assertIn("geometry.expected_extent", required)
        for value in [None, [3, 4], [2, 2], [1, float("nan")], [True, 3], ["1", "3"], [1, 2, 3]]:
            candidate = {"checks": {"expected_mass_kg": value}}
            gates = quality._Gates()
            quality._author_expectations(gates, candidate, model, points)
            self.assertIn("physics.expected_mass", quality.required_checks(candidate))
            self.assertEqual(len(gates.checks), 1)
            self.assertFalse(gates.checks[0]["passed"], value)


class SurfaceOnlyGeometryExemptionTests(unittest.TestCase):
    """The zero-volume gate accepts only sealed, qualified surface-only CAD records."""

    def _delivery(self, tmp, record):
        root = Path(tmp)
        (root / "evidence/raw").mkdir(parents=True, exist_ok=True)
        write_json(root / "evidence/raw/effective_instances.json", record)
        return root

    def _qualified(self, occurrence="pcb-1"):
        return {
            "schema_version": "solidworks-to-urdf.effective-instances/v1",
            "assembly": "robot.SLDASM",
            "configuration": "默认",
            "readings": {},
            "warnings": [
                {
                    "code": "cad_surface_only_effective_instance",
                    "blocking": False,
                    "component": occurrence,
                    "document": "board.SLDPRT",
                    "part_document_read": "unavailable",
                    "solid_bodies": 0,
                    "sheet_bodies": 2350,
                }
            ],
        }

    def test_qualified_surface_only_record_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._delivery(tmp, self._qualified())
            self.assertTrue(quality._qualified_surface_only(root, "pcb-1"))

    def test_unqualified_zero_volume_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._delivery(tmp, self._qualified())
            self.assertFalse(quality._qualified_surface_only(root, "other-1"))
        unqualified = self._qualified()
        unqualified["warnings"][0]["solid_bodies"] = 3
        with tempfile.TemporaryDirectory() as tmp:
            root = self._delivery(tmp, unqualified)
            self.assertFalse(quality._qualified_surface_only(root, "pcb-1"))
        blocking = self._qualified()
        blocking["warnings"][0]["blocking"] = True
        with tempfile.TemporaryDirectory() as tmp:
            root = self._delivery(tmp, blocking)
            self.assertFalse(quality._qualified_surface_only(root, "pcb-1"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(quality._qualified_surface_only(root, "pcb-1"))

