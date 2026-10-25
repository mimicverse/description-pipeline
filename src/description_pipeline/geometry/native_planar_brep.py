"""Bounded native planar BRep construction and deterministic triangulation."""

from __future__ import annotations

from fractions import Fraction
import math
import struct

from .native_u import (
    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
    native_binary64_sqrt,
    native_binary64_sum_upper,
    native_binary64_unit_vector,
)


NATIVE_PLANAR_BREP_SCHEMA = "solidworks-to-urdf.native-planar-brep/v2"
NATIVE_PLANAR_BREP_PROVIDER = "native-planar-brep-v2"
NATIVE_PLANAR_CONVEX_FAN_RULE = "native-planar-projected-convex-fan-v2"
NATIVE_PLANAR_COEDGE_DIRECTION_RULE = "native-coedge-directed-evaluate-tangent-v3"
NATIVE_PLANAR_COEDGE_VERTEX_RULE = "native-coedge-adjacent-edge-vertex-incidence-v2"
NATIVE_PLANAR_EDGE_VERTEX_RULE = "native-curve-sense-vertex-incidence-v1"
NATIVE_PLANAR_COEDGE_EVALUATION_RULE = "native-coedge-evaluate-float32-operation-v1"


class PlanarBrepNotApplicable(ValueError):
    """The native facts are valid but outside the strict planar provider domain."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PlanarBrepEvidenceError(ValueError):
    """The native facts are malformed or contradict one another."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _finite_vector(value, length: int, *, rational=False) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == length
        and all(
            type(item) in ((int, float, Fraction) if rational else (int, float))
            and (isinstance(item, Fraction) or math.isfinite(item))
            for item in value
        )
    )


def _normalized(vector: tuple[float, float, float]):
    if not any(vector):
        raise PlanarBrepEvidenceError("planar face normal must be finite and nonzero")
    return native_binary64_unit_vector(vector)


def _float32_xyz(point):
    values = tuple(float(value) for value in point)
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        raise PlanarBrepEvidenceError("planar triangle point must contain three finite values")
    try:
        packed = struct.pack("<3f", *values)
    except (OverflowError, struct.error) as error:
        raise PlanarBrepEvidenceError("planar triangle point is outside binary32") from error
    return struct.unpack("<3f", packed)


def _projection(
    points: tuple[tuple[float, float, float], ...],
    normal: tuple[float, float, float],
):
    axis = max(range(3), key=lambda index: abs(normal[index]))
    first = (axis + 1) % 3
    second = (axis + 2) % 3
    projected = tuple(
        (
            Fraction(point[first]),
            Fraction(point[second]),
        )
        for point in points
    )
    return normal[axis], projected


def _squared_distance(first, second) -> Fraction:
    return sum((Fraction(second[index]) - Fraction(first[index])) ** 2 for index in range(3))


def project_native_plane(point, plane_point, plane_normal) -> tuple[Fraction, Fraction, Fraction]:
    """Project along the dominant native normal axis using exact arithmetic."""

    normal = tuple(Fraction(value) for value in plane_normal)
    axis = max(range(3), key=lambda index: abs(normal[index]))
    if normal[axis] == 0:
        raise PlanarBrepEvidenceError("planar face normal must be nonzero")
    projected = [Fraction(value) for value in point]
    projected[axis] = (
        Fraction(plane_point[axis])
        - sum(normal[index] * (projected[index] - Fraction(plane_point[index])) for index in range(3) if index != axis)
        / normal[axis]
    )
    return tuple(projected)


def _plane_residual(
    point: tuple[float, float, float],
    plane_point: tuple[float, float, float],
    plane_normal: tuple[float, float, float],
) -> Fraction:
    """Return the exact distance from ``point`` to the supplied native plane."""

    normal = tuple(Fraction.from_float(value) for value in plane_normal)
    normal_length_squared = sum(value * value for value in normal)
    numerator = sum(
        normal[index] * (Fraction.from_float(point[index]) - Fraction.from_float(plane_point[index]))
        for index in range(3)
    )
    return numerator * numerator / normal_length_squared


def _outward_sqrt(value: Fraction) -> float:
    return native_binary64_sqrt(value, upward=True)


def _cross2(
    first: tuple[Fraction, Fraction],
    second: tuple[Fraction, Fraction],
    third: tuple[Fraction, Fraction],
) -> Fraction:
    return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (third[0] - first[0])


def _point_on_segment(
    point: tuple[Fraction, Fraction],
    start: tuple[Fraction, Fraction],
    end: tuple[Fraction, Fraction],
) -> bool:
    return (
        _cross2(start, end, point) == 0
        and min(start[0], end[0]) <= point[0] <= max(start[0], end[0])
        and min(start[1], end[1]) <= point[1] <= max(start[1], end[1])
    )


