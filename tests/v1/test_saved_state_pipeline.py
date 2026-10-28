"""End-to-end saved-state warning projection through Generate and Verify."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import read_data
from description_pipeline.sources.solidworks.saved_state import capture_open_matches, warning_from_selection
from description_pipeline.verification.native_discovery import _require_saved_state_open
from description_pipeline.sources.solidworks.captured import derive_engineering_input

from tests.v1 import verification_fixture as fixture


class SavedStatePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture.HermeticNativeFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_missing_reference_saved_state_reaches_generate_input_with_warning(self) -> None:
        capture, _native = self.fixture._prepare_and_capture(
            incomplete_saved_state=True
        )

        manifest = read_data(capture / "evidence/manifest.json")
        with tempfile.TemporaryDirectory(
            prefix="saved-state-generate-meshes-"
        ) as directory:
            scene = derive_engineering_input(
                capture / "evidence",
                manifest,
                mesh_root=Path(directory),
            )
        warning = next(
            item
            for item in scene["provenance"]["value_warnings"]
            if item["code"] == "source.saved_state_incomplete"
        )

        self.assertIs(warning["blocking"], False)
        self.assertEqual(warning["detail"]["open_errors"], 2)
        self.assertFalse(warning["detail"]["source_integrity_proven"])
        self.assertFalse(warning["detail"]["rebuilt"])
        self.assertFalse(warning["detail"]["mechanical_qualification_complete"])
        self.assertFalse(warning["detail"]["hardware_qualified"])
        self.assertEqual(
            warning["detail"]["actual_scope"]["selected_files"],
            ["cad/arm.SLDPRT", "cad/base.SLDPRT", "cad/robot.SLDASM"],
        )


    def test_saved_state_reaches_independent_verify_and_urdf(self) -> None:
        from tests.v1.test_pipeline_verify import PipelineVerifyHandlerTests

        prepare = fixture.HermeticNativeFixture._prepare_and_capture

        def incomplete_capture(instance):
            return prepare(instance, incomplete_saved_state=True)

        handler_fixture = PipelineVerifyHandlerTests(methodName="runTest")
        self.addCleanup(handler_fixture.doCleanups)
        with patch.object(fixture.HermeticNativeFixture, "_prepare_and_capture", incomplete_capture):
            handler_fixture.setUp()
        report = handler_fixture._bundle_report(handler_fixture.generate_ref)
        self.assertTrue(report["passed"], report)
        warning = next(item for item in report["diagnostics"] if item["code"] == "source.saved_state_incomplete")
        self.assertEqual(warning["detail"]["open_errors"], 2)
        result = handler_fixture._run_verify()
        self.assertEqual(result[1].outcome, "completed")
        urdf = next(handler_fixture.generate_root.rglob("*.urdf")).read_text()
        self.assertIn("WARNING source.saved_state_incomplete", urdf)
        self.assertIn("hidden_or_auto_suppressed_objects_unknown", urdf)

    def test_scope_changes_cannot_be_hidden_by_a_warning(self) -> None:
        record = fixture.incomplete_saved_state_record()
        _require_saved_state_open({"raw": record})
        record["components"].pop()
        with self.assertRaisesRegex(Exception, "occurrence scope differs"):
            _require_saved_state_open({"raw": record})

    def test_readable_occurrence_cannot_be_reclassified_as_missing(self) -> None:
        record = fixture.incomplete_saved_state_record()
        component = record["components"].pop()
        record["unresolved_components"].append({
            "instance": component.get("instance_id") or component["name2"],
            "configuration": component["configuration"],
            "suppression_state": component["suppression_state"],
        })
        with self.assertRaisesRegex(Exception, "active unresolved occurrence"):
            _require_saved_state_open({"raw": record})

    def test_scope_counts_are_derived_and_duplicate_native_instances_refuse(self) -> None:
        selection = fixture.incomplete_saved_state_record()["source_selection"]
        self.assertNotIn("actual_scope", selection["closure"]["open_status"])
        warning = warning_from_selection(selection)
        self.assertEqual(warning["detail"]["actual_scope"]["occurrence_count"], 2)
        forged = copy.deepcopy(selection)
        occurrences = forged["closure"]["observed_occurrences"]
        occurrences.append(occurrences[0])
        with self.assertRaises(ValueError):
            warning_from_selection(forged)

    def test_same_error_bits_cannot_hide_different_missing_reference_sources(self) -> None:
        status = fixture.incomplete_saved_state_record()["open_status"]
        different = copy.deepcopy(status)
        different["missing_references"]["items"][0]["last_known_path"] = "C:/other/missing.SLDPRT"
        self.assertFalse(capture_open_matches(status, different))

    def test_capture_cannot_forge_derived_open_classification(self) -> None:
        status = fixture.incomplete_saved_state_record()["open_status"]
        forged = {**status, "classification": "clean_open"}
        with self.assertRaises(ValueError):
            capture_open_matches(status, forged)


if __name__ == "__main__":
    unittest.main()
