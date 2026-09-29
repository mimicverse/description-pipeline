"""Declared working poses: contact and reset behaviour in the real consumer."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from description_pipeline.build import build, freeze, lock_toolchain
from description_pipeline.io import PipelineError, read_data, write_json
from description_pipeline.repository import init_model
from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.verification import inspect
from tests.pipeline import ledger as joint_ledger

POSES_SCHEMA = "description.validation-poses/v1"


def _link(name: str, size: list[float], xyz: list[float]) -> dict:
    geometry = {"kind": "box", "size": size, "xyz": xyz, "rpy": [0, 0, 0]}
    return {
        "id": name,
        "name": name,
        "inertial": {
            "mass": 1.0,
            "xyz": [0, 0, 0],
            "rpy": [0, 0, 0],
            "inertia": [0.002, 0, 0, 0.003, 0, 0.004],
        },
        "visuals": [copy.deepcopy(geometry)],
        "collisions": [copy.deepcopy(geometry)],
        "provenance": {"source_entities": [name]},
    }


def scene() -> dict:
    """base_link -> arm_link -> slider_link -> tip_link.

    The slider can travel far enough to enter the base box, and the two are not
    parent and child, so MuJoCo reports that overlap as a self collision.
    """

    links = [
        _link("base_link", [0.4, 0.4, 0.06], [0, 0, 0]),
        _link("arm_link", [0.1, 0.1, 0.1], [0.25, 0, 0]),
        _link("slider_link", [0.1, 0.1, 0.1], [0, 0, 0]),
        _link("tip_link", [0.05, 0.05, 0.05], [0, 0, 0]),
    ]
    joints = []
    for name, parent, child, kind, lower, upper, xyz in (
        ("arm_joint", "base_link", "arm_link", "revolute", -1.5, 1.5, [0, 0, 0.3]),
        ("slider_joint", "arm_link", "slider_link", "prismatic", -0.35, 0.0, [0, 0, 0]),
        ("tip_joint", "slider_link", "tip_link", "prismatic", 0.0, 0.1, [0, 0, 0]),
    ):
        joints.append(
            {
                "id": name,
                "name": name,
                "type": kind,
                "parent": parent,
                "child": child,
                "xyz": xyz,
                "rpy": [0, 0, 0],
                "axis": [0, 0, 1],
                "limits": {"lower": lower, "upper": upper, "effort": 2, "velocity": 3},
                "dynamics": {"damping": 0.01, "friction": 0},
                "provenance": {"reference": "analytic_fixture"},
            }
        )
    return {
        "schema_version": "description.scene/v1",
        "name": "robot",
        "units": "SI",
        "links": links,
        "joints": joints,
        "frames": [],
        "actuators": [],
        "sensors": [],
        "constraints": [],
        "control": {},
        "contact_excludes": [],
        "provenance": {"expected_entities": [item["name"] for item in links], "fixture": True},
    }


STAND = {"arm_joint": 0.0, "slider_joint": 0.0, "tip_joint": 0.0}


class ValidationPoseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.source = base / "source"
        self.source.mkdir()
        write_json(self.source / "scene.json", scene())
        write_manifest(self.source, kind="fixture", identity={"case": "declared-poses"}, evidence_class="fixture")
        self.root = base / "model"
        init_model(self.root, "fixture", {"provider": "fixture", "path": str(self.source)})
        joint_ledger.write(self.root, "arm_joint", "slider_joint", "tip_joint")
        freeze(self.root)
        lock_toolchain(self.root)

    # -- helpers ---------------------------------------------------------

    def _profile(self, **overrides) -> None:
        path = self.root / "config/profiles/kinematics.json"
        profile = read_data(path)
        profile.update({"root_mode": "floating", "ground": True, "steps": 20, **overrides})
        write_json(path, profile)

    def _declared(self, poses: list[dict]) -> None:
        write_json(
            self.root / "config/poses.json",
            {"schema_version": POSES_SCHEMA, "poses": poses},
        )

    def _pose(self, name: str, joints: dict | None = None, z: float = 0.5) -> dict:
        return {
            "name": name,
            "joints": {**STAND, **(joints or {})},
            "base": {"position": [0.0, 0.0, z], "rpy": [0.0, 0.0, 0.0]},
        }

    def _report(self, **overrides) -> tuple[dict, dict]:
        self._profile(**overrides)
        report = build(self.root, "kinematics")
        return report, {item["id"]: item for item in report["checks"]}

    def _checks(self, profile: dict) -> dict:
        """Re-run the consumer checks against one profile without rebuilding."""

        return {item["id"]: item for item in inspect(self.root, profile)}

    # -- tests -----------------------------------------------------------

    def test_declared_poses_drive_contact_and_reset(self) -> None:
        self._declared([self._pose("stand")])
        report, checks = self._report(validation_poses="config/poses.json")

        self.assertTrue(report["passed"], report["blockers"])
        declared = checks["consumer.validation_poses"]
        self.assertEqual(declared["status"], "passed")
        self.assertEqual(declared["details"]["contact_policy"], "declared_poses")
        self.assertEqual(declared["details"]["poses"], ["stand"])
        self.assertEqual(declared["details"]["reset_source"], "stand")
        contacts = checks["consumer.contacts"]
        self.assertEqual(contacts["status"], "passed")
        self.assertEqual(contacts["details"]["policy"], "declared_poses")
        self.assertEqual(contacts["details"]["poses_checked"], ["stand"])
        reset = checks["consumer.reset_step"]
        self.assertEqual(reset["status"], "passed")
        self.assertEqual(reset["details"]["pose_source"], "validation_pose:stand")

    def test_full_range_samples_alone_would_fail_on_a_legal_pose(self) -> None:
        self._declared([self._pose("stand")])
        report, _checks = self._report(validation_poses="config/poses.json")

        # simulation purpose makes the contact check a blocker; the same compiled
        # model then fails under the legacy all-sample policy because a full-range
        # sample drives the slider into the base box ...
        legacy = self._checks({**report["profile"], "purpose": "simulation", "validation_poses": None})
        contacts = legacy["consumer.contacts"]
        self.assertEqual(contacts["status"], "failed")
        self.assertEqual(contacts["details"]["policy"], "all_samples")
        self.assertTrue(contacts["details"]["violations"])

        # ... while the declared working pose is accepted
        declared = self._checks({**report["profile"], "purpose": "simulation"})
        self.assertEqual(declared["consumer.contacts"]["status"], "passed")
        self.assertEqual(declared["consumer.contacts"]["details"]["policy"], "declared_poses")

    def test_ground_penetration_in_a_declared_pose_is_rejected(self) -> None:
        self._declared([self._pose("stand")])
        report, _checks = self._report(validation_poses="config/poses.json")
        self._declared([self._pose("sunken", z=-0.02)])
        checks = self._checks(report["profile"])

        violations = checks["consumer.contacts"]["details"]["violations"]
        self.assertEqual({item["pose"] for item in violations}, {"sunken"})
        touched = {item["geom1_name"] for item in violations} | {item["geom2_name"] for item in violations}
        self.assertIn("ground", touched)
        reset = checks["consumer.reset_step"]
        self.assertEqual(reset["status"], "failed")
        self.assertLess(reset["details"]["start_min_contact_m"], 0.0)

    def test_self_collision_in_a_declared_pose_is_rejected(self) -> None:
        self._declared([self._pose("stand")])
        report, _checks = self._report(validation_poses="config/poses.json")
        self._declared([self._pose("crowded", {"slider_joint": -0.34})])
        checks = self._checks(report["profile"])

        violations = checks["consumer.contacts"]["details"]["violations"]
        names = {item["geom1_name"] for item in violations} | {item["geom2_name"] for item in violations}
        self.assertTrue(any(name and "slider_link" in name for name in names), names)
        self.assertTrue(any(name and "base_link" in name for name in names), names)

    def test_declared_pose_file_is_validated_not_ignored(self) -> None:
        self._declared([self._pose("stand")])
        report, _checks = self._report(validation_poses="config/poses.json")
        cases = {
            "unknown-joint": {
                "schema_version": POSES_SCHEMA,
                "poses": [self._pose("stand", {"not_a_joint": 0.0})],
            },
            "incomplete": {
                "schema_version": POSES_SCHEMA,
                "poses": [
                    {
                        "name": "stand",
                        "joints": {"arm_joint": 0.0},
                        "base": {"position": [0, 0, 0.5], "rpy": [0, 0, 0]},
                    }
                ],
            },
            "out-of-range": {"schema_version": POSES_SCHEMA, "poses": [self._pose("stand", {"arm_joint": 9.0})]},
            # a hand-edited file can carry NaN even though write_json refuses it
            "not-finite": (
                '{"schema_version": "description.validation-poses/v1", "poses": '
                '[{"name": "stand", "joints": {"arm_joint": NaN, "slider_joint": 0.0, '
                '"tip_joint": 0.0}, "base": {"position": [0, 0, 0.5], "rpy": [0, 0, 0]}}]}'
            ),
            "no-base": {"schema_version": POSES_SCHEMA, "poses": [{"name": "stand", "joints": STAND}]},
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                path = self.root / "config/poses.json"
                if isinstance(payload, str):
                    path.write_text(payload, encoding="utf-8")
                else:
                    write_json(path, payload)
                checks = self._checks(report["profile"])

                declared = checks["consumer.validation_poses"]
                self.assertEqual(declared["status"], "failed")
                self.assertTrue(declared["details"]["error"])
                # an unusable contract must not fall back to the legacy policy
                self.assertEqual(checks["consumer.contacts"]["status"], "failed")
                self.assertEqual(checks["consumer.reset_step"]["status"], "failed")

    def test_profile_path_must_stay_inside_config(self) -> None:
        for value in ("../poses.json", "/tmp/poses.json", "poses.json", "config/../poses.json"):
            with self.subTest(value=value):
                self._profile()
                path = self.root / "config/profiles/kinematics.json"
                profile = read_data(path)
                profile["validation_poses"] = value
                write_json(path, profile)
                with self.assertRaises(PipelineError):
                    build(self.root, "kinematics")

    def test_full_range_fk_samples_are_unchanged_by_declared_poses(self) -> None:
        self._declared([self._pose("stand")])
        _report, checks = self._report(validation_poses="config/poses.json")

        samples = checks["consumer.kinematics"]["details"]["samples"]
        limits = {
            "arm_joint": (-1.5, 1.5),
            "slider_joint": (-0.35, 0.0),
            "tip_joint": (0.0, 0.1),
        }
        # every joint still reaches both ends of its range: the declared poses only
        # changed which states the *contact* check looks at
        for joint, (lower, upper) in limits.items():
            values = [sample[joint] for sample in samples]
            span = upper - lower
            self.assertLessEqual(min(values), lower + 0.01 * span + 1e-12)
            self.assertGreaterEqual(max(values), upper - 0.01 * span - 1e-12)
            self.assertGreaterEqual(max(values) - min(values), 0.97 * span)
        self.assertEqual(checks["consumer.contacts"]["details"]["fk_samples"], len(samples))


if __name__ == "__main__":
    unittest.main()
