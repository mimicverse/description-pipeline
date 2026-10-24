"""PostgreSQL persistence for the canonical six-stage facts.

Every public mutation is one short transaction. Artifact bytes and external
effects are produced outside these methods; this module only commits the
already verified identities and observations.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from ..io import PipelineError, digest
from ..pipeline.admission import (
    AdmissionSpec,
    VerifiedDiscoverResult,
    VerifiedPredecessor,
    validate_store_command,
)
from ..pipeline.contracts import (
    ArtifactRef,
    DispatchCommand,
    DispatchConf,
    ExecutionClaim,
    FailureEnvelope,
    ProtocolError,
    RunAdmission,
    StageCandidate,
    StageCommand,
    StageDecision,
    StageResult,
)
from ..pipeline.effects import (
    EffectFact,
    EffectIntent,
    EffectSettlement,
    EffectState,
    project_effect_settlement,
    project_effect_state,
)


class FactStoreError(PipelineError):
    """A canonical platform fact cannot be read or committed safely."""


class FactConflict(FactStoreError):
    """A canonical key is already bound to a different immutable value."""


class StageResultReader(Protocol):
    """Read and independently validate one sealed StageResult artifact."""

    def verify(self, artifact: ArtifactRef) -> Mapping[str, object]: ...

    def read_result(self, artifact: ArtifactRef) -> StageResult: ...

    def read_dispatch_conf(self, artifact: ArtifactRef) -> DispatchConf: ...


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


class FactStore:
    """Canonical fact persistence with injected connection and result reader."""

    def __init__(self, connect, result_reader: StageResultReader) -> None:
        if not callable(connect):
            raise FactStoreError("A database connection factory is required")
        if result_reader is None or not callable(getattr(result_reader, "read_result", None)):
            raise FactStoreError("A StageResult artifact reader is required")
        self._connect = connect
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
        return RunAdmission(
            schema_version="description-pipeline.run-admission/v1",
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
            "schema_version": "description-pipeline.airflow-dispatch-effect/v1",
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
        return StageCommand(
            schema_version="description-pipeline.stage-command/v1",
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
        for artifact in command.input_artifacts:
            self._insert_artifact(cursor, artifact)
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
                _json(command.request),
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
            cursor.execute("SELECT run_id FROM run_admission WHERE run_id = %s FOR UPDATE", (command.run_id,))
            if cursor.fetchone() is None:
                raise FactStoreError(f"The run {command.run_id!r} is not admitted")
            stored_admission = self._load_admission_cursor(cursor, command.run_id)
            if stored_admission != admission:
                raise FactConflict("The supplied admission differs from the registered admission")
            for verified in predecessors:
                self._verify_registered_predecessor_cursor(cursor, verified)
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

    def _verify_registered_predecessor_cursor(self, cursor, predecessor: VerifiedPredecessor) -> None:
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
            row = self._decision_row(cursor, run_id=command.run_id, stage=command.stage)
            return None if row is None else self._decision_from_row(cursor, row)

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
    ) -> ExecutionClaim:
        """Acquire one open fenced execution right without treating lease expiry as success."""

        if not isinstance(now, datetime) or now.tzinfo is None:
            raise FactStoreError("Claim time must be timezone-aware")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at <= now:
            raise FactStoreError("Claim expiry must be after a timezone-aware acquisition time")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT command_id, run_id, stage, expected_executor_sha256
                FROM command
                WHERE command_id = %s
                FOR UPDATE
                """,
                (command_id,),
            )
            command = cursor.fetchone()
            if command is None:
                raise FactStoreError(f"The command {command_id!r} is not registered")
            if command[3] != executor_sha256:
                raise FactConflict("The claim executor identity differs from the command expectation")
            if command[2] is not None and self._decision_row(cursor, run_id=command[1], stage=command[2]) is not None:
                raise FactConflict("A decided command cannot acquire a new execution claim")
            cursor.execute(
                """
                SELECT claim_id, command_id, fence, owner_sha256, executor_sha256, lane_key, expires_at
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
                )
                if claim.expires_at > now:
                    raise FactConflict("Another live executor owns the command")
                if self._command_has_unresolved_effect(cursor, command_id):
                    raise FactConflict("An expired claim carries an unresolved effect and requires reconciliation")
                cursor.execute(
                    """
                    UPDATE execution_claim
                    SET closed_at = %s, closure_code = 'abandoned_before_effect'
                    WHERE claim_id = %s AND closed_at IS NULL
                    """,
                    (now, claim.claim_id),
                )
            elif self._command_has_unresolved_effect(cursor, command_id):
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
                    lane_key,
                    dag_id, dag_run_id, task_id, map_index,
                    issued_at, last_seen_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    claim_id,
                    command_id,
                    fence,
                    owner_sha256,
                    executor_sha256,
                    lane_key,
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
            )

    def _command_has_unresolved_effect(self, cursor, command_id: str) -> bool:
        cursor.execute("SELECT effect_id FROM effect_intent WHERE command_id = %s", (command_id,))
        for (effect_id,) in cursor.fetchall():
            state = self._effect_state_cursor(cursor, effect_id)
            if state in {EffectState.UNKNOWN, EffectState.CONFLICT}:
                return True
        return False

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
        lock_sql = " FOR UPDATE" if lock else ""
        cursor.execute(
            """
            SELECT effect_id, command_id, effect_key, effect_kind, target, expected_state,
                   expected_state_digest, outbound_artifact_id, outbound_digest, created_at
            FROM effect_intent
            WHERE effect_id = %s
            """
            + lock_sql,
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
                   state_class, payload_artifact_id, payload_digest, observed_at
            FROM effect_fact
            WHERE effect_id = %s
            ORDER BY seq
            """
            + lock_sql,
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
                observed_at=item[10],
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
        artifact_id = settlement.decisive_fact.payload_artifact_id if settlement.decisive_fact else None
        if artifact_id is None:
            return settlement
        return replace(settlement, payload_artifact=self._artifact_cursor(cursor, artifact_id))

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
            cursor.execute(
                """
                SELECT effect_id
                FROM effect_intent
                WHERE command_id = %s AND effect_key = %s
                FOR UPDATE
                """,
                (command_id, effect_key),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._effect_settlement_cursor(cursor, row[0], expected_command_id=command_id)

    def _effect_states_for_command_cursor(
        self,
        cursor,
        command_id: str,
        *,
        lock: bool = False,
    ) -> dict[str, EffectState]:
        cursor.execute(
            """
            SELECT effect_id
            FROM effect_intent
            WHERE command_id = %s
            ORDER BY effect_id
            """
            + (" FOR UPDATE" if lock else ""),
            (command_id,),
        )
        effect_ids = [row[0] for row in cursor.fetchall()]
        return {
            effect_id: project_effect_state(*self._effect_cursor(cursor, effect_id, lock=lock))
            for effect_id in effect_ids
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
    ) -> EffectFact:
        stored = None
        if claim is not None:
            cursor.execute(
                """
                SELECT command_id, fence, owner_sha256, executor_sha256, closed_at
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
            if fact_type == "dispatch_authorized" and stored[4] is not None:
                raise FactConflict("The dispatch authorization claim is already closed")
        cursor.execute("SELECT command_id FROM effect_intent WHERE effect_id = %s FOR UPDATE", (effect_id,))
        row = cursor.fetchone()
        if row is None:
            raise FactStoreError(f"The effect {effect_id!r} is not registered")
        if expected_command_id is not None and row[0] != expected_command_id:
            raise FactConflict("The effect fact names another command")
        if claim is not None and claim.command_id != row[0]:
            raise FactConflict("The effect fact claim names another command")
        if fact_type in {"dispatch_authorized", "response", "timeout"} and claim is None:
            raise FactConflict(f"{fact_type} requires its execution claim")
        if fact_type in {"response", "timeout"}:
            cursor.execute(
                """
                SELECT 1
                FROM effect_fact
                WHERE effect_id = %s
                  AND fact_type = 'dispatch_authorized'
                  AND claim_id = %s
                  AND fence = %s
                LIMIT 1
                """,
                (effect_id, claim.claim_id, claim.fence),
            )
            if cursor.fetchone() is None:
                raise FactConflict("The response/timeout has no matching dispatch authorization")
        if payload_artifact is not None:
            self._insert_artifact(cursor, payload_artifact)
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
        )
        cursor.execute(
            """
            INSERT INTO effect_fact (
                fact_id, effect_id, seq, claim_id, fence, fact_type, source,
                state_class, payload_artifact_id, payload_digest, observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                fact.observed_at,
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
        """Persist the only permission that may precede the external call."""

        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise FactStoreError("Effect dispatch time must be timezone-aware")
        with self._transaction() as connection:
            cursor = connection.cursor()
            self._verify_open_claim_cursor(cursor, claim)
            cursor.execute("SELECT command_id FROM effect_intent WHERE effect_id = %s FOR UPDATE", (effect_id,))
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

    def register_effect_intent(
        self,
        intent: EffectIntent,
        *,
        claim: ExecutionClaim,
        outbound_artifact: ArtifactRef,
    ) -> EffectIntent:
        if intent.command_id != claim.command_id:
            raise FactConflict("The effect intent names another command than the claim")
        with self._transaction() as connection:
            cursor = connection.cursor()
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
            SELECT command_id, fence, owner_sha256, executor_sha256, closed_at
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
        ):
            raise FactConflict("The execution claim identity is stale or foreign")
        if row[4] is not None:
            raise FactConflict("The execution claim is already closed")

    def close_open_claim(
        self,
        command_id: str,
        *,
        closure_code: str,
        closed_at: datetime,
    ) -> None:
        if closure_code not in {"completed", "definite_failure"}:
            raise FactStoreError("A technical command closes only as completed or definite_failure")
        if not isinstance(closed_at, datetime) or closed_at.tzinfo is None:
            raise FactStoreError("Claim closure time must be timezone-aware")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE execution_claim
                SET closed_at = %s, closure_code = %s, last_seen_at = %s
                WHERE command_id = %s AND closed_at IS NULL
                RETURNING claim_id
                """,
                (closed_at, closure_code, closed_at, command_id),
            )
            cursor.fetchall()

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
    ) -> EffectFact:
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
        if result.command_id != claim.command_id:
            raise FactConflict("The StageResult names another command than the claim")
        if result.output_artifact != output_artifact:
            raise FactStoreError("The StageResult does not carry its declared output artifact")
        if result_record_artifact.kind != "stage_result":
            raise FactStoreError("The result record artifact has the wrong kind")
        if result.actual_executor_sha256 != claim.executor_sha256:
            raise FactConflict("The candidate executor differs from the claim executor")
        with self._transaction() as connection:
            cursor = connection.cursor()
            self._verify_open_claim_cursor(cursor, claim)
            self._insert_artifact(cursor, output_artifact)
            self._insert_artifact(cursor, result_record_artifact)
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
        if candidate.candidate_id is None or candidate.claim_id is None or candidate.fence is None:
            raise FactStoreError("Only a persisted candidate can be committed")
        if (
            candidate.result.command_id != command.command_id
            or candidate.result.run_id != command.run_id
            or candidate.result.stage != command.stage
            or candidate.result.request_digest != command.request_digest
        ):
            raise FactStoreError("The candidate does not belong to the command")
        if candidate.result.actual_executor_sha256 != claim.executor_sha256:
            raise FactConflict("The StageResult executor differs from the claim executor")
        if candidate.result.actual_executor_sha256 != command.expected_executor_sha256:
            raise FactConflict("The actual executor differs from the command expectation")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT command_id FROM command WHERE command_id = %s FOR UPDATE", (command.command_id,))
            if cursor.fetchone() is None:
                raise FactStoreError("The command disappeared before decision commit")
            existing_row = self._decision_row(cursor, run_id=command.run_id, stage=command.stage)
            if existing_row is not None:
                existing = self._decision_from_row(cursor, existing_row)
                if (
                    existing.command_id != command.command_id
                    or existing.result_digest != candidate.result.digest
                    or existing.outcome != candidate.result.outcome
                ):
                    raise FactConflict("The command is already bound to a different decision")
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
                FOR UPDATE
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
        if (
            failure.command_id != command.command_id
            or failure.run_id != command.run_id
            or failure.stage != command.stage
            or failure.request_digest != command.request_digest
        ):
            raise FactConflict("The failure envelope names another command")
        if failure.actual_executor_sha256 != claim.executor_sha256:
            raise FactConflict("The failure executor differs from the claim executor")
        if failure.actual_executor_sha256 != command.expected_executor_sha256:
            raise FactConflict("The failure executor differs from the command expectation")
        with self._transaction() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT command_id FROM command WHERE command_id = %s FOR UPDATE",
                (command.command_id,),
            )
            if cursor.fetchone() is None:
                raise FactStoreError("The command disappeared before failure commit")
            existing_row = self._decision_row(cursor, run_id=command.run_id, stage=command.stage)
            if existing_row is not None:
                existing = self._decision_from_row(cursor, existing_row)
                if (
                    existing.command_id == command.command_id
                    and existing.outcome == failure.outcome
                    and existing.failure_envelope == failure
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
            for artifact in failure.evidence_artifacts:
                self._insert_artifact(cursor, artifact)
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
