"""把冻结的来源语义映射成 ``description.scene/v1``。

只做转换，不做判定：刚体分组（哪些叶子零件合成一个物理刚体）、URDF 语义与物理
合理性由规范化层负责。规则集中在三处并全部写进 provenance，便于上层复核或覆盖：

* 一个叶子 Part 实例 = 一个 link，link 帧取零件工作室坐标系；
* joint 的父子取 mate ``matedEntities`` 顺序，轴向取第 0 端 mate 连接器 z 轴；
* mate 角色取 CAD 里的命名前缀（``dof_``/``frame_``/``fix_``/``closing_``）。

凡是来源里读不到或无法唯一确定的量（限位、几何、effort/velocity、碰撞策略）一律留空
并记进 ``gaps``，不猜、不补默认值。
"""

from __future__ import annotations

import math
import re
from typing import Any
from collections.abc import Sequence

from . import linalg
from .assembly import Assembly
from .collector import Collection, safe_name

SCHEMA_VERSION = "description.scene/v1"
SCENE_NAME = "robot"
MATE_TYPES = {
    "REVOLUTE": "revolute",
    "SLIDER": "prismatic",
    "FASTENED": "fixed",
}
UNSUPPORTED_MATE_TYPES = {"CYLINDRICAL", "BALL", "PLANAR", "PARALLEL"}
_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


# --- 数值工具 ---------------------------------------------------------


def _rotation_to_rpy(matrix: linalg.Matrix) -> list[float]:
    """旋转矩阵 → URDF 固定轴 rpy（R = Rz(y)·Ry(p)·Rx(r)）。"""

    pitch = math.asin(max(-1.0, min(1.0, -matrix[2][0])))
    if abs(math.cos(pitch)) > 1e-9:
        roll = math.atan2(matrix[2][1], matrix[2][2])
        yaw = math.atan2(matrix[1][0], matrix[0][0])
    else:  # 万向锁：退化为只取 yaw
        roll = math.atan2(-matrix[1][2], matrix[1][1])
        yaw = 0.0
    return [roll, pitch, yaw]


def _inverse(matrix: linalg.Matrix) -> linalg.Matrix:
    """刚体变换求逆（旋转转置 + 平移反向）。"""

    rotation = tuple(tuple(matrix[row][column] for row in range(3)) for column in range(3))
    shift = linalg.translation(matrix)
    rows = tuple(
        (
            rotation[row][0],
            rotation[row][1],
            rotation[row][2],
            -(rotation[row][0] * shift[0] + rotation[row][1] * shift[1] + rotation[row][2] * shift[2]),
        )
        for row in range(3)
    )
    return (rows[0], rows[1], rows[2], (0.0, 0.0, 0.0, 1.0))


def _body_inertia(body: dict) -> list[float] | None:
    """Onshape 的惯量读数 → 合约的 6 值顺序。

    每个标量都是 ``[标称, 下界, 上界]``，张量是同一张量连写三遍，因此只取前 9 个
    数（第 1 遍，行主序、关于质心）。
    """

    values = body.get("inertia")
    if not isinstance(values, (list, tuple)) or len(values) < 9:
        return None
    ixx, ixy, ixz, _, iyy, iyz, _, _, izz = (float(value) for value in values[:9])
    return [ixx, ixy, ixz, iyy, iyz, izz]


def _body_centroid(body: dict) -> list[float] | None:
    """Onshape 的质心读数（``[x,y,z, 下界, 上界]``）→ 标称质心。"""

    values = body.get("centroid")
    if not isinstance(values, (list, tuple)) or len(values) < 3:
        return None
    return [float(value) for value in values[:3]]


def _identifier(raw: Any, used: set[str], *, fallback: str) -> str:
    """来源显示名 → 合约允许的稳定标识（唯一，且只依赖输入顺序）。"""

    text = str(raw or "").split(" <", 1)[0].strip()
    cleaned = _INVALID_NAME_CHARS.sub("_", text).strip("_.-")
    if not cleaned:
        cleaned = fallback
    elif not re.match(r"[A-Za-z_]", cleaned):
        cleaned = f"{fallback}_{cleaned}"
    candidate, index = cleaned, 2
    while candidate in used:
        candidate = f"{cleaned}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _entity_key(path: Sequence[str]) -> str:
    """实例路径 → 快照内稳定且唯一的实体键。"""

    return "/".join(path)


# --- links ------------------------------------------------------------


