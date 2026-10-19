"""The application-side platform store API.

One module, one transaction entry: admission close/open, ONE new-admission
entry that registers a request with its run and initial dispatch intent in a
single short transaction (all-or-nothing), qualified registration of
successor operations for admitted runs (cancel/reconcile included),
stage-confirmation writes and read-only queries over the two lifecycle
projection views.

The connection is injected - this module carries no import-time database
driver dependency, adds no HTTP service and no second controller; the schema,
views and roles belong to the deploy side (``deploy/platform``).  Every call
opens one fresh connection, commits or rolls back exactly one transaction and
closes the connection again; no transaction ever spans an external call.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..delivery import BUNDLE_SCHEMA, PIPELINE_ID, read_delivery_manifest
from ..io import PipelineError, artifact_path_parts, canonical, confined, digest, file_digest, read_data
from ..orchestration.airflow_client import JOB_SCHEMA, native_run_id
from ..stages import (
    CONFIRMED_STAGE_PASSED,
    CONFIRMED_STAGE_STATES,
    DELIVERY_STAGES,
    STAGE_EVIDENCE,
    STAGE_IDS,
    STAGE_STATES,
    TERMINAL_STAGE_STATES,
)
from .storage import SEALED_URI_PREFIX, parse_sealed_uri, sealed_resolver_from_environment


class PlatformStoreError(PipelineError):
    """The platform store refused a write or a read."""


class AdmissionClosedError(PlatformStoreError):
    """A new admission was attempted while the admission gate is closed."""

    code = "admission_closed"


class OpenAttemptConflictError(PlatformStoreError):
    """A parent run already holds a non-terminal linked attempt.

    The reservation is enforced inside the admission transaction (under the
    admission row lock), so two concurrent submissions can never each admit a
    child of the same parent.
    """

    code = "open_attempt_conflict"

    def __init__(self, parent_run_id: str, run_id: str, request_id: str):
        super().__init__(f"The parent run {parent_run_id!r} already holds the open attempt {run_id!r} ({request_id!r})")
        self.parent_run_id = parent_run_id
        self.run_id = run_id
        self.request_id = request_id


class RunNotAdmittedError(PlatformStoreError):
    """A display write named a run that has no platform row."""

    code = "run_not_admitted"


class OperationConflictError(PlatformStoreError):
    """The same operation key reappeared with a different scope or parameters."""

    code = "operation_conflict"


@dataclass(frozen=True)
class AdmissionView:
    state: str
    reason: str
    actor: str
    changed_at: datetime
    revision: int


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    state: str
    operation_key: str
    params_digest: str
    run_id: str
    stage: str
    kind: str


@dataclass(frozen=True)
class OperationClaim:
    """One claim result: the record plus whether this call took the execution grant."""

    record: OperationRecord
    granted: bool


@dataclass(frozen=True)
class NativeReceiptEvidence:
    """The immutable, byte-bound native failed-receipt fact a maintenance action carries.

    Produced only by :func:`load_native_failed_receipt`: the original receipt bytes are
    read once, their SHA-256 is computed from those bytes, and the native identity fields
    are validated against the bound endpoint-job UUID and stage before this value
    exists.  The reconcile action consumes exactly this fact; nothing downstream
    re-derives, fills in or rewrites its terminal state.
    """

    path: str
    sha256: str
    native_run_id: str
    stage: str
    reported_stage: str
    state: str
    error: str | None
    error_code: str | None
    detail: object
    request_main_assembly: str | None
    request_upload_sha256: str | None
    request_upload_files: dict[str, str] | None
    selection_state: str | None
    selection_files: dict[str, str] | None
    selection_upload_sha256: str | None


#: The job contract's final states: a terminal event row is one of these, and only a
#: ``failed`` terminal row authorizes the failure reconciliation.
NATIVE_TERMINAL_STATES = frozenset({"passed", "failed"})


def load_native_failed_receipt(
    path: object,
    *,
    stage: str,
    native_run_id: str,
    reported_stage: str | None = None,
) -> NativeReceiptEvidence:
    """Load and validate one original native failed receipt - the single acceptance source.

    The bytes on disk are the authority: they are read once, the receipt digest is
    computed from them, and the parsed document must be the bound endpoint job's own
    terminal failure under the real native job contract (``JOB_SCHEMA``): its identity
    fields name exactly the bound UUID, its request carries the same UUID, its status is
    ``failed``, and its events carry exactly one terminal row - ``failed`` at the reported
    stage.  The one exceptional reported/actual pairing is the historical selection
    failure at ``discover`` whose old executor labeled the terminal row as ``freeze``;
    that shape also requires a selection-read failure with a positive non-boolean error
    count.  A missing file, malformed bytes, a report-shaped document or any other
    identity/state refuses here, before the store transaction is ever entered; every
    caller (the operator entry and the maintenance route alike) shares this one
    implementation.
    """

    receipt_path = Path(str(path))
    try:
        raw = receipt_path.read_bytes()
    except OSError as error:
        raise PlatformStoreError(f"The native failed receipt is unreadable: {error}") from error
    sha256 = hashlib.sha256(raw).hexdigest()
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise PlatformStoreError("The native failed receipt is not a JSON document") from error
    if not isinstance(document, dict):
        raise PlatformStoreError("The native failed receipt must be a JSON object")
    if document.get("schema_version") != JOB_SCHEMA:
        raise PlatformStoreError("The native receipt is not a native job document under the job contract")
    if document.get("pipeline_id") != PIPELINE_ID:
        raise PlatformStoreError("The native failed receipt names another pipeline")
    if not _is_canonical_uuid(native_run_id):
        raise PlatformStoreError("The bound endpoint job UUID is not canonical")
    if document.get("run_id") != native_run_id:
        raise PlatformStoreError("The native failed receipt names another endpoint job")
    request = document.get("request")
    if not isinstance(request, dict) or request.get("run_id") != native_run_id:
        raise PlatformStoreError("The native receipt's request does not carry the bound endpoint job")
    if document.get("status") != "failed":
        raise PlatformStoreError("The native receipt is not the bound job's failed terminal fact")
    events = [event for event in document.get("events") or [] if isinstance(event, dict)]
    terminal = [event for event in events if event.get("state") in NATIVE_TERMINAL_STATES]
    if len(terminal) != 1:
        raise PlatformStoreError("The native receipt must carry exactly one terminal event row")
    if terminal[0].get("state") != "failed":
        raise PlatformStoreError("The native receipt's terminal event is not a failure")
    reported_stage = reported_stage if reported_stage is not None else stage
    if reported_stage != stage and (stage, reported_stage) != ("discover", "freeze"):
        raise PlatformStoreError(
            "The native receipt's reported and actual stages are not the one supported recovery shape"
        )
    if terminal[0].get("stage") != reported_stage:
        raise PlatformStoreError("The native receipt's terminal event names another stage")

    detail = document.get("detail")
    if detail is None:
        stage_document = document.get("stages")
        stage_rows = stage_document.get("stages") if isinstance(stage_document, dict) else None
        matches = [
            row
            for row in (stage_rows if isinstance(stage_rows, list) else [])
            if isinstance(row, dict) and row.get("id") == stage and row.get("state") == "failed"
        ]
        if len(matches) == 1:
            detail = matches[0].get("diagnostic")
    error = terminal[0].get("error")
    if not isinstance(error, str) or not error:
        error = document.get("error")
    error_code = document.get("error_code")
    selection = document.get("selection")
    if reported_stage != stage:
        if not isinstance(selection, dict) or selection.get("state") != "reading":
            raise PlatformStoreError("The reported freeze-stage native failure is not a selection read in progress")
        if error_code != "selection_open_errors":
            raise PlatformStoreError("The reported freeze-stage native failure is not a selection-open failure")
        open_status = detail.get("open_status") if isinstance(detail, dict) else None
        if (
            not isinstance(open_status, dict)
            or type(open_status.get("errors")) is not int
            or isinstance(open_status.get("errors"), bool)
            or open_status.get("errors") <= 0
            or type(open_status.get("warnings")) is not int
            or isinstance(open_status.get("warnings"), bool)
            or open_status.get("warnings") < 0
        ):
            raise PlatformStoreError("The reported freeze-stage native failure lacks valid selection-open counts")
    request_files = request.get("upload_files")
    request_main = request.get("main_assembly")
    request_upload = request.get("upload_sha256")
    selection_upload = selection.get("upload_sha256") if isinstance(selection, dict) else None
    selection_files = selection.get("staged_files") if isinstance(selection, dict) else None

    return NativeReceiptEvidence(
        path=str(receipt_path),
        sha256=sha256,
        native_run_id=native_run_id,
        stage=stage,
        reported_stage=reported_stage,
        state="failed",
        error=error if isinstance(error, str) and error else None,
        error_code=error_code if isinstance(error_code, str) and error_code else None,
        detail=detail,
        request_main_assembly=request_main if isinstance(request_main, str) and request_main else None,
        request_upload_sha256=request_upload if isinstance(request_upload, str) and request_upload else None,
        request_upload_files=request_files if isinstance(request_files, dict) else None,
        selection_state=selection.get("state") if isinstance(selection, dict) else None,
        selection_files=selection_files if isinstance(selection_files, dict) else None,
        selection_upload_sha256=selection_upload if isinstance(selection_upload, str) and selection_upload else None,
    )


@dataclass(frozen=True)
class AirflowTaskFailureEvidence:
    """The immutable, byte-bound Airflow task-failure fact a maintenance action carries.

    Produced only by :func:`load_airflow_task_failure`: the original log bytes are read
    once, their SHA-256 is computed from those bytes, and the record's own scheduling
    identity plus the verbatim exception pair are validated before this value exists.
    The admitted closure consumes exactly this fact; nothing downstream re-derives,
    fills in or rewrites it, and no native receipt is ever composed from it.
    """

    path: str
    sha256: str
    dag_id: str
    run_id: str
    task_id: str
    map_index: int
    try_number: int
    exc_type: str
    exc_value: str


def load_airflow_task_failure(path: object) -> AirflowTaskFailureEvidence:
    """Load one original Airflow task-failure log - the single acceptance source.

    The bytes on disk are the authority: the Airflow task's own structured log carries one
    JSON record per line, exactly one of which is the task's own failure (``Task failed
    with exception``) with its complete scheduling identity (dag_id, run_id, task_id,
    map_index, try_number) and its verbatim exception pair.  Nothing is defaulted or
    guessed: a missing file, unreadable or non-JSON bytes, zero or several failure
    records, or an incomplete identity/text refuses here.
    """

    log_path = Path(str(path))
    try:
        raw = log_path.read_bytes()
    except OSError as error:
        raise PlatformStoreError(f"The Airflow failure log is unreadable: {error}") from error
    sha256 = hashlib.sha256(raw).hexdigest()
    records: list[dict] = []
    for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise PlatformStoreError(f"The Airflow failure log line {number} is not a JSON record") from error
        if not isinstance(record, dict):
            raise PlatformStoreError(f"The Airflow failure log line {number} is not a JSON object")
        records.append(record)
    failures = [record for record in records if record.get("event") == "Task failed with exception"]
    if len(failures) != 1:
        raise PlatformStoreError("The Airflow failure log must carry exactly one task-failure record")
    record = failures[0]
    dag_id = record.get("dag_id")
    run_id = record.get("run_id")
    task_id = record.get("task_id")
    map_index = record.get("map_index")
    try_number = record.get("try_number")
    if not (
        isinstance(dag_id, str)
        and dag_id
        and isinstance(run_id, str)
        and run_id
        and isinstance(task_id, str)
        and task_id
        and type(map_index) is int
        and type(try_number) is int
    ):
        raise PlatformStoreError("The Airflow failure record lacks its scheduling identity fields")
    details = record.get("error_detail")
    if not isinstance(details, list) or len(details) != 1 or not isinstance(details[0], dict):
        raise PlatformStoreError("The Airflow failure record must carry exactly one exception detail")
    exc_type = details[0].get("exc_type")
    exc_value = details[0].get("exc_value")
    if not (isinstance(exc_type, str) and exc_type and isinstance(exc_value, str) and exc_value):
        raise PlatformStoreError("The Airflow failure record lacks its verbatim exception text")
    return AirflowTaskFailureEvidence(
        path=str(log_path),
        sha256=sha256,
        dag_id=dag_id,
        run_id=run_id,
        task_id=task_id,
        map_index=map_index,
        try_number=try_number,
        exc_type=exc_type,
        exc_value=exc_value,
    )


def _report_member(path: object) -> bool:
    """The one class projection: ``reports/**`` members are report-class."""

    return isinstance(path, str) and (path == "reports" or path.startswith("reports/"))


@dataclass(frozen=True)
class RunDisplay:
    """One run's display surface: the name and the reversible tombstone."""

    run_id: str
    display_name: str | None
    hidden_at: datetime | None
    hidden_reason: str | None
    updated_at: datetime


_LOCK_ADMISSION = "SELECT state, reason, actor, changed_at, revision FROM admission WHERE id = 1 FOR UPDATE"
_READ_ADMISSION = "SELECT state, reason, actor, changed_at, revision FROM admission_projection"
_UPDATE_ADMISSION = (
    "UPDATE admission SET state = %s, reason = %s, actor = %s, changed_at = now(),"
    " revision = revision + 1 WHERE id = 1 RETURNING state, reason, actor, changed_at, revision"
)
_INSERT_REQUEST = (
    "INSERT INTO request (request_id, principal, target_hardware, main_assembly, configuration, baseline)"
    " VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)"
)
_INSERT_RUN = (
    "INSERT INTO run (run_id, request_id, parent_run_id, from_stage, state, tool_identity)"
    " VALUES (%s, %s, %s, %s, 'queued', %s::jsonb)"
)
_INSERT_OPERATION = (
    "INSERT INTO operation"
    " (operation_id, run_id, stage, kind, state, operation_key, params_digest, dag_id, dag_run_id, task_id, map_index)"
    " VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (operation_key) DO NOTHING"
    " RETURNING operation_id, state, operation_key, params_digest, run_id, stage, kind"
)
_FIND_OPERATION = (
    "SELECT operation_id, state, operation_key, params_digest, run_id, stage, kind"
    " FROM operation WHERE operation_key = %s"
)
_LOCK_RUN = "SELECT state, from_stage FROM run WHERE run_id = %s FOR UPDATE"
_READ_RUN = "SELECT state, from_stage FROM run WHERE run_id = %s"
_READ_RUN_DISPLAY = "SELECT run_id, display_name, hidden_at, hidden_reason, updated_at FROM run WHERE run_id = %s"
_READ_RUN_DISPLAYS = (
    "SELECT run_id, display_name, hidden_at, hidden_reason, updated_at FROM run"
    " WHERE display_name IS NOT NULL OR hidden_at IS NOT NULL"
)
_SET_RUN_DISPLAY = (
    "UPDATE run SET display_name = %s, updated_at = now() WHERE run_id = %s"
    " RETURNING run_id, display_name, hidden_at, hidden_reason, updated_at"
)
_SET_RUN_HIDDEN = (
    "UPDATE run SET hidden_at = COALESCE(hidden_at, now()),"
    " hidden_reason = COALESCE(hidden_reason, %s), updated_at = now()"
    " WHERE run_id = %s RETURNING run_id, display_name, hidden_at, hidden_reason, updated_at"
)
_RESTORE_RUN = (
    "UPDATE run SET hidden_at = NULL, hidden_reason = NULL, updated_at = now()"
    " WHERE run_id = %s RETURNING run_id, display_name, hidden_at, hidden_reason, updated_at"
)
_START_RUN = (
    "UPDATE run SET state = 'running', updated_at = now() WHERE run_id = %s AND state = 'queued' RETURNING state"
)
_CLAIM_OPERATION = (
    "UPDATE operation SET state = 'running', updated_at = now()"
    " WHERE operation_key = %s AND run_id = %s AND stage = %s AND state = 'queued'"
    " RETURNING operation_id, state, operation_key, params_digest, run_id, stage, kind"
)
_BIND_MISSING_DISPATCH = (
    "UPDATE operation SET task_id = %s, map_index = %s, updated_at = now()"
    " WHERE operation_key = %s AND run_id = %s AND kind = 'dispatch' AND state = 'queued'"
    " AND dag_id = %s AND dag_run_id = %s"
    " AND task_id IS NULL AND map_index IS NULL"
    " RETURNING operation_id"
)
_READ_OPERATION_BINDING = (
    "SELECT operation_id, state, operation_key, params_digest, run_id, stage, kind,"
    " dag_id, dag_run_id, task_id, map_index"
    " FROM operation WHERE operation_key = %s FOR UPDATE"
)
_READ_RUN_DISPATCH_FOR_BINDING = (
    "SELECT operation_id, stage, state, dag_id, dag_run_id, task_id, map_index, operation_key"
    " FROM operation WHERE run_id = %s AND kind = 'dispatch' FOR UPDATE"
)
_READ_DISPATCH_BINDING_EVENTS = (
    "SELECT payload FROM run_event WHERE run_id = %s AND operation_id = %s"
    " AND kind = 'binding' AND payload->>'binding_kind' = 'dispatch' ORDER BY seq"
)
_READ_DISPATCH_TASK_FACT = (
    "SELECT ti.map_index, ti.state, ti.try_number, dr.state"
    " FROM task_instance ti JOIN dag_run dr"
    " ON dr.dag_id = ti.dag_id AND dr.run_id = ti.run_id"
    " WHERE ti.dag_id = %s AND ti.run_id = %s AND ti.task_id = %s"
)
_BIND_LEGACY_DISPATCH = (
    "UPDATE operation SET task_id = %s, map_index = %s, updated_at = now()"
    " WHERE operation_id = %s AND run_id = %s AND kind = 'dispatch'"
    " AND task_id IS NULL AND map_index IS NULL RETURNING operation_id"
)
_SET_RUN_OUTCOME = (
    "UPDATE run SET state = %s, updated_at = now()"
    " WHERE run_id = %s AND state NOT IN ('succeeded', 'failed', 'cancelled')"
)
_SET_RUN_UNRESOLVED = "UPDATE run SET state = 'unresolved', updated_at = now() WHERE run_id = %s AND state = 'running'"
_RESTORE_RUN_RUNNING = "UPDATE run SET state = 'running', updated_at = now() WHERE run_id = %s AND state = 'unresolved'"
_SETTLE_OPERATIONS = (
    "UPDATE operation SET state = CASE WHEN kind = 'cancel' THEN 'succeeded' ELSE %s END,"
    " updated_at = now()"
    " WHERE run_id = %s AND stage = %s AND kind NOT IN ('reconcile', 'cache_release')"
    " AND state IN ('queued', 'running', 'unresolved')"
)
_SETTLE_BLOCKED_OPERATIONS = (
    "UPDATE operation SET state = 'cancelled', updated_at = now()"
    " WHERE run_id = %s AND stage = %s AND kind NOT IN ('reconcile', 'cache_release')"
    " AND state = 'queued'"
)
_SETTLE_UNKNOWN = (
    "UPDATE operation SET state = 'unresolved', updated_at = now()"
    " WHERE run_id = %s AND stage = %s AND kind NOT IN ('reconcile', 'cache_release')"
    " AND state = 'running'"
)
_SETTLE_RECONCILE = (
    "UPDATE operation SET state = 'succeeded', updated_at = now()"
    " WHERE operation_id = %s AND run_id = %s AND stage = %s AND kind = 'reconcile'"
    " AND state <> 'succeeded'"
)
_LOCK_RUN_OPERATIONS = (
    "SELECT operation_id, stage, kind, state, dag_id, dag_run_id, task_id, map_index, operation_key"
    " FROM operation WHERE run_id = %s ORDER BY operation_id FOR UPDATE"
)
_COUNT_RUN_STAGES = "SELECT count(*) FROM stage WHERE run_id = %s"
#: One statement carries every six-stage task fact and the DagRun's real state: the UNION ALL
#: evaluates them in a single statement snapshot - the write-side read of the original
#: Airflow authority stays inside the one reconcile transaction, and a multi-row (ambiguous)
#: or missing key is never masked.
_READ_ADMITTED_FACTS = (
    "SELECT 'task'::text, ti.task_id, ti.map_index, ti.state, ti.try_number FROM task_instance ti"
    " WHERE ti.dag_id = %s AND ti.run_id = %s"
    " AND ti.task_id IN ('freeze', 'discover', 'capture', 'generate', 'verify', 'publish')"
    " UNION ALL"
    " SELECT 'dag_run'::text, NULL, NULL, dr.state, NULL FROM dag_run dr"
    " WHERE dr.dag_id = %s AND dr.run_id = %s"
)
_SETTLE_CACHE_RELEASE = (
    "UPDATE operation SET state = %s, updated_at = now()"
    " WHERE operation_key = %s AND run_id = %s AND stage = %s AND kind = 'cache_release'"
    " AND state = 'running'"
    " RETURNING operation_id, state, operation_key, params_digest, run_id, stage, kind"
)
_READ_RUN_RECORD = "SELECT state, request_id, parent_run_id, from_stage, tool_identity FROM run WHERE run_id = %s"
_READ_RUN_PARENT = "SELECT parent_run_id FROM run WHERE run_id = %s"
_OPEN_CHILD_RUN = (
    "SELECT run_id, request_id FROM run WHERE parent_run_id = %s"
    " AND state NOT IN ('succeeded', 'failed', 'cancelled')"
    " ORDER BY created_at, run_id LIMIT 1"
)
_READ_REQUEST_RECORD = (
    "SELECT principal, target_hardware, main_assembly, sealed_at,"
    " (SELECT o.params_digest FROM operation o"
    "  JOIN run u ON u.run_id = o.run_id"
    "  WHERE u.request_id = request.request_id AND o.kind = 'dispatch'"
    "  ORDER BY o.updated_at, o.operation_id LIMIT 1),"
    " configuration,"
    " baseline"
    " FROM request WHERE request_id = %s"
)
_READ_STAGE_RECORDS = (
    "SELECT stage, state, receipt, checkpoint_uri, reused_from_run, original_executed_at FROM stage WHERE run_id = %s"
)
_CONFIRMED_PUBLISH = "SELECT 1 FROM operation WHERE run_id = %s AND kind = 'publish' AND state = 'succeeded' LIMIT 1"
_READ_STAGE_ROW = "SELECT state, receipt, checkpoint_uri, reused_from_run FROM stage WHERE run_id = %s AND stage = %s"
_READ_STAGE = "SELECT state, receipt, checkpoint_uri, reused_from_run FROM stage WHERE run_id = %s AND stage = %s"
_READ_STAGE_TIME = "SELECT original_executed_at FROM stage WHERE run_id = %s AND stage = %s"
_LOCK_STAGE = "SELECT state FROM stage WHERE run_id = %s AND stage = %s FOR UPDATE"
_FIND_RECONCILE = (
    "SELECT 1 FROM operation WHERE operation_id = %s AND run_id = %s AND kind = 'reconcile' AND stage = %s LIMIT 1"
)
_FIND_UNKNOWN_ORIGIN = (
    "SELECT 1 FROM operation WHERE run_id = %s AND stage = %s AND kind <> 'reconcile' AND state = 'unresolved' LIMIT 1"
)
_READ_ORPHAN_OPERATION = (
    "SELECT stage, kind, state, dag_id, dag_run_id, task_id, map_index, operation_key"
    " FROM operation WHERE operation_id = %s AND run_id = %s FOR UPDATE"
)
_BIND_ORPHAN_OPERATION = (
    "UPDATE operation SET task_id = %s, map_index = %s, updated_at = now()"
    " WHERE operation_id = %s AND run_id = %s AND task_id IS NULL AND map_index IS NULL"
    " RETURNING operation_id"
)
#: One statement carries every lifecycle fact of the orphan acceptance: a single
#: query evaluates the view once, so the task's and the DagRun's states are always
#: the same database snapshot - two separate reads are never a snapshot.
_READ_ORPHAN_LIFECYCLE = (
    "SELECT object_kind, state, bucket FROM lifecycle_projection"
    " WHERE (object_kind = 'scheduling' AND object_id = %s)"
    "    OR (object_kind = 'dag_run' AND object_id = %s)"
)
_SETTLE_ORPHAN_INTENT = (
    "UPDATE operation SET state = %s, updated_at = now()"
    " WHERE operation_id = %s AND run_id = %s AND state = 'queued'"
    " AND kind NOT IN ('reconcile', 'cache_release')"
)
_INSERT_STAGE = (
    "INSERT INTO stage (run_id, stage, state, receipt, checkpoint_uri, reused_from_run, original_executed_at)"
    " VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)"
    " ON CONFLICT (run_id, stage) DO UPDATE SET"
    " state = EXCLUDED.state, receipt = EXCLUDED.receipt, checkpoint_uri = EXCLUDED.checkpoint_uri,"
    " reused_from_run = EXCLUDED.reused_from_run, original_executed_at = EXCLUDED.original_executed_at,"
    " updated_at = now()"
    " WHERE stage.state NOT IN ('completed','failed','cancelled','blocked','unresolved')"
    " RETURNING run_id, stage, state"
)
_RECONCILE_STAGE = (
    "INSERT INTO stage (run_id, stage, state, receipt, checkpoint_uri, reused_from_run, original_executed_at)"
    " VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)"
    " ON CONFLICT (run_id, stage) DO UPDATE SET"
    " state = EXCLUDED.state, receipt = EXCLUDED.receipt, checkpoint_uri = EXCLUDED.checkpoint_uri,"
    " reused_from_run = EXCLUDED.reused_from_run, original_executed_at = EXCLUDED.original_executed_at,"
    " updated_at = now()"
    " WHERE stage.state = 'unresolved'"
    " RETURNING run_id, stage, state"
)
_READ_LIFECYCLE = "SELECT object_kind, object_id, run_id, state, bucket, updated_at FROM lifecycle_projection"
_NEXT_EVENT_SEQ = "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_event WHERE run_id = %s"
_INSERT_EVENT = (
    "INSERT INTO run_event (run_id, seq, kind, stage, operation_id, payload) VALUES (%s, %s, %s, %s, %s, %s::jsonb)"
)
_READ_EVENTS = (
    "SELECT seq, at, kind, stage, operation_id, payload FROM run_event"
    " WHERE run_id = %s AND seq > %s ORDER BY seq LIMIT %s"
)
#: One events read can never pull an unbounded history; the portal asks for windows.
_EVENTS_LIMIT_CAP = 1000

#: Operation kinds that may continue on a terminal run (the reconciliation and
#: cancellation commands); every other kind needs a non-terminal run.
_TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled"})
_TERMINAL_KINDS = frozenset({"cancel", "reconcile"})

#: The bound on the registered reuse lineage of one attempt; a longer chain is malformed.
REUSE_LINEAGE_DEPTH = 256


def _stored_receipt(row) -> dict | None:
    """The parsed receipt of one locked stage row (jsonb arrives as text or a mapping)."""

    receipt = row[1] if row and len(row) > 1 else None
    if isinstance(receipt, str):
        try:
            receipt = json.loads(receipt)
        except ValueError:
            return None
    return receipt if isinstance(receipt, dict) else None


def _stored_reconciliation(row) -> object:
    """The recorded reconciliation of one locked stage row, for idempotent repeats."""

    receipt = _stored_receipt(row)
    return receipt.get("reconciliation") if receipt is not None else None


def _native_receipt_identity(receipt: object) -> tuple:
    """The bound native identity of one receipt: endpoint job UUID plus failed receipt digest.

    A repeat is only the same recorded fact when this identity is unchanged;
    a receipt that renames the native job or its digest is a conflicting
    submission, never an idempotent one.
    """

    if not isinstance(receipt, dict):
        return (None, None)
    return (receipt.get("native_run_id"), receipt.get("native_receipt_sha256"))


def _evidence_digests(entries: object) -> set:
    """The ``path#sha256=...`` digest fragments a reconciliation's evidence entries bind."""

    digests = set()
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, str) and "#sha256=" in entry:
            fragment = entry.split("#sha256=", 1)[1]
            if _is_sha256(fragment):
                digests.add(fragment)
    return digests


def _is_canonical_uuid(value: object) -> bool:
    """One canonical lowercase UUID string, the endpoint job identity shape."""

    if not isinstance(value, str):
        return False
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return value == parsed


def _json(value: Any) -> str | None:
    """jsonb parameters travel as the single canonical JSON text.

    The codec belongs to the shared foundation; this guard fail-closes
    instead of emitting NaN/Infinity.
    """

    if value is None:
        return None
    try:
        return canonical(value).decode()
    except ValueError as error:
        raise PlatformStoreError(f"Platform parameters must be finite JSON: {error}") from error


class PlatformStore:
    """The application-side store API: the single transaction entry for platform state."""

    def __init__(self, connect: Callable[[], Any], *, sealed: Callable[[str], Any] | None = None):
        self._connect = connect
        self._sealed = sealed

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, *, sealed: Callable[[str], Any] | None = None
    ) -> PlatformStore:
        """Build the store from the deploy side's derived service DSN.

        Install (``platform_env``) exports ``PLATFORM_SERVICE_DSN`` for the
        platform_service role; the driver is imported lazily so the package
        carries no database dependency, and a missing DSN is an explicit
        refusal - never a silent fallback to another store.  The one official
        instance also carries the deploy-side sealed resolver derived from the
        same environment (``PIPELINE_STORE_ROOT``); a missing store root is an
        explicit refusal too, so no production path exists that reads registered
        rows but cannot resolve their sealed checkpoints.
        """

        source = os.environ if environ is None else environ
        dsn = source.get("PLATFORM_SERVICE_DSN")
        if not dsn:
            raise PlatformStoreError("PLATFORM_SERVICE_DSN is not configured; the platform store is unavailable")
        if sealed is None:
            try:
                sealed = sealed_resolver_from_environment(source)
            except PipelineError as error:
                raise PlatformStoreError(str(error)) from error

        def connect():
            import psycopg2

            return psycopg2.connect(dsn)

        return cls(connect, sealed=sealed)

    @contextmanager
    def _transaction(self):
        """One fresh connection, one transaction, explicit close (no pool framework)."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    # ------------------------------------------------------------- admission

    def close_admission(self, *, reason: str, actor: str) -> AdmissionView:
        """Close the admission gate (drain/stop-controlled); idempotent."""

        return self._set_admission("closed", reason, actor)

    def open_admission(self, *, reason: str, actor: str) -> AdmissionView:
        """Reopen the admission gate for a new operating window; idempotent."""

        return self._set_admission("open", reason, actor)

    def admission(self) -> AdmissionView:
        """The read-only admission projection (single row)."""

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_ADMISSION)
            row = cursor.fetchone()
        if row is None:
            raise PlatformStoreError("The platform admission projection is empty")
        return AdmissionView(*row)

    def _set_admission(self, state: str, reason: str, actor: str) -> AdmissionView:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_LOCK_ADMISSION)
            row = cursor.fetchone()
            if row is None:
                raise PlatformStoreError("The platform admission row is missing")
            current = AdmissionView(*row)
            if current.state == state:
                return current
            cursor.execute(_UPDATE_ADMISSION, (state, reason, actor))
            updated = cursor.fetchone()
        if updated is None:
            raise PlatformStoreError("The platform admission row vanished during close/open")
        return AdmissionView(*updated)

    # ------------------------------------------------- the ONE admission entry

    def admit_run(
        self,
        *,
        request_id: str,
        run_id: str,
        principal: str,
        target_hardware: str,
        main_assembly: str,
        dispatch: dict,
        configuration: dict | None = None,
        baseline: dict | None = None,
        tool_identity: dict | None = None,
        from_stage: str | None = None,
        parent_run_id: str | None = None,
    ) -> None:
        """Register a new request, its run and its initial dispatch intent - all or nothing.

        This is the only public new-admission root.  ``dispatch`` is required
        and carries ``stage``, ``operation_key``, ``params_digest`` and the
        Airflow binding fields ``dag_id``/``dag_run_id``.  The DAG entry task
        adds its real ``task_id``/``map_index`` before any work.  The admission
        row lock is taken inside the same transaction that inserts all three
        rows: a closed gate (or any failure) leaves nothing behind.
        A linked admission additionally requires that its parent holds no
        non-terminal child: the reservation check runs under the same lock, so
        concurrent submissions can never each admit one.
        """

        if not isinstance(dispatch, dict):
            raise PlatformStoreError("An initial dispatch intent is required")
        intent = dict(dispatch)
        missing = {"stage", "operation_key", "params_digest"} - set(intent)
        if missing:
            raise PlatformStoreError(f"The initial dispatch intent is missing {sorted(missing)}")
        with self._transaction() as connection:
            _require_open(connection)
            cursor = connection.cursor()
            if parent_run_id is not None:
                cursor.execute(_OPEN_CHILD_RUN, (parent_run_id,))
                open_child = cursor.fetchone()
                if open_child is not None:
                    raise OpenAttemptConflictError(parent_run_id, open_child[0], open_child[1])
            cursor.execute(
                _INSERT_REQUEST,
                (request_id, principal, target_hardware, main_assembly, _json(configuration), _json(baseline)),
            )
            cursor.execute(_INSERT_RUN, (run_id, request_id, parent_run_id, from_stage, _json(tool_identity)))
            self._insert_operation(
                connection,
                run_id=run_id,
                stage=intent.pop("stage"),
                kind=intent.pop("kind", "dispatch"),
                operation_key=intent.pop("operation_key"),
                params_digest=intent.pop("params_digest"),
                dispatch=intent,
            )

    # ------------------------------------------- legitimate successor records

    def register_operation(
        self,
        *,
        run_id: str,
        stage: str,
        kind: str,
        operation_key: str,
        params_digest: str,
        dispatch: dict | None = None,
    ) -> OperationRecord:
        """Register a successor operation of an admitted run; **not** admission-gated.

        The run must be admitted (its row is locked for the short
        transaction); a terminal run only continues through cancel/reconcile
        kinds, and a confirmed publication can never be cancelled.  The same
        key with the same scope and parameters returns the existing record (a
        retry never restarts a side effect); any scope or parameter drift
        raises :class:`OperationConflictError`.  A concurrent duplicate is
        settled by the unique key: the insert does nothing and the same
        result is recovered, never re-sent as a new side effect.

        A duplicate request is settled by a pure identity read of the same
        key before any checkpoint is touched; only a genuinely new
        registration rebinds its predecessor from the sealed root *before*
        the transaction opens, and the locked transaction re-checks only the
        idempotency key and that same predecessor identity, reference and
        state before the insert.
        """

        existing = self._identity_operation(
            run_id=run_id, stage=stage, kind=kind, operation_key=operation_key, params_digest=params_digest
        )
        if existing is not None:
            if kind == "dispatch":
                with self._transaction() as connection:
                    return self._register_operation_locked(
                        connection,
                        run_id=run_id,
                        stage=stage,
                        kind=kind,
                        operation_key=operation_key,
                        params_digest=params_digest,
                        dispatch=dispatch or {},
                    )
            return existing
        binding = self._rebind_predecessor(run_id=run_id, stage=stage, kind=kind)
        with self._transaction() as connection:
            return self._register_operation_locked(
                connection,
                run_id=run_id,
                stage=stage,
                kind=kind,
                operation_key=operation_key,
                params_digest=params_digest,
                dispatch=dispatch or {},
                binding=binding,
            )

    def bind_initial_dispatch(
        self,
        *,
        run_id: str,
        stage: str,
        operation_key: str,
        dag_id: str,
        dag_run_id: str,
        task_id: str,
        map_index: int,
    ) -> OperationRecord:
        """Bind the admitted dispatch intent from the task that actually starts the run.

        This is intentionally narrower than ``register_operation``: the initial intent must
        already exist under the exact Portal key.  A missing row is a refusal, never a new
        dispatch record.  The intent stage records the requested start boundary; the binding is
        the real non-mapped freeze task that executes before every fresh or linked run.  Existing
        partial or conflicting bindings are refused without writing.
        """

        if task_id != "freeze" or type(map_index) is not int or map_index != -1:
            raise OperationConflictError("The initial dispatch must bind the executing freeze task")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_OPERATION_BINDING, (operation_key,))
            row = cursor.fetchone()
            if row is None:
                raise PlatformStoreError(f"The initial dispatch intent is not registered: {operation_key!r}")
            record = OperationRecord(*row[:7])
            if record.run_id != run_id or record.stage != stage or record.kind != "dispatch":
                raise OperationConflictError("The initial dispatch intent names another run or stage")
            target = operation_key.rsplit("/", 1)[-1].split(":", 1)[0]
            if (
                row[7] != dag_id
                or row[8] != dag_run_id
                or target != stage
                or operation_key != f"portal-{dag_run_id}/{stage}:0"
            ):
                raise OperationConflictError("The initial dispatch intent names another Portal or Airflow identity")
            if row[9] is not None or row[10] is not None:
                if (row[9], row[10]) == (task_id, map_index):
                    return record
                raise OperationConflictError("The initial dispatch intent is already bound to another Airflow task")
            if record.state != "queued":
                raise PlatformStoreError("Only a queued initial dispatch intent can accept its first task binding")
            cursor.execute(
                _BIND_MISSING_DISPATCH,
                (task_id, map_index, operation_key, run_id, dag_id, dag_run_id),
            )
            if cursor.fetchone() is None:
                raise PlatformStoreError("The initial dispatch binding lost its operation to another writer")
            return record

    def _register_operation_locked(
        self,
        connection,
        *,
        run_id: str,
        stage: str,
        kind: str,
        operation_key: str,
        params_digest: str,
        dispatch: dict,
        binding: dict | None = None,
    ) -> OperationRecord:
        """The in-transaction core of ``register_operation``.

        The same key with the same scope and parameters returns the recorded
        operation; a conflict refuses without touching anything else.  The
        reconcile confirmation calls this core inside its own write
        transaction, so the maintenance registration, the binding backfill and
        the settlement commit or roll back as one short unit.
        """

        cursor = connection.cursor()
        if kind == "dispatch":
            cursor.execute(_READ_OPERATION_BINDING, (operation_key,))
            row = cursor.fetchone()
        else:
            cursor.execute(_FIND_OPERATION, (operation_key,))
            row = cursor.fetchone()
        if row is not None:
            if kind != "dispatch":
                return _matching_operation(
                    row, run_id=run_id, stage=stage, kind=kind, params_digest=params_digest
                )
            existing = _matching_operation(
                row[:7], run_id=run_id, stage=stage, kind=kind, params_digest=params_digest
            )
            if kind != "dispatch" or not dispatch:
                return existing
            task_id = dispatch.get("task_id")
            map_index = dispatch.get("map_index")
            if (
                dispatch.get("dag_id") != row[7]
                or dispatch.get("dag_run_id") != row[8]
                or not isinstance(task_id, str)
                or not task_id
                or task_id != operation_key.rsplit("/", 1)[-1].split(":", 1)[0]
                or type(map_index) is not int
                or map_index < -1
            ):
                raise OperationConflictError("The duplicate dispatch registration names a different Airflow binding")
            if row[9] is not None or row[10] is not None:
                if (row[9], row[10]) == (task_id, map_index):
                    return existing
                raise OperationConflictError("The dispatch intent is already bound to another Airflow task")
            if existing.state != "queued":
                raise PlatformStoreError("Only a queued dispatch intent can accept its first task binding")
            cursor.execute(
                _BIND_MISSING_DISPATCH,
                (task_id, map_index, operation_key, run_id, row[7], row[8]),
            )
            if cursor.fetchone() is None:
                raise PlatformStoreError("The dispatch binding lost its operation to another writer")
            return existing
        if stage not in STAGE_IDS:
            raise PlatformStoreError(f"The stage must be one of {tuple(STAGE_IDS)}: {stage!r}")
        self._guard_domain(connection, run_id=run_id, stage=stage, kind=kind, claiming=False)
        _require_successor(self, connection, run_id=run_id, kind=kind, stage=stage, binding=binding)
        return self._insert_operation(
            connection,
            run_id=run_id,
            stage=stage,
            kind=kind,
            operation_key=operation_key,
            params_digest=params_digest,
            dispatch=dispatch,
        )

    def claim_operation(self, *, run_id: str, stage: str, operation_key: str) -> OperationClaim:
        """Claim a registered operation as ``running`` (queued -> running; idempotent).

        ``granted`` is True only for the call that performed the transition: a
        driver must execute the side effect exactly once, and a repeat call
        (``granted`` False) is a query/reconciliation of the already-claimed
        operation - never a permission to execute again.  A side-effect claim
        is refused while the run or the stage is terminal/unresolved, or while
        the stage carries an unresolved operation; the reconcile/cancel kinds
        stay claimable because reconciling the unknown outcome is the
        controlled continuation.  Run and stage rows are locked before the
        transition, so a claim racing a terminal confirmation is settled inside
        one transaction instead of by a caller-side read.
        """

        if stage not in STAGE_IDS:
            raise PlatformStoreError(f"The stage must be one of {tuple(STAGE_IDS)}: {stage!r}")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_FIND_OPERATION, (operation_key,))
            existing = cursor.fetchone()
            if existing is None:
                raise PlatformStoreError(f"The operation key {operation_key!r} is not registered")
            record = OperationRecord(*existing)
            if record.run_id != run_id or record.stage != stage:
                raise OperationConflictError("The operation key belongs to a different run or stage")
            self._guard_domain(connection, run_id=run_id, stage=stage, kind=record.kind, claiming=True)
            cursor.execute(_CLAIM_OPERATION, (operation_key, run_id, stage))
            row = cursor.fetchone()
            if row is not None:
                return OperationClaim(record=OperationRecord(*row), granted=True)
            cursor.execute(_FIND_OPERATION, (operation_key,))
            fresh_row = cursor.fetchone()
            if fresh_row is None:  # pragma: no cover - the row cannot vanish inside the transaction
                raise PlatformStoreError(f"The operation key {operation_key!r} is not registered")
            fresh = OperationRecord(*fresh_row)
            if fresh.state == "running":
                return OperationClaim(record=fresh, granted=False)
            raise PlatformStoreError(f"The operation is {fresh.state!r}; it cannot be claimed again")

    def settle_cache_release(self, *, run_id: str, stage: str, operation_key: str, receipt: dict) -> OperationRecord:
        """Close a claimed cache release on the canonical IF-10 release receipt.

        A cleanup's terminal state is never inferred from a stage outcome: it
        binds the receipt of the actual release call, validated by the one
        canonical ``validate_cache_release_receipt`` and bound to this database
        operation (its operation id and registered parameter digest) and to this
        Linux run.  Only a claimed (``running``) ``cache_release`` of the
        confirmed-import stage may settle, inside one short transaction that
        locks the run before the event sequence is allocated; repeating an
        already settled operation returns the recorded result.  A failed,
        unknown, foreign or otherwise non-canonical receipt is refused - never
        reported as success.
        """

        from ..interface_records import InterfaceRecordError, validate_cache_release_receipt

        if not isinstance(receipt, dict) or not receipt:
            raise PlatformStoreError("A cache release must bind its canonical release receipt")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_LOCK_RUN, (run_id,))
            if cursor.fetchone() is None:
                raise PlatformStoreError(f"The run {run_id!r} is not admitted")
            try:
                validate_cache_release_receipt(receipt)
            except InterfaceRecordError as error:
                raise PlatformStoreError(
                    "The cache release receipt is not the canonical IF-10 release receipt"
                ) from error
            cursor.execute(_FIND_OPERATION, (operation_key,))
            existing = cursor.fetchone()
            if existing is None:
                raise PlatformStoreError(f"The operation key {operation_key!r} is not registered")
            record = OperationRecord(*existing)
            if record.run_id != run_id or record.stage != stage:
                raise OperationConflictError("The operation key belongs to a different run or stage")
            if record.kind != "cache_release":
                raise PlatformStoreError("Only a registered cache release settles through its release receipt")
            if receipt["operation_id"] != record.operation_id:
                raise PlatformStoreError("The release receipt names another operation")
            if receipt["params_sha256"] != record.params_digest:
                raise PlatformStoreError("The release receipt does not bind this operation's parameters")
            if receipt["import"]["linux_run_id"] != run_id:
                raise PlatformStoreError("The release receipt names another Linux run")
            if record.state == "succeeded":
                return record
            if record.state != "running":
                raise PlatformStoreError(f"The cache release is {record.state!r}; claim it before it settles")
            cursor.execute(_SETTLE_CACHE_RELEASE, ("succeeded", operation_key, run_id, stage))
            row = cursor.fetchone()
            if row is None:
                raise PlatformStoreError(f"The cache release is {record.state!r}; claim it before it settles")
            settled = OperationRecord(*row)
            self._insert_event(
                connection,
                run_id=run_id,
                stage=stage,
                kind="cache_release",
                operation_id=settled.operation_id,
                payload={"state": "succeeded", "receipt": receipt},
            )
            return settled

    def _guard_domain(self, connection, *, run_id: str, stage: str, kind: str, claiming: bool) -> None:
        """One stage's side-effect domain: terminal and unknown states freeze it.

        Runs inside the write transaction (run and stage rows locked in the
        same order as every stage confirmation) so a registration or claim
        racing a terminal confirmation is settled transactionally.  A
        non-terminal kind is refused for a run that is not running (terminal
        or unknown), for a terminal or unresolved stage, and while the stage
        carries an unresolved operation - an unknown outcome only ever permits
        reconciling that same operation, never a new key or parameter.  The
        reconcile/cancel kinds skip the guard: they are the controlled
        continuation.
        """

        if kind in _TERMINAL_KINDS:
            return
        if kind == "cache_release":
            # The controlled same-identity cleanup: the intent may be registered
            # before the import confirms, but it may run only for the stage whose
            # import a confirmed record already binds - and it never re-does the
            # engineering side effect itself.
            if not claiming:
                return
            cursor = connection.cursor()
            cursor.execute(_LOCK_RUN, (run_id,))
            if cursor.fetchone() is None:
                raise PlatformStoreError(f"The run {run_id!r} is not admitted")
            cursor.execute(_LOCK_STAGE, (run_id, stage))
            stage_row = cursor.fetchone()
            current = stage_row[0] if stage_row else None
            if current != "completed":
                raise PlatformStoreError(f"A cache release requires the confirmed {stage!r} stage of run {run_id!r}")
            return
        cursor = connection.cursor()
        cursor.execute(_LOCK_RUN, (run_id,))
        row = cursor.fetchone()
        if row is None:
            raise PlatformStoreError(f"The run {run_id!r} is not admitted")
        state = row[0]
        if state in _TERMINAL_RUN_STATES:
            raise PlatformStoreError(f"The run is terminal ({state}); only cancel/reconcile operations continue")
        if state != "running":
            raise PlatformStoreError(
                f"The run is not running ({state!r}); a side effect of the {stage!r}"
                " stage may neither register nor claim"
            )
        cursor.execute(_LOCK_STAGE, (run_id, stage))
        stage_row = cursor.fetchone()
        current = stage_row[0] if stage_row else None
        if current is not None and (current in TERMINAL_STAGE_STATES or current == "unresolved"):
            raise PlatformStoreError(
                f"The {stage!r} stage of run {run_id!r} is {current!r}; only reconcile/cancel continue"
            )
        cursor.execute(_FIND_UNKNOWN_ORIGIN, (run_id, stage))
        if cursor.fetchone() is not None:
            raise PlatformStoreError("The stage has an unresolved operation; reconcile that same operation instead")

    def _identity_operation(
        self, *, run_id: str, stage: str, kind: str, operation_key: str, params_digest: str
    ) -> OperationRecord | None:
        """Pure identity read of one operation key; never touches a checkpoint.

        The duplicate path returns the registered record without consuming or
        re-verifying the sealed checkpoint: the lookup only proves the
        idempotency contract (the same request returns the same operation and
        a recorded side effect is never re-sent), and parameter drift is the
        same conflict as in the locked re-check.
        """

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_FIND_OPERATION, (operation_key,))
            row = cursor.fetchone()
        if row is None:
            return None
        return _matching_operation(row, run_id=run_id, stage=stage, kind=kind, params_digest=params_digest)

    def _rebind_predecessor(self, *, run_id: str, stage: str, kind: str) -> dict | None:
        """Rebind the successor's registered predecessor, outside any lock.

        The short-transaction boundary keeps the sealed-byte work - resolver, delivery
        manifest, independently rebuilt definition - out of the run-locking
        registration transaction.  This short read locates the registered
        predecessor and closes before the heavy verification runs; ``None``
        means this registration needs no bound predecessor yet (terminal
        kinds, a stage at or before the run's start stage, or a run and
        predecessor that cannot offer one - the locked transaction still
        raises the canonical refusal).
        """

        if kind in _TERMINAL_KINDS or stage not in STAGE_IDS:
            return None
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_RUN, (run_id,))
            run_row = cursor.fetchone()
            if run_row is None or run_row[0] != "running":
                return None
            from_stage = run_row[1]
            start = from_stage if from_stage in STAGE_IDS else STAGE_IDS[0]
            stage_index = STAGE_IDS.index(stage)
            start_index = STAGE_IDS.index(start)
            if stage_index <= start_index:
                return None
            predecessor = STAGE_IDS[stage_index - 1]
            cursor.execute(_READ_STAGE_ROW, (run_id, predecessor))
            predecessor_row = cursor.fetchone()
        if predecessor_row is None or predecessor_row[0] != "completed":
            return None
        definition = None
        sealed_root = None
        if predecessor in CONFIRMED_STAGE_STATES:
            self._verify_checkpoint_scope(
                predecessor_row[2], run_id=run_id, stage=predecessor, reused_from_run=predecessor_row[3]
            )
            definition, sealed_root = self._verify_sealed(predecessor_row[2], predecessor_row[1])
        _sealed_receipt(
            predecessor_row[1],
            run_id=run_id,
            stage=predecessor,
            state="completed",
            checkpoint_uri=predecessor_row[2],
            definition=definition,
            root=sealed_root,
        )
        return {
            "stage": predecessor,
            "checkpoint_uri": predecessor_row[2],
            "receipt_digest": digest(predecessor_row[1]),
        }

    def _insert_operation(
        self,
        connection,
        *,
        run_id: str,
        stage: str,
        kind: str,
        operation_key: str,
        params_digest: str,
        dispatch: dict,
    ) -> OperationRecord:
        cursor = connection.cursor()
        cursor.execute(
            _INSERT_OPERATION,
            (
                str(uuid.uuid4()),
                run_id,
                stage,
                kind,
                operation_key,
                params_digest,
                dispatch.get("dag_id"),
                dispatch.get("dag_run_id"),
                dispatch.get("task_id"),
                dispatch.get("map_index"),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            # A concurrent writer won the unique key: recover the same result.
            cursor.execute(_FIND_OPERATION, (operation_key,))
            row = cursor.fetchone()
            if row is None:
                raise PlatformStoreError("The operation insert conflicted but no row is visible")
        return _matching_operation(row, run_id=run_id, stage=stage, kind=kind, params_digest=params_digest)

    # ------------------------------------------------- stage confirmations

    def confirm_stage(
        self,
        *,
        run_id: str,
        stage: str,
        state: str,
        receipt: dict | None = None,
        checkpoint_uri: str | None = None,
        reused_from_run: str | None = None,
        original_executed_at: datetime | None = None,
        reconcile: dict | None = None,
    ) -> None:
        """Register one stage record: canonical state plus optional sealed receipt/checkpoint.

        This is the store path that writes ``stage`` rows on the
        platform side.  A sealed ``completed`` row is immutable - a later
        confirmation for the same (run, stage) is an explicit refusal instead
        of a rewrite (a redo is a linked run, not an overwrite).
        ``unresolved`` is not terminal: it is only confirmed through
        ``reconcile`` evidence of the same operation, which records the actual
        outcome - never a redo of an unknown side effect.
        """

        if stage not in STAGE_IDS:
            raise PlatformStoreError(f"The stage must be one of {tuple(STAGE_IDS)}: {stage!r}")
        if state not in STAGE_STATES:
            raise PlatformStoreError(f"The stage state must be one of {tuple(sorted(STAGE_STATES))}: {state!r}")
        if reconcile is not None and (not isinstance(reconcile, dict) or not reconcile):
            raise PlatformStoreError("Reconciliation evidence must be a non-empty record")
        if reconcile is not None and state not in {"completed", "failed", "cancelled"}:
            raise PlatformStoreError("A reconciliation confirms a definite outcome; unresolved is not an outcome")
        if reconcile is not None and isinstance(reconcile.get("binding"), dict):
            receipt, reconcile = self._byte_bound_orphan_receipt(
                run_id=run_id, stage=stage, state=state, receipt=receipt, reconcile=reconcile
            )
        elif reconcile is not None:
            # The admitted closure composes its own receipt from the byte-bound original
            # Airflow failure log; every other reconcile shape stays untouched and falls
            # through to the existing paths.
            admitted_closure = self._admitted_closure_receipt(
                run_id=run_id, stage=stage, state=state, reconcile=reconcile
            )
            if admitted_closure is not None:
                receipt = admitted_closure
        definition = None
        sealed_root = None
        reuse_binding = None
        if reused_from_run is not None:
            reuse_binding = self._verify_reuse_source(
                run_id=run_id, stage=stage, checkpoint_uri=checkpoint_uri, reused_from_run=reused_from_run
            )
            if digest(receipt) != reuse_binding["receipt_digest"]:
                raise PlatformStoreError(
                    "The reused receipt is not the confirmed source receipt; reuse never re-authors it"
                )
            if original_executed_at != reuse_binding["original_executed_at"]:
                raise PlatformStoreError("The reused execution instant is not the confirmed source instant")
        self._verify_checkpoint_scope(checkpoint_uri, run_id=run_id, stage=stage, reused_from_run=reused_from_run)
        if state == "completed" and stage in CONFIRMED_STAGE_STATES:
            definition, sealed_root = self._verify_sealed(checkpoint_uri, receipt)
        elif state == "failed" and (receipt or {}).get("artifacts"):
            # The failed diagnostic never skips byte verification: every
            # declared sealed member (subject- or report-class) is rebound to
            # the sealed bytes, while the success-only qualifications stay
            # with the completed path.
            self._verify_sealed(checkpoint_uri, receipt, completed=False)
        _sealed_receipt(
            receipt,
            run_id=run_id,
            stage=stage,
            state=state,
            checkpoint_uri=checkpoint_uri,
            definition=definition,
            root=sealed_root,
        )
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_LOCK_RUN, (run_id,))
            run_row = cursor.fetchone()
            if run_row is None:
                raise PlatformStoreError(f"The run {run_id!r} is not admitted")
            run_state = run_row[0]
            if reconcile is not None:
                reconcile = self._reconcile_operation(connection, run_id=run_id, stage=stage, reconcile=reconcile)
            cursor.execute(_LOCK_STAGE, (run_id, stage))
            row = cursor.fetchone()
            current = row[0] if row else None
            if current is not None:
                if current in TERMINAL_STAGE_STATES:
                    if reconcile is not None:
                        cursor.execute(_READ_STAGE_ROW, (run_id, stage))
                        stored = cursor.fetchone()
                        stored_receipt = _stored_receipt(stored)
                        if isinstance(stored_receipt, dict) and (
                            stored_receipt.get("admitted_before_dispatch") is True
                            or stored_receipt.get("closure_shape") == "freeze_dispatch_failed"
                        ):
                            # The admitted closure replays only with the identical original
                            # bytes: the recorded digest and terminal attempt must equal the
                            # freshly composed ones.
                            recorded = stored_receipt.get("airflow_failure")
                            incoming = (receipt or {}).get("airflow_failure")
                            if (
                                current == state
                                and _stored_reconciliation(stored) == reconcile
                                and isinstance(recorded, dict)
                                and isinstance(incoming, dict)
                                and recorded.get("path") == incoming.get("path")
                                and recorded.get("sha256") == incoming.get("sha256")
                            ):
                                return
                            raise PlatformStoreError(
                                "The repeated admitted closure does not match the recorded evidence; refused"
                            )
                        if (
                            current == state
                            and _stored_reconciliation(stored) == reconcile
                            and _native_receipt_identity(stored_receipt) == _native_receipt_identity(receipt)
                        ):
                            # The same operation, evidence, binding, native identity and outcome
                            # repeated: return the recorded result and change nothing.
                            if (
                                stage == "discover"
                                and isinstance(stored_receipt, dict)
                                and stored_receipt.get("reported_stage") == "freeze"
                                and stored_receipt.get("actual_stage") == "discover"
                            ):
                                self._bind_legacy_dispatch_freeze(
                                    connection,
                                    cursor,
                                    run_id=run_id,
                                    receipt=receipt,
                                    reconcile=reconcile,
                                )
                            return
                        if self._repair_terminal_native_discover_dispatch(
                            connection,
                            cursor,
                            run_id=run_id,
                            stage=stage,
                            state=state,
                            receipt=receipt,
                            reconcile=reconcile,
                            stored_receipt=stored_receipt,
                        ):
                            return
                    raise PlatformStoreError(
                        f"The {stage!r} stage of run {run_id!r} is already terminal; start a linked run instead"
                    )
                if current == "unresolved" and reconcile is None:
                    raise PlatformStoreError("The stage is unresolved; confirm it through an evidenced reconciliation")
                if current != "unresolved" and reconcile is not None:
                    raise PlatformStoreError("Reconciliation only confirms an unresolved stage")
            elif reconcile is not None:
                if isinstance(reconcile.get("binding"), dict):
                    self._reconcile_orphan_binding(
                        connection,
                        cursor,
                        run_id=run_id,
                        stage=stage,
                        state=state,
                        receipt=receipt,
                        reconcile=reconcile,
                        run_state=run_state,
                        checkpoint_uri=checkpoint_uri,
                        reused_from_run=reused_from_run,
                    )
                    return
                # The admitted closure is entered only through its internally composed,
                # byte-verified receipt: a caller-supplied marker can never open this path.
                admitted_closure = self._admitted_closure_receipt(
                    run_id=run_id, stage=stage, state=state, reconcile=reconcile
                )
                if admitted_closure is not None:
                    self._reconcile_admitted_closure(
                        connection,
                        run_id=run_id,
                        stage=stage,
                        state=state,
                        receipt=admitted_closure,
                        reconcile=reconcile,
                        run_state=run_state,
                    )
                    return
                raise PlatformStoreError("Reconciliation requires the unresolved stage record of this run")
            if reused_from_run is not None:
                cursor.execute(_READ_RUN_PARENT, (run_id,))
                parent_row = cursor.fetchone()
                if parent_row is None or parent_row[0] != reused_from_run:
                    raise PlatformStoreError("Reuse requires the registered parent run of this linked attempt")
                cursor.execute(_READ_STAGE, (reused_from_run, stage))
                source_row = cursor.fetchone()
                if (
                    source_row is None
                    or source_row[0] != "completed"
                    or source_row[2] != checkpoint_uri
                    or digest(source_row[1]) != reuse_binding["receipt_digest"]
                ):
                    raise PlatformStoreError("The registered reuse source changed; retry the confirmation")
                if digest(receipt) != reuse_binding["receipt_digest"]:
                    raise PlatformStoreError(
                        "The reused receipt is not the confirmed source receipt; retry the confirmation"
                    )
                cursor.execute(_READ_STAGE_TIME, (reused_from_run, stage))
                time_row = cursor.fetchone()
                if original_executed_at != (time_row[0] if time_row is not None else None):
                    raise PlatformStoreError(
                        "The reused execution instant is not the confirmed source instant; retry the confirmation"
                    )
            if reconcile is not None:
                operation_id = reconcile.get("operation_id")
                if not isinstance(operation_id, str) or not operation_id:
                    raise PlatformStoreError("Reconciliation must name its registered reconcile operation")
                cursor.execute(_FIND_RECONCILE, (operation_id, run_id, stage))
                if cursor.fetchone() is None:
                    raise PlatformStoreError(
                        "The reconciliation names no registered reconcile operation of this run and stage"
                    )
                cursor.execute(_FIND_UNKNOWN_ORIGIN, (run_id, stage))
                if cursor.fetchone() is None:
                    raise PlatformStoreError("The reconciliation binds no unresolved original operation of this stage")
            payload_receipt = {**receipt, "reconciliation": reconcile} if reconcile is not None else receipt
            if current == "unresolved":
                cursor.execute(
                    _RECONCILE_STAGE,
                    (
                        run_id,
                        stage,
                        state,
                        _json(payload_receipt),
                        checkpoint_uri,
                        reused_from_run,
                        original_executed_at,
                    ),
                )
                if cursor.fetchone() is None:
                    raise PlatformStoreError("The reconciliation lost its unresolved stage to another writer")
                self._insert_event(
                    connection,
                    run_id=run_id,
                    stage=stage,
                    kind="stage",
                    operation_id=reconcile.get("operation_id") if reconcile is not None else None,
                    payload={"state": state},
                )
                self._stage_outcome(connection, run_id=run_id, stage=stage, state=state)
                cursor.execute(_SETTLE_RECONCILE, (operation_id, run_id, stage))
                if state == "completed" and stage != "publish":
                    # The definite outcome lifts the whole run out of unresolved:
                    # a completed non-publication stage continues as running
                    # (the publication completion and the failed/cancelled
                    # outcomes already settled the run in _stage_outcome).
                    cursor.execute(_RESTORE_RUN_RUNNING, (run_id,))
                return
            cursor.execute(
                _INSERT_STAGE,
                (run_id, stage, state, _json(payload_receipt), checkpoint_uri, reused_from_run, original_executed_at),
            )
            if cursor.fetchone() is None:
                raise PlatformStoreError(
                    f"The {stage!r} stage of run {run_id!r} is already terminal; start a linked run instead"
                )
            self._insert_event(
                connection,
                run_id=run_id,
                stage=stage,
                kind="stage",
                operation_id=reconcile.get("operation_id") if reconcile is not None else None,
                payload={"state": state},
            )
            self._stage_outcome(connection, run_id=run_id, stage=stage, state=state)

    def _byte_bound_orphan_receipt(
        self, *, run_id: str, stage: str, state: str, receipt: dict | None, reconcile: dict
    ) -> tuple[dict, dict]:
        """Compose an orphan's failed receipt from the original native bytes - the one rule.

        An orphan recovery never accepts a formed receipt or a digest string: the
        reconciliation carries the raw inputs (the original file and the bound
        endpoint job UUID) and this Store action itself loads the bytes, validates
        the native job document, binds the outcome and identity, derives the digest
        from those bytes, and normalizes the recorded evidence to that digest.  The
        caller's layers hold no acceptance rules and cannot bypass the bytes.
        """

        if state != "failed":
            raise PlatformStoreError("An orphan reconciliation records the observed failure")
        if receipt is not None:
            raise PlatformStoreError("An orphan recovery carries its original receipt inputs, never a formed receipt")
        inputs = reconcile.get("native_receipt")
        if not isinstance(inputs, dict):
            raise PlatformStoreError(
                "An orphan reconciliation must carry its original native receipt (path and endpoint job UUID)"
            )
        path = inputs.get("path")
        native_run_id = inputs.get("native_run_id")
        if not isinstance(path, str) or not path or not isinstance(native_run_id, str) or not native_run_id:
            raise PlatformStoreError("The orphan receipt inputs name their original file and endpoint job UUID")
        reported_stage = reconcile.get("reported_stage")
        loaded = load_native_failed_receipt(
            path,
            stage=stage,
            native_run_id=native_run_id,
            reported_stage=reported_stage if isinstance(reported_stage, str) and reported_stage else None,
        )
        digest_entry = f"{loaded.path}#sha256={loaded.sha256}"
        entries = reconcile.get("evidence")
        entries = list(entries) if isinstance(entries, list) else []
        contradicting = [
            item
            for item in entries
            if isinstance(item, str)
            and item.startswith(f"{loaded.path}#sha256=")
            and item.split("#sha256=", 1)[1] != loaded.sha256
        ]
        if contradicting:
            raise PlatformStoreError(f"The evidence entry contradicts the original receipt bytes: {contradicting[0]}")
        normalized_evidence = [
            item
            for item in entries
            if not (isinstance(item, str) and (item == loaded.path or item.startswith(f"{loaded.path}#")))
        ] + [digest_entry]
        composed = {
            "schema_version": BUNDLE_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "stage": loaded.stage,
            "reported_stage": loaded.reported_stage,
            "actual_stage": loaded.stage,
            "request_main_assembly": loaded.request_main_assembly,
            "request_upload_sha256": loaded.request_upload_sha256,
            "request_upload_files": loaded.request_upload_files,
            "selection_state": loaded.selection_state,
            "selection_files": loaded.selection_files,
            "selection_upload_sha256": loaded.selection_upload_sha256,
            "state": loaded.state,
            "passed": False,
            "native_run_id": loaded.native_run_id,
            "native_receipt_sha256": loaded.sha256,
        }
        for key in ("error", "error_code", "detail"):
            value = getattr(loaded, key)
            if value is not None:
                composed[key] = value
        resolved = {key: value for key, value in reconcile.items() if key != "evidence"}
        resolved["evidence"] = normalized_evidence
        return composed, resolved

    def _reconcile_operation(self, connection, *, run_id: str, stage: str, reconcile: dict) -> dict:
        """Resolve the reconciliation's registered operation inside the write transaction.

        The caller either names an already-registered reconcile operation, or
        carries the registration itself (its key plus parameter digest); the
        registration then travels in the same short locked transaction as the
        binding and the settlement, so a refused reconciliation leaves no
        maintenance row behind and a retry reuses the same operation identity.
        """

        operation_id = reconcile.get("operation_id")
        operation_key = reconcile.get("operation_key")
        params_digest = reconcile.get("params_digest")
        if operation_id is not None and (operation_key is not None or params_digest is not None):
            raise PlatformStoreError("A reconciliation names its operation or its registration, not both")
        if operation_id is not None:
            return reconcile
        if not (isinstance(operation_key, str) and operation_key and isinstance(params_digest, str) and params_digest):
            raise PlatformStoreError(
                "A reconciliation must name its registered reconcile operation or carry its registration key"
            )
        record = self._register_operation_locked(
            connection,
            run_id=run_id,
            stage=stage,
            kind="reconcile",
            operation_key=operation_key,
            params_digest=params_digest,
            dispatch={},
        )
        resolved = {key: value for key, value in reconcile.items() if key not in {"operation_key", "params_digest"}}
        resolved["operation_id"] = record.operation_id
        return resolved

    def _admitted_closure_receipt(self, *, run_id: str, stage: str, state: str, reconcile: dict) -> dict | None:
        """The admitted-closure composed receipt, or None for other shapes.

        The flavor is recognized by the recorded reconcile shape alone: freeze failed with
        exactly one evidence file that parses as the freeze task's own Airflow failure log.
        The composed receipt binds the original bytes by digest and preserves the exception
        verbatim; every later guard (queued run, no stage rows, the one unclaimed unbound
        dispatch intent, the real Airflow facts and terminal try number read in one snapshot)
        runs inside the one write transaction.  The real failed freeze task proves the
        execution of every initial intent, including a linked restart boundary.
        """
        if stage != "freeze" or state != "failed":
            return None
        evidence = reconcile.get("evidence")
        if not (isinstance(evidence, list) and len(evidence) == 1 and isinstance(evidence[0], str)):
            return None
        try:
            loaded = load_airflow_task_failure(evidence[0])
        except PipelineError:
            # Not an Airflow failure log: every other reconcile keeps its existing paths.
            return None
        if loaded.task_id != "freeze":
            return None
        return {
            "schema_version": BUNDLE_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "stage": "freeze",
            "state": "failed",
            "passed": False,
            "error": f"{loaded.exc_type}: {loaded.exc_value}",
            "airflow_failure": {
                "path": loaded.path,
                "sha256": loaded.sha256,
                "try_number": loaded.try_number,
            },
        }

    def _reconcile_admitted_closure(
        self, connection, *, run_id: str, stage: str, state: str, receipt: dict, reconcile: dict, run_state: object
    ) -> None:
        """Close one admitted run from its original dispatch and Airflow records, atomically.

        Boundary (the frozen recovery contract): only a run still ``queued`` with zero stage
        records and exactly the one unclaimed, unbound dispatch intent may close here.  One
        statement reads the original Airflow authority (the terminal DagRun, freeze task with
        its terminal try number, and all five later tasks) inside this transaction; the freeze
        failure text is the verbatim exception from the byte-bound original log.  Every Portal
        initial intent, including a linked restart boundary, was dispatched by the real freeze
        task; its requested boundary never substitutes for that actual task key.  Any executed,
        unresolved, active, ambiguous, missing or mismatched fact refuses with zero writes.
        """
        if state != "failed" or stage != "freeze":
            raise PlatformStoreError("The admitted closure records the freeze failure only")
        if run_state != "queued":
            raise PlatformStoreError("The admitted closure requires the never-started queued run")
        loaded = load_airflow_task_failure((reconcile.get("evidence") or [""])[0])
        recorded = receipt.get("airflow_failure")
        if not (
            isinstance(recorded, dict)
            and recorded.get("path") == loaded.path
            and recorded.get("sha256") == loaded.sha256
            and recorded.get("try_number") == loaded.try_number
        ):
            raise PlatformStoreError("The admitted evidence changed between composition and commit; refused")

        cursor = connection.cursor()
        cursor.execute(_COUNT_RUN_STAGES, (run_id,))
        if cursor.fetchone()[0] != 0:
            raise PlatformStoreError("The run carries stage results; the admitted closure refuses")
        cursor.execute(_LOCK_RUN_OPERATIONS, (run_id,))
        operations = [row for row in cursor.fetchall() if row[2] not in {"reconcile", "cache_release"}]
        if len(operations) != 1:
            raise PlatformStoreError("The admitted closure requires exactly the one recorded dispatch intent")
        operation_id, op_stage, kind, op_state, dag_id, dag_run_id, task_id, map_index, operation_key = operations[0]
        if (
            kind != "dispatch"
            or op_state != "queued"
            or task_id is not None
            or map_index is not None
            or not (isinstance(dag_id, str) and dag_id and isinstance(dag_run_id, str) and dag_run_id)
        ):
            raise PlatformStoreError("The recorded dispatch intent is not an unclaimed, unbound queued intent")
        if not (isinstance(op_stage, str) and op_stage in STAGE_IDS):
            raise PlatformStoreError("The dispatch intent records no canonical stage")
        if loaded.dag_id != dag_id or loaded.run_id != dag_run_id:
            raise PlatformStoreError("The Airflow failure log names another DagRun than the recorded dispatch")
        cursor.execute(_READ_ADMITTED_FACTS, (dag_id, dag_run_id, dag_id, dag_run_id))
        facts = cursor.fetchall()
        dag_run_rows = [row for row in facts if row[0] == "dag_run"]
        if len(dag_run_rows) != 1 or dag_run_rows[0][3] != "failed":
            raise PlatformStoreError("The DagRun does not prove its terminal failed fact; refused")
        by_task: dict[str, list[tuple[int, object, int]]] = {}
        for _, task, task_map, task_state, task_try in facts:
            if isinstance(task, str):
                by_task.setdefault(task, []).append((task_map, task_state, task_try))
        freeze_rows = by_task.get("freeze") or []
        if (
            len(freeze_rows) != 1
            or freeze_rows[0][1] != "failed"
            or freeze_rows[0][0] != loaded.map_index
            or freeze_rows[0][2] != loaded.try_number
        ):
            raise PlatformStoreError("The freeze task does not prove its own terminal failed fact; refused")
        for later in STAGE_IDS[STAGE_IDS.index("freeze") + 1 :]:
            rows = by_task.get(later) or []
            if len(rows) != 1 or rows[0][1] != "upstream_failed":
                raise PlatformStoreError(
                    f"The successor task {later!r} does not prove its never-executed fact; refused"
                )

        cursor.execute(_BIND_ORPHAN_OPERATION, ("freeze", loaded.map_index, operation_id, run_id))
        if cursor.fetchone() is None:
            raise PlatformStoreError("The dispatch intent lost its unbound queued row to another writer; refused")
        payload_receipt = {
            **receipt,
            "reconciliation": reconcile,
            "closure_shape": "freeze_dispatch_failed",
            "freeze_execution": {
                "task_id": "freeze",
                "map_index": loaded.map_index,
                "try_number": loaded.try_number,
                "state": "failed",
            },
        }
        cursor.execute(_INSERT_STAGE, (run_id, "freeze", "failed", _json(payload_receipt), None, None, None))
        if cursor.fetchone() is None:
            raise PlatformStoreError("The admitted freeze record lost its run to another writer")
        for later in STAGE_IDS[STAGE_IDS.index("freeze") + 1 :]:
            cursor.execute(_INSERT_STAGE, (run_id, later, "blocked", _json({}), None, None, None))
            if cursor.fetchone() is None:
                raise PlatformStoreError("The admitted blocked record lost its run to another writer")
            self._insert_event(
                connection, run_id=run_id, stage=later, kind="stage", operation_id=None, payload={"state": "blocked"}
            )
        self._insert_event(
            connection,
            run_id=run_id,
            stage="freeze",
            kind="binding",
            operation_id=operation_id,
            payload={
                "operation_key": operation_key,
                "intent_stage": op_stage,
                "reconciled_stage": "freeze",
                "reconciled_state": "failed",
                "dag_id": dag_id,
                "dag_run_id": dag_run_id,
                "task_id": "freeze",
                "map_index": loaded.map_index,
                "try_number": loaded.try_number,
                "closure_shape": payload_receipt["closure_shape"],
                "freeze_dispatched": True,
                "airflow_failure_sha256": loaded.sha256,
            },
        )
        reconcile_operation = reconcile.get("operation_id")
        self._insert_event(
            connection,
            run_id=run_id,
            stage="freeze",
            kind="stage",
            operation_id=reconcile_operation,
            payload={"state": "failed"},
        )
        cursor.execute(_SETTLE_ORPHAN_INTENT, ("failed", operation_id, run_id))
        self._stage_outcome(connection, run_id=run_id, stage="freeze", state="failed")
        cursor.execute(_SETTLE_RECONCILE, (reconcile_operation, run_id, "freeze"))

    def _reconcile_orphan_binding(
        self,
        connection,
        cursor,
        *,
        run_id: str,
        stage: str,
        state: str,
        receipt: dict | None,
        reconcile: dict,
        run_state: object,
        checkpoint_uri,
        reused_from_run,
    ) -> None:
        """Controlled orphan recovery: bind a queued intent's missing Airflow key, then confirm.

        The one reconcile entry fills either the missing binding columns of a
        never-started queued intent (the 1.4.3 pre-candidate shape) or the one
        exact bound native ``discover`` failure shape from the real Airflow
        full key the caller evidences.  In both cases the receipt must name this
        run's own endpoint job - checked against the one canonical
        ``native_run_id`` derivation from the operation's recorded DagRun -
        and its failed-receipt digest must be the digest the reconciliation's
        evidence entries bind.  The binding is accepted only after the lifecycle
        view itself shows the bound task actually failed (never an unexecuted
        ``upstream_failed`` sibling); the action's own pre-transaction check
        refuses a DagRun that is not the settled failed fact before this
        transaction opens.  Conflict, foreign, missing, ambiguous, live or
        success facts refuse with zero change (the tentative binding rolls back
        with the transaction), and no unresolved intermediate state is ever
        fabricated.
        """

        if state != "failed":
            raise PlatformStoreError("An orphan reconciliation records the observed failure")
        if checkpoint_uri is not None or reused_from_run is not None:
            raise PlatformStoreError("An orphan reconciliation carries no checkpoint or reuse binding")
        if not isinstance(receipt, dict):
            raise PlatformStoreError("An orphan reconciliation carries its failed receipt")
        if (
            stage == "discover"
            and receipt.get("reported_stage") in {"discover", "freeze"}
            and receipt.get("actual_stage") == "discover"
        ):
            self._reconcile_bound_native_discover(
                connection,
                cursor,
                run_id=run_id,
                stage=stage,
                state=state,
                receipt=receipt,
                reconcile=reconcile,
                run_state=run_state,
            )
            return
        if run_state != "queued":
            raise PlatformStoreError("Orphan recovery requires the never-started queued run")
        binding = reconcile.get("binding")
        operation_id = binding.get("operation_id") if isinstance(binding, dict) else None
        task_id = binding.get("task_id") if isinstance(binding, dict) else None
        map_index = binding.get("map_index") if isinstance(binding, dict) else None
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(task_id, str)
            or not task_id
            or type(map_index) is not int
            or map_index < -1
        ):
            raise PlatformStoreError("An orphan binding names its operation, task id and integer map index")
        native_run = receipt.get("native_run_id")
        native_sha = receipt.get("native_receipt_sha256")
        if not _is_canonical_uuid(native_run) or not _is_sha256(native_sha):
            raise PlatformStoreError("An orphan reconciliation binds its native run UUID and failed receipt digest")
        if native_sha not in _evidence_digests(reconcile.get("evidence")):
            raise PlatformStoreError(
                "The failed receipt digest must be the one the reconciliation evidence entries bind"
            )
        reconcile_operation = reconcile.get("operation_id")
        cursor.execute(_FIND_RECONCILE, (reconcile_operation, run_id, stage))
        if cursor.fetchone() is None:
            raise PlatformStoreError("The reconciliation names no registered reconcile operation of this run and stage")
        cursor.execute(_READ_ORPHAN_OPERATION, (operation_id, run_id))
        row = cursor.fetchone()
        if row is None:
            raise PlatformStoreError("The orphan binding names no operation of this run")
        op_stage, kind, op_state, dag_id, dag_run_id, bound_task, bound_map, operation_key = row
        if kind not in {"dispatch", "native"}:
            raise PlatformStoreError("Only a queued dispatch intent may be recovered as an orphan")
        if op_state != "queued" or bound_task is not None or bound_map is not None:
            raise PlatformStoreError("The named operation is not an unstarted unbound orphan; refused")
        if not (isinstance(dag_id, str) and dag_id and isinstance(dag_run_id, str) and dag_run_id):
            raise PlatformStoreError("The operation lacks its recorded Airflow DagRun identity evidence")
        if native_run != native_run_id(dag_run_id):
            # The endpoint job identity is fixed by the run's own recorded DagRun id
            # through the one canonical derivation: a foreign, copied or merely
            # UUID-shaped value never authorizes recovery.
            raise PlatformStoreError("The native receipt names another run's endpoint job; refused")
        cursor.execute(_BIND_ORPHAN_OPERATION, (task_id, map_index, operation_id, run_id))
        if cursor.fetchone() is None:
            raise PlatformStoreError("The orphan binding lost its operation to another writer")
        # One statement carries every lifecycle fact of this acceptance: a single query
        # evaluates the view once, so the task's and the DagRun's states are the same
        # database snapshot - a schedule that flips between two reads is never two
        # separately-read terminal facts.
        cursor.execute(_READ_ORPHAN_LIFECYCLE, (operation_id, f"{dag_id}/{dag_run_id}"))
        lifecycle = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        scheduling = lifecycle.get("scheduling")
        if scheduling != ("failed", "terminal"):
            # A fabricated or ambiguous key joins nothing (unknown), an unexecuted
            # upstream_failed sibling never sent the native request, a live task is in
            # flight and a green task contradicts the failure: all refuse, and the
            # tentative binding rolls back with this transaction.
            raise PlatformStoreError("The bound Airflow task does not prove its own failed fact; refused")
        dag_run = lifecycle.get("dag_run")
        if dag_run != ("failed", "terminal"):
            raise PlatformStoreError("The bound DagRun does not prove its terminal failed fact; refused")
        payload_receipt = {**receipt, "reconciliation": reconcile}
        cursor.execute(_INSERT_STAGE, (run_id, stage, state, _json(payload_receipt), None, None, None))
        if cursor.fetchone() is None:
            raise PlatformStoreError("The orphan stage record lost its run to another writer")
        self._insert_event(
            connection,
            run_id=run_id,
            stage=stage,
            kind="binding",
            operation_id=operation_id,
            payload={
                "operation_key": operation_key,
                "intent_stage": op_stage,
                "reconciled_stage": stage,
                "reconciled_state": state,
                "dag_id": dag_id,
                "dag_run_id": dag_run_id,
                "task_id": task_id,
                "map_index": map_index,
                "native_run_id": native_run,
                "native_receipt_sha256": native_sha,
            },
        )
        self._insert_event(
            connection,
            run_id=run_id,
            stage=stage,
            kind="stage",
            operation_id=reconcile_operation,
            payload={"state": state},
        )
        self._stage_outcome(connection, run_id=run_id, stage=stage, state=state)
        cursor.execute(_SETTLE_ORPHAN_INTENT, (state, operation_id, run_id))
        cursor.execute(_SETTLE_RECONCILE, (reconcile_operation, run_id, stage))

    def _repair_terminal_native_discover_dispatch(
        self,
        connection,
        cursor,
        *,
        run_id: str,
        stage: str,
        state: str,
        receipt: dict,
        reconcile: dict,
        stored_receipt: dict | None,
    ) -> bool:
        """Repair one missing freeze dispatch key beside an already terminal discover failure.

        The discover row itself stays byte-for-byte and timestamp-for-timestamp unchanged.
        The original native job, immutable request, sealed freeze and real Airflow facts prove
        the unique succeeded freeze intent; only its missing task key is added.  An identical
        replay revalidates the same facts and appends no event.
        """

        if stage != "discover" or state != "failed" or not isinstance(stored_receipt, dict):
            return False
        if stored_receipt.get("stage") != "discover" or stored_receipt.get("state") != "failed":
            return False
        binding = reconcile.get("binding")
        if not isinstance(binding, dict) or binding.get("task_id") != "discover" or binding.get("map_index") != -1:
            return False
        operation_id = binding.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            return False
        cursor.execute(_READ_ORPHAN_OPERATION, (operation_id, run_id))
        orphan = cursor.fetchone()
        if orphan is None:
            return False
        op_stage, kind, op_state, dag_id, dag_run_id, task_id, map_index, _operation_key = orphan
        if (
            op_stage != "discover"
            or kind != "native"
            or op_state != "failed"
            or task_id != "discover"
            or map_index != -1
            or not isinstance(dag_id, str)
            or not dag_id
            or not isinstance(dag_run_id, str)
            or not dag_run_id
        ):
            return False

        native_code = receipt.get("error_code")
        stored_code = stored_receipt.get("error_code")
        native_error = receipt.get("error")
        stored_error = stored_receipt.get("error")
        expected_error = f"AirflowFailException: SolidWorks job {run_id} failed: {native_error}"
        if (
            not isinstance(native_code, str)
            or not native_code
            or native_code != stored_code
            or not isinstance(native_error, str)
            or not native_error
            or not isinstance(stored_error, str)
            or stored_error != expected_error
            or receipt.get("detail") != stored_receipt.get("detail")
        ):
            raise PlatformStoreError("The original native failure does not match the terminal discover record")

        self._verify_bound_discover_airflow(cursor, dag_id=dag_id, dag_run_id=dag_run_id, binding=binding)
        cursor.execute(_READ_RUN_DISPATCH_FOR_BINDING, (run_id,))
        dispatch = cursor.fetchone()
        if dispatch is None:
            raise PlatformStoreError("The run carries no freeze dispatch intent")
        dispatch_id, dispatch_stage, dispatch_state, dispatch_dag, dispatch_run, bound_task, bound_map, key = dispatch
        if (
            dispatch_stage != "freeze"
            or dispatch_state != "succeeded"
            or dispatch_dag != dag_id
            or dispatch_run != dag_run_id
        ):
            raise PlatformStoreError("The unique dispatch intent is not the settled freeze dispatch")

        cursor.execute(_READ_DISPATCH_BINDING_EVENTS, (run_id, dispatch_id))
        events = cursor.fetchall()
        native_sha = receipt.get("native_receipt_sha256")
        if bound_task is None and bound_map is None:
            if events:
                raise PlatformStoreError("The terminal dispatch repair carries binding evidence without a binding")
        elif (bound_task, bound_map) == ("freeze", -1):
            payloads = []
            for (payload,) in events:
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except ValueError as error:
                        raise PlatformStoreError("The recorded dispatch binding event is unreadable") from error
                payloads.append(payload)
            if (
                len(payloads) != 1
                or not isinstance(payloads[0], dict)
                or payloads[0].get("operation_key") != key
                or payloads[0].get("task_id") != "freeze"
                or payloads[0].get("map_index") != -1
                or payloads[0].get("native_receipt_sha256") != native_sha
            ):
                raise PlatformStoreError("The terminal dispatch binding does not match its recorded evidence")
        else:
            raise PlatformStoreError("The terminal freeze dispatch carries a partial or foreign binding")

        reconcile_operation = reconcile.get("operation_id")
        cursor.execute(_FIND_RECONCILE, (reconcile_operation, run_id, stage))
        if cursor.fetchone() is None:
            raise PlatformStoreError("The reconciliation names no registered reconcile operation of this run and stage")
        self._bind_legacy_dispatch_freeze(
            connection,
            cursor,
            run_id=run_id,
            receipt=receipt,
            reconcile=reconcile,
        )
        cursor.execute(_SETTLE_RECONCILE, (reconcile_operation, run_id, stage))
        return True

    def _reconcile_bound_native_discover(
        self,
        connection,
        cursor,
        *,
        run_id: str,
        stage: str,
        state: str,
        receipt: dict,
        reconcile: dict,
        run_state: object,
    ) -> None:
        """Close one bound running native discover failure from its original receipt.

        The historical reported-freeze/actual-discover alias and the direct
        discover failure with its own terminal event both enter here.  The
        native receipt must prove the observed failure, the bound native
        operation must be the recorded running discover operation, the frozen
        request must resolve to the same upload/main identity, and the real
        Airflow facts must show the one failed discover task with later tasks
        still unexecuted.  The completed freeze row is read-only here.
        """

        reported_stage = receipt.get("reported_stage")
        if (
            stage != "discover"
            or state != "failed"
            or reported_stage not in {"discover", "freeze"}
            or receipt.get("actual_stage") != "discover"
        ):
            raise PlatformStoreError("The bound native recovery is not a supported discover failure shape")
        if run_state != "running":
            raise PlatformStoreError("The bound native recovery requires a running admitted run")

        binding = reconcile.get("binding")
        operation_id = binding.get("operation_id") if isinstance(binding, dict) else None
        task_id = binding.get("task_id") if isinstance(binding, dict) else None
        map_index = binding.get("map_index") if isinstance(binding, dict) else None
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or task_id != "discover"
            or type(map_index) is not int
            or map_index < -1
        ):
            raise PlatformStoreError("The bound native recovery names the discover operation and its full task key")
        native_run = receipt.get("native_run_id")
        native_sha = receipt.get("native_receipt_sha256")
        if not _is_canonical_uuid(native_run) or not _is_sha256(native_sha):
            raise PlatformStoreError("The bound native recovery binds its endpoint job and original receipt bytes")
        if native_sha not in _evidence_digests(reconcile.get("evidence")):
            raise PlatformStoreError("The native receipt digest is not bound by the recovery evidence")

        reconcile_operation = reconcile.get("operation_id")
        cursor.execute(_FIND_RECONCILE, (reconcile_operation, run_id, stage))
        if cursor.fetchone() is None:
            raise PlatformStoreError("The reconciliation names no registered reconcile operation of this run and stage")
        cursor.execute(_READ_ORPHAN_OPERATION, (operation_id, run_id))
        row = cursor.fetchone()
        if row is None:
            raise PlatformStoreError("The bound native recovery names no operation of this run")
        op_stage, kind, op_state, dag_id, dag_run_id, bound_task, bound_map, operation_key = row
        if (
            op_stage != "discover"
            or kind != "native"
            or op_state != "running"
            or bound_task != task_id
            or bound_map != map_index
        ):
            raise PlatformStoreError("The bound native operation is not the recorded running discover operation")
        if not (isinstance(dag_id, str) and dag_id and isinstance(dag_run_id, str) and dag_run_id):
            raise PlatformStoreError("The bound native operation lacks its Airflow DagRun identity")
        if native_run != native_run_id(dag_run_id):
            raise PlatformStoreError("The native receipt names another run's endpoint job")

        cursor.execute(_READ_STAGE_RECORDS, (run_id,))
        stage_rows = cursor.fetchall()
        if len(stage_rows) != 1 or stage_rows[0][0] != "freeze" or stage_rows[0][1] != "completed":
            raise PlatformStoreError("The bound native recovery requires only the completed freeze stage")
        freeze_checkpoint = stage_rows[0][3] if len(stage_rows[0]) > 3 else None
        freeze_receipt = stage_rows[0][2]
        if isinstance(freeze_receipt, str):
            try:
                freeze_receipt = json.loads(freeze_receipt)
            except ValueError:
                freeze_receipt = None
        freeze_reused_from = stage_rows[0][4] if len(stage_rows[0]) > 4 else None
        if (
            not isinstance(freeze_receipt, dict)
            or freeze_reused_from is not None
            or not isinstance(freeze_checkpoint, str)
            or not freeze_checkpoint
        ):
            raise PlatformStoreError("The completed freeze carries no directly owned sealed checkpoint")
        try:
            checkpoint_run, checkpoint_stage = parse_sealed_uri(freeze_checkpoint)
        except PipelineError as error:
            raise PlatformStoreError("The completed freeze carries no standard sealed checkpoint") from error
        if checkpoint_run != run_id or checkpoint_stage != "freeze":
            raise PlatformStoreError("The completed freeze checkpoint belongs to another run or stage")
        self._verify_checkpoint_scope(
            freeze_checkpoint,
            run_id=run_id,
            stage="freeze",
            reused_from_run=None,
        )
        try:
            definition, sealed_root = self._verify_sealed(freeze_checkpoint, freeze_receipt)
        except PipelineError as error:
            raise PlatformStoreError("The completed freeze sealed bytes cannot be verified") from error
        _sealed_receipt(
            freeze_receipt,
            run_id=run_id,
            stage="freeze",
            state="completed",
            checkpoint_uri=freeze_checkpoint,
            definition=definition,
            root=sealed_root,
        )
        self._verify_dispatch_request_identity(
            cursor,
            run_id=run_id,
            receipt=receipt,
            freeze_receipt=freeze_receipt,
        )

        self._verify_bound_discover_airflow(cursor, dag_id=dag_id, dag_run_id=dag_run_id, binding=binding)

        closure_shape = (
            "reported_freeze_actual_discover"
            if reported_stage == "freeze"
            else "bound_native_discover_failed"
        )
        payload_receipt = {
            **receipt,
            "reconciliation": reconcile,
            "closure_shape": closure_shape,
            "reported_stage": reported_stage,
            "actual_stage": "discover",
        }
        cursor.execute(_INSERT_STAGE, (run_id, "discover", "failed", _json(payload_receipt), None, None, None))
        if cursor.fetchone() is None:
            raise PlatformStoreError("The bound discover failure lost its run to another writer")
        for later in ("capture", "generate", "verify", "publish"):
            cursor.execute(_INSERT_STAGE, (run_id, later, "blocked", _json({}), None, None, None))
            if cursor.fetchone() is None:
                raise PlatformStoreError(f"The bound blocked record for {later!r} lost its run to another writer")
            self._insert_event(
                connection,
                run_id=run_id,
                stage=later,
                kind="stage",
                operation_id=None,
                payload={"state": "blocked"},
            )
        self._insert_event(
            connection,
            run_id=run_id,
            stage="discover",
            kind="binding",
            operation_id=operation_id,
            payload={
                "operation_key": operation_key,
                "intent_stage": op_stage,
                "reconciled_stage": "discover",
                "reconciled_state": "failed",
                "reported_stage": reported_stage,
                "actual_stage": "discover",
                "dag_id": dag_id,
                "dag_run_id": dag_run_id,
                "task_id": task_id,
                "map_index": map_index,
                "native_run_id": native_run,
                "native_receipt_sha256": native_sha,
                "closure_shape": closure_shape,
            },
        )
        self._insert_event(
            connection,
            run_id=run_id,
            stage="discover",
            kind="stage",
            operation_id=reconcile_operation,
            payload={"state": "failed", "reported_stage": reported_stage, "actual_stage": "discover"},
        )
        self._stage_outcome(connection, run_id=run_id, stage="discover", state="failed")
        cursor.execute(_SETTLE_RECONCILE, (reconcile_operation, run_id, "discover"))
        self._bind_legacy_dispatch_freeze(
            connection,
            cursor,
            run_id=run_id,
            receipt=receipt,
            reconcile=reconcile,
        )

    def _verify_dispatch_request_identity(
        self, cursor, *, run_id: str, receipt: dict, freeze_receipt: dict
    ) -> None:
        """The shared immutable request/upload and direct-freeze identity check."""

        cursor.execute(_READ_RUN_RECORD, (run_id,))
        run_record = cursor.fetchone()
        if run_record is None or not isinstance(run_record[1], str) or not run_record[1]:
            raise PlatformStoreError("The recovery cannot resolve its admitted request")
        cursor.execute(_READ_REQUEST_RECORD, (run_record[1],))
        request_row = cursor.fetchone()
        if request_row is None:
            raise PlatformStoreError("The recovery cannot read its admitted request")
        baseline = request_row[6]
        request_files = receipt.get("request_upload_files")
        if (
            not isinstance(baseline, dict)
            or not isinstance(request_row[2], str)
            or not request_row[2]
            or receipt.get("request_main_assembly") != request_row[2]
            or receipt.get("request_upload_sha256") != baseline.get("handoff_sha256")
            or not isinstance(request_files, dict)
            or any(not isinstance(name, str) or not _is_sha256(value) for name, value in request_files.items())
            or len(request_files) != baseline.get("files")
            or digest(request_files) != baseline.get("handoff_sha256")
            or receipt.get("selection_files") != request_files
            or request_row[2] not in request_files
            or receipt.get("selection_upload_sha256") != baseline.get("handoff_sha256")
            or freeze_receipt.get("handoff_sha256") != baseline.get("handoff_sha256")
        ):
            raise PlatformStoreError("The recovery receipt does not match the admitted request")

    def _verify_bound_discover_airflow(
        self,
        cursor,
        *,
        dag_id: str,
        dag_run_id: str,
        binding: dict,
    ) -> None:
        """Require the unique successful freeze, failed discover, and unexecuted successors."""

        map_index = binding.get("map_index")
        cursor.execute(_READ_ADMITTED_FACTS, (dag_id, dag_run_id, dag_id, dag_run_id))
        facts = cursor.fetchall()
        dag_rows = [row for row in facts if row[0] == "dag_run"]
        if len(dag_rows) != 1 or dag_rows[0][3] != "failed":
            raise PlatformStoreError("The bound DagRun does not prove its terminal failed fact")
        by_task: dict[str, list[tuple[int, object, int]]] = {}
        for _, task, task_map, task_state, task_try in facts:
            if isinstance(task, str):
                by_task.setdefault(task, []).append((task_map, task_state, task_try))
        freeze_rows = by_task.get("freeze") or []
        if len(freeze_rows) != 1 or freeze_rows[0][1] != "success":
            raise PlatformStoreError("The real freeze task is not the completed prerequisite of discover")
        discover_rows = by_task.get("discover") or []
        if (
            len(discover_rows) != 1
            or discover_rows[0][0] != map_index
            or discover_rows[0][1] != "failed"
            or type(discover_rows[0][2]) is not int
            or discover_rows[0][2] < 1
        ):
            raise PlatformStoreError("The real discover task does not prove its bound failed fact")
        for later in ("capture", "generate", "verify", "publish"):
            rows = by_task.get(later) or []
            if len(rows) != 1 or rows[0][1] != "upstream_failed":
                raise PlatformStoreError(f"The successor task {later!r} does not prove its never-executed fact")

    def _bind_legacy_dispatch_freeze(self, connection, cursor, *, run_id: str, receipt: dict, reconcile: dict) -> None:
        """Repair the one legacy freeze dispatch whose task key is missing."""

        cursor.execute(_READ_RUN_DISPATCH_FOR_BINDING, (run_id,))
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise PlatformStoreError("The run does not carry exactly one dispatch intent")
        operation_id, stage, op_state, dag_id, dag_run_id, bound_task, bound_map, operation_key = rows[0]
        target = operation_key.rsplit("/", 1)[-1].split(":", 1)[0]
        if stage != "freeze" or target != "freeze" or op_state != "succeeded":
            raise PlatformStoreError("The legacy dispatch is not the settled freeze intent")
        if not isinstance(dag_id, str) or not dag_id or not isinstance(dag_run_id, str) or not dag_run_id:
            raise PlatformStoreError("The legacy dispatch lacks its DagRun identity")
        if native_run_id(dag_run_id) != run_id:
            raise PlatformStoreError("The legacy dispatch DagRun does not derive this platform run")
        already_bound = (bound_task, bound_map) == ("freeze", -1)
        if (bound_task is not None or bound_map is not None) and not already_bound:
            raise PlatformStoreError("The legacy dispatch is already bound to another Airflow task")
        if not already_bound:
            cursor.execute(_LOCK_ADMISSION)
            admission = cursor.fetchone()
            if admission is None or admission[0] != "closed":
                raise PlatformStoreError("A legacy dispatch binding requires a closed admission")
        cursor.execute(_READ_STAGE_ROW, (run_id, "freeze"))
        freeze = cursor.fetchone()
        if freeze is None or freeze[0] != "completed":
            raise PlatformStoreError("The legacy dispatch has no completed freeze")
        freeze_receipt = _stored_receipt(freeze)
        checkpoint = freeze[2]
        reused = freeze[3] if len(freeze) > 3 else None
        if (
            not isinstance(freeze_receipt, dict)
            or reused is not None
            or checkpoint != f"store://sealed/{run_id}/freeze"
        ):
            raise PlatformStoreError("The legacy dispatch freeze checkpoint is not the direct run checkpoint")
        self._verify_checkpoint_scope(checkpoint, run_id=run_id, stage="freeze", reused_from_run=None)
        try:
            definition, sealed_root = self._verify_sealed(checkpoint, freeze_receipt)
        except PipelineError as error:
            raise PlatformStoreError("The legacy dispatch sealed freeze cannot be verified") from error
        _sealed_receipt(
            freeze_receipt,
            run_id=run_id,
            stage="freeze",
            state="completed",
            checkpoint_uri=checkpoint,
            definition=definition,
            root=sealed_root,
        )
        self._verify_dispatch_request_identity(
            cursor,
            run_id=run_id,
            receipt=receipt,
            freeze_receipt=freeze_receipt,
        )
        cursor.execute(_READ_ORPHAN_LIFECYCLE, (operation_id, f"{dag_id}/{dag_run_id}"))
        lifecycle = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        task = lifecycle.get("scheduling")
        dag_run = lifecycle.get("dag_run")
        cursor.execute(_READ_DISPATCH_TASK_FACT, (dag_id, dag_run_id, "freeze"))
        fact = cursor.fetchall()
        expected_task = ("success", "terminal") if already_bound else (None, "unknown")
        if task != expected_task or len(fact) != 1:
            raise PlatformStoreError("The legacy dispatch task key is not uniquely provable")
        map_index, task_state, try_number, dag_run_state = fact[0]
        if (
            map_index != -1
            or dag_run != ("failed", "terminal")
            or task_state != "success"
            or dag_run_state != "failed"
        ):
            raise PlatformStoreError("The legacy dispatch has no successful freeze under a failed DagRun")
        if already_bound:
            return
        cursor.execute(_BIND_LEGACY_DISPATCH, ("freeze", -1, operation_id, run_id))
        if cursor.fetchone() is None:
            raise PlatformStoreError("The legacy dispatch binding lost its operation to another writer")
        self._insert_event(
            connection,
            run_id=run_id,
            stage="freeze",
            kind="binding",
            operation_id=operation_id,
            payload={
                "binding_kind": "dispatch",
                "operation_key": operation_key,
                "dag_id": dag_id,
                "dag_run_id": dag_run_id,
                "task_id": "freeze",
                "map_index": -1,
                "try_number": try_number,
                "task_state": task_state,
                "dag_run_state": dag_run_state,
                "native_receipt_sha256": receipt.get("native_receipt_sha256"),
                "evidence": reconcile.get("evidence"),
            },
        )

    def _stage_outcome(self, connection, *, run_id: str, stage: str, state: str) -> None:
        """The run-terminal transition a stage record concludes, in the same transaction.

        A failed or cancelled stage ends its run; the confirmed publication
        ends it successfully.  The guarded update keeps the first terminal
        outcome: a later record never rewrites a succeeded, failed or
        cancelled run.  The same transaction settles the stage's operations:
        every decided outcome closes the stage's non-reconcile operations -
        claimed and never-claimed alike - to the mapped terminal state (a
        ``cancel`` command closes as ``succeeded``: whether it actually
        cancelled is the stage record's business), so no queued/running shadow
        is left for maintenance.  A blocked stage never entered execution: it
        closes only its still-queued (never dispatched) intents as cancelled
        non-executions and never rewrites a claimed operation.  An unresolved
        outcome freezes only the claimed operation as unknown and leaves the
        run in place until the evidenced reconciliation, which then settles
        the rest.
        """

        run_outcome: str | None = None
        if state == "completed":
            outcome = "succeeded"
            if stage == "publish":
                run_outcome = "succeeded"
        elif state in {"failed", "cancelled"}:
            outcome = state
            run_outcome = state
        elif state == "blocked":
            # A blocked stage never entered execution: its still-queued intents are the
            # evidenced non-execution - they close as cancelled, never as failed, and a
            # claimed (possibly dispatched) operation is never rewritten here.
            connection.cursor().execute(_SETTLE_BLOCKED_OPERATIONS, (run_id, stage))
            return
        elif state == "unresolved":
            outcome = "unresolved"
        else:
            return
        cursor = connection.cursor()
        if outcome == "unresolved":
            cursor.execute(_SETTLE_UNKNOWN, (run_id, stage))
            cursor.execute(_SET_RUN_UNRESOLVED, (run_id,))
            return
        cursor.execute(_SETTLE_OPERATIONS, (outcome, run_id, stage))
        if run_outcome is not None:
            cursor.execute(_SET_RUN_OUTCOME, (run_outcome, run_id))

    def _insert_event(self, connection, *, run_id: str, stage: str, kind: str, operation_id, payload) -> None:
        """Append one run event in the same transaction as the state change it describes."""

        cursor = connection.cursor()
        cursor.execute(_NEXT_EVENT_SEQ, (run_id,))
        row = cursor.fetchone()
        sequence = row[0] if row else 1
        cursor.execute(_INSERT_EVENT, (run_id, sequence, kind, stage, operation_id, _json(payload)))

    def _verify_checkpoint_scope(self, checkpoint_uri, *, run_id: str, stage: str, reused_from_run: str | None) -> None:
        """A parseable sealed locator must name this run or the proven origin of its reuse chain.

        Being able to parse a locator is not a consumption right: a reference
        that reads as the standard ``store://sealed/<run_id>/<stage>`` locator
        is refused unless it names this run, or the direct parent's stage row
        proves the chain up to the original producer the locator names - every
        link explicit in ``reused_from_run``, every intermediate row completed
        and carrying the same locator, bounded and never walking a cycle.
        Run ancestry alone is never stage lineage.  Non-locator references
        stay opaque here and fail closed at their own resolver contract.
        """

        if not isinstance(checkpoint_uri, str) or not checkpoint_uri.startswith(SEALED_URI_PREFIX):
            return
        try:
            uri_run, uri_stage = parse_sealed_uri(checkpoint_uri)
        except PipelineError:
            # Not a standard locator: the deploy-side resolver contract decides.
            return
        if uri_stage != stage:
            raise PlatformStoreError("The sealed checkpoint names another stage than the confirmation")
        if uri_run == run_id:
            return
        if reused_from_run is not None and uri_run == self._stage_reuse_origin(reused_from_run, stage):
            return
        raise PlatformStoreError(
            "The sealed checkpoint names a run that is neither the current run nor the proven origin "
            "of its registered stage reuse chain"
        )

    def _stage_reuse_origin(self, run_id: str, stage: str) -> str | None:
        """The original producer one stage row's registered reuse chain resolves to, or ``None``.

        Every link must be explicit: the row names its direct source in
        ``reused_from_run``, every intermediate row is completed and carries
        the same locator, and the first row without a reuse declaration must
        name its own run.  The walk is bounded and never walks a cycle.
        """

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_STAGE, (run_id, stage))
            row = cursor.fetchone()
        if row is None or row[0] != "completed" or not row[2]:
            return None
        locator = row[2]
        seen = {run_id}
        current = run_id
        while row[3] is not None:
            reused = row[3]
            if reused in seen or len(seen) >= REUSE_LINEAGE_DEPTH:
                return None
            seen.add(reused)
            with self._transaction() as connection:
                cursor = connection.cursor()
                cursor.execute(_READ_STAGE, (reused, stage))
                row = cursor.fetchone()
            if row is None or row[0] != "completed" or row[2] != locator:
                return None
            current = reused
        if not isinstance(locator, str) or not locator.startswith(SEALED_URI_PREFIX):
            return None
        with suppress(PipelineError):
            origin_run, origin_stage = parse_sealed_uri(locator)
            if origin_stage == stage and origin_run == current:
                return origin_run
        return None

    def _verify_reuse_source(self, *, run_id: str, stage: str, checkpoint_uri, reused_from_run: str) -> dict:
        """A linked attempt may reuse only the registered source stage of its own parent run.

        The caller's string never grants consumption: the source must be the
        run's registered ``parent_run_id``, its stage record must be the
        confirmed completion whose registered checkpoint is exactly the
        reference being confirmed, and that registered receipt must re-verify
        against the sealed bytes.  The locked transaction re-checks only the
        same identity and receipt digest.
        """

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_RUN_PARENT, (run_id,))
            parent_row = cursor.fetchone()
            cursor.execute(_READ_STAGE, (reused_from_run, stage))
            source_row = cursor.fetchone()
            cursor.execute(_READ_STAGE_TIME, (reused_from_run, stage))
            source_time = cursor.fetchone()
        if parent_row is None or parent_row[0] != reused_from_run:
            raise PlatformStoreError("Reuse requires the registered parent run of this linked attempt")
        if source_row is None or source_row[0] != "completed":
            raise PlatformStoreError("Reuse names no confirmed source stage of that run and stage")
        if source_row[2] != checkpoint_uri:
            raise PlatformStoreError("The reused checkpoint is not the source stage's registered reference")
        definition, sealed_root = self._verify_sealed(source_row[2], source_row[1])
        _sealed_receipt(
            source_row[1],
            run_id=reused_from_run,
            stage=stage,
            state="completed",
            checkpoint_uri=source_row[2],
            definition=definition,
            root=sealed_root,
        )
        return {
            "checkpoint_uri": source_row[2],
            "receipt_digest": digest(source_row[1]),
            "original_executed_at": source_time[0] if source_time is not None else None,
        }

    def _verify_sealed(self, checkpoint_uri, receipt, *, completed: bool = True) -> tuple[dict | None, Path]:
        """Rebind the real sealed delivery for any stage before any registration.

        One owner for all six stages: the receipt's URI and inventory are
        self-reported; the bytes are not.  The resolver locates the sealed
        tree, ``read_delivery_manifest`` re-binds every subject record to the
        current bytes, the receipt's artifact inventory must equal those
        records verbatim, the receipt's handoff must be a sha256 equal to the
        sealed ``input/handoff.json`` record, and the tree must cover the
        stage contract's declared evidence paths.  The definition the quality
        inventory binds to is rebuilt - from that very sealed tree, never
        from platform records - only where a quality report exists
        (verify/publish).
        """

        if self._sealed is None:
            raise PlatformStoreError("Sealed storage is not configured; a completion cannot be registered")
        root = Path(self._sealed(checkpoint_uri))
        manifest = read_delivery_manifest(root)
        subject = manifest.get("subject") or {}
        stage = receipt.get("stage")
        if completed and stage in DELIVERY_STAGES and subject.get("digest") != receipt.get("subject_sha256"):
            raise PlatformStoreError("The sealed delivery subject does not match the receipt")
        records = subject.get("files") or []
        artifacts = receipt.get("artifacts")
        from ..interface_records import InterfaceRecordError, subject_file_records

        try:
            subject_file_records(
                artifacts if isinstance(artifacts, list) else [], "sealed_receipt_invalid", "receipt.artifacts"
            )
        except InterfaceRecordError as error:
            raise PlatformStoreError("The receipt's artifact inventory does not bind the sealed delivery") from error
        stamped = {record["path"]: (record["bytes"], record["sha256"]) for record in records}
        received = {record["path"]: (record["bytes"], record["sha256"]) for record in artifacts}
        subject_stamped = {path: value for path, value in stamped.items() if not _report_member(path)}
        subject_received = {path: value for path, value in received.items() if not _report_member(path)}
        if subject_received != subject_stamped:
            raise PlatformStoreError("The receipt's artifact inventory does not bind the sealed delivery")
        for path, (size, checksum) in received.items():
            if not _report_member(path):
                continue
            # Report members (class projected from the path) are verified
            # against the actual sealed bytes; the subject stays report-free.
            try:
                member = confined(root, path)
            except PipelineError as error:
                raise PlatformStoreError("The receipt's report inventory does not bind the sealed bytes") from error
            if not member.is_file() or member.stat().st_size != size or file_digest(member) != checksum:
                raise PlatformStoreError("The receipt's report inventory does not bind the sealed bytes")
        if not completed:
            # Byte binding only: every declared member was just verified against
            # the sealed bytes.  The handoff index, tool identities, the stage
            # contract's declared evidence and the rebuilt definition are
            # success qualifications and are never required to register a real
            # failure - and never granted by one.
            return None, root
        # The handoff identity is the sealed admission index's inventory digest
        # (the same owner as describe_handoff); the index file's own sha stays
        # an ordinary manifest artifact record, never conflated with it.
        try:
            index = read_data(confined(root, "input/handoff.json"))
        except (PipelineError, OSError) as error:
            raise PlatformStoreError("The sealed tree carries no readable input/handoff.json index") from error
        files = index.get("package_files") if isinstance(index, dict) else None
        if not isinstance(files, dict) or not files:
            raise PlatformStoreError("The sealed input/handoff.json index has no package_files inventory")
        for name, checksum in files.items():
            if not isinstance(name, str) or not name:
                raise PlatformStoreError("The sealed handoff index carries an invalid original path")
            try:
                artifact_path_parts(name)
            except PipelineError as error:
                raise PlatformStoreError("The sealed handoff index carries an unsafe original path") from error
            if not _is_sha256(checksum):
                raise PlatformStoreError("The sealed handoff index carries an invalid original digest")
            record = stamped.get("input/" + name)
            if record is None or record[1] != checksum:
                raise PlatformStoreError("The receipt's handoff binding does not match the sealed original bytes")
        # The prepared index must be complete: every staged input member except the
        # carried request and the index itself has to appear in its inventory.
        indexed = {path[len("input/") :] for path in stamped if path.startswith("input/")}
        missing = sorted(indexed - set(files) - {"request.json", "handoff.json"})
        if missing:
            raise PlatformStoreError(f"The sealed input/handoff.json index is incomplete: {missing}")
        expected_handoff = digest(files)
        if stage != "freeze":
            # Freeze binds the full admitted upload; every later stage binds the
            # selected native transfer identity, byte-bound to the native discovery
            # record.  A missing or self-declared-only identity refuses; the prepared
            # index above stays member-for-member and completeness validated.
            transfer_path = root / "transfer-manifest.json"
            if not transfer_path.is_file():
                raise PlatformStoreError("The sealed stage tree carries no native transfer identity")
            transfer = read_data(transfer_path)
            transfer_handoff = transfer.get("handoff_sha256") if isinstance(transfer, dict) else None
            if not _is_sha256(transfer_handoff):
                raise PlatformStoreError("The sealed transfer manifest carries no handoff identity")
            try:
                discovery = read_data(confined(root, "evidence/discovery.json"))
            except (PipelineError, OSError) as error:
                raise PlatformStoreError("The sealed tree carries no readable native discovery record") from error
            if not isinstance(discovery, dict) or discovery.get("handoff_sha256") != transfer_handoff:
                raise PlatformStoreError("The native transfer identity is not byte-bound to the sealed discovery")
            receipt_index = index.get("input_receipt")
            inventory = receipt_index.get("inventory") if isinstance(receipt_index, dict) else None
            if not isinstance(inventory, dict) or not inventory:
                raise PlatformStoreError("The sealed handoff index carries no native input inventory")
            natives = {path[len("input/cad/") :] for path in stamped if path.startswith("input/cad/")}
            if set(inventory) != natives:
                raise PlatformStoreError(
                    "The native input inventory does not match the sealed native originals: "
                    f"missing={sorted(natives - set(inventory))} extra={sorted(set(inventory) - natives)}"
                )
            for name, checksum in inventory.items():
                record = stamped.get("input/cad/" + str(name))
                if record is None or record[1] != checksum:
                    raise PlatformStoreError("The native input inventory is not byte-bound to the sealed originals")
            native_files = discovery.get("native_files")
            if not isinstance(native_files, dict) or native_files != inventory:
                raise PlatformStoreError("The native input inventory differs from the sealed discovery record")
            if digest(inventory) != transfer_handoff:
                raise PlatformStoreError(
                    "The native transfer identity is not the digest of the sealed native inventory"
                )
            expected_handoff = transfer_handoff
        if expected_handoff != receipt.get("handoff_sha256"):
            raise PlatformStoreError("The receipt's handoff binding does not match the sealed identity for this stage")
        if stage == "freeze":
            try:
                sealed_tool = read_data(confined(root, "tool.json"))
            except (PipelineError, OSError) as error:
                raise PlatformStoreError("The sealed tree carries no readable tool.json") from error
            if receipt.get("tool") != sealed_tool:
                raise PlatformStoreError("The frozen receipt's tool identity does not match the sealed tool.json")
            _machine_tool_identity(sealed_tool, role="portable")
        if stage in {"discover", "capture"}:
            try:
                sealed_native = read_data(confined(root, "reports/native-tool.json"))
            except (PipelineError, OSError) as error:
                raise PlatformStoreError("The sealed tree carries no readable native tool record") from error
            if receipt.get("native_tool") != sealed_native:
                raise PlatformStoreError(
                    "The native stage receipt's tool identity does not match the sealed native tool record"
                )
            _machine_tool_identity(sealed_native, role="native")
        missing = [
            entry
            for entry in STAGE_EVIDENCE.get(stage, ())
            if _evidence_path(entry) and not _covered(root, stamped, entry)
        ]
        if missing:
            raise PlatformStoreError(f"The sealed {stage!r} delivery does not cover its declared evidence: {missing}")
        if stage in {"verify", "publish"}:
            from ..verification.solidworks_urdf import rebuilt_definition

            definition = rebuilt_definition(root)
            if not isinstance(definition, dict) or not isinstance(definition.get("source"), dict):
                raise PlatformStoreError("The sealed delivery does not rebuild a definition inventory")
            return definition, root
        return None, root

    def confirmed_stage(self, run_id: str, stage: str) -> dict | None:
        """The platform-authority reuse query: the sealed receipt of a confirmed stage, or refusal.

        Returns ``None`` while the stage is not confirmed yet (the caller
        proceeds normally); a row that claims confirmation without binding its
        receipt fields is a damaged store and raises instead.  The sealed-byte
        rebinding (checkpoint/blob storage) is the deploy side's storage check;
        this query binds the record's identity, outcome and subject digest.
        """

        if stage not in CONFIRMED_STAGE_STATES:
            raise PlatformStoreError(f"stage must be one of {tuple(CONFIRMED_STAGE_STATES)}: {stage!r}")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_STAGE, (run_id, stage))
            row = cursor.fetchone()
        if row is None or row[0] != "completed":
            return None
        receipt = row[1]
        self._verify_checkpoint_scope(row[2], run_id=run_id, stage=stage, reused_from_run=row[3])
        definition, sealed_root = self._verify_sealed(row[2], receipt)
        _sealed_receipt(
            receipt,
            run_id=run_id,
            stage=stage,
            state="completed",
            checkpoint_uri=row[2],
            definition=definition,
            root=sealed_root,
        )
        return receipt

    # ------------------------------------------------------------- read side

    # ------------------------------------------------ run progression (driver)

    def start_run(self, run_id: str) -> dict:
        """Move an admitted run from ``queued`` to ``running`` (idempotent).

        The dispatch intent admitted the run; the first driver action claims
        it.  A terminal or unresolved run is never restarted - a redo is a
        linked run - and repeating the call while running keeps the first
        start.
        """

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_LOCK_RUN, (run_id,))
            row = cursor.fetchone()
            if row is None:
                raise PlatformStoreError(f"The run {run_id!r} is not admitted")
            state = row[0]
            if state == "running":
                return {"run_id": run_id, "state": state}
            if state != "queued":
                raise PlatformStoreError(f"The run cannot start from state {state!r}; link or reconcile instead")
            cursor.execute(_START_RUN, (run_id,))
            started = cursor.fetchone()
        if started is None:
            raise PlatformStoreError("The run was started by a concurrent writer")
        return {"run_id": run_id, "state": "running"}

    def append_event(self, run_id: str, *, stage: str, kind: str, payload: dict | None = None) -> None:
        """Append one observed run event (live activity) in its own transaction."""

        if stage not in STAGE_IDS:
            raise PlatformStoreError(f"The event stage must be one of {tuple(STAGE_IDS)}: {stage!r}")
        if not isinstance(kind, str) or not kind:
            raise PlatformStoreError("The event kind must be a nonempty string")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_LOCK_RUN, (run_id,))
            if cursor.fetchone() is None:
                raise PlatformStoreError(f"The run {run_id!r} is not admitted")
            self._insert_event(
                connection, run_id=run_id, stage=stage, kind=kind, operation_id=None, payload=payload or {}
            )

    def run_events(self, run_id: str, *, after_seq: int = 0, limit: int = 200) -> list[dict]:
        """One run's recorded events, strictly ordered and bounded (the event read API).

        ``after_seq`` pages forward only; ``limit`` is capped so one read never
        pulls an unbounded history.
        """

        if not isinstance(after_seq, int) or after_seq < 0:
            raise PlatformStoreError("after_seq must be a non-negative integer")
        if not isinstance(limit, int) or limit < 1:
            raise PlatformStoreError("limit must be a positive integer")
        bounded = min(limit, _EVENTS_LIMIT_CAP)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_EVENTS, (run_id, after_seq, bounded))
            rows = cursor.fetchall()
        return [
            {
                "seq": row[0],
                "at": row[1],
                "kind": row[2],
                "stage": row[3],
                "operation_id": row[4],
                "payload": row[5],
            }
            for row in rows
        ]

    def run_record(self, run_id: str) -> dict | None:
        """One run's identity and lineage (no display fields, no receipts)."""

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_RUN_RECORD, (run_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "run_id": run_id,
            "state": row[0],
            "request_id": row[1],
            "parent_run_id": row[2],
            "from_stage": row[3],
            "tool_identity": row[4],
        }

    def request_record(self, request_id: str) -> dict | None:
        """One admitted request's immutable identity and its initial dispatch digest.

        ``principal``/``target_hardware``/``main_assembly`` are the sealed acceptance
        facts; ``dispatch_digest`` is the initial dispatch intent's parameter digest,
        so an uncertain retry can prove the requested frame equals the immutable
        admission before re-triggering.  ``None`` means the request row is absent.
        """
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_REQUEST_RECORD, (request_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "request_id": request_id,
            "principal": row[0],
            "target_hardware": row[1],
            "main_assembly": row[2],
            "sealed_at": row[3],
            "dispatch_digest": row[4],
            "purpose": row[5].get("purpose") if isinstance(row[5], dict) else None,
            "baseline": row[6],
        }

    def stage_records(self, run_id: str) -> dict[str, dict]:
        """Every recorded stage row of one run, keyed by the engineering stage."""

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_STAGE_RECORDS, (run_id,))
            rows = cursor.fetchall()
        return {
            row[0]: {
                "stage": row[0],
                "state": row[1],
                "receipt": row[2],
                "checkpoint_uri": row[3],
                "reused_from_run": row[4],
                "original_executed_at": row[5],
            }
            for row in rows
        }

    def sealed_path(self, checkpoint_uri: str):
        """Resolve one registered checkpoint reference through sealed storage."""

        if self._sealed is None:
            raise PlatformStoreError("Sealed storage is not configured; a checkpoint cannot be resolved")
        return Path(self._sealed(checkpoint_uri))

    # ------------------------------------------------------------ run display
    def run_display(self, run_id: str) -> RunDisplay | None:
        """One run's display fields; ``None`` while the run has no platform row."""

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_RUN_DISPLAY, (run_id,))
            row = cursor.fetchone()
        return RunDisplay(*row) if row is not None else None

    def run_displays(self) -> dict[str, RunDisplay]:
        """Every run carrying display data (name or tombstone), in one read."""

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_RUN_DISPLAYS)
            rows = cursor.fetchall()
        return {row[0]: RunDisplay(*row) for row in rows}

    def set_run_display(self, run_id: str, display_name: str | None) -> RunDisplay:
        """Set or clear the display name of an admitted run (display-only)."""

        return self._update_run_display(_SET_RUN_DISPLAY, (display_name, run_id), run_id)

    def hide_run(self, run_id: str, *, actor: str) -> RunDisplay:
        """Tombstone the run; repeating keeps the first tombstone; the actor is recorded."""

        return self._update_run_display(_SET_RUN_HIDDEN, (actor, run_id), run_id)

    def restore_run(self, run_id: str) -> RunDisplay:
        """Clear the tombstone; idempotent for a never-hidden run."""

        return self._update_run_display(_RESTORE_RUN, (run_id,), run_id)

    def _update_run_display(self, statement, parameters, run_id: str) -> RunDisplay:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(statement, parameters)
            row = cursor.fetchone()
        if row is None:
            raise RunNotAdmittedError(f"The run {run_id!r} is not admitted")
        return RunDisplay(*row)

    def lifecycle_rows(self) -> list[dict]:
        """Every row of the lifecycle projection (single query, no pre-filtering)."""

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_LIFECYCLE)
            names = [column[0] for column in cursor.description or ()]
            rows = cursor.fetchall()
        return [dict(zip(names, row, strict=True)) for row in rows]


def _require_open(connection) -> None:
    """Assert the admission gate inside the current transaction, holding its row lock."""

    cursor = connection.cursor()
    cursor.execute(_LOCK_ADMISSION)
    row = cursor.fetchone()
    if row is None:
        raise PlatformStoreError("The platform admission row is missing")
    if row[0] != "open":
        raise AdmissionClosedError(f"Admission is closed: {row[1] or 'no reason recorded'}")


def _is_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _machine_tool_identity(record, *, role: str) -> None:
    """The light structural identity of one tool record (owner: schemas.TOOL_SCHEMA)."""

    from ..schemas import TOOL_SCHEMA

    if (
        not isinstance(record, dict)
        or record.get("schema_version") != TOOL_SCHEMA
        or record.get("pipeline_id") != PIPELINE_ID
        or not isinstance(record.get("version"), str)
        or not record.get("version")
        or not _is_sha256(record.get("source_sha256"))
        or (record.get("runtime") or {}).get("role") != role
    ):
        raise PlatformStoreError(f"The sealed tool identity is not a complete {role} tool record")


def _evidence_path(entry) -> bool:
    """A contract evidence entry that names a sealed path, not a receipt field."""

    return isinstance(entry, str) and "/" in entry and not entry.startswith("/")


def _covered(root: Path, records: dict, entry: str) -> bool:
    """Whether the sealed tree covers one declared evidence path or directory."""

    if entry.endswith("/"):
        return any(path.startswith(entry) for path in records) or (root / entry).is_dir()
    return entry in records or (root / entry).is_file()


def _sealed_receipt(receipt, *, run_id: str, stage: str, state: str, checkpoint_uri, definition, root=None) -> None:
    """The one sealed-receipt validator shared by the stage write and reuse paths.

    A terminal confirmation must bind its receipt completely, and the reuse
    query runs exactly the same validation - downstream can never trust the
    state string alone.  A receipt produced by a reused stage keeps its
    original producer's identity: it must name either this run or the run of
    the locator it was carried under.  ``blocked`` records an unexecuted
    stage and carries no receipt requirements; native stages validate the
    bindings they carry.
    """

    if state == "blocked":
        return
    if state in {"pending", "running"}:
        return
    if not isinstance(receipt, dict):
        raise PlatformStoreError("A sealed stage confirmation must carry its bound receipt")
    if receipt.get("schema_version") != BUNDLE_SCHEMA:
        raise PlatformStoreError("The sealed receipt uses an unknown schema")
    named = {run_id}
    if isinstance(checkpoint_uri, str) and checkpoint_uri.startswith(SEALED_URI_PREFIX):
        with suppress(PipelineError):
            named.add(parse_sealed_uri(checkpoint_uri)[0])
    if receipt.get("run_id") not in named or receipt.get("stage") != stage:
        raise PlatformStoreError("The sealed receipt names another run or stage")
    if state == "unresolved":
        if receipt.get("state") != "unresolved":
            raise PlatformStoreError("An unresolved stage must be recorded as unresolved")
        return
    if state == "failed":
        if receipt.get("state") != "failed" or receipt.get("passed") is not False:
            raise PlatformStoreError("A failed stage must be sealed with its failed receipt")
        return
    if state == "cancelled":
        if receipt.get("state") != "cancelled" or receipt.get("passed") is not False:
            raise PlatformStoreError("A cancelled stage must be sealed with its cancelled receipt")
        return
    handoff = receipt.get("handoff_sha256")
    if not _is_sha256(handoff):
        raise PlatformStoreError("The sealed receipt lacks its handoff binding")
    expected = CONFIRMED_STAGE_STATES.get(stage)
    if expected is None:
        raise PlatformStoreError(f"The stage {stage!r} carries no confirmed vocabulary")
    if receipt.get("state") not in expected:
        raise PlatformStoreError("A completed stage must be sealed with its confirmed receipt state")
    if receipt.get("passed") is not CONFIRMED_STAGE_PASSED[stage]:
        raise PlatformStoreError("The sealed receipt does not carry the stage's confirmed outcome")
    if not isinstance(checkpoint_uri, str) or not checkpoint_uri:
        raise PlatformStoreError("An unsealed stage completion is refused: no checkpoint reference")
    if not isinstance(receipt.get("artifacts"), list) or not receipt["artifacts"]:
        raise PlatformStoreError("The sealed receipt lacks its artifact inventory")
    if stage == "freeze":
        tool = receipt.get("tool")
        if not isinstance(tool, dict) or not tool:
            raise PlatformStoreError("The frozen receipt lacks its tool identity")
    if stage in {"discover", "capture"}:
        native_tool = receipt.get("native_tool")
        if not isinstance(native_tool, dict) or not native_tool:
            raise PlatformStoreError("The native stage receipt lacks its native tool identity")
    subject = receipt.get("subject_sha256")
    if stage in DELIVERY_STAGES and not _is_sha256(subject):
        raise PlatformStoreError("The sealed receipt lacks its subject digest")
    if stage in {"verify", "publish"}:
        from ..verification.solidworks_urdf import require_qualified_report

        try:
            quality = require_qualified_report(receipt.get("quality"), definition, root=root)
        except PipelineError as error:
            raise PlatformStoreError(str(error)) from error
        if quality.get("subject_sha256") != subject:
            raise PlatformStoreError("The sealed receipt's quality payload is bound to another subject")
        if root is not None:
            # The sealed report bytes are the delivery's own record: the file in
            # the sealed tree must itself be a qualified report for this
            # subject, its full deterministic payload (every check and detail)
            # must equal the inline quality payload, and the report member must
            # be registered with byte binding.  A sealed passed-bit flip, a
            # changed check detail or an unregistered report member refuses
            # instead of registering a contradictory success; the read-back and
            # reuse paths run this same check.
            try:
                sealed_quality = require_qualified_report(
                    read_data(confined(root, "reports/quality.json")), definition, root=root
                )
            except (PipelineError, OSError) as error:
                raise PlatformStoreError("The sealed tree carries no qualified reports/quality.json") from error
            if canonical(sealed_quality) != canonical(quality):
                raise PlatformStoreError("The sealed quality report contradicts the receipt's quality payload")
            artifacts = receipt.get("artifacts")
            members = (
                [
                    record
                    for record in artifacts
                    if isinstance(record, dict) and record.get("path") == "reports/quality.json"
                ]
                if isinstance(artifacts, list)
                else []
            )
            if not members:
                raise PlatformStoreError("The registered objects lack the byte-bound reports/quality.json member")
            report_path = root / "reports" / "quality.json"
            if not any(
                record.get("bytes") == report_path.stat().st_size and record.get("sha256") == file_digest(report_path)
                for record in members
            ):
                raise PlatformStoreError("The registered reports/quality.json member does not bind the sealed bytes")
    if stage == "publish":
        submission = receipt.get("submission")
        if (
            not isinstance(submission, dict)
            or submission.get("passed") is not True
            or submission.get("state") not in {"published", "updated", "noop"}
            or not submission.get("url")
            or submission.get("subject_sha256") not in (None, subject)
        ):
            raise PlatformStoreError("The sealed publication receipt has no confirmed submission")


def _require_successor(self, connection, *, run_id: str, kind: str, stage: str, binding: dict | None = None) -> None:
    """The successor must be a legal transition of the admitted run.

    The run row is locked; non-terminal kinds need a running run (queued or
    unresolved runs cannot jump straight into downstream stages), terminal
    runs continue only through cancel/reconcile, a confirmed publication can
    never be cancelled, and every stage after the run's start stage requires
    this run's own completed predecessor stage.  That predecessor was rebound
    from its sealed root before this transaction; here only the same
    identity, checkpoint reference and receipt digest are re-checked.
    """

    cursor = connection.cursor()
    cursor.execute(_LOCK_RUN, (run_id,))
    row = cursor.fetchone()
    if row is None:
        raise PlatformStoreError(f"The run {run_id!r} is not admitted")
    state, from_stage = row[0], row[1]
    if state in _TERMINAL_RUN_STATES and kind not in _TERMINAL_KINDS:
        raise PlatformStoreError(f"The run is terminal ({state}); only cancel/reconcile operations continue")
    if kind == "cancel":
        cursor.execute(_CONFIRMED_PUBLISH, (run_id,))
        if cursor.fetchone() is not None:
            raise PlatformStoreError("A confirmed publication can never be cancelled")
    if kind in _TERMINAL_KINDS:
        return
    if state != "running":
        raise PlatformStoreError(f"The run is not running ({state}); successors require a running run")
    start = from_stage if from_stage in STAGE_IDS else STAGE_IDS[0]
    stage_index = STAGE_IDS.index(stage)
    start_index = STAGE_IDS.index(start)
    if stage_index < start_index:
        raise PlatformStoreError(f"The {stage!r} operation precedes this run's start stage {start!r}")
    if stage_index > start_index:
        predecessor = STAGE_IDS[stage_index - 1]
        cursor.execute(_READ_STAGE_ROW, (run_id, predecessor))
        predecessor_row = cursor.fetchone()
        if predecessor_row is None or predecessor_row[0] != "completed":
            raise PlatformStoreError(
                f"The {stage!r} operation requires the completed {predecessor!r} stage of this run"
            )
        if binding is None or binding.get("stage") != predecessor:
            raise PlatformStoreError(
                f"The {predecessor!r} predecessor was not rebound from its sealed root; retry the registration"
            )
        if binding.get("checkpoint_uri") != predecessor_row[2] or binding.get("receipt_digest") != digest(
            predecessor_row[1]
        ):
            raise PlatformStoreError(
                f"The registered {predecessor!r} stage changed after it was verified; retry the registration"
            )


def _matching_operation(row, *, run_id: str, stage: str, kind: str, params_digest: str) -> OperationRecord:
    """The recorded row must match the requested scope and parameters exactly."""

    existing = OperationRecord(*row)
    if (
        existing.params_digest != params_digest
        or existing.run_id != run_id
        or existing.stage != stage
        or existing.kind != kind
    ):
        raise OperationConflictError(
            f"Operation key {existing.operation_key!r} already exists with a different scope or parameters"
        )
    return existing
