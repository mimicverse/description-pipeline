"""Regressions for the readable report projection (display-only mapping)."""

from __future__ import annotations

import unittest

from description_pipeline.orchestration.report_view import build_report
from tests.v1.protocol_support import protocol_events

RUN_ID = "portal-20261009T094843-d5230467"
SHA = "a" * 64
HSHA = "b" * 64


def quality_rows() -> dict:
    rows = [
        {
            "id": "physics.expected_mass",
            "state": "passed",
            "details": {"mass_kg": 3.3948682203507135, "expected_kg": [2.0, 4.5]},
        },
        {
            "id": "physics.mass_closure_equality",
            "state": "passed",
            "details": {
                "urdf_mass_kg": 3.3948682203507135,
                "whole_cad_mass_kg": 3.3948682203507135,
                "delta_kg": 0.0,
            },
        },
        {"id": "consumer.urdf", "state": "passed", "details": {"bodies": 4, "version": "3.13.0", "inputs": {}}},
        {"id": "joints.shoulder_pitch_joint", "state": "passed", "details": {}},
        {"id": "verification.complete", "state": "passed", "details": {}},
        {"id": "custom.unknown", "state": "passed", "details": {}},
    ]
    return {
        "passed": True,
        "subject_sha256": SHA,
        "checks": rows,
        "required_checks": [row["id"] for row in rows],
    }


def passed_job() -> dict:
    events = protocol_events(subject=SHA)
    for event in events:
        if (event.get("check") or {}).get("id") == "verification.gates":
            event["check"]["details"] = {
                "required_checks": ["bundle.subject"],
                "checks": [{"id": "bundle.subject", "state": "passed", "passed": True}],
            }
    return {
        "run_id": RUN_ID,
        "status": "passed",
        "request": {"handoff_sha256": HSHA, "package": "imports/x"},
        "events": events,
        "result": {"passed": True, "subject_sha256": SHA, "quality": quality_rows()},
    }


