"""Strict planar BRep qualification and triangulation tests."""

from __future__ import annotations

from fractions import Fraction
import math
import struct
import unittest

from . import _paths  # noqa: F401

from description_pipeline.geometry.native_planar_brep import (
    PlanarBrepEvidenceError,
    PlanarBrepNotApplicable,
    _orientation3,
    build_planar_convex_triangulation,
)
from description_pipeline.geometry.native_u import (
    native_binary64_sqrt,
    native_binary64_sum_upper,
    native_binary64_unit_vector,
)


def _square(axis: int, sign: int):
    points = [(0.0, 0.0, 0.0) for _ in range(4)]
    points[0] = [0.0, 0.0, 0.0]
    points[1] = [0.0, 0.0, 0.0]
    points[2] = [0.0, 0.0, 0.0]
    points[3] = [0.0, 0.0, 0.0]
    first = (axis + 1) % 3
    second = (axis + 2) % 3
    points[1][first] = 1.0
    points[2][first] = 1.0
    points[2][second] = 1.0
    points[3][second] = 1.0
    if sign < 0:
        points = [points[0], points[3], points[2], points[1]]
    return points


class NativePlanarBrepTests(unittest.TestCase):
    def test_binary64_rounding_is_exact_for_subnormals_and_midpoint_ties(self):
        smallest = math.ulp(0.0)
        self.assertEqual(native_binary64_sqrt(Fraction.from_float(smallest) ** 2), smallest)
        lower = Fraction(1)
        upper = Fraction.from_float(math.nextafter(1.0, math.inf))
        midpoint = (lower + upper) / 2
        self.assertEqual(native_binary64_sqrt(midpoint**2), 1.0)
        self.assertEqual(native_binary64_sqrt(midpoint**2, upward=True), float(upper))
        self.assertEqual(native_binary64_sum_upper([1.0, 2**-53]), float(upper))
        self.assertEqual(native_binary64_unit_vector([smallest, 0.0, 0.0]), (1.0, 0.0, 0.0))

    def test_combined_displacements_use_triangle_inequality(self):
        offset = 1024.0 + 3.0e-5
        points = [[offset, 0.0, 0.0], [offset + 1, 0.0, 0.0], [offset + 1, 1.0, 0.0], [offset, 1.0, 0.0]]
        with self.assertRaisesRegex(PlanarBrepEvidenceError, "complete surface bound"):
            build_planar_convex_triangulation(
                [0.0, 0.0, 1.0],
                points[0],
                points,
                source_displacements=[([0.0, 0.0, 0.0], [3e-5, 0.0, 0.0])],
            )

    def test_rounded_sloped_boundary_is_a_provider_miss(self):
        # Independent binary64 API results need not be rationally incident.
        normal = [0.6, 0.8, 0.0]
        point = [-0.8 * 0.3, 0.6 * 0.3, 0.0]
        with self.assertRaisesRegex(PlanarBrepNotApplicable, "exact-plane"):
            build_planar_convex_triangulation(
                normal, [0.0, 0.0, 0.0], [[0.0, 0.0, 0.0], point, [*point[:2], 1.0], [0.0, 0.0, 1.0]]
            )

    def test_all_axis_directions_preserve_face_orientation(self):
        for axis in range(3):
            for sign in (1, -1):
                with self.subTest(axis=axis, sign=sign):
                    normal = [0.0, 0.0, 0.0]
                    normal[axis] = float(sign)
                    result = build_planar_convex_triangulation(
                        normal,
                        [0.0, 0.0, 0.0],
                        _square(axis, sign),
                    )

                    self.assertEqual(len(result["triangles"]), 2)
                    for triangle in result["triangles"]:
                        self.assertGreater(
                            _orientation3(
                                *triangle["stl_points"],
                                normal,
                            ),
                            0,
                        )

    def test_reversed_boundary_is_rejected(self):
        with self.assertRaisesRegex(
            PlanarBrepEvidenceError,
            "contradicts face orientation",
        ):
            build_planar_convex_triangulation(
                [0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0],
                _square(2, -1),
            )

    def test_concave_polygon_is_rejected(self):
        with self.assertRaisesRegex(
            PlanarBrepNotApplicable,
            "strictly convex",
        ):
            build_planar_convex_triangulation(
                [0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0],
                [
                    [0.0, 0.0, 0.0],
                    [2.0, 0.0, 0.0],
                    [2.0, 1.0, 0.0],
                    [1.0, 0.5, 0.0],
                    [0.0, 1.0, 0.0],
                ],
            )

    def test_duplicate_vertex_is_rejected(self):
        with self.assertRaisesRegex(PlanarBrepEvidenceError, "repeats"):
            build_planar_convex_triangulation(
                [0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0],
                [
                    [0.0, 0.0, 0.0],
                    [1.0, 0.0, 0.0],
                    [0.0, 0.0, 0.0],
                ],
            )

    def test_binary32_triangle_collapse_is_rejected(self):
        base = 1.0e8
        with self.assertRaisesRegex(PlanarBrepEvidenceError, "binary32"):
            build_planar_convex_triangulation(
                [0.0, 0.0, 1.0],
                [base, base, 0.0],
                [
                    [base, base, 0.0],
                    [base + 1.0e-8, base, 0.0],
                    [base, base + 1.0e-8, 0.0],
                ],
            )

    def test_binary32_overflow_is_evidence_error(self):
        with self.assertRaisesRegex(
            PlanarBrepEvidenceError,
            "outside binary32",
        ):
            build_planar_convex_triangulation(
                [0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0],
                [
                    [0.0, 0.0, 0.0],
                    [1.0e100, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                ],
            )

    def test_nonplanar_native_vertex_is_outside_exact_provider_domain(self):
        with self.assertRaisesRegex(
            PlanarBrepNotApplicable,
            "exact-plane provider domain",
        ):
            build_planar_convex_triangulation(
                [0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0],
                [
                    [0.0, 0.0, 0.0],
                    [1.0, 0.0, 0.0],
                    [1.0, 1.0, 0.0001],
                    [0.0, 1.0, 0.0],
                ],
            )

    def test_binary32_displacement_bound_is_recorded(self):
        result = build_planar_convex_triangulation(
            [0.0, 0.0, 1.0],
            [0.0, 0.0, 0.0],
            _square(2, 1),
        )

        self.assertEqual(
            result["error_components_m"]["native_plane_residual"],
            0.0,
        )
        self.assertEqual(
            result["error_components_m"]["constructive_vertex_displacement"],
            0.0,
        )
        self.assertEqual(
            result["error_components_m"]["cross_api_vertex_displacement"],
            0.0,
        )
        self.assertEqual(
            result["error_components_m"]["binary32_vertex_displacement"],
            0.0,
        )
        self.assertEqual(result["boundary_area_m2"], 1.0)
        self.assertLessEqual(
            result["surface_error_upper_bound_m"],
            result["surface_error_limit_m"],
        )

    def test_cross_api_vertex_displacement_is_in_complete_bound(self):
        points = _square(2, 1)
        result = build_planar_convex_triangulation(
            [0.0, 0.0, 1.0],
            [0.0, 0.0, 0.0],
            points,
            source_displacements=[
                (points[0], [3.0e-6, 0.0, 0.0]),
            ],
        )

        self.assertGreaterEqual(
            result["error_components_m"]["cross_api_vertex_displacement"],
            3.0e-6,
        )
        self.assertGreaterEqual(
            result["surface_error_upper_bound_m"],
            result["error_components_m"]["cross_api_vertex_displacement"],
        )

    def test_sloped_plane_area_and_orientation(self):
        points = [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 1.0],
            [1.0, 1.0, 2.0],
            [0.0, 1.0, 1.0],
        ]
        result = build_planar_convex_triangulation(
            [-1.0, -1.0, 1.0],
            points[0],
            points,
        )

        self.assertAlmostEqual(
            result["boundary_area_m2"],
            math.sqrt(3.0),
            places=15,
        )
        for triangle in result["triangles"]:
            self.assertGreater(
                _orientation3(
                    *triangle["stl_points"],
                    result["face_normal"],
                ),
                0,
            )

    def test_binary32_bound_is_outward_rounded(self):
        points = [
            [0.0, 0.0, 0.0],
            [0.1, 0.0, 0.0],
            [0.1, 0.2, 0.0],
            [0.0, 0.2, 0.0],
        ]
        result = build_planar_convex_triangulation(
            [0.0, 0.0, 1.0],
            [0.0, 0.0, 0.0],
            points,
        )

        reported = Fraction.from_float(result["error_components_m"]["binary32_vertex_displacement"])
        actual_squared = max(
            sum(
                (Fraction.from_float(encoded) - Fraction.from_float(native)) ** 2
                for encoded, native in zip(
                    struct.unpack("<3f", struct.pack("<3f", *point)),
                    point,
                    strict=True,
                )
            )
            for point in points
        )
        self.assertGreater(actual_squared, 0)
        self.assertGreaterEqual(reported**2, actual_squared)
        self.assertEqual(
            result["surface_error_upper_bound_m"],
            result["error_components_m"]["binary32_vertex_displacement"],
        )

    def test_surface_budget_cannot_expand_exact_plane_domain(self):
        with self.assertRaisesRegex(
            PlanarBrepNotApplicable,
            "exact-plane provider domain",
        ):
            build_planar_convex_triangulation(
                [0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0],
                [
                    [0.0, 0.0, 4.0e-5],
                    [1.0, 0.0, 4.0e-5],
                    [1.0, 1.0, 4.0e-5],
                    [0.0, 1.0, 4.0e-5],
                ],
            )


if __name__ == "__main__":
    unittest.main()
