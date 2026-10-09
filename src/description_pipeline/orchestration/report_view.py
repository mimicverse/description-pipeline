"""Readable Chinese report projection over recorded run evidence (display only).

Everything here is derived from data the pipeline already records — the stage contract view
(``stages.stage_view``), the delivered quality document and the job error/detail fields. This
module adds NO verification decisions: it relabels recorded states, distinguishes a failed
stage from downstream stages that were never executed, and surfaces exact measured values
(mass window and mass closure separately). Labels are display metadata keyed by existing check
identifiers; the stage contract itself is never changed, so historical evidence keeps its
recorded contract hashes.
"""

from __future__ import annotations

from pathlib import PureWindowsPath

from ..stages import stage_view

REPORT_SCHEMA = "solidworks-to-urdf.report/v1"

_STAGE_STATE_ZH = {
    "completed": "已完成",
    "running": "进行中",
    "failed": "失败",
    "blocked": "上游阶段失败（未执行）",
    "not_run": "未执行",
}
_CHECK_STATE_ZH = {
    "passed": "通过",
    "failed": "失败",
    "not_run": "未执行（未获得结果）",
}
_OVERALL_STATE_ZH = {
    "passed": "已通过",
    "running": "进行中",
    "failed": "失败",
    "not_run": "未开始",
}

_BOUNDARY_LABELS_ZH = {
    "handoff.admission": "交接包准入（路径、常规文件与原生包）",
    "handoff.integrity": "交接包完整性（摘要与文件清单）",
    "discovery.inputs": "解析输入（原生文件夹可用）",
    "discovery.definition": "结构定义生成",
    "discovery.binding": "结构与身份绑定",
    "input.valid": "输入有效性（只读快照语义）",
    "runtime.ready": "原生运行环境就绪",
    "capture.integrity": "证据采集完整性（字节与清单）",
    "capture.input_stability": "输入稳定性复核",
    "generation.inputs": "生成输入有效性",
    "generation.artifacts": "生成产物校验（URDF、网格与报告）",
    "verification.subject": "交付主体摘要绑定",
    "verification.gates": "独立校验门（聚合）",
    "verification.report_binding": "校验报告与主体绑定",
    "publication.inputs": "发布输入（分支与基线）",
    "publication.git": "发布提交字节一致性（复制/暂存/提交）",
    "publication.receipt": "评审分支回执确认",
}

#: Chinese descriptions for the contract's declared input/output records (by path string).
_FILE_PATH_LABELS_ZH = {
    "handoff/": "已保存的 SolidWorks 工程文件夹",
    "input/discovery/": "原生观察与发现记录",
    "input/robot.yaml + input/cad-revision.json": "派生定义与结构修订",
    "input/": "准备后的输入定义与保存的 CAD",
    "evidence/": "采集的 CAD 证据、原始测量与清单",
    "model/robot.json": "规范化模型",
    "urdf/ + meshes/": "URDF 与本地网格",
    "reports/tool.json": "工具身份与文件主体",
    "input/ + evidence/ + model/ + urdf/ + meshes/": "原始证据与生成产物",
    "reports/quality.json": "确定性逐对象质量报告",
    "urdf/ + meshes/ + reports/quality.json": "已验证交付与配置的模型仓库",
    "reports/pr.json": "候选提交与评审回执",
}

