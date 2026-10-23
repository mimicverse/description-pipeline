"""Versioned native U, packed COM status, and binary32 round-trip rules."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from fractions import Fraction

NATIVE_U_IDENTITY_RULE = "native-u-identity-128ulp-v1"
NATIVE_U_POINT_PARAMETER_RULE = "native-u-point-parameter-authoritative-v2"
NATIVE_U_CLOSEST_FALLBACK_RULE = "native-u-closest-point-fallback-v1"
NATIVE_U_IDENTITY_ULPS = 128
NATIVE_U_COVERAGE_COVERED = "covered"
NATIVE_U_COVERAGE_UNCOVERED = "uncovered"
NATIVE_U_COVERAGE_UNRESOLVED = "unresolved"
NATIVE_POINT_ROUNDTRIP_RULE = "native-point-float32-pair-v3"
NATIVE_CURVE_ENDPOINT_OPERATION_RULE = (
    "native-curve-endpoint-float32-operation-v4"
)
NATIVE_TOLERANT_EDGE_BOUNDARY_RULE = (
    "native-tolerant-edge-boundary-euclidean-v5"
)
NATIVE_CURVE_TOPOLOGY_ENDPOINT_SET_RULE = (
    "native-curve-topology-endpoint-set-v2"
)
NATIVE_OPEN_EDGE_BOUNDARY_IDENTITY_RULE = (
    "native-open-edge-boundary-identity-v2"
)
NATIVE_OPEN_EDGE_SCOPE_RULE = "native-open-edge-scope-binding-v1"
NATIVE_TESSELLATION_EDGE_PROJECTION_RULE = (
    "tessellation-edge-projection-v1"
)
NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M = 50e-6
NATIVE_TOLERANT_EDGE_GAP_SOURCE = (
    "IEdge.IsTolerant(out VARIANT VT_BYREF|VT_R8)"
)
NATIVE_TOLERANT_EDGE_GAP_SOURCE_FIELD = (
    "native_edges[].edge_tolerance.gap_m"
)
NATIVE_TOLERANT_EDGE_EVALUATE_METRIC = "max_abs_coordinate"
NATIVE_TOLERANT_EDGE_PROJECTION_METRIC = "euclidean"
NATIVE_TOLERANT_EDGE_WARNING_REASON = (
    "tolerant boundary projection exceeds the binary32 v4 bound "
    "and uses the native gap"
)
NATIVE_TOLERANT_EDGE_WARNING_QUALIFICATION = (
    "native_declared_tolerant_edge;euclidean_projected_endpoint_bound"
)
NATIVE_TOLERANT_EDGE_AFFECTED_FIELDS = (
    "point_tolerance_m",
    "point_error_m",
    "point_roundtrip_rule",
    "tolerant_edge_evidence.evaluate_point_error_m",
    "projection_to_curve_l2_error_m",
    "eligible_gap_m",
    "composed_projection_tolerance_m",
    "gap_used",
)
NATIVE_TOLERANT_EDGE_GAP_WARNING = (
    "cad_native_tolerant_edge_boundary_projection"
)
NATIVE_TOLERANT_EDGE_WARNING_REQUIRED_FIELDS = frozenset(
    {
        "component",
        "body_type",
        "body_index",
        "edge_reference_sha256",
        "fin_id",
        "fin_endpoint",
        "parameter_boundary",
        "point_roundtrip_rule",
        "evaluate_rule_version",
        "evaluate_metric",
        "evaluate_point_error_m",
        "point_tolerance_m",
        "projection_metric",
        "projection_to_curve_l2_error_m",
        "eligible_gap_m",
        "gap_used",
        "composed_projection_tolerance_m",
        "gap_source_api",
        "gap_cap_m",
        "edge_tolerance_out_variant_type",
        "field",
    }
)
NATIVE_TOLERANT_EDGE_WARNING_DETAIL_FIELDS = (
    NATIVE_TOLERANT_EDGE_WARNING_REQUIRED_FIELDS
    | {"affected_fields", "reason", "qualification"}
)
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


def native_tolerant_edge_warning_is_valid(item: object) -> bool:
    """Whether one projected v5 warning retains complete field provenance."""

    if not isinstance(item, dict) or set(item) != {
        "code",
        "blocking",
        "severity",
        "message",
        "object",
        "detail",
    }:
        return False
    detail = item.get("detail")
    if (
        not isinstance(detail, dict)
        or set(detail) != NATIVE_TOLERANT_EDGE_WARNING_DETAIL_FIELDS
    ):
        return False
    numeric_fields = (
        "evaluate_point_error_m",
        "point_tolerance_m",
        "projection_to_curve_l2_error_m",
        "eligible_gap_m",
        "composed_projection_tolerance_m",
        "gap_cap_m",
    )
    return (
        item["code"] == NATIVE_TOLERANT_EDGE_GAP_WARNING
        and item["blocking"] is False
        and item["severity"] == "warning"
        and isinstance(item["message"], str)
        and bool(item["message"].strip())
        and item["object"] == detail["component"]
        and isinstance(detail["component"], str)
        and bool(detail["component"])
        and type(detail["body_type"]) is int
        and detail["body_type"] in (0, 1)
        and type(detail["body_index"]) is int
        and detail["body_index"] >= 0
        and isinstance(detail["edge_reference_sha256"], str)
        and bool(_HEX64.fullmatch(detail["edge_reference_sha256"]))
        and type(detail["fin_id"]) is int
        and detail["fin_id"] >= 0
        and detail["fin_endpoint"] in {"start", "end"}
        and detail["parameter_boundary"] in {"u_min", "u_max"}
        and detail["point_roundtrip_rule"]
        == NATIVE_TOLERANT_EDGE_BOUNDARY_RULE
        and detail["evaluate_rule_version"]
        == NATIVE_CURVE_ENDPOINT_OPERATION_RULE
        and detail["evaluate_metric"] == NATIVE_TOLERANT_EDGE_EVALUATE_METRIC
        and detail["projection_metric"] == NATIVE_TOLERANT_EDGE_PROJECTION_METRIC
        and all(
            type(detail[field]) in (int, float)
            and math.isfinite(float(detail[field]))
            and detail[field] >= 0.0
            for field in numeric_fields
        )
        and detail["evaluate_point_error_m"]
        <= detail["point_tolerance_m"]
        and detail["eligible_gap_m"]
        <= NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
        and detail["projection_to_curve_l2_error_m"]
        <= detail["composed_projection_tolerance_m"]
        and detail["gap_used"] is True
        and detail["gap_source_api"] == NATIVE_TOLERANT_EDGE_GAP_SOURCE
        and detail["gap_cap_m"]
        == NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
        and type(detail["edge_tolerance_out_variant_type"]) is int
        and detail["edge_tolerance_out_variant_type"] == 0x4005
        and isinstance(detail["field"], str)
        and detail["field"]
        == f"{detail['fin_endpoint']}_evidence.tolerant_edge_evidence"
        and detail["affected_fields"] == list(
            NATIVE_TOLERANT_EDGE_AFFECTED_FIELDS
        )
        and isinstance(detail["reason"], str)
        and bool(detail["reason"].strip())
        and detail["reason"] == NATIVE_TOLERANT_EDGE_WARNING_REASON
        and detail["qualification"]
        == NATIVE_TOLERANT_EDGE_WARNING_QUALIFICATION
    )


def native_tolerant_edge_warning_details_match(
    diagnostics: list[dict],
    raw_warnings: list[dict],
) -> bool:
    """Require quality diagnostics to reproduce every v5 warning exactly."""

    def comparable(items):
        return sorted(
            json.dumps(
                item,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            for item in items
        )

    projected = [
        native_scope_warning_projection(item)
        for item in raw_warnings
        if isinstance(item, dict)
        and item.get("code") == NATIVE_TOLERANT_EDGE_GAP_WARNING
    ]
    v5_diagnostics = [
        item
        for item in diagnostics
        if isinstance(item, dict)
        and item.get("code") == NATIVE_TOLERANT_EDGE_GAP_WARNING
    ]
    def identities(items):
        return [
            (
                item["detail"]["component"],
                item["detail"]["body_type"],
                item["detail"]["body_index"],
                item["detail"]["edge_reference_sha256"],
                item["detail"]["fin_id"],
                item["detail"]["fin_endpoint"],
                item["detail"]["parameter_boundary"],
                item["detail"]["field"],
            )
            for item in items
        ]

    return (
        all(native_tolerant_edge_warning_is_valid(item) for item in projected)
        and all(
            native_tolerant_edge_warning_is_valid(item)
            for item in v5_diagnostics
        )
        and len(identities(projected)) == len(set(identities(projected)))
        and len(identities(v5_diagnostics))
        == len(set(identities(v5_diagnostics)))
        and comparable(projected) == comparable(v5_diagnostics)
    )


def native_tolerant_edge_warning_set_matches(
    diagnostics: list[dict],
    raw_warnings: list[dict],
) -> bool:
    """Validate the paired tolerant warning sets without consumer-specific filters."""

    tolerant_diagnostics = [
        item
        for item in diagnostics
        if isinstance(item, dict)
        and item.get("code") == NATIVE_TOLERANT_EDGE_GAP_WARNING
    ]
    tolerant_raw = [
        item
        for item in raw_warnings
        if isinstance(item, dict)
        and item.get("code") == NATIVE_TOLERANT_EDGE_GAP_WARNING
    ]
    if not tolerant_diagnostics and not tolerant_raw:
        return True
    return native_tolerant_edge_warning_details_match(
        tolerant_diagnostics,
        tolerant_raw,
    )


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


def native_boolean_success(value: object) -> bool | None:
    """Normalize a COM boolean-like 0/1 result without accepting other values."""

    if type(value) is bool:
        return value
    if type(value) in (int, float) and value in (0, 1):
        return bool(value)
    return None


def native_get_parameter_success(value: object) -> bool | None:
    """Parse GetParameter's flag or packed double-long status strictly."""

    boolean = native_boolean_success(value)
    if boolean is not None:
        return boolean
    if type(value) is not float or not math.isfinite(value):
        return None
    bits = struct.unpack("<Q", struct.pack("<d", value))[0]
    low32 = bits & 0xFFFFFFFF
    if value == 0.0:
        return False
    if low32 == 1:
        return True
    return None


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


