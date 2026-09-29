"""Make the adapter importable with or without the integration checkout.

When the integration tree is on ``PYTHONPATH`` (a merged package that contains
both ``sources/snapshot.py`` and this adapter), that copy wins.  Otherwise the
adapter's own ``src`` directory is used, which is enough for every test that
does not need the shared model package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

if importlib.util.find_spec("description_pipeline") is None and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
