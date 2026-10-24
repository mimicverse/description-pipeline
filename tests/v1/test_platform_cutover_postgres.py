"""Real PostgreSQL proof for the canonical schema cutover."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.io import digest

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "platform"


def _sql(name: str, *, platform_schema: str, airflow_schema: str, database: str) -> str:
    text = (PLATFORM_DIR / name).read_text(encoding="utf-8")
    return (
        text.replace(':"platform_schema"', f'"{platform_schema}"')
        .replace(':"airflow_schema"', f'"{airflow_schema}"')
        .replace(':"platform_database"', f'"{database}"')
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
            options=f"-c search_path={self.schema},public",
        )

    def _apply(self, *names: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
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

    def test_cutover_archives_display_drops_legacy_tables_and_builds_canonical_view(self) -> None:
        run_id = "run-cutover"
        request_id = "request-cutover"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE run_admission (
                    run_id text PRIMARY KEY,
                    request_id text NOT NULL UNIQUE,
                    parent_run_id text REFERENCES run_admission (run_id),
                    from_stage text,
                    request jsonb NOT NULL,
                    request_digest char(64) NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                "INSERT INTO run_admission "
                "(run_id, request_id, request, request_digest) VALUES (%s, %s, %s::jsonb, %s)",
                (run_id, request_id, '{"request":"cutover"}', "a" * 64),
            )
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
            cursor.execute(
                "INSERT INTO run (run_id, display_name, updated_at) VALUES (%s, %s, now())",
                (run_id, "Archived display"),
            )

        self._apply("schema.sql")
        with self._connect() as connection, connection.cursor() as cursor:
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
        self._apply("views.sql", "roles.sql")

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
        self.assertIn(("run", "running", "in_flight"), rows)
        self.assertIn(("stage", "running", "in_flight"), rows)
        self.assertIn(("scheduling", "running", "in_flight"), rows)
        self.assertIn(("dag_run", "running", "in_flight"), rows)
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
            [(True, False)],
        )


if __name__ == "__main__":
    unittest.main()