def _segments_intersect(first, second) -> bool:
    a, b = first
    c, d = second
    ab_c = _cross2(a, b, c)
    ab_d = _cross2(a, b, d)
    cd_a = _cross2(c, d, a)
    cd_b = _cross2(c, d, b)
    if ((ab_c > 0 and ab_d < 0) or (ab_c < 0 and ab_d > 0)) and ((cd_a > 0 and cd_b < 0) or (cd_a < 0 and cd_b > 0)):
        return True
    return (
        (ab_c == 0 and _point_on_segment(c, a, b))
        or (ab_d == 0 and _point_on_segment(d, a, b))
        or (cd_a == 0 and _point_on_segment(a, c, d))
        or (cd_b == 0 and _point_on_segment(b, c, d))
    )


def _orientation3(first, second, third, normal) -> Fraction:
    first = tuple(Fraction.from_float(value) for value in first)
    second = tuple(Fraction.from_float(value) for value in second)
    third = tuple(Fraction.from_float(value) for value in third)
    normal = tuple(Fraction.from_float(value) for value in normal)
    left = tuple(second[index] - first[index] for index in range(3))
    right = tuple(third[index] - first[index] for index in range(3))
    return (
        left[0] * (right[1] * normal[2] - right[2] * normal[1])
        - left[1] * (right[0] * normal[2] - right[2] * normal[0])
        + left[2] * (right[0] * normal[1] - right[1] * normal[0])
    )


def _validate_boundary(projected, orientation_sign, *, encoded=False):
    """Check a complete projected disk boundary, including nonadjacent sides."""
    prefix = "binary32 planar" if encoded else "planar polygon"
    if len(set(projected)) != len(projected):
        raise PlanarBrepEvidenceError(f"{prefix} boundary repeats a vertex")
    for index in range(len(projected)):
        turn = _cross2(
            projected[index], projected[(index + 1) % len(projected)], projected[(index + 2) % len(projected)]
        )
        if turn * orientation_sign <= 0:
            if encoded:
                raise PlanarBrepEvidenceError("binary32 planar boundary loses strict convexity or orientation")
            raise PlanarBrepNotApplicable("planar polygon is not strictly convex")
    for first_index in range(len(projected)):
        for second_index in range(first_index + 1, len(projected)):
            if second_index == (first_index + 1) % len(projected) or first_index == (second_index + 1) % len(projected):
                continue
            if _segments_intersect(
                (projected[first_index], projected[(first_index + 1) % len(projected)]),
                (projected[second_index], projected[(second_index + 1) % len(projected)]),
            ):
                raise PlanarBrepEvidenceError(f"{prefix} boundary self-intersects")


