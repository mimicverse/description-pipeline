"""Ownership, timeout recovery and CLI failures must hold without a CAD licence."""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import _paths  # noqa: F401
from description_pipeline.sources.solidworks import isolation  # noqa: E402
from description_pipeline.sources.solidworks.errors import CadError, EnvironmentError_  # noqa: E402
from description_pipeline.sources.solidworks.isolation import (  # noqa: E402
    CadSession,
    terminate_remnant_processes,
)
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    SolidWorksBackend,
    _read_only_document,
    normalize_document_path,
)


class AbortOwnedProcessesTests(unittest.TestCase):
    """Reclaim-on-timeout coverage, migrated to its live owner (NAT-01).

    The retired ``ComExecutor`` covered the timeout/cancel reclaim path; the live
    owner is ``SolidWorksBackend.abort_owned_processes`` — it must cancel the
    capture, terminate every open owned session, keep an unreachable session from
    stopping the sweep, and refuse further CAD work once cancelled.
    """

    @staticmethod
    def _session(pid: int, *, closed: bool = False, error: Exception | None = None):
        session = SimpleNamespace(
            closed=closed,
            identity=Mock(return_value={"pid": pid, "executable": "SLDWORKS.exe"}),
        )
        session.terminate = Mock(side_effect=error)
        return session

    def test_abort_terminates_open_owned_sessions_and_collects_failures(self):
        backend = SolidWorksBackend()
        healthy = self._session(42)
        failing = self._session(43, error=OSError("termination refused"))
        retired = self._session(44, closed=True)
        backend._sessions.update({"source": healthy, "copy": failing, "retired": retired})

        receipt = backend.abort_owned_processes()

        self.assertTrue(backend._cancelled.is_set())
        self.assertEqual(receipt["terminated"], [{"pid": 42, "executable": "SLDWORKS.exe"}])
        self.assertEqual(
            receipt["errors"],
            [{"pid": 43, "executable": "SLDWORKS.exe", "error": "termination refused"}],
        )
        healthy.terminate.assert_called_once_with()
        failing.terminate.assert_called_once_with()
        retired.terminate.assert_not_called()

    def test_cancelled_backend_refuses_further_cad_work(self):
        backend = SolidWorksBackend()
        backend.abort_owned_processes()

        with self.assertRaises(EnvironmentError_) as caught:
            backend._app_obj()

        self.assertEqual(caught.exception.code, "cad_session_cancelled")


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
        state = SimpleNamespace(NeedsRebuild2=1)
        configuration = SimpleNamespace(
            Name="Default",
            NeedsRebuild=False,
        )
        doc = SimpleNamespace(
            ConfigurationManager=SimpleNamespace(
                ActiveConfiguration=configuration,
            ),
            Extension=state,
            GetSaveFlag=False,
            GetPathName="/snapshot/source/robot.SLDASM",
            IsOpenedReadOnly=True,
            Save=Mock(),
            ForceRebuild3=Mock(spec=["__call__"], return_value=True),
        )

        def rebuild(_force):
            state.NeedsRebuild2 = 0
            return True

        doc.ForceRebuild3.side_effect = rebuild
        refreshed = []

        def refresh(path):
            refreshed.append(path)
            return doc

        backend._document_by_path = refresh
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ):
            rebuilt, result = backend._rebuild_capture_copy(doc, doc.GetPathName)
        self.assertIs(rebuilt, doc)
        self.assertEqual(result["scope"], "collected_copy_in_memory")
        self.assertFalse(result["saved_to_disk"])
        self.assertTrue(result["read_only"])
        self.assertTrue(result["rebuild_performed"])
        doc.ForceRebuild3.assert_called_once_with(False)
        doc.Save.assert_not_called()
        self.assertEqual(refreshed, [doc.GetPathName])
        state.NeedsRebuild2 = 1
        doc.ForceRebuild3.side_effect = None
        doc.ForceRebuild3.return_value = False
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, doc.GetPathName)
        self.assertEqual(caught.exception.code, "cad_rebuild_failed")
        self.assertEqual(caught.exception.detail["path"], doc.GetPathName)
        self.assertIs(caught.exception.detail["result"], False)
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

    def test_startup_timeout_terminates_the_started_process(self):
        process = SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
        app = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=False)
        binder = Mock(return_value=app)
        session = CadSession(process=process, binder=binder, startup_timeout=0)

        with self.assertRaises(EnvironmentError_) as caught:
            session.connect(threading.Event())
        self.assertEqual(caught.exception.code, "cad_startup_failed")
        self.assertEqual(caught.exception.detail["pid"], 42)
        process.close.assert_called_once()
        self.assertFalse(session.closed)

    def test_startup_timeout_cleanup_failure_does_not_mask_the_timeout(self):
        process = SimpleNamespace(
            pid=42,
            executable="test.exe",
            alive=lambda: True,
            close=Mock(side_effect=OSError("still running")),
        )
        app = SimpleNamespace(Visible=True, CommandInProgress=False, StartupProcessCompleted=False)
        session = CadSession(process=process, binder=Mock(return_value=app), startup_timeout=0)

        with self.assertRaises(EnvironmentError_) as caught:
            session.connect(threading.Event())
        self.assertEqual(caught.exception.code, "cad_startup_failed")
        process.close.assert_called_once()

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


