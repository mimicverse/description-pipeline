"""PostgreSQL proof for serialized cancellation of an existing stage command."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.io import digest, write_json
from description_pipeline.pipeline.cancellation import (
    CancellationRequest,
    CancellationControlService,
)
from description_pipeline.pipeline.contracts import (
    DispatchCommand,
    DispatchConf,
    FailureEnvelope,
    StageCommand,
    StageDecision,
    StageResult,
)
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.engine import (
    execute_cancellation,
    execute_stage,
)
from description_pipeline.pipeline.contracts import StageExecution
from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.pipeline.reconciliation import reconciliation_metadata_document
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.pipeline.services import BoundStageEffectPort
from description_pipeline.platform.facts import FactConflict, FactStore
from description_pipeline.schemas import AIRFLOW_DISPATCH_CONF_SCHEMA
from tests.v1.test_pipeline_admission import AdmissionHarness

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = Path(__file__).resolve().parents[2] / "deploy" / "platform" / "schema.sql"


class _ForbiddenHandler:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("A pre-send cancellation must not start the native handler")

    def validate_candidate(self, *_args, **_kwargs):
        raise AssertionError("A pre-send cancellation must not validate a candidate")


class _CompletingHandler:
    def execute(self, command, claim, workspace, *, effects):
        del command, claim, effects
        output = workspace.root / "output"
        output.mkdir()
        (output / "result.txt").write_text("completed\n", encoding="utf-8")
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256=digest({"checks": "completed"}),
            resources_settled=True,
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        del command, claim, effects
        return candidate


class _CancelAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, command, target_command, target_effect, request):
        self.calls += 1
        if target_effect.command_id != target_command.command_id:
            raise AssertionError("target effect and target command differ")
        if request.target_command_id != target_command.command_id:
            raise AssertionError("cancel request and target command differ")
        return {
            "accepted": True,
            "cancel_command_id": command.command_id,
            "target_effect_id": target_effect.effect_id,
        }


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class CancellationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="cancel-postgres-")
        cls.server = pgserver.get_server(Path(cls.server_root.name) / "pgdata", cleanup_mode="delete")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="cancel-runtime-")
        self.addCleanup(self.directory.cleanup)
        self.schema = "cancel_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{self.schema}"',
        )
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
            cursor.execute(schema_sql)
            cursor.execute(f'UPDATE "{self.schema}".admission SET state = %s WHERE id = 1', ("open",))

        self.harness = AdmissionHarness(Path(self.directory.name) / "admission")
        self.artifacts = self.harness.store
        self.facts = FactStore(self._connect, self.artifacts)
        self.now = self.harness.spec.created_at + timedelta(minutes=1)
        self.target = self.harness.spec.executors.freeze
        self.dispatch_executor = digest({"transport": "airflow-api"})
        self.cancel_executor = digest({"transport": "native-cancel"})
        self.command = self._admit()

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _query(self, statement, parameters=()):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def _admit(self) -> StageCommand:
        from description_pipeline.pipeline.admission import (
            build_freeze_command,
            build_run_admission,
        )

        admission = build_run_admission(self.harness.spec)
        command = build_freeze_command(self.harness.spec)
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=admission.run_id,
            freeze_command_id=command.command_id,
        )
        conf_artifact = self.artifacts.seal_dispatch_conf(
            conf,
            producer_run_id=admission.run_id,
            producer_tool_sha256=self.dispatch_executor,
        )
        dispatch = DispatchCommand.create(
            command_id="dispatch-" + admission.run_id,
            run_id=admission.run_id,
            freeze_command_id=command.command_id,
            idempotency_key=f"{admission.run_id}/dispatch",
            dag_id="solidworks_to_urdf",
            dag_run_id=f"portal-{admission.run_id}",
            conf_artifact=conf_artifact,
            expected_executor_sha256=self.dispatch_executor,
        )
        stored, stored_dispatch, _intent = self.facts.admit_run(
            admission,
            command,
            dispatch,
            conf_artifact,
        )
        self.assertEqual(stored, command)
        self.assertEqual(stored_dispatch, dispatch)
        return stored

    def _services(
        self,
        suffix: str,
        *,
        executor: str | None = None,
        now: datetime | None = None,
    ) -> CanonicalStageServices:
        return CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / f"work-{suffix}",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"process": suffix}),
                executor_sha256=self.target if executor is None else executor,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now if now is None else now,
        )

    def _request_cancel(
        self,
        *,
        reason_code: str = "operator_cancelled",
        reason: str = "Operator cancelled before completion",
        requested_by: str = "cli_fixture:tenant:ou_fixture",
        requested_at: datetime | None = None,
    ):
        return self.facts.request_cancel(
            target_command_id=self.command.command_id,
            cancel_executor_sha256=self.cancel_executor,
            reason_code=reason_code,
            reason=reason,
            requested_by=requested_by,
            requested_at=self.now + timedelta(seconds=1) if requested_at is None else requested_at,
        )

    def _unknown_target(self, suffix: str):
        services = self._services(suffix)
        claim = services.acquire_claim(self.command)
        workspace = services.materialize(self.command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(self.command, claim, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"effect":"native"}\n', encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "loopback"},
            expected_state={"state": "created"},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="executor")
        return self._request_cancel(), intent

    def _cancellation_control(self, suffix: str, adapter):
        work_root = Path(self.directory.name) / f"cancel-control-{suffix}"
        return CancellationControlService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=work_root,
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"process": f"cancel-control-{suffix}"}),
                executor_sha256=self.cancel_executor,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now + timedelta(minutes=1),
            adapter=adapter,
        )

    def test_pre_send_cancel_is_idempotent_and_only_the_original_executor_decides(self) -> None:
        first = self._request_cancel()
        second = self._request_cancel(requested_at=self.now + timedelta(minutes=3))
        self.assertIsInstance(first, StageCommand)
        self.assertEqual(first.kind, "cancel")
        self.assertEqual(second, first)
        self.assertEqual(first.expected_executor_sha256, self.cancel_executor)
        self.assertEqual(CancellationRequest.from_command(first).target_command_id, self.command.command_id)
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM command WHERE request->>'target_command_id' = %s",
                (self.command.command_id,),
            ),
            [(1,)],
        )
        with self.assertRaisesRegex(FactConflict, "different cancellation"):
            self._request_cancel(
                reason_code="another_reason",
                reason="Different cancellation",
                requested_by="someone-else",
                requested_at=self.now + timedelta(seconds=2),
            )
        wrong_executor = self._services("wrong-executor", executor=digest({"tool": "foreign"}))
        with self.assertRaisesRegex(ProtocolError, "claim.executor_mismatch"):
            execute_cancellation(command_id=self.command.command_id, services=wrong_executor)
        handler = _ForbiddenHandler()
        outcome = execute_stage(
            command_id=self.command.command_id,
            handler=handler,
            services=self._services("executor"),
        )
        self.assertIsInstance(outcome, FailureEnvelope)
        self.assertEqual(outcome.outcome, "cancelled")
        self.assertEqual(outcome.reason_code, "operator_cancelled")
        self.assertEqual(handler.calls, 0)
        self.assertEqual(
            self._query("SELECT outcome FROM stage_decision WHERE command_id = %s", (self.command.command_id,)),
            [("cancelled",)],
        )
        self.assertEqual(
            self._query("SELECT count(*) FROM stage_decision WHERE command_id = %s", (first.command_id,)),
            [(0,)],
        )
        self.assertEqual(
            self._query("SELECT count(*) FROM effect_intent WHERE command_id = %s", (self.command.command_id,)),
            [(0,)],
        )
        confirmed = execute_cancellation(
            command_id=self.command.command_id,
            services=self._services("replay"),
        )
        self.assertIsInstance(confirmed, FailureEnvelope)
        self.assertEqual(confirmed, outcome)

    def test_cancel_before_dispatch_authorization_blocks_the_external_send(self) -> None:
        services = self._services("pre-send")
        claim = services.acquire_claim(self.command)
        workspace = services.materialize(self.command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(self.command, claim, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"effect":"native"}\n', encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "loopback"},
            expected_state={"state": "created"},
            outbound_root=outbound,
        )
        self._request_cancel()
        with self.assertRaisesRegex(FactConflict, "Cancellation was requested"):
            effects.authorize_dispatch(intent.effect_id, source="executor")
        self.assertEqual(
            self.facts.effect_state(intent.effect_id),
            EffectState.PREPARED,
        )
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM effect_fact WHERE effect_id = %s AND fact_type = 'dispatch_authorized'",
                (intent.effect_id,),
            ),
            [(0,)],
        )

    def test_sent_unknown_is_not_cancelled_until_reconciled_absent(self) -> None:
        services = self._services("unknown")
        claim = services.acquire_claim(self.command)
        workspace = services.materialize(self.command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(self.command, claim, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"effect":"native"}\n', encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "loopback"},
            expected_state={"state": "created"},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="executor")
        cancel_command = self._request_cancel()
        with self.assertRaisesRegex(ProtocolError, "cancel.effect_unresolved"):
            execute_cancellation(
                command_id=self.command.command_id,
                services=self._services("blocked", now=self.now + timedelta(minutes=1)),
            )
        self.assertEqual(
            self._query("SELECT count(*) FROM stage_decision WHERE command_id = %s", (self.command.command_id,)),
            [(0,)],
        )
        adapter = _CancelAdapter()
        control = CancellationControlService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "cancel-control",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"process": "cancel-control"}),
                executor_sha256=self.cancel_executor,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now + timedelta(minutes=1),
            adapter=adapter,
        )
        sent = control.send(
            cancel_command_id=cancel_command.command_id,
            target_effect_id=intent.effect_id,
        )
        self.assertTrue(sent.sent)
        self.assertEqual(sent.target_effect_state, EffectState.UNKNOWN)
        self.assertEqual(sent.cancel_effect_state, EffectState.SATISFIED)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(self.facts.effect_state(intent.effect_id), EffectState.UNKNOWN)
        self.assertEqual(
            self._query("SELECT count(*) FROM stage_decision WHERE command_id = %s", (self.command.command_id,)),
            [(0,)],
        )
        replay = control.send(
            cancel_command_id=cancel_command.command_id,
            target_effect_id=intent.effect_id,
        )
        self.assertFalse(replay.sent)
        self.assertEqual(adapter.calls, 1)
        cancel_effect = self.facts.effect_settlement_for_key(
            cancel_command.command_id,
            f"cancel:{intent.effect_id}",
        )
        self.assertIsNotNone(cancel_effect)
        self.assertIsNotNone(cancel_effect.outbound_artifact)
        self.artifacts.verify(cancel_effect.outbound_artifact)
        payload_root = Path(self.directory.name) / "absent-payload"
        metadata_root = Path(self.directory.name) / "absent-metadata"
        payload_root.mkdir()
        metadata_root.mkdir()
        (payload_root / "result.json").write_text('{"present":false}\n', encoding="utf-8")
        observer_tool = digest({"tool": "observer"})
        observed_at = self.now + timedelta(minutes=1)
        payload = self.artifacts.seal_tree(
            payload_root,
            kind="effect_observation",
            producer_run_id=self.command.run_id,
            producer_stage="freeze",
            producer_tool_sha256=observer_tool,
        )
        target_settlement = self.facts.effect_settlement(intent.effect_id)
        self.assertIsNotNone(target_settlement.outbound_artifact)
        write_json(
            metadata_root / "metadata.json",
            reconciliation_metadata_document(
                command=self.command,
                intent=intent,
                fact_type="reconciled",
                state_class="absent",
                metadata={"source": "exact-operation"},
                outbound_artifact=target_settlement.outbound_artifact,
                payload_artifact=payload,
                observer_tool_sha256=observer_tool,
                observed_at=observed_at,
            ),
        )
        metadata = self.artifacts.seal_tree(
            metadata_root,
            kind="effect_reconciliation_metadata",
            producer_run_id=self.command.run_id,
            producer_stage="freeze",
            producer_tool_sha256=observer_tool,
        )
        self.facts.append_reconciliation_fact(
            effect_id=intent.effect_id,
            fact_type="reconciled",
            source="effect-reconciler:" + observer_tool,
            state_class="absent",
            payload_artifact=payload,
            metadata_artifact=metadata,
            payload_digest=payload.set_sha256,
            observed_at=observed_at,
            fact_id="absent-" + intent.effect_id,
            expected_command_id=self.command.command_id,
        )
        self.facts.close_open_claim(
            claim,
            closure_code="definite_failure",
            closed_at=self.now + timedelta(minutes=2),
        )
        outcome = execute_cancellation(
            command_id=self.command.command_id,
            services=self._services("after-reconcile", now=self.now + timedelta(minutes=3)),
        )
        self.assertIsInstance(outcome, FailureEnvelope)
        self.assertEqual(outcome.outcome, "cancelled")
        self.assertEqual(dict(outcome.effect_states), {intent.effect_id: "definitely_failed"})
        self.assertEqual(
            self.facts.effect_state(intent.effect_id),
            EffectState.DEFINITELY_FAILED,
        )

    def test_cancel_control_preserves_raw_response_when_adapter_returns_non_mapping(self) -> None:
        cancel_command, target_effect = self._unknown_target("raw-response")
        adapter = mock.Mock()
        adapter.send.return_value = b"\x00native-cancel-response\xff"
        control = self._cancellation_control("raw-response", adapter)
        with self.assertRaisesRegex(ProtocolError, "cancel.response_invalid"):
            control.send(
                cancel_command_id=cancel_command.command_id,
                target_effect_id=target_effect.effect_id,
            )
        retained = control.services.work_root / cancel_command.command_id
        raw = next(retained.glob("*/cancel-raw-response/response.bin"))
        self.assertEqual(raw.read_bytes(), b"\x00native-cancel-response\xff")
        self.assertEqual(self.facts.effect_state(target_effect.effect_id), EffectState.UNKNOWN)

    def test_cancel_control_preserves_mapping_when_response_sealing_fails(self) -> None:
        cancel_command, target_effect = self._unknown_target("seal-failure")
        adapter = _CancelAdapter()
        control = self._cancellation_control("seal-failure", adapter)
        real_seal_tree = self.artifacts.seal_tree

        def seal_tree(source, **kwargs):
            if kwargs["kind"] == "effect_response":
                raise OSError("controlled cancel response seal failure")
            return real_seal_tree(source, **kwargs)

        with (
            mock.patch.object(self.artifacts, "seal_tree", side_effect=seal_tree),
            self.assertRaisesRegex(OSError, "controlled cancel response seal failure"),
        ):
            control.send(
                cancel_command_id=cancel_command.command_id,
                target_effect_id=target_effect.effect_id,
            )
        retained = control.services.work_root / cancel_command.command_id
        response = next(retained.glob("*/cancel-response/response.json"))
        self.assertEqual(
            json.loads(response.read_text(encoding="utf-8")),
            {
                "accepted": True,
                "cancel_command_id": cancel_command.command_id,
                "target_effect_id": target_effect.effect_id,
            },
        )
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(self.facts.effect_state(target_effect.effect_id), EffectState.UNKNOWN)

    def test_cancel_control_preserves_unserializable_mapping_after_write_failure(self) -> None:
        cancel_command, target_effect = self._unknown_target("write-failure")
        adapter = mock.Mock()
        adapter.send.return_value = {
            "accepted": True,
            "cancel_command_id": cancel_command.command_id,
            "raw": b"\xff",
            "target_effect_id": target_effect.effect_id,
        }
        control = self._cancellation_control("write-failure", adapter)
        with self.assertRaises(TypeError):
            control.send(
                cancel_command_id=cancel_command.command_id,
                target_effect_id=target_effect.effect_id,
            )
        retained = control.services.work_root / cancel_command.command_id
        fallback = next(retained.glob("*/cancel-response/response.repr.json"))
        self.assertEqual(json.loads(fallback.read_text(encoding="utf-8"))["type"], "dict")
        self.assertEqual(self.facts.effect_state(target_effect.effect_id), EffectState.UNKNOWN)

    def test_cancel_control_preserves_response_when_observation_recording_fails(self) -> None:
        cancel_command, target_effect = self._unknown_target("observation-failure")
        adapter = _CancelAdapter()
        control = self._cancellation_control("observation-failure", adapter)
        with (
            mock.patch.object(
                BoundStageEffectPort,
                "record_observation",
                side_effect=OSError("controlled observation failure"),
            ),
            self.assertRaisesRegex(OSError, "controlled observation failure"),
        ):
            control.send(
                cancel_command_id=cancel_command.command_id,
                target_effect_id=target_effect.effect_id,
            )
        retained = control.services.work_root / cancel_command.command_id
        self.assertTrue(next(retained.glob("*/cancel-response/response.json")).is_file())
        self.assertEqual(self.facts.effect_state(target_effect.effect_id), EffectState.UNKNOWN)

    def test_cancel_control_preserves_timeout_payload_when_timeout_recording_fails(self) -> None:
        cancel_command, target_effect = self._unknown_target("timeout-failure")
        adapter = mock.Mock()
        adapter.send.side_effect = TimeoutError("native cancel response missing")
        control = self._cancellation_control("timeout-failure", adapter)
        with (
            mock.patch.object(
                BoundStageEffectPort,
                "record_timeout",
                side_effect=OSError("controlled timeout record failure"),
            ),
            self.assertRaisesRegex(OSError, "controlled timeout record failure"),
        ):
            control.send(
                cancel_command_id=cancel_command.command_id,
                target_effect_id=target_effect.effect_id,
            )
        retained = control.services.work_root / cancel_command.command_id
        self.assertTrue(next(retained.glob("*/cancel-timeout/error.json")).is_file())
        self.assertEqual(self.facts.effect_state(target_effect.effect_id), EffectState.UNKNOWN)

    def test_old_expired_claim_cannot_close_the_new_fenced_owner(self) -> None:
        old_claim = self._services("old-owner").acquire_claim(self.command)
        new_services = self._services("new-owner", now=self.now + timedelta(minutes=6))
        new_claim = new_services.acquire_claim(self.command)
        self.assertGreater(new_claim.fence, old_claim.fence)
        with self.assertRaisesRegex(FactConflict, "already closed"):
            self.facts.close_open_claim(
                old_claim,
                closure_code="completed",
                closed_at=self.now + timedelta(minutes=7),
            )
        rows = self._query(
            "SELECT claim_id, fence, closed_at IS NULL "
            "FROM execution_claim WHERE command_id = %s ORDER BY fence",
            (self.command.command_id,),
        )
        self.assertEqual(
            rows,
            [
                (old_claim.claim_id, old_claim.fence, False),
                (new_claim.claim_id, new_claim.fence, True),
            ],
        )
        self.facts.close_open_claim(
            new_claim,
            closure_code="completed",
            closed_at=self.now + timedelta(minutes=7),
        )

    def test_satisfied_effect_is_preserved_in_a_cancelled_envelope(self) -> None:
        services = self._services("satisfied")
        claim = services.acquire_claim(self.command)
        workspace = services.materialize(self.command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(self.command, claim, workspace)
        outbound = workspace.root / "outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"effect":"native"}\n', encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"endpoint": "loopback"},
            expected_state={"state": "created"},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="executor")
        observation = workspace.root / "observation"
        observation.mkdir()
        (observation / "result.json").write_text('{"present":true}\n', encoding="utf-8")
        effects.record_observation(
            intent.effect_id,
            source="executor",
            state_class="known",
            observation_root=observation,
        )
        self._request_cancel()
        self.facts.close_open_claim(
            claim,
            closure_code="completed",
            closed_at=self.now + timedelta(seconds=2),
        )
        outcome = execute_cancellation(
            command_id=self.command.command_id,
            services=self._services("satisfied-cancel", now=self.now + timedelta(minutes=1)),
        )
        self.assertIsInstance(outcome, FailureEnvelope)
        self.assertEqual(outcome.outcome, "cancelled")
        self.assertEqual(dict(outcome.effect_states), {intent.effect_id: "satisfied"})
        self.assertEqual(self.facts.effect_state(intent.effect_id), EffectState.SATISFIED)

    def test_partial_publish_cancels_only_the_unsent_effect_and_preserves_candidate_evidence(self) -> None:
        services = self._services("partial-publish")
        claim = services.acquire_claim(self.command)
        workspace = services.materialize(self.command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(self.command, claim, workspace)
        git_outbound = workspace.root / "git-outbound"
        pr_outbound = workspace.root / "pr-outbound"
        git_outbound.mkdir()
        pr_outbound.mkdir()
        (git_outbound / "request.json").write_text('{"operation":"git"}\n', encoding="utf-8")
        (pr_outbound / "request.json").write_text('{"operation":"pr"}\n', encoding="utf-8")
        subject = digest({"subject": "published"})
        candidate_commit = "c" * 40
        git_expected = {
            "schema_version": "solidworks-to-urdf.git-ref-update/v1",
            "repository": "owner/repo",
            "base_ref": "refs/heads/main",
            "expected_base_oid": "a" * 40,
            "branch_ref": "refs/heads/generated",
            "expected_head_oid": None,
            "candidate_commit": candidate_commit,
            "candidate_tree": "d" * 40,
            "subject_sha256": subject,
        }
        git_intent = effects.prepare_effect(
            effect_key="git",
            effect_kind="git_ref_update",
            target={"repository": "owner/repo", "branch_ref": "refs/heads/generated"},
            expected_state=git_expected,
            outbound_root=git_outbound,
        )
        effects.authorize_dispatch(git_intent.effect_id, source="publish-executor")
        git_observation = workspace.root / "git-observation"
        git_observation.mkdir()
        (git_observation / "observation.json").write_text(
            '{"state":"known","head_oid":"cccccccccccccccccccccccccccccccccccccccc"}\n',
            encoding="utf-8",
        )
        effects.record_observation(
            git_intent.effect_id,
            source="publish-executor",
            state_class="known",
            observation_root=git_observation,
        )
        pr_expected = {
            "schema_version": "solidworks-to-urdf.pr-upsert/v1",
            "repository": "owner/repo",
            "base_ref": "refs/heads/main",
            "head_ref": "refs/heads/generated",
            "candidate_commit": candidate_commit,
            "subject_sha256": subject,
            "title_digest": digest({"title": "published"}),
            "body_digest": digest({"body": "published"}),
            "expected_pr": None,
        }
        pr_intent = effects.prepare_effect(
            effect_key="pr",
            effect_kind="pr_upsert",
            target={
                "repository": "owner/repo",
                "base_ref": "refs/heads/main",
                "head_ref": "refs/heads/generated",
            },
            expected_state=pr_expected,
            outbound_root=pr_outbound,
        )
        candidate_root = workspace.root / "candidate"
        candidate_root.mkdir()
        (candidate_root / "result.json").write_text('{"partial":true}\n', encoding="utf-8")
        candidate = services.seal_candidate(
            self.command,
            claim,
            StageExecution(
                outcome="completed",
                candidate_root=str(candidate_root),
                checks_sha256=digest({"checks": "partial-publish"}),
                resources_settled=True,
            ),
        )
        self._request_cancel()
        with self.assertRaisesRegex(FactConflict, "Cancellation was requested"):
            effects.authorize_dispatch(pr_intent.effect_id, source="publish-executor")
        with self.assertRaisesRegex(FactConflict, "Cancellation was requested"):
            self.facts.commit_decision(
                decision_id="must-not-win-" + self.command.command_id,
                command=self.command,
                claim=claim,
                candidate=candidate,
                decided_at=self.now + timedelta(seconds=2),
            )
        self.facts.close_open_claim(
            claim,
            closure_code="completed",
            closed_at=self.now + timedelta(seconds=3),
        )
        outcome = execute_cancellation(
            command_id=self.command.command_id,
            services=self._services("partial-publish-cancel", now=self.now + timedelta(minutes=1)),
        )
        self.assertIsInstance(outcome, FailureEnvelope)
        self.assertEqual(outcome.outcome, "cancelled")
        self.assertEqual(
            dict(outcome.effect_states),
            {
                git_intent.effect_id: "satisfied",
                pr_intent.effect_id: "prepared",
            },
        )
        self.assertEqual(self.facts.effect_state(git_intent.effect_id), EffectState.SATISFIED)
        self.assertEqual(self.facts.effect_state(pr_intent.effect_id), EffectState.PREPARED)
        self.assertEqual(self.facts.candidate_for(self.command), candidate)
        decision_rows = self._query(
            "SELECT outcome, result_record_artifact_id FROM stage_decision WHERE command_id = %s",
            (self.command.command_id,),
        )
        self.assertEqual(decision_rows, [("cancelled", None)])
        git_settlement = self.facts.effect_settlement_for_key(self.command.command_id, "git")
        self.assertIsNotNone(git_settlement)
        self.assertIsNotNone(git_settlement.outbound_artifact)
        self.artifacts.verify(git_settlement.outbound_artifact)
        self.artifacts.verify(candidate.result.output_artifact)

    def test_completed_stage_returns_the_original_decision_without_cancel_write(self) -> None:
        completed = execute_stage(
            command_id=self.command.command_id,
            handler=_CompletingHandler(),
            services=self._services("completed"),
        )
        self.assertIsNotNone(completed)
        decision = self._request_cancel()
        self.assertIsInstance(decision, StageDecision)
        self.assertEqual(decision.outcome, "completed")
        replay = execute_cancellation(
            command_id=self.command.command_id,
            services=self._services("completed-replay", now=self.now + timedelta(minutes=1)),
        )
        self.assertIsInstance(replay, StageResult)
        self.assertEqual(replay.digest, decision.result_digest)
        self.assertEqual(
            self._query("SELECT count(*) FROM command WHERE kind = 'cancel'"),
            [(0,)],
        )


if __name__ == "__main__":
    unittest.main()
