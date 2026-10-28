"""Project recorded pipeline findings into the structural-team report."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from .geometry.native_scope import (
    NATIVE_GATE_WARNING_CODES,
    NATIVE_SCOPE_IDENTITY_WARNING_CODES,
    native_gate_warning_details_match,
    native_scope_warning_diagnostic_is_valid,
)
from .io import PipelineError, file_digest, read_data, write_json
from .sources.solidworks.saved_state import (
    SAVED_STATE_WARNING_CODE,
    warning_from_selection,
)

REPORT_SCHEMA = "solidworks-to-urdf.structural-report/v1"
MARKDOWN_PATH = "reports/structural-team.md"
JSON_PATH = "reports/structural-team.json"
DIAGNOSTIC_SCHEMA = "solidworks-to-urdf.failure-diagnostics/v1"

REPORT_EVIDENCE_FILES = (
    "input/handoff.json",
    "input/discovery/native-discovery.json",
    "input/source.json",
    "reports/quality.json",
    "model/robot.json",
    "evidence/native-runtime.json",
    "evidence/raw/native_capture_facts.json",
    "evidence/raw/dependency_closure.json",
)
SEALED_DIAGNOSTIC_FILES = (*REPORT_EVIDENCE_FILES, "evidence/manifest.json")

_FINDING_LISTS = {
    "findings",
    "warnings",
    "errors",
    "problems",
    "diagnostics",
    "checks",
}


def sealable_diagnostic_files(evidence_root: Path | None) -> list[str]:
    """Return the canonical native members present in one delivery tree."""

    return sorted(diagnostic_file_sources(evidence_root))


def diagnostic_file_sources(
    evidence_root: Path | None,
    *,
    failure_roots: tuple[str, ...] = (),
) -> dict[str, str]:
    """Map canonical diagnostic names to one exact existing source."""

    if evidence_root is None:
        return {}
    root = Path(evidence_root)
    sources: dict[str, str] = {}
    for name in SEALED_DIAGNOSTIC_FILES:
        if (root / name).is_file():
            sources[name] = name
            continue
        spellings = [name]
        if name.startswith("evidence/raw/"):
            spellings.insert(0, name[len("evidence/") :])
        matches = []
        for failure_root in failure_roots:
            for spelling in spellings:
                candidate = f"{failure_root}/partial/{spelling}"
                if (root / candidate).is_file():
                    matches.append(candidate)
                    break
        if len(matches) > 1:
            raise PipelineError(
                f"Conflicting partial evidence for {name!r}: {matches}"
            )
        if matches:
            sources[name] = matches[0]
    urdf_root = root / "urdf"
    if urdf_root.is_dir():
        for path in sorted(urdf_root.rglob("*.urdf")):
            relative = path.relative_to(root).as_posix()
            sources[relative] = relative
    return sources


def is_sealed_diagnostic_member(relative: str) -> bool:
    """Return whether one member is a sealed diagnostic input or URDF."""

    if relative in SEALED_DIAGNOSTIC_FILES:
        return True
    parts = relative.split("/")
    return (
        len(parts) >= 2
        and parts[0] == "urdf"
        and parts[-1].endswith(".urdf")
        and all(part not in {"", ".", ".."} for part in parts)
    )


def _finding_severity(value: Mapping) -> str:
    if value.get("blocking") is True or value.get("passed") is False:
        return "blocker"
    if value.get("blocking") is False:
        return "warning"
    severity = str(value.get("severity") or "warning")
    return "blocker" if severity in {"blocker", "error", "fatal"} else "warning"


def _findings(value, source: str, *, severity: str | None = None):
    """Yield every recorded diagnostic without flattening away its source."""

    if isinstance(value, list):
        for index, entry in enumerate(value):
            yield from _findings(
                entry,
                f"{source}/{index}",
                severity=severity,
            )
        return
    if not isinstance(value, Mapping):
        return
    code = value.get("code") or value.get("id")
    message = value.get("message") or value.get("error")
    if (
        isinstance(code, str)
        and value.get("passed") is not True
        and (
            isinstance(message, str)
            or "blocking" in value
            or value.get("passed") is False
        )
    ):
        if (
            code in NATIVE_SCOPE_IDENTITY_WARNING_CODES
            and not native_scope_warning_diagnostic_is_valid(value)
        ):
            yield {
                "severity": "blocker",
                "code": "report.native_identity_warning_invalid",
                "object": value.get("object") or value.get("component"),
                "message": (
                    f"Native scope warning {code} is forged or does not locate "
                    "its component, entity, body, indices and field"
                ),
                "source": source,
                "evidence": dict(value),
            }
            return
        detail = value.get("detail")
        finding = {
            "severity": _finding_severity(value),
            "code": code,
            "object": (
                value.get("object")
                or value.get("component")
                or value.get("instance")
                or value.get("document")
            ),
            "message": message or code,
            "source": source,
            "evidence": dict(value),
        }
        if isinstance(detail, Mapping):
            finding["detail"] = dict(detail)
        yield finding
        return
    for key, child in sorted(value.items(), key=lambda item: str(item[0])):
        if key in {
            "artifacts",
            "client_failure",
            "owned_files",
            "request",
            "structural_report",
        }:
            continue
        child_severity = (
            "blocker"
            if key in {"errors", "problems"}
            else "warning"
            if key in {"warnings", "diagnostics"}
            else severity
        )
        if key in _FINDING_LISTS or isinstance(child, Mapping):
            yield from _findings(
                child,
                f"{source}/{key}",
                severity=child_severity,
            )


def _native_projection_findings(
    quality: Mapping,
    diagnostics: list,
) -> list[dict]:
    gate = next(
        (
            row
            for row in quality.get("checks") or []
            if isinstance(row, Mapping)
            and row.get("id") == "geometry.native_tessellation"
        ),
        None,
    )
    passed = bool(
        gate
        and (
            gate.get("state") == "passed"
            or gate.get("passed") is True
        )
    )
    if not passed:
        return []
    details = gate.get("details")
    raw_warnings = (
        details.get("warnings") if isinstance(details, Mapping) else None
    )
    projected = [
        row
        for row in diagnostics
        if isinstance(row, Mapping)
        and row.get("code") in NATIVE_GATE_WARNING_CODES
    ]
    raw = [
        row
        for row in raw_warnings or []
        if isinstance(row, Mapping)
        and row.get("code") in NATIVE_GATE_WARNING_CODES
    ]
    if (
        not any(
            row.get("code") in NATIVE_SCOPE_IDENTITY_WARNING_CODES
            for row in raw
        )
        or not isinstance(raw_warnings, list)
        or not native_gate_warning_details_match(projected, raw_warnings)
    ):
        return [
            {
                "severity": "blocker",
                "code": "report.native_identity_warning_invalid",
                "object": None,
                "message": "Native gate warning projection is incomplete or forged",
                "source": "reports/quality.json/diagnostics",
                "evidence": {"native_gate_warnings": raw},
            }
        ]
    return []


def _saved_state_findings(
    receipt: Mapping,
    quality: Mapping,
) -> list[dict]:
    selection = (receipt.get("selection") or {}).get("manifest")
    expected = None
    if isinstance(selection, Mapping):
        try:
            expected = warning_from_selection(selection)
        except ValueError as error:
            return [
                {
                    "severity": "blocker",
                    "code": "report.saved_state_invalid",
                    "object": receipt.get("main_assembly"),
                    "message": "Saved-state source evidence is incoherent",
                    "source": "run/selection/manifest/closure/open_status",
                    "evidence": {"error": str(error)},
                }
            ]
    diagnostics = quality.get("diagnostics")
    saved = (
        [
            item
            for item in diagnostics
            if isinstance(item, Mapping)
            and item.get("code") == SAVED_STATE_WARNING_CODE
        ]
        if isinstance(diagnostics, list)
        else []
    )
    if expected is None and saved:
        return [
            {
                "severity": "blocker",
                "code": "report.saved_state_warning_invalid",
                "object": receipt.get("main_assembly"),
                "message": "A clean source open carries a forged saved-state warning",
                "source": "reports/quality.json/diagnostics",
                "evidence": {"diagnostics": saved},
            }
        ]
    if expected is None:
        return []
    if not isinstance(diagnostics, list):
        return [{**expected, "source": "run/selection/manifest/closure/open_status"}]
    if len(saved) != 1 or not (
        saved[0].get("blocking") is False
        and saved[0].get("message") == expected["message"]
        and saved[0].get("objects") == expected["objects"]
        and saved[0].get("detail") == expected["detail"]
    ):
        return [
            {
                "severity": "blocker",
                "code": "report.saved_state_warning_invalid",
                "object": receipt.get("main_assembly"),
                "message": "Saved-state warning projection is incomplete or forged",
                "source": "reports/quality.json/diagnostics",
                "evidence": {"diagnostics": saved},
            }
        ]
    return [
        {
            "severity": "warning",
            **expected,
            "source": "reports/quality.json/diagnostics",
        }
    ]


def build_structural_report(
    receipt: dict,
    *,
    evidence_root: Path | None = None,
) -> dict:
    """Build one deterministic report from canonical raw and derived records."""

    sources = {"run": receipt}
    if evidence_root is not None:
        root = Path(evidence_root)
        for name in REPORT_EVIDENCE_FILES:
            path = root / name
            if not path.is_file():
                continue
            try:
                sources[name] = read_data(path)
            except (OSError, ValueError) as error:
                sources[name] = {
                    "errors": [
                        {
                            "code": "report.evidence_unreadable",
                            "message": str(error),
                        }
                    ]
                }
    entries: list[dict] = []
    if receipt.get("error"):
        detail = receipt.get("detail")
        failure_object = receipt.get("main_assembly")
        if isinstance(detail, Mapping):
            failure_object = (
                detail.get("component")
                or detail.get("document")
                or failure_object
            )
        entries.append(
            {
                "severity": "blocker",
                "code": receipt.get("error_code")
                or "pipeline.execution_failed",
                "object": failure_object,
                "message": str(receipt["error"]),
                "source": "run/error",
                "evidence": {
                    "detail": detail,
                    "stage": receipt.get("stage"),
                },
            }
        )
        if isinstance(detail, Mapping):
            entries.extend(_findings(detail, "run/detail"))

    for name in (
        "input/handoff.json",
        "input/discovery/native-discovery.json",
        "evidence/native-runtime.json",
    ):
        if name in sources:
            entries.extend(_findings(sources[name], name))

    raw_name = "evidence/raw/native_capture_facts.json"
    raw = sources.get(raw_name)
    if isinstance(raw, Mapping):
        entries.extend(
            _findings(raw.get("notes") or {}, f"{raw_name}/notes")
        )

    model = sources.get("model/robot.json")
    if isinstance(model, Mapping):
        warnings = (model.get("provenance") or {}).get("value_warnings")
        entries.extend(
            _findings(
                warnings or [],
                "model/robot.json/provenance/value_warnings",
            )
        )

    quality = sources.get("reports/quality.json")
    if isinstance(quality, Mapping):
        diagnostics = quality.get("diagnostics")
        if not isinstance(diagnostics, list):
            entries.append(
                {
                    "severity": "blocker",
                    "code": "report.native_identity_warning_invalid",
                    "object": receipt.get("main_assembly"),
                    "message": "Quality diagnostics are malformed",
                    "source": "reports/quality.json/diagnostics",
                    "evidence": {
                        "type": type(diagnostics).__name__,
                    },
                }
            )
        else:
            entries.extend(_findings(diagnostics, "reports/quality.json/diagnostics"))
            entries.extend(
                _native_projection_findings(quality, diagnostics)
            )
        entries.extend(_saved_state_findings(receipt, quality))
    else:
        entries.extend(_saved_state_findings(receipt, {}))

    for name in (
        "input/handoff.json",
        "input/discovery/native-discovery.json",
        "reports/quality.json",
        "evidence/native-runtime.json",
        "evidence/raw/native_capture_facts.json",
        "evidence/raw/dependency_closure.json",
    ):
        value = sources.get(name)
        if isinstance(value, Mapping) and value.get("errors"):
            entries.extend(_findings({"errors": value["errors"]}, name))

    unique = {}
    for entry in entries:
        key = json.dumps(
            {name: value for name, value in entry.items() if name != "source"},
            ensure_ascii=False,
            sort_keys=True,
        )
        unique.setdefault(key, entry)
    findings = list(unique.values())
    urdf_files = []
    if evidence_root is not None:
        urdf_root = Path(evidence_root) / "urdf"
        if urdf_root.is_dir():
            urdf_files = [
                path.relative_to(evidence_root).as_posix()
                for path in sorted(urdf_root.rglob("*.urdf"))
            ]
    return {
        "schema_version": REPORT_SCHEMA,
        "run_id": receipt.get("run_id"),
        "state": receipt.get("status") or receipt.get("state"),
        "main_assembly": receipt.get("main_assembly"),
        "upload_sha256": (receipt.get("request") or {}).get(
            "upload_sha256"
        ),
        "handoff_sha256": receipt.get("handoff_sha256"),
        "urdf_files": urdf_files,
        "summary": {
            "warnings": sum(
                entry["severity"] == "warning" for entry in findings
            ),
            "blockers": sum(
                entry["severity"] == "blocker" for entry in findings
            ),
        },
        "findings": findings,
    }


def _fenced(value) -> str:
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    fence = "`" * max(
        3,
        1
        + max(
            (len(match) for match in re.findall(r"`+", text)),
            default=0,
        ),
    )
    return f"{fence}json\n{text}\n{fence}"


def render_structural_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Structural CAD-to-URDF issue report",
        "",
        f"Run: {report.get('run_id') or 'not specified'}",
        "",
        f"Run state: {report.get('state') or 'not recorded'}; "
        f"{summary['warnings']} warnings and {summary['blockers']} blocking issues.",
        "",
        "This report retains recorded warnings without changing verification decisions or "
        "inventing missing CAD engineering parameters.",
        "",
        "## Inputs and outputs",
        "",
        f"- Main assembly: {report.get('main_assembly') or 'not recorded'}",
        f"- Handoff SHA-256: {report.get('handoff_sha256') or 'not recorded'}",
        f"- URDF files: {len(report['urdf_files'])}",
        "",
    ]
    if not report["urdf_files"]:
        lines.extend(
            [
                "This stage has not produced a URDF.",
                "",
            ]
        )
    if not report["findings"]:
        lines.extend(
            ["The stage records contain no issues requiring structural-team action.", ""]
        )
    for index, item in enumerate(report["findings"], 1):
        lines.extend(
            [
                f"## {index}. {'Warning' if item['severity'] == 'warning' else 'Blocking'}",
                "",
                _fenced(
                    {
                        name: item.get(name)
                        for name in (
                            "code",
                            "object",
                            "message",
                            "source",
                            "evidence",
                        )
                    }
                ),
                "",
                "Action: inspect the named CAD object and its raw evidence. "
                "If the reader is defective, fix the pipeline and rerun against the same input bytes.",
                "",
            ]
        )
    return "\n".join(lines)


def write_structural_report(
    root: Path,
    receipt: dict,
    *,
    evidence_root: Path | None = None,
) -> dict:
    root = Path(root)
    report = build_structural_report(receipt, evidence_root=evidence_root)
    write_json(root / JSON_PATH, report)
    markdown = root / MARKDOWN_PATH
    markdown.write_text(
        render_structural_markdown(report),
        encoding="utf-8",
    )
    return {
        "markdown": {
            "path": MARKDOWN_PATH,
            "sha256": file_digest(markdown),
        },
        "json": {
            "path": JSON_PATH,
            "sha256": file_digest(root / JSON_PATH),
        },
    }
