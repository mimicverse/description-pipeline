"""Make local source tests importable without installing the package."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

if importlib.util.find_spec("description_pipeline") is None and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
