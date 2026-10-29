"""Capture exceptions preserve the actual cleanup proof and native error detail."""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from description_pipeline.orchestration.native_operation_runtime import NativeFailureView, NativeOperationError
from description_pipeline.pipeline import capture
from description_pipeline.sources.solidworks.errors import CadError, EnvironmentError_
from description_pipeline.sources.solidworks.native import SolidWorksBackend
from tests.v1 import test_capture_handler as capture_tests

DETAIL = {"body": "base_link", "component": "base_link3.0-1/missing-occurrence"}


class SessionCleanupProofTests(unittest.TestCase):
    def session_failure(self, *, cleanup_fails=False):
        backend = SolidWorksBackend()
        receipt = {}
        session = SimpleNamespace(closed=False)
        def close():
            if cleanup_fails:
                raise OSError("Owned process exit is unproven")
            session.closed = True
        session.close = Mock(side_effect=close)
        error = CadError("link_group_member_unknown", "Unknown native occurrence", DETAIL)
        with self.assertRaises(CadError) as caught, backend.session(resource_settlement=receipt):
            backend._owner_thread = threading.current_thread()
            backend._sessions["copy"] = session
            raise error
        self.assertIs(caught.exception, error)
        session.close.assert_called_once()
        return backend, receipt

    def test_real_session_exit_records_success_after_domain_exception(self):
        backend, receipt = self.session_failure()
        self.assertTrue(receipt["settled"])
        self.assertEqual(backend._sessions, {})

    def test_real_session_cleanup_failure_cannot_claim_resource_release(self):
        backend, receipt = self.session_failure(cleanup_fails=True)
        self.assertFalse(receipt["settled"])
        self.assertEqual(receipt["cleanup_error"]["code"], "cad_process_cleanup_failed")
        self.assertIn("copy", backend._sessions)

    def test_nested_scope_cannot_claim_the_outer_session_was_released(self):
        backend = SolidWorksBackend()
        receipt = {}
        with backend.session():
            with backend.session(resource_settlement=receipt):
                pass
            self.assertFalse(receipt["settled"])
        self.assertFalse(receipt["settled"])


class CaptureFailureReceiptTests(unittest.TestCase):
    def setUp(self):
        self.case = capture_tests.CaptureHandlerTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.workspace = self.case.workspace(name="capture-failure-lifecycle")
        self.request = capture.CaptureRequest.from_command(self.case.command)
        self.inputs = capture.CaptureInputs.resolve(self.case.command, self.workspace, self.request)
        self.backend = capture_tests.fixture_backend(self.inputs.package, capture_tests.MAIN)

    def transaction(self, *, expected_inputs=None):
        return capture._capture_native_transaction(
            self.inputs.package, self.case.root / "failure-output", backend=self.backend,
            run_id=self.case.command.run_id, expected_inputs=(
                self.inputs.prepared_files if expected_inputs is None else expected_inputs
            ), handoff_sha256=self.request.handoff_sha256, upload_sha256=self.request.upload_sha256,
            main_assembly=self.request.main_assembly, upstream_artifacts=(), execution={},
            native_tool_factory=lambda: self.case.current_native_tool,
            request_evidence=self.inputs.request_evidence,
        )

    def test_actual_capture_exception_after_entry_retains_detail_and_successful_cleanup(self):
        self.backend.collect_prepared_scene = Mock(side_effect=CadError(
            "link_group_member_unknown", "Unknown native occurrence", DETAIL,
        ))
        receipt = self.transaction()
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["error_code"], "link_group_member_unknown")
        self.assertEqual(receipt["detail"], DETAIL)
        self.assertTrue(receipt["resource_settlement"]["settled"])
        self.assertTrue(Path(receipt["diagnostic_path"]).is_dir())
        self.assertEqual(self.backend.open_documents, [])

    def test_actual_capture_cleanup_failure_remains_unsettled_and_keeps_primary_detail(self):
        self.backend.collect_prepared_scene = Mock(side_effect=CadError(
            "link_group_member_unknown", "Unknown native occurrence", DETAIL,
        ))
        self.backend.release = Mock(side_effect=EnvironmentError_("cad_process_cleanup_failed", "Exit unproven"))
        receipt = self.transaction()
        self.assertFalse(receipt["resource_settlement"]["settled"])
        self.assertEqual(receipt["error_code"], "link_group_member_unknown")
        self.assertEqual(receipt["detail"], DETAIL)
        self.assertIn("cleanup_error", receipt["resource_settlement"])

    def test_rejection_before_session_has_no_native_resource_to_release(self):
        self.backend.session = Mock(side_effect=AssertionError("The native session must not start"))
        receipt = self.transaction(expected_inputs={})
        self.assertTrue(receipt["resource_settlement"]["settled"])
        self.assertEqual(receipt["resource_settlement"]["reason"], "rejected_before_native_session")
        self.backend.session.assert_not_called()

    def test_offline_failure_after_release_does_not_lose_cleanup_proof(self):
        with patch("description_pipeline.sources.solidworks.capture_projection.build_native_capture_observation",
                   side_effect=CadError("projection_failed", "Projection failed after CAD exit", DETAIL)):
            receipt = self.transaction()
        self.assertEqual(receipt["error_code"], "projection_failed")
        self.assertTrue(receipt["resource_settlement"]["settled"])
        self.assertEqual(receipt["detail"], DETAIL)

    def test_failure_view_current_shape_roundtrips_original_detail(self):
        detail = {"native_detail": DETAIL, "diagnostic_path": "operation-diagnostic"}
        view = NativeFailureView("link_group_member_unknown", "Unknown native occurrence", detail)
        self.assertEqual(NativeFailureView.from_record(view.to_record()).detail, detail)
        with self.assertRaises(NativeOperationError):
            NativeFailureView.from_record({"code": view.code, "message": view.message})

    def test_capture_handler_puts_native_detail_in_the_existing_stage_failure(self):
        failure_root = self.workspace.root / "remote-failure"
        failure_root.mkdir()
        (failure_root / "native-result.json").write_text("{}\n")
        operation = Mock(return_value=capture.NativeCaptureResult(
            "failed", failure_root, None,
            {"failure": NativeFailureView(
                "link_group_member_unknown", "Unknown native occurrence", {"native_detail": DETAIL},
            ).to_record()},
            True, "link_group_member_unknown",
        ))
        result = self.case.handler(native_operation=operation).execute(
            self.case.command, self.case.claim, self.workspace, effects=self.case.effects(),
        )
        self.assertEqual(result.failure.detail, {"native_detail": DETAIL})
        self.assertEqual(str(result.failure), "Unknown native occurrence")
