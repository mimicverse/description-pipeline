"""The application-side platform store API (v3.3 frozen semantics).

One module, one transaction entry: admission close/open, ONE new-admission
entry that registers a request with its run and initial dispatch intent in a
single short transaction (all-or-nothing), qualified registration of
successor operations for admitted runs (cancel/reconcile included),
stage-confirmation writes and read-only queries over the two frozen
projection views.

The connection is injected - this module carries no import-time database
driver dependency, adds no HTTP service and no second controller; the schema,
views and roles belong to the deploy side (``deploy/platform``).  Every call
opens one fresh connection, commits or rolls back exactly one transaction and
closes the connection again; no transaction ever spans an external call.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..io import PipelineError


class PlatformStoreError(PipelineError):
    """The platform store refused a write or a read."""


class AdmissionClosedError(PlatformStoreError):
    """A new admission was attempted while the admission gate is closed."""

    code = "admission_closed"


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


_LOCK_ADMISSION = (
    "SELECT state, reason, actor, changed_at, revision FROM platform.admission WHERE id = 1 FOR UPDATE"
)
_READ_ADMISSION = "SELECT state, reason, actor, changed_at, revision FROM platform.admission_projection"
_UPDATE_ADMISSION = (
    "UPDATE platform.admission SET state = %s, reason = %s, actor = %s, changed_at = now(),"
    " revision = revision + 1 WHERE id = 1 RETURNING state, reason, actor, changed_at, revision"
)
_INSERT_REQUEST = (
    "INSERT INTO platform.request (request_id, principal, target_hardware, main_assembly, configuration, baseline)"
    " VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)"
)
_INSERT_RUN = (
    "INSERT INTO platform.run (run_id, request_id, parent_run_id, from_stage, state, tool_identity)"
    " VALUES (%s, %s, %s, %s, 'queued', %s::jsonb)"
)
_INSERT_OPERATION = (
    "INSERT INTO platform.operation"
    " (operation_id, run_id, stage, kind, state, operation_key, params_digest, dag_id, dag_run_id, task_id, map_index)"
    " VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (operation_key) DO NOTHING"
    " RETURNING operation_id, state, operation_key, params_digest, run_id, stage, kind"
)
_FIND_OPERATION = (
    "SELECT operation_id, state, operation_key, params_digest, run_id, stage, kind"
    " FROM platform.operation WHERE operation_key = %s"
)
_LOCK_RUN = "SELECT state FROM platform.run WHERE run_id = %s FOR UPDATE"
_CONFIRMED_PUBLISH = (
    "SELECT 1 FROM platform.operation WHERE run_id = %s AND kind = 'publish' AND state = 'succeeded' LIMIT 1"
)
_INSERT_STAGE = (
    "INSERT INTO platform.stage (run_id, stage, state, receipt, checkpoint_uri, reused_from_run, original_executed_at)"
    " VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)"
    " ON CONFLICT (run_id, stage) DO UPDATE SET"
    " state = EXCLUDED.state, receipt = EXCLUDED.receipt, checkpoint_uri = EXCLUDED.checkpoint_uri,"
    " reused_from_run = EXCLUDED.reused_from_run, original_executed_at = EXCLUDED.original_executed_at,"
    " updated_at = now()"
    " WHERE platform.stage.state <> 'completed'"
    " RETURNING run_id, stage, state"
)
_READ_LIFECYCLE = (
    "SELECT object_kind, object_id, run_id, state, bucket, updated_at FROM platform.lifecycle_projection"
)

#: Operation kinds that may continue on a terminal run (the reconciliation and
#: cancellation commands); every other kind needs a non-terminal run.
_TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled"})
_TERMINAL_KINDS = frozenset({"cancel", "reconcile"})


def _json(value: Any) -> str | None:
    """jsonb parameters travel as canonical JSON text; non-finite numbers are refused.

    The single canonical codec belongs to the shared foundation (R17); until it
    lands this guard fail-closes instead of emitting NaN/Infinity.
    """

    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError as error:
        raise PlatformStoreError(f"Platform parameters must be finite JSON: {error}") from error


class PlatformStore:
    """The application-side store API: the single transaction entry for platform state."""

    def __init__(self, connect: Callable[[], Any]):
        self._connect = connect

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
        Airflow binding fields ``dag_id``/``dag_run_id``/``task_id``/
        ``map_index`` (bound later by reconciliation when unknown).  The
        admission row lock is taken inside the same transaction that inserts
        all three rows: a closed gate (or any failure) leaves nothing behind.
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
            cursor.execute(
                _INSERT_REQUEST,
                (request_id, principal, target_hardware, main_assembly, _json(configuration), _json(baseline)),
            )
            cursor.execute(
                _INSERT_RUN, (run_id, request_id, parent_run_id, from_stage, _json(tool_identity))
            )
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
        """

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_FIND_OPERATION, (operation_key,))
            row = cursor.fetchone()
            if row is not None:
                return _matching_operation(row, run_id=run_id, stage=stage, kind=kind, params_digest=params_digest)
            _require_successor(connection, run_id=run_id, kind=kind)
            return self._insert_operation(
                connection,
                run_id=run_id,
                stage=stage,
                kind=kind,
                operation_key=operation_key,
                params_digest=params_digest,
                dispatch=dispatch or {},
            )

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
    ) -> None:
        """Register one stage record: canonical state plus optional sealed receipt/checkpoint.

        This is the store path that writes ``platform.stage`` rows on the
        platform side.  A sealed ``completed`` row is immutable - a later
        confirmation for the same (run, stage) is an explicit refusal instead
        of a rewrite (a redo is a linked run, not an overwrite).
        """

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                _INSERT_STAGE,
                (run_id, stage, state, _json(receipt), checkpoint_uri, reused_from_run, original_executed_at),
            )
            if cursor.fetchone() is None:
                raise PlatformStoreError(
                    f"The sealed {stage!r} receipt of run {run_id!r} is immutable; start a linked run instead"
                )

    # ------------------------------------------------------------- read side

    def lifecycle_rows(self) -> list[dict]:
        """Every row of the frozen lifecycle projection (single query, no pre-filtering)."""

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


def _require_successor(connection, *, run_id: str, kind: str) -> None:
    """The run must be admitted; terminal runs only continue through terminal kinds."""

    cursor = connection.cursor()
    cursor.execute(_LOCK_RUN, (run_id,))
    row = cursor.fetchone()
    if row is None:
        raise PlatformStoreError(f"The run {run_id!r} is not admitted")
    state = row[0]
    if state in _TERMINAL_RUN_STATES and kind not in _TERMINAL_KINDS:
        raise PlatformStoreError(f"The run is terminal ({state}); only cancel/reconcile operations continue")
    if kind == "cancel":
        cursor.execute(_CONFIRMED_PUBLISH, (run_id,))
        if cursor.fetchone() is not None:
            raise PlatformStoreError("A confirmed publication can never be cancelled")


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
