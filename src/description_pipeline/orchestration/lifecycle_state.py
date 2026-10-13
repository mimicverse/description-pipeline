"""Operational lifecycle projection for the drain/stop gates (single owner).

Service control consumes this projection; it never scans run artifacts.  The scheduler half
reads Airflow's PostgreSQL state (dag_run/task_instance) - the shared platform authority owned
with A.  Platform outcome rows (``unresolved``/publishing) attach to the same projection when
the platform schema lands; until then they arrive solely through this entry point and are never
re-derived from files by service control.

Exit codes: 0 projection satisfies the requested requirement; 1 it does not; 2 usage.
"""

from __future__ import annotations

import argparse
import json
import sys

#: Scheduler states that mean work is still in flight (Airflow task_instance vocabulary).
ACTIVE_STATES = frozenset(
    {"running", "queued", "scheduled", "up_for_retry", "up_for_reschedule", "restarting", "deferred"}
)
UNRESOLVED_STATES = frozenset({"unresolved"})
UNKNOWN_STATES = frozenset({"", "unknown"})


def classify(rows) -> dict:
    """Split projection rows into in-flight, unresolved and unknown outcomes."""

    in_flight = [row for row in rows if str(row.get("state") or "") in ACTIVE_STATES]
    unresolved = [row for row in rows if str(row.get("state") or "") in UNRESOLVED_STATES]
    unknown = [row for row in rows if str(row.get("state") or "") in UNKNOWN_STATES]
    return {
        "in_flight": in_flight,
        "unresolved": unresolved,
        "unknown": unknown,
        "settled": not (in_flight or unresolved or unknown),
    }


def collect_airflow() -> list[dict]:
    """Read the non-terminal scheduler state from Airflow's PostgreSQL."""

    from airflow.models.dagrun import DagRun  # noqa: PLC0415
    from airflow.models.taskinstance import TaskInstance  # noqa: PLC0415
    from airflow.utils.session import create_session  # noqa: PLC0415

    rows: list[dict] = []
    with create_session() as session:
        for run in session.query(DagRun).filter(DagRun.state.in_(tuple(sorted(ACTIVE_STATES)))).all():
            rows.append({"run_id": run.run_id, "state": str(run.state), "kind": "dag_run"})
        for instance in session.query(TaskInstance).filter(TaskInstance.state.in_(tuple(sorted(ACTIVE_STATES)))).all():
            rows.append(
                {
                    "run_id": instance.run_id,
                    "task_id": instance.task_id,
                    "state": str(instance.state),
                    "kind": "task_instance",
                }
            )
    return rows


def main(argv: list[str] | None = None, *, collect=collect_airflow) -> int:
    parser = argparse.ArgumentParser(prog="lifecycle_state.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="action", required=True)
    report = subparsers.add_parser("report")
    report.add_argument("--require-settled", action="store_true")
    report.add_argument("--require-no-unresolved", action="store_true")
    args = parser.parse_args(argv)

    data = classify(collect())
    print(json.dumps(data))
    if args.require_settled and not data["settled"]:
        return 1
    if args.require_no_unresolved and (data["unresolved"] or data["unknown"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
