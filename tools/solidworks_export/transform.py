"""Rigid-body transform math.

Internal matrices are flat 16-tuples in ordinary row-major 4x4 order. This is
NOT SolidWorks MathTransform.ArrayData; convert that at the API boundary.
Rotations follow the URDF convention for
``origin.rpy``: extrinsic X-Y-Z, i.e. ``R = Rz(yaw) @ Ry(pitch) @ Rx(roll)``.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence, Tuple

Matrix = Tuple[float, ...]
Vector3 = Tuple[float, float, float]


def identity() -> Matrix:
    return (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def at(m: Sequence[float], row: int, col: int) -> float:
    return float(m[row * 4 + col])


def matmul(a: Sequence[float], b: Sequence[float]) -> Matrix:
    out = []
    for r in range(4):
        for c in range(4):
            out.append(sum(float(a[r * 4 + k]) * float(b[k * 4 + c]) for k in range(4)))
    return tuple(out)


def inverse_rigid(m: Sequence[float]) -> Matrix:
    """Inverse of a rigid transform (rotation + translation only)."""

    rot = [[float(m[r * 4 + c]) for c in range(3)] for r in range(3)]
    trans = [float(m[r * 4 + 3]) for r in range(3)]
    rt = [[rot[c][r] for c in range(3)] for r in range(3)]
    t_inv = [-sum(rt[r][c] * trans[c] for c in range(3)) for r in range(3)]
    return (
        rt[0][0],
        rt[0][1],
        rt[0][2],
        t_inv[0],
        rt[1][0],
        rt[1][1],
        rt[1][2],
        t_inv[1],
        rt[2][0],
        rt[2][1],
        rt[2][2],
        t_inv[2],
        0.0,
        0.0,
        0.0,
        1.0,
    )


def rotation_3x3(rpy: Sequence[float]):
    """3x3 rotation from extrinsic X-Y-Z rpy (URDF convention)."""

    roll, pitch, yaw = (float(v) for v in rpy)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def rpy_from_rotation(rot: Sequence[Sequence[float]]) -> Vector3:
    """Extract extrinsic X-Y-Z rpy from a 3x3 rotation matrix."""

    r20 = max(-1.0, min(1.0, float(rot[2][0])))
    pitch = math.asin(-r20)
    if abs(r20) > 1.0 - 1e-12:
        roll = 0.0
        yaw = math.atan2(-float(rot[0][1]), float(rot[1][1]))
    else:
        roll = math.atan2(float(rot[2][1]), float(rot[2][2]))
        yaw = math.atan2(float(rot[1][0]), float(rot[0][0]))
    return (roll, pitch, yaw)


def rotation_3x3_of(m: Sequence[float]):
    return tuple(tuple(float(m[r * 4 + c]) for c in range(3)) for r in range(3))


def from_xyz_rpy(xyz: Sequence[float], rpy: Sequence[float]) -> Matrix:
    rot = rotation_3x3(rpy)
    x, y, z = (float(v) for v in xyz)
    return (
        rot[0][0],
        rot[0][1],
        rot[0][2],
        x,
        rot[1][0],
        rot[1][1],
        rot[1][2],
        y,
        rot[2][0],
        rot[2][1],
        rot[2][2],
        z,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def xyz_rpy_of(m: Sequence[float]):
    xyz = (float(m[3]), float(m[7]), float(m[11]))
    rpy = rpy_from_rotation(rotation_3x3_of(m))
    return xyz, rpy


def apply_point(m: Sequence[float], p: Sequence[float]) -> Vector3:
    x, y, z = (float(v) for v in p)
    return (
        float(m[0]) * x + float(m[1]) * y + float(m[2]) * z + float(m[3]),
        float(m[4]) * x + float(m[5]) * y + float(m[6]) * z + float(m[7]),
        float(m[8]) * x + float(m[9]) * y + float(m[10]) * z + float(m[11]),
    )


def between(parent_world: Sequence[float], child_world: Sequence[float]) -> Matrix:
    """Relative transform ``inverse(parent_world) @ child_world``."""

    return matmul(inverse_rigid(parent_world), child_world)


def flatten(value: Iterable) -> Matrix:
    """Coerce a nested or flat 16-value sequence into a flat 16-tuple.

    SolidWorks COM returns transforms either as flat arrays or nested
    ``(rows, cols)`` tuples depending on the API; accept both.
    """

    items = list(value)
    if len(items) == 4 and all(hasattr(row, "__len__") for row in items):
        flat: list[float] = []
        for row in items:
            flat.extend(float(v) for v in row)
        if len(flat) == 16:
            return tuple(flat)
    flat = [float(v) for v in items]
    if len(flat) != 16:
        raise ValueError(f"expected a 4x4 transform, got {len(flat)} values")
    return tuple(flat)


def approx_equal(a: Sequence[float], b: Sequence[float], tol: float = 1e-9) -> bool:
    return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b, strict=True))
