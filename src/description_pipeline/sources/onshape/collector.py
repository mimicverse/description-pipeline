"""依赖闭包采集：根装配 →（子装配递归）→ 零件工作室质量属性与几何。

修订锁定：工作区引用先用 ``/w/`` 读一次根装配拿到 ``documentMicroversion``，
之后**所有请求都改用 ``/m/<microversion>``**（版本引用直接用 ``/v/``），因此同一次冻结里
的每个字节都属于同一个不可变修订。依赖实例必须与根同文档、同配置、同微版本，
否则明确拒绝（当前快照命名空间是单文档单配置的）。

只做采集与索引，不做 URDF/物理判定；所有原始响应都带稳定的缓存名，
便于冻结进快照并离线重放（缓存项与请求身份的绑定由 :mod:`.cache` 负责）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import geometry as geometry_module
from .cache import CachedFetcher
from .errors import (
    CACHE_MISS,
    DEPENDENCY_INCOMPLETE,
    FOREIGN_DOCUMENT,
    GEOMETRY_UNRESOLVED,
    IDENTITY_COLLISION,
    REVISION_MISMATCH,
    OnshapeSourceError,
)
from .reference import DocumentRef
from .stl import StlError, mesh_reading, sha256_bytes


def safe_name(value: str) -> str:
    """与既有缓存一致的文件名净化（``stl_<safe_name(partId)>.stl``）。"""

    return "".join(char if char.isalnum() else "_" for char in value)


@dataclass(frozen=True)
class RequestScope:
    """一次元素请求的不可变身份：文档 + (工作区|版本|微版本) + 配置。"""

    document_id: str
    wvm: str
    wvmid: str
    configuration: str

    def base(self, kind: str, element_id: str) -> str:
        return f"/api/{kind}/d/{self.document_id}/{self.wvm}/{self.wvmid}/e/{element_id}"

    @property
    def immutable(self) -> bool:
        return self.wvm in {"m", "v"}


@dataclass
class Collection:
    ref: DocumentRef
    configuration: str
    microversion: str | None = None
    pinned_scope: RequestScope | None = None
    assemblies: dict[str, dict] = field(default_factory=dict)
    assembly_features: dict[str, dict] = field(default_factory=dict)
    mate_values: dict[str, dict] = field(default_factory=dict)
    mass_properties: dict[str, dict] = field(default_factory=dict)
    gltf: dict[str, dict] = field(default_factory=dict)
    geometry: dict[str, bytes] = field(default_factory=dict)
    geometry_readings: dict[str, dict] = field(default_factory=dict)
    part_owners: dict[str, str] = field(default_factory=dict)
    element_configurations: dict[str, set[str]] = field(default_factory=dict)
    element_microversions: dict[str, str] = field(default_factory=dict)
    subassemblies: set[str] = field(default_factory=set)
    gaps: list[dict] = field(default_factory=list)

    def element_ids(self) -> list[str]:
        """依赖闭包里出现的全部元素（子装配 + 零件工作室）。"""

        return sorted(set(self.assemblies) | set(self.mass_properties))

    def counts(self) -> dict[str, int]:
        instances = sum(
            len(payload.get("rootAssembly", {}).get("instances", []) or []) for payload in self.assemblies.values()
        )
        occurrences = sum(
            len(payload.get("rootAssembly", {}).get("occurrences", []) or []) for payload in self.assemblies.values()
        )
        mates = sum(len(payload.get("features", []) or []) for payload in self.assembly_features.values())
        return {
            "assemblies": len(self.assemblies),
            "subassemblies": len(self.subassemblies),
            "instances": instances,
            "occurrences": occurrences,
            "mates": mates,
            "part_studios": len(self.mass_properties),
            "parts_with_geometry": len(self.geometry),
            "gaps": len(self.gaps),
        }

    def revision_locked(self) -> bool:
        """只有每个请求都用了不可变标识，才叫锁定。"""

        return self.pinned_scope is not None and self.pinned_scope.immutable


def _instance_elements(assembly: dict) -> list[dict[str, Any]]:
    root = assembly.get("rootAssembly", {})
    return list(root.get("instances", []) or [])


def _embedded_instances(collection: Collection, element_id: str) -> list[dict[str, Any]] | None:
    """已在已采集装配响应的 ``subAssemblies`` 里出现的子装配实例列表。

    真实装配响应内嵌全部子装配（含其 instances），因此这些元素不需要单独请求；
    只有响应没内嵌时才回退到逐个请求。
    """

    for payload in collection.assemblies.values():
        for sub in payload.get("subAssemblies", []) or []:
            if str(sub.get("elementId") or "") == element_id:
                return list(sub.get("instances", []) or [])
    return None


def collect(
    ref: DocumentRef,
    fetcher: CachedFetcher,
    *,
    configuration: str = "default",
    tolerance: float = 0.05,
    include_geometry: bool = True,
    elements: list[str] | None = None,
) -> Collection:
    """采集根装配及其依赖；``elements`` 可限定已知的零件工作室列表（离线缺目录时用）。"""

    collection = Collection(ref=ref, configuration=configuration)
    _note_configuration(collection, ref.element_id, configuration)
    head = _head_scope(ref, configuration)
    assembly = _read_assembly(fetcher, head, ref.element_id, collection, with_details=False)
    pinned = _pin_scope(ref, head, assembly, collection)
    collection.pinned_scope = pinned
    _read_details(fetcher, pinned, ref.element_id, collection)
    queue: list[str] = []
    for instance in _instance_elements(assembly):
        _enqueue(queue, collection, pinned, instance)
    seen: set[str] = {ref.element_id}
    while queue:
        element_id = queue.pop(0)
        if element_id in seen:
            continue
        seen.add(element_id)
        embedded = _embedded_instances(collection, element_id)
        if embedded is not None:
            collection.subassemblies.add(element_id)
            for item in embedded:
                _enqueue(queue, collection, pinned, item)
            continue
        if _try_part_studio(
            fetcher, pinned, element_id, collection, tolerance=tolerance, include_geometry=include_geometry
        ):
            continue
        collection.subassemblies.add(element_id)
        nested = _read_assembly(fetcher, pinned, element_id, collection)
        for instance in _instance_elements(nested):
            _enqueue(queue, collection, pinned, instance, seen=seen)
    for element_id in dict.fromkeys(elements or []):
        _try_part_studio(
            fetcher, pinned, element_id, collection, tolerance=tolerance, include_geometry=include_geometry
        )
    _check_configuration_conflicts(collection)
    return collection


# --- 作用域与修订 -----------------------------------------------------


def _head_scope(ref: DocumentRef, configuration: str) -> RequestScope:
    """根元素入口请求：版本引用用 ``/v/``，工作区引用先用可变的 ``/w/`` 探测微版本。"""

    if ref.version_id:
        return RequestScope(ref.document_id, "v", ref.version_id, configuration)
    return RequestScope(ref.document_id, "w", str(ref.workspace_id), configuration)


def _pin_scope(ref: DocumentRef, head: RequestScope, assembly: dict, collection: Collection) -> RequestScope:
    """读到根装配的 ``documentMicroversion`` 后，把整条链钉到不可变修订。"""

    microversion = assembly.get("rootAssembly", {}).get("documentMicroversion")
    if head.wvm == "v":
        collection.microversion = str(microversion) if microversion else None
        return head
    if not microversion:
        raise OnshapeSourceError(
            REVISION_MISMATCH,
            "工作区引用没有返回 documentMicroversion，无法锁定修订",
            {"element_id": ref.element_id, "document_id": ref.document_id},
        )
    collection.microversion = str(microversion)
    return RequestScope(ref.document_id, "m", str(microversion), head.configuration)


def _dependency_scope(instance: dict, root: RequestScope, collection: Collection) -> RequestScope:
    """依赖实例必须与根同文档、同配置、同微版本（当前快照命名空间是单文档单配置）。"""

    document_id = str(instance.get("documentId") or root.document_id)
    if document_id != root.document_id:
        raise OnshapeSourceError(
            FOREIGN_DOCUMENT,
            "依赖来自其它文档，当前快照命名空间无法唯一命名",
            {
                "document_id": document_id,
                "root_document_id": root.document_id,
                "element_id": instance.get("elementId"),
                "name": instance.get("name"),
            },
        )
    configuration = str(instance.get("configuration") or collection.configuration)
    if configuration != collection.configuration:
        raise OnshapeSourceError(
            IDENTITY_COLLISION,
            "依赖使用了未被请求的配置，缓存名无法区分",
            {"element_id": instance.get("elementId"), "configuration": configuration},
        )
    if root.wvm != "m":
        return RequestScope(document_id, root.wvm, root.wvmid, configuration)
    microversion = str(instance.get("documentMicroversion") or "")
    if not microversion:
        raise OnshapeSourceError(
            REVISION_MISMATCH,
            "依赖实例没有 documentMicroversion，无法锁定修订",
            {"element_id": instance.get("elementId"), "name": instance.get("name")},
        )
    if microversion != root.wvmid:
        raise OnshapeSourceError(
            REVISION_MISMATCH,
            "依赖引用了与根装配不同的微版本，同一快照无法同时表示",
            {
                "element_id": instance.get("elementId"),
                "dependency_microversion": microversion,
                "root_microversion": root.wvmid,
            },
        )
    return RequestScope(document_id, "m", microversion, configuration)


def _enqueue(
    queue: list[str],
    collection: Collection,
    pinned: RequestScope,
    instance: dict,
    *,
    seen: set[str] | None = None,
) -> None:
    """校验依赖身份（同文档/同配置/同微版本）后入队；非法依赖在这里就失败。"""

    element_id = str(instance.get("elementId") or "")
    kind = str(instance.get("type") or "")
    if not element_id or kind not in {"Part", "Assembly"}:
        # 缺元素 ID 或类型未知时无法保证依赖闭包完整：装配子树可能整段消失，必须拒绝。
        raise OnshapeSourceError(
            DEPENDENCY_INCOMPLETE,
            "实例缺少 elementId 或类型未知，无法保证依赖闭包完整",
            {
                "instance": instance.get("id"),
                "name": instance.get("name"),
                "type": kind or None,
                "element_id": element_id or None,
            },
        )
    _note_configuration(collection, element_id, instance.get("configuration"))
    scope = _dependency_scope(instance, pinned, collection)
    if seen is not None and element_id in seen:
        return
    if (scope.document_id, scope.wvm, scope.wvmid, scope.configuration) != (
        pinned.document_id,
        pinned.wvm,
        pinned.wvmid,
        pinned.configuration,
    ):  # pragma: no cover - _dependency_scope 已保证一致，这里只做兜底断言
        raise OnshapeSourceError(
            REVISION_MISMATCH,
            "依赖请求作用域与根作用域不一致",
            {"element_id": element_id, "dependency": scope.__dict__, "root": pinned.__dict__},
        )
    queue.append(element_id)


def _note_configuration(collection: Collection, element_id: str, configuration: Any) -> None:
    """记录元素被引用的配置；缓存文件名不含配置，冲突必须显式拒绝。"""

    if not element_id:
        return
    collection.element_configurations.setdefault(element_id, set()).add(str(configuration or "default"))


def _check_configuration_conflicts(collection: Collection) -> None:
    conflicts = {
        element_id: sorted(configurations)
        for element_id, configurations in collection.element_configurations.items()
        if len(configurations) > 1
    }
    if conflicts:
        raise OnshapeSourceError(
            IDENTITY_COLLISION,
            "同一元素在同一次冻结里被以多个配置引用，缓存名无法区分",
            {"conflicts": conflicts, "configuration": collection.configuration},
        )


# --- 请求 -------------------------------------------------------------


def _read_assembly(
    fetcher: CachedFetcher,
    scope: RequestScope,
    element_id: str,
    collection: Collection,
    *,
    with_details: bool = True,
) -> dict:
    base = scope.base("assemblies", element_id)
    assembly = fetcher.json(
        f"assembly_{element_id}",
        base,
        {"configuration": scope.configuration, "includeMateFeatures": "true"},
    )
    root = assembly.get("rootAssembly", {})
    expected = {"documentId": scope.document_id, "elementId": element_id, "configuration": scope.configuration}
    if scope.wvm == "m":
        expected["documentMicroversion"] = scope.wvmid
    mismatches = {
        key: {"expected": value, "actual": root[key]}
        for key, value in expected.items()
        if key in root and root[key] != value
    }
    if mismatches:
        raise OnshapeSourceError(REVISION_MISMATCH, "装配响应身份与请求不符", mismatches)
    collection.assemblies[element_id] = assembly
    if with_details:
        _read_details(fetcher, scope, element_id, collection)
    return assembly


def _read_details(fetcher: CachedFetcher, scope: RequestScope, element_id: str, collection: Collection) -> None:
    if element_id in collection.assembly_features:
        return
    base = scope.base("assemblies", element_id)
    features = fetcher.json(
        f"assembly_features_{element_id}", f"{base}/features", {"configuration": scope.configuration}
    )
    declared = features.get("sourceMicroversion")
    if scope.wvm == "m" and declared and str(declared) != scope.wvmid:
        raise OnshapeSourceError(
            REVISION_MISMATCH,
            "features 响应的微版本与请求的微版本不一致",
            {"element_id": element_id, "response": str(declared), "request": scope.wvmid},
        )
    collection.assembly_features[element_id] = features
    collection.mate_values[element_id] = fetcher.json(
        f"mate_values_{element_id}", f"{base}/matevalues", {"configuration": scope.configuration}
    )


def _try_part_studio(
    fetcher: CachedFetcher,
    scope: RequestScope,
    element_id: str,
    collection: Collection,
    *,
    tolerance: float,
    include_geometry: bool,
) -> bool:
    """先试零件工作室；返回 True 表示是零件工作室（并已采集）。

    离线重放时没有 HTTP 状态码可用，因此用缓存里是否存在 ``assembly_<id>`` 判断这是
    子装配；两者都不在缓存里才作为缓存不完整报错。
    """

    base = scope.base("partstudios", element_id)
    try:
        mass = fetcher.json(f"mass_properties_{element_id}", f"{base}/massproperties")
    except OnshapeSourceError as error:
        status = int(error.detail.get("status") or 0)
        cached_assembly = fetcher.cache is not None and (fetcher.cache.json_path(f"assembly_{element_id}").is_file())
        if status in (400, 404, 409) or (error.code == CACHE_MISS and cached_assembly):
            # 不是零件工作室：交给装配体路径继续递归
            return False
        raise
    collection.mass_properties[element_id] = mass
    element_microversion = mass.get("microversionId")
    if element_microversion:
        collection.element_microversions[element_id] = str(element_microversion)
    if not include_geometry:
        return True
    bodies = mass.get("bodies", {}) or {}
    try:
        gltf = fetcher.json(f"gltf_{element_id}", f"{base}/gltf")
    except OnshapeSourceError as error:
        cached = _cached_part_stls(fetcher, bodies)
        if cached:
            for part_id, payload in cached.items():
                _record_geometry(
                    collection,
                    element_id=element_id,
                    part_id=part_id,
                    payload=payload,
                    body=bodies.get(part_id) or {},
                    geometry_source="cached_part_stl",
                )
            collection.gaps.append(
                {
                    "kind": "gltf_unavailable",
                    "element_id": element_id,
                    "reason": error.code,
                    "fallback": "cached_part_stl",
                    "parts": len(cached),
                }
            )
        else:
            collection.gaps.append({"kind": "gltf", "element_id": element_id, "reason": error.code})
        return True
    collection.gltf[element_id] = gltf
    try:
        files, pending = geometry_module.split_gltf(gltf, bodies, tolerance=tolerance)
    except geometry_module.GeometryError as error:
        collection.gaps.append({"kind": "gltf_split", "element_id": element_id, "reason": str(error)})
        return True
    for item in pending:
        collection.gaps.append({"kind": "geometry_unmatched", "element_id": element_id, **item})
    for part_id, payload in files.items():
        _record_geometry(
            collection,
            element_id=element_id,
            part_id=part_id,
            payload=payload,
            body=bodies.get(part_id) or {},
            geometry_source="gltf_split",
        )
    missing = [part_id for part_id in bodies if part_id not in files and bodies[part_id].get("volume")]
    for part_id in missing:
        collection.gaps.append(
            {"kind": "geometry_missing", "element_id": element_id, "part_id": part_id, "code": GEOMETRY_UNRESOLVED}
        )
    return True


def _record_geometry(
    collection: Collection,
    *,
    element_id: str,
    part_id: str,
    payload: bytes,
    body: dict,
    geometry_source: str,
) -> None:
    """落盘字节总是保留为证据；只有通过公共 STL 接受标准的才进读数与场景。"""

    owner = collection.part_owners.get(part_id)
    if owner is not None and owner != element_id:
        raise OnshapeSourceError(
            IDENTITY_COLLISION,
            "同一个 partId 出现在两个零件工作室，几何/读数无法唯一命名",
            {"part_id": part_id, "elements": sorted({owner, element_id})},
        )
    collection.part_owners[part_id] = element_id
    collection.geometry[part_id] = payload
    try:
        collection.geometry_readings[part_id] = mesh_reading(
            payload,
            element_id=element_id,
            part_id=part_id,
            body=body,
            geometry_source=geometry_source,
        )
    except StlError as error:
        collection.gaps.append(
            {
                "kind": "geometry_invalid",
                "element_id": element_id,
                "part_id": part_id,
                "sha256": sha256_bytes(payload),
                "reason": str(error),
            }
        )


def post_capture_probe(
    ref: DocumentRef, fetcher: CachedFetcher, pinned_microversion: str | None, configuration: str
) -> dict:
    """采集前后来源一致性检查：只用于 live 采集，结果如实记录，不产生证据字节。

    * 版本引用：修订本身不可变，无需比较（仍然记录 not_applicable 与理由）。
    * 工作区引用：再读一次 ``/w/`` 头，比较 ``documentMicroversion`` 是否仍等于采集时钉住的
      微版本。工作区在采集期间移动时快照仍锁定在钉住的微版本上，但必须显式记下来。
    """

    if ref.version_id:
        return {
            "status": "not_applicable",
            "reason": "版本引用 /v/<version> 不可变，无前后可比的工作区头",
            "checked": False,
        }
    if fetcher.client is None or not fetcher.used_network:
        return {
            "status": "not_applicable",
            "reason": "cache_replay 不接触 CAD 接口，回放不证明当前接口可用",
            "checked": False,
        }
    path = f"/api/assemblies/d/{ref.document_id}/w/{ref.workspace_id}/e/{ref.element_id}"
    payload = fetcher.client.request("GET", path, query={"configuration": configuration})
    head = str(payload.get("rootAssembly", {}).get("documentMicroversion") or "")
    unchanged = bool(pinned_microversion) and head == pinned_microversion
    return {
        "status": "passed" if unchanged else "changed",
        "checked": True,
        "path": path,
        "pinned_microversion": pinned_microversion,
        "post_microversion": head or None,
        "unchanged": unchanged,
        "reason": (
            "采集期间工作区头未移动"
            if unchanged
            else "采集期间工作区头发生了移动；快照仍锁定在钉住的微版本，但需复核是否要重新采集"
        ),
    }


def _cached_part_stls(fetcher: CachedFetcher, bodies: dict) -> dict[str, bytes]:
    """旧缓存（``fetch-geometry``）只有 per-part STL：按零件 ID 取回字节。"""

    cache = fetcher.cache
    if cache is None:
        return {}
    files: dict[str, bytes] = {}
    for part_id in bodies:
        name = f"stl_{safe_name(part_id)}.stl"
        payload = cache.load_bytes(name)
        if payload is None:
            continue
        # This legacy geometry fallback has no immutable API request identity.
        fetcher.unbound.add(name)
        fetcher.bindings[name] = {
            "path": "legacy-cache/" + name,
            "sha256": sha256_bytes(payload),
            "immutable": False,
            "verified": False,
        }
        files[part_id] = payload
    return files
