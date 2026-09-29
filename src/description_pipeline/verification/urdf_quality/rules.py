"""URDF 合同规则 `URDF###`：与来源工具链无关的自洽性与物理合理性检查。

规则只读模型与仓库台账（`config/joint_names.yaml`、`config/urdf_quality.json`），
不修复、不联网、不依赖第三方库。规则表见 ``docs/urdf_standard.md``。
"""

from __future__ import annotations

import hashlib
import math
import posixpath
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import cast

from . import geometry as geometry_module
from . import model as model_module
from . import stl
from .findings import ERROR, INFO, WARNING, Finding

AXIS_TOL = 1.0e-3  # 轴向单位化容差
LIMIT_SANITY = 2 * math.pi  # rad，超过即怀疑单位错误
MAX_EXTENT = 5.0  # m，单个网格最大合理边长
MIN_EXTENT = 1.0e-4  # m，小于此值视为退化
ORIGIN_SANITY = 10.0  # m
SCALE_TOL = 1.0e-6
COM_TOL = 2.0e-3  # m，质心容差
DENSITY_RANGE = (100.0, 20000.0)  # kg/m³，数量级检查
GYRATION_HARD = 1.0005  # 回转半径 / 几何上限，超过即物理不可能
GYRATION_SOFT = 0.85  # 接近几何上限，需要复核
GYRATION_MIN = 0.02  # 相对几何上限，过小说明惯量可疑
INERTIA_RATIO = (0.75, 1.333)  # 均匀密度网格惯量与 URDF 主惯量的允许比值
INERTIA_AXIS_DEG = 20.0  # 最大主惯量轴允许夹角
DEGENERATE_RATIO = 0.01
MASS_TOL = 1.0e-6  # kg，URDF ↔ MJCF
MASSLESS_TOL = 1.0e-6  # kg，低于此值视为无质量参考系
LIMIT_TOL = 1.0e-5  # rad
SCALE_TOL_MJCF = 1.0e-6
MIRROR_MASS = 0.05
MIRROR_INERTIA = 0.10
MIRROR_LIMIT = 1.0e-3
COMPILED_MASS_TOL = 1.0e-6  # kg
COMPILED_COM_TOL = 1.0e-3  # m
COMPILED_INERTIA_TOL = 0.05  # 最大主惯量相对差
FK_TOL = 1.0e-4  # m，URDF 正运动学 vs 编译模型
# 旋转矩阵差的 Frobenius 上限（≈0.04°）。URDF 存 rpy、MJCF 存 quat，各自只保留约 6 位
# 有效数字，光是存储舍入就有 ~0.001°；真实轴/原点错位在度级以上。
FK_ROTATION_TOL = 1.0e-3
FK_POSES = 5
CONTACT_POSES = 8


