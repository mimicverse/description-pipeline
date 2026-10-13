"""Application-side platform store API: admission, transitions, stages, reads.

The fake world mirrors the deploy side's statements at the SQL level; real-PG
atomicity, locking and recovery are proven by the deploy side's battery, not
claimed by these unit tests.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from description_pipeline.io import PipelineError
from description_pipeline.platform.store import (
    AdmissionClosedError,
    OperationConflictError,
    PlatformStore,
    PlatformStoreError,
)

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


class FakeWorld:
    """A tiny in-memory stand-in for the deploy side's schema and two views."""

    def __init__(self, *, admission=("open", "window", "operator", NOW, 1)):
        self.admission = admission
        self.requests: list[tuple] = []
        self.runs: list[tuple] = []
        self.run_states: dict[str, str] = {}
        self.run_from_stage: dict[str, str | None] = {}
        self.operations: dict[str, tuple] = {}
        self.stages: dict[tuple[str, str], tuple] = {}
        self.confirmed_publishes: set[tuple[str, str]] = set()
        self.lifecycle_rows = [
            ("run", "r-1", "r-1", "running", "in_flight", NOW),
            ("scheduling", "dag/task/0", "r-1", "running", "in_flight", NOW),
        ]
        self.transactions = 0
        self.closes = 0
        self.hidden_for_one_find = False
        self.statements: list[str] = []

    def connect(self):
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, world: FakeWorld):
        self.world = world

    def cursor(self):
        return FakeCursor(self.world)

    def close(self):
        self.world.closes += 1

    def __enter__(self):
        self.world.transactions += 1
        return self

    def __exit__(self, *exc):
        return False


class FakeCursor:
    def __init__(self, world: FakeWorld):
        self.world = world
        self.description = None
        self._rows: list[tuple] = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        world = self.world
        world.statements.append(normalized)
        if "FROM platform.admission WHERE id = 1 FOR UPDATE" in normalized:
            self._rows = [world.admission] if world.admission is not None else []
        elif "UPDATE platform.admission" in normalized:
            state, reason, actor = params
            world.admission = (state, reason, actor, NOW, world.admission[4] + 1)
            self._rows = [world.admission]
        elif "FROM platform.admission_projection" in normalized:
            self._rows = [world.admission] if world.admission is not None else []
        elif normalized.startswith("INSERT INTO platform.request"):
            world.requests.append(tuple(params))
            self._rows = []
        elif normalized.startswith("INSERT INTO platform.run"):
            world.runs.append(tuple(params))
            world.run_states[params[0]] = "queued"
            world.run_from_stage[params[0]] = params[3]
            self._rows = []
        elif normalized.startswith("INSERT INTO platform.operation"):
            key = params[4]
            if key in world.operations:
                self._rows = []
            else:
                record = (params[0], "queued", key, params[5], params[1], params[2], params[3])
                world.operations[key] = record
                self._rows = [record]
        elif normalized.startswith(
            "SELECT operation_id, state, operation_key, params_digest, run_id, stage, kind"
            " FROM platform.operation WHERE operation_key"
        ):
            record = world.operations.get(params[0])
            if world.hidden_for_one_find:
                world.hidden_for_one_find = False
                record = None
            self._rows = [record] if record else []
        elif "SELECT state, from_stage FROM platform.run WHERE run_id = %s FOR UPDATE" in normalized:
            state = world.run_states.get(params[0])
            self._rows = [(state, world.run_from_stage.get(params[0]))] if state is not None else []
        elif "FROM platform.operation WHERE run_id = %s AND kind = 'publish' AND state = 'succeeded'" in normalized:
            self._rows = [(1,)] if (params[0], "publish") in world.confirmed_publishes else []
        elif normalized.startswith("SELECT state FROM platform.stage WHERE run_id = %s AND stage = %s"):
            record = world.stages.get((params[0], params[1]))
            self._rows = [(record[0],)] if record else []
        elif normalized.startswith("SELECT state, receipt FROM platform.stage WHERE run_id = %s AND stage = %s"):
            record = world.stages.get((params[0], params[1]))
            self._rows = [record] if record else []
        elif normalized.startswith("INSERT INTO platform.stage"):
            run_id, stage, state = params[0], params[1], params[2]
            receipt = json.loads(params[3]) if params[3] is not None else None
            existing = world.stages.get((run_id, stage))
            if existing is not None and existing[0] in {"completed", "failed", "cancelled", "unresolved"}:
                self._rows = []
            else:
                world.stages[(run_id, stage)] = (state, receipt)
                self._rows = [(run_id, stage, state)]
        elif "FROM platform.lifecycle_projection" in normalized:
            self.description = tuple(
                (name,) for name in ("object_kind", "object_id", "run_id", "state", "bucket", "updated_at")
            )
            self._rows = list(world.lifecycle_rows)
        else:
            raise AssertionError(f"Unexpected statement: {normalized}")
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


