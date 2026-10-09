"""Document reads bind the published common and domain interfaces (no fallback)."""

import sys
import types
import unittest
from unittest.mock import Mock, patch
import ast
import pathlib
from typing import ClassVar

from description_pipeline.sources.solidworks import native
from description_pipeline.sources.solidworks.native import (
    IASSEMBLYDOC_IID,
    ICONFIGURATION_IID,
    ICONFIGURATIONMANAGER_IID,
    IMODELDOC2_IID,
    IPARTDOC_IID,
    SolidWorksBackend,
    _active_configuration,
    _active_configuration_view,
    _assembly_components,
    _component_document,
    _configuration_context,
    _configuration,
    _configurationmanager,
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

    def test_configuration_manager_and_configuration_bind_their_published_interfaces(self):
        with patch.dict(sys.modules, self.stubs.modules):
            manager = _configurationmanager(self.stubs.raw())
        self.assertIs(manager, self.stubs.view)
        self.stubs.assert_bound_once(ICONFIGURATIONMANAGER_IID)
        self.stubs.generic.reset_mock()
        self.stubs.dynamic.DumbDispatch.reset_mock()
        with patch.dict(sys.modules, self.stubs.modules):
            configuration = _configuration(self.stubs.raw())
        self.assertIs(configuration, self.stubs.view)
        self.stubs.assert_bound_once(ICONFIGURATION_IID)

    def test_configuration_binding_failures_are_fail_closed(self):
        error = RuntimeError("IConfiguration interface unavailable")
        self.stubs.generic.QueryInterface.side_effect = error
        with patch.dict(sys.modules, self.stubs.modules), self.assertRaises(RuntimeError) as caught:
            _configuration(self.stubs.raw())
        self.assertIs(caught.exception, error)
        self.stubs.dynamic.DumbDispatch.assert_not_called()
        value = object()
        self.assertIs(_configurationmanager(value), value)
        self.assertIs(_configuration(None), None)

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
        doc = types.SimpleNamespace(
            GetType=Mock(spec=lambda: None, return_value=1),
            GetPathName=Mock(spec=lambda: None, return_value="C:/captures/base.SLDPRT"),
            GetTitle=Mock(spec=lambda: None, return_value="base"),
            ConfigurationManager=types.SimpleNamespace(ActiveConfiguration=types.SimpleNamespace(Name="Default")),
        )
        backend = SolidWorksBackend()
        with (
            patch.object(native, "_part_bodies", return_value=[]) as helper,
            self.assertRaises(native.CadError) as caught,
        ):
            backend._mass_properties_document(doc)
        self.assertEqual(caught.exception.code, "cad_empty_model")
        helper.assert_called_once_with(doc)


class _Revocable:
    """Dispatch whose member access fails after revocation, recording late reads."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "revoked", False)
        object.__setattr__(self, "late", [])

    def revoke(self):
        object.__setattr__(self, "revoked", True)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if object.__getattribute__(self, "revoked"):
            object.__getattribute__(self, "late").append(name)
            raise RuntimeError(f"revoked dispatch read: {name}")
        return getattr(object.__getattribute__(self, "_inner"), name)


class _Configuration:
    def __init__(self, root):
        self.root = root

    @property
    def Name(self):
        return "Default"

    def GetRootComponent3(self, *args):
        return self.root


class _MassProperty:
    def __init__(self, *handles):
        self.handles = handles
        self.Mass = 1.5
        self.Volume = 0.001
        self.Density = 7900.0
        self.CenterOfMass = (0.0, 0.0, 0.0)

    def Recalculate(self):
        for handle in self.handles:
            handle.revoke()

    def GetMomentOfInertia(self, _mode):
        return (1e-3, 0.0, 0.0, 0.0, 1e-3, 0.0, 0.0, 0.0, 1e-3)

    def GetOverrideOptions(self):
        return types.SimpleNamespace(OverrideMass=False, OverrideCenterOfMass=False, OverrideMomentsOfInertia=False)


class _Extension:
    def __init__(self, mass_property):
        self.mass_property = mass_property

    def CreateMassProperty2(self):
        return self.mass_property


class ConfigurationPrimitiveTests(unittest.TestCase):
    def setUp(self):
        pythoncom = types.ModuleType("pythoncom")
        pythoncom.VT_ARRAY = 0x2000
        pythoncom.VT_DISPATCH = 9
        pythoncom.IID_IDispatch = "IDispatch"
        client = types.ModuleType("win32com.client")
        client.VARIANT = Mock(return_value="VARIANT")
        win32com = types.ModuleType("win32com")
        win32com.client = client
        self.modules = {"pythoncom": pythoncom, "win32com": win32com, "win32com.client": client}
        self._modules_patch = patch.dict(sys.modules, self.modules)
        self._modules_patch.start()

    def tearDown(self):
        self._modules_patch.stop()

    def _backend(self):
        child = types.SimpleNamespace(
            Name2="arm-1",
            IsSuppressed=False,
            GetChildren=Mock(spec=lambda: None, return_value=[]),
            GetPathName=Mock(spec=lambda: None, return_value="C:/captures/arm.SLDPRT"),
        )
        root = types.SimpleNamespace(GetChildren=Mock(spec=lambda: None, return_value=[child]))
        configuration = _Revocable(_Configuration(root))
        manager = types.SimpleNamespace(ActiveConfiguration=configuration)
        inner = types.SimpleNamespace(
            GetType=Mock(spec=lambda: None, return_value=2),
            ConfigurationManager=manager,
            GetPathName=Mock(spec=lambda: None, return_value="C:/captures/robot.SLDASM"),
            GetTitle=Mock(spec=lambda: None, return_value="robot"),
        )
        document = _Revocable(inner)
        mass_property = _MassProperty(document, configuration)
        inner.Extension = _Extension(mass_property)
        backend = SolidWorksBackend()
        backend._document_by_path = lambda path: document
        return backend, document, configuration

    def test_configuration_context_captures_the_name_before_stateful_use(self):
        configuration = _Revocable(_Configuration(root=None))
        document = types.SimpleNamespace(ConfigurationManager=types.SimpleNamespace(ActiveConfiguration=configuration))
        self.assertIs(_active_configuration_view(document)[1], configuration)
        self.assertEqual(_active_configuration(document), "Default")
        manager, view, name = _configuration_context(document)
        self.assertIs(manager, document.ConfigurationManager)
        self.assertIs(view, configuration)
        self.assertEqual(name, "Default")
        configuration.revoke()  # a stateful read would now fail
        with self.assertRaises(RuntimeError):
            _ = configuration.Name
        self.assertEqual(configuration.late, ["Name"])

    def test_component_context_survives_configuration_invalidation_after_recalculation(self):
        backend, document, configuration = self._backend()
        result = backend.assembly_component_mass_properties("C:/captures/robot.SLDASM")
        self.assertTrue(configuration.revoked, "the reader must have run a stateful recalculation")
        self.assertEqual(document.late, [])
        self.assertEqual(configuration.late, [])
        self.assertEqual(result["configuration"], "Default")
        self.assertEqual(result["reference"]["configuration"], "Default")
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["instances"]), 1)
        self.assertEqual(result["instances"][0]["context_mass_kg"], 1.5)
        with self.assertRaises(RuntimeError):
            _ = document.GetPathName
        self.assertEqual(document.late, ["GetPathName"])

    def test_part_mass_reader_uses_captured_primitives_after_recalculation(self):
        body = object()
        inner = types.SimpleNamespace(
            GetType=Mock(spec=lambda: None, return_value=1),
            ConfigurationManager=None,
            GetPathName=Mock(spec=lambda: None, return_value="C:/captures/base.SLDPRT"),
            GetTitle=Mock(spec=lambda: None, return_value="base"),
        )
        configuration = _Revocable(_Configuration(root=None))
        inner.ConfigurationManager = types.SimpleNamespace(ActiveConfiguration=configuration)
        document = _Revocable(inner)
        mass_property = _MassProperty(document, configuration)
        inner.Extension = _Extension(mass_property)
        backend = SolidWorksBackend()
        error = native.CadError("cad_material_read_failed", "documented table without CAD material")
        with (
            patch.object(native, "_part_bodies", return_value=[body]),
            patch.object(native, "_material_assignments_document", side_effect=error),
        ):
            result = backend._mass_properties_document(document, require_material=False)
        self.assertTrue(configuration.revoked)
        self.assertEqual(document.late, [])
        self.assertEqual(configuration.late, [])
        self.assertEqual(result["reference"]["part_document"], "C:/captures/base.SLDPRT")
        self.assertEqual(result["reference"]["configuration"], "Default")
        self.assertEqual(result["reference"]["material_assignment"]["configuration"], "Default")

    def test_unreadable_component_path_keeps_other_rows_and_records_the_error(self):
        ok_child = types.SimpleNamespace(
            Name2="ok-1",
            IsSuppressed=False,
            GetChildren=Mock(spec=lambda: None, return_value=[]),
            GetPathName=Mock(spec=lambda: None, return_value="C:/captures/ok.SLDPRT"),
        )
        bad_child = types.SimpleNamespace(
            Name2="bad-2",
            IsSuppressed=False,
            GetChildren=Mock(spec=lambda: None, return_value=[]),
            GetPathName=Mock(spec=lambda: None, side_effect=RuntimeError("RPC_S_UNKNOWN_IF")),
        )
        root = types.SimpleNamespace(GetChildren=Mock(spec=lambda: None, return_value=[ok_child, bad_child]))
        configuration = _Revocable(_Configuration(root))
        mass_property = _MassProperty(configuration)
        inner = types.SimpleNamespace(
            GetType=Mock(spec=lambda: None, return_value=2),
            ConfigurationManager=types.SimpleNamespace(ActiveConfiguration=configuration),
            Extension=_Extension(mass_property),
            GetPathName=Mock(spec=lambda: None, return_value="C:/captures/robot.SLDASM"),
            GetTitle=Mock(spec=lambda: None, return_value="robot"),
        )
        document = _Revocable(inner)
        mass_property.handles = (document, configuration)
        backend = SolidWorksBackend()
        backend._document_by_path = lambda path: document
        result = backend.assembly_component_mass_properties("C:/captures/robot.SLDASM")
        self.assertEqual([row["name"] for row in result["instances"]], ["ok-1"])
        self.assertEqual([row["name"] for row in result["errors"]], ["bad-2"])
        self.assertEqual(result["errors"][0]["stage"], "hierarchy")
        self.assertEqual(document.late, [])
        self.assertEqual(configuration.late, [])
        self.assertEqual(result["configuration"], "Default")


class ReturnViewRegistryTests(unittest.TestCase):
    """Every declared single/array return is bound to its published interface."""

    EXPECTED_SINGLE: ClassVar[dict[str, str]] = {
        "Extension": native.IMODELDOCEXTENSION_IID,
        "CreateMassProperty2": native.IMASSPROPERTY2_IID,
        "CustomPropertyManager": native.ICUSTOMPROPERTYMANAGER_IID,
        "GetCoordinateSystemTransformByName": native.IMATHTRANSFORM_IID,
        "GetOverrideOptions": native.IMASSPROPERTYOVERRIDEOPTIONS_IID,
        "FirstFeature": native.IFEATURE_IID,
        "GetNextFeature": native.IFEATURE_IID,
        "GetNextSubFeature": native.IFEATURE_IID,
        "GetFirstSubFeature": native.IFEATURE_IID,
        "GetFeature": native.IFEATURE_IID,
        "GetSpecificFeature2": native.IMATE2_IID,
        "MateEntity": native.IMATEENTITY2_IID,
        "GetSurface": native.ISURFACE_IID,
        "GetCurve": native.ICURVE_IID,
        "GetTotalTransform": native.IMATHTRANSFORM_IID,
        "OpenDoc6": native.IMODELDOC2_IID,
    }
    EXPECTED_ARRAYS: ClassVar[dict[str, str]] = {"GetBodies2": native.IBODY2_IID, "GetFaces": native.IFACE2_IID}

    def setUp(self):
        self.stubs = _InterfaceModules()

    def test_registries_cover_the_installed_tlb_map_exactly(self):
        self.assertEqual(dict(native.RETURN_VIEWS), self.EXPECTED_SINGLE)
        self.assertEqual(dict(native.RETURN_ARRAY_VIEWS), self.EXPECTED_ARRAYS)

    def test_single_return_members_bind_the_declared_interface(self):
        for member, iid in self.EXPECTED_SINGLE.items():
            with self.subTest(member=member):
                stubs = _InterfaceModules()
                returned = types.SimpleNamespace(_oleobj_=stubs.generic)
                raw = types.SimpleNamespace(**{member: returned})
                with patch.dict(sys.modules, stubs.modules):
                    view = native._member(raw, member)
                stubs.generic.QueryInterface.assert_called_once_with(iid, "IDispatch")
                self.assertIs(view, stubs.view)

    def test_array_return_members_bind_every_element(self):
        for member, iid in self.EXPECTED_ARRAYS.items():
            with self.subTest(member=member):
                stubs = _InterfaceModules()
                elements = [types.SimpleNamespace(_oleobj_=Mock()) for _ in range(2)]
                for element in elements:
                    element._oleobj_.QueryInterface.return_value = stubs.vendor
                raw = types.SimpleNamespace(**{member: Mock(spec=lambda *args: None, return_value=elements)})
                with patch.dict(sys.modules, stubs.modules):
                    views = native._member(raw, member)
                self.assertEqual(len(views), 2)
                for element in elements:
                    element._oleobj_.QueryInterface.assert_called_once_with(iid, "IDispatch")
                self.assertTrue(all(view is stubs.view for view in views))

    def test_late_bound_non_callable_array_return_is_bound(self):
        stubs = _InterfaceModules()
        elements = [types.SimpleNamespace(_oleobj_=Mock()) for _ in range(2)]
        for element in elements:
            element._oleobj_.QueryInterface.return_value = stubs.vendor
        raw = types.SimpleNamespace(GetFaces=tuple(elements))  # property-shaped, not callable
        with patch.dict(sys.modules, stubs.modules):
            views = native._member(raw, "GetFaces")
        self.assertEqual(len(views), 2)
        for element in elements:
            element._oleobj_.QueryInterface.assert_called_once_with(native.IFACE2_IID, "IDispatch")
        self.assertTrue(all(view is stubs.view for view in views))

    def test_method_binding_covers_dynamic_step_members(self):
        for member in ("GetNextFeature", "GetNextSubFeature", "GetFirstSubFeature"):
            with self.subTest(member=member):
                stubs = _InterfaceModules()
                returned = types.SimpleNamespace(_oleobj_=stubs.generic)
                raw = types.SimpleNamespace(**{member: Mock(spec=lambda *args: None, return_value=returned)})
                with patch.dict(sys.modules, stubs.modules):
                    view = native._method(raw, member)
                stubs.generic.QueryInterface.assert_called_once_with(native.IFEATURE_IID, "IDispatch")
                self.assertIs(view, stubs.view)

    def test_mate_entity_reference_is_a_documented_multi_type_exemption(self):
        source = pathlib.Path(native.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_entity_view", source)
        self.assertNotIn("E_NOINTERFACE", source)
        self.assertIn("EXEMPT (untyped multi-type return): IMateEntity2.Reference", source)
        self.assertIn('target = _member(entity, "Reference")', source)
        self.assertIsNone(native.RETURN_VIEWS.get("Reference"))
        self.assertNotIn("Reference", native.RETURN_ARRAY_VIEWS)
        tree = ast.parse(source)
        guarded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            raises_cad = any(
                isinstance(stmt, ast.Raise)
                and isinstance(stmt.exc, ast.Call)
                and getattr(stmt.exc.func, "id", "") == "CadError"
                for handler in node.handlers
                for stmt in handler.body
            )
            reads_reference = any(
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == "_member"
                and len(item.args) > 1
                and isinstance(item.args[0], ast.Name)
                and item.args[0].id == "entity"
                and isinstance(item.args[1], ast.Constant)
                and item.args[1].value == "Reference"
                for item in ast.walk(node)
            )
            if raises_cad and reads_reference:
                guarded = True
                break
        self.assertTrue(guarded, "the Reference read must fail closed as cad_mate_unreadable")

    def test_reference_read_returns_a_generic_dispatch_without_a_geometry_qi(self):
        stubs = _InterfaceModules()
        raw_reference = types.SimpleNamespace(_oleobj_=stubs.generic)
        entity = types.SimpleNamespace(Reference=raw_reference)
        with patch.dict(sys.modules, stubs.modules):
            target = native._member(entity, "Reference")
        stubs.generic.QueryInterface.assert_not_called()
        stubs.dynamic.DumbDispatch.assert_called_once_with(stubs.generic)
        self.assertIs(target, stubs.view)

        feature_ole = Mock()
        feature_ole.QueryInterface.return_value = "feature-view"
        stubs.view.GetFeature = Mock(spec=lambda: None, return_value=types.SimpleNamespace(_oleobj_=feature_ole))
        with patch.dict(sys.modules, stubs.modules):
            feature = native._member(target, "GetFeature")
        feature_ole.QueryInterface.assert_called_once_with(native.IFEATURE_IID, "IDispatch")
        self.assertIs(feature, stubs.view)

    def test_mass_readers_capture_configuration_before_stateful_calls(self):
        tree = ast.parse(pathlib.Path(native.__file__).read_text(encoding="utf-8"))
        readers = (
            "_mass_properties_document",
            "assembly_mass_properties",
            "assembly_component_mass_properties",
            "assembly_group_mass_properties",
        )
        seen = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name in readers):
                continue
            seen.add(node.name)
            stateful = [
                item.lineno
                for item in ast.walk(node)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == "_member"
                and len(item.args) > 1
                and isinstance(item.args[1], ast.Constant)
                and item.args[1].value == "Recalculate"
            ]
            stateful += [
                item.lineno
                for item in ast.walk(node)
                if isinstance(item, ast.Assign)
                and isinstance(item.targets[0], ast.Attribute)
                and item.targets[0].attr == "SelectedItems"
            ]
            self.assertTrue(stateful, node.name)
            first_stateful = min(stateful)
            captures = [
                item.lineno
                for item in ast.walk(node)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == "_configuration_context"
            ]
            paths = [
                item.lineno
                for item in ast.walk(node)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == "_member"
                and len(item.args) > 1
                and isinstance(item.args[1], ast.Constant)
                and item.args[1].value in ("GetPathName", "GetTitle")
                and isinstance(item.args[0], ast.Name)
                and item.args[0].id == "doc"
            ]
            self.assertTrue(captures, node.name)
            self.assertTrue(paths, node.name)
            self.assertLess(max(captures), first_stateful, node.name)
            self.assertLess(max(paths), first_stateful, node.name)
            late = [
                item.lineno
                for item in ast.walk(node)
                if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id in ("_member", "_method")
                and item.args
                and isinstance(item.args[0], ast.Name)
                and item.args[0].id in ("doc", "active", "config", "configuration")
                and item.lineno > first_stateful
            ]
            self.assertEqual(late, [], f"{node.name} late doc/config reads at lines {late}")
        self.assertEqual(seen, set(readers))

    def test_configuration_reads_funnel_through_the_bound_helpers(self):
        source = pathlib.Path(native.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count('"ConfigurationManager"'), 1)
        self.assertEqual(source.count('"ActiveConfiguration"'), 1)
        self.assertGreaterEqual(source.count("_active_configuration_view("), 10)

    def test_mechanical_transform_never_falls_back_to_presentation_state(self):
        source = pathlib.Path(native.__file__).read_text(encoding="utf-8")
        self.assertNotIn('("GetTotalTransform", True)', source)
        self.assertIn('_member(component, "GetTotalTransform", False)', source)

    def test_undeclared_lightweight_member_is_reported_not_probed(self):
        source = pathlib.Path(native.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_optional_bool", source)
        self.assertNotIn('"IsLightWeight"', source)
        self.assertNotIn('"IsLightweight"', source)
        self.assertIn('state["lightweight"] = None', source)
        self.assertIn('state["unsupported"] = ["lightweight"]', source)
        self.assertIn("lightweight:{path_name}:unsupported_declared", source)

    def test_document_state_reports_unsupported_lightweight_without_probing(self):
        doc = types.SimpleNamespace(
            GetPathName=Mock(spec=lambda: None, return_value="C:/captures/robot.SLDASM"),
            GetTitle=Mock(spec=lambda: None, return_value="robot"),
            GetSaveFlag=Mock(spec=lambda: None, return_value=False),
            IsOpenedReadOnly=True,
            ConfigurationManager=types.SimpleNamespace(ActiveConfiguration=types.SimpleNamespace(Name="Default")),
        )
        backend = SolidWorksBackend()
        backend._ensure_document = lambda path: doc
        backend.list_configurations = lambda path: ["Default"]
        state = backend.document_state("C:/captures/robot.SLDASM")
        self.assertIsNone(state["lightweight"])
        self.assertEqual(state["unsupported"], ["lightweight"])
        self.assertTrue(state["saved"])
        self.assertTrue(state["read_only"])

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
