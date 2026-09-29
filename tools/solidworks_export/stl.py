"""Binary STL helpers: counting, validation and merging (no dependencies)."""

from __future__ import annotations

import struct
from typing import Iterator, Optional, Sequence, Tuple

HEADER_SIZE = 80
TRIANGLE_SIZE = 50


def count_triangles(path: str) -> int:
    with open(path, "rb") as handle:
        header = handle.read(HEADER_SIZE)
        if len(header) != HEADER_SIZE:
            raise ValueError(f"{path}: not a binary STL (short header)")
        count_bytes = handle.read(4)
        if len(count_bytes) != 4:
            raise ValueError(f"{path}: not a binary STL (missing triangle count)")
        count = struct.unpack("<I", count_bytes)[0]
    return int(count)


def iter_triangles(path: str) -> Iterator[Tuple[tuple, tuple, tuple, tuple]]:
    """Yield ``(normal, v0, v1, v2)`` for each facet of a binary STL."""

    validate_binary_stl(path)
    with open(path, "rb") as handle:
        handle.seek(HEADER_SIZE + 4)
        data = handle.read()
    for offset in range(0, len(data), TRIANGLE_SIZE):
        record = data[offset : offset + TRIANGLE_SIZE]
        yield (
            struct.unpack_from("<3f", record, 0),
            struct.unpack_from("<3f", record, 12),
            struct.unpack_from("<3f", record, 24),
            struct.unpack_from("<3f", record, 36),
        )


def aabb(path: str):
    """Axis-aligned bounding box ``(min_xyz, max_xyz)`` of a binary STL."""

    mins = [float("inf")] * 3
    maxs = [float("-inf")] * 3
    found = False
    for _normal, v0, v1, v2 in iter_triangles(path):
        found = True
        for vertex in (v0, v1, v2):
            for axis in range(3):
                mins[axis] = min(mins[axis], vertex[axis])
                maxs[axis] = max(maxs[axis], vertex[axis])
    if not found:
        raise ValueError(f"empty mesh: {path}")
    return tuple(mins), tuple(maxs)


def signed_volume(path: str) -> float:
    """Signed volume of a closed mesh; positive for outward winding."""

    total = 0.0
    for _normal, v0, v1, v2 in iter_triangles(path):
        total += (
            v0[0] * (v1[1] * v2[2] - v1[2] * v2[1])
            - v0[1] * (v1[0] * v2[2] - v1[2] * v2[0])
            + v0[2] * (v1[0] * v2[1] - v1[1] * v2[0])
        ) / 6.0
    return total


def validate_binary_stl(path: str) -> int:
    count = count_triangles(path)
    import os

    expected = HEADER_SIZE + 4 + count * TRIANGLE_SIZE
    actual = os.path.getsize(path)
    if actual != expected:
        raise ValueError(f"{path}: size mismatch (expected {expected} bytes for {count} triangles, found {actual})")
    return count


def _transform_facet(record: bytes, matrix) -> bytes:
    """Apply a rigid 4x4 transform to one 50-byte binary STL facet record."""

    normal = struct.unpack_from("<3f", record, 0)
    rotated_normal = (
        float(matrix[0]) * normal[0] + float(matrix[1]) * normal[1] + float(matrix[2]) * normal[2],
        float(matrix[4]) * normal[0] + float(matrix[5]) * normal[1] + float(matrix[6]) * normal[2],
        float(matrix[8]) * normal[0] + float(matrix[9]) * normal[1] + float(matrix[10]) * normal[2],
    )
    length = sum(value * value for value in rotated_normal) ** 0.5
    if length > 1e-12:
        rotated_normal = tuple(value / length for value in rotated_normal)
    out = struct.pack("<3f", *rotated_normal)
    for offset in (12, 24, 36):
        x, y, z = struct.unpack_from("<3f", record, offset)
        out += struct.pack(
            "<3f",
            float(matrix[0]) * x + float(matrix[1]) * y + float(matrix[2]) * z + float(matrix[3]),
            float(matrix[4]) * x + float(matrix[5]) * y + float(matrix[6]) * z + float(matrix[7]),
            float(matrix[8]) * x + float(matrix[9]) * y + float(matrix[10]) * z + float(matrix[11]),
        )
    out += record[48:50]
    return out


def merge_binary_stl(
    paths: Sequence[str],
    out_path: str,
    source_note: str = "",
    transforms: Optional[Sequence[Optional[Sequence[float]]]] = None,
) -> int:
    """Merge binary STLs into one file; triangle order is the input order.

    ``transforms`` optionally provides one rigid 4x4 transform per input file;
    each facet is transformed (rotation applied to its normal, full transform
    to its vertices) before merging, so the merged mesh lives in one frame.
    """

    if not paths:
        raise ValueError("no STL files to merge")
    if transforms is not None and len(transforms) != len(paths):
        raise ValueError("transforms length must match paths length")
    total = 0
    payload = []
    for index, path in enumerate(paths):
        total += validate_binary_stl(path)
        with open(path, "rb") as handle:
            handle.seek(HEADER_SIZE + 4)
            chunk = handle.read()
        matrix = transforms[index] if transforms else None
        if matrix is not None:
            chunk = b"".join(
                _transform_facet(chunk[offset : offset + TRIANGLE_SIZE], matrix)
                for offset in range(0, len(chunk), TRIANGLE_SIZE)
            )
        payload.append(chunk)
    header = (f"merged by swbridge {source_note}").encode("ascii", "replace")[:HEADER_SIZE]
    header = header + b" " * (HEADER_SIZE - len(header))
    with open(out_path, "wb") as handle:
        handle.write(header)
        handle.write(struct.pack("<I", total))
        for chunk in payload:
            handle.write(chunk)
    return total
