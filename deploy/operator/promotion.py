#!/usr/bin/env python3
"""Promotion receipts for the unified stage/promote/rollback protocol.

The install record is written once, at the pointer switch.  Every step of one switch attempt
(stage included) is registered here as it happens - the plan, the actual status and the
evidence - and a failed attempt leaves this receipt as the failure record that rollback reads.
Everything lives under ``<state root>/promotions/<install_id>/``; nothing here touches the
active pointer or the journal of an existing record.

Exit codes: 0 success; 1 error (message on stderr); 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

RECEIPT_SCHEMA = "solidworks-to-urdf.promotion/v1"
VERBS = ("promote", "rollback")
STATUSES = ("completed", "failed", "unknown")


def die(message: str) -> None:
    raise SystemExit(f"promotion: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        die(message)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def receipt_path(state_root: Path, install_id: str) -> Path:
    return Path(state_root) / "promotions" / install_id / "receipt.json"


def load_receipt(path: Path) -> dict:
    require(path.is_file(), f"promotion receipt not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        die(f"cannot parse promotion receipt {path}: {error}")
    require(document.get("schema_version") == RECEIPT_SCHEMA, f"unexpected receipt schema in {path}")
    require(isinstance(document.get("steps"), list), f"receipt steps are malformed in {path}")
    return document


def save_receipt(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="promotion.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="action", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--state-root", type=Path, required=True)
    init.add_argument("--install-id", required=True)
    init.add_argument("--from", dest="source", type=Path, required=True)
    init.add_argument("--to", dest="target", type=Path, required=True)
    init.add_argument("--plan", required=True, help="comma-separated planned step names")
    step = subparsers.add_parser("step")
    step.add_argument("--state-root", type=Path, required=True)
    step.add_argument("--install-id", required=True)
    step.add_argument("--verb", choices=sorted(VERBS), required=True)
    step.add_argument("--step", required=True)
    step.add_argument("--status", choices=sorted(STATUSES), required=True)
    step.add_argument("--at", default=None)
    step.add_argument("--evidence", action="append", required=True, help="repeatable evidence reference")
    journal = subparsers.add_parser("journal")
    journal.add_argument("--state-root", type=Path, required=True)
    journal.add_argument("--install-id", required=True)
    journal.add_argument("--verb", choices=sorted(VERBS), required=True)
    show = subparsers.add_parser("show")
    show.add_argument("--state-root", type=Path, required=True)
    show.add_argument("--install-id", required=True)
    args = parser.parse_args(argv)

    path = receipt_path(args.state_root, args.install_id)
    if args.action == "init":
        require(not path.exists(), f"preserve the existing promotion receipt: {path}")
        plan = [name for name in args.plan.split(",") if name]
        require(plan, "the promotion plan is empty")
        save_receipt(
            path,
            {
                "schema_version": RECEIPT_SCHEMA,
                "install_id": args.install_id,
                "from": str(args.source),
                "to": str(args.target),
                "plan": plan,
                "created_at": utc_now(),
                "steps": [],
            },
        )
        print(f"receipt={path}")
        return 0
    if args.action == "step":
        document = load_receipt(path)
        require(
            args.step in document["plan"],
            f"step {args.step!r} is not part of the recorded plan {document['plan']}",
        )
        evidence = [str(item) for item in args.evidence]
        require(all(item for item in evidence), "every journal step needs non-empty evidence")
        document["steps"].append(
            {
                "seq": len(document["steps"]) + 1,
                "verb": args.verb,
                "step": args.step,
                "status": args.status,
                "at": args.at or utc_now(),
                "evidence": evidence,
            }
        )
        save_receipt(path, document)
        print(f"receipt={path}")
        return 0
    if args.action == "journal":
        document = load_receipt(path)
        rows = [row for row in document["steps"] if row["verb"] == args.verb]
        require(rows, f"the receipt has no {args.verb} steps yet")
        print(json.dumps([{key: row[key] for key in ("verb", "step", "status", "at", "evidence")} for row in rows]))
        return 0
    if args.action == "show":
        document = load_receipt(path)
        print(f"receipt={path}")
        print(f"steps={len(document['steps'])} plan={','.join(document['plan'])}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
