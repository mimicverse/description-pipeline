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
LEFT = r"C:\capture\prepared\left.SLDASM"
RIGHT = r"C:\capture\prepared\right.SLDASM"
CHILD = r"C:\capture\prepared\child.SLDASM"
GRANDCHILD = r"C:\capture\prepared\grandchild.SLDASM"
VIRTUAL_ASSEMBLY = "/tmp/swx1234/VC~~/robot/virtual_assembly.SLDASM"
OUTSIDE_ASSEMBLY = r"C:\outside\foreign.SLDASM"
OUTSIDE_PART = r"C:\outside\foreign.SLDPRT"
CAPTURE_ROOT = normalize_document_path(os.path.abspath(PATH)).rsplit("\\", 2)[0]


class _Configuration:
    def __init__(self, doc, name="默认", needs_rebuild=False):
        self._doc = doc
        self.Name = name
        self.NeedsRebuild = needs_rebuild
        self.AddRebuildSaveMark = False

    def GetRootComponent3(self, _configuration):
        return self._doc._root


class _ConfigurationManager:
    def __init__(self, doc):
        self._doc = doc
        self.ActiveConfiguration = _Configuration(doc, doc._configuration, doc._configuration_needs_rebuild)


class _RootComponent:
    def __init__(self, children):
        self._children = list(children)

    def GetChildren(self):
        return list(self._children)


class _Component:
    def __init__(
        self,
        name,
        document,
        *,
        suppression_state=2,
        children=(),
        referenced_configuration=None,
        is_virtual=False,
        imported_path=None,
    ):
        self.Name2 = name
        self.ReferencedConfiguration = (
            document._configuration
            if referenced_configuration is None
            else referenced_configuration
        )
        self._document = document
        self._suppression_state = suppression_state
        self._children = list(children) if children else list(document._children)
        self.IsVirtual = is_virtual
        self._imported_path = imported_path

    def GetModelDoc2(self):
        return self._document

    def GetChildren(self):
        return list(self._children)

    def GetSuppression2(self):
        return self._suppression_state

    def GetImportedPath(self):
        return self._imported_path


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
        if self._doc._rebuild_configuration is not None:
            self._doc._configuration = self._doc._rebuild_configuration
        if self._doc._rebuild_callback is not None:
            self._doc._rebuild_callback()
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
        children=(),
        document_type=2,
        rebuild_configuration=None,
        rebuild_callback=None,
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
        self._children = list(children)
        self._root = _RootComponent(self._children)
        self._document_type = document_type
        self.EnableAssemblyRebuild = False
        self._rebuild_configuration = rebuild_configuration
        self._rebuild_callback = rebuild_callback
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
        return _ConfigurationManager(self)

    def GetConfigurationNames(self):
        return [self._configuration]

    def GetConfigurationByName(self, name):
        if name != self._configuration:
            raise ValueError(f"unknown configuration: {name}")
        return self.ConfigurationManager.ActiveConfiguration

    def GetType(self):
        return self._document_type

    def GetLightWeightComponentCount(self):
        return 0

    def ForceRebuild3(self, flag):
        self.force_rebuild_calls += 1
        raise AssertionError("capture rebuild must use IModelDocExtension.Rebuild")


