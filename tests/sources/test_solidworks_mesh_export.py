"""Occurrence meshes preserve solids and sheets without shared document reads."""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.geometry.stl import read as read_stl  # noqa: E402
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    FACE_READ_RETRY_ATTEMPTS,
    SolidWorksBackend,
)

TRIANGLE = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


class FakeFace:
    def __init__(self, triangles, surface=None, feature="shaft", triangle_count=None, area=0.0):
        self._triangles = triangles
        self._surface = surface
        self.Name = "Face3"
        self._feature = feature
        # Raw native value types are preserved verbatim so tests can pin the
        # evidence-type rejection (a boolean is not an integer count or area).
        self._triangle_count = triangle_count if triangle_count is not None else len(list(triangles)) // 9
        self._area = area

    def GetTessTriangles(self, flag):  # noqa: N802 - SolidWorks API name
        return list(self._triangles)

    def GetSurface(self):  # noqa: N802 - SolidWorks API name
        return self._surface

    def GetFeature(self):  # noqa: N802 - SolidWorks API name
        return SimpleNamespace(Name=self._feature)

    def GetTessTriangleCount(self):  # noqa: N802 - SolidWorks API name
        return self._triangle_count

    def GetArea(self):  # noqa: N802 - SolidWorks API name
        return self._area


class FakeSurface:
    def __init__(self, params):
        self.CylinderParams = list(params)


class FakeBody:
    def __init__(self, faces):
        self._faces = faces

    def GetFaces(self):  # noqa: N802 - SolidWorks API name
        return list(self._faces)


class FakeTessellation:
    """ITessellation twin: NeedFaceFacetMap put, boolean Tessellate, facet count."""

    def __init__(self, facets=0, tessellate_result=True, raises=False):
        self._facets = int(facets)
        self.tessellate_result = tessellate_result
        self.raises = raises
        self.tessellated = False

    def Tessellate(self):  # noqa: N802 - SolidWorks API name
        if self.raises:
            raise RuntimeError("tessellation engine unavailable")
        self.tessellated = True
        return self.tessellate_result

    def GetFaceFacets(self, _face):  # noqa: N802 - SolidWorks API name
        return list(range(self._facets))


class FakeTessellationBody(FakeBody):
    def __init__(self, faces, tessellation):
        super().__init__(faces)
        self._tessellation = tessellation

    def GetTessellation(self):  # noqa: N802 - SolidWorks API name
        return self._tessellation


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

    def GetSuppression2(self):  # noqa: N802 - SolidWorks API name
        # Documented IComponent2 state; 2 = swComponentFullyResolved.
        return 2

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


class TransientComError(RuntimeError):
    """The observed SolidWorks mid-export transient (DISP_E_EXCEPTION)."""

    def __str__(self):
        return "com_error: (-2147417851, '\u670d\u52a1\u5668\u51fa\u73b0\u610f\u5916\u60c5\u51b5\u3002', None, None)"


class ServerUnavailableComError(RuntimeError):
    """The observed RPC_S_SERVER_UNAVAILABLE face read (native18/r8 face 51)."""

    def __str__(self):
        return "com_error: (-2147023174, 'RPC \u670d\u52a1\u5668\u4e0d\u53ef\u7528\u3002', None, None)"


class FlakyTessellation(FakeTessellation):
    def __init__(self, *, failures, facets=0, tessellate_result=True):
        super().__init__(facets=facets, tessellate_result=tessellate_result)
        self.failures = failures
        self.calls = 0

    def Tessellate(self):  # noqa: N802 - SolidWorks API name
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientComError()
        return super().Tessellate()


class FlakyFace(FakeFace):
    def __init__(self, values, *, failures, **kwargs):
        super().__init__(values, **kwargs)
        self.failures = failures
        self.calls = 0

    def GetTessTriangles(self, flag):  # noqa: N802 - SolidWorks API name
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientComError()
        return super().GetTessTriangles(flag)