class RemnantSweepTests(unittest.TestCase):
    """The pre-start sweep terminates only RECORDED leftovers whose owning
    process is proven gone; anything unrecorded or unverifiable refuses the
    launch instead of being killed."""

    EXE = "/opt/SLDPRT/SLDWORKS.exe"
    OTHER = "/opt/other/other.exe"
    BOOT = 1_700_000_000.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "cad-ownership.json"
        # The resolver only restates a bound domain: pin this test's ledger.
        pin = patch.dict(os.environ, {"DESCRIPTION_CAD_OWNERSHIP_FILE": str(self.ledger)})
        pin.start()
        self.addCleanup(pin.stop)

    def _record(self, pid, *, creation=100, boot=None, owner="earlier-owner", owner_pid=999, owner_creation=555):
        record = {
            "pid": pid,
            "executable": self.EXE,
            "creation_time": creation,
            "boot_epoch": self.BOOT if boot is None else boot,
            "owner": owner,
            "owner_pid": owner_pid,
            "recorded_at": 0.0,
        }
        if owner_creation is not None:
            record["owner_creation_time"] = owner_creation
        isolation.OwnershipStore(self.ledger).add(record)

    def _sweep(self, table, *, tracked=(), exclude=(), creation=None, boot=None, killed=None, slept=None):
        killed = [] if killed is None else killed
        slept = [] if slept is None else slept

        def terminate(pid, _creation_time=None):
            killed.append(pid)
            return True

        with patch.object(isolation, "_TRACKED_PIDS", set(tracked)):
            result = terminate_remnant_processes(
                self.EXE,
                exclude_pids=exclude,
                process_table=lambda: table,
                terminate_pid=terminate,
                sleep=slept.append,
                ownership_file=self.ledger,
                creation_time=creation or (lambda pid: 100),
                boot_epoch=boot or (lambda: self.BOOT),
            )
        return result, killed, slept

    def test_recorded_leftover_of_an_earlier_owner_is_swept_with_one_settle(self):
        self._record(11)
        result, killed, slept = self._sweep([(11, self.EXE), (22, self.OTHER)])
        self.assertEqual(result, [11])
        self.assertEqual(killed, [11])
        self.assertEqual(slept, [isolation.REMNANT_SETTLE_SECONDS])
        self.assertEqual(isolation.OwnershipStore(self.ledger).load(), {})

    def test_unrecorded_same_executable_refuses_and_is_never_terminated(self):
        with self.assertRaises(EnvironmentError_) as caught:
            self._sweep([(4242, self.EXE)])
        self.assertEqual(caught.exception.code, "cad_external_solidworks")
        self.assertIn("close it before capturing", str(caught.exception))
        self.assertEqual(caught.exception.detail["pids"], [4242])

    def test_tracked_and_excluded_pids_are_never_swept_or_refused(self):
        result, killed, slept = self._sweep([(11, self.EXE), (33, self.EXE)], tracked={11}, exclude={33})
        self.assertEqual((result, killed, slept), ([], [], []))

    def test_other_executable_is_untouched(self):
        result, killed, slept = self._sweep([(22, self.OTHER)])
        self.assertEqual((result, killed, slept), ([], [], []))

    def test_pid_reuse_with_a_different_creation_time_is_never_terminated(self):
        self._record(11, creation=100)
        with self.assertRaises(EnvironmentError_) as caught:
            self._sweep([(11, self.EXE)], creation=lambda pid: 200)
        self.assertEqual(caught.exception.code, "cad_external_solidworks")

    def test_record_from_another_boot_is_not_trusted(self):
        self._record(11, boot=self.BOOT - 3600.0)
        with self.assertRaises(EnvironmentError_) as caught:
            self._sweep([(11, self.EXE)])
        self.assertEqual(caught.exception.code, "cad_external_solidworks")

    def test_own_record_is_left_to_the_windows_job(self):
        self._record(11, owner=isolation._OWNER_TOKEN)
        result, killed, slept = self._sweep([(11, self.EXE)])
        self.assertEqual((result, killed, slept), ([], [], []))

    def test_unreadable_recorded_process_refuses_instead_of_launching(self):
        self._record(11)
        killed = []
        with self.assertRaises(EnvironmentError_) as caught:
            self._sweep([(11, self.EXE)], creation=lambda pid: None, killed=killed)
        self.assertEqual(caught.exception.code, "cad_external_solidworks")
        self.assertIn("cannot be verified", str(caught.exception))
        self.assertEqual(caught.exception.detail["unverified_pids"], [11])
        self.assertEqual(killed, [])
        # the record stays: the process is still enumerated and undecided
        self.assertIn(11, isolation.OwnershipStore(self.ledger).load())

    def test_record_of_a_gone_owner_pid_is_swept(self):
        self._record(11, owner_pid=888, owner_creation=555)
        result, killed, slept = self._sweep(
            [(11, self.EXE)], creation=lambda pid: None if pid == 888 else 100
        )
        self.assertEqual(result, [11])
        self.assertEqual(killed, [11])
        self.assertEqual(slept, [isolation.REMNANT_SETTLE_SECONDS])
        self.assertEqual(isolation.OwnershipStore(self.ledger).load(), {})

    def test_live_owner_record_refuses_and_is_never_terminated(self):
        self._record(11, owner="other-live-owner", owner_pid=777, owner_creation=100)
        killed = []
        with self.assertRaises(EnvironmentError_) as caught:
            self._sweep([(11, self.EXE)], killed=killed)
        self.assertEqual(caught.exception.code, "cad_external_solidworks")
        self.assertIn("another live pipeline process", str(caught.exception))
        self.assertEqual(caught.exception.detail["live_owner_pids"], [11])
        self.assertEqual(killed, [])
        # the live owner's record is retained for its own later cleanup
        self.assertIn(11, isolation.OwnershipStore(self.ledger).load())

    def test_record_without_owner_evidence_is_never_terminated(self):
        self._record(11, owner_creation=None)
        killed = []
        with self.assertRaises(EnvironmentError_) as caught:
            self._sweep([(11, self.EXE)], killed=killed)
        self.assertEqual(caught.exception.detail["unverified_pids"], [11])
        self.assertEqual(killed, [])

    def test_record_without_a_usable_creation_identity_never_matches(self):
        self._record(11, creation=None)
        killed = []
        with self.assertRaises(EnvironmentError_) as caught:
            self._sweep([(11, self.EXE)], killed=killed)
        self.assertEqual(caught.exception.detail["unrecorded_pids"], [11])
        self.assertEqual(killed, [])

    def test_stale_record_for_a_gone_pid_is_pruned(self):
        self._record(99)
        result, _killed, _slept = self._sweep([(22, self.OTHER)])
        self.assertEqual(result, [])
        self.assertEqual(isolation.OwnershipStore(self.ledger).load(), {})

    def test_no_leftovers_leave_the_settle_untouched(self):
        result, killed, slept = self._sweep([])
        self.assertEqual((result, killed, slept), ([], [], []))

    def test_ledger_roundtrip_and_foreign_schema_reads_empty(self):
        store = isolation.OwnershipStore(self.ledger)
        store.add({"pid": 7, "executable": self.EXE, "creation_time": 1, "boot_epoch": 0.0, "owner": "x"})
        self.assertEqual(list(store.load()), [7])
        store.remove(7, creation_time=1)
        self.assertEqual(store.load(), {})
        self.ledger.write_text('{"schema_version": "foreign/v9", "records": []}', encoding="utf-8")
        self.assertEqual(store.load(), {})


