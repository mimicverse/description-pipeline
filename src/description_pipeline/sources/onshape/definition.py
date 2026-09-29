"""机器人定义：只有这里显式写下的语义才会被应用到来源场景上。

定义来自作者文件（`config/robot.yaml` 的 ``robot`` 映射），不是来源采集配置，
因此不需要重新访问 API；每个字段都可以带 ``source`` 说明它的依据。解析严格：
未知键、重复名字、引用不存在的 link 都会直接失败（``onshape_definition_invalid``），
避免"看起来像定义"的配置静默改变模型。
"""

from __future__ import annotations

import re
from math import isfinite
from dataclasses import dataclass, field
from typing import Any, NoReturn

from .errors import DEFINITION_INVALID, OnshapeSourceError

SCHEMA = "description.robot-definition/v1"
PROVIDER = "onshape"
AXIS_SOURCES = {"mate_connector_z"}
LIMIT_SOURCES = {"mate_limits", "none"}
COLLISION_POLICIES = {"undefined"}
_AXIS_KEYS = {"source", "sign"}
_LIMIT_KEYS = {"source", "lower", "upper"}
_ZERO_KEYS = {"value", "source"}
_COLLISION_KEYS = {"policy", "note", "contact_excludes_source"}
_EFFORT_KEYS = {"defined", "note"}
_EVIDENCE_KEYS = {"reason", "basis", "file", "derived_from", "reference", "date", "name"}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_LINK_KEYS = {"name", "members", "reference", "source"}
_JOINT_KEYS = {"name", "mate", "parent", "child", "type", "axis", "limits", "zero", "source"}
_FRAME_KEYS = {"name", "link", "mate", "parent", "source"}
_MASS_KEYS = {"part_ids", "names", "density_kg_m3", "source"}
_NON_PHYSICAL_KEYS = {"entities", "basis", "detail", "evidence"}
_ROBOT_KEYS = {
    "schema",
    "provider",
    "root",
    "reference",
    "density_default_kg_m3",
    "links",
    "joints",
    "frames",
    "mass",
    "non_physical",
    "collision",
    "effort_velocity",
}


@dataclass(frozen=True)
class LinkGroup:
    name: str
    members: tuple[str, ...]
    reference: str
    source: dict = field(default_factory=dict)


@dataclass(frozen=True)
class JointSpec:
    name: str
    mate: str
    parent: str
    child: str
    type: str
    axis_sign: int
    limits: dict[str, Any]
    zero: float
    source: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FrameSpec:
    name: str
    link: str
    mate: str
    parent: str
    source: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MassOverride:
    part_ids: tuple[str, ...]
    names: tuple[str, ...]
    density_kg_m3: float
    source: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RobotDefinition:
    provider: str
    root: str
    links: tuple[LinkGroup, ...]
    joints: tuple[JointSpec, ...]
    frames: tuple[FrameSpec, ...]
    mass_overrides: tuple[MassOverride, ...]
    non_physical: tuple[dict, ...]
    density_default_kg_m3: float | None
    reference: dict
    collision: dict
    effort_velocity: dict
    link_names: tuple[str, ...]
    frame_links: tuple[str, ...]


def _fail(message: str, detail: dict | None = None) -> NoReturn:
    raise OnshapeSourceError(DEFINITION_INVALID, message, detail)


def _mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        _fail(f"{where} 必须是映射", {"where": where, "got": type(value).__name__})
    return value


def _sequence(value: Any, where: str) -> list:
    if not isinstance(value, list):
        _fail(f"{where} 必须是列表", {"where": where, "got": type(value).__name__})
    return value


