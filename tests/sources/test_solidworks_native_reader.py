"""Portable producer-contract regressions for ``SolidWorksBackend.discover_native``.

These tests drive the *real* reader with analytic mock COM primitives (plain
Python objects under the same late-binding access discipline: parameterless
vendor methods are callables, properties are values).  They pin the per-mate
producer record the native reader must emit - native type, SI-bounded travel
with ``limitdistance``/``limitangle`` promotion, actual suppression state and
the solve error code - plus the structured failures for unreadable fields,
invalid entity counts, ambiguous occurrence scopes and feature-tree traversal.

They establish the portable contract only: no CAD and no Windows is involved,
and nothing here claims native qualification.  The Windows/CAD qualification
stays owned by the native owner.
"""

from __future__ import annotations

import copy
import os
import sys
import threading
import types
import unittest
import weakref
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import description_pipeline.sources.solidworks.native as native_module
from description_pipeline.sources.solidworks.errors import BridgeError, CadError
from description_pipeline.sources.solidworks.isolation import CadSession
from description_pipeline.sources.solidworks.jsonio import read_json, write_json
from description_pipeline.sources.solidworks.native import (
    SolidWorksBackend,
    _appearance_display_state,
    _appearance_document_display_state,
    _appearance_persistent_reference,
    _appearance_render_material,
    _state_render_materials_raw,
    _project_mass_reading,
    _project_link_group_mass,
    _raw_mass_fact,
    _temporary_configuration,
)
#: SolidWorks ``MathTransform.ArrayData`` order for an identity occurrence.
SW_IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
PLANE_XY = {"normal": [0.0, 0.0, 1.0], "point": [0.0, 0.0, 0.0]}
CYLINDER_Z = {"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.02}
_PRODUCTION_APPEARANCE_INTERFACE = native_module._appearance_interface


def _sw_translation(x, y, z):
    """SolidWorks ``ArrayData`` for an identity rotation plus a translation."""

    return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, x, y, z, 1.0, 0.0, 0.0, 0.0)


def _raiser(error: BaseException):
    def call(*_args, **_kwargs):
        raise error

    return call


def _fixture_appearance_interface(value, _iid, name):
    interfaces = getattr(value, "_fixture_interfaces", ())
    return (value if name in interfaces else None), None


class _FixtureAppearanceInterfaceMixin:
    @classmethod
    def setUpClass(cls):
        cls._appearance_interface_patcher = patch.object(
            native_module,
            "_appearance_interface",
            side_effect=_fixture_appearance_interface,
        )
        cls._appearance_interface_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._appearance_interface_patcher.stop()


class _Plane:
    def IsCylinder(self):
        return False

    def IsPlane(self):
        return True

    def __init__(self, normal, point):
        self.PlaneParams = tuple(normal) + tuple(point)


class _Cylinder:
    def IsCylinder(self):
        return True

    def IsPlane(self):
        return False

    def __init__(self, point, direction, radius):
        self.CylinderParams = tuple(point) + tuple(direction) + (radius,)


class _Circle:
    def IsCircle(self):
        return True

    def __init__(self, center, normal, radius):
        self.CircleParams = tuple(center) + tuple(normal) + (radius,)


class _Target:
    """``IEntity``-like mate reference with recorded surface geometry."""

    def __init__(self, feature_name, geometry=None, *, curve=None, point=None):
        self._feature_name = feature_name
        self._geometry = geometry
        self._curve = curve
        self._point = point

    def GetFeature(self):
        return types.SimpleNamespace(Name=self._feature_name)

    def GetSurface(self):
        return self._geometry

    def GetCurve(self):
        return self._curve

    def GetPoint(self):
        return list(self._point) if self._point is not None else None


class _MateEntity:
    def __init__(
        self,
        component,
        feature_name,
        geometry=None,
        *,
        curve=None,
        point=None,
        reference_type=0,
        reference_type2=0,
        entity_params=(0.0, 0.0, 0.0),
    ):
        self.ReferenceComponent = component
        self.Reference = _Target(feature_name, geometry, curve=curve, point=point)
        # Documented IMateEntity2 readings; values may also be callables whose
        # invocation raises, modelling unreadable native members.
        self.ReferenceType = reference_type
        self.ReferenceType2 = reference_type2
        self.EntityParams = entity_params

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if name in {"ReferenceType", "ReferenceType2", "EntityParams"} and callable(
            value
        ):
            return value()
        return value


class _MateSpecific:
    """``IMate2``-like specific feature. ``count`` overrides the entity list."""

    def __init__(self, mate_type, entities=(), lower=0.0, upper=0.0, count=None):
        self._mate_type = mate_type
        self._entities = entities if callable(entities) else tuple(entities)
        self._lower = lower
        self._upper = upper
        self._count = count if count is not None else len(self._entities)

    def _value(self, provider):
        return provider() if callable(provider) else provider

    @property
    def Type(self):
        return self._value(self._mate_type)

    def GetMateEntityCount(self):
        return self._value(self._count)

    def MateEntity(self, index):
        if callable(self._entities):
            return self._entities(index)
        return self._entities[index]

    @property
    def MinimumVariation(self):
        return self._value(self._lower)

    @property
    def MaximumVariation(self):
        return self._value(self._upper)


class _Feature:
    """``IFeature``-like node; providers may be values or raising callables."""

    def __init__(
        self,
        name,
        type_name,
        *,
        specific=None,
        suppressed=False,
        error_code=0,
        first_sub=None,
        next_sub=None,
        next_feature=None,
        definition=None,
    ):
        self.Name = name
        self._type_name = type_name
        self._specific = specific
        self._suppressed = suppressed
        self._error_code = error_code
        self._first_sub = first_sub
        self._next_sub = next_sub
        self._next_feature = next_feature
        self._definition = definition

    def _value(self, provider):
        return provider() if callable(provider) else provider

    def GetTypeName2(self):
        return self._value(self._type_name)

    def GetSpecificFeature2(self):
        return self._value(self._specific)

    def IsSuppressed(self):
        return self._value(self._suppressed)

    def GetErrorCode(self):
        return self._value(self._error_code)

    def GetFirstSubFeature(self):
        return self._value(self._first_sub)

    def GetNextSubFeature(self):
        return self._value(self._next_sub)

    def GetNextFeature(self):
        return self._value(self._next_feature)

    def GetDefinition(self):
        return self._value(self._definition)


class _Component:
    def __init__(
        self,
        name,
        path,
        *,
        children=(),
        doc=None,
        suppressed=False,
        suppression=None,
        transform=SW_IDENTITY,
        configuration="Default",
        is_root=False,
    ):
        self.Name2 = name
        self._path = str(path)
        self._children = list(children)
        # Unsuppressed components are always property-read, so every mock
        # component needs a real document; a missing API must be explicit.
        self._doc = doc if doc is not None else _Doc(self._path)
        self._transform = tuple(transform)
        #: ``swComponentSuppressionState_e`` raw state (0 suppressed, 1 lightweight,
        #: 2 fully resolved, 3 resolved, 4 fully lightweight, 5 internal-ID mismatch).
        self._suppression = suppression if suppression is not None else (0 if suppressed else 2)
        self._fixed = False
        # The read-only root-signature probe on the frozen production assembly
        # (2026-10-10) shows IsRoot as a native boolean: True on the root
        # reference, False on part references.  Mocks default to the part case.
        self._is_root = is_root
        self.ReferencedConfiguration = configuration
        self.IsVirtual = False

    def GetPathName(self):
        return self._path

    @property
    def IsSuppressed(self):
        return self._suppression == 0

    @IsSuppressed.setter
    def IsSuppressed(self, value):
        # Booleans keep the documented coherence; non-boolean values are stored
        # verbatim so tests can simulate an unreadable native state.
        self._suppression = (0 if value else 2) if isinstance(value, bool) else value

    def GetSuppression2(self):
        return self._suppression

    def IsFixed(self):
        return self._fixed

    def IsRoot(self):
        return self._is_root

    def GetTotalTransform(self, _resolved):
        return types.SimpleNamespace(ArrayData=list(self._transform))

    def GetChildren(self):
        return list(self._children)

    def GetModelDoc2(self):
        return self._doc


_AUTO = object()


class _PropertyManager:
    """``ICustomPropertyManager`` with the vendor six-argument ``Get6``.

    ``Get6(Name, ResolvedFlag, ValOut, ResolvedValOut, WasResolved, LinkToProperty)``
    writes four by-ref outputs and returns ``status2``.  ``Get`` exists only to
    record that the reader never falls back to the deprecated method.
    """

    def __init__(self, specs, *, names=_AUTO, get_names_error=None):
        self._specs = {
            name: (dict(raw) if isinstance(raw, dict) else {"value": raw, "resolved": raw})
            for name, raw in specs.items()
        }
        self._names = names
        self._get_names_error = get_names_error
        self.get6_calls = []
        self.get_calls = []
        self._oleobj_ = types.SimpleNamespace(
            QueryInterface=lambda *_args: self,
        )

    def GetNames(self):
        if self._get_names_error is not None:
            raise self._get_names_error
        if self._names is not _AUTO:
            return self._names
        return list(self._specs)

    def Get6(self, name, resolved_flag, value_out, resolved_out, was_resolved, linked):
        self.get6_calls.append((name, resolved_flag))
        spec = self._specs[name]
        if spec.get("error") is not None:
            raise spec["error"]
        value_out.value = spec.get("value", "")
        resolved_out.value = spec.get("resolved", spec.get("value", ""))
        was_resolved.value = spec.get("was_resolved", True)
        linked.value = spec.get("linked", True)
        return spec.get("status", 2)

    def Get(self, name):
        self.get_calls.append(name)
        return self._specs.get(name, {}).get("value", "")


class _Extension:
    def __init__(
        self,
        scopes,
        coordinate_systems=None,
        manager=_AUTO,
        scope_managers=None,
    ):
        self._scopes = scopes
        self._coordinate_systems = dict(coordinate_systems or {})
        self._manager = manager
        self._scope_managers = dict(scope_managers or {})
        self._oleobj_ = types.SimpleNamespace(
            QueryInterface=lambda *_args: self,
            GetIDsOfNames=lambda name: (
                83 if name == "CustomPropertyManager" else -1
            ),
            Invoke=self._invoke_property_get,
        )

    def _custom_property_manager(self, scope):
        if scope in self._scope_managers:
            return self._scope_managers[scope]
        if self._manager is not _AUTO:
            return self._manager
        return _PropertyManager(self._scopes.get(scope, {}))

    def _invoke_property_get(
        self,
        dispid,
        _lcid,
        invoke_type,
        result_wanted,
        scope,
    ):
        if (
            dispid != 83
            or invoke_type != sys.modules["pythoncom"].DISPATCH_PROPERTYGET
            or result_wanted != 1
        ):
            raise AssertionError("CustomPropertyManager property-get call differs")
        return self._custom_property_manager(scope)

    def GetCoordinateSystemTransformByName(self, name):
        transform = self._coordinate_systems.get(name)
        if transform is None:
            return None
        return types.SimpleNamespace(ArrayData=list(transform))

    def GetPersistReference3(self, item):
        reference = getattr(item, "_persist_reference", None)
        if reference is None:
            raise RuntimeError("fixture entity has no persistent reference")
        return list(reference)

    def CreateMassProperty2(self):
        return _PortableMassProperty()


class _PortableMassProperty:
    """Minimal native mass-property result for ordinary capture fixtures."""

    def __init__(self):
        self.UseSystemUnits = None
        self.IncludeHiddenBodiesOrComponents = None
        self.SelectedItems = None
        self.Mass = 1.0
        self.Volume = 0.001
        self.Density = 1000.0
        self.CenterOfMass = [0.0, 0.0, 0.0]

    def Recalculate(self):
        selected = tuple(component.Name2 for component in self.SelectedItems.value)
        self.Mass = float(max(1, len(selected)))

    def GetOverrideOptions(self):
        return types.SimpleNamespace(
            OverrideMass=False,
            OverrideCenterOfMass=False,
            OverrideMomentsOfInertia=False,
        )

    def GetMomentOfInertia(self, kind):
        if kind == 0:
            return [
                1e-6,
                0.0,
                0.0,
                0.0,
                1e-6,
                0.0,
                0.0,
                0.0,
                1e-6,
            ]
        raise RuntimeError("about-origin inertia is unavailable")


class _ConfigurationManager:
    """Reflects the document's *currently shown* configuration at each read."""

    def __init__(self, doc):
        self._doc = doc

    def _configuration(self, name):
        root = types.SimpleNamespace(
            Name2=os.path.splitext(os.path.basename(self._doc._path))[0],
            GetPathName=lambda: self._doc._path,
            GetChildren=lambda: list(self._doc.children_for(name)),
        )
        configuration = types.SimpleNamespace(
            Name=name,
            NeedsRebuild=False,
            AddRebuildSaveMark=False,
            GetRootComponent3=lambda _visible: root,
        )
        if self._doc._display_states is not None:
            configuration.GetDisplayStates = lambda: list(
                self._doc._display_states
            )
        return configuration

    @property
    def ActiveConfiguration(self):
        return self._configuration(self._doc.active_configuration)


class _Doc:
    """``IModelDoc2``-like assembly/part document, read-only and pre-opened."""

    def IsOpenedReadOnly(self):
        return True

    def __init__(
        self,
        path,
        *,
        properties=None,
        configuration_properties=None,
        coordinate_systems=None,
        first_feature=None,
        configuration_features=None,
        children=(),
        configuration_children=None,
        configuration="Default",
        doc_type=1,
        show_success=True,
        show_effect=True,
        custom_property_manager=_AUTO,
        scope_managers=None,
        display_states=None,
    ):
        self._path = str(path)
        self._children = list(children)
        self._configuration_children = {name: list(items) for name, items in (configuration_children or {}).items()}
        self._configuration_features = dict(configuration_features or {})
        self._doc_type = doc_type
        self.active_configuration = configuration
        self._show_success = show_success
        self._show_effect = show_effect
        self._display_states = (
            list(display_states) if display_states is not None else None
        )
        scopes = {"": dict(properties or {})}
        for name, values in (configuration_properties or {}).items():
            scopes[name] = dict(values)
        self.Extension = _Extension(
            scopes,
            coordinate_systems,
            custom_property_manager,
            scope_managers,
        )
        self.ConfigurationManager = _ConfigurationManager(self)
        self._first_feature = first_feature

    def children_for(self, name):
        return self._configuration_children.get(name, self._children)

    def ShowConfiguration2(self, name):
        """Documented contract: boolean success, never an exception for a bad name."""

        known = set(self._configuration_children) | set(self._configuration_features)
        if not self._show_success or (name not in known and name != self.active_configuration):
            return False
        if self._show_effect:
            self.active_configuration = name
        return True

    def GetPathName(self):
        return self._path

    def GetTitle(self):
        return os.path.basename(self._path)

    def GetType(self):
        return self._doc_type

    def FirstFeature(self):
        feature = self._configuration_features.get(self.active_configuration, self._first_feature)
        return feature() if callable(feature) else feature

    def GetComponents(self, _ignored):
        return list(self._children)

    def GetConfigurationNames(self):
        names = {
            self.active_configuration,
            *self._configuration_children,
            *self._configuration_features,
        }
        return sorted(names)

    def GetConfigurationByName(self, name):
        return self.ConfigurationManager._configuration(name)


class _App:
    StartupProcessCompleted = True
    """Minimal ``ISldWorks`` surface; documents are pre-opened read-only."""

    @staticmethod
    def RevisionNumber():
        return "2026-portable-mock"
    Visible = False

    def __init__(self, docs, open_errors=None, not_preopened=None, dependencies=None, dependency_error=None):
        self._docs = {os.path.normcase(os.path.abspath(str(path))): doc for path, doc in docs.items()}
        self._open_errors = {
            os.path.normcase(os.path.abspath(str(path))): code for path, code in (open_errors or {}).items()
        }
        self._not_preopened = {os.path.normcase(os.path.abspath(str(path))) for path in (not_preopened or ())}
        self._dependencies = {
            os.path.normcase(os.path.abspath(str(path))): tuple(entries)
            for path, entries in (dependencies or {}).items()
        }
        self._dependency_error = dependency_error
        self._search_folders: list[str] = []

    def GetSearchFolders(self, _folder_type):  # noqa: N802
        return ";".join(self._search_folders)

    def SetSearchFolders(self, _folder_type, folders):  # noqa: N802
        self._search_folders = [
            item
            for item in str(folders).split(";")
            if item
        ]
        return True

    def GetOpenDocumentByName(self, path):
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in self._docs and key not in self._not_preopened:
            return self._docs[key]
        # Opening an assembly also opens its resolved component documents.
        return self._component_document(key)

    def _component_document(self, key):
        stack = [component for doc in self._docs.values() for component in doc._children]
        visited = set()
        while stack:
            component = stack.pop()
            if id(component) in visited:
                continue
            visited.add(id(component))
            document = component.GetModelDoc2()
            if document is not None and os.path.normcase(os.path.abspath(document.GetPathName())) == key:
                return document
            stack.extend(component.GetChildren())
        return None

    def GetBuildNumbers(self):
        return "portable-mock"

    def GetProcessID(self):
        return 21040

    def GetCurrentLicenseType(self):
        return 1

    def GetDocuments(self):
        documents = list(self._docs.values())
        seen = {id(document) for document in documents}
        stack = [
            component
            for document in documents
            for component in document._children
        ]
        while stack:
            component = stack.pop()
            document = component.GetModelDoc2()
            if document is not None and id(document) not in seen:
                seen.add(id(document))
                documents.append(document)
            stack.extend(component.GetChildren())
        return documents

    def OpenDoc6(self, path, _kind, _options, _configuration, errors, warnings):
        key = os.path.normcase(os.path.abspath(str(path)))
        errors.value = self._open_errors.get(key, 0)
        warnings.value = 0
        self._not_preopened.discard(key)
        return self._docs.get(key) or self._component_document(key)

    def GetDocumentDependencies2(self, path, _traverse, _search, _read_only):
        if self._dependency_error is not None:
            raise self._dependency_error
        key = os.path.normcase(os.path.abspath(str(path)))
        return self._dependencies.get(key, ())


class _Session:
    def __init__(self, app):
        self.app = app
        self.process = types.SimpleNamespace(alive=lambda: True)
        self.closed = False

    def connect(self, _cancelled):
        return self.app

    def current_application(self):
        return self.app

    def close(self):
        self.closed = True
        self.app = None
        return None

    def identity(self):
        return {
            "pid": 21040,
            "reader": "portable-mock",
            "state": "closed" if self.closed else "active",
        }


class _CaptureBackend(SolidWorksBackend):
    """Prepared-Capture seam over the native COM doubles."""

    def capture_scene_for_test(self, doc_path, coordinate_systems, on_activity=None):
        from description_pipeline.sources.solidworks.native import project_prepared_capture

        root = str(Path(doc_path).resolve().parent)
        self.prepare_prepared_capture(root, str(doc_path), "Default")
        document = self._document_by_path(str(doc_path))
        leaves = []
        pending = list(document.children_for("Default"))
        while pending:
            component = pending.pop()
            if component._children:
                pending.extend(component._children)
            else:
                leaves.append(str(component.Name2))
        component = None
        pending.clear()
        document = None
        raw_facts = self.collect_prepared_scene(
            str(doc_path),
            coordinate_systems,
            groups=[
                {
                    "name": "fixture-leaf-group",
                    "components": sorted(leaves),
                }
            ],
            on_activity=on_activity,
        )
        self._fixture_source_scopes = copy.deepcopy(raw_facts["source_scopes"])
        return project_prepared_capture(raw_facts)["scene"]

    def capture_geometry_for_test(self, on_activity=None):
        return self.capture_prepared_geometry(
            self._fixture_source_scopes,
            on_activity=on_activity,
        )

    def _record_save_flag(self, doc, path):
        self.save_flags[str(path)] = False

    def _body_count(self, holder, body_type, component):
        return 1

    def _mass_properties_document(self, doc, require_material=True):
        return {
            "mass": 1.0,
            "com": (0.0, 0.0, 0.0),
            "native_raw": {
                "mass_kg": 1.0,
                "center_of_mass_m": [0.0, 0.0, 0.0],
                "inertia_values": [
                    1e-6,
                    0.0,
                    0.0,
                    0.0,
                    1e-6,
                    0.0,
                    0.0,
                    0.0,
                    1e-6,
                ],
                "volume_m3": 0.001,
                "density_kg_m3": 1000.0,
                "overrides": {
                    "OverrideMass": False,
                    "OverrideCenterOfMass": False,
                    "OverrideMomentsOfInertia": False,
                },
            },
            "read_errors": {},
            "mode": "full",
            "reference": {
                "used_api": "portable-capture-mock",
                "configuration": None,
                "reference_point": "center_of_mass",
                "axes": "part_document_axes",
                "use_system_units": True,
            },
        }

    def assembly_component_mass_properties(self, path):
        instances = []
        for name in sorted(self._components):
            instances.append(
                {
                    "name": name,
                    "parent": None,
                    "depth": 0,
                    "document": str(path),
                    "document_type": "part",
                    "context_mass_kg": 1.0,
                    "context_volume_m3": 0.001,
                    "overrides": {
                        "OverrideMass": False,
                        "OverrideCenterOfMass": False,
                        "OverrideMomentsOfInertia": False,
                    },
                    "read_errors": {},
                    "effective": None,
                    "effective_error": None,
                }
            )
        return {
            "assembly": str(path),
            "configuration": "Default",
            "instances": instances,
            "errors": [],
            "reference": {
                "method": "IMassProperty2 (SelectedItems = component instance)",
            },
        }

