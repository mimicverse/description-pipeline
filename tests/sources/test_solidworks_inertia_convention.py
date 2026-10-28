"""Raw dual reads prove the SolidWorks inertia product convention."""

from __future__ import annotations

import copy
import unittest

from description_pipeline.sources.solidworks.errors import BridgeError
from description_pipeline.sources.solidworks.inertia_convention import (
    POSITIVE,
    STANDARD,
    detect_family,
)


def _read(origin_xy: float) -> dict:
    return {
        "mass_kg": 2.0,
        "com_m": [0.1, 0.2, 0.0],
        "inertia_tensor_about_com": [
            [1.0, 0.0, 0.0],
            [0.0, 2.0, 0.0],
            [0.0, 0.0, 3.0],
        ],
        "inertia_values_about_origin_api": [
            1.08,
            origin_xy,
            0.0,
            origin_xy,
            2.02,
            0.0,
            0.0,
            0.0,
            3.10,
        ],
    }


def _links(first: dict, second: dict | None = None) -> dict:
    return {
        "links": {
            "base": {
                "reads": [
                    copy.deepcopy(first),
                    copy.deepcopy(second or first),
                ]
            }
        }
    }


class InertiaConventionTests(unittest.TestCase):
    def test_standard_and_positive_families_are_proven_from_raw_reads(self) -> None:
        standard = detect_family(_links(_read(-0.04)))
        positive = detect_family(_links(_read(0.04)))
        self.assertEqual(standard["family"], STANDARD)
        self.assertEqual(positive["family"], POSITIVE)
        self.assertEqual(standard["decisive"], 2)
        self.assertEqual(positive["decisive"], 2)

    def test_low_signal_cannot_prove_a_family(self) -> None:
        read = _read(-0.04)
        read["com_m"] = [0.0, 0.0, 0.0]
        read["inertia_values_about_origin_api"] = [
            1.0,
            0.0,
            0.0,
            0.0,
            2.0,
            0.0,
            0.0,
            0.0,
            3.0,
        ]
        with self.assertRaisesRegex(BridgeError, "too few decisive"):
            detect_family(_links(read))

    def test_decisive_samples_cannot_vote_for_both_families(self) -> None:
        record = {
            "links": {
                "base": {"reads": [_read(-0.04), _read(-0.04)]},
                "arm": {"reads": [_read(0.04), _read(0.04)]},
            }
        }
        with self.assertRaisesRegex(BridgeError, "disagree"):
            detect_family(record)


if __name__ == "__main__":
    unittest.main()
