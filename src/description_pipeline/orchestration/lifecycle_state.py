"""Operational lifecycle projection for the drain/stop gates (C consumer).

Domain split (joint with A): A owns the platform business state in the deployment PostgreSQL -
the same instance, a separate schema and separate privileges: requests, runs, stage receipts and
publish intents are the business authority.  This module is the consumer entry for the
operatorctl gates; service control never scans run artifacts and never substitutes scheduler
state for business receipts.

The projection is fail-closed: only recognized terminal outcomes settle.  Unresolved, unknown
and NULL states block drain completion, and every non-terminal object blocks stop.  The
scheduler observation below queries Airflow rows without any state pre-filter so NULL/unknown
states are never silently dropped.

Exit codes: 0 projection satisfies the requested requirement; 1 it does not; 2 usage.
"""

from __future__ import annotations

import argparse
import json
import sys

#: Recognized terminal outcomes across the platform vocabulary (runs, stages, publish intents).
TERMINAL_STATES = frozenset({"succeeded", "success", "published", "completed", "failed", "cancelled"})
#: Recognized non-terminal work still progressing (blocks stop; drain may complete).
IN_FLIGHT_STATES = frozenset(
    {
        "pending",
        "queued",
        "running",
        "scheduled",
        "up_for_retry",
        "up_for_reschedule",
        "restarting",
        "deferred",
        "publishing",
        "cancel_requested",
    }
)
UNRESOLVED_STATES = frozenset({"unresolved"})


def classify(rows) -> dict:
    """Split projection rows into in-flight, unresolved and unknown outcomes (fail-closed).

    Anything that is not a recognized terminal state lands in in-flight, unresolved or unknown;
    unrecognized vocabulary - including NULL/empty states - is unknown and blocks both gates.
    """

    in_flight, unresolved, unknown = [], [], []
    for row in rows:
        state = str(row.get("state") or "")
        if state in TERMINAL_STATES:
            continue
        if state in UNRESOLVED_STATES:
            unresolved.append(row)
        elif state in IN_FLIGHT_STATES:
            in_flight.append(row)
        else:
            unknown.append(row)
    return {
        "in_flight": in_flight,
        "unresolved": unresolved,
        "unknown": unknown,
        "settled": not (in_flight or unresolved or unknown),
    }


def observe(session, dag_run_model, task_instance_model) -> list[dict]:
    """Real ORM walk: rows are observed unfiltered so NULL/unknown states stay visible."""

    rows: list[dict] = []
    for run in session.query(dag_run_model).all():
        rows.append({"run_id": run.run_id, "state": run.state, "kind": "dag_run"})
    for instance in session.query(task_instance_model).all():
        rows.append(
            {
                "run_id": instance.run_id,
                "task_id": instance.task_id,
                "state": instance.state,
                "kind": "task_instance",
            }
        )
    return rows


def collect_airflow() -> list[dict]:
    """Read the scheduler half through the real ORM walk above (unfiltered)."""

    from airflow.models.dagrun import DagRun  # noqa: PLC0415
    from airflow.models.taskinstance import TaskInstance  # noqa: PLC0415
    from airflow.utils.session import create_session  # noqa: PLC0415

    with create_session() as session:
        return observe(session, DagRun, TaskInstance)


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
