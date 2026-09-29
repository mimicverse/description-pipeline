import math
import unittest

from tools.solidworks_export import transform as tf


class TransformTests(unittest.TestCase):
    def test_identity_multiply(self):
        self.assertTrue(tf.approx_equal(tf.matmul(tf.identity(), tf.identity()), tf.identity()))

    def test_inverse_of_rigid_transform(self):
        m = tf.from_xyz_rpy((1.0, -2.0, 0.5), (0.3, -0.2, 1.1))
        product = tf.matmul(m, tf.inverse_rigid(m))
        self.assertTrue(tf.approx_equal(product, tf.identity(), tol=1e-12))

    def test_rpy_roundtrip(self):
        for rpy in ((0.1, 0.2, 0.3), (-0.5, 0.9, -1.2), (0.0, 0.0, 0.0)):
            matrix = tf.from_xyz_rpy((0, 0, 0), rpy)
            back = tf.xyz_rpy_of(matrix)[1]
            rebuilt = tf.rotation_3x3(back)
            original = tf.rotation_3x3(rpy)
            for i in range(3):
                for j in range(3):
                    self.assertAlmostEqual(rebuilt[i][j], original[i][j], places=12)

    def test_gimbal_lock(self):
        matrix = tf.from_xyz_rpy((0, 0, 0), (0.7, math.pi / 2, 0.0))
        rpy = tf.xyz_rpy_of(matrix)[1]
        rebuilt = tf.rotation_3x3(rpy)
        original = tf.rotation_3x3((0.7, math.pi / 2, 0.0))
        for i in range(3):
            for j in range(3):
                self.assertAlmostEqual(rebuilt[i][j], original[i][j], places=12)

    def test_between_uses_parent_inverse(self):
        parent = tf.from_xyz_rpy((1.0, 0.0, 0.0), (0.0, 0.0, math.pi / 2))
        child = tf.from_xyz_rpy((1.0, 1.0, 0.0), (0.0, 0.0, math.pi / 2))
        relative = tf.between(parent, child)
        xyz, rpy = tf.xyz_rpy_of(relative)
        # parent's +X axis points along world +Y, so the world offset (0, 1, 0)
        # is local (1, 0, 0)
        self.assertAlmostEqual(xyz[0], 1.0, places=12)
        self.assertAlmostEqual(xyz[1], 0.0, places=12)
        self.assertAlmostEqual(xyz[2], 0.0, places=12)
        for angle in rpy:
            self.assertAlmostEqual(angle, 0.0, places=12)

    def test_flatten_accepts_nested_and_flat(self):
        nested = ((1, 0, 0, 5), (0, 1, 0, 6), (0, 0, 1, 7), (0, 0, 0, 1))
        flat = (1, 0, 0, 5, 0, 1, 0, 6, 0, 0, 1, 7, 0, 0, 0, 1)
        self.assertEqual(tf.flatten(nested), tf.flatten(flat))
        with self.assertRaises(ValueError):
            tf.flatten([1.0, 2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