def native_point_euclidean_error(
    left: tuple[float | int, float | int, float | int],
    right: tuple[float | int, float | int, float | int],
) -> float:
    """Return the Euclidean XYZ distance between two finite native points."""

    if len(left) != 3 or len(right) != 3:
        raise ValueError("points must contain three coordinates")
    values = [
        _native_u(left[index]) - _native_u(right[index])
        for index in range(3)
    ]
    if not all(map(math.isfinite, values)):
        raise ValueError("points must contain finite coordinates")
    return math.hypot(*values)


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


def native_point_within_euclidean_bound(left, right, bound_m) -> bool:
    """Compare one Euclidean distance to an outward binary64 bound exactly."""

    if len(left) != 3 or len(right) != 3:
        raise ValueError("points must contain three coordinates")
    if type(bound_m) not in (int, float):
        raise ValueError("Euclidean bound must be a JSON number")
    bound = float(bound_m)
    if not math.isfinite(bound) or bound < 0.0:
        raise ValueError("Euclidean bound must be finite and non-negative")
    squared = Fraction(0)
    for left_value, right_value in zip(left, right, strict=True):
        left_float = _native_u(left_value)
        right_float = _native_u(right_value)
        if not math.isfinite(left_float) or not math.isfinite(right_float):
            raise ValueError("points must contain finite coordinates")
        delta = Fraction.from_float(left_float) - Fraction.from_float(
            right_float
        )
        squared += delta * delta
    return squared <= Fraction.from_float(bound) ** 2


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


