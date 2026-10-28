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


def validate_certificate_diagnostics(certificate: Mapping[str, Any]) -> None:
    """Require nested observations to be present in the one complete diagnostic inventory."""

    records = verification_diagnostics(certificate)
    inventory = {canonical_json_value(item) for item in certificate["diagnostics"]}
    if len(inventory) != len(certificate["diagnostics"]):
        raise PipelineError("Verification certificate diagnostics contain duplicate records")
    if any(canonical_json_value(item) not in inventory for item in records):
        raise PipelineError("Verification certificate diagnostics omit a recorded check observation")


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
    "validate_certificate_diagnostics",
    "verification_diagnostics",
]
