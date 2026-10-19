"""Linked-run stage planning for native jobs (endpoint-side).

A linked run re-executes one chosen canonical stage and everything downstream as a NEW job;
the parent job stays immutable and upstream artifacts are reused read-only only when their
identity, digest and tool checks validate. This module holds the pure planning logic; the
filesystem probing lives with the endpoint.
"""

from __future__ import annotations

from ..stages import CONTRACT, STAGE_IDS, stage_view

STAGE_NAMES = {stage["id"]: stage.get("name") or stage["id"] for stage in CONTRACT["stages"]}
STAGE_INPUTS = {stage["id"]: list(stage.get("inputs") or []) for stage in CONTRACT["stages"]}
PREREQUISITES: dict[str, tuple[str, ...]] = {
    "freeze": (),
    # Upstream checkpoints whose recorded evidence must be complete before the
    # restarted stage can reuse them. Restarting discovery re-derives binding and
    # names, so a changed dependency snapshot never blocks discovery itself.
    "discover": ("freeze",),
    "capture": ("freeze", "discover"),
    "generate": ("freeze", "discover", "capture"),
    "verify": ("freeze", "discover", "capture", "generate"),
    "publish": ("freeze", "discover", "capture", "generate", "verify"),
}
#: Stages that reuse the parent discovery binding; only they depend on the
#: parent dependency snapshot (the publication target is the platform request's).
REUSES_DISCOVERY = frozenset({"capture", "generate", "verify", "publish"})
#: Probe entry that proves each checkpoint's retained bytes; the frozen upload
#: is already bound by the source probe and the qualified report is the verify output.
AVAILABILITY = {
    "freeze": None,
    "discover": "discover",
    "capture": "capture",
    "generate": "generate",
    "verify": "receipt",
}
REASON_MESSAGES = {
    "not_ready": "The original run has not finished, so it cannot be rerun from this step yet",
    "incomplete": "The original run produced no reusable checkpoint; restart from Freeze inputs",
    "source_changed": "The original upload cannot be reused; upload the folder again for a new run",
    "dependency_changed": (
        "The discovery dependency snapshot is inconsistent or unverifiable; restart from Discover structure"
    ),
    "tool_changed": (
        "The tool release changed or cannot be verified; evidence from different releases "
        "cannot be mixed; restart from Freeze inputs"
    ),
}


def stage_name(stage: object) -> str:
    return STAGE_NAMES.get(str(stage), str(stage))


def _stage_completed(job: dict, stage_id: str) -> bool:
    """Upstream reuse needs a completed stage whose recorded QC all passed."""
    view = stage_view(job)
    row = next((item for item in view.get("stages") or [] if item.get("id") == stage_id), None)
    if not isinstance(row, dict) or row.get("state") != "completed":
        return False
    for boundary in ("input_qc", "output_qc"):
        rows = row.get(boundary) or []
        if not rows or any(item.get("state") != "passed" for item in rows):
            return False
    return True


def _prerequisite_failure(requested: str, job: dict, probe: dict[str, str], tool: str) -> tuple[str, str | None] | None:
    """(reason, earliest_required) blocking ``requested``; None when it may start."""
    if probe.get("source") != "ok":
        return "source_changed", None
    if tool != "ok" and requested != "freeze":
        return "tool_changed", "freeze"
    for checkpoint in PREREQUISITES.get(requested, ()):
        # Validate strictly in prerequisite order: completion, retained bytes and
        # (only when the discovery binding is reused) its dependency snapshot.
        # Any refusal therefore names a checkpoint whose own restart is accepted.
        if not _stage_completed(job, checkpoint):
            return "prerequisite_invalid", checkpoint
        key = AVAILABILITY[checkpoint]
        if key is not None and probe.get(key) != "ok":
            return "prerequisite_invalid", checkpoint
        # Reusing the parent discovery binding: its dependency snapshot must
        # still be exactly verifiable.
        if checkpoint == "discover" and requested in REUSES_DISCOVERY and probe.get("dependency") != "ok":
            return "dependency_changed", "discover"
    return None


def start_plan(job: dict, requested: str, *, probe: dict[str, str], tool: str) -> dict:
    """Whether one linked run may start at ``requested``; exact earliest step on refusal."""
    if requested not in STAGE_IDS:
        return {
            "accepted": False,
            "reason": "unknown_stage",
            "earliest_required": None,
            "message": "Unknown engineering stage",
            "start_stage": None,
            "stage_name": None,
        }
    if job.get("status") not in {"failed", "passed", "native_complete"}:
        return {
            "accepted": False,
            "reason": "not_ready",
            "earliest_required": None,
            "message": REASON_MESSAGES["not_ready"],
            "start_stage": None,
            "stage_name": None,
        }
    failure = _prerequisite_failure(requested, job, probe, tool)
    if failure is not None:
        reason, earliest = failure
        text = REASON_MESSAGES.get(reason)
        if text is None:
            text = f"Upstream checkpoint {stage_name(earliest)} cannot be reused; restart from that step"
        return {
            "accepted": False,
            "reason": reason,
            "earliest_required": earliest,
            "message": text,
            "start_stage": None,
            "stage_name": None,
        }
    return {
        "accepted": True,
        "reason": "ok",
        "earliest_required": None,
        "message": "",
        "start_stage": requested,
        "stage_name": stage_name(requested),
    }


def stage_reruns(job: dict, *, probe: dict[str, str], tool: str) -> list[dict]:
    """Per-stage availability rows for the run detail (all six canonical stages)."""
    rows = []
    for stage_id in STAGE_IDS:
        plan = start_plan(job, stage_id, probe=probe, tool=tool)
        index = STAGE_IDS.index(stage_id)
        rows.append(
            {
                "stage": stage_id,
                "name": stage_name(stage_id),
                "eligible": plan["accepted"],
                "reason": plan["reason"],
                "message": plan["message"],
                "recomputes": list(STAGE_IDS[index:]),
                "retains": [
                    {
                        "label": stage_name(up),
                        "path": record.get("path"),
                        "availability": probe.get("source" if up == "freeze" else AVAILABILITY.get(up, up), "absent"),
                    }
                    for up in STAGE_IDS[:index]
                    for record in (STAGE_INPUTS.get(up) or [{}])
                ],
                "prerequisites": {
                    "inputs": probe.get("source", "absent"),
                    "tool": tool,
                    "dependency": probe.get("dependency", "unverifiable"),
                    "receipt": probe.get("receipt", "absent"),
                    "earliest_required": plan["earliest_required"],
                },
            }
        )
    return rows
