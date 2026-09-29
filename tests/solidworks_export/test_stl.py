import os
import struct
import tempfile
import unittest

from tools.solidworks_export.backends import _write_triangle_stl
from tools.solidworks_export.stl import count_triangles, merge_binary_stl, validate_binary_stl
from tools.solidworks_export.transform import from_xyz_rpy


def _vertices(path):
    with open(path, "rb") as handle:
        handle.seek(84)
        data = handle.read()
    points = []
    for offset in range(0, len(data), 50):
        record = data[offset : offset + 50]
        for vertex_offset in (12, 24, 36):
            points.append(struct.unpack_from("<3f", record, vertex_offset))
    return points


def _facets(path):
    with open(path, "rb") as handle:
        handle.seek(84)
        data = handle.read()
    facets = []
    for offset in range(0, len(data), 50):
        record = data[offset : offset + 50]
        normal = struct.unpack_from("<3f", record, 0)
        v0 = struct.unpack_from("<3f", record, 12)
        v1 = struct.unpack_from("<3f", record, 24)
        v2 = struct.unpack_from("<3f", record, 36)
        facets.append((normal, v0, v1, v2))
    return facets


def _key(point):
    return tuple(round(value, 6) for value in point)


class StlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swbridge-stl-")

    def test_merge_two_triangles(self):
        first = os.path.join(self.tmp, "a.stl")
        second = os.path.join(self.tmp, "b.stl")
        merged = os.path.join(self.tmp, "merged.stl")
        _write_triangle_stl(first, 0.0)
        _write_triangle_stl(second, 0.1)
        self.assertEqual(count_triangles(first), 1)
        total = merge_binary_stl([first, second], merged, source_note="test")
        self.assertEqual(total, 2)
        self.assertEqual(validate_binary_stl(merged), 2)
        expected_size = 80 + 4 + 2 * 50
        self.assertEqual(os.path.getsize(merged), expected_size)

    def test_corrupt_file_rejected(self):
        bad = os.path.join(self.tmp, "bad.stl")
        with open(bad, "wb") as handle:
            handle.write(b"x" * 90)
        with self.assertRaises(ValueError):
            validate_binary_stl(bad)

    def test_empty_merge_rejected(self):
        with self.assertRaises(ValueError):
            merge_binary_stl([], os.path.join(self.tmp, "out.stl"))

    def test_merge_applies_transforms(self):
        first = os.path.join(self.tmp, "a.stl")
        second = os.path.join(self.tmp, "b.stl")
        merged = os.path.join(self.tmp, "merged.stl")
        _write_triangle_stl(first, 0.0)
        _write_triangle_stl(second, 0.0)
        # rotate 90 deg about Z, then translate by (1, 0, 0)
        transform = from_xyz_rpy((1.0, 0.0, 0.0), (0.0, 0.0, 1.5707963267948966))
        merge_binary_stl([first, second], merged, transforms=[transform, None])
        points = _vertices(merged)[:3]
        for index, expected in enumerate(((1.0, 0.0, 0.0), (1.0, 0.01, 0.0), (0.99, 0.0, 0.0))):
            for axis in range(3):
                self.assertAlmostEqual(points[index][axis], expected[axis], places=5)

    def test_fake_backend_mesh_is_closed(self):
        from tools.solidworks_export.backends import FakeBackend

        path = os.path.join(self.tmp, "cube.stl")
        FakeBackend().export_component_mesh("Base-1", path)
        facets = _facets(path)
        self.assertEqual(len(facets), 12)
        edge_counts: dict = {}
        volume = 0.0
        for _normal, v0, v1, v2 in facets:
            for a, b in ((v0, v1), (v1, v2), (v2, v0)):
                key = tuple(sorted((_key(a), _key(b))))
                edge_counts[key] = edge_counts.get(key, 0) + 1
            volume += (
                v0[0] * (v1[1] * v2[2] - v1[2] * v2[1])
                - v0[1] * (v1[0] * v2[2] - v1[2] * v2[0])
                + v0[2] * (v1[0] * v2[1] - v1[1] * v2[0])
            ) / 6.0
        self.assertTrue(
            all(count == 2 for count in edge_counts.values()), "every edge must be shared by exactly two triangles"
        )
        self.assertGreater(volume, 0.0, "outward winding must give positive volume")

    def test_aabb_and_signed_volume_helpers(self):
        from tools.solidworks_export.backends import FakeBackend
        from tools.solidworks_export.stl import aabb, iter_triangles, signed_volume

        path = os.path.join(self.tmp, "cube.stl")
        FakeBackend().export_component_mesh("Base-1", path)
        self.assertEqual(len(list(iter_triangles(path))), 12)
        mins, maxs = aabb(path)
        self.assertAlmostEqual(maxs[0] - mins[0], 0.1, places=6)
        self.assertAlmostEqual(maxs[1] - mins[1], 0.1, places=6)
        self.assertAlmostEqual(maxs[2] - mins[2], 0.1, places=6)
        self.assertAlmostEqual(mins[2], -0.02, places=6)
        self.assertAlmostEqual(maxs[2], 0.08, places=6)
        self.assertAlmostEqual(signed_volume(path), 0.001, places=9)


if __name__ == "__main__":
    unittest.main()
