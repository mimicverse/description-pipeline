"""复合刚体几何量：把各网格的均匀密度惯量合成到 link 坐标系与质心。

用途是与 URDF 里由 CAD/材质给出的惯量做交叉核对（``URDF310``/``URDF311``）：
两者应量级一致、主轴方向接近；差异大说明质量归属、坐标系或单位有问题。
"""

from __future__ import annotations

import math
from typing import cast

from . import model as model_module

Matrix = tuple[tuple[float, float, float], ...]
Vector = tuple[float, float, float]


def identity() -> Matrix:
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def matmul(left: Matrix, right: Matrix) -> Matrix:
    return cast(
        Matrix,
        tuple(tuple(sum(left[row][k] * right[k][column] for k in range(3)) for column in range(3)) for row in range(3)),
    )


def transpose(matrix: Matrix) -> Matrix:
    return cast(Matrix, tuple(tuple(matrix[column][row] for column in range(3)) for row in range(3)))


def matvec(matrix: Matrix, vector) -> Vector:
    return cast(Vector, tuple(sum(matrix[row][k] * vector[k] for k in range(3)) for row in range(3)))


def scale_matrix(scale) -> Matrix:
    return ((scale[0], 0.0, 0.0), (0.0, scale[1], 0.0), (0.0, 0.0, scale[2]))


def determinant(matrix: Matrix) -> float:
    return (
        matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
        - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
        + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0])
    )


def outer(vector) -> Matrix:
    return cast(Matrix, tuple(tuple(vector[row] * vector[column] for column in range(3)) for row in range(3)))


def trace(matrix: Matrix) -> float:
    return matrix[0][0] + matrix[1][1] + matrix[2][2]


def add(*matrices: Matrix) -> Matrix:
    return cast(
        Matrix,
        tuple(tuple(sum(matrix[row][column] for matrix in matrices) for column in range(3)) for row in range(3)),
    )


def subtract(left: Matrix, right: Matrix) -> Matrix:
    return cast(
        Matrix,
        tuple(tuple(left[row][column] - right[row][column] for column in range(3)) for row in range(3)),
    )


def scale(matrix: Matrix, factor: float) -> Matrix:
    return cast(Matrix, tuple(tuple(value * factor for value in row) for row in matrix))


def from_second_moment(second: Matrix) -> Matrix:
    """惯量张量 I = tr(C)·E − C（C 是 ∫ x xᵀ dV）。"""

    return subtract(scale(identity(), trace(second)), second)


def link_mesh_inertia(link, stats_for) -> dict | None:
    """把 link 的所有网格合成为一个均匀密度刚体。

    返回 ``{"volume", "com", "inertia_about_com"}``；``inertia_about_com`` 对应
    "质量 = 体积" 的等效刚体（调用方按实际质量线性缩放）。没有可用网格时返回 None。
    """

    parts = []
    for mesh in link.meshes:
        if mesh.path is None:
            continue
        stats = stats_for(mesh.path)
        if stats is None or stats.volume <= 0:
            continue
        linear = matmul(model_module.rpy_matrix(mesh.rpy), scale_matrix(mesh.scale))
        jacobian = abs(determinant(linear))
        volume = stats.volume * jacobian
        second = _matrix(stats.second_moment)
        com = matvec(linear, stats.com)
        offset = mesh.origin
        # x' = L x + t：∫ x' x'ᵀ dV' = det(L)·[L C Lᵀ + L c tᵀ V + t cᵀ Lᵀ V + t tᵀ V]
        moved = add(
            matmul(matmul(linear, second), transpose(linear)),
            _cross_terms(com, offset, stats.volume),
            scale(outer(offset), stats.volume),
        )
        moved = scale(moved, jacobian)
        parts.append(
            {
                "volume": volume,
                "com": tuple(com[axis] + offset[axis] for axis in range(3)),
                "second": moved,
            }
        )
    if not parts:
        return None

    total_volume = sum(part["volume"] for part in parts)
    if total_volume <= 0:
        return None
    com = tuple(sum(part["volume"] * part["com"][axis] for part in parts) / total_volume for axis in range(3))
    second = add(*(part["second"] for part in parts))
    # 平移到质心：C_com = C_origin − V·com comᵀ
    second_com = subtract(second, scale(outer(com), total_volume))
    return {
        "volume": total_volume,
        "com": com,
        "inertia_about_com": from_second_moment(second_com),
    }


