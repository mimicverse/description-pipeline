"""Versioned native U, packed COM status, and binary32 round-trip rules."""

from __future__ import annotations

import math
import struct

NATIVE_U_IDENTITY_RULE = "native-u-identity-128ulp-v1"
NATIVE_U_IDENTITY_ULPS = 128
NATIVE_U_COVERAGE_COVERED = "covered"
NATIVE_U_COVERAGE_UNCOVERED = "uncovered"
NATIVE_U_COVERAGE_UNRESOLVED = "unresolved"
NATIVE_POINT_ROUNDTRIP_RULE = "native-point-float32-adjacent-v2"
NATIVE_CURVE_DOMAIN_RULE = "native-curve-domain-edge-local-v2"
NATIVE_U_RESOLUTION_RULE = "native-u-binary32-resolution-gate-v1"


def _native_u(value: float | int) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError("U values must be finite int/float values")
    return float(value)


def native_u_tolerance(left: float | int, right: float | int) -> float:
    """Return the exact v1 boundary: 128 ULP of max(abs(left), abs(right))."""

    scale = max(abs(_native_u(left)), abs(_native_u(right)))
    # math.ulp(0.0) is the explicit zero-scale boundary. It is not replaced by
    # an absolute epsilon, so a non-zero gap from zero remains comparable.
    return NATIVE_U_IDENTITY_ULPS * math.ulp(scale)


def native_u_equal(left: float | int, right: float | int) -> bool:
    """Compare two finite U values using the versioned 128-ULP identity."""

    left_value = _native_u(left)
    right_value = _native_u(right)
    return abs(left_value - right_value) <= native_u_tolerance(
        left_value,
        right_value,
    )


def native_u_in_closed_interval(
    value: float | int,
    lower: float | int,
    upper: float | int,
) -> bool:
    """Test closed interval membership with the same versioned boundary rule."""

    return (
        value >= lower or native_u_equal(value, lower)
    ) and (
        value <= upper or native_u_equal(value, upper)
    )


def native_u_coverage_status(
    intervals: list[tuple[float | int, float | int]],
    lower: float | int,
    upper: float | int,
) -> str:
    """Classify U coverage, refusing domains not separable by the identity rule."""

    lower_value = _native_u(lower)
    upper_value = _native_u(upper)
    if lower_value > upper_value:
        return NATIVE_U_COVERAGE_UNRESOLVED
    domain_width = upper_value - lower_value
    if domain_width <= native_u_tolerance(lower_value, upper_value):
        return NATIVE_U_COVERAGE_UNRESOLVED
    normalized = []
    for start, end in intervals:
        start_value = _native_u(start)
        end_value = _native_u(end)
        if start_value > end_value:
            return NATIVE_U_COVERAGE_UNRESOLVED
        normalized.append((start_value, end_value))
    if not normalized:
        return NATIVE_U_COVERAGE_UNCOVERED
    normalized.sort()
    first_start, covered_end = normalized[0]
    if first_start > lower_value and not native_u_equal(
        first_start,
        lower_value,
    ):
        return NATIVE_U_COVERAGE_UNCOVERED
    for start_value, end_value in normalized[1:]:
        if start_value > covered_end and not native_u_equal(
            start_value,
            covered_end,
        ):
            return NATIVE_U_COVERAGE_UNCOVERED
        covered_end = max(covered_end, end_value)
    if covered_end >= upper_value or native_u_equal(covered_end, upper_value):
        return NATIVE_U_COVERAGE_COVERED
    return NATIVE_U_COVERAGE_UNCOVERED


def native_u_intervals_cover(
    intervals: list[tuple[float | int, float | int]],
    lower: float | int,
    upper: float | int,
) -> bool:
    """Accept only a covered, distinguishable U domain."""

    return (
        native_u_coverage_status(intervals, lower, upper)
        == NATIVE_U_COVERAGE_COVERED
    )