_INDEPENDENT_LABELS_ZH = {
    # The fixed required checks (solidworks_urdf.required_checks).
    "bundle.subject": "交付包主体摘要绑定",
    "input.valid": "输入有效性（独立复核）",
    "source.native_discovery": "原生结构发现证据",
    "tool.identity": "工具版本身份",
    "source.integrity": "原生源完整性",
    "source.native": "原生源存在性",
    "model.schema": "模型模式符合性",
    "source.raw": "原生原始证据存在性",
    "physics.mass_closure_equality": "质量闭合（URDF 质量 = 整机 CAD 质量）",
    "source.dependencies": "原生依赖闭包",
    "source.coverage": "原生源覆盖率",
    "verification.complete": "独立校验完成标记",
    "model.policy": "模型策略符合性",
    "physics.independent": "物理量独立计算复核",
    "frames.native": "原生坐标系",
    "frames.components": "组件坐标系",
    "frames.references": "参考坐标系",
    "urdf.syntax_names": "URDF 语法与命名",
    "urdf.topology": "URDF 拓扑",
    "geometry.coverage": "几何网格覆盖",
    "geometry.assets": "几何网格资产",
    "geometry.expected_extent": "外形尺寸期望范围",
    "physics.expected_mass": "质量期望范围",
    "consumer.urdf": "消费端 URDF 可加载性",
    # Physics-module variants that can accompany the fixed set.
    "physics.authority": "物理证据权威性",
    "physics.closure": "物理质量闭合",
    "physics.link": "物理链接一致性",
    "physics.raw_inputs": "物理原始输入",
    "physics.reading": "物理读数",
}

_INDEPENDENT_FAMILIES = (
    ("inertia.", "惯量：{name}"),
    ("geometry.", "几何：{name}"),
    ("joints.", "关节：{name}"),
    ("shafts.", "轴：{name}"),
    ("frames.", "坐标系：{name}"),
)


def independent_label(identifier: object) -> str:
    """Chinese label for one independent check id; family patterns, then the raw id."""
    name = str(identifier or "")
    if name in _INDEPENDENT_LABELS_ZH:
        return _INDEPENDENT_LABELS_ZH[name]
    for prefix, template in _INDEPENDENT_FAMILIES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return template.format(name=name[len(prefix) :])
    return name


def _short(value: object, length: int = 12) -> str:
    text = str(value or "未记录")
    return text[:length] + "…" if len(text) > length else text


def _failure_object(raw_type: str, message: str, raw_detail: dict | None) -> str | None:
    """Basename of the failing document, only when safely parsed; never invented."""
    if isinstance(raw_detail, dict):
        candidate = raw_detail.get("document") or raw_detail.get("object")
        if isinstance(candidate, str) and candidate.strip():
            name = PureWindowsPath(candidate.strip()).name
            if name:
                return name
    if raw_type == "CadError":
        text = message.strip().strip('"')
        if ("\\" in text or "/" in text) and not text.startswith("{"):
            name = PureWindowsPath(text).name
            if name:
                return name
    return None


def _range_text(values: object) -> str | None:
    if (
        isinstance(values, (list, tuple))
        and len(values) == 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in values)
    ):
        return f"{values[0]}–{values[1]} kg"
    return None


