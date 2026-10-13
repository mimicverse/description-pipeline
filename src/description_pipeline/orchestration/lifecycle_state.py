"""Operational lifecycle projection for the drain/stop gates (C consumer).

The business authority is A's platform schema: ``platform.lifecycle_projection`` owns the state
vocabulary AND the ``bucket`` classification, and ``platform.admission_projection`` exposes the
admission gate.  This consumer holds no enumeration copies: rows are consumed by their
view-owned ``bucket`` (``terminal``/``in_flight``/``unresolved``/``unknown``); a row without a
recognized bucket - including the pre-switch scheduler observation below - is ``unknown`` and
fails closed, so the gates refuse until the platform authority answers.

Exit codes: 0 projection satisfies the requested requirement; 1 it does not; 2 usage.
"""

from __future__ import annotations

import argparse
import json
import sys

def classify(rows) -> dict:
    """Split projection rows by their view-owned bucket (fail-closed, no local vocabulary).

    A row is consumed purely through ``bucket``; missing or unrecognized buckets are ``unknown``
    and block both gates.  The consumer never re-derives the state vocabulary.
    """

    in_flight, unresolved, unknown = [], [], []
    for row in rows:
        bucket = str(row.get("bucket") or "")
        if bucket == "terminal":
            continue
        if bucket == "in_flight":
            in_flight.append(row)
        elif bucket == "unresolved":
            unresolved.append(row)
        else:
            unknown.append(row)
    return {
        "in_flight": in_flight,
        "unresolved": unresolved,
        "unknown": unknown,
        "settled": not (in_flight or unresolved or unknown),
    }


def collect_projection() -> list[dict]:
    """The single seam the projection source plugs into; rows follow the frozen row contract
    (``object_kind``/``object_id``/``run_id``/``state``/``bucket``/``updated_at``)."""

    return collect_airflow()


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


def main(argv: list[str] | None = None, *, collect=collect_projection) -> int:
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