def _silence_retry_sleep():
    """The face-read budget is pinned by attempt counts, not wall time."""

    return patch("description_pipeline.sources.solidworks.native.time.sleep", lambda _seconds: None)


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

    def test_export_emits_component_progress_and_survives_a_failing_callback(self):
        component = FakeComponent(
            FakeDocument([]),
            {0: [FakeBody([FakeFace(TRIANGLE)])], 1: []},
        )
        records = []
        with patch("description_pipeline.sources.solidworks.native._ACTIVITY_MIN_INTERVAL_SECONDS", 0.0):
            entry = _backend(component).export_component_meshes({"pcb-1": self.dest}, on_activity=records.append)[
                "pcb-1"
            ]
        self.assertEqual(entry["triangles"], 1)
        self.assertTrue(all(item["phase"] == "capture" for item in records))
        self.assertEqual(
            [(item["action"], item["current_object"], item["completed"], item["total"]) for item in records],
            [("mesh_export", "pcb-1", 0, 1), ("mesh_export", "pcb-1", 1, 1)],
        )

        def exploded(_record):
            raise RuntimeError("collector exploded")

        second = self.tmp / "mesh-boom.stl"
        again = FakeComponent(
            FakeDocument([]),
            {0: [FakeBody([FakeFace(TRIANGLE)])], 1: []},
        )
        entry = _backend(again).export_component_meshes({"pcb-1": second}, on_activity=exploded)["pcb-1"]
        self.assertEqual(entry["triangles"], 1)

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

    def _sheet_facetless_backend(self, face, tessellation):
        return _backend(
            FakeComponent(
                FakeDocument([]),
                {
                    0: [FakeBody([FakeFace(TRIANGLE)])],
                    1: [FakeTessellationBody([face], tessellation)],
                },
            )
        )

    def test_sheet_empty_face_with_native_facetless_evidence_is_recorded(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=True)
        backend = self._sheet_facetless_backend(
            FakeFace([], triangle_count=0, area=1.871564249915375e-13), tessellation
        )
        entry = backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(entry["triangles"], 1)
        self.assertEqual(read_stl(self.dest).triangles, 1)
        self.assertTrue(tessellation.tessellated)
        self.assertIs(getattr(tessellation, "NeedFaceFacetMap", None), True)
        self.assertEqual(entry["bodies"], {"solid": 1, "sheet": 1})
        tessellation_entry = entry["tessellation"]
        self.assertEqual(tessellation_entry["faces"], 2)
        self.assertEqual(
            tessellation_entry["no_facets_faces"],
            [
                {
                    "body_type": 1,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_count": 0,
                    "facet_count": 0,
                    "area_m2": 1.871564249915375e-13,
                    "tessellate_result": True,
                    "used_api": "IBody2.GetTessellation/ITessellation.Tessellate/GetFaceFacets",
                }
            ],
        )
        self.assertIn("mesh_tessellation:pcb-1", backend.notes)

    def test_sheet_empty_face_with_tessellate_refusal_is_recorded_not_failed(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=False)
        backend = self._sheet_facetless_backend(
            FakeFace([], triangle_count=0, area=7.486257000421631e-13), tessellation
        )
        entry = backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        row = entry["tessellation"]["no_facets_faces"][0]
        self.assertIs(row["tessellate_result"], False)
        self.assertEqual(row["area_m2"], 7.486257000421631e-13)

    def test_sheet_empty_face_with_positive_facets_fails_until_reconstruction_is_validated(self):
        tessellation = FakeTessellation(facets=1, tessellate_result=True)
        backend = self._sheet_facetless_backend(FakeFace([], triangle_count=0, area=1e-12), tessellation)
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
        self.assertEqual(caught.exception.detail["facetless_facets"], 1)
        self.assertFalse(self.dest.exists())

    def test_solid_empty_face_stays_fatal_without_sheet_qualification(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=True)
        component = FakeComponent(
            FakeDocument([]),
            {
                0: [FakeBody([FakeFace([], triangle_count=0, area=1e-12)])],
                1: [FakeTessellationBody([FakeFace(TRIANGLE)], tessellation)],
            },
        )
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
        self.assertEqual(caught.exception.detail["body_type"], 0)
        self.assertFalse(self.dest.exists())

    def test_sheet_empty_face_with_nonzero_native_count_fails(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=True)
        backend = self._sheet_facetless_backend(FakeFace([], triangle_count=2, area=1e-12), tessellation)
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.detail["triangle_count"], 2)

    def test_sheet_empty_face_with_nonfinite_area_fails(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=True)
        backend = self._sheet_facetless_backend(FakeFace([], triangle_count=0, area=float("nan")), tessellation)
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")

    def test_sheet_empty_face_with_unavailable_tessellation_fails(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=True, raises=True)
        backend = self._sheet_facetless_backend(FakeFace([], triangle_count=0, area=1e-12), tessellation)
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertTrue(caught.exception.detail["facetless_error"])

    def test_sheet_empty_face_with_non_boolean_tessellate_result_fails(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=None)
        backend = self._sheet_facetless_backend(FakeFace([], triangle_count=0, area=1e-12), tessellation)
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertTrue(caught.exception.detail["facetless_error"])

    def test_malformed_face_fails_with_classification_and_no_facetless_call(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=True)
        component = FakeComponent(
            FakeDocument([]),
            {
                0: [FakeBody([FakeFace(TRIANGLE)])],
                1: [FakeTessellationBody([FakeFace([0.0] * 6)], tessellation)],
            },
        )
        with self.assertRaises(CadError) as caught:
            _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.detail["classification"], "length_not_multiple_of_9")
        self.assertEqual(caught.exception.detail["raw_length"], 6)
        self.assertFalse(tessellation.tessellated)
        self.assertFalse(self.dest.exists())

    def test_all_no_facet_rows_travel_without_truncation(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=True)
        sheet_bodies = [
            FakeTessellationBody([FakeFace([], triangle_count=0, area=1e-13)], tessellation)
            for _ in range(55)
        ]
        component = FakeComponent(
            FakeDocument([]),
            {0: [FakeBody([FakeFace(TRIANGLE)])], 1: sheet_bodies},
        )
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        rows = entry["tessellation"]["no_facets_faces"]
        self.assertEqual(len(rows), 55)
        self.assertEqual([row["body_index"] for row in rows], list(range(55)))
        self.assertTrue(all(row["body_type"] == 1 and row["facet_count"] == 0 for row in rows))

    def test_boolean_native_triangle_count_is_not_accepted_as_zero(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=True)
        backend = self._sheet_facetless_backend(
            FakeFace([], triangle_count=False, area=1e-12), tessellation
        )
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
        self.assertFalse(self.dest.exists())

    def test_boolean_area_is_not_accepted_as_zero(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=True)
        backend = self._sheet_facetless_backend(
            FakeFace([], triangle_count=0, area=False), tessellation
        )
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
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


    def test_transient_face_read_is_retried_before_failure(self):
        face = FlakyFace(TRIANGLE, failures=1)
        component = FakeComponent(FakeDocument([]), {0: [FakeBody([face])], 1: []})
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(entry["triangles"], 1)
        self.assertEqual(face.calls, 2)

    def test_transient_facetless_probe_is_retried_and_warning_recorded(self):
        tessellation = FlakyTessellation(failures=1, facets=0, tessellate_result=True)
        face = FakeFace([], triangle_count=0, area=1.871564249915375e-13)
        backend = self._sheet_facetless_backend(face, tessellation)
        entry = backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        row = entry["tessellation"]["no_facets_faces"][0]
        self.assertEqual(row["tessellate_result"], True)
        self.assertEqual(row["area_m2"], 1.871564249915375e-13)
        self.assertEqual(tessellation.calls, 2)

    def test_persistent_transient_probe_on_degenerate_face_records_the_probe_error(self):
        for area in (1.871564249915375e-13, 1e-12):
            with self.subTest(area=area):
                dest = self.tmp / f"mesh-{area}.stl"
                tessellation = FlakyTessellation(failures=99, facets=0, tessellate_result=True)
                backend = self._sheet_facetless_backend(FakeFace([], triangle_count=0, area=area), tessellation)
                with _silence_retry_sleep():
                    entry = backend.export_component_meshes({"pcb-1": dest})["pcb-1"]
                row = entry["tessellation"]["no_facets_faces"][0]
                self.assertEqual(
                    set(row),
                    {
                        "body_type",
                        "body_index",
                        "face_index",
                        "triangle_count",
                        "area_m2",
                        "tessellate_probe_error",
                        "used_api",
                    },
                )
                self.assertEqual(row["triangle_count"], 0)
                self.assertEqual(row["area_m2"], area)
                self.assertIn("com_error", row["tessellate_probe_error"])
                self.assertIn("-2147417851", row["tessellate_probe_error"])
                self.assertEqual(row["used_api"], "IBody2.GetTessellation/ITessellation.Tessellate/GetFaceFacets")
                self.assertEqual(tessellation.calls, FACE_READ_RETRY_ATTEMPTS)
                self.assertEqual(entry["triangles"], 1)
                self.assertTrue(dest.exists())

    def test_persistent_transient_probe_on_non_degenerate_face_stays_fatal(self):
        tessellation = FlakyTessellation(failures=99, facets=0, tessellate_result=True)
        backend = self._sheet_facetless_backend(FakeFace([], triangle_count=0, area=1e-6), tessellation)
        with _silence_retry_sleep(), self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
        self.assertIn("com_error", caught.exception.detail["facetless_error"])
        self.assertEqual(tessellation.calls, FACE_READ_RETRY_ATTEMPTS)
        self.assertFalse(self.dest.exists())

    def test_transient_probe_error_with_weak_degeneracy_evidence_stays_fatal(self):
        cases = {
            "boolean-count": FakeFace([], triangle_count=True, area=1e-13),
            "boolean-area": FakeFace([], triangle_count=0, area=True),
        }
        for label, face in cases.items():
            with self.subTest(label=label):
                tessellation = FlakyTessellation(failures=99, facets=0, tessellate_result=True)
                backend = self._sheet_facetless_backend(face, tessellation)
                with _silence_retry_sleep(), self.assertRaises(CadError) as caught:
                    backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
                self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
                self.assertIn("com_error", caught.exception.detail["facetless_error"])
                self.assertFalse(self.dest.exists())

    def test_retry_helper_retries_only_the_transient_signature(self):
        from description_pipeline.sources.solidworks.native import (
            _is_transient_com_error,
            _retry_transient_com,
        )

        self.assertTrue(_is_transient_com_error(TransientComError()))
        self.assertFalse(_is_transient_com_error(RuntimeError("permanent")))
        calls = {"n": 0}

        def permanent() -> None:
            calls["n"] += 1
            raise RuntimeError("permanent")

        with self.assertRaises(RuntimeError):
            _retry_transient_com(permanent)
        self.assertEqual(calls["n"], 1)

    def test_rpc_server_unavailable_is_transient_and_retried(self):
        from description_pipeline.sources.solidworks.native import (
            TRANSIENT_COM_ERROR_CODES,
            _is_transient_com_error,
            _retry_transient_com,
        )

        self.assertIn("-2147023174", TRANSIENT_COM_ERROR_CODES)
        self.assertTrue(_is_transient_com_error(ServerUnavailableComError()))
        calls = {"n": 0}
        slept = []

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ServerUnavailableComError()
            return "face-values"

        result = _retry_transient_com(flaky, sleep=slept.append)
        self.assertEqual(result, "face-values")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(slept, [0.2])

    def test_face_read_budget_retries_transient_to_exhaustion_then_raises(self):
        from description_pipeline.sources.solidworks.native import (
            FACE_READ_RETRY_ATTEMPTS,
            FACE_READ_RETRY_DELAYS,
            _retry_transient_com,
        )

        calls = {"n": 0}
        slept = []

        def transient() -> None:
            calls["n"] += 1
            raise TransientComError()

        with self.assertRaises(TransientComError):
            _retry_transient_com(
                transient,
                attempts=FACE_READ_RETRY_ATTEMPTS,
                delays=FACE_READ_RETRY_DELAYS,
                sleep=slept.append,
            )
        self.assertEqual(calls["n"], FACE_READ_RETRY_ATTEMPTS)
        self.assertEqual(slept, list(FACE_READ_RETRY_DELAYS))
        self.assertGreaterEqual(FACE_READ_RETRY_ATTEMPTS, 6)
        self.assertLessEqual(FACE_READ_RETRY_ATTEMPTS, 8)
        self.assertLessEqual(max(FACE_READ_RETRY_DELAYS), 4.0)
        self.assertLessEqual(sum(FACE_READ_RETRY_DELAYS), 20.0)

    def test_face_read_budget_succeeds_on_the_fourth_attempt(self):
        from description_pipeline.sources.solidworks.native import (
            FACE_READ_RETRY_ATTEMPTS,
            FACE_READ_RETRY_DELAYS,
            _retry_transient_com,
        )

        calls = {"n": 0}
        slept = []

        def flaky():
            calls["n"] += 1
            if calls["n"] <= 3:
                raise TransientComError()
            return "face-values"

        result = _retry_transient_com(
            flaky,
            attempts=FACE_READ_RETRY_ATTEMPTS,
            delays=FACE_READ_RETRY_DELAYS,
            sleep=slept.append,
        )
        self.assertEqual(result, "face-values")
        self.assertEqual(calls["n"], 4)
        self.assertEqual(slept, list(FACE_READ_RETRY_DELAYS[:3]))

    def test_face_read_budget_does_not_retry_non_transient_errors(self):
        from description_pipeline.sources.solidworks.native import (
            FACE_READ_RETRY_ATTEMPTS,
            FACE_READ_RETRY_DELAYS,
            _retry_transient_com,
        )

        calls = {"n": 0}
        slept = []

        def permanent() -> None:
            calls["n"] += 1
            raise RuntimeError("permanent")

        with self.assertRaises(RuntimeError):
            _retry_transient_com(
                permanent,
                attempts=FACE_READ_RETRY_ATTEMPTS,
                delays=FACE_READ_RETRY_DELAYS,
                sleep=slept.append,
            )
        self.assertEqual(calls["n"], 1)
        self.assertEqual(slept, [])

    def test_default_retry_budget_stays_at_three_attempts(self):
        from description_pipeline.sources.solidworks.native import (
            TRANSIENT_COM_RETRY_ATTEMPTS,
            TRANSIENT_COM_RETRY_DELAYS,
            _retry_transient_com,
        )

        calls = {"n": 0}
        slept = []

        def transient() -> None:
            calls["n"] += 1
            raise TransientComError()

        self.assertEqual(TRANSIENT_COM_RETRY_ATTEMPTS, 3)
        self.assertEqual(TRANSIENT_COM_RETRY_DELAYS, (0.2, 0.5))
        with self.assertRaises(TransientComError):
            _retry_transient_com(transient, sleep=slept.append)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(slept, [0.2, 0.5])


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
        self.assertEqual(record["component"], "pcb-1")
        self.assertEqual(record["selector"], {"component": "pcb-1", "face_index": 0})
        self.assertEqual(record["face_index"], 0)
        self.assertNotIn("face_name", record)
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

