"""Portable Capture projection regression tests without a COM import."""

from __future__ import annotations

import copy
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from description_pipeline.sources.solidworks import capture_projection
from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.sources.solidworks.protocol import RawScene
from tests.sources import support


class CaptureProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="capture-projection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "prepared"
        assembly = support.make_cad_tree(self.root)
        parts = [self.root / "base.SLDPRT", self.root / "arm.SLDPRT"]
        self.backend = support.FixtureCadBackend(
            assembly,
            [
                {
                    "name": "base-1",
                    "path": str(parts[0]),
                    "transform": support.placement(),
                    "mass": support.mass_payload(
                        1.0,
                        (0.0, 0.0, 0.0),
                    ),
                },
                {
                    "name": "arm-1",
                    "path": str(parts[1]),
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(
                        0.5,
                        (0.0, 0.0, 0.05),
                    ),
                },
            ],
            dependencies=parts,
        )
        self.document = str(assembly)
        self.backend.prepare_prepared_capture(
            str(self.root),
            self.document,
            "Default",
        )

    def test_public_projections_reject_non_object_scene_with_typed_error(self) -> None:
        facts = self.backend.collect_prepared_scene(
            self.document, [], groups=[{"name": "body", "components": ["base-1", "arm-1"]}]
        )
        observation = capture_projection.build_native_capture_observation(
            facts=facts, geometry_raw={}, axis_references_raw=[], document_state={}, input_stability={}
        )
        for invalid in (None, [], "scene", True, 1, {}, {"transform_frame": "parent"}):
            for public in ("facts", "observation"):
                with self.subTest(scene=invalid, public=public):
                    record = copy.deepcopy(facts if public == "facts" else observation)
                    record["scene_raw"] = invalid
                    with self.assertRaises(CadError) as caught:
                        if public == "facts":
                            capture_projection.project_capture_facts(record)
                        else:
                            capture_projection.project_native_capture_observation(record, {})
                    self.assertEqual(caught.exception.code, "cad_prepared_scene_facts_invalid")

    def test_portable_projection_reconstructs_raw_scene_and_masses(self) -> None:
        raw_facts = self.backend.collect_prepared_scene(
            self.document,
            [],
            groups=[
                {"name": "base_link", "components": ["base-1"]},
                {"name": "arm_link", "components": ["arm-1"]},
            ],
        )
        self.backend.release()

        actual = capture_projection.project_capture_facts(
            copy.deepcopy(raw_facts)
        )

        self.assertIsInstance(actual["scene"], RawScene)
        self.assertEqual(
            {component.name for component in actual["scene"].components},
            {"base-1", "arm-1"},
        )
        self.assertEqual(
            set(actual["scene"].mass_properties),
            {"base-1", "arm-1"},
        )
        self.assertEqual(
            set(actual["link_group_mass_properties"]["links"]),
            {"base_link", "arm_link"},
        )

    def test_portable_axis_projection_summarizes_raw_geometry(
        self,
    ) -> None:
        raw = self.backend.capture_axis_reference(
            {
                "kind": "cylinder",
                "component": "arm-1",
                "body_type": "solid",
                "face_index": 0,
            }
        )
        self.backend.release()
        raw_rows = [{"joint": "hinge", **raw}]

        projected = capture_projection.project_axis_references(raw_rows)[0]
        self.assertEqual(projected["axis_point_m"], [0.0, 0.0, 0.1])
        self.assertEqual(projected["axis_direction"], [0.0, 0.0, 1.0])
        self.assertEqual(projected["radius_m"], 0.006)
        self.assertIs(projected["is_cylinder_raw"], True)

    def test_projection_module_imports_without_native_com_module(self) -> None:
        environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(Path.cwd() / "src"), str(Path.cwd())]
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        script = """
import sys
import description_pipeline.sources.solidworks.capture_projection
print(
    "description_pipeline.sources.solidworks.native" in sys.modules
)
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(result.stdout.strip(), "False")

    def test_positive_inertia_is_normalized_only_in_the_projection(self) -> None:
        tensor = [
            [2.0e-3, 2.0e-4, 1.0e-4],
            [2.0e-4, 3.0e-3, 5.0e-5],
            [1.0e-4, 5.0e-5, 4.0e-3],
        ]
        com = [0.05, -0.03, 0.02]
        mass = 1.0
        norm = math.fsum(value * value for value in com)
        origin = [
            [
                tensor[row][column]
                + mass
                * (
                    (norm - com[row] * com[column])
                    if row == column
                    else com[row] * com[column]
                )
                for column in range(3)
            ]
            for row in range(3)
        ]
        read = {
            "mass_kg": mass,
            "com_m": list(com),
            "inertia_tensor_about_com": copy.deepcopy(tensor),
            "inertia_values_about_origin_api": [
                origin[row][column]
                for row in range(3)
                for column in range(3)
            ],
            "overrides": {
                "OverrideMass": False,
                "OverrideCenterOfMass": False,
                "OverrideMomentsOfInertia": False,
            },
        }
        projected = {
            "scene": RawScene(
                document="robot.SLDASM",
                mass_properties={
                    "body": {
                        "mass": mass,
                        "com": list(com),
                        "inertia": copy.deepcopy(tensor),
                    }
                },
            ),
            "assembly_mass": None,
            "component_mass": None,
            "effective_instances": {},
            "link_group_mass_properties": {
                "links": {
                    "body_link": {
                        "selection": ["body"],
                        "reads": [
                            copy.deepcopy(read),
                            copy.deepcopy(read),
                        ],
                    }
                }
            },
        }

        decision = capture_projection.normalize_capture_projection(projected)

        self.assertEqual(decision["detected"], "solidworks_positive")
        self.assertTrue(decision["applied"])
        scene_tensor = projected["scene"].mass_properties["body"]["inertia"]
        link_tensor = projected["link_group_mass_properties"]["links"][
            "body_link"
        ]["reads"][0]["inertia_tensor_about_com"]
        for value in (scene_tensor, link_tensor):
            self.assertEqual(value[0][1], -tensor[0][1])
            self.assertEqual(value[0][2], -tensor[0][2])
            self.assertEqual(value[1][2], -tensor[1][2])


if __name__ == "__main__":
    unittest.main()
