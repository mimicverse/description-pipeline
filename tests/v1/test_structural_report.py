"""Structural-team reports retain canonical raw and derived warnings."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, file_digest, write_json
from description_pipeline.sources.solidworks.saved_state import (
    warning_from_selection,
)
from description_pipeline.structural_report import (
    diagnostic_file_sources,
    is_sealed_diagnostic_member,
    write_structural_report,
)

from .verification_fixture import incomplete_saved_state_record


class StructuralReportTests(unittest.TestCase):
    def test_saved_state_warning_is_reported_and_forgery_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection = incomplete_saved_state_record()["source_selection"]
            warning = warning_from_selection(selection)
            assert warning is not None
            diagnostic = {
                "code": warning["code"],
                "blocking": False,
                "message": warning["message"],
                "objects": warning["objects"],
                "detail": warning["detail"],
            }
            write_json(root / "reports/quality.json", {"diagnostics": [diagnostic]})
            receipt = {
                "run_id": "saved-state",
                "main_assembly": selection["main_assembly"],
                "selection": {"manifest": selection},
            }

            pins = write_structural_report(root, receipt, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            saved = next(
                item
                for item in report["findings"]
                if item["code"] == "source.saved_state_incomplete"
            )
            self.assertEqual(saved["severity"], "warning")
            self.assertEqual(saved["detail"]["open_errors"], 2)

            forged = {
                **diagnostic,
                "detail": {**diagnostic["detail"], "open_errors": 0},
            }
            write_json(root / "reports/quality.json", {"diagnostics": [forged]})
            pins = write_structural_report(root, receipt, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            self.assertIn(
                "report.saved_state_warning_invalid",
                {item["code"] for item in report["findings"]},
            )

    def test_discovery_quality_and_model_warnings_share_one_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "input/discovery/native-discovery.json",
                {
                    "findings": [
                        {
                            "code": "discovery.joint_drive_unspecified",
                            "object": "joint:hip",
                            "blocking": False,
                            "message": "CAD drive remains unqualified",
                            "detail": {"effort": 0, "velocity": 0},
                        }
                    ]
                },
            )
            write_json(
                root / "reports/quality.json",
                {
                    "diagnostics": [
                        {
                            "code": "units.si_conversion",
                            "blocking": False,
                            "message": "SI conversion applied",
                            "detail": {"objects": []},
                        }
                    ]
                },
            )
            write_json(
                root / "model/robot.json",
                {
                    "provenance": {
                        "value_warnings": [
                            {
                                "code": "physics.derived_from_native",
                                "message": "Inertia recombined in link axes",
                                "objects": [],
                                "detail": {"objects": []},
                            }
                        ]
                    }
                },
            )
            pins = write_structural_report(
                root,
                {"run_id": "warnings", "state": "verified"},
                evidence_root=root,
            )
            report = json.loads((root / pins["json"]["path"]).read_text())
            self.assertEqual(
                {item["code"] for item in report["findings"]},
                {
                    "discovery.joint_drive_unspecified",
                    "units.si_conversion",
                    "physics.derived_from_native",
                },
            )
            self.assertEqual(
                report["summary"],
                {"warnings": 3, "blockers": 0},
            )

    def test_pre_capture_failure_preserves_native_reference_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = {
                "run_id": "failed",
                "status": "failed",
                "error": "Native binding unresolved",
                "error_code": "selection_dependency_unresolved",
                "detail": {
                    "problems": [
                        {
                            "code": "selection_dependency_unresolved",
                            "message": "Reference unresolved",
                            "document": "robot.SLDASM",
                            "instance": "part-1",
                            "stored_location": r"C:\author\assembly\part.SLDPRT",
                            "resolved": None,
                        }
                    ]
                },
            }
            pins = write_structural_report(root, receipt)
            report = json.loads((root / pins["json"]["path"]).read_text())
            diagnostic = next(
                item
                for item in report["findings"]
                if item["object"] == "part-1"
            )
            self.assertEqual(
                diagnostic["evidence"]["stored_location"],
                r"C:\author\assembly\part.SLDPRT",
            )
            self.assertIsNone(diagnostic["evidence"]["resolved"])
            self.assertIn(
                "This stage has not produced a URDF",
                (root / pins["markdown"]["path"]).read_text(),
            )

    def test_unreadable_canonical_evidence_preserves_original_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            (root / "input/handoff.json").write_text("{invalid json")
            pins = write_structural_report(
                root,
                {"error": "original native failure"},
                evidence_root=root,
            )
            report = json.loads((root / pins["json"]["path"]).read_text())
            self.assertIn(
                "report.evidence_unreadable",
                {item["code"] for item in report["findings"]},
            )
            self.assertIn(
                "original native failure",
                (root / pins["markdown"]["path"]).read_text(),
            )

    def test_canonical_diagnostic_allowlist_and_partial_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "evidence/raw/native_capture_facts.json"
            canonical.parent.mkdir(parents=True)
            canonical.write_text("{}\n")
            partial = (
                root
                / "evidence.failed-001/partial/raw/native_capture_facts.json"
            )
            partial.parent.mkdir(parents=True)
            partial.write_text("{\"raw\": true}\n")
            canonical.unlink()
            sources = diagnostic_file_sources(
                root,
                failure_roots=("evidence.failed-001",),
            )
            self.assertEqual(
                sources["evidence/raw/native_capture_facts.json"],
                "evidence.failed-001/partial/raw/native_capture_facts.json",
            )
            self.assertTrue(
                is_sealed_diagnostic_member(
                    "input/discovery/native-discovery.json"
                )
            )
            self.assertFalse(
                is_sealed_diagnostic_member("evidence/capture.json")
            )

    def test_conflicting_partial_evidence_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for failure in ("evidence.failed-001", "evidence.failed-002"):
                path = root / failure / "partial/raw/native_capture_facts.json"
                path.parent.mkdir(parents=True)
                path.write_text(failure)
            with self.assertRaises(PipelineError):
                diagnostic_file_sources(
                    root,
                    failure_roots=(
                        "evidence.failed-001",
                        "evidence.failed-002",
                    ),
                )

    def test_written_report_pins_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pins = write_structural_report(root, {"run_id": "pins"})
            for pin in pins.values():
                self.assertEqual(
                    pin["sha256"],
                    file_digest(root / pin["path"]),
                )


if __name__ == "__main__":
    unittest.main()