def native_tolerant_edge_gap(gap_m: float | int) -> float:
    """Validate the source-bound tolerant-edge gap against the global bound."""

    if type(gap_m) is not float:
        raise ValueError("tolerant edge gap must be a float")
    gap = float(gap_m)
    if (
        not math.isfinite(gap)
        or gap < 0.0
        or gap > NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
    ):
        raise ValueError(
            "tolerant edge gap must be finite and within the 50 micrometre bound"
        )
    return gap


def native_tolerant_edge_boundary(
    projected_point,
    evaluated_point,
    curve_endpoint,
    opposite_curve_endpoint,
    *,
    is_tolerant,
    gap_m,
    topology_accepted,
) -> dict:
    """Evaluate one successful boundary fin with separate v4 and v5 metrics."""

    if type(is_tolerant) is not bool:
        raise ValueError("tolerant edge state must be a strict boolean")
    if type(topology_accepted) is not bool:
        raise ValueError("topology acceptance must be a strict boolean")
    gap = native_tolerant_edge_gap(gap_m)
    points = (
        projected_point,
        evaluated_point,
        curve_endpoint,
        opposite_curve_endpoint,
    )
    if any(len(point) != 3 for point in points):
        raise ValueError("points must contain three coordinates")
    curve_tolerance = native_curve_endpoint_operation_tolerance(
        curve_endpoint,
        evaluated_point,
        opposite_curve_endpoint,
    )
    curve_error = native_point_error(curve_endpoint, evaluated_point)
    projected_error = native_point_euclidean_error(
        projected_point,
        curve_endpoint,
    )
    projected_tolerance = math.nextafter(
        curve_tolerance + gap,
        math.inf,
    )
    ordinary_projection_closes = native_point_within_euclidean_bound(
        projected_point,
        curve_endpoint,
        curve_tolerance,
    )
    projected_within = native_point_within_euclidean_bound(
        projected_point,
        curve_endpoint,
        projected_tolerance,
    )
    gap_used = (
        not ordinary_projection_closes
        and curve_error <= curve_tolerance
        and is_tolerant
        and topology_accepted
        and projected_within
    )
    accepted = curve_error <= curve_tolerance and (
        ordinary_projection_closes or gap_used
    )
    return {
        "point_roundtrip_rule": NATIVE_TOLERANT_EDGE_BOUNDARY_RULE,
        "evaluate_rule_version": NATIVE_CURVE_ENDPOINT_OPERATION_RULE,
        "evaluate_metric": NATIVE_TOLERANT_EDGE_EVALUATE_METRIC,
        "evaluate_point_error_m": curve_error,
        "point_tolerance_m": curve_tolerance,
        "projection_metric": NATIVE_TOLERANT_EDGE_PROJECTION_METRIC,
        "ordinary_projection_closes": ordinary_projection_closes,
        "projection_to_curve_l2_error_m": projected_error,
        "composed_projection_tolerance_m": projected_tolerance,
        "gap_used": gap_used,
        "accepted": accepted,
    }


