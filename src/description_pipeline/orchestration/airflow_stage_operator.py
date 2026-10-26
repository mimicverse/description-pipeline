"""Class-based Airflow adapter for one canonical StageApplication command."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from airflow.models import BaseOperator
from airflow.sdk.exceptions import AirflowFailException

from ..pipeline.application import (
    AirflowTaskBinding,
    ApplicationError,
    StageApplication,
    application_from_environment,
)
from ..pipeline.contracts import (
    FailureEnvelope,
    StageId,
    StageResult,
)
from .airflow_effect_trigger import EffectPollTrigger

if TYPE_CHECKING:
    from ..pipeline.contracts import StageEffectWait


def _binding_from_context(
    *,
    stage: StageId,
    expected_task_id: str,
    expected_dag_id: str,
    context: Mapping[str, Any],
) -> tuple[AirflowTaskBinding, str]:
    dag_run = context.get("dag_run")
    task_instance = context.get("ti")
    dag = context.get("dag")
    dag_id = getattr(dag, "dag_id", None)
    if dag_id != expected_dag_id:
        raise AirflowFailException(
            f"Airflow dag_id {dag_id!r} differs from operator dag_id "
            f"{expected_dag_id!r}"
        )
    dag_run_id = getattr(dag_run, "run_id", None)
    task_id = getattr(task_instance, "task_id", None)
    map_index = getattr(task_instance, "map_index", None)
    if task_id != expected_task_id or task_id != stage:
        raise AirflowFailException(
            f"Airflow task_id {task_id!r} differs from stage {stage!r}"
        )
    ti_run_id = getattr(task_instance, "run_id", None)
    if ti_run_id is not None and ti_run_id != dag_run_id:
        raise AirflowFailException(
            "TaskInstance and DagRun identities differ"
        )
    conf = getattr(dag_run, "conf", None)
    conf = conf if isinstance(conf, dict) else {}
    run_id = conf.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise AirflowFailException("dag_run.conf.run_id is required")
    try:
        binding = AirflowTaskBinding(
            dag_id=dag_id,
            dag_run_id=dag_run_id,
            task_id=task_id,
            map_index=map_index,
        )
    except ApplicationError as error:
        raise AirflowFailException(f"{error.code}: {error}") from error
    return binding, run_id


def _application_from_binding(binding: AirflowTaskBinding) -> StageApplication:
    try:
        return application_from_environment(airflow_task_binding=binding)
    except ApplicationError as error:
        raise AirflowFailException(f"{error.code}: {error}") from error


def _wait_from_record(
    record: object,
    *,
    message: str,
) -> StageEffectWait:
    from ..pipeline.contracts import StageEffectWait

    try:
        return StageEffectWait.from_record(record)
    except Exception as error:
        raise AirflowFailException(message) from error


def _event_wait(event: object) -> StageEffectWait:
    if not isinstance(event, Mapping) or set(event) != {"wait"}:
        raise AirflowFailException("The effect event is malformed")
    return _wait_from_record(
        event["wait"],
        message="The effect event contains an invalid wait",
    )


class StageTaskOperator(BaseOperator):
    """Run or resume one canonical stage without copying stage business logic."""

    template_fields: Sequence[str] = ()
    ui_color = "#d9e7f5"

    def __init__(
        self,
        *,
        stage: StageId,
        poll_interval: float = 5.0,
        defer_timeout: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if stage not in {
            "freeze",
            "discover",
            "capture",
            "generate",
            "verify",
            "publish",
        }:
            raise ValueError(f"Unknown stage {stage!r}")
        if self.task_id != stage:
            raise ValueError("StageTaskOperator task_id must equal stage")
        if isinstance(poll_interval, bool) or not isinstance(
            poll_interval,
            (int, float),
        ):
            raise ValueError("poll_interval must be a number")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if defer_timeout is not None and defer_timeout <= 0:
            raise ValueError("defer_timeout must be positive or None")
        self.stage = stage
        self.poll_interval = float(poll_interval)
        self.defer_timeout = (
            None if defer_timeout is None else float(defer_timeout)
        )

    def execute(self, context: Mapping[str, Any]) -> dict[str, Any]:
        binding, run_id = _binding_from_context(
            stage=self.stage,
            expected_task_id=self.task_id,
            expected_dag_id=self.dag_id,
            context=context,
        )
        application = _application_from_binding(binding)
        try:
            result = application.run_stage(run_id, self.stage)
        except ApplicationError as error:
            raise AirflowFailException(
                json.dumps(
                    {
                        "code": error.code,
                        "message": str(error),
                        "detail": error.detail,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ) from error
        return self._handle_result(result)

    def execute_complete(
        self,
        context: Mapping[str, Any],
        wait: object,
        event: object,
    ) -> dict[str, Any]:
        """Resume from the persisted wait and require the event to match it."""

        binding, run_id = _binding_from_context(
            stage=self.stage,
            expected_task_id=self.task_id,
            expected_dag_id=self.dag_id,
            context=context,
        )
        expected = _wait_from_record(
            wait,
            message="The persisted effect wait is invalid",
        )
        hint = _event_wait(event)
        if expected.digest != hint.digest:
            raise AirflowFailException(
                "The effect event names another persisted wait"
            )
        if expected.run_id != run_id or expected.stage != self.stage:
            raise AirflowFailException(
                "The effect event names another run or stage"
            )
        application = _application_from_binding(binding)
        try:
            result = application.resume_stage(expected, wakeup=None)
        except ApplicationError as error:
            raise AirflowFailException(
                json.dumps(
                    {
                        "code": error.code,
                        "message": str(error),
                        "detail": error.detail,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ) from error
        return self._handle_result(result)

    def _handle_result(self, result: object) -> dict[str, Any]:
        from ..pipeline.contracts import StagePending

        if isinstance(result, StagePending):
            wait_record = result.wait.to_record()
            return self.defer(
                trigger=EffectPollTrigger(
                    wait=wait_record,
                    poll_interval=self.poll_interval,
                ),
                method_name="execute_complete",
                kwargs={"wait": wait_record},
                timeout=self.defer_timeout,
            )
        if isinstance(result, FailureEnvelope):
            raise AirflowFailException(
                json.dumps(result.to_record(), ensure_ascii=False, sort_keys=True)
            )
        if not isinstance(result, StageResult) or result.outcome != "completed":
            raise AirflowFailException(
                "The stage did not return one completed StageResult"
            )
        return {
            "stage": result.stage,
            "outcome": result.outcome,
            "result_digest": result.digest,
            "output_artifact": result.output_artifact.to_record(),
            "actual_executor_sha256": result.actual_executor_sha256,
        }


__all__ = ["StageTaskOperator"]
