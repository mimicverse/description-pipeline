"""Real restricted-role proofs for canonical fact ownership and serialization."""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.io import digest
from description_pipeline.pipeline.admission import (
    VerifiedPredecessor,
    build_discover_command,
    build_freeze_command,
    build_run_admission,
)
from description_pipeline.pipeline.cancellation import CancellationControlService
from description_pipeline.pipeline.contracts import (
    DispatchCommand,
    DispatchConf,
    ExecutionClaim,
    NativeTargetIdentity,
    ProtocolError,
    StageDeferral,
    StageEffectWait,
    StageExecution,
    StagePending,
)
from description_pipeline.pipeline.diagnostic import (
    DiagnosticAdmissionSpec,
    build_diagnostic_command,
)
from description_pipeline.pipeline.engine import execute_stage
from description_pipeline.pipeline.effects import (
    EffectResumePhase,
    EffectState,
    native_transfer_target_digest,
)
from description_pipeline.pipeline.reconciliation import (
    EffectReconciliationEvidence,
    EffectReconciliationFailure,
    EffectReconciliationService,
)
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.platform.facts import FactConflict, FactStore, RunControlStore
from description_pipeline.schemas import (
    AIRFLOW_DISPATCH_CONF_SCHEMA,
    DIAGNOSTIC_ADMISSION_SCHEMA,
)
from tests.v1.test_pipeline_admission import AdmissionHarness
from tests.v1.test_runtime_candidate_postgres import _EffectHandler


PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "platform"
IMMUTABLE_TABLES = (
    "run_admission",
    "artifact",
    "command",
    "command_input_artifact",
    "stage_decision",
    "stage_candidate",
    "effect_intent",
    "effect_fact",
)


def _rendered_sql(
    name: str,
    *,
    platform_schema: str,
    airflow_schema: str,
    database: str,
) -> str:
    text = (PLATFORM_DIR / name).read_text(encoding="utf-8")
    return (
        text.replace(':"platform_schema"', f'"{platform_schema}"')
        .replace(':"airflow_schema"', f'"{airflow_schema}"')
        .replace(':"platform_database"', f'"{database}"')
    )


class _Reader:
    def read_result(self, artifact):
        raise AssertionError(f"unexpected result read: {artifact}")


class _AcceptedCancellation:
    def send(self, command, target_command, target_effect, request):
        return {
            "accepted": True,
            "cancel_command_id": command.command_id,
            "target_effect_id": target_effect.effect_id,
        }


