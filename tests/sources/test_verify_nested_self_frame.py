"""Independent oracle tests for nested self-frame relations (verifier side).

A mate whose scope is a nested assembly ``S`` may carry exactly one assembly-frame
entity with ``component == S``: S's own reference geometry expressed in S's own space.
Per the design contract it is an ordinary in-scope pair -- it never takes part in the
frozen-top frame attachment, never grounds anything, and a defect on it is a
scope-isolated diagnostic that cannot veto the top aggregate.  The expected rows follow
from the geometry: the flagged entity's parent-space normal is the scope occurrence
transform applied to its recorded normal.
"""

from __future__ import annotations

import math
import unittest

from description_pipeline.verification import native_discovery as V

IDENTITY = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
]


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


def transform16(rotation_matrix, translation):
    return [
        rotation_matrix[0][0], rotation_matrix[0][1], rotation_matrix[0][2], translation[0],
        rotation_matrix[1][0], rotation_matrix[1][1], rotation_matrix[1][2], translation[1],
        rotation_matrix[2][0], rotation_matrix[2][1], rotation_matrix[2][2], translation[2],
        0.0, 0.0, 0.0, 1.0,
    ]


def inverse_vector(rotation_matrix, world):
    return [
        rotation_matrix[0][0] * world[0] + rotation_matrix[1][0] * world[1] + rotation_matrix[2][0] * world[2],
        rotation_matrix[0][1] * world[0] + rotation_matrix[1][1] * world[1] + rotation_matrix[2][1] * world[2],
        rotation_matrix[0][2] * world[0] + rotation_matrix[1][2] * world[1] + rotation_matrix[2][2] * world[2],
    ]


def inverse_point(rotation_matrix, translation, world):
    shifted = [
        world[0] - translation[0],
        world[1] - translation[1],
        world[2] - translation[2],
    ]
    return inverse_vector(rotation_matrix, shifted)


