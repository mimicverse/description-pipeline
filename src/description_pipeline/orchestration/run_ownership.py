"""Strict single-run ownership for the one SolidWorks workflow DAG.

A non-admin may resume transport on their own run through
Airflow's single-run ``clear_dag_run`` endpoint with the immutable-retry body. Every input is
server-owned — the routed endpoint and path parameters bound by the root middleware, the recorded
``triggering_user_name`` envelope and the validated Feishu user from the auth dependency.
Linked-run creation additionally checks the stored parent actor. Missing, damaged or absent
authority values deny; they never grant.

Eligibility is a positive stage/operation classification: the failed task set is read through the
operation model (admission, native boundary, portable stages, publication) and one of the four
recovery classes - temporary disconnection, evidence corruption, engineering terminal failure,
unknown publication response - decides the outcome.  No recovery path may repeat a CAD or a
publication side effect: a temporary disconnection resumes the same native job (created once for
the run UUID) or re-runs side-effect-free portable work, corrupted sealed evidence is re-imported
without touching CAD, and terminal engineering failures or unknown publication responses refuse
in-place recovery entirely.  Unknown or mapped-ambiguous failures always require a new run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .feishu_oauth import (
    TRIGGERING_USER_NAME_DELIMITER,
    TRIGGERING_USER_NAME_LIMIT,
    recorded_display_name,
    valid_actor_principal,
)
from ..io import digest
from ..pipeline.contracts import (
    DispatchCommand,
    DispatchConf,
    ProtocolError,
    StageCommand,
)
from .request_context import (
    CLEAR_BODY_SCOPE_KEY,
    CREATE_BODY_SCOPE_KEY,
    bound_request,
    valid_dag_run_id,
)

#: The pinned route module that owns the DAG-run handlers; matching module+name means a future
#: route rename fails closed instead of matching some other handler.
DAG_RUN_ROUTES_MODULE = "airflow.api_fastapi.core_api.routes.public.dag_run"
MANUAL_CREATE_ENDPOINT = "trigger_dag_run"
SINGLE_RUN_CLEAR_ENDPOINT = "clear_dag_run"

#: The one task vocabulary of the workflow DAG (seven stages), grouped by the operation model:
#: admission resolves the frozen intake (idempotent, no side effects), the native
#: boundary submits and imports the endpoint's capture (the native job is created
#: once for the run UUID and only poked afterwards, so a retry never replays CAD),
#: the portable stages generate and verify (no external side effects) and the
#: publication stage is the one external write.  Recovery decisions key off these
#: operation classes and the native status authority - never off task-name
#: compatibility rows.
ADMISSION_TASKS = frozenset({"freeze"})
NATIVE_TASKS = frozenset({"discover", "capture"})
PORTABLE_TASKS = frozenset({"generate", "verify", "mjcf"})
PUBLICATION_TASKS = frozenset({"publish"})
KNOWN_TASKS = ADMISSION_TASKS | NATIVE_TASKS | PORTABLE_TASKS | PUBLICATION_TASKS
FAILED_STATES = frozenset({"failed", "upstream_failed"})
#: Sentinel for a task id that appears with several map indices; eligibility is then unprovable.
AMBIGUOUS_TASK_STATE = "ambiguous_mapped_indices"
#: Native operation states that never prove a terminal CAD outcome.
NATIVE_IN_FLIGHT = frozenset({"accepted", "awaiting_input", "running"})
#: Native operation states that prove the sealed capture exists.
NATIVE_SEALED = frozenset({"satisfied"})
#: The four recovery classes of the stage/operation model.
RECOVERY_TEMPORARY_DISCONNECTION = "temporary_disconnection"
RECOVERY_EVIDENCE_CORRUPTION = "evidence_corruption"
RECOVERY_ENGINEERING_TERMINAL = "engineering_terminal_failure"
RECOVERY_UNKNOWN_PUBLICATION = "unknown_publication_response"
RECOVERY_RECONCILIATION_REQUIRED = "reconciliation_required"
_REGISTERED_RECOVERY_CLASSIFICATION = {
    "failed": RECOVERY_ENGINEERING_TERMINAL,
    "cancelled": RECOVERY_ENGINEERING_TERMINAL,
    "succeeded": "run_succeeded",
    "unresolved": RECOVERY_RECONCILIATION_REQUIRED,
}


@dataclass(frozen=True)
class RoutedRoute:
    """One recognized route from the trusted scope: endpoint, HTTP method, dag id, run id."""

    endpoint: str
    http_method: str
    dag_id: str
    dag_run_id: str | None
    clear_body_safe: bool = False


@dataclass(frozen=True)
class RecoveryAssessment:
    """One stage/operation recovery classification from authoritative run/task state."""

    eligible: bool
    classification: str
    reason: str
    cleared_tasks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CreateDispatchBinding:
    """The persisted admission and dispatch facts bound by one create body."""

    principal: str
    run_id: str
    parent_run_id: str | None
    parent_dag_run_id: str | None
    dag_id: str
    dag_run_id: str
    entry_command_id: str


def recorded_create_binding(
    dag_run_id: str,
    conf: DispatchConf,
) -> CreateDispatchBinding | None:
    """Read the exact admitted run and dispatch command as the operator role."""

    dsn = os.environ.get("PLATFORM_OPERATOR_DSN")
    if not dsn:
        return None
    try:
        import psycopg2  # noqa: PLC0415 - Airflow runtime dependency only

        with psycopg2.connect(
            dsn,
            connect_timeout=5,
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT admission.request->>'principal', admission.run_id,
                       admission.parent_run_id,
                       dispatch.request->>'dag_id',
                       dispatch.request->>'dag_run_id',
                       dispatch.request->>'entry_command_id',
                       parent_dispatch.request->>'dag_id',
                       parent_dispatch.request->>'dag_run_id'
                FROM run_admission AS admission
                JOIN command AS dispatch
                  ON dispatch.run_id = admission.run_id
                 AND dispatch.kind = 'dispatch'
                LEFT JOIN command AS parent_dispatch
                  ON parent_dispatch.run_id = admission.parent_run_id
                 AND parent_dispatch.kind = 'dispatch'
                WHERE admission.run_id = %s
                  AND dispatch.request->>'dag_run_id' = %s
                  AND dispatch.request->>'entry_command_id' = %s
                """,
                (conf.run_id, dag_run_id, conf.entry_command_id),
            )
            rows = cursor.fetchall()
    except Exception:
        return None
    if len(rows) != 1:
        return None
    (
        principal,
        run_id,
        parent_run_id,
        dag_id,
        stored_run_id,
        entry_command_id,
        parent_dag_id,
        parent_dag_run_id,
    ) = rows[0]
    if (
        not isinstance(principal, str)
        or not valid_actor_principal(principal)
        or (parent_run_id is not None and not isinstance(parent_run_id, str))
        or run_id != conf.run_id
        or dag_run_id != stored_run_id
        or entry_command_id != conf.entry_command_id
        or not isinstance(dag_id, str)
        or not dag_id
        or (
            parent_run_id is None
            and (parent_dag_id is not None or parent_dag_run_id is not None)
        )
        or (
            parent_run_id is not None
            and (
                parent_dag_id != dag_id
                or not isinstance(parent_dag_run_id, str)
                or not valid_dag_run_id(parent_dag_run_id)
            )
        )
    ):
        return None
    return CreateDispatchBinding(
        principal=principal,
        run_id=run_id,
        parent_run_id=parent_run_id,
        parent_dag_run_id=parent_dag_run_id,
        dag_id=dag_id,
        dag_run_id=stored_run_id,
        entry_command_id=entry_command_id,
    )


