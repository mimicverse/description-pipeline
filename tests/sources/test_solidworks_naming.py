"""CAD-derived deterministic naming and content-addressed mesh paths."""

from __future__ import annotations

import unittest

from description_pipeline.sources.solidworks.naming import (
    carrier_names,
    collision_name,
    deduplicate_names,
    fallback_name,
    fixed_joint_name,
    joint_name,
    link_name,
    material_name,
    mesh_path,
    neutral_link_name,
    robot_name,
    token,
    visual_name,
)


class NamingTests(unittest.TestCase):
    def test_links_and_joints_come_from_native_datum_and_axis_semantics(self):
        bodies = [
            {"name": "manual_root", "frame": {"coordinate_system": "c_base_link"}},
            {"name": "manual_left", "frame": {"coordinate_system": "c_l_ankle"}},
        ]
        joints = [
            {
                "name": "l_ankle",
                "axis_reference": {"feature_name": "a_l_ankle"},
            }
        ]
        body_names, joint_names = carrier_names(bodies, joints, "manual_root")
        self.assertEqual(body_names, {"manual_root": "base_link", "manual_left": "left_ankle_link"})
        self.assertEqual(joint_names, {"l_ankle": "left_ankle_joint"})

    def test_native_generic_names_are_formatted_once(self):
        self.assertEqual(token(" R_Shoulder Roll "), "right_shoulder_roll")
        self.assertEqual(token("LeftShoulderPitch"), "left_shoulder_pitch")
        self.assertEqual(link_name("r_shoulder_roll"), "right_shoulder_roll_link")
        self.assertEqual(link_name("CS_R_Shoulder_Roll"), "right_shoulder_roll_link")
        self.assertEqual(joint_name("r_shoulder_roll"), "right_shoulder_roll_joint")
        self.assertEqual(joint_name("a_r_shoulder_roll"), "right_shoulder_roll_joint")
        self.assertEqual(joint_name("LeftShoulderPitch"), "left_shoulder_pitch_joint")
        self.assertEqual(joint_name("左髋关节"), "left_hip_joint")
        self.assertEqual(
            fixed_joint_name("left_upper_arm_link", "left_hand_link"),
            "left_upper_arm_link_to_left_hand_link_joint",
        )
        self.assertEqual(robot_name("M3.0.3_20261016.SLDASM"), "m3_0_3_20261016")
        self.assertEqual(link_name("base_link", root=True), "base_link")

    def test_chinese_and_unknown_terms_use_semantic_neutral_rules(self):
        self.assertEqual(link_name("CS_左_上臂"), "left_upper_arm_link")
        self.assertEqual(joint_name("右_肩_roll"), "right_shoulder_roll_joint")
        self.assertEqual(neutral_link_name("unknown", root=True), "base_link")
        self.assertEqual(neutral_link_name("unknown"), "body_link")

    def test_duplicate_normalization_is_stable_under_reordering(self):
        first = deduplicate_names({"z_body": "body_link", "a_body": "body_link"}, "link")
        second = deduplicate_names({"a_body": "body_link", "z_body": "body_link"}, "link")
        self.assertEqual(first, second)
        self.assertEqual(first["a_body"], "body_link")
        self.assertTrue(first["z_body"].startswith("body_"))
        self.assertTrue(first["z_body"].endswith("_link"))

    def test_fixed_joint_uses_its_two_normalized_endpoints(self):
        bodies = [
            {"name": "base", "frame": {"coordinate_system": "CS_base_link"}},
            {"name": "tool", "frame": {"coordinate_system": "CS_tool"}},
        ]
        joints = [{"name": "tool_mount", "type": "fixed", "parent": "base", "child": "tool"}]
        _links, names = carrier_names(bodies, joints, "base")
        self.assertEqual(names["tool_mount"], "base_link_to_tool_link_joint")

    def test_real_root_reserves_base_link_before_collision_resolution(self):
        bodies = [
            {"name": "a", "frame": {"coordinate_system": "CS_base_link"}},
            {"name": "root", "frame": {"coordinate_system": "CS_base_link"}},
        ]
        names, _joints = carrier_names(bodies, [], "root")
        self.assertEqual(names["root"], "base_link")
        self.assertTrue(names["a"].startswith("body_"))
        self.assertTrue(names["a"].endswith("_link"))

    def test_normalization_is_idempotent(self):
        self.assertEqual(link_name(link_name("r_upper_arm")), link_name("r_upper_arm"))
        self.assertEqual(joint_name(joint_name("r_elbow")), joint_name("r_elbow"))

    def test_visual_identity_and_mesh_bytes_are_content_addressed(self):
        first = visual_name("waist-1/part-1", link="pelvis_link")
        self.assertEqual(first, visual_name("waist-1/part-1", link="pelvis_link"))
        self.assertNotEqual(first, visual_name("waist-1/part-2", link="pelvis_link"))
        self.assertTrue(first.endswith("_visual"))
        self.assertTrue(collision_name("waist-1/part-1", link="pelvis_link").endswith("_collision"))
        self.assertTrue(material_name("waist-1/part-1", link="pelvis_link").endswith("_material"))
        digest = "a" * 64
        self.assertEqual(
            mesh_path(digest, semantic=first),
            f"meshes/visual/{first}_{digest[:16]}.stl",
        )
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            mesh_path("not-a-digest")

    def test_empty_or_non_ascii_names_fail_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            token("***")
        self.assertEqual(fallback_name("link", "***"), fallback_name("link", "***"))
        self.assertRegex(fallback_name("joint", "***"), r"^joint_[0-9a-f]{16}_joint$")


if __name__ == "__main__":
    unittest.main()
