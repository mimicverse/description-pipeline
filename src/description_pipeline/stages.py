"""One engineering-stage contract and its evidence-driven run view.

Airflow transports one serialized native job. These six stages describe the
engineering work inside that job; reading this view never executes that work.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from .delivery import PIPELINE_ID
from .io import PipelineError, digest, file_digest

CONTRACT = json.loads(Path(__file__).with_name("stage-contract.json").read_text(encoding="utf-8"))
CONTRACT_SHA256 = digest(CONTRACT)
CONTRACT_FILE_SHA256 = file_digest(Path(__file__).with_name("stage-contract.json"))
STAGE_IDS = tuple(stage["id"] for stage in CONTRACT["stages"])
VIEW_SCHEMA = "solidworks-to-urdf.stages/v1"


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
        raise PipelineError("A complete native workflow requires all six engineering stages")
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
                "state": stage["state"],
                "checks_passed": stage["checks_passed"],
                "checks_total": stage["checks_total"],
                **{
                    key: [{"id": item["id"], "state": item["state"]} for item in stage[key]]
                    for key in ("input_qc", "output_qc")
                },
                "evidence": stage["evidence"],
                "confirmations": [
                    {"id": item["id"], "state": item["state"]} for item in stage["confirmations"]
                ],
            }
            for stage in view["stages"]
        ],
        "report": "reports/stages.json",
    }
