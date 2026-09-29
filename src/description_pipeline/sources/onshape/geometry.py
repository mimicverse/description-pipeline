"""从零件工作室 GLTF 拆出每个零件的二进制 STL。

为什么不用 STL 导出端点：它 307 跳到签名 URL，浏览器会话下会被 CORS 拦；
GLTF 是同源 JSON，网格已在零件坐标系，可与质量属性的体积/质心严格配对。
"""

from __future__ import annotations

import base64
import json
import struct
from collections.abc import Iterable
from typing import cast


class GeometryError(RuntimeError):
    pass


def split_gltf(
    gltf: dict,
    mass_properties: dict[str, dict],
    *,
    tolerance: float = 0.05,
) -> tuple[dict[str, bytes], list[dict]]:
    """返回 ``({part_id: stl_bytes}, 未匹配清单)``。

    Part Studio 里可能含无体积的面体（``hasMass`` 为假），它们不会出现在装配体中，
    因此未匹配项交给调用方判断严重性。
    """

    buffer = _decode_buffer(gltf)
    meshes = [_mesh_properties(gltf, buffer, node) for node in gltf.get("nodes", [])]
    parts = [(part_id, body) for part_id, body in mass_properties.items() if body.get("volume")]
    scores: list[tuple[float, int, str]] = []
    for index, mesh in enumerate(meshes):
        if not mesh["volume"]:
            continue
        for part_id, body in parts:
            volume = float(body["volume"][0])
            if volume <= 0:
                continue
            relative = abs(mesh["volume"] - volume) / volume
            centroid = tuple(float(value) for value in body.get("centroid", (0.0, 0.0, 0.0))[:3])
            distance = sum((a - b) ** 2 for a, b in zip(mesh["centroid"], centroid, strict=True)) ** 0.5
            scores.append((relative + distance, index, part_id))

    used_meshes: set[int] = set()
    used_parts: set[str] = set()
    result: dict[str, bytes] = {}
    unresolved: list[dict] = []
    for score, index, part_id in sorted(scores, key=lambda item: item[0]):
        if index in used_meshes or part_id in used_parts:
            continue
        if score > tolerance:
            break
        used_meshes.add(index)
        used_parts.add(part_id)
        result[part_id] = _to_stl(meshes[index]["triangles"])
    for index, mesh in enumerate(meshes):
        if index not in used_meshes and mesh["volume"]:
            unresolved.append({"mesh": mesh["name"], "reason": "no_matching_part"})
    for part_id, body in parts:
        if part_id not in used_parts:
            unresolved.append(
                {
                    "part_id": part_id,
                    "volume_m3": float(body["volume"][0]),
                    "reason": "no_matching_mesh",
                }
            )
    return result, unresolved


def _decode_buffer(gltf: dict) -> bytes:
    buffers = gltf.get("buffers") or []
    if not buffers or "uri" not in buffers[0]:
        raise GeometryError("GLTF 缺少内嵌 buffer（data URI）")
    payload = buffers[0]["uri"].split(",", 1)[1]
    return base64.b64decode(payload)


def _accessor(gltf: dict, buffer: bytes, index: int):
    accessor = gltf["accessors"][index]
    view = gltf["bufferViews"][accessor["bufferView"]]
    offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    formats = {5126: "f", 5123: "H", 5125: "I"}
    components = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[accessor["type"]]
    count = accessor["count"]
    values = struct.unpack_from(f"<{count * components}{formats[accessor['componentType']]}", buffer, offset)
    if components == 1:
        return [[value] for value in values]
    return [values[i * components : (i + 1) * components] for i in range(count)]


def _mesh_properties(gltf: dict, buffer: bytes, node: dict) -> dict:
    primitive = gltf["meshes"][node["mesh"]]["primitives"][0]
    positions = _accessor(gltf, buffer, primitive["attributes"]["POSITION"])
    indices = _accessor(gltf, buffer, primitive["indices"])
    triangles: list[tuple[tuple[float, float, float], ...]] = []
    volume = 0.0
    weighted = [0.0, 0.0, 0.0]
    for offset in range(0, len(indices) - 2, 3):
        corners = [positions[indices[offset + k][0]] for k in range(3)]
        triangles.append(
            cast(
                tuple[tuple[float, float, float], ...],
                tuple(tuple(float(value) for value in corner) for corner in corners),
            )
        )
        a, b, c = corners
        cross = (
            b[1] * c[2] - b[2] * c[1],
            b[2] * c[0] - b[0] * c[2],
            b[0] * c[1] - b[1] * c[0],
        )
        tetra = (a[0] * cross[0] + a[1] * cross[1] + a[2] * cross[2]) / 6.0
        volume += tetra
        for axis in range(3):
            weighted[axis] += tetra * (a[axis] + b[axis] + c[axis]) / 4.0
    centroid = tuple(value / volume for value in weighted) if volume else (0.0, 0.0, 0.0)
    return {"name": node.get("name", ""), "triangles": triangles, "volume": volume, "centroid": centroid}


def _to_stl(triangles: Iterable[tuple]) -> bytes:
    records = list(triangles)
    out = bytearray(b"\0" * 80)
    out += struct.pack("<I", len(records))
    for a, b, c in records:
        u = tuple(b[i] - a[i] for i in range(3))
        v = tuple(c[i] - a[i] for i in range(3))
        normal = (
            u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0],
        )
        length = sum(value * value for value in normal) ** 0.5 or 1.0
        out += struct.pack("<3f", *(value / length for value in normal))
        for corner in (a, b, c):
            out += struct.pack("<3f", *corner)
        out += b"\0\0"
    return bytes(out)


def load_gltf(path) -> dict:
    return json.loads(open(path, encoding="utf-8").read())
