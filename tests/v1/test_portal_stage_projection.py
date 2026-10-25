"""Registered platform rows reconstruct the portal stage view without weakening its gates."""

from __future__ import annotations

import hashlib
import json
import unittest

from description_pipeline.orchestration.portal import PortalApp, PortalError
from description_pipeline.orchestration.report_view import build_report
from description_pipeline.stages import CONTRACT, stage_view
from tests.v1.protocol_support import protocol_events
from tests.v1.portal_read_support import RegisteredProjectionArtifacts

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
    def setUp(self) -> None:
        self.artifacts = RegisteredProjectionArtifacts()

    def app(self, report: dict) -> PortalApp:
        app = PortalApp.__new__(PortalApp)
        app._registered_stage_report = lambda projection, *, run_id, stage: report
        return app

    def stage_members(
        self,
        stage: str,
        *,
        producer_run_id: str = RUN_ID,
        payload: bytes = b"{}\n",
    ) -> dict[str, dict]:
        artifact = self.artifacts.register(
            run_id=RUN_ID,
            producer_run_id=producer_run_id,
            stage=stage,
            files={"reports/native-stages.json": payload},
        )
        return {
            "reports/native-stages.json": {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "artifact": artifact,
            }
        }

    def projection(
        self,
        states: dict[str, str],
        *,
        parent_run_id: str | None = None,
        producer_runs: dict[str, str] | None = None,
    ) -> dict:
        producer_runs = producer_runs or {}
        return {
            "run_id": RUN_ID,
            "parent_run_id": parent_run_id,
            "stages": {
                stage: {
                    "stage": stage,
                    "state": state,
                    "updated_at": "2026-10-20T02:49:41+08:00",
                }
                for stage, state in states.items()
            },
            "by_stage": {
                stage: self.stage_members(
                    stage,
                    producer_run_id=producer_runs.get(stage, RUN_ID),
                )
                for stage in states
            },
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
        projection = self.projection(
            {
                stage["id"]: "completed"
                for stage in CONTRACT["stages"]
            },
            parent_run_id="origin-run",
            producer_runs={"generate": "origin-run"},
        )
        projection["stages"]["generate"][
            "reused_from_run"
        ] = "legacy-forgery"
        projection["stages"]["generate"][
            "checkpoint_uri"
        ] = "store://sealed/legacy-forgery/generate"
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
            {
                "parent_run": "origin-run",
                "reused": True,
                "source_run": "origin-run",
            },
        )

    def test_reuse_report_binds_the_artifact_producer_run(self) -> None:
        app = PortalApp.__new__(PortalApp)
        source_run = "origin-run"
        payload = json.dumps(
            registered_report(source_run),
            sort_keys=True,
        ).encode("utf-8")
        app._registered_member_bytes = lambda path, member: payload
        projection = self.projection(
            {"generate": "completed"},
            parent_run_id=source_run,
            producer_runs={"generate": source_run},
        )
        projection["by_stage"]["generate"] = self.stage_members(
            "generate",
            producer_run_id=source_run,
            payload=payload,
        )

        report = app._registered_stage_report(
            projection,
            run_id=RUN_ID,
            stage="generate",
        )

        self.assertIsNotNone(report)
        self.assertEqual(report["run_id"], source_run)

    def test_reuse_requires_the_artifact_to_bind_the_proven_parent(
        self,
    ) -> None:
        app = PortalApp.__new__(PortalApp)
        app._registered_stage_report = (
            lambda projection, *, run_id, stage: registered_report()
        )
        projection = self.projection(
            {"generate": "completed"},
            parent_run_id="parent-run",
            producer_runs={"generate": "other-run"},
        )

        with self.assertRaisesRegex(
            PortalError,
            "no proven parent run",
        ):
            app._registered_stage_job(
                {"run_id": RUN_ID},
                projection,
            )

    def test_reuse_report_cannot_bind_another_artifact_producer(
        self,
    ) -> None:
        app = PortalApp.__new__(PortalApp)
        source_run = "origin-run"
        payload = json.dumps(
            registered_report("other-run"),
            sort_keys=True,
        ).encode("utf-8")
        app._registered_member_bytes = lambda path, member: payload
        projection = self.projection(
            {"generate": "completed"},
            parent_run_id=source_run,
            producer_runs={"generate": source_run},
        )
        projection["by_stage"]["generate"] = self.stage_members(
            "generate",
            producer_run_id=source_run,
            payload=payload,
        )

        with self.assertRaisesRegex(PortalError, "conflicts with its run"):
            app._registered_stage_job(
                {"run_id": RUN_ID},
                projection,
            )

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