def build_planar_convex_triangulation(
    face_normal,
    plane_point,
    points,
    *,
    source_displacements=None,
    tangent_displacements=None,
) -> dict:
    """Return one orientation-preserving fan for a strict convex polygon.

    ``PlanarBrepNotApplicable`` means the facts are readable and internally
    coherent, but the boundary is outside the strict single-loop convex domain.
    ``PlanarBrepEvidenceError`` means malformed data, contradictory native
    direction, unresolved self-intersection, or an unrepresentable binary32
    result.  Callers must not turn the latter into an ITessellation fallback.
    """

    if not _finite_vector(face_normal, 3):
        raise PlanarBrepEvidenceError("planar face normal is malformed")
    if not _finite_vector(plane_point, 3):
        raise PlanarBrepEvidenceError("planar root point is malformed")
    normal = _normalized(tuple(float(value) for value in face_normal))
    plane_point = tuple(float(value) for value in plane_point)
    if not isinstance(points, (list, tuple)) or len(points) < 3:
        raise PlanarBrepEvidenceError("planar polygon requires at least three vertices")
    native_points = []
    for point in points:
        if not _finite_vector(point, 3):
            raise PlanarBrepEvidenceError("planar polygon vertex is malformed")
        native_points.append(tuple(float(value) for value in point))
    if len(set(native_points)) != len(native_points):
        raise PlanarBrepEvidenceError("planar polygon repeats a native vertex")
    if source_displacements is None:
        source_displacements = ()
    if not isinstance(source_displacements, (list, tuple)):
        raise PlanarBrepEvidenceError("planar source displacement evidence is malformed")
    source_error_squared = Fraction(0, 1)
    for pair in source_displacements:
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not _finite_vector(pair[0], 3, rational=True)
            or not _finite_vector(pair[1], 3, rational=True)
        ):
            raise PlanarBrepEvidenceError("planar source displacement evidence is malformed")
        source_error_squared = max(
            source_error_squared,
            _squared_distance(pair[0], pair[1]),
        )
    tangent_error_squared = Fraction(0, 1)
    if tangent_displacements is not None and not isinstance(tangent_displacements, (list, tuple)):
        raise PlanarBrepEvidenceError("planar tangent displacement evidence is malformed")
    for pair in tangent_displacements or ():
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not all(_finite_vector(point, 3, rational=True) for point in pair)
        ):
            raise PlanarBrepEvidenceError("planar tangent displacement evidence is malformed")
        tangent_error_squared = max(tangent_error_squared, _squared_distance(*pair))

    projection_normal = tuple(float(value) for value in face_normal)
    plane_residual_squared = tuple(_plane_residual(point, plane_point, projection_normal) for point in native_points)
    max_plane_residual_squared = max(plane_residual_squared)
    exact_constructed = tuple(project_native_plane(point, plane_point, projection_normal) for point in native_points)
    constructed_points = tuple(tuple(float(value) for value in point) for point in exact_constructed)
    if not all(_finite_vector(point, 3) for point in constructed_points):
        raise PlanarBrepEvidenceError("projected planar point is outside binary64")
    construction_squared = max(
        _squared_distance(raw, projected) for raw, projected in zip(native_points, exact_constructed, strict=True)
    )
    projection_rounding_squared = max(
        _squared_distance(exact, rounded) for exact, rounded in zip(exact_constructed, constructed_points, strict=True)
    )

    normal_axis, projected = _projection(exact_constructed, projection_normal)
    if normal_axis == 0.0:
        raise PlanarBrepEvidenceError("planar face normal cannot define polygon orientation")
    oriented_area_sign = Fraction(1, 1) if Fraction.from_float(normal_axis) > 0 else Fraction(-1, 1)
    signed_area = sum(
        (
            projected[index][0] * projected[(index + 1) % len(projected)][1]
            - projected[index][1] * projected[(index + 1) % len(projected)][0]
        )
        for index in range(len(projected))
    )
    if signed_area == 0:
        raise PlanarBrepEvidenceError("planar polygon boundary has no nonzero area")
    if signed_area * oriented_area_sign < 0:
        raise PlanarBrepEvidenceError("planar polygon boundary contradicts face orientation")
    _validate_boundary(projected, oriented_area_sign)
    stl_boundary = tuple(_float32_xyz(point) for point in constructed_points)
    _, stl_projected_boundary = _projection(stl_boundary, projection_normal)
    _validate_boundary(stl_projected_boundary, oriented_area_sign, encoded=True)

    triangles = []
    max_vertex_error_squared = Fraction(0, 1)
    for index in range(1, len(native_points) - 1):
        vertex_indices = (0, index, index + 1)
        stl_points = tuple(stl_boundary[vertex_index] for vertex_index in vertex_indices)
        for native_index, stl_point in zip(
            vertex_indices,
            stl_points,
            strict=True,
        ):
            max_vertex_error_squared = max(
                max_vertex_error_squared,
                _squared_distance(constructed_points[native_index], stl_point),
            )
        _, stl_projected = _projection(stl_points, projection_normal)
        stl_cross = _cross2(
            stl_projected[0],
            stl_projected[1],
            stl_projected[2],
        )
        if stl_cross * oriented_area_sign <= 0 or _orientation3(*stl_points, projection_normal) <= 0:
            raise PlanarBrepEvidenceError("binary32 planar triangle is degenerate or reverses orientation")
        triangles.append(
            {
                "triangle_index": index - 1,
                "vertex_indices": list(vertex_indices),
                "native_points": [
                    list(point)
                    for point in (
                        native_points[vertex_indices[0]],
                        native_points[vertex_indices[1]],
                        native_points[vertex_indices[2]],
                    )
                ],
                "constructed_points": [list(constructed_points[vertex_index]) for vertex_index in vertex_indices],
                "stl_points": [list(point) for point in stl_points],
            }
        )
    surface_error_limit = Fraction.from_float(NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M)
    error_components = {
        "native_plane_residual": _outward_sqrt(max_plane_residual_squared),
        "constructive_vertex_displacement": _outward_sqrt(construction_squared),
        "binary64_projection_displacement": _outward_sqrt(projection_rounding_squared),
        "cross_api_vertex_displacement": _outward_sqrt(source_error_squared),
        "tangent_segment_displacement": _outward_sqrt(tangent_error_squared),
        "binary32_vertex_displacement": _outward_sqrt(max_vertex_error_squared),
    }
    total_error_bound = native_binary64_sum_upper(error_components.values())
    if Fraction.from_float(total_error_bound) > surface_error_limit:
        raise PlanarBrepEvidenceError("planar provider complete surface bound exceeds its limit")
    return {
        "rule_version": NATIVE_PLANAR_CONVEX_FAN_RULE,
        "face_normal": list(normal),
        "error_components_m": error_components,
        "surface_error_upper_bound_m": total_error_bound,
        "surface_error_limit_m": (NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M),
        "boundary_area_m2": native_binary64_sqrt(
            signed_area**2
            * sum(Fraction.from_float(value) ** 2 for value in projection_normal)
            / (4 * Fraction.from_float(normal_axis) ** 2)
        ),
        "triangles": triangles,
    }
