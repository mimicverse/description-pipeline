"""The Airflow trigger observes one exact effect and owns no platform authority."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import digest
from description_pipeline.orchestration import airflow_effect_trigger
from description_pipeline.orchestration.airflow_effect_trigger import (
    EffectPollTrigger,
    _native_wakeable,
    probe_effect_wait,
)
from description_pipeline.orchestration.native_operation_client import (
    NativeOperationClientConflict,
    NativeOperationClientError,
    NativeOperationIdentityConflict,
    NativeOperationNotFound,
    NativeOperationProtocolError,
)
from description_pipeline.pipeline.application import EffectWakeupHint
from description_pipeline.pipeline.contracts import (
    ArtifactRef,
    ProtocolError,
    StageEffectWait,
)
from description_pipeline.pipeline.effects import EffectResumePhase, EffectState
from description_pipeline.schemas import STAGE_EFFECT_WAIT_SCHEMA
import description_pipeline.pipeline.stages.publish as publish_stage


def _wait(
    *,
    kind: str = "native_operation",
    stage: str = "capture",
) -> StageEffectWait:
    return StageEffectWait(
        schema_version=STAGE_EFFECT_WAIT_SCHEMA,
        run_id="run-1",
        stage=stage,
        command_id="command-capture",
        request_digest=digest({"request": "capture"}),
        effect_id="effect-capture",
        effect_key="native",
        effect_kind=kind,
        intent_identity_digest=digest({"intent": "capture"}),
        outbound_artifact_id="capture-outbound",
        outbound_digest=digest({"outbound": "capture"}),
        expected_state_digest=digest({"expected": "capture"}),
    )


def _operation() -> dict[str, object]:
    return {
        "operation_id": "operation-capture",
        "kind": "capture",
        "target_id": digest({"target": "capture"}),
        "request_sha256": digest({"request": "capture"}),
        "native_tool": {
            "requirement_sha256": digest({"tool": "capture"})
        },
        "input": {"archive_sha256": digest({"archive": "capture"})},
        "payload_sha256": digest({"payload": "capture"}),
    }


class EffectPollTriggerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.facts = Mock()
        self.facts.decision_for.return_value = None
        self.facts.effect_settlement.return_value = SimpleNamespace(
            state=EffectState.UNKNOWN, resume_phase=None,
        )
        self.evidence = SimpleNamespace(facts=self.facts, command=Mock())
        self.enterContext(patch.object(
            airflow_effect_trigger, "_load_effect_evidence", return_value=self.evidence,
        ))

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

        with self.assertRaisesRegex(ValueError, "different wait identity"):
            await anext(
                EffectPollTrigger(
                    wait=wait.to_record(),
                    probe=probe,
                ).run()
            )

    async def test_native_probe_uses_only_one_status_get(self) -> None:
        wait = _wait()
        expected = SimpleNamespace(
            operation_id="operation-capture",
            require=Mock(),
        )
        client = Mock()
        client.get_operation.return_value = None

        with patch.object(
            airflow_effect_trigger,
            "_native_probe_from_environment",
            return_value=(client, expected, Mock()),
        ):
            hint = await probe_effect_wait(wait)

        self.assertEqual(hint.wait_digest, wait.digest)
        self.assertFalse(hint.changed)
        client.get_operation.assert_called_once_with("operation-capture")
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()
        client.cancel_operation.assert_not_called()

    async def test_native_probe_records_activity_without_making_it_state_authority(
        self,
    ) -> None:
        wait = _wait()
        activity = {
            "seq": 1,
            "at": "2026-10-26T02:00:00Z",
            "phase": "capture",
            "action": "readings",
            "current_object": "cad/robot.SLDASM",
            "completed": 1,
            "total": 3,
            "unit": "documents",
        }
        view = SimpleNamespace(
            operation_id="operation-capture",
            state="running",
            resource_terminated=False,
            activity=activity,
        )
        expected = SimpleNamespace(
            operation_id="operation-capture",
            require=Mock(return_value=view),
        )
        client = Mock()
        client.get_operation.return_value = view
        facts = Mock()

        with patch.object(
            airflow_effect_trigger,
            "_native_probe_from_environment",
            return_value=(client, expected, facts),
        ):
            hint = await probe_effect_wait(wait)

        self.assertFalse(hint.changed)
        facts.record_run_activity.assert_called_once_with(
            wait.run_id,
            "operation-capture",
            activity,
            terminal=False,
        )

    async def test_poll_reuses_one_exact_identity_loader(self) -> None:
        wait = _wait()
        terminal = SimpleNamespace(
            state="satisfied",
            resource_terminated=True,
        )
        expected = SimpleNamespace(
            operation_id="operation-capture",
            require=Mock(),
        )
        client = Mock()
        client.get_operation.side_effect = [None, terminal]
        loader = Mock(return_value=(client, expected, Mock()))

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
        loader.assert_called_once_with(wait, self.evidence)
        self.assertEqual(client.get_operation.call_count, 2)
        sleep.assert_awaited_once_with(0.1)

    async def test_transient_observation_errors_keep_the_same_wait_pending(self) -> None:
        wait = _wait()
        accepted = SimpleNamespace(
            state="accepted",
            resource_terminated=False,
        )
        expected = SimpleNamespace(
            operation_id="operation-capture",
            require=Mock(return_value=accepted),
        )
        client = Mock()
        client.get_operation.side_effect = [
            NativeOperationClientError("native endpoint is unreachable"),
            NativeOperationProtocolError(
                "native endpoint response was incomplete"
            ),
            NativeOperationClientConflict(
                "routed to another target",
                status=409,
                code="native_target_mismatch",
            ),
            NativeOperationClientError(
                "native endpoint is unavailable",
                status=503,
            ),
            NativeOperationNotFound("operation has not arrived", status=404),
            accepted,
        ]

        with (
            patch.object(
                airflow_effect_trigger,
                "_native_probe_from_environment",
                return_value=(client, expected, Mock()),
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
        self.assertEqual(client.get_operation.call_count, 6)
        self.assertEqual(sleep.await_count, 5)
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()
        client.cancel_operation.assert_not_called()

    async def test_authorization_failure_aborts_observation(self) -> None:
        wait = _wait()
        expected = SimpleNamespace(operation_id="operation-capture")
        for status in (401, 403):
            with self.subTest(status=status):
                client = Mock()
                client.get_operation.side_effect = NativeOperationClientError(
                    "unauthorized",
                    status=status,
                )

                with (
                    patch.object(
                        airflow_effect_trigger,
                        "_native_probe_from_environment",
                        return_value=(client, expected, Mock()),
                    ),
                    self.assertRaises(NativeOperationClientError),
                ):
                    await anext(
                        EffectPollTrigger(
                            wait=wait.to_record(),
                            poll_interval=0.1,
                        ).run()
                    )

    async def test_operation_identity_conflict_aborts_observation(self) -> None:
        wait = _wait()
        expected = SimpleNamespace(operation_id="operation-capture")
        client = Mock()
        client.get_operation.side_effect = NativeOperationIdentityConflict(
            "native operation endpoint answered for another operation",
            status=409,
            code="native_operation_identity_mismatch",
        )

        with (
            patch.object(
                airflow_effect_trigger,
                "_native_probe_from_environment",
                return_value=(client, expected, Mock()),
            ),
            self.assertRaises(NativeOperationIdentityConflict),
        ):
            await anext(
                EffectPollTrigger(
                    wait=wait.to_record(),
                    poll_interval=0.1,
                ).run()
            )

    async def test_non_target_conflict_without_status_aborts_observation(self) -> None:
        wait = _wait()
        expected = SimpleNamespace(operation_id="operation-capture")
        client = Mock()
        client.get_operation.side_effect = NativeOperationClientConflict(
            "native operation differs from the frozen request",
            code="native_operation_conflict",
        )

        with (
            patch.object(
                airflow_effect_trigger,
                "_native_probe_from_environment",
                return_value=(client, expected, Mock()),
            ),
            self.assertRaises(NativeOperationClientConflict),
        ):
            await anext(
                EffectPollTrigger(
                    wait=wait.to_record(),
                    poll_interval=0.1,
                ).run()
            )

    async def test_foreign_view_identity_aborts_without_target_switch(self) -> None:
        wait = _wait()
        view = SimpleNamespace(
            state="accepted",
            resource_terminated=False,
        )
        conflict = NativeOperationClientConflict(
            "native operation differs from the frozen request",
            status=409,
            code="native_operation_conflict",
        )
        expected = SimpleNamespace(
            operation_id="operation-capture",
            require=Mock(side_effect=conflict),
        )
        client = Mock()
        client.get_operation.return_value = view

        with (
            patch.object(
                airflow_effect_trigger,
                "_native_probe_from_environment",
                return_value=(client, expected, Mock()),
            ),
            self.assertRaises(NativeOperationClientConflict),
        ):
            await anext(
                EffectPollTrigger(
                    wait=wait.to_record(),
                    poll_interval=0.1,
                ).run()
            )

        expected.require.assert_called_once_with(view)
        client.get_target_identity.assert_not_called()
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()

    async def test_absence_and_nonterminal_status_do_not_wake(self) -> None:
        wait = _wait()
        views = (
            None,
            SimpleNamespace(state="running", resource_terminated=False),
            SimpleNamespace(state="awaiting_input", resource_terminated=False),
            SimpleNamespace(state="satisfied", resource_terminated=False),
            SimpleNamespace(state="unknown", resource_terminated=False),
        )
        for view in views:
            with self.subTest(view=view):
                expected = SimpleNamespace(
                    operation_id="operation-capture",
                    require=Mock(),
                )
                client = Mock()
                client.get_operation.return_value = view
                with patch.object(
                    airflow_effect_trigger,
                    "_native_probe_from_environment",
                    return_value=(client, expected, Mock()),
                ):
                    hint = await probe_effect_wait(wait)
                self.assertFalse(hint.changed)

    def test_accepted_operation_and_settled_terminal_status_wake(self) -> None:
        self.assertTrue(
            _native_wakeable(
                SimpleNamespace(state="accepted", resource_terminated=False)
            )
        )
        for state in ("running", "awaiting_input", "satisfied", "failed"):
            with self.subTest(state=state):
                self.assertFalse(
                    _native_wakeable(
                        SimpleNamespace(
                            state=state,
                            resource_terminated=False,
                        )
                    )
                )
        self.assertFalse(_native_wakeable(None))
        for state in ("satisfied", "failed", "cancelled", "unknown"):
            with self.subTest(state=state):
                self.assertTrue(
                    _native_wakeable(
                        SimpleNamespace(
                            state=state,
                            resource_terminated=True,
                        )
                    )
                )

    async def test_all_waiting_effect_kinds_use_the_common_probe_dispatch(self) -> None:
        async def changed(value: StageEffectWait) -> EffectWakeupHint:
            return EffectWakeupHint(
                wait_digest=value.digest,
                observed_at=datetime(2026, 10, 25, tzinfo=UTC),
                changed=True,
            )

        for kind in ("native_operation", "git_ref_update", "pr_upsert"):
            with self.subTest(kind=kind):
                wait = replace(_wait(), effect_kind=kind, stage="publish")
                dispatcher = AsyncMock(return_value=changed)
                with patch.object(
                    airflow_effect_trigger,
                    "_prepare_effect_probe",
                    dispatcher,
                ):
                    hint = await probe_effect_wait(wait)
                self.assertTrue(hint.changed)
                dispatcher.assert_awaited_once_with(wait)

    async def test_canonical_results_wake_every_kind_without_a_remote_change(self) -> None:
        for kind in ("native_operation", "git_ref_update", "pr_upsert"):
            for state in (
                EffectState.SATISFIED, EffectState.DEFINITELY_FAILED,
                EffectState.CONFLICT,
            ):
                with self.subTest(kind=kind, state=state):
                    wait = _wait(kind=kind)
                    self.facts.effect_settlement.return_value = SimpleNamespace(
                        state=state, resume_phase=None,
                    )
                    with (
                        patch.object(airflow_effect_trigger, "_prepare_native_effect_probe", new=AsyncMock()) as native,
                        patch.object(
                            airflow_effect_trigger, "_prepare_publish_effect_probe", new=AsyncMock(),
                        ) as publish,
                    ):
                        event = await anext(EffectPollTrigger(wait=wait.to_record()).run())
                    self.assertEqual(event.payload, {"wait": wait.to_record()})
                    native.assert_not_awaited()
                    publish.assert_not_awaited()

    async def test_canonical_decision_and_transfer_resume_wake_without_remote_access(self) -> None:
        for decision in ("completed", "failed", "cancelled", None):
            with self.subTest(decision=decision):
                wait = _wait()
                self.facts.decision_for.return_value = (
                    SimpleNamespace(outcome=decision) if decision else None
                )
                self.facts.effect_settlement.return_value = SimpleNamespace(
                    state=EffectState.UNKNOWN,
                    resume_phase=EffectResumePhase.INPUT_TRANSFER_PENDING if decision is None else None,
                )
                with patch.object(
                    airflow_effect_trigger, "_prepare_native_effect_probe", new=AsyncMock(),
                ) as remote:
                    hint = await probe_effect_wait(wait)
                self.assertTrue(hint.changed)
                remote.assert_not_awaited()

    async def test_running_trigger_rereads_canonical_settlement_on_each_poll(self) -> None:
        for kind in ("native_operation", "git_ref_update", "pr_upsert"):
            with self.subTest(kind=kind):
                wait = _wait(kind=kind)
                self.facts.effect_settlement.side_effect = [
                    SimpleNamespace(state=EffectState.UNKNOWN, resume_phase=None),
                    SimpleNamespace(state=EffectState.DEFINITELY_FAILED, resume_phase=None),
                ]
                remote = AsyncMock(return_value=EffectWakeupHint(
                    wait_digest=wait.digest, observed_at=datetime.now(UTC), changed=False,
                ))
                with (
                    patch.object(
                        airflow_effect_trigger, "_prepare_native_effect_probe", new=AsyncMock(return_value=remote),
                    ),
                    patch.object(
                        airflow_effect_trigger, "_prepare_publish_effect_probe", new=AsyncMock(return_value=remote),
                    ),
                    patch.object(airflow_effect_trigger.asyncio, "sleep", new=AsyncMock()),
                ):
                    event = await anext(EffectPollTrigger(wait=wait.to_record()).run())
                self.assertEqual(event.payload, {"wait": wait.to_record()})
                remote.assert_awaited_once_with(wait)
                self.facts.effect_settlement.assert_called_with(
                    wait.effect_id, expected_command_id=wait.command_id,
                )

    async def test_canonical_read_failure_aborts_without_remote_guessing(self) -> None:
        self.facts.decision_for.side_effect = RuntimeError("canonical reader unavailable")
        with (
            patch.object(airflow_effect_trigger, "_prepare_native_effect_probe", new=AsyncMock()) as remote,
            self.assertRaisesRegex(RuntimeError, "canonical reader unavailable"),
        ):
            await probe_effect_wait(_wait())
        remote.assert_not_awaited()

    async def test_publish_probes_use_direct_cas_root_without_materializing(self) -> None:
        for kind, probe_name in (
            ("git_ref_update", "PublishGitStatusProbe"),
            ("pr_upsert", "PublishPullRequestStatusProbe"),
        ):
            with self.subTest(kind=kind):
                wait = replace(
                    _wait(),
                    effect_kind=kind,
                    stage="publish",
                )
                outbound = ArtifactRef(
                    artifact_id="publish-outbound",
                    kind="effect_outbound",
                    locator="cas://artifact/publish-outbound",
                    manifest_sha256=digest({"manifest": kind}),
                    set_sha256=digest({"set": kind}),
                    producer_run_id="run-1",
                    producer_stage="publish",
                    producer_tool_sha256=digest({"tool": "publish"}),
                )
                artifacts = SimpleNamespace(
                    objects=Path("/isolated/cas/objects"),
                    read_metadata_member=Mock(
                        side_effect=lambda _artifact, member, *, max_bytes: (
                            b"{}",
                            {"path": member},
                            {"members": []},
                        )
                    ),
                    materialize=Mock(
                        side_effect=AssertionError(
                            "trigger must not materialize"
                        )
                    ),
                )
                evidence = SimpleNamespace(
                    artifacts=artifacts,
                    settlement=SimpleNamespace(outbound_artifact=outbound),
                    facts=self.facts,
                    command=self.evidence.command,
                )
                status_probe = SimpleNamespace(
                    observe=AsyncMock(
                        return_value=EffectWakeupHint(
                            wait_digest=wait.digest,
                            observed_at=datetime(
                                2026,
                                10,
                                25,
                                tzinfo=UTC,
                            ),
                            changed=True,
                        )
                    )
                )
                factory = Mock(return_value=status_probe)
                with (
                    patch.object(
                        airflow_effect_trigger,
                        "_load_effect_evidence",
                        return_value=evidence,
                    ),
                    patch.object(
                        publish_stage,
                        probe_name,
                        SimpleNamespace(from_sealed_outbound=factory),
                    ),
                ):
                    hint = await probe_effect_wait(wait)
                self.assertTrue(hint.changed)
                factory.assert_called_once_with(
                    wait=wait,
                    outbound=outbound,
                    outbound_root=(
                        artifacts.objects / outbound.artifact_id
                    ),
                )
                artifacts.materialize.assert_not_called()
                expected_metadata_reads = (
                    2 if kind == "git_ref_update" else 1
                )
                self.assertEqual(
                    artifacts.read_metadata_member.call_count,
                    expected_metadata_reads,
                )

    def test_small_member_read_does_not_materialize_or_hash_large_payload(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="effect-small-member-"
        ) as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "request.json").write_bytes(
                b'{"command_id":"command-capture"}'
            )
            (source / "large.bin").write_bytes(b"x" * 1024)
            artifacts = ArtifactStore(root / "cas")
            artifact = artifacts.seal_tree(
                source,
                kind="effect_outbound",
                producer_run_id="run-1",
                producer_stage="capture",
                producer_tool_sha256=digest({"tool": "capture"}),
            )
            with patch.object(
                artifacts,
                "materialize",
                side_effect=AssertionError("materialize must not be called"),
            ):
                content, member, _manifest = artifacts.read_metadata_member(
                    artifact,
                    "request.json",
                    max_bytes=1024,
                )
        self.assertEqual(content, b'{"command_id":"command-capture"}')
        self.assertEqual(member["path"], "request.json")

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

    def test_native_expectation_requires_complete_immutable_identity(self) -> None:
        operation = _operation()
        expected = airflow_effect_trigger._expectation_from_operation(operation)
        self.assertEqual(expected.operation_id, "operation-capture")
        self.assertEqual(expected.target_id, operation["target_id"])
        self.assertEqual(
            expected.input_archive_sha256,
            operation["input"]["archive_sha256"],
        )
        self.assertEqual(
            expected.payload_sha256,
            operation["payload_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
