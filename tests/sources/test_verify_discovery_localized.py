"""Independent verifier: strict localized-provenance fallback for mate references.

The source reader localizes decoded mate references under ``mate_entity_reference``;
the verifier must reproduce the supported scope without sharing source code: line-line
parallel and line-plane coincident only, recorded face evidence stays primary, failed
localization (``error``) is never trusted, conflicting or malformed provenance blocks,
and a localized cylinder is never accepted without matching face evidence.  A present
but unusable recorded primary (a non-dict face, or a ``point`` that is not a bare
finite 3-vector) is refused outright and is never substituted by provenance, and the
flat face kinds are the only recorded evidence: line references arrive exclusively
through validated localization.
"""

from __future__ import annotations

import math
import unittest

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.verification import native_discovery as V  # noqa: E402

IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def rotation_z(angle: float):
    cosine, sine = math.cos(angle), math.sin(angle)
    return [
        [cosine, -sine, 0.0, 0.0],
        [sine, cosine, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


FRAMES = {"A": IDENTITY, "B": rotation_z(math.pi / 2)}


def entity(component: str, **values) -> dict:
    return {"component": component, **values}


def localized(kind: str, *, frame: str | None = "component-local", error=None, **geometry) -> dict:
    reference: dict = {"geometry": {"kind": kind, **({} if frame is None else {"frame": frame}), **geometry}}
    if error is not None:
        reference["error"] = error
    return {"mate_entity_reference": reference}


def mate(kind: str, first: dict, second: dict) -> dict:
    return {"name": "m1", "type": kind, "entities": [first, second]}


def rows(mate_item: dict, frames=None):
    return V._rows_for(mate_item, FRAMES if frames is None else frames)


def rank(result: dict) -> int:
    return 6 - len(V._null_space(result["rows"]))


class LocalizedFallbackScopeTests(unittest.TestCase):
    def test_line_line_parallel_uses_localized_directions_through_the_occurrence(self) -> None:
        # B is rotated 90 degrees about Z: its local +Y is the assembly -X, parallel to A.
        item = mate(
            "parallel",
            entity("A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])),
            entity("B", **localized("line", point=[0.0, 0.0, 0.0], direction=[0.0, 1.0, 0.0])),
        )
        result = rows(item)
        self.assertIsNotNone(result)
        self.assertEqual(rank(result), 2)

    def test_line_line_parallel_accepts_antiparallel_directions(self) -> None:
        item = mate(
            "parallel",
            entity("A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])),
            entity("A", **localized("line", point=[0.0, 0.0, 0.0], direction=[-1.0, 0.0, 0.0])),
        )
        result = rows(item)
        self.assertIsNotNone(result)
        self.assertEqual(rank(result), 2)

    def test_plane_plane_parallel_still_uses_localized_normals(self) -> None:
        item = mate(
            "parallel",
            entity("A", **localized("plane", point=[0.0, 0.0, 0.0], normal=[1.0, 0.0, 0.0])),
            entity("A", **localized("plane", point=[0.0, 0.0, 0.0], normal=[1.0, 0.0, 0.0])),
        )
        result = rows(item)
        self.assertIsNotNone(result)
        self.assertEqual(rank(result), 2)

    def test_line_plane_parallel_is_one_rotational_freedom(self) -> None:
        item = mate(
            "parallel",
            entity("A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])),
            entity("A", **localized("plane", point=[0.0, 0.0, 0.0], normal=[0.0, 0.0, 1.0])),
        )
        result = rows(item)
        self.assertIsNotNone(result)
        self.assertEqual(rank(result), 1)

    def test_line_plane_parallel_solved_state_uses_the_module_tolerance(self) -> None:
        # The solved-state check follows the module's 1e-6 convention; the 0.05-degree
        # bound belongs to the face-evidence crosscheck only.
        def line_plane(direction):
            return mate(
                "parallel",
                entity("A", **localized("line", point=[0.0, 0.0, 0.0], direction=direction)),
                entity("A", **localized("plane", point=[0.0, 0.0, 0.0], normal=[0.0, 0.0, 1.0])),
            )

        self.assertIsNone(rows(line_plane([1.0, 0.0, 1e-5])))
        accepted = rows(line_plane([1.0, 0.0, 1e-7]))
        self.assertIsNotNone(accepted)
        self.assertEqual(rank(accepted), 1)

    def test_line_plane_coincident_requires_a_solved_coplanar_line(self) -> None:
        item = mate(
            "coincident",
            entity("A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])),
            entity("A", **localized("plane", point=[0.0, 0.0, 0.0], normal=[0.0, 0.0, 1.0])),
        )
        result = rows(item)
        self.assertIsNotNone(result)
        self.assertEqual(rank(result), 2)

    def test_point_fallback_serves_point_coincidence(self) -> None:
        item = mate(
            "coincident",
            entity("A", **localized("point", point=[0.0, 0.0, 0.0])),
            entity("A", **localized("point", point=[0.0, 0.0, 0.01])),
        )
        result = rows(item)
        self.assertIsNotNone(result)
        self.assertEqual(rank(result), 3)

    def test_localized_cylinder_agrees_with_face_evidence_and_flips_freely(self) -> None:
        cylinder = {"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.01}
        item = mate(
            "concentric",
            entity("A", cylinder=dict(cylinder), **localized("cylinder", **cylinder)),
            entity(
                "B",
                cylinder=dict(cylinder),
                **localized("cylinder", point=[0.0, 0.0, 0.0], direction=[0.0, 0.0, -1.0], radius=0.01),
            ),
        )
        result = rows(item)
        self.assertIsNotNone(result)
        self.assertEqual(rank(result), 4)

    def test_failed_localization_leaves_a_valid_face_primary(self) -> None:
        line = entity("A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]))
        plane = entity("A", **localized("plane", point=[0.0, 0.0, 0.0], normal=[1.0, 0.0, 0.0]))
        # A line perpendicular to the plane can never read as parallel to it.
        self.assertIsNone(rows(mate("parallel", line, plane)))
        failed = entity(
            "A",
            plane={"point": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]},
            **localized("plane", error="decode failed", point=[0.0, 0.0, 0.5], normal=[0.0, 1.0, 0.0]),
        )
        other = entity("A", plane={"point": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]})
        result = rows(mate("parallel", failed, other))
        self.assertIsNotNone(result)  # the recorded plane stays primary despite the failure
        self.assertEqual(rank(result), 2)

    def test_malformed_or_failed_provenance_never_blocks_valid_face_evidence(self) -> None:
        plane = {"point": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]}
        other_plane = entity("A", plane={"point": [0.0, 0.0, 0.0], "normal": [1.0, 0.0, 0.0]})
        broken = {
            "missing normal": entity("A", plane=dict(plane), **localized("plane", point=[0.0, 0.0, 0.0])),
            "zero direction": entity(
                "A",
                plane=dict(plane),
                **localized("line", point=[0.0, 0.0, 0.0], direction=[0.0, 0.0, 0.0]),
            ),
            "boolean radius": entity(
                "A",
                cylinder={"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.01},
                **localized("cylinder", point=[0.0, 0.0, 0.0], direction=[0.0, 0.0, 1.0], radius=True),
            ),
            "unlocalized frame": entity(
                "A",
                plane=dict(plane),
                **localized("plane", frame="assembly", point=[0.0, 0.0, 0.0], normal=[1.0, 0.0, 0.0]),
            ),
            "failed decode": entity(
                "A",
                plane=dict(plane),
                **localized("plane", error="decode failed", point=[0.0, 0.0, 0.0], normal=[0.0, 1.0, 0.0]),
            ),
        }
        for name, broken_entity in broken.items():
            with self.subTest(case=name):
                result = rows(mate("parallel", broken_entity, other_plane))
                self.assertIsNotNone(result)
                self.assertEqual(rank(result), 2)

    def test_tampered_localized_provenance_is_rejected(self) -> None:
        line = lambda **values: entity("A", **localized("line", **values))  # noqa: E731
        cases = {
            "unlocalized frame": (
                mate(
                    "parallel",
                    entity(
                        "A",
                        **localized(
                            "line", frame="assembly", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]
                        ),
                    ),
                    entity("A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])),
                )
            ),
            "missing frame": (
                mate(
                    "parallel",
                    entity("A", **localized("line", frame=None, point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])),
                    entity("A", **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])),
                )
            ),
            "zero direction": (
                mate(
                    "parallel",
                    line(point=[0.0, 0.0, 0.0], direction=[0.0, 0.0, 0.0]),
                    line(point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]),
                )
            ),
            "missing point": (
                mate(
                    "parallel",
                    line(direction=[1.0, 0.0, 0.0]),
                    line(point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]),
                )
            ),
            "not parallel solved state": (
                mate(
                    "parallel",
                    line(point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]),
                    line(point=[0.0, 0.0, 0.0], direction=[0.0, 1.0, 0.0]),
                )
            ),
            "conflicting recorded plane": (
                mate(
                    "coincident",
                    entity(
                        "A",
                        plane={"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0]},
                        **localized("plane", point=[0.0, 0.0, 0.5], normal=[0.0, 0.0, 1.0]),
                    ),
                    entity("A", **localized("plane", point=[0.0, 0.0, 0.0], normal=[0.0, 0.0, 1.0])),
                )
            ),
            "line not in the plane": (
                mate(
                    "coincident",
                    line(point=[0.0, 0.0, 0.0], direction=[0.0, 0.0, 1.0]),
                    entity("A", **localized("plane", point=[0.0, 0.0, 0.0], normal=[0.0, 0.0, 1.0])),
                )
            ),
            "line offset from the plane": (
                mate(
                    "coincident",
                    line(point=[0.0, 0.0, 0.001], direction=[1.0, 0.0, 0.0]),
                    entity("A", **localized("plane", point=[0.0, 0.0, 0.0], normal=[0.0, 0.0, 1.0])),
                )
            ),
            "fabricated localized cylinder": (
                mate(
                    "concentric",
                    entity(
                        "A",
                        **localized(
                            "cylinder", point=[0.0, 0.0, 0.0], direction=[0.0, 0.0, 1.0], radius=0.01
                        ),
                    ),
                    entity("A", cylinder={"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.01}),
                )
            ),
            "cylinder radius conflict": (
                mate(
                    "concentric",
                    entity(
                        "A",
                        cylinder={"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.02},
                        **localized(
                            "cylinder", point=[0.0, 0.0, 0.0], direction=[0.0, 0.0, 1.0], radius=0.01
                        ),
                    ),
                    entity("A", cylinder={"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.02}),
                )
            ),
            "line outside angle scope": (
                mate(
                    "angle",
                    line(point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]),
                    line(point=[0.0, 0.0, 0.0], direction=[0.0, 1.0, 0.0]),
                )
            ),
            "reference is not an object": (
                mate(
                    "parallel",
                    entity("A", mate_entity_reference="broken"),
                    line(point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]),
                )
            ),
        }
        for name, item in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(rows(item))

    def test_malformed_recorded_primary_is_refused_never_substituted(self) -> None:
        # A present recorded primary that cannot be read is malformed evidence: it must
        # fail closed even when valid localized provenance could otherwise rescue it.
        plane = {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}
        other_plane = entity("A", plane=dict(plane))
        malformed = {
            "boolean point": entity(
                "A", point=[0.0, True, 0.0], **localized("point", point=[0.0, 0.0, 0.0])
            ),
            "non-finite point": entity(
                "A", point=[0.0, float("nan"), 0.0], **localized("point", point=[0.0, 0.0, 0.0])
            ),
            "short point": entity(
                "A", point=[0.0, 0.0], **localized("point", point=[0.0, 0.0, 0.0])
            ),
            "dict point": entity(
                "A", point={"x": 0.0, "y": 0.0, "z": 0.0}, **localized("point", point=[0.0, 0.0, 0.0])
            ),
            "string point": entity(
                "A", point="junk", **localized("point", point=[0.0, 0.0, 0.0])
            ),
            "string plane": entity(
                "A", plane="junk", **localized("plane", normal=[0.0, 1.0, 0.0], point=[0.0, 0.0, 0.0])
            ),
            "string cylinder": entity(
                "A", cylinder="junk", **localized("plane", normal=[0.0, 1.0, 0.0], point=[0.0, 0.0, 0.0])
            ),
            "string circle": entity(
                "A", circle="junk", **localized("plane", normal=[0.0, 1.0, 0.0], point=[0.0, 0.0, 0.0])
            ),
            "junk point beside a valid plane": entity("A", plane=dict(plane), point="junk"),
            "boolean normal": entity(
                "A",
                plane={"normal": [0.0, True, 0.0], "point": [0.0, 0.0, 0.0]},
                **localized("plane", normal=[0.0, 1.0, 0.0], point=[0.0, 0.0, 0.0]),
            ),
        }
        for name, broken in malformed.items():
            with self.subTest(case=name):
                self.assertIsNone(rows(mate("coincident", broken, other_plane)))
        # The refusal is a gate, not a branch: mates that never read geometry still block.
        broken = malformed["string point"]
        self.assertIsNone(rows(mate("limitangle", broken, other_plane)))
        self.assertIsNone(rows(mate("lock", broken, other_plane)))

    def test_localized_kind_must_be_comparable_to_the_recorded_primary(self) -> None:
        # A validated localization of a different comparable kind contradicts the
        # recorded primary and blocks, mirroring the source's entity view.
        plane = {"normal": [0.0, 1.0, 0.0], "point": [0.0, 0.0, 0.0]}
        other_plane = entity("A", plane=dict(plane))
        contradicting = {
            "line against a recorded plane": entity(
                "A",
                plane=dict(plane),
                **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]),
            ),
            "line against a recorded point": entity(
                "A",
                point=[0.0, 0.0, 0.0],
                **localized("line", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]),
            ),
            "plane against a recorded circle": entity(
                "A",
                circle={"center": [0.0, 0.0, 0.0], "normal": [0.0, 1.0, 0.0], "radius": 0.003},
                **localized("plane", normal=[0.0, 1.0, 0.0], point=[0.0, 0.0, 0.0]),
            ),
        }
        for name, broken in contradicting.items():
            with self.subTest(case=name):
                self.assertIsNone(rows(mate("coincident", broken, other_plane)))
        # A localized point never displaces the recorded face it accompanies: the
        # recorded plane still drives the coincident constraint.
        accompanied = entity("A", plane=dict(plane), **localized("point", point=[0.0, 0.0, 0.0]))
        result = rows(mate("coincident", accompanied, other_plane))
        self.assertIsNotNone(result)
        self.assertEqual(rank(result), 3)

    def test_flat_line_keys_are_not_recorded_evidence(self) -> None:
        # The reader records line references only through localization; a stray flat
        # ``line`` key is last-resort evidence at most and never overrides validated
        # provenance.
        stray = {"point": [0.0, 0.0, 0.0], "direction": [1.0, 0.0, 0.0]}

        def localized_line(**values) -> dict:
            return entity("A", **localized("line", **values))

        overridden = rows(
            mate(
                "parallel",
                entity(
                    "A",
                    line=dict(stray),
                    **localized("line", point=[0.0, 0.0, 0.0], direction=[0.0, 1.0, 0.0]),
                ),
                localized_line(point=[0.0, 0.0, 0.0], direction=[0.0, 1.0, 0.0]),
            )
        )
        self.assertIsNotNone(overridden)  # the validated localization wins
        self.assertEqual(rank(overridden), 2)
        only = rows(
            mate(
                "parallel",
                entity("A", line=dict(stray)),
                localized_line(point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]),
            )
        )
        self.assertIsNotNone(only)  # last-resort evidence still serves mates without provenance
        self.assertEqual(rank(only), 2)
        malformed = entity(
            "A",
            line=dict(stray),
            **localized("line", point=[0.0, 0.0, 0.0], direction=[0.0, 0.0]),
        )
        refused = rows(
            mate(
                "parallel",
                malformed,
                localized_line(point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0]),
            )
        )
        self.assertIsNone(refused)  # a malformed localization never falls back to a stray key


if __name__ == "__main__":
    unittest.main()
