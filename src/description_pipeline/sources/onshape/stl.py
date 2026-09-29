"""来源侧网格读数：接受标准直接复用公共 ``description_pipeline.geometry.stl``。

这里不加第二套宽松解析：损坏、非有限坐标、残余顶点都由公共 ``StlError`` 报出；
本模块只补来源需要的字段（字节级 sha256、来源归属、与质量属性读数的对账值），
并把统计折成快照 ``geometry/parts.json`` 的形状。
"""

from __future__ import annotations

import hashlib

from description_pipeline.geometry.stl import MeshStats, StlError, read_bytes

__all__ = ["MeshStats", "StlError", "mesh_reading", "sha256_bytes"]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mesh_reading(
    data: bytes,
    *,
    element_id: str,
    part_id: str,
    body: dict,
    geometry_source: str,
) -> dict:
    """网格字节 → 快照读数；不可解析时抛公共 :class:`StlError`。"""

    stats: MeshStats = read_bytes(data)
    return {
        "element_id": element_id,
        "part_id": part_id,
        "sha256": sha256_bytes(data),
        "bytes": len(data),
        "geometry_source": geometry_source,
        "triangles": stats.triangles,
        "binary": stats.binary,
        "bounds_min": list(stats.low),
        "bounds_max": list(stats.high),
        "extent": list(stats.extent),
        "area_m2": stats.area,
        "mesh_volume_m3": stats.volume,
        "degenerate_triangles": stats.degenerate,
        "boundary_edges": stats.boundary_edges,
        "mesh_centroid": list(stats.com),
        "source_volume_m3": (body.get("volume") or [None])[0],
        "source_centroid": [float(value) for value in (body.get("centroid") or [])[:3]] or None,
    }
