"""Pure conversion of SolidWorks ``IMathTransform.ArrayData`` facts."""

from __future__ import annotations

import math


def transform_from_solidworks(values) -> tuple[float, ...]:
    """Convert row-vector rotation, xyz, and scale to a URDF 4x4 matrix."""

    if (
        not isinstance(values, (list, tuple))
        or len(values) != 16
        or any(type(value) not in (int, float) for value in values)
    ):
        raise ValueError("expected 16 native numeric MathTransform values")
    raw = tuple(map(float, values))
    if len(raw) != 16 or not all(math.isfinite(value) for value in raw):
        raise ValueError("expected 16 finite MathTransform values")
    if abs(raw[12] - 1.0) > 1.0e-10:
        raise ValueError("scaled instances need explicit handling")
    rotation = [[raw[column * 3 + row] for column in range(3)] for row in range(3)]
    if any(
        abs(sum(rotation[row][axis] * rotation[column][axis] for axis in range(3)) - (1.0 if row == column else 0.0))
        > 1.0e-9
        for row in range(3)
        for column in range(3)
    ):
        raise ValueError("rotation is not orthonormal")
    determinant = (
        rotation[0][0] * (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1])
        - rotation[0][1] * (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0])
        + rotation[0][2] * (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0])
    )
    if abs(determinant - 1.0) > 1.0e-9:
        raise ValueError("reflection is not a rigid rotation")
    return (
        *(value for row_index, row in enumerate(rotation) for value in (*row, raw[9 + row_index])),
        0.0,
        0.0,
        0.0,
        1.0,
    )
