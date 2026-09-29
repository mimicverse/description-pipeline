"""`config/joint_names.yaml`：结构关节台账（URDF208）的读取。

只支持本仓库约定的形状（顶层 `structural_joint_names:` 键 + `- 名字` 行）。文件缺失、
读不出或解析不了都返回 ``None``，由 URDF208 报错——不静默跳过，也不让一次读取失手把
审计变成回溯。
"""

from __future__ import annotations

from pathlib import Path


def load(path: Path) -> list[str] | None:
    """台账里声明的关节名；不可读或不是这个形状时为 ``None``。"""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else None
    except (OSError, UnicodeDecodeError):
        return None
    if text is None:
        return None
    names: list[str] = []
    inside = False
    saw_key = False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("structural_joint_names:"):
            inside = True
            saw_key = True
            continue
        if inside:
            if stripped.startswith("- "):
                names.append(stripped[2:].strip().strip("'\""))
            elif not stripped.startswith("-"):
                break
    if not saw_key:
        return None
    return names