class OwnershipLedgerLockTests(unittest.TestCase):
    """Ledger read-modify-write is serialized in-process and across processes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "cad-ownership.json"

    def test_every_ledger_write_takes_the_sidecar_process_lock(self):
        store = isolation.OwnershipStore(self.ledger)
        seen: list[Path] = []
        real = isolation.acquire_process_lock

        def spy(path):
            seen.append(Path(path))
            return real(path)

        with patch.object(isolation, "acquire_process_lock", spy):
            store.add({"pid": 7, "executable": "/x", "creation_time": 1, "boot_epoch": 0.0, "owner": "t"})
            store.remove(7, creation_time=1)
        self.assertEqual(seen, [store.lock_path, store.lock_path])
        self.assertTrue(store.lock_path.is_file())
        self.assertEqual(store.load(), {})

    def test_concurrent_store_instances_do_not_lose_records(self):
        stores = (isolation.OwnershipStore(self.ledger), isolation.OwnershipStore(self.ledger))
        original = isolation.OwnershipStore._save
        errors: list = []

        def slow_save(self, records):
            time.sleep(0.002)  # widen the read-modify-write window
            return original(self, records)

        def writer(store, base):
            try:
                for index in range(120):
                    store.add(
                        {
                            "pid": base + index + 1,  # pid 0 is reserved and filtered by load()
                            "executable": "/x",
                            "creation_time": 1,
                            "boot_epoch": 0.0,
                            "owner": "t",
                        }
                    )
            except Exception as error:  # pragma: no cover - diagnostic only
                errors.append(error)

        with patch.object(isolation.OwnershipStore, "_save", slow_save):
            threads = [
                threading.Thread(target=writer, args=(store, 1000 * index)) for index, store in enumerate(stores)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(stores[0].load()), 240)


class _PyTime(datetime.datetime):
    """pywin32's PyTime is a datetime subclass; the parser must accept it."""


