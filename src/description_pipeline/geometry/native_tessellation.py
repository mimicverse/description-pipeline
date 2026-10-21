"""Strict native face-span grammar shared by verification and report projection."""

from __future__ import annotations

import math
import re
import struct
from collections import defaultdict

from ..io import PipelineError
from ..schemas import NATIVE_TESSELLATION_SCHEMA
from .native_u import (
    NATIVE_CURVE_ENDPOINT_OPERATION_RULE,
    NATIVE_COLLAPSED_U_FIN_RULE,
    NATIVE_COMPONENT_REFERENCE_ALIASED,
    NATIVE_FACE_IDENTITY_KEY_RULE,
    NATIVE_SCOPE_MAPPING_METHOD,
    NATIVE_SCOPE_SOURCE_AUTHORITY,
    NATIVE_SCOPE_VERTEX_MAPPING_METHOD,
    NATIVE_PERSISTENT_IDENTITY_RULE,
    NATIVE_POINT_ROUNDTRIP_RULE,
    NATIVE_IDENTITY_POLICY_RULE,
    NATIVE_PART_WITNESS_DERIVED,
    NATIVE_SCOPE_IDENTITY_RULE,
    NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
    NATIVE_TOPOLOGY_IDENTITY_DERIVED,
    NATIVE_TESSELLATION_EDGE_PROJECTION_RULE,
    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
    NATIVE_U_CLOSEST_FALLBACK_RULE,
    NATIVE_U_IDENTITY_RULE,
    NATIVE_U_POINT_PARAMETER_RULE,
    NATIVE_U_RESOLUTION_RULE,
    native_curve_endpoint_operation_tolerance,
    native_curve_endpoint_roles,
    native_curve_topology_endpoint_assignment,
    native_fin_interval,
    native_get_parameter_success,
    native_point_error,
    native_point_roundtrip_tolerance,
    native_u_equal,
    native_u_in_closed_interval,
    native_u_intervals_cover,
    native_curve_domain_transform,
    native_scope_identity_sha256,
)

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
ITESSELLATION_IID = "{699C7492-DB56-4CC2-B7A2-9FFD56698434}"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _integer(value) -> bool:
    return type(value) is int


def _binary64_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{16}", value) is not None
    )


def native_mapped_witnesses_are_global_unique(
    rows: list[dict],
    index_field: str,
    reference_field: str,
) -> bool:
    """Whether mapped part witnesses are unique across every component body."""

    witnesses = [
        (
            row["identity"]["mapping_source_body_reference_sha256"],
            row["identity"][index_field],
            row["identity"][reference_field],
        )
        for row in rows
        if row["identity"] is not None
    ]
    return len(witnesses) == len(set(witnesses))


def _valid_scope_identity(
    value: object,
    *,
    kind: str,
    component: str,
) -> bool:
    common = {
        "rule_version",
        "entity_kind",
        "identity_kind",
        "persistent",
        "owner_scope_key",
        "warnings",
    }
    keys = {
        "face": common
        | {
            "source_document_sha256",
            "referenced_configuration",
            "occurrence_scope",
            "body_type",
            "body_index",
            "body_name",
            "native_face_index",
            "area_binary64_hex",
            "tessellation_bbox_min_binary64_hex",
            "tessellation_bbox_max_binary64_hex",
            "tessellation_coordinate_sha256_float64_le",
            "topology_signature_sha256",
            "raw_component_reference_sha256",
            "raw_component_reference_kind",
            "mapping_source_part_document_sha256",
            "mapping_source_body_reference_sha256",
            "mapping_source_face_index",
            "mapped_source_face_persistent_reference_sha256",
            "mapping_method",
            "source_authority",
        },
        "edge": common
        | {
            "source_document_sha256",
            "native_edge_index",
            "start_point_binary64_hex",
            "end_point_binary64_hex",
            "start_vertex_reference_sha256",
            "end_vertex_reference_sha256",
            "curve_type",
            "curve_sense",
            "curve_domain_binary64_hex",
            "curve_closed",
            "curve_periodic",
            "adjacent_mapped_face_ids",
            "topology_signature_sha256",
            "mapping_source_part_document_sha256",
            "mapping_source_body_reference_sha256",
            "mapping_source_edge_index",
            "mapping_source_edge_persistent_reference_sha256",
            "mapping_method",
            "source_authority",
        },
        "vertex": common
        | {
            "source_document_sha256",
            "native_vertex_index",
            "point_binary64_hex",
            "coincident_occurrence_index",
            "incident_mapped_edge_indices",
            "incident_mapped_edge_reference_sha256",
            "topology_signature_sha256",
            "mapping_source_part_document_sha256",
            "mapping_source_body_reference_sha256",
            "mapping_source_vertex_index",
            "mapped_source_vertex_persistent_reference_sha256",
            "mapping_method",
            "source_authority",
        },
    }
    if not isinstance(value, dict) or set(value) != keys[kind]:
        return False
    warnings = value["warnings"]
    expected_warnings = {NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED}
    return (
        value["rule_version"] == NATIVE_SCOPE_IDENTITY_RULE
        and value["entity_kind"] == kind
        and value["identity_kind"] == "mapped_scope_local"
        and value["persistent"] is False
        and isinstance(value["owner_scope_key"], str)
        and bool(HEX64.fullmatch(value["owner_scope_key"]))
        and isinstance(warnings, list)
        and warnings == sorted(set(warnings))
        and set(warnings)
        == expected_warnings
        | {
            NATIVE_COMPONENT_REFERENCE_ALIASED,
            NATIVE_PART_WITNESS_DERIVED,
            NATIVE_TOPOLOGY_IDENTITY_DERIVED,
        }
        and (
            kind != "face"
            or (
                isinstance(value["source_document_sha256"], str)
                and bool(
                    HEX64.fullmatch(value["source_document_sha256"])
                )
                and isinstance(value["referenced_configuration"], str)
                and bool(value["referenced_configuration"])
                and value["occurrence_scope"] == component
                and _integer(value["body_type"])
                and value["body_type"] in (0, 1)
                and _integer(value["body_index"])
                and value["body_index"] >= 0
                and isinstance(value["body_name"], str)
                and bool(value["body_name"])
                and _integer(value["native_face_index"])
                and value["native_face_index"] >= 0
                and _binary64_hex(value["area_binary64_hex"])
                and isinstance(
                    value["tessellation_bbox_min_binary64_hex"], list
                )
                and len(value["tessellation_bbox_min_binary64_hex"]) == 3
                and all(
                    _binary64_hex(item)
                    for item in value[
                        "tessellation_bbox_min_binary64_hex"
                    ]
                )
                and isinstance(
                    value["tessellation_bbox_max_binary64_hex"], list
                )
                and len(value["tessellation_bbox_max_binary64_hex"]) == 3
                and all(
                    _binary64_hex(item)
                    for item in value[
                        "tessellation_bbox_max_binary64_hex"
                    ]
                )
                and isinstance(
                    value[
                        "tessellation_coordinate_sha256_float64_le"
                    ],
                    str,
                )
                and bool(
                    HEX64.fullmatch(
                        value[
                            "tessellation_coordinate_sha256_float64_le"
                        ]
                    )
                )
                and isinstance(value["topology_signature_sha256"], str)
                and bool(
                    HEX64.fullmatch(value["topology_signature_sha256"])
                )
                and isinstance(
                    value["mapping_source_part_document_sha256"], str
                )
                and bool(
                    HEX64.fullmatch(
                        value["mapping_source_part_document_sha256"]
                    )
                )
                and isinstance(
                    value["mapping_source_body_reference_sha256"], str
                )
                and bool(
                    HEX64.fullmatch(
                        value["mapping_source_body_reference_sha256"]
                    )
                )
                and isinstance(
                    value["raw_component_reference_sha256"], str
                )
                and bool(
                    HEX64.fullmatch(
                        value["raw_component_reference_sha256"]
                    )
                )
                and value["raw_component_reference_kind"]
                == "body_scope_alias"
                and isinstance(
                    value["mapping_source_part_document_sha256"], str
                )
                and bool(
                    HEX64.fullmatch(
                        value["mapping_source_part_document_sha256"]
                    )
                )
                and _integer(value["mapping_source_face_index"])
                and value["mapping_source_face_index"] >= 0
                and isinstance(
                    value[
                        "mapped_source_face_persistent_reference_sha256"
                    ],
                    str,
                )
                and bool(
                    HEX64.fullmatch(
                        value[
                            "mapped_source_face_persistent_reference_sha256"
                        ]
                    )
                )
                and value["mapping_method"]
                == NATIVE_SCOPE_MAPPING_METHOD
                and value["source_authority"]
                == NATIVE_SCOPE_SOURCE_AUTHORITY
            )
        )
        and (
            kind != "edge"
            or (
                isinstance(value["source_document_sha256"], str)
                and bool(
                    HEX64.fullmatch(value["source_document_sha256"])
                )
                and _integer(value["native_edge_index"])
                and value["native_edge_index"] >= 0
                and isinstance(value["start_point_binary64_hex"], list)
                and len(value["start_point_binary64_hex"]) == 3
                and all(
                    _binary64_hex(item)
                    for item in value["start_point_binary64_hex"]
                )
                and isinstance(value["end_point_binary64_hex"], list)
                and len(value["end_point_binary64_hex"]) == 3
                and all(
                    _binary64_hex(item)
                    for item in value["end_point_binary64_hex"]
                )
                and (
                    value["start_vertex_reference_sha256"] is None
                    or (
                        isinstance(
                            value["start_vertex_reference_sha256"],
                            str,
                        )
                        and bool(
                            HEX64.fullmatch(
                                value["start_vertex_reference_sha256"]
                            )
                        )
                    )
                )
                and (
                    value["end_vertex_reference_sha256"] is None
                    or (
                        isinstance(
                            value["end_vertex_reference_sha256"],
                            str,
                        )
                        and bool(
                            HEX64.fullmatch(
                                value["end_vertex_reference_sha256"]
                            )
                        )
                    )
                )
                and _integer(value["curve_type"])
                and value["curve_type"] >= 0
                and type(value["curve_sense"]) is bool
                and isinstance(value["curve_domain_binary64_hex"], list)
                and len(value["curve_domain_binary64_hex"]) == 2
                and all(
                    _binary64_hex(item)
                    for item in value["curve_domain_binary64_hex"]
                )
                and type(value["curve_closed"]) is bool
                and type(value["curve_periodic"]) is bool
                and isinstance(value["adjacent_mapped_face_ids"], list)
                and value["adjacent_mapped_face_ids"]
                == sorted(set(value["adjacent_mapped_face_ids"]))
                and all(
                    _integer(item) and item >= 0
                    for item in value["adjacent_mapped_face_ids"]
                )
                and isinstance(value["topology_signature_sha256"], str)
                and bool(
                    HEX64.fullmatch(value["topology_signature_sha256"])
                )
                and isinstance(
                    value[
                        "mapping_source_edge_persistent_reference_sha256"
                    ],
                    str,
                )
                and bool(
                    HEX64.fullmatch(
                        value[
                            "mapping_source_edge_persistent_reference_sha256"
                        ]
                    )
                )
                and _integer(value["mapping_source_edge_index"])
                and value["mapping_source_edge_index"] >= 0
                and isinstance(
                    value["mapping_source_body_reference_sha256"], str
                )
                and bool(
                    HEX64.fullmatch(
                        value["mapping_source_body_reference_sha256"]
                    )
                )
                and value["mapping_source_part_document_sha256"]
                == value["source_document_sha256"]
                and value["mapping_method"]
                == NATIVE_SCOPE_MAPPING_METHOD
                and value["source_authority"]
                == NATIVE_SCOPE_SOURCE_AUTHORITY
            )
        )
        and (
            kind != "vertex"
            or (
                isinstance(value["source_document_sha256"], str)
                and bool(
                    HEX64.fullmatch(value["source_document_sha256"])
                )
                and _integer(value["native_vertex_index"])
                and value["native_vertex_index"] >= 0
                and isinstance(value["point_binary64_hex"], list)
                and len(value["point_binary64_hex"]) == 3
                and all(
                    _binary64_hex(item)
                    for item in value["point_binary64_hex"]
                )
                and _integer(value["coincident_occurrence_index"])
                and value["coincident_occurrence_index"] >= 0
                and isinstance(
                    value["incident_mapped_edge_indices"], list
                )
                and value["incident_mapped_edge_indices"]
                == sorted(set(value["incident_mapped_edge_indices"]))
                and all(
                    _integer(item) and item >= 0
                    for item in value["incident_mapped_edge_indices"]
                )
                and isinstance(
                    value[
                        "incident_mapped_edge_reference_sha256"
                    ],
                    list,
                )
                and value[
                    "incident_mapped_edge_reference_sha256"
                ]
                == sorted(
                    set(
                        value[
                            "incident_mapped_edge_reference_sha256"
                        ]
                    )
                )
                and all(
                    isinstance(item, str) and bool(HEX64.fullmatch(item))
                    for item in value[
                        "incident_mapped_edge_reference_sha256"
                    ]
                )
                and isinstance(value["topology_signature_sha256"], str)
                and bool(
                    HEX64.fullmatch(value["topology_signature_sha256"])
                )
                and isinstance(
                    value["mapping_source_part_document_sha256"], str
                )
                and bool(
                    HEX64.fullmatch(
                        value["mapping_source_part_document_sha256"]
                    )
                )
                and isinstance(
                    value["mapping_source_body_reference_sha256"], str
                )
                and bool(
                    HEX64.fullmatch(
                        value["mapping_source_body_reference_sha256"]
                    )
                )
                and _integer(value["mapping_source_vertex_index"])
                and value["mapping_source_vertex_index"] >= 0
                and isinstance(
                    value[
                        "mapped_source_vertex_persistent_reference_sha256"
                    ],
                    str,
                )
                and bool(
                    HEX64.fullmatch(
                        value[
                            "mapped_source_vertex_persistent_reference_sha256"
                        ]
                    )
                )
                and value["mapping_source_part_document_sha256"]
                == value["source_document_sha256"]
                and value["mapping_method"]
                == NATIVE_SCOPE_VERTEX_MAPPING_METHOD
                and value["source_authority"]
                == NATIVE_SCOPE_SOURCE_AUTHORITY
            )
        )
    )


