"""Occurrence meshes preserve solids and sheets without shared document reads."""

from __future__ import annotations

import copy
import hashlib
import shutil
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import _paths  # noqa: F401  (import side effect: sys.path)
from . import support

from description_pipeline.geometry.stl import read as read_stl  # noqa: E402
from description_pipeline.geometry.stl import vertices as stl_vertices  # noqa: E402
from description_pipeline.schemas import NATIVE_FACETLESS_EVIDENCE_API  # noqa: E402
from description_pipeline.sources.solidworks import native as native_module  # noqa: E402
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import SolidWorksBackend  # noqa: E402

TRIANGLE = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
REVERSED_TRIANGLE = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2.0, 0.0]
NATIVE_VT_BYREF_R8 = 0x4005
NATIVE_VT_BYREF_BOOL = 0x400B


def _packed_success_status():
    return struct.unpack("<d", struct.pack("<Q", 1))[0]


def _fake_whole_body_face_list(_faces, face_reference_sha256):
    return "VT_ARRAY|VT_DISPATCH", support.native_tessellation_request(
        face_reference_sha256
    )


class FakeCurve:
    def __init__(self, edge):
        self._edge = edge
        self.end_params_calls = []

    def GetEndParams(  # noqa: N802 - SolidWorks API name
        self,
        start,
        end,
        is_closed,
        is_periodic,
    ):
        self.end_params_calls.append((start, end, is_closed, is_periodic))
        for output, value_type, value in (
            (start, NATIVE_VT_BYREF_R8, 0.0),
            (end, NATIVE_VT_BYREF_R8, 1.0),
            (is_closed, NATIVE_VT_BYREF_BOOL, False),
            (is_periodic, NATIVE_VT_BYREF_BOOL, False),
        ):
            output.varianttype = value_type
            output.value = value
        return True


def _read_fake_end_params(curve):
    start = SimpleNamespace(varianttype=None, value=None)
    end = SimpleNamespace(varianttype=None, value=None)
    is_closed = SimpleNamespace(varianttype=None, value=None)
    is_periodic = SimpleNamespace(varianttype=None, value=None)
    success = curve.GetEndParams(start, end, is_closed, is_periodic)
    outputs = (start, end, is_closed, is_periodic)
    return {
        "call_form": "pywin32-byref-variant",
        "success": success,
        "error": None if success else "GetEndParams returned false",
        "start": start.value,
        "end": end.value,
        "is_closed": is_closed.value,
        "is_periodic": is_periodic.value,
        "raw_outputs": [
            {"value_type": output.varianttype, "value": output.value}
            for output in outputs
        ],
    }


class FakeVertex:
    def __init__(self, point, reference):
        self._point = list(point)
        self._persist_reference = reference

    def GetPoint(self):  # noqa: N802 - SolidWorks API name
        return list(self._point)


class FakeExtension:
    def __init__(self):
        self.calls = []
        self.references = {}

    def GetPersistReference3(self, value):  # noqa: N802 - SolidWorks API name
        self.calls.append(value)
        return list(
            self.references.setdefault(
                id(value),
                hashlib.sha256(f"mesh-fixture-{len(self.references) + 1}".encode()).digest(),
            )
        )


