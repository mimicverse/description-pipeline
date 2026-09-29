"""合成 Onshape 响应：只用于结构断言与错误注入。

真实回放用仓库内夹具 ``tests/fixtures/onshape/cache``（2026-09-17 从 Onshape API
抓取的缓存）。合成数据覆盖分支与错误路径，不作为真实 CAD 证据。
"""

from __future__ import annotations

import json
from pathlib import Path

from description_pipeline.sources.onshape.errors import API_ERROR, OnshapeSourceError

DOCUMENT_ID = "doc0001"
WORKSPACE_ID = "ws0001"
ROOT_ELEMENT = "root0001"
SUBASSEMBLY = "sub0001"
SUB_MICROVERSION = "mv-root"  # 同一文档的修订：所有实例必须一致
STUDIO_A = "studioA0001"
STUDIO_B = "studioB0001"
URL = f"https://cad.onshape.com/documents/{DOCUMENT_ID}/w/{WORKSPACE_ID}/e/{ROOT_ELEMENT}"
FETCHED_AT = "2026-01-02T03:04:05Z"

# 真实装配响应里 occurrences[].transform 是行主序 4×4（平移落在 3/7/11 位）
IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
PART_A = "PART_A"
PART_L = "PART_L"
# 真实零件 ID 会出现 "/"（Onshape 的保险丝式 ID），缓存与快照都要净化成 PART_B
PART_B = "PART/B"
PART_N = "PART_N"


def translate(x: float, y: float, z: float) -> list[float]:
    return [1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z, 0, 0, 0, 1]


def binary_stl(vertices=((-1.0, 0.0, -1.0), (1.0, 0.0, -1.0), (0.0, 0.0, 1.0))) -> bytes:
    """最小合法二进制 STL（单个三角形），用于几何读数与摘要证据。"""

    import struct

    payload = b"mimicverse-synthetic-fixture".ljust(80, b"\0") + struct.pack("<I", 1)
    payload += struct.pack("<3f", 0.0, 0.0, 1.0)
    for vertex in vertices:
        payload += struct.pack("<3f", *vertex)
    return payload + struct.pack("<H", 0)


def safe_name(part_id: str) -> str:
    """与缓存布局一致的净化规则；测试里显式写出来，避免和被测算代码同源。"""

    return "".join(char if char.isalnum() else "_" for char in part_id)


def _range(value: float, delta: float = 0.0) -> list[float]:
    return [value, value - delta, value + delta]


def _centroid(values, delta: float = 1e-6) -> list[float]:
    x, y, z = values
    return [x, y, z, x - delta, y - delta, z - delta, x + delta, y + delta, z + delta]


def _inertia(diagonal) -> list[float]:
    ixx, iyy, izz = diagonal
    return [ixx, 0.0, 0.0, 0.0, iyy, 0.0, 0.0, 0.0, izz] * 3


def body(
    mass: float,
    *,
    centroid=(0.0, 0.0, 0.0),
    diagonal=(1e-4, 1e-4, 1e-4),
) -> dict:
    """真实响应的读数形状：每个标量三段（标称/下界/上界），张量连写三遍。"""

    return {
        "mass": _range(mass, mass * 1e-6),
        "volume": _range(mass / 1000.0, 1e-12),
        "periphery": _range(0.1, 1e-6),
        "inertia": _inertia(diagonal),
        "centroid": _centroid(centroid),
        "hasMass": mass > 0,
        "massMissingCount": 0 if mass > 0 else 1,
    }


def mass_properties(element_id: str) -> dict:
    if element_id == STUDIO_A:
        return {
            "microversionId": "mv-a",
            "bodies": {
                PART_A: body(1.5, centroid=(0.0, 0.0, 0.05)),
                PART_L: body(0.25, centroid=(0.0, 0.1, 0.0)),
                PART_N: {"hasMass": False, "massMissingCount": 1},
            },
        }
    return {
        "microversionId": "mv-b",
        "bodies": {PART_B: body(0.05, centroid=(0.01, 0.0, 0.0))},
    }


def _instance(
    instance_id: str,
    kind: str,
    name: str,
    *,
    element: str,
    part: str | None = None,
    suppressed: bool = False,
) -> dict:
    instance = {
        "id": instance_id,
        "type": kind,
        "name": name,
        "suppressed": suppressed,
        "fullConfiguration": "default",
        "configuration": "default",
        "documentId": DOCUMENT_ID,
        "elementId": element,
        "documentMicroversion": SUB_MICROVERSION if kind == "Assembly" else "mv-root",
    }
    if part:
        instance["partId"] = part
    return instance


def _cs(origin=(0.0, 0.0, 0.0), z=(0.0, 0.0, 1.0)) -> dict:
    return {
        "xAxis": [1.0, 0.0, 0.0],
        "yAxis": [0.0, 1.0, 0.0],
        "zAxis": list(z),
        "origin": list(origin),
    }


def _mate(mate_id: str, name: str, mate_type: str, entities) -> dict:
    return {
        "featureType": "mate",
        "id": mate_id,
        "featureData": {
            "name": name,
            "mateType": mate_type,
            "matedEntities": [{"matedOccurrence": list(occurrence), "matedCS": cs} for occurrence, cs in entities],
        },
    }


