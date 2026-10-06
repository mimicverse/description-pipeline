"""Ownership, timeout recovery and CLI failures must hold without a CAD licence."""

from __future__ import annotations

import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from . import _paths  # noqa: F401
from description_pipeline.sources.solidworks.errors import BridgeError, CadError, EnvironmentError_  # noqa: E402
from description_pipeline.sources.solidworks.executor import ComExecutor  # noqa: E402
from description_pipeline.sources.solidworks.isolation import CadSession  # noqa: E402
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    SolidWorksBackend,
    _read_only_document,
    normalize_document_path,
)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.backend = SimpleNamespace()
        self.executor = ComExecutor(self.backend)

    def tearDown(self):
        self.release.set()
        self.executor.close()
        self.assertFalse(self.executor.alive)

    def test_timeout_reclaims_owned_process_and_recovers(self):
        def reclaim():
            self.release.set()
            return {"terminated": [42]}

        abort = Mock(side_effect=reclaim)
        self.backend.abort_owned_processes = abort
        with self.assertRaises(BridgeError) as caught:
            self.executor.run(lambda: self.release.wait(10), timeout=0.05)
        self.assertEqual(caught.exception.code, "modal_dialog_blocked")
        assert isinstance(caught.exception.detail, dict)
        self.assertTrue(caught.exception.detail["operation_stopped"])
        abort.assert_called_once()
        self.assertFalse(self.executor.busy)
        self.assertEqual(self.executor.run(lambda: 42), 42)

    def test_unstopped_timeout_blocks_admission_and_remains_busy(self):
        with self.assertRaises(BridgeError) as caught:
            self.executor.run(lambda: self.release.wait(10), timeout=0.05)
        assert isinstance(caught.exception.detail, dict)
        self.assertFalse(caught.exception.detail["operation_stopped"])
        self.assertTrue(self.executor.busy)
        with self.assertRaisesRegex(BridgeError, "previous CAD operation"):
            self.executor.run(lambda: None)

    def test_queued_timeout_does_not_abort_active_job(self):
        started = threading.Event()
        failures = []
        self.backend.abort_owned_processes = Mock()

        def first():
            try:

                def wait_for_release():
                    started.set()
                    self.release.wait(10)

                self.executor.run(wait_for_release)
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(started.wait(2))
        should_not_run = Mock()
        with self.assertRaises(BridgeError):
            self.executor.run(should_not_run, timeout=0.05)
        self.backend.abort_owned_processes.assert_not_called()
        self.release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.executor.run(lambda: "recovered"), "recovered")
        should_not_run.assert_not_called()

    def test_callable_timeout_error_is_not_watchdog_timeout(self):
        def operation():
            raise TimeoutError("input transport failed")

        with self.assertRaisesRegex(TimeoutError, "input transport failed"):
            self.executor.run(operation)
        self.assertFalse(self.executor.busy)
        self.assertEqual(self.executor.run(lambda: 7), 7)

    def test_named_cancellation_does_not_abort_another_operation(self):
        started = threading.Event()
        errors = []

        def reclaim():
            self.release.set()
            return {"terminated": [42]}

        self.backend.abort_owned_processes = Mock(side_effect=reclaim)

        def operation():
            started.set()
            self.release.wait(5)

        def run():
            try:
                self.executor.run(operation, operation_id="job-one")
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2))
        self.executor.cancel("another-job")
        self.backend.abort_owned_processes.assert_not_called()
        self.executor.cancel("job-one")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.backend.abort_owned_processes.assert_called_once()
        self.assertEqual(self.executor.run(lambda: "next"), "next")

    def test_closed_executor_rejects_work(self):
        self.executor.close()
        with self.assertRaisesRegex(BridgeError, "has closed"):
            self.executor.run(lambda: None)


