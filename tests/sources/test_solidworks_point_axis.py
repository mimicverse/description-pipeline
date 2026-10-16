"""Analytic point-on-axis concentric reconstruction (source contract only).

A concentric mate between one recorded cylinder/circular-edge axis and one pure
point reference is a point-on-axis relation: the solved state must already place
the point on the axis, and exactly two translation rows at the recorded point
remove the perpendicular freedoms — never the four-row axis alignment, never an
invented shaft selector, and never an axis inferred from the point's own params.
The rows follow this module's twist convention (translation first, rotation
last, at the assembly origin), so their angular moment arms are the recorded
point's, verified here by finite differences.
"""

from __future__ import annotations

import math
import unittest

from description_pipeline.sources.solidworks import discovery as D

IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def rotation_z(angle: float):
    cosine, sine = math.cos(angle), math.sin(angle)
    return [
        [cosine, -sine, 0.0, 0.0],
        [sine, cosine, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def dot(left, right):
    return sum(one * two for one, two in zip(left, right, strict=True))


def cross(left, right):
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def rodrigues(omega, scale, vector):
    """Rotate ``vector`` about the origin by ``scale * omega`` (right-handed)."""

    norm = math.sqrt(dot(omega, omega))
    if norm == 0.0:
        return list(vector)
    axis = [value / norm for value in omega]
    angle = norm * scale
    cosine, sine = math.cos(angle), math.sin(angle)
    term = [axis[index] * dot(axis, vector) * (1.0 - cosine) for index in range(3)]
    return [vector[index] * cosine + cross(axis, vector)[index] * sine + term[index] for index in range(3)]


def cylinder_entity(component, point, direction, *, radius=0.01, feature="Cyl"):
    return {
        "component": component,
        "feature": feature,
        "cylinder": {"point": list(point), "direction": list(direction), "radius": radius},
    }


def point_entity(component, point):
    return {"component": component, "point": list(point)}


def localized_point_entity(component, point, *, frame="component-local", error=None, params=None, assembly_frame=False):
    entity = {
        "component": component,
        "mate_entity_reference": {
            "error": error,
            "geometry": {"kind": "point", "frame": frame, "point": list(point)},
        },
    }
    if params is not None:
        entity["mate_entity_reference"]["params"] = list(params)
    if assembly_frame:
        entity["assembly_frame"] = True
    return entity


def concentrate(first, second, frames, name="Concentric1"):
    findings: list[dict] = []
    result = D._mate_rows(
        {"name": name, "type": "concentric", "limits": None, "entities": [first, second]},
        frames,
        findings,
        f"mate:{name}",
    )
    return result, findings


def messages(findings):
    return [finding["message"] for finding in findings]


class PointOnAxisTests(unittest.TestCase):
    def test_solved_point_on_axis_builds_two_rows_at_the_point(self):
        axis_point, direction, point = [0.0, 0.0, 0.3], [0.0, 1.0, 0.0], [0.0, 0.25, 0.3]
        result, findings = concentrate(
            cylinder_entity("A", axis_point, direction),
            point_entity("B", point),
            {"A": IDENTITY, "B": IDENTITY},
        )
        self.assertEqual(findings, [])
        self.assertIsNotNone(result)
        rows = result["rows"]
        self.assertEqual(len(rows), 2)  # a two-row relation, never a four-row axis alignment
        self.assertIsNone(result["axis"])  # no invented joint/shaft selector
        self.assertIsNone(result["entity"])
        self.assertEqual(result["point"], point)
        normals = [row[:3] for row in rows]
        for normal, row in zip(normals, rows, strict=True):
            with self.subTest(normal=normal):
                self.assertAlmostEqual(math.sqrt(dot(normal, normal)), 1.0, places=12)
                self.assertAlmostEqual(dot(normal, direction), 0.0, places=12)
                expected_moment = cross(point, normal)
                for got, want in zip(row[3:], expected_moment, strict=True):
                    self.assertAlmostEqual(got, want, places=12)  # the point's moment arm, not the origin's
        self.assertAlmostEqual(dot(normals[0], normals[1]), 0.0, places=12)

    def test_finite_difference_confirms_the_point_jacobian(self):
        axis_point, direction, point = [0.05, -0.02, 0.3], [0.0, 1.0, 0.0], [0.05, 0.17, 0.3]
        result, findings = concentrate(
            cylinder_entity("A", axis_point, direction),
            point_entity("B", point),
            {"A": IDENTITY, "B": IDENTITY},
        )
        self.assertEqual(findings, [])
        twists = (
            ([0.3, -0.2, 0.5], [0.01, 0.02, -0.03]),
            ([-0.4, 0.1, 0.2], [-0.02, 0.005, 0.01]),
        )
        for omega, velocity in twists:
            for row in result["rows"]:
                normal = row[:3]
                analytic = dot(row[:3], velocity) + dot(row[3:], omega)

                def offset(scale, omega=omega, velocity=velocity, normal=normal):
                    rotated = rodrigues(omega, scale, point)
                    moved = [rotated[index] + velocity[index] * scale for index in range(3)]
                    return dot(normal, [moved[index] - axis_point[index] for index in range(3)])

                epsilon = 1e-6
                finite = (offset(epsilon) - offset(-epsilon)) / (2.0 * epsilon)
                self.assertAlmostEqual(analytic, finite, places=7)

    def test_rotated_occurrence_frames_transform_axis_and_point(self):
        frames = {"A": IDENTITY, "B": rotation_z(math.pi / 2)}
        # B's local +X axis becomes the assembly +Y axis through the occurrence frame.
        axis = cylinder_entity("B", [0.3, 0.0, 0.2], [1.0, 0.0, 0.0])
        on_axis = point_entity("A", [0.0, 0.1, 0.2])
        result, findings = concentrate(axis, on_axis, frames)
        self.assertEqual(findings, [])
        self.assertIsNotNone(result)
        self.assertEqual(len(result["rows"]), 2)
        for row in result["rows"]:
            self.assertAlmostEqual(dot(row[:3], [0.0, 1.0, 0.0]), 0.0, places=12)
            for got, want in zip(row[3:], cross([0.0, 0.1, 0.2], row[:3]), strict=True):
                self.assertAlmostEqual(got, want, places=12)
        off_axis, findings = concentrate(axis, point_entity("A", [0.01, 0.1, 0.2]), frames)
        self.assertIsNone(off_axis)
        self.assertIn("solved concentric point is off the recorded axis", messages(findings))

    def test_point_params_never_supply_the_axis(self):
        tempting = [0.0, 0.05, 0.2, 1.0, 0.0, 0.0, 0.05, 0.0]
        point = localized_point_entity("A", [0.0, 0.0, 0.5], params=tempting)
        result, findings = concentrate(
            cylinder_entity("B", [0.0, 0.0, 0.2], [0.0, 0.0, 1.0]), point, {"A": IDENTITY, "B": IDENTITY}
        )
        self.assertEqual(findings, [])
        self.assertIsNotNone(result)
        for row in result["rows"]:
            # the rows honour the recorded cylinder direction only; the params' +X vector is ignored
            self.assertAlmostEqual(dot(row[:3], [0.0, 0.0, 1.0]), 0.0, places=12)
        # an axis taken from the tempting +X params would produce rows perpendicular to X;
        # the Z axis instead admits X into the perpendicular basis — proving the axis source
        self.assertTrue(any(abs(dot(row[:3], [1.0, 0.0, 0.0])) > 0.9 for row in result["rows"]))
        # two pure points carry no axis and stay refused with the established message
        two_points, findings = concentrate(
            point_entity("A", [0.0, 0.0, 0.0]), point_entity("B", [0.0, 0.0, 0.0]), {"A": IDENTITY, "B": IDENTITY}
        )
        self.assertIsNone(two_points)
        self.assertIn("concentric mate needs two recorded cylinders or circular edges", messages(findings))

    def test_off_axis_and_unknown_point_frames_are_refused(self):
        axis = cylinder_entity("A", [0.0, 0.0, 0.0], [0.0, 1.0, 0.0])
        # just outside the 0.05 mm solved-state contract: refused with the geometric mismatch
        beyond = point_entity("B", [5.1e-5, 0.1, 0.0])
        result, findings = concentrate(axis, beyond, {"A": IDENTITY, "B": IDENTITY})
        self.assertIsNone(result)
        mismatch = next(finding for finding in findings if finding["code"] == "discovery.mate_geometry_mismatch")
        self.assertIn("solved concentric point is off the recorded axis", mismatch["message"])
        self.assertAlmostEqual(mismatch["detail"]["radial_gap_m"], 5.1e-5, places=9)
        # just inside the contract: accepted (the tolerance is honoured, not rounded away)
        inside = point_entity("B", [4.9e-5, 0.1, 0.0])
        accepted, findings = concentrate(axis, inside, {"A": IDENTITY, "B": IDENTITY})
        self.assertEqual(findings, [])
        self.assertIsNotNone(accepted)
        # a point whose provenance is not component-local keeps failing closed
        unknown = localized_point_entity("B", [0.0, 0.1, 0.0], frame="assembly")
        result, findings = concentrate(axis, unknown, {"A": IDENTITY, "B": IDENTITY})
        self.assertIsNone(result)
        self.assertTrue(any("component-local" in message for message in messages(findings)))
        # a failed localization is never trusted either
        failed = localized_point_entity("B", [0.0, 0.1, 0.0], error="decode failed")
        result, findings = concentrate(axis, failed, {"A": IDENTITY, "B": IDENTITY})
        self.assertIsNone(result)
        self.assertTrue(findings)

    def test_frame_origin_point_keeps_zero_moment_arms(self):
        # The assembly's own frame point: the origin itself, so the moment arms vanish and
        # the two rows are pure perpendicular translations at the twist origin.
        frame_point = localized_point_entity("", [0.0, 0.0, 0.0], assembly_frame=True)
        result, findings = concentrate(
            cylinder_entity("A", [0.0, 0.0, -0.02], [0.0, 0.0, 1.0]), frame_point, {"A": IDENTITY}
        )
        self.assertEqual(findings, [])
        self.assertIsNotNone(result)
        self.assertEqual(len(result["rows"]), 2)
        for row in result["rows"]:
            self.assertAlmostEqual(dot(row[:3], [0.0, 0.0, 1.0]), 0.0, places=12)
            for moment in row[3:]:
                self.assertAlmostEqual(moment, 0.0, places=12)


if __name__ == "__main__":
    unittest.main()
