"""Mass-property math: rotation, combination, principal moments, conditions."""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

Vector3 = Tuple[float, float, float]


def inertia_rotate(inertia: Sequence[Sequence[float]], rot: Sequence[Sequence[float]]):
    """Return ``R @ I @ R^T`` for a symmetric 3x3 inertia tensor."""

    tmp = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            tmp[i][j] = sum(float(rot[i][k]) * float(inertia[k][j]) for k in range(3))
    out = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            out[i][j] = sum(tmp[i][k] * float(rot[j][k]) for k in range(3))
    return tuple(tuple(row) for row in out)


def combine_mass_properties(entries: Sequence[Dict]) -> Dict:
    """Combine component mass properties expressed in a common frame.

    Each entry: ``{"mass": kg, "com": (x, y, z), "inertia": 3x3 about COM}``.
    Returns ``{"mass", "com", "inertia"}`` about the combined COM, same frame.
    Parallel-axis theorem: ``I = I_i + m_i * ((d.d)E - d d^T)``.
    """

    if not entries:
        raise ValueError("no mass-property entries")
    total = sum(float(e["mass"]) for e in entries)
    if total <= 0.0 or not math.isfinite(total):
        raise ValueError("non-positive total mass")
    com = [0.0, 0.0, 0.0]
    for e in entries:
        m = float(e["mass"])
        for axis in range(3):
            com[axis] += m * float(e["com"][axis])
    com = [c / total for c in com]
    inertia = [[0.0] * 3 for _ in range(3)]
    for e in entries:
        m = float(e["mass"])
        d = [float(e["com"][i]) - com[i] for i in range(3)]
        dd = sum(v * v for v in d)
        for i in range(3):
            for j in range(3):
                shift = m * ((dd if i == j else 0.0) - d[i] * d[j])
                inertia[i][j] += float(e["inertia"][i][j]) + shift
    return {
        "mass": total,
        "com": tuple(com),
        "inertia": tuple(tuple(row) for row in inertia),
    }


def inertia6_of(inertia: Sequence[Sequence[float]]):
    """Flatten a 3x3 tensor to URDF order (ixx, ixy, ixz, iyy, iyz, izz)."""

    return (
        float(inertia[0][0]),
        float(inertia[0][1]),
        float(inertia[0][2]),
        float(inertia[1][1]),
        float(inertia[1][2]),
        float(inertia[2][2]),
    )


def principal_moments(inertia: Sequence[Sequence[float]], tol: float = 1e-12, max_iter: int = 64) -> List[float]:
    """Eigenvalues of a symmetric 3x3 tensor via cyclic Jacobi rotations."""

    a = [[float(inertia[i][j]) for j in range(3)] for i in range(3)]
    for _ in range(max_iter):
        off = abs(a[0][1]) + abs(a[0][2]) + abs(a[1][2])
        if off <= tol:
            break
        for p, q in ((0, 1), (0, 2), (1, 2)):
            if abs(a[p][q]) <= tol / 3.0:
                continue
            theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
            t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0))
            c = 1.0 / math.sqrt(t * t + 1.0)
            s = t * c
            for k in range(3):
                akp, akq = a[k][p], a[k][q]
                a[k][p] = c * akp - s * akq
                a[k][q] = s * akp + c * akq
            for k in range(3):
                apk, aqk = a[p][k], a[q][k]
                a[p][k] = c * apk - s * aqk
                a[q][k] = s * apk + c * aqk
    return sorted(a[i][i] for i in range(3))


def physics_conditions(inertia: Sequence[Sequence[float]], tol: float = 1e-12) -> Dict:
    """Positive-definiteness and triangle inequalities of a central inertia."""

    a = [[float(inertia[i][j]) for j in range(3)] for i in range(3)]
    det1 = a[0][0]
    det2 = a[0][0] * a[1][1] - a[0][1] * a[1][0]
    det3 = (
        a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
        - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
        + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
    )
    positive = det1 > tol and det2 > tol and det3 > tol
    principal = principal_moments(inertia, tol=tol)
    triangle_ok = principal[0] + principal[1] - principal[2] >= -max(tol, 1e-9 * abs(principal[2]))
    return {
        "positive_definite": bool(positive),
        "determinants": (det1, det2, det3),
        "principal_moments": principal,
        "triangle_inequalities": bool(triangle_ok),
        "triangle_slack": principal[0] + principal[1] - principal[2],
    }