def _valid_evaluate2_packed_status(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "raw_type",
        "raw_bits_u64",
        "high32",
        "low32",
        "parsed_success",
    }:
        return False
    bits = value["raw_bits_u64"]
    return (
        value["raw_type"] in {"int", "float"}
        and _integer(bits)
        and 0 <= bits <= 0xFFFFFFFFFFFFFFFF
        and value["high32"] == (bits >> 32) & 0xFFFFFFFF
        and value["low32"] == bits & 0xFFFFFFFF
        and value["low32"] == 1
        and value["parsed_success"] is True
    )


def _valid_u_evidence(
    value: object,
    u_parameter: object,
    endpoint_point: object,
    domain_lower: object,
    domain_upper: object,
    curve_start: object,
    curve_end: object,
) -> bool:
    """Validate one retained GetClosestPointOn/GetParameter/Evaluate2 proof."""

    if not isinstance(value, dict) or set(value) != {
        "u",
        "u_parameter_rule",
        "parameter_u_source",
        "fallback_reason",
        "rule_version",
        "point_roundtrip_rule",
        "projection_rule",
        "projection_tolerance_m",
        "source_point_m",
        "projection_error_m",
        "point_tolerance_m",
        "closest_point_error_m",
        "evaluate_point_error_m",
        "source_evaluate_error_m",
        "closest_evaluate_error_m",
        "closest_point_on_raw",
        "parameter_input_point_m",
        "parameter_raw",
        "parameter_success_raw",
        "parameter_success_raw_type",
        "parameter_success_bit",
        "parameter_success_parsed",
        "evaluate_payload",
        "evaluate_packed_status",
    }:
        return False
    closest = value["closest_point_on_raw"]
    parameter = value["parameter_raw"]
    evaluated = value["evaluate_payload"]
    numeric = (int, float)
    if (
        not isinstance(endpoint_point, (list, tuple))
        or len(endpoint_point) != 3
        or not all(
            type(item) in numeric and math.isfinite(float(item))
            for item in endpoint_point
        )
        or not isinstance(closest, list)
        or len(closest) != 5
        or not all(
            type(item) in numeric and math.isfinite(float(item))
            for item in closest
        )
        or not isinstance(evaluated, list)
        or len(evaluated) != 6
        or not all(
            type(item) in numeric and math.isfinite(float(item))
            for item in evaluated
        )
        or not isinstance(value["source_point_m"], list)
        or len(value["source_point_m"]) != 3
        or not all(
            type(item) in numeric and math.isfinite(float(item))
            for item in value["source_point_m"]
        )
        or not isinstance(value["parameter_input_point_m"], list)
        or len(value["parameter_input_point_m"]) != 3
        or not all(
            type(item) in numeric and math.isfinite(float(item))
            for item in value["parameter_input_point_m"]
        )
        or not isinstance(curve_start, (list, tuple))
        or len(curve_start) != 3
        or not all(
            type(item) in numeric and math.isfinite(float(item))
            for item in curve_start
        )
        or not isinstance(curve_end, (list, tuple))
        or len(curve_end) != 3
        or not all(
            type(item) in numeric and math.isfinite(float(item))
            for item in curve_end
        )
        or type(domain_lower) not in numeric
        or type(domain_upper) not in numeric
        or not math.isfinite(float(domain_lower))
        or not math.isfinite(float(domain_upper))
        or type(u_parameter) not in numeric
        or not math.isfinite(float(u_parameter))
        or type(value["u"]) not in numeric
        or not math.isfinite(float(value["u"]))
    ):
        return False
    closest_point = tuple(closest[:3])
    evaluated_point = tuple(evaluated[:3])
    expected_point = tuple(float(item) for item in endpoint_point)
    source_point = tuple(value["source_point_m"])
    projection_error = native_point_error(source_point, closest_point)
    if (
        source_point != expected_point
        or tuple(value["parameter_input_point_m"]) != closest_point
        or value["projection_rule"]
        != NATIVE_TESSELLATION_EDGE_PROJECTION_RULE
        or value["projection_tolerance_m"]
        != NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
        or projection_error > NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
    ):
        return False
    parameter_success = native_get_parameter_success(
        parameter[1]
    ) if isinstance(parameter, list) and len(parameter) == 2 else None
    if parameter_success is False:
        computed_tolerance = (
            NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
        )
        point_roundtrip_rule = NATIVE_TESSELLATION_EDGE_PROJECTION_RULE
    elif native_u_equal(value["u"], domain_lower):
        computed_tolerance = native_curve_endpoint_operation_tolerance(
            closest_point,
            evaluated_point,
            curve_end,
        )
        point_roundtrip_rule = NATIVE_CURVE_ENDPOINT_OPERATION_RULE
    elif native_u_equal(value["u"], domain_upper):
        computed_tolerance = native_curve_endpoint_operation_tolerance(
            closest_point,
            evaluated_point,
            curve_start,
        )
        point_roundtrip_rule = NATIVE_CURVE_ENDPOINT_OPERATION_RULE
    else:
        computed_tolerance = native_point_roundtrip_tolerance(
            closest_point,
            evaluated_point,
        )
        point_roundtrip_rule = NATIVE_POINT_ROUNDTRIP_RULE
    evaluate_error = native_point_error(closest_point, evaluated_point)
    source_evaluate_error = native_point_error(
        source_point,
        evaluated_point,
    )
    closest_evaluate_error = native_point_error(
        closest_point,
        evaluated_point,
    )
    finite_values = (
        value["u"],
        value["point_tolerance_m"],
        value["closest_point_error_m"],
        value["evaluate_point_error_m"],
        value["source_evaluate_error_m"],
        value["closest_evaluate_error_m"],
    )
    fallback = parameter_success is False
    expected_u_rule = (
        NATIVE_U_CLOSEST_FALLBACK_RULE
        if fallback
        else NATIVE_U_POINT_PARAMETER_RULE
    )
    expected_u_source = (
        "closest_point_fallback"
        if fallback
        else "get_parameter"
    )
    expected_fallback_reason = "get_parameter_false" if fallback else None
    expected_success_bit = 0 if fallback else 1
    authoritative_u = closest[3] if fallback else parameter[0]
    return (
        value["rule_version"] == NATIVE_U_IDENTITY_RULE
        and value["u_parameter_rule"] == expected_u_rule
        and value["parameter_u_source"] == expected_u_source
        and value["fallback_reason"] == expected_fallback_reason
        and value["point_roundtrip_rule"] == point_roundtrip_rule
        and all(
            type(item) in numeric and math.isfinite(float(item))
            for item in finite_values
        )
        and value["point_tolerance_m"] >= 0.0
        and value["point_tolerance_m"] == computed_tolerance
        and value["projection_error_m"] == projection_error
        and value["closest_point_error_m"] == projection_error
        and value["evaluate_point_error_m"] == evaluate_error
        and value["source_evaluate_error_m"] == source_evaluate_error
        and value["closest_evaluate_error_m"] == closest_evaluate_error
        and max(evaluate_error, closest_evaluate_error)
        <= computed_tolerance
        and native_u_equal(value["u"], u_parameter)
        and isinstance(parameter, list)
        and len(parameter) == 2
        and type(parameter[0]) in numeric
        and math.isfinite(float(parameter[0]))
        and parameter_success is not None
        and native_u_in_closed_interval(
            closest[3],
            domain_lower,
            domain_upper,
        )
        and value["parameter_success_raw"] == parameter[1]
        and value["parameter_success_raw_type"] == type(parameter[1]).__name__
        and value["parameter_success_bit"] == expected_success_bit
        and value["parameter_success_parsed"] is (not fallback)
        and native_u_equal(authoritative_u, value["u"])
        and native_u_in_closed_interval(
            authoritative_u,
            domain_lower,
            domain_upper,
        )
        and (
            not fallback
            or source_evaluate_error
            <= NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
        )
        and _valid_evaluate2_packed_status(
            value["evaluate_packed_status"]
        )
    )


def _valid_curve_endpoint_evaluation(value: object, u_parameter: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "u",
        "point_roundtrip_rule",
        "payload",
        "packed_status",
        "error",
        "point_tolerance_m",
        "point_error_m",
    }:
        return False
    if type(value["u"]) not in (int, float) or not native_u_equal(
        value["u"],
        u_parameter,
    ):
        return False
    if value["error"] is not None:
        return False
    payload = value["payload"]
    tolerance = value["point_tolerance_m"]
    error_m = value["point_error_m"]
    return (
        isinstance(payload, list)
        and len(payload) == 6
        and all(
            type(value) in (int, float) and math.isfinite(float(value))
            for value in payload
        )
        and _valid_evaluate2_packed_status(value["packed_status"])
        and value["point_roundtrip_rule"]
        == NATIVE_CURVE_ENDPOINT_OPERATION_RULE
        and type(tolerance) in (int, float)
        and type(error_m) in (int, float)
        and math.isfinite(float(tolerance))
        and math.isfinite(float(error_m))
        and tolerance >= 0.0
        and error_m <= tolerance
    )


def _write_only_sense_error(value: object) -> bool:
    """Return whether a secondary sense read failed with documented COM 61836."""

    return (
        isinstance(value, str)
        and value.startswith("com_error:")
        and "(61836," in value
    )


def _finite_fin_intervals(
    *,
    fin_id: int,
    start_u: float,
    end_u: float,
    start_point: object,
    end_point: object,
    u_min: float,
    u_max: float,
    is_periodic: bool,
    is_closed: bool,
    component: str,
    reference: str,
) -> tuple[list[tuple[float, float]], float | None]:
    """Return coverage intervals, or the bounded span of a collapsed-U fin."""

    if not native_u_equal(start_u, end_u):
        return (
            native_fin_interval(
                start_u,
                end_u,
                u_min,
                u_max,
                is_periodic=is_periodic,
                is_closed=is_closed,
            ),
            None,
        )
    span = native_point_error(start_point, end_point)
    _require(
        0.0 < span <= NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
        component
        + f": native tessellation v6 edge {reference} fin {fin_id} collapsed U span is invalid",
    )
    return [], span


