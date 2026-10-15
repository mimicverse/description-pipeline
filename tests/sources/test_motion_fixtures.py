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
import math
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_product_gate_rejects_a_wrong_parent_on_the_delivered_graph(self):
        # G06 dependency boundary: with the native hinge line off the child
        # frame's origin, a swapped parent/child chain is statically plausible
        # but diverges from the re-derived motion as soon as the pose is
        # non-zero; the correctly attached chain passes on the same bytes.
        from description_pipeline.verification.solidworks_urdf import _motion

        point = (0.2, 0.0, 0.5)
        base = np.eye(4)
        tool = np.eye(4)
        tool[:3, 3] = point
        datums = {"base": base, "tool": tool}
        components = {"base_part": base.copy(), "tool_part": tool.copy()}
        bodies = [
            {"name": "base", "components": ["base_part"]},
            {"name": "tool", "components": ["tool_part"]},
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
            "axis_point_m": list(point),
            "axis_direction": [0.0, 0.0, 1.0],
        }
        details = _motion(authored, [authored], components, datums, bodies, record)
        self.assertLessEqual(details["worst_m"], 5e-5)
        swapped = {**authored, "parent": "tool", "child": "base"}
        with self.assertRaisesRegex(PipelineError, "diverges from the re-derived motion"):
            _motion(swapped, [swapped], components, datums, bodies, record)

    @staticmethod
    def _serial_joint(name, parent, child, owner):
        return {
            "name": name,
            "type": "revolute",
            "parent": parent,
            "child": child,
            "axis": [0.0, 0.0, 1.0],
            "limits": {"lower": -1.0, "upper": 1.0, "effort": 1.0, "velocity": 1.0},
            "axis_reference": {"component": owner, "feature_name": "Cyl-" + name},
        }

    @staticmethod
    def _serial_record(name, point, owner):
        return {
            "component": owner,
            "surface": "cylinder",
            "body_type": "solid",
            "radius_m": 0.01,
            "selector": {"feature_name": "Cyl-" + name, "component": owner, "body_type": "solid"},
            "coordinate_frame": "assembly",
            "axis_point_m": list(point),
            "axis_direction": [0.0, 0.0, 1.0],
        }

    def _serial_setup(self):
        datums = {
            "base_link": np.eye(4),
            "link1": np.array([[1, 0, 0, 0.2], [0, 1, 0, 0], [0, 0, 1, 0.5], [0, 0, 0, 1]], float),
            "link2": np.array([[1, 0, 0, 0.4], [0, 1, 0, 0], [0, 0, 1, 0.5], [0, 0, 0, 1]], float),
        }
        components = {f"part{index}": pose.copy() for index, pose in enumerate(datums.values())}
        bodies = [
            {"name": "base_link", "components": ["part0"]},
            {"name": "link1", "components": ["part1"]},
            {"name": "link2", "components": ["part2"]},
        ]
        joints = [
            self._serial_joint("J1", "base_link", "link1", "part0"),
            self._serial_joint("J2", "link1", "link2", "part1"),
        ]
        records = [
            self._serial_record("J1", (0.2, 0.0, 0.5), "part0"),
            self._serial_record("J2", (0.4, 0.0, 0.5), "part1"),
        ]
        return datums, components, bodies, joints, records

    def test_product_gate_accepts_a_correct_serial_chain(self):
        # G06: the expected side must carry the WHOLE delivered chain - a
        # correct 2R chain is accepted at the upstream joint (whose motion
        # moves the terminal body) and at the terminal joint; a combined
        # non-zero pose matches the closed-form composition.
        from description_pipeline.verification.motion import evaluate_motion
        from description_pipeline.verification.solidworks_urdf import _motion

        datums, components, bodies, joints, records = self._serial_setup()
        captured = []
        real_evaluate = evaluate_motion

        def trace(**kwargs):
            captured.append(dict(kwargs["poses"]))
            return real_evaluate(**kwargs)

        with patch("description_pipeline.verification.motion.evaluate_motion", side_effect=trace):
            upstream = _motion(joints[0], joints, components, datums, bodies, records[0])
            terminal = _motion(joints[1], joints, components, datums, bodies, records[1])
        self.assertLessEqual(upstream["worst_m"], 5e-5)
        self.assertLessEqual(upstream["worst_translation_m"], 5e-5)
        self.assertLessEqual(upstream["worst_rotation_rad"], 5e-5)
        self.assertLessEqual(terminal["worst_m"], 5e-5)
        # The product bridge itself must sample a combined non-zero pose; every
        # combined value comes from that joint's own evidence, never a zero or
        # default filler.
        combined = [
            poses for poses in captured if sum(abs(float(value)) > 1e-12 for value in poses.values()) >= 2
        ]
        self.assertTrue(combined, "the product bridge never sampled a combined non-zero pose")
        for poses in combined:
            for value in poses.values():
                self.assertGreater(abs(float(value)), 1e-12)
        doc_bodies = [
            {
                "id": body["name"],
                "group": body["name"],
                "frame": datums[body["name"]].tolist(),
                "leaves": [
                    {"id": leaf, "world": components[leaf].tolist()} for leaf in body["components"]
                ],
            }
            for body in bodies
        ]
        doc_joints = [
            {
                "id": joint["name"],
                "type": joint["type"],
                "parent": joint["parent"],
                "child": joint["child"],
                "axis_point": list(record["axis_point_m"]),
                "axis_dir": list(record["axis_direction"]),
                "stored_direction": list(record["axis_direction"]),
                "limits": joint["limits"],
                "drive": {"effort": 1.0, "velocity": 1.0},
                "origin_parent_child": np.eye(4).ravel().tolist(),
            }
            for joint, record in zip(joints, records, strict=True)
        ]
        interface = {
            "name": "IF-J2",
            "on_parent": "link1",
            "on_child": "link2",
            "axis_point": [0.4, 0.0, 0.5],
            "axis_dir": [0.0, 0.0, 1.0],
        }
        sampled = combined[0]
        q1, q2 = float(sampled["J1"]), float(sampled["J2"])

        def line(angle, point):
            cosine, sine = math.cos(angle), math.sin(angle)
            rotation = np.array([[cosine, -sine, 0.0], [sine, cosine, 0.0], [0.0, 0.0, 1.0]])
            matrix = np.eye(4)
            matrix[:3, :3] = rotation
            matrix[:3, 3] = np.asarray(point, float) - rotation @ np.asarray(point, float)
            return matrix

        combined = evaluate_motion(
            bodies=doc_bodies, joints=doc_joints, interface=interface, poses={"J1": q1, "J2": q2}
        )
        expected = line(q1, (0.2, 0.0, 0.5)) @ line(q2, (0.4, 0.0, 0.5)) @ datums["link2"]
        self.assertLessEqual(
            float(np.max(np.abs(np.asarray(combined["world"]["part2"], float) - expected))), 1e-9
        )

    def test_product_gate_still_refuses_broken_serial_edges(self):
        # G06 negatives kept on the chain: a reversed edge and a delivered axis
        # that disagrees with the native shaft line both diverge under motion;
        # one rigid group still cannot span two bodies.
        from description_pipeline.verification.motion import evaluate_motion
        from description_pipeline.verification.solidworks_urdf import _motion

        datums, components, bodies, joints, records = self._serial_setup()
        reversed_first = {**joints[0], "parent": "link1", "child": "base_link"}
        with self.assertRaisesRegex(PipelineError, "diverges from the re-derived motion"):
            _motion(reversed_first, [reversed_first, joints[1]], components, datums, bodies, records[0])
        wrong_axis = {**joints[0], "axis": [1.0, 0.0, 0.0]}
        with self.assertRaisesRegex(PipelineError, "diverges from the re-derived motion"):
            _motion(wrong_axis, [wrong_axis, joints[1]], components, datums, bodies, records[0])
        spanning = [
            {"id": "base_link", "group": "shared", "frame": datums["base_link"].tolist(), "leaves": []},
            {"id": "link1", "group": "shared", "frame": datums["link1"].tolist(), "leaves": []},
        ]
        with self.assertRaisesRegex(PipelineError, "spans more than one body"):
            evaluate_motion(
                bodies=spanning,
                joints=[],
                interface={
                    "name": "IF",
                    "on_parent": "base_link",
                    "on_child": "link1",
                    "axis_point": [0.0, 0.0, 0.0],
                    "axis_dir": [0.0, 0.0, 1.0],
                },
                poses={},
            )

    def test_motion_owner_translates_a_prismatic_joint(self):
        # G06: a prismatic joint moves the child along its evidenced axis by
        # the pose and rotates nothing - positive and negative displacements.
        from description_pipeline.verification.motion import evaluate_motion

        base = np.eye(4)
        tool = np.eye(4)
        tool[:3, 3] = (0.2, 0.0, 0.5)
        doc_bodies = [
            {
                "id": "base_link",
                "group": "base_link",
                "frame": base.tolist(),
                "leaves": [{"id": "base_part", "world": base.tolist()}],
            },
            {
                "id": "tool",
                "group": "tool",
                "frame": tool.tolist(),
                "leaves": [{"id": "tool_part", "world": tool.tolist()}],
            },
        ]
        doc_joint = {
            "id": "slide",
            "type": "prismatic",
            "parent": "base_link",
            "child": "tool",
            "axis_point": [0.2, 0.0, 0.5],
            "axis_dir": [0.0, 0.0, 1.0],
            "stored_direction": [0.0, 0.0, 1.0],
            "limits": {"lower": -1.0, "upper": 1.0, "effort": 1.0, "velocity": 1.0},
            "drive": {"effort": 1.0, "velocity": 1.0},
            "origin_parent_child": np.eye(4).ravel().tolist(),
        }
        interface = {
            "name": "slide-line",
            "on_parent": "base_link",
            "on_child": "tool",
            "axis_point": [0.2, 0.0, 0.5],
            "axis_dir": [0.0, 0.0, 1.0],
        }
        for displacement in (0.5, -0.5):
            with self.subTest(displacement=displacement):
                result = evaluate_motion(
                    bodies=doc_bodies, joints=[doc_joint], interface=interface, poses={"slide": displacement}
                )
                actual = np.asarray(result["world"]["tool_part"], float)
                expected = tool.copy()
                expected[:3, 3] += np.array([0.0, 0.0, displacement])
                self.assertLessEqual(float(np.max(np.abs(actual - expected))), 1e-9)
                self.assertLessEqual(float(np.max(np.abs(actual[:3, :3] - tool[:3, :3]))), 1e-9)
        flipped = {**doc_joint, "stored_direction": [0.0, 0.0, -1.0]}
        with self.assertRaisesRegex(PipelineError, "anti-parallel"):
            evaluate_motion(bodies=doc_bodies, joints=[flipped], interface=interface, poses={"slide": 0.5})

    def test_product_gate_accepts_a_prismatic_joint(self):
        # The product bridge must translate the delivered child too; a bridge
        # that still rotated it would diverge by the rotation's off-axis arc.
        from description_pipeline.verification.solidworks_urdf import _motion

        base = np.eye(4)
        tool = np.eye(4)
        tool[:3, 3] = (0.2, 0.0, 0.5)
        datums = {"base_link": base, "tool": tool}
        components = {"base_part": base.copy(), "tool_part": tool.copy()}
        bodies = [
            {"name": "base_link", "components": ["base_part"]},
            {"name": "tool", "components": ["tool_part"]},
        ]
        authored = {
            "name": "slide",
            "type": "prismatic",
            "parent": "base_link",
            "child": "tool",
            "axis": [0.0, 0.0, 1.0],
            "limits": {"lower": -1.0, "upper": 1.0, "effort": 1.0, "velocity": 1.0},
            "axis_reference": {"component": "base_part", "feature_name": "Cyl1"},
        }
        record = {
            "component": "base_part",
            "surface": "cylinder",
            "body_type": "solid",
            "radius_m": 0.01,
            "selector": {"feature_name": "Cyl1", "component": "base_part", "body_type": "solid"},
            "coordinate_frame": "assembly",
            "axis_point_m": [0.2, 0.0, 0.5],
            "axis_direction": [0.0, 0.0, 1.0],
        }
        details = _motion(authored, [authored], components, datums, bodies, record)
        self.assertEqual([abs(value) for value in details["samples"]], [0.5, 0.5])
        self.assertLessEqual(details["worst_m"], 5e-5)
        self.assertIn("combined_worst_translation_m", details)
        self.assertEqual(details["combined_worst_translation_m"], 0.0)
        self.assertEqual(details["combined_worst_rotation_rad"], 0.0)

    def test_motion_takes_the_signed_axis_from_the_declaration_not_the_endpoint_order(self):
        # G06: an explicitly undirected native reference axis fixes a line, not
        # a direction - exchanging its endpoints must not change motion
        # validity; the signed convention is the delivered authored axis.
        from description_pipeline.verification.solidworks_urdf import _motion

        base = np.eye(4)
        tool = np.eye(4)
        tool[:3, 3] = (0.2, 0.0, 0.5)
        datums = {"base_link": base, "tool": tool}
        components = {"part0": base.copy(), "part1": tool.copy()}
        bodies = [
            {"name": "base_link", "components": ["part0"]},
            {"name": "tool", "components": ["part1"]},
        ]
        reference = {"kind": "reference_axis", "component": "part0", "feature_name": "Axis1"}
        authored = {
            "name": "hinge",
            "type": "revolute",
            "parent": "base_link",
            "child": "tool",
            "axis": [0.0, 0.0, -1.0],
            "limits": {"lower": -1.0, "upper": 1.0, "effort": 1.0, "velocity": 1.0},
            "axis_reference": reference,
        }

        def record(start, end):
            start = np.asarray(start, float)
            end = np.asarray(end, float)
            direction = (end - start) / np.linalg.norm(end - start)
            return {
                "kind": "reference_axis",
                "selector": reference,
                "component": "part0",
                "direction_semantics": "undirected_axis_line",
                "used_api": "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams",
                "coordinate_frame": "assembly",
                "raw_endpoints_m": [*start.tolist(), *end.tolist()],
                "axis_point_m": start.tolist(),
                "axis_direction": direction.tolist(),
            }

        forward = record((0.2, 0.0, 0.5), (0.2, 0.0, 1.5))
        reversed_endpoints = record((0.2, 0.0, 1.5), (0.2, 0.0, 0.5))
        results = [
            _motion(authored, [authored], components, datums, bodies, entry)
            for entry in (forward, reversed_endpoints)
        ]
        for entry in results:
            self.assertEqual(entry["worst_m"], 0.0)
            self.assertEqual(entry["worst_translation_m"], 0.0)
            self.assertEqual(entry["worst_rotation_rad"], 0.0)
        self.assertLess(
            float(np.dot(forward["axis_direction"], reversed_endpoints["axis_direction"])), 0
        )


if __name__ == "__main__":
    unittest.main()
