"""Drive encoding, native limit-source carry-through and deterministic sampling."""

from __future__ import annotations

import unittest
import copy
from xml.etree import ElementTree as ET

import numpy as np

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.scene import _build_joints  # noqa: E402
from description_pipeline.sources.solidworks.scene import _value_warnings  # noqa: E402
from description_pipeline.io import PipelineError  # noqa: E402
from description_pipeline.verification.solidworks_urdf import (  # noqa: E402
    _joint,
    _joint_combined_sample,
    _joint_sample,
)


def _node(limit: str) -> ET.Element:
    return ET.fromstring(
        '<joint name="slide" type="revolute"><parent link="base_link"/><child link="tool"/>'
        '<origin xyz="0 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>' + limit + "</joint>"
    )


def _authored(**overrides) -> dict:
    value = {
        "name": "slide",
        "type": "revolute",
        "parent": "base_link",
        "child": "tool",
        "axis": [0.0, 0.0, 1.0],
        "limits": {"lower": -1.0, "upper": 1.0},
        "limit_source": "native",
    }
    value.update(overrides)
    return value


class DriveEncodingTests(unittest.TestCase):
    def _build(self, authored):
        eye = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        frames = {name: (eye, (0.0, 0.0, 0.0), name) for name in ("base_link", "tool")}
        return _build_joints({"joints": [authored]}, set(frames), frames)[0]

    def _call(self, authored, canonical, limit: str):
        eye = np.eye(4)
        datums = {"base_link": eye, "tool": eye}
        diagnostics: list[dict] = []
        result = _joint(_node(limit), authored, canonical, datums, datums, diagnostics)
        return result, diagnostics

    def test_missing_drive_uses_zero_defaults_and_authored_zero_stays_distinct(self) -> None:
        authored = _authored()
        original = copy.deepcopy(authored)
        canonical = self._build(authored)
        result, diagnostics = self._call(
            authored, canonical, '<limit lower="-1" upper="1" effort="0" velocity="0"/>'
        )
        self.assertEqual(result["limits"], {"lower": -1.0, "upper": 1.0, "effort": 0.0, "velocity": 0.0})
        self.assertEqual(authored, original)
        self.assertEqual(canonical["provenance"]["drive_defaults"]["fields"], ["effort", "velocity"])
        self.assertFalse(canonical["provenance"]["drive_defaults"]["drive_qualified"])
        warnings = _value_warnings({"joints": [authored]}, [], [], [canonical], [])
        default = next(row for row in warnings if row["code"] == "drive.default_zero")
        self.assertEqual({row["field"] for row in default["objects"]}, {"effort", "velocity"})
        self.assertTrue(all(row["actual"] == 0 and row["original"] is None for row in default["objects"]))
        self.assertTrue(all(row["rule_id"] == "drive.missing_zero" for row in default["objects"]))
        self.assertEqual(diagnostics[0]["code"], "urdf.motion_unqualified")

        zeroed = _authored(limits={"lower": -1.0, "upper": 1.0, "effort": 0.0, "velocity": 0.0})
        result, diagnostics = self._call(
            zeroed, {**zeroed, "xyz": [0.0, 0.0, 0.0], "rpy": [0.0, 0.0, 0.0]},
            '<limit lower="-1" upper="1" effort="0" velocity="0"/>',
        )
        self.assertEqual(result["limits"], {"lower": -1.0, "upper": 1.0, "effort": 0.0, "velocity": 0.0})
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["code"], "urdf.motion_unqualified")
        self.assertFalse(diagnostics[0]["blocking"])
        self.assertEqual(
            set(diagnostics[0]["detail"]["values"]),
            {"effort", "velocity"},
        )

    def test_valid_drive_values_are_preserved_and_only_missing_fields_default(self) -> None:
        authored = _authored(limits={"lower": -1.0, "upper": 1.0, "effort": 7.5})
        canonical = self._build(authored)
        self.assertEqual(canonical["limits"], {"lower": -1.0, "upper": 1.0, "effort": 7.5, "velocity": 0.0})
        self.assertEqual(canonical["provenance"]["drive_defaults"]["fields"], ["velocity"])
        self._call(authored, canonical, '<limit lower="-1" upper="1" effort="7.5" velocity="0"/>')
        for limits, provenance in (
            ('<limit lower="-1" upper="1" effort="0" velocity="0"/>', canonical["provenance"]),
            ('<limit lower="-1" upper="1" effort="7.5" velocity="2"/>', canonical["provenance"]),
            ('<limit lower="-1" upper="1" effort="7.5" velocity="0"/>', {}),
        ):
            with self.subTest(limits=limits, provenance=provenance), self.assertRaises(PipelineError):
                self._call(authored, {**canonical, "provenance": provenance}, limits)

    def test_sampling_uses_the_complete_native_range(self) -> None:
        joint = {"name": "j", "type": "revolute", "limits": {"lower": -2.0, "upper": 2.0}}
        self.assertEqual(_joint_sample(joint, 1.0), 1.0)
        self.assertEqual(_joint_combined_sample(joint, 1.0), 1.0)

    def test_continuous_missing_drive_defaults_without_position_bounds_and_fixed_stays_empty(self) -> None:
        for limits in ({}, None):
            with self.subTest(limits=limits):
                authored = _authored(type="continuous", limits=limits)
                canonical = self._build(authored)
                self.assertEqual(canonical["limits"], {"effort": 0.0, "velocity": 0.0})
                node = _node('<limit effort="0" velocity="0"/>')
                node.set("type", "continuous")
                eye = {name: np.eye(4) for name in ("base_link", "tool")}
                _joint(node, authored, canonical, eye, eye, [])
        fixed = self._build(_authored(type="fixed", limits=None))
        self.assertNotIn("limits", fixed)
        self.assertNotIn("drive_defaults", fixed["provenance"])

    def test_scene_carries_the_limit_source_and_exact_drive_keys(self) -> None:
        identity = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        frames = {"base_link": (identity, (0.0, 0.0, 0.0), "CS_base"), "tool": (identity, (0.0, 0.0, 0.0), "CS_tool")}
        base = {"id": "slide", "name": "slide", "type": "revolute", "parent": "base_link", "child": "tool",
                "axis": [0.0, 0.0, 1.0]}
        for limits in (
            {"lower": -1.0, "upper": 1.0},
            {"lower": -1.0, "upper": 1.0, "effort": 0.0, "velocity": 0.0},
        ):
            with self.subTest(limits=limits):
                cfg = {"joints": [{**base, "limits": limits, "limit_source": "native"}]}
                entry = _build_joints(cfg, {"base_link", "tool"}, frames)[0]
                self.assertEqual(entry["limits"], {"effort": 0.0, "velocity": 0.0, **limits})
                self.assertEqual(entry["limit_source"], "native")


if __name__ == "__main__":
    unittest.main()
