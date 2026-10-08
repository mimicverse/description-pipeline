"""A captured copy must never overlap or revive its retired source process."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from . import _paths  # noqa: F401
from description_pipeline.sources.solidworks.errors import EnvironmentError_  # noqa: E402
from description_pipeline.sources.solidworks.isolation import CadSession  # noqa: E402
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    SolidWorksBackend,
    normalize_document_path,
)


class SequentialSessionTests(unittest.TestCase):
    def setUp(self):
        self.sessions = []
        self.binders = []
        self.events = []
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.source = str(Path(self.root.name) / "original" / "robot.SLDASM")
        self.copy_root = str(Path(self.root.name) / "copy")
        self.copy = str(Path(self.copy_root) / "robot.SLDASM")

        def factory():
            index = len(self.sessions)
            if index:
                self.assertFalse(self.sessions[0].process.alive(), "copy launched before source retirement")
            self.events.append(("launch", index))
            process = SimpleNamespace(pid=100 + index, executable="test.exe", running=True)

            def close():
                process.running = False
                self.events.append(("close", index))

            process.close = Mock(side_effect=close)
            process.alive = lambda: process.running
            app = SimpleNamespace(
                StartupProcessCompleted=True,
                RevisionNumber=lambda: f"revision-{index}",
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

    def source_then_copy(self):
        source_app = self.backend._app_for_path(self.source)
        self.backend._capture_roots.append(normalize_document_path(self.copy_root))
        copy_app = self.backend._app_for_path(self.copy)
        return source_app, copy_app

    def test_source_job_closes_before_copy_process_is_created(self):
        self.source_then_copy()
        self.assertEqual(self.events, [("launch", 0), ("close", 0), ("launch", 1)])
        self.assertIsNone(self.sessions[0].app)
        self.assertFalse(self.sessions[0].process.alive())
        self.assertTrue(self.sessions[1].process.alive())

    def test_path_selection_restores_absent_thread_local_role(self):
        self.backend._app_for_path(self.source)
        self.assertFalse(hasattr(self.backend._local, "role"))

    def test_retired_source_path_cannot_bind_or_start_another_process(self):
        self.source_then_copy()
        before = [binder.call_count for binder in self.binders]
        with self.assertRaises(EnvironmentError_) as caught:
            self.backend._app_for_path(self.source)
        self.assertEqual(caught.exception.code, "cad_session_retired")
        self.assertEqual(len(self.sessions), 2)
        self.assertEqual([binder.call_count for binder in self.binders], before)

    def test_pathless_environment_uses_live_copy_and_retains_both_identities(self):
        _, copy_app = self.source_then_copy()
        source_calls = self.binders[0].call_count
        self.assertIs(self.backend._app_obj(), copy_app)
        environment = self.backend.environment()
        self.assertEqual(environment["revision"], "revision-1")
        self.assertEqual(environment["sessions"]["source"]["pid"], 100)
        self.assertEqual(environment["sessions"]["source"]["state"], "closed")
        self.assertEqual(environment["sessions"]["copy"]["state"], "active")
        self.assertEqual(self.binders[0].call_count, source_calls)
        self.assertEqual(len(self.sessions), 2)

    def test_closed_session_cannot_reconnect_and_close_is_idempotent(self):
        self.backend._app_for_path(self.source)
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

    def test_retirement_failure_blocks_copy_creation(self):
        self.backend._app_for_path(self.source)
        self.sessions[0].process.close.side_effect = EnvironmentError_("cad_process_cleanup_failed", "still running")
        self.backend._capture_roots.append(normalize_document_path(self.copy_root))
        try:
            with self.assertRaises(EnvironmentError_) as caught:
                self.backend._app_for_path(self.copy)
            self.assertEqual(caught.exception.code, "cad_process_cleanup_failed")
            self.assertEqual(len(self.sessions), 1)
        finally:
            self.sessions[0].process.close.side_effect = None

    def test_reference_rewrite_reads_saved_bytes_after_source_retirement(self):
        source = Path(self.source)
        source.parent.mkdir()
        source.write_bytes(b"saved assembly")
        reference = source.parent / "part.SLDPRT"
        reference.write_bytes(b"saved part")
        source_app = self.backend._app_for_path(self.source)
        source_app.GetDocumentDependencies2 = lambda document, *_: (
            ["part", str(reference)] if document == self.source else []
        )
        real_factory = self.backend._session_factory

        def factory():
            session = real_factory()
            app = self.binders[-1].return_value

            def rewrite(document, old, replacement):
                self.assertFalse(self.sessions[0].process.alive())
                self.assertEqual(Path(document).read_bytes(), b"saved assembly")
                self.assertEqual(Path(replacement).read_bytes(), b"saved part")
                self.assertEqual(old, str(reference))
                return True

            app.ReplaceReferencedDocument = rewrite
            return session

        with patch.object(self.backend, "_session_factory", factory):
            result = self.backend.collect_dependencies(self.source, self.copy_root)
        self.assertEqual(result["reference_edges"], 1)
        self.assertEqual(Path(result["top_level"]).read_bytes(), b"saved assembly")
        self.assertEqual(result["mapping"][self.source], os.path.join(self.copy_root, source.name))
