"""Nested assembly self-frame relations (source contract only).

A mate inside a sub-assembly whose frame entity carries that sub-assembly's own
identity is a definition-internal relation: the reader validates the scope
occurrence and localizes the parameters with ``inv(F_scope) x F_scope``, so the
recorded scope-local geometry is mapped back exactly by the occurrence
transform.  It is reconstructed by the ordinary pair machinery, can never ground
anything, and never vetoes the frozen top aggregate — while malformed frame
identities keep failing closed without that veto.
"""

from __future__ import annotations

import math
import unittest

from description_pipeline.sources.solidworks import discovery as D

IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def frame(rotation=None, translation=(0.0, 0.0, 0.0)):
    matrix = [list(row) for row in (rotation or IDENTITY)]
    for index in range(3):
        matrix[index][3] = float(translation[index])
    return matrix


def rotation_z(angle: float):
    cosine, sine = math.cos(angle), math.sin(angle)
    return [
        [cosine, -sine, 0.0, 0.0],
        [sine, cosine, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def flat(matrix):
    return [value for row in matrix for value in row]


def component(name, document):
    return {
        "name2": name,
        "document": document,
        "suppressed": False,
        "lightweight": False,
        "suppression_state": 2,
        "fixed": False,
    }


def record(components, mates):
    return {"components": components, "mates": mates, "properties": {}, "datums": []}


def mate(name, mtype, scope, first, second):
    return {
        "name": name,
        "type": mtype,
        "entity_count": 2,
        "scope": scope,
        "suppressed": False,
        "error_code": 0,
        "limits": None,
        "entities": [first, second],
    }


def self_frame(component_name, *, plane=((0.0, 0.0, 1.0), (0.1, 0.0, 0.0)), error=None):
    normal, point = plane
    return {
        "component": component_name,
        "assembly_frame": True,
        "mate_entity_reference": {
            "error": error,
            "geometry": {"kind": "plane", "frame": "component-local", "point": list(point), "normal": list(normal)},
        },
    }


def plane_on(component_name, normal, point):
    return {"component": component_name, "plane": {"normal": list(normal), "point": list(point)}}


def top_lock(occurrence):
    return mate("TopLock", "lock", "", {"component": occurrence}, {"component": "", "assembly_frame": True})


def cluster_state(components, mates):
    findings: list[dict] = []
    clusters = D._clusters(record(components, mates), findings)
    return clusters, findings


class NestedSelfFrameTests(unittest.TestCase):
    def _base(self):
        return [
            {**component("A", "A.SLDPRT"), "transform": flat(IDENTITY)},
            {**component("S", "S.SLDASM"), "transform": flat(IDENTITY)},
            {**component("S/part-1", "part.SLDPRT"), "transform": flat(IDENTITY)},
        ]

    def test_nested_self_frame_never_vetoes_the_frozen_top_aggregate(self):
        components = self._base()
        nested = mate(
            "Nested1",
            "parallel",
            "S",
            plane_on("S/part-1", [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]),
            self_frame("S", plane=([0.0, 1.0, 0.0], [0.0, 0.2, 0.0])),
        )
        clusters, findings = cluster_state(components, [top_lock("A"), nested])
        self.assertEqual([f for f in findings if f["code"] == "discovery.frame_attachment"], [])
        self.assertIsNotNone(clusters.frame)
        self.assertEqual(clusters.frame.members, frozenset({"A"}))
        # the nested relation is rank two: it neither grounds nor merges the pair
        self.assertNotEqual(clusters.of["S/part-1"], clusters.of["S"])
        nested_only, findings = cluster_state(components, [nested])
        self.assertIsNone(nested_only.frame)  # a nested frame never grounds anything
        self.assertEqual([f for f in findings if f["code"] == "discovery.frame_attachment"], [])

    def test_nested_self_frame_rows_equal_the_equivalent_ordinary_pair(self):
        scope_frame = frame(rotation_z(math.pi / 2), (0.4, -0.2, 0.1))
        frames = {"S": scope_frame, "S/part-1": IDENTITY}
        scope_plane = ([0.0, 0.0, 1.0], [0.1, 0.05, 0.0])
        nested_mate = mate(
            "Nested2",
            "parallel",
            "S",
            plane_on("S/part-1", [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]),
            self_frame("S", plane=scope_plane),
        )
        ordinary_mate = mate(
            "Ordinary2",
            "parallel",
            "S",
            plane_on("S/part-1", [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]),
            plane_on("S", scope_plane[0], scope_plane[1]),
        )
        nested_findings: list[dict] = []
        ordinary_findings: list[dict] = []
        nested_rows = D._mate_rows(nested_mate, frames, nested_findings, "mate:Nested2")
        ordinary_rows = D._mate_rows(ordinary_mate, frames, ordinary_findings, "mate:Ordinary2")
        self.assertEqual(nested_findings, [])
        self.assertEqual(ordinary_findings, [])
        self.assertIsNotNone(nested_rows)
        self.assertEqual(nested_rows["rows"], ordinary_rows["rows"])
        self.assertEqual(len(nested_rows["rows"]), 2)  # plane-plane parallel: two rotational rows
        self.assertIsNone(nested_rows["axis"])

    def test_rotated_nonzero_translation_scope_maps_the_scope_local_plane(self):
        scope_frame = frame(rotation_z(math.pi / 2), (0.4, -0.2, 0.1))
        components = [
            {**component("S", "S.SLDASM"), "transform": flat(scope_frame)},
            {**component("S/part-1", "part.SLDPRT"), "transform": flat(IDENTITY)},
        ]
        # the part plane is parallel in parent coordinates only after the scope mapping
        good = mate(
            "Nested3",
            "parallel",
            "S",
            plane_on("S/part-1", [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]),
            self_frame("S", plane=([0.0, 0.0, 1.0], [0.1, 0.0, 0.0])),
        )
        clusters, findings = cluster_state(components, [good])
        self.assertEqual(findings, [])
        pair = clusters.pairs[tuple(sorted(("S", "S/part-1")))]
        self.assertFalse(pair["unresolved"])
        self.assertEqual(pair["rank"], 2)
        # a plane parallel in scope space but not in parent coordinates is refused
        bad = mate(
            "Nested4",
            "parallel",
            "S",
            plane_on("S/part-1", [1.0, 0.0, 0.0], [0.0, 0.0, 0.0]),
            self_frame("S", plane=([0.0, 0.0, 1.0], [0.1, 0.0, 0.0])),
        )
        clusters, findings = cluster_state(components, [bad])
        self.assertTrue(clusters.pairs[tuple(sorted(("S", "S/part-1")))]["unresolved"])
        self.assertEqual([f for f in findings if f["code"] == "discovery.frame_attachment"], [])

    def test_malformed_nested_identities_fail_closed_without_the_veto(self):
        components = [*self._base(), {**component("T", "T.SLDASM"), "transform": flat(IDENTITY)}]
        cases = {
            "foreign-frame-component": mate(
                "Bad1", "parallel", "S",
                plane_on("S/part-1", [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]),
                self_frame("T"),
            ),
            "double-flagged": mate(
                "Bad2", "parallel", "S",
                self_frame("S"),
                self_frame("S"),
            ),
            "unknown-scope": mate(
                "Bad3", "parallel", "X",
                plane_on("S/part-1", [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]),
                self_frame("X"),
            ),
            "foreign-endpoint": mate(
                "Bad4", "parallel", "S",
                plane_on("ghost/part", [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]),
                self_frame("S"),
            ),
        }
        for label, bad in cases.items():
            with self.subTest(case=label):
                clusters, findings = cluster_state(components, [top_lock("A"), bad])
                self.assertTrue(
                    any(f["code"] == "discovery.frame_attachment" for f in findings),
                    findings,
                )
                self.assertIsNotNone(clusters.frame)  # the top aggregate is never vetoed
                self.assertEqual(clusters.frame.members, frozenset({"A"}))

    def test_nested_self_frame_requires_exactly_two_raw_entities(self):
        components = self._base()
        three_with_junk = mate(
            "Bad5",
            "parallel",
            "S",
            plane_on("S/part-1", [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]),
            self_frame("S", plane=([0.0, 1.0, 0.0], [0.0, 0.2, 0.0])),
        )
        three_with_junk["entities"].append("junk")
        three_dicts = mate(
            "Bad6",
            "parallel",
            "S",
            plane_on("S/part-1", [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]),
            self_frame("S", plane=([0.0, 1.0, 0.0], [0.0, 0.2, 0.0])),
        )
        three_dicts["entities"].append(plane_on("S/part-1", [0.0, 1.0, 0.0], [0.0, 0.1, 0.0]))
        for label, bad in {"third-non-dict": three_with_junk, "three-dicts": three_dicts}.items():
            with self.subTest(case=label):
                clusters, findings = cluster_state(components, [top_lock("A"), bad])
                self.assertTrue(any(f["code"] == "discovery.frame_attachment" for f in findings), findings)
                self.assertIsNotNone(clusters.frame)
                self.assertEqual(clusters.frame.members, frozenset({"A"}))

    def test_nested_self_frame_endpoint_outside_the_scope_is_rejected(self):
        components = [*self._base(), {**component("T", "T.SLDASM"), "transform": flat(IDENTITY)}]
        bad = mate(
            "Bad7",
            "parallel",
            "S",
            plane_on("T", [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]),
            self_frame("S", plane=([0.0, 1.0, 0.0], [0.0, 0.2, 0.0])),
        )
        clusters, findings = cluster_state(components, [top_lock("A"), bad])
        self.assertTrue(any(f["code"] == "discovery.frame_attachment" for f in findings), findings)
        self.assertIsNotNone(clusters.frame)
        self.assertEqual(clusters.frame.members, frozenset({"A"}))

    def test_nested_self_frame_rank_and_membership_invariants(self):
        components = self._base()
        parallel = mate(
            "Nested5",
            "parallel",
            "S",
            plane_on("S/part-1", [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]),
            self_frame("S", plane=([0.0, 1.0, 0.0], [0.0, 0.2, 0.0])),
        )
        clusters, findings = cluster_state(components, [parallel])
        self.assertEqual(findings, [])
        self.assertNotEqual(clusters.of["S/part-1"], clusters.of["S"])  # rank two never merges
        self.assertIsNone(clusters.frame)
        locking = mate("Nested6", "lock", "S", {"component": "S/part-1"}, self_frame("S"))
        clusters, findings = cluster_state(components, [locking])
        self.assertEqual(findings, [])
        self.assertIsNone(clusters.frame)  # still no grounding
        self.assertEqual(clusters.of["S/part-1"], clusters.of["S"])  # the ordinary rank-six rule applies

    def test_nested_self_frame_without_usable_geometry_stays_fail_closed(self):
        components = self._base()
        no_geometry = mate(
            "Nested7",
            "parallel",
            "S",
            plane_on("S/part-1", [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]),
            {"component": "S", "assembly_frame": True},
        )
        failed = mate(
            "Nested8",
            "parallel",
            "S",
            plane_on("S/part-1", [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]),
            self_frame("S", error="decode failed"),
        )
        clusters, findings = cluster_state(components, [top_lock("A"), no_geometry])
        self.assertIsNotNone(clusters.frame)  # fail-closed for the mate, never for the aggregate
        self.assertEqual([f for f in findings if f["code"] == "discovery.frame_attachment"], [])
        self.assertTrue(clusters.pairs[tuple(sorted(("S", "S/part-1")))]["unresolved"])
        clusters, findings = cluster_state(components, [top_lock("A"), failed])
        self.assertIsNotNone(clusters.frame)
        self.assertEqual([f for f in findings if f["code"] == "discovery.frame_attachment"], [])
        self.assertTrue(findings)  # the decode failure is reported; the aggregate is not vetoed


if __name__ == "__main__":
    unittest.main()
