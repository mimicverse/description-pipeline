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
    NATIVE_CURVE_TOPOLOGY_ENDPOINT_SET_RULE,
    native_curve_topology_endpoint_set_assignment,
    native_curve_endpoint_operation_tolerance,
    native_curve_parameter_endpoints,
    native_curve_domain_transform,
    native_u_equal,
)
from description_pipeline.io import PipelineError  # noqa: E402


class NativeCurveDomainTests(unittest.TestCase):
    def test_closed_endpoint_set_requires_shared_native_vertex_and_consistent_points(self):
        point = (1.0, 2.0, 3.0)
        self.assertFalse(native_curve_topology_endpoint_set_assignment(point, point, point, point)["accepted"])
        self.assertTrue(native_curve_topology_endpoint_set_assignment(
            point, point, point, point, shared_vertex=True)["accepted"])
        self.assertFalse(native_curve_topology_endpoint_set_assignment(
            point, (1.1, 2.0, 3.0), point, point, shared_vertex=True)["accepted"])
        self.assertFalse(native_curve_topology_endpoint_set_assignment(
            point, (1.1, 2.0, 3.0), point, (1.1, 2.0, 3.0), shared_vertex=True)["accepted"])
        with self.assertRaisesRegex(ValueError, "boolean"):
            native_curve_topology_endpoint_set_assignment(point, point, point, point, shared_vertex=1)

    def test_topology_pairing_accepts_observed_39nm_curve_wobble(self):
        expected_start = (
            0.01000643903049045,
            -0.0061647112233256295,
            -0.055676906273813,
        )
        expected_end = (
            expected_start[0] + 0.001,
            expected_start[1],
            expected_start[2],
        )
        actual_start = (
            expected_start[0] + 3.936764624706823e-8,
            expected_start[1] - 1.1517504897859e-8,
            expected_start[2] - 9.72215465027256e-11,
        )

        result = native_curve_topology_endpoint_set_assignment(
            expected_start,
            expected_end,
            actual_start,
            expected_end,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(
            result["rule_version"],
            NATIVE_CURVE_TOPOLOGY_ENDPOINT_SET_RULE,
        )
        self.assertEqual(result["canonical_result"], "direct")
        self.assertEqual(result["complete_match_count"], 1)
        self.assertLess(result["direct_error_m"], result["tolerance_m"])
        self.assertGreater(
            result["swapped_error_m"],
            result["tolerance_m"],
        )
        self.assertGreater(result["alternative_margin_m"], 0.0)

    def test_topology_endpoint_set_identifies_swapped_endpoints(self):
        result = native_curve_topology_endpoint_set_assignment(
            (0.0, 0.0, 0.0),
            (0.010, 0.0, 0.0),
            (0.010, 0.0, 0.0),
            (0.0, 0.0, 0.0),
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["canonical_result"], "swapped")
        self.assertEqual(result["complete_match_count"], 1)
        self.assertGreater(result["direct_error_m"], result["tolerance_m"])

    def test_topology_endpoint_set_records_ambiguous_short_edge(self):
        result = native_curve_topology_endpoint_set_assignment(
            (0.0, 0.0, 0.0),
            (40e-6, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (40e-6, 0.0, 0.0),
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["complete_match_count"], 2)
        self.assertEqual(result["canonical_result"], "direct")
        self.assertLessEqual(
            result["swapped_error_m"],
            result["tolerance_m"],
        )

    def test_topology_endpoint_set_tolerance_boundary_is_inclusive(self):
        accepted = native_curve_topology_endpoint_set_assignment(
            (0.0, 0.0, 0.0),
            (0.010, 0.0, 0.0),
            (50e-6, 0.0, 0.0),
            (0.010, 0.0, 0.0),
        )
        rejected = native_curve_topology_endpoint_set_assignment(
            (0.0, 0.0, 0.0),
            (0.010, 0.0, 0.0),
            (50e-6 + 1e-12, 0.0, 0.0),
            (0.010, 0.0, 0.0),
        )

        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["direct_error_m"], accepted["tolerance_m"])
        self.assertFalse(rejected["accepted"])
        self.assertIsNone(rejected["canonical_result"])
        self.assertGreater(
            rejected["direct_error_m"],
            rejected["tolerance_m"],
        )

        swapped_boundary = native_curve_topology_endpoint_set_assignment(
            (0.0, 0.0, 0.0),
            (50e-6, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (50e-6, 0.0, 0.0),
        )
        self.assertTrue(swapped_boundary["accepted"])
        self.assertEqual(swapped_boundary["complete_match_count"], 2)

    def test_topology_endpoint_set_rejects_malformed_inputs(self):
        with self.assertRaisesRegex(ValueError, "three coordinates"):
            native_curve_topology_endpoint_set_assignment(
                (0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
            )
        with self.assertRaisesRegex(ValueError, "finite"):
            native_curve_topology_endpoint_set_assignment(
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 0.0, 0.0),
                (float("nan"), 0.0, 0.0),
            )

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
        tessellation = support.native_tessellation_fixture(
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
        topology_endpoints = native_curve_parameter_endpoints(
            row["curve_start_point_m"],
            row["curve_end_point_m"],
        )
        row["topology_endpoint_assignment"] = (
            native_curve_topology_endpoint_set_assignment(
                topology_endpoints[0],
                topology_endpoints[1],
                row["start_point_m"],
                row["end_point_m"],
            )
        )
        return tessellation, triangles

    def parse(self, tessellation, triangles):
        support.refresh_topology_endpoint_assignment(tessellation)
        return self.parse_raw(tessellation, triangles)

    def parse_raw(self, tessellation, triangles):
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
                    u_value = evidence["u"]
                    evidence.clear()
                    evidence.update(
                        support._u_evidence(
                            u_value,
                            common_point,
                            projected_point_m=common_point,
                            evaluated_point_m=common_point,
                        )
                    )
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
                "topology_endpoint_assignment": None,
                "curve_closed": True,
                "curve_start_point_m": common_point,
                "curve_end_point_m": list(common_point),
            }
        )
        tessellation["itessellation"]["solidworks_vertex_identities"] = []
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

    def test_endpoint_closure_alone_does_not_prove_a_closed_fin_cycle(self):
        tessellation, triangles, _former_end = self.closed_absent_case()
        row = tessellation["native_edges"][0]
        self.assertEqual(row["curve_endpoint_evaluations"]["start"]["payload"][:3],
                         row["curve_endpoint_evaluations"]["end"]["payload"][:3])
        with self.assertRaisesRegex(PipelineError, "closed edge fins"):
            self.parse(tessellation, triangles)

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
                evidence["source_point_m"] = list(former_end)
                evidence["projection_error_m"] = 0.0
                evidence["source_evaluate_error_m"] = 0.0
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

        self.assertIn("U evidence is not independently reproducible", str(caught.exception))

    def test_transform_version_cannot_be_reused_as_old_domain_rule(self):
        tessellation, triangles = self.tessellation()
        old = copy.deepcopy(tessellation)
        old["native_edges"][0]["curve_domain_transform"]["rule_version"] = (
            "native-curve-domain-periodic-lift-v1"
        )

        with self.assertRaises(PipelineError):
            self.parse(old, triangles)

    def test_present_topology_assignment_is_required(self):
        tessellation, triangles = self.tessellation()
        del tessellation["native_edges"][0]["topology_endpoint_assignment"]

        with self.assertRaisesRegex(PipelineError, "unknown shape"):
            self.parse_raw(tessellation, triangles)

    def test_absent_topology_assignment_field_is_required(self):
        tessellation, triangles, _former_end = self.closed_absent_case()
        for edge in tessellation["native_edges"]:
            if {
                edge["start_vertex_state"],
                edge["end_vertex_state"],
            } == {"absent"}:
                del edge["topology_endpoint_assignment"]

        with self.assertRaisesRegex(PipelineError, "unknown shape"):
            self.parse_raw(tessellation, triangles)

    def test_absent_endpoints_reject_forged_topology_assignment(self):
        tessellation, triangles, _former_end = self.closed_absent_case()
        for edge in tessellation["native_edges"]:
            if {
                edge["start_vertex_state"],
                edge["end_vertex_state"],
            } == {"absent"}:
                edge["topology_endpoint_assignment"] = {"accepted": True}

        with self.assertRaisesRegex(
            PipelineError,
            "absent endpoints",
        ):
            self.parse_raw(tessellation, triangles)


if __name__ == "__main__":
    unittest.main()