def _boundary_summary(identifier: object, details: dict) -> dict | None:
    """Scope/expected/actual for one recorded boundary check, when the evidence supports it."""
    name = str(identifier or "")
    if name == "verification.gates":
        required = details.get("required_checks")
        rows = details.get("checks")
        if isinstance(required, list) and isinstance(rows, list):
            passed = sum(
                1
                for row in rows
                if isinstance(row, dict)
                and row.get("id") in required
                and row.get("state") == "passed"
                and row.get("passed") is True
            )
            return {
                "scope_zh": "全部必需独立校验",
                "expected": f"{len(required)} 项必需校验全部通过",
                "actual": f"{passed}/{len(required)} 通过（已记录 {len(rows)} 项）",
            }
        subject = details.get("subject_sha256")
        status = details.get("subject_status")
        if isinstance(subject, str) or status:
            return {
                "scope_zh": "独立校验报告",
                "expected": "必需校验全部通过且主体绑定",
                "actual": f"主体 {_short(subject)}（{status or '未标注'}）",
            }
        return None
    if name == "handoff.admission":
        return {
            "scope_zh": "交接包导入",
            "expected": "记录导入包与摘要",
            "actual": f"摘要 {_short(details.get('handoff_sha256'))}，已冻结导入包",
        }
    if name == "handoff.integrity":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope_zh": "交接包文件清单",
            "expected": "清单与交接摘要一致",
            "actual": f"{count} 个文件，清单校验通过" if count is not None else _short(details.get("handoff_sha256")),
        }
    if name == "discovery.inputs":
        return {
            "scope_zh": "冻结文件与命名注册",
            "expected": "冻结清单与命名注册表一致",
            "actual": f"清单 {_short(details.get('files_sha256'))}、命名 {_short(details.get('frozen_names_sha256'))}",
        }
    if name == "discovery.definition":
        findings = details.get("findings")
        blocked = (
            sum(1 for item in findings if isinstance(item, dict) and item.get("blocking") is not False)
            if isinstance(findings, list)
            else None
        )
        return {
            "scope_zh": "原生结构定义",
            "expected": "生成通过的原生结构定义",
            "actual": f"型号 {details.get('hardware_id') or '未记录'}，修订 {details.get('revision') or '未记录'}，"
            f"阻塞发现 {blocked if blocked is not None else '未记录'} 项",
        }
    if name == "discovery.binding":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        actual = (
            f"型号 {details.get('hardware_id') or '未记录'} → "
            f"{details.get('repository_slug') or '未记录'}@{details.get('base') or '未记录'}"
        )
        if count is not None:
            actual += f"，{count} 个准备文件"
        return {"scope_zh": "结构定义与目标仓库绑定", "expected": "绑定摘要与目标一致", "actual": actual}
    if name == "input.valid":
        revision = details.get("cad_revision")
        if isinstance(revision, dict):
            revision = revision.get("revision")
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope_zh": "CAD 包输入规范",
            "expected": "CAD 包通过输入规范并归档一致",
            "actual": f"CAD 修订 {revision or '未记录'}" + (f"，{count} 个文件" if count is not None else ""),
        }
    if name == "runtime.ready":
        return {
            "scope_zh": "原生运行环境",
            "expected": "读取器与原生平台就绪",
            "actual": f"{details.get('reader') or '读取器未记录'} 环境自检模型："
            f"{details.get('bodies', '未记录')} 个刚体、{details.get('joints', '未记录')} 个关节",
        }
    if name == "capture.integrity":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope_zh": "证据快照",
            "expected": "快照与清单逐字节一致",
            "actual": f"{count if count is not None else '未记录'} 个证据文件，"
            f"清单 {_short(details.get('manifest_sha256'))}",
        }
    if name == "capture.input_stability":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope_zh": "输入稳定性",
            "expected": "采集期间输入文件未变化",
            "actual": f"{count if count is not None else '未记录'} 个输入文件保持不变",
        }
    if name == "generation.inputs":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope_zh": "生成前证据快照",
            "expected": "快照有效且未变化",
            "actual": f"证据快照 {count if count is not None else '未记录'} 个文件",
        }
    if name == "generation.artifacts":
        files = details.get("files")
        count = len(files) if isinstance(files, dict) else None
        return {
            "scope_zh": "生成产物",
            "expected": "产物完整并绑定交付主体",
            "actual": f"{count if count is not None else '未记录'} 个产物文件，"
            f"主体 {_short(details.get('subject_sha256'))}",
        }
    if name == "verification.subject":
        return {
            "scope_zh": "交付主体",
            "expected": "主体与生成结果一致",
            "actual": _short(details.get("subject_sha256")),
        }
    if name == "verification.report_binding":
        return {
            "scope_zh": "质量报告绑定",
            "expected": "报告摘要与交付主体绑定",
            "actual": f"报告 {_short(details.get('report_sha256'))} 绑定主体 {_short(details.get('subject_sha256'))}",
        }
    if name == "publication.inputs":
        return {
            "scope_zh": "评审基线与确定性分支",
            "expected": "基线与确定分支校验通过",
            "actual": f"{details.get('repository_slug') or '未记录'}: "
            f"{details.get('base') or '未记录'} ← {details.get('branch') or '未记录'}",
        }
    if name == "publication.git":
        return {
            "scope_zh": "复制、暂存与提交的字节一致性",
            "expected": "提交与交付字节一致",
            "actual": f"提交 {_short(details.get('commit'))}，"
            f"字节复核 {details.get('copied_staged_committed') or '未记录'}",
        }
    if name == "publication.receipt":
        return {
            "scope_zh": "评审分支回执",
            "expected": "回执状态与提交一致",
            "actual": f"状态 {details.get('state') or '未记录'}，提交 {_short(details.get('commit'))}",
        }
    return None