def _write(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(f"portable fixture: {name}\n".encode())
    return path


def _mesh_face(read_triangles, key: str):
    try:
        fixture_values = list(read_triangles(True))
        fixture_readable = True
    except Exception:  # noqa: BLE001 - the production read must surface this later
        fixture_values = []
        fixture_readable = False
    face = types.SimpleNamespace(
        GetTessTriangles=read_triangles,
        GetTessTriangleCount=lambda: len(fixture_values) // 9,
        GetArea=lambda: 1.0,
        GetEdges=list,
        GetBody=lambda: None,
        _persist_reference=f"fixture-face-{key}".encode(),
        _fixture_interfaces={"IEntity"},
        _fixture_values=fixture_values,
        _fixture_readable=fixture_readable,
    )
    return face


def _mesh_body(faces, key: str):
    faces = list(faces)
    for face in faces:
        if not hasattr(face, "GetTessTriangles"):
            face._fixture_values = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
            face._fixture_readable = True
            face.GetTessTriangles = lambda _quality, values=face._fixture_values: list(values)
    body = types.SimpleNamespace(
        Name=f"fixture-body-{key}",
        GetFaces=lambda: list(faces),
        GetEdges=list,
        GetVertexCount=list,
        GetVertices=list,
        _persist_reference=f"fixture-body-{key}".encode(),
        _fixture_interfaces={"IBody2"},
    )
    for face in faces:
        face.GetBody = lambda body=body: body
    return body


def _component_bodies3(provider):
    def read(body_type, body_info):
        bodies = provider(body_type)
        body_info.value = [1] * len(bodies)
        return bodies

    return read


def _component_body_count(holder, body_type):
    body_info = types.SimpleNamespace(value=None)
    return len(holder.GetBodies3(body_type, body_info))




@contextmanager
def _com_stubs():
    """Minimal pythoncom/win32com surface used by the by-ref property reads."""

    pythoncom = types.ModuleType("pythoncom")
    pythoncom.VT_BYREF = 0x4000
    pythoncom.VT_BSTR = 8
    pythoncom.VT_BOOL = 11
    pythoncom.VT_I4 = 3
    pythoncom.VT_EMPTY = 0
    pythoncom.VT_DISPATCH = 9
    pythoncom.VT_ARRAY = 0x2000
    pythoncom.VT_VARIANT = 12
    pythoncom.VT_UI1 = 17
    pythoncom.DISPATCH_PROPERTYGET = 2
    pythoncom.IID_IDispatch = 9
    pythoncom.COINIT_APARTMENTTHREADED = 2
    pythoncom.CoInitializeEx = lambda *_args, **_kwargs: None
    pythoncom.CoUninitialize = lambda: None
    pythoncom.VARIANT = lambda vt, value: types.SimpleNamespace(
        value=value,
        varianttype=vt,
    )
    win32com = types.ModuleType("win32com")
    client = types.ModuleType("win32com.client")
    client.VARIANT = pythoncom.VARIANT
    client.Dispatch = lambda value: value
    win32com.client = client
    dynamic = types.ModuleType("win32com.client.dynamic")
    dynamic.DumbDispatch = lambda value: value
    client.dynamic = dynamic
    pywintypes = types.ModuleType("pywintypes")
    pywintypes.IID = lambda value: value
    added = {
        "pythoncom": pythoncom,
        "pywintypes": pywintypes,
        "win32com": win32com,
        "win32com.client": client,
        "win32com.client.dynamic": dynamic,
    }
    saved = {name: sys.modules.get(name) for name in added}
    sys.modules.update(added)
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _read(root: Path, app: _App, settings=None) -> dict:
    backend = SolidWorksBackend(session_factory=lambda: _Session(app))
    resolved = {"main_assembly": "robot.SLDASM"} if settings is None else settings
    with _com_stubs():
        return backend.discover_native(root, resolved)


def _mate(
    name, mate_type, entities, *, lower=0.0, upper=0.0, count=None, suppressed=False, error_code=0, definition=None
):
    specific = _MateSpecific(mate_type, entities, lower=lower, upper=upper, count=count)
    if definition is not None:
        if mate_type == 5 and not hasattr(definition, "Distance"):
            definition.Distance = 0.0
            definition.MinimumDistance = lower
            definition.MaximumDistance = upper
        if mate_type == 6 and not hasattr(definition, "Angle"):
            definition.Angle = 0.0
            definition.MinimumAngle = lower
            definition.MaximumAngle = upper
    return _Feature(
        name, f"Mate{mate_type}", specific=specific, suppressed=suppressed, error_code=error_code, definition=definition
    )


def _single_mate_scene(root: Path, make_feature):
    """One assembly, one component (``base-1``) and one mate from ``make_feature``."""

    base = _write(root, "base.SLDPRT")
    component = _Component("base-1", base)
    feature = make_feature(component)
    group = _Feature("MateGroup", "MateGroup", first_sub=feature)
    assembly = _write(root, "robot.SLDASM")
    doc = _Doc(assembly, first_feature=group, children=[component])
    return _App({assembly: doc})


class MateRecordTests(unittest.TestCase):
    def test_mate_records_type_limits_suppression_and_error_code(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            plane_entity = _MateEntity(component, "Plane1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)))
            cylinder_entity = _MateEntity(component, "Cylinder1", _Cylinder((0.0, 0.0, 0.0), (0.0, 0.0, 2.0), 0.02))
            travel = _mate(
                "travel_limit",
                5,
                [plane_entity],
                lower=0.0,
                upper=0.03,
                definition=types.SimpleNamespace(IsAdvancedMate=True),
            )
            swing = _mate(
                "swing_limit",
                6,
                [cylinder_entity],
                lower=-0.1,
                upper=0.2,
                suppressed=True,
                definition=types.SimpleNamespace(IsAdvancedMate=True),
            )
            open_distance = _mate(
                "travel_open",
                5,
                [plane_entity],
                lower=0.0,
                upper=0.0,
                definition=types.SimpleNamespace(IsAdvancedMate=False),
            )
            locked = _mate("locked_seat", 16, [plane_entity], suppressed=True)
            broken = _mate("broken_solve", 0, [plane_entity], error_code=3)
            travel._next_sub = swing
            swing._next_sub = open_distance
            open_distance._next_sub = locked
            locked._next_sub = broken
            group = _Feature("MateGroup", "MateGroup", first_sub=travel)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, first_feature=group, children=[component])

            record = _read(root, _App({assembly: doc}))

            mates = {mate["name"]: mate for mate in record["mates"]}
            self.assertEqual(
                sorted(mates), ["broken_solve", "locked_seat", "swing_limit", "travel_limit", "travel_open"]
            )
            self.assertEqual(
                set(mates["travel_limit"]),
                {
                    "name",
                    "type",
                    "suppressed",
                    "limits",
                    "is_advanced_mate",
                    "is_advanced_mate_error",
                    "is_advanced_mate_api",
                    "native_limit",
                    "entity_count",
                    "entities",
                    "error_code",
                    "scope",
                    "configuration",
                },
            )
            self.assertEqual({mate["configuration"] for mate in mates.values()}, {"Default"})
            self.assertEqual(mates["travel_limit"]["type"], "limitdistance")
            self.assertEqual(mates["travel_limit"]["limits"], {"lower": 0.0, "upper": 0.03, "unit": "m"})
            self.assertIs(mates["travel_limit"]["suppressed"], False)
            self.assertEqual(mates["travel_limit"]["error_code"], 0)
            self.assertEqual(mates["travel_limit"]["scope"], "")
            entity = mates["travel_limit"]["entities"][0]
            self.assertEqual(entity["component"], "base-1")
            self.assertEqual(entity["feature"], "Plane1")
            self.assertEqual(entity["plane"], PLANE_XY)
            self.assertEqual(mates["swing_limit"]["type"], "angle")
            self.assertIsNone(mates["swing_limit"]["limits"])
            self.assertIsNone(mates["swing_limit"]["native_limit"])
            self.assertIs(mates["swing_limit"]["suppressed"], True)
            self.assertEqual(mates["swing_limit"]["entities"], [])
            self.assertEqual(mates["travel_open"]["type"], "distance")
            self.assertIsNone(mates["travel_open"]["limits"])
            self.assertEqual(mates["locked_seat"]["type"], "lock")
            self.assertIs(mates["locked_seat"]["suppressed"], True)
            self.assertEqual(mates["broken_solve"]["type"], "coincident")
            self.assertEqual(mates["broken_solve"]["error_code"], 3)

    def test_limit_mate_flag_is_authoritative_for_zero_and_standard_mates(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            plane_entity = _MateEntity(component, "Plane1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)))
            zero = _mate(
                "zero_limit",
                5,
                [plane_entity],
                lower=0.0,
                upper=0.0,
                definition=types.SimpleNamespace(IsAdvancedMate=True),
            )
            standard = _mate(
                "standard_distance",
                5,
                [plane_entity],
                lower=-1.0,
                upper=2.0,
                definition=types.SimpleNamespace(IsAdvancedMate=False),
            )
            unknown = _mate("unknown_state", 6, [plane_entity], lower=0.0, upper=0.0)
            zero._next_sub = standard
            standard._next_sub = unknown
            group = _Feature("MateGroup", "MateGroup", first_sub=zero)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, first_feature=group, children=[component])

            mates = {mate["name"]: mate for mate in _read(root, _App({assembly: doc}))["mates"]}

            self.assertEqual(mates["zero_limit"]["limits"], {"lower": 0.0, "upper": 0.0, "unit": "m"})
            self.assertIs(mates["zero_limit"]["is_advanced_mate"], True)
            self.assertEqual(
                mates["zero_limit"]["is_advanced_mate_api"],
                "IDistanceMateFeatureData.IsAdvancedMate",
            )
            self.assertIsNone(mates["standard_distance"]["limits"])
            self.assertIs(mates["standard_distance"]["is_advanced_mate"], False)
            self.assertEqual(mates["standard_distance"]["type"], "distance")
            self.assertIsNone(mates["unknown_state"]["limits"])
            self.assertIsNone(mates["unknown_state"]["is_advanced_mate"])
            self.assertTrue(mates["unknown_state"]["is_advanced_mate_error"])

    def test_limit_mate_composes_nominal_and_variation_into_absolute_bounds(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            plane_entity = _MateEntity(component, "Plane1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)))
            definition = types.SimpleNamespace(
                IsAdvancedMate=True,
                Distance=0.05,
                MinimumDistance=0.04,
                MaximumDistance=0.07,
            )
            specific = _MateSpecific(5, [plane_entity], lower=-0.01, upper=0.02)
            feature = _Feature("travel_limit", "MateDistance", specific=specific, definition=definition)
            group = _Feature("MateGroup", "MateGroup", first_sub=feature)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, first_feature=group, children=[component])

            mate = _read(root, _App({assembly: doc}))["mates"][0]

            self.assertEqual(mate["limits"], {"lower": 0.04, "upper": 0.07, "unit": "m"})
            self.assertEqual(mate["native_limit"]["nominal_dimension"]["value"], 0.05)
            self.assertEqual(mate["native_limit"]["minimum_variation"]["value"], -0.01)
            self.assertEqual(mate["native_limit"]["maximum_variation"]["value"], 0.02)
            self.assertEqual(mate["entities"][0]["reference_configuration"], "Default")

    def test_concentric_mate_records_the_native_lock_rotation_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            cylinder_entity = _MateEntity(
                component, "Cylinder1", _Cylinder((0.0, 0.0, 0.0), (0.0, 0.0, 2.0), 0.02)
            )
            locked = _mate(
                "locked_spin", 1, [cylinder_entity], definition=types.SimpleNamespace(LockRotation=True)
            )
            free = _mate(
                "free_spin", 1, [cylinder_entity], definition=types.SimpleNamespace(LockRotation=False)
            )
            no_definition = _mate("no_definition", 1, [cylinder_entity])
            unrelated = _mate(
                "seat", 0, [cylinder_entity], definition=types.SimpleNamespace(LockRotation=True)
            )
            locked._next_sub = free
            free._next_sub = no_definition
            no_definition._next_sub = unrelated
            group = _Feature("MateGroup", "MateGroup", first_sub=locked)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, first_feature=group, children=[component])

            record = _read(root, _App({assembly: doc}))

            mates = {mate["name"]: mate for mate in record["mates"]}
            self.assertEqual(mates["locked_spin"]["type"], "concentric")
            self.assertIs(mates["locked_spin"]["lock_rotation"], True)
            self.assertEqual(
                mates["locked_spin"]["lock_rotation_api"], "IConcentricMateFeatureData.LockRotation"
            )
            self.assertIs(mates["free_spin"]["lock_rotation"], False)
            self.assertEqual(
                mates["free_spin"]["lock_rotation_api"], "IConcentricMateFeatureData.LockRotation"
            )
            self.assertIsNone(mates["no_definition"]["lock_rotation"])
            self.assertNotIn("lock_rotation_api", mates["no_definition"])
            self.assertNotIn("lock_rotation", mates["seat"])
            self.assertIn(
                "mate_lock_rotation:locked_spin:read:IConcentricMateFeatureData.LockRotation", record["notes"]
            )
            self.assertIn(
                "mate_lock_rotation:free_spin:read:IConcentricMateFeatureData.LockRotation", record["notes"]
            )
            self.assertTrue(
                any(str(note).startswith("mate_lock_rotation:no_definition") for note in record["notes"]),
                record["notes"],
            )

    def test_two_level_occurrence_context_scopes_nested_mate(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            arm_doc = _Doc(arm_path)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            nested = _mate(
                "nested_seat",
                0,
                [_MateEntity(arm_component, "Face1", _Plane((1.0, 0.0, 0.0), (0.0, 0.0, 0.0)))],
            )
            sub_group = _Feature("MateGroup", "MateGroup", first_sub=nested)
            sub_doc = _Doc(
                sub_path,
                first_feature=sub_group,
                children=[arm_component],
                configuration="Sub",
                configuration_children={"Sub": [arm_component]},
            )
            sub_component = _Component("sub-1", sub_path, children=[arm_component], doc=sub_doc, configuration="Sub")
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, children=[sub_component])

            record = _read(root, _App({assembly: main_doc, sub_path: sub_doc}))

            self.assertEqual(record["identity"]["main_assembly"], "robot.SLDASM")
            self.assertEqual([item["name2"] for item in record["components"]], ["sub-1", "sub-1/arm-1"])
            self.assertEqual([item["document"] for item in record["components"]], ["sub.SLDASM", "arm.SLDPRT"])
            self.assertEqual(len(record["mates"]), 1)
            self.assertEqual(record["mates"][0]["name"], "nested_seat")
            self.assertEqual(record["mates"][0]["scope"], "sub-1")
            self.assertEqual(record["mates"][0]["configuration"], "Sub")
            self.assertEqual(record["mates"][0]["entities"][0]["component"], "sub-1/arm-1")
            self.assertEqual(sorted(record["files"]), ["arm.SLDPRT", "robot.SLDASM", "sub.SLDASM"])


class ProducerContextTests(unittest.TestCase):
    def test_discovery_reacquires_top_document_after_nested_configuration_mutation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            assembly = _write(root, "robot.SLDASM")
            state = {"generation": 0}
            expired_reads = []

            class NestedDocument(_Doc):
                def ShowConfiguration2(self, name):
                    previous = self.active_configuration
                    selected = super().ShowConfiguration2(name)
                    if selected and self.active_configuration != previous:
                        state["generation"] += 1
                    return selected

            arm = _Doc(arm_path)
            child = _Component("arm-1", arm_path, doc=arm)
            sub = NestedDocument(
                sub_path,
                doc_type=2,
                configuration="Parked",
                configuration_children={"Parked": [], "Working": [child]},
            )
            occurrence = _Component("sub-1", sub_path, doc=sub, children=[child], configuration="Working")
            main = _Doc(
                assembly,
                doc_type=2,
                children=[occurrence],
                coordinate_systems={"CS_base": SW_IDENTITY},
                first_feature=_Feature("CS_base", "CoordSys"),
            )

            class MainHandle:
                def __init__(self):
                    self.generation = state["generation"]

                def __getattr__(self, name):
                    if not name.startswith("_") and self.generation != state["generation"]:
                        expired_reads.append(name)
                        raise RuntimeError("top document handle expired after a nested mutation")
                    return getattr(main, name)

            class App(_App):
                def GetOpenDocumentByName(self, path):
                    if os.path.abspath(path) == str(assembly):
                        return MainHandle()
                    return super().GetOpenDocumentByName(path)

            record = _read(root, App({assembly: main, sub_path: sub, arm_path: arm}))

            self.assertGreater(state["generation"], 0)
            self.assertEqual(expired_reads, [])
            self.assertIn("sub-1/arm-1", [item["name2"] for item in record["components"]])
            self.assertEqual([(item["owner"], item["name"]) for item in record["datums"]], [("", "CS_base")])

    def test_discovery_keeps_only_primitives_after_occurrence_traversal(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            part_path = _write(root, "shared.SLDPRT")
            assembly = _write(root, "robot.SLDASM")
            borrowed = []

            class SharedDocument(_Doc):
                def ShowConfiguration2(self, name):
                    if name != self.active_configuration and any(ref() is not None for ref in borrowed):
                        raise RuntimeError("borrowed occurrences survived a configuration boundary")
                    return super().ShowConfiguration2(name)

            shared = SharedDocument(
                part_path,
                configuration="Parked",
                configuration_children={"Parked": [], "Short": [], "Long": []},
                configuration_properties={"Short": {"dp.role": "short"}, "Long": {"dp.role": "long"}},
                coordinate_systems={"CS_tip": _sw_translation(0.1, 0.0, 0.0)},
                first_feature=_Feature("CS_tip", "CoordSys"),
            )
            main = _Doc(assembly, doc_type=2)

            def occurrences(_configuration):
                result = []
                for name, configuration in (("arm-short", "Short"), ("arm-long", "Long")):
                    component = _Component(name, part_path, doc=shared, configuration=configuration)
                    borrowed.append(weakref.ref(component))
                    result.append(component)
                return result

            main.children_for = occurrences
            record = _read(root, _App({assembly: main, part_path: shared}))

            self.assertEqual(record["properties"]["components"]["arm-short"]["dp.role"], "short")
            self.assertEqual(record["properties"]["components"]["arm-long"]["dp.role"], "long")
            self.assertEqual(
                {(datum["owner"], datum["configuration"]) for datum in record["datums"]},
                {("arm-short", "Short"), ("arm-long", "Long")},
            )
            self.assertEqual(set(record["files"]), {"robot.SLDASM", "shared.SLDPRT"})
            self.assertTrue(all(ref() is None for ref in borrowed))

    def test_suppressed_datum_with_a_stored_transform_is_not_discovered(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            inactive = _Feature("CS_inactive", "CoordSys", suppressed=True)
            active = _Feature("CS_active", "CoordSys", next_feature=inactive)
            doc = _Doc(
                assembly,
                first_feature=active,
                coordinate_systems={
                    "CS_active": SW_IDENTITY,
                    "CS_inactive": _sw_translation(1.0, 0.0, 0.0),
                },
            )

            record = _read(root, _App({assembly: doc}))

            self.assertEqual([datum["name"] for datum in record["datums"]], ["CS_active"])

    def test_unreadable_or_non_boolean_datum_suppression_blocks(self) -> None:
        for state in (None, 1, "false", _raiser(RuntimeError("suppression unavailable"))):
            with self.subTest(state=state), TemporaryDirectory() as tmp:
                root = Path(tmp)
                assembly = _write(root, "robot.SLDASM")
                doc = _Doc(
                    assembly,
                    first_feature=_Feature("CS_tip", "CoordSys", suppressed=state),
                    coordinate_systems={"CS_tip": SW_IDENTITY},
                )

                with self.assertRaises(CadError) as caught:
                    _read(root, _App({assembly: doc}))

                self.assertEqual(caught.exception.code, "cad_geometry_unreadable")
                self.assertEqual(caught.exception.detail["datum"], "CS_tip")
                self.assertEqual(caught.exception.detail["phase"], "suppression")

    def test_active_datum_without_a_transform_blocks_discovery(self) -> None:
        for scope in ("assembly", "component"):
            with self.subTest(scope=scope), TemporaryDirectory() as tmp:
                root = Path(tmp)
                assembly = _write(root, "robot.SLDASM")
                datum = _Feature("CS_tip", "CoordSys")
                if scope == "component":
                    part = _write(root, "arm.SLDPRT")
                    part_doc = _Doc(part, first_feature=datum)
                    component = _Component("arm-1", part, doc=part_doc)
                    doc = _Doc(assembly, children=[component])
                else:
                    doc = _Doc(assembly, first_feature=datum)

                with self.assertRaises(CadError) as caught:
                    _read(root, _App({assembly: doc}))

                self.assertEqual(caught.exception.code, "cad_missing_coordinate_system")
                self.assertIn("CS_tip", f"{caught.exception.message} {caught.exception.detail}")

    def test_nested_component_datum_composes_every_parent_occurrence_offset(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            arm_doc = _Doc(
                arm_path,
                configuration="Tip",
                coordinate_systems={"CS_tip": _sw_translation(0.05, 0.0, 0.0)},
                first_feature=_Feature("CS_tip", "CoordSys"),
            )
            arm_component = _Component(
                "arm-1", arm_path, doc=arm_doc, transform=_sw_translation(0.0, 0.5, 0.0), configuration="Tip"
            )
            sub_doc = _Doc(
                sub_path,
                first_feature=None,
                children=[arm_component],
                configuration="Sub",
                configuration_children={"Sub": [arm_component]},
            )
            sub_component = _Component(
                "sub-1",
                sub_path,
                children=[arm_component],
                doc=sub_doc,
                transform=_sw_translation(1.0, 0.0, 0.0),
                configuration="Sub",
            )
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(
                assembly,
                children=[sub_component],
                configuration="Robot",
                coordinate_systems={"CS_main": _sw_translation(0.0, 0.0, 0.0)},
                first_feature=_Feature("CS_main", "CoordSys"),
            )

            record = _read(root, _App({assembly: main_doc, sub_path: sub_doc}))

            main_datum = next(datum for datum in record["datums"] if datum["owner"] == "")
            self.assertEqual(set(main_datum), {"name", "owner", "array", "configuration", "references"})
            self.assertEqual(main_datum["name"], "CS_main")
            self.assertEqual(main_datum["configuration"], "Robot")
            datums = [datum for datum in record["datums"] if datum["owner"] == "sub-1/arm-1"]
            self.assertEqual([datum["name"] for datum in datums], ["CS_tip"])
            self.assertEqual(datums[0]["configuration"], "Tip")
            matrix = datums[0]["array"]
            self.assertEqual([round(value, 10) for value in (matrix[0], matrix[5], matrix[10])], [1.0, 1.0, 1.0])
            # sub-1 at (1, 0, 0), arm-1 at (0, 0.5, 0), CS_tip at (0.05, 0, 0) in the arm frame.
            self.assertAlmostEqual(matrix[3], 1.05, msg=f"translation x not composed through parents: {matrix}")
            self.assertAlmostEqual(matrix[7], 0.5, msg=f"translation y not composed through parents: {matrix}")
            self.assertAlmostEqual(matrix[11], 0.0, msg=f"translation z not composed through parents: {matrix}")

    def test_repeated_referenced_configuration_is_selected_per_occurrence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            arm_doc = _Doc(arm_path)
            arm_left = _Component("arm-left", arm_path, doc=arm_doc)
            arm_right = _Component("arm-right", arm_path, doc=arm_doc)
            sub_doc = _Doc(
                sub_path,
                first_feature=None,
                configuration="Right",
                configuration_children={"Left": [arm_left], "Right": [arm_right]},
                configuration_properties={"Left": {"dp.role": "left"}, "Right": {"dp.role": "right"}},
            )
            occurrence_left = _Component("sub-a", sub_path, children=[arm_left], doc=sub_doc, configuration="Left")
            occurrence_right = _Component("sub-b", sub_path, children=[arm_right], doc=sub_doc, configuration="Right")
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, children=[occurrence_left, occurrence_right])

            record = _read(root, _App({assembly: main_doc, sub_path: sub_doc}))

            nested = sorted(item["name2"] for item in record["components"] if "/" in item["name2"])
            self.assertEqual(
                nested,
                ["sub-a/arm-left", "sub-b/arm-right"],
                msg="a shared sub-assembly must be shown in each occurrence's referenced configuration",
            )
            properties = record["properties"]["components"]
            self.assertEqual(properties["sub-a"]["dp.role"], "left")
            self.assertEqual(properties["sub-b"]["dp.role"], "right")

    def test_configuration_switch_false_return_blocks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            arm_doc = _Doc(arm_path)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            sub_doc = _Doc(
                sub_path,
                first_feature=None,
                configuration="Right",
                configuration_children={"Left": [arm_component], "Right": [arm_component]},
                show_success=False,
            )
            occurrence = _Component("sub-a", sub_path, children=[arm_component], doc=sub_doc, configuration="Left")
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, children=[occurrence])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: main_doc, sub_path: sub_doc}))
            self.assertEqual(caught.exception.code, "cad_configuration_unreadable")

    def test_configuration_active_mismatch_after_switch_blocks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            arm_doc = _Doc(arm_path)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            sub_doc = _Doc(
                sub_path,
                first_feature=None,
                configuration="Right",
                configuration_children={"Left": [arm_component], "Right": [arm_component]},
                show_effect=False,
            )
            occurrence = _Component("sub-a", sub_path, children=[arm_component], doc=sub_doc, configuration="Left")
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, children=[occurrence])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: main_doc, sub_path: sub_doc}))
            self.assertEqual(caught.exception.code, "cad_configuration_unreadable")

    def test_top_level_mate_resolves_exact_scoped_descendant_after_traversal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            arm_doc = _Doc(arm_path)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            sub_doc = _Doc(sub_path, first_feature=None, children=[arm_component])
            sub_component = _Component("sub-1", sub_path, children=[arm_component], doc=sub_doc)
            scoped_reference = types.SimpleNamespace(
                Name2="sub-1/arm-1",
                GetPathName=arm_component.GetPathName,
                ReferencedConfiguration="Default",
            )
            descendant_reference = _mate(
                "top_descendant",
                0,
                [_MateEntity(scoped_reference, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)))],
            )
            main_group = _Feature("MateGroup", "MateGroup", first_sub=descendant_reference)
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, first_feature=main_group, children=[sub_component])

            record = _read(root, _App({assembly: main_doc, sub_path: sub_doc}))

            self.assertEqual(len(record["mates"]), 1)
            mate = record["mates"][0]
            self.assertEqual(mate["name"], "top_descendant")
            self.assertEqual(mate["scope"], "")
            self.assertEqual(mate["entities"][0]["component"], "sub-1/arm-1")

    def test_top_level_leaf_only_descendant_reference_blocks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            arm_doc = _Doc(arm_path)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            sub_doc = _Doc(sub_path, first_feature=None, children=[arm_component])
            sub_component = _Component("sub-1", sub_path, children=[arm_component], doc=sub_doc)
            leaf_reference = types.SimpleNamespace(
                Name2="arm-1",
                GetPathName=arm_component.GetPathName,
                ReferencedConfiguration="Default",
            )
            leaf_mate = _mate(
                "ambiguous_descendant",
                0,
                [_MateEntity(leaf_reference, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)))],
            )
            main_group = _Feature("MateGroup", "MateGroup", first_sub=leaf_mate)
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, first_feature=main_group, children=[sub_component])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: main_doc, sub_path: sub_doc}))
            self.assertEqual(caught.exception.code, "cad_mate_scope_ambiguous")
            self.assertEqual(caught.exception.detail["component"], "arm-1")

    def test_duplicate_leaf_occurrences_resolve_by_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            arm_doc = _Doc(arm_path)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            seat = _mate("seat", 0, [_MateEntity(arm_component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)))])
            sub_group = _Feature("MateGroup", "MateGroup", first_sub=seat)
            sub_doc = _Doc(sub_path, first_feature=sub_group, children=[arm_component])
            occurrence_a = _Component("sub-a", sub_path, children=[arm_component], doc=sub_doc)
            occurrence_b = _Component("sub-b", sub_path, children=[arm_component], doc=sub_doc)
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, children=[occurrence_a, occurrence_b])

            record = _read(root, _App({assembly: main_doc, sub_path: sub_doc}))

            resolved = {
                mate["scope"]: mate["entities"][0]["component"] for mate in record["mates"] if mate["name"] == "seat"
            }
            self.assertEqual(
                resolved,
                {"sub-a": "sub-a/arm-1", "sub-b": "sub-b/arm-1"},
                msg="a shared leaf name must resolve through its occurrence path, never by leaf name alone",
            )


class SolverObservationTests(unittest.TestCase):
    """Context-bound solver readings: provenance, context binding, explicit unknowns."""

    def test_solver_observation_binds_defining_context_and_keeps_reference_distinct(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            assembly = _write(root, "robot.SLDASM")
            arm_doc = _Doc(arm_path)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            arm_component.Solving = 1
            arm_component.GetConstrainedStatus = lambda: 2
            sub_doc = _Doc(
                sub_path,
                children=[arm_component],
                configuration="Sub",
                configuration_children={"Sub": [arm_component]},
            )
            sub_component = _Component("sub-1", sub_path, children=[arm_component], doc=sub_doc, configuration="Sub")
            sub_component.Solving = 0
            sub_component.GetConstrainedStatus = lambda: 3
            main_doc = _Doc(assembly, children=[sub_component], configuration="Parent")

            record = _read(root, _App({assembly: main_doc, sub_path: sub_doc, arm_path: arm_doc}))
            entries = {item["name2"]: item for item in record["components"]}

            top = entries["sub-1"]
            # The referenced child configuration stays untouched and separate.
            self.assertEqual(top["configuration"], "Sub")
            self.assertIs(top["fixed"], False)
            self.assertEqual(
                top["solver_observation"]["context"],
                {"document": "robot.SLDASM", "configuration": "Parent", "scope": ""},
            )
            self.assertEqual(
                top["solver_observation"]["solving"],
                {"method": "IComponent2.Solving", "family": "swComponentSolvingOption_e", "value": 0, "error": None},
            )
            self.assertEqual(
                top["solver_observation"]["constrained_status"],
                {
                    "method": "IComponent2.GetConstrainedStatus",
                    "family": "swConstrainedStatus_e",
                    "value": 3,
                    "error": None,
                },
            )

            nested = entries["sub-1/arm-1"]
            self.assertEqual(
                nested["solver_observation"]["context"],
                {"document": "sub.SLDASM", "configuration": "Sub", "scope": "sub-1"},
            )
            self.assertEqual(nested["solver_observation"]["solving"]["value"], 1)
            self.assertEqual(nested["solver_observation"]["constrained_status"]["value"], 2)

    def test_solver_observation_keeps_status_three_and_fixed_distinct(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            wrench = _write(root, "wrench.SLDPRT")
            grounded = _Component("base-1", base)
            grounded._fixed = True
            grounded.Solving = 0
            grounded.GetConstrainedStatus = lambda: 3
            free = _Component("wrench-1", wrench)
            free._fixed = False
            free.Solving = 0
            free.GetConstrainedStatus = lambda: 3
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, children=[grounded, free])

            record = _read(root, _App({assembly: doc}))
            entries = {item["name2"]: item for item in record["components"]}

            self.assertIs(entries["base-1"]["fixed"], True)
            self.assertIs(entries["wrench-1"]["fixed"], False)
            # Equal solver status does not collapse the fixed distinction and the
            # observation never overwrites the recorded IsFixed value.
            self.assertEqual(entries["base-1"]["solver_observation"]["constrained_status"]["value"], 3)
            self.assertEqual(entries["wrench-1"]["solver_observation"]["constrained_status"]["value"], 3)

    def test_non_boolean_is_fixed_is_preserved_as_an_explicit_error(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            component._fixed = 1
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, children=[component])

            record = _read(root, _App({assembly: doc}))
            entry = record["components"][0]

            self.assertIsNone(entry["fixed"])
            self.assertEqual(entry["fixed_error"]["code"], "cad_component_fixed_invalid")
            self.assertEqual(entry["fixed_error"]["detail"]["value_type"], "int")

    def test_solver_observation_preserves_unreadable_readings_without_failing(self) -> None:
        class _RaisingSolving(_Component):
            @property
            def Solving(self):
                raise RuntimeError("native property unavailable")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            part = _write(root, "part.SLDPRT")
            raising = _RaisingSolving("raising-1", part)
            raising.GetConstrainedStatus = _raiser(RuntimeError("status read failed"))
            junk = _Component("junk-1", part)
            junk.Solving = "not-an-int"
            junk.GetConstrainedStatus = lambda: None
            missing = _Component("missing-1", part)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, children=[raising, junk, missing])

            record = _read(root, _App({assembly: doc}))
            entries = {item["name2"]: item for item in record["components"]}

            raised = entries["raising-1"]["solver_observation"]
            self.assertIsNone(raised["solving"]["value"])
            self.assertIn("native property unavailable", raised["solving"]["error"])
            self.assertIsNone(raised["constrained_status"]["value"])
            self.assertIn("status read failed", raised["constrained_status"]["error"])

            junk_top = entries["junk-1"]["solver_observation"]
            self.assertIsNone(junk_top["solving"]["value"])
            self.assertIn("non-integer reading", junk_top["solving"]["error"])
            self.assertIsNone(junk_top["constrained_status"]["value"])
            self.assertIn("non-integer reading", junk_top["constrained_status"]["error"])

            missing_top = entries["missing-1"]["solver_observation"]
            self.assertIsNone(missing_top["solving"]["value"])
            self.assertTrue(missing_top["solving"]["error"])
            self.assertIsNone(missing_top["constrained_status"]["value"])
            self.assertTrue(missing_top["constrained_status"]["error"])

    def test_record_normalization_preserves_solver_observation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            component.Solving = 0
            component.GetConstrainedStatus = lambda: 3
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, children=[component])

            record = _read(root, _App({assembly: doc}))
            target = root / "native-discovery.json"
            write_json(target, record)
            normalized = read_json(target)

            self.assertEqual(normalized, record)
            self.assertEqual(normalized["components"][0]["solver_observation"]["constrained_status"]["value"], 3)