def native_tolerant_edge_evidence(
    metrics: dict,
    *,
    edge_reference_sha256: str,
    parameter_boundary: str,
    eligible_gap_m: float,
) -> dict:
    """Format one canonical v5 evidence row from independently computed metrics."""

    return {
        "point_roundtrip_rule": metrics["point_roundtrip_rule"],
        "evaluate_rule_version": metrics["evaluate_rule_version"],
        "evaluate_metric": metrics["evaluate_metric"],
        "projection_metric": metrics["projection_metric"],
        "edge_reference_sha256": edge_reference_sha256,
        "parameter_boundary": parameter_boundary,
        "gap_used": metrics["gap_used"],
        "eligible_gap_m": eligible_gap_m,
        "gap_source_field": NATIVE_TOLERANT_EDGE_GAP_SOURCE_FIELD,
        "evaluate_point_error_m": metrics["evaluate_point_error_m"],
        "point_tolerance_m": metrics["point_tolerance_m"],
        "projection_to_curve_l2_error_m": metrics[
            "projection_to_curve_l2_error_m"
        ],
        "composed_projection_tolerance_m": metrics[
            "composed_projection_tolerance_m"
        ],
    }


OPEN_EDGE_SCOPE_FIELDS = frozenset(
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
        "body_type",
        "body_index",
        "body_reference_sha256",
        "native_body_reference_sha256",
        "body_identity_sha256",
        "face_index",
        "face_reference_sha256",
        "native_face_reference_sha256",
        "face_identity_mode",
        "face_identity_sha256",
        "owner_scope_key",
    }
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
        value["rule_version"] == NATIVE_OPEN_EDGE_SCOPE_RULE
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


