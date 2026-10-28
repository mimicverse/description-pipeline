"""Coordinate transforms never enter the state-changing selection-access API."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from description_pipeline.sources.solidworks.native import SolidWorksBackend
from tests.sources.test_solidworks_native_reader import (
    SW_IDENTITY,
    _App,
    _Doc,
    _Feature,
    _Session,
    _com_stubs,
    _raiser,
    _write,
)


class CoordinateSystemReferenceTests(unittest.TestCase):
    def test_definition_ownership_is_explicitly_unread_without_access_selections(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(
                assembly,
                coordinate_systems={"CS_tip": SW_IDENTITY},
                first_feature=_Feature(
                    "CS_tip",
                    "CoordSys",
                    definition=_raiser(AssertionError("production must not acquire selection-access data")),
                ),
            )
            backend = SolidWorksBackend(session_factory=lambda: _Session(_App({assembly: doc})))
            with _com_stubs():
                record = backend.discover_native(root, {})
            datum = record["datums"][0]
            self.assertEqual(record["saved_configuration"], "Default")
            self.assertIsNone(record["identity"]["delivery_configuration"])
            self.assertEqual(
                datum["array_raw"],
                list(SW_IDENTITY),
            )
            refs = datum["references"]
            self.assertEqual(refs["state"], "unread")
            self.assertIn("saved state", refs["reason"])
            self.assertEqual(refs["context"], {"document": "robot.SLDASM", "configuration": "Default", "scope": ""})
            self.assertNotIn("origin", refs)
            self.assertNotIn("x_axis", refs)


if __name__ == "__main__":
    unittest.main()
