"""Untessellated CAD sheets are explicit evidence, never missing solid facets."""

import copy
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _paths  # noqa: F401
from . import support

from description_pipeline.geometry.native_tessellation import (
    parse_native_tessellation,
    summarize_native_topology,
)
from description_pipeline.geometry.native_fin_topology import (
    NativeFinRelationError,
    validate_native_edge_fin_relations,
)
from description_pipeline.geometry.native_u import native_curve_domain_transform
from description_pipeline.io import PipelineError
from description_pipeline.schemas import (
    NATIVE_FACETLESS_EVIDENCE_API,
    NATIVE_TESSELLATION_SCHEMA,
)
from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.verification.native_mesh_observations import verify_native_mesh_observations


class NativeMeshObservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        center = (0.0, 0.0, 0.0)
        axis_x = (1.0, 0.0, 0.0)
        axis_y = (0.0, 1.0, 0.0)
        axis_z = (0.0, 0.0, 1.0)
        tetra_a = (3.0, 0.0, 0.0)
        tetra_b = (4.0, 0.0, 0.0)
        tetra_c = (3.0, 1.0, 0.0)
        tetra_d = (3.0, 0.0, 1.0)
        face_triangles = {
            (0, 0): [(center, axis_x, axis_y)],
            (0, 1): [(center, axis_y, axis_z)],
            (0, 2): [(center, axis_z, axis_x)],
            (1, 0): [(center, axis_x, axis_y)],
            (1, 1): [(center, axis_y, axis_z)],
            (1, 2): [(center, axis_z, axis_x)],
            (2, 0): [],
            (2, 1): [(center, axis_x, axis_y)],
            (2, 2): [(axis_x, center, axis_z)],
            (3, 0): [(tetra_a, tetra_c, tetra_b)],
            (3, 1): [(tetra_a, tetra_b, tetra_d)],
            (3, 2): [(tetra_b, tetra_c, tetra_d)],
            (3, 3): [(tetra_a, tetra_d, tetra_c)],
        }
        face_owners = {
            key: index
            for index, key in enumerate(face_triangles, start=1)
        }
        triangles = [
            triangle
            for body_index in range(4)
            for face_index in range(4)
            for triangle in face_triangles.get((body_index, face_index), [])
        ]
        stl_path = self.root / "evidence/geometry/fixture.stl"
        stl_path.parent.mkdir(parents=True, exist_ok=True)
        support.write_target_delivery(
            self.root,
            capture={
                "collection": {},
                "geometry": [],
            },
        )
        # The strictness comes from the frozen snapshot manifest identity, never
        # from the capture collection block (which carries no kind fields).
        (self.root / "evidence/raw/scene_raw.json").write_text('{"scene": "fixture"}', encoding="utf-8")
        write_manifest(
            self.root / "evidence",
            kind="solidworks",
            identity={"capture": {"evidence_class": "cad"}},
            evidence_class="cad",
        )
        support.seal_delivery(self.root)
        counts = {
            (1, body_index, face_index): len(face_triangles[(body_index, face_index)])
            for body_index in range(4)
            for face_index in range(4)
            if (body_index, face_index) in face_triangles
            and (
                body_index < 3
                or face_index < 4
            )
        }
        spans = []
        start = 0
        for ordinal, ((body_type, body_index, face_index), count) in enumerate(counts.items()):
            spans.append(
                {
                    "body_type": body_type,
                    "body_index": body_index,
                    "body_name": f"Sheet{body_index}",
                    "body_reference_sha256": hashlib.sha256(f"body-{body_index}".encode()).hexdigest(),
                    "face_index": face_index,
                    "face_reference_sha256": hashlib.sha256(f"face-{ordinal}".encode()).hexdigest(),
                    "face_owner_id": face_owners[(body_index, face_index)],
                    "edge_references": [],
                    "triangle_start": start,
                    "triangle_count": count,
                    "triangle_records_sha256": "0" * 64,
                }
            )
            start += count
        span_by_face = {
            (span["body_index"], span["face_index"]): span
            for span in spans
        }
        occurrences = {}
        for (body_index, face_index), face_defs in face_triangles.items():
            for triangle in face_defs:
                for offset in range(3):
                    start = triangle[offset]
                    end = triangle[(offset + 1) % 3]
                    key = (
                        body_index,
                        frozenset(
                            (
                                struct.pack("<3f", *start),
                                struct.pack("<3f", *end),
                            )
                        ),
                    )
                    occurrences.setdefault(key, []).append(
                        (body_index, face_index, start, end)
                    )
        native_edges = []
        for ordinal, rows in enumerate(
            sorted(
                occurrences.values(),
                key=lambda items: (
                    items[0][0],
                    items[0][1],
                    struct.pack("<3f", *items[0][2]),
                ),
            ),
            start=1,
        ):
            body_index = rows[0][0]
            face_indices = sorted({row[1] for row in rows})
            if len(face_indices) != 2:
                continue
            start = rows[0][2]
            end = rows[0][3]
            reference = hashlib.sha256(f"fixture-edge-{ordinal}".encode()).hexdigest()
            native_edges.append(
                {
                    "body_type": 1,
                    "body_index": body_index,
                    "edge_reference_sha256": reference,
                    "start_vertex_reference_sha256": hashlib.sha256(
                        struct.pack("<3f", *start)
                    ).hexdigest(),
                    "end_vertex_reference_sha256": hashlib.sha256(
                        struct.pack("<3f", *end)
                    ).hexdigest(),
                    "start_point_m": list(start),
                    "end_point_m": list(end),
                    "face_indices": face_indices,
                }
            )
            for face_index in face_indices:
                span_by_face[(body_index, face_index)]["edge_references"].append(
                    reference
                )
        tessellation = support.native_tessellation_fixture(
            spans,
            native_edges,
            [
                {
                    "body_type": 1,
                    "body_index": 2,
                    "face_index": 0,
                    "triangle_count": 0,
                    "facet_count": 0,
                    "area_m2": 2e-13,
                    "tessellate_result": False,
                    "used_api": NATIVE_FACETLESS_EVIDENCE_API,
                }
            ],
            triangle_vertices=triangles,
        )
        self.stl_bytes = tessellation.stl_bytes
        stl_path.write_bytes(self.stl_bytes)
        self.row = {
            "component": "pcb",
            "path": "geometry/fixture.stl",
            "triangles": 12,
            "sha256": hashlib.sha256(self.stl_bytes).hexdigest(),
            "bodies": {"solid": 0, "sheet": 4},
            "tessellation": tessellation,
        }

    def verify(self, capture_class: str = "cad", rows=None):
        # Adversarial rows keep NaN/Infinity literals on purpose: the verifier
        # must reject them semantically, so the block bytes are written raw.
        path = support.capture_file_for(self.root)
        capture = json.loads(path.read_text(encoding="utf-8"))
        capture["geometry"] = [self.row] if rows is None else list(rows)
        path.write_text(json.dumps(capture, ensure_ascii=False, allow_nan=True), encoding="utf-8")
        write_manifest(
            self.root / "evidence",
            kind="solidworks",
            identity={"capture": {"evidence_class": capture_class}},
            evidence_class="cad",
        )
        support.seal_delivery(self.root)
        return verify_native_mesh_observations(self.root)

    def test_strictness_follows_the_snapshot_manifest_not_the_collection_block(self):
        self.row = {"component": "pcb", "bodies": {"solid": 0, "sheet": 4}}
        with self.assertRaisesRegex(PipelineError, "native tessellation accounting is missing"):
            self.verify()
        self.assertEqual(0, self.verify("fixture")["components"])

    def test_native_refusal_is_explicit_unqualified_surface_warning(self):
        result = self.verify()
        self.assertEqual(result["components"], 1)
        self.assertEqual(result["untessellated_native_area_m2"], 2e-13)
        warning, = result["warnings"]
        self.assertEqual(warning["code"], "cad_sheet_face_untessellated")
        self.assertFalse(warning["tessellate_result"])
        self.assertIn("unqualified", warning["qualification"])

    def test_source_scope_rejection_is_not_swallowed_by_mesh_consumer(self):
        with (
            patch(
                "description_pipeline.verification.native_mesh_observations."
                "evidence.source_scopes",
                side_effect=PipelineError("source scope evidence mismatch"),
            ),
            self.assertRaisesRegex(
                PipelineError,
                "source scope evidence mismatch",
            ),
        ):
            self.verify()

    def test_collapsed_fin_warning_is_separate_from_untessellated_area(self):
        from .test_solidworks_native_fin_capture import NativeFinCaptureTests  # noqa: PLC0415

        untessellated_row = copy.deepcopy(self.row)
        evidence, triangles, _edge, _start, _near, _end, _bind = (
            NativeFinCaptureTests.collapsed_u_tessellation()
        )
        stl = evidence.stl_bytes
        relative = "geometry/collapsed-native.stl"
        target = self.root / "evidence" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(stl)
        collapsed_row = {
            "component": "collapsed-native",
            "path": relative,
            "triangles": len(triangles),
            "sha256": hashlib.sha256(stl).hexdigest(),
            "bodies": {"solid": 1, "sheet": 0},
            "tessellation": evidence,
        }

        result = self.verify(rows=[collapsed_row, untessellated_row])

        self.assertEqual(result["components"], 2)
        self.assertEqual(result["untessellated_native_area_m2"], 2e-13)
        warnings = {
            warning["code"]: warning
            for warning in result["warnings"]
        }
        self.assertEqual(
            set(warnings),
            {"cad_sheet_face_untessellated", "native_fin_u_collapsed"},
        )
        self.assertEqual(len(warnings["native_fin_u_collapsed"]["fin_ids"]), 2)

    def test_zero_native_facets_after_success_are_also_explicit(self):
        self.row["tessellation"]["no_facets_faces"][0]["tessellate_result"] = True
        self.assertEqual(len(self.verify()["warnings"]), 1)

    def test_degenerate_probe_error_shape_is_accepted_for_zero_area(self):
        face = self.row["tessellation"]["no_facets_faces"][0]
        face.pop("tessellate_result")
        face.pop("facet_count")
        face["tessellate_probe_error"] = "com_error: DISP_E_EXCEPTION during Tessellate"
        face["area_m2"] = 1.8716e-13
        result = self.verify()
        warning, = result["warnings"]
        self.assertEqual(warning["code"], "cad_sheet_face_untessellated")
        self.assertEqual(warning["tessellate_probe_error"], "com_error: DISP_E_EXCEPTION during Tessellate")
        self.assertNotIn("tessellate_result", warning)
        self.assertNotIn("facet_count", warning)
        self.assertEqual(result["untessellated_native_area_m2"], 1.8716e-13)
        self.assertIn("unqualified", warning["qualification"])

        self.row = copy.deepcopy(self.row)
        self.row["tessellation"]["no_facets_faces"][0]["area_m2"] = 1e-12
        self.assertEqual(len(self.verify()["warnings"]), 1)

    def test_probe_error_shape_is_rejected_above_numerically_zero_area(self):
        face = self.row["tessellation"]["no_facets_faces"][0]
        face.pop("tessellate_result")
        face.pop("facet_count")
        face["tessellate_probe_error"] = "com_error: DISP_E_EXCEPTION"
        face["area_m2"] = 1e-11
        with self.assertRaisesRegex(PipelineError, "zero native area"):
            self.verify()

    def test_probe_error_and_tessellate_result_together_are_rejected(self):
        face = self.row["tessellation"]["no_facets_faces"][0]
        face["tessellate_probe_error"] = "com_error: DISP_E_EXCEPTION"
        with self.assertRaisesRegex(PipelineError, "malformed untessellated-face observation"):
            self.verify()

    def test_facet_count_without_a_result_or_probe_error_is_rejected(self):
        face = self.row["tessellation"]["no_facets_faces"][0]
        face.pop("tessellate_result")
        with self.assertRaisesRegex(PipelineError, "malformed untessellated-face observation"):
            self.verify()

    def test_probe_error_field_must_be_a_com_error_string(self):
        base = self.row["tessellation"]["no_facets_faces"][0]
        for value in (None, 7, "DISP_E_EXCEPTION", "", False):
            with self.subTest(value=value):
                face = copy.deepcopy(base)
                face.pop("tessellate_result")
                face.pop("facet_count")
                face["tessellate_probe_error"] = value
                self.row["tessellation"]["no_facets_faces"] = [face]
                with self.assertRaises(PipelineError):
                    self.verify()

    def test_invalid_and_nonempty_native_geometry_cannot_be_waived(self):
        valid = copy.deepcopy(self.row)
        for key, value in (
            ("body_type", 0), ("body_index", 4), ("face_index", -1),
            ("triangle_count", 1), ("facet_count", 1), ("area_m2", -1),
            ("area_m2", float("nan")), ("tessellate_result", None),
            ("triangle_count", False), ("used_api", "invented"),
        ):
            with self.subTest(key=key, value=value):
                self.row = copy.deepcopy(valid)
                self.row["tessellation"]["no_facets_faces"][0][key] = value
                with self.assertRaises(PipelineError):
                    self.verify()

    def test_missing_native_accounting_fails(self):
        self.row.pop("tessellation")
        with self.assertRaisesRegex(PipelineError, "accounting is missing"):
            self.verify()

    def test_duplicate_and_all_missing_faces_fail(self):
        self.row["tessellation"]["no_facets_faces"] *= 2
        with self.assertRaisesRegex(PipelineError, "duplicate"):
            self.verify()
        self.row["tessellation"]["no_facets_faces"].pop()
        self.row["tessellation"]["faces"] = 1
        with self.assertRaisesRegex(PipelineError, "no tessellated native faces"):
            self.verify()

    def test_face_span_schema_coverage_digest_and_stl_binding_fail_closed(self):
        original = copy.deepcopy(self.row)

        def legacy(row):
            row["tessellation"].pop("native_edges")

        def boolean_body_type(row):
            row["tessellation"]["face_spans"][0]["body_type"] = True

        def gap(row):
            row["tessellation"]["face_spans"][1]["triangle_start"] += 1

        def body_not_covered(row):
            row["tessellation"]["face_spans"] = row["tessellation"]["face_spans"][:-4]
            row["tessellation"]["faces"] -= 4

        def wrong_span_digest(row):
            row["tessellation"]["face_spans"][0]["triangle_records_sha256"] = "0" * 64

        def wrong_stl_digest(row):
            row["sha256"] = "0" * 64

        def missing_facetless_row(row):
            row["tessellation"]["no_facets_faces"] = []

        def edge_incidence_mismatch(row):
            edge = row["tessellation"]["native_edges"][0]
            edge["face_indices"] = edge["face_indices"][:1]
            edge["face_senses"] = edge["face_senses"][:1]

        def missing_reverse_edge_incidence(row):
            edge = row["tessellation"]["native_edges"][0]
            shared = edge["edge_reference_sha256"]
            face_index = edge["face_indices"][1]
            span = next(
                item
                for item in row["tessellation"]["face_spans"]
                if item["body_index"] == edge["body_index"]
                and item["face_index"] == face_index
            )
            span["edge_references"].remove(shared)

        def empty_edge_incidence(row):
            edge = row["tessellation"]["native_edges"][0]
            edge["face_indices"] = []
            for span in row["tessellation"]["face_spans"]:
                span["edge_references"] = []

        def role_identity_alias(row):
            face_reference = row["tessellation"]["face_spans"][0]["face_reference_sha256"]
            edge = row["tessellation"]["native_edges"][0]
            old = edge["edge_reference_sha256"]
            edge["edge_reference_sha256"] = face_reference
            for span in row["tessellation"]["face_spans"]:
                span["edge_references"] = [
                    face_reference if reference == old else reference
                    for reference in span["edge_references"]
                ]

        def vertex_role_alias(row):
            edge = row["tessellation"]["native_edges"][0]
            edge["start_vertex_reference_sha256"] = row["tessellation"]["face_spans"][0][
                "body_reference_sha256"
            ]

        def vertex_coordinate_conflict(row):
            edge = row["tessellation"]["native_edges"][0]
            edge["end_vertex_reference_sha256"] = edge["start_vertex_reference_sha256"]
            edge["end_point_m"] = [2.0, 0.0, 0.0]

        def boundary_shift_with_recomputed_digest(row):
            spans = row["tessellation"]["face_spans"]
            earlier = next(
                span
                for span in spans
                if span["body_index"] == 2 and span["face_index"] == 1
            )
            later = next(
                span
                for span in spans
                if span["body_index"] == 3 and span["face_index"] == 0
            )
            earlier["triangle_count"] = 1
            earlier["triangle_records_sha256"] = hashlib.sha256(
                self.stl_bytes[84 + 50 * 6 : 84 + 50 * 7]
            ).hexdigest()
            later["triangle_start"] = 7
            later["triangle_count"] = 2
            later["triangle_records_sha256"] = hashlib.sha256(
                self.stl_bytes[84 + 50 * 7 : 84 + 50 * 9]
            ).hexdigest()

        def missing_body_edge_evidence(row):
            row["tessellation"]["native_edges"] = [
                edge
                for edge in row["tessellation"]["native_edges"]
                if edge["body_index"] != 3
            ]
            body = next(
                body
                for body in row["tessellation"]["itessellation"]["bodies"]
                if body["body_index"] == 3
            )
            for fin in body["fins"]:
                fin["edge_reference_sha256"] = None
                fin["classification"] = "internal_only"
            for span in row["tessellation"]["face_spans"]:
                if span["body_index"] == 3:
                    span["edge_references"] = []
            body = next(
                item
                for item in row["tessellation"]["itessellation"]["bodies"]
                if item["body_index"] == 3
            )
            for fin in body["fins"]:
                fin["edge_reference_sha256"] = None
                fin["classification"] = "internal_only"
                fin["start_u"] = None
                fin["end_u"] = None
                fin["start_evidence"] = None
                fin["end_evidence"] = None
                fin["cofin_raw_id"] = None
                fin["cofin_id"] = None

        cases = (
            ("legacy", legacy, "unknown or legacy"),
            ("boolean-body", boolean_body_type, "face span 0 is malformed"),
            ("gap", gap, "gap or overlaps"),
            ("body-coverage", body_not_covered, "complete native STL triangle stream"),
            ("span-digest", wrong_span_digest, "does not bind"),
            ("stl-digest", wrong_stl_digest, "STL bytes do not bind"),
            ("facetless", missing_facetless_row, "differs from zero-length"),
            ("edge-incidence", edge_incidence_mismatch, "no matching native edge incidence"),
            (
                "edge-reverse-incidence",
                missing_reverse_edge_incidence,
                "not bound to every declared face",
            ),
            ("empty-edge-incidence", empty_edge_incidence, "native edge 0 is malformed"),
            ("role-alias", role_identity_alias, "identities must not alias"),
            ("vertex-role-alias", vertex_role_alias, "identities must not alias"),
            ("vertex-coordinate-conflict", vertex_coordinate_conflict, "multiple coordinates"),
            (
                "boundary-shift",
                boundary_shift_with_recomputed_digest,
                "gap or overlaps",
            ),
            (
                "missing-body-edges",
                missing_body_edge_evidence,
                "topology evidence is unresolved|not bound to its edges",
            ),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                self.row = copy.deepcopy(original)
                mutate(self.row)
                with self.assertRaisesRegex(PipelineError, message):
                    self.verify()

    def test_v3_itessellation_top_level_shape_and_properties_fail_closed(self):
        original = copy.deepcopy(self.row)
        mutations = (
            (
                "missing",
                lambda row: row["tessellation"].pop("itessellation"),
                "unknown or legacy",
            ),
            (
                "unknown",
                lambda row: row["tessellation"]["itessellation"].__setitem__(
                    "coordinate_tolerance", 1e-9
                ),
                "ITessellation evidence is missing",
            ),
            (
                "property",
                lambda row: row["tessellation"]["itessellation"]["properties"].__setitem__(
                    "need_edge_fin_map", False
                ),
                "properties are incomplete",
            ),
            (
                "property-int",
                lambda row: row["tessellation"]["itessellation"]["properties"].__setitem__(
                    "need_edge_fin_map", 1
                ),
                "properties are incomplete",
            ),
            (
                "match-type",
                lambda row: row["tessellation"]["itessellation"]["properties"].__setitem__(
                    "match_type", 1
                ),
                "properties are incomplete",
            ),
            (
                "match-type-bool",
                lambda row: row["tessellation"]["itessellation"]["properties"].__setitem__(
                    "match_type", False
                ),
                "properties are incomplete",
            ),
            (
                "interface",
                lambda row: row["tessellation"]["itessellation"].__setitem__(
                    "interface_iid", "invented"
                ),
                "interface is unsupported",
            ),
        )
        for label, mutate, message in mutations:
            with self.subTest(label=label):
                self.row = copy.deepcopy(original)
                mutate(self.row)
                with self.assertRaisesRegex(PipelineError, message):
                    self.verify()

    def test_body_local_face_facet_fin_and_vertex_ids_fail_closed(self):
        original = copy.deepcopy(self.row)

        def body(row):
            return row["tessellation"]["itessellation"]["bodies"][0]

        def misowned_facet(row):
            item = body(row)
            item["faces"][1]["facet_ids"].append(item["faces"][0]["facet_ids"][0])
            item["faces"][1]["facet_ids"].sort()
            item["faces"][1]["facet_count"] = len(item["faces"][1]["facet_ids"])
            item["faces"][1]["raw_facet_ids"] = list(
                item["faces"][1]["facet_ids"]
            )

        def missing_fin(row):
            body(row)["facets"][0]["fin_ids"].pop()

        def duplicate_fin_vertex(row):
            fin = body(row)["fins"][0]
            fin["vertex_ids"] = [fin["vertex_ids"][0], fin["vertex_ids"][0]]

        def fin_side_from_other_facet(row):
            item = body(row)
            fin = item["fins"][0]
            other_vertex_id = item["facets"][1]["vertex_ids"][0]
            fin["vertex_ids"][1] = other_vertex_id
            vertex_set = set(fin["vertex_ids"])
            vertex_set.add(item["facets"][1]["vertex_ids"][1])
            item["facets"][1]["vertex_ids"] = sorted(vertex_set)

        def reversed_fin(row):
            item = body(row)
            fin_id = item["facets"][0]["fin_ids"][0]
            fin = next(fin for fin in item["fins"] if fin["fin_id"] == fin_id)
            fin["vertex_ids"].reverse()
            fin["start_u"], fin["end_u"] = fin["end_u"], fin["start_u"]
            fin["start_evidence"], fin["end_evidence"] = (
                fin["end_evidence"],
                fin["start_evidence"],
            )

        def partial_face_list(row):
            body(row)["request"]["face_list"]["face_count"] = 1

        def missing_match_type_evidence(row):
            body(row)["properties"].pop("match_type")

        cases = (
            ("misowned", misowned_facet, "has misowned facets"),
            ("three-fins", missing_fin, "facet 0 is malformed"),
            ("fin-vertices", duplicate_fin_vertex, "fin 0 is malformed"),
            ("fin-sides", fin_side_from_other_facet, "facet 1 is malformed"),
            ("reversed-fin", reversed_fin, "not a directed three-cycle"),
            ("partial-face-list", partial_face_list, "is incomplete"),
            ("missing-match-type", missing_match_type_evidence, "is incomplete"),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                self.row = copy.deepcopy(original)
                mutate(self.row)
                with self.assertRaisesRegex(PipelineError, message):
                    self.verify()

    def test_native_edge_fin_ids_are_exact_and_bidirectional(self):
        self.assertGreater(
            sum(
                fin["classification"] == "internal_only"
                for body in self.row["tessellation"]["itessellation"]["bodies"]
                for fin in body["fins"]
            ),
            0,
        )
        original = copy.deepcopy(self.row)
        edge = original["tessellation"]["native_edges"][0]
        body = next(
            body
            for body in original["tessellation"]["itessellation"]["bodies"]
            if (body["body_type"], body["body_index"])
            == (edge["body_type"], edge["body_index"])
        )
        internal_fin = next(
            fin for fin in body["fins"] if fin["classification"] == "internal_only"
        )

        missing_edge_fin = copy.deepcopy(original)
        missing_edge_fin["tessellation"]["native_edges"][0]["fin_ids"].pop()
        missing_edge_fin["tessellation"]["native_edges"][0][
            "native_fin_count"
        ] = len(
            missing_edge_fin["tessellation"]["native_edges"][0]["fin_ids"]
        )
        missing_edge_fin["tessellation"]["native_edges"][0][
            "raw_fin_ids"
        ] = list(
            missing_edge_fin["tessellation"]["native_edges"][0]["fin_ids"]
        )

        extra_internal_fin = copy.deepcopy(original)
        extra_internal_fin["tessellation"]["native_edges"][0]["fin_ids"].append(
            internal_fin["fin_id"]
        )
        extra_internal_fin["tessellation"]["native_edges"][0]["fin_ids"].sort()
        extra_internal_fin["tessellation"]["native_edges"][0][
            "native_fin_count"
        ] = len(
            extra_internal_fin["tessellation"]["native_edges"][0]["fin_ids"]
        )
        extra_internal_fin["tessellation"]["native_edges"][0][
            "raw_fin_ids"
        ] = list(
            extra_internal_fin["tessellation"]["native_edges"][0]["fin_ids"]
        )

        edge_fin = next(
            fin for fin in body["fins"] if fin["edge_reference_sha256"] == edge["edge_reference_sha256"]
        )
        conflicting_edge = copy.deepcopy(original)
        conflicting_body = next(
            item
            for item in conflicting_edge["tessellation"]["itessellation"]["bodies"]
            if (item["body_type"], item["body_index"])
            == (edge["body_type"], edge["body_index"])
        )
        conflicting_fin = next(
            fin for fin in conflicting_body["fins"] if fin["fin_id"] == edge_fin["fin_id"]
        )
        conflicting_fin["edge_reference_sha256"] = "f" * 64

        cases = (
            ("missing", missing_edge_fin, "fin_ids are not bidirectional"),
            ("internal", extra_internal_fin, "fin_ids are not bidirectional"),
            ("unknown-edge", conflicting_edge, "edge references differ"),
        )
        for label, row, message in cases:
            with self.subTest(label=label):
                self.row = row
                with self.assertRaisesRegex(PipelineError, message):
                    self.verify()

    def test_internal_fins_require_explicit_classification(self):
        original = copy.deepcopy(self.row)
        body = original["tessellation"]["itessellation"]["bodies"][0]
        fin = next(fin for fin in body["fins"] if fin["classification"] == "internal_only")

        missing = copy.deepcopy(original)
        missing_body = missing["tessellation"]["itessellation"]["bodies"][0]
        missing_fin = next(
            item for item in missing_body["fins"] if item["fin_id"] == fin["fin_id"]
        )
        missing_fin.pop("classification")

        misclassified = copy.deepcopy(original)
        misclassified_body = misclassified["tessellation"]["itessellation"]["bodies"][0]
        misclassified_fin = next(
            item for item in misclassified_body["fins"] if item["fin_id"] == fin["fin_id"]
        )
        misclassified_fin["classification"] = "native_edge"

        for label, row, message in (
            ("missing", missing, "fin [0-9]+ has an unknown shape"),
            ("native-without-edge", misclassified, "malformed or unclassified"),
        ):
            with self.subTest(label=label):
                self.row = row
                with self.assertRaisesRegex(PipelineError, message):
                    self.verify()

    def test_vertex_cache_never_selects_edges_by_coordinate(self):
        start = (0.0, 0.0, 0.0)
        end = (1.0, 0.0, 0.0)
        triangles = [
            (start, end, (0.5, -0.5, 0.0)),
            (end, start, (0.5, 0.5, 0.0)),
            (start, end, (0.5, 0.0, 0.5)),
        ]
        edge_reference = hashlib.sha256(b"persistent-native-edge").hexdigest()
        body_reference = hashlib.sha256(b"coordinate-collision-body").hexdigest()
        spans = [
            {
                "body_type": 0,
                "body_index": 0,
                "body_name": "coordinate-collision",
                "body_reference_sha256": body_reference,
                "face_index": face_index,
                "face_reference_sha256": hashlib.sha256(
                    f"coordinate-collision-face-{face_index}".encode()
                ).hexdigest(),
                "face_owner_id": face_index + 1,
                "edge_references": [edge_reference] if face_index < 2 else [],
                "triangle_start": face_index,
                "triangle_count": 1,
                "triangle_records_sha256": "0" * 64,
            }
            for face_index in range(3)
        ]
        native_edges = [
            {
                "body_type": 0,
                "body_index": 0,
                "edge_reference_sha256": edge_reference,
                "start_vertex_reference_sha256": hashlib.sha256(
                    b"persistent-start-reference"
                ).hexdigest(),
                "end_vertex_reference_sha256": hashlib.sha256(
                    b"persistent-end-reference"
                ).hexdigest(),
                "start_point_m": list(start),
                "end_point_m": list(end),
                "face_indices": [0, 1],
                "u_min": 0.0,
                "u_max": 1.0,
                "curve_sense": True,
                "curve_type": 0,
            }
        ]
        tessellation = support.native_tessellation_fixture(
            spans,
            native_edges,
            triangle_vertices=triangles,
        )
        vertices = {
            row["vertex_id"]: row["point_m"]
            for row in tessellation["itessellation"]["vertices"]
        }
        internal_fin = next(
            fin
            for fin in tessellation["itessellation"]["bodies"][0]["fins"]
            if fin["classification"] == "internal_only"
            and fin["face_index"] == 2
        )
        self.assertEqual(
            [vertices[vertex_id] for vertex_id in internal_fin["vertex_ids"]],
            [list(start), list(end)],
        )
        self.assertEqual(
            len(
                parse_native_tessellation(
                    "coordinate-collision",
                    tessellation,
                    {"solid": 1, "sheet": 0},
                    len(triangles),
                    triangles,
                )
            ),
            3,
        )

    def test_vertex_cache_rejects_duplicate_or_missing_vertex_rows(self):
        original = copy.deepcopy(self.row)

        self.row = copy.deepcopy(original)
        duplicate = copy.deepcopy(
            self.row["tessellation"]["itessellation"]["vertices"][0]
        )
        self.row["tessellation"]["itessellation"]["vertices"].append(duplicate)
        with self.assertRaisesRegex(PipelineError, "vertex.*duplicated or unordered"):
            self.verify()

        self.row = copy.deepcopy(original)
        self.row["tessellation"]["itessellation"]["vertices"].pop()
        with self.assertRaisesRegex(
            PipelineError,
            "vertex count differs|vertex cache differs",
        ):
            self.verify()

    def test_shared_native_cycle_binds_float32_stl_vertices(self):
        original_row = copy.deepcopy(self.row)
        original_stl = self.stl_bytes
        tessellation = original_row["tessellation"]
        body = tessellation["itessellation"]["bodies"][0]
        facet = body["facets"][0]
        record_index = facet["triangle_record_index"]
        record_start = 84 + 50 * record_index
        points_offset = record_start + 12
        points = [
            struct.unpack_from("<3f", original_stl, points_offset + 12 * index)
            for index in range(3)
        ]
        face_span = next(
            span
            for span in tessellation["face_spans"]
            if (span["body_type"], span["body_index"], span["face_index"])
            == (body["body_type"], body["body_index"], facet["face_index"])
        )

        def assert_rejected(replacement):
            payload = bytearray(original_stl)
            for index, point in enumerate(replacement):
                struct.pack_into("<3f", payload, points_offset + 12 * index, *point)
            self.row = copy.deepcopy(original_row)
            row_span = next(
                span
                for span in self.row["tessellation"]["face_spans"]
                if span["face_index"] == face_span["face_index"]
                and span["body_type"] == face_span["body_type"]
                and span["body_index"] == face_span["body_index"]
            )
            start = 84 + 50 * row_span["triangle_start"]
            end = start + 50 * row_span["triangle_count"]
            row_span["triangle_records_sha256"] = hashlib.sha256(
                payload[start:end]
            ).hexdigest()
            self.row["sha256"] = hashlib.sha256(payload).hexdigest()
            self.stl_bytes = bytes(payload)
            (self.root / "evidence" / self.row["path"]).write_bytes(self.stl_bytes)
            with self.assertRaisesRegex(
                PipelineError,
                "does not bind triangle record",
            ):
                self.verify()

        shifted = [list(point) for point in points]
        shifted[0][0] += 1.0
        assert_rejected(shifted)

        assert_rejected(tuple(reversed(points)))

    def test_facet_triangle_record_indexes_cover_each_face_once(self):
        original = copy.deepcopy(self.row)
        facets = original["tessellation"]["itessellation"]["bodies"][0]["facets"]

        duplicate = copy.deepcopy(original)
        duplicate["tessellation"]["itessellation"]["bodies"][0]["facets"][1][
            "triangle_record_index"
        ] = duplicate["tessellation"]["itessellation"]["bodies"][0]["facets"][0][
            "triangle_record_index"
        ]

        misplaced = copy.deepcopy(original)
        misplaced["tessellation"]["itessellation"]["bodies"][0]["facets"][0][
            "triangle_record_index"
        ] = facets[-1]["triangle_record_index"]

        for label, row in (("duplicate", duplicate), ("misplaced", misplaced)):
            with self.subTest(label=label):
                self.row = row
                with self.assertRaisesRegex(PipelineError, "duplicate or misplaced triangle index"):
                    self.verify()

    def test_cross_body_vertex_identity_is_reported_without_welding_bodies(self):
        result = self.verify()
        topology = result["topologies"][0]
        self.assertEqual(len(topology["bodies"]), 4)
        self.assertGreaterEqual(topology["cross_body_vertex_reference_count"], 1)
        self.assertEqual(
            [row["body_index"] for row in topology["bodies"]],
            [0, 1, 2, 3],
        )


class CurvedNativeEdgeFixtureTests(unittest.TestCase):
    def fixture(self):
        def binary32(point):
            return struct.unpack("<3f", struct.pack("<3f", *point))

        start = binary32((0.0, 0.0, 0.0))
        middle_a = binary32((0.25, 0.05, 0.0))
        middle_b = binary32((0.75, 0.05, 0.0))
        end = binary32((1.0, 0.0, 0.0))
        face_0_apex = binary32((0.5, -0.5, 0.0))
        face_1_apex = binary32((0.5, 0.5, 0.0))
        triangles = [
            (start, middle_a, face_0_apex),
            (middle_a, middle_b, face_0_apex),
            (middle_b, end, face_0_apex),
            (end, middle_b, face_1_apex),
            (middle_b, middle_a, face_1_apex),
            (middle_a, start, face_1_apex),
        ]
        edge_reference = hashlib.sha256(b"curved-native-edge").hexdigest()
        spans = []
        for face_index in range(2):
            spans.append(
                {
                    "body_type": 0,
                    "body_index": 0,
                    "body_name": "curved-body",
                    "body_reference_sha256": hashlib.sha256(b"curved-body").hexdigest(),
                    "face_index": face_index,
                    "face_reference_sha256": hashlib.sha256(
                        f"curved-face-{face_index}".encode()
                    ).hexdigest(),
                    "face_owner_id": face_index + 1,
                    "edge_references": [edge_reference],
                    "triangle_start": face_index * 3,
                    "triangle_count": 3,
                    "triangle_records_sha256": "0" * 64,
                }
            )
        edge = {
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
            "u_min": 0.0,
            "u_max": 1.0,
            "curve_sense": True,
            "curve_type": 1,
        }
        tessellation = support.native_tessellation_fixture(
            spans,
            [edge],
            triangle_vertices=triangles,
        )
        return tessellation, triangles

    def boundary_fixture(self, body_type=1):
        def binary32(point):
            return struct.unpack("<3f", struct.pack("<3f", *point))

        start = binary32((0.0, 0.0, 0.0))
        end = binary32((1.0, 0.0, 0.0))
        apex = binary32((0.5, 0.5, 0.0))
        triangles = [(start, end, apex)]
        edge_reference = hashlib.sha256(b"open-native-edge").hexdigest()
        span = {
            "body_type": body_type,
            "body_index": 0,
            "body_name": "open-body",
            "body_reference_sha256": hashlib.sha256(b"open-body").hexdigest(),
            "face_index": 0,
            "face_reference_sha256": hashlib.sha256(b"open-face").hexdigest(),
            "face_owner_id": 1,
            "edge_references": [edge_reference],
            "triangle_start": 0,
            "triangle_count": 1,
            "triangle_records_sha256": "0" * 64,
        }
        edge = {
            "body_type": body_type,
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
            "face_indices": [0],
            "u_min": 0.0,
            "u_max": 1.0,
            "curve_sense": True,
            "curve_type": 1,
        }
        tessellation = support.native_tessellation_fixture(
            [span],
            [edge],
            triangle_vertices=triangles,
        )
        return tessellation, triangles

    def parse(self, tessellation, triangles, bodies=None):
        return parse_native_tessellation(
            "curved-body",
            tessellation,
            bodies or {"solid": 1, "sheet": 0},
            len(triangles),
            triangles,
        )

    def edge_fins(self, tessellation):
        edge = tessellation["native_edges"][0]
        fins = {
            row["fin_id"]: row
            for row in tessellation["itessellation"]["bodies"][0]["fins"]
        }
        return edge, [fins[fin_id] for fin_id in edge["fin_ids"]]

    def clear_edge_fins(self, tessellation):
        edge = tessellation["native_edges"][0]
        fin_ids = set(edge["fin_ids"])
        edge["fin_ids"] = []
        edge["native_fin_count"] = 0
        edge["raw_fin_ids"] = []
        for fin in tessellation["itessellation"]["bodies"][0]["fins"]:
            if fin["fin_id"] not in fin_ids:
                continue
            fin.update(
                {
                    "edge_reference_sha256": None,
                    "classification": "internal_only",
                    "start_u": None,
                    "end_u": None,
                    "start_evidence": None,
                    "end_evidence": None,
                    "cofin_raw_id": None,
                    "cofin_id": None,
                }
            )

    def set_u(self, fin, start_u, end_u):
        fin["start_u"] = start_u
        fin["end_u"] = end_u
        for evidence, value in (
            (fin["start_evidence"], start_u),
            (fin["end_evidence"], end_u),
        ):
            evidence["u"] = value
            evidence["closest_point_on_raw"][3] = value

    def test_open_edge_accepts_one_boundary_fin_and_summarizes_it(self):
        tessellation, triangles = self.boundary_fixture()
        edge, fins = self.edge_fins(tessellation)
        self.assertEqual(len(edge["fin_ids"]), 1)
        self.assertEqual(len(fins), 1)
        self.assertEqual(fins[0]["cofin_raw_id"], -1)
        self.assertIsNone(fins[0]["cofin_id"])
        spans = self.parse(
            tessellation,
            triangles,
            {"solid": 0, "sheet": 1},
        )
        self.assertEqual(len(spans), 1)
        summary = summarize_native_topology(tessellation, spans)
        body = summary["bodies"][0]
        self.assertEqual(body["open_edges"], 1)
        self.assertEqual(body["shared_edges"], 0)
        self.assertEqual(body["boundary_fins"], 1)
        self.assertEqual(body["paired_fins"], 0)

    def test_boundary_raw_and_normalized_id_must_agree(self):
        for raw, normalized in ((0, None), (-1, 101), (True, None), (None, None)):
            with self.subTest(raw=raw, normalized=normalized):
                tessellation, triangles = self.boundary_fixture()
                _edge, fins = self.edge_fins(tessellation)
                fins[0]["cofin_raw_id"] = raw
                fins[0]["cofin_id"] = normalized
                with self.assertRaisesRegex(PipelineError, "malformed|boundary"):
                    self.parse(
                        tessellation,
                        triangles,
                        {"solid": 0, "sheet": 1},
                    )

    def test_shared_edge_rejects_boundary_raw_with_matching_null_id(self):
        tessellation, triangles = self.fixture()
        _edge, fins = self.edge_fins(tessellation)
        fins[0]["cofin_raw_id"] = -1
        fins[0]["cofin_id"] = None
        with self.assertRaisesRegex(PipelineError, "boundary fin|malformed"):
            self.parse(tessellation, triangles)

    def test_nonmanifold_edge_incidence_is_rejected(self):
        tessellation, triangles = self.fixture()
        edge = tessellation["native_edges"][0]
        edge["face_indices"] = [0, 1, 2]
        edge["face_senses"].append(
            {
                "face_index": 2,
                "EdgeInFaceSense": {"value": True},
            }
        )
        with self.assertRaisesRegex(PipelineError, "non-manifold"):
            self.parse(tessellation, triangles)

    def test_solid_body_cannot_use_open_boundary_semantics(self):
        tessellation, triangles = self.boundary_fixture(body_type=0)
        with self.assertRaisesRegex(PipelineError, "only on a sheet body"):
            self.parse(
                tessellation,
                triangles,
                {"solid": 1, "sheet": 0},
            )

    def test_open_and_shared_edges_reject_empty_fin_sets(self):
        for fixture in (self.fixture, self.boundary_fixture):
            with self.subTest(fixture=fixture.__name__):
                tessellation, triangles = fixture()
                self.clear_edge_fins(tessellation)
                bodies = (
                    {"solid": 0, "sheet": 1}
                    if fixture == self.boundary_fixture
                    else {"solid": 1, "sheet": 0}
                )
                with self.assertRaisesRegex(
                    PipelineError,
                    "did not return any fins",
                ):
                    self.parse(tessellation, triangles, bodies)

    def test_topology_summary_rejects_an_empty_open_edge(self):
        tessellation, triangles = self.boundary_fixture()
        spans = self.parse(
            tessellation,
            triangles,
            {"solid": 0, "sheet": 1},
        )
        self.clear_edge_fins(tessellation)
        with self.assertRaisesRegex(
            PipelineError,
            "did not return any fins",
        ):
            summarize_native_topology(tessellation, spans)

    def test_topology_summary_rejects_boundary_on_wrong_face(self):
        tessellation, triangles = self.boundary_fixture()
        spans = self.parse(
            tessellation,
            triangles,
            {"solid": 0, "sheet": 1},
        )
        first = tessellation["itessellation"]["bodies"][0]["fins"][0]
        first["face_index"] = 1
        with self.assertRaises(PipelineError):
            summarize_native_topology(tessellation, spans)

    def test_topology_summary_rejects_shared_cofin_outside_edge(self):
        tessellation, triangles = self.fixture()
        spans = self.parse(tessellation, triangles)
        edge, fins = self.edge_fins(tessellation)
        internal = next(
            row
            for row in tessellation["itessellation"]["bodies"][0]["fins"]
            if row["fin_id"] not in edge["fin_ids"]
        )
        fins[0]["cofin_id"] = internal["fin_id"]
        fins[0]["cofin_raw_id"] = internal["fin_id"]
        with self.assertRaisesRegex(PipelineError, "outside the same edge"):
            summarize_native_topology(tessellation, spans)

    def test_topology_summary_rejects_raw_normalized_cofin_mismatch(self):
        tessellation, triangles = self.fixture()
        spans = self.parse(tessellation, triangles)
        _edge, fins = self.edge_fins(tessellation)
        fins[0]["cofin_raw_id"] = 999999
        fins[0]["cofin_id"] = 999998
        with self.assertRaises(PipelineError):
            summarize_native_topology(tessellation, spans)

    def test_topology_summary_rejects_duplicate_declared_fin_id(self):
        tessellation, triangles = self.fixture()
        spans = self.parse(tessellation, triangles)
        edge = tessellation["native_edges"][0]
        edge["fin_ids"] = [edge["fin_ids"][0], edge["fin_ids"][0]]
        with self.assertRaises(PipelineError):
            summarize_native_topology(tessellation, spans)

    def test_topology_summary_rejects_invalid_face_sense_inventory(self):
        cases = (
            (
                "duplicate-face",
                lambda edge: edge["face_senses"][1].__setitem__(
                    "face_index",
                    edge["face_senses"][0]["face_index"],
                ),
            ),
            (
                "missing-secondary",
                lambda edge: edge["face_senses"][0].pop(
                    "IEdgeInFaceSense2"
                ),
            ),
            (
                "secondary-disagrees",
                lambda edge: edge["face_senses"][0][
                    "IEdgeInFaceSense2"
                ].update(
                    {
                        "value": not edge["face_senses"][0][
                            "EdgeInFaceSense"
                        ]["value"]
                    }
                ),
            ),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                tessellation, triangles = self.fixture()
                spans = self.parse(tessellation, triangles)
                mutate(tessellation["native_edges"][0])
                with self.assertRaisesRegex(
                    PipelineError,
                    "face.?sense|orientation",
                ):
                    summarize_native_topology(tessellation, spans)

    def test_parser_and_summary_reject_face_sense_index_alias_types(self):
        for face_index in (False, 0.0):
            with self.subTest(face_index=face_index):
                tessellation, triangles = self.fixture()
                spans = self.parse(tessellation, triangles)
                tessellation["native_edges"][0]["face_senses"][0][
                    "face_index"
                ] = face_index
                with self.assertRaisesRegex(
                    PipelineError,
                    "face.?sense|orientation|malformed",
                ):
                    self.parse(tessellation, triangles)
                with self.assertRaisesRegex(PipelineError, "face.?sense"):
                    summarize_native_topology(tessellation, spans)

    def test_parser_and_summary_reject_boolean_fin_identity(self):
        for field, value in (("face_index", False), ("facet_id", True)):
            with self.subTest(field=field):
                tessellation, triangles = self.fixture()
                spans = self.parse(tessellation, triangles)
                fin = next(
                    row
                    for row in tessellation["itessellation"]["bodies"][0][
                        "fins"
                    ]
                    if row["classification"] == "native_edge"
                )
                fin[field] = value
                with self.assertRaisesRegex(
                    PipelineError,
                    "malformed|identity",
                ):
                    self.parse(tessellation, triangles)
                with self.assertRaisesRegex(
                    PipelineError,
                    "malformed|identity",
                ):
                    summarize_native_topology(tessellation, spans)

    def test_shared_validator_rejects_boolean_edge_and_fin_identity(self):
        fin = {
            "fin_id": 1,
            "facet_id": 1,
            "face_index": 0,
            "cofin_raw_id": -1,
            "cofin_id": None,
            "vertex_ids": [1, 2],
            "start_u": 0.0,
            "end_u": 1.0,
        }
        edge = {
            "body_type": 1,
            "face_indices": [0],
            "fin_ids": [1],
            "face_senses": [
                {
                    "face_index": 0,
                    "EdgeInFaceSense": {"value": False, "error": None},
                    "IEdgeInFaceSense2": {"value": False, "error": None},
                }
            ],
        }
        vertices = {1: (0.0, 0.0, 0.0), 2: (1.0, 0.0, 0.0)}
        validate_native_edge_fin_relations(
            component="validator",
            edge=edge,
            edge_fins=[1],
            incident_face_facet_counts=[1],
            fins_by_id={1: fin},
            vertices_by_id=vertices,
        )
        with self.assertRaisesRegex(NativeFinRelationError, "invalid ID type"):
            validate_native_edge_fin_relations(
                component="validator",
                edge=edge,
                edge_fins=[True],
                incident_face_facet_counts=[1],
                fins_by_id={1: fin},
                vertices_by_id=vertices,
            )
        bool_body_edge = {**edge, "body_type": True}
        with self.assertRaisesRegex(NativeFinRelationError, "invalid body type"):
            validate_native_edge_fin_relations(
                component="validator",
                edge=bool_body_edge,
                edge_fins=[1],
                incident_face_facet_counts=[1],
                fins_by_id={1: fin},
                vertices_by_id=vertices,
            )
        empty_edge = {**edge, "fin_ids": []}
        relation = validate_native_edge_fin_relations(
            component="validator",
            edge=empty_edge,
            edge_fins=[],
            incident_face_facet_counts=[0],
            fins_by_id={},
            vertices_by_id={},
        )
        self.assertEqual(relation["collapsed_pairs"], [])
        with self.assertRaisesRegex(
            NativeFinRelationError,
            "did not return any fins",
        ):
            validate_native_edge_fin_relations(
                component="validator",
                edge=empty_edge,
                edge_fins=[],
                incident_face_facet_counts=[1],
                fins_by_id={},
                vertices_by_id={},
            )
        with self.assertRaisesRegex(
            NativeFinRelationError,
            "did not return any fins",
        ):
            validate_native_edge_fin_relations(
                component="validator",
                edge={**empty_edge, "body_type": 0},
                edge_fins=[],
                incident_face_facet_counts=[0],
                fins_by_id={},
                vertices_by_id={},
            )

    def test_parser_and_summary_reject_boolean_native_identity_fields(self):
        mutations = (
            (
                "facet-face",
                lambda tessellation: tessellation["itessellation"]["bodies"][
                    0
                ]["facets"][0].__setitem__("face_index", False),
            ),
            (
                "vertex-body-type",
                lambda tessellation: tessellation["itessellation"]["vertices"][
                    0
                ].__setitem__("body_type", True),
            ),
            (
                "vertex-body-index",
                lambda tessellation: tessellation["itessellation"]["vertices"][
                    0
                ].__setitem__("body_index", False),
            ),
            (
                "scope-vertex-body-type",
                lambda tessellation: tessellation["itessellation"][
                    "solidworks_vertex_identities"
                ][0].__setitem__("body_type", True),
            ),
            (
                "scope-vertex-body-index",
                lambda tessellation: tessellation["itessellation"][
                    "solidworks_vertex_identities"
                ][0].__setitem__("body_index", False),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                tessellation, triangles = self.boundary_fixture()
                spans = self.parse(
                    tessellation,
                    triangles,
                    {"solid": 0, "sheet": 1},
                )
                mutate(tessellation)
                with self.assertRaises(PipelineError):
                    self.parse(
                        tessellation,
                        triangles,
                        {"solid": 0, "sheet": 1},
                    )
                with self.assertRaises(PipelineError):
                    summarize_native_topology(tessellation, spans)

    def test_topology_summary_rejects_inexact_declared_fin_ids(self):
        mutations = (
            ("unsorted", lambda fin_ids: list(reversed(fin_ids))),
            ("bool", lambda fin_ids: [True, *fin_ids[1:]]),
            ("float", lambda fin_ids: [float(fin_ids[0]), *fin_ids[1:]]),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                tessellation, triangles = self.fixture()
                spans = self.parse(tessellation, triangles)
                edge = tessellation["native_edges"][0]
                edge["fin_ids"] = mutate(edge["fin_ids"])
                with self.assertRaises(PipelineError):
                    summarize_native_topology(tessellation, spans)

    def test_topology_summary_rejects_invalid_vertex_and_u_evidence(self):
        tessellation, triangles = self.fixture()
        spans = self.parse(tessellation, triangles)
        fin = tessellation["itessellation"]["bodies"][0]["fins"][0]
        fin["vertex_ids"] = [999999, 1000000]
        with self.assertRaises(PipelineError):
            summarize_native_topology(tessellation, spans)

        tessellation, triangles = self.fixture()
        spans = self.parse(tessellation, triangles)
        _edge, fins = self.edge_fins(tessellation)
        fins[0]["start_u"] = -999.0
        fins[0]["end_u"] = float("inf")
        with self.assertRaises(PipelineError):
            summarize_native_topology(tessellation, spans)

        tessellation, triangles = self.fixture()
        spans = self.parse(tessellation, triangles)
        _edge, fins = self.edge_fins(tessellation)
        first, cofin = fins[:2]
        self.set_u(first, 2.0, 1.0)
        self.set_u(cofin, 1.0, 2.0)
        with self.assertRaises(PipelineError):
            summarize_native_topology(tessellation, spans)

    def test_topology_summary_rejects_facet_face_mismatch(self):
        tessellation, triangles = self.fixture()
        spans = self.parse(tessellation, triangles)
        _edge, fins = self.edge_fins(tessellation)
        first = fins[0]
        second = next(
            fin for fin in fins[1:] if fin["face_index"] != first["face_index"]
        )
        first["face_index"], second["face_index"] = (
            second["face_index"],
            first["face_index"],
        )
        with self.assertRaises(PipelineError):
            summarize_native_topology(tessellation, spans)

    def test_topology_summary_rejects_boolean_identity_aliases(self):
        tessellation, triangles = self.boundary_fixture()
        spans = self.parse(
            tessellation,
            triangles,
            {"solid": 0, "sheet": 1},
        )
        tessellation["native_edges"][0]["fin_ids"] = [True]
        with self.assertRaises(PipelineError):
            summarize_native_topology(tessellation, spans)

        tessellation, triangles = self.boundary_fixture()
        spans = self.parse(
            tessellation,
            triangles,
            {"solid": 0, "sheet": 1},
        )
        body = tessellation["itessellation"]["bodies"][0]
        body["body_type"] = True
        with self.assertRaises(PipelineError):
            summarize_native_topology(tessellation, spans)

        tessellation, triangles = self.boundary_fixture()
        spans = self.parse(
            tessellation,
            triangles,
            {"solid": 0, "sheet": 1},
        )
        facet = tessellation["itessellation"]["bodies"][0]["facets"][0]
        facet["facet_id"] = True
        with self.assertRaises(PipelineError):
            summarize_native_topology(tessellation, spans)

    def test_parser_and_summary_reject_non_integer_normalized_cofin(self):
        for normalized in (True, 1.0):
            with self.subTest(normalized=normalized):
                tessellation, triangles = self.fixture()
                spans = self.parse(tessellation, triangles)
                _edge, fins = self.edge_fins(tessellation)
                fins[0]["cofin_raw_id"] = 1
                fins[0]["cofin_id"] = normalized
                with self.assertRaisesRegex(
                    PipelineError,
                    "raw and normalized|malformed",
                ):
                    self.parse(tessellation, triangles)
                with self.assertRaisesRegex(
                    PipelineError,
                    "raw and normalized|malformed",
                ):
                    summarize_native_topology(tessellation, spans)

    def test_curved_native_edge_allows_multiple_reciprocal_fins_per_face(self):
        tessellation, triangles = self.fixture()
        self.assertEqual(tessellation["schema_version"], NATIVE_TESSELLATION_SCHEMA)
        self.assertEqual(len(tessellation["itessellation"]["vertices"]), 6)
        faces = {}
        for row in tessellation["itessellation"]["bodies"][0]["facets"]:
            faces.setdefault(row["face_index"], set()).update(row["vertex_ids"])
        self.assertGreaterEqual(
            len(set.intersection(set().union(*faces.values()), *faces.values())),
            4,
        )
        edge, fins = self.edge_fins(tessellation)
        self.assertEqual(len(edge["fin_ids"]), 6)
        self.assertEqual({row["face_index"] for row in fins}, {0, 1})
        self.assertTrue(
            all(
                next(item for item in fins if item["fin_id"] == row["cofin_id"])[
                    "cofin_id"
                ]
                == row["fin_id"]
                and row["cofin_raw_id"] == row["cofin_id"]
                for row in fins
            )
        )
        intervals = sorted(
            (min(row["start_u"], row["end_u"]), max(row["start_u"], row["end_u"]))
            for row in fins
        )
        self.assertEqual(intervals[0][0], 0.0)
        self.assertEqual(intervals[-1][1], 1.0)
        self.assertTrue(
            all(
                intervals[index][1] >= intervals[index + 1][0]
                for index in range(len(intervals) - 1)
            )
        )
        internal = [
            row
            for row in tessellation["itessellation"]["bodies"][0]["fins"]
            if row["classification"] == "internal_only"
        ]
        self.assertTrue(internal)
        self.assertTrue(
            all(
                row["start_u"] is None
                and row["end_u"] is None
                and row["cofin_raw_id"] is None
                and row["cofin_id"] is None
                for row in internal
            )
        )
        self.assertEqual(len(self.parse(tessellation, triangles)), 2)

    def test_v5_schema_is_rejected_after_open_boundary_schema_bump(self):
        tessellation, triangles = self.fixture()
        tessellation["schema_version"] = (
            "solidworks-to-urdf.native-tessellation/v5"
        )
        with self.assertRaisesRegex(PipelineError, "schema"):
            self.parse(tessellation, triangles)

    def test_curved_native_edge_rejects_u_gap(self):
        tessellation, triangles = self.fixture()
        _edge, fins = self.edge_fins(tessellation)
        first = next(
            row
            for row in fins
            if abs(row["start_u"] - 1.0 / 3.0) < 1e-12
        )
        cofin = next(row for row in fins if row["fin_id"] == first["cofin_id"])
        self.set_u(first, 0.2, 0.4)
        self.set_u(cofin, 0.4, 0.2)
        with self.assertRaisesRegex(PipelineError, "U domain|U interval has a gap"):
            self.parse(tessellation, triangles)

    def test_curved_native_edge_rejects_missing_cofin(self):
        tessellation, triangles = self.fixture()
        _edge, fins = self.edge_fins(tessellation)
        fins[0]["cofin_id"] = None
        with self.assertRaisesRegex(PipelineError, "malformed|cofin"):
            self.parse(tessellation, triangles)

    def test_curve_sense_does_not_flip_raw_curve_endpoint_order(self):
        tessellation, triangles = self.fixture()
        edge = tessellation["native_edges"][0]
        edge["curve_sense"] = False
        edge["curve_domain_transform"] = native_curve_domain_transform(
            edge["curve_end_params"]["start"],
            edge["curve_end_params"]["end"],
            edge["u_min"],
            edge["u_max"],
            False,
            edge["curve_end_params"]["is_periodic"],
        )
        support.refresh_topology_endpoint_assignment(tessellation)

        self.assertEqual(
            len(self.parse(tessellation, triangles)),
            len(tessellation["face_spans"]),
        )
        self.assertIs(edge["curve_sense"], False)
        self.assertEqual(
            edge["topology_endpoint_assignment"]["canonical_result"],
            "direct",
        )

    def test_curved_native_edge_rejects_same_u_direction(self):
        tessellation, triangles = self.fixture()
        _edge, external_fins = self.edge_fins(tessellation)
        by_id = {
            fin["fin_id"]: fin
            for fin in tessellation["itessellation"]["bodies"][0]["fins"]
        }
        vertices = {
            row["vertex_id"]: row["point_m"]
            for row in tessellation["itessellation"]["vertices"]
        }
        for fin in by_id.values():
            if fin["face_index"] == 1:
                fin["vertex_ids"].reverse()
        for fin in external_fins:
            if fin["face_index"] != 1:
                continue
            cofin = by_id[fin["cofin_id"]]
            self.set_u(fin, cofin["start_u"], cofin["end_u"])
            for endpoint in ("start", "end"):
                point = vertices[fin["vertex_ids"][0 if endpoint == "start" else 1]]
                fin[f"{endpoint}_evidence"].update(
                    support._u_evidence(
                        fin[f"{endpoint}_u"],
                        point,
                    )
                )
        for index in (3, 4, 5):
            triangles[index] = tuple(reversed(triangles[index]))
        with self.assertRaisesRegex(
            PipelineError,
            "reciprocal cofin U intervals do not correspond",
        ):
            self.parse(tessellation, triangles)

