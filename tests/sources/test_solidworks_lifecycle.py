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
        refreshed = []

        def refresh(path):
            refreshed.append(path)
            return doc

        backend._document_by_path = refresh
        rebuilt, result = backend._rebuild_capture_copy(doc, doc.GetPathName)
        self.assertIs(rebuilt, doc)
        self.assertEqual(result["scope"], "collected_copy_in_memory")
        self.assertFalse(result["saved_to_disk"])
        self.assertTrue(result["read_only"])
        doc.ForceRebuild3.assert_called_once_with(False)
        doc.Save.assert_not_called()
        self.assertEqual(refreshed, [doc.GetPathName])
        doc.ForceRebuild3.return_value = False
        with self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, doc.GetPathName)
        self.assertEqual(caught.exception.code, "cad_rebuild_failed")
        self.assertEqual(refreshed, [doc.GetPathName])

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
        app = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=True)
        binder = Mock(return_value=app)
        session = CadSession(process=process, binder=binder)
        self.assertIs(session.connect(threading.Event()), app)
        binder.assert_called_once_with(42)
        self.assertFalse(app.Visible)
        self.assertTrue(app.CommandInProgress)
        session.close()
        process.close.assert_called_once()
        self.assertIsNone(session.app)

    def test_startup_waits_for_add_ins_before_enabling_automation(self):
        events = []

        class StartingApplication:
            ready = False

            @property
            def StartupProcessCompleted(self):
                self.ready = sum(event[0] == "startup" for event in events) == 2
                events.append(("startup", self.ready))
                return self.ready

            def __setattr__(self, name, value):
                if name in {"CommandInProgress", "Visible"}:
                    if not self.ready:
                        raise AssertionError("automation started before SolidWorks finished loading add-ins")
                    events.append((name, value))
                object.__setattr__(self, name, value)

        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        app = StartingApplication()
        binder = Mock(return_value=app)
        cancelled = Mock(is_set=Mock(return_value=False), wait=Mock(side_effect=lambda _: events.append(("wait",))))
        session = CadSession(process=process, binder=binder)

        self.assertIs(session.connect(cancelled), app)
        self.assertEqual(
            events,
            [
                ("startup", False),
                ("wait",),
                ("startup", False),
                ("wait",),
                ("startup", True),
                ("CommandInProgress", True),
                ("Visible", False),
            ],
        )
        binder.assert_called_once_with(42)
        session.close()

    def test_incomplete_startup_has_a_deadline_and_never_enables_automation(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        app = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=False)
        binder = Mock(return_value=app)
        session = CadSession(process=process, binder=binder, startup_timeout=0)

        with self.assertRaises(EnvironmentError_) as caught:
            session.connect(threading.Event())
        self.assertEqual(caught.exception.code, "cad_startup_failed")
        self.assertIsNone(session.app)
        self.assertTrue(app.Visible)
        self.assertFalse(app.CommandInProgress)
        binder.assert_called_once_with(42)
        session.close()

    def test_startup_read_failure_is_preserved_and_never_retried(self):
        failure = RuntimeError("RPC_S_UNKNOWN_IF")

        class UnreadableApplication:
            @property
            def StartupProcessCompleted(self):
                raise failure

        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        binder = Mock(return_value=UnreadableApplication())
        cancelled = Mock(is_set=Mock(return_value=False), wait=Mock())
        session = CadSession(process=process, binder=binder)

        with self.assertRaises(EnvironmentError_) as caught:
            session.connect(cancelled)
        self.assertEqual(caught.exception.code, "cad_startup_unreadable")
        self.assertIs(caught.exception.__cause__, failure)
        self.assertIsNone(session.app)
        binder.assert_called_once_with(42)
        cancelled.wait.assert_not_called()
        session.close()

    def test_missing_startup_readiness_cannot_be_assumed_ready(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        binder = Mock(return_value=SimpleNamespace())
        session = CadSession(process=process, binder=binder)

        with self.assertRaises(EnvironmentError_) as caught:
            session.connect(threading.Event())
        self.assertEqual(caught.exception.code, "cad_startup_unreadable")
        self.assertIsInstance(caught.exception.__cause__, AttributeError)
        self.assertIsNone(session.app)
        binder.assert_called_once_with(42)
        session.close()

    def test_non_boolean_startup_readiness_cannot_enable_automation(self):
        for value in (1, "True", None):
            with self.subTest(value=value):
                process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
                app = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=value)
                binder = Mock(return_value=app)
                session = CadSession(process=process, binder=binder)

                with self.assertRaises(EnvironmentError_) as caught:
                    session.connect(threading.Event())
                self.assertEqual(caught.exception.code, "cad_startup_unreadable")
                self.assertIsNone(session.app)
                self.assertTrue(app.Visible)
                self.assertFalse(app.CommandInProgress)
                binder.assert_called_once_with(42)
                session.close()

    def test_cancellation_during_startup_does_not_enable_automation(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        app = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=False)
        binder = Mock(return_value=app)
        cancelled = threading.Event()
        cancelled.wait = Mock(side_effect=lambda _: cancelled.set())
        session = CadSession(process=process, binder=binder)

        with self.assertRaises(EnvironmentError_) as caught:
            session.connect(cancelled)
        self.assertEqual(caught.exception.code, "cad_session_cancelled")
        self.assertIsNone(session.app)
        self.assertTrue(app.Visible)
        self.assertFalse(app.CommandInProgress)
        binder.assert_called_once_with(42)
        session.close()

    def test_process_exit_at_readiness_cannot_return_a_session(self):
        alive = True

        class ExitingApplication:
            @property
            def StartupProcessCompleted(self):
                nonlocal alive
                alive = False
                return True

        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: alive, close=Mock())
        binder = Mock(return_value=ExitingApplication())
        session = CadSession(process=process, binder=binder, startup_timeout=0)

        with self.assertRaises(EnvironmentError_) as caught:
            session.connect(threading.Event())
        self.assertEqual(caught.exception.code, "cad_startup_failed")
        self.assertIsNone(session.app)
        binder.assert_called_once_with(42)
        session.close()

    def test_startup_registration_polling_still_checks_readiness(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        app = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=True)
        binder = Mock(side_effect=[None, app])
        cancelled = Mock(is_set=Mock(return_value=False), wait=Mock())
        session = CadSession(process=process, binder=binder)

        self.assertIs(session.connect(cancelled), app)
        self.assertEqual([call.args for call in binder.call_args_list], [(42,), (42,)])
        cancelled.wait.assert_called_once_with(0.2)
        session.close()

    def test_current_application_uses_a_fresh_interface_for_the_same_owned_pid(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        first = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=True)
        current = SimpleNamespace()
        binder = Mock(side_effect=[first, current])
        session = CadSession(process=process, binder=binder)
        session.connect(threading.Event())

        self.assertIs(session.current_application(), current)
        self.assertIs(session.app, current)
        self.assertEqual([call.args for call in binder.call_args_list], [(42,), (42,)])
        process.close.assert_not_called()
        session.close()

    def test_current_application_does_not_rebind_a_dead_owned_process(self):
        alive = True
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: alive, close=Mock())
        binder = Mock(return_value=SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=True))
        session = CadSession(process=process, binder=binder)
        session.connect(threading.Event())
        alive = False

        with self.assertRaises(EnvironmentError_) as caught:
            session.current_application()
        self.assertEqual(caught.exception.code, "cad_application_unreadable")
        binder.assert_called_once_with(42)
        process.close.assert_not_called()
        session.close()

    def test_current_application_does_not_wait_or_retry_a_missing_binding(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        original = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=True)
        binder = Mock(side_effect=[original, None])
        session = CadSession(process=process, binder=binder)
        session.connect(threading.Event())

        with self.assertRaises(EnvironmentError_) as caught:
            session.current_application()
        self.assertEqual(caught.exception.code, "cad_application_unreadable")
        self.assertIs(session.app, original)
        self.assertEqual(binder.call_count, 2)
        process.close.assert_not_called()
        session.close()

    def test_current_application_retains_the_binding_failure_as_its_cause(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        failure = RuntimeError("RPC_S_UNKNOWN_IF")
        original = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=True)
        binder = Mock(side_effect=[original, failure])
        session = CadSession(process=process, binder=binder)
        session.connect(threading.Event())

        with self.assertRaises(EnvironmentError_) as caught:
            session.current_application()
        self.assertEqual(caught.exception.code, "cad_application_unreadable")
        self.assertIs(caught.exception.__cause__, failure)
        self.assertEqual(binder.call_count, 2)
        self.assertIs(session.app, original)
        session.close()

    def test_current_application_refuses_an_owner_that_died_during_binding(self):
        alive = True
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: alive, close=Mock())
        original = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=True)
        replacement = SimpleNamespace()

        def bind(pid):
            nonlocal alive
            self.assertEqual(pid, 42)
            if not hasattr(bind, "connected"):
                bind.connected = True
                return original
            alive = False
            return replacement

        session = CadSession(process=process, binder=bind)
        session.connect(threading.Event())
        with self.assertRaises(EnvironmentError_) as caught:
            session.current_application()
        self.assertEqual(caught.exception.code, "cad_application_unreadable")
        self.assertIs(session.app, original)
        session.close()

    def test_current_application_cannot_cross_the_owning_sta_thread(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        binder = Mock(return_value=SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=True))
        session = CadSession(process=process, binder=binder)
        session.connect(threading.Event())
        failures = []

        def read_elsewhere():
            try:
                session.current_application()
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=read_elsewhere)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], EnvironmentError_)
        self.assertEqual(failures[0].code, "cad_thread_mismatch")
        binder.assert_called_once_with(42)
        session.close()

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
            session = SimpleNamespace(app=object(), connect=Mock(), close=Mock(), terminate=Mock(), closed=False)
            session.close.side_effect = lambda: setattr(session, "closed", True)
            session.identity = lambda: {"ownership": "test"}
            session.connect.return_value = session.app
            session.current_application = lambda: session.app
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
                with self.assertRaises(EnvironmentError_) as retired:
                    backend._app_for_path("/source/robot.SLDASM")
                self.assertEqual(retired.exception.code, "cad_session_retired")
            raise RuntimeError("failed")
        self.assertEqual(len(sessions), 2)
        sessions[0].close.assert_called_once()
        sessions[1].close.assert_called_once()
        self.assertEqual(backend._sessions, {})
