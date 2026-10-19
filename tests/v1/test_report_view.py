"""Regressions for the readable report projection (display-only mapping)."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from description_pipeline.orchestration.report_view import build_report
from description_pipeline.stages import CONTRACT
from description_pipeline.verification.solidworks_urdf import evaluate_bundle
from tests.v1.protocol_support import protocol_events

RUN_ID = "portal-20261009T094843-d5230467"
SHA = "a" * 64
HSHA = "b" * 64


def quality_rows() -> dict:
    rows = [
        {
            "id": "physics.expected_mass",
            "state": "passed",
            "passed": True,
            "details": {"mass_kg": 3.3948682203507135, "expected_kg": [2.0, 4.5]},
        },
        {
            "id": "physics.mass_closure_equality",
            "state": "passed",
            "passed": True,
            "details": {
                "urdf_mass_kg": 3.3948682203507135,
                "whole_cad_mass_kg": 3.3948682203507135,
                "delta_kg": 0.0,
                "atol_kg": 1e-12,
                "rtol": 0.0,
            },
        },
        {
            "id": "consumer.urdf",
            "state": "passed",
            "passed": True,
            "details": {"bodies": 4, "version": "3.13.0", "inputs": {}},
        },
        {"id": "joints.shoulder_pitch_joint", "state": "passed", "passed": True, "details": {}},
        {"id": "verification.complete", "state": "passed", "passed": True, "details": {}},
        {"id": "custom.unknown", "state": "passed", "passed": True, "details": {}},
        {"id": "custom.not_run", "state": "not_run", "passed": False, "details": {}},
        {"id": "custom.contradiction", "state": "passed", "passed": False, "details": {}},
    ]
    return {
        "passed": True,
        "subject_sha256": SHA,
        "checks": rows,
        "required_checks": [row["id"] for row in rows],
    }


def passed_job() -> dict:
    events = protocol_events(subject=SHA)
    injected = {
        "handoff.integrity": {
            "files": {"handoff/a.SLDPRT": "a" * 64, "handoff/b.SLDPRT": "b" * 64, "handoff/c.SLDPRT": "c" * 64}
        },
        "discovery.definition": {"passed": True, "findings": [], "hardware_id": "nd_cfg_gap", "revision": "r1"},
        "runtime.ready": {
            "role": "native",
            "scope": "Native prerequisites; the capture session checks CAD access and document readiness",
            "solidworks_executable": "D:\\DOWNLO~1\\SOLIDWORKS\\SLDWORKS.exe",
        },
        "publication.inputs": {
            "repository_slug": "example/repo",
            "base": "feature/nd_cfg_gap",
            "branch": "work/solidworks/nd_cfg_gap/engineering",
        },
        "publication.git": {"commit": "f" * 40, "copied_staged_committed": "reverified"},
    }
    for event in events:
        identifier = (event.get("check") or {}).get("id")
        if identifier == "verification.gates":
            event["check"]["details"] = {
                "required_checks": ["bundle.subject"],
                "checks": [{"id": "bundle.subject", "state": "passed", "passed": True}],
            }
        elif identifier in injected:
            event["check"]["details"] = injected[identifier]
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
        self.assertEqual(stages["discover"]["name"], "Discover structure")
        freeze_counts = stages["freeze"]["counts"]["boundary"]
        self.assertEqual(freeze_counts, {"passed": 2, "executed": 2, "total": 2})
        self.assertTrue(all(row["executed"] for row in stages["freeze"]["boundary"]))
        self.assertEqual(
            stages["freeze"]["boundary"][0]["label"],
            "Handoff admission (path, regular files, native package)",
        )
        verify = stages["verify"]
        independent = {row["id"]: row for row in verify["independent"]}
        window = independent["physics.expected_mass"]
        self.assertEqual(window["label"], "Expected mass range")
        self.assertEqual(window["summary"]["expected"], "2.0–4.5 kg")
        self.assertIn("3.3948682203507135", window["summary"]["actual"])
        closure = independent["physics.mass_closure_equality"]
        self.assertEqual(closure["summary"]["expected"], "absolute error <= 1e-12 kg")
        self.assertIn("URDF", closure["summary"]["actual"])
        self.assertEqual(independent["joints.shoulder_pitch_joint"]["label"], "Joint: shoulder_pitch_joint")
        self.assertEqual(independent["custom.unknown"]["label"], "custom.unknown")
        gate_row = next(row for row in verify["boundary"] if row["id"] == "verification.gates")
        self.assertEqual(gate_row["summary"]["actual"], "1/1 passed (1 recorded)")
        self.assertEqual(
            verify["counts"]["independent"],
            {"passed": 6, "executed": 7, "total": 8},
        )
        self.assertEqual(len(verify["independent"]), len(quality_rows()["checks"]))
        by_id = {row["id"]: row for row in verify["independent"]}
        self.assertEqual(by_id["custom.not_run"]["state"], "not_run")
        self.assertFalse(by_id["custom.not_run"]["executed"])
        self.assertEqual(by_id["custom.contradiction"]["state"], "failed")
        self.assertTrue(by_id["custom.contradiction"]["executed"])
        self.assertEqual(report["measured"]["expected_mass_window"]["expected_kg"], [2.0, 4.5])
        self.assertEqual(report["measured"]["mass_closure"]["delta_kg"], 0.0)
        discover = stages["discover"]
        expected_scopes = [item for item in CONTRACT["confirmations"] if item.get("review_stage") == "discover"]
        self.assertEqual(len(discover["confirmations"]), len(expected_scopes))
        for item in discover["confirmations"]:
            self.assertEqual(
                set(item),
                {
                    "id",
                    "label",
                    "reference",
                    "review_stage",
                    "scope",
                    "automatic_exclusion",
                    "state",
                    "approval_tracking",
                },
            )
            self.assertTrue(item["reference"])
        self.assertEqual(report["overall"]["engineering_state"], "external_review")
        self.assertEqual(report["external_review"]["tracking"], "external")
        self.assertIn("does not read", report["external_review"]["note"])
        self.assertTrue(report["external_review"]["scopes"])
        self.assertIn(
            "Handoff admission (path, regular files, native package)",
            stages["freeze"]["automatic"]["passed_labels"],
        )
        self.assertEqual(stages["freeze"]["automatic"]["failed_labels"], [])
        self.assertIn("external review", discover["manual_scope_note"])
        freeze_rows = {row["id"]: row for row in stages["freeze"]["boundary"]}
        self.assertEqual(freeze_rows["handoff.integrity"]["summary"]["actual"], "3 files; inventory verified")
        self.assertIn(
            "Inventory agrees with the handoff digest", freeze_rows["handoff.integrity"]["summary"]["expected"]
        )
        definition = next(row for row in discover["boundary"] if row["id"] == "discovery.definition")
        self.assertIn("Hardware nd_cfg_gap", definition["summary"]["actual"])
        self.assertIn("blocking findings 0", definition["summary"]["actual"])
        runtime = next(row for row in stages["capture"]["boundary"] if row["id"] == "runtime.ready")
        # Native readiness reports prerequisites; the CAD session itself is not claimed here, and
        # the raw host executable path never reaches the operator summary.
        self.assertEqual(runtime["summary"]["expected"], "Native platform and SolidWorks are ready")
        self.assertIn("Windows, COM, and SolidWorks prerequisites", runtime["summary"]["actual"])
        self.assertIn("capture session", runtime["summary"]["actual"])
        self.assertNotIn("not recorded", runtime["summary"]["actual"])
        self.assertNotIn("SLDWORKS.exe", runtime["summary"]["actual"])
        consumer = independent["consumer.urdf"]
        self.assertEqual(consumer["label"], "Consumer URDF loadability")
        self.assertIn("4 consumer bodies", consumer["summary"]["actual"])
        self.assertIn("3.13.0", consumer["summary"]["actual"])
        publish_rows = {row["id"]: row for row in stages["publish"]["boundary"]}
        self.assertEqual(
            publish_rows["publication.git"]["label"], "Published commit byte consistency (copy/stage/commit)"
        )
        self.assertIn("byte consistency", publish_rows["publication.git"]["summary"]["scope"])
        self.assertIn("reverified", publish_rows["publication.git"]["summary"]["actual"])
        file_rows = stages["freeze"]["files"]
        self.assertEqual(file_rows[0]["boundary"], "input")
        self.assertEqual(file_rows[0]["label"], "Saved SolidWorks engineering folder")
        self.assertTrue(any(row["boundary"] == "output" for row in file_rows))

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
        self.assertEqual(failure["stage_name"], "Discover structure")
        self.assertEqual(failure["object"], "lowerbody.SLDASM")
        self.assertIn("cannot be located", failure["meaning"])
        self.assertNotIn("missing part", failure["meaning"])
        self.assertNotIn("damaged", failure["meaning"])
        self.assertEqual(failure["solidworks_codes"], {"errors": 2, "warnings": 0})
        self.assertEqual(failure["raw_type"], "CadError")
        stages = {stage["id"]: stage for stage in report["stages"]}
        self.assertEqual(stages["discover"]["state"], "failed")
        self.assertIn("Fix the issues", stages["discover"]["manual_scope_note"])
        self.assertIn("Structure definition generation", stages["discover"]["automatic"]["failed_labels"])
        definition = next(row for row in stages["discover"]["boundary"] if row["id"] == "discovery.definition")
        self.assertIn("Failed", definition["summary"]["actual"])
        for downstream in ("capture", "generate", "verify", "publish"):
            with self.subTest(stage=downstream):
                self.assertEqual(stages[downstream]["state"], "blocked")
                self.assertEqual(stages[downstream]["state_label"], "Blocked by upstream failure")
                self.assertEqual(stages[downstream]["counts"]["boundary"]["executed"], 0)
                self.assertTrue(all(not row["executed"] for row in stages[downstream]["boundary"]))

    def test_structured_discovery_failure_groups_findings_without_blame(self) -> None:
        job = passed_job()
        job["status"] = "failed"
        job["events"] = protocol_events(subject=SHA, failed_stage="discover")
        job["error"] = "PipelineError: Required check failed: discovery.definition"
        job["discovery"] = {
            "passed": False,
            "hardware_id": None,
            "revision": None,
            "discovery_sha256": "c" * 64,
            "findings": [
                {
                    "code": "discovery.link_name_missing",
                    "object": f"body:{name}",
                    "message": "no CS_<link> coordinate system is owned by this body",
                    "blocking": True,
                }
                for name in ("a", "b", "c", "d")
            ]
            + [
                {
                    "code": "discovery.joint_unsupported_pattern",
                    "object": "mate:平行43",
                    "message": "the reconstructed mate constraints do not leave exactly one joint motion",
                    "blocking": True,
                },
                {"code": "discovery.unknown_code", "object": "x", "message": "m", "blocking": True},
                {"code": "discovery.link_name_missing", "object": "body:skipped", "message": "w", "blocking": False},
            ],
        }
        failure = build_report(job)["failure"]
        self.assertEqual(failure["title"], "Structure discovery checks failed")
        self.assertEqual(failure["kind"], "definition")
        self.assertEqual(failure["finding_total"], 6)
        self.assertEqual(failure["discovery_sha256"], "c" * 64)
        self.assertEqual(
            failure["finding_counts"][0],
            {
                "code": "discovery.link_name_missing",
                "label": "Rigid-body coordinate system is unidentified",
                "count": 4,
            },
        )
        labels = {group["code"]: group["label"] for group in failure["finding_counts"]}
        self.assertEqual(labels["discovery.unknown_code"], "discovery.unknown_code")
        self.assertEqual(len(failure["finding_samples"]["discovery.link_name_missing"]), 3)
        self.assertIn("6 checks failed", failure["meaning"])
        for word in ("damaged", "input is wrong", "error"):
            self.assertNotIn(word, failure["meaning"])

    def test_later_runtime_failure_is_not_overridden_by_stale_discovery_findings(self) -> None:
        job = passed_job()
        job["status"] = "failed"
        job["events"] = protocol_events(subject=SHA, failed_stage="capture")
        job["error"] = "CadError: C:\\packages\\imports\\x\\robot.SLDASM"
        job["detail"] = {"errors": 2, "warnings": 0}
        job["discovery"] = {
            "passed": False,
            "discovery_sha256": "c" * 64,
            "findings": [
                {"code": "discovery.link_name_missing", "object": "body:a", "message": "no CS", "blocking": True}
            ],
        }
        failure = build_report(job)["failure"]
        self.assertEqual(failure["stage"], "capture")
        self.assertEqual(failure["title"], "Native CAD open failed")
        self.assertEqual(failure["kind"], "runtime")
        self.assertNotIn("finding_counts", failure)
        self.assertNotIn("finding_total", failure)

    def test_discovery_failure_without_findings_keeps_the_generic_fallback(self) -> None:
        job = passed_job()
        job["status"] = "failed"
        job["events"] = protocol_events(subject=SHA, failed_stage="discover")
        job["error"] = "PipelineError: Required check failed: discovery.definition"
        failure = build_report(job)["failure"]
        self.assertEqual(failure["title"], "Stage failed (cause undetermined)")
        self.assertEqual(failure["kind"], "unknown")

    def test_failed_summary_points_to_findings_and_never_blanks(self) -> None:
        from description_pipeline.orchestration.report_view import _failed_summary

        long_row = _failed_summary("Structure definition generation", None, {"error": "x" * 300})
        self.assertTrue(long_row["actual"].startswith("Failed: "))
        self.assertIn("see the issue list and raw evidence", long_row["actual"])
        blank_row = _failed_summary("Structure definition generation", None, {})
        self.assertIn("Failed", blank_row["actual"])
        self.assertIn("reason not recorded", blank_row["actual"])

    def test_exact_unresolved_references_render_only_when_recorded(self) -> None:
        job = passed_job()
        job["status"] = "failed"
        job["events"] = protocol_events(subject=SHA, failed_stage="discover")
        job["error"] = "CadError: C:\\description-v1-review\\packages\\imports\\x\\lowerbody.SLDASM"
        job["detail"] = {
            "errors": 2,
            "warnings": 0,
            "unresolved_dependencies": [
                {"name": "imported_part.SLDPRT", "path": "C:\\author\\imported_part.SLDPRT"},
                {"name": "bracket_a.stp.SLDASM", "path": "C:\\temp\\swx\\bracket_a.stp.SLDASM"},
            ],
        }
        failure = build_report(job)["failure"]
        self.assertEqual(failure["object"], "lowerbody.SLDASM")
        self.assertEqual(
            [item["name"] for item in failure["unresolved_dependencies"]],
            ["imported_part.SLDPRT", "bracket_a.stp.SLDASM"],
        )
        self.assertIn("imported_part.SLDPRT", failure["meaning"])
        self.assertIn("cannot be located", failure["meaning"])
        self.assertIn("not yet proven", failure["meaning"])
        self.assertEqual(failure["applicability"], "unproven")
        plain = passed_job()
        plain["status"] = "failed"
        plain["events"] = protocol_events(subject=SHA, failed_stage="discover")
        plain["error"] = job["error"]
        plain["detail"] = {"errors": 2, "warnings": 0}
        generic = build_report(plain)["failure"]
        self.assertNotIn("unresolved_dependencies", generic)
        self.assertIn("cannot be located", generic["meaning"])
        self.assertIn("not yet proven", generic["meaning"])
        self.assertEqual(generic["applicability"], "unproven")

    def test_missing_details_are_explicit_and_terminal_failure_cannot_pass(self) -> None:
        job = passed_job()
        for event in job["events"]:
            check = event.get("check") or {}
            if check.get("id") == "publication.git":
                check["details"] = {"commit": "f" * 40}
            elif check.get("id") == "discovery.inputs":
                check["details"] = {}
            elif check.get("id") == "discovery.definition":
                check["details"] = {"findings": [{"blocking": None}, {"blocking": False}]}
        report = build_report(job)
        rows = {row["id"]: row for stage in report["stages"] for row in stage["boundary"]}
        self.assertIn("byte recheck not recorded", rows["publication.git"]["summary"]["actual"])
        self.assertEqual(
            rows["discovery.inputs"]["summary"]["actual"],
            "Inventory not recorded; names not recorded",
        )
        self.assertIn("blocking findings 1", rows["discovery.definition"]["summary"]["actual"])
        job["status"] = "failed"
        self.assertEqual(build_report(job)["overall"]["state"], "failed")

    def test_revision_summary_does_not_dump_the_full_revision_record(self) -> None:
        job = passed_job()
        for event in job["events"]:
            check = event.get("check") or {}
            if check.get("id") == "input.valid":
                check["details"] = {"cad_revision": {"revision": "r3", "owner": "private-owner", "files": {"a": SHA}}}
        report = build_report(job)
        capture = next(stage for stage in report["stages"] if stage["id"] == "capture")
        row = next(row for row in capture["boundary"] if row["id"] == "input.valid")
        self.assertEqual(row["summary"]["actual"], "CAD revision r3")
        self.assertIn("owner", row["raw_details"]["cad_revision"])

    def test_reused_stage_retains_provenance_without_claiming_new_execution(self) -> None:
        job = passed_job()
        parent = "original-native-run"
        for event in job["events"]:
            if event["stage"] == "freeze":
                event["reuse"] = {"parent_run": parent, "source_run": "original-producer", "reused": True}
                event["at"] = "2026-10-09T08:00:00Z"
        report = build_report(job)
        freeze = report["stages"][0]
        self.assertEqual(freeze["reuse"]["parent_run"], parent)
        self.assertEqual(freeze["reuse"]["source_run"], "original-producer")
        self.assertEqual(freeze["at"], "2026-10-09T08:00:00Z")
        self.assertTrue(all(row["reuse"]["reused"] for row in freeze["boundary"]))
        self.assertNotIn("reuse", report["stages"][1])
        job["events"].append({"stage": "freeze", "state": "running"})
        self.assertNotIn("reuse", build_report(job)["stages"][0])

    def test_main_assembly_ambiguity_is_not_reported_as_unreadable_cad(self) -> None:
        job = passed_job()
        job.update(
            status="failed",
            events=protocol_events(subject=SHA, failed_stage="discover"),
            error="CadError: no unique delivered assembly",
            error_code="native_discovery_main_assembly_ambiguous",
        )
        failure = build_report(job)["failure"]
        self.assertEqual(failure["title"], "Main assembly is not unique")
        self.assertIn("dp.delivery_configuration", failure["meaning"])

    def test_external_review_passes_scope_and_automatic_exclusion_through(self) -> None:
        view = {
            "stages": [
                {
                    "id": "discover",
                    "name": "Discover structure",
                    "state": "completed",
                    "input_qc": [{"id": "discovery.inputs", "state": "passed", "details": {}}],
                    "output_qc": [],
                    "inputs": [],
                    "outputs": [],
                    "unsupported": [],
                    "confirmations": [
                        {
                            "id": "design_fidelity",
                            "label": "Design-to-physical correspondence",
                            "reference": "docs/mechanical-handoff-spec.md#design",
                            "review_stage": "discover",
                            "scope": "Confirm that drawings, model, and physical assembly agree",
                            "automatic_exclusion": (
                                "File existence, dependency closure, and revision consistency "
                                "are already covered by automatic checks"
                            ),
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
        self.assertIsNone(confirmation["approval_tracking"])
        self.assertIn("physical", confirmation["scope"])
        self.assertIn("automatic checks", confirmation["automatic_exclusion"])
        scopes = report["external_review"]["scopes"]
        self.assertEqual(scopes[0]["id"], "design_fidelity")
        self.assertEqual(scopes[0]["stage"], "discover")

    def test_real_evaluator_failures_render_reason_and_never_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_bundle(Path(tmp))
        self.assertIs(report.get("passed"), False)
        job = passed_job()
        job["status"] = "failed"
        job["events"] = protocol_events(subject=SHA, failed_stage="verify")
        job["result"] = {"passed": False, "subject_sha256": SHA, "quality": report}
        rendered = build_report(job)
        verify = next(stage for stage in rendered["stages"] if stage["id"] == "verify")
        rows = verify["independent"]
        self.assertTrue(rows)
        self.assertFalse(any(row["state"] == "passed" for row in rows))
        self.assertTrue(any(row["state"] == "failed" for row in rows))
        for row in rows:
            if row["state"] == "failed":
                self.assertTrue(row["summary"]["actual"].startswith("Failed"))
            else:
                self.assertEqual(row["state"], "not_run")
                self.assertFalse(row["executed"])
        self.assertTrue(any("Missing" in row["summary"]["actual"] for row in rows if row["state"] == "failed"))
        self.assertEqual(rendered["overall"]["state"], "failed")
        self.assertEqual(rendered["overall"]["engineering_state"], "not_ready")

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

    def test_declaration_warnings_render_values_without_passing_or_confirmations(self) -> None:
        job = passed_job()
        job["discovery"] = {
            "passed": True,
            "discovery_sha256": "d" * 64,
            "findings": [
                {
                    "code": "discovery.exporter_membership_uncovered",
                    "object": "assembly",
                    "message": "active material occurrences remain unassigned; capture retains their CAD "
                    "geometry and mass, but complete material coverage is not qualified",
                    "blocking": False,
                    "detail": {
                        "components": ["unowned-1", "unowned-2"],
                        "mass_kg": 0.42,
                        "qualification_blocker": "complete_material_coverage",
                    },
                },
                {
                    "code": "discovery.joint_limits_degenerate",
                    "object": "joint:r_knee",
                    "message": "equal authored position bounds are retained for capture and generation",
                    "blocking": False,
                    "detail": {"limits": {"lower": 0.0, "upper": 0.0}, "qualification_blocker": "usable_joint_range"},
                },
                {
                    "code": "discovery.joint_drive_unspecified",
                    "object": "joint:r_knee",
                    "message": "authored zero effort or velocity is retained for capture and generation",
                    "blocking": False,
                    "detail": {
                        "limits": {"effort": 0.0, "velocity": 0.0},
                        "qualification_blocker": "usable_joint_drive",
                    },
                },
                {"code": "discovery.link_name_missing", "object": "body:a", "message": "no CS", "blocking": True},
            ],
        }
        report = build_report(job)
        self.assertEqual(report["overall"]["state"], "passed")
        self.assertEqual(report["overall"]["engineering_state"], "external_review")
        self.assertEqual(report["warnings"]["total"], 3)
        self.assertEqual(
            {row["code"] for row in report["warnings"]["items"]},
            {
                "discovery.exporter_membership_uncovered",
                "discovery.joint_limits_degenerate",
                "discovery.joint_drive_unspecified",
            },
        )
        stages = {stage["id"]: stage for stage in report["stages"]}
        self.assertEqual(stages["discover"]["counts"]["warnings"], 3)
        uncovered = next(
            row for row in report["warnings"]["items"] if row["code"] == "discovery.exporter_membership_uncovered"
        )
        self.assertEqual(uncovered["label"], "Unassigned instances remain (geometry and mass retained)")
        self.assertEqual(uncovered["components"], ["unowned-1", "unowned-2"])
        self.assertEqual(uncovered["mass_kg"], 0.42)
        degenerate = next(
            row for row in report["warnings"]["items"] if row["code"] == "discovery.joint_limits_degenerate"
        )
        self.assertEqual(degenerate["object"], "joint:r_knee")
        self.assertEqual(degenerate["limits"], {"lower": 0.0, "upper": 0.0})
        self.assertEqual(degenerate["state_label"], "Warning")
        for row in report["warnings"]["items"]:
            with self.subTest(code=row["code"]):
                self.assertEqual(row["state_label"], "Warning")
                self.assertIs(row["blocking"], False)
                self.assertNotIn("Passed", row["state_label"])
        self.assertNotIn("link_name_missing", {row["code"] for row in report["warnings"]["items"]})
        self.assertNotIn(
            "Unassigned instances remain (geometry and mass retained)",
            stages["discover"]["automatic"]["passed_labels"],
        )
        baseline = build_report(passed_job())
        self.assertEqual(len(report["external_review"]["scopes"]), len(baseline["external_review"]["scopes"]))
        scopes = repr(report["external_review"]["scopes"])
        for code in ("discovery.exporter_membership_uncovered", "discovery.joint_limits_degenerate"):
            self.assertNotIn(code, scopes)

    def test_saved_pose_diagnostics_render_as_warnings_and_never_fail_the_check(self) -> None:
        job = passed_job()
        job["result"]["quality"]["checks"].append(
            {
                "id": "discovery.declarations",
                "state": "passed",
                "passed": True,
                "details": {
                    "bodies": 26,
                    "joints": 25,
                    "checks": [],
                    "diagnostics": [
                        {
                            "code": "v14.saved_pose_restriction",
                            "blocking": False,
                            "message": "an active native mate restricts the declared joint in the saved CAD pose",
                            "detail": {
                                "joint": "r_knee",
                                "mate": "平行24",
                                "scope": "",
                                "mate_type": "parallel",
                                "components": ["hip-1", "thigh_link-1"],
                                "links": ["r_thigh_yaw", "r_thigh_roll"],
                                "max_abs_residual": 1.0,
                                "max_normalized_residual": 1.0,
                                "threshold": 1e-6,
                            },
                        }
                    ],
                },
            }
        )
        report = build_report(job)
        self.assertEqual(report["overall"]["state"], "passed")
        self.assertEqual(report["overall"]["engineering_state"], "external_review")
        self.assertIsNone(report["failure"])
        verify = next(stage for stage in report["stages"] if stage["id"] == "verify")
        self.assertEqual(verify["counts"]["warnings"], 1)
        warning = verify["warnings"][0]
        self.assertEqual(warning["code"], "v14.saved_pose_restriction")
        self.assertEqual(warning["label"], "Saved-pose restriction (diagnostic)")
        self.assertEqual(warning["state_label"], "Warning")
        self.assertEqual(warning["object"], "joint:r_knee")
        self.assertEqual(warning["mate"], "平行24")
        self.assertEqual(warning["components"], ["hip-1", "thigh_link-1"])
        self.assertEqual(warning["links"], ["r_thigh_yaw", "r_thigh_roll"])
        self.assertEqual(
            warning["residual"], {"max_abs_residual": 1.0, "max_normalized_residual": 1.0, "threshold": 1e-6}
        )
        check = next(row for row in verify["independent"] if row["id"] == "discovery.declarations")
        self.assertEqual(check["state"], "passed")


if __name__ == "__main__":
    unittest.main()
