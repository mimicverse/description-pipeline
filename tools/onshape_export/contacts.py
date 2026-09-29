"""MJCF 接触排除：把设计上贴合/干涉的刚体对写成 ``<contact><exclude>``。

引擎给每个零件生成网格碰撞体，装配在一起时相邻零件常按设计互相干涉
（轴承压在孔里、螺钉穿过安装柱），零位就会出现自接触并把静置姿态顶偏。
排除关系只能写进 MJCF；URDF 没有对应机制，其他消费者要自行过滤。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from collections.abc import Iterable

BODY_NAME = re.compile(r'<body\b[^>]*\bname="([^"]+)"')
CONTACT_OPEN = re.compile(r"<contact\b[^>]*>")
EXCLUDE_TAG = re.compile(r"<exclude\b([^>]*?)/?>")
ATTRIBUTE = re.compile(r'([A-Za-z_][\w.-]*)="([^"]*)"')
CONTACT_ANCHOR = "</worldbody>"


class ContactError(ValueError):
    pass


def load(path: Path) -> list[tuple[str, str]]:
    """读取排除列表。接受 ``[[body1, body2], …]`` 或 ``{"excludes": […]}``，其余键忽略。

    额外键用于记录来源与理由（见仓库 fixture），不影响解析。
    """

    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ContactError(f"{path} 不是合法 JSON：{error}") from error
    entries = payload.get("excludes") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ContactError(f'{path} 需要 [[body1, body2], …] 或 {{"excludes": […]}}')
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries, start=1):
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 2
            or not all(isinstance(name, str) and name.strip() for name in entry)
        ):
            raise ContactError(f"{path} 第 {index} 项不是 [body1, body2]：{entry!r}")
        pair = (entry[0].strip(), entry[1].strip())
        if pair[0] == pair[1]:
            raise ContactError(f"{path} 第 {index} 项两端同名：{pair[0]}")
        key = tuple(sorted(pair))
        if key in seen:
            raise ContactError(f"{path} 第 {index} 项重复：{pair[0]} ↔ {pair[1]}")
        seen.add(key)
        pairs.append(pair)
    if not pairs:
        raise ContactError(f"{path} 没有任何排除项")
    return pairs


def declared(text: str) -> list[tuple[str, str]]:
    """解析 MJCF 文本里已声明的排除（属性顺序不敏感）。"""

    pairs = []
    for attributes in EXCLUDE_TAG.findall(text):
        values = dict(ATTRIBUTE.findall(attributes))
        if values.get("body1") and values.get("body2"):
            pairs.append((values["body1"], values["body2"]))
    return pairs


def apply(text: str, excludes: Iterable[tuple[str, str]]) -> str:
    """把排除写进 MJCF。刚体名必须存在于模型，写错直接报错而不是静默失效。"""

    excludes = list(excludes)
    known = set(BODY_NAME.findall(text))
    unknown = sorted({name for pair in excludes for name in pair} - known)
    if unknown:
        sample = ", ".join(sorted(known)[:12])
        raise ContactError(
            f"接触排除引用了模型里不存在的刚体：{', '.join(unknown)}；模型有 {len(known)} 个刚体，例如 {sample} …"
        )
    lines = "\n".join(f'    <exclude body1="{a}" body2="{b}"/>' for a, b in excludes)
    if CONTACT_OPEN.search(text):
        return CONTACT_OPEN.sub(lambda match: f"{match.group(0)}\n{lines}", text, count=1)
    if CONTACT_ANCHOR not in text:
        raise ContactError(f"MJCF 里没有 {CONTACT_ANCHOR}，无法插入接触排除")
    block = f"  <contact>\n{lines}\n  </contact>\n"
    return text.replace(CONTACT_ANCHOR, f"{CONTACT_ANCHOR}\n{block.rstrip()}", 1)
