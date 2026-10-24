"""Mutation tests for strict actual URDF definitions, references and materials."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.io import PipelineError
from description_pipeline.verification import actual_urdf
from description_pipeline.verification import solidworks_urdf as quality


DEFINITION = {"source": {"robot_name": "actual_urdf_fixture"}}


def model() -> dict:
    return {
        "schema_version": "solidworks-to-urdf.scene/v1",
        "name": "actual_urdf_fixture",
        "units": "SI",
        "links": [
            {
                "id": "base",
                "name": "base_link",
                "inertial": None,
                "visuals": [
                    {
                        "name": "base_link_visual",
                        "kind": "box",
                        "xyz": [0.0, 0.0, 0.0],
                        "rpy": [0.0, 0.0, 0.0],
                        "size": [1.0, 1.0, 1.0],
                        "material": "display_fallback_gray",
                        "rgba": [0.7, 0.7, 0.7, 1.0],
                        "provenance": {},
                    }
                ],
                "collisions": [],
                "provenance": {},
            },
            {
                "id": "tool",
                "name": "tool_link",
                "inertial": None,
                "visuals": [],
                "collisions": [],
                "provenance": {},
            },
        ],
        "joints": [
            {
                "id": "hinge",
                "name": "hinge_joint",
                "type": "revolute",
                "parent": "base_link",
                "child": "tool_link",
                "xyz": [0.0, 0.0, 0.1],
                "rpy": [0.0, 0.0, 0.0],
                "axis": [0.0, 0.0, 1.0],
                "limits": {"lower": -1.0, "upper": 1.0, "effort": 2.0, "velocity": 3.0},
                "provenance": {},
            }
        ],
        "frames": [],
        "provenance": {"value_warnings": []},
    }


def urdf() -> str:
    return """<?xml version="1.0" encoding="utf-8"?>
<robot name="actual_urdf_fixture">
  <link name="base_link">
    <visual name="base_link_visual">
      <origin xyz="0 0 0" rpy="0 0 0" />
      <geometry><box size="1 1 1" /></geometry>
      <material name="display_fallback_gray">
        <color rgba="0.7 0.7 0.7 1.0" />
      </material>
    </visual>
  </link>
  <link name="tool_link" />
  <joint name="hinge_joint" type="revolute">
    <parent link="base_link" />
    <child link="tool_link" />
    <origin xyz="0 0 0.1" rpy="0 0 0" />
    <axis xyz="0 0 1" />
    <limit lower="-1" upper="1" effort="2" velocity="3" />
  </joint>
</robot>
"""


class ActualUrdfBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "urdf").mkdir()

    def write(self, payload: str) -> None:
        (self.root / "urdf/robot.urdf").write_text(payload, encoding="utf-8")

    def validate(self, payload: str | None = None, candidate: dict | None = None) -> dict:
        self.write(urdf() if payload is None else payload)
        _document, details = actual_urdf.read_and_validate_actual_urdf(
            self.root,
            DEFINITION,
            model() if candidate is None else candidate,
        )
        return details

    def assert_rejected(self, payload: str, message: str, candidate: dict | None = None) -> None:
        with self.assertRaisesRegex(PipelineError, message):
            self.validate(payload, candidate)

    def test_valid_inline_material_is_bound_to_its_visual(self):
        details = self.validate()
        self.assertEqual(details["links"], 2)
        self.assertEqual(details["joints"], 1)
        self.assertEqual(details["visuals"], 1)
        self.assertRegex(details["xml_sha256"], r"^[0-9a-f]{64}$")

    def test_xml_gate_uses_strict_actual_binding(self):
        self.write(urdf().replace(
            """      <material name="display_fallback_gray">
        <color rgba="0.7 0.7 0.7 1.0" />
      </material>