class CaptureSceneTests(unittest.TestCase):
    def test_capture_binds_embedded_virtual_documents_in_raw_source_scopes(self) -> None:
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            virtual_path = (
                root
                / "swx21040"
                / "VC~~"
                / "waist"
                / "Copy of 复件 (2) 零件10^waist.SLDPRT"
            )
            virtual_path.parent.mkdir(parents=True)
            virtual_path.write_bytes(b"engine virtual document")
            virtual = _Component(
                "Copy of 复件 (2) 零件10^waist-1",
                virtual_path,
                doc=_Doc(virtual_path, configuration="默认"),
                configuration="默认",
            )
            virtual.IsVirtual = True
            virtual.GetImportedPath = lambda: None
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[virtual])
            backend = _CaptureBackend(session_factory=lambda: _Session(_App({assembly: main})))
            scene = backend.capture_scene_for_test(str(assembly), [])

            self.assertIn(str(virtual_path), {component.path for component in scene.components})
            scopes = backend.source_scopes([virtual.Name2])
            scope = scopes[virtual.Name2]
            self.assertEqual(
                scope["source_document_identity_kind"],
                "virtual_path_sha256",
            )
            self.assertEqual(
                scope["virtual_owner"],
                "robot.SLDASM",
            )
            self.assertEqual(
                scope["virtual_session_document_path"].replace(
                    "\\",
                    "/",
                ),
                str(virtual_path).lower().replace("\\", "/"),
            )
            self.assertEqual(
                scope["virtual_session_temp_root"],
                str(virtual_path.parent.parent.parent),
            )
            self.assertEqual(scope["virtual_native_api_pid"], 21040)
            backend._source_provenance = {}
            with self.assertRaises(CadError):
                backend.source_scopes([virtual.Name2])

    def test_capture_source_scopes_fail_closed_for_missing_frozen_file(self) -> None:
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "part.SLDPRT")
            component = _Component("part-1", part)
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[component])
            backend = _CaptureBackend(
                session_factory=lambda: _Session(
                    _App({assembly: main, part: _Doc(part)})
                )
            )
            backend.capture_scene_for_test(str(assembly), [])
            part.unlink()

            with self.assertRaises(CadError):
                backend.source_scopes([component.Name2])

    def test_read_only_mutation_waits_until_all_occurrence_primitives_are_collected(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            events = []
            components = []

            class PartDocument(_Doc):
                def __init__(self, path):
                    super().__init__(path)
                    self.read_only = False

                def IsOpenedReadOnly(self):
                    return self.read_only

                def SetReadOnlyState(self, value):
                    events.append(("read_only", self._path))
                    # A native mutation may revoke any borrowed occurrence.
                    # The algorithm must finish the tree before crossing it.
                    for comp in components:
                        comp.revoked = True
                    self.read_only = value
                    return True

            class Occurrence(_Component):
                revoked = False

                def GetPathName(self):
                    if self.revoked:
                        raise RuntimeError("Borrowed occurrence was revoked by a native mutation")
                    events.append(("path", self.Name2))
                    return super().GetPathName()

            documents = {}
            for name in ("arm1", "arm2"):
                path = _write(root, name + ".SLDPRT")
                doc = PartDocument(path)
                documents[path] = doc
                components.append(Occurrence(name, path, doc=doc))
            assembly = _write(root, "robot.SLDASM")
            documents[assembly] = _Doc(assembly, doc_type=2, children=components)
            backend = _CaptureBackend(session_factory=lambda: _Session(_App(documents)))
            scene = backend.capture_scene_for_test(str(assembly), [])

            self.assertEqual({c.name for c in scene.components}, {"arm1", "arm2"})
            paths = [i for i, event in enumerate(events) if event[0] == "path"]
            mutations = [i for i, event in enumerate(events) if event[0] == "read_only"]
            self.assertEqual(len(paths), 2)
            self.assertEqual(len(mutations), 2)
            self.assertLess(max(paths), min(mutations))
            self.assertTrue(all(doc.read_only for path, doc in documents.items() if path != assembly))

    def test_owned_document_lookup_does_not_reuse_a_revoked_application_interface(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            part = _write(Path(tmp), "arm.SLDPRT")
            document = _Doc(part)
            previous = _App({})
            previous.GetOpenDocumentByName = _raiser(RuntimeError("RPC_S_UNKNOWN_IF"))
            current = _App({str(part): document})
            process = types.SimpleNamespace(pid=42, executable="test.exe", alive=lambda: True, close=Mock())
            binder = Mock(side_effect=[previous, current])
            session = CadSession(process=process, binder=binder)
            session.connect(threading.Event())
            backend = SolidWorksBackend()
            backend._sessions["source"] = session
            backend._owner_thread = threading.current_thread()

            self.assertIs(backend._document_by_path(str(part)), document)
            self.assertIs(backend._sessions["source"], session)
            self.assertEqual([call.args for call in binder.call_args_list], [(42,), (42,)])
            process.close.assert_not_called()
            backend.release()

    """Requested datums must survive capture from every occurrence/document."""

    def _capture(self, app, assembly, requested):
        backend = _CaptureBackend(session_factory=lambda: _Session(app))
        with _com_stubs():
            return backend.capture_scene_for_test(str(assembly), requested)

    def _configured_occurrences(self, root, *, nested=False):
        class ConfiguredDocument(_Doc):
            def GetTessTriangles(self, _quality):
                raise AssertionError("occurrence geometry must not read the shared document")

        class ConfiguredComponent(_Component):
            def GetBodies3(self, body_type, body_info):
                if body_type != 0:
                    body_info.value = []
                    return []
                if not hasattr(self, "_configured_body"):
                    triangles = {
                        "Short": [0, 0, 0, 1, 0, 0, 0, 1, 0],
                        "Long": [0, 0, 0, 2, 0, 0, 0, 2, 0, 2, 0, 0, 2, 2, 0, 0, 2, 0],
                    }[self.ReferencedConfiguration]
                    face = _mesh_face(
                        lambda _quality, values=triangles: values,
                        self.ReferencedConfiguration,
                    )
                    self._configured_body = _mesh_body(
                        [face],
                        self.ReferencedConfiguration,
                    )
                body_info.value = [1]
                return [self._configured_body]

        class ConfiguredBackend(_CaptureBackend):
            def _mass_properties_document(self, doc, require_material=True):
                return {
                    "mass_kg": {"Short": 2.0, "Long": 3.0}[doc.active_configuration],
                    "reference": {
                        "configuration": doc.active_configuration,
                        "used_api": "portable-config-mass",
                    },
                }

        part = _write(root, "shared.SLDPRT")
        shared = ConfiguredDocument(
            part,
            configuration="Parked",
            configuration_children={"Short": [], "Long": [], "Parked": []},
        )
        short_name = "sub-1/part-1" if nested else "part-short"
        long_name = "sub-2/part-1" if nested else "part-long"
        short = ConfiguredComponent(short_name, part, doc=shared, configuration="Short")
        long = ConfiguredComponent(long_name, part, doc=shared, configuration="Long")
        children = [short, long]
        if nested:
            children = []
            for index, leaf in enumerate((short, long), 1):
                sub_path = _write(root, f"sub{index}.SLDASM")
                sub_doc = _Doc(sub_path, doc_type=2, children=[leaf])
                children.append(_Component(f"sub-{index}", sub_path, children=[leaf], doc=sub_doc))
        assembly = _write(root, "robot.SLDASM")
        main = _Doc(assembly, doc_type=2, children=children)
        backend = ConfiguredBackend(session_factory=lambda: _Session(_App({assembly: main})))
        scene = backend.capture_scene_for_test(str(assembly), [])
        return backend, scene, shared, short, part

    def test_capture_does_not_reuse_occurrence_interfaces_after_configuration_switches(self):
        borrowed_occurrences = []

        class SharedDocument(_Doc):
            generation = 0

            def ShowConfiguration2(self, name):
                previous = self.active_configuration
                if name != previous and any(reference() is not None for reference in borrowed_occurrences):
                    raise RuntimeError("borrowed occurrences remain alive across configuration selection")
                selected = super().ShowConfiguration2(name)
                if selected and previous != self.active_configuration:
                    self.generation += 1
                return selected

        class ConfiguredBackend(_CaptureBackend):
            def _mass_properties_document(self, doc, require_material=True):
                mass = {"Short": 2.0, "Long": 3.0}[doc.active_configuration]
                return {
                    "mass": mass,
                    "com": (0.0, 0.0, 0.0),
                    "native_raw": {
                        "mass_kg": mass,
                        "center_of_mass_m": [0.0, 0.0, 0.0],
                        "inertia_values": [
                            1e-6,
                            0.0,
                            0.0,
                            0.0,
                            1e-6,
                            0.0,
                            0.0,
                            0.0,
                            1e-6,
                        ],
                        "overrides": {
                            "OverrideMass": False,
                            "OverrideCenterOfMass": False,
                            "OverrideMomentsOfInertia": False,
                        },
                    },
                    "read_errors": {},
                    "reference": {"used_api": "portable-config-mass", "configuration": doc.active_configuration},
                }

            def _body_count(self, holder, body_type, component):
                return _component_body_count(holder, body_type)

        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            path = _write(root, "shared.SLDPRT")
            assembly = _write(root, "robot.SLDASM")
            shared = SharedDocument(
                path,
                configuration="Parked",
                configuration_children={"Short": [], "Long": [], "Parked": []},
            )
            expired_reads = []

            class BorrowedOccurrence:
                def __init__(self, inner):
                    self.inner = inner
                    self.generation = shared.generation
                    borrowed_occurrences.append(weakref.ref(self))

                def __getattr__(self, name):
                    if not name.startswith("_") and self.generation != shared.generation:
                        expired_reads.append(name)
                        raise RuntimeError("occurrence interface expired after shared configuration selection")
                    return getattr(self.inner, name)

            main = _Doc(assembly, doc_type=2)

            def current_occurrences(_configuration):
                result = []
                for name, configuration, count in (("part-short", "Short", 1), ("part-long", "Long", 2)):
                    component = _Component(name, path, doc=shared, configuration=configuration)
                    component.GetBodies3 = _component_bodies3(
                        lambda body_type, count=count: (
                            [object()] * count if body_type == 0 else []
                        )
                    )
                    result.append(BorrowedOccurrence(component))
                return result

            main.children_for = current_occurrences
            backend = ConfiguredBackend(session_factory=lambda: _Session(_App({assembly: main, path: shared})))
            scene = backend.capture_scene_for_test(str(assembly), [])
            self.assertEqual(scene.mass_properties["part-short"]["mass"], 2.0)
            self.assertEqual(scene.mass_properties["part-long"]["mass"], 3.0)
            self.assertEqual(
                scene.notes["bodies:part-short"],
                {"solid": 1, "sheet": 0, "errors": {}},
            )
            self.assertEqual(
                scene.notes["bodies:part-long"],
                {"solid": 2, "sheet": 0, "errors": {}},
            )
            self.assertEqual(shared.active_configuration, "Parked")
            self.assertGreater(shared.generation, 0)
            self.assertEqual(expired_reads, [])

    def test_leaf_mass_failure_names_the_occurrence_and_recorded_body_counts(self):
        """A failed leaf mass read keeps its classification and gains context.

        Regression for the native7 ``cad_empty_model`` capture failure that
        carried ``detail: null``: the next capture must name the occurrence,
        its document, the referenced configuration and the already-recorded
        native solid/sheet counts without re-probing or skipping anything.
        """

        class FailingBackend(_CaptureBackend):
            def _body_count(self, holder, body_type, component):
                return _component_body_count(holder, body_type)

            def _mass_properties_document(self, doc, require_material=True):
                raise CadError("cad_empty_model", "part has no solid bodies", {"marker": "kept"})

            def assembly_group_mass_properties(self, path, names, *, allow_overrides=False):
                raise CadError("cad_group_unavailable", "no assembly context here")

        class SheetOnlyComponent(_Component):
            def GetBodies3(self, body_type, body_info):
                return _component_bodies3(
                    lambda value: [] if value == 0 else [object()]
                )(body_type, body_info)

        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "empty.SLDPRT")
            shared = _Doc(part, configuration="Default", configuration_children={"Default": []})
            component = SheetOnlyComponent("sub-1/empty-1", part, doc=shared, configuration="Default")
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[component])
            backend = FailingBackend(session_factory=lambda: _Session(_App({assembly: main, part: shared})))
            with self.assertRaises(CadError) as caught:
                backend.capture_scene_for_test(str(assembly), [])
        error = caught.exception
        self.assertEqual(error.code, "cad_empty_model")
        self.assertEqual(error.message, "part has no solid bodies")
        self.assertEqual(error.detail["marker"], "kept")
        self.assertEqual(error.detail["component"], "sub-1/empty-1")
        self.assertEqual(error.detail["document"], str(part))
        self.assertEqual(error.detail["referenced_configuration"], "Default")
        self.assertEqual(
            error.detail["bodies"],
            {"solid": 0, "sheet": 1, "errors": {}},
        )
        self.assertEqual(error.detail["phase"], "leaf_mass_read")
        self.assertIn(
            "no assembly context here",
            backend.notes["mass_error:sub-1/empty-1"]["fallback_error"],
        )

    def test_sheet_only_fallback_records_the_effective_instance_row(self):
        class FallbackBackend(_CaptureBackend):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.group_calls = []

            def _body_count(self, holder, body_type, component):
                return _component_body_count(holder, body_type)

            def _mass_properties_document(self, doc, require_material=True):
                raise CadError("cad_empty_model", "part has no solid bodies")

            def assembly_group_mass_properties(self, path, names, *, allow_overrides=False):
                self.group_calls.append((str(path), tuple(names), allow_overrides))
                flags = {
                    "OverrideMass": True,
                    "OverrideCenterOfMass": True,
                    "OverrideMomentsOfInertia": True,
                }
                return {
                    "assembly": str(path),
                    "configuration": "Default",
                    "group": list(names),
                    "members": {names[0]: flags},
                    "native_raw": {
                        "mass_kg": 0.026,
                        "center_of_mass_m": [0.0, 0.0, 0.05],
                        "inertia_values": [
                            1e-6,
                            1e-8,
                            0.0,
                            1e-8,
                            1.2e-6,
                            0.0,
                            0.0,
                            0.0,
                            2e-6,
                        ],
                        "volume_m3": 3.3e-6,
                        "density_kg_m3": 7878.0,
                        "overrides": flags,
                    },
                    "read_errors": {},
                    "reference": {
                        "used_api": "IMassProperty2.GetMomentOfInertia(0)",
                        "selection_api": "IMassProperty2 (SelectedItems = component instance)",
                        "override_api": "IMassProperty2.GetOverrideOptions (same selection)",
                        "scope": "assembly_component_group",
                        "convention_basis": "analytic_fixture_v2_20261006",
                        "product_convention": "solidworks_standard",
                        "reference_point": "center_of_mass",
                        "axes": "assembly_document_axes",
                        "use_system_units": True,
                        "effective_instance": True,
                        "measurement_basis": "cad_authored_effective_instance",
                        "volume_m3": 3.3e-6,
                        "density_kg_m3": 7878.0,
                    },
                }

        class SheetOnlyComponent(_Component):
            def GetBodies3(self, body_type, body_info):
                return _component_bodies3(
                    lambda value: (
                        [] if value == 0 else [object()] * 2350
                    )
                )(body_type, body_info)

        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "EasyEDA PCB Model~Nz70.SLDPRT")
            shared = _Doc(part, configuration="Default", configuration_children={"Default": []})
            component = SheetOnlyComponent("part-sheet", part, doc=shared, configuration="Default")
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[component])
            backend = FallbackBackend(session_factory=lambda: _Session(_App({assembly: main, part: shared})))
            scene = backend.capture_scene_for_test(str(assembly), [])
        row = scene.mass_properties["part-sheet"]
        self.assertEqual(row["mode"], "effective_instance")
        self.assertEqual(row["state"], "sheet_only")
        self.assertEqual(row["measurement_basis"], "cad_authored_effective_instance")
        self.assertEqual(row["mass"], 0.026)
        self.assertEqual(row["com"], [0.0, 0.0, 0.05])
        reference = row["reference"]
        self.assertEqual(reference["used_api"], "IMassProperty2.GetMomentOfInertia(0)")
        self.assertEqual(reference["selection_api"], "IMassProperty2 (SelectedItems = component instance)")
        self.assertEqual(reference["override_api"], "IMassProperty2.GetOverrideOptions (same selection)")
        self.assertEqual(reference["scope"], "assembly_component_group")
        self.assertEqual(reference["axes"], "assembly_document_axes")
        self.assertEqual(reference["selected_components"], ["part-sheet"])
        self.assertEqual(reference["part_document"], str(part))
        self.assertEqual(reference["assembly_document"], str(assembly))
        self.assertEqual(reference["assembly_configuration"], "Default")
        self.assertEqual(reference["configuration"], "Default")
        self.assertEqual(reference["fallback_error_code"], "cad_empty_model")
        self.assertEqual(reference["fallback_reason"], "cad_part_document_mass_unavailable")
        self.assertEqual(reference["solid_bodies"], 0)
        self.assertEqual(reference["sheet_bodies"], 2350)
        self.assertEqual(
            reference["overrides"],
            {"OverrideMass": True, "OverrideCenterOfMass": True, "OverrideMomentsOfInertia": True},
        )
        self.assertEqual(reference["volume_m3"], 3.3e-6)
        self.assertEqual(reference["density_kg_m3"], 7878.0)
        self.assertEqual(scene.notes["effective_instance:part-sheet"], "cad_authored_effective_instance")
        self.assertEqual(backend.group_calls, [(str(assembly), ("part-sheet",), True)])

    def test_no_bodies_at_all_still_fails_without_a_fallback(self):
        class NoBodyBackend(_CaptureBackend):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.group_calls = 0

            def _body_count(self, holder, body_type, component):
                return _component_body_count(holder, body_type)

            def _mass_properties_document(self, doc, require_material=True):
                raise CadError("cad_empty_model", "part has no solid bodies")

            def assembly_group_mass_properties(self, path, names, *, allow_overrides=False):
                self.group_calls += 1
                raise AssertionError("no fallback may be attempted without sheet bodies")

        class EmptyComponent(_Component):
            def GetBodies3(self, body_type, body_info):
                return _component_bodies3(lambda _value: [])(body_type, body_info)

        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "empty.SLDPRT")
            shared = _Doc(part, configuration="Default", configuration_children={"Default": []})
            component = EmptyComponent("part-empty", part, doc=shared, configuration="Default")
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[component])
            backend = NoBodyBackend(session_factory=lambda: _Session(_App({assembly: main, part: shared})))
            with self.assertRaises(CadError) as caught:
                backend.capture_scene_for_test(str(assembly), [])
        error = caught.exception
        self.assertEqual(error.code, "cad_empty_model")
        self.assertEqual(error.detail["component"], "part-empty")
        self.assertEqual(
            error.detail["bodies"],
            {"solid": 0, "sheet": 0, "errors": {}},
        )
        self.assertNotIn("fallback_error", error.detail)
        self.assertEqual(backend.group_calls, 0)

    def _expiring_document(self, root, *, foreign_replacement=False):
        part = _write(root, "shared.SLDPRT")
        replacement_path = _write(root, "foreign.SLDPRT") if foreign_replacement else part
        replacement = _Doc(
            replacement_path,
            configuration="Short",
            configuration_children={"Short": [], "Parked": []},
        )
        app = _App({})

        class ExpiringDocument(_Doc):
            expired = False

            def __getattribute__(self, name):
                if name == "ConfigurationManager" and object.__getattribute__(self, "expired"):
                    raise RuntimeError("RPC_S_UNKNOWN_IF: stale document interface")
                return super().__getattribute__(name)

            def GetTessTriangles(self, _quality):
                self.assert_configuration = self.active_configuration
                app._docs[os.path.normcase(os.path.abspath(str(part)))] = replacement
                component._doc = replacement
                self.expired = True
                return [0, 0, 0, 1, 0, 0, 0, 1, 0]

        original = ExpiringDocument(
            part,
            configuration="Parked",
            configuration_children={"Short": [], "Parked": []},
        )
        app._docs[os.path.normcase(os.path.abspath(str(part)))] = original
        component = _Component("part-short", part, doc=original, configuration="Short")
        backend = SolidWorksBackend(session_factory=lambda: _Session(app))
        backend._components.add("part-short")
        return backend, original, replacement

    def test_configuration_window_restores_through_fresh_owned_document_handle(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, original, replacement = self._expiring_document(root)
            path = str(root / "shared.SLDPRT")
            with _temporary_configuration(lambda: backend._document_by_path(path), "Short", "part-short") as (
                document,
                _previous,
            ):
                document.GetTessTriangles(True)

            self.assertTrue(original.expired)
            self.assertEqual(original.assert_configuration, "Short")
            self.assertEqual(replacement.active_configuration, "Parked")

    def test_configuration_restore_refuses_replacement_from_another_document(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, original, replacement = self._expiring_document(root, foreign_replacement=True)
            path = str(root / "shared.SLDPRT")

            with (
                self.assertRaises(CadError) as caught,
                _temporary_configuration(lambda: backend._document_by_path(path), "Short", "part-short") as (
                    document,
                    _previous,
                ),
            ):
                document.GetTessTriangles(True)

            self.assertTrue(original.expired)
            self.assertEqual(caught.exception.code, "document_not_open")
            self.assertEqual(caught.exception.detail["phase"], "restore")
            self.assertEqual(replacement.active_configuration, "Short")

    def test_failed_configuration_restore_does_not_mask_a_read_failure(self):
        class RestoreFailure(_Doc):
            def ShowConfiguration2(self, name):
                return False if name == "Parked" else super().ShowConfiguration2(name)

        for fail_read in (False, True):
            with self.subTest(fail_read=fail_read), _com_stubs():
                doc = RestoreFailure(
                    "shared.SLDPRT",
                    configuration="Parked",
                    configuration_children={"Short": [], "Parked": []},
                )
                read_error = CadError("cad_mesh_export_failed", "original reading failure")
                with (
                    self.assertRaises(CadError) as caught,
                    _temporary_configuration(lambda document=doc: document, "Short", "part-short"),
                ):
                    if fail_read:
                        raise read_error
                if fail_read:
                    self.assertIs(caught.exception, read_error)
                    self.assertIn("restoration also failed", caught.exception.__notes__[0])
                else:
                    self.assertEqual(caught.exception.code, "cad_configuration_unreadable")
                    self.assertEqual(caught.exception.detail["phase"], "restore")

    def test_component_owned_datum_composes_with_occurrence_placement(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            arm_doc = _Doc(
                arm_path,
                coordinate_systems={"CS_tip": _sw_translation(0.1, 0.0, 0.0)},
                first_feature=_Feature("CS_tip", "CoordSys"),
            )
            arm_component = _Component("arm-1", arm_path, doc=arm_doc, transform=_sw_translation(1.0, 2.0, 0.0))
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, doc_type=2, children=[arm_component])

            scene = self._capture(_App({assembly: main_doc}), assembly, ["CS_tip"])

            self.assertEqual([component.name for component in scene.components], ["arm-1"])
            matrix = scene.coordinate_systems["CS_tip"]
            self.assertAlmostEqual(matrix[3], 1.1, msg=f"datum not composed with the occurrence: {matrix}")
            self.assertAlmostEqual(matrix[7], 2.0, msg=f"datum not composed with the occurrence: {matrix}")
            self.assertAlmostEqual(matrix[11], 0.0, msg=f"datum not composed with the occurrence: {matrix}")

    def test_repeated_reference_uses_each_referenced_configuration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            arm_doc = _Doc(
                arm_path,
                configuration="Left",
                configuration_children={"Left": [], "Right": []},
                coordinate_systems={
                    "CS_left": _sw_translation(0.5, 0.0, 0.0),
                    "CS_right": _sw_translation(0.0, 0.5, 0.0),
                },
            )
            # SolidWorks retains suppressed features in the tree, and a stored
            # transform can remain readable. Selection must use suppression.
            right_datum = _Feature("CS_right", "CoordSys", suppressed=lambda: arm_doc.active_configuration != "Right")
            arm_doc._first_feature = _Feature(
                "CS_left",
                "CoordSys",
                suppressed=lambda: arm_doc.active_configuration != "Left",
                next_feature=right_datum,
            )
            left = _Component(
                "arm-left", arm_path, doc=arm_doc, configuration="Left", transform=_sw_translation(1.0, 0.0, 0.0)
            )
            right = _Component(
                "arm-right", arm_path, doc=arm_doc, configuration="Right", transform=_sw_translation(0.0, 2.0, 0.0)
            )
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, doc_type=2, children=[left, right])

            scene = self._capture(_App({assembly: main_doc}), assembly, ["CS_left", "CS_right"])

            left_matrix = scene.coordinate_systems["CS_left"]
            right_matrix = scene.coordinate_systems["CS_right"]
            self.assertAlmostEqual(left_matrix[3], 1.5, msg=f"left datum wrong: {left_matrix}")
            self.assertAlmostEqual(left_matrix[7], 0.0, msg=f"left datum wrong: {left_matrix}")
            self.assertAlmostEqual(right_matrix[3], 0.0, msg=f"right datum wrong: {right_matrix}")
            self.assertAlmostEqual(right_matrix[7], 2.5, msg=f"right datum wrong: {right_matrix}")

    def test_missing_datums_are_diagnostic_and_duplicate_unqualified_datums_fail(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            arm_doc = _Doc(arm_path)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, doc_type=2, children=[arm_component])
            scene = self._capture(
                _App({assembly: main_doc}),
                assembly,
                ["CS_missing"],
            )
            self.assertEqual(scene.coordinate_systems, {})
            self.assertEqual(
                scene.notes["missing_coordinate_systems"],
                ["CS_missing"],
            )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_path = _write(root, "first.SLDPRT")
            second_path = _write(root, "second.SLDPRT")
            first_doc = _Doc(
                first_path,
                coordinate_systems={"CS_tip": _sw_translation(0.1, 0.0, 0.0)},
                first_feature=_Feature("CS_tip", "CoordSys"),
            )
            second_doc = _Doc(
                second_path,
                coordinate_systems={"CS_tip": _sw_translation(0.0, 0.1, 0.0)},
                first_feature=_Feature("CS_tip", "CoordSys"),
            )
            first = _Component("first-1", first_path, doc=first_doc)
            second = _Component("second-1", second_path, doc=second_doc)
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, doc_type=2, children=[first, second])
            with self.assertRaises(CadError) as caught:
                self._capture(_App({assembly: main_doc}), assembly, ["CS_tip"])
            error = caught.exception
            self.assertEqual(error.code, "cad_coordinate_system_ambiguous")
            self.assertEqual(error.detail["datum"], "CS_tip")


