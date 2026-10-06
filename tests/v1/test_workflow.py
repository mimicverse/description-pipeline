"""Workflow failure/ownership controls; these tests do not qualify native CAD."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks
from description_pipeline.cli import main
from description_pipeline.delivery import BUNDLE_SCHEMA, PIPELINE_ID
from description_pipeline.io import PipelineError
from description_pipeline.sources.solidworks.revision import seal_revision


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.package = self.root / "handoff"
        self.package.mkdir()
        (self.package / "assembly.SLDASM").write_bytes(b"non-native control fixture")
        (self.package / "part.SLDPRT").write_bytes(b"non-native control fixture part")
        definition = {
            "schema_version": "solidworks-to-urdf.input/v1",
            "hardware_id": "arm",
            "source": {
                "provider": "solidworks",
                "robot_name": "arm",
                "assembly": "assembly.SLDASM",
                "configuration": "Default",
                "material_source": "cad",
                "bodies": [
                    {
                        "id": "base",
                        "name": "base_link",
                        "components": ["part-1"],
                        "frame": {"coordinate_system": "base_datum"},
                    },
                    {
                        "id": "arm",
                        "name": "arm_link",
                        "components": ["part-2"],
                        "frame": {"coordinate_system": "arm_datum"},
                    },
                ],
                "joints": [
                    {
                        "id": "base_to_arm",
                        "name": "arm_fixed_joint",
                        "type": "fixed",
                        "parent": "base_link",
                        "child": "arm_link",
                    }
                ],
            },
            "checks": {"expected_mass_kg": [0.01, 1.0], "expected_extent_m": [0.001, 1.0]},
        }
        # JSON is a YAML subset; no SolidWorks capture or green verifier is mocked here.
        (self.package / "robot.yaml").write_text(json.dumps(definition))
        self.output = self.root / "delivery"

    def seal(self):
        seal_revision(
            self.package,
            hardware_id="arm",
            revision="r1",
            owner="mechanical",
            system="handoff",
            reference="fixture/arm/r1",
            summary="Control test",
        )

    def test_inspection_requires_mechanical_revision(self):
        report = solidworks.inspect_input(self.package)
        self.assertFalse(report["passed"])
        self.assertIn("input.cad_revision", [e["code"] for e in report["errors"]])
        self.seal()
        self.assertTrue(solidworks.inspect_input(self.package)["passed"])

    def test_linux_never_opens_native_cad_and_keeps_failure_evidence(self):
        self.seal()
        events = []
        with (
            patch.object(solidworks.sys, "platform", "linux"),
            patch("description_pipeline.sources.solidworks.freeze.freeze") as native,
        ):
            result = solidworks.run(self.package, self.output, on_event=events.append)
        native.assert_not_called()
        self.assertFalse(result["passed"])
        self.assertIn("Windows", result["error"])
        self.assertTrue(result["run_id"])
        self.assertEqual("failed", events[-1]["state"])
        self.assertFalse(self.output.exists())
        self.assertTrue((Path(result["diagnostic_path"]) / "reports/input.json").is_file())

    def test_failed_input_does_not_replace_existing_owned_output(self):
        self.output.mkdir()
        marker = self.output / "README.md"
        marker.write_text("Existing output bytes")
        solidworks._stamp(self.output, {"schema_version": BUNDLE_SCHEMA, "pipeline_id": PIPELINE_ID})
        result = solidworks.run(self.package, self.output)
        self.assertFalse(result["passed"])
        self.assertEqual("Existing output bytes", marker.read_text())
        self.assertTrue(solidworks._owned_output(self.output))

    def test_annotated_diagnostic_is_preserved_on_retry(self):
        first = solidworks.run(self.package, self.output)
        old = Path(first["diagnostic_path"])
        note = old / "operator-note.txt"
        note.write_text("Do not erase this review")
        second = solidworks.run(self.package, self.output)
        self.assertNotEqual(first["diagnostic_path"], second["diagnostic_path"])
        self.assertEqual("Do not erase this review", note.read_text())

    def test_output_and_input_cannot_overlap(self):
        before = (self.package / "robot.yaml").read_bytes()
        with self.assertRaises(PipelineError):
            solidworks.run(self.package, self.package / "output")
        self.assertEqual(before, (self.package / "robot.yaml").read_bytes())

    def test_report_option_cannot_overwrite_author_input(self):
        before = (self.package / "robot.yaml").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = main(["inspect", str(self.package), "--report", str(self.package / "robot.yaml")])
        self.assertEqual(1, result)
        self.assertEqual(before, (self.package / "robot.yaml").read_bytes())

    def test_original_cad_is_archived_without_editing(self):
        self.seal()
        target = self.root / "staging"
        target.mkdir()
        original = (self.package / "assembly.SLDASM").read_bytes()
        solidworks._copy_definition(self.package, target)
        self.assertEqual(original, (target / "input/assembly.SLDASM").read_bytes())
        self.assertEqual(original, (self.package / "assembly.SLDASM").read_bytes())
        self.assertTrue((target / "input/cad-revision.json").is_file())

    def test_concurrent_output_writer_is_refused(self):
        with solidworks.output_lock(self.output), self.assertRaises(PipelineError), solidworks.output_lock(self.output):
            self.fail("Second writer acquired the same output lock")


if __name__ == "__main__":
    unittest.main()
