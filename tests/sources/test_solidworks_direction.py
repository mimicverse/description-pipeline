"""Versioned positive-motion rules for CAD joint axes."""

from __future__ import annotations

import unittest

from description_pipeline.sources.solidworks.direction import canonicalize_axis


IDENTITY = [
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
]


class DirectionTests(unittest.TestCase):
    def test_named_pitch_is_locally_aligned_without_rewriting_the_authored_sign(self):
        right = canonicalize_axis("r_shoulder_pitch", [0, 1, 0], IDENTITY, IDENTITY, joint_type="revolute")
        left = canonicalize_axis("l_shoulder_pitch", [0, -1, 0], IDENTITY, IDENTITY, joint_type="revolute")
        self.assertEqual(right["motion"], "pitch")
        self.assertEqual(right["parent_axis"], [0.0, 1.0, 0.0])
        self.assertEqual(left["motion"], "pitch")
        self.assertEqual(left["parent_axis"], [0.0, -1.0, 0.0])
        self.assertEqual(left["delivered_local_axis"], [0.0, -1.0, 0.0])

    def test_unnamed_axis_uses_only_a_confident_geometric_class(self):
        result = canonicalize_axis("waist", [0.6, 0.8, 0], IDENTITY, IDENTITY, joint_type="revolute")
        self.assertEqual(result["status"], "unverified")
        self.assertIsNone(result["motion"])
        self.assertEqual(result["local_principal"], "oblique")
        self.assertEqual(result["parent_axis"], [0.6, 0.8, 0.0])

    def test_explicit_named_oblique_axis_keeps_its_real_direction(self):
        result = canonicalize_axis("r_test_pitch", [0, 0.6, 0.8], IDENTITY, IDENTITY, joint_type="revolute")
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["motion"], "pitch")
        self.assertAlmostEqual(result["parent_axis"][1], 0.6)
        self.assertAlmostEqual(result["parent_axis"][2], 0.8)

    def test_a_named_axis_that_is_orthogonal_to_its_class_fails_closed(self):
        result = canonicalize_axis("r_wrist_pitch", [1, 0, 0], IDENTITY, IDENTITY, joint_type="revolute")
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["motion"], "pitch")
        self.assertEqual(result["delivered_local_axis"], [1.0, 0.0, 0.0])

    def test_non_rigid_reference_frame_is_rejected(self):
        invalid = list(IDENTITY)
        invalid[0] = 2.0
        with self.assertRaisesRegex(ValueError, "orthonormal"):
            canonicalize_axis("waist", [0, 1, 0], IDENTITY, invalid, joint_type="revolute")


if __name__ == "__main__":
    unittest.main()