class FakeEdge:
    def __init__(self, reference, start, end):
        self._persist_reference = reference
        self._start = FakeVertex(start, reference + b"-start")
        self._end = FakeVertex(end, reference + b"-end")
        self._owner = None
        self._faces = []
        self._curve = FakeCurve(self)
        self.is_tolerant = False
        self.tolerant_gap_m = 0.0
        self.tolerance_calls = 0

    def GetStartVertex(self):  # noqa: N802 - SolidWorks API name
        return self._start

    def GetEndVertex(self):  # noqa: N802 - SolidWorks API name
        return self._end

    def GetBody(self):  # noqa: N802 - SolidWorks API name
        return self._owner

    def GetTwoAdjacentFaces2(self):  # noqa: N802 - SolidWorks API name
        return list(self._faces)

    def GetCurve(self):  # noqa: N802 - SolidWorks API name
        return self._curve

    def IsTolerant(self, tolerance):  # noqa: N802 - SolidWorks API name
        self.tolerance_calls += 1
        tolerance.value = self.tolerant_gap_m
        return self.is_tolerant

    def GetCurveParams3(self):  # noqa: N802 - SolidWorks API name
        return SimpleNamespace(
            UMinValue=0.0,
            UMaxValue=1.0,
            StartPoint=self._start._point,
            EndPoint=self._end._point,
            Sense=True,
            CurveType=0,
        )

    def EdgeInFaceSense(self, face):  # noqa: N802 - SolidWorks API name
        return face is self._faces[-1]

    def IEdgeInFaceSense2(self, face):  # noqa: N802 - SolidWorks API name
        return face is self._faces[-1]

    def _u(self, x, y, z):
        start = self._start._point
        end = self._end._point
        axis = [end[index] - start[index] for index in range(3)]
        length_squared = sum(value * value for value in axis)
        if length_squared == 0.0:
            return 0.0
        return sum(
            (point - origin) * direction
            for point, origin, direction in zip(
                (x, y, z),
                start,
                axis,
                strict=True,
            )
        ) / length_squared

    def GetClosestPointOn(self, x, y, z):  # noqa: N802 - SolidWorks API name
        parameter = self._u(x, y, z)
        start = self._start._point
        end = self._end._point
        point = [
            start[index] + parameter * (end[index] - start[index])
            for index in range(3)
        ]
        return [*point, parameter, 0.0]

    def GetParameter(self, x, y, z):  # noqa: N802 - SolidWorks API name
        return [self._u(x, y, z), True]

    def Evaluate2(self, parameter, _derivatives):  # noqa: N802 - SolidWorks API name
        start = self._start._point
        end = self._end._point
        return [
            *(
                start[index] + parameter * (end[index] - start[index])
                for index in range(3)
            ),
            0.0,
            0.0,
            0.0,
            _packed_success_status(),
        ]


class FakeFace:
    _next_reference = 1

    def __init__(
        self,
        triangles,
        surface=None,
        feature="shaft",
        triangle_count=None,
        area=0.0,
        edges=None,
        facet_triangles=None,
    ):
        self._triangles = triangles
        self._facet_triangles = (
            list(triangles)
            if facet_triangles is None
            else list(facet_triangles)
        )
        self._surface = surface
        self.Name = "Face3"
        self._feature = feature
        # Raw native value types are preserved verbatim so tests can pin the
        # evidence-type rejection (a boolean is not an integer count or area).
        self._triangle_count = (
            triangle_count
            if triangle_count is not None
            else len(self._facet_triangles) // 9
        )
        self._area = area
        self._edges = list(edges or ())
        self._owner = None
        self.triangle_calls = 0
        self._persist_reference = b"face-" + FakeFace._next_reference.to_bytes(8, "little")
        FakeFace._next_reference += 1

    def GetTessTriangles(self, flag):  # noqa: N802 - SolidWorks API name
        self.triangle_calls += 1
        return list(self._triangles)

    def GetSurface(self):  # noqa: N802 - SolidWorks API name
        return self._surface

    def GetFeature(self):  # noqa: N802 - SolidWorks API name
        return SimpleNamespace(Name=self._feature)

    def GetTessTriangleCount(self):  # noqa: N802 - SolidWorks API name
        return self._triangle_count

    def GetArea(self):  # noqa: N802 - SolidWorks API name
        return self._area

    def GetEdges(self):  # noqa: N802 - SolidWorks API name
        return list(self._edges)

    def GetBody(self):  # noqa: N802 - SolidWorks API name
        return self._owner


class FakeSurface:
    def __init__(self, params):
        self.CylinderParams = list(params)


class FakeBody:
    _next_reference = 1

    def __init__(self, faces, *, name: str | None = None):
        self._faces = faces
        self.Name = name or f"Body{FakeBody._next_reference}"
        self._persist_reference = b"body-" + FakeBody._next_reference.to_bytes(8, "little")
        self._tessellation = None
        self.tessellation_face_lists = []
        FakeBody._next_reference += 1

    def GetFaces(self):  # noqa: N802 - SolidWorks API name
        return list(self._faces)

    def GetEdges(self):  # noqa: N802 - SolidWorks API name
        edges = {}
        for face in self._faces:
            for edge in face._edges:
                edges[id(edge)] = edge
        return list(edges.values())

    def GetTessellation(self, face_list):  # noqa: N802 - SolidWorks API name
        self.tessellation_face_lists.append(face_list)
        if self._tessellation is None:
            self._tessellation = FakeTessellation()
        return self._tessellation


