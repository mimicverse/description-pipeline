"""Prepared capture facts, release ordering, and offline triangle materialization."""

from __future__ import annotations

import copy
import struct
import tempfile
import unittest
from pathlib import Path

from description_pipeline.geometry.native_tessellation import (
    parse_native_tessellation,
    read_native_triangle_stream,
)
from description_pipeline.geometry.native_transform import transform_from_solidworks
from description_pipeline.io import PipelineError
from description_pipeline.schemas import NATIVE_TESSELLATION_SCHEMA
from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.sources.solidworks.native import (
    materialize_native_triangles,
    project_axis_references,
    project_prepared_capture,
)
from description_pipeline.verification.native_mesh_observations import (
    _verify_scene_binding,
    _verify_stl,
)
from tests.sources import support


class PreparedCaptureFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="prepared-capture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "prepared"
        assembly = support.make_cad_tree(self.root)
        parts = [self.root / "base.SLDPRT", self.root / "arm.SLDPRT"]
        self.backend = support.FixtureCadBackend(
            assembly,
            [
                {
                    "name": "base-1",
                    "path": str(parts[0]),
                    "transform": support.placement(),
                    "mass": support.mass_payload(1.0, (0.0, 0.0, 0.0)),
                },
                {
                    "name": "arm-1",
                    "path": str(parts[1]),
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(0.5, (0.0, 0.0, 0.05)),
                },
            ],
            dependencies=parts,
        )
        self.document = str(assembly)
        self.backend.prepare_prepared_capture(
            str(self.root),
            self.document,
            "Default",
        )

    def test_seven_facts_project_six_keys_before_offline_stl(self):
        raw_facts = self.backend.collect_prepared_scene(
            self.document,
            [],
            groups=[
                {"name": "base_link", "components": ["base-1"]},
                {"name": "arm_link", "components": ["arm-1"]},
            ],
        )
        self.assertEqual(
            set(raw_facts),
            {
                "scene_raw",
                "part_mass_raw",
                "assembly_mass_raw",
                "component_mass_raw",
                "link_group_mass_raw",
                "source_scopes",
                "notes",
            },
        )
        raw_geometry = self.backend.capture_prepared_geometry(
            raw_facts["source_scopes"]
        )
        self.backend.release()
        projected = project_prepared_capture(copy.deepcopy(raw_facts))
        self.assertEqual(
            set(projected),
            {
                "scene",
                "assembly_mass",
                "component_mass",
                "effective_instances",
                "effective_instances_error",
                "link_group_mass_properties",
            },
        )
        destinations = {
            component: str(self.root / "geometry" / f"{index:04d}.stl")
            for index, component in enumerate(sorted(raw_geometry), start=1)
        }
        serialized = materialize_native_triangles(raw_geometry, destinations)
        self.assertEqual(set(serialized), set(raw_geometry))
        for component, entry in serialized.items():
            self.assertEqual(
                set(entry),
                {
                    "component",
                    "raw_path",
                    "raw_sha",
                    "path",
                    "sha",
                    "triangles",
                    "bodies",
                    "used_api",
                    "tessellation",
                    "transform_raw",
                },
            )
            self.assertEqual(entry["component"], component)
            self.assertEqual(entry["tessellation"]["schema_version"], NATIVE_TESSELLATION_SCHEMA)
            self.assertGreater(entry["triangles"], 0)
        order = self.backend.order
        self.assertLess(order.index("collect_prepared_scene"), order.index("capture_prepared_geometry"))
        self.assertLess(order.index("capture_prepared_geometry"), order.index("release"))

    def test_missing_source_scope_and_malformed_triangles_fail_closed(self):
        raw_facts = self.backend.collect_prepared_scene(self.document, [], groups=[])
        source_scopes = copy.deepcopy(raw_facts["source_scopes"])
        source_scopes.pop(next(iter(source_scopes)))
        with self.assertRaises(ValueError):
            self.backend.capture_prepared_geometry(source_scopes)

        raw_geometry = self.backend.capture_prepared_geometry(
            raw_facts["source_scopes"]
        )
        component = next(iter(raw_geometry))
        raw_geometry[component]["triangle_values"] = raw_geometry[component][
            "triangle_values"
        ][:-1]
        destinations = {
            component: str(self.root / "geometry" / f"{component}.stl")
            for component in raw_geometry
        }
        with self.assertRaises(CadError) as caught:
            materialize_native_triangles(raw_geometry, destinations)
        self.assertEqual(caught.exception.code, "cad_mesh_export_failed")

    def test_fixture_axis_capture_is_raw_until_offline_projection(self):
        cylinder_raw = self.backend.capture_axis_reference(
            {
                "kind": "cylinder",
                "component": "arm-1",
                "body_type": "solid",
                "face_index": 0,
            }
        )
        self.assertNotIn("axis_point_m", cylinder_raw)
        self.assertNotIn("axis_direction", cylinder_raw)
        self.assertNotIn("radius_m", cylinder_raw)
        self.assertEqual(
            cylinder_raw["face_candidates"][0]["cylinder_params_raw"],
            [0.0, 0.0, 0.1, 0.0, 0.0, 1.0, 0.006],
        )
        cylinder = project_axis_references(
            [{"joint": "hinge", **cylinder_raw}]
        )[0]
        self.assertEqual(cylinder["cylinder_params_raw"][2:], [0.1, 0.0, 0.0, 1.0, 0.006])
        self.assertIs(cylinder["is_cylinder_raw"], True)
        self.assertEqual(cylinder["axis_point_m"], [0.0, 0.0, 0.1])
        self.assertEqual(cylinder["axis_direction"], [0.0, 0.0, 1.0])

        reference_raw = self.backend.capture_axis_reference(
            {
                "kind": "reference_axis",
                "component": "",
                "feature_name": "hinge_axis",
            }
        )
        self.assertEqual(
            reference_raw["endpoints_raw"],
            [0.0, 0.0, 0.1, 0.0, 0.0, 1.1],
        )
        self.assertNotIn("raw_endpoints_m", reference_raw)
        reference = project_axis_references(
            [{"joint": "hinge", **reference_raw}]
        )[0]
        self.assertEqual(reference["raw_endpoints_m"], reference_raw["endpoints_raw"])
        self.assertEqual(reference["axis_direction"], [0.0, 0.0, 1.0])

    def _mesh_consumer_artifacts(self, name: str):
        evidence = self.root.parent / name
        (evidence / "raw").mkdir(parents=True)
        raw_facts = self.backend.collect_prepared_scene(
            self.document,
            [],
            groups=[
                {"name": "base_link", "components": ["base-1"]},
                {"name": "arm_link", "components": ["arm-1"]},
            ],
        )
        raw_geometry = self.backend.capture_prepared_geometry(
            raw_facts["source_scopes"]
        )
        self.backend.release()
        destinations = {
            component: str(evidence / "geometry" / f"{component}.stl")
            for component in raw_geometry
        }
        geometry = list(
            materialize_native_triangles(raw_geometry, destinations).values()
        )
        entry = geometry[0]
        component = entry["component"]
        source_scope = raw_geometry[component]["source_scope"]
        spans = parse_native_tessellation(
            component,
            entry["tessellation"],
            entry["bodies"],
            entry["triangles"],
            source_scope=source_scope,
            warnings=[],
        )
        occurrence = {
            "name": component,
            "path": source_scope["source_document_path"],
            "transform": list(
                transform_from_solidworks(entry["transform_raw"])
            ),
            "is_fixed": False,
            "document_type": "part",
        }
        return evidence, entry, source_scope, spans, occurrence

    def test_independent_mesh_consumer_rejects_raw_stl_and_transform_tampering(
        self,
    ):
        evidence, entry, _source_scope, spans, _occurrence = (
            self._mesh_consumer_artifacts("raw-tamper")
        )
        old_raw = evidence / entry["raw_path"]
        raw_bytes = bytearray(old_raw.read_bytes())
        first_coordinate = struct.unpack_from("<d", raw_bytes, 24)[0]
        struct.pack_into("<d", raw_bytes, 24, first_coordinate + 1.0)
        old_raw.write_bytes(raw_bytes)
        raw_triangles = read_native_triangle_stream(
            old_raw,
            expected_count=entry["triangles"],
        )
        with self.assertRaisesRegex(
            PipelineError,
            "STL record .* does not bind its raw triangle",
        ):
            _verify_stl(
                entry["component"],
                evidence / entry["path"],
                old_raw,
                raw_triangles,
                entry["triangles"],
                spans,
            )

        evidence, entry, _source_scope, spans, _occurrence = (
            self._mesh_consumer_artifacts("stl-tamper")
        )
        raw_path = evidence / entry["raw_path"]
        old_stl = evidence / entry["path"]
        stl_bytes = bytearray(old_stl.read_bytes())
        stl_bytes[-1] ^= 1
        old_stl.write_bytes(stl_bytes)
        raw_triangles = read_native_triangle_stream(
            raw_path,
            expected_count=entry["triangles"],
        )
        with self.assertRaisesRegex(
            PipelineError,
            "STL record .* does not bind its raw triangle",
        ):
            _verify_stl(
                entry["component"],
                old_stl,
                raw_path,
                raw_triangles,
                entry["triangles"],
                spans,
            )

        _evidence, entry, source_scope, _spans, occurrence = (
            self._mesh_consumer_artifacts("transform-tamper")
        )
        occurrence["transform"][3] += 0.01
        with self.assertRaisesRegex(
            PipelineError,
            "offline occurrence transform differs",
        ):
            _verify_scene_binding(
                entry["component"],
                occurrence,
                entry["transform_raw"],
                source_scope,
            )


if __name__ == "__main__":
    unittest.main()
