"""例外台账 `config/urdf_quality.json`：只允许豁免 warning，且必须留下理由与日期。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .findings import ERROR, Finding
from description_pipeline.io import PipelineError, read_data

CODE = re.compile(r"^URDF\d{3}$")
REQUIRED = ("code", "reason", "owner", "date")


@dataclass
class Waiver:
    code: str
    subject: str
    reason: str
    owner: str
    date: str
    review_after: str | None = None
    raw: dict = field(default_factory=dict)

    def matches(self, finding: Finding) -> bool:
        if finding.code != self.code:
            return False
        return not self.subject or self.subject == finding.subject


@dataclass
class Ledger:
    waivers: list[Waiver] = field(default_factory=list)
    massless_links: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)  # 台账自身的结构问题

    def match(self, finding: Finding) -> Waiver | None:
        for waiver in self.waivers:
            if waiver.matches(finding):
                return waiver
        return None


def load(path: Path) -> Ledger:
    """读取台账；文件不存在时返回空台账（不是错误，main 模板就没有例外）。"""

    path = Path(path)
    ledger = Ledger()
    if not path.is_file():
        return ledger
    try:
        payload = read_data(path)
    except PipelineError as error:
        ledger.errors.append(f"{path.name} 不是合法 JSON：{error}")
        return ledger
    if not isinstance(payload, dict):
        ledger.errors.append(f"{path.name} 顶层必须是对象")
        return ledger
    entries = payload.get("waivers", [])
    if not isinstance(entries, list):
        ledger.errors.append(f"{path.name} 的 waivers 必须是数组")
        entries = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            ledger.errors.append(f"waivers[{index}] 不是对象")
            continue
        missing = [key for key in REQUIRED if not str(entry.get(key, "")).strip()]
        if missing:
            ledger.errors.append(f"waivers[{index}] 缺少字段：{', '.join(missing)}")
            continue
        if not CODE.match(str(entry["code"])):
            ledger.errors.append(f"waivers[{index}] 规则编号不合法：{entry['code']}")
            continue
        try:
            date.fromisoformat(str(entry["date"]))
            if entry.get("review_after"):
                date.fromisoformat(str(entry["review_after"]))
        except ValueError:
            ledger.errors.append(f"waivers[{index}] 的日期不是 ISO 格式（YYYY-MM-DD）")
            continue
        ledger.waivers.append(
            Waiver(
                code=str(entry["code"]),
                subject=str(entry.get("subject", "")),
                reason=str(entry["reason"]),
                owner=str(entry["owner"]),
                date=str(entry["date"]),
                review_after=(str(entry["review_after"]) if entry.get("review_after") else None),
                raw=entry,
            )
        )
    massless = payload.get("massless_links", [])
    if isinstance(massless, list) and all(isinstance(name, str) for name in massless):
        ledger.massless_links = set(massless)
    elif massless:
        ledger.errors.append(f"{path.name} 的 massless_links 必须是字符串数组")
    return ledger


def apply(
    findings: list[Finding],
    ledger: Ledger,
    today: date,
    *,
    not_evaluated: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[Finding], list[dict], list[dict]]:
    """返回（未豁免结论、已豁免结论、未命中的例外）。

    ``not_evaluated`` 是本次运行**没有评估**的规则编号：为它们写的例外不算死例外
    （URDF702），因为这次运行根本没有机会命中它。
    """

    kept: list[Finding] = []
    waived: list[dict] = []
    used: set[int] = set()
    for finding in findings:
        waiver = ledger.match(finding)
        if waiver is None:
            kept.append(finding)
            continue
        used.add(id(waiver))
        if finding.severity == ERROR:
            kept.append(
                Finding(
                    "URDF704",
                    ERROR,
                    f"规则 {finding.code} 的结论是 error，不能豁免；请修模型或改规则",
                    finding.subject,
                    {"waiver": waiver.raw},
                )
            )
            kept.append(finding)
            continue
        if waiver.review_after and date.fromisoformat(waiver.review_after) < today:
            kept.append(
                Finding(
                    "URDF705",
                    ERROR,
                    f"例外已过期（review_after={waiver.review_after}），请复核后更新",
                    finding.subject,
                    {"code": finding.code, "reason": waiver.reason},
                )
            )
            kept.append(finding)
            continue
        waived.append({**finding.as_dict(), "waiver": waiver.raw})
    unused = [waiver.raw for waiver in ledger.waivers if id(waiver) not in used]
    for entry in unused:
        if str(entry.get("code", "")) in not_evaluated:
            continue
        kept.append(
            Finding(
                "URDF702",
                ERROR,
                f"例外没有命中任何结论（死例外）：{entry.get('code')} {entry.get('subject', '')}".strip(),
                str(entry.get("subject", "")),
                {"waiver": entry},
            )
        )
    return kept, waived, unused