def parse_native_tessellation(
    component: str,
    tessellation: object,
    bodies: dict,
    triangles: int,
    triangle_vertices: list[tuple[tuple[float, float, float], ...]] | None = None,
    *,
    native_fin_resolutions: list[dict] | None = None,
    native_identity_warnings: list[dict] | None = None,
) -> list[dict]:
    """Validate the one current tessellation schema and return its face spans."""

    _require(
        native_fin_resolutions is None
        or (
            isinstance(native_fin_resolutions, list)
            and not native_fin_resolutions
        ),
        component
        + ": native fin resolution output must be an empty list",
    )
    _require(
        native_identity_warnings is None
        or (
            isinstance(native_identity_warnings, list)
            and not native_identity_warnings
        ),
        component
        + ": native identity warning output must be an empty list",
    )
    _require(
        isinstance(tessellation, dict)
        and set(tessellation) == {
            "schema_version",
            "faces",
            "face_spans",
            "native_edges",
            "no_facets_faces",
            "itessellation",
        },
        component + ": unknown or legacy tessellation accounting",
    )
    _require(
        tessellation["schema_version"] == NATIVE_TESSELLATION_SCHEMA,
        component + ": unsupported native tessellation schema",
    )
    spans = tessellation["face_spans"]
    faces = tessellation["faces"]
    _require(
        _integer(faces)
        and faces > 0
        and isinstance(spans, list)
        and len(spans) == faces,
        component + ": native face-span count is inconsistent",
    )
    required = {
        "body_type",
        "body_index",
        "body_name",
        "body_reference_sha256",
        "native_body_reference_sha256",
        "face_index",
        "face_reference_sha256",
        "native_face_reference_sha256",
        "face_identity_mode",
        "face_identity",
        "face_owner_id",
        "edge_references",
        "triangle_start",
        "triangle_count",
        "triangle_records_sha256",
    }
    previous = None
    next_start = 0
    body_names = {}
    body_references = {}
    face_references = set()
    face_owner_ids = set()
    face_keys = set()
    bodies_by_face = {}
    face_edge_references = {}
    face_spans_by_key = {}
    spans_by_body = {}
    for index, span in enumerate(spans):
        _require(
            isinstance(span, dict) and set(span) == required,
            component + f": face span {index} has an unknown shape",
        )
        body_type = span["body_type"]
        body_index = span["body_index"]
        face_index = span["face_index"]
        start = span["triangle_start"]
        count = span["triangle_count"]
        body_name = span["body_name"]
        body_reference = span["body_reference_sha256"]
        native_body_reference = span["native_body_reference_sha256"]
        face_reference = span["face_reference_sha256"]
        native_face_reference = span["native_face_reference_sha256"]
        face_identity_mode = span["face_identity_mode"]
        face_identity = span["face_identity"]
        face_owner_id = span["face_owner_id"]
        edge_references = span["edge_references"]
        records_digest = span["triangle_records_sha256"]
        expected_owner_scope_key = (
            native_scope_identity_sha256(
                {
                    "occurrence_scope": face_identity["occurrence_scope"],
                    "source_document_sha256": face_identity[
                        "source_document_sha256"
                    ],
                    "referenced_configuration": face_identity[
                        "referenced_configuration"
                    ],
                    "body_type": face_identity["body_type"],
                    "body_index": face_identity["body_index"],
                    "body_name": face_identity["body_name"],
                }
            )
            if isinstance(face_identity, dict)
            else None
        )
        scope_identity_valid = (
            (
                face_identity_mode == NATIVE_PERSISTENT_IDENTITY_RULE
                and face_identity is None
            )
            or (
                face_identity_mode == NATIVE_SCOPE_IDENTITY_RULE
                and _valid_scope_identity(
                    face_identity,
                    kind="face",
                    component=component,
                )
                and face_identity["body_type"] == body_type
                and face_identity["body_index"] == body_index
                and face_identity["body_name"] == body_name
                and face_identity["native_face_index"] == face_index
                and face_identity["raw_component_reference_sha256"]
                == native_face_reference
                and face_identity["mapping_source_part_document_sha256"]
                == face_identity["source_document_sha256"]
                and face_reference
                == native_scope_identity_sha256(face_identity)
                and face_identity["owner_scope_key"]
                == expected_owner_scope_key
            )
        )
        _require(
            _integer(body_type)
            and body_type in (0, 1)
            and _integer(body_index)
            and body_index >= 0
            and _integer(face_index)
            and face_index >= 0
            and _integer(start)
            and _integer(count)
            and count >= 0
            and isinstance(body_name, str)
            and bool(body_name)
            and isinstance(body_reference, str)
            and bool(HEX64.fullmatch(body_reference))
            and isinstance(native_body_reference, str)
            and bool(HEX64.fullmatch(native_body_reference))
            and isinstance(face_reference, str)
            and bool(HEX64.fullmatch(face_reference))
            and isinstance(native_face_reference, str)
            and bool(HEX64.fullmatch(native_face_reference))
            and scope_identity_valid
            and _integer(face_owner_id)
            and 0 < face_owner_id <= 0xFFFF
            and isinstance(edge_references, list)
            and len(edge_references) == len(set(edge_references))
            and all(
                isinstance(reference, str) and bool(HEX64.fullmatch(reference))
                for reference in edge_references
            )
            and isinstance(records_digest, str)
            and bool(HEX64.fullmatch(records_digest)),
            component + f": face span {index} is malformed",
        )
        body_count = bodies["solid" if body_type == 0 else "sheet"]
        _require(
            body_index < body_count,
            component + f": face span {index} names a body outside its native inventory",
        )
        order = (body_type, body_index, face_index)
        _require(
            previous is None or order > previous,
            component + f": face span {index} is duplicated or out of canonical order",
        )
        _require(
            start == next_start,
            component + f": face span {index} leaves a gap or overlaps the triangle stream",
        )
        body_key = (body_type, body_index)
        _require(
            body_names.setdefault(body_key, body_name) == body_name,
            component + f": native body {body_key} changes name across its faces",
        )
        _require(
            body_references.setdefault(body_key, body_reference) == body_reference,
            component + f": native body {body_key} changes persistent reference across its faces",
        )
        _require(
            face_reference not in face_references,
            component + f": native face reference repeats at span {index}",
        )
        face_references.add(face_reference)
        _require(
            face_owner_id not in face_owner_ids,
            component + f": face owner id repeats at span {index}",
        )
        face_owner_ids.add(face_owner_id)
        face_key = (*body_key, face_index)
        _require(face_key not in face_keys, component + f": duplicate native face {face_key}")
        face_keys.add(face_key)
        bodies_by_face.setdefault(body_key, []).append(face_index)
        face_spans_by_key[face_key] = span
        spans_by_body.setdefault(body_key, []).append(span)
        face_edge_references[face_key] = set(edge_references)
        previous = order
        next_start += count
    _require(
        _integer(triangles) and next_start == triangles,
        component + ": face spans do not cover the complete native STL triangle stream",
    )
    expected_bodies = {
        (body_type, body_index)
        for body_type, count in ((0, bodies["solid"]), (1, bodies["sheet"]))
        for body_index in range(count)
    }
    _require(
        set(bodies_by_face) == expected_bodies,
        component + ": face spans do not cover every native body",
    )
    _require(
        all(indices == list(range(len(indices))) for indices in bodies_by_face.values()),
        component + ": each native body must expose a contiguous face-index sequence",
    )
    _require(
        len(set(body_references.values())) == len(expected_bodies),
        component + ": a persistent body reference is missing or reused",
    )
    native_edges = tessellation["native_edges"]
    _require(isinstance(native_edges, list), component + ": native edge evidence is missing")
    edge_required = {
        "body_type",
        "body_index",
        "edge_reference_sha256",
        "native_edge_reference_sha256",
        "edge_identity_mode",
        "edge_identity",
        "mapping_source_edge_index",
        "mapping_source_edge_persistent_reference_sha256",
        "start_vertex_reference_sha256",
        "end_vertex_reference_sha256",
        "start_point_m",
        "end_point_m",
        "start_vertex_error",
        "end_vertex_error",
        "start_point_error",
        "end_point_error",
        "start_vertex_state",
        "end_vertex_state",
        "start_reference_error",
        "end_reference_error",
        "face_indices",
        "fin_ids",
        "u_min",
        "u_max",
        "curve_start_point_m",
        "curve_end_point_m",
        "curve_sense",
        "curve_type",
        "curve_closed",
        "curve_periodic",
        "curve_end_params",
        "curve_domain_transform",
        "curve_endpoint_evaluations",
        "topology_endpoint_assignment",
        "endpoint_points_derived_from_curve",
        "face_senses",
    }
    edge_owners = {}
    seen_edges = set()
    vertex_points = {}
    for index, edge in enumerate(native_edges):
        _require(
            isinstance(edge, dict) and frozenset(edge) == edge_required,
            component + f": native edge {index} has an unknown shape",
        )
        _require(
            isinstance(edge["face_indices"], list)
            and bool(edge["face_indices"]),
            component + f": native edge {index} is malformed",
        )
        _require(
            len(edge["face_indices"]) <= 2,
            component + f": native edge {index} is non-manifold",
        )
        reference = edge["edge_reference_sha256"]
        native_reference = edge["native_edge_reference_sha256"]
        identity_mode = edge["edge_identity_mode"]
        identity = edge["edge_identity"]
        mapped_source_index = edge["mapping_source_edge_index"]
        mapped_source_reference = edge[
            "mapping_source_edge_persistent_reference_sha256"
        ]
        start_reference = edge["start_vertex_reference_sha256"]
        end_reference = edge["end_vertex_reference_sha256"]
        start_point = edge["start_point_m"]
        end_point = edge["end_point_m"]
        endpoint_errors = (
            edge["start_vertex_error"],
            edge["end_vertex_error"],
            edge["start_point_error"],
            edge["end_point_error"],
            edge["start_reference_error"],
            edge["end_reference_error"],
        )
        _require(
            _integer(edge["body_type"])
            and edge["body_type"] in (0, 1)
            and _integer(edge["body_index"])
            and edge["body_index"] >= 0
            and isinstance(reference, str)
            and bool(HEX64.fullmatch(reference))
            and isinstance(native_reference, str)
            and bool(HEX64.fullmatch(native_reference))
            and identity_mode
            in {
                NATIVE_PERSISTENT_IDENTITY_RULE,
                NATIVE_SCOPE_IDENTITY_RULE,
            }
            and (
                (
                    mapped_source_index is None
                    and mapped_source_reference is None
                )
                or (
                    _integer(mapped_source_index)
                    and mapped_source_index >= 0
                    and isinstance(mapped_source_reference, str)
                    and bool(HEX64.fullmatch(mapped_source_reference))
                )
            )
            and (
                start_reference is None
                or (
                    isinstance(start_reference, str)
                    and bool(HEX64.fullmatch(start_reference))
                )
            )
            and (
                end_reference is None
                or (
                    isinstance(end_reference, str)
                    and bool(HEX64.fullmatch(end_reference))
                )
            )
            and (
                start_point is None
                or (
                    isinstance(start_point, list)
                    and len(start_point) == 3
                    and all(
                        type(value) in (int, float)
                        and math.isfinite(value)
                        for value in start_point
                    )
                )
            )
            and (
                end_point is None
                or (
                    isinstance(end_point, list)
                    and len(end_point) == 3
                    and all(
                        type(value) in (int, float)
                        and math.isfinite(value)
                        for value in end_point
                    )
                )
            )
            and all(
                error is None or isinstance(error, str)
                for error in endpoint_errors
            )
            and edge["start_vertex_state"]
            in {"present", "absent", "read_failed"}
            and edge["end_vertex_state"]
            in {"present", "absent", "read_failed"}
            and type(edge["endpoint_points_derived_from_curve"]) is bool
            and isinstance(edge["face_indices"], list)
            and bool(edge["face_indices"])
            and edge["face_indices"] == sorted(set(edge["face_indices"]))
            and all(_integer(value) and value >= 0 for value in edge["face_indices"])
            and isinstance(edge["fin_ids"], list)
            and edge["fin_ids"] == sorted(set(edge["fin_ids"]))
            and all(_integer(value) and value >= 0 for value in edge["fin_ids"])
            and type(edge["u_min"]) in (int, float)
            and type(edge["u_max"]) in (int, float)
            and math.isfinite(edge["u_min"])
            and math.isfinite(edge["u_max"])
            and edge["u_min"] < edge["u_max"]
            and isinstance(edge["curve_start_point_m"], list)
            and len(edge["curve_start_point_m"]) == 3
            and all(
                type(value) in (int, float)
                and math.isfinite(value)
                for value in edge["curve_start_point_m"]
            )
            and isinstance(edge["curve_end_point_m"], list)
            and len(edge["curve_end_point_m"]) == 3
            and all(
                type(value) in (int, float)
                and math.isfinite(value)
                for value in edge["curve_end_point_m"]
            )
            and type(edge["curve_sense"]) is bool
            and _integer(edge["curve_type"])
            and edge["curve_type"] >= 0
            and isinstance(edge["curve_end_params"], dict)
            and isinstance(edge["curve_domain_transform"], dict)
            and isinstance(edge["face_senses"], list),
            component + f": native edge {index} is malformed",
        )
        body_key = (edge["body_type"], edge["body_index"])
        if identity_mode == NATIVE_PERSISTENT_IDENTITY_RULE:
            _require(
                identity is None,
                component
                + f": native edge {index} carries a derived identity "
                + "without an alias",
            )
        else:
            _parameter_endpoints, topology_endpoints = (
                native_curve_endpoint_roles(
                    edge["curve_start_point_m"],
                    edge["curve_end_point_m"],
                    edge["curve_sense"],
                )
            )
            identity_start_point = (
                start_point
                if start_point is not None
                else topology_endpoints[0]
            )
            identity_end_point = (
                end_point
                if end_point is not None
                else topology_endpoints[1]
            )
            adjacent_face_identities = [
                face_spans_by_key[(*body_key, face_index)][
                    "face_identity"
                ]
                for face_index in edge["face_indices"]
            ]
            expected_face_ids = (
                sorted(
                    face_identity["mapping_source_face_index"]
                    for face_identity in adjacent_face_identities
                )
                if all(
                    face_identity is not None
                    for face_identity in adjacent_face_identities
                )
                else identity["adjacent_mapped_face_ids"]
            )
            expected_topology = native_scope_identity_sha256(
                {
                    "start_point_binary64": (
                        None
                        if start_point is None
                        else [
                            struct.pack("<d", float(value)).hex()
                            for value in start_point
                        ]
                    ),
                    "end_point_binary64": (
                        None
                        if end_point is None
                        else [
                            struct.pack("<d", float(value)).hex()
                            for value in end_point
                        ]
                    ),
                    "curve_u_min_binary64": struct.pack(
                        "<d", float(edge["u_min"])
                    ).hex(),
                    "curve_u_max_binary64": struct.pack(
                        "<d", float(edge["u_max"])
                    ).hex(),
                    "curve_start_point_binary64": [
                        struct.pack("<d", float(value)).hex()
                        for value in edge["curve_start_point_m"]
                    ],
                    "curve_end_point_binary64": [
                        struct.pack("<d", float(value)).hex()
                        for value in edge["curve_end_point_m"]
                    ],
                    "curve_sense": edge["curve_sense"],
                    "curve_type": edge["curve_type"],
                    "curve_closed": edge["curve_closed"],
                    "curve_periodic": edge["curve_periodic"],
                    "adjacent_mapped_face_ids": expected_face_ids,
                }
            )
            _require(
                _valid_scope_identity(
                    identity,
                    kind="edge",
                    component=component,
                )
                and identity["native_edge_index"] == index
                and identity["start_point_binary64_hex"]
                == [
                    struct.pack("<d", float(value)).hex()
                    for value in identity_start_point
                ]
                and identity["end_point_binary64_hex"]
                == [
                    struct.pack("<d", float(value)).hex()
                    for value in identity_end_point
                ]
                and identity["start_vertex_reference_sha256"]
                == edge["start_vertex_reference_sha256"]
                and identity["end_vertex_reference_sha256"]
                == edge["end_vertex_reference_sha256"]
                and identity["curve_type"] == edge["curve_type"]
                and identity["curve_sense"] is edge["curve_sense"]
                and identity["curve_domain_binary64_hex"]
                == [
                    struct.pack("<d", float(edge["u_min"])).hex(),
                    struct.pack("<d", float(edge["u_max"])).hex(),
                ]
                and identity["curve_closed"] is edge["curve_closed"]
                and identity["curve_periodic"] is edge["curve_periodic"]
                and identity["adjacent_mapped_face_ids"]
                == expected_face_ids
                and (
                    face_spans_by_key[
                        (*body_key, edge["face_indices"][0])
                    ]["face_identity"]
                    is None
                    or identity["owner_scope_key"]
                    == face_spans_by_key[
                        (*body_key, edge["face_indices"][0])
                    ]["face_identity"]["owner_scope_key"]
                )
                and reference == native_scope_identity_sha256(identity)
                and mapped_source_index
                == identity["mapping_source_edge_index"]
                and mapped_source_reference
                == identity[
                    "mapping_source_edge_persistent_reference_sha256"
                ]
                and identity["topology_signature_sha256"]
                == expected_topology,
                component + f": native edge {index} scope identity is invalid",
            )
        end_params = edge["curve_end_params"]
        _require(
            set(end_params)
            == {
                "call_form",
                "success",
                "error",
                "start",
                "end",
                "is_closed",
                "is_periodic",
                "raw_outputs",
            },
            component
            + f": native edge {index} periodicity shape is unsupported",
        )
        if end_params["success"] is True:
            _require(
                end_params["call_form"] == "pywin32-byref-variant"
                and end_params["error"] is None
                and type(end_params["start"]) in (int, float)
                and type(end_params["end"]) in (int, float)
                and math.isfinite(end_params["start"])
                and math.isfinite(end_params["end"])
                and type(end_params["is_closed"]) is bool
                and type(end_params["is_periodic"]) is bool
                and edge["curve_closed"] is end_params["is_closed"]
                and edge["curve_periodic"] is end_params["is_periodic"],
                component
                + f": native edge {index} curve periodicity is inconsistent",
            )
        else:
            _require(
                end_params["success"] is False
                and isinstance(end_params["error"], str)
                and bool(end_params["error"])
                and end_params["start"] is None
                and end_params["end"] is None
                and end_params["is_closed"] is None
                and end_params["is_periodic"] is None
                and end_params["call_form"] == "pywin32-byref-variant"
                and edge["curve_closed"] is None
                and edge["curve_periodic"] is None
                and end_params["raw_outputs"] == [],
                component
                + f": native edge {index} unreadable periodicity is not explicit",
            )
        if end_params["success"] is True:
            raw_outputs = end_params["raw_outputs"]
            _require(
                isinstance(raw_outputs, list)
                and len(raw_outputs) == 4
                and all(
                    isinstance(row, dict)
                    and set(row) == {"value_type", "value"}
                    for row in raw_outputs
                )
                and [row["value_type"] for row in raw_outputs]
                == [0x4005, 0x4005, 0x400B, 0x400B]
                and raw_outputs[0]["value"] == end_params["start"]
                and raw_outputs[1]["value"] == end_params["end"]
                and raw_outputs[2]["value"] is end_params["is_closed"]
                and raw_outputs[3]["value"] is end_params["is_periodic"],
                component
                + f": native edge {index} periodicity raw result differs",
            )
        _require(
            end_params["success"] is True,
            component
            + f": native edge {index} periodicity is unresolved",
        )
        expected_transform = native_curve_domain_transform(
            end_params["start"],
            end_params["end"],
            edge["u_min"],
            edge["u_max"],
            edge["curve_sense"],
            end_params["is_periodic"],
        )
        endpoint_states = {
            "start": edge["start_vertex_state"],
            "end": edge["end_vertex_state"],
        }
        if end_params["is_periodic"] is False:
            allowed_transform_statuses = (
                {"edge_local_nonperiodic"}
                if (
                    set(endpoint_states.values()) == {"present"}
                    or (
                        set(endpoint_states.values()) == {"absent"}
                        and end_params["is_closed"] is True
                    )
                )
                else set()
            )
        else:
            allowed_transform_statuses = {
                "contained",
                "contained_seam_wrapped",
            }
        _require(
            edge["curve_domain_transform"] == expected_transform
            and expected_transform["status"] in allowed_transform_statuses,
            component
            + f": native edge {index} has no valid native curve-domain proof",
        )
        evaluations = edge["curve_endpoint_evaluations"]
        _require(
            isinstance(evaluations, dict)
            and set(evaluations) == {"start", "end"}
            and _valid_curve_endpoint_evaluation(
                evaluations["start"],
                edge["u_min"],
            )
            and _valid_curve_endpoint_evaluation(
                evaluations["end"],
                edge["u_max"],
            ),
            component
            + f": native edge {index} curve endpoint evaluation is malformed",
        )
        parameter_endpoints, _ = native_curve_endpoint_roles(
            edge["curve_start_point_m"],
            edge["curve_end_point_m"],
            edge["curve_sense"],
        )
        for index, (name, expected_point) in enumerate(
            zip(
                ("start", "end"),
                parameter_endpoints,
                strict=True,
            )
        ):
            evaluation = evaluations[name]
            evaluated_point = evaluation["payload"][:3]
            opposite_point = parameter_endpoints[1 - index]
            computed_tolerance = native_curve_endpoint_operation_tolerance(
                expected_point,
                evaluated_point,
                opposite_point,
            )
            computed_error = native_point_error(
                expected_point,
                evaluated_point,
            )
            _require(
                evaluation["point_tolerance_m"] == computed_tolerance
                and evaluation["point_error_m"] == computed_error
                and computed_error <= computed_tolerance,
                component
                + f": native edge {index} {name} endpoint evaluation is not independently reproducible",
            )

        if "read_failed" in endpoint_states.values():
            _require(
                False,
                component + f": native edge {index} present vertex failed GetPoint",
            )
        absent_count = sum(
            state == "absent" for state in endpoint_states.values()
        )
        if absent_count == 1:
            _require(
                False,
                component + f": native edge {index} has one absent endpoint",
            )
        if absent_count == 2:
            start_evaluated = tuple(evaluations["start"]["payload"][:3])
            end_evaluated = tuple(evaluations["end"]["payload"][:3])
            closure_tolerance = native_point_roundtrip_tolerance(
                start_evaluated,
                end_evaluated,
            )
            closure_error = native_point_error(
                start_evaluated,
                end_evaluated,
            )
            _require(
                edge.get("topology_endpoint_assignment") is None,
                component
                + f": native edge {index} absent endpoints carry a topology assignment",
            )
            _require(
                edge["endpoint_points_derived_from_curve"] is True
                and end_params["is_closed"] is True
                and start_reference is None
                and end_reference is None
                and start_point is None
                and end_point is None
                and edge["start_point_error"] == "vertex_absent"
                and edge["end_point_error"] == "vertex_absent"
                and closure_error <= closure_tolerance,
                component
                + f": native edge {index} absent endpoints lack closed-curve derivation",
            )
        else:
            _require(
                edge["endpoint_points_derived_from_curve"] is False
                and edge["topology_endpoint_assignment"] is not None
                and isinstance(start_reference, str)
                and isinstance(end_reference, str)
                and isinstance(start_point, list)
                and isinstance(end_point, list)
                and edge["start_point_error"] is None
                and edge["end_point_error"] is None,
                component
                + f": native edge {index} present endpoint evidence is incomplete",
            )
        body_key = (edge["body_type"], edge["body_index"])
        _require(body_key in expected_bodies, component + f": native edge {index} names an unknown body")
        _require(reference not in seen_edges, component + f": native edge {index} is duplicated")
        seen_edges.add(reference)
        for vertex_reference, point in (
            (start_reference, start_point),
            (end_reference, end_point),
        ):
            if vertex_reference is None or point is None:
                continue
            observed = vertex_points.setdefault(vertex_reference, tuple(point))
            _require(
                observed == tuple(point),
                component + f": native vertex {vertex_reference} binds multiple coordinates",
            )
        edge_owners[reference] = (body_key, set(edge["face_indices"]))
        face_senses = edge["face_senses"]
        _require(
            len(face_senses) == len(edge["face_indices"])
            and [
                row.get("face_index") if isinstance(row, dict) else None
                for row in face_senses
            ]
            == edge["face_indices"],
            component + f": native edge {index} face-sense inventory is inconsistent",
        )
        sense_values = []
        for row in face_senses:
            _require(
                isinstance(row, dict)
                and set(row)
                == {
                    "face_index",
                    "EdgeInFaceSense",
                    "IEdgeInFaceSense2",
                },
                component + f": native edge {index} face-sense row is malformed",
            )
            primary = row["EdgeInFaceSense"]
            _require(
                isinstance(primary, dict)
                and set(primary) == {"value", "error"}
                and primary["error"] is None
                and type(primary["value"]) is bool,
                component
                + f": native edge {index} primary orientation is unreadable",
            )
            secondary = row["IEdgeInFaceSense2"]
            _require(
                isinstance(secondary, dict)
                and set(secondary) == {"value", "error"}
                and (
                    (
                        secondary["error"] is None
                        and type(secondary["value"]) is bool
                        and secondary["value"] is primary["value"]
                    )
                    or (
                        _write_only_sense_error(secondary["error"])
                        and secondary["value"] is None
                    )
                ),
                component + f": native edge {index} secondary orientation is invalid",
            )
            sense_values.append(primary["value"])
        _require(
            len(sense_values) == 1 or sense_values[0] is not sense_values[1],
            component + f": native edge {index} cofaces have the same orientation",
        )
        _require(
            all((*body_key, face_index) in face_edge_references for face_index in edge_owners[reference][1]),
            component + f": native edge {index} names an unknown face",
        )
    for face_key, references in face_edge_references.items():
        body_key = face_key[:2]
        for reference in references:
            owner = edge_owners.get(reference)
            _require(
                owner is not None
                and owner[0] == body_key
                and face_key[2] in owner[1],
                component + f": face {face_key} has no matching native edge incidence for {reference}",
            )
    for reference, (body_key, face_indices) in edge_owners.items():
        _require(
            all(
                reference in face_edge_references[(*body_key, face_index)]
                for face_index in face_indices
            ),
            component + ": native edge incidence is not bound to every declared face",
        )
    _require(
        not (
            set(body_references.values())
            & face_references
            or set(body_references.values()) & seen_edges
            or face_references & seen_edges
            or set(body_references.values()) & set(vertex_points)
            or face_references & set(vertex_points)
            or seen_edges & set(vertex_points)
        ),
        component + ": body, face, edge and vertex identities must not alias",
    )
    _parse_itessellation_evidence(
        component,
        tessellation["itessellation"],
        spans_by_body,
        face_spans_by_key,
        native_edges,
        body_references,
        expected_bodies,
        triangles,
        triangle_vertices,
        native_fin_resolutions,
        native_identity_warnings,
    )
    return spans


