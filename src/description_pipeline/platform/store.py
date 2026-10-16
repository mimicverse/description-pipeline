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


_LOCK_ADMISSION = (
    "SELECT state, reason, actor, changed_at, revision FROM admission WHERE id = 1 FOR UPDATE"
)
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
_READ_RUN_DISPLAY = (
    "SELECT run_id, display_name, hidden_at, hidden_reason, updated_at FROM run WHERE run_id = %s"
)
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
    "UPDATE run SET state = 'running', updated_at = now() WHERE run_id = %s AND state = 'queued'"
    " RETURNING state"
)
_CLAIM_OPERATION = (
    "UPDATE operation SET state = 'running', updated_at = now()"
    " WHERE operation_key = %s AND run_id = %s AND stage = %s AND state = 'queued'"
    " RETURNING operation_id, state, operation_key, params_digest, run_id, stage, kind"
)
_SET_RUN_OUTCOME = (
    "UPDATE run SET state = %s, updated_at = now()"
    " WHERE run_id = %s AND state NOT IN ('succeeded', 'failed', 'cancelled')"
)
_SET_RUN_UNRESOLVED = (
    "UPDATE run SET state = 'unresolved', updated_at = now() WHERE run_id = %s AND state = 'running'"
)
_RESTORE_RUN_RUNNING = (
    "UPDATE run SET state = 'running', updated_at = now() WHERE run_id = %s AND state = 'unresolved'"
)
_SETTLE_OPERATIONS = (
    "UPDATE operation SET state = CASE WHEN kind = 'cancel' THEN 'succeeded' ELSE %s END,"
    " updated_at = now()"
    " WHERE run_id = %s AND stage = %s AND kind NOT IN ('reconcile', 'cache_release')"
    " AND state IN ('queued', 'running', 'unresolved')"
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
_SETTLE_CACHE_RELEASE = (
    "UPDATE operation SET state = %s, updated_at = now()"
    " WHERE operation_key = %s AND run_id = %s AND stage = %s AND kind = 'cache_release'"
    " AND state = 'running'"
    " RETURNING operation_id, state, operation_key, params_digest, run_id, stage, kind"
)
_READ_RUN_RECORD = (
    "SELECT state, request_id, parent_run_id, from_stage, tool_identity FROM run WHERE run_id = %s"
)
_READ_RUN_PARENT = "SELECT parent_run_id FROM run WHERE run_id = %s"
_READ_STAGE_RECORDS = (
    "SELECT stage, state, receipt, checkpoint_uri, reused_from_run, original_executed_at"
    " FROM stage WHERE run_id = %s"
)
_CONFIRMED_PUBLISH = (
    "SELECT 1 FROM operation WHERE run_id = %s AND kind = 'publish' AND state = 'succeeded' LIMIT 1"
)
_READ_STAGE_ROW = (
    "SELECT state, receipt, checkpoint_uri, reused_from_run FROM stage WHERE run_id = %s AND stage = %s"
)
_READ_STAGE = "SELECT state, receipt, checkpoint_uri, reused_from_run FROM stage WHERE run_id = %s AND stage = %s"
_READ_STAGE_TIME = "SELECT original_executed_at FROM stage WHERE run_id = %s AND stage = %s"
_LOCK_STAGE = "SELECT state FROM stage WHERE run_id = %s AND stage = %s FOR UPDATE"
_FIND_RECONCILE = (
    "SELECT 1 FROM operation"
    " WHERE operation_id = %s AND run_id = %s AND kind = 'reconcile' AND stage = %s LIMIT 1"
)
_FIND_UNKNOWN_ORIGIN = (
    "SELECT 1 FROM operation"
    " WHERE run_id = %s AND stage = %s AND kind <> 'reconcile' AND state = 'unresolved' LIMIT 1"
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
_READ_LIFECYCLE = (
    "SELECT object_kind, object_id, run_id, state, bucket, updated_at FROM lifecycle_projection"
)
_NEXT_EVENT_SEQ = "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_event WHERE run_id = %s"
_INSERT_EVENT = (
    "INSERT INTO run_event (run_id, seq, kind, stage, operation_id, payload)"
    " VALUES (%s, %s, %s, %s, %s, %s::jsonb)"
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
            return existing
        binding = self._rebind_predecessor(run_id=run_id, stage=stage, kind=kind)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_FIND_OPERATION, (operation_key,))
            row = cursor.fetchone()
            if row is not None:
                return _matching_operation(row, run_id=run_id, stage=stage, kind=kind, params_digest=params_digest)
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
                dispatch=dispatch or {},
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
                raise PlatformStoreError(
                    f"A cache release requires the confirmed {stage!r} stage of run {run_id!r}"
                )
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
        self._verify_checkpoint_scope(
            checkpoint_uri, run_id=run_id, stage=stage, reused_from_run=reused_from_run
        )
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
            if cursor.fetchone() is None:
                raise PlatformStoreError(f"The run {run_id!r} is not admitted")
            cursor.execute(_LOCK_STAGE, (run_id, stage))
            row = cursor.fetchone()
            current = row[0] if row else None
            if current is not None:
                if current in TERMINAL_STAGE_STATES:
                    raise PlatformStoreError(
                        f"The {stage!r} stage of run {run_id!r} is already terminal; start a linked run instead"
                    )
                if current == "unresolved" and reconcile is None:
                    raise PlatformStoreError(
                        "The stage is unresolved; confirm it through an evidenced reconciliation"
                    )
                if current != "unresolved" and reconcile is not None:
                    raise PlatformStoreError("Reconciliation only confirms an unresolved stage")
            elif reconcile is not None:
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
                    raise PlatformStoreError(
                        "The reconciliation binds no unresolved original operation of this stage"
                    )
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
        is left for maintenance.  An unresolved outcome freezes only the
        claimed operation as unknown and leaves the run in place until the
        evidenced reconciliation, which then settles the rest.
        """

        run_outcome: str | None = None
        if state == "completed":
            outcome = "succeeded"
            if stage == "publish":
                run_outcome = "succeeded"
        elif state in {"failed", "cancelled"}:
            outcome = state
            run_outcome = state
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

    def _verify_checkpoint_scope(
        self, checkpoint_uri, *, run_id: str, stage: str, reused_from_run: str | None
    ) -> None:
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

    def _verify_reuse_source(
        self, *, run_id: str, stage: str, checkpoint_uri, reused_from_run: str
    ) -> dict:
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
        indexed = {path[len("input/"):] for path in stamped if path.startswith("input/")}
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
            natives = {path[len("input/cad/"):] for path in stamped if path.startswith("input/cad/")}
            if set(inventory) != natives:
                raise PlatformStoreError(
                    "The native input inventory does not match the sealed native originals: "
                    f"missing={sorted(natives - set(inventory))} extra={sorted(set(inventory) - natives)}"
                )
            for name, checksum in inventory.items():
                record = stamped.get("input/cad/" + str(name))
                if record is None or record[1] != checksum:
                    raise PlatformStoreError(
                        "The native input inventory is not byte-bound to the sealed originals"
                    )
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

    return isinstance(entry, str) and "/" in entry and entry.isascii() and not entry.startswith("/")


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
                record.get("bytes") == report_path.stat().st_size
                and record.get("sha256") == file_digest(report_path)
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


def _require_successor(
    self, connection, *, run_id: str, kind: str, stage: str, binding: dict | None = None
) -> None:
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
