"""Versioned positive-motion semantics for CAD joint axes.

The CAD declaration supplies a signed child-frame vector.  The root datum is
the mechanical reference frame; the rule only selects its sign from the
declared or geometrically demonstrated roll/pitch/yaw class.  An oblique axis
stays oblique: no component is snapped to a principal axis.
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
    child_frame,
    base_frame,
    *,
    joint_type: str,
    threshold: float = _ALIGNMENT_THRESHOLD,
) -> dict:
    """Derive the versioned canonical positive direction and its evidence."""

    if joint_type not in {"revolute", "continuous", "prismatic"}:
        raise ValueError("positive motion semantics only apply to moving joints")
    local = _unit([float(value) for value in local_axis])
    child = _rotation(child_frame)
    base = _rotation(base_frame)
    assembly_axis = _unit(_matvec(child, local))
    base_axis = _unit(_matvec(_transpose(base), assembly_axis))

    named_motion = _motion_token(native_name)
    if named_motion is not None:
        motion = named_motion
        classification_source = "name"
        alignment = _dot(base_axis, _PRINCIPAL[motion])
        if abs(alignment) <= 1e-9:
            motion = None
    else:
        motion, alignment = _geometry_motion(base_axis, threshold)
        classification_source = "geometry" if motion is not None else "unresolved"

    if motion is None:
        return {
            "status": "unresolved",
            "motion": motion,
            "classification_source": classification_source,
            "alignment": alignment,
            "raw_base_axis": base_axis,
            "canonical_base_axis": base_axis,
            "delivered_local_axis": local,
            "sign": 1,
            "rule_id": "direction.positive_semantics",
            "rule_version": "v3",
        }

    sign = 1 if alignment >= 0.0 else -1
    canonical_base = [sign * value for value in base_axis]
    return {
        "status": "canonical",
        "motion": motion,
        "classification_source": classification_source,
        "alignment": alignment,
        "raw_base_axis": base_axis,
        "canonical_base_axis": canonical_base,
        "delivered_local_axis": [sign * value for value in local],
        "sign": sign,
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
