"""Geometry export must never drop a part's bodies.

SolidWorks answers ``IPartDoc.GetTessTriangles`` from *solid* bodies only, so a
sheet-body part (the PCB) once exported as "invalid tessellation".  These tests
drive the adapter with duck-typed COM objects: sheet bodies are tessellated per
face, mixed parts export both, and a part whose bodies yield no display mesh
fails instead of writing an empty file.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.geometry.stl import read as read_stl  # noqa: E402
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import SolidWorksBackend  # noqa: E402

TRIANGLE = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


class FakeFace:
    def __init__(self, triangles):
        self._triangles = triangles

    def GetTessTriangles(self, flag):  # noqa: N802 - SolidWorks API name
        return list(self._triangles)


class FakeBody:
    def __init__(self, faces):
        self._faces = faces

    def GetFaces(self):  # noqa: N802 - SolidWorks API name
        return list(self._faces)


class FakeDocument:
    def __init__(self, values):
        self._values = values

    def GetTessTriangles(self, flag):  # noqa: N802 - SolidWorks API name
        return list(self._values)


class FakeComponent:
    def __init__(self, document, bodies):
        self._document = document
        self._bodies = bodies

    def GetModelDoc2(self):  # noqa: N802 - SolidWorks API name
        return self._document

    def GetBodies2(self, body_type, visible_only):  # noqa: N802 - SolidWorks API name
        return list(self._bodies.get(body_type, ()))


def _backend(component) -> SolidWorksBackend:
    backend = object.__new__(SolidWorksBackend)
    backend._components = {"pcb-1": component}
    backend.notes = {}
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
        entry = _backend(component).export_component_mesh("pcb-1", self.dest)
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
        entry = _backend(component).export_component_mesh("pcb-1", self.dest)
        self.assertEqual(entry["triangles"], 2)
        self.assertEqual(entry["tessellation_sources"], ["part_document", "sheet_body_faces"])

    def test_solid_part_keeps_the_document_tessellation(self):
        component = FakeComponent(FakeDocument(list(TRIANGLE) * 2), {0: [FakeBody([FakeFace(TRIANGLE)])]})
        entry = _backend(component).export_component_mesh("pcb-1", self.dest)
        self.assertEqual(entry["triangles"], 2)
        self.assertEqual(entry["tessellation_sources"], ["part_document"])
        self.assertEqual(entry["bodies"], {"solid": 1, "sheet": 0})
        self.assertIn("GetTessTriangles", entry["used_api"])

    def test_solid_fallback_uses_body_faces_when_document_is_empty(self):
        component = FakeComponent(FakeDocument([]), {0: [FakeBody([FakeFace(TRIANGLE)])], 1: []})
        entry = _backend(component).export_component_mesh("pcb-1", self.dest)
        self.assertEqual(entry["tessellation_sources"], ["solid_body_faces"])
        self.assertEqual(read_stl(self.dest).triangles, 1)

    def test_bodies_without_display_mesh_fail_instead_of_exporting_nothing(self):
        component = FakeComponent(FakeDocument([]), {0: [], 1: [FakeBody([FakeFace([])])]})
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_mesh("pcb-1", self.dest)
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
        self.assertFalse(self.dest.exists())

    def test_empty_part_fails(self):
        component = FakeComponent(FakeDocument([]), {0: [], 1: []})
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_mesh("pcb-1", self.dest)
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")

    def test_unknown_component_fails(self):
        component = FakeComponent(FakeDocument([]), {})
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_mesh("missing-1", self.dest)
        self.assertEqual(caught.exception.code, "cad_missing_component")


if __name__ == "__main__":
    unittest.main()
