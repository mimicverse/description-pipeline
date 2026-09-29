"""Legacy shape of the shared strict STL reader; no separate parser."""

from pathlib import Path
from typing import cast

from description_pipeline.geometry.stl import StlError, read_bytes
from description_pipeline.io import file_digest


def bounds(path: Path) -> dict:
    return bounds_bytes(Path(path).read_bytes())


def bounds_bytes(data: bytes) -> dict:
    try:
        stats = read_bytes(data)
    except StlError as error:
        raise ValueError(f"STL: {error}") from error
    return {
        "format": "binary" if stats.binary else "ascii",
        "triangles": stats.triangles,
        "min": list(stats.low),
        "max": list(stats.high),
        "degenerate_triangles": stats.degenerate,
    }


def extent(box: dict) -> tuple[float, float, float]:
    return cast(
        tuple[float, float, float],
        tuple(box["max"][axis] - box["min"][axis] if box["triangles"] else 0.0 for axis in range(3)),
    )


def sha256(path: Path) -> str:
    return file_digest(Path(path))
