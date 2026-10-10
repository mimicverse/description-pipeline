"""One engineering-stage contract and its evidence-driven run view.

Airflow coordinates native acquisition and Linux verification. These six stages
describe the engineering work; reading this view never executes that work.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from pathlib import Path

from .delivery import PIPELINE_ID
from .io import PipelineError, digest, file_digest

CONTRACT = json.loads(Path(__file__).with_name("stage-contract.json").read_text(encoding="utf-8"))
CONTRACT_SHA256 = digest(CONTRACT)
CONTRACT_FILE_SHA256 = file_digest(Path(__file__).with_name("stage-contract.json"))
STAGE_IDS = tuple(stage["id"] for stage in CONTRACT["stages"])
#: One execution host per stage: Windows native CAD evidence versus the portable
#: Linux generation, verification and publication half.
HOST_IDS = ("native", "portable")
HOST_BY_STAGE = {stage["id"]: stage.get("host") for stage in CONTRACT["stages"]}
if set(HOST_BY_STAGE.values()) - set(HOST_IDS) or any(host is None for host in HOST_BY_STAGE.values()):
    raise PipelineError("Every engineering stage must declare exactly one execution host")
VIEW_SCHEMA = "solidworks-to-urdf.stages/v1"

# ---------------------------------------------------------------------------
# Live activity — what a running stage is doing right now.  This is deliberately
# separate from the check/stage evidence above: activity is observation, never
# completed work, and it never feeds quality, receipts or terminal state.
# ---------------------------------------------------------------------------

#: Bounded history of (phase, action) transitions kept alongside the latest activity.
ACTIVITY_HISTORY_LIMIT = 40
#: Bounded number of recent transitions exposed in the run view.
ACTIVITY_RECENT_LIMIT = 5
_ACTIVITY_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def _activity_token(value) -> str | None:
    return value if isinstance(value, str) and _ACTIVITY_TOKEN.fullmatch(value) else None


def _activity_text(value, limit: int) -> str | None:
    """Control-free text up to ``limit`` characters, or ``None`` when unusable."""

    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in unicodedata.normalize("NFC", value) if unicodedata.category(ch)[0] != "C")
    cleaned = cleaned.strip()
    if not cleaned or len(cleaned) > limit:
        return None
    return cleaned


def _activity_object(value) -> str | None:
    """A safe relative posix name; absolute paths and traversals are refused."""

    cleaned = _activity_text(value, 160)
    if cleaned is None:
        return None
    name = cleaned.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def normalize_activity(record) -> dict | None:
    """Validate one emitted activity record; unusable records are ignored, never defaulted.

    The emitter reports only what it knows (``phase``/``action`` and optional
    ``params``/``current_object``/``completed``+``total``/``unit``); everything
    else — timestamps, sequence and history — is added by the collector.  A
    malformed record is dropped: activity must never fabricate or fail a run.
    """

    if not isinstance(record, dict):
        return None
    phase = _activity_token(record.get("phase"))
    action = _activity_token(record.get("action"))
    if phase is None or phase not in STAGE_IDS or action is None:
        return None
    normalized: dict = {"phase": phase, "action": action}
    params = record.get("params")
    if params is not None:
        if not isinstance(params, dict) or not params or len(params) > 6:
            return None
        clean_params = {}
        for key, value in params.items():
            if _activity_token(key) is None:
                return None
            if isinstance(value, bool) or type(value) is int:
                clean_params[key] = value
            elif isinstance(value, str):
                text = _activity_text(value, 120)
                if text is None:
                    return None
                clean_params[key] = text
            else:
                return None
        normalized["params"] = clean_params
    if record.get("current_object") is not None:
        safe_object = _activity_object(record.get("current_object"))
        if safe_object is None:
            return None
        normalized["current_object"] = safe_object
    completed, total = record.get("completed"), record.get("total")
    if completed is not None or total is not None:
        # Counts exist together or not at all; a growing total is honest scope
        # discovery, a shrinking completed count never is.
        if type(completed) is not int or type(total) is not int or completed < 0 or total < completed:
            return None
        normalized["completed"], normalized["total"] = completed, total
    if record.get("unit") is not None:
        unit = _activity_token(record.get("unit"))
        if unit is None:
            return None
        normalized["unit"] = unit
    return normalized


def merge_activity(previous, record, *, at: str) -> tuple[dict, dict | None]:
    """Fold one normalized record into the latest activity.

    A repeated (phase, action) keeps the run's identity (``started_at``/``seq``)
    while the record REPLACES the observed fields: omitted optional fields are
    cleared rather than carried over, so a stale object or count can never leak
    into a later batch and a legitimate per-document reset is reported as the
    emitter sent it.  A new pair starts a fresh run and one bounded history
    entry.
    """

    previous = previous if isinstance(previous, dict) else {}
    same = previous.get("phase") == record.get("phase") and previous.get("action") == record.get("action")
    merged = dict(record)
    merged["at"] = at
    merged["started_at"] = previous.get("started_at") if same else at
    merged["updated_at"] = at
    merged["seq"] = int(previous.get("seq") or 0) + 1 if same else 1
    history = None
    if not same:
        history = {"at": at, "code": f"{merged['phase']}.{merged['action']}", "object": merged.get("current_object")}
    return merged, history


def _stage_started_at(job: dict, phase: str | None) -> str | None:
    """The true start of the current stage execution, from stage-state events only.

    Check records also carry ``state: running`` but describe a check, not the
    stage: they are excluded so the rendered elapsed time never resets per
    check.  The first stage-state ``running`` event of the execution is the
    start; a completed/failed transition leaves no current execution start.
    """

    if not phase:
        return None
    started = None
    for event in job.get("events") or []:
        if not isinstance(event, dict) or event.get("stage") != phase or "check" in event:
            continue
        at = event.get("at")
        if not isinstance(at, str):
            continue
        if event.get("state") == "running":
            if started is None:
                started = at
        else:
            started = None
    return started


def activity_view(job) -> dict:
    """The run-view activity object; always present, explicit about absence.

    ``available`` is true only when recorded telemetry exists: a live record
    (``busy``) or a finished run with its final observation/history
    (``finished``).  Stages without an emitter — a queued run, a running run
    whose current stage reports nothing, legacy attempts — stay explicitly
    unavailable (``available: false``) instead of implying an action.
    ``stage_started_at`` is the true stage start from recorded stage events;
    ``action_started_at`` is when the current action began.  Timestamps come
    from the recorder only — a successful poll never refreshes freshness.
    """

    view = {
        "available": False,
        "state": "none",
        "stage": None,
        "stage_started_at": None,
        "action_started_at": None,
        "updated_at": None,
        "action": None,
        "object": None,
        "counts": None,
        "recent": [],
    }
    if not isinstance(job, dict):
        return view
    status = str(job.get("status") or "")
    recent = []
    for entry in job.get("activity_history") or []:
        if not isinstance(entry, dict):
            continue
        code, at = entry.get("code"), entry.get("at")
        if not isinstance(code, str) or not isinstance(at, str):
            continue
        recent.append(
            {"at": at, "code": code, "object": entry.get("object") if isinstance(entry.get("object"), str) else None}
        )
    view["recent"] = recent[-ACTIVITY_RECENT_LIMIT:][::-1]

    def describe(source: dict) -> None:
        phase, action = source.get("phase"), source.get("action")
        view["stage"] = phase if isinstance(phase, str) else None
        view["stage_started_at"] = _stage_started_at(job, view["stage"])
        view["action_started_at"] = source.get("started_at")
        view["updated_at"] = source.get("updated_at")
        if isinstance(phase, str) and isinstance(action, str):
            params = source.get("params") if isinstance(source.get("params"), dict) else {}
            view["action"] = {"code": f"{phase}.{action}", "params": params}
        view["object"] = source.get("current_object")
        if type(source.get("completed")) is int and type(source.get("total")) is int:
            counts = {"done": source["completed"], "total": source["total"]}
            if isinstance(source.get("unit"), str):
                counts["unit"] = source["unit"]
            view["counts"] = counts

    final = job.get("activity_final") if isinstance(job.get("activity_final"), dict) else None
    if status in {"passed", "failed", "native_complete"}:
        view["state"] = "finished"
        if final is not None or view["recent"]:
            view["available"] = True
            describe(final or {})
        return view
    if status == "queued":
        view["state"] = "queued"
        return view
    if status != "running":
        return view
    activity = job.get("activity") if isinstance(job.get("activity"), dict) else None
    if activity is None:
        view["state"] = "waiting"
        return view
    view["state"] = "busy"
    view["available"] = True
    describe(activity)
    return view


def stage_host(stage_id: str) -> str:
    """The declared execution host of one engineering stage."""

    try:
        return HOST_BY_STAGE[stage_id]
    except KeyError as error:
        raise PipelineError(f"Unknown engineering stage: {stage_id!r}") from error


def _boundary(stage, boundary, identifier):
    definition = next((item for item in CONTRACT["stages"] if item["id"] == stage), None)
    if (
        boundary not in {"input", "output"}
        or definition is None
        or identifier not in {item["id"] for item in definition[f"{boundary}_qc"]}
    ):
        raise PipelineError("Boundary check is absent from the engineering-stage contract")


def record_check(emit, stage, boundary, identifier, state, details):
    """Record one domain-owned boundary result through the shared observation contract."""
    _boundary(stage, boundary, identifier)
    if state not in {"passed", "failed"}:
        raise PipelineError("A recorded boundary check must pass or fail")
    if emit is not None:
        emit(
            {
                "stage": stage,
                "state": "failed" if state == "failed" else "running",
                "check": {"id": identifier, "boundary": boundary, "state": state, "details": details or {}},
            }
        )


def checked(emit, stage, boundary, identifier, action, *, describe=None):
    """Execute a real boundary check once and retain its result before returning/raising."""
    _boundary(stage, boundary, identifier)
    try:
        value = action()
        details = describe(value) if describe else value
        if isinstance(details, dict) and details.get("passed") is False:
            error = PipelineError("Required check failed: " + identifier)
            error.details = details
            raise error
    except Exception as error:
        details = {"error": f"{type(error).__name__}: {error}"}
        detail = getattr(error, "detail", None) or getattr(error, "details", None)
        if detail is not None:
            details["diagnostic"] = detail
        record_check(emit, stage, boundary, identifier, "failed", details)
        raise
    record_check(emit, stage, boundary, identifier, "passed", details)
    return value


def _complete_checks(details):
    """Nested gates must be explicit, unique and individually passed."""
    required, rows = details.get("required_checks"), details.get("checks")
    if (
        not isinstance(required, list)
        or not required
        or not all(isinstance(name, str) for name in required)
        or not isinstance(rows, list)
        or not all(isinstance(row, dict) for row in rows)
    ):
        return False
    names = [row.get("id") for row in rows]
    return (
        all(isinstance(name, str) for name in names)
        and len(set(names)) == len(names)
        and len(set(required)) == len(required)
        and set(required) <= set(names)
        and all(row.get("state") == "passed" and row.get("passed") is True for row in rows)
    )


def stage_view(job=None):
    """Render only recorded observations; completed events cannot manufacture passing QC."""
    job = job or {}
    result = job.get("result") or job
    events = job.get("events") or []
    observations = [event["check"].get("details") or {} for event in events if isinstance(event.get("check"), dict)]
    subject = result.get("subject_sha256") or next(
        (value.get("subject_sha256") for value in reversed(observations) if value.get("subject_sha256")), None
    )
    handoff = next(
        (value.get("handoff_sha256") for value in reversed(observations) if value.get("handoff_sha256")), None
    )
    files = {}
    for record in observations:
        files.update(record.get("files") or {})
    files.update(result.get("artifacts") or {})
    view = {
        "schema_version": VIEW_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "contract_sha256": CONTRACT_SHA256,
        "contract_file_sha256": CONTRACT_FILE_SHA256,
        "run_id": job.get("run_id"),
        "execution_scope": result.get("execution_scope", list(STAGE_IDS)),
        "subject_sha256": subject,
        "handoff_sha256": (job.get("request") or {}).get("handoff_sha256") or result.get("handoff_sha256") or handoff,
        "stages": [],
        "release_approval": "required",
        "unsupported": [{**item, "state": "unsupported"} for item in CONTRACT["unsupported"]],
    }
    blocked = False
    for definition in CONTRACT["stages"]:
        stage = copy.deepcopy(definition)
        observed = [event for event in events if event.get("stage") == stage["id"]]
        latest = observed[-1] if observed else {}
        reuse = latest.get("reuse")
        if (
            isinstance(reuse, dict)
            and reuse.get("reused") is True
            and isinstance(reuse.get("parent_run"), str)
            and reuse["parent_run"]
            and all(event.get("reuse") == reuse for event in observed)
        ):
            stage["reuse"] = {"parent_run": reuse["parent_run"], "reused": True}
            if isinstance(reuse.get("source_run"), str) and reuse["source_run"]:
                stage["reuse"]["source_run"] = reuse["source_run"]
        for boundary in ("input", "output"):
            for item in stage[f"{boundary}_qc"]:
                matches = [
                    event["check"]
                    for event in observed
                    if isinstance(event.get("check"), dict)
                    and event["check"].get("boundary") == boundary
                    and event["check"].get("id") == item["id"]
                ]
                item.update(state="not_run", details={"reason": "No recorded check result"})
                if matches:
                    record = matches[-1]
                    item["state"] = record["state"] if record.get("state") in {"passed", "failed"} else "not_run"
                    item["details"] = record.get("details") or {}
                    if item["state"] == "passed" and item["details"].get("passed") is False:
                        item["state"] = "failed"
                    bound = item["details"].get("subject_sha256")
                    if subject is not None and bound is not None and bound != subject:
                        item.update(state="failed", details={"error": "Check evidence binds a different file subject"})
                    if (
                        "required_checks" in item["details"]
                        and item["state"] == "passed"
                        and not _complete_checks(item["details"])
                    ):
                        item["state"] = "failed"
        checks = stage["input_qc"] + stage["output_qc"]
        failed = any(item["state"] == "failed" for item in checks)
        complete = all(item["state"] == "passed" for item in checks)
        state = latest.get("state", "not_run")
        if job.get("status") == "failed" and observed and observed[-1] is events[-1]:
            state = "failed"
        if failed or state == "failed":
            state = "failed"
        elif blocked:
            state = "blocked"
        elif state == "completed" and not complete:
            state = "failed"
        elif state not in {"running", "completed"}:
            state = "not_run"
        stage.update(
            state=state,
            in_scope=stage["id"] in view["execution_scope"],
            at=latest.get("at"),
            checks_passed=sum(item["state"] == "passed" for item in checks),
            checks_total=len(checks),
            findings=[event["check"] for event in observed if (event.get("check") or {}).get("state") == "failed"],
        )
        for item in stage["inputs"] + stage["outputs"]:
            paths = item["path"].split(" + ")
            matches = {
                name: checksum
                for name, checksum in files.items()
                if any(name == path or (path.endswith("/") and name.startswith(path)) for path in paths)
            }
            item.update(files=matches, availability="recorded" if matches else "not_produced")
        if state == "failed":
            stage["error"] = job.get("error") or result.get("error") or "Required stage checks failed or were not run"
            stage["diagnostic"] = job.get("detail") or result.get("detail") or latest.get("detail")
            stage["error_code"] = job.get("error_code") or result.get("error_code")
            blocked = True
        stage["confirmations"] = [
            {**item, "state": "not_ready", "subject_sha256": subject, "approval_tracking": "external"}
            for item in CONTRACT["confirmations"]
            if item["review_stage"] == stage["id"]
        ]
        stage["unsupported"] = [{**item} for item in view["unsupported"] if item["stage"] == stage["id"]]
        view["stages"].append(stage)
    # Engineering review concerns facts outside the automatic proof. Do not
    # manufacture outstanding approvals merely because this service does not
    # read the review PR or the organization's controlled approval records.
    if any(stage["id"] == "verify" and stage["state"] == "completed" for stage in view["stages"]):
        for stage in view["stages"]:
            for item in stage["confirmations"]:
                item["state"] = "external_review"
    return view


def require_complete(view):
    if view.get("contract_sha256") != CONTRACT_SHA256 or [s["id"] for s in view.get("stages", [])] != list(STAGE_IDS):
        raise PipelineError("Stage result does not match the installed engineering contract")
    if view.get("execution_scope") != list(STAGE_IDS):
        raise PipelineError("A complete workflow requires all six engineering stages")
    for stage, definition in zip(view["stages"], CONTRACT["stages"], strict=True):
        for boundary in ("input_qc", "output_qc"):
            rows = stage.get(boundary) or []
            if (
                [row.get("id") for row in rows] != [row["id"] for row in definition[boundary]]
                or any(row.get("state") != "passed" for row in rows)
                or stage.get("state") != "completed"
            ):
                raise PipelineError("Engineering stages are incomplete; missing checks cannot qualify a run")


def contract_markdown():
    """Shared table for Airflow's DAG documentation and the architecture document."""
    rows = [
        "| # | Stage | Input | Input QC | Output | Output QC |",
        "|---|---|---|---|---|---|",
    ]
    for number, stage in enumerate(CONTRACT["stages"], 1):
        cells = [str(number), f"`{stage['id']}` — {stage['name']}"]
        for field in ("inputs", "input_qc", "outputs", "output_qc"):
            cells.append("; ".join(item["label"] for item in stage[field]))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def stage_log(view):
    """Detailed JSON lines remain readable in Airflow even when a native job fails."""
    for stage in view["stages"]:
        yield json.dumps(stage, ensure_ascii=False, sort_keys=True)


def compact_view(view):
    """Bounded terminal XCom: contract and file hashes point to the detailed run receipt."""
    return {
        **{
            key: view[key]
            for key in (
                "schema_version",
                "pipeline_id",
                "contract_sha256",
                "contract_file_sha256",
                "run_id",
                "execution_scope",
                "subject_sha256",
                "handoff_sha256",
                "release_approval",
            )
        },
        "stages": [
            {
                "id": stage["id"],
                "host": stage["host"],
                "state": stage["state"],
                "checks_passed": stage["checks_passed"],
                "checks_total": stage["checks_total"],
                **{
                    key: [{"id": item["id"], "state": item["state"]} for item in stage[key]]
                    for key in ("input_qc", "output_qc")
                },
                "evidence": stage["evidence"],
                **({"reuse": stage["reuse"]} if stage.get("reuse") else {}),
                "confirmations": [{"id": item["id"], "state": item["state"]} for item in stage["confirmations"]],
            }
            for stage in view["stages"]
        ],
        "report": "reports/stages.json",
    }
