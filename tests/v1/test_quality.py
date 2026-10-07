"""Independent analytic and adversarial checks; fixtures grant no native qualification."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import numpy as np

from description_pipeline.io import PipelineError, canonical, write_json
from description_pipeline.sources.solidworks.freeze import _component_context_record
from description_pipeline.verification import solidworks_urdf as quality
from description_pipeline.verification.solidworks_physics import verify_physics, verify_urdf_mass_equality


class PhysicsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        first = np.eye(4)
        first[:3, 3] = [0.1, 0.2, 0]
        second = np.array([[0, -1, 0, -0.2], [1, 0, 0, 0.1], [0, 0, 1, 0.3], [0, 0, 0, 1]], dtype=float)
        datum = np.array([[0, -1, 0, 0.05], [1, 0, 0, -0.03], [0, 0, 1, 0.1], [0, 0, 0, 1]], dtype=float)
        self.source = {
            "material_source": "cad",
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": ["a", "b"],
                    "frame": {"coordinate_system": "base_datum"},
                }
            ],
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
        reading = {
            "instances": [
                {
                    "name": name,
                    "parent": None,
                    "depth": 0,
                    "document_type": "part",
                    "context_mass_kg": value["mass"],
                    "context_volume_m3": 0.001,
                    "overrides": flags,
                }
                for name, value in self.masses.items()
            ],
            "errors": [],
        }
        scene = SimpleNamespace(
            components=[SimpleNamespace(name=name) for name in self.masses], mass_properties=self.masses
        )
        context = _component_context_record(reading, scene, 3.0)
        world = {"mass": 3.0, "com": com_world.tolist(), "inertia": tensor_world.tolist()}
        self.closure = {
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
        write_json(self.root / "raw/mass_closure.json", self.closure)

    def failures(self):
        self.write()
        checks = verify_physics(self.root, self.source, self.model)
        return [check["id"] for check in checks if not check["passed"]]

    def test_analytic_rotated_translated_parts_and_link_datum_match(self):
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
        quality._joint(node, authored, canonical_joint, {}, frames)
        node.find("axis").set("xyz", "0 0 -1")
        with self.assertRaises(PipelineError):
            quality._joint(node, authored, canonical_joint, {}, frames)

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
