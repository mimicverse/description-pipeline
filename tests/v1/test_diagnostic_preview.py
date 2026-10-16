"""Retained diagnostic preview after a failed verification (read-only until served).

These tests pin the warning/blocking boundary of the retained preview over the
registered platform projection:

* a registered failed verify stage keeps its sealed delivery, and the single
  preview interface serves it only as an explicitly unverified diagnostic;
* the diagnostic payload binds the recorded subject and carries the exact
  failing checks plus the warning findings the registered members already hold;
* anything the registration does not carry is never served and never invented;
* the preview UI labels the diagnostic and forbids publication.

The store-level binding tests of the retired Linux store moved with its
product surface: the registration oracle owns sealed-byte binding
(``test_platform_store``, ``test_airflow_dag``) and the portal projection owns
the served refusals (``test_portal``, ``test_registered_delivery``).
Nothing here publishes or deploys: the diagnostic preview never authorizes publication.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import shutil
from pathlib import Path

from description_pipeline.io import write_json
from description_pipeline.orchestration import portal as portal_module
from description_pipeline.orchestration.airflow_client import EndpointError, native_run_id

from .portal_read_support import RegisteredPlatform

DAG_RUN_ID = "portal-20261010T000000-abcdef01"
RUN = native_run_id(DAG_RUN_ID)
URDF = b'<robot name="probe"><link name="base_link"/></robot>\n'
FAILING_CHECKS = [
    {
        "id": "joints.shoulder_pitch_joint",
        "state": "failed",
        "passed": False,
        "details": {"error": "Invalid joint limits"},
    },
    {
        "id": "physics.mass_closure_equality",
        "state": "failed",
        "passed": False,
        "details": {"reason": "URDF mass sum differs from the whole-CAD reading"},
    },
    {"id": "urdf.topology", "state": "passed", "passed": True, "details": {}},
]
DISCOVERY_FINDINGS = {
    "schema_version": "solidworks-to-urdf.native-findings/v1",
    "findings": [
        {
            "code": "discovery.exporter_membership_uncovered",
            "object": "assembly",
            "message": "active material occurrences are not covered by exactly one declared selection",
            "detail": {"components": ["headM3.0-1/垫片2-1"]},
            "blocking": False,
        },
        {
            "code": "discovery.internal_error",
            "object": "assembly",
            "message": "a blocking finding is never a warning",
            "blocking": True,
        },
    ],
}
INPUT_WARNINGS = {
    "passed": True,
    "warnings": [
        {
            "code": "input.coordinate_systems_unverified",
            "message": "datum existence is verified against the CAD assembly during the native capture",
            "detail": {"coordinate_systems": ["c_base_link"]},
        }
    ],
}


class _PermissiveAirflow:
    """Only the portal's per-request authorization call; a real tenant is not required."""

    def dag_run(self, token, dag_id, dag_run_id):
        return {"dag_run_id": dag_run_id, "state": "failed"}


class _DownEndpoint:
    def get_job(self, run_id):
        raise EndpointError("windows down")


class DiagnosticPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="diagnostic-preview-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _registered_failed_verify(self) -> RegisteredPlatform:
        """The registered state a failing verify stage leaves: sealed members, failed receipt."""
        platform = RegisteredPlatform(self.tmp / "platform")
        platform.admit(RUN)
        tree = platform.tree(RUN, "verify")
        (tree / "urdf").mkdir()
        (tree / "urdf/robot.urdf").write_bytes(URDF)
        (tree / "reports").mkdir()
        write_json(
            tree / "reports/quality.json",
            {
                "passed": False,
                "subject_sha256": hashlib.sha256(URDF).hexdigest(),
                "checks": FAILING_CHECKS,
            },
        )
        (tree / "input").mkdir()
        write_json(tree / "input/handoff.json", {"passed": True, "warnings": INPUT_WARNINGS["warnings"]})
        (tree / "evidence").mkdir()
        write_json(tree / "evidence/discovery.json", DISCOVERY_FINDINGS)
        members = []
        for path in ("urdf/robot.urdf", "reports/quality.json", "input/handoff.json", "evidence/discovery.json"):
            data = (tree / path).read_bytes()
            members.append({"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        platform.stage(
            RUN,
            "verify",
            state="failed",
            receipt={
                "subject_sha256": hashlib.sha256(URDF).hexdigest(),
                "artifacts": members,
                "error": "URDF verification failed; see reports/quality.json",
            },
        )
        return platform

    # ------------------------------------------------------------------ portal
    def test_portal_serves_the_retained_diagnostic_through_the_single_interface(self) -> None:
        platform = self._registered_failed_verify()
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: _DownEndpoint(),
                platform_store=platform.store,
            )
        )
        session = app.sessions.create("user", "token")
        preview = app._preview_payload(session, DAG_RUN_ID)
        self.assertIs(preview["verified"], False)
        self.assertEqual(preview["state"], "unverified")
        self.assertEqual(preview["reason"], "verification_failed")
        self.assertEqual(preview["subject_sha256"], hashlib.sha256(URDF).hexdigest())
        self.assertEqual(preview["urdf"], "urdf/robot.urdf")
        self.assertIn("urdf/robot.urdf", preview["files"])
        self.assertEqual(
            [row["id"] for row in preview["failing_checks"]],
            ["joints.shoulder_pitch_joint", "physics.mass_closure_equality"],
        )
        self.assertEqual(preview["failing_checks"][0]["message"], "Invalid joint limits")
        self.assertEqual(preview["failing_checks_total"], 2)
        self.assertEqual(
            [(row["source"], row["code"]) for row in preview["warnings"]],
            [
                ("input", "input.coordinate_systems_unverified"),
                ("discovery", "discovery.exporter_membership_uncovered"),
            ],
        )
        self.assertEqual(preview["warnings_total"], 2)
        # The artifact route re-authorizes first and then streams the bound bytes.
        environ = {"HTTP_COOKIE": f"{portal_module.DEFAULT_SESSION_COOKIE}={session.session_id}"}
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        body = b"".join(app._artifact(environ, start_response, DAG_RUN_ID, "urdf/robot.urdf"))
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(body, URDF)
        with self.assertRaises(portal_module.PortalError):
            app._artifact(environ, start_response, DAG_RUN_ID, "reports/stages.json")

    def test_a_linked_child_serves_its_diagnostic_with_the_parent_transfer(self) -> None:
        """The child's reused verify registration serves the parent's sealed
        transfer: the diagnostic comes from the registered source tree, never a
        re-materialized copy or a second store."""
        platform = self._registered_failed_verify()
        child_dag = "portal-20261010T000001-abcdef02"
        child = native_run_id(child_dag)
        platform.admit(child, from_stage="publish", parent_run_id=RUN)
        parent_row = platform.store.stage_records(RUN)["verify"]
        platform.stage(
            child,
            "verify",
            state="failed",
            receipt=parent_row["receipt"],
            checkpoint_uri=parent_row["checkpoint_uri"],
            reused_from_run=RUN,
        )
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: _DownEndpoint(),
                platform_store=platform.store,
            )
        )
        session = app.sessions.create("user", "token")
        preview = app._preview_payload(session, child_dag)
        self.assertIs(preview["verified"], False)
        self.assertEqual(preview["run_id"], child)
        self.assertEqual(
            [row["id"] for row in preview["failing_checks"]],
            ["joints.shoulder_pitch_joint", "physics.mass_closure_equality"],
        )
        environ = {"HTTP_COOKIE": f"{portal_module.DEFAULT_SESSION_COOKIE}={session.session_id}"}
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = status

        body = b"".join(app._artifact(environ, start_response, child_dag, "urdf/robot.urdf"))
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(body, URDF)

    def test_failing_checks_accept_the_state_failed_convention(self) -> None:
        from description_pipeline.orchestration.portal import _failing_checks

        rows = _failing_checks(
            {
                "checks": [
                    # canonical Gate.run rows carry both fields
                    {"id": "canonical.limit", "state": "failed", "passed": False, "details": {"reason": "limit"}},
                    {"id": "ok", "state": "passed", "passed": True, "details": {}},
                    # the missing-field producer anomaly stays covered by the same convention
                    {"id": "limit.dof", "state": "failed", "details": {"reason": "8 control limits"}},
                    {"id": "other", "passed": False, "details": {"error": "boom"}},
                ]
            }
        )
        self.assertEqual([row["id"] for row in rows], ["canonical.limit", "limit.dof", "other"])
        self.assertEqual(rows[0]["state"], "failed")

    # ---------------------------------------------------------------------- ui
    def test_preview_ui_labels_the_diagnostic_and_forbids_publication(self) -> None:
        static = Path(__file__).resolve().parents[2] / "src/description_pipeline/orchestration/static"
        html = (static / "index.html").read_text(encoding="utf-8")
        app = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="preview-diagnostic"', html)
        self.assertIn("renderPreviewDiagnostic(preview)", app)
        self.assertIn("preview.verified !== false", app)
        self.assertIn("禁止发布", app)


if __name__ == "__main__":
    unittest.main()
