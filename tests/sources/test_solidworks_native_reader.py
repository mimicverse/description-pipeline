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

import hashlib
import os
import struct
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

from . import support

from description_pipeline.geometry.stl import read as read_stl
from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.sources.solidworks.freeze import _raw_scene_payload
from description_pipeline.sources.solidworks.isolation import CadSession
from description_pipeline.sources.solidworks.jsonio import read_json, write_json
from description_pipeline.sources.solidworks.native import (
    SolidWorksBackend,
    normalize_document_path,
    _read_only_document,
    _temporary_configuration,
)

#: SolidWorks ``MathTransform.ArrayData`` order for an identity occurrence.
SW_IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
PLANE_XY = {"normal": [0.0, 0.0, 1.0], "point": [0.0, 0.0, 0.0]}
CYLINDER_Z = {"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.02}


def _sw_translation(x, y, z):
    """SolidWorks ``ArrayData`` for an identity rotation plus a translation."""

    return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, x, y, z, 1.0, 0.0, 0.0, 0.0)


def _raiser(error: BaseException):
    def call(*_args, **_kwargs):
        raise error

    return call


class _Plane:
    IsCylinder = False
    IsPlane = True

    def __init__(self, normal, point):
        self.PlaneParams = tuple(normal) + tuple(point)


class _Cylinder:
    IsCylinder = True
    IsPlane = False

    def __init__(self, point, direction, radius):
        self.CylinderParams = tuple(point) + tuple(direction) + (radius,)


class _Circle:
    IsCircle = True

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
        self.IsFixed = False
        # The read-only root-signature probe on the frozen production assembly
        # (2026-10-10) shows IsRoot as a native boolean: True on the root
        # reference, False on part references.  Mocks default to the part case.
        self.IsRoot = is_root
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
        needs_rebuild2=0,
    ):
        self._scopes = scopes
        self._coordinate_systems = dict(coordinate_systems or {})
        self._manager = manager
        self._scope_managers = dict(scope_managers or {})
        self.NeedsRebuild2 = needs_rebuild2

    def CustomPropertyManager(self, scope):
        if scope in self._scope_managers:
            return self._scope_managers[scope]
        if self._manager is not _AUTO:
            return self._manager
        return _PropertyManager(self._scopes.get(scope, {}))

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

    def Rebuild(self, options):
        self.NeedsRebuild2 = 0
        return True


class _ConfigurationManager:
    """Reflects the document's *currently shown* configuration at each read."""

    def __init__(self, doc):
        self._doc = doc

    @property
    def ActiveConfiguration(self):
        name = self._doc.active_configuration
        root = types.SimpleNamespace(
            Name2=os.path.splitext(os.path.basename(self._doc._path))[0],
            GetPathName=lambda: self._doc._path,
            GetChildren=lambda: list(self._doc.children_for(name)),
        )
        return types.SimpleNamespace(
            Name=name,
            NeedsRebuild=False,
            GetRootComponent3=lambda _visible: root,
        )