class FakeTessellation:
    """ITessellation twin for both explicit fault counts and captured body graphs."""

    def __init__(self, facets=None, tessellate_result=True, raises=False):
        self._facets = None if facets is None else int(facets)
        self.tessellate_result = tessellate_result
        self.raises = raises
        self.tessellated = False
        self._graph = None
        self._facets_by_face = {}
        self._facets_by_reference = {}
        self._fins_by_facet = {}
        self._fins_by_edge = {}
        self._fins_by_edge_reference = {}
        self._fin_vertices = {}
        self._vertex_points = {}
        self._cofins = {}
        self.requested = {}

    def __setattr__(self, name, value):
        if name in {
            "ImprovedQuality",
            "MatchType",
            "NeedErrorList",
            "NeedFaceFacetMap",
            "NeedEdgeFinMap",
            "NeedVertexParams",
        }:
            self.__dict__.setdefault("requested", {})[name] = value
        object.__setattr__(self, name, value)

    @property
    def automatic(self):
        return self._facets is None and self._graph is None

    def configure(self, tessellation, faces, edges):
        body = tessellation["itessellation"]["bodies"][0]
        self._graph = tessellation
        self._facets_by_face = {
            id(face): next(
                row["facet_ids"]
                for row in body["faces"]
                if row["face_index"] == face_index
            )
            for face_index, face in enumerate(faces)
        }
        self._facets_by_reference = {
            hashlib.sha256(face._persist_reference).hexdigest(): values
            for face, values in (
                (face, self._facets_by_face[id(face)]) for face in faces
            )
        }
        self._fins_by_facet = {
            row["facet_id"]: row["fin_ids"] for row in body["facets"]
        }
        edge_rows = {
            row["edge_reference_sha256"]: row
            for row in tessellation["native_edges"]
        }
        self._fins_by_edge = {
            id(edge): list(
                edge_rows[
                    hashlib.sha256(edge._persist_reference).hexdigest()
                ]["fin_ids"]
            )
            for edge in edges
        }
        self._fins_by_edge_reference = {
            hashlib.sha256(edge._persist_reference).hexdigest(): values
            for edge, values in (
                (edge, self._fins_by_edge[id(edge)]) for edge in edges
            )
        }
        fins = {row["fin_id"]: row for row in body["fins"]}
        self._fin_vertices = {
            fin_id: list(row["vertex_ids"]) for fin_id, row in fins.items()
        }
        self._vertex_points = {
            row["vertex_id"]: row["point_m"]
            for row in tessellation["itessellation"]["vertices"]
        }
        self._cofins = {
            fin_id: row["cofin_id"]
            for fin_id, row in fins.items()
            if row["cofin_id"] is not None
        }

    def Tessellate(self):  # noqa: N802 - SolidWorks API name
        if self.raises:
            raise RuntimeError("tessellation engine unavailable")
        self.tessellated = True
        return self.tessellate_result

    def GetFaceFacets(self, face):  # noqa: N802 - SolidWorks API name
        if self._graph is not None:
            values = self._facets_by_face.get(id(face))
            if values is None:
                reference = getattr(face, "_persist_reference", b"")
                values = self._facets_by_reference[
                    hashlib.sha256(reference).hexdigest()
                ]
            return list(values)
        return list(range(1, int(self._facets or 0) + 1))

    def GetFacetFins(self, facet_id):  # noqa: N802 - SolidWorks API name
        if self._graph is not None:
            return list(self._fins_by_facet[facet_id])
        first = facet_id * 10
        return [first, first + 1, first + 2]

    def GetEdgeFins(self, edge):  # noqa: N802 - SolidWorks API name
        values = self._fins_by_edge.get(id(edge))
        if values is None:
            reference = getattr(edge, "_persist_reference", b"")
            values = self._fins_by_edge_reference.get(
                hashlib.sha256(reference).hexdigest(),
                (),
            )
        return list(values)

    def GetFinVertices(self, fin_id):  # noqa: N802 - SolidWorks API name
        if self._graph is not None:
            return list(self._fin_vertices[fin_id])
        vertex = fin_id * 10
        return [vertex, vertex + 1]

    def GetVertexPoint(self, vertex_id):  # noqa: N802 - SolidWorks API name
        if self._graph is not None:
            return list(self._vertex_points[vertex_id])
        return [float(vertex_id), 0.0, 0.0]

    def GetFacetCount(self):  # noqa: N802 - SolidWorks API name
        if self._graph is not None:
            return len(self._graph["itessellation"]["bodies"][0]["facets"])
        return int(self._facets or 0)

    def GetFinCount(self):  # noqa: N802 - SolidWorks API name
        if self._graph is not None:
            return len(self._graph["itessellation"]["bodies"][0]["fins"])
        return int(self._facets or 0) * 3

    def GetVertexCount(self):  # noqa: N802 - SolidWorks API name
        if self._graph is not None:
            return len(self._graph["itessellation"]["vertices"])
        return int(self._facets or 0) * 3

    def GetFinCoFin(self, fin_id):  # noqa: N802 - SolidWorks API name
        return self._cofins[fin_id]


