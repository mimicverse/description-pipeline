"""Regression contracts for native edge and underlying curve parameter domains."""

from __future__ import annotations

import copy
import hashlib
import json
import struct
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)
from . import support

from description_pipeline.geometry.native_tessellation import (  # noqa: E402
    parse_native_tessellation,
)
from description_pipeline.geometry.native_u import (  # noqa: E402
    NATIVE_CURVE_DOMAIN_RULE,
    native_curve_endpoint_operation_tolerance,
    native_curve_domain_transform,
    native_u_equal,
)
from description_pipeline.io import PipelineError  # noqa: E402


class NativeCurveDomainTests(unittest.TestCase):
    def counterexample(self):
        path = Path(__file__).with_name("native-curve-domain-r5-counterexample.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def tessellation(self):
        case = self.counterexample()["first_failure"]

        def binary32(point):
            return struct.unpack("<3f", struct.pack("<3f", *point))

        start = binary32(case["edge_endpoints_m"]["start"])
        end = binary32(case["edge_endpoints_m"]["end"])
        apex_0 = binary32((start[0] + 0.001, start[1], 0.0238))
        apex_1 = binary32((start[0] + 0.001, start[1], 0.0218))
        triangles = [(start, end, apex_0), (end, start, apex_1)]
        edge_reference = case["edge_reference_sha256"]
        spans = [
            {
                "body_type": 0,
                "body_index": 0,
                "body_name": "curve-domain-fixture",
                "body_reference_sha256": hashlib.sha256(
                    b"curve-domain-body"
                ).hexdigest(),
                "face_index": face_index,
                "face_reference_sha256": hashlib.sha256(
                    f"curve-domain-face-{face_index}".encode()
                ).hexdigest(),
                "face_owner_id": face_index + 1,
                "edge_references": [edge_reference],
                "triangle_start": face_index,
                "triangle_count": 1,
                "triangle_records_sha256": "0" * 64,
            }
            for face_index in range(2)
        ]
        native_edges = [
            {
                "body_type": 0,
                "body_index": 0,
                "edge_reference_sha256": edge_reference,
                "start_vertex_reference_sha256": hashlib.sha256(
                    struct.pack("<3f", *start)
                ).hexdigest(),
                "end_vertex_reference_sha256": hashlib.sha256(
                    struct.pack("<3f", *end)
                ).hexdigest(),
                "start_point_m": list(start),
                "end_point_m": list(end),
                "face_indices": [0, 1],
                # Parser acceptance uses exactly representable negative U
                # bounds so fixture interval arithmetic cannot self-introduce
                # a non-native gap. The exact R5 values remain in the case
                # fixture and are covered by the transform test.
                "u_min": -0.5,
                "u_max": -0.25,
                "curve_sense": case["curve_sense"],
                "curve_type": case["curve_type"],
            }
        ]
        tessellation = support.native_tessellation_v3(
            spans,
            native_edges,
            triangle_vertices=triangles,
        )
        row = tessellation["native_edges"][0]
        curve = case["curve_end_params"]
        row.update(
            {
                "curve_sense": case["curve_sense"],
                "curve_type": case["curve_type"],
                "curve_closed": curve["is_closed"],
                "curve_periodic": curve["is_periodic"],
                "curve_end_params": {
                    "call_form": "pywin32-byref-variant",
                    "success": True,
                    "error": None,
                    "start": curve["start"],
                    "end": curve["end"],
                    "is_closed": curve["is_closed"],
                    "is_periodic": curve["is_periodic"],
                    "raw_outputs": [
                        {"value_type": 0x4005, "value": curve["start"]},
                        {"value_type": 0x4005, "value": curve["end"]},
                        {"value_type": 0x400B, "value": curve["is_closed"]},
                        {
                            "value_type": 0x400B,
                            "value": curve["is_periodic"],
                        },
                    ],
                },
            }
        )
        row["curve_domain_transform"] = native_curve_domain_transform(
            curve["start"],
            curve["end"],
            row["u_min"],
            row["u_max"],
            row["curve_sense"],
            curve["is_periodic"],
        )
        return tessellation, triangles

    def parse(self, tessellation, triangles):
        return parse_native_tessellation(
            "curve-domain-fixture",
            tessellation,
            {"solid": 1, "sheet": 0},
            len(triangles),
            triangles,
        )

    def closed_absent_case(self):
        tessellation, triangles = self.tessellation()
        row = tessellation["native_edges"][0]
        common_point = list(row["curve_start_point_m"])
        former_end_point = list(row["curve_end_point_m"])
        old_edge_points = {
            tuple(row["curve_start_point_m"]),
            tuple(row["curve_end_point_m"]),
        }
        affected_vertices = set()
        for fin_id in row["fin_ids"]:
            fin = next(
                item
                for item in tessellation["itessellation"]["bodies"][0]["fins"]
                if item["fin_id"] == fin_id
            )
            for vertex_id, evidence in zip(
                fin["vertex_ids"],
                (fin["start_evidence"], fin["end_evidence"]),
                strict=True,
            ):
                if native_u_equal(
                    evidence["u"],
                    row["u_min"],
                ) or native_u_equal(evidence["u"], row["u_max"]):
                    affected_vertices.add(vertex_id)
                    evidence["closest_point_on_raw"][:3] = common_point
                    evidence["evaluate_payload"][:3] = common_point
                    evidence["point_tolerance_m"] = (
                        native_curve_endpoint_operation_tolerance(
                            common_point,
                            common_point,
                            common_point,
                        )
                    )
                    evidence["closest_point_error_m"] = 0.0
                    evidence["evaluate_point_error_m"] = 0.0
                    evidence["closest_evaluate_error_m"] = 0.0
        for vertex in tessellation["itessellation"]["vertices"]:
            if vertex["vertex_id"] in affected_vertices:
                vertex["point_m"] = list(common_point)
        for index, triangle in enumerate(triangles):
            triangles[index] = tuple(
                tuple(common_point)
                if tuple(point) in old_edge_points
                else point
                for point in triangle
            )
        row.update(
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
                "curve_start_point_m": common_point,
                "curve_end_point_m": list(common_point),
            }
        )
        row["curve_end_params"]["is_closed"] = True
        row["curve_end_params"]["raw_outputs"][2]["value"] = True
        row["curve_domain_transform"] = native_curve_domain_transform(
            row["curve_end_params"]["start"],
            row["curve_end_params"]["end"],
            row["u_min"],
            row["u_max"],
            row["curve_sense"],
            row["curve_end_params"]["is_periodic"],
        )
        for evaluation in row["curve_endpoint_evaluations"].values():
            evaluation["payload"] = [*common_point, 0.0, 0.0, 0.0]
            evaluation["point_tolerance_m"] = native_curve_endpoint_operation_tolerance(
                common_point,
                common_point,
                common_point,
            )
            evaluation["point_error_m"] = 0.0
        return tessellation, triangles, former_end_point

    def test_r5_negative_edge_u_with_positive_curve_end_params_is_edge_local(self):
        case = self.counterexample()
        observed = case["first_failure"]
        curve = observed["curve_end_params"]
        transform = native_curve_domain_transform(
            curve["start"],
            curve["end"],
            observed["edge_u"]["u_min"],
            observed["edge_u"]["u_max"],
            observed["curve_sense"],
            curve["is_periodic"],
        )

        self.assertEqual(transform["rule_version"], NATIVE_CURVE_DOMAIN_RULE)
        self.assertEqual(transform["status"], case["expected"]["status"])
        self.assertEqual(
            transform["domain_basis"],
            case["expected"]["domain_basis"],
        )
        self.assertEqual(
            (transform["underlying_start"], transform["underlying_end"]),
            (curve["start"], curve["end"]),
        )
        self.assertEqual(
            (transform["lifted_start"], transform["lifted_end"]),
            (observed["edge_u"]["u_min"], observed["edge_u"]["u_max"]),
        )
        self.assertNotIn("translation", transform)

    def test_parser_accepts_present_nonperiodic_edge_local_domain(self):
        tessellation, triangles = self.tessellation()

        spans = self.parse(tessellation, triangles)

        self.assertEqual(len(spans), 2)
        transform = tessellation["native_edges"][0]["curve_domain_transform"]
        self.assertEqual(transform["status"], "edge_local_nonperiodic")
        self.assertEqual(transform["domain_basis"], "edge_curve_params3")

    def test_parser_rejects_varianttype_mismatch(self):
        tessellation, triangles = self.tessellation()
        tessellation["native_edges"][0]["curve_end_params"]["raw_outputs"][0][
            "value_type"
        ] = 0x4004

        with self.assertRaises(PipelineError) as caught:
            self.parse(tessellation, triangles)

        self.assertIn("periodicity raw result differs", str(caught.exception))

    def test_parser_rejects_spoofed_contained_status(self):
        tessellation, triangles = self.tessellation()
        transform = tessellation["native_edges"][0]["curve_domain_transform"]
        transform["status"] = "contained"
        transform["domain_basis"] = "underlying_curve_get_end_params"

        with self.assertRaises(PipelineError) as caught:
            self.parse(tessellation, triangles)

        self.assertIn("curve-domain proof", str(caught.exception))

    def test_parser_rejects_periodic_claim_on_edge_local_proof(self):
        tessellation, triangles = self.tessellation()
        row = tessellation["native_edges"][0]
        row["curve_periodic"] = True
        row["curve_end_params"]["is_periodic"] = True
        row["curve_end_params"]["raw_outputs"][3]["value"] = True

        with self.assertRaises(PipelineError):
            self.parse(tessellation, triangles)

    def test_parser_rejects_single_absent_endpoint_for_edge_local_domain(self):
        tessellation, triangles = self.tessellation()
        row = tessellation["native_edges"][0]
        row["start_vertex_reference_sha256"] = None
        row["start_point_m"] = None
        row["start_vertex_state"] = "absent"
        row["start_point_error"] = "vertex_absent"

        with self.assertRaises(PipelineError) as caught:
            self.parse(tessellation, triangles)

        self.assertIn("curve-domain proof", str(caught.exception))

    def test_closed_absent_edge_requires_equal_evaluate2_endpoints(self):
        tessellation, triangles, _former_end = self.closed_absent_case()

        spans = self.parse(tessellation, triangles)

        self.assertEqual(len(spans), 2)
        row = tessellation["native_edges"][0]
        self.assertEqual(row["curve_domain_transform"]["status"], "edge_local_nonperiodic")
        self.assertEqual(
            row["curve_endpoint_evaluations"]["start"]["payload"][:3],
            row["curve_endpoint_evaluations"]["end"]["payload"][:3],
        )

    def test_closed_absent_edge_with_distinct_evaluate2_points_is_rejected(self):
        tessellation, triangles, former_end = self.closed_absent_case()
        edge = tessellation["native_edges"][0]
        fins = tessellation["itessellation"]["bodies"][0]["fins"]
        affected_vertices = set()
        for fin_id in edge["fin_ids"]:
            fin = next(item for item in fins if item["fin_id"] == fin_id)
            for vertex_id, evidence in zip(
                fin["vertex_ids"],
                (fin["start_evidence"], fin["end_evidence"]),
                strict=True,
            ):
                if not native_u_equal(evidence["u"], edge["u_max"]):
                    continue
                affected_vertices.add(vertex_id)
                evidence["closest_point_on_raw"][:3] = former_end
                evidence["evaluate_payload"][:3] = former_end
                evidence["point_tolerance_m"] = (
                    native_curve_endpoint_operation_tolerance(
                        former_end,
                        former_end,
                        former_end,
                    )
                )
        for vertex in tessellation["itessellation"]["vertices"]:
            if vertex["vertex_id"] in affected_vertices:
                vertex["point_m"] = list(former_end)
        triangles[0] = (
            triangles[0][0],
            tuple(former_end),
            triangles[0][2],
        )
        triangles[1] = (
            tuple(former_end),
            triangles[1][1],
            triangles[1][2],
        )

        with self.assertRaises(PipelineError) as caught:
            self.parse(tessellation, triangles)

        self.assertIn("endpoint conflicts with fin", str(caught.exception))

    def test_transform_version_cannot_be_reused_as_old_domain_rule(self):
        tessellation, triangles = self.tessellation()
        old = copy.deepcopy(tessellation)
        old["native_edges"][0]["curve_domain_transform"]["rule_version"] = (
            "native-curve-domain-periodic-lift-v1"
        )

        with self.assertRaises(PipelineError):
            self.parse(old, triangles)


if __name__ == "__main__":
    unittest.main()
