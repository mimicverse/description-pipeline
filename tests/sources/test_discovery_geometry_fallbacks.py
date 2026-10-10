"""Strict localized-geometry fallback in native discovery (synthetic controls only).

The reader localizes decoded mate references under ``mate_entity_reference`` while the flat
face evidence is only recorded for some kinds (line references and assembly-frame references
have no flat view today).  These tests pin the fallback contract: component-local provenance
only, complete finite shapes, recorded face evidence stays primary, conflicting views block,
a localized cylinder is never fabricated, and the joint-shaft requirement is not weakened.
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


def component(name: str, transform=None) -> dict:
    matrix = transform or IDENTITY
    return {"name2": name, "document": f"{name}.SLDPRT", "transform": [value for row in matrix for value in row]}


def localized(kind: str, *, frame: str = "component-local", **geometry) -> dict:
    return {"mate_entity_reference": {"error": None, "geometry": {"kind": kind, "frame": frame, **geometry}}}


def mate(name: str, kind: str, first: dict, second: dict) -> dict:
    return {
        "name": name,
        "type": kind,
        "error_code": 0,
        "suppressed": False,
        "scope": "",
        "entities": [first, second],
    }


def record(components: list[dict], mates: list[dict]) -> dict:
    return {"components": components, "mates": mates, "datums": [], "properties": {}}


class GeometryFallbackTests(unittest.TestCase):
    def test_localized_point_does_not_inherit_an_unrecorded_line_key(self) -> None:
        item = mate(
            "point_on_plane",
            "coincident",
            {
                "component": "A",
                "line": {"point": [0, 0, 0], "direction": [1, 0, 0]},
                **localized("point", point=[0, 0, 0]),
            },
            {"component": "B", "plane": {"normal": [0, 1, 0], "point": [0, 0, 0]}},
        )
        result, findings, _ = self.rows(item, [component("A"), component("B")])
        self.assertEqual(findings, [])
        self.assertEqual(result["rank"], 1)

    def rows(self, mate_item: dict, components: list[dict]) -> tuple[dict | None, list[dict], dict]:
        raw = record(components, [mate_item])
        findings: list[dict] = []
        frames = D._component_frames(raw, findings)
        self.assertEqual(findings, [])
        result = D._mate_rows(mate_item, frames, findings, f"mate:{mate_item['name']}")
        if result is not None:
            result = {**result, "rank": D._rank(result["rows"])}
        return result, findings, frames

    def test_parallel_lines_fall_back_to_localized_direction_with_rotated_occurrence(self) -> None:
        # B is rotated 90 degrees about Z; its local +Y is the assembly -X, parallel to A's +X.
        components = [component("A"), component("B", rotation_z(math.pi / 2))]
        item = mate(
            "平行1",
            "parallel",
            {"component": "A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])},
            {"component": "B", **localized("line", point=[0.0, 0.0, 0.0], direction=[0.0, 1.0, 0.0])},
        )
        result, findings, _ = self.rows(item, components)
        self.assertEqual(findings, [])
        self.assertIsNotNone(result)
        self.assertEqual(result["rank"], 2)  # a parallel pair removes the two rotational freedoms

    def test_parallel_lines_that_are_not_parallel_in_the_solved_state_block(self) -> None:
        components = [component("A"), component("B", rotation_z(math.pi / 2))]
        item = mate(
            "平行2",
            "parallel",
            {"component": "A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])},
            {"component": "B", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])},
        )
        result, findings, _ = self.rows(item, components)
        self.assertIsNone(result)
        self.assertEqual([finding["code"] for finding in findings], ["discovery.mate_geometry_mismatch"])

    def test_parallel_line_plane_uses_the_correct_single_constraint(self) -> None:
        # A line parallel to a plane removes ONE rotational freedom; a line perpendicular to
        # the plane is not this mate and must never be accepted as "parallel".
        parallel = mate(
            "平行3",
            "parallel",
            {"component": "A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])},
            {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
        )
        result, findings, _ = self.rows(parallel, [component("A"), component("B")])
        self.assertEqual(findings, [])
        self.assertEqual(result["rank"], 1)

        perpendicular = mate(
            "平行4",
            "parallel",
            {"component": "A", **localized("line", point=[0.0, 0.0, 0.0], direction=[0.0, 1.0, 0.0])},
            {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
        )
        result, findings, _ = self.rows(perpendicular, [component("A"), component("B")])
        self.assertIsNone(result)
        self.assertEqual(findings[0]["code"], "discovery.mate_geometry_mismatch")

    def test_failed_decoded_provenance_never_creates_evidence(self) -> None:
        failed = {"mate_entity_reference": {"error": "EntityParams is unavailable", "geometry": None}}
        needed = mate(
            "重合16",
            "coincident",
            {"component": "A", **failed},
            {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
        )
        result, findings, _ = self.rows(needed, [component("A"), component("B")])
        self.assertIsNone(result)
        self.assertIn("failed to decode", findings[0]["message"])

        primary = mate(
            "重合17",
            "coincident",
            {
                "component": "A",
                "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]},
                **failed,
            },
            {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
        )
        result, findings, _ = self.rows(primary, [component("A"), component("B")])
        self.assertEqual(findings, [])
        self.assertEqual(result["rank"], 3)

    def test_frame_plane_fallback_grounds_the_top_assembly_base(self) -> None:
        components = [component("A")]
        mates = [
            mate(
                f"重合{index}",
                "coincident",
                {"component": "A", "plane": {"normal": normal, "point": [0.0, 0.0, 0.0]}},
                {
                    "component": "",
                    "assembly_frame": True,
                    **localized("plane", normal=normal, point=[0.0, 0.0, 0.0]),
                },
            )
            for index, normal in enumerate(([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]), 1)
        ]
        raw = record(components, mates)
        findings: list[dict] = []
        clusters = D._clusters(raw, findings)
        self.assertEqual([finding["code"] for finding in findings], [])
        self.assertIsNotNone(clusters.frame)
        self.assertEqual(clusters.frame.rank, 6)
        self.assertEqual(set(clusters.frame.members), {"A"})

    def test_conflicting_views_block_and_flipped_normals_are_the_same_plane(self) -> None:
        conflicting = mate(
            "重合9",
            "coincident",
            {"component": "A", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
            {
                "component": "B",
                "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]},
                **localized("plane", normal=[1.0, 0.0, 0.0], point=[0.0, 0.0, 0.0]),
            },
        )
        result, findings, _ = self.rows(conflicting, [component("A"), component("B")])
        self.assertIsNone(result)
        self.assertIn("contradicts recorded face evidence", findings[0]["message"])

        flipped = mate(
            "重合10",
            "coincident",
            {"component": "A", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
            {"component": "B", "plane": {"normal": [0.0, -1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
        )
        result, findings, _ = self.rows(flipped, [component("A"), component("B")])
        self.assertEqual(findings, [])
        self.assertEqual(result["rank"], 3)

    def test_irrelevant_or_unverifiable_provenance_never_blocks_valid_face_evidence(self) -> None:
        # A valid recorded plane must survive provenance that cannot (or need not) be compared:
        # an unsupported kind, a non-component-local frame, or a malformed shape.
        provenances = {
            "unsupported-kind": localized("cone", point=[0.0, 0.0, 0.0], direction=[0.0, 1.0, 0.0]),
            "not-component-local": localized(
                "plane", frame="mate-assembly", normal=[0.0, 1.0, 0.0], point=[0.0, 0.0, 0.0]
            ),
            "malformed": localized("plane", normal=[0.0, True, 0.0], point=[0.0, 0.0, 0.0]),
        }
        for label, entity in provenances.items():
            with self.subTest(label=label):
                item = mate(
                    "重合14",
                    "coincident",
                    {"component": "A", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
                    {
                        "component": "B",
                        "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]},
                        **entity,
                    },
                )
                result, findings, _ = self.rows(item, [component("A"), component("B")])
                self.assertEqual(findings, [])
                self.assertEqual(result["rank"], 3)

    def test_needed_fallback_rejects_boolean_and_non_finite_numbers(self) -> None:
        for label, geometry in {
            "boolean": localized("plane", normal=[0.0, True, 0.0], point=[0.0, 0.0, 0.0]),
            "nan": localized("plane", normal=[0.0, float("nan"), 0.0], point=[0.0, 0.0, 0.0]),
            "infinite": localized("plane", normal=[0.0, float("inf"), 0.0], point=[0.0, 0.0, 0.0]),
        }.items():
            with self.subTest(label=label):
                item = mate(
                    "重合15",
                    "coincident",
                    {"component": "A", **geometry},
                    {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
                )
                result, findings, _ = self.rows(item, [component("A"), component("B")])
                self.assertIsNone(result)
                self.assertEqual(findings[0]["code"], "discovery.mate_entities_unsupported")

    def test_malformed_primary_is_refused_never_substituted_by_provenance(self) -> None:
        plane = {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}}
        for label, primary in {
            "boolean-component": [0.0, True, 0.0],
            "non-finite": [0.0, float("nan"), 0.0],
            "wrong-length": [0.0, 0.0],
            "not-a-vector": {"x": 0.0, "y": 0.0, "z": 0.0},
        }.items():
            with self.subTest(label=label):
                item = mate(
                    "重合18",
                    "coincident",
                    {
                        "component": "A",
                        "point": primary,
                        **localized("point", point=[0.0, 0.0, 0.0]),
                    },
                    plane,
                )
                result, findings, _ = self.rows(item, [component("A"), component("B")])
                self.assertIsNone(result)
                self.assertIn("recorded mate entity geometry is malformed", findings[0]["message"])

        # A valid recorded point stays primary even when the optional provenance is unusable.
        item = mate(
            "重合19",
            "coincident",
            {
                "component": "A",
                "point": [0.0, 0.0, 0.0],
                **localized("point", frame="mate-assembly", point=[0.0, 0.0, 0.0]),
            },
            plane,
        )
        result, findings, _ = self.rows(item, [component("A"), component("B")])
        self.assertEqual(findings, [])
        self.assertEqual(result["rank"], 1)

    def test_localized_point_never_merges_with_recorded_face_evidence(self) -> None:
        # A valid localized point beside a valid recorded plane is a contradictory shape and
        # must block; only a recorded bare point of the same kind stays cross-checkable.
        mixed = mate(
            "重合20",
            "coincident",
            {
                "component": "A",
                "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]},
                **localized("point", point=[0.0, 0.0, 0.0]),
            },
            {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
        )
        result, findings, _ = self.rows(mixed, [component("A"), component("B")])
        self.assertIsNone(result)
        self.assertIn("contradicts recorded face evidence", findings[0]["message"])

        same_kind = mate(
            "重合21",
            "coincident",
            {"component": "A", "point": [0.0, 0.0, 0.0], **localized("point", point=[0.0, 0.0, 0.0])},
            {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
        )
        result, findings, _ = self.rows(same_kind, [component("A"), component("B")])
        self.assertEqual(findings, [])
        self.assertEqual(result["rank"], 1)

    def test_stray_flat_line_key_is_never_evidence_beside_a_validated_reference(self) -> None:
        # A top-level ``line`` key is outside the recorded face-evidence schema; the validated
        # localized point stays the single reference (point-plane coincident = one row).
        item = mate(
            "重合22",
            "coincident",
            {
                "component": "A",
                "line": {"point": [0.0, 0.0, 0.0], "direction": [1.0, 0.0, 0.0]},
                **localized("point", point=[0.0, 0.0, 0.0]),
            },
            {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
        )
        result, findings, _ = self.rows(item, [component("A"), component("B")])
        self.assertEqual(findings, [])
        self.assertEqual(result["rank"], 1)

    def test_malformed_or_nonlocal_provenance_blocks_and_cylinders_are_never_fabricated(self) -> None:
        cases = {
            "not-component-local": localized("plane", frame="mate-assembly", normal=[0, 1, 0], point=[0, 0, 0]),
            "malformed": localized("plane", normal=[0, 1, 0]),
            "cylinder-without-face-evidence": localized(
                "cylinder", direction=[0, 1, 0], point=[0, 0, 0], radius=0.01
            ),
        }
        for label, entity in cases.items():
            with self.subTest(label=label):
                item = mate("重合11", "coincident", {"component": "A", **entity}, {"component": "B", **entity})
                result, findings, _ = self.rows(item, [component("A"), component("B")])
                self.assertIsNone(result)
                self.assertTrue(findings)
                self.assertEqual(findings[0]["code"], "discovery.mate_entities_unsupported")

    def test_circle_edges_never_gain_a_shaft(self) -> None:
        item = mate(
            "同心1",
            "concentric",
            {"component": "A", "circle": {"center": [0, 0, 0], "normal": [0, 1, 0], "radius": 0.003}},
            {"component": "B", "circle": {"center": [0, 0, 0], "normal": [0, 1, 0], "radius": 0.003}},
        )
        result, findings, _ = self.rows(item, [component("A"), component("B")])
        self.assertEqual(findings, [])
        self.assertIsNone(result["axis"])  # edge evidence still cannot qualify a re-readable shaft

    def test_line_plane_coincident_is_validated_before_rows_are_emitted(self) -> None:
        lying_in_plane = mate(
            "重合12",
            "coincident",
            {"component": "A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])},
            {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
        )
        result, findings, _ = self.rows(lying_in_plane, [component("A"), component("B")])
        self.assertEqual(findings, [])
        self.assertEqual(result["rank"], 2)

        crossing = mate(
            "重合13",
            "coincident",
            {"component": "A", **localized("line", point=[0.0, 0.0, 0.0], direction=[0.0, 1.0, 0.0])},
            {"component": "B", "plane": {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}},
        )
        result, findings, _ = self.rows(crossing, [component("A"), component("B")])
        self.assertIsNone(result)
        self.assertEqual(findings[0]["code"], "discovery.mate_geometry_mismatch")

if __name__ == "__main__":
    unittest.main()
