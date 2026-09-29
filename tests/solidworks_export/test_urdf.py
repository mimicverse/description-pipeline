import unittest

from tools.solidworks_export.model import RobotJoint, RobotLink, RobotModel
from tools.solidworks_export.urdf import build_urdf, format_number

EXPECTED = """<?xml version="1.0"?>
<robot name="r1">
  <link name="base_link">
    <inertial>
      <origin xyz="0 0 0.1" rpy="0 0 0"/>
      <mass value="2"/>
      <inertia ixx="0.1" ixy="0" ixz="0" iyy="0.2" iyz="0" izz="0.3"/>
    </inertial>
    <visual>
      <geometry>
        <mesh filename="meshes/base_link.STL"/>
      </geometry>
    </visual>
    <collision>
      <geometry>
        <mesh filename="meshes/base_link.STL"/>
      </geometry>
    </collision>
  </link>
  <link name="arm_link">
    <inertial>
      <origin xyz="0 0 0" rpy="0 0 0"/>
      <mass value="1"/>
      <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.02" iyz="0" izz="0.03"/>
    </inertial>
    <visual>
      <geometry>
        <mesh filename="meshes/arm_link.STL"/>
      </geometry>
    </visual>
    <collision>
      <geometry>
        <mesh filename="meshes/arm_link.STL"/>
      </geometry>
    </collision>
  </link>
  <joint name="dof_arm" type="revolute">
    <origin xyz="0 0 0.1" rpy="0 0 0"/>
    <parent link="base_link"/>
    <child link="arm_link"/>
    <axis xyz="0 0 1"/>
    <limit lower="-1" upper="1" effort="10" velocity="2"/>
    <dynamics damping="0.5" friction="0.1"/>
  </joint>
</robot>
"""


def _model() -> RobotModel:
    return RobotModel(
        name="r1",
        links=[
            RobotLink(
                name="base_link",
                mass=2.0,
                com=(0.0, 0.0, 0.1),
                inertia6=(0.1, 0.0, 0.0, 0.2, 0.0, 0.3),
                inertial_rpy=(0.0, 0.0, 0.0),
                mesh="meshes/base_link.STL",
            ),
            RobotLink(
                name="arm_link",
                mass=1.0,
                com=(0.0, 0.0, 0.0),
                inertia6=(0.01, 0.0, 0.0, 0.02, 0.0, 0.03),
                inertial_rpy=(0.0, 0.0, 0.0),
                mesh="meshes/arm_link.STL",
            ),
        ],
        joints=[
            RobotJoint(
                name="dof_arm",
                type="revolute",
                parent="base_link",
                child="arm_link",
                xyz=(0.0, 0.0, 0.1),
                rpy=(0.0, 0.0, 0.0),
                axis=(0.0, 0.0, 1.0),
                limit={"lower": -1.0, "upper": 1.0, "effort": 10.0, "velocity": 2.0},
                dynamics={"damping": 0.5, "friction": 0.1},
            ),
        ],
    )


class UrdfTests(unittest.TestCase):
    def test_golden_output(self):
        self.assertEqual(build_urdf(_model()), EXPECTED)

    def test_deterministic(self):
        self.assertEqual(build_urdf(_model()), build_urdf(_model()))

    def test_number_formatting(self):
        self.assertEqual(format_number(0.0), "0")
        self.assertEqual(format_number(-0.0), "0")
        self.assertEqual(format_number(1.0), "1")
        self.assertEqual(format_number(0.1), "0.1")
        self.assertEqual(format_number(1e-13), "0")
        self.assertEqual(format_number(-2.5), "-2.5")

    def test_fixed_joint_has_no_axis_or_limit(self):
        model = _model()
        model.joints[0].type = "fixed"
        model.joints[0].axis = None
        model.joints[0].limit = None
        model.joints[0].dynamics = None
        text = build_urdf(model)
        self.assertIn('type="fixed"', text)
        self.assertNotIn("<axis", text)
        self.assertNotIn("<limit", text)


if __name__ == "__main__":
    unittest.main()
