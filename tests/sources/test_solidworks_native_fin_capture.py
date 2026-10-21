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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.geometry.native_tessellation import (  # noqa: E402
    _valid_u_evidence,
    parse_native_tessellation,
)
from description_pipeline.geometry.native_u import (  # noqa: E402
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
        return {100: 200, 200: 100}[fin_id]


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
    def __init__(self, body):
        self.body = body

    def GetBodies2(self, body_type):  # noqa: N802 - SolidWorks API name
        return [self.body] if body_type == 0 else []


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

    def capture(self, body, extension):
        return SolidWorksBackend()._capture_body_tessellation(
            body,
            extension,
            component="part-1",
            body_type=0,
            body_index=0,
            read_end_params=fake_read_end_params,
        )

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

    def group_333_tessellation(self):
        body, _face_10, _face_20, edge, tessellation, extension = self.fixture()
        self.configure_group_333(edge, tessellation)
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
        self.assertEqual(len(extension.calls), 11)
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

        spans = parse_native_tessellation(
            "part-1",
            evidence,
            {"solid": 1, "sheet": 0},
            len(triangles),
            triangles,
        )

        self.assertEqual(len(spans), 2)

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

    def test_cross_kind_persistent_reference_alias_is_rejected(self):
        body, face_10, _face_20, _edge, _tessellation, extension = self.fixture()
        shared = hashlib.sha256(b"same").digest()
        extension.references[id(body)] = shared
        extension.references[id(face_10)] = shared

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)
        self.assertEqual(caught.exception.code, "cad_persistent_reference_alias")
        self.assertEqual(caught.exception.detail["kind"], "face")

    def test_same_kind_persistent_reference_alias_is_rejected(self):
        body, face_10, _face_20, _edge, _tessellation, extension = self.fixture()
        body.faces = [face_10, face_10]

        with self.assertRaises(CadError) as caught:
            self.capture(body, extension)
        self.assertEqual(caught.exception.code, "cad_persistent_reference_alias")
        self.assertEqual(caught.exception.detail["previous"]["kind"], "face")

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