@dataclass
class Context:
    root: Path
    urdf: model_module.UrdfModel
    mjcf: model_module.MjcfModel | None = None
    joint_ledger: list[str] | None = None
    massless_links: set[str] = field(default_factory=set)
    template: bool = False
    mujoco: bool = False
    mesh_stats: dict[Path, stl.MeshStats] = field(default_factory=dict)
    mesh_errors: dict[Path, str] = field(default_factory=dict)
    mesh_digests: dict[Path, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    compiled_bodies: int = 0
    compiled_version: str = ""
    compiled_fk_error: float = 0.0
    contact_counts: list[int] = field(default_factory=list)
    uniform_density_links: set[str] | None = None
    require_collision: bool = True
    require_unit_scale: bool = True
    require_mirror_symmetry: bool = True

    def add(self, code: str, severity: str, message: str, subject: str = "", **context) -> None:
        self.findings.append(Finding(code, severity, message, subject, context))

    def stats(self, path: Path) -> stl.MeshStats | None:
        if path in self.mesh_stats:
            return self.mesh_stats[path]
        if path in self.mesh_errors:
            return None
        try:
            stats = stl.read(path)
        except stl.StlError as error:
            self.mesh_errors[path] = str(error)
            return None
        self.mesh_stats[path] = stats
        return stats

    def digest(self, path: Path) -> str:
        if path not in self.mesh_digests:
            self.mesh_digests[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        return self.mesh_digests[path]


#: 某些编号只在开关打开时才会被评估；例外台账是**工作区级**文件，为本次没有评估的规则
#: 写的例外不能被当成"死例外"（URDF702）。`description check` 按用途裁剪规则、独立审计
#: 按开关裁剪，两边都要把各自没跑的编号交给 `waivers.apply`。
GATED_CODES = {
    "mjcf": ("URDF502", "URDF503", "URDF504"),
    "mujoco": ("URDF507", "URDF510", "URDF511"),
    "template": ("URDF102", "URDF110"),
    "uniform_density": ("URDF310", "URDF311"),
    "collision": ("URDF407",),
    "unit_scale": ("URDF405",),
    "mirror_symmetry": ("URDF601", "URDF602", "URDF603"),
}


def not_evaluated(context: Context) -> set[str]:
    """这份上下文**不可能产生**的规则编号。

    例如按运动学用途跑的 `description check` 不评估 URDF407，所以一份为"没有碰撞几何"
    写下的、被独立审计接受的例外，不该反过来把这次资格判定判死。
    """

    codes: set[str] = set()
    if context.mjcf is None:
        codes.update(GATED_CODES["mjcf"])
    if not context.mujoco:
        codes.update(GATED_CODES["mujoco"])
    if context.template:
        codes.update(GATED_CODES["template"])
    if context.uniform_density_links is not None:
        codes.update(GATED_CODES["uniform_density"])
    if not context.require_collision:
        codes.update(GATED_CODES["collision"])
    if not context.require_unit_scale:
        codes.update(GATED_CODES["unit_scale"])
    if not context.require_mirror_symmetry:
        codes.update(GATED_CODES["mirror_symmetry"])
    return codes


def run(context: Context) -> list[Finding]:
    _structure(context)
    _joints(context)
    _inertia(context)
    _geometry(context)
    _mjcf(context)
    _compiled(context)
    if context.require_mirror_symmetry:
        _mirrors(context)
    # 同一条结论可能被多条路径触发（例如同一网格同时用于 visual 与 collision）
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[Finding] = []
    for finding in context.findings:
        key = (finding.code, finding.severity, finding.subject, finding.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


# --- 结构 -------------------------------------------------------------


def _structure(context: Context) -> None:
    urdf = context.urdf
    if not urdf.name:
        context.add("URDF101", ERROR, "URDF 缺少机器人名字（<robot name=…>）")
    if not urdf.links:
        if not context.template:
            context.add("URDF102", ERROR, "URDF 没有任何 link")
        return
    for name in sorted(set(urdf.duplicate_links)):
        context.add("URDF103", ERROR, f"link 名字重复：{name}", name)
    for name in sorted(set(urdf.duplicate_joints)):
        context.add("URDF104", ERROR, f"joint 名字重复：{name}", name)

    known = set(urdf.links)
    for joint in urdf.joints.values():
        for role, link in (("parent", joint.parent), ("child", joint.child)):
            if link not in known:
                context.add(
                    "URDF105",
                    ERROR,
                    f"joint {joint.name} 的 {role} link 不存在：{link or '(空)'}",
                    joint.name,
                )
    children = {joint.child for joint in urdf.joints.values()}
    roots = sorted(known - children)
    if len(roots) != 1:
        context.add(
            "URDF106",
            ERROR,
            f"关节图不是单根树：根 link {len(roots)} 个 {roots[:5]}",
            "",
            roots=roots,
        )
    # 环：从每个根做一次遍历，统计可覆盖的 link
    reachable: set[str] = set()
    frontier = list(roots)
    adjacency: dict[str, list[str]] = {}
    for joint in urdf.joints.values():
        adjacency.setdefault(joint.parent, []).append(joint.child)
    while frontier:
        link = frontier.pop()
        if link in reachable:
            continue
        reachable.add(link)
        frontier.extend(adjacency.get(link, []))
    unreachable = sorted(known - reachable)
    if unreachable:
        context.add(
            "URDF107",
            ERROR,
            f"{len(unreachable)} 个 link 无法从根到达（环或悬空子图）：{unreachable[:5]}",
            "",
            links=unreachable,
        )
    parents: dict[str, int] = {}
    for joint in urdf.joints.values():
        parents[joint.child] = parents.get(joint.child, 0) + 1
    for name in sorted(name for name, count in parents.items() if count > 1):
        context.add(
            "URDF107",
            ERROR,
            f"link 有 {parents[name]} 个父关节（图里有环或重复连接）",
            name,
        )
    moveable = [joint for joint in urdf.joints.values() if joint.moveable]
    if not moveable and not context.template:
        context.add("URDF110", ERROR, "没有任何可动关节（revolute/continuous/prismatic）")


# --- 关节 -------------------------------------------------------------


def _joints(context: Context) -> None:
    for name in sorted(context.urdf.joints):
        joint = context.urdf.joints[name]
        if joint.type not in model_module.ROBOT_TYPES:
            context.add("URDF108", ERROR, f"关节类型非法：{joint.type or '(空)'}", name)
            continue
        if joint.type in ("floating", "planar"):
            context.add(
                "URDF109",
                ERROR,
                f"本仓库不接受 {joint.type} 关节（无法从 CAD 复核，也不是单轴驱动）",
                name,
            )
            continue
        for value in (*joint.origin, *joint.rpy, *(joint.axis or ())):
            if not math.isfinite(value):
                context.add("URDF207", ERROR, "关节 origin/axis 含非有限值", name)
                break
        if max(abs(value) for value in joint.origin) > ORIGIN_SANITY:
            context.add(
                "URDF207",
                ERROR,
                f"关节 origin 偏移 {max(abs(v) for v in joint.origin):.3f} m 超过 {ORIGIN_SANITY} m",
                name,
            )
        if joint.type == "fixed":
            continue
        if joint.axis is None:
            context.add("URDF205", ERROR, "可动关节缺少 <axis>", name)
        else:
            length = math.dist((0.0, 0.0, 0.0), joint.axis)
            if length == 0.0:
                context.add("URDF205", ERROR, "关节 axis 是零向量", name)
            elif abs(length - 1.0) > AXIS_TOL:
                context.add(
                    "URDF206",
                    ERROR,
                    f"关节 axis 未单位化（长度 {length:.6f}），动力学与限位语义会偏",
                    name,
                    length=length,
                )
        limits = joint.limits or {}
        bounded = joint.type in ("revolute", "prismatic")
        required = ["effort", "velocity", *(["lower", "upper"] if bounded else [])]
        missing = [key for key in required if key not in limits]
        if missing:
            context.add("URDF201", ERROR, f"关节限位缺少字段：{', '.join(missing)}", name)
        if any(not math.isfinite(value) for value in limits.values()):
            context.add("URDF202", ERROR, "关节限位含非有限值", name)
        if bounded:
            lower, upper = limits.get("lower"), limits.get("upper")
            if lower is not None and upper is not None and not lower < upper:
                context.add("URDF202", ERROR, f"限位上下界颠倒或相等：[{lower}, {upper}]", name)
        for key in ("effort", "velocity"):
            value = limits.get(key)
            if value is not None and not value > 0:
                context.add("URDF202", ERROR, f"limit {key} 必须为正：{value}", name)
        if joint.type == "revolute":
            for key in ("lower", "upper"):
                value = limits.get(key)
                if value is not None and abs(value) > LIMIT_SANITY:
                    context.add(
                        "URDF203",
                        ERROR,
                        f"limit {key}={value:.4f} 超出 ±2π，疑似单位错误（deg 当成 rad）",
                        name,
                    )
        elif joint.type == "continuous" and {"lower", "upper"} & limits.keys():
            context.add("URDF204", WARNING, "continuous 的 lower/upper 不生效；仅保留 effort/velocity", name)
    _joint_ledger(context)


def _joint_ledger(context: Context) -> None:
    moveable = {joint.name for joint in context.urdf.joints.values() if joint.moveable}
    ledger = context.joint_ledger
    if ledger is None:
        if moveable:
            context.add(
                "URDF208",
                ERROR,
                "缺少或无法解析 config/joint_names.yaml：可动关节必须登记在台账里"
                "（`description model init` 会生成空模板）",
                "",
            )
        return
    duplicates = sorted({name for name in ledger if ledger.count(name) > 1})
    for name in duplicates:
        context.add("URDF208", ERROR, f"台账重复登记关节：{name}", name)
    missing = sorted(moveable - set(ledger))
    extra = sorted(set(ledger) - moveable)
    for name in missing:
        context.add("URDF208", ERROR, "可动关节没有登记在 config/joint_names.yaml", name)
    for name in extra:
        context.add("URDF208", ERROR, "台账登记的关节在 URDF 里不存在或不是可动关节", name)


# --- 惯性 -------------------------------------------------------------


def _link_box(context: Context, link: model_module.Link) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    """把各网格包围盒按 origin/scale 变换到 link 坐标系，返回合并后的包围盒。"""

    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    for mesh in link.meshes:
        if mesh.path is None:
            continue
        stats = context.stats(mesh.path)
        if stats is None:
            continue
        matrix = model_module.rpy_matrix(mesh.rpy)
        for corner in _box_corners(stats.low, stats.high):
            point = [corner[axis] * mesh.scale[axis] for axis in range(3)]
            world = [sum(matrix[row][axis] * point[axis] for axis in range(3)) + mesh.origin[row] for row in range(3)]
            for axis in range(3):
                low[axis] = min(low[axis], world[axis])
                high[axis] = max(high[axis], world[axis])
    if any(math.isinf(value) for value in low):
        return None
    return cast(tuple[float, float, float], tuple(low)), cast(tuple[float, float, float], tuple(high))


def _box_corners(low, high):
    for x in (low[0], high[0]):
        for y in (low[1], high[1]):
            for z in (low[2], high[2]):
                yield (x, y, z)


def _inertia(context: Context) -> None:
    for name in sorted(context.urdf.links):
        link = context.urdf.links[name]
        has_mesh = bool(link.meshes)
        massless = name in context.massless_links
        if link.inertial is None:
            if has_mesh and not massless:
                context.add(
                    "URDF301",
                    ERROR,
                    "有网格的 link 缺少 <inertial>；无质量参考系请写进 config/urdf_quality.json",
                    name,
                )
            continue
        inertial = link.inertial
        if not inertial.finite():
            context.add("URDF303", ERROR, "质量/质心/惯量含非有限值", name)
            continue
        if inertial.mass <= MASSLESS_TOL:
            # 无质量参考系（引擎用 1e-9 kg 占位）：有几何说明是漏了质量，无几何才合法
            if link.meshes and massless:
                context.add("URDF301", INFO, "已登记的无质量 link 带网格，几何不参与动力学", name)
            elif link.meshes:
                context.add(
                    "URDF302",
                    ERROR,
                    f"有几何的 link 质量必须为正（当前 {inertial.mass:g} kg）",
                    name,
                )
            else:
                context.add("URDF309", INFO, "无质量参考系（质量 ≤ 1e-6 kg）", name)
            continue
        if not massless and not inertial.mass > 0:
            context.add("URDF302", ERROR, f"质量必须为正：{inertial.mass}", name)
        if context.require_collision and link.visuals and link.collisions == 0:
            context.add("URDF407", WARNING, "有 visual 但没有 collision", name)
        if not has_mesh and not (link.visuals or link.collisions):
            context.add(
                "URDF309",
                INFO if massless else WARNING,
                "有惯性但没有几何（box/sphere/cylinder 也可作为几何；无质量参考系请登记）",
                name,
            )
        eigenvalues = _eigenvalues(inertial.matrix())
        if eigenvalues is None:
            context.add("URDF304", ERROR, "惯量张量无法求解特征值", name)
        else:
            if min(eigenvalues) <= 0:
                context.add(
                    "URDF304",
                    ERROR,
                    f"惯量非正定：主惯量 {[round(v, 9) for v in eigenvalues]}",
                    name,
                )
            elif not (
                eigenvalues[0] + eigenvalues[1] >= eigenvalues[2] * (1 - 1e-9)
                and eigenvalues[0] + eigenvalues[2] >= eigenvalues[1] * (1 - 1e-9)
                and eigenvalues[1] + eigenvalues[2] >= eigenvalues[0] * (1 - 1e-9)
            ):
                context.add(
                    "URDF304",
                    ERROR,
                    f"违反主惯量三角不等式：{[round(v, 9) for v in eigenvalues]}",
                    name,
                )
        box = _link_box(context, link)
        if box is None:
            continue
        low, high = box
        com = inertial.origin
        if any(com[axis] < low[axis] - COM_TOL or com[axis] > high[axis] + COM_TOL for axis in range(3)):
            context.add(
                "URDF307",
                WARNING,
                "质心不在 link 几何的包围盒内（坐标系或选区可能错了）",
                name,
                com=[round(value, 5) for value in com],
                box=[[round(value, 5) for value in low], [round(value, 5) for value in high]],
            )
        volume = 0.0
        for mesh in link.meshes:
            stats = context.stats(mesh.path) if mesh.path is not None else None
            if stats is not None:
                volume += stats.volume * mesh.scale[0] * mesh.scale[1] * mesh.scale[2]
        if volume > 0 and inertial.mass > 0:
            density = inertial.mass / volume
            if not DENSITY_RANGE[0] <= density <= DENSITY_RANGE[1]:
                context.add(
                    "URDF308",
                    WARNING,
                    f"等效密度 {density:.0f} kg/m³ 超出 {DENSITY_RANGE[0]:.0f}–{DENSITY_RANGE[1]:.0f} 数量级"
                    "（网格可能是外壳/多实体，先复核质量与网格）",
                    name,
                    density_kg_m3=round(density, 2),
                )
        if eigenvalues and inertial.mass > 0:
            feature = max(math.dist(com, corner) for corner in _box_corners(low, high))
            gyration = math.sqrt(eigenvalues[2] / inertial.mass)
            if feature > 0:
                ratio = gyration / feature
                if ratio > GYRATION_HARD:
                    context.add(
                        "URDF305",
                        ERROR,
                        f"回转半径 {gyration:.5f} m 超过几何上限 {feature:.5f} m（惯量或单位错误）",
                        name,
                        ratio=round(ratio, 4),
                    )
                elif ratio > GYRATION_SOFT:
                    context.add(
                        "URDF306",
                        WARNING,
                        f"回转半径接近几何上限（{ratio:.2f}×），复核 CAD 选区和质量分布",
                        name,
                        ratio=round(ratio, 4),
                    )
                elif ratio < GYRATION_MIN:
                    context.add(
                        "URDF306",
                        WARNING,
                        f"回转半径过小（{ratio:.4f}×几何上限），复核惯量是否漏了零件",
                        name,
                        ratio=round(ratio, 4),
                    )
        _mesh_inertia_oracle(context, name, link, inertial)


def _mesh_inertia_oracle(context: Context, name: str, link, inertial) -> None:
    """与"均匀密度网格惯量"交叉核对：量级与主轴都应该接近。

    网格本身只是几何（可能含伺服壳体、外壳），因此这是 warning 级复核提示；
    但它能抓到数量级错误、张量填错、质量归属错与坐标系错。
    """

    if inertial.mass <= MASSLESS_TOL:
        return
    if context.uniform_density_links is not None and name not in context.uniform_density_links:
        return
    mesh = geometry_module.link_mesh_inertia(link, context.stats)
    if mesh is None:
        return
    comparison = geometry_module.compare_inertia(inertial.matrix(), inertial.mass, mesh)
    ratio = comparison["ratio"]
    if not math.isfinite(ratio) or not INERTIA_RATIO[0] <= ratio <= INERTIA_RATIO[1]:
        context.add(
            "URDF310",
            WARNING,
            f"主惯量与均匀密度网格惯量差 {abs(1 - ratio) * 100:.0f}%"
            f"（回转半径 URDF {comparison['urdf_gyration']:.5f} m vs "
            f"网格 {comparison['mesh_gyration']:.5f} m）",
            name,
            ratio=round(ratio, 4),
        )
    if comparison["axis_angle_deg"] > INERTIA_AXIS_DEG:
        context.add(
            "URDF311",
            WARNING,
            f"最大主惯量轴与网格主轴相差 {comparison['axis_angle_deg']:.1f}°（质量分布或坐标系可疑）",
            name,
            angle_deg=round(comparison["axis_angle_deg"], 2),
        )


_eigenvalues = geometry_module.eigenvalues_symmetric


# --- 几何 -------------------------------------------------------------


def _geometry(context: Context) -> None:
    digests: dict[str, list[str]] = {}
    seen_paths: set[Path] = set()
    for link_name in sorted(context.urdf.links):
        link = context.urdf.links[link_name]
        for mesh in link.meshes:
            uri = mesh.uri
            if not uri:
                context.add("URDF401", ERROR, "mesh 缺少 filename", link_name)
                continue
            if uri.startswith(("package://", "file://", "http://", "https://")) or Path(uri).is_absolute():
                context.add(
                    "URDF401",
                    ERROR,
                    f"mesh 引用必须是相对路径，禁止 {uri.split(':', 1)[0]} 形式",
                    link_name,
                    uri=uri,
                )
                continue
            if mesh.path is None:
                context.add("URDF402", ERROR, f"网格文件不存在：{uri}", link_name, uri=uri)
                continue
            if context.require_unit_scale and any(abs(value - 1.0) > SCALE_TOL for value in mesh.scale):
                context.add(
                    "URDF405",
                    ERROR,
                    f"mesh scale 必须为 1（当前 {mesh.scale}）；单位换算应在导出阶段完成",
                    link_name,
                    uri=uri,
                )
            if mesh.path not in seen_paths:
                seen_paths.add(mesh.path)
                stats = context.stats(mesh.path)
                if stats is None:
                    context.add(
                        "URDF403",
                        ERROR,
                        f"网格无法解析：{context.mesh_errors.get(mesh.path, '未知错误')}",
                        link_name,
                        uri=uri,
                    )
                else:
                    extent = max(stats.extent)
                    if not MIN_EXTENT <= extent <= MAX_EXTENT:
                        context.add(
                            "URDF404",
                            ERROR,
                            f"网格最大边长 {extent:.5f} m 超出合理范围（{MIN_EXTENT}–{MAX_EXTENT} m）",
                            link_name,
                            uri=uri,
                        )
                    if stats.triangles and stats.degenerate / stats.triangles > DEGENERATE_RATIO:
                        context.add(
                            "URDF406",
                            WARNING,
                            f"{stats.degenerate}/{stats.triangles} 个退化三角面"
                            f"（{100 * stats.degenerate / stats.triangles:.1f}%）",
                            link_name,
                            uri=uri,
                        )
                    if stats.volume <= 0.0:
                        context.add(
                            "URDF409",
                            WARNING,
                            "网格不是封闭实体（有向体积≈0），体积类检查不可用",
                            link_name,
                            uri=uri,
                        )
                    elif stats.signed_volume < 0:
                        context.add(
                            "URDF411",
                            WARNING,
                            "网格法线整体朝内（有向体积为负），渲染与碰撞法线会反",
                            link_name,
                            uri=uri,
                        )
                    digests.setdefault(context.digest(mesh.path), []).append(mesh.path.name)
    boundary = sum(stats.boundary_edges for path in seen_paths for stats in (context.stats(path),) if stats is not None)
    if boundary:
        context.add(
            "URDF410",
            INFO,
            f"{len(seen_paths)} 个网格共有 {boundary} 条非闭合边（CAD 导出的 T 型接缝；不影响质量/惯量检查，仅记录）",
            "",
            boundary_edges=boundary,
        )
    for digest, names in sorted(digests.items()):
        if len(names) > 1:
            context.add(
                "URDF408",
                INFO,
                f"{len(names)} 个网格文件内容相同：{', '.join(sorted(names)[:4])}",
                "",
                sha256=digest[:16],
            )


# --- MJCF 一致性 -------------------------------------------------------


def _mjcf(context: Context) -> None:
    mjcf = context.mjcf
    if mjcf is None:
        return
    # Validate both POSIX and Windows absolute-path syntax on every host.
    # Otherwise ``/tmp`` is accepted on Windows while a Linux audit rejects it
    # (and the inverse happens for drive and UNC paths).
    meshdir = mjcf.meshdir.replace("\\", "/")
    if mjcf.meshdir and (
        posixpath.isabs(meshdir)
        or bool(PureWindowsPath(mjcf.meshdir).drive)
        or meshdir.startswith("//")
        or "://" in mjcf.meshdir
    ):
        context.add("URDF502", ERROR, f"MJCF meshdir 必须是相对路径：{mjcf.meshdir}")
    urdf_meshes = {mesh.path.resolve() for mesh in context.urdf.mesh_references() if mesh.path}
    unknown = sorted(
        name for name in mjcf.mesh_files if (mjcf.path.parent / mjcf.meshdir / name).resolve() not in urdf_meshes
    )
    for name in unknown:
        context.add("URDF503", ERROR, "MJCF 引用了 URDF 没有的网格", name)
    body_names = set(mjcf.bodies)
    link_names = set(context.urdf.links)
    for name in sorted(body_names - link_names):
        context.add("URDF504", ERROR, "MJCF body 在 URDF 里没有同名 link", name)
    for name in sorted(link_names & body_names):
        link = context.urdf.links[name]
        body = mjcf.bodies[name]
        if link.inertial is None or body.mass is None:
            continue
        if abs(link.inertial.mass - body.mass) > MASS_TOL:
            context.add(
                "URDF505",
                ERROR,
                f"URDF/MJCF 质量不一致：{link.inertial.mass:.6f} vs {body.mass:.6f} kg",
                name,
            )
    # 关节限位：MJCF 关节挂在 body 上，按关节名匹配 URDF
    urdf_joints = context.urdf.joints
    for body in mjcf.bodies.values():
        for joint in body.joints:
            name = joint.get("name", "")
            reference = urdf_joints.get(name)
            if reference is None or joint.get("type") == "free":
                continue
            parsed = _parse_range(joint.get("range"))
            if parsed is None:
                continue
            limits = reference.limits or {}
            if reference.type == "continuous":
                continue
            lower, upper = parsed
            if (
                abs(lower - limits.get("lower", lower)) > LIMIT_TOL
                or abs(upper - limits.get("upper", upper)) > LIMIT_TOL
            ):
                context.add(
                    "URDF506",
                    ERROR,
                    f"URDF/MJCF 限位不一致：URDF [{limits.get('lower')}, {limits.get('upper')}]"
                    f" vs MJCF [{lower}, {upper}]",
                    name,
                )


def _parse_range(text: str | None) -> tuple[float, float] | None:
    if not text:
        return None
    fields = text.replace(",", " ").split()
    if len(fields) != 2:
        return None
    try:
        return float(fields[0]), float(fields[1])
    except ValueError:
        return None


def _compiled(context: Context) -> None:
    """`--mujoco`：把 MJCF 编译后 MuJoCo 眼里的质量属性与 URDF 对照。

    XML 层比对（URDF505/506）看字面值；这里看**编译结果**——密度推断、default
    继承、fullinertia/diaginertia 语义差异都会在这一层暴露。
    """

    if not context.mujoco:
        return
    if context.mjcf is None:
        context.add("URDF510", ERROR, "要求编译验算但缺少 mjcf/robot.xml")
        return
    try:
        import mujoco
    except ModuleNotFoundError:
        context.add(
            "URDF510",
            ERROR,
            "要求编译验算但未安装 mujoco；不能形成通过结论",
        )
        return
    try:
        model = mujoco.MjModel.from_xml_path(str(context.mjcf.path))
    except Exception as error:  # noqa: BLE001 - mujoco 的异常类型不稳定
        context.add("URDF510", ERROR, f"MJCF 无法编译：{error}")
        return
    import numpy as np

    compiled_values = _compiled_values(model, mujoco)
    required = {
        name
        for name, link in context.urdf.links.items()
        if link.inertial is not None and link.inertial.mass > MASSLESS_TOL
    }
    for name in sorted(required - compiled_values.keys()):
        context.add("URDF507", ERROR, "URDF 物理 link 在编译结果中缺失", name)
    for name, compiled in sorted(compiled_values.items()):
        link = context.urdf.links.get(name)
        if link is None or link.inertial is None or link.inertial.mass <= MASSLESS_TOL:
            continue
        context.compiled_bodies += 1
        inertial = link.inertial
        if abs(compiled["mass"] - inertial.mass) > COMPILED_MASS_TOL:
            context.add(
                "URDF507",
                ERROR,
                f"编译后质量与 URDF 不一致：{compiled['mass']:.6f} vs {inertial.mass:.6f} kg",
                name,
            )
        offset = math.dist(compiled["com"], inertial.origin)
        if offset > COMPILED_COM_TOL:
            context.add(
                "URDF508",
                ERROR,
                f"编译后质心与 URDF 相差 {offset * 1000:.2f} mm",
                name,
                compiled_com=[round(value, 6) for value in compiled["com"]],
            )
        rotation = np.asarray(model_module.rpy_matrix(inertial.rpy))
        expected = rotation @ np.asarray(inertial.matrix()) @ rotation.T
        actual = np.asarray(compiled["tensor"])
        if not np.allclose(actual, expected, rtol=1e-6, atol=1e-10):
            context.add(
                "URDF509",
                ERROR,
                "编译后完整惯量张量（含方向）与 URDF 不一致",
                name,
                tensor_error=float(np.linalg.norm(actual - expected)),
            )
    _compiled_kinematics(context, model, mujoco)
    context.compiled_version = str(getattr(mujoco, "__version__", ""))


def _compiled_values(model, mujoco) -> dict[str, dict]:
    import numpy as np

    values = {}
    for index in range(1, model.nbody):
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, index)
        if not name:
            continue
        flat = np.zeros(9)
        mujoco.mju_quat2Mat(flat, model.body_iquat[index])
        rotation = flat.reshape(3, 3)
        values[name] = {
            "tensor": (rotation @ np.diag(model.body_inertia[index]) @ rotation.T).tolist(),
            "mass": float(model.body_mass[index]),
            "com": tuple(float(value) for value in model.body_ipos[index]),
            "inertia": sorted(float(value) for value in model.body_inertia[index]),
        }
    return values


def _compiled_kinematics(context: Context, model, mujoco) -> None:
    """URDF 正运动学 vs 编译后的 body 世界位置；同时统计多姿态自接触。

    位置比对能抓住轴/原点/符号在 URDF 与 MJCF 之间的任何不一致（阈值 0.1 mm；
    实测需独立记录）。自接触只作为证据记录：碰撞策略要机械/仿真签核，
    不在本工具的门禁范围内。
    """

    import numpy as np
    from description_pipeline.verification import _poses

    data = mujoco.MjData(model)
    hinge = {}
    # pybind11 枚举与 numpy 标量的集合判断不对称（`enum == np.int32` 为假），
    # 必须先取 int：否则 hinge/slide 关节会被全部跳过，URDF511 误报“缺少可动关节”。
    hinge_types = (int(mujoco.mjtJoint.mjJNT_HINGE), int(mujoco.mjtJoint.mjJNT_SLIDE))
    for index in range(model.njnt):
        if int(model.jnt_type[index]) not in hinge_types:
            continue
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, index)
        if name:
            hinge[name] = (model.jnt_qposadr[index], _limit_range(context, name))
    body_names = {index: mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, index) for index in range(1, model.nbody)}
    missing_joints = {joint.name for joint in context.urdf.joints.values() if joint.moveable} - hinge.keys()
    if missing_joints:
        context.add("URDF511", ERROR, "编译模型缺少可动关节", "", missing=sorted(missing_joints))
    worst = 0.0
    worst_rotation = 0.0
    contacts: list[int] = []
    for pose in range(max(FK_POSES, CONTACT_POSES)):
        mujoco.mj_resetData(model, data)
        values = {}
        for order, (name, (address, limits)) in enumerate(sorted(hinge.items())):
            if pose == 0:
                value = 0.0
            else:
                value = 0.5 * math.sin(1.7 * order + 0.6 * pose)
                value = min(max(value, limits[0]), limits[1])
            data.qpos[address] = value
            values[name] = value
        mujoco.mj_forward(model, data)
        if pose < FK_POSES:
            transforms = _poses(context.urdf, values)
            for index, name in body_names.items():
                if not name or name not in transforms:
                    continue
                transform = transforms[name]
                error = math.dist(transform[:3, 3], data.xpos[index])
                worst = max(worst, error)
                worst_rotation = max(
                    worst_rotation, float(np.linalg.norm(transform[:3, :3] - data.xmat[index].reshape(3, 3)))
                )
        contacts.append(int(data.ncon))
    context.compiled_fk_error = worst
    if worst > FK_TOL or worst_rotation > FK_ROTATION_TOL:
        if worst > FK_TOL:
            message = (
                f"URDF 正运动学与编译模型最多相差 {worst * 1000:.3f} mm（轴、原点或父子关系在 URDF 与 MJCF 之间不一致）"
            )
        else:
            message = (
                f"URDF 与编译模型姿态最多相差 {_rotation_degrees(worst_rotation):.4f}°"
                "（轴或原点方向在 URDF 与 MJCF 之间不一致）"
            )
        context.add(
            "URDF511",
            ERROR,
            message,
            "",
            error_m=round(worst, 8),
            rotation_matrix_error=worst_rotation,
        )
    context.contact_counts = contacts
    touching = [count for count in contacts if count > 0]
    context.add(
        "URDF512",
        INFO,
        f"{len(contacts)} 个采样姿态里 {len(touching)} 个存在自接触"
        f"（最多 {max(contacts) if contacts else 0} 个接触点；碰撞策略仍需机械/仿真签核）",
        "",
        contacts=contacts,
    )


