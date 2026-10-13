"""Independent analytic and adversarial checks; fixtures grant no native qualification."""

from __future__ import annotations

import copy
import hashlib
import struct
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from description_pipeline.io import PipelineError, canonical, write_json
from description_pipeline.sources.solidworks import verify as oracle
from description_pipeline.verification import solidworks_urdf as quality
from description_pipeline.verification.solidworks_physics import verify_physics, verify_urdf_mass_equality
from tests.sources import support


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
        # The record quotes the capture session's assembly path (an environment
        # fact, kept verbatim); the frozen collection identity names the same
        # document package-relative and binds the record by normalized suffix.
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
            "schema_version": "solidworks-to-urdf.component-mass-context/v1",
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
            "schema_version": "solidworks-to-urdf.mass-closure/v1",
            "status": "recorded",
            "mode": "full",
            "top_level": {
                **world,
                "reference": {**reference, "scope": "assembly_document", "axes": "assembly_document_axes"},
            },
            "leaf_total": world,
            "component_context": context,
        }
        self.link_direct = self._link_direct_payload()
        self.write()

    def _link_direct_payload(self) -> dict:
        """The producer-shaped direct link group record this fixture's model stands for."""

        datum = np.array(self.raw["coordinate_systems"]["base_datum"], dtype=float).reshape(4, 4)
        link = next(item for item in self.model["links"] if item["name"] == "base_link")
        inertial = link["inertial"]
        tensor_link = np.array(
            [
                [inertial["inertia"][0], inertial["inertia"][1], inertial["inertia"][2]],
                [inertial["inertia"][1], inertial["inertia"][3], inertial["inertia"][4]],
                [inertial["inertia"][2], inertial["inertia"][4], inertial["inertia"][5]],
            ]
        )
        com_assembly = datum[:3, :3] @ np.array(inertial["xyz"], dtype=float) + datum[:3, 3]
        tensor_assembly = datum[:3, :3] @ tensor_link @ datum[:3, :3].T
        effective = oracle.effective_math_map(self.masses, self.raw)
        selection = sorted(
            str(note["component"])
            for *_rest, note in oracle._resolved_contributions(
                self.source["bodies"][0], self.raw, self.masses, {}, effective
            )
        )
        read = {
            "mass_kg": inertial["mass"],
            "volume_m3": 0.001,
            "density_kg_m3": 1000.0,
            "com_m": com_assembly.tolist(),
            "inertia_tensor_about_com": tensor_assembly.tolist(),
            "inertia_values_about_origin_api": {"error": "fixture"},
            "overrides": {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            },
        }
        return {
            "schema_version": "solidworks-to-urdf.link-group-mass-properties/v1",
            "assembly": self.captured_identity["assembly"],
            "configuration": self.source["configuration"],
            "used_api": "IMassProperty2 (SelectedItems = component set)",
            "selection_rule": "top_most_providers+uncovered_leaves",
            "links": {
                "base_link": {
                    "selection": selection,
                    "reads": [copy.deepcopy(read), copy.deepcopy(read)],
                    "reproducibility": {
                        "mass_delta_kg": 0.0,
                        "com_delta_norm_m": 0.0,
                        "tensor_delta_max_abs": 0.0,
                    },
                }
            },
        }

    def write(self):
        self.raw["mass_properties"] = copy.deepcopy(self.masses)
        native = self._native_closure()
        source = support.default_source_record(
            main_assembly="robot.SLDASM",
            configuration=self.captured_identity["configuration"],
            revision=None,
        )
        capture = {
            "scene": copy.deepcopy(self.raw),
            "components": [
                {"name": name, "mass_properties": copy.deepcopy(reading)}
                for name, reading in self.masses.items()
            ],
            "datums": [
                {"name": name, "matrix": copy.deepcopy(matrix)}
                for name, matrix in self.raw["coordinate_systems"].items()
            ],
            "link_group_mass_properties": copy.deepcopy(self.link_direct),
            "declared_masses": {"items": {}, "evidence": None},
            "mass_closure": native,
            "collection": {
                "kind": "solidworks",
                "evidence_class": "cad",
                "identity": copy.deepcopy(source["identity"]),
                "limits": {"native_cad": True},
                "environment": {"solidworks": {"revision": "34.0"}},
            },
        }
        capture.update(getattr(self, "extra_capture_blocks", {}))
        previous_path = support.capture_file_for(self.root)
        if previous_path.is_file():
            # The recording helpers seal material/effective blocks directly into
            # the capture file; a later write refreshes the derived blocks but
            # must not drop those recorded records.
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            for name, value in previous.items():
                capture.setdefault(name, value)
        support.write_target_delivery(
            self.bundle,
            capture=capture,
            source=source,
        )

    def block(self, name):
        return support.load_capture_block(self.root, name)

    def store_block(self, name, value) -> None:
        support.store_capture_block(self.root, name, value)

    def delete_block(self, name) -> None:
        support.delete_capture_block(self.root, name)

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

    def failures(self):
        self.write()
        checks = verify_physics(self.root, self.source, self.model)
        return [check["id"] for check in checks if not check["passed"]]

    def test_analytic_rotated_translated_parts_and_link_datum_match(self):
        self._record_clean_provenance()
        self.assertEqual([], self.failures())

    def test_closure_com_uses_the_evidence_bound_and_records_the_deltas(self):
        self._record_clean_provenance()
        self.assertEqual([], self.failures())
        # top_level and leaf_total share their nested lists in this fixture; isolate
        # the reconciliation value before mutating it.
        self.closure["top_level"] = copy.deepcopy(self.closure["top_level"])
        base_com = copy.deepcopy(self.closure["top_level"]["com"])

        self.closure["top_level"]["com"] = [base_com[0] + 5e-4, base_com[1], base_com[2]]
        self.assertEqual([], self.failures())
        checks = {check["id"]: check for check in verify_physics(self.root, self.source, self.model)}
        reconciliation = checks["physics.closure"]["details"]["reconciliation"]
        self.assertEqual(reconciliation["com_atol_m"], 1e-3)
        self.assertEqual(reconciliation["tensor_rtol"], 1e-2)
        record = reconciliation["direct_sum"]
        self.assertAlmostEqual(record["com_delta_m"][0], 5e-4, places=12)
        self.assertAlmostEqual(record["com_delta_m"][1], 0.0, places=15)
        self.assertAlmostEqual(record["com_delta_norm_m"], 5e-4, places=12)
        self.assertAlmostEqual(record["mass_delta_kg"], 0.0, places=15)
        self.assertGreater(record["tensor_scale_kg_m2"], 0.0)
        self.assertAlmostEqual(record["tensor_delta_max_abs_kg_m2"], 0.0, places=15)

        self.closure["top_level"]["com"] = [base_com[0] + 2e-3, base_com[1], base_com[2]]
        self.assertIn("physics.closure", self.failures())

        self.closure["top_level"]["com"] = base_com
        self.closure["top_level"]["inertia"][0][0] *= 1.005
        self.assertEqual([], self.failures())
        self.closure["top_level"]["inertia"][0][0] *= 1.05
        self.assertIn("physics.closure", self.failures())

    def test_closure_bound_keeps_mass_strict_and_does_not_widen_other_com_checks(self):
        self._record_clean_provenance()
        self.assertEqual([], self.failures())
        base_mass = self.closure["top_level"]["mass"]

        self.closure["top_level"]["mass"] = base_mass + 1e-4
        self.write()
        checks = {check["id"]: check for check in verify_physics(self.root, self.source, self.model)}
        self.assertFalse(checks["physics.closure"]["passed"])
        self.assertIn("mass differs", checks["physics.closure"]["details"]["error"])
        self.closure["top_level"]["mass"] = base_mass

        link = next(item for item in self.model["links"] if item["name"] == "base_link")
        original = copy.deepcopy(link["inertial"]["xyz"])
        link["inertial"]["xyz"] = [original[0] + 1e-4, original[1], original[2]]
        failed = self.failures()
        self.assertIn("physics.link.base_link", failed)
        self.assertNotIn("physics.closure", failed)
        link["inertial"]["xyz"] = original
        self.assertEqual([], self.failures())

    def test_link_direct_record_selection_and_repeatability_are_gated(self):
        self._record_clean_provenance()
        self.assertEqual([], self.failures())
        entry = self.link_direct["links"]["base_link"]

        selection = copy.deepcopy(entry["selection"])
        entry["selection"] = selection[:-1]
        self.assertIn("physics.link.base_link", self.failures())
        entry["selection"] = selection
        self.assertEqual([], self.failures())

        reproducibility = copy.deepcopy(entry["reproducibility"])
        entry["reproducibility"] = {**reproducibility, "com_delta_norm_m": 0.1}
        self.assertIn("physics.link.base_link", self.failures())
        entry["reproducibility"] = reproducibility

        second = copy.deepcopy(entry["reads"][1])
        second["com_m"] = [second["com_m"][0] + 1e-3, second["com_m"][1], second["com_m"][2]]
        original_reads = entry["reads"]
        entry["reads"] = [entry["reads"][0], second]
        self.assertIn("physics.link.base_link", self.failures())
        entry["reads"] = original_reads
        self.assertEqual([], self.failures())

    def test_link_direct_record_records_active_object_overrides_instead_of_refusing(self):
        self._record_clean_provenance()
        self.assertEqual([], self.failures())
        entry = self.link_direct["links"]["base_link"]

        # A single-occurrence selection surfaces the instance's authored override
        # as an object-level flag in the pristine reopened window; the verifier
        # records it beside the passing link check instead of refusing the read.
        active = {"OverrideMass": True, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}
        for read in entry["reads"]:
            read["overrides"] = dict(active)
        self.write()
        checks = {check["id"]: check for check in verify_physics(self.root, self.source, self.model)}
        self.assertTrue(checks["physics.link_direct_record"]["passed"])
        self.assertTrue(checks["physics.link.base_link"]["passed"])
        details = checks["physics.link.base_link"]["details"]
        self.assertEqual(
            details["object_overrides"],
            [{"read": 0, "active": ["OverrideMass"]}, {"read": 1, "active": ["OverrideMass"]}],
        )
        self.assertIn("recorded verbatim", details["object_overrides_note"])
        self.assertEqual([], self.failures())

        # The evidence stays fail-closed on malformed flags: a non-boolean value
        # is not tolerated, even though active booleans now are.
        entry["reads"][0]["overrides"]["OverrideMass"] = "true"
        self.assertIn("physics.link.base_link", self.failures())

    def test_link_direct_record_is_required_and_schema_bound(self):
        self._record_clean_provenance()
        original = copy.deepcopy(self.link_direct)
        self.delete_block("link_group_mass_properties")
        checks = {check["id"]: check for check in verify_physics(self.root, self.source, self.model)}
        self.assertFalse(checks["physics.link_direct_record"]["passed"])
        self.assertFalse(checks["physics.link.base_link"]["passed"])
        self.assertFalse(checks["physics.closure"]["passed"])
        self.link_direct["schema_version"] = "solidworks-to-urdf.link-group-mass-properties/v2"
        self.write()
        self.assertIn("physics.link_direct_record", self.failures())
        self.link_direct = original
        self.write()
        self.assertEqual([], self.failures())

    def test_component_transform_tamper_keeps_the_direct_binding_and_fails_the_derived_ledger(self):
        # The direct link read is authoritative: a tampered component placement no
        # longer moves link inertials.  It stays visible in the non-blocking
        # partition audit and keeps failing the derived-closure binding.
        self._record_clean_provenance()
        self.raw["components"][1]["transform"][3] += 0.01
        failed = self.failures()
        self.assertNotIn("physics.link.base_link", failed)
        self.assertIn("physics.closure", failed)
        checks = {check["id"]: check for check in verify_physics(self.root, self.source, self.model)}
        audit = checks["physics.link_partition_audit"]["details"]["links"]["base_link"]
        self.assertGreater(audit["com_delta_norm_m"], 1e-3)

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

        self.store_block(
            "material_provenance",
            {
                "schema_version": "solidworks-to-urdf.material-provenance/v1",
                "assembly": self.captured_identity["assembly"],
                "configuration": self.source["configuration"],
                "material_source": "cad",
                "measured_source": "cad_api",
                "warnings": [],
            },
        )
        self.store_block(
            "effective_instances",
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
        self.store_block(
            "material_provenance",
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
        self.delete_block("material_provenance")
        self.assertIn("physics.material_provenance", self.failures())

    def test_material_provenance_record_is_required_and_bound_to_the_readings(self):
        def load() -> dict:
            return self.block("material_provenance")

        def store(payload: dict) -> None:
            self.store_block("material_provenance", payload)

        def warning(payload: dict) -> dict:
            return payload["warnings"][0]

        def silent_use() -> None:
            self._mark_unverified_material()

        def deletion_after_sealing() -> None:
            self._record_unverified_material()
            self.delete_block("material_provenance")

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
            # The record seals the capture-session path it was read under; the
            # frozen source identity names another document, so the suffix
            # binding must refuse.
            self.captured_identity["assembly"] = "C:\\endpoint\\prepared\\other.SLDASM"
            self._record_unverified_material()

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
        # The ratified producer contract keeps `measurement_basis` on the
        # sheet-only mass ROW and re-states it in the sealed reading's reference.
        sealed_reference = {
            **copy.deepcopy(reference),
            "measurement_basis": self.masses["a"].get("measurement_basis") or reference.get("measurement_basis"),
        }
        reading = {
            "role": "leaf",
            "document_type": "part",
            "mass": self.masses["a"]["mass"],
            "com": copy.deepcopy(self.masses["a"]["com"]),
            "inertia": copy.deepcopy(self.masses["a"]["inertia"]),
            "reference": sealed_reference,
        }
        context = self.closure["component_context"]
        row = next(entry for entry in context["instances"] if entry["name"] == "a")
        row["overrides"] = copy.deepcopy(reference["overrides"])
        row["effective"] = copy.deepcopy(reading)
        self.raw.setdefault("notes", {})["effective_instances"] = {"a": copy.deepcopy(reading)}
        self.closure["effective_total"] = {
            key: copy.deepcopy(self.closure["top_level"][key]) for key in ("mass", "com", "inertia")
        }
        # Only assembly-type parents are substitution entries; a leaf effective
        # row is simply its own authoritative reading.
        self.closure["effective_total"]["substituted"] = []
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
                               "measurement_basis": sealed_reference["measurement_basis"]},
            },
        ]
        self.store_block(
            "effective_instances",
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
        self.masses["a"]["measurement_basis"] = "cad_authored_effective_instance"
        self.raw.setdefault("notes", {})["bodies:a"] = {"solid": 0, "sheet": 2350}
        self._record_clean_provenance()
        self._write_effective()

    def test_effective_instance_is_accepted_and_bound(self):
        self._mark_effective_instance()
        checks = {check["id"]: check for check in verify_physics(self.root, self.source, self.model)}
        self.assertTrue(checks["physics.effective_instances"]["passed"], checks["physics.effective_instances"])
        self.assertTrue(checks["physics.reading.a"]["passed"], checks["physics.reading.a"])
        self.assertEqual([], self.failures())

    def test_sheet_only_basis_must_be_the_ratified_row_level_literal(self):
        self._mark_effective_instance()
        self.assertEqual([], self.failures())

        ratified = self.masses["a"].pop("measurement_basis")
        self.assertTrue({"physics.reading.a", "physics.effective_instances"} <= set(self.failures()))

        self.masses["a"]["measurement_basis"] = "cad_assembly_instance_measurement"
        self.assertTrue({"physics.reading.a", "physics.effective_instances"} <= set(self.failures()))

        self.masses["a"]["measurement_basis"] = ratified
        self.masses["a"]["reference"]["measurement_basis"] = "cad_authored_effective_instance_guess"
        self.assertTrue({"physics.reading.a", "physics.effective_instances"} <= set(self.failures()))

        self.masses["a"]["reference"]["measurement_basis"] = "cad_authored_effective_instance"
        self.assertEqual([], self.failures())

    def test_effective_instance_evidence_is_required_and_bound(self):
        def load() -> dict:
            return self.block("effective_instances")

        def store(payload: dict) -> None:
            self.store_block("effective_instances", payload)

        def removed_record() -> None:
            self.delete_block("effective_instances")

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

        def unrelated_effective_reading() -> None:
            payload = load()
            payload["readings"]["unrelated"] = copy.deepcopy(payload["readings"]["a"])
            self.raw["notes"]["effective_instances"] = copy.deepcopy(payload["readings"])
            store(payload)

        for label, mutate in (
            ("removed_record", removed_record),
            ("changed_flags", changed_flags),
            ("changed_measured_mass", changed_measured_mass),
            ("changed_warning_blocking", changed_warning_blocking),
            ("changed_row_sheet_count", changed_row_sheet_count),
            ("inactive_overrides", inactive_overrides),
            ("duplicated_selection", duplicated_selection),
            ("changed_assembly_identity", changed_assembly_identity),
            ("unrelated_effective_reading", unrelated_effective_reading),
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

    def _mark_instance_measurement(self) -> None:
        """a's part row stays the diagnostic; the assembly singleton is authoritative."""

        grid = np.array(next(r for r in self.raw["components"] if r["name"] == "a")["transform"]).reshape(4, 4)
        rotation, translation = grid[:3, :3], grid[:3, 3]
        local_tensor = np.array(self.masses["a"]["inertia"], dtype=float)
        singleton = {
            "role": "leaf",
            "document_type": "part",
            "mass": 1.5,
            "com": translation.tolist(),
            "inertia": (rotation @ local_tensor @ rotation.T).tolist(),
            "reference": {
                "used_api": "IMassProperty2.GetMomentOfInertia(0)",
                "selection_api": "IMassProperty2 (SelectedItems = component instance)",
                "override_api": "IMassProperty2.GetOverrideOptions (same selection)",
                "scope": "assembly_component_group",
                "axes": "assembly_document_axes",
                "reference_point": "center_of_mass",
                "product_convention": "solidworks_standard",
                "use_system_units": True,
                "effective_instance": True,
                "measurement_basis": "cad_assembly_instance_measurement",
                "selected_components": ["a"],
                "configuration": self.masses["a"]["reference"]["configuration"],
                "part_document": self.masses["a"]["reference"]["part_document"],
                "assembly_document": self.raw["document"],
                "assembly_configuration": "Default",
                "overrides": {
                    "OverrideMass": False,
                    "OverrideCenterOfMass": False,
                    "OverrideMomentsOfInertia": False,
                },
            },
            "part_measurement": {
                "mass_kg": self.masses["a"]["mass"],
                "center_of_mass_m": copy.deepcopy(self.masses["a"]["com"]),
                "inertia_kg_m2": copy.deepcopy(self.masses["a"]["inertia"]),
                "scope": "part_document",
                "used_api": self.masses["a"]["reference"]["used_api"],
                "product_convention": "solidworks_standard",
            },
        }
        context = self.closure["component_context"]
        row = next(entry for entry in context["instances"] if entry["name"] == "a")
        row["context_mass_kg"] = 1.5
        row["overrides"] = dict(singleton["reference"]["overrides"])
        row["effective"] = copy.deepcopy(singleton)
        self.raw.setdefault("notes", {})["effective_instances"] = {"a": copy.deepcopy(singleton)}

        def place(name: str, payload: dict):
            frame = np.array(next(r for r in self.raw["components"] if r["name"] == name)["transform"]).reshape(4, 4)
            com = frame[:3, :3] @ np.array(payload["com"], dtype=float) + frame[:3, 3]
            tensor = frame[:3, :3] @ np.array(payload["inertia"], dtype=float) @ frame[:3, :3].T
            return float(payload["mass"]), com, tensor

        a_part = (1.5, np.array(singleton["com"], dtype=float), np.array(singleton["inertia"], dtype=float))
        b_part = place("b", self.masses["b"])
        total = a_part[0] + b_part[0]
        com = (a_part[0] * a_part[1] + b_part[0] * b_part[1]) / total
        inertia = np.zeros((3, 3))
        for mass, part_com, part_tensor in (a_part, b_part):
            delta = part_com - com
            inertia += part_tensor + mass * (float(delta @ delta) * np.eye(3) - np.outer(delta, delta))
        world = {"mass": total, "com": com.tolist(), "inertia": inertia.tolist()}
        context["assembly_mass_kg"] = total
        self.closure["top_level"] = {
            **copy.deepcopy(world),
            "reference": copy.deepcopy(self.closure["top_level"]["reference"]),
        }
        self.closure["effective_total"] = {**copy.deepcopy(world), "substituted": []}
        datum = np.array(self.raw["coordinate_systems"]["base_datum"], dtype=float).reshape(4, 4)
        link_com = datum[:3, :3].T @ (np.array(world["com"]) - datum[:3, 3])
        link_tensor = datum[:3, :3].T @ np.array(world["inertia"]) @ datum[:3, :3]
        self.model["links"][0]["inertial"] = {
            "mass": world["mass"],
            "xyz": link_com.tolist(),
            "rpy": [0.0, 0.0, 0.0],
            "inertia": [
                link_tensor[0, 0],
                link_tensor[0, 1],
                link_tensor[0, 2],
                link_tensor[1, 1],
                link_tensor[1, 2],
                link_tensor[2, 2],
            ],
        }
        self.link_direct = self._link_direct_payload()
        self._record_clean_provenance()
        payload = self.block("effective_instances")
        payload["readings"]["a"] = copy.deepcopy(singleton)
        payload["warnings"] = [
            {
                "code": "cad_instance_measurement_differs_from_part",
                "blocking": False,
                "component": "a",
                "role": "leaf",
                "document_type": "part",
                "configuration": singleton["reference"]["configuration"],
                "part_document": singleton["reference"]["part_document"],
                "assembly_document": singleton["reference"]["assembly_document"],
                "assembly_configuration": singleton["reference"]["assembly_configuration"],
                "selected_components": ["a"],
                "overrides": dict(singleton["reference"]["overrides"]),
                "qualification": (
                    "cad_assembly_instance_measurement; cause_of_part_difference_unproven; "
                    "not geometry-derived, not independently physically qualified"
                ),
                "measurement": {
                    "scope": singleton["reference"]["scope"],
                    "axes": singleton["reference"]["axes"],
                    "used_api": singleton["reference"]["used_api"],
                    "selection_api": singleton["reference"]["selection_api"],
                    "override_api": singleton["reference"]["override_api"],
                    "mass_kg": singleton["mass"],
                    "center_of_mass_m": copy.deepcopy(singleton["com"]),
                    "inertia_kg_m2": copy.deepcopy(singleton["inertia"]),
                    "volume_m3": None,
                    "density_kg_m3": None,
                },
                "part_measurement": copy.deepcopy(singleton["part_measurement"]),
            }
        ]
        self.store_block("effective_instances", payload)
        self.write()

    def test_instance_measurement_is_accepted_and_bound(self):
        self._mark_instance_measurement()
        checks = {check["id"]: check for check in verify_physics(self.root, self.source, self.model)}
        self.assertTrue(checks["physics.effective_instances"]["passed"], checks["physics.effective_instances"])
        self.assertEqual([], self.failures())

    def test_instance_measurement_evidence_is_required_and_bound(self):
        def load() -> dict:
            return self.block("effective_instances")

        def store(payload: dict) -> None:
            self.store_block("effective_instances", payload)

        def removed_entry() -> None:
            payload = load()
            payload["readings"].pop("a")
            payload["warnings"] = []
            store(payload)
            self.raw["notes"]["effective_instances"] = {}
            self.write()

        def active_flag() -> None:
            payload = load()
            payload["readings"]["a"]["reference"]["overrides"]["OverrideMass"] = True
            payload["warnings"][0]["overrides"]["OverrideMass"] = True
            store(payload)

        def wrong_basis() -> None:
            payload = load()
            payload["readings"]["a"]["reference"]["measurement_basis"] = "cad_authored_effective_instance"
            store(payload)

        def changed_part_diagnostic() -> None:
            payload = load()
            payload["readings"]["a"]["part_measurement"]["mass_kg"] = 1.2
            store(payload)

        def negative_tensor() -> None:
            payload = load()
            payload["readings"]["a"]["inertia"][0][0] = -0.002
            payload["warnings"][0]["measurement"]["inertia_kg_m2"][0][0] = -0.002
            store(payload)

        for label, mutate in (
            ("removed_entry", removed_entry),
            ("active_flag", active_flag),
            ("wrong_basis", wrong_basis),
            ("changed_part_diagnostic", changed_part_diagnostic),
            ("negative_tensor", negative_tensor),
        ):
            with self.subTest(case=label):
                self.setUp()
                self._mark_instance_measurement()
                mutate()
                self.assertIn("physics.effective_instances", self.failures())

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
                root / "tool.json",
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

    def test_recomputed_tuple_values_bind_through_the_json_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {"schema_version": "probe", "checks": [], "passed": True, "value": ("a", 1)}
            write_json(root / "reports/quality.json", json.loads(json.dumps(report)))
            with mock.patch.object(quality, "evaluate_bundle", return_value=report):
                result = quality.check_bundle(root)
            self.assertNotIn("report.binding", [check["id"] for check in result["checks"]])

    def test_altered_saved_report_still_fails_with_the_same_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {"schema_version": "probe", "checks": [], "passed": True, "value": ("a", 1)}
            write_json(root / "reports/quality.json", {"schema_version": "probe", "checks": [], "passed": True,
                                                       "value": ["a", 2]})
            with mock.patch.object(quality, "evaluate_bundle", return_value=report):
                result = quality.check_bundle(root)
            binding = [check for check in result["checks"] if check["id"] == "report.binding"]
            self.assertEqual(len(binding), 1)
            self.assertIn("Saved quality report differs from recomputation", binding[0]["details"]["error"])
            self.assertFalse(result["passed"])


class UrdfMassEqualityTests(unittest.TestCase):
    """The delivered URDF must equal the bound whole-CAD mass exactly (atol 1e-12, rtol 0)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        support.write_target_delivery(self.root, capture={})
        self.masses = [1.0, 2.0]
        self.write()

    def store_block(self, name, value) -> None:
        support.store_capture_block(self.root, name, value)

    def delete_block(self, name) -> None:
        support.delete_capture_block(self.root, name)

    def write(self, whole=3.0, status="recorded", mode="full", top=True, write_closure=True):
        body = "".join(
            f'<link name="link_{index}"><inertial><origin xyz="0 0 0" rpy="0 0 0"/>'
            f'<mass value="{mass}"/>'
            '<inertia ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.001"/>'
            "</inertial></link>"
            for index, mass in enumerate(self.masses)
        )
        (self.root / "urdf/robot.urdf").write_text(f'<robot name="unit">{body}</robot>', encoding="utf-8")
        if write_closure:
            payload = {"status": status, "mode": mode}
            if top:
                payload["top_level"] = {"mass": whole}
            self.store_block("mass_closure", payload)
        else:
            self.delete_block("mass_closure")

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

    def _accounting(self, urdf_masses, whole, context, scene_names, bodies):
        self.masses = list(urdf_masses)
        self.write(whole=whole)
        self.store_block("scene", {"components": [{"name": name} for name in scene_names]})
        self.store_block(
            "mass_closure",
            {
                "status": "recorded",
                "mode": "full",
                "top_level": {"mass": whole},
                "component_context": {"status": "recorded", "errors": [], "instances": context},
            },
        )
        return {"bodies": [{"name": "base", "components": bodies}]}

    def test_unassigned_native_mass_closes_the_identity_and_withholds_qualification(self):
        source = self._accounting(
            [1.0],
            3.0,
            [
                {"name": "a", "context_mass_kg": 1.0, "parent": None, "document": "a.SLDPRT"},
                {"name": "b", "context_mass_kg": 2.0, "parent": "frame-9", "document": "b.SLDPRT"},
            ],
            ["a", "b"],
            ["a"],
        )
        details = verify_urdf_mass_equality(self.root, source)
        self.assertEqual(1.0, details["urdf_mass_kg"])
        self.assertEqual(2.0, details["unassigned_mass_kg"])
        self.assertEqual(3.0, details["accounted_mass_kg"])
        self.assertEqual(0.0, details["delta_kg"])
        self.assertFalse(details["whole_mass_qualified"])
        self.assertEqual([item["component"] for item in details["warnings"]], ["b"])
        self.assertEqual(details["unassigned"], [{"component": "b", "document": "b.SLDPRT", "mass_kg": 2.0}])

    def test_unassigned_masses_do_not_explain_an_unrelated_gap(self):
        source = self._accounting(
            [1.0],
            3.0,
            [
                {"name": "a", "context_mass_kg": 1.0},
                {"name": "b", "context_mass_kg": 1.999999},
            ],
            ["a", "b"],
            ["a"],
        )
        with self.assertRaises(PipelineError):
            verify_urdf_mass_equality(self.root, source)

    def test_unread_or_duplicated_unassigned_evidence_fails(self):
        missing = self._accounting([1.0], 3.0, [{"name": "a", "context_mass_kg": 1.0}], ["a", "b"], ["a"])
        with self.assertRaises(PipelineError):
            verify_urdf_mass_equality(self.root, missing)
        duplicated = self._accounting(
            [1.0],
            3.0,
            [
                {"name": "a", "context_mass_kg": 1.0},
                {"name": "b", "context_mass_kg": 1.0},
                {"name": "b", "context_mass_kg": 1.0},
            ],
            ["a", "b"],
            ["a"],
        )
        with self.assertRaises(PipelineError):
            verify_urdf_mass_equality(self.root, duplicated)

    def test_assigned_components_leave_the_identity_strict(self):
        source = self._accounting(
            [1.0],
            3.0,
            [
                {"name": "a", "context_mass_kg": 1.0},
                {"name": "b", "context_mass_kg": 2.0},
            ],
            ["a", "b"],
            ["a", "b"],
        )
        with self.assertRaises(PipelineError):
            verify_urdf_mass_equality(self.root, source)


class DerivedSelectionAccountingTests(unittest.TestCase):
    """The independent recomputation binds each native instance and its own measurements."""

    def test_unassigned_rows_keep_each_instance_identity_and_never_redistribute(self):
        from description_pipeline.verification.solidworks_physics import _derive_closure

        placement = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        masses = {
            "a": {
                "mass": 1.0,
                "com": [0.0, 0.0, 0.0],
                "inertia": [[1e-3, 0.0, 0.0], [0.0, 1e-3, 0.0], [0.0, 0.0, 1e-3]],
                "reference": {"product_convention": "solidworks_standard", "configuration": "Default"},
            },
            "b": {
                "mass": 2.0,
                "com": [0.0, 0.0, 0.0],
                "inertia": [[2e-3, 0.0, 0.0], [0.0, 2e-3, 0.0], [0.0, 0.0, 2e-3]],
                "reference": {"product_convention": "solidworks_standard", "configuration": "Other"},
            },
        }
        native = {
            "top_level": {
                "mass": 3.0,
                "com": [0.0, 0.0, 0.0],
                "inertia": [[3e-3, 0.0, 0.0], [0.0, 3e-3, 0.0], [0.0, 0.0, 3e-3]],
            },
            "component_context": {
                "status": "recorded",
                "assembly_mass_kg": 3.0,
                "instances": [
                    {
                        "name": "a",
                        "parent": None,
                        "depth": 0,
                        "document": "a.SLDPRT",
                        "document_type": "part",
                        "context_mass_kg": 1.0,
                        "context_volume_m3": None,
                        "overrides": {},
                        "effective": None,
                        "effective_error": None,
                    },
                    {
                        "name": "b",
                        "parent": "frame-9",
                        "depth": 1,
                        "document": "b.SLDPRT",
                        "document_type": "part",
                        "context_mass_kg": 2.0,
                        "context_volume_m3": None,
                        "overrides": {},
                        "effective": None,
                        "effective_error": None,
                    },
                ],
                "errors": [],
            },
        }
        scene = {
            "components": [{"name": "a", "transform": placement}, {"name": "b", "transform": placement}],
            "mass_properties": copy.deepcopy(masses),
            "notes": {},
        }
        source = {"bodies": [{"name": "base", "components": ["a"]}]}
        derived = _derive_closure(native, scene, scene["mass_properties"], source)
        selection = derived["selection"]
        self.assertAlmostEqual(selection["native_mass_kg"], 3.0, places=12)
        self.assertAlmostEqual(selection["unassigned_mass_kg"], 2.0, places=12)
        self.assertAlmostEqual(selection["selected_mass_kg"], 1.0, places=12)
        self.assertAlmostEqual(selection["accounting_delta_kg"], 0.0, places=12)
        self.assertFalse(selection["whole_mass_qualified"])
        self.assertEqual(len(selection["unassigned"]), 1)
        row = selection["unassigned"][0]
        self.assertEqual(row["component"], "b")
        self.assertEqual(row["document"], "b.SLDPRT")
        self.assertEqual(row["configuration"], "Other")
        self.assertEqual(row["parent"], "frame-9")
        self.assertAlmostEqual(row["mass_kg"], 2.0, places=12)


class MeshChunkContractTests(unittest.TestCase):
    """Delivered mesh parts must reproduce the captured source records exactly."""

    def _mesh(self, triangles):
        header = b"chunk contract test".ljust(80, b" ")
        records = b"".join(struct.pack("<12fH", 0.0, 0.0, 1.0, *a, *b, *c, 0) for a, b, c in triangles)
        return header + struct.pack("<I", len(triangles)) + records

    def _parts(self, payload, limit):
        total = struct.unpack("<I", payload[80:84])[0]
        count = (total + limit - 1) // limit
        parts = []
        for part in range(count):
            start = limit * part
            size = min(limit, total - start)
            parts.append(
                payload[:80] + struct.pack("<I", size) + payload[84 + 50 * start : 84 + 50 * (start + size)]
            )
        return parts

    def _fixture(self, root, *, limit, mutate):
        triangles = [
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
            ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        ]
        payload = self._mesh(triangles)
        parts = self._parts(payload, limit)
        if mutate is not None:
            mutate(payload, parts)
        (root / "evidence/geometry").mkdir(parents=True, exist_ok=True)
        (root / "meshes/visual").mkdir(parents=True, exist_ok=True)
        (root / "evidence/geometry/0001_mesh.stl").write_bytes(payload)
        names = []
        for index, data in enumerate(parts):
            name = f"meshes/visual/board_0_part_{index}.stl"
            (root / name).write_bytes(data)
            names.append(name)
        visuals = "".join(
            '<visual><origin xyz="0 0 0" rpy="0 0 0"/><geometry>'
            f'<mesh filename="../{name}" scale="1 1 1"/></geometry></visual>'
            for name in names
        )
        node = ET.fromstring(f'<link name="board">{visuals}</link>')
        body = {"components": ["pcb-1"]}
        model_link = {
            "inertial": {
                "xyz": [0.0, 0.0, 0.0],
                "inertia": [1e-6, 0.0, 0.0, 1e-6, 0.0, 1e-6],
                "mass": 1.0,
            },
            "visuals": [
                {"filename": "geometry/0001_mesh.stl", "scale": [1, 1, 1], "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
            ],
            "collisions": [],
        }
        geometry = {
            "pcb-1": {
                "path": "geometry/0001_mesh.stl",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "triangles": 4,
                "scale": [1, 1, 1],
                "xyz": [0, 0, 0],
                "rpy": [0, 0, 0],
            }
        }
        components = {"pcb-1": np.identity(4)}
        return node, body, model_link, geometry, components

    def _run(self, root, *, limit=2, mutate=None):
        node, body, model_link, geometry, components = self._fixture(root, limit=limit, mutate=mutate)
        with mock.patch.object(quality, "CHUNK_TRIANGLES_MAX", limit):
            return quality._geometry(
                root, body, node, model_link, geometry, components, np.identity(4), np.identity(4), set()
            )

    def test_lossless_multipart_delivery_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            used, points = self._run(Path(tmp))
            self.assertEqual(
                used, ["meshes/visual/board_0_part_0.stl", "meshes/visual/board_0_part_1.stl"]
            )
            self.assertEqual(len(points), 12)

    def test_any_part_deviation_fails(self):
        mutations = {
            "swapped_parts": lambda _payload, parts: parts.__setitem__(slice(0, 2), [parts[1], parts[0]]),
            "truncated_part": lambda _payload, parts: parts.__setitem__(1, parts[1][:-50]),
            "corrupt_header": lambda _payload, parts: parts.__setitem__(1, b"X" + parts[1][1:]),
            "wrong_count": lambda _payload, parts: parts.__setitem__(
                0, parts[0][:80] + struct.pack("<I", 1) + parts[0][84:]
            ),
            "dropped_record": lambda _payload, parts: parts.__setitem__(1, parts[1][:84] + parts[1][84 + 50 :]),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp, self.assertRaises(PipelineError):
                self._run(Path(tmp), mutate=mutate)

    def test_chunk_count_boundaries_are_deterministic(self):
        self.assertEqual(quality._mesh_chunk_count(199_999), 1)
        self.assertEqual(quality._mesh_chunk_count(200_000), 2)
        self.assertEqual(quality._mesh_chunk_count(362_415), 2)
        self.assertEqual(quality._mesh_chunk_count(399_998), 2)
        self.assertEqual(quality._mesh_chunk_count(399_999), 3)


class EntityPartitionTests(unittest.TestCase):
    """Native, selected and unassigned entity partitions bind to the sealed scene."""

    def _bundle(self):
        source = {"bodies": [{"name": "base_link", "id": "base_link", "components": ["a", "b"]}]}
        raw = {"components": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
        model = {
            "links": [{"name": "base_link", "id": "base_link", "provenance": {"source_entities": ["a", "b"]}}],
            "provenance": {
                "expected_entities": ["a", "b"],
                "native_entities": ["a", "b", "c"],
                "unassigned_entities": ["c"],
            },
        }
        return source, raw, model

    def test_partitions_are_bound_to_the_sealed_scene(self):
        source, raw, model = self._bundle()
        self.assertEqual(
            quality._entities(source, raw, model),
            {"occurrences": 3, "bodies": 1, "selected": 2, "unassigned": 1},
        )
        for key in ("expected_entities", "native_entities", "unassigned_entities"):
            candidate = copy.deepcopy(model)
            candidate["provenance"][key].pop()
            with self.assertRaises(PipelineError):
                quality._entities(source, raw, candidate)
        missing = copy.deepcopy(model)
        missing["provenance"]["unassigned_entities"] = []
        with self.assertRaises(PipelineError):
            quality._entities(source, raw, missing)
        unknown = copy.deepcopy(source)
        unknown["bodies"][0]["components"] = ["a", "z"]
        with self.assertRaises(PipelineError):
            quality._entities(unknown, raw, model)
        duplicate = copy.deepcopy(raw)
        duplicate["components"].append({"name": "a"})
        with self.assertRaises(PipelineError):
            quality._entities(source, duplicate, model)


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

    QUALIFICATION = (
        "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
    )

    def _delivery(self, tmp, *, qualified=True):
        root = Path(tmp)
        occurrence = "pcb-1"
        record = {
            "schema_version": "solidworks-to-urdf.effective-instances/v1",
            "assembly": "robot.SLDASM",
            "configuration": "默认",
            "readings": {occurrence: {"role": "leaf", "document_type": "part"}} if qualified else {},
            "warnings": (
                [
                    {
                        "code": "cad_surface_only_effective_instance",
                        "blocking": False,
                        "component": occurrence,
                        "document": "board.SLDPRT",
                        "part_document_read": "unavailable",
                        "solid_bodies": 0,
                        "sheet_bodies": 2350,
                        "qualification": self.QUALIFICATION,
                    }
                ]
                if qualified
                else []
            ),
        }
        support.write_target_delivery(
            root,
            capture={
                "effective_instances": record,
                "components": [
                    {
                        "name": occurrence,
                        "mass_properties": {
                            "mode": "effective_instance",
                            "state": "effective_instance",
                            "reference": {"solid_bodies": 0, "sheet_bodies": 2350},
                        },
                    }
                ],
            },
        )
        return root

    def test_qualified_surface_only_record_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._delivery(tmp)
            self.assertTrue(quality._qualified_surface_only(root, "pcb-1"))
            self.assertFalse(quality._qualified_surface_only(root, "other-1"))
            record = support.load_capture_block(root, "effective_instances")
            record["warnings"][0]["solid_bodies"] = False
            support.store_capture_block(root, "effective_instances", record)
            self.assertFalse(quality._qualified_surface_only(root, "pcb-1"))
            rows = support.load_capture_block(root, "components")
            rows[0]["mass_properties"]["reference"]["solid_bodies"] = False
            support.store_capture_block(root, "components", rows)
            self.assertFalse(quality._qualified_surface_only(root, "pcb-1"))
            rows[0]["mass_properties"]["reference"]["solid_bodies"] = 0
            rows[0]["mass_properties"]["mode"] = "document"
            support.store_capture_block(root, "components", rows)
            self.assertFalse(quality._qualified_surface_only(root, "pcb-1"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(quality._qualified_surface_only(root, "pcb-1"))

    def _stl_bytes(self, *, collinear):
        header = b"planar test mesh".ljust(80, b" ")
        if collinear:
            # One valid planar triangle plus one zero-area triangle: the mesh keeps
            # positive area and must be rejected by the degenerate-ratio gate.
            triangles = [
                ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            ]
        else:
            triangles = [
                ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
            ]
        payload = b"".join(struct.pack("<12fH", 0.0, 0.0, 1.0, *a, *b, *c, 0) for a, b, c in triangles)
        return header + struct.pack("<I", len(triangles)) + payload

    def _geometry_fixture(self, root, *, qualified, collinear, raw_payload=None):
        payload = raw_payload or self._stl_bytes(collinear=collinear)
        (root / "evidence/geometry").mkdir(parents=True, exist_ok=True)
        (root / "meshes/visual").mkdir(parents=True, exist_ok=True)
        (root / "evidence/geometry/0001_mesh.stl").write_bytes(payload)
        (root / "meshes/visual/board_0.stl").write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        occurrence = "pcb-1"
        if qualified:
            self._delivery(root)
        node = ET.fromstring(
            '<link name="board"><visual><origin xyz="0 0 0" rpy="0 0 0"/>'
            '<geometry><mesh filename="../meshes/visual/board_0.stl" scale="1 1 1"/></geometry></visual></link>'
        )
        body = {"components": [occurrence]}
        model_link = {
            "inertial": {"xyz": [0.0, 0.0, 0.0], "inertia": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0], "mass": 0.0},
            "visuals": [
                {
                    "filename": "geometry/0001_mesh.stl",
                    "scale": [1, 1, 1],
                    "xyz": [0, 0, 0],
                    "rpy": [0, 0, 0],
                }
            ],
            "collisions": [],
        }
        geometry = {
            occurrence: {
                "path": "geometry/0001_mesh.stl",
                "sha256": digest,
                "triangles": struct.unpack("<I", payload[80:84])[0],
                "scale": [1, 1, 1],
                "xyz": [0, 0, 0],
                "rpy": [0, 0, 0],
            }
        }
        components = {occurrence: np.identity(4)}
        return node, body, model_link, geometry, components

    def _run_geometry(self, root, *, qualified, collinear, raw_payload=None):
        node, body, model_link, geometry, components = self._geometry_fixture(
            root, qualified=qualified, collinear=collinear, raw_payload=raw_payload
        )
        return quality._geometry(
            root, body, node, model_link, geometry, components, np.identity(4), np.identity(4), set()
        )

    def test_zero_volume_planar_mesh_is_accepted_when_qualified(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_geometry(Path(tmp), qualified=True, collinear=False)

    def test_zero_volume_planar_mesh_is_rejected_when_unqualified(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PipelineError) as caught:
                self._run_geometry(Path(tmp), qualified=False, collinear=False)
            self.assertIn("Zero-volume", str(caught.exception))

    def _binary_mesh(self, triangles):
        header = b"gate mesh".ljust(80, b" ")
        payload = b"".join(struct.pack("<12fH", 0.0, 0.0, 1.0, *a, *b, *c, 0) for a, b, c in triangles)
        return header + struct.pack("<I", len(triangles)) + payload

    def _sliver(self, x, y):
        return ((x, y, 0.0), (x + 1e-7, y, 0.0), (x, y + 1e-7, 0.0))

    def _tetra(self, offset):
        v0 = (offset, 0.0, 0.0)
        v1 = (offset + 0.001, 0.0, 0.0)
        v2 = (offset, 0.001, 0.0)
        v3 = (offset, 0.0, 0.001)
        return [(v0, v2, v1), (v0, v1, v3), (v1, v2, v3), (v2, v0, v3)]

    def test_area_based_gate_fails_a_mostly_degenerate_mesh(self):
        triangles = self._tetra(0.0) + [self._sliver(0.01 + index * 1e-6, 0.02) for index in range(1200)]
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(PipelineError) as caught:
            self._run_geometry(
                Path(tmp), qualified=False, collinear=False, raw_payload=self._binary_mesh(triangles)
            )
        message = str(caught.exception)
        self.assertIn("area ratio", message)
        self.assertIn("count ratio", message)
        self.assertIn("1.0e-06", message)
        self.assertIn("1.0e-02", message)

    def test_area_based_gate_passes_many_tiny_slivers_on_a_large_mesh(self):
        triangles = [face for index in range(750) for face in self._tetra(index * 0.002)]
        triangles += [self._sliver(2.0 + index * 1e-6, 3.0) for index in range(30)]
        with tempfile.TemporaryDirectory() as tmp:
            self._run_geometry(
                Path(tmp), qualified=False, collinear=False, raw_payload=self._binary_mesh(triangles)
            )

    def test_stl_degenerate_area_is_accumulated_for_binary_and_ascii(self):
        from description_pipeline.geometry import stl

        triangles = [
            ((0.0, 0.0, 0.0), (0.001, 0.0, 0.0), (0.0, 0.001, 0.0)),
            self._sliver(0.01, 0.01),
            self._sliver(0.02, 0.02),
        ]
        sliver = struct.unpack("<f", struct.pack("<f", 1e-7))[0]
        valid = struct.unpack("<f", struct.pack("<f", 1e-3))[0]
        binary = stl.read_bytes(self._binary_mesh(triangles))
        text = "solid gate\n" + "".join(
            "facet normal 0 0 1\n" + "".join(f"vertex {v[0]} {v[1]} {v[2]}\n" for v in triangle) + "endfacet\n"
            for triangle in triangles
        ) + "endsolid gate\n"
        ascii_stats = stl.read_bytes(text.encode("ascii"))
        self.assertEqual(binary.degenerate, 2)
        self.assertAlmostEqual(binary.degenerate_area, sliver * sliver, places=12)
        self.assertAlmostEqual(binary.area, 0.5 * valid * valid + sliver * sliver, places=12)
        self.assertEqual(ascii_stats.degenerate, 2)
        self.assertAlmostEqual(ascii_stats.degenerate_area, 1e-14, places=12)
        self.assertAlmostEqual(ascii_stats.area, 5e-7 + 1e-14, places=12)

    def test_degenerate_plane_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PipelineError) as caught:
                self._run_geometry(Path(tmp), qualified=True, collinear=True)
            self.assertIn("degenerate", str(caught.exception))

