"""Every rejection in the canonical model contract, taken one mutation at a time.

`Robot.from_dict` is the single gate every canonical model passes before anything is generated from
it: geometry has to be physical, the kinematic graph a tree with one root, actuators and frames have
to point at things that exist.  Coverage showed the rejections were never executed — the examples
are all valid — so each one is written here as a mutation of the shipped demo model.
"""

import copy
import math
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, read_data
from description_pipeline.model import Robot

ROOT = Path(__file__).resolve().parents[2]
MODEL = read_data(ROOT / "examples" / "demo-arm" / "model" / "robot.json")


def model() -> dict:
    return copy.deepcopy(MODEL)


def link(data: dict, name: str) -> dict:
    return next(item for item in data["links"] if item["name"] == name)


def joint(data: dict, name: str) -> dict:
    return next(item for item in data["joints"] if item["name"] == name)


def actuator(data: dict, **overrides) -> dict:
    """A schema-valid actuator; the shipped examples declare none."""

    value = {
        "id": "shoulder_motor",
        "name": "shoulder_motor",
        "joint": "shoulder_joint",
        "type": "motor",
        "gear": 1.0,
        "control_range": [-1.0, 1.0],
        "provenance": {},
    }
    value.update(overrides)
    return value


class RobotContractTests(unittest.TestCase):
    def test_the_shipped_model_is_valid(self):
        robot = Robot.from_dict(model())
        self.assertEqual(robot.to_dict(), MODEL)
        self.assertIsNot(robot.to_dict(), robot.to_dict(), "callers get copies")

    def test_duplicate_identities_are_refused(self):
        data = model()
        data["links"][1]["name"] = data["links"][0]["name"]
        with self.assertRaisesRegex(PipelineError, "Duplicate links.name"):
            Robot.from_dict(data)

    def test_a_frame_may_not_collide_with_a_generated_link(self):
        data = model()
        data["frames"][0]["name"] = "base_link"
        with self.assertRaisesRegex(PipelineError, "Frame names collide"):
            Robot.from_dict(data)

        # The generator names a frame's fixed joint `<frame>_fixed`, so that name is taken too.
        data = model()
        clone = copy.deepcopy(joint(data, "shoulder_joint"))
        clone["id"] = clone["name"] = "tool_fixed"
        clone["type"] = "fixed"
        clone.pop("axis", None)
        clone.pop("limits", None)
        data["joints"].append(clone)
        with self.assertRaisesRegex(PipelineError, "Frame names collide"):
            Robot.from_dict(data)

    def test_geometry_has_to_be_physical(self):
        data = model()
        link(data, "base_link")["collisions"][0]["size"] = [0.1, 0.0, 0.06]
        with self.assertRaisesRegex(PipelineError, "Geometry size must be positive"):
            Robot.from_dict(data)

        data = model()
        link(data, "base_link")["collisions"][0]["rgba"] = [0.0, 0.0, 0.0, 1.5]
        with self.assertRaisesRegex(PipelineError, r"RGBA must be in \[0, 1\]"):
            Robot.from_dict(data)

    def test_the_kinematic_graph_has_to_be_a_single_tree(self):
        data = model()
        joint(data, "shoulder_joint")["child"] = "ghost_link"
        with self.assertRaisesRegex(PipelineError, "Unknown joint endpoint"):
            Robot.from_dict(data)

        data = model()
        joint(data, "shoulder_joint")["parent"] = "forearm_link"
        with self.assertRaisesRegex(PipelineError, "Disconnected or cyclic links"):
            Robot.from_dict(data)

        data = model()
        data["joints"] = [joint(data, "shoulder_joint")]
        with self.assertRaisesRegex(PipelineError, "Expected one kinematic root"):
            Robot.from_dict(data)

        data = model()
        link(data, "forearm_link")["inertial"] = None
        with self.assertRaisesRegex(PipelineError, "Missing physical inertia"):
            Robot.from_dict(data)

    def test_movable_joints_need_an_explicit_normalized_axis_and_limits(self):
        data = model()
        joint(data, "shoulder_joint").pop("axis")
        with self.assertRaisesRegex(PipelineError, "Missing or zero joint axis"):
            Robot.from_dict(data)

        data = model()
        joint(data, "shoulder_joint")["axis"] = [0.0, 2.0, 0.0]
        with self.assertRaisesRegex(PipelineError, "must be normalized"):
            Robot.from_dict(data)

        data = model()
        joint(data, "shoulder_joint")["limits"] = {"lower": 1.0, "upper": 1.0, "effort": 10.0, "velocity": 3.0}
        with self.assertRaisesRegex(PipelineError, "position limits"):
            Robot.from_dict(data)

    def test_multiple_parents_are_refused(self):
        data = model()
        clone = copy.deepcopy(joint(data, "elbow_joint"))
        clone["id"] = clone["name"] = "second_parent"
        clone["parent"] = "base_link"
        clone["type"] = "fixed"
        clone.pop("axis", None)
        clone.pop("limits", None)
        data["joints"].append(clone)
        with self.assertRaisesRegex(PipelineError, "Multiple parent joints"):
            Robot.from_dict(data)

    def test_mimic_and_actuators_have_to_reference_movable_joints(self):
        data = model()
        joint(data, "elbow_joint")["mimic"] = {"joint": "tool_fixed", "multiplier": 1.0, "offset": 0.0}
        with self.assertRaisesRegex(PipelineError, "Mimic must reference movable joints"):
            Robot.from_dict(data)

        data = model()
        data["actuators"] = [actuator(data, joint="tool_fixed")]
        with self.assertRaisesRegex(PipelineError, "Unknown actuator joint"):
            Robot.from_dict(data)

        data = model()
        data["actuators"] = [actuator(data, gear=0.0)]
        with self.assertRaisesRegex(PipelineError, "nonzero gear and an ordered control range"):
            Robot.from_dict(data)

        data = model()
        data["actuators"] = [actuator(data, control_range=[1.0, 1.0])]
        with self.assertRaisesRegex(PipelineError, "nonzero gear and an ordered control range"):
            Robot.from_dict(data)

    def test_frames_and_contact_exclusions_have_to_exist(self):
        data = model()
        data["frames"][0]["parent"] = "ghost_link"
        with self.assertRaisesRegex(PipelineError, "Unknown frame parent"):
            Robot.from_dict(data)

        data = model()
        data["contact_excludes"] = [{"body1": "base_link", "body2": "ghost_link", "reason": "test"}]
        with self.assertRaisesRegex(PipelineError, "Unknown body in contact exclusion"):
            Robot.from_dict(data)

    def test_the_schema_and_finiteness_are_enforced(self):
        data = model()
        data.pop("units")
        with self.assertRaisesRegex(PipelineError, "Model contract"):
            Robot.from_dict(data)

        data = model()
        link(data, "base_link")["inertial"]["mass"] = math.nan
        with self.assertRaisesRegex(PipelineError, "Nonfinite model value|Model contract"):
            Robot.from_dict(data)


if __name__ == "__main__":
    unittest.main()
