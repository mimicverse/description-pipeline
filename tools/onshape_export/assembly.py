"""装配体数据模型：把 Onshape 的三个响应整理成"实例 + mate + 关节树"。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from collections.abc import Iterable

from . import linalg
from .url import DocumentRef

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
        world = linalg.matmul(transform, local)
        return linalg.translation(world), linalg.axis(world)


def _feature_message(feature: dict) -> dict:
    """兼容两种响应形状：未加版本前缀（``{typeName, message}``）与 v17（扁平）。"""

    inner = feature.get("message")
    return inner if isinstance(inner, dict) else feature


def mate_role_plan(assembly: dict) -> dict:
    """列出未按约定命名的 mate，并给出 --auto-dof 会采用的导入角色与最终名称。"""

    plan: dict = {"unclassified": [], "counts": {}, "collisions": []}
    taken = {
        feature.get("featureData", {}).get("name", "")
        for feature in assembly.get("rootAssembly", {}).get("features", []) or []
        if feature.get("featureType") == "mate" and not feature.get("suppressed")
    }
    for feature in assembly.get("rootAssembly", {}).get("features", []) or []:
        if feature.get("featureType") != "mate" or feature.get("suppressed"):
            continue
        data = feature.get("featureData", {})
        name, mate_type = data.get("name", ""), data.get("mateType", "")
        role = next((item for item, prefix in ROLE_PREFIXES.items() if name.startswith(prefix)), None)
        if role is not None:
            plan["counts"][role] = plan["counts"].get(role, 0) + 1
            continue
        suggested = "dof" if mate_type in DOF_MATE_TYPES else "fix"
        candidate = f"{ROLE_PREFIXES[suggested]}{name}"
        suffix = 2
        while candidate in taken:
            candidate = f"{ROLE_PREFIXES[suggested]}{name}_{suffix}"
            suffix += 1
            plan["collisions"].append({"name": name, "renamed_to": candidate})
        taken.add(candidate)
        plan["unclassified"].append(
            {"name": name, "mate_type": mate_type, "suggested_role": suggested, "imported_as": candidate}
        )
        plan["counts"][suggested] = plan["counts"].get(suggested, 0) + 1
    return plan


def apply_mate_roles(assembly: dict) -> dict:
    """把未按约定命名的 mate 改写成引擎认识的 dof_/fix_ 前缀（返回深拷贝）。"""

    import copy

    rewritten = copy.deepcopy(assembly)
    taken = {
        feature["featureData"]["name"]
        for feature in rewritten.get("rootAssembly", {}).get("features", []) or []
        if feature.get("featureType") == "mate" and not feature.get("suppressed")
    }
    for feature in rewritten.get("rootAssembly", {}).get("features", []) or []:
        if feature.get("featureType") != "mate" or feature.get("suppressed"):
            continue
        data = feature.get("featureData", {})
        name, mate_type = data.get("name", ""), data.get("mateType", "")
        if any(name.startswith(prefix) for prefix in ROLE_PREFIXES.values()):
            continue
        role = "dof" if mate_type in DOF_MATE_TYPES else "fix"
        candidate = f"{ROLE_PREFIXES[role]}{name}"
        suffix = 2
        while candidate in taken:
            candidate = f"{ROLE_PREFIXES[role]}{name}_{suffix}"
            suffix += 1
        taken.add(candidate)
        data["name"] = candidate
    return rewritten


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
