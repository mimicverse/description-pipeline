"""Stage observations are evidence, never an inferred or fabricated acceptance pass."""

from __future__ import annotations

import copy
import ast
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError
from description_pipeline.stages import (
    CONTRACT, STAGE_IDS, checked, compact_view, contract_markdown, record_check, require_complete, stage_view,
)
from description_pipeline.verification.solidworks_urdf import evaluate_bundle, require_qualified_report


class StageTests(unittest.TestCase):
    def stage(self, view, name):
        return next(item for item in view["stages"] if item["id"] == name)

    def test_missing_results_and_green_events_cannot_pass_checks(self):
        for job in ({}, {"events": [{"stage": "capture", "state": "completed"}]}):
            view = stage_view(job)
            self.assertEqual(tuple(stage["id"] for stage in view["stages"]), STAGE_IDS)
            self.assertFalse(any(check["state"] == "passed" for stage in view["stages"]
                                 for check in stage["input_qc"] + stage["output_qc"]))
        self.assertEqual(self.stage(stage_view(job), "capture")["state"], "failed")
        self.assertEqual(self.stage(stage_view(job), "generate")["state"], "blocked")

    def test_failed_boundary_action_executes_once_and_retains_diagnostics(self):
        calls, events = [], []

        def fail():
            calls.append("read")
            error = PipelineError("Missing original CAD")
            error.details = {"object": "part-1", "path": "native/part.SLDPRT"}
            raise error

        with self.assertRaisesRegex(PipelineError, "Missing original CAD"):
            checked(events.append, "capture", "input", "input.valid", fail)
        self.assertEqual(calls, ["read"])
        view = stage_view({"events": events, "status": "failed"})
        stage = self.stage(view, "capture")
        self.assertEqual(stage["state"], "failed")
        self.assertEqual(stage["input_qc"][0]["details"]["diagnostic"]["object"], "part-1")
        self.assertEqual(self.stage(view, "publish")["state"], "blocked")

    def test_failed_boolean_cannot_be_recorded_as_a_passing_boundary(self):
        events = []
        with self.assertRaises(PipelineError):
            checked(events.append, "verify", "output", "verification.gates", lambda: {"passed": False})
        self.assertEqual(events[0]["check"]["state"], "failed")

    def test_incomplete_delivery_lists_required_unexecuted_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            report = evaluate_bundle(Path(directory))
        rows = {row["id"]: row for row in report["checks"]}
        self.assertFalse(report["passed"])
        self.assertEqual(rows["consumer.urdf"]["state"], "not_run")
        self.assertIs(rows["consumer.urdf"]["passed"], False)
        self.assertTrue(set(report["required_checks"]) <= set(rows))

    def test_check_must_bind_the_same_subject(self):
        events = []
        checked(events.append, "verify", "input", "verification.subject", lambda: {"subject_sha256": "a" * 64})
        view = stage_view({"events": events, "subject_sha256": "b" * 64})
        check = self.stage(view, "verify")["input_qc"][0]
        self.assertEqual(check["state"], "failed")
        self.assertIn("different file subject", check["details"]["error"])

    def test_unknown_gates_or_forged_quality_flag_never_qualify_verification(self):
        for report in ({"passed": True}, {"passed": True, "required_checks": ["native"], "checks": []},
                       {"passed": True, "required_checks": [1],
                        "checks": [{"id": "x", "state": "passed", "passed": True}]}):
            events = []
            with self.assertRaises(PipelineError):
                checked(events.append, "verify", "output", "verification.gates",
                        lambda report=report: require_qualified_report(report))
            self.assertEqual(self.stage(stage_view({"events": events}), "verify")["output_qc"][0]["state"], "failed")

    def test_unknown_boundary_id_is_rejected_before_executing_an_action(self):
        calls = []
        with self.assertRaises(PipelineError):
            checked(None, "capture", "input", "invented_gate", lambda: calls.append("CAD"))
        self.assertEqual(calls, [])
        events = []
        with self.assertRaises(PipelineError):
            record_check(events.append, "publish", "output", "invented_gate", "passed", {})
        self.assertEqual(events, [])

    def test_completed_labels_without_boundary_evidence_cannot_qualify_a_view(self):
        view = stage_view()
        for row in view["stages"]:
            row["state"] = "completed"
        with self.assertRaises(PipelineError):
            require_complete(view)

    def test_scoped_run_cannot_qualify_as_a_complete_native_workflow(self):
        view = stage_view({"execution_scope": ["generate", "verify"]})
        with self.assertRaisesRegex(PipelineError, "all six engineering stages"):
            require_complete(view)

    def test_contract_has_exactly_one_domain_producer_per_boundary(self):
        root = Path(__file__).resolve().parents[2]
        producers = []
        for name in ("steps.py", "repository/urdf_pr.py"):
            tree = ast.parse((root / "src/description_pipeline" / name).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id == "checked":
                    stage, boundary, identifier = [ast.literal_eval(value) for value in node.args[1:4]]
                    producers.append((stage, boundary, identifier))
                elif (node.func.id == "observed" and isinstance(node.args[0], ast.Constant)
                      and len(node.args) >= 2 and node.args[1].value == "passed"):
                    identifier = node.args[0].value
                    boundary = "input" if identifier == "publication.inputs" else "output"
                    producers.append(("publish", boundary, identifier))
        expected = [(stage["id"], boundary, check["id"]) for stage in CONTRACT["stages"]
                    for boundary in ("input", "output") for check in stage[f"{boundary}_qc"]]
        self.assertCountEqual(producers, expected)

    def test_architecture_table_and_mechanical_responsibilities_match_the_contract(self):
        docs = Path(__file__).resolve().parents[2] / "docs"
        design = (docs / "design.md").read_text()
        table = design.split("<!-- stage-contract:start -->\n", 1)[1].split("\n<!-- stage-contract:end -->", 1)[0]
        self.assertEqual(table, contract_markdown())
        spec = (docs / "mechanical-handoff-spec.md").read_text()
        for item in CONTRACT["confirmations"]:
            self.assertIn(f"| {item['label']} |", spec)
            self.assertIn(item["review_stage"], STAGE_IDS)
        view = stage_view()
        self.assertEqual({row["stage"] for row in view["unsupported"]}, {"discover", "verify"})

    def test_transport_is_not_an_engineering_stage_and_manual_remains_pending(self):
        job = {"events": [{"stage": "wait_for_job", "state": "completed"}],
               "confirmations": [{"id": "bodies", "state": "confirmed"}], "subject_sha256": "a" * 64}
        view = stage_view(job)
        self.assertEqual(len(view["stages"]), 6)
        self.assertNotIn("wait_for_job", STAGE_IDS)
        for stage in view["stages"]:
            self.assertEqual(stage["state"], "not_run")
            for row in stage["confirmations"]:
                self.assertEqual(row["state"], "pending")
                self.assertEqual(row["subject_sha256"], "a" * 64)
        self.assertEqual(view["unsupported"][0]["state"], "unsupported")
        self.assertEqual(view["release_approval"], "required")

    def test_contract_render_is_pure_and_terminal_xcom_excludes_large_evidence(self):
        job = {"events": [], "artifacts": {f"evidence/{i}.json": "a" * 64 for i in range(10000)}}
        before = copy.deepcopy(job)
        view = stage_view(job)
        self.assertEqual(job, before)
        self.assertLess(len(json.dumps(compact_view(view))), 7000)
        table = contract_markdown()
        self.assertIn("Input QC", table)
        self.assertIn("Output QC", table)
        for definition in CONTRACT["stages"]:
            self.assertIn(f"`{definition['id']}`", table)


if __name__ == "__main__":
    unittest.main()
