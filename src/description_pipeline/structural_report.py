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
_FINDING_LISTS = {"findings", "warnings", "errors", "problems", "diagnostics", "checks"}


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
    for key, child in value.items():
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
    sources = {"run": receipt}
    if evidence_root is not None:
        for name in (
            "reports/input.json",
            "reports/quality.json",
            "input/discovery/findings.json",
            "input/discovery/native-discovery.json",
            "evidence/raw/dependency_closure.json",
        ):
            path = Path(evidence_root) / name
            if path.is_file():
                try:
                    sources[name] = read_data(path)
                except (OSError, ValueError) as error:
                    sources[name] = {"errors": [{"code": "report.evidence_unreadable", "message": str(error)}]}
    entries = []
    if receipt.get("error"):
        entries.append(
            {
                "severity": "blocker",
                "code": receipt.get("error_code") or "pipeline.execution_failed",
                "object": receipt.get("main_assembly"),
                "message": str(receipt["error"]),
                "source": "run/error",
                "evidence": {"detail": receipt.get("detail"), "stage": receipt.get("stage")},
            }
        )
    for source, value in sources.items():
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