def recorded_actor_principal(actor: object) -> str | None:
    """The stable principal of a genuine recorded actor envelope, or ``None``.

    Ownership never trusts a prefix-shaped or damaged envelope: the compound principal must be
    structurally valid, the raw value must fit the metadata column, and the JSON-recorded display
    name must already be the canonical normalized form.
    """
    if not isinstance(actor, str):
        return None
    if not actor or len(actor) > TRIGGERING_USER_NAME_LIMIT:
        return None
    principal, delimiter, encoded = actor.partition(TRIGGERING_USER_NAME_DELIMITER)
    if not delimiter or not valid_actor_principal(principal):
        return None
    try:
        name = json.loads(encoded)
    except ValueError:
        return None
    try:
        canonical = recorded_display_name(name)
    except ValueError:
        return None
    if canonical != name:
        # Whitespace-padded or non-NFC values are damaged envelopes, not identities.
        return None
    return principal


def _endpoint_named(endpoint: object, name: str) -> bool:
    return getattr(endpoint, "__module__", "") == DAG_RUN_ROUTES_MODULE and getattr(endpoint, "__name__", "") == name


def routed_route() -> RoutedRoute | None:
    """The recognized route from the bound request scope, or ``None`` when not provable."""
    request = bound_request()
    if request is None:
        return None
    scope = getattr(request, "scope", {})
    params = getattr(request, "path_params", {})
    dag_id = params.get("dag_id")
    if not isinstance(dag_id, str) or not dag_id:
        return None
    http_method = scope.get("method")
    if not isinstance(http_method, str):
        return None
    endpoint = scope.get("endpoint")
    if _endpoint_named(endpoint, MANUAL_CREATE_ENDPOINT):
        return RoutedRoute(MANUAL_CREATE_ENDPOINT, http_method, dag_id, None)
    if _endpoint_named(endpoint, SINGLE_RUN_CLEAR_ENDPOINT):
        run_id = params.get("dag_run_id")
        if not isinstance(run_id, str) or not run_id or run_id == "~":
            return None
        candidate = scope.get(CLEAR_BODY_SCOPE_KEY)
        clear_body_safe = isinstance(candidate, dict) and candidate.get("safe") is True
        return RoutedRoute(SINGLE_RUN_CLEAR_ENDPOINT, http_method, dag_id, run_id, clear_body_safe)
    return None


