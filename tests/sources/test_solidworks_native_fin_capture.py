"""Focused fake-COM tests for the direct native tessellation boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import struct
import sys
import tempfile
import unittest
from contextlib import nullcontext
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import _paths, support  # noqa: F401  (import side effect: sys.path)

from description_pipeline.geometry.native_tessellation import (  # noqa: E402
    _finite_fin_intervals,
    _valid_u_evidence,
    native_mapped_witnesses_are_global_unique,
    parse_native_tessellation,
)
from description_pipeline.sources.solidworks.native import (  # noqa: E402
    _NativeReferenceRegistry,
    _com_object_identity,
    _fin_edge_row_map,
    _group_vertex_observations,
    _scope_bijection,
    _scope_edge_rows,
    _scope_edge_mapping_key,
    _scope_face_rows,
    _scope_owner_key,
    _scope_vertex_rows,
    _typed_reference_sha256,
)
from description_pipeline.geometry.native_u import (  # noqa: E402
    NATIVE_COLLAPSED_U_FIN_RULE,
    NATIVE_SCOPE_MAPPING_METHOD,
    NATIVE_SCOPE_SOURCE_AUTHORITY,
    NATIVE_SCOPE_VERTEX_MAPPING_METHOD,
    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
    NATIVE_U_IDENTITY_RULE,
    native_binary32_spacing,
    native_curve_endpoint_roles,
    native_curve_domain_transform,
    native_curve_endpoint_operation_tolerance,
    native_evaluate2_evidence,
    native_evaluate2_success,
    native_float32_adjacency_bound,
    native_fin_interval,
    native_get_parameter_success,
    native_point_error,
    native_point_roundtrip_tolerance,
    native_scope_identity_sha256,
    native_u_equal,
    native_u_domain_is_resolvable,
    native_u_intervals_cover,
)
from description_pipeline.io import PipelineError  # noqa: E402
from description_pipeline.schemas import NATIVE_TESSELLATION_SCHEMA  # noqa: E402
import description_pipeline.sources.solidworks.native as native_module  # noqa: E402
from description_pipeline.sources.solidworks.errors import CadError  # noqa: E402
from description_pipeline.sources.solidworks.native import SolidWorksBackend  # noqa: E402

POINTS = {
    1: (0.0, 0.0, 0.0),
    2: (1.0, 0.0, 0.0),
    3: (0.0, 1.0, 0.0),
    4: (1.0, 1.0, 0.0),
}


def packed_evaluate_status(value):
    return struct.unpack("<d", struct.pack("<Q", value))[0]


class FakeVariant:
    def __init__(self, varianttype, value):
        self.varianttype = varianttype
        self.value = value


def whole_body_request(face_references):
    references = tuple(face_references)
    return {
        "api": "IBody2.GetTessellation",
        "argument_count": 1,
        "face_list": {
            "scope": "whole_body",
            "variant_type": "VT_ARRAY|VT_DISPATCH",
            "face_count": len(references),
            "first_face_reference_sha256": references[0],
            "last_face_reference_sha256": references[-1],
        },
    }


def fake_whole_body_face_list(_faces, face_reference_sha256):
    return "VT_ARRAY|VT_DISPATCH", whole_body_request(face_reference_sha256)


def fake_read_end_params(curve):
    start = FakeVariant(0x4005, 0.0)
    end = FakeVariant(0x4005, 0.0)
    is_closed = FakeVariant(0x400B, False)
    is_periodic = FakeVariant(0x400B, False)
    try:
        success = curve.GetEndParams(start, end, is_closed, is_periodic)
    except Exception as error:
        return {
            "call_form": "pywin32-byref-variant",
            "success": False,
            "error": f"{type(error).__name__}: {error}",
            "start": None,
            "end": None,
            "is_closed": None,
            "is_periodic": None,
            "raw_outputs": [],
        }
    outputs = [start, end, is_closed, is_periodic]
    result = {
        "call_form": "pywin32-byref-variant",
        "success": success,
        "error": None,
        "start": None,
        "end": None,
        "is_closed": None,
        "is_periodic": None,
        "raw_outputs": [
            {"value_type": item.varianttype, "value": item.value}
            for item in outputs
        ],
    }
    if success is not True:
        result["error"] = "GetEndParams returned false"
        return result
    result.update(
        {
            "start": float(start.value),
            "end": float(end.value),
            "is_closed": is_closed.value,
            "is_periodic": is_periodic.value,
        }
    )
    return result


class FakeExtension:
    def __init__(self):
        self.calls = []
        self.references = {}

    def GetPersistReference3(self, value):  # noqa: N802 - SolidWorks API name
        self.calls.append(value)
        return list(
            self.references.setdefault(
                id(value),
                hashlib.sha256(f"native-{len(self.references) + 1}".encode()).digest(),
            )
        )


class FakeFace:
    def __init__(self, display_triangles, facet_id, owner):
        self.display_triangles = display_triangles
        self.facet_id = facet_id
        self.owner = owner
        self.body_calls = 0
        self.display_calls = 0

    def GetTessTriangles(self, _flag):  # noqa: N802 - SolidWorks API name
        self.display_calls += 1
        return list(self.display_triangles)

    def GetArea(self):  # noqa: N802 - SolidWorks API name
        points = [
            self.display_triangles[offset : offset + 3]
            for offset in range(0, len(self.display_triangles), 3)
        ]
        area = 0.0
        for start, end in pairwise([*points, points[0]]):
            area += (
                start[0] * end[1]
                - end[0] * start[1]
            ) / 2.0
        return abs(area)

    def GetBody(self):  # noqa: N802 - SolidWorks API name
        self.body_calls += 1
        return self.owner


class FakeEdge:
    def __init__(
        self,
        owner,
        faces,
        vertices,
        *,
        u_min=0.0,
        u_max=1.0,
        closest_offset=0.0,
    ):
        self.owner = owner
        self.faces = faces
        self.vertices = vertices
        self.body_calls = 0
        self.closest_calls = 0
        self.parameter_calls = 0
        self.evaluate_calls = 0
        self.sense_calls = 0
        self.u_min = u_min
        self.u_max = u_max
        self.curve_start_point = [0.0, 0.0, 0.0]
        self.curve_end_point = [1.0, 0.0, 0.0]
        self.closest_offset = closest_offset
        self.closest_not_used = 0.0
        self.evaluate_status_double = packed_evaluate_status(1)
        self.parameter_success = True
        self.sense = True
        self.evaluate_points = []
        self.evaluate_raw = []
        self.evaluate_u_points = {}
        self.curve = FakeCurve(u_min, u_max)

    def GetBody(self):  # noqa: N802 - SolidWorks API name
        self.body_calls += 1
        return self.owner

    def GetStartVertex(self):  # noqa: N802 - SolidWorks API name
        return self.vertices[0]

    def GetEndVertex(self):  # noqa: N802 - SolidWorks API name
        return self.vertices[1]

    def GetTwoAdjacentFaces2(self):  # noqa: N802 - SolidWorks API name
        return list(self.faces)

    def GetCurve(self):  # noqa: N802 - SolidWorks API name
        return self.curve

    def GetCurveParams3(self):  # noqa: N802 - SolidWorks API name
        closed = self.curve.is_closed
        return SimpleNamespace(
            UMinValue=self.u_min,
            UMaxValue=self.u_max,
            StartPoint=list(self.curve_start_point),
            EndPoint=(
                list(self.curve_start_point)
                if closed
                else list(self.curve_end_point)
            ),
            Sense=self.sense,
            CurveType=0,
        )

    def EdgeInFaceSense(self, face):  # noqa: N802 - SolidWorks API name
        self.sense_calls += 1
        return face is self.faces[-1]

    def IEdgeInFaceSense2(self, face):  # noqa: N802 - SolidWorks API name
        self.sense_calls += 1
        return face is self.faces[-1]

    def GetClosestPointOn(self, x, y, z):  # noqa: N802 - SolidWorks API name
        self.closest_calls += 1
        return [
            x + self.closest_offset,
            y,
            z,
            x,
            self.closest_not_used,
        ]

    def GetParameter(self, x, _y, _z):  # noqa: N802 - SolidWorks API name
        self.parameter_calls += 1
        return [x, self.parameter_success]

    def Evaluate2(self, u_parameter, _derivative):  # noqa: N802
        self.evaluate_calls += 1
        if self.evaluate_raw:
            return self.evaluate_raw.pop(0)
        if self.evaluate_points:
            point = self.evaluate_points.pop(0)
            return [
                *point,
                0.0,
                0.0,
                0.0,
                self.evaluate_status_double,
            ]
        if u_parameter in self.evaluate_u_points:
            return [
                *self.evaluate_u_points[u_parameter],
                0.0,
                0.0,
                0.0,
                self.evaluate_status_double,
            ]
        return [
            u_parameter,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            self.evaluate_status_double,
        ]


class FakeVertex:
    def __init__(self, point, *, error=None):
        self.point = point
        self.error = error

    def GetPoint(self):  # noqa: N802 - SolidWorks API name
        if self.error is not None:
            raise RuntimeError(self.error)
        return list(self.point)


class FakeCurve:
    def __init__(
        self,
        u_min=0.0,
        u_max=1.0,
        *,
        is_closed=False,
        is_periodic=False,
        error=None,
    ):
        self.u_min = u_min
        self.u_max = u_max
        self.is_closed = is_closed
        self.is_periodic = is_periodic
        self.error = error
        self.end_params_arguments = None

    def GetEndParams(  # noqa: N802 - SolidWorks API name
        self,
        start,
        end,
        is_closed,
        is_periodic,
    ):
        if self.error is not None:
            raise RuntimeError(self.error)
        self.end_params_arguments = (start, end, is_closed, is_periodic)
        start.value = self.u_min
        end.value = self.u_max
        is_closed.value = self.is_closed
        is_periodic.value = self.is_periodic
        return True


class FakeTessellation:
    _option_names = frozenset(
        {
            "MatchType",
            "NeedFaceFacetMap",
            "NeedEdgeFinMap",
            "NeedVertexParams",
        }
    )

    def __init__(self, face_facets, *, transient_failures=None):
        self.face_facets = face_facets
        self.transient_remaining = dict(transient_failures or {})
        self.call_counts = {}
        self.tessellate_calls = 0
        self.edge_fin_calls = []
        self.face_facet_calls = []
        self.facet_fin_calls = []
        self.fin_vertex_calls = []
        self.vertex_point_calls = []
        self.native_count_calls = []
        self.cofin_calls = []
        self.cofin_values = {100: 200, 200: 100}
        self.native_totals = {"GetFacetCount": 2, "GetFinCount": 6, "GetVertexCount": 4}
        self.requested = {}
        self.property_readbacks = {}
        self.property_writes = []
        self.property_reads = []
        self._property_values = {}

    def __setattr__(self, name, value):
        if name in self._option_names:
            self.__dict__.setdefault("requested", {})[name] = value
            self.__dict__.setdefault("property_writes", []).append(name)
            self.__dict__.setdefault("_property_values", {})[name] = value
            return
        object.__setattr__(self, name, value)

    def __getattr__(self, name):
        values = self.__dict__.get("_property_values", {})
        if name in values:
            self.__dict__.setdefault("property_reads", []).append(name)
            return self.__dict__.get("property_readbacks", {}).get(
                name,
                values[name],
            )
        raise AttributeError(name)

    def _observe(self, api):
        self.call_counts[api] = self.call_counts.get(api, 0) + 1
        if self.transient_remaining.get(api, 0):
            self.transient_remaining[api] -= 1
            raise RuntimeError(
                "com_error: (-2147417851, 'transient', None, None)"
            )

    def Tessellate(self):  # noqa: N802 - SolidWorks API name
        self._observe("Tessellate")
        self.tessellate_calls += 1
        return True

    def GetFaceFacets(self, face):  # noqa: N802 - SolidWorks API name
        self._observe("GetFaceFacets")
        self.face_facet_calls.append(face)
        value = self.face_facets[id(face)]
        return list(value) if isinstance(value, (list, tuple)) else [value]

    def GetFacetFins(self, facet_id):  # noqa: N802 - SolidWorks API name
        self._observe("GetFacetFins")
        self.facet_fin_calls.append(facet_id)
        return {
            10: [100, 101, 102],
            11: [103, 104, 105],
            20: [200, 201, 202],
        }[facet_id]

    def GetEdgeFins(self, edge):  # noqa: N802 - SolidWorks API name
        self._observe("GetEdgeFins")
        self.edge_fin_calls.append(edge)
        return [100, 200]

    def GetFinVertices(self, fin_id):  # noqa: N802 - SolidWorks API name
        self._observe("GetFinVertices")
        self.fin_vertex_calls.append(fin_id)
        return {
            100: [1, 2],
            101: [2, 3],
            102: [3, 1],
            103: [1, 2],
            104: [2, 3],
            105: [3, 1],
            200: [2, 1],
            201: [1, 4],
            202: [4, 2],
        }[fin_id]

    def GetVertexPoint(self, vertex_id):  # noqa: N802 - SolidWorks API name
        self._observe("GetVertexPoint")
        self.vertex_point_calls.append(vertex_id)
        return POINTS[vertex_id]

    def GetFacetCount(self):  # noqa: N802 - SolidWorks API name
        self._observe("GetFacetCount")
        self.native_count_calls.append("GetFacetCount")
        return self.native_totals["GetFacetCount"]

    def GetFinCount(self):  # noqa: N802 - SolidWorks API name
        self._observe("GetFinCount")
        self.native_count_calls.append("GetFinCount")
        return self.native_totals["GetFinCount"]

    def GetVertexCount(self):  # noqa: N802 - SolidWorks API name
        self._observe("GetVertexCount")
        self.native_count_calls.append("GetVertexCount")
        return self.native_totals["GetVertexCount"]

    def GetFinCoFin(self, fin_id):  # noqa: N802 - SolidWorks API name
        self._observe("GetFinCoFin")
        self.cofin_calls.append(fin_id)
        return self.cofin_values[fin_id]


class FakeBody:
    def __init__(self, faces, edge, tessellation):
        self.faces = faces
        self.edge = edge
        self.tessellation = tessellation
        self.tessellation_calls = 0
        self.tessellation_face_lists = []
        self.Name = "fixture-body"

    def GetFaces(self):  # noqa: N802 - SolidWorks API name
        return list(self.faces)

    def GetEdges(self):  # noqa: N802 - SolidWorks API name
        return [self.edge]

    def GetTessellation(self, face_list):  # noqa: N802 - SolidWorks API name
        self.tessellation_calls += 1
        self.tessellation_face_lists.append(face_list)
        return self.tessellation


class FakeHolder:
    def __init__(self, body, body_type=0):
        self.body = body
        self.body_type = body_type

    def GetBodies2(self, body_type):  # noqa: N802 - SolidWorks API name
        return [self.body] if body_type == self.body_type else []


class NativeFinCaptureTests(unittest.TestCase):
    def setUp(self):
        self.production_whole_body_face_list = (
            native_module._whole_body_face_list
        )
        helper_patch = patch.object(
            native_module,
            "_whole_body_face_list",
            fake_whole_body_face_list,
        )
        helper_patch.start()
        self.addCleanup(helper_patch.stop)

    def fixture(self):
        body = FakeBody([], None, None)
        face_10 = FakeFace(
            [
                0.0,
                0.0,
                0.0,
                1.0 + 1e-10,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
            ],
            10,
            body,
        )
        face_20 = FakeFace(
            [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0],
            20,
            body,
        )
        tessellation = FakeTessellation({id(face_10): 10, id(face_20): 20})
        edge = FakeEdge(
            body,
            [face_10, face_20],
            [FakeVertex((0.0, 0.0, 0.0)), FakeVertex((1.0, 0.0, 0.0))],
        )
        body.faces = [face_10, face_20]
        body.edge = edge
        body.tessellation = tessellation
        return body, face_10, face_20, edge, tessellation, FakeExtension()

    def capture(self, body, extension, *, body_type=0):
        return SolidWorksBackend()._capture_body_tessellation(
            body,
            extension,
            component="part-1",
            body_type=body_type,
            body_index=0,
            read_end_params=fake_read_end_params,
            scope_context={
                "occurrence": "part-1",
                "source_document_path": "C:/fixture.SLDPRT",
                "source_document_sha256": "a" * 64,
                "source_document_identity_sha256": "a" * 64,
                "source_document_identity_kind": "file_sha256",
                "active_configuration": "Default",
                "referenced_configuration": "Default",
            },
        )

    def open_boundary_fixture(self):
        body, face_10, _face_20, edge, tessellation, extension = self.fixture()
        body.faces = [face_10]
        edge.faces = [face_10]
        tessellation.face_facets = {id(face_10): 10}
        tessellation.native_totals = {
            "GetFacetCount": 1,
            "GetFinCount": 3,
            "GetVertexCount": 3,
        }
        tessellation.cofin_values = {100: -1}
        tessellation.GetEdgeFins = lambda _edge: [100]
        tessellation.GetVertexPoint = lambda vertex_id: {
            1: (0.0, 0.0, 0.0),
            2: (1.0, 0.0, 0.0),
            3: (0.0, 1.0, 0.0),
        }[vertex_id]
        return body, face_10, edge, tessellation, extension

    def configure_group_333(self, edge, tessellation, *, evaluate_start=None):
        start = (
            -0.005322370050934642,
            0.00013700629315608413,
            -0.0021180644098793633,
        )
        end = (
            -0.005322370560299019,
            0.00013700674222233032,
            0.0013000000000000002,
        )
        evaluated_start = (
            -0.005322370560299019,
            0.00013700509113409066,
            -0.0021180644098793633,
        )
        evaluated_end = (
            -0.005322370560299019,
            0.00013700509113409066,
            0.0013,
        )
        edge.u_min = 8.193559012063638e-05
        edge.u_max = 0.0034999999999999996
        edge.curve = FakeCurve(-10.0, 10.0, is_closed=False, is_periodic=False)
        edge.parameter_success = packed_evaluate_status(1)
        edge.curve_start_point = list(start)
        edge.curve_end_point = list(end)
        edge.vertices = [FakeVertex(start), FakeVertex(end)]
        tessellation.GetVertexPoint = lambda vertex_id: {
            1: evaluated_start if evaluate_start is None else evaluate_start,
            2: evaluated_end,
            3: (0.0, 1.0, 0.0),
            4: (1.0, 1.0, 0.0),
        }[vertex_id]
        u_by_point = {
            (evaluated_start if evaluate_start is None else evaluate_start): edge.u_min,
            evaluated_end: edge.u_max,
        }
        edge.GetClosestPointOn = lambda x, y, z: [
            x,
            y,
            z,
            u_by_point[(x, y, z)],
            0.0,
        ]
        edge.GetParameter = lambda x, y, z: [u_by_point[(x, y, z)], True]
        edge.evaluate_points = [
            evaluated_start if evaluate_start is None else evaluate_start,
            evaluated_end,
        ]
        edge.evaluate_u_points = {
            edge.u_min: evaluated_start if evaluate_start is None else evaluate_start,
            edge.u_max: evaluated_end,
        }
        return start, end, evaluated_start, evaluated_end

    def group_333_tessellation(self, *, topology_start=None):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        self.configure_group_333(edge, tessellation)
        if topology_start is not None:
            edge.vertices[0] = FakeVertex(topology_start)
        backend = SolidWorksBackend()
        backend._source_components = {"part-1": (object(), "Default")}
        backend._captured_document = lambda *_args, **_kwargs: SimpleNamespace(
            Extension=extension
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            native_module,
            "_read_curve_end_params",
            fake_read_end_params,
        ):
            destination = Path(directory) / "mesh.stl"
            entry = backend._write_component_mesh(
                "part-1",
                destination,
                FakeHolder(body),
            )
            triangles = self.stl_triangles(destination)
        return entry["tessellation"], triangles

    @staticmethod
    def collapsed_u_tessellation():
        def binary32(point):
            return struct.unpack("<3f", struct.pack("<3f", *point))

        u_min = 2.667937521368035e-7
        u_max = 0.0005305172218399963
        start = binary32((0.0, 0.0, 0.0))
        near = binary32((9.489442548454158e-6, 0.0, 0.0))
        end = binary32((20e-6, 0.0, 0.0))
        apex_a = binary32((10e-6, -1.0, 0.0))
        apex_b = binary32((10e-6, 1.0, 0.0))
        triangles = [
            (start, near, apex_a),
            (near, end, apex_a),
            (near, start, apex_b),
            (end, near, apex_b),
        ]
        reference = hashlib.sha256(b"collapsed-native-edge").hexdigest()
        spans = []
        for face_index in range(2):
            spans.append(
                {
                    "body_type": 0,
                    "body_index": 0,
                    "body_name": "collapsed-body",
                    "body_reference_sha256": hashlib.sha256(
                        b"collapsed-body"
                    ).hexdigest(),
                    "face_index": face_index,
                    "face_reference_sha256": hashlib.sha256(
                        f"collapsed-face-{face_index}".encode()
                    ).hexdigest(),
                    "face_owner_id": face_index + 1,
                    "edge_references": [reference],
                    "triangle_start": face_index * 2,
                    "triangle_count": 2,
                    "triangle_records_sha256": "0" * 64,
                }
            )
        edge = {
            "body_type": 0,
            "body_index": 0,
            "edge_reference_sha256": reference,
            "start_vertex_reference_sha256": hashlib.sha256(
                struct.pack("<3f", *start)
            ).hexdigest(),
            "end_vertex_reference_sha256": hashlib.sha256(
                struct.pack("<3f", *end)
            ).hexdigest(),
            "start_point_m": list(start),
            "end_point_m": list(end),
            "face_indices": [0, 1],
            "u_min": u_min,
            "u_max": u_max,
            "curve_sense": True,
            "curve_type": 0,
        }
        evidence = support.native_tessellation_fixture(
            spans,
            [edge],
            triangle_vertices=triangles,
        )
        body = evidence["itessellation"]["bodies"][0]
        edge_row = evidence["native_edges"][0]
        vertex_points = {
            row["vertex_id"]: tuple(row["point_m"])
            for row in evidence["itessellation"]["vertices"]
        }
        fins = {row["fin_id"]: row for row in body["fins"]}

        def bind(evidence_row, point, u_parameter, *, fallback):
            evidence_row.clear()
            evidence_row.update(
                support._u_evidence(
                    u_parameter,
                    point,
                    domain_lower=u_min,
                    domain_upper=u_max,
                    curve_start=start,
                    curve_end=end,
                )
            )
            if not fallback:
                return
            evaluated = start
            error = native_point_error(point, evaluated)
            evidence_row.update(
                {
                    "u_parameter_rule": "native-u-closest-point-fallback-v1",
                    "parameter_u_source": "closest_point_fallback",
                    "fallback_reason": "get_parameter_false",
                    "point_roundtrip_rule": (
                        "tessellation-edge-projection-v1"
                    ),
                    "point_tolerance_m": (
                        NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
                    ),
                    "parameter_raw": [u_parameter, 0.0],
                    "parameter_success_raw": 0.0,
                    "parameter_success_raw_type": "float",
                    "parameter_success_bit": 0,
                    "parameter_success_parsed": False,
                    "evaluate_point_error_m": error,
                    "source_evaluate_error_m": error,
                    "closest_evaluate_error_m": error,
                }
            )
            evidence_row["evaluate_payload"][:3] = list(evaluated)

        u_by_point = {start: u_min, near: u_min, end: u_max}
        for fin_id in edge_row["fin_ids"]:
            fin = fins[fin_id]
            start_point = vertex_points[fin["vertex_ids"][0]]
            end_point = vertex_points[fin["vertex_ids"][1]]
            fin["start_u"] = u_by_point[start_point]
            fin["end_u"] = u_by_point[end_point]
            bind(
                fin["start_evidence"],
                start_point,
                fin["start_u"],
                fallback=start_point == near,
            )
            bind(
                fin["end_evidence"],
                end_point,
                fin["end_u"],
                fallback=end_point == near,
            )
        support.refresh_topology_endpoint_assignment(evidence)
        return evidence, triangles, edge_row, start, near, end, bind

    @staticmethod
    def next_after(value, count):
        for _ in range(count):
            value = math.nextafter(value, math.inf)
        return value

    def assert_directed_cycle(self, actual, expected):
        actual = tuple(tuple(float(value) for value in point) for point in actual)
        expected = tuple(
            tuple(float(value) for value in point) for point in expected
        )
        rotations = {
            expected[offset:] + expected[:offset]
            for offset in range(3)
        }
        self.assertIn(actual, rotations)

    @staticmethod
    def stl_triangles(path):
        payload = Path(path).read_bytes()
        count = struct.unpack_from("<I", payload, 80)[0]
        return [
            tuple(
                struct.unpack_from(
                    "<3f",
                    payload,
                    84 + 50 * triangle_index + 12 + 12 * point_index,
                )
                for point_index in range(3)
            )
            for triangle_index in range(count)
        ]

    def test_native_u_identity_accepts_128_and_rejects_129_ulps(self):
        accepted = self.next_after(1.0, 128)
        rejected = self.next_after(1.0, 129)

        self.assertTrue(native_u_equal(1.0, accepted))
        self.assertFalse(native_u_equal(1.0, rejected))

    def test_native_u_coverage_accepts_128_and_rejects_129_ulp_gaps(self):
        covered = 1.0 - 128 * math.ulp(1.0)
        uncovered = 1.0 - 129 * math.ulp(1.0)

        self.assertTrue(
            native_u_intervals_cover([(0.0, covered)], 0.0, 1.0)
        )
        self.assertFalse(
            native_u_intervals_cover([(0.0, uncovered)], 0.0, 1.0)
        )

    def test_trimmed_domain_uses_containment_and_periodic_seam_lift(self):
        edge_local = native_curve_domain_transform(
            -10.0,
            10.0,
            1.0,
            2.0,
            True,
            False,
        )
        self.assertEqual(edge_local["status"], "edge_local_nonperiodic")
        self.assertEqual(edge_local["domain_basis"], "edge_curve_params3")
        self.assertEqual(edge_local["segments"], [[1.0, 2.0]])

        wrapped = native_curve_domain_transform(
            0.0,
            2 * math.pi,
            6.0,
            6.6,
            True,
            True,
        )
        self.assertEqual(wrapped["status"], "contained_seam_wrapped")
        self.assertIs(wrapped["seam_wrapped"], True)
        self.assertGreater(wrapped["period"], 0.0)
        self.assertEqual(len(wrapped["segments"]), 2)

        unresolved = native_curve_domain_transform(
            0.0,
            2 * math.pi,
            6.0,
            6.6,
            True,
            None,
        )
        self.assertEqual(unresolved["status"], "unresolved")

        reverse = native_curve_domain_transform(
            0.0,
            2 * math.pi,
            -7.0,
            -6.0,
            False,
            True,
        )
        self.assertEqual(reverse["direction"], -1)
        self.assertEqual((reverse["positive_start"], reverse["positive_end"]), (6.0, 7.0))
        self.assertEqual(reverse["status"], "contained_seam_wrapped")

    def test_float32_roundtrip_and_unresolvable_u_domain_counterexamples(self):
        fixture = Path(__file__).with_name(
            "native-float32-and-u-resolution-counterexamples.json"
        )
        root = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(
            fixture.name,
            "native-float32-and-u-resolution-counterexamples.json",
        )
        point_case = root["float32_roundtrip"]
        tolerance = native_point_roundtrip_tolerance(
            point_case["source"],
            point_case["rounded"],
        )
        self.assertGreaterEqual(tolerance, point_case["error_m"])

        u_case = root["u_resolution"]
        lower = u_case["lower"]
        upper = u_case["upper"]
        self.assertGreater(native_binary32_spacing(upper), upper - lower)
        self.assertFalse(native_u_domain_is_resolvable(lower, upper))
        self.assertFalse(
            native_u_intervals_cover(
                [tuple(u_case["covered_intervals"][0])],
                lower,
                upper,
            )
        )
        # A mathematically complete interval still cannot establish coverage
        # when the domain is below the binary32-resolvable threshold.
        self.assertFalse(
            native_u_intervals_cover([(lower, upper)], lower, upper)
        )

    def test_point_tolerance_binds_both_endpoint_roundtrips(self):
        expected = (-0.0028346584947739543, 0.01, 0.01535)
        evaluated = (-0.0028346575612019013, 0.009999999999999608, 0.01535)
        tolerance = native_point_roundtrip_tolerance(expected, evaluated)
        expected_tolerance = max(
            native_float32_adjacency_bound(expected[index])
            + native_float32_adjacency_bound(evaluated[index])
            for index in range(3)
        )
        error = max(
            abs(expected[index] - evaluated[index])
            for index in range(3)
        )

        self.assertEqual(tolerance, expected_tolerance)
        self.assertGreaterEqual(tolerance, error)
        self.assertLess(tolerance, 2e-9)
        self.assertGreater(
            error,
            max(
                native_binary32_spacing(value)
                for point in (expected, evaluated)
                for value in point
            ),
        )
        self.assertLess(tolerance, 100.0)

    def test_point_tolerance_handles_zero_negative_and_binade_coordinates(self):
        zero = (0.0, -0.0, 0.0)
        negative = (-0.0028346584947739543, -0.01, -0.01535)
        binade = (1.0, -2.0, 0.5)
        for point in (zero, negative, binade):
            expected = max(
                native_float32_adjacency_bound(point[0])
                + native_float32_adjacency_bound(point[0]),
                native_float32_adjacency_bound(point[1])
                + native_float32_adjacency_bound(point[1]),
                native_float32_adjacency_bound(point[2])
                + native_float32_adjacency_bound(point[2]),
            )
            self.assertEqual(
                native_point_roundtrip_tolerance(point, point),
                expected,
            )

    def test_curve_endpoint_operation_tolerance_covers_group_333(self):
        start = (
            -0.005322370050934642,
            0.00013700629315608413,
            -0.0021180644098793633,
        )
        end = (
            -0.005322370560299019,
            0.00013700674222233032,
            0.0013000000000000002,
        )
        evaluated_start = (
            -0.005322370560299019,
            0.00013700509113409066,
            -0.0021180644098793633,
        )
        evaluated_end = (
            -0.005322370560299019,
            0.00013700509113409066,
            0.0013,
        )
        tolerance = native_curve_endpoint_operation_tolerance(
            start,
            evaluated_start,
            end,
        )

        self.assertEqual(tolerance, 1.862645149230957e-9)
        self.assertGreaterEqual(
            tolerance,
            native_point_error(start, evaluated_start),
        )
        self.assertGreaterEqual(
            tolerance,
            native_point_error(end, evaluated_end),
        )

    def test_periodic_fin_interval_normalizes_seam_and_rejects_ambiguity(self):
        upper = 2.0 * math.pi
        seam = upper - 0.120830486676529

        self.assertEqual(
            native_fin_interval(
                0.0,
                seam,
                0.0,
                upper,
                is_periodic=True,
                is_closed=True,
            ),
            [(seam, upper), (0.0, 0.0)],
        )
        self.assertEqual(
            native_fin_interval(
                seam,
                0.0,
                0.0,
                upper,
                is_periodic=True,
                is_closed=True,
            ),
            [(seam, upper), (0.0, 0.0)],
        )
        self.assertEqual(
            native_fin_interval(
                0.0,
                0.120830486676529,
                0.0,
                upper,
                is_periodic=True,
                is_closed=True,
            ),
            [(0.0, 0.120830486676529)],
        )
        with self.assertRaises(ValueError):
            native_fin_interval(
                0.0,
                math.pi,
                0.0,
                upper,
                is_periodic=True,
                is_closed=True,
            )
        for near_half in (
            math.nextafter(math.pi, 0.0),
            math.nextafter(math.pi, upper),
        ):
            with self.subTest(near_half=near_half), self.assertRaises(ValueError):
                native_fin_interval(
                    0.0,
                    near_half,
                    0.0,
                    upper,
                    is_periodic=True,
                    is_closed=True,
                )

    def test_collapsed_u_fin_requires_positive_bounded_geometric_span(self):
        point = (0.0, 0.0, 0.0)
        moved = (9.489442548454158e-6, 0.0, 0.0)
        intervals, span = _finite_fin_intervals(
            fin_id=742,
            start_u=2.667937521368035e-7,
            end_u=2.667937521368035e-7,
            start_point=point,
            end_point=moved,
            u_min=2.667937521368035e-7,
            u_max=1.0,
            is_periodic=False,
            is_closed=False,
            component="fixture",
            reference="edge",
        )
        self.assertEqual(intervals, [])
        self.assertEqual(span, 9.489442548454158e-6)

        for moved_point in (
            point,
            (50.1e-6, 0.0, 0.0),
        ):
            with self.subTest(moved_point=moved_point), self.assertRaises(
                PipelineError
            ):
                _finite_fin_intervals(
                    fin_id=742,
                    start_u=2.667937521368035e-7,
                    end_u=2.667937521368035e-7,
                    start_point=point,
                    end_point=moved_point,
                    u_min=2.667937521368035e-7,
                    u_max=1.0,
                    is_periodic=False,
                    is_closed=False,
                    component="fixture",
                    reference="edge",
                )

    def test_ninety_percent_leading_gap_counterexample_is_rejected(self):
        fixture = Path(__file__).with_name(
            "native-u-leading-gap-counterexample.json"
        )
        evidence = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(
            evidence["parser_sha256"],
            "ea0afaa451778f1d1d1edf4d73d0c3e033fc0e479edc725c2950dbdbf0b0e95f",
        )
        self.assertFalse(
            native_u_intervals_cover([(0.9, 1.0)], 0.0, 1.0)
        )
        self.assertTrue(
            native_u_intervals_cover(
                [(0.0, 1.0), (0.9, 1.0)],
                0.0,
                1.0,
            )
        )
        self.assertFalse(
            all(
                native_u_intervals_cover(intervals, 0.0, 1.0)
                for intervals in ([(0.0, 1.0)], [(0.9, 1.0)])
            )
        )

    def test_native_u_identity_has_explicit_zero_scale_boundary(self):
        boundary = 128 * math.ulp(0.0)

        self.assertTrue(native_u_equal(0.0, boundary))
        self.assertFalse(native_u_equal(0.0, math.nextafter(boundary, math.inf)))

    def test_whole_body_face_list_is_one_explicit_all_face_dispatch_array(self):
        pythoncom = SimpleNamespace(VT_ARRAY=0x2000, VT_DISPATCH=9)
        client = SimpleNamespace(VARIANT=FakeVariant)
        faces = ("face-0", "face-1")
        references = ("a" * 64, "b" * 64)
        with patch.dict(sys.modules, {"pythoncom": pythoncom}), patch.object(
            native_module,
            "_win32",
            return_value=client,
        ):
            face_list, request = self.production_whole_body_face_list(
                faces,
                references,
            )

        self.assertEqual(
            face_list.varianttype,
            pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
        )
        self.assertEqual(face_list.value, faces)
        self.assertEqual(request, whole_body_request(references))

    def test_evaluate2_packed_status_counterexample_is_rejected(self):
        fixture = Path(__file__).with_name(
            "edge-evaluate-packed-status-counterexample.json"
        )
        evidence = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(
            evidence["native_sha256"],
            "d9e927e19ef767b910d82a9a5fb4c8d65767b039e9321a9748077e8c6d129ce0",
        )
        correct_point = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        self.assertTrue(
            native_evaluate2_success(
                [*correct_point, packed_evaluate_status(1)]
            )
        )
        self.assertFalse(
            native_evaluate2_success(
                [*correct_point, packed_evaluate_status(0)]
            )
        )
        self.assertFalse(native_evaluate2_success([*correct_point, 1.0]))
        for bits in (0x7FF8000000000001, 0x7FF0000000000001):
            packed = packed_evaluate_status(bits)
            serialized = native_evaluate2_evidence([*correct_point, packed])
            self.assertIsNotNone(serialized)
            self.assertTrue(serialized["packed_status"]["parsed_success"])
            self.assertEqual(serialized["packed_status"]["raw_bits_u64"], bits)
            self.assertEqual(
                serialized["packed_status"]["low32"],
                bits & 0xFFFFFFFF,
            )
            self.assertEqual(
                serialized["packed_status"]["high32"],
                bits >> 32,
            )
            json.dumps(serialized, allow_nan=False)
        self.assertFalse(
            native_evaluate2_success(
                [math.nan, 0.0, 0.0, 0.0, 0.0, 0.0, packed_evaluate_status(1)]
            )
        )
        self.assertFalse(native_evaluate2_success([*correct_point, True]))
        self.assertFalse(
            native_evaluate2_success(
                [*correct_point[:5], packed_evaluate_status(1)]
            )
        )
        for status in (packed_evaluate_status(0), 1.0):
            with self.subTest(status=status):
                body, _face_10, _face_20, edge, _tess, extension = self.fixture()
                edge.evaluate_status_double = status
                with self.assertRaises(CadError) as caught:
                    self.capture(body, extension)
                self.assertEqual(
                    caught.exception.code,
                    "cad_native_tessellation_unreadable",
                )

    def test_get_parameter_success_is_strict_but_accepts_packed_status(self):
        packed_success = packed_evaluate_status(1)
        packed_non_success = struct.unpack(
            "<d",
            struct.pack("<Q", 12),
        )[0]
        self.assertIs(native_get_parameter_success(True), True)
        self.assertIs(native_get_parameter_success(False), False)
        self.assertIs(native_get_parameter_success(1), True)
        self.assertIs(native_get_parameter_success(1.0), True)
        self.assertIs(native_get_parameter_success(0.0), False)
        self.assertIs(native_get_parameter_success(packed_success), True)
        for invalid in (
            2.0,
            -1.0,
            packed_non_success,
            math.nan,
            math.inf,
            -math.inf,
            "1",
            None,
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(native_get_parameter_success(invalid))
        body, _face_10, _face_20, edge, _tess, extension = self.fixture()
        edge.parameter_success = 2.0
        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)
        self.assertIn("packed low32 exactly 1", caught.exception.message)

    def test_get_end_params_uses_four_byref_outputs_and_scalar_bool(self):
        curve = FakeCurve(-10.0, 10.0, is_closed=True, is_periodic=False)

        result = native_module._read_curve_end_params(
            curve,
            variant_factory=FakeVariant,
            vt_byref=0x4000,
            vt_r8=5,
            vt_bool=11,
        )

        self.assertIs(result["success"], True)
        self.assertEqual((result["start"], result["end"]), (-10.0, 10.0))
        self.assertEqual(result["call_form"], "pywin32-byref-variant")
        self.assertEqual(
            [item["value_type"] for item in result["raw_outputs"]],
            [0x4005, 0x4005, 0x400B, 0x400B],
        )

    def test_get_end_params_requires_real_varianttype(self):
        class VariantTypeOnly:
            def __init__(self, varianttype, value):
                self.varianttype = varianttype
                self.value = value

        class ValueTypeOnly:
            def __init__(self, varianttype, value):
                self.value_type = varianttype
                self.value = value

        curve = FakeCurve(-10.0, 10.0)
        result = native_module._read_curve_end_params(
            curve,
            variant_factory=VariantTypeOnly,
            vt_byref=0x4000,
            vt_r8=5,
            vt_bool=11,
        )
        self.assertEqual(
            [item["value_type"] for item in result["raw_outputs"]],
            [0x4005, 0x4005, 0x400B, 0x400B],
        )

        curve = FakeCurve(-10.0, 10.0)
        result = native_module._read_curve_end_params(
            curve,
            variant_factory=ValueTypeOnly,
            vt_byref=0x4000,
            vt_r8=5,
            vt_bool=11,
        )
        self.assertEqual(
            [item["value_type"] for item in result["raw_outputs"]],
            [None, None, None, None],
        )

    def test_nonperiodic_present_edge_uses_get_curve_params3_domain(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        edge.u_min = -0.0044499999945766125
        edge.u_max = -1.4284990122115763e-18
        edge.curve = FakeCurve(0.0, 0.004449999994576611)
        edge.curve_start_point = [edge.u_min, 0.0, 0.0]
        edge.curve_end_point = [edge.u_max, 0.0, 0.0]
        edge.vertices = [
            FakeVertex(tuple(edge.curve_start_point)),
            FakeVertex(tuple(edge.curve_end_point)),
        ]
        tessellation.GetVertexPoint = lambda vertex_id: {
            1: tuple(edge.curve_start_point),
            2: tuple(edge.curve_end_point),
            3: (0.0, 1.0, 0.0),
            4: (1.0, 1.0, 0.0),
        }[vertex_id]

        captured = self.capture(body, extension)

        transform = captured["native_edge_rows"][0]["curve_domain_transform"]
        self.assertEqual(transform["status"], "edge_local_nonperiodic")
        self.assertEqual(transform["domain_basis"], "edge_curve_params3")
        self.assertEqual(
            (transform["lifted_start"], transform["lifted_end"]),
            (edge.u_min, edge.u_max),
        )
        self.assertEqual(
            (transform["underlying_start"], transform["underlying_end"]),
            (0.0, 0.004449999994576611),
        )

    def test_100_metre_closest_point_error_is_rejected_at_both_u_scales(self):
        fixture = Path(__file__).with_name(
            "native-u-metre-tolerance-counterexample.json"
        )
        evidence = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(
            evidence["native_sha256"],
            "3ed1b92adcc84aa0562f32e417fc6187d1ed4f8f08a032344c8807cf3f42e6a7",
        )
        for scale in (1.0, 1_000_000_000_000.0):
            with self.subTest(scale=scale):
                body, _face_10, _face_20, edge, _tessellation, extension = (
                    self.fixture()
                )
                edge.u_min = 0.0
                edge.u_max = scale
                edge.closest_offset = 100.0
                edge.curve_end_point = [scale, 0.0, 0.0]
                edge.curve = FakeCurve(0.0, scale)
                edge.vertices[1] = FakeVertex((scale, 0.0, 0.0))
                with self.assertRaises(CadError) as caught:
                    self.capture(body, extension)
                self.assertEqual(
                    caught.exception.code,
                    "cad_native_tessellation_unreadable",
                )
                self.assertIn(
                    "projection exceeds the 50 micrometre bound",
                    caught.exception.message,
                )

    def test_unknown_curve_periodicity_is_unresolved_not_defaulted(self):
        body, _face_10, _face_20, edge, _tessellation, extension = self.fixture()
        edge.curve = FakeCurve(error="unsupported COM out-parameter shape")

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)
        self.assertEqual(
            caught.exception.code,
            "cad_native_tessellation_unreadable",
        )

    def test_open_edge_with_missing_vertex_points_remains_fatal(self):
        body, _face_10, _face_20, edge, _tessellation, extension = self.fixture()
        edge.vertices = [None, None]
        edge.curve = FakeCurve(is_closed=False, is_periodic=False)

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)

        self.assertEqual(
            caught.exception.code,
            "cad_native_tessellation_unreadable",
        )
        self.assertIn("both absent endpoints", caught.exception.message)

    def test_closed_absent_endpoints_serialize_null_reference_errors(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        closed_point = (0.0, 0.0, 0.0)
        projected_point = (1e-7, 0.0, 0.0)
        edge.vertices = [None, None]
        edge.curve = FakeCurve(-10.0, 10.0, is_closed=True)
        edge.u_min = 0.0
        edge.u_max = 1.0
        edge.curve_start_point = list(closed_point)
        edge.curve_end_point = list(closed_point)
        edge.evaluate_u_points = {
            0.0: closed_point,
            1.0: closed_point,
        }
        edge.GetClosestPointOn = lambda x, y, z: [
            x,
            y,
            z,
            1.0 if x > 0.0 else 0.0,
            0.0,
        ]
        edge.GetParameter = lambda _x, _y, _z: [0.0, 0.0]
        tessellation.GetVertexPoint = lambda vertex_id: {
            1: closed_point,
            2: projected_point,
            3: (0.0, 1.0, 0.0),
            4: (1.0, 1.0, 0.0),
        }[vertex_id]

        captured = self.capture(body, extension)

        edge_row = captured["native_edge_rows"][0]
        self.assertEqual(edge_row["start_vertex_state"], "absent")
        self.assertEqual(edge_row["end_vertex_state"], "absent")
        self.assertIsNone(edge_row["start_reference_error"])
        self.assertIsNone(edge_row["end_reference_error"])

    def test_present_vertex_get_point_failure_is_not_reclassified_as_absent(self):
        body, _face_10, _face_20, edge, _tessellation, extension = self.fixture()
        edge.vertices = [
            FakeVertex(None, error="GetPoint"),
            FakeVertex(None, error="GetPoint"),
        ]
        edge.curve = FakeCurve(-10.0, 10.0, is_closed=True)

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)

        self.assertEqual(
            caught.exception.code,
            "cad_native_tessellation_unreadable",
        )
        self.assertIn("inconsistent or unreadable", caught.exception.message)

    def test_open_sheet_edge_accepts_single_boundary_fin(self):
        body, _face_10, _edge, tessellation, extension = (
            self.open_boundary_fixture()
        )
        direct = self.capture(body, extension, body_type=1)
        self.assertEqual(direct["itessellation_body"]["body_type"], 1)
        self.assertEqual(direct["edges"][0]["fin_ids"], (100,))
        self.assertEqual(tessellation.cofin_calls, [100])
        tessellation.cofin_calls.clear()
        backend = SolidWorksBackend()
        backend._source_components = {"part-1": (object(), "Default")}
        backend._captured_document = lambda *_args, **_kwargs: SimpleNamespace(
            Extension=extension
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            native_module,
            "_read_curve_end_params",
            fake_read_end_params,
        ):
            destination = Path(directory) / "mesh.stl"
            entry = backend._write_component_mesh(
                "part-1",
                destination,
                FakeHolder(body, body_type=1),
            )
            triangles = self.stl_triangles(destination)
        captured = entry["tessellation"]
        self.assertEqual(entry["bodies"], {"solid": 0, "sheet": 1})
        self.assertEqual(
            captured["itessellation"]["bodies"][0]["body_type"],
            1,
        )
        self.assertEqual(captured["native_edges"][0]["fin_ids"], [100])
        self.assertEqual(tessellation.cofin_calls, [100])
        fins = {
            row["fin_id"]: row
            for row in captured["itessellation"]["bodies"][0]["fins"]
        }
        self.assertEqual(fins[100]["cofin_raw_id"], -1)
        self.assertIsNone(fins[100]["cofin_id"])
        self.assertIsNone(fins[101]["cofin_raw_id"])
        self.assertIsNone(fins[101]["cofin_id"])
        spans = parse_native_tessellation(
            "part-1",
            captured,
            entry["bodies"],
            entry["triangles"],
            triangles,
        )
        self.assertEqual(len(spans), 1)

    def test_shared_edge_rejects_boundary_sentinel(self):
        body, _face_10, _face_20, _edge, tessellation, extension = (
            self.fixture()
        )
        tessellation.cofin_values = {100: -1, 200: 100}

        with self.assertRaisesRegex(
            CadError,
            "shared-edge fin did not report a cofin ID",
        ):
            self.capture(body, extension)

    def test_solid_body_cannot_use_open_boundary_semantics(self):
        body, _face_10, _edge, _tessellation, extension = (
            self.open_boundary_fixture()
        )

        with self.assertRaisesRegex(
            CadError,
            "open native edge is valid only on a sheet body",
        ):
            self.capture(body, extension, body_type=0)

    def test_open_edge_rejects_nonnegative_cofin(self):
        body, _face_10, _edge, tessellation, extension = (
            self.open_boundary_fixture()
        )
        tessellation.cofin_values = {100: 200}

        with self.assertRaisesRegex(
            CadError,
            "open-edge fin did not report the boundary sentinel",
        ):
            self.capture(body, extension, body_type=1)

    def test_open_edge_rejects_invalid_boundary_reads(self):
        for value in (-2, None, True, 1.0):
            with self.subTest(value=value):
                body, _face_10, _edge, tessellation, extension = (
                    self.open_boundary_fixture()
                )
                tessellation.cofin_values = {100: value}
                with self.assertRaises(CadError) as caught:
                    self.capture(body, extension, body_type=1)
                self.assertEqual(
                    caught.exception.code,
                    "cad_native_tessellation_unreadable",
                )
                self.assertIn(
                    "did not return an integer boundary or cofin ID",
                    caught.exception.message,
                )

    def test_native_edge_rejects_an_empty_fin_set(self):
        body, _face_10, _edge, tessellation, extension = (
            self.open_boundary_fixture()
        )
        tessellation.GetEdgeFins = lambda _edge: []

        with self.assertRaisesRegex(
            CadError,
            "native edge did not return any fins",
        ):
            self.capture(body, extension, body_type=1)

    def test_one_tessellate_partitions_edge_and_internal_fins(self):
        body, face_10, face_20, edge, tessellation, extension = self.fixture()

        captured = self.capture(body, extension)

        self.assertEqual(face_10.display_calls, 0)
        self.assertEqual(face_20.display_calls, 0)
        self.assertEqual(body.tessellation_calls, 1)
        self.assertEqual(body.tessellation_face_lists, ["VT_ARRAY|VT_DISPATCH"])
        self.assertEqual(tessellation.tessellate_calls, 1)
        self.assertEqual(
            tessellation.requested,
            {
                "MatchType": 0,
                "NeedFaceFacetMap": True,
                "NeedEdgeFinMap": True,
                "NeedVertexParams": True,
            },
        )
        self.assertEqual(
            tessellation.property_writes,
            [
                "MatchType",
                "NeedFaceFacetMap",
                "NeedEdgeFinMap",
                "NeedVertexParams",
            ],
        )
        self.assertEqual(
            tessellation.property_reads,
            [
                "MatchType",
                "NeedFaceFacetMap",
                "NeedEdgeFinMap",
                "NeedVertexParams",
            ],
        )
        self.assertEqual(
            captured["itessellation_body"]["request"],
            whole_body_request(
                row["reference"]["typed_sha256"]
                for row in captured["faces"]
            ),
        )
        self.assertEqual(
            captured["itessellation_body"]["properties"],
            {
                "match_type": 0,
                "need_face_facet_map": True,
                "need_edge_fin_map": True,
                "need_vertex_params": True,
            },
        )
        self.assertEqual(len(tessellation.face_facet_calls), 2)
        self.assertCountEqual(tessellation.facet_fin_calls, [10, 20])
        self.assertEqual(tessellation.edge_fin_calls, [edge])
        self.assertEqual(
            tessellation.native_count_calls,
            ["GetFacetCount", "GetFinCount", "GetVertexCount"],
        )
        self.assertCountEqual(tessellation.fin_vertex_calls, [100, 101, 102, 200, 201, 202])
        self.assertCountEqual(tessellation.vertex_point_calls, [1, 2, 3, 4])
        self.assertEqual(edge.closest_calls, 2)
        self.assertEqual(edge.parameter_calls, 2)
        self.assertEqual(edge.evaluate_calls, 4)
        self.assertEqual(edge.sense_calls, 4)
        self.assertCountEqual(tessellation.cofin_calls, [100, 200])
        self.assertEqual(
            [item.varianttype for item in edge.curve.end_params_arguments],
            [0x4005, 0x4005, 0x400B, 0x400B],
        )
        self.assertEqual(
            captured["u_observation_stats"],
            {
                "unique_observations": 2,
                "cache_hits": 2,
                "get_closest_point_on_calls": 2,
                "get_parameter_calls": 2,
                "evaluate2_calls": 2,
            },
        )
        self.assertEqual(captured["edges"][0]["fin_ids"], (100, 200))
        self.assertEqual(captured["internal_fin_ids"], (101, 102, 201, 202))
        self.assertIsNone(captured["fins"][101]["edge_reference_sha256"])
        self.assertTrue(captured["fins"][101]["internal"])
        self.assertFalse(captured["fins"][100]["internal"])
        self.assertEqual(
            captured["native_totals"],
            {"facets": 2, "fins": 6, "vertices": 4},
        )
        self.assertEqual(len(extension.calls), 9)
        self.assertNotEqual(
            captured["body_reference"]["raw_sha256"],
            captured["body_reference"]["typed_sha256"],
        )
        self.assertEqual(captured["facets"][10]["triangle_record_index"], 0)
        self.assertEqual(captured["facets"][20]["triangle_record_index"], 1)
        self.assertEqual(captured["triangle_records"][0]["facet_id"], 10)
        self.assertEqual(captured["triangle_records"][1]["facet_id"], 20)
        self.assert_directed_cycle(
            captured["triangle_records"][0]["native_binary64_points"],
            (POINTS[1], POINTS[2], POINTS[3]),
        )
        self.assert_directed_cycle(
            captured["triangle_records"][1]["native_binary64_points"],
            (POINTS[2], POINTS[1], POINTS[4]),
        )
        self.assertEqual(
            captured["triangle_records"][0]["output_float32_points"][1][0],
            1.0,
        )

    def test_nonperiodic_reverse_sense_keeps_parameter_endpoint_order(self):
        body, _face_10, _face_20, edge, _tessellation, extension = self.fixture()
        edge.sense = False
        edge.vertices = [
            FakeVertex((1.0, 0.0, 0.0)),
            FakeVertex((0.0, 0.0, 0.0)),
        ]

        captured = self.capture(body, extension)

        edge_row = captured["native_edge_rows"][0]
        self.assertIs(edge_row["curve_sense"], False)
        self.assertEqual(
            [
                edge_row["curve_endpoint_evaluations"][name]["payload"][:3]
                for name in ("start", "end")
            ],
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
        )
        self.assertTrue(
            all(
                evidence["error"] is None
                for evidence in edge_row["curve_endpoint_evaluations"].values()
            )
        )

    def test_present_curve_endpoint_operation_accepts_group_333_residual(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        self.configure_group_333(edge, tessellation)

        captured = self.capture(body, extension)

        evaluations = captured["native_edge_rows"][0][
            "curve_endpoint_evaluations"
        ]
        self.assertTrue(
            all(evidence["error"] is None for evidence in evaluations.values())
        )
        self.assertEqual(
            {
                evidence["point_tolerance_m"]
                for evidence in evaluations.values()
            },
            {1.862645149230957e-9},
        )

    def test_present_curve_endpoint_operation_rejects_one_micrometre_error(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        start, _end, _evaluated_start, _evaluated_end = (
            self.configure_group_333(edge, tessellation)
        )
        edge.evaluate_points[0] = (start[0] + 1e-6, start[1], start[2])

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)

        self.assertEqual(
            caught.exception.code,
            "cad_native_tessellation_unreadable",
        )
        self.assertIn("curve-domain evidence", caught.exception.message)

    def test_one_failed_curve_endpoint_evaluation_cannot_be_bypassed(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        self.configure_group_333(edge, tessellation)
        edge.evaluate_points = []
        edge.evaluate_raw = [
            [
                *edge.curve_start_point,
                0.0,
                0.0,
                0.0,
                edge.evaluate_status_double,
            ],
            [0.0, 0.0, 5e-324],
        ]

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)

        self.assertIn("curve-domain evidence", caught.exception.message)

    def test_curve_endpoint_bound_does_not_mask_conflicting_fin_point(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        _start, _end, evaluated_start, evaluated_end = (
            self.configure_group_333(edge, tessellation)
        )
        conflicting_start = (
            evaluated_start[0] + 1e-6,
            evaluated_start[1],
            evaluated_start[2],
        )
        tessellation.GetVertexPoint = lambda vertex_id: {
            1: conflicting_start,
            2: evaluated_end,
            3: (0.0, 1.0, 0.0),
            4: (1.0, 1.0, 0.0),
        }[vertex_id]
        u_by_point = {
            conflicting_start: edge.u_min,
            evaluated_end: edge.u_max,
        }
        edge.GetClosestPointOn = lambda x, y, z: [
            x,
            y,
            z,
            u_by_point[(x, y, z)],
            0.0,
        ]
        edge.GetParameter = lambda x, y, z: [u_by_point[(x, y, z)], True]

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)

        self.assertIn("does not reproduce the fin endpoint", caught.exception.message)

    def test_get_parameter_is_authoritative_when_closest_u_differs(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        _start, _end, evaluated_start, _evaluated_end = (
            self.configure_group_333(edge, tessellation)
        )
        auxiliary_closest_u = (edge.u_min + edge.u_max) / 2

        def closest_u(point):
            return (
                auxiliary_closest_u
                if point == evaluated_start
                else edge.u_max
            )

        edge.GetClosestPointOn = lambda x, y, z: [
            x,
            y,
            z,
            closest_u((x, y, z)),
            0.0,
        ]
        edge.GetParameter = lambda x, y, z: [
            edge.u_min if (x, y, z) == evaluated_start else edge.u_max,
            packed_evaluate_status(1),
        ]

        captured = self.capture(body, extension)

        start_evidence = captured["itessellation_body"]["fins"][0][
            "start_evidence"
        ]
        self.assertEqual(start_evidence["u"], edge.u_min)
        self.assertEqual(
            start_evidence["u_parameter_rule"],
            "native-u-point-parameter-authoritative-v2",
        )
        self.assertNotEqual(
            start_evidence["u"],
            start_evidence["closest_point_on_raw"][3],
        )

    def test_get_parameter_u_outside_edge_domain_is_rejected(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        _start, _end, evaluated_start, _evaluated_end = (
            self.configure_group_333(edge, tessellation)
        )
        edge.GetParameter = lambda x, y, z: [
            edge.u_max + 1.0
            if (x, y, z) == evaluated_start
            else edge.u_max,
            packed_evaluate_status(1),
        ]

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)

        self.assertIn("outside the edge domain", caught.exception.message)

    def test_fin_at_edge_boundary_uses_curve_operation_tolerance(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        start, end, evaluated_start, evaluated_end = (
            self.configure_group_333(edge, tessellation)
        )
        tessellation.GetVertexPoint = lambda vertex_id: {
            1: start,
            2: end,
            3: (0.0, 1.0, 0.0),
            4: (1.0, 1.0, 0.0),
        }[vertex_id]
        u_by_point = {start: edge.u_min, end: edge.u_max}
        edge.GetClosestPointOn = lambda x, y, z: [
            x,
            y,
            z,
            u_by_point[(x, y, z)],
            0.0,
        ]
        edge.GetParameter = lambda x, y, z: [u_by_point[(x, y, z)], True]
        edge.evaluate_u_points = {
            edge.u_min: evaluated_start,
            edge.u_max: evaluated_end,
        }

        captured = self.capture(body, extension)

        fin = next(
            row
            for row in captured["itessellation_body"]["fins"]
            if row["classification"] == "native_edge"
            and native_u_equal(row["start_u"], edge.u_min)
        )
        self.assertEqual(fin["start_u"], edge.u_min)
        self.assertGreater(
            fin["start_evidence"]["evaluate_point_error_m"],
            1e-9,
        )
        self.assertEqual(
            fin["start_evidence"]["point_tolerance_m"],
            1.862645149230957e-9,
        )

    def test_projection_point_not_raw_tessellation_vertex_feeds_parameter(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        _start, _end, evaluated_start, evaluated_end = (
            self.configure_group_333(edge, tessellation)
        )
        source_point = (
            evaluated_start[0] + 1.9e-6,
            evaluated_start[1],
            evaluated_start[2],
        )
        tessellation.GetVertexPoint = lambda vertex_id: {
            1: source_point,
            2: evaluated_end,
            3: (0.0, 1.0, 0.0),
            4: (1.0, 1.0, 0.0),
        }[vertex_id]

        def closest_point(x, y, z):
            if (x, y, z) == source_point:
                return evaluated_start
            return (x, y, z)

        def closest_u(x, y, z):
            return edge.u_min if (x, y, z) == source_point else edge.u_max

        edge.GetClosestPointOn = lambda x, y, z: [
            *closest_point(x, y, z),
            closest_u(x, y, z),
            0.0,
        ]

        def parameter(x, y, z):
            if (x, y, z) == evaluated_start:
                return [edge.u_min, packed_evaluate_status(1)]
            if (x, y, z) == source_point:
                return [edge.u_min, 0.0]
            return [edge.u_max, packed_evaluate_status(1)]

        edge.GetParameter = parameter

        captured = self.capture(body, extension)

        evidence = next(
            row
            for row in captured["itessellation_body"]["fins"]
            if row["classification"] == "native_edge"
            and native_u_equal(row["start_u"], edge.u_min)
        )["start_evidence"]
        self.assertEqual(evidence["source_point_m"], list(source_point))
        self.assertEqual(
            evidence["parameter_input_point_m"],
            list(evaluated_start),
        )
        self.assertGreaterEqual(evidence["projection_error_m"], 1.8e-6)
        self.assertLessEqual(evidence["projection_error_m"], 50e-6)

    def test_projection_over_50_micrometres_is_rejected(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        _start, _end, evaluated_start, evaluated_end = (
            self.configure_group_333(edge, tessellation)
        )
        source_point = (
            evaluated_start[0] + 60e-6,
            evaluated_start[1],
            evaluated_start[2],
        )
        tessellation.GetVertexPoint = lambda vertex_id: {
            1: source_point,
            2: evaluated_end,
            3: (0.0, 1.0, 0.0),
            4: (1.0, 1.0, 0.0),
        }[vertex_id]
        edge.GetClosestPointOn = lambda x, y, z: [
            *(
                evaluated_start
                if (x, y, z) == source_point
                else (x, y, z)
            ),
            edge.u_min if (x, y, z) == source_point else edge.u_max,
            0.0,
        ]

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)

        self.assertIn("50 micrometre bound", caught.exception.message)

    def test_get_parameter_false_uses_closest_u_fallback(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        _start, _end, evaluated_start, _evaluated_end = (
            self.configure_group_333(edge, tessellation)
        )
        fallback_evaluation = (
            evaluated_start[0] + 5e-6,
            evaluated_start[1],
            evaluated_start[2],
        )
        edge.evaluate_u_points[edge.u_min] = fallback_evaluation
        edge.GetParameter = lambda x, y, z: [
            edge.u_min if (x, y, z) == evaluated_start else edge.u_max,
            0.0 if (x, y, z) == evaluated_start else packed_evaluate_status(1),
        ]

        captured = self.capture(body, extension)

        evidence = next(
            row
            for row in captured["itessellation_body"]["fins"]
            if row["classification"] == "native_edge"
            and native_u_equal(row["start_u"], edge.u_min)
        )["start_evidence"]
        self.assertEqual(
            evidence["u_parameter_rule"],
            "native-u-closest-point-fallback-v1",
        )
        self.assertEqual(
            evidence["parameter_u_source"],
            "closest_point_fallback",
        )
        self.assertEqual(evidence["fallback_reason"], "get_parameter_false")
        self.assertGreaterEqual(evidence["source_evaluate_error_m"], 4.9e-6)
        self.assertEqual(
            evidence["point_roundtrip_rule"],
            "tessellation-edge-projection-v1",
        )
        self.assertEqual(evidence["parameter_success_bit"], 0)
        self.assertIs(evidence["parameter_success_parsed"], False)

    def test_closest_fallback_over_50_micrometre_evaluation_is_rejected(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        _start, _end, evaluated_start, _evaluated_end = (
            self.configure_group_333(edge, tessellation)
        )
        edge.evaluate_u_points[edge.u_min] = (
            evaluated_start[0] + 60e-6,
            evaluated_start[1],
            evaluated_start[2],
        )
        edge.GetParameter = lambda x, y, z: [
            edge.u_min if (x, y, z) == evaluated_start else edge.u_max,
            0.0 if (x, y, z) == evaluated_start else packed_evaluate_status(1),
        ]

        with self.assertRaises(CadError):
            self.capture(body, extension)

    def test_parser_accepts_group_333_operation_bound(self):
        evidence, triangles = self.group_333_tessellation()
        alias_audit = evidence["itessellation"]["reference_alias_audit"]
        self.assertEqual(
            set(alias_audit),
            {"body", "face", "edge", "vertex"},
        )
        self.assertTrue(all(not alias_audit[kind] for kind in alias_audit))
        self.assertEqual(
            evidence["itessellation"]["cross_kind_raw_aliases"],
            [],
        )

        identity_warnings = []
        spans = parse_native_tessellation(
            "part-1",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            triangles,
            native_identity_warnings=identity_warnings,
        )

        self.assertEqual(len(spans), 2)
        self.assertEqual(identity_warnings, [])
        self.assertTrue(
            all(
                span["face_identity_mode"]
                == "native-persistent-reference-v1"
                and span["face_identity"] is None
                for span in evidence["face_spans"]
            )
        )

    def test_production_capture_ignores_recycled_vertex_addresses(self):
        original_pointer = native_module._com_object_pointer

        def pointer(value):
            if isinstance(value, FakeVertex):
                return 0xDEADBEEF
            return original_pointer(value)

        with patch.object(
            native_module,
            "_com_object_pointer",
            side_effect=pointer,
        ):
            evidence, triangles = self.group_333_tessellation()

        spans = parse_native_tessellation(
            "part-1",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            triangles,
        )
        self.assertEqual(len(spans), 2)
        identities = evidence["itessellation"]["solidworks_vertex_identities"]
        self.assertEqual(len(identities), 2)
        native_edge = evidence["native_edges"][0]
        expected_points = {
            tuple(native_edge["start_point_m"]),
            tuple(native_edge["end_point_m"]),
        }
        self.assertEqual(
            {tuple(row["point_m"]) for row in identities},
            expected_points,
        )

    def test_parser_rejects_malformed_or_ambiguous_alias_evidence(self):
        evidence, triangles = self.group_333_tessellation()
        raw_sha = hashlib.sha256(b"raw-alias-evidence").hexdigest()
        component = "part-1"
        body_owner = {
            "kind": "body",
            "component": component,
            "body_type": 0,
            "body_index": 0,
            "typed_sha256": _typed_reference_sha256("body", b"raw-alias-evidence"),
        }
        face_owner = {
            "kind": "face",
            "component": component,
            "body_type": 0,
            "body_index": 0,
            "face_index": 0,
            "typed_sha256": _typed_reference_sha256("face", b"raw-alias-evidence"),
        }
        valid = {
            "raw_sha256": raw_sha,
            "owners": [body_owner, face_owner],
        }
        mutations = (
            (
                "extra-owner-key",
                lambda row: row["owners"][1].update({"injected": True}),
            ),
            (
                "duplicate-typed",
                lambda row: row["owners"][1].update(
                    {"typed_sha256": row["owners"][0]["typed_sha256"]}
                ),
            ),
            (
                "cross-body",
                lambda row: row["owners"][1].update({"body_index": 1}),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = copy.deepcopy(evidence)
                alias = copy.deepcopy(valid)
                mutate(alias)
                changed["itessellation"]["cross_kind_raw_aliases"] = [alias]
                with self.assertRaises(PipelineError):
                    parse_native_tessellation(
                        component,
                        changed,
                        {"solid": 1, "sheet": 0},
                        len(triangles),
                        triangles,
                    )

    def test_parser_accepts_collapsed_pair_as_zero_coverage_with_warning(self):
        evidence, triangles, _edge, _start, _near, _end, _bind = (
            self.collapsed_u_tessellation()
        )
        original = copy.deepcopy(evidence)
        resolutions = []

        spans = parse_native_tessellation(
            "part-1",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            triangles,
            native_fin_resolutions=resolutions,
        )

        self.assertEqual(len(spans), 2)
        self.assertEqual(evidence, original)
        warning, = resolutions
        self.assertEqual(warning["code"], "native_fin_u_collapsed")
        self.assertEqual(warning["rule"], NATIVE_COLLAPSED_U_FIN_RULE)
        self.assertEqual(warning["component"], "part-1")
        self.assertEqual(
            warning["qualification"],
            "zero_u_coverage; native_geometry_bounded_50um",
        )
        self.assertEqual(len(warning["fin_ids"]), 2)
        self.assertEqual(sorted(warning["spans_m"]), sorted(map(str, warning["fin_ids"])))
        self.assertTrue(
            all(
                0.0 < span <= NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
                for span in warning["spans_m"].values()
            )
        )

    def test_parser_rejects_injected_collapse_resolution_fields(self):
        evidence, triangles, _edge, _start, _near, _end, _bind = (
            self.collapsed_u_tessellation()
        )
        fin = next(
            row
            for row in evidence["itessellation"]["bodies"][0]["fins"]
            if row["classification"] == "internal_only"
        )
        fin.update(
            {
                "u_resolution_rule": NATIVE_COLLAPSED_U_FIN_RULE,
                "u_resolution_span_m": -1.0,
                "u_resolution_warning": 123,
            }
        )

        with self.assertRaisesRegex(PipelineError, "unknown shape"):
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                triangles,
            )

    def test_parser_requires_an_empty_native_fin_resolution_sink(self):
        evidence, triangles, _edge, _start, _near, _end, _bind = (
            self.collapsed_u_tessellation()
        )
        for sink in ((), [{"code": "spoof"}]):
            with self.subTest(sink=sink), self.assertRaisesRegex(
                PipelineError,
                "resolution output must be an empty list",
            ):
                parse_native_tessellation(
                    "part-1",
                    evidence,
                    {"solid": 1, "sheet": 0},
                    len(triangles),
                    triangles,
                    native_fin_resolutions=sink,
                )

    def test_parser_rejects_partial_collapsed_fin_pair(self):
        evidence, triangles, edge, _start, near, _end, bind = (
            self.collapsed_u_tessellation()
        )
        body = evidence["itessellation"]["bodies"][0]
        fins = {row["fin_id"]: row for row in body["fins"]}
        vertices = {
            row["vertex_id"]: tuple(row["point_m"])
            for row in evidence["itessellation"]["vertices"]
        }
        cofin = next(
            fins[fin_id]
            for fin_id in edge["fin_ids"]
            if vertices[fins[fin_id]["vertex_ids"][0]] == near
            and vertices[fins[fin_id]["vertex_ids"][1]] == _start
        )
        new_u = edge["u_min"] + 1e-12
        cofin["end_u"] = new_u
        bind(
            cofin["end_evidence"],
            _start,
            new_u,
            fallback=False,
        )

        with self.assertRaisesRegex(PipelineError, "partial U collapse"):
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                triangles,
            )

    def test_parser_rejects_nonreciprocal_or_same_face_collapsed_cofin(self):
        base, triangles, edge, _start, _near, _end, bind = (
            self.collapsed_u_tessellation()
        )
        base_body = base["itessellation"]["bodies"][0]
        base_fins = {row["fin_id"]: row for row in base_body["fins"]}
        base_vertices = {
            row["vertex_id"]: tuple(row["point_m"])
            for row in base["itessellation"]["vertices"]
        }
        collapsed = next(
            fin
            for fin_id in edge["fin_ids"]
            if (fin := base_fins[fin_id])["start_u"] == fin["end_u"]
            and len(
                {
                    base_vertices[fin["vertex_ids"][0]],
                    base_vertices[fin["vertex_ids"][1]],
                }
            )
            == 2
        )

        for label, same_face in (("same-face", True), ("nonreciprocal", False)):
            with self.subTest(label=label):
                evidence = copy.deepcopy(base)
                body = evidence["itessellation"]["bodies"][0]
                fins = {row["fin_id"]: row for row in body["fins"]}
                fin = fins[collapsed["fin_id"]]
                replacement = next(
                    row
                    for row in body["fins"]
                    if row["classification"] == "native_edge"
                    and row["fin_id"] not in {
                        fin["fin_id"],
                        fin["cofin_id"],
                    }
                    and (
                        row["face_index"] == fin["face_index"]
                        if same_face
                        else row["face_index"] != fin["face_index"]
                    )
                )
                fin["cofin_id"] = replacement["fin_id"]
                fin["cofin_raw_id"] = replacement["fin_id"]
                replacement["start_u"] = edge["u_min"]
                replacement["end_u"] = edge["u_min"]
                replacement_points = tuple(
                    base_vertices[vertex_id]
                    for vertex_id in replacement["vertex_ids"]
                )
                bind(
                    replacement["start_evidence"],
                    replacement_points[0],
                    edge["u_min"],
                    fallback=replacement_points[0] != _start,
                )
                bind(
                    replacement["end_evidence"],
                    replacement_points[1],
                    edge["u_min"],
                    fallback=replacement_points[1] != _start,
                )
                with self.assertRaisesRegex(
                    PipelineError,
                    "cofin/orientation is inconsistent",
                ):
                    parse_native_tessellation(
                        "part-1",
                        evidence,
                        {"solid": 1, "sheet": 0},
                        len(triangles),
                        triangles,
                    )

    def test_parser_rejects_collapsed_cofin_reverse_coordinate_mismatch(self):
        evidence, triangles, edge, start, near, _end, bind = (
            self.collapsed_u_tessellation()
        )
        body = evidence["itessellation"]["bodies"][0]
        vertices = evidence["itessellation"]["vertices"]
        vertex_by_id = {row["vertex_id"]: row for row in vertices}
        fins = {row["fin_id"]: row for row in body["fins"]}
        vertex_points = {
            row["vertex_id"]: tuple(row["point_m"])
            for row in vertices
        }
        cofin = next(
            fins[fin_id]
            for fin_id in edge["fin_ids"]
            if vertex_points[fins[fin_id]["vertex_ids"][0]] == near
            and vertex_points[fins[fin_id]["vertex_ids"][1]] == start
        )
        face_index = cofin["face_index"]
        near_id = cofin["vertex_ids"][0]
        new_id = max(vertex_points) + 1
        shifted = copy.deepcopy(vertex_by_id[near_id])
        shifted["vertex_id"] = new_id
        shifted["point_m"][0] += 1e-8
        shifted["point_m"] = list(
            struct.unpack("<3f", struct.pack("<3f", *shifted["point_m"]))
        )
        shifted_point = tuple(shifted["point_m"])
        vertices.append(shifted)
        body["vertex_count"] += 1
        for fin in body["fins"]:
            if fin["face_index"] != face_index or near_id not in fin["vertex_ids"]:
                continue
            fin["vertex_ids"] = [
                new_id if vertex_id == near_id else vertex_id
                for vertex_id in fin["vertex_ids"]
            ]
            for endpoint in ("start", "end"):
                if fin[f"{endpoint}_evidence"] is None:
                    continue
                if fin["vertex_ids"][0 if endpoint == "start" else 1] != new_id:
                    continue
                bind(
                    fin[f"{endpoint}_evidence"],
                    shifted_point,
                    fin[f"{endpoint}_u"],
                    fallback=True,
                )
        for facet in body["facets"]:
            if facet["face_index"] != face_index:
                continue
            facet["vertex_ids"] = [
                new_id if vertex_id == near_id else vertex_id
                for vertex_id in facet["vertex_ids"]
            ]
            facet["vertex_ids"].sort()
        for index in (2, 3):
            triangles[index] = tuple(
                shifted_point if point == near else point
                for point in triangles[index]
            )

        with self.assertRaisesRegex(
            PipelineError,
            "cofin/orientation is inconsistent",
        ):
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                triangles,
            )

    def test_parser_rejects_collapsed_pair_with_equal_or_unreadable_sense(self):
        base, triangles, _edge, _start, _near, _end, _bind = (
            self.collapsed_u_tessellation()
        )
        for label in ("equal", "unreadable"):
            with self.subTest(label=label):
                evidence = copy.deepcopy(base)
                senses = evidence["native_edges"][0]["face_senses"]
                if label == "equal":
                    for row in senses:
                        row["EdgeInFaceSense"]["value"] = True
                        row["IEdgeInFaceSense2"]["value"] = True
                    expected = "cofaces have the same orientation"
                else:
                    senses[0]["EdgeInFaceSense"]["value"] = None
                    expected = "primary orientation is unreadable"
                with self.assertRaisesRegex(PipelineError, expected):
                    parse_native_tessellation(
                        "part-1",
                        evidence,
                        {"solid": 1, "sheet": 0},
                        len(triangles),
                        triangles,
                    )

    def test_parser_rejects_collapsed_only_active_face_coverage(self):
        evidence, triangles, edge, start, near, end, bind = (
            self.collapsed_u_tessellation()
        )
        body = evidence["itessellation"]["bodies"][0]
        fins = {row["fin_id"]: row for row in body["fins"]}
        vertices = {
            row["vertex_id"]: tuple(row["point_m"])
            for row in evidence["itessellation"]["vertices"]
        }
        for fin_id in edge["fin_ids"]:
            fin = fins[fin_id]
            points = tuple(vertices[vertex_id] for vertex_id in fin["vertex_ids"])
            if set(points) != {near, end}:
                continue
            fin["start_u"] = edge["u_min"]
            fin["end_u"] = edge["u_min"]
            bind(
                fin["start_evidence"],
                points[0],
                edge["u_min"],
                fallback=points[0] != start,
            )
            bind(
                fin["end_evidence"],
                points[1],
                edge["u_min"],
                fallback=points[1] != start,
            )

        with self.assertRaisesRegex(
            PipelineError,
            "collapsed-only face without positive U coverage",
        ):
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                triangles,
            )

    def test_parser_uses_point_parameter_when_closest_u_is_auxiliary(self):
        evidence, triangles = self.group_333_tessellation()
        edge = evidence["native_edges"][0]
        fins = evidence["itessellation"]["bodies"][0]["fins"]
        fin = next(
            row
            for row in fins
            if native_u_equal(row["start_evidence"]["u"], edge["u_min"])
        )
        fin_evidence = fin["start_evidence"]
        fin_evidence["closest_point_on_raw"][3] = (
            edge["u_min"] + edge["u_max"]
        ) / 2

        spans = parse_native_tessellation(
            "part-1",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            triangles,
        )

        self.assertEqual(len(spans), 2)
        self.assertNotEqual(
            fin_evidence["u"],
            fin_evidence["closest_point_on_raw"][3],
        )

    def test_parser_rejects_auxiliary_closest_u_outside_edge_domain(self):
        evidence, triangles = self.group_333_tessellation()
        edge = evidence["native_edges"][0]
        fins = evidence["itessellation"]["bodies"][0]["fins"]
        fin = next(
            row
            for row in fins
            if native_u_equal(row["start_evidence"]["u"], edge["u_min"])
        )
        fin["start_evidence"]["closest_point_on_raw"][3] = edge["u_max"] + 1.0

        with self.assertRaises(PipelineError) as caught:
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                triangles,
            )

        self.assertIn("U evidence is not independently reproducible", str(caught.exception))

    def test_parser_accepts_curve_operation_bound_at_fin_edge_boundary(self):
        evidence, triangles = self.group_333_tessellation()
        edge = evidence["native_edges"][0]
        start = tuple(edge["curve_start_point_m"])
        end = tuple(edge["curve_end_point_m"])
        evaluated_start = tuple(
            edge["curve_endpoint_evaluations"]["start"]["payload"][:3]
        )
        tolerance = native_curve_endpoint_operation_tolerance(
            start,
            evaluated_start,
            end,
        )
        error = native_point_error(start, evaluated_start)
        affected_vertices = set()
        for fin in evidence["itessellation"]["bodies"][0]["fins"]:
            if fin["classification"] == "internal_only":
                continue
            for vertex_id, fin_u, fin_evidence in (
                (fin["vertex_ids"][0], fin["start_u"], fin["start_evidence"]),
                (fin["vertex_ids"][1], fin["end_u"], fin["end_evidence"]),
            ):
                if not native_u_equal(fin_u, edge["u_min"]):
                    continue
                affected_vertices.add(vertex_id)
                fin_evidence["closest_point_on_raw"][:3] = start
                fin_evidence["evaluate_payload"][:3] = evaluated_start
                fin_evidence["source_point_m"] = list(start)
                fin_evidence["parameter_input_point_m"] = list(start)
                fin_evidence["projection_error_m"] = 0.0
                fin_evidence["source_evaluate_error_m"] = error
                fin_evidence["point_tolerance_m"] = tolerance
                fin_evidence["closest_point_error_m"] = 0.0
                fin_evidence["evaluate_point_error_m"] = error
                fin_evidence["closest_evaluate_error_m"] = error
        for vertex in evidence["itessellation"]["vertices"]:
            if vertex["vertex_id"] in affected_vertices:
                vertex["point_m"] = list(start)
        triangles = [
            tuple(start if point == evaluated_start else point for point in triangle)
            for triangle in triangles
        ]

        spans = parse_native_tessellation(
            "part-1",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            None,
        )

        self.assertEqual(len(spans), 2)

    def test_parser_accepts_tessellation_projection_within_50_micrometres(self):
        evidence, triangles = self.group_333_tessellation()
        edge = evidence["native_edges"][0]
        projected = tuple(edge["curve_start_point_m"])
        evaluated = tuple(
            edge["curve_endpoint_evaluations"]["start"]["payload"][:3]
        )
        source = (projected[0] + 1.9e-6, projected[1], projected[2])
        projection_error = native_point_error(source, projected)
        tolerance = native_curve_endpoint_operation_tolerance(
            projected,
            evaluated,
            edge["curve_end_point_m"],
        )
        error = native_point_error(projected, evaluated)
        affected_vertices = set()
        for fin in evidence["itessellation"]["bodies"][0]["fins"]:
            if fin["classification"] == "internal_only":
                continue
            for vertex_id, fin_u, fin_evidence in (
                (fin["vertex_ids"][0], fin["start_u"], fin["start_evidence"]),
                (fin["vertex_ids"][1], fin["end_u"], fin["end_evidence"]),
            ):
                if not native_u_equal(fin_u, edge["u_min"]):
                    continue
                affected_vertices.add(vertex_id)
                fin_evidence["source_point_m"] = list(source)
                fin_evidence["closest_point_on_raw"][:3] = projected
                fin_evidence["parameter_input_point_m"] = list(projected)
                fin_evidence["evaluate_payload"][:3] = evaluated
                fin_evidence["projection_error_m"] = projection_error
                fin_evidence["closest_point_error_m"] = projection_error
                fin_evidence["point_tolerance_m"] = tolerance
                fin_evidence["point_roundtrip_rule"] = (
                    "native-curve-endpoint-float32-operation-v4"
                )
                fin_evidence["evaluate_point_error_m"] = error
                fin_evidence["closest_evaluate_error_m"] = error
                fin_evidence["source_evaluate_error_m"] = native_point_error(
                    source,
                    evaluated,
                )
        for vertex in evidence["itessellation"]["vertices"]:
            if vertex["vertex_id"] in affected_vertices:
                vertex["point_m"] = list(source)

        spans = parse_native_tessellation(
            "part-1",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            None,
        )

        self.assertEqual(len(spans), 2)

    def test_parser_rejects_tessellation_projection_over_50_micrometres(self):
        evidence, triangles = self.group_333_tessellation()
        fin = next(
            row
            for row in evidence["itessellation"]["bodies"][0]["fins"]
            if row["classification"] == "native_edge"
        )
        fin_evidence = fin["start_evidence"]
        source = list(fin_evidence["source_point_m"])
        source[0] += 60e-6
        fin_evidence["source_point_m"] = source
        fin_evidence["projection_error_m"] = 60e-6
        fin_evidence["closest_point_error_m"] = 60e-6
        fin_evidence["source_evaluate_error_m"] = 60e-6

        with self.assertRaises(PipelineError) as caught:
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                None,
            )

        self.assertIn("U evidence is not independently reproducible", str(caught.exception))

    def test_parser_boundary_tolerance_uses_projected_point_across_binade(self):
        projected = (0.0, 0.0, 0.0)
        source = (1e-7, 0.0, 0.0)
        opposite = (0.0, 0.0, 0.0)
        projected_tolerance = native_curve_endpoint_operation_tolerance(
            projected,
            projected,
            opposite,
        )
        source_tolerance = native_curve_endpoint_operation_tolerance(
            source,
            projected,
            opposite,
        )
        projection_error = native_point_error(source, projected)
        payload = [*projected, 0.0, 0.0, 0.0]
        packed_status = native_evaluate2_evidence(
            [*payload, packed_evaluate_status(1)]
        )["packed_status"]
        success = packed_evaluate_status(1)
        evidence = {
            "u": 0.0,
            "u_parameter_rule": "native-u-point-parameter-authoritative-v2",
            "parameter_u_source": "get_parameter",
            "fallback_reason": None,
            "rule_version": NATIVE_U_IDENTITY_RULE,
            "point_roundtrip_rule": (
                "native-curve-endpoint-float32-operation-v4"
            ),
            "projection_rule": "tessellation-edge-projection-v1",
            "projection_tolerance_m": 50e-6,
            "source_point_m": list(source),
            "projection_error_m": projection_error,
            "point_tolerance_m": projected_tolerance,
            "closest_point_error_m": projection_error,
            "evaluate_point_error_m": 0.0,
            "source_evaluate_error_m": projection_error,
            "closest_evaluate_error_m": 0.0,
            "closest_point_on_raw": [*projected, 0.0, 0.0],
            "parameter_input_point_m": list(projected),
            "parameter_raw": [0.0, success],
            "parameter_success_raw": success,
            "parameter_success_raw_type": type(success).__name__,
            "parameter_success_bit": 1,
            "parameter_success_parsed": True,
            "evaluate_payload": payload,
            "evaluate_packed_status": packed_status,
        }

        self.assertNotEqual(projected_tolerance, source_tolerance)
        self.assertTrue(
            _valid_u_evidence(
                evidence,
                0.0,
                source,
                0.0,
                1.0,
                opposite,
                opposite,
            )
        )

    def test_parser_accepts_closest_u_fallback_for_exact_false_status(self):
        evidence, triangles = self.group_333_tessellation()
        edge = evidence["native_edges"][0]
        fin = next(
            row
            for row in evidence["itessellation"]["bodies"][0]["fins"]
            if row["classification"] == "native_edge"
            and native_u_equal(row["start_u"], edge["u_min"])
        )
        fin_evidence = fin["start_evidence"]
        fin_evidence["parameter_raw"][0] = edge["u_min"]
        fin_evidence["parameter_raw"][1] = 0.0
        fin_evidence["parameter_success_raw"] = 0.0
        fin_evidence["parameter_success_raw_type"] = "float"
        fin_evidence["parameter_success_bit"] = 0
        fin_evidence["parameter_success_parsed"] = False
        fin_evidence["u_parameter_rule"] = (
            "native-u-closest-point-fallback-v1"
        )
        fin_evidence["parameter_u_source"] = "closest_point_fallback"
        fin_evidence["fallback_reason"] = "get_parameter_false"
        fin_evidence["point_roundtrip_rule"] = (
            "tessellation-edge-projection-v1"
        )
        fin_evidence["point_tolerance_m"] = 50e-6

        spans = parse_native_tessellation(
            "part-1",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            None,
        )

        self.assertEqual(len(spans), 2)

    def test_parser_uses_primary_sense_when_secondary_is_write_only(self):
        evidence, triangles = self.group_333_tessellation()
        secondary = evidence["native_edges"][0]["face_senses"][0][
            "IEdgeInFaceSense2"
        ]
        secondary["value"] = None
        secondary["error"] = (
            "com_error: (-2147352567, 'write-only property', "
            "(61836, None, None, None, 0, None))"
        )

        spans = parse_native_tessellation(
            "part-1",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            triangles,
        )

        self.assertEqual(len(spans), 2)

    def test_parser_rejects_unrelated_secondary_sense_error(self):
        evidence, triangles = self.group_333_tessellation()
        secondary = evidence["native_edges"][0]["face_senses"][0][
            "IEdgeInFaceSense2"
        ]
        secondary["value"] = None
        secondary["error"] = (
            "com_error: (-2147352567, 'unexpected condition', "
            "(61837, None, None, None, 0, None))"
        )

        with self.assertRaises(PipelineError):
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                triangles,
            )

    def test_parser_accepts_face_local_cofin_vertices_with_equal_coordinates(self):
        evidence, triangles = self.group_333_tessellation()
        body = evidence["itessellation"]["bodies"][0]
        fins = {fin["fin_id"]: fin for fin in body["fins"]}
        facets = {facet["facet_id"]: facet for facet in body["facets"]}
        vertices = {
            vertex["vertex_id"]: vertex
            for vertex in evidence["itessellation"]["vertices"]
        }
        local_vertices = {}
        for replacement, source_id in ((21, 1), (22, 2)):
            row = dict(vertices[source_id])
            row["vertex_id"] = replacement
            local_vertices[replacement] = row
            evidence["itessellation"]["vertices"].append(row)
        body["vertex_count"] += 2
        replacements = {2: 22, 1: 21}
        for fin in body["fins"]:
            if fin["facet_id"] != 20:
                continue
            fin["vertex_ids"] = [
                replacements.get(vertex_id, vertex_id)
                for vertex_id in fin["vertex_ids"]
            ]
        facets[20]["vertex_ids"] = sorted(
            replacements.get(vertex_id, vertex_id)
            for vertex_id in facets[20]["vertex_ids"]
        )

        spans = parse_native_tessellation(
            "part-1",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            None,
        )

        self.assertEqual(len(spans), 2)
        self.assertNotEqual(
            fins[100]["vertex_ids"],
            list(reversed(fins[200]["vertex_ids"])),
        )
        self.assertIn(21, local_vertices)

    def test_parser_rejects_fallback_with_source_evaluate_error_over_50_micrometres(self):
        evidence, triangles = self.group_333_tessellation()
        edge = evidence["native_edges"][0]
        fin = next(
            row
            for row in evidence["itessellation"]["bodies"][0]["fins"]
            if row["classification"] == "native_edge"
            and native_u_equal(row["start_u"], edge["u_min"])
        )
        fin_evidence = fin["start_evidence"]
        source = list(fin_evidence["source_point_m"])
        source[0] += 60e-6
        fin_evidence["source_point_m"] = source
        fin_evidence["projection_error_m"] = 60e-6
        fin_evidence["closest_point_error_m"] = 60e-6
        fin_evidence["source_evaluate_error_m"] = 60e-6
        fin_evidence["parameter_raw"][1] = 0.0
        fin_evidence["parameter_success_raw"] = 0.0
        fin_evidence["parameter_success_raw_type"] = "float"
        fin_evidence["parameter_success_bit"] = 0
        fin_evidence["parameter_success_parsed"] = False
        fin_evidence["u_parameter_rule"] = (
            "native-u-closest-point-fallback-v1"
        )
        fin_evidence["parameter_u_source"] = "closest_point_fallback"
        fin_evidence["fallback_reason"] = "get_parameter_false"

        with self.assertRaises(PipelineError):
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                None,
            )

    def test_parser_rejects_fallback_with_closest_u_outside_domain(self):
        evidence, triangles = self.group_333_tessellation()
        edge = evidence["native_edges"][0]
        fin = next(
            row
            for row in evidence["itessellation"]["bodies"][0]["fins"]
            if row["classification"] == "native_edge"
        )
        fin_evidence = fin["start_evidence"]
        fin_evidence["closest_point_on_raw"][3] = edge["u_max"] + 1.0
        fin_evidence["parameter_raw"][1] = 0.0
        fin_evidence["parameter_success_raw"] = 0.0
        fin_evidence["parameter_success_raw_type"] = "float"
        fin_evidence["parameter_success_bit"] = 0
        fin_evidence["parameter_success_parsed"] = False
        fin_evidence["u_parameter_rule"] = (
            "native-u-closest-point-fallback-v1"
        )
        fin_evidence["parameter_u_source"] = "closest_point_fallback"
        fin_evidence["fallback_reason"] = "get_parameter_false"

        with self.assertRaises(PipelineError):
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                None,
            )

    def test_parser_rejects_fallback_with_malformed_parameter_result(self):
        evidence, triangles = self.group_333_tessellation()
        fin = next(
            row
            for row in evidence["itessellation"]["bodies"][0]["fins"]
            if row["classification"] == "native_edge"
        )
        fin_evidence = fin["start_evidence"]
        fin_evidence["parameter_raw"] = [fin_evidence["u"]]
        fin_evidence["parameter_success_raw"] = 0.0
        fin_evidence["parameter_success_raw_type"] = "float"
        fin_evidence["parameter_success_bit"] = 0
        fin_evidence["parameter_success_parsed"] = False
        fin_evidence["u_parameter_rule"] = (
            "native-u-closest-point-fallback-v1"
        )
        fin_evidence["parameter_u_source"] = "closest_point_fallback"
        fin_evidence["fallback_reason"] = "get_parameter_false"

        with self.assertRaises(PipelineError):
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                None,
            )

    def test_parser_rejects_group_333_operation_bound_overshoot(self):
        evidence, triangles = self.group_333_tessellation()
        evaluation = evidence["native_edges"][0][
            "curve_endpoint_evaluations"
        ]["start"]
        evaluation["payload"][0] += 1e-6

        with self.assertRaises(PipelineError) as caught:
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                triangles,
            )

        self.assertIn("not independently reproducible", str(caught.exception))

    def test_parser_rejects_one_failed_curve_endpoint_evaluation(self):
        evidence, triangles = self.group_333_tessellation()
        evaluation = evidence["native_edges"][0][
            "curve_endpoint_evaluations"
        ]["end"]
        evaluation["packed_status"]["parsed_success"] = False

        with self.assertRaises(PipelineError) as caught:
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                triangles,
            )

        self.assertIn("endpoint evaluation is malformed", str(caught.exception))

    def test_parser_rejects_malformed_fin_u_payload_without_type_error(self):
        evidence, triangles = self.group_333_tessellation()
        fin = next(
            row
            for row in evidence["itessellation"]["bodies"][0]["fins"]
            if row["classification"] == "native_edge"
        )
        fin["start_evidence"]["evaluate_payload"] = None

        with self.assertRaises(PipelineError) as caught:
            parse_native_tessellation(
                "part-1",
                evidence,
                {"solid": 1, "sheet": 0},
                len(triangles),
                triangles,
            )

        self.assertIn("U evidence is not independently reproducible", str(caught.exception))

    def test_reverse_sense_requires_directed_topology_mapping(self):
        body, _face_10, _face_20, edge, _tessellation, extension = self.fixture()
        edge.sense = False

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)

        self.assertEqual(
            caught.exception.code,
            "cad_native_tessellation_unreadable",
        )
        self.assertIn(
            "topology endpoints disagree with curve parameter direction",
            caught.exception.message,
        )

    def test_topology_pairing_accepts_39nm_native_point_wobble(self):
        start = (
            -0.005322370050934642,
            0.00013700629315608413,
            -0.0021180644098793633,
        )
        tessellation, _triangles = self.group_333_tessellation(
            topology_start=(
                start[0] + 3.936764624706823e-8,
                start[1] - 1.1517504897859e-8,
                start[2] - 9.72215465027256e-11,
            )
        )

        assignment = tessellation["native_edges"][0][
            "topology_endpoint_assignment"
        ]
        self.assertTrue(assignment["accepted"])
        self.assertEqual(assignment["result"], "sense_directed")
        self.assertLess(assignment["direct_error_m"], 50e-6)
        self.assertGreater(assignment["swapped_error_m"], 50e-6)
        self.assertGreater(assignment["wrong_assignment_margin_m"], 0.0)

    def test_topology_pairing_rejects_swapped_edge_vertices(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        start, end, _evaluated_start, _evaluated_end = self.configure_group_333(
            edge,
            tessellation,
        )
        edge.vertices = [FakeVertex(end), FakeVertex(start)]
        backend = SolidWorksBackend()
        backend._source_components = {"part-1": (object(), "Default")}
        backend._captured_document = lambda *_args, **_kwargs: SimpleNamespace(
            Extension=extension
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            native_module,
            "_read_curve_end_params",
            fake_read_end_params,
        ), self.assertRaises(CadError) as caught:
            backend._write_component_mesh(
                "part-1",
                Path(directory) / "mesh.stl",
                FakeHolder(body),
            )

        self.assertEqual(
            caught.exception.code,
            "cad_native_tessellation_unreadable",
        )
        self.assertIn(
            "edge topology endpoints disagree with curve parameter direction",
            caught.exception.message,
        )
        assignment = caught.exception.detail["topology_endpoint_assignment"]
        self.assertFalse(assignment["accepted"])
        self.assertEqual(assignment["result"], "swapped_only")

    def test_producer_defers_ambiguous_topology_to_collapsed_parser_gate(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        start = (0.0, 0.0, 0.0)
        end = (20e-6, 0.0, 0.0)
        edge.u_min = 2.667937521368035e-7
        edge.u_max = 0.0005305172218399963
        edge.curve = FakeCurve(
            -10.0,
            10.0,
            is_closed=False,
            is_periodic=False,
        )
        edge.parameter_success = True
        edge.curve_start_point = list(start)
        edge.curve_end_point = list(end)
        edge.vertices = [FakeVertex(start), FakeVertex(end)]
        tessellation.GetVertexPoint = lambda vertex_id: {
            1: start,
            2: end,
            3: (0.0, 1.0, 0.0),
            4: (1.0, 1.0, 0.0),
        }[vertex_id]
        u_by_point = {start: edge.u_min, end: edge.u_max}
        edge.GetClosestPointOn = lambda x, y, z: [
            x,
            y,
            z,
            u_by_point[(x, y, z)],
            0.0,
        ]
        edge.GetParameter = lambda x, y, z: [u_by_point[(x, y, z)], True]
        edge.evaluate_points = [start, end]
        edge.evaluate_u_points = {edge.u_min: start, edge.u_max: end}
        backend = SolidWorksBackend()
        backend._source_components = {"part-1": (object(), "Default")}
        backend._captured_document = lambda *_args, **_kwargs: SimpleNamespace(
            Extension=extension
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            native_module,
            "_read_curve_end_params",
            fake_read_end_params,
        ):
            entry = backend._write_component_mesh(
                "part-1",
                Path(directory) / "mesh.stl",
                FakeHolder(body),
            )

        assignment = entry["tessellation"]["native_edges"][0][
            "topology_endpoint_assignment"
        ]
        self.assertEqual(assignment["result"], "ambiguous")
        self.assertFalse(assignment["accepted"])

    def test_curve_endpoint_roles_separate_parameter_order_from_sense(self):
        start = (0.0, 1.0, 2.0)
        end = (3.0, 4.0, 5.0)

        forward = native_curve_endpoint_roles(start, end, True)
        reverse = native_curve_endpoint_roles(start, end, False)

        self.assertEqual(forward, ((start, end), (start, end)))
        self.assertEqual(reverse, ((start, end), (end, start)))

    def test_closed_absent_endpoints_parser_derives_curve_closure(self):
        body, face_10, face_20, _edge, _tessellation, extension = self.fixture()
        _edge.curve = FakeCurve(-10.0, 10.0, is_closed=False)
        _edge.parameter_success = 1.0
        backend = SolidWorksBackend()
        backend._source_components = {"part-1": (object(), "Default")}
        backend._captured_document = lambda *_args, **_kwargs: SimpleNamespace(
            Extension=extension
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            native_module,
            "_read_curve_end_params",
            fake_read_end_params,
        ):
            destination = Path(directory) / "mesh.stl"
            entry = backend._write_component_mesh(
                "part-1",
                destination,
                FakeHolder(body),
            )
            tessellation = entry["tessellation"]
            self.assertEqual(
                set(tessellation),
                {
                    "schema_version",
                    "faces",
                    "face_spans",
                    "native_edges",
                    "no_facets_faces",
                    "itessellation",
                },
            )
            self.assertEqual(
                tessellation["schema_version"],
                NATIVE_TESSELLATION_SCHEMA,
            )
            self.assertIn("ITessellation", entry["used_api"])
            self.assertNotIn("IFace2.GetTessTriangles", entry["used_api"])
            self.assertEqual(face_10.display_calls, 0)
            self.assertEqual(face_20.display_calls, 0)
            triangle_vertices = self.stl_triangles(destination)
            self.assertEqual(len(triangle_vertices), 2)
            self.assert_directed_cycle(
                triangle_vertices[0],
                (POINTS[1], POINTS[2], POINTS[3]),
            )
            self.assert_directed_cycle(
                triangle_vertices[1],
                (POINTS[2], POINTS[1], POINTS[4]),
            )
            spans = parse_native_tessellation(
                "part-1",
                tessellation,
                entry["bodies"],
                entry["triangles"],
                triangle_vertices,
            )
            self.assertEqual(len(spans), 2)
            self.assertEqual(
                [
                    facet["triangle_record_index"]
                    for facet in tessellation["itessellation"]["bodies"][0][
                        "facets"
                    ]
                ],
                [0, 1],
            )
            edge = tessellation["native_edges"][0]
            self.assertEqual(edge["start_vertex_state"], "present")
            self.assertEqual(edge["end_vertex_state"], "present")
            self.assertIs(edge["endpoint_points_derived_from_curve"], False)
            self.assertEqual((edge["u_min"], edge["u_max"]), (0.0, 1.0))
            self.assertEqual(
                [
                    row["EdgeInFaceSense"]["value"]
                    for row in edge["face_senses"]
                ],
                [False, True],
            )
            old_edge_points = {
                tuple(edge["curve_start_point_m"]),
                tuple(edge["curve_end_point_m"]),
            }
            derived = copy.deepcopy(tessellation)
            derived_edge = derived["native_edges"][0]
            derived_edge.update(
                {
                    "start_vertex_reference_sha256": None,
                    "end_vertex_reference_sha256": None,
                    "start_point_m": None,
                    "end_point_m": None,
                    "start_vertex_state": "absent",
                    "end_vertex_state": "absent",
                    "start_point_error": "vertex_absent",
                    "end_point_error": "vertex_absent",
                    "endpoint_points_derived_from_curve": True,
                    "curve_closed": True,
                    "curve_start_point_m": [0.0, 0.0, 0.0],
                    "curve_end_point_m": [0.0, 0.0, 0.0],
                }
            )
            derived_edge["curve_end_params"]["is_closed"] = True
            derived_edge["curve_end_params"]["raw_outputs"][2]["value"] = True
            derived["itessellation"]["solidworks_vertex_identities"] = []
            derived_edge["curve_domain_transform"] = (
                native_curve_domain_transform(
                    -10.0,
                    10.0,
                    0.0,
                    1.0,
                    True,
                    False,
                )
            )
            for name in ("start", "end"):
                evaluation = derived_edge["curve_endpoint_evaluations"][name]
                evaluation["payload"] = [0.0] * 6
                evaluation["point_tolerance_m"] = (
                    native_curve_endpoint_operation_tolerance(
                        [0.0, 0.0, 0.0],
                        [0.0, 0.0, 0.0],
                        [0.0, 0.0, 0.0],
                    )
                )
                evaluation["point_error_m"] = 0.0
            affected_vertices = set()
            derived_fins = derived["itessellation"]["bodies"][0]["fins"]
            for fin in derived_fins:
                if fin["fin_id"] not in derived_edge["fin_ids"]:
                    continue
                for vertex_id, evidence in zip(
                    fin["vertex_ids"],
                    (fin["start_evidence"], fin["end_evidence"]),
                    strict=True,
                ):
                    if native_u_equal(
                        evidence["u"],
                        derived_edge["u_min"],
                    ) or native_u_equal(
                        evidence["u"],
                        derived_edge["u_max"],
                    ):
                        affected_vertices.add(vertex_id)
                        evidence["closest_point_on_raw"][:3] = [0.0, 0.0, 0.0]
                        evidence["evaluate_payload"][:3] = [0.0, 0.0, 0.0]
                        evidence["source_point_m"] = [0.0, 0.0, 0.0]
                        evidence["parameter_input_point_m"] = [0.0, 0.0, 0.0]
                        evidence["projection_error_m"] = 0.0
                        evidence["source_evaluate_error_m"] = 0.0
                        evidence["point_tolerance_m"] = (
                            native_curve_endpoint_operation_tolerance(
                                [0.0, 0.0, 0.0],
                                [0.0, 0.0, 0.0],
                                [0.0, 0.0, 0.0],
                            )
                        )
                        evidence["closest_point_error_m"] = 0.0
                        evidence["evaluate_point_error_m"] = 0.0
                        evidence["closest_evaluate_error_m"] = 0.0
            for vertex in derived["itessellation"]["vertices"]:
                if vertex["vertex_id"] in affected_vertices:
                    vertex["point_m"] = [0.0, 0.0, 0.0]
            for index, triangle in enumerate(triangle_vertices):
                triangle_vertices[index] = tuple(
                    (0.0, 0.0, 0.0) if tuple(point) in old_edge_points else point
                    for point in triangle
                )
            support.refresh_topology_endpoint_assignment(derived)
            derived_spans = parse_native_tessellation(
                "part-1",
                derived,
                entry["bodies"],
                entry["triangles"],
                triangle_vertices,
            )
            self.assertEqual(len(derived_spans), 2)
            fins = {
                row["fin_id"]: row
                for row in tessellation["itessellation"]["bodies"][0]["fins"]
            }
            self.assertEqual((fins[100]["start_u"], fins[100]["end_u"]), (0.0, 1.0))
            self.assertEqual((fins[200]["start_u"], fins[200]["end_u"]), (1.0, 0.0))
            self.assertEqual(fins[100]["cofin_id"], 200)
            self.assertEqual(
                fins[100]["start_evidence"]["rule_version"],
                NATIVE_U_IDENTITY_RULE,
            )
            self.assertEqual(
                len(fins[100]["start_evidence"]["closest_point_on_raw"]),
                5,
            )
            self.assertEqual(
                fins[100]["end_evidence"]["parameter_raw"][1],
                1.0,
            )
            self.assertEqual(
                fins[100]["end_evidence"]["parameter_success_raw_type"],
                "float",
            )
            self.assertEqual(
                fins[100]["end_evidence"]["parameter_success_bit"],
                1,
            )
            self.assertIs(
                fins[100]["end_evidence"]["parameter_success_parsed"],
                True,
            )
            self.assertEqual(
                len(fins[100]["end_evidence"]["evaluate_payload"]),
                6,
            )
            self.assertTrue(
                fins[100]["end_evidence"]["evaluate_packed_status"][
                    "parsed_success"
                ]
            )
            self.assertIsNone(fins[101]["start_u"])
            self.assertIsNone(fins[101]["cofin_id"])
            broken = copy.deepcopy(tessellation)
            broken["itessellation"]["bodies"][0]["facets"][1][
                "triangle_record_index"
            ] = 0
            with self.assertRaises(PipelineError):
                parse_native_tessellation(
                    "part-1",
                    broken,
                    entry["bodies"],
                    entry["triangles"],
                    triangle_vertices,
                )

            def introduce_leading_gap(value):
                external = [
                    fin
                    for fin in value["itessellation"]["bodies"][0]["fins"]
                    if fin["cofin_id"] is not None
                ]
                for fin, field in ((external[0], "start_u"), (external[1], "end_u")):
                    fin[field] = 0.9
                    evidence_key = (
                        "start_evidence"
                        if field == "start_u"
                        else "end_evidence"
                    )
                    evidence = fin[evidence_key]
                    evidence["u"] = 0.9
                    evidence["closest_point_on_raw"][3] = 0.9
                    evidence["parameter_raw"][0] = 0.9

            def set_packed_bits(value, bits):
                status = value["itessellation"]["bodies"][0]["fins"][0][
                    "start_evidence"
                ]["evaluate_packed_status"]
                status.update(
                    {
                        "raw_bits_u64": bits,
                        "high32": bits >> 32,
                        "low32": bits & 0xFFFFFFFF,
                        "parsed_success": (bits & 0xFFFFFFFF) == 1,
                    }
                )

            for mutate in (
                lambda value: value["native_edges"][0].__setitem__("u_min", None),
                lambda value: value["itessellation"]["bodies"][0]["fins"][0].__setitem__(
                    "cofin_id",
                    100,
                ),
                introduce_leading_gap,
                lambda value: value["native_edges"][0]["face_senses"][0][
                    "EdgeInFaceSense"
                ].__setitem__("value", None),
                lambda value: value["native_edges"][0][
                    "curve_end_params"
                ].update(
                    {
                        "success": False,
                        "error": "unsupported GetEndParams return shape",
                        "start": None,
                        "end": None,
                        "is_closed": None,
                        "is_periodic": None,
                    }
                ),
                lambda value: set_packed_bits(value, 0x7FF8000000000000),
                lambda value: set_packed_bits(value, 0x3FF0000000000000),
                lambda value: value["itessellation"]["bodies"][0]["fins"][0][
                    "start_evidence"
                ].__setitem__(
                    "point_tolerance_m",
                    value["itessellation"]["bodies"][0]["fins"][0][
                        "start_evidence"
                    ]["point_tolerance_m"]
                    * 2.0,
                ),
            ):
                malformed = copy.deepcopy(tessellation)
                mutate(malformed)
                with self.assertRaises(PipelineError):
                    parse_native_tessellation(
                        "part-1",
                        malformed,
                        entry["bodies"],
                        entry["triangles"],
                        triangle_vertices,
                    )

    def test_production_capture_retries_critical_itessellation_reads(self):
        for api in (
            "GetFaceFacets",
            "GetFacetFins",
            "GetFinVertices",
            "GetVertexPoint",
        ):
            with self.subTest(api=api):
                (
                    body,
                    face_10,
                    face_20,
                    _edge,
                    tessellation,
                    extension,
                ) = self.fixture()
                tessellation.transient_remaining[api] = 1
                backend = SolidWorksBackend()
                backend._source_components = {
                    "part-1": (object(), "Default")
                }
                backend._captured_document = (
                    lambda *_args, _extension=extension, **_kwargs: SimpleNamespace(
                        Extension=_extension
                    )
                )
                with tempfile.TemporaryDirectory() as directory, patch(
                    "description_pipeline.sources.solidworks.native.time.sleep",
                    lambda _seconds: None,
                ), patch.object(
                    native_module,
                    "_read_curve_end_params",
                    fake_read_end_params,
                ):
                    entry = backend._write_component_mesh(
                        "part-1",
                        Path(directory) / "mesh.stl",
                        FakeHolder(body),
                    )
                self.assertGreaterEqual(tessellation.call_counts[api], 2)
                self.assertEqual(entry["triangles"], 2)
                self.assertEqual(face_10.display_calls, 0)
                self.assertEqual(face_20.display_calls, 0)

    def test_display_triangle_count_is_not_read_or_reconciled(self):
        body, face_10, face_20, _edge, _tessellation, extension = self.fixture()
        face_10.display_triangles = [0.0] * 18
        face_20.display_triangles = []

        captured = self.capture(body, extension)

        self.assertEqual(face_10.display_calls, 0)
        self.assertEqual(face_20.display_calls, 0)
        self.assertEqual(len(captured["triangle_records"]), 2)

    def test_direct_facet_cycle_defines_stl_orientation(self):
        body, face_10, face_20, _edge, _tessellation, extension = self.fixture()
        face_10.display_triangles = [
            *POINTS[1],
            *POINTS[3],
            *POINTS[2],
        ]
        face_20.display_triangles = []

        captured = self.capture(body, extension)

        self.assertEqual(face_10.display_calls, 0)
        self.assertEqual(face_20.display_calls, 0)
        self.assert_directed_cycle(
            captured["triangle_records"][0]["output_float32_points"],
            (POINTS[1], POINTS[2], POINTS[3]),
        )
        self.assert_directed_cycle(
            captured["triangle_records"][1]["output_float32_points"],
            (POINTS[2], POINTS[1], POINTS[4]),
        )

    def test_cross_kind_raw_reference_alias_keeps_typed_identity_separate(self):
        body, face_10, _face_20, _edge, _tessellation, extension = self.fixture()
        shared = hashlib.sha256(b"same").digest()
        extension.references[id(body)] = shared
        extension.references[id(face_10)] = shared

        captured = self.capture(body, extension)

        self.assertEqual(
            captured["body_reference"]["raw_sha256"],
            captured["faces"][0]["reference"]["raw_sha256"],
        )
        self.assertNotEqual(
            captured["body_reference"]["typed_sha256"],
            captured["faces"][0]["reference"]["typed_sha256"],
        )
        alias, = captured["cross_kind_raw_aliases"]
        self.assertEqual(alias["raw_sha256"], hashlib.sha256(shared).hexdigest())
        self.assertEqual(
            [row["kind"] for row in alias["owners"]],
            ["body", "face"],
        )

    def test_capture_wide_registry_enforces_typed_identity(self):
        registry = _NativeReferenceRegistry()
        raw = hashlib.sha256(b"shared-across-kinds").digest()
        raw_sha = hashlib.sha256(raw).hexdigest()
        registry.claim(
            kind="body",
            raw_sha256=raw_sha,
            typed_sha256=_typed_reference_sha256("body", raw),
            owner={"kind": "body", "body_index": 0},
            base={"component": "part-1"},
        )
        registry.claim(
            kind="face",
            raw_sha256=raw_sha,
            typed_sha256=_typed_reference_sha256("face", raw),
            owner={"kind": "face", "face_index": 0},
            base={"component": "part-1"},
        )
        alias, = registry.cross_kind_aliases()
        self.assertEqual(alias["raw_sha256"], raw_sha)
        with self.assertRaises(CadError) as caught:
            registry.claim(
                kind="face",
                raw_sha256=raw_sha,
                typed_sha256=_typed_reference_sha256("face", raw),
                owner={"kind": "face", "body_index": 1, "face_index": 0},
                base={"component": "part-1"},
            )
        self.assertEqual(caught.exception.code, "cad_persistent_reference_alias")

    def test_vertex_reference_reuse_requires_same_com_object(self):
        registry = _NativeReferenceRegistry()
        raw = hashlib.sha256(b"shared-vertex").digest()
        raw_sha = hashlib.sha256(raw).hexdigest()
        typed_sha = _typed_reference_sha256("vertex", raw)
        registry.claim(
            kind="vertex",
            raw_sha256=raw_sha,
            typed_sha256=typed_sha,
            owner={"kind": "vertex", "vertex_index": 0},
            base={"component": "part-1"},
        )
        registry.claim_scope(
            kind="vertex",
            raw_sha256=raw_sha,
            identity_sha256=hashlib.sha256(b"scope-a").hexdigest(),
            owner={"kind": "vertex", "vertex_index": 1},
            base={"component": "part-1"},
        )
        self.assertEqual(
            registry.alias_audit()["vertex"],
            (raw_sha,),
        )
        with self.assertRaises(CadError) as caught:
            registry.claim_scope(
                kind="vertex",
                raw_sha256=raw_sha,
                identity_sha256=hashlib.sha256(b"scope-a").hexdigest(),
                owner={"kind": "vertex", "vertex_index": 2},
                base={"component": "part-1"},
            )
        self.assertEqual(caught.exception.code, "cad_scope_identity_alias")
    def test_com_object_identity_uses_underlying_dispatch(self):
        native_object = object()
        wrapper_a = SimpleNamespace(_oleobj_=native_object)
        wrapper_b = SimpleNamespace(_oleobj_=native_object)
        self.assertEqual(
            _com_object_identity(wrapper_a),
            _com_object_identity(wrapper_b),
        )
        self.assertNotEqual(
            _com_object_identity(wrapper_a),
            _com_object_identity(SimpleNamespace(_oleobj_=object())),
        )

    def test_vertex_group_rejects_inconsistent_typed_reference(self):
        raw_reference = hashlib.sha256(b"raw-vertex").hexdigest()
        with self.assertRaisesRegex(
            ValueError,
            "inconsistent typed references",
        ):
            _group_vertex_observations(
                [
                    (
                        (0.0, 0.0, 0.0),
                        raw_reference,
                        hashlib.sha256(b"typed-a").hexdigest(),
                        0,
                        0,
                        0x1000,
                    ),
                    (
                        (0.0, 0.0, 0.0),
                        raw_reference,
                        hashlib.sha256(b"typed-b").hexdigest(),
                        1,
                        0,
                        0x1001,
                    ),
                ]
            )

    def test_scope_mapping_rejects_duplicate_topology_and_keeps_coincident_vertices(self):
        def edge_row(edge_index, *, start_pointer, start_reference, end_x):
            return {
                "edge_index": edge_index,
                "reference_sha256": hashlib.sha256(
                    f"edge-{edge_index}".encode()
                ).hexdigest(),
                "endpoints": {
                    "start": {
                        "state": "present",
                        "object_pointer": start_pointer,
                        "reference_sha256": start_reference,
                        "point_m": [0.0, 0.0, 0.0],
                    },
                    "end": {
                        "state": "present",
                        "object_pointer": start_pointer + 100,
                        "reference_sha256": hashlib.sha256(
                            f"end-{edge_index}".encode()
                        ).hexdigest(),
                        "point_m": [end_x, 0.0, 0.0],
                    },
                },
                "face_indices": [0, 1],
                "u_min": 0.0,
                "u_max": 1.0,
                "curve_start_point_m": [0.0, 0.0, 0.0],
                "curve_end_point_m": [1.0, 0.0, 0.0],
                "curve_sense": True,
                "curve_type": 0,
                "curve_closed": False,
                "curve_periodic": False,
            }

        first = edge_row(
            0,
            start_pointer=1,
            start_reference="1" * 64,
            end_x=1.0,
        )
        second = edge_row(
            1,
            start_pointer=2,
            start_reference="2" * 64,
            end_x=2.0,
        )
        vertices = _scope_vertex_rows([first, second])
        coincident = [
            row
            for row in vertices
            if row["point_m"] == [0.0, 0.0, 0.0]
        ]
        self.assertEqual(
            [
                row["coincident_occurrence_index"]
                for row in coincident
            ],
            [0, 1],
        )

        duplicate = copy.deepcopy(first)
        duplicate["edge_index"] = 2
        duplicate["mapping_index"] = 2
        first["mapping_index"] = 0
        first["mapping_key"] = _scope_edge_mapping_key(first, [0, 1])
        duplicate["mapping_key"] = _scope_edge_mapping_key(
            duplicate,
            [0, 1],
        )
        with self.assertRaises(ValueError):
            _scope_bijection(
                [first, duplicate],
                [copy.deepcopy(first)],
            )

    def test_fin_edges_bind_exact_rows_across_aliased_references(self):
        shared_reference = {
            "raw_sha256": "1" * 64,
            "typed_sha256": "2" * 64,
        }
        first = {"index": 0, "reference": dict(shared_reference)}
        second = {"index": 1, "reference": dict(shared_reference)}

        mapping = _fin_edge_row_map(
            [first, second],
            {0: (10, 11), 1: (12, 13)},
        )

        self.assertIs(mapping[10], first)
        self.assertIs(mapping[11], first)
        self.assertIs(mapping[12], second)
        self.assertIs(mapping[13], second)
        with self.assertRaisesRegex(
            ValueError,
            "multiple native edges",
        ):
            _fin_edge_row_map(
                [first, second],
                {0: (10, 11), 1: (11, 12)},
            )

    def test_scope_vertex_identity_uses_point_and_reference_not_address(self):
        def edge_row(edge_index, start, end):
            return {
                "edge_index": edge_index,
                "endpoints": {
                    "start": {
                        "state": "present",
                        "object_pointer": start[2],
                        "reference_sha256": start[1],
                        "point_m": start[0],
                    },
                    "end": {
                        "state": "present",
                        "object_pointer": end[2],
                        "reference_sha256": end[1],
                        "point_m": end[0],
                    },
                },
            }

        first_reference = hashlib.sha256(b"first-vertex").hexdigest()
        second_reference = hashlib.sha256(b"second-vertex").hexdigest()
        rows = _scope_vertex_rows(
            [
                edge_row(
                    0,
                    ([0.0, 0.0, 0.0], first_reference, 0x1000),
                    ([1.0, 0.0, 0.0], second_reference, 0x1000),
                ),
                edge_row(
                    1,
                    ([0.0, 0.0, 0.0], first_reference, 0x1001),
                    ([2.0, 0.0, 0.0], second_reference, 0x1001),
                ),
                edge_row(
                    2,
                    (
                        [0.0, 0.0, 0.0],
                        hashlib.sha256(b"coincident-vertex").hexdigest(),
                        0x1000,
                    ),
                    (
                        [3.0, 0.0, 0.0],
                        hashlib.sha256(b"third-end").hexdigest(),
                        0x1000,
                    ),
                ),
            ]
        )

        coincident = [
            row for row in rows if row["point_m"] == [0.0, 0.0, 0.0]
        ]
        self.assertEqual(len(coincident), 2)
        merged = next(
            row
            for row in coincident
            if row["reference_sha256"] == first_reference
        )
        self.assertEqual(merged["object_pointers"], [0x1000, 0x1001])
        self.assertEqual(
            sorted(
                tuple(row["point_m"])
                for row in rows
                if row["reference_sha256"] == second_reference
            ),
            [(1.0, 0.0, 0.0), (2.0, 0.0, 0.0)],
        )

    def test_scope_vertex_mapping_uses_mapped_edge_incidence(self):
        def edge_row(edge_index, start_reference, end_x):
            return {
                "edge_index": edge_index,
                "endpoints": {
                    "start": {
                        "state": "present",
                        "object_pointer": 0x2000 + edge_index,
                        "reference_sha256": start_reference,
                        "point_m": [0.0, 0.0, 0.0],
                    },
                    "end": {
                        "state": "present",
                        "object_pointer": 0x2100 + edge_index,
                        "reference_sha256": hashlib.sha256(
                            f"end-{edge_index}".encode()
                        ).hexdigest(),
                        "point_m": [end_x, 0.0, 0.0],
                    },
                },
            }

        first = hashlib.sha256(b"first").hexdigest()
        second = hashlib.sha256(b"second").hexdigest()
        component_rows = _scope_vertex_rows(
            [edge_row(0, first, 1.0), edge_row(1, second, 2.0)],
            {0: 1, 1: 0},
        )
        part_rows = _scope_vertex_rows(
            [edge_row(0, second, 2.0), edge_row(1, first, 1.0)]
        )

        component_by_reference = {
            row["reference_sha256"]: row["mapping_key"]
            for row in component_rows
            if row["point_m"] == [0.0, 0.0, 0.0]
        }
        part_by_reference = {
            row["reference_sha256"]: row["mapping_key"]
            for row in part_rows
            if row["point_m"] == [0.0, 0.0, 0.0]
        }
        self.assertEqual(component_by_reference, part_by_reference)
        self.assertEqual(
            component_by_reference[first][-1],
            (1,),
        )
        self.assertEqual(
            component_by_reference[second][-1],
            (0,),
        )
        self.assertEqual(
            {
                row["reference_sha256"]: row["vertex_index"]
                for row in component_rows
                if row["point_m"] == [0.0, 0.0, 0.0]
            },
            {
                row["reference_sha256"]: row["vertex_index"]
                for row in part_rows
                if row["point_m"] == [0.0, 0.0, 0.0]
            },
        )

    def test_scope_snapshot_accepts_only_both_absent_closed_endpoints(self):
        body, _face_10, _face_20, edge, _tessellation, extension = (
            self.fixture()
        )
        face_rows = _scope_face_rows(body.faces, extension)
        edge.vertices = [None, None]
        edge.curve = FakeCurve(0.0, 1.0, is_closed=True)
        with patch.object(
            native_module,
            "_read_curve_end_params",
            fake_read_end_params,
        ):
            rows = _scope_edge_rows(body, extension, face_rows)
        self.assertEqual(
            {
                rows[0]["endpoints"]["start"]["state"],
                rows[0]["endpoints"]["end"]["state"],
            },
            {"absent"},
        )
        self.assertEqual(_scope_vertex_rows(rows), [])
        _scope_edge_mapping_key(rows[0], rows[0]["face_indices"])

        edge.vertices = [FakeVertex((0.0, 0.0, 0.0)), None]
        with patch.object(
            native_module,
            "_read_curve_end_params",
            fake_read_end_params,
        ), self.assertRaises(ValueError):
            _scope_edge_rows(body, extension, face_rows)

    def test_scope_mapping_retains_live_vertex_dispatches(self):
        body, _face_10, _face_20, edge, _tessellation, extension = (
            self.fixture()
        )
        face_rows = _scope_face_rows(body.faces, extension)
        with patch.object(
            native_module,
            "_read_curve_end_params",
            fake_read_end_params,
        ):
            rows = _scope_edge_rows(body, extension, face_rows)
        self.assertIs(
            rows[0]["endpoints"]["start"]["native_object"],
            edge.vertices[0],
        )
        self.assertIs(
            rows[0]["endpoints"]["end"]["native_object"],
            edge.vertices[1],
        )

    def test_part_identity_witness_is_globally_unique_across_bodies(self):
        reference = hashlib.sha256(b"shared-part-witness").hexdigest()
        rows = [
            {
                "identity": {
                    "mapping_source_body_reference_sha256": reference,
                    "mapping_source_face_index": 0,
                    "mapped_source_face_persistent_reference_sha256":
                        hashlib.sha256(b"part-face-0").hexdigest(),
                }
            },
            {
                "identity": {
                    "mapping_source_body_reference_sha256": reference,
                    "mapping_source_face_index": 1,
                    "mapped_source_face_persistent_reference_sha256":
                        hashlib.sha256(b"part-face-1").hexdigest(),
                }
            },
        ]
        self.assertTrue(
            native_mapped_witnesses_are_global_unique(
                rows,
                "mapping_source_face_index",
                "mapped_source_face_persistent_reference_sha256",
            )
        )
        rows[1]["identity"][
            "mapped_source_face_persistent_reference_sha256"
        ] = rows[0]["identity"][
            "mapped_source_face_persistent_reference_sha256"
        ]
        rows[1]["identity"]["mapping_source_face_index"] = 0
        self.assertFalse(
            native_mapped_witnesses_are_global_unique(
                rows,
                "mapping_source_face_index",
                "mapped_source_face_persistent_reference_sha256",
            )
        )

    def test_scope_witness_stays_in_the_owned_document_session(self):
        body, _face_10, _face_20, _edge, _tessellation, extension = (
            self.fixture()
        )

        class OwnedDocument:
            Extension = extension

            def OpenDoc6(self, *_args):  # noqa: N802 - SolidWorks API name
                raise AssertionError("the witness path must not open a document")

            def CloseDoc(self, *_args):  # noqa: N802 - SolidWorks API name
                raise AssertionError("the witness path must not close the document")

        document = OwnedDocument()
        backend = SolidWorksBackend()
        document_key = object()
        captured = []
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "witness.SLDPRT"
            source.write_bytes(b"frozen source")
            backend._source_components = {
                "part-1": (document_key, "Default")
            }
            backend._source_documents = {
                document_key: (
                    str(source),
                    "source",
                    "Default",
                    document,
                )
            }

            def captured_document(*args):
                captured.append(args)
                return document

            backend._captured_document = captured_document
            with patch.object(
                native_module,
                "_part_bodies",
                lambda *_args: [body],
            ), patch.object(
                native_module,
                "_temporary_configuration",
                lambda *_args, **_kwargs: nullcontext(
                    (document, "Default")
                ),
            ):
                witnesses = backend._scope_witnesses_for_body(
                    "part-1",
                    0,
                    0,
                    body,
                    body.faces,
                )

        self.assertEqual(len(captured), 1)
        self.assertEqual(
            set(witnesses),
            {"face", "edge", "vertex"},
        )
        self.assertEqual(
            backend.notes["scope_witness:part-1:0:0"][
                "document_ownership"
            ],
            "existing_owned_session_no_close",
        )

    def test_vertex_alias_with_persistent_edges_round_trips(self):
        body, _face_10, _face_20, _edge, _tessellation, extension = (
            self.fixture()
        )
        shared_vertex_reference = hashlib.sha256(
            b"shared-vertex-reference"
        ).digest()

        def reference(value):
            if isinstance(value, FakeVertex):
                return list(shared_vertex_reference)
            return list(
                hashlib.sha256(
                    f"native-{id(value)}".encode()
                ).digest()
            )

        extension.GetPersistReference3 = reference
        backend = SolidWorksBackend()
        document_key = object()
        source_path = Path(__file__).resolve()
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        part_body_reference = hashlib.sha256(
            b"persistent-edge-part-body"
        ).hexdigest()
        edge_witness = {
            "part_document_sha256": source_sha256,
            "part_body_reference_sha256": part_body_reference,
            "edge_index": 0,
            "persistent_reference_sha256": hashlib.sha256(
                b"part-edge-0"
            ).hexdigest(),
        }
        backend._source_components = {
            "part-1": (document_key, "Default")
        }
        backend._source_documents = {
            document_key: (
                str(source_path),
                "source",
                "Default",
                object(),
            )
        }
        backend._scope_witnesses_override = lambda *_args: {
            "face": {},
            "edge": {0: edge_witness},
            "vertex": {
                index: {
                    "part_document_sha256": source_sha256,
                    "part_body_reference_sha256": part_body_reference,
                    "vertex_index": index,
                    "persistent_reference_sha256": hashlib.sha256(
                        f"part-vertex-{index}".encode()
                    ).hexdigest(),
                }
                for index in range(2)
            },
        }
        backend._captured_document = lambda *_args, **_kwargs: (
            SimpleNamespace(Extension=extension)
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            native_module,
            "_read_curve_end_params",
            fake_read_end_params,
        ):
            destination = Path(directory) / "vertex-alias.stl"
            entry = backend._write_component_mesh(
                "part-1",
                destination,
                FakeHolder(body),
            )
            triangles = self.stl_triangles(destination)
            native_edge = entry["tessellation"]["native_edges"][0]
            self.assertIsNone(native_edge["edge_identity"])
            self.assertEqual(
                native_edge["mapping_source_edge_index"],
                0,
            )
            self.assertEqual(
                native_edge[
                    "mapping_source_edge_persistent_reference_sha256"
                ],
                edge_witness["persistent_reference_sha256"],
            )
            parse_native_tessellation(
                "part-1",
                entry["tessellation"],
                entry["bodies"],
                entry["triangles"],
                triangles,
            )

            vertex_row = next(
                row
                for row in entry["tessellation"]["itessellation"][
                    "solidworks_vertex_identities"
                ]
                if row["identity"] is not None
            )
            self.assertEqual(
                vertex_row["identity"][
                    "incident_mapped_edge_indices"
                ],
                [0],
            )
            self.assertEqual(
                vertex_row["identity"][
                    "incident_mapped_edge_reference_sha256"
                ],
                [edge_witness["persistent_reference_sha256"]],
            )

            forged = copy.deepcopy(entry["tessellation"])
            forged_vertex = next(
                row
                for row in forged["itessellation"][
                    "solidworks_vertex_identities"
                ]
                if row["identity"] is not None
            )
            forged_vertex["identity"][
                "incident_mapped_edge_reference_sha256"
            ] = ["0" * 64]
            forged_vertex["identity_sha256"] = (
                native_scope_identity_sha256(
                    forged_vertex["identity"]
                )
            )
            with self.assertRaises(PipelineError):
                parse_native_tessellation(
                    "part-1",
                    forged,
                    entry["bodies"],
                    entry["triangles"],
                    triangles,
                )

    def test_same_kind_edge_alias_uses_mapped_scope_identity(self):
        triangles = []
        spans = []
        body_reference = hashlib.sha256(b"edge-alias-body").hexdigest()
        for block in range(2):
            origin = float(block * 10)
            vertices = (
                (origin, 0.0, 0.0),
                (origin + 1.0, 0.0, 0.0),
                (origin, 1.0, 0.0),
                (origin + 1.0, 1.0, 0.0),
            )
            for triangle in (
                (vertices[0], vertices[1], vertices[2]),
                (vertices[1], vertices[0], vertices[3]),
            ):
                face_index = len(spans)
                triangles.append(triangle)
                spans.append(
                    {
                        "body_type": 0,
                        "body_index": 0,
                        "body_name": "edge-alias-body",
                        "body_reference_sha256": body_reference,
                        "face_index": face_index,
                        "face_reference_sha256": hashlib.sha256(
                            f"edge-alias-face-{face_index}".encode()
                        ).hexdigest(),
                        "face_owner_id": face_index + 1,
                        "edge_references": [],
                        "triangle_start": face_index,
                        "triangle_count": 1,
                        "triangle_records_sha256": "0" * 64,
                    }
                )
        shared_edge_reference = hashlib.sha256(
            b"body-scope-edge-alias"
        ).hexdigest()
        shared_vertex_reference = hashlib.sha256(
            b"body-scope-vertex-alias"
        ).hexdigest()
        native_edges = []
        for edge_index, first_face in enumerate((0, 2)):
            origin = float((edge_index) * 10)
            native_edges.append(
                {
                    "body_type": 0,
                    "body_index": 0,
                    "edge_reference_sha256": shared_edge_reference,
                    "start_vertex_reference_sha256":
                        shared_vertex_reference,
                    "end_vertex_reference_sha256":
                        shared_vertex_reference,
                    "start_point_m": [origin, 0.0, 0.0],
                    "end_point_m": [origin + 1.0, 0.0, 0.0],
                    "face_indices": [first_face, first_face + 1],
                    "u_min": 0.0,
                    "u_max": 1.0,
                    "curve_sense": True,
                    "curve_type": 0,
                }
            )
        evidence = support.native_tessellation_fixture(
            spans,
            native_edges,
            triangle_vertices=triangles,
        )
        source_sha256 = "a" * 64
        part_body_reference = hashlib.sha256(
            b"edge-alias-part-body"
        ).hexdigest()
        owner_scope = {
            "occurrence": "edge-alias",
            "source_document_path": "C:/edge-alias.SLDPRT",
            "source_document_sha256": source_sha256,
            "source_document_identity_sha256": source_sha256,
            "source_document_identity_kind": "file_sha256",
            "active_configuration": "Default",
            "referenced_configuration": "Default",
        }
        owner_scope_key = _scope_owner_key(
            owner_scope,
            body_type=0,
            body_index=0,
            body_name="edge-alias-body",
        )
        vertex_rows = evidence["itessellation"][
            "solidworks_vertex_identities"
        ]
        vertex_identity_by_point = {}
        for vertex_index, row in enumerate(vertex_rows):
            point = row["point_m"]
            incident_edges = []
            for edge_index, edge in enumerate(evidence["native_edges"]):
                if (
                    point == edge["start_point_m"]
                    or point == edge["end_point_m"]
                ):
                    incident_edges.append(edge_index)
            incident_part_edges = sorted(incident_edges)
            incident_part_references = sorted(
                hashlib.sha256(
                    f"part-edge-{edge_index}".encode()
                ).hexdigest()
                for edge_index in incident_part_edges
            )
            point_bits = [
                struct.pack("<d", float(value)).hex()
                for value in point
            ]
            topology_key = {
                "point_binary64": point_bits,
                "coincident_occurrence_index": 0,
                "incident_mapped_edge_indices": incident_part_edges,
                "incident_mapped_edge_reference_sha256":
                    incident_part_references,
            }
            identity = {
                "rule_version": "native-scope-entity-id-v1",
                "entity_kind": "vertex",
                "identity_kind": "mapped_scope_local",
                "persistent": False,
                "owner_scope_key": owner_scope_key,
                "source_document_sha256": source_sha256,
                "native_vertex_index": vertex_index,
                "point_binary64_hex": point_bits,
                "coincident_occurrence_index": 0,
                "incident_mapped_edge_indices": incident_part_edges,
                "incident_mapped_edge_reference_sha256":
                    incident_part_references,
                "topology_signature_sha256":
                    native_scope_identity_sha256(topology_key),
                "mapping_source_part_document_sha256": source_sha256,
                "mapping_source_body_reference_sha256":
                    part_body_reference,
                "mapping_source_vertex_index": vertex_index,
                "mapped_source_vertex_persistent_reference_sha256":
                    hashlib.sha256(
                        f"part-vertex-{vertex_index}".encode()
                    ).hexdigest(),
                "mapping_method": NATIVE_SCOPE_VERTEX_MAPPING_METHOD,
                "source_authority": NATIVE_SCOPE_SOURCE_AUTHORITY,
                "warnings": [
                    "cad_component_persistent_reference_aliased",
                    "cad_part_identity_witness_derived",
                    "cad_scope_local_identity_derived",
                    "cad_topology_identity_derived",
                ],
            }
            identity_sha256 = native_scope_identity_sha256(identity)
            row.update(
                {
                    "identity_mode": "native-scope-entity-id-v1",
                    "identity": identity,
                    "identity_sha256": identity_sha256,
                    "native_reference_sha256":
                        shared_vertex_reference,
                }
            )
            vertex_identity_by_point[tuple(point)] = identity_sha256
        for edge in evidence["native_edges"]:
            for endpoint in ("start", "end"):
                point = tuple(edge[f"{endpoint}_point_m"])
                edge[f"{endpoint}_vertex_reference_sha256"] = (
                    vertex_identity_by_point[point]
                )

        fins = {
            row["fin_id"]: row
            for row in evidence["itessellation"]["bodies"][0]["fins"]
        }
        for edge_index, edge in enumerate(evidence["native_edges"]):
            _parameter_endpoints, topology_endpoints = (
                native_curve_endpoint_roles(
                    edge["curve_start_point_m"],
                    edge["curve_end_point_m"],
                    edge["curve_sense"],
                )
            )
            identity_start_point = (
                edge["start_point_m"]
                if edge["start_point_m"] is not None
                else topology_endpoints[0]
            )
            identity_end_point = (
                edge["end_point_m"]
                if edge["end_point_m"] is not None
                else topology_endpoints[1]
            )
            edge_identity = {
                "rule_version": "native-scope-entity-id-v1",
                "entity_kind": "edge",
                "identity_kind": "mapped_scope_local",
                "persistent": False,
                "owner_scope_key": owner_scope_key,
                "source_document_sha256": source_sha256,
                "native_edge_index": edge_index,
                "start_point_binary64_hex": [
                    struct.pack("<d", float(value)).hex()
                    for value in identity_start_point
                ],
                "end_point_binary64_hex": [
                    struct.pack("<d", float(value)).hex()
                    for value in identity_end_point
                ],
                "start_vertex_reference_sha256": edge[
                    "start_vertex_reference_sha256"
                ],
                "end_vertex_reference_sha256": edge[
                    "end_vertex_reference_sha256"
                ],
                "curve_type": edge["curve_type"],
                "curve_sense": edge["curve_sense"],
                "curve_domain_binary64_hex": [
                    struct.pack("<d", float(edge["u_min"])).hex(),
                    struct.pack("<d", float(edge["u_max"])).hex(),
                ],
                "curve_closed": edge["curve_closed"],
                "curve_periodic": edge["curve_periodic"],
                "adjacent_mapped_face_ids": edge["face_indices"],
                "mapping_source_part_document_sha256": source_sha256,
                "mapping_source_body_reference_sha256": part_body_reference,
                "mapping_source_edge_index": edge_index,
                "mapping_source_edge_persistent_reference_sha256": hashlib.sha256(
                    f"part-edge-{edge_index}".encode()
                ).hexdigest(),
                "mapping_method": NATIVE_SCOPE_MAPPING_METHOD,
                "source_authority": NATIVE_SCOPE_SOURCE_AUTHORITY,
                "warnings": [
                    "cad_component_persistent_reference_aliased",
                    "cad_part_identity_witness_derived",
                    "cad_scope_local_identity_derived",
                    "cad_topology_identity_derived",
                ],
            }
            mapping_row = {
                "endpoints": {
                    "start": {
                        "state": (
                            "present"
                            if edge["start_point_m"] is not None
                            else "absent"
                        ),
                        "point_m": edge["start_point_m"],
                    },
                    "end": {
                        "state": (
                            "present"
                            if edge["end_point_m"] is not None
                            else "absent"
                        ),
                        "point_m": edge["end_point_m"],
                    },
                },
                "u_min": edge["u_min"],
                "u_max": edge["u_max"],
                "curve_start_point_m": edge["curve_start_point_m"],
                "curve_end_point_m": edge["curve_end_point_m"],
                "curve_sense": edge["curve_sense"],
                "curve_type": edge["curve_type"],
                "curve_closed": edge["curve_closed"],
                "curve_periodic": edge["curve_periodic"],
            }
            edge_identity["topology_signature_sha256"] = (
                _scope_edge_mapping_key(
                    mapping_row,
                    edge["face_indices"],
                )
            )
            identity_sha256 = native_scope_identity_sha256(edge_identity)
            edge["edge_identity_mode"] = "native-scope-entity-id-v1"
            edge["edge_identity"] = edge_identity
            edge["mapping_source_edge_index"] = edge_identity[
                "mapping_source_edge_index"
            ]
            edge[
                "mapping_source_edge_persistent_reference_sha256"
            ] = edge_identity[
                "mapping_source_edge_persistent_reference_sha256"
            ]
            edge["edge_reference_sha256"] = identity_sha256
            for fin_id in edge["fin_ids"]:
                fins[fin_id]["edge_reference_sha256"] = identity_sha256
            for face_index in edge["face_indices"]:
                evidence["face_spans"][face_index][
                    "edge_references"
                ].append(identity_sha256)
        evidence["itessellation"]["reference_alias_audit"] = {
            "body": [],
            "face": [],
            "edge": [shared_edge_reference],
            "vertex": [shared_vertex_reference],
        }
        identity_warnings = []
        parse_native_tessellation(
            "edge-alias",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            triangles,
            native_identity_warnings=identity_warnings,
        )
        self.assertEqual(
            {row["entity_kind"] for row in identity_warnings},
            {"edge", "vertex"},
        )

    def test_same_kind_alias_uses_scope_local_identity(self):
        body, face_10, _face_20, edge, tessellation, extension = self.fixture()
        face_alias = FakeFace(
            [2.0, 0.0, 0.0, 3.0, 0.0, 0.0, 2.0, 1.0, 0.0],
            20,
            body,
        )
        body.faces = [face_10, face_alias]
        edge.faces = [face_10, face_alias]
        tessellation.face_facets[id(face_alias)] = 20
        shared = hashlib.sha256(b"body-scope-face-alias").digest()

        def reference(value):
            if value is face_10 or value is face_alias:
                return list(shared)
            return list(
                hashlib.sha256(
                    f"native-{id(value)}".encode()
                ).digest()
            )

        extension.GetPersistReference3 = reference
        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)
        self.assertEqual(
            caught.exception.code,
            "cad_scope_witness_unavailable",
        )
        backend = SolidWorksBackend()
        document_key = object()
        source_path = Path(__file__).resolve()
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        part_body_reference = hashlib.sha256(
            b"part-body-witness"
        ).hexdigest()
        backend._source_components = {"part-1": (document_key, "Default")}
        backend._source_documents = {
            document_key: (
                str(source_path),
                "source",
                "Default",
                object(),
            )
        }
        backend._scope_witnesses_override = lambda *_args: {
            "face": {
                index: {
                    "part_document_sha256": source_sha256,
                    "part_body_reference_sha256": part_body_reference,
                    "face_index": index,
                    "persistent_reference_sha256": hashlib.sha256(
                        f"part-face-{index}".encode()
                    ).hexdigest(),
                }
                for index in range(2)
            },
            "edge": {},
            "vertex": {},
        }
        backend._captured_document = lambda *_args, **_kwargs: SimpleNamespace(
            Extension=extension
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            native_module,
            "_read_curve_end_params",
            fake_read_end_params,
        ):
            destination = Path(directory) / "alias.stl"
            entry = backend._write_component_mesh(
                "part-1",
                destination,
                FakeHolder(body),
            )
            mesh_triangles = self.stl_triangles(destination)
            identity_warnings = []
            parse_native_tessellation(
                "part-1",
                entry["tessellation"],
                entry["bodies"],
                entry["triangles"],
                mesh_triangles,
                native_identity_warnings=identity_warnings,
            )
        identities = [
            row["face_identity"]
            for row in entry["tessellation"]["face_spans"]
        ]
        self.assertTrue(all(identity is not None for identity in identities))
        self.assertTrue(
            all(
                identity["persistent"] is False
                and "cad_component_persistent_reference_aliased"
                in identity["warnings"]
                for identity in identities
            )
        )
        self.assertEqual(
            entry["tessellation"]["itessellation"]["reference_alias_audit"][
                "face"
            ],
            [hashlib.sha256(shared).hexdigest()],
        )
        self.assertEqual(
            {
                row["code"]
                for row in identity_warnings
            },
            {
                "cad_component_persistent_reference_aliased",
                "cad_part_identity_witness_derived",
                "cad_scope_local_identity_derived",
                "cad_topology_identity_derived",
            },
        )
        self.assertTrue(
            all(
                row["object_indices"] == [0, 1]
                for row in identity_warnings
            )
        )

        def mutate_identity(value, mutate):
            span = value["face_spans"][0]
            mutate(span["face_identity"])
            span["face_reference_sha256"] = native_scope_identity_sha256(
                span["face_identity"]
            )

        def change_referenced_configuration(identity):
            identity["referenced_configuration"] = "Different"
            identity["owner_scope_key"] = native_scope_identity_sha256(
                {
                    "occurrence_scope": identity["occurrence_scope"],
                    "source_document_sha256": identity[
                        "source_document_sha256"
                    ],
                    "referenced_configuration": identity[
                        "referenced_configuration"
                    ],
                    "body_type": identity["body_type"],
                    "body_index": identity["body_index"],
                    "body_name": identity["body_name"],
                }
            )

        mutations = (
            lambda value: value["face_spans"][0]["face_identity"].pop(
                "area_binary64_hex"
            ),
            lambda value: mutate_identity(
                value,
                lambda identity: identity.__setitem__(
                    "area_binary64_hex", "Z" * 16
                ),
            ),
            lambda value: mutate_identity(
                value,
                lambda identity: identity.__setitem__(
                    "topology_signature_sha256", "0" * 64
                ),
            ),
            lambda value: mutate_identity(
                value,
                lambda identity: identity.__setitem__(
                    "body_name", "different-body"
                ),
            ),
            lambda value: mutate_identity(
                value,
                lambda identity: identity.__setitem__(
                    "mapping_source_face_index", 99
                ),
            ),
            lambda value: mutate_identity(
                value,
                change_referenced_configuration,
            ),
            lambda value: mutate_identity(
                value,
                lambda identity: identity["warnings"].remove(
                    "cad_component_persistent_reference_aliased"
                ),
            ),
            lambda value: mutate_identity(
                value,
                lambda identity: identity["warnings"].append(
                    "cad_unexpected_scope_identity_warning"
                ),
            ),
            lambda value: value["itessellation"][
                "reference_alias_audit"
            ].__setitem__("face", []),
            lambda value: value["itessellation"].__setitem__(
                "cross_kind_raw_aliases", None
            ),
            lambda value: value["itessellation"].pop(
                "cross_kind_raw_aliases"
            ),
            lambda value: value["face_spans"][1].update(
                {
                    "face_identity_mode": (
                        "native-persistent-reference-v1"
                    ),
                    "face_identity": None,
                }
            ),
            lambda value: mutate_identity(
                value,
                lambda identity: identity.__setitem__(
                    "mapped_source_face_persistent_reference_sha256",
                    "0" * 64,
                ),
            ),
        )
        for mutate in mutations:
            broken = copy.deepcopy(entry["tessellation"])
            mutate(broken)
            with self.assertRaises(PipelineError):
                parse_native_tessellation(
                    "part-1",
                    broken,
                    entry["bodies"],
                    entry["triangles"],
                    mesh_triangles,
                )

    def test_match_type_readback_mismatch_is_rejected(self):
        body, _face_10, _face_20, _edge, tessellation, extension = self.fixture()
        tessellation.property_readbacks["MatchType"] = 1

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)

        self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
        self.assertEqual(caught.exception.detail["api"], "ITessellation.MatchType")

    def test_native_total_mismatch_is_rejected(self):
        body, _face_10, _face_20, _edge, tessellation, extension = self.fixture()
        tessellation.native_totals["GetFinCount"] = 7

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)
        self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
        self.assertEqual(caught.exception.detail["native"]["fins"], 7)
        self.assertEqual(caught.exception.detail["observed"]["fins"], 6)

    def test_face_owner_body_mismatch_is_rejected(self):
        body, face_10, _face_20, _edge, _tessellation, extension = self.fixture()
        face_10.owner = FakeBody([], None, None)

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)
        self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
        self.assertEqual(caught.exception.detail["face_index"], 0)

    def test_edge_owner_body_mismatch_is_rejected(self):
        body, _face_10, _face_20, edge, _tessellation, extension = self.fixture()
        edge.owner = FakeBody([], None, None)

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)
        self.assertEqual(caught.exception.code, "cad_native_tessellation_unreadable")
        self.assertEqual(caught.exception.detail["edge_index"], 0)

    def test_duplicate_direct_facet_cycles_preserve_record_multiplicity(self):
        body, face_10, _face_20, _edge, tessellation, extension = self.fixture()
        tessellation.face_facets[id(face_10)] = [10, 11]
        tessellation.native_totals["GetFacetCount"] = 3
        tessellation.native_totals["GetFinCount"] = 9
        face_10.display_triangles = []

        captured = self.capture(body, extension)

        self.assertEqual(face_10.display_calls, 0)
        self.assertEqual(
            {
                captured["facets"][10]["triangle_record_index"],
                captured["facets"][11]["triangle_record_index"],
            },
            {0, 1},
        )
        self.assertEqual(
            [
                record["facet_id"]
                for record in captured["triangle_records"]
            ],
            [10, 11, 20],
        )
        self.assertEqual(
            captured["facets"][10]["output_float32_key"],
            captured["facets"][11]["output_float32_key"],
        )


if __name__ == "__main__":
    unittest.main()