""",
            "",
        ))
        with self.assertRaisesRegex(PipelineError, "lacks its canonical material"):
            quality._xml(self.root, DEFINITION, model())

    def test_visual_name_must_match_the_model_and_snake_case(self):
        payload = urdf().replace('name="base_link_visual"', 'name="BAD-VISUAL"', 1)
        self.assert_rejected(payload, "not snake_case")

    def test_duplicate_cross_object_visual_binding_is_rejected(self):
        candidate = model()
        candidate["links"][1]["visuals"] = [
            {
                "name": "tool_link_visual",
                "kind": "box",
                "xyz": [0.0, 0.0, 0.0],
                "rpy": [0.0, 0.0, 0.0],
                "size": [1.0, 1.0, 1.0],
                "material": "display_fallback_gray",
                "rgba": [0.7, 0.7, 0.7, 1.0],
                "provenance": {},
            }
        ]
        payload = urdf().replace('<link name="tool_link" />', """<link name="tool_link">
    <visual name="base_link_visual">
      <origin xyz="0 0 0" rpy="0 0 0" />
      <geometry><box size="1 1 1" /></geometry>
      <material name="display_fallback_gray"><color rgba="0.7 0.7 0.7 1.0" /></material>
    </visual>
  </link>""")
        self.assert_rejected(payload, "has no canonical model definition", candidate)

    def test_duplicate_canonical_visual_names_are_rejected(self):
        candidate = model()
        candidate["links"][1]["visuals"] = [
            {
                "name": "base_link_visual",
                "kind": "box",
                "xyz": [0.0, 0.0, 0.0],
                "rpy": [0.0, 0.0, 0.0],
                "size": [1.0, 1.0, 1.0],
                "material": "display_fallback_gray",
                "rgba": [0.7, 0.7, 0.7, 1.0],
                "provenance": {},
            }
        ]
        self.assert_rejected(urdf(), "repeat a name across links", candidate)

    def test_missing_material_is_rejected(self):
        payload = urdf().replace(
            """      <material name="display_fallback_gray">
        <color rgba="0.7 0.7 0.7 1.0" />
      </material>
""",
            "",
        )
        self.assert_rejected(payload, "lacks its canonical material")

    def test_wrong_material_name_is_rejected(self):
        self.assert_rejected(
            urdf().replace('name="display_fallback_gray"', 'name="alternate_finish"', 1),
            "material name differs",
        )

    def test_wrong_material_color_is_rejected(self):
        self.assert_rejected(
            urdf().replace('rgba="0.7 0.7 0.7 1.0"', 'rgba="1 0 0 1"', 1),
            "material.rgba differs",
        )

    def test_material_color_outside_the_urdf_range_is_rejected(self):
        candidate = model()
        candidate["links"][0]["visuals"][0]["rgba"] = [1.2, 0.7, 0.7, 1.0]
        self.assert_rejected(
            urdf().replace('rgba="0.7 0.7 0.7 1.0"', 'rgba="1.2 0.7 0.7 1.0"', 1),
            r"outside \[0, 1\]",
            candidate,
        )

    def test_texture_is_rejected_until_its_provenance_is_qualified(self):
        payload = urdf().replace(
            '<color rgba="0.7 0.7 0.7 1.0" />',
            '<texture filename="../meshes/texture.png" />',
            1,
        )
        self.assert_rejected(payload, "texture is not qualified")

    def test_material_must_bind_to_the_expected_visual(self):
        candidate = model()
        second = copy.deepcopy(candidate["links"][0]["visuals"][0])
        second.update(
            {
                "name": "base_link_second_visual",
                "material": "alternate_finish",
                "rgba": [0.1, 0.2, 0.3, 1.0],
            }
        )
        candidate["links"][0]["visuals"].append(second)
        payload = urdf().replace(
            "</visual>\n  </link>",
            """</visual>
    <visual name="base_link_second_visual">
      <origin xyz="0 0 0" rpy="0 0 0" />
      <geometry><box size="1 1 1" /></geometry>
      <material name="display_fallback_gray"><color rgba="0.7 0.7 0.7 1.0" /></material>
    </visual>
  </link>""",
            1,
        )
        self.assert_rejected(payload, "material name differs", candidate)

    def test_a_legal_alternative_label_is_accepted_when_model_and_xml_agree(self):
        candidate = model()
        candidate["links"][0]["visuals"][0]["material"] = "alternate_finish"
        payload = urdf().replace('name="display_fallback_gray"', 'name="alternate_finish"', 1)
        details = self.validate(payload, candidate)
        self.assertEqual(details["materials"], 1)

    def test_dangling_joint_reference_is_rejected(self):
        self.assert_rejected(
            urdf().replace('<child link="tool_link" />', '<child link="missing_link" />'),
            "references an undefined link",
        )

    def test_global_material_definition_fails_closed(self):
        payload = urdf().replace(
            '<robot name="actual_urdf_fixture">',
            """<robot name="actual_urdf_fixture">
  <material name="orphan_material"><color rgba="1 0 0 1" /></material>""",
        )
        self.assert_rejected(payload, "Unknown top-level URDF definition")

    def test_urdf_is_reread_after_binding(self):
        self.write(urdf())
        original = actual_urdf._validate_document

        def mutate_after_validation(document, root, definition, candidate):
            details = original(document, root, definition, candidate)
            path = root / "urdf/robot.urdf"
            path.write_bytes(path.read_bytes() + b"\n")
            return details

        with (
            mock.patch.object(actual_urdf, "_validate_document", side_effect=mutate_after_validation),
            self.assertRaisesRegex(PipelineError, "changed while actual definitions were binding"),
        ):
            actual_urdf.read_and_validate_actual_urdf(self.root, DEFINITION, model())


if __name__ == "__main__":
    unittest.main()