def native_boolean_success(value: object) -> bool | None:
    """Normalize a COM boolean-like 0/1 result without accepting other values."""

    if type(value) is bool:
        return value
    if type(value) in (int, float) and value in (0, 1):
        return bool(value)
    return None


def native_get_parameter_success(value: object) -> bool | None:
    """Parse GetParameter's SAFEARRAY flag without truthiness coercion."""

    return native_boolean_success(value)


def native_evaluate2_evidence(raw: list | tuple) -> dict | None:
    """Serialize seven Evaluate2 doubles without placing packed bits in JSON."""

    if not isinstance(raw, (list, tuple)) or len(raw) != 7:
        return None
    payload = raw[:6]
    if any(
        type(value) not in (int, float)
        or not math.isfinite(float(value))
        for value in payload
    ):
        return None
    packed = raw[6]
    if type(packed) not in (int, float):
        return None
    bits = struct.unpack("<Q", struct.pack("<d", float(packed)))[0]
    low32 = bits & 0xFFFFFFFF
    return {
        "payload": [float(value) for value in payload],
        "packed_status": {
            "raw_type": type(packed).__name__,
            "raw_bits_u64": bits,
            "high32": (bits >> 32) & 0xFFFFFFFF,
            "low32": low32,
            "parsed_success": low32 == 1,
        },
    }


def native_evaluate2_success(raw: list | tuple) -> bool:
    """Decode the packed double-long status in Evaluate2's seventh value."""

    evidence = native_evaluate2_evidence(raw)
    return bool(
        evidence is not None
        and evidence["packed_status"]["parsed_success"] is True
    )


def _binary32(value: float | int) -> float:
    numeric = _native_u(value)
    try:
        return struct.unpack(">f", struct.pack(">f", numeric))[0]
    except OverflowError:
        return math.copysign(math.inf, numeric)


def _binary32_from_bits(bits: int) -> float:
    return struct.unpack(">f", struct.pack(">I", bits))[0]


def _binary32_bits(value: float) -> int:
    return struct.unpack(">I", struct.pack(">f", value))[0]


def native_float32_adjacency_bound(value: float | int) -> float:
    """Return the largest exact gap to an adjacent finite binary32 value."""

    rounded = _binary32(value)
    if not math.isfinite(rounded):
        raise ValueError("point coordinates must fit finite binary32 values")
    if rounded == 0.0:
        candidates = (1, 0x80000001)
    else:
        bits = _binary32_bits(rounded)
        candidates = (bits - 1, bits + 1)
    gaps = []
    for candidate in candidates:
        if not 0 <= candidate <= 0xFFFFFFFF:
            continue
        neighbor = _binary32_from_bits(candidate)
        if math.isfinite(neighbor):
            gaps.append(abs(neighbor - rounded))
    if not gaps:
        raise ValueError("binary32 adjacency is unavailable")
    return max(gaps)


def native_binary32_spacing(value: float | int) -> float:
    """Return the positive adjacent binary32 spacing at the input scale."""

    scale = abs(_binary32(value))
    if not math.isfinite(scale):
        raise ValueError("point coordinates must fit finite binary32 values")
    bits = _binary32_bits(scale)
    if bits == 0x7F7FFFFF:
        return math.inf
    return _binary32_from_bits(bits + 1) - scale


def native_u_domain_is_resolvable(
    lower: float | int,
    upper: float | int,
) -> bool:
    """Return whether the identity rule can distinguish a complete U domain."""

    return (
        native_u_coverage_status([(lower, upper)], lower, upper)
        != NATIVE_U_COVERAGE_UNRESOLVED
    )


def native_point_error(
    left: tuple[float | int, float | int, float | int],
    right: tuple[float | int, float | int, float | int],
) -> float:
    """Return max coordinate error without selecting a tolerance rule."""

    if len(left) != 3 or len(right) != 3:
        raise ValueError("points must contain three coordinates")
    return max(
        abs(_native_u(left[index]) - _native_u(right[index]))
        for index in range(3)
    )