class ReportViewTests(unittest.TestCase):
    def test_passed_report_labels_counts_and_measured_values(self) -> None:
        report = build_report(passed_job())
        self.assertEqual(report["schema_version"], "solidworks-to-urdf.report/v1")
        self.assertEqual(report["overall"]["state"], "passed")
        self.assertEqual(report["overall"]["stages_completed"], 6)
        self.assertEqual(report["overall"]["stages_total"], 6)
        self.assertIsNone(report["failure"])
        stages = {stage["id"]: stage for stage in report["stages"]}
        self.assertEqual(stages["discover"]["name_zh"], "解析结构")
        freeze_counts = stages["freeze"]["counts"]["boundary"]
        self.assertEqual(freeze_counts, {"passed": 2, "executed": 2, "total": 2})
        self.assertTrue(all(row["executed"] for row in stages["freeze"]["boundary"]))
        self.assertEqual(
            stages["freeze"]["boundary"][0]["label_zh"],
            "交接包准入（路径、常规文件与原生包）",
        )
        verify = stages["verify"]
        independent = {row["id"]: row for row in verify["independent"]}
        window = independent["physics.expected_mass"]
        self.assertEqual(window["label_zh"], "质量期望范围")
        self.assertEqual(window["summary"]["expected"], "2.0–4.5 kg")
        self.assertIn("3.3948682203507135", window["summary"]["actual"])
        closure = independent["physics.mass_closure_equality"]
        self.assertEqual(closure["summary"]["expected"], "差值 = 0")
        self.assertIn("URDF", closure["summary"]["actual"])
        self.assertEqual(independent["joints.shoulder_pitch_joint"]["label_zh"], "关节：shoulder_pitch_joint")
        self.assertEqual(independent["custom.unknown"]["label_zh"], "custom.unknown")
        gate_row = next(row for row in verify["boundary"] if row["id"] == "verification.gates")
        self.assertEqual(gate_row["summary"]["actual"], "1/1 通过（已记录 1 项）")
        self.assertEqual(
            verify["counts"]["independent"],
            {"passed": 6, "executed": 6, "total": 6},
        )
        self.assertEqual(len(verify["independent"]), len(quality_rows()["checks"]))
        self.assertEqual(report["measured"]["expected_mass_window"]["expected_kg"], [2.0, 4.5])
        self.assertEqual(report["measured"]["mass_closure"]["delta_kg"], 0.0)
        discover = stages["discover"]
        confirmations = {item["id"]: item for item in discover["confirmations"]}
        self.assertIn("scope", confirmations)
        self.assertTrue(confirmations["scope"]["reference"])
        self.assertEqual(confirmations["scope"]["state"], "pending")
        self.assertEqual(
            set(confirmations["scope"]),
            {"id", "label", "reference", "review_stage", "scope", "automatic_exclusion", "state"},
        )
        self.assertEqual(report["overall"]["engineering_state"], "external_review")
        self.assertEqual(report["external_review"]["tracking"], "external")
        self.assertIn("不读取", report["external_review"]["note_zh"])
        self.assertTrue(report["external_review"]["scopes"])
        self.assertIn("交接包准入（路径、常规文件与原生包）", stages["freeze"]["automatic"]["passed_labels"])
        self.assertEqual(stages["freeze"]["automatic"]["failed_labels"], [])
        self.assertIn("外部评审", discover["manual_scope_note_zh"])

    def test_failed_discovery_meaning_is_safe_and_downstream_is_blocked(self) -> None:
        job = passed_job()
        job["status"] = "failed"
        job["events"] = protocol_events(subject=SHA, failed_stage="discover")
        job["error"] = "CadError: C:\\description-v1-review\\packages\\imports\\x\\lowerbody.SLDASM"
        job["detail"] = {"errors": 2, "warnings": 0}
        report = build_report(job)
        self.assertEqual(report["overall"]["state"], "failed")
        self.assertEqual(report["overall"]["engineering_state"], "not_ready")
        failure = report["failure"]
        self.assertEqual(failure["stage"], "discover")
        self.assertEqual(failure["stage_name_zh"], "解析结构")
        self.assertIn("无法定位", failure["meaning_zh"])
        self.assertNotIn("缺少零件", failure["meaning_zh"])
        self.assertNotIn("损坏", failure["meaning_zh"])
        self.assertEqual(failure["solidworks_codes"], {"errors": 2, "warnings": 0})
        self.assertEqual(failure["raw_type"], "CadError")
        stages = {stage["id"]: stage for stage in report["stages"]}
        self.assertEqual(stages["discover"]["state"], "failed")
        self.assertIn("先修复问题", stages["discover"]["manual_scope_note_zh"])
        self.assertIn("结构定义生成", stages["discover"]["automatic"]["failed_labels"])
        for downstream in ("capture", "generate", "verify", "publish"):
            with self.subTest(stage=downstream):
                self.assertEqual(stages[downstream]["state"], "blocked")
                self.assertEqual(stages[downstream]["state_zh"], "上游阶段失败（未执行）")
                self.assertEqual(stages[downstream]["counts"]["boundary"]["executed"], 0)
                self.assertTrue(all(not row["executed"] for row in stages[downstream]["boundary"]))

    def test_exact_unresolved_references_render_only_when_recorded(self) -> None:
        job = passed_job()
        job["status"] = "failed"
        job["events"] = protocol_events(subject=SHA, failed_stage="discover")
        job["error"] = "CadError: C:\\description-v1-review\\packages\\imports\\x\\lowerbody.SLDASM"
        job["detail"] = {
            "errors": 2,
            "warnings": 0,
            "unresolved_dependencies": [
                {"name": "NP-F550.SLDPRT", "path": "C:\\author\\NP-F550.SLDPRT"},
                {"name": "HD-1910-C001-20260902.stp.SLDASM", "path": "C:\\temp\\swx\\HD-1910.stp.SLDASM"},
            ],
        }
        failure = build_report(job)["failure"]
        self.assertEqual(
            [item["name"] for item in failure["unresolved_dependencies"]],
            ["NP-F550.SLDPRT", "HD-1910-C001-20260902.stp.SLDASM"],
        )
        self.assertIn("NP-F550.SLDPRT", failure["meaning_zh"])
        self.assertIn("无法定位", failure["meaning_zh"])
        self.assertIn("尚未证明", failure["meaning_zh"])
        self.assertEqual(failure["applicability"], "unproven")
        plain = passed_job()
        plain["status"] = "failed"
        plain["events"] = protocol_events(subject=SHA, failed_stage="discover")
        plain["error"] = job["error"]
        plain["detail"] = {"errors": 2, "warnings": 0}
        generic = build_report(plain)["failure"]
        self.assertNotIn("unresolved_dependencies", generic)
        self.assertIn("无法定位", generic["meaning_zh"])
        self.assertIn("尚未证明", generic["meaning_zh"])
        self.assertEqual(generic["applicability"], "unproven")

    def test_external_review_passes_scope_and_automatic_exclusion_through(self) -> None:
        view = {
            "stages": [
                {
                    "id": "discover",
                    "name_zh": "解析结构",
                    "state": "completed",
                    "input_qc": [{"id": "discovery.inputs", "state": "passed", "details": {}}],
                    "output_qc": [],
                    "inputs": [],
                    "outputs": [],
                    "unsupported": [],
                    "confirmations": [
                        {
                            "id": "design_fidelity",
                            "label": "设计与实物对应",
                            "reference": "docs/mechanical-handoff-spec.md#design",
                            "review_stage": "discover",
                            "scope": "确认图纸/模型与实物装配一致",
                            "automatic_exclusion": "文件存在性、依赖闭包与版本一致性已由自动校验覆盖",
                            "state": "needs_review",
                        }
                    ],
                }
            ],
            "subject_sha256": None,
            "handoff_sha256": None,
        }
        report = build_report({}, view=view)
        confirmation = report["stages"][0]["confirmations"][0]
        self.assertEqual(confirmation["id"], "design_fidelity")
        self.assertEqual(confirmation["state"], "needs_review")
        self.assertIn("实物", confirmation["scope"])
        self.assertIn("自动校验", confirmation["automatic_exclusion"])
        scopes = report["external_review"]["scopes"]
        self.assertEqual(scopes[0]["id"], "design_fidelity")
        self.assertEqual(scopes[0]["stage"], "discover")

    def test_running_and_empty_states_never_claim_passes(self) -> None:
        events = [
            {
                "at": "t",
                "stage": "freeze",
                "state": "running",
                "check": {"id": "handoff.admission", "boundary": "input", "state": "passed", "details": {}},
            },
            {
                "at": "t",
                "stage": "freeze",
                "state": "running",
                "check": {"id": "handoff.integrity", "boundary": "output", "state": "passed", "details": {}},
            },
            {"at": "t", "stage": "freeze", "state": "completed"},
            {"at": "t", "stage": "discover", "state": "running"},
        ]
        report = build_report({"status": "running", "events": events})
        self.assertEqual(report["overall"]["state"], "running")
        stages = {stage["id"]: stage for stage in report["stages"]}
        self.assertEqual(stages["freeze"]["state"], "completed")
        self.assertEqual(stages["discover"]["state"], "running")
        self.assertEqual(stages["capture"]["state"], "not_run")
        empty = build_report({})
        self.assertEqual(empty["overall"]["state"], "not_run")
        self.assertEqual(empty["overall"]["stages_completed"], 0)
        self.assertIsNone(empty["failure"])
        self.assertEqual(
            empty["stages"][0]["counts"]["boundary"],
            {"passed": 0, "executed": 0, "total": 2},
        )

    def test_incomplete_boundary_rows_never_fabricate_passes(self) -> None:
        job = passed_job()
        job["events"] = [
            event for event in job["events"] if (event.get("check") or {}).get("id") != "discovery.definition"
        ]
        report = build_report(job)
        stages = {stage["id"]: stage for stage in report["stages"]}
        discover = stages["discover"]
        self.assertEqual(discover["state"], "failed")
        counts = discover["counts"]["boundary"]
        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["passed"], 2)
        self.assertEqual(counts["executed"], 2)
        self.assertEqual(report["overall"]["state"], "failed")


if __name__ == "__main__":
    unittest.main()
