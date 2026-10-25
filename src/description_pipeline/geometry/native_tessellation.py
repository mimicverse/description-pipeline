"""Strict native face-span grammar shared by verification and report projection."""

from __future__ import annotations

import math
import re
import struct
from collections import defaultdict
from fractions import Fraction

from ..io import PipelineError
from ..schemas import (
    NATIVE_ID_ARRAY_SOURCE_FLAT,
    NATIVE_ID_ARRAY_SOURCE_BODY_GLOBAL_NULL_ZERO,
    NATIVE_ID_ARRAY_SOURCE_INCIDENT_FACES_NULL_ZERO,
    NATIVE_ID_ARRAY_SOURCE_PLANAR_BREP,
    NATIVE_TESSELLATION_SCHEMA,
)
from .native_fin_topology import (
    NativeFinRelationError,
    closed_edge_fin_partition,
    validate_native_edge_face_senses,
    validate_native_edge_fin_relations,
)
from .native_planar_brep import (
    NATIVE_PLANAR_COEDGE_DIRECTION_RULE,
    NATIVE_PLANAR_COEDGE_VERTEX_RULE,
    NATIVE_PLANAR_EDGE_VERTEX_RULE,
    NATIVE_PLANAR_BREP_PROVIDER,
    NATIVE_PLANAR_CONVEX_FAN_RULE,
    NATIVE_PLANAR_BREP_SCHEMA,
)
from .native_u import (
    NATIVE_CURVE_ENDPOINT_OPERATION_RULE,
    NATIVE_COLLAPSED_U_FIN_RULE,
    NATIVE_COMPONENT_REFERENCE_ALIASED,
    NATIVE_EDGE_FIN_IDS_DERIVED,
    NATIVE_FACE_FACET_IDS_DERIVED,
    NATIVE_FACE_IDENTITY_KEY_RULE,
    NATIVE_SCOPE_MAPPING_METHOD,
    NATIVE_SCOPE_SOURCE_AUTHORITY,
    NATIVE_SCOPE_VERTEX_MAPPING_METHOD,
    NATIVE_PERSISTENT_IDENTITY_RULE,
    NATIVE_POINT_ROUNDTRIP_RULE,
    NATIVE_PLANAR_BREP_TRIANGULATION_DERIVED,
    NATIVE_PLANAR_BREP_UNIT_NORMAL_DERIVED,
    NATIVE_PLANAR_COEDGE_TANGENT_DERIVED,
    NATIVE_IDENTITY_POLICY_RULE,
    NATIVE_PART_WITNESS_DERIVED,
    NATIVE_SCOPE_IDENTITY_RULE,
    NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED,
    NATIVE_TOPOLOGY_IDENTITY_DERIVED,
    NATIVE_TESSELLATION_EDGE_PROJECTION_METRIC,
    NATIVE_TESSELLATION_EDGE_PROJECTION_RULE,
    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
    NATIVE_U_CLOSEST_POINT_PARAMETER_RULE,
    NATIVE_U_IDENTITY_RULE,
    NATIVE_U_RESOLUTION_RULE,
    native_curve_endpoint_operation_tolerance,
    native_binary64_sqrt,
    native_binary64_sum_upper,
    native_binary64_unit_vector,
    native_curve_parameter_endpoints,
    native_curve_topology_endpoint_set_assignment,
    native_evaluate2_evidence,
    native_open_fin_interval,
    native_point_error,
    native_point_roundtrip_tolerance,
    native_point_within_max_coordinate_bound,
    native_u_equal,
    native_u_domain_is_resolvable,
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
) -> bool:
    """Validate one retained closest-point/U projection proof."""

    if not isinstance(value, dict) or set(value) != {
        "u",
        "u_parameter_rule",
        "rule_version",
        "projection_rule",
        "projection_metric",
        "projection_tolerance_m",
        "source_point_m",
        "closest_point_on_raw",
        "evaluate2_raw",
        "evaluate_payload",
        "evaluate_packed_status",
        "projection_errors_m",
    }:
        return False
    closest = value["closest_point_on_raw"]
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
        or len(closest) != 4
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
    closest_parameter = closest[3]
    evaluated_point = tuple(evaluated[:3])
    expected_point = tuple(float(item) for item in endpoint_point)
    source_point = tuple(value["source_point_m"])
    projection_errors = value["projection_errors_m"]
    if not isinstance(projection_errors, dict) or set(projection_errors) != {
        "source_to_closest",
        "closest_to_evaluate",
        "source_to_evaluate",
    }:
        return False
    if not all(
        type(error) in numeric
        and math.isfinite(float(error))
        and float(error) >= 0.0
        for error in projection_errors.values()
    ):
        return False
    computed_projection_errors = {
        "source_to_closest": native_point_error(
            source_point,
            closest_point,
        ),
        "closest_to_evaluate": native_point_error(
            closest_point,
            evaluated_point,
        ),
        "source_to_evaluate": native_point_error(
            source_point,
            evaluated_point,
        ),
    }
    parsed_evaluate2 = native_evaluate2_evidence(value["evaluate2_raw"])
    return not (
        source_point != expected_point
        or value["u_parameter_rule"]
        != NATIVE_U_CLOSEST_POINT_PARAMETER_RULE
        or value["rule_version"] != NATIVE_U_IDENTITY_RULE
        or value["projection_rule"]
        != NATIVE_TESSELLATION_EDGE_PROJECTION_RULE
        or value["projection_metric"]
        != NATIVE_TESSELLATION_EDGE_PROJECTION_METRIC
        or value["projection_tolerance_m"]
        != NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M
        or not native_u_equal(value["u"], closest_parameter)
        or not native_u_equal(u_parameter, closest_parameter)
        or projection_errors != computed_projection_errors
        or not all(
            native_point_within_max_coordinate_bound(
                left,
                right,
                NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
            )
            for left, right in (
                (source_point, closest_point),
                (closest_point, evaluated_point),
                (source_point, evaluated_point),
            )
        )
        or parsed_evaluate2 is None
        or parsed_evaluate2["payload"] != evaluated
        or parsed_evaluate2["packed_status"]
        != value["evaluate_packed_status"]
        or not _valid_evaluate2_packed_status(
            value["evaluate_packed_status"]
        )
        or not native_u_in_closed_interval(
            closest_parameter,
            domain_lower,
            domain_upper,
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


def _finite_fin_intervals(
    *,
    fin_id: int,
    start_u: float,
    end_u: float,
    start_point: object,
    end_point: object,
    u_min: float,
    u_max: float,
    component: str,
    reference: str,
) -> tuple[list[tuple[float, float]], float | None]:
    """Return coverage intervals, or the bounded span of a collapsed-U fin."""

    if not native_u_equal(start_u, end_u):
        return (
            native_open_fin_interval(
                start_u,
                end_u,
                u_min,
                u_max,
            ),
            None,
        )
    span = native_point_error(start_point, end_point)
    _require(
        0.0 < span <= NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
        component
        + f": native tessellation edge {reference} fin {fin_id} collapsed U span is invalid",
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
        and set(tessellation)
        == {
            "schema_version",
            "faces",
            "face_spans",
            "native_edges",
            "no_facets_faces",
            "itessellation",
            "planar_brep",
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
        "fin_ids_source",
        "raw_fin_ids",
        "warnings",
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
        "closed_fin_cycles",
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
        _require(isinstance(edge["closed_fin_cycles"], list),
                 component + f": native edge {index} closed partition evidence is malformed")
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
                (mapped_source_index is None and mapped_source_reference is None)
                or (
                    _integer(mapped_source_index)
                    and mapped_source_index >= 0
                    and isinstance(mapped_source_reference, str)
                    and bool(HEX64.fullmatch(mapped_source_reference))
                )
            )
            and (
                start_reference is None or (isinstance(start_reference, str) and bool(HEX64.fullmatch(start_reference)))
            )
            and (end_reference is None or (isinstance(end_reference, str) and bool(HEX64.fullmatch(end_reference))))
            and (
                start_point is None
                or (
                    isinstance(start_point, list)
                    and len(start_point) == 3
                    and all(type(value) in (int, float) and math.isfinite(value) for value in start_point)
                )
            )
            and (
                end_point is None
                or (
                    isinstance(end_point, list)
                    and len(end_point) == 3
                    and all(type(value) in (int, float) and math.isfinite(value) for value in end_point)
                )
            )
            and all(error is None or isinstance(error, str) for error in endpoint_errors)
            and edge["start_vertex_state"] in {"present", "absent", "read_failed"}
            and edge["end_vertex_state"] in {"present", "absent", "read_failed"}
            and type(edge["endpoint_points_derived_from_curve"]) is bool
            and isinstance(edge["face_indices"], list)
            and bool(edge["face_indices"])
            and edge["face_indices"] == sorted(set(edge["face_indices"]))
            and all(_integer(value) and value >= 0 for value in edge["face_indices"])
            and isinstance(edge["fin_ids"], list)
            and edge["fin_ids"] == sorted(set(edge["fin_ids"]))
            and all(_integer(value) and value >= 0 for value in edge["fin_ids"])
            and edge["fin_ids_source"]
            in {
                NATIVE_ID_ARRAY_SOURCE_FLAT,
                NATIVE_ID_ARRAY_SOURCE_INCIDENT_FACES_NULL_ZERO,
                NATIVE_ID_ARRAY_SOURCE_PLANAR_BREP,
            }
            and (
                (
                    edge["fin_ids_source"] == NATIVE_ID_ARRAY_SOURCE_FLAT
                    and isinstance(edge["raw_fin_ids"], list)
                    and all(_integer(value) and value >= 0 for value in edge["raw_fin_ids"])
                    and sorted(edge["raw_fin_ids"]) == edge["fin_ids"]
                )
                or (
                    edge["fin_ids_source"] == NATIVE_ID_ARRAY_SOURCE_INCIDENT_FACES_NULL_ZERO
                    and edge["body_type"] == 1
                    and edge["raw_fin_ids"] is None
                    and edge["fin_ids"] == []
                    and all(
                        face_spans_by_key[
                            (
                                edge["body_type"],
                                edge["body_index"],
                                face_index,
                            )
                        ]["triangle_count"]
                        == 0
                        for face_index in edge["face_indices"]
                    )
                )
                or (
                    edge["fin_ids_source"] == NATIVE_ID_ARRAY_SOURCE_PLANAR_BREP
                    and edge["body_type"] == 1
                    and edge["raw_fin_ids"] is None
                    and edge["fin_ids"] == []
                )
            )
            and edge["warnings"]
            == (
                [NATIVE_EDGE_FIN_IDS_DERIVED]
                if edge["fin_ids_source"] == NATIVE_ID_ARRAY_SOURCE_INCIDENT_FACES_NULL_ZERO
                else (
                    ["cad_edge_fin_ids_not_applicable_for_planar_brep"]
                    if edge["fin_ids_source"] == NATIVE_ID_ARRAY_SOURCE_PLANAR_BREP
                    else []
                )
            )
            and type(edge["u_min"]) in (int, float)
            and type(edge["u_max"]) in (int, float)
            and math.isfinite(edge["u_min"])
            and math.isfinite(edge["u_max"])
            and edge["u_min"] < edge["u_max"]
            and isinstance(edge["curve_start_point_m"], list)
            and len(edge["curve_start_point_m"]) == 3
            and all(type(value) in (int, float) and math.isfinite(value) for value in edge["curve_start_point_m"])
            and isinstance(edge["curve_end_point_m"], list)
            and len(edge["curve_end_point_m"]) == 3
            and all(type(value) in (int, float) and math.isfinite(value) for value in edge["curve_end_point_m"])
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
            topology_endpoints = native_curve_parameter_endpoints(
                edge["curve_start_point_m"],
                edge["curve_end_point_m"],
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
        parameter_endpoints = native_curve_parameter_endpoints(
            edge["curve_start_point_m"],
            edge["curve_end_point_m"],
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
        if start_reference is not None and start_reference == end_reference:
            evaluated_start = evaluations["start"]["payload"][:3]
            evaluated_end = evaluations["end"]["payload"][:3]
            _require(native_u_domain_is_resolvable(edge["u_min"], edge["u_max"])
                     and native_point_within_max_coordinate_bound(evaluated_start, evaluated_end,
                         native_point_roundtrip_tolerance(evaluated_start, evaluated_end)),
                     component + ": shared native vertex lacks a closed nonzero edge domain")
        edge_owners[reference] = (body_key, set(edge["face_indices"]))
        try:
            validate_native_edge_face_senses(
                component=component,
                face_indices=edge["face_indices"],
                face_senses=edge["face_senses"],
            )
        except NativeFinRelationError as error:
            raise PipelineError(error.message) from error
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
    planar_bodies = _parse_planar_brep_evidence(
        component,
        tessellation["planar_brep"],
        spans_by_body,
        face_spans_by_key,
        native_edges,
        body_references,
        expected_bodies,
        triangle_vertices,
    )
    _parse_itessellation_evidence(
        component,
        tessellation["itessellation"],
        spans_by_body,
        face_spans_by_key,
        native_edges,
        body_references,
        triangles,
        triangle_vertices,
        native_fin_resolutions,
        native_identity_warnings,
        expected_bodies - planar_bodies,
    )
    return spans


def _finite_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _finite_point(value) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_finite_number(coordinate) for coordinate in value)


def _fraction_point(value):
    return tuple(Fraction.from_float(float(coordinate)) for coordinate in value)


def _fraction_vector(value):
    return tuple(Fraction.from_float(float(item)) for item in value)


def _vector_cross(first, second):
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _vector_difference(first, second):
    return tuple(second[index] - first[index] for index in range(3))


def _cross2(first, second, third) -> Fraction:
    return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (third[0] - first[0])


def _point_on_segment(point, start, end) -> bool:
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


def _segment_line_intersection(start, end, line_start, line_end):
    direction = (
        end[0] - start[0],
        end[1] - start[1],
    )
    line_direction = (
        line_end[0] - line_start[0],
        line_end[1] - line_start[1],
    )
    denominator = direction[0] * line_direction[1] - direction[1] * line_direction[0]
    if denominator == 0:
        return end
    offset = (
        line_start[0] - start[0],
        line_start[1] - start[1],
    )
    parameter = (offset[0] * line_direction[1] - offset[1] * line_direction[0]) / denominator
    return (
        start[0] + parameter * direction[0],
        start[1] + parameter * direction[1],
    )


def _clip_convex_polygon(subject, clip):
    output = list(subject)
    for index, clip_start in enumerate(clip):
        clip_end = clip[(index + 1) % len(clip)]
        source = output
        output = []
        if not source:
            break
        previous = source[-1]
        previous_inside = _cross2(clip_start, clip_end, previous) >= 0
        for current in source:
            current_inside = _cross2(clip_start, clip_end, current) >= 0
            if current_inside is not previous_inside:
                output.append(
                    _segment_line_intersection(
                        previous,
                        current,
                        clip_start,
                        clip_end,
                    )
                )
            if current_inside:
                output.append(current)
            previous = current
            previous_inside = current_inside
    return output


def _polygon_area2(points) -> Fraction:
    return sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[index][1] * points[(index + 1) % len(points)][0]
        for index in range(len(points))
    )


def _triangles_overlap(first, second) -> bool:
    first = first if _polygon_area2(first) > 0 else tuple(reversed(first))
    second = second if _polygon_area2(second) > 0 else tuple(reversed(second))
    intersection = _clip_convex_polygon(first, second)
    return len(intersection) >= 3 and _polygon_area2(intersection) > 0


def _outward_sqrt(value: Fraction) -> float:
    return native_binary64_sqrt(value, upward=True)


def _binary32_point(point):
    try:
        packed = struct.pack(
            "<3f",
            *(float(coordinate) for coordinate in point),
        )
    except (OverflowError, struct.error) as error:
        raise PipelineError("planar provider point is outside binary32") from error
    return struct.unpack("<3f", packed)


def _projected_boundary(points, face_normal):
    axis = max(range(3), key=lambda index: abs(face_normal[index]))
    first = (axis + 1) % 3
    second = (axis + 2) % 3
    return (
        face_normal[axis],
        tuple(
            (
                Fraction(point[first]),
                Fraction(point[second]),
            )
            for point in points
        ),
    )


def _plane_residual_squared(point, plane_point, plane_normal):
    normal = tuple(Fraction.from_float(value) for value in plane_normal)
    numerator = sum(
        normal[index] * (Fraction.from_float(point[index]) - Fraction.from_float(plane_point[index]))
        for index in range(3)
    )
    return numerator * numerator / sum(value * value for value in normal)


def _planar_distance_squared(first, second):
    return sum((Fraction(first[axis]) - Fraction(second[axis])) ** 2 for axis in range(3))


def _planar_line_projection(point, line):
    origin = tuple(Fraction(value) for value in line[:3])
    direction = tuple(Fraction(value) for value in line[3:])
    scalar = sum((Fraction(point[axis]) - origin[axis]) * direction[axis] for axis in range(3))
    scalar /= sum(value**2 for value in direction)
    return tuple(origin[axis] + scalar * direction[axis] for axis in range(3))


def _planar_directed_segment_bound(component, points, targets):
    observed = _vector_difference(*(tuple(Fraction(value) for value in point) for point in points))
    expected = _vector_difference(*(tuple(Fraction(value) for value in point) for point in targets))
    _require(sum(observed[axis] * expected[axis] for axis in range(3)) > 0,
             component + ": curve observation reverses its native endpoint correspondence")
    return max(_planar_distance_squared(point, target) for point, target in zip(points, targets, strict=True))


def _planar_project_to_plane(point, plane_point, normal):
    axis = max(range(3), key=lambda index: abs(normal[index]))
    exact = [Fraction(value) for value in point]
    # Solve the raw native plane equation without a binary64 intermediate.
    residual = sum(Fraction(normal[index]) * (exact[index] - Fraction(plane_point[index])) for index in range(3))
    exact[axis] -= residual / Fraction(normal[axis])
    return tuple(exact)


def _validate_planar_boundary(component, points, normal, label):
    normal_axis, projected = _projected_boundary(points, normal)
    sign = 1 if normal_axis > 0 else -1
    area = _polygon_area2(projected)
    _require(len(set(projected)) == len(projected), component + f": {label} repeats a boundary vertex")
    _require(area * sign > 0, component + f": {label} boundary does not follow face orientation")
    _require(
        all(
            _cross2(projected[i], projected[(i + 1) % len(projected)], projected[(i + 2) % len(projected)]) * sign > 0
            for i in range(len(projected))
        ),
        component + f": {label} boundary is not strictly convex",
    )
    for first in range(len(projected)):
        for second in range(first + 1, len(projected)):
            if second == (first + 1) % len(projected) or first == (second + 1) % len(projected):
                continue
            _require(
                not _segments_intersect(
                    (projected[first], projected[(first + 1) % len(projected)]),
                    (projected[second], projected[(second + 1) % len(projected)]),
                ),
                component + f": {label} boundary self-intersects",
            )
    return sign, projected, area


def _parse_planar_brep_evidence(
    component: str,
    value: object,
    spans_by_body: dict,
    face_spans_by_key: dict,
    native_edges: list[dict],
    body_references: dict,
    expected_bodies: set,
    triangle_vertices: list[tuple[tuple[float, float, float], ...]] | None,
) -> set:
    """Validate planar provider facts and return the bodies it covers."""

    _require(
        isinstance(value, dict)
        and set(value) == {"schema_version", "bodies"}
        and value["schema_version"] == NATIVE_PLANAR_BREP_SCHEMA
        and isinstance(value["bodies"], list),
        component + ": native planar BRep evidence is missing or unsupported",
    )
    raw_bodies = value["bodies"]
    body_required = {
        "provider",
        "schema_version",
        "body_type",
        "body_index",
        "body_reference_sha256",
        "native_body_reference_sha256",
        "face_index",
        "face_reference_sha256",
        "native_face_reference_sha256",
        "face_area_m2",
        "warnings",
        "surface",
        "loop",
        "boundary_vertices",
        "triangulation",
    }
    coedge_required = {
        "coedge_index",
        "edge_index",
        "edge_reference_sha256",
        "native_edge_reference_sha256",
        "edge_is_line",
        "edge_line_params",
        "coedge_line_params",
        "line_orientation",
        "same_direction_as_edge",
        "vertex_identity_rule",
        "curve_vertex_identity_rule",
        "curve_vertex_references_sha256",
        "tangent_direction_rule",
        "start_parameter",
        "end_parameter",
        "parameter_order",
        "start_point_m",
        "end_point_m",
        "start_vertex_reference_sha256",
        "end_vertex_reference_sha256",
        "start_vertex_point_m",
        "end_vertex_point_m",
        "start_vertex_point_error_m",
        "end_vertex_point_error_m",
        "start_vertex_point_tolerance_m",
        "end_vertex_point_tolerance_m",
        "curve_params_raw",
        "coedge_curve_present",
    }
    boundary_required = {
        "vertex_index",
        "vertex_reference_sha256",
        "point_m",
    }
    triangle_required = {
        "triangle_index",
        "vertex_indices",
        "native_points",
        "constructed_points",
        "stl_points",
    }
    covered_bodies = set()
    previous_body = None
    for body_index, body in enumerate(raw_bodies):
        _require(
            isinstance(body, dict) and set(body) == body_required,
            component + f": native planar BRep body {body_index} has an unknown shape",
        )
        body_key = (body["body_type"], body["body_index"])
        _require(
            body["provider"] == NATIVE_PLANAR_BREP_PROVIDER
            and body["schema_version"] == NATIVE_PLANAR_BREP_SCHEMA
            and _integer(body["body_type"])
            and body["body_type"] == 1
            and _integer(body["body_index"])
            and body["body_index"] >= 0
            and body_key in expected_bodies
            and (previous_body is None or body_key > previous_body),
            component + f": native planar BRep body {body_index} has an unsupported identity",
        )
        previous_body = body_key
        _require(
            body_key not in covered_bodies,
            component + f": native planar BRep body {body_key} is duplicated",
        )
        covered_bodies.add(body_key)
        body_reference = body["body_reference_sha256"]
        native_body_reference = body["native_body_reference_sha256"]
        _require(
            isinstance(body_reference, str)
            and bool(HEX64.fullmatch(body_reference))
            and body_reference == body_references[body_key]
            and isinstance(native_body_reference, str)
            and bool(HEX64.fullmatch(native_body_reference))
            and _finite_number(body["face_area_m2"])
            and body["face_area_m2"] > 0.0
            and body["warnings"]
            == [
                NATIVE_PLANAR_BREP_UNIT_NORMAL_DERIVED,
                NATIVE_PLANAR_BREP_TRIANGULATION_DERIVED,
                NATIVE_PLANAR_COEDGE_TANGENT_DERIVED,
            ],
            component + f": native planar BRep body {body_key} is malformed",
        )
        body_spans = spans_by_body[body_key]
        _require(
            len(body_spans) == 1
            and body_spans[0]["face_index"] == body["face_index"]
            and body_spans[0]["face_reference_sha256"] == body["face_reference_sha256"]
            and body_spans[0]["native_face_reference_sha256"] == body["native_face_reference_sha256"],
            component + f": native planar BRep body {body_key} does not bind one face",
        )
        span = body_spans[0]
        face_key = (*body_key, body["face_index"])
        _require(
            face_key in face_spans_by_key,
            component + f": native planar BRep body {body_key} names an unknown face",
        )

        surface = body["surface"]
        _require(
            isinstance(surface, dict)
            and set(surface)
            == {
                "is_plane",
                "plane_params",
                "face_in_surface_sense",
                "face_normal_raw",
                "face_normal_unit",
            }
            and surface["is_plane"] is True
            and isinstance(surface["plane_params"], list)
            and len(surface["plane_params"]) == 6
            and all(_finite_number(parameter) for parameter in surface["plane_params"])
            and math.hypot(*surface["plane_params"][:3]) > 0.0
            and type(surface["face_in_surface_sense"]) is bool
            and _finite_point(surface["face_normal_raw"])
            and _finite_point(surface["face_normal_unit"]),
            component + f": native planar BRep surface is malformed for body {body_key}",
        )
        raw_normal = surface["plane_params"][:3]
        expected_face_normal_raw = [(-value if surface["face_in_surface_sense"] else value) for value in raw_normal]
        expected_face_normal_unit = list(native_binary64_unit_vector(expected_face_normal_raw))
        _require(
            surface["face_normal_raw"] == expected_face_normal_raw
            and surface["face_normal_unit"] == expected_face_normal_unit,
            component + f": native planar BRep face normal is inconsistent for body {body_key}",
        )

        loop = body["loop"]
        _require(
            isinstance(loop, dict)
            and set(loop) == {"outer", "reference_sha256", "coedges"}
            and loop["outer"] is True
            and isinstance(loop["reference_sha256"], str)
            and bool(HEX64.fullmatch(loop["reference_sha256"]))
            and isinstance(loop["coedges"], list)
            and len(loop["coedges"]) >= 3,
            component + f": native planar BRep outer loop is malformed for body {body_key}",
        )
        native_edges_for_body = [edge for edge in native_edges if (edge["body_type"], edge["body_index"]) == body_key]
        edge_by_reference = {edge["edge_reference_sha256"]: edge for edge in native_edges_for_body}
        _require(
            set(span["edge_references"]) == set(edge_by_reference) and bool(edge_by_reference),
            component + f": native planar BRep face does not close its body edges for {body_key}",
        )
        coedges = loop["coedges"]
        _require(
            len(coedges) == len(edge_by_reference),
            component + f": native planar BRep loop and edge counts differ for body {body_key}",
        )
        boundary_points = []
        boundary_references = []
        source_error_squared = Fraction(0, 1)
        tangent_error_squared = Fraction(0, 1)
        endpoint_sets = []
        vertex_points = {}
        for coedge in coedges:
            _require(
                isinstance(coedge, dict) and isinstance(coedge.get("edge_reference_sha256"), str),
                component + ": native planar coedge reference is malformed",
            )
            edge = edge_by_reference.get(coedge["edge_reference_sha256"])
            _require(isinstance(edge, dict), component + ": native planar coedge names an unknown edge")
            refs = set()
            for endpoint in ("start", "end"):
                ref = edge[f"{endpoint}_vertex_reference_sha256"]
                point = edge[f"{endpoint}_point_m"]
                _require(
                    isinstance(ref, str) and bool(HEX64.fullmatch(ref)) and _finite_point(point),
                    component + ": native planar edge lacks a complete vertex identity",
                )
                _require(
                    ref not in vertex_points or vertex_points[ref] == point,
                    component + ": one native planar vertex has conflicting coordinates",
                )
                vertex_points[ref] = point
                refs.add(ref)
            _require(len(refs) == 2, component + ": native planar edge must have two distinct vertices")
            endpoint_sets.append(refs)
        seen_edges = set()
        for coedge_index, coedge in enumerate(coedges):
            _require(
                isinstance(coedge, dict) and set(coedge) == coedge_required,
                component + f": native planar BRep coedge {coedge_index} has an unknown shape",
            )
            edge_reference = coedge["edge_reference_sha256"]
            edge = edge_by_reference.get(edge_reference)
            _require(
                coedge["coedge_index"] == coedge_index
                and _integer(coedge["coedge_index"])
                and coedge["edge_is_line"] is True
                and type(coedge["same_direction_as_edge"]) is bool
                and coedge["tangent_direction_rule"] == NATIVE_PLANAR_COEDGE_DIRECTION_RULE
                and type(coedge["coedge_curve_present"]) is bool
                and _integer(coedge["edge_index"])
                and 0 <= coedge["edge_index"] < len(native_edges_for_body)
                and isinstance(coedge["native_edge_reference_sha256"], str)
                and bool(HEX64.fullmatch(coedge["native_edge_reference_sha256"]))
                and edge_reference not in seen_edges
                and isinstance(edge, dict)
                and edge["native_edge_reference_sha256"] == coedge["native_edge_reference_sha256"]
                and edge["fin_ids_source"] == NATIVE_ID_ARRAY_SOURCE_PLANAR_BREP
                and edge["fin_ids"] == []
                and edge["raw_fin_ids"] is None
                and _finite_number(coedge["start_parameter"])
                and _finite_number(coedge["end_parameter"])
                and coedge["parameter_order"]
                == ("increasing" if coedge["start_parameter"] < coedge["end_parameter"] else "decreasing")
                and coedge["start_parameter"] != coedge["end_parameter"]
                and _finite_point(coedge["start_point_m"])
                and _finite_point(coedge["end_point_m"]),
                component + f": native planar BRep coedge {coedge_index} is malformed for body {body_key}",
            )
            _require(
                coedge["same_direction_as_edge"] is next(
                    row["EdgeInFaceSense"]["value"] for row in edge["face_senses"]
                    if row["face_index"] == body["face_index"]
                ),
                component + f": native planar coedge {coedge_index} disagrees with its native face sense",
            )
            edge_line_raw = coedge["edge_line_params"]
            coedge_line_raw = coedge["coedge_line_params"]
            _require(
                isinstance(edge_line_raw, list)
                and len(edge_line_raw) == 6
                and all(_finite_number(item) for item in edge_line_raw)
                and (
                    (
                        coedge["coedge_curve_present"] is True
                        and isinstance(coedge_line_raw, list)
                        and len(coedge_line_raw) == 6
                        and all(_finite_number(item) for item in coedge_line_raw)
                    )
                    or (
                        coedge["coedge_curve_present"] is False
                        and coedge_line_raw is None
                        and coedge["line_orientation"] == "not_observed"
                    )
                ),
                component + f": native planar line parameters are malformed for coedge {coedge_index}",
            )
            edge_line = _fraction_vector(edge_line_raw)
            edge_direction = edge_line[3:6]
            coedge_line = None if coedge_line_raw is None else _fraction_vector(coedge_line_raw)
            coedge_direction = None if coedge_line is None else coedge_line[3:6]
            trim_points = (
                _fraction_point(coedge["start_point_m"]),
                _fraction_point(coedge["end_point_m"]),
            )
            line_dot = (
                sum(edge_direction[index] * coedge_direction[index] for index in range(3))
                if coedge_direction is not None
                else 0
            )
            expected_orientation = "not_observed"
            if coedge_direction is not None:
                expected_orientation = (
                    "non_collinear"
                    if _vector_cross(edge_direction, coedge_direction) != (0, 0, 0)
                    else ("same" if line_dot > 0 else "opposite")
                )
            _require(
                any(edge_direction)
                and (coedge_direction is None or any(coedge_direction))
                and coedge["line_orientation"] == expected_orientation,
                component + f": native planar line relation differs for coedge {coedge_index}",
            )
            seen_edges.add(edge_reference)
            raw_curve_params = coedge["curve_params_raw"]
            _require(
                isinstance(raw_curve_params, list)
                and len(raw_curve_params) == 10
                and all(_finite_number(parameter) for parameter in raw_curve_params[:8])
                and raw_curve_params[6] == coedge["start_parameter"]
                and raw_curve_params[7] == coedge["end_parameter"]
                and raw_curve_params[:3] == coedge["start_point_m"]
                and raw_curve_params[3:6] == coedge["end_point_m"],
                component + f": native planar BRep curve parameters differ for coedge {coedge_index}",
            )
            start_set = endpoint_sets[coedge_index - 1] & endpoint_sets[coedge_index]
            end_set = endpoint_sets[coedge_index] & endpoint_sets[(coedge_index + 1) % len(coedges)]
            _require(
                len(start_set) == 1 and len(end_set) == 1 and start_set != end_set,
                component + f": native planar coedge {coedge_index} has ambiguous adjacent vertex incidence",
            )
            start_ref, end_ref = next(iter(start_set)), next(iter(end_set))
            _require(
                coedge["vertex_identity_rule"] == NATIVE_PLANAR_COEDGE_VERTEX_RULE
                and coedge["start_vertex_reference_sha256"] == start_ref
                and coedge["end_vertex_reference_sha256"] == end_ref,
                component + f": native planar coedge {coedge_index} violates adjacent vertex incidence",
            )
            expected_edge_point, expected_edge_end = vertex_points[start_ref], vertex_points[end_ref]
            start_tolerance = native_point_roundtrip_tolerance(coedge["start_point_m"], expected_edge_point)
            end_tolerance = native_point_roundtrip_tolerance(coedge["end_point_m"], expected_edge_end)
            for endpoint, trim_point, vertex_point, vertex_tolerance in (
                (
                    "start",
                    coedge["start_point_m"],
                    coedge["start_vertex_point_m"],
                    start_tolerance,
                ),
                (
                    "end",
                    coedge["end_point_m"],
                    coedge["end_vertex_point_m"],
                    end_tolerance,
                ),
            ):
                computed_error = native_point_error(
                    trim_point,
                    vertex_point,
                )
                computed_tolerance = native_point_roundtrip_tolerance(
                    trim_point,
                    vertex_point,
                )
                _require(
                    _finite_point(vertex_point)
                    and vertex_point == (expected_edge_point if endpoint == "start" else expected_edge_end)
                    and coedge[f"{endpoint}_vertex_point_error_m"] == computed_error
                    and coedge[f"{endpoint}_vertex_point_tolerance_m"] == computed_tolerance
                    and computed_tolerance == vertex_tolerance,
                    component
                    + f": native planar BRep coedge {coedge_index} {endpoint} vertex displacement is not reproducible",
                )
                source_error_squared = max(
                    source_error_squared,
                    sum(
                        (Fraction.from_float(trim_point[axis]) - Fraction.from_float(vertex_point[axis])) ** 2
                        for axis in range(3)
                    ),
                )
            # Independently reconstruct the directed view from edge-native
            # UMin/UMax payloads. Coedge-local parameters never enter this API.
            edge_evaluations = edge["curve_endpoint_evaluations"]
            parameter_points = tuple(edge_evaluations[key]["payload"][:3] for key in ("start", "end"))
            parameter_segments = (
                _vector_difference(_fraction_point(edge["curve_start_point_m"]),
                                   _fraction_point(edge["curve_end_point_m"])),
                _vector_difference(*map(_fraction_point, parameter_points)),
            )
            for key in ("start", "end"):
                derivative = _fraction_point(edge_evaluations[key]["payload"][3:6])
                _require(
                    all(sum(segment[axis] * derivative[axis] for axis in range(3)) > 0
                        for segment in parameter_segments),
                    component + f": native planar coedge {coedge_index} edge derivative "
                    "contradicts its parameter direction",
                )
            same_parameter_direction = edge["curve_sense"] is coedge["same_direction_as_edge"]
            source_endpoints = ("start", "end") if same_parameter_direction else ("end", "start")
            tangent_sign = 1 if same_parameter_direction else -1
            evaluations = [
                {
                    "point_m": edge_evaluations[key]["payload"][:3],
                    "tangent": [tangent_sign * value for value in edge_evaluations[key]["payload"][3:6]],
                }
                for key in source_endpoints
            ]
            segment = _vector_difference(_fraction_point(expected_edge_point), _fraction_point(expected_edge_end))
            raw_segment = _vector_difference(*trim_points)
            _require(
                sum(segment[axis] * raw_segment[axis] for axis in range(3)) > 0,
                component + f": native planar coedge {coedge_index} trim reverses its topological direction",
            )
            targets = (expected_edge_point, expected_edge_end)
            for endpoint_index, evaluation in enumerate(evaluations):
                tangent = _fraction_point(evaluation["tangent"])
                tangent_dot = sum(segment[axis] * tangent[axis] for axis in range(3))
                _require(
                    tangent_dot > 0 and sum(raw_segment[axis] * tangent[axis] for axis in range(3)) > 0,
                    component + f": native planar coedge {coedge_index} tangent contradicts its boundary line",
                )
                scale = tangent_dot / sum(value**2 for value in tangent)
                difference = tuple(segment[axis] - scale * tangent[axis] for axis in range(3))
                tangent_error_squared = max(tangent_error_squared, sum(value**2 for value in difference))
                source_error_squared = max(
                    source_error_squared, _planar_distance_squared(evaluation["point_m"], targets[endpoint_index])
                )
            curve_refs = [start_ref, end_ref]
            if edge["curve_sense"] is not coedge["same_direction_as_edge"]:
                curve_refs.reverse()
            _require(coedge["curve_vertex_identity_rule"] == NATIVE_PLANAR_EDGE_VERTEX_RULE
                     and coedge["curve_vertex_references_sha256"] == curve_refs,
                     component + f": native planar coedge {coedge_index} curve endpoint references differ")
            curve_targets = tuple(vertex_points[ref] for ref in curve_refs)
            curve_points = (edge["curve_start_point_m"], edge["curve_end_point_m"])
            evaluated_points = tuple(edge["curve_endpoint_evaluations"][key]["payload"][:3] for key in ("start", "end"))
            source_error_squared = max(
                source_error_squared,
                _planar_directed_segment_bound(component, curve_points, curve_targets),
                _planar_directed_segment_bound(component, evaluated_points, curve_targets),
                _planar_directed_segment_bound(component,
                    tuple(_planar_line_projection(point, edge_line) for point in curve_points), curve_targets),
            )
            if coedge_line is not None:
                source_error_squared = max(source_error_squared, _planar_directed_segment_bound(component,
                    tuple(_planar_line_projection(point, coedge_line) for point in trim_points), targets))
            start_reference = coedge["start_vertex_reference_sha256"]
            end_reference = coedge["end_vertex_reference_sha256"]
            _require(
                isinstance(start_reference, str)
                and bool(HEX64.fullmatch(start_reference))
                and isinstance(end_reference, str)
                and bool(HEX64.fullmatch(end_reference))
                and start_reference != end_reference,
                component + f": native planar BRep coedge {coedge_index} has invalid vertex identities",
            )
            boundary_references.append(start_reference)
            boundary_points.append(tuple(expected_edge_point))
        _require(
            seen_edges == set(edge_by_reference),
            component + f": native planar BRep loop does not cover every body edge for {body_key}",
        )
        for coedge_index, coedge in enumerate(coedges):
            next_coedge = coedges[(coedge_index + 1) % len(coedges)]
            _require(
                coedge["end_vertex_reference_sha256"] == next_coedge["start_vertex_reference_sha256"]
                and coedge["end_vertex_point_m"] == next_coedge["start_vertex_point_m"],
                component + f": native planar BRep loop is open at coedge {coedge_index}",
            )
        _require(
            len(set(boundary_references)) == len(boundary_references),
            component + f": native planar BRep repeats a boundary vertex for {body_key}",
        )
        expected_loop_reference = native_scope_identity_sha256(
            {
                "rule": f"{NATIVE_PLANAR_BREP_PROVIDER}/loop",
                "face_reference_sha256": span["face_reference_sha256"],
                "edge_references": [coedge["edge_reference_sha256"] for coedge in coedges],
            }
        )
        _require(
            loop["reference_sha256"] == expected_loop_reference,
            component + f": native planar BRep loop identity is not reproducible for {body_key}",
        )

        boundary_vertices = body["boundary_vertices"]
        _require(
            isinstance(boundary_vertices, list) and len(boundary_vertices) == len(boundary_points),
            component + f": native planar BRep boundary inventory is incomplete for {body_key}",
        )
        for vertex_index, row in enumerate(boundary_vertices):
            _require(
                isinstance(row, dict)
                and set(row) == boundary_required
                and row["vertex_index"] == vertex_index
                and _integer(row["vertex_index"])
                and row["vertex_reference_sha256"] == boundary_references[vertex_index]
                and row["point_m"] == list(boundary_points[vertex_index]),
                component + f": native planar BRep boundary vertex {vertex_index} is inconsistent",
            )

        triangulation = body["triangulation"]
        _require(
            isinstance(triangulation, dict)
            and set(triangulation)
            == {
                "rule_version",
                "face_normal",
                "error_components_m",
                "surface_error_upper_bound_m",
                "surface_error_limit_m",
                "boundary_area_m2",
                "triangles",
            }
            and _finite_number(triangulation["boundary_area_m2"])
            and triangulation["boundary_area_m2"] > 0
            and isinstance(triangulation["error_components_m"], dict)
            and all(_finite_number(value) and value >= 0 for value in triangulation["error_components_m"].values())
            and _finite_number(triangulation["surface_error_upper_bound_m"])
            and _finite_number(triangulation["surface_error_limit_m"])
            and isinstance(triangulation["triangles"], list),
            component + f": native planar BRep triangulation is malformed for {body_key}",
        )
        computed_face_normal = tuple(surface["face_normal_unit"])
        raw_face_normal = surface["face_normal_raw"]
        plane_point = surface["plane_params"][3:6]
        exact_projected = tuple(
            _planar_project_to_plane(point, plane_point, raw_face_normal) for point in boundary_points
        )
        constructed_points = tuple(tuple(float(value) for value in point) for point in exact_projected)
        _require(
            all(all(math.isfinite(value) for value in point) for point in constructed_points),
            component + ": native planar projection is outside binary64",
        )
        orientation_sign, _projected_boundary_points, boundary_area2_signed = _validate_planar_boundary(
            component, exact_projected, raw_face_normal, "native planar BRep"
        )
        stl_boundary = tuple(_binary32_point(point) for point in constructed_points)
        _, _projected_stl_boundary, stl_boundary_area2 = _validate_planar_boundary(
            component, stl_boundary, raw_face_normal, "binary32 planar BRep"
        )
        plane_residual_squared = max(
            _plane_residual_squared(point, plane_point, raw_face_normal) for point in boundary_points
        )
        construction_squared = max(
            _planar_distance_squared(native, exact)
            for native, exact in zip(boundary_points, exact_projected, strict=True)
        )
        projection_rounding_squared = max(
            _planar_distance_squared(exact, rounded)
            for exact, rounded in zip(exact_projected, constructed_points, strict=True)
        )
        triangles = triangulation["triangles"]
        _require(
            len(triangles) == span["triangle_count"] == len(boundary_points) - 2,
            component + f": native planar BRep triangle count differs from its face span for {body_key}",
        )
        _require(
            triangulation["rule_version"] == NATIVE_PLANAR_CONVEX_FAN_RULE
            and triangulation["face_normal"] == list(computed_face_normal),
            component + f": native planar BRep triangulation rule or normal differs for {body_key}",
        )
        normal_exact = _fraction_point(surface["face_normal_raw"])
        dominant_axis = max(range(3), key=lambda axis: abs(raw_face_normal[axis]))
        boundary_area = native_binary64_sqrt(
            boundary_area2_signed**2 * sum(value**2 for value in normal_exact) / (4 * normal_exact[dominant_axis] ** 2)
        )
        _require(
            triangulation["boundary_area_m2"] == boundary_area,
            component + f": native planar BRep boundary area is not reproducible for {body_key}",
        )
        triangle_polygons = []
        triangle_area2 = Fraction(0, 1)
        stl_triangle_area2 = Fraction(0, 1)
        stl_triangle_polygons = []
        binary32_error_squared = Fraction(0, 1)
        directed_edges = defaultdict(int)
        boundary_directed_edges = {(index, (index + 1) % len(boundary_points)) for index in range(len(boundary_points))}
        for expected_index, triangle in enumerate(triangles):
            _require(
                isinstance(triangle, dict)
                and set(triangle) == triangle_required
                and triangle["triangle_index"] == expected_index
                and _integer(triangle["triangle_index"])
                and isinstance(triangle["vertex_indices"], list)
                and len(triangle["vertex_indices"]) == 3
                and all(
                    _integer(vertex_index) and 0 <= vertex_index < len(boundary_points)
                    for vertex_index in triangle["vertex_indices"]
                )
                and len(set(triangle["vertex_indices"])) == 3
                and triangle["vertex_indices"] == [0, expected_index + 1, expected_index + 2]
                and isinstance(triangle["native_points"], list)
                and len(triangle["native_points"]) == 3
                and all(_finite_point(point) for point in triangle["native_points"])
                and isinstance(triangle["constructed_points"], list)
                and len(triangle["constructed_points"]) == 3
                and all(_finite_point(point) for point in triangle["constructed_points"])
                and all(
                    tuple(triangle["constructed_points"][position]) == constructed_points[vertex_index]
                    for position, vertex_index in enumerate(triangle["vertex_indices"])
                )
                and isinstance(triangle["stl_points"], list)
                and len(triangle["stl_points"]) == 3
                and all(_finite_point(point) for point in triangle["stl_points"])
                and all(
                    tuple(triangle["native_points"][position]) == boundary_points[vertex_index]
                    for position, vertex_index in enumerate(triangle["vertex_indices"])
                ),
                component + f": native planar BRep triangle {expected_index} is malformed",
            )
            _, projected_triangle = _projected_boundary(
                [exact_projected[index] for index in triangle["vertex_indices"]],
                raw_face_normal,
            )
            triangle_orientation = _cross2(
                projected_triangle[0],
                projected_triangle[1],
                projected_triangle[2],
            )
            _require(
                triangle_orientation * orientation_sign > 0,
                component + f": native planar BRep triangle {expected_index} reverses orientation",
            )
            constructed_triangle = tuple(_fraction_point(point) for point in triangle["constructed_points"])
            for position in range(3):
                expected_stl_point = _binary32_point(triangle["constructed_points"][position])
                _require(
                    expected_stl_point == tuple(triangle["stl_points"][position]),
                    component
                    + f": native planar BRep triangle {expected_index} "
                    + "does not contain the exact binary32 point",
                )
                binary32_error_squared = max(
                    binary32_error_squared,
                    sum(
                        (
                            Fraction.from_float(triangle["stl_points"][position][axis])
                            - constructed_triangle[position][axis]
                        )
                        ** 2
                        for axis in range(3)
                    ),
                )
            _, stl_polygon = _projected_boundary(triangle["stl_points"], raw_face_normal)
            stl_orientation = _cross2(*stl_polygon)
            stl_exact = tuple(_fraction_point(point) for point in triangle["stl_points"])
            stl_cross = _vector_cross(
                _vector_difference(stl_exact[0], stl_exact[1]), _vector_difference(stl_exact[0], stl_exact[2])
            )
            _require(
                stl_orientation * orientation_sign > 0
                and sum(stl_cross[axis] * Fraction(raw_face_normal[axis]) for axis in range(3)) > 0,
                component + f": native planar BRep binary32 triangle {expected_index} reverses orientation",
            )
            stl_triangle_polygons.append(stl_polygon)
            stl_triangle_area2 += stl_orientation * orientation_sign
            triangle_area2 += triangle_orientation * orientation_sign
            triangle_polygons.append(projected_triangle)
            first, second, third = triangle["vertex_indices"]
            for edge in (
                (first, second),
                (second, third),
                (third, first),
            ):
                directed_edges[edge] += 1
            if triangle_vertices is not None:
                stl_record = triangle_vertices[span["triangle_start"] + expected_index]
                _require(
                    [list(point) for point in stl_record] == triangle["stl_points"],
                    component + f": native planar BRep triangle {expected_index} " + "does not bind its STL record",
                )
        _require(
            all(directed_edges[edge] == 1 for edge in boundary_directed_edges)
            and set(boundary_directed_edges) <= set(directed_edges),
            component + f": native planar BRep triangles do not cover every directed boundary edge for {body_key}",
        )
        for edge, count in directed_edges.items():
            if edge in boundary_directed_edges:
                _require(
                    count == 1,
                    component + f": native planar BRep repeats a boundary edge for {body_key}",
                )
                continue
            _require(
                count == 1 and directed_edges[tuple(reversed(edge))] == 1,
                component + f": native planar BRep internal edges are not reciprocal for {body_key}",
            )
        _require(
            triangle_area2 == abs(boundary_area2_signed) and stl_triangle_area2 == abs(stl_boundary_area2),
            component + f": native planar BRep triangles do not preserve boundary area for {body_key}",
        )
        for first_index, first_triangle in enumerate(triangle_polygons):
            for second_triangle in triangle_polygons[first_index + 1 :]:
                _require(
                    not _triangles_overlap(first_triangle, second_triangle),
                    component + f": native planar BRep triangles overlap for {body_key}",
                )
        for first_index, first_triangle in enumerate(stl_triangle_polygons):
            for second_triangle in stl_triangle_polygons[first_index + 1 :]:
                _require(
                    not _triangles_overlap(first_triangle, second_triangle),
                    component + f": native planar BRep binary32 triangles overlap for {body_key}",
                )
        surface_error_limit = Fraction.from_float(NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M)
        error_components = {
            "native_plane_residual": _outward_sqrt(plane_residual_squared),
            "constructive_vertex_displacement": _outward_sqrt(construction_squared),
            "binary64_projection_displacement": _outward_sqrt(projection_rounding_squared),
            "cross_api_vertex_displacement": _outward_sqrt(source_error_squared),
            "tangent_segment_displacement": _outward_sqrt(tangent_error_squared),
            "binary32_vertex_displacement": _outward_sqrt(binary32_error_squared),
        }
        surface_error_bound = native_binary64_sum_upper(error_components.values())
        _require(
            Fraction.from_float(surface_error_bound) <= surface_error_limit,
            component + f": native planar BRep complete surface bound exceeds its limit for {body_key}",
        )
        _require(
            triangulation["error_components_m"] == error_components
            and triangulation["surface_error_upper_bound_m"] == surface_error_bound
            and triangulation["surface_error_limit_m"] == NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
            component + f": native planar BRep surface bound is not reproducible for {body_key}",
        )
    return covered_bodies


def _parse_itessellation_evidence(
    component: str,
    value: object,
    spans_by_body: dict,
    face_spans_by_key: dict,
    native_edges: list[dict],
    body_references: dict,
    triangles: int,
    triangle_vertices: list[tuple[tuple[float, float, float], ...]] | None,
    native_fin_resolutions: list[dict] | None,
    native_identity_warnings: list[dict] | None,
    itessellation_bodies: set,
) -> None:
    """Validate body-local ITessellation facet/fin evidence fail closed."""

    required = {
        "interface_iid",
        "u_rule_version",
        "u_resolution_rule",
        "point_roundtrip_rule",
        "property_requests",
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
            "edge": base_owner_keys,
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
            "evaluate2_calls",
        }
        and all(_integer(item) and item >= 0 for item in stats.values())
        and stats["get_closest_point_on_calls"]
        == stats["unique_observations"]
        and stats["evaluate2_calls"] == stats["unique_observations"],
        component + ": native U observation statistics are inconsistent",
    )
    properties = value["properties"]
    property_requests = value["property_requests"]
    expected_boolean_properties = (
        "improved_quality",
        "need_error_list",
        "need_face_facet_map",
        "need_edge_fin_map",
        "need_vertex_params",
    )

    def properties_are_strict(candidate):
        return (
            isinstance(candidate, dict)
            and set(candidate) == {"match_type", *expected_boolean_properties}
            and type(candidate["match_type"]) is int
            and candidate["match_type"] == 0
            and all(
                type(candidate[key]) is bool and candidate[key] is True
                for key in expected_boolean_properties
            )
        )

    _require(
        properties_are_strict(property_requests),
        component + ": native ITessellation property requests are incomplete",
    )
    _require(
        properties_are_strict(properties),
        component + ": native ITessellation properties are incomplete",
    )
    _require(
        properties == property_requests,
        component
        + ": native ITessellation requested and observed properties differ",
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
            component + ": native STL triangle vertices are unavailable for v7 binding",
        )
    raw_bodies = value["bodies"]
    _require(
        isinstance(raw_bodies, list),
        component + ": native ITessellation body evidence is missing",
    )

    body_evidence = {}
    body_counts = {}
    previous_body = None
    for index, body in enumerate(raw_bodies):
        _require(
            isinstance(body, dict)
            and set(body)
            in (
                {
                "body_type",
                "body_index",
                "body_reference_sha256",
                "tessellate_result",
                "request",
                "property_requests",
                "properties",
                "facet_count",
                "fin_count",
                "vertex_count",
                "faces",
                "facets",
                "fins",
                },
                {
                    "body_type",
                    "body_index",
                    "body_reference_sha256",
                    "scope",
                    "tessellate_result",
                    "request",
                    "property_requests",
                    "properties",
                    "facet_count",
                    "fin_count",
                    "vertex_count",
                    "faces",
                    "facets",
                    "fins",
                },
            ),
            component + f": native tessellation body {index} has an unknown shape",
        )
        body_key = (body["body_type"], body["body_index"])
        _require(
            _integer(body_key[0])
            and body_key[0] in (0, 1)
            and _integer(body_key[1])
            and body_key[1] >= 0
            and body_key in itessellation_bodies,
            component + f": native tessellation body {index} is unknown",
        )
        _require(
            previous_body is None or body_key > previous_body,
            component + f": native tessellation body {index} is duplicated or unordered",
        )
        _require(
            properties_are_strict(body["property_requests"])
            and properties_are_strict(body["properties"])
            and body["property_requests"] == property_requests
            and body["properties"] == properties,
            component
            + f": native tessellation body {index} properties differ from the request",
        )
        request = body["request"]
        _require(
            isinstance(request, dict)
            and set(request) == {"api", "argument_count", "face_list"}
            and request["api"] == "IBody2.GetTessellation"
            and type(request["argument_count"]) is int
            and request["argument_count"] == 1
            and isinstance(request["face_list"], dict)
            and set(request["face_list"])
            == {"scope", "variant_type", "value"}
            and request["face_list"]["scope"] == "whole_body"
            and request["face_list"]["variant_type"] == "VT_EMPTY"
            and request["face_list"]["value"] is None,
            component
            + f": native tessellation body {index} has an unsupported tessellation request",
        )
        previous_body = body_key
        _require(
            body["body_reference_sha256"] == body_references[body_key]
            and body["tessellate_result"] is True
            and _integer(body["facet_count"])
            and body["facet_count"] >= 0
            and _integer(body["fin_count"])
            and body["fin_count"] >= 0
            and _integer(body["vertex_count"])
            and body["vertex_count"] >= 0
            and isinstance(body["faces"], list)
            and isinstance(body["facets"], list)
            and isinstance(body["fins"], list),
            component + f": native tessellation body {body_key} is incomplete",
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
        set(body_evidence) == itessellation_bodies,
        component + ": native ITessellation evidence does not cover every assigned body",
    )

    raw_vertices = value["vertices"]
    _require(
        isinstance(raw_vertices, list),
        component + ": native tessellation vertex point cache is missing",
    )
    vertices = {}
    previous_vertex = None
    for index, vertex in enumerate(raw_vertices):
        _require(
            isinstance(vertex, dict)
            and set(vertex) == {"body_type", "body_index", "vertex_id", "point_m"},
            component + f": native tessellation vertex {index} has an unknown shape",
        )
        body_key = (vertex["body_type"], vertex["body_index"])
        vertex_id = vertex["vertex_id"]
        point = vertex["point_m"]
        _require(
            _integer(vertex["body_type"])
            and vertex["body_type"] in (0, 1)
            and _integer(vertex["body_index"])
            and vertex["body_index"] >= 0
            and body_key in body_evidence
            and _integer(vertex_id)
            and vertex_id >= 0
            and isinstance(point, list)
            and len(point) == 3
            and all(
                type(coordinate) in (int, float) and math.isfinite(coordinate)
                for coordinate in point
            ),
            component + f": native tessellation vertex {index} is malformed",
        )
        order = (*body_key, vertex_id)
        _require(
            previous_vertex is None or order > previous_vertex,
            component + f": native tessellation vertex {index} is duplicated or unordered",
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
            _integer(row["body_type"])
            and row["body_type"] in (0, 1)
            and _integer(row["body_index"])
            and row["body_index"] >= 0
            and body_key in body_references
            and _integer(row["vertex_index"])
            and row["vertex_index"] >= 0
            and isinstance(point, list)
            and len(point) == 3
            and all(type(value) in (int, float) and math.isfinite(value) for value in point)
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
            component + f": native scope-local vertex {index} is malformed",
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
    for edge in native_edges:
        expected_cross_kind_owners[
            edge["native_edge_reference_sha256"]
        ].append(
            {
                "kind": "edge",
                "component": component,
                "body_type": edge["body_type"],
                "body_index": edge["body_index"],
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
            str(owner.get("typed_sha256", "")),
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
            if (
                edge["body_type"],
                edge["body_index"],
            )
            in itessellation_bodies
        ),
        component + ": an edge endpoint is not a captured scope-local vertex",
    )

    for body_key, evidence in body_evidence.items():
        _require(
            len(evidence["facets"]) == body_counts[body_key]["facet_count"],
            component + f": native tessellation facet count differs for body {body_key}",
        )
        _require(
            len(evidence["fins"]) == body_counts[body_key]["fin_count"],
            component + f": native tessellation fin count differs for body {body_key}",
        )
        cached = {vertex_id for owner, vertex_id in vertices if owner == body_key}
        referenced = {
            vertex_id
            for fin in evidence["fins"].values()
            for vertex_id in fin["vertex_ids"]
        }
        _require(
            len(cached) == body_counts[body_key]["vertex_count"],
            component + f": native tessellation vertex count differs for body {body_key}",
        )
        _require(
            cached == referenced,
            component + ": native tessellation vertex cache differs from fin vertices",
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
            + f": native tessellation edge references differ for body {body_key}",
        )
        for reference, edge in edges_by_reference.items():
            if (
                edge["start_point_m"] is None
                or edge["end_point_m"] is None
            ):
                _require(
                    edge["topology_endpoint_assignment"] is None,
                    component
                    + f": native tessellation edge {reference} "
                    + "absent endpoints carry a topology assignment",
                )
                continue
            topology_endpoints = native_curve_parameter_endpoints(
                edge["curve_start_point_m"],
                edge["curve_end_point_m"],
            )
            assignment = native_curve_topology_endpoint_set_assignment(
                topology_endpoints[0],
                topology_endpoints[1],
                edge["start_point_m"],
                edge["end_point_m"],
                shared_vertex=(edge["start_vertex_reference_sha256"] == edge["end_vertex_reference_sha256"]),
            )
            _require(
                edge["topology_endpoint_assignment"] == assignment,
                component
                + f": native tessellation edge {reference} "
                + "topology endpoint assignment is not independently reproducible",
            )
        for fin in evidence["fins"].values():
            if fin["classification"] == "internal_only":
                continue
            reference = fin["edge_reference_sha256"]
            edge = edges_by_reference.get(reference)
            _require(
                edge is not None,
                component
                + f": native fin {fin['fin_id']} has no captured edge domain",
            )
            for _endpoint_name, vertex_id, u_value, evidence_value in (
                (
                    "start",
                    fin["vertex_ids"][0],
                    fin["start_u"],
                    fin["start_evidence"],
                ),
                (
                    "end",
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
            component + f": native tessellation edge references differ for body {body_key}",
        )
        for reference, edge in body_edges.items():
            declared = set(edge["fin_ids"])
            actual = fins_by_edge_reference.get(reference, set())
            _require(
                declared == actual,
                component
                + f": native tessellation edge {reference} fin_ids are not bidirectional",
            )
            body_vertices = {
                vertex_id: point
                for (owner_body, vertex_id), point in vertices.items()
                if owner_body == body_key
            }
            try:
                relation = validate_native_edge_fin_relations(
                    component=component,
                    edge={
                        "body_type": body_key[0],
                        "face_indices": edge["face_indices"],
                        "fin_ids": edge["fin_ids"],
                        "face_senses": edge["face_senses"],
                    },
                    edge_fins=sorted(actual),
                    incident_face_facet_counts=[
                        face_spans_by_key[
                            (*body_key, face_index)
                        ]["triangle_count"]
                        for face_index in edge["face_indices"]
                    ],
                    fins_by_id=evidence["fins"],
                    vertices_by_id=body_vertices,
                )
            except NativeFinRelationError as error:
                raise PipelineError(error.message) from error
            u_min = float(edge["u_min"])
            u_max = float(edge["u_max"])
            is_closed_edge = bool(
                (edge["topology_endpoint_assignment"] is not None
                 and edge["topology_endpoint_assignment"]["shared_vertex"])
                or edge["endpoint_points_derived_from_curve"]
            )
            partitions = []
            partition_by_fin = {}
            if is_closed_edge:
                for face_index in edge["face_indices"]:
                    face_fins = {fin_id: evidence["fins"][fin_id] for fin_id in actual
                                 if evidence["fins"][fin_id]["face_index"] == face_index}
                    if not face_fins:
                        continue
                    try:
                        partition = closed_edge_fin_partition(
                            component=component, fins=face_fins, lower=u_min, upper=u_max)
                    except NativeFinRelationError as error:
                        raise PipelineError(error.message) from error
                    partitions.append({"face_index": face_index, **partition})
                    partition_by_fin.update({row["fin_id"]: row["intervals"] for row in partition["segments"]})
            _require(edge["closed_fin_cycles"] == partitions,
                     component + ": closed edge partition is not independently reproducible")
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
                    + f": native tessellation edge {reference} fin {fin_id} U is invalid",
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
                    component=component,
                    reference=reference,
                )
                if span is not None:
                    collapsed_fin_span[fin_id] = span
                    continue
                intervals_by_face.setdefault(fin["face_index"], []).extend(
                    partition_by_fin[fin_id] if is_closed_edge else intervals
                )
            for face_index, intervals in intervals_by_face.items():
                _require(
                    native_u_intervals_cover(intervals, u_min, u_max),
                    component
                    + f": native tessellation edge {reference} face {face_index} does not cover its U domain",
                )
            if (edge["start_vertex_reference_sha256"] is not None
                    and edge["start_vertex_reference_sha256"] == edge["end_vertex_reference_sha256"]):
                evaluated_edge_points = {
                    tuple(evidence["fins"][fin_id][f"{endpoint}_evidence"]["evaluate_payload"][:3])
                    for fin_id in actual for endpoint in ("start", "end")
                }
                _require(len(evaluated_edge_points) >= 2 and any(
                    not native_u_equal(evidence["fins"][fin_id]["start_u"], evidence["fins"][fin_id]["end_u"])
                    for fin_id in actual),
                    component + ": closed native edge lacks nonzero evaluated fin geometry")
            curve_points = {
                tuple(_float32(value) for value in edge["curve_start_point_m"]),
                tuple(_float32(value) for value in edge["curve_end_point_m"]),
            }
            endpoint_points = (
                edge["start_point_m"],
                edge["end_point_m"],
            )
            if all(point is not None for point in endpoint_points):
                topology_endpoints = native_curve_parameter_endpoints(
                    edge["curve_start_point_m"],
                    edge["curve_end_point_m"],
                )
                assignment = native_curve_topology_endpoint_set_assignment(
                    topology_endpoints[0],
                    topology_endpoints[1],
                    endpoint_points[0],
                    endpoint_points[1],
                    shared_vertex=(edge["start_vertex_reference_sha256"] == edge["end_vertex_reference_sha256"]),
                )
                _require(
                    edge["topology_endpoint_assignment"] == assignment,
                    component
                    + f": native tessellation edge {reference} "
                    + "topology endpoint assignment is not independently reproducible",
                )
                _require(
                    assignment["accepted"],
                    component
                    + f": native tessellation edge {reference} "
                    + "unordered endpoint set is incomplete",
                )
            else:
                _require(
                    edge.get("topology_endpoint_assignment") is None,
                    component
                    + f": native tessellation edge {reference} "
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
                    + f": native tessellation edge {reference} curve endpoints differ",
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
                            + f": native tessellation edge {reference} "
                            + f"{edge_endpoint} endpoint conflicts with fin {fin_id} "
                            + f"{fin_endpoint} at the same U",
                        )
            for fin_id, cofin_id in relation["collapsed_pairs"]:
                if native_fin_resolutions is None:
                    continue
                fin = evidence["fins"][fin_id]
                cofin = evidence["fins"][cofin_id]
                _require(
                    fin_id in collapsed_fin_span
                    and cofin_id in collapsed_fin_span,
                    component
                    + f": native tessellation fin {fin_id} "
                    + "has unbound collapsed span evidence",
                )
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
                + f": native tessellation edge {reference} has a collapsed-only face without positive U coverage",
            )
            _require(
                fin_faces == active_faces,
                component
                + f": native tessellation edge {reference} does not cover its tessellated faces",
            )
            _require(
                bool(active_faces)
                or all(
                    face_spans_by_key[(*body_key, face_index)]["triangle_count"] == 0
                    for face_index in edge["face_indices"]
                ),
                component
                + f": native tessellation edge {reference} has no fin without an untessellated face",
            )
            if native_fin_resolutions is not None:
                for partition in partitions:
                    native_fin_resolutions.append({
                        "code": "native_closed_fin_partition_derived",
                        "blocking": False,
                        "severity": "warning",
                        "message": "Closed edge parameter coverage is derived from the complete native fin cycle.",
                        "object": f"{component}:edge:{reference}",
                        "detail": {
                            "component": component,
                            "body_type": body_key[0],
                            "body_index": body_key[1],
                            "edge_reference_sha256": reference,
                            "face_index": partition["face_index"],
                            "field": "native_edges.closed_fin_cycles",
                            "rule_version": partition["rule_version"],
                            "derived_fields": ["vertex_cycle", "fin_cycle", "parameter_node_count", "segments"],
                        },
                    })


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
                "facet_count",
                "facet_ids_source",
                "raw_facet_ids",
                "warnings",
                "face_identity_sha256",
            },
            component + f": native tessellation face {index} has an unknown shape",
        )
        face_index = face["face_index"]
        facet_ids = face["facet_ids"]
        facet_count = face["facet_count"]
        _require(
            _integer(face_index)
            and face_index in face_spans
            and isinstance(facet_ids, list)
            and facet_ids == sorted(set(facet_ids))
            and all(_integer(facet_id) and facet_id >= 0 for facet_id in facet_ids)
            and _integer(facet_count)
            and facet_count >= 0
            and facet_count == len(facet_ids)
            and face["facet_ids_source"]
            in {
                NATIVE_ID_ARRAY_SOURCE_FLAT,
                NATIVE_ID_ARRAY_SOURCE_BODY_GLOBAL_NULL_ZERO,
            }
            and (
                (
                    face["facet_ids_source"] == NATIVE_ID_ARRAY_SOURCE_FLAT
                    and isinstance(face["raw_facet_ids"], list)
                    and all(
                        _integer(value) and value >= 0
                        for value in face["raw_facet_ids"]
                    )
                    and sorted(face["raw_facet_ids"]) == facet_ids
                    and len(face["raw_facet_ids"]) == facet_count
                )
                or (
                    face["facet_ids_source"]
                    == NATIVE_ID_ARRAY_SOURCE_BODY_GLOBAL_NULL_ZERO
                    and body_key[0] == 1
                    and facet_count == 0
                    and facet_ids == []
                    and face["raw_facet_ids"] is None
                )
            )
            and face["warnings"]
            == (
                [NATIVE_FACE_FACET_IDS_DERIVED]
                if face["facet_ids_source"]
                == NATIVE_ID_ARRAY_SOURCE_BODY_GLOBAL_NULL_ZERO
                else []
            )
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
            component + f": native tessellation face {index} is malformed",
        )
        previous_face = face_index
        faces[face_index] = set(facet_ids)
    _require(
        set(faces) == set(face_spans),
        component + f": native tessellation body {body_key} does not cover every face",
    )
    _require(
        sum(len(facet_ids) for facet_ids in faces.values())
        == body["facet_count"]
        and len(
            {
                facet_id
                for facet_ids in faces.values()
                for facet_id in facet_ids
            }
        )
        == body["facet_count"],
        component
        + f": native tessellation body {body_key} face arrays do not close the native facet total",
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
            component + f": native tessellation facet {index} has an unknown shape",
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
            and _integer(face_index)
            and face_index >= 0
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
            component + f": native tessellation facet {index} is malformed",
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
            + f": native tessellation facet {facet_id} has duplicate or misplaced triangle index",
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
        component + f": native tessellation body {body_key} does not cover every triangle record",
    )
    _require(
        {
            facet_id
            for facet_ids in faces.values()
            for facet_id in facet_ids
        }
        == set(facets),
        component + f": native tessellation body {body_key} face-to-facet map is not closed",
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
            + f": native tessellation body {body_key} face {face_index} has misowned facets",
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
            component + f": native tessellation fin {index} has an unknown shape",
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
            and _integer(facet_id)
            and facet_id >= 0
            and facet_id in facets
            and _integer(face_index)
            and face_index >= 0
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
            component + f": native tessellation fin {index} is malformed or unclassified",
        )
        previous_fin = fin_id
        _require(
            fin_id in facets[facet_id]["fin_ids"],
            component + f": native tessellation fin {fin_id} is absent from its facet",
        )
        previous = facet_fin_membership.setdefault(fin_id, facet_id)
        _require(
            previous == facet_id,
            component + f": native tessellation fin {fin_id} belongs to multiple facets",
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
        component + f": native tessellation body {body_key} facet-to-fin map is not closed",
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
            + f": native tessellation facet {facet['facet_id']} is not a directed three-cycle",
        )
        cycle = [fins[0]["vertex_ids"][0]]
        current = fins[0]
        used = set()
        for _ in range(3):
            _require(
                current["fin_id"] not in used,
                component
                + f": native tessellation facet {facet['facet_id']} repeats a directed fin",
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
                + f": native tessellation facet {facet['facet_id']} has an ambiguous directed cycle",
            )
            current = matches[0]
        _require(
            current["fin_id"] == fins[0]["fin_id"]
            and cycle[0] == cycle[-1]
            and len(set(cycle[:-1])) == 3,
            component
            + f": native tessellation facet {facet['facet_id']} does not form one directed three-cycle",
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
            + f": native tessellation facet {facet['facet_id']} does not bind "
            + f"triangle record {facet['triangle_record_index']}",
        )


