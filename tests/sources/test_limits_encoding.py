"""Drive encoding, limit-source carry-through and the policy-exact preview window."""

from __future__ import annotations

import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

import numpy as np

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.interface_records import (  # noqa: E402
    preview_revolute_window_rad,
    preview_sampling_interval_fraction,
)
from description_pipeline.io import PipelineError  # noqa: E402
from description_pipeline.sources.solidworks.scene import _build_joints  # noqa: E402
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
    }
    value.update(overrides)
    return value


class DriveEncodingTests(unittest.TestCase):
    def _call(self, authored, canonical, limit: str):
        eye = np.eye(4)
        datums = {"base_link": eye, "tool": eye}
        diagnostics: list[dict] = []
        result = _joint(_node(limit), authored, canonical, datums, datums, diagnostics)
        return result, diagnostics

    def test_missing_drive_is_omitted_and_zero_drive_stays_present(self) -> None:
        authored = _authored()
        canonical = {**authored, "xyz": [0.0, 0.0, 0.0], "rpy": [0.0, 0.0, 0.0]}
        result, diagnostics = self._call(authored, canonical, '<limit lower="-1" upper="1"/>')
        self.assertEqual(result["limits"], {"lower": -1.0, "upper": 1.0})
        self.assertNotIn("effort", result["limits"])
        self.assertNotIn("velocity", result["limits"])
        self.assertEqual(diagnostics, [])

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

    def test_the_preview_window_equals_the_frozen_policy_radius(self) -> None:
        radius = preview_revolute_window_rad()
        authored = _authored(
            limits={"lower": -radius, "upper": radius},
            limit_source="policy_preview_window",
        )
        canonical = {**authored, "xyz": [0.0, 0.0, 0.0], "rpy": [0.0, 0.0, 0.0]}
        result, _diagnostics = self._call(
            authored, canonical,
            f'<limit lower="-{radius}" upper="{radius}"/>',
        )
        self.assertEqual(result["limits"], {"lower": -radius, "upper": radius})

        wrong = _authored(limits={"lower": -1.0, "upper": 1.0}, limit_source="policy_preview_window")
        with self.assertRaisesRegex(PipelineError, "policy radius"):
            self._call(
                wrong, {**wrong, "xyz": [0.0, 0.0, 0.0], "rpy": [0.0, 0.0, 0.0]},
                '<limit lower="-1" upper="1"/>',
            )

        dropped = dict(canonical)
        dropped.pop("limit_source")
        with self.assertRaisesRegex(PipelineError, "limit source changed"):
            self._call(authored, dropped, f'<limit lower="-{radius}" upper="{radius}"/>')

    def test_sampling_reads_the_policy_fraction(self) -> None:
        joint = {"name": "j", "type": "revolute", "limits": {"lower": -2.0, "upper": 2.0}}
        self.assertEqual(preview_sampling_interval_fraction(), 1.0)
        self.assertEqual(_joint_sample(joint, 1.0), 1.0)
        self.assertEqual(_joint_combined_sample(joint, 1.0), 1.0)
        with patch(
            "description_pipeline.verification.solidworks_urdf.preview_sampling_interval_fraction",
            return_value=0.5,
        ):
            self.assertEqual(_joint_sample(joint, 1.0), 0.5)
            self.assertEqual(_joint_combined_sample(joint, 1.0), 0.5)

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
                cfg = {"joints": [{**base, "limits": limits, "limit_source": "policy_preview_window"}]}
                entry = _build_joints(cfg, {"base_link", "tool"}, frames)[0]
                self.assertEqual(entry["limits"], limits)
                self.assertEqual(entry["limit_source"], "policy_preview_window")


if __name__ == "__main__":
    unittest.main()
