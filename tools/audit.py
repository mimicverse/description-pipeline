#!/usr/bin/env python3
"""URDF 合同质检入口：见 docs/urdf_standard.md。

    python tools/audit.py --root . [--policy strict] [--json] [--report docs/urdf_audit.json]

退出码：0 通过；1 有 error 或（strict 下）未豁免的 warning；2 用法/解析错误。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from urdf_quality.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
