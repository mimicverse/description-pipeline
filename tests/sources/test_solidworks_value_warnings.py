"""Independent value-provenance gate for CAD-derived names and content paths."""

from __future__ import annotations

import copy
import hashlib
import math
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from description_pipeline.io import PipelineError
from description_pipeline.sources.solidworks.naming import material_name, mesh_path, visual_name
from description_pipeline.sources.solidworks.scene import _value_warnings
from description_pipeline.verification.solidworks_urdf import _carrier_binding, _joint, _value_warning_gate


class ValueWarningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "evidence/geometry").mkdir(parents=True)
        (self.root / "evidence/geometry/mesh.stl").write_bytes(b"mesh")
        self.digest = hashlib.sha256(b"mesh").hexdigest()
        self.visual = visual_name("part-1", link="base_link")
        self.mesh_name = mesh_path(self.digest, namespace="M3.0.3_20261016.SLDASM")
        self.material = material_name("part-1", link="base_link")
        self.source = {
            "robot_name": "robot",
            "robot_name_source": "cad",
            "assembly": "/source/M3.0.3_20261016.SLDASM",
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
                                "source": "CAD-derived interface naming for CS_base_link",
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

    def test_source_name_rule_claims_are_re_derived_not_trusted(self):
        source = copy.deepcopy(self.source)
        source["bodies"][0]["name_rule"] = {
            "rule_id": "naming.fabricated",
            "rule_version": "forged-v9",
            "reason": "manual export",
        }
        model = copy.deepcopy(self.model)
        model["provenance"]["value_warnings"][0]["objects"][0].update(
            rule_id="naming.fabricated",
            rule_version="forged-v9",
            reason="manual export",
        )
        with self.assertRaises(PipelineError):
            _value_warning_gate(self.root, source, model)

    def test_carrier_binding_rejects_tampered_names_members_and_topology(self):
        carrier = {
            "root": "CS_base_link",
            "bodies": [
                {"name": "CS_base_link", "frame": {"coordinate_system": "CS_base_link"}, "components": ["base"]},
                {"name": "a_arm", "frame": {"coordinate_system": "c_arm"}, "components": ["arm"]},
            ],
            "joints": [
                {"name": "a_arm", "type": "revolute", "parent": "CS_base_link", "child": "a_arm"},
            ],
        }
        source = {
            "bodies": [
                {
                    "name": "base_link",
                    "source_name": "CS_base_link",
                    "components": ["base"],
                    "frame": {"coordinate_system": "CS_base_link"},
                },
                {
                    "name": "arm_link",
                    "source_name": "a_arm",
                    "components": ["arm"],
                    "frame": {"coordinate_system": "c_arm"},
                },
            ],
            "joints": [
                {
                    "name": "arm_joint",
                    "source_name": "a_arm",
                    "type": "revolute",
                    "parent": "base_link",
                    "child": "arm_link",
                },
            ],
        }
        self.assertEqual(_carrier_binding(source, carrier), {"bodies": 2, "joints": 1})

        forged_name = copy.deepcopy(source)
        forged_name["bodies"][1]["source_name"] = "forged"
        with self.assertRaises(PipelineError):
            _carrier_binding(forged_name, carrier)

        forged_members = copy.deepcopy(source)
        forged_members["bodies"][1]["components"] = ["forged"]
        with self.assertRaises(PipelineError):
            _carrier_binding(forged_members, carrier)

        forged_topology = copy.deepcopy(source)
        forged_topology["joints"][0]["parent"] = "arm_link"
        with self.assertRaises(PipelineError):
            _carrier_binding(forged_topology, carrier)

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

    def test_direction_rule_warning_preserves_an_unverified_authored_axis(self):
        source = copy.deepcopy(self.source)
        model = copy.deepcopy(self.model)
        source["joints"] = [
            {
                "name": "left_shoulder_pitch_joint",
                "source_name": "left_shoulder_pitch_joint",
                "type": "revolute",
                "axis_provenance": {
                    "status": "unverified",
                    "motion": "pitch",
                    "parent_axis": [0.0, -0.9396926208, -0.3420201433],
                    "rule_id": "direction.positive_semantics",
                    "rule_version": "v3",
                    "reason": "declared motion does not match the local mechanical axis",
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
                "code": "direction.semantics_unresolved",
                "message": "Authored CAD axes are preserved; functional positive motion is not proven.",
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
                        "reason": "declared motion does not match the local mechanical axis",
                        "motion": "pitch",
                    }
                ],
            }
        )
        result = _value_warning_gate(self.root, source, model)
        self.assertEqual(result["objects"], 10)

    def test_preview_policy_limit_warning_is_field_level(self):
        policy = {
            "rule_id": "inspection_rotation_full_turn/v1",
            "rule_version": "v1",
            "window_rad": [-math.pi, math.pi],
            "native_cad_ranges": False,
            "hardware_qualified": False,
            "author_limits": {"lower": 0.0, "upper": 0.0, "effort": 0.0, "velocity": 0.0},
        }
        cfg = {
            "joints": [
                {
                    "name": "hinge",
                    "source_name": "hinge",
                    "type": "revolute",
                    "limit_source": "preview_policy",
                    "preview_policy": policy,
                }
            ],
            "frames": [],
        }
        joints = [
            {
                "name": "hinge",
                "limits": {
                    "lower": -math.pi,
                    "upper": math.pi,
                    "effort": 0.0,
                    "velocity": 0.0,
                },
            }
        ]
        warnings = _value_warnings(cfg, [], [], joints, [])
        warning = next(item for item in warnings if item["code"] == "limits.preview_policy")
        row = warning["objects"][0]
        self.assertEqual(row["field"], "limits")
        self.assertEqual(row["rule_id"], "inspection_rotation_full_turn/v1")
        self.assertEqual(row["actual"], joints[0]["limits"])
        self.assertEqual(row["original"], policy["author_limits"])
        self.assertFalse(row["native_cad_ranges"])
        self.assertFalse(row["hardware_qualified"])

    def test_preview_policy_warning_gate_accepts_the_policy(self):
        source = copy.deepcopy(self.source)
        model = copy.deepcopy(self.model)
        policy = {
            "rule_id": "inspection_rotation_full_turn/v1",
            "rule_version": "v1",
            "window_rad": [-math.pi, math.pi],
            "native_cad_ranges": False,
            "hardware_qualified": False,
            "author_limits": {"lower": 0.0, "upper": 0.0, "effort": 0.0, "velocity": 0.0},
        }
        limits = {"lower": -math.pi, "upper": math.pi, "effort": 0.0, "velocity": 0.0}
        source["joints"] = [
            {
                "name": "right_elbow_pitch_joint",
                "source_name": "right_elbow_pitch_joint",
                "type": "revolute",
                "limit_source": "preview_policy",
                "preview_policy": policy,
                "limits": limits,
            }
        ]
        model["joints"] = [{"name": "right_elbow_pitch_joint", "limits": limits}]
        next(
            item for item in model["provenance"]["value_warnings"]
            if item["code"] == "geometry.derived_from_native"
        )["objects"].append(
            {
                "object_type": "joint",
                "object": "right_elbow_pitch_joint",
                "fields": ["xyz", "rpy", "axis"],
                "actual": "derived from native coordinate systems and axis declarations",
                "source": "native_derived",
            }
        )
        model["provenance"]["value_warnings"].append(
            {
                "code": "limits.preview_policy",
                "message": (
                    "Inspection preview uses a versioned full-turn rotation window; "
                    "no native mechanical range is claimed."
                ),
                "objects": [
                    {
                        "object_type": "joint",
                        "object": "right_elbow_pitch_joint",
                        "field": "limits",
                        "actual": limits,
                        "source": "inspection preview full-turn policy",
                        "original": policy["author_limits"],
                        "rule_id": "inspection_rotation_full_turn/v1",
                        "rule_version": "v1",
                        "reason": "inspection preview window; no native mechanical range is claimed",
                        "native_cad_ranges": False,
                        "hardware_qualified": False,
                    }
                ],
            }
        )
        result = _value_warning_gate(self.root, source, model)
        self.assertGreaterEqual(result["objects"], 1)

    def test_preview_policy_joint_gate_requires_inspection_and_the_exact_window(self):
        node = ET.fromstring(
            "<joint name='hinge' type='revolute'>"
            "<parent link='base'/><child link='tool'/>"
            "<origin xyz='0 0 0' rpy='0 0 0'/><axis xyz='0 0 1'/>"
            "<limit lower='-3.141592653589793' upper='3.141592653589793' effort='0' velocity='0'/>"
            "</joint>"
        )
        limits = {"lower": -math.pi, "upper": math.pi, "effort": 0.0, "velocity": 0.0}
        policy = {
            "rule_id": "inspection_rotation_full_turn/v1",
            "rule_version": "v1",
            "native_cad_ranges": False,
            "hardware_qualified": False,
        }
        authored = {
            "name": "hinge",
            "type": "revolute",
            "parent": "base",
            "child": "tool",
            "axis": [0.0, 0.0, 1.0],
            "limits": limits,
            "limit_source": "preview_policy",
            "preview_policy": policy,
        }
        canonical = {
            **authored,
            "xyz": [0.0, 0.0, 0.0],
            "rpy": [0.0, 0.0, 0.0],
        }
        body_datums = {"base": np.eye(4), "tool": np.eye(4)}
        self.assertEqual(
            _joint(node, authored, canonical, {}, body_datums, [], preview=True)["limits"],
            limits,
        )
        with self.assertRaises(PipelineError):
            _joint(node, authored, canonical, {}, body_datums, [], preview=False)


if __name__ == "__main__":
    unittest.main()
