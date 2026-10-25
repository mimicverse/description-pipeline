"""Versioned native U, packed COM status, and binary32 round-trip rules."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from fractions import Fraction


def native_binary64_sqrt(value: Fraction, *, upward: bool = False) -> float:
    """Round an exact nonnegative square root without platform libm results.

    The integer square root locates adjacent binary64 values. Their exact
    rational midpoint selects round-to-nearest, ties-to-even; upward selects
    the least representable upper bound. Subnormal results use the same rule.
    """

    if value < 0:
        raise ValueError("a native squared quantity must be nonnegative")
    if value == 0:
        return 0.0
    numerator, denominator = value.numerator, value.denominator
    exponent = numerator.bit_length() - denominator.bit_length()
    if numerator < denominator << exponent if exponent >= 0 else numerator << -exponent < denominator:
        exponent -= 1
    quantum = max(exponent // 2 - 52, -1074)
    if quantum >= 0:
        scaled_numerator = numerator
        scaled_denominator = denominator << (2 * quantum)
        unit = Fraction(1 << quantum)
    else:
        scaled_numerator = numerator << (-2 * quantum)
        scaled_denominator = denominator
        unit = Fraction(1, 1 << -quantum)
    significand = math.isqrt(scaled_numerator // scaled_denominator)
    lower_exact = significand * unit
    lower = float(lower_exact)
    if lower_exact**2 == value:
        return lower
    upper = math.nextafter(lower, math.inf)
    if not math.isfinite(upper):
        raise ValueError("a native square root is outside binary64")
    if upward:
        return upper
    midpoint = (lower_exact + Fraction.from_float(upper)) / 2
    if value < midpoint**2:
        return lower
    if value > midpoint**2:
        return upper
    return lower if struct.unpack("<Q", struct.pack("<d", lower))[0] % 2 == 0 else upper


def native_binary64_unit_vector(vector) -> tuple[float, ...]:
    """Normalize exact binary64 coordinates with deterministic nearest rounding."""

    exact = tuple(Fraction.from_float(float(value)) for value in vector)
    squared_length = sum(value**2 for value in exact)
    if squared_length == 0:
        raise ValueError("a native direction must be nonzero")
    return tuple(math.copysign(native_binary64_sqrt(value**2 / squared_length), float(value)) for value in exact)


def native_binary64_sum_upper(values) -> float:
    """Return the least binary64 upper bound of a sum of binary64 bounds."""

    exact = sum(Fraction.from_float(value) for value in values)
    result = float(exact)
    return math.nextafter(result, math.inf) if Fraction.from_float(result) < exact else result


NATIVE_U_IDENTITY_RULE = "native-u-identity-128ulp-v1"
NATIVE_U_CLOSEST_POINT_PARAMETER_RULE = "native-u-closest-point-parameter-v1"
NATIVE_U_IDENTITY_ULPS = 128
NATIVE_U_COVERAGE_COVERED = "covered"
NATIVE_U_COVERAGE_UNCOVERED = "uncovered"
NATIVE_U_COVERAGE_UNRESOLVED = "unresolved"
NATIVE_POINT_ROUNDTRIP_RULE = "native-point-float32-pair-v3"
NATIVE_CURVE_ENDPOINT_OPERATION_RULE = (
    "native-curve-endpoint-float32-operation-v4"
)
NATIVE_CURVE_TOPOLOGY_ENDPOINT_SET_RULE = (
    "native-curve-topology-endpoint-set-v3"
)
# Retained as the frozen source-scope schema value; it grants no open-edge identity.
NATIVE_SOURCE_SCOPE_RULE = "native-open-edge-scope-binding-v1"
NATIVE_TESSELLATION_EDGE_PROJECTION_RULE = (
    "tessellation-edge-projection-v1"
)
NATIVE_TESSELLATION_EDGE_PROJECTION_METRIC = "max_abs_coordinate"
NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M = 50e-6
NATIVE_COLLAPSED_U_FIN_RULE = "native-fin-u-collapse-v1"
NATIVE_PERIODIC_FIN_INTERVAL_RULE = "native-periodic-fin-interval-v1"
NATIVE_CURVE_DOMAIN_RULE = "native-curve-domain-edge-local-v2"
NATIVE_U_RESOLUTION_RULE = "native-u-binary32-resolution-gate-v1"
NATIVE_SCOPE_IDENTITY_RULE = "native-scope-entity-id-v1"
NATIVE_PERSISTENT_IDENTITY_RULE = "native-persistent-reference-v1"
NATIVE_IDENTITY_POLICY_RULE = "native-reference-or-scope-identity-v1"
NATIVE_SCOPE_MAPPING_METHOD = (
    "direct_part_reference_witness+exact_native_area+"
    "exact_tessellation_bbox+bidirectional_one_to_one+topology_replay"
)
NATIVE_SCOPE_VERTEX_MAPPING_METHOD = (
    "direct_part_reference_witness+exact_native_point+"
    "coincident_ordinal+topology_incidence_replay"
)
NATIVE_SCOPE_SOURCE_AUTHORITY = (
    "frozen_part_persistent_reference_witness"
)
NATIVE_FACE_IDENTITY_KEY_RULE = "native-face-area-bbox-exact-v1"
NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED = "cad_scope_local_identity_derived"
NATIVE_COMPONENT_REFERENCE_ALIASED = "cad_component_persistent_reference_aliased"
NATIVE_PART_WITNESS_DERIVED = "cad_part_identity_witness_derived"
NATIVE_TOPOLOGY_IDENTITY_DERIVED = "cad_topology_identity_derived"
NATIVE_FACE_FACET_IDS_DERIVED = (
    "cad_face_facet_ids_derived_by_body_global_closure"
)
NATIVE_EDGE_FIN_IDS_DERIVED = (
    "cad_edge_fin_ids_derived_by_empty_incident_faces"
)
NATIVE_PLANAR_BREP_BOUNDARY = "cad_edge_fin_ids_not_applicable_for_planar_brep"
NATIVE_PLANAR_BREP_UNIT_NORMAL_DERIVED = "cad_planar_brep_unit_normal_derived"
NATIVE_PLANAR_BREP_TRIANGULATION_DERIVED = "cad_planar_brep_triangulation_derived"
NATIVE_SCOPE_IDENTITY_WARNING_CODES = frozenset(
    {
        NATIVE_COMPONENT_REFERENCE_ALIASED,
        NATIVE_PART_WITNESS_DERIVED,
        NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
        NATIVE_TOPOLOGY_IDENTITY_DERIVED,
    }
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
NATIVE_SCOPE_WARNING_DETAIL_FIELDS = frozenset(
    {
        "component",
        "entity_kind",
        "body_type",
        "body_index",
        "owner_scope_key",
        "persistent",
        "rule_version",
        "object_indices",
        "field",
        "native_reference_sha256",
        "source_document_sha256",
        "referenced_configuration",
        "occurrence_scope",
        "body_name",
        "mapping_source_part_document_sha256",
        "mapping_source_body_reference_sha256",
        "mapped_source_indices",
        "mapped_source_persistent_reference_sha256",
        "topology_signature_sha256s",
    }
)


def native_scope_warning_detail_is_valid(detail: object) -> bool:
    """Whether one projected native-scope warning fully locates its object."""

    return (
        isinstance(detail, dict)
        and set(detail) == NATIVE_SCOPE_WARNING_DETAIL_FIELDS
        and isinstance(detail.get("component"), str)
        and bool(detail["component"])
        and detail.get("entity_kind") in {"face", "edge", "vertex"}
        and type(detail.get("body_type")) is int
        and detail["body_type"] in {0, 1}
        and type(detail.get("body_index")) is int
        and detail["body_index"] >= 0
        and isinstance(detail.get("owner_scope_key"), str)
        and bool(_HEX64.fullmatch(detail["owner_scope_key"]))
        and detail.get("persistent") is False
        and detail.get("rule_version") == NATIVE_SCOPE_IDENTITY_RULE
        and isinstance(detail.get("object_indices"), list)
        and bool(detail["object_indices"])
        and detail["object_indices"]
        == sorted(set(detail["object_indices"]))
        and all(
            type(index) is int and index >= 0
            for index in detail["object_indices"]
        )
        and detail.get("field") == "identity.warnings"
        and isinstance(detail.get("native_reference_sha256"), str)
        and bool(_HEX64.fullmatch(detail["native_reference_sha256"]))
        and isinstance(detail.get("source_document_sha256"), str)
        and bool(_HEX64.fullmatch(detail["source_document_sha256"]))
        and (
            detail.get("referenced_configuration") is None
            or isinstance(detail["referenced_configuration"], str)
        )
        and (
            detail.get("occurrence_scope") is None
            or isinstance(detail["occurrence_scope"], str)
        )
        and (
            detail.get("body_name") is None
            or isinstance(detail["body_name"], str)
        )
        and (
            detail["entity_kind"] != "face"
            or (
                isinstance(detail["referenced_configuration"], str)
                and bool(detail["referenced_configuration"])
                and isinstance(detail["occurrence_scope"], str)
                and detail["occurrence_scope"] == detail["component"]
                and isinstance(detail["body_name"], str)
                and bool(detail["body_name"])
            )
        )
        and isinstance(
            detail.get("mapping_source_part_document_sha256"),
            str,
        )
        and bool(
            _HEX64.fullmatch(
                detail["mapping_source_part_document_sha256"]
            )
        )
        and isinstance(
            detail.get("mapping_source_body_reference_sha256"),
            str,
        )
        and bool(
            _HEX64.fullmatch(
                detail["mapping_source_body_reference_sha256"]
            )
        )
        and isinstance(detail.get("mapped_source_indices"), list)
        and bool(detail["mapped_source_indices"])
        and detail["mapped_source_indices"]
        == sorted(set(detail["mapped_source_indices"]))
        and all(
            type(index) is int and index >= 0
            for index in detail["mapped_source_indices"]
        )
        and isinstance(
            detail.get("mapped_source_persistent_reference_sha256"),
            list,
        )
        and bool(
            detail["mapped_source_persistent_reference_sha256"]
        )
        and detail[
            "mapped_source_persistent_reference_sha256"
        ]
        == sorted(
            set(
                detail[
                    "mapped_source_persistent_reference_sha256"
                ]
            )
        )
        and all(
            isinstance(reference, str)
            and bool(_HEX64.fullmatch(reference))
            for reference in detail[
                "mapped_source_persistent_reference_sha256"
            ]
        )
        and isinstance(
            detail.get("topology_signature_sha256s"),
            list,
        )
        and bool(detail["topology_signature_sha256s"])
        and detail["topology_signature_sha256s"]
        == sorted(set(detail["topology_signature_sha256s"]))
        and all(
            isinstance(reference, str)
            and bool(_HEX64.fullmatch(reference))
            for reference in detail["topology_signature_sha256s"]
        )
        and len(detail["object_indices"])
        == len(detail["mapped_source_indices"])
        == len(detail["mapped_source_persistent_reference_sha256"])
        == len(detail["topology_signature_sha256s"])
    )


def native_scope_warning_diagnostic_is_valid(item: object) -> bool:
    """Whether one quality diagnostic is a complete non-blocking scope warning."""

    return (
        isinstance(item, dict)
        and item.get("code") in NATIVE_SCOPE_IDENTITY_WARNING_CODES
        and item.get("blocking") is False
        and item.get("severity") == "warning"
        and isinstance(item.get("message"), str)
        and bool(item["message"].strip())
        and isinstance(item.get("object"), str)
        and native_scope_warning_detail_is_valid(item.get("detail"))
        and item["object"]
        == (
            f"{item['detail']['component']}:"
            f"{item['detail']['entity_kind']}:"
            f"{item['detail']['body_type']}/"
            f"{item['detail']['body_index']}"
        )
    )


def native_scope_warning_projection(item: dict) -> dict:
    """Project one raw native warning without losing its full object location."""

    detail = item.get("detail")
    if not isinstance(detail, dict):
        detail = {
            key: value
            for key, value in item.items()
            if key
            not in {
                "code",
                "blocking",
                "severity",
                "message",
                "object",
                "detail",
            }
        }
    return {
        "code": str(item["code"]),
        "blocking": item.get("blocking", False),
        "severity": item.get("severity", "warning"),
        "message": str(
            item.get("message")
            or f"Native tessellation recorded {item['code']}"
        ),
        "object": str(
            item.get("object")
            or detail.get("component")
            or detail.get("document")
            or ""
        ),
        "detail": detail,
    }


def native_scope_warning_diagnostics_are_complete(
    items: list[dict],
) -> bool:
    """Require all four warnings for every component/scope/object group."""

    groups: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not native_scope_warning_diagnostic_is_valid(item):
            return False
        detail = item["detail"]
        group = json.dumps(
            detail,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        marker = (group, item["code"])
        if marker in seen:
            return False
        seen.add(marker)
        groups.setdefault(group, []).append(item)
    return all(
        {item["code"] for item in group}
        == NATIVE_SCOPE_IDENTITY_WARNING_CODES
        and len(group) == len(NATIVE_SCOPE_IDENTITY_WARNING_CODES)
        for group in groups.values()
    )


def native_scope_warning_details_match(
    diagnostics: list[dict],
    raw_warnings: list[dict],
) -> bool:
    """Require diagnostics to reproduce the gate rows exactly, group by group."""

    raw_scope = [
        native_scope_warning_projection(item)
        for item in raw_warnings
        if isinstance(item, dict)
        and item.get("code") in NATIVE_SCOPE_IDENTITY_WARNING_CODES
    ]
    if not native_scope_warning_diagnostics_are_complete(raw_scope):
        return False
    if not native_scope_warning_diagnostics_are_complete(diagnostics):
        return False

    def comparable(items):
        return sorted(
            (
                item["code"],
                json.dumps(
                    item["detail"],
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                item["object"],
                item["severity"],
                item["message"],
            )
            for item in items
        )

    return comparable(raw_scope) == comparable(diagnostics)


def native_scope_identity_sha256(value: dict) -> str:
    """Hash a canonical, JSON-safe scope-local identity record."""

    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


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


def native_fin_interval(
    start: float | int,
    end: float | int,
    lower: float | int,
    upper: float | int,
    *,
    is_periodic: bool,
    is_closed: bool,
) -> list[tuple[float, float]]:
    """Return the native fin interval without confusing a seam segment for its complement."""

    start_value = _native_u(start)
    end_value = _native_u(end)
    lower_value = _native_u(lower)
    upper_value = _native_u(upper)
    if (
        lower_value >= upper_value
        or not native_u_in_closed_interval(start_value, lower_value, upper_value)
        or not native_u_in_closed_interval(end_value, lower_value, upper_value)
    ):
        raise ValueError("fin endpoints must lie inside a non-empty edge domain")
    direct = (min(start_value, end_value), max(start_value, end_value))
    if not (is_periodic and is_closed):
        return [direct]
    period = upper_value - lower_value
    direct_width = direct[1] - direct[0]
    complement_width = period - direct_width
    if native_u_equal(direct_width, complement_width):
        raise ValueError(
            "periodic fin interval is ambiguous at half the edge domain"
        )
    if direct_width < complement_width:
        return [direct]
    return [
        (direct[1], upper_value),
        (lower_value, direct[0]),
    ]


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


def native_point_within_max_coordinate_bound(
    left,
    right,
    bound_m,
) -> bool:
    """Compare max-coordinate distance using exact binary64 differences."""

    if len(left) != 3 or len(right) != 3:
        raise ValueError("points must contain three coordinates")
    if type(bound_m) not in (int, float):
        raise ValueError("coordinate bound must be a JSON number")
    bound = float(bound_m)
    if not math.isfinite(bound) or bound < 0.0:
        raise ValueError("coordinate bound must be finite and non-negative")
    bound_fraction = Fraction.from_float(bound)
    for left_value, right_value in zip(left, right, strict=True):
        left_float = _native_u(left_value)
        right_float = _native_u(right_value)
        difference = abs(
            Fraction.from_float(left_float)
            - Fraction.from_float(right_float)
        )
        if difference > bound_fraction:
            return False
    return True


def native_point_roundtrip_tolerance(*points) -> float:
    """Bound the largest coordinate-wise roundtrip between any two XYZ points."""

    if not points:
        raise ValueError("at least one point is required")
    for point in points:
        if len(point) != 3:
            raise ValueError("points must contain three coordinates")
    if len(points) == 1:
        return max(
            native_float32_adjacency_bound(coordinate)
            for coordinate in points[0]
        )
    return max(
        max(
            native_float32_adjacency_bound(left[index])
            + native_float32_adjacency_bound(right[index])
            for index in range(3)
        )
        for left_index, left in enumerate(points)
        for right in points[left_index + 1 :]
    )


def native_curve_endpoint_operation_tolerance(*points) -> float:
    """Bound one evaluated endpoint against its peer-defined native curve."""

    if len(points) != 3:
        raise ValueError("curve endpoint operation requires three points")
    if any(len(point) != 3 for point in points):
        raise ValueError("points must contain three coordinates")
    endpoint, evaluated, opposite = points
    return (
        native_point_roundtrip_tolerance(endpoint, evaluated)
        + native_point_roundtrip_tolerance(endpoint, opposite)
    )


SOURCE_SCOPE_FIELDS = frozenset(
    {
        "rule_version",
        "component",
        "source_document_path",
        "source_copy_path",
        "source_document_sha256",
        "source_copy_sha256",
        "source_document_identity_sha256",
        "source_document_identity_kind",
        "referenced_configuration",
        "occurrence_scope",
        "virtual_owner",
        "virtual_owner_sha256",
        "virtual_session_document_path",
        "virtual_session_temp_root",
        "virtual_native_api_pid",
    }
)


def _virtual_component_relative_path(value: object) -> str | None:
    """Return the exact VC~~ subtree without binding one session root."""

    if not isinstance(value, str):
        return None
    text = value.replace("\\", "/")
    marker = "/vc~~/"
    position = text.casefold().find(marker)
    if position < 0:
        return None
    relative = text[position + 1 :]
    if not relative or any(
        part in {"", ".", ".."} for part in relative.split("/")
    ):
        return None
    return relative.casefold()


def native_source_scope_is_valid(
    value: object,
    component: str | None = None,
) -> bool:
    """Validate one sealed source scope without trusting tessellation scope."""

    if not isinstance(value, dict) or set(value) != SOURCE_SCOPE_FIELDS:
        return False
    component_name = value["component"]
    path = value["source_document_path"]
    path_is_text = isinstance(path, str) and bool(path)
    copy_path = value["source_copy_path"]
    session_path = value["virtual_session_document_path"]
    path_virtual_relative = _virtual_component_relative_path(path)
    session_virtual_relative = _virtual_component_relative_path(session_path)
    file_identity = (
        value["source_document_identity_kind"] == "file_sha256"
        and isinstance(value["source_document_sha256"], str)
        and bool(_HEX64.fullmatch(value["source_document_sha256"]))
        and value["source_document_sha256"]
        == value["source_document_identity_sha256"]
        and isinstance(value["source_copy_sha256"], str)
        and bool(_HEX64.fullmatch(value["source_copy_sha256"]))
        and isinstance(copy_path, str)
        and copy_path.startswith("source/")
        and "\\" not in copy_path
        and all(
            part not in {"", ".", ".."}
            for part in copy_path.split("/")
        )
        and value["virtual_owner"] is None
        and value["virtual_owner_sha256"] is None
        and value["virtual_session_document_path"] is None
        and value["virtual_session_temp_root"] is None
        and value["virtual_native_api_pid"] is None
    )
    virtual_identity = (
        value["source_document_identity_kind"] == "virtual_path_sha256"
        and copy_path is None
        and value["source_document_sha256"] is None
        and value["source_copy_sha256"] is None
        and path_is_text
        and value["source_document_identity_sha256"]
        == hashlib.sha256(path.encode("utf-8")).hexdigest()
        and isinstance(value["virtual_owner"], str)
        and bool(value["virtual_owner"])
        and isinstance(value["virtual_owner_sha256"], str)
        and bool(_HEX64.fullmatch(value["virtual_owner_sha256"]))
        and path_virtual_relative is not None
        and session_virtual_relative == path_virtual_relative
        and isinstance(value["virtual_session_temp_root"], str)
        and bool(value["virtual_session_temp_root"])
        and type(value["virtual_native_api_pid"]) is int
        and value["virtual_native_api_pid"] > 0
    )
    return (
        value["rule_version"] == NATIVE_SOURCE_SCOPE_RULE
        and isinstance(component_name, str)
        and bool(component_name)
        and (component is None or component_name == component)
        and value["occurrence_scope"] == component_name
        and path_is_text
        and isinstance(value["source_document_identity_sha256"], str)
        and bool(_HEX64.fullmatch(value["source_document_identity_sha256"]))
        and (file_identity or virtual_identity)
        and isinstance(value["referenced_configuration"], str)
        and bool(value["referenced_configuration"])
    )


def native_curve_topology_endpoint_set_assignment(
    expected_start,
    expected_end,
    actual_start,
    actual_end,
) -> dict:
    """Match unordered native endpoint sets without assigning edge direction."""

    points = (expected_start, expected_end, actual_start, actual_end)
    if any(
        not isinstance(point, (list, tuple))
        or len(point) != 3
        for point in points
    ):
        raise ValueError("topology endpoint points must contain three coordinates")
    expected_start = tuple(_native_u(value) for value in expected_start)
    expected_end = tuple(_native_u(value) for value in expected_end)
    actual_start = tuple(_native_u(value) for value in actual_start)
    actual_end = tuple(_native_u(value) for value in actual_end)
    direct_error = max(
        native_point_error(expected_start, actual_start),
        native_point_error(expected_end, actual_end),
    )
    swapped_error = max(
        native_point_error(expected_start, actual_end),
        native_point_error(expected_end, actual_start),
    )
    tolerance = NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
    direct_complete = (
        native_point_within_max_coordinate_bound(
            expected_start,
            actual_start,
            tolerance,
        )
        and native_point_within_max_coordinate_bound(
            expected_end,
            actual_end,
            tolerance,
        )
    )
    swapped_complete = (
        native_point_within_max_coordinate_bound(
            expected_start,
            actual_end,
            tolerance,
        )
        and native_point_within_max_coordinate_bound(
            expected_end,
            actual_start,
            tolerance,
        )
    )
    matching_count = int(direct_complete) + int(swapped_complete)
    distinct_endpoints = (
        expected_start != expected_end
        and actual_start != actual_end
    )
    accepted = distinct_endpoints and matching_count >= 1
    if direct_complete and not swapped_complete:
        canonical_result = "direct"
        canonical_error = direct_error
        alternative_error = swapped_error
    elif swapped_complete and not direct_complete:
        canonical_result = "swapped"
        canonical_error = swapped_error
        alternative_error = direct_error
    elif direct_complete and swapped_complete:
        canonical_result = "direct"
        canonical_error = min(direct_error, swapped_error)
        alternative_error = abs(swapped_error - direct_error)
    else:
        canonical_result = None
        canonical_error = None
        alternative_error = None
    record = {
        "rule_version": NATIVE_CURVE_TOPOLOGY_ENDPOINT_SET_RULE,
        "metric": "max_abs_coordinate",
        "tolerance_m": tolerance,
        "expected_start_point_m": list(expected_start),
        "expected_end_point_m": list(expected_end),
        "actual_start_point_m": list(actual_start),
        "actual_end_point_m": list(actual_end),
        "direct_error_m": direct_error,
        "swapped_error_m": swapped_error,
        "direct_complete": direct_complete,
        "swapped_complete": swapped_complete,
        "complete_match_count": matching_count,
        "canonical_result": canonical_result,
        "canonical_error_m": canonical_error,
        "alternative_margin_m": alternative_error,
        "distinct_endpoint_sets": distinct_endpoints,
        "accepted": accepted,
    }
    return record


def native_curve_parameter_endpoints(
    curve_start: tuple[float | int, float | int, float | int],
    curve_end: tuple[float | int, float | int, float | int],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return ICurveParamData endpoints in raw parameter order."""

    points = []
    for point in (curve_start, curve_end):
        if len(point) != 3:
            raise ValueError("curve endpoints must contain three coordinates")
        points.append(tuple(_native_u(value) for value in point))
    return points[0], points[1]


def native_curve_domain_transform(
    underlying_start: float | int,
    underlying_end: float | int,
    trimmed_start: float | int,
    trimmed_end: float | int,
    sense: bool,
    is_periodic: bool | None,
) -> dict:
    """Classify an edge U domain against underlying-curve periodicity.

    ``Sense`` declares whether the curve and edge share direction.  It also
    orients the periodic parameter lift.  Raw ``UMin``/``StartPoint`` and
    ``UMax``/``EndPoint`` remain the native parameter endpoints; the unordered
    endpoint set is an independent coverage gate, not the direction authority.
    """

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
