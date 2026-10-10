"""Portal split wiring: Linux-owned attempts stay readable when Windows is down."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from description_pipeline.orchestration import portal as portal_module
from description_pipeline.orchestration.airflow_client import EndpointError
from description_pipeline.orchestration.linux_store import LinuxStore

RUN = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"
CHILD = "3f6a5c2e-6c41-4a1e-9c2a-2a1f7a1d0c33"
SUBJECT = "a" * 64
TASK_IDS = (
    "resolve_handoff",
    "start_job",
    "wait_for_job",
    "fetch_capture",
    "run_generate",
    "run_verify",
    "run_publish",
    "confirm_job",
)


class _DownEndpoint:
    """A Windows endpoint that is not reachable at all."""

    def get_job(self, run_id):
        raise EndpointError("windows down")

    def rerun_plan(self, run_id):
        raise EndpointError("windows down")


class _EndpointSlot:
    """The portal's Windows endpoint: unreachable until a test assigns one native job."""

    def __init__(self) -> None:
        self.job: dict | None = None
        self.calls = 0

    def get_job(self, run_id):
        self.calls += 1
        if self.job is None:
            raise EndpointError("windows down")
        return self.job

    def rerun_plan(self, run_id):
        self.calls += 1
        raise EndpointError("windows down")


class _ExpiredAirflow:
    """The Airflow session is expired; calls are counted so early refusals can be proven."""

    def __init__(self) -> None:
        self.calls = 0

    def dag_run(self, token, dag_id, dag_run_id):
        self.calls += 1
        raise portal_module.AirflowAuthError("expired")

    def task_instances(self, token, dag_id, dag_run_id):
        self.calls += 1
        return [{"task_id": task_id, "state": "success"} for task_id in TASK_IDS]


class PortalSplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="portal-split-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = LinuxStore(self.tmp / "store")
        self.store.update_meta(
            RUN,
            state="published",
            handoff_sha256="b" * 64,
            subject_sha256=SUBJECT,
            repository_slug="owner/repo",
            repository_base="main",
        )
        capture = self.store.capture_dir(RUN)
        (capture / "input").mkdir(parents=True)
        (capture / "evidence").mkdir()
        (capture / "reports").mkdir()
        generate = self.store.stage_dir(RUN, "generate")
        (generate / "input").mkdir(parents=True)
        (generate / "reports").mkdir(parents=True)
        (generate / "reports/quality.json").write_text(
            json.dumps({"passed": True, "subject_sha256": SUBJECT}), encoding="utf-8"
        )
        (generate / "reports/pr.json").write_text(
            json.dumps(
                {
                    "state": "published",
                    "passed": True,
                    "url": "https://github.com/owner/repo/pull/1",
                    "commit": "c" * 40,
                    "base": "main",
                    "subject_sha256": SUBJECT,
                }
            ),
            encoding="utf-8",
        )
        self.store.update_meta(
            CHILD,
            state="verified",
            source_run_id=RUN,
            resume_from="generate",
            handoff_sha256="b" * 64,
            subject_sha256=SUBJECT,
        )
        verify = self.store.stage_dir(CHILD, "verify")
        (verify / "input").mkdir(parents=True)
        (verify / "reports").mkdir(parents=True)
        (verify / "reports/quality.json").write_text("{}\n", encoding="utf-8")
        self.airflow = _ExpiredAirflow()
        self.windows = _EndpointSlot()
        self.app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=self.airflow,
                endpoint=lambda: self.windows,
                pipeline_store_root=self.store.root,
            )
        )

    def test_snapshot_and_rerun_rows_survive_windows_downtime(self) -> None:
        snapshot = self.app._job_snapshot(RUN, _DownEndpoint())
        self.assertEqual(snapshot["run_id"], RUN)
        self.assertEqual(snapshot["status"], "passed")
        rows = self.app._rerun_rows(RUN)
        self.assertEqual([row["stage"] for row in rows], [stage["id"] for stage in portal_module.CONTRACT["stages"]])
        run_rows = {row["stage"]: row for row in rows}
        self.assertTrue(run_rows["generate"]["eligible"])
        self.assertTrue(run_rows["verify"]["eligible"])
        self.assertFalse(run_rows["publish"]["eligible"])
        self.assertFalse(run_rows["capture"]["eligible"])
        child_rows = {row["stage"]: row for row in self.app._rerun_rows(CHILD)}
        self.assertTrue(child_rows["publish"]["eligible"])
        self.assertFalse(child_rows["verify"]["eligible"])

    def test_store_paths_stay_behind_airflow_authorization(self) -> None:
        with self.assertRaises(portal_module.PortalError) as caught:
            self.app._preview_payload(SimpleNamespace(token="t"), "portal-20261007T000000-abcdef01")
        self.assertEqual(caught.exception.status, HTTPStatus.UNAUTHORIZED)

    def test_native_complete_is_a_live_boundary_for_retry_and_delete(self) -> None:
        """The native boundary must stay retryable, deletable and visibly in progress."""
        job = {"status": "native_complete", "result": {"native_complete": True}}
        self.windows.job = job
        tasks = [
            {"task_id": "resolve_handoff", "state": "success"},
            {"task_id": "start_job", "state": "success"},
            {"task_id": "wait_for_job", "state": "failed"},
            {"task_id": "fetch_capture", "state": "upstream_failed"},
            {"task_id": "run_generate", "state": "upstream_failed"},
            {"task_id": "run_verify", "state": "upstream_failed"},
            {"task_id": "run_publish", "state": "upstream_failed"},
            {"task_id": "confirm_job", "state": "upstream_failed"},
        ]
        assessment = portal_module._retry_assessment({"state": "failed"}, tasks, job, None)
        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.reason, "transport_recovery")
        self.assertIn("wait_for_job", assessment.cleared_tasks)
        self.assertEqual(portal_module._automatic_summary(job)["state"], "running")

        session = portal_module.PortalSession(
            session_id="sid",
            user="user",
            avatar_url="",
            principal="principal",
            token="token",
            csrf_token="csrf",
            created_at=0.0,
            last_seen=0.0,
        )
        # The run ended and every task is terminal: the sealed native half is done evidence.
        self.assertIsNone(self.app._delete_refusal(session, {"state": "success"}, RUN))
        # A native job that is genuinely still executing keeps refusing deletion ...
        self.windows.job = {"status": "running"}
        self.assertEqual(self.app._delete_refusal(session, {"state": "success"}, RUN)[0], "run_active")
        # ... and an unrecognized native status still fails closed.
        self.windows.job = {"status": "unexpected"}
        self.assertEqual(self.app._delete_refusal(session, {"state": "success"}, RUN)[0], "unconfirmed")

    def test_unauthorized_linked_rerun_is_refused_before_side_effects(self) -> None:
        upload_root = self.tmp / "uploads"
        upload_root.mkdir()
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=self.airflow,
                endpoint=lambda: self.windows,
                pipeline_store_root=self.store.root,
                upload_root=upload_root,
            )
        )
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = status

        body = json.dumps({"stage": "publish"}).encode("utf-8")
        response = b"".join(
            app(
                {
                    "REQUEST_METHOD": "POST",
                    "PATH_INFO": f"/api/runs/{RUN}/attempts",
                    "CONTENT_LENGTH": str(len(body)),
                    "wsgi.input": io.BytesIO(body),
                },
                start_response,
            )
        )
        self.assertEqual(captured["status"], "401 Unauthorized")
        self.assertEqual(json.loads(response)["error"], "请使用飞书登录")
        # Nothing downstream was consulted and nothing was reserved on disk.
        self.assertEqual(self.airflow.calls, 0)
        self.assertEqual(self.windows.calls, 0)
        self.assertFalse((upload_root / ".rerun-attempts").exists())
