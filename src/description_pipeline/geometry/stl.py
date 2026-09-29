"""STL 读取（二进制/ASCII）与几何统计：包围盒、体积、面积、退化面。

只做质检需要的一次流式扫描，不缓存全部三角形；文件损坏或超限时抛
``StlError``，由调用方转成质检结论而不是崩溃。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import cast

MAX_TRIANGLES = 5_000_000
DEGENERATE_AREA = 1.0e-14  # m²，约 (0.1 µm)²


class StlError(ValueError):
    pass


@dataclass(frozen=True)
class MeshStats:
    triangles: int
    low: tuple[float, float, float]
    high: tuple[float, float, float]
    volume: float  # m³，封闭网格的有向体积绝对值
    signed_volume: float  # m³，带符号（负值说明法线整体朝内）
    area: float  # m²
    degenerate: int
    binary: bool
    com: tuple[float, float, float]  # 均匀密度质心（网格坐标系）
    second_moment: tuple[float, ...]  # ∫ x xᵀ dV，单位密度，9 个分量
    boundary_edges: int  # 未被恰好两个三角面共享的边（CAD 导出的 T 型接缝）

    @property
    def extent(self) -> tuple[float, float, float]:
        return cast(
            tuple[float, float, float],
            tuple(self.high[axis] - self.low[axis] for axis in range(3)),
        )

    @property
    def half_diagonal(self) -> float:
        return 0.5 * math.dist(self.low, self.high)

    @property
    def center(self) -> tuple[float, float, float]:
        return cast(
            tuple[float, float, float],
            tuple((self.low[axis] + self.high[axis]) / 2 for axis in range(3)),
        )


def read(path: Path) -> MeshStats:
    """读取 STL 并统计。文件不存在/不可解析/超限时抛 :class:`StlError`。"""

    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise StlError(f"无法读取：{error}") from error
    return read_bytes(data)


def read_bytes(data: bytes) -> MeshStats:
    """Use the identical strict acceptance contract for file and source-adapter bytes."""
    if len(data) < 15:
        raise StlError("文件过短，不是有效 STL")
    if len(data) >= 84 and len(data) == 84 + 50 * struct.unpack("<I", data[80:84])[0]:
        return _read_binary(data)
    return _read_ascii(data)


def vertices(path: Path):
    """Return triangle vertices after the same strict format validation."""
    import numpy as np

    data = Path(path).read_bytes()
    stats = read_bytes(data)
    if stats.binary:
        records = np.frombuffer(
            data,
            dtype=np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")]),
            count=stats.triangles,
            offset=84,
        )
        return records["vertices"].reshape(-1, 3).astype(float)
    return np.array(
        [
            [float(value) for value in fields[1:]]
            for line in data.decode("ascii", errors="ignore").splitlines()
            if len(fields := line.split()) == 4 and fields[0] == "vertex"
        ]
    )


def _read_binary(data: bytes) -> MeshStats:
    count = struct.unpack("<I", data[80:84])[0]
    if count == 0:
        raise StlError("二进制 STL 没有三角面")
    if count > MAX_TRIANGLES:
        raise StlError(f"三角面数 {count} 超过上限 {MAX_TRIANGLES}")
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    volume = 0.0
    area = 0.0
    degenerate = 0
    moment = [0.0] * 9
    centroid = [0.0] * 3
    edges: dict[tuple, int] = {}
    for index in range(count):
        offset = 84 + 50 * index + 12
        corners = [struct.unpack_from("<3f", data, offset + vertex * 12) for vertex in range(3)]
        for corner in corners:
            for axis in range(3):
                value = corner[axis]
                if not math.isfinite(value):
                    raise StlError(f"第 {index + 1} 个三角面含非有限坐标")
                low[axis] = min(low[axis], value)
                high[axis] = max(high[axis], value)
        triangle_area, signed = _triangle(corners)
        area += triangle_area
        volume += signed
        _accumulate(corners, signed, moment, centroid, edges)
        if triangle_area <= DEGENERATE_AREA:
            degenerate += 1
    return _finish(count, low, high, volume, area, degenerate, True, moment, centroid, edges)


def _read_ascii(data: bytes) -> MeshStats:
    text = data.decode("ascii", errors="ignore")
    if "vertex" not in text or not text.lstrip().lower().startswith("solid"):
        raise StlError("既不是二进制 STL，也不是带 solid 头的 ASCII STL")
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    volume = 0.0
    area = 0.0
    degenerate = 0
    count = 0
    moment = [0.0] * 9
    centroid = [0.0] * 3
    edges: dict[tuple, int] = {}
    corners: list[tuple[float, float, float]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0] == "vertex":
            try:
                corner = tuple(float(value) for value in fields[1:])
            except ValueError as error:
                raise StlError(f"顶点坐标无法解析：{line.strip()}") from error
            if not all(math.isfinite(value) for value in corner):
                raise StlError(f"顶点坐标非有限：{line.strip()}")
            corners.append(corner)  # type: ignore[arg-type]
            for axis in range(3):
                low[axis] = min(low[axis], corner[axis])
                high[axis] = max(high[axis], corner[axis])
            if len(corners) == 3:
                count += 1
                if count > MAX_TRIANGLES:
                    raise StlError(f"三角面数超过上限 {MAX_TRIANGLES}")
                triangle_area, signed = _triangle(corners)
                area += triangle_area
                volume += signed
                _accumulate(corners, signed, moment, centroid, edges)
                if triangle_area <= DEGENERATE_AREA:
                    degenerate += 1
                corners = []
    if count == 0 or corners:
        raise StlError("ASCII STL 的顶点数不是 3 的倍数或没有三角面")
    return _finish(count, low, high, volume, area, degenerate, False, moment, centroid, edges)


def _accumulate(corners, signed: float, moment: list, centroid: list, edges: dict) -> None:
    """累加单位密度下的体积矩、质心与边表（有向体积的符号处理朝向）。"""

    if abs(signed) > 1.0e-18:
        # 四面体 (0, a, b, c)：∫ x xᵀ dV = V/20 · A (I + J) Aᵀ，A = [a b c]
        for row in range(3):
            for column in range(3):
                value = 0.0
                for k in range(3):
                    for other in range(3):
                        weight = 2.0 if k == other else 1.0
                        value += corners[k][row] * corners[other][column] * weight
                moment[row * 3 + column] += signed / 20.0 * value
        for axis in range(3):
            centroid[axis] += signed * (corners[0][axis] + corners[1][axis] + corners[2][axis]) / 4.0
    for first, second in ((0, 1), (1, 2), (2, 0)):
        key = tuple(sorted((_rounded(corners[first]), _rounded(corners[second]))))
        edges[key] = edges.get(key, 0) + 1


def _rounded(point):
    return tuple(round(value, 6) for value in point)


def _finish(count, low, high, volume, area, degenerate, binary, moment, centroid, edges) -> MeshStats:
    sign = 1.0 if volume >= 0 else -1.0
    magnitude = abs(volume)
    if magnitude > 1.0e-15:
        com = tuple(sign * value / magnitude for value in centroid)
        moment = [sign * value for value in moment]
    else:
        com = tuple((low[axis] + high[axis]) / 2 for axis in range(3))
        moment = [0.0] * 9
    return MeshStats(
        triangles=count,
        low=tuple(low),
        high=tuple(high),
        volume=abs(volume),
        signed_volume=volume,
        area=area,
        degenerate=degenerate,
        binary=binary,
        com=com,
        second_moment=tuple(moment),
        boundary_edges=sum(1 for shared in edges.values() if shared != 2),
    )


def _triangle(corners) -> tuple[float, float]:
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = corners
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    area = 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)
    signed = (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
    return area, signed
