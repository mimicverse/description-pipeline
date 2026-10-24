"""The Airflow trigger observes one exact effect and owns no platform authority."""

from __future__ import annotations

import json
import subprocess
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
    _GitProbeTarget,
    _PullRequestProbeTarget,
    _native_wakeable,
    probe_effect_wait,
)
from description_pipeline.pipeline.application import EffectWakeupHint
from description_pipeline.pipeline.contracts import (
    ProtocolError,
    StageEffectWait,
)
from description_pipeline.pipeline.effects import EffectSettlement, EffectState
from description_pipeline.repository.publish import PullRequestObservation
from description_pipeline.schemas import STAGE_EFFECT_WAIT_SCHEMA


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
        "request_sha256": digest({"request": "capture"}),
        "native_tool": {
            "requirement_sha256": digest({"tool": "capture"})
        },
        "input": {"archive_sha256": digest({"archive": "capture"})},
        "payload_sha256": digest({"payload": "capture"}),
    }


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
            return_value=(client, expected),
        ):
            hint = await probe_effect_wait(wait)

        self.assertEqual(hint.wait_digest, wait.digest)
        self.assertFalse(hint.changed)
        client.get_operation.assert_called_once_with("operation-capture")
        client.create_operation.assert_not_called()
        client.upload_input.assert_not_called()
        client.cancel_operation.assert_not_called()

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
        loader = Mock(return_value=(client, expected))

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
        self.assertEqual(client.get_operation.call_count, 2)
        sleep.assert_awaited_once_with(0.1)

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
                    return_value=(client, expected),
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

    def test_git_observation_is_read_only_and_requires_exact_head(self) -> None:
        target = _GitProbeTarget(
            origin_url="https://example.test/example/robot.git",
            branch_ref="review/run-1",
            candidate_commit="a" * 40,
        )
        exact = subprocess.CompletedProcess(
            [],
            0,
            stdout=f"{'a' * 40}\trefs/heads/review/run-1\n",
            stderr="",
        )
        with patch.object(
            airflow_effect_trigger.subprocess,
            "run",
            return_value=exact,
        ) as run:
            self.assertTrue(airflow_effect_trigger._observe_git_branch(target))
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["git", "ls-remote"])
        self.assertNotIn("push", command)

        for output in ("", f"{'b' * 40}\trefs/heads/review/run-1\n"):
            with self.subTest(output=output), patch.object(
                airflow_effect_trigger.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=output,
                    stderr="",
                ),
            ):
                self.assertFalse(
                    airflow_effect_trigger._observe_git_branch(target)
                )

    def test_pull_request_observation_requires_one_exact_open_request(self) -> None:
        target = _PullRequestProbeTarget(
            repository="example/robot",
            base_ref="main",
            head_ref="review/run-1",
            candidate_commit="a" * 40,
            title="Ship robot",
            body="Evidence",
            expected_pr=None,
        )
        exact = PullRequestObservation(
            number=7,
            url="https://example.test/example/robot/pull/7",
            state="OPEN",
            base_ref="main",
            head_ref="review/run-1",
            head_oid="a" * 40,
            title="Ship robot",
            body="Evidence",
        )
        client = Mock()
        client.observe.return_value = (exact,)
        with patch(
            "description_pipeline.repository.publish."
            "GitHubCliPullRequestClient",
            return_value=client,
        ):
            self.assertTrue(
                airflow_effect_trigger._observe_pull_request(target)
            )
        client.observe.assert_called_once_with(
            repository="example/robot",
            base_ref="main",
            head_ref="review/run-1",
            candidate_commit="a" * 40,
        )
        client.create.assert_not_called()
        client.update.assert_not_called()

        for observations in ((), (exact, exact), (replace(exact, title="other"),)):
            with self.subTest(observations=observations):
                client = Mock()
                client.observe.return_value = observations
                with patch(
                    "description_pipeline.repository.publish."
                    "GitHubCliPullRequestClient",
                    return_value=client,
                ):
                    self.assertFalse(
                        airflow_effect_trigger._observe_pull_request(target)
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
                content, member, _manifest = (
                    airflow_effect_trigger._read_small_member(
                        artifacts,
                        artifact,
                        "request.json",
                        max_bytes=1024,
                    )
                )
        self.assertEqual(content, b'{"command_id":"command-capture"}')
        self.assertEqual(member["path"], "request.json")

    def test_git_and_pull_request_targets_come_from_small_canonical_requests(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="publish-probe-identity-"
        ) as directory:
            root = Path(directory)
            artifacts = ArtifactStore(root / "cas")
            source = root / "source"
            source.mkdir()

            git_state = {
                "schema_version": "solidworks-to-urdf.git-ref-update/v1",
                "repository": "example/robot",
                "base_ref": "main",
                "expected_base_oid": "a" * 40,
                "branch_ref": "review/run-1",
                "expected_head_oid": None,
                "candidate_commit": "b" * 40,
                "candidate_tree": "c" * 40,
                "subject_sha256": digest({"subject": "git"}),
            }
            (source / "request.json").write_bytes(
                json.dumps(
                    {**git_state, "parents": ["a" * 40]},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
            git_outbound = artifacts.seal_tree(
                source,
                kind="effect_outbound",
                producer_run_id="run-1",
                producer_stage="publish",
                producer_tool_sha256=digest({"tool": "publish"}),
            )
            git_evidence = SimpleNamespace(
                artifacts=artifacts,
                command=SimpleNamespace(
                    request={"origin_url": "https://example.test/example/robot.git"}
                ),
                intent=SimpleNamespace(
                    effect_kind="git_ref_update",
                    target={
                        "repository": "example/robot",
                        "branch_ref": "review/run-1",
                    },
                    expected_state=git_state,
                ),
                settlement=EffectSettlement(
                    effect_id="effect-git",
                    state=EffectState.UNKNOWN,
                    decisive_fact=None,
                    outbound_artifact=git_outbound,
                ),
            )
            git_target = airflow_effect_trigger._git_probe_target(git_evidence)
            self.assertEqual(git_target.branch_ref, "review/run-1")
            self.assertEqual(git_target.candidate_commit, "b" * 40)

            (source / "request.json").unlink()
            pr_state = {
                "schema_version": "solidworks-to-urdf.pr-upsert/v1",
                "repository": "example/robot",
                "base_ref": "main",
                "head_ref": "review/run-1",
                "candidate_commit": "b" * 40,
                "subject_sha256": digest({"subject": "pr"}),
                "title_digest": digest({"title": "Ship robot"}),
                "body_digest": digest({"body": "Evidence"}),
                "expected_pr": None,
            }
            (source / "request.json").write_bytes(
                json.dumps(
                    {
                        **pr_state,
                        "title": "Ship robot",
                        "body": "Evidence",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
            pr_outbound = artifacts.seal_tree(
                source,
                kind="effect_outbound",
                producer_run_id="run-1",
                producer_stage="publish",
                producer_tool_sha256=digest({"tool": "publish"}),
            )
            pr_evidence = SimpleNamespace(
                artifacts=artifacts,
                command=SimpleNamespace(request={}),
                intent=SimpleNamespace(
                    effect_kind="pr_upsert",
                    target={
                        "repository": "example/robot",
                        "base_ref": "main",
                        "head_ref": "review/run-1",
                    },
                    expected_state=pr_state,
                ),
                settlement=EffectSettlement(
                    effect_id="effect-pr",
                    state=EffectState.UNKNOWN,
                    decisive_fact=None,
                    outbound_artifact=pr_outbound,
                ),
            )
            with patch.object(
                artifacts,
                "materialize",
                side_effect=AssertionError("materialize must not be called"),
            ):
                pr_target = airflow_effect_trigger._pull_request_probe_target(
                    pr_evidence
                )
            self.assertEqual(pr_target.title, "Ship robot")
            self.assertEqual(pr_target.head_ref, "review/run-1")

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
