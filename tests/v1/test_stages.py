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
    CONTRACT,
    HOST_IDS,
    STAGE_IDS,
    checked,
    canonical_freeze_timestamp,
    compact_view,
    contract_markdown,
    record_check,
    require_complete,
    stage_view,
    validate_freeze_events,
)
from description_pipeline.verification.solidworks_urdf import evaluate_bundle, require_qualified_report
from tests.v1.freeze_event_support import freeze_events


class StageTests(unittest.TestCase):
    def stage(self, view, name):
        return next(item for item in view["stages"] if item["id"] == name)

    def test_stage_contract_declares_the_execution_host_per_stage(self):
        expected = {
            "freeze": ("linux",),
            "discover": ("windows", "linux"),
            "capture": ("windows", "linux"),
            "generate": ("linux",),
            "verify": ("linux",),
            "publish": ("linux",),
        }
        self.assertEqual(HOST_IDS, ("linux", "windows"))
        self.assertEqual({stage["id"]: tuple(stage["hosts"]) for stage in CONTRACT["stages"]}, expected)
        view = stage_view()
        self.assertEqual({stage["id"]: tuple(stage["hosts"]) for stage in view["stages"]}, expected)
        self.assertEqual({stage["id"]: tuple(stage["hosts"]) for stage in compact_view(view)["stages"]}, expected)

    def test_freeze_events_require_one_canonical_raw_block(self):
        events = freeze_events()
        self.assertEqual(validate_freeze_events(events), events)
        self.assertEqual(canonical_freeze_timestamp("2026-10-24T14:39:24.893914+08:00"), "2026-10-24T06:39:24.893914Z")

        invalids = []
        duplicate = copy.deepcopy(events)
        duplicate[1]["stage"] = "discover"
        invalids.append(duplicate)
        extra = copy.deepcopy(events)
        extra[0]["seq"] = 1
        invalids.append(extra)
        two_completed = copy.deepcopy(events)
        two_completed[1]["state"] = "completed"
        invalids.append(two_completed)
        naive = copy.deepcopy(events)
        naive[0]["at"] = "2026-10-24T00:00:00.000000"
        invalids.append(naive)
        for invalid in invalids:
            with self.subTest(events=invalid), self.assertRaises(PipelineError):
                validate_freeze_events(invalid)

    def test_missing_results_and_green_events_cannot_pass_checks(self):
        for job in ({}, {"events": [{"stage": "capture", "state": "completed"}]}):
            view = stage_view(job)
            self.assertEqual(tuple(stage["id"] for stage in view["stages"]), STAGE_IDS)
            self.assertFalse(
                any(
                    check["state"] == "passed"
                    for stage in view["stages"]
                    for check in stage["input_qc"] + stage["output_qc"]
                )
            )
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
        for report in (
            {"passed": True},
            {"passed": True, "required_checks": ["native"], "checks": []},
            {"passed": True, "required_checks": [1], "checks": [{"id": "x", "state": "passed", "passed": True}]},
        ):
            events = []
            with self.assertRaises(PipelineError):
                checked(
                    events.append,
                    "verify",
                    "output",
                    "verification.gates",
                    lambda report=report: require_qualified_report(
                        report, {"checks": {}, "source": {"bodies": [], "joints": [], "frames": []}}
                    ),
                )
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
                elif (
                    node.func.id == "observed"
                    and isinstance(node.args[0], ast.Constant)
                    and len(node.args) >= 2
                    and node.args[1].value == "passed"
                ):
                    identifier = node.args[0].value
                    boundary = "input" if identifier == "publication.inputs" else "output"
                    producers.append(("publish", boundary, identifier))
        expected = [
            (stage["id"], boundary, check["id"])
            for stage in CONTRACT["stages"]
            for boundary in ("input", "output")
            for check in stage[f"{boundary}_qc"]
        ]
        self.assertCountEqual(producers, expected)

    def test_stage_table_and_mechanical_responsibilities_match_the_contract(self):
        docs = Path(__file__).resolve().parents[2] / "docs"
        contracts = (docs / "contracts.md").read_text()
        section = contracts.split("## 六阶段契约", 1)[1].split("\n## ", 1)[0]
        table = "\n".join(line for line in section.splitlines() if line.startswith("|")).rstrip()
        documented_rows = [
            [cell.strip() for cell in line.strip("|").split("|")]
            for line in table.splitlines()
            if line.startswith("| `")
        ]
        documented_stages = [row[0].strip("`").split("`", 1)[0] for row in documented_rows]
        self.assertEqual(tuple(documented_stages), STAGE_IDS)
        documented_hosts = {
            "freeze": "Linux",
            "discover": "Windows 原生观察 + Linux 推导",
            "capture": "Windows 原生读取 + Linux 传输核验",
            "generate": "Linux",
            "verify": "Linux",
            "publish": "Linux",
        }
        expected_qc = {
            "freeze": (("handoff.admission",), ("handoff.integrity",)),
            "discover": (
                ("discovery.inputs",),
                ("discovery.definition", "discovery.binding"),
            ),
            "capture": (
                ("input.valid", "runtime.ready"),
                ("capture.integrity", "capture.input_stability"),
            ),
            "generate": (("generation.inputs",), ("generation.artifacts",)),
            "verify": (
                ("verification.subject",),
                ("verification.gates", "verification.report_binding"),
            ),
            "publish": (
                ("publication.inputs",),
                ("publication.git", "publication.receipt"),
            ),
        }
        for row, stage in zip(documented_rows, CONTRACT["stages"], strict=True):
            self.assertEqual(row[1], documented_hosts[stage["id"]])
            self.assertEqual(
                tuple(check["id"] for check in stage["input_qc"]),
                expected_qc[stage["id"]][0],
            )
            self.assertEqual(
                tuple(check["id"] for check in stage["output_qc"]),
                expected_qc[stage["id"]][1],
            )
        rendered = contract_markdown()
        rendered_rows = [
            [cell.strip() for cell in line.strip("|").split("|")]
            for line in rendered.splitlines()
            if line.startswith("| `")
        ]
        self.assertEqual(tuple(documented_stages), tuple(row[0].strip("`").split("`", 1)[0] for row in rendered_rows))
        for row, stage in zip(rendered_rows, CONTRACT["stages"], strict=True):
            self.assertEqual(row[0], f"`{stage['id']}` {stage['name']}")
            self.assertEqual(row[1], stage["host_text"])
            self.assertEqual(row[2], stage["doc"]["input"])
            self.assertEqual(row[3], stage["doc"]["output"])
        for stage in CONTRACT["stages"]:
            self.assertTrue(stage["name"])
            self.assertTrue(stage["host_text"].isascii())
            self.assertTrue(stage["doc"]["input"].isascii())
            self.assertTrue(stage["doc"]["output"].isascii())
        spec = (docs / "mechanical-handoff-spec.md").read_text()
        self.assertEqual(len(CONTRACT["confirmations"]), 5)
        self.assertEqual(len({item["id"] for item in CONTRACT["confirmations"]}), 5)
        responsibilities = {}
        for line in spec.splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) == 2 and cells[0] in {
                "结构负责人",
                "机械安全责任人",
                "参数责任人",
                "物理参数责任人",
                "预算责任人",
            }:
                responsibilities[cells[0]] = cells[1]
        self.assertEqual(len(responsibilities), 5)
        expected_responsibility = {
            "design_fidelity": "结构负责人",
            "motion_safety": "机械安全责任人",
            "drive_applicability": "参数责任人",
            "physical_fidelity": "物理参数责任人",
            "acceptance_basis": "预算责任人",
        }
        for item in CONTRACT["confirmations"]:
            self.assertEqual(item["reference"], "docs/mechanical-handoff-spec.md#review")
            self.assertIn(expected_responsibility[item["id"]], responsibilities)
            self.assertTrue(responsibilities[expected_responsibility[item["id"]]])
            self.assertTrue(item["label"].isascii())
            self.assertTrue(item["scope"].isascii())
            self.assertTrue(item["automatic_exclusion"].isascii())
            self.assertIn(item["review_stage"], STAGE_IDS)
        self.assertIn('<a id="stages"></a>', contracts)
        self.assertIn('<a id="review"></a>', spec)
        view = stage_view()
        self.assertEqual({row["stage"] for row in view["unsupported"]}, {"discover", "verify"})

    def test_transport_cannot_create_engineering_review_or_approve_external_facts(self):
        job = {
            "events": [{"stage": "wait_for_job", "state": "completed"}],
            "confirmations": [{"id": "bodies", "state": "confirmed"}],
            "subject_sha256": "a" * 64,
        }
        view = stage_view(job)
        self.assertEqual(len(view["stages"]), 6)
        self.assertNotIn("wait_for_job", STAGE_IDS)
        for stage in view["stages"]:
            self.assertEqual(stage["state"], "not_run")
            for row in stage["confirmations"]:
                self.assertEqual(row["state"], "not_ready")
                self.assertEqual(row["approval_tracking"], "external")
                self.assertEqual(row["subject_sha256"], "a" * 64)
        self.assertEqual(view["unsupported"][0]["state"], "unsupported")
        self.assertEqual(view["release_approval"], "required")

    def test_verified_delivery_exposes_only_external_review_scope(self):
        events = []
        for definition in CONTRACT["stages"]:
            for boundary in ("input", "output"):
                for item in definition[f"{boundary}_qc"]:
                    record_check(events.append, definition["id"], boundary, item["id"], "passed", {})
            events.append({"stage": definition["id"], "state": "completed"})
        job = {"events": events, "subject_sha256": "a" * 64, "confirmations": [{"state": "confirmed"}]}
        view = stage_view(job)
        scopes = [row for stage in view["stages"] for row in stage["confirmations"]]
        self.assertTrue(scopes)
        self.assertTrue(all(row["state"] == "external_review" for row in scopes))
        self.assertTrue(all(row["scope"] and row["automatic_exclusion"] for row in scopes))
        self.assertFalse({row["id"] for row in scopes} & {"dependencies", "saved_state", "revision"})
        self.assertTrue(all(row["approval_tracking"] == "external" for row in scopes))
        # A later PR transport error does not invalidate verified evidence or
        # manufacture a new list of unresolved engineering approvals.
        job["events"].append({"stage": "publish", "state": "failed"})
        job["status"] = "failed"
        after = stage_view(job)
        self.assertTrue(
            all(row["state"] == "external_review" for stage in after["stages"] for row in stage["confirmations"])
        )

    def test_contract_render_is_pure_and_terminal_xcom_excludes_large_evidence(self):
        job = {"events": [], "artifacts": {f"evidence/{i}.json": "a" * 64 for i in range(10000)}}
        before = copy.deepcopy(job)
        view = stage_view(job)
        self.assertEqual(job, before)
        self.assertLess(len(json.dumps(compact_view(view))), 7000)
        table = contract_markdown()
        self.assertEqual(len(table.splitlines()), 8)
        self.assertIn("| Stage | Execution | Input and input checks | Sealed output and output checks |", table)
        for definition in CONTRACT["stages"]:
            self.assertIn(f"`{definition['id']}` {definition['name']}", table)


if __name__ == "__main__":
    unittest.main()