class SessionTests(unittest.TestCase):
    def test_capture_rebuild_cannot_touch_the_original_assembly(self):
        backend = SolidWorksBackend()
        backend._capture_roots = [normalize_document_path("/snapshot/source")]
        doc = Mock()
        with self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, "/handoff/robot.SLDASM")
        self.assertEqual(caught.exception.code, "cad_rebuild_scope")
        doc.ForceRebuild3.assert_not_called()

    def test_capture_rebuild_is_in_memory_and_failure_blocks_measurement(self):
        backend = SolidWorksBackend()
        backend._capture_roots = [normalize_document_path("/snapshot/source")]
        doc = SimpleNamespace(
            ConfigurationManager=SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name="Default")),
            GetSaveFlag=False,
            GetPathName="/snapshot/source/robot.SLDASM",
            IsOpenedReadOnly=True,
            Save=Mock(),
            ForceRebuild3=Mock(spec=["__call__"], return_value=True),
        )
        result = backend._rebuild_capture_copy(doc, doc.GetPathName)
        self.assertEqual(result["scope"], "collected_copy_in_memory")
        self.assertFalse(result["saved_to_disk"])
        self.assertTrue(result["read_only"])
        doc.ForceRebuild3.assert_called_once_with(False)
        doc.Save.assert_not_called()
        doc.ForceRebuild3.return_value = False
        with self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, doc.GetPathName)
        self.assertEqual(caught.exception.code, "cad_rebuild_failed")

    def test_loaded_references_must_also_be_read_only(self):
        doc = SimpleNamespace(IsOpenedReadOnly=False, GetPathName="part.SLDPRT")
        calls = []

        def protect(value):
            calls.append(value)
            doc.IsOpenedReadOnly = value
            return True

        doc.SetReadOnlyState = protect
        self.assertIs(_read_only_document(doc), doc)
        self.assertTrue(doc.IsOpenedReadOnly)
        _read_only_document(doc)
        self.assertEqual(calls, [True])

    def test_read_only_failure_blocks_capture(self):
        doc = SimpleNamespace(IsOpenedReadOnly=False, GetPathName="part.SLDPRT", SetReadOnlyState=lambda value: True)
        with self.assertRaises(CadError) as caught:
            _read_only_document(doc)
        self.assertEqual(caught.exception.code, "cad_read_only_failed")

    def test_com_is_bound_only_to_owned_pid(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        app = SimpleNamespace(Visible=True, CommandInProgress=False)
        binder = Mock(return_value=app)
        session = CadSession(process=process, binder=binder)
        self.assertIs(session.connect(threading.Event()), app)
        binder.assert_called_once_with(42)
        self.assertFalse(app.Visible)
        self.assertTrue(app.CommandInProgress)
        session.close()
        process.close.assert_called_once()
        self.assertIsNone(session.app)

    def test_cancelled_startup_never_binds_an_application(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        cancelled = threading.Event()
        cancelled.set()
        binder = Mock()
        session = CadSession(process=process, binder=binder)
        with self.assertRaises(EnvironmentError_) as caught:
            session.connect(cancelled)
        self.assertEqual(caught.exception.code, "cad_session_cancelled")
        binder.assert_not_called()
        session.close()
        process.close.assert_called_once()

    def test_backend_releases_source_and_copy_after_failed_capture(self):
        sessions = []

        def factory():
            session = SimpleNamespace(app=object(), connect=Mock(), close=Mock(), terminate=Mock())
            sessions.append(session)
            return session

        backend = SolidWorksBackend(session_factory=factory)
        with self.assertRaisesRegex(RuntimeError, "failed"), backend.session():
            source = backend._app_for_path("/source/robot.SLDASM")
            from description_pipeline.sources.solidworks.native import normalize_document_path

            copy_root = os.path.abspath("/copy")
            backend._capture_roots.append(normalize_document_path(copy_root))
            copied = backend._app_for_path(os.path.join(copy_root, "robot.SLDASM"))
            self.assertIsNot(source, copied)
            with backend.session():
                self.assertIs(backend._app_for_path("/source/robot.SLDASM"), source)
            raise RuntimeError("failed")
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            session.close.assert_called_once()
        self.assertEqual(backend._sessions, {})