def _part_links(
    collection: Collection, leaves: list[dict]
) -> tuple[list[dict], list[dict], dict[tuple[str, ...], str]]:
    """叶子零件实例 → links；返回（links, gaps, occurrence→link name）。

    ``joints[].parent/child`` 与 ``frames[].parent`` 按公共 schema 引用 link 的
    ``name``，因此映射里存名字；link 的 ``id`` 仍是来源实例键。
    """

    links: list[dict] = []
    gaps: list[dict] = []
    by_occurrence: dict[tuple[str, ...], str] = {}
    used_names: set[str] = set()
    for leaf in leaves:
        occurrence = tuple(leaf["occurrence"])
        entity = _entity_key(occurrence)
        if leaf.get("suppressed"):
            gaps.append({"kind": "instance_suppressed", "entity": entity})
            continue
        name = _identifier(leaf.get("name"), used_names, fallback="part")
        by_occurrence[occurrence] = name
        element_id = str(leaf.get("elementId") or "")
        part_id = str(leaf.get("partId") or "")
        bodies = collection.mass_properties.get(element_id, {}).get("bodies", {}) or {}
        body = bodies.get(part_id) or {}
        reading = collection.geometry_readings.get(part_id)
        # 零件 ID 可能含 "/" 或 "+"，落盘文件名用与缓存一致的净化名。
        mesh_file = f"geometry/parts/{safe_name(part_id)}.stl"
        link: dict[str, Any] = {
            "id": entity,
            "name": name,
            "inertial": None,
            "visuals": [],
            "collisions": [],
            "provenance": {
                "source_entities": [entity],
                "source_instance": list(occurrence),
                "source_name": leaf.get("name"),
                "source_part": {
                    "element_id": element_id,
                    "part_id": part_id,
                    "configuration": leaf.get("configuration"),
                    "document_microversion": leaf.get("documentMicroversion"),
                },
                "source_mass": f"raw/mass_properties_{element_id}.json#bodies/{part_id}",
                "source_geometry": mesh_file if reading else None,
                "geometry_source": (reading or {}).get("geometry_source") if reading else None,
                "collision_policy": "not_in_source",
                "frame": "part_studio_origin",
            },
        }
        mass = body.get("mass") or [None]
        inertia = _body_inertia(body)
        centroid = _body_centroid(body)
        if mass[0] is None or float(mass[0]) <= 0:
            gaps.append({"kind": "mass_reading_missing", "link": entity, "element_id": element_id, "part_id": part_id})
        elif inertia is None or centroid is None:
            gaps.append(
                {"kind": "inertia_reading_missing", "link": entity, "element_id": element_id, "part_id": part_id}
            )
        else:
            link["inertial"] = {
                "mass": float(mass[0]),
                "xyz": centroid,
                "rpy": [0.0, 0.0, 0.0],
                "inertia": inertia,
            }
        if reading:
            link["visuals"] = [
                {
                    "kind": "mesh",
                    "filename": mesh_file,
                    "scale": [1, 1, 1],
                    "xyz": [0, 0, 0],
                    "rpy": [0, 0, 0],
                }
            ]
        elif part_id not in collection.geometry:
            # 有字节但读数不合格的情况由采集层记 ``geometry_invalid``，这里不重复报。
            gaps.append({"kind": "geometry_missing", "link": entity, "element_id": element_id, "part_id": part_id})
        links.append(link)
    return links, gaps, by_occurrence


# --- joints / frames --------------------------------------------------


def _joints(assembly: Assembly, by_occurrence: dict[tuple[str, ...], str]) -> tuple[list[dict], list[dict], list[dict]]:
    """mate → joints/frames；不能唯一确定的量写进 gaps。"""

    joints: list[dict] = []
    frames: list[dict] = []
    gaps: list[dict] = []
    used_joint_names: set[str] = set()
    used_frame_names: set[str] = set()
    for mate in assembly.active_mates:
        if not mate.occurrences or mate.occurrences[0] not in by_occurrence:
            gaps.append(
                {
                    "kind": "mate_parent_unresolved",
                    "mate": mate.name,
                    "type": mate.mate_type,
                    "occurrences": [list(item) for item in mate.occurrences],
                }
            )
            continue
        parent_occurrence = mate.occurrences[0]
        try:
            origin, axis = assembly.mate_world_frame(mate, 0)
            local = linalg.matmul(
                _inverse(assembly.occurrence_transform(parent_occurrence)),
                linalg.from_axes((1, 0, 0), (0, 1, 0), axis, origin),
            )
        except (ValueError, KeyError) as error:
            gaps.append({"kind": "mate_frame_unresolved", "mate": mate.name, "reason": str(error)})
            continue
        common: dict[str, Any] = {
            "source_mate": mate.id,
            "source_mate_type": mate.mate_type,
            "source_mate_entities": [list(item) for item in mate.occurrences],
            "role": mate.role,
            "role_source": "mate_name_prefix",
            "axis_from": "mate_connector_z",
            "parent_child_from": "mate_entity_order",
        }
        if mate.is_frame:
            frames.append(
                {
                    "id": mate.id,
                    "name": _identifier(mate.joint_name, used_frame_names, fallback="frame"),
                    "parent": by_occurrence[parent_occurrence],
                    "xyz": list(linalg.translation(local)),
                    "rpy": _rotation_to_rpy(local),
                    "provenance": common,
                }
            )
            continue
        joint_type = MATE_TYPES.get(mate.mate_type)
        if joint_type is None:
            gaps.append({"kind": "mate_type_unsupported", "mate": mate.name, "type": mate.mate_type})
            continue
        child_link = by_occurrence.get(mate.occurrences[1]) if len(mate.occurrences) > 1 else None
        if child_link is None:
            gaps.append({"kind": "joint_child_unresolved", "mate": mate.name})
            continue
        limits: dict[str, float] = {}
        if mate.limits is not None:
            limits = {"lower": float(mate.limits[0]), "upper": float(mate.limits[1])}
        elif joint_type in ("revolute", "prismatic"):
            gaps.append({"kind": "joint_limits_missing", "mate": mate.name})
        joint: dict[str, Any] = {
            "id": mate.id,
            "name": _identifier(mate.joint_name, used_joint_names, fallback="joint"),
            "type": joint_type,
            "parent": by_occurrence[parent_occurrence],
            "child": child_link,
            "xyz": list(linalg.translation(local)),
            "rpy": _rotation_to_rpy(local),
            "axis": list(axis),
            "provenance": {
                **common,
                "limits_from": "assembly_features" if mate.limits is not None else None,
                "effort_velocity": "not_in_source",
            },
        }
        if limits:  # 公共 schema 不允许 limits: null；读不到就不写这个键并记 gap
            joint["limits"] = limits
        joints.append(joint)
    return joints, frames, gaps


