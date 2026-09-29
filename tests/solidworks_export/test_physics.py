import unittest

from tools.solidworks_export import physics


class PhysicsTests(unittest.TestCase):
    def test_inertia_rotation_quarter_turn(self):
        inertia = ((1.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 3.0))
        rot = ((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0))
        rotated = physics.inertia_rotate(inertia, rot)
        self.assertAlmostEqual(rotated[0][0], 2.0, places=12)
        self.assertAlmostEqual(rotated[1][1], 1.0, places=12)
        self.assertAlmostEqual(rotated[2][2], 3.0, places=12)
        self.assertAlmostEqual(rotated[0][1], 0.0, places=12)

    def test_combine_two_point_masses(self):
        entries = [
            {"mass": 1.0, "com": (0.0, 0.0, 0.0), "inertia": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))},
            {"mass": 1.0, "com": (1.0, 0.0, 0.0), "inertia": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))},
        ]
        combined = physics.combine_mass_properties(entries)
        self.assertAlmostEqual(combined["mass"], 2.0, places=12)
        self.assertAlmostEqual(combined["com"][0], 0.5, places=12)
        self.assertAlmostEqual(combined["inertia"][0][0], 0.0, places=12)
        self.assertAlmostEqual(combined["inertia"][1][1], 0.5, places=12)
        self.assertAlmostEqual(combined["inertia"][2][2], 0.5, places=12)

    def test_principal_moments_of_diagonal(self):
        values = physics.principal_moments(((0.3, 0.0, 0.0), (0.0, 0.1, 0.0), (0.0, 0.0, 0.2)))
        self.assertAlmostEqual(values[0], 0.1, places=12)
        self.assertAlmostEqual(values[1], 0.2, places=12)
        self.assertAlmostEqual(values[2], 0.3, places=12)

    def test_physics_conditions(self):
        valid = physics.physics_conditions(((1.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 3.0)))
        self.assertTrue(valid["positive_definite"])
        self.assertTrue(valid["triangle_inequalities"])
        self.assertAlmostEqual(valid["triangle_slack"], 0.0, places=12)

        invalid = physics.physics_conditions(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 3.0)))
        self.assertFalse(invalid["triangle_inequalities"])

        not_positive = physics.physics_conditions(((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
        self.assertFalse(not_positive["positive_definite"])

    def test_inertia6_order(self):
        values = physics.inertia6_of(((1.0, 2.0, 3.0), (2.0, 4.0, 5.0), (3.0, 5.0, 6.0)))
        self.assertEqual(values, (1.0, 2.0, 3.0, 4.0, 5.0, 6.0))


if __name__ == "__main__":
    unittest.main()
