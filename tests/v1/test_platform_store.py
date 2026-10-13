"""Application-side platform store API: admission semantics, idempotency, reads."""

from __future__ import annotations

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
        self.operations: dict[str, tuple] = {}
        self.lifecycle_rows = [
            ("run", "r-1", "r-1", "running", "in_flight", NOW),
            ("scheduling", "dag/task/0", "r-1", "running", "in_flight", NOW),
        ]
        self.commits = 0
        self.statements: list[str] = []

    def connect(self):
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, world: FakeWorld):
        self.world = world

    def cursor(self):
        return FakeCursor(self.world)

    def __enter__(self):
        self.world.commits += 1
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
            self._rows = []
        elif normalized.startswith("INSERT INTO platform.operation"):
            record = (params[0], "queued", params[4], params[5])
            world.operations[params[4]] = record
            self._rows = [record]
        elif normalized.startswith(
            "SELECT operation_id, state, operation_key, params_digest FROM platform.operation"
        ):
            record = world.operations.get(params[0])
            self._rows = [record] if record else []
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


class PlatformStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = FakeWorld()
        self.store = PlatformStore(self.world.connect)

    def test_close_and_open_are_idempotent_and_versioned(self) -> None:
        closed = self.store.close_admission(reason="drain", actor="operator")
        self.assertEqual(closed.state, "closed")
        self.assertEqual(closed.revision, 2)
        again = self.store.close_admission(reason="other", actor="someone")
        self.assertEqual(again, closed, "an already-closed gate returns the same snapshot")
        opened = self.store.open_admission(reason="window", actor="operator")
        self.assertEqual(opened.state, "open")
        self.assertEqual(opened.revision, 3)
        self.assertEqual(self.store.admission(), opened)

    def test_new_admissions_are_refused_when_closed_and_write_nothing(self) -> None:
        self.store.close_admission(reason="drain", actor="operator")
        with self.assertRaises(AdmissionClosedError) as caught:
            self.store.submit_request(
                request_id="q-1", principal="portal", target_hardware="nd", main_assembly="cad/robot.SLDASM"
            )
        self.assertEqual(caught.exception.code, "admission_closed")
        with self.assertRaises(AdmissionClosedError):
            self.store.create_run(run_id="r-1", request_id="q-1")
        self.assertEqual(self.world.requests, [])
        self.assertEqual(self.world.runs, [])
        self.assertEqual(self.world.operations, {})

    def test_admitted_request_run_and_initial_intent_land_together(self) -> None:
        commits = self.world.commits
        self.store.submit_request(
            request_id="q-1",
            principal="portal",
            target_hardware="nd",
            main_assembly="cad/robot.SLDASM",
            configuration={"root": "/srv/cell"},
        )
        self.store.create_run(
            run_id="r-1",
            request_id="q-1",
            tool_identity={"version": "1.4.0"},
            dispatch={"stage": "capture", "operation_key": "r-1/capture:0", "params_digest": "a" * 64},
        )
        self.assertEqual(len(self.world.requests), 1)
        self.assertEqual(len(self.world.runs), 1)
        self.assertEqual(self.world.operations["r-1/capture:0"][1], "queued")
        self.assertEqual(self.world.commits, commits + 2, "one transaction per admitted object")

    def test_operation_registration_is_not_gated_and_is_idempotent(self) -> None:
        self.store.close_admission(reason="drain", actor="operator")
        first = self.store.register_operation(
            run_id="r-1", stage="publish", kind="publish", operation_key="r-1/publish:0", params_digest="b" * 64
        )
        self.assertEqual(first.state, "queued")
        again = self.store.register_operation(
            run_id="r-1", stage="publish", kind="publish", operation_key="r-1/publish:0", params_digest="b" * 64
        )
        self.assertEqual(again, first)
        self.assertEqual(len(self.world.operations), 1, "a retry never restarts the side effect")
        with self.assertRaises(OperationConflictError):
            self.store.register_operation(
                run_id="r-1",
                stage="publish",
                kind="publish",
                operation_key="r-1/publish:0",
                params_digest="c" * 64,
            )

    def test_reads_parse_the_two_projections(self) -> None:
        rows = self.store.lifecycle_rows()
        self.assertEqual([row["object_kind"] for row in rows], ["run", "scheduling"])
        self.assertEqual(rows[1]["bucket"], "in_flight")
        self.assertEqual(rows[0]["run_id"], "r-1")

    def test_missing_admission_row_is_refused_without_typing_it_closed(self) -> None:
        self.world.admission = None
        with self.assertRaises(PlatformStoreError) as caught:
            self.store.close_admission(reason="drain", actor="operator")
        self.assertIsInstance(caught.exception, PipelineError)
        self.assertNotIsInstance(caught.exception, AdmissionClosedError)
        with self.assertRaises(PlatformStoreError):
            self.store.submit_request(
                request_id="q-1", principal="portal", target_hardware="nd", main_assembly="cad/robot.SLDASM"
            )


if __name__ == "__main__":
    unittest.main()
