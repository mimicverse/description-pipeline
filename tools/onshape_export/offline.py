"""离线缓存导出：把引擎的 HTTP 传输层换成 cache/ 目录，其余代码路径不变。

用途：Onshape 年度 API 配额耗尽（HTTP 402），或需要在 CI 中复现同一份模型。
cache 由 ``onshape_to_urdf.py fetch``（JSON 数据）与 ``fetch-geometry``（网格）生成。
缓存键与 fetch 一一对应，缺少数据时立即失败，而不是静默换源。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .assembly import apply_mate_roles
from .cache import ResponseCache
from .densities import apply_overrides, part_names_from_assembly


def cached_client_class(
    cache_dir: Path,
    element_id: str,
    *,
    auto_dof: bool = False,
    density_overrides: dict | None = None,
):
    """构造 Client 子类：本工具需要的端点全部改读缓存。"""

    from onshape_to_robot.onshape_api.client import Client

    cache = ResponseCache(cache_dir)
    assembly = cache.require_json(f"assembly_{element_id}")
    if auto_dof:
        # 未按 dof_/fix_ 命名的 mate 按类型改写后再喂给引擎
        assembly = apply_mate_roles(assembly)
    overrides = density_overrides or {}
    by_part = overrides.get("parts", {})
    by_name = overrides.get("names", {})
    part_names = part_names_from_assembly(assembly) if by_name else {}
    studios: dict[str, dict] = {}
    for path in sorted(cache.json_dir.glob("mass_properties_*.json")):
        studio_id = path.stem[len("mass_properties_") :]
        studios[studio_id] = json.loads(path.read_text(encoding="utf-8")).get("bodies", {})
    if by_part or by_name:
        studios, _applied = apply_overrides(studios, by_part, by_name, part_names)
    features = legacy_features(cache.require_json(f"assembly_features_{element_id}"))
    mate_values = cache.require_json(f"mate_values_{element_id}")
    mass_properties = studios

    class CachedClient(Client):
        def __init__(self, *args, **kwargs):  # noqa: D107 - 不初始化 HTTP 层
            self.assembly_data = assembly
            self.features_data = features
            self.mate_values = mate_values
            self.metadata_cache = {}

        def get_assembly(self, did, wmvid, eid, wmv="w", configuration="default"):
            return self.assembly_data

        def get_features(self, did, wvid, eid, wmv="w", configuration="default"):
            return self.features_data

        def matevalues(self, did, wmvid, eid, wmv="w", configuration="default"):
            return self.mate_values

        def part_studio_stl_m(
            self,
            did,
            wmvid,
            eid,
            partid="",
            wmv="m",
            configuration="default",
            linked_document_id=None,
        ):
            data = cache.load_bytes(f"stl_{safe_name(partid)}.stl")
            if data is None:
                raise FileNotFoundError(f"缓存缺少 partId {partid} 的 STL；先运行 fetch-geometry")
            return data

        def part_mass_properties(
            self,
            did,
            wmvid,
            eid,
            partid,
            wmv="m",
            configuration="default",
            linked_document_id=None,
        ):
            if eid not in mass_properties:
                raise FileNotFoundError(f"缓存缺少元素 {eid} 的质量属性；先运行 fetch（或 fetch-geometry）")
            bodies = mass_properties[eid]
            if partid not in bodies:
                raise FileNotFoundError(f"缓存缺少 partId {partid}（元素 {eid}）的质量属性")
            return {"bodies": {partid: bodies[partid]}}

    return CachedClient


def run_with_cache(
    robot_dir: Path,
    cache_dir: Path,
    *,
    auto_dof: bool = False,
    density_overrides: dict | None = None,
) -> None:
    import onshape_to_robot.assembly as assembly_module
    import onshape_to_robot.export as export_module

    element_id = element_id_of(Path(robot_dir) / "config.json")
    assembly_module.Client = cached_client_class(
        Path(cache_dir), element_id, auto_dof=auto_dof, density_overrides=density_overrides
    )
    argv = sys.argv
    try:
        sys.argv = ["onshape-to-robot", str(robot_dir)]
        export_module.main()
    finally:
        sys.argv = argv


def element_id_of(config_path: Path) -> str:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    url = config.get("url", "")
    match = re.search(r"/e/([^/?#]+)", url)
    if not match:
        raise ValueError(f"{config_path} 的 url 缺少 element id：{url!r}")
    return match.group(1)


def legacy_features(features: dict) -> dict:
    """把 v17 扁平 features 响应重包装成引擎期望的 ``{typeName, message}`` 形状。"""

    def strip(node_type: str) -> str:
        return re.sub(r"-\d+$", "", node_type or "")

    wrapped = []
    for feature in features.get("features", []) or []:
        if isinstance(feature.get("message"), dict):
            wrapped.append(feature)
            continue
        message = {key: value for key, value in feature.items() if key != "btType"}
        message["parameters"] = [
            {
                "typeName": strip(parameter.get("btType", "")),
                "message": {k: v for k, v in parameter.items() if k != "btType"},
            }
            for parameter in feature.get("parameters", []) or []
        ]
        wrapped.append({"typeName": strip(feature.get("btType", "")), "message": message})
    return {"features": wrapped}


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value)
