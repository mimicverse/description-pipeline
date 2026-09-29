"""Every rejection the declared-pose parser can make, and the two shapes it accepts.

`profile.validation_poses` names a file inside `config/` whose poses decide where contact and reset
are checked, so a wrong declaration has to stop the run with a diagnostic.  Coverage showed the
rejections were never executed: the suite only ever declared a valid file.
"""

import json
import math
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError
from description_pipeline.verification import poses as poses_module
from description_pipeline.verification.urdf_quality import model as parser

ROOT = Path(__file__).resolve().parents[2]
DEMO_URDF = ROOT / "examples" / "demo-arm" / "urdf" / "robot.urdf"
SCHEMA = poses_module.SCHEMA
JOINTS = {"shoulder_joint": 0.0, "elbow_joint": 0.0}


class FakeJoint:
    def __init__(self, name: str, *, kind: str = "revolute", limits=None, moveable: bool = True) -> None:
        self.name = name
        self.type = kind
        self.limits = limits
        self.moveable = moveable


class FakeUrdf:
    def __init__(self, joints: dict) -> None:
        self.joints = joints


def real_urdf():
    return parser.load_urdf(DEMO_URDF)


def write_poses(root: Path, payload: object, relative: str = "config/poses.json") -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


class PathTests(unittest.TestCase):
    def test_the_declared_path_has_to_be_a_config_relative_json(self):
        cases = {
            "empty": "",
            "not a string": 3,
            "absolute": "/etc/poses.json",
            "windows separator": "config\\poses.json",
            "parent traversal": "config/../poses.json",
            "not json": "config/poses.yaml",
        }
        for label, value in cases.items():
            with self.subTest(case=label), self.assertRaises(PipelineError):
                poses_module.validate_path(value)
        with self.assertRaisesRegex(PipelineError, "inside config/"):
            poses_module.validate_path("poses.json")
        self.assertEqual(poses_module.validate_path("config/poses.json"), "config/poses.json")
        self.assertIsNone(poses_module.validate_path(None))

    def test_numbers_are_length_and_finiteness_checked(self):
        self.assertEqual(poses_module._numbers([1, 2, 3], "position", 3), [1.0, 2.0, 3.0])
        with self.assertRaisesRegex(PipelineError, "list of 3 numbers"):
            poses_module._numbers([1, 2], "position", 3)
        for value in ([1, 2, math.inf], [1, 2, True], [1, 2, "3"]):
            with self.subTest(value=value), self.assertRaisesRegex(PipelineError, "finite numbers"):
                poses_module._numbers(value, "position", 3)


