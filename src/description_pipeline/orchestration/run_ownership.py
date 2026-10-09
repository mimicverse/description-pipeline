"""Strict single-run ownership for the one SolidWorks workflow DAG.

A non-admin may perform exactly one mutation on an existing run: retrying their own run through
Airflow's single-run ``clear_dag_run`` endpoint with the immutable-retry body. Every input is
server-owned — the routed endpoint and path parameters bound by the root middleware, the recorded
``triggering_user_name`` envelope and the validated Feishu user from the auth dependency.
Missing, damaged or absent values deny; they never grant.

Eligibility is a positive transport classification: the run must be failed, the failed task set
must be ``wait_for_job`` or ``start_job`` (the latter only with a positively successful
``resolve_handoff``, whose service-side create is idempotent for the identical frozen request and
refuses a changed one), with any other failed instance merely upstream-failed. Resolution,
publication, unknown or mapped-ambiguous failures always require a new run.
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
from .request_context import CLEAR_BODY_SCOPE_KEY, bound_request

#: The pinned route module that owns the DAG-run handlers; matching module+name means a future
#: route rename fails closed instead of matching some other handler.
DAG_RUN_ROUTES_MODULE = "airflow.api_fastapi.core_api.routes.public.dag_run"
MANUAL_CREATE_ENDPOINT = "trigger_dag_run"
SINGLE_RUN_CLEAR_ENDPOINT = "clear_dag_run"

#: The DAG's tasks and their roles in one immutable retry. Only a failed ``wait_for_job`` is a
#: positively proven transport recovery: a failed resolution/capture, an ambiguous failed
#: ``start_job`` and a real publication failure all require a new run. ``confirm_job`` may only
#: be present as upstream-failed, never as a real failure.
RESOLUTION_TASK = "resolve_handoff"
START_TASK = "start_job"
TRANSPORT_TASK = "wait_for_job"
PUBLICATION_TASK = "confirm_job"
KNOWN_TASKS = frozenset({RESOLUTION_TASK, START_TASK, TRANSPORT_TASK, PUBLICATION_TASK})
ALLOWED_FAILED_TASKS = frozenset({START_TASK, TRANSPORT_TASK, PUBLICATION_TASK})
FAILED_STATES = frozenset({"failed", "upstream_failed"})
#: Sentinel for a task id that appears with several map indices; eligibility is then unprovable.
AMBIGUOUS_TASK_STATE = "ambiguous_mapped_indices"


@dataclass(frozen=True)
class RoutedRoute:
    """One recognized route from the trusted scope: endpoint, HTTP method, dag id, run id."""

    endpoint: str
    http_method: str
    dag_id: str
    dag_run_id: str | None
    clear_body_safe: bool = False


@dataclass(frozen=True)
class RetryAssessment:
    """Positive transport-recovery classification from authoritative run/task state."""

    eligible: bool
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


def classify_transport_retry(run_state: object, task_states: dict[str, object]) -> RetryAssessment:
    """Classify one run as positive transport recovery, or name the reason it is not.

    Resolution must be successful. Only submission or polling may fail; confirmation may be
    upstream-failed. Missing or ambiguous tasks, failed resolution or confirmation, active or
    successful runs and an empty failed set refuse. The portal additionally checks native status.
    """
    state = _state_value(run_state)
    if state != "failed":
        return RetryAssessment(False, "run_succeeded" if state == "success" else "run_active", ())
    normalized = {task: _state_value(value) for task, value in task_states.items()}
    if any(value == AMBIGUOUS_TASK_STATE for value in normalized.values()):
        return RetryAssessment(False, "unknown_failed_task", ())
    failed = {task for task, value in normalized.items() if value in FAILED_STATES}
    if not failed:
        return RetryAssessment(False, "no_failed_transport_task", ())
    if failed - KNOWN_TASKS:
        return RetryAssessment(False, "unknown_failed_task", ())
    resolve_state = normalized.get(RESOLUTION_TASK)
    if resolve_state in FAILED_STATES:
        return RetryAssessment(False, "resolution_or_capture_failed", ())
    if resolve_state != "success":
        # No positively successful resolution means no proven frozen input to replay.
        return RetryAssessment(False, "resolution_not_success", ())
    real_failed = {task for task in failed if normalized[task] == "failed"}
    if PUBLICATION_TASK in real_failed:
        return RetryAssessment(False, "publication_failed", ())
    if not real_failed & {START_TASK, TRANSPORT_TASK}:
        return RetryAssessment(False, "no_failed_transport_task", ())
    if real_failed - {START_TASK, TRANSPORT_TASK}:
        return RetryAssessment(False, "native_terminal_failure", ())
    return RetryAssessment(True, "transport_recovery", tuple(sorted(failed)))


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


def retry_assessment_for_run(dag_id: str, run_id: str) -> RetryAssessment:
    """The authoritative classification of one run; a missing row refuses as ``run_missing``."""
    facts = recorded_run_facts(dag_id, run_id)
    if facts is None:
        return RetryAssessment(False, "run_missing", ())
    _, run_state, task_states = facts
    return classify_transport_retry(run_state, task_states)


def owner_retry_allowed(user: Any, dag_id: str, run_id: str) -> bool:
    """True only for the caller's own run that is a positively classified transport recovery."""
    facts = recorded_run_facts(dag_id, run_id)
    if facts is None:
        return False
    principal, run_state, task_states = facts
    if principal is None or principal != user.get_id():
        return False
    return classify_transport_retry(run_state, task_states).eligible
