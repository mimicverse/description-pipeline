"""Shared Verify contract identities for admission and execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..io import PipelineError, canonical_json_value
from ..schemas import (
    VERIFICATION_CERTIFICATE_SCHEMA as CERTIFICATE_SCHEMA,
    VERIFICATION_EXECUTION_SCHEMA as RECEIPT_SCHEMA,
    VERIFY_REQUEST_SCHEMA as REQUEST_SCHEMA,
)
from .generate_contract import (
    CAPTURE_ARTIFACT_KIND,
    GENERATE_ARTIFACT_KIND,
    GENERATE_RECEIPT,
)

OUTPUT_CONTRACT = "solidworks-to-urdf.verification-artifact/v1"
VERIFY_ARTIFACT_KIND = "stage_output:verify"
CERTIFICATE_FILENAME = "verification-certificate.json"
EXECUTION_RECEIPT_FILENAME = "verification-execution.json"

CERTIFICATE_FIELDS = frozenset(
    {
        "actual_verifier",
        "actual_verifier_sha256",
        "capture_artifact",
        "checks",
        "closure_sha256",
        "config_snapshot_sha256",
        "contract_sha256",
        "decision",
        "diagnostics",
        "evaluation_subject_sha256",
        "generate_artifact",
        "generate_receipt_sha256",
        "passed",
        "pipeline_id",
        "policy_sha256",
        "purpose",
        "qualifications",
        "request",
        "request_sha256",
        "required_checks",
        "schema_version",
        "subject_sha256",
        "subject_status",
        "verification_key",
    }
)


def verification_diagnostics(report: Mapping[str, Any]) -> list[dict]:
    """Collect the complete diagnostic inventory without changing recorded semantics."""

    groups = [report.get("diagnostics")]
    for check in report.get("checks") or []:
        details = check.get("details") if isinstance(check, Mapping) else None
        if isinstance(details, Mapping):
            groups.extend(details[key] for key in ("warnings", "diagnostics") if key in details)
    records = {}
    for group in groups:
        if not isinstance(group, list):
            raise PipelineError("Verification certificate diagnostics must be a list")
        for item in group:
            if not isinstance(item, dict):
                raise PipelineError("Verification certificate diagnostic must be an object")
            code = item.get("code")
            if not isinstance(code, str) or not code.strip():
                raise PipelineError("Verification certificate diagnostic has no code")
            if "message" in item and not isinstance(item["message"], str):
                raise PipelineError("Verification certificate diagnostic message must be text")
            if type(item.get("blocking")) is not bool:
                raise PipelineError("Verification certificate diagnostic has no blocking state")
            if (report.get("decision") == "PASS" or report.get("passed") is True) and item["blocking"]:
                raise PipelineError("Verification certificate PASS contains a blocking diagnostic")
            records.setdefault(canonical_json_value(item), dict(item))
    return list(records.values())


_DIAGNOSTIC_INDICES = {"warnings": "warning_indices", "diagnostics": "diagnostic_indices"}


def index_check_diagnostics(checks: list[dict], diagnostics: list[dict]) -> list[dict]:
    """Store each check observation as an index into the one diagnostic inventory."""

    positions = {canonical_json_value(item): index for index, item in enumerate(diagnostics)}
    indexed = []
    for check in checks:
        details = check.get("details")
        if not isinstance(details, dict):
            raise PipelineError("Verification check details must be an object")
        details = dict(details)
        for field, index_field in _DIAGNOSTIC_INDICES.items():
            if index_field in details:
                raise PipelineError("Verifier report already contains diagnostic indices")
            if field not in details:
                continue
            records = details.pop(field)
            if not isinstance(records, list):
                raise PipelineError("Verification check diagnostics must be a list")
            try:
                details[index_field] = list(dict.fromkeys(positions[canonical_json_value(item)] for item in records))
            except KeyError as error:
                raise PipelineError("Verification check observation is absent from the diagnostic inventory") from error
        indexed.append({**check, "details": details})
    return indexed


def certificate_check_details(certificate: Mapping[str, Any], check: Mapping[str, Any]) -> dict:
    """Resolve strictly typed check references without accepting embedded legacy records."""

    details = check.get("details")
    inventory = certificate.get("diagnostics")
    if not isinstance(details, dict) or not isinstance(inventory, list):
        raise PipelineError("Verification check details or diagnostic inventory is malformed")
    resolved = dict(details)
    for field, index_field in _DIAGNOSTIC_INDICES.items():
        if field in details:
            raise PipelineError("Verification certificate embeds diagnostic records in check details")
        if index_field not in details:
            continue
        indices = resolved.pop(index_field)
        if (
            not isinstance(indices, list)
            or any(type(index) is not int or index < 0 or index >= len(inventory) for index in indices)
            or len(indices) != len(set(indices))
        ):
            raise PipelineError("Verification check diagnostic indices are invalid")
        resolved[field] = [inventory[index] for index in indices]
    return resolved


def validate_certificate_diagnostics(certificate: Mapping[str, Any]) -> None:
    """Validate the single inventory, check references and native scope completeness."""

    from ..geometry.native_scope import (
        NATIVE_GATE_WARNING_CODES, NATIVE_SCOPE_IDENTITY_WARNING_CODES,
        native_gate_warning_details_match,
    )

    records = verification_diagnostics({
        "diagnostics": certificate.get("diagnostics"), "checks": [],
        "passed": certificate.get("passed"), "decision": certificate.get("decision"),
    })
    if len(records) != len(certificate["diagnostics"]):
        raise PipelineError("Verification certificate diagnostics contain duplicate records")
    checks = certificate.get("checks", [])
    if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
        raise PipelineError("Verification certificate checks must be a list of objects")
    native = [item for item in records if item["code"] in NATIVE_GATE_WARNING_CODES]
    native_gate = None
    for check in checks:
        details = certificate_check_details(certificate, check)
        if check.get("id") == "geometry.native_tessellation":
            if native_gate is not None:
                raise PipelineError("Verification certificate repeats the native tessellation gate")
            native_gate = (check, details)
    if native_gate is None:
        if native:
            raise PipelineError("Native gate diagnostics have no matching gate warnings")
        return
    check, details = native_gate
    warnings = details.get("warnings")
    passed = check.get("state") == "passed" or check.get("passed") is True
    if passed and (
        not isinstance(warnings, list)
        or not any(item.get("code") in NATIVE_SCOPE_IDENTITY_WARNING_CODES for item in warnings)
    ):
        raise PipelineError("Passed native tessellation gate carries no scope identity warnings")
    if (native or warnings) and (
        not isinstance(warnings, list) or not native_gate_warning_details_match(native, warnings)
    ):
        raise PipelineError("Native gate warnings differ from quality diagnostics")


__all__ = [
    "CAPTURE_ARTIFACT_KIND",
    "CERTIFICATE_FIELDS",
    "CERTIFICATE_FILENAME",
    "CERTIFICATE_SCHEMA",
    "EXECUTION_RECEIPT_FILENAME",
    "GENERATE_ARTIFACT_KIND",
    "GENERATE_RECEIPT",
    "OUTPUT_CONTRACT",
    "RECEIPT_SCHEMA",
    "REQUEST_SCHEMA",
    "VERIFY_ARTIFACT_KIND",
    "certificate_check_details",
    "index_check_diagnostics",
    "validate_certificate_diagnostics",
    "verification_diagnostics",
]
