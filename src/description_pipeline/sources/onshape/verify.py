"""独立校验：规范化产物 vs 原始装配与质量读数。

这里的算法**不复用** ``normalize`` 的融合/定位实现：实例集合、质量、质心与惯量都从
``raw/assembly_*.json`` + ``raw/mass_properties_*.json`` 现算，模型侧只做 q=0 正向运动学
把这些量搬到同一坐标系再比较，因此能抓到"融合自洽但物理错误"的问题。

入口 :func:`verify_normalization` 返回公共 ``result`` 形状的检查列表
（``id/version/status/expected/checked/missing/details``），可直接接进公共 ``assess``：

* ``source.occurrences`` —— 原始实例/零件/容器集合与计数
* ``source.entities`` —— 原始物理零件 = 已覆盖 + 显式排除
* ``source.exclusions`` —— 排除项必须有实际消费方或独立文件证据，并给出物理影响
* ``source.mass_conservation`` —— 世界系 Σm / Σm·c / 完整张量 与模型 FK 对账
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Sequence

from . import linalg
from .assembly import Assembly
from .collector import safe_name
from .definition import RobotDefinition, parse_robot_definition
from .errors import EVIDENCE_MISSING, OnshapeSourceError
from .evidence import evidence_problems
from .reference import parse_reference
from ...io import confined

VERSION = 1
#: 两侧都用同一份原始读数与同一份定义，差异只来自浮点求和顺序；容差仍留足量级。
MASS_ATOL, MASS_RTOL = 1e-12, 1e-9
COM_ATOL = 1e-9
INERTIA_ATOL, INERTIA_RTOL = 1e-12, 1e-6


def _result(
    code: str,
    passed: bool,
    *,
    expected: Iterable[str] = (),
    checked: Iterable[str] = (),
    details: Any = None,
    status: str | None = None,
) -> dict:
    expected_list = sorted(str(item) for item in expected)
    checked_list = sorted(str(item) for item in checked)
    missing = sorted(set(expected_list) - set(checked_list))
    return {
        "id": code,
        "version": VERSION,
        "status": status or ("passed" if passed and not missing else "failed"),
        "expected": expected_list,
        "checked": checked_list,
        "missing": missing,
        "details": {} if details is None else details,
    }


def _entity(path: Sequence[str]) -> str:
    return "/".join(path)


class _RawEvidence:
    """原始装配 + 质量读数的现算视图（不依赖 normalize 的任何实现）。"""

    def __init__(self, scene: dict, root: Path) -> None:
        self.root = Path(root)
        manifest_path = self.root / "manifest.json"
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        source = scene.get("provenance", {}).get("source", {})
        element = source.get("element_id")
        url = source.get("url")
        if not element or not url:
            raise OnshapeSourceError(EVIDENCE_MISSING, "来源场景缺少 element_id 或 URL", {})
        payloads: dict[str, dict] = {}
        for kind in ("assembly", "assembly_features", "mate_values"):
            path = self.root / "raw" / f"{kind}_{element}.json"
            if not path.is_file():
                raise OnshapeSourceError(
                    EVIDENCE_MISSING,
                    "校验缺少原始响应",
                    {"missing": f"raw/{kind}_{element}.json", "snapshot": str(self.root)},
                )
            payloads[kind] = json.loads(path.read_text(encoding="utf-8"))
        self.assembly = Assembly.from_responses(
            parse_reference(url),
            payloads["assembly"],
            payloads["assembly_features"],
            payloads["mate_values"],
        )
        self.occurrences = [tuple(path) for path in self.assembly.occurrence_paths()]
        leaves = self.assembly.leaf_instances()
        self.suppressed = {tuple(leaf["occurrence"]): leaf for leaf in leaves if leaf.get("suppressed")}
        self.leaves = {tuple(leaf["occurrence"]): leaf for leaf in leaves if not leaf.get("suppressed")}
        self.unresolved = [list(path) for path in self.assembly.unresolved_paths]
        # partId 只在零件工作室内唯一：读数按 (element, partId) 作用域保存，
        # 跨工作室同名零件不会互相覆盖；同一作用域出现两份不同读数则记录冲突。
        self.bodies: dict[str, dict[str, dict]] = {}
        self.body_conflicts: list[dict] = []
        for path in sorted((self.root / "raw").glob("mass_properties_*.json")):
            element_id = path.stem.removeprefix("mass_properties_")
            payload = json.loads(path.read_text(encoding="utf-8"))
            scoped = self.bodies.setdefault(element_id, {})
            for part_id, body in (payload.get("bodies") or {}).items():
                previous = scoped.get(part_id)
                if previous is not None and previous != body:
                    self.body_conflicts.append({"element_id": element_id, "part_id": part_id})
                scoped[part_id] = body

    @property
    def occurrence_keys(self) -> set[str]:
        return {_entity(path) for path in self.occurrences}

    @property
    def entity_keys(self) -> set[str]:
        """参与模型的物理零件实例（不含被抑制的）。"""

        return {_entity(path) for path in self.leaves}

    @property
    def suppressed_keys(self) -> set[str]:
        return {_entity(path) for path in self.suppressed}

    @property
    def part_instance_keys(self) -> set[str]:
        """全部零件实例（含被抑制的），用于与 entity_counts.part_instances 对账。"""

        return self.entity_keys | self.suppressed_keys

    @property
    def container_keys(self) -> set[str]:
        """装配容器（非零件实例）：抑制零件既不是容器，也不算在模型实体里。"""

        return self.occurrence_keys - self.part_instance_keys

    def transform(self, path: Sequence[str]) -> linalg.Matrix:
        return self.assembly.occurrence_transform(list(path))

    def path_of(self, entity: str) -> list[str] | None:
        for path in self.leaves:
            if _entity(path) == entity:
                return list(path)
        return None

    def body_for(self, element_id: Any, part_id: Any) -> dict | None:
        """按 (element, partId) 取读数，与生成侧同作用域。"""

        return (self.bodies.get(str(element_id)) or {}).get(str(part_id))

    def bound_digest(self, relative: str) -> str | None:
        """证据文件必须落在快照内且出现在清单里；返回清单摘要，否则 None。"""

        files = self.manifest.get("files") or {}
        try:
            confined(self.root, relative)
        except ValueError:
            return None
        return str(files[relative]) if relative in files else None


def _occurrences(scene: dict, raw: _RawEvidence) -> dict:
    provenance = scene.get("provenance", {})
    declared_occurrences = list(provenance.get("expected_occurrences", []))
    declared_entities = list(provenance.get("expected_entities", []))
    declared_containers = list(provenance.get("non_physical", {}).get("containers", []))
    declared_suppressed = list(provenance.get("non_physical", {}).get("suppressed", []))
    counts = provenance.get("entity_counts", {}) or {}
    problems = {
        "occurrences_missing": sorted(raw.occurrence_keys - set(declared_occurrences)),
        "occurrences_extra": sorted(set(declared_occurrences) - raw.occurrence_keys),
        "entities_missing": sorted(raw.entity_keys - set(declared_entities)),
        "entities_extra": sorted(set(declared_entities) - raw.entity_keys),
        "containers_mismatch": sorted(set(declared_containers) ^ raw.container_keys),
        "suppressed_mismatch": sorted(set(declared_suppressed) ^ raw.suppressed_keys),
        "counts_mismatch": [
            key
            for key, value in {
                "occurrences": len(raw.occurrence_keys),
                "part_instances": len(raw.part_instance_keys),
                "assembly_instances": len(raw.container_keys),
                "suppressed_instances": len(raw.suppressed_keys),
            }.items()
            if counts.get(key) != value
        ],
        "unresolved_paths": raw.unresolved,
        "duplicate_declared": (
            len(declared_occurrences) != len(set(declared_occurrences))
            or len(declared_entities) != len(set(declared_entities))
        ),
    }
    passed = not any(problems[key] for key in problems)
    return _result(
        "source.occurrences",
        passed,
        expected=sorted(raw.occurrence_keys | raw.part_instance_keys),
        checked=declared_occurrences,
        details={**problems, "counts": counts},
    )


def _entities(raw: _RawEvidence, canonical: dict) -> dict:
    physical = [
        link for link in canonical.get("links", []) if link.get("provenance", {}).get("kind") != "reference_frame"
    ]
    covered = [str(entity) for link in physical for entity in link.get("provenance", {}).get("source_entities", [])]
    excluded = [
        str(entry.get("id"))
        for entry in canonical.get("provenance", {}).get("excluded_entities", [])
        if entry.get("id")
    ]
    duplicates = sorted({item for item in covered if covered.count(item) > 1})
    overlap = sorted(set(covered) & set(excluded))
    closed = set(covered) | set(excluded)
    passed = (
        closed == raw.entity_keys
        and not duplicates
        and not overlap
        and len(covered + excluded) == len(set(covered + excluded))
    )
    return _result(
        "source.entities",
        passed,
        expected=sorted(raw.entity_keys),
        checked=sorted(closed),
        details={
            "missing": sorted(raw.entity_keys - closed),
            "extra": sorted(closed - raw.entity_keys),
            "duplicates": duplicates,
            "covered_excluded_overlap": overlap,
            "covered": len(covered),
            "excluded": len(excluded),
        },
    )


def _exclusions(canonical: dict, raw: _RawEvidence, robot: RobotDefinition) -> dict:
    """排除项必须：有真实消费方或快照内被绑定的证据，且质量/零件/几何由 raw 独立复算一致。"""

    frame_links = {
        link["name"]
        for link in canonical.get("links", [])
        if link.get("provenance", {}).get("kind") == "reference_frame"
    }
    frame_joints = {
        joint["name"]
        for joint in canonical.get("joints", [])
        if joint.get("provenance", {}).get("kind") == "reference_frame"
    }
    link_by_name = {link["name"]: link for link in canonical.get("links", [])}
    joint_by_name = {joint["name"]: joint for joint in canonical.get("joints", [])}
    mates = {mate.name: mate for mate in raw.assembly.active_mates}
    entries = canonical.get("provenance", {}).get("excluded_entities", [])
    justified: list[str] = []
    rejected: list[dict] = []
    impact: list[dict] = []
    for entry in entries:
        entity = str(entry.get("id"))
        evidence = entry.get("evidence") or {}
        consumers = evidence.get("consumed_by") or []
        problems: list[str] = []
        for item in consumers:
            if not isinstance(item, dict):
                problems.append("consumer_not_object")
                continue
            link_name, joint_name, mate_name = item.get("link"), item.get("joint"), item.get("mate")
            if link_name not in frame_links:
                problems.append(f"unknown_frame_link:{link_name}")
            if joint_name not in frame_joints:
                problems.append(f"unknown_frame_joint:{joint_name}")
            joint = joint_by_name.get(str(joint_name))
            link = link_by_name.get(str(link_name))
            if joint is not None and link is not None and joint.get("child") != link.get("name"):
                problems.append(f"consumer_pair_mismatch:{joint_name}!={link_name}")
            mate = mates.get(str(mate_name))
            if mate is None:
                problems.append(f"unknown_mate:{mate_name}")
            elif not any(_entity(occurrence) == entity for occurrence in mate.occurrences):
                problems.append(f"mate_does_not_consume:{mate_name}")
        file_path = evidence.get("file")
        file_digest = raw.bound_digest(str(file_path)) if file_path else None
        if file_path and file_digest is None:
            problems.append("evidence_file_not_bound")
        claimed_digest = evidence.get("sha256")
        if file_digest is not None and claimed_digest not in (None, file_digest):
            problems.append("evidence_digest_mismatch")
        path = raw.path_of(entity)
        leaf = raw.leaves.get(tuple(path)) if path else None
        if file_digest is not None:
            # Identity comes from raw assembly evidence, not the candidate's claim.
            problems.extend(
                evidence_problems(
                    raw.root,
                    str(file_path),
                    entity,
                    str((leaf or {}).get("partId") or ""),
                    str((leaf or {}).get("elementId") or ""),
                )
            )
        recomputed_mass: float | None = None
        raw_part_id: str | None = None
        geometry_expected: str | None = None
        if leaf is None:
            problems.append("entity_not_a_raw_leaf")
        else:
            raw_part_id = str(leaf.get("partId") or "")
            body = raw.body_for(leaf.get("elementId"), raw_part_id)
            name = str(leaf.get("name") or "").split(" <", 1)[0]
            quantity = _member_quantity(body, _density_rule(robot, raw_part_id, name)) if body else None
            if quantity is None:
                problems.append("raw_mass_reading_missing")
            else:
                recomputed_mass = quantity[0]
                if (raw.root / f"geometry/parts/{safe_name(raw_part_id)}.stl").is_file():
                    geometry_expected = f"geometry/parts/{safe_name(raw_part_id)}.stl"
        claimed_mass = entry.get("mass_kg")
        mass_declared = "mass_kg" in entry
        if recomputed_mass is None:
            # 已排除的 active 实例必须能算出排除影响；算不出来就是"未知影响"，不能当验收通过。
            problems.append("impact_not_recomputable")
        elif not isinstance(claimed_mass, (int, float)) or abs(float(claimed_mass) - recomputed_mass) > (
            1e-12 + 1e-9 * max(abs(recomputed_mass), 1.0)
        ):
            problems.append("mass_claim_mismatch")
        if not mass_declared:
            problems.append("mass_not_declared")
        if raw_part_id is not None and str(entry.get("part_id") or "") != raw_part_id:
            problems.append("part_id_claim_mismatch")
        geometry_claim = entry.get("source_geometry")
        if geometry_claim:
            if raw.bound_digest(str(geometry_claim)) is None:
                problems.append("geometry_not_in_inventory")
            if geometry_expected is not None and str(geometry_claim) != geometry_expected:
                problems.append("geometry_claim_mismatch")
        if not consumers and file_digest is None:
            problems.append("no_consumer_and_no_evidence")
        if problems:
            rejected.append(
                {
                    "entity": entity,
                    "reason": entry.get("reason"),
                    "problems": sorted(set(problems)),
                }
            )
            continue
        justified.append(entity)
        impact.append(
            {
                "entity": entity,
                "reason": entry.get("reason"),
                "mass_kg": entry.get("mass_kg"),
                "recomputed_mass_kg": recomputed_mass,
                "part_id": entry.get("part_id"),
                "source_geometry": entry.get("source_geometry"),
                "consumed_by": consumers,
                "evidence_file": file_path,
                "evidence_sha256": file_digest,
            }
        )
    total_mass = sum(float(item["mass_kg"]) for item in impact if isinstance(item["mass_kg"], (int, float)))
    passed = len(justified) == len(entries) and not rejected
    return _result(
        "source.exclusions",
        passed,
        expected=[str(entry.get("id")) for entry in entries],
        checked=justified,
        details={
            "rejected": rejected,
            "frame_links": sorted(frame_links),
            "declared": len(entries),
            "excluded_mass_total_kg": total_mass,
            "impact": impact,
            "not_machine_checked": "robot.reference 里的声明性数字未由流水线复算",
        },
    )


def _rpy_matrix(rpy: Sequence[float]) -> list[list[float]]:
    roll, pitch, yaw = (float(value) for value in rpy)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def _rotation(matrix: linalg.Matrix) -> list[list[float]]:
    return [[matrix[row][column] for column in range(3)] for row in range(3)]


def _transpose(rotation: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[rotation[column][row] for column in range(3)] for row in range(3)]


def _apply(matrix: linalg.Matrix, point: Sequence[float]) -> list[float]:
    return [sum(matrix[row][column] * point[column] for column in range(3)) + matrix[row][3] for row in range(3)]


def _rotate(tensor: Sequence[Sequence[float]], rotation: Sequence[Sequence[float]]) -> list[list[float]]:
    return [
        [
            sum(rotation[row][i] * tensor[i][j] * rotation[column][j] for i in range(3) for j in range(3))
            for column in range(3)
        ]
        for row in range(3)
    ]


def _tensor(values: Sequence[float]) -> list[list[float]]:
    xx, xy, xz, yy, yz, zz = (float(value) for value in values)
    return [[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]]


def _invert(matrix: linalg.Matrix) -> linalg.Matrix:
    rotation = [[matrix[row][column] for row in range(3)] for column in range(3)]
    origin = linalg.translation(matrix)
    rows = tuple(
        (
            rotation[0][row],
            rotation[1][row],
            rotation[2][row],
            -(rotation[0][row] * origin[0] + rotation[1][row] * origin[1] + rotation[2][row] * origin[2]),
        )
        for row in range(3)
    )
    return (rows[0], rows[1], rows[2], (0.0, 0.0, 0.0, 1.0))


def _pose(rotation: Sequence[Sequence[float]], xyz: Sequence[float]) -> linalg.Matrix:
    """由旋转矩阵与平移构造 4×4（URDF 的 origin：先平移后旋转，均在父坐标系表达）。"""

    return (
        (float(rotation[0][0]), float(rotation[0][1]), float(rotation[0][2]), float(xyz[0])),
        (float(rotation[1][0]), float(rotation[1][1]), float(rotation[1][2]), float(xyz[1])),
        (float(rotation[2][0]), float(rotation[2][1]), float(rotation[2][2]), float(xyz[2])),
        (0.0, 0.0, 0.0, 1.0),
    )


def _canonical_frames(canonical: dict) -> dict[str, linalg.Matrix]:
    children = {joint["child"]: joint for joint in canonical.get("joints", [])}
    roots = [link["name"] for link in canonical.get("links", []) if link["name"] not in children]
    frames: dict[str, linalg.Matrix] = {}

    def walk(name: str, matrix: linalg.Matrix) -> None:
        frames[name] = matrix
        for joint in canonical.get("joints", []):
            if joint["parent"] != name:
                continue
            rotation = _rpy_matrix(joint["rpy"])
            local = _pose(rotation, joint["xyz"])
            walk(joint["child"], linalg.matmul(matrix, local))

    if len(roots) == 1:
        walk(roots[0], linalg.identity())
    return frames


def _density_rule(robot: RobotDefinition, part_id: str, name: str) -> float | None:
    for override in robot.mass_overrides:
        if part_id in override.part_ids or name in override.names:
            return override.density_kg_m3
    return robot.density_default_kg_m3


def _member_quantity(body: dict, density: float | None) -> tuple[float, list[float], list[list[float]]] | None:
    mass = (body.get("mass") or [None])[0]
    centroid = (body.get("centroid") or [])[:3]
    tensor = (body.get("inertia") or [])[:9]
    if not mass or len(centroid) != 3 or len(tensor) != 9:
        return None
    values = [float(tensor[index]) for index in (0, 1, 2, 4, 5, 8)]
    mass = float(mass)
    volume = float((body.get("volume") or [0.0])[0] or 0.0)
    if density and volume > 0:
        target = density * volume
        scale = target / mass
        values = [value * scale for value in values]
        mass = target
    return mass, [float(value) for value in centroid], _tensor(values)


def _conservation(raw: _RawEvidence, robot: RobotDefinition, canonical: dict) -> dict:
    root_reference = next(group.reference for group in robot.links if group.name == robot.root)
    root_frame = raw.transform(raw.path_of(root_reference) or [root_reference])
    inverse_root = _invert(root_frame)
    frames = _canonical_frames(canonical)
    expected_links = [link["name"] for link in canonical.get("links", []) if link.get("inertial") is not None]
    report: list[dict] = []
    failures: list[str] = []
    raw_total = 0.0
    for link in canonical.get("links", []):
        inertial = link.get("inertial")
        if inertial is None:
            continue
        link_name = link["name"]
        entities = [str(item) for item in link.get("provenance", {}).get("source_entities", [])]
        members: list[dict] = []
        for entity in entities:
            path = raw.path_of(entity)
            leaf = raw.leaves.get(tuple(path)) if path else None
            if leaf is None or path is None:
                failures.append(f"{link_name}:{entity}:unresolved")
                continue
            part_id = str(leaf.get("partId") or "")
            body = raw.body_for(leaf.get("elementId"), part_id)
            if not body:
                failures.append(f"{link_name}:{entity}:no-body")
                continue
            name = str(leaf.get("name") or "").split(" <", 1)[0]
            quantity = _member_quantity(body, _density_rule(robot, part_id, name))
            if quantity is None:
                failures.append(f"{link_name}:{entity}:no-reading")
                continue
            mass, com, tensor = quantity
            transform = raw.transform(path)
            members.append(
                {
                    "mass": mass,
                    "com": _apply(transform, com),
                    "inertia": _rotate(tensor, _rotation(transform)),
                }
            )
        if not members:
            failures.append(f"{link_name}:no-members")
            continue
        total = sum(item["mass"] for item in members)
        raw_total += total
        world_com = [0.0, 0.0, 0.0]
        for item in members:
            for axis in range(3):
                world_com[axis] += item["mass"] * item["com"][axis]
        world_com = [value / total for value in world_com]
        world_inertia = [[0.0] * 3 for _ in range(3)]
        for item in members:
            delta = [item["com"][axis] - world_com[axis] for axis in range(3)]
            offset = sum(value * value for value in delta)
            for row in range(3):
                for column in range(3):
                    parallel = item["mass"] * ((offset if row == column else 0.0) - delta[row] * delta[column])
                    world_inertia[row][column] += item["inertia"][row][column] + parallel
        raw_com = _apply(inverse_root, world_com)
        raw_tensor = _rotate(world_inertia, _rotation(inverse_root))
        frame = frames.get(link_name)
        if frame is None:
            failures.append(f"{link_name}:no-frame")
            continue
        model_com = _apply(frame, inertial["xyz"])
        # inertial.inertia 表达在 inertial.rpy 描述的惯量系里：先转到 link 系，再由 FK 转到参考系。
        link_tensor = _rotate(_tensor(inertial["inertia"]), _rpy_matrix(inertial.get("rpy", (0.0, 0.0, 0.0))))
        model_tensor = _rotate(link_tensor, _rotation(frame))
        model_mass = float(inertial["mass"])
        mass_error = abs(total - model_mass)
        com_error = math.dist(raw_com, model_com)
        inertia_error = math.sqrt(
            sum((raw_tensor[row][column] - model_tensor[row][column]) ** 2 for row in range(3) for column in range(3))
        )
        # 容限量级只取两侧张量自身的 Frobenius 范数：不把 kg 质量当成 kg·m² 的量级。
        scale = max(
            math.sqrt(sum(value * value for row in raw_tensor for value in row)),
            math.sqrt(sum(value * value for row in model_tensor for value in row)),
        )
        inertia_tolerance = INERTIA_ATOL + INERTIA_RTOL * scale
        ok = (
            mass_error <= MASS_ATOL + MASS_RTOL * abs(model_mass)
            and com_error <= COM_ATOL
            and inertia_error <= inertia_tolerance
        )
        if not ok:
            failures.append(link_name)
        report.append(
            {
                "link": link_name,
                "members": len(members),
                "mass_error_kg": mass_error,
                "com_error_m": com_error,
                "inertia_error": inertia_error,
                "inertia_tolerance": inertia_tolerance,
                "tensor_scale": scale,
                "passed": ok,
            }
        )
    model_total = sum(float(link["inertial"]["mass"]) for link in canonical.get("links", []) if link.get("inertial"))
    total_error = abs(raw_total - model_total)
    conflicts = raw.body_conflicts
    passed = not failures and not conflicts and total_error <= MASS_ATOL + MASS_RTOL * max(abs(model_total), 1.0)
    return _result(
        "source.mass_conservation",
        passed,
        expected=expected_links,
        checked=sorted(item["link"] for item in report),
        details={
            "links": report,
            "failures": sorted(set(failures)),
            "raw_total_kg": raw_total,
            "model_total_kg": model_total,
            "total_error_kg": total_error,
            "raw_body_conflicts": conflicts,
            "tolerance": {
                "mass_atol_kg": MASS_ATOL,
                "mass_rtol": MASS_RTOL,
                "com_atol_m": COM_ATOL,
                "inertia_atol": INERTIA_ATOL,
                "inertia_rtol": INERTIA_RTOL,
            },
            "method": "raw 质量读数 + occurrence 变换 → 世界系 Σm/Σm·c/Σ(RIRᵀ+m·平行轴)；"
            "模型侧按 q=0 FK 变到同一世界系",
        },
    )


def verify_normalization(raw_scene: dict, definition: dict, snapshot_root: Path, canonical: dict) -> list[dict]:
    """返回公共 ``result`` 形状的独立校验列表（供 ``assess`` 直接 extend）。"""

    robot = parse_robot_definition(definition)
    raw = _RawEvidence(raw_scene, Path(snapshot_root))
    return [
        _occurrences(raw_scene, raw),
        _entities(raw, canonical),
        _exclusions(canonical, raw, robot),
        _conservation(raw, robot, canonical),
    ]
