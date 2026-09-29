"""极简 4×4 刚体变换工具（纯标准库）：避免为几个矩阵运算引入第三方依赖。"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import cast

Matrix = tuple[tuple[float, float, float, float], ...]
Vector = Sequence[float]


def identity() -> Matrix:
    return (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def from_flat(values: Sequence[float]) -> Matrix:
    if len(values) != 16:
        raise ValueError("4×4 变换需要 16 个数")
    return cast(
        Matrix,
        tuple(tuple(float(values[row * 4 + col]) for col in range(4)) for row in range(4)),
    )


def from_axes(x: Vector, y: Vector, z: Vector, origin: Vector) -> Matrix:
    """x/y/z 为列向量（坐标系三轴），origin 为平移。"""

    return (
        (float(x[0]), float(y[0]), float(z[0]), float(origin[0])),
        (float(x[1]), float(y[1]), float(z[1]), float(origin[1])),
        (float(x[2]), float(y[2]), float(z[2]), float(origin[2])),
        (0.0, 0.0, 0.0, 1.0),
    )


def matmul(left: Matrix, right: Matrix) -> Matrix:
    return cast(
        Matrix,
        tuple(tuple(sum(left[row][k] * right[k][col] for k in range(4)) for col in range(4)) for row in range(4)),
    )


def translation(matrix: Matrix) -> tuple[float, float, float]:
    return (matrix[0][3], matrix[1][3], matrix[2][3])


def axis(matrix: Matrix, index: int = 2) -> tuple[float, float, float]:
    return (matrix[0][index], matrix[1][index], matrix[2][index])


def distance(first: Sequence[float], second: Sequence[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second, strict=True)))


def angle_deg(first: Vector, second: Vector, *, signed: bool = True) -> float:
    """两向量夹角（度）。``signed=False`` 时忽略方向，只看轴向是否共线。"""

    norm = math.sqrt(sum(v * v for v in first)) * math.sqrt(sum(v * v for v in second))
    if norm == 0:
        return 0.0
    cosine = sum(a * b for a, b in zip(first, second, strict=True)) / norm
    cosine = max(-1.0, min(1.0, cosine if signed else abs(cosine)))
    return math.degrees(math.acos(cosine))


def rotation_rpy(roll: float, pitch: float, yaw: float) -> Matrix:
    """URDF 约定的固定轴 X-Y-Z 旋转。"""

    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, 0.0),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, 0.0),
        (-sp, cp * sr, cp * cr, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def rotation_difference_deg(left: Matrix, right: Matrix) -> float:
    """两个旋转矩阵之间的夹角（度），用于独立核验 MJCF/URDF 姿态。"""

    trace = sum(
        sum(left[k][i] * right[k][j] for k in range(3)) * (1.0 if i == j else 0.0) for i in range(3) for j in range(3)
    )
    cosine = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
    return math.degrees(math.acos(cosine))
