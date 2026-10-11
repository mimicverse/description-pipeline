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

    def test_missing_material_is_a_warning_with_measured_cad_values(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            assignment = {
                "schema_version": "swbridge.material-assignment/v1",
                "configuration": "默认",
                "query_configuration": "",
                "part_api": "IPartDoc.GetMaterialPropertyName2",
                "body_api": "IBody2.GetMaterialPropertyName",
                "part": {"name": "", "database": ""},
                "body_count": 1,
                "bodies": [
                    {
                        "index": 0,
                        "name": "block-1",
                        "material": {"name": "", "database": ""},
                        "effective_source": "part",
                    }
                ],
            }
            write_json(
                root / "evidence/raw/material_provenance.json",
                {
                    "schema_version": "solidworks-to-urdf.material-provenance/v1",
                    "warnings": [
                        {
                            "code": "cad_material_provenance_missing",
                            "message": "Every solid requires an explicit physical material",
                            "document": "cad/arm.SLDPRT",
                            "configuration": "默认",
                            "missing_body_indices": [0],
                            "material_assignment": assignment,
                            "measured": {
                                "source": "cad_api",
                                "bodies": [
                                    {
                                        "index": 0,
                                        "name": "block-1",
                                        "mass_kg": 0.1256,
                                        "center_of_mass_m": [0.015, 0.001, 0.02],
                                        "inertia_kg_m2": [
                                            [1.0e-5, 2.0e-6, 0.0],
                                            [2.0e-6, 3.0e-5, 0.0],
                                            [0.0, 0.0, 4.0e-5],
                                        ],
                                    }
                                ],
                            },
                        }
                    ],
                },
            )
            receipt = {"run_id": "m3", "state": "verified", "passed": True}
            before = dict(receipt)
            pins = write_structural_report(root, receipt, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            self.assertEqual(before, receipt)
            warnings = [item for item in report["findings"] if item["code"] == "native.material_provenance_missing"]
            self.assertEqual(len(warnings), 1)
            finding = warnings[0]
            self.assertEqual(finding["severity"], "warning")
            self.assertEqual(finding["object"], "cad/arm.SLDPRT")
            self.assertEqual(finding["evidence"]["missing_body_indices"], [0])
            self.assertEqual(finding["evidence"]["configuration"], "默认")
            self.assertEqual(finding["evidence"]["material_assignment"], assignment)
            measured = finding["evidence"]["measured"]
            self.assertEqual(measured["source"], "cad_api")
            self.assertEqual(measured["bodies"][0]["mass_kg"], 0.1256)
            self.assertEqual(measured["bodies"][0]["center_of_mass_m"], [0.015, 0.001, 0.02])
            self.assertEqual(measured["bodies"][0]["inertia_kg_m2"][0][1], 2.0e-6)
            self.assertIn("不据此声称材料已核验", finding["message"])
            self.assertEqual(report["summary"], {"warnings": 1, "blockers": 0})
            markdown = (root / pins["markdown"]["path"]).read_text()
            self.assertIn("native.material_provenance_missing", markdown)

    def test_missing_material_warning_sits_beside_a_failed_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "evidence/raw/material_provenance.json",
                {
                    "schema_version": "solidworks-to-urdf.material-provenance/v1",
                    "warnings": [
                        {
                            "code": "cad_material_provenance_missing",
                            "document": "cad/base.SLDPRT",
                            "configuration": "默认",
                            "missing_body_indices": [1],
                            "measured": {"source": "cad_api", "bodies": [{"index": 1, "mass_kg": 0.02}]},
                        }
                    ],
                },
            )
            pins = write_structural_report(
                root, {"run_id": "m3", "status": "failed", "error": "capture failed"}, evidence_root=root
            )
            report = json.loads((root / pins["json"]["path"]).read_text())
            codes = [item["code"] for item in report["findings"]]
            self.assertIn("native.material_provenance_missing", codes)
            self.assertIn("pipeline.execution_failed", codes)
            self.assertEqual(report["summary"], {"warnings": 1, "blockers": 1})
            finding = next(item for item in report["findings"] if item["code"] == "native.material_provenance_missing")
            self.assertIsNone(finding["evidence"]["material_assignment"])
            self.assertEqual(finding["evidence"]["measured"]["bodies"][0]["mass_kg"], 0.02)
            self.assertEqual(finding["evidence"]["code"], "cad_material_provenance_missing")

    def test_malformed_material_provenance_is_independently_rejected(self):
        payloads = {
            "wrong_schema": {
                "schema_version": "solidworks-to-urdf.material-provenance/v2",
                "warnings": [],
            },
            "warnings_not_a_list": {
                "schema_version": "solidworks-to-urdf.material-provenance/v1",
                "warnings": {"document": "cad/base.SLDPRT"},
            },
            "bare_list": [{"document": "cad/base.SLDPRT", "missing_body_indices": [0]}],
        }
        for label, payload in payloads.items():
            with self.subTest(label=label), TemporaryDirectory() as directory:
                root = Path(directory)
                write_json(root / "evidence/raw/material_provenance.json", payload)
                pins = write_structural_report(root, {"run_id": "m3", "state": "verified"}, evidence_root=root)
                report = json.loads((root / pins["json"]["path"]).read_text())
                codes = [item["code"] for item in report["findings"]]
                self.assertIn("report.material_provenance_invalid", codes)
                self.assertNotIn("native.material_provenance_missing", codes)
                self.assertEqual(report["summary"]["blockers"], 1)
                invalid = next(
                    item for item in report["findings"] if item["code"] == "report.material_provenance_invalid"
                )
                self.assertIn("不采用默认值", invalid["message"])


if __name__ == "__main__":
    unittest.main()
