"""Occurrence meshes preserve solids and sheets without shared document reads."""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.geometry.stl import read as read_stl  # noqa: E402
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import SolidWorksBackend  # noqa: E402

TRIANGLE = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


class FakeFace:
    def __init__(self, triangles, surface=None, feature="shaft"):
        self._triangles = triangles
        self._surface = surface
        self.Name = "Face3"
        self._feature = feature

    def GetTessTriangles(self, flag):  # noqa: N802 - SolidWorks API name
        return list(self._triangles)

    def GetSurface(self):  # noqa: N802 - SolidWorks API name
        return self._surface

    def GetFeature(self):  # noqa: N802 - SolidWorks API name
        return SimpleNamespace(Name=self._feature)


class FakeSurface:
    def __init__(self, params):
        self.CylinderParams = list(params)


class FakeBody:
    def __init__(self, faces):
        self._faces = faces

    def GetFaces(self):  # noqa: N802 - SolidWorks API name
        return list(self._faces)


class FakeDocument:
    def __init__(self, values):
        self._values = values
        self.ConfigurationManager = SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name="Default"))

    def GetTessTriangles(self, flag):  # noqa: N802 - SolidWorks API name
        raise AssertionError("mesh export must not read the shared part document")


class FakeComponent:
    def __init__(self, document, bodies):
        self._document = document
        self._bodies = bodies
        self.ReferencedConfiguration = "Default"
        self.IsSuppressed = False
        self.body_calls = []

    def GetModelDoc2(self):  # noqa: N802 - SolidWorks API name
        raise AssertionError("mesh export must not acquire the shared part document")

    def GetPathName(self):  # noqa: N802 - SolidWorks API name
        return "C:/neutral/pcb.SLDPRT"

    def GetBodies2(self, *arguments):  # noqa: N802 - SolidWorks API name
        self.body_calls.append(arguments)
        if len(arguments) != 1:
            raise TypeError("IComponent2.GetBodies2 takes one body type")
        return list(self._bodies.get(arguments[0], ()))

    def GetChildren(self):  # noqa: N802 - SolidWorks API name
        return []


def _backend(component, name="pcb-1") -> SolidWorksBackend:
    backend = SolidWorksBackend()
    component.Name2 = name
    path = "C:/neutral/robot.SLDASM"
    doc = SimpleNamespace(
        ConfigurationManager=SimpleNamespace(
            ActiveConfiguration=SimpleNamespace(
                Name="Default",
                GetRootComponent3=lambda _resolve: SimpleNamespace(GetChildren=lambda: [component]),
            )
        ),
        IsOpenedReadOnly=True,
        GetPathName=lambda: path,
    )
    backend._sessions["source"] = SimpleNamespace(
        app=SimpleNamespace(GetOpenDocumentByName=lambda _path: doc),
        process=SimpleNamespace(alive=lambda: True),
        closed=False,
    )
    session = backend._sessions["source"]
    session.current_application = lambda: session.app
    backend._owner_thread = threading.current_thread()
    backend._scene_document_key = backend._record_source_document(path, "Default")
    part_key = backend._record_source_document(component.GetPathName(), "Default")
    backend._source_components = {name: (part_key, "Default")}
    backend._components = {name}
    return backend


class ComponentMeshExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-mesh-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dest = self.tmp / "mesh.stl"

    def test_sheet_only_part_exports_its_faces(self):
        component = FakeComponent(
            FakeDocument([]),
            {0: [], 1: [FakeBody([FakeFace(TRIANGLE)])]},
        )
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(entry["triangles"], 1)
        self.assertEqual(entry["tessellation_sources"], ["sheet_body_faces"])
        self.assertEqual(entry["bodies"], {"solid": 0, "sheet": 1})
        self.assertIn("GetBodies2(1)", entry["used_api"])
        self.assertEqual(read_stl(self.dest).triangles, 1)

    def test_mixed_part_exports_solids_and_sheets(self):
        component = FakeComponent(
            FakeDocument(list(TRIANGLE)),
            {0: [FakeBody([FakeFace(TRIANGLE)])], 1: [FakeBody([FakeFace(TRIANGLE)])]},
        )
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(entry["triangles"], 2)
        self.assertEqual(entry["tessellation_sources"], ["solid_body_faces", "sheet_body_faces"])

    def test_solid_part_exports_every_occurrence_body(self):
        component = FakeComponent(
            FakeDocument([]),
            {0: [FakeBody([FakeFace(TRIANGLE)]), FakeBody([FakeFace(TRIANGLE)])]},
        )
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(entry["triangles"], 2)
        self.assertEqual(entry["tessellation_sources"], ["solid_body_faces"])
        self.assertEqual(entry["bodies"], {"solid": 2, "sheet": 0})
        self.assertIn("GetTessTriangles", entry["used_api"])
        self.assertEqual(component.body_calls, [(0,), (1,)])

    def test_solid_geometry_does_not_need_document_tessellation(self):
        component = FakeComponent(FakeDocument([]), {0: [FakeBody([FakeFace(TRIANGLE)])], 1: []})
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(entry["tessellation_sources"], ["solid_body_faces"])
        self.assertEqual(read_stl(self.dest).triangles, 1)

    def test_unreadable_body_blocks_instead_of_being_omitted(self):
        component = FakeComponent(
            FakeDocument([]),
            {0: [FakeBody([FakeFace(TRIANGLE)]), FakeBody([FakeFace([])])]},
        )
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
        self.assertEqual(caught.exception.detail["body_index"], 1)
        self.assertFalse(self.dest.exists())

    def test_unreadable_occurrence_bodies_block_without_signature_retry(self):
        class UnreadableComponent(FakeComponent):
            def GetBodies2(self, *arguments):
                self.body_calls.append(arguments)
                raise RuntimeError("component interface unavailable")

        component = UnreadableComponent(FakeDocument(TRIANGLE), {})
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_component_bodies_unreadable")
        self.assertEqual(component.body_calls, [(0,)])
        self.assertFalse(self.dest.exists())

    def test_missing_face_blocks_even_when_other_faces_have_triangles(self):
        component = FakeComponent(FakeDocument([]), {0: [FakeBody([FakeFace(TRIANGLE), FakeFace([])])]})
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
        self.assertEqual(caught.exception.detail["face_index"], 1)
        self.assertFalse(self.dest.exists())

    def test_bodies_without_display_mesh_fail_instead_of_exporting_nothing(self):
        component = FakeComponent(FakeDocument([]), {0: [], 1: [FakeBody([FakeFace([])])]})
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
        self.assertFalse(self.dest.exists())

    def test_empty_part_fails(self):
        component = FakeComponent(FakeDocument([]), {0: [], 1: []})
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")

    def test_unknown_component_fails(self):
        component = FakeComponent(FakeDocument([]), {})
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_meshes({"missing-1": self.dest})
        self.assertEqual(caught.exception.code, "cad_missing_component")


