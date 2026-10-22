"""Hermetic regression: capture rebuild must refresh the model-document handle.

Self-contained (no Windows, no COM): the doubles below mimic the observed
late-bound behaviour where ``IModelDocExtension.Rebuild`` revokes the document
automation handle, while the same owned session can re-resolve it by path.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.sources.solidworks.native import SolidWorksBackend, normalize_document_path

PATH = r"C:\capture\prepared\robot.SLDASM"
OTHER = r"C:\capture\prepared\other.SLDASM"
CAPTURE_ROOT = normalize_document_path(os.path.abspath(PATH)).rsplit("\\", 2)[0]


class _Configuration:
    def __init__(self, name="默认", needs_rebuild=False):
        self.Name = name
        self.NeedsRebuild = needs_rebuild


class _ConfigurationManager:
    def __init__(self, name="默认", needs_rebuild=False):
        self.ActiveConfiguration = _Configuration(name, needs_rebuild)


class _Extension:
    def __init__(self, doc):
        self._doc = doc

    @property
    def NeedsRebuild2(self):
        return self._doc.needs_rebuild2

    def Rebuild(self, options):
        self._doc.rebuild_calls += 1
        if self._doc._rebuild_exception is not None:
            raise self._doc._rebuild_exception
        if self._doc._revoke:
            self._doc._revoked = True
        elif self._doc._rebuild_clears:
            self._doc.needs_rebuild2 = 0
        if self._doc._rebuild_marks_dirty:
            self._doc._save_flag = True
        return self._doc._rebuild_result


class _Doc:
    """Mimics the observed COM behaviour: Rebuild revokes later members."""

    def __init__(
        self,
        path,
        *,
        revoke=False,
        configuration="默认",
        needs_rebuild2=0,
        configuration_needs_rebuild=False,
        read_only=True,
        rebuild_clears=True,
        rebuild_result=True,
        rebuild_exception=None,
        rebuild_marks_dirty=False,
    ):
        self._path = path
        self._revoked = False
        self._configuration = configuration
        self._configuration_needs_rebuild = configuration_needs_rebuild
        self._read_only = read_only
        self._revoke = revoke
        self._rebuild_clears = rebuild_clears
        self._rebuild_result = rebuild_result
        self._rebuild_exception = rebuild_exception
        self._rebuild_marks_dirty = rebuild_marks_dirty
        self._save_flag = False
        self.needs_rebuild2 = needs_rebuild2
        self.rebuild_calls = 0
        self.force_rebuild_calls = 0
        self.save_calls = 0

    @property
    def Extension(self):
        return _Extension(self)

    @property
    def GetPathName(self):
        if self._revoked:
            raise AttributeError("<unknown>.GetPathName")
        return self._path

    def GetSaveFlag(self):
        return self._save_flag

    def Save(self):
        self.save_calls += 1
        return True

    @property
    def IsOpenedReadOnly(self):
        return self._read_only

    @property
    def ConfigurationManager(self):
        return _ConfigurationManager(
            self._configuration,
            self._configuration_needs_rebuild,
        )

    def ForceRebuild3(self, flag):
        self.force_rebuild_calls += 1
        raise AssertionError("capture rebuild must use IModelDocExtension.Rebuild")


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


class _UnreadablePathDoc(_Doc):
    @property
    def GetPathName(self):
        raise RuntimeError("path unreadable")


class _UnreadableConfigurationDoc(_Doc):
    @property
    def ConfigurationManager(self):
        raise RuntimeError("configuration manager unreadable")


class _RaisingRebuildDoc(_Doc):
    def __init__(self, *args, **kwargs):
        super().__init__(
            *args,
            rebuild_exception=RuntimeError("rebuild native failure"),
            **kwargs,
        )


class RebuildDocumentLifecycleTests(unittest.TestCase):
    def test_rebuild_refreshes_the_document_handle_in_the_owned_session(self):
        refreshed = _Doc(PATH)
        backend = _Backend(lambda: refreshed)
        doc = _Doc(PATH, revoke=True, needs_rebuild2=1)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ):
            refreshed_doc, preparation = backend._rebuild_capture_copy(doc, PATH)
        self.assertIs(refreshed_doc, refreshed)
        self.assertIsInstance(preparation, dict)
        self.assertEqual(doc.rebuild_calls, 1)
        self.assertEqual(doc.force_rebuild_calls, 0)
        self.assertEqual(backend.refresh_calls, [PATH])
        self.assertEqual(preparation["document"], PATH)
        self.assertEqual(preparation["configuration"], "默认")
        self.assertIs(preparation["read_only"], True)
        self.assertIs(preparation["rebuild_performed"], True)
        self.assertEqual(preparation["needs_rebuild2_before"], 1)
        self.assertEqual(preparation["needs_rebuild2_after"], 0)
        self.assertEqual(
            preparation["used_api"],
            "IModelDocExtension.NeedsRebuild2/"
            "IConfiguration.NeedsRebuild/"
            "IModelDocExtension.Rebuild(swForceRebuildAll=2)",
        )
        self.assertIs(preparation["save_flag_after"], False)

    def test_missing_document_after_rebuild_fails_closed_without_reopen(self):
        backend = _Backend(CadError("document_not_open", "gone"))
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(
                _Doc(PATH, revoke=True, needs_rebuild2=1),
                PATH,
            )
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

    def test_unreadable_pre_path_preserves_capture_evidence(self):
        backend = _Backend(lambda: _Doc(PATH))
        with self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(_UnreadablePathDoc(PATH), PATH)
        self.assertEqual(caught.exception.code, "cad_document_identity")
        self.assertEqual(caught.exception.detail["path"], PATH)
        self.assertEqual(caught.exception.detail["phase"], "before")
        self.assertIn("state", caught.exception.detail)
        self.assertIn("RuntimeError: path unreadable", caught.exception.detail["error"])

    def test_unreadable_pre_configuration_preserves_capture_evidence(self):
        backend = _Backend(lambda: _Doc(PATH))
        with self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(_UnreadableConfigurationDoc(PATH), PATH)
        self.assertEqual(caught.exception.code, "cad_configuration_mismatch")
        self.assertEqual(caught.exception.detail["path"], PATH)
        self.assertEqual(caught.exception.detail["phase"], "before")
        self.assertIn("state", caught.exception.detail)
        self.assertIn(
            "RuntimeError: configuration manager unreadable",
            caught.exception.detail["error"],
        )

    def test_configuration_change_across_rebuild_is_rejected(self):
        backend = _Backend(lambda: _Doc(PATH, configuration="Other"))
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(
                _Doc(PATH, needs_rebuild2=1),
                PATH,
            )
        self.assertEqual(caught.exception.code, "cad_configuration_mismatch")
        self.assertEqual(backend.refresh_calls, [PATH])

    def test_unreadable_post_configuration_preserves_state_and_phase(self):
        refreshed = _UnreadableConfigurationDoc(PATH)
        backend = _Backend(lambda: refreshed)
        doc = _Doc(PATH, needs_rebuild2=1)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_configuration_mismatch")
        self.assertEqual(caught.exception.detail["path"], PATH)
        self.assertEqual(caught.exception.detail["phase"], "after")
        self.assertIn("state", caught.exception.detail)
        self.assertIn(
            "RuntimeError: configuration manager unreadable",
            caught.exception.detail["error"],
        )
        self.assertEqual(backend.refresh_calls, [PATH])

    def test_unreadable_post_path_preserves_state_and_phase(self):
        refreshed = _UnreadablePathDoc(PATH)
        backend = _Backend(lambda: refreshed)
        doc = _Doc(PATH, needs_rebuild2=1)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_document_identity")
        self.assertEqual(caught.exception.detail["path"], PATH)
        self.assertEqual(caught.exception.detail["phase"], "after")
        self.assertIn("state", caught.exception.detail)
        self.assertIn("RuntimeError: path unreadable", caught.exception.detail["error"])
        self.assertEqual(backend.refresh_calls, [PATH])

    def test_clean_saved_state_skips_rebuild_and_preserves_source(self):
        backend = _Backend(lambda: self.fail("clean state must not refresh the handle"))
        doc = _Doc(PATH, needs_rebuild2=0)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="b" * 64,
        ) as digest:
            prepared, evidence = backend._rebuild_capture_copy(doc, PATH)
        self.assertIs(prepared, doc)
        self.assertFalse(evidence["rebuild_performed"])
        self.assertEqual(evidence["needs_rebuild2_before"], 0)
        self.assertEqual(evidence["needs_rebuild2_after"], 0)
        self.assertEqual(evidence["source_sha256_before"], "b" * 64)
        self.assertEqual(evidence["source_sha256_after"], "b" * 64)
        self.assertEqual(digest.call_count, 2)
        self.assertEqual(doc.rebuild_calls, 0)
        self.assertEqual(backend.refresh_calls, [])

    def test_nonzero_rebuild_state_does_not_skip_the_rebuild(self):
        backend = _Backend(lambda: _Doc(PATH))
        doc = _Doc(PATH, needs_rebuild2=2)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="c" * 64,
        ):
            _, evidence = backend._rebuild_capture_copy(doc, PATH)
        self.assertTrue(evidence["rebuild_performed"])
        self.assertEqual(evidence["needs_rebuild2_before"], 2)
        self.assertEqual(evidence["needs_rebuild2_after"], 0)
        self.assertEqual(doc.rebuild_calls, 1)
        self.assertEqual(backend.refresh_calls, [PATH])

    def test_unknown_rebuild_state_is_rejected_before_mutation(self):
        backend = _Backend(lambda: _Doc(PATH))
        doc = _Doc(PATH, needs_rebuild2=None)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="d" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_state_unreadable")
        self.assertEqual(doc.rebuild_calls, 0)
        self.assertEqual(backend.refresh_calls, [])

    def test_capture_copy_must_be_explicitly_read_only(self):
        backend = _Backend(lambda: _Doc(PATH))
        doc = _Doc(PATH, needs_rebuild2=0, read_only=False)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="e" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_capture_not_read_only")
        self.assertEqual(doc.rebuild_calls, 0)

    def test_clean_document_still_requires_a_rebuilt_configuration(self):
        backend = _Backend(lambda: _Doc(PATH))
        doc = _Doc(
            PATH,
            needs_rebuild2=0,
            configuration_needs_rebuild=True,
        )
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="e" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_incomplete")
        self.assertEqual(doc.rebuild_calls, 0)

    def test_unreadable_source_digest_blocks_capture(self):
        backend = _Backend(lambda: _Doc(PATH))
        doc = _Doc(PATH, needs_rebuild2=0)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            side_effect=OSError("unreadable"),
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_capture_source_unreadable")
        self.assertEqual(doc.rebuild_calls, 0)

    def test_rebuild_must_end_fully_rebuilt(self):
        backend = _Backend(
            lambda: _Doc(
                PATH,
                needs_rebuild2=1,
                rebuild_clears=False,
            )
        )
        doc = _Doc(
            PATH,
            needs_rebuild2=1,
            rebuild_clears=False,
        )
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="f" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_incomplete")
        self.assertEqual(doc.rebuild_calls, 1)
        self.assertEqual(doc.force_rebuild_calls, 0)
        self.assertEqual(backend.refresh_calls, [PATH])

    def test_rebuild_can_dirty_memory_without_saving_source(self):
        refreshed = _Doc(PATH)
        refreshed._save_flag = True
        backend = _Backend(lambda: refreshed)
        doc = _Doc(
            PATH,
            needs_rebuild2=1,
            rebuild_marks_dirty=True,
        )
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="f" * 64,
        ):
            _, preparation = backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(doc.rebuild_calls, 1)
        self.assertEqual(doc.save_calls, 0)
        self.assertIs(preparation["save_flag_before"], False)
        self.assertIs(preparation["save_flag_after"], True)
        self.assertEqual(preparation["source_sha256_before"], "f" * 64)
        self.assertEqual(preparation["source_sha256_after"], "f" * 64)

    def test_rebuild_must_end_read_only(self):
        backend = _Backend(lambda: _Doc(PATH, read_only=False))
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="f" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(_Doc(PATH, needs_rebuild2=1), PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_incomplete")
        self.assertEqual(caught.exception.detail["state"]["read_only"], False)

    def test_rebuild_must_end_with_a_clean_configuration(self):
        backend = _Backend(
            lambda: _Doc(
                PATH,
                configuration_needs_rebuild=True,
            )
        )
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="f" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(_Doc(PATH, needs_rebuild2=1), PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_incomplete")
        self.assertIs(
            caught.exception.detail["state"]["configuration_needs_rebuild"],
            True,
        )

    def test_native_rebuild_exception_preserves_phase_and_state(self):
        backend = _Backend(lambda: _Doc(PATH))
        doc = _RaisingRebuildDoc(PATH, needs_rebuild2=1)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="f" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_failed")
        self.assertEqual(caught.exception.detail["path"], PATH)
        self.assertEqual(caught.exception.detail["phase"], "rebuild")
        self.assertIn("state", caught.exception.detail)
        self.assertIn("RuntimeError: rebuild native failure", caught.exception.detail["error"])
        self.assertEqual(backend.refresh_calls, [])

    def test_native_refresh_exception_preserves_phase_and_error(self):
        backend = _Backend(RuntimeError("document lookup failed"))
        doc = _Doc(PATH, needs_rebuild2=1)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="f" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_capture_refresh_failed")
        self.assertEqual(caught.exception.detail["path"], PATH)
        self.assertEqual(caught.exception.detail["phase"], "refresh")
        self.assertIn(
            "RuntimeError: document lookup failed",
            caught.exception.detail["error"],
        )
        self.assertEqual(backend.refresh_calls, [PATH])

    def test_rebuild_cannot_change_source_bytes(self):
        backend = _Backend(lambda: _Doc(PATH))
        doc = _Doc(PATH, needs_rebuild2=1)
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            side_effect=("1" * 64, "2" * 64),
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(doc, PATH)
        self.assertEqual(caught.exception.code, "cad_capture_source_changed")


if __name__ == "__main__":
    unittest.main()
