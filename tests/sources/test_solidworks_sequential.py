"""The owned prepared session must close cleanly and never reconnect."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from . import _paths  # noqa: F401
from description_pipeline.sources.solidworks.errors import EnvironmentError_  # noqa: E402
from description_pipeline.sources.solidworks.isolation import CadSession  # noqa: E402
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    SolidWorksBackend,
)


class SequentialSessionTests(unittest.TestCase):
    def setUp(self):
        self.sessions = []
        self.binders = []
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.document = str(Path(self.root.name) / "prepared" / "robot.SLDASM")

        def factory():
            process = SimpleNamespace(pid=100, executable="test.exe", running=True)

            def close():
                process.running = False

            process.close = Mock(side_effect=close)
            process.alive = lambda: process.running
            app = SimpleNamespace(
                StartupProcessCompleted=True,
                RevisionNumber=lambda: "revision-0",
                GetBuildNumbers=lambda: "test-build",
                GetCurrentLicenseType=lambda: 1,
            )
            binder = Mock(return_value=app)
            session = CadSession(process=process, binder=binder)
            self.sessions.append(session)
            self.binders.append(binder)
            return session

        self.backend = SolidWorksBackend(session_factory=factory)
        self.addCleanup(self.backend.release)

    def test_path_selection_restores_absent_thread_local_role(self):
        self.backend._app_for_path(self.document)
        self.assertFalse(hasattr(self.backend._local, "role"))

    def test_closed_session_cannot_reconnect_and_close_is_idempotent(self):
        self.backend._app_for_path(self.document)
        session = self.sessions[0]
        session.close()
        with self.assertRaises(EnvironmentError_) as caught:
            session.connect(threading.Event())
        self.assertEqual(caught.exception.code, "cad_session_retired")
        session.close()
        session.process.close.assert_called_once()
        self.assertEqual(self.binders[0].call_count, 1)
        with self.assertRaises(EnvironmentError_) as caught:
            self.backend._app_obj()
        self.assertEqual(caught.exception.code, "cad_session_retired")

    def test_release_keeps_a_failed_session_for_the_same_owner_to_retry(self):
        self.backend._app_for_path(self.document)
        session = self.sessions[0]
        close = session.process.close
        original_close = close.side_effect
        attempts = 0

        def close_once_failed():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise EnvironmentError_("cad_process_cleanup_failed", "still running")
            original_close()

        session.process.close.side_effect = close_once_failed
        with self.assertRaises(EnvironmentError_) as caught:
            self.backend.release()
        self.assertEqual(caught.exception.code, "cad_process_cleanup_failed")
        self.assertIs(self.backend._sessions["source"], session)
        self.assertIs(self.backend._owner_thread, threading.current_thread())

        self.backend.release()
        self.assertNotIn("source", self.backend._sessions)
        self.assertIsNone(self.backend._owner_thread)
        self.assertEqual(attempts, 2)
