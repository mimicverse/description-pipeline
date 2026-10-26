"""Focused contract checks for the native triangle facts and offline STL."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from description_pipeline.geometry.native_tessellation import (
    build_native_tessellation,
    iter_native_triangles,
    materialize_native_stl,
    native_stl_relative_path,
    native_triangle_stl_warning,
    native_triangle_relative_path,
    parse_native_tessellation,
    read_native_triangle_stream,
    summarize_native_topology,
    write_native_triangle_stream,
)
from description_pipeline.geometry.native_transform import transform_from_solidworks
from description_pipeline.io import PipelineError


class NativeTrianglesContractTest(unittest.TestCase):
    def setUp(self):
        self.component = "Root-1"
        self.source_scope = {
            "rule_version": "native-open-edge-scope-binding-v1",
            "component": self.component,
            "source_document_path": "C:/capture/source/part.sldprt",
            "source_copy_path": "source/part.sldprt",
            "source_document_sha256": "a" * 64,
            "source_copy_sha256": "a" * 64,
            "source_document_identity_sha256": "a" * 64,
            "source_document_identity_kind": "file_sha256",
            "referenced_configuration": "Default",
            "occurrence_scope": self.component,
            "virtual_owner": None,
            "virtual_owner_sha256": None,
            "virtual_session_document_path": None,
            "virtual_session_temp_root": None,
            "virtual_native_api_pid": None,
        }
        self.body_inventory = [
            {
                "body_type": 0,
                "body_index": 0,
                "body_name": "Body<1>",
                "face_count": 4,
            }
        ]
        self.face_inventory = [
            {
                "body_type": 0,
                "body_index": 0,
                "face_index": index,
                "triangle_start": index,
                "triangle_count": 1,
            }
            for index in range(4)
        ]
        self.triangles = [
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
            ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
        ]
        self.values = [value for triangle in self.triangles for point in triangle for value in point]
        self.tessellation = build_native_tessellation(
            self.component,
            self.body_inventory,
            self.face_inventory,
            source_scope=self.source_scope,
        )

    def test_raw_triangles_materialize_to_closed_stl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.triangles"
            stl = root / "mesh.stl"
            self.assertEqual(write_native_triangle_stream(raw, self.values), 4)
            warnings = []
            spans = parse_native_tessellation(
                self.component,
                self.tessellation,
                {"solid": 1, "sheet": 0},
                4,
                source_scope=self.source_scope,
                warnings=warnings,
            )
            self.assertEqual(materialize_native_stl(raw, stl, spans), 4)
            replay = list(iter_native_triangles(raw))
            self.assertEqual(replay, self.triangles)
            self.assertEqual(read_native_triangle_stream(raw, expected_count=4), self.triangles)
            topology = summarize_native_topology(spans, replay)
            self.assertEqual(topology["bodies"][0]["open_edges"], 0)
            self.assertEqual(topology["bodies"][0]["non_manifold_edges"], 0)
            self.assertEqual(len(warnings), 5)
            warning = native_triangle_stl_warning(
                self.component,
                raw_path="raw/triangles/raw.triangles",
                raw_sha="a" * 64,
                path="geometry/mesh.stl",
                sha="b" * 64,
                triangles=4,
            )
            self.assertEqual(warning["detail"]["precision_qualification"], "not_established")
            self.assertEqual(
                native_triangle_relative_path("a" * 64),
                f"raw/triangles/{'a' * 64}.triangles",
            )
            self.assertEqual(native_stl_relative_path("b" * 64), f"geometry/{'b' * 64}.stl")

    def test_face_spans_must_partition_the_raw_stream(self):
        tessellation = {
            **self.tessellation,
            "face_spans": [
                {**span, "triangle_start": index + 1} for index, span in enumerate(self.tessellation["face_spans"])
            ],
        }
        with self.assertRaisesRegex(PipelineError, "complete ordered partition"):
            parse_native_tessellation(
                self.component,
                tessellation,
                {"solid": 1, "sheet": 0},
                4,
                source_scope=self.source_scope,
                warnings=[],
            )

    def test_zero_cross_product_triangle_is_rejected(self):
        triangle = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (2.0, 0.0, 0.0),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.triangles"
            stl = root / "mesh.stl"
            write_native_triangle_stream(
                raw,
                [
                    value
                    for point in triangle
                    for value in point
                ],
            )
            with self.assertRaisesRegex(
                PipelineError,
                "native triangle is degenerate",
            ):
                materialize_native_stl(
                    raw,
                    stl,
                    [
                        {
                            "triangle_start": 0,
                            "triangle_count": 1,
                            "face_owner_id": 0,
                        }
                    ],
                )

    def test_small_nonzero_cross_product_uses_the_real_unit_normal(self):
        scale = 1.0e-100
        triangle = (
            (0.0, 0.0, 0.0),
            (scale, 0.0, 0.0),
            (0.0, scale, 0.0),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.triangles"
            stl = root / "mesh.stl"
            write_native_triangle_stream(
                raw,
                [
                    value
                    for point in triangle
                    for value in point
                ],
            )
            self.assertEqual(
                materialize_native_stl(
                    raw,
                    stl,
                    [
                        {
                            "triangle_start": 0,
                            "triangle_count": 1,
                            "face_owner_id": 0,
                        }
                    ],
                ),
                1,
            )
            normal = struct.unpack_from("<3f", stl.read_bytes(), 84)
            self.assertEqual(normal, (0.0, 0.0, 1.0))

    def test_occurrence_transform_is_converted_offline(self):
        raw = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.1, -0.2, 0.3, 1.0, 0.0, 0.0, 0.0]
        self.assertEqual(
            transform_from_solidworks(raw),
            (1.0, 0.0, 0.0, 0.1, 0.0, 1.0, 0.0, -0.2, 0.0, 0.0, 1.0, 0.3, 0.0, 0.0, 0.0, 1.0),
        )

    def test_native_transform_types_are_not_coerced_into_engineering_values(self):
        raw = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.1, -0.2, 0.3, 1.0, 0.0, 0.0, 0.0]
        for invalid in (True, "1.0", None, [1.0]):
            with self.subTest(invalid=invalid):
                changed = [invalid, *raw[1:]]
                with self.assertRaises(ValueError):
                    transform_from_solidworks(changed)


if __name__ == "__main__":
    unittest.main()
