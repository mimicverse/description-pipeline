"""Untessellated CAD sheets are explicit evidence, never missing solid facets."""

import copy
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401
from . import support

from description_pipeline.io import PipelineError
from description_pipeline.schemas import NATIVE_TESSELLATION_SCHEMA
from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.verification.native_mesh_observations import verify_native_mesh_observations


def binary_stl(count: int, *, owners: list[int] | None = None) -> bytes:
    owners = owners or [0] * count
    assert len(owners) == count
    payload = bytearray(b"fixture".ljust(80, b" ") + struct.pack("<I", count))
    for index in range(count):
        x = float(index)
        payload.extend(
            struct.pack(
                "<12fH",
                0.0,
                0.0,
                1.0,
                x,
                0.0,
                0.0,
                x + 1.0,
                0.0,
                0.0,
                x,
                1.0,
                0.0,
                owners[index],
            )
        )
    return bytes(payload)


class NativeMeshObservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        triangle_owners = [1, 2, 3, 4, 5, 6, 8, 8, 9, 10, 11, 12]
        self.stl_bytes = binary_stl(12, owners=triangle_owners)
        stl_path = self.root / "evidence/geometry/fixture.stl"
        stl_path.parent.mkdir(parents=True, exist_ok=True)
        stl_path.write_bytes(self.stl_bytes)
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
            (1, 0, 0): 1,
            (1, 0, 1): 1,
            (1, 0, 2): 1,
            (1, 1, 0): 1,
            (1, 1, 1): 1,
            (1, 1, 2): 1,
            (1, 2, 0): 0,
            (1, 2, 1): 2,
            (1, 3, 0): 1,
            (1, 3, 1): 1,
            (1, 3, 2): 1,
            (1, 3, 3): 1,
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
                    "face_owner_id": ordinal + 1,
                    "edge_references": [],
                    "triangle_start": start,
                    "triangle_count": count,
                    "triangle_records_sha256": hashlib.sha256(
                        self.stl_bytes[84 + 50 * start : 84 + 50 * (start + count)]
                    ).hexdigest(),
                }
            )
            start += count
        shared_edge = hashlib.sha256(b"shared-edge").hexdigest()
        spans[0]["edge_references"] = [shared_edge]
        spans[1]["edge_references"] = [shared_edge]
        native_edges = [
            {
                "body_type": 1,
                "body_index": 0,
                "edge_reference_sha256": shared_edge,
                "start_vertex_reference_sha256": hashlib.sha256(b"vertex-0").hexdigest(),
                "end_vertex_reference_sha256": hashlib.sha256(b"vertex-1").hexdigest(),
                "start_point_m": [0.0, 0.0, 0.0],
                "end_point_m": [1.0, 0.0, 0.0],
                "face_indices": [0, 1],
            }
        ]
        for body_index, face_index in ((1, 0), (2, 0), (3, 0)):
            reference = hashlib.sha256(f"body-{body_index}-edge".encode()).hexdigest()
            native_edges.append(
                {
                    "body_type": 1,
                    "body_index": body_index,
                    "edge_reference_sha256": reference,
                    "start_vertex_reference_sha256": hashlib.sha256(
                        f"body-{body_index}-vertex-0".encode()
                    ).hexdigest(),
                    "end_vertex_reference_sha256": hashlib.sha256(
                        f"body-{body_index}-vertex-1".encode()
                    ).hexdigest(),
                    "start_point_m": [0.0, float(body_index), 0.0],
                    "end_point_m": [1.0, float(body_index), 0.0],
                    "face_indices": [face_index],
                }
            )
            span = next(
                entry
                for entry in spans
                if entry["body_index"] == body_index
                and entry["face_index"] == face_index
            )
            span["edge_references"].append(reference)
        self.row = {
            "component": "pcb",
            "path": "geometry/fixture.stl",
            "triangles": 12,
            "sha256": hashlib.sha256(self.stl_bytes).hexdigest(),
            "bodies": {"solid": 0, "sheet": 4},
            "tessellation": {
                "schema_version": NATIVE_TESSELLATION_SCHEMA,
                "faces": 12,
                "face_spans": spans,
                "native_edges": native_edges,
                "no_facets_faces": [
                    {
                        "body_type": 1,
                        "body_index": 2,
                        "face_index": 0,
                        "triangle_count": 0,
                        "facet_count": 0,
                        "area_m2": 2e-13,
                        "tessellate_result": False,
                        "used_api": "IBody2.GetTessellation/ITessellation.Tessellate/GetFaceFacets",
                    }
                ],
            },
        }

    def verify(self, capture_class: str = "cad"):
        # Adversarial rows keep NaN/Infinity literals on purpose: the verifier
        # must reject them semantically, so the block bytes are written raw.
        path = support.capture_file_for(self.root)
        capture = json.loads(path.read_text(encoding="utf-8"))
        capture["geometry"] = [self.row]
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
            row["triangles"] -= 4

        def wrong_span_digest(row):
            row["tessellation"]["face_spans"][0]["triangle_records_sha256"] = "0" * 64

        def wrong_stl_digest(row):
            row["sha256"] = "0" * 64

        def missing_facetless_row(row):
            row["tessellation"]["no_facets_faces"] = []

        def edge_incidence_mismatch(row):
            row["tessellation"]["native_edges"][0]["face_indices"] = [0]

        def missing_reverse_edge_incidence(row):
            shared = row["tessellation"]["native_edges"][0]["edge_reference_sha256"]
            row["tessellation"]["face_spans"][1]["edge_references"].remove(shared)

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
            for span in row["tessellation"]["face_spans"]:
                if span["body_index"] == 3:
                    span["edge_references"] = []

        cases = (
            ("legacy", legacy, "unknown or legacy"),
            ("boolean-body", boolean_body_type, "face span 0 is malformed"),
            ("gap", gap, "gap or overlaps"),
            ("body-coverage", body_not_covered, "cover every native body"),
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
            ("boundary-shift", boundary_shift_with_recomputed_digest, "triangle owners"),
            ("missing-body-edges", missing_body_edge_evidence, "topology evidence is unresolved"),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                self.row = copy.deepcopy(original)
                mutate(self.row)
                with self.assertRaisesRegex(PipelineError, message):
                    self.verify()

    def test_cross_body_vertex_identity_is_reported_without_welding_bodies(self):
        original = copy.deepcopy(self.row)
        shared_vertex = original["tessellation"]["native_edges"][0]["start_vertex_reference_sha256"]
        for body_index, face_index in ((1, 0), (2, 1)):
            edge_reference = hashlib.sha256(f"cross-body-{body_index}".encode()).hexdigest()
            original["tessellation"]["native_edges"].append(
                {
                    "body_type": 1,
                    "body_index": body_index,
                    "edge_reference_sha256": edge_reference,
                    "start_vertex_reference_sha256": shared_vertex,
                    "end_vertex_reference_sha256": hashlib.sha256(
                        f"end-{body_index}".encode()
                    ).hexdigest(),
                    "start_point_m": [0.0, 0.0, 0.0],
                    "end_point_m": [float(body_index), 0.0, 0.0],
                    "face_indices": [face_index],
                }
            )
            span = next(
                entry
                for entry in original["tessellation"]["face_spans"]
                if entry["body_index"] == body_index and entry["face_index"] == face_index
            )
            span["edge_references"].append(edge_reference)
        self.row = original
        result = self.verify()
        topology = result["topologies"][0]
        self.assertEqual(len(topology["bodies"]), 4)
        self.assertGreaterEqual(topology["cross_body_vertex_reference_count"], 1)
        self.assertEqual(
            [row["body_index"] for row in topology["bodies"]],
            [0, 1, 2, 3],
        )

