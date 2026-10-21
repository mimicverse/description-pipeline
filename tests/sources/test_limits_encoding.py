"""Drive encoding, native limit-source carry-through and deterministic sampling."""

from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

import numpy as np

from . import _paths  # noqa: F401  (import side effect: sys.path)

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
        "limit_source": "native",
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

    def test_sampling_uses_the_complete_native_range(self) -> None:
        joint = {"name": "j", "type": "revolute", "limits": {"lower": -2.0, "upper": 2.0}}
        self.assertEqual(_joint_sample(joint, 1.0), 1.0)
        self.assertEqual(_joint_combined_sample(joint, 1.0), 1.0)

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
                self.assertEqual(entry["limits"], limits)
                self.assertEqual(entry["limit_source"], "native")


if __name__ == "__main__":
    unittest.main()
