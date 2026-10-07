"""v1 joints and frames carry no authored numbers; CAD datums are the authority.

The scene layer derives each joint origin/RPY from the raw parent and child
link frames (which come from named CAD coordinate systems) and reads named
frames straight from the same capture. Authored origin numbers are rejected.
"""

from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import ConfigError  # noqa: E402
from description_pipeline.sources.solidworks.scene import (  # noqa: E402
    _build_frames,
    _build_joints,
    identity_matrix,
)


def _rz(angle: float) -> tuple[tuple[float, float, float], ...]:
    cosine, sine = math.cos(angle), math.sin(angle)
    return ((cosine, -sine, 0.0), (sine, cosine, 0.0), (0.0, 0.0, 1.0))


def _array(rotation, translation) -> list[float]:
    return [
        rotation[0][0],
        rotation[0][1],
        rotation[0][2],
        translation[0],
        rotation[1][0],
        rotation[1][1],
        rotation[1][2],
        translation[1],
        rotation[2][0],
        rotation[2][1],
        rotation[2][2],
        translation[2],
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def _joint(**overrides) -> dict:
    joint = {
        "id": "j1",
        "name": "joint_1",
        "type": "revolute",
        "parent": "base_link",
        "child": "upper_link",
        "axis": [0.0, 0.0, 1.0],
        "axis_reference": "HipYaw.SLDPRT cylindrical face",
        "limits": {"lower": -1.0, "upper": 1.0, "effort": 5.0, "velocity": 2.0},
        "limit_evidence": {"file": "evidence/spec.txt", "sha256": "0" * 64, "anchor": "LIMIT-J1"},
    }
    joint.update(overrides)
    return joint


class DerivedJointFrameTests(unittest.TestCase):
    def test_origin_is_derived_from_identity_parent_frame(self):
        frames = {
            "base_link": (identity_matrix(), (0.0, 0.0, 0.0), "base_datum"),
            "upper_link": (_rz(math.pi / 2), (0.1, 0.2, 0.3), "tool_datum"),
        }
        entry = _build_joints({"joints": [_joint()]}, {"base_link", "upper_link"}, frames)[0]
        self.assertAlmostEqual(entry["xyz"][0], 0.1)
        self.assertAlmostEqual(entry["xyz"][1], 0.2)
        self.assertAlmostEqual(entry["xyz"][2], 0.3)
        self.assertAlmostEqual(entry["rpy"][2], math.pi / 2)
        self.assertEqual(entry["provenance"]["geometry"], "cad_body_frames")
        self.assertEqual(entry["provenance"]["child_frame"], "tool_datum")
        self.assertEqual(entry["provenance"]["axis_reference"], "HipYaw.SLDPRT cylindrical face")
        self.assertEqual(entry["provenance"]["limits_evidence"]["anchor"], "LIMIT-J1")

    def test_derivation_uses_the_parent_frame_as_reference(self):
        frames = {
            "base_link": (_rz(math.pi / 2), (1.0, 0.0, 0.0), "base_datum"),
            "upper_link": (identity_matrix(), (1.0, 1.0, 0.5), "tool_datum"),
        }
        entry = _build_joints({"joints": [_joint()]}, {"base_link", "upper_link"}, frames)[0]
        self.assertAlmostEqual(entry["xyz"][0], 1.0)
        self.assertAlmostEqual(entry["xyz"][1], 0.0)
        self.assertAlmostEqual(entry["xyz"][2], 0.5)
        self.assertAlmostEqual(entry["rpy"][2], -math.pi / 2)

    def test_authored_joint_origins_are_rejected(self):
        joint = _joint(xyz=[0.0, 0.0, 0.25], rpy=[0.0, 0.0, 0.0])
        with self.assertRaises(ConfigError):
            _build_joints({"joints": [joint]}, {"base_link", "upper_link"}, {})

    def test_joint_without_numbers_or_frames_is_rejected(self):
        with self.assertRaises(ConfigError):
            _build_joints({"joints": [_joint()]}, {"base_link", "upper_link"}, {})

    def test_incomplete_authored_origins_are_rejected(self):
        joint = _joint(xyz=[0.0, 0.0, 0.25])
        with self.assertRaises(ConfigError):
            _build_joints({"joints": [joint]}, {"base_link", "upper_link"}, {})


class NamedFrameTests(unittest.TestCase):
    def test_named_frame_is_expressed_relative_to_its_parent(self):
        raw_scene = SimpleNamespace(
            coordinate_systems={
                "tool_datum": _array(_rz(math.pi / 2), (0.1, 0.2, 0.3)),
            }
        )
        cfg = {
            "frames": [
                {
                    "id": "tool",
                    "name": "tool_frame",
                    "parent": "upper_link",
                    "coordinate_system": "tool_datum",
                }
            ]
        }
        link_frames = {"upper_link": (identity_matrix(), (0.1, 0.0, 0.0), "upper_datum")}
        entry = _build_frames(cfg, {"upper_link"}, raw_scene, link_frames)[0]
        # The datum is a world pose; URDF wants it in the parent link frame.
        self.assertAlmostEqual(entry["xyz"][0], 0.0)
        self.assertAlmostEqual(entry["xyz"][1], 0.2)
        self.assertAlmostEqual(entry["xyz"][2], 0.3)
        self.assertAlmostEqual(entry["rpy"][2], math.pi / 2)
        self.assertEqual(entry["provenance"]["geometry"], "cad_coordinate_system:tool_datum")

    def test_parent_rotation_and_translation_are_removed(self):
        raw_scene = SimpleNamespace(coordinate_systems={"tool_datum": _array(identity_matrix(), (1.0, 1.0, 0.5))})
        cfg = {
            "frames": [
                {
                    "id": "tool",
                    "name": "tool_frame",
                    "parent": "upper_link",
                    "coordinate_system": "tool_datum",
                }
            ]
        }
        link_frames = {"upper_link": (_rz(math.pi / 2), (1.0, 0.0, 0.0), "upper_datum")}
        entry = _build_frames(cfg, {"upper_link"}, raw_scene, link_frames)[0]
        self.assertAlmostEqual(entry["xyz"][0], 1.0)
        self.assertAlmostEqual(entry["xyz"][1], 0.0)
        self.assertAlmostEqual(entry["xyz"][2], 0.5)
        self.assertAlmostEqual(entry["rpy"][2], -math.pi / 2)

    def test_missing_captured_datum_is_an_error(self):
        raw_scene = SimpleNamespace(coordinate_systems={})
        cfg = {
            "frames": [
                {
                    "id": "tool",
                    "name": "tool_frame",
                    "parent": "upper_link",
                    "coordinate_system": "tool_datum",
                }
            ]
        }
        link_frames = {"upper_link": (identity_matrix(), (0.0, 0.0, 0.0), "upper_datum")}
        with self.assertRaises(ConfigError):
            _build_frames(cfg, {"upper_link"}, raw_scene, link_frames)


if __name__ == "__main__":
    unittest.main()
