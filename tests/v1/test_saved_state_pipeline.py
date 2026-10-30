"""End-to-end saved-state warning projection through Generate and Verify."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import PipelineError, read_data
from description_pipeline.sources.solidworks.saved_state import (
    normalize_open_status,
    same_open_observation,
    warning_from_capture,
    warning_from_selection,
)
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
        self.assertEqual(warning["detail"]["open_observations"]["capture"]["status"]["errors"], 2)
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
        open_document = fixture.MissingReferenceFixtureBackend.open_document

        def incomplete_capture(instance):
            return prepare(instance, incomplete_saved_state=True)

        def additional_capture_diagnostic(instance, path):
            opened = open_document(instance, path)
            opened["missing_references"]["items"].append({
                "name": "another_missing_part",
                "last_known_path": "C:/missing/another_part.SLDPRT",
            })
            return normalize_open_status(opened)

        handler_fixture = PipelineVerifyHandlerTests(methodName="runTest")
        self.addCleanup(handler_fixture.doCleanups)
        with (
            patch.object(fixture.HermeticNativeFixture, "_prepare_and_capture", incomplete_capture),
            patch.object(fixture.MissingReferenceFixtureBackend, "open_document", additional_capture_diagnostic),
        ):
            handler_fixture.setUp()
        report = handler_fixture._bundle_report(handler_fixture.generate_ref)
        self.assertTrue(report["passed"], report)
        warning = next(item for item in report["diagnostics"] if item["code"] == "source.saved_state_incomplete")
        observations = warning["detail"]["open_observations"]
        self.assertEqual(observations["discover"]["status"]["errors"], 2)
        self.assertEqual(observations["capture"]["status"]["errors"], 2)
        self.assertEqual(len(observations["capture"]["status"]["missing_references"]["items"]), 2)
        self.assertEqual(len(observations["discover"]["status"]["missing_references"]["items"]), 1)
        result = handler_fixture._run_verify()
        self.assertEqual(result[1].outcome, "completed")
        urdf = next(handler_fixture.generate_root.rglob("*.urdf")).read_text()
        self.assertIn("WARNING source.saved_state_incomplete", urdf)
        self.assertIn("hidden_or_auto_suppressed_objects_unknown", urdf)
        self.assertIn("another_missing_part", urdf)

    def test_independent_openings_preserve_each_diagnostic_and_clean_observation(self) -> None:
        selection = fixture.incomplete_saved_state_record()["source_selection"]
        missing = selection["closure"]["open_status"]
        clean = normalize_open_status({"errors": 0, "warnings": 0})
        warning_only = normalize_open_status({"errors": 0, "warnings": 64})
        for selected, captured in ((missing, clean), (clean, missing), (warning_only, clean), (clean, warning_only)):
            with self.subTest(selection=selected["classification"], capture=captured["classification"]):
                source = copy.deepcopy(selection)
                source["closure"]["open_status"] = selected
                warning = warning_from_capture(source, captured)
                observations = warning["detail"]["open_observations"]
                self.assertEqual(observations["discover"]["status"], selected)
                self.assertEqual(observations["capture"]["status"], captured)
        source["closure"]["open_status"] = clean
        self.assertIsNone(warning_from_capture(source, clean))

    def test_capture_cannot_borrow_selection_missing_paths_or_hide_severe_errors(self) -> None:
        selection = fixture.incomplete_saved_state_record()["source_selection"]
        for captured in (
            {"errors": 1, "warnings": 0},
            {"errors": 3, "warnings": 0},
            {"errors": 2, "warnings": 0},
            {"errors": 2, "warnings": 0, "missing_references": {"state": "observed", "items": []}},
            {**selection["closure"]["open_status"], "classification": "clean_open"},
        ):
            with self.subTest(captured=captured), self.assertRaises(PipelineError):
                warning_from_capture(selection, captured)

    def test_clean_selection_scope_is_independently_bound_to_raw_occurrences(self) -> None:
        raw = fixture.incomplete_saved_state_record()
        clean = normalize_open_status({"errors": 0, "warnings": 0})
        raw["open_status"] = clean
        raw["source_selection"]["closure"]["open_status"] = clean
        _require_saved_state_open({"raw": raw})
        raw["source_selection"]["closure"]["observed_occurrences"][0]["configuration"] = "other"
        with self.assertRaisesRegex(Exception, "occurrence scope differs"):
            _require_saved_state_open({"raw": raw})

    def test_clean_scope_requires_exact_native_field_types(self) -> None:
        for state in (2.0, True, "2", None, -1, 5):
            raw = fixture.capture_record()
            raw["source_selection"]["closure"]["observed_occurrences"][0]["suppression_state"] = state
            with self.subTest(state=state), self.assertRaisesRegex(Exception, "malformed native fields"):
                _require_saved_state_open({"raw": raw})

    def test_saved_state_discovery_findings_are_readable_by_the_registered_report(self) -> None:
        from description_pipeline.orchestration.report_view import registered_discovery_record
        from description_pipeline.schemas import NATIVE_DISCOVERY_FINDING_SCHEMA

        capture, _native = self.fixture._prepare_and_capture(incomplete_saved_state=True)
        record = read_data(capture / "input/discovery/native-discovery.json")
        validated = registered_discovery_record(record)
        finding = next(item for item in validated["findings"] if item["code"] == "source.saved_state_incomplete")
        warning = warning_from_selection(record["raw"]["source_selection"])
        self.assertEqual(finding["schema_version"], NATIVE_DISCOVERY_FINDING_SCHEMA)
        self.assertEqual(finding["object"], warning["objects"][0]["object"])
        self.assertEqual(finding["detail"], warning["detail"])
        self.assertEqual(finding["message"], warning["message"])
        self.assertFalse(finding["blocking"])

    def test_missing_drive_defaults_reach_actual_urdf_and_independent_verify(self) -> None:
        from tests.v1.test_pipeline_verify import PipelineVerifyHandlerTests

        native = fixture.capture_record()
        native["properties"]["mates"]["shoulder_pitch_joint__coaxial"].pop("dp.joint.drive_record")
        handler_fixture = PipelineVerifyHandlerTests(methodName="runTest")
        self.addCleanup(handler_fixture.doCleanups)
        with patch.object(fixture, "capture_record", return_value=native):
            handler_fixture.setUp()
        report = handler_fixture._bundle_report(handler_fixture.generate_ref)
        self.assertTrue(report["passed"], [item for item in report["checks"] if not item["passed"]])
        defaults = next(item for item in report["diagnostics"] if item["code"] == "drive.default_zero")
        fields = defaults["detail"]["objects"]
        self.assertEqual({item["field"] for item in fields}, {"effort", "velocity"})
        self.assertTrue(all(item["actual"] == 0 and item["drive_qualified"] is False for item in fields))
        self.assertEqual(handler_fixture._run_verify()[1].outcome, "completed")
        urdf = next(handler_fixture.generate_root.rglob("*.urdf")).read_text()
        self.assertIn("WARNING drive.default_zero", urdf)
        self.assertIn('effort="0"', urdf)
        self.assertIn('velocity="0"', urdf)

    def test_scope_changes_cannot_be_hidden_by_a_warning(self) -> None:
        record = fixture.incomplete_saved_state_record()
        _require_saved_state_open({"raw": record})
        record["components"].pop()
        with self.assertRaisesRegex(Exception, "occurrence scope differs"):
            _require_saved_state_open({"raw": record})

    def test_partial_controlled_drive_preserves_provided_value_and_defaults_only_missing_field(self) -> None:
        from tests.v1.test_pipeline_verify import PipelineVerifyHandlerTests

        original_setup = fixture.HermeticNativeFixture.setUp

        def partial_drive(instance):
            original_setup(instance)
            path = instance.records / "joints/arm.json"
            record = json.loads(path.read_text())
            record["drive"] = {"effort": 7.5, "velocity": None}
            path.write_text(json.dumps(record))

        scenario = PipelineVerifyHandlerTests(methodName="runTest")
        self.addCleanup(scenario.doCleanups)
        with patch.object(fixture.HermeticNativeFixture, "setUp", partial_drive):
            scenario.setUp()
        report = scenario._bundle_report(scenario.generate_ref)
        self.assertTrue(report["passed"], [item for item in report["checks"] if not item["passed"]])
        defaults = next(item for item in report["diagnostics"] if item["code"] == "drive.default_zero")
        self.assertEqual([item["field"] for item in defaults["objects"]], ["velocity"])
        authored = next(item for item in report["diagnostics"] if item["code"] == "drive.declared_author")
        self.assertEqual([(item["field"], item["actual"]) for item in authored["objects"]], [("effort", 7.5)])
        self.assertEqual(scenario._run_verify()[1].outcome, "completed")
        urdf = next(scenario.generate_root.rglob("*.urdf")).read_text()
        self.assertIn('effort="7.5"', urdf)
        self.assertIn('velocity="0"', urdf)

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
        self.assertFalse(same_open_observation(status, different))

    def test_capture_cannot_forge_derived_open_classification(self) -> None:
        status = fixture.incomplete_saved_state_record()["open_status"]
        forged = {**status, "classification": "clean_open"}
        with self.assertRaises(ValueError):
            same_open_observation(status, forged)


if __name__ == "__main__":
    unittest.main()