class NestedSelfFrameTests(unittest.TestCase):
    PARENT_NORMAL = unit([0.35, -0.2, 0.9])

    def setUp(self):
        self.head_rotation = rotation([0.2, 0.9, -0.3], 0.7)
        self.head_translation = [0.4, -0.15, 0.22]
        self.neck_rotation = rotation([0.6, -0.4, 0.15], -0.5)
        self.neck_translation = [-0.12, 0.36, 0.07]

    def component(self, name, rotation_matrix, translation, document):
        return {
            "name2": name,
            "document": document,
            "suppressed": False,
            "transform": transform16(rotation_matrix, translation),
        }

    def top_mates(self):
        return [
            {
                "name": "coaxial__top-frame",
                "type": "concentric",
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {
                        "component": "",
                        "assembly_frame": True,
                        "cylinder": {"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.01},
                    },
                    {
                        "component": "torso-1",
                        "cylinder": {"point": [0.0, 0.0, 0.05], "direction": [0.0, 0.0, 1.0], "radius": 0.01},
                    },
                ],
            },
            {
                "name": "parallel__top-frame",
                "type": "parallel",
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {
                        "component": "",
                        "assembly_frame": True,
                        "plane": {"point": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]},
                    },
                    {
                        "component": "torso-1",
                        "plane": {"point": [0.0, 0.0, 0.2], "normal": [-1.0, 0.0, 0.0]},
                    },
                ],
            },
            {
                "name": "coincident__top-frame",
                "type": "coincident",
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {
                        "component": "",
                        "assembly_frame": True,
                        "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
                    },
                    {
                        "component": "torso-1",
                        "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, -1.0]},
                    },
                ],
            },
        ]

    def nested_mate(
        self,
        *,
        flagged_component=None,
        parent_normal=None,
        neck_normal=None,
        extra_flagged=False,
        endpoint="neck-1",
    ):
        normal = unit(parent_normal or self.PARENT_NORMAL)
        endpoint_normal = unit(neck_normal or parent_normal or self.PARENT_NORMAL)
        entities = [
            {
                "component": flagged_component or "head-1",
                "assembly_frame": True,
                "plane": {
                    "point": inverse_point(self.head_rotation, self.head_translation, [0.0, 0.0, 0.0]),
                    "normal": inverse_vector(self.head_rotation, normal),
                },
            },
            {
                "component": endpoint,
                "plane": {
                    "point": inverse_point(self.neck_rotation, self.neck_translation, [0.0, 0.0, 0.0]),
                    "normal": inverse_vector(self.neck_rotation, endpoint_normal),
                },
            },
        ]
        if extra_flagged:
            entities.append(
                {
                    "component": "head-1",
                    "assembly_frame": True,
                    "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
                }
            )
        return {
            "name": "parallel__nested",
            "type": "parallel",
            "suppressed": False,
            "error_code": 0,
            "scope": "head-1",
            "limits": None,
            "entities": entities,
        }

    def record(self, *, nested=None, other_component=False, endpoint_component=True):
        components = [
            self.component(
                "torso-1", [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], [0.0, 0.0, 0.0], "torso.sldprt"
            ),
            self.component("head-1", self.head_rotation, self.head_translation, "head.sldasm"),
        ]
        if endpoint_component:
            components.append(self.component("neck-1", self.neck_rotation, self.neck_translation, "neck.sldprt"))
        if other_component:
            components.append(self.component("other-1", self.neck_rotation, self.neck_translation, "other.sldprt"))
        mates = self.top_mates()
        if nested is not None:
            mates.append(nested)
        return {"components": components, "mates": mates, "datums": []}

    def test_top_aggregate_resolves_and_nested_mate_never_vetoes_or_grounds(self):
        base = V._frame_attachment(self.record())
        self.assertEqual(base["problems"], [])
        self.assertEqual(base["members"], frozenset({"torso-1"}))
        self.assertEqual(base["rank"], 6)

        with_nested = self.record(nested=self.nested_mate())
        self.assertEqual(V._frame_attachment(with_nested), base)
        # Adding, removing or breaking the nested relation never changes the top claim.
        broken = self.record(
            nested=self.nested_mate(parent_normal=[0.0, 0.0, 1.0], neck_normal=list(self.PARENT_NORMAL))
        )
        self.assertEqual(V._frame_attachment(broken), base)
        # A record carrying only the nested relation never grounds anything.
        nested_only = {"components": with_nested["components"], "mates": [self.nested_mate()], "datums": []}
        lone = V._frame_attachment(nested_only)
        self.assertEqual(lone["mates"], 0)
        self.assertIsNone(lone["members"])
        self.assertEqual(lone["problems"], [])

    def test_nested_rows_equal_the_ordinary_pair_and_map_the_scope_transform(self):
        nested = self.nested_mate()
        frames = V._frames(self.record(nested=nested))
        nested_rows = V._rows_for(nested, frames)
        self.assertIsNotNone(nested_rows)
        self.assertEqual(len(nested_rows["rows"]), 2)
        self.assertIsNone(nested_rows["axis"])
        ordinary = {
            **nested,
            "entities": [
                {"component": "head-1", "plane": dict(nested["entities"][0]["plane"])},
                dict(nested["entities"][1]),
            ],
        }
        ordinary_rows = V._rows_for(ordinary, frames)
        self.assertEqual(ordinary_rows, nested_rows)
        # The locked tilts span exactly the plane orthogonal to the mapped parent normal.
        for row in nested_rows["rows"]:
            direction = row[0:3]
            self.assertAlmostEqual(dot(direction, self.PARENT_NORMAL), 0.0, places=12)
        self.assertEqual(len(V._span(nested_rows["rows"])), 2)

    def test_nonparallel_nested_state_is_refused_and_isolated(self):
        nested = self.nested_mate(parent_normal=[0.0, 0.0, 1.0], neck_normal=list(self.PARENT_NORMAL))
        frames = V._frames(self.record(nested=nested))
        self.assertIsNone(V._rows_for(nested, frames))
        members, pairs = V._independent_clusters(self.record(nested=nested))
        self.assertTrue(pairs[("head-1", "neck-1")]["unresolved"])
        self.assertEqual(members["head-1"], ["head-1"])
        self.assertEqual(members["neck-1"], ["neck-1"])
        # The top aggregate is computed on the same record and is unaffected.
        attachment = V._frame_attachment(self.record(nested=nested))
        self.assertEqual(attachment["members"], frozenset({"torso-1"}))
        self.assertEqual(attachment["rank"], 6)
        self.assertEqual(attachment["problems"], [])

    def test_nested_identity_defects_are_scope_isolated(self):
        cases = {
            "flagged-outside-scope": self.nested_mate(flagged_component="other-1"),
            "two-flagged-entities": self.nested_mate(extra_flagged=True),
            "unknown-scope": self.nested_mate(flagged_component="ghost-1"),
            "unknown-endpoint": self.nested_mate(endpoint="void-1"),
        }
        for label, nested in cases.items():
            with self.subTest(label=label):
                record = self.record(nested=nested, other_component=True)
                attachment = V._frame_attachment(record)
                self.assertEqual(attachment["top_problems"], [])
                self.assertEqual(attachment["members"], frozenset({"torso-1"}))
                self.assertEqual(attachment["rank"], 6)
                self.assertTrue(attachment["scope_problems"], label)
                self.assertTrue(attachment["problems"], label)

    def test_membership_neutrality_and_the_ordinary_rank_six_rule(self):
        nested = self.nested_mate()
        without = {"components": self.record()["components"], "mates": self.top_mates(), "datums": []}
        with_nested = self.record(nested=nested)
        for record in (without, with_nested):
            members, _pairs = V._independent_clusters(record)
            self.assertEqual(members["neck-1"], ["neck-1"])
        # Reaching rank six through in-scope rows unions the pair exactly like the same
        # relation written as an ordinary pair (no solver-flag inference anywhere).
        shaft = {
            "name": "coaxial__pair",
            "type": "concentric",
            "suppressed": False,
            "error_code": 0,
            "scope": "head-1",
            "limits": None,
            "entities": [
                {
                    "component": "head-1",
                    "cylinder": {
                        "point": inverse_point(self.head_rotation, self.head_translation, [0.0, 0.0, 0.0]),
                        "direction": inverse_vector(self.head_rotation, [0.0, 0.0, 1.0]),
                        "radius": 0.008,
                    },
                },
                {
                    "component": "neck-1",
                    "cylinder": {
                        "point": inverse_point(self.neck_rotation, self.neck_translation, [0.0, 0.0, 0.03]),
                        "direction": inverse_vector(self.neck_rotation, [0.0, 0.0, 1.0]),
                        "radius": 0.008,
                    },
                },
            ],
        }
        unioned = self.record(nested=nested)
        unioned["mates"].append(shaft)
        coincident_pair = {
            "name": "coincident__pair",
            "type": "coincident",
            "suppressed": False,
            "error_code": 0,
            "scope": "head-1",
            "limits": None,
            "entities": [
                {
                    "component": "head-1",
                    "plane": {
                        "point": inverse_point(self.head_rotation, self.head_translation, [0.0, 0.0, 0.0]),
                        "normal": inverse_vector(self.head_rotation, [0.0, 0.0, 1.0]),
                    },
                },
                {
                    "component": "neck-1",
                    "plane": {
                        "point": inverse_point(self.neck_rotation, self.neck_translation, [0.0, 0.0, 0.0]),
                        "normal": inverse_vector(self.neck_rotation, [0.0, 0.0, -1.0]),
                    },
                },
            ],
        }
        unioned["mates"].append(coincident_pair)
        ordinary_equivalent = self.record(nested=None)
        ordinary_equivalent["mates"].append(shaft)
        ordinary_equivalent["mates"].append(coincident_pair)
        ordinary_equivalent["mates"].append(
            {
                **nested,
                "entities": [
                    {"component": "head-1", "plane": dict(nested["entities"][0]["plane"])},
                    dict(nested["entities"][1]),
                ],
            }
        )
        members_a, _ = V._independent_clusters(unioned)
        members_b, _ = V._independent_clusters(ordinary_equivalent)
        self.assertEqual(members_a, members_b)
        self.assertEqual(sorted(members_a["head-1"]), ["head-1", "neck-1"])


if __name__ == "__main__":
    unittest.main()