def _parse_itessellation_evidence(
    component: str,
    value: object,
    spans_by_body: dict,
    face_spans_by_key: dict,
    native_edges: list[dict],
    body_references: dict,
    expected_bodies: set,
    triangles: int,
    triangle_vertices: list[tuple[tuple[float, float, float], ...]] | None,
    native_fin_resolutions: list[dict] | None,
    native_identity_warnings: list[dict] | None,
) -> None:
    """Validate body-local ITessellation facet/fin evidence fail closed."""

    required = {
        "interface_iid",
        "u_rule_version",
        "u_resolution_rule",
        "point_roundtrip_rule",
        "properties",
        "identity_rule",
        "bodies",
        "vertices",
        "solidworks_vertex_identities",
        "reference_alias_audit",
        "cross_kind_raw_aliases",
        "u_observation_stats",
    }
    _require(
        isinstance(value, dict)
        and set(value) == required,
        component + ": native ITessellation evidence is missing",
    )
    audit = value["reference_alias_audit"]
    _require(
        isinstance(audit, dict)
        and set(audit) == {"body", "face", "edge", "vertex"}
        and all(
            isinstance(references, list)
            and references == sorted(set(references))
            and all(
                isinstance(reference, str)
                and bool(HEX64.fullmatch(reference))
                for reference in references
            )
            for references in audit.values()
        ),
        component + ": native persistent-reference alias audit is malformed",
    )
    aliases = value["cross_kind_raw_aliases"]
    _require(
        isinstance(aliases, list),
        component + ": native cross-kind raw alias audit is missing",
    )
    if aliases is not None:
        base_owner_keys = {
            "kind",
            "component",
            "body_type",
            "body_index",
            "typed_sha256",
        }
        kind_owner_keys = {
            "body": base_owner_keys,
            "face": base_owner_keys | {"face_index"},
            "edge": base_owner_keys | {"edge_index"},
            "vertex": base_owner_keys | {"vertex_index"},
        }
        _require(
            isinstance(aliases, list)
            and all(
                isinstance(row, dict)
                and set(row) == {"raw_sha256", "owners"}
                and isinstance(row["raw_sha256"], str)
                and bool(HEX64.fullmatch(row["raw_sha256"]))
                and isinstance(row["owners"], list)
                and bool(row["owners"])
                and all(
                    isinstance(owner, dict)
                    and owner.get("kind") in {"body", "face", "edge", "vertex"}
                    and set(owner) == kind_owner_keys[owner["kind"]]
                    and isinstance(owner["component"], str)
                    and bool(owner["component"])
                    and _integer(owner["body_type"])
                    and owner["body_type"] >= 0
                    and _integer(owner["body_index"])
                    and owner["body_index"] >= 0
                    and isinstance(owner["typed_sha256"], str)
                    and bool(HEX64.fullmatch(owner["typed_sha256"]))
                    and (
                        owner["kind"] != "face"
                        or (_integer(owner["face_index"]) and owner["face_index"] >= 0)
                    )
                    and (
                        owner["kind"] != "edge"
                        or (_integer(owner["edge_index"]) and owner["edge_index"] >= 0)
                    )
                    and (
                        owner["kind"] != "vertex"
                        or (
                            _integer(owner["vertex_index"])
                            and owner["vertex_index"] >= 0
                        )
                    )
                    for owner in row["owners"]
                )
                and len({owner["typed_sha256"] for owner in row["owners"]})
                == len(row["owners"])
                for row in aliases
            ),
            component + ": native persistent-reference raw-alias evidence is malformed",
        )
        for row in aliases:
            _require(
                len({owner["kind"] for owner in row["owners"]}) > 1,
                component + ": native persistent-reference alias does not cross kinds",
            )
            body_owners = {
                (owner["component"], owner["body_type"], owner["body_index"])
                for owner in row["owners"]
                if owner["kind"] == "body"
            }
            _require(
                len(body_owners) <= 1
                and all(
                    (owner["component"], owner["body_type"], owner["body_index"])
                    in body_owners
                    for owner in row["owners"]
                    if owner["kind"] != "body"
                )
                if body_owners
                else True,
                component
                + ": native persistent-reference raw alias crosses native body identities",
            )
        owner_order = [
            (
                row["raw_sha256"],
                tuple(
                    (
                        owner["kind"],
                        owner["component"],
                        owner["body_type"],
                        owner["body_index"],
                        owner.get("face_index", -1),
                        owner.get("edge_index", -1),
                        owner.get("vertex_index", -1),
                    )
                    for owner in row["owners"]
                ),
            )
            for row in aliases
        ]
        _require(
            owner_order == sorted(owner_order),
            component + ": native persistent-reference raw-alias evidence is not canonical",
        )
    _require(
        value["interface_iid"] == ITESSELLATION_IID,
        component + ": native ITessellation interface is unsupported",
    )
    _require(
        value["u_rule_version"] == NATIVE_U_IDENTITY_RULE,
        component + ": native U identity rule is unsupported",
    )
    _require(
        value["u_resolution_rule"] == NATIVE_U_RESOLUTION_RULE,
        component + ": native U resolution rule is unsupported",
    )
    _require(
        value["point_roundtrip_rule"] == NATIVE_POINT_ROUNDTRIP_RULE,
        component + ": native point round-trip rule is unsupported",
    )
    _require(
        value["identity_rule"] == NATIVE_IDENTITY_POLICY_RULE,
        component + ": native identity policy is unsupported",
    )
    stats = value["u_observation_stats"]
    _require(
        isinstance(stats, dict)
        and set(stats)
        == {
            "unique_observations",
            "cache_hits",
            "get_closest_point_on_calls",
            "get_parameter_calls",
            "evaluate2_calls",
        }
        and all(_integer(item) and item >= 0 for item in stats.values())
        and stats["get_closest_point_on_calls"]
        == stats["unique_observations"]
        and stats["get_parameter_calls"] == stats["unique_observations"]
        and stats["evaluate2_calls"] == stats["unique_observations"],
        component + ": native U observation statistics are inconsistent",
    )
    properties = value["properties"]
    expected_boolean_properties = (
        "need_face_facet_map",
        "need_edge_fin_map",
        "need_vertex_params",
    )
    _require(
        isinstance(properties, dict)
        and set(properties) == {"match_type", *expected_boolean_properties}
        and type(properties["match_type"]) is int
        and properties["match_type"] == 0
        and all(
            type(properties[key]) is bool and properties[key] is True
            for key in expected_boolean_properties
        ),
        component + ": native ITessellation properties are incomplete",
    )
    if triangle_vertices is not None:
        _require(
            isinstance(triangle_vertices, list)
            and len(triangle_vertices) == triangles
            and all(
                isinstance(triangle, (list, tuple))
                and len(triangle) == 3
                and all(
                    isinstance(point, (list, tuple))
                    and len(point) == 3
                    and all(
                        type(coordinate) in (int, float)
                        and math.isfinite(coordinate)
                        for coordinate in point
                    )
                    for point in triangle
                )
                for triangle in triangle_vertices
            ),
            component + ": native STL triangle vertices are unavailable for v6 binding",
        )
    raw_bodies = value["bodies"]
    _require(
        isinstance(raw_bodies, list) and bool(raw_bodies),
        component + ": native tessellation v6 body evidence is missing",
    )

    body_evidence = {}
    body_counts = {}
    previous_body = None
    for index, body in enumerate(raw_bodies):
        _require(
            isinstance(body, dict)
            and set(body)
            == {
                "body_type",
                "body_index",
                "body_reference_sha256",
                "tessellate_result",
                "request",
                "properties",
                "facet_count",
                "fin_count",
                "vertex_count",
                "faces",
                "facets",
                "fins",
            },
            component + f": native tessellation v6 body {index} has an unknown shape",
        )
        body_key = (body["body_type"], body["body_index"])
        _require(
            _integer(body_key[0])
            and body_key[0] in (0, 1)
            and _integer(body_key[1])
            and body_key[1] >= 0
            and body_key in expected_bodies,
            component + f": native tessellation v6 body {index} is unknown",
        )
        _require(
            previous_body is None or body_key > previous_body,
            component + f": native tessellation v6 body {index} is duplicated or unordered",
        )
        previous_body = body_key
        body_face_references = tuple(
            span["face_reference_sha256"]
            for span in sorted(
                spans_by_body[body_key],
                key=lambda item: item["face_index"],
            )
        )
        _require(
            body["body_reference_sha256"] == body_references[body_key]
            and body["tessellate_result"] is True
            and body["request"]
            == {
                "api": "IBody2.GetTessellation",
                "argument_count": 1,
                "face_list": {
                    "scope": "whole_body",
                    "variant_type": "VT_ARRAY|VT_DISPATCH",
                    "face_count": len(body_face_references),
                    "first_face_reference_sha256": body_face_references[0],
                    "last_face_reference_sha256": body_face_references[-1],
                },
            }
            and body["properties"] == properties
            and _integer(body["facet_count"])
            and body["facet_count"] >= 0
            and _integer(body["fin_count"])
            and body["fin_count"] >= 0
            and _integer(body["vertex_count"])
            and body["vertex_count"] >= 0
            and isinstance(body["faces"], list)
            and isinstance(body["facets"], list)
            and isinstance(body["fins"], list),
            component + f": native tessellation v6 body {body_key} is incomplete",
        )
        body_evidence[body_key] = _parse_itessellation_body(
            component,
            body,
            spans_by_body[body_key],
            face_spans_by_key,
        )
        body_counts[body_key] = {
            "facet_count": body["facet_count"],
            "fin_count": body["fin_count"],
            "vertex_count": body["vertex_count"],
        }
    _require(
        set(body_evidence) == expected_bodies,
        component + ": native tessellation v6 evidence does not cover every body",
    )

    raw_vertices = value["vertices"]
    _require(
        isinstance(raw_vertices, list),
        component + ": native tessellation v6 vertex point cache is missing",
    )
    vertices = {}
    previous_vertex = None
    for index, vertex in enumerate(raw_vertices):
        _require(
            isinstance(vertex, dict)
            and set(vertex) == {"body_type", "body_index", "vertex_id", "point_m"},
            component + f": native tessellation v6 vertex {index} has an unknown shape",
        )
        body_key = (vertex["body_type"], vertex["body_index"])
        vertex_id = vertex["vertex_id"]
        point = vertex["point_m"]
        _require(
            body_key in body_evidence
            and _integer(vertex_id)
            and vertex_id >= 0
            and isinstance(point, list)
            and len(point) == 3
            and all(
                type(coordinate) in (int, float) and math.isfinite(coordinate)
                for coordinate in point
            ),
            component + f": native tessellation v6 vertex {index} is malformed",
        )
        order = (*body_key, vertex_id)
        _require(
            previous_vertex is None or order > previous_vertex,
            component + f": native tessellation v6 vertex {index} is duplicated or unordered",
        )
        previous_vertex = order
        vertices[(body_key, vertex_id)] = tuple(point)

    raw_scope_vertices = value["solidworks_vertex_identities"]
    _require(
        isinstance(raw_scope_vertices, list),
        component + ": native scope-local vertex identities are missing",
    )
    scope_vertex_keys = set()
    previous_scope_vertex = None
    edges_by_owner_reference = {
        (
            edge["body_type"],
            edge["body_index"],
            edge["edge_reference_sha256"],
        ): edge
        for edge in native_edges
    }
    for index, row in enumerate(raw_scope_vertices):
        _require(
            isinstance(row, dict)
            and set(row)
            == {
                "body_type",
                "body_index",
                "vertex_index",
                "point_m",
                "identity_mode",
                "native_reference_sha256",
                "identity",
                "identity_sha256",
            },
            component
            + f": native scope-local vertex {index} has an unknown shape",
        )
        body_key = (row["body_type"], row["body_index"])
        identity_mode = row["identity_mode"]
        identity = row["identity"]
        point = row["point_m"]
        identity_valid = (
            identity_mode == NATIVE_PERSISTENT_IDENTITY_RULE
            and identity is None
        ) or (
            identity_mode == NATIVE_SCOPE_IDENTITY_RULE
            and _valid_scope_identity(
                identity,
                kind="vertex",
                component=component,
            )
            and identity["native_vertex_index"] == row["vertex_index"]
            and identity["point_binary64_hex"]
            == [
                struct.pack("<d", float(value)).hex()
                for value in point
            ]
            and identity["owner_scope_key"]
            == (
                spans_by_body[body_key][0]["face_identity"][
                    "owner_scope_key"
                ]
                if spans_by_body[body_key][0]["face_identity"] is not None
                else identity["owner_scope_key"]
            )
            and row["identity_sha256"]
            == native_scope_identity_sha256(identity)
        )
        _require(
            body_key in body_evidence
            and _integer(row["vertex_index"])
            and row["vertex_index"] >= 0
            and isinstance(point, list)
            and len(point) == 3
            and all(
                type(value) in (int, float) and math.isfinite(value)
                for value in point
            )
            and identity_mode
            in {
                NATIVE_PERSISTENT_IDENTITY_RULE,
                NATIVE_SCOPE_IDENTITY_RULE,
            }
            and identity_valid
            and isinstance(row["identity_sha256"], str)
            and bool(HEX64.fullmatch(row["identity_sha256"]))
            and isinstance(row["native_reference_sha256"], str)
            and bool(HEX64.fullmatch(row["native_reference_sha256"]))
            and identity_valid,
            component
            + f": native scope-local vertex {index} is malformed",
        )
        order = (*body_key, row["vertex_index"])
        _require(
            previous_scope_vertex is None or order > previous_scope_vertex,
            component
            + f": native scope-local vertex {index} is duplicated or unordered",
        )
        previous_scope_vertex = order
        _require(
            (*body_key, row["identity_sha256"]) not in scope_vertex_keys,
            component
            + f": native scope-local vertex identity repeats at {index}",
        )
        scope_vertex_keys.add(
            (*body_key, row["identity_sha256"])
        )
        if identity is not None:
            scope_edges_by_source = {}
            for edge in native_edges:
                edge_identity = edge["edge_identity"]
                source_index = (
                    edge_identity["mapping_source_edge_index"]
                    if edge_identity is not None
                    else edge["mapping_source_edge_index"]
                )
                source_reference = (
                    edge_identity[
                        "mapping_source_edge_persistent_reference_sha256"
                    ]
                    if edge_identity is not None
                    else edge[
                        "mapping_source_edge_persistent_reference_sha256"
                    ]
                )
                if source_index is None:
                    _require(
                        source_reference is None,
                        component
                        + ": native edge has a partial part-witness mapping",
                    )
                    continue
                key = (
                    edge["body_type"],
                    edge["body_index"],
                    source_index,
                )
                _require(
                    key not in scope_edges_by_source,
                    component
                    + ": native part edge witness is reused",
                )
                scope_edges_by_source[key] = (
                    edge,
                    source_reference,
                )
            incident_edges = []
            for edge_index, edge_reference in zip(
                identity["incident_mapped_edge_indices"],
                identity[
                    "incident_mapped_edge_reference_sha256"
                ],
                strict=True,
            ):
                mapped = scope_edges_by_source.get(
                    (*body_key, edge_index)
                )
                _require(
                    mapped is not None
                    and mapped[1] == edge_reference,
                    component
                    + f": native scope-local vertex {index} names an unknown mapped edge",
                )
                incident_edges.append(mapped[0])
        else:
            incident_edges = [
                edge
                for (edge_body_type, edge_body_index, _), edge
                in edges_by_owner_reference.items()
                if (edge_body_type, edge_body_index) == body_key
                and row["identity_sha256"]
                in {
                    edge["start_vertex_reference_sha256"],
                    edge["end_vertex_reference_sha256"],
                }
            ]
        _require(
            incident_edges
            and all(edge is not None for edge in incident_edges)
            and (
                (
                    identity is not None
                    and all(
                        row["identity_sha256"]
                        in {
                            edge["start_vertex_reference_sha256"],
                            edge["end_vertex_reference_sha256"],
                        }
                        for edge in incident_edges
                    )
                )
                or (
                    identity is None
                    and all(
                    (
                        edge["start_vertex_reference_sha256"]
                        == row["identity_sha256"]
                    )
                    or (
                        edge["end_vertex_reference_sha256"]
                        == row["identity_sha256"]
                    )
                    for edge in incident_edges
                    )
                )
            ),
            component
            + f": native scope-local vertex {index} is not bound to its edges",
        )

    def expected_body_aliases(
        kind: str,
        rows: list[dict],
    ) -> set[str]:
        by_body = {}
        for row in rows:
            by_body.setdefault(
                (row["body_type"], row["body_index"]),
                [],
            ).append(row)
        aliases = set()
        for body_key, body_rows in by_body.items():
            scope_rows = [
                row
                for row in body_rows
                if row["identity"] is not None
            ]
            _require(
                not scope_rows or len(scope_rows) == len(body_rows),
                component
                + f": {kind} identity modes are mixed for body {body_key}",
            )
            identities_by_raw = {}
            for row in body_rows:
                identities_by_raw.setdefault(
                    row["native_reference_sha256"],
                    set(),
                ).add(
                    row.get("identity_sha256")
                    or row.get("face_reference_sha256")
                    or row.get("edge_reference_sha256")
                )
            reused = {
                raw_sha256
                for raw_sha256, identities in identities_by_raw.items()
                if len(identities) > 1
            }
            _require(
                (
                    not reused
                    and not scope_rows
                )
                or (
                    reused
                    and scope_rows
                    and len(scope_rows) == len(body_rows)
                    and len(identities_by_raw) == 1
                ),
                component
                + f": {kind} identity mode does not match reference reuse "
                + f"for body {body_key}",
            )
            aliases.update(reused)
            for row in scope_rows:
                identity = row["identity"]
                warning_expected = (
                    NATIVE_COMPONENT_REFERENCE_ALIASED
                    in identity["warnings"]
                )
                _require(
                    warning_expected
                    is (
                        row["native_reference_sha256"] in reused
                    ),
                    component
                    + f": {kind} alias warning does not match raw reference reuse",
                )
        return aliases

    face_alias_rows = [
        {
            "body_type": span["body_type"],
            "body_index": span["body_index"],
            "face_reference_sha256": span["face_reference_sha256"],
            "native_reference_sha256": span[
                "native_face_reference_sha256"
            ],
            "identity_mode": span["face_identity_mode"],
            "identity": span["face_identity"],
        }
        for spans in spans_by_body.values()
        for span in spans
    ]
    edge_alias_rows = [
        {
            "body_type": edge["body_type"],
            "body_index": edge["body_index"],
            "edge_reference_sha256": edge["edge_reference_sha256"],
            "native_reference_sha256": edge[
                "native_edge_reference_sha256"
            ],
            "identity_mode": edge["edge_identity_mode"],
            "identity": edge["edge_identity"],
        }
        for edge in native_edges
    ]
    vertex_alias_rows = [
        {
            "body_type": row["body_type"],
            "body_index": row["body_index"],
            "identity_sha256": row["identity_sha256"],
            "native_reference_sha256": row[
                "native_reference_sha256"
            ],
            "identity_mode": row["identity_mode"],
            "identity": row["identity"],
        }
        for row in raw_scope_vertices
    ]
    owners_by_body = {}
    for row in [*face_alias_rows, *edge_alias_rows, *vertex_alias_rows]:
        if row["identity"] is None:
            continue
        owners_by_body.setdefault(
            (row["body_type"], row["body_index"]),
            set(),
        ).add(row["identity"]["owner_scope_key"])
    _require(
        all(len(keys) == 1 for keys in owners_by_body.values()),
        component
        + ": native scope-local identities disagree on one body scope",
    )
    scope_part_documents = {
        row["identity"]["mapping_source_part_document_sha256"]
        for row in [*face_alias_rows, *edge_alias_rows, *vertex_alias_rows]
        if row["identity"] is not None
    }
    _require(
        len(scope_part_documents) <= 1,
        component
        + ": native scope-local identities disagree on the part witness",
    )
    face_scope_source_documents = {
        row["identity"]["source_document_sha256"]
        for row in face_alias_rows
        if row["identity"] is not None
    }
    _require(
        not scope_part_documents
        or not face_scope_source_documents
        or scope_part_documents == face_scope_source_documents,
        component
        + ": the part witness does not match the frozen source document",
    )
    expected_cross_kind_owners = defaultdict(list)
    for body_key, body_spans in spans_by_body.items():
        first_span = body_spans[0]
        base_owner = {
            "component": component,
            "body_type": body_key[0],
            "body_index": body_key[1],
        }
        expected_cross_kind_owners[
            first_span["native_body_reference_sha256"]
        ].append(
            {
                "kind": "body",
                **base_owner,
                "typed_sha256": first_span["body_reference_sha256"],
            }
        )
        for span in body_spans:
            expected_cross_kind_owners[
                span["native_face_reference_sha256"]
            ].append(
                {
                    "kind": "face",
                    **base_owner,
                    "face_index": span["face_index"],
                    "typed_sha256": span["face_reference_sha256"],
                }
            )
    for edge_index, edge in enumerate(native_edges):
        expected_cross_kind_owners[
            edge["native_edge_reference_sha256"]
        ].append(
            {
                "kind": "edge",
                "component": component,
                "body_type": edge["body_type"],
                "body_index": edge["body_index"],
                "edge_index": edge_index,
                "typed_sha256": edge["edge_reference_sha256"],
            }
        )
    for row in raw_scope_vertices:
        expected_cross_kind_owners[row["native_reference_sha256"]].append(
            {
                "kind": "vertex",
                "component": component,
                "body_type": row["body_type"],
                "body_index": row["body_index"],
                "vertex_index": row["vertex_index"],
                "typed_sha256": row["identity_sha256"],
            }
        )

    def cross_kind_owner_key(owner):
        return (
            str(owner.get("component", "")),
            int(owner.get("body_type", -1)),
            int(owner.get("body_index", -1)),
            str(owner["kind"]),
            int(owner.get("face_index", -1)),
            int(owner.get("edge_index", -1)),
            str(owner.get("endpoint", "")),
        )

    expected_cross_kind_aliases = [
        {
            "raw_sha256": raw_sha256,
            "owners": sorted(owners, key=cross_kind_owner_key),
        }
        for raw_sha256, owners in sorted(
            expected_cross_kind_owners.items()
        )
        if len({owner["kind"] for owner in owners}) > 1
    ]
    actual_cross_kind_aliases = [
        {
            "raw_sha256": row["raw_sha256"],
            "owners": row["owners"],
        }
        for row in aliases
    ]
    _require(
        actual_cross_kind_aliases == expected_cross_kind_aliases,
        component
        + ": native cross-kind raw alias audit differs from raw references",
    )
    for entity_kind, rows, index_field, reference_field in (
        (
            "face",
            face_alias_rows,
            "mapping_source_face_index",
            "mapped_source_face_persistent_reference_sha256",
        ),
        (
            "edge",
            edge_alias_rows,
            "mapping_source_edge_index",
            "mapping_source_edge_persistent_reference_sha256",
        ),
        (
            "vertex",
            vertex_alias_rows,
            "mapping_source_vertex_index",
            "mapped_source_vertex_persistent_reference_sha256",
        ),
    ):
        by_body = {}
        for row in rows:
            if row["identity"] is None:
                continue
            by_body.setdefault(
                (row["body_type"], row["body_index"]),
                [],
            ).append(row["identity"])
        for body_key, identities in by_body.items():
            mapped_indices = [
                identity[index_field]
                for identity in identities
            ]
            mapped_references = [
                identity[reference_field]
                for identity in identities
            ]
            _require(
                len(mapped_indices) == len(set(mapped_indices))
                and len(mapped_references)
                == len(set(mapped_references))
                and set(mapped_indices)
                == set(range(len(mapped_indices))),
                component
                + f": {entity_kind} part witness mapping is not one-to-one "
                + f"for body {body_key}",
            )
        _require(
            native_mapped_witnesses_are_global_unique(
                rows,
                index_field,
                reference_field,
            ),
            component
            + f": {entity_kind} part witness repeats across bodies",
        )
    if native_identity_warnings is not None:
        grouped_warnings = {}
        for (
            entity_kind,
            rows,
            index_field,
            identity_index_field,
            mapped_reference_field,
        ) in (
            (
                "face",
                face_alias_rows,
                "native_face_index",
                "mapping_source_face_index",
                "mapped_source_face_persistent_reference_sha256",
            ),
            (
                "edge",
                edge_alias_rows,
                "native_edge_index",
                "mapping_source_edge_index",
                "mapping_source_edge_persistent_reference_sha256",
            ),
            (
                "vertex",
                vertex_alias_rows,
                "native_vertex_index",
                "mapping_source_vertex_index",
                "mapped_source_vertex_persistent_reference_sha256",
            ),
        ):
            for row in rows:
                identity = row["identity"]
                if identity is None:
                    continue
                for code in identity["warnings"]:
                    key = (
                        code,
                        entity_kind,
                        row["body_type"],
                        row["body_index"],
                        identity["owner_scope_key"],
                    )
                    group = grouped_warnings.setdefault(
                        key,
                        {
                            "code": code,
                            "component": component,
                            "entity_kind": entity_kind,
                            "body_type": row["body_type"],
                            "body_index": row["body_index"],
                            "owner_scope_key": identity[
                                "owner_scope_key"
                            ],
                            "persistent": False,
                            "rule_version": NATIVE_SCOPE_IDENTITY_RULE,
                            "object_indices": [],
                            "field": "identity.warnings",
                            "native_reference_sha256": row[
                                "native_reference_sha256"
                            ],
                            "source_document_sha256": identity[
                                "source_document_sha256"
                            ],
                            "referenced_configuration": identity.get(
                                "referenced_configuration"
                            ),
                            "occurrence_scope": identity.get(
                                "occurrence_scope"
                            ),
                            "body_name": identity.get("body_name"),
                            "mapping_source_part_document_sha256": identity[
                                "mapping_source_part_document_sha256"
                            ],
                            "mapping_source_body_reference_sha256": identity[
                                "mapping_source_body_reference_sha256"
                            ],
                            "mapped_source_indices": [],
                            "mapped_source_persistent_reference_sha256": [],
                            "topology_signature_sha256s": [],
                        },
                    )
                    group["object_indices"].append(identity[index_field])
                    group["mapped_source_indices"].append(
                        identity[identity_index_field]
                    )
                    group[
                        "mapped_source_persistent_reference_sha256"
                    ].append(identity[mapped_reference_field])
                    group["topology_signature_sha256s"].append(
                        identity["topology_signature_sha256"]
                    )
        for key in sorted(grouped_warnings):
            group = grouped_warnings[key]
            group["object_indices"] = sorted(
                set(group["object_indices"])
            )
            group["mapped_source_indices"] = sorted(
                set(group["mapped_source_indices"])
            )
            group[
                "mapped_source_persistent_reference_sha256"
            ] = sorted(
                set(
                    group[
                        "mapped_source_persistent_reference_sha256"
                    ]
                )
            )
            group["topology_signature_sha256s"] = sorted(
                set(group["topology_signature_sha256s"])
            )
            group["object"] = (
                f"{group['component']}:{group['entity_kind']}:"
                f"{group['body_type']}/{group['body_index']}"
            )
            group["detail"] = {
                name: group[name]
                for name in (
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
                )
            }
            native_identity_warnings.append(group)
    expected_aliases = {
        "face": expected_body_aliases("face", face_alias_rows),
        "edge": expected_body_aliases("edge", edge_alias_rows),
        "vertex": expected_body_aliases("vertex", vertex_alias_rows),
    }
    expected_audit = {
        kind: sorted(references)
        for kind, references in expected_aliases.items()
    }
    _require(
        value.get("reference_alias_audit", {
            "body": [],
            "face": [],
            "edge": [],
            "vertex": [],
        })
        == {
            **expected_audit,
            "body": [],
        },
        component + ": native scope-local alias audit differs from objects",
    )
    _require(
        all(
            (
                edge["start_vertex_reference_sha256"] is None
                or (
                    edge["body_type"],
                    edge["body_index"],
                    edge["start_vertex_reference_sha256"],
                )
                in scope_vertex_keys
            )
            and (
                edge["end_vertex_reference_sha256"] is None
                or (
                    edge["body_type"],
                    edge["body_index"],
                    edge["end_vertex_reference_sha256"],
                )
                in scope_vertex_keys
            )
            for edge in native_edges
        ),
        component + ": an edge endpoint is not a captured scope-local vertex",
    )

    for body_key, evidence in body_evidence.items():
        _require(
            len(evidence["facets"]) == body_counts[body_key]["facet_count"],
            component + f": native tessellation v6 facet count differs for body {body_key}",
        )
        _require(
            len(evidence["fins"]) == body_counts[body_key]["fin_count"],
            component + f": native tessellation v6 fin count differs for body {body_key}",
        )
        cached = {vertex_id for owner, vertex_id in vertices if owner == body_key}
        referenced = {
            vertex_id
            for fin in evidence["fins"].values()
            for vertex_id in fin["vertex_ids"]
        }
        _require(
            len(cached) == body_counts[body_key]["vertex_count"],
            component + f": native tessellation v6 vertex count differs for body {body_key}",
        )
        _require(
            cached == referenced,
            component + ": native tessellation v6 vertex cache differs from fin vertices",
        )
        edges_by_reference = {
            edge["edge_reference_sha256"]: edge
            for edge in native_edges
            if edge["body_type"] == body_key[0]
            and edge["body_index"] == body_key[1]
        }
        _require(
            {
                fin["edge_reference_sha256"]
                for fin in evidence["fins"].values()
                if fin["classification"] == "native_edge"
            }
            <= set(edges_by_reference),
            component
            + f": native tessellation v6 edge references differ for body {body_key}",
        )
        for fin in evidence["fins"].values():
            if fin["classification"] == "internal_only":
                continue
            edge = edges_by_reference.get(fin["edge_reference_sha256"])
            _require(
                edge is not None,
                component
                + f": native fin {fin['fin_id']} has no captured edge domain",
            )
            for vertex_id, u_value, evidence_value in (
                (
                    fin["vertex_ids"][0],
                    fin["start_u"],
                    fin["start_evidence"],
                ),
                (
                    fin["vertex_ids"][1],
                    fin["end_u"],
                    fin["end_evidence"],
                ),
            ):
                _require(
                    _valid_u_evidence(
                        evidence_value,
                        u_value,
                        vertices[(body_key, vertex_id)],
                        edge["u_min"],
                        edge["u_max"],
                        edge["curve_start_point_m"],
                        edge["curve_end_point_m"],
                    ),
                    component
                    + f": native fin {fin['fin_id']} U evidence is not independently reproducible",
                )
        cycles = _validate_facet_side_coverage(component, body_key, evidence)
        if triangle_vertices is not None:
            _validate_facet_stl_binding(
                component,
                body_key,
                evidence,
                cycles,
                vertices,
                triangle_vertices,
            )

    edges_by_body = {}
    for edge in native_edges:
        edges_by_body.setdefault((edge["body_type"], edge["body_index"]), {})[
            edge["edge_reference_sha256"]
        ] = edge
    for body_key, evidence in body_evidence.items():
        fins_by_edge_reference = {}
        for fin in evidence["fins"].values():
            reference = fin["edge_reference_sha256"]
            if reference is not None:
                fins_by_edge_reference.setdefault(reference, set()).add(fin["fin_id"])
        body_edges = edges_by_body.get(body_key, {})
        _require(
            set(fins_by_edge_reference) <= set(body_edges),
            component + f": native tessellation v6 edge references differ for body {body_key}",
        )
        for reference, edge in body_edges.items():
            declared = set(edge["fin_ids"])
            actual = fins_by_edge_reference.get(reference, set())
            _require(
                declared == actual,
                component
                + f": native tessellation v6 edge {reference} fin_ids are not bidirectional",
            )
            incidence = len(edge["face_indices"])
            _require(
                incidence in {1, 2},
                component
                + f": native tessellation v6 edge {reference} is non-manifold",
            )
            if incidence == 1:
                _require(
                    body_key[0] == 1,
                    component
                    + f": native tessellation v6 open edge {reference} "
                    + "is valid only on a sheet body",
                )
                _require(
                    bool(actual)
                    and all(
                        evidence["fins"][fin_id]["cofin_raw_id"] == -1
                        and evidence["fins"][fin_id]["cofin_id"] is None
                        and evidence["fins"][fin_id]["face_index"]
                        == edge["face_indices"][0]
                        for fin_id in actual
                    ),
                    component
                    + f": native tessellation v6 open edge {reference} "
                    + "does not carry the boundary sentinel",
                )
            else:
                _require(
                    len(actual) >= 2
                    and all(
                        evidence["fins"][fin_id]["cofin_raw_id"] >= 0
                        for fin_id in actual
                    ),
                    component
                    + f": native tessellation v6 shared edge {reference} "
                    + "contains a boundary fin",
                )
            u_min = float(edge["u_min"])
            u_max = float(edge["u_max"])
            intervals_by_face = {}
            collapsed_fin_span = {}
            for fin_id in actual:
                fin = evidence["fins"][fin_id]
                start_u = float(fin["start_u"])
                end_u = float(fin["end_u"])
                _require(
                    native_u_in_closed_interval(start_u, u_min, u_max)
                    and native_u_in_closed_interval(end_u, u_min, u_max),
                    component
                    + f": native tessellation v6 edge {reference} fin {fin_id} U is invalid",
                )
                fin_start = vertices[(body_key, fin["vertex_ids"][0])]
                fin_end = vertices[(body_key, fin["vertex_ids"][1])]
                intervals, span = _finite_fin_intervals(
                    fin_id=fin_id,
                    start_u=start_u,
                    end_u=end_u,
                    start_point=fin_start,
                    end_point=fin_end,
                    u_min=u_min,
                    u_max=u_max,
                    is_periodic=edge["curve_periodic"],
                    is_closed=edge["curve_closed"],
                    component=component,
                    reference=reference,
                )
                if span is not None:
                    collapsed_fin_span[fin_id] = span
                    continue
                intervals_by_face.setdefault(fin["face_index"], []).extend(
                    intervals
                )
            for face_index, intervals in intervals_by_face.items():
                _require(
                    native_u_intervals_cover(intervals, u_min, u_max),
                    component
                    + f": native tessellation v6 edge {reference} face {face_index} does not cover its U domain",
                )
            curve_points = {
                tuple(_float32(value) for value in edge["curve_start_point_m"]),
                tuple(_float32(value) for value in edge["curve_end_point_m"]),
            }
            endpoint_points = (
                edge["start_point_m"],
                edge["end_point_m"],
            )
            if all(point is not None for point in endpoint_points):
                _, topology_endpoints = native_curve_endpoint_roles(
                    edge["curve_start_point_m"],
                    edge["curve_end_point_m"],
                    edge["curve_sense"],
                )
                assignment = native_curve_topology_endpoint_assignment(
                    topology_endpoints[0],
                    topology_endpoints[1],
                    endpoint_points[0],
                    endpoint_points[1],
                )
                _require(
                    edge["topology_endpoint_assignment"] == assignment,
                    component
                    + f": native tessellation v6 edge {reference} "
                    + "topology endpoint assignment is not independently reproducible",
                )
                _require(
                    assignment["accepted"] is True
                    or (
                        assignment["result"] == "ambiguous"
                        and bool(collapsed_fin_span)
                    ),
                    component
                    + f": native tessellation v6 edge {reference} "
                    + (
                        "topology endpoint disagrees with curve direction"
                        if assignment["result"]
                        in {"swapped_only", "unmatched"}
                        else "topology endpoint direction is not uniquely proven"
                    ),
                )
            else:
                _require(
                    edge.get("topology_endpoint_assignment") is None,
                    component
                    + f": native tessellation v6 edge {reference} "
                    + "absent endpoints carry a topology assignment",
                )
                evaluations = edge["curve_endpoint_evaluations"]
                edge_points = {
                    tuple(
                        _float32(value)
                        for value in evaluations[name]["payload"][:3]
                    )
                    for name in ("start", "end")
                }
                _require(
                    curve_points == edge_points,
                    component
                    + f": native tessellation v6 edge {reference} curve endpoints differ",
                )
            evaluations = edge["curve_endpoint_evaluations"]
            for edge_endpoint in ("start", "end"):
                edge_evaluation = evaluations[edge_endpoint]
                edge_point = tuple(edge_evaluation["payload"][:3])
                for fin_id in actual:
                    fin = evidence["fins"][fin_id]
                    for fin_endpoint in ("start", "end"):
                        fin_evaluation = fin[f"{fin_endpoint}_evidence"]
                        if not native_u_equal(
                            edge_evaluation["u"],
                            fin_evaluation["u"],
                        ):
                            continue
                        fin_point = tuple(fin_evaluation["evaluate_payload"][:3])
                        point_tolerance = native_point_roundtrip_tolerance(
                            edge_point,
                            fin_point,
                        )
                        point_error = native_point_error(edge_point, fin_point)
                        _require(
                            point_error <= point_tolerance,
                            component
                            + f": native tessellation v6 edge {reference} "
                            + f"{edge_endpoint} endpoint conflicts with fin {fin_id} "
                            + f"{fin_endpoint} at the same U",
                        )
            face_sense = {
                row["face_index"]: row["EdgeInFaceSense"]["value"]
                for row in edge["face_senses"]
            }
            if incidence == 2:
                for fin_id in actual:
                    fin = evidence["fins"][fin_id]
                    cofin_id = fin["cofin_id"]
                    _require(
                        cofin_id in actual and cofin_id != fin_id,
                        component
                        + f": native tessellation v6 fin {fin_id} has no distinct cofin",
                    )
                    cofin = evidence["fins"][cofin_id]
                    fin_start = vertices[
                        (body_key, fin["vertex_ids"][0])
                    ]
                    fin_end = vertices[
                        (body_key, fin["vertex_ids"][1])
                    ]
                    cofin_start = vertices[
                        (body_key, cofin["vertex_ids"][0])
                    ]
                    cofin_end = vertices[
                        (body_key, cofin["vertex_ids"][1])
                    ]
                    reverse_start_tolerance = native_point_roundtrip_tolerance(
                        fin_start,
                        cofin_end,
                    )
                    reverse_end_tolerance = native_point_roundtrip_tolerance(
                        fin_end,
                        cofin_start,
                    )
                    fin_collapsed = fin_id in collapsed_fin_span
                    cofin_collapsed = cofin_id in collapsed_fin_span
                    collapsed_pair = fin_collapsed and cofin_collapsed
                    _require(
                        fin_collapsed is cofin_collapsed,
                        component
                        + f": native tessellation v6 fin {fin_id} has partial U collapse",
                    )
                    _require(
                        cofin["cofin_id"] == fin_id
                        and cofin["cofin_raw_id"] == fin_id
                        and fin["cofin_raw_id"] == cofin_id
                        and cofin["face_index"] != fin["face_index"]
                        and native_point_error(fin_start, cofin_end)
                        <= reverse_start_tolerance
                        and native_point_error(fin_end, cofin_start)
                        <= reverse_end_tolerance
                        and (
                            (
                                collapsed_pair
                                and native_u_equal(fin["start_u"], fin["end_u"])
                                and native_u_equal(
                                    cofin["start_u"],
                                    cofin["end_u"],
                                )
                            )
                            or (
                                not collapsed_pair
                                and (fin["end_u"] - fin["start_u"])
                                * (cofin["end_u"] - cofin["start_u"])
                                < 0.0
                            )
                        )
                        and native_u_equal(fin["start_u"], cofin["end_u"])
                        and native_u_equal(fin["end_u"], cofin["start_u"])
                        and face_sense[fin["face_index"]]
                        is not face_sense[cofin["face_index"]],
                        component
                        + f": native tessellation v6 fin {fin_id} cofin/orientation is inconsistent",
                    )
                    if (
                        collapsed_pair
                        and fin_id < cofin_id
                        and native_fin_resolutions is not None
                    ):
                        native_fin_resolutions.append(
                            {
                                "code": "native_fin_u_collapsed",
                                "component": component,
                                "body_type": body_key[0],
                                "body_index": body_key[1],
                                "edge_reference_sha256": reference,
                                "fin_ids": [fin_id, cofin_id],
                                "face_indices": [
                                    fin["face_index"],
                                    cofin["face_index"],
                                ],
                                "u": fin["start_u"],
                                "spans_m": {
                                    str(fin_id): collapsed_fin_span[fin_id],
                                    str(cofin_id): collapsed_fin_span[cofin_id],
                                },
                                "rule": NATIVE_COLLAPSED_U_FIN_RULE,
                                "qualification": (
                                    "zero_u_coverage; native_geometry_bounded_50um"
                                ),
                            }
                        )
            fin_faces = {
                evidence["fins"][fin_id]["face_index"] for fin_id in actual
            }
            active_faces = {
                face_index
                for face_index in edge["face_indices"]
                if face_spans_by_key[(*body_key, face_index)]["triangle_count"] > 0
            }
            _require(
                all(
                    face_index in intervals_by_face
                    and bool(intervals_by_face[face_index])
                    for face_index in active_faces
                ),
                component
                + f": native tessellation v6 edge {reference} has a collapsed-only face without positive U coverage",
            )
            _require(
                fin_faces == active_faces,
                component
                + f": native tessellation v6 edge {reference} does not cover its tessellated faces",
            )
            _require(
                bool(active_faces)
                or all(
                    face_spans_by_key[(*body_key, face_index)]["triangle_count"] == 0
                    for face_index in edge["face_indices"]
                ),
                component
                + f": native tessellation v6 edge {reference} has no fin without an untessellated face",
            )