def _independent_summary(identifier: object, details: dict) -> dict | None:
    """Scope/expected/actual for one independent check, exact values only when recorded."""
    name = str(identifier or "")
    if name == "physics.expected_mass":
        mass = details.get("mass_kg")
        window = _range_text(details.get("expected_kg"))
        if isinstance(mass, (int, float)) and not isinstance(mass, bool):
            return {"scope_zh": "质量在期望窗口内", "expected": window or "期望窗口", "actual": f"{mass} kg"}
        return None
    if name in {"physics.mass_closure_equality", "physics.closure"}:
        urdf, whole = details.get("urdf_mass_kg"), details.get("whole_cad_mass_kg")
        if isinstance(urdf, (int, float)) and isinstance(whole, (int, float)):
            delta = details.get("delta_kg")
            return {
                "scope_zh": "URDF 质量与整机 CAD 质量一致",
                "expected": f"绝对误差 ≤ {details['atol_kg']} kg"
                if isinstance(details.get("atol_kg"), (int, float)) and details.get("rtol") == 0
                else "与整机 CAD 质量一致（按记录的数值容差）",
                "actual": f"URDF {urdf} kg / CAD {whole} kg"
                + (f"（差值 {delta} kg）" if isinstance(delta, (int, float)) else "（差值未记录）"),
            }
        return None
    if name == "consumer.urdf":
        bodies = details.get("bodies")
        version = details.get("version")
        if isinstance(bodies, int):
            return {
                "scope_zh": "消费端（MuJoCo）可加载",
                "expected": "可加载且刚体完整",
                "actual": f"{bodies} 个消费端刚体（含世界坐标体），版本 {version or '未记录'}",
            }
        return None
    if name == "geometry.expected_extent":
        extent = details.get("extent_m")
        expected = details.get("expected_largest_m")
        if isinstance(extent, list) and isinstance(expected, list):
            return {"scope_zh": "外形尺寸", "expected": f"最大边 {expected} m", "actual": f"三轴尺寸 {extent} m"}
        return None
    if name == "bundle.subject":
        return {
            "scope_zh": "交付包主体摘要",
            "expected": "主体摘要与交付一致",
            "actual": _short(details.get("sha256")),
        }
    if name == "tool.identity":
        return {
            "scope_zh": "工具身份",
            "expected": "版本与来源摘要记录",
            "actual": f"版本 {details.get('version') or '未记录'}，来源 {_short(details.get('source_sha256'))}",
        }
    if name == "source.native":
        return {
            "scope_zh": "原生源信息",
            "expected": "记录 SolidWorks 修订与配置",
            "actual": f"SolidWorks {details.get('solidworks_revision') or '未记录'}，"
            f"配置 {details.get('configuration') or '未记录'}",
        }
    if name == "source.dependencies" and isinstance(details.get("native_documents"), int):
        return {
            "scope_zh": "原生依赖与采集副本覆盖",
            "expected": "每个原生文档均有完整、可追溯的采集副本",
            "actual": f"原生 {details['native_documents']} 个文档，"
            f"副本 {details.get('collected_documents', '未记录')} 个，"
            f"抑制实例 {details.get('suppressed_instances', '未记录')} 个",
        }
    if name in {"source.coverage", "source.native_discovery"} and isinstance(details.get("bodies"), int):
        field, label = ("occurrences", "实例") if name == "source.coverage" else ("joints", "关节")
        return {
            "scope_zh": "实例与刚体覆盖" if name == "source.coverage" else "原生结构定义",
            "expected": "独立重建结果与定义一致",
            "actual": f"{details['bodies']} 个刚体，{details.get(field, '未记录')} 个{label}",
        }
    if name.startswith("shafts.") and isinstance(details.get("angle_deg"), (int, float)):
        return {
            "scope_zh": "关节轴与原生圆柱轴对齐",
            "expected": "轴线共线、原点在轴线上；按质量规范的数值容差验收",
            "actual": f"夹角 {details['angle_deg']}°，轴线偏距 {details.get('offset_m', '未记录')} m",
        }
    if name.startswith("inertia.") and isinstance(details.get("mass_kg"), (int, float)):
        return {
            "scope_zh": "刚体质量与惯量",
            "expected": "质量及主惯量为正，惯量满足三角关系并与独立计算一致",
            "actual": f"质量 {details['mass_kg']} kg；主惯量 {details.get('principal_inertia_kg_m2', '未记录')} kg·m²",
        }
    if name.startswith("joints.") and isinstance(details.get("type"), str):
        kind = details["type"]
        limits = details.get("limits") or {}
        unit = "m" if kind == "prismatic" else "rad"
        actual = f"类型 {kind}，有符号轴 {details.get('axis', '未记录')}"
        if isinstance(limits, dict) and "lower" in limits and "upper" in limits:
            actual += f"，范围 [{limits['lower']}, {limits['upper']}] {unit}"
        return {"scope_zh": "关节类型、轴向与限位", "expected": "输出与原生结构定义一致", "actual": actual}
    if name == "urdf.syntax_names":
        if details.get("validated") is True:
            return {"scope_zh": "URDF 语法与命名", "expected": "语法与命名校验通过", "actual": "校验通过"}
        return None
    if name == "urdf.topology":
        links, root = details.get("links"), details.get("root")
        if isinstance(links, int):
            return {"scope_zh": "URDF 拓扑", "expected": "单一根链接且连通", "actual": f"{links} 个链接，根 {root}"}
        return None
    if name in {"geometry.coverage", "geometry.assets"}:
        meshes = details.get("meshes")
        if isinstance(meshes, int):
            return {"scope_zh": "几何网格", "expected": "全部刚体网格存在", "actual": f"{meshes} 个网格"}
        return None
    if name == "input.valid":
        if details.get("passed") is True or details.get("validated") is True:
            return {"scope_zh": "输入规范", "expected": "输入包符合规范", "actual": "规范校验通过"}
        return None
    return None


