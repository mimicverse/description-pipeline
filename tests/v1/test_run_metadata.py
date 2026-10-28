"""Focused backend tests for shared run rename and reversible soft deletion."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from wsgiref.simple_server import make_server

from tests.v1.test_portal import (
    ADMIN_TOKEN,
    AIRFLOW_TOKEN,
    FEISHU_NAME,
    MockAirflow,
    PortalClient,
    TEST_SUMMARY,
    TEST_TARGET,
    TEST_TARGETS,
    VIEWER_TOKEN,
    _actor_envelope,
    _QuietHandler,
    _ThreadingWSGIServer,
)

from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.orchestration.portal import AirflowApi, PortalApp, PortalConfig
from description_pipeline.platform.facts import (
    FactStoreError,
    RunDisplayMetadata,
    RunNotAdmittedError,
)

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


class _RunWorld:
    def __init__(self) -> None:
        self.run_display_rows: dict[str, list] = {}
        self.run_states: dict[str, str] = {}


class FakeRunControlStore:
    """Canonical run-control surface backed by the fixture's display rows."""

    def __init__(self, rows: dict[str, list]) -> None:
        self.rows = rows

    def admission(self):
        return SimpleNamespace(state="open")

    def _display(self, run_id: str) -> RunDisplayMetadata:
        row = self.rows.get(run_id)
        if row is None:
            raise RunNotAdmittedError("The run is not admitted")
        return RunDisplayMetadata(run_id, *row)

    def list_run_display_metadata(self) -> dict[str, RunDisplayMetadata]:
        return {
            run_id: RunDisplayMetadata(run_id, *row)
            for run_id, row in self.rows.items()
            if row[0] is not None or row[1] is not None
        }

    def get_run_display_metadata(self, run_id: str) -> RunDisplayMetadata | None:
        row = self.rows.get(run_id)
        return RunDisplayMetadata(run_id, *row) if row is not None else None

    def rename_run(self, run_id: str, display_name: str | None) -> RunDisplayMetadata:
        row = self.rows.get(run_id)
        if row is None:
            raise RunNotAdmittedError("The run is not admitted")
        row[0] = display_name
        row[3] = NOW
        return RunDisplayMetadata(run_id, *row)

    def hide_run(self, run_id: str, *, actor: str) -> RunDisplayMetadata:
        row = self.rows.get(run_id)
        if row is None:
            raise RunNotAdmittedError("The run is not admitted")
        row[1] = row[1] or NOW
        row[2] = row[2] or actor
        row[3] = NOW
        return RunDisplayMetadata(run_id, *row)

    def restore_run(self, run_id: str) -> RunDisplayMetadata:
        row = self.rows.get(run_id)
        if row is None:
            raise RunNotAdmittedError("The run is not admitted")
        row[1] = None
        row[2] = None
        row[3] = NOW
        return RunDisplayMetadata(run_id, *row)


class UnavailableRunControlStore:
    def admission(self):
        raise FactStoreError("canonical storage unavailable")

    def list_run_display_metadata(self):
        raise FactStoreError("canonical storage unavailable")

    def get_run_display_metadata(self, run_id: str):
        raise FactStoreError("canonical storage unavailable")

    def rename_run(self, run_id: str, display_name: str | None):
        raise FactStoreError("canonical storage unavailable")

    def hide_run(self, run_id: str, *, actor: str):
        raise FactStoreError("canonical storage unavailable")

    def restore_run(self, run_id: str):
        raise FactStoreError("canonical storage unavailable")


class RunMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.airflow = MockAirflow()
        self.airflow.__enter__()
        self.addCleanup(self.airflow.__exit__, None, None, None)
        self.world = _RunWorld()
        self.run_control = FakeRunControlStore(self.world.run_display_rows)
        self.projection_metadata = {}
        self.upload_root = Path(tempfile.mkdtemp(prefix="portal-upload-"))
        self.addCleanup(shutil.rmtree, self.upload_root, ignore_errors=True)
        self.static_dir = Path(__file__).resolve().parents[2] / "src/description_pipeline/orchestration/static"
        self.server = self._serve()
        self.client = PortalClient(f"http://127.0.0.1:{self.server.server_address[1]}")
        self.client.login()

    # ------------------------------------------------------------------ helpers
    def _serve(self, *, store=None):
        config = PortalConfig(
            airflow=AirflowApi(self.airflow.url),
            static_dir=self.static_dir,
            upload_root=self.upload_root,
            run_control_store=store if store is not None else self.run_control,
            run_projection_factory=self._projection,
            pipeline_targets=TEST_TARGETS,
            config_identity_provider=lambda: "c" * 64,
        )
        server = make_server(
            "127.0.0.1", 0, PortalApp(config), server_class=_ThreadingWSGIServer, handler_class=_QuietHandler
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(self._stop, server)
        return server

    def _projection(self, run_id: str) -> dict | None:
        dag_run_id = next(
            (
                dag_id
                for dag_id in self.airflow.dag_runs
                if native_run_id(dag_id) == run_id
            ),
            None,
        )
        if dag_run_id is None:
            return None
        metadata = self.projection_metadata.get(run_id, {})
        state = self.world.run_states.get(run_id, "succeeded")
        stages = [
            {
                "stage": stage,
                "state": "waiting",
                "decision": None,
                "receipt": {"artifacts": []},
            }
            for stage in ("freeze", "discover", "capture", "generate", "verify", "publish")
        ]
        return {
            "run_id": run_id,
            "request_id": dag_run_id,
            "parent_run_id": metadata.get("parent_run_id"),
            "from_stage": metadata.get("from_stage"),
            "request": metadata.get("request", {}),
            "purpose": "engineering",
            "scope": {
                "kind": "formal",
                "target_stage": None,
                "formal_eligible": state == "succeeded",
            },
            "formal_eligible": state == "succeeded",
            "state": state,
            "events": [],
            "stages": stages,
            "by_stage": {stage: {} for stage in ("freeze", "discover", "capture", "generate", "verify", "publish")},
            "members": {},
            "technical": {"commands": [], "claims": [], "effects": []},
        }

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
        self.world.run_display_rows[native_run_id(dag_run_id)] = [None, None, None, NOW]
        native_id = native_run_id(dag_run_id)
        self.world.run_states[native_id] = {
            "success": "succeeded",
            "failed": "failed",
            "running": "running",
            "unresolved": "unresolved",
        }.get(state, state)
        conf = conf or {}
        parent = conf.get("parent_dag_run_id")
        self.projection_metadata[native_id] = {
            "parent_run_id": native_run_id(parent) if isinstance(parent, str) and parent else None,
            "from_stage": conf.get("resume_from"),
            "request": {
                "change_summary": conf.get("change_summary"),
                "main_assembly": conf.get("main_assembly"),
                "purpose": conf.get("purpose"),
                "target_hardware": conf.get("target_hardware"),
            },
        }
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
        self.assertEqual(status, 400, body)
        self.assertIn("start from Freeze", json.loads(body)["error"])

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
        # Canonical run facts still report active work.
        active_fact = self._seed(self._uuid())
        self.world.run_states[native_run_id(active_fact)] = "running"
        status, _, body = self.client.request("DELETE", f"/api/runs/{active_fact}")
        self.assertEqual(json.loads(body)["reason"], "run_active")
        unresolved = self._seed(self._uuid())
        self.world.run_states[native_run_id(unresolved)] = "unresolved"
        status, _, body = self.client.request("DELETE", f"/api/runs/{unresolved}")
        self.assertEqual(json.loads(body)["reason"], "reconciliation_required")
        # An authoritative 404 (no native job ever existed) allows hiding a terminal record.
        no_job = self._seed(self._uuid(), state="failed", tasks=("failed",))
        status, _, body = self.client.request("DELETE", f"/api/runs/{no_job}")
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["deleted"])
        # An unrecognized canonical state fails closed.
        unknown = self._seed(self._uuid())
        self.world.run_states[native_run_id(unknown)] = "mystery"
        status, _, body = self.client.request("DELETE", f"/api/runs/{unknown}")
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

    def test_an_unavailable_platform_fails_the_run_surfaces_closed(self) -> None:
        run_id = self._seed(self._uuid())
        restarted = self._serve(store=UnavailableRunControlStore())
        client = PortalClient(f"http://127.0.0.1:{restarted.server_address[1]}")
        client.login()
        status, _, body = client.request("GET", "/api/runs")
        self.assertEqual(status, 503, body)
        status, _, body = client.request("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 503, body)
        status, _, body = client.request("PATCH", f"/api/runs/{run_id}", {"title": "x"})
        self.assertEqual(status, 503, body)
        status, _, body = client.request("DELETE", f"/api/runs/{run_id}")
        self.assertEqual(status, 503, body)

    def test_runs_without_a_platform_row_refuse_display_writes(self) -> None:
        run_id = self._seed(self._uuid())
        self.world.run_display_rows.pop(native_run_id(run_id))
        for method, path, payload in (
            ("PATCH", f"/api/runs/{run_id}", {"title": "x"}),
            ("DELETE", f"/api/runs/{run_id}", None),
            ("POST", f"/api/runs/{run_id}/restore", None),
        ):
            with self.subTest(case=method):
                status, _, body = self.client.request(method, path, payload)
                self.assertEqual(status, 404, body)
                self.assertIn("platform registration", json.loads(body)["error"])
        status, _, body = self.client.request("GET", f"/api/runs/{run_id}")
        self.assertEqual(status, 200, body)
        self.assertFalse(json.loads(body)["deleted"])

    def test_unknown_native_status_refuses_and_list_failure_is_503(self) -> None:
        run_id = self._seed(self._uuid())
        from unittest import mock

        from description_pipeline.orchestration.portal import AirflowApi as PortalAirflowApi
        from description_pipeline.orchestration.portal import AirflowApiError

        # A canonical state that is neither active nor terminal cannot be hidden.
        self.world.run_states[native_run_id(run_id)] = "mystery"
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
                "target_hardware": TEST_TARGET,
                "change_summary": TEST_SUMMARY,
                "purpose": "engineering",
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
        self.assertTrue(detail["resume_from_name"])
        status, payload = self._list(self.client)
        row = next(item for item in payload["runs"] if item["dag_run_id"] == child)
        self.assertEqual(row["parent_dag_run_id"], parent)
        self.assertFalse(row["deleted"])
        # Old arbitrary-stage resume is rejected for the parent...
        status, _, body = self.client.request("POST", f"/api/runs/{parent}/attempts", {"stage": "verify"})
        self.assertEqual(status, 400, body)
        # ...and so is every downstream attempt from the child.
        status, _, body = self.client.request("POST", f"/api/runs/{child}/attempts", {"stage": "publish"})
        self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()
