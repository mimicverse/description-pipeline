"""Interface-layer reuse fixtures against one real platform PostgreSQL.

This is the mandatory real-chain fixture the CI job runs after the
portal-admission smoke, against the canonical platform schema with the real
``PlatformStore``: the interface-layer reuse contract, driven directly through
the production store APIs.

- a linked attempt (registered parent, same frozen request evidence) reuses the
  parent's confirmed stage checkpoint through ``confirm_stage``: the source
  receipt and its registered execution instant are carried verbatim - reuse
  never re-authors the receipt and never invents a timestamp - the DB registers
  the source run, and the attempt has no native operation, so a pure reuse
  never opens CAD;
- reuse under a run whose parent is not registered, and reuse naming a
  checkpoint that is not the source stage's registered reference, are refused
  with the store's specific errors.

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

    from description_pipeline.io import digest  # noqa: PLC0415
    from description_pipeline.platform.store import PlatformStore, PlatformStoreError  # noqa: PLC0415
    from tests.v1.test_platform_store import build_sealed_delivery  # noqa: PLC0415

    dsn = os.environ.get("PLATFORM_SERVICE_DSN", "")
    if not dsn:
        print(json.dumps({"ok": False, "error": "PLATFORM_SERVICE_DSN is not set"}))
        return 1

    def connect():
        return psycopg2.connect(dsn)

    sealed_root = Path(tempfile.mkdtemp(prefix="settlement-fixture-sealed-"))
    subject, artifacts, _handoff_full, handoff_native = build_sealed_delivery(sealed_root)
    store = PlatformStore(connect, sealed=lambda _uri: sealed_root)

    # Canonical lowercase UUID run identities; the sealed locator names exactly
    # one run and one engineering stage.
    parent = str(uuid.uuid4())
    linked = str(uuid.uuid4())
    standalone = str(uuid.uuid4())
    request_id = f"fixture-request-{uuid.uuid4().hex[:10]}"
    checkpoint = f"store://sealed/{parent}/generate"
    source_executed_at = datetime.now(UTC)

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

    # A post-freeze stage binds the sealed native transfer identity (the digest
    # of the selected native inventory), not the full prepared-package digest.
    receipt = {
        "schema_version": "solidworks-to-urdf.bundle/v1",
        "pipeline_id": "solidworks-to-urdf",
        "run_id": parent,
        "stage": "generate",
        "state": "generated",
        "passed": False,
        "subject_sha256": subject,
        "handoff_sha256": handoff_native,
        "artifacts": [dict(record) for record in artifacts],
    }
    cases: list[dict] = []

    admit(parent)
    store.confirm_stage(
        run_id=parent,
        stage="generate",
        state="completed",
        receipt=receipt,
        checkpoint_uri=checkpoint,
        original_executed_at=source_executed_at,
    )
    source_row = store.stage_records(parent)["generate"]
    # The reused confirmation carries the source's OWN receipt and the instant
    # REGISTERED for the source stage - the values are read back from the store,
    # never re-authored here.
    admit(linked, parent_run_id=parent, request=f"fixture-linked-request-{uuid.uuid4().hex[:10]}")
    store.confirm_stage(
        run_id=linked,
        stage="generate",
        state="completed",
        receipt=source_row["receipt"],
        checkpoint_uri=checkpoint,
        reused_from_run=parent,
        original_executed_at=source_row["original_executed_at"],
    )
    reused_receipt = store.confirmed_stage(linked, "generate")
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
    reused_is_source = bool(
        reused_receipt is not None
        and digest(reused_receipt) == digest(source_row["receipt"])
        and reused_receipt.get("run_id") == parent
    )
    cases.append(
        {
            "name": "content_reuse_registers_the_source_and_keeps_the_original_evidence",
            "passed": (
                request_rows == 1
                and reused_from == parent
                and original_at == source_row["original_executed_at"]
                and "native" not in linked_kinds
                and reused_is_source
            ),
            "observed": {
                "original_request_rows": request_rows,
                "reused_from": reused_from,
                "original_executed_at": original_at.isoformat() if original_at else None,
                "registered_source_executed_at": (
                    source_row["original_executed_at"].isoformat()
                    if source_row["original_executed_at"]
                    else None
                ),
                "operation_kinds": linked_kinds,
                "reused_receipt_is_the_source_receipt": reused_is_source,
            },
        }
    )

    admit(standalone, request=f"fixture-other-request-{uuid.uuid4().hex[:10]}")
    try:
        store.confirm_stage(
            run_id=standalone,
            stage="generate",
            state="completed",
            receipt=source_row["receipt"],
            checkpoint_uri=checkpoint,
            reused_from_run=parent,
            original_executed_at=source_row["original_executed_at"],
        )
        parent_error = None
    except PlatformStoreError as error:
        parent_error = str(error)
    cases.append(
        {
            "name": "unregistered_parent_refused",
            "passed": parent_error == "Reuse requires the registered parent run of this linked attempt",
            "observed": parent_error,
        }
    )

    try:
        store.confirm_stage(
            run_id=linked,
            stage="generate",
            state="completed",
            receipt=source_row["receipt"],
            checkpoint_uri=f"store://sealed/{linked}/generate",
            reused_from_run=parent,
            original_executed_at=source_row["original_executed_at"],
        )
        checkpoint_error = None
    except PlatformStoreError as error:
        checkpoint_error = str(error)
    cases.append(
        {
            "name": "unregistered_checkpoint_refused",
            "passed": checkpoint_error
            == "The reused checkpoint is not the source stage's registered reference",
            "observed": checkpoint_error,
        }
    )

    report = {
        "scope": "interface-layer production-store fixture on one real platform PostgreSQL; "
        "the mandatory real-chain reuse contract (registered source receipt and "
        "registered execution instant carried verbatim, no native operation on pure reuse)",
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
