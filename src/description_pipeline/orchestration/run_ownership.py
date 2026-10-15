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
from dataclasses import dataclass
from typing import Any

from .feishu_oauth import (
    TRIGGERING_USER_NAME_DELIMITER,
    TRIGGERING_USER_NAME_LIMIT,
    recorded_display_name,
    valid_actor_principal,
)
from .request_context import CLEAR_BODY_SCOPE_KEY, CREATE_BODY_SCOPE_KEY, bound_request

#: The pinned route module that owns the DAG-run handlers; matching module+name means a future
#: route rename fails closed instead of matching some other handler.
DAG_RUN_ROUTES_MODULE = "airflow.api_fastapi.core_api.routes.public.dag_run"
MANUAL_CREATE_ENDPOINT = "trigger_dag_run"
SINGLE_RUN_CLEAR_ENDPOINT = "clear_dag_run"

#: The one task vocabulary of the workflow DAG, grouped by the operation model:
#: admission resolves the frozen intake (idempotent, no side effects), the native
#: boundary submits and imports the endpoint's capture (the native job is created
#: once for the run UUID and only poked afterwards, so a retry never replays CAD),
#: the portable stages generate and verify (no external side effects) and the
#: publication stage is the one external write.  Recovery decisions key off these
#: operation classes and the native status authority - never off task-name
#: compatibility rows.
ADMISSION_TASKS = frozenset({"resolve_handoff"})
NATIVE_TASKS = frozenset({"start_job", "fetch_capture"})
PORTABLE_TASKS = frozenset({"run_generate", "run_verify"})
PUBLICATION_TASKS = frozenset({"run_publish"})
KNOWN_TASKS = ADMISSION_TASKS | NATIVE_TASKS | PORTABLE_TASKS | PUBLICATION_TASKS
FAILED_STATES = frozenset({"failed", "upstream_failed"})
#: Sentinel for a task id that appears with several map indices; eligibility is then unprovable.
AMBIGUOUS_TASK_STATE = "ambiguous_mapped_indices"
#: Native job statuses that never prove a terminal CAD outcome.
NATIVE_IN_FLIGHT = frozenset({"queued", "selecting", "running", "awaiting_definition"})
#: Native job statuses that prove the sealed capture exists.
NATIVE_SEALED = frozenset({"passed", "native_complete"})
#: The four recovery classes of the stage/operation model.
RECOVERY_TEMPORARY_DISCONNECTION = "temporary_disconnection"
RECOVERY_EVIDENCE_CORRUPTION = "evidence_corruption"
RECOVERY_ENGINEERING_TERMINAL = "engineering_terminal_failure"
RECOVERY_UNKNOWN_PUBLICATION = "unknown_publication_response"


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
    if native_status == "failed":
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


def retry_assessment_for_run(dag_id: str, run_id: str) -> RecoveryAssessment:
    """The authoritative classification of one run; a missing row refuses as ``run_missing``."""
    facts = recorded_run_facts(dag_id, run_id)
    if facts is None:
        return RecoveryAssessment(False, "", "run_missing", ())
    _, run_state, task_states = facts
    return classify_recovery(run_state, task_states)


def owner_retry_allowed(user: Any, dag_id: str, run_id: str) -> bool:
    """True only for the caller's own run that is a positively classified transport recovery."""
    facts = recorded_run_facts(dag_id, run_id)
    if facts is None:
        return False
    principal, run_state, task_states = facts
    if principal is None or principal != user.get_id():
        return False
    return classify_recovery(run_state, task_states).eligible


def owner_create_allowed(user: Any, dag_id: str) -> bool:
    """A linked run may reuse only a terminal parent owned by this authenticated user."""
    request = bound_request()
    candidate = getattr(request, "scope", {}).get(CREATE_BODY_SCOPE_KEY)
    if not isinstance(candidate, dict) or candidate.get("safe") is not True:
        return False
    parent = candidate.get("parent_dag_run_id")
    if parent is None:
        return True
    facts = recorded_run_facts(dag_id, parent)
    return bool(
        facts and facts[0] is not None and facts[0] == user.get_id() and _state_value(facts[1]) in {"success", "failed"}
    )