class FakeTessellationBody(FakeBody):
    def __init__(self, faces, tessellation, *, name: str | None = None):
        super().__init__(faces, name=name)
        self._tessellation = tessellation

    def GetTessellation(self, face_list):  # noqa: N802 - SolidWorks API name
        self.tessellation_face_lists.append(face_list)
        return self._tessellation


class FakeDocument:
    def __init__(self, values):
        self._values = values
        self._path = None
        self.IsOpenedReadOnly = True
        self.ConfigurationManager = SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name="Default"))

    def GetTessTriangles(self, flag):  # noqa: N802 - SolidWorks API name
        raise AssertionError("mesh export must not read the shared part document")

    def GetPathName(self):  # noqa: N802 - SolidWorks API name
        return self._path


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


class FlakyFinTessellation(FakeTessellation):
    def __init__(self, *, failures):
        super().__init__(facets=None, tessellate_result=True)
        self.failures = failures
        self.calls = 0

    def GetFinVertices(self, fin_id):  # noqa: N802 - SolidWorks API name
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientComError()
        return super().GetFinVertices(fin_id)


def _silence_retry_sleep():
    """The face-read budget is pinned by attempt counts, not wall time."""

    return patch("description_pipeline.sources.solidworks.native.time.sleep", lambda _seconds: None)


def _configure_fake_body(body, extension):
    faces = list(body._faces)
    edges = {}
    for face in faces:
        face._owner = body
        for edge in face._edges:
            edge._owner = body
            edges[id(edge)] = edge
    for edge in edges.values():
        edge._faces = [face for face in faces if edge in face._edges]

    triangles = []
    spans = []
    for face_index, face in enumerate(faces):
        values = list(face._facet_triangles)
        if len(values) % 9:
            face_triangles = []
        else:
            face_triangles = [
                tuple(
                    tuple(values[offset : offset + 3])
                    for offset in range(triangle_start, triangle_start + 9, 3)
                )
                for triangle_start in range(0, len(values), 9)
            ]
        triangle_start = len(triangles)
        triangles.extend(face_triangles)
        face_reference = hashlib.sha256(
            bytes(extension.GetPersistReference3(face))
        ).hexdigest()
        span_triangles = [
            point
            for triangle in face_triangles
            for point in triangle
        ]
        spans.append(
            {
                "body_type": 0,
                "body_index": 0,
                "body_name": body.Name,
                "body_reference_sha256": hashlib.sha256(
                    bytes(extension.GetPersistReference3(body))
                ).hexdigest(),
                "face_index": face_index,
                "face_reference_sha256": face_reference,
                "face_owner_id": face_index + 1,
                "edge_references": [],
                "triangle_start": triangle_start,
                "triangle_count": len(face_triangles),
                "triangle_records_sha256": hashlib.sha256(
                    b"".join(
                        struct.pack("<3f", *point)
                        for point in span_triangles
                    )
                ).hexdigest(),
            }
        )

    edge_rows = []
    for edge in edges.values():
        if len(edge._faces) != 2:
            continue
        reference = hashlib.sha256(edge._persist_reference).hexdigest()
        face_indices = sorted(faces.index(face) for face in edge._faces)
        for face_index in face_indices:
            spans[face_index]["edge_references"].append(reference)
        edge_rows.append(
            {
                "body_type": 0,
                "body_index": 0,
                "edge_reference_sha256": reference,
                "start_vertex_reference_sha256": hashlib.sha256(
                    edge._start._persist_reference
                ).hexdigest(),
                "end_vertex_reference_sha256": hashlib.sha256(
                    edge._end._persist_reference
                ).hexdigest(),
                "start_point_m": list(edge._start._point),
                "end_point_m": list(edge._end._point),
                "face_indices": face_indices,
            }
        )

    tessellation = body.GetTessellation(None)
    if tessellation.automatic and faces:
        graph = support.native_tessellation_fixture(
            spans,
            edge_rows,
            triangle_vertices=triangles,
        )
        tessellation.configure(graph, faces, list(edges.values()))


