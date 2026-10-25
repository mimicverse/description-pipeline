"""Real PostgreSQL proof for the canonical schema cutover."""

from __future__ import annotations

import argparse
import json
import os
import fcntl
import importlib.util
import multiprocessing
import subprocess
import sys
import tarfile
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
from description_pipeline.orchestration.lifecycle_state import collect_projection
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.pipeline.diagnostic import (
    DiagnosticAdmissionSpec,
    build_diagnostic_run_admission,
)
from description_pipeline.platform.facts import (
    FactStoreError,
    RunControlStore,
    read_admitted_runs,
)
from description_pipeline.schemas import (
    AIRFLOW_DISPATCH_CONF_SCHEMA,
    DIAGNOSTIC_ADMISSION_SCHEMA,
)

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "platform"
APPLY = PLATFORM_DIR / "apply.sh"
PG_BIN = Path(pgserver.__file__).resolve().parent / "pginstall" / "bin" if pgserver else None


def _legacy_cutover_module():
    spec = importlib.util.spec_from_file_location(
        "legacy_cutover_test_module",
        PLATFORM_DIR / "legacy_cutover.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("legacy cutover module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _attempt_legacy_ddl(
    uri: str,
    statement: str,
    start,
    result,
) -> None:
    import psycopg2

    start.wait(10)
    try:
        with psycopg2.connect(uri) as connection, connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '500ms'")
            cursor.execute(statement)
    except psycopg2.errors.LockNotAvailable:
        result.put(("locked", statement))
    except Exception as error:
        result.put(("error", statement, type(error).__name__, str(error)))
    else:
        result.put(("changed", statement))


def _sql(
    name: str,
    *,
    platform_schema: str,
    airflow_schema: str,
    database: str,
    legacy_archive_sha256: str = "e" * 64,
) -> str:
    text = (PLATFORM_DIR / name).read_text(encoding="utf-8")
    return (
        text.replace(':"platform_schema"', f'"{platform_schema}"')
        .replace(':"airflow_schema"', f'"{airflow_schema}"')
        .replace(':"platform_database"', f'"{database}"')
        .replace(":'legacy_archive_sha256'", f"'{legacy_archive_sha256}'")
    )


@unittest.skipUnless(PG_AVAILABLE, "pgserver/psycopg2 are not installed")
class CanonicalSchemaCutoverPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(prefix="schema-cutover-postgres-")
        cls.server = pgserver.get_server(Path(cls.server_root.name) / "pgdata", cleanup_mode="delete")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.schema = "cutover_" + uuid.uuid4().hex
        self.airflow_schema = "airflow_cutover_" + uuid.uuid4().hex
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

    def _apply(
        self,
        *names: str,
        legacy_archive_sha256: str = "e" * 64,
        cluster_system_identifier: str | None = None,
        archive_platform_schema: str | None = None,
    ) -> None:
        receipt_schema = archive_platform_schema or self.schema
        with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
            for name in names:
                if name == "schema.sql":
                    cursor.execute(
                        """
                        SELECT
                            set_config('description.legacy_receipt_sha256', %s, false),
                            set_config('description.legacy_archive_sha256', %s, false),
                            set_config('description.legacy_evidence_sha256', %s, false),
                            set_config(
                                'description.legacy_archive_cluster_system_identifier',
                                COALESCE(
                                    %s,
                                    (
                                        SELECT system_identifier::text
                                        FROM pg_control_system()
                                    )
                                ),
                                false
                            ),
                            set_config('description.legacy_archive_database', current_database(), false),
                            set_config(
                                'description.legacy_archive_database_oid',
                                (SELECT oid::text FROM pg_database WHERE datname = current_database()),
                                false
                            ),
                            set_config('description.legacy_archive_platform_schema', %s, false),
                            set_config(
                                'description.legacy_archive_platform_schema_oid',
                                (SELECT oid::text FROM pg_namespace WHERE nspname = %s),
                                false
                            ),
                            set_config('description.legacy_archive_migration_role', current_user, false)
                        """,
                        (
                            legacy_archive_sha256,
                            legacy_archive_sha256,
                            legacy_archive_sha256,
                            cluster_system_identifier,
                            receipt_schema,
                            receipt_schema,
                        ),
                    )
                cursor.execute(
                    _sql(
                        name,
                        platform_schema=self.schema,
                        airflow_schema=self.airflow_schema,
                        database=self.database,
                        legacy_archive_sha256=legacy_archive_sha256,
                    )
                )

    def _query(self, statement, parameters=()):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def _apply_without_receipt(self) -> subprocess.CompletedProcess[str]:
        parsed = urllib.parse.urlsplit(self.server.get_uri())
        host = (
            urllib.parse.parse_qs(parsed.query).get("host", [""])[0]
            or parsed.hostname
            or "localhost"
        )
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
            env={
                **os.environ,
                "PLATFORM_ARCHIVE_PYTHON": sys.executable,
            },
        )

    def _apply_script(
        self,
        receipt: Path,
        *,
        user: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        parsed = urllib.parse.urlsplit(self.server.get_uri())
        host = (
            urllib.parse.parse_qs(parsed.query).get("host", [""])[0]
            or parsed.hostname
            or "localhost"
        )
        directory = receipt.parent / f"maintenance-{uuid.uuid4().hex}"
        directory.mkdir()
        lock_path = directory / "maintenance.lock"
        owner = digest({"lock": str(lock_path)})
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                    "owner_id": owner,
                    "acquired_at": "2026-10-25T00:00:00Z",
                    "pid": os.getpid(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            environment = {
                **os.environ,
                "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
                "OPERATORCTL_MAINTENANCE_LOCK_FD": str(descriptor),
                "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
                "PLATFORM_ARCHIVE_PYTHON": sys.executable,
            }
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
                    user or parsed.username or "postgres",
                    "--platform-schema",
                    self.schema,
                    "--airflow-schema",
                    self.airflow_schema,
                    "--legacy-archive-receipt",
                    str(receipt),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                pass_fds=(descriptor,),
            )
        finally:
            os.close(descriptor)

    def _prepare_archive(
        self,
        receipt: Path,
        *,
        user: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        parsed = urllib.parse.urlsplit(self.server.get_uri())
        host = (
            urllib.parse.parse_qs(parsed.query).get("host", [""])[0]
            or parsed.hostname
            or "localhost"
        )
        directory = receipt.parent / f"maintenance-{uuid.uuid4().hex}"
        directory.mkdir()
        lock_path = directory / "maintenance.lock"
        owner = digest({"lock": str(lock_path)})
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                    "owner_id": owner,
                    "acquired_at": "2026-10-25T00:00:00Z",
                    "pid": os.getpid(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            environment = {
                **os.environ,
                "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
                "OPERATORCTL_MAINTENANCE_LOCK_FD": str(descriptor),
                "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
            }
            return subprocess.run(
                [
                    sys.executable,
                    str(PLATFORM_DIR / "legacy_cutover.py"),
                    "prepare",
                    "--receipt",
                    str(receipt),
                    "--psql",
                    str(PG_BIN / "psql"),
                    "--pg-dump",
                    str(PG_BIN / "pg_dump"),
                    "--pg-restore",
                    str(PG_BIN / "pg_restore"),
                    "--initdb",
                    str(PG_BIN / "initdb"),
                    "--pg-ctl",
                    str(PG_BIN / "pg_ctl"),
                    "--createdb",
                    str(PG_BIN / "createdb"),
                    "--database",
                    self.database,
                    "--platform-schema",
                    self.schema,
                    "--airflow-schema",
                    self.airflow_schema,
                    "--schema-file",
                    str(PLATFORM_DIR / "schema.sql"),
                    "--views-file",
                    str(PLATFORM_DIR / "views.sql"),
                    "--roles-file",
                    str(PLATFORM_DIR / "roles.sql"),
                    "--libpq",
                    f"h={host}",
                    "--libpq",
                    f"U={user or parsed.username or 'postgres'}",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                pass_fds=(descriptor,),
            )
        finally:
            os.close(descriptor)

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

    def _create_legacy_run_table(self) -> None:
        with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE run (
                    run_id text PRIMARY KEY,
                    display_name text,
                    hidden_at timestamptz,
                    hidden_reason text,
                    updated_at timestamptz NOT NULL
                )
                """
            )

    def _create_legacy_lifecycle_schema(self, *, include_view: bool = True) -> None:
        with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE request (
                    request_id text PRIMARY KEY,
                    principal text NOT NULL,
                    target_hardware text NOT NULL,
                    main_assembly text,
                    configuration jsonb,
                    baseline jsonb,
                    sealed_at timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE run (
                    run_id text PRIMARY KEY,
                    request_id text NOT NULL REFERENCES request (request_id),
                    parent_run_id text REFERENCES run (run_id),
                    from_stage text,
                    state text NOT NULL,
                    tool_identity jsonb,
                    display_name text,
                    hidden_at timestamptz,
                    hidden_reason text,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE stage (
                    run_id text NOT NULL REFERENCES run (run_id),
                    stage text NOT NULL,
                    state text NOT NULL,
                    receipt jsonb,
                    checkpoint_uri text,
                    reused_from_run text,
                    original_executed_at timestamptz,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (run_id, stage)
                );
                CREATE TABLE operation (
                    operation_id text PRIMARY KEY,
                    run_id text NOT NULL REFERENCES run (run_id),
                    stage text,
                    kind text NOT NULL,
                    state text NOT NULL,
                    operation_key text NOT NULL UNIQUE,
                    params_digest text NOT NULL,
                    dag_id text,
                    dag_run_id text,
                    task_id text,
                    map_index integer,
                    try_number integer,
                    updated_at timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE run_event (
                    run_id text NOT NULL REFERENCES run (run_id),
                    seq bigint NOT NULL CHECK (seq > 0),
                    at timestamptz NOT NULL DEFAULT now(),
                    kind text NOT NULL,
                    stage text,
                    operation_id text,
                    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                    PRIMARY KEY (run_id, seq)
                );
                """
                + (
                    """
                    CREATE VIEW lifecycle_projection AS
                        SELECT run_id, state FROM run;
                    """
                    if include_view
                    else ""
                )
            )

    def _insert_legacy_unresolved(
        self,
        *,
        request_id: str,
        run_id: str,
        operation_id: str,
        display_name: str | None = None,
        configuration: str | None = None,
        recorded_at: datetime | None = None,
    ) -> None:
        recorded_at = recorded_at or datetime(2026, 10, 25, tzinfo=UTC)
        with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO request (
                    request_id, principal, target_hardware, configuration, sealed_at
                ) VALUES (%s, 'fixture', 'm3.0', %s::jsonb, %s)
                """,
                (request_id, configuration, recorded_at),
            )
            cursor.execute(
                """
                INSERT INTO run (
                    run_id, request_id, state, display_name,
                    created_at, updated_at
                ) VALUES (%s, %s, 'unresolved', %s, %s, %s)
                """,
                (run_id, request_id, display_name, recorded_at, recorded_at),
            )
            cursor.execute(
                """
                INSERT INTO stage (run_id, stage, state, updated_at)
                VALUES (%s, 'capture', 'unresolved', %s)
                """,
                (run_id, recorded_at),
            )
            cursor.execute(
                """
                INSERT INTO operation (
                    operation_id, run_id, stage, kind, state,
                    operation_key, params_digest, task_id, updated_at
                ) VALUES (
                    %s, %s, 'capture', 'native', 'unresolved',
                    %s, %s, 'capture', %s
                )
                """,
                (
                    operation_id,
                    run_id,
                    f"{run_id}/native",
                    digest({"params": run_id}),
                    recorded_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO run_event (
                    run_id, seq, at, kind, stage, operation_id
                ) VALUES (%s, 1, %s, 'native-unknown', 'capture', %s)
                """,
                (run_id, recorded_at, operation_id),
            )

    def _insert_run(
        self,
        run_id: str,
        *,
        parent_run_id: str | None = None,
        from_stage: str | None = None,
        request: dict | None = None,
    ) -> None:
        request = request or {"purpose": "engineering"}
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO run_admission (
                    run_id, request_id, parent_run_id, from_stage, request, request_digest
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    run_id,
                    f"request-{run_id}",
                    parent_run_id,
                    from_stage,
                    json.dumps(request),
                    digest({"run_id": run_id}),
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

    def _insert_open_claim(self, command_id: str, *, stage: str) -> str:
        claim_id = f"claim-{command_id}"
        now = datetime(2026, 10, 25, tzinfo=UTC)
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

    def test_cutover_archives_display_drops_legacy_tables_and_builds_canonical_view(self) -> None:
        run_id = "run-cutover"
        request_id = "request-cutover"
        self._install_canonical_schema()
        self._create_legacy_lifecycle_schema(include_view=False)
        with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO request (
                    request_id, principal, target_hardware
                ) VALUES (%s, 'fixture', 'm3.0')
                """,
                (request_id,),
            )
            cursor.execute(
                """
                INSERT INTO run (
                    run_id, request_id, state, display_name, updated_at
                ) VALUES (%s, %s, 'unresolved', %s, now())
                """,
                (run_id, request_id, "Archived display"),
            )
            cursor.execute(
                """
                INSERT INTO stage (run_id, stage, state)
                VALUES (%s, 'capture', 'unresolved')
                """,
                (run_id,),
            )
            cursor.execute(
                """
                INSERT INTO operation (
                    operation_id, run_id, stage, kind, state,
                    operation_key, params_digest, task_id
                ) VALUES (
                    %s, %s, 'capture', 'native', 'unresolved',
                    %s, %s, 'capture'
                )
                """,
                (
                    "operation-cutover",
                    run_id,
                    f"{run_id}/native",
                    digest({"params": run_id}),
                ),
            )
            cursor.execute(
                """
                INSERT INTO run_event (
                    run_id, seq, kind, stage, operation_id
                ) VALUES (%s, 1, 'native-unknown', 'capture', %s)
                """,
                (run_id, "operation-cutover"),
            )

        self._insert_run(run_id, request={"purpose": "cutover archive"})
        unresolved_command = self._insert_stage_command(run_id, "capture")
        unresolved_claim = self._insert_open_claim(
            unresolved_command,
            stage="capture",
        )
        self._insert_effect(
            run_id,
            "capture",
            unresolved_command,
            fact_type="dispatch_authorized",
            claim_id=unresolved_claim,
        )
        archived_airflow_time = datetime(2026, 10, 25, tzinfo=UTC)
        with self._role_connect("airflow_migrate") as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO "{self.airflow_schema}".task_instance (
                    dag_id, task_id, run_id, map_index, state,
                    try_number, start_date
                ) VALUES (
                    'solidworks_to_urdf', 'capture', 'archived-airflow',
                    0, 'running', 1, %s
                )
                """,
                (archived_airflow_time,),
            )
            cursor.execute(
                f"""
                INSERT INTO "{self.airflow_schema}".dag_run (
                    dag_id, run_id, state, start_date
                ) VALUES (
                    'solidworks_to_urdf', 'archived-airflow', 'running', %s
                )
                """,
                (archived_airflow_time,),
            )
        directory = Path(tempfile.mkdtemp(prefix="legacy-archive-", dir=self.server_root.name))
        receipt = directory / "legacy-cutover.json"
        completed = self._apply_script(receipt)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        archive = Path(str(receipt) + ".archive.tar.gz")
        self.assertTrue(receipt.is_file())
        self.assertTrue(archive.is_file())
        with tarfile.open(archive, "r:gz") as bundle:
            names = set(bundle.getnames())
            self.assertEqual(
                names,
                {
                    "manifest.json",
                    "evidence.json",
                    "platform.dump",
                    "schema.sql",
                    "toc.txt",
                },
            )
            evidence = json.loads(bundle.extractfile("evidence.json").read())
        self.assertEqual(
            evidence["unresolved"],
            {
                "legacy_operation": {"active": 0, "unresolved": 1},
                "legacy_run": {"active": 0, "unresolved": 1},
            },
        )
        self.assertIn("effect_fact", evidence["relations"])
        self.assertTrue(evidence["relations"]["effect_fact"]["rows"])
        self.assertIn("execution_claim", evidence["relations"])
        self.assertTrue(evidence["relations"]["execution_claim"]["rows"])
        self.assertEqual(
            len(evidence["airflow_relations"]["task_instance"]["rows"]),
            1,
        )
        self.assertIn(
            '"state": "running"',
            evidence["airflow_relations"]["task_instance"]["rows"][0],
        )
        self.assertEqual(
            len(evidence["airflow_relations"]["dag_run"]["rows"]),
            1,
        )

        self.assertEqual(
            self._query(
                "SELECT display_name FROM run_display_metadata WHERE run_id = %s",
                (run_id,),
            ),
            [("Archived display",)],
        )
        tables = {
            row[0]
            for row in self._query(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s",
                (self.schema,),
            )
        }
        self.assertEqual(
            tables,
            {
                "admission",
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
            },
        )
        for table in ("request", "run", "stage", "operation", "run_event"):
            self.assertNotIn(table, tables)

        command_id = "freeze:cutover"
        now = datetime(2026, 10, 25, tzinfo=UTC)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO command (
                    command_id, run_id, kind, stage, idempotency_key, request,
                    request_digest, config_snapshot_sha256, contract_sha256,
                    policy_sha256, expected_executor_sha256, predecessor_decision_id
                ) VALUES (
                    %s, %s, 'stage', 'freeze', %s, '{}'::jsonb,
                    %s, %s, %s, %s, %s, NULL
                )
                """,
                (
                    command_id,
                    run_id,
                    "cutover/freeze",
                    digest({"request": "freeze"}),
                    digest({"config": 1}),
                    digest({"contract": 1}),
                    digest({"policy": 1}),
                    digest({"executor": "freeze"}),
                ),
            )
            cursor.execute(
                """
                INSERT INTO execution_claim (
                    claim_id, command_id, fence, owner_sha256, executor_sha256,
                    lane_key, dag_id, dag_run_id, task_id, map_index,
                    issued_at, last_seen_at, expires_at
                ) VALUES (
                    %s, %s, 1, %s, %s, NULL, 'solidworks_to_urdf',
                    'portal-cutover', 'freeze', -1, %s, %s, %s
                )
                """,
                (
                    "claim-cutover",
                    command_id,
                    digest({"owner": 1}),
                    digest({"executor": "freeze"}),
                    now,
                    now,
                    now + timedelta(minutes=5),
                ),
            )
            cursor.execute(
                f"""
                INSERT INTO "{self.airflow_schema}".task_instance (
                    dag_id, task_id, run_id, map_index, state, try_number, start_date
                ) VALUES ('solidworks_to_urdf', 'freeze', 'portal-cutover', -1, 'running', 1, %s)
                """,
                (now,),
            )
            cursor.execute(
                f"""
                INSERT INTO "{self.airflow_schema}".dag_run (
                    dag_id, run_id, state, start_date
                ) VALUES ('solidworks_to_urdf', 'portal-cutover', 'running', %s)
                """,
                (now,),
            )

        rows = self._query(
            "SELECT object_kind, state, bucket FROM lifecycle_projection "
            "WHERE run_id = %s ORDER BY object_kind",
            (run_id,),
        )
        self.assertNotIn("run", {row[0] for row in rows})
        self.assertNotIn("stage", {row[0] for row in rows})
        self.assertIn(("scheduling", "running", "in_flight"), rows)
        self.assertIn(("dag_run", "running", "in_flight"), rows)
        self.assertEqual(
            RunControlStore(self._connect).get_admitted_run(run_id).state,
            "unresolved",
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SET ROLE operator_ro")
            self.assertEqual(
                read_admitted_runs(connection, run_id=run_id)[0].state,
                "unresolved",
            )
        self._query(
            "UPDATE execution_claim SET task_id = NULL "
            "WHERE claim_id = %s RETURNING claim_id",
            ("claim-cutover",),
        )
        self.assertEqual(
            self._query(
                "SELECT state, bucket FROM lifecycle_projection "
                "WHERE object_kind = 'scheduling' AND object_id = %s",
                ("claim-cutover",),
            ),
            [(None, "unknown")],
        )
        self.assertEqual(
            self._query(
                "SELECT has_table_privilege('operator_ro', %s, 'SELECT'),"
                " has_table_privilege('operator_ro', %s, 'SELECT')",
                (
                    f"{self.schema}.lifecycle_projection",
                    f"{self.schema}.command",
                ),
            ),
            [(True, True)],
        )

    def test_legacy_cutover_requires_an_archive_receipt(self) -> None:
        self._create_legacy_run_table()
        with self.assertRaises(psycopg2.errors.RaiseException) as raised:
            self._apply("schema.sql", legacy_archive_sha256="")
        self.assertIn("legacy_lifecycle_archive_receipt_invalid", str(raised.exception))
        self.assertEqual(
            self._query(
                "SELECT to_regclass(%s)::text",
                (f"{self.schema}.run",),
            ),
            [("run",)],
        )

    def test_fresh_apply_without_receipt_uses_actual_platform_schema(self) -> None:
        self._create_airflow_tables()

        first = self._apply_without_receipt()
        second = self._apply_without_receipt()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            self._query(
                "SELECT to_regclass(%s)::text",
                (f"{self.schema}.run_admission",),
            ),
            [("run_admission",)],
        )

    def test_guard_inventory_is_bound_to_deployed_platform_schema(self) -> None:
        self._create_legacy_lifecycle_schema()
        self._insert_legacy_unresolved(
            request_id="request-decoy-schema",
            run_id="run-decoy-schema",
            operation_id="operation-decoy-schema",
        )
        decoy_schema = "decoy_" + uuid.uuid4().hex
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute(
                f'CREATE SCHEMA "{decoy_schema}" AUTHORIZATION platform_migrate'
            )

        with self.assertRaises(psycopg2.errors.RaiseException) as raised:
            self._apply(
                "schema.sql",
                archive_platform_schema=decoy_schema,
            )

        self.assertIn(
            "legacy_lifecycle_archive_receipt_invalid",
            str(raised.exception),
        )
        self.assertEqual(
            self._query(
                """
                SELECT c.relname
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = %s
                  AND c.relname IN (
                      'request', 'run', 'stage', 'operation', 'run_event'
                  )
                ORDER BY c.relname
                """,
                (self.schema,),
            ),
            [
                ("operation",),
                ("request",),
                ("run",),
                ("run_event",),
                ("stage",),
            ],
        )

    def test_arbitrary_receipt_does_not_authorize_legacy_drop(self) -> None:
        self._create_legacy_lifecycle_schema()
        self._insert_legacy_unresolved(
            request_id="request-arbitrary",
            run_id="run-arbitrary",
            operation_id="operation-arbitrary",
        )
        directory = Path(tempfile.mkdtemp(prefix="arbitrary-receipt-", dir=self.server_root.name))
        receipt = directory / "receipt.json"
        receipt.write_text('{"archive":"verified"}\n', encoding="utf-8")
        Path(str(receipt) + ".archive.tar.gz").write_bytes(b"unrelated archive\n")

        completed = self._apply_script(receipt)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("archive receipt", completed.stderr)
        self.assertEqual(
            self._query(
                """
                SELECT r.run_id, o.operation_id
                FROM run AS r
                JOIN operation AS o ON o.run_id = r.run_id
                WHERE r.run_id = %s
                """,
                ("run-arbitrary",),
            ),
            [("run-arbitrary", "operation-arbitrary")],
        )

    def test_archive_dump_must_pass_independent_restore(self) -> None:
        self._create_legacy_lifecycle_schema()
        self._insert_legacy_unresolved(
            request_id="request-restore",
            run_id="run-restore",
            operation_id="operation-restore",
        )
        directory = Path(tempfile.mkdtemp(prefix="restore-receipt-", dir=self.server_root.name))
        receipt = directory / "receipt.json"
        prepared = self._prepare_archive(receipt)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        archive = Path(str(receipt) + ".archive.tar.gz")
        module = _legacy_cutover_module()
        with tarfile.open(archive, "r:gz") as bundle:
            manifest = json.loads(bundle.extractfile("manifest.json").read())
            evidence = json.loads(bundle.extractfile("evidence.json").read())
            members = {
                name: (metadata["kind"], bundle.extractfile(name).read())
                for name, metadata in manifest["members"].items()
            }
        members["platform.dump"] = ("postgres-custom-dump", b"not-a-postgres-dump\n")
        tampered_manifest = module._manifest(members, evidence)
        tampered_archive = directory / "tampered.tar.gz"
        module._write_archive(tampered_archive, members, tampered_manifest)
        os.replace(tampered_archive, archive)
        receipt_record = json.loads(receipt.read_text(encoding="utf-8"))
        receipt_record["archive_sha256"] = module.sha256_file(archive)
        receipt_record["manifest_sha256"] = module.sha256_bytes(
            module.canonical(tampered_manifest)
        )
        receipt.write_text(
            module.canonical(receipt_record).decode("utf-8"),
            encoding="utf-8",
        )

        verified = self._prepare_archive(receipt)
        self.assertNotEqual(verified.returncode, 0)
        self.assertIn("pg_restore", verified.stderr)
        self.assertEqual(
            self._query(
                "SELECT to_regclass(%s)::text",
                (f"{self.schema}.run",),
            ),
            [("run",)],
        )

    def test_archive_receipt_without_database_locale_is_rejected(self) -> None:
        self._create_legacy_lifecycle_schema()
        self._insert_legacy_unresolved(
            request_id="request-missing-locale",
            run_id="run-missing-locale",
            operation_id="operation-missing-locale",
        )
        directory = Path(
            tempfile.mkdtemp(
                prefix="missing-locale-receipt-",
                dir=self.server_root.name,
            )
        )
        receipt = directory / "receipt.json"
        prepared = self._prepare_archive(receipt)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        receipt_record = json.loads(receipt.read_text(encoding="utf-8"))
        tampered_locale = {
            **receipt_record,
            "database_collate": "POSIX",
        }
        receipt.write_text(
            json.dumps(tampered_locale, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        locale_rejected = self._prepare_archive(receipt)
        self.assertNotEqual(locale_rejected.returncode, 0)
        self.assertIn("database locale differs", locale_rejected.stderr)

        receipt_record.pop("database_encoding")
        receipt.write_text(
            json.dumps(receipt_record, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )

        rejected = self._prepare_archive(receipt)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("invalid shape", rejected.stderr)

    def test_archive_receipt_cannot_authorize_another_database_schema(self) -> None:
        self._create_legacy_lifecycle_schema()
        self._insert_legacy_unresolved(
            request_id="request-source",
            run_id="run-source",
            operation_id="operation-source",
        )
        directory = Path(tempfile.mkdtemp(prefix="source-receipt-", dir=self.server_root.name))
        receipt = directory / "receipt.json"
        prepared = self._prepare_archive(receipt)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)

        original_schema = self.schema
        other_schema = "cutover_other_" + uuid.uuid4().hex
        with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
            cursor.execute(
                _sql(
                    "bootstrap.sql",
                    platform_schema=other_schema,
                    airflow_schema=self.airflow_schema,
                    database=self.database,
                )
            )
        self.schema = other_schema
        try:
            self._create_legacy_lifecycle_schema()
            self._insert_legacy_unresolved(
                request_id="request-other",
                run_id="run-other",
                operation_id="operation-other",
            )
            rejected = self._prepare_archive(receipt)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("another cluster, database, or schema", rejected.stderr)
            self.assertEqual(
                self._query(
                    "SELECT to_regclass(%s)::text",
                    (f"{other_schema}.run",),
                ),
                [("run",)],
            )
        finally:
            with psycopg2.connect(self.server.get_uri()) as connection, connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{other_schema}" CASCADE')
            self.schema = original_schema

    def test_archive_receipt_binds_cluster_identity_not_locator(self) -> None:
        original_server = self.server
        server_roots = tempfile.TemporaryDirectory(
            prefix="cutover-clusters-",
            dir=self.server_root.name,
        )
        servers = {}
        receipt = Path(server_roots.name) / "cluster-receipt.json"
        try:
            for label in ("source", "foreign"):
                root = Path(server_roots.name) / label
                root.mkdir()
                server = pgserver.get_server(root / "pgdata", cleanup_mode="delete")
                servers[label] = server
                with psycopg2.connect(server.get_uri()) as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT current_database()")
                    database = cursor.fetchone()[0]
                    self.assertEqual(database, "postgres")
                    cursor.execute(
                        _sql(
                            "bootstrap.sql",
                            platform_schema=self.schema,
                            airflow_schema=self.airflow_schema,
                            database=database,
                        )
                    )
                self.server = server
                self.database = database
                self._create_legacy_lifecycle_schema()
                self._insert_legacy_unresolved(
                    request_id="request-cluster",
                    run_id="run-cluster",
                    operation_id="operation-cluster",
                )
                if label == "foreign":
                    prepared = self._prepare_archive(receipt)
                    self.assertEqual(prepared.returncode, 0, prepared.stderr)

            def identity(server):
                with psycopg2.connect(server.get_uri()) as connection, connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            (SELECT system_identifier::text FROM pg_control_system()),
                            current_database(),
                            (
                                SELECT oid::text
                                FROM pg_database
                                WHERE datname = current_database()
                            ),
                            (
                                SELECT oid::text
                                FROM pg_namespace
                                WHERE nspname = %s
                            )
                        """,
                        (self.schema,),
                    )
                    return cursor.fetchone()

            source_identity = identity(servers["source"])
            foreign_identity = identity(servers["foreign"])
            self.assertEqual(source_identity[1:], foreign_identity[1:])
            self.assertNotEqual(source_identity[0], foreign_identity[0])
            receipt_record = json.loads(receipt.read_text(encoding="utf-8"))
            with tarfile.open(
                str(receipt) + ".archive.tar.gz",
                "r:gz",
            ) as bundle:
                manifest = json.loads(bundle.extractfile("manifest.json").read())
            self.assertEqual(
                receipt_record["cluster_system_identifier"],
                foreign_identity[0],
            )
            self.assertEqual(
                manifest["cluster_system_identifier"],
                foreign_identity[0],
            )

            self.server = servers["source"]
            self.database = "postgres"
            rejected = self._apply_script(receipt)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("another cluster, database, or schema", rejected.stderr)
            with self.assertRaises(psycopg2.errors.RaiseException) as raised:
                self._apply(
                    "schema.sql",
                    cluster_system_identifier=foreign_identity[0],
                )
            self.assertIn(
                "legacy_lifecycle_archive_receipt_invalid",
                str(raised.exception),
            )
            self.assertEqual(
                self._query(
                    "SELECT to_regclass(%s)::text",
                    (f"{self.schema}.run",),
                ),
                [("run",)],
            )
        finally:
            self.server = original_server
            for temp_server in servers.values():
                temp_server.cleanup()
            server_roots.cleanup()

    def test_jsonb_numeric_precision_is_lossless_in_row_evidence(self) -> None:
        first = "0.123456789012345678901"
        second = "0.123456789012345678902"
        self.assertEqual(
            json.loads('{"value":' + first + "}"),
            json.loads('{"value":' + second + "}"),
        )
        self._create_legacy_lifecycle_schema()
        self._insert_legacy_unresolved(
            request_id="request-precision",
            run_id="run-precision",
            operation_id="operation-precision",
            configuration='{"value":' + first + "}",
        )
        directory = Path(tempfile.mkdtemp(prefix="precision-receipt-", dir=self.server_root.name))
        receipt = directory / "receipt.json"
        prepared = self._prepare_archive(receipt)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        archive = Path(str(receipt) + ".archive.tar.gz")
        with tarfile.open(archive, "r:gz") as bundle:
            evidence = json.loads(bundle.extractfile("evidence.json").read())
            dump = bundle.extractfile("platform.dump").read()
            schema_sql = bundle.extractfile("schema.sql").read()
        request_rows = evidence["relations"]["request"]["rows"]
        self.assertTrue(any(first in row for row in request_rows), request_rows)

        parsed = urllib.parse.urlsplit(self.server.get_uri())
        host = (
            urllib.parse.parse_qs(parsed.query).get("host", [""])[0]
            or parsed.hostname
            or "localhost"
        )
        module = _legacy_cutover_module()
        restore_args = argparse.Namespace(
            psql=str(PG_BIN / "psql"),
            pg_dump=str(PG_BIN / "pg_dump"),
            pg_restore=str(PG_BIN / "pg_restore"),
            initdb=str(PG_BIN / "initdb"),
            pg_ctl=str(PG_BIN / "pg_ctl"),
            createdb=str(PG_BIN / "createdb"),
            database=self.database,
            platform_schema=self.schema,
            airflow_schema=self.airflow_schema,
            libpq=[
                f"h={host}",
                f"U={parsed.username or 'postgres'}",
            ],
        )
        tampered_evidence = json.loads(json.dumps(evidence))
        tampered_evidence["relations"]["request"]["rows"] = [
            row.replace(first, second)
            for row in tampered_evidence["relations"]["request"]["rows"]
        ]
        with tempfile.TemporaryDirectory(prefix="precision-dump-") as restore_directory:
            dump_path = Path(restore_directory) / "platform.dump"
            dump_path.write_bytes(dump)
            with self.assertRaises(module.ArchiveError) as raised:
                module._restore_full(
                    restore_args,
                    dump_path,
                    schema_sql,
                    tampered_evidence,
                )
        self.assertIn("restored archive differs in relations", str(raised.exception))

        self._query(
            "UPDATE request SET configuration = %s::jsonb "
            "WHERE request_id = %s RETURNING request_id",
            ('{"value":' + second + "}", "request-precision"),
        )
        rejected = self._apply_script(receipt)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("live legacy facts differ", rejected.stderr)
        self.assertEqual(
            self._query(
                "SELECT to_regclass(%s)::text",
                (f"{self.schema}.run",),
            ),
            [("run",)],
        )

    def test_non_c_database_locale_round_trip_and_c_order_rows(self) -> None:
        original_database = self.database
        original_schema = self.schema
        original_airflow_schema = self.airflow_schema
        source_database = "locale_source_" + uuid.uuid4().hex
        self.schema = "locale_platform_" + uuid.uuid4().hex
        self.airflow_schema = "locale_airflow_" + uuid.uuid4().hex
        admin = psycopg2.connect(self.server.get_uri())
        admin.autocommit = True
        try:
            with admin.cursor() as cursor:
                try:
                    cursor.execute(
                        f'CREATE DATABASE "{source_database}" TEMPLATE template0 '
                        "ENCODING 'UTF8' LC_COLLATE 'en_US.utf8' "
                        "LC_CTYPE 'en_US.utf8'"
                    )
                except psycopg2.errors.InvalidParameterValue:
                    self.skipTest("the en_US.utf8 locale is unavailable")

            self.database = source_database
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    _sql(
                        "bootstrap.sql",
                        platform_schema=self.schema,
                        airflow_schema=self.airflow_schema,
                        database=self.database,
                    )
                )
            self._create_legacy_lifecycle_schema()
            self._insert_legacy_unresolved(
                request_id="request-locale",
                run_id="run-locale",
                operation_id="operation-locale",
            )
            with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
                cursor.execute(
                    f'CREATE TABLE "{self.schema}".sort_probe '
                    "(value text PRIMARY KEY)"
                )
                cursor.execute(
                    f'INSERT INTO "{self.schema}".sort_probe (value) '
                    "VALUES ('only-row')"
                )

            source_locale = self._query(
                """
                SELECT
                    pg_encoding_to_char(encoding),
                    datlocprovider::text,
                    datcollate,
                    datctype,
                    daticulocale,
                    daticurules,
                    datcollversion,
                    pg_database_collation_actual_version(oid)
                FROM pg_database
                WHERE datname = current_database()
                """
            )[0]
            default_order = self._query(
                """
                SELECT jsonb_agg(value ORDER BY value)
                FROM (
                    VALUES ('B'), ('a'), ('ä'), ('z')
                ) AS probe(value)
                """
            )[0][0]
            self.assertNotEqual(default_order, ["B", "a", "z", "ä"])
            self.assertEqual(source_locale[1], "c")
            self.assertEqual(source_locale[6], source_locale[7])
            self.assertIsNotNone(source_locale[7])

            directory = Path(
                tempfile.mkdtemp(
                    prefix="locale-receipt-",
                    dir=self.server_root.name,
                )
            )
            single_receipt = directory / "single.json"
            single = self._prepare_archive(single_receipt)
            self.assertEqual(single.returncode, 0, single.stderr)
            receipt_record = json.loads(single_receipt.read_text(encoding="utf-8"))
            self.assertEqual(receipt_record["database_encoding"], source_locale[0])
            self.assertEqual(receipt_record["database_locale_provider"], "libc")
            self.assertEqual(receipt_record["database_collate"], source_locale[2])
            self.assertEqual(receipt_record["database_ctype"], source_locale[3])
            self.assertEqual(
                receipt_record["database_icu_locale"],
                source_locale[4],
            )
            self.assertEqual(
                receipt_record["database_icu_rules"],
                source_locale[5],
            )
            self.assertEqual(
                receipt_record["database_collation_version"],
                source_locale[6],
            )
            self.assertEqual(
                receipt_record["database_collation_actual_version"],
                source_locale[7],
            )

            with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
                cursor.execute(
                    f'INSERT INTO "{self.schema}".sort_probe (value) '
                    "VALUES ('B'), ('a'), ('ä'), ('z')"
                )
            multi_receipt = directory / "multi.json"
            multi = self._prepare_archive(multi_receipt)
            self.assertEqual(multi.returncode, 0, multi.stderr)
            with tarfile.open(
                str(multi_receipt) + ".archive.tar.gz",
                "r:gz",
            ) as bundle:
                manifest = json.loads(bundle.extractfile("manifest.json").read())
                evidence = json.loads(bundle.extractfile("evidence.json").read())
            multi_record = json.loads(multi_receipt.read_text(encoding="utf-8"))
            for key in (
                "database_encoding",
                "database_locale_provider",
                "database_collate",
                "database_ctype",
                "database_icu_locale",
                "database_icu_rules",
                "database_collation_version",
                "database_collation_actual_version",
                "server_version_num",
            ):
                self.assertEqual(manifest[key], multi_record[key])
                self.assertEqual(evidence["identity"][key], multi_record[key])
            self.assertEqual(
                evidence["relations"]["sort_probe"]["rows"],
                [
                    '{"value": "B"}',
                    '{"value": "a"}',
                    '{"value": "only-row"}',
                    '{"value": "z"}',
                    '{"value": "ä"}',
                ],
            )
        finally:
            self.database = original_database
            self.schema = original_schema
            self.airflow_schema = original_airflow_schema
            try:
                with admin.cursor() as cursor:
                    cursor.execute(f'DROP DATABASE IF EXISTS "{source_database}" WITH (FORCE)')
            finally:
                admin.close()

    def test_non_utf8_database_encoding_round_trip(self) -> None:
        original_database = self.database
        original_schema = self.schema
        original_airflow_schema = self.airflow_schema
        source_database = "encoding_source_" + uuid.uuid4().hex
        self.schema = "encoding_platform_" + uuid.uuid4().hex
        self.airflow_schema = "encoding_airflow_" + uuid.uuid4().hex
        admin = psycopg2.connect(self.server.get_uri())
        admin.autocommit = True
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    f'CREATE DATABASE "{source_database}" TEMPLATE template0 '
                    "ENCODING 'LATIN1' LC_COLLATE 'C' LC_CTYPE 'C'"
                )
            self.database = source_database
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    _sql(
                        "bootstrap.sql",
                        platform_schema=self.schema,
                        airflow_schema=self.airflow_schema,
                        database=self.database,
                    )
                )
            self._create_legacy_lifecycle_schema()
            self._insert_legacy_unresolved(
                request_id="request-encoding",
                run_id="run-encoding",
                operation_id="operation-encoding",
            )

            directory = Path(
                tempfile.mkdtemp(
                    prefix="encoding-receipt-",
                    dir=self.server_root.name,
                )
            )
            receipt = directory / "receipt.json"
            prepared = self._prepare_archive(receipt)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            receipt_record = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(receipt_record["database_encoding"], "LATIN1")
            self.assertEqual(receipt_record["database_locale_provider"], "libc")
            self.assertEqual(receipt_record["database_collate"], "C")
            self.assertEqual(receipt_record["database_ctype"], "C")
        finally:
            self.database = original_database
            self.schema = original_schema
            self.airflow_schema = original_airflow_schema
            try:
                with admin.cursor() as cursor:
                    cursor.execute(f'DROP DATABASE IF EXISTS "{source_database}" WITH (FORCE)')
            finally:
                admin.close()

    def test_unknown_or_unreproducible_database_locale_is_rejected(self) -> None:
        module = _legacy_cutover_module()
        identity = {
            "database_encoding": "UTF8",
            "database_locale_metadata_version": "postgresql_16",
            "database_locale_provider": "libc",
            "database_locale_provider_source": "pg_database.datlocprovider",
            "database_collate": "C.UTF-8",
            "database_ctype": "C.UTF-8",
            "database_icu_locale": None,
            "database_icu_rules": None,
            "database_collation_binding": "default",
            "database_collation_binding_source": "pg_database_default_collation",
            "database_collation_version": None,
            "database_collation_version_source": "pg_database.datcollversion",
            "database_collation_actual_version": None,
            "database_collation_actual_version_source": (
                "pg_database_collation_actual_version(pg_database.oid)"
            ),
            "server_version_num": "160002",
        }
        module.validate_database_locale_identity(identity)

        unknown_provider = {**identity, "database_locale_provider": "builtin"}
        with self.assertRaises(module.ArchiveError):
            module.validate_database_locale_identity(unknown_provider)

        unknown_version = {**identity, "server_version_num": "170000"}
        with self.assertRaises(module.ArchiveError):
            module.validate_database_locale_identity(unknown_version)

        missing_icu_locale = {
            **identity,
            "database_locale_provider": "icu",
            "database_collate": None,
            "database_ctype": None,
            "database_icu_locale": None,
            "database_collation_version": "153",
            "database_collation_actual_version": "153",
        }
        with self.assertRaises(module.ArchiveError):
            module.validate_database_locale_identity(missing_icu_locale)

        stale_version = {
            **identity,
            "database_collate": "en_US.utf8",
            "database_ctype": "en_US.utf8",
            "database_collation_version": "2.42",
            "database_collation_actual_version": "2.43",
        }
        with self.assertRaises(module.ArchiveError):
            module.validate_database_locale_identity(stale_version)

        unavailable_version = {
            **identity,
            "database_collate": "en_US.utf8",
            "database_ctype": "en_US.utf8",
        }
        with self.assertRaises(module.ArchiveError):
            module.validate_database_locale_identity(unavailable_version)

        icu_identity = {
            **identity,
            "database_locale_provider": "icu",
            "database_collate": None,
            "database_ctype": None,
            "database_icu_locale": "en-US",
            "database_icu_rules": "und-x-private",
            "database_collation_version": "153",
            "database_collation_actual_version": "153",
        }
        options = module.database_locale_create_options(icu_identity)
        self.assertIn("--locale-provider", options)
        self.assertIn("icu", options)
        self.assertIn("--icu-locale", options)
        self.assertIn("en-US", options)
        self.assertIn("--icu-rules", options)
        self.assertIn("und-x-private", options)
        self.assertNotIn("--no-locale", options)

        postgres14_identity = {
            **identity,
            "database_locale_metadata_version": "postgresql_14_libc",
            "database_locale_provider_source": (
                "inferred_from_postgresql_14_database_default_collation"
            ),
            "database_collate": "en_US.utf8",
            "database_ctype": "en_US.utf8",
            "database_collation_binding": "en_US",
            "database_collation_binding_source": "exact_pg_collation_definition",
            "database_collation_version": "2.43",
            "database_collation_version_source": "pg_collation.collversion",
            "database_collation_actual_version": "2.43",
            "database_collation_actual_version_source": (
                "pg_collation_actual_version(pg_collation.oid)"
            ),
            "server_version_num": "140024",
        }
        options = module.database_locale_create_options(postgres14_identity)
        self.assertIn("UTF8", options)
        self.assertIn("--lc-collate", options)
        self.assertIn("en_US.utf8", options)
        self.assertNotIn("--locale-provider", options)

    def test_schema_comparison_normalizes_only_pg_dump_restrict_tokens(self) -> None:
        module = _legacy_cutover_module()
        first = (
            b"--\n-- PostgreSQL database dump\n--\n\n"
            b"\\restrict firstToken\n"
            b"CREATE TABLE probe (value text DEFAULT 'platform.value');\n"
            b"COMMENT ON TABLE probe IS 'platform.value';\n"
            b"CREATE FUNCTION probe_fn() RETURNS void AS $fn$\n"
            b"\\restrict literalRestrict\n"
            b"\\unrestrict literalUnrestrict\n"
            b"$fn$ LANGUAGE sql;\n"
            b"-- PostgreSQL database dump complete\n--\n\n"
            b"\\unrestrict firstToken\n"
        )
        second = (
            b"--\n-- PostgreSQL database dump\n--\n\n"
            b"\\restrict secondToken\n"
            b"CREATE TABLE probe (value text DEFAULT 'platform.value');\n"
            b"COMMENT ON TABLE probe IS 'platform.value';\n"
            b"CREATE FUNCTION probe_fn() RETURNS void AS $fn$\n"
            b"\\restrict literalRestrict\n"
            b"\\unrestrict literalUnrestrict\n"
            b"$fn$ LANGUAGE sql;\n"
            b"-- PostgreSQL database dump complete\n--\n\n"
            b"\\unrestrict secondToken\n"
        )
        first_normalized = module.canonical_schema_sql(first)
        self.assertEqual(first_normalized, module.canonical_schema_sql(second))
        self.assertIn(b"\\restrict literalRestrict", first_normalized)
        self.assertIn(b"\\unrestrict literalUnrestrict", first_normalized)

        changed_literal = first.replace(b"'platform.value'", b"'value'", 1)
        self.assertNotEqual(
            first_normalized,
            module.canonical_schema_sql(changed_literal),
        )
        pseudo_only = (
            b"CREATE FUNCTION probe_fn() RETURNS void AS $fn$\n"
            b"\\restrict notMetadata\n"
            b"\\unrestrict notMetadata\n"
            b"$fn$ LANGUAGE sql;\n"
        )
        self.assertEqual(
            module.canonical_schema_sql(pseudo_only),
            pseudo_only,
        )

    def test_archived_airflow_rows_change_blocks_cutover(self) -> None:
        self._create_legacy_lifecycle_schema()
        self._create_airflow_tables()
        self._insert_legacy_unresolved(
            request_id="request-airflow-drift",
            run_id="run-airflow-drift",
            operation_id="operation-airflow-drift",
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO "{self.airflow_schema}".task_instance (
                    dag_id, task_id, run_id, map_index, state, try_number
                ) VALUES ('solidworks_to_urdf', 'capture', 'airflow-drift', 0, 'running', 1)
                """
            )
        directory = Path(
            tempfile.mkdtemp(
                prefix="airflow-drift-receipt-",
                dir=self.server_root.name,
            )
        )
        receipt = directory / "receipt.json"
        prepared = self._prepare_archive(receipt)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self._query(
            f"""
            UPDATE "{self.airflow_schema}".task_instance
            SET state = 'success'
            WHERE dag_id = 'solidworks_to_urdf'
              AND task_id = 'capture'
              AND run_id = 'airflow-drift'
              AND map_index = 0
            RETURNING task_id
            """
        )

        rejected = self._apply_script(receipt)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("live legacy facts differ", rejected.stderr)
        self.assertEqual(
            self._query(
                f'''
                SELECT state
                FROM "{self.airflow_schema}".task_instance
                WHERE dag_id = 'solidworks_to_urdf'
                  AND task_id = 'capture'
                  AND run_id = 'airflow-drift'
                  AND map_index = 0
                '''
            ),
            [("success",)],
        )
        self.assertEqual(
            self._query(
                "SELECT to_regclass(%s)::text",
                (f"{self.schema}.run",),
            ),
            [("run",)],
        )

    def test_archived_rows_change_blocks_cutover(self) -> None:
        self._create_legacy_lifecycle_schema()
        self._insert_legacy_unresolved(
            request_id="request-drift",
            run_id="run-drift",
            operation_id="operation-drift",
            display_name="Before archive",
        )
        directory = Path(tempfile.mkdtemp(prefix="drift-receipt-", dir=self.server_root.name))
        receipt = directory / "receipt.json"
        prepared = self._prepare_archive(receipt)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self._query(
            "UPDATE run SET display_name = %s WHERE run_id = %s RETURNING run_id",
            ("Changed after archive", "run-drift"),
        )

        rejected = self._apply_script(receipt)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("live legacy facts differ", rejected.stderr)
        self.assertEqual(
            self._query(
                "SELECT display_name FROM run WHERE run_id = %s",
                ("run-drift",),
            ),
            [("Changed after archive",)],
        )

    def _assert_catalog_ddl_drift_blocks_cutover(
        self,
        before_archive: str,
        after_archive: str,
    ) -> None:
        self._create_legacy_lifecycle_schema()
        self._create_airflow_tables()
        self._insert_legacy_unresolved(
            request_id="request-catalog-drift",
            run_id="run-catalog-drift",
            operation_id="operation-catalog-drift",
        )
        with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
            cursor.execute(before_archive)
        directory = Path(
            tempfile.mkdtemp(
                prefix="catalog-drift-receipt-",
                dir=self.server_root.name,
            )
        )
        receipt = directory / "receipt.json"
        prepared = self._prepare_archive(receipt)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        with tarfile.open(str(receipt) + ".archive.tar.gz", "r:gz") as bundle:
            evidence_text = bundle.extractfile("evidence.json").read().decode("utf-8")
        self.assertIn(
            f"{self.schema}.catalog-sentinel",
            evidence_text,
        )
        with self._role_connect("platform_migrate") as connection, connection.cursor() as cursor:
            cursor.execute(after_archive)

        rejected = self._apply_script(receipt)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(
            "live legacy schema or ACL SQL differs",
            rejected.stderr,
        )
        self.assertEqual(
            self._query(
                "SELECT to_regclass(%s)::text",
                (f"{self.schema}.run",),
            ),
            [("run",)],
        )

    def test_archived_default_literal_change_blocks_cutover(self) -> None:
        self._assert_catalog_ddl_drift_blocks_cutover(
            f"""
            ALTER TABLE request
            ALTER COLUMN target_hardware
            SET DEFAULT '{self.schema}.catalog-sentinel'
            """,
            """
            ALTER TABLE request
            ALTER COLUMN target_hardware
            SET DEFAULT 'catalog-sentinel'
            """,
        )

    def test_archived_comment_literal_change_blocks_cutover(self) -> None:
        self._assert_catalog_ddl_drift_blocks_cutover(
            f"COMMENT ON TABLE request IS '{self.schema}.catalog-sentinel'",
            "COMMENT ON TABLE request IS 'catalog-sentinel'",
        )

    def test_cutover_requires_one_time_database_administrator(self) -> None:
        self._create_legacy_lifecycle_schema()
        self._insert_legacy_unresolved(
            request_id="request-privilege",
            run_id="run-privilege",
            operation_id="operation-privilege",
        )
        directory = Path(tempfile.mkdtemp(prefix="privilege-receipt-", dir=self.server_root.name))
        receipt = directory / "receipt.json"
        prepared = self._prepare_archive(receipt, user="platform_migrate")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        rejected = self._apply_script(receipt, user="platform_migrate")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("one-time database administrator", rejected.stderr)
        self.assertEqual(
            self._query(
                "SELECT to_regclass(%s)::text",
                (f"{self.schema}.run",),
            ),
            [("run",)],
        )

    def test_cutover_lock_blocks_concurrent_index_acl_owner_and_view_ddl(self) -> None:
        self._create_legacy_lifecycle_schema()
        self._create_airflow_tables()
        self._insert_legacy_unresolved(
            request_id="request-lock",
            run_id="run-lock",
            operation_id="operation-lock",
        )
        before = self._query(
            """
            SELECT pg_get_userbyid(c.relowner),
                   has_table_privilege('operator_ro', c.oid, 'SELECT')
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = 'run'
            """,
            (self.schema,),
        )
        module = _legacy_cutover_module()
        statements = (
            f'UPDATE "{self.schema}".run SET state = \'running\' WHERE run_id = \'run-lock\'',
            f'CREATE TABLE "{self.schema}".concurrent_table (id integer)',
            f'CREATE INDEX run_concurrent_idx ON "{self.schema}".run (state)',
            f'GRANT SELECT ON "{self.schema}".run TO operator_ro',
            f'ALTER TABLE "{self.schema}".run OWNER TO platform_migrate',
            f'ALTER VIEW "{self.schema}".lifecycle_projection SET (security_barrier=true)',
            f"""
            INSERT INTO "{self.airflow_schema}".dag_run (
                dag_id, run_id, state
            ) VALUES ('concurrent', 'run-concurrent', 'running')
            """,
        )
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        result = context.Queue()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '30s'")
            module.lock_cutover_relations(
                cursor,
                self.schema,
                self.airflow_schema,
            )
            workers = [
                context.Process(
                    target=_attempt_legacy_ddl,
                    args=(self.server.get_uri(), statement, start, result),
                )
                for statement in statements
            ]
            for process in workers:
                self.addCleanup(
                    lambda item=process: item.terminate() if item.is_alive() else None
                )
                process.start()
            start.set()
            for process in workers:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            outcomes = [result.get(timeout=5) for _ in workers]
            self.assertEqual(
                sorted(row[0] for row in outcomes),
                [
                    "locked",
                    "locked",
                    "locked",
                    "locked",
                    "locked",
                    "locked",
                    "locked",
                ],
                outcomes,
            )
            connection.rollback()
        self.assertEqual(
            self._query(
                "SELECT to_regclass(%s)::text, to_regclass(%s)::text",
                (
                    f"{self.schema}.run_concurrent_idx",
                    f"{self.schema}.lifecycle_projection",
                ),
            ),
            [(None, "lifecycle_projection")],
        )
        self.assertEqual(
            self._query(
                """
                SELECT pg_get_userbyid(c.relowner),
                       has_table_privilege('operator_ro', c.oid, 'SELECT')
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = 'run'
                """,
                (self.schema,),
            ),
            before,
        )
        self.assertEqual(
            self._query(
                "SELECT state, display_name FROM run WHERE run_id = 'run-lock'"
            ),
            [("unresolved", None)],
        )
        definition = self._query(
            """
            SELECT pg_get_viewdef(%s::regclass, true)
            """,
            (f"{self.schema}.lifecycle_projection",),
        )[0][0]
        self.assertIn("state", definition)
        self.assertEqual(
            self._query(
                """
                SELECT c.reloptions
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = 'lifecycle_projection'
                """,
                (self.schema,),
            ),
            [(None,)],
        )

    def test_cutover_postcondition_rejects_unexpected_relation(self) -> None:
        self._install_canonical_schema()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE TABLE "{self.schema}".unexpected_table (id integer)')
        module = _legacy_cutover_module()
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
            self.assertRaises(module.ArchiveError),
        ):
            module.verify_canonical_cutover_state(cursor, self.schema)

    def test_legacy_cutover_does_not_cascade_through_an_unarchived_dependency(self) -> None:
        self._create_legacy_lifecycle_schema()
        self._insert_legacy_unresolved(
            request_id="request-dependency",
            run_id="run-dependency",
            operation_id="operation-dependency",
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("CREATE VIEW legacy_run_dependency AS SELECT run_id FROM run")
        self._create_airflow_tables()
        directory = Path(tempfile.mkdtemp(prefix="dependency-receipt-", dir=self.server_root.name))
        completed = self._apply_script(directory / "receipt.json")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("DependentObjectsStillExist", completed.stderr)
        self.assertEqual(
            self._query(
                "SELECT to_regclass(%s)::text, to_regclass(%s)::text",
                (f"{self.schema}.run", f"{self.schema}.legacy_run_dependency"),
            ),
            [("run", "legacy_run_dependency")],
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
        self._insert_run(running_run)
        freeze_command = self._insert_stage_command(running_run, "freeze")
        freeze_decision = self._insert_decision(
            running_run,
            "freeze",
            freeze_command,
            outcome="completed",
        )
        discover_command = self._insert_stage_command(
            running_run,
            "discover",
            predecessor_decision_id=freeze_decision,
        )
        self._insert_open_claim(discover_command, stage="discover")

        for run_id, fact_type in (
            ("run-effect-unknown", "dispatch_authorized"),
            ("run-effect-conflict", "conflict"),
        ):
            self._insert_run(run_id)
            freeze_command = self._insert_stage_command(run_id, "freeze")
            freeze_decision = self._insert_decision(
                run_id,
                "freeze",
                freeze_command,
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
        self.assertEqual(store.get_admitted_run("run-effect-unknown").state, "unresolved")
        self.assertEqual(store.get_admitted_run("run-effect-conflict").state, "unresolved")
        self.assertEqual(store.get_admitted_run(contradiction_run).state, "unresolved")

    def test_canonical_checkpoint_query_emits_recovery_graph(self) -> None:
        import sys

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
        import sys
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
                freeze_command_id=command.command_id,
            )
            dispatch_artifact = harness.store.seal_dispatch_conf(
                dispatch_conf,
                producer_run_id=admission.run_id,
                producer_tool_sha256=digest({"transport": "airflow-api"}),
            )
            dispatch = DispatchCommand.create(
                command_id="dispatch-" + admission.run_id,
                run_id=admission.run_id,
                freeze_command_id=command.command_id,
                idempotency_key=f"{admission.run_id}/dispatch",
                dag_id="solidworks_to_urdf",
                dag_run_id=f"portal-{admission.run_id}",
                conf_artifact=dispatch_artifact,
                expected_executor_sha256=digest({"transport": "airflow-api"}),
            )
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f'UPDATE "{self.schema}".admission SET state = \'open\' WHERE id = 1'
                )
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
                {
                    item["candidate_id"]
                    for item in recovery_root["candidates"]
                },
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
                    cursor.execute(
                        f'INSERT INTO "{target_schema}".{table} '
                        f'SELECT * FROM "{self.schema}".{table}'
                    )
                cursor.execute(
                    f'UPDATE "{target_schema}".admission SET state = \'open\' WHERE id = 1'
                )

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
        freeze_command = self._insert_stage_command("run-freeze-only", "freeze")
        self._insert_decision(
            "run-freeze-only",
            "freeze",
            freeze_command,
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
        run_rows = [row for row in projection["rows"] if row["object_kind"] == "run"]
        self.assertEqual(
            [(row["run_id"], row["state"], row["bucket"]) for row in run_rows],
            [("run-gate", "incomplete", "in_flight")],
        )
        self.assertIn(
            ("request", "request-run-gate", None),
            [
                (row["object_kind"], row["object_id"], row["run_id"])
                for row in projection["rows"]
            ],
        )


if __name__ == "__main__":
    unittest.main()