def single_run_clear_requested() -> bool:
    """Whether the resolved request endpoint is the guarded single-run clear route."""

    request = bound_request()
    if request is None:
        return False
    return _endpoint_named(
        getattr(request, "scope", {}).get("endpoint"),
        SINGLE_RUN_CLEAR_ENDPOINT,
    )


def _state_value(state: object) -> str | None:
    value = getattr(state, "value", state)
    return value if isinstance(value, str) else None


def classify_recovery(
    run_state: object, task_states: dict[str, object], native_status: str | None = None
) -> RecoveryAssessment:
    """Classify one failed run through the stage/operation model.

    ``native_status`` is the endpoint job snapshot's status when the caller can
    consult it (the portal), or ``None`` when no endpoint authority is available
    (the authorization layer): without that authority only the permissive
    operation patterns are decided and the portal narrows them with the
    authoritative snapshot.  Terminal native failures and attempted
    publications refuse in every case; an unattempted publication is untouched.
    """

    state = _state_value(run_state)
    if state != "failed":
        return RecoveryAssessment(False, "", "run_succeeded" if state == "success" else "run_active", ())
    normalized = {task: _state_value(value) for task, value in task_states.items()}
    if any(value == AMBIGUOUS_TASK_STATE for value in normalized.values()):
        return RecoveryAssessment(False, "", "unknown_failed_task", ())
    failed = {task for task, value in normalized.items() if value in FAILED_STATES}
    if not failed or failed - KNOWN_TASKS:
        return RecoveryAssessment(False, "", "unknown_failed_task", ())
    real_failed = {task for task in failed if normalized[task] == "failed"}
    cleared = tuple(sorted(failed))
    if not real_failed:
        return RecoveryAssessment(False, "", "no_failed_operation", ())
    if real_failed & PUBLICATION_TASKS:
        # A publish attempt with an unknown outcome must be reconciled first;
        # retrying could repeat the external write.
        return RecoveryAssessment(False, RECOVERY_UNKNOWN_PUBLICATION, "publication_response_unknown", ())
    if real_failed & ADMISSION_TASKS and real_failed - ADMISSION_TASKS:
        # A failed admission cannot resume native or portable operations: their
        # frozen request never resolved.
        return RecoveryAssessment(False, RECOVERY_TEMPORARY_DISCONNECTION, "admission_failed", ())
    if native_status in {"failed", "cancelled"}:
        return RecoveryAssessment(False, RECOVERY_ENGINEERING_TERMINAL, "native_terminal_failure", ())
    if real_failed & NATIVE_TASKS:
        if native_status in NATIVE_SEALED:
            # The CAD capture is sealed; recovery re-reads the sealed evidence
            # and never repeats the capture.
            return RecoveryAssessment(True, RECOVERY_EVIDENCE_CORRUPTION, "sealed_evidence_reimport", cleared)
        if native_status is None or native_status in NATIVE_IN_FLIGHT:
            # Either no endpoint authority exists here (the portal narrows it) or
            # the same native job is still in flight: resuming pokes the job that
            # the run UUID already created and never replays CAD.
            return RecoveryAssessment(True, RECOVERY_TEMPORARY_DISCONNECTION, "temporary_disconnection", cleared)
        return RecoveryAssessment(False, RECOVERY_TEMPORARY_DISCONNECTION, "native_evidence_unavailable", ())
    if real_failed & PORTABLE_TASKS:
        if native_status is None or native_status in NATIVE_SEALED or native_status in NATIVE_IN_FLIGHT:
            # Portable work has no external side effects: a retry can repeat
            # neither CAD nor a publication.
            return RecoveryAssessment(True, RECOVERY_TEMPORARY_DISCONNECTION, "portable_retry", cleared)
        return RecoveryAssessment(False, RECOVERY_TEMPORARY_DISCONNECTION, "native_evidence_unavailable", ())
    if real_failed & ADMISSION_TASKS:
        if native_status is None or native_status in NATIVE_IN_FLIGHT:
            return RecoveryAssessment(True, RECOVERY_TEMPORARY_DISCONNECTION, "temporary_disconnection", cleared)
        return RecoveryAssessment(False, RECOVERY_TEMPORARY_DISCONNECTION, "native_evidence_unavailable", ())
    return RecoveryAssessment(False, "", "unknown_failed_task", ())


