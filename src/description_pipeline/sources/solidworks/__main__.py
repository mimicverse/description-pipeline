"""``python -m description_pipeline.sources.solidworks`` runs the worker CLI."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
