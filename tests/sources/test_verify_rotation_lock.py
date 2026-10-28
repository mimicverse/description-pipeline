"""Locked concentric rigidity is bound to its documented native API witness."""

import unittest

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.verification.native_discovery import LOCK_ROTATION_API, _rows_for, _span  # noqa: E402

IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


class VerifierRotationLockTests(unittest.TestCase):
    def setUp(self):
        self.frames = {"base": [list(row) for row in IDENTITY], "washer": [list(row) for row in IDENTITY]}
        self.mate = {
            "name": "coaxial__concentric",
            "type": "concentric",
            "entity_count": 2,
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [
                {
                    "component": name,
                    "feature": f"Edge{index}",
                    "circle": {"center": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0], "radius": 0.01},
                }
                for index, name in enumerate(self.frames)
            ],
            "lock_rotation": True,
            "lock_rotation_api": LOCK_ROTATION_API,
        }

    def rows(self, mate):
        view = _rows_for(mate, self.frames)
        self.assertIsNotNone(view)
        return view["rows"]

    def plane_mate(self):
        return {
            "name": "seated__coincident",
            "type": "coincident",
            "entity_count": 2,
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [
                {
                    "component": name,
                    "feature": f"Face{index}",
                    "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0 if index == 0 else -1.0]},
                }
                for index, name in enumerate(self.frames)
            ],
        }

    def test_locked_concentric_is_axial_only_and_reaches_rank_six_with_its_plane(self):
        rows = self.rows(self.mate)
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(_span(rows)), 5)
        self.assertEqual(len(_span(rows + self.rows(self.plane_mate()))), 6)
        lock_row = rows[-1]
        self.assertEqual(lock_row[:3], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(abs(lock_row[5]), 1.0, places=12)

    def test_lock_requires_true_and_the_documented_witness(self):
        for label, value, witness in (
            ("free", False, LOCK_ROTATION_API),
            ("unread", None, LOCK_ROTATION_API),
            ("boolean_int", 1, LOCK_ROTATION_API),
            ("string_true", "true", LOCK_ROTATION_API),
            ("missing_witness", True, None),
            ("guessed_witness", True, LOCK_ROTATION_API + "-guess"),
        ):
            with self.subTest(case=label):
                mate = {**self.mate, "lock_rotation": value}
                if witness is None:
                    mate.pop("lock_rotation_api")
                else:
                    mate["lock_rotation_api"] = witness
                self.assertEqual(len(_span(self.rows(mate))), 4)
        bare = {key: value for key, value in self.mate.items() if key not in {"lock_rotation", "lock_rotation_api"}}
        self.assertEqual(len(_span(self.rows(bare))), 4)


if __name__ == "__main__":
    unittest.main()
