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
from description_pipeline.sources.solidworks.naming import (
    DISPLAY_MATERIAL_FALLBACK_NAME,
    DISPLAY_MATERIAL_FALLBACK_RGBA,
    DISPLAY_MATERIAL_FALLBACK_RULE,
    mesh_path,
    visual_name,
)
from description_pipeline.sources.solidworks.scene import _value_warnings
from description_pipeline.verification.solidworks_urdf import (
    _carrier_binding,
    _carrier_snapshot,
    _joint,
    _value_warning_gate,
)


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
        self.material = DISPLAY_MATERIAL_FALLBACK_NAME
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
                            "rgba": list(DISPLAY_MATERIAL_FALLBACK_RGBA),
                            "scale": [1.0, 1.0, 1.0],
                            "provenance": {
                                "native_component": "part-1",
                                "material_source": "platform_display_fallback",
                                **DISPLAY_MATERIAL_FALLBACK_RULE,
                            },
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
                        "code": "display.material_fallback",
                        "message": (
                            "CAD appearance is unavailable; a platform display fallback is used "
                            "without material semantics."
                        ),
                        "objects": [
                            {
                                "object_type": "visual",
                                "object": self.visual,
                                "field": "material",
                                "actual": {"name": self.material, "rgba": list(DISPLAY_MATERIAL_FALLBACK_RGBA)},
                                "source": "platform display fallback; CAD appearance unobserved",
                                "original": None,
                                "rule_id": DISPLAY_MATERIAL_FALLBACK_RULE["rule_id"],
                                "rule_version": DISPLAY_MATERIAL_FALLBACK_RULE["rule_version"],
                                "reason": DISPLAY_MATERIAL_FALLBACK_RULE["reason"],
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
                                "fields": ["scale", "material.rgba"],
                                "actual": {
                                    "scale": [1.0, 1.0, 1.0],
                                    "rgba": list(DISPLAY_MATERIAL_FALLBACK_RGBA),
                                },
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

    def test_metadata_warning_is_compared_as_an_exact_existing_value_warning(self):
        metadata_group = {
            "code": "metadata.non_native",
            "message": (
                "Platform metadata includes values that are not direct CAD engineering readings; "
                "each field is bound to recorded evidence."
            ),
            "objects": [
                {
                    "object_type": "metadata",
                    "object": "evidence/native-runtime.json",
                    "fields": ["/session_boundary/opened/read_only"],
                    "source": "native operation claim",
                    "rule_id": "metadata.operation_claim",
                    "rule_version": "v1",
                    "reason": "read_only is declared, not observed",
                    "file": "evidence/native-runtime.json",
                    "sha256": "c" * 64,
                    "reference": "evidence/native-runtime.json",
                }
            ],
        }
        model = copy.deepcopy(self.model)
        model["provenance"]["value_warnings"].append(copy.deepcopy(metadata_group))

        result = _value_warning_gate(
            self.root,
            self.source,
            model,
            metadata_warnings=[metadata_group],
        )
        self.assertEqual(result["objects"], 9)

        forged = copy.deepcopy(metadata_group)
        forged["objects"][0]["fields"] = ["/captured_at"]
        with self.assertRaisesRegex(PipelineError, "metadata.non_native"):
            _value_warning_gate(
                self.root,
                self.source,
                model,
                metadata_warnings=[forged],
            )

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

    def test_model_cannot_self_claim_cad_material_when_raw_schema_has_no_appearance(self):
        model = copy.deepcopy(self.model)
        visual = model["links"][0]["visuals"][0]
        visual.update(material="fabricated_blue", rgba=[0.0, 0.0, 1.0, 1.0])
        visual["provenance"].update(
            material_source="cad",
            material_original="fabricated_blue",
            rule_id="naming.cad_appearance",
            rule_version="naming-v1",
            reason="CAD appearance material formatting",
        )
        warning = next(
            item
            for item in model["provenance"]["value_warnings"]
            if item["code"] == "display.material_fallback"
        )
        warning["objects"][0].update(
            actual={"name": "fabricated_blue", "rgba": [0.0, 0.0, 1.0, 1.0]},
            source="CAD appearance",
            original="fabricated_blue",
            rule_id="naming.cad_appearance",
        )
        with self.assertRaisesRegex(
            PipelineError,
            "capture schema carries no independently verified CAD appearance",
        ):
            _value_warning_gate(
                self.root,
                self.source,
                model,
                native_geometry_rows=[
                    {
                        "component": "part-1",
                        "path": "geometry/mesh.stl",
                        "sha256": self.digest,
                    }
                ],
            )

    def test_unverified_raw_appearance_is_rejected_instead_of_becoming_fallback(self):
        with self.assertRaisesRegex(
            PipelineError,
            "capture schema carries no independently verified CAD appearance",
        ):
            _value_warning_gate(
                self.root,
                self.source,
                self.model,
                native_geometry_rows=[
                    {
                        "component": "part-1",
                        "path": "geometry/mesh.stl",
                        "sha256": self.digest,
                        "material": "cad_gray",
                        "rgba": [0.7, 0.7, 0.7, 1.0],
                    }
                ],
            )

    def test_functional_joint_name_is_rederived_from_raw_axis_not_source_provenance(self):
        native_bodies = [
            {
                "name": "base_link3.0-1",
                "frame": {"coordinate_system": "CS_base_link"},
                "components": ["base"],
            },
            {
                "name": "l_elbow3.0.3-1",
                "frame": {"coordinate_system": "c_l_elbow"},
                "components": ["forearm"],
            },
            {
                "name": "l_wrist_pitch3.0-1",
                "frame": {"coordinate_system": "c_l_wrist_pitch"},
                "components": ["hand"],
            },
        ]
        native_joints = [
            {
                "name": "l_elbow",
                "type": "revolute",
                "parent": "base_link3.0-1",
                "child": "l_elbow3.0.3-1",
                "axis_reference": {
                    "kind": "reference_axis",
                    "component": "",
                    "feature_name": "a_l_elbow",
                },
            },
            {
                "name": "l_wrist_pitch",
                "type": "revolute",
                "parent": "l_elbow3.0.3-1",
                "child": "l_wrist_pitch3.0-1",
                "axis_reference": {
                    "kind": "reference_axis",
                    "component": "",
                    "feature_name": "a_l_wrist_pitch",
                },
            },
        ]
        raw = {
            "reference_axes": [
                {
                    "selector": {
                        "kind": "reference_axis",
                        "component": "",
                        "feature_name": "a_l_elbow",
                    },
                    "axis_direction": [0.0, 1.0, 0.0],
                    "coordinate_frame": "assembly",
                    "direction_semantics": "undirected_axis_line",
                },
                {
                    "selector": {
                        "kind": "reference_axis",
                        "component": "",
                        "feature_name": "a_l_wrist_pitch",
                    },
                    "axis_direction": [0.0, 0.0, 1.0],
                    "coordinate_frame": "assembly",
                    "direction_semantics": "undirected_axis_line",
                },
            ]
        }
        carrier = _carrier_snapshot("base_link3.0-1", native_bodies, native_joints, raw)
        self.assertEqual(
            carrier["joints"][0]["axis_assembly_direction"],
            [0.0, 1.0, 0.0],
        )

        source = copy.deepcopy(self.source)
        source["bodies"] = [
            {
                "name": "base_link",
                "source_name": "base_link3.0-1",
                "frame": {"coordinate_system": "CS_base_link"},
                "components": ["base"],
            },
            {
                "name": "left_forearm_link",
                "source_name": "l_elbow3.0.3-1",
                "frame": {"coordinate_system": "c_l_elbow"},
                "components": ["forearm"],
            },
            {
                "name": "left_hand_link",
                "source_name": "l_wrist_pitch3.0-1",
                "frame": {"coordinate_system": "c_l_wrist_pitch"},
                "components": ["hand"],
            },
        ]
        source["joints"] = [
            {
                "name": "left_elbow_pitch_joint",
                "source_name": "l_elbow",
                "type": "revolute",
                "parent": "base_link",
                "child": "left_forearm_link",
                "axis": [0.0, 1.0, 0.0],
                "axis_provenance": {
                    "assembly_direction": [0.0, 1.0, 0.0],
                },
            },
            {
                "name": "left_wrist_pitch_joint",
                "source_name": "l_wrist_pitch",
                "type": "revolute",
                "parent": "left_forearm_link",
                "child": "left_hand_link",
                "axis": [0.0, 0.0, 1.0],
            },
        ]
        model = copy.deepcopy(self.model)
        model["links"] = [
            {"name": "base_link", "visuals": []},
            {"name": "left_forearm_link", "visuals": []},
            {"name": "left_hand_link", "visuals": []},
        ]
        model["joints"] = [
            {"name": "left_elbow_pitch_joint", "axis": [0.0, 1.0, 0.0]},
            {"name": "left_wrist_pitch_joint", "axis": [0.0, 0.0, 1.0]},
        ]
        with self.assertRaisesRegex(
            PipelineError,
            "joint name is not the deterministic CAD-derived rule output",
        ):
            _value_warning_gate(self.root, source, model, carrier)

    def test_author_sign_reversal_warning_is_derived_and_field_level(self):
        source = copy.deepcopy(self.source)
        provenance = {
            "direction_relation": "author_sign_applied",
            "rule_id": "direction.native_axis_sign",
            "rule_version": "v1",
            "native_sign": -1,
            "sign_reversal": True,
            "sign_warning": "discovery.direction_sign_reversed",
            "native_direction": [0.0, 0.0, 1.0],
            "direction": [0.0, 0.0, -1.0],
            "original_frame": "assembly",
            "output_frame": "assembly",
        }
        source["joints"] = [
            {
                "name": "arm_joint",
                "source_name": "arm_joint",
                "type": "revolute",
                "parent": "base_link",
                "child": "base_link",
                "axis": [0.0, 0.0, -1.0],
                "limits": {"lower": -0.3, "upper": 0.7, "unit": "rad"},
                "drive": {"effort": 1.0, "velocity": 2.0},
                "axis_provenance": provenance,
            }
        ]
        carrier = _carrier_snapshot(
            None,
            [],
            [{"name": "arm_joint"}],
            {
                "properties": {
                    "document": {"dp.joint.arm_joint.axis_sign": "-1"}
                }
            },
        )
        model = copy.deepcopy(self.model)
        model["joints"] = [{"name": "arm_joint", "axis": [0.0, 0.0, -1.0]}]
        warnings = model["provenance"]["value_warnings"]
        warnings.append(
            {
                "code": "direction.semantics_unresolved",
                "message": "Authored CAD axes are preserved; functional positive motion is not proven.",
                "objects": [
                    {
                        "object_type": "joint",
                        "object": "arm_joint",
                        "field": "axis",
                        "actual": [0.0, 0.0, -1.0],
                        "source": "CAD signed axis and versioned positive-motion rule",
                        "original": None,
                        "rule_id": "direction.native_axis_sign",
                        "rule_version": "v1",
                        "reason": "author_sign_applied",
                        "motion": None,
                    }
                ],
            }
        )
        warnings.append(
            {
                "code": "direction.author_sign_applied",
                "message": (
                    "CAD author axis_sign selects the positive direction of the undirected "
                    "shaft; q and ranges remain in author q."
                ),
                "objects": [
                    {
                        "object_type": "joint",
                        "object": "arm_joint",
                        "field": "axis",
                        "actual": [0.0, 0.0, -1.0],
                        "source": "CAD author dp.joint.axis_sign",
                        "original": [0.0, 0.0, 1.0],
                        "rule_id": "direction.native_axis_sign",
                        "rule_version": "v1",
                        "reason": (
                            "CAD author axis_sign selects the positive direction of the "
                            "undirected shaft; q and ranges remain in author q"
                        ),
                        "native_joint_identity": "arm_joint",
                        "native_sign": -1,
                        "sign_reversal": True,
                        "sign_warning": "discovery.direction_sign_reversed",
                        "original_axis_assembly": [0.0, 0.0, 1.0],
                        "output_axis_assembly": [0.0, 0.0, -1.0],
                        "actual_child_axis": [0.0, 0.0, -1.0],
                        "original_frame": "assembly",
                        "output_frame": "assembly",
                        "model_frame": "child",
                        "q_sign": 1,
                        "range_transform": "not_applied",
                        "limits_before": {"lower": -0.3, "upper": 0.7, "unit": "rad"},
                        "limits_after": {"lower": -0.3, "upper": 0.7, "unit": "rad"},
                        "drive_before": {"effort": 1.0, "velocity": 2.0},
                        "drive_after": {"effort": 1.0, "velocity": 2.0},
                        "limits_status": "present",
                        "drive_status": "present",
                        "warning_does_not_authorize": True,
                        "warning_does_not_replace_evidence": True,
                    }
                ],
            }
        )
        warnings.append(
            {
                "code": "naming.motion_unresolved",
                "message": "Joint names without a CAD motion classification retain a neutral name.",
                "objects": [
                    {
                        "object_type": "joint",
                        "object": "arm_joint",
                        "field": "name",
                        "actual": "arm_joint",
                        "source": "neutral joint naming",
                        "original": "arm_joint",
                        "rule_id": "naming.joint_motion",
                        "rule_version": "naming-v1",
                        "reason": "motion classification unavailable",
                    }
                ],
            }
        )
        next(
            item for item in warnings if item["code"] == "geometry.derived_from_native"
        )["objects"].append(
            {
                "object_type": "joint",
                "object": "arm_joint",
                "fields": ["xyz", "rpy", "axis"],
                "actual": "derived from native coordinate systems and axis declarations",
                "source": "native_derived",
            }
        )
        result = _value_warning_gate(self.root, source, model, carrier)
        self.assertIn("direction.author_sign_applied", result["codes"])
        tampered = copy.deepcopy(model)
        tampered["provenance"]["value_warnings"] = [
            item
            for item in tampered["provenance"]["value_warnings"]
            if item["code"] != "direction.author_sign_applied"
        ]
        tampered_source = copy.deepcopy(source)
        tampered_source["joints"][0].pop("axis_provenance")
        with self.assertRaises(PipelineError):
            _value_warning_gate(self.root, tampered_source, tampered, carrier)

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
        warnings.append(
            {
                "code": "naming.derived_frame_joint",
                "message": "Auxiliary frame joints derive their names from normalized parent and child identities.",
                "objects": [
                    {
                        "object_type": "joint",
                        "object": "base_link_to_tool_tcp_joint",
                        "field": "name",
                        "actual": "base_link_to_tool_tcp_joint",
                        "source": "normalized parent and frame identities",
                        "original": {"parent": "base_link", "child": "tool_tcp"},
                        "rule_id": "naming.fixed_endpoint_pair",
                        "rule_version": "naming-v1",
                        "reason": "fixed auxiliary joint derived from its normalized parent and child endpoints",
                    }
                ],
            }
        )
        result = _value_warning_gate(self.root, source, model)
        self.assertEqual(result["objects"], 12)

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
        model["provenance"]["value_warnings"].append(
            {
                "code": "naming.derived_frame_joint",
                "message": "Auxiliary frame joints derive their names from normalized parent and child identities.",
                "objects": [
                    {
                        "object_type": "joint",
                        "object": "base_link_to_tool_tcp_joint",
                        "field": "name",
                        "actual": "base_link_to_tool_tcp_joint",
                        "source": "normalized parent and frame identities",
                        "original": {"parent": "base_link", "child": "tool_tcp"},
                        "rule_id": "naming.fixed_endpoint_pair",
                        "rule_version": "naming-v1",
                        "reason": "fixed auxiliary joint derived from its normalized parent and child endpoints",
                    }
                ],
            }
        )
        result = _value_warning_gate(self.root, source, model)
        self.assertEqual(result["objects"], 10)
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

    def test_engineering_default_policy_warning_is_field_level(self):
        policy = {
            "rule_id": "engineering_rotation_default_window/v1",
            "rule_version": "v1",
            "window_rad": [-math.pi, math.pi],
            "native_cad_ranges": False,
            "hardware_qualified": False,
            "engineering_position_range_qualified": False,
            "author_limits": {"lower": 0.0, "upper": 0.0, "effort": 0.0, "velocity": 0.0},
            "native_absence": "complete_mate_inventory_no_enabled_limit",
            "native_observation_errors": [],
        }
        cfg = {
            "joints": [
                {
                    "name": "hinge",
                    "source_name": "hinge",
                    "type": "revolute",
                    "limit_source": "default_policy",
                    "default_policy": policy,
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
        warning = next(item for item in warnings if item["code"] == "limits.default_policy")
        row = warning["objects"][0]
        self.assertEqual(row["field"], "limits")
        self.assertEqual(row["rule_id"], "engineering_rotation_default_window/v1")
        self.assertEqual(row["actual"], joints[0]["limits"])
        self.assertEqual(row["original"], policy["author_limits"])
        self.assertFalse(row["native_cad_ranges"])
        self.assertFalse(row["hardware_qualified"])
        self.assertFalse(row["engineering_position_range_qualified"])
        self.assertEqual(row["native_observation_errors"], [])

    def test_declared_author_limits_and_drive_have_field_level_record_provenance(self):
        limits = {"lower": -1.5, "upper": 1.5, "effort": 0.0, "velocity": 0.0}
        native_errors = [{"mate": "native_limit", "reason": "IsAdvancedMate is missing or unreadable"}]
        limit_evidence = {
            "kind": "controlled_record",
            "file": "records/record_arm.txt",
            "sha256": "a" * 64,
            "anchor": '"limits"',
            "key": "limits",
            "reference": "joints/arm.json#limits",
            "declared_values": {"lower": -1.5, "upper": 1.5},
            "native_observation_errors": native_errors,
        }
        drive_evidence = {
            "kind": "controlled_record",
            "file": "records/record_arm.txt",
            "sha256": "b" * 64,
            "anchor": '"drive"',
            "key": "drive",
            "reference": "joints/arm.json#drive",
            "declared_values": {"effort": 0.0, "velocity": 0.0},
        }
        cfg = {
            "joints": [
                {
                    "name": "right_elbow_pitch_joint",
                    "source_name": "right_elbow_pitch_joint",
                    "type": "revolute",
                    "limit_source": "declared_author",
                    "limits": limits,
                    "limit_evidence": limit_evidence,
                    "drive_evidence": drive_evidence,
                    "drive_source": "controlled",
                }
            ],
            "frames": [],
        }
        joints = [{"name": "right_elbow_pitch_joint", "limits": limits}]
        warnings = _value_warnings(cfg, [], [], joints, [])

        declared = next(item for item in warnings if item["code"] == "limits.declared_author")
        self.assertEqual(
            [row["field"] for row in declared["objects"]],
            ["lower", "upper"],
        )
        for row in declared["objects"]:
            self.assertEqual(row["actual"], limits[row["field"]])
            self.assertEqual(row["original"], limits[row["field"]])
            self.assertIn(
                row["source"],
                {"controlled author dp.joint.limits_record", "controlled author dp.joint.drive_record"},
            )
        for row in declared["objects"][:2]:
            self.assertEqual(row["file"], limit_evidence["file"])
            self.assertEqual(row["sha256"], limit_evidence["sha256"])
            self.assertEqual(row["anchor"], limit_evidence["anchor"])
            self.assertEqual(row["key"], limit_evidence["key"])
            self.assertEqual(row["native_observation_errors"], native_errors)
        drive = next(item for item in warnings if item["code"] == "drive.declared_author")
        self.assertEqual([row["field"] for row in drive["objects"]], ["effort", "velocity"])
        for row in drive["objects"]:
            self.assertEqual(row["file"], drive_evidence["file"])
            self.assertEqual(row["sha256"], drive_evidence["sha256"])
            self.assertEqual(row["key"], drive_evidence["key"])

        unqualified = next(item for item in warnings if item["code"] == "drive.unqualified")
        self.assertEqual([row["field"] for row in unqualified["objects"]], ["effort", "velocity"])

    def test_declared_author_warning_gate_rejects_missing_foreign_duplicate_and_cross_joint_evidence(self):
        limits = {"lower": -1.5, "upper": 1.5, "effort": 0.0, "velocity": 0.0}
        limit_evidence = {
            "kind": "controlled_record",
            "file": "records/record_arm.txt",
            "sha256": "a" * 64,
            "anchor": '"limits"',
            "key": "limits",
            "reference": "joints/arm.json#limits",
            "declared_values": {"lower": -1.5, "upper": 1.5},
            "native_observation_errors": [{"mate": "native_limit", "reason": "unreadable"}],
        }
        drive_evidence = {
            "kind": "controlled_record",
            "file": "records/record_arm.txt",
            "sha256": "b" * 64,
            "anchor": '"drive"',
            "key": "drive",
            "reference": "joints/arm.json#drive",
            "declared_values": {"effort": 0.0, "velocity": 0.0},
        }
        source = copy.deepcopy(self.source)
        source["joints"] = [
            {
                "name": "right_elbow_pitch_joint",
                "source_name": "right_elbow_pitch_joint",
                "type": "revolute",
                "limit_source": "declared_author",
                "limits": limits,
                "limit_evidence": limit_evidence,
                "drive_evidence": drive_evidence,
                "drive_source": "controlled",
            }
        ]
        model = copy.deepcopy(self.model)
        model["joints"] = [
            {
                "name": "right_elbow_pitch_joint",
                "limits": limits,
                "provenance": {
                    "limits_evidence": copy.deepcopy(limit_evidence),
                    "drive_evidence": copy.deepcopy(drive_evidence),
                },
            }
        ]
        generated = [
            warning
            for warning in _value_warnings(source, source["bodies"], model["links"], model["joints"], [])
            if warning["code"] in {"limits.declared_author", "drive.declared_author", "drive.unqualified"}
        ]
        model["provenance"]["value_warnings"].extend(copy.deepcopy(generated))
        next(
            item for item in model["provenance"]["value_warnings"] if item["code"] == "geometry.derived_from_native"
        )["objects"].append(
            {
                "object_type": "joint",
                "object": "right_elbow_pitch_joint",
                "fields": ["xyz", "rpy", "axis"],
                "actual": "derived from native coordinate systems and axis declarations",
                "source": "native_derived",
            }
        )
        self.assertGreaterEqual(_value_warning_gate(self.root, source, model)["objects"], 1)

        missing = copy.deepcopy(model)
        missing["provenance"]["value_warnings"] = [
            item for item in missing["provenance"]["value_warnings"] if item["code"] != "limits.declared_author"
        ]
        with self.assertRaises(PipelineError):
            _value_warning_gate(self.root, source, missing)

        foreign = copy.deepcopy(model)
        foreign["joints"][0]["provenance"]["limits_evidence"]["sha256"] = "c" * 64
        with self.assertRaises(PipelineError):
            _value_warning_gate(self.root, source, foreign)

        duplicate = copy.deepcopy(model)
        duplicate["provenance"]["value_warnings"].append(
            copy.deepcopy(
                next(
                    item
                    for item in duplicate["provenance"]["value_warnings"]
                    if item["code"] == "limits.declared_author"
                )
            )
        )
        with self.assertRaises(PipelineError):
            _value_warning_gate(self.root, source, duplicate)

        reused_source = copy.deepcopy(source)
        reused_model = copy.deepcopy(model)
        second = copy.deepcopy(reused_source["joints"][0])
        second["name"] = "left_elbow_pitch_joint"
        second["source_name"] = "left_elbow_pitch_joint"
        reused_source["joints"].append(second)
        reused_model["joints"].append(
            {
                "name": "left_elbow_pitch_joint",
                "limits": limits,
                "provenance": {
                    "limits_evidence": copy.deepcopy(limit_evidence),
                    "drive_evidence": copy.deepcopy(drive_evidence),
                },
            }
        )
        extra = [
            warning
            for warning in _value_warnings(
                reused_source,
                reused_source["bodies"],
                reused_model["links"],
                reused_model["joints"],
                [],
            )
            if warning["code"] in {"limits.declared_author", "drive.declared_author", "drive.unqualified"}
        ]
        reused_model["provenance"]["value_warnings"] = [
            item
            for item in reused_model["provenance"]["value_warnings"]
            if item["code"] not in {"limits.declared_author", "drive.declared_author", "drive.unqualified"}
        ] + extra
        next(
            item
            for item in reused_model["provenance"]["value_warnings"]
            if item["code"] == "geometry.derived_from_native"
        )["objects"].append(
            {
                "object_type": "joint",
                "object": "left_elbow_pitch_joint",
                "fields": ["xyz", "rpy", "axis"],
                "actual": "derived from native coordinate systems and axis declarations",
                "source": "native_derived",
            }
        )
        with self.assertRaisesRegex(PipelineError, "reused across joint identities"):
            _value_warning_gate(self.root, reused_source, reused_model)

    def test_native_selected_author_placeholder_keeps_evidence_without_replacing_native_limits(self):
        limits = {"lower": -1.5, "upper": 1.5, "effort": 1.0, "velocity": 1.0}
        author_evidence = {
            "kind": "controlled_record",
            "file": "records/record_arm.txt",
            "sha256": "a" * 64,
            "anchor": '"limits"',
            "key": "limits",
            "reference": "joints/arm.json#limits",
            "declared_values": {"lower": 0.0, "upper": 0.0},
            "native_observation_errors": [],
        }
        node = ET.fromstring(
            "<joint name='right_elbow_pitch_joint' type='revolute'>"
            "<parent link='base'/><child link='tool'/>"
            "<origin xyz='0 0 0' rpy='0 0 0'/><axis xyz='0 0 1'/>"
            "<limit lower='-1.5' upper='1.5' effort='1' velocity='1'/>"
            "</joint>"
        )
        authored = {
            "name": "right_elbow_pitch_joint",
            "type": "revolute",
            "parent": "base",
            "child": "tool",
            "axis": [0.0, 0.0, 1.0],
            "limits": limits,
            "limit_source": "native",
            "limit_evidence": copy.deepcopy(author_evidence),
        }
        canonical = {
            **authored,
            "xyz": [0.0, 0.0, 0.0],
            "rpy": [0.0, 0.0, 0.0],
            "provenance": {"limits_evidence": copy.deepcopy(author_evidence)},
        }
        body_datums = {"base": np.eye(4), "tool": np.eye(4)}
        self.assertEqual(
            _joint(node, authored, canonical, {}, body_datums, [])["limits"],
            limits,
        )
        forged = copy.deepcopy(canonical)
        forged["provenance"]["limits_evidence"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(PipelineError, "exact embedded controlled record"):
            _joint(node, authored, forged, {}, body_datums, [])

        source = copy.deepcopy(self.source)
        source["joints"] = [authored]
        model = copy.deepcopy(self.model)
        model["joints"] = [
            {
                "name": "right_elbow_pitch_joint",
                "limits": limits,
                "provenance": {"limits_evidence": copy.deepcopy(author_evidence)},
            }
        ]
        next(
            item for item in model["provenance"]["value_warnings"] if item["code"] == "geometry.derived_from_native"
        )["objects"].append(
            {
                "object_type": "joint",
                "object": "right_elbow_pitch_joint",
                "fields": ["xyz", "rpy", "axis"],
                "actual": "derived from native coordinate systems and axis declarations",
                "source": "native_derived",
            }
        )
        self.assertGreaterEqual(_value_warning_gate(self.root, source, model)["objects"], 1)
        foreign = copy.deepcopy(model)
        foreign["joints"][0]["provenance"]["limits_evidence"]["anchor"] = '"forged"'
        with self.assertRaisesRegex(PipelineError, "native-selected limits evidence"):
            _value_warning_gate(self.root, source, foreign)

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

    def test_engineering_default_policy_warning_gate_accepts_the_policy(self):
        source = copy.deepcopy(self.source)
        model = copy.deepcopy(self.model)
        policy = {
            "rule_id": "engineering_rotation_default_window/v1",
            "rule_version": "v1",
            "window_rad": [-math.pi, math.pi],
            "native_cad_ranges": False,
            "hardware_qualified": False,
            "engineering_position_range_qualified": False,
            "author_limits": {"lower": 0.0, "upper": 0.0, "effort": 0.0, "velocity": 0.0},
            "native_absence": "complete_mate_inventory_no_enabled_limit",
            "native_observation_errors": [],
        }
        limits = {"lower": -math.pi, "upper": math.pi, "effort": 0.0, "velocity": 0.0}
        source["joints"] = [
            {
                "name": "right_elbow_pitch_joint",
                "source_name": "right_elbow_pitch_joint",
                "type": "revolute",
                "limit_source": "default_policy",
                "default_policy": policy,
                "limits": limits,
            }
        ]
        model["joints"] = [{"name": "right_elbow_pitch_joint", "limits": limits}]
        next(item for item in model["provenance"]["value_warnings"] if item["code"] == "geometry.derived_from_native")[
            "objects"
        ].append(
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
                "code": "limits.default_policy",
                "message": (
                    "Engineering uses a versioned default rotation window because no usable native "
                    "or authored position range was read; the engineering position range is unqualified."
                ),
                "objects": [
                    {
                        "object_type": "joint",
                        "object": "right_elbow_pitch_joint",
                        "field": "limits",
                        "actual": limits,
                        "source": "engineering default rotation window policy",
                        "original": policy["author_limits"],
                        "rule_id": "engineering_rotation_default_window/v1",
                        "rule_version": "v1",
                        "reason": (
                            "engineering analysis default window; no native mechanical range or engineering "
                            "position-range qualification is claimed"
                        ),
                        "native_cad_ranges": False,
                        "hardware_qualified": False,
                        "engineering_position_range_qualified": False,
                        "native_absence": policy["native_absence"],
                        "native_observation_errors": [],
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

    def test_engineering_default_policy_joint_gate_requires_engineering_and_exact_qualification(self):
        node = ET.fromstring(
            "<joint name='hinge' type='revolute'>"
            "<parent link='base'/><child link='tool'/>"
            "<origin xyz='0 0 0' rpy='0 0 0'/><axis xyz='0 0 1'/>"
            "<limit lower='-3.141592653589793' upper='3.141592653589793' effort='0' velocity='0'/>"
            "</joint>"
        )
        limits = {"lower": -math.pi, "upper": math.pi, "effort": 0.0, "velocity": 0.0}
        policy = {
            "rule_id": "engineering_rotation_default_window/v1",
            "rule_version": "v1",
            "native_cad_ranges": False,
            "hardware_qualified": False,
            "engineering_position_range_qualified": False,
            "native_absence": "complete_mate_inventory_no_enabled_limit",
            "native_observation_errors": [],
        }
        authored = {
            "name": "hinge",
            "type": "revolute",
            "parent": "base",
            "child": "tool",
            "axis": [0.0, 0.0, 1.0],
            "limits": limits,
            "limit_source": "default_policy",
            "default_policy": policy,
        }
        canonical = {
            **authored,
            "xyz": [0.0, 0.0, 0.0],
            "rpy": [0.0, 0.0, 0.0],
        }
        body_datums = {"base": np.eye(4), "tool": np.eye(4)}
        self.assertEqual(
            _joint(node, authored, canonical, {}, body_datums, [], preview=False)["limits"],
            limits,
        )
        with self.assertRaises(PipelineError):
            _joint(node, authored, canonical, {}, body_datums, [], preview=True)
        forged = copy.deepcopy(authored)
        forged["default_policy"]["engineering_position_range_qualified"] = True
        forged_canonical = copy.deepcopy(canonical)
        forged_canonical["default_policy"] = forged["default_policy"]
        with self.assertRaises(PipelineError):
            _joint(node, forged, forged_canonical, {}, body_datums, [], preview=False)


if __name__ == "__main__":
    unittest.main()