class AxisReferenceTests(unittest.TestCase):
    def _component_with(self, face):
        return FakeComponent(FakeDocument([]), {0: [FakeBody([face])], 1: []})

    def test_cylinder_face_resolves_to_a_native_line(self):
        face = FakeFace(TRIANGLE, FakeSurface([0.01, 0.02, 0.03, 0.0, 0.0, 1.0, 0.005]))
        record = _backend(self._component_with(face)).capture_axis_reference({"component": "pcb-1", "face_index": 0})
        self.assertEqual(record["surface"], "cylinder")
        self.assertEqual(record["axis_point_m"], [0.01, 0.02, 0.03])
        self.assertEqual(record["axis_direction"], [0.0, 0.0, 1.0])
        self.assertEqual(record["radius_m"], 0.005)
        self.assertEqual(record["face_name"], "Face3")
        self.assertEqual(record["component"], "pcb-1")
        self.assertIn("CylinderParams", record["used_api"])

    def test_named_feature_uses_occurrence_faces_without_shared_document_reads(self):
        face = FakeFace(TRIANGLE, FakeSurface([0.01, 0.02, 0.03, 0.0, 0.0, 1.0, 0.005]))
        record = _backend(self._component_with(face)).capture_axis_reference(
            {"component": "pcb-1", "feature_name": "shaft"}
        )
        self.assertEqual(record["radius_m"], 0.005)
        self.assertEqual(record["axis_point_m"], [0.01, 0.02, 0.03])

    def test_named_feature_ambiguous_cylindrical_faces_block(self):
        face = FakeFace(TRIANGLE, FakeSurface([0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.005]))
        component = FakeComponent(FakeDocument([]), {0: [FakeBody([face, face])], 1: []})
        with self.assertRaises(CadError) as caught:
            _backend(component).capture_axis_reference({"component": "pcb-1", "feature_name": "shaft"})
        self.assertEqual(caught.exception.code, "cad_axis_reference_ambiguous")

    def test_non_cylindrical_face_fails(self):
        face = FakeFace(TRIANGLE, FakeSurface([0.0, 0.0, 0.0, 0.0, 0.0, 1.0]))
        with self.assertRaises(CadError) as caught:
            _backend(self._component_with(face)).capture_axis_reference({"component": "pcb-1", "face_index": 0})
        self.assertEqual(caught.exception.code, "cad_axis_reference_not_cylinder")

    def test_planar_garbage_params_are_not_accepted_as_a_cylinder(self):
        # SolidWorks answers CylinderParams for planar faces with garbage; the
        # geometry itself must prove it is a cylinder.
        face = FakeFace(TRIANGLE, FakeSurface([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 5.6e-315]))
        with self.assertRaises(CadError) as caught:
            _backend(self._component_with(face)).capture_axis_reference({"component": "pcb-1", "face_index": 0})
        self.assertEqual(caught.exception.code, "cad_axis_reference_not_cylinder")

    def test_face_index_out_of_range_fails(self):
        face = FakeFace(TRIANGLE, FakeSurface([0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.01]))
        with self.assertRaises(CadError) as caught:
            _backend(self._component_with(face)).capture_axis_reference({"component": "pcb-1", "face_index": 4})
        self.assertEqual(caught.exception.code, "cad_axis_reference_invalid")

    def test_missing_component_fails(self):
        face = FakeFace(TRIANGLE, FakeSurface([0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.01]))
        with self.assertRaises(CadError) as caught:
            _backend(self._component_with(face)).capture_axis_reference({"component": "ghost-1", "face_index": 0})
        self.assertEqual(caught.exception.code, "cad_missing_component")

    def test_component_body_query_uses_its_native_signature_once(self):
        face = FakeFace(TRIANGLE, FakeSurface([0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.006]))
        component = FakeComponent(FakeDocument([]), {0: [FakeBody([face])], 1: []})
        backend = _backend(component, "arm-1")
        record = backend.capture_axis_reference({"component": "arm-1", "face_index": 0})
        self.assertEqual(record["radius_m"], 0.006)
        self.assertIn("IComponent2.GetBodies2(type)", backend.notes["bodies_api:arm-1:0"])
        self.assertEqual(component.body_calls, [(0,)])


if __name__ == "__main__":
    unittest.main()
