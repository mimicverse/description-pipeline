"""Hermetic regression: force-rebuild must refresh the model-document handle.

Self-contained (no Windows, no COM): the doubles below mimic the observed
late-bound behaviour where ``ForceRebuild3`` revokes the document automation
handle, while the same owned session can re-resolve it by path.
"""

from __future__ import annotations

import os
import unittest

from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.sources.solidworks.native import SolidWorksBackend, normalize_document_path

PATH = r"C:\capture\prepared\robot.SLDASM"
OTHER = r"C:\capture\prepared\other.SLDASM"
CAPTURE_ROOT = normalize_document_path(os.path.abspath(PATH)).rsplit("\\", 2)[0]


class _Configuration:
    def __init__(self, name="默认"):
        self.Name = name


class _ConfigurationManager:
    def __init__(self, name="默认"):
        self.ActiveConfiguration = _Configuration(name)


class _Doc:
    """Mimics the observed COM behaviour: ForceRebuild3 revokes later members."""

    def __init__(self, path, *, revoke=False, configuration="默认"):
        self._path = path
        self._revoked = False
        self._configuration = configuration
        self._revoke = revoke
        self.rebuild_calls = 0

    @property
    def GetPathName(self):
        if self._revoked:
            raise AttributeError("<unknown>.GetPathName")
        return self._path

    @property
    def GetSaveFlag(self):
        return False

    @property
    def IsOpenedReadOnly(self):
        return True

    @property
    def ConfigurationManager(self):
        return _ConfigurationManager(self._configuration)

    def ForceRebuild3(self, flag):
        self.rebuild_calls += 1
        if self._revoke:
            self._revoked = True
        return True


class _Backend(SolidWorksBackend):
    def __init__(self, refresh):
        self._capture_roots = [CAPTURE_ROOT]
        self._refresh = refresh
        self.refresh_calls = []

    def _document_by_path(self, path):
        self.refresh_calls.append(str(path))
        if isinstance(self._refresh, Exception):
            raise self._refresh
        return self._refresh() if callable(self._refresh) else self._refresh

    def _ensure_document(self, path):
        raise AssertionError("a missing document must never be reopened")

    def open_document(self, path):
        raise AssertionError("OpenDoc6 must never be used after a rebuild")


class RebuildDocumentLifecycleTests(unittest.TestCase):
    def test_rebuild_refreshes_the_document_handle_in_the_owned_session(self):
        refreshed = _Doc(PATH)
        backend = _Backend(lambda: refreshed)
        doc = _Doc(PATH, revoke=True)
        preparation = backend._rebuild_capture_copy(doc, PATH)
        self.assertIsInstance(preparation, dict)
        self.assertEqual(doc.rebuild_calls, 1)
        self.assertEqual(backend.refresh_calls, [PATH])
        self.assertEqual(preparation["document"], PATH)
        self.assertEqual(preparation["configuration"], "默认")
        self.assertIs(preparation["read_only"], True)
        self.assertIs(preparation["save_flag_after"], False)

    def test_missing_document_after_rebuild_fails_closed_without_reopen(self):
        backend = _Backend(CadError("document_not_open", "gone"))
        with self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(_Doc(PATH, revoke=True), PATH)
        self.assertEqual(caught.exception.code, "document_not_open")
        self.assertEqual(backend.refresh_calls, [PATH])

    def test_wrong_pre_rebuild_identity_is_rejected_before_the_rebuild(self):
        backend = _Backend(lambda: _Doc(PATH))
        doc = _Doc(OTHER)
        with self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_document_identity")
        self.assertEqual(doc.rebuild_calls, 0)
        self.assertEqual(backend.refresh_calls, [])

    def test_configuration_change_across_rebuild_is_rejected(self):
        backend = _Backend(lambda: _Doc(PATH, configuration="Other"))
        with self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(_Doc(PATH), PATH)
        self.assertEqual(caught.exception.code, "cad_configuration_mismatch")
        self.assertEqual(backend.refresh_calls, [PATH])


if __name__ == "__main__":
    unittest.main()
