"""装配体来源语义：把 Onshape 的响应整理成"实例 + mate + 连接器坐标系"。

本模块只描述**来源读到的事实**（谁连到谁、连接器在世界里的位姿、限位参数），
不做 URDF 语义判定；角色分类只用于报告与后续规范化层参考。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Iterable

from . import linalg
from .reference import DocumentRef

DOF_PREFIX = "dof_"
FIXED_PREFIXES = ("fix_", "frame_", "closing_")
DOF_MATE_TYPES = frozenset({"REVOLUTE", "CYLINDRICAL", "SLIDER", "BALL"})
ROLE_PREFIXES = {"dof": DOF_PREFIX, "frame": "frame_", "closing": "closing_", "fix": "fix_"}


@dataclass(frozen=True)
class Instance:
    id: str
    name: str
    type: str
    element_id: str | None = None
    part_id: str | None = None
    configuration: str = "default"
    suppressed: bool = False

    @property
    def is_part(self) -> bool:
        return self.type == "Part"


@dataclass(frozen=True)
class Mate:
    id: str
    name: str
    mate_type: str
    occurrences: tuple[tuple[str, ...], ...]
    origins: tuple[tuple[float, float, float], ...] = ()
    limits: tuple[float, float] | None = None
    suppressed: bool = False

    @property
    def is_dof(self) -> bool:
        return self.name.startswith(DOF_PREFIX)

    @property
    def is_frame(self) -> bool:
        return self.name.startswith("frame_")

    @property
    def is_fixed(self) -> bool:
        return self.name.startswith("fix_") or (
            self.mate_type == "FASTENED"
            and not self.is_dof
            and not self.is_frame
            and not self.name.startswith("closing_")
        )

    @property
    def resolved(self) -> bool:
        """Onshape 侧未解算的 mate（UI 提示 "cannot resolve mate connectors"）为 False。"""

        return len(self.occurrences) == 2 and all(self.occurrences)

    @property
    def joint_name(self) -> str:
        if self.is_dof:
            return self.name[len(DOF_PREFIX) :]
        if self.is_frame:
            return self.name[len("frame_") :]
        return self.name

    @property
    def role(self) -> str:
        """mate 在引擎里的角色：dof / frame / closing / fix / unclassified。"""

        for role, prefix in ROLE_PREFIXES.items():
            if self.name.startswith(prefix):
                return role
        return "unclassified"

    @property
    def suggested_role(self) -> str:
        """未按约定命名的 mate 按类型推断的角色。"""

        return "dof" if self.mate_type in DOF_MATE_TYPES else "fix"

    @property
    def bodies(self) -> tuple[str, ...]:
        """mate 两端所属的顶层实例 id。"""

        return tuple(path[0] for path in self.occurrences if path)


@dataclass
class Assembly:
    ref: DocumentRef
    raw: dict
    features: dict
    mate_values: dict
    instances: list[Instance] = field(default_factory=list)
    mates: list[Mate] = field(default_factory=list)
    occurrences: dict[tuple[str, ...], dict] = field(default_factory=dict)
    unresolved_paths: list[list[str]] = field(default_factory=list)

    # --- 构造 -----------------------------------------------------------

    @classmethod
    def from_responses(cls, ref: DocumentRef, assembly: dict, features: dict, mate_values: dict) -> Assembly:
        root = assembly.get("rootAssembly", {})
        instances = [
            Instance(
                id=item.get("id", ""),
                name=item.get("name", ""),
                type=item.get("type", ""),
                element_id=item.get("elementId"),
                part_id=item.get("partId"),
                configuration=item.get("configuration", "default"),
                suppressed=bool(item.get("suppressed")),
            )
            for item in root.get("instances", [])
            if item.get("id")
        ]
        occurrences = {tuple(item.get("path", [])): item for item in root.get("occurrences", [])}
        limits = _limits_by_name(features)
        mates = []
        for feature in root.get("features", []):
            if feature.get("featureType") != "mate":
                continue
            data = feature.get("featureData", {})
            entities = data.get("matedEntities", []) or []
            mates.append(
                Mate(
                    id=feature.get("id", ""),
                    name=data.get("name", ""),
                    mate_type=data.get("mateType", ""),
                    occurrences=tuple(tuple(item.get("matedOccurrence", [])) for item in entities),
                    origins=tuple(tuple(item.get("matedCS", {}).get("origin", ())) for item in entities),
                    limits=limits.get(data.get("name", "")),
                    suppressed=bool(feature.get("suppressed")),
                )
            )
        return cls(
            ref=ref,
            raw=assembly,
            features=features,
            mate_values=mate_values,
            instances=instances,
            mates=mates,
            occurrences=occurrences,
        )

    # --- 查询 -----------------------------------------------------------

    @property
    def active_mates(self) -> list[Mate]:
        return [mate for mate in self.mates if not mate.suppressed]

    @property
    def dof_mates(self) -> list[Mate]:
        return [mate for mate in self.active_mates if mate.is_dof]

    @property
    def frame_mates(self) -> list[Mate]:
        return [mate for mate in self.active_mates if mate.is_frame]

    @property
    def root_instance(self) -> Instance | None:
        """库把第一个顶层实例当作 URDF 根。"""

        return self.instances[0] if self.instances else None

    def instance(self, instance_id: str) -> Instance | None:
        return next((item for item in self.instances if item.id == instance_id), None)

    # --- 拍平实例树（来源语义，不做刚体分组）------------------------------

    def _subassembly_index(self) -> dict[tuple[str, str, str, str], list[dict]]:
        index: dict[tuple[str, str, str, str], list[dict]] = {}
        for sub in self.raw.get("subAssemblies", []) or []:
            key = (
                str(sub.get("documentId", "")),
                str(sub.get("documentMicroversion", "")),
                str(sub.get("elementId", "")),
                str(sub.get("configuration", "")),
            )
            index[key] = list(sub.get("instances", []) or [])
        return index

    def _instances_at(self, level: dict) -> list[dict]:
        if level.get("root"):
            return list(self.raw.get("rootAssembly", {}).get("instances", []) or [])
        key = (
            str(level.get("documentId", "")),
            str(level.get("documentMicroversion", "")),
            str(level.get("elementId", "")),
            str(level.get("configuration", "")),
        )
        return self._subassembly_index().get(key, [])

    def leaf_instances(self) -> list[dict]:
        """全部叶子（Part）实例，带完整 occurrence path（取自拍平的 occurrences）。

        根层零件实例的路径只有一段，子装配内的零件有两段以上；两者都是叶子，不能按
        路径长度过滤。
        """

        self.unresolved_paths = []
        level: dict = {"root": True}
        result: list[dict] = []
        for path in self.occurrence_paths():
            current = None
            for depth, instance_id in enumerate(path):
                candidates = self._instances_at(level) if depth == 0 else self._instances_at(current or {})
                current = next((item for item in candidates if item.get("id") == instance_id), None)
                if current is None:
                    break
            if current is None:
                self.unresolved_paths.append(path)
                continue
            if current.get("type") != "Part":
                continue
            result.append({**current, "occurrence": path})
        return result

    def occurrence_paths(self) -> list[list[str]]:
        """全部实例路径（逐级实例 ID），取自拍平的 ``occurrences`` 列表。"""

        return [
            [str(item) for item in occurrence.get("path", [])]
            for occurrence in self.raw.get("rootAssembly", {}).get("occurrences", []) or []
            if occurrence.get("path")
        ]

    def part_studios(self) -> list[tuple[str, str]]:
        """装配体引用到的 (elementId, configuration) 去重列表（仅 Part）。"""

        seen: dict[str, str] = {}
        for item in self.instances:
            if item.is_part and item.element_id:
                seen.setdefault(item.element_id, item.configuration)
        for entry in self.raw.get("subAssemblies", []) or []:
            for item in entry.get("instances", []) or []:
                element_id = item.get("elementId")
                if element_id and item.get("type") == "Part":
                    seen.setdefault(element_id, item.get("configuration", "default"))
        return sorted(seen.items())

    # --- 运动学图 -------------------------------------------------------

    def graph(self, mates: Iterable[Mate] | None = None) -> dict[str, set[str]]:
        nodes: dict[str, set[str]] = {item.id: set() for item in self.instances}
        for mate in mates if mates is not None else self.dof_mates:
            if not mate.resolved:
                continue
            first, second = mate.bodies[0], mate.bodies[-1]
            if first in nodes and second in nodes and first != second:
                nodes[first].add(second)
                nodes[second].add(first)
        return nodes

    def components(self, mates: Iterable[Mate] | None = None) -> list[list[str]]:
        graph = self.graph(mates)
        seen: set[str] = set()
        result: list[list[str]] = []
        for node in graph:
            if node in seen:
                continue
            stack, group = [node], []
            seen.add(node)
            while stack:
                current = stack.pop()
                group.append(current)
                for neighbour in graph[current]:
                    if neighbour not in seen:
                        seen.add(neighbour)
                        stack.append(neighbour)
            result.append(sorted(group))
        return result

    def tree_report(self, mates: Iterable[Mate] | None = None) -> dict[str, Any]:
        """返回 DOF 图的连通性结论：是否单树、环、游离实例。"""

        use = list(mates if mates is not None else self.dof_mates)
        graph = self.graph(use)
        edges = sum(len(value) for value in graph.values()) // 2
        nodes = [node for node, value in graph.items() if value]
        components = [group for group in self.components(use) if len(group) > 1]
        isolated = [item.id for item in self.instances if not graph[item.id]]
        return {
            "nodes": len(nodes),
            "edges": edges,
            "is_tree": bool(nodes) and len(components) == 1 and edges == len(nodes) - 1,
            "has_cycle": bool(nodes) and edges >= len(nodes) and len(components) == 1,
            "components": components,
            "isolated": isolated,
        }

    # --- 几何 -----------------------------------------------------------

    def occurrence_transform(self, path: Iterable[str]) -> linalg.Matrix:
        entry = self.occurrences.get(tuple(path))
        matrix = linalg.identity()
        if entry and entry.get("transform"):
            matrix = linalg.from_flat(entry["transform"])
        return matrix

    def mate_world_frame(
        self, mate: Mate, entity_index: int = 0
    ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """mate 第 ``entity_index`` 端在世界系（装配体坐标系）下的原点与 z 轴。"""

        world = self.mate_world_transform(mate, entity_index)
        return linalg.translation(world), linalg.axis(world)

    def mate_world_transform(self, mate: Mate, entity_index: int = 0) -> linalg.Matrix:
        """mate 第 ``entity_index`` 端连接器在世界系下的完整坐标系（供关节/参考系定位）。"""

        if not mate.resolved:
            raise ValueError(f"{mate.name} 未解算，无法取坐标系")
        entity = self.raw["rootAssembly"]["features"]
        data = next(
            item["featureData"] for item in entity if item["featureType"] == "mate" and item.get("id") == mate.id
        )["matedEntities"][entity_index]
        transform = self.occurrence_transform(data["matedOccurrence"])
        cs = data.get("matedCS", {})
        local = linalg.from_axes(
            cs.get("xAxis", (1.0, 0.0, 0.0)),
            cs.get("yAxis", (0.0, 1.0, 0.0)),
            cs.get("zAxis", (0.0, 0.0, 1.0)),
            cs.get("origin", (0.0, 0.0, 0.0)),
        )
        return linalg.matmul(transform, local)


def _feature_message(feature: dict) -> dict:
    """兼容两种响应形状：未加版本前缀（``{typeName, message}``）与 v17（扁平）。"""

    inner = feature.get("message")
    return inner if isinstance(inner, dict) else feature


def _limits_by_name(features: dict) -> dict[str, tuple[float, float]]:
    """从装配体 features 响应里取每个 mate 的限位（含表达式与布尔开关）。"""

    result: dict[str, tuple[float, float]] = {}
    for feature in features.get("features", []) or []:
        message = _feature_message(feature)
        name = message.get("name", "")
        if not name:
            continue
        enabled = None
        lower = upper = None
        for parameter in message.get("parameters", []) or []:
            inner = parameter.get("message", parameter)
            parameter_id = inner.get("parameterId")
            if parameter_id == "limitsEnabled":
                enabled = bool(inner.get("value"))
            elif parameter_id == "limitAxialZMin":
                lower = _quantity(inner)
            elif parameter_id == "limitAxialZMax":
                upper = _quantity(inner)
        if enabled and lower is not None and upper is not None:
            result[name] = (lower, upper)
    return result


def _quantity(parameter: dict) -> float | None:
    """限位参数可能是 "0.5 rad" 这类表达式，也可能是纯数值。"""

    expression = parameter.get("expression")
    if isinstance(expression, str) and expression.strip():
        token = expression.strip().split()[0]
        try:
            return float(token)
        except ValueError:
            return None
    value = parameter.get("value")
    if isinstance(value, (int, float)):
        return float(value)
    return None