def assembly_payload() -> dict:
    """根装配：1 处子装配、4 个叶子零件（含 1 个被抑制）、各类 mate 分支。"""

    return {
        "rootAssembly": {
            "documentMicroversion": "mv-root",
            "instances": [
                _instance("base", "Part", "Base <1>", element=STUDIO_A, part=PART_A),
                _instance("arm", "Assembly", "arm <1>", element=SUBASSEMBLY),
                _instance("extra", "Part", "Extra (1) <1>", element=STUDIO_B, part=PART_B),
                _instance("nomass", "Part", "No mass <1>", element=STUDIO_A, part=PART_N),
                _instance("spare", "Part", "Spare <1>", element=STUDIO_A, part=PART_A, suppressed=True),
            ],
            "occurrences": [
                {"path": ["base"], "transform": IDENTITY},
                {"path": ["arm"], "transform": IDENTITY},
                {"path": ["arm", "arm_1/child"], "transform": translate(0.0, 0.0, 0.5)},
                {"path": ["extra"], "transform": translate(0.1, 0.0, 0.0)},
                {"path": ["nomass"], "transform": IDENTITY},
                {"path": ["spare"], "transform": IDENTITY},
            ],
            "features": [
                {"featureType": "mateConnector", "id": "mc_base", "featureData": {"name": "MC_base"}},
                _mate(
                    "mate_hinge",
                    "dof_hinge",
                    "REVOLUTE",
                    [(("base",), _cs((0.0, 0.0, 0.2))), (("arm", "arm_1/child"), _cs((0.0, 0.0, 0.2)))],
                ),
                _mate(
                    "mate_free",
                    "dof_free",
                    "REVOLUTE",
                    [(("base",), _cs((0.0, 0.0, 0.3))), (("extra",), _cs((0.0, 0.0, 0.3)))],
                ),
                _mate(
                    "mate_frame",
                    "frame_body_frame",
                    "FASTENED",
                    [(("base",), _cs()), (("extra",), _cs())],
                ),
                _mate(
                    "mate_cyl",
                    "dof_cyl",
                    "CYLINDRICAL",
                    [(("base",), _cs()), (("extra",), _cs())],
                ),
                _mate(
                    "mate_orphan",
                    "dof_orphan",
                    "REVOLUTE",
                    [(("missing",), _cs()), (("extra",), _cs())],
                ),
            ],
        },
        "subAssemblies": [
            {
                "documentId": DOCUMENT_ID,
                "documentMicroversion": SUB_MICROVERSION,
                "elementId": SUBASSEMBLY,
                "configuration": "default",
                "instances": [_instance("arm_1/child", "Part", "Arm link <1>", element=STUDIO_A, part=PART_L)],
            }
        ],
    }


def features_payload() -> dict:
    """限位参数两种真实形状都覆盖：裸参数与 ``message`` 包裹。"""

    return {
        "features": [
            {
                "name": "dof_hinge",
                "parameters": [
                    {"parameterId": "limitsEnabled", "value": True},
                    {"message": {"parameterId": "limitAxialZMin", "expression": "-0.5 rad"}},
                    {"parameterId": "limitAxialZMax", "expression": "0.75 rad"},
                ],
            },
            {"name": "dof_free", "parameters": [{"parameterId": "limitsEnabled", "value": False}]},
        ]
    }


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_cache(
    root: Path,
    *,
    source: dict | None = None,
    with_source: bool = True,
    stl_parts: tuple[str, ...] = (PART_A, PART_L, PART_B),
) -> Path:
    """写一份与旧采集工具同布局的缓存（``json/`` + ``bytes/``）。"""

    root = Path(root)
    _write(root / "json" / f"assembly_{ROOT_ELEMENT}.json", assembly_payload())
    _write(root / "json" / f"assembly_features_{ROOT_ELEMENT}.json", features_payload())
    _write(root / "json" / f"mate_values_{ROOT_ELEMENT}.json", {"mateValues": []})
    for element_id in (STUDIO_A, STUDIO_B):
        _write(root / "json" / f"mass_properties_{element_id}.json", mass_properties(element_id))
    for part_id in stl_parts:
        path = root / "bytes" / f"stl_{safe_name(part_id)}.stl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(binary_stl())
    if with_source:
        _write(
            root / "json" / "source.json",
            {
                "url": URL,
                "document_id": DOCUMENT_ID,
                "workspace_id": WORKSPACE_ID,
                "element_id": ROOT_ELEMENT,
                "fetched_at": FETCHED_AT,
                **(source or {}),
            },
        )
    return root


class StubClient:
    """只读端点的桩：按路径回放合成响应，用来验证 live_api 通道而不联网。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def request(self, method: str, path: str, query=None, body=None, ctype="application/json", *, raw=False, **kwargs):
        self.calls.append(path)
        clean = path.split("?")[0]
        if clean.startswith("/api/assemblies/") and clean.endswith("/features"):
            return features_payload()
        if clean.startswith("/api/assemblies/") and clean.endswith("/matevalues"):
            return {"mateValues": []}
        if clean.startswith("/api/assemblies/"):
            return assembly_payload()
        element_id = clean.split("/e/")[-1].removesuffix("/massproperties").removesuffix("/gltf")
        if clean.endswith("/massproperties") and element_id in (STUDIO_A, STUDIO_B):
            return mass_properties(element_id)
        raise OnshapeSourceError(API_ERROR, f"stub 没有 {path}", {"status": 404, "path": clean})
