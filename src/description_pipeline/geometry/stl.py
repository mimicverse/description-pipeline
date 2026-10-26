"""STL reading (binary/ASCII) and geometry statistics: bounds, volume, area, and degeneracy.

Uses one streaming pass for quality checks without caching every triangle. A
damaged or oversized file raises ``StlError`` so the caller can turn it into a
quality result instead of crashing.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import cast

MAX_TRIANGLES = 5_000_000
DEGENERATE_AREA = 1.0e-14  # m^2, approximately (0.1 um)^2
#: Largest triangle count per delivered mesh part; larger native meshes are split
#: losslessly.  Owned here so the generator and the verifier cannot drift (VER-03).
MESH_PART_TRIANGLES_MAX = 199_999


class StlError(ValueError):
    pass


@dataclass(frozen=True)
class MeshStats:
    triangles: int
    low: tuple[float, float, float]
    high: tuple[float, float, float]
    volume: float  # m^3, absolute signed volume of a closed mesh
    signed_volume: float  # m^3; a negative value means normals point inward
    area: float  # m^2
    degenerate: int
    degenerate_area: float  # m^2, total area of degenerate triangles
    binary: bool
    com: tuple[float, float, float]  # uniform-density center of mass in mesh coordinates
    second_moment: tuple[float, ...]  # integral of x*x^T dV at unit density, nine components
    boundary_edges: int  # edges not shared by exactly two triangles (CAD T-junctions)

    @property
    def extent(self) -> tuple[float, float, float]:
        return cast(
            tuple[float, float, float],
            tuple(self.high[axis] - self.low[axis] for axis in range(3)),
        )

    @property
    def half_diagonal(self) -> float:
        return 0.5 * math.dist(self.low, self.high)

    @property
    def center(self) -> tuple[float, float, float]:
        return cast(
            tuple[float, float, float],
            tuple((self.low[axis] + self.high[axis]) / 2 for axis in range(3)),
        )


def read(path: Path) -> MeshStats:
    """Read and measure an STL file; raise :class:`StlError` when unavailable or invalid."""

    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise StlError(f"cannot read STL: {error}") from error
    return read_bytes(data)


def read_bytes(data: bytes) -> MeshStats:
    """Use the identical strict acceptance contract for file and source-adapter bytes."""
    if len(data) < 15:
        raise StlError("STL file is too short")
    if len(data) >= 84 and len(data) == 84 + 50 * struct.unpack("<I", data[80:84])[0]:
        return _read_binary(data)
    return _read_ascii(data)


def vertices(path: Path):
    """Return triangle vertices after the same strict format validation."""
    return vertices_bytes(Path(path).read_bytes())


def vertices_bytes(data: bytes):
    """Return triangle vertices from already loaded bytes."""
    import numpy as np

    stats = read_bytes(data)
    if stats.binary:
        records = np.frombuffer(
            data,
            dtype=np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attribute", "<u2")]),
            count=stats.triangles,
            offset=84,
        )
        return records["vertices"].reshape(-1, 3).astype(float)
    return np.array(
        [
            [float(value) for value in fields[1:]]
            for line in data.decode("ascii", errors="ignore").splitlines()
            if len(fields := line.split()) == 4 and fields[0] == "vertex"
        ]
    )


def _read_binary(data: bytes) -> MeshStats:
    count = struct.unpack("<I", data[80:84])[0]
    if count == 0:
        raise StlError("binary STL contains no triangles")
    if count > MAX_TRIANGLES:
        raise StlError(f"triangle count {count} exceeds the limit {MAX_TRIANGLES}")
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    volume = 0.0
    area = 0.0
    degenerate = 0
    degenerate_area = 0.0
    moment = [0.0] * 9
    centroid = [0.0] * 3
    edges: dict[tuple, int] = {}
    for index in range(count):
        offset = 84 + 50 * index + 12
        corners = [struct.unpack_from("<3f", data, offset + vertex * 12) for vertex in range(3)]
        for corner in corners:
            for axis in range(3):
                value = corner[axis]
                if not math.isfinite(value):
                    raise StlError(f"triangle {index + 1} contains a non-finite coordinate")
                low[axis] = min(low[axis], value)
                high[axis] = max(high[axis], value)
        triangle_area, signed = _triangle(corners)
        area += triangle_area
        volume += signed
        _accumulate(corners, signed, moment, centroid, edges)
        if triangle_area <= DEGENERATE_AREA:
            degenerate += 1
            degenerate_area += triangle_area
    return _finish(count, low, high, volume, area, degenerate, degenerate_area, True, moment, centroid, edges)


def _read_ascii(data: bytes) -> MeshStats:
    text = data.decode("ascii", errors="ignore")
    if "vertex" not in text or not text.lstrip().lower().startswith("solid"):
        raise StlError("data is neither binary STL nor ASCII STL with a solid header")
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    volume = 0.0
    area = 0.0
    degenerate = 0
    degenerate_area = 0.0
    count = 0
    moment = [0.0] * 9
    centroid = [0.0] * 3
    edges: dict[tuple, int] = {}
    corners: list[tuple[float, float, float]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0] == "vertex":
            try:
                corner = tuple(float(value) for value in fields[1:])
            except ValueError as error:
                raise StlError(f"cannot parse vertex coordinates: {line.strip()}") from error
            if not all(math.isfinite(value) for value in corner):
                raise StlError(f"vertex coordinates are non-finite: {line.strip()}")
            corners.append(corner)  # type: ignore[arg-type]
            for axis in range(3):
                low[axis] = min(low[axis], corner[axis])
                high[axis] = max(high[axis], corner[axis])
            if len(corners) == 3:
                count += 1
                if count > MAX_TRIANGLES:
                    raise StlError(f"triangle count exceeds the limit {MAX_TRIANGLES}")
                triangle_area, signed = _triangle(corners)
                area += triangle_area
                volume += signed
                _accumulate(corners, signed, moment, centroid, edges)
                if triangle_area <= DEGENERATE_AREA:
                    degenerate += 1
                    degenerate_area += triangle_area
                corners = []
    if count == 0 or corners:
        raise StlError("ASCII STL vertex count is not divisible by three or contains no triangles")
    return _finish(count, low, high, volume, area, degenerate, degenerate_area, False, moment, centroid, edges)


def _accumulate(corners, signed: float, moment: list, centroid: list, edges: dict) -> None:
    """Accumulate unit-density volume moments, centroid, and edge counts."""

    if abs(signed) > 1.0e-18:
        # Tetrahedron (0, a, b, c): integral(x*x^T dV) = V/20 * A (I + J) A^T.
        for row in range(3):
            for column in range(3):
                value = 0.0
                for k in range(3):
                    for other in range(3):
                        weight = 2.0 if k == other else 1.0
                        value += corners[k][row] * corners[other][column] * weight
                moment[row * 3 + column] += signed / 20.0 * value
        for axis in range(3):
            centroid[axis] += signed * (corners[0][axis] + corners[1][axis] + corners[2][axis]) / 4.0
    for first, second in ((0, 1), (1, 2), (2, 0)):
        key = tuple(sorted((_rounded(corners[first]), _rounded(corners[second]))))
        edges[key] = edges.get(key, 0) + 1


def _rounded(point):
    return tuple(round(value, 6) for value in point)


def _finish(count, low, high, volume, area, degenerate, degenerate_area, binary, moment, centroid, edges) -> MeshStats:
    sign = 1.0 if volume >= 0 else -1.0
    magnitude = abs(volume)
    if magnitude > 1.0e-15:
        com = tuple(sign * value / magnitude for value in centroid)
        moment = [sign * value for value in moment]
    else:
        com = tuple((low[axis] + high[axis]) / 2 for axis in range(3))
        moment = [0.0] * 9
    return MeshStats(
        triangles=count,
        low=tuple(low),
        high=tuple(high),
        volume=abs(volume),
        signed_volume=volume,
        area=area,
        degenerate=degenerate,
        degenerate_area=degenerate_area,
        binary=binary,
        com=com,
        second_moment=tuple(moment),
        boundary_edges=sum(1 for shared in edges.values() if shared != 2),
    )


def _triangle(corners) -> tuple[float, float]:
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = corners
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    area = 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)
    signed = (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
    return area, signed
