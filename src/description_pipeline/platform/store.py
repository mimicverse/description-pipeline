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
import os
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..delivery import BUNDLE_SCHEMA, read_delivery_manifest
from ..io import PipelineError, digest
from ..stages import (
    CONFIRMED_STAGE_PASSED,
    CONFIRMED_STAGE_STATES,
    STAGE_IDS,
    STAGE_STATES,
    TERMINAL_STAGE_STATES,
)


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
_LOCK_RUN = "SELECT state, from_stage FROM platform.run WHERE run_id = %s FOR UPDATE"
_READ_RUN = "SELECT state, from_stage FROM platform.run WHERE run_id = %s"
_CONFIRMED_PUBLISH = (
    "SELECT 1 FROM platform.operation WHERE run_id = %s AND kind = 'publish' AND state = 'succeeded' LIMIT 1"
)
_READ_STAGE_ROW = "SELECT state, receipt, checkpoint_uri FROM platform.stage WHERE run_id = %s AND stage = %s"
_READ_STAGE = "SELECT state, receipt, checkpoint_uri FROM platform.stage WHERE run_id = %s AND stage = %s"
_LOCK_STAGE = "SELECT state FROM platform.stage WHERE run_id = %s AND stage = %s FOR UPDATE"
_FIND_RECONCILE = (
    "SELECT 1 FROM platform.operation"
    " WHERE operation_id = %s AND run_id = %s AND kind = 'reconcile' AND stage = %s LIMIT 1"
)
_FIND_UNKNOWN_ORIGIN = (
    "SELECT 1 FROM platform.operation"
    " WHERE run_id = %s AND stage = %s AND kind <> 'reconcile' AND state = 'unresolved' LIMIT 1"
)
_INSERT_STAGE = (
    "INSERT INTO platform.stage (run_id, stage, state, receipt, checkpoint_uri, reused_from_run, original_executed_at)"
    " VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)"
    " ON CONFLICT (run_id, stage) DO UPDATE SET"
    " state = EXCLUDED.state, receipt = EXCLUDED.receipt, checkpoint_uri = EXCLUDED.checkpoint_uri,"
    " reused_from_run = EXCLUDED.reused_from_run, original_executed_at = EXCLUDED.original_executed_at,"
    " updated_at = now()"
    " WHERE platform.stage.state NOT IN ('completed','failed','cancelled','blocked','unresolved')"
    " RETURNING run_id, stage, state"
)
_RECONCILE_STAGE = (
    "INSERT INTO platform.stage (run_id, stage, state, receipt, checkpoint_uri, reused_from_run, original_executed_at)"
    " VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)"
    " ON CONFLICT (run_id, stage) DO UPDATE SET"
    " state = EXCLUDED.state, receipt = EXCLUDED.receipt, checkpoint_uri = EXCLUDED.checkpoint_uri,"
    " reused_from_run = EXCLUDED.reused_from_run, original_executed_at = EXCLUDED.original_executed_at,"
    " updated_at = now()"
    " WHERE platform.stage.state = 'unresolved'"
    " RETURNING run_id, stage, state"
)
_READ_LIFECYCLE = (
    "SELECT object_kind, object_id, run_id, state, bucket, updated_at FROM platform.lifecycle_projection"
)
_NEXT_EVENT_SEQ = "SELECT COALESCE(MAX(seq), 0) + 1 FROM platform.run_event WHERE run_id = %s"
_INSERT_EVENT = (
    "INSERT INTO platform.run_event (run_id, seq, kind, stage, operation_id, payload)"
    " VALUES (%s, %s, %s, %s, %s, %s::jsonb)"
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
        refusal - never a silent fallback to another store.
        """

        source = os.environ if environ is None else environ
        dsn = source.get("PLATFORM_SERVICE_DSN")
        if not dsn:
            raise PlatformStoreError("PLATFORM_SERVICE_DSN is not configured; the platform store is unavailable")

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

        A downstream stage's registered predecessor is rebound from its
        sealed root *before* the registration transaction opens; the locked
        transaction itself only re-checks that same predecessor identity,
        reference and state before the insert.
        """

        binding = self._rebind_predecessor(run_id=run_id, stage=stage, kind=kind)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(_FIND_OPERATION, (operation_key,))
            row = cursor.fetchone()
            if row is not None:
                return _matching_operation(row, run_id=run_id, stage=stage, kind=kind, params_digest=params_digest)
            if stage not in STAGE_IDS:
                raise PlatformStoreError(f"The stage must be one of {tuple(STAGE_IDS)}: {stage!r}")
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

    def _rebind_predecessor(self, *, run_id: str, stage: str, kind: str) -> dict | None:
        """Rebind the successor's registered predecessor, outside any lock.

        The frozen boundary keeps the sealed-byte work - resolver, delivery
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
        if predecessor in CONFIRMED_STAGE_STATES:
            definition = self._verify_sealed(predecessor_row[2], predecessor_row[1])
        _sealed_receipt(
            predecessor_row[1],
            run_id=run_id,
            stage=predecessor,
            state="completed",
            checkpoint_uri=predecessor_row[2],
            definition=definition,
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

        This is the store path that writes ``platform.stage`` rows on the
        platform side.  A sealed ``completed`` row is immutable - a later
        confirmation for the same (run, stage) is an explicit refusal instead
        of a rewrite (a redo is a linked run, not an overwrite).
        ``unresolved`` is not terminal: it is only confirmed through
        ``reconcile`` evidence of the same operation, which records the actual
        outcome - never a redo of an unknown side effect.
        """

        if state not in STAGE_STATES:
            raise PlatformStoreError(f"The stage state must be one of {tuple(sorted(STAGE_STATES))}: {state!r}")
        if reconcile is not None and (not isinstance(reconcile, dict) or not reconcile):
            raise PlatformStoreError("Reconciliation evidence must be a non-empty record")
        if reconcile is not None and state not in {"completed", "failed", "cancelled"}:
            raise PlatformStoreError("A reconciliation confirms a definite outcome; unresolved is not an outcome")
        definition = None
        if state == "completed" and stage in CONFIRMED_STAGE_STATES:
            definition = self._verify_sealed(checkpoint_uri, receipt)
        _sealed_receipt(
            receipt,
            run_id=run_id,
            stage=stage,
            state=state,
            checkpoint_uri=checkpoint_uri,
            definition=definition,
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

    def _insert_event(self, connection, *, run_id: str, stage: str, kind: str, operation_id, payload) -> None:
        """Append one run event in the same transaction as the state change it describes."""

        cursor = connection.cursor()
        cursor.execute(_NEXT_EVENT_SEQ, (run_id,))
        row = cursor.fetchone()
        sequence = row[0] if row else 1
        cursor.execute(_INSERT_EVENT, (run_id, sequence, kind, stage, operation_id, _json(payload)))

    def _verify_sealed(self, checkpoint_uri, receipt) -> dict:
        """Rebind the real sealed delivery before any registration.

        The receipt's URI and inventory are self-reported; the bytes are not.
        The resolver (deploy-side sealed storage) locates the sealed delivery
        root, ``read_delivery_manifest`` re-binds every subject record and the
        subject digest, the receipt's artifact inventory must carry the same
        file digests, and the definition the quality inventory binds to is
        rebuilt from that very sealed tree (never from platform records) - so
        a syntactic URI or an invented inventory can never mint a completion.
        """

        if self._sealed is None:
            raise PlatformStoreError("Sealed storage is not configured; a completion cannot be registered")
        root = Path(self._sealed(checkpoint_uri))
        manifest = read_delivery_manifest(root)
        subject = manifest.get("subject") or {}
        if subject.get("digest") != receipt.get("subject_sha256"):
            raise PlatformStoreError("The sealed delivery subject does not match the receipt")
        artifacts = receipt.get("artifacts") or {}
        for record in subject.get("files") or []:
            name = record.get("path") if isinstance(record, dict) else None
            if name is None or artifacts.get(name) != record.get("sha256"):
                raise PlatformStoreError("The receipt's artifact inventory does not bind the sealed delivery")
        from ..verification.solidworks_urdf import rebuilt_definition

        definition = rebuilt_definition(root)
        if not isinstance(definition, dict) or not isinstance(definition.get("source"), dict):
            raise PlatformStoreError("The sealed delivery does not rebuild a definition inventory")
        return definition

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
        definition = self._verify_sealed(row[2], receipt)
        _sealed_receipt(
            receipt,
            run_id=run_id,
            stage=stage,
            state="completed",
            checkpoint_uri=row[2],
            definition=definition,
        )
        return receipt

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


def _is_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _sealed_receipt(receipt, *, run_id: str, stage: str, state: str, checkpoint_uri, definition) -> None:
    """The one sealed-receipt validator shared by the stage write and reuse paths.

    A terminal confirmation must bind its receipt completely, and the reuse
    query runs exactly the same validation - downstream can never trust the
    state string alone.  ``blocked`` records an unexecuted stage and carries
    no receipt requirements; native stages validate the bindings they carry.
    """

    if state == "blocked":
        return
    if state in {"pending", "running"}:
        return
    if not isinstance(receipt, dict):
        raise PlatformStoreError("A sealed stage confirmation must carry its bound receipt")
    if receipt.get("schema_version") != BUNDLE_SCHEMA:
        raise PlatformStoreError("The sealed receipt uses an unknown schema")
    if receipt.get("run_id") != run_id or receipt.get("stage") != stage:
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
    if not isinstance(handoff, str) or not handoff:
        raise PlatformStoreError("The sealed receipt lacks its handoff binding")
    expected = CONFIRMED_STAGE_STATES.get(stage)
    if expected is None:
        return
    if receipt.get("state") not in expected:
        raise PlatformStoreError("A completed stage must be sealed with its confirmed receipt state")
    if receipt.get("passed") is not CONFIRMED_STAGE_PASSED[stage]:
        raise PlatformStoreError("The sealed receipt does not carry the stage's confirmed outcome")
    if not isinstance(checkpoint_uri, str) or not checkpoint_uri:
        raise PlatformStoreError("An unsealed stage completion is refused: no checkpoint reference")
    if not isinstance(receipt.get("artifacts"), dict) or not receipt["artifacts"]:
        raise PlatformStoreError("The sealed receipt lacks its artifact inventory")
    subject = receipt.get("subject_sha256")
    if not _is_sha256(subject):
        raise PlatformStoreError("The sealed receipt lacks its subject digest")
    if stage in {"verify", "publish"}:
        from ..verification.solidworks_urdf import require_qualified_report

        try:
            quality = require_qualified_report(receipt.get("quality"), definition)
        except PipelineError as error:
            raise PlatformStoreError(str(error)) from error
        if quality.get("subject_sha256") != subject:
            raise PlatformStoreError("The sealed receipt's quality payload is bound to another subject")
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
