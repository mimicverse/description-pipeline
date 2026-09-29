"""Shared fixtures for the test-suite."""

from __future__ import annotations


def make_config_dict(model: str = "fake_robot_v1") -> dict:
    return {
        "schema_version": "swbridge.export-config/v1",
        "model": model,
        "source_length_unit": "m",
        "mesh": {"format": "stl_binary", "merge": "per_link"},
        "links": [
            {"name": "base_link", "components": ["Base-1"]},
            {"name": "arm_link", "components": ["Arm-1"]},
        ],
        "joints": [
            {
                "name": "dof_arm",
                "type": "revolute",
                "parent": "base_link",
                "child": "arm_link",
                "coordinate_system": "CS_dof_arm",
                "axis": [0.0, 0.0, 1.0],
                "limits": {"lower": -1.0, "upper": 1.0, "effort": 10.0, "velocity": 2.0},
                "dynamics": {"damping": 0.5, "friction": 0.1},
            }
        ],
    }
