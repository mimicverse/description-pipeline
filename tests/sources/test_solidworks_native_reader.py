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

import os
import types
import unittest
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory

from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.sources.solidworks.native import SolidWorksBackend

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
    def __init__(self, component, feature_name, geometry=None, *, curve=None, point=None):
        self.ReferenceComponent = component
        self.Reference = _Target(feature_name, geometry, curve=curve, point=point)


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

    def MinimumVariation(self):
        return self._value(self._lower)

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
    ):
        self.Name = name
        self._type_name = type_name
        self._specific = specific
        self._suppressed = suppressed
        self._error_code = error_code
        self._first_sub = first_sub
        self._next_sub = next_sub
        self._next_feature = next_feature

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


class _Component:
    def __init__(
        self,
        name,
        path,
        *,
        children=(),
        doc=None,
        suppressed=False,
        transform=SW_IDENTITY,
        configuration="Default",
    ):
        self.Name2 = name
        self._path = str(path)
        self._children = list(children)
        self._doc = doc
        self._transform = tuple(transform)
        self.IsSuppressed = suppressed
        self.IsFixed = False
        self.ReferencedConfiguration = configuration

    def GetPathName(self):
        return self._path

    def GetTotalTransform(self, _resolved):
        return types.SimpleNamespace(ArrayData=list(self._transform))

    def GetChildren(self):
        return list(self._children)

    def GetModelDoc2(self):
        return self._doc


class _PropertyManager:
    def __init__(self, values):
        self._values = dict(values)

    def GetNames(self):
        return list(self._values)

    def Get6(self, name, _configuration, _resolved):
        return (self._values[name],)


class _Extension:
    def __init__(self, scopes, coordinate_systems=None):
        self._scopes = scopes
        self._coordinate_systems = dict(coordinate_systems or {})

    def CustomPropertyManager(self, scope):
        return _PropertyManager(self._scopes.get(scope, {}))

    def GetCoordinateSystemTransformByName(self, name):
        return types.SimpleNamespace(ArrayData=list(self._coordinate_systems[name]))


class _ConfigurationManager:
    """Reflects the document's *currently shown* configuration at each read."""

    def __init__(self, doc):
        self._doc = doc

    @property
    def ActiveConfiguration(self):
        name = self._doc.active_configuration
        root = types.SimpleNamespace(GetChildren=lambda: list(self._doc.children_for(name)))
        return types.SimpleNamespace(Name=name, GetRootComponent3=lambda _visible: root)


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
        children=(),
        configuration_children=None,
        configuration="Default",
        show_success=True,
        show_effect=True,
    ):
        self._path = str(path)
        self._children = list(children)
        self._configuration_children = {
            name: list(items) for name, items in (configuration_children or {}).items()
        }
        self.active_configuration = configuration
        self._show_success = show_success
        self._show_effect = show_effect
        scopes = {"": dict(properties or {})}
        for name, values in (configuration_properties or {}).items():
            scopes[name] = dict(values)
        self.Extension = _Extension(scopes, coordinate_systems)
        self.ConfigurationManager = _ConfigurationManager(self)
        self._first_feature = first_feature

    def children_for(self, name):
        return self._configuration_children.get(name, self._children)

    def ShowConfiguration2(self, name):
        """Documented contract: boolean success, never an exception for a bad name."""

        if not self._show_success or name not in self._configuration_children:
            return False
        if self._show_effect:
            self.active_configuration = name
        return True

    def GetPathName(self):
        return self._path

    def GetTitle(self):
        return os.path.basename(self._path)

    def ForceRebuild3(self, _force):
        return True

    def FirstFeature(self):
        return self._first_feature() if callable(self._first_feature) else self._first_feature

    def GetComponents(self, _ignored):
        return list(self._children)


class _App:
    """Minimal ``ISldWorks`` surface; documents are pre-opened read-only."""

    RevisionNumber = "2026-portable-mock"
    Visible = False

    def __init__(self, docs):
        self._docs = {os.path.normcase(os.path.abspath(str(path))): doc for path, doc in docs.items()}

    def GetOpenDocumentByName(self, path):
        return self._docs.get(os.path.normcase(os.path.abspath(str(path))))

    def GetBuildNumbers(self):
        return "portable-mock"

    def GetCurrentLicenseType(self):
        return 1

    def GetDocuments(self):
        return list(self._docs.values())


class _Session:
    def __init__(self, app):
        self.app = app

    def connect(self, _cancelled):
        return None

    def close(self):
        return None

    def identity(self):
        return {"reader": "portable-mock"}


