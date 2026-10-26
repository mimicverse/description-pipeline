"""PostgreSQL persistence for the canonical six-stage facts.

Every public mutation is one short transaction. Artifact bytes and external
effects are produced outside these methods; this module only commits the
already verified identities and observations.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from ..io import PipelineError, digest
from ..pipeline.admission import (
    AdmissionSpec,
    VerifiedDiscoverResult,
    VerifiedPredecessor,
    VerifiedPublishIntent,
    VerifiedVerifyResult,
    validate_run_admission,
    validate_store_command,
)
from ..pipeline.diagnostic import (
    DiagnosticAdmissionSpec,
    build_diagnostic_run_admission,
    validate_diagnostic_command,
)
from ..pipeline.contracts import (
    AdmissionControl,
    ArtifactRef,
    CancellationRequest,
    DispatchCommand,
    DispatchConf,
    ExecutionClaim,
    FailureEnvelope,
    ProtocolError,
    RunAdmission,
    RunStatus,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageEffectWait,
    StagePending,
    StageQualification,
    StageResult,
    STAGES,
    StageStatus,
    project_run_status,
    project_stage_status,
    qualify_stage,
    require_formal_qualification,
)
from ..pipeline.effects import (
    EffectFact,
    EffectIntent,
    EffectResumePhase,
    EffectSettlement,
    EffectState,
    project_effect_settlement,
    project_effect_state,
    native_transfer_target_digest,
    require_native_transfer_target,
)
from ..schemas import (
    AIRFLOW_DISPATCH_EFFECT_SCHEMA,
    CANCEL_REQUEST_SCHEMA,
    DIAGNOSTIC_ADMISSION_SCHEMA,
    DIAGNOSTIC_COMMAND_REQUEST_SCHEMA,
    RUN_ADMISSION_SCHEMA,
    RUN_ACTIVITY_SCHEMA,
    STAGE_EFFECT_WAIT_SCHEMA,
    STAGE_COMMAND_SCHEMA,
)
from ..stages import ACTIVITY_HISTORY_LIMIT, merge_activity, normalize_activity

# Transaction locks are keyed by (namespace, hashtext(identity)). A hash collision can
# only serialize unrelated operations, never permit two owners. Callers acquire command
# locks before effect-ledger locks and effect-ledger locks before individual effect locks;
# mutable execution-claim rows are locked only after those immutable scopes.
_ADVISORY_RUN = 1
_ADVISORY_COMMAND = 2
_ADVISORY_EFFECT_LEDGER = 3
_ADVISORY_EFFECT = 4


def _encoded_run_activity(activity: Mapping[str, object]) -> str:
    return json.dumps(
        activity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class FactStoreError(PipelineError):
    """A canonical platform fact cannot be read or committed safely."""


class FactConflict(FactStoreError):
    """A canonical key is already bound to a different immutable value."""


class RunNotAdmittedError(FactStoreError):
    """A display-only mutation names no canonical run admission."""


class StageResultReader(Protocol):
    """Read and independently validate one sealed StageResult artifact."""

    def verify(self, artifact: ArtifactRef) -> Mapping[str, object]: ...

    def read_result(self, artifact: ArtifactRef) -> StageResult: ...

    def read_dispatch_conf(self, artifact: ArtifactRef) -> DispatchConf: ...


@dataclass(frozen=True, slots=True)
class RunDisplayMetadata:
    """The display-only name and reversible tombstone of one admitted run."""

    run_id: str
    display_name: str | None
    hidden_at: datetime | None
    hidden_reason: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AdmittedRunSummary:
    """One admitted run identity with the canonical run and Airflow projection state."""

    run_id: str
    request_id: str
    parent_run_id: str | None
    from_stage: str | None
    state: str
    created_at: datetime
    updated_at: datetime


def _run_state_from_canonical_facts(
    *,
    request: Mapping[str, object],
    stage_commands: tuple[str, ...],
    decisions: Mapping[str, str],
    effect_states: tuple[str, ...],
    pending_effect_states: tuple[str, ...],
) -> str:
    """Consume the common stage/run projectors; this layer only loads raw DB facts."""

    if request.get("schema_version") != DIAGNOSTIC_ADMISSION_SCHEMA:
        outcomes = tuple(decisions.get(stage) for stage in STAGES)
        return project_run_status(
            stage_outcomes=outcomes,
            effect_states=effect_states,
            pending_effect_states=pending_effect_states,
        ).value

    diagnostic = DiagnosticAdmissionSpec.from_record(request)
    if (
        len(stage_commands) > 1
        or any(stage != diagnostic.target_stage for stage in stage_commands)
        or any(stage != diagnostic.target_stage for stage in decisions)
    ):
        raise FactStoreError("A diagnostic run does not contain exactly its unique target stage")
    status = project_stage_status(
        decision_outcome=decisions.get(diagnostic.target_stage),
        effect_states=effect_states,
        predecessor_completed=True,
        open_claim=False,
        pending_effect_states=pending_effect_states,
    )
    if status in {StageStatus.WAITING, StageStatus.ACTIVE, StageStatus.BLOCKED}:
        return RunStatus.INCOMPLETE.value
    return {
        StageStatus.COMPLETED: RunStatus.SUCCEEDED,
        StageStatus.FAILED: RunStatus.FAILED,
        StageStatus.CANCELLED: RunStatus.CANCELLED,
        StageStatus.UNRESOLVED: RunStatus.UNRESOLVED,
    }[status].value


def read_admitted_runs(
    connection,
    *,
    run_id: str | None = None,
) -> list[AdmittedRunSummary]:
    """Project run state from canonical facts with the shared Python projectors."""

    cursor = connection.cursor()
    try:
        if run_id is None:
            cursor.execute(
                """
                SELECT run_id, request_id, parent_run_id, from_stage, request, created_at
                FROM run_admission
                ORDER BY created_at DESC, run_id
                """
            )
        else:
            cursor.execute(
                """
                SELECT run_id, request_id, parent_run_id, from_stage, request, created_at
                FROM run_admission
                WHERE run_id = %s
                """,
                (run_id,),
            )
        admission_rows = cursor.fetchall()
        if not admission_rows:
            return []
        run_ids = [row[0] for row in admission_rows]

        cursor.execute(
            """
            SELECT command_id, run_id, kind, stage, created_at
            FROM command
            WHERE run_id = ANY(%s::text[])
            ORDER BY run_id, command_id
            """,
            (run_ids,),
        )
        commands: dict[str, dict[str, object]] = {}
        stages_by_run: dict[str, list[str]] = {run: [] for run in run_ids}
        updated_at = {
            row[0]: row[5]
            for row in admission_rows
        }
        for command_id, command_run, kind, stage, created_at in cursor.fetchall():
            if command_run not in stages_by_run:
                raise FactStoreError("A command names an unselected run")
            updated_at[command_run] = max(updated_at[command_run], created_at)
            if kind != "stage":
                continue
            if not isinstance(stage, str):
                raise FactStoreError("A stage command carries an invalid canonical stage")
            commands[command_id] = {
                "run_id": command_run,
                "stage": stage,
            }
            stages_by_run[command_run].append(stage)

        cursor.execute(
            """
            SELECT run_id, stage, command_id, outcome, decided_at
            FROM stage_decision
            WHERE run_id = ANY(%s::text[])
            ORDER BY run_id, stage
            """,
            (run_ids,),
        )
        decisions: dict[str, dict[str, str]] = {run: {} for run in run_ids}
        for decision_run, stage, command_id, outcome, decided_at in cursor.fetchall():
            command = commands.get(command_id)
            if (
                command is None
                or command["run_id"] != decision_run
                or command["stage"] != stage
            ):
                raise FactStoreError("A stage decision differs from its canonical stage command")
            decisions[decision_run][stage] = outcome
            updated_at[decision_run] = max(updated_at[decision_run], decided_at)

        cursor.execute(
            """
            SELECT effect.effect_id, effect.command_id, effect.effect_key,
                   effect.effect_kind, effect.target, effect.expected_state,
                   effect.expected_state_digest, effect.outbound_artifact_id,
                   effect.outbound_digest, effect.created_at, command.run_id,
                   fact.fact_id, fact.seq, fact.claim_id, fact.fence,
                   fact.fact_type, fact.source, fact.state_class,
                   fact.payload_artifact_id, fact.payload_digest,
                   fact.metadata_artifact_id, fact.observed_at, fact.resume_phase
            FROM effect_intent AS effect
            JOIN command ON command.command_id = effect.command_id
            LEFT JOIN effect_fact AS fact ON fact.effect_id = effect.effect_id
            WHERE command.run_id = ANY(%s::text[])
            ORDER BY effect.effect_id, fact.seq
            """,
            (run_ids,),
        )
        effects: dict[str, tuple[str, EffectIntent, list[EffectFact]]] = {}
        for row in cursor.fetchall():
            effect_run = row[10]
            record = effects.get(row[0])
            if record is None:
                intent = EffectIntent(
                    effect_id=row[0],
                    command_id=row[1],
                    effect_key=row[2],
                    effect_kind=row[3],
                    target=row[4],
                    expected_state=row[5],
                    expected_state_digest=row[6],
                    outbound_artifact_id=row[7],
                    outbound_digest=row[8],
                    created_at=row[9],
                )
                facts: list[EffectFact] = []
                effects[row[0]] = (effect_run, intent, facts)
                updated_at[effect_run] = max(updated_at[effect_run], row[9])
            elif record[0] != effect_run or record[1].command_id != row[1]:
                raise FactStoreError("An effect intent is bound to inconsistent commands")
            if row[11] is not None:
                fact = EffectFact(
                    fact_id=row[11],
                    effect_id=row[0],
                    seq=row[12],
                    claim_id=row[13],
                    fence=row[14],
                    fact_type=row[15],
                    source=row[16],
                    state_class=row[17],
                    payload_artifact_id=row[18],
                    payload_digest=row[19],
                    observed_at=row[21],
                    metadata_artifact_id=row[20],
                    resume_phase=(
                        EffectResumePhase(row[22]) if row[22] is not None else None
                    ),
                )
                effects[row[0]][2].append(fact)
                updated_at[effect_run] = max(updated_at[effect_run], row[21])

        effect_states: dict[str, dict[str, EffectState]] = {run: {} for run in run_ids}
        for effect_run, intent, facts in effects.values():
            effect_states[effect_run][intent.effect_id] = project_effect_state(
                intent,
                tuple(facts),
            )

        cursor.execute(
            """
            SELECT DISTINCT ON (claim.command_id)
                   command.run_id, claim.yield_effect_id
            FROM execution_claim AS claim
            JOIN command ON command.command_id = claim.command_id
            WHERE command.run_id = ANY(%s::text[])
              AND claim.closure_code = 'yielded'
              AND claim.yield_effect_id IS NOT NULL
            ORDER BY claim.command_id, claim.fence DESC
            """,
            (run_ids,),
        )
        yielded_effect_ids: dict[str, set[str]] = {run: set() for run in run_ids}
        for claim_run, effect_id in cursor.fetchall():
            if not isinstance(effect_id, str):
                continue
            state = effect_states[claim_run].get(effect_id)
            if state is EffectState.UNKNOWN:
                yielded_effect_ids[claim_run].add(effect_id)

        cursor.execute(
            """
            SELECT command.run_id, max(claim.last_seen_at)
            FROM command
            JOIN execution_claim AS claim ON claim.command_id = command.command_id
            WHERE command.run_id = ANY(%s::text[])
            GROUP BY command.run_id
            """,
            (run_ids,),
        )
        for claim_run, last_seen_at in cursor.fetchall():
            updated_at[claim_run] = max(updated_at[claim_run], last_seen_at)
    finally:
        cursor.close()

    summaries = [
        AdmittedRunSummary(
            run_id=admission_run,
            request_id=request_id,
            parent_run_id=parent_run_id,
            from_stage=from_stage,
            state=_run_state_from_canonical_facts(
                request=request,
                stage_commands=tuple(stages_by_run[admission_run]),
                decisions=decisions[admission_run],
                effect_states=tuple(
                    state.value for state in effect_states[admission_run].values()
                ),
                pending_effect_states=tuple(
                    state.value
                    for effect_id, state in effect_states[admission_run].items()
                    if effect_id in yielded_effect_ids[admission_run]
                    and state is EffectState.UNKNOWN
                ),
            ),
            created_at=created_at,
            updated_at=updated_at[admission_run],
        )
        for (
            admission_run,
            request_id,
            parent_run_id,
            from_stage,
            request,
            created_at,
        ) in admission_rows
    ]
    return summaries


def _stored_command_request(command: StageCommand) -> Mapping[str, object]:
    if command.diagnostic_scope is None:
        return command.request
    return {
        "schema_version": DIAGNOSTIC_COMMAND_REQUEST_SCHEMA,
        "command": command.to_record(),
    }


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _json(value) -> str:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _require_open(cursor) -> None:
    cursor.execute("SELECT state FROM admission WHERE id = 1 FOR UPDATE")
    row = cursor.fetchone()
    if row is None:
        raise FactStoreError("The platform admission row is missing")
    if row[0] != "open":
        raise FactStoreError("The platform admission gate is closed")


def _artifact_from_row(row) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=row[0],
        kind=row[1],
        locator=row[2],
        manifest_sha256=row[3],
        set_sha256=row[4],
        producer_run_id=row[5],
        producer_stage=row[6],
        producer_tool_sha256=row[7],
    )


class AdmissionControlStore:
    """The single administrative admission gate with no business-state authority."""

    def __init__(self, connect) -> None:
        if not callable(connect):
            raise FactStoreError("A database connection factory is required")
        self._connect = connect

    @classmethod
    def from_environment(cls, environ):
        dsn = environ.get("PLATFORM_SERVICE_DSN")
        if not dsn:
            raise FactStoreError("PLATFORM_SERVICE_DSN is not configured")

        def connect():
            import psycopg2

            return psycopg2.connect(dsn)

        return cls(connect)

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def admission(self) -> AdmissionControl:
        """Read the single administrative admission gate."""

        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT state, reason, actor, changed_at, revision FROM admission WHERE id = 1")
            row = cursor.fetchone()
        if row is None:
            raise FactStoreError("The platform admission projection is empty")
        return AdmissionControl(*row)

    def close_admission(self, *, reason: str, actor: str) -> AdmissionControl:
        return self._set_admission("closed", reason=reason, actor=actor)

    def open_admission(self, *, reason: str, actor: str) -> AdmissionControl:
        return self._set_admission("open", reason=reason, actor=actor)

    def _set_admission(self, state: str, *, reason: str, actor: str) -> AdmissionControl:
        for name, value in (("reason", reason), ("actor", actor)):
            if not isinstance(value, str) or not value or "\x00" in value:
                raise FactStoreError(f"The admission {name} is invalid")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT state, reason, actor, changed_at, revision "
                "FROM admission WHERE id = 1 FOR UPDATE"
            )
            row = cursor.fetchone()
            if row is None:
                raise FactStoreError("The platform admission row is missing")
            current = AdmissionControl(*row)
            if current.state == state:
                return current
            cursor.execute(
                "UPDATE admission SET state = %s, reason = %s, actor = %s,"
                " changed_at = now(), revision = revision + 1"
                " WHERE id = 1"
                " RETURNING state, reason, actor, changed_at, revision",
                (state, reason, actor),
            )
            updated = cursor.fetchone()
        if updated is None:
            raise FactStoreError("The platform admission row vanished during close/open")
        return AdmissionControl(*updated)


class RunControlStore(AdmissionControlStore):
    """Read run identities and manage display-only metadata without old lifecycle rows."""

    def list_run_display_metadata(self) -> dict[str, RunDisplayMetadata]:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT run_id, display_name, hidden_at, hidden_reason, updated_at
                FROM run_display_metadata
                ORDER BY run_id
                """
            )
            rows = cursor.fetchall()
        return {row[0]: RunDisplayMetadata(*row) for row in rows}

    def get_run_display_metadata(self, run_id: str) -> RunDisplayMetadata | None:
        self._require_run_id(run_id)
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT run_id, display_name, hidden_at, hidden_reason, updated_at
                FROM run_display_metadata
                WHERE run_id = %s
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        return RunDisplayMetadata(*row) if row is not None else None

    def record_run_activity(
        self,
        run_id: str,
        operation_id: str,
        activity: Mapping[str, object] | None,
        *,
        terminal: bool = False,
    ) -> None:
        """Merge one display-only native activity observation into its run."""

        self._require_run_id(run_id)
        if not isinstance(activity, Mapping):
            return
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or len(operation_id) > 128
            or "\x00" in operation_id
        ):
            raise FactStoreError("The native operation identity is invalid")
        record = dict(activity)
        sequence = record.get("seq")
        if type(sequence) is not int or sequence <= 0:
            raise FactStoreError("The run activity sequence is invalid")
        observed_at = record.get("at")
        if not isinstance(observed_at, str) or not observed_at:
            raise FactStoreError("The run activity timestamp is invalid")
        try:
            parsed_at = datetime.fromisoformat(observed_at)
        except ValueError as error:
            raise FactStoreError("The run activity timestamp is invalid") from error
        if parsed_at.tzinfo is None:
            raise FactStoreError("The run activity timestamp must carry a timezone")
        normalized = normalize_activity(
            {
                key: value
                for key, value in record.items()
                if key not in {"seq", "at"}
            }
        )
        if normalized is None:
            raise FactStoreError("The run activity record is invalid")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT 1 FROM run_admission WHERE run_id = %s FOR SHARE",
                (run_id,),
            )
            if cursor.fetchone() is None:
                raise RunNotAdmittedError("The run is not admitted")
            cursor.execute(
                """
                INSERT INTO run_display_metadata (
                    run_id, display_name, hidden_at, hidden_reason, updated_at
                )
                SELECT run_id, NULL, NULL, NULL, now()
                FROM run_admission
                WHERE run_id = %s
                ON CONFLICT (run_id) DO NOTHING
                """,
                (run_id,),
            )
            cursor.execute(
                "SELECT activity FROM run_display_metadata "
                "WHERE run_id = %s FOR UPDATE",
                (run_id,),
            )
            row = cursor.fetchone()
            previous = row[0] if row is not None and isinstance(row[0], Mapping) else {}
            same_operation = (
                previous.get("schema_version") == RUN_ACTIVITY_SCHEMA
                and previous.get("operation_id") == operation_id
            )
            previous_source_seq = previous.get("source_seq")
            previous_current = (
                previous.get("current") if isinstance(previous.get("current"), dict) else None
            )
            if (
                same_operation
                and type(previous_source_seq) is int
                and sequence <= previous_source_seq
            ):
                if (
                    not terminal
                    or isinstance(previous.get("final"), dict)
                    or previous_current is None
                ):
                    return
                projection = copy.deepcopy(previous)
                projection["final"] = copy.deepcopy(previous_current)
                cursor.execute(
                    "UPDATE run_display_metadata SET activity = %s::jsonb "
                    "WHERE run_id = %s",
                    (_encoded_run_activity(projection), run_id),
                )
                return
            if not same_operation and previous_current is not None:
                previous_at = previous_current.get("updated_at")
                try:
                    previous_observed_at = datetime.fromisoformat(previous_at)
                except (TypeError, ValueError):
                    previous_observed_at = None
                if (
                    previous_observed_at is not None
                    and previous_observed_at.tzinfo is not None
                    and parsed_at < previous_observed_at
                ):
                    return
            current, transition = merge_activity(
                previous_current if same_operation else None,
                normalized,
                at=observed_at,
            )
            previous_history = previous.get("history")
            history = (
                [
                    copy.deepcopy(item)
                    for item in previous_history
                    if isinstance(item, dict)
                    and isinstance(item.get("at"), str)
                    and isinstance(item.get("code"), str)
                ][-ACTIVITY_HISTORY_LIMIT:]
                if isinstance(previous_history, list)
                else []
            )
            if transition is not None:
                history = [*history, transition][-ACTIVITY_HISTORY_LIMIT:]
            projection = {
                "schema_version": RUN_ACTIVITY_SCHEMA,
                "operation_id": operation_id,
                "source_seq": sequence,
                "current": current,
                "history": history,
                "final": copy.deepcopy(current) if terminal else None,
            }
            cursor.execute(
                "UPDATE run_display_metadata SET activity = %s::jsonb "
                "WHERE run_id = %s",
                (_encoded_run_activity(projection), run_id),
            )

    def rename_run(self, run_id: str, display_name: str | None) -> RunDisplayMetadata:
        self._require_run_id(run_id)
        if display_name is not None and (not isinstance(display_name, str) or "\x00" in display_name):
            raise FactStoreError("The run display name is invalid")
        return self._update_run_display_metadata(
            """
            INSERT INTO run_display_metadata (
                run_id, display_name, hidden_at, hidden_reason, updated_at
            )
            SELECT run_id, %s, NULL, NULL, now()
            FROM run_admission
            WHERE run_id = %s
            ON CONFLICT (run_id) DO UPDATE
            SET display_name = EXCLUDED.display_name, updated_at = now()
            RETURNING run_id, display_name, hidden_at, hidden_reason, updated_at
            """,
            (display_name, run_id),
        )

    def hide_run(self, run_id: str, *, actor: str) -> RunDisplayMetadata:
        self._require_run_id(run_id)
        if not isinstance(actor, str) or not actor or "\x00" in actor:
            raise FactStoreError("The run hide actor is invalid")
        return self._update_run_display_metadata(
            """
            INSERT INTO run_display_metadata (
                run_id, display_name, hidden_at, hidden_reason, updated_at
            )
            SELECT run_id, NULL, now(), %s, now()
            FROM run_admission
            WHERE run_id = %s
            ON CONFLICT (run_id) DO UPDATE
            SET hidden_at = COALESCE(run_display_metadata.hidden_at, now()),
                hidden_reason = COALESCE(run_display_metadata.hidden_reason, EXCLUDED.hidden_reason),
                updated_at = now()
            RETURNING run_id, display_name, hidden_at, hidden_reason, updated_at
            """,
            (actor, run_id),
        )

    def restore_run(self, run_id: str) -> RunDisplayMetadata:
        self._require_run_id(run_id)
        return self._update_run_display_metadata(
            """
            INSERT INTO run_display_metadata (
                run_id, display_name, hidden_at, hidden_reason, updated_at
            )
            SELECT run_id, NULL, NULL, NULL, now()
            FROM run_admission
            WHERE run_id = %s
            ON CONFLICT (run_id) DO UPDATE
            SET hidden_at = NULL, hidden_reason = NULL, updated_at = now()
            RETURNING run_id, display_name, hidden_at, hidden_reason, updated_at
            """,
            (run_id,),
        )

    def list_admitted_runs(self) -> list[AdmittedRunSummary]:
        with self._transaction() as connection:
            return read_admitted_runs(connection)

    def get_admitted_run(self, run_id: str) -> AdmittedRunSummary | None:
        self._require_run_id(run_id)
        with self._transaction() as connection:
            rows = read_admitted_runs(connection, run_id=run_id)
        return rows[0] if rows else None

    @staticmethod
    def _require_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id or "\x00" in run_id:
            raise FactStoreError("The run id is invalid")

    def _update_run_display_metadata(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> RunDisplayMetadata:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(statement, parameters)
            row = cursor.fetchone()
        if row is None:
            raise RunNotAdmittedError("The run is not admitted")
        return RunDisplayMetadata(*row)


class FactStore(RunControlStore):
    """Canonical fact persistence with injected connection and result reader."""

    def __init__(self, connect, result_reader: StageResultReader) -> None:
        super().__init__(connect)
        if result_reader is None or not callable(getattr(result_reader, "read_result", None)):
            raise FactStoreError("A StageResult artifact reader is required")
        self._result_reader = result_reader

    @classmethod
    def from_environment(cls, environ, *, result_reader: StageResultReader) -> FactStore:
        dsn = environ.get("PLATFORM_SERVICE_DSN")
        if not dsn:
            raise FactStoreError("PLATFORM_SERVICE_DSN is not configured")

        def connect():
            import psycopg2

            return psycopg2.connect(dsn)

        return cls(connect, result_reader)

    @staticmethod
    def _lock_advisory_cursor(cursor, namespace: int, identity: str) -> None:
        """Take one transaction-scoped lock without requiring UPDATE on a fact table."""

        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
            (namespace, identity),
        )

    def _lock_run_cursor(self, cursor, run_id: str) -> None:
        self._lock_advisory_cursor(cursor, _ADVISORY_RUN, run_id)

    def _lock_command_cursor(self, cursor, command_id: str) -> None:
        self._lock_advisory_cursor(cursor, _ADVISORY_COMMAND, command_id)

    def _lock_command_chain_cursor(self, cursor, command: StageCommand) -> None:
        self._lock_run_cursor(cursor, command.run_id)
        predecessors = self._completed_predecessor_chain_cursor(cursor, command)
        for predecessor in reversed(predecessors):
            self._lock_command_cursor(cursor, predecessor.command_id)
        self._lock_command_cursor(cursor, command.command_id)

    def _lock_command_scope_cursor(self, cursor, command_id: str):
        cursor.execute(
            "SELECT run_id, kind FROM command WHERE command_id = %s",
            (command_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise FactStoreError(f"The command {command_id!r} is not registered")
        self._lock_run_cursor(cursor, row[0])
        if row[1] == "stage":
            command = self._load_command_cursor(cursor, command_id)
            if command is None:
                raise FactStoreError(f"The command {command_id!r} disappeared")
            self._lock_command_chain_cursor(cursor, command)
        else:
            self._lock_command_cursor(cursor, command_id)
        return row

    def _lock_effect_ledger_cursor(self, cursor, command_id: str) -> None:
        self._lock_advisory_cursor(cursor, _ADVISORY_EFFECT_LEDGER, command_id)

    def _lock_effect_cursor(self, cursor, effect_id: str) -> None:
        self._lock_advisory_cursor(cursor, _ADVISORY_EFFECT, effect_id)

    def _insert_artifact(self, cursor, artifact: ArtifactRef) -> ArtifactRef:
        cursor.execute(
            """
            INSERT INTO artifact (
                artifact_id, kind, locator, manifest_sha256, set_sha256,
                producer_run_id, producer_stage, producer_tool_sha256
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING artifact_id, kind, locator, manifest_sha256, set_sha256,
                      producer_run_id, producer_stage, producer_tool_sha256
            """,
            (
                artifact.artifact_id,
                artifact.kind,
                artifact.locator,
                artifact.manifest_sha256,
                artifact.set_sha256,
                artifact.producer_run_id,
                artifact.producer_stage,
                artifact.producer_tool_sha256,
            ),
        )
        inserted = cursor.fetchone()
        if inserted is not None:
            return _artifact_from_row(inserted)
        cursor.execute(
            """
            SELECT artifact_id, kind, locator, manifest_sha256, set_sha256,
                   producer_run_id, producer_stage, producer_tool_sha256
            FROM artifact
            WHERE artifact_id = %s OR locator = %s
            """,
            (artifact.artifact_id, artifact.locator),
        )
        existing = cursor.fetchone()
        if existing is None:
            raise FactStoreError("The artifact registration conflicted without an existing row")
        stored = _artifact_from_row(existing)
        if stored != artifact:
            raise FactConflict(f"The artifact identity {artifact.artifact_id!r} is already bound to different bytes")
        return stored

    def _insert_artifacts_cursor(
        self,
        cursor,
        artifacts: tuple[ArtifactRef, ...],
    ) -> None:
        for artifact in sorted(
            artifacts,
            key=lambda item: (item.artifact_id, item.locator),
        ):
            self._insert_artifact(cursor, artifact)

    def register_artifact(self, artifact: ArtifactRef) -> ArtifactRef:
        if not isinstance(artifact, ArtifactRef):
            raise FactStoreError("A canonical ArtifactRef is required")
        with self._transaction() as connection:
            return self._insert_artifact(connection.cursor(), artifact)

    def _load_admission_cursor(self, cursor, run_id: str) -> RunAdmission | None:
        cursor.execute(
            """
            SELECT run_id, request_id, parent_run_id, from_stage, request, request_digest, created_at
            FROM run_admission
            WHERE run_id = %s
            """,
            (run_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._admission_from_row(row)

    @staticmethod
    def _admission_from_row(row) -> RunAdmission:
        return RunAdmission(
            schema_version=RUN_ADMISSION_SCHEMA,
            run_id=row[0],
            request_id=row[1],
            parent_run_id=row[2],
            from_stage=row[3],
            request=row[4],
            request_digest=row[5],
            created_at=row[6],
        )

    def load_admission(self, run_id: str) -> RunAdmission | None:
        with self._transaction() as connection:
            return self._load_admission_cursor(connection.cursor(), run_id)

    def load_admission_by_request_id(self, request_id: str) -> RunAdmission | None:
        """Read the immutable admission identity for one stable request key."""

        if not isinstance(request_id, str) or not request_id:
            raise FactStoreError("A request id is required")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT run_id, request_id, parent_run_id, from_stage, request, request_digest, created_at
                FROM run_admission
                WHERE request_id = %s
                """,
                (request_id,),
            )
            row = cursor.fetchone()
        return None if row is None else self._admission_from_row(row)

    def admit_diagnostic(
        self,
        spec: DiagnosticAdmissionSpec,
        command: StageCommand,
    ) -> tuple[RunAdmission, StageCommand]:
        """Admit exactly one diagnostic target command in the caller's private store."""

        if not isinstance(spec, DiagnosticAdmissionSpec):
            raise FactStoreError("A DiagnosticAdmissionSpec is required")
        admission = build_diagnostic_run_admission(spec)
        try:
            validate_diagnostic_command(spec, admission, command)
        except ProtocolError as error:
            raise FactStoreError("The diagnostic command fails typed validation") from error
        artifact_verifier = getattr(self._result_reader, "verify", None)
        if not callable(artifact_verifier):
            raise FactStoreError("The configured artifact reader cannot verify diagnostic inputs")
        for artifact in command.input_artifacts:
            artifact_verifier(artifact)
        with self._transaction() as connection:
            cursor = connection.cursor()
            _require_open(cursor)
            existing = self._load_admission_cursor(cursor, admission.run_id)
            if existing is not None:
                if existing != admission:
                    raise FactConflict("The diagnostic run is already admitted with different facts")
                stored = self._load_command_cursor(cursor, command.command_id)
                if stored != command:
                    raise FactConflict("The diagnostic run carries a different target command")
                return existing, stored
            cursor.execute("SELECT run_id FROM run_admission WHERE request_id = %s", (admission.request_id,))
            if cursor.fetchone() is not None:
                raise FactConflict("The diagnostic request is already admitted")
            cursor.execute(
                """
                INSERT INTO run_admission (
                    run_id, request_id, parent_run_id, from_stage, request, request_digest, created_at
                ) VALUES (%s, %s, NULL, %s, %s::jsonb, %s, %s)
                """,
                (
                    admission.run_id,
                    admission.request_id,
                    admission.from_stage,
                    _json(admission.request),
                    admission.request_digest,
                    admission.created_at,
                ),
            )
            self._insert_command_cursor(cursor, command)
            return admission, command

    def admit_run(
        self,
        admission: RunAdmission,
        command: StageCommand,
        dispatch_command: DispatchCommand,
        dispatch_outbound_artifact: ArtifactRef,
    ) -> tuple[StageCommand, DispatchCommand, EffectIntent]:
        """Atomically admit the run, Freeze command and technical dispatch."""

        if not isinstance(admission, RunAdmission):
            raise FactStoreError("A canonical RunAdmission is required")
        if not isinstance(command, StageCommand) or not isinstance(dispatch_command, DispatchCommand):
            raise FactStoreError("Canonical Freeze and dispatch commands are required")
        if command.run_id != admission.run_id or command.stage != "freeze" or command.kind != "stage":
            raise FactStoreError("Admission must create the initial Freeze stage command")
        if command.predecessor_decision_id is not None:
            raise FactStoreError("The initial Freeze command cannot have a predecessor decision")
        if (
            dispatch_command.run_id != admission.run_id
            or dispatch_command.freeze_command_id != command.command_id
            or dispatch_outbound_artifact != dispatch_command.conf_artifact
        ):
            raise FactStoreError("The technical dispatch is not bound to this admission and Freeze command")
        try:
            spec = AdmissionSpec.from_record(admission.request)
            validate_store_command(spec, admission, command)
        except ProtocolError as error:
            raise FactStoreError("The admission and Freeze command fail typed semantic validation") from error
        conf = self._read_dispatch_conf(dispatch_command.conf_artifact)
        if conf.run_id != admission.run_id or conf.freeze_command_id != command.command_id:
            raise FactStoreError("The Airflow dispatch conf names another run or Freeze command")
        dispatch_intent = self._dispatch_effect_intent(admission, dispatch_command)
        with self._transaction() as connection:
            cursor = connection.cursor()
            _require_open(cursor)
            existing = self._load_admission_cursor(cursor, admission.run_id)
            if existing is not None:
                if existing != admission:
                    raise FactConflict(f"The run {admission.run_id!r} is already admitted with different facts")
                stored_command = self._load_command_cursor(cursor, command.command_id)
                if stored_command != command:
                    raise FactConflict(f"The run {admission.run_id!r} carries a different Freeze command")
                stored_dispatch = self._load_dispatch_command_cursor(cursor, dispatch_command.command_id)
                stored_intent, _ = self._effect_cursor(cursor, dispatch_intent.effect_id)
                if (
                    stored_dispatch != dispatch_command
                    or stored_intent.identity_digest != dispatch_intent.identity_digest
                ):
                    raise FactConflict(f"The run {admission.run_id!r} carries a different dispatch")
                return stored_command, stored_dispatch, stored_intent
            cursor.execute("SELECT run_id FROM run_admission WHERE request_id = %s", (admission.request_id,))
            if cursor.fetchone() is not None:
                raise FactConflict(f"The request {admission.request_id!r} is already admitted")
            cursor.execute(
                """
                INSERT INTO run_admission (
                    run_id, request_id, parent_run_id, from_stage, request, request_digest, created_at
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    admission.run_id,
                    admission.request_id,
                    admission.parent_run_id,
                    admission.from_stage,
                    _json(admission.request),
                    admission.request_digest,
                    admission.created_at,
                ),
            )
            self._insert_artifacts_cursor(
                cursor,
                (
                    spec.originals.ref,
                    spec.ruleset.artifact.ref,
                    spec.configuration.ref,
                    spec.native_tool_requirement.ref,
                    spec.publish_intent.repository.artifact.ref,
                    *command.input_artifacts,
                    dispatch_command.conf_artifact,
                    dispatch_outbound_artifact,
                ),
            )
            self._insert_command_cursor(cursor, command)
            self._insert_dispatch_command_cursor(
                cursor,
                dispatch_command,
                freeze_command=command,
                admission=admission,
            )
            self._insert_effect_intent_cursor(
                cursor,
                dispatch_intent,
                dispatch_outbound_artifact,
            )
            return command, dispatch_command, dispatch_intent

    def _read_dispatch_conf(self, artifact: ArtifactRef) -> DispatchConf:
        reader = getattr(self._result_reader, "read_dispatch_conf", None)
        if not callable(reader):
            raise FactStoreError("The configured artifact reader cannot validate Airflow dispatch conf")
        return reader(artifact)

    def _dispatch_effect_intent(
        self,
        admission: RunAdmission,
        command: DispatchCommand,
    ) -> EffectIntent:
        expected = {
            "schema_version": AIRFLOW_DISPATCH_EFFECT_SCHEMA,
            "conf_artifact_id": command.conf_artifact.artifact_id,
            "conf_set_sha256": command.conf_artifact.set_sha256,
            "dag_id": command.dag_id,
            "dag_run_id": command.dag_run_id,
            "freeze_command_id": command.freeze_command_id,
            "request_digest": command.request_digest,
            "run_id": command.run_id,
        }
        return EffectIntent(
            effect_id="effect-" + digest(
                {
                    "command_id": command.command_id,
                    "effect_key": command.effect_key,
                }
            ),
            command_id=command.command_id,
            effect_key=command.effect_key,
            effect_kind="airflow_dispatch",
            target={"dag_id": command.dag_id, "dag_run_id": command.dag_run_id},
            expected_state=expected,
            expected_state_digest=digest(expected),
            outbound_artifact_id=command.conf_artifact.artifact_id,
            outbound_digest=command.conf_artifact.set_sha256,
            created_at=admission.created_at,
        )

    def _load_command_cursor(self, cursor, command_id: str) -> StageCommand | None:
        cursor.execute(
            """
            SELECT command_id, run_id, kind, stage, idempotency_key, request, request_digest,
                   config_snapshot_sha256, contract_sha256, policy_sha256,
                   expected_executor_sha256, predecessor_decision_id
            FROM command
            WHERE command_id = %s
            """,
            (command_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            """
            SELECT a.artifact_id, a.kind, a.locator, a.manifest_sha256, a.set_sha256,
                   a.producer_run_id, a.producer_stage, a.producer_tool_sha256
            FROM command_input_artifact ci
            JOIN artifact a ON a.artifact_id = ci.artifact_id
            WHERE ci.command_id = %s
            ORDER BY ci.position
            """,
            (command_id,),
        )
        inputs = tuple(_artifact_from_row(item) for item in cursor.fetchall())
        stored_request = row[5]
        if (
            isinstance(stored_request, Mapping)
                and stored_request.get("schema_version") == DIAGNOSTIC_COMMAND_REQUEST_SCHEMA
        ):
            if set(stored_request) != {"schema_version", "command"} or not isinstance(
                stored_request["command"],
                Mapping,
            ):
                raise FactStoreError("The diagnostic command envelope is invalid")
            command = StageCommand.from_record(stored_request["command"])
            if (
                command.command_id != row[0]
                or command.run_id != row[1]
                or command.kind != row[2]
                or command.stage != row[3]
                or command.idempotency_key != row[4]
                or command.request_digest != row[6]
                or command.input_artifacts != inputs
                or command.config_snapshot_sha256 != row[7]
                or command.contract_sha256 != row[8]
                or command.policy_sha256 != row[9]
                or command.expected_executor_sha256 != row[10]
                or command.predecessor_decision_id != row[11]
                or command.diagnostic_scope is None
            ):
                raise FactStoreError("The diagnostic command row differs from its sealed request")
            return command
        return StageCommand(
            schema_version=STAGE_COMMAND_SCHEMA,
            command_id=row[0],
            run_id=row[1],
            kind=row[2],
            stage=row[3],
            idempotency_key=row[4],
            request=row[5],
            request_digest=row[6],
            input_artifacts=inputs,
            config_snapshot_sha256=row[7],
            contract_sha256=row[8],
            policy_sha256=row[9],
            expected_executor_sha256=row[10],
            predecessor_decision_id=row[11],
        )

    def load_command(self, command_id: str) -> StageCommand:
        with self._transaction() as connection:
            command = self._load_command_cursor(connection.cursor(), command_id)
        if command is None:
            raise FactStoreError(f"The command {command_id!r} is not registered")
        return command

    def load_stage_command(self, run_id: str, stage: str) -> StageCommand:
        if stage not in STAGES:
            raise FactStoreError(f"The stage {stage!r} is not a canonical pipeline stage")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT command_id
                FROM command
                WHERE run_id = %s AND kind = 'stage' AND stage = %s
                """,
                (run_id, stage),
            )
            row = cursor.fetchone()
            if row is None:
                raise FactStoreError(f"The run {run_id!r} has no {stage!r} command")
            command = self._load_command_cursor(cursor, row[0])
        if command is None:  # pragma: no cover - selected in the same transaction
            raise FactStoreError("The selected stage command disappeared")
        return command

    def _load_dispatch_command_cursor(self, cursor, command_id: str) -> DispatchCommand | None:
        cursor.execute(
            """
            SELECT run_id, kind, stage, request, request_digest, expected_executor_sha256
            FROM command
            WHERE command_id = %s
            """,
            (command_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if row[1] != "dispatch" or row[2] is not None or not isinstance(row[3], Mapping):
            raise FactStoreError("The command is not a canonical technical dispatch")
        command = DispatchCommand.from_record(row[3])
        if (
            command.command_id != command_id
            or command.run_id != row[0]
            or command.request_digest != row[4]
            or command.expected_executor_sha256 != row[5]
        ):
            raise FactStoreError("The technical dispatch row differs from its recorded command")
        return command

    def load_dispatch_command(self, command_id: str) -> DispatchCommand:
        with self._transaction() as connection:
            command = self._load_dispatch_command_cursor(connection.cursor(), command_id)
        if command is None:
            raise FactStoreError(f"The dispatch command {command_id!r} is not registered")
        return command

    def _insert_command_cursor(self, cursor, command: StageCommand) -> None:
        self._insert_artifacts_cursor(cursor, command.input_artifacts)
        cursor.execute(
            """
            INSERT INTO command (
                command_id, run_id, kind, stage, idempotency_key, request, request_digest,
                config_snapshot_sha256, contract_sha256, policy_sha256,
                expected_executor_sha256, predecessor_decision_id
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
            """,
            (
                command.command_id,
                command.run_id,
                command.kind,
                command.stage,
                command.idempotency_key,
                _json(_stored_command_request(command)),
                command.request_digest,
                command.config_snapshot_sha256,
                command.contract_sha256,
                command.policy_sha256,
                command.expected_executor_sha256,
                command.predecessor_decision_id,
            ),
        )
        for position, artifact in enumerate(command.input_artifacts):
            cursor.execute(
                """
                INSERT INTO command_input_artifact (command_id, artifact_id, position)
                VALUES (%s, %s, %s)
                """,
                (command.command_id, artifact.artifact_id, position),
            )

    def _insert_dispatch_command_cursor(
        self,
        cursor,
        command: DispatchCommand,
        *,
        freeze_command: StageCommand,
        admission: RunAdmission,
    ) -> None:
        if (
            command.run_id != admission.run_id
            or command.freeze_command_id != freeze_command.command_id
            or freeze_command.run_id != admission.run_id
        ):
            raise FactStoreError("The technical dispatch is not bound to the admission Freeze command")
        self._insert_artifact(cursor, command.conf_artifact)
        cursor.execute(
            """
            INSERT INTO command (
                command_id, run_id, kind, stage, idempotency_key, request, request_digest,
                config_snapshot_sha256, contract_sha256, policy_sha256,
                expected_executor_sha256, predecessor_decision_id
            ) VALUES (%s, %s, 'dispatch', NULL, %s, %s::jsonb, %s, %s, %s, %s, %s, NULL)
            """,
            (
                command.command_id,
                command.run_id,
                command.idempotency_key,
                _json(command.to_record()),
                command.request_digest,
                freeze_command.config_snapshot_sha256,
                freeze_command.contract_sha256,
                freeze_command.policy_sha256,
                command.expected_executor_sha256,
            ),
        )

    def create_command(
        self,
        command: StageCommand,
        *,
        admission: RunAdmission,
        predecessor: VerifiedPredecessor,
        supporting_predecessors: tuple[VerifiedPredecessor, ...] = (),
        discover_result: VerifiedDiscoverResult | None = None,
        verify_result: VerifiedVerifyResult | None = None,
        publish_intent: VerifiedPublishIntent | None = None,
    ) -> StageCommand:
        """Validate and register one successor command after its predecessor decision."""

        if not isinstance(admission, RunAdmission):
            raise FactStoreError("A canonical RunAdmission is required")
        if not isinstance(predecessor, VerifiedPredecessor):
            raise FactStoreError("A verified committed predecessor is required")
        if not isinstance(supporting_predecessors, tuple) or not all(
            isinstance(item, VerifiedPredecessor) for item in supporting_predecessors
        ):
            raise FactStoreError("Supporting predecessors must be verified immutable facts")
        if discover_result is not None and not isinstance(discover_result, VerifiedDiscoverResult):
            raise FactStoreError("A verified Discover result is required")
        if verify_result is not None and not isinstance(verify_result, VerifiedVerifyResult):
            raise FactStoreError("A verified Verify result is required")
        if publish_intent is not None and not isinstance(publish_intent, VerifiedPublishIntent):
            raise FactStoreError("A verified Publish intent is required")
        if not isinstance(command, StageCommand) or command.kind != "stage" or command.stage is None:
            raise FactStoreError("A successor stage command is required")
        try:
            spec = AdmissionSpec.from_record(admission.request)
            validate_store_command(
                spec,
                admission,
                command,
                predecessor=predecessor,
                supporting_predecessors=supporting_predecessors,
                discover_result=discover_result,
                verify_result=verify_result,
                publish_intent=publish_intent,
            )
        except ProtocolError as error:
            raise FactStoreError("The successor command fails typed semantic validation") from error
        predecessors = (predecessor, *supporting_predecessors)
        artifact_verifier = getattr(self._result_reader, "verify", None)
        if not callable(artifact_verifier):
            raise FactStoreError("The configured artifact reader cannot verify predecessor outputs")
        for verified in predecessors:
            if verified.decision.result_record_artifact is None:  # pragma: no cover - verified fact
                raise FactStoreError("A predecessor has no StageResult record")
            artifact_verifier(verified.result.output_artifact)
            sealed_result = self._result_reader.read_result(verified.decision.result_record_artifact)
            if sealed_result != verified.result:
                raise FactStoreError("A sealed predecessor StageResult differs from its verified fact")
        with self._transaction() as connection:
            cursor = connection.cursor()
            self._lock_run_cursor(cursor, command.run_id)
            self._lock_command_chain_cursor(cursor, command)
            cursor.execute("SELECT run_id FROM run_admission WHERE run_id = %s", (command.run_id,))
            if cursor.fetchone() is None:
                raise FactStoreError(f"The run {command.run_id!r} is not admitted")
            stored_admission = self._load_admission_cursor(cursor, command.run_id)
            if stored_admission != admission:
                raise FactConflict("The supplied admission differs from the registered admission")
            for verified in predecessors:
                self._verify_registered_predecessor_cursor(cursor, admission, verified)
            if discover_result is not None:
                for artifact in (
                    discover_result.source_set_artifact,
                    discover_result.ruleset_artifact,
                    discover_result.configuration_artifact,
                ):
                    if self._artifact_cursor(cursor, artifact.artifact_id) != artifact:
                        raise FactConflict("A Discover result artifact differs from its registered fact")
            existing = self._load_command_cursor(cursor, command.command_id)
            if existing is not None:
                if existing != command:
                    raise FactConflict(f"The command {command.command_id!r} is already bound differently")
                return existing
            self._insert_command_cursor(cursor, command)
            return command

    def _verify_registered_predecessor_cursor(
        self,
        cursor,
        admission: RunAdmission,
        predecessor: VerifiedPredecessor,
    ) -> None:
        stored_command = self._load_command_cursor(cursor, predecessor.command.command_id)
        if stored_command != predecessor.command:
            raise FactConflict("A predecessor command differs from its registered command")
        decision_row = self._decision_row(
            cursor,
            run_id=predecessor.command.run_id,
            stage=predecessor.stage,
        )
        if decision_row is None:
            raise FactStoreError("A predecessor is not a completed decision of this run")
        stored_decision = self._decision_from_row(cursor, decision_row)
        if stored_decision != predecessor.decision:
            raise FactConflict("A predecessor decision differs from its verified fact")
        if predecessor.decision.result_record_artifact is None:  # pragma: no cover - verified fact
            raise FactStoreError("A predecessor has no StageResult record")
        if (
            self._artifact_cursor(cursor, predecessor.result.output_artifact.artifact_id)
            != predecessor.result.output_artifact
            or self._artifact_cursor(cursor, predecessor.decision.result_record_artifact.artifact_id)
            != predecessor.decision.result_record_artifact
        ):
            raise FactConflict("Predecessor artifacts differ from their verified facts")
        qualification = qualify_stage(admission, stored_command, result=predecessor.result)
        require_formal_qualification(
            qualification,
            admission,
            stored_command,
            result=predecessor.result,
        )
        self._verify_completed_predecessor_chain_cursor(cursor, stored_command)
        self._verify_completed_command_effects_cursor(
            cursor,
            stored_command,
            decision=stored_decision,
        )

    def _decision_row(self, cursor, *, run_id: str, stage: str):
        cursor.execute(
            """
            SELECT decision_id, run_id, stage, command_id, outcome, result_record_artifact_id,
                   result_digest, actual_executor_sha256, original_producer_decision_id,
                   reason_code, decided_at, failure_envelope, failure_digest
            FROM stage_decision
            WHERE run_id = %s AND stage = %s
            """,
            (run_id, stage),
        )
        return cursor.fetchone()

    def _artifact_cursor(self, cursor, artifact_id: str) -> ArtifactRef:
        cursor.execute(
            """
            SELECT artifact_id, kind, locator, manifest_sha256, set_sha256,
                   producer_run_id, producer_stage, producer_tool_sha256
            FROM artifact
            WHERE artifact_id = %s
            """,
            (artifact_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise FactStoreError(f"The artifact {artifact_id!r} is not registered")
        return _artifact_from_row(row)

    def _decision_from_row(self, cursor, row) -> StageDecision:
        failure_record = row[11]
        failure = FailureEnvelope.from_record(failure_record) if failure_record is not None else None
        if failure is not None and digest(failure.to_record()) != row[12]:
            raise FactStoreError("The failure envelope digest differs from the registered digest")
        return StageDecision(
            decision_id=row[0],
            run_id=row[1],
            stage=row[2],
            command_id=row[3],
            outcome=row[4],
            result_record_artifact=self._artifact_cursor(cursor, row[5]) if row[5] is not None else None,
            result_digest=row[6],
            actual_executor_sha256=row[7],
            original_producer_decision_id=row[8],
            reason_code=row[9],
            decided_at=row[10],
            failure_envelope=failure,
        )

    def decision_for(self, command: StageCommand) -> StageDecision | None:
        if command.stage is None:
            raise FactStoreError("Only a stage command has a stage decision")
        with self._transaction() as connection:
            cursor = connection.cursor()
            stored_command = self._load_command_cursor(cursor, command.command_id)
            if stored_command != command:
                raise FactConflict("The stage command differs from its registered command")
            row = self._decision_row(cursor, run_id=command.run_id, stage=command.stage)
            if row is None:
                return None
            decision = self._decision_from_row(cursor, row)
            if decision.outcome == "completed":
                self._verify_completed_predecessor_chain_cursor(cursor, command)
                self._verify_completed_command_effects_cursor(cursor, command, decision=decision)
        if decision.command_id != command.command_id:
            raise FactConflict("The stage decision is bound to another command")
        return decision

    def _decision_by_id_cursor(self, cursor, decision_id: str):
        cursor.execute(
            """
            SELECT decision_id, run_id, stage, command_id, outcome, result_record_artifact_id,
                   result_digest, actual_executor_sha256, original_producer_decision_id,
                   reason_code, decided_at, failure_envelope, failure_digest
            FROM stage_decision
            WHERE decision_id = %s
            """,
            (decision_id,),
        )
        return cursor.fetchone()

    def _completed_predecessor_chain_cursor(self, cursor, command: StageCommand) -> list[StageCommand]:
        """Resolve the immutable upstream command chain from leaf to root."""

        if command.kind != "stage" or command.stage is None:
            return []
        chain = []
        current = command
        seen = set()
        while current.predecessor_decision_id is not None:
            chain_key = (current.run_id, current.predecessor_decision_id)
            if chain_key in seen or len(seen) >= len(STAGES):
                raise FactConflict("The command predecessor chain is cyclic or too deep")
            seen.add(chain_key)
            decision_row = self._decision_by_id_cursor(cursor, current.predecessor_decision_id)
            if decision_row is None:
                raise FactConflict("The command predecessor decision is missing")
            decision = self._decision_from_row(cursor, decision_row)
            predecessor = self._load_command_cursor(cursor, decision.command_id)
            if predecessor is None or predecessor.kind != "stage" or predecessor.stage is None:
                raise FactConflict("The predecessor decision has no canonical stage command")
            if (
                decision.outcome != "completed"
                or predecessor.run_id != command.run_id
                or STAGES.index(predecessor.stage) >= STAGES.index(current.stage)
            ):
                raise FactConflict("The predecessor decision is not an earlier completed stage of this run")
            chain.append(predecessor)
            current = predecessor
        return chain

    def _verify_completed_command_effects_cursor(
        self,
        cursor,
        command: StageCommand,
        *,
        decision: StageDecision | None = None,
    ) -> StageResult:
        if command.kind != "stage" or command.stage is None:
            raise FactConflict("Only a canonical stage command can carry a completed decision")
        if decision is None:
            row = self._decision_row(cursor, run_id=command.run_id, stage=command.stage)
            if row is None:
                raise FactConflict("A completed predecessor decision is missing")
            decision = self._decision_from_row(cursor, row)
        if (
            decision.command_id != command.command_id
            or decision.run_id != command.run_id
            or decision.stage != command.stage
            or decision.outcome != "completed"
            or decision.result_record_artifact is None
        ):
            raise FactConflict("The command is not bound to its completed stage decision")
        result = self._result_reader.read_result(decision.result_record_artifact)
        if (
            result.command_id != command.command_id
            or result.run_id != command.run_id
            or result.stage != command.stage
            or result.outcome != "completed"
            or result.actual_executor_sha256 != command.expected_executor_sha256
            or result.digest != decision.result_digest
        ):
            raise FactConflict("The completed StageResult differs from its command and decision")
        states = self._effect_states_for_command_cursor(cursor, command.command_id, lock=True)
        if set(result.required_effect_ids) != set(states):
            raise FactConflict("The completed StageResult effect ids differ from the current effect ledger")
        if any(state is not EffectState.SATISFIED for state in states.values()):
            raise FactConflict("The completed stage predecessor now has a non-satisfied effect")
        return result

    def _verify_completed_predecessor_chain_cursor(self, cursor, command: StageCommand) -> None:
        chain = self._completed_predecessor_chain_cursor(cursor, command)
        for predecessor in reversed(chain):
            self._verify_completed_command_effects_cursor(cursor, predecessor)

    def _cancel_command_cursor(
        self,
        cursor,
        target_command_id: str,
        *,
        lock: bool = False,
    ) -> StageCommand | None:
        if lock:
            cursor.execute(
                "SELECT run_id, kind FROM command WHERE command_id = %s",
                (target_command_id,),
            )
            target_row = cursor.fetchone()
            if target_row is None:
                return None
            self._lock_run_cursor(cursor, target_row[0])
            if target_row[1] != "stage":
                self._lock_command_cursor(cursor, target_command_id)
                return None
            target = self._load_command_cursor(cursor, target_command_id)
            if target is None:  # pragma: no cover - selected in the same transaction
                return None
            self._lock_command_chain_cursor(cursor, target)
        cursor.execute(
            """
            SELECT command_id
            FROM command
            WHERE kind = 'cancel'
              AND request->>'target_command_id' = %s
            ORDER BY command_id
            """,
            (target_command_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise FactConflict("A target command carries multiple cancellation requests")
        command = self._load_command_cursor(cursor, rows[0][0])
        if command is None:
            raise FactStoreError("The cancellation command disappeared")
        request = CancellationRequest.from_command(command)
        if request.target_command_id != target_command_id:
            raise FactConflict("The cancellation command names another target")
        return command

    def request_cancel(
        self,
        *,
        target_command_id: str,
        cancel_executor_sha256: str,
        reason_code: str,
        reason: str,
        requested_by: str,
        requested_at: datetime,
    ) -> StageCommand | StageDecision:
        """Record one immutable cancellation intent without writing a StageDecision."""

        with self._transaction() as connection:
            cursor = connection.cursor()
            target = self._load_command_cursor(cursor, target_command_id)
            if target is None:
                raise FactStoreError(f"The target command {target_command_id!r} is not registered")
            self._lock_command_chain_cursor(cursor, target)
            target = self._load_command_cursor(cursor, target_command_id)
            if target is None or target.kind != "stage" or target.stage is None:
                raise FactStoreError("Only a stage command can be cancelled")
            if target.diagnostic_scope is not None:
                raise FactStoreError("Diagnostic commands do not accept production cancellation")
            decision_row = self._decision_row(cursor, run_id=target.run_id, stage=target.stage)
            if decision_row is not None:
                decision = self._decision_from_row(cursor, decision_row)
                if decision.command_id != target.command_id:
                    raise FactConflict("The target stage decision belongs to another command")
                return decision
            request = CancellationRequest(
                schema_version=CANCEL_REQUEST_SCHEMA,
                target_command_id=target.command_id,
                target_run_id=target.run_id,
                target_stage=target.stage,
                target_request_digest=target.request_digest,
                reason_code=reason_code,
                reason=reason,
                requested_by=requested_by,
                requested_at=requested_at,
            )
            existing = self._cancel_command_cursor(
                cursor,
                target.command_id,
                lock=True,
            )
            if existing is not None:
                if (
                    CancellationRequest.from_command(existing).logical_identity()
                    != request.logical_identity()
                    or existing.expected_executor_sha256 != cancel_executor_sha256
                ):
                    raise FactConflict("The target command already has a different cancellation request")
                return existing
            command = StageCommand.create_cancel(
                command_id="cancel:" + digest(
                    {
                        "target_command_id": target.command_id,
                        "target_request_digest": target.request_digest,
                    }
                ),
                run_id=target.run_id,
                idempotency_key=f"{target.run_id}/cancel/{target.command_id}",
                request=request,
                config_snapshot_sha256=target.config_snapshot_sha256,
                contract_sha256=target.contract_sha256,
                policy_sha256=target.policy_sha256,
                expected_executor_sha256=cancel_executor_sha256,
            )
            self._insert_command_cursor(cursor, command)
            return command

    def cancellation_for(self, command: StageCommand) -> StageCommand | None:
        if command.kind != "stage" or command.stage is None:
            raise FactStoreError("Only a stage command can carry a cancellation request")
        with self._transaction() as connection:
            return self._cancel_command_cursor(connection.cursor(), command.command_id)

    def load_result(self, decision: StageDecision) -> StageResult:
        if decision.result_record_artifact is None:
            raise FactStoreError("A minimal failure envelope has no StageResult artifact")
        result = self._result_reader.read_result(decision.result_record_artifact)
        if (
            result.command_id != decision.command_id
            or result.run_id != decision.run_id
            or result.stage != decision.stage
            or result.outcome != decision.outcome
            or result.digest != decision.result_digest
        ):
            raise FactStoreError("The sealed StageResult differs from its registered decision")
        return result

    def qualification_for(self, decision: StageDecision) -> StageQualification:
        command = self.load_command(decision.command_id)
        if decision.outcome == "completed":
            current = self.decision_for(command)
            if current != decision:
                raise FactConflict("The completed decision no longer qualifies under the current effect ledger")
        admission = self.load_admission(decision.run_id)
        if admission is None:
            raise FactStoreError("The decision run has no admission")
        if command.diagnostic_scope is not None:
            spec = DiagnosticAdmissionSpec.from_record(admission.request)
            validate_diagnostic_command(spec, admission, command)
        else:
            spec = AdmissionSpec.from_record(admission.request)
            validate_run_admission(spec, admission)
        if decision.result_record_artifact is not None:
            return qualify_stage(admission, command, result=self.load_result(decision))
        if decision.failure_envelope is None:
            raise FactStoreError("The decision carries neither a StageResult nor a FailureEnvelope")
        return qualify_stage(admission, command, failure=decision.failure_envelope)

    def acquire_claim(
        self,
        command_id: str,
        *,
        claim_id: str,
        owner_sha256: str,
        executor_sha256: str,
        now: datetime,
        expires_at: datetime,
        lane_key: str | None = None,
        dag_id: str | None = None,
        dag_run_id: str | None = None,
        task_id: str | None = None,
        map_index: int | None = None,
        claim_scope: str = "execution",
        effect_scope_id: str | None = None,
    ) -> ExecutionClaim:
        """Acquire one open fenced execution right without treating lease expiry as success."""

        if not isinstance(now, datetime) or now.tzinfo is None:
            raise FactStoreError("Claim time must be timezone-aware")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at <= now:
            raise FactStoreError("Claim expiry must be after a timezone-aware acquisition time")
        if claim_scope not in {"execution", "transfer_resume"}:
            raise FactStoreError("The execution claim scope is invalid")
        if (claim_scope == "transfer_resume") != (effect_scope_id is not None):
            raise FactStoreError("The execution claim scope and effect are inconsistent")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT command_id, run_id, kind, stage, expected_executor_sha256,
                       predecessor_decision_id
                FROM command
                WHERE command_id = %s
                """,
                (command_id,),
            )
            command = cursor.fetchone()
            if command is None:
                raise FactStoreError(f"The command {command_id!r} is not registered")
            self._lock_run_cursor(cursor, command[1])
            stored_command = None
            if command[2] == "stage":
                if command[5] is not None:
                    stored_command = self._load_command_cursor(cursor, command_id)
                    if stored_command is None:
                        raise FactStoreError(f"The command {command_id!r} is not registered")
                    self._lock_command_chain_cursor(cursor, stored_command)
                else:
                    self._lock_command_cursor(cursor, command_id)
            else:
                self._lock_command_cursor(cursor, command_id)
            if stored_command is not None:
                self._verify_completed_predecessor_chain_cursor(cursor, stored_command)
            self._lock_effect_ledger_cursor(cursor, command_id)
            effect_settlements = self._effect_settlements_for_command_cursor(
                cursor,
                command_id,
                lock=False,
            )
            effect_states = {effect_id: item.state for effect_id, item in effect_settlements.items()}
            if claim_scope == "transfer_resume":
                scoped = effect_settlements.get(effect_scope_id)
                if (
                    scoped is None
                    or scoped.state is not EffectState.UNKNOWN
                    or scoped.resume_phase is not EffectResumePhase.INPUT_TRANSFER_PENDING
                ):
                    raise FactConflict(
                        "A transfer resume claim requires its exact pending native effect"
                    )
                scoped_intent, _scoped_facts = self._effect_cursor(
                    cursor,
                    effect_scope_id,
                    lock=False,
                )
                if scoped_intent.effect_kind != "native_operation":
                    raise FactConflict(
                        "A transfer resume claim is limited to native operation effects"
                    )
                require_native_transfer_target(scoped_intent.target)
            unresolved = (
                any(
                    state in {EffectState.UNKNOWN, EffectState.CONFLICT}
                    for effect_id, state in effect_states.items()
                    if effect_id != effect_scope_id
                )
                if claim_scope == "transfer_resume"
                else any(
                    state in {EffectState.UNKNOWN, EffectState.CONFLICT}
                    for state in effect_states.values()
                )
            )
            if command[4] != executor_sha256:
                raise FactConflict("The claim executor identity differs from the command expectation")
            if (
                command[2] == "stage"
                and command[3] is not None
                and self._decision_row(cursor, run_id=command[1], stage=command[3]) is not None
            ):
                raise FactConflict("A decided command cannot acquire a new execution claim")
            cursor.execute(
                """
                SELECT claim_id, command_id, fence, owner_sha256, executor_sha256,
                       lane_key, expires_at, scope, effect_scope_id
                FROM execution_claim
                WHERE command_id = %s AND closed_at IS NULL
                FOR UPDATE
                """,
                (command_id,),
            )
            current = cursor.fetchone()
            if current is not None:
                claim = ExecutionClaim(
                    claim_id=current[0],
                    command_id=current[1],
                    fence=current[2],
                    owner_sha256=current[3],
                    executor_sha256=current[4],
                    lane_key=current[5],
                    expires_at=current[6],
                    scope=current[7],
                    effect_scope_id=current[8],
                )
                if claim.expires_at > now:
                    raise FactConflict("Another live executor owns the command")
                if unresolved:
                    raise FactConflict("An expired claim carries an unresolved effect and requires reconciliation")
                closure_code = (
                    "reconciled"
                    if any(state is not EffectState.PREPARED for state in effect_states.values())
                    else "abandoned_before_effect"
                )
                cursor.execute(
                    """
                    UPDATE execution_claim
                    SET closed_at = %s, closure_code = %s
                    WHERE claim_id = %s AND closed_at IS NULL
                    """,
                    (now, closure_code, claim.claim_id),
                )
            elif unresolved:
                raise FactConflict("The command carries an unresolved effect and requires reconciliation")
            cursor.execute(
                "SELECT COALESCE(MAX(fence), 0) + 1 FROM execution_claim WHERE command_id = %s",
                (command_id,),
            )
            fence = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO execution_claim (
                    claim_id, command_id, fence, owner_sha256, executor_sha256,
                    lane_key, scope, effect_scope_id,
                    dag_id, dag_run_id, task_id, map_index,
                    issued_at, last_seen_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    claim_id,
                    command_id,
                    fence,
                    owner_sha256,
                    executor_sha256,
                    lane_key,
                    claim_scope,
                    effect_scope_id,
                    dag_id,
                    dag_run_id,
                    task_id,
                    map_index,
                    now,
                    now,
                    expires_at,
                ),
            )
            return ExecutionClaim(
                claim_id=claim_id,
                command_id=command_id,
                fence=fence,
                owner_sha256=owner_sha256,
                executor_sha256=executor_sha256,
                lane_key=lane_key,
                expires_at=expires_at,
                scope=claim_scope,
                effect_scope_id=effect_scope_id,
            )

    def _effect_state_cursor(
        self,
        cursor,
        effect_id: str,
        *,
        expected_command_id: str | None = None,
    ) -> EffectState:
        intent, facts = self._effect_cursor(cursor, effect_id, lock=True)
        if expected_command_id is not None and intent.command_id != expected_command_id:
            raise FactConflict("The effect names another command")
        return project_effect_state(intent, facts)

    def _effect_cursor(
        self,
        cursor,
        effect_id: str,
        *,
        lock: bool = False,
    ) -> tuple[EffectIntent, tuple[EffectFact, ...]]:
        if lock:
            cursor.execute(
                "SELECT command_id FROM effect_intent WHERE effect_id = %s",
                (effect_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise FactStoreError(f"The effect {effect_id!r} is not registered")
            self._lock_command_scope_cursor(cursor, row[0])
            self._lock_effect_ledger_cursor(cursor, row[0])
            self._lock_effect_cursor(cursor, effect_id)
        cursor.execute(
            """
            SELECT effect_id, command_id, effect_key, effect_kind, target, expected_state,
                   expected_state_digest, outbound_artifact_id, outbound_digest, created_at
            FROM effect_intent
            WHERE effect_id = %s
            """,
            (effect_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise FactStoreError(f"The effect {effect_id!r} is not registered")
        intent = EffectIntent(
            effect_id=row[0],
            command_id=row[1],
            effect_key=row[2],
            effect_kind=row[3],
            target=row[4],
            expected_state=row[5],
            expected_state_digest=row[6],
            outbound_artifact_id=row[7],
            outbound_digest=row[8],
            created_at=row[9],
        )
        cursor.execute(
            """
            SELECT fact_id, effect_id, seq, claim_id, fence, fact_type, source,
                   state_class, payload_artifact_id, payload_digest, metadata_artifact_id,
                   observed_at, resume_phase
            FROM effect_fact
            WHERE effect_id = %s
            ORDER BY seq
            """,
            (effect_id,),
        )
        facts = tuple(
            EffectFact(
                fact_id=item[0],
                effect_id=item[1],
                seq=item[2],
                claim_id=item[3],
                fence=item[4],
                fact_type=item[5],
                source=item[6],
                state_class=item[7],
                payload_artifact_id=item[8],
                payload_digest=item[9],
                metadata_artifact_id=item[10],
                observed_at=item[11],
                resume_phase=EffectResumePhase(item[12]) if item[12] is not None else None,
            )
            for item in cursor.fetchall()
        )
        return intent, facts

    def effect_state(self, effect_id: str, *, expected_command_id: str | None = None) -> EffectState:
        with self._transaction() as connection:
            return self._effect_state_cursor(
                connection.cursor(),
                effect_id,
                expected_command_id=expected_command_id,
            )

    def effect_intent(
        self,
        effect_id: str,
        *,
        expected_command_id: str | None = None,
    ) -> EffectIntent:
        with self._transaction() as connection:
            intent, _facts = self._effect_cursor(connection.cursor(), effect_id)
        if expected_command_id is not None and intent.command_id != expected_command_id:
            raise FactConflict("The effect names another command")
        return intent

    def _effect_settlement_cursor(
        self,
        cursor,
        effect_id: str,
        *,
        expected_command_id: str | None = None,
    ) -> EffectSettlement:
        intent, facts = self._effect_cursor(cursor, effect_id, lock=True)
        if expected_command_id is not None and intent.command_id != expected_command_id:
            raise FactConflict("The effect names another command")
        settlement = project_effect_settlement(intent, facts)
        outbound_artifact = self._artifact_cursor(cursor, intent.outbound_artifact_id)
        artifact_id = settlement.decisive_fact.payload_artifact_id if settlement.decisive_fact else None
        if artifact_id is None:
            return replace(settlement, outbound_artifact=outbound_artifact)
        return replace(
            settlement,
            payload_artifact=self._artifact_cursor(cursor, artifact_id),
            outbound_artifact=outbound_artifact,
        )

    def effect_settlement(self, effect_id: str, *, expected_command_id: str | None = None) -> EffectSettlement:
        with self._transaction() as connection:
            return self._effect_settlement_cursor(
                connection.cursor(),
                effect_id,
                expected_command_id=expected_command_id,
            )

    def effect_settlement_for_key(self, command_id: str, effect_key: str) -> EffectSettlement | None:
        with self._transaction() as connection:
            cursor = connection.cursor()
            self._lock_command_scope_cursor(cursor, command_id)
            self._lock_effect_ledger_cursor(cursor, command_id)
            cursor.execute(
                """
                SELECT effect_id
                FROM effect_intent
                WHERE command_id = %s AND effect_key = %s
                """,
                (command_id, effect_key),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._effect_settlement_cursor(cursor, row[0], expected_command_id=command_id)

    def effect_intent_for_key(self, command_id: str, effect_key: str) -> EffectIntent | None:
        with self._transaction() as connection:
            cursor = connection.cursor()
            self._lock_command_scope_cursor(cursor, command_id)
            self._lock_effect_ledger_cursor(cursor, command_id)
            cursor.execute(
                """
                SELECT effect_id
                FROM effect_intent
                WHERE command_id = %s AND effect_key = %s
                """,
                (command_id, effect_key),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            intent, _ = self._effect_cursor(cursor, row[0], lock=True)
            if intent.command_id != command_id:
                raise FactConflict("The effect names another command")
            return intent

    @staticmethod
    def _effect_wait(command: StageCommand, intent: EffectIntent) -> StageEffectWait:
        if command.kind != "stage" or command.stage is None:
            raise FactConflict("Only a stage command can yield an effect")
        return StageEffectWait(
            schema_version=STAGE_EFFECT_WAIT_SCHEMA,
            run_id=command.run_id,
            stage=command.stage,
            command_id=command.command_id,
            request_digest=command.request_digest,
            effect_id=intent.effect_id,
            effect_key=intent.effect_key,
            effect_kind=intent.effect_kind,
            intent_identity_digest=intent.identity_digest,
            outbound_artifact_id=intent.outbound_artifact_id,
            outbound_digest=intent.outbound_digest,
            expected_state_digest=intent.expected_state_digest,
        )

    def pending_effect_for_command(self, command_id: str) -> StagePending | None:
        with self._transaction() as connection:
            cursor = connection.cursor()
            command = self._load_command_cursor(cursor, command_id)
            if command is None or command.kind != "stage" or command.stage is None:
                raise FactConflict("Only a registered stage command can carry a pending effect")
            cursor.execute(
                "SELECT 1 FROM stage_decision WHERE command_id = %s LIMIT 1",
                (command_id,),
            )
            if cursor.fetchone() is not None:
                return None
            cursor.execute(
                """
                SELECT yield_effect_id
                FROM execution_claim
                WHERE command_id = %s
                  AND closure_code = 'yielded'
                  AND yield_effect_id IS NOT NULL
                ORDER BY fence DESC
                LIMIT 1
                """,
                (command_id,),
            )
            claim = cursor.fetchone()
            if claim is None:
                return None
            intent, facts = self._effect_cursor(cursor, claim[0])
            state = project_effect_state(intent, facts)
            if state is EffectState.CONFLICT:
                raise FactConflict("A yielded effect is conflicted and requires operator resolution")
            return StagePending(self._effect_wait(command, intent))

    def verify_pending_effect(
        self,
        command: StageCommand,
        pending: StagePending,
    ) -> StagePending:
        if not isinstance(pending, StagePending):
            raise FactStoreError("A canonical StagePending is required")
        wait = pending.wait
        if (
            wait.run_id != command.run_id
            or wait.stage != command.stage
            or wait.command_id != command.command_id
            or wait.request_digest != command.request_digest
        ):
            raise FactConflict("The pending effect names another stage command")
        with self._transaction() as connection:
            cursor = connection.cursor()
            stored_command = self._load_command_cursor(cursor, command.command_id)
            if stored_command != command:
                raise FactConflict("The pending command differs from its registered command")
            self._lock_command_chain_cursor(cursor, stored_command)
            intent, _facts = self._effect_cursor(cursor, wait.effect_id, lock=True)
            canonical = StagePending(self._effect_wait(command, intent))
            if canonical != pending:
                raise FactConflict("The pending effect identity differs from its canonical intent")
            cursor.execute(
                """
                SELECT yield_effect_id
                FROM execution_claim
                WHERE command_id = %s AND closure_code = 'yielded'
                ORDER BY fence DESC
                LIMIT 1
                """,
                (command.command_id,),
            )
            yielded = cursor.fetchone()
            if yielded is None or yielded[0] != wait.effect_id:
                raise FactConflict("The effect has no canonical yielded claim")
        return pending

    def yield_claim_for_effect(
        self,
        *,
        command: StageCommand,
        claim: ExecutionClaim,
        effect_id: str,
        yielded_at: datetime,
    ) -> StagePending:
        if not isinstance(command, StageCommand) or command.kind != "stage" or command.stage is None:
            raise FactStoreError("A canonical stage command is required for deferral")
        if claim.command_id != command.command_id:
            raise FactConflict("The yielded claim names another command")
        if claim.scope == "transfer_resume":
            self._require_transfer_resume_claim(
                claim,
                effect_id,
                "Transfer yield",
            )
        else:
            self._require_execution_claim(claim, "Effect yield")
        if not isinstance(yielded_at, datetime) or yielded_at.tzinfo is None:
            raise FactStoreError("Effect yield time must be timezone-aware")
        verifier = getattr(self._result_reader, "verify", None)
        if not callable(verifier):
            raise FactStoreError("The configured artifact reader cannot verify deferred outbound bytes")
        with self._transaction() as connection:
            cursor = connection.cursor()
            stored_command = self._load_command_cursor(cursor, command.command_id)
            if stored_command != command:
                raise FactConflict("The deferred command differs from its registered command")
            self._lock_command_chain_cursor(cursor, stored_command)
            self._lock_effect_ledger_cursor(cursor, command.command_id)
            cursor.execute(
                """
                SELECT command_id, fence, owner_sha256, executor_sha256,
                       closed_at, closure_code, yield_effect_id, scope, effect_scope_id
                FROM execution_claim
                WHERE claim_id = %s
                FOR UPDATE
                """,
                (claim.claim_id,),
            )
            stored = cursor.fetchone()
            if (
                stored is None
                or stored[:4]
                != (
                    claim.command_id,
                    claim.fence,
                    claim.owner_sha256,
                    claim.executor_sha256,
                )
                or stored[7] != claim.scope
                or stored[8] != claim.effect_scope_id
            ):
                raise FactConflict("The deferred claim identity is stale or foreign")
            if stored[4] is not None:
                if stored[5] == "yielded" and stored[6] == effect_id:
                    intent, _facts = self._effect_cursor(cursor, effect_id, lock=True)
                    outbound = self._artifact_cursor(cursor, intent.outbound_artifact_id)
                    if outbound.set_sha256 != intent.outbound_digest:
                        raise FactConflict("The deferred outbound artifact differs from its intent")
                    verifier(outbound)
                    return StagePending(self._effect_wait(command, intent))
                raise FactConflict("The execution claim is already closed")
            intent, facts = self._effect_cursor(cursor, effect_id, lock=True)
            if intent.command_id != command.command_id:
                raise FactConflict("The deferred effect names another command")
            outbound = self._artifact_cursor(cursor, intent.outbound_artifact_id)
            if outbound.set_sha256 != intent.outbound_digest:
                raise FactConflict("The deferred outbound artifact differs from its intent")
            verifier(outbound)
            state = project_effect_state(intent, facts)
            if state is not EffectState.UNKNOWN:
                raise FactConflict(f"The effect is {state.value}; it cannot be yielded")
            other_states = self._effect_states_for_command_cursor(
                cursor,
                command.command_id,
            )
            if any(
                other_id != effect_id
                and other_state in {EffectState.UNKNOWN, EffectState.CONFLICT}
                for other_id, other_state in other_states.items()
            ):
                raise FactConflict("Another unresolved effect prevents an unambiguous yield")
            cursor.execute(
                """
                SELECT 1
                FROM effect_fact
                WHERE effect_id = %s
                  AND claim_id = %s
                  AND fence = %s
                  AND fact_type IN ('dispatch_authorized', 'resume_authorized')
                LIMIT 1
                """,
                (effect_id, claim.claim_id, claim.fence),
            )
            if cursor.fetchone() is None:
                raise FactConflict("A yielded effect requires its exact scoped authorization")
            cursor.execute(
                """
                UPDATE execution_claim
                SET closed_at = %s, last_seen_at = %s,
                    closure_code = 'yielded', yield_effect_id = %s
                WHERE claim_id = %s
                  AND command_id = %s
                  AND fence = %s
                  AND owner_sha256 = %s
                  AND executor_sha256 = %s
                  AND scope = %s
                  AND effect_scope_id IS NOT DISTINCT FROM %s
                  AND closed_at IS NULL
                RETURNING claim_id
                """,
                (
                    yielded_at,
                    yielded_at,
                    effect_id,
                    claim.claim_id,
                    claim.command_id,
                    claim.fence,
                    claim.owner_sha256,
                    claim.executor_sha256,
                    claim.scope,
                    claim.effect_scope_id,
                ),
            )
            if len(cursor.fetchall()) != 1:
                raise FactConflict("The execution claim changed before it could be yielded")
            return StagePending(self._effect_wait(command, intent))

    def _effect_settlements_for_command_cursor(
        self,
        cursor,
        command_id: str,
        *,
        lock: bool = False,
    ) -> dict[str, EffectSettlement]:
        if lock:
            self._lock_command_scope_cursor(cursor, command_id)
            self._lock_effect_ledger_cursor(cursor, command_id)
        cursor.execute(
            """
            SELECT effect_id
            FROM effect_intent
            WHERE command_id = %s
            ORDER BY effect_id
            """,
            (command_id,),
        )
        effect_ids = [row[0] for row in cursor.fetchall()]
        return {
            effect_id: project_effect_settlement(
                *self._effect_cursor(cursor, effect_id, lock=lock)
            )
            for effect_id in effect_ids
        }

    def _effect_states_for_command_cursor(
        self,
        cursor,
        command_id: str,
        *,
        lock: bool = False,
    ) -> dict[str, EffectState]:
        return {
            effect_id: settlement.state
            for effect_id, settlement in self._effect_settlements_for_command_cursor(
                cursor,
                command_id,
                lock=lock,
            ).items()
        }

    def effect_states_for_command(self, command_id: str) -> dict[str, EffectState]:
        with self._transaction() as connection:
            return self._effect_states_for_command_cursor(connection.cursor(), command_id)

    def effect_ids_for_command(self, command_id: str) -> tuple[str, ...]:
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT effect_id FROM effect_intent WHERE command_id = %s ORDER BY effect_id",
                (command_id,),
            )
            return tuple(row[0] for row in cursor.fetchall())

    def _append_effect_fact_cursor(
        self,
        cursor,
        *,
        effect_id: str,
        claim: ExecutionClaim | None,
        fact_type: str,
        source: str,
        state_class: str | None,
        payload_artifact: ArtifactRef | None,
        payload_digest: str,
        observed_at: datetime,
        fact_id: str,
        expected_command_id: str | None = None,
        metadata_artifact: ArtifactRef | None = None,
        resume_phase: EffectResumePhase | None = None,
    ) -> EffectFact:
        cursor.execute(
            "SELECT command_id FROM effect_intent WHERE effect_id = %s",
            (effect_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise FactStoreError(f"The effect {effect_id!r} is not registered")
        command_id = row[0]
        if claim is not None and claim.command_id != command_id:
            raise FactConflict("The effect fact claim names another command")
        self._lock_command_scope_cursor(cursor, command_id)
        self._lock_effect_ledger_cursor(cursor, command_id)
        stored = None
        if claim is not None:
            if fact_type == "resume_authorized":
                self._require_transfer_resume_claim(
                    claim,
                    effect_id,
                    "Resume authorization",
                )
            elif fact_type in {"response", "timeout"} and claim.scope == "transfer_resume":
                self._require_transfer_resume_claim(
                    claim,
                    effect_id,
                    f"Transfer {fact_type}",
                )
            else:
                self._require_execution_claim(
                    claim,
                    f"{fact_type} fact",
                )
            if fact_type in {"dispatch_authorized", "resume_authorized"}:
                cursor.execute(
                    "SELECT command_id FROM command WHERE command_id = %s",
                    (claim.command_id,),
                )
                if cursor.fetchone() is None:
                    raise FactStoreError("The dispatch authorization command disappeared")
                if self._cancel_command_cursor(cursor, claim.command_id, lock=True) is not None:
                    raise FactConflict("Cancellation was requested before effect dispatch")
            cursor.execute(
                """
                SELECT command_id, fence, owner_sha256, executor_sha256,
                       closed_at, closure_code
                FROM execution_claim
                WHERE claim_id = %s
                FOR UPDATE
                """,
                (claim.claim_id,),
            )
            stored = cursor.fetchone()
            if stored is None or stored[:4] != (
                claim.command_id,
                claim.fence,
                claim.owner_sha256,
                claim.executor_sha256,
            ):
                raise FactConflict("The effect fact names a foreign claim/fence")
            if stored[4] is not None:
                if stored[5] == "yielded":
                    raise FactConflict("A yielded execution claim cannot append owner facts")
                if fact_type not in {"response", "timeout"}:
                    raise FactConflict(
                        "A closed execution claim may append only late response or timeout evidence"
                    )
        self._lock_effect_cursor(cursor, effect_id)
        cursor.execute("SELECT command_id FROM effect_intent WHERE effect_id = %s", (effect_id,))
        row = cursor.fetchone()
        if row is None:
            raise FactStoreError(f"The effect {effect_id!r} is not registered")
        if expected_command_id is not None and row[0] != expected_command_id:
            raise FactConflict("The effect fact names another command")
        if claim is not None:
            cursor.execute(
                """
                SELECT 1
                FROM execution_claim
                WHERE command_id = %s
                  AND closure_code = 'yielded'
                  AND yield_effect_id = %s
                LIMIT 1
                """,
                (claim.command_id, effect_id),
            )
            if (
                cursor.fetchone() is not None
                and not (
                    claim.scope == "transfer_resume"
                    and fact_type in {"resume_authorized", "response", "timeout"}
                )
            ):
                raise FactConflict(
                    "A yielded effect accepts only claim-free reconciliation or a scoped resume authorization"
                )
        if fact_type in {"dispatch_authorized", "resume_authorized", "response", "timeout"} and claim is None:
            raise FactConflict(f"{fact_type} requires its execution claim")
        if fact_type == "resume_authorized":
            intent, facts = self._effect_cursor(cursor, effect_id, lock=False)
            settlement = project_effect_settlement(intent, facts)
            if (
                settlement.state is not EffectState.UNKNOWN
                or settlement.resume_phase is not EffectResumePhase.INPUT_TRANSFER_PENDING
            ):
                raise FactConflict(
                    "A resume authorization requires the exact input transfer pending phase"
                )
        if fact_type in {"response", "timeout"}:
            cursor.execute(
                """
                SELECT 1
                FROM effect_fact
                WHERE effect_id = %s
                  AND fact_type IN ('dispatch_authorized', 'resume_authorized')
                  AND claim_id = %s
                  AND fence = %s
                LIMIT 1
                """,
                (effect_id, claim.claim_id, claim.fence),
            )
            if cursor.fetchone() is None:
                raise FactConflict("The response/timeout has no matching dispatch authorization")
        if fact_type in {"observation", "reconciled"}:
            intent, facts = self._effect_cursor(cursor, effect_id, lock=True)
            state = project_effect_state(intent, facts)
            conflict_resolution = (
                state is EffectState.CONFLICT
                and fact_type == "reconciled"
                and state_class in {"known", "absent"}
                and metadata_artifact is not None
                and metadata_artifact.kind == "effect_reconciliation_metadata"
            )
            conflict_audit = state is EffectState.CONFLICT and fact_type == "observation"
            if state in {
                EffectState.SATISFIED,
                EffectState.DEFINITELY_FAILED,
                EffectState.CONFLICT,
            } and not conflict_resolution and not conflict_audit:
                raise FactConflict(
                    "An observation or reconciliation cannot reinterpret a terminal effect"
                )
        if metadata_artifact is not None and metadata_artifact.kind != "effect_reconciliation_metadata":
            raise FactStoreError("The effect metadata artifact has the wrong kind")
        artifacts = tuple(
            artifact
            for artifact in (payload_artifact, metadata_artifact)
            if artifact is not None
        )
        self._insert_artifacts_cursor(cursor, artifacts)
        cursor.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM effect_fact WHERE effect_id = %s",
            (effect_id,),
        )
        seq = cursor.fetchone()[0]
        fact = EffectFact(
            fact_id=fact_id,
            effect_id=effect_id,
            seq=seq,
            claim_id=None if claim is None else claim.claim_id,
            fence=None if claim is None else claim.fence,
            fact_type=fact_type,
            source=source,
            state_class=state_class,
            payload_artifact_id=None if payload_artifact is None else payload_artifact.artifact_id,
            payload_digest=payload_digest,
            observed_at=observed_at,
            metadata_artifact_id=None if metadata_artifact is None else metadata_artifact.artifact_id,
            resume_phase=resume_phase,
        )
        cursor.execute(
            """
            INSERT INTO effect_fact (
                fact_id, effect_id, seq, claim_id, fence, fact_type, source,
                state_class, payload_artifact_id, payload_digest, metadata_artifact_id,
                observed_at, resume_phase
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                fact.fact_id,
                fact.effect_id,
                fact.seq,
                fact.claim_id,
                fact.fence,
                fact.fact_type,
                fact.source,
                fact.state_class,
                fact.payload_artifact_id,
                fact.payload_digest,
                fact.metadata_artifact_id,
                fact.observed_at,
                None if fact.resume_phase is None else fact.resume_phase.value,
            ),
        )
        return fact

    def authorize_effect_dispatch(
        self,
        *,
        effect_id: str,
        claim: ExecutionClaim,
        source: str,
        payload_digest: str,
        observed_at: datetime,
        fact_id: str,
        expected_command_id: str | None = None,
    ) -> EffectFact:
        """Persist the only permission that may precede the external call.

        Locking and validating the completed ancestor effect ledgers in this
        transaction is the external-send linearization point. A later conflict
        remains immutable evidence and invalidates subsequent qualifications,
        but it cannot rewrite an authorization or an already reported effect.
        """

        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise FactStoreError("Effect dispatch time must be timezone-aware")
        self._require_execution_claim(claim, "Effect dispatch")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT command_id, run_id, kind, stage, predecessor_decision_id "
                "FROM command WHERE command_id = %s",
                (claim.command_id,),
            )
            stored = cursor.fetchone()
            if stored is None:
                raise FactStoreError("The effect dispatch command disappeared")
            self._lock_run_cursor(cursor, stored[1])
            if stored[2] == "stage":
                stored_command = self._load_command_cursor(cursor, claim.command_id)
                if stored_command is None:
                    raise FactStoreError("The effect dispatch command disappeared")
                self._lock_command_chain_cursor(cursor, stored_command)
                if stored[4] is not None:
                    self._verify_completed_predecessor_chain_cursor(cursor, stored_command)
                if self._cancel_command_cursor(cursor, claim.command_id) is not None:
                    raise FactConflict("Cancellation was requested before effect dispatch")
            else:
                self._lock_command_cursor(cursor, claim.command_id)
                if (
                    stored[2] == "dispatch"
                    and self._load_dispatch_command_cursor(cursor, claim.command_id) is None
                ):
                    raise FactStoreError("The effect dispatch command disappeared")
            self._lock_effect_ledger_cursor(cursor, claim.command_id)
            self._verify_open_claim_cursor(cursor, claim)
            cursor.execute(
                "SELECT command_id FROM effect_intent WHERE effect_id = %s",
                (effect_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise FactStoreError(f"The effect {effect_id!r} is not registered")
            if row[0] != claim.command_id:
                raise FactConflict("The effect dispatch claim names another command")
            cursor.execute(
                "SELECT expires_at FROM execution_claim WHERE claim_id = %s FOR UPDATE",
                (claim.claim_id,),
            )
            expiry = cursor.fetchone()
            if expiry is None or observed_at >= expiry[0]:
                raise FactConflict("Lease expiry does not authorize an external dispatch")
            state = self._effect_state_cursor(cursor, effect_id)
            if state is not EffectState.PREPARED:
                raise FactConflict(f"The effect is {state.value}; observe or reconcile it before another dispatch")
            return self._append_effect_fact_cursor(
                cursor,
                effect_id=effect_id,
                claim=claim,
                fact_type="dispatch_authorized",
                source=source,
                state_class=None,
                payload_artifact=None,
                payload_digest=payload_digest,
                observed_at=observed_at,
                fact_id=fact_id,
                expected_command_id=expected_command_id,
            )

    def authorize_effect_resume(
        self,
        *,
        effect_id: str,
        claim: ExecutionClaim,
        source: str,
        payload_digest: str,
        observed_at: datetime,
        fact_id: str,
        expected_command_id: str | None = None,
    ) -> EffectFact:
        """Authorize one exact continuation of a transfer-pending effect."""

        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise FactStoreError("Effect resume time must be timezone-aware")
        self._require_transfer_resume_claim(
            claim,
            effect_id,
            "Effect resume",
        )
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT command_id FROM effect_intent WHERE effect_id = %s",
                (effect_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise FactStoreError(f"The effect {effect_id!r} is not registered")
            if row[0] != claim.command_id:
                raise FactConflict("The effect resume claim names another command")
            self._lock_command_scope_cursor(cursor, claim.command_id)
            command = self._load_command_cursor(cursor, claim.command_id)
            if command is None:
                raise FactStoreError("The effect resume command disappeared")
            if command.predecessor_decision_id is not None:
                self._verify_completed_predecessor_chain_cursor(cursor, command)
            self._lock_effect_ledger_cursor(cursor, claim.command_id)
            self._verify_open_claim_cursor(cursor, claim)
            self._lock_effect_cursor(cursor, effect_id)
            intent, facts = self._effect_cursor(cursor, effect_id, lock=False)
            if intent.effect_kind != "native_operation":
                raise FactConflict(
                    "Input transfer resume is limited to native operation effects"
                )
            require_native_transfer_target(intent.target)
            settlement = project_effect_settlement(intent, facts)
            if (
                settlement.state is not EffectState.UNKNOWN
                or settlement.resume_phase is not EffectResumePhase.INPUT_TRANSFER_PENDING
            ):
                raise FactConflict(
                    "The effect is not authorized for input transfer resume"
                )
            outbound = self._artifact_cursor(cursor, intent.outbound_artifact_id)
            if outbound.set_sha256 != intent.outbound_digest:
                raise FactConflict("The resumable outbound bytes differ from the effect intent")
            verifier = getattr(self._result_reader, "verify", None)
            if not callable(verifier):
                raise FactStoreError("The configured artifact reader cannot verify resumable outbound bytes")
            verifier(outbound)
            cursor.execute(
                "SELECT expires_at FROM execution_claim WHERE claim_id = %s FOR UPDATE",
                (claim.claim_id,),
            )
            expiry = cursor.fetchone()
            if expiry is None or observed_at >= expiry[0]:
                raise FactConflict(
                    "Lease expiry does not authorize an input transfer resume"
                )
            if self._cancel_command_cursor(cursor, claim.command_id) is not None:
                raise FactConflict("Cancellation was requested before effect resume")
            for fact in facts:
                if (
                    fact.fact_type == "resume_authorized"
                    and fact.claim_id == claim.claim_id
                    and fact.fence == claim.fence
                ):
                    return fact
            return self._append_effect_fact_cursor(
                cursor,
                effect_id=effect_id,
                claim=claim,
                fact_type="resume_authorized",
                source=source,
                state_class=None,
                payload_artifact=None,
                payload_digest=payload_digest,
                observed_at=observed_at,
                fact_id=fact_id,
                expected_command_id=expected_command_id,
            )

    def register_effect_intent(
        self,
        intent: EffectIntent,
        *,
        claim: ExecutionClaim,
        outbound_artifact: ArtifactRef,
    ) -> EffectIntent:
        if intent.command_id != claim.command_id:
            raise FactConflict("The effect intent names another command than the claim")
        self._require_execution_claim(claim, "Effect intent registration")
        with self._transaction() as connection:
            cursor = connection.cursor()
            command = self._load_command_cursor(cursor, intent.command_id)
            if command is None:
                raise FactStoreError("The effect intent command is missing")
            self._lock_command_chain_cursor(cursor, command)
            self._lock_effect_ledger_cursor(cursor, intent.command_id)
            self._verify_open_claim_cursor(cursor, claim)
            return self._insert_effect_intent_cursor(cursor, intent, outbound_artifact)

    def _insert_effect_intent_cursor(
        self,
        cursor,
        intent: EffectIntent,
        outbound_artifact: ArtifactRef,
    ) -> EffectIntent:
        if outbound_artifact.artifact_id != intent.outbound_artifact_id:
            raise FactStoreError("The outbound artifact does not match the effect intent")
        self._insert_artifact(cursor, outbound_artifact)
        cursor.execute(
            """
            INSERT INTO effect_intent (
                effect_id, command_id, effect_key, effect_kind, target, expected_state,
                expected_state_digest, outbound_artifact_id, outbound_digest, created_at
            ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING effect_id
            """,
            (
                intent.effect_id,
                intent.command_id,
                intent.effect_key,
                intent.effect_kind,
                _json(intent.target),
                _json(intent.expected_state),
                intent.expected_state_digest,
                intent.outbound_artifact_id,
                intent.outbound_digest,
                intent.created_at,
            ),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                """
                SELECT effect_id FROM effect_intent
                WHERE effect_id = %s OR (command_id = %s AND effect_key = %s)
                """,
                (intent.effect_id, intent.command_id, intent.effect_key),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise FactStoreError("The effect intent conflicted without an existing row")
            stored_intent, _ = self._effect_cursor(cursor, existing[0])
            if stored_intent.identity_digest != intent.identity_digest:
                raise FactConflict("The command/effect key is bound to a different effect intent")
            return stored_intent
        return intent

    def _verify_open_claim_cursor(self, cursor, claim: ExecutionClaim) -> None:
        cursor.execute(
            """
            SELECT command_id, fence, owner_sha256, executor_sha256, closed_at,
                   scope, effect_scope_id
            FROM execution_claim
            WHERE claim_id = %s
            FOR UPDATE
            """,
            (claim.claim_id,),
        )
        row = cursor.fetchone()
        if (
            row is None
            or row[0] != claim.command_id
            or row[1] != claim.fence
            or row[2] != claim.owner_sha256
            or row[3] != claim.executor_sha256
            or row[5] != claim.scope
            or row[6] != claim.effect_scope_id
        ):
            raise FactConflict("The execution claim identity is stale or foreign")
        if row[4] is not None:
            raise FactConflict("The execution claim is already closed")

    @staticmethod
    def _require_execution_claim(claim: ExecutionClaim, operation: str) -> None:
        if claim.scope != "execution" or claim.effect_scope_id is not None:
            raise FactConflict(f"{operation} requires a general execution claim")

    @staticmethod
    def _require_transfer_resume_claim(
        claim: ExecutionClaim,
        effect_id: str,
        operation: str,
    ) -> None:
        if (
            claim.scope != "transfer_resume"
            or claim.effect_scope_id != effect_id
        ):
            raise FactConflict(
                f"{operation} requires its matching restricted transfer claim"
            )

    def close_open_claim(
        self,
        claim: ExecutionClaim,
        *,
        closure_code: str,
        closed_at: datetime,
    ) -> None:
        if not isinstance(claim, ExecutionClaim):
            raise FactStoreError(
                "A canonical execution claim is required for closure"
            )
        if closure_code not in {"completed", "definite_failure"}:
            raise FactStoreError("A technical command closes only as completed or definite_failure")
        if not isinstance(closed_at, datetime) or closed_at.tzinfo is None:
            raise FactStoreError("Claim closure time must be timezone-aware")
        self._require_execution_claim(claim, "General claim closure")
        with self._transaction() as connection:
            cursor = connection.cursor()
            command = self._lock_command_scope_cursor(
                cursor,
                claim.command_id,
            )
            if (
                command[1] == "dispatch"
                and self._load_dispatch_command_cursor(
                    cursor,
                    claim.command_id,
                )
                is None
            ):
                raise FactStoreError(
                    "The execution claim command is missing"
                )
            self._lock_effect_ledger_cursor(
                cursor,
                claim.command_id,
            )
            self._verify_open_claim_cursor(cursor, claim)
            cursor.execute(
                """
                UPDATE execution_claim
                SET closed_at = %s, closure_code = %s, last_seen_at = %s
                WHERE claim_id = %s
                  AND command_id = %s
                  AND fence = %s
                  AND owner_sha256 = %s
                  AND executor_sha256 = %s
                  AND closed_at IS NULL
                RETURNING claim_id
                """,
                (
                    closed_at,
                    closure_code,
                    closed_at,
                    claim.claim_id,
                    claim.command_id,
                    claim.fence,
                    claim.owner_sha256,
                    claim.executor_sha256,
                ),
            )
            closed_rows = cursor.fetchall()
            if len(closed_rows) != 1:
                raise FactConflict(
                    "The execution claim changed before closure"
                )

    def append_effect_fact(
        self,
        *,
        effect_id: str,
        claim: ExecutionClaim | None,
        fact_type: str,
        source: str,
        state_class: str | None,
        payload_artifact: ArtifactRef | None,
        payload_digest: str,
        observed_at: datetime,
        fact_id: str,
        expected_command_id: str | None = None,
        metadata_artifact: ArtifactRef | None = None,
    ) -> EffectFact:
        if metadata_artifact is not None:
            raise FactStoreError("Reconciliation metadata must use append_reconciliation_fact")
        with self._transaction() as connection:
            return self._append_effect_fact_cursor(
                connection.cursor(),
                effect_id=effect_id,
                claim=claim,
                fact_type=fact_type,
                source=source,
                state_class=state_class,
                payload_artifact=payload_artifact,
                payload_digest=payload_digest,
                observed_at=observed_at,
                fact_id=fact_id,
                expected_command_id=expected_command_id,
                metadata_artifact=metadata_artifact,
            )

    def append_reconciliation_fact(
        self,
        *,
        effect_id: str,
        fact_type: str,
        source: str,
        state_class: str,
        payload_artifact: ArtifactRef,
        metadata_artifact: ArtifactRef,
        payload_digest: str,
        observed_at: datetime,
        fact_id: str,
        expected_command_id: str,
        resume_phase: EffectResumePhase | None = None,
        resume_target_digest: str | None = None,
    ) -> EffectFact:
        """Append read-only reconciliation evidence without granting a claim."""

        if fact_type not in {"observation", "reconciled"}:
            raise FactStoreError("A reconciliation fact must be observation or reconciled")
        if metadata_artifact.kind != "effect_reconciliation_metadata":
            raise FactStoreError("The reconciliation metadata artifact has the wrong kind")
        if payload_digest != payload_artifact.set_sha256:
            raise FactStoreError("The reconciliation payload digest differs from its artifact closure")
        verifier = getattr(self._result_reader, "verify", None)
        if not callable(verifier):
            raise FactStoreError("The configured artifact reader cannot verify reconciliation evidence")
        verifier(payload_artifact)
        with self._transaction() as connection:
            cursor = connection.cursor()
            intent, facts = self._effect_cursor(cursor, effect_id, lock=True)
            if intent.command_id != expected_command_id:
                raise FactConflict("The reconciliation effect names another command")
            if resume_phase is not None and intent.effect_kind != "native_operation":
                raise FactConflict(
                    "Input transfer resume is limited to native operation effects"
                )
            if resume_phase is not None:
                require_native_transfer_target(intent.target)
            command = self._load_command_cursor(cursor, intent.command_id)
            if command is None:
                raise FactStoreError("The reconciliation command is missing")
            outbound_artifact = self._artifact_cursor(cursor, intent.outbound_artifact_id)
            from ..pipeline.reconciliation import read_reconciliation_metadata

            record = read_reconciliation_metadata(
                self._result_reader,
                metadata_artifact,
                command=command,
                intent=intent,
                fact_type=fact_type,
                state_class=state_class,
                outbound_artifact=outbound_artifact,
                payload_artifact=payload_artifact,
                observed_at=observed_at,
                resume_phase=resume_phase,
                resume_target_digest=resume_target_digest,
            )
            if (
                resume_phase is not None
                and resume_target_digest != native_transfer_target_digest(intent.target)
            ):
                raise FactConflict("The transfer target differs from the registered effect intent")
            if source != "effect-reconciler:" + record["observer_tool_sha256"]:
                raise FactConflict("The reconciliation source differs from its metadata observer")
            state = project_effect_state(intent, facts)
            if state is EffectState.CONFLICT:
                if fact_type != "reconciled" or state_class not in {"known", "absent"}:
                    raise FactConflict("A conflict requires exact known or absent reconciliation evidence")
            elif state is not EffectState.UNKNOWN:
                raise FactConflict(f"The effect is {state.value}; read-only reconciliation is not applicable")
            return self._append_effect_fact_cursor(
                cursor,
                effect_id=effect_id,
                claim=None,
                fact_type=fact_type,
                source=source,
                state_class=state_class,
                payload_artifact=payload_artifact,
                payload_digest=payload_digest,
                observed_at=observed_at,
                fact_id=fact_id,
                expected_command_id=expected_command_id,
                metadata_artifact=metadata_artifact,
                resume_phase=resume_phase,
            )

    def register_candidate(
        self,
        *,
        candidate_id: str,
        claim: ExecutionClaim,
        output_artifact: ArtifactRef,
        result_record_artifact: ArtifactRef,
        result: StageResult,
        created_at: datetime,
    ) -> StageCandidate:
        self._require_execution_claim(claim, "Candidate registration")
        if result.command_id != claim.command_id:
            raise FactConflict("The StageResult names another command than the claim")
        command = self.load_command(result.command_id)
        if result.diagnostic_scope != command.diagnostic_scope:
            raise FactConflict("The StageResult diagnostic scope differs from its command")
        if result.output_artifact != output_artifact:
            raise FactStoreError("The StageResult does not carry its declared output artifact")
        if result_record_artifact.kind != "stage_result":
            raise FactStoreError("The result record artifact has the wrong kind")
        if result.actual_executor_sha256 != claim.executor_sha256:
            raise FactConflict("The candidate executor differs from the claim executor")
        with self._transaction() as connection:
            cursor = connection.cursor()
            self._lock_command_chain_cursor(cursor, command)
            self._lock_effect_ledger_cursor(cursor, command.command_id)
            self._verify_open_claim_cursor(cursor, claim)
            self._insert_artifacts_cursor(
                cursor,
                (output_artifact, result_record_artifact),
            )
            cursor.execute(
                """
                INSERT INTO stage_candidate (
                    candidate_id, command_id, claim_id, fence, output_artifact_id,
                    result_record_artifact_id, result_digest, outcome, reason_code, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING candidate_id
                """,
                (
                    candidate_id,
                    result.command_id,
                    claim.claim_id,
                    claim.fence,
                    output_artifact.artifact_id,
                    result_record_artifact.artifact_id,
                    result.digest,
                    result.outcome,
                    result.reason_code,
                    created_at,
                ),
            )
            if cursor.fetchone() is None:
                stored = self._candidate_cursor(cursor, result.command_id)
                if stored is None or stored.result != result:
                    raise FactConflict("The command is already bound to a different sealed candidate")
                return stored
            return StageCandidate(
                result=result,
                result_record_artifact=result_record_artifact,
                candidate_id=candidate_id,
                claim_id=claim.claim_id,
                fence=claim.fence,
            )

    def _candidate_cursor(self, cursor, command_id: str) -> StageCandidate | None:
        cursor.execute(
            """
            SELECT candidate_id, claim_id, fence, output_artifact_id, result_record_artifact_id
            FROM stage_candidate
            WHERE command_id = %s
            """,
            (command_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        output_artifact = self._artifact_cursor(cursor, row[3])
        result_record_artifact = self._artifact_cursor(cursor, row[4])
        result = self._result_reader.read_result(result_record_artifact)
        if result.output_artifact != output_artifact:
            raise FactStoreError("The candidate output artifact differs from its StageResult")
        return StageCandidate(
            result=result,
            result_record_artifact=result_record_artifact,
            candidate_id=row[0],
            claim_id=row[1],
            fence=row[2],
        )

    def candidate_for(self, command: StageCommand) -> StageCandidate | None:
        with self._transaction() as connection:
            return self._candidate_cursor(connection.cursor(), command.command_id)

    def commit_decision(
        self,
        *,
        decision_id: str,
        command: StageCommand,
        claim: ExecutionClaim,
        candidate: StageCandidate,
        decided_at: datetime,
        original_producer_decision_id: str | None = None,
    ) -> StageDecision:
        if command.stage is None:
            raise FactStoreError("Only a stage command can commit a decision")
        self._require_execution_claim(claim, "Stage decision commit")
        if candidate.candidate_id is None or candidate.claim_id is None or candidate.fence is None:
            raise FactStoreError("Only a persisted candidate can be committed")
        if (
            candidate.result.command_id != command.command_id
            or candidate.result.run_id != command.run_id
            or candidate.result.stage != command.stage
            or candidate.result.request_digest != command.request_digest
            or candidate.result.diagnostic_scope != command.diagnostic_scope
        ):
            raise FactStoreError("The candidate does not belong to the command")
        if candidate.result.actual_executor_sha256 != claim.executor_sha256:
            raise FactConflict("The StageResult executor differs from the claim executor")
        if candidate.result.actual_executor_sha256 != command.expected_executor_sha256:
            raise FactConflict("The actual executor differs from the command expectation")
        with self._transaction() as connection:
            cursor = connection.cursor()
            self._lock_command_chain_cursor(cursor, command)
            cursor.execute("SELECT command_id FROM command WHERE command_id = %s", (command.command_id,))
            if cursor.fetchone() is None:
                raise FactStoreError("The command disappeared before decision commit")
            stored_command = self._load_command_cursor(cursor, command.command_id)
            if stored_command != command:
                raise FactConflict("The decision command differs from its registered command")
            self._verify_completed_predecessor_chain_cursor(cursor, command)
            self._lock_effect_ledger_cursor(cursor, command.command_id)
            if self._cancel_command_cursor(cursor, command.command_id, lock=True) is not None:
                raise FactConflict("Cancellation was requested; commit the cancelled result instead")
            existing_row = self._decision_row(cursor, run_id=command.run_id, stage=command.stage)
            if existing_row is not None:
                existing = self._decision_from_row(cursor, existing_row)
                if (
                    existing.command_id != command.command_id
                    or existing.result_digest != candidate.result.digest
                    or existing.outcome != candidate.result.outcome
                ):
                    raise FactConflict("The command is already bound to a different decision")
                existing_states = self._effect_states_for_command_cursor(
                    cursor,
                    command.command_id,
                    lock=True,
                )
                if set(candidate.result.required_effect_ids) != set(existing_states):
                    raise FactConflict(
                        "The completed decision effect ids differ from the current ledger"
                    )
                if candidate.result.outcome == "completed" and any(
                    state is not EffectState.SATISFIED
                    for state in existing_states.values()
                ):
                    raise FactConflict(
                        "The completed decision now conflicts with its effect ledger"
                    )
                if candidate.result.outcome != "completed" and any(
                    state in {EffectState.UNKNOWN, EffectState.CONFLICT}
                    for state in existing_states.values()
                ):
                    raise FactConflict(
                        "The definite decision now conflicts with its effect ledger"
                    )
                return existing
            self._verify_open_claim_cursor(cursor, claim)
            states = self._effect_states_for_command_cursor(
                cursor,
                command.command_id,
                lock=True,
            )
            declared = set(candidate.result.required_effect_ids)
            if declared != set(states):
                raise FactConflict(
                    "The candidate effect ids do not exactly match the command ledger: "
                    f"declared={sorted(declared)} ledger={sorted(states)}"
                )
            for effect_id, state in states.items():
                if candidate.result.outcome == "completed":
                    if state is not EffectState.SATISFIED:
                        raise FactConflict(f"A completed decision requires effect {effect_id!r} to be satisfied")
                elif state in {EffectState.UNKNOWN, EffectState.CONFLICT}:
                    raise FactConflict(f"A definite failure cannot hide the unresolved external effect {effect_id!r}")
            cursor.execute(
                """
                SELECT candidate_id, claim_id, fence, result_digest,
                       output_artifact_id, result_record_artifact_id
                FROM stage_candidate
                WHERE command_id = %s
                """,
                (command.command_id,),
            )
            stored = cursor.fetchone()
            if stored != (
                candidate.candidate_id,
                candidate.claim_id,
                candidate.fence,
                candidate.result.digest,
                candidate.result.output_artifact.artifact_id,
                candidate.result_record_artifact.artifact_id,
            ):
                raise FactConflict("The stage candidate changed before its decision commit")
            cursor.execute(
                """
                INSERT INTO stage_decision (
                    decision_id, run_id, stage, command_id, outcome, result_record_artifact_id,
                    result_digest, actual_executor_sha256, original_producer_decision_id,
                    reason_code, failure_envelope, failure_digest, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s)
                """,
                (
                    decision_id,
                    command.run_id,
                    command.stage,
                    command.command_id,
                    candidate.result.outcome,
                    candidate.result_record_artifact.artifact_id,
                    candidate.result.digest,
                    candidate.result.actual_executor_sha256,
                    original_producer_decision_id,
                    candidate.result.reason_code,
                    decided_at,
                ),
            )
            closure = "completed" if candidate.result.outcome == "completed" else "definite_failure"
            cursor.execute(
                """
                UPDATE execution_claim
                SET closed_at = %s, closure_code = %s, last_seen_at = %s
                WHERE claim_id = %s AND closed_at IS NULL
                """,
                (decided_at, closure, decided_at, claim.claim_id),
            )
            row = self._decision_row(cursor, run_id=command.run_id, stage=command.stage)
            if row is None:  # pragma: no cover - inserted in this transaction
                raise FactStoreError("The committed decision disappeared")
            return self._decision_from_row(cursor, row)

    def commit_failure_envelope(
        self,
        *,
        decision_id: str,
        command: StageCommand,
        claim: ExecutionClaim,
        failure: FailureEnvelope,
    ) -> FailureEnvelope:
        """Commit a minimal failure without depending on diagnostic artifact sealing."""

        if command.stage is None:
            raise FactStoreError("Only a stage command can commit a failure envelope")
        self._require_execution_claim(claim, "Failure envelope commit")
        if (
            failure.command_id != command.command_id
            or failure.run_id != command.run_id
            or failure.stage != command.stage
            or failure.request_digest != command.request_digest
            or failure.diagnostic_scope != command.diagnostic_scope
        ):
            raise FactConflict("The failure envelope names another command")
        if failure.actual_executor_sha256 != claim.executor_sha256:
            raise FactConflict("The failure executor differs from the claim executor")
        if failure.actual_executor_sha256 != command.expected_executor_sha256:
            raise FactConflict("The failure executor differs from the command expectation")
        with self._transaction() as connection:
            cursor = connection.cursor()
            self._lock_command_chain_cursor(cursor, command)
            cursor.execute(
                "SELECT command_id FROM command WHERE command_id = %s",
                (command.command_id,),
            )
            if cursor.fetchone() is None:
                raise FactStoreError("The command disappeared before failure commit")
            self._verify_completed_predecessor_chain_cursor(cursor, command)
            self._lock_effect_ledger_cursor(cursor, command.command_id)
            if (
                failure.outcome != "cancelled"
                and self._cancel_command_cursor(cursor, command.command_id, lock=True) is not None
            ):
                raise FactConflict("Cancellation was requested; commit the cancelled result instead")
            existing_row = self._decision_row(cursor, run_id=command.run_id, stage=command.stage)
            if existing_row is not None:
                existing = self._decision_from_row(cursor, existing_row)
                existing_states = self._effect_states_for_command_cursor(
                    cursor,
                    command.command_id,
                    lock=True,
                )
                declared_existing = {
                    effect_id: state.value
                    for effect_id, state in existing_states.items()
                }
                if (
                    existing.command_id == command.command_id
                    and existing.outcome == failure.outcome
                    and existing.failure_envelope == failure
                    and dict(failure.effect_states) == declared_existing
                    and not any(
                        state in {EffectState.UNKNOWN, EffectState.CONFLICT}
                        for state in existing_states.values()
                    )
                ):
                    return failure
                raise FactConflict("The command is already bound to a different decision")
            self._verify_open_claim_cursor(cursor, claim)
            states = self._effect_states_for_command_cursor(cursor, command.command_id, lock=True)
            declared = dict(failure.effect_states)
            if declared != {effect_id: state.value for effect_id, state in states.items()}:
                raise FactConflict("The failure effect states do not exactly match the command ledger")
            if any(state in {EffectState.UNKNOWN, EffectState.CONFLICT} for state in states.values()):
                raise FactConflict("A definite failure cannot hide an unknown or conflicting effect")
            self._insert_artifacts_cursor(cursor, failure.evidence_artifacts)
            cursor.execute(
                """
                INSERT INTO stage_decision (
                    decision_id, run_id, stage, command_id, outcome, result_record_artifact_id,
                    result_digest, actual_executor_sha256, original_producer_decision_id,
                    reason_code, failure_envelope, failure_digest, decided_at
                ) VALUES (%s, %s, %s, %s, %s, NULL, NULL, %s, NULL, %s, %s::jsonb, %s, %s)
                """,
                (
                    decision_id,
                    command.run_id,
                    command.stage,
                    command.command_id,
                    failure.outcome,
                    failure.actual_executor_sha256,
                    failure.reason_code,
                    _json(failure.to_record()),
                    digest(failure.to_record()),
                    failure.occurred_at,
                ),
            )
            cursor.execute(
                """
                UPDATE execution_claim
                SET closed_at = %s, closure_code = 'definite_failure', last_seen_at = %s
                WHERE claim_id = %s AND closed_at IS NULL
                """,
                (failure.occurred_at, failure.occurred_at, claim.claim_id),
            )
            return failure
