"""Contract tests for the one engineering model and deterministic projection."""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, canonical, read_data
from description_pipeline.model import EngineeringModel, compile, emit
from description_pipeline.sources.solidworks.scene import load_scene
from description_pipeline.verification.solidworks_urdf import _xml

from tests.sources import test_solidworks_mass_authority as authority_fixtures


class EngineeringModelTests(unittest.TestCase):
    def test_compile_is_pure_and_location_independent(self) -> None:
        authority = authority_fixtures.CadMassAuthorityTests(methodName="runTest")
        authority.setUp()
        self.addCleanup(authority.tearDown)

        first = authority._freeze("engineering-model-a")
        authority_fixtures._compose_scene_inputs(first, authority.tmp / "cad")
        second = authority.tmp / "engineering-model-b"
        shutil.copytree(first, second)

        first_input = load_scene(first)
        second_input = load_scene(second)
        before_first = copy.deepcopy(first_input)
        before_second = copy.deepcopy(second_input)
        first_model = compile(first_input, native_warnings=[])
        second_model = compile(second_input, native_warnings=[])

        self.assertEqual(first_model.semantic_sha256, second_model.semantic_sha256)
        self.assertEqual(first_input, before_first)
        self.assertEqual(second_input, before_second)
        self.assertEqual(first_model.to_dict()["provenance"]["assembly"], "input/cad/robot.SLDASM")
        self.assertNotIn(str(first), canonical(first_model.to_dict()).decode())
        self.assertNotIn(str(second), canonical(second_model.to_dict()).decode())

    def test_one_model_field_mutation_is_rejected_by_independent_xml_gate(self) -> None:
        source = Path(__file__).resolve().parents[1] / "fixtures/v1/mesh-source"
        scene = read_data(source / "scene.json")
        scene["name"] = "fixture_arm"
        model = compile(scene)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            emission = emit(model, evidence_root=source, destination=root)
            self.assertEqual(emission.model_sha256, model.semantic_sha256)

            mutated = model.to_dict()
            mutated["name"] = "mutated_robot"
            mutated_model = EngineeringModel.from_dict(mutated)
            self.assertNotEqual(mutated_model.semantic_sha256, model.semantic_sha256)
            with self.assertRaisesRegex(PipelineError, "robot identity"):
                _xml(root, {"source": {"robot_name": "fixture_arm"}}, mutated_model.to_dict())


if __name__ == "__main__":
    unittest.main()