class _Backend(SolidWorksBackend):
    def __init__(self, refresh):
        self._capture_roots = [CAPTURE_ROOT]
        self._refresh = refresh
        self.refresh_calls = []

    def _document_handle_by_path(self, path):
        self.refresh_calls.append(str(path))
        if isinstance(self._refresh, Exception):
            raise self._refresh
        if isinstance(self._refresh, dict):
            try:
                value = self._refresh[str(path)]
            except KeyError as error:
                raise CadError("document_not_open", "missing test document") from error
            return value.pop(0) if isinstance(value, list) else value
        return self._refresh() if callable(self._refresh) else self._refresh

    def _document_by_path(self, path):
        return self._document_handle_by_path(path)

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
        self.assertEqual(refreshed.save_calls, 0)
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

    def test_pending_children_rebuild_once_before_top(self):
        left = _Doc(LEFT, needs_rebuild2=1)
        right = _Doc(RIGHT, needs_rebuild2=1)
        top = _Doc(
            PATH,
            needs_rebuild2=1,
            children=[_Component("left-1", left), _Component("right-1", right)],
        )
        backend = _Backend({PATH: top, LEFT: left, RIGHT: right})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ):
            _, preparation = backend._rebuild_capture_copy(top, PATH)
        self.assertEqual([row["path"] for row in preparation["child_rebuilds"]], [LEFT, RIGHT])
        self.assertEqual([left.rebuild_calls, right.rebuild_calls, top.rebuild_calls], [1, 1, 1])
        self.assertIsNotNone(preparation["top_rebuild"])
        self.assertEqual(preparation["needs_rebuild2_after"], 0)
        self.assertIs(preparation["state_after"]["read_only"], True)
        self.assertFalse(preparation["state_after"]["controls"]["enable_assembly_rebuild"])

    def test_deepest_child_rebuilds_before_its_parent(self):
        grandchild = _Doc(GRANDCHILD, needs_rebuild2=1)
        child = _Doc(
            CHILD,
            needs_rebuild2=1,
            children=[_Component("grandchild-1", grandchild)],
        )
        top = _Doc(PATH, needs_rebuild2=1, children=[_Component("child-1", child)])
        backend = _Backend({PATH: top, CHILD: child, GRANDCHILD: grandchild})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="b" * 64,
        ):
            _, preparation = backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(
            [row["path"] for row in preparation["child_rebuilds"]],
            [GRANDCHILD, CHILD],
        )
        self.assertEqual([grandchild.rebuild_calls, child.rebuild_calls, top.rebuild_calls], [1, 1, 1])

    def test_repeated_child_document_instance_rebuilds_once(self):
        child = _Doc(CHILD, needs_rebuild2=1)
        top = _Doc(
            PATH,
            children=[_Component("child-1", child), _Component("child-2", child)],
        )
        backend = _Backend({PATH: top, CHILD: child})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="c" * 64,
        ):
            _, preparation = backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(child.rebuild_calls, 1)
        self.assertEqual(top.rebuild_calls, 0)
        self.assertEqual(preparation["child_rebuilds"][0]["instances"], ["child-1", "child-2"])

    def test_child_that_remains_pending_blocks_without_top_retry(self):
        child = _Doc(CHILD, needs_rebuild2=1, rebuild_clears=False)
        top = _Doc(PATH, needs_rebuild2=1, children=[_Component("child-1", child)])
        backend = _Backend({PATH: top, CHILD: child})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="d" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_incomplete")
        self.assertEqual(caught.exception.detail["phase"], "child")
        self.assertEqual(child.rebuild_calls, 1)
        self.assertEqual(top.rebuild_calls, 0)

    def test_suppressed_and_clean_dependencies_are_not_rebuilt(self):
        suppressed = _Doc(LEFT, needs_rebuild2=1)
        clean = _Doc(RIGHT)
        top = _Doc(
            PATH,
            children=[
                _Component("suppressed-1", suppressed, suppression_state=0),
                _Component("clean-1", clean),
            ],
        )
        backend = _Backend({PATH: top, LEFT: suppressed, RIGHT: clean})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="e" * 64,
        ):
            _, preparation = backend._rebuild_capture_copy(top, PATH)
        self.assertEqual([suppressed.rebuild_calls, clean.rebuild_calls, top.rebuild_calls], [0, 0, 0])
        self.assertEqual(preparation["child_rebuilds"], [])
        self.assertIsNone(preparation["top_rebuild"])

    def test_conflicting_configuration_for_one_child_document_blocks_before_mutation(self):
        child = _Doc(CHILD, needs_rebuild2=1, configuration="默认")
        top = _Doc(
            PATH,
            children=[
                _Component("child-1", child, referenced_configuration="默认"),
                _Component("child-2", child, referenced_configuration="Other"),
            ],
        )
        backend = _Backend({PATH: top, CHILD: child})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="f" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_state_unreadable")
        self.assertEqual([child.rebuild_calls, top.rebuild_calls], [0, 0])

    def test_unknown_child_state_blocks_before_any_rebuild(self):
        child = _Doc(CHILD, needs_rebuild2=True)
        top = _Doc(PATH, needs_rebuild2=1, children=[_Component("child-1", child)])
        backend = _Backend({PATH: top, CHILD: child})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_state_unreadable")
        self.assertEqual([child.rebuild_calls, top.rebuild_calls], [0, 0])

    def test_child_configuration_change_is_rejected_before_top_rebuild(self):
        child = _Doc(CHILD, needs_rebuild2=1, rebuild_configuration="Other")
        top = _Doc(PATH, needs_rebuild2=1, children=[_Component("child-1", child)])
        backend = _Backend({PATH: top, CHILD: child})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="b" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(caught.exception.code, "cad_configuration_mismatch")
        self.assertEqual([child.rebuild_calls, top.rebuild_calls], [1, 0])

    def test_child_source_change_after_rebuild_is_rejected(self):
        child = _Doc(CHILD, needs_rebuild2=1)
        top = _Doc(PATH, children=[_Component("child-1", child)])
        backend = _Backend({PATH: top, CHILD: child})
        child_hash_calls = 0

        def source_hash(path):
            nonlocal child_hash_calls
            if path != CHILD:
                return "c" * 64
            child_hash_calls += 1
            return "c" * 64 if child_hash_calls == 1 else "d" * 64

        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            side_effect=source_hash,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(caught.exception.code, "cad_capture_source_changed")
        self.assertEqual([child.rebuild_calls, top.rebuild_calls], [1, 0])

    def test_top_rebuild_that_invalidates_a_child_blocks_without_another_rebuild(self):
        child = _Doc(CHILD, needs_rebuild2=1)
        top = _Doc(PATH, needs_rebuild2=1, children=[_Component("child-1", child)])
        top._rebuild_callback = lambda: setattr(child, "needs_rebuild2", 1)
        backend = _Backend({PATH: top, CHILD: child})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="e" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_incomplete")
        self.assertEqual(caught.exception.detail["path"], CHILD)
        self.assertEqual([child.rebuild_calls, top.rebuild_calls], [1, 1])

    def test_sibling_rebuild_can_clear_a_later_child_before_its_turn(self):
        left = _Doc(LEFT, needs_rebuild2=1)
        right = _Doc(RIGHT, needs_rebuild2=1)
        top = _Doc(
            PATH,
            needs_rebuild2=1,
            children=[_Component("left-1", left), _Component("right-1", right)],
        )
        left._rebuild_callback = lambda: setattr(right, "needs_rebuild2", 0)
        backend = _Backend({PATH: top, LEFT: left, RIGHT: right})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="f" * 64,
        ):
            _, preparation = backend._rebuild_capture_copy(top, PATH)
        self.assertEqual([left.rebuild_calls, right.rebuild_calls, top.rebuild_calls], [1, 0, 1])
        self.assertEqual([row["path"] for row in preparation["child_rebuilds"]], [LEFT])

    def test_grandchild_rebuild_can_make_a_clean_parent_pending(self):
        grandchild = _Doc(GRANDCHILD, needs_rebuild2=1)
        child = _Doc(CHILD, needs_rebuild2=0, children=[_Component("grandchild-1", grandchild)])
        grandchild._rebuild_callback = lambda: setattr(child, "needs_rebuild2", 1)
        top = _Doc(PATH, needs_rebuild2=0, children=[_Component("child-1", child)])
        backend = _Backend({PATH: top, CHILD: child, GRANDCHILD: grandchild})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ):
            _, preparation = backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(
            [row["path"] for row in preparation["child_rebuilds"]],
            [GRANDCHILD, CHILD],
        )
        self.assertEqual([grandchild.rebuild_calls, child.rebuild_calls, top.rebuild_calls], [1, 1, 0])

    def test_top_rebuild_uses_the_handle_refreshed_after_children(self):
        child = _Doc(CHILD, needs_rebuild2=1)
        stale_top = _Doc(PATH, needs_rebuild2=1, children=[_Component("child-1", child)])
        refreshed_top = _Doc(PATH, needs_rebuild2=1, children=[_Component("child-1", child)])
        final_top = _Doc(PATH, children=[_Component("child-1", child)])
        backend = _Backend({PATH: [refreshed_top, final_top], CHILD: child})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="b" * 64,
        ):
            returned, preparation = backend._rebuild_capture_copy(stale_top, PATH)
        self.assertIs(returned, final_top)
        self.assertEqual([stale_top.rebuild_calls, refreshed_top.rebuild_calls, final_top.rebuild_calls], [0, 1, 0])
        self.assertEqual(preparation["needs_rebuild2_after"], 0)

    def test_non_read_only_child_assembly_is_not_reopened_or_mutated(self):
        child = _Doc(CHILD, read_only=False, needs_rebuild2=1)
        top = _Doc(PATH, read_only=True, children=[_Component("child-1", child)])
        backend = _Backend({PATH: top, CHILD: child})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ):
            _, preparation = backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(child.rebuild_calls, 1)
        self.assertIs(preparation["state_after"]["read_only"], True)
        child_state = next(
            row["state"]
            for row in preparation["assembly_states_after"]
            if row["path"] == CHILD
        )
        self.assertIs(child_state["read_only"], False)

    def test_clean_virtual_assembly_is_a_session_document_not_a_source_file(self):
        virtual = _Doc(VIRTUAL_ASSEMBLY, read_only=False)
        top = _Doc(
            PATH,
            children=[
                _Component(
                    "virtual-1",
                    virtual,
                    is_virtual=True,
                )
            ],
        )
        backend = _Backend({PATH: top, VIRTUAL_ASSEMBLY: virtual})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="b" * 64,
        ) as source_hash:
            _, preparation = backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(preparation["assembly_document_count"], 2)
        self.assertEqual(preparation["session_dependency_paths"], [VIRTUAL_ASSEMBLY])
        self.assertNotIn(
            normalize_document_path(os.path.abspath(VIRTUAL_ASSEMBLY)),
            preparation["source_files_before"],
        )
        self.assertEqual(source_hash.call_count, 2)

    def test_pending_virtual_assembly_rebuilds_as_the_owning_session_document(self):
        virtual = _Doc(VIRTUAL_ASSEMBLY, read_only=False, needs_rebuild2=1)
        top = _Doc(
            PATH,
            children=[
                _Component(
                    "virtual-1",
                    virtual,
                    is_virtual=True,
                )
            ],
        )
        backend = _Backend({PATH: top, VIRTUAL_ASSEMBLY: virtual})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="c" * 64,
        ):
            _, preparation = backend._rebuild_capture_copy(top, PATH)
        self.assertEqual([row["path"] for row in preparation["child_rebuilds"]], [VIRTUAL_ASSEMBLY])
        self.assertEqual(virtual.rebuild_calls, 1)
        self.assertEqual(top.rebuild_calls, 0)

    def test_file_backed_dependency_outside_the_copy_is_rejected(self):
        for path, document_type in (
            (OUTSIDE_ASSEMBLY, 2),
            (OUTSIDE_PART, 1),
        ):
            with self.subTest(path=path):
                outside = _Doc(path, document_type=document_type)
                top = _Doc(PATH, children=[_Component("outside-1", outside)])
                backend = _Backend({PATH: top, path: outside})
                with patch(
                    "description_pipeline.sources.solidworks.native._hash",
                    return_value="d" * 64,
                ), self.assertRaises(CadError) as caught:
                    backend._rebuild_capture_copy(top, PATH)
                self.assertEqual(caught.exception.code, "cad_rebuild_scope")
                self.assertEqual(
                    caught.exception.detail["source_provenance"]["native_document_path"],
                    path,
                )
                self.assertEqual(top.rebuild_calls, 0)

    def test_topology_addition_during_rebuild_is_rejected(self):
        child = _Doc(CHILD)
        added = _Doc(OTHER, needs_rebuild2=1)
        top = _Doc(PATH, needs_rebuild2=1, children=[_Component("child-1", child)])
        top._rebuild_callback = lambda: top._root._children.append(
            _Component("added-1", added)
        )
        backend = _Backend({PATH: top, CHILD: child, OTHER: added})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="e" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_topology_changed")
        self.assertEqual(added.rebuild_calls, 0)

    def test_topology_removal_during_rebuild_is_rejected(self):
        child = _Doc(CHILD)
        top = _Doc(PATH, needs_rebuild2=1, children=[_Component("child-1", child)])
        top._rebuild_callback = lambda: top._root._children.clear()
        backend = _Backend({PATH: top, CHILD: child})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="f" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_topology_changed")

    def test_topology_rebinding_during_rebuild_is_rejected(self):
        child = _Doc(CHILD)
        replacement = _Doc(OTHER)
        top = _Doc(PATH, needs_rebuild2=1, children=[_Component("child-1", child)])
        top._rebuild_callback = lambda: top._root._children.__setitem__(
            0,
            _Component("child-1", replacement),
        )
        backend = _Backend({PATH: top, CHILD: child, OTHER: replacement})
        with patch(
            "description_pipeline.sources.solidworks.native._hash",
            return_value="a" * 64,
        ), self.assertRaises(CadError) as caught:
            backend._rebuild_capture_copy(top, PATH)
        self.assertEqual(caught.exception.code, "cad_rebuild_topology_changed")


if __name__ == "__main__":
    unittest.main()
