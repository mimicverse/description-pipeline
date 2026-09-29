"""质检结论的数据结构：级别、排序与汇总。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ERROR = "error"
WARNING = "warning"
INFO = "info"

SEVERITIES = (ERROR, WARNING, INFO)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    subject: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.subject:
            payload["subject"] = self.subject
        if self.context:
            payload["context"] = self.context
        return payload


def sort_findings(findings: list[Finding]) -> list[Finding]:
    order = {severity: index for index, severity in enumerate(SEVERITIES)}
    return sorted(findings, key=lambda item: (order[item.severity], item.code, item.subject))


def summarize(findings: list[Finding]) -> dict[str, int]:
    summary = dict.fromkeys(SEVERITIES, 0)
    for finding in findings:
        summary[finding.severity] += 1
    return summary