class NoFullScanExportTests(unittest.TestCase):
    """The native boundary writes and receipts meshes without a full STL scan."""

    def _planar_stl(self):
        import struct

        header = b"planar fixture".ljust(80, b" ")
        triangles = [
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
        ]
        payload = b"".join(struct.pack("<12fH", 0.0, 0.0, 1.0, *a, *b, *c, 0) for a, b, c in triangles)
        return header + struct.pack("<I", len(triangles)) + payload

    def test_export_records_identity_and_header_count_without_reader(self):
        import hashlib
        import tempfile
        from pathlib import Path
        from unittest import mock

        from importlib import import_module

        freeze_module = import_module("description_pipeline.sources.solidworks.freeze")
        native_module = import_module("description_pipeline.sources.solidworks.native")

        self.assertFalse(hasattr(freeze_module, "read_stl"))
        self.assertFalse(hasattr(native_module, "read_stl"))

        payload = self._planar_stl()

        class Backend:
            def export_component_meshes(self, destinations, on_activity=None):
                entries = {}
                for component, destination in destinations.items():
                    Path(destination).write_bytes(payload)
                    entries[component] = {"component": component, "written": destination, "used_api": "fixture"}
                return entries

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("description_pipeline.geometry.stl.read", side_effect=AssertionError("full scan")):
                exported = freeze_module._export_geometry(
                    Backend(), {"assembly": "robot.SLDASM"}, root / "evidence" / "geometry", ["pcb-1"]
                )
        self.assertEqual(len(exported), 1)
        row = exported[0]
        self.assertEqual(set(row), {
            "component", "path", "triangles", "used_api", "sha256", "bodies", "tessellation",
        })
        self.assertEqual(row["triangles"], 2)
        self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())