class AppearanceRawTests(_FixtureAppearanceInterfaceMixin, unittest.TestCase):
    def test_component_body_info_must_align_with_each_returned_body(self) -> None:
        class Component:
            @staticmethod
            def _FlagAsMethod(_name):
                return None

            @staticmethod
            def GetBodies3(_body_type, body_info):
                body_info.value = [1, 0]
                return [object()]

        with _com_stubs(), self.assertRaises(CadError) as caught:
            SolidWorksBackend()._component_bodies(
                Component(),
                0,
                "part-1",
            )
        self.assertEqual(
            caught.exception.code,
            "cad_component_body_info_invalid",
        )

    def test_capture_records_document_occurrence_body_and_face_appearance_raw(self) -> None:
        occurrence_calls = []
        body_type_calls = []
        face_enumeration_calls = []
        body_appearance_calls = []
        part_render_material_calls = []
        part_render_material_count_calls = []
        top_render_material_calls = []
        top_render_material_count_calls = []
        texture_calls = []
        document_values = [0.1, 0.2, 0.3, 0.2, 0.4, 0.5, 0.6, 0.0, 0.0]
        occurrence_values = [-1.0] * 9
        face_values = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]

        with TemporaryDirectory() as tmp, _com_stubs():
            fixture_root = Path(tmp)
            part = _write(fixture_root, "arm.SLDPRT")
            part_document = _Doc(
                part,
                configuration="Painted",
                configuration_children={"Painted": []},
            )
            document_texture = types.SimpleNamespace(MaterialName="C:\\textures\\paint.jpg")
            part_document.MaterialPropertyValues = list(document_values)
            part_document.Extension.GetMaterialPropertyValues = _raiser(
                AssertionError("document material must use IModelDoc2 property")
            )
            part_document.Extension.HasMaterialPropertyValues = lambda: True
            part_document.Extension.GetTexture = (
                lambda configuration: texture_calls.append(
                    ("document", configuration)
                )
                or document_texture
            )

            face = _mesh_face(
                lambda _quality: [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
                "appearance",
            )
            face._persist_reference = b"assembly-face-alias"
            face._fixture_interfaces = {"IEntity"}
            material_face = _mesh_face(
                lambda _quality: [0.0] * 9,
                "material-document",
            )
            material_face._persist_reference = b"appearance"
            material_face._fixture_interfaces = {"IEntity", "IFace2"}
            face_feature = types.SimpleNamespace(
                Name="PaintedFace",
                GetTypeName2=lambda: "Extrusion",
                _persist_reference=b"feature",
                _fixture_interfaces={"IFeature"},
            )
            converted_feature_reads = []
            face.GetFeature = _raiser(
                AssertionError(
                    "features must be derived from the converted Part face"
                )
            )
            material_face.GetFeature = (
                lambda: converted_feature_reads.append(material_face)
                or face_feature
            )
            face_texture = types.SimpleNamespace(
                MaterialName="C:\\textures\\face.jpg"
            )
            face.MaterialPropertyValues = list(face_values)
            face.GetMaterialPropertyValues2 = _raiser(
                AssertionError(
                    "face material must use IFace2.MaterialPropertyValues"
                )
            )
            face.HasMaterialPropertyValues = lambda: True
            face.GetTexture = (
                lambda configuration: texture_calls.append(
                    ("face", configuration)
                )
                or face_texture
            )
            render_material = types.SimpleNamespace(
                PrimaryColor=0x00AABBCC,
                Transparency=0.25,
                ColorForm=1,
                TextureFilename="",
                SurfaceFinishFile="C:\\finish.p2m",
                GetLinkedDisplayStates=lambda: ["FaceState"],
                GetEntities=lambda: [material_face],
            )
            incomplete_render_material = types.SimpleNamespace(
                GetLinkedDisplayStates=list,
                GetEntities=list,
            )
            part_document.Extension.GetRenderMaterials2 = (
                lambda option, names: part_render_material_calls.append(
                    (option, names)
                )
                or [render_material, incomplete_render_material]
            )
            part_document.Extension.GetRenderMaterialsCount2 = (
                lambda option, names: part_render_material_count_calls.append(
                    (option, names)
                )
                or 2
            )
            body = _mesh_body([face], "appearance")
            underlying_body = types.SimpleNamespace(
                _persist_reference=b"part-body",
                _fixture_interfaces={"IBody2"},
            )
            material_face.GetBody = lambda: underlying_body
            body_faces = body.GetFaces

            def get_faces():
                face_enumeration_calls.append(True)
                return body_faces()

            body.GetFaces = get_faces

            class BodyView:
                def __init__(self, value):
                    self._value = value

                def __getattr__(self, name):
                    if name in {
                        "MaterialPropertyValues2",
                        "HasMaterialPropertyValues",
                        "GetTexture",
                    }:
                        body_appearance_calls.append(name)
                        raise AssertionError(
                            "IComponent2.GetBodies3 bodies are not the documented "
                            "IBody2.MaterialPropertyValues2 owner path"
                        )
                    return getattr(self._value, name)

            body_view = BodyView(body)
            component = _Component(
                "arm-1",
                part,
                doc=part_document,
                configuration="Painted",
            )
            face._fixture_component = component
            component.ReferencedDisplayState2 = "FaceState"

            def get_bodies(body_type, body_info):
                import pythoncom

                self.assertEqual(
                    body_info.varianttype,
                    pythoncom.VT_BYREF | pythoncom.VT_VARIANT,
                )
                body_type_calls.append(body_type)
                bodies = [body_view] if body_type == 0 else []
                body_info.value = [1] * len(bodies)
                return bodies

            component.GetBodies3 = get_bodies
            component.GetMaterialPropertyValues2 = (
                lambda option, names: occurrence_calls.append((option, names))
                or list(occurrence_values)
            )
            component.HasMaterialPropertyValues = lambda: False
            component.GetTexture = (
                lambda configuration: texture_calls.append(
                    ("occurrence", configuration)
                )
                or None
            )
            component.GetCorrespondingEntity = _raiser(
                AssertionError(
                    "component-document appearance must stay in its owner namespace"
                )
            )
            component.GetCorresponding = lambda entity: entity
            assembly = _write(fixture_root, "robot.SLDASM")
            main = _Doc(
                assembly,
                doc_type=2,
                children=[component],
                display_states=["TopState"],
            )
            main_reference = main.Extension.GetPersistReference3

            def main_persistent_reference(entity):
                if entity is material_face:
                    raise RuntimeError(
                        "document entity has no assembly-context reference"
                    )
                return main_reference(entity)

            main.Extension.GetPersistReference3 = main_persistent_reference
            main.Extension.GetRenderMaterials2 = (
                lambda option, names: top_render_material_calls.append(
                    (option, names)
                )
                or []
            )
            main.Extension.GetRenderMaterialsCount2 = (
                lambda option, names: top_render_material_count_calls.append(
                    (option, names)
                )
                or 0
            )
            main.Extension.GetCorrespondingEntity2 = _raiser(
                AssertionError(
                    "assembly extension must not receive the Part conversion"
                )
            )
            main.Extension.GetCorresponding2 = _raiser(
                AssertionError(
                    "assembly extension must not receive the Part conversion"
                )
            )
            part_document.Extension.GetCorrespondingEntity2 = (
                lambda entity: (
                    material_face if entity is face else None
                )
            )
            part_document.Extension.GetCorresponding2 = _raiser(
                AssertionError(
                    "body identity must be derived from the converted Part face"
                )
            )
            resolved_pid_objects = {
                b"appearance": material_face,
                b"feature": face_feature,
                b"part-body": underlying_body,
            }

            def resolve_pid(pid, errors):
                errors.value = 0
                return resolved_pid_objects[pid.value]

            part_document.Extension.GetObjectByPersistReference3 = (
                resolve_pid
            )
            backend = _CaptureBackend(
                session_factory=lambda: _Session(_App({assembly: main}))
            )
            backend.capture_scene_for_test(str(assembly), [])
            geometry = backend.capture_geometry_for_test()
            raw = geometry["arm-1"]["appearance_raw"]

            self.assertEqual(geometry["arm-1"]["state"], "observed")
            self.assertEqual(geometry["arm-1"]["triangles"], 1)
            self.assertEqual(part_document.active_configuration, "Painted")
            self.assertEqual(occurrence_calls, [(3, ("Painted",))])
            self.assertEqual(
                texture_calls,
                [
                    ("document", "Painted"),
                    ("occurrence", "Painted"),
                    ("face", "Painted"),
                ],
            )
            self.assertEqual(raw["component"], "arm-1")
            self.assertEqual(
                raw["source_document_path"].replace("\\", "/").casefold(),
                str(part).replace("\\", "/").casefold(),
            )
            self.assertEqual(raw["referenced_configuration"], "Painted")
            self.assertEqual(raw["native_active_configuration"], "Painted")
            self.assertIsNone(raw["native_active_configuration_error"])
            self.assertEqual(
                raw["document"]["used_api"],
                "IModelDoc2.MaterialPropertyValues"
                "/IModelDocExtension.HasMaterialPropertyValues"
                "/IModelDocExtension.GetTexture/ITexture.MaterialName",
            )
            self.assertEqual(
                raw["document"]["material_property_values"],
                document_values,
            )
            self.assertEqual(
                raw["document"]["material_call"],
                {
                    "configuration_semantics": "active_configuration",
                    "active_configuration": "Painted",
                },
            )
            self.assertTrue(raw["document"]["has_material_property_values"])
            self.assertEqual(
                raw["document"]["presence_call"],
                {
                    "arguments": [],
                    "configuration_semantics": "native_owner_context",
                    "same_owner": False,
                    "owner_api": "IModelDocExtension.HasMaterialPropertyValues",
                },
            )
            self.assertEqual(
                raw["document"]["texture"],
                {
                    "configuration": "Painted",
                    "state": "present",
                    "material_name": "C:\\textures\\paint.jpg",
                },
            )
            self.assertEqual(
                raw["occurrence"]["material_property_values"],
                occurrence_values,
            )
            self.assertFalse(raw["occurrence"]["has_material_property_values"])
            self.assertTrue(
                raw["occurrence"]["presence_call"]["same_owner"]
            )
            self.assertEqual(raw["occurrence"]["texture"]["state"], "absent")
            self.assertIsNone(raw["occurrence"]["texture"]["material_name"])
            self.assertEqual(len(raw["bodies"]), 1)
            body_raw = raw["bodies"][0]
            self.assertEqual(
                set(body_raw),
                {
                    "component",
                    "enumeration_api",
                    "body_type",
                    "body_index",
                    "body_name",
                    "body_info_raw",
                    "native_identity",
                    "faces",
                },
            )
            self.assertEqual(
                {
                    "component": body_raw["component"],
                    "enumeration_api": body_raw["enumeration_api"],
                    "body_type": body_raw["body_type"],
                    "body_index": body_raw["body_index"],
                    "body_name": body_raw["body_name"],
                },
                {
                    "component": "arm-1",
                    "enumeration_api": "IComponent2.GetBodies3",
                    "body_type": 0,
                    "body_index": 0,
                    "body_name": body.Name,
                },
            )
            self.assertEqual(body_raw["body_info_raw"], 1)
            self.assertEqual(body_appearance_calls, [])
            self.assertEqual(body_type_calls, [0, 1])
            self.assertEqual(face_enumeration_calls, [True])
            self.assertEqual(len(part_render_material_calls), 1)
            self.assertEqual(len(part_render_material_count_calls), 1)
            count_option, count_names = part_render_material_count_calls[0]
            material_option, material_names = (
                part_render_material_calls[0]
            )
            self.assertEqual(count_option, material_option)
            self.assertIs(count_names, material_names)
            self.assertEqual(tuple(count_names.value), ("FaceState",))
            self.assertEqual(
                count_names.varianttype,
                0x2000 | 8,
            )
            state_materials = {
                row["owner_scope"]: row
                for row in raw["state_render_materials"]
            }
            self.assertEqual(len(top_render_material_calls), 1, state_materials)
            self.assertEqual(len(top_render_material_count_calls), 1)
            top_count_option, top_count_names = (
                top_render_material_count_calls[0]
            )
            self.assertEqual(top_count_option, top_render_material_calls[0][0])
            self.assertIs(
                top_count_names,
                top_render_material_calls[0][1],
            )
            self.assertEqual(tuple(top_count_names.value), ("TopState",))
            self.assertEqual(
                state_materials["top_assembly"]["call_state"],
                "empty",
            )
            self.assertEqual(
                state_materials["top_assembly"][
                    "native_render_material_count"
                ],
                0,
            )
            component_materials = state_materials["component_document"]
            self.assertEqual(
                component_materials["call_state"],
                "observed",
            )
            self.assertEqual(
                component_materials["display_state"],
                "FaceState",
            )
            self.assertEqual(
                component_materials["native_render_material_count"],
                2,
            )
            self.assertEqual(
                component_materials["native_render_material_count_api"],
                "IModelDocExtension.GetRenderMaterialsCount2",
            )
            material = component_materials["materials"][0]
            self.assertEqual(material["linked_display_states"], ["FaceState"])
            self.assertEqual(material["primary_color"], 0x00AABBCC)
            self.assertEqual(material["transparency"], 0.25)
            self.assertEqual(material["color_form"], 1)
            self.assertEqual(material["texture_filename"], "")
            self.assertEqual(
                material["surface_finish_file"],
                "C:\\finish.p2m",
            )
            incomplete = component_materials["materials"][1]
            self.assertEqual(incomplete["entities_state"], "empty")
            self.assertEqual(
                incomplete["read_errors"]["primary_color"]["code"],
                "cad_member_missing",
            )
            entity = material["entity_records"][0]
            self.assertEqual(
                entity["native_interface_kinds"],
                ["IFace2", "IEntity"],
            )
            self.assertEqual(
                entity["persistent_reference"],
                list(material_face._persist_reference),
            )
            self.assertEqual(entity["read_errors"], {})
            self.assertEqual(
                entity["native_identity"]["kind"],
                "reference",
            )
            self.assertEqual(
                entity["native_identity"]["interface_kinds"],
                ["IFace2", "IEntity"],
            )
            self.assertNotIn("binding_state", entity)
            self.assertNotIn("binding_candidates", entity)
            self.assertNotIn("matched_identity", entity)
            reference_index = component_materials["native_reference_index"]
            self.assertEqual(reference_index, [])
            self.assertEqual(
                body_raw["native_identity"]["state"],
                "observed",
            )
            self.assertEqual(
                body_raw["native_identity"]["part_body_reference_count"],
                1,
            )
            self.assertNotIn("conversion", body_raw["native_identity"])
            face_raw = body_raw["faces"]
            feature_identity = face_raw[0]["appearance"]["native_identity"][
                "feature"
            ]
            self.assertEqual(
                face_raw[0]["appearance"]["native_identity"][
                    "conversion"
                ]["api"],
                "IModelDocExtension.GetCorrespondingEntity2",
            )
            self.assertEqual(
                face_raw[0]["appearance"]["native_identity"][
                    "conversion"
                ]["output_domain"]["interface_kind"],
                "IFace2",
            )
            self.assertEqual(feature_identity["name"], "PaintedFace")
            self.assertEqual(feature_identity["type_name"], "Extrusion")
            self.assertEqual(
                feature_identity["persistent_reference"],
                list(b"feature"),
            )
            self.assertEqual(
                feature_identity["conversion"]["output_domain"][
                    "interface_kind"
                ],
                "IFeature",
            )
            self.assertEqual(
                converted_feature_reads,
                [material_face],
            )
            self.assertEqual(len(face_raw), 1)
            self.assertEqual(set(face_raw[0]), {"face_index", "appearance"})
            self.assertEqual(face_raw[0]["face_index"], 0)
            self.assertEqual(
                face_raw[0]["appearance"]["material_property_values"],
                face_values,
            )
            self.assertEqual(
                face_raw[0]["appearance"]["material_call"],
                {
                    "configuration_semantics": "active_configuration",
                    "active_configuration": "Painted",
                },
            )
            self.assertTrue(
                face_raw[0]["appearance"]["has_material_property_values"]
            )
            self.assertEqual(
                face_raw[0]["appearance"]["texture"]["material_name"],
                "C:\\textures\\face.jpg",
            )
            self.assertEqual(face_raw[0]["appearance"]["read_errors"], {})

    def test_appearance_receiver_requires_the_exact_part_document(self) -> None:
        for failure in ("type", "path"):
            with self.subTest(failure=failure), TemporaryDirectory() as tmp, _com_stubs():
                root = Path(tmp)
                part = _write(root, "arm.SLDPRT")
                other = _write(root, "other.SLDPRT")
                part_document = _Doc(part)
                face = _mesh_face(
                    lambda _quality: [
                        0.0,
                        0.0,
                        0.0,
                        1.0,
                        0.0,
                        0.0,
                        0.0,
                        1.0,
                        0.0,
                    ],
                    "owner-domain",
                )
                face.MaterialPropertyValues = None
                face.HasMaterialPropertyValues = lambda: False
                face.GetTexture = lambda _configuration: None
                body = _mesh_body([face], "owner-domain")
                component = _Component(
                    "arm-1",
                    part,
                    doc=part_document,
                )
                face._fixture_component = component
                component.GetBodies3 = _component_bodies3(
                    lambda body_type, value=body: (
                        [value] if body_type == 0 else []
                    )
                )
                component.GetMaterialPropertyValues2 = (
                    lambda _option, _names: None
                )
                component.HasMaterialPropertyValues = lambda: False
                component.GetTexture = lambda _configuration: None
                assembly = _write(root, "robot.SLDASM")
                main = _Doc(
                    assembly,
                    doc_type=2,
                    children=[component],
                )
                backend = _CaptureBackend(
                    session_factory=lambda path=assembly, document=main: _Session(
                        _App({path: document})
                    )
                )
                backend.capture_scene_for_test(str(assembly), [])
                if failure == "type":
                    part_document._doc_type = 2
                else:
                    part_document._path = str(other)
                geometry = backend.capture_geometry_for_test()["arm-1"]
                document_errors = geometry["appearance_raw"]["document"][
                    "read_errors"
                ]
                self.assertTrue(
                    any(
                        error["code"]
                        in {
                            "cad_appearance_part_document_mismatch",
                            "cad_source_state_unreadable",
                        }
                        for error in document_errors.values()
                    ),
                    document_errors,
                )

    def test_null_material_values_follow_same_owner_presence(self) -> None:
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "arm.SLDPRT")
            part_document = _Doc(part)
            part_document.MaterialPropertyValues = None
            part_document.Extension.HasMaterialPropertyValues = lambda: True
            part_document.Extension.GetTexture = lambda _configuration: None

            face = _mesh_face(
                lambda _quality: [
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                    0.0,
                ],
                "null-appearance",
            )
            face.MaterialPropertyValues = None
            face.HasMaterialPropertyValues = lambda: False
            face.GetTexture = lambda _configuration: None
            body = _mesh_body([face], "null-appearance")
            component = _Component("arm-1", part, doc=part_document)
            face._fixture_component = component
            component.GetBodies3 = _component_bodies3(
                lambda body_type: [body] if body_type == 0 else []
            )
            component.GetMaterialPropertyValues2 = (
                lambda _option, _names: None
            )
            component.HasMaterialPropertyValues = lambda: True
            component.GetTexture = lambda _configuration: None
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[component])
            backend = _CaptureBackend(
                session_factory=lambda: _Session(_App({assembly: main}))
            )
            backend.capture_scene_for_test(str(assembly), [])
            raw = backend.capture_geometry_for_test()["arm-1"][
                "appearance_raw"
            ]

            self.assertNotIn(
                "material_property_values",
                raw["document"]["read_errors"],
            )
            self.assertTrue(
                raw["document"]["has_material_property_values"]
            )
            self.assertFalse(
                raw["document"]["presence_call"]["same_owner"]
            )
            self.assertEqual(
                raw["occurrence"]["read_errors"]["material_property_values"]["code"],
                "cad_appearance_values_unresolved",
            )
            for owner in (
                raw["bodies"][0]["faces"][0]["appearance"],
            ):
                self.assertIsNone(owner["material_property_values"])
                self.assertFalse(owner["has_material_property_values"])
                self.assertNotIn(
                    "material_property_values",
                    owner["read_errors"],
                )

    def test_face_appearance_survives_triangle_failure_and_has_error(self) -> None:
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "arm.SLDPRT")
            part_document = _Doc(part)

            def fail_has():
                raise RuntimeError("presence read failed")

            part_document.MaterialPropertyValues = None
            part_document.Extension.HasMaterialPropertyValues = fail_has
            part_document.Extension.GetTexture = lambda _configuration: None

            face_values = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]
            face = _mesh_face(
                _raiser(RuntimeError("triangle read failed")),
                "triangle-error",
            )
            face.MaterialPropertyValues = list(face_values)
            face.HasMaterialPropertyValues = lambda: True
            face.GetTexture = lambda _configuration: types.SimpleNamespace(
                MaterialName="C:\\textures\\face.jpg"
            )
            body = _mesh_body([face], "triangle-error")
            component = _Component("arm-1", part, doc=part_document)
            face._fixture_component = component
            component.GetBodies3 = _component_bodies3(
                lambda body_type: [body] if body_type == 0 else []
            )
            component.GetMaterialPropertyValues2 = (
                lambda _option, _names: None
            )
            component.HasMaterialPropertyValues = lambda: False
            component.GetTexture = lambda _configuration: None
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[component])
            backend = _CaptureBackend(
                session_factory=lambda: _Session(_App({assembly: main}))
            )
            backend.capture_scene_for_test(str(assembly), [])
            geometry = backend.capture_geometry_for_test()["arm-1"]
            raw = geometry["appearance_raw"]

            self.assertEqual(geometry["state"], "failed")
            self.assertEqual(geometry["triangles"], 0)
            self.assertTrue(
                any(
                    row["api"] == "IFace2.GetTessTriangles(True)"
                    for row in geometry["errors"]
                )
            )
            self.assertTrue(
                all(
                    "appearance" not in row["api"].casefold()
                    for row in geometry["errors"]
                )
            )
            document = raw["document"]
            self.assertIsNone(document["material_property_values"])
            self.assertIsNone(document["has_material_property_values"])
            self.assertIn(
                "has_material_property_values",
                document["read_errors"],
            )
            self.assertNotIn(
                "material_property_values",
                document["read_errors"],
            )
            face_appearance = raw["bodies"][0]["faces"][0]["appearance"]
            self.assertEqual(
                face_appearance["material_property_values"],
                face_values,
            )
            self.assertTrue(
                face_appearance["has_material_property_values"]
            )
            self.assertEqual(
                face_appearance["texture"]["material_name"],
                "C:\\textures\\face.jpg",
            )
            self.assertEqual(face_appearance["read_errors"], {})

    def test_document_material_survives_extension_owner_failure(self) -> None:
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "arm.SLDPRT")
            document_values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.0, 0.0]
            part_document = _Doc(part)
            part_document.MaterialPropertyValues = list(document_values)
            face = _mesh_face(
                lambda _quality: [
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                    0.0,
                ],
                "extension-failure",
            )
            face.MaterialPropertyValues = None
            face.HasMaterialPropertyValues = lambda: False
            face.GetTexture = lambda _configuration: None
            body = _mesh_body([face], "extension-failure")
            component = _Component("arm-1", part, doc=part_document)
            face._fixture_component = component
            component.GetBodies3 = _component_bodies3(
                lambda body_type: [body] if body_type == 0 else []
            )
            component.GetMaterialPropertyValues2 = (
                lambda _option, _names: None
            )
            component.HasMaterialPropertyValues = lambda: False
            component.GetTexture = lambda _configuration: None
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[component])
            backend = _CaptureBackend(
                session_factory=lambda: _Session(_App({assembly: main}))
            )
            backend.capture_scene_for_test(str(assembly), [])
            part_document.Extension = None
            geometry = backend.capture_geometry_for_test()["arm-1"]
            document = geometry["appearance_raw"]["document"]

            self.assertEqual(geometry["state"], "observed")
            self.assertEqual(
                document["material_property_values"],
                document_values,
            )
            self.assertFalse(document["presence_call"]["same_owner"])
            self.assertIn(
                "has_material_property_values",
                document["read_errors"],
            )
            self.assertIn("texture", document["read_errors"])

    def test_appearance_failures_do_not_block_native_triangle_capture(self) -> None:
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "arm.SLDPRT")

            class FailingAppearanceDoc(_Doc):
                @property
                def MaterialPropertyValues(self):
                    raise RuntimeError("material property read failed")

            part_document = FailingAppearanceDoc(part)

            def fail_values(*_args):
                raise RuntimeError("material property read failed")

            def fail_texture(_configuration):
                raise RuntimeError("texture read failed")

            part_document.Extension.HasMaterialPropertyValues = lambda: "true"
            part_document.Extension.GetTexture = fail_texture
            part_document.Extension.GetRenderMaterials2 = _raiser(
                RuntimeError("render material read failed")
            )
            part_document.Extension.GetRenderMaterialsCount2 = _raiser(
                RuntimeError("render material count failed")
            )
            face_base = _mesh_face(
                lambda _quality: [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
                "appearance-errors",
            )

            class FailingFace(types.SimpleNamespace):
                @property
                def MaterialPropertyValues(self):
                    return fail_values()

            face = FailingFace(**vars(face_base))
            face.HasMaterialPropertyValues = lambda: False
            face.GetTexture = fail_texture
            body = _mesh_body([face], "appearance-errors")
            component = _Component("arm-1", part, doc=part_document)
            face._fixture_component = component
            component.GetBodies3 = _component_bodies3(
                lambda body_type: [body] if body_type == 0 else []
            )
            component.GetMaterialPropertyValues2 = (
                lambda _option, _names: [0.0] * 8
            )
            component.HasMaterialPropertyValues = lambda: False
            component.GetTexture = fail_texture
            component.ReferencedDisplayState2 = "ErrorState"
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(
                assembly,
                doc_type=2,
                children=[component],
                display_states=["TopNullState"],
            )
            main.Extension.GetRenderMaterials2 = (
                lambda _option, _names: None
            )
            main.Extension.GetRenderMaterialsCount2 = (
                lambda option, names: 0
            )
            backend = _CaptureBackend(
                session_factory=lambda: _Session(_App({assembly: main}))
            )
            scene = backend.capture_scene_for_test(str(assembly), [])
            geometry = backend.capture_geometry_for_test()
            raw = geometry["arm-1"]["appearance_raw"]

            self.assertEqual(scene.components[0].name, "arm-1")
            self.assertEqual(geometry["arm-1"]["state"], "observed")
            self.assertEqual(geometry["arm-1"]["triangles"], 1)
            document = raw["document"]
            self.assertIsNone(document["material_property_values"])
            self.assertIsNone(document["has_material_property_values"])
            self.assertEqual(document["texture"]["state"], "unreadable")
            self.assertIn(
                "material_property_values",
                document["read_errors"],
            )
            self.assertIn(
                "has_material_property_values",
                document["read_errors"],
            )
            self.assertIn("texture", document["read_errors"])
            occurrence = raw["occurrence"]
            self.assertEqual(
                occurrence["material_property_values"],
                [0.0] * 8,
            )
            self.assertFalse(
                occurrence["has_material_property_values"]
            )
            self.assertEqual(
                occurrence["texture"]["state"],
                "unreadable",
            )
            self.assertIn(
                "material_property_values",
                occurrence["read_errors"],
            )
            self.assertIn("texture", occurrence["read_errors"])
            state_materials = {
                row["owner_scope"]: row
                for row in raw["state_render_materials"]
            }
            self.assertEqual(
                state_materials["top_assembly"]["call_state"],
                "null_result",
            )
            self.assertIsNone(
                state_materials["top_assembly"]["materials"]
            )
            self.assertEqual(
                state_materials["component_document"]["call_state"],
                "error",
            )
            self.assertIn(
                "materials",
                state_materials["component_document"]["read_errors"],
            )
            self.assertIn(
                "native_render_material_count",
                state_materials["component_document"]["read_errors"],
            )
            self.assertEqual(
                raw["occurrence"]["read_errors"]["material_property_values"]["code"],
                "cad_appearance_values_invalid",
            )
            face_appearance = raw["bodies"][0]["faces"][0]["appearance"]
            self.assertIsNone(face_appearance["material_property_values"])
            self.assertFalse(
                face_appearance["has_material_property_values"]
            )
            self.assertEqual(
                face_appearance["texture"]["state"],
                "unreadable",
            )
            self.assertIn(
                "material_property_values",
                face_appearance["read_errors"],
            )
            self.assertIn("texture", face_appearance["read_errors"])


class AppearanceHelperTests(_FixtureAppearanceInterfaceMixin, unittest.TestCase):
    def test_material_object_records_every_public_interface(self) -> None:
        entity = types.SimpleNamespace(
            _fixture_interfaces={
                "IPartDoc",
                "IComponent2",
                "IBody2",
                "IEntity",
            },
        )
        resolution_calls = []
        with patch.object(
            native_module,
            "_get_object_by_persist_reference",
            side_effect=lambda _extension, reference: (
                resolution_calls.append(list(reference))
                or (entity, 0)
            ),
        ):
            interfaces, output_domain, error = (
                native_module._appearance_resolve_persistent_reference(
                    types.SimpleNamespace(),
                    list(b"object"),
                    owner_document_path="C:\\part.SLDPRT",
                    cache={},
                )
            )

        self.assertIsNone(error)
        self.assertEqual(
            list(interfaces),
            ["IPartDoc", "IComponent2", "IBody2", "IEntity"],
        )
        self.assertEqual(
            output_domain["interface_kinds"],
            ["IPartDoc", "IComponent2", "IBody2", "IEntity"],
        )
        self.assertEqual(output_domain["status"], 0)
        self.assertEqual(resolution_calls, [list(b"object")])

    def test_material_object_without_supported_interface_fails_closed(
        self,
    ) -> None:
        unsupported = RuntimeError("unsupported interface")
        unsupported.hresult = native_module._COM_NOINTERFACE_HRESULT
        with (
            patch.object(
                native_module,
                "_get_object_by_persist_reference",
                return_value=(types.SimpleNamespace(), 0),
            ),
            patch.object(
                native_module,
                "_interface",
                side_effect=unsupported,
            ),
        ):
            interfaces, output_domain, error = (
                native_module._appearance_resolve_persistent_reference(
                    types.SimpleNamespace(),
                    list(b"object"),
                    owner_document_path="C:\\part.SLDPRT",
                    cache={},
                )
            )

        self.assertIsNone(interfaces)
        self.assertIsNone(output_domain)
        self.assertEqual(error["code"], "cad_appearance_object_interface_missing")

    def test_part_body_summary_marks_mixed_witnesses_partial(self) -> None:
        observation_errors = [
            {
                "face_index": 1,
                "read_errors": {
                    "body": {
                        "code": "cad_appearance_part_body_missing"
                    }
                },
            }
        ]
        state, errors = native_module._appearance_body_observation_summary(
            [{"face_index": 0, "persistent_reference": list(b"body")}],
            observation_errors,
        )

        self.assertEqual(state, "partial")
        self.assertEqual(
            errors["face_body_observations"],
            observation_errors,
        )
        self.assertIsNot(
            errors["face_body_observations"],
            observation_errors,
        )

    def test_underlying_part_identity_uses_face_conversion_and_face_body(
        self,
    ) -> None:
        face_proxy = types.SimpleNamespace(
            _persist_reference=b"assembly-face-proxy",
        )
        underlying_body = types.SimpleNamespace(
            _persist_reference=b"part-body",
            _fixture_interfaces={"IBody2"},
        )
        underlying_feature = types.SimpleNamespace(
            Name="PaintedFace",
            GetTypeName2=lambda: "Extrusion",
            _persist_reference=b"part-feature",
            _fixture_interfaces={"IFeature"},
        )
        method_hints = []

        class PartFace:
            _persist_reference = b"part-face"
            _fixture_interfaces = ("IFace2",)

            def __init__(self):
                self.hinted = set()

            def _FlagAsMethod(self, name):
                method_hints.append(name)
                self.hinted.add(name)

            def __getattr__(self, name):
                if name == "GetFeature":
                    if "GetFeature" not in self.hinted:
                        raise AssertionError(
                            "GetFeature must be flagged before getattr"
                        )
                    return lambda: underlying_feature
                raise AttributeError(name)

            def GetBody(self):
                return underlying_body

        underlying_face = PartFace()
        conversion_calls = []
        reference_calls = []

        def persist_reference(entity):
            reference_calls.append(entity)
            return list(entity._persist_reference)

        part_extension = types.SimpleNamespace(
            GetPersistReference3=persist_reference,
        )
        part_extension.GetCorrespondingEntity2 = (
            lambda entity: (
                conversion_calls.append(("face", entity))
                or underlying_face
                if entity is face_proxy
                else None
            )
        )
        part_extension.GetCorresponding2 = _raiser(
            AssertionError(
                "body identity must be derived from the converted Part face"
            )
        )
        resolved_objects = {
            b"part-face": underlying_face,
            b"part-body": underlying_body,
            b"part-feature": underlying_feature,
        }
        with patch.object(
            native_module,
            "_get_object_by_persist_reference",
            side_effect=lambda extension, reference: (
                resolved_objects[bytes(reference)],
                0,
            ),
        ):
            (
                converted_face,
                face_reference,
                face_conversion,
            ) = _CaptureBackend._appearance_underlying_part_face(
                face_proxy,
                component="parent-1/arm-1",
                part_document_path="C:\\part.SLDPRT",
                part_extension=part_extension,
            )
        self.assertIs(converted_face, underlying_face)
        self.assertEqual(face_reference, list(b"part-face"))
        self.assertEqual(
            face_conversion["api"],
            "IModelDocExtension.GetCorrespondingEntity2",
        )
        self.assertEqual(
            face_conversion["component"],
            "parent-1/arm-1",
        )
        self.assertEqual(
            face_conversion["input_context"],
            {
                "owner_scope": "assembly_occurrence",
                "object_kind": "assembly_occurrence",
                "source_api": "IComponent2.GetBodies3/IBody2.GetFaces",
                "traversal_api": (
                    "IConfiguration.GetRootComponent3/"
                    "IComponent2.GetChildren"
                ),
            },
        )
        self.assertEqual(
            face_conversion["receiver"],
            {
                "owner_scope": "component_document",
                "owner_document_path": "C:\\part.SLDPRT",
                "interface": "IModelDocExtension",
                "object_source": "captured_component_source_document",
            },
        )
        self.assertEqual(
            face_conversion["output_domain"]["interface_kind"],
            "IFace2",
        )
        self.assertEqual(face_conversion["state"], "observed")
        self.assertNotIn("persistent_reference", face_conversion)

        with patch.object(
            native_module,
            "_get_object_by_persist_reference",
            side_effect=lambda extension, reference: (
                resolved_objects[bytes(reference)],
                0,
            ),
        ):
            (
                converted_body,
                body_reference,
                body_conversion,
            ) = _CaptureBackend._appearance_underlying_part_body(
                underlying_face,
                component="parent-1/arm-1",
                part_document_path="C:\\part.SLDPRT",
                part_extension=part_extension,
                resolution_cache={},
            )
        self.assertIs(converted_body, underlying_body)
        self.assertIn("GetBody", method_hints)
        self.assertEqual(body_reference, list(b"part-body"))
        self.assertEqual(
            body_conversion["api"],
            "IFace2.GetBody+IModelDocExtension.GetPersistReference3",
        )
        self.assertEqual(body_conversion["state"], "observed")
        self.assertEqual(
            body_conversion["output_domain"]["interface_kind"],
            "IBody2",
        )
        self.assertNotIn("persistent_reference", body_conversion)
        self.assertEqual(
            conversion_calls,
            [("face", face_proxy)],
        )
        self.assertEqual(
            reference_calls,
            [underlying_face, underlying_body],
        )

        with patch.object(
            native_module,
            "_get_object_by_persist_reference",
            side_effect=lambda extension, reference: (
                resolved_objects[bytes(reference)],
                0,
            ),
        ):
            converted_feature, feature_identity, feature_conversion = (
                _CaptureBackend._appearance_underlying_part_feature(
                    underlying_face,
                    component="parent-1/arm-1",
                    part_document_path="C:\\part.SLDPRT",
                    part_extension=part_extension,
                    resolution_cache={},
                )
            )
        self.assertIs(converted_feature, underlying_feature)
        self.assertIn("GetFeature", method_hints)
        self.assertEqual(feature_identity["name"], "PaintedFace")
        self.assertEqual(
            feature_conversion["output_domain"]["interface_kind"],
            "IFeature",
        )

    def test_face_derived_body_requires_exact_interface_and_resolution(
        self,
    ) -> None:
        for failure, expected_code in (
            ("missing", "cad_appearance_part_body_missing"),
            (
                "wrong_interface",
                "cad_appearance_part_body_interface_missing",
            ),
            (
                "resolution",
                "cad_appearance_persist_reference_resolve_failed",
            ),
        ):
            body = types.SimpleNamespace(
                _persist_reference=b"part-body",
                _fixture_interfaces=(
                    set()
                    if failure == "wrong_interface"
                    else {"IBody2"}
                ),
            )
            face = types.SimpleNamespace(
                _fixture_interfaces={"IFace2"},
                GetBody=(
                    (lambda: None)
                    if failure == "missing"
                    else (lambda value=body: value)
                ),
            )
            extension = types.SimpleNamespace(
                GetPersistReference3=lambda entity: list(
                    entity._persist_reference
                )
            )
            with (
                self.subTest(failure=failure),
                patch.object(
                    native_module,
                    "_get_object_by_persist_reference",
                    return_value=(
                        (body, 0)
                        if failure != "resolution"
                        else (None, 1)
                    ),
                ),
            ):
                converted, reference, conversion = (
                    _CaptureBackend._appearance_underlying_part_body(
                        face,
                        component="parent-1/arm-1",
                        part_document_path="C:\\part.SLDPRT",
                        part_extension=extension,
                        resolution_cache={},
                    )
                )
                self.assertIsNone(converted)
                self.assertIn(
                    expected_code,
                    {
                        error["code"]
                        for error in conversion["read_errors"].values()
                    },
                )
                if failure == "resolution":
                    self.assertEqual(reference, list(b"part-body"))

    def test_underlying_part_conversion_fails_closed_without_raw_answers(
        self,
    ) -> None:
        face_proxy = types.SimpleNamespace(
            _persist_reference=b"assembly-face-proxy"
        )
        part_extension = types.SimpleNamespace(
            GetCorrespondingEntity2=lambda entity: None,
            GetPersistReference3=_raiser(
                AssertionError(
                    "an assembly proxy must never reach the Part reference API"
                )
            ),
        )
        converted, reference, conversion = (
            _CaptureBackend._appearance_underlying_part_face(
                face_proxy,
                component="parent-1/arm-1",
                part_document_path="C:\\part.SLDPRT",
                part_extension=part_extension,
            )
        )
        self.assertIsNone(converted)
        self.assertIsNone(reference)
        self.assertEqual(conversion["state"], "error")
        self.assertEqual(
            conversion["read_errors"]["conversion"]["code"],
            "cad_appearance_underlying_part_missing",
        )

        wrong_interface = types.SimpleNamespace(
            _fixture_interfaces=set(),
        )
        part_extension.GetCorrespondingEntity2 = (
            lambda entity: wrong_interface
        )
        converted, reference, conversion = (
            _CaptureBackend._appearance_underlying_part_face(
                face_proxy,
                component="parent-1/arm-1",
                part_document_path="C:\\part.SLDPRT",
                part_extension=part_extension,
            )
        )
        self.assertIsNone(converted)
        self.assertIsNone(reference)
        self.assertEqual(conversion["state"], "error")
        self.assertEqual(
            conversion["read_errors"]["output_interface"]["code"],
            "cad_appearance_part_interface_missing",
        )
        errors, rows = _CaptureBackend._appearance_reference_index(
            part_extension,
            owner_scope="component_document",
            component_name="parent-1/arm-1",
            owner_document_path="C:\\part.SLDPRT",
            holders=[],
            )
        self.assertEqual(rows, [])
        self.assertEqual(errors, [])

        reference_calls = []
        alias_face = types.SimpleNamespace(
            _persist_reference=b"assembly-alias",
            _fixture_interfaces={"IFace2"},
        )
        part_extension.GetPersistReference3 = (
            lambda entity: reference_calls.append(entity)
            or list(entity._persist_reference)
        )
        part_extension.GetCorrespondingEntity2 = (
            lambda entity: alias_face
        )
        with patch.object(
            native_module,
            "_get_object_by_persist_reference",
            return_value=(alias_face, 0),
        ):
            converted, reference, conversion = (
                _CaptureBackend._appearance_underlying_part_face(
                    alias_face,
                    component="parent-1/arm-1",
                    part_document_path="C:\\part.SLDPRT",
                    part_extension=part_extension,
                )
            )
        self.assertIs(converted, alias_face)
        self.assertEqual(reference, list(b"assembly-alias"))
        self.assertEqual(reference_calls, [alias_face])
        with patch.object(
            native_module,
            "_get_object_by_persist_reference",
            return_value=(None, 1),
        ):
            errors, rows = _CaptureBackend._appearance_reference_index(
                part_extension,
                owner_scope="component_document",
                component_name="parent-1/arm-1",
                owner_document_path="C:\\part.SLDPRT",
                holders=[],
                )
        self.assertEqual(rows, [])
        self.assertEqual(errors, [])

        for resolved, expected_code in (
            (None, "cad_appearance_persist_reference_resolve_missing"),
            (
                types.SimpleNamespace(_fixture_interfaces=set()),
                "cad_appearance_object_interface_missing",
            ),
        ):
            output = types.SimpleNamespace(
                _persist_reference=b"part-face",
                _fixture_interfaces={"IFace2"},
            )
            extension = types.SimpleNamespace(
                GetCorrespondingEntity2=lambda entity, value=output: value,
                GetPersistReference3=lambda entity: list(
                    entity._persist_reference
                ),
            )
            with patch.object(
                native_module,
                "_get_object_by_persist_reference",
                return_value=(resolved, 0),
            ):
                converted, reference, conversion = (
                    _CaptureBackend._appearance_underlying_part_face(
                        types.SimpleNamespace(),
                        component="parent-1/arm-1",
                        part_document_path="C:\\part.SLDPRT",
                        part_extension=extension,
                    )
                )
            self.assertIsNone(converted)
            self.assertEqual(reference, list(b"part-face"))
            with patch.object(
                native_module,
                "_get_object_by_persist_reference",
                return_value=(resolved, 0),
            ):
                errors, rows = _CaptureBackend._appearance_reference_index(
                    extension,
                    owner_scope="component_document",
                    component_name="parent-1/arm-1",
                    owner_document_path="C:\\part.SLDPRT",
                    holders=[],
                    )
            self.assertEqual(rows, [])
            self.assertEqual(errors, [])
            self.assertIn(
                expected_code.split("_", 3)[-1],
                str(conversion["read_errors"]),
            )

        bool_status_output = types.SimpleNamespace(
            _persist_reference=b"part-face",
            _fixture_interfaces={"IFace2"},
        )
        bool_status_extension = types.SimpleNamespace(
            GetCorrespondingEntity2=lambda entity: bool_status_output,
            GetPersistReference3=lambda entity: list(
                entity._persist_reference
            ),
        )
        with patch.object(
            native_module,
            "_get_object_by_persist_reference",
            return_value=(bool_status_output, False),
        ):
            converted, reference, conversion = (
                _CaptureBackend._appearance_underlying_part_face(
                    types.SimpleNamespace(),
                    component="parent-1/arm-1",
                    part_document_path="C:\\part.SLDPRT",
                    part_extension=bool_status_extension,
                )
            )
        self.assertIsNone(converted)
        with patch.object(
            native_module,
            "_get_object_by_persist_reference",
            return_value=(bool_status_output, False),
        ):
            errors, rows = _CaptureBackend._appearance_reference_index(
                bool_status_extension,
                owner_scope="component_document",
                component_name="parent-1/arm-1",
                owner_document_path="C:\\part.SLDPRT",
                holders=[],
                )
        self.assertEqual(rows, [])
        self.assertEqual(errors, [])
        self.assertIn(
            "cad_appearance_persist_reference_status_invalid",
            str(conversion["read_errors"]),
        )

        resolution_calls = []
        cached_output = types.SimpleNamespace(
            _persist_reference=b"part-face",
            _fixture_interfaces={"IFace2"},
        )
        cached_extension = types.SimpleNamespace(
            GetCorrespondingEntity2=lambda entity: cached_output,
            GetPersistReference3=lambda entity: list(
                entity._persist_reference
            ),
        )
        cache = {}
        with patch.object(
            native_module,
            "_get_object_by_persist_reference",
            side_effect=lambda extension, reference: (
                resolution_calls.append(list(reference))
                or (cached_output, 0)
            ),
        ):
            first_conversion = (
                _CaptureBackend._appearance_underlying_part_face(
                    types.SimpleNamespace(),
                    component="parent-1/arm-1",
                    part_document_path="C:\\part.SLDPRT",
                    part_extension=cached_extension,
                    resolution_cache=cache,
                )
            )
            second_conversion = (
                _CaptureBackend._appearance_underlying_part_face(
                    types.SimpleNamespace(),
                    component="parent-1/arm-1",
                    part_document_path="C:\\part.SLDPRT",
                    part_extension=cached_extension,
                    resolution_cache=cache,
                )
            )
            self.assertEqual(first_conversion, second_conversion)
            self.assertEqual(first_conversion[2]["state"], "observed")
        self.assertEqual(resolution_calls, [list(b"part-face")])

    def test_persistent_reference_resolution_uses_exact_variant_marshalling(
        self,
    ) -> None:
        resolved_face = types.SimpleNamespace(
            _fixture_interfaces={"IFace2"},
        )
        calls = []

        class Extension:
            def GetObjectByPersistReference3(self, variant, errors):
                calls.append(
                    (
                        variant.varianttype,
                        variant.value,
                        errors.varianttype,
                        errors.value,
                    )
                )
                errors.value = 0
                return resolved_face

        cache = {}
        with _com_stubs():
            interfaces, output_domain, error = (
                native_module._appearance_resolve_persistent_reference(
                    Extension(),
                    [1, 2, 255],
                    owner_document_path="C:\\part.SLDPRT",
                    cache=cache,
                )
            )
            second_value, second_domain, second_error = (
                native_module._appearance_resolve_typed_reference(
                    Extension(),
                    [1, 2, 255],
                    interface_kind="IFace2",
                    owner_document_path="C:\\part.SLDPRT",
                    cache=cache,
                )
            )
        self.assertIs(interfaces["IFace2"], resolved_face)
        self.assertIs(second_value, resolved_face)
        self.assertEqual(
            second_domain["interface_kind"],
            "IFace2",
        )
        self.assertIsNone(second_error)
        self.assertIsNone(error)
        self.assertEqual(
            output_domain["proof_api"],
            "IModelDocExtension.GetObjectByPersistReference3",
        )
        self.assertEqual(
            calls,
            [
                (
                    0x2000 | 17,
                    b"\x01\x02\xff",
                    0x4000 | 3,
                    0,
                )
            ],
        )

    def test_display_state_preserves_null_and_empty_results(self) -> None:
        for raw_states, expected_state, expected_raw in (
            (None, "null_result", None),
            ([], "empty", []),
        ):
            active = types.SimpleNamespace(
                Name="Default",
                GetDisplayStates=lambda value=raw_states: value,
            )
            with (
                self.subTest(states=raw_states),
                patch.object(
                    native_module,
                    "_active_configuration_view",
                    return_value=(None, active),
                ),
            ):
                (
                    configuration,
                    display_state,
                    error,
                    states,
                    states_state,
                ) = _appearance_document_display_state(object())
                self.assertEqual(configuration, "Default")
                self.assertIsNone(display_state)
                self.assertEqual(
                    error["code"],
                    "cad_appearance_display_state_missing",
                )
                self.assertEqual(states, expected_raw)
                self.assertEqual(states_state, expected_state)

    def test_referenced_display_state_distinguishes_failure_and_null(self) -> None:
        missing, missing_error = _appearance_display_state(object())
        self.assertIsNone(missing)
        self.assertEqual(missing_error["code"], "cad_member_missing")

        holder = types.SimpleNamespace(ReferencedDisplayState2=None)
        invalid, invalid_error = _appearance_display_state(holder)
        self.assertIsNone(invalid)
        self.assertEqual(
            invalid_error["code"],
            "cad_appearance_display_state_invalid",
        )

    def test_display_states_invalid_and_exception_are_field_local(self) -> None:
        invalid_active = types.SimpleNamespace(
            Name="Default",
            GetDisplayStates=lambda: "not-an-array",
        )
        with patch.object(
            native_module,
            "_active_configuration_view",
            return_value=(None, invalid_active),
        ):
            *_, error, states, states_state = (
                _appearance_document_display_state(object())
            )
            self.assertEqual(
                error["code"],
                "cad_appearance_display_states_invalid",
            )
            self.assertEqual(states, "not-an-array")
            self.assertEqual(states_state, "error")

        failing_active = types.SimpleNamespace(
            Name="Default",
            GetDisplayStates=_raiser(RuntimeError("display state read failed")),
        )
        with patch.object(
            native_module,
            "_active_configuration_view",
            return_value=(None, failing_active),
        ):
            configuration, state, error, states, states_state = (
                _appearance_document_display_state(object())
            )
            self.assertEqual(configuration, "Default")
            self.assertIsNone(state)
            self.assertIsNone(states)
            self.assertEqual(states_state, "error")
            self.assertEqual(error["code"], "RuntimeError")

    def test_persistent_reference_missing_and_invalid_are_errors(self) -> None:
        missing_extension = types.SimpleNamespace(
            GetPersistReference3=lambda _entity: None
        )
        value, error = _appearance_persistent_reference(
            missing_extension,
            object(),
        )
        self.assertIsNone(value)
        self.assertEqual(
            error["code"],
            "cad_appearance_persistent_reference_missing",
        )
        signed_extension = types.SimpleNamespace(
            GetPersistReference3=lambda _entity: [-1, 0]
        )
        value, error = _appearance_persistent_reference(
            signed_extension,
            object(),
        )
        self.assertEqual(value, [-1, 0])
        self.assertIsNone(error)
        for raw, expected in (
            (b"\x00\xff", [0, 255]),
            (bytearray(b"\x00\xff"), [0, 255]),
            (memoryview(b"\x00\xff"), [0, 255]),
        ):
            bytes_extension = types.SimpleNamespace(
                GetPersistReference3=lambda _entity, value=raw: value
            )
            value, error = _appearance_persistent_reference(
                bytes_extension,
                object(),
            )
            self.assertEqual(value, expected)
            self.assertIsNone(error)
        entity = object()
        material = types.SimpleNamespace(
            GetLinkedDisplayStates=list,
            GetEntities=lambda: [entity],
            PrimaryColor=1,
            Transparency=0.0,
            ColorForm=1,
            TextureFilename="",
            SurfaceFinishFile="",
        )
        material_record, material_entities = _appearance_render_material(
            material,
            0,
            missing_extension,
        )
        self.assertEqual(material_entities, [entity])
        self.assertEqual(
            material_record["entity_records"][0]["read_errors"][
                "persistent_reference"
            ]["code"],
            "cad_appearance_persistent_reference_missing",
        )

        invalid_extension = types.SimpleNamespace(
            GetPersistReference3=lambda _entity: [1, 300]
        )
        value, error = _appearance_persistent_reference(
            invalid_extension,
            object(),
        )
        self.assertEqual(value, [1, 300])
        self.assertEqual(
            error["code"],
            "cad_appearance_persistent_reference_invalid",
        )

    def test_document_entity_uses_path_identity_without_persistent_reference(
        self,
    ) -> None:
        persistent_calls = []
        document = types.SimpleNamespace(
            _fixture_interfaces={"IModelDoc2", "IPartDoc", "IEntity"},
            GetPathName=lambda: "C:\\part.SLDPRT",
        )
        extension = types.SimpleNamespace(
            GetPersistReference3=lambda entity: persistent_calls.append(
                entity
            )
        )
        material = types.SimpleNamespace(
            GetLinkedDisplayStates=list,
            GetEntities=lambda: [document],
            PrimaryColor=1,
            Transparency=0.0,
            ColorForm=1,
            TextureFilename="",
            SurfaceFinishFile="",
        )
        record, entities = _appearance_render_material(
            material,
            0,
            extension,
        )
        entity = record["entity_records"][0]
        self.assertEqual(entities, [document])
        self.assertEqual(
            entity["native_interface_kinds"],
            ["IModelDoc2", "IPartDoc", "IEntity"],
        )
        self.assertIsNone(entity["persistent_reference"])
        self.assertEqual(entity["read_errors"], {})
        self.assertEqual(persistent_calls, [])

        backend = _CaptureBackend(session_factory=lambda: None)
        with patch.object(
            native_module,
            "_appearance_active_configuration",
            return_value=("Configured", None),
        ):
            context = backend._appearance_entity_context(
                document,
                entity,
                owner_scope="component_document",
                component_name="arm-1",
                owner_document_path="C:\\part.SLDPRT",
                owner_extension=extension,
                resolution_cache={},
            )
        self.assertEqual(
            context["record"]["native_identity"]["document_path"],
            "C:\\part.SLDPRT",
        )
        self.assertEqual(
            context["record"]["native_identity"]["active_configuration"],
            "Configured",
        )
        self.assertEqual(
            context["record"]["native_interface_kinds"],
            ["IModelDoc2", "IPartDoc", "IEntity"],
        )
        self.assertIsNone(context["persistent_reference"])
        self.assertEqual(persistent_calls, [])

    def test_interface_query_failure_does_not_fall_back_to_reference_or_api(
        self,
    ) -> None:
        entity = types.SimpleNamespace(_oleobj_=object())
        persistent_calls = []
        extension = types.SimpleNamespace(
            GetPersistReference3=lambda value: persistent_calls.append(
                value
            )
        )
        material = types.SimpleNamespace(
            GetLinkedDisplayStates=list,
            GetEntities=lambda: [entity],
            PrimaryColor=1,
            Transparency=0.0,
            ColorForm=1,
            TextureFilename="",
            SurfaceFinishFile="",
        )
        with (
            patch.object(
                native_module,
                "_appearance_interface",
                _PRODUCTION_APPEARANCE_INTERFACE,
            ),
            patch.object(
                native_module,
                "_interface",
                side_effect=RuntimeError("interface query failed"),
            ),
        ):
            record, entities = _appearance_render_material(
                material,
                0,
                extension,
            )
        rendered = record["entity_records"][0]
        self.assertEqual(entities, [entity])
        self.assertEqual(rendered["native_interface_kinds"], [])
        self.assertIsNone(rendered["persistent_reference"])
        self.assertEqual(
            rendered["read_errors"]["interface_query"]["code"],
            "RuntimeError",
        )
        self.assertEqual(persistent_calls, [])

    def test_unsupported_interface_is_absence_not_a_qi_failure(self) -> None:
        entity = types.SimpleNamespace(
            _oleobj_=object(),
            _persist_reference=b"generic",
        )
        unsupported = RuntimeError("unsupported interface")
        unsupported.hresult = native_module._COM_NOINTERFACE_HRESULT
        material = types.SimpleNamespace(
            GetLinkedDisplayStates=list,
            GetEntities=lambda: [entity],
            PrimaryColor=1,
            Transparency=0.0,
            ColorForm=1,
            TextureFilename="",
            SurfaceFinishFile="",
        )
        with patch.object(
            native_module,
            "_interface",
            side_effect=unsupported,
        ):
            record, _ = _appearance_render_material(
                material,
                0,
                types.SimpleNamespace(
                    GetPersistReference3=lambda _entity: list(
                        entity._persist_reference
                    )
                ),
            )
        rendered = record["entity_records"][0]
        self.assertEqual(rendered["native_interface_kinds"], [])
        self.assertEqual(
            rendered["persistent_reference"],
            list(entity._persist_reference),
        )
        self.assertEqual(rendered["read_errors"], {})

    def test_document_entity_identity_never_falls_back_to_owner_values(
        self,
    ) -> None:
        document = types.SimpleNamespace(
            _fixture_interfaces={"IModelDoc2"},
            GetPathName=_raiser(RuntimeError("path read failed")),
        )
        backend = _CaptureBackend(session_factory=lambda: None)
        with patch.object(
            native_module,
            "_appearance_active_configuration",
            return_value=(
                None,
                {
                    "code": "configuration_read_failed",
                    "detail": None,
                    "message": "configuration read failed",
                },
            ),
        ):
            context = backend._appearance_entity_context(
                document,
                {"native_interface_kinds": ["IModelDoc2"]},
                owner_scope="top_assembly",
                component_name="arm-1",
                owner_document_path="C:\\part.SLDPRT",
                owner_extension=None,
                resolution_cache={},
            )
        identity = context["record"]["native_identity"]
        self.assertIsNone(identity["document_path"])
        self.assertIsNone(identity["active_configuration"])
        self.assertEqual(identity["state"], "error")
        self.assertEqual(
            identity["read_errors"]["document_path"]["code"],
            "RuntimeError",
        )
        self.assertEqual(
            identity["read_errors"]["active_configuration"]["code"],
            "configuration_read_failed",
        )

    def test_document_identity_without_active_configuration_is_unbound(
        self,
    ) -> None:
        document = types.SimpleNamespace(
            _fixture_interfaces={"IModelDoc2"},
            GetPathName=lambda: "C:\\part.SLDPRT",
        )
        backend = _CaptureBackend(session_factory=lambda: None)
        with patch.object(
            native_module,
            "_appearance_active_configuration",
            return_value=(
                None,
                {
                    "code": "configuration_read_failed",
                    "detail": None,
                    "message": "configuration read failed",
                },
            ),
        ):
            context = backend._appearance_entity_context(
                document,
                {"native_interface_kinds": ["IModelDoc2"]},
                owner_scope="component_document",
                component_name="arm-1",
                owner_document_path="C:\\part.SLDPRT",
                owner_extension=None,
                resolution_cache={},
            )
        self.assertEqual(
            context["record"]["native_identity"]["state"],
            "partial",
        )

    def test_render_material_entities_preserve_null_and_error(self) -> None:
        null_material = types.SimpleNamespace(
            GetLinkedDisplayStates=list,
            GetEntities=lambda: None,
            PrimaryColor=1,
            Transparency=0.0,
            ColorForm=1,
            TextureFilename="",
            SurfaceFinishFile="",
        )
        null_record, null_entities = _appearance_render_material(
            null_material,
            0,
            object(),
        )
        self.assertEqual(null_entities, [])
        self.assertEqual(null_record["entities_state"], "null_result")
        self.assertIsNone(null_record["entities"])
        self.assertNotIn("entities", null_record["read_errors"])

        failing_material = types.SimpleNamespace(
            GetLinkedDisplayStates=list,
            GetEntities=_raiser(RuntimeError("entity read failed")),
            PrimaryColor=1,
            Transparency=0.0,
            ColorForm=1,
            TextureFilename="",
            SurfaceFinishFile="",
        )
        failing_record, failing_entities = _appearance_render_material(
            failing_material,
            0,
            object(),
        )
        self.assertEqual(failing_entities, [])
        self.assertEqual(failing_record["entities_state"], "error")
        self.assertIn("entities", failing_record["read_errors"])

    def test_null_render_materials_retain_count_and_count_error(self) -> None:
        with _com_stubs():
            count_extension = types.SimpleNamespace(
                GetRenderMaterialsCount2=lambda _option, _names: 1,
                GetRenderMaterials2=lambda _option, _names: None,
            )
            count_record, _, _ = _state_render_materials_raw(
                types.SimpleNamespace(Extension=count_extension),
                configuration="Default",
                display_state="State",
            )
            self.assertEqual(count_record["call_state"], "null_result")
            self.assertEqual(
                count_record["native_render_material_count_api"],
                "IModelDocExtension.GetRenderMaterialsCount2",
            )
            self.assertEqual(count_record["native_render_material_count"], 1)
            self.assertIsNone(count_record["native_render_material_count_error"])
            self.assertIsNone(count_record["materials"])

            failing_count_extension = types.SimpleNamespace(
                GetRenderMaterialsCount2=_raiser(
                    RuntimeError("count read failed")
                ),
                GetRenderMaterials2=lambda _option, _names: None,
            )
            failing_count_record, _, _ = _state_render_materials_raw(
                types.SimpleNamespace(Extension=failing_count_extension),
                configuration="Default",
                display_state="State",
            )
            self.assertEqual(
                failing_count_record["call_state"],
                "null_result",
            )
            self.assertIsNone(
                failing_count_record["native_render_material_count"]
            )
            self.assertEqual(
                failing_count_record["read_errors"][
                    "native_render_material_count"
                ]["code"],
                "RuntimeError",
            )

    def test_owner_entity_context_records_document_identity_without_conversion(
        self,
    ) -> None:
        backend = _CaptureBackend(session_factory=lambda: None)
        owner_document = types.SimpleNamespace(
            _fixture_interfaces={"IModelDoc2"},
            GetPathName=lambda: "C:\\part.SLDPRT",
        )
        with patch.object(
            native_module,
            "_appearance_active_configuration",
            return_value=("Configured", None),
        ):
            document_result = backend._appearance_entity_context(
                owner_document,
                {
                    "native_interface_kinds": ["IModelDoc2"],
                    "persistent_reference": None,
                    "read_errors": {},
                },
                owner_scope="component_document",
                component_name="arm-1",
                owner_document_path="C:\\part.SLDPRT",
                owner_extension=None,
                resolution_cache={},
            )
        self.assertEqual(
            document_result["record"]["native_identity"],
            {
                "kind": "document",
                "qi_iid": native_module.IMODELDOC2_IID,
                "document_path": "C:\\part.SLDPRT",
                "active_configuration": "Configured",
                "state": "observed",
                "read_errors": {},
            },
        )
        self.assertIsNone(document_result["persistent_reference"])

        part_feature = types.SimpleNamespace(
            _persist_reference=b"part-feature",
            _fixture_interfaces={"IFeature"},
        )
        with patch.object(
            native_module,
            "_get_object_by_persist_reference",
            return_value=(part_feature, 0),
        ):
            owner_context = backend._appearance_entity_context(
                part_feature,
                {
                    "native_interface_kinds": [],
                    "persistent_reference": list(
                        part_feature._persist_reference
                    ),
                    "read_errors": {},
                },
                owner_scope="component_document",
                component_name="arm-1",
                owner_document_path="C:\\part.SLDPRT",
                owner_extension=types.SimpleNamespace(),
                resolution_cache={},
            )
        self.assertEqual(
            owner_context["record"],
            {
                "persistent_reference": list(
                    part_feature._persist_reference
                ),
                "native_interface_kinds": ["IFeature"],
                "read_errors": {},
                "binding_context": {
                    "owner_scope": "component_document",
                    "owner_document_path": "C:\\part.SLDPRT",
                    "component": "arm-1",
                    "api": "IModelDocExtension.GetPersistReference3",
                },
                "native_identity": {
                    "kind": "reference",
                    "state": "observed",
                    "proof_api": (
                        "IModelDocExtension.GetObjectByPersistReference3"
                    ),
                    "status": 0,
                    "owner_document_path": "C:\\part.SLDPRT",
                    "interface_kinds": ["IFeature"],
                    "read_errors": {},
                },
            },
        )

        missing_reference = backend._appearance_entity_context(
            part_feature,
            {
                "native_interface_kinds": [],
                "persistent_reference": None,
                "read_errors": {
                    "persistent_reference": {
                        "code": "cad_appearance_persistent_reference_missing",
                        "message": "missing",
                        "detail": None,
                    }
                },
            },
            owner_scope="component_document",
            component_name="arm-1",
            owner_document_path="C:\\part.SLDPRT",
            owner_extension=None,
            resolution_cache={},
        )
        self.assertIsNone(missing_reference["persistent_reference"])
        self.assertEqual(
            missing_reference["record"]["read_errors"][
                "persistent_reference"
            ]["code"],
            "cad_appearance_persistent_reference_missing",
        )

    def test_state_unavailable_owner_record_is_explicit(self) -> None:
        backend = _CaptureBackend(session_factory=lambda: None)
        owner = {
            "document_path": "C:\\model.SLDPRT",
            "owner_scope": "component_document",
            "document": types.SimpleNamespace(),
            "configuration": None,
            "display_state": None,
            "display_state_error": {
                "code": "cad_appearance_display_state_missing",
                "message": "missing",
                "detail": {"value": None},
            },
            "display_states": None,
            "display_states_state": "not_applicable",
            "owner_error": None,
        }
        with patch.object(
            backend,
            "_appearance_owner_rows",
            return_value=([owner], []),
        ):
            records = backend._capture_state_render_materials(
                component="arm-1",
                holder=types.SimpleNamespace(),
                current={"arm-1": types.SimpleNamespace()},
                leaf_document=owner["document"],
                source_configuration="Default",
                leaf_display_state=None,
                leaf_display_state_error=owner["display_state_error"],
                cache={},
            )

        self.assertEqual(records[0]["call_state"], "state_unavailable")
        self.assertEqual(
            records[0]["read_errors"]["display_state"]["code"],
            "cad_appearance_display_state_missing",
        )

    def test_later_owner_failure_does_not_discard_successful_records(self) -> None:
        def owner(path, scope):
            return {
                "document_path": path,
                "owner_scope": scope,
                "document": types.SimpleNamespace(),
                "configuration": "Default",
                "display_state": f"{scope}-state",
                "display_state_error": None,
                "display_states": None,
                "display_states_state": "not_applicable",
                "owner_error": None,
            }

        backend = _CaptureBackend(session_factory=lambda: None)
        rows = [
            owner("C:\\first.SLDASM", "top_assembly"),
            owner("C:\\second.SLDASM", "parent_assembly"),
        ]
        successful = (
            {
                "api": "IModelDocExtension.GetRenderMaterials2",
                "display_state_option": 3,
                "display_state": "top_assembly-state",
                "call_state": "observed",
                "materials": [],
                "read_errors": {},
            },
            object(),
            [],
        )
        with (
            patch.object(
                backend,
                "_appearance_owner_rows",
                return_value=(rows, []),
            ),
            patch.object(
                native_module,
                "_state_render_materials_raw",
                side_effect=[successful, RuntimeError("second owner failed")],
            ),
        ):
            records = backend._capture_state_render_materials(
                component="arm-1",
                holder=types.SimpleNamespace(),
                current={"arm-1": types.SimpleNamespace()},
                leaf_document=rows[0]["document"],
                source_configuration="Default",
                leaf_display_state="ArmState",
                leaf_display_state_error=None,
                cache={},
            )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["call_state"], "observed")
        self.assertEqual(records[1]["call_state"], "error")
        self.assertIn("owner", records[1]["read_errors"])


