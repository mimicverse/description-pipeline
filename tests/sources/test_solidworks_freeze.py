"""Windows Capture seals raw facts and performs no semantic projection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import read_data
from description_pipeline.schemas import (
    NATIVE_CAPTURE_OBSERVATION_KEYS,
    NATIVE_CAPTURE_OBSERVATION_SCHEMA,
)

from . import support


class RawCaptureFreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="solidworks-raw-freeze-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assembly = support.make_cad_tree(self.root / "cad")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement(),
                    "mass": support.mass_payload(
                        1.0,
                        (0.01, 0.02, 0.0),
                    ),
                },
                {
                    "name": "arm-1",
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(0.5),
                },
            ],
            dependencies=[
                self.root / "cad/base.SLDPRT",
                self.root / "cad/arm.SLDPRT",
            ],
            coordinate_systems={
                "CS_base_link": support.placement(),
                "CS_arm_link": support.placement((0.0, 0.0, 0.2)),
            },
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "geometry": {"enabled": True, "format": "stl_binary"},
            "coordinate_systems": ["CS_base_link", "CS_arm_link"],
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": ["base-1"],
                    "frame": {"coordinate_system": "CS_base_link"},
                },
                {
                    "id": "arm",
                    "name": "arm_link",
                    "components": ["arm-1"],
                    "frame": {"coordinate_system": "CS_arm_link"},
                },
            ],
            "joints": [],
        }
        self.destination = self.root / "capture"

    def capture(self) -> dict:
        return support.capture_prepared_fixture(
            self.config,
            self.destination,
            backend=self.backend,
        )

    def test_capture_seals_only_raw_native_members(self) -> None:
        manifest = self.capture()
        self.assertEqual(
            manifest["scene"],
            "raw/native_capture_facts.json",
        )
        self.assertEqual(
            set(manifest["files"]),
            {
                "native-runtime.json",
                "raw/dependency_closure.json",
                "raw/native_capture_facts.json",
                "raw/triangles/f56f6426f638a44dc1da3320e7d1c79c149af69c01ae6e0f147733f8a5dbeff5.triangles",
            },
        )
        for retired in (
            "capture.json",
            "geometry",
            "raw/scene_raw.json",
            "raw/mass_properties.json",
            "raw/coordinate_systems.json",
            "raw/link_group_mass_properties.json",
            "raw/material_provenance.json",
            "raw/effective_instances.json",
            "raw/mass_closure.json",
            "raw/geometry.json",
            "raw/axis_references.json",
            "raw/source_scopes.json",
            "raw/inertia_convention.json",
        ):
            self.assertFalse((self.destination / retired).exists(), retired)

        facts = read_data(
            self.destination / "raw/native_capture_facts.json"
        )
        self.assertEqual(
            set(facts),
            set(NATIVE_CAPTURE_OBSERVATION_KEYS),
        )
        self.assertEqual(
            facts["schema_version"],
            NATIVE_CAPTURE_OBSERVATION_SCHEMA,
        )
        self.assertEqual(
            set(facts["source_scopes"]),
            {"base-1", "arm-1"},
        )
        self.assertEqual(
            set(facts["geometry_raw"]),
            {"base-1", "arm-1"},
        )

    def test_cad_is_released_after_the_native_reads(self) -> None:
        self.capture()
        self.assertEqual(self.backend.order[-1], "release")
        self.assertLess(
            self.backend.order.index("collect_prepared_scene"),
            self.backend.order.index("capture_prepared_geometry"),
        )
        self.assertLess(
            self.backend.order.index("capture_prepared_geometry"),
            self.backend.order.index("release"),
        )

    def test_geometry_identity_mismatch_fails_after_release(self) -> None:
        capture_geometry = self.backend.capture_prepared_geometry

        def mismatched(source_scopes, on_activity=None):
            rows = capture_geometry(
                source_scopes,
                on_activity=on_activity,
            )
            rows.pop("base-1")
            return rows

        self.backend.capture_prepared_geometry = mismatched
        with self.assertRaises(Exception) as caught:
            self.capture()
        self.assertIn("geometry", str(caught.exception).lower())
        self.assertIn("release", self.backend.order)


if __name__ == "__main__":
    unittest.main()