def _backend(component, name="pcb-1") -> SolidWorksBackend:
    backend = SolidWorksBackend()
    component.Name2 = name
    path = "C:/neutral/robot.SLDASM"
    extension = FakeExtension()
    component._document.Extension = extension
    component._document._path = component.GetPathName()
    doc = SimpleNamespace(
        ConfigurationManager=SimpleNamespace(
            ActiveConfiguration=SimpleNamespace(
                Name="Default",
                GetRootComponent3=lambda _resolve: SimpleNamespace(GetChildren=lambda: [component]),
            )
        ),
        IsOpenedReadOnly=True,
        GetPathName=lambda: path,
        Extension=extension,
    )
    part_path = component.GetPathName()
    component._document.Extension = extension
    backend._sessions["source"] = SimpleNamespace(
        app=SimpleNamespace(
            GetOpenDocumentByName=lambda requested: (
                component._document
                if requested == part_path
                else doc if requested == path else None
            )
        ),
        process=SimpleNamespace(alive=lambda: True),
        closed=False,
    )
    session = backend._sessions["source"]
    session.current_application = lambda: session.app
    backend._owner_thread = threading.current_thread()
    backend._scene_document_key = backend._record_source_document(path, "Default")
    part_key = backend._record_source_document(part_path, "Default")
    backend._source_components = {name: (part_key, "Default")}
    backend._components = {name}
    for bodies in component._bodies.values():
        for body in bodies:
            _configure_fake_body(body, extension)
    return backend


class ComponentMeshExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-mesh-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dest = self.tmp / "mesh.stl"
        if hasattr(native_module, "_read_curve_end_params"):
            end_params_patch = patch.object(
                native_module,
                "_read_curve_end_params",
                _read_fake_end_params,
            )
            end_params_patch.start()
            self.addCleanup(end_params_patch.stop)
        face_list_patch = patch.object(
            native_module,
            "_whole_body_face_list",
            _fake_whole_body_face_list,
        )
        face_list_patch.start()
        self.addCleanup(face_list_patch.stop)

    def test_sheet_only_part_exports_its_faces(self):
        body = FakeBody([FakeFace(TRIANGLE)])
        component = FakeComponent(
            FakeDocument([]),
            {0: [], 1: [body]},
        )
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(entry["triangles"], 1)
        self.assertEqual(entry["tessellation_sources"], ["sheet_itessellation"])
        self.assertEqual(entry["bodies"], {"solid": 0, "sheet": 1})
        self.assertIn("ITessellation", entry["used_api"])
        self.assertEqual(component.body_calls, [(0,), (1,), (1,)])
        self.assertEqual(
            body.tessellation_face_lists[-1],
            "VT_ARRAY|VT_DISPATCH",
        )
        self.assertIs(body._tessellation.requested["ImprovedQuality"], True)
        self.assertEqual(body._tessellation.requested["MatchType"], 0)
        self.assertIs(body._tessellation.requested["NeedErrorList"], True)
        self.assertEqual(
            entry["tessellation"]["itessellation"]["bodies"][0]["request"],
            support.native_tessellation_request(
                span["face_reference_sha256"]
                for span in entry["tessellation"]["face_spans"]
            ),
        )
        self.assertEqual(
            entry["tessellation"]["itessellation"]["bodies"][0]["properties"],
            support.NATIVE_TESSELLATION_PROPERTIES,
        )
        self.assertEqual(read_stl(self.dest).triangles, 1)

    def test_native_face_spans_carry_shared_edge_incidence(self):
        shared = FakeEdge(
            b"shared-native-edge",
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
        )
        component = FakeComponent(
            FakeDocument([]),
            {
                0: [],
                1: [
                    FakeBody(
                        [
                            FakeFace(TRIANGLE, edges=[shared]),
                            FakeFace(REVERSED_TRIANGLE, edges=[shared]),
                        ],
                        name="fixture-sheet",
                    )
                ],
            },
        )
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        spans = entry["tessellation"]["face_spans"]
        edges = entry["tessellation"]["native_edges"]
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["edge_references"], spans[1]["edge_references"])
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["face_indices"], [0, 1])
        self.assertEqual(edges[0]["start_point_m"], [0.0, 0.0, 0.0])
        self.assertEqual(edges[0]["end_point_m"], [1.0, 0.0, 0.0])

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
            [(item["action"], item.get("current_object"), item["completed"], item["total"]) for item in records],
            [("mesh_export", "pcb-1", 0, 1), ("mesh_export", "pcb-1", 1, 1), ("mesh_export", None, 1, 1)],
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

        # Under the 0.25 s repeat throttle the per-component notes may be dropped;
        # the forced final note must still leave the accepted activity at n/N.
        throttled = []
        third = self.tmp / "mesh-throttled.stl"
        final = FakeComponent(
            FakeDocument([]),
            {0: [FakeBody([FakeFace(TRIANGLE)])], 1: []},
        )
        entry = _backend(final).export_component_meshes({"pcb-1": third}, on_activity=throttled.append)["pcb-1"]
        self.assertEqual(entry["triangles"], 1)
        self.assertEqual((throttled[-1]["completed"], throttled[-1]["total"]), (1, 1))
        self.assertNotIn("current_object", throttled[-1])

    def test_mixed_part_exports_solids_and_sheets(self):
        component = FakeComponent(
            FakeDocument(list(TRIANGLE)),
            {0: [FakeBody([FakeFace(TRIANGLE)])], 1: [FakeBody([FakeFace(TRIANGLE)])]},
        )
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(entry["triangles"], 2)
        self.assertEqual(
            entry["tessellation_sources"],
            ["solid_itessellation", "sheet_itessellation"],
        )
        self.assertEqual(
            [span["triangle_start"] for span in entry["tessellation"]["face_spans"]],
            [0, 1],
        )

    def test_solid_part_exports_every_occurrence_body(self):
        component = FakeComponent(
            FakeDocument([]),
            {0: [FakeBody([FakeFace(TRIANGLE)]), FakeBody([FakeFace(TRIANGLE)])]},
        )
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(entry["triangles"], 2)
        self.assertEqual(entry["tessellation_sources"], ["solid_itessellation"])
        self.assertEqual(entry["bodies"], {"solid": 2, "sheet": 0})
        self.assertIn("ITessellation", entry["used_api"])
        self.assertNotIn("GetTessTriangles", entry["used_api"])
        self.assertEqual(component.body_calls, [(0,), (0,), (1,)])
        self.assertEqual(
            [span["triangle_start"] for span in entry["tessellation"]["face_spans"]],
            [0, 1],
        )
        self.assertEqual(
            [span["face_owner_id"] for span in entry["tessellation"]["face_spans"]],
            [1, 2],
        )

    def test_solid_geometry_does_not_need_document_tessellation(self):
        component = FakeComponent(FakeDocument([]), {0: [FakeBody([FakeFace(TRIANGLE)])], 1: []})
        entry = _backend(component).export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(entry["tessellation_sources"], ["solid_itessellation"])
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

    def test_bodies_without_native_facets_fail_instead_of_exporting_nothing(self):
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
                    "used_api": NATIVE_FACETLESS_EVIDENCE_API,
                    "source_rule": "native-flat-array",
                    "warnings": [],
                    "derived_empty_edges": [],
                }
            ],
        )
        self.assertIn("mesh_tessellation:pcb-1", backend.notes)

    def test_sheet_empty_face_with_tessellate_refusal_is_a_primary_failure(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=False)
        backend = self._sheet_facetless_backend(
            FakeFace([], triangle_count=0, area=7.486257000421631e-13), tessellation
        )
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(
            caught.exception.detail["api"],
            "ITessellation.Tessellate",
        )
        self.assertIs(caught.exception.detail["tessellate_result"], False)
        self.assertEqual(
            caught.exception.detail["face_diagnostics"][0]["area_m2"][
                "value"
            ],
            7.486257000421631e-13,
        )
        self.assertFalse(self.dest.exists())

    def test_sheet_empty_face_with_positive_facets_fails_until_reconstruction_is_validated(self):
        tessellation = FakeTessellation(facets=1, tessellate_result=True)
        backend = self._sheet_facetless_backend(FakeFace([], triangle_count=0, area=1e-12), tessellation)
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
        self.assertEqual(caught.exception.detail["native"]["facets"], 1)
        self.assertEqual(caught.exception.detail["observed"]["vertices"], 6)
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
        self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
        self.assertEqual(caught.exception.detail["api"], "ITessellation.Tessellate")
        self.assertIn("engine unavailable", caught.exception.detail["error"])

    def test_sheet_empty_face_with_non_boolean_tessellate_result_fails(self):
        tessellation = FakeTessellation(facets=0, tessellate_result=None)
        backend = self._sheet_facetless_backend(FakeFace([], triangle_count=0, area=1e-12), tessellation)
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
        self.assertEqual(caught.exception.detail["api"], "ITessellation.Tessellate")
        self.assertEqual(caught.exception.detail["returned"], "None")

    def test_malformed_display_array_is_not_a_graph_input(self):
        tessellation = FakeTessellation(facets=None, tessellate_result=True)
        component = FakeComponent(
            FakeDocument([]),
            {
                0: [FakeBody([FakeFace(TRIANGLE)])],
                1: [
                    FakeTessellationBody(
                        [FakeFace([0.0] * 6, facet_triangles=TRIANGLE)],
                        tessellation,
                    )
                ],
            },
        )
        entry = _backend(component).export_component_meshes(
            {"pcb-1": self.dest}
        )["pcb-1"]
        self.assertEqual(entry["triangles"], 2)
        self.assertEqual(entry["tessellation"]["no_facets_faces"], [])
        self.assertTrue(tessellation.tessellated)
        self.assertTrue(self.dest.exists())

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


    def test_display_triangle_read_is_not_part_of_graph_export(self):
        face = FakeFace(TRIANGLE, facet_triangles=REVERSED_TRIANGLE)
        component = FakeComponent(FakeDocument([]), {0: [FakeBody([face])], 1: []})
        entry = _backend(component).export_component_meshes(
            {"pcb-1": self.dest}
        )["pcb-1"]
        self.assertEqual(entry["triangles"], 1)
        self.assertEqual(face.triangle_calls, 0)
        self.assertEqual(
            stl_vertices(self.dest).tolist(),
            [
                [1.0, 0.0, 0.0],
                [0.0, 0.0, 0.0],
                [0.0, 2.0, 0.0],
            ],
        )

    def test_transient_itessellation_read_uses_face_retry_budget(self):
        tessellation = FlakyFinTessellation(failures=1)
        face = FakeFace(TRIANGLE)
        body = FakeTessellationBody([face], tessellation)
        component = FakeComponent(FakeDocument([]), {0: [body], 1: []})
        with _silence_retry_sleep():
            entry = _backend(component).export_component_meshes(
                {"pcb-1": self.dest}
            )["pcb-1"]
        self.assertEqual(entry["triangles"], 1)
        self.assertGreaterEqual(tessellation.calls, 2)
        self.assertEqual(face.triangle_calls, 0)

    def test_transient_body_tessellation_failure_is_explicit_unreadable(self):
        tessellation = FlakyTessellation(failures=1, facets=0, tessellate_result=True)
        face = FakeFace([], triangle_count=0, area=1.871564249915375e-13)
        backend = self._sheet_facetless_backend(face, tessellation)
        with self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
        self.assertEqual(caught.exception.detail["api"], "ITessellation.Tessellate")
        self.assertIn("com_error", caught.exception.detail["error"])
        self.assertEqual(tessellation.calls, 1)

    def test_persistent_transient_body_tessellation_is_explicit_unreadable(self):
        for area in (1.871564249915375e-13, 1e-12):
            with self.subTest(area=area):
                dest = self.tmp / f"mesh-{area}.stl"
                tessellation = FlakyTessellation(failures=99, facets=0, tessellate_result=True)
                backend = self._sheet_facetless_backend(FakeFace([], triangle_count=0, area=area), tessellation)
                with _silence_retry_sleep(), self.assertRaises(CadError) as caught:
                    backend.export_component_meshes({"pcb-1": dest})["pcb-1"]
                self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
                self.assertEqual(caught.exception.detail["api"], "ITessellation.Tessellate")
                self.assertIn("-2147417851", caught.exception.detail["error"])
                self.assertEqual(tessellation.calls, 1)
                self.assertFalse(dest.exists())

    def test_persistent_transient_probe_on_non_degenerate_face_stays_fatal(self):
        tessellation = FlakyTessellation(failures=99, facets=0, tessellate_result=True)
        backend = self._sheet_facetless_backend(FakeFace([], triangle_count=0, area=1e-6), tessellation)
        with _silence_retry_sleep(), self.assertRaises(CadError) as caught:
            backend.export_component_meshes({"pcb-1": self.dest})["pcb-1"]
        self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
        self.assertEqual(caught.exception.detail["api"], "ITessellation.Tessellate")
        self.assertIn("com_error", caught.exception.detail["error"])
        self.assertEqual(tessellation.calls, 1)
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
                self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
                self.assertEqual(caught.exception.detail["api"], "ITessellation.Tessellate")
                self.assertIn("com_error", caught.exception.detail["error"])
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

    def _planar_triangles(self):
        return [
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
        ]

    def test_export_records_identity_and_header_count_without_reader(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from importlib import import_module

        freeze_module = import_module("description_pipeline.sources.solidworks.freeze")
        native_module = import_module("description_pipeline.sources.solidworks.native")

        self.assertFalse(hasattr(freeze_module, "read_stl"))
        self.assertFalse(hasattr(native_module, "read_stl"))

        span = {
            "body_type": 0,
            "body_index": 0,
            "body_name": "fixture",
            "body_reference_sha256": "1" * 64,
            "face_index": 0,
            "face_reference_sha256": "2" * 64,
            "face_owner_id": 1,
            "edge_references": [],
            "triangle_start": 0,
            "triangle_count": 2,
            "triangle_records_sha256": "0" * 64,
        }
        tessellation = support.native_tessellation_fixture(
            [span],
            [],
            triangle_vertices=self._planar_triangles(),
        )
        payload = tessellation.stl_bytes

        class Backend:
            def export_component_meshes(self, destinations, on_activity=None):
                entries = {}
                for component, destination in destinations.items():
                    Path(destination).write_bytes(payload)
                    entries[component] = {
                        "component": component,
                        "written": destination,
                        "used_api": "fixture",
                        "bodies": {"solid": 1, "sheet": 0},
                        "tessellation": copy.deepcopy(tessellation),
                    }
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

    def test_export_rejects_five_key_v2_and_non_mapping_itessellation(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from importlib import import_module

        freeze_module = import_module("description_pipeline.sources.solidworks.freeze")
        triangles = self._planar_triangles()
        span = {
            "body_type": 0,
            "body_index": 0,
            "body_name": "fixture",
            "body_reference_sha256": "1" * 64,
            "face_index": 0,
            "face_reference_sha256": "2" * 64,
            "face_owner_id": 1,
            "edge_references": [],
            "triangle_start": 0,
            "triangle_count": 2,
            "triangle_records_sha256": "0" * 64,
        }
        valid = support.native_tessellation_fixture(
            [span],
            [],
            triangle_vertices=triangles,
        )
        payload = valid.stl_bytes
        five_key = {
            key: value
            for key, value in valid.items()
            if key != "itessellation"
        }
        legacy_v2 = {
            **valid,
            "schema_version": "solidworks-to-urdf.native-tessellation/v2",
        }
        non_mapping = {**valid, "itessellation": []}

        for label, tessellation in (
            ("five-key", five_key),
            ("v2", legacy_v2),
            ("non-mapping", non_mapping),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:

                def backend_for(value):
                    class Backend:
                        def export_component_meshes(self, destinations, on_activity=None):
                            entries = {}
                            for component, destination in destinations.items():
                                Path(destination).write_bytes(payload)
                                entries[component] = {
                                    "component": component,
                                    "written": destination,
                                    "used_api": "fixture",
                                    "bodies": {"solid": 1, "sheet": 0},
                                    "tessellation": copy.deepcopy(value),
                                }
                            return entries

                    return Backend()

                with (
                    mock.patch(
                        "description_pipeline.geometry.stl.read",
                        side_effect=AssertionError("full scan"),
                    ),
                    self.assertRaisesRegex(
                        freeze_module.BridgeError,
                        "complete face-span evidence",
                    ),
                ):
                    freeze_module._export_geometry(
                        backend_for(tessellation),
                        {"assembly": "robot.SLDASM"},
                        Path(tmp) / "evidence" / "geometry",
                        ["pcb-1"],
                    )

