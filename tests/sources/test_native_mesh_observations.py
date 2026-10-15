"""Untessellated CAD sheets are explicit evidence, never missing solid facets."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401
from . import support

from description_pipeline.io import PipelineError
from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.verification.native_mesh_observations import verify_native_mesh_observations


class NativeMeshObservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
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
        self.row = {
            "component": "pcb", "bodies": {"solid": 0, "sheet": 4},
            "tessellation": {"faces": 12, "no_facets_faces": [{
                "body_type": 1, "body_index": 2, "face_index": 0,
                "triangle_count": 0, "facet_count": 0, "area_m2": 2e-13,
                "tessellate_result": False,
                "used_api": "IBody2.GetTessellation/ITessellation.Tessellate/GetFaceFacets",
            }]},
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

