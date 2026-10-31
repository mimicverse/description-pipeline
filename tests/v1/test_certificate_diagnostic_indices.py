"""Certificate observation ownership and shared read-boundary regressions."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from description_pipeline.io import PipelineError
from description_pipeline.pipeline.verify import _certificate_checks
from description_pipeline.pipeline.verify_contract import (
    certificate_check_details,
    validate_certificate_diagnostics, verification_diagnostics,
)
from description_pipeline.orchestration.portal import PortalError, _quality_warnings
from description_pipeline.orchestration.report_view import build_report
from description_pipeline.pipeline.stages.publish import _publication_warning_text
from description_pipeline.stages import stage_view
from description_pipeline.verification.solidworks_urdf import _native_discovery
from tests.v1.test_native_scope_warning_projection import quality_report, raw_warnings


class CertificateDiagnosticIndexTests(unittest.TestCase):
    def test_complete_extensions_are_stored_once_and_keep_check_ownership(self):
        observation = {"code": "cad.observation", "blocking": False,
                       "detail": {"stderr": "native source extension", "objects": [{"object": "arm-1"}]}}
        report = {"diagnostics": [], "checks": [{"id": "one", "details": {
            "warnings": [observation, copy.deepcopy(observation)],
            "diagnostics": [observation], "stderr": "process output",
        }}]}
        inventory = verification_diagnostics(report)
        checks = _certificate_checks(report["checks"], inventory)
        certificate = {"diagnostics": inventory, "checks": checks}
        validate_certificate_diagnostics(certificate)
        self.assertEqual(inventory, [observation])
        self.assertEqual(checks[0]["details"], {"warning_indices": [0], "diagnostic_indices": [0]})
        decoded = certificate_check_details(certificate, checks[0])
        self.assertEqual(decoded["warnings"], [observation])
        self.assertEqual(decoded["diagnostics"], [observation])
        self.assertEqual(report["checks"][0]["details"]["stderr"], "process output")

    def test_reference_types_bounds_and_duplicates_are_rejected_everywhere(self):
        original = quality_report(raw_warnings())
        for indices in (True, None, "0", [True], [-1], [1], [0, 0], [[0]]):
            with self.subTest(indices=indices):
                invalid = copy.deepcopy(original)
                invalid["checks"][0]["details"]["warning_indices"] = indices
                with self.assertRaises(PipelineError):
                    validate_certificate_diagnostics(invalid)
                with self.assertRaises(PortalError):
                    _quality_warnings(invalid)
                with self.assertRaises(PipelineError):
                    _publication_warning_text(invalid)
                with self.assertRaises(PipelineError):
                    build_report({"result": {"quality": invalid}})

    def test_embedded_old_records_are_rejected_without_a_compatibility_path(self):
        certificate = quality_report(raw_warnings())
        certificate["checks"][0]["details"]["warnings"] = certificate["diagnostics"]
        with self.assertRaisesRegex(PipelineError, "embeds"):
            validate_certificate_diagnostics(certificate)

    def test_top_level_duplicates_and_missing_native_membership_are_rejected(self):
        for change in ("duplicate", "missing_reference", "missing_gate"):
            with self.subTest(change=change):
                certificate = quality_report(raw_warnings())
                if change == "duplicate":
                    certificate["diagnostics"].append(copy.deepcopy(certificate["diagnostics"][0]))
                elif change == "missing_reference":
                    certificate["checks"][0]["details"]["warning_indices"] = []
                else:
                    certificate["checks"] = []
                with self.assertRaises(PipelineError):
                    validate_certificate_diagnostics(certificate)

    def test_full_seven_stage_report_attributes_native_warnings_only_to_verify(self):
        certificate = quality_report(raw_warnings())
        certificate["passed"] = True
        view = stage_view()
        for stage in view["stages"]:
            stage.update(state="completed", in_scope=True, input_qc=[], output_qc=[])
        report = build_report({"result": {"quality": certificate}}, view=view)
        self.assertEqual(len(report["stages"]), 7)
        self.assertNotIn("items", report["warnings"])
        for stage in report["stages"]:
            self.assertEqual(stage["counts"]["warnings"], 1 if stage["id"] == "verify" else 0)
        self.assertEqual(len(_quality_warnings(certificate)), 1)
        self.assertIn("cad_scope_local_identity_derived", _publication_warning_text(certificate))

    def test_bounded_display_keeps_every_category_count_and_located_examples(self):
        from description_pipeline.io import canonical
        from description_pipeline.orchestration.report_view import warning_summaries

        records = [{"code": f"source.warning_{index:02d}", "message": message,
                    "blocking": False, "objects": [
                        {"object": f"part-{part}", "field": "appearance", "source": "native",
                         "original": "x" * 10000} for part in range(40)
                    ]} for index in range(12) for message in ("First semantic category", "Second semantic category")]
        before = copy.deepcopy(records)
        summaries = warning_summaries(records)
        self.assertEqual(len(summaries), 24)
        self.assertEqual({(row["code"], row["message"]) for row in summaries},
                         {(row["code"], row["message"]) for row in records})
        for summary in summaries:
            self.assertEqual(summary["records_total"], 1)
            self.assertEqual(summary["source_rows_total"], 40)
            self.assertEqual(summary["source_rows_shown"], 8)
            self.assertEqual(summary["source_rows_omitted"], 32)
            self.assertEqual(len(summary["objects"]), 8)
            example = summary["objects"][0]
            self.assertEqual(example["object"], "part-0")
            self.assertEqual(example["source"], "native")
            self.assertGreater(example["original"]["omitted_characters"], 9000)
            self.assertEqual(example["original"]["complete_record"], {"record": "registered source record"})
        self.assertLess(len(canonical(summaries)), 160000)
        self.assertEqual(records, before)

    def test_large_capture_details_are_validated_before_display_excerpting(self):
        warning = {"code": "input.located_capture_warning", "message": "Frozen native source warning",
                   "blocking": False, "objects": [{"object": "part-17", "field": "native_name",
                   "source": "sealed_native_input", "original": "x" * 5000}]}
        view = stage_view()
        capture = next(stage for stage in view["stages"] if stage["id"] == "capture")
        capture.update(state="completed", input_qc=[{
            "id": "input.valid", "state": "passed", "details": {"warnings": [warning]},
        }], output_qc=[])
        report = build_report({}, view=view)
        capture = next(stage for stage in report["stages"] if stage["id"] == "capture")
        self.assertEqual(capture["counts"]["warnings"], 1)
        summary = capture["warnings"][0]
        self.assertEqual(summary["code"], warning["code"])
        self.assertEqual(summary["objects"][0]["object"], "part-17")
        self.assertEqual(summary["objects"][0]["source"], "sealed_native_input")
        self.assertIn("excerpt", capture["boundary"][0]["raw_details"])
        self.assertEqual(summary["record_location"]["record"], "input/handoff.json")
        self.assertEqual(
            capture["boundary"][0]["raw_details"]["complete_record"]["record"], "reports/native-stages.json",
        )
        self.assertGreater(summary["objects"][0]["original"]["omitted_characters"], 4000)
        self.assertEqual(warning["objects"][0]["original"], "x" * 5000)

    def test_native_discovery_preserves_all_original_diagnostic_fields(self):
        observations = [{"code": "saved_native.pose_mates", "blocking": False,
                         "message": "Saved native observation", "mates": [{"name": "pose-1"}],
                         "source": "native", "qualification": {"hardware": False}}]
        diagnostics = []
        with patch("description_pipeline.verification.native_discovery.verify_discovery",
                   return_value={"passed": True, "diagnostics": observations}):
            _native_discovery("unused", diagnostics)
        self.assertEqual(diagnostics, observations)
        inventory = verification_diagnostics({"diagnostics": diagnostics + observations, "checks": []})
        self.assertEqual(inventory, observations)


if __name__ == "__main__":
    unittest.main()
