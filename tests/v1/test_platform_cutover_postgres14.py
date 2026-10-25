"""Real PostgreSQL 14 proof for the product legacy cutover."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

try:
    import psycopg2
except ImportError:
    psycopg2 = None

ROOT = Path(__file__).resolve().parents[2]
PLATFORM_DIR = ROOT / "deploy" / "platform"
PG14_BIN = Path(os.environ["PG14_BIN"]) if os.environ.get("PG14_BIN") else None
PG14_LD_LIBRARY_PATH = os.environ.get("PG14_LD_LIBRARY_PATH")
PG14_AVAILABLE = (
    psycopg2 is not None
    and PG14_BIN is not None
    and (PG14_BIN / "initdb").is_file()
    and (PG14_BIN / "postgres").is_file()
)


def _render_sql(name: str, platform_schema: str, airflow_schema: str, database: str) -> str:
    text = (PLATFORM_DIR / name).read_text(encoding="utf-8")
    return (
        text.replace(':"platform_schema"', f'"{platform_schema}"')
        .replace(':"airflow_schema"', f'"{airflow_schema}"')
        .replace(':"platform_database"', f'"{database}"')
    )


def _run(command: list[str], environment: dict[str, str]) -> bytes:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        env=environment,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed: {command[0]}: "
            f"{completed.stderr.decode('utf-8', errors='replace')}"
        )
    return completed.stdout


@unittest.skipUnless(PG14_AVAILABLE, "PG14_BIN is not configured")
class Postgres14CutoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.TemporaryDirectory(prefix="cutover-pg14-")
        cls.base = Path(cls.root.name)
        cls.data = cls.base / "data"
        cls.sockets = cls.base / "socket"
        cls.sockets.mkdir()
        cls.environment = {
            **os.environ,
            "PLATFORM_ARCHIVE_PYTHON": sys.executable,
        }
        if PG14_LD_LIBRARY_PATH:
            cls.environment["LD_LIBRARY_PATH"] = PG14_LD_LIBRARY_PATH

        version = _run(
            [str(PG14_BIN / "postgres"), "--version"],
            cls.environment,
        ).decode("utf-8")
        if "PostgreSQL) 14." not in version:
            cls.root.cleanup()
            raise AssertionError(f"PG14_BIN is not PostgreSQL 14: {version.strip()}")

        socket_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        socket_probe.bind(("127.0.0.1", 0))
        cls.port = socket_probe.getsockname()[1]
        socket_probe.close()
        _run(
            [
                str(PG14_BIN / "initdb"),
                "-D",
                str(cls.data),
                "--auth=trust",
                "--encoding=UTF8",
                "--username=postgres",
            ],
            cls.environment,
        )
        _run(
            [
                str(PG14_BIN / "pg_ctl"),
                "-D",
                str(cls.data),
                "-l",
                str(cls.base / "postgres.log"),
                "-o",
                (
                    f"-F -k {cls.sockets} -p {cls.port} "
                    "-c listen_addresses=''"
                ),
                "-w",
                "start",
            ],
            cls.environment,
        )
        cls.started = True

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "started", False):
            subprocess.run(
                [
                    str(PG14_BIN / "pg_ctl"),
                    "-D",
                    str(cls.data),
                    "-m",
                    "immediate",
                    "-w",
                    "stop",
                ],
                check=False,
                capture_output=True,
                env=cls.environment,
            )
        cls.root.cleanup()

    def _connection(self, database: str, *, user: str = "postgres"):
        return psycopg2.connect(
            host=str(self.sockets),
            port=self.port,
            dbname=database,
            user=user,
        )

    def _create_database(self, name: str, collate: str) -> None:
        connection = self._connection("postgres")
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f'CREATE DATABASE "{name}" TEMPLATE template0 '
                    f"ENCODING 'UTF8' LC_COLLATE '{collate}' LC_CTYPE '{collate}'"
                )
        finally:
            connection.close()

    def _source_collation_version(self, database: str, collate: str) -> str | None:
        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    c.collversion,
                    pg_collation_actual_version(c.oid)
                FROM pg_database AS d
                JOIN pg_collation AS c
                  ON c.collprovider = 'c'
                 AND c.collcollate = d.datcollate
                 AND c.collctype = d.datctype
                WHERE d.datname = current_database()
                  AND c.collversion IS NOT DISTINCT FROM
                      pg_collation_actual_version(c.oid)
                ORDER BY c.collname, c.oid
                LIMIT 1
                """
            )
            exact = cursor.fetchone()
            if exact is not None:
                recorded_version, actual_version = exact
                self.assertEqual(recorded_version, actual_version)
                return actual_version

            self.assertIn(collate, {"C", "C.UTF-8", "C.utf8", "POSIX"})
            cursor.execute(
                """
                SELECT collversion, pg_collation_actual_version(oid)
                FROM pg_collation
                WHERE oid = 100
                """
            )
            recorded_version, actual_version = cursor.fetchone()
            self.assertIsNone(recorded_version)
            self.assertIsNone(actual_version)
            return None

    def _prepare_source(
        self,
        database: str,
        platform_schema: str,
        airflow_schema: str,
    ) -> None:
        self._prepare_airflow(database, platform_schema, airflow_schema)
        with self._connection(
            database,
            user="platform_migrate",
        ) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE "{platform_schema}".request (
                    request_id text PRIMARY KEY,
                    principal text NOT NULL,
                    target_hardware text NOT NULL,
                    main_assembly text,
                    configuration jsonb,
                    baseline jsonb,
                    sealed_at timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE "{platform_schema}".run (
                    run_id text PRIMARY KEY,
                    request_id text NOT NULL,
                    parent_run_id text,
                    from_stage text,
                    state text NOT NULL,
                    tool_identity jsonb,
                    display_name text,
                    hidden_at timestamptz,
                    hidden_reason text,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                );
                CREATE TABLE "{platform_schema}".stage (
                    run_id text NOT NULL,
                    stage text NOT NULL,
                    state text NOT NULL,
                    receipt jsonb,
                    checkpoint_uri text,
                    reused_from_run text,
                    original_executed_at timestamptz,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (run_id, stage)
                );
                CREATE TABLE "{platform_schema}".operation (
                    operation_id text PRIMARY KEY,
                    run_id text NOT NULL,
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
                CREATE TABLE "{platform_schema}".run_event (
                    run_id text NOT NULL,
                    seq bigint NOT NULL CHECK (seq > 0),
                    at timestamptz NOT NULL DEFAULT now(),
                    kind text NOT NULL,
                    stage text,
                    operation_id text,
                    payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                    PRIMARY KEY (run_id, seq)
                );
                CREATE VIEW "{platform_schema}".lifecycle_projection AS
                    SELECT run_id, state FROM "{platform_schema}".run
                """
            )

    def _prepare_airflow(
        self,
        database: str,
        platform_schema: str,
        airflow_schema: str,
    ) -> None:
        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                _render_sql(
                    "bootstrap.sql",
                    platform_schema,
                    airflow_schema,
                    database,
                )
            )
            cursor.execute(
                f"""
                CREATE TABLE "{airflow_schema}".task_instance (
                    dag_id text,
                    task_id text,
                    run_id text,
                    map_index integer,
                    state text,
                    try_number integer,
                    start_date timestamptz,
                    end_date timestamptz
                );
                CREATE TABLE "{airflow_schema}".dag_run (
                    dag_id text,
                    run_id text,
                    state text,
                    start_date timestamptz,
                    end_date timestamptz
                )
                """
            )
            cursor.execute(
                _render_sql(
                    "grants-airflow.sql",
                    platform_schema,
                    airflow_schema,
                    database,
                )
            )

    def _cutover(
        self,
        database: str,
        platform_schema: str,
        airflow_schema: str,
    ) -> dict:
        lock_dir = self.base / f"maintenance-{uuid.uuid4().hex}"
        lock_dir.mkdir()
        lock_path = lock_dir / "maintenance.lock"
        owner = uuid.uuid4().hex
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
        descriptor = os.open(lock_path, os.O_RDWR)
        receipt = self.base / f"{database}-cutover.json"
        environment = {
            **self.environment,
            "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
            "OPERATORCTL_MAINTENANCE_LOCK_FD": str(descriptor),
            "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
        }
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            completed = subprocess.run(
                [
                    "bash",
                    str(PLATFORM_DIR / "apply.sh"),
                    "--psql",
                    str(PG14_BIN / "psql"),
                    "--database",
                    database,
                    "--host",
                    str(self.sockets),
                    "--port",
                    str(self.port),
                    "--user",
                    "postgres",
                    "--platform-schema",
                    platform_schema,
                    "--airflow-schema",
                    airflow_schema,
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
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(receipt.read_text(encoding="utf-8"))

    def _apply_without_receipt(
        self,
        database: str,
        platform_schema: str,
        airflow_schema: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                str(PLATFORM_DIR / "apply.sh"),
                "--psql",
                str(PG14_BIN / "psql"),
                "--database",
                database,
                "--host",
                str(self.sockets),
                "--port",
                str(self.port),
                "--user",
                "platform_migrate",
                "--platform-schema",
                platform_schema,
                "--airflow-schema",
                airflow_schema,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )

    def _guard_with_wrong_archive_schema(
        self,
        database: str,
        platform_schema: str,
        airflow_schema: str,
        decoy_schema: str,
    ) -> subprocess.CompletedProcess[str]:
        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (SELECT oid::text FROM pg_database WHERE datname = current_database()),
                    (SELECT system_identifier::text FROM pg_control_system()),
                    (SELECT oid::text FROM pg_namespace WHERE nspname = %s)
                """,
                (decoy_schema,),
            )
            database_oid, system_identifier, schema_oid = cursor.fetchone()
        digest = "e" * 64
        settings = (
            "SELECT "
            f"set_config('description.legacy_receipt_sha256', '{digest}', false), "
            f"set_config('description.legacy_archive_sha256', '{digest}', false), "
            f"set_config('description.legacy_evidence_sha256', '{digest}', false), "
            "set_config("
            "'description.legacy_archive_cluster_system_identifier', "
            f"'{system_identifier}', false), "
            f"set_config('description.legacy_archive_database', '{database}', false), "
            "set_config("
            "'description.legacy_archive_database_oid', "
            f"'{database_oid}', false), "
            "set_config("
            "'description.legacy_archive_platform_schema', "
            f"'{decoy_schema}', false), "
            "set_config("
            "'description.legacy_archive_platform_schema_oid', "
            f"'{schema_oid}', false), "
            "set_config("
            "'description.legacy_archive_migration_role', "
            "'postgres', false)"
        )
        return subprocess.run(
            [
                str(PG14_BIN / "psql"),
                "-h",
                str(self.sockets),
                "-p",
                str(self.port),
                "-U",
                "postgres",
                "-d",
                database,
                "-X",
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"platform_schema={platform_schema}",
                "-v",
                f"airflow_schema={airflow_schema}",
                "-v",
                f"platform_database={database}",
                "-c",
                settings,
                "--single-transaction",
                "-f",
                str(PLATFORM_DIR / "schema.sql"),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )

    def test_real_postgres14_product_locales_complete_cutover(self) -> None:
        cases = (
            ("pg14_c", "C.UTF-8", "pg14_default_catalog_collation"),
            (
                "pg14_en_us",
                "en_US.utf8",
                "exact_pg_collation_definition",
            ),
        )
        for (
            database,
            collate,
            expected_binding_source,
        ) in cases:
            with self.subTest(database=database):
                platform_schema = "platform_" + uuid.uuid4().hex
                airflow_schema = "airflow_" + uuid.uuid4().hex
                self._create_database(database, collate)
                expected_version = self._source_collation_version(
                    database,
                    collate,
                )
                if expected_binding_source == "exact_pg_collation_definition":
                    self.assertIsNotNone(expected_version)
                else:
                    self.assertIsNone(expected_version)
                self._prepare_source(database, platform_schema, airflow_schema)
                receipt = self._cutover(
                    database,
                    platform_schema,
                    airflow_schema,
                )

                self.assertEqual(
                    receipt["database_locale_metadata_version"],
                    "postgresql_14_libc",
                )
                self.assertEqual(
                    receipt["database_locale_provider_source"],
                    "inferred_from_postgresql_14_database_default_collation",
                )
                self.assertEqual(
                    receipt["database_collation_binding_source"],
                    expected_binding_source,
                )
                self.assertEqual(
                    receipt["database_collation_actual_version"],
                    expected_version,
                )
                with self._connection(database) as connection, connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT to_regclass('{platform_schema}.run_admission')"
                    )
                    self.assertIsNotNone(cursor.fetchone()[0])
                    cursor.execute(
                        """
                        SELECT c.relname
                        FROM pg_catalog.pg_class AS c
                        JOIN pg_catalog.pg_namespace AS n
                          ON n.oid = c.relnamespace
                        WHERE n.nspname = %s AND c.relname = 'run'
                        """,
                        (platform_schema,),
                    )
                    self.assertEqual(cursor.fetchall(), [])

    def test_pg14_fresh_apply_without_receipt_is_idempotent(self) -> None:
        database = "pg14_fresh"
        platform_schema = "platform_" + uuid.uuid4().hex
        airflow_schema = "airflow_" + uuid.uuid4().hex
        self._create_database(database, "C.UTF-8")
        self._prepare_airflow(database, platform_schema, airflow_schema)

        first = self._apply_without_receipt(
            database,
            platform_schema,
            airflow_schema,
        )
        second = self._apply_without_receipt(
            database,
            platform_schema,
            airflow_schema,
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"SELECT to_regclass('{platform_schema}.run_admission')"
            )
            self.assertIsNotNone(cursor.fetchone()[0])

    def test_pg14_wrong_archive_schema_preserves_legacy_tables(self) -> None:
        database = "pg14_guard"
        platform_schema = "platform_" + uuid.uuid4().hex
        airflow_schema = "airflow_" + uuid.uuid4().hex
        decoy_schema = "decoy_" + uuid.uuid4().hex
        self._create_database(database, "C.UTF-8")
        self._prepare_source(database, platform_schema, airflow_schema)
        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                f'CREATE SCHEMA "{decoy_schema}" AUTHORIZATION platform_migrate'
            )

        rejected = self._guard_with_wrong_archive_schema(
            database,
            platform_schema,
            airflow_schema,
            decoy_schema,
        )

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(
            "legacy_lifecycle_archive_receipt_invalid",
            rejected.stderr,
        )
        with self._connection(database) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.relname
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = %s
                  AND c.relname IN (
                      'request', 'run', 'stage', 'operation', 'run_event'
                  )
                ORDER BY c.relname
                """,
                (platform_schema,),
            )
            self.assertEqual(
                cursor.fetchall(),
                [
                    ("operation",),
                    ("request",),
                    ("run",),
                    ("run_event",),
                    ("stage",),
                ],
            )