def _parse_itessellation_body(
    component: str,
    body: dict,
    spans: list[dict],
    face_spans_by_key: dict,
) -> dict:
    body_key = (body["body_type"], body["body_index"])
    face_spans = {span["face_index"]: span for span in spans}
    faces = {}
    previous_face = None
    for index, face in enumerate(body["faces"]):
        _require(
            isinstance(face, dict)
            and set(face)
            == {
                "face_index",
                "facet_ids",
                "face_identity_sha256",
            },
            component + f": native tessellation v6 face {index} has an unknown shape",
        )
        face_index = face["face_index"]
        facet_ids = face["facet_ids"]
        _require(
            _integer(face_index)
            and face_index in face_spans
            and isinstance(facet_ids, list)
            and facet_ids == sorted(set(facet_ids))
            and all(_integer(facet_id) and facet_id >= 0 for facet_id in facet_ids)
            and face["face_identity_sha256"]
            == face_spans[face_index]["face_reference_sha256"]
            and (
                face_spans[face_index]["face_identity"] is None
                or (
                    face_spans[face_index]["face_identity"][
                        "topology_signature_sha256"
                    ]
                    == native_scope_identity_sha256(
                        {
                            "rule": NATIVE_FACE_IDENTITY_KEY_RULE,
                            "area_binary64": face_spans[face_index][
                                "face_identity"
                            ]["area_binary64_hex"],
                            "bbox_binary64": [
                                *face_spans[face_index]["face_identity"][
                                    "tessellation_bbox_min_binary64_hex"
                                ],
                                *face_spans[face_index]["face_identity"][
                                    "tessellation_bbox_max_binary64_hex"
                                ],
                            ],
                            "facet_ids": facet_ids,
                        }
                    )
                )
            )
            and (previous_face is None or face_index > previous_face),
            component + f": native tessellation v6 face {index} is malformed",
        )
        previous_face = face_index
        faces[face_index] = set(facet_ids)
    _require(
        set(faces) == set(face_spans),
        component + f": native tessellation v6 body {body_key} does not cover every face",
    )

    facets = {}
    previous_facet = None
    used_triangle_records = set()
    for index, facet in enumerate(body["facets"]):
        _require(
            isinstance(facet, dict)
            and set(facet)
            == {
                "facet_id",
                "face_index",
                "fin_ids",
                "vertex_ids",
                "triangle_record_index",
            },
            component + f": native tessellation v6 facet {index} has an unknown shape",
        )
        facet_id = facet["facet_id"]
        face_index = facet["face_index"]
        fin_ids = facet["fin_ids"]
        vertex_ids = facet["vertex_ids"]
        triangle_record_index = facet["triangle_record_index"]
        _require(
            _integer(facet_id)
            and facet_id >= 0
            and (previous_facet is None or facet_id > previous_facet)
            and face_index in faces
            and facet_id in faces[face_index]
            and isinstance(fin_ids, list)
            and len(fin_ids) == 3
            and fin_ids == sorted(set(fin_ids))
            and all(_integer(fin_id) and fin_id >= 0 for fin_id in fin_ids)
            and isinstance(vertex_ids, list)
            and len(vertex_ids) == 3
            and vertex_ids == sorted(set(vertex_ids))
            and all(_integer(vertex_id) and vertex_id >= 0 for vertex_id in vertex_ids)
            and _integer(triangle_record_index)
            and triangle_record_index >= 0,
            component + f": native tessellation v6 facet {index} is malformed",
        )
        previous_facet = facet_id
        span = face_spans_by_key[(*body_key, face_index)]
        expected_records = range(
            span["triangle_start"],
            span["triangle_start"] + span["triangle_count"],
        )
        _require(
            triangle_record_index in expected_records
            and triangle_record_index not in used_triangle_records,
            component
            + f": native tessellation v6 facet {facet_id} has duplicate or misplaced triangle index",
        )
        used_triangle_records.add(triangle_record_index)
        facets[facet_id] = {
            "facet_id": facet_id,
            "face_index": face_index,
            "fin_ids": tuple(fin_ids),
            "vertex_ids": tuple(vertex_ids),
            "triangle_record_index": triangle_record_index,
        }

    expected_triangle_records = {
        triangle_index
        for span in spans
        for triangle_index in range(
            span["triangle_start"],
            span["triangle_start"] + span["triangle_count"],
        )
    }
    _require(
        used_triangle_records == expected_triangle_records,
        component + f": native tessellation v6 body {body_key} does not cover every triangle record",
    )
    _require(
        {
            facet_id
            for facet_ids in faces.values()
            for facet_id in facet_ids
        }
        == set(facets),
        component + f": native tessellation v6 body {body_key} face-to-facet map is not closed",
    )
    for face_index, facet_ids in faces.items():
        _require(
            facet_ids
            == {
                facet_id
                for facet_id, facet in facets.items()
                if facet["face_index"] == face_index
            },
            component
            + f": native tessellation v6 body {body_key} face {face_index} has misowned facets",
        )

    fins = {}
    previous_fin = None
    facet_fin_membership = {}
    for index, fin in enumerate(body["fins"]):
        _require(
            isinstance(fin, dict)
            and set(fin)
            == {
                "fin_id",
                "facet_id",
                "face_index",
                "vertex_ids",
                "edge_reference_sha256",
                "classification",
                "start_u",
                "end_u",
                "start_evidence",
                "end_evidence",
                "cofin_raw_id",
                "cofin_id",
            },
            component + f": native tessellation v6 fin {index} has an unknown shape",
        )
        fin_id = fin["fin_id"]
        facet_id = fin["facet_id"]
        face_index = fin["face_index"]
        vertex_ids = fin["vertex_ids"]
        reference = fin["edge_reference_sha256"]
        classification = fin["classification"]
        start_u = fin["start_u"]
        end_u = fin["end_u"]
        start_evidence = fin["start_evidence"]
        end_evidence = fin["end_evidence"]
        cofin_raw_id = fin["cofin_raw_id"]
        cofin_id = fin["cofin_id"]
        _require(
            _integer(fin_id)
            and fin_id >= 0
            and (previous_fin is None or fin_id > previous_fin)
            and facet_id in facets
            and face_index == facets[facet_id]["face_index"]
            and isinstance(vertex_ids, list)
            and len(vertex_ids) == 2
            and len(set(vertex_ids)) == 2
            and all(_integer(vertex_id) and vertex_id >= 0 for vertex_id in vertex_ids)
            and (
                (
                    isinstance(reference, str)
                    and bool(HEX64.fullmatch(reference))
                    and classification == "native_edge"
                    and type(start_u) in (int, float)
                    and type(end_u) in (int, float)
                    and math.isfinite(start_u)
                    and math.isfinite(end_u)
                    and _integer(cofin_raw_id)
                    and cofin_raw_id >= -1
                    and (
                        (
                            cofin_raw_id == -1
                            and cofin_id is None
                        )
                        or (
                            cofin_raw_id >= 0
                            and cofin_id == cofin_raw_id
                        )
                    )
                )
                or (
                    reference is None
                    and classification == "internal_only"
                    and start_u is None
                    and end_u is None
                    and start_evidence is None
                    and end_evidence is None
                    and cofin_raw_id is None
                    and cofin_id is None
                )
            ),
            component + f": native tessellation v6 fin {index} is malformed or unclassified",
        )
        previous_fin = fin_id
        _require(
            fin_id in facets[facet_id]["fin_ids"],
            component + f": native tessellation v6 fin {fin_id} is absent from its facet",
        )
        previous = facet_fin_membership.setdefault(fin_id, facet_id)
        _require(
            previous == facet_id,
            component + f": native tessellation v6 fin {fin_id} belongs to multiple facets",
        )
        fins[fin_id] = {
            "fin_id": fin_id,
            "facet_id": facet_id,
            "face_index": face_index,
            "vertex_ids": tuple(vertex_ids),
            "edge_reference_sha256": reference,
            "classification": classification,
            "start_u": start_u,
            "end_u": end_u,
            "start_evidence": start_evidence,
            "end_evidence": end_evidence,
            "cofin_raw_id": cofin_raw_id,
            "cofin_id": cofin_id,
        }
    _require(
        set(facet_fin_membership)
        == {
            fin_id
            for facet in facets.values()
            for fin_id in facet["fin_ids"]
        },
        component + f": native tessellation v6 body {body_key} facet-to-fin map is not closed",
    )
    return {"faces": faces, "facets": facets, "fins": fins}