def recorded_run_facts(dag_id: str, run_id: str) -> tuple[str | None, object, dict[str, object]] | None:
    """One session reads the exact run: (actor principal, state, per-task states), or ``None``.

    A task id that appears with several map indices is recorded as an ambiguous sentinel so no
    eligibility decision can be made from an arbitrary row.
    """
    from airflow.models.dagrun import DagRun  # noqa: PLC0415 - Airflow runtime only
    from airflow.models.taskinstance import TaskInstance  # noqa: PLC0415
    from airflow.settings import Session  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    with Session() as session:
        run = session.scalar(select(DagRun).where(DagRun.dag_id == dag_id, DagRun.run_id == run_id))
        if run is None:
            return None
        rows = session.execute(
            select(TaskInstance.task_id, TaskInstance.state).where(
                TaskInstance.dag_id == dag_id, TaskInstance.run_id == run_id
            )
        ).all()
        task_states: dict[str, object] = {}
        for task_id, state in rows:
            key = str(task_id)
            task_states[key] = AMBIGUOUS_TASK_STATE if key in task_states else state
    return recorded_actor_principal(run.triggering_user_name), run.state, task_states


def registered_recovery_block(record: dict | None) -> tuple[str, str] | None:
    """Return the reason and class blocking in-place recovery from a platform projection."""

    state = record.get("state") if isinstance(record, dict) else None
    if state in {"queued", "running"}:
        return None
    if isinstance(state, str) and state in _REGISTERED_RECOVERY_CLASSIFICATION:
        return f"registered_{state}", _REGISTERED_RECOVERY_CLASSIFICATION[state]
    return "registration_missing", RECOVERY_TEMPORARY_DISCONNECTION