def native_point_roundtrip_tolerance(*points) -> float:
    """Bound one finite XYZ point to its binary32 serialization and back."""

    if not points:
        raise ValueError("at least one point is required")
    bound = 0.0
    for point in points:
        if len(point) != 3:
            raise ValueError("points must contain three coordinates")
        for coordinate in point:
            bound = max(bound, native_float32_adjacency_bound(coordinate))
    return bound


def native_curve_domain_transform(
    underlying_start: float | int,
    underlying_end: float | int,
    trimmed_start: float | int,
    trimmed_end: float | int,
    sense: bool,
    is_periodic: bool | None,
) -> dict:
    """Map a trimmed edge U domain into its underlying native curve domain."""

    curve_start = _native_u(underlying_start)
    curve_end = _native_u(underlying_end)
    edge_start = _native_u(trimmed_start)
    edge_end = _native_u(trimmed_end)
    if type(sense) is not bool:
        raise ValueError("edge sense must be bool")
    if is_periodic is not None and type(is_periodic) is not bool:
        raise ValueError("periodicity must be bool or None")
    if curve_start >= curve_end or edge_start >= edge_end:
        raise ValueError("curve and trimmed U domains must be strictly ordered")
    result = {
        "rule_version": NATIVE_CURVE_DOMAIN_RULE,
        "underlying_start": curve_start,
        "underlying_end": curve_end,
        "trimmed_start": edge_start,
        "trimmed_end": edge_end,
        "sense": sense,
        "direction": 1 if sense else -1,
        "is_periodic": is_periodic,
        "period": None,
        "shift_periods": 0,
        "lifted_start": None,
        "lifted_end": None,
        "seam_wrapped": False,
        "segments": [],
        "status": "unresolved",
        "domain_basis": "unresolved",
    }
    positive_start = edge_start if sense else -edge_end
    positive_end = edge_end if sense else -edge_start
    result.update(
        {
            "positive_start": positive_start,
            "positive_end": positive_end,
        }
    )
    if is_periodic is None:
        if native_u_in_closed_interval(
            positive_start,
            curve_start,
            curve_end,
        ) and native_u_in_closed_interval(
            positive_end,
            curve_start,
            curve_end,
        ):
            result.update(
                {
                    "lifted_start": positive_start,
                    "lifted_end": positive_end,
                    "segments": [[positive_start, positive_end]],
                    "status": "contained_periodicity_unknown",
                    "domain_basis": "underlying_curve_get_end_params",
                }
            )
        return result
    if is_periodic is False:
        result.update(
            {
                "lifted_start": edge_start,
                "lifted_end": edge_end,
                "segments": [[edge_start, edge_end]],
                "status": "edge_local_nonperiodic",
                "domain_basis": "edge_curve_params3",
            }
        )
        return result

    period = curve_end - curve_start
    length = positive_end - positive_start
    if length > period and not native_u_equal(length, period):
        return result
    shift = math.ceil((curve_start - positive_start) / period)
    lifted_start = positive_start + shift * period
    if lifted_start < curve_start and not native_u_equal(
        lifted_start,
        curve_start,
    ):
        shift += 1
        lifted_start = positive_start + shift * period
    lifted_end = positive_end + shift * period
    result.update(
        {
            "period": period,
            "shift_periods": shift,
            "lifted_start": lifted_start,
            "lifted_end": lifted_end,
        }
    )
    if lifted_end <= curve_end or native_u_equal(lifted_end, curve_end):
        result.update(
            {
                "segments": [[lifted_start, lifted_end]],
                "status": "contained",
                "domain_basis": "underlying_curve_get_end_params",
            }
        )
        return result
    wrapped_end = curve_start + (lifted_end - curve_end)
    if wrapped_end <= curve_end or native_u_equal(wrapped_end, curve_end):
        result.update(
            {
                "seam_wrapped": True,
                "segments": [
                    [lifted_start, curve_end],
                    [curve_start, wrapped_end],
                ],
                "status": "contained_seam_wrapped",
                "domain_basis": "underlying_curve_get_end_params",
            }
        )
    return result
