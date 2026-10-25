"""Native source-scope identity and derived-value warning rules."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from fractions import Fraction

from ..io import PipelineError


def native_binary64_sqrt(value: Fraction) -> float:
    """Round an exact nonnegative square root without platform libm results.

    The integer square root locates adjacent binary64 values. Their exact
    rational midpoint selects round-to-nearest, ties-to-even. Subnormal
    results use the same rule.
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


# Retained as the frozen source-scope schema value; it grants no open-edge identity.
NATIVE_SOURCE_SCOPE_RULE = "native-open-edge-scope-binding-v1"
NATIVE_SCOPE_IDENTITY_RULE = "native-scope-entity-id-v2"
NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED = "cad_scope_local_identity_derived"
NATIVE_TRIANGLE_STL_DERIVED = "cad_native_triangle_stl_derived"
NATIVE_SCOPE_IDENTITY_WARNING_CODES = frozenset({NATIVE_SCOPE_LOCAL_IDENTITY_DERIVED})
NATIVE_GATE_WARNING_CODES = NATIVE_SCOPE_IDENTITY_WARNING_CODES | frozenset(
    {
        NATIVE_TRIANGLE_STL_DERIVED,
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
        "source_scope_sha256",
    }
)


def native_scope_warning_detail_is_valid(detail: object) -> bool:
    """Whether one projected native-scope warning fully locates its object."""

    kind = detail.get("entity_kind") if isinstance(detail, dict) else None
    fields = {
        "body": "body_reference_sha256",
        "face": "face_reference_sha256",
    }
    return (
        isinstance(detail, dict)
        and set(detail) == NATIVE_SCOPE_WARNING_DETAIL_FIELDS
        and isinstance(detail.get("component"), str)
        and bool(detail["component"])
        and isinstance(kind, str)
        and kind in fields
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
        and all(type(index) is int and index >= 0 for index in detail["object_indices"])
        and detail["object_indices"] == sorted(set(detail["object_indices"]))
        and detail.get("field") == fields.get(kind)
        and isinstance(detail.get("source_scope_sha256"), str)
        and bool(_HEX64.fullmatch(detail["source_scope_sha256"]))
        and detail["owner_scope_key"]
        == native_scope_identity_sha256(
            {
                "source_scope_sha256": detail["source_scope_sha256"],
                "body_type": detail["body_type"],
                "body_index": detail["body_index"],
            }
        )
    )


def native_scope_warning_diagnostic_is_valid(item: object) -> bool:
    """Whether one quality diagnostic is a complete non-blocking scope warning."""

    return (
        isinstance(item, dict)
        and isinstance(item.get("code"), str)
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


def native_warning_projection(item: dict) -> dict:
    """Project one raw native warning without losing its full object location."""

    detail = item.get("detail") if isinstance(item, dict) else None
    if not isinstance(item, dict) or not isinstance(item.get("code"), str) or not isinstance(detail, dict):
        raise PipelineError("Native warning lacks its structured detail")
    return {
        "code": str(item["code"]),
        "blocking": item.get("blocking", False),
        "severity": item.get("severity", "warning"),
        "message": str(item.get("message") or f"Native tessellation recorded {item['code']}"),
        "object": str(item.get("object") or detail.get("component") or detail.get("document") or ""),
        "detail": detail,
    }


def native_scope_warning_diagnostics_are_complete(
    items: list[dict],
) -> bool:
    """Validate unique, fully located scope-local identity warnings."""

    seen: set[str] = set()
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
        if group in seen:
            return False
        seen.add(group)
    return True


def native_gate_warning_details_match(
    diagnostics: list[dict],
    raw_warnings: list[dict],
) -> bool:
    """Compare the complete native gate warning projection without geometry replay."""

    if not isinstance(diagnostics, list) or not isinstance(raw_warnings, list):
        return False
    if not all(
        isinstance(item, dict) and isinstance(item.get("code"), str) and item["code"] in NATIVE_GATE_WARNING_CODES
        and isinstance(item.get("detail"), dict)
        for item in raw_warnings
    ):
        return False
    projected = [native_warning_projection(item) for item in raw_warnings]
    for rows in (projected, diagnostics):
        if not all(
            isinstance(item, dict)
            and isinstance(item.get("code"), str)
            and item["code"] in NATIVE_GATE_WARNING_CODES
            and item.get("blocking") is False
            and item.get("severity") == "warning"
            and isinstance(item.get("message"), str)
            and bool(item["message"].strip())
            and isinstance(item.get("object"), str)
            and bool(item["object"])
            and isinstance(item.get("detail"), dict)
            for item in rows
        ):
            return False
        if not native_scope_warning_diagnostics_are_complete(
            [item for item in rows if item["code"] in NATIVE_SCOPE_IDENTITY_WARNING_CODES]
        ):
            return False

    def comparable(items):
        return sorted(
            json.dumps(
                {key: item[key] for key in ("code", "blocking", "severity", "message", "object", "detail")},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            for item in items
        )

    return comparable(projected) == comparable(diagnostics)


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
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
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
        and value["source_document_sha256"] == value["source_document_identity_sha256"]
        and isinstance(value["source_copy_sha256"], str)
        and bool(_HEX64.fullmatch(value["source_copy_sha256"]))
        and isinstance(copy_path, str)
        and copy_path.startswith("source/")
        and "\\" not in copy_path
        and all(part not in {"", ".", ".."} for part in copy_path.split("/"))
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
        and value["source_document_identity_sha256"] == hashlib.sha256(path.encode("utf-8")).hexdigest()
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
