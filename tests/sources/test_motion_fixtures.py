"""D's neutral motion fixtures executed by the product motion owner.

Each case's assertions run through ``verification.motion.evaluate_motion``: the
accept cases must reproduce the recorded per-leaf worlds, interface continuity
and intra-group relatives; the reject cases must either fail closed on the
evidence or show the statically green candidate diverging from the re-derived
worlds (the refusal basis of the ``motion.{joint}`` gate).
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import numpy as np

from description_pipeline.io import PipelineError
from description_pipeline.verification.motion import evaluate_motion

FIXTURES = Path(__file__).resolve().parent / "motion_fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def evaluate(doc: dict, *, poses: dict | None = None) -> dict:
    bodies = [
        {
            "id": body["id"],
            "group": body["rigid_group"],
            "frame": body["frame"],
            "leaves": [{"id": leaf["id"], "world": leaf["world"]} for leaf in body["leaves"]],
        }
        for body in doc["bodies"]
    ]
    return evaluate_motion(
        bodies=bodies,
        joints=doc["joints"],
        interface=doc["interface"],
        poses=doc["case"]["poses"] if poses is None else poses,
        tolerance=doc["tolerance"],
    )


class MotionFixtureTests(unittest.TestCase):
    def test_fixture_members_match_their_manifest(self):
        for line in (FIXTURES / "motion-fixtures.sha256").read_text(encoding="utf-8").splitlines():
            expected, name = line.split(None, 1)
            data = (FIXTURES / name.strip()).read_bytes()
            self.assertEqual(expected, hashlib.sha256(data).hexdigest(), name)

    def _accept(self, doc: dict) -> dict:
        result = evaluate(doc)
        tolerance = doc["tolerance"]
        for leaf, expected in doc["case"]["expected_world"].items():
            actual = np.asarray(result["world"][leaf], float)
            self.assertLessEqual(
                float(np.max(np.abs(actual - np.asarray(expected, float).reshape(4, 4)))),
                tolerance["position_m"],
                f"{leaf} does not follow the re-derived motion",
            )
        self.assertLessEqual(result["interface"]["line_distance_m"], tolerance["interface_line_m"])
        self.assertGreaterEqual(result["interface"]["parallelism"], 1 - tolerance["parallelism_1m"])
        for group, deviation in result["intra_group_relative"].items():
            self.assertLessEqual(deviation, tolerance["position_m"], f"{group} drifted internally")
        return result

    def test_case01_single_j1_follow(self):
        self._accept(load("case-01-single-j1.json"))

    def test_case02_single_j2_follow(self):
        self._accept(load("case-02-single-j2.json"))

    def test_case03_combined_pose(self):
        self._accept(load("case-03-combined.json"))

    def test_case04_interface_continuity(self):
        self._accept(load("case-04-interface-continuity.json"))

    def test_case05_wrong_axis_sign_rejected(self):
        with self.assertRaisesRegex(PipelineError, "anti-parallel"):
            evaluate(load("case-05-reject-axis-sign.json"))

    def test_case06_axis_offset_rejected(self):
        case = load("case-06-reject-axis-offset.json")
        # The offset evidence must be rejected against the analytic hinge; the
        # fixture carries no pose, so the baseline comparison uses the documented
        # single-J1 acceptance pose.
        pose = {"J1": load("case-01-single-j1.json")["case"]["poses"]["J1"]}
        correct = evaluate(load("model-correct.json"), poses=pose)
        candidate = evaluate(case, poses=pose)
        difference = max(
            float(
                np.max(
                    np.abs(
                        np.asarray(candidate["world"][leaf], float)
                        - np.asarray(correct["world"][leaf], float)
                    )
                )
            )
            for leaf in correct["world"]
        )
        self.assertGreater(difference, 1e-4, "a 2 mm axis offset must move the re-derived worlds")

    def test_case07_inverted_limits_rejected(self):
        with self.assertRaisesRegex(PipelineError, "ordered limits"):
            evaluate(load("case-07-reject-limits-order.json"))

    def test_case08_missing_drive_rejected(self):
        with self.assertRaisesRegex(PipelineError, "drive evidence"):
            evaluate(load("case-08-reject-missing-drive.json"))

    def test_case09_static_green_wrong_parent_rejected(self):
        case = load("case-09-static-green-wrong-parent.json")
        rederived = evaluate(case)
        tolerance = case["tolerance"]
        mismatches = {
            leaf: float(
                np.max(
                    np.abs(
                        np.asarray(case["case"]["candidate_world"][leaf], float).reshape(4, 4)
                        - np.asarray(rederived["world"][leaf], float)
                    )
                )
            )
            for leaf in case["case"]["candidate_world"]
        }
        self.assertGreater(max(mismatches.values()), 0.02, "the wrong parent graph must diverge under motion")
        self.assertGreater(
            case["case"]["interface_candidate"]["line_distance_m"],
            tolerance["interface_line_m"],
            "the wrong parent graph must break the re-derived interface continuity",
        )
        self.assertLessEqual(rederived["interface"]["line_distance_m"], tolerance["interface_line_m"])

    def test_case10_cross_group_leaf_rejected(self):
        case = load("case-10-reject-cross-group-leaf.json")
        rederived = evaluate(case)
        candidate = np.asarray(case["case"]["candidate_world"]["L1b"], float).reshape(4, 4)
        self.assertGreater(
            float(np.max(np.abs(candidate - np.asarray(rederived["world"]["L1b"], float)))),
            0.02,
            "the cross-group leaf must diverge from its evidenced rigid group",
        )
        declared = np.asarray(
            case["case"]["intra_group_relative"]["G1"]["relative"], float
        ).reshape(4, 4)
        candidate_relative = np.asarray(case["case"]["intra_group_relative_candidate_G1"], float).reshape(4, 4)
        self.assertGreater(
            float(np.max(np.abs(candidate_relative - declared))),
            case["tolerance"]["position_m"],
            "the candidate group relative must differ from the recorded invariant",
        )

    def test_product_gate_compares_the_delivered_graph_under_motion(self):
        from description_pipeline.verification.solidworks_urdf import _motion

        base = np.eye(4)
        tool = np.eye(4)
        tool[:3, 3] = (0.0, 0.0, 0.5)
        datums = {"base": base, "tool": tool}
        components = {"base_part": base.copy(), "tool_part": tool.copy()}
        bodies = [
            {"name": "base", "components": ["base_part"]},
            {"name": "tool", "components": ["tool_part"]},
        ]
        joints = [
            {
                "name": "hinge",
                "type": "revolute",
                "parent": "base",
                "child": "tool",
                "axis": [0.0, 0.0, 1.0],
            }
        ]
        authored = {
            "name": "hinge",
            "type": "revolute",
            "parent": "base",
            "child": "tool",
            "axis": [0.0, 0.0, 1.0],
            "limits": {"lower": -1.0, "upper": 1.0, "effort": 1.0, "velocity": 1.0},
            "axis_reference": {"component": "base_part", "feature_name": "Cyl1"},
        }
        record = {
            "component": "base_part",
            "surface": "cylinder",
            "selector": {"feature_name": "Cyl1", "component": "base_part", "body_type": "solid"},
            "body_type": "solid",
            "radius_m": 0.01,
            "coordinate_frame": "assembly",
            "axis_point_m": [0.0, 0.0, 0.5],
            "axis_direction": [0.0, 0.0, 1.0],
        }
        details = _motion(authored, joints, components, datums, bodies, record)
        self.assertLessEqual(details["worst_m"], 5e-5)
        flipped = {**record, "axis_direction": [0.0, 0.0, -1.0]}
        with self.assertRaisesRegex(PipelineError, "diverges from the re-derived motion"):
            _motion(authored, joints, components, datums, bodies, flipped)


if __name__ == "__main__":
    unittest.main()
