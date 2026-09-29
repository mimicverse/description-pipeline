"""两个 STL 解析器的边界：ASCII/二进制、损坏输入、退化面、包围盒与哈希。"""

import hashlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from onshape_export import stl as onshape_stl  # noqa: E402
from urdf_quality import stl as quality_stl  # noqa: E402


def binary_stl(faces, *, declared: int | None = None) -> bytes:
    count = len(faces) if declared is None else declared
    out = bytearray(b"\0" * 80 + struct.pack("<I", count))
    for triangle in faces:
        out += struct.pack("<3f", 0.0, 0.0, 1.0)
        for corner in triangle:
            out += struct.pack("<3f", *corner)
        out += b"\0\0"
    return bytes(out)


def ascii_stl(faces) -> bytes:
    lines = ["solid fixture"]
    for triangle in faces:
        lines.append("facet normal 0 0 1")
        lines.append("  outer loop")
        for corner in triangle:
            lines.append(f"    vertex {corner[0]} {corner[1]} {corner[2]}")
        lines.append("  endloop")
        lines.append("endfacet")
    lines.append("endsolid fixture")
    return ("\n".join(lines) + "\n").encode("ascii")


TRIANGLE = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
TETRA = (
    ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
    ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
)


class QualityStlTests(unittest.TestCase):
    def test_binary_tetrahedron_volume_and_bounds(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tetra.stl"
            path.write_bytes(binary_stl(TETRA))
            stats = quality_stl.read(path)
        self.assertEqual(stats.triangles, 4)
        self.assertTrue(stats.binary)
        self.assertAlmostEqual(stats.volume, 1.0 / 6.0, places=9)
        self.assertEqual(stats.low, (0.0, 0.0, 0.0))
        self.assertEqual(stats.high, (1.0, 1.0, 1.0))
        self.assertEqual(stats.extent, (1.0, 1.0, 1.0))
        self.assertAlmostEqual(stats.half_diagonal, 3**0.5 / 2, places=9)
        self.assertEqual(stats.center, (0.5, 0.5, 0.5))
        self.assertEqual(stats.degenerate, 0)

    def test_ascii_tetrahedron_matches_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tetra-ascii.stl"
            path.write_bytes(ascii_stl(TETRA))
            stats = quality_stl.read(path)
        self.assertFalse(stats.binary)
        self.assertEqual(stats.triangles, 4)
        self.assertAlmostEqual(stats.volume, 1.0 / 6.0, places=9)

    def test_degenerate_and_open_meshes_are_flagged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "open.stl"
            path.write_bytes(binary_stl([TRIANGLE]))
            stats = quality_stl.read(path)
        self.assertGreater(stats.boundary_edges, 0, "单个三角面必然有开边")
        self.assertEqual(stats.volume, 0.0)

    def test_error_paths_are_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cases = {
                "short.stl": b"abc",
                "empty.stl": binary_stl([], declared=0),
                "broken.stl": b"solid broken\nfacet normal 0 0 1\n",
                "garbage-vertices.stl": ascii_stl([TRIANGLE]).replace(b"vertex 0.0", b"vertex x", 1),
                "nonfinite.stl": binary_stl([((float("nan"), 0.0, 0.0), *TRIANGLE[1:])]),
            }
            for name, payload in cases.items():
                with self.subTest(name=name):
                    path = root / name
                    path.write_bytes(payload)
                    with self.assertRaises(quality_stl.StlError):
                        quality_stl.read(path)
            with self.assertRaises(quality_stl.StlError):
                quality_stl.read(root / "missing.stl")

    def test_ascii_with_partial_triangle_is_rejected(self):
        # 删掉一个顶点：顶点数不再是 3 的倍数，必须报错而不是静默算出半个三角面
        payload = ascii_stl([TRIANGLE]).replace(b"    vertex 0.0 1.0 0.0\n", b"", 1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "partial.stl"
            path.write_bytes(payload)
            with self.assertRaises(quality_stl.StlError):
                quality_stl.read(path)


class OnshapeStlTests(unittest.TestCase):
    def test_ascii_and_binary_agree(self):
        binary = onshape_stl.bounds_bytes(binary_stl(TETRA))
        ascii_box = onshape_stl.bounds_bytes(ascii_stl(TETRA))
        self.assertEqual(binary["format"], "binary")
        self.assertEqual(ascii_box["format"], "ascii")
        self.assertEqual(binary["triangles"], ascii_box["triangles"] == 4 and binary["triangles"])
        self.assertEqual(binary["min"], ascii_box["min"])
        self.assertEqual(binary["max"], ascii_box["max"])

    def test_extent_and_sha256(self):
        box = onshape_stl.bounds_bytes(binary_stl(TETRA))
        self.assertEqual(onshape_stl.extent(box), (1.0, 1.0, 1.0))
        empty = onshape_stl.bounds_bytes(binary_stl([TRIANGLE]))
        self.assertEqual(onshape_stl.extent({"max": [0, 0, 0], "min": [0, 0, 0], "triangles": 0}), (0.0, 0.0, 0.0))
        self.assertEqual(onshape_stl.extent(empty), (1.0, 1.0, 0.0))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tetra.stl"
            path.write_bytes(binary_stl(TETRA))
            self.assertEqual(onshape_stl.sha256(path), hashlib.sha256(path.read_bytes()).hexdigest())

    def test_invalid_bytes_report_a_value_error(self):
        with self.assertRaises(ValueError) as caught:
            onshape_stl.bounds_bytes(b"nope")
        self.assertIn("STL", str(caught.exception))

    def test_degenerate_triangles_are_counted(self):
        box = onshape_stl.bounds_bytes(binary_stl([TRIANGLE, (TRIANGLE[0], TRIANGLE[0], TRIANGLE[0])]))
        self.assertEqual(box["degenerate_triangles"], 1)


if __name__ == "__main__":
    unittest.main()
