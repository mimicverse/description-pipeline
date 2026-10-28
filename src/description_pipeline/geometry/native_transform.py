"""Pure conversion of SolidWorks ``IMathTransform.ArrayData`` facts."""

from __future__ import annotations

import math

_SCALE_TOLERANCE = 1.0e-10
_RIGID_TOLERANCE = 1.0e-9


def _native_numbers(values) -> tuple[float, ...]:
    if (
        not isinstance(values, (list, tuple))
        or len(values) != 16
        or any(type(value) not in (int, float) for value in values)
    ):
        raise ValueError("expected 16 native numeric MathTransform values")
    raw = tuple(map(float, values))
    if not all(math.isfinite(value) for value in raw):
        raise ValueError("expected 16 finite MathTransform values")
    return raw


def rigid_matrix_from_row_major(values) -> tuple[float, ...]:
    """Return a row-major 4x4 proper rigid transform or raise ``ValueError``."""

    raw = _native_numbers(values)
    rows = [raw[index : index + 4] for index in range(0, 16, 4)]
    if any(abs(rows[3][index] - (1.0 if index == 3 else 0.0)) > _RIGID_TOLERANCE for index in range(4)):
        raise ValueError("scaled instances need explicit handling")
    rotation = [[rows[row][column] for column in range(3)] for row in range(3)]
    if any(
        abs(
            sum(rotation[row][axis] * rotation[column][axis] for axis in range(3))
            - (1.0 if row == column else 0.0)
        )
        > _RIGID_TOLERANCE
        for row in range(3)
        for column in range(3)
    ):
        raise ValueError("rotation is not orthonormal")
    determinant = (
        rotation[0][0] * (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1])
        - rotation[0][1] * (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0])
        + rotation[0][2] * (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0])
    )
    if abs(determinant - 1.0) > _RIGID_TOLERANCE:
        raise ValueError("reflection is not a rigid rotation")
    return raw


def transform_from_solidworks(values) -> tuple[float, ...]:
    """Convert row-vector rotation, xyz, and scale to a URDF 4x4 matrix."""

    raw = _native_numbers(values)
    if abs(raw[12] - 1.0) > _SCALE_TOLERANCE:
        raise ValueError("scaled instances need explicit handling")
    return rigid_matrix_from_row_major(
        (
            raw[0],
            raw[3],
            raw[6],
            raw[9],
            raw[1],
            raw[4],
            raw[7],
            raw[10],
            raw[2],
            raw[5],
            raw[8],
            raw[11],
            0.0,
            0.0,
            0.0,
            1.0,
        )
    )