def _failure(job: dict, stage: dict) -> dict:
    """Safe, non-asserting failure meaning; raw evidence always preserved."""
    raw_error = str(stage.get("error") or "")
    raw_type, _, message = raw_error.partition(": ")
    diagnostic = stage.get("diagnostic")
    raw_detail = diagnostic if isinstance(diagnostic, dict) and diagnostic else None
    codes = None
    if stage.get("error_code") == "native_discovery_main_assembly_ambiguous":
        title = "无法唯一确定主装配"
        meaning = (
            "请在交付主装配中保存 dp.hardware_id 和 dp.delivery_configuration；"
            "多个装配声明交付身份时须明确唯一入口，流水线不会按文件名或大小猜测。"
        )
    elif raw_type == "CadError" and raw_detail and {"errors", "warnings"} <= set(raw_detail):
        codes = {"errors": raw_detail.get("errors"), "warnings": raw_detail.get("warnings")}
        if raw_detail.get("errors") == 2:
            title = "原生 CAD 打开失败"
            meaning = (
                "文件或其引用的文档无法定位；影响范围尚未证明，"
                "请由平台维护人员核实该文档及其引用对本次交付的必要性后再处理。"
            )
        else:
            title = "原生 CAD 打开错误"
            meaning = (
                f"原生会话返回错误码 {raw_detail.get('errors')}（警告 {raw_detail.get('warnings')}）；"
                "无法据此判定文件缺失，请由平台维护人员核实后处理。"
            )
    elif raw_type == "CadError":
        title = "原生 CAD 阶段失败"
        meaning = "当前文档或其依赖无法被读取，阶段未完成；请按诊断信息核查后重试。"
    elif raw_type in {"EnvironmentError_", "EnvironmentError"}:
        title = "原生运行环境不可用"
        meaning = "Windows 原生环境未就绪（服务或许可等），阶段未完成；请联系平台维护人员。"
    elif raw_type == "TimeoutError" or "modal" in message.lower():
        title = "原生会话被阻塞"
        meaning = "原生会话被对话框或超时阻塞，阶段未完成；请在 Windows 会话上处理后重试。"
    else:
        title = "阶段失败（原因未判定）"
        meaning = "阶段未完成，且未获得可判定的原因；请提供运行编号联系平台维护人员。"
    failure = {
        "stage": stage.get("id"),
        "stage_name_zh": stage.get("name_zh"),
        "title_zh": title,
        "meaning_zh": meaning,
        "raw_type": raw_type or None,
        "error_code": stage.get("error_code"),
        "raw_error": raw_error or None,
        "raw_detail": raw_detail,
    }
    failed_object = _failure_object(raw_type, message, raw_detail)
    if failed_object:
        failure["object"] = failed_object
    if codes is not None:
        failure["solidworks_codes"] = codes
    unresolved = _unresolved_dependencies(raw_detail)
    if unresolved:
        failure["unresolved_dependencies"] = unresolved
        names = "、".join(item.get("name") or item.get("path") or "" for item in unresolved)
        failure["meaning_zh"] = (
            f"文件或其引用的文档无法定位（原生会话报告以下引用未解析：{names}）；"
            "这些引用是否被当前配置实际使用尚未证明，请由维护人员核实影响后再处理。"
        )
    if codes is not None:
        applicability = raw_detail.get("applicability") if isinstance(raw_detail, dict) else None
        failure["applicability"] = applicability if applicability in {"proven", "unproven"} else "unproven"
    return failure


