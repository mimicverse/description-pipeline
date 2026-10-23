"""CAD-derived deterministic naming and content-addressed mesh paths."""

from __future__ import annotations

import unittest

from description_pipeline.sources.solidworks.naming import (
    asset_namespace,
    carrier_names_and_rules,
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
    role_link_name,
    token,
    visual_name,
)

def _carrier_names(bodies, joints, root: str) -> tuple[dict[str, str], dict[str, str]]:
    body_names, joint_names, _body_rules, _joint_rules = carrier_names_and_rules(
        bodies, joints, root
    )
    return body_names, joint_names


class NamingTests(unittest.TestCase):
    def test_functional_pitch_names_require_topology_and_parallel_cad_axes(self):
        joints = [
            {"name": "waist", "type": "revolute", "parent": "base", "child": "pelvis",
             "axis": {"assembly_direction": [1.0, 0.0, 0.0]}},
            {"name": "r_thigh_yaw", "type": "revolute", "parent": "pelvis", "child": "r_thigh_yaw_body"},
            {"name": "l_thigh_yaw", "type": "revolute", "parent": "pelvis", "child": "l_thigh_yaw_body"},
            {"name": "r_thigh_pitch", "type": "revolute", "parent": "r_thigh_yaw_body", "child": "r_thigh",
             "axis": {"assembly_direction": [1.0, 0.0, 0.0]}},
            {"name": "l_thigh_pitch", "type": "revolute", "parent": "l_thigh_yaw_body", "child": "l_thigh",
             "axis": {"assembly_direction": [-1.0, 0.0, 0.0]}},
            {"name": "r_thigh_roll", "type": "revolute", "parent": "r_thigh", "child": "r_thigh_roll_body"},
            {"name": "l_thigh_roll", "type": "revolute", "parent": "l_thigh", "child": "l_thigh_roll_body"},
            {"name": "r_knee", "type": "revolute", "parent": "r_thigh_roll_body", "child": "r_shank",
             "axis": {"assembly_direction": [1.0, 0.0, 0.0]}},
            {"name": "l_knee", "type": "revolute", "parent": "l_thigh_roll_body", "child": "l_shank",
             "axis": {"assembly_direction": [-1.0, 0.0, 0.0]}},
            {"name": "r_ankle", "type": "revolute", "parent": "r_shank", "child": "r_foot",
             "axis": {"assembly_direction": [-1.0, 0.0, 0.0]}},
            {"name": "l_ankle", "type": "revolute", "parent": "l_shank", "child": "l_foot",
             "axis": {"assembly_direction": [1.0, 0.0, 0.0]}},
            {"name": "r_elbow", "type": "revolute", "parent": "r_shoulder", "child": "r_forearm",
             "axis": {"assembly_direction": [0.0, 1.0, 0.0]}},
            {"name": "l_elbow", "type": "revolute", "parent": "l_shoulder", "child": "l_forearm",
             "axis": {"assembly_direction": [0.0, -1.0, 0.0]}},
            {"name": "r_wrist_pitch", "type": "revolute", "parent": "r_forearm", "child": "r_hand",
             "axis": {"assembly_direction": [0.0, 1.0, 0.0]}},
            {"name": "l_wrist_pitch", "type": "revolute", "parent": "l_forearm", "child": "l_hand",
             "axis": {"assembly_direction": [0.0, -1.0, 0.0]}},
        ]
        bodies = [
            {"name": name}
            for name in {
                "base",
                *(str(value) for joint in joints for value in (joint["parent"], joint["child"])),
            }
        ]
        _bodies, names, _body_rules, rules = carrier_names_and_rules(bodies, joints, "base")
        self.assertEqual(names["waist"], "waist_pitch_joint")
        self.assertEqual(names["r_knee"], "right_knee_pitch_joint")
        self.assertEqual(names["r_ankle"], "right_ankle_pitch_joint")
        self.assertEqual(names["l_elbow"], "left_elbow_pitch_joint")
        self.assertEqual(rules["r_ankle"]["rule_id"], "naming.functional_motion")
        next(joint for joint in joints if joint["name"] == "r_ankle")["axis"]["assembly_direction"] = [
            0.0,
            1.0,
            0.0,
        ]
        _bodies, names, _body_rules, _rules = carrier_names_and_rules(bodies, joints, "base")
        self.assertEqual(names["r_ankle"], "right_ankle_joint")

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
        body_names, joint_names = _carrier_names(bodies, joints, "manual_root")
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
        body_names, joint_names = _carrier_names(bodies, joints, "base")
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
        body_names, joint_names = _carrier_names(bodies, joints, "base")
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
        body_names, joint_names = _carrier_names(bodies, joints, "base")
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
        body_names, joint_names = _carrier_names(bodies, joints, "base")
        self.assertEqual(
            body_names,
            {
                "base": "base_link",
                "waist": "waist_link",
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

    def test_waist_requires_one_thigh_yaw_per_side(self):
        def names(outgoing):
            bodies = [
                {"name": "base", "frame": {"coordinate_system": "CS_base_link"}, "components": ["base"]},
                {"name": "waist", "frame": {"coordinate_system": "c_waist"}, "components": ["waist"]},
                *[
                    {
                        "name": f"leg{index}",
                        "frame": {"coordinate_system": f"c_{side}thigh_yaw"},
                        "components": [f"leg{index}"],
                    }
                    for index, side in enumerate(outgoing)
                ],
            ]
            joints = [
                {"name": "waist", "type": "revolute", "parent": "base", "child": "waist"},
                *[
                    {
                        "name": f"{side}thigh_yaw",
                        "type": "revolute",
                        "parent": "waist",
                        "child": f"leg{index}",
                    }
                    for index, side in enumerate(outgoing)
                ],
            ]
            return _carrier_names(bodies, joints, "base")[0]["waist"]

        self.assertEqual(names(["r_", "l_"]), "pelvis_link")
        self.assertEqual(names(["r_"]), "waist_link")
        self.assertEqual(names(["r_", "r_"]), "waist_link")

    def test_elbow_child_topology_must_share_the_joint_side(self):
        bodies = [
            {"name": "base", "frame": {"coordinate_system": "c_base_link"}, "components": ["base"]},
            {"name": "forearm", "frame": {"coordinate_system": "c_r_elbow"}, "components": ["forearm"]},
            {"name": "hand", "frame": {"coordinate_system": "c_l_wrist_pitch"}, "components": ["hand"]},
        ]
        joints = [
            {"name": "r_elbow", "type": "revolute", "parent": "base", "child": "forearm"},
            {"name": "l_wrist_pitch", "type": "revolute", "parent": "forearm", "child": "hand"},
        ]
        body_names, joint_names = _carrier_names(bodies, joints, "base")
        self.assertEqual(body_names["forearm"], "right_elbow_link")
        self.assertEqual(joint_names["r_elbow"], "right_elbow_joint")

    def test_unknown_topology_and_qualified_names_never_prove_a_role(self):
        self.assertIsNone(role_link_name("r_ankle", [None]))
        self.assertEqual(role_link_name("r_ankle", []), "right_foot_link")
        self.assertIsNone(role_link_name("r_sensor_elbow", [("right_", "wrist_pitch")]))
        self.assertEqual(
            role_link_name("r_elbow", [("right_", "wrist_pitch")]),
            "right_forearm_link",
        )

    def test_full_humanoid_topology_role_names_and_unresolved_neutral_names(self):
        bodies = [{"name": "base", "frame": {"coordinate_system": "CS_base_link"}, "components": ["base"]}]
        joints = []

        def add_body(native, datum=None):
            bodies.append(
                {
                    "name": native,
                    "frame": {"coordinate_system": datum or f"c_{native}"},
                    "components": [native],
                }
            )

        def add_joint(native, parent, child):
            joints.append({"name": native, "type": "revolute", "parent": parent, "child": child})

        add_body("waist")
        add_joint("waist", "base", "waist")
        for side in ("r", "l"):
            add_body(f"{side}_shoulder_pitch")
            add_joint(f"{side}_shoulder_pitch", "base", f"{side}_shoulder_pitch")
            for native, parent in (
                ("shoulder_roll", "shoulder_pitch"),
                ("shoulder_yaw", "shoulder_roll"),
                ("elbow", "shoulder_yaw"),
                ("wrist_pitch", "elbow"),
            ):
                add_body(f"{side}_{native}")
                add_joint(f"{side}_{native}", f"{side}_{parent}", f"{side}_{native}")
            add_body(f"{side}_thigh_yaw")
            add_joint(f"{side}_thigh_yaw", "waist", f"{side}_thigh_yaw")
            for native, parent in (
                ("thigh_pitch", "thigh_yaw"),
                ("thigh_roll", "thigh_pitch"),
                ("knee", "thigh_roll"),
                ("ankle", "knee"),
            ):
                add_body(f"{side}_{native}")
                add_joint(f"{side}_{native}", f"{side}_{parent}", f"{side}_{native}")
        add_body("head_yaw")
        add_joint("head_yaw", "base", "head_yaw")
        add_body("head_pitch")
        add_joint("head_pitch", "head_yaw", "head_pitch")
        add_body("sensor", "CS_sensor_housing_link")
        joints.append({"name": "sensor_mount", "type": "fixed", "parent": "base", "child": "sensor"})

        body_names, joint_names = _carrier_names(bodies, joints, "base")

        self.assertEqual(
            set(body_names.values()),
            {
                "base_link",
                "pelvis_link",
                "neck_yaw_link",
                "head_link",
                "sensor_housing_link",
                "right_shoulder_pitch_link",
                "right_upper_arm_link",
                "right_shoulder_yaw_link",
                "right_forearm_link",
                "right_hand_link",
                "left_shoulder_pitch_link",
                "left_upper_arm_link",
                "left_shoulder_yaw_link",
                "left_forearm_link",
                "left_hand_link",
                "right_hip_yaw_link",
                "right_thigh_link",
                "right_hip_roll_link",
                "right_shank_link",
                "right_foot_link",
                "left_hip_yaw_link",
                "left_thigh_link",
                "left_hip_roll_link",
                "left_shank_link",
                "left_foot_link",
            },
        )
        self.assertEqual(
            set(joint_names.values()),
            {
                "waist_joint",
                "neck_yaw_joint",
                "neck_pitch_joint",
                "right_shoulder_pitch_joint",
                "right_shoulder_roll_joint",
                "right_shoulder_yaw_joint",
                "right_elbow_joint",
                "right_wrist_pitch_joint",
                "left_shoulder_pitch_joint",
                "left_shoulder_roll_joint",
                "left_shoulder_yaw_joint",
                "left_elbow_joint",
                "left_wrist_pitch_joint",
                "right_hip_yaw_joint",
                "right_hip_pitch_joint",
                "right_hip_roll_joint",
                "right_knee_joint",
                "right_ankle_joint",
                "left_hip_yaw_joint",
                "left_hip_pitch_joint",
                "left_hip_roll_joint",
                "left_knee_joint",
                "left_ankle_joint",
                "base_link_to_sensor_housing_link_joint",
            },
        )
        # The four role families below have no proven motion token in the
        # current CAD capture.  They stay neutral and are reported as an
        # unresolved reference/standard conflict; a topology-only guess never
        # fabricates a `_pitch` name.
        self.assertEqual(
            {
                name: joint_names[name]
                for name in (
                    "r_elbow",
                    "l_elbow",
                    "waist",
                    "r_knee",
                    "l_knee",
                    "r_ankle",
                    "l_ankle",
                )
            },
            {
                "r_elbow": "right_elbow_joint",
                "l_elbow": "left_elbow_joint",
                "waist": "waist_joint",
                "r_knee": "right_knee_joint",
                "l_knee": "left_knee_joint",
                "r_ankle": "right_ankle_joint",
                "l_ankle": "left_ankle_joint",
            },
        )
        self.assertEqual(len(body_names), 25)
        self.assertEqual(len(joint_names), 24)
        self.assertEqual(len(set(body_names.values())), 25)
        self.assertEqual(len(set(joint_names.values())), 24)
        self.assertTrue(joint_names["sensor_mount"].endswith("_joint"))
        for joint in joints:
            self.assertIn(body_names[joint["parent"]], body_names.values())
            self.assertIn(body_names[joint["child"]], body_names.values())

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
        _links, names = _carrier_names(bodies, joints, "base")
        self.assertEqual(names["tool_mount"], "base_link_to_tool_link_joint")

    def test_real_root_reserves_base_link_before_collision_resolution(self):
        bodies = [
            {"name": "a", "frame": {"coordinate_system": "CS_base_link"}},
            {"name": "root", "frame": {"coordinate_system": "CS_base_link"}},
        ]
        names, _joints = _carrier_names(bodies, [], "root")
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
        self.assertTrue(material_name("waist-1/part-1", link="pelvis_link").endswith("_material"))
        digest = "a" * 64
        self.assertEqual(asset_namespace("M3.0.3_20261016.SLDASM"), "m303_20261016")
        self.assertEqual(
            mesh_path(digest, namespace="M3.0.3_20261016.SLDASM"),
            f"meshes/visual/m303_20261016/native_{digest}.stl",
        )
        self.assertEqual(
            mesh_path(digest, namespace="M3.0.3_20261016.SLDASM"),
            mesh_path(digest, namespace="M3.0.3_20261016.SLDASM"),
        )
        with self.assertRaises(ValueError):
            asset_namespace("")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            mesh_path("not-a-digest", namespace="robot")

    def test_empty_or_non_ascii_names_fail_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            token("***")
        self.assertEqual(fallback_name("link", "***"), fallback_name("link", "***"))
        self.assertRegex(fallback_name("joint", "***"), r"^joint_[0-9a-f]{16}_joint$")


if __name__ == "__main__":
    unittest.main()
