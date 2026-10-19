"""Application-side platform store API: admission, transitions, stages, reads.

The fake world mirrors the deploy side's statements at the SQL level; the
sealed resolver points at a real on-disk delivery so receipt bytes are rebound
for real.  Real-PG atomicity, locking and recovery are proven by the deploy
side's battery, not claimed by these unit tests.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from description_pipeline.delivery import PIPELINE_ID, read_delivery_manifest, write_delivery_manifest
from description_pipeline.interface_records import (
    CACHE_IMPORT_CONFIRMATION_SCHEMA,
    CACHE_RELEASE_RECEIPT_SCHEMA,
    CHECK_POLICY_SCHEMA,
    CHECK_POLICY_V1,
    cache_import_confirmation_digest,
    cache_release_params_digest,
    cache_release_receipt_digest,
    check_policy_digest,
)
from description_pipeline.io import PipelineError, digest, file_digest, read_data, write_json
from description_pipeline.platform import store as store_module
from description_pipeline.platform.store import (
    AdmissionClosedError,
    OpenAttemptConflictError,
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


def build_sealed_delivery(root: Path) -> tuple[str, list[dict], str, str]:
    """One real sealed delivery: manifest record-for-record plus its digests.

    The tree covers the declared evidence of all six stages, so the same
    delivery serves the portable and the native confirmations. The digests
    mirror the stage identity domains: the full prepared-index digest that
    freeze binds, and the selected native transfer digest bound by every
    later stage.
    """

    for name in ("input", "evidence", "model", "urdf", "meshes", "reports"):
        (root / name).mkdir(parents=True)
    (root / "input" / "records").mkdir()
    (root / "evidence" / "geometry").mkdir()
    write_json(root / "input" / "source.json", {"source": "fixture"})
    write_json(root / "input" / "records" / "arm.json", {"record": "fixture"})
    (root / "input" / "cad" / "cad").mkdir(parents=True)
    (root / "input" / "cad" / "cad" / "robot.SLDASM").write_bytes(b"fixture-native")
    package_files = {
        "source.json": file_digest(root / "input" / "source.json"),
        "records/arm.json": file_digest(root / "input" / "records" / "arm.json"),
        "cad/cad/robot.SLDASM": file_digest(root / "input" / "cad" / "cad" / "robot.SLDASM"),
    }
    native_inventory = {"cad/robot.SLDASM": package_files["cad/cad/robot.SLDASM"]}
    write_json(
        root / "input" / "handoff.json",
        {
            "passed": True,
            "package_files": package_files,
            "input_receipt": {"inventory": native_inventory},
        },
    )
    write_json(
        root / "evidence" / "discovery.json",
        {
            "discovery": "fixture",
            "handoff_sha256": digest(native_inventory),
            "native_files": native_inventory,
        },
    )
    write_json(root / "evidence" / "capture-plan.json", {"plan": "fixture"})
    write_json(root / "evidence" / "capture.json", {"capture": "fixture"})
    write_json(root / "evidence" / "native-runtime.json", {"runtime": "fixture"})
    (root / "evidence" / "geometry" / "part.stl").write_bytes(b"native-stl")
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
    write_json(root / "transfer-manifest.json", {"handoff_sha256": digest(native_inventory)})
    from description_pipeline.request_evidence import build_request_evidence, write_request_evidence

    write_request_evidence(
        root / "input" / "request.json",
        build_request_evidence(
            purpose="engineering",
            request_id="fixture-sealed-delivery",
            target_hardware="nd",
            change_summary="fixture",
            main_assembly="cad/robot.SLDASM",
            handoff_digest=digest(package_files),
            platform_config_digest="c" * 64,
        ),
    )
    write_delivery_manifest(
        root,
        contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
        policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
    )
    manifest = read_delivery_manifest(root)
    artifacts = [dict(record) for record in manifest["subject"]["files"]]
    handoff_full = digest(package_files)
    handoff_native = digest(native_inventory)
    return manifest["subject"]["digest"], artifacts, handoff_full, handoff_native


class FakeWorld:
    """A tiny in-memory stand-in for the deploy side's schema and two views."""

    _ROLLBACK_FIELDS = (
        "admission",
        "requests",
        "runs",
        "run_states",
        "run_from_stage",
        "operations",
        "operation_bindings",
        "task_instances",
        "task_try_numbers",
        "dag_runs",
        "stages",
        "events",
        "confirmed_publishes",
        "lifecycle_rows",
        "run_display_rows",
    )

    def snapshot_state(self) -> dict:
        import copy as _copy

        return {name: _copy.deepcopy(getattr(self, name)) for name in self._ROLLBACK_FIELDS}

    def restore_state(self, state: dict) -> None:
        for name, value in state.items():
            setattr(self, name, value)

    def __init__(self, *, admission=("open", "window", "operator", NOW, 1)):
        self.admission = admission
        self.requests: list[tuple] = []
        self.runs: list[tuple] = []
        self.run_states: dict[str, str] = {}
        self.run_from_stage: dict[str, str | None] = {}
        self.operations: dict[str, tuple] = {}
        #: operation_id -> (dag_id, dag_run_id, task_id, map_index); the view's join key.
        self.operation_bindings: dict[str, tuple] = {}
        #: (dag_id, task_id, dag_run_id, map_index) -> task_instance state.
        self.task_instances: dict[tuple[str, str, str, int], str] = {}
        #: (dag_id, task_id, dag_run_id, map_index) -> terminal try number.
        self.task_try_numbers: dict[tuple[str, str, str, int], int] = {}
        #: (dag_id, dag_run_id) -> dag_run state; the view's DagRun-settled source.
        self.dag_runs: dict[tuple[str, str], str] = {}
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
        self._undo = self.world.snapshot_state()
        return self

    def __exit__(self, *exc):
        self.world.open_transactions -= 1
        if exc[0] is not None:
            # A refused transaction rolls back exactly like the real database: the
            # world is restored and only the statement log and test instruments stay.
            self.world.restore_state(self._undo)
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
        if "FROM admission WHERE id = 1 FOR UPDATE" in normalized:
            self._rows = [world.admission] if world.admission is not None else []
        elif "UPDATE admission" in normalized:
            state, reason, actor = params
            world.admission = (state, reason, actor, NOW, world.admission[4] + 1)
            self._rows = [world.admission]
        elif "FROM admission_projection" in normalized:
            self._rows = [world.admission] if world.admission is not None else []
        elif normalized.startswith("INSERT INTO request"):
            world.requests.append(tuple(params))
            self._rows = []
        elif normalized.startswith("INSERT INTO run ("):
            world.runs.append(tuple(params))
            world.run_states[params[0]] = "queued"
            world.run_from_stage[params[0]] = params[3]
            self._rows = []
        elif normalized.startswith("UPDATE run SET state = 'running'"):
            if "state = 'unresolved'" in normalized:
                if world.run_states.get(params[0]) == "unresolved":
                    world.run_states[params[0]] = "running"
                    self._rows = [("running",)]
                else:
                    self._rows = []
            elif world.run_states.get(params[0]) == "queued":
                world.run_states[params[0]] = "running"
                self._rows = [("running",)]
            else:
                self._rows = []
        elif normalized.startswith("UPDATE run SET state = 'unresolved'"):
            if world.run_states.get(params[0]) == "running":
                world.run_states[params[0]] = "unresolved"
            self._rows = []
        elif normalized.startswith("UPDATE run SET state = %s"):
            outcome, run_id = params
            if world.run_states.get(run_id) not in {"succeeded", "failed", "cancelled", None}:
                world.run_states[run_id] = outcome
            self._rows = []
        elif normalized.startswith("SELECT run_id, request_id FROM run WHERE parent_run_id"):
            parent = params[0]
            open_child = next(
                (
                    row
                    for row in world.runs
                    if row[2] == parent and world.run_states.get(row[0]) not in {"succeeded", "failed", "cancelled"}
                ),
                None,
            )
            self._rows = [(open_child[0], open_child[1])] if open_child is not None else []
        elif normalized.startswith("SELECT principal, target_hardware, main_assembly"):
            entry = next((row for row in world.requests if row[0] == params[0]), None)
            if entry is None:
                self._rows = []
            else:
                run_ids = {row[0] for row in world.runs if row[1] == entry[0]}
                dispatch_digest = next(
                    (
                        record[3]
                        for record in world.operations.values()
                        if record[4] in run_ids and record[6] == "dispatch"
                    ),
                    None,
                )
                configuration = entry[4]
                if isinstance(configuration, str):
                    configuration = json.loads(configuration)
                baseline = entry[5] if len(entry) > 5 else None
                if isinstance(baseline, str):
                    baseline = json.loads(baseline)
                self._rows = [(entry[1], entry[2], entry[3], NOW, dispatch_digest, configuration, baseline)]
        elif normalized.startswith(
            "SELECT state, request_id, parent_run_id, from_stage, tool_identity FROM run WHERE run_id = %s"
        ):
            entry = next((row for row in world.runs if row[0] == params[0]), None)
            if entry is None:
                self._rows = []
            else:
                state = world.run_states.get(entry[0])
                self._rows = [(state, entry[1], entry[2], entry[3], entry[4])]
        elif normalized.startswith(
            "SELECT stage, state, receipt, checkpoint_uri, reused_from_run, original_executed_at"
            " FROM stage WHERE run_id = %s"
        ):
            self._rows = [
                (
                    stage,
                    record[0],
                    record[1],
                    record[2],
                    record[3] if len(record) > 3 else None,
                    record[4] if len(record) > 4 else None,
                )
                for (run_id, stage), record in world.stages.items()
                if run_id == params[0]
            ]
        elif normalized.startswith("INSERT INTO operation"):
            key = params[4]
            if key in world.operations:
                self._rows = []
            else:
                record = (params[0], "queued", key, params[5], params[1], params[2], params[3])
                world.operations[key] = record
                world.operation_bindings[params[0]] = (params[6], params[7], params[8], params[9])
                self._rows = [record]
        elif normalized.startswith("SELECT dag_id, dag_run_id FROM operation WHERE operation_id"):
            operation_id, run_id = params
            bound = None
            for record in world.operations.values():
                if record[0] != operation_id or record[4] != run_id:
                    continue
                dag_id, dag_run_id = world.operation_bindings.get(operation_id, (None, None))[:2]
                bound = (dag_id, dag_run_id)
                break
            self._rows = [bound] if bound is not None else []
        elif normalized.startswith(
            "SELECT stage, kind, state, dag_id, dag_run_id, task_id, map_index, operation_key"
            " FROM operation WHERE operation_id"
        ):
            operation_id, run_id = params
            bound = None
            for key, record in world.operations.items():
                if record[0] != operation_id or record[4] != run_id:
                    continue
                dag_id, dag_run_id, task_id, map_index = world.operation_bindings.get(
                    operation_id, (None, None, None, None)
                )
                bound = (record[5], record[6], record[1], dag_id, dag_run_id, task_id, map_index, key)
                break
            self._rows = [bound] if bound is not None else []
        elif normalized.startswith(
            "UPDATE operation SET task_id = %s, map_index = %s, updated_at = now()"
            " WHERE operation_id = %s AND run_id = %s AND task_id IS NULL AND map_index IS NULL"
        ):
            task_id, map_index, operation_id, run_id = params
            updated = None
            for record in world.operations.values():
                if record[0] != operation_id or record[4] != run_id:
                    continue
                dag_id, dag_run_id, current_task, current_map = world.operation_bindings.get(
                    operation_id, (None, None, None, None)
                )
                if current_task is None and current_map is None:
                    world.operation_bindings[operation_id] = (dag_id, dag_run_id, task_id, map_index)
                    updated = (operation_id,)
                break
            self._rows = [updated] if updated is not None else []
        elif normalized.startswith(
            "SELECT object_kind, state, bucket FROM lifecycle_projection WHERE (object_kind = 'scheduling'"
        ):
            # The one statement that carries every lifecycle fact of an orphan acceptance.
            operation_id, dag_run_object = params
            rows = []
            state = None
            for record in world.operations.values():
                if record[0] != operation_id:
                    continue
                dag_id, dag_run_id, task_id, map_index = world.operation_bindings.get(
                    operation_id, (None, None, None, None)
                )
                if None not in (dag_id, dag_run_id, task_id, map_index):
                    state = world.task_instances.get((dag_id, task_id, dag_run_id, map_index))
                break
            if state in {
                "queued",
                "running",
                "scheduled",
                "deferred",
                "up_for_retry",
                "up_for_reschedule",
                "restarting",
            }:
                bucket = "in_flight"
            elif state in {"success", "failed", "skipped", "upstream_failed", "removed"}:
                bucket = "terminal"
            else:
                bucket = "unknown"
            rows.append(("scheduling", state, bucket))
            for (dag_id, dag_run_id), dag_state in world.dag_runs.items():
                if f"{dag_id}/{dag_run_id}" != dag_run_object:
                    continue
                if dag_state in {"success", "failed"}:
                    dag_bucket = "terminal"
                elif dag_state in {"queued", "running"}:
                    dag_bucket = "in_flight"
                else:
                    dag_bucket = "unknown"
                rows.append(("dag_run", dag_state, dag_bucket))
                break
            self._rows = rows
        elif normalized.startswith("SELECT state, bucket FROM lifecycle_projection"):
            (object_id,) = params
            state = None
            found = False
            for record in world.operations.values():
                if record[0] != object_id:
                    continue
                found = True
                dag_id, dag_run_id, task_id, map_index = world.operation_bindings.get(
                    object_id, (None, None, None, None)
                )
                if None not in (dag_id, dag_run_id, task_id, map_index):
                    state = world.task_instances.get((dag_id, task_id, dag_run_id, map_index))
                break
            if state in {
                "queued",
                "running",
                "scheduled",
                "deferred",
                "up_for_retry",
                "up_for_reschedule",
                "restarting",
            }:
                bucket = "in_flight"
            elif state in {"success", "failed", "skipped", "upstream_failed", "removed"}:
                bucket = "terminal"
            else:
                bucket = "unknown"
            self._rows = [(state, bucket)] if found else []
        elif normalized.startswith(
            "SELECT operation_id, state, operation_key, params_digest, run_id, stage, kind"
            " FROM operation WHERE operation_key"
        ):
            record = world.operations.get(params[0])
            if world.hidden_finds:
                world.hidden_finds -= 1
                record = None
            self._rows = [record] if record else []
        elif normalized.startswith(
            "SELECT operation_id, state, operation_key, params_digest, run_id, stage, kind,"
            " dag_id, dag_run_id, task_id, map_index FROM operation WHERE operation_key"
        ):
            record = world.operations.get(params[0])
            if record is None:
                self._rows = []
            else:
                binding = world.operation_bindings.get(record[0], (None, None, None, None))
                self._rows = [(*record, *binding)]
        elif normalized.startswith(
            "UPDATE operation SET task_id = %s, map_index = %s, updated_at = now() WHERE operation_key"
        ):
            task_id, map_index, key, run_id, dag_id, dag_run_id = params
            record = world.operations.get(key)
            binding = world.operation_bindings.get(record[0], (None, None, None, None)) if record else None
            if (
                record is not None
                and record[4] == run_id
                and record[6] == "dispatch"
                and record[1] == "queued"
                and binding == (dag_id, dag_run_id, None, None)
            ):
                world.operation_bindings[record[0]] = (dag_id, dag_run_id, task_id, map_index)
                self._rows = [(record[0],)]
            else:
                self._rows = []
        elif normalized.startswith(
            "SELECT operation_id, stage, state, dag_id, dag_run_id, task_id, map_index, operation_key"
            " FROM operation WHERE run_id = %s AND kind = 'dispatch'"
        ):
            rows = []
            for key, record in world.operations.items():
                if record[4] != params[0] or record[6] != "dispatch":
                    continue
                binding = world.operation_bindings.get(record[0], (None, None, None, None))
                rows.append((record[0], record[5], record[1], *binding, key))
            self._rows = rows
        elif normalized.startswith(
            "SELECT ti.map_index, ti.state, ti.try_number, dr.state FROM task_instance ti JOIN dag_run dr"
        ):
            dag_id, dag_run_id, task_id = params
            dag_state = world.dag_runs.get((dag_id, dag_run_id))
            self._rows = [
                (map_index, task_state, world.task_try_numbers.get((dag_id, task_id, dag_run_id, map_index)), dag_state)
                for (row_dag, row_task, row_run, map_index), task_state in world.task_instances.items()
                if row_dag == dag_id and row_task == task_id and row_run == dag_run_id and dag_state is not None
            ]
        elif normalized.startswith(
            "UPDATE operation SET task_id = %s, map_index = %s, updated_at = now() WHERE operation_id"
        ):
            task_id, map_index, operation_id, run_id = params
            record = next((r for r in world.operations.values() if r[0] == operation_id and r[4] == run_id), None)
            binding = world.operation_bindings.get(operation_id, (None, None, None, None))
            if record is not None and record[6] == "dispatch" and binding[2:] == (None, None):
                world.operation_bindings[operation_id] = (binding[0], binding[1], task_id, map_index)
                self._rows = [(operation_id,)]
            else:
                self._rows = []
        elif "SELECT state, from_stage FROM run WHERE run_id = %s" in normalized:
            state = world.run_states.get(params[0])
            self._rows = [(state, world.run_from_stage.get(params[0]))] if state is not None else []
        elif normalized.startswith("SELECT run_id, display_name, hidden_at, hidden_reason, updated_at FROM run"):
            if "WHERE run_id = %s" in normalized:
                row = world.run_display_rows.get(params[0])
                self._rows = [(params[0], *row)] if row is not None else []
            else:
                self._rows = [
                    (run_id, *row)
                    for run_id, row in world.run_display_rows.items()
                    if row[0] is not None or row[1] is not None
                ]
        elif normalized.startswith("UPDATE run SET display_name"):
            row = world.run_display_rows.get(params[1])
            if row is not None:
                row[0] = params[0]
                row[3] = NOW
                self._rows = [(params[1], *row)]
            else:
                self._rows = []
        elif normalized.startswith("UPDATE run SET hidden_at = COALESCE"):
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
        elif normalized.startswith("UPDATE run SET hidden_at = NULL"):
            row = world.run_display_rows.get(params[0])
            if row is not None:
                row[1] = None
                row[2] = None
                row[3] = NOW
                self._rows = [(params[0], *row)]
            else:
                self._rows = []
        elif "FROM operation WHERE run_id = %s AND kind = 'publish' AND state = 'succeeded'" in normalized:
            self._rows = [(1,)] if (params[0], "publish") in world.confirmed_publishes else []
        elif (
            "FROM operation WHERE operation_id = %s AND run_id = %s AND kind = 'reconcile' AND stage = %s" in normalized
        ):
            found = any(
                item[0] == params[0] and item[4] == params[1] and item[6] == "reconcile" and item[5] == params[2]
                for item in world.operations.values()
            )
            self._rows = [(1,)] if found else []
        elif (
            "FROM operation WHERE run_id = %s AND stage = %s AND kind <> 'reconcile' AND state = 'unresolved'"
        ) in normalized:
            found = any(
                item[4] == params[0] and item[5] == params[1] and item[6] != "reconcile" and item[1] == "unresolved"
                for item in world.operations.values()
            )
            self._rows = [(1,)] if found else []
        elif normalized.startswith("SELECT count(*) FROM stage WHERE run_id = %s"):
            self._rows = [(sum(1 for (run_id, _stage) in world.stages if run_id == params[0]),)]
        elif normalized.startswith(
            "SELECT operation_id, stage, kind, state, dag_id, dag_run_id, task_id, map_index, operation_key"
            " FROM operation WHERE run_id = %s ORDER BY operation_id FOR UPDATE"
        ):
            rows = []
            for key, record in sorted(world.operations.items()):
                if record[4] != params[0]:
                    continue
                dag_id, dag_run_id, task_id, map_index = world.operation_bindings.get(
                    record[0], (None, None, None, None)
                )
                rows.append((record[0], record[5], record[6], record[1], dag_id, dag_run_id, task_id, map_index, key))
            self._rows = rows
        elif normalized.startswith(
            "SELECT 'task'::text, ti.task_id, ti.map_index, ti.state, ti.try_number FROM task_instance"
        ):
            dag_id, run_id = params[0], params[1]
            rows = []
            for (task_dag, task_id, task_run, map_index), state in world.task_instances.items():
                if (
                    task_dag == dag_id
                    and task_run == run_id
                    and task_id
                    in {
                        "freeze",
                        "discover",
                        "capture",
                        "generate",
                        "verify",
                        "publish",
                    }
                ):
                    rows.append(
                        (
                            "task",
                            task_id,
                            map_index,
                            state,
                            world.task_try_numbers.get((task_dag, task_id, task_run, map_index), 0),
                        )
                    )
            dag_run_state = world.dag_runs.get((dag_id, run_id))
            if dag_run_state is not None:
                rows.append(("dag_run", None, None, dag_run_state, None))
            self._rows = rows
        elif normalized.startswith("SELECT state FROM stage WHERE run_id = %s AND stage = %s FOR UPDATE"):
            record = world.stages.get((params[0], params[1]))
            self._rows = [(record[0],)] if record else []
        elif normalized.startswith(
            "SELECT state, receipt, checkpoint_uri, reused_from_run FROM stage WHERE run_id = %s AND stage = %s"
        ):
            record = world.stages.get((params[0], params[1]))
            self._rows = [(record[0], record[1], record[2], record[3] if len(record) > 3 else None)] if record else []
        elif normalized.startswith("SELECT original_executed_at FROM stage WHERE run_id = %s AND stage = %s"):
            record = world.stages.get((params[0], params[1]))
            self._rows = [(record[4] if len(record) > 4 else None,)] if record else []
        elif normalized.startswith("INSERT INTO stage"):
            run_id, stage, state = params[0], params[1], params[2]
            receipt = json.loads(params[3]) if params[3] is not None else None
            checkpoint = params[4]
            existing = world.stages.get((run_id, stage))
            if "= 'unresolved'" in normalized:  # the reconciliation statement
                if existing is not None and existing[0] == "unresolved":
                    world.stages[(run_id, stage)] = (
                        state,
                        receipt,
                        checkpoint,
                        params[5] if len(params) > 5 else None,
                        params[6] if len(params) > 6 else None,
                    )
                    self._rows = [(run_id, stage, state)]
                else:
                    self._rows = []
            elif existing is not None and existing[0] in {"completed", "failed", "cancelled", "blocked", "unresolved"}:
                self._rows = []
            else:
                world.stages[(run_id, stage)] = (
                    state,
                    receipt,
                    checkpoint,
                    params[5] if len(params) > 5 else None,
                    params[6] if len(params) > 6 else None,
                )
                self._rows = [(run_id, stage, state)]
        elif normalized.startswith("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_event"):
            highest = max((item[1] for item in world.events if item[0] == params[0]), default=0)
            self._rows = [(highest + 1,)]
        elif normalized.startswith("INSERT INTO run_event"):
            world.events.append(tuple(params))
            self._rows = []
        elif normalized.startswith("UPDATE operation SET state = 'running'"):
            key = params[0]
            record = world.operations.get(key)
            if record is not None and record[1] == "queued" and record[4] == params[1] and record[5] == params[2]:
                updated = (record[0], "running", record[2], record[3], record[4], record[5], record[6])
                world.operations[key] = updated
                self._rows = [updated]
            else:
                self._rows = []
        elif normalized.startswith("UPDATE operation SET state = CASE WHEN kind = 'cancel'"):
            outcome, run_id, stage = params
            for key, record in list(world.operations.items()):
                if (
                    record[4] == run_id
                    and record[5] == stage
                    and record[6] not in {"reconcile", "cache_release"}
                    and record[1] in {"queued", "running", "unresolved"}
                ):
                    settled = "succeeded" if record[6] == "cancel" else outcome
                    world.operations[key] = (record[0], settled, *record[2:])
            self._rows = []
        elif normalized.startswith("UPDATE operation SET state = 'cancelled', updated_at = now()"):
            run_id, stage = params
            for key, record in list(world.operations.items()):
                if (
                    record[4] == run_id
                    and record[5] == stage
                    and record[6] not in {"reconcile", "cache_release"}
                    and record[1] == "queued"
                ):
                    world.operations[key] = (record[0], "cancelled", *record[2:])
            self._rows = []
        elif normalized.startswith("UPDATE operation SET state = 'unresolved'"):
            run_id, stage = params
            for key, record in list(world.operations.items()):
                if (
                    record[4] == run_id
                    and record[5] == stage
                    and record[6] not in {"reconcile", "cache_release"}
                    and record[1] == "running"
                ):
                    world.operations[key] = (record[0], "unresolved", *record[2:])
            self._rows = []
        elif normalized.startswith("UPDATE operation SET state = 'succeeded'"):
            operation_id, run_id, stage = params
            for key, record in list(world.operations.items()):
                if (
                    record[0] == operation_id
                    and record[4] == run_id
                    and record[5] == stage
                    and record[6] == "reconcile"
                    and record[1] != "succeeded"
                ):
                    world.operations[key] = (record[0], "succeeded", *record[2:])
            self._rows = []
        elif normalized.startswith(
            "UPDATE operation SET state = %s, updated_at = now() WHERE operation_id = %s AND run_id = %s"
        ):
            settled, operation_id, run_id = params
            for key, record in list(world.operations.items()):
                if (
                    record[0] == operation_id
                    and record[4] == run_id
                    and record[1] == "queued"
                    and record[6] not in {"reconcile", "cache_release"}
                ):
                    world.operations[key] = (record[0], settled, *record[2:])
                    break
            self._rows = []
        elif normalized.startswith("UPDATE operation SET state = %s, updated_at = now() WHERE operation_key"):
            state, key, run_id, stage = params
            record = world.operations.get(key)
            if (
                record is not None
                and record[6] == "cache_release"
                and record[4] == run_id
                and record[5] == stage
                and record[1] == "running"
            ):
                updated = (record[0], state, *record[2:])
                world.operations[key] = updated
                self._rows = [updated]
            else:
                self._rows = []
        elif normalized.startswith("SELECT seq, at, kind, stage, operation_id, payload FROM run_event"):
            run_id, after_seq, limit = params
            self._rows = [
                (
                    item[1],
                    NOW,
                    item[2],
                    item[3],
                    item[4],
                    json.loads(item[5]) if isinstance(item[5], str) else item[5],
                )
                for item in world.events
                if item[0] == run_id and item[1] > after_seq
            ][:limit]
        elif normalized.startswith("SELECT parent_run_id FROM run WHERE run_id = %s"):
            entry = next((row for row in world.runs if row[0] == params[0]), None)
            self._rows = [(entry[2],)] if entry is not None else []
        elif "FROM lifecycle_projection" in normalized:
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
        self.subject, self.artifacts, self.handoff_full, self.handoff_native = build_sealed_delivery(self.delivery)
        self.definition = {"source": {"bodies": [{"name": "base_link"}]}, "checks": {}}
        # The sealed delivery's own report is the qualified payload the receipts
        # carry inline: the completed agreement gate compares the two, so the
        # fixture must pair them (reports/ stays outside the manifest subject).
        write_json(self.delivery / "reports" / "quality.json", self.qualified_quality())
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
            "handoff_sha256": self.handoff_native,
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
            "handoff_sha256": self.handoff_native,
            "artifacts": [dict(record) for record in self.artifacts],
            "quality": self.qualified_quality(),
        }
        payload.update(overrides)
        return payload

    def sealed_report_member(self) -> dict:
        """The registered byte-bound report member of this fixture's sealed tree."""

        path = self.delivery / "reports" / "quality.json"
        return {"path": "reports/quality.json", "bytes": path.stat().st_size, "sha256": file_digest(path)}

    def registered_verify_receipt(self, **overrides) -> dict:
        payload = {"artifacts": [*self.artifacts, self.sealed_report_member()]}
        payload.update(overrides)
        return self.verify_receipt(**payload)

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
            self.world.stages[("r-1", "verify")] = (
                "completed",
                self.registered_verify_receipt(),
                CHECKPOINT,
            )

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
        """One pre-existing original operation that already went unknown.

        The row is seeded directly: a production run registers its intent
        before the side effect, so a fresh registration after the stage is
        already unresolved is refused by the store guard - exactly what the
        real write path enforces.
        """

        key = "r-1/native:0"
        self.world.operations[key] = ("op-native-0", "unresolved", key, "e" * 64, "r-1", "generate", "native")

    @staticmethod
    def _release_import_block(name: str) -> dict:
        production = {"run_id": "native-" + name, "operation_id": "production-" + name}
        confirmation = {
            "schema_version": CACHE_IMPORT_CONFIRMATION_SCHEMA,
            "imported": [{"path": "cad/synthetic.SLDASM", "bytes": 1, "sha256": "e" * 64}],
            "production": production,
            "sealed": {"confirmed_at": "2026-10-15T17:00:00Z", "receipt_sha256": "f" * 64},
        }
        confirmation["digest"] = cache_import_confirmation_digest(confirmation)
        return {"linux_run_id": name, "production": production, "confirmation": confirmation}

    @staticmethod
    def _release_receipt(operation_id: str, imported: dict) -> dict:
        value = {
            "schema_version": CACHE_RELEASE_RECEIPT_SCHEMA,
            "status": "released",
            "operation_id": operation_id,
            "params_sha256": cache_release_params_digest(imported),
            "import": imported,
            "released": {"files": 1, "bytes": 1},
            "retained": ["terminal_state", "import_confirmation"],
            "released_at": "2026-10-15T17:00:01Z",
            "identity": {
                "version": "1.4.0",
                "source_commit": "a" * 40,
                "source_sha256": "b" * 64,
                "role": "native",
            },
        }
        value["receipt_sha256"] = cache_release_receipt_digest(value)
        return value

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

    def test_request_record_reads_the_sealed_admission_identity(self) -> None:
        self.admit(configuration={"purpose": "engineering"})
        self.assertEqual(
            self.store.request_record("q-1"),
            {
                "request_id": "q-1",
                "principal": "portal",
                "target_hardware": "nd",
                "main_assembly": "cad/robot.SLDASM",
                "sealed_at": NOW,
                "dispatch_digest": "a" * 64,
                "purpose": "engineering",
                "baseline": None,
            },
        )
        self.assertIsNone(self.store.request_record("q-missing"))

    def test_a_parent_holds_at_most_one_open_linked_attempt(self) -> None:
        self.admit()  # the parent r-1

        def child(run_id: str, request_id: str, digest: str) -> None:
            self.admit(
                run_id=run_id,
                request_id=request_id,
                parent_run_id="r-1",
                from_stage="capture",
                dispatch={"stage": "capture", "operation_key": f"{run_id}/capture:0", "params_digest": digest},
            )

        child("r-child-1", "q-child-1", "c" * 64)
        with self.assertRaises(OpenAttemptConflictError) as raised:
            child("r-child-2", "q-child-2", "d" * 64)
        self.assertEqual(raised.exception.parent_run_id, "r-1")
        self.assertEqual(raised.exception.run_id, "r-child-1")
        self.assertEqual(raised.exception.request_id, "q-child-1")
        # The refused admission left nothing behind.
        self.assertIsNone(self.store.run_record("r-child-2"))
        self.assertIsNone(self.store.request_record("q-child-2"))
        # A different parent stays free.
        self.admit(
            run_id="r-other",
            request_id="q-other",
            dispatch={"stage": "capture", "operation_key": "r-other/capture:0", "params_digest": "e" * 64},
        )
        # Only a non-terminal child reserves the parent: once it is terminal, a new
        # linked attempt is admitted.
        self.store.confirm_stage(
            run_id="r-child-1",
            stage="generate",
            state="failed",
            checkpoint_uri=CHECKPOINT,
            receipt=self.generate_receipt(run_id="r-child-1", state="failed", passed=False),
        )
        child("r-child-3", "q-child-3", "f" * 64)
        self.assertIsNotNone(self.store.run_record("r-child-3"))

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
        self.world.stages[("r-1", "verify")] = ("completed", self.registered_verify_receipt(), CHECKPOINT)
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
                receipt=self.generate_receipt(artifacts=[{"path": "model/robot.json", "bytes": 1, "sha256": "f" * 64}])
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
            "handoff_sha256": self.handoff_full if stage == "freeze" else self.handoff_native,
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

    def test_claim_settles_with_the_stage_confirmation_in_one_transaction(self) -> None:
        self._running_run()
        record = self.store.register_operation(
            run_id="r-1",
            stage="freeze",
            kind="dispatch",
            operation_key="r-1/freeze:0",
            params_digest="a" * 64,
        )
        self.assertEqual(record.state, "queued")
        claimed = self.store.claim_operation(run_id="r-1", stage="freeze", operation_key="r-1/freeze:0")
        self.assertTrue(claimed.granted)
        self.assertEqual(claimed.record.state, "running")
        replay = self.store.claim_operation(run_id="r-1", stage="freeze", operation_key="r-1/freeze:0")
        self.assertFalse(replay.granted)
        self.assertEqual(replay.record.state, "running")
        self.store.confirm_stage(
            run_id="r-1",
            stage="freeze",
            state="completed",
            checkpoint_uri=CHECKPOINT,
            receipt=self._native_receipt("freeze"),
        )
        self.assertEqual(self.world.operations["r-1/freeze:0"][1], "succeeded")
        self.assertEqual(self.world.open_transactions, 0)
        with self.assertRaisesRegex(PlatformStoreError, "only reconcile/cancel"):
            self.store.claim_operation(run_id="r-1", stage="freeze", operation_key="r-1/freeze:0")
        with self.assertRaisesRegex(PlatformStoreError, "not registered"):
            self.store.claim_operation(run_id="r-1", stage="freeze", operation_key="r-1/missing:0")
        with self.assertRaisesRegex(OperationConflictError, "different run or stage"):
            self.store.claim_operation(run_id="r-1", stage="capture", operation_key="r-1/freeze:0")

    def test_initial_portal_dispatch_binds_only_the_existing_intent(self) -> None:
        self.admit(
            dispatch={
                "stage": "freeze",
                "operation_key": "portal-portal-r-1/freeze:0",
                "params_digest": "a" * 64,
                "dag_id": "solidworks_to_urdf",
                "dag_run_id": "portal-r-1",
            }
        )
        record = self.store.bind_initial_dispatch(
            run_id="r-1",
            stage="freeze",
            operation_key="portal-portal-r-1/freeze:0",
            dag_id="solidworks_to_urdf",
            dag_run_id="portal-r-1",
            task_id="freeze",
            map_index=-1,
        )
        self.assertEqual(record.state, "queued")
        operation_id = record.operation_id
        self.assertEqual(
            self.world.operation_bindings[operation_id],
            ("solidworks_to_urdf", "portal-r-1", "freeze", -1),
        )
        before = dict(self.world.operation_bindings)
        self.store.bind_initial_dispatch(
            run_id="r-1",
            stage="freeze",
            operation_key="portal-portal-r-1/freeze:0",
            dag_id="solidworks_to_urdf",
            dag_run_id="portal-r-1",
            task_id="freeze",
            map_index=-1,
        )
        self.assertEqual(self.world.operation_bindings, before)
        with self.assertRaisesRegex(PlatformStoreError, "not registered"):
            self.store.bind_initial_dispatch(
                run_id="r-1",
                stage="freeze",
                operation_key="portal-portal-r-1/freeze:1",
                dag_id="solidworks_to_urdf",
                dag_run_id="portal-r-1",
                task_id="freeze",
                map_index=-1,
            )
        self.assertEqual(self.world.operation_bindings, before)
        for task_id, map_index in (("capture", -1), ("freeze", 0)):
            with self.subTest(task_id=task_id, map_index=map_index):
                with self.assertRaisesRegex(OperationConflictError, "executing freeze task"):
                    self.store.bind_initial_dispatch(
                        run_id="r-1",
                        stage="freeze",
                        operation_key="portal-portal-r-1/freeze:0",
                        dag_id="solidworks_to_urdf",
                        dag_run_id="portal-r-1",
                        task_id=task_id,
                        map_index=map_index,
                    )
                self.assertEqual(self.world.operation_bindings, before)

    def test_initial_portal_dispatch_at_a_linked_boundary_binds_the_real_freeze_task(self) -> None:
        self.store.admit_run(
            request_id="portal-portal-r-1",
            run_id="r-1",
            principal="portal",
            target_hardware="nd",
            main_assembly="cad/robot.SLDASM",
            configuration={"purpose": "engineering"},
            baseline={},
            dispatch={
                "stage": "capture",
                "operation_key": "portal-portal-r-1/capture:0",
                "params_digest": "a" * 64,
                "dag_id": "solidworks_to_urdf",
                "dag_run_id": "portal-r-1",
            },
            from_stage="capture",
            parent_run_id="parent-r-1",
        )
        record = self.store.bind_initial_dispatch(
            run_id="r-1",
            stage="capture",
            operation_key="portal-portal-r-1/capture:0",
            dag_id="solidworks_to_urdf",
            dag_run_id="portal-r-1",
            task_id="freeze",
            map_index=-1,
        )
        self.assertEqual(
            self.world.operation_bindings[record.operation_id],
            ("solidworks_to_urdf", "portal-r-1", "freeze", -1),
        )

    def test_unknown_outcome_cannot_be_bypassed_with_a_new_key(self) -> None:
        self._running_run()
        self.store.register_operation(
            run_id="r-1", stage="freeze", kind="dispatch", operation_key="r-1/freeze:0", params_digest="a" * 64
        )
        self.assertTrue(self.store.claim_operation(run_id="r-1", stage="freeze", operation_key="r-1/freeze:0").granted)
        self.store.confirm_stage(
            run_id="r-1",
            stage="freeze",
            state="unresolved",
            receipt={
                "schema_version": store_module.BUNDLE_SCHEMA,
                "run_id": "r-1",
                "stage": "freeze",
                "state": "unresolved",
            },
        )
        self.assertEqual(self.world.operations["r-1/freeze:0"][1], "unresolved")
        with self.assertRaisesRegex(PlatformStoreError, "unresolved"):
            self.store.register_operation(
                run_id="r-1", stage="freeze", kind="dispatch", operation_key="r-1/freeze:1", params_digest="a" * 64
            )
        with self.assertRaisesRegex(PlatformStoreError, "unresolved"):
            self.store.claim_operation(run_id="r-1", stage="freeze", operation_key="r-1/freeze:0")
        # The evidenced reconciliation stays the one controlled continuation and
        # settles both the original operation and its own reconcile operation.
        reconcile_op = self.store.register_operation(
            run_id="r-1", stage="freeze", kind="reconcile", operation_key="r-1/reconcile:0", params_digest="c" * 64
        )
        self.assertTrue(
            self.store.claim_operation(run_id="r-1", stage="freeze", operation_key="r-1/reconcile:0").granted
        )
        self.store.confirm_stage(
            run_id="r-1",
            stage="freeze",
            state="completed",
            checkpoint_uri=CHECKPOINT,
            receipt=self._native_receipt("freeze"),
            reconcile={"operation_id": reconcile_op.operation_id},
        )
        self.assertEqual(self.world.operations["r-1/freeze:0"][1], "succeeded")
        self.assertEqual(self.world.operations["r-1/reconcile:0"][1], "succeeded")

    def test_terminal_run_refuses_claiming_a_queued_side_effect(self) -> None:
        self._running_run()
        self.store.confirm_stage(
            run_id="r-1",
            stage="capture",
            state="failed",
            receipt={
                "schema_version": store_module.BUNDLE_SCHEMA,
                "run_id": "r-1",
                "stage": "capture",
                "state": "failed",
                "passed": False,
            },
        )
        self.assertEqual(self.world.run_states["r-1"], "failed")
        self.assertEqual(self.world.operations["r-1/capture:0"][1], "failed")
        with self.assertRaisesRegex(PlatformStoreError, "failed"):
            self.store.claim_operation(run_id="r-1", stage="capture", operation_key="r-1/capture:0")
        # Reconciliation stays the controlled continuation on a terminal run.
        reconcile_op = self.store.register_operation(
            run_id="r-1", stage="capture", kind="reconcile", operation_key="r-1/reconcile:0", params_digest="c" * 64
        )
        self.assertTrue(
            self.store.claim_operation(run_id="r-1", stage="capture", operation_key="r-1/reconcile:0").granted
        )
        self.assertEqual(self.world.operations[reconcile_op.operation_key][1], "running")

    def test_completed_confirmation_refuses_a_foreign_sealed_scope(self) -> None:
        self._running_run()
        with self.assertRaisesRegex(PlatformStoreError, "another stage"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="freeze",
                state="completed",
                checkpoint_uri="store://sealed/r-1/generate",
                receipt=self._native_receipt("freeze"),
            )
        with self.assertRaisesRegex(PlatformStoreError, "neither the current run"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="freeze",
                state="completed",
                checkpoint_uri="store://sealed/run-9/freeze",
                receipt=self._native_receipt("freeze"),
            )
        self.store.confirm_stage(
            run_id="r-1",
            stage="freeze",
            state="completed",
            checkpoint_uri="store://sealed/r-1/freeze",
            receipt=self._native_receipt("freeze"),
        )
        self.assertEqual(self.world.stages[("r-1", "freeze")][0], "completed")

    def test_report_members_are_byte_verified_with_the_subject_untouched(self) -> None:
        self._running_run()
        report_file = self.delivery / "reports" / "quality.json"
        record = {
            "path": "reports/quality.json",
            "bytes": report_file.stat().st_size,
            "sha256": file_digest(report_file),
        }
        self.store.confirm_stage(
            run_id="r-1",
            stage="freeze",
            state="completed",
            checkpoint_uri=CHECKPOINT,
            receipt=self._native_receipt("freeze", artifacts=[*self.artifacts, record]),
        )
        self.assertEqual(self.world.stages[("r-1", "freeze")][0], "completed")
        report_file.write_text('{"tampered": true}\n', encoding="utf-8")
        with self.assertRaisesRegex(PlatformStoreError, "report inventory"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="capture",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=self._native_receipt("capture", artifacts=[*self.artifacts, record]),
            )

    def test_failed_diagnostics_verify_report_bytes_too(self) -> None:
        self._running_run()
        report_file = self.delivery / "reports" / "quality.json"
        record = {
            "path": "reports/quality.json",
            "bytes": report_file.stat().st_size,
            "sha256": file_digest(report_file),
        }
        report_file.write_text('{"tampered": true}\n', encoding="utf-8")
        failed_receipt = {
            "schema_version": store_module.BUNDLE_SCHEMA,
            "run_id": "r-1",
            "stage": "freeze",
            "state": "failed",
            "passed": False,
            "artifacts": [*self.artifacts, record],
        }
        with self.assertRaisesRegex(PlatformStoreError, "report inventory"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="freeze",
                state="failed",
                checkpoint_uri=CHECKPOINT,
                receipt=failed_receipt,
            )
        self.assertNotIn(("r-1", "freeze"), self.world.stages)

    def test_failed_registration_verifies_declared_subject_bytes_without_reports(self) -> None:
        self._running_run()
        subject_artifacts = [dict(record) for record in self.artifacts]
        evidence = self.delivery / "evidence" / "capture.json"
        evidence.write_text('{"tampered": true}\n', encoding="utf-8")
        failed_receipt = {
            "schema_version": store_module.BUNDLE_SCHEMA,
            "run_id": "r-1",
            "stage": "freeze",
            "state": "failed",
            "passed": False,
            "artifacts": subject_artifacts,
        }
        with self.assertRaisesRegex(PipelineError, "differ from manifest|bind the sealed delivery"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="freeze",
                state="failed",
                checkpoint_uri=CHECKPOINT,
                receipt=failed_receipt,
            )
        self.assertNotIn(("r-1", "freeze"), self.world.stages)

    def test_a_failed_registration_lists_only_the_real_products(self) -> None:
        self._running_run()
        failed_receipt = {
            "schema_version": store_module.BUNDLE_SCHEMA,
            "run_id": "r-1",
            "stage": "freeze",
            "state": "failed",
            "passed": False,
            "artifacts": [dict(record) for record in self.artifacts],
        }
        self.store.confirm_stage(
            run_id="r-1",
            stage="freeze",
            state="failed",
            checkpoint_uri=CHECKPOINT,
            receipt=failed_receipt,
        )
        self.assertEqual(self.world.stages[("r-1", "freeze")][0], "failed")

    def test_capture_completion_does_not_settle_an_unexecuted_cache_release(self) -> None:
        self._running_run(from_stage="capture")
        self.store.register_operation(
            run_id="r-1", stage="capture", kind="cache_release", operation_key="r-1/release:0", params_digest="c" * 64
        )
        self.assertEqual(self.world.operations["r-1/release:0"][1], "queued")
        self.store.confirm_stage(
            run_id="r-1",
            stage="capture",
            state="completed",
            checkpoint_uri=CHECKPOINT,
            receipt=self._native_receipt("capture"),
        )
        self.assertEqual(self.world.operations["r-1/release:0"][1], "queued")

    def test_confirmed_capture_allows_a_bound_cache_release(self) -> None:
        self._running_run(from_stage="capture")
        self.store.confirm_stage(
            run_id="r-1",
            stage="capture",
            state="completed",
            checkpoint_uri=CHECKPOINT,
            receipt=self._native_receipt("capture"),
        )
        imported = self._release_import_block("r-1")
        unclaimed = self.store.register_operation(
            run_id="r-1",
            stage="capture",
            kind="cache_release",
            operation_key="r-1/release:1",
            params_digest=cache_release_params_digest(imported),
        )
        with self.assertRaisesRegex(PlatformStoreError, "claim it before it settles"):
            self.store.settle_cache_release(
                run_id="r-1",
                stage="capture",
                operation_key="r-1/release:1",
                receipt=self._release_receipt(unclaimed.operation_id, imported),
            )
        operation = self.store.register_operation(
            run_id="r-1",
            stage="capture",
            kind="cache_release",
            operation_key="r-1/release:0",
            params_digest=cache_release_params_digest(imported),
        )
        claim = self.store.claim_operation(run_id="r-1", stage="capture", operation_key="r-1/release:0")
        self.assertTrue(claim.granted)
        # A caller's dict is never a success report: malformed, foreign-operation
        # and foreign-run receipts are refused and the operation stays running.
        for label, receipt, expected in (
            ("malformed", {"released": False}, "canonical IF-10 release receipt"),
            (
                "other-operation",
                self._release_receipt("different-release-operation", imported),
                "names another operation",
            ),
            (
                "other-run",
                self._release_receipt(operation.operation_id, self._release_import_block("different-linux-run")),
                "does not bind this operation's parameters",
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(PlatformStoreError, expected):
                    self.store.settle_cache_release(
                        run_id="r-1", stage="capture", operation_key="r-1/release:0", receipt=receipt
                    )
                self.assertEqual(self.world.operations["r-1/release:0"][1], "running")
        before = len(self.world.statements)
        settled = self.store.settle_cache_release(
            run_id="r-1",
            stage="capture",
            operation_key="r-1/release:0",
            receipt=self._release_receipt(operation.operation_id, imported),
        )
        self.assertEqual(settled.state, "succeeded")
        self.assertEqual(self.world.operations["r-1/release:0"][1], "succeeded")
        # The same-run lock order: run row first, then the operation transition,
        # then the event - the one short transaction every event writer shares.
        tail = self.world.statements[before:]
        lock_index = next(i for i, text in enumerate(tail) if "FROM run" in text and "FOR UPDATE" in text)
        settle_index = next(i for i, text in enumerate(tail) if text.startswith("UPDATE operation SET state = %s"))
        event_index = next(i for i, text in enumerate(tail) if text.startswith("INSERT INTO run_event"))
        self.assertLess(lock_index, settle_index)
        self.assertLess(settle_index, event_index)
        recorded = [event for event in self.world.events if event[2] == "cache_release"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(json.loads(recorded[0][5])["receipt"]["operation_id"], operation.operation_id)

    def test_declared_reuse_requires_the_registered_source_stage(self) -> None:
        self._running_run()
        with self.assertRaisesRegex(PlatformStoreError, "registered parent run"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="freeze",
                state="completed",
                checkpoint_uri="store://sealed/source-with-no-db-row/freeze",
                receipt=self._native_receipt("freeze"),
                reused_from_run="source-with-no-db-row",
            )

    def test_registered_reuse_source_stage_is_accepted(self) -> None:
        from datetime import timedelta

        self.admit(
            run_id="r-0",
            request_id="q-0",
            dispatch={"stage": "capture", "operation_key": "r-0/capture:0", "params_digest": "a" * 64},
        )
        instant = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
        source_receipt = self._native_receipt("capture", run_id="r-0")
        self.world.stages[("r-0", "capture")] = (
            "completed",
            source_receipt,
            "store://sealed/r-0/capture",
            None,
            instant,
        )
        self.admit(run_id="r-1", parent_run_id="r-0")
        self.world.run_states["r-1"] = "running"
        # The payload must BE the confirmed source: a rewritten producer, a
        # shifted instant and a dropped instant all refuse before any write.
        for label, receipt, when, expected in (
            ("rewritten", {**source_receipt, "run_id": "r-1"}, instant, "not the confirmed source receipt"),
            ("shifted", {**source_receipt}, instant + timedelta(seconds=1), "not the confirmed source instant"),
            ("dropped", {**source_receipt}, None, "not the confirmed source instant"),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(PlatformStoreError, expected):
                self.store.confirm_stage(
                    run_id="r-1",
                    stage="capture",
                    state="completed",
                    checkpoint_uri="store://sealed/r-0/capture",
                    receipt=receipt,
                    reused_from_run="r-0",
                    original_executed_at=when,
                )
        self.store.confirm_stage(
            run_id="r-1",
            stage="capture",
            state="completed",
            checkpoint_uri="store://sealed/r-0/capture",
            receipt={**source_receipt},
            reused_from_run="r-0",
            original_executed_at=instant,
        )
        row = self.world.stages[("r-1", "capture")]
        self.assertEqual(row[0], "completed")
        self.assertEqual(row[1]["run_id"], "r-0")
        self.assertEqual(row[4], instant)

    def test_unresolved_moves_the_run_and_reconciliation_restores_it(self) -> None:
        self._running_run(from_stage="capture")
        self.store.claim_operation(run_id="r-1", stage="capture", operation_key="r-1/capture:0")
        self.store.confirm_stage(
            run_id="r-1",
            stage="capture",
            state="unresolved",
            receipt={
                "schema_version": store_module.BUNDLE_SCHEMA,
                "run_id": "r-1",
                "stage": "capture",
                "state": "unresolved",
            },
        )
        self.assertEqual(self.world.run_states["r-1"], "unresolved")
        reconcile_op = self.store.register_operation(
            run_id="r-1", stage="capture", kind="reconcile", operation_key="r-1/reconcile:0", params_digest="c" * 64
        )
        self.assertTrue(
            self.store.claim_operation(run_id="r-1", stage="capture", operation_key="r-1/reconcile:0").granted
        )
        self.store.confirm_stage(
            run_id="r-1",
            stage="capture",
            state="completed",
            checkpoint_uri=CHECKPOINT,
            receipt=self._native_receipt("capture"),
            reconcile={"operation_id": reconcile_op.operation_id},
        )
        self.assertEqual(self.world.run_states["r-1"], "running")

    def test_run_events_reads_a_bounded_ordered_window(self) -> None:
        self._running_run()
        before = len(self.world.statements)
        self.store.append_event("r-1", stage="freeze", kind="observed", payload={"step": 1})
        tail = self.world.statements[before:]
        lock_index = next(i for i, text in enumerate(tail) if "FROM run" in text and "FOR UPDATE" in text)
        event_index = next(i for i, text in enumerate(tail) if text.startswith("INSERT INTO run_event"))
        self.assertLess(lock_index, event_index)
        self.store.append_event("r-1", stage="freeze", kind="observed", payload={"step": 2})
        events = self.store.run_events("r-1")
        self.assertEqual(
            [(event["seq"], event["kind"], event["payload"]) for event in events],
            [(1, "observed", {"step": 1}), (2, "observed", {"step": 2})],
        )
        window = self.store.run_events("r-1", after_seq=1, limit=1)
        self.assertEqual([event["seq"] for event in window], [2])
        self.assertEqual(self.store.run_events("r-1", after_seq=9), [])
        with self.assertRaisesRegex(PlatformStoreError, "limit must be a positive integer"):
            self.store.run_events("r-1", limit=0)
        with self.assertRaisesRegex(PlatformStoreError, "after_seq"):
            self.store.run_events("r-1", after_seq=-1)

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
            receipt=self.registered_verify_receipt(),
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

    def test_a_blocked_stage_closes_its_never_dispatched_intent(self) -> None:
        self._running_run()
        self.store.confirm_stage(
            run_id="r-1",
            stage="freeze",
            state="failed",
            receipt={
                "schema_version": store_module.BUNDLE_SCHEMA,
                "run_id": "r-1",
                "stage": "freeze",
                "state": "failed",
                "passed": False,
            },
        )
        self.assertEqual(self.world.operations["r-1/capture:0"][1], "queued")
        self.store.confirm_stage(run_id="r-1", stage="capture", state="blocked")
        self.assertEqual(
            self.world.operations["r-1/capture:0"][1],
            "cancelled",
            "a never-dispatched intent closes as a cancelled non-execution, never as failed",
        )

    def test_a_blocked_stage_never_rewrites_a_claimed_intent(self) -> None:
        self._running_run()
        self.assertTrue(
            self.store.claim_operation(run_id="r-1", stage="capture", operation_key="r-1/capture:0").granted
        )
        self.store.confirm_stage(run_id="r-1", stage="capture", state="blocked")
        self.assertEqual(
            self.world.operations["r-1/capture:0"][1],
            "running",
            "a claimed operation was already dispatched: a blocked record never rewrites it",
        )

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
        receipt = self.registered_verify_receipt(
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

    # -------------------------------------- completed report binding (owned)

    def test_completed_verify_accepts_the_matching_registered_report(self) -> None:
        self._running_run()
        self.store.confirm_stage(
            run_id="r-1",
            stage="verify",
            state="completed",
            checkpoint_uri=CHECKPOINT,
            receipt=self.registered_verify_receipt(),
        )
        self.assertEqual(self.store.confirmed_stage("r-1", "verify")["quality"]["passed"], True)

    def test_completed_publish_accepts_the_matching_registered_report(self) -> None:
        self._running_run()
        receipt = self.registered_verify_receipt(
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

    def test_publication_receipt_fields_are_not_paths_and_missing_files_still_refuse(self) -> None:
        self._running_run()
        receipt = self.registered_verify_receipt(
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
        self.assertIn("submission", store_module.STAGE_EVIDENCE["publish"])
        self.assertFalse(store_module._evidence_path("submission"))
        self.assertTrue(store_module._evidence_path("reports/absent.json"))
        self.assertTrue(store_module._evidence_path("reports/中文.json"))
        with (
            mock.patch.object(
                store_module,
                "STAGE_EVIDENCE",
                {"publish": ("submission", "reports/中文.json")},
            ),
            self.assertRaisesRegex(PlatformStoreError, "declared evidence"),
        ):
            self.store.confirm_stage(
                run_id="r-1",
                stage="publish",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=receipt,
            )
        self.assertNotIn(("r-1", "publish"), self.world.stages)

    def test_completed_verify_refuses_a_changed_check_detail(self) -> None:
        import copy

        self._running_run()
        report = copy.deepcopy(self.qualified_quality())
        report["checks"][0]["details"] = {"observation": "different report evidence statement"}
        write_json(self.delivery / "reports" / "quality.json", report)
        with self.assertRaisesRegex(PlatformStoreError, "contradicts the receipt"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="verify",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=self.registered_verify_receipt(),
            )
        self.assertNotIn(("r-1", "verify"), self.world.stages)

    def test_completed_verify_refuses_an_unregistered_report_member(self) -> None:
        self._running_run()
        with self.assertRaisesRegex(PlatformStoreError, "lack the byte-bound"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="verify",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=self.verify_receipt(),
            )
        self.assertNotIn(("r-1", "verify"), self.world.stages)

    def test_completed_verify_refuses_a_sealed_passed_flip(self) -> None:
        import copy

        self._running_run()
        report = copy.deepcopy(self.qualified_quality())
        report["passed"] = False
        write_json(self.delivery / "reports" / "quality.json", report)
        with self.assertRaisesRegex(PlatformStoreError, "no qualified reports/quality.json"):
            self.store.confirm_stage(
                run_id="r-1",
                stage="verify",
                state="completed",
                checkpoint_uri=CHECKPOINT,
                receipt=self.registered_verify_receipt(),
            )
        self.assertNotIn(("r-1", "verify"), self.world.stages)

    def test_confirmed_stage_rechecks_the_report_after_registration(self) -> None:
        import copy

        self._running_run()
        self.store.confirm_stage(
            run_id="r-1",
            stage="verify",
            state="completed",
            checkpoint_uri=CHECKPOINT,
            receipt=self.registered_verify_receipt(),
        )
        report = copy.deepcopy(self.qualified_quality())
        report["checks"][0]["details"] = {"observation": "tampered after registration"}
        write_json(self.delivery / "reports" / "quality.json", report)
        with self.assertRaisesRegex(PlatformStoreError, "does not bind|contradicts the receipt"):
            self.store.confirmed_stage("r-1", "verify")

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

    def test_the_environment_factory_carries_the_deploy_sealed_resolver(self) -> None:
        """The one official instance always has a real sealed resolver: no production
        path reads registered rows and then cannot resolve their checkpoints."""

        dsn = "postgresql://service@/platform?host=/sock"
        with self.assertRaisesRegex(PlatformStoreError, "PIPELINE_STORE_ROOT"):
            PlatformStore.from_environment({"PLATFORM_SERVICE_DSN": dsn})
        with tempfile.TemporaryDirectory() as tmp:
            store = PlatformStore.from_environment({"PLATFORM_SERVICE_DSN": dsn, "PIPELINE_STORE_ROOT": tmp})
            self.assertEqual(
                store.sealed_path("store://sealed/run-1/verify"),
                Path(tmp).resolve() / "sealed/run-1/verify",
            )

    def test_missing_admission_row_is_refused_without_typing_it_closed(self) -> None:
        self.world.admission = None
        with self.assertRaises(PlatformStoreError) as caught:
            self.store.close_admission(reason="drain", actor="operator")
        self.assertIsInstance(caught.exception, PipelineError)
        self.assertNotIsInstance(caught.exception, AdmissionClosedError)
        with self.assertRaises(PlatformStoreError):
            self.admit()


RUN_A = "portal-20261015T144524-c89993da"
RUN_B = "portal-20261015T145141-ff63fddc"
#: The real 1.4.3 task_instance facts of both runs (4090 raw evidence, sha a7c7867a…).
REAL_TIS = {
    "resolve_handoff": "success",
    "start_job": "failed",
    "fetch_capture": "upstream_failed",
    "run_generate": "upstream_failed",
    "run_verify": "upstream_failed",
    "run_publish": "upstream_failed",
}
NATIVE_RUNS = {
    RUN_A: "1eae5a3b-01f8-5d57-8385-8877018105c9",
    RUN_B: "f9f328cd-ab28-5a33-9f4d-518a2b64f6f1",
}


class OrphanReconcileTests(unittest.TestCase):
    """G-1 controlled orphan recovery through the one canonical reconcile entry."""

    def setUp(self) -> None:
        self.world = FakeWorld()
        self.store = PlatformStore(self.world.connect)

    # ------------------------------------------------------------------ fixtures
    def _legacy_orphan(self, run_id: str, *, dag_run_id: str | None = None) -> str:
        """One never-started 1.4.3-shaped attempt; returns its dispatch operation id."""
        dispatch = {
            "stage": "capture",
            "operation_key": f"portal-{run_id}/capture:0",
            "params_digest": "a" * 64,
            "dag_id": "solidworks_to_urdf",
            "dag_run_id": dag_run_id if dag_run_id is not None else run_id,
        }
        self.store.admit_run(
            request_id=f"portal-{run_id}",
            run_id=run_id,
            principal="portal",
            target_hardware="m3.0",
            main_assembly="3.0 总装.SLDASM",
            dispatch=dispatch,
        )
        for task, state in REAL_TIS.items():
            self.world.task_instances[("solidworks_to_urdf", task, run_id, -1)] = state
        self.world.dag_runs[("solidworks_to_urdf", run_id)] = "failed"
        return self.world.operations[f"portal-{run_id}/capture:0"][0]

    def _registered_reconcile(self, run_id: str, state: str = "failed") -> str:
        """A pre-registered maintenance intent, as the schema shows it before the action."""

        operation = self.store.register_operation(
            run_id=run_id,
            stage="freeze",
            kind="reconcile",
            operation_key=f"operatorctl-reconcile:{run_id}:freeze:{state}",
            params_digest=digest({"outcome": state}),
        )
        return operation.operation_id

    def _call(
        self,
        run_id: str,
        dispatch_id: str,
        *,
        reconcile_operation_id: str | None = None,
        task: str = "start_job",
        map_index: int = -1,
        state: str = "failed",
        evidence: str | None = None,
        extra_evidence: str | None = None,
        claimed_sha: str | None = None,
        native_run: str | None = None,
        native_document: dict | None = None,
        native_path: str | None = None,
        with_binding: bool = True,
        with_receipt_inputs: bool = True,
        formed_receipt: dict | None = None,
    ) -> str:
        """One whole reconcile action through the one canonical entry.

        The action carries only the raw inputs (the original file and the bound
        endpoint job UUID); the Store action itself loads and validates the
        bytes - this helper holds no acceptance rules of its own.  It returns
        the receipt path so callers can compute the expected byte digest.
        Without ``reconcile_operation_id`` the action carries its own
        registration (key plus parameter digest): the maintenance registration,
        the binding and the settlement commit or roll back as one short unit.
        With it, the schema-visible pre-registered intent is named by id.
        """

        binding_native = native_run if native_run is not None else NATIVE_RUNS[run_id]
        if not hasattr(self, "_receipt_dir_path"):
            self._receipt_dir_path = Path(tempfile.mkdtemp(prefix="orphan-native-"))
            self.addCleanup(shutil.rmtree, self._receipt_dir_path, ignore_errors=True)
        receipt_path = self._receipt_dir_path / "native-failed-receipt.json"
        document = (
            native_document
            if native_document is not None
            else {
                "schema_version": "solidworks-to-urdf.job/v1",
                "pipeline_id": PIPELINE_ID,
                "run_id": binding_native,
                "request": {"run_id": binding_native, "main_assembly": "3.0 总装.SLDASM"},
                "status": "failed",
                "events": [{"stage": "freeze", "state": "failed", "error": "SelectionError: two open errors"}],
                "error": "SelectionError: two open errors",
                "error_code": "selection_open_errors",
            }
        )
        receipt_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        named_path = Path(native_path) if native_path is not None else receipt_path
        if evidence is not None:
            # A raw evidence entry names the original it binds: its path is the raw input.
            named_path = Path(evidence.split("#", 1)[0])
        action_evidence = [extra_evidence] if extra_evidence is not None else []
        if evidence is not None:
            action_evidence.append(evidence)
        if claimed_sha is not None:
            action_evidence.append(f"{named_path}#sha256={claimed_sha}")
        reconcile: dict = {"evidence": action_evidence}
        if reconcile_operation_id is None:
            reconcile["operation_key"] = f"operatorctl-reconcile:{run_id}:freeze:{state}"
            reconcile["params_digest"] = digest(
                {
                    "outcome": state,
                    "evidence": action_evidence,
                    "operation": dispatch_id,
                    "task": task,
                    "map_index": map_index,
                }
            )
        else:
            reconcile["operation_id"] = reconcile_operation_id
        if with_binding:
            reconcile["binding"] = {"operation_id": dispatch_id, "task_id": task, "map_index": map_index}
        if with_receipt_inputs:
            reconcile["native_receipt"] = {"path": str(named_path), "native_run_id": binding_native}
        self.store.confirm_stage(
            run_id=run_id, stage="freeze", state=state, receipt=formed_receipt, reconcile=reconcile
        )
        return str(named_path)

    def _snapshot(self):
        """The complete world state (rows, bindings, events) for zero-change proofs."""

        return self.world.snapshot_state()

    # ------------------------------------------------------------------ positive
    def test_an_orphan_reconciliation_binds_the_real_key_and_records_the_failure(self) -> None:
        for run_id in (RUN_A, RUN_B):
            with self.subTest(run_id=run_id):
                self.setUp()
                dispatch_id = self._legacy_orphan(run_id)
                receipt_path = self._call(run_id, dispatch_id)
                loaded_sha = file_digest(Path(receipt_path))
                self.assertEqual(
                    self.world.operation_bindings[dispatch_id],
                    ("solidworks_to_urdf", run_id, "start_job", -1),
                )
                self.assertEqual(self.world.run_states[run_id], "failed")
                stage = self.world.stages[(run_id, "freeze")]
                self.assertEqual(stage[0], "failed")
                self.assertEqual(stage[1]["error"], "SelectionError: two open errors")
                self.assertEqual(stage[1]["reconciliation"]["binding"]["task_id"], "start_job")
                self.assertEqual(stage[1]["native_run_id"], NATIVE_RUNS[run_id])
                self.assertEqual(stage[1]["native_receipt_sha256"], loaded_sha)
                self.assertEqual(
                    stage[1]["reconciliation"]["evidence"],
                    [f"{receipt_path}#sha256={loaded_sha}"],
                )
                self.assertEqual(
                    stage[1]["reconciliation"]["native_receipt"],
                    {"path": receipt_path, "native_run_id": NATIVE_RUNS[run_id]},
                )
                self.assertEqual(self.world.operations[f"portal-{run_id}/capture:0"][1], "failed", "the intent settles")
                reconcile_key = f"operatorctl-reconcile:{run_id}:freeze:failed"
                self.assertEqual(self.world.operations[reconcile_key][1], "succeeded")
                kinds = [row[2] for row in self.world.events if row[0] == run_id]
                self.assertEqual(kinds, ["binding", "stage"])
                binding_payload = json.loads(self.world.events[0][5])
                self.assertEqual(binding_payload["task_id"], "start_job")
                self.assertEqual(binding_payload["map_index"], -1)
                self.assertEqual(binding_payload["operation_key"], f"portal-{run_id}/capture:0")
                self.assertEqual(binding_payload["intent_stage"], "capture")
                self.assertEqual(binding_payload["reconciled_stage"], "freeze")
                self.assertEqual(binding_payload["reconciled_state"], "failed")
                self.assertEqual(binding_payload["dag_id"], "solidworks_to_urdf")
                self.assertEqual(binding_payload["dag_run_id"], run_id)
                self.assertEqual(binding_payload["native_run_id"], NATIVE_RUNS[run_id])
                self.assertEqual(binding_payload["native_receipt_sha256"], loaded_sha)

    def test_a_repeat_of_the_same_whole_action_changes_nothing(self) -> None:
        dispatch_id = self._legacy_orphan(RUN_A)
        self._call(RUN_A, dispatch_id)
        before = self._snapshot()
        self._call(RUN_A, dispatch_id)
        self.assertEqual(self._snapshot(), before, "the identical whole action repeats as a no-op")

    # ------------------------------------------------------------------ refusals
    def test_unprovable_or_conflicting_bindings_change_nothing(self) -> None:
        cases = {
            "ghost_task": {"task": "ghost_task"},
            # upstream_failed never executed: the sibling's existence is not intent binding.
            "unexecuted_sibling": {"task": "run_publish"},
            "live_task": {"task": "fetch_capture"},
            "green_task": {"task": "resolve_handoff"},
            "wrong_map_index": {"map_index": 0},
            "foreign_native_uuid": {"native_run": "deadbeef-0000-4000-8000-000000000000"},
            "foreign_receipt_digest": {"claimed_sha": "0" * 64},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                self.setUp()
                dispatch_id = self._legacy_orphan(RUN_A)
                if name == "live_task":
                    self.world.task_instances[("solidworks_to_urdf", "fetch_capture", RUN_A, -1)] = "running"
                before = self._snapshot()
                with self.assertRaises(PlatformStoreError):
                    self._call(RUN_A, dispatch_id, **overrides)
                self.assertEqual(self._snapshot(), before, "a refused whole action leaves zero change")
                self.assertNotIn(
                    f"operatorctl-reconcile:{RUN_A}:freeze:failed",
                    self.world.operations,
                    "the maintenance registration rolls back with the refused action",
                )

    def test_a_foreign_native_uuid_never_settles_the_run(self) -> None:
        dispatch_id = self._legacy_orphan(RUN_A)
        before = self._snapshot()
        with self.assertRaises(PlatformStoreError):
            self._call(
                RUN_A,
                dispatch_id,
                native_run="deadbeef-0000-4000-8000-000000000000",
            )
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.world.run_states.get(RUN_A), "queued")
        self.assertEqual(self.world.events, [])
        self.assertEqual(
            self.world.operation_bindings[dispatch_id],
            ("solidworks_to_urdf", RUN_A, None, None),
            "the intent stays unbound and queued",
        )

    def test_the_original_receipt_bytes_are_the_only_evidence_source(self) -> None:
        def job_document(**overrides) -> dict:
            document = {
                "schema_version": "solidworks-to-urdf.job/v1",
                "pipeline_id": PIPELINE_ID,
                "run_id": NATIVE_RUNS[RUN_A],
                "request": {"run_id": NATIVE_RUNS[RUN_A]},
                "status": "failed",
                "events": [{"stage": "freeze", "state": "failed", "error": "SelectionError: two open errors"}],
                "error": "SelectionError: two open errors",
                "error_code": "selection_open_errors",
            }
            document.update(overrides)
            return document

        cases = {
            # A shaped digest string over a file that does not exist proves nothing.
            "missing_file": {"native_path": "/nonexistent/review/native.json"},
            "empty_document": {"native_document": {}},
            "report_shaped_document": {
                "native_document": {"schema_version": "solidworks-to-urdf.structural-report/v1", "passed": True}
            },
            "completed_receipt": {
                "native_document": job_document(status="passed", events=[{"stage": "freeze", "state": "passed"}])
            },
            "passed_terminal_event": {
                "native_document": job_document(
                    events=[
                        {"stage": "freeze", "state": "passed", "error": None},
                        {"stage": "capture", "state": "failed"},
                    ]
                )
            },
            "no_terminal_event": {"native_document": job_document(events=[])},
            "ambiguous_terminal_events": {
                "native_document": job_document(
                    events=[
                        {"stage": "freeze", "state": "failed"},
                        {"stage": "capture", "state": "failed"},
                    ]
                )
            },
            "foreign_receipt_run": {
                "native_document": job_document(
                    run_id="deadbeef-0000-4000-8000-000000000000",
                    request={"run_id": "deadbeef-0000-4000-8000-000000000000"},
                )
            },
            "foreign_receipt_stage": {
                "native_document": job_document(events=[{"stage": "capture", "state": "failed"}])
            },
            "foreign_receipt_job": {
                "native_document": job_document(run_id=NATIVE_RUNS[RUN_B], request={"run_id": NATIVE_RUNS[RUN_B]})
            },
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                self.setUp()
                dispatch_id = self._legacy_orphan(RUN_A)
                before = self._snapshot()
                with self.assertRaises(PlatformStoreError):
                    self._call(RUN_A, dispatch_id, **overrides)
                self.assertEqual(self._snapshot(), before, "the refused bytes changed nothing")
                self.assertEqual(self.world.run_states.get(RUN_A), "queued")

    def test_the_native_identity_must_derive_from_the_runs_own_dag_run(self) -> None:
        for name, overrides in {
            "empty": {"native_run": ""},
            "not_a_uuid": {"native_run": "not-a-uuid"},
            "another_runs_job": {"native_run": NATIVE_RUNS[RUN_B]},
        }.items():
            with self.subTest(name=name):
                self.setUp()
                dispatch_id = self._legacy_orphan(RUN_A)
                before = self._snapshot()
                with self.assertRaises(PlatformStoreError):
                    self._call(RUN_A, dispatch_id, **overrides)
                self.assertEqual(self._snapshot(), before)
        with self.subTest(name="dag_run_id_belongs_to_another_attempt"):
            self.setUp()
            dispatch_id = self._legacy_orphan(RUN_A, dag_run_id=RUN_B)
            before = self._snapshot()
            with self.assertRaises(PlatformStoreError):
                self._call(RUN_A, dispatch_id)
            self.assertEqual(self._snapshot(), before)

    def test_the_failed_receipt_digest_must_be_the_evidence_bound_one(self) -> None:
        dispatch_id = self._legacy_orphan(RUN_A)
        before = self._snapshot()
        # A caller-supplied digest that contradicts the original bytes is never normalized away.
        with self.assertRaises(PlatformStoreError):
            self._call(RUN_A, dispatch_id, claimed_sha="0" * 64)
        # A formed receipt with matching digest strings never substitutes for the raw inputs.
        with self.assertRaises(PlatformStoreError):
            self._call(
                RUN_A,
                dispatch_id,
                with_receipt_inputs=False,
                formed_receipt={
                    "schema_version": store_module.BUNDLE_SCHEMA,
                    "pipeline_id": PIPELINE_ID,
                    "run_id": RUN_A,
                    "stage": "freeze",
                    "state": "failed",
                    "passed": False,
                    "native_run_id": NATIVE_RUNS[RUN_A],
                    "native_receipt_sha256": "f" * 64,
                },
            )
        self.assertEqual(self._snapshot(), before)

    def test_a_formed_receipt_at_the_direct_store_boundary_proves_nothing(self) -> None:
        """The direct Store boundary: a formed receipt plus matching digest strings refuses.

        These four predicates mirror the design side's independent direct-store probe:
        a missing original, an empty document, a successful ("passed") document and a
        foreign-run document, each presented as a ready-made platform receipt with a
        matching ``#sha256=`` evidence entry and no raw inputs.
        """

        variants = {
            "missing_original": ("/nonexistent/review/native.json", "f" * 64, None),
            "empty_original": (None, None, {}),
            "successful_original": (
                None,
                None,
                {
                    "schema_version": "solidworks-to-urdf.job/v1",
                    "pipeline_id": PIPELINE_ID,
                    "run_id": NATIVE_RUNS[RUN_A],
                    "request": {"run_id": NATIVE_RUNS[RUN_A]},
                    "status": "passed",
                    "events": [{"stage": "freeze", "state": "passed"}],
                },
            ),
            "foreign_original": (
                None,
                None,
                {
                    "schema_version": "solidworks-to-urdf.job/v1",
                    "pipeline_id": PIPELINE_ID,
                    "run_id": "deadbeef-0000-4000-8000-000000000000",
                    "request": {"run_id": "deadbeef-0000-4000-8000-000000000000"},
                    "status": "failed",
                    "events": [{"stage": "freeze", "state": "failed"}],
                },
            ),
        }
        for name, (path_override, sha_override, document) in variants.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="native-bytes-direct-") as directory:
                self.setUp()
                dispatch_id = self._legacy_orphan(RUN_A)
                if path_override is not None:
                    path = Path(path_override)
                    sha = sha_override
                else:
                    path = Path(directory) / "original.json"
                    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
                    sha = file_digest(path)
                receipt = {
                    "schema_version": store_module.BUNDLE_SCHEMA,
                    "pipeline_id": PIPELINE_ID,
                    "run_id": RUN_A,
                    "stage": "freeze",
                    "state": "failed",
                    "passed": False,
                    "native_run_id": NATIVE_RUNS[RUN_A],
                    "native_receipt_sha256": sha,
                }
                evidence = [f"{path}#sha256={sha}"]
                binding = {"operation_id": dispatch_id, "task_id": "start_job", "map_index": -1}
                reconcile = {
                    "operation_key": f"operatorctl-reconcile:{RUN_A}:freeze:failed",
                    "params_digest": digest({"outcome": "failed", "evidence": evidence, "binding": binding}),
                    "evidence": evidence,
                    "binding": binding,
                }
                before = self._snapshot()
                with self.assertRaises(PlatformStoreError):
                    self.store.confirm_stage(
                        run_id=RUN_A, stage="freeze", state="failed", receipt=receipt, reconcile=reconcile
                    )
                self.assertEqual(self._snapshot(), before, "the refused direct call changed nothing")
                self.assertEqual(self.world.run_states.get(RUN_A), "queued")

    def test_the_lifecycle_facts_travel_in_one_statement(self) -> None:
        """One query carries the task and the DagRun: two separate reads are never a snapshot."""

        dispatch_id = self._legacy_orphan(RUN_A)
        self.world.dag_runs[("solidworks_to_urdf", RUN_A)] = "running"
        self.world.statements.clear()
        before = self._snapshot()
        with self.assertRaises(PlatformStoreError):
            self._call(RUN_A, dispatch_id)
        self.assertEqual(self._snapshot(), before)
        reads = [row for row in self.world.statements if "FROM lifecycle_projection" in row]
        self.assertEqual(len(reads), 1, f"the acceptance read the lifecycle association {len(reads)} times")
        self.assertIn("object_kind = 'scheduling'", reads[0])
        self.assertIn("object_kind = 'dag_run'", reads[0])

    def test_a_live_or_unknown_dag_run_never_authorizes_recovery(self) -> None:
        for name, dag_state in (("live", "running"), ("green", "success")):
            with self.subTest(name=name):
                self.setUp()
                dispatch_id = self._legacy_orphan(RUN_A)
                self.world.dag_runs[("solidworks_to_urdf", RUN_A)] = dag_state
                before = self._snapshot()
                with self.assertRaises(PlatformStoreError):
                    self._call(RUN_A, dispatch_id)
                self.assertEqual(self._snapshot(), before)
        with self.subTest(name="unreadable"):
            self.setUp()
            dispatch_id = self._legacy_orphan(RUN_A)
            del self.world.dag_runs[("solidworks_to_urdf", RUN_A)]
            before = self._snapshot()
            with self.assertRaises(PlatformStoreError):
                self._call(RUN_A, dispatch_id)
            self.assertEqual(self._snapshot(), before)

    def test_a_repeat_that_renames_the_native_identity_changes_nothing(self) -> None:
        dispatch_id = self._legacy_orphan(RUN_A)
        self._call(RUN_A, dispatch_id)
        before = self._snapshot()
        with self.assertRaises(PlatformStoreError):
            self._call(
                RUN_A,
                dispatch_id,
                native_run="deadbeef-0000-4000-8000-000000000000",
            )
        self.assertEqual(self._snapshot(), before, "a conflicting native identity is never an idempotent repeat")

    def test_a_repeat_that_conflicts_with_the_recorded_outcome_changes_nothing(self) -> None:
        dispatch_id = self._legacy_orphan(RUN_A)
        self._call(RUN_A, dispatch_id)
        before = self._snapshot()
        recorded = self.world.operations[f"operatorctl-reconcile:{RUN_A}:freeze:failed"][0]
        with self.assertRaises(PlatformStoreError):
            self._call(RUN_A, dispatch_id, reconcile_operation_id=recorded, state="cancelled")
        with self.assertRaises(PlatformStoreError):
            self._call(RUN_A, dispatch_id, state="cancelled")
        with self.assertRaises(PlatformStoreError):
            # The same native fact under a second, different evidence identity is a conflict.
            self._call(RUN_A, dispatch_id, extra_evidence="reports/other.json#sha256=" + "0" * 64)
        self.assertEqual(self._snapshot(), before, "a conflicting outcome or evidence is never a repeat")

    def test_a_registered_intent_refuses_the_same_conflicts_with_zero_change(self) -> None:
        dispatch_id = self._legacy_orphan(RUN_A)
        registered = self._registered_reconcile(RUN_A)
        before = self._snapshot()
        for overrides in (
            {"native_run": "deadbeef-0000-4000-8000-000000000000"},
            {"task": "run_publish"},
        ):
            with self.assertRaises(PlatformStoreError):
                self._call(RUN_A, dispatch_id, reconcile_operation_id=registered, **overrides)
        self.assertEqual(self._snapshot(), before)

    def test_structurally_invalid_intents_change_nothing(self) -> None:
        for name, setup in (
            ("foreign_operation", None),
            ("missing_dag_run_evidence", "missing"),
            ("already_bound_conflict", "conflict"),
            ("started_intent", "started"),
        ):
            with self.subTest(name=name):
                self.setUp()
                if setup == "missing":
                    dispatch_id = self._legacy_orphan(RUN_A, dag_run_id="")
                else:
                    dispatch_id = self._legacy_orphan(RUN_A)
                if setup == "conflict":
                    self.world.operation_bindings[dispatch_id] = (
                        "solidworks_to_urdf",
                        RUN_A,
                        "start_job",
                        -1,
                    )
                if setup == "started":
                    key = f"portal-{RUN_A}/capture:0"
                    record = self.world.operations[key]
                    self.world.operations[key] = (*record[:1], "running", *record[2:])
                before = self._snapshot()
                target = "deadbeef-0000-0000-0000-000000000000" if name == "foreign_operation" else dispatch_id
                with self.assertRaises(PlatformStoreError):
                    self._call(RUN_A, target)
                self.assertEqual(self._snapshot(), before, "refused binding leaves zero change")

    def test_non_failed_or_unbound_reconciles_keep_the_classic_rules(self) -> None:
        dispatch_id = self._legacy_orphan(RUN_A)
        before = self._snapshot()
        with self.assertRaises(PlatformStoreError):
            self._call(RUN_A, dispatch_id, state="completed")
        with self.assertRaises(PlatformStoreError):
            self._call(RUN_A, dispatch_id, with_binding=False)
        self.assertEqual(self._snapshot(), before)
        # A recorded orphan stage never takes a different binding later.
        self._call(RUN_A, dispatch_id)
        before = self._snapshot()
        with self.assertRaises(PlatformStoreError):
            self._call(RUN_A, dispatch_id, task="ghost_task")
        self.assertEqual(self._snapshot(), before)


BOUND_DISCOVER_RUN = "3324bcb0-085a-5f77-8edc-a4c1be2f1a5a"
BOUND_DISCOVER_DAG_RUN = "portal-20261019T063230-88dee231"
DAG_ID = "solidworks_to_urdf"


class ReportedFreezeActualDiscoverReconcileTests(unittest.TestCase):
    """The one bound-native reported-freeze/actual-discover recovery shape."""

    def setUp(self) -> None:
        import uuid

        from description_pipeline.delivery import BUNDLE_SCHEMA
        from description_pipeline.io import read_data
        from description_pipeline.orchestration.airflow_client import native_run_id
        from tests.v1.test_platform_store import build_sealed_delivery

        self.tmp = Path(tempfile.mkdtemp(prefix="bound-native-discover-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.world = FakeWorld()
        self.run_id = BOUND_DISCOVER_RUN
        self.dag_run_id = BOUND_DISCOVER_DAG_RUN
        self.native_run = native_run_id(self.dag_run_id)
        self.sealed_root = self.tmp / self.run_id / "freeze"
        subject, artifacts, self.upload_sha, _native_handoff = build_sealed_delivery(self.sealed_root)
        index = read_data(self.sealed_root / "input" / "handoff.json")
        self.upload_files = dict(index["package_files"])
        self.main_assembly = "cad/cad/robot.SLDASM"
        self.store = PlatformStore(
            self.world.connect,
            sealed=lambda uri: self.tmp / uri.removeprefix("store://sealed/"),
        )
        self.store.admit_run(
            request_id=f"portal-{self.dag_run_id}",
            run_id=self.run_id,
            principal="portal",
            target_hardware="m3.0",
            main_assembly=self.main_assembly,
            configuration={"purpose": "inspection_preview"},
            baseline={
                "handoff_sha256": self.upload_sha,
                "files": len(self.upload_files),
                "bytes": sum((self.sealed_root / "input" / name).stat().st_size for name in self.upload_files),
                "folder": "M3.0.3_20261016",
            },
            dispatch={
                "stage": "freeze",
                "operation_key": f"portal-{self.dag_run_id}/freeze:0",
                "params_digest": "f" * 64,
                "dag_id": DAG_ID,
                "dag_run_id": self.dag_run_id,
                "task_id": "freeze",
                "map_index": -1,
            },
        )
        self.store.start_run(self.run_id)
        freeze_receipt = {
            "schema_version": BUNDLE_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": self.run_id,
            "stage": "freeze",
            "state": "frozen",
            "passed": False,
            "handoff_sha256": self.upload_sha,
            "subject_sha256": subject,
            "tool": read_data(self.sealed_root / "tool.json"),
            "artifacts": artifacts,
        }
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="freeze",
            state="completed",
            receipt=freeze_receipt,
            checkpoint_uri=f"store://sealed/{self.run_id}/freeze",
        )
        self.native_id = str(uuid.uuid4())
        self.native_key = f"{self.run_id}/native:0"
        self.world.operations[self.native_key] = (
            self.native_id,
            "running",
            self.native_key,
            "e" * 64,
            self.run_id,
            "discover",
            "native",
        )
        self.world.operation_bindings[self.native_id] = (DAG_ID, self.dag_run_id, "discover", -1)
        self.world.task_instances[(DAG_ID, "freeze", self.dag_run_id, -1)] = "success"
        self.world.task_try_numbers[(DAG_ID, "freeze", self.dag_run_id, -1)] = 1
        self.world.task_instances[(DAG_ID, "discover", self.dag_run_id, -1)] = "failed"
        self.world.task_try_numbers[(DAG_ID, "discover", self.dag_run_id, -1)] = 1
        for task in ("capture", "generate", "verify", "publish"):
            self.world.task_instances[(DAG_ID, task, self.dag_run_id, -1)] = "upstream_failed"
            self.world.task_try_numbers[(DAG_ID, task, self.dag_run_id, -1)] = 0
        self.world.dag_runs[(DAG_ID, self.dag_run_id)] = "failed"
        self.document = self._document()
        self.log = self.tmp / "windows-job.json"
        self._write_document(self.document)

    def _document(self) -> dict:
        return {
            "schema_version": "solidworks-to-urdf.job/v1",
            "pipeline_id": PIPELINE_ID,
            "run_id": self.native_run,
            "request": {
                "run_id": self.native_run,
                "main_assembly": self.main_assembly,
                "upload_sha256": self.upload_sha,
                "upload_files": dict(self.upload_files),
            },
            "selection": {
                "state": "reading",
                "staged_files": dict(self.upload_files),
                "upload_sha256": self.upload_sha,
            },
            "status": "failed",
            "error": "SelectionError: the staged main opened with CAD-reported errors",
            "error_code": "selection_open_errors",
            "detail": {
                "open_status": {"errors": 2, "warnings": 1},
                "problems": [
                    {
                        "code": "selection_open_errors",
                        "document": self.main_assembly,
                        "message": "the staged main opened with CAD-reported errors",
                    }
                ],
            },
            "events": [
                {
                    "stage": "freeze",
                    "state": "failed",
                    "error": "SelectionError: the staged main opened with CAD-reported errors",
                }
            ],
        }

    def _write_document(self, document: dict) -> None:
        self.log.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8")

    def _set_platform_main(self, main_assembly: str) -> None:
        request_id = f"portal-{self.dag_run_id}"
        for index, row in enumerate(self.world.requests):
            if row[0] == request_id:
                self.world.requests[index] = (*row[:3], main_assembly, *row[4:])
                return
        raise AssertionError("test request row is missing")

    def _reconcile(self) -> dict:
        return {
            "operation_key": f"operatorctl-reconcile:{self.run_id}:discover:failed",
            "params_digest": digest(
                {
                    "outcome": "failed",
                    "evidence": [str(self.log)],
                    "operation": self.native_id,
                    "task": "discover",
                    "map_index": -1,
                }
            ),
            "evidence": [str(self.log)],
            "binding": {"operation_id": self.native_id, "task_id": "discover", "map_index": -1},
            "native_receipt": {"path": str(self.log), "native_run_id": self.native_run},
            "reported_stage": "freeze",
        }

    def _apply(self) -> None:
        self.store.confirm_stage(
            run_id=self.run_id,
            stage="discover",
            state="failed",
            reconcile=self._reconcile(),
        )

    def test_bound_native_discover_closes_once_and_replays_without_writes(self) -> None:
        self._apply()
        stages = self.store.stage_records(self.run_id)
        self.assertEqual(stages["freeze"]["state"], "completed")
        self.assertEqual(stages["discover"]["state"], "failed")
        self.assertEqual(
            [stages[task]["state"] for task in ("capture", "generate", "verify", "publish")],
            ["blocked", "blocked", "blocked", "blocked"],
        )
        receipt = stages["discover"]["receipt"]
        self.assertEqual(receipt["reported_stage"], "freeze")
        self.assertEqual(receipt["actual_stage"], "discover")
        self.assertEqual(receipt["native_run_id"], self.native_run)
        self.assertEqual(receipt["native_receipt_sha256"], file_digest(self.log))
        self.assertEqual(self.store.run_record(self.run_id)["state"], "failed")
        self.assertEqual(self.world.operations[self.native_key][1], "failed")
        reconcile_row = self.world.operations[f"operatorctl-reconcile:{self.run_id}:discover:failed"]
        self.assertEqual(reconcile_row[1], "succeeded")
        binding = [
            json.loads(payload) if isinstance(payload, str) else payload
            for _run_id, _seq, kind, _stage, _operation_id, payload in self.world.events
            if kind == "binding"
        ]
        self.assertEqual(len(binding), 1)
        self.assertEqual(binding[0]["reported_stage"], "freeze")
        self.assertEqual(binding[0]["actual_stage"], "discover")
        self.assertEqual(binding[0]["task_id"], "discover")
        self.assertEqual(binding[0]["map_index"], -1)

        before = self.world.snapshot_state()
        self._apply()
        self.assertEqual(self.world.snapshot_state(), before, "the identical recovery is a no-op")

    def test_bound_running_discover_with_its_own_failed_event_reconciles_once(self) -> None:
        self.document["events"][0]["stage"] = "discover"
        self._write_document(self.document)
        reconcile = self._reconcile()
        reconcile.pop("reported_stage")

        def apply() -> None:
            self.store.confirm_stage(
                run_id=self.run_id,
                stage="discover",
                state="failed",
                reconcile=reconcile,
            )

        apply()
        stages = self.store.stage_records(self.run_id)
        self.assertEqual(stages["freeze"]["state"], "completed")
        self.assertEqual(stages["discover"]["state"], "failed")
        self.assertEqual(
            [stages[task]["state"] for task in ("capture", "generate", "verify", "publish")],
            ["blocked", "blocked", "blocked", "blocked"],
        )
        receipt = stages["discover"]["receipt"]
        self.assertEqual(receipt["reported_stage"], "discover")
        self.assertEqual(receipt["actual_stage"], "discover")
        self.assertEqual(receipt["closure_shape"], "bound_native_discover_failed")
        self.assertEqual(self.store.run_record(self.run_id)["state"], "failed")
        self.assertEqual(self.world.operations[self.native_key][1], "failed")

        before = self.world.snapshot_state()
        apply()
        self.assertEqual(self.world.snapshot_state(), before, "the identical direct discover recovery is a no-op")

        (self.sealed_root / "input" / "handoff.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(PlatformStoreError, "sealed freeze cannot be verified"):
            apply()
        self.assertEqual(
            self.world.snapshot_state(),
            before,
            "a repeated direct recovery must revalidate the frozen bytes and change nothing",
        )

    def test_wrong_selection_or_airflow_fact_leaves_zero_writes(self) -> None:
        variants = []
        for name, mutate in (
            ("selection_not_reading", lambda d: d["selection"].__setitem__("state", "sealed")),
            ("wrong_error_code", lambda d: d.__setitem__("error_code", "other_error")),
            (
                "wrong_open_counts",
                lambda d: d["detail"]["open_status"].__setitem__("errors", 0),
            ),
            (
                "wrong_main",
                lambda d: d["request"].__setitem__("main_assembly", "other.SLDASM"),
            ),
        ):
            variants.append((name, mutate, "document"))
        variants.append(
            (
                "wrong_native_task",
                lambda: self.world.operation_bindings.__setitem__(
                    self.native_id, (DAG_ID, self.dag_run_id, "capture", -1)
                ),
                "world",
            )
        )
        variants.append(
            (
                "wrong_upload",
                lambda d: (
                    d["request"].__setitem__("upload_sha256", "f" * 64),
                    d["selection"].__setitem__("upload_sha256", "f" * 64),
                ),
                "document",
            )
        )
        variants.append(
            (
                "dag_run_success",
                lambda: self.world.dag_runs.__setitem__((DAG_ID, self.dag_run_id), "success"),
                "world",
            )
        )
        variants.append(
            (
                "successor_executed",
                lambda: self.world.task_instances.__setitem__(
                    (DAG_ID, "capture", self.dag_run_id, -1), "success"
                ),
                "world",
            )
        )
        variants.append(
            (
                "wrong_operation_uuid",
                lambda: setattr(self, "native_id", "deadbeef-0000-4000-8000-000000000000"),
                "world",
            )
        )
        variants.append(
            (
                "main_not_in_upload_files",
                lambda: self._set_platform_main("missing.SLDASM"),
                "missing_main",
            )
        )
        variants.append(
            (
                "discover_task_green",
                lambda: self.world.task_instances.__setitem__(
                    (DAG_ID, "discover", self.dag_run_id, -1), "success"
                ),
                "world",
            )
        )
        variants.append(
            (
                "sealed_freeze_missing",
                lambda: shutil.rmtree(self.sealed_root),
                "side_effect",
            )
        )
        variants.append(
            (
                "sealed_freeze_corrupt",
                lambda: (self.sealed_root / "input" / "cad" / "cad" / "robot.SLDASM").write_bytes(b"corrupt"),
                "side_effect",
            )
        )
        for name, mutate, kind in variants:
            with self.subTest(name=name):
                self.setUp()
                document = json.loads(json.dumps(self.document))
                if kind == "document":
                    mutate(document)
                    self._write_document(document)
                elif kind == "missing_main":
                    mutate()
                    document["request"]["main_assembly"] = "missing.SLDASM"
                    self._write_document(document)
                else:
                    mutate()
                before = self.world.snapshot_state()
                with self.assertRaises(PlatformStoreError):
                    self._apply()
                self.assertEqual(self.world.snapshot_state(), before, f"{name} changed state")


PREDISPATCH_RUN = "9c1f5e42-3b7d-4a8e-b0c2-5d6f7a8b9c0d"


class PredispatchClosureTests(unittest.TestCase):
    """The admitted-before-dispatch closure through the one canonical reconcile entry."""

    DAG_RUN = "portal-predispatch-1"
    EXCEPTION = "PLATFORM_CONFIG_DIGEST is unavailable: the controlled platform configuration identity is required"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="predispatch-closure-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.world = FakeWorld()
        self.store = PlatformStore(self.world.connect)

    # ------------------------------------------------------------------ fixtures
    def _snapshot(self) -> dict:
        return self.world.snapshot_state()

    def _admit(self, *, dag_run_id: str = DAG_RUN, stage: str = "capture") -> str:
        self.store.admit_run(
            request_id=f"portal-{dag_run_id}",
            run_id=PREDISPATCH_RUN,
            principal="portal",
            target_hardware="m3.0",
            main_assembly="3.0 总装.SLDASM",
            dispatch={
                "stage": stage,
                "operation_key": f"portal-{dag_run_id}/{stage}:0",
                "params_digest": "a" * 64,
                "dag_id": "solidworks_to_urdf",
                "dag_run_id": dag_run_id,
            },
        )
        return PREDISPATCH_RUN

    def _airflow_facts(
        self,
        *,
        dag_run_id: str = DAG_RUN,
        freeze: str = "failed",
        dag_run: str = "failed",
        freeze_map: int = -1,
        try_number: int = 1,
        successors: dict[str, str] | None = None,
    ) -> None:
        states = {
            "discover": "upstream_failed",
            "capture": "upstream_failed",
            "generate": "upstream_failed",
            "verify": "upstream_failed",
            "publish": "upstream_failed",
        }
        states.update(successors or {})
        self.world.task_instances[("solidworks_to_urdf", "freeze", dag_run_id, freeze_map)] = freeze
        self.world.task_try_numbers[("solidworks_to_urdf", "freeze", dag_run_id, freeze_map)] = try_number
        for task, state in states.items():
            self.world.task_instances[("solidworks_to_urdf", task, dag_run_id, -1)] = state
            self.world.task_try_numbers[("solidworks_to_urdf", task, dag_run_id, -1)] = 0
        self.world.dag_runs[("solidworks_to_urdf", dag_run_id)] = dag_run

    def _log(
        self,
        *,
        dag_run_id: str = DAG_RUN,
        task_id: str = "freeze",
        map_index: int = -1,
        try_number: int = 1,
        exc_value: str = EXCEPTION,
    ) -> Path:
        record = {
            "timestamp": "2026-10-16T13:51:10.237927Z",
            "level": "error",
            "event": "Task failed with exception",
            "try_number": try_number,
            "run_id": dag_run_id,
            "task_id": task_id,
            "dag_id": "solidworks_to_urdf",
            "map_index": map_index,
            "error_detail": [{"exc_type": "AirflowFailException", "exc_value": exc_value, "exc_notes": []}],
        }
        path = self.tmp / f"failure-{task_id}-{map_index}-{len(list(self.tmp.iterdir()))}.log"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        return path

    def _reconcile(self, log: Path) -> dict:
        evidence = [str(log)]
        return {
            "operation_key": f"operatorctl-reconcile:{PREDISPATCH_RUN}:freeze:failed",
            "evidence": evidence,
            "params_digest": digest({"outcome": "failed", "evidence": evidence, "binding": None}),
        }

    def _close(self, log: Path) -> None:
        self.store.confirm_stage(run_id=PREDISPATCH_RUN, stage="freeze", state="failed", reconcile=self._reconcile(log))

    # ------------------------------------------------------------------ positive
    def test_the_closure_binds_every_initial_intent_to_the_real_failed_freeze_task(self) -> None:
        run_id = self._admit()
        self._airflow_facts()
        log = self._log()
        self._close(log)
        freeze = self.world.stages[(run_id, "freeze")]
        self.assertEqual(freeze[0], "failed")
        receipt = freeze[1]
        self.assertEqual(receipt["error"], f"AirflowFailException: {self.EXCEPTION}")
        self.assertNotIn("admitted_before_dispatch", receipt)
        self.assertEqual(receipt["closure_shape"], "freeze_dispatch_failed")
        self.assertEqual(
            receipt["freeze_execution"],
            {"task_id": "freeze", "map_index": -1, "try_number": 1, "state": "failed"},
        )
        self.assertEqual(
            receipt["airflow_failure"],
            {"path": str(log), "sha256": file_digest(log), "try_number": 1},
        )
        for later in ("discover", "capture", "generate", "verify", "publish"):
            self.assertEqual(self.world.stages[(run_id, later)][0], "blocked")
        self.assertEqual(self.world.run_states[run_id], "failed")
        intent = self.world.operations["portal-portal-predispatch-1/capture:0"]
        self.assertEqual(intent[1], "failed", "the real failed freeze dispatch closes the initial intent")
        self.assertEqual(
            self.world.operation_bindings[intent[0]],
            ("solidworks_to_urdf", "portal-predispatch-1", "freeze", -1),
            "the linked start boundary never substitutes for the task that actually executed",
        )
        self.assertEqual(self.world.operations[f"operatorctl-reconcile:{run_id}:freeze:failed"][1], "succeeded")
        kinds = [row[2] for row in self.world.events if row[0] == run_id]
        self.assertEqual(kinds, ["stage", "stage", "stage", "stage", "stage", "binding", "stage"])
        binding = next(json.loads(row[5]) for row in self.world.events if row[0] == run_id and row[2] == "binding")
        self.assertEqual(binding["intent_stage"], "capture")
        self.assertEqual(binding["task_id"], "freeze")
        self.assertEqual(binding["map_index"], -1)
        self.assertTrue(binding["freeze_dispatched"])

    def test_the_same_evidence_replays_as_a_no_op(self) -> None:
        self._admit()
        self._airflow_facts()
        log = self._log()
        self._close(log)
        before = self._snapshot()
        self._close(log)
        self.assertEqual(self._snapshot(), before, "the identical whole action repeats as a no-op")

    def test_a_legacy_receipt_without_try_number_replays_as_a_no_op(self) -> None:
        run_id = self._admit()
        self._airflow_facts()
        log = self._log()
        self._close(log)
        receipt = dict(self.world.stages[(run_id, "freeze")][1])
        legacy = {key: value for key, value in receipt.items() if key not in {"closure_shape", "freeze_execution"}}
        legacy["airflow_failure"] = {
            "path": str(log),
            "sha256": file_digest(log),
        }
        legacy["admitted_before_dispatch"] = True
        self.world.stages[(run_id, "freeze")] = ("failed", legacy, None, None, None)
        before = self._snapshot()
        self._close(log)
        self.assertEqual(self._snapshot(), before, "the sealed path and bytes alone keep the replay idempotent")

    def test_a_replay_with_changed_evidence_bytes_is_refused(self) -> None:
        self._admit()
        self._airflow_facts()
        log = self._log()
        self._close(log)
        before = self._snapshot()
        with log.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaisesRegex(PlatformStoreError, "does not match the recorded evidence"):
            self._close(log)
        self.assertEqual(self._snapshot(), before)

    def test_a_failed_freeze_intent_is_bound_and_settles_failed(self) -> None:
        run_id = self._admit(stage="freeze")
        self._airflow_facts(try_number=3)
        log = self._log(try_number=3)
        self._close(log)
        freeze = self.world.stages[(run_id, "freeze")]
        self.assertEqual(freeze[0], "failed")
        receipt = freeze[1]
        self.assertEqual(receipt["closure_shape"], "freeze_dispatch_failed")
        self.assertNotIn("admitted_before_dispatch", receipt)
        self.assertEqual(
            receipt["freeze_execution"],
            {"task_id": "freeze", "map_index": -1, "try_number": 3, "state": "failed"},
        )
        intent = self.world.operations["portal-portal-predispatch-1/freeze:0"]
        self.assertEqual(intent[1], "failed", "the executed freeze intent settles failed")
        self.assertEqual(
            self.world.operation_bindings[intent[0]],
            ("solidworks_to_urdf", "portal-predispatch-1", "freeze", -1),
            "the freeze binding comes from the real failed task row",
        )
        for later in ("discover", "capture", "generate", "verify", "publish"):
            self.assertEqual(self.world.stages[(run_id, later)][0], "blocked")
        self.assertEqual(self.world.run_states[run_id], "failed")
        binding_events = [json.loads(row[5]) for row in self.world.events if row[0] == run_id and row[2] == "binding"]
        self.assertEqual(len(binding_events), 1)
        self.assertEqual(binding_events[0]["intent_stage"], "freeze")
        self.assertEqual(binding_events[0]["task_id"], "freeze")
        self.assertEqual(binding_events[0]["try_number"], 3)
        self.assertTrue(binding_events[0]["freeze_dispatched"])
        self.assertEqual(binding_events[0]["closure_shape"], "freeze_dispatch_failed")

    def test_the_failed_freeze_intent_replays_only_with_identical_original_bytes(self) -> None:
        self._admit(stage="freeze")
        self._airflow_facts(try_number=3)
        log = self._log(try_number=3)
        self._close(log)
        before = self._snapshot()
        self._close(log)
        self.assertEqual(self._snapshot(), before)
        with log.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self.assertRaisesRegex(PlatformStoreError, "does not match the recorded evidence"):
            self._close(log)
        self.assertEqual(self._snapshot(), before)

    def test_the_failed_freeze_intent_requires_the_terminal_attempt_and_real_facts(self) -> None:
        cases = ("earlier_attempt", "bound_intent", "successor_executed", "dag_run_running")
        for name in cases:
            with self.subTest(name=name):
                self.setUp()
                self._admit(stage="freeze")
                self._airflow_facts(
                    try_number=3,
                    dag_run="running" if name == "dag_run_running" else "failed",
                    successors={"capture": "success"} if name == "successor_executed" else None,
                )
                log = self._log(try_number=1 if name == "earlier_attempt" else 3)
                if name == "bound_intent":
                    intent_id = self.world.operations["portal-portal-predispatch-1/freeze:0"][0]
                    self.world.operation_bindings[intent_id] = (
                        "solidworks_to_urdf",
                        self.DAG_RUN,
                        "freeze",
                        -1,
                    )
                before = self._snapshot()
                with self.assertRaises(PlatformStoreError):
                    self._close(log)
                self.assertEqual(self._snapshot(), before, "a refused freeze closure leaves zero change")

    # ------------------------------------------------------------------ refusals
    def test_every_executed_unresolved_or_ambiguous_shape_refuses_with_zero_change(self) -> None:
        cases = (
            "run_running",
            "stage_rows",
            "intent_claimed",
            "intent_bound",
            "freeze_upstream_failed",
            "freeze_map_mismatch",
            "capture_failed",
            "capture_running",
            "generate_executed",
            "dag_run_running",
            "ambiguous_freeze_rows",
            "log_another_dag_run",
            "log_not_freeze",
        )
        for name in cases:
            with self.subTest(name=name):
                self.setUp()
                run_id = self._admit()
                self._airflow_facts()
                log = self._log()
                if name == "run_running":
                    self.world.run_states[run_id] = "running"
                elif name == "stage_rows":
                    self.store.confirm_stage(run_id=run_id, stage="discover", state="blocked")
                elif name == "intent_claimed":
                    self.store.start_run(run_id)
                    self.store.claim_operation(
                        run_id=run_id, stage="capture", operation_key="portal-portal-predispatch-1/capture:0"
                    )
                elif name == "intent_bound":
                    intent_id = self.world.operations["portal-portal-predispatch-1/capture:0"][0]
                    self.world.operation_bindings[intent_id] = (
                        "solidworks_to_urdf",
                        self.DAG_RUN,
                        "capture",
                        -1,
                    )
                elif name == "freeze_upstream_failed":
                    self.world.task_instances[("solidworks_to_urdf", "freeze", self.DAG_RUN, -1)] = "upstream_failed"
                elif name == "freeze_map_mismatch":
                    self.world.task_instances.pop(("solidworks_to_urdf", "freeze", self.DAG_RUN, -1))
                    self.world.task_instances[("solidworks_to_urdf", "freeze", self.DAG_RUN, 0)] = "failed"
                elif name == "capture_failed":
                    self.world.task_instances[("solidworks_to_urdf", "capture", self.DAG_RUN, -1)] = "failed"
                elif name == "capture_running":
                    self.world.task_instances[("solidworks_to_urdf", "capture", self.DAG_RUN, -1)] = "running"
                elif name == "generate_executed":
                    self.world.task_instances[("solidworks_to_urdf", "generate", self.DAG_RUN, -1)] = "success"
                elif name == "dag_run_running":
                    self.world.dag_runs[("solidworks_to_urdf", self.DAG_RUN)] = "running"
                elif name == "ambiguous_freeze_rows":
                    self.world.task_instances[("solidworks_to_urdf", "freeze", self.DAG_RUN, 0)] = "failed"
                elif name == "log_another_dag_run":
                    log = self._log(dag_run_id="portal-predispatch-2")
                elif name == "log_not_freeze":
                    log = self._log(task_id="discover")
                before = self._snapshot()
                with self.assertRaises(PlatformStoreError):
                    self._close(log)
                self.assertEqual(self._snapshot(), before, "a refused closure leaves zero change")

    def test_a_caller_supplied_marker_receipt_cannot_open_the_closure(self) -> None:
        run_id = self._admit()
        self._airflow_facts()
        log = self._log()
        forged = {
            "schema_version": "solidworks-to-urdf.bundle/v1",
            "pipeline_id": "solidworks-to-urdf",
            "run_id": run_id,
            "stage": "freeze",
            "state": "failed",
            "passed": False,
            "error": "FORGED MESSAGE THAT IS NOT IN THE LOG",
            "admitted_before_dispatch": True,
            "airflow_failure": {"path": str(log), "sha256": file_digest(log)},
        }
        reconcile = self._reconcile(log)
        reconcile["evidence"] = [str(log), str(log)]
        before = self._snapshot()
        with self.assertRaises(PlatformStoreError):
            self.store.confirm_stage(
                run_id=run_id, stage="freeze", state="failed", receipt=dict(forged), reconcile=reconcile
            )
        self.assertEqual(self._snapshot(), before, "a caller-supplied marker settles nothing")

    def test_a_forged_receipt_never_replaces_the_internally_composed_one(self) -> None:
        run_id = self._admit()
        self._airflow_facts()
        log = self._log()
        forged = {
            "schema_version": "solidworks-to-urdf.bundle/v1",
            "pipeline_id": "solidworks-to-urdf",
            "run_id": run_id,
            "stage": "freeze",
            "state": "failed",
            "passed": False,
            "error": "FORGED MESSAGE THAT IS NOT IN THE LOG",
            "admitted_before_dispatch": True,
            "airflow_failure": {"path": str(log), "sha256": file_digest(log)},
        }
        self.store.confirm_stage(
            run_id=run_id, stage="freeze", state="failed", receipt=dict(forged), reconcile=self._reconcile(log)
        )
        stored = self.world.stages[(run_id, "freeze")][1]
        self.assertEqual(stored["error"], f"AirflowFailException: {self.EXCEPTION}")
        self.assertNotIn("FORGED", json.dumps(stored))


if __name__ == "__main__":
    unittest.main()
