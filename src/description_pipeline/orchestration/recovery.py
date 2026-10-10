"""Linked-run stage planning for native jobs (endpoint-side).

A linked run re-executes one chosen canonical stage and everything downstream as a NEW job;
the parent job stays immutable and upstream artifacts are reused read-only only when their
identity, digest and tool checks validate. This module holds the pure planning logic; the
filesystem probing lives with the endpoint.
"""

from __future__ import annotations

from ..stages import CONTRACT, STAGE_IDS, stage_view

STAGE_NAMES_ZH = {stage["id"]: stage.get("name_zh") or stage["id"] for stage in CONTRACT["stages"]}
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
#: parent dependency snapshot and the unchanged publication target.
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
REASON_ZH = {
    "not_ready": "原运行尚未结束，暂不能从此步骤重新运行",
    "incomplete": "原运行未产生可复用的检查点，最早可从“冻结输入”重新开始",
    "source_changed": "原始上传不可复用，请重新上传后开始新运行",
    "dependency_changed": "解析依赖快照（命名注册表或记录来源）与原始运行不一致或无法验证，最早可从“解析结构”重新开始",
    "tool_changed": "工具版本已变化或无法验证；不能混用不同版本的证据，最早可从“冻结输入”重新开始",
    "target_changed": "发布目标（仓库或分支）与原始绑定不一致；为避免沿用旧绑定，最早可从“解析结构”重新开始",
}


def stage_name_zh(stage: object) -> str:
    return STAGE_NAMES_ZH.get(str(stage), str(stage))


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
        # (only when the discovery binding is reused) its dependency and target.
        # Any refusal therefore names a checkpoint whose own restart is accepted.
        if not _stage_completed(job, checkpoint):
            return "prerequisite_invalid", checkpoint
        key = AVAILABILITY[checkpoint]
        if key is not None and probe.get(key) != "ok":
            return "prerequisite_invalid", checkpoint
        if checkpoint == "discover" and requested in REUSES_DISCOVERY:
            # Reusing the parent discovery binding: its dependency snapshot and the
            # publication target must still be exactly verifiable.
            if probe.get("dependency") != "ok":
                return "dependency_changed", "discover"
            if probe.get("target") != "ok":
                return "target_changed", "discover"
    return None


def start_plan(job: dict, requested: str, *, probe: dict[str, str], tool: str) -> dict:
    """Whether one linked run may start at ``requested``; exact earliest step on refusal."""
    if requested not in STAGE_IDS:
        return {
            "accepted": False,
            "reason": "unknown_stage",
            "earliest_required": None,
            "reason_zh": "未知的工程阶段",
            "start_stage": None,
            "stage_name_zh": None,
        }
    if job.get("status") not in {"failed", "passed", "native_complete"}:
        return {
            "accepted": False,
            "reason": "not_ready",
            "earliest_required": None,
            "reason_zh": REASON_ZH["not_ready"],
            "start_stage": None,
            "stage_name_zh": None,
        }
    failure = _prerequisite_failure(requested, job, probe, tool)
    if failure is not None:
        reason, earliest = failure
        text = REASON_ZH.get(reason)
        if text is None:
            text = f"上游检查点“{stage_name_zh(earliest)}”不可复用，请从该步骤重新开始"
        return {
            "accepted": False,
            "reason": reason,
            "earliest_required": earliest,
            "reason_zh": text,
            "start_stage": None,
            "stage_name_zh": None,
        }
    return {
        "accepted": True,
        "reason": "ok",
        "earliest_required": None,
        "reason_zh": "",
        "start_stage": requested,
        "stage_name_zh": stage_name_zh(requested),
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
                "name_zh": stage_name_zh(stage_id),
                "eligible": plan["accepted"],
                "reason": plan["reason"],
                "reason_zh": plan["reason_zh"],
                "recomputes": list(STAGE_IDS[index:]),
                "retains": [
                    {
                        "label_zh": stage_name_zh(up),
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
                    "target": probe.get("target", "ok"),
                    "receipt": probe.get("receipt", "absent"),
                    "earliest_required": plan["earliest_required"],
                },
                "target_changed": probe.get("target") == "changed",
            }
        )
    return rows
