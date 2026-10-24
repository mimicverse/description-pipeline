"""The Airflow trigger observes one exact effect and owns no platform authority."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from description_pipeline.io import digest
from description_pipeline.orchestration import airflow_effect_trigger
from description_pipeline.orchestration.airflow_effect_trigger import (
    EffectPollTrigger,
    _status_changed,
    probe_effect_wait,
)
from description_pipeline.pipeline.application import EffectWakeupHint
from description_pipeline.pipeline.contracts import (
    ProtocolError,
    StageEffectWait,
)
from description_pipeline.schemas import STAGE_EFFECT_WAIT_SCHEMA


def _wait() -> StageEffectWait:
    return StageEffectWait(
        schema_version=STAGE_EFFECT_WAIT_SCHEMA,
        run_id="run-1",
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


class EffectPollTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_observation_emits_only_the_revalidated_wait(self) -> None:
        wait = _wait()
        calls = 0

        async def probe(value: StageEffectWait) -> EffectWakeupHint:
            nonlocal calls
            calls += 1
            self.assertEqual(value, wait)
            return EffectWakeupHint(
                wait_digest=wait.digest,
                observed_at=datetime(2026, 10, 25, tzinfo=UTC),
                changed=calls == 2,
            )

        trigger = EffectPollTrigger(
            wait=wait.to_record(),
            poll_interval=0.1,
            probe=probe,
        )
        with patch.object(
            airflow_effect_trigger.asyncio,
            "sleep",
            new=AsyncMock(),
        ) as sleep:
            event = await anext(trigger.run())

        self.assertEqual(calls, 2)
        sleep.assert_awaited_once_with(0.1)
        self.assertEqual(event.payload, {"wait": wait.to_record()})

    async def test_wrong_wait_digest_is_refused(self) -> None:
        wait = _wait()

        async def probe(_: StageEffectWait) -> EffectWakeupHint:
            return EffectWakeupHint(
                wait_digest="0" * 64,
                observed_at=datetime(2026, 10, 25, tzinfo=UTC),
                changed=True,
            )

        trigger = EffectPollTrigger(
            wait=wait.to_record(),
            probe=probe,
        )
        with self.assertRaisesRegex(ValueError, "different wait identity"):
            await anext(trigger.run())

    async def test_probe_uses_only_the_read_only_status_call(self) -> None:
        wait = _wait()
        expected = object()
        client = Mock()
        client.probe_operation_status_async = AsyncMock(return_value=None)

        with patch.object(
            airflow_effect_trigger,
            "_native_probe_from_environment",
            return_value=(client, expected),
        ):
            hint = await probe_effect_wait(wait)

        self.assertEqual(hint.wait_digest, wait.digest)
        self.assertFalse(hint.changed)
        client.probe_operation_status_async.assert_awaited_once_with(expected)
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()
        client.cancel_operation.assert_not_called()

    async def test_poll_reuses_one_exact_identity_loader(self) -> None:
        wait = _wait()
        terminal = SimpleNamespace(
            state="satisfied",
            resource_terminated=True,
        )
        client = Mock()
        client.probe_operation_status_async = AsyncMock(
            side_effect=[None, terminal]
        )
        loader = Mock(return_value=(client, object()))

        with (
            patch.object(
                airflow_effect_trigger,
                "_native_probe_from_environment",
                loader,
            ),
            patch.object(
                airflow_effect_trigger.asyncio,
                "sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            event = await anext(
                EffectPollTrigger(
                    wait=wait.to_record(),
                    poll_interval=0.1,
                ).run()
            )

        self.assertEqual(event.payload, {"wait": wait.to_record()})
        loader.assert_called_once_with(wait)
        self.assertEqual(client.probe_operation_status_async.await_count, 2)
        sleep.assert_awaited_once_with(0.1)

    async def test_absence_and_nonterminal_status_do_not_wake(self) -> None:
        wait = _wait()
        views = (
            None,
            SimpleNamespace(state="running", resource_terminated=False),
            SimpleNamespace(state="satisfied", resource_terminated=False),
            SimpleNamespace(state="unknown", resource_terminated=False),
        )
        for view in views:
            with self.subTest(view=view):
                client = Mock()
                client.probe_operation_status_async = AsyncMock(
                    return_value=view
                )
                with patch.object(
                    airflow_effect_trigger,
                    "_native_probe_from_environment",
                    return_value=(client, object()),
                ):
                    hint = await probe_effect_wait(wait)
                self.assertFalse(hint.changed)

    def test_only_settled_terminal_status_can_wake(self) -> None:
        for state in ("running", "accepted", "satisfied", "failed"):
            with self.subTest(state=state):
                self.assertFalse(
                    _status_changed(
                        SimpleNamespace(
                            state=state,
                            resource_terminated=False,
                        )
                    )
                )
        self.assertFalse(_status_changed(None))
        for state in ("satisfied", "failed", "cancelled", "unknown"):
            with self.subTest(state=state):
                self.assertTrue(
                    _status_changed(
                        SimpleNamespace(
                            state=state,
                            resource_terminated=True,
                        )
                    )
                )

    def test_serialization_excludes_locator_claim_and_fence(self) -> None:
        wait = _wait()
        kind, payload = EffectPollTrigger(
            wait=wait.to_record(),
            poll_interval=2,
        ).serialize()

        self.assertEqual(
            kind,
            "description_pipeline.orchestration.airflow_effect_trigger."
            "EffectPollTrigger",
        )
        self.assertEqual(set(payload), {"poll_interval", "wait"})
        self.assertEqual(payload["wait"], wait.to_record())
        text = json.dumps(payload, sort_keys=True)
        for forbidden in (
            "claim_id",
            "filesystem",
            "fence",
            "locator",
            "token",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_constructor_rejects_noncanonical_wait_fields(self) -> None:
        record = {**_wait().to_record(), "claim_id": "foreign-claim"}
        with self.assertRaises(ProtocolError):
            EffectPollTrigger(wait=record)

    def test_native_status_expectation_requires_immutable_archive_and_payload(self) -> None:
        identity = SimpleNamespace(
            operation_id="operation-capture",
            kind="capture",
            request_sha256=digest({"request": "capture"}),
            native_tool_requirement_sha256=digest({"tool": "capture"}),
            input_archive_sha256=digest({"archive": "capture"}),
            payload_sha256=digest({"payload": "capture"}),
        )
        expected = airflow_effect_trigger._native_status_expectation(identity)
        self.assertEqual(expected.operation_id, identity.operation_id)
        self.assertEqual(
            expected.input_archive_sha256,
            identity.input_archive_sha256,
        )
        self.assertEqual(expected.payload_sha256, identity.payload_sha256)

        for field in ("input_archive_sha256", "payload_sha256"):
            with self.subTest(field=field):
                invalid = SimpleNamespace(**vars(identity))
                setattr(invalid, field, None)
                with self.assertRaisesRegex(ValueError, "identity"):
                    airflow_effect_trigger._native_status_expectation(invalid)


if __name__ == "__main__":
    unittest.main()
