"""Project Generate value warnings from sealed native records.

This module is producer-owned. It reads the raw tessellation observations and
records the values Generate used. The verification package independently
re-derives the same claims from the same evidence and must not call this code.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from ...geometry.native_u import (
    NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
    NATIVE_TOLERANT_EDGE_AFFECTED_FIELDS,
    NATIVE_TOLERANT_EDGE_GAP_SOURCE,
    NATIVE_TOLERANT_EDGE_GAP_WARNING,
    NATIVE_TOLERANT_EDGE_WARNING_MESSAGE,
    NATIVE_TOLERANT_EDGE_WARNING_QUALIFICATION,
    NATIVE_TOLERANT_EDGE_WARNING_REASON,
)
from ...io import PipelineError, confined, read_data


def _require(condition: object, message: str) -> None:
    if not condition:
        raise PipelineError(message)


def _object(value: object, where: str) -> dict:
    _require(isinstance(value, dict), f"{where} must be an object")
    return value


def _list(value: object, where: str) -> list:
    _require(isinstance(value, list), f"{where} must be a list")
    return value


def _text(value: object, where: str) -> str:
    _require(isinstance(value, str) and bool(value), f"{where} must be non-empty text")
    return value


def _integer(value: object, where: str) -> int:
    _require(type(value) is int and value >= 0, f"{where} must be a non-negative integer")
    return value


def _warning_object(
    *,
    component: str,
    body_type: int,
    body_index: int,
    fin: dict,
    edge: dict,
    endpoint: str,
    source_scope: dict,
) -> dict:
    endpoint_evidence = _object(fin.get(f"{endpoint}_evidence"), f"{component}:{fin.get('fin_id')}:{endpoint}_evidence")
    tolerant = _object(
        endpoint_evidence.get("tolerant_edge_evidence"),
        f"{component}:{fin.get('fin_id')}:{endpoint}:tolerant_edge_evidence",
    )
    _require(tolerant.get("gap_used") is True, "Tolerant-edge observation did not use the native gap")
    tolerance = _object(edge.get("edge_tolerance"), f"{component}:edge_tolerance")
    open_identity = fin.get("open_edge_boundary_identity")
    open_identity_sha256 = (
        _text(open_identity.get("identity_chain_sha256"), "open_edge_boundary_identity.identity_chain_sha256")
        if isinstance(open_identity, dict)
        else None
    )
    numeric = {}
    for name in (
        "evaluate_point_error_m",
        "point_tolerance_m",
        "projection_to_curve_l2_error_m",
        "eligible_gap_m",
        "composed_projection_tolerance_m",
    ):
        value = tolerant.get(name)
        _require(
            type(value) in (int, float) and math.isfinite(float(value)) and value >= 0,
            f"tolerant_edge_evidence.{name} must be a finite non-negative number",
        )
        numeric[name] = float(value)
    detail = {
        "component": component,
        "body_type": body_type,
        "body_index": body_index,
        "edge_reference_sha256": _text(
            fin.get("edge_reference_sha256"),
            f"{component}:{fin.get('fin_id')}:edge_reference_sha256",
        ),
        "fin_id": _integer(fin.get("fin_id"), f"{component}:fin_id"),
        "fin_endpoint": endpoint,
        "parameter_boundary": _text(tolerant.get("parameter_boundary"), "tolerant_edge_evidence.parameter_boundary"),
        "point_roundtrip_rule": _text(
            tolerant.get("point_roundtrip_rule"),
            "tolerant_edge_evidence.point_roundtrip_rule",
        ),
        "evaluate_rule_version": _text(
            tolerant.get("evaluate_rule_version"),
            "tolerant_edge_evidence.evaluate_rule_version",
        ),
        "evaluate_metric": _text(tolerant.get("evaluate_metric"), "tolerant_edge_evidence.evaluate_metric"),
        **numeric,
        "projection_metric": _text(tolerant.get("projection_metric"), "tolerant_edge_evidence.projection_metric"),
        "gap_used": True,
        "field": f"{endpoint}_evidence.tolerant_edge_evidence",
        "affected_fields": list(NATIVE_TOLERANT_EDGE_AFFECTED_FIELDS),
        "gap_source_api": NATIVE_TOLERANT_EDGE_GAP_SOURCE,
        "gap_cap_m": NATIVE_TESSELLATION_EDGE_PROJECTION_TOLERANCE_M,
        "reason": NATIVE_TOLERANT_EDGE_WARNING_REASON,
        "qualification": NATIVE_TOLERANT_EDGE_WARNING_QUALIFICATION,
        "edge_tolerance_out_variant_type": _integer(
            tolerance.get("out_variant_type"),
            f"{component}:edge_tolerance.out_variant_type",
        ),
        "topology_endpoint_set": copy.deepcopy(
            _object(edge.get("topology_endpoint_assignment"), f"{component}:topology_endpoint_assignment")
        ),
        "source_scope": copy.deepcopy(source_scope),
        "open_edge_boundary_identity_sha256": open_identity_sha256,
    }
    return {
        "object_type": "native_tessellation_fin_endpoint",
        "object": (
            f"{component}:body:{body_type}/{body_index}:"
            f"edge:{detail['edge_reference_sha256']}:"
            f"fin:{detail['fin_id']}:{endpoint}"
        ),
        **detail,
    }


def generation_value_warnings(bundle: Path) -> list[dict]:
    """Return production warnings projected directly from sealed native rows."""

    delivery = Path(bundle)
    delivery = delivery.parent if delivery.name == "evidence" else delivery
    geometry_path = confined(delivery, "evidence/raw/geometry.json", exists=False)
    if not geometry_path.is_file():
        return []
    rows = _list(read_data(geometry_path), "evidence/raw/geometry.json")
    scopes_path = confined(delivery, "evidence/raw/source_scopes.json", exists=False)
    scopes = read_data(scopes_path) if scopes_path.is_file() else {}
    _require(isinstance(scopes, dict), "evidence/raw/source_scopes.json must be an object")

    objects = []
    for row_index, row in enumerate(rows):
        row = _object(row, f"geometry[{row_index}]")
        tessellation = row.get("tessellation")
        if tessellation is None:
            continue
        tessellation = _object(tessellation, f"geometry[{row_index}].tessellation")
        component = _text(row.get("component"), f"geometry[{row_index}].component")
        edges = _list(tessellation.get("native_edges"), f"{component}:tessellation.native_edges")
        edge_by_owner = {}
        for edge in edges:
            edge = _object(edge, f"{component}:native_edge")
            key = (
                _integer(edge.get("body_type"), f"{component}:native_edge.body_type"),
                _integer(edge.get("body_index"), f"{component}:native_edge.body_index"),
                _text(edge.get("edge_reference_sha256"), f"{component}:native_edge.edge_reference_sha256"),
            )
            _require(key not in edge_by_owner, "Native tessellation repeats an edge reference")
            edge_by_owner[key] = edge
        itessellation = _object(tessellation.get("itessellation"), f"{component}:tessellation.itessellation")
        bodies = _list(itessellation.get("bodies"), f"{component}:itessellation.bodies")
        component_objects = []
        for body in bodies:
            body = _object(body, f"{component}:itessellation.body")
            body_type = _integer(body.get("body_type"), f"{component}:body_type")
            body_index = _integer(body.get("body_index"), f"{component}:body_index")
            for fin in _list(body.get("fins"), f"{component}:body.fins"):
                fin = _object(fin, f"{component}:fin")
                if fin.get("classification") != "native_edge":
                    continue
                for endpoint in ("start", "end"):
                    evidence = fin.get(f"{endpoint}_evidence")
                    if not isinstance(evidence, dict) or evidence.get("tolerant_edge_evidence") is None:
                        continue
                    reference = _text(
                        fin.get("edge_reference_sha256"),
                        f"{component}:{fin.get('fin_id')}:edge_reference_sha256",
                    )
                    edge = edge_by_owner.get((body_type, body_index, reference))
                    _require(edge is not None, f"{component}: tolerant fin references no native edge")
                    scope = scopes.get(component)
                    _require(
                        isinstance(scope, dict),
                        f"{component}: tolerant-edge warning has no source scope",
                    )
                    component_objects.append(
                        _warning_object(
                            component=component,
                            body_type=body_type,
                            body_index=body_index,
                            fin=fin,
                            edge=edge,
                            endpoint=endpoint,
                            source_scope=scope,
                        )
                    )
        objects.extend(component_objects)

    if not objects:
        return []
    objects.sort(
        key=lambda value: json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    identities = [(item["object"], item["field"]) for item in objects]
    _require(len(identities) == len(set(identities)), "Tolerant-edge value warnings contain duplicate object fields")
    return [
        {
            "code": NATIVE_TOLERANT_EDGE_GAP_WARNING,
            "message": NATIVE_TOLERANT_EDGE_WARNING_MESSAGE,
            "objects": objects,
        }
    ]
