"""来源适配器公开接口：``freeze`` 冻结快照，``load_scene`` 离线重放。

共享契约（见 docs/pipeline.md）：

* ``freeze(config: dict, destination: Path) -> dict`` 返回公共清单；
* ``load_scene(snapshot_root: Path) -> dict`` 直接返回 ``description.scene/v1``；
* 清单与摘要用公共 helper ``description_pipeline.sources.snapshot``，场景结构用公共
  ``description_pipeline.model/schema.json`` 校验，本模块都不自行实现一份。

证据边界：``identity.capture.mode`` 只描述取数通道（``live_api``/``cache_replay``），
``evidence_class`` 描述这批字节的来源（``cad``/``fixture``/``imported``）。真实 CAD
快照即使离线重放仍是 ``cad``；仓库夹具必须在 config 里如实声明为 ``fixture``；
没有任何采集声明的缓存一律记 ``imported`` 并追加 ``capture_provenance_missing``。
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import scene as scene_module
from ... import __version__
from ...io import digest
from .cache import CachedFetcher, ResponseCache
from .client import OnshapeClient
from .collector import collect, post_capture_probe, safe_name
from .errors import (
    IDENTITY_COLLISION,
    SCENE_INVALID,
    SHARED_HELPER_MISSING,
    SOURCE_CONFIG_INVALID,
    SNAPSHOT_INCOMPLETE,
    SNAPSHOT_TAMPERED,
    OnshapeSourceError,
)
from .reference import parse_reference

#: 版本单一来源：与安装包描述一致（由 description_pipeline.__version__ 提供）。
SNAPSHOT_KIND = "onshape"
EVIDENCE_CAD = "cad"
EVIDENCE_FIXTURE = "fixture"
EVIDENCE_IMPORTED = "imported"
EVIDENCE_CLASSES = (EVIDENCE_CAD, EVIDENCE_FIXTURE, EVIDENCE_IMPORTED)
#: ``config/robot.yaml`` 里 ``source`` 允许出现的键；拼错或未实现的键直接失败。
SOURCE_KEYS = {
    "provider",
    "url",
    "document_id",
    "element_id",
    "workspace_id",
    "version_id",
    "stack",
    "configuration",
    "cache",
    "offline",
    "include_geometry",
    "tolerance",
    "elements",
    "capture",
    "client",
}


def validate_source_config(config: dict) -> None:
    """拒绝未知键与错误类型：``source`` 里的拼写错误不允许静默改变采集行为。"""

    if not isinstance(config, dict):
        raise OnshapeSourceError(SOURCE_CONFIG_INVALID, "source 必须是映射", {"got": type(config).__name__})
    unknown = sorted(set(config) - SOURCE_KEYS)
    if unknown:
        raise OnshapeSourceError(
            SOURCE_CONFIG_INVALID,
            "source 出现未定义的键",
            {"unknown": unknown, "supported": sorted(SOURCE_KEYS)},
        )
    provider = config.get("provider")
    if provider is not None and provider != SNAPSHOT_KIND:
        raise OnshapeSourceError(
            SOURCE_CONFIG_INVALID,
            "source.provider 与来源不符",
            {"provider": provider, "expected": SNAPSHOT_KIND},
        )
    for key in ("offline", "include_geometry"):
        if key in config and not isinstance(config[key], bool):
            raise OnshapeSourceError(SOURCE_CONFIG_INVALID, f"source.{key} 必须是布尔值", {"value": config[key]})
    for key in ("url", "document_id", "element_id", "workspace_id", "version_id", "stack", "configuration"):
        if key in config and config[key] is not None and not isinstance(config[key], str):
            raise OnshapeSourceError(SOURCE_CONFIG_INVALID, f"source.{key} 必须是字符串", {"value": config[key]})
    tolerance = config.get("tolerance")
    if tolerance is not None and (
        not isinstance(tolerance, (int, float)) or not math.isfinite(float(tolerance)) or float(tolerance) <= 0
    ):
        raise OnshapeSourceError(SOURCE_CONFIG_INVALID, "source.tolerance 必须是有限正数", {"value": tolerance})
    elements = config.get("elements")
    if elements is not None and (
        not isinstance(elements, list) or not all(isinstance(item, str) and item for item in elements)
    ):
        raise OnshapeSourceError(SOURCE_CONFIG_INVALID, "source.elements 必须是非空字符串列表", {"value": elements})
    capture = config.get("capture")
    if capture is not None and not isinstance(capture, dict):
        raise OnshapeSourceError(SOURCE_CONFIG_INVALID, "source.capture 必须是映射", {"value": capture})


def _shared_snapshot():
    """公共清单 helper（由集成方提供）；缺失时明确失败而不是自定义一份。"""

    try:
        from description_pipeline.sources import snapshot as shared
    except ModuleNotFoundError as error:  # pragma: no cover - 集成后才可能发生
        raise OnshapeSourceError(
            SHARED_HELPER_MISSING,
            "缺少公共 snapshot helper：description_pipeline.sources.snapshot",
            {"expected": "write_manifest/verify_snapshot/load_scene"},
        ) from error
    return shared


def _capture_settings(config: dict, cache: ResponseCache | None, fetcher: CachedFetcher) -> dict:
    """采集身份三层记录，互不覆盖：

    * ``origin``：**原始采集记录**（缓存里的 capture / source.json 时间），原样保留；
    * ``declared``：调用方在 ``config.capture`` 里的显式选择或补充 + ``reason``；
    * ``effective``：``origin`` 与 ``declared`` 合并后的生效值（兼容字段用它，但绝不回写 origin）。

    另外 ``run`` 记录本次运行方式：``api`` / ``cache_replay`` / ``mixed``（混合缓存与 API 时不把
    整批宣称为新采集）、当前工具版本、实际运行时间；本次真的联网取数时把本次采集身份记进
    ``run.capture``（mode/tool_version/at + 取到的项）。原记录没有的字段一律不补。
    """

    origin: dict[str, Any] = {}
    if cache is not None:
        stored = cache.load_json("source")
        if isinstance(stored, dict):
            if isinstance(stored.get("capture"), dict):
                origin.update(stored["capture"])
                origin.setdefault("record", "cache_capture")
            if stored.get("fetched_at"):
                origin.setdefault("cache_fetched_at", str(stored["fetched_at"]))
                origin.setdefault("at", str(stored["fetched_at"]))
                origin.setdefault("at_source", "cache_source_json")
    declared: dict[str, Any] = {}
    override = config.get("capture")
    if isinstance(override, dict):
        if not override.get("reason"):
            raise OnshapeSourceError(
                SNAPSHOT_INCOMPLETE,
                "capture 覆盖必须写 reason，否则无法复核证据来源",
                {"capture": sorted(override)},
            )
        declared = dict(override)
        if declared.get("at"):
            declared["at_source"] = "caller"
    effective = {**origin, **declared}
    if effective.get("evidence") is not None and effective["evidence"] not in EVIDENCE_CLASSES:
        raise OnshapeSourceError(
            SNAPSHOT_INCOMPLETE,
            "capture.evidence 必须是 cad/fixture/imported",
            {"evidence": effective["evidence"]},
        )
    network_names = fetcher.network_names
    cache_names = fetcher.cache_names
    if network_names and cache_names:
        transport = "mixed"
    elif network_names:
        transport = "api"
    else:
        transport = "cache_replay"
    run: dict[str, Any] = {
        "method": "api" if network_names else "cache_replay",
        "transport": transport,
        "mixed": transport == "mixed",
        "tool_version": __version__,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "at_source": "run",
        "network_requests": network_names,
        "cache_requests": cache_names,
    }
    if network_names:
        # 本次确实生成了原始证据：明确记录本次采集身份，不让历史 cache.capture 冒充。
        run["capture"] = {
            "mode": "live_api",
            "tool_version": __version__,
            "at": run["at"],
            "requests": network_names,
            "note": "mixed 时只有 network_requests 列出的项是本次采集，其余来自历史缓存",
        }
    legacy_at = effective.get("at")
    legacy_at_source = effective.get("at_source", "unrecorded")
    if legacy_at is None and network_names:
        # 只有"本次真的采集"才把运行时间写进兼容字段的采集时刻
        legacy_at, legacy_at_source = run["at"], "run"
    return {
        "mode": "live_api" if network_names else "cache_replay",
        "tool_version": run["tool_version"],
        "at": legacy_at,
        "at_source": legacy_at_source,
        "evidence": effective.get("evidence"),
        "reason": effective.get("reason"),
        "cache_fetched_at": effective.get("cache_fetched_at"),
        "origin": origin,
        "declared": declared,
        "effective": effective,
        "run": run,
    }


#: 原始读数的语义说明：单位、坐标系与惯量参考点，供规范模型与验收引用。
SOURCE_SEMANTICS = {
    "units": "SI",
    "length": "m",
    "mass": "kg",
    "inertia_unit": "kg*m^2",
    "angular_unit": "rad",
    "inertia_reference": "center_of_mass",
    # 原始读数与 scene 格式必须分开写：Onshape 质量属性 API 返回的是"标称+上下界"的三份数据。
    "raw_scalar_reading": "[标称, 下界, 上界]（mass / volume / periphery）",
    "raw_centroid_reading": "9 个数：[标称(x,y,z), 下界(x,y,z), 上界(x,y,z)]",
    "raw_inertia_reading": "27 个数：同一个 3×3 行主序惯量矩阵连写三份——标称、下界、上界",
    "scene_inertia_form": "description.scene 只保留标称张量的 6 个独立分量 "
    "[ixx, ixy, ixz, iyy, iyz, izz]，关于质心、在零件工作室坐标系；上下界不下传",
    "part_frame": "part studio origin",
    "assembly_frame": "root assembly origin",
    "occurrence_transform": "行主序 4x4（平移在 3/7/11 位），part frame → assembly frame",
    "raw_evidence_form": {
        "json": "按响应内容重新序列化（内容摘要见 request_bindings）",
        "bytes": "逐字节原样保存（网格 STL）",
    },
    "joint_axis": "mate 连接器第 0 端 z 轴；轴线方向由定义给出",
}


def _capture_settings_block(config: dict, configuration: str, offline: bool) -> dict:
    """导出/采集设置：复现快照所需的输入参数。"""

    return {
        "configuration": configuration,
        # 沿用旧工具的 glTF 网格↔零件匹配阈值：score = |ΔV|/V + |Δcom|，
        # 即"无量纲相对体积误差 + 质心距离"，混合量纲，不能标成米。
        "tolerance": float(config.get("tolerance", 0.05)),
        "tolerance_meaning": "glTF 网格↔零件匹配阈值，作用在 (|ΔV|/V + |Δcom|)，沿用旧工具语义；"
        "混合量纲（比值 + 质心距离），来源未为其单独声明单位",
        "include_geometry": bool(config.get("include_geometry", True)),
        "elements": list(config.get("elements") or []),
        "offline": offline,
    }


def _snapshot_origin(capture: dict, evidence_class: str) -> dict:
    """快照的原始采集身份：只报告原始记录里确实有的字段（缺失即不出现）。"""

    origin = dict(capture.get("origin") or {})
    if origin.get("at") is None:
        origin.pop("at", None)
    return {
        "evidence_class": evidence_class,
        "recorded_capture": origin,
        "cache_binding": capture.get("cache_binding"),
        "unbound": capture.get("unbound", []),
    }


def _this_run_identity(capture: dict, bindings: dict, revision: dict) -> dict:
    """本次运行方式：回放不接触 CAD 接口，因此不能证明当前接口可用。"""
    from ...build import tool_identity

    run = dict(capture.get("run") or {})
    method = str(run.get("method") or ("api" if capture.get("mode") == "live_api" else "cache_replay"))
    contacted = method == "api"
    post = revision["post_probe"]
    return {
        "method": method,
        "transport": run.get("transport", method),
        "mixed": bool(run.get("mixed")),
        "capture": run.get("capture"),
        "network_requests": run.get("network_requests", []),
        "cache_requests": run.get("cache_requests", []),
        "cad_api_contacted": contacted,
        # 接口可用性只看"本次是否真的拿到过 API 响应"，与工作区头是否移动无关。
        "cad_api_available_proven": contacted,
        # 工作区头稳定性是独立结论：版本引用/回放为 None（不适用），并给出理由。
        "cad_api_head_unchanged": post.get("unchanged"),
        "cad_api_head_stability": {"status": post.get("status"), "reason": post.get("reason")},
        "tool_version": run.get("tool_version"),
        "toolchain": tool_identity(),
        "at": run.get("at"),
        "at_source": run.get("at_source", "run"),
        "cache_binding": bindings["kind"],
        "verified_requests": sum(1 for entry in bindings["bindings"].values() if entry["verified"]),
        "unbound_requests": bindings["unbound"],
        "note": "回放只证明快照自洽，不证明当前 CAD 接口可用；接口可用性只能在 live 采集中验证",
    }


def _evidence_class(capture: dict) -> str:
    """取数通道 + 采集声明 → 证据等级；不声明来源的缓存只能记 ``imported``。"""

    if capture.get("mode") == "live_api":
        return EVIDENCE_CAD
    evidence = capture.get("evidence")
    return evidence if evidence in EVIDENCE_CLASSES else EVIDENCE_IMPORTED


def _request_bindings(fetcher: CachedFetcher, ref, collection) -> dict:
    """把每项缓存/请求绑定到 (path, query, sha256)，并说明证据强度。"""

    probe = f"assembly_{ref.element_id}"
    bindings = {
        name: {
            "path": entry.get("path"),
            "query": entry.get("query"),
            "sha256": entry.get("sha256"),
            "immutable": bool(entry.get("immutable")),
            "verified": bool(entry.get("verified")),
        }
        for name, entry in sorted(fetcher.bindings.items())
    }
    unbound = sorted(fetcher.unbound)
    if not bindings:
        kind = "unknown"
    elif fetcher.used_network:
        kind = "live"
    elif unbound:
        kind = "legacy_unverified"
    else:
        kind = "indexed"
    unverified = sorted(name for name, entry in bindings.items() if not entry["verified"])
    return {
        "kind": kind,
        "probe": probe,
        "bindings": bindings,
        "unbound": unbound,
        "unverified": unverified,
        "digest": digest(bindings),
        "count": len(bindings),
    }


def _revision_evidence(collection, fetcher: CachedFetcher, ref, bindings: dict) -> dict:
    """修订是否真正锁定：每个请求都要用不可变标识，且缓存项要能被证明。"""

    probe = bindings["probe"]
    pinned = [
        name
        for name, entry in bindings["bindings"].items()
        if name != probe and entry["immutable"] and entry["verified"]
    ]
    others = [name for name in bindings["bindings"] if name != probe]
    pinned_ok = bool(others) and len(pinned) == len(others)
    probe_verified = (bindings["bindings"].get(probe) or {}).get("verified") is True
    locked = bool(pinned_ok and probe_verified and not bindings["unbound"] and collection.revision_locked())
    if ref.version_id and locked:
        reason = "版本引用 /v/<version> 本身不可变"
    elif locked:
        reason = "根装配微版本已解析，其余请求全部使用 /m/<microversion>"
    elif bindings["kind"] == "legacy_unverified":
        reason = "旧缓存没有请求身份索引，无法证明字节来自同一修订"
    elif not collection.microversion:
        reason = "没有可用的 documentMicroversion"
    else:
        reason = "存在非不可变请求或未经验证的缓存项"
    return {
        "locked": locked,
        "reason": reason,
        "microversion": collection.microversion,
        "pinned_requests": sorted(pinned),
        "probe_request": probe,
        "probe_path": (bindings["bindings"].get(probe) or {}).get("path"),
        "part_studio_element_microversions": dict(sorted(collection.element_microversions.items())),
        "element_microversions_proven": bool(locked and fetcher.used_network),
    }


def _validate_scene(scene: dict) -> None:
    """用公共 schema 校验场景结构；物理合理性留给规范化层判断。"""

    try:
        from importlib.resources import files

        import jsonschema

        schema_path = files("description_pipeline.model").joinpath("schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (ModuleNotFoundError, FileNotFoundError) as error:  # pragma: no cover - 集成后才有
        raise OnshapeSourceError(
            SHARED_HELPER_MISSING,
            "缺少公共模型 schema：description_pipeline/model/schema.json",
            {"expected": "description.scene/v1"},
        ) from error
    try:
        jsonschema.Draft202012Validator(schema).validate(scene)
    except jsonschema.ValidationError as error:
        raise OnshapeSourceError(
            SCENE_INVALID,
            f"派生场景不符合公共 schema：{list(error.absolute_path)}",
            {"message": error.message},
        ) from error


def freeze(config: dict, destination: Path) -> dict:
    """采集 → 冻结快照。``destination`` 必须是空的暂存目录。"""

    validate_source_config(config)
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise OnshapeSourceError(SNAPSHOT_INCOMPLETE, "destination 必须为空目录", {"destination": str(destination)})
    destination.mkdir(parents=True, exist_ok=True)
    ref = parse_reference(
        config.get("url"),
        document_id=config.get("document_id"),
        element_id=config.get("element_id"),
        workspace_id=config.get("workspace_id"),
        version_id=config.get("version_id"),
        stack=config.get("stack") or "https://cad.onshape.com",
    )
    configuration = str(config.get("configuration") or "default")
    offline = bool(config.get("offline"))
    cache_dir = config.get("cache")
    cache = ResponseCache(Path(cache_dir), read_only=offline or not cache_dir) if cache_dir else None
    client = config.get("client")
    if client is None and not offline:
        client = OnshapeClient.from_env(ref.stack)
    fetcher = CachedFetcher(client, cache)

    collection = collect(
        ref,
        fetcher,
        configuration=configuration,
        tolerance=float(config.get("tolerance", 0.05)),
        include_geometry=bool(config.get("include_geometry", True)),
        elements=list(config.get("elements") or []),
    )
    capture_settings = _capture_settings_block(config, configuration, offline)
    capture = _capture_settings(config, cache, fetcher)
    bindings = _request_bindings(fetcher, ref, collection)
    revision = _revision_evidence(collection, fetcher, ref, bindings)
    revision["post_probe"] = post_capture_probe(ref, fetcher, collection.microversion, configuration)
    if revision["post_probe"].get("status") == "changed":
        collection.gaps.append(
            {
                "kind": "workspace_moved_during_capture",
                "detail": revision["post_probe"]["reason"],
                "pinned_microversion": revision["post_probe"].get("pinned_microversion"),
                "post_microversion": revision["post_probe"].get("post_microversion"),
            }
        )
    this_run = _this_run_identity(capture, bindings, revision)
    capture["cache_binding"] = bindings["kind"]
    if bindings["unbound"]:
        capture["unbound"] = bindings["unbound"]
        collection.gaps.append(
            {
                "kind": "cache_request_identity_unverified",
                "detail": "旧缓存没有请求身份索引，无法证明这些项来自当前 path/query/配置",
                "names": bindings["unbound"],
            }
        )
    if not revision["locked"]:
        collection.gaps.append({"kind": "revision_not_locked", "detail": revision["reason"]})
    if collection.element_microversions and not revision["element_microversions_proven"]:
        collection.gaps.append(
            {
                "kind": "part_studio_microversion_unproven",
                "detail": "零件工作室响应只带元素级 microversion，无法据此证明与文档修订的对应关系",
                "element_microversions": collection.element_microversions,
            }
        )
    evidence_class = _evidence_class(capture)
    gaps = list(collection.gaps)
    if evidence_class == EVIDENCE_IMPORTED:
        gaps.append(
            {
                "kind": "capture_provenance_missing",
                "detail": "缓存/输入没有采集声明，无法证明来自真实 API 采集",
            }
        )

    raw_dir = destination / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for element_id, payload in sorted(collection.assemblies.items()):
        _write_json(raw_dir / f"assembly_{element_id}.json", payload)
    for element_id, payload in sorted(collection.assembly_features.items()):
        _write_json(raw_dir / f"assembly_features_{element_id}.json", payload)
    for element_id, payload in sorted(collection.mate_values.items()):
        _write_json(raw_dir / f"mate_values_{element_id}.json", payload)
    for element_id, payload in sorted(collection.mass_properties.items()):
        _write_json(raw_dir / f"mass_properties_{element_id}.json", payload)
    for element_id, payload in sorted(collection.gltf.items()):
        _write_json(raw_dir / f"gltf_{element_id}.json", payload)
    geometry_dir = destination / "geometry" / "parts"
    geometry_dir.mkdir(parents=True, exist_ok=True)
    files_by_part: dict[str, str] = {}
    for part_id, mesh in sorted(collection.geometry.items()):
        name = safe_name(part_id)
        other = files_by_part.get(name)
        if other is not None and other != part_id:
            raise OnshapeSourceError(
                IDENTITY_COLLISION,
                "两个 partId 净化后同名，快照文件名无法唯一",
                {"name": name, "part_ids": sorted({other, part_id})},
            )
        files_by_part[name] = part_id
        (geometry_dir / f"{name}.stl").write_bytes(mesh)
    _write_json(
        destination / "geometry" / "parts.json",
        {
            "parts": {
                part_id: {
                    "part_id": part_id,
                    "file": f"geometry/parts/{safe_name(part_id)}.stl",
                    **reading,
                }
                for part_id, reading in sorted(collection.geometry_readings.items())
            }
        },
    )

    scene = scene_module.build_scene(collection)
    scene["provenance"]["capture"] = capture
    scene["provenance"]["evidence_class"] = evidence_class
    scene["provenance"]["gaps"] = gaps
    _validate_scene(scene)
    _write_json(destination / "scene.json", scene)

    identity = {
        "provider": SNAPSHOT_KIND,
        "provider_version": __version__,
        **ref.identity(),
        "configuration": configuration,
        "microversion_id": collection.microversion,
        "revision_locked": revision["locked"],
        "revision_evidence": revision,
        "dependency_closure": {
            "elements": collection.element_ids(),
            "subassemblies": sorted(collection.subassemblies),
            "part_studios": sorted(collection.mass_properties),
            "parts": sorted(collection.geometry_readings),
        },
        "capture": capture,
        "snapshot_origin": _snapshot_origin(capture, evidence_class),
        "this_run": this_run,
        "capture_settings": capture_settings,
        "source_semantics": SOURCE_SEMANTICS,
        "request_bindings": bindings,
        "counts": collection.counts(),
    }
    return _manifest(destination, identity=identity, evidence_class=evidence_class)


def load_scene(snapshot_root: Path) -> dict:
    """离线重放：校验清单与全部文件摘要后，直接返回 ``description.scene/v1``。"""

    root = Path(snapshot_root)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise OnshapeSourceError(SNAPSHOT_INCOMPLETE, "快照缺少 manifest.json", {"snapshot": str(root)})
    shared = _shared_snapshot()
    manifest = _verified(shared, root)
    if manifest.get("kind") != SNAPSHOT_KIND:
        raise OnshapeSourceError(
            SNAPSHOT_INCOMPLETE,
            "快照不是 onshape 来源",
            {"kind": manifest.get("kind"), "snapshot": str(root)},
        )
    # verify_snapshot 已对每个声明文件做过 confined 校验，这里直接读取公共清单指定的场景。
    try:
        scene = json.loads((root / manifest["scene"]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise OnshapeSourceError(SNAPSHOT_TAMPERED, "场景文件不可读", {"scene": manifest.get("scene")}) from error
    if not isinstance(scene, dict):
        raise OnshapeSourceError(SNAPSHOT_TAMPERED, "场景文件必须是对象", {})
    return scene


def _manifest(root: Path, *, identity: dict, evidence_class: str) -> dict:
    """写公共清单；清单不一致时按来源层错误码报错。"""

    shared = _shared_snapshot()
    try:
        return shared.write_manifest(root, kind=SNAPSHOT_KIND, identity=identity, evidence_class=evidence_class)
    except ValueError as error:
        raise OnshapeSourceError(SNAPSHOT_INCOMPLETE, "清单写入失败", {"reason": str(error)}) from error


def _verified(shared, root: Path) -> dict:
    try:
        return shared.verify_snapshot(root)
    except ValueError as error:
        raise OnshapeSourceError(SNAPSHOT_TAMPERED, "快照与清单不一致", {"reason": str(error)}) from error


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Frozen readings are byte-committed evidence: pin LF so a snapshot captured on Windows has the
    # same digests as one captured on Linux.
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