NAMED = {
    "request_id": "q-1",
    "run_id": "r-1",
    "principal": "portal",
    "target_hardware": "nd",
    "main_assembly": "cad/robot.SLDASM",
}

GENERATE_RECEIPT = {
    "run_id": "r-1",
    "stage": "generate",
    "state": "generated",
    "passed": False,
    "subject_sha256": "a" * 64,
}


class PlatformStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = FakeWorld()
        self.store = PlatformStore(self.world.connect)

    def admit(self, **overrides):
        payload = {
            **NAMED,
            "dispatch": {"stage": "capture", "operation_key": "r-1/capture:0", "params_digest": "a" * 64},
        }
        payload.update(overrides)
        self.store.admit_run(**payload)

    def _running_run(self, *, from_stage=None, verify_done=False):
        self.admit(from_stage=from_stage)
        self.world.run_states["r-1"] = "running"
        if verify_done:
            self.world.stages[("r-1", "verify")] = ("completed", {"stage": "verify"})

    def _publish_registration(self, **overrides):
        payload = {
            "run_id": "r-1",
            "stage": "publish",
            "kind": "publish",
            "operation_key": "r-1/publish:0",
            "params_digest": "b" * 64,
        }
        payload.update(overrides)
        return self.store.register_operation(**payload)

    # ------------------------------------------------------------- admission

    def test_close_and_open_are_idempotent_versioned_and_released(self) -> None:
        closed = self.store.close_admission(reason="drain", actor="operator")
        self.assertEqual(closed.state, "closed")
        self.assertEqual(closed.revision, 2)
        again = self.store.close_admission(reason="other", actor="someone")
        self.assertEqual(again, closed, "an already-closed gate returns the same snapshot")
        opened = self.store.open_admission(reason="window", actor="operator")
        self.assertEqual(opened.state, "open")
        self.assertEqual(opened.revision, 3)
        self.assertEqual(self.store.admission(), opened)
        self.assertEqual(self.world.closes, 4, "every call closes its fresh connection")

    def test_one_admission_entry_registers_request_run_and_intent_together(self) -> None:
        transactions = self.world.transactions
        self.admit(configuration={"root": "/srv/cell"}, tool_identity={"version": "1.4.0"})
        self.assertEqual(len(self.world.requests), 1)
        self.assertEqual(len(self.world.runs), 1)
        self.assertEqual(self.world.operations["r-1/capture:0"][1], "queued")
        self.assertEqual(
            self.world.transactions - transactions, 1, "request+run+intent share one admission transaction"
        )

    def test_a_missing_dispatch_intent_is_refused_and_writes_nothing(self) -> None:
        with self.assertRaises(PlatformStoreError):
            self.store.admit_run(**NAMED, dispatch=None)
        with self.assertRaises(PlatformStoreError):
            self.store.admit_run(**NAMED, dispatch={"stage": "capture"})
        self.assertEqual(self.world.requests, [])
        self.assertEqual(self.world.runs, [])
        self.assertEqual(self.world.operations, {})

    def test_new_admissions_are_refused_when_closed_and_write_nothing(self) -> None:
        self.store.close_admission(reason="drain", actor="operator")
        with self.assertRaises(AdmissionClosedError) as caught:
            self.admit()
        self.assertEqual(caught.exception.code, "admission_closed")
        self.assertEqual(self.world.requests, [])
        self.assertEqual(self.world.runs, [])
        self.assertEqual(self.world.operations, {})

    def test_non_finite_parameters_are_refused(self) -> None:
        with self.assertRaises(PlatformStoreError):
            self.admit(configuration={"gain": float("nan")})
        self.assertEqual(self.world.requests, [])

    # ------------------------------------------------------------- successors

    def test_successor_registration_is_not_gated_and_is_idempotent(self) -> None:
        self._running_run(from_stage="publish")
        self.store.close_admission(reason="drain", actor="operator")
        first = self._publish_registration()
        self.assertEqual(first.state, "queued")
        again = self._publish_registration()
        self.assertEqual(again, first)
        self.assertEqual(len(self.world.operations), 2, "a retry never restarts the side effect")

    def test_scope_or_parameter_drift_on_the_same_key_is_a_conflict(self) -> None:
        self._running_run(from_stage="publish")
        self._publish_registration(operation_key="k")
        for drift in (
            {"run_id": "r-2"},
            {"stage": "verify"},
            {"kind": "native"},
            {"params_digest": "c" * 64},
        ):
            with self.assertRaises(OperationConflictError):
                self._publish_registration(operation_key="k", **drift)

    def test_a_concurrent_duplicate_recovers_the_existing_result(self) -> None:
        self._running_run(from_stage="publish")
        self._publish_registration(operation_key="k")
        before = len(self.world.operations)
        self.world.hidden_for_one_find = True  # the winner commits between our select and insert
        recovered = self._publish_registration(operation_key="k")
        self.assertEqual(recovered.params_digest, "b" * 64)
        self.assertEqual(len(self.world.operations), before, "the collision was not re-sent as a new side effect")

    def test_successors_require_an_admitted_running_run(self) -> None:
        with self.assertRaisesRegex(PlatformStoreError, "not admitted"):
            self._publish_registration()
        self.admit()
        for state in ("queued", "unresolved"):
            self.world.run_states["r-1"] = state
            with self.assertRaisesRegex(PlatformStoreError, "not running"):
                self._publish_registration()

    def test_a_stage_after_the_start_requires_its_completed_predecessor(self) -> None:
        self._running_run()  # a fresh run starts at freeze; publish needs verify first
        with self.assertRaisesRegex(PlatformStoreError, "requires the completed 'verify' stage"):
            self._publish_registration()
        self.world.stages[("r-1", "verify")] = ("completed", {"stage": "verify"})
        self.assertEqual(self._publish_registration().stage, "publish")

    def test_terminal_runs_only_continue_via_cancel_or_reconcile(self) -> None:
        self._running_run(from_stage="publish")
        self.world.run_states["r-1"] = "failed"
        with self.assertRaisesRegex(PlatformStoreError, "terminal"):
            self._publish_registration()
        record = self._publish_registration(kind="cancel")
        self.assertEqual(record.kind, "cancel")

    def test_a_confirmed_publication_can_never_be_cancelled(self) -> None:
        self._running_run(from_stage="publish")
        self.world.confirmed_publishes.add(("r-1", "publish"))
        with self.assertRaisesRegex(PlatformStoreError, "confirmed publication"):
            self._publish_registration(kind="cancel")

    # ----------------------------------------------------------------- stages

    def test_confirm_stage_requires_bound_terminal_receipts_and_is_immutable(self) -> None:
        self._running_run()
        with self.assertRaisesRegex(PlatformStoreError, "state must be one of"):
            self.store.confirm_stage(run_id="r-1", stage="generate", state="generated")
        with self.assertRaisesRegex(PlatformStoreError, "bound sealed receipt"):
            self.store.confirm_stage(run_id="r-1", stage="generate", state="completed")
        with self.assertRaisesRegex(PlatformStoreError, "confirmed receipt state"):
            self.store.confirm_stage(
                run_id="r-1", stage="generate", state="completed", receipt={**GENERATE_RECEIPT, "state": "failed"}
            )
        self.store.confirm_stage(run_id="r-1", stage="generate", state="running")
        self.store.confirm_stage(
            run_id="r-1", stage="generate", state="completed", receipt=GENERATE_RECEIPT
        )
        with self.assertRaisesRegex(PlatformStoreError, "already terminal"):
            self.store.confirm_stage(run_id="r-1", stage="generate", state="failed", receipt=GENERATE_RECEIPT)
        self.store.confirm_stage(
            run_id="r-1", stage="verify", state="failed", receipt={"run_id": "r-1", "stage": "verify"}
        )
        with self.assertRaisesRegex(PlatformStoreError, "already terminal"):
            self.store.confirm_stage(run_id="r-1", stage="verify", state="running")

    def test_confirmed_stage_returns_sealed_receipts_or_refuses(self) -> None:
        self._running_run()
        self.assertEqual(self.store.confirmed_stage("r-1", "generate"), None)
        self.store.confirm_stage(run_id="r-1", stage="generate", state="running")
        self.assertEqual(self.store.confirmed_stage("r-1", "generate"), None)
        self.store.confirm_stage(run_id="r-1", stage="generate", state="completed", receipt=GENERATE_RECEIPT)
        self.assertEqual(self.store.confirmed_stage("r-1", "generate"), GENERATE_RECEIPT)
        self.world.stages[("r-1", "generate")] = ("completed", {**GENERATE_RECEIPT, "run_id": "r-2"})
        with self.assertRaisesRegex(PlatformStoreError, "does not bind"):
            self.store.confirmed_stage("r-1", "generate")

    # ------------------------------------------------------------------ reads

    def test_reads_parse_the_two_projections(self) -> None:
        rows = self.store.lifecycle_rows()
        self.assertEqual([row["object_kind"] for row in rows], ["run", "scheduling"])
        self.assertEqual(rows[1]["bucket"], "in_flight")
        self.assertEqual(self.store.admission().state, "open")

    def test_missing_admission_row_is_refused_without_typing_it_closed(self) -> None:
        self.world.admission = None
        with self.assertRaises(PlatformStoreError) as caught:
            self.store.close_admission(reason="drain", actor="operator")
        self.assertIsInstance(caught.exception, PipelineError)
        self.assertNotIsInstance(caught.exception, AdmissionClosedError)
        with self.assertRaises(PlatformStoreError):
            self.admit()


if __name__ == "__main__":
    unittest.main()