class AppearanceOwnerResolutionTests(
    _FixtureAppearanceInterfaceMixin,
    unittest.TestCase,
):

    def test_component_materials_join_the_part_owner_reference_namespace(
        self,
    ) -> None:
        face_zero = types.SimpleNamespace(
            _persist_reference=b"part-face-zero",
            _fixture_interfaces={"IEntity", "IFace2"},
        )
        face_one = types.SimpleNamespace(
            _persist_reference=b"part-face-one",
            _fixture_interfaces={"IEntity", "IFace2"},
        )
        material = types.SimpleNamespace(
            GetLinkedDisplayStates=lambda: ["LeafState"],
            GetEntities=lambda: [face_zero, face_one],
            PrimaryColor=1,
            Transparency=0.0,
            ColorForm=1,
            TextureFilename="",
            SurfaceFinishFile="",
        )
        owner_reference_reads = []

        def read_owner_reference(entity):
            owner_reference_reads.append(entity)
            return list(entity._persist_reference)

        leaf_extension = types.SimpleNamespace(
            GetRenderMaterialsCount2=lambda _option, _names: 1,
            GetRenderMaterials2=lambda _option, _names: [material],
            GetPersistReference3=read_owner_reference,
            GetObjectByPersistReference3=lambda pid, errors: (
                setattr(errors, "value", 0)
                or {
                    b"part-face-zero": face_zero,
                    b"part-face-one": face_one,
                }[pid.value]
            ),
        )
        parent_reference_calls = []

        def constant_parent_reference(entity):
            parent_reference_calls.append(entity)
            return list(b"constant-component-reference")

        parent_extension = types.SimpleNamespace(
            GetRenderMaterialsCount2=lambda _option, _names: 0,
            GetRenderMaterials2=lambda _option, _names: [],
            GetPersistReference3=constant_parent_reference,
        )
        top_extension = types.SimpleNamespace(
            GetRenderMaterialsCount2=lambda _option, _names: 0,
            GetRenderMaterials2=lambda _option, _names: [],
            GetPersistReference3=constant_parent_reference,
        )
        leaf_document = types.SimpleNamespace(
            Extension=leaf_extension,
            GetPathName=lambda: "C:\\leaf.SLDPRT",
        )
        parent_document = types.SimpleNamespace(
            Extension=parent_extension,
            GetPathName=lambda: "C:\\parent.SLDASM",
        )
        top_document = types.SimpleNamespace(
            Extension=top_extension,
            GetPathName=lambda: "C:\\root.SLDASM",
        )

        def owner(document, path, scope, state):
            return {
                "document_path": path,
                "owner_scope": scope,
                "document": document,
                "configuration": "Default",
                "display_state": state,
                "display_state_error": None,
                "display_states": (
                    [state] if scope == "top_assembly" else None
                ),
                "display_states_state": (
                    "observed"
                    if scope == "top_assembly"
                    else "not_applicable"
                ),
                "owner_error": None,
            }

        owner_rows = [
            owner(top_document, "C:\\root.SLDASM", "top_assembly", "Top"),
            owner(
                parent_document,
                "C:\\parent.SLDASM",
                "parent_assembly",
                "Parent",
            ),
            owner(
                leaf_document,
                "C:\\leaf.SLDPRT",
                "component_document",
                "LeafState",
            ),
        ]
        holder = types.SimpleNamespace(
            GetCorrespondingEntity=_raiser(
                AssertionError("component-document objects must not be converted")
            ),
            GetCorresponding=_raiser(
                AssertionError("component-document objects must not be converted")
            ),
        )
        backend = _CaptureBackend(session_factory=lambda: None)
        with _com_stubs(), patch.object(
            backend,
            "_appearance_owner_rows",
            return_value=(owner_rows, ["sub-1"]),
        ):
            records = backend._capture_state_render_materials(
                component="sub-1/leaf-1",
                holder=holder,
                current={"sub-1": holder},
                leaf_document=leaf_document,
                source_configuration="Default",
                leaf_display_state="LeafState",
                leaf_display_state_error=None,
                cache={},
            )

        leaf_record = records[-1]
        entities = leaf_record["materials"][0]["entity_records"]
        self.assertEqual(
            [row["persistent_reference"] for row in entities],
            [list(b"part-face-zero"), list(b"part-face-one")],
        )
        self.assertEqual(leaf_record["native_reference_index"], [])
        self.assertTrue(
            all(
                row["binding_context"]["owner_document_path"]
                == "C:\\leaf.SLDPRT"
                for row in entities
            )
        )
        self.assertEqual(parent_reference_calls, [])
        self.assertEqual(owner_reference_reads, [face_zero, face_one])
        self.assertEqual(leaf_record["mapping_errors"], [])

    def test_assembly_owned_material_uses_the_assembly_owner_reference(
        self,
    ) -> None:
        holder = types.SimpleNamespace(
            _persist_reference=b"top-occurrence",
            GetCorrespondingEntity=_raiser(
                AssertionError("assembly-owned objects must not be converted")
            ),
            GetCorresponding=_raiser(
                AssertionError("assembly-owned objects must not be converted")
            ),
        )
        material = types.SimpleNamespace(
            GetLinkedDisplayStates=lambda: ["TopState"],
            GetEntities=lambda: [holder],
            PrimaryColor=0x000000FF,
            Transparency=0.0,
            ColorForm=1,
            TextureFilename="",
            SurfaceFinishFile="",
        )
        top_extension = types.SimpleNamespace(
            GetRenderMaterialsCount2=lambda _option, _names: 1,
            GetRenderMaterials2=lambda _option, _names: [material],
            GetPersistReference3=lambda entity: list(
                entity._persist_reference
            ),
        )
        empty_extension = types.SimpleNamespace(
            GetRenderMaterialsCount2=lambda _option, _names: 0,
            GetRenderMaterials2=lambda _option, _names: [],
            GetPersistReference3=_raiser(
                AssertionError("empty assembly owners must not read references")
            ),
        )
        top_document = types.SimpleNamespace(
            Extension=top_extension,
            GetPathName=lambda: "C:\\root.SLDASM",
        )
        parent_document = types.SimpleNamespace(
            Extension=empty_extension,
            GetPathName=lambda: "C:\\parent.SLDASM",
        )
        leaf_document = types.SimpleNamespace(
            Extension=empty_extension,
            GetPathName=lambda: "C:\\leaf.SLDPRT",
        )

        def owner(document, path, scope, state):
            return {
                "document_path": path,
                "owner_scope": scope,
                "document": document,
                "configuration": "Default",
                "display_state": state,
                "display_state_error": None,
                "display_states": (
                    [state] if scope == "top_assembly" else None
                ),
                "display_states_state": (
                    "observed"
                    if scope == "top_assembly"
                    else "not_applicable"
                ),
                "owner_error": None,
            }

        owner_rows = [
            owner(top_document, "C:\\root.SLDASM", "top_assembly", "TopState"),
            owner(
                parent_document,
                "C:\\parent.SLDASM",
                "parent_assembly",
                "ParentState",
            ),
            owner(
                leaf_document,
                "C:\\leaf.SLDPRT",
                "component_document",
                "LeafState",
            ),
        ]
        backend = _CaptureBackend(session_factory=lambda: None)
        with _com_stubs(), patch.object(
            backend,
            "_appearance_owner_rows",
            return_value=(owner_rows, []),
        ):
            records = backend._capture_state_render_materials(
                component="component-1",
                holder=holder,
                current={"component-1": holder},
                leaf_document=leaf_document,
                source_configuration="Default",
                leaf_display_state="LeafState",
                leaf_display_state_error=None,
                cache={},
            )

        top_record = records[0]
        entity = top_record["materials"][0]["entity_records"][0]
        occurrence = top_record["native_reference_index"][0]
        self.assertEqual(
            entity["persistent_reference"],
            list(b"top-occurrence"),
        )
        self.assertEqual(
            occurrence["persistent_reference"],
            entity["persistent_reference"],
        )
        self.assertEqual(
            entity["binding_context"],
            occurrence["binding_context"],
        )
        self.assertEqual(
            entity["binding_context"]["owner_scope"],
            "top_assembly",
        )
        self.assertEqual(occurrence["target"]["kind"], "occurrence")
        self.assertEqual(
            {
                row["target"]["kind"]
                for row in top_record["native_reference_index"]
            },
            {"occurrence"},
        )
        self.assertEqual(top_record["mapping_errors"], [])

    def test_component_document_materials_use_the_actual_owner_extension(
        self,
    ) -> None:
        source_face = types.SimpleNamespace(
            _persist_reference=b"leaf-face",
            _fixture_interfaces={"IEntity", "IFace2"},
        )
        material = types.SimpleNamespace(
            GetLinkedDisplayStates=lambda: ["LeafState"],
            GetEntities=lambda: [source_face],
            PrimaryColor=1,
            Transparency=0.0,
            ColorForm=1,
            TextureFilename="",
            SurfaceFinishFile="",
        )
        leaf_extension = types.SimpleNamespace(
            GetRenderMaterialsCount2=lambda _option, _names: 1,
            GetRenderMaterials2=lambda _option, _names: [material],
            GetPersistReference3=lambda entity: list(
                entity._persist_reference
            ),
            GetObjectByPersistReference3=lambda _pid, errors: (
                setattr(errors, "value", 0) or source_face
            ),
        )
        parent_extension = types.SimpleNamespace(
            GetPersistReference3=_raiser(
                AssertionError(
                    "component-document objects must not use the parent namespace"
                )
            )
        )
        top_extension = types.SimpleNamespace(
            GetPersistReference3=_raiser(
                AssertionError("the root assembly is not the immediate owner")
            )
        )
        leaf_document = types.SimpleNamespace(
            Extension=leaf_extension,
            GetPathName=lambda: "C:\\leaf.SLDPRT",
        )
        parent_document = types.SimpleNamespace(
            Extension=parent_extension,
            GetPathName=lambda: "C:\\parent.SLDASM",
        )
        top_document = types.SimpleNamespace(
            Extension=top_extension,
            GetPathName=lambda: "C:\\root.SLDASM",
        )

        def owner(document, path, scope, state):
            return {
                "document_path": path,
                "owner_scope": scope,
                "document": document,
                "configuration": "Default",
                "display_state": state,
                "display_state_error": None,
                "display_states": None,
                "display_states_state": "not_applicable",
                "owner_error": None,
            }

        owner_rows = [
            owner(top_document, "C:\\root.SLDASM", "top_assembly", "Top"),
            owner(
                parent_document,
                "C:\\parent.SLDASM",
                "parent_assembly",
                "Parent",
            ),
            owner(
                leaf_document,
                "C:\\leaf.SLDPRT",
                "component_document",
                "LeafState",
            ),
        ]
        parent_component = types.SimpleNamespace()
        holder = types.SimpleNamespace(
            GetCorrespondingEntity=_raiser(
                AssertionError(
                    "component-document objects must not be converted"
                )
            )
        )
        backend = _CaptureBackend(session_factory=lambda: None)
        with _com_stubs(), patch.object(
            backend,
            "_appearance_owner_rows",
            return_value=(owner_rows, ["sub-1"]),
        ):
            records = backend._capture_state_render_materials(
                component="sub-1/leaf-1",
                holder=holder,
                current={"sub-1": parent_component},
                leaf_document=leaf_document,
                source_configuration="Default",
                leaf_display_state="LeafState",
                leaf_display_state_error=None,
                cache={},
            )

        leaf_record = records[-1]
        entity = leaf_record["materials"][0]["entity_records"][0]
        self.assertEqual(entity["persistent_reference"], list(b"leaf-face"))
        self.assertEqual(
            entity["binding_context"]["owner_document_path"],
            "C:\\leaf.SLDPRT",
        )
        self.assertEqual(
            entity["native_identity"]["interface_kinds"],
            ["IFace2", "IEntity"],
        )
        self.assertEqual(leaf_record["native_reference_index"], [])

    def test_top_parent_and_leaf_use_their_own_configuration_and_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            parent_path = _write(root, "sub.SLDASM")
            leaf_path = _write(root, "arm.SLDPRT")
            top_document = _Doc(
                assembly,
                doc_type=2,
                display_states=["TopState"],
            )
            parent_document = _Doc(parent_path, doc_type=2)
            leaf_document = _Doc(leaf_path)
            top_calls = []
            parent_calls = []
            leaf_calls = []
            top_document.Extension.GetRenderMaterials2 = (
                lambda option, names: top_calls.append(
                    (option, tuple(names.value))
                )
                or []
            )
            top_document.Extension.GetRenderMaterialsCount2 = (
                lambda option, names: 0
            )
            parent_document.Extension.GetRenderMaterials2 = (
                lambda option, names: parent_calls.append(
                    (option, tuple(names.value))
                )
                or []
            )
            parent_document.Extension.GetRenderMaterialsCount2 = (
                lambda option, names: 0
            )
            leaf_document.Extension.GetRenderMaterials2 = (
                lambda option, names: leaf_calls.append(
                    (option, tuple(names.value))
                )
                or []
            )
            leaf_document.Extension.GetRenderMaterialsCount2 = (
                lambda option, names: 0
            )
            parent = _Component(
                "sub-1",
                parent_path,
                doc=parent_document,
                configuration="ParentConfig",
            )
            parent.ReferencedDisplayState2 = "ParentState"
            leaf = _Component(
                "sub-1/arm-1",
                leaf_path,
                doc=leaf_document,
                configuration="LeafConfig",
            )
            leaf.ReferencedDisplayState2 = "LeafState"
            backend = _CaptureBackend(session_factory=lambda: None)
            with _com_stubs(), patch.object(
                backend,
                "_captured_document",
                return_value=top_document,
            ):
                rows, ancestors = backend._appearance_owner_rows(
                    "sub-1/arm-1",
                    leaf,
                    {"sub-1": parent, "sub-1/arm-1": leaf},
                    leaf_document,
                    "LeafState",
                    None,
                )

            by_scope = {row["owner_scope"]: row for row in rows}
            self.assertEqual(ancestors, ["sub-1"])
            self.assertEqual(by_scope["top_assembly"]["configuration"], "Default")
            self.assertEqual(by_scope["top_assembly"]["display_state"], "TopState")
            self.assertEqual(
                by_scope["parent_assembly"]["configuration"],
                "ParentConfig",
            )
            self.assertEqual(
                by_scope["parent_assembly"]["display_state"],
                "ParentState",
            )
            self.assertEqual(
                by_scope["component_document"]["display_state"],
                "LeafState",
            )

            cache = {}
            with _com_stubs(), patch.object(
                backend,
                "_captured_document",
                return_value=top_document,
            ):
                first = backend._capture_state_render_materials(
                    component="sub-1/arm-1",
                    holder=leaf,
                    current={"sub-1": parent, "sub-1/arm-1": leaf},
                    leaf_document=leaf_document,
                    source_configuration="LeafConfig",
                    leaf_display_state="LeafState",
                    leaf_display_state_error=None,
                    cache=cache,
                )
                second = backend._capture_state_render_materials(
                    component="sub-1/arm-1",
                    holder=leaf,
                    current={"sub-1": parent, "sub-1/arm-1": leaf},
                    leaf_document=leaf_document,
                    source_configuration="LeafConfig",
                    leaf_display_state="LeafState",
                    leaf_display_state_error=None,
                    cache=cache,
                )
            self.assertEqual(first, second)
            self.assertEqual(top_calls, [(3, ("TopState",))])
            self.assertEqual(parent_calls, [(3, ("ParentState",))])
            self.assertEqual(leaf_calls, [(3, ("LeafState",))])

    def test_cached_owner_facts_are_remapped_per_occurrence(self) -> None:
        render_calls = []
        face_a = types.SimpleNamespace(
            _persist_reference=b"face-a",
            _fixture_interfaces={"IEntity", "IFace2"},
        )
        failing_face = types.SimpleNamespace(
            _persist_reference=b"face-fail",
            _fixture_interfaces={"IEntity", "IFace2"},
        )
        null_reference_face = types.SimpleNamespace(
            _persist_reference=b"face-null",
            _fixture_interfaces={"IEntity", "IFace2"},
        )
        invalid_reference_face = types.SimpleNamespace(
            _persist_reference=b"face-invalid",
            _fixture_interfaces={"IEntity", "IFace2"},
        )
        material = types.SimpleNamespace(
            GetLinkedDisplayStates=lambda: ["SharedState"],
            GetEntities=lambda: [face_a],
            PrimaryColor=1,
            Transparency=0.0,
            ColorForm=1,
            TextureFilename="",
            SurfaceFinishFile="",
        )
        extension = types.SimpleNamespace()

        def persistent_reference(entity):
            if entity is failing_face:
                raise RuntimeError("persistent reference failed")
            if entity is null_reference_face:
                return None
            if entity is invalid_reference_face:
                return [1, 300]
            return list(entity._persist_reference)

        extension.GetPersistReference3 = persistent_reference
        extension.GetObjectByPersistReference3 = lambda pid, errors: (
            setattr(errors, "value", 0) or face_a
        )
        extension.GetRenderMaterials2 = (
            lambda option, names: render_calls.append(
                (option, tuple(names.value))
            )
            or [material]
        )
        extension.GetRenderMaterialsCount2 = lambda _option, _names: 1
        owner_document = types.SimpleNamespace(Extension=extension)
        context_extension = types.SimpleNamespace(
            GetPersistReference3=_raiser(
                AssertionError(
                    "component-document objects must not use assembly references"
                )
            )
        )
        top_document = types.SimpleNamespace(
            Extension=context_extension
        )
        top_owner = {
            "document_path": "C:\\robot.SLDASM",
            "owner_scope": "top_assembly",
            "document": top_document,
            "configuration": "Default",
            "display_state": "TopState",
            "display_state_error": None,
            "display_states": ["TopState"],
            "display_states_state": "observed",
            "owner_error": None,
        }
        owner = {
            "document_path": "C:\\shared.SLDPRT",
            "owner_scope": "component_document",
            "document": owner_document,
            "configuration": None,
            "display_state": "SharedState",
            "display_state_error": None,
            "display_states": None,
            "display_states_state": "not_applicable",
            "owner_error": None,
        }

        holder_a = types.SimpleNamespace(
            GetCorrespondingEntity=_raiser(
                AssertionError("component-document objects must not be converted")
            )
        )
        holder_b = types.SimpleNamespace(
            GetCorrespondingEntity=_raiser(
                AssertionError("component-document objects must not be converted")
            )
        )
        backend = _CaptureBackend(session_factory=lambda: None)
        cache = {}
        with _com_stubs(), patch.object(
            backend,
            "_appearance_owner_rows",
            side_effect=[
                ([top_owner, owner], []),
                ([top_owner, owner], []),
            ],
        ):
            first = backend._capture_state_render_materials(
                component="component-a",
                holder=holder_a,
                current={"component-a": holder_a},
                leaf_document=owner_document,
                source_configuration="Default",
                leaf_display_state="SharedState",
                leaf_display_state_error=None,
                cache=cache,
            )
            second = backend._capture_state_render_materials(
                component="component-b",
                holder=holder_b,
                current={"component-b": holder_b},
                leaf_document=owner_document,
                source_configuration="Default",
                leaf_display_state="SharedState",
                leaf_display_state_error=None,
                cache=cache,
            )

        self.assertEqual(render_calls, [(3, ("SharedState",))])
        first_entity = first[1]["materials"][0]["entity_records"][0]
        second_entity = second[1]["materials"][0]["entity_records"][0]
        self.assertEqual(first_entity["read_errors"], {})
        self.assertEqual(second_entity["read_errors"], {})
        self.assertEqual(first[1]["native_reference_index"], [])
        self.assertEqual(second[1]["native_reference_index"], [])
        self.assertEqual(first[1]["mapping_errors"], [])
        self.assertEqual(second[1]["mapping_errors"], [])