def _validate_facet_side_coverage(
    component: str,
    body_key: tuple[int, int],
    evidence: dict,
) -> dict[int, tuple[int, int, int]]:
    """Require each facet's directed fins to form one unique three-cycle."""

    cycles = {}
    for facet in evidence["facets"].values():
        facet_vertices = set(facet["vertex_ids"])
        fin_ids = facet["fin_ids"]
        fins = [evidence["fins"][fin_id] for fin_id in fin_ids]
        starts = [fin["vertex_ids"][0] for fin in fins]
        ends = [fin["vertex_ids"][1] for fin in fins]
        _require(
            set(starts) == facet_vertices
            and set(ends) == facet_vertices
            and len(set(starts)) == 3
            and len(set(ends)) == 3,
            component
            + f": native tessellation v6 facet {facet['facet_id']} is not a directed three-cycle",
        )
        cycle = [fins[0]["vertex_ids"][0]]
        current = fins[0]
        used = set()
        for _ in range(3):
            _require(
                current["fin_id"] not in used,
                component
                + f": native tessellation v6 facet {facet['facet_id']} repeats a directed fin",
            )
            used.add(current["fin_id"])
            cycle.append(current["vertex_ids"][1])
            matches = [
                fin
                for fin in fins
                if fin["vertex_ids"][0] == current["vertex_ids"][1]
            ]
            _require(
                len(matches) == 1,
                component
                + f": native tessellation v6 facet {facet['facet_id']} has an ambiguous directed cycle",
            )
            current = matches[0]
        _require(
            current["fin_id"] == fins[0]["fin_id"]
            and cycle[0] == cycle[-1]
            and len(set(cycle[:-1])) == 3,
            component
            + f": native tessellation v6 facet {facet['facet_id']} does not form one directed three-cycle",
        )
        cycles[facet["facet_id"]] = (cycle[0], cycle[1], cycle[2])
    return cycles


