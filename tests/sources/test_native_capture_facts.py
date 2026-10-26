"""Independent checks over sealed native capture facts."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.geometry.native_transform import transform_from_solidworks
from description_pipeline.io import PipelineError, write_json
from description_pipeline.schemas import EFFECTIVE_INSTANCES_SCHEMA
from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.verification.native_capture_facts import verify_native_capture_facts
from description_pipeline.verification.solidworks_physics import verify_urdf_mass_equality


class NativeCaptureFactsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-capture-facts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "evidence/raw").mkdir(parents=True)
        (self.root / "urdf").mkdir()
        (self.root / "urdf/robot.urdf").write_text(
            '<robot name="fixture"><link name="base"><inertial><mass value="2.0"/></inertial></link></robot>',
            encoding="utf-8",
        )
        self.identity = [
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.1,
            0.2,
            0.3,
            1.0,
            0.0,
            0.0,
            0.0,
        ]
        self.transform = list(transform_from_solidworks(self.identity))
        self.datum_transform = [
            sum(
                self.transform[4 * row + index] * self.transform[4 * index + column]
                for index in range(4)
            )
            for row in range(4)
            for column in range(4)
        ]
        self.about = [[1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 3.0]]
        self.origin = [1.08, -0.04, 0.0, -0.04, 2.02, 0.0, 0.0, 0.0, 3.10]
        self.overrides = {
            "OverrideMass": False,
            "OverrideCenterOfMass": False,
            "OverrideMomentsOfInertia": False,
        }
        self.read = {
            "mass_kg": 2.0,
            "com_m": [0.1, 0.2, 0.0],
            "inertia_values_raw": [1.0, 0.0, 0.0, 0.0, 2.0, 0.0, 0.0, 0.0, 3.0],
            "inertia_values_about_origin_api": self.origin,
            "overrides": self.overrides,
            "read_errors": {},
        }
        raw_mass = {
            "native_raw": {
                "mass_kg": 2.0,
                "center_of_mass_m": [0.1, 0.2, 0.0],
                "inertia_values": [1.0, 0.0, 0.0, 0.0, 2.0, 0.0, 0.0, 0.0, 3.0],
                "overrides": self.overrides,
            },
            "reference": {"scope": "part_document"},
            "read_errors": {},
        }
        source_scope = {
            "rule_version": "native-open-edge-scope-binding-v1",
            "component": "base",
            "source_document_path": "C:/source/base.SLDPRT",
            "source_copy_path": "source/base.SLDPRT",
            "source_document_sha256": "a" * 64,
            "source_copy_sha256": "a" * 64,
            "source_document_identity_sha256": "a" * 64,
            "source_document_identity_kind": "file_sha256",
            "referenced_configuration": "Default",
            "occurrence_scope": "base",
            "virtual_owner": None,
            "virtual_owner_sha256": None,
            "virtual_session_document_path": None,
            "virtual_session_temp_root": None,
            "virtual_native_api_pid": None,
        }
        raw_facts = {
            "scene_raw": {
                "document": "/source/root.SLDASM",
                "configuration": "Default",
                "occurrences": [
                    {
                        "name": "base",
                        "path": "/source/base.SLDPRT",
                        "transform_raw": self.identity,
                        "is_fixed": True,
                        "document_type": "part",
                    }
                ],
                "components": [
                    {
                        "name": "base",
                        "path": "/source/base.SLDPRT",
                        "transform_raw": self.identity,
                        "is_fixed": True,
                        "document_type": "part",
                    }
                ],
                "coordinate_systems": {
                    "base::Datum": {
                        "key": "base::Datum",
                        "name": "Datum",
                        "owner": "base",
                        "configuration": "Default",
                        "document": "/source/base.SLDPRT",
                        "array_data": self.identity,
                        "occurrence_transform_raw": self.identity,
                    }
                },
            },
            "part_mass_raw": {"base": raw_mass},
            "assembly_mass_raw": raw_mass,
            "component_mass_raw": {
                "assembly": "/source/root.SLDASM",
                "configuration": "Default",
                "instances": [
                    {
                        "name": "base",
                        "parent": None,
                        "depth": 0,
                        "document": "/source/base.SLDPRT",
                        "document_type": "part",
                        "context_mass_kg": 2.0,
                        "context_volume_m3": 1.0,
                        "overrides": self.overrides,
                        "read_errors": {},
                    }
                ],
                "errors": [],
                "reference": {},
            },
            "link_group_mass_raw": {
                "assembly": "/source/root.SLDASM",
                "configuration": "Default",
                "used_api": "fixture",
                "selection_rule": "fixture",
                "links": {
                    "base": {"selection": ["base"], "reads": [self.read, self.read]}
                },
            },
            "source_scopes": {"base": source_scope},
            "notes": {},
        }
        projected_mass = {
            "mass": 2.0,
            "com": [0.1, 0.2, 0.0],
            "inertia": self.about,
            "reference": {"scope": "part_document"},
        }
        projected_context = {
            "status": "recorded",
            "assembly_mass_kg": 2.0,
            "instances": [
                {
                    "name": "base",
                    "parent": None,
                    "depth": 0,
                    "document": "/source/base.SLDPRT",
                    "document_type": "part",
                    "context_mass_kg": 2.0,
                    "context_volume_m3": 1.0,
                    "overrides": self.overrides,
                    "effective": None,
                }
            ],
            "errors": [],
            "reference": {},
        }
        projected_link_read = {
            "mass_kg": 2.0,
            "com_m": [0.1, 0.2, 0.0],
            "inertia_tensor_about_com": self.about,
            "inertia_values_about_origin_api": self.origin,
            "overrides": self.overrides,
        }
        self.raw_facts = raw_facts
        self.capture = {
            "scene": {
                "document": "/source/root.SLDASM",
                "components": [
                    {
                        "name": "base",
                        "path": "/source/base.SLDPRT",
                        "transform": self.transform,
                        "is_fixed": True,
                        "document_type": "part",
                    }
                ],
                "coordinate_systems": {"base::Datum": self.datum_transform},
                "notes": {"effective_instances": {}},
            },
            "components": [{"name": "base", "mass_properties": projected_mass}],
            "datums": [],
            "mass_closure": {
                "status": "recorded",
                "mode": "full",
                "top_level": {
                    "mass": 2.0,
                    "com": [0.1, 0.2, 0.0],
                    "inertia": self.about,
                    "reference": {"scope": "assembly_document"},
                },
                "component_context": projected_context,
            },
            "effective_instances": {
                "schema_version": EFFECTIVE_INSTANCES_SCHEMA,
                "assembly": "/source/root.SLDASM",
                "configuration": "Default",
                "readings": {},
                "warnings": [],
            },
            "link_group_mass_properties": {
                "assembly": "/source/root.SLDASM",
                "configuration": "Default",
                "used_api": "fixture",
                "selection_rule": "fixture",
                "links": {
                    "base": {
                        "selection": ["base"],
                        "reads": [projected_link_read, dict(projected_link_read)],
                        "reproducibility": {
                            "mass_delta_kg": 0.0,
                            "com_delta_norm_m": 0.0,
                            "tensor_delta_max_abs": 0.0,
                        },
                    }
                },
            },
        }
        self.write_capture()

    def write_capture(
        self,
        *,
        detected: str = "solidworks_standard",
        applied: bool = False,
    ):
        write_json(
            self.root / "evidence/raw/native_capture_facts.json",
            self.raw_facts,
        )
        write_json(
            self.root / "evidence/raw/scene_raw.json",
            self.raw_facts["scene_raw"],
        )
        write_json(self.root / "evidence/capture.json", self.capture)
        write_json(
            self.root / "evidence/raw/inertia_convention.json",
            {
                "schema_version": "solidworks-to-urdf.inertia-convention/v1",
                "detected": detected,
                "capture_transform": {"applied": applied},
            },
        )
        write_manifest(
            self.root / "evidence",
            kind="solidworks",
            identity={
                "assembly": "C:/source/root.SLDASM",
                "configuration": "Default",
            },
            evidence_class="fixture",
        )

    def test_projected_values_are_bound_to_raw_facts(self):
        capture = json.loads(
            (self.root / "evidence/capture.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("native_capture_facts", capture)
        self.assertTrue(
            (self.root / "evidence/raw/native_capture_facts.json").is_file()
        )
        result = verify_native_capture_facts(self.root)
        self.assertTrue(result["native_capture_facts"])
        self.assertEqual(result["inertia_family"], "solidworks_standard")
        self.assertEqual(verify_urdf_mass_equality(self.root)["urdf_mass_kg"], 2.0)

    def test_projected_mass_tamper_fails_with_raw_facts_unchanged(self):
        self.capture["components"][0]["mass_properties"]["mass"] = 2.01
        self.write_capture()
        with self.assertRaisesRegex(PipelineError, "projected mass differs"):
            verify_native_capture_facts(self.root)

    def test_projected_transform_tamper_fails_with_raw_facts_unchanged(self):
        self.capture["scene"]["components"][0]["transform"][3] = 0.25
        self.write_capture()
        with self.assertRaisesRegex(PipelineError, "transform differs"):
            verify_native_capture_facts(self.root)

    def test_projected_datum_tamper_fails_with_raw_facts_unchanged(self):
        self.capture["scene"]["coordinate_systems"]["base::Datum"][3] = 0.25
        self.write_capture()
        with self.assertRaisesRegex(PipelineError, "datum matrix differs"):
            verify_native_capture_facts(self.root)

    def test_each_projected_mass_surface_rejects_small_tampering(self):
        def inertia(capture):
            capture["components"][0]["mass_properties"]["inertia"][0][1] = 1e-13

        def com(capture):
            capture["components"][0]["mass_properties"]["com"][0] += 1e-13

        def assembly(capture):
            capture["mass_closure"]["top_level"]["mass"] += 1e-13

        def link(capture):
            capture["link_group_mass_properties"]["links"]["base"]["reads"][0][
                "mass_kg"
            ] += 1e-13

        base_capture = copy.deepcopy(self.capture)
        for mutate, message in (
            (inertia, "projected inertia differs"),
            (com, "projected COM differs"),
            (assembly, "assembly: projected mass differs"),
            (link, "link-group base read 0: projected mass differs"),
        ):
            with self.subTest(message=message):
                capture = copy.deepcopy(base_capture)
                mutate(capture)
                self.capture = capture
                self.write_capture()
                with self.assertRaisesRegex(PipelineError, message):
                    verify_native_capture_facts(self.root)

    def test_positive_products_are_normalized_from_raw_facts(self):
        capture = copy.deepcopy(self.capture)
        raw_facts = copy.deepcopy(self.raw_facts)
        positive_about = [1.0, 0.04, 0.0, 0.04, 2.0, 0.0, 0.0, 0.0, 3.0]
        positive_origin = [1.08, 0.08, 0.0, 0.08, 2.02, 0.0, 0.0, 0.0, 3.10]
        normalized_about = [[1.0, -0.04, 0.0], [-0.04, 2.0, 0.0], [0.0, 0.0, 3.0]]
        normalized_origin = [1.08, -0.08, 0.0, -0.08, 2.02, 0.0, 0.0, 0.0, 3.10]
        for raw in (
            raw_facts["part_mass_raw"]["base"],
            raw_facts["assembly_mass_raw"],
        ):
            raw["native_raw"]["inertia_values"] = positive_about
        for read in raw_facts["link_group_mass_raw"]["links"]["base"]["reads"]:
            read["inertia_values_raw"] = positive_about
            read["inertia_values_about_origin_api"] = positive_origin
        capture["components"][0]["mass_properties"]["inertia"] = normalized_about
        capture["mass_closure"]["top_level"]["inertia"] = normalized_about
        for read in capture["link_group_mass_properties"]["links"]["base"]["reads"]:
            read["inertia_tensor_about_com"] = normalized_about
            read["inertia_values_about_origin_api"] = normalized_origin
        self.capture = capture
        self.raw_facts = raw_facts
        self.write_capture(detected="solidworks_positive", applied=True)
        result = verify_native_capture_facts(self.root)
        self.assertTrue(result["inertia_transform_applied"])

    def test_effective_projected_tamper_fails_with_raw_facts_unchanged(self):
        capture = copy.deepcopy(self.capture)
        raw_facts = copy.deepcopy(self.raw_facts)
        raw = raw_facts["component_mass_raw"]["instances"][0]
        raw["effective"] = {
            "native_raw": {
                "mass_kg": 1.0,
                "center_of_mass_m": [0.01, 0.0, 0.0],
                "inertia_values": [0.1, 0.0, 0.0, 0.0, 0.2, 0.0, 0.0, 0.0, 0.3],
                "overrides": self.overrides,
            },
            "reference": {"scope": "assembly_component_group"},
            "read_errors": {},
        }
        projected_effective = {
            "mass": 1.0,
            "com": [0.01, 0.0, 0.0],
            "inertia": [[0.1, 0.0, 0.0], [0.0, 0.2, 0.0], [0.0, 0.0, 0.3]],
        }
        capture["mass_closure"]["component_context"]["instances"][0]["effective"] = (
            projected_effective
        )
        capture["effective_instances"]["readings"] = {"base": projected_effective}
        capture["scene"]["notes"]["effective_instances"] = {"base": projected_effective}
        self.capture = capture
        self.raw_facts = raw_facts
        self.write_capture()
        verify_native_capture_facts(self.root)
        projected_effective["inertia"][0][1] = 1e-13
        self.write_capture()
        with self.assertRaisesRegex(
            PipelineError, "effective instance base: projected inertia differs"
        ):
            verify_native_capture_facts(self.root)

    def test_near_symmetric_raw_inertia_preserves_each_offdiagonal(self):
        capture = copy.deepcopy(self.capture)
        raw_facts = copy.deepcopy(self.raw_facts)
        raw_facts["assembly_mass_raw"] = copy.deepcopy(
            raw_facts["assembly_mass_raw"]
        )
        raw = raw_facts["part_mass_raw"]["base"]["native_raw"]
        raw["inertia_values"] = [1.0, 1e-13, 0.0, 2e-13, 2.0, 0.0, 0.0, 0.0, 3.0]
        capture["components"][0]["mass_properties"]["inertia"] = [
            [1.0, 1e-13, 0.0],
            [2e-13, 2.0, 0.0],
            [0.0, 0.0, 3.0],
        ]
        self.capture = capture
        self.raw_facts = raw_facts
        self.write_capture()
        verify_native_capture_facts(self.root)
        capture["components"][0]["mass_properties"]["inertia"][1][0] = 1e-13
        self.write_capture()
        with self.assertRaisesRegex(PipelineError, "projected inertia differs"):
            verify_native_capture_facts(self.root)

    def test_missing_sealed_native_facts_member_is_rejected(self):
        (self.root / "evidence/raw/native_capture_facts.json").unlink()
        write_manifest(
            self.root / "evidence",
            kind="solidworks",
            identity={
                "assembly": "C:/source/root.SLDASM",
                "configuration": "Default",
            },
            evidence_class="fixture",
        )
        with self.assertRaisesRegex(
            PipelineError,
            "no sealed native capture facts member",
        ):
            verify_native_capture_facts(self.root)

    def test_raw_member_tamper_breaks_the_source_manifest(self):
        path = self.root / "evidence/raw/native_capture_facts.json"
        record = self.raw_facts
        record["assembly_mass_raw"]["native_raw"]["mass_kg"] = 2.01
        write_json(path, record)
        with self.assertRaisesRegex(
            PipelineError,
            "Source snapshot is incomplete or changed",
        ):
            verify_native_capture_facts(self.root)


if __name__ == "__main__":
    unittest.main()