class AppearanceProductionInterfaceTests(unittest.TestCase):
    def test_non_com_values_never_use_fixture_interface_labels(self) -> None:
        value, error = native_module._appearance_interface(
            types.SimpleNamespace(_fixture_interfaces={"IFace2"}),
            native_module.IFACE2_IID,
            "IFace2",
        )
        self.assertIsNone(value)
        self.assertIsNone(error)

    def test_com_values_use_the_public_interface_query(self) -> None:
        raw = types.SimpleNamespace(_oleobj_=object())
        bound = object()
        with patch.object(
            native_module,
            "_interface",
            return_value=bound,
        ) as interface_query:
            value, error = native_module._appearance_interface(
                raw,
                native_module.IFACE2_IID,
                "IFace2",
            )
        self.assertIs(value, bound)
        self.assertIsNone(error)
        interface_query.assert_called_once_with(
            raw,
            native_module.IFACE2_IID,
        )


class PropertyContractTests(unittest.TestCase):
    """Vendor six-argument Get6 by-ref contract, no deprecated Get fallback."""

    def _identity_record(self, root, **doc_kwargs):
        assembly = _write(root, "robot.SLDASM")
        doc = _Doc(assembly, **doc_kwargs)
        return assembly, _read(root, _App({assembly: doc}))

    def test_get6_byref_records_resolved_value_and_flags(self) -> None:
        manager = _PropertyManager(
            {
                "dp.hardware_id": {"value": "raw-id", "resolved": "m3.0", "was_resolved": True, "linked": True},
                "dp.revision": {"value": "r1", "resolved": "r1", "linked": False},
            }
        )
        with TemporaryDirectory() as tmp:
            _assembly, record = self._identity_record(Path(tmp), scope_managers={"": manager})

        # ResolvedValOut is recorded, not ValOut, and both by-ref calls used the
        # exact six-argument vendor signature with ResolvedFlag False.
        self.assertEqual(record["identity"]["hardware_id"], "m3.0")
        self.assertEqual(record["identity"]["revision"], "r1")
        # The record reads the identity once from the same six-argument by-ref
        # contract; selection never probes document properties.
        self.assertEqual(
            manager.get6_calls,
            [
                ("dp.hardware_id", False),
                ("dp.revision", False),
            ],
        )
        self.assertEqual(manager.get_calls, [])

    def test_get6_status_must_be_actual_two(self) -> None:
        for status in (True, 1, 3, 1.0, "2"):
            with self.subTest(status=status), TemporaryDirectory() as tmp:
                manager = _PropertyManager({"dp.x": {"value": "v", "resolved": "v", "status": status}})
                with self.assertRaises(CadError) as caught:
                    self._identity_record(Path(tmp), scope_managers={"": manager})
                self.assertEqual(caught.exception.code, "cad_property_unreadable")

    def test_get6_requires_resolved_true_and_bool_link(self) -> None:
        cases = ({"was_resolved": False}, {"was_resolved": 1}, {"linked": 1}, {"linked": None})
        for extra in cases:
            spec = {"value": "v", "resolved": "v", **extra}
            with self.subTest(spec=extra), TemporaryDirectory() as tmp:
                manager = _PropertyManager({"dp.x": spec})
                with self.assertRaises(CadError) as caught:
                    self._identity_record(Path(tmp), scope_managers={"": manager})
                self.assertEqual(caught.exception.code, "cad_property_unreadable")

    def test_get6_requires_string_values(self) -> None:
        cases = ({"value": 5}, {"resolved": None}, {"resolved": 3.0})
        for extra in cases:
            spec = {"value": "v", "resolved": "v", **extra}
            with self.subTest(spec=extra), TemporaryDirectory() as tmp:
                manager = _PropertyManager({"dp.x": spec})
                with self.assertRaises(CadError) as caught:
                    self._identity_record(Path(tmp), scope_managers={"": manager})
                self.assertEqual(caught.exception.code, "cad_property_unreadable")

    def test_empty_namespace_is_valid(self) -> None:
        for names in ([], None):
            with self.subTest(names=names), TemporaryDirectory() as tmp:
                manager = _PropertyManager({}, names=names)
                _assembly, record = self._identity_record(Path(tmp), scope_managers={"": manager})
                self.assertIsNone(record["identity"]["hardware_id"])
                self.assertEqual(manager.get6_calls, [])

    def test_unreadable_property_enumerations_block_with_detail(self) -> None:
        for index, manager in enumerate(
            (None, _PropertyManager({}, get_names_error=RuntimeError("no names"))), start=1
        ):
            with self.subTest(manager=index), TemporaryDirectory() as tmp:
                kwargs = {"custom_property_manager": None} if manager is None else {"scope_managers": {"": manager}}
                with self.assertRaises(CadError) as caught:
                    self._identity_record(Path(tmp), **kwargs)
                error = caught.exception
                self.assertEqual(error.code, "cad_property_unreadable")
                self.assertEqual(error.detail["configuration"], "")
                self.assertIn("document", error.detail)

    def test_get6_failure_reports_the_property_and_never_uses_get(self) -> None:
        manager = _PropertyManager({"dp.x": {"value": "v", "error": RuntimeError("get6 failed")}})
        with TemporaryDirectory() as tmp, self.assertRaises(CadError) as caught:
            self._identity_record(Path(tmp), scope_managers={"": manager})
        error = caught.exception
        self.assertEqual(error.code, "cad_property_unreadable")
        self.assertEqual(error.detail["property"], "dp.x")
        self.assertEqual(error.detail["configuration"], "")
        self.assertEqual(manager.get_calls, [], "the deprecated Get fallback must never be used")

    def test_suppressed_components_skip_property_reads(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            arm_doc = _Doc(arm_path, scope_managers={"": _PropertyManager({"dp.role": "left"})})
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            ghost_path = _write(root, "ghost.SLDPRT")
            ghost_doc = _Doc(ghost_path, custom_property_manager=None)
            ghost_component = _Component("ghost-1", ghost_path, doc=ghost_doc, suppressed=True)
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, children=[arm_component, ghost_component])

            record = _read(root, _App({assembly: main_doc}))

            properties = record["properties"]["components"]
            self.assertEqual(properties["arm-1"]["dp.role"], "left")
            self.assertNotIn("ghost-1", properties)

    def test_configuration_conflict_and_unreadable_scope_block(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            conflict_doc = _Doc(
                arm_path,
                properties={"dp.role": "left"},
                configuration_properties={"Right": {"dp.role": "right"}},
                configuration_children={"Right": []},
            )
            component = _Component("arm-1", arm_path, doc=conflict_doc, configuration="Right")
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, children=[component])
            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: main_doc}))
            self.assertEqual(caught.exception.code, "cad_property_conflict")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            unreadable_doc = _Doc(
                arm_path,
                properties={"dp.role": "left"},
                configuration_children={"Right": []},
                scope_managers={"Right": _PropertyManager({}, get_names_error=RuntimeError("no scope"))},
            )
            component = _Component("arm-1", arm_path, doc=unreadable_doc, configuration="Right")
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, children=[component])
            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: main_doc}))
            error = caught.exception
            self.assertEqual(error.code, "cad_property_unreadable")
            self.assertEqual(error.detail["configuration"], "Right")


