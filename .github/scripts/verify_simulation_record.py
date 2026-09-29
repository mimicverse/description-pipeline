"""Verify that a simulation record qualifies the dispatched subject and candidate.

The runner measures the *candidate* the trusted tool built for the requested profile,
which may differ from the model checkout when the checkout was published under another
profile, so the record is bound to the candidate's own consumer scene.  The dispatched
subject is checked as well: it is the digest the workflow claims to have verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from description_pipeline.io import write_json

CONSUMER_MJCF = "mjcf/scene.xml"
PENDING = "consumer.application"


def verify(*, record: Path, subject: str, candidate: Path, summary: dict) -> dict:
    """Fail unless the record measures exactly this subject and candidate scene."""

    if not re.fullmatch(r"[0-9a-f]{64}", subject or ""):
        raise ValueError("Dispatched subject must be a 64-hex digest")
    payload = json.loads(Path(record).read_text(encoding="utf-8"))
    if payload["subject"] != subject:
        raise ValueError("Measured subject differs from the dispatched subject")
    path = payload["model"]["mjcf"]
    if path != CONSUMER_MJCF:
        raise ValueError(f"Record measures {path!r} instead of the consumer scene")
    scene = Path(candidate) / CONSUMER_MJCF
    if payload["model"]["mjcf_sha256"] != hashlib.sha256(scene.read_bytes()).hexdigest():
        raise ValueError("Record scene differs from the candidate this run built")
    pending = [name for name in payload["model"]["oracle"]["pending"] if name != PENDING]
    if pending:
        raise ValueError(f"Record tolerates pending checks: {pending}")
    summary["tests"] = {item["suite"]: item["passed"] for item in payload["results"]}
    summary["release_qualified"] = payload["qualification"]["release_qualified"]
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--record", type=Path, required=True, help="Produced acceptance.json")
    parser.add_argument("--candidate", type=Path, required=True, help="Candidate the runner measured")
    parser.add_argument("--identity", type=Path, required=True, help="Where to keep the run identity summary")
    parser.add_argument("--driver", type=Path, default=Path("driver"), help="Tooling checkout to record HEAD for")
    args = parser.parse_args(argv)
    environment = os.environ
    summary = {
        "subject": environment.get("INPUT_SUBJECT", ""),
        "model_sha": environment.get("INPUT_MODEL_SHA", ""),
        "profile": environment.get("INPUT_PROFILE", ""),
        "run_id": environment.get("GITHUB_RUN_ID", ""),
        "run_attempt": environment.get("GITHUB_RUN_ATTEMPT", ""),
        "repository": environment.get("GITHUB_REPOSITORY", ""),
        "head_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.driver, text=True).strip(),
        "record_present": args.record.is_file(),
    }
    try:
        if not args.record.is_file():
            raise ValueError("Simulation acceptance produced no record")
        verify(record=args.record, subject=summary["subject"], candidate=args.candidate, summary=summary)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        write_json(args.identity, summary)
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    write_json(args.identity, summary)
    print(
        json.dumps(
            {"ok": True, "tests": summary["tests"], "release_qualified": summary["release_qualified"]},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
