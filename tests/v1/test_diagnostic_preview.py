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
import json
import tempfile
import unittest
import shutil
from pathlib import Path

from description_pipeline.delivery import read_delivery_manifest, write_delivery_manifest
from description_pipeline.io import write_json
from description_pipeline.orchestration import portal as portal_module
from description_pipeline.orchestration.airflow_client import EndpointError, native_run_id
from tests.v1.test_platform_store import build_sealed_delivery

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
PREVIEW_LIMIT_WARNING = {
    "code": "limits.preview_policy",
    "object": "joint:r_elbow",
    "blocking": False,
    "message": "inspection preview uses a versioned full-turn rotation window",
    "detail": {
        "objects": [
            {
                "object": "joint:r_elbow",
                "object_type": "joint",
                "field": "limits",
                "source": "preview_policy",
            }
        ]
    },
}
ENGINEERING_DEFAULT_POLICY_WARNING = {
    "code": "limits.default_policy",
    "object": "joint:r_elbow",
    "blocking": False,
    "message": "engineering default rotation window; engineering position range remains unqualified",
    "detail": {
        "objects": [
            {
                "object": "joint:r_elbow",
                "object_type": "joint",
                "field": "limits",
                "source": "engineering default rotation window policy",
                "rule_id": "engineering_rotation_default_window/v1",
                "engineering_position_range_qualified": False,
            }
        ]
    },
}
BLOCKING_QUALITY_DIAGNOSTIC = {
    "code": "quality.required_failure",
    "object": "joint:r_elbow",
    "blocking": True,
    "message": "a blocking quality diagnostic is never a warning",
    "detail": {"field": "limits"},
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

    def _registered_failed_verify(
        self,
        *,
        passed: bool = False,
        diagnostics: list[dict] | None = None,
        state: str = "failed",
    ) -> RegisteredPlatform:
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
                "passed": passed,
                "subject_sha256": hashlib.sha256(URDF).hexdigest(),
                "checks": FAILING_CHECKS,
                "diagnostics": diagnostics or [],
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
            state=state,
            receipt={
                "subject_sha256": hashlib.sha256(URDF).hexdigest(),
                "artifacts": members,
                "error": "URDF verification failed; see reports/quality.json",
            },
        )
        return platform

    # ------------------------------------------------------------------ portal
    def test_verified_preview_includes_registered_quality_warnings(self) -> None:
        platform = self._registered_failed_verify(
            passed=True,
            state="completed",
            diagnostics=[PREVIEW_LIMIT_WARNING],
        )
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: _DownEndpoint(),
                platform_store=platform.store,
            )
        )
        session = app.sessions.create("user", "token")
        preview = app._preview_payload(session, DAG_RUN_ID)

        self.assertIs(preview["verified"], True)
        self.assertEqual(preview["warnings_total"], 1)
        self.assertEqual(preview["warnings"][0]["source"], "quality")
        self.assertEqual(preview["warnings"][0]["code"], "limits.preview_policy")
        self.assertEqual(preview["warnings"][0]["object"], "joint:r_elbow")
        self.assertEqual(preview["warnings"][0]["detail"], PREVIEW_LIMIT_WARNING["detail"])

    def test_verified_preview_includes_engineering_default_policy_warning(self) -> None:
        platform = self._registered_failed_verify(
            passed=True,
            state="completed",
            diagnostics=[ENGINEERING_DEFAULT_POLICY_WARNING],
        )
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: _DownEndpoint(),
                platform_store=platform.store,
            )
        )
        session = app.sessions.create("user", "token")
        preview = app._preview_payload(session, DAG_RUN_ID)

        self.assertIs(preview["verified"], True)
        self.assertEqual(preview["warnings_total"], 1)
        self.assertEqual(preview["warnings"][0]["code"], "limits.default_policy")
        self.assertEqual(preview["warnings"][0]["detail"], ENGINEERING_DEFAULT_POLICY_WARNING["detail"])

    def test_failed_preview_merges_and_deduplicates_quality_warnings(self) -> None:
        duplicate = {
            "code": INPUT_WARNINGS["warnings"][0]["code"],
            "blocking": False,
            "message": INPUT_WARNINGS["warnings"][0]["message"],
            "detail": INPUT_WARNINGS["warnings"][0]["detail"],
        }
        platform = self._registered_failed_verify(
            diagnostics=[PREVIEW_LIMIT_WARNING, duplicate, BLOCKING_QUALITY_DIAGNOSTIC]
        )
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: _DownEndpoint(),
                platform_store=platform.store,
            )
        )
        session = app.sessions.create("user", "token")
        preview = app._preview_payload(session, DAG_RUN_ID)

        self.assertEqual(
            [row["code"] for row in preview["warnings"]],
            [
                "discovery.exporter_membership_uncovered",
                "input.coordinate_systems_unverified",
                "limits.preview_policy",
            ],
        )
        self.assertEqual(preview["warnings_total"], 3)
        self.assertEqual(
            sum(row["code"] == "input.coordinate_systems_unverified" for row in preview["warnings"]),
            1,
        )
        self.assertNotIn(BLOCKING_QUALITY_DIAGNOSTIC["code"], {row["code"] for row in preview["warnings"]})

    def test_blocking_quality_diagnostics_are_not_preview_warnings(self) -> None:
        platform = self._registered_failed_verify(
            passed=True,
            state="completed",
            diagnostics=[BLOCKING_QUALITY_DIAGNOSTIC],
        )
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: _DownEndpoint(),
                platform_store=platform.store,
            )
        )
        session = app.sessions.create("user", "token")
        preview = app._preview_payload(session, DAG_RUN_ID)

        self.assertEqual(preview["warnings"], [])
        self.assertEqual(preview["warnings_total"], 0)

    def test_quality_diagnostic_without_explicit_nonblocking_flag_is_not_a_warning(self) -> None:
        platform = self._registered_failed_verify(
            passed=True,
            state="completed",
            diagnostics=[{"code": "quality.unknown_blocking", "message": "flag is missing"}],
        )
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: _DownEndpoint(),
                platform_store=platform.store,
            )
        )
        session = app.sessions.create("user", "token")
        preview = app._preview_payload(session, DAG_RUN_ID)

        self.assertEqual(preview["warnings"], [])
        self.assertEqual(preview["warnings_total"], 0)

    def test_preview_limits_warnings_but_reports_the_full_total(self) -> None:
        diagnostics = [
            {
                "code": f"quality.warning_{index:02d}",
                "blocking": False,
                "message": f"warning {index}",
            }
            for index in range(portal_module._PREVIEW_LIMIT + 1)
        ]
        platform = self._registered_failed_verify(
            passed=True,
            state="completed",
            diagnostics=diagnostics,
        )
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: _DownEndpoint(),
                platform_store=platform.store,
            )
        )
        session = app.sessions.create("user", "token")
        preview = app._preview_payload(session, DAG_RUN_ID)

        self.assertEqual(len(preview["warnings"]), portal_module._PREVIEW_LIMIT)
        self.assertEqual(preview["warnings_total"], portal_module._PREVIEW_LIMIT + 1)
        self.assertEqual(preview["warnings"][0]["code"], "quality.warning_00")
        self.assertEqual(
            preview["warnings"][-1]["code"],
            f"quality.warning_{portal_module._PREVIEW_LIMIT - 1:02d}",
        )

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
                ("discovery", "discovery.exporter_membership_uncovered"),
                ("input", "input.coordinate_systems_unverified"),
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
        """A reachable linked failure: the child really registers its reuse of the
        parent's completed capture and really registers its own failed verify; the
        diagnostic is then served from the child's registered tree with the
        parent's sealed transfer behind it."""
        platform = RegisteredPlatform(self.tmp / "linked-platform")
        platform.admit(RUN)
        platform.register_capture(RUN)
        child_dag = "portal-20261010T000001-abcdef02"
        child = native_run_id(child_dag)
        platform.admit(child, from_stage="verify", parent_run_id=RUN)
        parent_row = platform.store.stage_records(RUN)["capture"]
        # The real reuse registration: the writer itself validates the completed
        # source row and re-verifies its sealed receipt.
        platform.store.confirm_stage(
            run_id=child,
            stage="capture",
            state="completed",
            receipt=parent_row["receipt"],
            checkpoint_uri=parent_row["checkpoint_uri"],
            reused_from_run=RUN,
        )
        self._register_child_failed_verify(platform, child)
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

        child_urdf = (platform.sealed(f"store://sealed/{child}/verify") / "urdf/robot.urdf").read_bytes()
        body = b"".join(app._artifact(environ, start_response, child_dag, "urdf/robot.urdf"))
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(body, child_urdf)

    def _register_child_failed_verify(self, platform: RegisteredPlatform, child: str) -> None:
        """The child's own executed-and-failed verify stage, registered through the writer."""
        tree = platform.sealed(f"store://sealed/{child}/verify")
        tree.mkdir(parents=True)
        build_sealed_delivery(tree, run_id=child)
        manifest = read_delivery_manifest(tree)
        write_json(
            tree / "reports/quality.json",
            {
                "passed": False,
                "subject_sha256": hashlib.sha256(URDF).hexdigest(),
                "checks": FAILING_CHECKS,
            },
        )
        handoff = json.loads((tree / "input/handoff.json").read_text(encoding="utf-8"))
        handoff["warnings"] = INPUT_WARNINGS["warnings"]
        write_json(tree / "input/handoff.json", handoff)
        discovery = json.loads((tree / "evidence/discovery.json").read_text(encoding="utf-8"))
        discovery["findings"] = DISCOVERY_FINDINGS["findings"]
        write_json(tree / "evidence/discovery.json", discovery)
        write_delivery_manifest(tree, contract=manifest["contract"], policy=manifest["policy"])
        manifest = read_delivery_manifest(tree)
        report = (tree / "reports/quality.json").read_bytes()
        artifacts = [dict(record) for record in manifest["subject"]["files"]] + [
            {
                "path": "reports/quality.json",
                "bytes": len(report),
                "sha256": hashlib.sha256(report).hexdigest(),
            }
        ]
        platform.store.confirm_stage(
            run_id=child,
            stage="verify",
            state="failed",
            receipt={
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "pipeline_id": "solidworks-to-urdf",
                "run_id": child,
                "stage": "verify",
                "state": "failed",
                "passed": False,
                "error": "URDF verification failed; see reports/quality.json",
                "subject_sha256": manifest["subject"]["digest"],
                "artifacts": artifacts,
            },
            checkpoint_uri=f"store://sealed/{child}/verify",
        )

    def test_a_success_labelled_failed_report_is_refused(self) -> None:
        """A failed-verify registration whose report claims success contradicts the
        registered outcome: the diagnostic is refused while the failed products
        stay registered and downloadable."""
        platform = self._registered_failed_verify(passed=True)
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: _DownEndpoint(),
                platform_store=platform.store,
            )
        )
        session = app.sessions.create("user", "token")
        with self.assertRaises(portal_module.PortalError) as caught:
            app._preview_payload(session, DAG_RUN_ID)
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("conflicts", caught.exception.message)
        # The failed member stays byte-verifiable and downloadable.
        environ = {"HTTP_COOKIE": f"{portal_module.DEFAULT_SESSION_COOKIE}={session.session_id}"}
        captured: dict = {}

        def start_response(status, headers):
            captured["status"] = status

        body = b"".join(app._artifact(environ, start_response, DAG_RUN_ID, "reports/quality.json"))
        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(
            json.loads(body.decode("utf-8"))["passed"],
            True,
        )

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
        self.assertIn('<div id="preview-diagnostic"', html)
        self.assertIn("renderPreviewDiagnostic(preview)", app)
        self.assertIn("previewDiagnosticState(preview)", app)
        self.assertIn("Source warnings", app)
        self.assertIn("appendWarningSourceRows", app)
        self.assertIn("replaceChildren()", app)
        self.assertIn("must not be published", app)
        self.assertNotIn("innerHTML", app)


if __name__ == "__main__":
    unittest.main()