class MainAssemblySelectionTests(unittest.TestCase):
    """Explicit selection or a sole frozen assembly; ambiguity never opens CAD."""

    def _delivery_scene(self, root: Path, name: str):
        stem = name.split(".", 1)[0]
        base = _write(root, f"{stem}-base.SLDPRT")
        component = _Component("base-1", base)
        seat = _mate(
            "seat",
            0,
            [_MateEntity(component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)))],
        )
        group = _Feature("MateGroup", "MateGroup", first_sub=seat)
        assembly = _write(root, name)
        properties = {"dp.hardware_id": "m3.0", "dp.delivery_configuration": "Default"}
        doc = _Doc(assembly, first_feature=group, children=[component], properties=properties)
        return assembly, doc

    @staticmethod
    def _unreachable_session_backend() -> SolidWorksBackend:
        def _raise():  # pragma: no cover - the session must never be requested
            raise AssertionError("a CAD session must not be started")

        return SolidWorksBackend(session_factory=_raise)

    def test_sole_assembly_is_accepted_without_a_selection(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly, doc = self._delivery_scene(root, "robot.SLDASM")

            record = _read(root, _App({assembly: doc}), {})

            self.assertEqual(record["identity"]["main_assembly"], "robot.SLDASM")
            self.assertEqual(record["identity"]["hardware_id"], "m3.0")
            self.assertEqual({"mode": "sole_candidate", "main_assembly": "robot.SLDASM"}, record["selection"])

    def test_several_assemblies_require_a_selection_before_any_session(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "aaa.SLDASM")
            _write(root, "zzz.SLDASM")

            with self.assertRaises(CadError) as caught:
                self._unreachable_session_backend().discover_native(root, {})

            error = caught.exception
            self.assertEqual(error.code, "native_discovery_main_assembly_ambiguous")
            self.assertEqual(error.detail["candidates"], ["aaa.SLDASM", "zzz.SLDASM"])
            self.assertIn("select the delivered main assembly", error.message)

    def test_declared_markers_do_not_select_a_main_assembly(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = {"dp.hardware_id": "m3.0", "dp.delivery_configuration": "Default"}
            first = _write(root, "first.SLDASM")
            second = _write(root, "second.SLDASM")
            app = _App(
                {
                    first: _Doc(first, doc_type=2, properties=dict(marker)),
                    second: _Doc(second, doc_type=2, properties=dict(marker)),
                }
            )

            with self.assertRaises(CadError) as caught:
                _read(root, app, {})

            error = caught.exception
            self.assertEqual(error.code, "native_discovery_main_assembly_ambiguous")
            self.assertEqual(error.detail["candidates"], ["first.SLDASM", "second.SLDASM"])

    def test_no_assembly_fails_before_any_session(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "base.SLDPRT")

            with self.assertRaises(CadError) as caught:
                self._unreachable_session_backend().discover_native(root, {})

            self.assertEqual(caught.exception.code, "native_discovery_assembly_missing")

    def test_known_missing_reference_open_continues_with_exact_diagnostics(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(
                assembly,
                doc_type=2,
                properties={"dp.hardware_id": "m3.0", "dp.delivery_configuration": "Default"},
            )
            app = _App(
                {assembly: doc},
                open_errors={assembly: 2},
                not_preopened={assembly},
                dependencies={
                    assembly: (
                        "foot",
                        str(assembly),
                        False,
                        "imported_part",
                        r"C:\missing\imported_part.SLDPRT",
                        True,
                    )
                },
            )

            record = _read(root, app)
            status = record["open_status"]
            self.assertEqual(status["classification"], "continuable_missing_referenced_documents")
            self.assertEqual(status["errors"], 2)
            self.assertEqual(
                status["missing_references"]["items"],
                [{"name": "imported_part", "last_known_path": r"C:\missing\imported_part.SLDPRT"}],
            )
            self.assertNotIn("source_integrity_proven", status)

    def test_missing_reference_evidence_retains_every_enumerated_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            missing = [
                {"name": f"part-{index:02d}", "last_known_path": f"C:/missing/part-{index:02d}.SLDPRT"}
                for index in range(40)
            ]
            app = _App(
                {assembly: _Doc(assembly, doc_type=2)},
                open_errors={assembly: 2},
                not_preopened={assembly},
                dependencies={
                    assembly: tuple(
                        value for item in missing
                        for value in (item["name"], item["last_known_path"], True)
                    )
                },
            )
            self.assertEqual(_read(root, app)["open_status"]["missing_references"]["items"], missing)

    def test_missing_reference_warning_does_not_hide_unknown_component_source(self) -> None:
        for source_kind in ("absent", "external", "unknown_virtual", "import_error"):
            with self.subTest(source_kind=source_kind), TemporaryDirectory() as tmp:
                root = Path(tmp)
                assembly = _write(root, "robot.SLDASM")
                component = _Component("missing-1", "", suppressed=True)
                component._doc = None
                component.GetImportedPath = lambda: None
                if source_kind == "external":
                    component._path = str(root.parent / "outside.SLDPRT")
                    component._suppression = 2
                elif source_kind == "unknown_virtual":
                    component.IsVirtual = None
                elif source_kind == "import_error":
                    component.GetImportedPath = _raiser(RuntimeError("import path unreadable"))
                doc = _Doc(assembly, doc_type=2, children=[component])
                app = _App({assembly: doc}, open_errors={assembly: 2}, not_preopened={assembly},
                    dependencies={assembly: ("construction", "C:/missing/construction.SLDPRT", True)})
                if source_kind == "absent":
                    record = _read(root, app)
                    self.assertEqual(record["unresolved_components"][0]["instance"], "missing-1")
                else:
                    with self.assertRaises(CadError) as caught:
                        _read(root, app)
                    self.assertEqual(
                        caught.exception.code,
                        "cad_component_unresolved" if source_kind == "external" else "cad_component_source_unread",
                    )

    def test_missing_reference_open_cannot_skip_an_active_unloaded_component(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            component = _Component("active-1", "")
            component._doc = None
            component.GetImportedPath = lambda: None
            app = _App(
                {assembly: _Doc(assembly, doc_type=2, children=[component])},
                open_errors={assembly: 2}, not_preopened={assembly},
                    dependencies={assembly: ("construction", "C:/missing/construction.SLDPRT", True)},
            )
            with self.assertRaises(CadError):
                _read(root, app)

    def test_missing_reference_enumeration_failure_is_explicitly_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(
                assembly,
                doc_type=2,
                properties={"dp.hardware_id": "m3.0", "dp.delivery_configuration": "Default"},
            )
            app = _App(
                {assembly: doc},
                open_errors={assembly: 2},
                not_preopened={assembly},
                dependency_error=RuntimeError("RPC unavailable"),
            )

            with self.assertRaises(CadError) as caught:
                _read(root, app)
            self.assertEqual(caught.exception.detail["errors"], 2)
            missing = caught.exception.detail["missing_references"]
            self.assertEqual(missing["state"], "unavailable")
            self.assertEqual(missing["items"], [])
            self.assertIn("RPC unavailable", str(missing["error"]))

    def test_malformed_dependency_reply_keeps_open_status_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, doc_type=2)
            app = _App({assembly: doc}, open_errors={assembly: 2}, not_preopened={assembly},
                    dependencies={assembly: ("construction", "C:/missing/construction.SLDPRT", True)})
            app.GetDocumentDependencies2 = lambda *args: 7
            with self.assertRaises(CadError) as caught:
                _read(root, app)
            self.assertEqual(caught.exception.detail["errors"], 2)
            self.assertEqual(caught.exception.detail["missing_references"]["state"], "unavailable")
            self.assertIn("native triples", str(caught.exception.detail["missing_references"]["error"]))

    def test_unreadable_open_status_blocks_the_selected_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(
                assembly,
                doc_type=2,
                properties={"dp.hardware_id": "m3.0", "dp.delivery_configuration": "Default"},
            )
            app = _App({assembly: doc}, open_errors={assembly: "unreadable"}, not_preopened={assembly})

            with self.assertRaises(CadError) as caught:
                _read(root, app)

            error = caught.exception
            self.assertEqual(error.code, "cad_document_open_failed")
            self.assertIsNone(error.detail["errors"])

    def test_transitively_open_main_uses_its_fresh_exact_missing_reference_status(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            carried = _write(root, "robot.SLDASM")
            carried_doc = _Doc(
                carried,
                doc_type=2,
                properties={"dp.hardware_id": "m3.0", "dp.delivery_configuration": "Default"},
            )
            carrier = _write(root, "carrier.SLDASM")
            component = _Component("robot-1", carried, doc=carried_doc)
            carrier_doc = _Doc(carrier, doc_type=2, children=[component])
            app = _App(
                {carrier: carrier_doc}, open_errors={carried: 2},
                dependencies={carried: ("construction", "C:/missing/construction.SLDPRT", True)},
            )

            record = _read(root, app)
            self.assertEqual(record["open_status"]["errors"], 2)
            self.assertEqual(
                record["open_status"]["classification"],
                "continuable_missing_referenced_documents",
            )

    def test_main_gate_still_refuses_unknown_or_mixed_open_errors(self) -> None:
        for error_value in (1, 3, 6):
            with self.subTest(error_value=error_value), TemporaryDirectory() as tmp:
                root = Path(tmp)
                assembly = _write(root, "robot.SLDASM")
                doc = _Doc(
                    assembly,
                    doc_type=2,
                    properties={"dp.hardware_id": "m3.0", "dp.delivery_configuration": "Default"},
                )
                app = _App(
                    {assembly: doc},
                    open_errors={assembly: error_value},
                    not_preopened={assembly},
                )
                with self.assertRaises(CadError) as caught:
                    _read(root, app)
                self.assertEqual(caught.exception.code, "cad_document_open_failed")
                self.assertEqual(caught.exception.detail["errors"], error_value)

    def test_exact_root_probe_observes_the_native_status_even_if_already_resident(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            app = _App({assembly: _Doc(assembly, doc_type=2)})
            backend = SolidWorksBackend(session_factory=lambda: _Session(app))
            with _com_stubs():
                backend.seal_documents([assembly])
                entry = backend._probe_document(str(assembly))
            self.assertNotIn("already_open", entry)
            self.assertEqual(entry["errors"], 0)
            self.assertEqual(entry["warnings"], 0)

    def test_clean_reference_open_keeps_selected_main_status(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            part = _write(root, "part.SLDPRT")
            app = _App(
                {assembly: _Doc(assembly, doc_type=2), part: _Doc(part)},
                open_errors={assembly: 2}, not_preopened={assembly, part},
                dependencies={assembly: ("construction", "C:/missing/construction.SLDPRT", True)},
            )
            backend = SolidWorksBackend(session_factory=lambda: _Session(app))
            with _com_stubs():
                backend.prepare_prepared_capture(root, assembly, "Default")
                opened = backend.open_document(str(assembly))
                reference_opened = backend.open_document(str(part))
            self.assertEqual(opened["errors"], 2)
            self.assertEqual(reference_opened["errors"], 0)

    def test_prepared_capture_allows_missing_error_only_for_the_bound_main(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            part = _write(root, "part.SLDPRT")
            app = _App(
                {
                    assembly: _Doc(assembly, doc_type=2),
                    part: _Doc(part),
                },
                open_errors={assembly: 2, part: 2},
                dependencies={assembly: ("construction", "C:/missing/construction.SLDPRT", True)},
                not_preopened={assembly, part},
            )
            backend = SolidWorksBackend(session_factory=lambda: _Session(app))
            with _com_stubs():
                backend.prepare_prepared_capture(root, assembly, "Default")
                opened = backend.open_document(str(assembly))
                self.assertEqual(opened["errors"], 2)
                self.assertNotIn("source_integrity_proven", opened)
                with self.assertRaises(CadError) as caught:
                    backend.open_document(str(part))
                self.assertEqual(caught.exception.code, "cad_document_open_failed")
                self.assertEqual(caught.exception.detail["errors"], 2)


class EntityGeometryTests(unittest.TestCase):
    """The reader decodes real surface/curve/vertex primitives, never heuristics."""

    def _record(self, tmp, entities):
        root = Path(tmp)
        base = _write(root, "base.SLDPRT")
        component = _Component("base-1", base)
        mates = [_mate(name, 0, [entity]) for name, entity in entities]
        for current, following in pairwise(mates):
            current._next_sub = following
        group = _Feature("MateGroup", "MateGroup", first_sub=mates[0])
        assembly = _write(root, "robot.SLDASM")
        doc = _Doc(assembly, first_feature=group, children=[component])
        return _read(root, _App({assembly: doc}))

    def test_infinite_cylinder_geometry_is_normalized(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            entity = _MateEntity(component, "Cylinder1", _Cylinder((0.1, 0.2, 0.3), (0.0, 3.0, 0.0), 0.05))

            record = self._record(tmp, [("seat", entity)])

            geometry = record["mates"][0]["entities"][0]
            self.assertEqual(
                set(geometry),
                {
                    "component",
                    "entity_index",
                    "feature",
                    "face_index",
                    "reference_configuration",
                    "cylinder",
                    "mate_entity_reference",
                },
            )
            self.assertEqual(
                geometry["cylinder"],
                {"point": [0.1, 0.2, 0.3], "direction": [0.0, 1.0, 0.0], "radius": 0.05},
            )
            self.assertEqual(geometry["component"], "base-1")
            self.assertEqual(geometry["feature"], "Cylinder1")

    def test_circle_geometry_is_normalized(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            entity = _MateEntity(component, "Circle1", curve=_Circle((1.0, 2.0, 3.0), (0.0, 0.0, -2.0), 0.25))

            record = self._record(tmp, [("seat", entity)])

            geometry = record["mates"][0]["entities"][0]
            self.assertEqual(
                set(geometry),
                {
                    "component",
                    "entity_index",
                    "feature",
                    "face_index",
                    "reference_configuration",
                    "circle",
                    "mate_entity_reference",
                },
            )
            self.assertEqual(
                geometry["circle"],
                {"center": [1.0, 2.0, 3.0], "normal": [0.0, 0.0, -1.0], "radius": 0.25},
            )

    def test_plane_geometry_uses_documented_normal_then_point(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            both_unit = _MateEntity(component, "Face1", _Plane((0.0, 1.0, 0.0), (1.0, 0.0, 0.0)))
            non_unit_normal = _MateEntity(component, "Face2", _Plane((0.0, 0.0, 2.0), (1.0, 0.0, 0.0)))

            record = self._record(tmp, [("both_unit", both_unit), ("scaled_normal", non_unit_normal)])

            geometry = {mate["name"]: mate["entities"][0] for mate in record["mates"]}
            self.assertEqual(geometry["both_unit"]["plane"], {"normal": [0.0, 1.0, 0.0], "point": [1.0, 0.0, 0.0]})
            self.assertEqual(geometry["scaled_normal"]["plane"], {"normal": [0.0, 0.0, 1.0], "point": [1.0, 0.0, 0.0]})

    def test_vertex_point_geometry_is_recorded(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            entity = _MateEntity(component, "Vertex1", point=(1.0, 2.0, 3.0))

            record = self._record(tmp, [("seat", entity)])

            geometry = record["mates"][0]["entities"][0]
            self.assertEqual(
                set(geometry),
                {
                    "component",
                    "entity_index",
                    "feature",
                    "face_index",
                    "reference_configuration",
                    "point",
                    "mate_entity_reference",
                },
            )
            self.assertEqual(geometry["point"], [1.0, 2.0, 3.0])

    def test_nonfinite_entity_geometry_blocks(self) -> None:
        def cylinder(component):
            return _MateEntity(component, "C1", _Cylinder((float("nan"), 0.0, 0.0), (0.0, 0.0, 1.0), 0.05))

        def plane(component):
            return _MateEntity(component, "F1", _Plane((0.0, 0.0, 1.0), (float("nan"), 0.0, 0.0)))

        def circle(component):
            return _MateEntity(component, "C1", curve=_Circle((float("nan"), 0.0, 0.0), (0.0, 0.0, 1.0), 0.05))

        def vertex(component):
            return _MateEntity(component, "V1", point=(float("nan"), 0.0, 0.0))

        cases = (("cylinder", cylinder), ("plane", plane), ("circle", circle), ("vertex", vertex))
        for label, make_entity in cases:
            with self.subTest(geometry=label), TemporaryDirectory() as tmp:
                root = Path(tmp)

                def build(component, make=make_entity):
                    return _mate("seat", 0, [make(component)])

                app = _single_mate_scene(root, build)
                with self.assertRaises(CadError) as caught:
                    _read(root, app)
                self.assertEqual(caught.exception.code, "cad_geometry_nonfinite")


class MateFailureTests(unittest.TestCase):
    def _error(self, make_feature) -> CadError:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = _single_mate_scene(root, make_feature)
            with self.assertRaises(CadError) as caught:
                _read(root, app)
            return caught.exception

    def test_unreadable_mate_fields_block_with_structured_codes(self) -> None:
        cases = [
            (
                "cad_mate_limits_unreadable",
                lambda component: _mate(
                    "travel",
                    5,
                    [_MateEntity(component, "Plane1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)))],
                    lower=_raiser(RuntimeError("no variation")),
                    upper=0.1,
                ),
            ),
            (
                "cad_mate_unreadable",
                lambda component: _Feature(
                    "seat",
                    "MateCoincident",
                    specific=_MateSpecific(0, [_MateEntity(component, "Plane1")]),
                    suppressed=_raiser(RuntimeError("no suppression state")),
                ),
            ),
            (
                "cad_mate_unreadable",
                lambda component: _Feature(
                    "seat",
                    "MateCoincident",
                    specific=_MateSpecific(0, [_MateEntity(component, "Plane1")]),
                    error_code=_raiser(RuntimeError("no solve state")),
                ),
            ),
            (
                "cad_mate_unreadable",
                lambda component: _Feature(
                    "seat",
                    "MateCoincident",
                    specific=_raiser(RuntimeError("no specific feature")),
                ),
            ),
            (
                "cad_mate_unreadable",
                lambda component: _Feature(
                    "seat",
                    "MateCoincident",
                    specific=_MateSpecific(
                        0,
                        lambda index: (
                            _MateEntity(component, "Plane1") if index else _raiser(RuntimeError("no entity"))(index)
                        ),
                        count=1,
                    ),
                ),
            ),
        ]
        for expected_code, make_feature in cases:
            with self.subTest(code=expected_code, feature=make_feature):
                error = self._error(make_feature)
                self.assertEqual(error.code, expected_code)

    def test_invalid_entity_counts_block(self) -> None:
        for raw_count in (0, -1, 1.5, True, "2", float("nan")):
            with self.subTest(count=raw_count):
                error = self._error(
                    lambda component, count=raw_count: _Feature(
                        "seat",
                        "MateCoincident",
                        specific=_MateSpecific(0, [_MateEntity(component, "Plane1")], count=count),
                    )
                )
                self.assertEqual(error.code, "cad_mate_unreadable")

    def test_non_native_type_status_and_suppression_values_block(self) -> None:
        def entity(component):
            return [_MateEntity(component, "Plane1")]

        cases = (
            ("mate-type", lambda component, value: _mate("seat", value, entity(component)), (1.0, True, "1")),
            (
                "solve-state",
                lambda component, value: _Feature(
                    "seat", "MateCoincident", specific=_MateSpecific(0, entity(component)), error_code=value
                ),
                (0.0, True, "0"),
            ),
            (
                "suppression",
                lambda component, value: _Feature(
                    "seat", "MateCoincident", specific=_MateSpecific(0, entity(component)), suppressed=value
                ),
                (1, 0, "true", 0.0),
            ),
        )
        for label, make_feature, invalid_values in cases:
            for raw_value in invalid_values:
                with self.subTest(field=label, value=raw_value):
                    error = self._error(lambda component, make=make_feature, value=raw_value: make(component, value))
                    self.assertEqual(error.code, "cad_mate_unreadable")

    def test_invalid_or_unsupported_travel_blocks(self) -> None:
        def entity(component):
            return [_MateEntity(component, "Plane1")]

        cases = (
            ("cad_mate_limits_invalid", 5, float("inf"), 0.1),
            ("cad_mate_limits_invalid", 5, 0.2, 0.1),
            ("cad_mate_limits_invalid", 0, 0.0, 0.1),
        )
        for code, mate_type, lower, upper in cases:
            with self.subTest(code=code, mate_type=mate_type, lower=lower, upper=upper):
                error = self._error(
                    lambda component, mate_type=mate_type, lower=lower, upper=upper: _mate(
                        "travel", mate_type, entity(component), lower=lower, upper=upper
                    )
                )
                self.assertEqual(error.code, code)

    def test_ambiguous_or_unreadable_entity_scope_blocks(self) -> None:
        def wrong_component(component):
            reference = types.SimpleNamespace(
                Name2="ghost-1",
                GetPathName=component.GetPathName,
                ReferencedConfiguration="Default",
            )
            return _mate("seat", 0, [_MateEntity(reference, "Plane1")])

        error = self._error(wrong_component)
        self.assertEqual(error.code, "cad_mate_scope_ambiguous")
        self.assertEqual(error.detail["component"], "ghost-1")

    def test_feature_tree_and_mate_group_traversal_failures_block(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            assembly = _write(root, "robot.SLDASM")
            unreadable_tree = _Doc(assembly, first_feature=_raiser(RuntimeError("tree unreadable")))
            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: unreadable_tree}))
            self.assertEqual(caught.exception.code, "cad_mate_unreadable")

            group = _Feature("MateGroup", "MateGroup", first_sub=_raiser(RuntimeError("mate list unreadable")))
            doc = _Doc(assembly, first_feature=group, children=[component])
            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: doc}))
            self.assertEqual(caught.exception.code, "cad_mate_unreadable")


class ExplicitSelectionTests(unittest.TestCase):
    """One explicit main assembly opens exactly that entry; decoys are never scanned."""

    def _scene(self, root: Path):
        base = _write(root, "base.SLDPRT")
        component = _Component("base-1", base)
        seat = _mate("seat", 5, [_MateEntity(component, "Plane1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)))])
        group = _Feature("MateGroup", "MateGroup", first_sub=seat)
        assembly = _write(root, "robot.SLDASM")
        decoy = _write(root, "decoy.SLDASM")
        doc = _Doc(assembly, first_feature=group, children=[component])
        return _App({assembly: doc}), assembly, decoy

    def test_explicit_selection_opens_only_the_named_entry(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, _assembly, _decoy = self._scene(root)
            record = _read(root, app, {"main_assembly": "robot.SLDASM"})
            self.assertEqual("robot.SLDASM", record["identity"]["main_assembly"])
            self.assertEqual({"mode": "explicit", "main_assembly": "robot.SLDASM"}, record["selection"])
            self.assertIn("robot.SLDASM", record["files"])

    def test_explicit_selection_of_an_unreadable_decoy_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, _assembly, _decoy = self._scene(root)
            with self.assertRaises(CadError) as caught:
                _read(root, app, {"main_assembly": "decoy.SLDASM"})
            self.assertIn("decoy.SLDASM", str(caught.exception))
            self.assertNotEqual("native_discovery_main_assembly_ambiguous", caught.exception.code)

    def test_without_selection_the_decoy_still_blocks_as_ambiguous(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, _assembly, _decoy = self._scene(root)
            with self.assertRaises(CadError) as caught:
                _read(root, app, {})
            self.assertEqual("native_discovery_main_assembly_ambiguous", caught.exception.code)
            self.assertEqual(caught.exception.detail["candidates"], ["decoy.SLDASM", "robot.SLDASM"])

    def test_invalid_and_missing_selections_fail_before_any_session(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "robot.SLDASM").write_bytes(b"assembly")
            backend = SolidWorksBackend()  # no session factory: a CAD session must not be reached
            with self.assertRaises(CadError) as invalid:
                backend.discover_native(root, {"main_assembly": "..\\x.SLDASM"})
            self.assertEqual("native_discovery_selection_invalid", invalid.exception.code)
            with self.assertRaises(CadError) as foreign:
                backend.discover_native(root, {"main_assembly": "notes.txt"})
            self.assertEqual("native_discovery_selection_invalid", foreign.exception.code)
            with self.assertRaises(CadError) as missing:
                backend.discover_native(root, {"main_assembly": "missing.SLDASM"})
            self.assertEqual("native_discovery_selection_missing", missing.exception.code)


class AssemblyFrameMateTests(unittest.TestCase):
    """Mate entities on the assembly's own geometry bind to that assembly's frame."""

    def test_top_level_mate_to_the_assembly_frame_binds_to_the_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            assembly = _write(root, "robot.SLDASM")
            root_reference = _Component("robot", assembly, is_root=True)
            mate = _mate(
                "ground_pin",
                0,
                [
                    _MateEntity(component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                    _MateEntity(root_reference, "Face2", _Plane((0.0, 0.0, -1.0), (0.0, 0.0, 0.0))),
                ],
            )
            group = _Feature("MateGroup", "MateGroup", first_sub=mate)
            doc = _Doc(assembly, first_feature=group, children=[component])

            record = _read(root, _App({assembly: doc}))

            entities = record["mates"][0]["entities"]
            self.assertEqual(entities[0]["component"], "base-1")
            self.assertEqual(entities[1]["component"], "")
            self.assertIs(entities[1]["assembly_frame"], True)
            self.assertNotIn("reference_name", entities[1])

    def test_nested_mate_to_the_sub_assembly_frame_binds_to_its_occurrence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            arm_doc = _Doc(arm_path)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            sub_reference = _Component("sub", sub_path, is_root=True)
            nested = _mate(
                "nested_ground_pin",
                0,
                [
                    _MateEntity(arm_component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                    _MateEntity(sub_reference, "Face2", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                ],
            )
            sub_group = _Feature("MateGroup", "MateGroup", first_sub=nested)
            sub_doc = _Doc(
                sub_path,
                first_feature=sub_group,
                children=[arm_component],
                configuration="Sub",
                configuration_children={"Sub": [arm_component]},
            )
            sub_component = _Component("sub-1", sub_path, children=[arm_component], doc=sub_doc, configuration="Sub")
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, children=[sub_component])

            record = _read(root, _App({assembly: main_doc, sub_path: sub_doc}))

            nested_mate = record["mates"][0]
            self.assertEqual(nested_mate["scope"], "sub-1")
            entities = nested_mate["entities"]
            self.assertEqual(entities[0]["component"], "sub-1/arm-1")
            self.assertEqual(entities[1]["component"], "sub-1")
            self.assertIs(entities[1]["assembly_frame"], True)

    def test_unrecorded_reference_still_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            orphan_path = _write(root, "orphan.SLDPRT")
            component = _Component("base-1", base)
            orphan = _Component("orphan-1", orphan_path)
            mate = _mate(
                "dangling_seat",
                0,
                [
                    _MateEntity(component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                    _MateEntity(orphan, "Face2", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                ],
            )
            group = _Feature("MateGroup", "MateGroup", first_sub=mate)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, first_feature=group, children=[component])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: doc}))
            self.assertEqual(caught.exception.code, "cad_mate_scope_ambiguous")
            self.assertEqual(caught.exception.detail.get("component"), "orphan-1")

    def test_same_document_with_a_different_name_is_not_the_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            assembly = _write(root, "robot.SLDASM")
            imposter = _Component("robot (copy)", assembly)
            mate = _mate(
                "imposter_seat",
                0,
                [
                    _MateEntity(component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                    _MateEntity(imposter, "Face2", _Plane((0.0, 0.0, -1.0), (0.0, 0.0, 0.0))),
                ],
            )
            group = _Feature("MateGroup", "MateGroup", first_sub=mate)
            doc = _Doc(assembly, first_feature=group, children=[component])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: doc}))
            self.assertEqual(caught.exception.code, "cad_mate_scope_ambiguous")
            self.assertEqual(caught.exception.detail.get("component"), "robot (copy)")

    def test_explicit_is_root_false_refutes_the_root_reading(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            assembly = _write(root, "robot.SLDASM")
            not_root = _Component("robot", assembly)
            not_root._is_root = False
            mate = _mate(
                "not_root_seat",
                0,
                [
                    _MateEntity(component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                    _MateEntity(not_root, "Face2", _Plane((0.0, 0.0, -1.0), (0.0, 0.0, 0.0))),
                ],
            )
            group = _Feature("MateGroup", "MateGroup", first_sub=mate)
            doc = _Doc(assembly, first_feature=group, children=[component])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: doc}))
            self.assertEqual(caught.exception.code, "cad_mate_scope_ambiguous")

    def test_unreadable_is_root_is_not_the_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            assembly = _write(root, "robot.SLDASM")
            unreadable = _Component("robot", assembly, is_root=True)
            unreadable.IsRoot = _raiser(RuntimeError("unreadable"))
            mate = _mate(
                "unreadable_seat",
                0,
                [
                    _MateEntity(component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                    _MateEntity(unreadable, "Face2", _Plane((0.0, 0.0, -1.0), (0.0, 0.0, 0.0))),
                ],
            )
            group = _Feature("MateGroup", "MateGroup", first_sub=mate)
            doc = _Doc(assembly, first_feature=group, children=[component])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: doc}))
            self.assertEqual(caught.exception.code, "cad_mate_scope_ambiguous")
            self.assertEqual(caught.exception.detail.get("component"), "robot")

    def test_non_boolean_is_root_is_not_the_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            assembly = _write(root, "robot.SLDASM")
            not_bool = _Component("robot", assembly, is_root=True)
            not_bool.IsRoot = lambda: 1
            mate = _mate(
                "non_boolean_seat",
                0,
                [
                    _MateEntity(component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                    _MateEntity(not_bool, "Face2", _Plane((0.0, 0.0, -1.0), (0.0, 0.0, 0.0))),
                ],
            )
            group = _Feature("MateGroup", "MateGroup", first_sub=mate)
            doc = _Doc(assembly, first_feature=group, children=[component])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: doc}))
            self.assertEqual(caught.exception.code, "cad_mate_scope_ambiguous")
            self.assertEqual(caught.exception.detail.get("component"), "robot")

    def test_bare_name_document_match_is_not_the_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            assembly = _write(root, "robot.SLDASM")
            bare = _Component("robot", "robot.SLDASM", is_root=True)
            mate = _mate(
                "bare_name_seat",
                0,
                [
                    _MateEntity(component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                    _MateEntity(bare, "Face2", _Plane((0.0, 0.0, -1.0), (0.0, 0.0, 0.0))),
                ],
            )
            group = _Feature("MateGroup", "MateGroup", first_sub=mate)
            doc = _Doc(assembly, first_feature=group, children=[component])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: doc}))
            self.assertEqual(caught.exception.code, "cad_mate_scope_ambiguous")
            self.assertEqual(caught.exception.detail.get("component"), "robot")


class ReferenceCycleTests(unittest.TestCase):
    """A containment chain or mate that repeats a document is a reference cycle."""

    def test_self_contained_occurrence_is_a_reference_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            assembly = _write(root, "robot.SLDASM")
            inner = _Component("inner-1", base)
            self_occurrence = _Component("robot-2", assembly, children=[inner])
            doc = _Doc(assembly, children=[self_occurrence])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: doc}))
            self.assertEqual(caught.exception.code, "cad_reference_cycle")
            self.assertEqual(caught.exception.detail.get("component"), "robot-2")

    def test_ancestor_document_repeat_is_a_reference_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            assembly = _write(root, "robot.SLDASM")
            sub_path = _write(root, "sub.SLDASM")
            inner = _Component("inner-1", base)
            ancestor_copy = _Component("robot-2", assembly, children=[inner])
            sub_doc = _Doc(sub_path, children=[ancestor_copy])
            sub_component = _Component("b-1", sub_path, children=[ancestor_copy], doc=sub_doc)
            main_doc = _Doc(assembly, children=[sub_component])

            with self.assertRaises(CadError) as caught:
                _read(
                    root,
                    _App({assembly: main_doc, sub_path: sub_doc}),
                    settings={"main_assembly": "robot.SLDASM"},
                )
            self.assertEqual(caught.exception.code, "cad_reference_cycle")
            self.assertEqual(caught.exception.detail.get("component"), "b-1/robot-2")

    def test_mate_to_a_self_instance_occurrence_is_a_reference_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            assembly = _write(root, "robot.SLDASM")
            leaf_self = _Component("robot-2", assembly)
            mate = _mate(
                "self_seat",
                0,
                [
                    _MateEntity(component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))),
                    _MateEntity(leaf_self, "Face2", _Plane((0.0, 0.0, -1.0), (0.0, 0.0, 0.0))),
                ],
            )
            group = _Feature("MateGroup", "MateGroup", first_sub=mate)
            doc = _Doc(assembly, first_feature=group, children=[component, leaf_self])

            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: doc}))
            self.assertEqual(caught.exception.code, "cad_reference_cycle")
            self.assertEqual(caught.exception.detail.get("component"), "robot-2")


