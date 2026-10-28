"""Raw geometry sealing and Linux-side STL projection regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, write_json
from description_pipeline.schemas import NATIVE_CAPTURE_OBSERVATION_SCHEMA
from description_pipeline.sources.solidworks.errors import CadError
from description_pipeline.sources.solidworks.capture_geometry import (
    materialize_native_geometry,
    seal_native_geometry,
)
from description_pipeline.sources.solidworks.capture_projection import (
    build_native_capture_observation,
    load_native_capture_observation,
    project_native_capture_observation,
    validate_native_capture_plan,
)
from tests.sources import support


class CaptureGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="capture-geometry-")
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
                    "mass": support.mass_payload(
                        1.0,
                        (0.01, 0.02, 0.0),
                    ),
                },
                {
                    "name": "arm-1",
                    "path": str(parts[1]),
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(
                        0.5,
                        (0.0, 0.0, 0.05),
                    ),
                },
            ],
            dependencies=parts,
        )
        document = str(assembly)
        self.backend.prepare_prepared_capture(
            str(self.root),
            document,
            "Default",
        )
        self.raw_facts = self.backend.collect_prepared_scene(
            document,
            [],
            groups=[
                {"name": "base_link", "components": ["base-1"]},
                {"name": "arm_link", "components": ["arm-1"]},
            ],
        )
        self.raw_geometry = self.backend.capture_prepared_geometry(
            self.raw_facts["source_scopes"]
        )
        for component, facts in self.raw_geometry.items():
            scope = self.raw_facts["source_scopes"][component]
            facts["appearance_raw"] = {
                "component": component,
                "source_document_path": scope["source_document_path"],
                "referenced_configuration": scope[
                    "referenced_configuration"
                ],
                "fixture": True,
            }
        self.raw_axis = self.backend.capture_axis_reference(
            {
                "kind": "cylinder",
                "component": "arm-1",
                "body_type": "solid",
                "face_index": 0,
            }
        )
        self.backend.release()

    def test_raw_sealing_has_no_stl_and_linux_materialization_matches_legacy(
        self,
    ) -> None:
        evidence_root = Path(self.root) / "sealed/evidence"
        geometry_raw = seal_native_geometry(
            self.raw_geometry,
            self.raw_facts["source_scopes"],
            evidence_root,
        )
        self.assertFalse((evidence_root / "geometry").exists())
        self.assertTrue((evidence_root / "raw/triangles").is_dir())
        for row in geometry_raw.values():
            self.assertNotIn("triangle_values", row)
            self.assertNotIn("source_scope", row)
            self.assertNotIn(
                "source_document_path",
                row["appearance_raw"],
            )
            self.assertNotIn(
                "referenced_configuration",
                row["appearance_raw"],
            )
            raw_path = evidence_root / row["stream_path"]
            self.assertTrue(raw_path.is_file())

        actual = materialize_native_geometry(
            geometry_raw,
            self.raw_facts["source_scopes"],
            evidence_root=evidence_root,
            destination_root=Path(self.root) / "linux",
        )
        self.assertEqual(set(actual), set(geometry_raw))
        for component, row in actual.items():
            self.assertEqual(row["component"], component)
            self.assertGreater(row["triangles"], 0)
            self.assertTrue(
                (Path(self.root) / "linux" / row["path"]).is_file()
            )

    def test_linux_materialization_refuses_a_tampered_stream(self) -> None:
        evidence_root = Path(self.root) / "sealed/evidence"
        geometry_raw = seal_native_geometry(
            self.raw_geometry,
            self.raw_facts["source_scopes"],
            evidence_root,
        )
        component = sorted(geometry_raw)[0]
        raw_path = evidence_root / geometry_raw[component]["stream_path"]
        raw_path.write_bytes(raw_path.read_bytes() + b"tamper")

        with self.assertRaisesRegex(
            CadError,
            "raw triangle stream digest differs",
        ):
            materialize_native_geometry(
                geometry_raw,
                self.raw_facts["source_scopes"],
                evidence_root=evidence_root,
                destination_root=Path(self.root) / "linux",
            )

    def test_raw_observation_reader_and_projection_use_one_record(self) -> None:
        evidence_root = Path(self.root) / "sealed/evidence"
        geometry_raw = seal_native_geometry(
            self.raw_geometry,
            self.raw_facts["source_scopes"],
            evidence_root,
        )
        observation = build_native_capture_observation(
            facts=self.raw_facts,
            geometry_raw=geometry_raw,
            axis_references_raw=[{"joint": "hinge", **self.raw_axis}],
            document_state={
                "before": {"read_only": True},
                "after": {"read_only": True},
            },
            input_stability={
                "unchanged": True,
                "files_checked": 3,
            },
        )
        self.assertEqual(
            observation["schema_version"],
            NATIVE_CAPTURE_OBSERVATION_SCHEMA,
        )
        write_json(
            evidence_root / "raw/native_capture_facts.json",
            observation,
        )

        loaded = load_native_capture_observation(evidence_root)
        projected = project_native_capture_observation(
            loaded,
            {
                "configuration": "Default",
                "groups": [
                    {
                        "name": "base_link",
                        "components": ["base-1"],
                    },
                    {
                        "name": "arm_link",
                        "components": ["arm-1"],
                    },
                ],
            },
        )
        plan = {
            "objects": [
                {"name": "base-1"},
                {"name": "arm-1"},
            ],
            "groups": [
                {"name": "base_link", "components": ["base-1"]},
                {"name": "arm_link", "components": ["arm-1"]},
            ],
            "axis_requests": [
                {
                    "name": "hinge",
                    "axis_reference": {
                        "component": "arm-1",
                        "body_type": "solid",
                        "face_index": 0,
                    },
                }
            ],
            "datums": [],
        }
        validate_native_capture_plan(loaded, plan)
        with self.assertRaisesRegex(
            PipelineError,
            "axis inventory differs",
        ):
            validate_native_capture_plan(
                loaded,
                {**plan, "axis_requests": []},
            )

        self.assertEqual(loaded, observation)
        self.assertEqual(
            {component.name for component in projected["scene"].components},
            {"base-1", "arm-1"},
        )
        self.assertEqual(projected["axis_references"][0]["joint"], "hinge")
        component = sorted(geometry_raw)[0]
        stream = evidence_root / geometry_raw[component]["stream_path"]
        stream.write_bytes(stream.read_bytes() + b"tamper")
        with self.assertRaisesRegex(
            PipelineError,
            "triangle stream differs",
        ):
            load_native_capture_observation(evidence_root)

if __name__ == "__main__":
    unittest.main()
