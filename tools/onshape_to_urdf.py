#!/usr/bin/env python3
"""Onshape 装配体 → URDF/MJCF 的入口脚本；细节见 docs/onshape_export.md。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from onshape_export.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