def validated_canonical_projection(
    projection: object,
    *,
    dag_id: str,
    dag_run_id: str,
    canonical_run_id: str,
    source_projection: dict | None = None,
    artifacts: Any | None = None,
) -> dict | None:
    """Authenticate one registered run from the existing admission and command contracts."""

    if not isinstance(projection, dict):
        return None
    try:
        from ..pipeline.admission import (  # noqa: PLC0415 - authorization runtime only
            AdmissionSpec,
            build_freeze_command,
            build_run_admission,
        )

        request = projection.get("request")
        spec = AdmissionSpec.from_record(request)
        admission = build_run_admission(spec)
        if (
            projection.get("run_id") != canonical_run_id
            or admission.run_id != canonical_run_id
            or projection.get("request_id") != admission.request_id
            or projection.get("parent_run_id") != admission.parent_run_id
            or projection.get("from_stage") != admission.from_stage
            or request != spec.to_record()
            or projection.get("request_digest") != admission.request_digest
            or datetime.fromisoformat(projection.get("created_at")) != admission.created_at
        ):
            return None

        technical = projection.get("technical")
        commands = technical.get("commands") if isinstance(technical, dict) else None
        stages = projection.get("stages")
        if (
            not isinstance(commands, list)
            or not all(isinstance(command, dict) for command in commands)
            or not isinstance(stages, list)
            or not all(isinstance(stage, dict) for stage in stages)
        ):
            return None

        dispatch_rows = [
            command for command in commands if command.get("kind") == "dispatch"
        ]
        entry_stage = spec.from_stage or "freeze"
        freeze_rows = [
            command
            for command in commands
            if command.get("kind") == "stage" and command.get("stage") == entry_stage
        ]
        freeze_stages = [stage for stage in stages if stage.get("stage") == entry_stage]
        if len(dispatch_rows) != 1 or len(freeze_rows) != 1 or len(freeze_stages) != 1:
            return None

        dispatch_row = dispatch_rows[0]
        dispatch = DispatchCommand.from_record(dispatch_row.get("request"))
        freeze = StageCommand.from_record(freeze_stages[0].get("command"))
        if spec.from_stage not in {None, "freeze"}:
            from ..pipeline.admission import source_context_from_projection, build_stage_command

            if not isinstance(source_projection, dict):
                return None
            predecessors, discovered, verified = source_context_from_projection(spec, source_projection, artifacts)
            expected_entry = build_stage_command(spec, entry_stage, predecessors, discovered, verified)
            from ..stages import execution_stage_ids

            selected = execution_stage_ids(spec.from_stage, through=spec.through_stage or "publish")
            if any(row.get("command") for row in stages if row.get("stage") not in selected):
                return None
        else:
            expected_entry = build_freeze_command(spec)
        if (
            freeze != expected_entry
            or dispatch.run_id != canonical_run_id
            or dispatch.dag_id != dag_id
            or dispatch.dag_run_id != dag_run_id
            or dispatch.entry_command_id != freeze.command_id
            or dispatch.expected_executor_sha256 != spec.dispatch_executor_sha256
        ):
            return None

        expected_dispatch_id = "dispatch:" + digest(
            {
                "admission_sha256": spec.admission_sha256,
                "dag_id": dag_id,
                "dag_run_id": dag_run_id,
                "entry_command_id": freeze.command_id,
                "run_id": canonical_run_id,
            }
        )
        if (
            dispatch.command_id != expected_dispatch_id
            or dispatch_row.get("command_id") != dispatch.command_id
            or dispatch_row.get("run_id") != canonical_run_id
            or dispatch_row.get("request") != dispatch.to_record()
            or dispatch_row.get("request_digest") != dispatch.request_digest
            or dispatch_row.get("idempotency_key") != dispatch.idempotency_key
            or dispatch_row.get("expected_executor_sha256")
            != dispatch.expected_executor_sha256
            or dispatch_row.get("predecessor_decision_id") is not None
        ):
            return None

        freeze_row = freeze_rows[0]
        if (
            freeze_row.get("command_id") != freeze.command_id
            or freeze_row.get("run_id") != canonical_run_id
            or any(
                freeze_row.get(field) != getattr(freeze, field)
                for field in (
                    "config_snapshot_sha256",
                    "contract_sha256",
                    "policy_sha256",
                )
            )
            or any(
                dispatch_row.get(field) != freeze_row.get(field)
                for field in (
                    "config_snapshot_sha256",
                    "contract_sha256",
                    "policy_sha256",
                )
            )
        ):
            return None
    except Exception:  # noqa: BLE001 - damaged authority always fails closed
        return None
    return projection