# --- scene ------------------------------------------------------------


def build_scene(collection: Collection) -> dict:
    """来源采集结果 → ``description.scene/v1``（不含任何 URDF/物理判定）。"""

    assembly = Assembly.from_responses(
        collection.ref,
        collection.assemblies[collection.ref.element_id],
        collection.assembly_features[collection.ref.element_id],
        collection.mate_values[collection.ref.element_id],
    )
    all_leaves = assembly.leaf_instances()
    leaves = [leaf for leaf in all_leaves if not leaf.get("suppressed")]
    suppressed = [leaf for leaf in all_leaves if leaf.get("suppressed")]
    links, link_gaps, by_occurrence = _part_links(collection, leaves)
    joints, frames, mate_gaps = _joints(assembly, by_occurrence)
    occurrences = assembly.occurrence_paths()
    leaf_paths = {tuple(leaf["occurrence"]) for leaf in all_leaves}
    containers = [path for path in occurrences if tuple(path) not in leaf_paths]
    gaps = list(collection.gaps) + link_gaps + mate_gaps
    provenance: dict[str, Any] = {
        "provider": "onshape",
        "source": collection.ref.identity(),
        "configuration": collection.configuration,
        "microversion": collection.microversion,
        "element_ids": collection.element_ids(),
        # expected_occurrences 对账全部 CAD 实例；expected_entities 只列**参与模型**的物理零件实例：
        # 装配容器与抑制实例都单列在 non_physical 并给出依据，避免"声明了却不建模"。
        "expected_occurrences": [_entity_key(path) for path in occurrences],
        "expected_entities": [_entity_key(leaf["occurrence"]) for leaf in leaves],
        "non_physical": {
            "containers": [_entity_key(path) for path in containers],
            "suppressed": [_entity_key(leaf["occurrence"]) for leaf in suppressed],
            "basis": "occurrence 的实例类型是装配容器（Assembly），只组织结构、不承载质量；"
            "suppressed 为被抑制的零件实例，不进入模型",
        },
        "entity_counts": {
            "occurrences": len(occurrences),
            "part_instances": len(all_leaves),
            "assembly_instances": len(occurrences) - len(all_leaves),
            "suppressed_instances": len(suppressed),
            "links": len(links),
            "part_studios": len(collection.mass_properties),
        },
        "expected_joints": [mate.name for mate in assembly.active_mates],
        "subassemblies": sorted(collection.subassemblies),
        "notes": [
            "每个 link 对应一个叶子 Part 实例，link 帧 = 零件工作室坐标系；"
            "刚体分组（哪些叶子合成一个物理刚体）由机器人定义决定",
            "joint 的父子直接取 mate 的 matedEntities 顺序，方向与符号由机器人定义决定",
            "joint 轴向取 mate 连接器第 0 端的 z 轴；限位取装配体特征里的 mate 限位",
            "effort/velocity、碰撞策略与材质密度不在 CAD 里，由机器人定义提供",
        ],
        "gaps": gaps,
    }
    if assembly.unresolved_paths:
        provenance["unresolved_occurrences"] = [_entity_key(path) for path in assembly.unresolved_paths]
    return {
        "schema_version": SCHEMA_VERSION,
        "name": SCENE_NAME,
        "units": "SI",
        "links": links,
        "joints": joints,
        "frames": frames,
        "actuators": [],
        "sensors": [],
        "constraints": [],
        "control": {},
        "contact_excludes": [],
        "provenance": provenance,
    }
