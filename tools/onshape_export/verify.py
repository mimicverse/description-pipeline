"""导出后独立核验：只读已提交的 URDF/MJCF 与 meshes/，不依赖引擎内部状态。

规则编号 ``OSV###``。默认零第三方依赖；装了 ``mujoco`` 时可加 --mujoco 做真加载比对。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from . import contacts, linalg, stl
from .assembly import Assembly

ERROR = "error"
WARNING = "warning"
INFO = "info"

POSITION_TOL = 1.0e-4  # m，相对 Onshape mate 帧
AXIS_TOL = 0.5  # deg
MASS_TOL = 1.0e-6  # kg，URDF ↔ MJCF
MASSLESS_TOL = 1.0e-6  # kg，低于此值视为"无质量参考系 link"（引擎用 1e-9 占位）
RANGE_TOL = 1.0e-5  # rad
MAX_EXTENT = 5.0  # m，单件网格的最大合理边长


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message, "context": self.context}


class VerifyError(RuntimeError):
    pass


def verify(
    root: Path,
    *,
    assembly: Assembly | None = None,
    reference: Path | None = None,
    reference_mjcf: Path | None = None,
    use_mujoco: bool = False,
) -> dict:
    root = Path(root)
    findings: list[Finding] = []
    urdf_path = root / "urdf" / "robot.urdf"
    if not urdf_path.is_file():
        raise VerifyError(f"缺少 {urdf_path}")

    links, joints = parse_urdf(urdf_path)
    findings += _check_tree(links, joints)
    findings += _check_meshes(root, links)
    findings += _check_masses(links)
    findings += _check_inertia(links)
    findings += _check_limits(joints)
    findings += _check_mjcf(root, root / "mjcf" / "robot.xml", links, joints)
    if assembly is not None:
        findings += _check_against_onshape(assembly, links, joints)
    reference_details = None
    if reference is not None:
        reference_findings, reference_details = compare_with_reference(links, joints, Path(reference))
        findings += reference_findings
        _, ref_joints = parse_urdf(Path(reference))
        findings += _check_effort_velocity(joints, ref_joints)
    findings += _check_contact_excludes(root / "mjcf" / "robot.xml")
    if use_mujoco:
        findings += _check_mujoco_runtime(root, links)
        findings += _check_self_collision(root)
    if reference_mjcf is not None:
        findings += _compare_mjcf_dynamics(root, Path(reference_mjcf))

    summary = summarize(findings)
    report = {
        "schema": "mimicverse.onshape_export/verify/v1",
        "structure": {
            "links": len(links),
            "joints": len(joints),
            "revolute": sum(1 for joint in joints.values() if joint["type"] == "revolute"),
            "fixed": sum(1 for joint in joints.values() if joint["type"] == "fixed"),
            "total_mass_kg": round(sum(link["mass"] for link in links.values()), 6),
        },
        "summary": summary,
        "findings": [finding.as_dict() for finding in findings],
    }
    if reference_details is not None:
        report["reference_comparison"] = reference_details
    return report


# --- URDF 解析 ---------------------------------------------------------


def parse_urdf(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise VerifyError(f"URDF 无法解析: {error}") from error

    links: dict[str, dict] = {}
    for element in root.findall("link"):
        inertial = element.find("inertial")
        mass = 0.0
        origin = (0.0, 0.0, 0.0)
        inertia = None
        mass_element = inertial.find("mass") if inertial is not None else None
        origin_element = inertial.find("origin") if inertial is not None else None
        inertia_element = inertial.find("inertia") if inertial is not None else None
        if mass_element is not None:
            mass = float(mass_element.get("value", "0"))
            if origin_element is not None:
                origin = _xyz(origin_element.get("xyz", "0 0 0"))
        if inertia_element is not None:
            node = inertia_element
            inertia = {key: float(node.get(key, "0")) for key in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")}
        meshes = [
            mesh.get("filename", "") for mesh in element.findall("visual//mesh") + element.findall("collision//mesh")
        ]
        links[element.get("name", "")] = {
            "mass": mass,
            "com": origin,
            "meshes": meshes,
            "has_inertial": inertial is not None,
            "inertia": inertia,
        }

    joints: dict[str, dict] = {}
    for element in root.findall("joint"):
        origin_element = element.find("origin")
        axis_element = element.find("axis")
        limit_element = element.find("limit")
        parent_element = element.find("parent")
        child_element = element.find("child")
        joints[element.get("name", "")] = {
            "type": element.get("type", ""),
            "parent": parent_element.get("link") if parent_element is not None else "",
            "child": child_element.get("link") if child_element is not None else "",
            "origin": _xyz(origin_element.get("xyz", "0 0 0")) if origin_element is not None else (0.0, 0.0, 0.0),
            "rpy": _xyz(origin_element.get("rpy", "0 0 0")) if origin_element is not None else (0.0, 0.0, 0.0),
            "axis": _xyz(axis_element.get("xyz", "0 0 1")) if axis_element is not None else (0.0, 0.0, 1.0),
            "lower": _number(limit_element.get("lower")) if limit_element is not None else None,
            "upper": _number(limit_element.get("upper")) if limit_element is not None else None,
            "effort": _number(limit_element.get("effort")) if limit_element is not None else None,
            "velocity": _number(limit_element.get("velocity")) if limit_element is not None else None,
        }
    return links, joints


def forward_kinematics(links: dict[str, dict], joints: dict[str, dict]) -> dict[str, linalg.Matrix]:
    """零位下的 link 世界位姿；根节点为未被任何关节当作 child 的 link。"""

    children = {joint["child"] for joint in joints.values()}
    roots = [name for name in links if name not in children]
    if len(roots) != 1:
        raise VerifyError(f"URDF 根节点不唯一: {roots}")
    world: dict[str, linalg.Matrix] = {roots[0]: linalg.identity()}
    pending = list(joints.values())
    while pending:
        progressed = False
        for joint in list(pending):
            parent = world.get(joint["parent"])
            if parent is None:
                continue
            rotation = linalg.rotation_rpy(*joint["rpy"])
            origin = linalg.matmul(
                parent,
                (
                    (rotation[0][0], rotation[0][1], rotation[0][2], joint["origin"][0]),
                    (rotation[1][0], rotation[1][1], rotation[1][2], joint["origin"][1]),
                    (rotation[2][0], rotation[2][1], rotation[2][2], joint["origin"][2]),
                    (0.0, 0.0, 0.0, 1.0),
                ),
            )
            world[joint["child"]] = origin
            pending.remove(joint)
            progressed = True
        if not progressed:
            raise VerifyError("关节树断开，存在无法到达的 link")
    return world


# --- 单项检查 -----------------------------------------------------------


def _check_tree(links: dict[str, dict], joints: dict[str, dict]) -> list[Finding]:
    findings: list[Finding] = []
    if not joints:
        findings.append(Finding("OSV002", ERROR, "URDF 没有任何关节"))
        return findings
    children = [joint["child"] for joint in joints.values()]
    duplicates = {name for name in children if children.count(name) > 1}
    for name in sorted(duplicates):
        findings.append(Finding("OSV002b", ERROR, f"link {name} 被多个关节当作子节点", {"link": name}))
    unknown = sorted({value for joint in joints.values() for value in (joint["parent"], joint["child"])} - set(links))
    for name in unknown:
        findings.append(Finding("OSV002c", ERROR, f"关节引用了不存在的 link：{name}", {"link": name}))
    valid_types = {"revolute", "continuous", "prismatic", "fixed", "floating", "planar"}
    for name, joint in sorted(joints.items()):
        if joint["type"] not in valid_types:
            findings.append(
                Finding(
                    "OSV002e",
                    ERROR,
                    f"{name} 的关节类型非法：{joint['type']}（常见原因：把执行器类型写进了 joint_properties 的 type）",
                    {"joint": name, "type": joint["type"]},
                )
            )
    roots = sorted(set(links) - set(children))
    if len(roots) != 1:
        findings.append(Finding("OSV002d", ERROR, "URDF 不是单根树", {"roots": roots}))
    return findings


def _check_meshes(root: Path, links: dict[str, dict]) -> list[Finding]:
    findings: list[Finding] = []
    for name, link in sorted(links.items()):
        for reference in sorted(set(link["meshes"])):
            if not reference.startswith("../meshes/"):
                findings.append(Finding("OSV003", ERROR, f"{name} 的网格引用不是 ../meshes/<file>：{reference}"))
                continue
            path = root / "urdf" / reference
            if not path.is_file() or path.stat().st_size == 0:
                findings.append(Finding("OSV003b", ERROR, f"网格缺失或为空：{reference}"))
                continue
            try:
                box = stl.bounds(path)
            except ValueError as error:
                findings.append(Finding("OSV004", ERROR, f"{reference} 不是有效 STL：{error}"))
                continue
            extent = stl.extent(box)
            largest = max(extent)
            if box["triangles"] == 0 or largest <= 0:
                findings.append(Finding("OSV004b", ERROR, f"{reference} 是退化网格（无有效三角面）"))
            elif largest > MAX_EXTENT:
                findings.append(
                    Finding(
                        "OSV004c",
                        ERROR,
                        f"{reference} 尺寸异常（最大边 {largest:.3f} m），疑似单位错误",
                        {"extent": extent},
                    )
                )
            if box.get("degenerate_triangles"):
                findings.append(
                    Finding(
                        "OSV004d",
                        WARNING,
                        f"{reference} 含 {box['degenerate_triangles']} 个零面积三角面",
                        {"degenerate_triangles": box["degenerate_triangles"]},
                    )
                )
    return findings


def _check_masses(links: dict[str, dict]) -> list[Finding]:
    findings: list[Finding] = []
    for name, link in sorted(links.items()):
        if link["has_inertial"] and link["mass"] <= 0:
            findings.append(Finding("OSV005", ERROR, f"link {name} 质量非正：{link['mass']}（检查 Onshape 材质）"))
        elif not link["has_inertial"] and link["meshes"]:
            findings.append(Finding("OSV005b", WARNING, f"link {name} 有网格但没有惯性参数", {"link": name}))
    total = sum(link["mass"] for link in links.values())
    if total <= 0:
        findings.append(Finding("OSV005c", ERROR, "整机质量为零，模型无法用于动力学"))
    return findings


def _check_limits(joints: dict[str, dict]) -> list[Finding]:
    findings: list[Finding] = []
    for name, joint in sorted(joints.items()):
        if joint["type"] not in {"revolute", "prismatic"}:
            continue
        lower, upper = joint["lower"], joint["upper"]
        if lower is None or upper is None:
            findings.append(Finding("OSV006", WARNING, f"{name} 没有限位"))
            continue
        if lower >= upper:
            findings.append(Finding("OSV006b", ERROR, f"{name} 限位非法：[{lower}, {upper}]"))
        if joint["type"] == "revolute" and max(abs(lower), abs(upper)) > 2 * math.pi + 1e-6:
            findings.append(Finding("OSV006c", ERROR, f"{name} 限位超出 2π，疑似单位错误：[{lower}, {upper}]"))
        if abs(lower + upper) > 1e-6 and joint["type"] == "prismatic":
            findings.append(Finding("OSV006d", WARNING, f"{name} 平动限位不对称：[{lower}, {upper}]"))
    return findings


def _check_inertia(links: dict[str, dict]) -> list[Finding]:
    """惯量张量必须对称正定且满足主惯量三角不等式（工程规范要求）。"""

    findings: list[Finding] = []
    for name, link in sorted(links.items()):
        inertia = link.get("inertia")
        if link["mass"] <= MASSLESS_TOL:
            # 无质量参考系 link（body/imu/foot frame）允许零惯量：引擎用 1e-9 kg 占位。
            continue
        if inertia is None:
            findings.append(Finding("OSV030", ERROR, f"{name} 有质量但没有惯量张量"))
            continue
        ixx, ixy, ixz = inertia["ixx"], inertia["ixy"], inertia["ixz"]
        iyy, iyz, izz = inertia["iyy"], inertia["iyz"], inertia["izz"]
        # 对称正定：三个顺序主子式 > 0
        minor1 = ixx
        minor2 = ixx * iyy - ixy * ixy
        minor3 = ixx * (iyy * izz - iyz * iyz) - ixy * (ixy * izz - iyz * ixz) + ixz * (ixy * iyz - iyy * ixz)
        if min(minor1, minor2, minor3) <= 0:
            findings.append(
                Finding(
                    "OSV030b",
                    ERROR,
                    f"{name} 惯量张量非正定（主子式 {minor1:.3e}/{minor2:.3e}/{minor3:.3e}）",
                    {"link": name},
                )
            )
            continue
        # 三角不等式：任一主对角元素不得大于另外两者之和（等价于主惯量非负）
        for axis, value, other in (
            ("ixx", ixx, iyy + izz),
            ("iyy", iyy, ixx + izz),
            ("izz", izz, ixx + iyy),
        ):
            if value > other + 1e-12:
                findings.append(
                    Finding(
                        "OSV030c",
                        ERROR,
                        f"{name} 惯量违反三角不等式：{axis}={value:.6g} > {other:.6g}",
                        {"link": name},
                    )
                )
                break
        if all(abs(value) < 1e-15 for value in inertia.values()) and link["mass"] > 0:
            findings.append(Finding("OSV031", ERROR, f"{name} 惯量为零但有质量"))
    return findings


def _check_mjcf(
    root: Path,
    mjcf_path: Path,
    links: dict[str, dict],
    joints: dict[str, dict],
) -> list[Finding]:
    findings: list[Finding] = []
    if not mjcf_path.is_file():
        findings.append(Finding("OSV007", WARNING, "没有 mjcf/robot.xml，跳过 URDF↔MJCF 比对"))
        return findings
    try:
        tree = ET.parse(mjcf_path).getroot()
    except ET.ParseError as error:
        findings.append(Finding("OSV007b", ERROR, f"MJCF 无法解析：{error}"))
        return findings

    mesh_dir = "../meshes/"
    compiler = tree.find("compiler")
    if compiler is not None:
        mesh_dir = compiler.get("meshdir", mesh_dir).rstrip("/") + "/"
    if mesh_dir != "../meshes/":
        findings.append(Finding("OSV008", ERROR, f"MJCF meshdir 不是 ../meshes：{mesh_dir}"))

    masses: dict[str, float] = {}
    for body in tree.iter("body"):
        name = body.get("name", "")
        inertial = body.find("inertial")
        if name and inertial is not None:
            masses[name] = float(inertial.get("mass", "0"))
    for name, mass in sorted(masses.items()):
        link = links.get(name)
        if link is None:
            continue
        if abs(link["mass"] - mass) > MASS_TOL:
            findings.append(
                Finding(
                    "OSV009",
                    ERROR,
                    f"{name} 的 URDF/MJCF 质量不一致：{link['mass']} vs {mass}",
                    {"urdf": link["mass"], "mjcf": mass},
                )
            )
    ranges = {
        joint.get("name", ""): joint.get("range")
        for joint in tree.iter("joint")
        if joint.get("name") and joint.get("type") in {"hinge", "slide"}
    }
    for name, value in sorted(ranges.items()):
        joint = joints.get(name)
        if joint is None or joint["lower"] is None or value is None:
            continue
        lower, upper = (float(part) for part in value.split()[:2])
        if abs(lower - joint["lower"]) > RANGE_TOL or abs(upper - joint["upper"]) > RANGE_TOL:
            findings.append(
                Finding(
                    "OSV010",
                    ERROR,
                    f"{name} 的 URDF/MJCF 限位不一致：[{joint['lower']}, {joint['upper']}] vs [{lower}, {upper}]",
                )
            )
    return findings


def _check_against_onshape(assembly: Assembly, links: dict[str, dict], joints: dict[str, dict]) -> list[Finding]:
    """把导出结果与源装配体的 mate 帧比对（位置/朝向/限位/方向符号）。"""

    findings: list[Finding] = []
    try:
        world = forward_kinematics(links, joints)
    except VerifyError as error:
        findings.append(Finding("OSV011", ERROR, f"无法做 FK 比对：{error}"))
        return findings

    for mate in assembly.dof_mates:
        joint = joints.get(mate.joint_name)
        if joint is None:
            findings.append(Finding("OSV011b", ERROR, f"源 mate {mate.name} 在 URDF 里没有对应关节"))
            continue
        if not mate.resolved:
            continue
        origin, axis = assembly.mate_world_frame(mate)
        parent = world.get(joint["parent"])
        if parent is None:
            continue
        rotation = linalg.rotation_rpy(*joint["rpy"])
        joint_origin = linalg.matmul(parent, _translation_matrix(joint["origin"], rotation))
        exported_origin = linalg.translation(joint_origin)
        exported_axis = linalg.axis(
            linalg.matmul(
                joint_origin, linalg.from_axes((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), joint["axis"], (0.0, 0.0, 0.0))
            )
        )
        offset = linalg.distance(exported_origin, origin)
        if offset > POSITION_TOL:
            findings.append(
                Finding(
                    "OSV011c",
                    ERROR,
                    f"{mate.joint_name} 的世界位置与源 mate 差 {offset * 1000:.3f} mm",
                    {"onshape": origin, "urdf": exported_origin},
                )
            )
        angle = linalg.angle_deg(exported_axis, axis, signed=False)
        if angle > AXIS_TOL:
            findings.append(
                Finding(
                    "OSV011d",
                    ERROR,
                    f"{mate.joint_name} 的关节轴与源 mate 相差 {angle:.3f}°",
                    {"onshape": axis, "urdf": exported_axis},
                )
            )
        elif linalg.angle_deg(exported_axis, axis) > 90.0:
            findings.append(
                Finding(
                    "OSV012",
                    INFO,
                    f"{mate.joint_name} 的关节正方向与 mate 第一连接器相反"
                    "（引擎按树的父子方向决定符号；限位语义请用 --reference 与权威模型比对）",
                    {"onshape": axis, "urdf": exported_axis},
                )
            )
    return findings


def _check_mujoco_runtime(root: Path, links: dict[str, dict]) -> list[Finding]:
    findings: list[Finding] = []
    try:
        import mujoco
    except ModuleNotFoundError:
        findings.append(Finding("OSV013", WARNING, "未安装 mujoco，跳过真加载比对"))
        return findings
    scene = root / "mjcf" / "scene.xml"
    if not scene.is_file():
        findings.append(Finding("OSV013b", WARNING, "没有 scene.xml，跳过真加载比对"))
        return findings
    model = mujoco.MjModel.from_xml_path(str(scene))
    compared = 0
    for body_index in range(1, model.nbody):
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, body_index)
        link = links.get(name)
        if link is None:
            continue
        compared += 1
        if abs(float(model.body_mass[body_index]) - link["mass"]) > MASS_TOL:
            findings.append(
                Finding(
                    "OSV014",
                    ERROR,
                    f"{name} 真加载质量与 URDF 不一致：{model.body_mass[body_index]} vs {link['mass']}",
                )
            )
    findings.append(
        Finding(
            "OSV013c",
            INFO,
            f"MuJoCo 真加载通过：{compared}/{model.nbody - 1} 个 body 的质量与 URDF 一致"
            f"（{model.njnt} joints / {model.nu} actuators）",
            {
                "bodies_compared": compared,
                "mjcf_bodies": model.nbody - 1,
                "mjcf_joints": model.njnt,
                "actuators": model.nu,
            },
        )
    )
    return findings


def compare_with_reference(
    links: dict[str, dict],
    joints: dict[str, dict],
    reference_path: Path,
    *,
    com_tolerance: float = 0.02,
    mass_warning: float = 0.05,
) -> tuple[list[Finding], dict]:
    """与权威 URDF 逐关节比对：连杆映射、世界帧、轴向、**可达区间**与质量。

    可达区间用父子角色与轴向符号一起换算后再比，能识别"轴反向 + 非对称限位"这类
    只有物理语义才暴露的缺陷（见 docs/onshape_export.md 的案例）。

    返回 ``(findings, details)``；``details`` 是逐连杆/逐关节明细，用来回答
    "为什么判定通过"（写入 verification.json 的 ``reference_comparison``）。
    """

    findings: list[Finding] = []
    details: dict = {
        "reference": _reference_identity(reference_path),
        "links": {},
        "joints": [],
        "summary": {},
    }
    if not reference_path.is_file():
        return [Finding("OSV020", ERROR, f"参考模型不存在：{reference_path}")], details
    ref_links, ref_joints = parse_urdf(reference_path)
    try:
        our_world = forward_kinematics(links, joints)
        ref_world = forward_kinematics(ref_links, ref_joints)
    except VerifyError as error:
        return [Finding("OSV020b", ERROR, f"参考比对无法做 FK：{error}")], details

    mapping = _map_links_by_com(links, ref_links, our_world, ref_world, com_tolerance)
    if isinstance(mapping, Finding):
        findings.append(mapping)
    else:
        # mapping 是 ref link → 导出 link；关节匹配必须按这个方向查，否则会漏配。
        details["links"] = {
            ref_name: {
                "urdf_link": our_name,
                "com_distance_m": round(
                    linalg.distance(
                        _world_com(our_world[our_name], links[our_name]["com"]),
                        _world_com(ref_world[ref_name], ref_links[ref_name]["com"]),
                    ),
                    6,
                ),
            }
            for ref_name, our_name in sorted(mapping.items())
        }
        pairs = _match_joints(joints, ref_joints, mapping)
        for our_joint, ref_joint in pairs:
            joint_findings, row = _compare_joint(
                links, joints, our_world, our_joint, ref_joints, ref_world, ref_joint, mapping
            )
            findings += joint_findings
            details["joints"].append(row)
        details["summary"] = _reference_summary(details["joints"])
        findings += _compare_masses(links, ref_links, mapping, mass_warning)
        findings += _compare_inertias(links, ref_links, mapping, details)
        unmatched = len(ref_joints) - len(details["joints"])
        if unmatched > 0:
            findings.append(
                Finding(
                    "OSV020d",
                    ERROR,
                    f"有 {unmatched}/{len(ref_joints)} 个参考关节没有匹配到导出关节，比对结论不完整",
                    {"reference_joints": len(ref_joints), "compared": len(details["joints"])},
                )
            )
    return findings, details


def _map_links_by_com(links, ref_links, our_world, ref_world, tolerance):
    """世界质心最近邻映射（ref link → our link）；不唯一或过远时报错。"""

    mapping: dict[str, str] = {}
    for ref_name, ref_link in ref_links.items():
        ref_com = _world_com(ref_world[ref_name], ref_link["com"])
        best, best_distance = None, None
        for name, link in links.items():
            distance = linalg.distance(_world_com(our_world[name], link["com"]), ref_com)
            if best_distance is None or distance < best_distance:
                best, best_distance = name, distance
        if best_distance is None or best_distance > tolerance:
            return Finding(
                "OSV020",
                ERROR,
                f"参考 link {ref_name} 找不到对应的导出 link（最近 {best}：{best_distance:.4f} m）",
                {
                    "reference_link": ref_name,
                    "nearest": best,
                    "distance_m": None if best_distance is None else round(best_distance, 6),
                },
            )
        assert best is not None, "有距离就应该有最近邻"
        mapping[ref_name] = best
    duplicates = {name for name in mapping.values() if list(mapping.values()).count(name) > 1}
    if duplicates:
        return Finding("OSV020c", ERROR, f"连杆映射不唯一：{sorted(duplicates)}")
    return mapping


def _match_joints(joints, ref_joints, ref_to_our) -> list[tuple[str, str]]:
    """按"参考两端 link 映射到导出模型后的无序对"匹配关节。

    这样既不依赖关节命名，也不受根节点/父子方向影响；找不到对应 link 的关节会被跳过，
    因此调用方需要 ``joints_compared`` 来确认覆盖度（见 OSV020 系列规则）。
    """

    index: dict[frozenset, str] = {}
    for name, joint in joints.items():
        index[frozenset((joint["parent"], joint["child"]))] = name
    pairs: list[tuple[str, str]] = []
    for ref_name, ref_joint in ref_joints.items():
        key = frozenset((ref_to_our.get(ref_joint["parent"]), ref_to_our.get(ref_joint["child"])))
        if None in key:
            continue
        our_name = index.get(key)
        if our_name:
            pairs.append((our_name, ref_name))
    return sorted(pairs)


def _compare_joint(links, joints, our_world, our_name, ref_joints, ref_world, ref_name, ref_to_our):
    findings: list[Finding] = []
    ours, ref = joints[our_name], ref_joints[ref_name]
    our_matrix = _joint_world(our_world[ours["parent"]], ours)
    ref_matrix = _joint_world(ref_world[ref["parent"]], ref)
    offset = linalg.distance(linalg.translation(our_matrix), linalg.translation(ref_matrix))
    # 角色是否互换必须用"参考 link → 导出 link"的映射来判断，不能直接比字符串。
    swapped = (
        ref_to_our.get(ref["parent"]),
        ref_to_our.get(ref["child"]),
    ) == (ours["child"], ours["parent"])
    row: dict = {
        "joint": our_name,
        "reference_joint": ref_name,
        "parent": ours["parent"],
        "child": ours["child"],
        "reference_parent": ref["parent"],
        "reference_child": ref["child"],
        "roles": "swapped" if swapped else "same",
        "position_error_m": round(offset, 9),
        "axis_error_deg": None,
        "axis_direction": None,
        "range_flip": None,
        "our_limits": None if ours["lower"] is None else [ours["lower"], ours["upper"]],
        "reference_limits": None if ref["lower"] is None else [ref["lower"], ref["upper"]],
        "effective_limits": None,
        "verdict": "ok",
    }
    if offset > POSITION_TOL:
        row["verdict"] = "position_mismatch"
        findings.append(
            Finding(
                "OSV021",
                ERROR,
                f"{our_name} 世界位置与参考差 {offset * 1000:.3f} mm",
                {"urdf": linalg.translation(our_matrix), "reference": linalg.translation(ref_matrix)},
            )
        )
    if ours["type"] != ref["type"]:
        row["verdict"] = "type_mismatch"
        findings.append(Finding("OSV021b", ERROR, f"{our_name} 关节类型与参考不同：{ours['type']} vs {ref['type']}"))
    if ours["type"] == "fixed":
        return findings, row
    our_axis = _world_axis(our_matrix, ours["axis"])
    ref_axis = _world_axis(ref_matrix, ref["axis"])
    angle = linalg.angle_deg(our_axis, ref_axis, signed=False)
    row["axis_error_deg"] = round(angle, 6)
    if angle > AXIS_TOL:
        row["verdict"] = "axis_mismatch"
        findings.append(
            Finding(
                "OSV022", ERROR, f"{our_name} 关节轴与参考相差 {angle:.3f}°", {"urdf": our_axis, "reference": ref_axis}
            )
        )
        return findings, row
    sign = 1 if sum(a * b for a, b in zip(our_axis, ref_axis, strict=True)) > 0 else -1
    row["axis_direction"] = "aligned" if sign > 0 else "opposite"
    # 角色互换 = 关节角语义取反；轴反向同样取反。两者叠加才等价。
    flip = sign * (-1 if swapped else 1)
    row["range_flip"] = flip
    if ours["lower"] is None or ref["lower"] is None:
        return findings, row
    effective = tuple(sorted((flip * ours["lower"], flip * ours["upper"])))
    row["effective_limits"] = [effective[0], effective[1]]
    expected = (ref["lower"], ref["upper"])
    if abs(effective[0] - expected[0]) > RANGE_TOL or abs(effective[1] - expected[1]) > RANGE_TOL:
        row["verdict"] = "range_mismatch"
        findings.append(
            Finding(
                "OSV023",
                ERROR,
                f"{our_name} 的可达区间与参考不一致：导出等效 [{effective[0]:.5f}, {effective[1]:.5f}]"
                f" vs 参考 [{expected[0]:.5f}, {expected[1]:.5f}]",
                {"flip": flip, "urdf": [ours["lower"], ours["upper"]], "reference": [ref["lower"], ref["upper"]]},
            )
        )
    return findings, row


def _reference_summary(rows: list[dict]) -> dict:
    axes = [row for row in rows if row["axis_error_deg"] is not None]
    return {
        "joints_compared": len(rows),
        "max_position_error_m": max((row["position_error_m"] for row in rows), default=0.0),
        "max_axis_error_deg": max((row["axis_error_deg"] for row in axes), default=0.0),
        "roles_swapped": sum(1 for row in rows if row["roles"] == "swapped"),
        "axes_opposite": sum(1 for row in rows if row["axis_direction"] == "opposite"),
        "range_mismatch": sum(1 for row in rows if row["verdict"] == "range_mismatch"),
        "verdicts": sorted({row["verdict"] for row in rows}),
    }


def _compare_masses(links, ref_links, mapping, threshold) -> list[Finding]:
    findings: list[Finding] = []
    worst = []
    for ref_name, our_name in mapping.items():
        reference_mass = ref_links[ref_name]["mass"]
        our_mass = links[our_name]["mass"]
        if reference_mass <= 0:
            continue
        relative = abs(our_mass - reference_mass) / reference_mass
        if relative > threshold:
            worst.append((round(relative, 4), ref_name, our_name, round(reference_mass, 6), round(our_mass, 6)))
    if worst:
        findings.append(
            Finding(
                "OSV024",
                WARNING,
                f"{len(worst)} 个 link 质量与参考差异超过 {threshold:.0%}（材质/分组可能不同）",
                {"links": sorted(worst, reverse=True)[:10]},
            )
        )
    total_our = sum(link["mass"] for link in links.values())
    total_ref = sum(link["mass"] for link in ref_links.values())
    findings.append(
        Finding(
            "OSV025",
            INFO,
            f"总质量：导出 {total_our:.6f} kg vs 参考 {total_ref:.6f} kg",
            {"urdf": round(total_our, 6), "reference": round(total_ref, 6)},
        )
    )
    return findings


def _compare_inertias(links, ref_links, mapping, details, threshold: float = 0.10) -> list[Finding]:
    """按"单位质量惯量"（I/m，量纲 m²）比对惯量张量。

    质量差来自材质假设，直接比 I 会把密度差异混进来；I/m 只反映几何与质量分布，
    因此适合做跨模型的形状一致性核对。
    """

    findings: list[Finding] = []
    rows: list[dict] = []
    for ref_name, our_name in mapping.items():
        our_link, ref_link = links[our_name], ref_links[ref_name]
        if not our_link.get("inertia") or not ref_link.get("inertia"):
            continue
        our_mass, ref_mass = our_link["mass"], ref_link["mass"]
        if our_mass <= 0 or ref_mass <= 0:
            continue
        worst = None
        floor = 1e-9  # m²：避免接近零的分量把相对差放大
        reference_norm = 0.0
        difference_norm = 0.0
        for key in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz"):
            ours = our_link["inertia"][key] / our_mass
            reference = ref_link["inertia"][key] / ref_mass
            reference_norm += reference * reference
            difference_norm += (ours - reference) ** 2
            relative = abs(ours - reference) / max(abs(reference), floor)
            if worst is None or relative > worst[1]:
                worst = (key, relative, ours, reference)
        relative = (difference_norm**0.5) / max(reference_norm**0.5, floor)
        assert worst is not None, "inertia 比较在无分量时不生成行"
        rows.append(
            {
                "reference_link": ref_name,
                "urdf_link": our_name,
                "worst_component": worst[0],
                "relative_difference": round(relative, 4),
                "worst_component_relative_difference": round(worst[1], 4),
                "our_over_mass": worst[2],
                "reference_over_mass": worst[3],
            }
        )
    rows.sort(key=lambda row: row["relative_difference"], reverse=True)
    details["inertia"] = rows[:10]
    details["summary"]["inertia_compared"] = len(rows)
    details["summary"]["inertia_max_relative_difference"] = rows[0]["relative_difference"] if rows else None
    over = [row for row in rows if row["relative_difference"] > threshold]
    if over:
        findings.append(
            Finding(
                "OSV032",
                WARNING,
                f"{len(over)}/{len(rows)} 个 link 的单位质量惯量与参考差异超过 {threshold:.0%}"
                "（质量分布或零件归属不同）",
                {"links": over[:8]},
            )
        )
    if rows:
        findings.append(
            Finding(
                "OSV033",
                INFO,
                f"单位质量惯量最大相对差 {rows[0]['relative_difference']:.1%}"
                f"（{rows[0]['urdf_link']} 的 {rows[0]['worst_component']}）",
                {"links": rows[:3]},
            )
        )
    return findings


def _check_effort_velocity(joints: dict[str, dict], ref_joints: dict[str, dict]) -> list[Finding]:
    """effort/velocity 只做与参考的一致性核对；真实伺服限制来自控制器/数据手册。"""

    findings: list[Finding] = []
    compared = 0
    defaults = set()
    mismatches = []
    for name, joint in sorted(joints.items()):
        reference = ref_joints.get(name)
        if reference is None:
            continue
        compared += 1
        if joint["effort"] is not None:
            defaults.add((joint["effort"], joint["velocity"]))
        if (joint["effort"], joint["velocity"]) != (reference["effort"], reference["velocity"]):
            mismatches.append(
                {
                    "joint": name,
                    "urdf": [joint["effort"], joint["velocity"]],
                    "reference": [reference["effort"], reference["velocity"]],
                }
            )
    if mismatches:
        findings.append(
            Finding(
                "OSV034", ERROR, f"{len(mismatches)} 个关节的 effort/velocity 与参考不同", {"joints": mismatches[:5]}
            )
        )
    elif compared:
        values = ", ".join(f"{effort}/{velocity}" for effort, velocity in sorted(defaults))
        findings.append(
            Finding(
                "OSV034b",
                WARNING if defaults == {(10.0, 10.0)} else INFO,
                f"{compared} 个关节的 effort/velocity 与参考一致（{values}）；"
                "这是引擎默认值，不是 XL330 实际限制——控制器侧的力矩上限与速度限制另行记录",
                {"values": sorted(defaults)},
            )
        )
    return findings


def _check_self_collision(root: Path) -> list[Finding]:
    """在零位姿态下真加载 MJCF，统计自接触（不含地面；接触可能是有意限位，需人工判断）。"""

    findings: list[Finding] = []
    try:
        import mujoco
    except ModuleNotFoundError:
        return findings
    robot_xml = root / "mjcf" / "robot.xml"
    if not robot_xml.is_file():
        return findings
    model = mujoco.MjModel.from_xml_path(str(robot_xml))
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    pairs = sorted(
        {
            tuple(
                sorted(
                    (
                        mujoco.mj_id2name(
                            model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[data.contact[index].geom1]
                        ),
                        mujoco.mj_id2name(
                            model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[data.contact[index].geom2]
                        ),
                    )
                )
            )
            for index in range(data.ncon)
        }
    )
    findings.append(
        Finding(
            "OSV035",
            WARNING if pairs else INFO,
            f"零位自接触 {data.ncon} 处（{len(pairs)} 对刚体）"
            + ("；需要人工确认是否为设计的限位/贴合" if pairs else "；无自接触"),
            {"pairs": [list(pair) for pair in pairs[:10]], "contacts": int(data.ncon)},
        )
    )
    return findings


def _check_contact_excludes(robot_xml: Path) -> list[Finding]:
    """报告 MJCF 里声明的接触排除；URDF 没有对应机制，须让使用方看见这条差异。"""

    if not robot_xml.is_file():
        return []
    pairs = contacts.declared(robot_xml.read_text(encoding="utf-8"))
    if not pairs:
        return []
    listed = "、".join(f"{a} ↔ {b}" for a, b in pairs[:6])
    return [
        Finding(
            "OSV036",
            INFO,
            f"MJCF 声明 {len(pairs)} 对接触排除（{listed}）；这些刚体对在 MJCF 中不再自碰撞，"
            "URDF 与其他消费者不继承该设置",
            {"pairs": [list(pair) for pair in pairs]},
        )
    ]


def _compare_mjcf_dynamics(root: Path, reference_path: Path) -> list[Finding]:
    """与参考 MJCF 比对动力学参数：执行器类型、关节阻尼/摩擦/转子惯量。

    关节属性走 MuJoCo 解析（默认 class 继承由 MuJoCo 自己处理，比手写 XML 解析可靠）；
    执行器类型直接读 XML 标签（motor / position / general…）。
    """

    findings: list[Finding] = []
    our_xml = root / "mjcf" / "robot.xml"
    if not our_xml.is_file() or not Path(reference_path).is_file():
        return [Finding("OSV040", WARNING, f"缺少可比对的 MJCF：{our_xml} / {reference_path}")]
    try:
        import mujoco
    except ModuleNotFoundError:
        return [Finding("OSV040", INFO, "未安装 mujoco，跳过与参考 MJCF 的动力学参数比对")]

    ours = mujoco.MjModel.from_xml_path(str(our_xml))
    ref = mujoco.MjModel.from_xml_path(str(reference_path))

    def dof_values(model):
        values = {}
        for index in range(model.njnt):
            if model.jnt_type[index] == mujoco.mjtJoint.mjJNT_FREE:
                continue
            name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, index)
            dof = int(model.jnt_dofadr[index])
            values[name] = {
                "damping": float(model.dof_damping[dof]),
                "armature": float(model.dof_armature[dof]),
                "frictionloss": float(model.dof_frictionloss[dof]),
            }
        return values

    our_values, ref_values = dof_values(ours), dof_values(ref)
    mismatches = []
    for name, reference in sorted(ref_values.items()):
        current = our_values.get(name)
        if current is None:
            mismatches.append({"joint": name, "missing": True})
            continue
        for key, expected in reference.items():
            actual = current[key]
            if abs(actual - expected) > max(1e-6, abs(expected) * 0.01):
                mismatches.append({"joint": name, "property": key, "urdf": actual, "reference": expected})
    findings.append(
        Finding(
            "OSV041",
            ERROR if mismatches else INFO,
            f"关节阻尼/摩擦/转子惯量与参考 MJCF {'不一致' if mismatches else '一致'}"
            f"（{len(ref_values)} 个关节比对，差异 {len(mismatches)} 处）",
            {"mismatches": mismatches[:10]},
        )
    )

    our_actuators = _actuator_types(our_xml)
    ref_actuators = _actuator_types(Path(reference_path))
    type_mismatch = [
        {"joint": joint, "urdf": our_actuators.get(joint), "reference": kind}
        for joint, kind in sorted(ref_actuators.items())
        if our_actuators.get(joint) != kind
    ]
    findings.append(
        Finding(
            "OSV040",
            ERROR if type_mismatch else INFO,
            f"执行器类型与参考 MJCF {'不一致' if type_mismatch else '一致'}"
            f"（我们 {len(our_actuators)} 个 / 参考 {len(ref_actuators)} 个）",
            {"mismatches": type_mismatch[:10]},
        )
    )
    return findings


def _actuator_types(path: Path) -> dict[str, str]:
    """读 MJCF 的 actuator 段：{joint 名: 标签名}（motor / position / general…）。"""

    types: dict[str, str] = {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return types
    for actuator in root.findall("actuator"):
        for entry in actuator:
            joint = entry.get("joint")
            if joint:
                types[joint] = entry.tag
    return types


def _world_com(matrix: linalg.Matrix, com) -> tuple[float, float, float]:
    return (
        matrix[0][0] * com[0] + matrix[0][1] * com[1] + matrix[0][2] * com[2] + matrix[0][3],
        matrix[1][0] * com[0] + matrix[1][1] * com[1] + matrix[1][2] * com[2] + matrix[1][3],
        matrix[2][0] * com[0] + matrix[2][1] * com[1] + matrix[2][2] * com[2] + matrix[2][3],
    )


def _reference_identity(path: Path) -> dict:
    """记录参照模型的身份（文件名 + SHA-256），而不是本机路径。"""

    import hashlib

    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None
    return {"file": Path(path).name, "sha256": digest}


def _joint_world(parent: linalg.Matrix, joint: dict) -> linalg.Matrix:
    return linalg.matmul(parent, _translation_matrix(joint["origin"], linalg.rotation_rpy(*joint["rpy"])))


def _world_axis(joint_matrix: linalg.Matrix, axis) -> tuple[float, float, float]:
    """把 URDF 的 ``axis``（定义在关节坐标系里）旋转到世界系。

    不能拿关节坐标系的 z 轴代替：只有 ``axis="0 0 1"`` 时两者才相同。
    """

    return tuple(sum(joint_matrix[row][col] * axis[col] for col in range(3)) for row in range(3))


# --- 工具 ---------------------------------------------------------------


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


def _xyz(value: str) -> tuple[float, float, float]:
    parts = [float(item) for item in value.split()]
    while len(parts) < 3:
        parts.append(0.0)
    return (parts[0], parts[1], parts[2])


def _number(value: str | None) -> float | None:
    return None if value is None else float(value)


def _translation_matrix(origin, rotation) -> linalg.Matrix:
    return (
        (rotation[0][0], rotation[0][1], rotation[0][2], origin[0]),
        (rotation[1][0], rotation[1][1], rotation[1][2], origin[1]),
        (rotation[2][0], rotation[2][1], rotation[2][2], origin[2]),
        (0.0, 0.0, 0.0, 1.0),
    )
