"""Interface-layer reuse fixtures against one real platform PostgreSQL.

Not whole-chain qualification (the driver-level reuse/invalidation branches run
through linked-run commands after the alpha2 wiring): this fixture drives the
production store APIs directly and proves the interface-layer contract:

- a linked attempt (same request evidence, registered parent) reuses the
  parent's confirmed stage checkpoint through ``confirm_stage`` and the DB
  registers the source run and the original execution time; the attempt has no
  native operation, so a pure reuse never opens CAD;
- an unregistered parent and an unregistered checkpoint are refused.

The sealed delivery is built with the same builder the platform-store suite
uses; the store re-verifies the real sealed bytes before registering.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))

    import psycopg2  # noqa: PLC0415

    from description_pipeline.platform.store import PlatformStore, PlatformStoreError  # noqa: PLC0415
    from tests.v1.test_platform_store import build_sealed_delivery  # noqa: PLC0415

    dsn = os.environ.get("PLATFORM_SERVICE_DSN", "")
    if not dsn:
        print(json.dumps({"ok": False, "error": "PLATFORM_SERVICE_DSN is not set"}))
        return 1

    def connect():
        return psycopg2.connect(dsn)

    sealed_root = Path(tempfile.mkdtemp(prefix="settlement-fixture-sealed-"))
    subject, artifacts, handoff = build_sealed_delivery(sealed_root)
    checkpoint = "store://sealed/fixture-reuse"
    store = PlatformStore(connect, sealed=lambda _uri: sealed_root)

    suffix = uuid.uuid4().hex[:10]
    request_id = f"fixture-request-{suffix}"
    parent = f"fixture-parent-{suffix}"
    linked = f"fixture-linked-{suffix}"
    standalone = f"fixture-standalone-{suffix}"
    executed_at = datetime.now(UTC)

    def admit(run_id: str, *, parent_run_id: str | None = None, request: str | None = None) -> None:
        store.admit_run(
            request_id=request or request_id,
            run_id=run_id,
            principal="fixture:principal",
            target_hardware="nd_fixture",
            main_assembly="cad/robot.SLDASM",
            parent_run_id=parent_run_id,
            from_stage="generate" if parent_run_id else None,
            dispatch={
                "stage": "generate",
                "operation_key": f"{run_id}/generate:0",
                "params_digest": "a" * 64,
            },
        )

    receipt = {
        "schema_version": "solidworks-to-urdf.bundle/v1",
        "pipeline_id": "solidworks-to-urdf",
        "run_id": parent,
        "stage": "generate",
        "state": "generated",
        "passed": False,
        "subject_sha256": subject,
        "handoff_sha256": handoff,
        "artifacts": [dict(record) for record in artifacts],
    }
    cases: list[dict] = []
    try:
        admit(parent)
        store.confirm_stage(
            run_id=parent, stage="generate", state="completed", receipt=receipt, checkpoint_uri=checkpoint
        )
        # One request row per admission is a store invariant; carrying the ORIGINAL
        # request_id inside the frozen request evidence is a driver branch (alpha2
        # wiring).  The interface layer proves the checkpoint/source registration.
        admit(linked, parent_run_id=parent, request=f"fixture-linked-request-{suffix}")
        store.confirm_stage(
            run_id=linked,
            stage="generate",
            state="completed",
            receipt={**receipt, "run_id": linked},
            checkpoint_uri=checkpoint,
            reused_from_run=parent,
            original_executed_at=executed_at,
        )
        with psycopg2.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*)::int FROM request WHERE request_id=%s", (request_id,))
            request_rows = cursor.fetchone()[0]
            cursor.execute(
                "SELECT reused_from_run, original_executed_at FROM stage WHERE run_id=%s AND stage='generate'",
                (linked,),
            )
            reused_from, original_at = cursor.fetchone()
            cursor.execute("SELECT kind FROM operation WHERE run_id=%s", (linked,))
            linked_kinds = [row[0] for row in cursor.fetchall()]
        cases.append(
            {
                "name": "content_reuse_registers_the_source_and_keeps_the_original_evidence",
                "passed": request_rows == 1
                and reused_from == parent
                and original_at is not None
                and "native" not in linked_kinds,
                "observed": {
                    "original_request_rows": request_rows,
                    "reused_from": reused_from,
                    "original_executed_at": original_at.isoformat() if original_at else None,
                    "operation_kinds": linked_kinds,
                },
            }
        )

        admit(standalone, request=f"fixture-other-request-{suffix}")
        refused_parent = False
        try:
            store.confirm_stage(
                run_id=standalone,
                stage="generate",
                state="completed",
                receipt={**receipt, "run_id": standalone},
                checkpoint_uri=checkpoint,
                reused_from_run=parent,
            )
        except PlatformStoreError:
            refused_parent = True
        cases.append({"name": "unregistered_parent_refused", "passed": refused_parent, "observed": refused_parent})

        refused_checkpoint = False
        try:
            store.confirm_stage(
                run_id=linked,
                stage="capture",
                state="completed",
                receipt={**receipt, "run_id": linked, "stage": "capture"},
                checkpoint_uri="store://sealed/other",
                reused_from_run=parent,
            )
        except PlatformStoreError:
            refused_checkpoint = True
        cases.append(
            {"name": "unregistered_checkpoint_refused", "passed": refused_checkpoint, "observed": refused_checkpoint}
        )
    finally:
        pass

    report = {
        "scope": "interface-layer production-store fixture on one real platform PostgreSQL; "
        "not whole-chain reuse qualification (driver branches after the alpha2 wiring)",
        "cases": cases,
        "ok": all(case["passed"] for case in cases),
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        args.report.write_text(payload, encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
