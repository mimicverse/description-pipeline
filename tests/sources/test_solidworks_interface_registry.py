"""Controlled interface-registry validation for the exporter carrier."""

from __future__ import annotations

import unittest

from description_pipeline.sources.solidworks.discovery import (
    DiscoverySettings,
    _apply_exporter_names,
    _Clusters,
)
from description_pipeline.sources.solidworks.errors import ConfigError
from description_pipeline.sources.solidworks.scene import _validate_scene_interface_names


def facts():
    bodies = [
        {"name": "base_link", "components": ["base-1"], "owners": ["base-1"]},
        {"name": "arm", "components": ["arm-1"], "owners": ["arm-1"]},
    ]
    joints = [
        {
            "name": "hinge",
            "parent": "base_link",
            "child": "arm",
            "declared_axis": [0.0, 0.0, 1.0],
            "limits": {"lower": -0.2, "upper": 1.3, "effort": 1.0, "velocity": 2.0},
        }
    ]
    clusters = _Clusters(
        members={"base_link": ["base-1"], "arm": ["arm-1"]},
        of={"base-1": "base_link", "arm-1": "arm"},
        pairs={},
    )
    return bodies, joints, clusters


class InterfaceRegistryTests(unittest.TestCase):
    def test_link_joint_and_sign_mapping_are_applied_together(self):
        bodies, joints, clusters = facts()
        findings = []
        _apply_exporter_names(
            bodies,
            joints,
            clusters,
            DiscoverySettings(
                frozen_names={
                    "exporter:arm": "arm_link",
                    "joint:hinge": "shoulder_pivot_joint",
                    "axis_sign:hinge": -1,
                }
            ),
            findings,
        )
        self.assertEqual(findings, [])
        self.assertEqual((bodies[1]["source_name"], bodies[1]["name"]), ("arm", "arm_link"))
        self.assertEqual((joints[0]["source_name"], joints[0]["name"]), ("hinge", "shoulder_pivot_joint"))
        self.assertEqual(joints[0]["declared_axis"], [0.0, 0.0, -1.0])
        self.assertEqual(joints[0]["limits"]["lower"], -1.3)
        self.assertEqual(joints[0]["limits"]["upper"], 0.2)
        self.assertEqual(clusters.members["arm_link"], ["arm-1"])
        self.assertEqual(clusters.of["arm-1"], "arm_link")

    def test_unknown_and_duplicate_targets_block(self):
        cases = (
            {"exporter:missing": "other_link"},
            {"joint:missing": "other_joint"},
            {"exporter:arm": "base_link"},
            {"exporter:arm": "Bad-Name"},
        )
        for registry in cases:
            with self.subTest(registry=registry):
                bodies, joints, clusters = facts()
                findings = []
                _apply_exporter_names(
                    bodies,
                    joints,
                    clusters,
                    DiscoverySettings(frozen_names=registry),
                    findings,
                )
                self.assertIn("discovery.interface_registry_invalid", [item["code"] for item in findings])

    def test_axis_sign_must_be_the_integer_minus_one_or_one(self):
        for value in (0, 2, True, -1.0, "1"):
            with self.subTest(value=value):
                bodies, joints, clusters = facts()
                findings = []
                _apply_exporter_names(
                    bodies,
                    joints,
                    clusters,
                    DiscoverySettings(frozen_names={"axis_sign:hinge": value}),
                    findings,
                )
                self.assertIn("discovery.interface_registry_invalid", [item["code"] for item in findings])

    def test_axis_sign_cannot_name_an_unknown_joint(self):
        bodies, joints, clusters = facts()
        findings = []
        _apply_exporter_names(
            bodies,
            joints,
            clusters,
            DiscoverySettings(frozen_names={"axis_sign:missing": -1}),
            findings,
        )
        self.assertIn("discovery.interface_registry_invalid", [item["code"] for item in findings])


class SceneInterfaceRegistryTests(unittest.TestCase):
    def test_scene_mapping_rejects_unknown_partial_and_colliding_entries(self):
        entries = {"part-1": {"sha256": "a" * 64}, "part-2": {"sha256": "b" * 64}}
        invalid = (
            {"visual:missing": "native_2"},
            {"visual:part-1": "native_0"},
            {
                "visual:part-1": "native_0",
                "visual:part-2": "native_0",
                "material:part-1": "cad_gray",
                "material:part-2": "cad_gray",
            },
        )
        for names in invalid:
            with self.subTest(names=names), self.assertRaises(ConfigError):
                _validate_scene_interface_names(names, entries)

    def test_mesh_alias_must_remain_a_relative_stl_under_delivery_meshes(self):
        alias_a = "a" * 64
        alias_b = "b" * 64
        base = {
            "visual:part-1": "native_0",
            "visual:part-2": "native_1",
            "material:part-1": "cad_gray",
            "material:part-2": "cad_gray",
            "mesh:part-1": f"../meshes/visual/reference/native_{alias_a}.stl",
            "mesh:part-2": f"../meshes/visual/reference/native_{alias_b}.stl",
        }
        entries = {"part-1": {"sha256": "a" * 64}, "part-2": {"sha256": "b" * 64}}
        _validate_scene_interface_names(base, entries)
        for value in (
            "../../outside.stl",
            "/absolute/native.stl",
            "C:/outside/native.stl",
            "meshes/visual/reference/native.txt",
            "meshes/other/native.stl",
            "meshes/visual/reference/../native.stl",
        ):
            with self.subTest(value=value):
                names = dict(base)
                names["mesh:part-1"] = value
                with self.assertRaises(ConfigError):
                    _validate_scene_interface_names(names, entries)
        collision = dict(base)
        collision["mesh:part-2"] = collision["mesh:part-1"]
        with self.assertRaisesRegex(ConfigError, "different current mesh bytes"):
            _validate_scene_interface_names(collision, entries)


if __name__ == "__main__":
    unittest.main()
