"""The generated team report retains evidence on both successful and failed runs."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from description_pipeline.io import file_digest, write_json
from description_pipeline.structural_report import write_structural_report


class StructuralReportTests(unittest.TestCase):
    def test_warned_urdf_is_retained_and_reported_without_changing_the_receipt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "urdf").mkdir()
            (root / "urdf/robot.urdf").write_text('<robot name="cad"/>')
            write_json(
                root / "input/discovery/findings.json",
                {
                    "findings": [
                        {
                            "code": "discovery.joint_drive_unspecified",
                            "object": "joint:hip",
                            "blocking": False,
                            "message": "CAD authored zero drive retained",
                            "detail": {"effort": 0, "velocity": 0},
                        }
                    ]
                },
            )
            receipt = {"run_id": "m3", "state": "verified", "passed": True}
            before = dict(receipt)
            pins = write_structural_report(root, receipt, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            self.assertEqual(before, receipt)
            self.assertEqual(report["summary"], {"warnings": 1, "blockers": 0})
            self.assertEqual(report["urdf_files"], ["urdf/robot.urdf"])
            self.assertEqual(report["findings"][0]["evidence"]["detail"], {"effort": 0, "velocity": 0})
            for pin in pins.values():
                self.assertEqual(pin["sha256"], file_digest(root / pin["path"]))
            self.assertIn("结构组", (root / pins["markdown"]["path"]).read_text())

    def test_pre_capture_failure_preserves_native_reference_diagnostics(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stored = r"C:\author\assembly\part.SLDPRT"
            receipt = {
                "run_id": "m3",
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
                            "stored_location": stored,
                            "resolved": None,
                        }
                    ]
                },
            }
            pins = write_structural_report(root, receipt)
            report = json.loads((root / pins["json"]["path"]).read_text())
            diagnostic = next(item for item in report["findings"] if item["object"] == "part-1")
            self.assertEqual(diagnostic["evidence"]["stored_location"], stored)
            self.assertIsNone(diagnostic["evidence"]["resolved"])
            self.assertEqual(report["urdf_files"], [])
            self.assertIn("尚未产生 URDF", (root / pins["markdown"]["path"]).read_text())

    def test_unreadable_evidence_is_reported_without_masking_the_original_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "reports/input.json").write_text("{invalid json")
            pins = write_structural_report(root, {"error": "original native failure"}, evidence_root=root)
            data = json.loads((root / pins["json"]["path"]).read_text())
            self.assertIn("report.evidence_unreadable", [item["code"] for item in data["findings"]])
            self.assertIn("original native failure", (root / pins["markdown"]["path"]).read_text())

    def test_stale_stored_references_are_warnings_with_exact_document_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "evidence/raw/dependency_closure.json",
                {
                    "stale_stored_references": [
                        {
                            "document": "cad/shank.SLDPRT",
                            "location": r"C:\author\stale\stale_part.SLDASM",
                            "resolved_path": r"C:\author\stale\stale_part.SLDASM",
                            "exists": False,
                        },
                        {
                            "document": "cad/arm.SLDPRT",
                            "location": r"D:\outside\external_part.SLDASM",
                            "resolved_path": r"D:\outside\external_part.SLDASM",
                            "exists": True,
                        },
                    ]
                },
            )
            receipt = {"run_id": "m3", "state": "verified", "passed": True}
            before = dict(receipt)
            pins = write_structural_report(root, receipt, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            self.assertEqual(before, receipt)
            warnings = [item for item in report["findings"] if item["code"] == "native.stale_stored_reference"]
            self.assertEqual(len(warnings), 2)
            self.assertTrue(all(item["severity"] == "warning" for item in warnings))
            first = next(item for item in warnings if item["evidence"]["document"] == "cad/shank.SLDPRT")
            self.assertEqual(first["evidence"]["location"], r"C:\author\stale\stale_part.SLDASM")
            self.assertEqual(first["evidence"]["resolved_path"], r"C:\author\stale\stale_part.SLDASM")
            self.assertIs(first["evidence"]["exists"], False)
            self.assertEqual(first["object"], "cad/shank.SLDPRT")
            self.assertIn("未读取", first["message"])
            self.assertEqual(report["summary"], {"warnings": 2, "blockers": 0})
            markdown = (root / pins["markdown"]["path"]).read_text()
            self.assertIn("stale_part.SLDASM", markdown)

    def test_failed_run_reports_stale_references_beside_the_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "evidence/raw/dependency_closure.json",
                {
                    "stale_stored_references": [
                        {
                            "document": "cad/shank.SLDPRT",
                            "location": r"C:\author\stale\stale_part.SLDASM",
                            "resolved_path": r"C:\author\stale\stale_part.SLDASM",
                            "exists": False,
                        }
                    ]
                },
            )
            pins = write_structural_report(
                root,
                {"run_id": "m3", "status": "failed", "error": "capture failed", "error_code": "cad_capture_failed"},
                evidence_root=root,
            )
            report = json.loads((root / pins["json"]["path"]).read_text())
            codes = [item["code"] for item in report["findings"]]
            self.assertIn("native.stale_stored_reference", codes)
            self.assertIn("cad_capture_failed", codes)
            self.assertEqual(report["summary"], {"warnings": 1, "blockers": 1})
            self.assertIn("capture failed", (root / pins["markdown"]["path"]).read_text())


if __name__ == "__main__":
    unittest.main()