def _unresolved_dependencies(raw_detail: dict | None) -> list[dict] | None:
    """Exact unresolved reference rows when the native diagnostic carries them (never invented)."""
    if not isinstance(raw_detail, dict):
        return None
    for key in ("unresolved_dependencies", "unresolved_references", "unresolved"):
        value = raw_detail.get(key)
        if not isinstance(value, list):
            continue
        rows = []
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("file") or item.get("document") or item.get("id")
                path = item.get("path") or item.get("last_known_path") or item.get("reference")
                if name or path:
                    rows.append({"name": name, "path": path})
            elif isinstance(item, str) and item:
                rows.append({"name": item, "path": None})
        if rows:
            return rows
    return None


def _counts(rows: list[dict]) -> dict:
    executed = [row for row in rows if row.get("executed")]
    return {
        "passed": sum(1 for row in executed if row.get("state") == "passed"),
        "executed": len(executed),
        "total": len(rows),
    }


def _failed_summary(label: str, summary: dict | None, details: dict) -> dict:
    """Failed rows must carry the recorded reason, never a blank or digest-only actual."""
    reason = details.get("error") or details.get("message") or details.get("reason")
    errors = details.get("errors")
    if not isinstance(reason, str) and isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            reason = first.get("message") or first.get("code")
    row = dict(summary) if summary else {"scope_zh": f"{label}未通过"}
    row.setdefault("expected", "检查通过")
    row["actual"] = f"未通过：{str(reason)[:160]}" if reason else "未通过（未提供原因）"
    return row


