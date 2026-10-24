"""Real restricted-role proofs for canonical fact ownership and serialization."""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import timedelta
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
)
from description_pipeline.pipeline.engine import execute_stage
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.reconciliation import (
    EffectReconciliationEvidence,
    EffectReconciliationService,
)
from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
from description_pipeline.platform.facts import FactStore
from description_pipeline.schemas import (
    AIRFLOW_DISPATCH_CONF_SCHEMA,
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