class _KnownReconciliationAdapter:
    def observe(self, *, command, intent, outbound_root, workspace_root):
        payload = workspace_root / "known-payload"
        payload.mkdir()
        (payload / "observation.json").write_text(
            json.dumps(
                {
                    "command_id": command.command_id,
                    "effect_id": intent.effect_id,
                    "state": "known",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return EffectReconciliationEvidence(
            fact_type="reconciled",
            state_class="known",
            payload_root=payload,
            metadata={"observer": "restricted-service-test"},
        )


class _UnknownReconciliationAdapter:
    def observe(self, *, command, intent, outbound_root, workspace_root):
        payload = workspace_root / "unknown-payload"
        payload.mkdir()
        (payload / "observation.json").write_text(
            json.dumps(
                {
                    "command_id": command.command_id,
                    "effect_id": intent.effect_id,
                    "state": "running",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return EffectReconciliationEvidence(
            fact_type="observation",
            state_class="unobservable",
            payload_root=payload,
            metadata={"observer": "unknown-resume-test"},
        )


class _TransferPendingReconciliationAdapter:
    def __init__(self, *, target_digest: str | None = None) -> None:
        self.target_digest = target_digest

    def observe(self, *, command, intent, outbound_root, workspace_root):
        payload = workspace_root / "transfer-pending"
        payload.mkdir()
        (payload / "accepted.json").write_text(
            json.dumps(
                {
                    "command_id": command.command_id,
                    "effect_id": intent.effect_id,
                    "input_admitted": False,
                    "state": "accepted",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (payload / "request.json").write_bytes((outbound_root / "request.json").read_bytes())
        return EffectReconciliationEvidence(
            fact_type="observation",
            state_class="unobservable",
            payload_root=payload,
            metadata={"remote_state": "accepted", "input_admitted": False},
            resume_phase=EffectResumePhase.INPUT_TRANSFER_PENDING,
            resume_target_digest=(
                self.target_digest
                if self.target_digest is not None
                else native_transfer_target_digest(intent.target)
            ),
        )


class _ResumeHandler:
    def __init__(self, artifacts) -> None:
        self.artifacts = artifacts
        self.executions = 0
        self.validations = 0

    def execute(self, command, claim, workspace, *, effects, resume=None):
        if not isinstance(resume, StageEffectWait):
            raise AssertionError("resume wait is required")
        settlement = effects.effect_settlement_for_key("native")
        if settlement is None or settlement.state is not EffectState.SATISFIED:
            raise AssertionError("the resumed effect is not satisfied")
        if settlement.payload_artifact is None:
            raise AssertionError("the resumed effect has no sealed payload")
        payload = self.artifacts.materialize(
            settlement.payload_artifact,
            workspace.root / "resume-payload",
        )
        if not (payload / "observation.json").is_file():
            raise AssertionError("the resumed payload is incomplete")
        output = workspace.root / "resume-output"
        output.mkdir()
        (output / "result.json").write_text(
            json.dumps(
                {
                    "command_id": command.command_id,
                    "effect_id": settlement.effect_id,
                    "fence": claim.fence,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.executions += 1
        return StageExecution(
            outcome="completed",
            candidate_root=str(output),
            checks_sha256=digest({"resume": command.command_id}),
            resources_settled=True,
        )

    def validate_candidate(self, command, claim, candidate, *, effects):
        del command, claim, effects
        self.validations += 1
        return candidate


class _SequentialDeferralHandler:
    def execute(self, command, claim, workspace, *, effects, resume=None):
        if not isinstance(resume, StageEffectWait):
            raise AssertionError("resume wait is required")
        first = effects.effect_settlement_for_key("native")
        if first is None or first.state is not EffectState.SATISFIED:
            raise AssertionError("the first effect is not satisfied")
        outbound = workspace.root / "second-outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"effect":"second"}\n', encoding="utf-8")
        second = effects.prepare_effect(
            effect_key="second",
            effect_kind="native_operation",
            target={"operation": "second"},
            expected_state={"state": "complete"},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(second.effect_id, source="restricted-service")
        return StageDeferral(second.effect_id)

    def validate_candidate(self, command, claim, candidate, *, effects):
        raise AssertionError("sequential deferral has no candidate")


class _Reconciler:
    def __init__(self, service: EffectReconciliationService) -> None:
        self.service = service

    def reconcile_effect(self, *, command_id: str, effect_key: str):
        return self.service.reconcile(
            command_id=command_id,
            effect_key=effect_key,
            adapter=_KnownReconciliationAdapter(),
        )


class _UnknownReconciler(_Reconciler):
    def reconcile_effect(self, *, command_id: str, effect_key: str):
        return self.service.reconcile(
            command_id=command_id,
            effect_key=effect_key,
            adapter=_UnknownReconciliationAdapter(),
        )


class _TransferPendingReconciler(_Reconciler):
    def __init__(
        self,
        service: EffectReconciliationService,
        *,
        adapter: _TransferPendingReconciliationAdapter | None = None,
    ) -> None:
        super().__init__(service)
        self.adapter = adapter or _TransferPendingReconciliationAdapter()

    def reconcile_effect(self, *, command_id: str, effect_key: str):
        return self.service.reconcile(
            command_id=command_id,
            effect_key=effect_key,
            adapter=self.adapter,
        )


class _TransferResumeHandler:
    def __init__(self) -> None:
        self.transfer_calls = 0

    def execute(self, command, claim, workspace, *, effects, resume=None):
        del command, claim, workspace
        if not isinstance(resume, StageEffectWait):
            raise AssertionError("resume wait is required")
        settlement = effects.effect_settlement_for_key("native")
        if (
            settlement is None
            or settlement.state is not EffectState.UNKNOWN
            or settlement.resume_phase is not EffectResumePhase.INPUT_TRANSFER_PENDING
        ):
            raise AssertionError("transfer resume requires the canonical pending phase")
        effects.authorize_resume(resume.effect_id, source="transfer-resume")
        self.transfer_calls += 1
        return StageDeferral(resume.effect_id)

    def validate_candidate(self, *_args, **_kwargs):
        raise AssertionError("transfer deferral has no candidate")


class _PausedEffectLockStore(FactStore):
    def __init__(
        self,
        connect,
        artifacts,
        *,
        effect_id: str,
        ready,
        release,
    ) -> None:
        super().__init__(connect, artifacts)
        self._paused_effect_id = effect_id
        self._ready = ready
        self._release = release
        self._paused = False

    def _lock_effect_cursor(self, cursor, effect_id: str) -> None:
        if effect_id == self._paused_effect_id and not self._paused:
            self._paused = True
            self._ready.set()
            if not self._release.wait(20):
                raise TimeoutError("ledger pause release timed out")
        super()._lock_effect_cursor(cursor, effect_id)


def _settlement_during_pause(
    uri: str,
    schema: str,
    artifact_root: str,
    command_id: str,
    effect_id: str,
    ready,
    release,
    result,
) -> None:
    import psycopg2
    from description_pipeline.artifacts import ArtifactStore

    def connect():
        return psycopg2.connect(
            uri,
            user="platform_service",
            options=f"-c search_path={schema},public",
        )

    artifacts = ArtifactStore(artifact_root)
    facts = _PausedEffectLockStore(
        connect,
        artifacts,
        effect_id=effect_id,
        ready=ready,
        release=release,
    )
    try:
        settlement = facts.effect_settlement_for_key(command_id, "native")
        result.put(("ok", None if settlement is None else settlement.state.value))
    except Exception as error:
        result.put(("error", f"{type(error).__name__}: {error}"))


def _reconcile_during_pause(
    uri: str,
    schema: str,
    artifact_root: str,
    work_root: str,
    command_id: str,
    result,
) -> None:
    import psycopg2
    from description_pipeline.artifacts import ArtifactStore

    def connect():
        return psycopg2.connect(
            uri,
            user="platform_service",
            options=f"-c search_path={schema},public",
        )

    artifacts = ArtifactStore(artifact_root)
    facts = FactStore(connect, artifacts)
    service = EffectReconciliationService(
        facts=facts,
        artifacts=artifacts,
        work_root=work_root,
        observer_tool_sha256=digest({"observer": "lock-order"}),
        clock=lambda: datetime(2026, 10, 25, 6, 0, tzinfo=UTC),
    )
    try:
        reconciled = service.reconcile(
            command_id=command_id,
            effect_key="native",
            adapter=_KnownReconciliationAdapter(),
        )
        result.put(("ok", reconciled.settlement.state.value))
    except Exception as error:
        cause = getattr(error, "cause", error)
        retained = getattr(error, "retained_workspace", None)
        result.put(
            (
                "error",
                f"{type(cause).__name__}: {cause}; retained={retained}",
            )
        )


def _effect_first_lock_control(
    uri: str,
    schema: str,
    command_id: str,
    effect_id: str,
    effect_acquired,
    result,
) -> None:
    import psycopg2

    def connect():
        return psycopg2.connect(
            uri,
            user="platform_service",
            options=f"-c search_path={schema},public",
        )

    facts = FactStore(connect, _Reader())
    try:
        with facts._transaction() as connection:
            cursor = connection.cursor()
            facts._lock_effect_cursor(cursor, effect_id)
            effect_acquired.set()
            facts._lock_command_scope_cursor(cursor, command_id)
            facts._lock_effect_ledger_cursor(cursor, command_id)
    except psycopg2.errors.DeadlockDetected:
        result.put(("deadlock", None))
    except Exception as error:
        result.put(("error", f"{type(error).__name__}: {error}"))
    else:
        result.put(("ok", None))


def _claim_worker(
    uri: str,
    schema: str,
    command_id: str,
    claim_id: str,
    owner_sha256: str,
    executor_sha256: str,
    now_iso,
    expires_iso,
    start,
    result,
) -> None:
    import psycopg2
    from datetime import datetime

    from description_pipeline.platform.facts import FactConflict, FactStore

    def connect():
        return psycopg2.connect(
            uri,
            user="platform_service",
            options=f"-c search_path={schema},public",
        )

    facts = FactStore(connect, _Reader())
    start.wait(20)
    try:
        claim = facts.acquire_claim(
            command_id,
            claim_id=claim_id,
            owner_sha256=owner_sha256,
            executor_sha256=executor_sha256,
            now=datetime.fromisoformat(now_iso),
            expires_at=datetime.fromisoformat(expires_iso),
        )
    except FactConflict:
        result.put(("conflict", claim_id, None, owner_sha256))
    except Exception as error:
        result.put(("error", claim_id, type(error).__name__, str(error)))
    else:
        result.put(("acquired", claim.claim_id, claim.fence, claim.owner_sha256))


def _diagnostic_admission_worker(
    uri: str,
    schema: str,
    artifact_root: str,
    spec_record,
    command_record,
    start,
    result,
) -> None:
    import psycopg2
    from description_pipeline.artifacts import ArtifactStore

    spec = DiagnosticAdmissionSpec.from_record(spec_record)
    from description_pipeline.pipeline.contracts import StageCommand

    command = StageCommand.from_record(command_record)

    def connect():
        return psycopg2.connect(
            uri,
            user="platform_service",
            options=f"-c search_path={schema},public",
        )

    facts = FactStore(connect, ArtifactStore(artifact_root))
    start.wait(20)
    try:
        facts.admit_diagnostic(spec, command)
    except Exception as error:
        result.put(("error", type(error).__name__, str(error)))
    else:
        result.put(("ok", command.command_id))


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class PlatformServicePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="platform-service-postgres-")
        cls.server = pgserver.get_server(
            Path(cls.server_root.name) / "pgdata",
            cleanup_mode="delete",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="platform-service-cas-")
        self.addCleanup(self.directory.cleanup)
        self.schema = "service_" + uuid.uuid4().hex
        self.airflow_schema = "airflow_service_" + uuid.uuid4().hex
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            self.database = cursor.fetchone()[0]
            cursor.execute(
                _rendered_sql(
                    "bootstrap.sql",
                    platform_schema=self.schema,
                    airflow_schema=self.airflow_schema,
                    database=self.database,
                )
            )
        with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
            cursor.execute(
                _rendered_sql(
                    "schema.sql",
                    platform_schema=self.schema,
                    airflow_schema=self.airflow_schema,
                    database=self.database,
                )
            )
        with self._role_connect("airflow_migrate") as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE "{self.airflow_schema}".task_instance (
                    dag_id text NOT NULL,
                    task_id text NOT NULL,
                    run_id text NOT NULL,
                    map_index integer NOT NULL,
                    state text,
                    try_number integer,
                    start_date timestamptz,
                    end_date timestamptz,
                    PRIMARY KEY (dag_id, task_id, run_id, map_index)
                )
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE "{self.airflow_schema}".dag_run (
                    dag_id text NOT NULL,
                    run_id text NOT NULL,
                    state text,
                    start_date timestamptz,
                    end_date timestamptz,
                    PRIMARY KEY (dag_id, run_id)
                )
                """
            )
        with self._admin_connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                _rendered_sql(
                    "grants-airflow.sql",
                    platform_schema=self.schema,
                    airflow_schema=self.airflow_schema,
                    database=self.database,
                )
            )
        with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
            cursor.execute(
                _rendered_sql(
                    "views.sql",
                    platform_schema=self.schema,
                    airflow_schema=self.airflow_schema,
                    database=self.database,
                )
            )
            cursor.execute(
                _rendered_sql(
                    "roles.sql",
                    platform_schema=self.schema,
                    airflow_schema=self.airflow_schema,
                    database=self.database,
                )
            )

        self.harness = AdmissionHarness(Path(self.directory.name) / "admission")
        self.artifacts = self.harness.store
        self.facts = FactStore(self._service_connect, self.artifacts)
        from description_pipeline.platform.facts import AdmissionControlStore

        AdmissionControlStore(self._service_connect).open_admission(
            reason="restricted-role-test",
            actor="test",
        )
        self.now = self.harness.spec.created_at + timedelta(minutes=1)

    def _admin_connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _role_connect(self, role: str):
        return psycopg2.connect(
            self.server.get_uri(),
            user=role,
            options=f"-c search_path={self.schema},public",
        )

    def _service_connect(self):
        return self._role_connect("platform_service")

    def _query(self, statement, parameters=()):
        with self._admin_connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def _admit(self, suffix: str):
        spec = replace(
            self.harness.spec,
            run_id=f"run-{suffix}",
            request_id=f"request-{suffix}",
            parent_run_id=None,
            from_stage="freeze",
        )
        admission = build_run_admission(spec)
        command = build_freeze_command(spec)
        dispatch_executor = spec.dispatch_executor_sha256
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=admission.run_id,
            freeze_command_id=command.command_id,
        )
        conf_artifact = self.artifacts.seal_dispatch_conf(
            conf,
            producer_run_id=admission.run_id,
            producer_tool_sha256=dispatch_executor,
        )
        dispatch = DispatchCommand.create(
            command_id=f"dispatch-{suffix}",
            run_id=admission.run_id,
            freeze_command_id=command.command_id,
            idempotency_key=f"{admission.run_id}/dispatch",
            dag_id="solidworks_to_urdf",
            dag_run_id=f"portal-{suffix}",
            conf_artifact=conf_artifact,
            expected_executor_sha256=dispatch_executor,
        )
        stored, stored_dispatch, _effect = self.facts.admit_run(
            admission,
            command,
            dispatch,
            conf_artifact,
        )
        self.assertEqual(stored, command)
        self.assertEqual(stored_dispatch, dispatch)
        return spec, admission, command

    def _services(
        self,
        suffix: str,
        *,
        executor_sha256: str,
    ) -> CanonicalStageServices:
        return CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / f"work-{suffix}",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": suffix}),
                executor_sha256=executor_sha256,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now,
        )

    def _prepared_pending_effect(self, suffix: str):
        spec, _admission, command = self._admit(suffix)
        services = self._services(suffix, executor_sha256=spec.executors.freeze)
        claim = services.acquire_claim(command)
        workspace = services.materialize(command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(command, claim, workspace)
        outbound = workspace.root / f"{suffix}-outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"operation":"native"}\n', encoding="utf-8")
        native_target = NativeTargetIdentity.create(
            install_id="test-native-install",
            pipeline_id="solidworks-to-urdf",
        )
        target_identity = {
            "operation": command.command_id,
            "native_target_id": native_target.target_id,
            "native_target_identity": native_target.to_record(),
        }
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={
                **target_identity,
                "target_binding_digest": digest(target_identity),
            },
            expected_state={"operation": command.command_id},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="restricted-service")
        pending = services.yield_claim_for_effect(command, claim, intent.effect_id)
        return spec, command, services, claim, workspace, effects, intent, pending, outbound

    def test_restricted_service_completes_decision_and_reads_candidate(self) -> None:
        spec, _admission, command = self._admit("decision")
        services = self._services("decision", executor_sha256=spec.executors.freeze)
        result = execute_stage(
            command_id=command.command_id,
            handler=_EffectHandler(),
            services=services,
        )
        recovered = FactStore(self._service_connect, self.artifacts)
        stored = recovered.load_command(command.command_id)
        decision = recovered.decision_for(stored)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.outcome, result.outcome)
        candidate = recovered.candidate_for(stored)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.result.digest, result.digest)
        predecessor = VerifiedPredecessor(
            stage="freeze",
            command=stored,
            result=recovered.load_result(decision),
            decision=decision,
        )
        discover = build_discover_command(spec, predecessor)
        self.assertEqual(
            recovered.create_command(
                discover,
                admission=recovered.load_admission(spec.run_id),
                predecessor=predecessor,
            ),
            discover,
        )
        discover_services = self._services(
            "discover",
            executor_sha256=spec.executors.discover,
        )
        discover_claim = discover_services.acquire_claim(discover)
        discover_workspace = discover_services.materialize(
            discover,
            discover_claim,
            candidate=None,
        )
        self.addCleanup(discover_services.discard_workspace, discover_workspace)
        discover_effects = discover_services.effect_port(
            discover,
            discover_claim,
            discover_workspace,
        )
        outbound = discover_workspace.root / "discover-outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"operation":"discover"}\n', encoding="utf-8")
        discover_intent = discover_effects.prepare_effect(
            effect_key="native-discover",
            effect_kind="native_operation",
            target={"operation": discover.command_id},
            expected_state={"operation": discover.command_id},
            outbound_root=outbound,
        )
        discover_effects.authorize_dispatch(
            discover_intent.effect_id,
            source="restricted-service",
        )

    def test_conflicted_yielded_effect_does_not_abort_other_admitted_runs(self) -> None:
        normal_spec, _normal_admission, normal_command = self._admit("batch-normal")
        (
            conflict_spec,
            conflict_command,
            _services,
            _claim,
            _workspace,
            _effects,
            intent,
            _pending,
            _outbound,
        ) = self._prepared_pending_effect("batch-conflict")
        self.facts.append_effect_fact(
            effect_id=intent.effect_id,
            claim=None,
            fact_type="conflict",
            source="batch-read-conflict",
            state_class=None,
            payload_artifact=None,
            payload_digest=digest({"conflict": intent.effect_id}),
            observed_at=self.now + timedelta(seconds=5),
            fact_id="batch-read-conflict-" + intent.effect_id,
            expected_command_id=conflict_command.command_id,
        )

        summaries = {
            row.run_id: row
            for row in FactStore(self._service_connect, self.artifacts).list_admitted_runs()
        }
        self.assertIn(normal_spec.run_id, summaries)
        self.assertIn(conflict_spec.run_id, summaries)
        self.assertEqual(summaries[normal_spec.run_id].state, "incomplete")
        self.assertEqual(summaries[conflict_spec.run_id].state, "unresolved")
        self.assertNotEqual(normal_command.run_id, conflict_command.run_id)

    def test_restricted_service_authorizes_cancel_sends_and_reconciles_metadata(self) -> None:
        spec, _admission, command = self._admit("reconcile")
        services = self._services("reconcile", executor_sha256=spec.executors.freeze)
        claim = services.acquire_claim(command)
        workspace = services.materialize(command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(command, claim, workspace)
        outbound = workspace.root / "native-outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"operation":"native"}\n', encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"operation": command.command_id},
            expected_state={"operation": command.command_id},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="restricted-service")
        self.assertEqual(self.facts.effect_state(intent.effect_id), EffectState.UNKNOWN)

        cancel_executor = digest({"executor": "cancel"})
        cancel_command = self.facts.request_cancel(
            target_command_id=command.command_id,
            cancel_executor_sha256=cancel_executor,
            reason_code="operator_cancelled",
            reason="Restricted service cancellation proof",
            requested_by="test",
            requested_at=self.now + timedelta(seconds=1),
        )
        self.assertIsNotNone(cancel_command.command_id)
        cancellation = CancellationControlService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "cancel-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "cancel"}),
                executor_sha256=cancel_executor,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now + timedelta(seconds=2),
            adapter=_AcceptedCancellation(),
        )
        cancelled = cancellation.send(
            cancel_command_id=cancel_command.command_id,
            target_effect_id=intent.effect_id,
        )
        self.assertTrue(cancelled.sent)
        self.assertEqual(cancelled.target_effect_state, EffectState.UNKNOWN)
        self.assertEqual(cancelled.cancel_effect_state, EffectState.SATISFIED)

        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "reconcile-work",
            observer_tool_sha256=digest({"observer": "restricted-service"}),
            clock=lambda: self.now + timedelta(seconds=3),
        )
        reconciled = reconciler.reconcile(
            command_id=command.command_id,
            effect_key="native",
            adapter=_KnownReconciliationAdapter(),
        )
        self.assertEqual(reconciled.settlement.state, EffectState.SATISFIED)
        recovered = FactStore(self._service_connect, self.artifacts)
        restored = recovered.effect_settlement_for_key(command.command_id, "native")
        self.assertEqual(restored.state, EffectState.SATISFIED)

    def test_concurrent_diagnostics_share_artifact_order_without_deadlock(self) -> None:
        first_root = Path(self.directory.name) / "shared-artifact-a"
        second_root = Path(self.directory.name) / "shared-artifact-b"
        first_root.mkdir()
        second_root.mkdir()
        (first_root / "payload.json").write_text('{"artifact":"a"}\n', encoding="utf-8")
        (second_root / "payload.json").write_text('{"artifact":"b"}\n', encoding="utf-8")
        producer = digest({"tool": "diagnostic-artifact"})
        first, second = sorted(
            (
                self.artifacts.seal_tree(
                    first_root,
                    kind="fixture_input",
                    producer_run_id=None,
                    producer_stage=None,
                    producer_tool_sha256=producer,
                ),
                self.artifacts.seal_tree(
                    second_root,
                    kind="fixture_input",
                    producer_run_id=None,
                    producer_stage=None,
                    producer_tool_sha256=producer,
                ),
            ),
            key=lambda artifact: artifact.artifact_id,
        )
        specs = []
        commands = []
        for suffix, inputs in (
            ("reverse-a", (second, first)),
            ("reverse-b", (first, second)),
        ):
            spec = DiagnosticAdmissionSpec(
                schema_version=DIAGNOSTIC_ADMISSION_SCHEMA,
                run_id=f"artifact-order-{suffix}",
                request_id=f"artifact-order-request-{suffix}",
                target_stage="capture",
                request={"schema_version": "artifact-order/v1", "suffix": suffix},
                input_artifacts=inputs,
                expected_executor_sha256=digest({"executor": "diagnostic"}),
                config_snapshot_sha256=digest({"config": "diagnostic"}),
                contract_sha256=digest({"contract": "diagnostic"}),
                policy_sha256=digest({"policy": "diagnostic"}),
                created_at=self.now,
            )
            specs.append(spec)
            commands.append(build_diagnostic_command(spec))

        context = multiprocessing.get_context("spawn")
        start = context.Event()
        result = context.Queue()
        workers = [
            context.Process(
                target=_diagnostic_admission_worker,
                args=(
                    self.server.get_uri(),
                    self.schema,
                    str(self.artifacts.root),
                    spec.to_record(),
                    command.to_record(),
                    start,
                    result,
                ),
            )
            for spec, command in zip(specs, commands, strict=True)
        ]
        for process in workers:
            self.addCleanup(lambda item=process: item.terminate() if item.is_alive() else None)
            process.start()
        start.set()
        for process in workers:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        outcomes = [result.get(timeout=5), result.get(timeout=5)]
        self.assertEqual(
            sorted(row[0] for row in outcomes),
            ["ok", "ok"],
            outcomes,
        )

    def test_restricted_service_two_process_claim_race_preserves_fence_order(self) -> None:
        spec, _admission, command = self._admit("fence")
        executor = spec.executors.freeze
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        result = context.Queue()
        workers = [
            context.Process(
                target=_claim_worker,
                args=(
                    self.server.get_uri(),
                    self.schema,
                    command.command_id,
                    f"claim-race-{index}",
                    digest({"owner": f"race-{index}"}),
                    executor,
                    self.now.isoformat(),
                    (self.now + timedelta(minutes=5)).isoformat(),
                    start,
                    result,
                ),
            )
            for index in range(2)
        ]
        for process in workers:
            self.addCleanup(lambda item=process: item.terminate() if item.is_alive() else None)
            process.start()
        start.set()
        for process in workers:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        outcomes = [result.get(timeout=5), result.get(timeout=5)]
        acquired = [row for row in outcomes if row[0] == "acquired"]
        conflicts = [row for row in outcomes if row[0] == "conflict"]
        self.assertEqual(len(acquired), 1, outcomes)
        self.assertEqual(len(conflicts), 1, outcomes)
        self.assertEqual(acquired[0][2], 1)

        winner = acquired[0]
        claim = ExecutionClaim(
            claim_id=winner[1],
            command_id=command.command_id,
            fence=1,
            owner_sha256=winner[3],
            executor_sha256=executor,
            lane_key=None,
            expires_at=self.now + timedelta(minutes=5),
        )
        self.facts.close_open_claim(
            claim,
            closure_code="definite_failure",
            closed_at=self.now + timedelta(seconds=1),
        )
        next_claim = self._services("fence-next", executor_sha256=executor).acquire_claim(command)
        self.assertEqual(next_claim.fence, 2)

    def test_restricted_service_yields_and_resumes_the_same_effect_with_a_new_fence(self) -> None:
        (
            _spec,
            command,
            services,
            claim,
            _workspace,
            effects,
            intent,
            pending,
            outbound,
        ) = self._prepared_pending_effect("pending")
        self.assertEqual(pending.wait.command_id, command.command_id)
        self.assertEqual(pending.wait.effect_id, intent.effect_id)
        self.assertEqual(services.pending_effect_for_command(command.command_id), pending)
        self.assertIsNone(self.facts.decision_for(command))
        self.assertEqual(
            RunControlStore(self._service_connect)
            .get_admitted_run(command.run_id)
            .state,
            "incomplete",
        )
        self.assertEqual(
            self._query(
                """
                SELECT closure_code, yield_effect_id
                FROM execution_claim
                WHERE claim_id = %s
                """,
                (claim.claim_id,),
            ),
            [("yielded", intent.effect_id)],
        )
        with self.assertRaisesRegex(FactConflict, "yielded"):
            effects.record_response(
                intent.effect_id,
                source="stale-owner",
                response_root=outbound,
            )
        with self.assertRaisesRegex(FactConflict, "unresolved effect"):
            services.acquire_claim(command)
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM effect_fact WHERE effect_id = %s "
                "AND fact_type = 'dispatch_authorized'",
                (intent.effect_id,),
            ),
            [(1,)],
        )

        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "resume-reconcile",
            observer_tool_sha256=digest({"observer": "resume"}),
            clock=lambda: self.now + timedelta(seconds=4),
        )
        handler = _ResumeHandler(self.artifacts)
        result = execute_stage(
            command_id=command.command_id,
            handler=handler,
            services=services,
            resume=pending.wait,
            resume_reconciler=_Reconciler(reconciler),
        )
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(handler.executions, 1)
        self.assertEqual(handler.validations, 1)
        candidate = self.facts.candidate_for(command)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.fence, 2)
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM effect_fact WHERE effect_id = %s "
                "AND fact_type = 'dispatch_authorized'",
                (intent.effect_id,),
            ),
            [(1,)],
        )
        self.assertIsNone(services.pending_effect_for_command(command.command_id))

    def test_input_transfer_pending_resume_yields_the_same_effect_until_terminal(self) -> None:
        (
            _spec,
            command,
            services,
            _claim,
            _workspace,
            _effects,
            intent,
            pending,
            _outbound,
        ) = self._prepared_pending_effect("transfer-pending")
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "transfer-pending-reconcile",
            observer_tool_sha256=digest({"observer": "transfer-pending"}),
            clock=lambda: self.now + timedelta(seconds=20),
        )
        transfer_handler = _TransferResumeHandler()
        transfer_pending = execute_stage(
            command_id=command.command_id,
            handler=transfer_handler,
            services=services,
            resume=pending.wait,
            resume_reconciler=_TransferPendingReconciler(reconciler),
        )
        self.assertEqual(transfer_pending, pending)
        self.assertEqual(transfer_handler.transfer_calls, 1)
        settlement = self.facts.effect_settlement_for_key(command.command_id, "native")
        self.assertEqual(settlement.state, EffectState.UNKNOWN)
        self.assertEqual(
            settlement.resume_phase,
            EffectResumePhase.INPUT_TRANSFER_PENDING,
        )
        self.assertEqual(services.pending_effect_for_command(command.command_id), pending)
        self.assertEqual(
            self._query(
                """
                SELECT fence, closure_code, yield_effect_id
                FROM execution_claim
                WHERE command_id = %s
                ORDER BY fence
                """,
                (command.command_id,),
            ),
            [
                (1, "yielded", intent.effect_id),
                (2, "yielded", intent.effect_id),
            ],
        )
        self.assertEqual(
            self._query(
                """
                SELECT fact_type, resume_phase
                FROM effect_fact
                WHERE effect_id = %s
                ORDER BY seq
                """,
                (intent.effect_id,),
            ),
            [
                ("dispatch_authorized", None),
                ("observation", "input_transfer_pending"),
                ("resume_authorized", None),
            ],
        )

        terminal_handler = _ResumeHandler(self.artifacts)
        completed = execute_stage(
            command_id=command.command_id,
            handler=terminal_handler,
            services=services,
            resume=pending.wait,
            resume_reconciler=_Reconciler(reconciler),
        )
        self.assertEqual(completed.outcome, "completed")
        self.assertEqual(terminal_handler.executions, 1)
        self.assertEqual(
            self._query(
                """
                SELECT fence, closure_code
                FROM execution_claim
                WHERE command_id = %s
                ORDER BY fence
                """,
                (command.command_id,),
            ),
            [
                (1, "yielded"),
                (2, "yielded"),
                (3, "completed"),
            ],
        )

    def test_input_transfer_pending_phase_cannot_authorize_git_retry(self) -> None:
        spec, _admission, command = self._admit("git-phase-rejected")
        services = self._services("git-phase-rejected", executor_sha256=spec.executors.freeze)
        claim = services.acquire_claim(command)
        workspace = services.materialize(command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(command, claim, workspace)
        outbound = workspace.root / "git-outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"operation":"git"}\n', encoding="utf-8")
        expected = {
            "schema_version": "solidworks-to-urdf.git-ref-update/v1",
            "repository": "owner/repo",
            "base_ref": "refs/heads/main",
            "expected_base_oid": "a" * 40,
            "branch_ref": "refs/heads/generated",
            "expected_head_oid": "b" * 40,
            "candidate_commit": "c" * 40,
            "candidate_tree": "d" * 40,
            "subject_sha256": digest({"subject": command.command_id}),
        }
        intent = effects.prepare_effect(
            effect_key="git-phase",
            effect_kind="git_ref_update",
            target={
                "repository": "owner/repo",
                "branch_ref": "refs/heads/generated",
            },
            expected_state=expected,
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="git-phase")
        services.yield_claim_for_effect(command, claim, intent.effect_id)
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "git-phase-reconcile",
            observer_tool_sha256=digest({"observer": "git-phase"}),
            clock=lambda: self.now + timedelta(seconds=30),
        )
        with self.assertRaises(EffectReconciliationFailure) as raised:
            reconciler.reconcile(
                command_id=command.command_id,
                effect_key=intent.effect_key,
                adapter=_TransferPendingReconciliationAdapter(),
            )
        self.assertIsInstance(raised.exception.cause, ProtocolError)
        self.assertEqual(
            self._query(
                "SELECT fact_type FROM effect_fact WHERE effect_id = %s ORDER BY seq",
                (intent.effect_id,),
            ),
            [("dispatch_authorized",)],
        )

    def test_expired_input_transfer_claim_is_closed_only_after_reconciliation(self) -> None:
        spec, command, services, _claim, _workspace, _effects, intent, pending, _outbound = (
            self._prepared_pending_effect("transfer-claim-crash")
        )
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "transfer-claim-crash",
            observer_tool_sha256=digest({"observer": "transfer-claim-crash"}),
            clock=lambda: self.now + timedelta(seconds=35),
        )
        resolved = reconciler.reconcile(
            command_id=command.command_id,
            effect_key="native",
            adapter=_TransferPendingReconciliationAdapter(),
        )
        self.assertEqual(resolved.resume_phase, EffectResumePhase.INPUT_TRANSFER_PENDING)
        crashed_claim = services.acquire_claim(command)
        self.assertEqual(crashed_claim.fence, 2)
        with self.assertRaisesRegex(FactConflict, "live executor"):
            services.acquire_claim(command)

        later = self.now + timedelta(minutes=10)
        restarted = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "transfer-restarted",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "transfer-restarted"}),
                executor_sha256=spec.executors.freeze,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: later,
        )
        restarted_reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "transfer-restarted-reconcile",
            observer_tool_sha256=digest({"observer": "transfer-restarted"}),
            clock=lambda: later,
        )
        handler = _TransferResumeHandler()
        result = execute_stage(
            command_id=command.command_id,
            handler=handler,
            services=restarted,
            resume=pending.wait,
            resume_reconciler=_TransferPendingReconciler(restarted_reconciler),
        )
        self.assertEqual(result, pending)
        self.assertEqual(handler.transfer_calls, 1)
        self.assertEqual(
            self._query(
                """
                SELECT fence, closure_code, yield_effect_id
                FROM execution_claim
                WHERE command_id = %s
                ORDER BY fence
                """,
                (command.command_id,),
            ),
            [
                (1, "yielded", intent.effect_id),
                (2, "reconciled", None),
                (3, "yielded", intent.effect_id),
            ],
        )

    def test_transfer_resume_rejects_target_drift_and_cancellation_before_grant(self) -> None:
        (
            _spec,
            mismatch_command,
            _services,
            _claim,
            _workspace,
            _effects,
            mismatch_intent,
            _pending,
            _outbound,
        ) = self._prepared_pending_effect("transfer-target-drift")
        mismatch_reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "transfer-target-drift",
            observer_tool_sha256=digest({"observer": "transfer-target-drift"}),
            clock=lambda: self.now + timedelta(seconds=40),
        )
        with self.assertRaises(EffectReconciliationFailure) as raised:
            mismatch_reconciler.reconcile(
                command_id=mismatch_command.command_id,
                effect_key=mismatch_intent.effect_key,
                adapter=_TransferPendingReconciliationAdapter(target_digest="f" * 64),
            )
        self.assertIsInstance(raised.exception.cause, ProtocolError)
        self.assertEqual(
            self._query(
                "SELECT fact_type FROM effect_fact WHERE effect_id = %s ORDER BY seq",
                (mismatch_intent.effect_id,),
            ),
            [("dispatch_authorized",)],
        )

        (
            _cancel_spec,
            cancel_command,
            cancel_services,
            _cancel_claim,
            _cancel_workspace,
            _cancel_effects,
            cancel_intent,
            cancel_pending,
            _cancel_outbound,
        ) = self._prepared_pending_effect("transfer-before-cancel")
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "transfer-before-cancel",
            observer_tool_sha256=digest({"observer": "transfer-before-cancel"}),
            clock=lambda: self.now + timedelta(seconds=50),
        )
        reconciled = reconciler.reconcile(
            command_id=cancel_command.command_id,
            effect_key="native",
            adapter=_TransferPendingReconciliationAdapter(),
        )
        self.assertEqual(reconciled.resume_phase, EffectResumePhase.INPUT_TRANSFER_PENDING)
        resume_claim = cancel_services.acquire_claim(cancel_command)
        self.facts.request_cancel(
            target_command_id=cancel_command.command_id,
            cancel_executor_sha256=digest({"executor": "cancel-transfer"}),
            reason_code="operator_cancelled",
            reason="Cancel before transfer continuation grant",
            requested_by="test",
            requested_at=self.now + timedelta(seconds=51),
        )
        with self.assertRaisesRegex(FactConflict, "Cancellation"):
            self.facts.authorize_effect_resume(
                effect_id=cancel_pending.wait.effect_id,
                claim=resume_claim,
                source="transfer-resume",
                payload_digest=digest({"resume": cancel_command.command_id}),
                observed_at=self.now + timedelta(seconds=52),
                fact_id="transfer-before-cancel-authorization",
            )
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM effect_fact WHERE effect_id = %s "
                "AND fact_type = 'resume_authorized'",
                (cancel_intent.effect_id,),
            ),
            [(0,)],
        )

    def test_yield_history_resumes_without_defer_kwargs_after_reconcile_ack_loss(self) -> None:
        (
            _spec,
            command,
            services,
            _claim,
            _workspace,
            _effects,
            intent,
            _pending,
            _outbound,
        ) = self._prepared_pending_effect("pending-reconcile-ack")
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "reconcile-ack",
            observer_tool_sha256=digest({"observer": "reconcile-ack"}),
            clock=lambda: self.now + timedelta(seconds=10),
        )
        resolved = reconciler.reconcile(
            command_id=command.command_id,
            effect_key="native",
            adapter=_KnownReconciliationAdapter(),
        )
        self.assertEqual(resolved.settlement.state, EffectState.SATISFIED)
        handler = _ResumeHandler(self.artifacts)
        result = execute_stage(
            command_id=command.command_id,
            handler=handler,
            services=services,
        )
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(handler.executions, 1)
        candidate = self.facts.candidate_for(command)
        self.assertEqual(candidate.fence, 2)
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM effect_fact WHERE effect_id = %s "
                "AND fact_type = 'dispatch_authorized'",
                (intent.effect_id,),
            ),
            [(1,)],
        )
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM execution_claim WHERE command_id = %s",
                (command.command_id,),
            ),
            [(2,)],
        )

    def test_expired_resume_claim_is_replaced_using_historical_wait(self) -> None:
        (
            spec,
            command,
            _services,
            _claim,
            _workspace,
            _effects,
            intent,
            _pending,
            _outbound,
        ) = self._prepared_pending_effect("pending-resume-crash")
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "resume-crash",
            observer_tool_sha256=digest({"observer": "resume-crash"}),
            clock=lambda: self.now + timedelta(seconds=10),
        )
        reconciler.reconcile(
            command_id=command.command_id,
            effect_key="native",
            adapter=_KnownReconciliationAdapter(),
        )
        crashed_services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "crashed-resume",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "crashed-resume"}),
                executor_sha256=spec.executors.freeze,
                lease=timedelta(minutes=1),
            ),
            clock=lambda: self.now + timedelta(seconds=11),
        )
        crashed_claim = crashed_services.acquire_claim(command)
        self.assertEqual(crashed_claim.fence, 2)

        restarted_services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "restarted-resume",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "restarted-resume"}),
                executor_sha256=spec.executors.freeze,
                lease=timedelta(minutes=1),
            ),
            clock=lambda: self.now + timedelta(minutes=2),
        )
        handler = _ResumeHandler(self.artifacts)
        result = execute_stage(
            command_id=command.command_id,
            handler=handler,
            services=restarted_services,
        )
        self.assertEqual(result.outcome, "completed")
        candidate = self.facts.candidate_for(command)
        self.assertEqual(candidate.fence, 3)
        rows = self._query(
            "SELECT fence, closure_code FROM execution_claim "
            "WHERE command_id = %s ORDER BY fence",
            (command.command_id,),
        )
        self.assertEqual(
            rows,
            [
                (1, "yielded"),
                (2, "reconciled"),
                (3, "completed"),
            ],
        )
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM effect_fact WHERE effect_id = %s "
                "AND fact_type = 'dispatch_authorized'",
                (intent.effect_id,),
            ),
            [(1,)],
        )

    def test_second_yield_supersedes_old_wait_without_ambiguous_ledger(self) -> None:
        (
            _spec,
            command,
            services,
            _claim,
            _workspace,
            _effects,
            _first_intent,
            first_pending,
            _outbound,
        ) = self._prepared_pending_effect("pending-first")
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "sequential-reconcile",
            observer_tool_sha256=digest({"observer": "sequential"}),
            clock=lambda: self.now + timedelta(seconds=12),
        )
        second_pending = execute_stage(
            command_id=command.command_id,
            handler=_SequentialDeferralHandler(),
            services=services,
            resume=first_pending.wait,
            resume_reconciler=_Reconciler(reconciler),
        )
        self.assertIsInstance(second_pending, StagePending)
        self.assertNotEqual(second_pending.wait.effect_id, first_pending.wait.effect_id)
        self.assertEqual(services.pending_effect_for_command(command.command_id), second_pending)
        claims = self._query(
            "SELECT fence, closure_code, yield_effect_id "
            "FROM execution_claim WHERE command_id = %s ORDER BY fence",
            (command.command_id,),
        )
        self.assertEqual(
            claims,
            [
                (1, "yielded", first_pending.wait.effect_id),
                (2, "yielded", second_pending.wait.effect_id),
            ],
        )
        with self.assertRaisesRegex(FactConflict, "canonical yielded claim"):
            execute_stage(
                command_id=command.command_id,
                handler=_ResumeHandler(self.artifacts),
                services=services,
                resume=first_pending.wait,
                resume_reconciler=_Reconciler(reconciler),
            )

    def test_unknown_resume_rewais_without_acquiring_dispatch_rights(self) -> None:
        (
            _spec,
            command,
            services,
            _claim,
            _workspace,
            _effects,
            intent,
            pending,
            _outbound,
        ) = self._prepared_pending_effect("pending-unknown")
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "unknown-resume-reconcile",
            observer_tool_sha256=digest({"observer": "unknown-resume"}),
            clock=lambda: self.now + timedelta(seconds=5),
        )
        result = execute_stage(
            command_id=command.command_id,
            handler=_ResumeHandler(self.artifacts),
            services=services,
            resume=pending.wait,
            resume_reconciler=_UnknownReconciler(reconciler),
        )
        self.assertEqual(result, pending)
        self.assertEqual(services.pending_effect_for_command(command.command_id), pending)
        self.assertEqual(
            self._query(
                """
                SELECT count(*), count(*) FILTER (WHERE closed_at IS NULL)
                FROM execution_claim
                WHERE command_id = %s
                """,
                (command.command_id,),
            ),
            [(1, 0)],
        )
        facts_before = self._query(
            "SELECT count(*) FROM effect_fact WHERE effect_id = %s",
            (intent.effect_id,),
        )
        wrong = replace(pending.wait, effect_key="foreign-effect")
        with self.assertRaisesRegex(FactConflict, "canonical intent"):
            execute_stage(
                command_id=command.command_id,
                handler=_ResumeHandler(self.artifacts),
                services=services,
                resume=wrong,
                resume_reconciler=_UnknownReconciler(reconciler),
            )
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM effect_fact WHERE effect_id = %s",
                (intent.effect_id,),
            ),
            facts_before,
        )
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM execution_claim WHERE command_id = %s",
                (command.command_id,),
            ),
            [(1,)],
        )

    def test_yield_committed_before_caller_ack_reconstructs_pending_without_wait_kwargs(self) -> None:
        (
            spec,
            command,
            _services,
            _claim,
            _workspace,
            _effects,
            intent,
            pending,
            _outbound,
        ) = self._prepared_pending_effect("pending-lost-ack")
        restarted = FactStore(self._service_connect, self.artifacts)
        restarted_services = CanonicalStageServices(
            facts=restarted,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "restarted-service",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "restarted"}),
                executor_sha256=spec.executors.freeze,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now + timedelta(seconds=6),
        )
        reconstructed = restarted_services.pending_effect_for_command(command.command_id)
        self.assertEqual(reconstructed, pending)
        handler = _EffectHandler()
        result = execute_stage(
            command_id=command.command_id,
            handler=handler,
            services=restarted_services,
        )
        self.assertEqual(result, pending)
        self.assertEqual(handler.external_calls, 0)
        self.assertEqual(
            self._query(
                """
                SELECT count(*), count(*) FILTER (WHERE closed_at IS NULL),
                       count(*) FILTER (WHERE yield_effect_id = %s)
                FROM execution_claim
                WHERE command_id = %s
                """,
                (intent.effect_id, command.command_id),
            ),
            [(1, 0, 1)],
        )

    def test_ledger_selects_the_exact_pending_effect_when_another_effect_is_settled(self) -> None:
        spec, _admission, command = self._admit("pending-multi-effect")
        services = self._services("pending-multi", executor_sha256=spec.executors.freeze)
        claim = services.acquire_claim(command)
        workspace = services.materialize(command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(command, claim, workspace)

        settled_root = workspace.root / "settled-outbound"
        settled_root.mkdir()
        (settled_root / "request.json").write_text('{"effect":"settled"}\n', encoding="utf-8")
        settled = effects.prepare_effect(
            effect_key="settled",
            effect_kind="native_operation",
            target={"operation": "settled"},
            expected_state={"state": "complete"},
            outbound_root=settled_root,
        )
        effects.authorize_dispatch(settled.effect_id, source="restricted-service")
        settled_payload = workspace.root / "settled-payload"
        settled_payload.mkdir()
        (settled_payload / "result.json").write_text('{"state":"complete"}\n', encoding="utf-8")
        effects.record_observation(
            settled.effect_id,
            source="restricted-service",
            state_class="known",
            observation_root=settled_payload,
        )

        pending_root = workspace.root / "pending-outbound"
        pending_root.mkdir()
        (pending_root / "request.json").write_text('{"effect":"pending"}\n', encoding="utf-8")
        pending_intent = effects.prepare_effect(
            effect_key="pending",
            effect_kind="native_operation",
            target={"operation": "pending"},
            expected_state={"state": "complete"},
            outbound_root=pending_root,
        )
        effects.authorize_dispatch(pending_intent.effect_id, source="restricted-service")
        pending = services.yield_claim_for_effect(command, claim, pending_intent.effect_id)
        self.assertEqual(pending.wait.effect_id, pending_intent.effect_id)
        self.assertEqual(services.pending_effect_for_command(command.command_id), pending)
        self.assertEqual(
            services.effect_states_for_command(command.command_id),
            {
                settled.effect_id: EffectState.SATISFIED,
                pending_intent.effect_id: EffectState.UNKNOWN,
            },
        )

    def test_registered_cancel_advances_while_target_waits_without_a_target_claim(self) -> None:
        (
            _spec,
            command,
            _services,
            _claim,
            _workspace,
            _effects,
            intent,
            pending,
            _outbound,
        ) = self._prepared_pending_effect("pending-cancel")
        cancel_executor = digest({"executor": "cancel"})
        cancel_command = self.facts.request_cancel(
            target_command_id=command.command_id,
            cancel_executor_sha256=cancel_executor,
            reason_code="operator_cancelled",
            reason="Cancel while target effect is pending",
            requested_by="test",
            requested_at=self.now + timedelta(seconds=7),
        )
        self.assertEqual(cancel_command.request["target_command_id"], command.command_id)
        control = CancellationControlService(
            facts=self.facts,
            artifacts=self.artifacts,
            work_root=Path(self.directory.name) / "pending-cancel",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "pending-cancel"}),
                executor_sha256=cancel_executor,
                lease=timedelta(minutes=5),
            ),
            clock=lambda: self.now + timedelta(seconds=8),
            adapter=_AcceptedCancellation(),
        )
        sent = control.send(
            cancel_command_id=cancel_command.command_id,
            target_effect_id=intent.effect_id,
        )
        self.assertTrue(sent.sent)
        self.assertEqual(sent.target_effect_state, EffectState.UNKNOWN)
        self.assertEqual(sent.cancel_effect_state, EffectState.SATISFIED)
        self.assertEqual(self.facts.pending_effect_for_command(command.command_id).wait, pending.wait)
        rows = self._query(
            "SELECT closure_code, yield_effect_id, closed_at IS NULL "
            "FROM execution_claim WHERE command_id = %s ORDER BY fence",
            (command.command_id,),
        )
        self.assertEqual(rows, [("yielded", intent.effect_id, False)])
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM execution_claim WHERE command_id = %s",
                (cancel_command.command_id,),
            ),
            [(1,)],
        )

    def test_public_reconcile_and_settlement_share_one_lock_order_without_deadlock(self) -> None:
        (
            _spec,
            command,
            _services,
            _claim,
            _workspace,
            _effects,
            intent,
            _pending,
            _outbound,
        ) = self._prepared_pending_effect("lock-order")
        context = multiprocessing.get_context("spawn")
        paused = context.Event()
        release = context.Event()
        result = context.Queue()
        reader = context.Process(
            target=_settlement_during_pause,
            args=(
                self.server.get_uri(),
                self.schema,
                str(self.artifacts.root),
                command.command_id,
                intent.effect_id,
                paused,
                release,
                result,
            ),
        )
        reconciler = context.Process(
            target=_reconcile_during_pause,
            args=(
                self.server.get_uri(),
                self.schema,
                str(self.artifacts.root),
                str(Path(self.directory.name) / "public-lock-order"),
                command.command_id,
                result,
            ),
        )
        for process in (reader, reconciler):
            self.addCleanup(lambda item=process: item.terminate() if item.is_alive() else None)
        reader.start()
        self.assertTrue(paused.wait(10), "settlement reader did not hold its canonical locks")
        reconciler.start()
        time.sleep(0.2)
        self.assertTrue(reader.is_alive())
        self.assertTrue(reconciler.is_alive())
        release.set()
        for process in (reader, reconciler):
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        outcomes = [result.get(timeout=5), result.get(timeout=5)]
        self.assertNotIn("error", {row[0] for row in outcomes}, outcomes)
        self.assertIn(("ok", "unknown"), outcomes)
        self.assertIn(("ok", "satisfied"), outcomes)

    def test_reverse_lock_order_control_is_detected_by_postgres(self) -> None:
        (
            _spec,
            command,
            _services,
            _claim,
            _workspace,
            _effects,
            intent,
            _pending,
            _outbound,
        ) = self._prepared_pending_effect("reverse-lock-control")
        context = multiprocessing.get_context("spawn")
        paused = context.Event()
        effect_acquired = context.Event()
        release = context.Event()
        result = context.Queue()
        command_first = context.Process(
            target=_settlement_during_pause,
            args=(
                self.server.get_uri(),
                self.schema,
                str(self.artifacts.root),
                command.command_id,
                intent.effect_id,
                paused,
                release,
                result,
            ),
        )
        effect_first = context.Process(
            target=_effect_first_lock_control,
            args=(
                self.server.get_uri(),
                self.schema,
                command.command_id,
                intent.effect_id,
                effect_acquired,
                result,
            ),
        )
        for process in (command_first, effect_first):
            self.addCleanup(lambda item=process: item.terminate() if item.is_alive() else None)
        command_first.start()
        self.assertTrue(paused.wait(10), "command-first worker did not reach the effect lock")
        effect_first.start()
        self.assertTrue(effect_acquired.wait(10), "effect-first worker did not acquire its first lock")
        release.set()
        for process in (command_first, effect_first):
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        outcomes = [result.get(timeout=5), result.get(timeout=5)]
        self.assertTrue(
            any(
                row[0] == "deadlock"
                or (
                    row[0] == "error"
                    and isinstance(row[1], str)
                    and "DeadlockDetected" in row[1]
                )
                for row in outcomes
            ),
            outcomes,
        )

    def test_non_yield_closed_claim_keeps_late_response_audit_without_advancing_fence(self) -> None:
        spec, _admission, command = self._admit("late-response")
        services = self._services("late-response", executor_sha256=spec.executors.freeze)
        claim = services.acquire_claim(command)
        workspace = services.materialize(command, claim, candidate=None)
        self.addCleanup(services.discard_workspace, workspace)
        effects = services.effect_port(command, claim, workspace)
        outbound = workspace.root / "late-outbound"
        outbound.mkdir()
        (outbound / "request.json").write_text('{"operation":"native"}\n', encoding="utf-8")
        intent = effects.prepare_effect(
            effect_key="native",
            effect_kind="native_operation",
            target={"operation": command.command_id},
            expected_state={"operation": command.command_id},
            outbound_root=outbound,
        )
        effects.authorize_dispatch(intent.effect_id, source="restricted-service")
        self.facts.close_open_claim(
            claim,
            closure_code="completed",
            closed_at=self.now + timedelta(seconds=9),
        )
        response = workspace.root / "late-response"
        response.mkdir()
        (response / "response.json").write_text('{"late":true}\n', encoding="utf-8")
        fact = effects.record_response(
            intent.effect_id,
            source="late-audit",
            response_root=response,
        )
        self.assertEqual(fact.fact_type, "response")
        self.assertEqual(
            self._query(
                "SELECT count(*), count(*) FILTER (WHERE closed_at IS NULL) "
                "FROM execution_claim WHERE command_id = %s",
                (command.command_id,),
            ),
            [(1, 0)],
        )
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM effect_fact "
                "WHERE effect_id = %s AND fact_type = 'response'",
                (intent.effect_id,),
            ),
            [(1,)],
        )

    def test_platform_service_cannot_mutate_immutable_facts(self) -> None:
        with self._service_connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user, session_user, "
                "(SELECT NOT rolsuper FROM pg_roles WHERE rolname = current_user)"
            )
            self.assertEqual(
                cursor.fetchone(),
                ("platform_service", "platform_service", True),
            )
        spec, _admission, command = self._admit("immutable")
        for table in IMMUTABLE_TABLES:
            qualified = f"{self.schema}.{table}"
            with self.subTest(table=table):
                privileges = self._query(
                    "SELECT has_table_privilege('platform_service', %s, 'UPDATE'),"
                    " has_table_privilege('platform_service', %s, 'DELETE'),"
                    " has_table_privilege('platform_service', %s, 'TRUNCATE'),"
                    " (SELECT count(*) FROM pg_trigger "
                    "  WHERE tgrelid = %s::regclass "
                    "    AND NOT tgisinternal "
                    "    AND tgname IN ('reject_update_delete', 'reject_truncate'))",
                    (qualified, qualified, qualified, qualified),
                )[0]
                self.assertEqual(privileges, (False, False, False, 2))
        with self._admin_connect() as connection, connection.cursor() as cursor:
            for table in (*IMMUTABLE_TABLES, "admission", "execution_claim", "run_display_metadata"):
                qualified = f"{self.schema}.{table}"
                cursor.execute(
                    "SELECT has_table_privilege('operator_ro', %s, 'INSERT'),"
                    " has_table_privilege('operator_ro', %s, 'UPDATE'),"
                    " has_table_privilege('operator_ro', %s, 'DELETE'),"
                    " has_table_privilege('operator_ro', %s, 'TRUNCATE')",
                    (qualified, qualified, qualified, qualified),
                )
                self.assertEqual(cursor.fetchone(), (False, False, False, False), table)

        with (
            self._service_connect() as connection,
            connection.cursor() as cursor,
            self.assertRaises(psycopg2.errors.InsufficientPrivilege),
        ):
            cursor.execute(
                "UPDATE command SET request = request WHERE command_id = %s",
                (command.command_id,),
            )
        with (
            self._service_connect() as connection,
            connection.cursor() as cursor,
            self.assertRaises(psycopg2.errors.InsufficientPrivilege),
        ):
            cursor.execute(
                "DELETE FROM command WHERE command_id = %s",
                (command.command_id,),
            )
        for table in ("command", "effect_intent"):
            with (
                self._service_connect() as connection,
                connection.cursor() as cursor,
                self.assertRaises(psycopg2.errors.InsufficientPrivilege),
            ):
                cursor.execute(f"SELECT 1 FROM {table} FOR UPDATE")

        claim = self._services(
            "immutable-claim",
            executor_sha256=spec.executors.freeze,
        ).acquire_claim(command)
        with self._service_connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE execution_claim SET last_seen_at = last_seen_at WHERE claim_id = %s",
                (claim.claim_id,),
            )
        with (
            self._service_connect() as connection,
            connection.cursor() as cursor,
            self.assertRaises(psycopg2.errors.InsufficientPrivilege),
        ):
            cursor.execute(
                "UPDATE execution_claim SET owner_sha256 = %s WHERE claim_id = %s",
                (digest({"owner": "tampered"}), claim.claim_id),
            )

        with (
            self._role_connect("platform_migrate") as connection,
            connection.cursor() as cursor,
            self.assertRaises(psycopg2.errors.IntegrityConstraintViolation) as raised,
        ):
            cursor.execute(
                "UPDATE command SET request = request WHERE command_id = %s",
                (command.command_id,),
            )
        self.assertIn("immutable_fact_mutation_rejected", str(raised.exception))
        with (
            self._role_connect("platform_migrate") as connection,
            connection.cursor() as cursor,
            self.assertRaises(psycopg2.errors.IntegrityConstraintViolation) as raised,
        ):
            cursor.execute("DELETE FROM artifact")
        self.assertIn("immutable_fact_mutation_rejected", str(raised.exception))
        with (
            self._role_connect("operator_ro") as connection,
            connection.cursor() as cursor,
            self.assertRaises(psycopg2.errors.InsufficientPrivilege),
        ):
            cursor.execute("UPDATE admission SET state = state WHERE id = 1")


if __name__ == "__main__":
    unittest.main()
