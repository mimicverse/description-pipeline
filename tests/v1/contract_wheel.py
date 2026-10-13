"""The release-wheel fixture shared by bootstrap derive/emit tests.

The bootstrap runs the canonical IF-16 snapshot validation from the contract closure
carried inside the digest-bound release wheel; fixtures that drive the real install/emit
entry points therefore build a wheel-shaped archive from the package source instead of
placeholder bytes.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def write_contract_wheel(path: Path) -> None:
    """Write the release wheel fixture: the package source (closure included) from ``src``."""
    source = ROOT / "src" / "description_pipeline"
    with zipfile.ZipFile(path, "w") as archive:
        for file in sorted(source.rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts:
                archive.write(file, f"description_pipeline/{file.relative_to(source).as_posix()}")
