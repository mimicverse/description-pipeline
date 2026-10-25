"""Pure validation for historical native stage evidence."""

from __future__ import annotations

from ..delivery import PIPELINE_ID
from ..io import PipelineError
from ..stages import CONTRACT, CONTRACT_SHA256, VIEW_SCHEMA


def validate_native_discovery_stage(
    native_stages: object,
    *,
    run_id: str,
    handoff_sha256: str,
) -> dict:
    """Require completed Discover and failed Capture facts for recovery."""

    if not isinstance(native_stages, dict):
        raise PipelineError("The native capture failure carries no stage view")
    if (
        native_stages.get("schema_version") != VIEW_SCHEMA
        or native_stages.get("pipeline_id") != PIPELINE_ID
        or native_stages.get("contract_sha256") != CONTRACT_SHA256
        or native_stages.get("run_id") != run_id
        or native_stages.get("handoff_sha256") != handoff_sha256
        or not _valid_native_execution_scope(native_stages.get("execution_scope"))
    ):
        raise PipelineError(
            "The native capture failure carries another stage contract or run identity"
        )
    rows = native_stages.get("stages")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PipelineError("The native capture failure carries no readable stage rows")
    expected_ids = [stage["id"] for stage in CONTRACT["stages"]]
    if [row.get("id") for row in rows] != expected_ids:
        raise PipelineError(
            "The native stage view does not cover the installed contract exactly once"
        )
    by_id = {row["id"]: row for row in rows}
    definition = next(
        stage for stage in CONTRACT["stages"] if stage["id"] == "discover"
    )
    discover = by_id["discover"]
    if discover.get("state") != "completed" or discover.get("in_scope") is not True:
        raise PipelineError("The native stage view does not prove completed discovery")
    for boundary in ("input_qc", "output_qc"):
        expected = [item["id"] for item in definition[boundary]]
        rows_for_boundary = discover.get(boundary)
        if (
            not isinstance(rows_for_boundary, list)
            or [
                row.get("id")
                for row in rows_for_boundary
                if isinstance(row, dict)
            ]
            != expected
            or any(
                not isinstance(row, dict)
                or row.get("state") != "passed"
                or (
                    isinstance(row.get("details"), dict)
                    and row["details"].get("passed") is False
                )
                for row in rows_for_boundary
            )
        ):
            raise PipelineError(
                f"The native discover {boundary} is not completely passed"
            )
    capture = by_id["capture"]
    if capture.get("state") != "failed" or capture.get("in_scope") is not True:
        raise PipelineError("The native stage view does not prove failed Capture")
    return native_stages


def _valid_native_execution_scope(scope: object) -> bool:
    """The native half is exactly discover/capture, optionally led by freeze."""

    return scope in (["discover", "capture"], ["freeze", "discover", "capture"])


__all__ = ["validate_native_discovery_stage"]
