"""The normalization oracle must re-derive the numbers and catch injected errors."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.freeze import freeze as _freeze  # noqa: E402
from description_pipeline.sources.solidworks.freeze import validate_source_config as _validate_source_config  # noqa: E402

from . import support  # noqa: E402
from .test_solidworks_freeze import _Backend, _compose_scene_inputs  # noqa: E402


def freeze(config, *args, **kwargs):  # noqa: F811 - fixture adapter for the request sets
    return _freeze(support.capture_requests(config), *args, **kwargs)


def validate_source_config(config):  # noqa: F811 - fixture adapter for the request sets
    return _validate_source_config(support.capture_requests(config))


class NormalizationOracleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-oracle-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = _Backend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement(),
                    # Off-axis COM: the product signal proves the convention at capture time.
                    "mass": support.mass_payload(1.0, (0.01, 0.02, 0.0)),
                },
                {
                    "name": "arm-1",
                    "transform": support.placement((0.0, 0.0, 0.2), (0.0, 0.0, 0.0)),
                    "mass": support.mass_payload(
                        0.5, (0.0, 0.0, 0.05), [[0.002, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]]
                    ),
                },
            ],
            dependencies=[self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"],
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": True},
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
            "joints": [
                {
                    "id": "hinge",
                    "name": "hinge_joint",
                    "type": "revolute",
                    "parent": "base_link",
                    "child": "arm_link",
                    "axis": [0.0, 0.0, 1.0],
                    "limits": {"lower": -1.0, "upper": 1.0, "effort": 2.0, "velocity": 3.0},
                }
            ],
        }
        self.snapshot = self.tmp / "snapshot"
        freeze(self.config, self.snapshot, backend=self.backend)
        _compose_scene_inputs(self.snapshot, self.tmp / "cad", with_arm=True, with_joint=True)
        self.raw_scene = support.read_scene(self.snapshot)

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def _definition(self, **source_overrides):
        source = dict(self.config)
        source.update(source_overrides)
        return {
            "schema_version": "description.definition/v1",
            "hardware_id": "fixture",
            "source": source,
            "overrides": [],
        }

    def test_strict_source_validation(self) -> None:
        from description_pipeline.sources.solidworks.errors import ConfigError

        for overrides in (
            {"material_source": "documented_table"},
            {"documented_masses": {"arm-1": 1.5}},
            {"mass_evidence": {"reference": "manual", "file": "table.json", "sha256": "0" * 64}},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ConfigError) as raised:
                validate_source_config(dict(self.config, **overrides))
            self.assertEqual(raised.exception.code, "invalid_config")

        bad_key = dict(self.config, assembly_typo=str(self.assembly))
        with self.assertRaises(ConfigError):
            validate_source_config(bad_key)