class MateEntityReferenceTests(unittest.TestCase):
    """Documented IMateEntity2 EntityParams provenance and geometry."""

    def _read_single_entity(self, tmp: Path, component: _Component, entity: _MateEntity) -> dict:
        assembly = _write(tmp, "robot.SLDASM")
        mate = _mate("seat", 0, [entity])
        group = _Feature("MateGroup", "MateGroup", first_sub=mate)
        doc = _Doc(assembly, first_feature=group, children=[component])
        return _read(tmp, _App({assembly: doc}))["mates"][0]["entities"][0]

    def test_line_reference_keeps_native_shape_and_params(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            entity = _MateEntity(
                component,
                "Edge1",
                reference_type=1,
                reference_type2=1,
                entity_params=[0.1, 0.2, 0.3, 0.0, 2.0, 0.0, 0.0, 0.0],
            )
            record = self._read_single_entity(root, component, entity)
            reference = record["mate_entity_reference"]
            self.assertEqual(reference["source"], "mate-entity-params")
            self.assertEqual(reference["reference_type"], 1)
            self.assertEqual(reference["reference_type_name"], "Line")
            self.assertEqual(reference["reference_type2"], 1)
            self.assertEqual(reference["space"], "mate-assembly")
            self.assertEqual(reference["params"], [0.1, 0.2, 0.3, 0.0, 2.0, 0.0, 0.0, 0.0])
            self.assertEqual(
                reference["geometry"],
                {"kind": "line", "point": [0.1, 0.2, 0.3], "direction": [0.0, 1.0, 0.0], "frame": "component-local"},
            )
            self.assertIsNone(reference["error"])

    def test_frame_reference_keeps_assembly_parameters_and_shape(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            assembly = _write(root, "robot.SLDASM")
            frame = _Component("robot", assembly, is_root=True)
            frame_entity = _MateEntity(
                frame,
                "Face2",
                reference_type=3,
                reference_type2=4,
                entity_params=[0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            )
            mate = _mate(
                "frame_seat",
                0,
                [_MateEntity(component, "Face1", _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0))), frame_entity],
            )
            group = _Feature("MateGroup", "MateGroup", first_sub=mate)
            doc = _Doc(assembly, first_feature=group, children=[component])
            record = _read(root, _App({assembly: doc}))["mates"][0]["entities"][1]
            self.assertEqual(record["component"], "")
            self.assertIs(record["assembly_frame"], True)
            reference = record["mate_entity_reference"]
            self.assertEqual(reference["reference_type_name"], "Plane")
            self.assertEqual(
                reference["geometry"],
                {"kind": "plane", "point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0], "frame": "component-local"},
            )
            self.assertIsNone(reference["error"])

    def test_entity_params_are_transformed_into_component_local_frame(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base, transform=_sw_translation(1.0, 2.0, 3.0))
            entity = _MateEntity(
                component,
                "Vertex1",
                reference_type=0,
                reference_type2=3,
                entity_params=[1.5, 2.5, 3.0],
            )
            record = self._read_single_entity(root, component, entity)
            reference = record["mate_entity_reference"]
            self.assertEqual(reference["params"], [1.5, 2.5, 3.0])
            self.assertEqual(reference["geometry"]["kind"], "point")
            for actual, expected in zip(reference["geometry"]["point"], [0.5, 0.5, 0.0], strict=True):
                self.assertAlmostEqual(actual, expected, places=9)
            self.assertEqual(reference["geometry"]["frame"], "component-local")
            self.assertIsNone(reference["error"])

    def test_entity_params_direction_rotates_into_component_local_frame(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            # SolidWorks ArrayData for a +90 degree rotation about Z (local X -> assembly Y).
            rotation = (0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
            component = _Component("base-1", base, transform=rotation)
            entity = _MateEntity(
                component,
                "Edge1",
                reference_type=1,
                reference_type2=1,
                entity_params=[1.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
            )
            record = self._read_single_entity(root, component, entity)
            geometry = record["mate_entity_reference"]["geometry"]
            self.assertEqual(geometry["kind"], "line")
            for actual, expected in zip(geometry["direction"], [0.0, -1.0, 0.0], strict=True):
                self.assertAlmostEqual(actual, expected, places=9)
            self.assertIsNone(record["mate_entity_reference"]["error"])

    def test_unreadable_entity_params_are_recorded_not_dropped(self) -> None:
        def raiser():
            raise RuntimeError("boom")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            entity = _MateEntity(component, "Face1", reference_type=1, reference_type2=1, entity_params=raiser)
            record = self._read_single_entity(root, component, entity)
            reference = record["mate_entity_reference"]
            self.assertIn("RuntimeError: boom", reference["error"])
            self.assertIsNone(reference["params"])
            self.assertIsNone(reference["geometry"])
            self.assertEqual(record["component"], "base-1")

    def test_unreadable_reference_type2_is_provenance_only(self) -> None:
        def raiser():
            raise RuntimeError("no type2")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            entity = _MateEntity(
                component,
                "Edge1",
                reference_type=1,
                reference_type2=raiser,
                entity_params=[0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
            )
            record = self._read_single_entity(root, component, entity)
            reference = record["mate_entity_reference"]
            self.assertIn("RuntimeError: no type2", reference["reference_type2_error"])
            self.assertIsNone(reference["reference_type2"])
            self.assertIsNone(reference["error"])
            self.assertEqual(reference["geometry"]["kind"], "line")

    def test_frame_matrix_requires_proper_rigid_rotation(self) -> None:
        identity = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
        scaled = (2.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
        reflected = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
        skewed_tail = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 2.0)
        boolean = (True, *identity[1:])
        self.assertIsNotNone(native_module._frame_matrix(identity))
        self.assertIsNone(native_module._frame_matrix(scaled))
        self.assertIsNone(native_module._frame_matrix(reflected))
        self.assertIsNone(native_module._frame_matrix(skewed_tail))
        self.assertIsNone(native_module._frame_matrix(boolean))
        self.assertIsNone(native_module._frame_matrix(identity[:12]))

    def test_malformed_entity_params_fail_closed(self) -> None:
        cases = (
            ("non-finite", {"reference_type": 1, "entity_params": [0.0, float("nan"), 0.0]}, "non-finite", None),
            ("non-numeric", {"reference_type": 1, "entity_params": [0.0, "x", 0.0]}, "non-numeric", None),
            (
                "short-plane",
                {"reference_type": 3, "entity_params": [0.0, 0.0, 0.0]},
                "documented layout",
                [0.0, 0.0, 0.0],
            ),
            (
                "zero-vector",
                {"reference_type": 1, "entity_params": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
                "zero-length",
                [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            ),
            (
                "bad-type",
                {"reference_type": "Plane", "entity_params": [0.0, 0.0, 0.0]},
                "ReferenceType",
                [0.0, 0.0, 0.0],
            ),
        )
        for name, kwargs, expected_error, expected_params in cases:
            with self.subTest(case=name), TemporaryDirectory() as tmp:
                root = Path(tmp)
                base = _write(root, "base.SLDPRT")
                component = _Component("base-1", base)
                entity = _MateEntity(component, "Face1", **kwargs)
                record = self._read_single_entity(root, component, entity)
                reference = record["mate_entity_reference"]
                self.assertIn(expected_error, reference["error"])
                self.assertEqual(reference["params"], expected_params)
                self.assertIsNone(reference["geometry"])

    def test_missing_occurrence_frame_rejects_geometry(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base, transform=())
            entity = _MateEntity(component, "Face1", reference_type=0, entity_params=[1.0, 2.0, 3.0])
            record = self._read_single_entity(root, component, entity)
            reference = record["mate_entity_reference"]
            self.assertIn("frame for this reference is unavailable", reference["error"])
            self.assertIsNone(reference["geometry"])
            self.assertEqual(reference["params"], [1.0, 2.0, 3.0])

    def test_nested_scope_params_localize_through_owner_and_child(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            sub_path = _write(root, "sub.SLDASM")
            arm_doc = _Doc(arm_path)
            # Child: +90 degree Z rotation plus translation (1, 2, 3) inside the sub-assembly.
            arm_transform = (0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 2.0, 3.0, 1.0, 0.0, 0.0, 0.0)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc, transform=arm_transform)
            sub_reference = _Component("sub", sub_path, is_root=True)
            nested = _mate(
                "nested_seat",
                0,
                [
                    _MateEntity(
                        arm_component,
                        "Face1",
                        _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)),
                        reference_type=0,
                        reference_type2=3,
                        entity_params=[2.0, 0.0, 4.0],
                    ),
                    _MateEntity(
                        sub_reference,
                        "Face2",
                        _Plane((0.0, 0.0, 1.0), (0.0, 0.0, 0.0)),
                        reference_type=3,
                        reference_type2=4,
                        entity_params=[0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
                    ),
                ],
            )
            sub_group = _Feature("MateGroup", "MateGroup", first_sub=nested)
            sub_doc = _Doc(
                sub_path,
                first_feature=sub_group,
                children=[arm_component],
                configuration="Sub",
                configuration_children={"Sub": [arm_component]},
            )
            # Owner: pure translation (10, 0, 0) in the top assembly.
            sub_component = _Component(
                "sub-1",
                sub_path,
                children=[arm_component],
                doc=sub_doc,
                configuration="Sub",
                transform=_sw_translation(10.0, 0.0, 0.0),
            )
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, children=[sub_component])

            record = _read(root, _App({assembly: main_doc, sub_path: sub_doc}))

            nested_mate = record["mates"][0]
            self.assertEqual(nested_mate["scope"], "sub-1")
            entities = nested_mate["entities"]
            self.assertEqual(entities[0]["component"], "sub-1/arm-1")
            arm_reference = entities[0]["mate_entity_reference"]
            self.assertIsNone(arm_reference["error"])
            self.assertEqual(arm_reference["params"], [2.0, 0.0, 4.0])
            for actual, expected in zip(arm_reference["geometry"]["point"], [-2.0, -1.0, 1.0], strict=True):
                self.assertAlmostEqual(actual, expected, places=9)
            self.assertEqual(arm_reference["geometry"]["frame"], "component-local")
            self.assertEqual(entities[1]["component"], "sub-1")
            self.assertIs(entities[1]["assembly_frame"], True)
            frame_reference = entities[1]["mate_entity_reference"]
            self.assertIsNone(frame_reference["error"])
            self.assertEqual(
                frame_reference["geometry"],
                {"kind": "plane", "point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0], "frame": "component-local"},
            )


class ComponentSuppressionStateTests(unittest.TestCase):
    """Raw ``swComponentSuppressionState_e``: emitted, coherent, fail-closed on mismatch."""

    def test_helper_reads_each_documented_state_once_and_refuses_the_rest(self):
        from description_pipeline.sources.solidworks.native import _component_state

        reads = []
        for state in (0, 1, 2, 3, 4):
            component = types.SimpleNamespace(GetSuppression2=lambda s=state: (reads.append(s), s)[1])
            self.assertEqual(_component_state(component), state)
        self.assertEqual(reads, [0, 1, 2, 3, 4])
        with self.assertRaises(CadError) as caught:
            _component_state(types.SimpleNamespace(GetSuppression2=lambda: 5, IsSuppressed=lambda: False))
        self.assertEqual(caught.exception.code, "cad_component_internal_id_mismatch")
        self.assertEqual(caught.exception.detail["enum"], "swComponentInternalIdMismatch")
        self.assertEqual(caught.exception.detail["state"], 5)
        unreadable = (
            types.SimpleNamespace(),
            types.SimpleNamespace(GetSuppression2=lambda: "3"),
            types.SimpleNamespace(GetSuppression2=lambda: True),
            types.SimpleNamespace(GetSuppression2=_raiser(RuntimeError("unavailable"))),
        )
        for bad in unreadable:
            with self.subTest(state=repr(bad)):
                with self.assertRaises(CadError) as caught:
                    _component_state(bad)
                self.assertEqual(caught.exception.code, "cad_component_state_unreadable")
        with self.assertRaises(CadError) as caught:
            _component_state(types.SimpleNamespace(GetSuppression2=lambda: 7))
        self.assertEqual(caught.exception.code, "cad_component_state_unsupported")
        self.assertEqual(caught.exception.detail["state"], 7)

    def test_collect_scene_refuses_a_mismatched_binding(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            bad = _Component("arm-1", _write(root, "arm.SLDPRT"), suppression=5)
            assembly = _write(root, "robot.SLDASM")
            documents = {assembly: _Doc(assembly, doc_type=2, children=[bad])}
            backend = _CaptureBackend(session_factory=lambda: _Session(_App(documents)))
            with self.assertRaises(CadError) as caught:
                backend.capture_scene_for_test(str(assembly), [])
            self.assertEqual(caught.exception.code, "cad_component_internal_id_mismatch")

    def test_measurement_paths_refuse_lightweight_occurrences(self):
        from description_pipeline.sources.solidworks.native import _measured_component_state

        for state in (1, 4):
            with self.subTest(state=state):
                with self.assertRaises(CadError) as caught:
                    _measured_component_state(types.SimpleNamespace(GetSuppression2=lambda s=state: s))
                self.assertEqual(caught.exception.code, "cad_component_lightweight_unresolved")
                self.assertEqual(caught.exception.detail["state"], state)
        for state in (0, 2, 3):
            with self.subTest(state=state):
                component = types.SimpleNamespace(GetSuppression2=lambda s=state: s)
                self.assertEqual(_measured_component_state(component), state)
        for state in (1, 4):
            with self.subTest(scene_state=state), TemporaryDirectory() as tmp, _com_stubs():
                root = Path(tmp)
                light = _Component("arm-1", _write(root, "arm.SLDPRT"), suppression=state)
                assembly = _write(root, "robot.SLDASM")
                documents = {assembly: _Doc(assembly, doc_type=2, children=[light])}
                backend = _CaptureBackend(session_factory=lambda documents=documents: _Session(_App(documents)))
                with self.assertRaises(CadError) as caught:
                    backend.capture_scene_for_test(str(assembly), [])
                self.assertEqual(caught.exception.code, "cad_component_lightweight_unresolved")

    def test_geometry_capture_refuses_a_lightweight_occurrence_after_discovery(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            light = _Component("arm-1", _write(root, "arm.SLDPRT"), suppression=1)
            assembly = _write(root, "robot.SLDASM")
            documents = {assembly: _Doc(assembly, doc_type=2, children=[light])}
            backend = _CaptureBackend(session_factory=lambda: _Session(_App(documents)))
            with _com_stubs():
                record = backend.discover_native(root, {"main_assembly": "robot.SLDASM"})
                entry = next(item for item in record["components"] if item["name2"] == "arm-1")
                self.assertEqual((entry["suppression_state"], entry["lightweight"]), (1, True))
                with self.assertRaises(CadError) as caught:
                    backend.capture_scene_for_test(str(assembly), [])
            self.assertEqual(caught.exception.code, "cad_component_lightweight_unresolved")

    def test_discover_native_refuses_mismatch_and_emits_raw_state_otherwise(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = _Component("arm-1", _write(root, "arm.SLDPRT"), suppression=5)
            assembly = _write(root, "robot.SLDASM")
            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: _Doc(assembly, doc_type=2, children=[bad])}))
            self.assertEqual(caught.exception.code, "cad_component_internal_id_mismatch")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolved = _Component("arm-1", _write(root, "arm.SLDPRT"), suppression=2)
            lightweight = _Component("arm-2", _write(root, "arm2.SLDPRT"), suppression=1)
            assembly = _write(root, "robot.SLDASM")
            record = _read(root, _App({assembly: _Doc(assembly, doc_type=2, children=[resolved, lightweight])}))
            self.assertNotIn("masses", record)
            by_name = {item["name2"]: item for item in record["components"]}

            def triple(name):
                item = by_name[name]
                return (item["suppression_state"], item["suppressed"], item["lightweight"])

            self.assertEqual(triple("arm-1"), (2, False, False))
            self.assertEqual(triple("arm-2"), (1, False, True))

    def test_discover_native_refuses_an_empty_component_name(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = _Component("base-1", _write(root, "base.SLDPRT"))
            unnamed = _Component("", _write(root, "orphan.SLDPRT"))
            assembly = _write(root, "robot.SLDASM")
            with self.assertRaises(CadError) as caught:
                _read(root, _App({assembly: _Doc(assembly, doc_type=2, children=[valid, unnamed])}))
            self.assertEqual(caught.exception.code, "cad_component_identity_unreadable")
            self.assertEqual(caught.exception.detail["raw_name"], "")

    def test_unreadable_child_transform_blocks_descent_without_fabrication(self):
        """An unreadable occurrence transform must not become an empty parent frame.

        Regression for the empty-parent-transform defect: the occurrence keeps its
        empty transform (a blocking finding downstream) and its recorded note, the
        subtree is skipped instead of composing against an empty matrix, and
        siblings still record their own placements.
        """

        class UnreadableTransformComponent(_Component):
            def GetTotalTransform(self, _presentation):
                return None

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            leaf = _Component("leaf-1", _write(root, "leaf.SLDPRT"))
            sub_path = _write(root, "sub.SLDASM")
            sub_doc = _Doc(sub_path, doc_type=2, children=[leaf])
            broken = UnreadableTransformComponent("sub-1", sub_path, children=[leaf], doc=sub_doc)
            sibling = _Component("arm-1", _write(root, "arm.SLDPRT"))
            assembly = _write(root, "robot.SLDASM")
            record = _read(root, _App({assembly: _Doc(assembly, doc_type=2, children=[broken, sibling])}))
            by_name = {item["name2"]: item for item in record["components"]}
            self.assertEqual(by_name["sub-1"]["transform"], [])
            self.assertNotIn("sub-1/leaf-1", by_name)
            self.assertEqual(len(by_name["arm-1"]["transform"]), 16)
            notes = record["notes"]
            self.assertTrue(
                any(note.startswith("transform:sub-1:") and "GetTotalTransform(False)" in note for note in notes),
                notes,
            )
            self.assertIn("transform:sub-1:subtree_skipped_unreadable_parent", notes)


class ComponentSourceDocumentTests(unittest.TestCase):
    """Raw import readings for the discovery walk (no selection carrier)."""

    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="sw-source-doc-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.source_root = self.tmp / "stage"
        self.source_root.mkdir()
        self.materialization = self.tmp / "swx1" / "IC~~" / "X.step.SLDPRT"
        self.materialization.parent.mkdir(parents=True)
        self.materialization.write_bytes(b"temp")

    def component(self, path, *, imported, virtual=False):
        component = _Component("X.step-1", str(path))
        component.GetImportedPath = lambda: imported
        component.IsVirtual = virtual
        return component

    def test_session_materialization_is_classified_with_raw_fields(self) -> None:
        from description_pipeline.sources.solidworks.native import _component_source_document

        result = _component_source_document(
            self.component(self.materialization, imported=r"D:\author\X.stp"),
            self.source_root,
        )
        self.assertIsNone(result["document"])
        provenance = result["source_provenance"]
        self.assertEqual(provenance["state"], "cached_materialization")
        self.assertEqual(provenance["kind"], "native_import_materialization")
        self.assertEqual(provenance["materialization_path"], str(self.materialization))
        self.assertEqual(provenance["session_temp_root"], str(self.materialization.parent.parent))
        self.assertEqual(provenance["author_imported_path"], r"D:\author\X.stp")
        self.assertIs(provenance["is_virtual"], False)

    def test_ic_marker_outside_session_temp_is_unread_not_cached(self) -> None:
        from description_pipeline.sources.solidworks.native import _component_source_document

        outside = r"C:\Users\operator\Documents\IC~~\X.step.SLDPRT"
        result = _component_source_document(
            self.component(outside, imported=r"C:\Users\operator\Desktop\X.stp"), self.source_root
        )
        provenance = result["source_provenance"]
        self.assertEqual(provenance["state"], "unread")
        self.assertEqual(provenance["materialization_path"], outside)
        self.assertNotIn("kind", provenance)

    def test_virtual_flag_alone_requires_a_materialization_path(self) -> None:
        from description_pipeline.sources.solidworks.native import _component_source_document

        result = _component_source_document(
            self.component(r"C:\elsewhere\X.SLDPRT", imported=r"D:\author\X.stp", virtual=True),
            self.source_root,
        )
        # A virtual reading with an out-of-temp path is not a session
        # materialization; it stays unread with the readings recorded verbatim.
        provenance = result["source_provenance"]
        self.assertEqual(provenance["state"], "unread")
        self.assertIs(provenance["is_virtual"], True)

    def test_in_root_document_requires_an_explicit_false_virtual_reading(self) -> None:
        from description_pipeline.sources.solidworks.native import _component_source_document

        part = self.source_root / "part.SLDPRT"
        part.write_bytes(b"delivered")
        result = _component_source_document(
            self.component(part, imported=None, virtual=None),
            self.source_root,
        )
        self.assertIsNone(result["document"])
        self.assertEqual(result["source_provenance"]["state"], "unread")
        self.assertIn("explicit False IsVirtual", result["source_provenance"]["error"])

    def test_in_stage_author_source_does_not_override_the_cached_class(self) -> None:
        from description_pipeline.sources.solidworks.native import _component_source_document

        (self.source_root / "imported_part.step").write_bytes(b"np-bytes")
        result = _component_source_document(
            self.component(self.materialization, imported=str(self.source_root / "imported_part.step")),
            self.source_root,
        )
        provenance = result["source_provenance"]
        # The engine-resolved document is a structural session materialization,
        # so the class stays cached on both sides; the in-stage author source is
        # validated metadata (retained as a selection-level bound edge).
        self.assertEqual(provenance["state"], "cached_materialization")
        self.assertIsNone(result["document"])
        self.assertTrue(provenance["author_imported_in_source_root"])
        self.assertEqual(provenance["author_imported_relative"], "imported_part.step")

    def test_embedded_virtual_component_requires_vc_and_true_virtual_state(self) -> None:
        from description_pipeline.sources.solidworks.native import _component_source_document

        virtual_document = self.tmp / "swx21040" / "VC~~" / "waist" / "Copy of 复件 (2) 零件10^waist.SLDPRT"
        virtual_document.parent.mkdir(parents=True)
        virtual_document.write_bytes(b"virtual")
        result = _component_source_document(
            self.component(virtual_document, imported=None, virtual=True),
            self.source_root,
        )
        self.assertIsNone(result["document"])
        provenance = result["source_provenance"]
        self.assertEqual(provenance["state"], "embedded_virtual_component")
        self.assertEqual(provenance["kind"], "native_embedded_virtual_component")
        self.assertEqual(provenance["session_document_path"], str(virtual_document))
        self.assertEqual(provenance["session_temp_root"], str(virtual_document.parent.parent.parent))
        self.assertIs(provenance["is_virtual"], True)
        self.assertIsNone(provenance["author_imported_path"])
        self.assertNotIn("error", provenance)

        cases = {
            "false_virtual": self.component(virtual_document, imported=None, virtual=False),
            "unknown_virtual": self.component(virtual_document, imported=None, virtual=None),
            "import_path_present": self.component(
                virtual_document,
                imported=r"D:\author\Copy of 复件 (2) 零件10^waist.step",
                virtual=True,
            ),
            "import_path_non_text": self.component(
                virtual_document,
                imported=123,
                virtual=True,
            ),
            "in_stage_path_with_virtual": self.component(
                self.source_root / "part.SLDPRT",
                imported=None,
                virtual=True,
            ),
        }
        unreadable_import = self.component(virtual_document, imported=None, virtual=True)

        def fail_import_read():
            raise RuntimeError("GetImportedPath failed")

        unreadable_import.GetImportedPath = fail_import_read
        cases["unreadable_import_path"] = unreadable_import
        wrong_root = self.tmp / "not-a-session" / "VC~~" / "waist" / "part.SLDPRT"
        wrong_root.parent.mkdir(parents=True)
        wrong_root.write_bytes(b"virtual")
        cases["wrong_session_root"] = self.component(wrong_root, imported=None, virtual=True)
        mixed_markers = (
            self.tmp
            / "swx21042"
            / "VC~~"
            / "IC~~"
            / "waist"
            / "part.SLDPRT"
        )
        mixed_markers.parent.mkdir(parents=True)
        mixed_markers.write_bytes(b"virtual")
        cases["mixed_session_markers"] = self.component(
            mixed_markers, imported=None, virtual=True
        )
        for label, component in cases.items():
            with self.subTest(case=label):
                rejected = _component_source_document(component, self.source_root)
                self.assertEqual(rejected["source_provenance"]["state"], "unread")
                self.assertIn("error", rejected["source_provenance"])

    def test_ic_and_vc_session_classes_do_not_cross(self) -> None:
        from description_pipeline.sources.solidworks.native import _component_source_document

        ic_but_virtual = _component_source_document(
            self.component(self.materialization, imported=None, virtual=True),
            self.source_root,
        )
        self.assertEqual(ic_but_virtual["source_provenance"]["state"], "unread")
        self.assertIn("cannot be conflated", ic_but_virtual["source_provenance"]["error"])

        virtual_document = self.tmp / "swx21041" / "VC~~" / "waist" / "part.SLDPRT"
        virtual_document.parent.mkdir(parents=True)
        virtual_document.write_bytes(b"virtual")
        vc_but_import = _component_source_document(
            self.component(virtual_document, imported=r"D:\author\part.step", virtual=False),
            self.source_root,
        )
        self.assertEqual(vc_but_import["source_provenance"]["state"], "unread")
        self.assertIn("explicit True IsVirtual", vc_but_import["source_provenance"]["error"])


class MaterializationSessionOwnershipTests(unittest.TestCase):
    """Cached import capture routes the materialization to its owning session."""

    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="sw-materialization-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.materialization = self.tmp / "swx4242" / "IC~~" / "imported_part.step.SLDPRT"
        self.materialization.parent.mkdir(parents=True)
        self.materialization.write_bytes(b"materialized")

    class Session:
        def __init__(self, app=None, closed=False):
            self.app = app
            self.closed = closed

        def current_application(self):
            return self.app

        def identity(self):
            return {
                "pid": 4242,
                "executable": "fake",
                "ownership": "windows_job",
                "state": "closed" if self.closed else "active",
            }

        def close(self):
            self.closed = True

        def terminate(self):
            self.closed = True

    @staticmethod
    def _doc(path):
        return types.SimpleNamespace(
            IsOpenedReadOnly=lambda: True,
            GetPathName=lambda: str(path),
        )

    def test_collected_copy_owns_the_materialization_without_creating_a_source(self) -> None:
        from description_pipeline.sources.solidworks import native as N

        app = types.SimpleNamespace(GetOpenDocumentByName=lambda path: self._doc(path))
        backend = N.SolidWorksBackend()
        backend._sessions = {"source": self.Session(closed=True), "copy": self.Session(app=app)}
        self.assertEqual(backend._role_for_path(self.materialization), "copy")
        key = backend._record_source_document(
            self.materialization, "Default", session_document=True
        )
        self.assertEqual(key[0], "copy")
        doc = backend._document_by_path(str(self.materialization))
        self.assertEqual(doc.GetPathName(), str(self.materialization))
        # The retired source session is never revived and no new session exists.
        self.assertEqual(sorted(backend._sessions), ["copy", "source"])
        self.assertTrue(backend._sessions["source"].closed)

    def test_walk_role_wins_when_explicitly_contextual(self) -> None:
        from description_pipeline.sources.solidworks import native as N

        backend = N.SolidWorksBackend()
        backend._sessions = {"source": self.Session(app=object()), "copy": self.Session(app=object())}
        backend._local.role = "copy"
        self.assertEqual(backend._role_for_path(self.materialization), "copy")

    def test_session_documents_must_share_one_controlled_temp_root(self) -> None:
        from description_pipeline.sources.solidworks import native as N
        from description_pipeline.sources.solidworks.errors import CadError

        other = self.tmp / "swx4243" / "VC~~" / "other" / "part.SLDPRT"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"other session")
        backend = N.SolidWorksBackend()
        backend._sessions = {"copy": self.Session(app=object())}
        backend._local.role = "copy"
        backend._record_source_document(self.materialization, "Default", session_document=True)
        with self.assertRaises(CadError) as caught:
            backend._record_source_document(other, "Default", session_document=True)
        self.assertEqual(caught.exception.code, "cad_materialization_session_mismatch")

    def test_session_document_registration_is_bound_to_its_role_and_session(self) -> None:
        from description_pipeline.sources.solidworks import native as N
        from description_pipeline.sources.solidworks.errors import CadError

        backend = N.SolidWorksBackend()
        backend._sessions = {
            "source": self.Session(app=object()),
            "copy": self.Session(app=object()),
        }
        backend._local.role = "source"
        backend._record_source_document(
            self.materialization, "Default", session_document=True
        )
        backend._sealed_documents = set()
        backend._local.role = "copy"
        with self.assertRaises(CadError) as caught:
            backend._require_sealed(self.materialization)
        self.assertEqual(caught.exception.code, "document_not_sealed")
        backend._local.role = "source"
        backend._require_sealed(self.materialization)
        backend.release()
        self.assertEqual(backend._session_document_paths, {})
        self.assertEqual(backend._session_artifact_roots, {})
        self.assertEqual(backend._session_artifact_root_owners, {})

    def test_session_document_cannot_be_rebound_or_share_a_temp_root_across_sessions(self) -> None:
        from description_pipeline.sources.solidworks import native as N
        from description_pipeline.sources.solidworks.errors import CadError

        backend = N.SolidWorksBackend()
        backend._sessions = {
            "source": self.Session(app=object()),
            "copy": self.Session(app=object()),
        }
        backend._local.role = "source"
        backend._record_source_document(
            self.materialization, "Default", session_document=True
        )
        backend._local.role = "copy"
        with self.assertRaises(CadError) as caught:
            backend._record_source_document(
                self.materialization, "Default", session_document=True
            )
        self.assertEqual(caught.exception.code, "cad_materialization_session_mismatch")

        other_in_same_root = self.materialization.with_name("other.step.SLDPRT")
        with self.assertRaises(CadError) as caught:
            backend._record_source_document(
                other_in_same_root, "Default", session_document=True
            )
        self.assertEqual(caught.exception.code, "cad_materialization_session_mismatch")

    def test_ambiguity_and_missing_owner_refuse_precisely(self) -> None:
        from description_pipeline.sources.solidworks import native as N
        from description_pipeline.sources.solidworks.errors import CadError

        backend = N.SolidWorksBackend()
        backend._sessions = {"source": self.Session(app=object()), "copy": self.Session(app=object())}
        with self.assertRaises(CadError) as caught:
            backend._role_for_path(self.materialization)
        self.assertEqual(caught.exception.code, "cad_materialization_session_ambiguous")

        backend2 = N.SolidWorksBackend()
        backend2._sessions = {"source": self.Session(closed=True)}
        with self.assertRaises(CadError) as caught:
            backend2._role_for_path(self.materialization)
        self.assertEqual(caught.exception.code, "cad_materialization_session_ambiguous")

        backend3 = N.SolidWorksBackend()
        backend3._role_for_path = lambda path: "ghost"  # type: ignore[method-assign]
        with self.assertRaises(CadError) as caught:
            backend3._record_source_document(
                self.materialization, "Default", session_document=True
            )
        self.assertEqual(caught.exception.code, "cad_materialization_session_unavailable")


class _LinkMassProperty:
    """Fake IMassProperty2 for the direct link group reader."""

    _next_sequence = 0

    def __init__(self, state, reads, getter_calls=None):
        type(self)._next_sequence += 1
        self._sequence = type(self)._next_sequence
        self.UseSystemUnits = None
        self.IncludeHiddenBodiesOrComponents = None
        self.SelectedItems = None
        self.Mass = 0.0
        self.Volume = 0.001
        self.Density = 1.0
        self.CenterOfMass = [0.0, 0.0, 0.0]
        self._state = state
        self._reads = reads
        self._overrides = {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}
        self._tensor = [[1e-6, 0.0, 0.0], [0.0, 1e-6, 0.0], [0.0, 0.0, 1e-6]]
        self._origin = None
        self._origin_error = None
        self._getter_calls = getter_calls

    def Recalculate(self):
        names = tuple(component.Name2 for component in self.SelectedItems.value)
        self._state.append(names)
        seen = sum(1 for entry in self._state if entry == names)
        payload = self._reads[names][seen - 1]
        self.Mass = float(payload["mass_kg"])
        self.CenterOfMass = [float(value) for value in payload["com_m"]]
        self._tensor = [[float(value) for value in row] for row in payload["inertia_tensor_about_com"]]
        self._overrides = dict(payload.get("overrides") or self._overrides)
        self._origin = copy.deepcopy(payload.get("origin"))
        self._origin_error = payload.get("origin_error")

    def GetOverrideOptions(self):
        return types.SimpleNamespace(**self._overrides)

    def GetMomentOfInertia(self, kind):
        if self._getter_calls is not None:
            self._getter_calls.append((self._sequence, kind))
        if kind == 0:
            return [value for row in self._tensor for value in row]
        if self._origin_error is not None:
            raise RuntimeError(str(self._origin_error))
        if self._origin is None:
            raise RuntimeError("about-origin read unavailable")
        return copy.deepcopy(self._origin)


class LinkGroupMassPropertiesTests(unittest.TestCase):
    """The direct one-shot reader: exact paths, two fresh reads, fail closed."""

    def _read(
        self,
        mass,
        com=(0.1, 0.2, 0.3),
        tensor=None,
        overrides=None,
        *,
        origin=None,
        origin_error=None,
    ):
        row = {
            "mass_kg": mass,
            "com_m": list(com),
            "inertia_tensor_about_com": tensor or [[1e-6, 0.0, 0.0], [0.0, 2e-6, 0.0], [0.0, 0.0, 3e-6]],
            "overrides": overrides
            or {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False},
        }
        if origin is not None:
            row["origin"] = list(origin)
        if origin_error is not None:
            row["origin_error"] = str(origin_error)
        return row

    @contextmanager
    def _backend(self, reads, *, doc_type=2, extra_components=()):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "part.SLDPRT")
            nested = _Component("sub-1", part, children=[_Component("sub-1/b-1", part)])
            components = [nested, _Component("c-1", part), *extra_components]
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, doc_type=doc_type, children=components)
            state: list = []
            getter_calls: list = []
            doc.Extension.CreateMassProperty2 = lambda: _LinkMassProperty(
                state,
                reads,
                getter_calls,
            )
            backend = SolidWorksBackend(
                session_factory=lambda: _Session(_App({assembly: doc}))
            )
            backend._link_mass_getter_calls = getter_calls
            yield backend, assembly, state

    def test_reads_each_body_twice_with_the_exact_sorted_selection(self):
        origin = [0.260001, -0.04, -0.06, -0.04, 0.200002, -0.12, -0.06, -0.12, 0.100003]
        reads = {
            ("c-1", "sub-1"): [
                self._read(2.0, origin=origin),
                self._read(2.0, origin=origin),
            ]
        }
        with self._backend(reads) as (backend, assembly, state):
            payload = backend.link_group_mass_properties(str(assembly), {"body": ["sub-1", "c-1"]})
        self.assertEqual(payload["used_api"], "IMassProperty2 (SelectedItems = component set)")
        self.assertEqual(payload["selection_rule"], "top_most_providers+uncovered_leaves")
        self.assertEqual(payload["assembly"], str(assembly))
        row = payload["links"]["body"]
        self.assertEqual(row["selection"], ["c-1", "sub-1"])
        self.assertEqual(len(row["reads"]), 2)
        self.assertEqual(row["reads"][0]["mass_kg"], 2.0)
        self.assertEqual(row["reads"][0]["volume_m3"], 0.001)
        self.assertEqual(row["reads"][0]["density_kg_m3"], 1.0)
        self.assertEqual(
            row["reads"][0]["overrides"],
            {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False},
        )
        self.assertEqual(state, [("c-1", "sub-1"), ("c-1", "sub-1")])
        projected = _project_link_group_mass(payload)["links"]["body"]
        self.assertEqual(
            projected["reproducibility"],
            {
                "mass_delta_kg": 0.0,
                "com_delta_norm_m": 0.0,
                "tensor_delta_max_abs": 0.0,
            },
        )

    def test_origin_getter_reaches_projected_seal_convention(self):
        from description_pipeline.sources.solidworks.inertia_convention import (
            STANDARD,
            normalize_capture_inertia,
        )

        origin = [
            0.260001,
            -0.04,
            -0.06,
            -0.04,
            0.200002,
            -0.12,
            -0.06,
            -0.12,
            0.100003,
        ]
        reads = {
            ("c-1", "sub-1"): [
                self._read(2.0, origin=origin),
                self._read(2.0, origin=origin),
            ]
        }
        with self._backend(reads) as (backend, assembly, state):
            payload = backend.link_group_mass_properties(
                str(assembly),
                {"body": ["c-1", "sub-1"]},
            )
        self.assertEqual(state, [("c-1", "sub-1"), ("c-1", "sub-1")])
        calls = backend._link_mass_getter_calls
        self.assertEqual([kind for _reader, kind in calls], [0, 1, 0, 1])
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertEqual(calls[2][0], calls[3][0])
        self.assertNotEqual(calls[0][0], calls[2][0])
        for read in payload["links"]["body"]["reads"]:
            self.assertEqual(read["inertia_values_about_origin_api"], origin)
            self.assertNotIn(
                "inertia_values_about_origin_api",
                read["read_errors"],
            )

        projected = _project_link_group_mass(payload)
        for read in projected["links"]["body"]["reads"]:
            self.assertEqual(read["inertia_values_about_origin_api"], origin)
        report = normalize_capture_inertia(
            {"link_group_mass_properties.json": projected}
        )
        self.assertEqual(report["detected"], STANDARD)
        self.assertFalse(report["capture_transform"]["applied"])

    def test_origin_getter_failure_is_preserved_and_refused_independently(self):
        from description_pipeline.sources.solidworks.inertia_convention import (
            normalize_capture_inertia,
        )

        reads = {
            ("c-1", "sub-1"): [
                self._read(2.0, origin_error="native origin failed"),
                self._read(2.0, origin_error="native origin failed"),
            ]
        }
        with self._backend(reads) as (backend, assembly, _state):
            payload = backend.link_group_mass_properties(
                str(assembly),
                {"body": ["c-1", "sub-1"]},
            )
        calls = backend._link_mass_getter_calls
        self.assertEqual([kind for _reader, kind in calls], [0, 1, 0, 1])
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertEqual(calls[2][0], calls[3][0])
        self.assertNotEqual(calls[0][0], calls[2][0])
        for read in payload["links"]["body"]["reads"]:
            self.assertEqual(read["mass_kg"], 2.0)
            self.assertEqual(read["volume_m3"], 0.001)
            self.assertEqual(read["density_kg_m3"], 1.0)
            self.assertEqual(
                read["inertia_values_raw"],
                [1e-6, 0.0, 0.0, 0.0, 2e-6, 0.0, 0.0, 0.0, 3e-6],
            )
            self.assertEqual(
                read["inertia_values_about_origin_api"]["code"],
                "RuntimeError",
            )
            self.assertEqual(
                read["inertia_values_about_origin_api"]["message"],
                "native origin failed",
            )
            self.assertEqual(
                read["read_errors"]["inertia_values_about_origin_api"],
                read["inertia_values_about_origin_api"],
            )

        projected = _project_link_group_mass(payload)
        for read in projected["links"]["body"]["reads"]:
            self.assertEqual(
                read["inertia_values_about_origin_api"]["message"],
                "native origin failed",
            )
        with self.assertRaises(BridgeError) as caught:
            normalize_capture_inertia(
                {"link_group_mass_properties.json": projected}
            )
        self.assertEqual(
            caught.exception.code,
            "inertia_convention_insufficient_samples",
        )
        self.assertEqual(caught.exception.detail["total_samples"], 0)

    def test_reproducibility_records_the_repeat_deltas(self):
        first = self._read(
            2.0,
            (0.1, 0.2, 0.3),
            origin=[1.26e-6, -0.04, -0.06, -0.04, 2.2e-6, -0.12, -0.06, -0.12, 3.1e-6],
        )
        second = self._read(
            2.001,
            (0.1, 0.2, 0.31),
            [[1.1e-6, 0.0, 0.0], [0.0, 2e-6, 0.0], [0.0, 0.0, 3e-6]],
            origin=[1.36e-6, -0.04, -0.06, -0.04, 2.2e-6, -0.12, -0.06, -0.12, 3.1e-6],
        )
        with self._backend({("c-1", "sub-1"): [first, second]}) as (backend, assembly, _state):
            payload = backend.link_group_mass_properties(str(assembly), {"body": ["c-1", "sub-1"]})
        row = _project_link_group_mass(payload)["links"]["body"]
        self.assertAlmostEqual(row["reproducibility"]["mass_delta_kg"], 0.001, places=12)
        self.assertAlmostEqual(row["reproducibility"]["com_delta_norm_m"], 0.01, places=12)
        self.assertAlmostEqual(row["reproducibility"]["tensor_delta_max_abs"], 1e-7, places=15)

    def test_missing_path_fails_closed_before_any_read(self):
        with self._backend({}) as (backend, assembly, state):
            with self.assertRaises(CadError) as caught:
                backend.link_group_mass_properties(str(assembly), {"body": ["ghost-1"]})
            self.assertEqual(caught.exception.code, "cad_missing_component")
            self.assertEqual(caught.exception.detail["missing"], ["ghost-1"])
            self.assertEqual(caught.exception.detail["link"], "body")
            self.assertEqual(state, [])

    def test_empty_and_duplicate_selections_fail(self):
        with self._backend({}) as (backend, assembly, _state):
            with self.assertRaises(CadError) as caught:
                backend.link_group_mass_properties(str(assembly), {"body": []})
            self.assertEqual(caught.exception.code, "cad_empty_selection")
            with self.assertRaises(CadError) as caught:
                backend.link_group_mass_properties(str(assembly), {})
            self.assertEqual(caught.exception.code, "cad_empty_selection")
            with self.assertRaises(CadError) as caught:
                backend.link_group_mass_properties(str(assembly), {"body": ["c-1", "c-1"]})
            self.assertEqual(caught.exception.code, "cad_duplicate_selection")

    def test_single_instance_object_override_is_recorded_and_repeats(self):
        overrides = {"OverrideMass": True, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}
        origin = [0.260001, -0.04, -0.06, -0.04, 0.200002, -0.12, -0.06, -0.12, 0.100003]
        reads = {
            ("c-1",): [
                self._read(2.0, overrides=overrides, origin=origin),
                self._read(2.0, overrides=overrides, origin=origin),
            ]
        }
        with self._backend(reads) as (backend, assembly, state):
            payload = backend.link_group_mass_properties(str(assembly), {"body": ["c-1"]})
        self.assertEqual(state, [("c-1",), ("c-1",)])
        row = payload["links"]["body"]
        self.assertEqual(row["selection"], ["c-1"])
        self.assertEqual(len(row["reads"]), 2)
        for read in row["reads"]:
            self.assertEqual(read["overrides"], overrides)
        self.assertEqual(
            _project_link_group_mass(payload)["links"]["body"][
                "reproducibility"
            ]["com_delta_norm_m"],
            0.0,
        )

    def test_multi_instance_object_override_is_recorded_verbatim(self):
        overrides = {"OverrideMass": True, "OverrideCenterOfMass": True, "OverrideMomentsOfInertia": False}
        reads = {("c-1", "sub-1"): [self._read(2.0, overrides=overrides), self._read(2.0, overrides=overrides)]}
        with self._backend(reads) as (backend, assembly, _state):
            payload = backend.link_group_mass_properties(str(assembly), {"body": ["c-1", "sub-1"]})
        row = payload["links"]["body"]
        self.assertEqual([read["overrides"] for read in row["reads"]], [overrides, overrides])

    def test_whole_assembly_override_refusal_is_unchanged(self):
        overrides = {"OverrideMass": True, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}
        reads = {("robot",): [self._read(2.0, overrides=overrides), self._read(2.0, overrides=overrides)]}
        with self._backend(reads) as (backend, assembly, _state):
            raw = _raw_mass_fact(
                backend.assembly_mass_properties(str(assembly))
            )
        with self.assertRaises(CadError) as caught:
            _project_mass_reading(raw)
        self.assertEqual(caught.exception.code, "cad_mass_override")
        self.assertTrue(caught.exception.detail["OverrideMass"])

    def test_a_part_document_is_refused(self):
        with self._backend({}, doc_type=1) as (backend, assembly, _state):
            with self.assertRaises(CadError) as caught:
                backend.link_group_mass_properties(str(assembly), {"body": ["c-1"]})
            self.assertEqual(caught.exception.code, "cad_not_assembly")


if __name__ == "__main__":
    unittest.main()
