"""Generate the structural team's report from recorded pipeline evidence.

This is a projection of existing findings. It neither changes verification
decisions nor supplies engineering values to the model generator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .io import file_digest, read_data, write_json

REPORT_SCHEMA = "solidworks-to-urdf.structural-report/v1"
MARKDOWN_PATH = "reports/structural-team.md"
JSON_PATH = "reports/structural-team.json"

#: Diagnostic-bundle schema the native worker seals and the Linux side renders from.
DIAGNOSTIC_SCHEMA = "solidworks-to-urdf.failure-diagnostics/v1"

#: Evidence files the report builder projects into findings.
REPORT_EVIDENCE_FILES = (
    "reports/input.json",
    "reports/quality.json",
    "input/discovery/findings.json",
    "input/discovery/native-discovery.json",
    "evidence/raw/dependency_closure.json",
    "evidence/raw/material_provenance.json",
    "evidence/raw/effective_instances.json",
    "evidence/raw/mass_closure.json",
    "reports/mass-closure.json",
)

#: Files sealed for one failed job: the projected evidence plus the snapshot
#: manifest the builder consults for its fail-closed material checks.
SEALED_DIAGNOSTIC_FILES = (*REPORT_EVIDENCE_FILES, "evidence/manifest.json")


def sealable_diagnostic_files(evidence_root: Path | None) -> list[str]:
    """Existing diagnostic files of one delivery tree, delivery-relative paths.

    The native side seals exactly these files and the Linux side renders from the
    same bytes, so no report ever depends on where it was rendered.
    """

    return sorted(diagnostic_file_sources(evidence_root))


#: A partial capture keeps its copies under this prefix, preserving either the
#: full delivery-relative name or the name without the leading ``evidence/``.
PARTIAL_PREFIX = "partial"


def diagnostic_file_sources(evidence_root: Path | None) -> dict[str, str]:
    """Canonical sealed-diagnostic names mapped to existing sources, delivery-relative.

    The bundle layout, allowlist and hashes always use the canonical names, so a
    file recovered from ``partial/`` renders exactly like a complete one; which
    sources came from partial storage stays visible in the manifest instead of
    being presented as a complete capture.
    """

    if evidence_root is None:
        return {}
    root = Path(evidence_root)
    sources: dict[str, str] = {}
    for name in SEALED_DIAGNOSTIC_FILES:
        short = name[len("evidence/") :] if name.startswith("evidence/") else None
        candidates = [name]
        candidates.extend(f"{PARTIAL_PREFIX}/{candidate}" for candidate in (name, short) if candidate)
        for candidate in candidates:
            if (root / candidate).is_file():
                sources[name] = candidate
                break
    urdf = root / "urdf"
    if urdf.is_dir():
        for path in sorted(urdf.rglob("*.urdf")):
            relative = path.relative_to(root).as_posix()
            sources[relative] = relative
    return sources


def is_sealed_diagnostic_member(relative: str) -> bool:
    """Whether one member name is a known sealed diagnostic (projected evidence or URDF)."""

    if relative in SEALED_DIAGNOSTIC_FILES:
        return True
    parts = relative.split("/")
    return (
        len(parts) >= 2
        and parts[0] == "urdf"
        and parts[-1].endswith(".urdf")
        and all(part not in {"", ".", ".."} for part in parts)
    )
_FINDING_LISTS = {"findings", "warnings", "errors", "problems", "diagnostics", "checks"}

#: The single canonical material-provenance record (producer: 71f489a): written for every
#: capture under ``evidence/raw/material_provenance.json``, bound by the snapshot manifest.
_MATERIAL_SCHEMA = "solidworks-to-urdf.material-provenance/v1"
_MATERIAL_TOP_KEYS = ("schema_version", "assembly", "configuration", "material_source", "measured_source")
_MATERIAL_ENTRY_KEYS = (
    "code",
    "message",
    "blocking",
    "component",
    "document",
    "configuration",
    "missing_body_indices",
    "material_assignment",
    "measured",
)
_MATERIAL_MEASURED_KEYS = (
    "source",
    "scope",
    "mass_kg",
    "center_of_mass_m",
    "inertia_kg_m2",
    "volume_m3",
    "density_kg_m3",
)


def _canonical_material_warnings(value) -> list | None:
    """The canonical warning list of the material-provenance record, or ``None``."""

    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != _MATERIAL_SCHEMA:
        return None
    if any(key not in value for key in _MATERIAL_TOP_KEYS):
        return None
    if value.get("measured_source") != "cad_api":
        return None
    warnings = value.get("warnings")
    if not isinstance(warnings, list):
        return None
    for item in warnings:
        if not isinstance(item, dict) or any(key not in item for key in _MATERIAL_ENTRY_KEYS):
            return None
        if item.get("code") != "cad_material_provenance_missing":
            # Unknown reason values fail closed instead of being rendered as warnings.
            return None
        if not isinstance(item.get("message"), str):
            return None
        if not isinstance(item.get("blocking"), bool):
            return None
        if not isinstance(item.get("material_assignment"), dict) or not isinstance(item.get("measured"), dict):
            return None
        if any(key not in item["measured"] for key in _MATERIAL_MEASURED_KEYS):
            return None
        if item["measured"].get("source") != "cad_api":
            return None
    return warnings


#: The single canonical effective-instance record (producer: the native freeze): written for
#: every capture under ``evidence/raw/effective_instances.json``, bound by the manifest.
_EFFECTIVE_SCHEMA = "solidworks-to-urdf.effective-instances/v1"
_EFFECTIVE_TOP_KEYS = ("schema_version", "assembly", "configuration", "readings", "warnings")
_EFFECTIVE_CODES = (
    "cad_instance_override_effective",
    "cad_surface_only_effective_instance",
    "cad_instance_measurement_differs_from_part",
)
_EFFECTIVE_QUALIFICATION = (
    "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
)
_INSTANCE_DIFFERENCE_QUALIFICATION = (
    "cad_assembly_instance_measurement; cause_of_part_difference_unproven; "
    "not geometry-derived, not independently physically qualified"
)
_PART_MEASUREMENT_KEYS = (
    "mass_kg",
    "center_of_mass_m",
    "inertia_kg_m2",
    "scope",
    "used_api",
    "product_convention",
)


def _canonical_effective_warnings(value) -> list | None:
    """The canonical warning list of the effective-instance record, or ``None``."""

    if not isinstance(value, dict) or value.get("schema_version") != _EFFECTIVE_SCHEMA:
        return None
    if any(key not in value for key in _EFFECTIVE_TOP_KEYS):
        return None
    if not isinstance(value.get("readings"), dict):
        return None
    warnings = value.get("warnings")
    if not isinstance(warnings, list):
        return None
    for item in warnings:
        if not isinstance(item, dict):
            return None
        code = item.get("code")
        if code not in _EFFECTIVE_CODES:
            # Unknown codes fail closed instead of being rendered as warnings.
            return None
        if item.get("blocking") is not False:
            return None
        if not isinstance(item.get("component"), str) or not item["component"].strip():
            return None
        if not isinstance(item.get("measurement"), dict):
            return None
        overrides = item.get("overrides")
        if not isinstance(overrides, dict) or any(not isinstance(flag, bool) for flag in overrides.values()):
            return None
        if code == "cad_instance_measurement_differs_from_part":
            # Flagless leaf instance whose assembly-context measurement differs from
            # its part measurement: the cause is unproven, so the exact literal is
            # required and every override flag must be false.
            if item.get("qualification") != _INSTANCE_DIFFERENCE_QUALIFICATION:
                return None
            if item.get("role") != "leaf":
                return None
            if not overrides or any(flag is not False for flag in overrides.values()):
                return None
            part = item.get("part_measurement")
            if not isinstance(part, dict) or any(key not in part for key in _PART_MEASUREMENT_KEYS):
                return None
            continue
        if item.get("qualification") != _EFFECTIVE_QUALIFICATION:
            return None
        required = (
            ("document", "part_document_read", "solid_bodies", "sheet_bodies")
            if code == "cad_surface_only_effective_instance"
            else ("role", "document_type", "part_document", "assembly_document")
        )
        if any(key not in item for key in required):
            return None
    return warnings


def _findings(value, source, *, severity=None):
    if isinstance(value, list):
        for index, entry in enumerate(value):
            yield from _findings(entry, f"{source}/{index}", severity=severity)
        return
    if not isinstance(value, dict):
        return
    code = value.get("code") or value.get("id")
    message = value.get("message") or value.get("error")
    is_finding = isinstance(code, str) and (
        isinstance(message, str) or "blocking" in value or value.get("passed") is False
    )
    if is_finding and value.get("passed") is not True:
        level = "blocker" if value.get("blocking") is True or value.get("passed") is False else severity or "warning"
        if value.get("blocking") is False:
            level = "warning"
        yield {
            "severity": level,
            "code": code,
            "object": value.get("object") or value.get("instance") or value.get("document"),
            "message": message or code,
            "source": source,
            "evidence": value,
        }
        return
    # Deterministic traversal: a persisted (sorted) receipt must render exactly
    # like the live one, so neither finding order nor the first-seen duplicate
    # anchor depends on how the job record was serialized.
    for key, child in sorted(value.items(), key=lambda item: str(item[0])):
        if key in {"structural_report", "artifacts", "owned_files", "request", "client_failure"}:
            continue
        child_severity = (
            "blocker"
            if key in {"errors", "problems"}
            else "warning"
            if key in {"warnings", "diagnostics"}
            else severity
        )
        if key in _FINDING_LISTS or isinstance(child, dict):
            yield from _findings(child, f"{source}/{key}", severity=child_severity)


def build_structural_report(receipt: dict, *, evidence_root: Path | None = None) -> dict:
    """Retain exact findings and source locations, including pre-capture refusals."""
    material_source = "evidence/raw/material_provenance.json"
    effective_source = "evidence/raw/effective_instances.json"
    sources = {"run": receipt}
    if evidence_root is not None:
        for name in REPORT_EVIDENCE_FILES:
            path = Path(evidence_root) / name
            if path.is_file():
                try:
                    sources[name] = read_data(path)
                except (OSError, ValueError) as error:
                    sources[name] = {"errors": [{"code": "report.evidence_unreadable", "message": str(error)}]}
    entries = []
    if receipt.get("error"):
        detail = receipt.get("detail")
        failure_object = receipt.get("main_assembly")
        if isinstance(detail, dict):
            # The native failure context names the exact failing leaf. Use it
            # verbatim and never invent a component the record does not carry.
            component = detail.get("component")
            document = detail.get("document")
            if isinstance(component, str) and component.strip():
                failure_object = component
            elif isinstance(document, str) and document.strip():
                failure_object = document
        entries.append(
            {
                "severity": "blocker",
                "code": receipt.get("error_code") or "pipeline.execution_failed",
                "object": failure_object,
                "message": str(receipt["error"]),
                "source": "run/error",
                "evidence": {"detail": detail, "stage": receipt.get("stage")},
            }
        )
    for source, value in sources.items():
        if source in {material_source, effective_source}:
            # Projected explicitly below: the raw warning entries carry their own
            # code/message and must not be double-reported by the generic walk.
            continue
        entries.extend(_findings(value, source))
    closure = ((receipt.get("selection") or {}).get("manifest") or {}).get("closure") or {}
    opened = closure.get("open_status") or {}
    if opened.get("warnings"):
        entries.append(
            {
                "severity": "warning",
                "code": "native.open_warnings",
                "object": receipt.get("main_assembly"),
                "message": "SolidWorks 打开装配时返回警告，原始位掩码保留在证据中。",
                "source": "run/selection/manifest/closure/open_status",
                "evidence": opened,
            }
        )
    dependency_closure = sources.get("evidence/raw/dependency_closure.json")
    if isinstance(dependency_closure, dict):
        # Every recorded automation issue is reported, even when it can remain a
        # warning: evidence-covered unused stored references were never read or
        # copied, so they are projected verbatim as warnings with their exact
        # document/location evidence instead of disappearing into the raw record.
        for item in dependency_closure.get("stale_stored_references") or []:
            if not isinstance(item, dict):
                continue
            entries.append(
                {
                    "severity": "warning",
                    "code": "native.stale_stored_reference",
                    "object": item.get("document") or item.get("resolved_path"),
                    "message": (
                        "SolidWorks 的保存引用未被所选闭包使用（未解析为活动边，位置可能已失效且在交付包外）；"
                        "流水线未读取、未复制该外部文件，仅按原文保留记录，不阻断生成与独立校验。"
                    ),
                    "source": "evidence/raw/dependency_closure.json/stale_stored_references",
                    "evidence": {
                        "document": item.get("document"),
                        "location": item.get("location"),
                        "resolved_path": item.get("resolved_path"),
                        "exists": item.get("exists"),
                    },
                }
            )
    material_required = False
    effective_required = False
    if evidence_root is not None:
        snapshot_manifest = Path(evidence_root) / "evidence/manifest.json"
        if snapshot_manifest.is_file():
            try:
                snapshot = read_data(snapshot_manifest)
            except (OSError, ValueError):
                snapshot = None
            if isinstance(snapshot, dict) and isinstance(snapshot.get("files"), dict):
                material_required = "raw/material_provenance.json" in snapshot["files"]
                effective_required = "raw/effective_instances.json" in snapshot["files"]
    effective_instances = sources.get(effective_source)
    if isinstance(effective_instances, dict) and effective_instances.get("errors"):
        entries.extend(_findings(effective_instances, effective_source))
        entries.append(
            {
                "severity": "blocker",
                "code": "report.effective_instances_invalid",
                "object": receipt.get("main_assembly"),
                "message": (
                    "有效实例证据不可读：证据无效，必须由独立检查判为失败。"
                    "本报告不采用默认值、不做替代解释、不据此输出有效实例警告。"
                ),
                "source": effective_source,
                "evidence": {"unreadable": True},
            }
        )
    elif effective_instances is None:
        if effective_required:
            # The manifest binds the record for every current capture; a deleted or
            # absent record is required evidence and fails closed.
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.effective_instances_missing",
                    "object": receipt.get("main_assembly"),
                    "message": (
                        "快照清单要求有效实例记录（evidence/raw/effective_instances.json），但该证据缺失："
                        "证据无效，必须由独立检查判为失败。本报告不采用默认值或替代解释。"
                    ),
                    "source": effective_source,
                    "evidence": {"required_by_manifest": True},
                }
            )
    else:
        # One canonical schema only; anything else is independently rejected instead of
        # being reinterpreted, defaulted or accepted through an alternate interface.
        effective_warnings = _canonical_effective_warnings(effective_instances)
        if effective_warnings is None:
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.effective_instances_invalid",
                    "object": receipt.get("main_assembly"),
                    "message": (
                        "有效实例证据不符合当前 schema：证据无效，必须由独立检查判为失败。"
                        "本报告不采用默认值、不做替代解释、不据此输出有效实例警告；"
                        "请用当前生产者的规范记录替换该证据。"
                    ),
                    "source": effective_source,
                    "evidence": {
                        "type": type(effective_instances).__name__,
                        "schema_version": (
                            effective_instances.get("schema_version")
                            if isinstance(effective_instances, dict)
                            else None
                        ),
                        "keys": sorted(effective_instances) if isinstance(effective_instances, dict) else None,
                    },
                }
            )
        else:
            # CAD-authored effective values are warnable: the report keeps the native
            # flags and readings verbatim, states the instance scope plainly, and never
            # claims geometric derivation or independent physical qualification.
            override_message = (
                "CAD 在装配实例上按有效覆盖值记录该组件（assembly_component_group 范围，使用装配文档坐标轴）："
                "父级实例的覆盖值在单个刚体范围内整体替换其子树，未向运动链分摊；"
                "该值为 CAD 编写的有效值，未经几何推导与独立物理校核，本报告不据此声称物理准确性。"
            )
            surface_message = (
                "该组件仅有曲面（无实体），零件文档无法回答质量属性，CAD 改按装配实例记录有效值"
                "（assembly_component_group 范围，使用装配文档坐标轴）：质量、质心与惯量来自 CAD 编写的有效值，"
                "未经几何推导与独立物理校核，本报告不据此声称物理准确性，也不做分摊或替代。"
            )
            difference_message = (
                "该叶实例在装配组件组范围内的测量与其零件文档测量不同"
                "（basis：cad_assembly_instance_measurement）：差异原因尚未证实；"
                "本报告保留实例测量与零件测量双方的原始数值供工程确认，不做缩放或替代；"
                "该值为 CAD 实例上下文记录，未经几何推导与独立物理校核，不据此声称物理准确性。"
            )
            for item in effective_warnings:
                if item["code"] == "cad_surface_only_effective_instance":
                    message = surface_message
                elif item["code"] == "cad_instance_measurement_differs_from_part":
                    message = difference_message
                else:
                    message = override_message
                entries.append(
                    {
                        "severity": "warning",
                        "code": item["code"],
                        "object": item.get("component") or item.get("document") or item.get("part_document"),
                        "message": message,
                        "source": "evidence/raw/effective_instances.json/warnings",
                        "evidence": item,
                    }
                )
    material_provenance = sources.get(material_source)
    if isinstance(material_provenance, dict) and material_provenance.get("errors"):
        entries.extend(_findings(material_provenance, material_source))
        entries.append(
            {
                "severity": "blocker",
                "code": "report.material_provenance_invalid",
                "object": receipt.get("main_assembly"),
                "message": (
                    "材料来源证据不可读：证据无效，必须由独立检查判为失败。"
                    "本报告不采用默认值、不做替代解释、不据此输出材料警告。"
                ),
                "source": material_source,
                "evidence": {"unreadable": True},
            }
        )
    elif material_provenance is None:
        if material_required:
            # The snapshot manifest binds the record for every current capture; a deleted or
            # absent record is required evidence and fails closed.
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.material_provenance_missing",
                    "object": receipt.get("main_assembly"),
                    "message": (
                        "快照清单要求材料来源记录（evidence/raw/material_provenance.json），但该证据缺失："
                        "证据无效，必须由独立检查判为失败。本报告不采用默认值或替代解释。"
                    ),
                    "source": material_source,
                    "evidence": {"required_by_manifest": True},
                }
            )
    else:
        # One canonical schema only; anything else is independently rejected instead of
        # being reinterpreted, defaulted or accepted through an alternate interface.
        material_warnings = _canonical_material_warnings(material_provenance)
        if material_warnings is None:
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.material_provenance_invalid",
                    "object": receipt.get("main_assembly"),
                    "message": (
                        "材料来源证据不符合当前 schema：证据无效，必须由独立检查判为失败。"
                        "本报告不采用默认值、不做替代解释、不据此输出材料警告；"
                        "请用当前生产者的规范记录替换该证据。"
                    ),
                    "source": material_source,
                    "evidence": {
                        "type": type(material_provenance).__name__,
                        "schema_version": (
                            material_provenance.get("schema_version") if isinstance(material_provenance, dict) else None
                        ),
                        "keys": sorted(material_provenance) if isinstance(material_provenance, dict) else None,
                    },
                }
            )
        else:
            # Missing explicit material is a warnable defect: the report renders the exact
            # reported assignment, body indices and native measured values, and never
            # claims the material or the physical/dynamic behaviour is verified.
            for item in material_warnings:
                blocking = item["blocking"] is True
                entries.append(
                    {
                        "severity": "blocker" if blocking else "warning",
                        "code": "native.material_provenance_missing",
                        "object": item.get("component") or item.get("document"),
                        "message": "SolidWorks 实体缺少显式物理材料赋值（默认密度不作为材料证据）："
                        + (
                            "该记录标记为阻断，必须由独立检查判为失败。"
                            if blocking
                            else "按可警告缺陷记录——流水线保留 CAD 实测质量、质心与惯量并继续生成与独立校验；"
                            "本报告不据此声称材料已核验，材料真实性与物理/动力学合格仍属外部工程确认。"
                        ),
                        "source": "evidence/raw/material_provenance.json/warnings",
                        "evidence": {
                            "code": item.get("code"),
                            "component": item.get("component"),
                            "document": item.get("document"),
                            "configuration": item.get("configuration"),
                            "missing_body_indices": item.get("missing_body_indices"),
                            "material_assignment": item.get("material_assignment"),
                            "measured": item.get("measured"),
                        },
                    }
                )
    # The same warning may appear in discovery, input validation and a stage
    # event. Preserve its first evidence location without repeating it.
    unique = {}
    for entry in entries:
        key = json.dumps({k: v for k, v in entry.items() if k != "source"}, ensure_ascii=False, sort_keys=True)
        unique.setdefault(key, entry)
    findings = list(unique.values())
    urdf_files = []
    if evidence_root is not None:
        urdf_root = Path(evidence_root) / "urdf"
        if urdf_root.is_dir():
            urdf_files = [p.relative_to(evidence_root).as_posix() for p in sorted(urdf_root.rglob("*.urdf"))]
    return {
        "schema_version": REPORT_SCHEMA,
        "run_id": receipt.get("run_id"),
        "state": receipt.get("status") or receipt.get("state"),
        "main_assembly": receipt.get("main_assembly"),
        "upload_sha256": (receipt.get("request") or {}).get("upload_sha256"),
        "handoff_sha256": receipt.get("handoff_sha256"),
        "urdf_files": urdf_files,
        "summary": {
            "warnings": sum(item["severity"] == "warning" for item in findings),
            "blockers": sum(item["severity"] == "blocker" for item in findings),
        },
        "findings": findings,
    }


def _fenced(value) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    fence = "`" * max(3, 1 + max((len(match) for match in re.findall(r"`+", text)), default=0))
    return f"{fence}json\n{text}\n{fence}"


def render_structural_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# 结构组 CAD → URDF 问题报告",
        "",
        f"运行：{report.get('run_id') or '未指定'}",
        "",
        f"运行状态：{report.get('state') or '未记录'}；"
        f"警告 {summary['warnings']} 项，阻断问题 {summary['blockers']} 项。",
        "",
        "本报告由 pipeline 自动生成，汇总本次运行已记录的事实。警告保留在报告中；"
        "是否通过独立校验，以该次运行的质量报告为准。报告不补写 CAD 中缺失的工程参数。",
        "",
        "## 输入与输出",
        "",
        _fenced({key: report.get(key) for key in ("main_assembly", "upload_sha256", "handoff_sha256", "urdf_files")}),
        "",
    ]
    if not report["urdf_files"]:
        lines.extend(["本阶段尚未产生 URDF；原生采集成功时，由 Linux 后续阶段生成。", ""])
    if not report["findings"]:
        lines.extend(["本次阶段记录中未发现需结构组处理的问题。", ""])
    for index, item in enumerate(report["findings"], 1):
        label = "警告" if item["severity"] == "warning" else "阻断"
        # Evidence strings remain inside JSON fences, so CAD names cannot alter
        # report structure or turn recorded text into active markup.
        lines.extend(
            [
                f"## {index}. {label}",
                "",
                _fenced({key: item.get(key) for key in ("code", "object", "message", "source", "evidence")}),
                "",
                "处理依据：请根据上述 CAD 对象和原始证据检查装配引用、导出声明或相关参数；"
                "如属读取器问题，应修复 pipeline 后对相同 CAD 字节重跑。",
                "",
            ]
        )
    return "\n".join(lines)


def write_structural_report(root: Path, receipt: dict, *, evidence_root: Path | None = None) -> dict:
    root = Path(root)
    report = build_structural_report(receipt, evidence_root=evidence_root)
    write_json(root / JSON_PATH, report)
    markdown = root / MARKDOWN_PATH
    markdown.write_text(render_structural_markdown(report), encoding="utf-8")
    return {
        "markdown": {"path": MARKDOWN_PATH, "sha256": file_digest(markdown)},
        "json": {"path": JSON_PATH, "sha256": file_digest(root / JSON_PATH)},
    }
