"""Focused admin-deferral tests for the bundle verifier (no CAD, no pipeline)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.io import PipelineError
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.verification import solidworks_urdf


class InputSealReadinessTests(unittest.TestCase):
    def _root(self, directory, definition):
        root = Path(directory)
        input_root = root / "input"
        input_root.mkdir()
        (input_root / "robot.SLDASM").write_bytes(b"native bytes for the admin-deferral tests")
        (root / "reports").mkdir()
        (root / "reports/input.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "input": definition,
                    "package_files": package_inventory(input_root),
                }
            )
        )
        return root, input_root

    def test_absent_seal_is_a_readiness_warning_only_for_incomplete_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = {"hardware_id": None, "source": {"robot_name": "robot"}}
            root, _ = self._root(directory, definition)
            diagnostics: list[dict] = []
            with mock.patch.object(
                solidworks_urdf, "inspect_package", return_value={"passed": True, "input": definition}
            ):
                returned = solidworks_urdf._input(root, diagnostics)
            self.assertEqual(returned, definition)
            self.assertEqual([item["code"] for item in diagnostics], ["input.cad_revision_not_sealed"])
            self.assertTrue(all(item["blocking"] is False for item in diagnostics))

    def test_absent_seal_fails_for_a_fully_identified_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = {"hardware_id": "fixture", "source": {"robot_name": "fixture"}}
            root, _ = self._root(directory, definition)
            with mock.patch.object(
                solidworks_urdf, "inspect_package", return_value={"passed": True, "input": definition}
            ), self.assertRaises(PipelineError):
                solidworks_urdf._input(root, [])

    def test_supplied_malformed_seal_keeps_failing_with_incomplete_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = {"hardware_id": None, "source": {"robot_name": "robot"}}
            root, input_root = self._root(directory, definition)
            (input_root / "cad-revision.json").write_text(json.dumps({"schema_version": "not-the-contract"}))
            with mock.patch.object(
                solidworks_urdf, "inspect_package", return_value={"passed": True, "input": definition}
            ), self.assertRaises(PipelineError):
                solidworks_urdf._input(root, [])


class UrdfRobotLabelTests(unittest.TestCase):
    @staticmethod
    def _root(directory, name):
        root = Path(directory)
        (root / "urdf").mkdir()
        (root / "urdf/robot.urdf").write_text(f'<robot name="{name}"><link name="base"/></robot>')
        return root

    def test_robot_tag_must_match_the_definition_robot_name(self):
        model = {"links": [{"name": "base"}], "frames": [], "joints": []}
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, "robot")
            definition = {"hardware_id": None, "source": {"robot_name": "robot"}}
            document = solidworks_urdf._xml(root, definition, model)
            self.assertEqual(document.get("name"), "robot")
        with tempfile.TemporaryDirectory() as directory:
            # A hardware label is not the URDF identity anymore.
            root = self._root(directory, "fixture")
            definition = {"hardware_id": "fixture", "source": {"robot_name": "robot"}}
            with self.assertRaises(PipelineError):
                solidworks_urdf._xml(root, definition, model)


class HandoffHashBindingTests(unittest.TestCase):
    def test_archived_native_evidence_binds_handoff_bytes_without_a_seal(self):
        with tempfile.TemporaryDirectory() as directory:
            input_root = Path(directory)
            (input_root / "discovery").mkdir()
            (input_root / "discovery/native-discovery.json").write_text(
                json.dumps(
                    {
                        "native_files": {"a.SLDPRT": "a" * 64},
                        "raw": {"files": {"b.SLDPRT": "b" * 64}},
                    }
                )
            )
            self.assertEqual(solidworks_urdf._handoff_native_hashes(input_root), {"a" * 64, "b" * 64})

    def test_missing_or_empty_archived_evidence_fails(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises((PipelineError, OSError)):
            solidworks_urdf._handoff_native_hashes(Path(directory))
        with tempfile.TemporaryDirectory() as directory:
            input_root = Path(directory)
            (input_root / "discovery").mkdir()
            (input_root / "discovery/native-discovery.json").write_text(json.dumps({"native_files": {}}))
            with self.assertRaises(PipelineError):
                solidworks_urdf._handoff_native_hashes(input_root)


if __name__ == "__main__":
    unittest.main()
