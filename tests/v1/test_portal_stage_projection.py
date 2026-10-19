"""Registered platform rows reconstruct the portal stage view without weakening its gates."""

from __future__ import annotations

import json
import unittest

from description_pipeline.orchestration.portal import PortalApp, PortalError
from description_pipeline.orchestration.report_view import build_report
from description_pipeline.stages import CONTRACT, stage_view
from tests.v1.protocol_support import protocol_events

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
    def app(self, report: dict) -> PortalApp:
        app = PortalApp.__new__(PortalApp)
        app._registered_stage_report = lambda projection, *, run_id, stage: report
        return app

    def projection(self, states: dict[str, str]) -> dict:
        return {
            "run": {"run_id": RUN_ID},
            "stages": {
                stage: {
                    "stage": stage,
                    "state": state,
                    "reused_from_run": None,
                    "updated_at": "2026-10-20T02:49:41+08:00",
                }
                for stage, state in states.items()
            },
            "by_stage": {},
        }

    def test_complete_projection_keeps_native_checks_and_final_subject(self) -> None:
        job = {
            "run_id": RUN_ID,
            "status": "passed",
            "result": {"subject_sha256": SUBJECT},
            "events": [{"stage": stage["id"], "state": "completed"} for stage in CONTRACT["stages"]],
        }
        display = self.app(registered_report())._registered_stage_job(
            job,
            self.projection({stage["id"]: "completed" for stage in CONTRACT["stages"]}),
        )
        view = stage_view(display)
        self.assertEqual([stage["state"] for stage in view["stages"]], ["completed"] * 6)
        self.assertTrue(
            all(
                check["state"] == "passed"
                for stage in view["stages"]
                for check in stage["input_qc"] + stage["output_qc"]
            )
        )
        self.assertEqual(view["subject_sha256"], SUBJECT)

    def test_registered_row_preserves_time_and_reuse_lineage(self) -> None:
        report = registered_report()
        projection = self.projection({stage["id"]: "completed" for stage in CONTRACT["stages"]})
        projection["stages"]["generate"]["reused_from_run"] = "source-run"
        projection["stages"]["generate"]["checkpoint_uri"] = "store://sealed/source-run/generate"
        projection["stages"]["generate"]["updated_at"] = "2026-10-20T03:00:00+08:00"
        job = {
            "run_id": RUN_ID,
            "result": {"subject_sha256": SUBJECT},
            "events": [
                {
                    "stage": "generate",
                    "state": "completed",
                    "reuse": {"parent_run": "forged-run", "source_run": "forged-run", "reused": True},
                }
            ],
        }
        display = self.app(report)._registered_stage_job(job, projection)
        generate = next(stage for stage in stage_view(display)["stages"] if stage["id"] == "generate")
        self.assertEqual(generate["at"], "2026-10-20T03:00:00+08:00")
        self.assertEqual(
            generate["reuse"],
            {"parent_run": "source-run", "reused": True, "source_run": "source-run"},
        )

    def test_reuse_report_may_bind_the_proven_checkpoint_origin(self) -> None:
        for source, immediate_parent in (
            ("parent-run", "parent-run"),
            ("origin-run", "parent-run"),
        ):
            with self.subTest(source=source):
                app = PortalApp.__new__(PortalApp)
                payload = json.dumps(registered_report(source), sort_keys=True).encode("utf-8")

                def member_bytes(path, member, data=payload):
                    return data

                app._registered_member_bytes = member_bytes
                projection = {
                    "run": {"run_id": RUN_ID},
                    "stages": {
                        "generate": {
                            "stage": "generate",
                            "state": "completed",
                            "reused_from_run": immediate_parent,
                            "checkpoint_uri": f"store://sealed/{source}/generate",
                        }
                    },
                    "by_stage": {"generate": {"reports/native-stages.json": {"sha256": "a" * 64}}},
                }
                report = app._registered_stage_report(projection, run_id=RUN_ID, stage="generate")
                self.assertEqual(report["run_id"], source)

    def test_reuse_report_cannot_bind_an_unproven_source(self) -> None:
        app = PortalApp.__new__(PortalApp)
        payload = json.dumps(registered_report("other-run"), sort_keys=True).encode("utf-8")
        app._registered_member_bytes = lambda path, member: payload
        projection = {
            "run": {"run_id": RUN_ID},
            "stages": {
                "generate": {
                    "stage": "generate",
                    "state": "completed",
                    "reused_from_run": "parent-run",
                    "checkpoint_uri": "store://sealed/origin-run/generate",
                }
            },
            "by_stage": {"generate": {"reports/native-stages.json": {"sha256": "a" * 64}}},
        }
        with self.assertRaisesRegex(PortalError, "conflicts with its run"):
            app._registered_stage_report(projection, run_id=RUN_ID, stage="generate")

    def test_partial_rows_cannot_turn_a_native_failure_into_success(self) -> None:
        events = protocol_events(subject=SUBJECT, failed_stage="capture")
        events.extend(
            {"stage": stage, "state": "blocked"}
            for stage in ("generate", "verify", "publish")
        )
        events.append({"stage": "binding", "state": "failed"})
        job = {
            "run_id": RUN_ID,
            "status": "failed",
            "result": {"subject_sha256": SUBJECT},
            "events": events,
        }
        display = self.app(registered_report())._registered_stage_job(
            job,
            self.projection({"freeze": "completed", "discover": "completed"}),
        )
        view = stage_view(display)
        states = {stage["id"]: stage["state"] for stage in view["stages"]}
        self.assertEqual(states["capture"], "failed")
        self.assertEqual(states["publish"], "blocked")
        job_without_failure_event = {**job, "events": []}
        pending = self.app(registered_report())._registered_stage_job(
            job_without_failure_event,
            self.projection({"freeze": "completed", "discover": "completed"}),
        )
        pending_view = stage_view(pending)
        self.assertEqual(build_report(job_without_failure_event, view=pending_view)["overall"]["state"], "failed")

    def test_completed_row_without_receipt_report_stays_failed(self) -> None:
        job = {
            "run_id": RUN_ID,
            "events": [{"stage": "discover", "state": "completed"}],
        }
        display = self.app(None)._registered_stage_job(
            job,
            self.projection({"discover": "completed"}),
        )
        discover = next(stage for stage in stage_view(display)["stages"] if stage["id"] == "discover")
        self.assertEqual(discover["state"], "failed")
        self.assertEqual(discover["checks_passed"], 0)

    def test_completed_row_without_report_preserves_only_its_recorded_checks(self) -> None:
        events = [
            {
                "stage": "freeze",
                "state": "running",
                "check": {
                    "id": "handoff.admission",
                    "boundary": "input",
                    "state": "passed",
                    "details": {"handoff_sha256": SUBJECT},
                },
            },
            {
                "stage": "freeze",
                "state": "running",
                "check": {
                    "id": "handoff.integrity",
                    "boundary": "output",
                    "state": "passed",
                    "details": {"handoff_sha256": SUBJECT},
                },
            },
            {
                "stage": "freeze",
                "state": "running",
                "check": {
                    "id": "invented",
                    "boundary": "output",
                    "state": "passed",
                    "details": {},
                },
            },
            {"stage": "freeze", "state": "completed"},
        ]
        job = {"run_id": RUN_ID, "events": events}
        display = self.app(None)._registered_stage_job(
            job,
            self.projection({"freeze": "completed"}),
        )
        freeze = next(stage for stage in stage_view(display)["stages"] if stage["id"] == "freeze")
        self.assertEqual(freeze["state"], "completed")
        self.assertEqual(freeze["checks_passed"], 2)
        self.assertNotIn("invented", {row["id"] for row in freeze["input_qc"] + freeze["output_qc"]})


if __name__ == "__main__":
    unittest.main()
