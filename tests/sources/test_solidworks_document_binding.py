"""Document reads bind the published common and domain interfaces (no fallback)."""

import sys
import types
import unittest
from unittest.mock import Mock, patch

from description_pipeline.sources.solidworks import native
from description_pipeline.sources.solidworks.native import (
    IASSEMBLYDOC_IID,
    IMODELDOC2_IID,
    IPARTDOC_IID,
    SolidWorksBackend,
    _assembly_components,
    _component_document,
    _modeldoc2,
    _part_bodies,
    _part_material,
    _partdoc,
)


class _Dispatch:
    """Minimal DumbDispatch stand-in: keeps the raw interface pointer."""

    def __init__(self, ole):
        self._oleobj_ = ole


class _InterfaceModules:
    """sys.modules stubs that record the interface a binding asked for."""

    def __init__(self):
        self.generic = Mock()
        self.vendor = object()
        self.generic.QueryInterface.return_value = self.vendor
        self.view = types.SimpleNamespace()
        pc = types.ModuleType("pythoncom")
        pc.IID_IDispatch = "IDispatch"
        wt = types.ModuleType("pywintypes")
        wt.IID = lambda value: value
        self.dynamic = types.ModuleType("win32com.client.dynamic")
        self.dynamic.DumbDispatch = Mock(return_value=self.view)
        client = types.ModuleType("win32com.client")
        client.dynamic = self.dynamic
        wc = types.ModuleType("win32com")
        wc.client = client
        self.modules = {
            "pythoncom": pc,
            "pywintypes": wt,
            "win32com": wc,
            "win32com.client": client,
            "win32com.client.dynamic": self.dynamic,
        }

    def raw(self):
        return types.SimpleNamespace(_oleobj_=self.generic)

    def assert_bound_once(self, iid):
        self.generic.QueryInterface.assert_called_once_with(iid, "IDispatch")
        self.dynamic.DumbDispatch.assert_called_once_with(self.vendor)


class DocumentInterfaceBindingTests(unittest.TestCase):
    def setUp(self):
        self.stubs = _InterfaceModules()

    def test_common_document_reads_bind_imodeldoc2(self):
        with patch.dict(sys.modules, self.stubs.modules):
            view = _modeldoc2(self.stubs.raw())
        self.assertIs(view, self.stubs.view)
        self.stubs.assert_bound_once(IMODELDOC2_IID)

    def test_part_bodies_bind_ipartdoc_with_the_documented_signature(self):
        self.stubs.view.GetBodies2 = Mock(spec=lambda body_type, include_surfaces: None, return_value=("body-1",))
        with patch.dict(sys.modules, self.stubs.modules):
            bodies = _part_bodies(self.stubs.raw())
        self.assertEqual(bodies, ["body-1"])
        self.stubs.assert_bound_once(IPARTDOC_IID)
        self.stubs.view.GetBodies2.assert_called_once_with(0, False)

    def test_part_material_binds_ipartdoc_and_keeps_the_physical_api(self):
        captured = {}

        def fake_read_material(obj, method, configuration):
            captured.update(obj=obj, method=method, configuration=configuration)
            return {"name": "Steel", "database": "SolidWorks Materials"}

        with patch.dict(sys.modules, self.stubs.modules), patch.object(native, "_read_material", fake_read_material):
            material = _part_material(self.stubs.raw(), "Default")
        self.assertEqual(material["name"], "Steel")
        self.stubs.assert_bound_once(IPARTDOC_IID)
        self.assertIs(captured["obj"], self.stubs.view)
        self.assertEqual(captured["method"], "GetMaterialPropertyName2")
        self.assertEqual(captured["configuration"], "Default")

    def test_assembly_traversal_binds_iassemblydoc(self):
        self.stubs.view.GetComponents = Mock(spec=lambda flag: None, return_value=("component-1",))
        with patch.dict(sys.modules, self.stubs.modules):
            components = _assembly_components(self.stubs.raw())
        self.assertEqual(components, ["component-1"])
        self.stubs.assert_bound_once(IASSEMBLYDOC_IID)
        self.stubs.view.GetComponents.assert_called_once_with(False)

    def test_domain_binding_failures_are_fail_closed(self):
        error = RuntimeError("IPartDoc interface unavailable")
        self.stubs.generic.QueryInterface.side_effect = error
        with patch.dict(sys.modules, self.stubs.modules), self.assertRaises(RuntimeError) as caught:
            _partdoc(self.stubs.raw())
        self.assertIs(caught.exception, error)
        self.stubs.dynamic.DumbDispatch.assert_not_called()

    def test_component_document_binds_imodeldoc2_at_acquisition(self):
        raw_document = types.SimpleNamespace(_oleobj_=self.stubs.generic)
        component = types.SimpleNamespace(
            _FlagAsMethod=Mock(),
            GetModelDoc2=Mock(spec=lambda: None, return_value=raw_document),
        )
        self.stubs.dynamic.DumbDispatch.side_effect = _Dispatch
        with patch.dict(sys.modules, self.stubs.modules):
            document = _component_document(component)
        component._FlagAsMethod.assert_called_once_with("GetModelDoc2")
        self.stubs.generic.QueryInterface.assert_called_once_with(IMODELDOC2_IID, "IDispatch")
        self.assertIsInstance(document, _Dispatch)
        self.assertIs(document._oleobj_, self.stubs.vendor)
        self.assertIsNone(
            _component_document(
                types.SimpleNamespace(_FlagAsMethod=Mock(), GetModelDoc2=Mock(spec=lambda: None, return_value=None))
            )
        )

    def test_non_com_values_and_none_keep_identity(self):
        value = object()
        self.assertIs(_modeldoc2(value), value)
        self.assertIs(_modeldoc2(None), None)
        self.assertIs(_partdoc(value), value)


class DocumentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stubs = _InterfaceModules()
        self.path = "C:/captures/robot.SLDASM"
        self.stubs.view.GetPathName = Mock(spec=lambda: None, return_value=self.path)
        self.stubs.view.IsOpenedReadOnly = True

    def test_document_by_path_binds_the_imodeldoc2_interface(self):
        raw = self.stubs.raw()
        self.stubs.dynamic.DumbDispatch.return_value = raw
        view = types.SimpleNamespace(GetPathName=Mock(spec=lambda: None, return_value=self.path), IsOpenedReadOnly=True)
        backend = SolidWorksBackend()
        backend._app_for_path = lambda path: types.SimpleNamespace(
            GetOpenDocumentByName=Mock(spec=lambda path: None, return_value=raw)
        )
        with (
            patch.dict(sys.modules, self.stubs.modules),
            patch.object(native, "_modeldoc2", return_value=view) as binder,
        ):
            document = backend._document_by_path(self.path)
        binder.assert_called_once_with(raw)
        self.assertIs(document, view)

    def test_mass_properties_document_reads_bodies_through_the_part_helper(self):
        doc = types.SimpleNamespace(GetType=Mock(spec=lambda: None, return_value=1))
        backend = SolidWorksBackend()
        with (
            patch.object(native, "_part_bodies", return_value=[]) as helper,
            self.assertRaises(native.CadError) as caught,
        ):
            backend._mass_properties_document(doc)
        self.assertEqual(caught.exception.code, "cad_empty_model")
        helper.assert_called_once_with(doc)

    def test_only_presence_checks_read_getmodeldoc2_directly(self):
        import ast
        import pathlib

        source = pathlib.Path(native.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        raw_sites = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_method"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "GetModelDoc2"
        ]
        # Exactly three raw calls: one inside _component_document itself and
        # the two documented presence-only checks. Every acquisition that is
        # read afterwards goes through _component_document (declared sites:
        # collect_scene traversal and inspect_copy).
        self.assertEqual(len(raw_sites), 3, raw_sites)
        self.assertEqual(source.count("_component_document("), 3, "definition + two acquisition sites")


if __name__ == "__main__":
    unittest.main()
