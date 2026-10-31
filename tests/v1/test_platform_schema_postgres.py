"""Real PostgreSQL validation of the current platform schema and lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.io import digest
from description_pipeline.orchestration.lifecycle_state import collect_projection, main as lifecycle_main
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.pipeline.diagnostic import (
    DiagnosticAdmissionSpec,
    build_diagnostic_run_admission,
)
from description_pipeline.platform.facts import (
    FactStoreError,
    RunControlStore,
)
from description_pipeline.platform.queries import read_run_projection
from description_pipeline.schemas import (
    AIRFLOW_DISPATCH_CONF_SCHEMA,
    DIAGNOSTIC_ADMISSION_SCHEMA,
    RUN_ADMISSION_SCHEMA,
)

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "platform"
APPLY = PLATFORM_DIR / "apply.sh"
PG_BIN = Path(pgserver.__file__).resolve().parent / "pginstall" / "bin" if pgserver else None


def _sql(
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


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class PlatformSchemaPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="platform-schema-postgres-")
        cls.server = pgserver.get_server(Path(cls.server_root.name) / "pgdata", cleanup_mode="delete")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.schema = "platform_" + uuid.uuid4().hex
        self.airflow_schema = "airflow_platform_" + uuid.uuid4().hex
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            self.database = cursor.fetchone()[0]
            cursor.execute(
                _sql(
                    "bootstrap.sql",
                    platform_schema=self.schema,
                    airflow_schema=self.airflow_schema,
                    database=self.database,
                )
            )

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            dbname=self.database,
            options=f"-c search_path={self.schema},public",
        )

    def _role_connect(self, role: str):
        return psycopg2.connect(
            self.server.get_uri(),
            dbname=self.database,
            user=role,
            options=f"-c search_path={self.schema},public",
        )

    def _apply(self, *names: str) -> None:
        with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
            for name in names:
                cursor.execute(
                    _sql(
                        name,
                        platform_schema=self.schema,
                        airflow_schema=self.airflow_schema,
                        database=self.database,
                    )
                )

    def _query(self, statement, parameters=()):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def _apply_script(self) -> subprocess.CompletedProcess[str]:
        parsed = urllib.parse.urlsplit(self.server.get_uri())
        host = urllib.parse.parse_qs(parsed.query).get("host", [""])[0] or parsed.hostname or "localhost"
        return subprocess.run(
            [
                "bash",
                str(APPLY),
                "--psql",
                str(PG_BIN / "psql"),
                "--database",
                self.database,
                "--host",
                host,
                "--user",
                "platform_migrate",
                "--platform-schema",
                self.schema,
                "--airflow-schema",
                self.airflow_schema,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def _create_airflow_tables(self) -> None:
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
        with self._role_connect("airflow_migrate") as connection, connection.cursor() as cursor:
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
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                _sql(
                    "grants-airflow.sql",
                    platform_schema=self.schema,
                    airflow_schema=self.airflow_schema,
                    database=self.database,
                )
            )

    def _install_canonical_schema(self) -> None:
        self._apply("schema.sql")
        self._create_airflow_tables()
        self._apply("views.sql", "roles.sql")

    def _insert_run(
        self,
        run_id: str,
        *,
        parent_run_id: str | None = None,
        from_stage: str | None = None,
        request: dict | None = None,
    ) -> None:
        request = request or {"purpose": "engineering"}
        request_id = f"request-{run_id}"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO run_admission (
                    run_id, request_id, parent_run_id, from_stage, request, request_digest
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    run_id,
                    request_id,
                    parent_run_id,
                    from_stage,
                    json.dumps(request),
                    digest(
                        {
                            "from_stage": from_stage,
                            "parent_run_id": parent_run_id,
                            "request": request,
                            "request_id": request_id,
                            "run_id": run_id,
                            "schema_version": RUN_ADMISSION_SCHEMA,
                        }
                    ),
                ),
            )

    def _insert_artifact(self, artifact_id: str, *, run_id: str, stage: str, kind: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO artifact (
                    artifact_id, kind, locator, manifest_sha256, set_sha256,
                    producer_run_id, producer_stage, producer_tool_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    artifact_id,
                    kind,
                    f"cas://artifact/{artifact_id}",
                    digest({"artifact": artifact_id, "part": "manifest"}),
                    digest({"artifact": artifact_id, "part": "set"}),
                    run_id,
                    stage,
                    digest({"executor": stage}),
                ),
            )

    def _insert_stage_command(
        self,
        run_id: str,
        stage: str,
        *,
        command_id: str | None = None,
        predecessor_decision_id: str | None = None,
    ) -> str:
        command_id = command_id or f"{run_id}:{stage}"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO command (
                    command_id, run_id, kind, stage, idempotency_key, request,
                    request_digest, config_snapshot_sha256, contract_sha256,
                    policy_sha256, expected_executor_sha256, predecessor_decision_id
                ) VALUES (
                    %s, %s, 'stage', %s, %s, '{}'::jsonb,
                    %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    command_id,
                    run_id,
                    stage,
                    f"{run_id}/{stage}",
                    digest({"command": command_id}),
                    digest({"config": 1}),
                    digest({"contract": 1}),
                    digest({"policy": 1}),
                    digest({"executor": stage}),
                    predecessor_decision_id,
                ),
            )
        return command_id

    def _insert_decision(
        self,
        run_id: str,
        stage: str,
        command_id: str,
        *,
        outcome: str,
        decision_id: str | None = None,
        decided_at: datetime | None = None,
    ) -> str:
        decision_id = decision_id or f"decision-{run_id}-{stage}"
        decided_at = decided_at or datetime(2026, 10, 25, tzinfo=UTC)
        executor = digest({"executor": stage})
        if outcome == "completed":
            artifact_id = f"result-{run_id}-{stage}"
            self._insert_artifact(artifact_id, run_id=run_id, stage=stage, kind="stage_result")
            values = (
                decision_id,
                run_id,
                stage,
                command_id,
                outcome,
                artifact_id,
                digest({"result": decision_id}),
                executor,
                None,
                None,
                decided_at,
            )
            statement = """
                INSERT INTO stage_decision (
                    decision_id, run_id, stage, command_id, outcome,
                    result_record_artifact_id, result_digest, actual_executor_sha256,
                    failure_envelope, failure_digest, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        else:
            failure = {"outcome": outcome, "reason_code": f"{stage}.fixture"}
            values = (
                decision_id,
                run_id,
                stage,
                command_id,
                outcome,
                None,
                None,
                executor,
                f"{stage}.fixture",
                json.dumps(failure),
                digest(failure),
                decided_at,
            )
            statement = """
                INSERT INTO stage_decision (
                    decision_id, run_id, stage, command_id, outcome,
                    result_record_artifact_id, result_digest, actual_executor_sha256,
                    reason_code, failure_envelope, failure_digest, decided_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, values)
        return decision_id

    def _insert_open_claim(self, command_id: str, *, stage: str, expired: bool = False) -> str:
        claim_id = f"claim-{command_id}"
        now = datetime.now(UTC) - (timedelta(minutes=10) if expired else timedelta())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO execution_claim (
                    claim_id, command_id, fence, owner_sha256, executor_sha256,
                    issued_at, last_seen_at, expires_at
                ) VALUES (%s, %s, 1, %s, %s, %s, %s, %s)
                """,
                (
                    claim_id,
                    command_id,
                    digest({"owner": claim_id}),
                    digest({"executor": stage}),
                    now,
                    now,
                    now + timedelta(minutes=5),
                ),
            )
        return claim_id

    def _insert_closed_candidate(self, run_id: str, stage: str, command_id: str) -> tuple[str, str]:
        claim_id = f"claim-{command_id}"
        candidate_id = f"candidate-{command_id}"
        output_artifact_id = f"output-{command_id}"
        result_digest = digest({"result": f"decision-{run_id}-{stage}"})
        now = datetime(2026, 10, 25, tzinfo=UTC)
        executor = digest({"executor": stage})
        self._insert_artifact(
            output_artifact_id,
            run_id=run_id,
            stage=stage,
            kind=f"{stage}_delivery",
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO execution_claim (
                    claim_id, command_id, fence, owner_sha256, executor_sha256,
                    issued_at, last_seen_at, expires_at, closed_at, closure_code
                ) VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s, 'completed')
                """,
                (
                    claim_id,
                    command_id,
                    digest({"owner": claim_id}),
                    executor,
                    now,
                    now,
                    now + timedelta(minutes=5),
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO stage_candidate (
                    candidate_id, command_id, claim_id, fence, output_artifact_id,
                    result_record_artifact_id, result_digest, outcome, created_at
                ) VALUES (%s, %s, %s, 1, %s, %s, %s, 'completed', %s)
                """,
                (
                    candidate_id,
                    command_id,
                    claim_id,
                    output_artifact_id,
                    f"result-{run_id}-{stage}",
                    result_digest,
                    now,
                ),
            )
        return candidate_id, output_artifact_id

    def _insert_effect(
        self,
        run_id: str,
        stage: str,
        command_id: str,
        *,
        fact_type: str,
        claim_id: str | None = None,
    ) -> str:
        effect_id = f"effect-{run_id}-{stage}"
        artifact_id = f"outbound-{run_id}-{stage}"
        self._insert_artifact(artifact_id, run_id=run_id, stage=stage, kind="effect_outbound")
        payload_artifact_id = None
        if fact_type in {"observation", "reconciled"}:
            payload_artifact_id = f"payload-{run_id}-{stage}"
            self._insert_artifact(
                payload_artifact_id,
                run_id=run_id,
                stage=stage,
                kind="effect_payload",
            )
        now = datetime(2026, 10, 25, tzinfo=UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO effect_intent (
                    effect_id, command_id, effect_key, effect_kind, target,
                    expected_state, expected_state_digest, outbound_artifact_id,
                    outbound_digest, created_at
                ) VALUES (
                    %s, %s, 'native', 'native_operation', '{}'::jsonb,
                    %s::jsonb, %s, %s, %s, %s
                )
                """,
                (
                    effect_id,
                    command_id,
                    json.dumps({"effect": effect_id}),
                    digest({"effect": effect_id}),
                    artifact_id,
                    digest({"outbound": artifact_id}),
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO effect_fact (
                    fact_id, effect_id, seq, claim_id, fence, fact_type,
                    source, state_class, payload_artifact_id, payload_digest, observed_at
                ) VALUES (%s, %s, 1, %s, %s, %s, 'fixture', %s, %s, %s, %s)
                """,
                (
                    f"fact-{effect_id}",
                    effect_id,
                    claim_id,
                    None if claim_id is None else 1,
                    fact_type,
                    "absent" if fact_type == "reconciled" else None,
                    payload_artifact_id,
                    digest({"fact": effect_id}),
                    now + timedelta(seconds=1),
                ),
            )
        return effect_id

    def test_fresh_apply_script_uses_actual_platform_schema(self) -> None:
        self._create_airflow_tables()

        first = self._apply_script()
        second = self._apply_script()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            self._query(
                "SELECT to_regclass(%s)::text",
                (f"{self.schema}.run_admission",),
            ),
            [("run_admission",)],
        )

        constraints = dict(
            self._query(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = %s::regclass AND contype = 'f'",
                (f"{self.schema}.execution_claim",),
            )
        )
        for column in ("effect_scope_id", "yield_effect_id"):
            self.assertIn(
                f"FOREIGN KEY ({column}) REFERENCES effect_intent(effect_id)",
                constraints.values(),
            )
        self.assertEqual(
            self._query(
                "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
                "JOIN pg_attribute a ON a.attrelid=d.adrelid AND a.attnum=d.adnum "
                "WHERE d.adrelid=%s::regclass AND a.attname='scope'",
                (f"{self.schema}.execution_claim",),
            ),
            [("'execution'::text",)],
        )

    def test_lifecycle_matrix_distinguishes_not_run_blocked_running_and_unresolved(self) -> None:
        self._install_canonical_schema()

        artifact_run = "run-freeze-complete"
        self._insert_run(artifact_run)
        artifact_command = self._insert_stage_command(artifact_run, "freeze")
        self._insert_decision(
            artifact_run,
            "freeze",
            artifact_command,
            outcome="completed",
        )
        empty_run = "run-no-stage-facts"
        self._insert_run(empty_run)

        for run_id, outcome in (
            ("run-freeze-failed", "failed"),
            ("run-freeze-cancelled", "cancelled"),
        ):
            self._insert_run(run_id)
            command_id = self._insert_stage_command(run_id, "freeze")
            self._insert_decision(run_id, "freeze", command_id, outcome=outcome)

        running_run = "run-running"
        expired_run = "run-expired"
        for run_id, expired in ((running_run, False), (expired_run, True)):
            self._insert_run(run_id)
            entry_command = self._insert_stage_command(run_id, "freeze")
            freeze_decision = self._insert_decision(
                run_id, "freeze", entry_command, outcome="completed",
            )
            discover_command = self._insert_stage_command(
                run_id, "discover", predecessor_decision_id=freeze_decision,
            )
            self._insert_open_claim(discover_command, stage="discover", expired=expired)

        for run_id, fact_type in (
            ("run-effect-unknown", "dispatch_authorized"),
            ("run-effect-conflict", "conflict"),
        ):
            self._insert_run(run_id)
            entry_command = self._insert_stage_command(run_id, "freeze")
            freeze_decision = self._insert_decision(
                run_id,
                "freeze",
                entry_command,
                outcome="completed",
            )
            discover_command = self._insert_stage_command(
                run_id,
                "discover",
                predecessor_decision_id=freeze_decision,
            )
            claim_id = (
                self._insert_open_claim(discover_command, stage="discover")
                if fact_type == "dispatch_authorized"
                else None
            )
            self._insert_effect(
                run_id,
                "discover",
                discover_command,
                fact_type=fact_type,
                claim_id=claim_id,
            )

        contradiction_run = "run-effect-contradiction"
        self._insert_run(contradiction_run)
        contradiction_command = self._insert_stage_command(contradiction_run, "freeze")
        self._insert_decision(
            contradiction_run,
            "freeze",
            contradiction_command,
            outcome="completed",
        )
        self._insert_effect(
            contradiction_run,
            "freeze",
            contradiction_command,
            fact_type="reconciled",
        )
        for stage in ("discover", "capture", "generate", "verify", "publish"):
            command_id = self._insert_stage_command(contradiction_run, stage)
            self._insert_decision(contradiction_run, stage, command_id, outcome="completed")

        store = RunControlStore(self._connect)
        self.assertEqual(store.get_admitted_run(artifact_run).state, "incomplete")
        self.assertEqual(store.get_admitted_run(empty_run).state, "incomplete")
        self.assertEqual(store.get_admitted_run("run-freeze-failed").state, "failed")
        self.assertEqual(store.get_admitted_run("run-freeze-cancelled").state, "cancelled")
        self.assertEqual(store.get_admitted_run(running_run).state, "incomplete")
        self.assertEqual(store.get_admitted_run(expired_run).state, "unresolved")
        self.assertEqual(store.get_admitted_run("run-effect-unknown").state, "unresolved")
        self.assertEqual(store.get_admitted_run("run-effect-conflict").state, "unresolved")
        self.assertEqual(store.get_admitted_run(contradiction_run).state, "unresolved")

    def test_canonical_checkpoint_query_emits_recovery_graph(self) -> None:

        sys.path.insert(0, str(PLATFORM_DIR.parent / "operator"))
        import recovery

        self._install_canonical_schema()
        run_id = "run-query"
        stage = "freeze"
        self._insert_run(run_id)
        command_id = self._insert_stage_command(run_id, stage)
        self._insert_decision(run_id, stage, command_id, outcome="completed")
        self._insert_closed_candidate(run_id, stage, command_id)
        query = recovery.CHECKPOINT_REFERENCES_QUERY.replace("platform.", f'"{self.schema}".')
        references = self._query(query)[0][0]
        self.assertEqual(len(references), 1)
        self.assertEqual(
            set(references[0]),
            {
                "run_id",
                "stage",
                "outcome",
                "request_id",
                "parent_run_id",
                "decision_id",
                "command_id",
                "result_digest",
                "actual_executor_sha256",
                "original_producer_decision_id",
                "failure_digest",
                "result_record_artifact",
                "output_artifact",
            },
        )
        self.assertEqual(
            recovery.checkpoint_origins(references),
            {(run_id, stage): run_id},
        )

    def test_recovery_root_preserves_unknown_effect_candidate_and_unsealed_workspace(self) -> None:
        from types import SimpleNamespace

        from description_pipeline.artifacts.store import ArtifactStore
        from description_pipeline.pipeline.admission import (
            build_freeze_command,
            build_run_admission,
        )
        from description_pipeline.pipeline.contracts import (
            DispatchCommand,
            DispatchConf,
            StageExecution,
        )
        from description_pipeline.pipeline.effects import EffectState
        from description_pipeline.pipeline.services import CanonicalStageServices, ClaimPolicy
        from description_pipeline.platform.facts import FactConflict, FactStore
        from tests.v1.test_pipeline_admission import AdmissionHarness

        sys.path.insert(0, str(PLATFORM_DIR.parent / "operator"))
        import recovery

        self._install_canonical_schema()
        with tempfile.TemporaryDirectory(prefix="recovery-root-pg-") as directory:
            root = Path(directory)
            harness = AdmissionHarness(root / "admission")
            facts = FactStore(self._connect, harness.store)
            admission = build_run_admission(harness.spec)
            command = build_freeze_command(harness.spec)
            dispatch_conf = DispatchConf(
                schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
                run_id=admission.run_id,
                entry_command_id=command.command_id,
            )
            dispatch_artifact = harness.store.seal_dispatch_conf(
                dispatch_conf,
                producer_run_id=admission.run_id,
                producer_tool_sha256=digest({"transport": "airflow-api"}),
            )
            dispatch = DispatchCommand.create(
                command_id="dispatch-" + admission.run_id,
                run_id=admission.run_id,
                entry_command_id=command.command_id,
                idempotency_key=f"{admission.run_id}/dispatch",
                dag_id="solidworks_to_urdf",
                dag_run_id=f"portal-{admission.run_id}",
                conf_artifact=dispatch_artifact,
                expected_executor_sha256=digest({"transport": "airflow-api"}),
            )
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(f"UPDATE \"{self.schema}\".admission SET state = 'open' WHERE id = 1")
            command, _dispatch, _dispatch_effect = facts.admit_run(
                admission,
                command,
                dispatch,
                dispatch_artifact,
            )
            workspace_root = root / "workspaces"
            services = CanonicalStageServices(
                facts=facts,
                artifacts=harness.store,
                work_root=workspace_root,
                claim_policy=ClaimPolicy(
                    owner_sha256=digest({"process": "recovery-root"}),
                    executor_sha256=harness.spec.executors.freeze,
                    lease=timedelta(minutes=5),
                ),
                clock=lambda: harness.spec.created_at + timedelta(minutes=1),
            )
            claim = services.acquire_claim(command)
            workspace = services.materialize(command, claim, candidate=None)
            effects = services.effect_port(command, claim, workspace)
            outbound = workspace.root / "outbound"
            outbound.mkdir()
            (outbound / "request.json").write_text('{"operation":"native"}\n', encoding="utf-8")
            intent = effects.prepare_effect(
                effect_key="native",
                effect_kind="native_operation",
                target={"endpoint": "native-fixture"},
                expected_state={"state": "complete"},
                outbound_root=outbound,
            )
            effects.authorize_dispatch(intent.effect_id, source="native-executor")
            raw_response = workspace.root / "raw-native-response.bin"
            raw_response.write_bytes(b"\x00unsealed-native-response\xff")
            candidate_root = workspace.root / "candidate"
            candidate_root.mkdir()
            (candidate_root / "candidate.json").write_text('{"candidate":true}\n', encoding="utf-8")
            candidate = services.seal_candidate(
                command,
                claim,
                StageExecution(
                    outcome="completed",
                    candidate_root=str(candidate_root),
                    checks_sha256=digest({"checks": "candidate"}),
                    resources_settled=True,
                ),
            )

            failure_run = "run-failure-history"
            failure_stage = "discover"
            self._insert_run(failure_run)
            failure_command = self._insert_stage_command(failure_run, failure_stage)
            failure_root = workspace_root / failure_command / "failure-evidence"
            failure_root.mkdir(parents=True)
            (failure_root / "diagnostic.bin").write_bytes(b"failed-native-evidence")
            with tempfile.TemporaryDirectory(prefix="failure-artifact-") as evidence_directory:
                evidence_source = Path(evidence_directory)
                (evidence_source / "failure.json").write_text(
                    '{"error":"native failure"}\n',
                    encoding="utf-8",
                )
                evidence = harness.store.seal_tree(
                    evidence_source,
                    kind="failure_evidence",
                    producer_run_id=failure_run,
                    producer_stage=failure_stage,
                    producer_tool_sha256=digest({"executor": failure_stage}),
                )
            facts.register_artifact(evidence)
            failure_envelope = {
                "actual_executor_sha256": digest({"executor": failure_stage}),
                "command_id": failure_command,
                "effect_states": [],
                "error_message": "native failure",
                "error_detail": None,
                "error_type": "NativeFailure",
                "evidence_artifacts": [evidence.to_record()],
                "occurred_at": harness.spec.created_at.isoformat(),
                "outcome": "failed",
                "reason_code": "native_failed",
                "request_digest": digest({"request": failure_command}),
                "retained_locations": [str(failure_root)],
                "run_id": failure_run,
                "schema_version": "description-pipeline.failure-envelope/v1",
                "stage": failure_stage,
                "unavailable_reason": None,
            }
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO stage_decision (
                        decision_id, run_id, stage, command_id, outcome,
                        actual_executor_sha256, reason_code, failure_envelope,
                        failure_digest, decided_at
                    ) VALUES (
                        %s, %s, %s, %s, 'failed', %s, %s, %s::jsonb, %s, %s
                    )
                    """,
                    (
                        "decision-" + failure_run,
                        failure_run,
                        failure_stage,
                        failure_command,
                        digest({"executor": failure_stage}),
                        "native_failed",
                        json.dumps(failure_envelope),
                        digest(failure_envelope),
                        harness.spec.created_at,
                    ),
                )

            future_run = "run-future-input"
            with tempfile.TemporaryDirectory(prefix="future-input-artifact-") as future_directory:
                future_source = Path(future_directory)
                (future_source / "repository-snapshot.json").write_text(
                    '{"repository":"owner/model","head":"future"}\n',
                    encoding="utf-8",
                )
                future_artifact = harness.store.seal_tree(
                    future_source,
                    kind="repository_snapshot",
                    producer_run_id=None,
                    producer_stage=None,
                    producer_tool_sha256=digest({"tool": "repository-reader"}),
                )
            facts.register_artifact(future_artifact)
            self._insert_run(
                future_run,
                request={
                    "purpose": "engineering",
                    "future_stage_input": future_artifact.to_record(),
                },
            )

            query = recovery.RECOVERY_ROOT_QUERY.replace("platform.", f'"{self.schema}".')
            recovery_root = recovery.validate_recovery_root(self._query(query)[0][0])
            artifact_ids = {item["artifact_id"] for item in recovery_root["artifacts"]}
            self.assertEqual(recovery_root["missing_artifact_ids"], [])
            self.assertIn(intent.outbound_artifact_id, artifact_ids)
            self.assertIn(candidate.result.output_artifact.artifact_id, artifact_ids)
            self.assertIn(candidate.result_record_artifact.artifact_id, artifact_ids)
            self.assertIn(dispatch_artifact.artifact_id, artifact_ids)
            self.assertIn(harness.originals_ref.artifact_id, artifact_ids)
            self.assertIn(harness.ruleset_ref.artifact_id, artifact_ids)
            self.assertIn(harness.configuration_ref.artifact_id, artifact_ids)
            self.assertIn(evidence.artifact_id, artifact_ids)
            self.assertIn(future_artifact.artifact_id, artifact_ids)
            self.assertIn(str(failure_root), recovery_root["retained_locations"])
            self.assertIn(command.command_id, recovery_root["workspace_commands"])
            self.assertEqual(
                {item["candidate_id"] for item in recovery_root["candidates"]},
                {candidate.candidate_id},
            )

            preserved = recovery.preserved_workspace_roots(
                SimpleNamespace(resolved={"storage": {"workspace_root": str(workspace_root)}}),
                recovery_root,
            )
            source_selection = recovery.checkpoint_artifact_selection(recovery_root)
            target_artifacts = root / "restored-artifacts"
            recovery.copy_selected_root(
                harness.store.root,
                target_artifacts,
                source_selection,
            )
            target_workspaces = root / "restored-workspaces"
            for item in preserved:
                relative = Path(item["path"]).relative_to(workspace_root)
                recovery.copy_tree_strict(
                    Path(item["path"]),
                    target_workspaces / relative,
                )

            target_schema = "restore_" + uuid.uuid4().hex
            with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
                cursor.execute(
                    _sql(
                        "bootstrap.sql",
                        platform_schema=target_schema,
                        airflow_schema=self.airflow_schema,
                        database=self.database,
                    )
                )
                cursor.execute(
                    _sql(
                        "schema.sql",
                        platform_schema=target_schema,
                        airflow_schema=self.airflow_schema,
                        database=self.database,
                    )
                )
                for table in (
                    "run_admission",
                    "artifact",
                    "command",
                    "command_input_artifact",
                    "execution_claim",
                    "stage_decision",
                    "stage_candidate",
                    "effect_intent",
                    "effect_fact",
                    "run_display_metadata",
                ):
                    cursor.execute(f'INSERT INTO "{target_schema}".{table} SELECT * FROM "{self.schema}".{table}')
                cursor.execute(f"UPDATE \"{target_schema}\".admission SET state = 'open' WHERE id = 1")

            def target_connect():
                return psycopg2.connect(
                    self.server.get_uri(),
                    options=f"-c search_path={target_schema},public",
                )

            target_facts = FactStore(target_connect, ArtifactStore(target_artifacts))
            restored_command = target_facts.load_command(command.command_id)
            restored_candidate = target_facts.candidate_for(restored_command)
            self.assertEqual(restored_candidate, candidate)
            self.assertEqual(
                target_facts.effect_state(intent.effect_id),
                EffectState.UNKNOWN,
            )
            with self.assertRaisesRegex(FactConflict, "observe or reconcile"):
                target_facts.authorize_effect_dispatch(
                    effect_id=intent.effect_id,
                    claim=claim,
                    source="stale-restored-executor",
                    payload_digest=digest({"retry": "forbidden"}),
                    observed_at=harness.spec.created_at + timedelta(minutes=2),
                    fact_id="retry-after-restore",
                )
            restored_workspace = target_workspaces / command.command_id
            self.assertEqual(
                next(restored_workspace.glob("*/raw-native-response.bin")).read_bytes(),
                b"\x00unsealed-native-response\xff",
            )
            self.assertEqual(
                (target_workspaces / failure_command / "failure-evidence" / "diagnostic.bin").read_bytes(),
                b"failed-native-evidence",
            )
            restored_query = recovery.RECOVERY_ROOT_QUERY.replace("platform.", f'"{target_schema}".')
            with target_connect() as connection, connection.cursor() as cursor:
                cursor.execute(restored_query)
                restored_root = recovery.validate_recovery_root(cursor.fetchone()[0])
            self.assertEqual(restored_root, recovery_root)
            recovery.verify_checkpoint_index(
                recovery.checkpoint_index(restored_root, target_artifacts),
                target_artifacts,
            )

    def test_run_control_store_reads_canonical_runs_and_owns_display_metadata(self) -> None:
        self._install_canonical_schema()
        self._insert_run("run-parent")
        self._insert_run("run-child", parent_run_id="run-parent", from_stage="freeze")
        store = RunControlStore(self._connect)

        summaries = {row.run_id: row for row in store.list_admitted_runs()}
        self.assertEqual(set(summaries), {"run-parent", "run-child"})
        self.assertEqual(summaries["run-child"].parent_run_id, "run-parent")
        self.assertEqual(summaries["run-child"].from_stage, "freeze")
        self.assertEqual(summaries["run-child"].state, "incomplete")
        self.assertEqual(store.get_admitted_run("run-parent"), summaries["run-parent"])
        self.assertIsNone(store.get_admitted_run("run-missing"))

        renamed = store.rename_run("run-child", "Custom title")
        self.assertEqual(renamed.display_name, "Custom title")
        self.assertIsNone(renamed.hidden_at)
        hidden = store.hide_run("run-child", actor="operator")
        self.assertEqual(hidden.hidden_reason, "operator")
        repeat = store.hide_run("run-child", actor="another operator")
        self.assertEqual(repeat.hidden_at, hidden.hidden_at)
        self.assertEqual(repeat.hidden_reason, "operator")
        renamed_while_hidden = store.rename_run("run-child", None)
        self.assertTrue(renamed_while_hidden.hidden_at is not None)
        self.assertIsNone(renamed_while_hidden.display_name)
        restored = store.restore_run("run-child")
        self.assertIsNone(restored.hidden_at)
        self.assertIsNone(restored.hidden_reason)
        self.assertEqual(store.get_run_display_metadata("run-child"), restored)
        self.assertEqual(store.list_run_display_metadata(), {"run-child": restored})

        with self.assertRaisesRegex(FactStoreError, "not admitted"):
            store.rename_run("run-missing", "missing")
        with self.assertRaisesRegex(FactStoreError, "not admitted"):
            store.hide_run("run-missing", actor="operator")
        with self.assertRaisesRegex(FactStoreError, "not admitted"):
            store.restore_run("run-missing")

    def test_run_control_uses_the_shared_projector_for_formal_runs(self) -> None:
        self._install_canonical_schema()

        self._insert_run("run-freeze-only")
        entry_command = self._insert_stage_command("run-freeze-only", "freeze")
        self._insert_decision(
            "run-freeze-only",
            "freeze",
            entry_command,
            outcome="completed",
        )
        store = RunControlStore(self._connect)
        self.assertEqual(
            store.get_admitted_run("run-freeze-only").state,
            "incomplete",
        )

        run_id = "run-reconciled-effect"
        self._insert_run(run_id)
        commands = {}
        for stage in ("freeze", "discover", "capture", "generate", "verify", "publish"):
            command_id = self._insert_stage_command(run_id, stage)
            commands[stage] = command_id
            self._insert_decision(run_id, stage, command_id, outcome="completed")

        claim_id = self._insert_open_claim(commands["discover"], stage="discover")
        self._insert_artifact(
            "reconciled-outbound",
            run_id=run_id,
            stage="discover",
            kind="effect_outbound",
        )
        self._insert_artifact(
            "reconciled-payload",
            run_id=run_id,
            stage="discover",
            kind="effect_payload",
        )
        self._insert_artifact(
            "reconciled-metadata",
            run_id=run_id,
            stage="discover",
            kind="effect_reconciliation_metadata",
        )
        now = datetime(2026, 10, 25, tzinfo=UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO effect_intent (
                    effect_id, command_id, effect_key, effect_kind, target,
                    expected_state, expected_state_digest, outbound_artifact_id,
                    outbound_digest, created_at
                ) VALUES (
                    %s, %s, 'native-discover', 'native_operation', '{}'::jsonb,
                    '{}'::jsonb, %s, %s, %s, %s
                )
                """,
                (
                    "effect-reconciled",
                    commands["discover"],
                    digest({}),
                    "reconciled-outbound",
                    digest({"outbound": "reconciled"}),
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO effect_fact (
                    fact_id, effect_id, seq, claim_id, fence, fact_type,
                    source, state_class, payload_artifact_id, payload_digest,
                    metadata_artifact_id, observed_at
                ) VALUES
                    ('fact-authorized', 'effect-reconciled', 1, %s, 1,
                     'dispatch_authorized', 'fixture', NULL, NULL, %s, NULL, %s),
                    ('fact-conflict', 'effect-reconciled', 2, NULL, NULL,
                     'conflict', 'fixture', NULL, NULL, %s, NULL, %s),
                    ('fact-reconciled', 'effect-reconciled', 3, NULL, NULL,
                     'reconciled', 'effect-reconciler:' || %s, 'known', %s, %s, %s, %s),
                    ('fact-old-fence', 'effect-reconciled', 4, %s, 1,
                     'response', 'fixture', NULL, NULL, %s, NULL, %s)
                """,
                (
                    claim_id,
                    digest({"fact": "authorized"}),
                    now,
                    digest({"fact": "conflict"}),
                    now + timedelta(seconds=1),
                    "a" * 64,
                    "reconciled-payload",
                    digest({"fact": "reconciled"}),
                    "reconciled-metadata",
                    now + timedelta(seconds=2),
                    claim_id,
                    digest({"fact": "old-fence"}),
                    now + timedelta(seconds=3),
                ),
            )

        self.assertEqual(
            store.get_admitted_run(run_id).state,
            "succeeded",
        )
        self.assertEqual(
            self._query(
                """
                SELECT object_kind, object_id
                FROM lifecycle_projection
                WHERE object_kind IN ('run', 'stage', 'effect')
                """
            ),
            [],
            "SQL must not carry a second business reducer",
        )

    def test_run_control_diagnostic_state_uses_only_the_unique_target_stage(self) -> None:
        self._install_canonical_schema()
        created_at = datetime(2026, 10, 25, tzinfo=UTC)
        artifact = ArtifactRef(
            artifact_id="diagnostic-input",
            kind="diagnostic_input",
            locator="cas://artifact/diagnostic-input",
            manifest_sha256=digest({"diagnostic": "manifest"}),
            set_sha256=digest({"diagnostic": "set"}),
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"diagnostic": "tool"}),
        )
        spec = DiagnosticAdmissionSpec(
            schema_version=DIAGNOSTIC_ADMISSION_SCHEMA,
            run_id="run-diagnostic",
            request_id="request-diagnostic",
            target_stage="verify",
            request={"diagnostic": "verify"},
            input_artifacts=(artifact,),
            expected_executor_sha256=digest({"executor": "verify"}),
            config_snapshot_sha256=digest({"config": 1}),
            contract_sha256=digest({"contract": 1}),
            policy_sha256=digest({"policy": 1}),
            created_at=created_at,
        )
        admission = build_diagnostic_run_admission(spec)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO run_admission (
                    run_id, request_id, parent_run_id, from_stage,
                    request, request_digest, created_at
                ) VALUES (%s, %s, NULL, %s, %s::jsonb, %s, %s)
                """,
                (
                    admission.run_id,
                    admission.request_id,
                    admission.from_stage,
                    json.dumps(spec.to_record()),
                    admission.request_digest,
                    admission.created_at,
                ),
            )
        verify_command = self._insert_stage_command("run-diagnostic", "verify")
        self._insert_decision(
            "run-diagnostic",
            "verify",
            verify_command,
            outcome="completed",
        )
        store = RunControlStore(self._connect)
        self.assertEqual(store.get_admitted_run("run-diagnostic").state, "succeeded")

        capture_command = self._insert_stage_command("run-diagnostic", "capture")
        self._insert_decision(
            "run-diagnostic",
            "capture",
            capture_command,
            outcome="failed",
        )
        with self.assertRaisesRegex(FactStoreError, "unique target stage"):
            store.get_admitted_run("run-diagnostic")

    def test_lifecycle_gate_reads_shared_run_state_as_operator_ro(self) -> None:
        self._install_canonical_schema()
        self._insert_run("run-gate")
        with mock.patch.dict(
            os.environ,
            {
                "PLATFORM_OPERATOR_DSN": psycopg2.extensions.make_dsn(
                    self.server.get_uri(),
                    user="operator_ro",
                )
            },
            clear=False,
        ):
            projection = collect_projection()
            operator_dsn = os.environ["PLATFORM_OPERATOR_DSN"]
            with psycopg2.connect(operator_dsn) as connection:
                shared_projection = read_run_projection(
                    connection,
                    "run-gate",
                )
        run_rows = [row for row in projection["rows"] if row["object_kind"] == "run"]
        self.assertEqual(shared_projection["run_id"], "run-gate")
        self.assertEqual(shared_projection["state"], "running")
        self.assertEqual(
            [(row["run_id"], row["state"], row["bucket"]) for row in run_rows],
            [("run-gate", "incomplete", "in_flight")],
        )
        self.assertIn(
            ("request", "request-run-gate", None),
            [(row["object_kind"], row["object_id"], row["run_id"]) for row in projection["rows"]],
        )

    def _insert_scheduling_claim(
        self,
        run_id: str,
        *,
        dag_id: str | None,
        dag_run_id: str | None,
        task_id: str | None = None,
        map_index: int | None = None,
    ) -> str:
        command_id = f"{run_id}:scheduling"
        claim_id = f"claim-{command_id}"
        task_bound = task_id is not None or map_index is not None
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO command (
                    command_id, run_id, kind, stage, idempotency_key, request,
                    request_digest, config_snapshot_sha256, contract_sha256,
                    policy_sha256, expected_executor_sha256
                ) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, %s, %s, %s)
                """,
                (
                    command_id,
                    run_id,
                    "stage" if task_bound else "dispatch",
                    "discover" if task_bound else None,
                    command_id,
                    *[digest({"scheduling": command_id})] * 5,
                ),
            )
            cursor.execute(
                """
                INSERT INTO execution_claim (
                    claim_id, command_id, fence, owner_sha256, executor_sha256,
                    dag_id, dag_run_id, task_id, map_index,
                    issued_at, last_seen_at, expires_at, closed_at, closure_code
                ) VALUES (
                    %s, %s, 1, %s, %s, %s, %s, %s, %s,
                    now(), now(), now(), now(), 'completed'
                )
                """,
                (
                    claim_id,
                    command_id,
                    digest({"owner": claim_id}),
                    digest({"scheduling": command_id}),
                    dag_id,
                    dag_run_id,
                    task_id,
                    map_index,
                ),
            )
        return claim_id

    def test_terminal_dispatch_claim_allows_real_operator_stop_gate(self) -> None:
        self._install_canonical_schema()
        run_id = "run-dispatch-failed"
        self._insert_run(run_id)
        command_id = self._insert_stage_command(run_id, "freeze")
        self._insert_decision(run_id, "freeze", command_id, outcome="failed")
        self._insert_scheduling_claim(run_id, dag_id="pipeline", dag_run_id=run_id)
        self._query(
            f'INSERT INTO "{self.airflow_schema}".dag_run (dag_id, run_id, state) '
            "VALUES ('pipeline', %s, 'failed') RETURNING run_id",
            (run_id,),
        )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "PLATFORM_OPERATOR_DSN": psycopg2.extensions.make_dsn(
                        self.server.get_uri(),
                        user="operator_ro",
                    )
                },
            ),
            mock.patch("sys.stdout"),
        ):
            projection = collect_projection()
            self.assertEqual(
                lifecycle_main(
                    [
                        "report",
                        "--require-settled",
                        "--require-no-unresolved",
                        "--require-admission-closed",
                    ]
                ),
                0,
            )
        scheduler_rows = [row for row in projection["rows"] if row["object_kind"] in {"dag_run", "scheduling"}]
        self.assertEqual(
            [(row["object_kind"], row["state"], row["bucket"]) for row in scheduler_rows],
            [("dag_run", "failed", "terminal")],
        )

    def test_dispatch_projection_preserves_missing_unknown_and_active_dags(self) -> None:
        self._install_canonical_schema()
        for state, bucket in (
            ("success", "terminal"),
            ("failed", "terminal"),
            ("queued", "in_flight"),
            ("running", "in_flight"),
            (None, "unknown"),
            ("unexpected", "unknown"),
            ("missing", "unknown"),
        ):
            with self.subTest(state=state):
                run_id = "run-dag-" + str(state)
                self._insert_run(run_id)
                self._insert_scheduling_claim(run_id, dag_id="pipeline", dag_run_id=run_id)
                if state != "missing":
                    self._query(
                        f'INSERT INTO "{self.airflow_schema}".dag_run (dag_id, run_id, state) '
                        "VALUES ('pipeline', %s, %s) RETURNING run_id",
                        (run_id, state),
                    )
                self.assertEqual(
                    self._query(
                        "SELECT object_kind, bucket FROM lifecycle_projection WHERE run_id = %s",
                        (run_id,),
                    ),
                    [("dag_run", bucket)],
                )

    def test_task_projection_preserves_missing_unknown_active_and_partial_bindings(self) -> None:
        self._install_canonical_schema()
        cases = [
            ("success", "terminal", "task", -1, "pipeline", "bound"),
            ("failed", "terminal", "task", -1, "pipeline", "bound"),
            ("running", "in_flight", "task", -1, "pipeline", "bound"),
            ("deferred", "in_flight", "task", -1, "pipeline", "bound"),
            (None, "unknown", "task", -1, "pipeline", "bound"),
            ("unexpected", "unknown", "task", -1, "pipeline", "bound"),
            ("missing", "unknown", "task", -1, "pipeline", "bound"),
            ("partial-map", "unknown", None, -1, "pipeline", "bound"),
            ("partial-task", "unknown", "task", None, "pipeline", "bound"),
            ("partial-dag", "unknown", None, None, "pipeline", None),
            ("partial-run", "unknown", None, None, None, "bound"),
        ]
        for index, (state, bucket, task_id, map_index, dag_id, dag_run_id) in enumerate(cases):
            with self.subTest(state=state):
                run_id = f"run-task-{index}"
                dag_run_id = f"bound-{index}" if dag_run_id is not None else None
                self._insert_run(run_id)
                claim_id = self._insert_scheduling_claim(
                    run_id,
                    dag_id=dag_id,
                    dag_run_id=dag_run_id,
                    task_id=task_id,
                    map_index=map_index,
                )
                if state in {"success", "failed", "running", "deferred", None, "unexpected"}:
                    self._query(
                        f'INSERT INTO "{self.airflow_schema}".task_instance '
                        "(dag_id, task_id, run_id, map_index, state) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING task_id",
                        (dag_id, task_id, dag_run_id, map_index, state),
                    )
                rows = self._query(
                    "SELECT object_kind, object_id, bucket FROM lifecycle_projection WHERE run_id = %s",
                    (run_id,),
                )
                expected_kind = "scheduling" if task_id is not None or map_index is not None else "dag_run"
                selected = [row for row in rows if row[0] == expected_kind]
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0][2], bucket)
                if expected_kind == "scheduling":
                    self.assertEqual(selected[0][1], claim_id)


if __name__ == "__main__":
    unittest.main()