def recorded_canonical_projection(dag_id: str, dag_run_id: str) -> dict | None:
    """Read the exact platform projection used by the Portal recovery gate."""

    dsn = os.environ.get("PLATFORM_OPERATOR_DSN")
    if not dsn:
        return None
    try:
        import psycopg2  # noqa: PLC0415 - Airflow runtime dependency only

        from ..platform.queries import read_run_projection  # noqa: PLC0415
        from .airflow_client import native_run_id  # noqa: PLC0415

        canonical_run_id = native_run_id(dag_run_id)
        with psycopg2.connect(
            dsn,
            connect_timeout=5,
            options="-c statement_timeout=5000",
        ) as connection:
            connection.set_session(readonly=True)
            projection = read_run_projection(connection, canonical_run_id)
            request = (projection or {}).get("request") or {}
            source = artifacts = None
            if request.get("from_stage") not in {None, "freeze"}:
                from pathlib import Path
                from ..artifacts import ArtifactStore

                artifact_root = os.environ.get("PLATFORM_ARTIFACT_ROOT")
                if not artifact_root or not Path(artifact_root).is_dir():
                    return None
                artifacts = ArtifactStore(Path(artifact_root))
                projection = read_run_projection(connection, canonical_run_id, artifacts=artifacts)
                from ..stages import STAGE_IDS

                source = read_run_projection(
                    connection, request["parent_run_id"], artifacts=artifacts,
                    _qualified_stages=STAGE_IDS[: STAGE_IDS.index(request["from_stage"])],
                )
    except Exception:  # noqa: BLE001 - authorization fails closed on unavailable authority
        return None
    return validated_canonical_projection(
        projection,
        dag_id=dag_id,
        dag_run_id=dag_run_id,
        canonical_run_id=canonical_run_id,
        source_projection=source,
        artifacts=artifacts,
    )


def retry_allowed(
    user: Any,
    dag_id: str,
    run_id: str,
    *,
    require_owner: bool,
) -> bool:
    """Require canonical registration plus positively classified scheduling recovery."""

    facts = recorded_run_facts(dag_id, run_id)
    if facts is None:
        return False
    principal, run_state, task_states = facts
    if principal is None or (require_owner and principal != user.get_id()):
        return False
    projection = recorded_canonical_projection(dag_id, run_id)
    request = projection.get("request") if isinstance(projection, dict) else None
    if (
        not isinstance(request, dict)
        or request.get("principal") != principal
        or registered_recovery_block(projection) is not None
    ):
        return False
    return classify_recovery(run_state, task_states).eligible


def owner_retry_allowed(user: Any, dag_id: str, run_id: str) -> bool:
    """True only for the caller's own run that is a positively classified transport recovery."""

    return retry_allowed(user, dag_id, run_id, require_owner=True)


def owner_create_allowed(user: Any, dag_id: str) -> bool:
    """Allow create only for the caller's exact persisted admission and dispatch."""

    request = bound_request()
    candidate = getattr(request, "scope", {}).get(CREATE_BODY_SCOPE_KEY)
    if not isinstance(candidate, dict) or candidate.get("safe") is not True:
        return False
    try:
        conf = DispatchConf.from_record(candidate.get("conf"))
    except (ProtocolError, TypeError, ValueError):
        return False
    binding = recorded_create_binding(candidate.get("dag_run_id"), conf)
    if (
        binding is None
        or binding.dag_id != dag_id
        or binding.run_id != conf.run_id
        or binding.dag_run_id != candidate.get("dag_run_id")
        or binding.entry_command_id != conf.entry_command_id
        or binding.principal != user.get_id()
    ):
        return False
    parent = binding.parent_run_id
    if parent is None:
        return True
    if binding.parent_dag_run_id is None:
        return False
    facts = recorded_run_facts(dag_id, binding.parent_dag_run_id)
    return bool(
        facts and facts[0] is not None and facts[0] == user.get_id() and _state_value(facts[1]) in {"success", "failed"}
    )