def summarize_native_topology(
    tessellation: dict,
    spans: list[dict],
    *,
    component: str | None = None,
) -> dict:
    """Summarize native edge incidence without inventing geometric adjacency."""

    _require(
        isinstance(tessellation, dict) and isinstance(spans, list),
        "native topology summary received malformed evidence",
    )
    itessellation_bodies = (tessellation.get("itessellation") or {}).get("bodies") or []
    planar_bodies = (tessellation.get("planar_brep") or {}).get("bodies") or []
    raw_bodies = [*itessellation_bodies, *planar_bodies]
    _require(
        isinstance(raw_bodies, list) and bool(raw_bodies),
        "native topology summary has no native bodies",
    )
    body_counts = {"solid": 0, "sheet": 0}
    for body in raw_bodies:
        _require(
            isinstance(body, dict)
            and type(body.get("body_type")) is int
            and body["body_type"] in (0, 1)
            and type(body.get("body_index")) is int
            and body["body_index"] >= 0,
            "native topology summary has malformed body identity",
        )
        body_counts["solid" if body["body_type"] == 0 else "sheet"] += 1
    body_keys = [(body["body_type"], body["body_index"]) for body in raw_bodies]
    _require(
        len(set(body_keys)) == len(body_keys),
        "native topology summary providers overlap or repeat a body",
    )
    raw_spans = tessellation.get("face_spans")
    _require(
        isinstance(raw_spans, list) and bool(raw_spans),
        "native topology summary has no face spans",
    )
    triangle_count = 0
    for span in raw_spans:
        _require(
            isinstance(span, dict)
            and type(span.get("body_type")) is int
            and span["body_type"] in (0, 1)
            and type(span.get("body_index")) is int
            and span["body_index"] >= 0
            and type(span.get("face_index")) is int
            and span["face_index"] >= 0
            and type(span.get("triangle_count")) is int
            and span["triangle_count"] >= 0,
            "native topology summary has malformed face-span identity",
        )
        triangle_count += span["triangle_count"]
    reparsed_spans = parse_native_tessellation(
        component or "native topology summary",
        tessellation,
        body_counts,
        triangle_count,
        None,
    )
    _require(
        spans == reparsed_spans,
        "native topology summary spans differ from the current native graph",
    )
    body_rows = {}
    for span in reparsed_spans:
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
    vertex_owners = {}
    for edge in tessellation["native_edges"]:
        key = (edge["body_type"], edge["body_index"])
        row = body_rows[key]
        row["native_edges"] += 1
        incidence = len(edge["face_indices"])
        if incidence == 1:
            row["open_edges"] += 1
            row["boundary_fins"] += len(edge["fin_ids"])
        elif incidence == 2:
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
