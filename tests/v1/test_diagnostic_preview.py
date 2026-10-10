"""Retained diagnostic preview after a failed verification (read-only until served).

These tests pin the warning/blocking boundary of the retained preview:

* a failed verify stage keeps its generated delivery, and the existing single preview
  interface serves it only as an explicitly unverified diagnostic;
* the diagnostic payload binds the recorded subject to the retained bytes and carries
  the exact failing checks plus the warning findings the delivery already holds;
* tampering with members, the quality report or the recorded location denies preview;
* a qualified delivery keeps the verified preview exactly as before.

Nothing here publishes or deploys: the diagnostic preview never authorizes publication.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.delivery import subject_digest
from description_pipeline.io import PipelineError, inventory, write_json
from description_pipeline.orchestration import portal as portal_module
from description_pipeline.orchestration import linux_store as store_module
from description_pipeline.orchestration.airflow_client import EndpointError, native_run_id
from description_pipeline.orchestration.linux_store import LinuxStore
from description_pipeline.orchestration.stage_transfer import seal_capture

from .test_stage_transfer import HANDOFF, MAIN_ASSEMBLY, capture_root, native_tool

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
        self.store = LinuxStore(self.tmp / "store")
        self.tool = native_tool()
        source = capture_root(self.tmp, run_id=RUN)
        archive = self.tmp / "native-evidence.zip"
        seal_capture(
            source,
            archive,
            run_id=RUN,
            handoff_sha256=HANDOFF,
            main_assembly=MAIN_ASSEMBLY,
            native_tool=self.tool,
        )
        self.store.import_capture(
            RUN,
            archive,
            expected_handoff_sha256=HANDOFF,
            expected_main_assembly=MAIN_ASSEMBLY,
            expected_native_tool=self.tool,
        )

    # ------------------------------------------------------------------ fixture
    def _retained_failed_verify(self, *, passed: bool = False, stamp: bool = True) -> Path:
        """Build the state a failing verify stage leaves behind: bytes, report, receipt."""

        failed = self.store.stage_dir(RUN, "verify").with_name("verify.failed")
        shutil.copytree(self.store.capture_dir(RUN), failed)
        for name in ("model", "meshes", "urdf"):
            (failed / name).mkdir(parents=True, exist_ok=True)
        (failed / "model/robot.json").write_text("{}\n", encoding="utf-8")
        (failed / "urdf/robot.urdf").write_bytes(URDF)
        (failed / "meshes/base_link.stl").write_text("solid base_link\nendsolid base_link\n", encoding="utf-8")
        (failed / "README.md").write_text("diagnostic delivery\n", encoding="utf-8")
        write_json(failed / "reports/input.json", INPUT_WARNINGS)
        write_json(
            failed / "reports/tool.json",
            {"pipeline_id": "solidworks-to-urdf", "schema_version": "solidworks-to-urdf.tool/v1"},
        )
        write_json(failed / "input/discovery/findings.json", DISCOVERY_FINDINGS)
        subject = subject_digest(failed)
        report = {"passed": passed, "subject_sha256": subject, "checks": FAILING_CHECKS}
        write_json(failed / "reports/quality.json", report)
        if stamp:
            write_json(
                failed / "reports/run.json",
                {
                    "schema_version": "solidworks-to-urdf.bundle/v1",
                    "run_id": RUN,
                    "state": "failed",
                    "passed": False,
                    "stage": "verify",
                    "subject_sha256": subject,
                    "owned_files": inventory(failed, exclude=("reports/run.json",)),
                },
            )
        self.store.record_stage(
            RUN,
            "verify",
            {
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "run_id": RUN,
                "state": "failed",
                "passed": False,
                "stage": "verify",
                "subject_sha256": subject,
                "error": "URDF verification failed; see reports/quality.json",
                "quality": report,
                "diagnostic_path": str(failed),
                "events": [],
            },
        )
        return failed

    # ------------------------------------------------------------- store payload
    def test_failed_verify_serves_an_explicitly_unverified_diagnostic(self) -> None:
        failed = self._retained_failed_verify()
        subject = subject_digest(failed)
        preview = self.store.preview(RUN)
        self.assertIs(preview["verified"], False)
        self.assertEqual(preview["state"], "unverified")
        self.assertEqual(preview["reason"], "verification_failed")
        self.assertEqual(preview["subject_sha256"], subject)
        self.assertEqual(preview["urdf"], "urdf/robot.urdf")
        self.assertIn("urdf/robot.urdf", preview["files"])
        self.assertEqual(
            [row["id"] for row in preview["failing_checks"]],
            ["joints.shoulder_pitch_joint", "physics.mass_closure_equality"],
        )
        self.assertEqual(preview["failing_checks"][0]["message"], "Invalid joint limits")
        self.assertEqual(preview["failing_checks_total"], 2)
        codes = [(row["source"], row["code"]) for row in preview["warnings"]]
        self.assertEqual(
            codes,
            [
                ("input", "input.coordinate_systems_unverified"),
                ("discovery", "discovery.exporter_membership_uncovered"),
            ],
        )
        self.assertEqual(preview["warnings_total"], 2)
        # Binding is once per immutable checkpoint: the same object serves the next call.
        self.assertIs(self.store.preview(RUN), preview)

    def test_diagnostic_artifacts_stream_only_bound_bytes(self) -> None:
        failed = self._retained_failed_verify()
        preview = self.store.preview(RUN)
        digest = preview["files"]["urdf/robot.urdf"]
        stream, size = self.store.open_artifact(RUN, "urdf/robot.urdf", sha256=digest)
        with stream:
            self.assertEqual(stream.read(), URDF)
            self.assertEqual(stream.read(), b"")
        self.assertEqual(size, len(URDF))
        with self.assertRaises(PipelineError):
            self.store.open_artifact(RUN, "urdf/robot.urdf", sha256="0" * 64)
        with self.assertRaises(PipelineError):
            self.store.open_artifact(RUN, "../reports/run.json", sha256=digest)
        # A member edit after the binding is refused on the serving stream itself.
        (failed / "urdf/robot.urdf").write_bytes(b'<robot name="tampered"/>\n')
        tampered, _size = self.store.open_artifact(RUN, "urdf/robot.urdf", sha256=digest)
        with tampered, self.assertRaises(PipelineError):
            while tampered.read(65536):
                pass

    # ---------------------------------------------------------------- refusals
    def test_member_tamper_before_preview_denies_the_diagnostic(self) -> None:
        failed = self._retained_failed_verify()
        (failed / "urdf/robot.urdf").write_bytes(b'<robot name="tampered"/>\n')
        with self.assertRaises(PipelineError) as caught:
            self.store.preview(RUN)
        self.assertIn("stamped pipeline output", str(caught.exception))

    def test_forged_quality_report_denies_the_diagnostic(self) -> None:
        self._retained_failed_verify(passed=True)
        with self.assertRaises(PipelineError) as caught:
            self.store.preview(RUN)
        self.assertIn("failed report", str(caught.exception))

    def test_missing_stamp_or_foreign_location_denies_the_diagnostic(self) -> None:
        self._retained_failed_verify(stamp=False)
        with self.assertRaises(PipelineError) as caught:
            self.store.preview(RUN)
        self.assertIn("stamped pipeline output", str(caught.exception))
        # A receipt that points outside the attempt is refused, never followed.
        shutil.rmtree(self.store.run_dir(RUN) / "stages/verify.failed")
        outside = self.tmp / "outside"
        outside.mkdir()
        receipt = self.store.receipt_path(RUN, "verify")
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["diagnostic_path"] = str(outside)
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(PipelineError) as caught:
            self.store.preview(RUN)
        self.assertIn("outside this attempt", str(caught.exception))

    def test_generate_only_checkpoint_still_waits_for_verification(self) -> None:
        generate = self.store.stage_dir(RUN, "generate")
        (generate / "input").mkdir(parents=True)
        (generate / "reports").mkdir(parents=True)
        (generate / "urdf").mkdir()
        (generate / "urdf/robot.urdf").write_bytes(URDF)
        with self.assertRaises(PipelineError):
            self.store.preview(RUN)

    # ---------------------------------------------------------------- verified
    def test_a_qualified_delivery_keeps_the_verified_preview(self) -> None:
        failed = self._retained_failed_verify()
        verify = self.store.stage_dir(RUN, "verify")
        shutil.copytree(failed, verify)
        report = {"passed": True, "subject_sha256": subject_digest(verify)}
        with (
            mock.patch.object(store_module, "check_bundle", return_value=report),
            mock.patch.object(store_module, "require_qualified_report", return_value=report),
        ):
            preview = self.store.preview(RUN)
        self.assertIs(preview["verified"], True)
        self.assertEqual(preview["state"], "verified")
        self.assertEqual(preview["subject_sha256"], subject_digest(verify))
        stream, _size = self.store.open_artifact(RUN, "urdf/robot.urdf", sha256=preview["files"]["urdf/robot.urdf"])
        with stream:
            self.assertEqual(stream.read(), URDF)

    # ------------------------------------------------------------------ portal
    def test_portal_serves_the_retained_diagnostic_through_the_single_interface(self) -> None:
        self._retained_failed_verify()
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: _DownEndpoint(),
                pipeline_store_root=self.store.root,
            )
        )
        session = app.sessions.create("user", "token")
        preview = app._preview_payload(session, DAG_RUN_ID)
        self.assertIs(preview["verified"], False)
        self.assertEqual(preview["state"], "unverified")
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
            app._artifact(environ, start_response, DAG_RUN_ID, "reports/run.json")

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
