"""Independent value-provenance gate for CAD-derived names and content paths."""

from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError
from description_pipeline.sources.solidworks.naming import material_name, mesh_path, visual_name
from description_pipeline.verification.solidworks_urdf import _value_warning_gate


class ValueWarningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "evidence/geometry").mkdir(parents=True)
        (self.root / "evidence/geometry/mesh.stl").write_bytes(b"mesh")
        self.digest = hashlib.sha256(b"mesh").hexdigest()
        self.visual = visual_name("part-1", link="base_link")
        self.mesh_name = mesh_path(self.digest, semantic=self.visual)
        self.material = material_name("part-1", link="base_link")
        self.source = {
            "robot_name": "robot",
            "robot_name_source": "cad",
            "bodies": [{"name": "base_link", "source_name": "CS_base_link"}],
            "joints": [],
        }
        self.model = {
            "name": "robot",
            "links": [
                {
                    "name": "base_link",
                    "inertial": {"mass": 1.0},
                    "visuals": [
                        {
                            "name": self.visual,
                            "filename": "geometry/mesh.stl",
                            "sha256": self.digest,
                            "mesh_name": self.mesh_name,
                            "material": self.material,
                            "scale": [1.0, 1.0, 1.0],
                            "provenance": {"native_component": "part-1"},
                        }
                    ],
                }
            ],
            "joints": [],
            "frames": [],
            "provenance": {
                "value_warnings": [
                    {
                        "code": "naming.formatted_interface",
                        "message": (
                            "Link, joint, and frame names are deterministic formatting of native CAD identities."
                        ),
                        "objects": [
                            {
                                "object_type": "link",
                                "object": "base_link",
                                "field": "name",
                                "actual": "base_link",
                                "source": "CAD datum formatting for CS_base_link",
                                "original": "CS_base_link",
                                "rule_id": "naming.interface",
                                "rule_version": "naming-v1",
                                "reason": "CAD datum formatting",
                            }
                        ],
                    },
                    {
                        "code": "naming.derived_visual",
                        "message": "Visual names are derived from the exact native component occurrence identity.",
                        "objects": [
                            {
                                "object_type": "visual",
                                "object": self.visual,
                                "field": "name",
                                "actual": self.visual,
                                "source": "native component identity formatting",
                                "original": "part-1",
                                "rule_id": "naming.visual",
                                "rule_version": "naming-v1",
                                "reason": "native component identity formatting",
                            }
                        ],
                    },
                    {
                        "code": "naming.content_addressed_mesh",
                        "message": "Mesh paths are derived from the current captured STL content digest.",
                        "objects": [
                            {
                                "object_type": "visual",
                                "object": self.visual,
                                "field": "mesh filename",
                                "actual": self.mesh_name,
                                "source": "current capture content addressing",
                                "original": self.digest,
                                "rule_id": "naming.mesh",
                                "rule_version": "naming-v1",
                                "reason": "current capture content addressing",
                            }
                        ],
                    },
                    {
                        "code": "naming.derived_material",
                        "message": "Material names are derived from the owner link and native component identity.",
                        "objects": [
                            {
                                "object_type": "visual",
                                "object": self.visual,
                                "field": "material",
                                "actual": self.material,
                                "source": "native component identity formatting",
                                "original": "part-1",
                                "rule_id": "naming.material",
                                "rule_version": "naming-v1",
                                "reason": "native component identity formatting",
                            }
                        ],
                    },
                    {
                        "code": "geometry.derived_from_native",
                        "message": "Geometry, frame transforms and signed joint axes are derived from native evidence.",
                        "objects": [
                            {
                                "object_type": "link",
                                "object": "base_link",
                                "fields": ["visual.xyz", "visual.rpy", "visual.filename", "visual.scale"],
                                "actual": "derived from native occurrence transforms and capture geometry",
                                "source": "native_derived",
                            }
                        ],
                    },
                    {
                        "code": "units.si_conversion",
                        "message": "Delivered values use SI units after conversion from native CAD units.",
                        "objects": [
                            {
                                "object_type": "robot",
                                "object": "robot",
                                "field": "units",
                                "actual": "SI",
                                "source": "native_derived",
                            }
                        ],
                    },
                    {
                        "code": "physics.derived_from_native",
                        "message": (
                            "Link mass properties are transformed, translated or recombined from native readings."
                        ),
                        "objects": [
                            {
                                "object_type": "link",
                                "object": "base_link",
                                "fields": ["inertial.mass", "inertial.xyz", "inertial.rpy", "inertial.inertia"],
                                "actual": "transformed or recombined from native readings",
                                "source": "native_derived",
                            }
                        ],
                    },
                    {
                        "code": "display.constant",
                        "message": "Display scale and material color are platform constants, not CAD readings.",
                        "objects": [
                            {
                                "object_type": "visual",
                                "object": self.visual,
                                "fields": ["scale"],
                                "actual": {"scale": [1.0, 1.0, 1.0]},
                                "source": "display constant",
                            }
                        ],
                    },
                ]
            },
        }

    def test_valid_cad_derived_warning_inventory_passes(self):
        result = _value_warning_gate(self.root, self.source, self.model)
        self.assertEqual(result["objects"], 8)

    def test_missing_spoofed_or_wrong_field_warnings_are_rejected(self):
        mutations = (
            lambda warnings: warnings.pop(),
            lambda warnings: warnings.append(
                {
                    "code": "manual.registry",
                    "message": "manual",
                    "objects": [{"object": "x", "source": "y", "field": "z"}],
                }
            ),
            lambda warnings: warnings[1]["objects"][0].update(source="manual export"),
            lambda warnings: warnings[0]["objects"][0].update(object="wrong"),
            lambda warnings: warnings[3]["objects"][0].update(actual="meshes/visual/manual.stl"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                model = copy.deepcopy(self.model)
                mutate(model["provenance"]["value_warnings"])
                with self.assertRaises(PipelineError):
                    _value_warning_gate(self.root, self.source, model)

    def test_visual_name_and_mesh_path_must_match_current_cad_bytes(self):
        model = copy.deepcopy(self.model)
        model["links"][0]["visuals"][0]["mesh_name"] = "meshes/visual/manual.stl"
        with self.assertRaisesRegex(PipelineError, "content addressing"):
            _value_warning_gate(self.root, self.source, model)
        model = copy.deepcopy(self.model)
        model["links"][0]["visuals"][0]["sha256"] = "b" * 64
        with self.assertRaisesRegex(PipelineError, "digest"):
            _value_warning_gate(self.root, self.source, model)

    def test_frame_warning_and_robot_source_name_are_rederived(self):
        source = copy.deepcopy(self.source)
        model = copy.deepcopy(self.model)
        source["assembly"] = "input/cad/M3.0.3_20261016.SLDASM"
        source["robot_name_source"] = "cad_filename_derived"
        source["frames"] = [
            {
                "id": "tool_tcp",
                "name": "tool_tcp",
                "parent": "base_link",
                "coordinate_system": "TCP_Tool",
                "source_name": "TCP_Tool",
            }
        ]
        model["frames"] = [{"id": "tool_tcp", "name": "tool_tcp"}]
        warnings = model["provenance"]["value_warnings"]
        next(item for item in warnings if item["code"] == "naming.formatted_interface")["objects"].append(
            {
                "object_type": "frame",
                "object": "tool_tcp",
                "field": "name",
                "actual": "tool_tcp",
                "source": "CAD interface datum formatting for TCP_Tool",
                "original": "TCP_Tool",
                "rule_id": "naming.frame",
                "rule_version": "naming-v1",
                "reason": "CAD interface datum formatting",
            }
        )
        next(item for item in warnings if item["code"] == "geometry.derived_from_native")["objects"].append(
            {
                "object_type": "frame",
                "object": "tool_tcp",
                "fields": ["xyz", "rpy"],
                "actual": "derived from native coordinate systems",
                "source": "native_derived",
            }
        )
        warnings.append(
            {
                "code": "naming.formatted_robot_name",
                "message": "The robot name is formatted from the native top-assembly filename.",
                "objects": [
                    {
                        "object_type": "robot",
                        "object": "robot",
                        "field": "name",
                        "actual": "robot",
                        "source": "native top assembly filename formatting",
                        "original": "M3.0.3_20261016.SLDASM",
                        "rule_id": "naming.robot",
                        "rule_version": "naming-v1",
                        "reason": "native top assembly filename formatting",
                    }
                ],
            }
        )
        result = _value_warning_gate(self.root, source, model)
        self.assertEqual(result["objects"], 11)

    def test_unclassified_joint_has_a_direct_warning_fixture(self):
        source = copy.deepcopy(self.source)
        model = copy.deepcopy(self.model)
        source["joints"] = [{"name": "waist_joint", "source_name": "waist_joint", "type": "revolute"}]
        model["joints"] = [{"name": "waist_joint"}]
        warnings = model["provenance"]["value_warnings"]
        next(item for item in warnings if item["code"] == "geometry.derived_from_native")["objects"].append(
            {
                "object_type": "joint",
                "object": "waist_joint",
                "fields": ["xyz", "rpy", "axis"],
                "actual": "derived from native coordinate systems and axis declarations",
                "source": "native_derived",
            }
        )
        warnings.append(
            {
                "code": "naming.motion_unresolved",
                "message": "Joint names without a CAD motion classification retain a neutral name.",
                "objects": [
                    {
                        "object_type": "joint",
                        "object": "waist_joint",
                        "field": "name",
                        "actual": "waist_joint",
                        "source": "neutral joint naming",
                        "original": "waist_joint",
                        "rule_id": "naming.joint_motion",
                        "rule_version": "naming-v1",
                        "reason": "motion classification unavailable",
                    }
                ],
            }
        )
        result = _value_warning_gate(self.root, source, model)
        self.assertEqual(result["objects"], 10)

    def test_conforming_frame_keeps_source_name_without_a_formatting_warning(self):
        source = copy.deepcopy(self.source)
        model = copy.deepcopy(self.model)
        source["frames"] = [
            {
                "id": "tool_tcp",
                "name": "tool_tcp",
                "parent": "base_link",
                "coordinate_system": "TCP_tool_tcp",
                "source_name": "TCP_tool_tcp",
            }
        ]
        model["frames"] = [{"id": "tool_tcp", "name": "tool_tcp"}]
        next(
            item
            for item in model["provenance"]["value_warnings"]
            if item["code"] == "geometry.derived_from_native"
        )["objects"].append(
            {
                "object_type": "frame",
                "object": "tool_tcp",
                "fields": ["xyz", "rpy"],
                "actual": "derived from native coordinate systems",
                "source": "native_derived",
            }
        )
        result = _value_warning_gate(self.root, source, model)
        self.assertEqual(result["objects"], 9)
        formatted = next(
            item
            for item in model["provenance"]["value_warnings"]
            if item["code"] == "naming.formatted_interface"
        )
        self.assertFalse(any(item["object_type"] == "frame" for item in formatted["objects"]))

    def test_direction_rule_warning_is_rederived_for_a_reversed_axis(self):
        source = copy.deepcopy(self.source)
        model = copy.deepcopy(self.model)
        source["joints"] = [
            {
                "name": "left_shoulder_pitch_joint",
                "source_name": "left_shoulder_pitch_joint",
                "type": "revolute",
                "axis_provenance": {
                    "status": "canonical",
                    "sign": -1,
                    "motion": "pitch",
                    "raw_base_axis": [0.0, -0.9396926208, -0.3420201433],
                    "rule_id": "direction.positive_semantics",
                    "rule_version": "v3",
                },
            }
        ]
        model["joints"] = [{"name": "left_shoulder_pitch_joint", "axis": [0.0, 0.0, 1.0]}]
        warnings = model["provenance"]["value_warnings"]
        next(
            item for item in warnings if item["code"] == "geometry.derived_from_native"
        )["objects"].append(
            {
                "object_type": "joint",
                "object": "left_shoulder_pitch_joint",
                "fields": ["xyz", "rpy", "axis"],
                "actual": "derived from native coordinate systems and axis declarations",
                "source": "native_derived",
            }
        )
        warnings.append(
            {
                "code": "direction.sign_normalized",
                "message": "Joint axes were normalized to the versioned positive-motion convention.",
                "objects": [
                    {
                        "object_type": "joint",
                        "object": "left_shoulder_pitch_joint",
                        "field": "axis",
                        "actual": [0.0, 0.0, 1.0],
                        "source": "CAD signed axis and versioned positive-motion rule",
                        "original": [0.0, -0.9396926208, -0.3420201433],
                        "rule_id": "direction.positive_semantics",
                        "rule_version": "v3",
                        "reason": "canonical",
                        "sign": -1,
                        "motion": "pitch",
                    }
                ],
            }
        )
        result = _value_warning_gate(self.root, source, model)
        self.assertEqual(result["objects"], 10)


if __name__ == "__main__":
    unittest.main()
