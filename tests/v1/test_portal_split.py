"""Portal split wiring: Linux-owned attempts stay readable when Windows is down."""

from __future__ import annotations

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


class _DownEndpoint:
    """A Windows endpoint that is not reachable at all."""

    def get_job(self, run_id):
        raise EndpointError("windows down")

    def rerun_plan(self, run_id):
        raise EndpointError("windows down")


class _ExpiredAirflow:
    def dag_run(self, token, dag_id, dag_run_id):
        raise portal_module.AirflowAuthError("expired")


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
        self.app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_ExpiredAirflow(),
                endpoint=lambda: _DownEndpoint(),
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
