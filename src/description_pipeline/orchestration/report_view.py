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
    "publication.git": "发布推送（Git）",
    "publication.receipt": "发布回执确认",
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
    text = str(value or "")
    return text[:length] + "…" if len(text) > length else text


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
            passed = sum(1 for row in rows if isinstance(row, dict) and row.get("state") == "passed")
            return {
                "scope_zh": "全部必需独立校验",
                "expected": f"{len(required)} 项必需校验",
                "actual": f"{passed}/{len(required)} 通过（已记录 {len(rows)} 项）",
            }
        return None
    if name.startswith("handoff."):
        return {"scope_zh": "交接包", "expected": "与申请一致", "actual": _short(details.get("handoff_sha256"))}
    if name == "verification.subject":
        return {"scope_zh": "交付主体", "expected": "绑定交付文件摘要", "actual": _short(details.get("subject_sha256"))}
    if name == "verification.report_binding":
        return {"scope_zh": "校验报告", "expected": "与交付主体绑定", "actual": _short(details.get("report_sha256"))}
    if name == "publication.git":
        return {
            "scope_zh": "评审分支推送",
            "expected": "推送成功",
            "actual": _short(details.get("commit") or details.get("result")),
        }
    if name == "publication.receipt":
        state = details.get("state")
        return {"scope_zh": "发布回执", "expected": "回执完整", "actual": str(state or "已记录")}
    if name in {"discovery.inputs", "discovery.definition", "discovery.binding"}:
        return {
            "scope_zh": "原生解析",
            "expected": "生成可绑定的结构定义",
            "actual": _short(details.get("discovery_sha256") or details.get("definition_sha256")),
        }
    if name in {
        "input.valid",
        "runtime.ready",
        "capture.integrity",
        "capture.input_stability",
        "generation.inputs",
        "generation.artifacts",
    }:
        actual = details.get("subject_sha256") or details.get("files") or details.get("output_files")
        if isinstance(actual, dict):
            actual = f"{len(actual)} 个文件"
        return {"scope_zh": "阶段输入/产物", "expected": "校验通过", "actual": _short(actual) or "已记录"}
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
                "expected": "差值 = 0",
                "actual": f"URDF {urdf} kg / CAD {whole} kg（差值 {delta} kg）",
            }
        return None
    if name == "consumer.urdf":
        bodies = details.get("bodies")
        version = details.get("version")
        if isinstance(bodies, int):
            return {
                "scope_zh": "消费端（MuJoCo）可加载",
                "expected": "可加载且刚体完整",
                "actual": f"{bodies} 个刚体，版本 {version}",
            }
        return None
    if name == "geometry.expected_extent":
        extent = details.get("extent_m")
        expected = details.get("expected_largest_m")
        if isinstance(extent, list) and isinstance(expected, list):
            return {"scope_zh": "外形尺寸", "expected": f"最大边 {expected}", "actual": f"{extent}"}
        return None
    return None


def _failure(job: dict, stage: dict) -> dict:
    """Safe, non-asserting failure meaning; raw evidence always preserved."""
    raw_error = str(stage.get("error") or "")
    raw_type, _, message = raw_error.partition(": ")
    diagnostic = stage.get("diagnostic")
    raw_detail = diagnostic if isinstance(diagnostic, dict) and diagnostic else None
    codes = None
    if raw_type == "CadError" and raw_detail and {"errors", "warnings"} <= set(raw_detail):
        codes = {"errors": raw_detail.get("errors"), "warnings": raw_detail.get("warnings")}
        title = "原生 CAD 打开失败"
        meaning = (
            "文件或其引用的文档无法定位；影响范围尚未证明，"
            "请由平台维护人员核实该文档及其引用对本次交付的必要性后再处理。"
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
        "raw_error": raw_error or None,
        "raw_detail": raw_detail,
    }
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
                }
                summary = _boundary_summary(item.get("id"), details)
                if summary is not None:
                    row["summary"] = summary
                boundary_rows.append(row)
        independent_rows: list[dict] = []
        if stage.get("id") == "verify":
            for item in quality_rows:
                details = item.get("details") if isinstance(item.get("details"), dict) else {}
                state = (
                    item.get("state")
                    if item.get("state") in {"passed", "failed"}
                    else (
                        "passed"
                        if item.get("passed") is True
                        else "failed"
                        if item.get("passed") is False
                        else "not_run"
                    )
                )
                row = {
                    "id": item.get("id"),
                    "label_zh": independent_label(item.get("id")),
                    "state": state,
                    "state_zh": _CHECK_STATE_ZH[state],
                    "executed": state != "not_run",
                    "raw_details": details,
                }
                summary = _independent_summary(item.get("id"), details)
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
            "in_scope": stage.get("in_scope"),
            "counts": {
                "boundary": _counts(boundary_rows),
                "independent": _counts(independent_rows) if independent_rows else None,
            },
            "boundary": boundary_rows,
            "files": [
                {
                    "label": record.get("label"),
                    "path": record.get("path"),
                    "availability": record.get("availability"),
                    "files": len(record.get("files") or {}),
                }
                for record in (stage.get("inputs", []) + stage.get("outputs", []))
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
                    "id": item.get("id"),
                    "label": item.get("label"),
                    "reference": item.get("reference"),
                    "review_stage": item.get("review_stage"),
                    "scope": item.get("scope"),
                    "automatic_exclusion": item.get("automatic_exclusion"),
                    "state": item.get("state"),
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
