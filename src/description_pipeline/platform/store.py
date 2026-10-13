"""The application-side platform store API (v3.3 frozen semantics).

One module, one transaction entry: admission close/open, the short
all-or-nothing admission of a new request with its run and initial dispatch
intent, idempotent operation registration for legitimate successors of an
already-admitted run (cancel/reconcile included), and read-only queries over
the two frozen projection views.  The connection is injected - this module
carries no import-time database driver dependency, adds no HTTP service and no
second controller; the schema, views and roles belong to the deploy side
(``deploy/platform``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
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
    """The same operation key reappeared with different parameters."""

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
    " (operation_id, run_id, stage, kind, state, operation_key, params_digest, dag_run_id, task_id, map_index)"
    " VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s)"
    " RETURNING operation_id, state, operation_key, params_digest"
)
_FIND_OPERATION = (
    "SELECT operation_id, state, operation_key, params_digest FROM platform.operation WHERE operation_key = %s"
)
_READ_LIFECYCLE = (
    "SELECT object_kind, object_id, run_id, state, bucket, updated_at FROM platform.lifecycle_projection"
)


def _json(value: Any) -> str | None:
    """jsonb parameters travel as canonical JSON text; the SQL casts them."""

    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PlatformStore:
    """The application-side store API: the single transaction entry for platform state.

    ``connect`` returns a DB-API connection (psycopg); every call commits or
    rolls back exactly one transaction (DB-API ``with connection`` semantics)
    and never spans an external call.  The shared admission row lock
    serializes new admissions against close/open.
    """

    def __init__(self, connect: Callable[[], Any]):
        self._connect = connect

    # ------------------------------------------------------------- admission

    def close_admission(self, *, reason: str, actor: str) -> AdmissionView:
        """Close the admission gate (drain/stop-controlled); idempotent."""

        return self._set_admission("closed", reason, actor)

    def open_admission(self, *, reason: str, actor: str) -> AdmissionView:
        """Reopen the admission gate for a new operating window; idempotent."""

        return self._set_admission("open", reason, actor)

    def admission(self) -> AdmissionView:
        """The read-only admission projection (single row)."""

        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(_READ_ADMISSION)
            row = cursor.fetchone()
        if row is None:
            raise PlatformStoreError("The platform admission projection is empty")
        return AdmissionView(*row)

    def _set_admission(self, state: str, reason: str, actor: str) -> AdmissionView:
        with self._connect() as connection:
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

    # -------------------------------------------------- gated new admissions

    def submit_request(
        self,
        *,
        request_id: str,
        principal: str,
        target_hardware: str,
        main_assembly: str,
        configuration: dict | None = None,
        baseline: dict | None = None,
    ) -> None:
        """Admit one new request; refused when the gate is closed (nothing is written)."""

        with self._connect() as connection:
            _require_open(connection)
            connection.cursor().execute(
                _INSERT_REQUEST,
                (request_id, principal, target_hardware, main_assembly, _json(configuration), _json(baseline)),
            )

    def create_run(
        self,
        *,
        run_id: str,
        request_id: str,
        from_stage: str | None = None,
        parent_run_id: str | None = None,
        tool_identity: dict | None = None,
        dispatch: dict | None = None,
    ) -> None:
        """Admit one new run; with ``dispatch`` the initial intent lands in the same transaction.

        ``dispatch`` keys: ``stage``, ``operation_key``, ``params_digest`` and
        optional Airflow binding fields ``dag_run_id``/``task_id``/``map_index``.
        """

        with self._connect() as connection:
            _require_open(connection)
            connection.cursor().execute(
                _INSERT_RUN, (run_id, request_id, parent_run_id, from_stage, _json(tool_identity))
            )
            if dispatch is not None:
                payload = dict(dispatch)
                self._insert_operation(
                    connection,
                    run_id=run_id,
                    stage=payload.pop("stage"),
                    kind=payload.pop("kind", "dispatch"),
                    operation_key=payload.pop("operation_key"),
                    params_digest=payload.pop("params_digest"),
                    dispatch=payload,
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

        Same key with the same parameters returns the existing record (a retry
        never restarts a side effect); the same key with different parameters
        raises :class:`OperationConflictError`.  A concurrent duplicate is
        rejected by the unique key; the caller re-reads or reconciles.
        """

        with self._connect() as connection:
            cursor = connection.cursor()
            cursor.execute(_FIND_OPERATION, (operation_key,))
            row = cursor.fetchone()
            if row is not None:
                existing = OperationRecord(*row)
                if existing.params_digest != params_digest:
                    raise OperationConflictError(
                        f"Operation key {operation_key!r} already exists with different parameters"
                    )
                return existing
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
                dispatch.get("dag_run_id"),
                dispatch.get("task_id"),
                dispatch.get("map_index"),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise PlatformStoreError("The operation insert returned no record")
        return OperationRecord(*row)

    # ------------------------------------------------------------- read side

    def lifecycle_rows(self) -> list[dict]:
        """Every row of the frozen lifecycle projection (single query, no pre-filtering)."""

        with self._connect() as connection:
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
