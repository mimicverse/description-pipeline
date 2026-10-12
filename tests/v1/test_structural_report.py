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

    @staticmethod
    def _provenance(warnings):
        """The producer's canonical record (freeze._material_provenance_record, 71f489a)."""

        return {
            "schema_version": "solidworks-to-urdf.material-provenance/v1",
            "assembly": "robot.SLDASM",
            "configuration": "默认",
            "material_source": "cad",
            "measured_source": "cad_api",
            "warnings": warnings,
        }

    @staticmethod
    def _provenance_entry(**overrides):
        entry = {
            "code": "cad_material_provenance_missing",
            "message": "Every solid requires an explicit physical material",
            "blocking": False,
            "component": "arm-1",
            "document": "cad/arm.SLDPRT",
            "configuration": "默认",
            "missing_body_indices": [0],
            "material_assignment": {
                "schema_version": "swbridge.material-assignment/v1",
                "configuration": "默认",
                "body_count": 1,
                "bodies": [
                    {
                        "index": 0,
                        "name": "block-1",
                        "material": {"name": "", "database": ""},
                        "effective_source": "part",
                    }
                ],
            },
            "measured": {
                "source": "cad_api",
                "scope": "part_document",
                "mass_kg": 0.1256,
                "center_of_mass_m": [0.015, 0.001, 0.02],
                "inertia_kg_m2": [[1.0e-5, 2.0e-6, 0.0], [2.0e-6, 3.0e-5, 0.0], [0.0, 0.0, 4.0e-5]],
                "volume_m3": 1.6e-5,
                "density_kg_m3": 7850.0,
            },
        }
        entry.update(overrides)
        return entry

    @staticmethod
    def _copy(payload):
        return json.loads(json.dumps(payload))

    def test_missing_material_is_a_warning_with_measured_cad_values(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entry = self._provenance_entry()
            write_json(root / "evidence/raw/material_provenance.json", self._provenance([entry]))
            receipt = {"run_id": "m3", "state": "verified", "passed": True}
            before = dict(receipt)
            pins = write_structural_report(root, receipt, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            self.assertEqual(before, receipt)
            warnings = [item for item in report["findings"] if item["code"] == "native.material_provenance_missing"]
            self.assertEqual(len(warnings), 1)
            finding = warnings[0]
            self.assertEqual(finding["severity"], "warning")
            self.assertEqual(finding["object"], "arm-1")
            self.assertEqual(finding["evidence"]["missing_body_indices"], [0])
            self.assertEqual(finding["evidence"]["configuration"], "默认")
            self.assertEqual(finding["evidence"]["document"], "cad/arm.SLDPRT")
            self.assertEqual(finding["evidence"]["material_assignment"], entry["material_assignment"])
            measured = finding["evidence"]["measured"]
            self.assertEqual(measured["source"], "cad_api")
            self.assertEqual(measured["scope"], "part_document")
            self.assertEqual(measured["mass_kg"], 0.1256)
            self.assertEqual(measured["center_of_mass_m"], [0.015, 0.001, 0.02])
            self.assertEqual(measured["inertia_kg_m2"][0][1], 2.0e-6)
            self.assertEqual(measured["density_kg_m3"], 7850.0)
            self.assertIn("不据此声称材料已核验", finding["message"])
            self.assertEqual(report["summary"], {"warnings": 1, "blockers": 0})
            markdown = (root / pins["markdown"]["path"]).read_text()
            self.assertIn("native.material_provenance_missing", markdown)

    def test_missing_material_warning_sits_beside_a_failed_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "evidence/raw/material_provenance.json",
                self._provenance([self._provenance_entry(component="base-1", document="cad/base.SLDPRT")]),
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
            self.assertEqual(finding["object"], "base-1")
            self.assertEqual(finding["evidence"]["code"], "cad_material_provenance_missing")
            self.assertEqual(finding["evidence"]["measured"]["mass_kg"], 0.1256)

    def test_malformed_material_provenance_is_independently_rejected(self):
        missing_top_key = self._copy(self._provenance([]))
        del missing_top_key["measured_source"]
        entry_without_measured = self._provenance_entry()
        del entry_without_measured["measured"]
        measured_missing_key = self._provenance_entry()
        del measured_missing_key["measured"]["density_kg_m3"]
        measured_wrong_source = self._provenance_entry()
        measured_wrong_source["measured"]["source"] = "author_default"
        unknown_reason = self._provenance_entry(code="cad_other_reason")
        payloads = {
            "wrong_schema": {**self._provenance([]), "schema_version": "solidworks-to-urdf.material-provenance/v2"},
            "missing_top_key": missing_top_key,
            "warnings_not_a_list": {**self._provenance([]), "warnings": {"component": "arm-1"}},
            "bare_list": [{"component": "arm-1"}],
            "entry_missing_measured": self._provenance([entry_without_measured]),
            "measured_missing_key": self._provenance([measured_missing_key]),
            "measured_wrong_source": self._provenance([measured_wrong_source]),
            "unknown_reason": self._provenance([unknown_reason]),
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
                self.assertIn("必须由独立检查判为失败", invalid["message"])
                self.assertIn("不采用默认值", invalid["message"])

    def test_manifest_required_missing_material_record_is_a_blocker(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "evidence/manifest.json",
                {
                    "schema_version": "description.source/v1",
                    "kind": "solidworks",
                    "identity": {"main_assembly": "robot.SLDASM"},
                    "evidence_class": "cad",
                    "scene": "scene.json",
                    "files": {"scene.json": "0" * 64, "raw/material_provenance.json": "1" * 64},
                },
            )
            pins = write_structural_report(root, {"run_id": "m3", "state": "verified"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            codes = [item["code"] for item in report["findings"]]
            self.assertIn("report.material_provenance_missing", codes)
            self.assertNotIn("native.material_provenance_missing", codes)
            missing = next(item for item in report["findings"] if item["code"] == "report.material_provenance_missing")
            self.assertEqual(missing["severity"], "blocker")
            self.assertIn("证据缺失", missing["message"])
            self.assertEqual(report["summary"]["blockers"], 1)

    def test_legacy_snapshot_without_the_required_record_is_unaffected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "evidence/manifest.json",
                {
                    "schema_version": "description.source/v1",
                    "kind": "solidworks",
                    "identity": {"main_assembly": "robot.SLDASM"},
                    "evidence_class": "cad",
                    "scene": "scene.json",
                    "files": {"scene.json": "0" * 64},
                },
            )
            pins = write_structural_report(root, {"run_id": "m3", "state": "verified"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            self.assertEqual(report["findings"], [])
            self.assertEqual(report["summary"], {"warnings": 0, "blockers": 0})

    def test_unreadable_material_record_is_a_blocker(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence/raw").mkdir(parents=True)
            (root / "evidence/raw/material_provenance.json").write_text("{invalid json", encoding="utf-8")
            pins = write_structural_report(root, {"run_id": "m3", "state": "verified"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            codes = [item["code"] for item in report["findings"]]
            self.assertIn("report.evidence_unreadable", codes)
            invalid = next(item for item in report["findings"] if item["code"] == "report.material_provenance_invalid")
            self.assertEqual(invalid["severity"], "blocker")
            self.assertEqual(invalid["evidence"], {"unreadable": True})
            self.assertNotIn("native.material_provenance_missing", codes)

    def test_warning_object_uses_component_and_keeps_repeat_occurrence_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._provenance_entry()
            second = self._provenance_entry(
                component="arm-2",
                configuration="tall",
                measured={**self._provenance_entry()["measured"], "mass_kg": 0.1456, "volume_m3": 1.86e-5},
            )
            write_json(root / "evidence/raw/material_provenance.json", self._provenance([first, second]))
            pins = write_structural_report(root, {"run_id": "m3", "state": "verified"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            warnings = [item for item in report["findings"] if item["code"] == "native.material_provenance_missing"]
            self.assertEqual([item["object"] for item in warnings], ["arm-1", "arm-2"])
            self.assertEqual([item["evidence"]["configuration"] for item in warnings], ["默认", "tall"])
            self.assertEqual([item["evidence"]["measured"]["mass_kg"] for item in warnings], [0.1256, 0.1456])
            self.assertTrue(all(item["evidence"]["document"] == "cad/arm.SLDPRT" for item in warnings))
            self.assertEqual(report["summary"], {"warnings": 2, "blockers": 0})

    def test_blocking_material_entry_renders_as_blocker(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entry = self._provenance_entry(blocking=True)
            write_json(root / "evidence/raw/material_provenance.json", self._provenance([entry]))
            pins = write_structural_report(root, {"run_id": "m3", "state": "verified"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            findings = [item for item in report["findings"] if item["code"] == "native.material_provenance_missing"]
            self.assertEqual([item["severity"] for item in findings], ["blocker"])
            self.assertIn("标记为阻断", findings[0]["message"])
            self.assertEqual(report["summary"], {"warnings": 0, "blockers": 1})

    def test_primary_blocker_uses_the_exact_component_from_the_native_detail(self):
        from description_pipeline.structural_report import build_structural_report

        receipt = {
            "run_id": "m3",
            "state": "failed",
            "passed": False,
            "error": "The part has no solid bodies",
            "error_code": "cad_empty_model",
            "main_assembly": "robot.SLDASM",
            "detail": {
                "component": "arm-2",
                "document": "arm.SLDPRT",
                "referenced_configuration": "tall",
                "bodies": 0,
                "phase": "capture",
            },
        }
        report = build_structural_report(receipt)
        blocker = next(item for item in report["findings"] if item["source"] == "run/error")
        self.assertEqual(blocker["severity"], "blocker")
        self.assertEqual(blocker["object"], "arm-2")
        self.assertNotEqual(blocker["object"], receipt["main_assembly"])
        self.assertEqual(
            blocker["evidence"]["detail"],
            {
                "component": "arm-2",
                "document": "arm.SLDPRT",
                "referenced_configuration": "tall",
                "bodies": 0,
                "phase": "capture",
            },
        )
        self.assertEqual(report["summary"], {"warnings": 0, "blockers": 1})

    def test_primary_blocker_falls_back_without_inventing_a_component(self):
        from description_pipeline.structural_report import build_structural_report

        report = build_structural_report(
            {
                "run_id": "m3",
                "state": "failed",
                "error": "collected files changed",
                "error_code": "cad_copy_changed",
                "main_assembly": "robot.SLDASM",
                "detail": {"document": "base.SLDPRT", "changed": ["a.sldprt"]},
            }
        )
        blocker = next(item for item in report["findings"] if item["source"] == "run/error")
        self.assertEqual(blocker["object"], "base.SLDPRT")

        report = build_structural_report(
            {"run_id": "m3", "state": "failed", "error": "boom", "main_assembly": "robot.SLDASM"}
        )
        blocker = next(item for item in report["findings"] if item["source"] == "run/error")
        self.assertEqual(blocker["object"], "robot.SLDASM")
        self.assertIsNone(blocker["evidence"]["detail"])

    def test_finding_order_is_independent_of_stored_key_order(self):
        from description_pipeline.structural_report import build_structural_report

        receipt = {
            "run_id": "m3",
            "state": "failed",
            "error": "native readings missing",
            "selection": {"manifest": {"closure": {"findings": [{"code": "closure.open_failed", "object": "a"}]}}},
            "native_record": {"diagnostics": [{"code": "reader.absent", "object": "b", "message": "gone"}]},
            "warnings": [{"code": "cad.warning", "object": "c", "message": "w"}],
        }
        direct = build_structural_report(receipt)
        reloaded = build_structural_report(json.loads(json.dumps(receipt, sort_keys=True)))
        self.assertEqual(direct, reloaded)
        self.assertEqual(
            [item["source"] for item in direct["findings"]],
            [item["source"] for item in reloaded["findings"]],
        )

    def _effective_record(self, warnings, *, readings=None):
        return {
            "schema_version": "solidworks-to-urdf.effective-instances/v1",
            "assembly": "robot.SLDASM",
            "configuration": "默认",
            "readings": readings if readings is not None else {},
            "warnings": warnings,
        }

    def _effective_warning(self, code, component):
        item = {
            "code": code,
            "blocking": False,
            "component": component,
            "configuration": "默认",
            "overrides": {"mass": True, "center_of_mass": False, "inertia": True},
            "qualification": (
                "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
            ),
            "measurement": {
                "scope": "assembly_component_group",
                "axes": "assembly_document_axes",
                "mass_kg": 0.0632,
                "center_of_mass_m": [0.0, 0.0, 0.07],
                "inertia_kg_m2": [[0.001, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.0004]],
            },
        }
        if code == "cad_surface_only_effective_instance":
            item.update(
                document="board.SLDPRT",
                part_document_read="unavailable",
                solid_bodies=0,
                sheet_bodies=2350,
            )
        else:
            item.update(
                role="parent",
                document_type="assembly",
                part_document="board.SLDPRT",
                assembly_document="robot.SLDASM",
            )
        return item

    def test_effective_instance_warnings_render_with_honest_qualification(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._effective_record(
                [
                    self._effective_warning("cad_instance_override_effective", "board-1"),
                    self._effective_warning("cad_surface_only_effective_instance", "pcb-1"),
                ],
                readings={"board-1": {"role": "parent", "mass": 0.0632}, "pcb-1": {"role": "leaf", "mass": 0.03}},
            )
            write_json(root / "evidence/raw/effective_instances.json", record)
            pins = write_structural_report(root, {"run_id": "m3", "state": "verified"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            findings = [item for item in report["findings"] if item["code"].startswith("cad_")]
            self.assertEqual(
                [item["code"] for item in findings],
                ["cad_instance_override_effective", "cad_surface_only_effective_instance"],
            )
            self.assertTrue(all(item["severity"] == "warning" for item in findings))
            self.assertEqual([item["object"] for item in findings], ["board-1", "pcb-1"])
            self.assertTrue(all("未经几何推导与独立物理校核" in item["message"] for item in findings))
            self.assertIn("未向运动链分摊", findings[0]["message"])
            self.assertEqual(
                findings[0]["evidence"]["qualification"],
                "cad_authored_effective_instance; not geometry-derived, not independently physically qualified",
            )
            self.assertEqual(
                findings[0]["evidence"]["overrides"],
                {"mass": True, "center_of_mass": False, "inertia": True},
            )
            self.assertEqual(report["summary"], {"warnings": 2, "blockers": 0})

    def test_manifest_bound_effective_record_missing_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "evidence/manifest.json", {"files": {"raw/effective_instances.json": "0" * 64}})
            pins = write_structural_report(root, {"run_id": "m3", "state": "failed"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            missing = [item for item in report["findings"] if item["code"] == "report.effective_instances_missing"]
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0]["severity"], "blocker")
            self.assertEqual(missing[0]["evidence"], {"required_by_manifest": True})

    def test_malformed_effective_record_is_rejected_and_unbound_absence_is_valid(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._effective_record([{"code": "cad_unknown", "blocking": False}])
            write_json(root / "evidence/raw/effective_instances.json", record)
            pins = write_structural_report(root, {"run_id": "m3", "state": "failed"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            invalid = [item for item in report["findings"] if item["code"] == "report.effective_instances_invalid"]
            self.assertEqual(len(invalid), 1)
            self.assertEqual(invalid[0]["severity"], "blocker")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            pins = write_structural_report(root, {"run_id": "m3", "state": "failed"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            codes = [item["code"] for item in report["findings"]]
            self.assertNotIn("report.effective_instances_missing", codes)
            self.assertNotIn("report.effective_instances_invalid", codes)




    def test_mass_closure_records_are_projected_and_allowlisted(self):
        from description_pipeline.structural_report import is_sealed_diagnostic_member

        self.assertTrue(is_sealed_diagnostic_member("evidence/raw/mass_closure.json"))
        self.assertTrue(is_sealed_diagnostic_member("reports/mass-closure.json"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "reports/mass-closure.json",
                {
                    "findings": [
                        {
                            "code": "closure.effective_total_mismatch",
                            "object": "board-1",
                            "blocking": True,
                            "message": "有效实例总质量与闭包不一致",
                        }
                    ]
                },
            )
            write_json(
                root / "evidence/raw/mass_closure.json",
                {
                    "status": "recorded",
                    "errors": [{"code": "closure.context_missing", "message": "缺少组件上下文读数"}],
                },
            )
            pins = write_structural_report(root, {"run_id": "m3", "state": "failed"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            codes = [item["code"] for item in report["findings"]]
            self.assertIn("closure.effective_total_mismatch", codes)
            self.assertIn("closure.context_missing", codes)
            blocker = next(item for item in report["findings"] if item["code"] == "closure.effective_total_mismatch")
            self.assertEqual(blocker["severity"], "blocker")
            self.assertEqual(blocker["object"], "board-1")

    def test_partial_evidence_resolves_via_gated_failure_roots(self):
        from description_pipeline.structural_report import diagnostic_file_sources

        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "evidence.failed-001/partial/raw/material_provenance.json", {"schema_version": "x"})
            write_json(
                root / "evidence.failed-001/partial/evidence/raw/effective_instances.json",
                {"schema_version": "y"},
            )
            write_json(root / "evidence/raw/mass_closure.json", {"status": "recorded"})
            write_json(root / "partial/raw/material_provenance.json", {"top-level-guess": True})
            sources = diagnostic_file_sources(root, failure_roots=("evidence.failed-001",))
            self.assertEqual(
                sources["evidence/raw/material_provenance.json"],
                "evidence.failed-001/partial/raw/material_provenance.json",
            )
            self.assertEqual(
                sources["evidence/raw/effective_instances.json"],
                "evidence.failed-001/partial/evidence/raw/effective_instances.json",
            )
            self.assertEqual(sources["evidence/raw/mass_closure.json"], "evidence/raw/mass_closure.json")
            self.assertNotIn("evidence/raw/dependency_closure.json", sources)

    def test_conflicting_partial_evidence_fails_instead_of_choosing(self):
        from description_pipeline.io import PipelineError
        from description_pipeline.structural_report import diagnostic_file_sources

        with TemporaryDirectory() as directory:
            root = Path(directory)
            for number in ("001", "002"):
                write_json(root / f"evidence.failed-{number}/partial/raw/material_provenance.json", {"n": number})
            with self.assertRaises(PipelineError):
                diagnostic_file_sources(root, failure_roots=("evidence.failed-001", "evidence.failed-002"))

    def _instance_difference_warning(self, component="shim-1"):
        return {
            "code": "cad_instance_measurement_differs_from_part",
            "blocking": False,
            "component": component,
            "role": "leaf",
            "overrides": {"mass": False, "center_of_mass": False, "inertia": False},
            "qualification": (
                "cad_assembly_instance_measurement; cause_of_part_difference_unproven; "
                "not geometry-derived, not independently physically qualified"
            ),
            "measurement": {
                "scope": "assembly_component_group",
                "mass_kg": 0.0042,
                "center_of_mass_m": [0.0, 0.0, 0.01],
                "inertia_kg_m2": [[1e-6, 0.0, 0.0], [0.0, 1e-6, 0.0], [0.0, 0.0, 2e-6]],
            },
            "part_measurement": {
                "mass_kg": 0.0040,
                "center_of_mass_m": [0.0, 0.0, 0.011],
                "inertia_kg_m2": [[1.1e-6, 0.0, 0.0], [0.0, 1.1e-6, 0.0], [0.0, 0.0, 2.1e-6]],
                "scope": "part_document",
                "used_api": "IMassProperty2",
                "product_convention": "solidworks-default",
                "volume_m3": 5.3e-7,
            },
        }

    def test_instance_measurement_difference_renders_with_exact_literal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            record = self._effective_record(
                [self._instance_difference_warning()],
                readings={"shim-1": {"role": "leaf", "mass": 0.0042}},
            )
            write_json(root / "evidence/raw/effective_instances.json", record)
            pins = write_structural_report(root, {"run_id": "m3", "state": "verified"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            finding = next(
                item
                for item in report["findings"]
                if item["code"] == "cad_instance_measurement_differs_from_part"
            )
            self.assertEqual(finding["severity"], "warning")
            self.assertEqual(finding["object"], "shim-1")
            self.assertIn("差异原因尚未证实", finding["message"])
            self.assertIn("未经几何推导与独立物理校核", finding["message"])
            self.assertEqual(
                finding["evidence"]["qualification"],
                "cad_assembly_instance_measurement; cause_of_part_difference_unproven; "
                "not geometry-derived, not independently physically qualified",
            )
            self.assertEqual(finding["evidence"]["part_measurement"]["product_convention"], "solidworks-default")
            self.assertEqual(report["summary"], {"warnings": 1, "blockers": 0})

    def test_instance_measurement_difference_refusals(self):
        from description_pipeline.structural_report import _canonical_effective_warnings

        def record_with(item):
            return self._effective_record([item], readings={"shim-1": {"role": "leaf"}})

        self.assertIsNotNone(_canonical_effective_warnings(record_with(self._instance_difference_warning())))
        wrong_literal = self._instance_difference_warning()
        wrong_literal["qualification"] = (
            "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
        )
        self.assertIsNone(_canonical_effective_warnings(record_with(wrong_literal)))
        flagged = self._instance_difference_warning()
        flagged["overrides"]["mass"] = True
        self.assertIsNone(_canonical_effective_warnings(record_with(flagged)))
        parent_role = self._instance_difference_warning()
        parent_role["role"] = "parent"
        self.assertIsNone(_canonical_effective_warnings(record_with(parent_role)))
        no_part = self._instance_difference_warning()
        del no_part["part_measurement"]
        self.assertIsNone(_canonical_effective_warnings(record_with(no_part)))
        no_convention = self._instance_difference_warning()
        del no_convention["part_measurement"]["product_convention"]
        self.assertIsNone(_canonical_effective_warnings(record_with(no_convention)))

    def test_partial_failure_bundle_renders_recovered_warnings(self):
        import shutil

        from description_pipeline.structural_report import diagnostic_file_sources

        with TemporaryDirectory() as directory:
            tree = Path(directory) / "failed-tree"
            (tree / "evidence.failed-001/partial/raw").mkdir(parents=True)
            write_json(
                tree / "evidence.failed-001/partial/raw/material_provenance.json",
                self._provenance([self._provenance_entry()]),
            )
            write_json(
                tree / "evidence.failed-001/partial/raw/effective_instances.json",
                self._effective_record([self._effective_warning("cad_surface_only_effective_instance", "pcb-1")]),
            )
            write_json(tree / "evidence.failed-001/partial/raw/mass_closure.json", {"status": "recorded"})
            sources = diagnostic_file_sources(tree, failure_roots=("evidence.failed-001",))
            bundle = Path(directory) / "bundle"
            for name, relative in sources.items():
                target = bundle / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tree / relative, target)
            pins = write_structural_report(
                Path(directory) / "run",
                {"run_id": "m3", "state": "failed", "error": "boom"},
                evidence_root=bundle,
            )
            report = json.loads((Path(directory) / "run" / pins["json"]["path"]).read_text())
            codes = [item["code"] for item in report["findings"]]
            self.assertIn("native.material_provenance_missing", codes)
            self.assertIn("cad_surface_only_effective_instance", codes)

    def _passive_finding(self, **overrides):
        import hashlib

        component = "headM3.0-1/垫片2-2"
        tag = hashlib.sha256(component.encode("utf-8")).hexdigest()[:12]
        detail = {
            "component": component,
            "parent": "headM3.0-1",
            "body": f"passive_{tag}",
            "joint": f"passive_joint_{tag}",
            "axis_reference": {
                "kind": "mate_axis",
                "component": component,
                "mates": [{"scope": "headM3.0-1", "name": "同心27"}],
            },
            "rank": 5,
            "qualification": "cad_derived_passive_dof; author_intent_and_drive_unqualified",
        }
        detail.update(overrides)
        return {"code": "discovery.cad_unassigned_passive_joint", "blocking": False, "detail": detail}

    def test_passive_completion_renders_chinese_warning(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "input/discovery/findings.json", {"findings": [self._passive_finding()]})
            pins = write_structural_report(root, {"run_id": "m3", "state": "verified"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            finding = next(
                item
                for item in report["findings"]
                if item["code"] == "discovery.cad_unassigned_passive_joint"
            )
            self.assertEqual(finding["severity"], "warning")
            self.assertEqual(finding["object"], finding["evidence"]["joint"])
            self.assertIn(finding["evidence"]["body"], finding["message"])
            self.assertIn(finding["evidence"]["joint"], finding["message"])
            self.assertIn("垫片2-2", finding["message"])
            self.assertIn("headM3.0-1", finding["message"])
            self.assertIn("作者意图与驱动尚未取得工程确认", finding["message"])
            self.assertEqual(finding["evidence"]["rank"], 5)
            self.assertEqual(report["summary"]["blockers"], 0)

    def test_passive_completion_outside_the_schema_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "input/discovery/findings.json", {"findings": [self._passive_finding(rank=4)]})
            pins = write_structural_report(root, {"run_id": "m3", "state": "verified"}, evidence_root=root)
            report = json.loads((root / pins["json"]["path"]).read_text())
            codes = [item["code"] for item in report["findings"]]
            self.assertIn("report.cad_unassigned_passive_joint_invalid", codes)
            self.assertNotIn("discovery.cad_unassigned_passive_joint", codes)


if __name__ == "__main__":
    unittest.main()