def _valid_open_edge_scope_binding(value: object) -> bool:
    """Whether one scope record binds the exact component, body, and face."""

    if not isinstance(value, dict) or set(value) != OPEN_EDGE_SCOPE_FIELDS:
        return False
    hashes = (
        "source_document_identity_sha256",
        "body_reference_sha256",
        "native_body_reference_sha256",
        "body_identity_sha256",
        "face_reference_sha256",
        "native_face_reference_sha256",
        "face_identity_sha256",
    )
    path_is_text = (
        isinstance(value["source_document_path"], str)
        and bool(value["source_document_path"])
    )
    copy_path = value["source_copy_path"]
    path_virtual_relative = _virtual_component_relative_path(
        value["source_document_path"]
    )
    session_virtual_relative = _virtual_component_relative_path(
        value["virtual_session_document_path"]
    )
    return (
        value["rule_version"] == NATIVE_OPEN_EDGE_SCOPE_RULE
        and isinstance(value["component"], str)
        and bool(value["component"])
        and path_is_text
        and all(
            isinstance(value[field], str)
            and bool(_HEX64.fullmatch(value[field]))
            for field in hashes
        )
        and (
            (
                value["source_document_identity_kind"] == "file_sha256"
                and isinstance(value["source_document_sha256"], str)
                and bool(
                    _HEX64.fullmatch(value["source_document_sha256"])
                )
                and value["source_document_sha256"]
                == value["source_document_identity_sha256"]
                and isinstance(value["source_copy_sha256"], str)
                and bool(
                    _HEX64.fullmatch(value["source_copy_sha256"])
                )
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
            or (
                value["source_document_identity_kind"]
                == "virtual_path_sha256"
                and copy_path is None
                and value["source_document_sha256"] is None
                and value["source_copy_sha256"] is None
                and path_is_text
                and value["source_document_identity_sha256"]
                == hashlib.sha256(
                    value["source_document_path"].encode("utf-8")
                ).hexdigest()
                and isinstance(value["virtual_owner"], str)
                and bool(value["virtual_owner"])
                and isinstance(value["virtual_owner_sha256"], str)
                and bool(_HEX64.fullmatch(value["virtual_owner_sha256"]))
                and path_virtual_relative is not None
                and session_virtual_relative == path_virtual_relative
                and isinstance(
                    value["virtual_session_temp_root"],
                    str,
                )
                and bool(value["virtual_session_temp_root"])
                and type(value["virtual_native_api_pid"]) is int
                and value["virtual_native_api_pid"] > 0
            )
        )
        and isinstance(value["referenced_configuration"], str)
        and bool(value["referenced_configuration"])
        and isinstance(value["occurrence_scope"], str)
        and value["occurrence_scope"] == value["component"]
        and type(value["body_type"]) is int
        and value["body_type"] in {0, 1}
        and type(value["body_index"]) is int
        and value["body_index"] >= 0
        and value["body_identity_sha256"]
        == value["body_reference_sha256"]
        and type(value["face_index"]) is int
        and value["face_index"] >= 0
        and value["face_identity_sha256"]
        == value["face_reference_sha256"]
        and value["face_identity_mode"]
        in {
            NATIVE_PERSISTENT_IDENTITY_RULE,
            NATIVE_SCOPE_IDENTITY_RULE,
        }
        and (
            (
                value["face_identity_mode"]
                == NATIVE_PERSISTENT_IDENTITY_RULE
                and value["owner_scope_key"] is None
            )
            or (
                value["face_identity_mode"]
                == NATIVE_SCOPE_IDENTITY_RULE
                and isinstance(value["owner_scope_key"], str)
                and bool(_HEX64.fullmatch(value["owner_scope_key"]))
            )
        )
    )


def native_open_edge_scope_binding(
    *,
    component: str,
    source_document_path: str,
    source_copy_path: str | None,
    source_document_sha256: str,
    source_copy_sha256: str,
    source_document_identity_sha256: str,
    source_document_identity_kind: str,
    referenced_configuration: str,
    occurrence_scope: str,
    virtual_owner: str | None,
    virtual_owner_sha256: str | None,
    virtual_session_document_path: str | None,
    virtual_session_temp_root: str | None,
    virtual_native_api_pid: int | None,
    body_type: int,
    body_index: int,
    body_reference_sha256: str,
    native_body_reference_sha256: str,
    face_index: int,
    face_reference_sha256: str,
    native_face_reference_sha256: str,
    face_identity_mode: str,
    owner_scope_key: str | None,
) -> dict:
    """Build one exact component/body/face binding for an open-edge identity."""

    record = {
        "rule_version": NATIVE_OPEN_EDGE_SCOPE_RULE,
        "component": component,
        "source_document_path": source_document_path,
        "source_copy_path": source_copy_path,
        "source_document_sha256": source_document_sha256,
        "source_copy_sha256": source_copy_sha256,
        "source_document_identity_sha256": source_document_identity_sha256,
        "source_document_identity_kind": source_document_identity_kind,
        "referenced_configuration": referenced_configuration,
        "occurrence_scope": occurrence_scope,
        "virtual_owner": virtual_owner,
        "virtual_owner_sha256": virtual_owner_sha256,
        "virtual_session_document_path": virtual_session_document_path,
        "virtual_session_temp_root": virtual_session_temp_root,
        "virtual_native_api_pid": virtual_native_api_pid,
        "body_type": body_type,
        "body_index": body_index,
        "body_reference_sha256": body_reference_sha256,
        "native_body_reference_sha256": native_body_reference_sha256,
        "body_identity_sha256": body_reference_sha256,
        "face_index": face_index,
        "face_reference_sha256": face_reference_sha256,
        "native_face_reference_sha256": native_face_reference_sha256,
        "face_identity_mode": face_identity_mode,
        "face_identity_sha256": face_reference_sha256,
        "owner_scope_key": owner_scope_key,
    }
    if not _valid_open_edge_scope_binding(record):
        raise ValueError("open-edge scope binding is incomplete")
    return record


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
        "complete_match_count": matching_count,
        "canonical_result": canonical_result,
        "canonical_error_m": canonical_error,
        "alternative_margin_m": alternative_error,
        "distinct_endpoint_sets": distinct_endpoints,
        "accepted": accepted,
    }
    record["endpoint_set_sha256"] = hashlib.sha256(
        json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()
    return record


def _boundary_role(
    u_value: float,
    u_min: float,
    u_max: float,
) -> str | None:
    at_min = native_u_equal(u_value, u_min)
    at_max = native_u_equal(u_value, u_max)
    if at_min == at_max:
        return None
    return "u_min" if at_min else "u_max"


def _gap_mapping_options(
    source_start,
    source_end,
    target_start,
    target_end,
    gap_m: float,
) -> dict:
    def pair_error(left, right) -> float:
        return native_point_euclidean_error(tuple(left), tuple(right))

    direct_errors = (
        pair_error(source_start, target_start),
        pair_error(source_end, target_end),
    )
    swapped_errors = (
        pair_error(source_start, target_end),
        pair_error(source_end, target_start),
    )
    direct_complete = (
        native_point_within_euclidean_bound(
            source_start,
            target_start,
            gap_m,
        )
        and native_point_within_euclidean_bound(
            source_end,
            target_end,
            gap_m,
        )
    )
    swapped_complete = (
        native_point_within_euclidean_bound(
            source_start,
            target_end,
            gap_m,
        )
        and native_point_within_euclidean_bound(
            source_end,
            target_start,
            gap_m,
        )
    )
    options = []
    for name, errors, complete in (
        ("direct", direct_errors, direct_complete),
        ("swapped", swapped_errors, swapped_complete),
    ):
        options.append(
            {
                "result": name,
                "start_error_m": errors[0],
                "end_error_m": errors[1],
                "maximum_error_m": max(errors),
                "complete": complete,
            }
        )
    accepted = [item for item in options if item["complete"]]
    selected = accepted[0] if len(accepted) == 1 else None
    rejected = [item for item in options if not item["complete"]]
    margin = (
        min(item["maximum_error_m"] for item in rejected) - selected["maximum_error_m"]
        if selected is not None
        else None
    )
    return {
        "metric": "euclidean",
        "gap_m": gap_m,
        "options": options,
        "complete_match_count": len(accepted),
        "selected_result": selected["result"] if selected is not None else None,
        "selected_maximum_error_m": (
            selected["maximum_error_m"] if selected is not None else None
        ),
        "matching_margin_m": margin,
        "unique": selected is not None,
    }


def native_open_edge_boundary_identity(
    *,
    component: str,
    scope_binding: dict,
    edge_reference_sha256: str,
    edge_endpoint_refs: tuple[str, str],
    edge_start_point: tuple,
    edge_end_point: tuple,
    fin_id: int,
    fin_vertex_ids: tuple[int, int],
    fin_start_point: tuple,
    fin_end_point: tuple,
    curve_start_point: tuple,
    curve_end_point: tuple,
    curve_u_min: float,
    curve_u_max: float,
    fin_endpoint_u: tuple[float, float],
    fin_endpoint_success: tuple[bool, bool],
    curve_sense: bool,
    gap_m: float,
    body_type: int,
    face_count: int,
    cofin_raw_id: int,
    edge_fin_ids: tuple[int, ...],
    topology_set_accepted: bool,
) -> dict:
    """Prove one open sheet boundary fin through unordered native identities."""

    if not isinstance(component, str) or not component:
        raise ValueError("component must be a non-empty string")
    if (
        not _valid_open_edge_scope_binding(scope_binding)
        or scope_binding["component"] != component
        or scope_binding["body_type"] != body_type
    ):
        raise ValueError("open-edge scope binding differs from the native object")
    if (
        not isinstance(edge_reference_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", edge_reference_sha256) is None
    ):
        raise ValueError("edge reference must be a SHA-256")
    if (
        not isinstance(edge_endpoint_refs, tuple)
        or len(edge_endpoint_refs) != 2
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in edge_endpoint_refs
        )
        or edge_endpoint_refs[0] == edge_endpoint_refs[1]
    ):
        raise ValueError("edge endpoint refs must be two distinct SHA-256 values")
    if (
        not isinstance(fin_vertex_ids, tuple)
        or len(fin_vertex_ids) != 2
        or any(type(value) is not int or value < 0 for value in fin_vertex_ids)
        or fin_vertex_ids[0] == fin_vertex_ids[1]
    ):
        raise ValueError("fin vertex IDs must be two distinct non-negative integers")
    if type(fin_id) is not int or fin_id < 0:
        raise ValueError("fin ID must be a non-negative integer")
    if (
        not isinstance(edge_fin_ids, tuple)
        or not edge_fin_ids
        or any(type(value) is not int or value < 0 for value in edge_fin_ids)
        or len(edge_fin_ids) != len(set(edge_fin_ids))
        or fin_id not in edge_fin_ids
    ):
        raise ValueError("fin must belong to one unique edge fin set")
    if (
        type(body_type) is not int
        or type(face_count) is not int
        or type(cofin_raw_id) is not int
        or body_type != 1
        or face_count != 1
        or cofin_raw_id != -1
    ):
        raise ValueError("open-edge identity requires a one-face sheet boundary fin")
    if type(curve_sense) is not bool:
        raise ValueError("curve sense must be a strict boolean")
    if (
        not isinstance(fin_endpoint_success, tuple)
        or len(fin_endpoint_success) != 2
        or any(type(value) is not bool or value is not True for value in fin_endpoint_success)
    ):
        raise ValueError("both fin endpoints require successful GetParameter")
    if (
        not isinstance(fin_endpoint_u, tuple)
        or len(fin_endpoint_u) != 2
        or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in fin_endpoint_u)
    ):
        raise ValueError("fin endpoint U values must be finite numbers")
    if type(topology_set_accepted) is not bool or topology_set_accepted is not True:
        raise ValueError("unordered curve/edge endpoint identity must be accepted")
    if (
        type(curve_u_min) not in (int, float)
        or type(curve_u_max) not in (int, float)
        or not math.isfinite(float(curve_u_min))
        or not math.isfinite(float(curve_u_max))
        or float(curve_u_min) >= float(curve_u_max)
    ):
        raise ValueError("curve U boundaries must be finite and increasing")
    gap = native_tolerant_edge_gap(gap_m)
    points = (
        edge_start_point,
        edge_end_point,
        fin_start_point,
        fin_end_point,
        curve_start_point,
        curve_end_point,
    )
    if any(
        not isinstance(point, (list, tuple))
        or len(point) != 3
        for point in points
    ):
        raise ValueError("identity points must contain three coordinates")
    edge_start_point = tuple(_native_u(value) for value in edge_start_point)
    edge_end_point = tuple(_native_u(value) for value in edge_end_point)
    fin_start_point = tuple(_native_u(value) for value in fin_start_point)
    fin_end_point = tuple(_native_u(value) for value in fin_end_point)
    curve_start_point = tuple(_native_u(value) for value in curve_start_point)
    curve_end_point = tuple(_native_u(value) for value in curve_end_point)

    def endpoint_name(result: str, source: str) -> str:
        if result == "direct":
            return "start" if source == "start" else "end"
        return "end" if source == "start" else "start"

    roles = (
        _boundary_role(float(fin_endpoint_u[0]), curve_u_min, curve_u_max),
        _boundary_role(float(fin_endpoint_u[1]), curve_u_min, curve_u_max),
    )
    boundary_roles = tuple(role for role in roles if role is not None)
    if not boundary_roles:
        raise ValueError("fin must contain an exact edge boundary endpoint")
    if (
        len(boundary_roles) == 2
        and boundary_roles[0] == boundary_roles[1]
    ):
        raise ValueError(
            "fin endpoints must not collapse to one exact edge boundary"
        )
    full_edge = set(roles) == {"u_min", "u_max"}
    segmented = len(boundary_roles) == 1
    if not full_edge and not segmented:
        raise ValueError(
            "fin endpoints must identify a full or segmented edge interval"
        )

    curve_mapping = _gap_mapping_options(
        curve_start_point,
        curve_end_point,
        edge_start_point,
        edge_end_point,
        gap,
    )
    if not curve_mapping["unique"]:
        raise ValueError("gap-bounded endpoint mapping is not unique")
    curve_to_edge = {
        "start": endpoint_name(curve_mapping["selected_result"], "start"),
        "end": endpoint_name(curve_mapping["selected_result"], "end"),
    }

    fin_to_edge = {"start": None, "end": None}
    if full_edge:
        fin_mapping = _gap_mapping_options(
            fin_start_point,
            fin_end_point,
            edge_start_point,
            edge_end_point,
            gap,
        )
        if not fin_mapping["unique"]:
            raise ValueError("gap-bounded fin endpoint mapping is not unique")
        fin_to_edge = {
            "start": endpoint_name(fin_mapping["selected_result"], "start"),
            "end": endpoint_name(fin_mapping["selected_result"], "end"),
        }
        for fin_endpoint, role in zip(("start", "end"), roles, strict=True):
            curve_endpoint = "start" if role == "u_min" else "end"
            if fin_to_edge[fin_endpoint] != curve_to_edge[curve_endpoint]:
                raise ValueError(
                    "fin U boundary and gap mapping disagree on edge endpoint"
                )
    else:
        boundary_index = 0 if roles[0] is not None else 1
        interior_index = 1 - boundary_index
        boundary_role = roles[boundary_index]
        interior_u = float(fin_endpoint_u[interior_index])
        if (
            not native_u_in_closed_interval(
                interior_u,
                curve_u_min,
                curve_u_max,
            )
            or native_u_equal(interior_u, curve_u_min)
            or native_u_equal(interior_u, curve_u_max)
        ):
            raise ValueError(
                "segmented fin interior endpoint must be strictly inside the U domain"
            )
        curve_endpoint = "start" if boundary_role == "u_min" else "end"
        edge_endpoint = curve_to_edge[curve_endpoint]
        fin_endpoint = "start" if boundary_index == 0 else "end"
        fin_boundary_point = (
            fin_start_point if boundary_index == 0 else fin_end_point
        )
        curve_boundary_point = (
            curve_start_point if curve_endpoint == "start" else curve_end_point
        )
        opposite_curve_point = (
            curve_end_point if curve_endpoint == "start" else curve_start_point
        )
        boundary_to_curve_tolerance = math.nextafter(
            native_curve_endpoint_operation_tolerance(
                fin_boundary_point,
                fin_boundary_point,
                opposite_curve_point,
            )
            + gap,
            math.inf,
        )
        boundary_to_curve_error = native_point_euclidean_error(
            fin_boundary_point,
            curve_boundary_point,
        )
        if boundary_to_curve_error > boundary_to_curve_tolerance:
            raise ValueError(
                "segmented fin boundary endpoint does not match its curve boundary"
            )
        target_point = (
            edge_start_point if edge_endpoint == "start" else edge_end_point
        )
        boundary_error = native_point_euclidean_error(
            curve_boundary_point,
            target_point,
        )
        candidates = []
        for candidate_name, candidate_point in (
            ("start", edge_start_point),
            ("end", edge_end_point),
        ):
            if native_point_within_euclidean_bound(
                curve_boundary_point,
                candidate_point,
                gap,
            ):
                candidates.append(candidate_name)
        if candidates != [edge_endpoint]:
            raise ValueError(
                "segmented fin boundary endpoint does not map uniquely to its edge endpoint"
            )
        fin_to_edge[fin_endpoint] = edge_endpoint
        fin_mapping = {
            "mode": "segmented",
            "metric": "euclidean",
            "gap_m": gap,
            "boundary_endpoint": fin_endpoint,
            "boundary_role": boundary_role,
            "edge_endpoint": edge_endpoint,
            "boundary_error_m": boundary_error,
            "fin_boundary_to_curve_error_m": boundary_to_curve_error,
            "fin_boundary_to_curve_tolerance_m": (
                boundary_to_curve_tolerance
            ),
            "interior_endpoint": "end" if interior_index == 1 else "start",
            "interior_u": interior_u,
            "complete_match_count": 1,
            "unique": True,
        }

    two_gap = math.nextafter(2.0 * gap, math.inf)
    separation_diagnostics = {
        "possible_ambiguity_bound_m": two_gap,
        "edge_separation_at_most_bound": (
            native_point_within_euclidean_bound(
                edge_start_point,
                edge_end_point,
                two_gap,
            )
        ),
        "fin_separation_at_most_bound": (
            native_point_within_euclidean_bound(
                fin_start_point,
                fin_end_point,
                two_gap,
            )
        ),
        "curve_separation_at_most_bound": (
            native_point_within_euclidean_bound(
                curve_start_point,
                curve_end_point,
                two_gap,
            )
        ),
        "used_for_decision": False,
    }

    record = {
        "rule_version": NATIVE_OPEN_EDGE_BOUNDARY_IDENTITY_RULE,
        "identity_mode": "full_edge" if full_edge else "segmented",
        "scope_binding": scope_binding,
        "edge_reference_sha256": edge_reference_sha256,
        "edge_endpoint_refs": {
            "start": edge_endpoint_refs[0],
            "end": edge_endpoint_refs[1],
        },
        "body_type": body_type,
        "face_count": face_count,
        "cofin_raw_id": cofin_raw_id,
        "edge_fin_ids": list(edge_fin_ids),
        "fin_id": fin_id,
        "fin_vertex_ids": list(fin_vertex_ids),
        "edge_endpoint_points_m": {
            "start": list(edge_start_point),
            "end": list(edge_end_point),
        },
        "fin_endpoint_points_m": {
            "start": list(fin_start_point),
            "end": list(fin_end_point),
        },
        "curve_endpoint_points_m": {
            "start": list(curve_start_point),
            "end": list(curve_end_point),
        },
        "curve_u_boundaries": {
            "u_min": float(curve_u_min),
            "u_max": float(curve_u_max),
        },
        "fin_endpoint_u": {
            "start": float(fin_endpoint_u[0]),
            "end": float(fin_endpoint_u[1]),
        },
        "fin_endpoint_boundary_roles": {
            "start": roles[0],
            "end": roles[1],
        },
        "curve_sense": curve_sense,
        "curve_sense_metadata": {
            "used_for_decision": False,
        },
        "gap": {
            "metric": "euclidean",
            "value_m": gap,
            "inclusive": True,
            "source": NATIVE_TOLERANT_EDGE_GAP_SOURCE,
        },
        "separation_diagnostics": separation_diagnostics,
        "curve_to_edge_mapping": curve_mapping,
        "fin_to_edge_mapping": fin_mapping,
        "selected_fin_to_edge": fin_to_edge,
        "selected_curve_to_edge": curve_to_edge,
        "fin_vertex_com_mapping": {
            "required": False,
            "used_for_gate": False,
        },
        "direction_dot": {
            "used_for_decision": False,
        },
        "accepted": True,
    }
    record["identity_chain_sha256"] = hashlib.sha256(
        json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()
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

    ``Sense`` may orient the periodic parameter lift only. It never changes the
    raw ``UMin``/``StartPoint`` and ``UMax``/``EndPoint`` correspondence or the
    unordered curve/edge endpoint-set identity.
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
