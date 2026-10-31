"""Registered platform rows reconstruct the portal stage view without weakening its gates."""

from __future__ import annotations

import hashlib
import json
import unittest

from description_pipeline.orchestration.portal import PortalApp, PortalError, _automatic_summary
from description_pipeline.io import digest
from description_pipeline.orchestration.report_view import build_report
from description_pipeline.stages import CONTRACT, stage_view
from description_pipeline.schemas import PUBLICATION_RECEIPT_SCHEMA
from tests.v1.protocol_support import protocol_events
from tests.v1.protocol_support import native_discovery_record
from tests.v1.portal_read_support import RegisteredProjectionArtifacts, registered_certificate_fixture

RUN_ID = "7b7dbb68-b7ae-5545-859e-f351415e0fb9"
SUBJECT = "a" * 64


def registered_report(run_id: str = RUN_ID) -> dict:
    return stage_view(
        {
            "run_id": run_id,
            "result": {"subject_sha256": SUBJECT},
            "events": protocol_events(subject=SUBJECT),
        }
    )


class RegisteredStageProjectionTests(unittest.TestCase):
    def test_display_uses_only_certificate_and_publication_artifact(self) -> None:
        certificate = {"decision": "FAIL", "passed": False, "subject_sha256": SUBJECT,
                       "checks": [], "diagnostics": []}
        publication = {"schema_version": PUBLICATION_RECEIPT_SCHEMA, "run_id": RUN_ID,
                       "command_id": "publish-command", "request_digest": SUBJECT,
                       "publication_subject_sha256": SUBJECT, "repository": "actual/repo",
                       "base_ref": "main", "branch_ref": "work/current", "candidate_parents": ["parent"],
                       "pr_effect": {"observation": {"url": "https://github.com/actual/repo/pull/1",
                                                      "head_oid": "b" * 40, "number": 1}}}
        projection = {
            "run_id": RUN_ID, "state": "succeeded", "request": {},
            "stages": {
                "verify": {"formal_eligible": True, "qualification": {
                    "formal_eligible": True, "scope_kind": "formal", "outcome": "completed"},
                    "receipt": {"quality": {"passed": True, "checks": [{"id": "stale"}]}}},
                "publish": {"command": {"command_id": "publish-command", "request_digest": SUBJECT},
                            "receipt": {"checks_sha256": digest(publication), "submission": {
                    "passed": True, "state": "published", "subject_sha256": SUBJECT,
                    "url": "https://github.com/stale/repo/pull/2"}}},
            },
            "by_stage": {"verify": {"verification-certificate.json": {}},
                         "publish": {"publication-receipt.json": {}}},
        }
        app = PortalApp.__new__(PortalApp)
        # Isolate stale display summaries; HTTP artifact tests cover real pair binding.
        app._bind_verification_certificate = lambda *_args: (None, None, None)
        app._registered_member_bytes = lambda path, member: json.dumps(
            certificate if path.endswith("certificate.json") else publication).encode()
        job = app._fact_display_job(projection)
        self.assertFalse(job["formal_eligible"])
        self.assertFalse(job["result"]["quality"]["passed"])
        self.assertNotEqual(_automatic_summary(job, None)["state"], "passed")
        self.assertEqual(job["pr"]["url"], publication["pr_effect"]["observation"]["url"])

    def setUp(self) -> None:
        self.artifacts = RegisteredProjectionArtifacts()
        self.application = PortalApp.__new__(PortalApp)
        self.application._artifact_store = lambda: self.artifacts

    def stage_members(self, stage: str, files: dict[str, bytes], *, producer: str = RUN_ID) -> dict:
        artifact = self.artifacts.register(run_id=RUN_ID, producer_run_id=producer, stage=stage, files=files)
        return {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "artifact": artifact}
                for name, data in files.items()}

    def projection(self, states: dict[str, str], *, parent: str | None = None) -> dict:
        projection = {"run_id": RUN_ID, "parent_run_id": parent, "stages": {}, "by_stage": {}}
        for stage, state in states.items():
            command = {"command_id": f"command:{stage}", "request_digest": SUBJECT, "input_artifacts": []}
            checks = {"command_id": command["command_id"], "request_digest": SUBJECT,
                      "checks": [{"id": "handoff.integrity" if stage == "freeze" else "native.observation",
                                  "state": "passed", "details": {"files": {"robot.SLDASM": SUBJECT}}}]}
            files = {"receipt.json": b"{}"}
            if stage in {"freeze", "discover"}:
                files = {"checks.json": json.dumps(checks).encode()}
            if stage == "discover":
                files["prepared/discovery/native-discovery.json"] = json.dumps(native_discovery_record(RUN_ID)).encode()
            if stage == "capture" and state == "completed":
                files = {"reports/native-stages.json": json.dumps(registered_report()).encode()}
            projection["by_stage"][stage] = self.stage_members(stage, files)
            projection["stages"][stage] = {
                "stage": stage, "state": state, "command": command,
                "decision": {"outcome": state, "reason_code": "capture.native_failed" if state == "failed" else None},
                "receipt": {"checks_sha256": digest(checks)}, "updated_at": "2026-10-29T01:09:49Z",
            }
        preceding = "completed"
        for definition in CONTRACT["stages"]:
            stage = definition["id"]
            row = projection["stages"].setdefault(stage, {
                "stage": stage, "state": "waiting" if preceding == "completed" else "blocked",
            })
            preceding = row["state"]
        return projection

    def test_diagnostic_scope_and_pending_states_remain_canonical(self) -> None:
        projection = self.projection({"capture": "active"})
        for stage, row in projection["stages"].items():
            row["in_scope"] = stage == "capture"
        projection["stages"]["publish"]["state"] = "waiting"
        view = self.application._registered_stage_view({}, projection)
        self.assertEqual(view["execution_scope"], ["capture"])
        self.assertEqual(view["stages"][-1]["state"], "waiting")
        self.assertEqual(
            [stage["in_scope"] for stage in view["stages"]], [False, False, True, False, False, False, False],
        )

    def test_actual_stage_checks_keep_completed_predecessors_and_exact_failed_stage(self) -> None:
        projection = self.projection({"freeze": "completed", "discover": "completed", "capture": "failed"})
        job = {"run_id": RUN_ID, "status": "failed", "failed_stage": "capture",
               "error_code": "capture.native_failed", "error": "FileNotFoundError: a long CAD path"}
        view = self.application._registered_stage_view(job, projection)
        self.assertEqual([stage["state"] for stage in view["stages"]],
                         ["completed", "completed", "failed", "blocked", "blocked", "blocked", "blocked"])
        self.assertEqual(view["stages"][0]["checks_passed"], 1)
        self.assertEqual(view["stages"][0]["input_inventory_count"], 1)
        report = build_report(job, view=view)
        self.assertEqual(report["failure"]["stage"], "capture")
        self.assertEqual(report["failure"]["error_code"], "capture.native_failed")
        self.assertEqual(report["failure"]["raw_error"], job["error"])
        self.assertEqual(report["stages"][0]["boundary"][0]["boundary"], "stage")

    def test_all_seven_completed_decisions_do_not_require_a_legacy_qc_inventory(self) -> None:
        projection = self.projection({item["id"]: "completed" for item in CONTRACT["stages"]})
        view = self.application._registered_stage_view({"result": {"subject_sha256": SUBJECT}}, projection)
        self.assertEqual([stage["state"] for stage in view["stages"]], ["completed"] * 7)
        self.assertEqual(view["subject_sha256"], SUBJECT)
        self.assertEqual(view["stages"][2]["checks_passed"], 3)
        self.assertTrue(all(item["state"] == "external_review"
                            for stage in view["stages"] for item in stage["confirmations"]))

    def test_stage_states_are_owned_by_the_registered_decisions(self) -> None:
        for canonical, displayed in (("active", "running"), ("cancelled", "cancelled"),
                                     ("unresolved", "unresolved")):
            with self.subTest(state=canonical):
                projection = self.projection({"freeze": "completed", "discover": "completed", "capture": canonical})
                view = self.application._registered_stage_view(
                    {"events": [{"stage": "freeze", "state": "failed"}]}, projection
                )
                self.assertEqual(view["stages"][0]["state"], "completed")
                self.assertEqual(view["stages"][2]["state"], displayed)
                self.assertEqual(view["stages"][3]["state"], "blocked")

    def test_missing_tampered_or_foreign_checks_refuse_without_redeciding_business_outcome(self) -> None:
        for mutation in ("missing", "bytes", "digest", "command"):
            with self.subTest(mutation=mutation):
                projection = self.projection({"freeze": "completed"})
                row = projection["stages"]["freeze"]
                member = projection["by_stage"]["freeze"]["checks.json"]
                if mutation == "missing":
                    projection["by_stage"]["freeze"] = {}
                elif mutation == "bytes":
                    self.artifacts.objects[member["artifact"]["artifact_id"]]["checks.json"] = b"{}"
                elif mutation == "digest":
                    row["receipt"]["checks_sha256"] = "b" * 64
                else:
                    row["command"]["command_id"] = "another-command"
                with self.assertRaises(PortalError):
                    self.application._registered_stage_view({}, projection)
                self.assertEqual(row["state"], "completed")

    def test_reused_output_requires_a_proven_parent(self) -> None:
        projection = self.projection({"generate": "completed"}, parent="parent-run")
        projection["by_stage"]["generate"] = self.stage_members(
            "generate", {"receipt.json": b"{}"}, producer="parent-run"
        )
        view = self.application._registered_stage_view({}, projection)
        self.assertEqual(view["stages"][3]["reuse"]["source_run"], "parent-run")
        projection["by_stage"]["generate"] = self.stage_members(
            "generate", {"receipt.json": b"{}"}, producer="other-run"
        )
        with self.assertRaisesRegex(PortalError, "no proven parent"):
            self.application._registered_stage_view({}, projection)

    def test_native_capture_report_must_bind_its_actual_artifact_producer(self) -> None:
        projection = self.projection({"capture": "completed"})
        projection["by_stage"]["capture"] = self.stage_members(
            "capture", {"reports/native-stages.json": json.dumps(registered_report("other-run")).encode()})
        with self.assertRaisesRegex(PortalError, "conflicts with its run"):
            self.application._registered_stage_view({}, projection)

    def test_sealed_native_failure_is_read_from_the_same_failed_stage(self) -> None:
        projection = self.projection({"capture": "failed"})
        projection["state"] = "failed"
        projection["request"] = {}
        native = {"kind": "capture", "state": "failed", "failure": {
            "code": "capture.native_failed", "message": "FileNotFoundError: exact native CAD path"}}
        projection["by_stage"]["capture"] = self.stage_members(
            "capture", {"native-result.json": json.dumps(native).encode()})
        job = self.application._fact_display_job(projection)
        self.assertEqual(job["error"], native["failure"]["message"])
        self.assertEqual(job["error_code"], "capture.native_failed")
        projection["stages"]["capture"]["decision"]["reason_code"] = "another_reason"
        with self.assertRaisesRegex(PortalError, "conflicts with its stage decision"):
            self.application._fact_display_job(projection)

    def test_completed_discover_cannot_silently_drop_its_source_findings(self) -> None:
        projection = self.projection({"discover": "completed"})
        checks = projection["by_stage"]["discover"]["checks.json"]
        projection["by_stage"]["discover"] = {"checks.json": checks}
        with self.assertRaisesRegex(PortalError, "Discover source evidence is missing"):
            self.application._registered_stage_view({}, projection)
        self.assertEqual(projection["stages"]["discover"]["state"], "completed")

    def test_corrupt_verify_and_publish_members_are_not_empty_evidence(self) -> None:
        for stage, path in (("verify", "verification-certificate.json"), ("publish", "publication-receipt.json")):
            with self.subTest(stage=stage):
                projection = self.projection({stage: "failed"})
                members = self.stage_members(stage, {path: b"{}"})
                projection["by_stage"][stage] = members
                self.artifacts.objects[members[path]["artifact"]["artifact_id"]][path] = b'{"changed":true}'
                with self.assertRaisesRegex(PortalError, "digest verification failed"):
                    self.application._fact_display_job(projection)

    def test_certificate_must_match_its_canonical_result_digest(self) -> None:
        projection = self.projection({"verify": "failed"})
        capture_files = {"input/handoff.json": b"{}"}
        generate_files = {"tool.json": b"{}", "reports/generate.json": b"{}"}
        capture = self.stage_members("capture", capture_files)
        generate = self.stage_members("generate", generate_files)
        certificate = registered_certificate_fixture(
            capture=next(iter(capture.values()))["artifact"],
            generate=next(iter(generate.values()))["artifact"],
            capture_files=capture_files, generate_files=generate_files,
            report={"passed": False},
        )
        projection["by_stage"]["verify"] = self.stage_members(
            "verify", {"verification-certificate.json": json.dumps(certificate).encode()},
        )
        projection["stages"]["verify"]["receipt"]["checks_sha256"] = "0" * 64
        with self.assertRaisesRegex(PortalError, "differs from its stage result"):
            self.application._fact_display_job(projection)

    def test_publication_must_match_canonical_digest_and_command_identity(self) -> None:
        for mismatch in ("digest", "schema", "command_id", "request_digest", "run_id"):
            with self.subTest(mismatch=mismatch):
                projection = self.projection({"publish": "completed"})
                command = projection["stages"]["publish"]["command"]
                publication = {
                    "schema_version": PUBLICATION_RECEIPT_SCHEMA, "run_id": RUN_ID,
                    "command_id": command["command_id"], "request_digest": command["request_digest"],
                }
                if mismatch == "schema":
                    publication = {}
                elif mismatch != "digest":
                    publication[mismatch] = "another-identity"
                projection["by_stage"]["publish"] = self.stage_members(
                    "publish", {"publication-receipt.json": json.dumps(publication).encode()},
                )
                projection["stages"]["publish"]["receipt"]["checks_sha256"] = (
                    "0" * 64 if mismatch == "digest" else digest(publication)
                )
                with self.assertRaisesRegex(PortalError, "differs from its canonical stage result"):
                    self.application._fact_display_job(projection)


if __name__ == "__main__":
    unittest.main()
