"""A concentric rotation lock removes spin, not axial translation."""

import copy
import unittest

from . import _paths  # noqa: F401

from description_pipeline.sources.solidworks.discovery import _mate_rows, _rank


class ConcentricRotationLockTests(unittest.TestCase):
    def setUp(self):
        self.frames = {name: [[1., 0., 0., 0.], [0., 1., 0., 0.], [0., 0., 1., 0.], [0., 0., 0., 1.]]
                       for name in ("base", "washer")}
        self.mate = {
            "name": "coaxial", "type": "concentric", "entities": [
                {"component": name, "circle": {"center": [0., 0., 0.], "normal": [0., 0., 1.], "radius": .01}}
                for name in self.frames
            ],
        }

    def rows(self, mate):
        findings = []
        rows = _mate_rows(mate, self.frames, findings, "test")["rows"]
        self.assertEqual(findings, [])
        return rows

    def test_locked_concentric_retains_axial_slide(self):
        self.mate["lock_rotation"] = True
        rows = self.rows(self.mate)
        self.assertEqual(_rank(rows), 5)
        self.assertTrue(all(abs(row[2]) < 1e-12 for row in rows))
        plane = copy.deepcopy(self.mate)
        plane["type"] = "coincident"
        for entity in plane["entities"]:
            entity.pop("circle")
            entity["plane"] = {"point": [0., 0., 0.], "normal": [0., 0., 1.]}
        self.assertEqual(_rank(rows + self.rows(plane)), 6)

    def test_only_boolean_true_is_rotation_lock_evidence(self):
        for value in (None, False, 1, "true"):
            with self.subTest(value=value):
                self.mate["lock_rotation"] = value
                self.assertEqual(_rank(self.rows(self.mate)), 4)
