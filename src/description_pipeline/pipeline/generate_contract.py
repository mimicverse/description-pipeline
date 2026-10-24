"""Shared immutable identities at the Capture-to-Generate boundary."""

CAPTURE_ARTIFACT_KIND = "stage_output:capture"
GENERATE_ARTIFACT_KIND = "stage_output:generate"
GENERATE_OUTPUT_CONTRACT = "solidworks-to-urdf.generated-artifact/v1"
GENERATE_RECEIPT = "reports/generate.json"

__all__ = [
    "CAPTURE_ARTIFACT_KIND",
    "GENERATE_ARTIFACT_KIND",
    "GENERATE_OUTPUT_CONTRACT",
    "GENERATE_RECEIPT",
]
