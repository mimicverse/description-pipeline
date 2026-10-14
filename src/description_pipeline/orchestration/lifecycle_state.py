"""Operational lifecycle projection for the drain/stop gates (C consumer).

The platform views own the state vocabulary and the ``bucket`` classification; this consumer
reads them once, in one REPEATABLE READ snapshot, through the operator_ro DSN
(``PLATFORM_OPERATOR_DSN`` from the derived environment).  It never queries the scheduler and
holds no enumeration copies: rows are consumed by their view-owned ``bucket``, and every
unreadable authority (missing DSN, connection or query failure) refuses the gate - fail
closed, no file or scheduler fallback.

``request`` rows are accepted facts for reports only and never enter the activity gates.

Exit codes: 0 the projection satisfies the requested requirement; 1 it does not; 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def classify(rows) -> dict:
    """Split projection rows by their view-owned bucket (fail-closed, no local vocabulary).

    Request rows are reported separately - they are acceptance facts, not activity.  A row
    without a recognized bucket is ``unknown`` and blocks both gates.
    """

    in_flight, unresolved, unknown, requests = [], [], [], []
    for row in rows:
        if str(row.get("object_kind") or "") == "request":
            requests.append(row)
            continue
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
        "requests": requests,
        "settled": not (in_flight or unresolved or unknown),
    }


def collect_projection() -> dict:
    """Read the two platform views in one consistent snapshot as ``operator_ro``.

    The connection arrives through the platform.json-derived ``PLATFORM_OPERATOR_DSN``; the
    driver comes with the deployment interpreter (the gates run under the Airflow venv).
    Every failure raises so the caller refuses - nothing is cached, retried or substituted.
    """

    dsn = os.environ.get("PLATFORM_OPERATOR_DSN", "")
    if not dsn:
        raise RuntimeError("PLATFORM_OPERATOR_DSN is not set; run install to derive the platform read DSN")
    import psycopg2  # noqa: PLC0415  (provided by the deployment interpreter)

    connection = psycopg2.connect(dsn)
    try:
        connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT object_kind, object_id, run_id, state, bucket, updated_at"
                " FROM platform.lifecycle_projection"
            )
            columns = [description[0] for description in cursor.description]
            rows = [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]
            cursor.execute("SELECT state, reason, actor, changed_at, revision FROM platform.admission_projection")
            columns = [description[0] for description in cursor.description]
            current = cursor.fetchone()
            admission = dict(zip(columns, current, strict=True)) if current else None
    finally:
        connection.close()
    return {"rows": rows, "admission": admission}


def snapshot(collect=collect_projection) -> dict:
    """One consistent read of both views: the bucket classification plus the admission fact.

    ``drain_requested`` means the controlled close happened; ``drained`` means the queue is
    actually empty (settled) while the admission is closed - acknowledged and drained are
    distinct facts for the reports.
    """

    collected = collect()
    data = classify(collected["rows"])
    admission = collected.get("admission")
    data["admission"] = admission
    data["drain_requested"] = bool(admission and admission.get("state") == "closed")
    data["drained"] = bool(data["settled"] and data["drain_requested"])
    return data


def main(argv: list[str] | None = None, *, collect=collect_projection) -> int:
    parser = argparse.ArgumentParser(prog="lifecycle_state.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="action", required=True)
    report = subparsers.add_parser("report")
    report.add_argument("--require-settled", action="store_true", help="no in_flight/unresolved/unknown activity")
    report.add_argument("--require-no-unresolved", action="store_true", help="no unresolved/unknown closure debt")
    report.add_argument("--require-admission-closed", action="store_true", help="the platform admission is closed")
    args = parser.parse_args(argv)

    try:
        data = snapshot(collect)
    except Exception as error:  # noqa: BLE001 - any unreadable authority refuses the gate
        print(f"lifecycle_state: projection unreadable: {error}", file=sys.stderr)
        return 1
    print(json.dumps(data, default=str))
    if args.require_admission_closed and not data["drain_requested"]:
        print("lifecycle_state: the platform admission is not closed", file=sys.stderr)
        return 1
    if args.require_no_unresolved and (data["unresolved"] or data["unknown"]):
        return 1
    if args.require_settled and not data["settled"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
