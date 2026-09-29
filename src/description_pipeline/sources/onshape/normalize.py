"""来源语义 → 机器人语义：按显式定义分组、融合、闭合运动链。

输入是**冻结快照**（原始装配 + 质量读数）和一个**显式机器人定义**；输出是可直接交给
公共核心的 ``description.scene/v1``。全程只读原始证据，不从最终 URDF 反抄：

* 分组：定义里列出的成员（CAD 顶层实例）→ 一个刚体；
* 融合：质量求和、质心按质量加权、惯量按平行轴定理搬移（完整张量，不是只搬对角）；
* 网格：每个成员网格按"成员 → 组参考帧"的相对变换放置；
* 关节：父子、轴向符号、限位、零位只取定义；位姿取 mate 连接器（换算到父刚体参考帧）；
* 参考系：``frame_*`` mate 生成无质量 link 与固定关节；
* effort/velocity 与碰撞**不补默认值**：缺失即缺失，由用途规则阻断。

被切掉的实体（装配容器、夹具基准件）必须在定义的 ``non_physical`` 里写明依据；
没被覆盖的物理零件、定义里引用了不存在的实体、或没被定义的 mate 都会直接报错。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from . import linalg
from .assembly import Assembly, Mate
from .contract import validate_scene
from .definition import FrameSpec, JointSpec, LinkGroup, RobotDefinition, parse_robot_definition
from .errors import (
    ATTRIBUTION_MISMATCH,
    DEFINITION_INVALID,
    EVIDENCE_MISSING,
    IDENTITY_COLLISION,
    OnshapeSourceError,
)
from .evidence import evidence_problems, messages as evidence_messages
from .reference import parse_reference
from .scene import SCHEMA_VERSION, SCENE_NAME, _inverse, _rotation_to_rpy
from ...io import confined  # noqa: I001 - 显式跨到公共 io 边界


def normalize_scene(scene: dict, definition: dict, snapshot_root: Path) -> dict:
    """按定义把来源场景规范化为机器人语义场景（纯函数，不改动入参）。"""

    robot = parse_robot_definition(definition)
    root = Path(snapshot_root)
    assembly = _load_assembly(scene, root)
    provenance = scene.get("provenance", {})
    source = provenance.get("source", {})

    links_by_entity = {link["id"]: link for link in scene["links"]}
    expected = set(provenance.get("expected_entities", []))
    expected_occurrences = list(provenance.get("expected_occurrences", []))
    containers = set(provenance.get("non_physical", {}).get("containers", []))
    non_physical = _non_physical_entities(robot)
    assignment, owner, attribution = _attribute(
        robot,
        expected,
        len(expected_occurrences),
        containers,
        non_physical,
        suppressed_count=len(provenance.get("non_physical", {}).get("suppressed", [])),
    )
    advisor = _MassAdvisor(robot, _raw_bodies(root))
    # 守恒对账：原始期望实体 = 已覆盖物理零件 + 显式排除（夹具基准件等），一个不能少。
    excluded = _excluded_entities(robot, non_physical, owner, links_by_entity, advisor, root)
    covered = set(owner)
    if covered | {entry["id"] for entry in excluded} != expected:
        raise OnshapeSourceError(
            ATTRIBUTION_MISMATCH,
            "规范化没有对全部期望实体守恒",
            {
                "expected": sorted(expected),
                "covered": sorted(covered),
                "excluded": sorted(entry["id"] for entry in excluded),
            },
        )
    if not expected <= set(expected_occurrences):
        raise OnshapeSourceError(
            ATTRIBUTION_MISMATCH,
            "期望实体不在原始实例列表里",
            {"missing_in_occurrences": sorted(expected - set(expected_occurrences))},
        )
    unlisted = _unlisted_mates(assembly, robot)
    if unlisted:
        raise OnshapeSourceError(
            ATTRIBUTION_MISMATCH,
            "有 mate 没有出现在定义里，无法判断它是不是物理自由度",
            {"unlisted": unlisted},
        )

    fused: dict[str, dict] = {}
    mass_report: list[dict] = []
    for group in robot.links:
        members = [links_by_entity[entity] for entity in assignment[group.name]]
        fused[group.name], applied = _fuse_world(group, members, assembly, advisor)
        mass_report.extend(applied)

    # URDF 语义：子 link 在 q=0 的坐标系就是驱动它的关节坐标系，而 mate 连接器
    # 在世界系下的位姿是原始证据，可直接测；因此子 link 帧取连接器帧，根 link 帧取
    # 定义里的参考实体帧。
    frames_world: dict[str, linalg.Matrix] = {
        robot.root: assembly.occurrence_transform(_entity_path(assembly, _root_reference(robot)))
    }
    mates: dict[str, Mate] = {}
    for spec in robot.joints:
        mate = _mate(assembly, spec.mate, spec.name)
        mates[spec.name] = mate
        frames_world[spec.child] = assembly.mate_world_transform(mate, 0)
    for frame_spec in robot.frames:
        mate = _mate(assembly, frame_spec.mate, frame_spec.name)
        mates[frame_spec.name] = mate
        frames_world[frame_spec.link] = assembly.mate_world_transform(mate, 0)

    links: list[dict] = []
    for group in robot.links:
        frame_from = "reference_entity" if group.name == robot.root else "driver_mate_connector"
        links.append(_localized_link(group, fused[group.name], frames_world[group.name], frame_from))
    joints: list[dict] = []
    joint_report: list[dict] = []
    for spec in robot.joints:
        joint, report = _revolute_joint(
            spec, mates[spec.name], frames_world[spec.parent], frames_world[spec.child], owner
        )
        joints.append(joint)
        joint_report.append(report)
    for frame_spec in robot.frames:
        links.append(_frame_link(frame_spec, mates[frame_spec.name], owner, non_physical))
        joint, report = _fixed_joint(
            frame_spec,
            mates[frame_spec.name],
            frames_world[frame_spec.parent],
            frames_world[frame_spec.link],
            owner,
        )
        joints.append(joint)
        joint_report.append(report)

    _check_tree(links, joints, robot)
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "name": SCENE_NAME,
        "units": "SI",
        "links": links,
        "joints": joints,
        "frames": [],
        "actuators": [],
        "sensors": [],
        "constraints": [],
        "control": {},
        "contact_excludes": [],
        "provenance": {
            "provider": "onshape",
            "normalized": True,
            "from": {
                "scene_schema": scene.get("schema_version"),
                "element_id": source.get("element_id"),
                "microversion": provenance.get("microversion"),
                "configuration": provenance.get("configuration"),
            },
            "definition": {
                "root": robot.root,
                "links": len(robot.links),
                "joints": len(robot.joints),
                "frames": len(robot.frames),
                "reference": robot.reference,
            },
            "attribution": attribution,
            "expected_entities": sorted(expected),
            "expected_occurrences": expected_occurrences,
            "covered_entities": sorted(covered),
            "excluded_entities": excluded,
            "containers": sorted(containers),
            "mass_assumptions": mass_report,
            "total_mass_kg": round(sum(link["inertial"]["mass"] for link in links if link["inertial"]), 9),
            "joints_from_source": joint_report,
            "conventions": {
                "link_frame": "分组参考实体在冻结快照里的实例坐标系",
                "joint_frame": "mate 连接器坐标系换算到父 link 参考帧；轴向为其 z 轴",
                "zero": "CAD 装配零位；定义可用 zero 覆盖",
                "collision_policy": robot.collision or {"policy": "undefined"},
                "effort_velocity": robot.effort_velocity or {"defined": False},
            },
        },
    }
    validate_scene(normalized)
    return normalized


# --- 装载原始证据 ------------------------------------------------------


def _load_assembly(scene: dict, root: Path) -> Assembly:
    source = scene.get("provenance", {}).get("source", {})
    element = source.get("element_id")
    url = source.get("url")
    if not element or not url:
        raise OnshapeSourceError(EVIDENCE_MISSING, "来源场景缺少 element_id 或 URL", {})
    payloads: dict[str, dict] = {}
    for kind in ("assembly", "assembly_features", "mate_values"):
        path = root / "raw" / f"{kind}_{element}.json"
        if not path.is_file():
            raise OnshapeSourceError(
                EVIDENCE_MISSING,
                "快照缺少规范化所需的原始响应",
                {"missing": f"raw/{kind}_{element}.json", "snapshot": str(root)},
            )
        payloads[kind] = json.loads(path.read_text(encoding="utf-8"))
    return Assembly.from_responses(
        parse_reference(url),
        payloads["assembly"],
        payloads["assembly_features"],
        payloads["mate_values"],
    )


def _raw_bodies(root: Path) -> dict[str, dict[str, dict]]:
    """``(element, partId) → 质量属性读数``。

    partId 只在零件工作室内唯一：跨工作室同名零件必须按元素作用域读取，
    否则会把 A 工作室的体积/质心当成 B 工作室的（或静默 last-wins）。
    """

    bodies: dict[str, dict[str, dict]] = {}
    for path in sorted((root / "raw").glob("mass_properties_*.json")):
        element_id = path.stem.removeprefix("mass_properties_")
        payload = json.loads(path.read_text(encoding="utf-8"))
        scoped = bodies.setdefault(element_id, {})
        for part_id, body in (payload.get("bodies") or {}).items():
            previous = scoped.get(part_id)
            if previous is not None and previous != body:
                raise OnshapeSourceError(
                    IDENTITY_COLLISION,
                    "同一 (element, partId) 在原始读数里出现两次且内容不同",
                    {"element_id": element_id, "part_id": part_id},
                )
            scoped[part_id] = body
    return bodies


def _root_reference(robot: RobotDefinition) -> str:
    for group in robot.links:
        if group.name == robot.root:
            return group.reference
    raise OnshapeSourceError(DEFINITION_INVALID, "root 不在 link 列表里", {"root": robot.root})


def _non_physical_entities(robot: RobotDefinition) -> dict[str, str]:
    declared: dict[str, str] = {}
    for entry in robot.non_physical:
        for entity in entry["entities"]:
            declared[entity] = str(entry["basis"])
    return declared


def _excluded_entities(
    robot: RobotDefinition,
    non_physical: dict[str, str],
    owner: dict[str, str],
    links_by_entity: dict[str, dict],
    advisor: _MassAdvisor,
    root: Path,
) -> list[dict]:
    """被排除的物理零件实例：必须有实际消费方或**快照内被清单绑定**的独立证据。"""

    consumed: dict[str, list[dict]] = {}
    for spec in robot.frames:
        anchor = spec.source.get("anchor_entity")
        if isinstance(anchor, str):
            consumed.setdefault(anchor, []).append({"link": spec.link, "joint": spec.name, "mate": spec.mate})
    declared = {entity: entry for entry in robot.non_physical for entity in entry["entities"]}
    excluded = []
    for entity, basis in sorted(non_physical.items()):
        if entity in owner:
            continue
        entry = declared.get(entity, {})
        consumers = sorted(consumed.get(entity, []), key=lambda item: str(item["link"]))
        link = links_by_entity.get(entity)
        part = link["provenance"]["source_part"] if link is not None else {}
        evidence_file = entry.get("evidence")
        evidence_sha: str | None = None
        if evidence_file:
            evidence_sha = _bound_evidence(root, evidence_file, entity, part)
        if not consumers and not evidence_file:
            raise OnshapeSourceError(
                ATTRIBUTION_MISMATCH,
                "排除项既没有被参考系消费，也没有独立文件证据",
                {"entity": entity, "reason": basis, "declared_by": "robot.non_physical"},
            )
        mass = None
        part_id = None
        geometry = None
        if link is not None:
            part_id = link["provenance"]["source_part"].get("part_id")
            geometry = link["provenance"].get("source_geometry")
            # 排除影响必须可复算：缺质量读数时 _member_reading 会直接失败（unknown impact 不放行）。
            mass, _com, _inertia, _applied = _member_reading(
                link,
                advisor.body_for(link["provenance"]["source_part"].get("element_id"), part_id),
                advisor,
            )
        excluded.append(
            {
                "id": entity,
                "reason": basis,
                "mass_kg": mass,
                "part_id": part_id,
                "source_geometry": geometry,
                "evidence": {
                    "declared_by": "robot.non_physical",
                    "consumed_by": consumers,
                    "file": evidence_file,
                    "sha256": evidence_sha,
                    "detail": entry.get("detail"),
                },
            }
        )
    return excluded


def _bound_evidence(root: Path, relative: str, entity: str, part: dict) -> str:
    """证据文件必须落在快照内、在清单里、来自采集层且绑定到该实体；返回清单摘要。"""

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise OnshapeSourceError(EVIDENCE_MISSING, "快照缺少 manifest.json", {"snapshot": str(root)})
    files = json.loads(manifest_path.read_text(encoding="utf-8")).get("files") or {}
    try:
        confined(root, relative)
    except ValueError as error:
        raise OnshapeSourceError(
            EVIDENCE_MISSING,
            "排除项证据文件越界或不可读",
            {"entity": entity, "evidence": relative, "reason": str(error), "snapshot": str(root)},
        ) from error
    if relative not in files:
        raise OnshapeSourceError(
            EVIDENCE_MISSING,
            "排除项证据文件不在快照清单里",
            {"entity": entity, "evidence": relative, "snapshot": str(root)},
        )
    problems = evidence_problems(
        root, relative, entity, str(part.get("part_id") or ""), str(part.get("element_id") or "")
    )
    if problems:
        raise OnshapeSourceError(
            EVIDENCE_MISSING,
            "排除项证据不合格：" + evidence_messages(problems),
            {"entity": entity, "evidence": relative, "problems": problems, "snapshot": str(root)},
        )
    return str(files[relative])


# --- 归属 -------------------------------------------------------------


def _attribute(
    robot: RobotDefinition,
    expected: set[str],
    occurrence_count: int,
    containers: set[str],
    non_physical: dict[str, str],
    suppressed_count: int = 0,
) -> tuple[dict[str, list[str]], dict[str, str], dict]:
    """实体 → 分组；同时算出 missing/unexpected/non_physical 归属报告。"""

    assignment: dict[str, list[str]] = {group.name: [] for group in robot.links}
    owner: dict[str, str] = {}
    unexpected: list[dict] = []
    for group in robot.links:
        for selector in group.members:
            if selector in expected:
                targets = [selector]
            elif selector in containers:
                targets = sorted(
                    entity for entity in expected if entity == selector or entity.startswith(f"{selector}/")
                )
            else:
                unexpected.append({"group": group.name, "selector": selector})
                continue
            if not targets:
                unexpected.append({"group": group.name, "selector": selector, "reason": "没有任何物理零件"})
            for entity in targets:
                if entity in non_physical:
                    continue  # 夹具基准件由 frame_* 消费，不计入物理刚体
                if entity in owner:
                    raise OnshapeSourceError(
                        ATTRIBUTION_MISMATCH,
                        "同一个物理零件被分到多个分组",
                        {"entity": entity, "groups": [owner[entity], group.name]},
                    )
                owner[entity] = group.name
                assignment[group.name].append(entity)
    missing = sorted(expected - set(owner) - set(non_physical))
    unknown_non_physical = sorted(set(non_physical) - expected)
    if unexpected or missing or unknown_non_physical:
        raise OnshapeSourceError(
            ATTRIBUTION_MISMATCH,
            "定义与快照的物理实体对不上",
            {
                "unexpected": unexpected,
                "missing": missing,
                "unknown_non_physical": unknown_non_physical,
                "counts": {"expected": len(expected), "assigned": len(owner)},
            },
        )
    selectors = {selector for group in robot.links for selector in group.members}
    attribution = {
        "expected_occurrences": occurrence_count,
        "suppressed_instances": suppressed_count,
        "expected_entities": len(expected),
        "assigned": len(owner),
        "non_physical": sorted(non_physical),
        "ignored_containers": sorted(entity for entity in containers if entity not in selectors),
        "per_link": {name: sorted(entities) for name, entities in assignment.items()},
    }
    return assignment, owner, attribution


# --- 质量假设 ---------------------------------------------------------


class _MassAdvisor:
    """密度覆盖 → 质量/惯量（等比缩放，形状与质心不变）。"""

    def __init__(self, robot: RobotDefinition, raw_bodies: dict[str, dict[str, dict]]) -> None:
        self.raw_bodies = raw_bodies
        self.by_part: dict[str, tuple[float, dict]] = {}
        for override in robot.mass_overrides:
            for part_id in override.part_ids:
                self.by_part[part_id] = (override.density_kg_m3, override.source)
        self.by_name = [
            (name, override.density_kg_m3, override.source)
            for override in robot.mass_overrides
            for name in override.names
        ]
        self.default_density = robot.density_default_kg_m3

    def density_for(self, part_id: str, name: str) -> tuple[float, dict] | None:
        if part_id in self.by_part:
            return self.by_part[part_id]
        for candidate, density, source in self.by_name:
            if candidate == name:
                return density, source
        return None

    def body_for(self, element_id: Any, part_id: Any) -> dict | None:
        """按 (element, partId) 取读数：跨工作室同名零件不会互相污染。"""

        return (self.raw_bodies.get(str(element_id)) or {}).get(str(part_id))


def _member_reading(
    link: dict, raw_body: dict | None, advisor: _MassAdvisor
) -> tuple[float, list[float], list[float], dict]:
    """成员的质量/质心/惯量：优先快照读数，其次原始读数，再按密度假设换算。"""

    part = link["provenance"]["source_part"]
    part_id = str(part.get("part_id") or "")
    name = str(link["provenance"].get("source_name") or "").split(" <", 1)[0]
    inertial = link.get("inertial")
    if inertial is None and raw_body:
        mass = (raw_body.get("mass") or [None])[0]
        centroid = (raw_body.get("centroid") or [])[:3]
        tensor = (raw_body.get("inertia") or [])[:9]
        if mass and len(centroid) == 3 and len(tensor) == 9:
            inertial = {
                "mass": float(mass),
                "xyz": [float(value) for value in centroid],
                "inertia": [float(tensor[index]) for index in (0, 1, 2, 4, 5, 8)],
            }
    if inertial is None:
        raise OnshapeSourceError(
            EVIDENCE_MISSING,
            "零件缺少质量读数，定义也没有提供质量假设",
            {"entity": link["id"], "part_id": part_id},
        )
    mass = float(inertial["mass"])
    com = [float(value) for value in inertial["xyz"]]
    inertia = [float(value) for value in inertial["inertia"]]
    override = advisor.density_for(part_id, name)
    volume = float((raw_body or {}).get("volume", [0.0])[0] or 0.0)
    if override and volume <= 0:
        raise OnshapeSourceError(
            EVIDENCE_MISSING,
            "零件有密度假设但缺少体积读数，无法换算质量",
            {"entity": link["id"], "part_id": part_id},
        )
    density, source = override if override else (advisor.default_density, {"default": True})
    if density and volume > 0:
        new_mass = float(density) * volume
        scale = new_mass / mass
        applied = {
            "part_id": part_id,
            "name": name,
            "density_kg_m3": float(density),
            "base_mass_kg": mass,
            "mass_kg": new_mass,
            "inertia_scale": scale,
            "source": source,
        }
        return new_mass, com, [value * scale for value in inertia], applied
    return mass, com, inertia, {}


# --- 融合 -------------------------------------------------------------


def _tensor(values: Sequence[float]) -> list[list[float]]:
    xx, xy, xz, yy, yz, zz = (float(value) for value in values)
    return [[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]]


def _flatten(tensor: Sequence[Sequence[float]]) -> list[float]:
    return [tensor[0][0], tensor[0][1], tensor[0][2], tensor[1][1], tensor[1][2], tensor[2][2]]


def _rotation(matrix: linalg.Matrix) -> list[list[float]]:
    return [[matrix[row][column] for column in range(3)] for row in range(3)]


def _transpose(rotation: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[rotation[column][row] for column in range(3)] for row in range(3)]


def _rotate(tensor: Sequence[Sequence[float]], rotation: Sequence[Sequence[float]]) -> list[list[float]]:
    """R · I · Rᵀ（纯 Python，避免为几个矩阵运算引入依赖）。"""

    return [
        [
            sum(rotation[row][i] * tensor[i][j] * rotation[column][j] for i in range(3) for j in range(3))
            for column in range(3)
        ]
        for row in range(3)
    ]


def _fuse_world(
    group: LinkGroup,
    members: list[dict],
    assembly: Assembly,
    advisor: _MassAdvisor,
) -> tuple[dict, list[dict]]:
    """成员 → 世界系下的质量/质心/惯量；link 帧在定位阶段才选，避免混入参考坐标系。"""

    readings: list[dict] = []
    applied: list[dict] = []
    for link in members:
        path = tuple(link["provenance"]["source_instance"])
        part_id = str(link["provenance"]["source_part"].get("part_id") or "")
        mass, com, inertia, override = _member_reading(
            link, advisor.body_for(link["provenance"]["source_part"].get("element_id"), part_id), advisor
        )
        if override:
            applied.append({**override, "entity": link["id"]})
        readings.append(
            {
                "link": link,
                "mass": mass,
                "com": com,
                "inertia": _tensor(inertia),
                "transform": assembly.occurrence_transform(path),
            }
        )
    total = sum(item["mass"] for item in readings)
    if total <= 0:
        raise OnshapeSourceError(EVIDENCE_MISSING, "分组总质量为零", {"group": group.name, "members": len(members)})
    world_com = [0.0, 0.0, 0.0]
    for item in readings:
        item["world_com"] = _apply(item["transform"], item["com"])
        for axis in range(3):
            world_com[axis] += item["mass"] * item["world_com"][axis]
    world_com = [value / total for value in world_com]
    world_inertia = [[0.0] * 3 for _ in range(3)]
    for item in readings:
        rotated = _rotate(item["inertia"], _rotation(item["transform"]))
        delta = [item["world_com"][axis] - world_com[axis] for axis in range(3)]
        offset = sum(value * value for value in delta)
        for row in range(3):
            for column in range(3):
                parallel = item["mass"] * ((offset if row == column else 0.0) - delta[row] * delta[column])
                world_inertia[row][column] += rotated[row][column] + parallel
    return (
        {
            "readings": readings,
            "mass": total,
            "com_world": world_com,
            "inertia_world": world_inertia,
            "applied": applied,
        },
        applied,
    )


def _localized_link(group: LinkGroup, fused: dict, frame: linalg.Matrix, frame_from: str) -> dict:
    """世界系融合量 → link 局部量；成员网格按相对 link 帧的刚体变换放置。"""

    inverse = _inverse(frame)
    visuals = []
    for item in fused["readings"]:
        filename = item["link"]["provenance"].get("source_geometry")
        if not filename:
            continue
        relative = linalg.matmul(inverse, item["transform"])
        visuals.append(
            {
                "kind": "mesh",
                "filename": filename,
                "scale": [1, 1, 1],
                "xyz": list(linalg.translation(relative)),
                "rpy": _rotation_to_rpy(relative),
                "provenance": {"source_entities": [item["link"]["id"]]},
            }
        )
    local_inertia = _rotate(fused["inertia_world"], _transpose(_rotation(frame)))
    return {
        "id": group.name,
        "name": group.name,
        "inertial": {
            "mass": fused["mass"],
            "xyz": _apply(inverse, fused["com_world"]),
            "rpy": [0.0, 0.0, 0.0],
            "inertia": _flatten(local_inertia),
        },
        "visuals": visuals,
        "collisions": [],
        "provenance": {
            "source_entities": sorted(item["link"]["id"] for item in fused["readings"]),
            "members": [
                {
                    "entity": item["link"]["id"],
                    "part_id": item["link"]["provenance"]["source_part"].get("part_id"),
                    "name": item["link"]["provenance"].get("source_name"),
                    "mass_kg": item["mass"],
                }
                for item in fused["readings"]
            ],
            "reference_entity": group.reference,
            "frame_from": frame_from,
            "mass_assumptions": fused["applied"],
            "collision_policy": "undefined",
            "source": group.source,
        },
    }


# --- 关节与参考系 -----------------------------------------------------


def _entity(path: Iterable[str]) -> str:
    return "/".join(path)


def _entity_path(assembly: Assembly, selector: str) -> list[str]:
    for path in assembly.occurrence_paths():
        if _entity(path) == selector:
            return path
    raise OnshapeSourceError(EVIDENCE_MISSING, "参考实体不在冻结装配里", {"selector": selector})


def _apply(matrix: linalg.Matrix, point: Sequence[float]) -> list[float]:
    return [sum(matrix[row][column] * point[column] for column in range(3)) + matrix[row][3] for row in range(3)]


def _mate(assembly: Assembly, selector: str, owner: str) -> Mate:
    """按名字或 ID 解析唯一 mate；同名（或名字与别人的 ID 撞车）必须失败。

    装配里允许出现同名 mate，但那样就无法用名字寻址。这里不做"取第一个"的猜测：
    静默绑定会让关节连到错误的零件上，而且报告里看不出来。
    """

    matches = [mate for mate in assembly.active_mates if mate.name == selector or mate.id == selector]
    if not matches:
        raise OnshapeSourceError(
            ATTRIBUTION_MISMATCH,
            "定义里的关节/参考系找不到对应 mate",
            {"mate": selector, "definition_entry": owner},
        )
    if len(matches) > 1:
        raise OnshapeSourceError(
            DEFINITION_INVALID,
            "mate 选择器不唯一：请改用 mate ID 或给 mate 改名",
            {
                "mate": selector,
                "definition_entry": owner,
                "candidates": [{"id": mate.id, "name": mate.name} for mate in matches],
            },
        )
    return matches[0]


def _source_order(mate: Mate, owner: dict[str, str]) -> list[dict]:
    return [{"entity": _entity(occurrence), "group": owner.get(_entity(occurrence))} for occurrence in mate.occurrences]


def _joint_limits(spec: JointSpec, mate: Mate) -> dict[str, float] | None:
    limits = spec.limits or {}
    source = limits.get("source")
    if source == "mate_limits":
        if mate.limits is None:
            raise OnshapeSourceError(
                ATTRIBUTION_MISMATCH,
                "定义声明限位取 mate，但 mate 没有限位读数",
                {"joint": spec.name, "mate": mate.name},
            )
        lower, upper = float(mate.limits[0]), float(mate.limits[1])
    elif "lower" in limits and "upper" in limits:
        lower, upper = float(limits["lower"]), float(limits["upper"])
    elif source in (None, "none"):
        return None
    else:
        raise OnshapeSourceError(
            DEFINITION_INVALID,
            "joint.limits 既不是 mate_limits 也不是显式区间",
            {"joint": spec.name},
        )
    if spec.axis_sign < 0:  # 父子与来源顺序相反时必须镜像区间，保持同一物理行程
        lower, upper = -upper, -lower
    return {"lower": lower, "upper": upper}


def _revolute_joint(
    spec: JointSpec,
    mate: Mate,
    parent_frame: linalg.Matrix,
    child_frame: linalg.Matrix,
    owner: dict[str, str],
) -> tuple[dict, dict]:
    local = linalg.matmul(_inverse(parent_frame), child_frame)
    limits = _joint_limits(spec, mate)
    joint: dict[str, Any] = {
        "id": spec.name,
        "name": spec.name,
        "type": spec.type,
        "parent": spec.parent,
        "child": spec.child,
        "xyz": list(linalg.translation(local)),
        "rpy": _rotation_to_rpy(local),
        "axis": [0.0, 0.0, float(spec.axis_sign)],
        "provenance": {
            "source_mate": mate.id,
            "source_mate_name": mate.name,
            "source_mate_type": mate.mate_type,
            "source_mate_entities": [list(item) for item in mate.occurrences],
            "source_order": _source_order(mate, owner),
            "parent_child_from": "definition",
            "axis_from": spec.source.get("axis_from", "mate_connector_z"),
            "axis_sign_from": spec.source.get("axis_sign_from", "definition"),
            "limits_from": spec.limits.get("source", "mate_limits") if spec.limits else "none",
            "zero": {"value": spec.zero, "source": spec.source.get("zero_source", "cad_assembled_pose")},
            "effort_velocity": "not_defined",
            "source": spec.source,
        },
    }
    if limits:
        joint["limits"] = limits
    report = {
        "joint": spec.name,
        "mate": mate.name,
        "type": spec.type,
        "parent": spec.parent,
        "child": spec.child,
        "axis_sign": spec.axis_sign,
        "limits": limits,
        "source_order": joint["provenance"]["source_order"],
        "source": spec.source,
    }
    return joint, report


def _frame_link(spec: FrameSpec, mate: Mate, owner: dict[str, str], non_physical: dict[str, str]) -> dict:
    anchors = [_entity(occurrence) for occurrence in mate.occurrences if _entity(occurrence) in non_physical]
    return {
        "id": spec.link,
        "name": spec.link,
        "inertial": None,
        "visuals": [],
        "collisions": [],
        "provenance": {
            "source_entities": anchors,
            "source_mate": mate.id,
            "source_mate_name": mate.name,
            "source_order": _source_order(mate, owner),
            "kind": "reference_frame",
            "source": spec.source,
        },
    }


def _fixed_joint(
    spec: FrameSpec,
    mate: Mate,
    parent_frame: linalg.Matrix,
    child_frame: linalg.Matrix,
    owner: dict[str, str],
) -> tuple[dict, dict]:
    local = linalg.matmul(_inverse(parent_frame), child_frame)
    joint = {
        "id": spec.name,
        "name": spec.name,
        "type": "fixed",
        "parent": spec.parent,
        "child": spec.link,
        "xyz": list(linalg.translation(local)),
        "rpy": _rotation_to_rpy(local),
        "provenance": {
            "source_mate": mate.id,
            "source_mate_name": mate.name,
            "source_mate_type": mate.mate_type,
            "source_mate_entities": [list(item) for item in mate.occurrences],
            "source_order": _source_order(mate, owner),
            "parent_child_from": "definition",
            "kind": "reference_frame",
            "source": spec.source,
        },
    }
    return joint, {
        "joint": spec.name,
        "mate": mate.name,
        "type": "fixed",
        "parent": spec.parent,
        "child": spec.link,
        "source": spec.source,
    }


def _unlisted_mates(assembly: Assembly, robot: RobotDefinition) -> list[dict]:
    listed = {spec.mate for spec in robot.joints} | {spec.mate for spec in robot.frames}
    return [
        {"mate": mate.name, "type": mate.mate_type, "role": mate.role}
        for mate in assembly.active_mates
        if mate.name not in listed and mate.id not in listed
    ]


def _check_tree(links: list[dict], joints: list[dict], robot: RobotDefinition) -> None:
    """运动链必须真正闭合：唯一根、每个 link 最多一个父、无环、全连通。"""

    names = [link["name"] for link in links]
    graph: dict[str, list[str]] = {name: [] for name in names}
    parents: dict[str, int] = dict.fromkeys(names, 0)
    for joint in joints:
        if joint["parent"] not in graph or joint["child"] not in graph:
            raise OnshapeSourceError(DEFINITION_INVALID, "关节端点不在 link 列表里", {"joint": joint["name"]})
        graph[joint["parent"]].append(joint["child"])
        parents[joint["child"]] += 1
    multi = sorted(name for name, count in parents.items() if count > 1)
    if multi:
        raise OnshapeSourceError(DEFINITION_INVALID, "多个父关节", {"links": multi})
    roots = sorted(name for name, count in parents.items() if count == 0)
    if roots != [robot.root]:
        raise OnshapeSourceError(DEFINITION_INVALID, "运动树根不唯一", {"roots": roots, "expected": robot.root})
    seen: set[str] = set()
    stack = [robot.root]
    while stack:
        name = stack.pop()
        if name in seen:
            raise OnshapeSourceError(DEFINITION_INVALID, "运动树有环", {"link": name})
        seen.add(name)
        stack.extend(graph[name])
    if seen != set(names):
        raise OnshapeSourceError(DEFINITION_INVALID, "有 link 不在运动树里", {"links": sorted(set(names) - seen)})
