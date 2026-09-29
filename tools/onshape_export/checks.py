"""导出前诊断：把 Onshape 侧会导致导出失败或静默错误的模式提前拦住。

规则编号稳定，可在评审、CI 与工单里直接引用（``OSX###``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .assembly import Assembly, mate_role_plan

ERROR = "error"
WARNING = "warning"
INFO = "info"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "context": self.context,
        }


def run_checks(
    assembly: Assembly,
    *,
    mass_properties: dict[str, dict] | None = None,
    allow_unlimited: bool = False,
    auto_dof: bool = False,
) -> list[Finding]:
    """返回诊断列表，调用方按 severity 决定退出码。"""

    findings: list[Finding] = []
    mates = assembly.active_mates
    dof = assembly.dof_mates

    if not dof:
        findings.append(Finding("OSX001", ERROR, "装配体里没有 dof_* mate，导出不会产生任何关节"))

    unresolved = [mate for mate in mates if not mate.resolved]
    for mate in unresolved:
        findings.append(
            Finding(
                "OSX002",
                ERROR,
                f"mate {mate.name} 未解算（Onshape 报 cannot resolve mate connectors）",
                {"mate": mate.name, "mate_id": mate.id, "entities": len(mate.occurrences)},
            )
        )

    duplicates = _duplicate_names([mate.name for mate in mates])
    for name, count in duplicates.items():
        findings.append(
            Finding(
                "OSX003",
                ERROR,
                f"mate 名称重复 {count} 次：{name}（库会生成重复关节/非树结构）",
                {"mate": name, "count": count},
            )
        )

    tree = assembly.tree_report()
    if dof and not tree["is_tree"]:
        findings.append(
            Finding(
                "OSX004",
                ERROR,
                "dof_* 关节图不是一棵树（存在环或存在多棵子树）",
                {
                    "nodes": tree["nodes"],
                    "edges": tree["edges"],
                    "components": len(tree["components"]),
                },
            )
        )
    for group in tree["components"][1:]:
        names = [_instance_name(assembly, node) for node in group]
        findings.append(Finding("OSX004b", ERROR, "存在孤立的关节子树，导出会得到多个根节点", {"instances": names}))
    orphan_bodies = {body for mate in assembly.active_mates if not mate.is_dof for body in mate.bodies}
    unused = [node for node in tree["isolated"] if node not in orphan_bodies]
    if unused:
        names = [_instance_name(assembly, node) for node in unused]
        findings.append(
            Finding(
                "OSX005",
                WARNING,
                "有实例不参与任何 dof_* mate，会作为额外根节点出现在导出里",
                {"instances": names},
            )
        )

    shared = _shared_frame_orphans(assembly)
    for instance_id, names in shared.items():
        findings.append(
            Finding(
                "OSX006",
                ERROR,
                "多个 frame_* mate 复用同一个孤儿实例；库要求每个 frame 有独立孤儿体",
                {"instance": instance_id, "frames": names},
            )
        )

    for mate in dof:
        if mate.mate_type in {"REVOLUTE", "SLIDER"} and mate.limits is None and not allow_unlimited:
            findings.append(
                Finding(
                    "OSX007",
                    WARNING,
                    f"{mate.name} 没有启用限位；确认这是有意的（可用 --allow-unlimited 抑制）",
                    {"mate": mate.name},
                )
            )

    root = assembly.root_instance
    if root is not None:
        findings.append(
            Finding(
                "OSX008",
                INFO,
                f"URDF 根节点将是装配体第一个实例：{root.name}（库行为，非文档设定）",
                {"instance": root.name, "instance_id": root.id},
            )
        )

    plan = mate_role_plan(assembly.raw)
    dof_like = [item for item in plan["unclassified"] if item["suggested_role"] == "dof"]
    if plan["unclassified"]:
        if auto_dof:
            findings.append(
                Finding(
                    "OSX010",
                    INFO,
                    f"{len(plan['unclassified'])} 个 mate 未按约定命名，将按类型导入："
                    f"{len(dof_like)} 个作为关节（dof_）、"
                    f"{len(plan['unclassified']) - len(dof_like)} 个作为固定（fix_）",
                    {"mates": plan["unclassified"][:20]},
                )
            )
        elif dof_like:
            findings.append(
                Finding(
                    "OSX010",
                    ERROR,
                    f"{len(dof_like)} 个转动/平动 mate 未按 dof_ 命名，导出会被当成固定连接",
                    {"mates": dof_like[:20], "hint": "用 --auto-dof 按类型导入，或在 Onshape 里改名"},
                )
            )
        else:
            findings.append(
                Finding(
                    "OSX010b",
                    INFO,
                    f"{len(plan['unclassified'])} 个未命名约定的 mate 都是固定类型，会按固定连接导入",
                    {"mates": plan["unclassified"][:10]},
                )
            )
    for collision in plan["collisions"]:
        findings.append(
            Finding(
                "OSX011",
                WARNING,
                f"自动改名与既有 mate 重名，已顺延：{collision['name']} → {collision['renamed_to']}",
                collision,
            )
        )

    if mass_properties is not None:
        used_parts = {
            item.get("partId")
            for sub in assembly.raw.get("subAssemblies", []) or []
            for item in sub.get("instances", []) or []
            if item.get("partId")
        } | {item.part_id for item in assembly.instances if item.part_id}
        missing = sorted(
            {
                element_id
                for element_id, bodies in mass_properties.items()
                for part_id, body in bodies.items()
                if part_id in used_parts and not body.get("hasMass", True)
            }
        )
        for element_id in missing:
            findings.append(
                Finding(
                    "OSX009",
                    WARNING,
                    "有零件没有质量（STEP 导入不带材质），导出质量为 0，需先在 Onshape 赋材质",
                    {"element_id": element_id},
                )
            )

    return findings


def summarize(findings: list[Finding]) -> dict:
    counts = {ERROR: 0, WARNING: 0, INFO: 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return {
        "errors": counts[ERROR],
        "warnings": counts[WARNING],
        "infos": counts[INFO],
        "passed": counts[ERROR] == 0,
    }


def _duplicate_names(names: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return {name: count for name, count in counts.items() if count > 1}


def _shared_frame_orphans(assembly: Assembly) -> dict[str, list[str]]:
    """找出被多个 frame_* 共用的孤儿实例（库要求彼此独立）。"""

    graph = assembly.graph()
    tree_nodes = {node for node, neighbours in graph.items() if neighbours}
    users: dict[str, list[str]] = {}
    for mate in assembly.frame_mates:
        if not mate.resolved:
            continue
        for body in mate.bodies:
            if body in tree_nodes:
                continue
            users.setdefault(body, []).append(mate.name)
    return {body: names for body, names in users.items() if len(names) > 1}


def _instance_name(assembly: Assembly, node: str) -> str:
    """实例存在时用可读名字，否则退回节点 id（两者都是 str，避免 Optional）。"""

    instance = assembly.instance(node)
    return instance.name if instance is not None else node
