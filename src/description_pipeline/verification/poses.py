"""Declared working poses for the contact and reset checks (contract in docs/pipeline.md).

Collision is asserted only at these poses - a humanoid may legitimately touch itself
elsewhere in its joint range - and the first pose is the state the consumer resets to.
Full-range FK/dynamics sampling stays independent of this input.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from ..io import PipelineError, confined, read_data
from .urdf_quality import model as parser

SCHEMA = "description.validation-poses/v1"
CONFIG_DIR = "config"
FIELDS = ("schema_version", "poses")
POSE_FIELDS = ("name", "joints", "base")
MIMIC_TOLERANCE = 1e-9


def validate_path(value: object) -> str | None:
    """Shape of ``profile.validation_poses``; the file itself is read later."""

    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PipelineError("profile.validation_poses must be a config-relative JSON path")
    parts = value.split("/")
    if value.startswith("/") or "\\" in value or ".." in parts or not value.endswith(".json"):
        raise PipelineError("profile.validation_poses must be a relative config/*.json path without '..'")
    if len(parts) < 2 or parts[0] != CONFIG_DIR:
        raise PipelineError("profile.validation_poses must point inside config/")
    return value


def _numbers(value: object, label: str, count: int) -> list[float]:
    if not isinstance(value, list) or len(value) != count:
        raise PipelineError(f"{label} must be a list of {count} numbers")
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise PipelineError(f"{label} must contain finite numbers")
        out.append(float(item))
    return out


def load(root: Path, profile: dict, urdf, mimics: dict | None = None) -> list[dict]:
    """Read and strictly validate the declared poses; ``[]`` when none are declared."""

    relative = validate_path(profile.get("validation_poses"))
    if relative is None:
        return []
    path = root / relative
    if not path.is_file():
        raise PipelineError(f"validation poses file not found: {relative}")
    data = read_data(confined(root, relative))
    if not isinstance(data, dict) or set(data) != set(FIELDS):
        raise PipelineError("validation poses file must declare exactly schema_version and poses")
    if data["schema_version"] != SCHEMA:
        raise PipelineError("Unsupported validation poses schema")
    raw = data["poses"]
    if not isinstance(raw, list) or not raw:
        raise PipelineError("validation poses must be a non-empty list")

    moving = {joint.name: joint for joint in urdf.joints.values() if joint.moveable}
    floating = profile.get("root_mode") == "floating"
    relations = mimics or {}
    poses: list[dict] = []
    seen: set[str] = set()
    for index, pose in enumerate(raw):
        if not isinstance(pose, dict) or set(pose) - set(POSE_FIELDS):
            raise PipelineError(f"validation pose {index} must be an object with name/joints/base only")
        name = pose.get("name", f"pose-{index}")
        if not isinstance(name, str) or not name.strip() or name in seen:
            raise PipelineError(f"validation pose {index} needs a unique non-empty name")
        seen.add(name)
        joints = pose.get("joints")
        if not isinstance(joints, dict):
            raise PipelineError(f"validation pose {name} must declare a joints object")
        missing = sorted(set(moving) - set(joints))
        unknown = sorted(set(joints) - set(moving))
        if missing or unknown:
            raise PipelineError(
                f"validation pose {name} must cover every moveable joint exactly",
                {"missing": missing, "unknown": unknown},
            )
        values: dict[str, float] = {}
        for joint_name, raw_value in joints.items():
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise PipelineError(f"validation pose {name}: {joint_name} must be a number")
            value = float(raw_value)
            if not math.isfinite(value):
                raise PipelineError(f"validation pose {name}: {joint_name} must be finite")
            joint = moving[joint_name]
            if joint.type != "continuous":
                limits = joint.limits or {}
                lower, upper = limits.get("lower"), limits.get("upper")
                if lower is None or upper is None:
                    raise PipelineError(f"validation pose {name}: {joint_name} has no declared limits")
                if not (lower - MIMIC_TOLERANCE <= value <= upper + MIMIC_TOLERANCE):
                    raise PipelineError(
                        f"validation pose {name}: {joint_name} is outside its limits",
                        {"value": value, "lower": lower, "upper": upper},
                    )
            values[joint_name] = value
        for joint_name, relation in relations.items():
            if joint_name not in values:
                continue
            expected = relation["multiplier"] * values[relation["joint"]] + relation["offset"]
            if abs(values[joint_name] - expected) > MIMIC_TOLERANCE:
                raise PipelineError(
                    f"validation pose {name}: {joint_name} contradicts its mimic relation",
                    {"declared": values[joint_name], "expected": expected},
                )
            values[joint_name] = expected
        matrix = None
        base = pose.get("base")
        if floating:
            if not isinstance(base, dict) or set(base) != {"position", "rpy"}:
                raise PipelineError(f"validation pose {name} must declare base position and rpy")
            position = _numbers(base["position"], f"validation pose {name}: base.position", 3)
            rpy = _numbers(base["rpy"], f"validation pose {name}: base.rpy", 3)
            matrix = np.eye(4)
            matrix[:3, :3] = parser.rpy_matrix(tuple(rpy))
            matrix[:3, 3] = position
        elif "base" in pose:
            raise PipelineError(f"validation pose {name} declares a base pose for a fixed-root profile")
        poses.append({"name": name, "joints": values, "base": base, "matrix": matrix})
    return poses
