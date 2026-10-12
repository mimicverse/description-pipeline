"""Untessellated CAD sheets are explicit evidence, never missing solid facets."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401

from description_pipeline.io import PipelineError
from description_pipeline.verification.native_mesh_observations import verify_native_mesh_observations


class NativeMeshObservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "evidence/raw").mkdir(parents=True)
        (self.root / "evidence/manifest.json").write_text(json.dumps({
            "identity": {"capture": {"evidence_class": "cad"}},
        }))
        self.row = {
            "component": "pcb", "bodies": {"solid": 0, "sheet": 4},
            "tessellation": {"faces": 12, "no_facets_faces": [{
                "body_type": 1, "body_index": 2, "face_index": 0,
                "triangle_count": 0, "facet_count": 0, "area_m2": 2e-13,
                "tessellate_result": False,
                "used_api": "IBody2.GetTessellation/ITessellation.Tessellate/GetFaceFacets",
            }]},
        }

    def verify(self):
        (self.root / "evidence/raw/geometry.json").write_text(json.dumps([self.row]))
        return verify_native_mesh_observations(self.root)

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

