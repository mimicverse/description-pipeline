"""Per-execution run bookkeeping for the CLI.

A ``run_id`` names one invocation on one machine.  It is deliberately **not** part of the model:
the subject digest, the source lock, the bundle manifest and the quality report stay deterministic
and run-free.  The record lives in the ignored ``build/runs/`` directory beside the diagnostics and
is echoed in the CLI JSON output, so an operator can correlate a run with its diagnostics without
duplicating the pipeline identity (the ``pipeline_id``) or the capture's own ``job_id``.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .io import PipelineError, confined, read_data, write_json

RUNS = "build/runs"
SCHEMA = "description.run/v1"

#: Commands that produce or verify model artifacts keep a run record.  Read-only discovery
#: (``pipeline``, ``doctor``, ``diff``) and one-shot scaffolding do not.
RECORDED_COMMANDS = frozenset({"build", "check", "source", "tool", "model"})


def new_run_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return f"{moment:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}"


def _command(args) -> str:
    parts = [str(args.command)]
    operation = getattr(args, "operation", None)
    if operation:
        parts.append(str(operation))
    return " ".join(parts)


def _source_job_id(root: Path) -> str | None:
    """Reuse the capture's own job id when the definition carries one (the resume path)."""

    try:
        value = read_data(confined(root, "config/robot.yaml"))
    except (PipelineError, OSError):
        return None
    source = value.get("source") if isinstance(value, dict) else None
    job = source.get("job_id") if isinstance(source, dict) else None
    return str(job) if job else None


def _workspace_pipeline_id(root: Path) -> str | None:
    """Best-effort identity for diagnostics; the binding itself is verified by build/check."""

    try:
        locked = read_data(confined(root, "sources/source.lock.json"))
    except (PipelineError, OSError, KeyError, TypeError, ValueError):
        locked = None
    if isinstance(locked, dict):
        block = locked.get("pipeline")
        found = block.get("id") if isinstance(block, dict) else locked.get("pipeline_id")
        if isinstance(found, str) and found:
            return found
    try:
        config = read_data(confined(root, "config/robot.yaml"))
    except (PipelineError, OSError, KeyError, TypeError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    declared = config.get("pipeline_id")
    if isinstance(declared, str) and declared:
        return declared
    try:
        from . import pipeline

        robot = config.get("robot") if isinstance(config.get("robot"), dict) else None
        return pipeline.resolve_identity(
            None, source=config.get("source") if isinstance(config.get("source"), dict) else {}, robot=robot
        )["id"]
    except (PipelineError, OSError, KeyError, TypeError, ValueError):
        return None


def begin(args, argv: list[str]) -> dict | None:
    """Start a record when the command operates on an existing model workspace."""

    root = getattr(args, "root", None)
    if root is None:
        return None
    if getattr(args, "command", None) not in RECORDED_COMMANDS:
        return None
    root = Path(root).resolve()
    if not (root / "config/robot.yaml").is_file():
        return None
    started = datetime.now(UTC)
    return {
        "schema_version": SCHEMA,
        "run_id": new_run_id(started),
        "root": str(root),
        "command": _command(args),
        "argv": [str(item) for item in argv],
        "started_at": started.isoformat(),
        "tool_version": __version__,
        "source_job_id": _source_job_id(root),
        "outcome": "running",
    }


def note(run: dict | None, value: dict, *, outcome: str | None = None) -> None:
    """Record what the command reported; never raises."""

    if not run:
        return
    try:
        passed = value.get("passed", value.get("ok"))
        if passed is None:
            build = value.get("build")
            passed = build.get("passed") if isinstance(build, dict) else None
        if outcome is None:
            # A command whose result is not a boolean (for example ``source freeze`` returns the
            # lock) succeeded by reaching this point; only an explicit false is a failure.
            outcome = "completed" if passed is None else ("passed" if passed else "failed")
        run["outcome"] = outcome
        pipeline_id = value.get("pipeline_id")
        if pipeline_id is None:
            build = value.get("build")
            pipeline_id = build.get("pipeline_id") if isinstance(build, dict) else None
        if pipeline_id is None and run.get("root"):
            pipeline_id = _workspace_pipeline_id(Path(run["root"]))
        run["pipeline_id"] = pipeline_id
        if value.get("diagnostic_path"):
            run["diagnostic_path"] = str(value["diagnostic_path"])
        if value.get("error"):
            run["error"] = str(value.get("message") or value["error"])[:500]
    except (PipelineError, OSError, KeyError, TypeError, ValueError):
        # Bookkeeping must never mask the command's own error.
        run.setdefault("outcome", outcome or "error")


def finish(run: dict | None) -> None:
    """Write the record under the ignored ``build/runs`` directory; never fail the command."""

    if not run:
        return
    run["finished_at"] = datetime.now(UTC).isoformat()
    root = Path(run.pop("root"))
    run.setdefault("outcome", "error")
    try:
        destination = root / RUNS / f"{run['run_id']}.json"
        counter = 1
        while destination.exists():
            destination = root / RUNS / f"{run['run_id']}.{counter}.json"
            counter += 1
        run["record"] = destination.name
        write_json(destination, run)
    except (PipelineError, OSError, ValueError):
        print(f"warning: could not write the run record under {root / RUNS}", file=sys.stderr)