def _write(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(f"portable fixture: {name}\n".encode())
    return path


def _read(root: Path, app: _App, settings=None) -> dict:
    backend = SolidWorksBackend(session_factory=lambda: _Session(app))
    return backend.discover_native(root, settings if settings is not None else {})


def _mate(name, mate_type, entities, *, lower=0.0, upper=0.0, count=None, suppressed=False, error_code=0):
    specific = _MateSpecific(mate_type, entities, lower=lower, upper=upper, count=count)
    return _Feature(name, f"Mate{mate_type}", specific=specific, suppressed=suppressed, error_code=error_code)


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
            cylinder_entity = _MateEntity(
                component, "Cylinder1", _Cylinder((0.0, 0.0, 0.0), (0.0, 0.0, 2.0), 0.02)
            )
            travel = _mate("travel_limit", 5, [plane_entity], lower=0.0, upper=0.03)
            swing = _mate("swing_limit", 6, [cylinder_entity], lower=-0.1, upper=0.2, suppressed=True)
            open_distance = _mate("travel_open", 5, [plane_entity], lower=0.0, upper=0.0)
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
                {"name", "type", "suppressed", "limits", "entities", "error_code", "scope", "configuration"},
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
            self.assertEqual(mates["swing_limit"]["type"], "limitangle")
            self.assertEqual(mates["swing_limit"]["limits"], {"lower": -0.1, "upper": 0.2, "unit": "rad"})
            self.assertIs(mates["swing_limit"]["suppressed"], True)
            self.assertEqual(mates["swing_limit"]["entities"][0]["cylinder"], CYLINDER_Z)
            self.assertEqual(mates["travel_open"]["type"], "distance")
            self.assertIsNone(mates["travel_open"]["limits"])
            self.assertEqual(mates["locked_seat"]["type"], "lock")
            self.assertIs(mates["locked_seat"]["suppressed"], True)
            self.assertEqual(mates["broken_solve"]["type"], "coincident")
            self.assertEqual(mates["broken_solve"]["error_code"], 3)

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
            sub_component = _Component(
                "sub-1", sub_path, children=[arm_component], doc=sub_doc, configuration="Sub"
            )
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
            self.assertEqual(
                sorted(record["files"]), ["arm.SLDPRT", "robot.SLDASM", "sub.SLDASM"]
            )


class ProducerContextTests(unittest.TestCase):
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
            self.assertEqual(set(main_datum), {"name", "owner", "array", "configuration"})
            self.assertEqual(main_datum["name"], "CS_main")
            self.assertEqual(main_datum["configuration"], "Robot")
            datums = [datum for datum in record["datums"] if datum["owner"] == "sub-1/arm-1"]
            self.assertEqual([datum["name"] for datum in datums], ["CS_tip"])
            self.assertEqual(datums[0]["configuration"], "Tip")
            matrix = datums[0]["array"]
            self.assertEqual(
                [round(value, 10) for value in (matrix[0], matrix[5], matrix[10])], [1.0, 1.0, 1.0]
            )
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
            occurrence_left = _Component(
                "sub-a", sub_path, children=[arm_left], doc=sub_doc, configuration="Left"
            )
            occurrence_right = _Component(
                "sub-b", sub_path, children=[arm_right], doc=sub_doc, configuration="Right"
            )
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
            scoped_reference = types.SimpleNamespace(Name2="sub-1/arm-1", GetPathName=arm_component.GetPathName)
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
            leaf_reference = types.SimpleNamespace(Name2="arm-1", GetPathName=arm_component.GetPathName)
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
                set(geometry), {"component", "feature", "face_index", "cylinder"}
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
            self.assertEqual(set(geometry), {"component", "feature", "face_index", "circle"})
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
            self.assertEqual(
                geometry["scaled_normal"]["plane"], {"normal": [0.0, 0.0, 1.0], "point": [1.0, 0.0, 0.0]}
            )

    def test_vertex_point_geometry_is_recorded(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            component = _Component("base-1", base)
            entity = _MateEntity(component, "Vertex1", point=(1.0, 2.0, 3.0))

            record = self._record(tmp, [("seat", entity)])

            geometry = record["mates"][0]["entities"][0]
            self.assertEqual(set(geometry), {"component", "feature", "face_index", "point"})
            self.assertEqual(geometry["point"], [1.0, 2.0, 3.0])

    def test_nonfinite_entity_geometry_blocks(self) -> None:
        def cylinder(component):
            return _MateEntity(
                component, "C1", _Cylinder((float("nan"), 0.0, 0.0), (0.0, 0.0, 1.0), 0.05)
            )

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
                        lambda index: _MateEntity(component, "Plane1")
                        if index
                        else _raiser(RuntimeError("no entity"))(index),
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
            reference = types.SimpleNamespace(Name2="ghost-1", GetPathName=component.GetPathName)
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


if __name__ == "__main__":
    unittest.main()