class _Doc:
    """``IModelDoc2``-like assembly/part document, read-only and pre-opened."""

    IsOpenedReadOnly = True

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
        needs_rebuild2=0,
        custom_property_manager=_AUTO,
        scope_managers=None,
    ):
        self._path = str(path)
        self._children = list(children)
        self._configuration_children = {name: list(items) for name, items in (configuration_children or {}).items()}
        self._configuration_features = dict(configuration_features or {})
        self._doc_type = doc_type
        self.active_configuration = configuration
        self._show_success = show_success
        self._show_effect = show_effect
        scopes = {"": dict(properties or {})}
        for name, values in (configuration_properties or {}).items():
            scopes[name] = dict(values)
        self.Extension = _Extension(
            scopes,
            coordinate_systems,
            custom_property_manager,
            scope_managers,
            needs_rebuild2,
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

    def ForceRebuild3(self, _force):
        raise AssertionError("capture rebuild must use IModelDocExtension.Rebuild")

    def GetType(self):
        return self._doc_type

    def FirstFeature(self):
        feature = self._configuration_features.get(self.active_configuration, self._first_feature)
        return feature() if callable(feature) else feature

    def GetComponents(self, _ignored):
        return list(self._children)


class _App:
    StartupProcessCompleted = True
    """Minimal ``ISldWorks`` surface; documents are pre-opened read-only."""

    RevisionNumber = "2026-portable-mock"
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
        return list(self._docs.values())

    def OpenDoc6(self, path, _kind, _options, _configuration, errors, warnings):
        key = os.path.normcase(os.path.abspath(str(path)))
        errors.value = self._open_errors.get(key, 0)
        warnings.value = 0
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
        return {"reader": "portable-mock"}


class _CaptureBackend(SolidWorksBackend):
    """Real ``collect_scene`` frame loop; only heavy CAD reads are stubbed."""

    def _rebuild_capture_copy(self, doc, path):
        return doc, {
            "rebuilt": True,
            "path": str(path),
            "document": str(path),
            "configuration": str(doc.active_configuration),
        }

    def _record_save_flag(self, doc, path):
        self.save_flags[str(path)] = False

    def _body_count(self, holder, body_type, component):
        return 1

    def _mass_properties_document(self, doc, require_material=True):
        return {
            "mass_kg": 1.0,
            "volume_m3": 0.001,
            "material": None,
            "reference": {"used_api": "portable-capture-mock", "configuration": None},
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
    return types.SimpleNamespace(
        GetTessTriangles=read_triangles,
        GetTessTriangleCount=lambda: len(fixture_values) // 9,
        GetArea=lambda: 1.0,
        GetEdges=list,
        GetBody=lambda: None,
        _persist_reference=f"fixture-face-{key}".encode(),
        _fixture_values=fixture_values,
        _fixture_readable=fixture_readable,
    )


def _mesh_body(faces, key: str):
    faces = list(faces)
    for face in faces:
        if not hasattr(face, "GetTessTriangles"):
            face._fixture_values = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
            face._fixture_readable = True
            face.GetTessTriangles = lambda _quality, values=face._fixture_values: list(values)
    triangles = [
        tuple(
            tuple(values[offset : offset + 3])
            for offset in range(triangle_start, triangle_start + 9, 3)
        )
        for face in faces
        for values in (list(face._fixture_values),)
        for triangle_start in range(0, len(values), 9)
    ]
    spans = []
    triangle_start = 0
    body_reference = f"fixture-body-{key}".encode()
    for face_index, face in enumerate(faces):
        values = list(face._fixture_values)
        triangle_count = len(values) // 9
        record_points = [
            point
            for triangle in triangles[
                triangle_start : triangle_start + triangle_count
            ]
            for point in triangle
        ]
        spans.append(
            {
                "body_type": 0,
                "body_index": 0,
                "body_name": f"fixture-body-{key}",
                "body_reference_sha256": hashlib.sha256(body_reference).hexdigest(),
                "face_index": face_index,
                "face_reference_sha256": hashlib.sha256(face._persist_reference).hexdigest(),
                "face_owner_id": face_index + 1,
                "edge_references": [],
                "triangle_start": triangle_start,
                "triangle_count": triangle_count,
                "triangle_records_sha256": hashlib.sha256(
                    b"".join(
                        struct.pack("<3f", *point) for point in record_points
                    )
                ).hexdigest(),
            }
        )
        triangle_start += triangle_count
    tessellation = support.native_tessellation_fixture(
        spans,
        [],
        triangle_vertices=triangles,
    )
    cursor = 0
    for span, face in zip(spans, faces, strict=True):
        values = [
            coordinate
            for triangle in tessellation.triangle_vertices[
                cursor : cursor + span["triangle_count"]
            ]
            for point in triangle
            for coordinate in point
        ]
        cursor += span["triangle_count"]
        if face._fixture_readable:
            face.GetTessTriangles = lambda _quality, values=values: list(values)
    body_graph = tessellation["itessellation"]["bodies"][0]
    facets_by_face = {
        face_index: row["facet_ids"]
        for face_index, row in enumerate(body_graph["faces"])
    }
    facets_by_reference = {
        hashlib.sha256(face._persist_reference).hexdigest(): facets_by_face[face_index]
        for face_index, face in enumerate(faces)
    }
    fins_by_facet = {
        row["facet_id"]: row["fin_ids"] for row in body_graph["facets"]
    }
    fin_vertices = {
        row["fin_id"]: row["vertex_ids"] for row in body_graph["fins"]
    }
    vertex_points = {
        row["vertex_id"]: row["point_m"]
        for row in tessellation["itessellation"]["vertices"]
    }
    face_by_id = {id(face): index for index, face in enumerate(faces)}

    class _MeshTessellation:
        NeedFaceFacetMap = True
        NeedEdgeFinMap = True
        NeedVertexParams = True

        def Tessellate(self):  # noqa: N802 - SolidWorks API name
            return True

        def GetFaceFacets(self, face):  # noqa: N802 - SolidWorks API name
            values = facets_by_face.get(face_by_id.get(id(face)))
            if values is None:
                values = facets_by_reference[
                    hashlib.sha256(face._persist_reference).hexdigest()
                ]
            return list(values)

        def GetFacetFins(self, facet_id):  # noqa: N802 - SolidWorks API name
            return list(fins_by_facet[facet_id])

        def GetEdgeFins(self, _edge):  # noqa: N802 - SolidWorks API name
            return []

        def GetFinVertices(self, fin_id):  # noqa: N802 - SolidWorks API name
            return list(fin_vertices[fin_id])

        def GetVertexPoint(self, vertex_id):  # noqa: N802 - SolidWorks API name
            return list(vertex_points[vertex_id])

        def GetFacetCount(self):  # noqa: N802 - SolidWorks API name
            return len(fins_by_facet)

        def GetFinCount(self):  # noqa: N802 - SolidWorks API name
            return len(fin_vertices)

        def GetVertexCount(self):  # noqa: N802 - SolidWorks API name
            return len(vertex_points)

    body = types.SimpleNamespace(
        Name=f"fixture-body-{key}",
        GetFaces=lambda: list(faces),
        GetEdges=list,
        GetTessellation=lambda _face_list: _MeshTessellation(),
        _persist_reference=body_reference,
    )
    for face in faces:
        face.GetBody = lambda body=body: body
    return body


@contextmanager
def _com_stubs():
    """Minimal pythoncom/win32com surface used by the by-ref property reads."""

    pythoncom = types.ModuleType("pythoncom")
    pythoncom.VT_BYREF = 0x4000
    pythoncom.VT_BSTR = 8
    pythoncom.VT_BOOL = 11
    pythoncom.VT_I4 = 3
    pythoncom.VT_DISPATCH = 9
    pythoncom.VT_ARRAY = 0x2000
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
    win32com.client = client
    added = {"pythoncom": pythoncom, "win32com": win32com, "win32com.client": client}
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
            grounded.IsFixed = True
            grounded.Solving = 0
            grounded.GetConstrainedStatus = lambda: 3
            free = _Component("wrench-1", wrench)
            free.IsFixed = False
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
    def test_capture_excludes_embedded_virtual_session_documents_from_source_files(self) -> None:
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
            scene = backend.collect_scene(str(assembly), [])

            self.assertIn(str(virtual_path), {component.path for component in scene.components})
            self.assertNotIn(str(virtual_path), backend.source_files)
            self.assertNotIn(str(virtual_path), backend.verify_sources_unchanged())

    def test_copy_inspection_releases_occurrences_before_read_only_mutation(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            borrowed = []
            events = []

            class PartDocument(_Doc):
                read_only = False

                def IsOpenedReadOnly(self):
                    return self.read_only

                def SetReadOnlyState(self, value):
                    if any(ref() is not None for ref in borrowed):
                        raise RuntimeError("borrowed occurrences survived a read-only boundary")
                    events.append(("read_only", self._path))
                    self.read_only = value
                    return True

            paths = [_write(root, name + ".SLDPRT") for name in ("arm1", "arm2")]
            documents = {path: PartDocument(path) for path in paths}
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2)
            documents[assembly] = main

            def occurrences(_configuration):
                result = []
                for path in paths:
                    component = _Component(path.stem, path, doc=documents[path])
                    borrowed.append(weakref.ref(component))
                    events.append(("occurrence", path.stem))
                    result.append(component)
                return result

            main.children_for = occurrences
            app = _App(documents)
            app.OpenDoc6 = lambda *_args: main
            backend = SolidWorksBackend(session_factory=lambda: _Session(app))
            result = backend.inspect_copy(str(assembly))

            self.assertEqual({item["instance"] for item in result["instances"]}, {"arm1", "arm2"})
            self.assertEqual(result["configuration"], "Default")
            self.assertEqual(result["unresolved"], [])
            self.assertTrue(all(doc.read_only for path, doc in documents.items() if path != assembly))
            self.assertEqual([kind for kind, _ in events], ["occurrence", "occurrence", "read_only", "read_only"])

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
            scene = backend.collect_scene(str(assembly), [])

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
            return backend.collect_scene(str(assembly), requested)

    def _configured_occurrences(self, root, *, nested=False):
        class ConfiguredDocument(_Doc):
            def GetTessTriangles(self, _quality):
                raise AssertionError("occurrence geometry must not read the shared document")

        class ConfiguredComponent(_Component):
            def GetBodies2(self, body_type):
                if body_type != 0:
                    return []
                triangles = {
                    "Short": [0, 0, 0, 1, 0, 0, 0, 1, 0],
                    "Long": [0, 0, 0, 2, 0, 0, 0, 2, 0, 2, 0, 0, 2, 2, 0, 0, 2, 0],
                }[self.ReferencedConfiguration]
                face = _mesh_face(
                    lambda _quality, values=triangles: values,
                    self.ReferencedConfiguration,
                )
                return [_mesh_body([face], self.ReferencedConfiguration)]

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
        scene = backend.collect_scene(str(assembly), [])
        return backend, scene, shared, short, part

    def test_repeated_configurations_keep_their_own_mass_mesh_and_restored_state(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, scene, shared, _, _ = self._configured_occurrences(root)
            self.assertEqual(scene.mass_properties["part-short"]["mass_kg"], 2.0)
            self.assertEqual(scene.mass_properties["part-long"]["mass_kg"], 3.0)
            for name, triangles, extent in (("part-long", 2, 2.0), ("part-short", 1, 1.0)):
                path = root / f"{name}.stl"
                result = backend.export_component_meshes({name: path})[name]
                self.assertEqual(result["triangles"], triangles)
                self.assertEqual(read_stl(path).high, (extent, extent, 0.0))
                self.assertEqual(shared.active_configuration, "Parked")
            backend.verify_sources_unchanged()

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
                return {
                    "mass_kg": {"Short": 2.0, "Long": 3.0}[doc.active_configuration],
                    "reference": {"used_api": "portable-config-mass", "configuration": doc.active_configuration},
                }

            def _body_count(self, holder, body_type, component):
                return len(holder.GetBodies2(body_type))

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
                    component.GetBodies2 = lambda body_type, count=count: [object()] * count if body_type == 0 else []
                    result.append(BorrowedOccurrence(component))
                return result

            main.children_for = current_occurrences
            backend = ConfiguredBackend(session_factory=lambda: _Session(_App({assembly: main, path: shared})))
            scene = backend.collect_scene(str(assembly), [])
            self.assertEqual(scene.mass_properties["part-short"]["mass_kg"], 2.0)
            self.assertEqual(scene.mass_properties["part-long"]["mass_kg"], 3.0)
            self.assertEqual(scene.notes["bodies:part-short"], {"solid": 1, "sheet": 0})
            self.assertEqual(scene.notes["bodies:part-long"], {"solid": 2, "sheet": 0})
            self.assertEqual(shared.active_configuration, "Parked")
            self.assertGreater(shared.generation, 0)
            self.assertEqual(expired_reads, [])
            backend.verify_sources_unchanged()

    def test_leaf_mass_failure_names_the_occurrence_and_recorded_body_counts(self):
        """A failed leaf mass read keeps its classification and gains context.

        Regression for the native7 ``cad_empty_model`` capture failure that
        carried ``detail: null``: the next capture must name the occurrence,
        its document, the referenced configuration and the already-recorded
        native solid/sheet counts without re-probing or skipping anything.
        """

        class FailingBackend(_CaptureBackend):
            def _body_count(self, holder, body_type, component):
                return len(holder.GetBodies2(body_type))

            def _mass_properties_document(self, doc, require_material=True):
                raise CadError("cad_empty_model", "part has no solid bodies", {"marker": "kept"})

            def assembly_group_mass_properties(self, path, names, *, allow_overrides=False):
                raise CadError("cad_group_unavailable", "no assembly context here")

        class SheetOnlyComponent(_Component):
            def GetBodies2(self, body_type):
                return [] if body_type == 0 else [object()]

        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "empty.SLDPRT")
            shared = _Doc(part, configuration="Default", configuration_children={"Default": []})
            component = SheetOnlyComponent("sub-1/empty-1", part, doc=shared, configuration="Default")
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[component])
            backend = FailingBackend(session_factory=lambda: _Session(_App({assembly: main, part: shared})))
            with self.assertRaises(CadError) as caught:
                backend.collect_scene(str(assembly), [])
        error = caught.exception
        self.assertEqual(error.code, "cad_empty_model")
        self.assertEqual(error.message, "part has no solid bodies")
        self.assertEqual(error.detail["marker"], "kept")
        self.assertEqual(error.detail["component"], "sub-1/empty-1")
        self.assertEqual(error.detail["document"], str(part))
        self.assertEqual(error.detail["referenced_configuration"], "Default")
        self.assertEqual(error.detail["bodies"], {"solid": 0, "sheet": 1})
        self.assertEqual(error.detail["phase"], "leaf_mass_read")
        self.assertEqual(error.detail["fallback_error"], "cad_group_unavailable")

    def test_sheet_only_fallback_records_the_effective_instance_row(self):
        class FallbackBackend(_CaptureBackend):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.group_calls = []

            def _body_count(self, holder, body_type, component):
                return len(holder.GetBodies2(body_type))

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
                    "mass": 0.026,
                    "com": [0.0, 0.0, 0.05],
                    "inertia": [[1e-6, 1e-8, 0.0], [1e-8, 1.2e-6, 0.0], [0.0, 0.0, 2e-6]],
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
            def GetBodies2(self, body_type):
                return [] if body_type == 0 else [object()] * 2350

        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "EasyEDA PCB Model~Nz70.SLDPRT")
            shared = _Doc(part, configuration="Default", configuration_children={"Default": []})
            component = SheetOnlyComponent("part-sheet", part, doc=shared, configuration="Default")
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[component])
            backend = FallbackBackend(session_factory=lambda: _Session(_App({assembly: main, part: shared})))
            scene = backend.collect_scene(str(assembly), [])
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
                return len(holder.GetBodies2(body_type))

            def _mass_properties_document(self, doc, require_material=True):
                raise CadError("cad_empty_model", "part has no solid bodies")

            def assembly_group_mass_properties(self, path, names, *, allow_overrides=False):
                self.group_calls += 1
                raise AssertionError("no fallback may be attempted without sheet bodies")

        class EmptyComponent(_Component):
            def GetBodies2(self, body_type):
                return []

        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part = _write(root, "empty.SLDPRT")
            shared = _Doc(part, configuration="Default", configuration_children={"Default": []})
            component = EmptyComponent("part-empty", part, doc=shared, configuration="Default")
            assembly = _write(root, "robot.SLDASM")
            main = _Doc(assembly, doc_type=2, children=[component])
            backend = NoBodyBackend(session_factory=lambda: _Session(_App({assembly: main, part: shared})))
            with self.assertRaises(CadError) as caught:
                backend.collect_scene(str(assembly), [])
        error = caught.exception
        self.assertEqual(error.code, "cad_empty_model")
        self.assertEqual(error.detail["component"], "part-empty")
        self.assertEqual(error.detail["bodies"], {"solid": 0, "sheet": 0})
        self.assertNotIn("fallback_error", error.detail)
        self.assertEqual(backend.group_calls, 0)

    def test_geometry_phase_does_not_release_and_reacquire_parents_between_occurrences(self):
        from contextlib import contextmanager

        from description_pipeline.sources.solidworks.freeze import _export_geometry

        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, _, shared, _, _ = self._configured_occurrences(root)
            original = backend._current_components
            acquisitions = []

            @contextmanager
            def live_phase(phase):
                if phase == "geometry":
                    if acquisitions:
                        raise CadError("cad_source_state_unreadable", "geometry owner released between occurrences")
                    acquisitions.append(phase)
                with original(phase) as current:
                    yield current

            backend._current_components = live_phase
            entries = _export_geometry(backend, {}, root / "geometry", ["part-long", "part-short"])
            self.assertEqual([entry["triangles"] for entry in entries], [2, 1])
            self.assertEqual(read_stl(root / entries[0]["path"]).high, (2.0, 2.0, 0.0))
            self.assertEqual(read_stl(root / entries[1]["path"]).high, (1.0, 1.0, 0.0))
            self.assertEqual(shared.active_configuration, "Parked")
            backend.verify_sources_unchanged()

    def test_geometry_batch_rejects_shared_destinations_before_writing(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, _, _, _, _ = self._configured_occurrences(root)
            target = root / "same.stl"
            with self.assertRaises(CadError) as caught:
                backend.export_component_meshes({"part-long": target, "part-short": root / "unused/../same.stl"})
            self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
            self.assertFalse(target.exists())

    def test_repeated_configuration_source_guard_still_rejects_real_drift(self):
        for drift in ("reference", "active", "file", "path", "suppression", "added", "removed", "renamed", "duplicate"):
            with self.subTest(drift=drift), TemporaryDirectory() as tmp, _com_stubs():
                backend, _, shared, short, part = self._configured_occurrences(Path(tmp))
                if drift == "reference":
                    short.ReferencedConfiguration = "Long"
                elif drift == "active":
                    self.assertTrue(shared.ShowConfiguration2("Long"))
                elif drift == "path":
                    short._path = str(_write(Path(tmp), "foreign.SLDPRT"))
                elif drift == "suppression":
                    short.IsSuppressed = True
                elif drift in {"added", "removed", "renamed", "duplicate"}:
                    main = backend._document_by_path(str(Path(tmp) / "robot.SLDASM"))
                    if drift == "added":
                        main._children.append(_Component("extra-1", part, doc=shared, configuration="Short"))
                    elif drift == "removed":
                        main._children.remove(short)
                    elif drift == "renamed":
                        short.Name2 = "renamed-1"
                    else:
                        main._children.append(short)
                else:
                    part.write_bytes(b"changed native source")
                with self.assertRaises(CadError) as caught:
                    backend.verify_sources_unchanged()
                self.assertEqual(caught.exception.code, "cad_source_changed")

    def test_mesh_and_guard_use_fresh_occurrence_after_scene_handle_expires(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, _, shared, stale, part = self._configured_occurrences(root)
            fresh = type(stale)(stale.Name2, part, doc=shared, configuration="Short")
            main = backend._document_by_path(str(root / "robot.SLDASM"))
            main._children[main._children.index(stale)] = fresh
            stale.GetBodies2 = _raiser(RuntimeError("RPC_S_UNKNOWN_IF: expired occurrence"))
            stale.GetPathName = _raiser(RuntimeError("expired occurrence"))
            path = root / "fresh.stl"
            backend.export_component_meshes({"part-short": path})["part-short"]
            self.assertEqual(read_stl(path).high, (1.0, 1.0, 0.0))
            backend.verify_sources_unchanged()

    def _borrowed_occurrence_geometry(self, backend, root):
        """Model vendor geometry that is valid only while its parent interfaces live."""
        path = str(root / "robot.SLDASM")
        main = backend._document_by_path(path)
        lifetimes = []

        class Borrowed:
            def __init__(self, inner, parents):
                self.inner = inner
                self.parents = parents

            def check(self):
                if not all(parent() is not None for parent in self.parents):
                    raise RuntimeError("borrowed geometry outlived its native parent interfaces")

            def __getattr__(self, name):
                self.check()
                return getattr(self.inner, name)

        class Body(Borrowed):
            def GetFaces(self):
                self.check()
                return [Borrowed(face, self.parents) for face in self.inner.GetFaces()]

        class Component(Borrowed):
            def GetBodies2(self, body_type):
                self.check()
                return [Body(body, self.parents) for body in self.inner.GetBodies2(body_type)]

        class Root:
            def __init__(self, manager, configuration):
                self.parents = (manager, weakref.ref(configuration), weakref.ref(self))
                lifetimes.append(self.parents)

            def GetChildren(self):
                return [Component(child, self.parents) for child in main._children]

        class Configuration:
            Name = main.active_configuration

            def __init__(self, manager):
                self.manager = weakref.ref(manager)

            def GetRootComponent3(self, _resolved):
                return Root(self.manager, self)

        class Manager:
            @property
            def ActiveConfiguration(self):
                return Configuration(self)

        class Document:
            @property
            def ConfigurationManager(self):
                return Manager()

            def __getattr__(self, name):
                return getattr(main, name)

        backend._app_obj()._docs[os.path.normcase(os.path.abspath(path))] = Document()
        return lifetimes

    def test_mesh_keeps_borrowed_parent_interfaces_alive_until_all_faces_are_read(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, _, _, _, _ = self._configured_occurrences(root)
            lifetimes = self._borrowed_occurrence_geometry(backend, root)
            path = root / "borrowed.stl"
            backend.export_component_meshes({"part-long": path})["part-long"]
            self.assertEqual(read_stl(path).triangles, 2)
            self.assertEqual(read_stl(path).high, (2.0, 2.0, 0.0))
            self.assertTrue(lifetimes)
            self.assertTrue(all(parent() is None for parents in lifetimes for parent in parents))

    def test_shaft_keeps_borrowed_parents_alive_through_the_surface_read(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, _, _, short, _ = self._configured_occurrences(root)
            face = types.SimpleNamespace(
                GetFeature=lambda: types.SimpleNamespace(Name="shaft"),
                GetSurface=lambda: _Cylinder((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 0.02),
            )
            face.GetEdges = list
            face._persist_reference = b"fixture-shaft-face"
            short.GetBodies2 = lambda body_type: [_mesh_body([face], "shaft")]
            lifetimes = self._borrowed_occurrence_geometry(backend, root)
            result = backend.capture_axis_reference({"component": "part-short", "feature_name": "shaft"})
            self.assertEqual(result["axis_direction"], [0.0, 0.0, 1.0])
            self.assertEqual(result["radius_m"], 0.02)
            self.assertTrue(all(parent() is None for parents in lifetimes for parent in parents))

    def test_display_read_failure_is_isolated_from_the_tessellation_graph(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, _, _, short, _ = self._configured_occurrences(root)
            good = _mesh_face(lambda _quality: [0, 0, 0, 1, 0, 0, 0, 1, 0], "good")
            failed = _mesh_face(_raiser(RuntimeError("RPC_S_UNKNOWN_IF")), "failed")
            short.GetBodies2 = lambda body_type: (
                [_mesh_body([good, failed], "failure")] if body_type == 0 else []
            )
            path = root / "failed.stl"
            with self.assertRaises(CadError) as caught:
                backend.export_component_meshes({"part-short": path})["part-short"]
            self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
            self.assertEqual(caught.exception.detail["component"], "part-short")
            self.assertEqual(caught.exception.detail["face_index"], 1)
            self.assertEqual(caught.exception.detail["completed_faces"], 0)
            self.assertEqual(caught.exception.detail["classification"], "empty")
            self.assertFalse(path.exists())

    def test_fresh_nested_occurrences_keep_duplicate_leaf_names_distinct(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, _, shared, _, _ = self._configured_occurrences(root, nested=True)
            main = backend._document_by_path(str(root / "robot.SLDASM"))
            main._children.reverse()
            for name, count, extent in (("sub-1/part-1", 1, 1.0), ("sub-2/part-1", 2, 2.0)):
                path = root / f"{count}.stl"
                backend.export_component_meshes({name: path})[name]
                self.assertEqual(read_stl(path).triangles, count)
                self.assertEqual(read_stl(path).high, (extent, extent, 0.0))
            self.assertEqual(shared.active_configuration, "Parked")
            backend.verify_sources_unchanged()

    def test_unreadable_fresh_occurrence_blocks_with_context_and_no_cached_fallback(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, _, _, short, _ = self._configured_occurrences(root)
            short.GetChildren = _raiser(RuntimeError("fresh occurrence tree unavailable"))
            path = root / "blocked.stl"
            with self.assertRaises(CadError) as caught:
                backend.export_component_meshes({"part-short": path})["part-short"]
            self.assertEqual(caught.exception.code, "cad_source_state_unreadable")
            self.assertEqual(caught.exception.detail["phase"], "geometry")
            self.assertEqual(caught.exception.detail["component"], "part-short")
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
            self.assertFalse(path.exists())

    def test_geometry_cannot_restart_a_lost_capture_session(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, _, _, _, _ = self._configured_occurrences(root)
            backend._sessions.clear()
            backend._session_factory = _raiser(AssertionError("a new CAD application must not be started"))
            with self.assertRaises(CadError) as caught:
                backend.export_component_meshes({"part-short": root / "blocked.stl"})["part-short"]
            self.assertEqual(caught.exception.code, "cad_source_state_unreadable")
            self.assertEqual(caught.exception.detail["phase"], "geometry")
            self.assertIn("original owned capture session", caught.exception.detail["error"])

    def test_unreadable_fresh_occurrence_fields_are_not_reported_as_drift(self):
        for field in ("Name2", "IsSuppressed", "ReferencedConfiguration"):
            with self.subTest(field=field), TemporaryDirectory() as tmp, _com_stubs():
                root = Path(tmp)
                backend, _, _, short, _ = self._configured_occurrences(root)
                setattr(short, field, None)
                with self.assertRaises(CadError) as caught:
                    backend.export_component_meshes({"part-short": root / "blocked.stl"})["part-short"]
                self.assertEqual(caught.exception.code, "cad_source_state_unreadable")
                self.assertEqual(caught.exception.detail["phase"], "geometry")

    def test_unreadable_name_does_not_report_the_previous_occurrence(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            backend, _, shared, short, part = self._configured_occurrences(root)

            class UnreadableName(type(short)):
                def __getattribute__(self, name):
                    if name == "Name2":
                        raise RuntimeError("current occurrence name unavailable")
                    return super().__getattribute__(name)

            main = backend._document_by_path(str(root / "robot.SLDASM"))
            main._children[main._children.index(short)] = UnreadableName(
                "part-short", part, doc=shared, configuration="Short"
            )
            with self.assertRaises(CadError) as caught:
                backend.export_component_meshes({"part-short": root / "blocked.stl"})["part-short"]
            self.assertEqual(caught.exception.code, "cad_source_state_unreadable")
            self.assertEqual(caught.exception.detail["component"], "")
            self.assertEqual(caught.exception.detail["phase"], "geometry")

    def test_source_guard_reacquires_assembly_instead_of_reading_retained_proxy(self):
        class Expired:
            @property
            def ActiveConfiguration(self):
                raise RuntimeError("retained assembly interface is unavailable")

        with TemporaryDirectory() as tmp, _com_stubs():
            backend, _, _, _, _ = self._configured_occurrences(Path(tmp))
            path = str(Path(tmp) / "robot.SLDASM")
            app = backend._app_obj()
            original = backend._document_by_path(path)
            app._docs[os.path.normcase(os.path.abspath(path))] = _Doc(
                path,
                doc_type=2,
                children=original._children,
            )
            original.ConfigurationManager = Expired()
            self.assertIn(str(Path(tmp) / "robot.SLDASM"), backend.verify_sources_unchanged())

    def test_source_guard_reads_owned_document_when_component_document_proxy_is_lost(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            backend, _, shared, short, part = self._configured_occurrences(Path(tmp))
            app = backend._app_obj()
            app._docs[os.path.normcase(os.path.abspath(str(part)))] = shared
            short.GetModelDoc2 = lambda: None
            self.assertIn(str(part), backend.verify_sources_unchanged())

    def test_source_guard_unreadable_document_keeps_path_and_occurrence_context(self):
        class Unreadable(_Doc):
            def __getattribute__(self, name):
                if name == "ConfigurationManager":
                    raise RuntimeError("current document configuration interface is unavailable")
                return super().__getattribute__(name)

        with TemporaryDirectory() as tmp, _com_stubs():
            backend, _, _, _, part = self._configured_occurrences(Path(tmp))
            app = backend._app_obj()
            app._docs[os.path.normcase(os.path.abspath(str(part)))] = Unreadable(part, configuration="Parked")
            with self.assertRaises(CadError) as caught:
                backend.verify_sources_unchanged()
            self.assertEqual(caught.exception.code, "cad_source_state_unreadable")
            self.assertEqual(caught.exception.detail["path"], str(part))
            self.assertIn(caught.exception.detail["component"], {"part-short", "part-long"})
            self.assertEqual(caught.exception.detail["phase"], "verify_sources")
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_source_guard_does_not_start_a_session_when_the_original_is_lost(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            backend, _, _, _, _ = self._configured_occurrences(Path(tmp))
            backend._sessions.clear()

            def forbidden_factory():
                raise AssertionError("verification cannot start another CAD application")

            backend._session_factory = forbidden_factory
            with self.assertRaises(CadError) as caught:
                backend.verify_sources_unchanged()
            self.assertEqual(caught.exception.code, "cad_source_state_unreadable")
            self.assertIn("original owned capture session", caught.exception.detail["error"])

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

    def test_missing_and_duplicate_requested_datums_fail(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm_path = _write(root, "arm.SLDPRT")
            arm_doc = _Doc(arm_path)
            arm_component = _Component("arm-1", arm_path, doc=arm_doc)
            assembly = _write(root, "robot.SLDASM")
            main_doc = _Doc(assembly, doc_type=2, children=[arm_component])
            with self.assertRaises(CadError) as caught:
                self._capture(_App({assembly: main_doc}), assembly, ["CS_missing"])
            error = caught.exception
            self.assertEqual(error.code, "cad_missing_coordinate_system")
            self.assertIn("CS_missing", f"{error.message} {error.detail}")

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

    def test_collect_scene_continues_on_the_refreshed_document_after_rebuild(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part_path = _write(root, "part.SLDPRT")
            part = _Doc(part_path, doc_type=1)
            component = _Component("part-1", part_path, doc=part)
            assembly = _write(root, "robot.SLDASM")
            first = _Doc(assembly, doc_type=2, children=[component])
            refreshed = _Doc(assembly, doc_type=2, children=[component])

            class RefreshingBackend(_CaptureBackend):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self.lookups = []
                    self.save_flag_docs = []

                def _rebuild_capture_copy(self, doc, path):
                    assert doc is first, "the pre-rebuild handle is the collected copy"
                    refreshed_doc = self._document_by_path(path)
                    return refreshed_doc, {
                        "rebuilt": True,
                        "document": str(path),
                        "configuration": str(first.active_configuration),
                    }

                def _document_by_path(self, path):
                    self._app_for_path(path)  # keep the owned session registered
                    self.lookups.append(str(path))
                    if str(path) == str(assembly):
                        return first if self.lookups.count(str(assembly)) == 1 else refreshed
                    return part

                def _record_save_flag(self, doc, path):
                    self.save_flag_docs.append(doc)
                    self.save_flags[str(path)] = False

            backend = RefreshingBackend(session_factory=lambda: _Session(_App({assembly: first})))
            backend.collect_scene(str(assembly), [])
            self.assertEqual(backend.lookups[:2], [str(assembly), str(assembly)])
            self.assertTrue(backend.save_flag_docs)
            self.assertIs(backend.save_flag_docs[0], refreshed)

    def test_collect_scene_success_persists_production_rebuild_preparation(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            left_path = _write(root, "left.SLDPRT")
            right_path = _write(root, "right.SLDPRT")
            subassembly_path = _write(root, "arm-stack.SLDASM")
            assembly_path = _write(root, "robot.SLDASM")

            class SaveTrackingDocument(_Doc):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.save_calls = 0

                def GetSaveFlag(self):
                    return False

                def Save(self):
                    self.save_calls += 1
                    return True

            class RebuildTrackingExtension:
                def __init__(self, extension, calls):
                    self._extension = extension
                    self._calls = calls

                @property
                def NeedsRebuild2(self):
                    return self._extension.NeedsRebuild2

                def Rebuild(self, options):
                    self._calls.append(options)
                    return self._extension.Rebuild(options)

            left_document = SaveTrackingDocument(left_path, doc_type=1)
            right_document = SaveTrackingDocument(right_path, doc_type=1)
            left = _Component("left-1", left_path, doc=left_document)
            right = _Component("right-1", right_path, doc=right_document)
            subassembly_document = SaveTrackingDocument(
                subassembly_path,
                doc_type=2,
                children=[left, right],
            )
            subassembly = _Component(
                "arm-stack-1",
                subassembly_path,
                children=[left, right],
                doc=subassembly_document,
            )
            first = SaveTrackingDocument(
                assembly_path,
                doc_type=2,
                children=[subassembly],
                needs_rebuild2=1,
            )
            refreshed = SaveTrackingDocument(
                assembly_path,
                doc_type=2,
                children=[subassembly],
            )
            rebuild_calls = []
            first.Extension = RebuildTrackingExtension(first.Extension, rebuild_calls)
            documents = {
                str(left_path): left_document,
                str(right_path): right_document,
                str(subassembly_path): subassembly_document,
                str(assembly_path): first,
            }

            class ProductionRebuildBackend(_CaptureBackend):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self.lookups = []
                    self._capture_roots = [normalize_document_path(os.path.abspath(str(root)))]

                def _rebuild_capture_copy(self, doc, path):
                    return SolidWorksBackend._rebuild_capture_copy(self, doc, path)

                def _document_by_path(self, path):
                    self._app_for_path(path)
                    self.lookups.append(str(path))
                    if str(path) == str(assembly_path):
                        return (
                            first
                            if self.lookups.count(str(assembly_path)) == 1
                            else refreshed
                        )
                    return documents[str(path)]

            backend = ProductionRebuildBackend(
                session_factory=lambda: _Session(
                    _App(
                        {
                            assembly_path: first,
                            subassembly_path: subassembly_document,
                            left_path: left_document,
                            right_path: right_document,
                        }
                    )
                )
            )
            source_documents = (
                assembly_path,
                subassembly_path,
                left_path,
                right_path,
            )
            hashes_before = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_documents
            }

            scene = backend.collect_scene(str(assembly_path), [])

            hashes_after = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_documents
            }
            preparation = scene.notes["capture_preparation"]
            raw = _raw_scene_payload(
                scene,
                {"assembly": str(assembly_path)},
                root,
            )
            raw_path = root / "raw" / "scene_raw.json"
            raw_path.parent.mkdir()
            write_json(raw_path, raw)
            persisted = read_json(raw_path)

            self.assertEqual({component.name for component in scene.components}, {"left-1", "right-1"})
            self.assertEqual(rebuild_calls, [2])
            self.assertEqual(backend.lookups[:2], [str(assembly_path), str(assembly_path)])
            self.assertTrue(preparation["rebuild_performed"])
            self.assertIs(preparation["saved_to_disk"], False)
            self.assertEqual(
                preparation["source_sha256_before"],
                hashes_before[assembly_path],
            )
            self.assertEqual(
                preparation["source_sha256_after"],
                hashes_before[assembly_path],
            )
            self.assertEqual(hashes_after, hashes_before)
            self.assertEqual(
                persisted["notes"]["capture_preparation"],
                preparation,
            )
            self.assertTrue(
                all(
                    document.save_calls == 0
                    for document in (
                        first,
                        refreshed,
                        subassembly_document,
                        left_document,
                        right_document,
                    )
                )
            )

    def test_collect_scene_enforces_read_only_on_the_refreshed_handle(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part_path = _write(root, "part.SLDPRT")
            part = _Doc(part_path, doc_type=1)
            component = _Component("part-1", part_path, doc=part)
            assembly = _write(root, "robot.SLDASM")

            class MutableReadOnly(_Doc):
                def SetReadOnlyState(self, value):
                    self.read_only_calls.append(value)
                    self.IsOpenedReadOnly = value
                    return True

            first = _Doc(assembly, doc_type=2, children=[component])
            refreshed = MutableReadOnly(assembly, doc_type=2, children=[component])
            refreshed.IsOpenedReadOnly = False
            refreshed.read_only_calls = []
            refreshed.rebuilt_generation = 0

            class ReadOnlyRefreshingBackend(_CaptureBackend):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self.lookups = []
                    self.reopens = []
                    self.save_flag_docs = []

                def _rebuild_capture_copy(self, doc, path):
                    refreshed.rebuilt_generation += 1
                    refreshed_doc = self._document_by_path(path)
                    return refreshed_doc, {
                        "rebuilt": True,
                        "document": str(path),
                        "configuration": str(first.active_configuration),
                    }

                def _document_by_path(self, path):
                    self._app_for_path(path)
                    self.lookups.append(str(path))
                    doc = part
                    if str(path) == str(assembly):
                        doc = first if self.lookups.count(str(assembly)) == 1 else refreshed
                    return _read_only_document(doc)

                def _record_save_flag(self, doc, path):
                    self.save_flag_docs.append(doc)
                    self.save_flags[str(path)] = False

                def open_document(self, path):
                    self.reopens.append(path)
                    raise AssertionError("a refreshed handle must not reopen the document")

                def _ensure_document(self, path):
                    raise AssertionError("no reopen fallback is allowed")

            backend = ReadOnlyRefreshingBackend(session_factory=lambda: _Session(_App({assembly: first})))
            backend.collect_scene(str(assembly), [])
            self.assertEqual(refreshed.read_only_calls, [True])
            self.assertIs(refreshed.IsOpenedReadOnly, True)
            self.assertEqual(refreshed.rebuilt_generation, 1)
            self.assertEqual(backend.lookups[:2], [str(assembly), str(assembly)])
            self.assertTrue(backend.save_flag_docs)
            self.assertIs(backend.save_flag_docs[0], refreshed)
            self.assertEqual(backend.reopens, [])

    def test_collect_scene_rejects_configuration_change_at_the_refresh_boundary(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part_path = _write(root, "part.SLDPRT")
            part = _Doc(part_path, doc_type=1)
            component = _Component("part-1", part_path, doc=part)
            assembly = _write(root, "robot.SLDASM")

            class RebuildableDoc(_Doc):
                def GetSaveFlag(self):
                    return False

            first = RebuildableDoc(
                assembly,
                doc_type=2,
                children=[component],
                needs_rebuild2=1,
            )
            refreshed = RebuildableDoc(assembly, doc_type=2, children=[component], configuration="Other")

            class ConfigChangingBackend(_CaptureBackend):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self.lookups = []
                    self._capture_roots = [normalize_document_path(os.path.abspath(str(root)))]

                def _rebuild_capture_copy(self, doc, path):
                    return SolidWorksBackend._rebuild_capture_copy(self, doc, path)

                def _document_by_path(self, path):
                    self._app_for_path(path)
                    self.lookups.append(str(path))
                    if str(path) == str(assembly):
                        return first if self.lookups.count(str(assembly)) == 1 else refreshed
                    return part

            backend = ConfigChangingBackend(session_factory=lambda: _Session(_App({assembly: first})))
            with self.assertRaises(CadError) as caught:
                backend.collect_scene(str(assembly), [])
            self.assertEqual(caught.exception.code, "cad_configuration_mismatch")
            self.assertEqual(backend.lookups[:2], [str(assembly), str(assembly)])

    def test_collect_scene_performs_one_post_rebuild_document_lookup(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            part_path = _write(root, "part.SLDPRT")
            part = _Doc(part_path, doc_type=1)
            component = _Component("part-1", part_path, doc=part)
            assembly = _write(root, "robot.SLDASM")
            first = _Doc(assembly, doc_type=2, children=[component])
            refreshed = _Doc(assembly, doc_type=2, children=[component])

            class CountingBackend(_CaptureBackend):
                """Mimics production: the rebuild resolves the document once itself."""

                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self.lookups = []

                def _rebuild_capture_copy(self, doc, path):
                    refreshed_doc = self._document_by_path(path)
                    return refreshed_doc, {
                        "rebuilt": True,
                        "document": str(path),
                        "configuration": str(first.active_configuration),
                    }

                def _document_by_path(self, path):
                    self._app_for_path(path)
                    self.lookups.append(str(path))
                    if str(path) == str(assembly):
                        return first if self.lookups.count(str(assembly)) == 1 else refreshed
                    return part

            backend = CountingBackend(session_factory=lambda: _Session(_App({assembly: first})))
            backend.collect_scene(str(assembly), [])
            self.assertEqual(backend.lookups.count(str(assembly)), 2)


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

    def test_marked_main_must_pass_the_strict_open_with_diagnostics(self) -> None:
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

            with self.assertRaises(CadError) as caught:
                _read(root, app)

            error = caught.exception
            self.assertEqual(error.code, "cad_document_open_failed")
            self.assertEqual(error.detail["errors"], 2)
            self.assertEqual(
                error.detail["unresolved_references"],
                [{"name": "imported_part", "last_known_path": r"C:\missing\imported_part.SLDPRT"}],
            )

    def test_open_failure_without_dependency_enumeration_keeps_original_error(self) -> None:
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

            error = caught.exception
            self.assertEqual(error.code, "cad_document_open_failed")
            self.assertEqual(error.detail["errors"], 2)
            self.assertNotIn("unresolved_references", error.detail)

    def test_malformed_dependency_reply_cannot_mask_open_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(assembly, doc_type=2)
            app = _App({assembly: doc}, open_errors={assembly: 2}, not_preopened={assembly})
            app.GetDocumentDependencies2 = lambda *args: 7
            with self.assertRaises(CadError) as caught:
                _read(root, app)
            self.assertEqual(caught.exception.code, "cad_document_open_failed")
            self.assertEqual(caught.exception.detail["errors"], 2)
            self.assertNotIn("unresolved_references", caught.exception.detail)

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

    def test_transitively_open_declared_main_needs_a_fresh_exact_status(self) -> None:
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
            app = _App({carrier: carrier_doc}, open_errors={carried: 2})

            with self.assertRaises(CadError) as caught:
                _read(root, app)

            error = caught.exception
            self.assertEqual(error.code, "cad_document_open_failed")
            self.assertEqual(error.detail["errors"], 2)


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
            not_root.IsRoot = False
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
            del unreadable.IsRoot
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
            not_bool.IsRoot = 1
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
        from description_pipeline.sources.solidworks import native as native_module

        identity = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
        scaled = (2.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
        reflected = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
        skewed_tail = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 2.0)
        self.assertIsNotNone(native_module._frame_matrix(identity))
        self.assertIsNone(native_module._frame_matrix(scaled))
        self.assertIsNone(native_module._frame_matrix(reflected))
        self.assertIsNone(native_module._frame_matrix(skewed_tail))
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

    def test_inspect_copy_refuses_a_mismatched_binding_that_is_not_suppressed(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            bad = _Component("arm-1", _write(root, "arm.SLDPRT"), suppression=5)
            assembly = _write(root, "robot.SLDASM")
            documents = {assembly: _Doc(assembly, doc_type=2, children=[bad])}
            backend = SolidWorksBackend(session_factory=lambda: _Session(_App(documents)))
            self.assertFalse(bad.IsSuppressed)
            with self.assertRaises(CadError) as caught:
                backend.inspect_copy(str(assembly))
            self.assertEqual(caught.exception.code, "cad_component_internal_id_mismatch")

    def test_inspect_copy_emits_raw_state_with_coherent_flags(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            resolved = _Component("arm-1", _write(root, "arm.SLDPRT"), suppression=3)
            shim = _Component("shim-1", _write(root, "shim.SLDPRT"), suppression=0)
            assembly = _write(root, "robot.SLDASM")
            documents = {assembly: _Doc(assembly, doc_type=2, children=[resolved, shim])}
            backend = SolidWorksBackend(session_factory=lambda: _Session(_App(documents)))
            result = backend.inspect_copy(str(assembly))
            by_name = {item["name"]: item for item in result["instances"]}
            self.assertEqual((by_name["arm-1"]["suppression_state"], by_name["arm-1"]["suppressed"]), (3, False))
            self.assertEqual((by_name["shim-1"]["suppression_state"], by_name["shim-1"]["suppressed"]), (0, True))

    def test_collect_scene_refuses_a_mismatched_binding(self):
        with TemporaryDirectory() as tmp, _com_stubs():
            root = Path(tmp)
            bad = _Component("arm-1", _write(root, "arm.SLDPRT"), suppression=5)
            assembly = _write(root, "robot.SLDASM")
            documents = {assembly: _Doc(assembly, doc_type=2, children=[bad])}
            backend = _CaptureBackend(session_factory=lambda: _Session(_App(documents)))
            with self.assertRaises(CadError) as caught:
                backend.collect_scene(str(assembly), [])
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
                    backend.collect_scene(str(assembly), [])
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
                    backend.collect_scene(str(assembly), [])
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
            by_name = {item["name2"]: item for item in record["components"]}

            def triple(name):
                item = by_name[name]
                return (item["suppression_state"], item["suppressed"], item["lightweight"])

            self.assertEqual(triple("arm-1"), (2, False, False))
            self.assertEqual(triple("arm-2"), (1, False, True))

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
        return types.SimpleNamespace(IsOpenedReadOnly=True, GetPathName=lambda: str(path))

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

    def __init__(self, state, reads):
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

    def Recalculate(self):
        names = tuple(component.Name2 for component in self.SelectedItems.value)
        self._state.append(names)
        seen = sum(1 for entry in self._state if entry == names)
        payload = self._reads[names][seen - 1]
        self.Mass = float(payload["mass_kg"])
        self.CenterOfMass = [float(value) for value in payload["com_m"]]
        self._tensor = [[float(value) for value in row] for row in payload["inertia_tensor_about_com"]]
        self._overrides = dict(payload.get("overrides") or self._overrides)

    def GetOverrideOptions(self):
        return types.SimpleNamespace(**self._overrides)

    def GetMomentOfInertia(self, kind):
        if kind == 0:
            return [value for row in self._tensor for value in row]
        raise RuntimeError("about-origin read unavailable")


class LinkGroupMassPropertiesTests(unittest.TestCase):
    """The direct one-shot reader: exact paths, two fresh reads, fail closed."""

    def _read(self, mass, com=(0.1, 0.2, 0.3), tensor=None, overrides=None):
        return {
            "mass_kg": mass,
            "com_m": list(com),
            "inertia_tensor_about_com": tensor or [[1e-6, 0.0, 0.0], [0.0, 2e-6, 0.0], [0.0, 0.0, 3e-6]],
            "overrides": overrides
            or {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False},
        }

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
            doc.Extension.CreateMassProperty2 = lambda: _LinkMassProperty(state, reads)
            backend = _CaptureBackend(session_factory=lambda: _Session(_App({assembly: doc})))
            yield backend, assembly, state

    def test_reads_each_body_twice_with_the_exact_sorted_selection(self):
        reads = {("c-1", "sub-1"): [self._read(2.0), self._read(2.0)]}
        with self._backend(reads) as (backend, assembly, state):
            payload = backend.link_group_mass_properties(str(assembly), {"body": ["sub-1", "c-1"]})
        self.assertEqual(payload["used_api"], "IMassProperty2 (SelectedItems = component set)")
        self.assertEqual(payload["selection_rule"], "top_most_providers+uncovered_leaves")
        self.assertEqual(payload["assembly"], str(assembly))
        row = payload["links"]["body"]
        self.assertEqual(row["selection"], ["c-1", "sub-1"])
        self.assertEqual(len(row["reads"]), 2)
        self.assertEqual(
            row["reproducibility"], {"mass_delta_kg": 0.0, "com_delta_norm_m": 0.0, "tensor_delta_max_abs": 0.0}
        )
        self.assertEqual(row["reads"][0]["mass_kg"], 2.0)
        self.assertEqual(row["reads"][0]["volume_m3"], 0.001)
        self.assertEqual(row["reads"][0]["density_kg_m3"], 1.0)
        self.assertEqual(
            row["reads"][0]["overrides"],
            {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False},
        )
        self.assertEqual(state, [("c-1", "sub-1"), ("c-1", "sub-1")])

    def test_reproducibility_records_the_repeat_deltas(self):
        first = self._read(2.0, (0.1, 0.2, 0.3))
        second = self._read(2.001, (0.1, 0.2, 0.31), [[1.1e-6, 0.0, 0.0], [0.0, 2e-6, 0.0], [0.0, 0.0, 3e-6]])
        with self._backend({("c-1", "sub-1"): [first, second]}) as (backend, assembly, _state):
            payload = backend.link_group_mass_properties(str(assembly), {"body": ["c-1", "sub-1"]})
        row = payload["links"]["body"]
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
        reads = {("c-1",): [self._read(2.0, overrides=overrides), self._read(2.0, overrides=overrides)]}
        with self._backend(reads) as (backend, assembly, state):
            payload = backend.link_group_mass_properties(str(assembly), {"body": ["c-1"]})
        self.assertEqual(state, [("c-1",), ("c-1",)])
        row = payload["links"]["body"]
        self.assertEqual(row["selection"], ["c-1"])
        self.assertEqual(len(row["reads"]), 2)
        for read in row["reads"]:
            self.assertEqual(read["overrides"], overrides)
        self.assertEqual(row["reproducibility"]["com_delta_norm_m"], 0.0)

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
        with self._backend(reads) as (backend, assembly, _state), self.assertRaises(CadError) as caught:
            backend.assembly_mass_properties(str(assembly))
        self.assertEqual(caught.exception.code, "cad_mass_override")
        self.assertTrue(caught.exception.detail["OverrideMass"])

    def test_a_part_document_is_refused(self):
        with self._backend({}, doc_type=1) as (backend, assembly, _state):
            with self.assertRaises(CadError) as caught:
                backend.link_group_mass_properties(str(assembly), {"body": ["c-1"]})
            self.assertEqual(caught.exception.code, "cad_not_assembly")


class CloseAllDocumentsTests(unittest.TestCase):
    """The pristine readout window's close-all helper fails closed on leftovers."""

    def _backend(self, app):
        return SolidWorksBackend(session_factory=lambda: _Session(app))

    def _fast_settle(self):
        return (
            patch("description_pipeline.sources.solidworks.native.CLOSE_ALL_SETTLE_ATTEMPTS", 2),
            patch("description_pipeline.sources.solidworks.native.CLOSE_ALL_SETTLE_DELAY_S", 0.0),
        )

    def test_close_all_documents_closes_without_saving_dirty_documents(self):
        calls = []
        app = _App({})

        def close(confirm):
            calls.append(confirm)
            return True

        def documents():
            # The real capture's read-write copy only releases with the
            # close-without-save flag; a False call leaves it open.
            if calls and calls[-1] is True:
                return []
            return [types.SimpleNamespace(GetTitle=lambda: "dirty", GetPathName=lambda: "C:/dirty.SLDASM")]

        app.CloseAllDocuments = close
        app.GetDocuments = documents
        attempts, delay = self._fast_settle()
        with _com_stubs(), attempts, delay:
            self.assertEqual(self._backend(app).close_all_documents(), {"closed": True})
        self.assertEqual(calls, [True])

    def test_close_all_documents_settles_before_concluding_leftovers(self):
        state = {"enumerations": 0}
        app = _App({})
        app.CloseAllDocuments = lambda confirm: True

        def documents():
            state["enumerations"] += 1
            if state["enumerations"] == 1:
                return [types.SimpleNamespace(GetTitle=lambda: "slow", GetPathName=lambda: "C:/slow.SLDASM")]
            return []

        app.GetDocuments = documents
        attempts, delay = self._fast_settle()
        with _com_stubs(), attempts, delay:
            self.assertEqual(self._backend(app).close_all_documents(), {"closed": True})
        self.assertEqual(state["enumerations"], 2)

    def test_close_all_documents_force_closes_remaining_documents_once(self):
        closed = []
        state = {"settled": False}
        app = _App({})
        app.CloseAllDocuments = lambda confirm: True

        def documents():
            if state["settled"]:
                return []
            return [types.SimpleNamespace(GetTitle=lambda: "stuck", GetPathName=lambda: "C:/stuck.SLDASM")]

        def close_doc(name):
            closed.append(name)
            state["settled"] = True
            return 1

        app.GetDocuments = documents
        app.CloseDoc = close_doc
        attempts, delay = self._fast_settle()
        with _com_stubs(), attempts, delay:
            self.assertEqual(self._backend(app).close_all_documents(), {"closed": True})
        self.assertEqual(closed, ["stuck"])

    def test_close_all_documents_persistent_leftover_fails_closed(self):
        closed = []
        calls = []
        app = _App({})
        app.CloseAllDocuments = lambda confirm: calls.append(confirm) or True
        app.GetDocuments = lambda: [
            types.SimpleNamespace(GetTitle=lambda: "left", GetPathName=lambda: "C:/left.SLDASM")
        ]
        app.CloseDoc = lambda name: closed.append(name) or 0
        attempts, delay = self._fast_settle()
        with _com_stubs(), attempts, delay, self.assertRaises(CadError) as caught:
            self._backend(app).close_all_documents()
        self.assertEqual(caught.exception.code, "cad_close_failed")
        self.assertEqual(caught.exception.detail["open"], ["C:/left.SLDASM"])
        self.assertEqual(caught.exception.detail["count"], 1)
        self.assertEqual(calls, [True])
        self.assertEqual(closed, ["left"])

    def test_close_all_documents_reports_success(self):
        app = _App({})
        app.CloseAllDocuments = lambda confirm: True
        app.GetDocuments = list
        with _com_stubs():
            self.assertEqual(self._backend(app).close_all_documents(), {"closed": True})

    def test_close_all_documents_failure_is_fail_closed(self):
        def refuse(_confirm):
            raise RuntimeError("server busy")

        app = _App({})
        app.CloseAllDocuments = refuse
        app.GetDocuments = list
        with _com_stubs(), self.assertRaises(CadError) as caught:
            self._backend(app).close_all_documents()
        self.assertEqual(caught.exception.code, "cad_close_failed")


if __name__ == "__main__":
    unittest.main()
