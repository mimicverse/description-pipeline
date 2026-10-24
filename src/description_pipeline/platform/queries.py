"""Read-only projections over canonical pipeline facts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..io import PipelineError
from ..pipeline.contracts import STAGES


def read_run_projection(connection, run_id: str) -> dict[str, Any]:
    """Read one run without touching mutable legacy state or writing any fact."""

    if not isinstance(run_id, str) or not run_id:
        raise PipelineError("A run id is required")
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT request_id, parent_run_id, from_stage, request_digest, created_at
            FROM run_admission
            WHERE run_id = %s
            """,
            (run_id,),
        )
        admission = cursor.fetchone()
        if admission is None:
            raise PipelineError(f"Run {run_id!r} is not admitted")
        cursor.execute(
            """
            SELECT command_id, stage, kind, request_digest, predecessor_decision_id
            FROM command
            WHERE run_id = %s
            ORDER BY command_id
            """,
            (run_id,),
        )
        commands = {
            row[0]: {
                "command_id": row[0],
                "stage": row[1],
                "kind": row[2],
                "request_digest": row[3],
                "predecessor_decision_id": row[4],
            }
            for row in cursor.fetchall()
        }
        cursor.execute(
            """
            SELECT decision_id, stage, command_id, outcome, result_record_artifact_id,
                   result_digest, actual_executor_sha256, reason_code, decided_at
            FROM stage_decision
            WHERE run_id = %s
            """,
            (run_id,),
        )
        decisions = {
            row[1]: {
                "decision_id": row[0],
                "stage": row[1],
                "command_id": row[2],
                "outcome": row[3],
                "result_record_artifact_id": row[4],
                "result_digest": row[5],
                "actual_executor_sha256": row[6],
                "reason_code": row[7],
                "decided_at": row[8].isoformat() if row[8] is not None else None,
            }
            for row in cursor.fetchall()
        }
    finally:
        cursor.close()
    return {
        "run_id": run_id,
        "request_id": admission[0],
        "parent_run_id": admission[1],
        "from_stage": admission[2],
        "request_digest": admission[3],
        "created_at": admission[4].isoformat() if admission[4] is not None else None,
        "stages": [
            {
                "stage": stage,
                "command": next(
                    (
                        command
                        for command in commands.values()
                        if command["stage"] == stage and command["kind"] == "stage"
                    ),
                    None,
                ),
                "decision": decisions.get(stage),
            }
            for stage in STAGES
        ],
    }


def render_stage_summary(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stable display subset derived from a run projection."""

    if not isinstance(projection, Mapping) or not isinstance(projection.get("stages"), list):
        raise PipelineError("A run projection is required")
    return [
        {
            "stage": row["stage"],
            "state": row["decision"]["outcome"] if row.get("decision") else "waiting",
            "command_id": row["decision"]["command_id"]
            if row.get("decision")
            else (row.get("command") or {}).get("command_id"),
            "reason_code": row["decision"]["reason_code"] if row.get("decision") else None,
        }
        for row in projection["stages"]
    ]


__all__ = ["read_run_projection", "render_stage_summary"]