def _rotation_degrees(norm: float) -> float:
    """把两个旋转矩阵之差的 Frobenius 范数换算成夹角（度）：‖ΔR‖ = 2√2·sin(θ/2)。"""

    return math.degrees(2 * math.asin(min(1.0, norm / (2 * math.sqrt(2)))))


def _limit_range(context: Context, name: str):
    joint = context.urdf.joints.get(name)
    if joint is None or joint.limits is None:
        return (-0.5, 0.5)
    lower = joint.limits.get("lower", -0.5)
    upper = joint.limits.get("upper", 0.5)
    return (lower, upper)


# --- 左右镜像 ----------------------------------------------------------


def _mirrors(context: Context) -> None:
    links = context.urdf.links
    for name in sorted(links):
        if not name.startswith("left_"):
            continue
        opposite = "right_" + name[len("left_") :]
        if opposite not in links:
            continue
        left, right = links[name], links[opposite]
        if left.inertial and right.inertial:
            if left.inertial.mass > 0 and right.inertial.mass > 0:
                difference = abs(left.inertial.mass - right.inertial.mass) / max(
                    left.inertial.mass, right.inertial.mass
                )
                if difference > MIRROR_MASS:
                    context.add(
                        "URDF601",
                        WARNING,
                        f"左右质量差 {100 * difference:.1f}%：{left.inertial.mass:.5f} vs {right.inertial.mass:.5f} kg",
                        name,
                    )
            left_values = left.inertial.matrix()
            right_values = right.inertial.matrix()
            norm = math.sqrt(sum(value * value for row in left_values for value in row))
            if norm > 0:
                delta = (
                    math.sqrt(
                        sum(
                            (left_values[row][column] - right_values[row][column]) ** 2
                            for row in range(3)
                            for column in range(3)
                        )
                    )
                    / norm
                )
                if delta > MIRROR_INERTIA:
                    context.add(
                        "URDF602",
                        WARNING,
                        f"左右惯量 Frobenius 相对差 {100 * delta:.1f}%",
                        name,
                    )
    for name in sorted(context.urdf.joints):
        if not name.startswith("left_"):
            continue
        opposite = "right_" + name[len("left_") :]
        pair = context.urdf.joints.get(opposite)
        if pair is None:
            continue
        left_joint = context.urdf.joints[name]
        if left_joint.type != pair.type:
            context.add(
                "URDF603",
                WARNING,
                f"左右关节类型不同：{left_joint.type} vs {pair.type}",
                name,
            )
            continue
        if left_joint.limits is None or pair.limits is None:
            continue
        # 左右轴向符号约定可以相反，因此只看区间形状：宽度必须一致，中心要么相反
        # （典型镜像），要么相同（镜像由轴向/父坐标系约定表达）。逐字比较 lower/upper
        # 会把合法的符号约定误判成缺陷。
        left_lower, left_upper = (
            left_joint.limits.get("lower", 0.0),
            left_joint.limits.get("upper", 0.0),
        )
        right_lower, right_upper = pair.limits.get("lower", 0.0), pair.limits.get("upper", 0.0)
        width_gap = abs((left_upper - left_lower) - (right_upper - right_lower))
        left_center = (left_upper + left_lower) / 2
        right_center = (right_upper + right_lower) / 2
        center_gap = min(abs(left_center + right_center), abs(left_center - right_center))
        if width_gap > MIRROR_LIMIT or center_gap > MIRROR_LIMIT:
            context.add(
                "URDF603",
                WARNING,
                f"左右限位形状不同：宽度差 {width_gap:.5f}、中心差 {center_gap:.5f}"
                f"（left [{left_lower}, {left_upper}] vs right [{right_lower}, {right_upper}]）",
                name,
            )
