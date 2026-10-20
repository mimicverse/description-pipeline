"""Versioned audit of CAD-authored joint axes.

The native signed ``<Axis>`` value is the starting fact. The parent body's
datum is the local mechanical reference; the child datum carries the authored
axis and the joint frame. This module verifies a declared motion token against
that local evidence and reports unresolved semantics. It never substitutes a
different principal axis or sign when the authored value is valid.
"""

from __future__ import annotations

import math

from .naming import token

_PRINCIPAL = {
    "roll": (1.0, 0.0, 0.0),
    "pitch": (0.0, 1.0, 0.0),
    "yaw": (0.0, 0.0, 1.0),
}
_PRINCIPAL_ORDER = ("roll", "pitch", "yaw")
_ALIGNMENT_THRESHOLD = 0.9


def _rotation(frame) -> list[list[float]]:
    values = list(frame or ())
    if len(values) != 16 or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in values):
        raise ValueError("a 4x4 finite transform is required")
    matrix = [[float(values[row * 4 + column]) for column in range(3)] for row in range(3)]
    for left in range(3):
        for right in range(3):
            value = _dot(matrix[left], matrix[right])
            expected = 1.0 if left == right else 0.0
            if abs(value - expected) > 1e-7:
                raise ValueError("CAD reference frame is not orthonormal")
    determinant = _dot(matrix[0], _cross(matrix[1], matrix[2]))
    if determinant <= 0.0:
        raise ValueError("CAD reference frame is not right handed")
    return matrix


def _matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [
        sum(matrix[row][column] * vector[column] for column in range(3))
        for row in range(3)
    ]


def _transpose(matrix: list[list[float]]) -> list[list[float]]:
    return [[matrix[column][row] for column in range(3)] for row in range(3)]


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError("joint axis has no finite nonzero direction")
    return [value / norm for value in vector]


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(left: list[float], right: list[float]) -> list[float]:
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def _motion_token(native_name: str) -> str | None:
    tokens = token(native_name).split("_")
    return next((motion for motion in _PRINCIPAL_ORDER if motion in tokens), None)


def _geometry_motion(base_axis: list[float], threshold: float) -> tuple[str | None, float]:
    magnitudes = sorted((abs(value) for value in base_axis), reverse=True)
    index = max(range(3), key=lambda item: abs(base_axis[item]))
    projection = base_axis[index]
    if len(magnitudes) > 1 and magnitudes[0] - magnitudes[1] <= 1e-9:
        return None, projection
    if abs(projection) < threshold:
        return "oblique", projection
    return _PRINCIPAL_ORDER[index], projection


def canonicalize_axis(
    native_name: str,
    local_axis,
    parent_frame,
    child_frame,
    *,
    joint_type: str,
    threshold: float = _ALIGNMENT_THRESHOLD,
) -> dict:
    """Audit an authored child-frame axis against its parent-local semantics."""

    if joint_type not in {"revolute", "continuous", "prismatic"}:
        raise ValueError("positive motion semantics only apply to moving joints")
    local = _unit([float(value) for value in local_axis])
    parent = _rotation(parent_frame)
    child = _rotation(child_frame)
    assembly_axis = _unit(_matvec(child, local))
    parent_axis = _unit(_matvec(_transpose(parent), assembly_axis))
    declared_motion = _motion_token(native_name)
    local_principal, local_projection = _geometry_motion(parent_axis, threshold)
    if declared_motion is None:
        return {
            "status": "unverified",
            "motion": None,
            "declared_motion": None,
            "local_principal": local_principal,
            "classification_source": "unresolved",
            "alignment": local_projection,
            "raw_base_axis": parent_axis,
            "canonical_base_axis": parent_axis,
            "delivered_local_axis": local,
            "sign": 1,
            "reason": "no explicit CAD motion class",
            "rule_id": "direction.positive_semantics",
            "rule_version": "v3",
        }

    alignment = _dot(parent_axis, _PRINCIPAL[declared_motion])
    verified = abs(alignment) >= threshold
    return {
        "status": "verified" if verified else "unverified",
        "motion": declared_motion,
        "declared_motion": declared_motion,
        "local_principal": local_principal,
        "classification_source": "name",
        "alignment": alignment,
        "raw_base_axis": parent_axis,
        "canonical_base_axis": parent_axis,
        "delivered_local_axis": local,
        "sign": 1,
        "reason": "declared motion matches the local mechanical axis"
        if verified
        else "declared motion does not match the local mechanical axis",
        "rule_id": "direction.positive_semantics",
        "rule_version": "v3",
    }


def transform_limits(limits: dict | None, sign: int) -> dict | None:
    """Apply a canonical axis sign to a joint's coordinate interval."""

    if not isinstance(limits, dict):
        return limits
    result = dict(limits)
    if sign == -1 and "lower" in result and "upper" in result:
        result["lower"], result["upper"] = -float(result["upper"]), -float(result["lower"])
    return result
