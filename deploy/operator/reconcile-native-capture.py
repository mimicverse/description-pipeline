#!/usr/bin/env python3
"""Controlled maintenance recovery for a native capture failure.

This entry reconstructs the completed discover stage from the run's original
prepared package and settles the one bound native owner operation through the
same store rules used by the DAG.  It never retries CAD, never reuses a native
job, and never changes the real capture failure into a discover failure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

from description_pipeline.io import digest, file_digest
from description_pipeline.platform.native_recovery import (
    build_discovery_tree_from_prepared,
    discovery_receipt,
    validate_capture_failure_events,
)
from description_pipeline.platform.store import PlatformStore, PlatformStoreError
from description_pipeline.platform.storage import seal_stage_confirmation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--derived", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--task-id", default="discover")
    parser.add_argument("--map-index", type=int, default=-1)
    parser.add_argument("--evidence", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        parsed_run = uuid.UUID(args.run_id)
    except (ValueError, AttributeError) as error:
        raise PlatformStoreError("The maintenance run id is not a canonical UUID") from error
    if str(parsed_run) != args.run_id:
        raise PlatformStoreError("The maintenance run id is not canonical")
    job = json.loads(args.job.read_text(encoding="utf-8"))
    if not isinstance(job, dict) or job.get("status") != "failed":
        raise PlatformStoreError("The maintenance receipt is not the original failed native job")
    if job.get("run_id") != args.run_id:
        raise PlatformStoreError("The maintenance receipt names another run")
    events = [event for event in job.get("events") or [] if isinstance(event, dict)]
    try:
        validate_capture_failure_events(events)
    except Exception as error:
        raise PlatformStoreError(f"The maintenance receipt is not an ordered capture failure: {error}") from error
    prepared = job.get("prepared_files")
    native_tool = job.get("native_tool")
    native_request = job.get("request") if isinstance(job.get("request"), dict) else {}
    expected_archive = (job.get("capture_request") or {}).get("sha256")
    if not isinstance(prepared, dict) or not isinstance(native_tool, dict):
        raise PlatformStoreError("The native failed job carries no prepared identity")
    if not args.archive.is_file() or file_digest(args.archive) != expected_archive:
        raise PlatformStoreError("The maintenance archive differs from the native job identity")
    run_root = Path(os.environ["PIPELINE_STORE_ROOT"]) / args.run_id
    tree = run_root / "seal" / "native-discover-maintenance"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    built = build_discovery_tree_from_prepared(
        tree=tree,
        derived=args.derived,
        archive_path=args.archive,
        prepared=prepared,
        native_tool=native_tool,
        run_id=args.run_id,
        main_assembly=str(job["request"]["main_assembly"]),
        native_stages=job.get("stages") if isinstance(job.get("stages"), dict) else None,
        request_evidence=(
            (run_root / "stages" / "freeze" / "input" / "request.json").read_bytes()
            if (run_root / "stages" / "freeze" / "input" / "request.json").is_file()
            else None
        ),
        portable_tool=(
            json.loads((run_root / "stages" / "freeze" / "tool.json").read_text(encoding="utf-8"))
            if (run_root / "stages" / "freeze" / "tool.json").is_file()
            else None
        ),
        request_upload_files=native_request.get("upload_files"),
        request_upload_sha256=native_request.get("upload_sha256"),
    )
    if job.get("handoff_sha256") != built["native_handoff"]:
        raise PlatformStoreError("The native prepared inventory differs from the failed job handoff")
    store_root = Path(os.environ["PIPELINE_STORE_ROOT"])
    platform = PlatformStore.from_environment()
    discover_receipt = None
    discover_checkpoint = None
    if platform.confirmed_stage(args.run_id, "discover") is None:
        receipt = discovery_receipt(
            run_id=args.run_id,
            tree=tree,
            native_tool=native_tool,
            native_handoff=built["native_handoff"],
            native_stages_sha256=built["native_stages_sha256"],
            request_evidence_sha256=built["request_evidence_sha256"],
            portable_tool_sha256=built["portable_tool_sha256"],
        )
        discover_checkpoint, discover_receipt = seal_stage_confirmation(
            store_root,
            args.run_id,
            "discover",
            tree,
            receipt,
        )
    evidence = list(args.evidence) or [f"{args.job}#sha256={file_digest(args.job)}"]
    reconcile = {
        "operation_key": f"operatorctl-reconcile:{args.run_id}:discover:failed",
        "params_digest": digest(
            {
                "outcome": "failed",
                "evidence": evidence,
                "binding": {
                    "operation_id": args.operation_id,
                    "task_id": args.task_id,
                    "map_index": args.map_index,
                },
                "reported_stage": "capture",
            }
        ),
        "evidence": evidence,
        "binding": {
            "operation_id": args.operation_id,
            "task_id": args.task_id,
            "map_index": args.map_index,
        },
        "native_receipt": {"path": str(args.job), "native_run_id": args.run_id},
        "reported_stage": "capture",
        "discover_receipt": discover_receipt,
        "discover_checkpoint": discover_checkpoint,
    }
    platform.confirm_stage(
        run_id=args.run_id,
        stage="discover",
        state="failed",
        reconcile=reconcile,
    )
    print(f"reconciled native capture owner: {args.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
