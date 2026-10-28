"""Independent oracle tests for the generic point-on-axis concentric reconstruction.

A SolidWorks Concentric relation may join a bare recorded point (a vertex with no
direction of its own) to one axis entity (a cylindrical face or a circular edge).  These
tests derive the expected verifier rows from the geometry itself:

* exactly two rows ``[n, P x n]`` for two independent directions ``n`` spanning the plane
  orthogonal to the axis, evaluated at the recorded point ``P`` (non-zero offsets along
  the axis must appear in the moment arms);
* the rows reproduce a finite difference of the true point-on-line residual
  ``(I - d d^T)(P - o)`` when either side moves under an arbitrary twist;
* the solved-state gate uses the point-axis distance and refuses anything beyond it,
  while malformed, direction-bearing or axis-less pairs stay unsupported;
* no shaft/axis evidence and no ground binding are produced.

No names, coordinates or expected numbers are taken from the source implementation.
"""

from __future__ import annotations

import math
import unittest

from description_pipeline.verification import native_discovery as V


def cross(left, right):
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def dot(left, right):
    return sum(first * second for first, second in zip(left, right, strict=True))


def unit(values):
    return [value / math.sqrt(dot(values, values)) for value in values]


def rotation(axis, angle):
    x, y, z = unit(axis)
    cos, sin = math.cos(angle), math.sin(angle)
    return [
        [cos + x * x * (1 - cos), x * y * (1 - cos) - z * sin, x * z * (1 - cos) + y * sin],
        [y * x * (1 - cos) + z * sin, cos + y * y * (1 - cos), y * z * (1 - cos) - x * sin],
        [z * x * (1 - cos) - y * sin, z * y * (1 - cos) + x * sin, cos + z * z * (1 - cos)],
    ]