def _float32(value: float) -> float:
    """Apply the manifest's public binary32 rule with round-to-nearest-even."""

    return struct.unpack("<f", struct.pack("<f", value))[0]


def _validate_facet_stl_binding(
    component: str,
    body_key: tuple[int, int],
    evidence: dict,
    cycles: dict[int, tuple[int, int, int]],
    vertices: dict,
    triangle_vertices: list,
) -> None:
    """Bind the directed native facet cycle to its exact binary STL record."""

    for facet in evidence["facets"].values():
        native = [
            tuple(_float32(coordinate) for coordinate in vertices[(body_key, vertex_id)])
            for vertex_id in cycles[facet["facet_id"]]
        ]
        record = triangle_vertices[facet["triangle_record_index"]]
        stl = [tuple(float(coordinate) for coordinate in point) for point in record]
        _require(
            any(native == stl[offset:] + stl[:offset] for offset in range(3)),
            component
            + f": native tessellation v6 facet {facet['facet_id']} does not bind "
            + f"triangle record {facet['triangle_record_index']}",
        )


def summarize_native_topology(tessellation: dict, spans: list[dict]) -> dict:
    """Summarize native edge incidence without inventing geometric adjacency."""

    body_rows = {}
    for span in spans:
        key = (span["body_type"], span["body_index"])
        row = body_rows.setdefault(
            key,
            {
                "body_type": key[0],
                "body_index": key[1],
                "body_name": span["body_name"],
                "body_reference_sha256": span["body_reference_sha256"],
                "faces": 0,
                "triangles": 0,
                "native_edges": 0,
                "shared_edges": 0,
                "open_edges": 0,
                "nonmanifold_edges": 0,
                "boundary_fins": 0,
                "paired_fins": 0,
            },
        )
        row["faces"] += 1
        row["triangles"] += span["triangle_count"]
    fins_by_body = defaultdict(dict)
    for body in tessellation["itessellation"]["bodies"]:
        key = (body["body_type"], body["body_index"])
        fins_by_body[key] = {fin["fin_id"]: fin for fin in body["fins"]}
    vertex_owners = {}
    for edge in tessellation["native_edges"]:
        key = (edge["body_type"], edge["body_index"])
        row = body_rows[key]
        row["native_edges"] += 1
        incidence = len(edge["face_indices"])
        if incidence == 1:
            _require(
                key[0] == 1,
                f"native edge body {key} uses sheet-only boundary semantics",
            )
            _require(
                bool(edge["fin_ids"])
                and all(
                    fins_by_body[key][fin_id]["cofin_raw_id"] == -1
                    and fins_by_body[key][fin_id]["cofin_id"] is None
                    for fin_id in edge["fin_ids"]
                ),
                f"native edge body {key} has an invalid open boundary fin",
            )
            row["open_edges"] += 1
            row["boundary_fins"] += len(edge["fin_ids"])
        elif incidence == 2:
            _require(
                len(edge["fin_ids"]) >= 2
                and all(
                    fins_by_body[key][fin_id]["cofin_raw_id"] >= 0
                    and fins_by_body[key][fin_id]["cofin_id"] is not None
                    for fin_id in edge["fin_ids"]
                ),
                f"native edge body {key} has an invalid paired fin",
            )
            row["shared_edges"] += 1
            row["paired_fins"] += len(edge["fin_ids"])
        else:
            raise PipelineError(
                f"native edge body {key} has non-manifold face incidence"
            )
        for reference in (
            edge["start_vertex_reference_sha256"],
            edge["end_vertex_reference_sha256"],
        ):
            vertex_owners.setdefault(reference, set()).add(key)
    rows = [body_rows[key] for key in sorted(body_rows)]
    complete = (
        bool(rows)
        and all(row["native_edges"] > 0 and row["faces"] > 0 for row in rows)
    )
    return {
        "status": "evidence_complete" if complete else "unresolved",
        "qualification": (
            "native_edge_incidence_only; normalized_boundary_evidence_required"
        ),
        "cross_body_vertex_reference_count": sum(
            len(owners) > 1 for owners in vertex_owners.values()
        ),
        "bodies": rows,
    }