class LoadTests(unittest.TestCase):
    def load(self, root: Path, profile: dict, urdf=None, mimics=None):
        return poses_module.load(root, profile, urdf or real_urdf(), mimics)

    def test_no_declaration_means_no_poses(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(self.load(Path(temporary), {}), [])

    def test_a_missing_file_is_named(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(PipelineError, "validation poses file not found: config/poses.json"):
                self.load(root, {"validation_poses": "config/poses.json"})

    def test_the_file_shape_is_exact(self):
        cases = {
            "extra key": {"schema_version": SCHEMA, "poses": [], "note": "x"},
            "wrong schema": {"schema_version": "other/v1", "poses": []},
            "empty list": {"schema_version": SCHEMA, "poses": []},
            "not a list": {"schema_version": SCHEMA, "poses": {}},
        }
        for label, payload in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_poses(root, payload)
                with self.assertRaises(PipelineError):
                    self.load(root, {"validation_poses": "config/poses.json"})

    def test_each_pose_rejection_names_the_problem(self):
        cases = {
            "not an object": ([42], "must be an object with name/joints/base only"),
            "extra field": ([{"name": "p", "joints": JOINTS, "note": "x"}], "must be an object with"),
            "empty name": ([{"name": "  ", "joints": JOINTS}], "unique non-empty name"),
            "duplicate name": ([{"name": "p", "joints": JOINTS}, {"name": "p", "joints": JOINTS}], "unique non-empty"),
            "joints missing": ([{"name": "p"}], "must declare a joints object"),
            "joint missing": ([{"name": "p", "joints": {"shoulder_joint": 0.0}}], "cover every moveable joint"),
            "unknown joint": (
                [{"name": "p", "joints": {**JOINTS, "ghost": 0.0}}],
                "cover every moveable joint",
            ),
            "value not a number": ([{"name": "p", "joints": {**JOINTS, "elbow_joint": "x"}}], "must be a number"),
            "value not finite": ([{"name": "p", "joints": {**JOINTS, "elbow_joint": math.nan}}], "must be finite"),
            "outside limits": ([{"name": "p", "joints": {**JOINTS, "elbow_joint": 9.0}}], "outside its limits"),
        }
        for label, (raw, fragment) in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_poses(root, {"schema_version": SCHEMA, "poses": raw})
                with self.assertRaises(PipelineError) as raised:
                    self.load(root, {"validation_poses": "config/poses.json"})
                self.assertIn(fragment, str(raised.exception))

    def test_a_joint_without_limits_is_refused(self):
        urdf = FakeUrdf({"shoulder_joint": FakeJoint("shoulder_joint", limits=None)})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_poses(
                root,
                {"schema_version": SCHEMA, "poses": [{"name": "p", "joints": {"shoulder_joint": 0.0}}]},
            )
            with self.assertRaisesRegex(PipelineError, "has no declared limits"):
                self.load(root, {"validation_poses": "config/poses.json"}, urdf=urdf)

    def test_a_mimic_relation_has_to_agree_and_is_then_rewritten(self):
        relations = {
            "elbow_joint": {"joint": "shoulder_joint", "multiplier": 2.0, "offset": 0.1},
            # A relation for a joint the pose cannot declare (here: a fixed one) is skipped, not an error.
            "tool_fixed": {"joint": "elbow_joint", "multiplier": 1.0, "offset": 0.0},
        }
        base: dict = {
            "schema_version": SCHEMA,
            "poses": [{"name": "p", "joints": {"shoulder_joint": 0.5, "elbow_joint": 1.1}}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_poses(root, base)
            loaded = self.load(root, {"validation_poses": "config/poses.json"}, mimics=relations)
        self.assertAlmostEqual(loaded[0]["joints"]["elbow_joint"], 1.1, places=12)

        base["poses"][0]["joints"]["elbow_joint"] = -1.1
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_poses(root, base)
            with self.assertRaisesRegex(PipelineError, "contradicts its mimic relation"):
                self.load(root, {"validation_poses": "config/poses.json"}, mimics=relations)

    def test_a_base_pose_belongs_to_a_floating_profile(self):
        valid = {
            "schema_version": SCHEMA,
            "poses": [{"name": "p", "joints": JOINTS, "base": {"position": [0, 0, 0.5], "rpy": [0, 0, 0]}}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_poses(root, valid)
            loaded = self.load(root, {"validation_poses": "config/poses.json", "root_mode": "floating"})
        self.assertEqual(loaded[0]["base"]["position"], [0, 0, 0.5])
        self.assertIsNotNone(loaded[0]["matrix"])

        fixed = {
            "schema_version": SCHEMA,
            "poses": [{"name": "p", "joints": JOINTS, "base": {"position": [0, 0, 0], "rpy": [0, 0, 0]}}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_poses(root, fixed)
            with self.assertRaisesRegex(PipelineError, "fixed-root profile"):
                self.load(root, {"validation_poses": "config/poses.json"})

        incomplete = {
            "schema_version": SCHEMA,
            "poses": [{"name": "p", "joints": JOINTS, "base": {"position": [0, 0, 0]}}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_poses(root, incomplete)
            with self.assertRaisesRegex(PipelineError, "must declare base position and rpy"):
                self.load(root, {"validation_poses": "config/poses.json", "root_mode": "floating"})

    def test_a_valid_declaration_returns_the_pose(self):
        payload = {"schema_version": SCHEMA, "poses": [{"name": "home", "joints": JOINTS}]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_poses(root, payload)
            loaded = self.load(root, {"validation_poses": "config/poses.json"})
        self.assertEqual([pose["name"] for pose in loaded], ["home"])
        self.assertEqual(loaded[0]["joints"], JOINTS)
        self.assertIsNone(loaded[0]["matrix"])

    def test_a_continuous_joint_needs_no_limits(self):
        urdf = FakeUrdf({"spin": FakeJoint("spin", kind="continuous", limits=None)})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_poses(root, {"schema_version": SCHEMA, "poses": [{"name": "p", "joints": {"spin": 12.0}}]})
            loaded = self.load(root, {"validation_poses": "config/poses.json"}, urdf=urdf)
        self.assertEqual(loaded[0]["joints"]["spin"], 12.0)


if __name__ == "__main__":
    unittest.main()
