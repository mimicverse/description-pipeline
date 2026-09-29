"""Blocking uniform-density geometry oracle for links that explicitly claim it.

`model/robot.json` 里 `links[].provenance.inertia_model == "uniform_density_visual"` 是**显式声明**：
该 link 的惯量应当等于"把它的可视几何当作均匀密度刚体"的结果。`urdf_quality.rules` 的
URDF310/311 只对网格做近似的主惯量比/主轴比较，primitive 或不可用网格会被静默跳过；本模块把
这一档补成"声明了就必须有结论"，并且用**完整张量**在同一参考点坐标系里比较：

* 几何独立计算：网格走公共 :mod:`description_pipeline.geometry.stl` 的读数（体积、质心、∫xxᵀdV），
  再按 ``x' = R·S·x + t`` 施加旋转与缩放（``μ' = det(S)·L μ Lᵀ + 平移项``，``L = R·S``）；
  box/sphere/cylinder 用解析惯量（只接受均匀缩放，非均匀缩放的形状语义未定义 → ``not_run``）。
* 表达系：模型侧的 ``inertial.inertia`` 是相对 ``inertial.rpy`` 描述的惯量系，比较前先转到 link 坐标系：
  ``I_link = R(rpy)·I·R(rpy)ᵀ``；质心 ``inertial.xyz`` 本就在 link 坐标系。
* 判据：完整张量在**同一参考点（各自质心）**下的误差
  ``‖I_model − ρ·I_geometry‖_F ≤ inertia_atol + uniform_density_rtol·‖ρ·I_geometry‖_F``
  且 ``‖com_model − com_geometry‖ ≤ uniform_density_com_atol_m``；容差全部写进证据，便于复核。
  用范数而不是主惯量比/主轴夹角，避免近重根时主轴不唯一导致的假通过或假失败。
* 重叠：多视觉实体可能重叠，除非作者显式声明 ``provenance.uniform_density_overlap == "disjoint"``，
  否则记 ``not_run``——不把外壳/重叠 visual 当实心材料。
* 质量是**基准**：由 ``density = mass / volume`` 反推等效密度；这里只校验惯量与几何形状是否自洽，
  真实质量分布属于独立实物证据，不在本模块结论内。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from ..geometry import stl
from ..io import confined

CLAIM = "uniform_density_visual"
OVERLAP_DECLARATION = "uniform_density_overlap"
DISJOINT = "disjoint"
# Defaults match the versioned build profile.
DEFAULT_RTOL = 1e-3
DEFAULT_COM_ATOL_M = 1e-6
DEFAULT_INERTIA_ATOL = 1e-10
PRIMITIVE_KINDS = {"box", "sphere", "cylinder"}


def _rotation(rpy) -> np.ndarray:
    roll, pitch, yaw = (float(value) for value in rpy)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )


def _tensor_from_six(values) -> np.ndarray:
    """模型里的惯量是 6 分量 [ixx, ixy, ixz, iyy, iyz, izz]。"""

    ixx, ixy, ixz, iyy, iyz, izz = (float(value) for value in values)
    return np.array([[ixx, ixy, ixz], [ixy, iyy, iyz], [ixz, iyz, izz]])


def _second_moment_to_inertia(second: np.ndarray) -> np.ndarray:
    """I = tr(μ)·E − μ（μ = ∫ x xᵀ dV）。"""

    return float(np.trace(second)) * np.eye(3) - second


def _inertia_to_second_moment(inertia: np.ndarray) -> np.ndarray:
    """μ = (tr(I)/2)·E − I（逆变换，用于把解析惯量折算回统一累加路径）。"""

    return 0.5 * float(np.trace(inertia)) * np.eye(3) - inertia


def _mesh(geom: dict, root: Path) -> tuple[dict | None, str | None]:
    try:
        path = confined(root, geom["filename"])
    except ValueError as error:
        return None, f"网格路径不可用：{error}"
    try:
        stats = stl.read(path)
    except stl.StlError as error:
        return None, f"网格不可解析：{error}"
    if stats.triangles == 0:
        return None, "网格没有三角面"
    if stats.boundary_edges:
        return None, f"网格不水密（{stats.boundary_edges} 条边未被两个面共享），体积/二阶矩不可信"
    if stats.volume <= 0:
        return None, "网格体积为零"
    return {
        "volume": float(stats.volume),
        "com": np.asarray(stats.com, dtype=float),
        "second": np.asarray(stats.second_moment, dtype=float).reshape(3, 3),
    }, None


def _primitive(geom: dict) -> tuple[dict | None, str | None]:
    """解析惯量（关于形状自身质心）；只接受均匀缩放。"""

    scale = np.asarray(geom.get("scale", (1.0, 1.0, 1.0)), dtype=float)
    if not np.allclose(scale, scale[0], rtol=0.0, atol=1e-12):
        return None, f"{geom['kind']} 的非均匀缩放没有定义形状语义"
    factor = float(scale[0])
    kind = geom["kind"]
    if kind == "box":
        x, y, z = (float(value) * factor for value in geom["size"])
        volume = x * y * z
        inertia = volume / 12.0 * np.diag([y * y + z * z, x * x + z * z, x * x + y * y])
    elif kind == "sphere":
        radius = float(geom["radius"]) * factor
        volume = 4.0 / 3.0 * math.pi * radius**3
        inertia = 2.0 / 5.0 * volume * radius**2 * np.eye(3)
    elif kind == "cylinder":
        radius = float(geom["radius"]) * factor
        length = float(geom["length"]) * factor
        volume = math.pi * radius**2 * length
        axial = 0.5 * volume * radius**2
        radial = volume * (radius**2 / 4.0 + length**2 / 12.0)
        inertia = np.diag([radial, radial, axial])
    else:  # pragma: no cover - 调用方已按 PRIMITIVE_KINDS 过滤
        return None, f"不支持的几何类型：{kind}"
    return {"volume": volume, "com": np.zeros(3), "inertia": inertia}, None


def _contribution(geom: dict, root: Path) -> tuple[dict | None, str | None]:
    """单个 visual → link 坐标系下的 (体积, 质心, 关于自身质心的惯量)。"""

    rotation = _rotation(geom.get("rpy", (0.0, 0.0, 0.0)))
    scale = np.asarray(geom.get("scale", (1.0, 1.0, 1.0)), dtype=float)
    offset = np.asarray(geom.get("xyz", (0.0, 0.0, 0.0)), dtype=float)
    if geom["kind"] == "mesh":
        item, reason = _mesh(geom, root)
        if item is None:
            return None, reason
        linear = rotation @ np.diag(scale)
        determinant = float(np.linalg.det(np.diag(scale)))
        volume = item["volume"] * determinant
        if volume <= 0:
            return None, "缩放后体积非正（scale 含 0 或负值）"
        com = linear @ item["com"] + offset
        # μ' = det(S)·[L μ Lᵀ + L c tᵀ V + t cᵀ Lᵀ V + t tᵀ V]，L = R·S
        second = determinant * (
            linear @ item["second"] @ linear.T
            + np.outer(linear @ item["com"], offset) * item["volume"]
            + np.outer(offset, linear @ item["com"]) * item["volume"]
            + np.outer(offset, offset) * item["volume"]
        )
        centered = second - volume * np.outer(com, com)
        inertia = _second_moment_to_inertia(centered)
    elif geom["kind"] in PRIMITIVE_KINDS:
        item, reason = _primitive(geom)
        if item is None:
            return None, reason
        volume = item["volume"]
        com = offset
        inertia = rotation @ item["inertia"] @ rotation.T
    else:
        return None, f"不支持的几何类型：{geom['kind']}"
    return {"volume": volume, "com": com, "inertia": inertia}, None


def _synthesis(link: dict, root: Path) -> tuple[dict | None, str | None]:
    visuals = list(link.get("visuals", []))
    if not visuals:
        return None, "声明了均匀密度但没有可视几何"
    declared = link.get("provenance", {}).get(OVERLAP_DECLARATION)
    if len(visuals) > 1 and declared != DISJOINT:
        return None, (f'多个视觉实体可能重叠，未声明互不重叠（provenance.{OVERLAP_DECLARATION} = "{DISJOINT}"）')
    parts: list[dict] = []
    for geom in visuals:
        item, reason = _contribution(geom, root)
        if item is None:
            return None, reason
        parts.append(item)
    volume = float(sum(part["volume"] for part in parts))
    if volume <= 0:
        return None, "可视几何总体积为零"
    com = sum((part["volume"] * part["com"] for part in parts), np.zeros(3)) / volume
    # 统一累加路径：各分量先折算成关于 link 原点的 ∫xxᵀdV，再整体搬到质心。
    second_origin = np.zeros((3, 3))
    for part in parts:
        second_origin += _inertia_to_second_moment(part["inertia"]) + part["volume"] * np.outer(
            part["com"], part["com"]
        )
    centered = second_origin - volume * np.outer(com, com)
    return {"volume": volume, "com": com, "inertia": _second_moment_to_inertia(centered)}, None


def uniform_density_evidence(root: Path, data: dict, profile: dict) -> list[dict[str, Any]]:
    """逐 link 的均匀密度几何证据；``passed`` 才是通过，其余状态必须显式处理。"""

    rtol = float(profile.get("uniform_density_rtol", DEFAULT_RTOL))
    com_atol = float(profile.get("uniform_density_com_atol_m", DEFAULT_COM_ATOL_M))
    inertia_atol = float(profile.get("inertia_atol", DEFAULT_INERTIA_ATOL))
    root = Path(root)
    evidence: list[dict[str, Any]] = []
    for link in data.get("links", []):
        claimed = link.get("provenance", {}).get("inertia_model") == CLAIM
        entry: dict[str, Any] = {"object": link["name"], "claimed": claimed}
        if not claimed:
            evidence.append({**entry, "status": "not_applicable", "reason": "未声明 uniform_density_visual"})
            continue
        inertial = link.get("inertial")
        if inertial is None or float(inertial.get("mass", 0.0)) <= 0:
            evidence.append({**entry, "status": "not_run", "reason": "缺少正质量，无法据此推出等效密度"})
            continue
        synthesis, reason = _synthesis(link, root)
        if synthesis is None:
            evidence.append({**entry, "status": "not_run", "reason": reason})
            continue
        mass = float(inertial["mass"])
        density = mass / synthesis["volume"]
        rotation = _rotation(inertial.get("rpy", (0.0, 0.0, 0.0)))
        model_tensor = rotation @ _tensor_from_six(inertial["inertia"]) @ rotation.T
        expected_tensor = density * synthesis["inertia"]
        tensor_error = float(np.linalg.norm(model_tensor - expected_tensor))
        tensor_tolerance = inertia_atol + rtol * float(np.linalg.norm(expected_tensor))
        declared_com = np.asarray(inertial.get("xyz", (0.0, 0.0, 0.0)), dtype=float)
        com_error = float(np.linalg.norm(declared_com - synthesis["com"]))
        passed = tensor_error <= tensor_tolerance and com_error <= com_atol
        evidence.append(
            {
                **entry,
                "status": "passed" if passed else "failed",
                "density_kg_m3": density,
                "volume_m3": synthesis["volume"],
                "tensor_error": tensor_error,
                "com_error_m": com_error,
                "geometry_com_m": synthesis["com"].tolist(),
                "model_tensor": model_tensor.tolist(),
                "expected_tensor": expected_tensor.tolist(),
                "tolerances": {
                    "inertia_atol": inertia_atol,
                    "uniform_density_rtol": rtol,
                    "uniform_density_com_atol_m": com_atol,
                    "tensor_tolerance": tensor_tolerance,
                },
                "overlap_declared": link.get("provenance", {}).get(OVERLAP_DECLARATION),
                "geometry": [geom["kind"] for geom in link.get("visuals", [])],
            }
        )
    return evidence


def summarize(evidence: list[dict]) -> dict:
    """调用方组装 ``result`` 用的计数：只有"有适用对象且全部 passed"才算 ok。"""

    by_status: dict[str, list[str]] = {}
    for item in evidence:
        by_status.setdefault(item["status"], []).append(item["object"])
    applicable = [item["object"] for item in evidence if item["status"] != "not_applicable"]
    return {
        "applicable": sorted(applicable),
        "passed": sorted(by_status.get("passed", [])),
        "failed": sorted(by_status.get("failed", [])),
        "not_run": sorted(by_status.get("not_run", [])),
        "not_applicable": sorted(by_status.get("not_applicable", [])),
        "ok": bool(applicable) and len(by_status.get("passed", [])) == len(applicable),
    }