def build_report(job: dict | None = None, *, view: dict | None = None) -> dict:
    """Readable report projection; pure display mapping over recorded evidence."""
    job = job if isinstance(job, dict) else {}
    view = view if isinstance(view, dict) else stage_view(job)
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
    quality_rows = [row for row in (quality.get("checks") or []) if isinstance(row, dict)]
    stages_out: list[dict] = []
    first_failed: dict | None = None
    measured_window: dict | None = None
    measured_closure: dict | None = None
    for stage in view.get("stages", []):
        boundary_rows = []
        for boundary in ("input", "output"):
            for item in stage.get(f"{boundary}_qc", []):
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                state = item.get("state") if item.get("state") in {"passed", "failed"} else "not_run"
                row = {
                    "id": item.get("id"),
                    "boundary": boundary,
                    "label_zh": _BOUNDARY_LABELS_ZH.get(item.get("id"), str(item.get("label") or item.get("id") or "")),
                    "state": state,
                    "state_zh": _CHECK_STATE_ZH[state],
                    "executed": state != "not_run",
                    "raw_details": details,
                    **({"reuse": stage["reuse"]} if stage.get("reuse") else {}),
                }
                summary = _boundary_summary(item.get("id"), details)
                if state == "failed":
                    summary = _failed_summary(row["label_zh"], summary, details)
                elif state == "not_run":
                    summary = {**(summary or {}), "actual": "未执行，尚无检查结果"}
                if summary is not None:
                    row["summary"] = summary
                boundary_rows.append(row)
        independent_rows: list[dict] = []
        if stage.get("id") == "verify":
            for item in quality_rows:
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                declared = item.get("state")
                if declared in {"passed", "failed", "not_run"}:
                    state = declared
                    if state == "passed" and item.get("passed") is False:
                        state = "failed"
                elif item.get("passed") is True:
                    state = "passed"
                elif item.get("passed") is False:
                    state = "failed"
                else:
                    state = "not_run"
                row = {
                    "id": item.get("id"),
                    "label_zh": independent_label(item.get("id")),
                    "state": state,
                    "state_zh": _CHECK_STATE_ZH[state],
                    "executed": state != "not_run",
                    "raw_details": details,
                    **({"reuse": stage["reuse"]} if stage.get("reuse") else {}),
                }
                summary = _independent_summary(item.get("id"), details)
                if state == "failed":
                    summary = _failed_summary(row["label_zh"], summary, details)
                elif state == "not_run":
                    summary = {**(summary or {}), "actual": "未执行，尚无检查结果"}
                if summary is not None:
                    row["summary"] = summary
                independent_rows.append(row)
                if item.get("id") == "physics.expected_mass" and isinstance(details.get("mass_kg"), (int, float)):
                    measured_window = {"mass_kg": details.get("mass_kg"), "expected_kg": details.get("expected_kg")}
                if item.get("id") in {"physics.mass_closure_equality", "physics.closure"} and isinstance(
                    details.get("urdf_mass_kg"), (int, float)
                ):
                    measured_closure = {
                        "urdf_mass_kg": details.get("urdf_mass_kg"),
                        "whole_cad_mass_kg": details.get("whole_cad_mass_kg"),
                        "delta_kg": details.get("delta_kg"),
                    }
        stage_out = {
            "id": stage.get("id"),
            "name_zh": stage.get("name_zh"),
            "state": stage.get("state", "not_run"),
            "state_zh": _STAGE_STATE_ZH.get(stage.get("state"), str(stage.get("state") or "未执行")),
            "at": stage.get("at"),
            **({"reuse": stage["reuse"]} if stage.get("reuse") else {}),
            "in_scope": stage.get("in_scope"),
            "counts": {
                "boundary": _counts(boundary_rows),
                "independent": _counts(independent_rows) if independent_rows else None,
            },
            "boundary": boundary_rows,
            "files": [
                {
                    "boundary": boundary,
                    "label_zh": "冻结交接包与清单"
                    if stage.get("id") == "freeze" and boundary == "output"
                    else _FILE_PATH_LABELS_ZH.get(
                        str(record.get("path") or ""), str(record.get("label") or record.get("path") or "")
                    ),
                    "path": record.get("path"),
                    "availability": record.get("availability"),
                    "files": len(record.get("files") or {}),
                }
                for boundary, records in (("input", stage.get("inputs", [])), ("output", stage.get("outputs", [])))
                for record in records
            ],
            "automatic": {
                "passed_labels": [
                    row["label_zh"] for row in boundary_rows if row["executed"] and row["state"] == "passed"
                ]
                + [row["label_zh"] for row in independent_rows if row["executed"] and row["state"] == "passed"],
                "failed_labels": [
                    row["label_zh"] for row in boundary_rows if row["executed"] and row["state"] == "failed"
                ]
                + [row["label_zh"] for row in independent_rows if row["executed"] and row["state"] == "failed"],
            },
            "unsupported": [
                {"id": item.get("id"), "label": item.get("label")} for item in stage.get("unsupported", [])
            ],
            "confirmations": [
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "label",
                        "reference",
                        "review_stage",
                        "scope",
                        "automatic_exclusion",
                        "state",
                        "approval_tracking",
                    )
                }
                for item in stage.get("confirmations", [])
            ],
        }
        if stage.get("state") in {"completed", "running"}:
            stage_out["manual_scope_note_zh"] = (
                "以下为需外部评审确认的事实范围；平台不读取评审批准记录，重跑时仅相关事实发生变化才需要重新确认。"
            )
        else:
            stage_out["manual_scope_note_zh"] = "自动校验未通过或未执行：先修复问题并完成自动校验，人工确认项不适用。"
        if independent_rows:
            stage_out["independent"] = independent_rows
        if stage.get("state") == "failed" and first_failed is None:
            first_failed = stage
        stages_out.append(stage_out)
    states = {stage["state"] for stage in stages_out}
    if "failed" in states:
        overall_state, headline = "failed", f"{first_failed['name_zh']}阶段失败，后续阶段未执行"
    elif job.get("status") == "failed":
        overall_state, headline = "failed", "作业失败，请查看原始诊断"
    elif "running" in states:
        overall_state, headline = "running", "运行进行中，尚未完成全部阶段"
    elif stages_out and states == {"completed"}:
        overall_state, headline = "passed", "六个阶段全部完成，独立校验已通过"
    else:
        overall_state, headline = "not_run", "尚无阶段记录"
    report = {
        "schema_version": REPORT_SCHEMA,
        "overall": {
            "state": overall_state,
            "state_zh": _OVERALL_STATE_ZH[overall_state],
            "headline_zh": headline,
            "stages_completed": sum(1 for stage in stages_out if stage["state"] == "completed"),
            "stages_total": len(stages_out),
            "engineering_state": "not_ready",
        },
        "failure": _failure(job, first_failed) if first_failed is not None else None,
        "measured": {
            "subject_sha256": result.get("subject_sha256") or view.get("subject_sha256"),
            "handoff_sha256": view.get("handoff_sha256"),
            "expected_mass_window": measured_window,
            "mass_closure": measured_closure,
        },
        "stages": stages_out,
    }
    verified = (
        any(stage["id"] == "verify" and stage["state"] == "completed" for stage in stages_out)
        and quality.get("passed") is True
    )
    report["overall"]["engineering_state"] = "external_review" if verified else "not_ready"
    report["external_review"] = {
        "tracking": "external",
        "scopes": [
            {**confirmation, "stage": stage["id"]} for stage in stages_out for confirmation in stage["confirmations"]
        ],
        "note_zh": "平台不读取或代管评审批准记录；重跑时可直接沿用未变化事实的既有批准，仅变化项需要重新确认。",
    }
    return report