def _cross_terms(com, offset, volume) -> Matrix:
    return scale(add(outer_pair(com, offset), outer_pair(offset, com)), volume)


def outer_pair(left, right) -> Matrix:
    return cast(Matrix, tuple(tuple(left[row] * right[column] for column in range(3)) for row in range(3)))


def _matrix(values: tuple[float, ...]) -> Matrix:
    return (
        (values[0], values[1], values[2]),
        (values[3], values[4], values[5]),
        (values[6], values[7], values[8]),
    )


def compare_inertia(urdf_inertia: Matrix, mass: float, mesh: dict) -> dict:
    """比较 URDF 惯量与"均匀密度网格惯量（按实际质量缩放）"。

    返回主惯量比、最大主惯量轴夹角与各自的回转半径。
    """

    density = mass / mesh["volume"]
    scaled = scale(mesh["inertia_about_com"], density)
    urdf_values = eigenvalues_symmetric(urdf_inertia) or [0.0, 0.0, 0.0]
    mesh_values = eigenvalues_symmetric(scaled) or [0.0, 0.0, 0.0]
    ratio = urdf_values[2] / mesh_values[2] if mesh_values[2] > 0 else float("inf")
    urdf_axis = _principal_axis(urdf_inertia)
    mesh_axis = _principal_axis(scaled)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, abs(sum(urdf_axis[i] * mesh_axis[i] for i in range(3)))))))
    return {
        "ratio": ratio,
        "axis_angle_deg": angle,
        "urdf_gyration": math.sqrt(urdf_values[2] / mass) if mass > 0 else 0.0,
        "mesh_gyration": math.sqrt(mesh_values[2] / mass) if mass > 0 else 0.0,
        "com_offset": math.dist(mesh["com"], (0.0, 0.0, 0.0)),
    }


def _principal_axis(matrix: Matrix) -> Vector:
    """最大特征值对应的特征向量（幂迭代，零依赖）。"""

    vector: Vector = (1.0, 1.0, 1.0)
    for _ in range(64):
        product = matvec(matrix, vector)
        norm = math.sqrt(sum(value * value for value in product))
        if norm <= 0:
            return (0.0, 0.0, 0.0)
        vector = cast(Vector, tuple(value / norm for value in product))
    return vector


def eigenvalues_symmetric(matrix) -> list[float] | None:
    """3×3 对称矩阵特征值（Jacobi 迭代，不依赖 numpy）。"""

    a = [list(row) for row in matrix]
    if any(not math.isfinite(value) for row in a for value in row):
        return None
    for _ in range(64):
        off = max(abs(a[0][1]), abs(a[0][2]), abs(a[1][2]))
        scale_value = max(abs(a[i][i]) for i in range(3)) + off
        if off <= 1e-12 * max(scale_value, 1e-300):
            break
        for p, q in ((0, 1), (0, 2), (1, 2)):
            if abs(a[p][q]) < 1e-300:
                continue
            theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
            sign = 1.0 if theta >= 0 else -1.0
            t = sign / (abs(theta) + math.sqrt(theta * theta + 1))
            c, s = 1 / math.sqrt(t * t + 1), t / math.sqrt(t * t + 1)
            for k in range(3):
                akp, akq = a[k][p], a[k][q]
                a[k][p] = c * akp - s * akq
                a[k][q] = s * akp + c * akq
            for k in range(3):
                apk, aqk = a[p][k], a[q][k]
                a[p][k] = c * apk - s * aqk
                a[q][k] = s * apk + c * aqk
    return sorted(a[i][i] for i in range(3))