class OwnershipRemovalTests(unittest.TestCase):
    """Removal must prove identity: a stale cleanup never deletes a newer record."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "cad-ownership.json"

    def test_removal_without_an_identity_never_deletes_a_newer_record(self):
        store = isolation.OwnershipStore(self.ledger)
        store.add({"pid": 7, "executable": "/x", "creation_time": None, "boot_epoch": 0.0, "owner": "old"})
        store.add({"pid": 7, "executable": "/x", "creation_time": 200, "boot_epoch": 0.0, "owner": "new"})
        store.remove(7, creation_time=None)  # stale cleanup that cannot state the identity
        self.assertIn(7, store.load())
        store.remove(7, creation_time=100)  # wrong identity
        self.assertIn(7, store.load())
        store.remove(7, creation_time=200)  # exact identity
        self.assertEqual(store.load(), {})
        store.add({"pid": 8, "executable": "/x", "creation_time": None, "boot_epoch": 0.0, "owner": "old"})
        store.remove(8, creation_time=None)
        self.assertIn(8, store.load())  # None is never a removable identity


class CreationTicksTests(unittest.TestCase):
    """pywin32 exposes GetProcessTimes in several shapes; all normalize alike."""

    def test_pywin32_dict_shape_normalizes_to_ticks(self):
        # epoch + 1 s + 1 us = 10**7 + 10 ticks, a literal independent of the parser
        moment = _PyTime(1601, 1, 1, 0, 0, 1, 1)
        snapshot = {"CreationTime": moment, "ExitTime": None, "KernelTime": None, "UserTime": None}
        self.assertEqual(isolation._creation_ticks(snapshot), 10**7 + 10)

    def test_timezone_aware_pywin32_shape_normalizes_via_utc(self):
        # The endpoint's own Python returns dict + timezone-aware pywintypes
        # datetimes; the same instant in any zone must yield the same ticks.
        utc = _PyTime(2026, 10, 13, 8, 33, 16, 528000, tzinfo=datetime.UTC)
        aware = _PyTime(2026, 10, 13, 16, 33, 16, 528000, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        named = _PyTime(2026, 10, 13, 9, 33, 16, 528000, tzinfo=datetime.timezone(datetime.timedelta(hours=1)))
        expected = isolation._creation_ticks({"CreationTime": utc})
        self.assertEqual(isolation._creation_ticks({"CreationTime": aware}), expected)
        self.assertEqual(isolation._creation_ticks({"CreationTime": named}), expected)
        self.assertEqual(isolation._creation_ticks((aware, None, None, None)), expected)

    def test_legacy_tuple_shape_normalizes_to_ticks(self):
        moment = _PyTime(1601, 1, 1, 0, 0, 2)
        self.assertEqual(isolation._creation_ticks((moment, None, None, None)), 2 * 10**7)

    def test_filetime_struct_shape_normalizes_to_ticks(self):
        struct = SimpleNamespace(dwHighDateTime=2, dwLowDateTime=5)
        self.assertEqual(isolation._creation_ticks(struct), (2 << 32) | 5)

    def test_unusable_shapes_are_never_authority(self):
        self.assertIsNone(isolation._creation_ticks({}))
        self.assertIsNone(isolation._creation_ticks((None,)))
        self.assertIsNone(isolation._creation_ticks(None))
        self.assertIsNone(isolation._creation_ticks({"CreationTime": "not-a-time"}))


class WindowsTerminateTests(unittest.TestCase):
    """The kill verifies the creation moment on the very handle that may kill."""

    def _modules(self, handle, times, killed, *, fail_read=False):
        def get_times(_handle):
            if fail_read:
                raise OSError("unreadable")
            return times

        return {
            "win32api": SimpleNamespace(
                OpenProcess=lambda access, inherit, pid: handle,
                TerminateProcess=lambda opened, code: killed.append(opened),
            ),
            "win32process": SimpleNamespace(GetProcessTimes=get_times),
            "win32event": SimpleNamespace(WaitForSingleObject=lambda opened, ms: 0),
            "win32con": SimpleNamespace(
                PROCESS_QUERY_INFORMATION=1, PROCESS_TERMINATE=2, SYNCHRONIZE=4
            ),
        }

    def test_matching_creation_time_terminates_on_the_same_handle(self):
        handle = SimpleNamespace()
        killed: list = []
        times = {"CreationTime": isolation._CREATION_EPOCH + datetime.timedelta(seconds=1)}
        with patch.dict(sys.modules, self._modules(handle, times, killed)):
            self.assertTrue(isolation._windows_terminate_matching(11, 10**7))
        self.assertEqual(killed, [handle])  # the exact handle opened for verification

    def test_mismatched_creation_time_never_terminates(self):
        handle = SimpleNamespace()
        killed: list = []
        times = {"CreationTime": isolation._CREATION_EPOCH + datetime.timedelta(seconds=9)}
        with patch.dict(sys.modules, self._modules(handle, times, killed)):
            self.assertFalse(isolation._windows_terminate_matching(11, 10**7))
        self.assertEqual(killed, [])

    def test_unreadable_process_times_never_terminate(self):
        handle = SimpleNamespace()
        killed: list = []
        with patch.dict(sys.modules, self._modules(handle, None, killed, fail_read=True)):
            self.assertFalse(isolation._windows_terminate_matching(11, 10**7))
        self.assertEqual(killed, [])
