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


def binary_stl_triangles(payload: bytes) -> list[tuple[tuple[float, ...], ...]]:
    count = struct.unpack_from("<I", payload, 80)[0]
    return [
        tuple(
            struct.unpack_from("<3f", payload, 84 + 50 * triangle_index + 12 + 12 * vertex)
            for vertex in range(3)
        )
        for triangle_index in range(count)
    ]


def itessellation_fixture(
    spans: list[dict],
    native_edges: list[dict],
    stl_triangles: list[tuple[tuple[float, ...], ...]],
) -> dict:
    """Build strict body-local synthetic ITessellation evidence."""

    body_keys = sorted({(span["body_type"], span["body_index"]) for span in spans})
    body_rows = []
    vertex_rows = []
    for body_type, body_index in body_keys:
        body_key = (body_type, body_index)
        body_spans = [
            span
            for span in spans
            if (span["body_type"], span["body_index"]) == body_key
        ]
        next_facet_id = 1
        next_fin_id = 1
        next_vertex_id = 1
        faces = []
        facets = []
        fins = []
        facets_by_face = {}
        for span in body_spans:
            facet_ids = []
            for local_index in range(span["triangle_count"]):
                triangle_record_index = span["triangle_start"] + local_index
                triangle = stl_triangles[triangle_record_index]
                facet_id = next_facet_id
                next_facet_id += 1
                vertex_ids = [next_vertex_id, next_vertex_id + 1, next_vertex_id + 2]
                next_vertex_id += 3
                for vertex_id, point in zip(vertex_ids, triangle, strict=True):
                    vertex_rows.append(
                        {
                            "body_type": body_type,
                            "body_index": body_index,
                            "vertex_id": vertex_id,
                            "point_m": list(point),
                        }
                    )
                fin_ids = [next_fin_id, next_fin_id + 1, next_fin_id + 2]
                next_fin_id += 3
                for fin_id, side in zip(
                    fin_ids,
                    (
                        (vertex_ids[0], vertex_ids[1]),
                        (vertex_ids[1], vertex_ids[2]),
                        (vertex_ids[2], vertex_ids[0]),
                    ),
                    strict=True,
                ):
                    fins.append(
                        {
                            "fin_id": fin_id,
                            "facet_id": facet_id,
                            "face_index": span["face_index"],
                            "vertex_ids": list(side),
                            "edge_reference_sha256": None,
                            "classification": "internal_only",
                        }
                    )
                facet = {
                    "facet_id": facet_id,
                    "face_index": span["face_index"],
                    "fin_ids": fin_ids,
                    "vertex_ids": vertex_ids,
                    "triangle_record_index": triangle_record_index,
                }
                facets.append(facet)
                facets_by_face.setdefault(span["face_index"], []).append(facet)
                facet_ids.append(facet_id)
            faces.append(
                {
                    "face_index": span["face_index"],
                    "facet_ids": facet_ids,
                }
            )

        fins_by_id = {fin["fin_id"]: fin for fin in fins}
        for edge in native_edges:
            if (edge["body_type"], edge["body_index"]) != body_key:
                continue
            edge_fin_ids = []
            for face_index in edge["face_indices"]:
                candidates = facets_by_face.get(face_index, [])
                if not candidates:
                    continue
                fin_id = next(
                    fin_id
                    for fin_id in candidates[0]["fin_ids"]
                    if fins_by_id[fin_id]["classification"] == "internal_only"
                )
                fin = fins_by_id[fin_id]
                fin["edge_reference_sha256"] = edge["edge_reference_sha256"]
                fin["classification"] = "native_edge"
                edge_fin_ids.append(fin_id)
            edge["fin_ids"] = sorted(edge_fin_ids)

        body_rows.append(
            {
                "body_type": body_type,
                "body_index": body_index,
                "body_reference_sha256": body_spans[0]["body_reference_sha256"],
                "tessellate_result": True,
                "facet_count": len(facets),
                "fin_count": len(fins),
                "vertex_count": next_vertex_id - 1,
                "faces": faces,
                "facets": facets,
                "fins": fins,
            }
        )
    vertex_rows.sort(
        key=lambda row: (row["body_type"], row["body_index"], row["vertex_id"])
    )
    return {
        "interface_iid": "{699C7492-DB56-4CC2-B7A2-9FFD56698434}",
        "properties": {
            "need_face_facet_map": True,
            "need_edge_fin_map": True,
            "need_vertex_params": True,
        },
        "bodies": body_rows,
        "vertices": vertex_rows,
    }


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
        itessellation = itessellation_fixture(
            spans,
            native_edges,
            binary_stl_triangles(self.stl_bytes),
        )
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
                "itessellation": itessellation,
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
                "facet 2 has duplicate or misplaced triangle index",
            ),
            ("missing-body-edges", missing_body_edge_evidence, "topology evidence is unresolved"),
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

        cases = (
            ("misowned", misowned_facet, "has misowned facets"),
            ("three-fins", missing_fin, "facet 0 is malformed"),
            ("fin-vertices", duplicate_fin_vertex, "fin 0 is malformed"),
            ("fin-sides", fin_side_from_other_facet, "not a directed three-cycle"),
            ("reversed-fin", reversed_fin, "not a directed three-cycle"),
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

        extra_internal_fin = copy.deepcopy(original)
        extra_internal_fin["tessellation"]["native_edges"][0]["fin_ids"].append(
            internal_fin["fin_id"]
        )
        extra_internal_fin["tessellation"]["native_edges"][0]["fin_ids"].sort()

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
        original = copy.deepcopy(self.row)
        itessellation = original["tessellation"]["itessellation"]
        body = itessellation["bodies"][0]
        internal_fin = next(fin for fin in body["fins"] if fin["classification"] == "internal_only")
        vertices = {
            vertex["vertex_id"]: vertex
            for vertex in itessellation["vertices"]
            if (vertex["body_type"], vertex["body_index"]) == (1, 0)
        }
        edge = original["tessellation"]["native_edges"][0]
        edge["start_point_m"] = list(vertices[internal_fin["vertex_ids"][0]]["point_m"])
        edge["end_point_m"] = list(vertices[internal_fin["vertex_ids"][1]]["point_m"])
        self.row = original
        self.assertEqual(self.verify()["components"], 1)

        self.row = copy.deepcopy(original)
        duplicate = copy.deepcopy(self.row["tessellation"]["itessellation"]["vertices"][0])
        self.row["tessellation"]["itessellation"]["vertices"].append(duplicate)
        with self.assertRaisesRegex(PipelineError, "vertex.*duplicated or unordered"):
            self.verify()

        self.row = copy.deepcopy(original)
        self.row["tessellation"]["itessellation"]["vertices"].pop()
        with self.assertRaisesRegex(PipelineError, "vertex count differs|vertex cache differs"):
            self.verify()

    def test_directed_native_cycle_binds_float32_stl_vertices(self):
        original = copy.deepcopy(self.row)
        itessellation = original["tessellation"]["itessellation"]
        body = itessellation["bodies"][0]
        facet = body["facets"][0]
        fins = {
            fin["fin_id"]: fin
            for fin in body["fins"]
        }
        first = fins[facet["fin_ids"][0]]
        cycle = [first["vertex_ids"][0]]
        current = first
        for _ in range(3):
            cycle.append(current["vertex_ids"][1])
            current = next(
                fin
                for fin_id in facet["fin_ids"]
                if (fin := fins[fin_id])["vertex_ids"][0] == current["vertex_ids"][1]
            )
        cycle = cycle[:3]
        rows = {
            row["vertex_id"]: row
            for row in itessellation["vertices"]
            if (row["body_type"], row["body_index"]) == (1, 0)
        }
        points = [list(rows[vertex_id]["point_m"]) for vertex_id in cycle]

        shifted = copy.deepcopy(original)
        shifted_rows = {
            row["vertex_id"]: row
            for row in shifted["tessellation"]["itessellation"]["vertices"]
            if (row["body_type"], row["body_index"]) == (1, 0)
        }
        shifted_rows[cycle[0]]["point_m"][0] += 100.0
        self.row = shifted
        with self.assertRaisesRegex(PipelineError, "does not bind triangle record"):
            self.verify()

        rotated = copy.deepcopy(original)
        rotated_rows = {
            row["vertex_id"]: row
            for row in rotated["tessellation"]["itessellation"]["vertices"]
            if (row["body_type"], row["body_index"]) == (1, 0)
        }
        for index, vertex_id in enumerate(cycle):
            rotated_rows[vertex_id]["point_m"] = points[(index - 1) % 3]
        self.row = rotated
        self.assertEqual(self.verify()["components"], 1)

        reversed_points = copy.deepcopy(original)
        reversed_rows = {
            row["vertex_id"]: row
            for row in reversed_points["tessellation"]["itessellation"]["vertices"]
            if (row["body_type"], row["body_index"]) == (1, 0)
        }
        for index, vertex_id in enumerate(cycle):
            reversed_rows[vertex_id]["point_m"] = points[-index]
        self.row = reversed_points
        with self.assertRaisesRegex(PipelineError, "does not bind triangle record"):
            self.verify()

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
        original = copy.deepcopy(self.row)
        shared_vertex = original["tessellation"]["native_edges"][0]["start_vertex_reference_sha256"]
        for body_index, face_index in ((1, 0), (2, 1)):
            edge_reference = hashlib.sha256(f"cross-body-{body_index}".encode()).hexdigest()
            body = next(
                body
                for body in original["tessellation"]["itessellation"]["bodies"]
                if body["body_index"] == body_index
            )
            fin = next(
                fin
                for fin in body["fins"]
                if fin["face_index"] == face_index
                and fin["classification"] == "internal_only"
            )
            fin["edge_reference_sha256"] = edge_reference
            fin["classification"] = "native_edge"
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
                    "fin_ids": [fin["fin_id"]],
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

