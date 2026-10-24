"""Shared Verify contract identities for admission and execution."""

from __future__ import annotations

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

__all__ = [
    "CAPTURE_ARTIFACT_KIND",
    "CERTIFICATE_FILENAME",
    "CERTIFICATE_SCHEMA",
    "EXECUTION_RECEIPT_FILENAME",
    "GENERATE_ARTIFACT_KIND",
    "GENERATE_RECEIPT",
    "OUTPUT_CONTRACT",
    "RECEIPT_SCHEMA",
    "REQUEST_SCHEMA",
    "VERIFY_ARTIFACT_KIND",
]
