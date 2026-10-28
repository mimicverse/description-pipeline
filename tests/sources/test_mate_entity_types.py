"""Native mate-entity reference type semantics."""

from __future__ import annotations

import unittest

from description_pipeline.sources.solidworks.discovery import (
    _projected_entity_geometry,
)
from description_pipeline.verification.native_discovery import (
    _verification_entity_geometry,
)


class MateEntityTypeTests(unittest.TestCase):
    def test_reference_types_follow_sw_mate_entity_types(self) -> None:
        supported = {
            1: (
                [1.0, 2.0, 3.0],
                {
                    "kind": "point",
                    "point": [1.0, 2.0, 3.0],
                },
            ),
            2: (
                [1.0, 2.0, 3.0, 0.0, 1.0, 0.0, 0.0, 0.0],
                {
                    "kind": "line",
                    "point": [1.0, 2.0, 3.0],
                    "direction": [0.0, 1.0, 0.0],
                },
            ),
            3: (
                [1.0, 2.0, 3.0, 0.0, 0.0, 1.0, 0.0, 0.0],
                {
                    "kind": "plane",
                    "point": [1.0, 2.0, 3.0],
                    "normal": [0.0, 0.0, 1.0],
                },
            ),
            4: (
                [1.0, 2.0, 3.0, 0.0, 0.0, 1.0, 0.01, 0.0],
                {
                    "kind": "cylinder",
                    "point": [1.0, 2.0, 3.0],
                    "direction": [0.0, 0.0, 1.0],
                    "radius": 0.01,
                },
            ),
            5: (
                [1.0, 2.0, 3.0, 0.0, 0.0, 1.0, 0.01, 0.02],
                {
                    "kind": "cone",
                    "point": [1.0, 2.0, 3.0],
                    "direction": [0.0, 0.0, 1.0],
                    "radius": 0.01,
                    "radius2": 0.02,
                },
            ),
        }
        for reference_type, (params, expected) in supported.items():
            with self.subTest(reference_type=reference_type):
                producer = _projected_entity_geometry(reference_type, params)
                verifier = _verification_entity_geometry(reference_type, params)
                self.assertEqual(producer, (expected, None))
                self.assertEqual(verifier, (expected, None))

        unsupported = {
            0: ("Unsupported", "swMateUnsupported"),
            6: ("Sphere", "swMateSphere"),
            7: ("Circle", "swMateCircle"),
        }
        params = [1.0, 2.0, 3.0, 0.0, 0.0, 1.0, 0.01, 0.0]
        for reference_type, (type_name, reason_prefix) in unsupported.items():
            with self.subTest(reference_type=reference_type):
                reason = (
                    "swMateUnsupported carries no mate entity geometry"
                    if reference_type == 0
                    else f"swMate{type_name} local geometry and rank layout are not verified"
                )
                expected = (
                    {
                        "kind": "unsupported",
                        "type": type_name,
                        "reason": reason,
                    },
                    None,
                )
                self.assertIn(reason_prefix, reason)
                self.assertEqual(
                    _projected_entity_geometry(reference_type, params),
                    expected,
                )
                self.assertEqual(
                    _verification_entity_geometry(reference_type, params),
                    expected,
                )

        outside = _projected_entity_geometry(99, params)
        self.assertIsNone(outside[0])
        self.assertIn("outside swMateEntityTypes_e", outside[1])


if __name__ == "__main__":
    unittest.main()