def _reject_unknown(payload: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        _fail(f"{where} 出现未定义字段", {"where": where, "unknown": unknown})


def _name(payload: dict, where: str) -> str:
    value = payload.get("name")
    if not isinstance(value, str) or not _NAME.match(value):
        _fail(f"{where}.name 必须是合法标识", {"where": where, "name": value})
    return str(value)


def _strings(value: Any, where: str) -> tuple[str, ...]:
    items = _sequence(value, where)
    if not items or not all(isinstance(item, str) and item for item in items):
        _fail(f"{where} 必须是非空字符串列表", {"where": where})
    return tuple(items)


def _finite(value: Any, where: str, *, positive: bool) -> float:
    if not isinstance(value, (int, float)) or not isfinite(float(value)):
        _fail(f"{where} 必须是有限数", {"where": where, "value": value})
    number = float(value)
    if positive and number <= 0:
        _fail(f"{where} 必须是正数", {"where": where, "value": value})
    return number


def _evidence(payload: dict, where: str) -> dict:
    """质量覆盖必须带可复核依据：空 source 或没有说明键一律拒绝。"""

    source = dict(_mapping(payload.get("source", {}), f"{where}.source"))
    if not source:
        _fail(f"{where}.source 不能为空：改变物理必须写清依据", {"where": where})
    described = [key for key in _EVIDENCE_KEYS if isinstance(source.get(key), str) and source[key].strip()]
    if not described:
        _fail(
            f"{where}.source 缺少说明性字段（reason/basis/file/derived_from/reference）",
            {"where": where, "source": sorted(source)},
        )
    return source


def _declared_mapping(value: Any, allowed_keys: set[str], where: str, supported: set[Any], field: str) -> dict:
    """只允许已实现的取值：声明了却不生效的字段必须失败，而不是被忽略。"""

    payload = dict(_mapping(value, where))
    _reject_unknown(payload, allowed_keys, where)
    declared = payload.get(field)
    if declared is not None and declared not in supported:
        _fail(
            f"{where}.{field} 暂不支持的取值",
            {"where": where, field: declared, "supported": sorted(supported)},
        )
    return payload


def _provider(robot: dict) -> str:
    provider = robot.get("provider")
    if provider != PROVIDER:
        _fail("robot.provider 必须是本来源", {"provider": provider, "expected": PROVIDER})
    return PROVIDER


def parse_robot_definition(definition: dict) -> RobotDefinition:
    """作者文件 → 定义对象；`definition['robot']` 缺失即视为没有定义。"""

    robot = _mapping(definition, "definition").get("robot")
    if robot is None:
        _fail("缺少 robot 定义（作者文件的 robot 映射）", {"expected": "definition.robot"})
    robot = _mapping(robot, "robot")
    _reject_unknown(robot, _ROBOT_KEYS, "robot")
    schema = robot.get("schema")
    if schema != SCHEMA:
        _fail("robot.schema 不符合本来源定义版本", {"schema": schema, "expected": SCHEMA})
    root = _name({"name": robot.get("root")}, "robot.root")

    links: list[LinkGroup] = []
    for index, payload in enumerate(_sequence(robot.get("links"), "robot.links")):
        where = f"robot.links[{index}]"
        payload = _mapping(payload, where)
        _reject_unknown(payload, _LINK_KEYS, where)
        members = _strings(payload.get("members"), f"{where}.members")
        reference = payload.get("reference", members[0])
        if not isinstance(reference, str) or not reference:
            _fail(f"{where}.reference 必须是实体键", {"where": where})
        links.append(
            LinkGroup(
                name=_name(payload, where),
                members=members,
                reference=str(reference),
                source=dict(_mapping(payload.get("source", {}), f"{where}.source")),
            )
        )

    frames: list[FrameSpec] = []
    for index, payload in enumerate(_sequence(robot.get("frames", []), "robot.frames")):
        where = f"robot.frames[{index}]"
        payload = _mapping(payload, where)
        _reject_unknown(payload, _FRAME_KEYS, where)
        frames.append(
            FrameSpec(
                name=_name(payload, where),
                link=_name({"name": payload.get("link")}, f"{where}.link"),
                mate=str(payload.get("mate") or ""),
                parent=_name({"name": payload.get("parent")}, f"{where}.parent"),
                source=dict(_mapping(payload.get("source", {}), f"{where}.source")),
            )
        )

    joints: list[JointSpec] = []
    for index, payload in enumerate(_sequence(robot.get("joints", []), "robot.joints")):
        where = f"robot.joints[{index}]"
        payload = _mapping(payload, where)
        _reject_unknown(payload, _JOINT_KEYS, where)
        axis = _declared_mapping(
            payload.get("axis", {"source": "mate_connector_z"}), _AXIS_KEYS, f"{where}.axis", AXIS_SOURCES, "source"
        )
        limits = _declared_mapping(
            payload.get("limits", {"source": "mate_limits"}),
            _LIMIT_KEYS,
            f"{where}.limits",
            LIMIT_SOURCES,
            "source",
        )
        zero = dict(_mapping(payload.get("zero", {"value": 0.0}), f"{where}.zero"))
        _reject_unknown(zero, _ZERO_KEYS, f"{where}.zero")
        sign = axis.get("sign", 1)
        if sign not in (1, -1):
            _fail(f"{where}.axis.sign 只能是 +1/-1", {"where": where, "sign": sign})
        zero_value = _finite(zero.get("value", 0.0), f"{where}.zero.value", positive=False)
        if "source" in zero and (not isinstance(zero["source"], str) or not zero["source"].strip()):
            _fail(f"{where}.zero.source 必须说明零位依据", {"where": where})
        explicit_limits = "lower" in limits or "upper" in limits
        if explicit_limits and ("lower" not in limits or "upper" not in limits):
            _fail(f"{where}.limits 显式区间必须同时给出 lower 与 upper", {"where": where})
        if explicit_limits and limits.get("source") == "mate_limits":
            _fail(f"{where}.limits 不能同时声明 mate_limits 与显式区间", {"where": where})
        if explicit_limits:
            lower = _finite(limits["lower"], f"{where}.limits.lower", positive=False)
            upper = _finite(limits["upper"], f"{where}.limits.upper", positive=False)
            if lower >= upper:
                _fail(f"{where}.limits 区间必须 lower < upper", {"where": where, "lower": lower, "upper": upper})
        if limits.get("source") == "none" and explicit_limits:
            _fail(f"{where}.limits 声明 none 时不能再给区间", {"where": where})
        if not payload.get("mate"):
            _fail(f"{where}.mate 必须给出来源 mate", {"where": where})
        joints.append(
            JointSpec(
                name=_name(payload, where),
                mate=str(payload.get("mate") or ""),
                parent=_name({"name": payload.get("parent")}, f"{where}.parent"),
                child=_name({"name": payload.get("child")}, f"{where}.child"),
                type=str(payload.get("type") or "revolute"),
                axis_sign=int(sign),
                limits=dict(limits),
                zero=zero_value,
                source=dict(_mapping(payload.get("source", {}), f"{where}.source")),
            )
        )

    overrides: list[MassOverride] = []
    mass = _mapping(robot.get("mass", {}), "robot.mass")
    _reject_unknown(mass, {"overrides", "density_default_kg_m3"}, "robot.mass")
    for index, payload in enumerate(_sequence(mass.get("overrides", []), "robot.mass.overrides")):
        where = f"robot.mass.overrides[{index}]"
        payload = _mapping(payload, where)
        _reject_unknown(payload, _MASS_KEYS, where)
        density = _finite(payload.get("density_kg_m3"), f"{where}.density_kg_m3", positive=True)
        if not payload.get("part_ids") and not payload.get("names"):
            _fail(f"{where} 必须给出 part_ids 或 names", {"where": where})
        overrides.append(
            MassOverride(
                part_ids=_strings(payload["part_ids"], f"{where}.part_ids") if payload.get("part_ids") else (),
                names=_strings(payload["names"], f"{where}.names") if payload.get("names") else (),
                density_kg_m3=density,
                source=_evidence(payload, where),
            )
        )

    non_physical: list[dict] = []
    for index, payload in enumerate(_sequence(robot.get("non_physical", []), "robot.non_physical")):
        where = f"robot.non_physical[{index}]"
        payload = _mapping(payload, where)
        _reject_unknown(payload, _NON_PHYSICAL_KEYS, where)
        entries = _strings(payload.get("entities"), f"{where}.entities")
        basis = payload.get("basis")
        if not isinstance(basis, str) or not basis:
            _fail(f"{where}.basis 必须说明这些实体为什么不是物理零件", {"where": where})
        evidence = payload.get("evidence")
        if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
            _fail(f"{where}.evidence 必须是快照内相对路径", {"where": where, "evidence": evidence})
        non_physical.append(
            {
                "entities": list(entries),
                "basis": basis,
                "detail": payload.get("detail"),
                "evidence": evidence,
            }
        )

    link_names = tuple(link.name for link in links)
    frame_links = tuple(frame.link for frame in frames)
    if len(set(link_names)) != len(link_names) or len(set(frame_links)) != len(frame_links):
        _fail("link 名字重复", {"links": list(link_names), "frames": list(frame_links)})
    known = set(link_names) | set(frame_links)
    if root not in known:
        _fail("robot.root 不在 link 列表里", {"root": root})
    for spec in joints:
        if spec.parent not in known or spec.child not in known:
            _fail("joint 引用了未定义的 link", {"joint": spec.name, "parent": spec.parent, "child": spec.child})
    for frame in frames:
        if frame.parent not in known:
            _fail("frame 引用了未定义的父 link", {"frame": frame.name, "parent": frame.parent})

    density_default = mass.get("density_default_kg_m3", robot.get("density_default_kg_m3"))
    resolved_density = (
        _finite(density_default, "robot.mass.density_default_kg_m3", positive=True)
        if density_default is not None
        else None
    )
    collision = _declared_mapping(
        robot.get("collision", {}), _COLLISION_KEYS, "robot.collision", COLLISION_POLICIES, "policy"
    )
    effort_velocity = _declared_mapping(
        robot.get("effort_velocity", {}), _EFFORT_KEYS, "robot.effort_velocity", {False}, "defined"
    )
    if effort_velocity.get("defined", False) not in (False, None):
        _fail(
            "robot.effort_velocity.defined=true 暂不支持：本层不写动力额定参数",
            {"defined": effort_velocity.get("defined")},
        )
    return RobotDefinition(
        provider=_provider(robot),
        root=root,
        links=tuple(links),
        joints=tuple(joints),
        frames=tuple(frames),
        mass_overrides=tuple(overrides),
        non_physical=tuple(non_physical),
        density_default_kg_m3=resolved_density,
        reference=dict(_mapping(robot.get("reference", {}), "robot.reference")),
        collision=collision,
        effort_velocity=effort_velocity,
        link_names=link_names,
        frame_links=frame_links,
    )
