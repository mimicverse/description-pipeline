"""Portal split wiring: registered attempts stay readable when Windows is down."""

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

from .portal_read_support import RegisteredPlatform

RUN = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"
CHILD = "3f6a5c2e-6c41-4a1e-9c2a-2a1f7a1d0c33"
TASK_IDS = (
    "freeze",
    "discover",
    "capture",
    "generate",
    "verify",
    "publish",
)


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
        self.platform = RegisteredPlatform(self.tmp / "platform")
        self.platform.admit(RUN)
        self.platform.stage(RUN, "capture")
        self.platform.stage(RUN, "generate")
        self.platform.conclude(RUN, "succeeded")
        self.platform.admit(CHILD, from_stage="generate", parent_run_id=RUN)
        self.platform.stage(CHILD, "verify")
        self.airflow = _ExpiredAirflow()
        self.windows = _EndpointSlot()
        self.app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=self.airflow,
                run_control_store=self.platform.run_control,
                config_identity_provider=lambda: "c" * 64,
            )
        )

    def test_store_paths_stay_behind_airflow_authorization(self) -> None:
        with self.assertRaises(portal_module.PortalError) as caught:
            self.app._preview_payload(SimpleNamespace(token="t"), "portal-20261007T000000-abcdef01")
        self.assertEqual(caught.exception.status, HTTPStatus.UNAUTHORIZED)

    def test_canonical_state_is_the_live_boundary_for_retry_and_delete(self) -> None:
        """Deletion follows canonical run and claim state, not the retired job route."""
        job = {"status": "native_complete", "result": {"native_complete": True}}
        self.windows.job = job
        tasks = [
            {"task_id": "freeze", "state": "success"},
            {"task_id": "discover", "state": "success"},
            {"task_id": "capture", "state": "failed"},
            {"task_id": "generate", "state": "upstream_failed"},
            {"task_id": "verify", "state": "upstream_failed"},
            {"task_id": "publish", "state": "upstream_failed"},
        ]
        assessment = portal_module._retry_assessment({"state": "failed"}, tasks, job, None)
        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.reason, "sealed_evidence_reimport")
        self.assertEqual(assessment.classification, "evidence_corruption")
        self.assertIn("capture", assessment.cleared_tasks)
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
        # The admitted run is terminal and has no open canonical claim.
        self.assertIsNone(self.app._delete_refusal(session, {"state": "success"}, RUN))
        self.platform.conclude(RUN, "running")
        self.assertEqual(self.app._delete_refusal(session, {"state": "success"}, RUN)[0], "run_active")
        self.platform.conclude(RUN, "unresolved")
        self.assertEqual(
            self.app._delete_refusal(session, {"state": "success"}, RUN)[0],
            "reconciliation_required",
        )
        self.platform.conclude(RUN, "unexpected")
        self.assertEqual(self.app._delete_refusal(session, {"state": "success"}, RUN)[0], "unconfirmed")

    def test_unauthorized_linked_rerun_is_refused_before_side_effects(self) -> None:
        upload_root = self.tmp / "uploads"
        upload_root.mkdir()
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=self.airflow,
                run_control_store=self.platform.run_control,
                upload_root=upload_root,
                config_identity_provider=lambda: "c" * 64,
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
        self.assertEqual(json.loads(response)["error"], "Sign in with Feishu")
        # Nothing downstream was consulted and nothing was reserved on disk.
        self.assertEqual(self.airflow.calls, 0)
        self.assertEqual(self.windows.calls, 0)
        self.assertFalse((upload_root / ".rerun-attempts").exists())