def frame(rotation_matrix, translation):
    return [
        [rotation_matrix[0][0], rotation_matrix[0][1], rotation_matrix[0][2], translation[0]],
        [rotation_matrix[1][0], rotation_matrix[1][1], rotation_matrix[1][2], translation[1]],
        [rotation_matrix[2][0], rotation_matrix[2][1], rotation_matrix[2][2], translation[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def to_local_point(matrix, world):
    shifted = [world[index] - matrix[index][3] for index in range(3)]
    return [
        matrix[0][0] * shifted[0] + matrix[1][0] * shifted[1] + matrix[2][0] * shifted[2],
        matrix[0][1] * shifted[0] + matrix[1][1] * shifted[1] + matrix[2][1] * shifted[2],
        matrix[0][2] * shifted[0] + matrix[1][2] * shifted[1] + matrix[2][2] * shifted[2],
    ]


def to_local_vector(matrix, world):
    return [
        matrix[0][0] * world[0] + matrix[1][0] * world[1] + matrix[2][0] * world[2],
        matrix[0][1] * world[0] + matrix[1][1] * world[1] + matrix[2][1] * world[2],
        matrix[0][2] * world[0] + matrix[1][2] * world[1] + matrix[2][2] * world[2],
    ]


def radial_residual(point, origin, direction):
    delta = [point[index] - origin[index] for index in range(3)]
    along = dot(delta, direction)
    return [delta[index] - along * direction[index] for index in range(3)]


class PointOnAxisConcentricTests(unittest.TestCase):
    def fixture(self, *, offset=0.0, order="cylinder-first"):
        axis_direction = unit([1.0, 2.0, -2.0])
        axis_point = [0.2, -0.1, 0.05]
        along = 0.35
        on_axis = [axis_point[index] + along * axis_direction[index] for index in range(3)]
        perpendicular = unit(cross(axis_direction, [0.3, -0.7, 0.2]))
        point_world = [on_axis[index] + offset * perpendicular[index] for index in range(3)]
        frame_a = frame(rotation([0.1, 0.2, 0.9], 0.6), [0.4, -0.2, 0.15])
        frame_b = frame(rotation([0.7, -0.3, 0.2], -0.35), [-0.1, 0.3, 0.05])
        frames = {"body-a": frame_a, "body-b": frame_b}
        cylinder = {
            "component": "body-a",
            "cylinder": {
                "point": to_local_point(frame_a, axis_point),
                "direction": to_local_vector(frame_a, axis_direction),
                "radius": 0.012,
            },
        }
        point = {"component": "body-b", "point": to_local_point(frame_b, point_world)}
        entities = [cylinder, point] if order == "cylinder-first" else [point, cylinder]
        mate = {
            "name": "concentric_point__axis",
            "type": "concentric",
            "entity_count": 2,
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": entities,
        }
        return mate, frames, point_world, axis_point, axis_direction

    def test_rows_are_point_on_line_rows_at_the_recorded_point(self):
        mate, frames, point, _origin, direction = self.fixture()
        result = V._rows_for(mate, frames)
        self.assertIsNotNone(result)
        # No shaft/joint identity and no ground binding (contract): the point relation
        # carries exactly the two rows.
        self.assertIsNone(result["axis"])
        self.assertIsNone(result["point"])
        rows = result["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(V._span(rows)), 2)
        for row in rows:
            normal = row[0:3]
            self.assertAlmostEqual(dot(normal, direction), 0.0, places=12)
            expected_moment = cross(point, normal)
            for left, right in zip(row[3:6], expected_moment, strict=True):
                self.assertAlmostEqual(left, right, places=9)

    def test_entity_order_does_not_change_the_row_space(self):
        first_order, frames, *_ = self.fixture(order="cylinder-first")
        second_order, *_ = self.fixture(order="point-first")
        rows_first = V._rows_for(first_order, frames)["rows"]
        rows_second = V._rows_for(second_order, frames)["rows"]
        self.assertEqual(len(V._span(rows_first + rows_second)), 2)

    def test_rows_match_a_finite_difference_of_the_true_residual(self):
        for order in ("cylinder-first", "point-first"):
            with self.subTest(order=order):
                mate, frames, point, origin, direction = self.fixture(order=order)
                rows = V._rows_for(mate, frames)["rows"]
                translation = [0.13, -0.07, 0.09]
                omega = [0.05, 0.12, -0.03]
                epsilon = 1e-7
                omega_norm = math.sqrt(dot(omega, omega))
                turn = rotation(omega, epsilon * omega_norm)
                before = {}
                after = {}
                for row in rows:
                    normal = row[0:3]
                    baseline = radial_residual(point, origin, direction)
                    before[tuple(row)] = dot(normal, baseline)
                    if order == "cylinder-first":
                        moved_point = [
                            epsilon * translation[index]
                            + sum(turn[index][column] * point[column] for column in range(3))
                            for index in range(3)
                        ]
                        moved_origin, moved_direction = origin, direction
                    else:
                        moved_point = point
                        moved_origin = [
                            epsilon * translation[index]
                            + sum(turn[index][column] * origin[column] for column in range(3))
                            for index in range(3)
                        ]
                        moved_direction = [
                            sum(turn[index][column] * direction[column] for column in range(3))
                            for index in range(3)
                        ]
                    changed = radial_residual(moved_point, moved_origin, moved_direction)
                    after[tuple(row)] = dot(normal, changed)
                for row in rows:
                    finite = (after[tuple(row)] - before[tuple(row)]) / epsilon
                    expected = dot(row[0:3], translation) + dot(row[3:6], omega)
                    direct = abs(finite - expected)
                    flipped = abs(finite + expected)
                    self.assertLess(min(direct, flipped), 1e-6)

    def test_solved_state_gate_uses_the_point_axis_distance(self):
        inside, frames, *_ = self.fixture(offset=4.9e-5)
        self.assertIsNotNone(V._rows_for(inside, frames))
        outside, frames, *_ = self.fixture(offset=5.1e-5)
        self.assertIsNone(V._rows_for(outside, frames))

    def test_unsupported_and_malformed_pairs_stay_unsupported(self):
        mate, frames, *_ = self.fixture()
        cylinder_entity = mate["entities"][0]
        point_entity = mate["entities"][1]
        extra_frames = dict(frames)
        extra_frames["body-c"] = frames["body-b"]
        point_point = {
            "name": "point__point",
            "type": "concentric",
            "entity_count": 2,
            "limits": None,
            "entities": [dict(point_entity), {"component": "body-c", "point": [0.0, 0.0, 0.0]}],
        }
        self.assertIsNone(V._rows_for(point_point, extra_frames))
        plane_entity = {
            "component": "body-a",
            "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
        }
        plane_point = {
            "name": "plane__point",
            "type": "concentric",
            "entity_count": 2,
            "limits": None,
            "entities": [plane_entity, dict(point_entity)],
        }
        self.assertIsNone(V._rows_for(plane_point, frames))
        malformed_point = {
            "name": "cylinder__malformed",
            "type": "concentric",
            "entity_count": 2,
            "limits": None,
            "entities": [dict(cylinder_entity), {"component": "body-b", "point": [0.0, 0.0]}],
        }
        self.assertIsNone(V._rows_for(malformed_point, frames))
        missing_frame = {
            "name": "cylinder__missing-frame",
            "type": "concentric",
            "entity_count": 2,
            "limits": None,
            "entities": [dict(cylinder_entity), {"component": "nowhere", "point": [0.0, 0.0, 0.0]}],
        }
        self.assertIsNone(V._rows_for(missing_frame, frames))


if __name__ == "__main__":
    unittest.main()
