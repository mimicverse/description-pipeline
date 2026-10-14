"""Application-side platform store API: admission, transitions, stages, reads.

The fake world mirrors the deploy side's statements at the SQL level; the
sealed resolver points at a real on-disk delivery so receipt bytes are rebound
for real.  Real-PG atomicity, locking and recovery are proven by the deploy
side's battery, not claimed by these unit tests.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from description_pipeline.delivery import PIPELINE_ID, read_delivery_manifest, write_delivery_manifest
from description_pipeline.interface_records import CHECK_POLICY_SCHEMA, CHECK_POLICY_V1, check_policy_digest
from description_pipeline.io import PipelineError, digest, file_digest, read_data, write_json
from description_pipeline.platform import store as store_module
from description_pipeline.platform.store import (
    AdmissionClosedError,
    OperationConflictError,
    PlatformStore,
    PlatformStoreError,
    RunNotAdmittedError,
)
from description_pipeline.runtime import tool_record
from description_pipeline.schemas import TOOL_SCHEMA
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256
from description_pipeline.verification import solidworks_urdf
from description_pipeline.verification.solidworks_urdf import required_checks

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
CHECKPOINT = "store://sealed/run-r-1"
NATIVE_STAGE_STATES = {"freeze": "frozen", "discover": "discovered", "capture": "native_complete"}


def build_sealed_delivery(root: Path) -> tuple[str, list[dict], str]:
    """One real sealed delivery: manifest record-for-record plus its digests.

    The tree covers the declared evidence of all six stages, so the same
    delivery serves the portable and the native confirmations; the third
    return value is the sealed ``input/handoff.json`` digest every receipt
    must bind.
    """

    for name in ("input", "evidence", "model", "urdf", "meshes", "reports"):
        (root / name).mkdir(parents=True)
    (root / "input" / "records").mkdir()
    (root / "evidence" / "native-meshes").mkdir()
    write_json(root / "input" / "source.json", {"source": "fixture"})
    write_json(root / "input" / "records" / "arm.json", {"record": "fixture"})
    package_files = {
        "source.json": file_digest(root / "input" / "source.json"),
        "records/arm.json": file_digest(root / "input" / "records" / "arm.json"),
    }
    write_json(root / "input" / "handoff.json", {"passed": True, "package_files": package_files})
    write_json(root / "evidence" / "discovery.json", {"discovery": "fixture"})
    write_json(root / "evidence" / "capture-plan.json", {"plan": "fixture"})
    write_json(root / "evidence" / "capture.json", {"capture": "fixture"})
    write_json(root / "evidence" / "native-runtime.json", {"runtime": "fixture"})
    (root / "evidence" / "native-meshes" / "part.stl").write_bytes(b"native-stl")
    write_json(root / "model" / "robot.json", {"model": "fixture"})
    (root / "urdf" / "robot.urdf").write_text("<robot/>\n", encoding="utf-8")
    (root / "meshes" / "part.stl").write_bytes(b"mesh-stl")
    write_json(root / "tool.json", tool_record())
    write_json(root / "reports" / "quality.json", {"quality": "fixture"})
    write_json(
        root / "reports" / "native-tool.json",
        {
            "schema_version": TOOL_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "version": "1.4.0",
            "source_sha256": "a" * 64,
            "runtime": {"role": "native"},
        },
    )
    write_delivery_manifest(
        root,
        contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
        policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
    )
    manifest = read_delivery_manifest(root)
    artifacts = [dict(record) for record in manifest["subject"]["files"]]
    handoff = digest(package_files)
    return manifest["subject"]["digest"], artifacts, handoff


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
        self.events: list[tuple] = []
        self.confirmed_publishes: set[tuple[str, str]] = set()
        self.lifecycle_rows = [
            ("run", "r-1", "r-1", "running", "in_flight", NOW),
            ("scheduling", "dag/task/0", "r-1", "running", "in_flight", NOW),
        ]
        self.transactions = 0
        self.open_transactions = 0
        self.closes = 0
        self.hidden_finds = 0
        self.statements: list[str] = []
        #: run_id -> [display_name, hidden_at, hidden_reason, updated_at]
        self.run_display_rows: dict[str, list] = {}

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
        self.world.open_transactions += 1
        return self

    def __exit__(self, *exc):
        self.world.open_transactions -= 1
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
        elif normalized.startswith("INSERT INTO platform.run ("):
            world.runs.append(tuple(params))
            world.run_states[params[0]] = "queued"
            world.run_from_stage[params[0]] = params[3]
            self._rows = []
        elif normalized.startswith("UPDATE platform.run SET state = 'running'"):
            if world.run_states.get(params[0]) == "queued":
                world.run_states[params[0]] = "running"
                self._rows = [("running",)]
            else:
                self._rows = []
        elif normalized.startswith("UPDATE platform.run SET state = %s"):
            outcome, run_id = params
            if world.run_states.get(run_id) not in {"succeeded", "failed", "cancelled", None}:
                world.run_states[run_id] = outcome
            self._rows = []
        elif normalized.startswith(
            "SELECT state, request_id, parent_run_id, from_stage, tool_identity FROM platform.run WHERE run_id = %s"
        ):
            entry = next((row for row in world.runs if row[0] == params[0]), None)
            if entry is None:
                self._rows = []
            else:
                state = world.run_states.get(entry[0])
                self._rows = [(state, entry[1], entry[2], entry[3], entry[4])]
        elif normalized.startswith(
            "SELECT stage, state, receipt, checkpoint_uri, reused_from_run, original_executed_at"
            " FROM platform.stage WHERE run_id = %s"
        ):
            self._rows = [
                (stage, record[0], record[1], record[2], None, None)
                for (run_id, stage), record in world.stages.items()
                if run_id == params[0]
            ]
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
            if world.hidden_finds:
                world.hidden_finds -= 1
                record = None
            self._rows = [record] if record else []
        elif "SELECT state, from_stage FROM platform.run WHERE run_id = %s" in normalized:
            state = world.run_states.get(params[0])
            self._rows = [(state, world.run_from_stage.get(params[0]))] if state is not None else []
        elif normalized.startswith(
            "SELECT run_id, display_name, hidden_at, hidden_reason, updated_at FROM platform.run"
        ):
            if "WHERE run_id = %s" in normalized:
                row = world.run_display_rows.get(params[0])
                self._rows = [(params[0], *row)] if row is not None else []
            else:
                self._rows = [
                    (run_id, *row)
                    for run_id, row in world.run_display_rows.items()
                    if row[0] is not None or row[1] is not None
                ]
        elif normalized.startswith("UPDATE platform.run SET display_name"):
            row = world.run_display_rows.get(params[1])
            if row is not None:
                row[0] = params[0]
                row[3] = NOW
                self._rows = [(params[1], *row)]
            else:
                self._rows = []
        elif normalized.startswith("UPDATE platform.run SET hidden_at = COALESCE"):
            row = world.run_display_rows.get(params[1])
            if row is not None:
                if row[1] is None:
                    row[1] = NOW
                if row[2] is None:
                    row[2] = params[0]
                row[3] = NOW
                self._rows = [(params[1], *row)]
            else:
                self._rows = []
        elif normalized.startswith("UPDATE platform.run SET hidden_at = NULL"):
            row = world.run_display_rows.get(params[0])
            if row is not None:
                row[1] = None
                row[2] = None
                row[3] = NOW
                self._rows = [(params[0], *row)]
            else:
                self._rows = []
        elif "FROM platform.operation WHERE run_id = %s AND kind = 'publish' AND state = 'succeeded'" in normalized:
            self._rows = [(1,)] if (params[0], "publish") in world.confirmed_publishes else []
        elif (
            "FROM platform.operation WHERE operation_id = %s AND run_id = %s AND kind = 'reconcile' AND stage = %s"
            in normalized
        ):
            found = any(
                item[0] == params[0] and item[4] == params[1] and item[6] == "reconcile" and item[5] == params[2]
                for item in world.operations.values()
            )
            self._rows = [(1,)] if found else []
        elif (
            "FROM platform.operation WHERE run_id = %s AND stage = %s AND kind <> 'reconcile'"
            " AND state = 'unresolved'"
        ) in normalized:
            found = any(
                item[4] == params[0] and item[5] == params[1] and item[6] != "reconcile" and item[1] == "unresolved"
                for item in world.operations.values()
            )
            self._rows = [(1,)] if found else []
        elif normalized.startswith("SELECT state FROM platform.stage WHERE run_id = %s AND stage = %s FOR UPDATE"):
            record = world.stages.get((params[0], params[1]))
            self._rows = [(record[0],)] if record else []
        elif normalized.startswith(
            "SELECT state, receipt, checkpoint_uri FROM platform.stage WHERE run_id = %s AND stage = %s"
        ):
            record = world.stages.get((params[0], params[1]))
            self._rows = [record] if record else []
        elif normalized.startswith("INSERT INTO platform.stage"):
            run_id, stage, state = params[0], params[1], params[2]
            receipt = json.loads(params[3]) if params[3] is not None else None
            checkpoint = params[4]
            existing = world.stages.get((run_id, stage))
            if "= 'unresolved'" in normalized:  # the reconciliation statement
                if existing is not None and existing[0] == "unresolved":
                    world.stages[(run_id, stage)] = (state, receipt, checkpoint)
                    self._rows = [(run_id, stage, state)]
                else:
                    self._rows = []
            elif existing is not None and existing[0] in {"completed", "failed", "cancelled", "blocked", "unresolved"}:
                self._rows = []
            else:
                world.stages[(run_id, stage)] = (state, receipt, checkpoint)
                self._rows = [(run_id, stage, state)]
        elif normalized.startswith("SELECT COALESCE(MAX(seq), 0) + 1 FROM platform.run_event"):
            highest = max((item[1] for item in world.events if item[0] == params[0]), default=0)
            self._rows = [(highest + 1,)]
        elif normalized.startswith("INSERT INTO platform.run_event"):
            world.events.append(tuple(params))
            self._rows = []
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


class PlatformStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="platform-store-")
        self.addCleanup(self.directory.cleanup)
        self.delivery = Path(self.directory.name) / "sealed"
        self.delivery.mkdir()
        self.subject, self.artifacts, self.handoff = build_sealed_delivery(self.delivery)
        self.definition = {"source": {"bodies": [{"name": "base_link"}]}, "checks": {}}
        self.rebuilt = mock.Mock(return_value=self.definition)
        definition_patch = mock.patch.object(solidworks_urdf, "rebuilt_definition", new=self.rebuilt)
        definition_patch.start()
        self.addCleanup(definition_patch.stop)
        self.world = FakeWorld()
        self.store = PlatformStore(self.world.connect, sealed=lambda uri: self.delivery)

    # ------------------------------------------------------------- fixtures

    def qualified_quality(self, **overrides) -> dict:
        expected = required_checks(self.definition)
        checks = [{"id": name, "state": "passed", "passed": True, "details": {}} for name in expected]
        payload = {
            "passed": True,
            "subject_sha256": self.subject,
            "checks": checks,
            "required_checks": expected,
            # The qualification inventory is assembled by the same owner the
            # production report uses, with evidence bound to the sealed tree.
            "qualifications": solidworks_urdf.qualifications(self.delivery, self.definition, checks),
        }
        payload.update(overrides)
        return payload

    def fixed_only_quality(self) -> dict:
        fixed = sorted(set(CHECK_POLICY_V1["fixed_checks"]))
        return {
            "passed": True,
            "subject_sha256": self.subject,
            "checks": [{"id": name, "state": "passed", "passed": True, "details": {}} for name in fixed],
            "required_checks": fixed,
        }

    def generate_receipt(self, **overrides) -> dict:
        payload = {
            "schema_version": "solidworks-to-urdf.bundle/v1",
            "pipeline_id": "solidworks-to-urdf",
            "run_id": "r-1",
            "stage": "generate",
            "state": "generated",
            "passed": False,
            "subject_sha256": self.subject,
            "handoff_sha256": self.handoff,
            "artifacts": [dict(record) for record in self.artifacts],
        }
        payload.update(overrides)
        return payload

    def verify_receipt(self, **overrides) -> dict:
        payload = {
            "schema_version": "solidworks-to-urdf.bundle/v1",
            "pipeline_id": "solidworks-to-urdf",
            "run_id": "r-1",
            "stage": "verify",
            "state": "verified",
            "passed": True,
            "subject_sha256": self.subject,
            "handoff_sha256": self.handoff,
            "artifacts": [dict(record) for record in self.artifacts],
            "quality": self.qualified_quality(),
        }
        payload.update(overrides)
        return payload

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
            self.world.stages[("r-1", "verify")] = ("completed", self.verify_receipt(), CHECKPOINT)

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

    def _confirm_generate(self, **overrides):
        payload = {
            "run_id": "r-1",
            "stage": "generate",
            "state": "completed",
            "checkpoint_uri": CHECKPOINT,
            "receipt": self.generate_receipt(),
        }
        payload.update(overrides)
        return self.store.confirm_stage(**payload)

    def _unresolved_original(self):
        record = self.store.register_operation(
            run_id="r-1", stage="generate", kind="native", operation_key="r-1/native:0", params_digest="e" * 64
        )
        stored = self.world.operations["r-1/native:0"]
        self.world.operations["r-1/native:0"] = (stored[0], "unresolved", *stored[2:])
        return record

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
        self.world.hidden_finds = 2  # the winner commits between the identity read and the insert
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

    def test_a_stage_after_the_start_requires_a_fully_bound_predecessor(self) -> None:
        self._running_run()  # a fresh run starts at freeze; publish needs verify first
        with self.assertRaisesRegex(PlatformStoreError, "requires the completed 'verify' stage"):
            self._publish_registration()
        self.world.stages[("r-1", "verify")] = ("completed", {"run_id": "r-1", "stage": "verify"}, CHECKPOINT)
        with self.assertRaisesRegex(PlatformStoreError, "sealed"):
            self._publish_registration()
        self.world.stages[("r-1", "verify")] = ("completed", self.verify_receipt(), CHECKPOINT)
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

    def test_completed_confirmations_bind_the_whole_receipt(self) -> None:
        self._running_run()
        with self.assertRaisesRegex(PlatformStoreError, "state must be one of"):
            self.store.confirm_stage(run_id="r-1", stage="generate", state="generated")
        with self.assertRaisesRegex(PlatformStoreError, "sealed delivery subject"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="verify",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt={"run_id": "r-1", "stage": "verify", "state": "verified"},
            )
        with self.assertRaisesRegex(PlatformStoreError, "schema"):
            self.store.confirm_stage(
                run_id="r-1", stage="generate", state="failed", receipt={"run_id": "r-1", "stage": "generate"}
            )
        with self.assertRaisesRegex(PlatformStoreError, "confirmed receipt state"):
            self._confirm_generate(receipt=self.generate_receipt(state="failed"))
        with self.assertRaisesRegex(PlatformStoreError, "required checks"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="verify",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=self.verify_receipt(quality=self.qualified_quality(passed=False)),
            )
        with self.assertRaisesRegex(PlatformStoreError, "no checkpoint reference"):
            self._confirm_generate(checkpoint_uri=None)
        with self.assertRaisesRegex(PlatformStoreError, "artifact inventory"):
            self._confirm_generate(receipt=self.generate_receipt(artifacts=[]))
        self.store.confirm_stage(run_id="r-1", stage="generate", state="running")
        self._confirm_generate()
        with self.assertRaisesRegex(PlatformStoreError, "already terminal"):
            self._confirm_generate(state="failed", receipt=self.generate_receipt(state="failed", passed=False))
        self.store.confirm_stage(
            run_id="r-1",
            stage="verify",
            state="failed",
            receipt={**self.verify_receipt(), "state": "failed", "passed": False},
        )
        with self.assertRaisesRegex(PlatformStoreError, "already terminal"):
            self.store.confirm_stage(run_id="r-1", stage="verify", state="running")

    def test_a_completion_needs_the_real_sealed_bytes(self) -> None:
        self._running_run()
        self.rebuilt.reset_mock()
        unconfigured = PlatformStore(self.world.connect)
        with self.assertRaisesRegex(PlatformStoreError, "Sealed storage is not configured"):
            unconfigured.confirm_stage(
                run_id="r-1",
                stage="generate",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=self.generate_receipt(),
            )
        foreign = PlatformStore(self.world.connect, sealed=lambda uri: Path(self.directory.name) / "absent")
        with self.assertRaises(PipelineError):
            foreign.confirm_stage(
                run_id="r-1",
                stage="generate",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=self.generate_receipt(),
            )
        with self.assertRaisesRegex(PlatformStoreError, "artifact inventory"):
            self._confirm_generate(
                receipt=self.generate_receipt(
                    artifacts=[{"path": "model/robot.json", "bytes": 1, "sha256": "f" * 64}]
                )
            )
        with self.assertRaisesRegex(PlatformStoreError, "does not match the receipt"):
            self._confirm_generate(receipt=self.generate_receipt(subject_sha256="f" * 64))
        self._confirm_generate()
        # The definition rebuild belongs to the verify/publish quality binding.
        self.rebuilt.assert_not_called()

    def test_the_receipt_inventory_must_mirror_the_sealed_records_verbatim(self) -> None:
        self._running_run()
        tampered = [dict(record) for record in self.artifacts]
        tampered[0]["bytes"] = tampered[0]["bytes"] + 1
        with self.assertRaisesRegex(PlatformStoreError, "artifact inventory"):
            self._confirm_generate(receipt=self.generate_receipt(artifacts=tampered))
        extra = [dict(record) for record in self.artifacts[:1]] + [
            {"path": "meshes/extra.stl", "bytes": 1, "sha256": "e" * 64}
        ]
        with self.assertRaisesRegex(PlatformStoreError, "artifact inventory"):
            self._confirm_generate(receipt=self.generate_receipt(artifacts=extra))
        records = [dict(record) for record in self.artifacts]
        duplicated = [dict(record) for record in records]
        duplicated[1] = dict(duplicated[0])
        with self.assertRaisesRegex(PlatformStoreError, "artifact inventory"):
            self._confirm_generate(receipt=self.generate_receipt(artifacts=duplicated))
        tripled = [dict(records[0]) for _ in records]
        with self.assertRaisesRegex(PlatformStoreError, "artifact inventory"):
            self._confirm_generate(receipt=self.generate_receipt(artifacts=tripled))
        self._confirm_generate(receipt=self.generate_receipt(artifacts=list(reversed(records))))
        self.assertEqual(self.world.stages[("r-1", "generate")][0], "completed")

    def _native_receipt(self, stage: str, **overrides) -> dict:
        payload = {
            "schema_version": "solidworks-to-urdf.bundle/v1",
            "pipeline_id": "solidworks-to-urdf",
            "run_id": "r-1",
            "stage": stage,
            "state": NATIVE_STAGE_STATES[stage],
            "passed": False,
            "handoff_sha256": self.handoff,
            "artifacts": [dict(record) for record in self.artifacts],
        }
        if stage == "freeze":
            payload["tool"] = read_data(self.delivery / "tool.json")
        else:
            payload["native_tool"] = read_data(self.delivery / "reports" / "native-tool.json")
        payload.update(overrides)
        return payload

    def test_native_stages_bind_their_sealed_evidence_like_the_portable_ones(self) -> None:
        self._running_run()
        self.rebuilt.reset_mock()
        for stage in ("freeze", "discover", "capture"):
            with self.subTest(stage=stage):
                self.store.confirm_stage(
                    run_id="r-1",
                    stage=stage,
                    state="completed",
                    checkpoint_uri=CHECKPOINT,
                    receipt=self._native_receipt(stage),
                )
                self.assertEqual(self.world.stages[("r-1", stage)][0], "completed")
                self.assertIsNotNone(self.store.confirmed_stage("r-1", stage))
        self.rebuilt.assert_not_called()

    def test_native_receipts_without_their_bindings_are_refused(self) -> None:
        self._running_run()
        base = self._native_receipt("capture")
        without_tool = {key: value for key, value in base.items() if key != "native_tool"}
        with self.assertRaisesRegex(PlatformStoreError, "native tool"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="capture",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=without_tool,
            )
        with self.assertRaisesRegex(PlatformStoreError, "handoff"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="capture",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt={**base, "handoff_sha256": "not-a-sha256"},
            )
        with self.assertRaisesRegex(PlatformStoreError, "handoff"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="capture",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt={**base, "handoff_sha256": "f" * 64},
            )
        with self.assertRaisesRegex(PlatformStoreError, "confirmed receipt state"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="capture",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt={**base, "state": "bogus-outcome"},
            )
        with self.assertRaisesRegex(PlatformStoreError, "tool identity"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="capture",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt={**base, "native_tool": {"invented": True}},
            )
        with self.assertRaisesRegex(PlatformStoreError, "tool identity"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="freeze",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt={**self._native_receipt("freeze"), "tool": {"invented": True}},
            )
        index_sha = next(record["sha256"] for record in base["artifacts"] if record["path"] == "input/handoff.json")
        with self.assertRaisesRegex(PlatformStoreError, "handoff"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="capture",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt={**base, "handoff_sha256": index_sha},
            )
        trimmed = [record for record in base["artifacts"] if not record["path"].startswith("evidence/")]
        with self.assertRaisesRegex(PlatformStoreError, "artifact inventory"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="capture",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt={**base, "artifacts": trimmed},
            )
        with (
            mock.patch.object(store_module, "STAGE_EVIDENCE", {"capture": ("evidence/absent.json",)}),
            self.assertRaisesRegex(PlatformStoreError, "declared evidence"),
        ):
            self.store.confirm_stage(
                run_id="r-1",
                stage="capture",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=self._native_receipt("capture"),
            )
        with self.assertRaisesRegex(PlatformStoreError, "stage must be one of"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="pending",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt={**self._native_receipt("capture"), "stage": "pending"},
            )
        with self.assertRaisesRegex(PlatformStoreError, "stage must be one of"):
            self.store.confirmed_stage("r-1", "pending")

    def test_the_quality_inventory_binds_the_rebuilt_definition(self) -> None:
        self._running_run()
        # The report's own required list is never the authority: a fixed-only
        # subset is refused because the sealed tree rebuilds more gates.
        with self.assertRaisesRegex(PlatformStoreError, "required checks"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="verify",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=self.verify_receipt(quality=self.fixed_only_quality()),
            )
        self.rebuilt.assert_called_with(self.delivery)
        self.store.confirm_stage(
            run_id="r-1",
            stage="verify",
            state="completed",
            checkpoint_uri=CHECKPOINT,
            receipt=self.verify_receipt(),
        )
        self.assertEqual(self.world.stages[("r-1", "verify")][0], "completed")

    def test_the_successor_rebinding_runs_outside_the_registration_transaction(self) -> None:
        self._running_run()
        self.world.stages[("r-1", "generate")] = ("completed", self.generate_receipt(), CHECKPOINT)
        observed = []

        def sealing(uri):
            observed.append(self.world.open_transactions)
            return self.delivery

        store = PlatformStore(self.world.connect, sealed=sealing)
        record = store.register_operation(
            run_id="r-1",
            stage="verify",
            kind="dispatch",
            operation_key="r-1/verify:0",
            params_digest="d" * 64,
        )
        self.assertEqual(record.stage, "verify")
        self.assertIn("r-1/verify:0", self.world.operations)
        self.assertEqual(observed, [0], "the sealed rebinding runs with no open transaction")
        self.rebuilt.assert_not_called()

    def test_a_predecessor_that_changes_after_verification_is_refused(self) -> None:
        self._running_run()
        self.world.stages[("r-1", "generate")] = ("completed", self.generate_receipt(), CHECKPOINT)
        original = PlatformStore._rebind_predecessor

        def racing(store, **kwargs):
            binding = original(store, **kwargs)
            self.world.stages[("r-1", "generate")] = ("completed", self.generate_receipt(), "checkpoint-moved")
            return binding

        payload = {
            "run_id": "r-1",
            "stage": "verify",
            "kind": "dispatch",
            "operation_key": "r-1/verify:0",
            "params_digest": "d" * 64,
        }
        with (
            mock.patch.object(PlatformStore, "_rebind_predecessor", new=racing),
            self.assertRaisesRegex(PlatformStoreError, "changed after it was verified"),
        ):
            self.store.register_operation(**payload)
        self.assertNotIn("r-1/verify:0", self.world.operations)
        self.assertEqual(self.store.register_operation(**payload).stage, "verify")

    def test_a_duplicate_request_returns_the_operation_without_consuming_the_checkpoint(self) -> None:
        self._running_run()
        self.world.stages[("r-1", "generate")] = ("completed", self.generate_receipt(), CHECKPOINT)
        payload = {
            "run_id": "r-1",
            "stage": "verify",
            "kind": "dispatch",
            "operation_key": "r-1/verify:0",
            "params_digest": "d" * 64,
        }
        first = self.store.register_operation(**payload)
        before = len(self.world.operations)
        self.world.stages[("r-1", "generate")] = ("completed", self.generate_receipt(), "checkpoint-moved")
        guarded = PlatformStore(
            self.world.connect,
            sealed=lambda uri: self.fail("the operation identity lookup consumed the checkpoint"),
        )
        again = guarded.register_operation(**payload)
        self.assertEqual(again, first)
        self.assertEqual(len(self.world.operations), before, "the recorded side effect was not re-sent")
        with self.assertRaises(OperationConflictError):
            guarded.register_operation(**{**payload, "params_digest": "e" * 64})

    def test_run_display_rename_hide_restore_round_trip(self) -> None:
        self._running_run()
        self.world.run_display_rows["r-1"] = [None, None, None, NOW]

        renamed = self.store.set_run_display("r-1", "装配线 A")
        self.assertEqual(renamed.display_name, "装配线 A")
        self.assertIsNone(renamed.hidden_at)

        hidden = self.store.hide_run("r-1", actor="崔工")
        self.assertEqual(hidden.hidden_reason, "崔工")
        self.assertIsNotNone(hidden.hidden_at)
        repeat = self.store.hide_run("r-1", actor="someone else")
        self.assertEqual(repeat.hidden_at, hidden.hidden_at, "the first tombstone survives repeats")
        self.assertEqual(repeat.hidden_reason, "崔工")

        restored = self.store.restore_run("r-1")
        self.assertIsNone(restored.hidden_at)
        self.assertIsNone(restored.hidden_reason)
        self.assertEqual(restored.display_name, "装配线 A", "the display name survives the tombstone")
        self.assertIsNone(self.store.restore_run("r-1").hidden_at, "restore is idempotent")

        cleared = self.store.set_run_display("r-1", None)
        self.assertIsNone(cleared.display_name)
        self.assertNotIn("r-1", self.store.run_displays(), "a run without display data is not listed")

    def test_run_display_writes_refuse_a_run_without_a_platform_row(self) -> None:
        with self.assertRaisesRegex(RunNotAdmittedError, "is not admitted"):
            self.store.set_run_display("r-9", "x")
        with self.assertRaisesRegex(RunNotAdmittedError, "is not admitted"):
            self.store.hide_run("r-9", actor="崔工")
        with self.assertRaisesRegex(RunNotAdmittedError, "is not admitted"):
            self.store.restore_run("r-9")
        self.assertIsNone(self.store.run_display("r-9"))

    def test_run_displays_lists_only_runs_with_display_data(self) -> None:
        self.world.run_display_rows["r-2"] = ["备选", None, None, NOW]
        self.world.run_display_rows["r-3"] = [None, NOW, "崔工", NOW]
        self.world.run_display_rows["r-4"] = [None, None, None, NOW]
        listed = self.store.run_displays()
        self.assertEqual(sorted(listed), ["r-2", "r-3"])
        self.assertEqual(listed["r-2"].display_name, "备选")
        self.assertIsNotNone(listed["r-3"].hidden_at)

    def test_unresolved_is_confirmed_only_by_evidenced_reconciliation(self) -> None:
        self._running_run(from_stage="generate")
        self.store.confirm_stage(
            run_id="r-1",
            stage="generate",
            state="unresolved",
            receipt={**self.generate_receipt(), "state": "unresolved"},
        )
        with self.assertRaisesRegex(PlatformStoreError, "evidenced reconciliation"):
            self._confirm_generate()
        with self.assertRaisesRegex(PlatformStoreError, "must name its registered reconcile operation"):
            self._confirm_generate(reconcile={"result_ref": "unbound"})
        with self.assertRaisesRegex(PlatformStoreError, "registered reconcile operation"):
            self._confirm_generate(reconcile={"operation_id": "unregistered-op"})
        other_stage = self.store.register_operation(
            run_id="r-1", stage="verify", kind="reconcile", operation_key="r-1/reconcile:9", params_digest="c" * 64
        )
        with self.assertRaisesRegex(PlatformStoreError, "registered reconcile operation"):
            self._confirm_generate(reconcile={"operation_id": other_stage.operation_id})
        self._unresolved_original()
        reconcile_op = self.store.register_operation(
            run_id="r-1", stage="generate", kind="reconcile", operation_key="r-1/reconcile:0", params_digest="c" * 64
        )
        self._confirm_generate(reconcile={"operation_id": reconcile_op.operation_id})
        self.assertEqual(self.world.stages[("r-1", "generate")][0], "completed")
        self.assertEqual(
            self.world.stages[("r-1", "generate")][1]["reconciliation"],
            {"operation_id": reconcile_op.operation_id},
        )
        with self.assertRaisesRegex(PlatformStoreError, "already terminal"):
            self.store.confirm_stage(run_id="r-1", stage="generate", state="running")

    def test_reconciliation_requires_the_unresolved_original_operation(self) -> None:
        self._running_run(from_stage="generate")
        self.store.confirm_stage(
            run_id="r-1",
            stage="generate",
            state="unresolved",
            receipt={**self.generate_receipt(), "state": "unresolved"},
        )
        reconcile_op = self.store.register_operation(
            run_id="r-1", stage="generate", kind="reconcile", operation_key="r-1/reconcile:0", params_digest="c" * 64
        )
        with self.assertRaisesRegex(PlatformStoreError, "unresolved original operation"):
            self._confirm_generate(reconcile={"operation_id": reconcile_op.operation_id})

    def test_reconciliation_requires_an_existing_stage_record(self) -> None:
        self._running_run(from_stage="generate")
        self._unresolved_original()
        reconcile_op = self.store.register_operation(
            run_id="r-1", stage="generate", kind="reconcile", operation_key="r-1/reconcile:0", params_digest="c" * 64
        )
        with self.assertRaisesRegex(PlatformStoreError, "unresolved stage record"):
            self._confirm_generate(reconcile={"operation_id": reconcile_op.operation_id})

    def test_blocked_is_terminal_and_carries_no_receipt(self) -> None:
        self._running_run()
        self.store.confirm_stage(run_id="r-1", stage="generate", state="blocked")
        self.assertEqual(self.world.stages[("r-1", "generate")][0], "blocked")
        with self.assertRaisesRegex(PlatformStoreError, "already terminal"):
            self._confirm_generate()

    # ------------------------------------------------------- run progression

    def test_start_run_is_idempotent_and_refuses_terminal_or_unknown_runs(self) -> None:
        with self.assertRaisesRegex(PlatformStoreError, "not admitted"):
            self.store.start_run("r-1")
        self.admit()
        started = self.store.start_run("r-1")
        self.assertEqual(started, {"run_id": "r-1", "state": "running"})
        self.assertEqual(self.store.start_run("r-1"), started)
        self.world.run_states["r-1"] = "failed"
        with self.assertRaisesRegex(PlatformStoreError, "cannot start"):
            self.store.start_run("r-1")

    def test_a_failed_stage_ends_its_run_and_the_first_outcome_stands(self) -> None:
        self._running_run()
        self.store.confirm_stage(
            run_id="r-1", stage="generate", state="failed", receipt=self.generate_receipt(state="failed")
        )
        self.assertEqual(self.world.run_states["r-1"], "failed")
        self.store.confirm_stage(
            run_id="r-1",
            stage="verify",
            state="failed",
            receipt=self.verify_receipt(state="failed", passed=False),
        )
        self.assertEqual(self.world.run_states["r-1"], "failed")

    def test_a_confirmed_publication_ends_its_run_successfully(self) -> None:
        self._running_run()
        receipt = self.verify_receipt(
            stage="publish",
            state="published",
            passed=True,
            submission={
                "passed": True,
                "state": "published",
                "url": "https://example.test/repo/pull/1",
                "subject_sha256": self.subject,
            },
        )
        self.store.confirm_stage(
            run_id="r-1", stage="publish", state="completed", checkpoint_uri=CHECKPOINT, receipt=receipt
        )
        self.assertEqual(self.world.run_states["r-1"], "succeeded")

    def test_append_event_sequences_observed_events_and_refuses_unknown_runs(self) -> None:
        with self.assertRaisesRegex(PlatformStoreError, "not admitted"):
            self.store.append_event("r-1", stage="generate", kind="activity")
        self._running_run()
        self.store.append_event("r-1", stage="generate", kind="activity", payload={"seq": 1})
        self.store.append_event("r-1", stage="generate", kind="activity", payload={"seq": 2})
        sequences = [item[1] for item in self.world.events if item[0] == "r-1"]
        self.assertEqual(sequences, [1, 2])
        with self.assertRaisesRegex(PlatformStoreError, "event stage"):
            self.store.append_event("r-1", stage="nope", kind="activity")

    def test_run_and_stage_records_are_the_driver_reads(self) -> None:
        self._running_run()
        self._confirm_generate()
        record = self.store.run_record("r-1")
        self.assertEqual(record["state"], "running")
        self.assertEqual(record["request_id"], "q-1")
        stages = self.store.stage_records("r-1")
        self.assertEqual(stages["generate"]["state"], "completed")
        self.assertEqual(stages["generate"]["checkpoint_uri"], CHECKPOINT)
        self.assertEqual(stages["generate"]["receipt"]["stage"], "generate")
        self.assertIsNone(self.store.run_record("absent"))

    def test_confirmed_stage_returns_sealed_receipts_or_refuses(self) -> None:
        self._running_run()
        self.assertEqual(self.store.confirmed_stage("r-1", "generate"), None)
        self.store.confirm_stage(run_id="r-1", stage="generate", state="running")
        self.assertEqual(self.store.confirmed_stage("r-1", "generate"), None)
        receipt = self.generate_receipt()
        self._confirm_generate(receipt=receipt)
        self.assertEqual(self.store.confirmed_stage("r-1", "generate"), receipt)
        self.world.stages[("r-1", "generate")] = ("completed", {**receipt, "run_id": "r-2"}, CHECKPOINT)
        with self.assertRaisesRegex(PlatformStoreError, "another run or stage"):
            self.store.confirmed_stage("r-1", "generate")
        self.world.stages[("r-1", "generate")] = ("completed", receipt, None)
        with self.assertRaisesRegex(PlatformStoreError, "no checkpoint reference"):
            self.store.confirmed_stage("r-1", "generate")

    # ------------------------------------------------------------------ reads

    def test_reads_parse_the_two_projections(self) -> None:
        rows = self.store.lifecycle_rows()
        self.assertEqual([row["object_kind"] for row in rows], ["run", "scheduling"])
        self.assertEqual(rows[1]["bucket"], "in_flight")
        self.assertEqual(self.store.admission().state, "open")

    def test_confirm_stage_appends_the_event_in_the_same_transaction(self) -> None:
        self._running_run()
        transactions = self.world.transactions
        self._confirm_generate()
        self.assertEqual(self.world.transactions - transactions, 1, "state, receipt and event share one transaction")
        self.assertEqual(len(self.world.events), 1)
        run_id, sequence, kind, stage, operation_id, payload = self.world.events[0]
        self.assertEqual((run_id, sequence, kind, stage), ("r-1", 1, "stage", "generate"))
        self.assertIsNone(operation_id)
        self.assertEqual(payload, '{"state":"completed"}')
        self.store.confirm_stage(run_id="r-1", stage="verify", state="pending", checkpoint_uri=None)
        self.assertEqual(self.world.events[1][1], 2, "the sequence is assigned under the run lock")

    def test_the_environment_factory_needs_the_derived_service_dsn(self) -> None:
        with self.assertRaisesRegex(PlatformStoreError, "PLATFORM_SERVICE_DSN"):
            PlatformStore.from_environment({})
        store = PlatformStore.from_environment(
            {"PLATFORM_SERVICE_DSN": "postgresql://service@/platform?host=/sock"},
            sealed=lambda uri: self.delivery,
        )
        self.assertIsInstance(store, PlatformStore)

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
