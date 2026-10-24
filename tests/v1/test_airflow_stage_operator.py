"""The Airflow stage operator is a thin, exact pending/resume adapter."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from airflow.sdk.exceptions import AirflowFailException, TaskDeferred

from description_pipeline.io import digest
from description_pipeline.orchestration import airflow_stage_operator
from description_pipeline.orchestration.airflow_effect_trigger import EffectPollTrigger
from description_pipeline.orchestration.airflow_stage_operator import StageTaskOperator
from description_pipeline.pipeline.contracts import (
    ArtifactRef,
    StageEffectWait,
    StagePending,
    StageResult,
)
from description_pipeline.schemas import (
    STAGE_EFFECT_WAIT_SCHEMA,
    STAGE_RESULT_SCHEMA,
)


DAG_ID = "solidworks_to_urdf"
RUN_ID = "run-1"
DAG_RUN_ID = "portal-run-1"
EXECUTOR_SHA256 = digest({"executor": "capture"})


def _artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id="capture-output",
        kind="stage_output:capture",
        locator="cas://artifact/capture-output",
        manifest_sha256=digest({"manifest": "capture"}),
        set_sha256=digest({"set": "capture"}),
        producer_run_id=RUN_ID,
        producer_stage="capture",
        producer_tool_sha256=EXECUTOR_SHA256,
    )


def _wait() -> StageEffectWait:
    return StageEffectWait(
        schema_version=STAGE_EFFECT_WAIT_SCHEMA,
        run_id=RUN_ID,
        stage="capture",
        command_id="command-capture",
        request_digest=digest({"request": "capture"}),
        effect_id="effect-capture",
        effect_key="native",
        effect_kind="native_operation",
        intent_identity_digest=digest({"intent": "capture"}),
        outbound_artifact_id="capture-outbound",
        outbound_digest=digest({"outbound": "capture"}),
        expected_state_digest=digest({"expected": "capture"}),
    )


def _result() -> StageResult:
    return StageResult(
        schema_version=STAGE_RESULT_SCHEMA,
        command_id="command-capture",
        run_id=RUN_ID,
        stage="capture",
        request_digest=digest({"request": "capture"}),
        outcome="completed",
        output_artifact=_artifact(),
        actual_executor_sha256=EXECUTOR_SHA256,
        checks_sha256=digest({"checks": "capture"}),
        resources_settled=True,
    )


def _context(
    *,
    run_id: str = RUN_ID,
    dag_run_id: str = DAG_RUN_ID,
    task_id: str = "capture",
    map_index: int = -1,
) -> dict[str, object]:
    return {
        "dag": SimpleNamespace(dag_id=DAG_ID),
        "dag_run": SimpleNamespace(
            run_id=dag_run_id,
            conf={"run_id": run_id},
        ),
        "ti": SimpleNamespace(
            task_id=task_id,
            run_id=dag_run_id,
            map_index=map_index,
        ),
    }


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for item in value.values()
            for key in _keys(item)
        }
    if isinstance(value, (list, tuple)):
        return {key for item in value for key in _keys(item)}
    return set()


class StageTaskOperatorTests(unittest.TestCase):
    def test_execute_binds_all_four_real_task_fields_and_returns_result(self) -> None:
        operator = StageTaskOperator(task_id="capture", stage="capture")
        application = Mock()
        application.run_stage.return_value = _result()
        factory = Mock(return_value=application)

        with patch.object(
            airflow_stage_operator,
            "_application_from_binding",
            factory,
        ):
            output = operator.execute(_context(map_index=3))

        binding = factory.call_args.args[0]
        self.assertEqual(binding.dag_id, DAG_ID)
        self.assertEqual(binding.dag_run_id, DAG_RUN_ID)
        self.assertEqual(binding.task_id, "capture")
        self.assertEqual(binding.map_index, 3)
        application.run_stage.assert_called_once_with(RUN_ID, "capture")
        self.assertEqual(output["stage"], "capture")
        self.assertEqual(output["outcome"], "completed")
        self.assertEqual(output["actual_executor_sha256"], EXECUTOR_SHA256)

    def test_pending_defers_with_only_the_canonical_wait(self) -> None:
        wait = _wait()
        operator = StageTaskOperator(
            task_id="capture",
            stage="capture",
            poll_interval=7,
            defer_timeout=90,
        )
        application = Mock()
        application.run_stage.return_value = StagePending(wait)

        with patch.object(
            airflow_stage_operator,
            "_application_from_binding",
            return_value=application,
        ), self.assertRaises(TaskDeferred) as caught:
            operator.execute(_context())

        deferred = caught.exception
        self.assertIsInstance(deferred.trigger, EffectPollTrigger)
        self.assertEqual(deferred.method_name, "execute_complete")
        self.assertEqual(deferred.kwargs, {"wait": wait.to_record()})
        self.assertEqual(deferred.timeout, 90)
        kind, payload = deferred.trigger.serialize()
        self.assertEqual(
            kind,
            "description_pipeline.orchestration.airflow_effect_trigger."
            "EffectPollTrigger",
        )
        self.assertEqual(set(payload), {"poll_interval", "wait"})
        self.assertEqual(payload["wait"], wait.to_record())
        self.assertEqual(payload["poll_interval"], 7)
        self.assertFalse(
            _keys(payload)
            & {
                "claim",
                "claim_id",
                "fence",
                "filesystem",
                "locator",
                "path",
                "token",
            }
        )
        json.dumps(payload, sort_keys=True)

    def test_complete_uses_a_new_task_binding_and_resumes_the_same_wait(self) -> None:
        wait = _wait()
        operator = StageTaskOperator(task_id="capture", stage="capture")
        application = Mock()
        application.resume_stage.return_value = _result()
        factory = Mock(return_value=application)

        with patch.object(
            airflow_stage_operator,
            "_application_from_binding",
            factory,
        ):
            output = operator.execute_complete(
                _context(map_index=5),
                {"wait": wait.to_record()},
            )

        binding = factory.call_args.args[0]
        self.assertEqual(
            (
                binding.dag_id,
                binding.dag_run_id,
                binding.task_id,
                binding.map_index,
            ),
            (DAG_ID, DAG_RUN_ID, "capture", 5),
        )
        application.resume_stage.assert_called_once_with(wait, wakeup=None)
        self.assertEqual(output["result_digest"], _result().digest)

    def test_still_pending_resume_keeps_the_same_wait(self) -> None:
        wait = _wait()
        operator = StageTaskOperator(task_id="capture", stage="capture")
        application = Mock()
        application.resume_stage.return_value = StagePending(wait)

        with patch.object(
            airflow_stage_operator,
            "_application_from_binding",
            return_value=application,
        ), self.assertRaises(TaskDeferred) as caught:
            operator.execute_complete(
                _context(),
                {"wait": wait.to_record()},
            )

        deferred = caught.exception
        self.assertEqual(deferred.method_name, "execute_complete")
        self.assertEqual(deferred.kwargs, {"wait": wait.to_record()})

    def test_stale_run_stage_or_extra_event_field_is_refused_before_application(self) -> None:
        operator = StageTaskOperator(task_id="capture", stage="capture")
        wait = _wait()
        factory = Mock()

        events = (
            (replace(wait, run_id="foreign-run").to_record(), "another run"),
            (replace(wait, stage="publish").to_record(), "another run"),
            ({**wait.to_record(), "claim_id": "foreign-claim"}, "malformed"),
        )
        with patch.object(
            airflow_stage_operator,
            "_application_from_binding",
            factory,
        ):
            for wait_record, message in events:
                with self.subTest(message=message), self.assertRaises(
                    AirflowFailException
                ):
                    operator.execute_complete(
                        _context(),
                        {"wait": wait_record},
                    )
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
