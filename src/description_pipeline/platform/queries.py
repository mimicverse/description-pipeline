"""Read-only projections over canonical pipeline facts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..stages import STAGE_IDS
from ..artifacts import ArtifactStore
from ..io import PipelineError
from ..pipeline.contracts import (
    ArtifactRef,
    FailureEnvelope,
    RunAdmission,
    RunStatus,
    StageCommand,
    StageStatus,
    project_run_status,
    project_stage_status,
    qualify_stage,
)
from ..pipeline.effects import EffectFact, EffectIntent, EffectState, project_effect_state
from ..pipeline.diagnostic import DIAGNOSTIC_ADMISSION_SCHEMA, DiagnosticAdmissionSpec
from ..schemas import (
    DIAGNOSTIC_COMMAND_REQUEST_SCHEMA,
    RUN_ADMISSION_SCHEMA,
    STAGE_COMMAND_SCHEMA,
)


def _artifact_ref(row) -> ArtifactRef:
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


def _run_state(
    stages: list[dict[str, Any]],
    *,
    diagnostic_target: str | None = None,
) -> str:
    effect_states = tuple(
        row["state"]
        for stage in stages
        for row in (stage.get("receipt") or {}).get("effect_states", ())
        if isinstance(row, Mapping) and isinstance(row.get("state"), str)
    )
    pending_effect_states = tuple(
        row["state"]
        for stage in stages
        for row in (stage.get("receipt") or {}).get(
            "pending_effect_states",
            (),
        )
        if isinstance(row, Mapping) and isinstance(row.get("state"), str)
    )
    if diagnostic_target is not None:
        target = next((row for row in stages if row.get("stage") == diagnostic_target), None)
        unresolved = list(effect_states)
        for state in pending_effect_states:
            if state == "unknown" and "unknown" in unresolved:
                unresolved.remove(state)
        if "unknown" in unresolved or "conflict" in unresolved:
            return RunStatus.UNRESOLVED.value
        state = target.get("state") if isinstance(target, dict) else None
        if state == StageStatus.COMPLETED.value:
            return RunStatus.SUCCEEDED.value
        if state == StageStatus.CANCELLED.value:
            return RunStatus.CANCELLED.value
        if state in {
            StageStatus.FAILED.value,
            StageStatus.BLOCKED.value,
            StageStatus.UNRESOLVED.value,
        }:
            return (
                RunStatus.UNRESOLVED.value
                if state == StageStatus.UNRESOLVED.value
                else RunStatus.FAILED.value
            )
        return "running"
    outcomes = tuple(
        (
            decision.get("outcome")
            if isinstance((decision := stage.get("decision")), Mapping)
            else None
        )
        for stage in stages
    )
    projected = project_run_status(
        stage_outcomes=outcomes,
        effect_states=effect_states,
        pending_effect_states=pending_effect_states,
    )
    return "running" if projected is RunStatus.INCOMPLETE else projected.value


def _diagnostic_target(request: object) -> str | None:
    """Return the one diagnostic target, or ``None`` for a formal admission."""

    if not isinstance(request, Mapping):
        raise PipelineError("Run admission request must be an object")
    if request.get("schema_version") != DIAGNOSTIC_ADMISSION_SCHEMA:
        return None
    return DiagnosticAdmissionSpec.from_record(request).target_stage


def _stage_command(
    row: tuple,
    *,
    input_artifacts: tuple[ArtifactRef, ...],
) -> StageCommand:
    """Rebuild one canonical stage command from its immutable database row."""

    stored_request = row[4]
    if (
        isinstance(stored_request, Mapping)
        and stored_request.get("schema_version") == DIAGNOSTIC_COMMAND_REQUEST_SCHEMA
    ):
        if set(stored_request) != {"schema_version", "command"} or not isinstance(
            stored_request["command"],
            Mapping,
        ):
            raise PipelineError("The diagnostic command envelope is invalid")
        command = StageCommand.from_record(stored_request["command"])
    else:
        command = StageCommand(
            schema_version=STAGE_COMMAND_SCHEMA,
            command_id=row[0],
            run_id=row[1],
            kind=row[2],
            stage=row[3],
            idempotency_key=row[6],
            request=stored_request,
            request_digest=row[5],
            input_artifacts=input_artifacts,
            config_snapshot_sha256=row[7],
            contract_sha256=row[8],
            policy_sha256=row[9],
            expected_executor_sha256=row[10],
            predecessor_decision_id=row[11],
        )
    if (
        command.command_id != row[0]
        or command.run_id != row[1]
        or command.kind != row[2]
        or command.stage != row[3]
        or command.request_digest != row[5]
        or command.idempotency_key != row[6]
        or command.config_snapshot_sha256 != row[7]
        or command.contract_sha256 != row[8]
        or command.policy_sha256 != row[9]
        or command.expected_executor_sha256 != row[10]
        or command.predecessor_decision_id != row[11]
        or command.input_artifacts != input_artifacts
    ):
        raise PipelineError("The stage command row differs from its sealed request")
    return command


def _effect_intent(row: tuple) -> EffectIntent:
    return EffectIntent(
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


def _effect_fact(row: tuple) -> EffectFact:
    return EffectFact(
        fact_id=row[0],
        effect_id=row[1],
        seq=row[2],
        claim_id=row[3],
        fence=row[4],
        fact_type=row[5],
        source=row[6],
        state_class=row[7],
        payload_artifact_id=row[8],
        payload_digest=row[9],
        observed_at=row[10],
    )


def _stage_projection(
    *,
    stage: str,
    command: Mapping[str, Any] | None,
    decision: Mapping[str, Any] | None,
    result,
    output_record: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    members: dict[str, dict[str, Any]] = {}
    artifacts: list[dict[str, Any]] = []
    output_artifact = None
    result_record = None
    if result is not None:
        output_artifact = result.output_artifact
        result_record = result.to_record()
        verification = output_record or {}
        for member in verification.get("members", ()):
            if not isinstance(member, Mapping):
                continue
            row = {
                "bytes": member.get("bytes"),
                "sha256": member.get("sha256"),
                "artifact": output_artifact.to_record(),
            }
            path = member.get("path")
            artifacts.append({"path": path, **row})
            members[str(path)] = {
                **row,
                "stage": stage,
                "state": decision.get("outcome") if decision else "waiting",
            }
    failure = decision.get("failure_envelope") if isinstance(decision, Mapping) else None
    state = (
        str(decision["outcome"])
        if isinstance(decision, Mapping) and isinstance(decision.get("outcome"), str)
        else "waiting"
    )
    receipt: dict[str, Any] = {"artifacts": artifacts}
    if result_record is not None:
        receipt.update(
            {
                "checks_sha256": result.checks_sha256,
                "resources_settled": result.resources_settled,
                "result": result_record,
                "result_digest": result.digest,
            }
        )
    if isinstance(failure, Mapping):
        receipt.update(
            {
                "error": failure.get("error_message"),
                "error_code": failure.get("reason_code"),
                "detail": failure.get("error_detail"),
                "failure": dict(failure),
            }
        )
    row = {
        "stage": stage,
        "state": state,
        "command": dict(command) if isinstance(command, Mapping) else None,
        "decision": dict(decision) if isinstance(decision, Mapping) else None,
        "receipt": receipt,
        "original_executed_at": None,
        "updated_at": decision.get("decided_at") if isinstance(decision, Mapping) else None,
    }
    return row, members


def read_run_projection(
    connection,
    run_id: str,
    *,
    artifacts: ArtifactStore | None = None,
) -> dict[str, Any]:
    """Read one run from immutable admission, command and decision facts."""

    if not isinstance(run_id, str) or not run_id:
        raise PipelineError("A run id is required")
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT request_id, parent_run_id, from_stage, request, request_digest, created_at
            FROM run_admission
            WHERE run_id = %s
            """,
            (run_id,),
        )
        admission = cursor.fetchone()
        if admission is None:
            raise PipelineError(f"Run {run_id!r} is not admitted")
        cursor.execute(
            "SELECT activity FROM run_display_metadata WHERE run_id = %s",
            (run_id,),
        )
        activity_row = cursor.fetchone()
        activity = (
            activity_row[0]
            if activity_row is not None
            and isinstance(activity_row[0], Mapping)
            else None
        )
        cursor.execute(
            """
            SELECT command_id, run_id, kind, stage, request, request_digest,
                   idempotency_key, config_snapshot_sha256, contract_sha256,
                   policy_sha256, expected_executor_sha256, predecessor_decision_id
            FROM command
            WHERE run_id = %s
            ORDER BY command_id
            """,
            (run_id,),
        )
        commands = {
            row[0]: {
                "command_id": row[0],
                "run_id": row[1],
                "kind": row[2],
                "stage": row[3],
                "request": row[4],
                "request_digest": row[5],
                "idempotency_key": row[6],
                "config_snapshot_sha256": row[7],
                "contract_sha256": row[8],
                "policy_sha256": row[9],
                "expected_executor_sha256": row[10],
                "predecessor_decision_id": row[11],
            }
            for row in cursor.fetchall()
        }
        cursor.execute(
            """
            SELECT input_artifact.command_id,
                   artifact.artifact_id, artifact.kind, artifact.locator,
                   artifact.manifest_sha256, artifact.set_sha256,
                   artifact.producer_run_id, artifact.producer_stage,
                   artifact.producer_tool_sha256
            FROM command_input_artifact AS input_artifact
            JOIN command AS stage_command
              ON stage_command.command_id = input_artifact.command_id
            JOIN artifact
              ON artifact.artifact_id = input_artifact.artifact_id
            WHERE stage_command.run_id = %s
            ORDER BY input_artifact.command_id, input_artifact.position
            """,
            (run_id,),
        )
        command_inputs: dict[str, list[ArtifactRef]] = {}
        for row in cursor.fetchall():
            command_inputs.setdefault(row[0], []).append(_artifact_ref(row[1:]))
        canonical_commands: dict[str, StageCommand] = {}
        for command_id, row in commands.items():
            if row["kind"] != "stage":
                continue
            database_row = (
                command_id,
                row["run_id"],
                row["kind"],
                row["stage"],
                row["request"],
                row["request_digest"],
                row["idempotency_key"],
                row.get("config_snapshot_sha256"),
                row.get("contract_sha256"),
                row.get("policy_sha256"),
                row.get("expected_executor_sha256"),
                row["predecessor_decision_id"],
            )
            canonical_commands[command_id] = _stage_command(
                database_row,
                input_artifacts=tuple(command_inputs.get(command_id, ())),
            )
        cursor.execute(
            """
            SELECT decision_id, stage, command_id, outcome, result_record_artifact_id,
                   result_digest, actual_executor_sha256, reason_code, failure_envelope,
                   decided_at
            FROM stage_decision
            WHERE run_id = %s
            """,
            (run_id,),
        )
        decisions = {
            row[1]: {
                "decision_id": row[0],
                "stage": row[1],
                "command_id": row[2],
                "outcome": row[3],
                "result_record_artifact_id": row[4],
                "result_digest": row[5],
                "actual_executor_sha256": row[6],
                "reason_code": row[7],
                "failure_envelope": row[8],
                "decided_at": row[9].isoformat() if isinstance(row[9], datetime) else row[9],
            }
            for row in cursor.fetchall()
        }
        artifact_rows: dict[str, ArtifactRef] = {}
        for decision in decisions.values():
            artifact_id = decision.get("result_record_artifact_id")
            if not isinstance(artifact_id, str) or artifact_id in artifact_rows:
                continue
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
            if row is not None:
                artifact_rows[artifact_id] = _artifact_ref(row)
        cursor.execute(
            """
            SELECT claim.claim_id, claim.command_id, claim.fence, claim.owner_sha256,
                   claim.executor_sha256, claim.lane_key, claim.dag_id, claim.dag_run_id,
                   claim.task_id, claim.map_index, claim.issued_at, claim.expires_at,
                   claim.closed_at, claim.closure_code, claim.yield_effect_id
            FROM execution_claim AS claim
            JOIN command AS stage_command ON stage_command.command_id = claim.command_id
            WHERE stage_command.run_id = %s
            ORDER BY claim.issued_at, claim.claim_id
            """,
            (run_id,),
        )
        technical_claims = [
            {
                "claim_id": row[0],
                "command_id": row[1],
                "fence": row[2],
                "owner_sha256": row[3],
                "executor_sha256": row[4],
                "lane_key": row[5],
                "dag_id": row[6],
                "dag_run_id": row[7],
                "task_id": row[8],
                "map_index": row[9],
                "issued_at": row[10].isoformat() if isinstance(row[10], datetime) else row[10],
                "expires_at": row[11].isoformat() if isinstance(row[11], datetime) else row[11],
                "closed_at": row[12].isoformat() if isinstance(row[12], datetime) else row[12],
                "closure_code": row[13],
                "yield_effect_id": row[14],
            }
            for row in cursor.fetchall()
        ]
        cursor.execute(
            """
            SELECT effect.effect_id, effect.command_id, effect.effect_key,
                   effect.effect_kind, effect.target, effect.expected_state,
                   effect.expected_state_digest, effect.outbound_artifact_id,
                   effect.outbound_digest, effect.created_at
            FROM effect_intent AS effect
            JOIN command AS stage_command
              ON stage_command.command_id = effect.command_id
            WHERE stage_command.run_id = %s
            ORDER BY effect.effect_id
            """,
            (run_id,),
        )
        effect_intents = [_effect_intent(row) for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT fact.fact_id, fact.effect_id, fact.seq, fact.claim_id,
                   fact.fence, fact.fact_type, fact.source, fact.state_class,
                   fact.payload_artifact_id, fact.payload_digest, fact.observed_at
            FROM effect_fact AS fact
            JOIN effect_intent AS effect
              ON effect.effect_id = fact.effect_id
            JOIN command AS stage_command
              ON stage_command.command_id = effect.command_id
            WHERE stage_command.run_id = %s
            ORDER BY fact.effect_id, fact.seq
            """,
            (run_id,),
        )
        effect_facts = [_effect_fact(row) for row in cursor.fetchall()]
    finally:
        cursor.close()

    facts_by_effect: dict[str, list[EffectFact]] = {}
    for fact in effect_facts:
        facts_by_effect.setdefault(fact.effect_id, []).append(fact)
    effect_states_by_command: dict[str, dict[str, EffectState]] = {}
    effect_state_rows: list[dict[str, Any]] = []
    for intent in effect_intents:
        state = project_effect_state(
            intent,
            tuple(facts_by_effect.get(intent.effect_id, ())),
        )
        effect_states_by_command.setdefault(intent.command_id, {})[
            intent.effect_id
        ] = state
        effect_state_rows.append(
            {
                "effect_id": intent.effect_id,
                "command_id": intent.command_id,
                "effect_key": intent.effect_key,
                "effect_kind": intent.effect_kind,
                "state": state.value,
            }
        )

    admission_record = RunAdmission(
        schema_version=RUN_ADMISSION_SCHEMA,
        run_id=run_id,
        request_id=admission[0],
        parent_run_id=admission[1],
        from_stage=admission[2],
        request=admission[3],
        request_digest=admission[4],
        created_at=admission[5],
    )
    diagnostic_target = _diagnostic_target(admission[3])
    if diagnostic_target is not None:
        diagnostic_spec = DiagnosticAdmissionSpec.from_record(admission[3])
        if (
            diagnostic_spec.run_id != run_id
            or diagnostic_spec.request_id != admission[0]
            or diagnostic_spec.target_stage != admission[2]
        ):
            raise PipelineError("The diagnostic admission row differs from its request")
        stage_commands = [
            command for command in canonical_commands.values() if command.kind == "stage"
        ]
        if (
            len(stage_commands) != 1
            or stage_commands[0].stage != diagnostic_target
            or stage_commands[0].diagnostic_scope is None
            or stage_commands[0].diagnostic_scope.target_stage != diagnostic_target
            or stage_commands[0].predecessor_decision_id is not None
        ):
            raise PipelineError("The diagnostic run does not carry one scoped target command")

    stage_rows: list[dict[str, Any]] = []
    by_stage: dict[str, dict[str, dict[str, Any]]] = {}
    members: dict[str, dict[str, Any]] = {}
    open_claim_commands = {
        claim["command_id"]
        for claim in technical_claims
        if claim.get("closed_at") is None
    }
    yielded_effect_by_command: dict[str, tuple[int, str]] = {}
    for claim in technical_claims:
        if (
            claim.get("closure_code") != "yielded"
            or not isinstance(claim.get("yield_effect_id"), str)
        ):
            continue
        current = yielded_effect_by_command.get(claim["command_id"])
        if current is None or claim["fence"] > current[0]:
            yielded_effect_by_command[claim["command_id"]] = (
                claim["fence"],
                claim["yield_effect_id"],
            )
    for stage in STAGE_IDS:
        command = next(
            (
                command
                for command in commands.values()
                if command["stage"] == stage and command["kind"] == "stage"
            ),
            None,
        )
        decision = decisions.get(stage)
        command_model = (
            canonical_commands.get(command["command_id"])
            if isinstance(command, Mapping)
            else None
        )
        result = None
        output_record = None
        qualification = None
        if decision is not None and artifacts is not None:
            if command_model is None:
                raise PipelineError("A stage decision has no canonical stage command")
            result_artifact = artifact_rows.get(decision.get("result_record_artifact_id"))
            if result_artifact is not None:
                result = artifacts.read_result(result_artifact)
                if result.digest != decision.get("result_digest"):
                    raise PipelineError("The StageResult differs from its stage decision")
                if result.outcome != decision.get("outcome"):
                    raise PipelineError("The StageResult outcome differs from its stage decision")
                output_record = artifacts.verify(result.output_artifact)
                qualification = qualify_stage(
                    admission_record,
                    command_model,
                    result=result,
                )
            else:
                failure_record = decision.get("failure_envelope")
                if isinstance(failure_record, Mapping):
                    failure = FailureEnvelope.from_record(failure_record)
                    if failure.outcome != decision.get("outcome"):
                        raise PipelineError("The failure outcome differs from its stage decision")
                    qualification = qualify_stage(
                        admission_record,
                        command_model,
                        failure=failure,
                    )
        row, stage_members = _stage_projection(
            stage=stage,
            command=command_model.to_record() if command_model is not None else command,
            decision=decision,
            result=result,
            output_record=output_record,
        )
        command_id = (
            command_model.command_id if command_model is not None else None
        )
        command_effects = effect_states_by_command.get(command_id or "", {})
        effect_states = tuple(
            state.value for state in command_effects.values()
        )
        yielded = yielded_effect_by_command.get(command_id or "")
        pending_effects = (
            (yielded[1],)
            if yielded is not None
            and command_effects.get(yielded[1]) is EffectState.UNKNOWN
            else ()
        )
        pending_effect_states = tuple(
            "unknown" for _effect_id in pending_effects
        )
        stage_index = STAGE_IDS.index(stage)
        predecessor_completed = (
            stage_index == 0
            or diagnostic_target == stage
            or (
                stage_index > 0
                and stage_rows[-1].get("state") == StageStatus.COMPLETED.value
            )
        )
        status = project_stage_status(
            decision_outcome=(
                decision.get("outcome") if isinstance(decision, Mapping) else None
            ),
            effect_states=effect_states,
            predecessor_completed=predecessor_completed,
            open_claim=command_id in open_claim_commands,
            pending_effect_states=pending_effect_states,
        )
        row["state"] = status.value
        receipt = row.get("receipt")
        if isinstance(receipt, dict):
            receipt["effect_states"] = [
                {
                    "effect_id": effect_id,
                    "state": state.value,
                }
                for effect_id, state in sorted(command_effects.items())
            ]
            receipt["pending_effect_states"] = [
                {
                    "effect_id": effect_id,
                    "state": "unknown",
                }
                for effect_id in pending_effects
            ]
        row["scope_kind"] = (
            command_model.diagnostic_scope is not None
            if command_model is not None
            else ("diagnostic" if diagnostic_target is not None else "formal") == "diagnostic"
        )
        row["scope_kind"] = "diagnostic" if row["scope_kind"] else "formal"
        row["formal_eligible"] = bool(
            status is StageStatus.COMPLETED
            and qualification is not None
            and qualification.formal_eligible
        )
        row["qualification"] = (
            qualification.to_record() if qualification is not None else None
        )
        row["in_scope"] = diagnostic_target is None or stage == diagnostic_target
        stage_rows.append(row)
        by_stage[stage] = stage_members
        for path, member in stage_members.items():
            members[path] = member
    purpose = (
        admission[3].get("purpose")
        if isinstance(admission[3], Mapping)
        else None
    )
    run_state = _run_state(stage_rows, diagnostic_target=diagnostic_target)
    run_scope = (
        {
            "kind": "diagnostic",
            "target_stage": diagnostic_target,
            "formal_eligible": False,
        }
        if diagnostic_target is not None
        else {
            "kind": "formal",
            "target_stage": None,
            "formal_eligible": run_state == "succeeded",
        }
    )
    return {
        "run_id": run_id,
        "request_id": admission[0],
        "parent_run_id": admission[1],
        "from_stage": admission[2],
        "request": dict(admission[3]) if isinstance(admission[3], Mapping) else None,
        "purpose": purpose,
        "request_digest": admission[4],
        "created_at": admission[5].isoformat() if isinstance(admission[5], datetime) else admission[5],
        "scope": run_scope,
        "formal_eligible": run_scope["formal_eligible"],
        "state": run_state,
        "stages": stage_rows,
        "by_stage": by_stage,
        "members": members,
        "activity": activity,
        "technical": {
            "commands": list(commands.values()),
            "claims": technical_claims,
            "effects": effect_state_rows,
        },
    }


def render_stage_summary(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stable display subset derived from a run projection."""

    if not isinstance(projection, Mapping) or not isinstance(projection.get("stages"), list):
        raise PipelineError("A run projection is required")
    return [
        {
            "stage": row["stage"],
            "state": row.get("state") or "waiting",
            "command_id": row["decision"]["command_id"]
            if row.get("decision")
            else (row.get("command") or {}).get("command_id"),
            "reason_code": row["decision"]["reason_code"] if row.get("decision") else None,
            "scope_kind": row.get("scope_kind"),
            "formal_eligible": row.get("formal_eligible") is True,
            "in_scope": row.get("in_scope") is not False,
        }
        for row in projection["stages"]
    ]


__all__ = ["read_run_projection", "render_stage_summary"]
