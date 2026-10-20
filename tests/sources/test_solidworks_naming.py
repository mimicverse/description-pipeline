"""CAD-derived deterministic naming and content-addressed mesh paths."""

from __future__ import annotations

import unittest

from description_pipeline.sources.solidworks.naming import (
    carrier_names,
    collision_name,
    deduplicate_names,
    disambiguated_frame_name,
    fallback_name,
    frame_identity,
    fixed_joint_name,
    frame_name,
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
            {
                "name": "manual_root",
                "frame": {"coordinate_system": "c_base_link"},
                "components": ["root"],
            },
            {
                "name": "manual_left",
                "frame": {"coordinate_system": "c_l_ankle"},
                "components": ["left"],
            },
        ]
        joints = [
            {
                "name": "l_ankle",
                "type": "revolute",
                "parent": "manual_root",
                "child": "manual_left",
            }
        ]
        body_names, joint_names = carrier_names(bodies, joints, "manual_root")
        self.assertEqual(body_names, {"manual_root": "base_link", "manual_left": "left_foot_link"})
        self.assertEqual(joint_names, {"l_ankle": "left_ankle_joint"})

    def test_joint_centric_aliases_use_child_topology_but_physical_names_are_preserved(self):
        bodies = [
            {"name": "base", "frame": {"coordinate_system": "CS_base_link"}, "components": ["base"]},
            {"name": "upper", "frame": {"coordinate_system": "c_r_shoulder_roll"}, "components": ["upper"]},
            {"name": "forearm", "frame": {"coordinate_system": "c_r_elbow"}, "components": ["forearm"]},
            {"name": "hand", "frame": {"coordinate_system": "c_r_wrist_pitch"}, "components": ["hand"]},
            {"name": "sensor", "frame": {"coordinate_system": "CS_sensor_housing_link"}, "components": ["sensor"]},
        ]
        joints = [
            {"name": "r_shoulder_roll", "type": "revolute", "parent": "base", "child": "upper"},
            {"name": "r_shoulder_yaw", "type": "revolute", "parent": "upper", "child": "sensor"},
            {"name": "r_elbow", "type": "revolute", "parent": "base", "child": "forearm"},
            {"name": "r_wrist_pitch", "type": "revolute", "parent": "forearm", "child": "hand"},
        ]
        body_names, joint_names = carrier_names(bodies, joints, "base")
        self.assertEqual(body_names["upper"], "right_upper_arm_link")
        self.assertEqual(body_names["forearm"], "right_forearm_link")
        self.assertEqual(body_names["hand"], "right_hand_link")
        self.assertEqual(body_names["sensor"], "sensor_housing_link")
        self.assertEqual(joint_names["r_shoulder_roll"], "right_shoulder_roll_joint")
        self.assertEqual(joint_names["r_elbow"], "right_elbow_joint")
        self.assertEqual(joint_names["r_wrist_pitch"], "right_wrist_pitch_joint")

    def test_side_mismatch_is_not_unique_child_topology(self):
        bodies = [
            {"name": "base", "frame": {"coordinate_system": "c_base_link"}, "components": ["base"]},
            {"name": "upper", "frame": {"coordinate_system": "c_r_shoulder_roll"}, "components": ["upper"]},
            {"name": "yaw", "frame": {"coordinate_system": "c_l_shoulder_yaw"}, "components": ["yaw"]},
        ]
        joints = [
            {"name": "r_shoulder_roll", "type": "revolute", "parent": "base", "child": "upper"},
            {"name": "l_shoulder_yaw", "type": "revolute", "parent": "upper", "child": "yaw"},
        ]
        body_names, joint_names = carrier_names(bodies, joints, "base")
        self.assertEqual(body_names["upper"], "right_shoulder_roll_link")
        self.assertEqual(joint_names["r_shoulder_roll"], "right_shoulder_roll_joint")

    def test_missing_components_blocks_semantic_rename(self):
        bodies = [
            {"name": "base", "frame": {"coordinate_system": "c_base_link"}, "components": ["base"]},
            {"name": "forearm", "frame": {"coordinate_system": "c_r_elbow"}},
            {"name": "hand", "frame": {"coordinate_system": "c_r_wrist_pitch"}, "components": ["hand"]},
        ]
        joints = [
            {"name": "r_elbow", "type": "revolute", "parent": "base", "child": "forearm"},
            {"name": "r_wrist_pitch", "type": "revolute", "parent": "forearm", "child": "hand"},
        ]
        body_names, joint_names = carrier_names(bodies, joints, "base")
        self.assertEqual(body_names["forearm"], "right_elbow_link")
        self.assertEqual(joint_names["r_elbow"], "right_elbow_joint")

    def test_leg_waist_and_head_topology_produce_the_manual_segments(self):
        bodies = [
            {"name": "base", "frame": {"coordinate_system": "CS_base_link"}, "components": ["base"]},
            {"name": "waist", "frame": {"coordinate_system": "c_waist"}, "components": ["waist"]},
            {"name": "thigh_yaw", "frame": {"coordinate_system": "c_r_thigh_yaw"}, "components": ["thigh_yaw"]},
            {"name": "thigh_pitch", "frame": {"coordinate_system": "c_r_thigh_pitch"}, "components": ["thigh_pitch"]},
            {"name": "thigh_roll", "frame": {"coordinate_system": "c_r_thigh_roll"}, "components": ["thigh_roll"]},
            {"name": "knee", "frame": {"coordinate_system": "c_r_knee"}, "components": ["knee"]},
            {"name": "ankle", "frame": {"coordinate_system": "c_r_ankle"}, "components": ["ankle"]},
            {"name": "head_yaw", "frame": {"coordinate_system": "c_head_yaw"}, "components": ["head_yaw"]},
            {"name": "head_pitch", "frame": {"coordinate_system": "c_head_pitch"}, "components": ["head_pitch"]},
        ]
        joints = [
            {"name": "waist", "type": "revolute", "parent": "base", "child": "waist"},
            {"name": "r_thigh_yaw", "type": "revolute", "parent": "waist", "child": "thigh_yaw"},
            {"name": "r_thigh_pitch", "type": "revolute", "parent": "thigh_yaw", "child": "thigh_pitch"},
            {"name": "r_thigh_roll", "type": "revolute", "parent": "thigh_pitch", "child": "thigh_roll"},
            {"name": "r_knee", "type": "revolute", "parent": "thigh_roll", "child": "knee"},
            {"name": "r_ankle", "type": "revolute", "parent": "knee", "child": "ankle"},
            {"name": "head_yaw", "type": "revolute", "parent": "base", "child": "head_yaw"},
            {"name": "head_pitch", "type": "revolute", "parent": "head_yaw", "child": "head_pitch"},
        ]
        body_names, joint_names = carrier_names(bodies, joints, "base")
        self.assertEqual(
            body_names,
            {
                "base": "base_link",
                "waist": "pelvis_link",
                "thigh_yaw": "right_hip_yaw_link",
                "thigh_pitch": "right_thigh_link",
                "thigh_roll": "right_hip_roll_link",
                "knee": "right_shank_link",
                "ankle": "right_foot_link",
                "head_yaw": "neck_yaw_link",
                "head_pitch": "head_link",
            },
        )
        self.assertEqual(
            joint_names,
            {
                "waist": "waist_joint",
                "r_thigh_yaw": "right_hip_yaw_joint",
                "r_thigh_pitch": "right_hip_pitch_joint",
                "r_thigh_roll": "right_hip_roll_joint",
                "r_knee": "right_knee_joint",
                "r_ankle": "right_ankle_joint",
                "head_yaw": "neck_yaw_joint",
                "head_pitch": "neck_pitch_joint",
            },
        )

    def test_native_generic_names_are_formatted_once(self):
        self.assertEqual(token(" R_Shoulder Roll "), "right_shoulder_roll")
        self.assertEqual(token("LeftShoulderPitch"), "left_shoulder_pitch")
        self.assertEqual(link_name("r_shoulder_roll"), "right_shoulder_roll_link")
        self.assertEqual(link_name("CS_R_Shoulder_Roll"), "right_shoulder_roll_link")
        self.assertEqual(joint_name("r_shoulder_roll"), "right_shoulder_roll_joint")
        self.assertEqual(joint_name("a_r_shoulder_roll"), "right_shoulder_roll_joint")
        self.assertEqual(joint_name("LeftShoulderPitch"), "left_shoulder_pitch_joint")
        self.assertEqual(joint_name("左髋关节"), "left_hip_joint")
        self.assertEqual(frame_name("TCP_Tool"), "tool_tcp")
        self.assertEqual(frame_name("SCS_tool_flange"), "tool_flange_frame")
        self.assertEqual(frame_name("CS_arm_link_mount"), "arm_link_mount")
        self.assertEqual(frame_name("SCS_camera_optical"), "camera_optical")
        self.assertEqual(frame_name("tool_mount"), "tool_mount")
        self.assertEqual(frame_name("tool_tcp"), "tool_tcp")
        identity = frame_identity("arm-1", "TCP_Tool", "Default")
        self.assertRegex(disambiguated_frame_name("tool_tcp", identity), r"^tool_[0-9a-f]{16}_tcp$")
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
