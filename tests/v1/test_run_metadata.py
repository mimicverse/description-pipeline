"""Focused backend tests for shared run rename and reversible soft deletion."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from wsgiref.simple_server import make_server

from tests.v1.test_airflow_client import JOB_SCHEMA, MockEndpoint, PIPELINE_ID
from tests.v1.test_portal import (
    ADMIN_TOKEN,
    AIRFLOW_TOKEN,
    FEISHU_NAME,
    MockAirflow,
    PortalClient,
    VIEWER_TOKEN,
    _actor_envelope,
    _QuietHandler,
    _ThreadingWSGIServer,
)

from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    WindowsEndpoint,
    native_run_id,
)
from description_pipeline.orchestration.portal import AirflowApi, PortalApp, PortalConfig


class RunMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.airflow = MockAirflow()
        self.airflow.__enter__()
        self.addCleanup(self.airflow.__exit__, None, None, None)
        self.upload_root = Path(tempfile.mkdtemp(prefix="portal-upload-"))
        self.addCleanup(shutil.rmtree, self.upload_root, ignore_errors=True)
        self.endpoint_server = MockEndpoint()
        self.endpoint_server.__enter__()
        self.addCleanup(self.endpoint_server.__exit__, None, None, None)
        self.endpoint = WindowsEndpoint(EndpointConfig(base_url=self.endpoint_server.url, token="test-token"))
        self.static_dir = Path(__file__).resolve().parents[2] / "src/description_pipeline/orchestration/static"
        self.server = self._serve()
        self.client = PortalClient(f"http://127.0.0.1:{self.server.server_address[1]}")
        self.client.login()

    # ------------------------------------------------------------------ helpers
    def _serve(self, *, endpoint=None):
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url),
            endpoint=endpoint or (lambda: self.endpoint),
            static_dir=self.static_dir,
            upload_root=self.upload_root,
        )
        server = make_server(
            "127.0.0.1", 0, PortalApp(config), server_class=_ThreadingWSGIServer, handler_class=_QuietHandler
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(self._stop, server)
        return server

    @staticmethod
    def _stop(server) -> None:
        server.shutdown()
        server.server_close()

    def _login(self, token: str) -> PortalClient:
        client = PortalClient(f"http://127.0.0.1:{self.server.server_address[1]}")
        client.login(token)
        return client

    def _seed(
        self,
        dag_run_id: str,
        *,
        state: str = "success",
        owner: str = AIRFLOW_TOKEN,
        tasks: tuple = ("success",),
        conf: dict | None = None,
        start: str = "2026-10-09T00:00:00Z",
    ) -> str:
        profile = self.airflow.profiles[owner]
        self.airflow.dag_runs[dag_run_id] = {
            "dag_run_id": dag_run_id,
            "dag_id": "solidworks_to_urdf",
            "state": state,
            "conf": conf if conf is not None else {"handoff_path": "/srv/robot-cell"},
            "triggering_user_name": _actor_envelope(profile["name"], profile["principal"]),
            "start_date": start,
            "end_date": None,
        }
        self.airflow.task_states[dag_run_id] = [
            {"task_id": f"task-{index}", "state": value} for index, value in enumerate(tasks)
        ]
        return dag_run_id

    @staticmethod
    def _uuid() -> str:
        return str(uuid.uuid4())

    def _list(self, client: PortalClient, **params) -> tuple[int, dict]:
        query = "&".join(f"{key}={value}" for key, value in params.items())
        status, _, body = client.request("GET", f"/api/runs?{query}" if query else "/api/runs")
        return status, json.loads(body)

    # ------------------------------------------------------------------ tests
    def test_rename_delete_restore_round_trip_and_shared_visibility(self) -> None:
        run_id = self._seed(self._uuid())
        status, _, body = self.client.request("PATCH", f"/api/runs/{run_id}", {"title": "装配线 A"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["title"], "装配线 A")
        self.assertEqual(json.loads(body)["deleted"], False)

        status, _, body = self.client.request("DELETE", f"/api/runs/{run_id}")
        self.assertEqual(status, 200, body)
        deleted = json.loads(body)
        self.assertTrue(deleted["deleted"])
        self.assertTrue(deleted["deleted_at"])
        self.assertEqual(deleted["deleted_by"], FEISHU_NAME)

        # Default listing hides the tombstone for everyone; the shared view shows it with its name.
        status, payload = self._list(self.client)
        self.assertNotIn(run_id, [row["dag_run_id"] for row in payload["runs"]])
        status, payload = self._list(self.client, include_deleted=1)
        row = next(item for item in payload["runs"] if item["dag_run_id"] == run_id)
        self.assertTrue(row["deleted"])
        self.assertEqual(row["title"], "装配线 A")

        # The record stays readable, but acting on it is refused until restored.
        status, _, body = self.client.request("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["deleted"])
        status, _, body = self.client.request("POST", f"/api/runs/{run_id}/retry")
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "deleted")
        status, _, body = self.client.request("POST", f"/api/runs/{run_id}/attempts", {"stage": "verify"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "deleted")

        # Renaming a deleted record is allowed (display-only) and restore brings it back.
        status, _, body = self.client.request("PATCH", f"/api/runs/{run_id}", {"title": "装配线 A（待恢复）"})
        self.assertEqual(status, 200, body)
        status, _, body = self.client.request("POST", f"/api/runs/{run_id}/restore")
        self.assertEqual(status, 200, body)
        self.assertFalse(json.loads(body)["deleted"])
        status, payload = self._list(self.client)
        row = next(item for item in payload["runs"] if item["dag_run_id"] == run_id)
        self.assertEqual(row["title"], "装配线 A（待恢复）")
        self.assertFalse(row["deleted"])

        # Idempotency: repeating delete/restore keeps the original tombstone and stays 200.
        status, _, body = self.client.request("DELETE", f"/api/runs/{run_id}")
        first = json.loads(body)["deleted_at"]
        status, _, body = self.client.request("DELETE", f"/api/runs/{run_id}")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["deleted_at"], first)
        status, _, body = self.client.request("POST", f"/api/runs/{run_id}/restore")
        self.assertEqual(status, 200, body)
        status, _, body = self.client.request("POST", f"/api/runs/{run_id}/restore")
        self.assertEqual(status, 200, body)

    def test_authorization_csrf_and_unknown_ids(self) -> None:
        run_id = self._seed(self._uuid())
        viewer = self._login(VIEWER_TOKEN)
        for method, path, payload in (
            ("PATCH", f"/api/runs/{run_id}", {"title": "x"}),
            ("DELETE", f"/api/runs/{run_id}", None),
            ("POST", f"/api/runs/{run_id}/restore", None),
        ):
            with self.subTest(method=method):
                status, _, _ = viewer.request(method, path, payload)
                self.assertEqual(status, 403)
        # Admin acts for another operator.
        admin = self._login(ADMIN_TOKEN)
        status, _, body = admin.request("DELETE", f"/api/runs/{run_id}")
        self.assertEqual(status, 200, body)
        status, _, body = admin.request("POST", f"/api/runs/{run_id}/restore")
        self.assertEqual(status, 200, body)

        # A missing CSRF header is refused for every mutation.
        token, self.client.csrf = self.client.csrf, None
        try:
            for method, path, payload in (
                ("PATCH", f"/api/runs/{run_id}", {"title": "x"}),
                ("DELETE", f"/api/runs/{run_id}", None),
                ("POST", f"/api/runs/{run_id}/restore", None),
            ):
                with self.subTest(method=method):
                    status, _, _ = self.client.request(method, path, payload)
                    self.assertEqual(status, 403)
        finally:
            self.client.csrf = token

        missing = self._uuid()
        for method, path, payload in (
            ("PATCH", f"/api/runs/{missing}", {"title": "x"}),
            ("DELETE", f"/api/runs/{missing}", None),
        ):
            with self.subTest(method=method):
                status, _, _ = self.client.request(method, path, payload)
                self.assertEqual(status, 404)

        # A revoked live session is refused before any metadata change.
        self.airflow.revoked = True
        try:
            status, _, _ = self.client.request("PATCH", f"/api/runs/{run_id}", {"title": "x"})
            self.assertEqual(status, 401)
        finally:
            self.airflow.revoked = False

    def test_delete_refuses_active_or_unknown_execution(self) -> None:
        # Airflow run still running.
        running = self._seed(self._uuid(), state="running")
        status, _, body = self.client.request("DELETE", f"/api/runs/{running}")
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "run_active")
        # Terminal run with an active task.
        active_task = self._seed(self._uuid(), tasks=("success", "running"))
        status, _, body = self.client.request("DELETE", f"/api/runs/{active_task}")
        self.assertEqual(json.loads(body)["reason"], "run_active")
        # Unknown (None) task state must not be treated as terminal.
        unknown_task = self._seed(self._uuid(), tasks=("success", None))
        status, _, body = self.client.request("DELETE", f"/api/runs/{unknown_task}")
        self.assertEqual(json.loads(body)["reason"], "unconfirmed")
        # Native job still running.
        job_run = self._seed(self._uuid())
        native_id = native_run_id(job_run)
        self.endpoint_server.jobs[native_id] = {
            "schema_version": JOB_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": native_id,
            "status": "running",
            "events": [{"stage": "job", "state": "running", "at": "t0"}],
            "result": None,
            "error": None,
            "request": {"run_id": native_id, "package": "handoff/x", "handoff_sha256": "a" * 64},
            "pokes": 0,
        }
        status, _, body = self.client.request("DELETE", f"/api/runs/{job_run}")
        self.assertEqual(json.loads(body)["reason"], "run_active")
        # An authoritative 404 (no native job ever existed) allows hiding a terminal record.
        no_job = self._seed(self._uuid(), state="failed", tasks=("failed",))
        status, _, body = self.client.request("DELETE", f"/api/runs/{no_job}")
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["deleted"])
        # An unreachable endpoint refuses instead of assuming no job exists.
        dead = WindowsEndpoint(EndpointConfig(base_url="http://127.0.0.1:1", token="test-token"))
        dead_server = self._serve(endpoint=lambda: dead)
        dead_client = PortalClient(f"http://127.0.0.1:{dead_server.server_address[1]}")
        dead_client.login()
        unreachable = self._seed(self._uuid())
        status, _, body = dead_client.request("DELETE", f"/api/runs/{unreachable}")
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "unconfirmed")

    def test_title_validation_and_clearing(self) -> None:
        run_id = self._seed(self._uuid())
        for title, expected in (
            ("x" * 121, 400),
            ("line\u0007control", 400),
            ("   ", 400),
            (123, 400),
        ):
            with self.subTest(title=repr(title)[:24]):
                status, _, _ = self.client.request("PATCH", f"/api/runs/{run_id}", {"title": title})
                self.assertEqual(status, expected)
        status, _, body = self.client.request("PATCH", f"/api/runs/{run_id}", {"title": "  named  "})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["title"], "named")
        status, _, body = self.client.request("PATCH", f"/api/runs/{run_id}", {"title": None})
        self.assertEqual(status, 200, body)
        self.assertIsNone(json.loads(body)["title"])
        status, _, body = self.client.request("PATCH", f"/api/runs/{run_id}", {"other": "x"})
        self.assertEqual(status, 400, body)

    def test_pagination_scans_past_deleted_runs(self) -> None:
        run_ids = [self._seed(self._uuid(), start=f"2026-10-09T00:{index:02d}:00Z") for index in range(25)]
        # Delete the three newest: default pages must still surface all older runs.
        for run_id in run_ids[-3:]:
            status, _, body = self.client.request("DELETE", f"/api/runs/{run_id}")
            self.assertEqual(status, 200, body)
        status, first = self._list(self.client, limit=20, offset=0)
        self.assertEqual(status, 200)
        self.assertEqual(len(first["runs"]), 20)
        self.assertEqual(first["next_offset"], 20)
        self.assertTrue(all(not row["deleted"] for row in first["runs"]))
        status, second = self._list(self.client, limit=20, offset=20)
        # The two oldest runs stay reachable even though three younger entries are hidden.
        self.assertEqual([row["dag_run_id"] for row in second["runs"]], [run_ids[1], run_ids[0]])
        self.assertIsNone(second["next_offset"])
        status, shared = self._list(self.client, include_deleted=1, limit=100, offset=0)
        self.assertEqual(len(shared["runs"]), 25)
        self.assertEqual(sum(1 for row in shared["runs"] if row["deleted"]), 3)
        status, _, _ = self.client.request("GET", "/api/runs?limit=0")
        self.assertEqual(status, 400)
        status, _, _ = self.client.request("GET", "/api/runs?offset=-1")
        self.assertEqual(status, 400)

    def test_can_manage_flag_and_single_profile_check_per_list(self) -> None:
        owner_run = self._seed(self._uuid(), owner=AIRFLOW_TOKEN, start="2026-10-09T00:02:00Z")
        other_run = self._seed(self._uuid(), owner=VIEWER_TOKEN, start="2026-10-09T00:01:00Z")
        viewer = self._login(VIEWER_TOKEN)
        hits = self.airflow.hits
        status, payload = self._list(viewer)
        self.assertEqual(status, 200)
        flags = {row["dag_run_id"]: row["can_manage"] for row in payload["runs"]}
        self.assertFalse(flags[owner_run])
        self.assertTrue(flags[other_run])
        # One live profile recheck and one Airflow page request for the whole list.
        self.assertEqual(self.airflow.hits - hits, 2)
        admin = self._login(ADMIN_TOKEN)
        status, payload = self._list(admin)
        self.assertTrue(all(row["can_manage"] for row in payload["runs"]))

    def test_metadata_is_durable_across_portal_restart(self) -> None:
        run_id = self._seed(self._uuid())
        self.client.request("PATCH", f"/api/runs/{run_id}", {"title": "restart check"})
        self.client.request("DELETE", f"/api/runs/{run_id}")
        restarted = self._serve()
        client = PortalClient(f"http://127.0.0.1:{restarted.server_address[1]}")
        client.login()
        status, payload = self._list(client)
        self.assertNotIn(run_id, [row["dag_run_id"] for row in payload["runs"]])
        status, payload = self._list(client, include_deleted=1)
        row = next(item for item in payload["runs"] if item["dag_run_id"] == run_id)
        self.assertEqual(row["title"], "restart check")
        self.assertTrue(row["deleted"])
        status, _, body = client.request("POST", f"/api/runs/{run_id}/restore")
        self.assertEqual(status, 200, body)

    def test_corrupt_ledger_fails_closed_everywhere(self) -> None:
        run_id = self._seed(self._uuid())
        ledger = self.upload_root / ".run-metadata.json"
        ledger.write_text("{ not json", encoding="utf-8")
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 503, body)
        status, _, body = self.client.request("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 503, body)
        status, _, body = self.client.request("PATCH", f"/api/runs/{run_id}", {"title": "x"})
        self.assertEqual(status, 503, body)
        status, _, body = self.client.request("DELETE", f"/api/runs/{run_id}")
        self.assertEqual(status, 503, body)
        # Valid JSON with an invalid entry is refused just as strictly.
        ledger.write_text(
            json.dumps({"schema_version": "solidworks-to-urdf.run-metadata/v1", "runs": {run_id: {"deleted": "yes"}}}),
            encoding="utf-8",
        )
        status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 503, body)

    def test_unknown_native_status_refuses_and_list_failure_is_503(self) -> None:
        run_id = self._seed(self._uuid())
        from unittest import mock

        from description_pipeline.orchestration.portal import AirflowApi as PortalAirflowApi
        from description_pipeline.orchestration.portal import AirflowApiError

        # A native job whose status is neither active nor terminal cannot be hidden.
        with mock.patch.object(WindowsEndpoint, "get_job", return_value={"status": "mystery"}):
            status, _, body = self.client.request("DELETE", f"/api/runs/{run_id}")
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "unconfirmed")
        # An Airflow list failure is an actionable 503, never a silently truncated history.
        with mock.patch.object(PortalAirflowApi, "list_dag_runs", side_effect=AirflowApiError("boom")):
            status, _, body = self.client.request("GET", "/api/runs")
        self.assertEqual(status, 503, body)

    def test_deleted_parent_keeps_child_provenance_and_child_actions(self) -> None:
        parent = self._seed(self._uuid(), start="2026-10-09T00:02:00Z")
        child = self._seed(
            self._uuid(),
            start="2026-10-09T00:01:00Z",
            conf={
                "handoff_path": "/srv/robot-cell",
                "parent_dag_run_id": parent,
                "resume_from": "verify",
            },
        )
        status, _, body = self.client.request("DELETE", f"/api/runs/{parent}")
        self.assertEqual(status, 200, body)
        status, _, body = self.client.request("GET", f"/api/runs/{child}")
        self.assertEqual(status, 200, body)
        detail = json.loads(body)
        self.assertFalse(detail["deleted"])
        # Detail exposes the same authoritative lineage as the list rows.
        self.assertEqual(detail["parent_dag_run_id"], parent)
        self.assertEqual(detail["resume_from"], "verify")
        self.assertTrue(detail["resume_from_name_zh"])
        status, payload = self._list(self.client)
        row = next(item for item in payload["runs"] if item["dag_run_id"] == child)
        self.assertEqual(row["parent_dag_run_id"], parent)
        self.assertFalse(row["deleted"])
        # Starting a new attempt from the deleted parent is refused...
        status, _, body = self.client.request("POST", f"/api/runs/{parent}/attempts", {"stage": "verify"})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["reason"], "deleted")
        # ...while the existing child still starts its own linked attempt.
        child_native = native_run_id(child)
        self.endpoint_server.jobs[child_native] = {
            "schema_version": JOB_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": child_native,
            "status": "failed",
            "events": [{"stage": "job", "state": "failed", "at": "t0"}],
            "result": None,
            "error": "verification failed",
            "request": {"run_id": child_native, "package": "handoff/x", "handoff_sha256": "a" * 64},
            "pokes": 0,
        }
        status, _, body = self.client.request("POST", f"/api/runs/{child}/attempts", {"stage": "publish"})
        self.assertEqual(status, 202, body)


if __name__ == "__main__":
    unittest.main()
