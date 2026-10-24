"""Platform schema parameterization: one variable, role search_path, no hidden qualifier.

The deployment keeps the production defaults (`platform` / `airflow`); an isolated environment
(CI) binds its own `platform_ci_<run>` / `airflow_ci_<run>` pair through apply.sh.  The runtime
SQL is schema-free by construction: store/service/read roles carry the schema in their
search_path, so an explicit qualifier in Python or SQL would silently defeat the switch.
"""

from __future__ import annotations

import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPLY = ROOT / "deploy/platform/apply.sh"
PLATFORM_DIR = ROOT / "deploy/platform"
PYTHON_SQL_OWNERS = (
    ROOT / "src/description_pipeline/platform/store.py",
    ROOT / "src/description_pipeline/platform/facts.py",
    ROOT / "src/description_pipeline/orchestration/lifecycle_state.py",
    ROOT / "src/description_pipeline/pipeline/cancellation.py",
)


def fake_psql(log: Path) -> Path:
    script = log.parent / "fake-psql"
    script.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "{log}"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def run_apply(log: Path, *arguments: str) -> None:
    script = fake_psql(log)
    subprocess.run(
        ["bash", str(APPLY), "--psql", str(script), "--database", "ci_db", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


class ApplySchemaVariableTests(unittest.TestCase):
    def test_default_schema_names_keep_production_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="platform-schema-") as temporary:
            log = Path(temporary) / "psql.log"
            run_apply(log)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(calls.count("platform_schema=platform"), 3)
            self.assertEqual(calls.count("airflow_schema=airflow"), 3)
            self.assertEqual(calls.count("platform_database=ci_db"), 3)
            self.assertEqual(
                [line for line in calls if line.endswith((".sql",))],
                [
                    str(PLATFORM_DIR / "schema.sql"),
                    str(PLATFORM_DIR / "views.sql"),
                    str(PLATFORM_DIR / "roles.sql"),
                ],
            )

    def test_ci_pair_reaches_every_psql_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="platform-schema-") as temporary:
            log = Path(temporary) / "psql.log"
            run_apply(log, "--platform-schema", "platform_ci_run7", "--airflow-schema", "airflow_ci_run7")
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(calls.count("platform_schema=platform_ci_run7"), 3)
            self.assertEqual(calls.count("airflow_schema=airflow_ci_run7"), 3)
            self.assertNotIn("platform_schema=platform", calls)

    def test_bootstrap_is_one_run_carrying_both_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="platform-schema-") as temporary:
            log = Path(temporary) / "psql.log"
            run_apply(
                log, "--bootstrap", "--platform-schema", "platform_ci_run7", "--airflow-schema", "airflow_ci_run7"
            )
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(calls.count("platform_schema=platform_ci_run7"), 1)
            self.assertEqual(calls.count("airflow_schema=airflow_ci_run7"), 1)
            self.assertEqual([line for line in calls if line.endswith(".sql")], [str(PLATFORM_DIR / "bootstrap.sql")])

    def test_airflow_grants_mode_runs_one_parameterized_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="platform-schema-") as temporary:
            log = Path(temporary) / "psql.log"
            run_apply(
                log, "--airflow-grants", "--platform-schema", "platform_ci_run7", "--airflow-schema", "airflow_ci_run7"
            )
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(calls.count("platform_schema=platform_ci_run7"), 1)
            self.assertEqual(calls.count("airflow_schema=airflow_ci_run7"), 1)
            self.assertEqual(
                [line for line in calls if line.endswith(".sql")], [str(PLATFORM_DIR / "grants-airflow.sql")]
            )


class SchemaQualifierTests(unittest.TestCase):
    def test_bootstrap_parameterizes_the_schema_and_the_read_role(self) -> None:
        bootstrap = (PLATFORM_DIR / "bootstrap.sql").read_text(encoding="utf-8")
        self.assertIn('CREATE SCHEMA IF NOT EXISTS :"platform_schema"', bootstrap)
        self.assertIn('ALTER SCHEMA :"platform_schema" OWNER TO platform_migrate;', bootstrap)
        self.assertIn(
            'ALTER ROLE operator_ro IN DATABASE :"platform_database" SET search_path = :"platform_schema", pg_catalog;',
            bootstrap,
        )

    def test_migration_sql_uses_the_variable_not_a_bare_platform_qualifier(self) -> None:
        for name in ("schema.sql", "views.sql", "roles.sql"):
            text = (PLATFORM_DIR / name).read_text(encoding="utf-8")
            self.assertIn(':"platform_schema".', text, name)
            self.assertIsNone(re.search(r"(?<![_a-z])platform\.[a-z_]", text), name)

    def test_no_sql_file_carries_a_bare_schema_identifier(self) -> None:
        # A standalone `platform`/`airflow` token in SQL code (SCHEMA platform, "airflow", ...)
        # would bind a default schema again; comments may still describe the families in prose.
        files = ("bootstrap.sql", "schema.sql", "views.sql", "roles.sql", "grants-airflow.sql")
        for name in files:
            text = (PLATFORM_DIR / name).read_text(encoding="utf-8")
            code = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
            for token in ("platform", "airflow"):
                pattern = re.compile(rf"(?<![_a-zA-Z]){token}(?![_a-zA-Z])")
                self.assertIsNone(pattern.search(code), f"{name}: {token}")

    def test_the_airflow_grants_file_is_schema_variable_aware(self) -> None:
        text = (PLATFORM_DIR / "grants-airflow.sql").read_text(encoding="utf-8")
        self.assertIn('GRANT SELECT ON :"airflow_schema".task_instance TO platform_migrate;', text)
        self.assertIn(
            "GRANT SELECT (dag_id, run_id, task_id, map_index, state, try_number)",
            text,
            "the platform-service reconcile reads the terminal attempt number",
        )

    def test_lifecycle_view_keeps_only_real_airflow_scheduling_evidence(self) -> None:
        text = (PLATFORM_DIR / "views.sql").read_text(encoding="utf-8")
        for token in (
            ':"platform_schema".command',
            ':"platform_schema".execution_claim',
            ':"airflow_schema".task_instance',
            ':"airflow_schema".dag_run',
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)
        for token in (
            ':"platform_schema".run_admission',
            ':"platform_schema".stage_decision',
            ':"platform_schema".effect_intent',
            ':"platform_schema".effect_fact',
        ):
            with self.subTest(absent=token):
                self.assertNotIn(token, text)
        self.assertIn("task.dag_id = claim.dag_id", text)
        self.assertIn("task.task_id = claim.task_id", text)
        self.assertIn("task.run_id = claim.dag_run_id", text)
        self.assertIn("task.map_index = claim.map_index", text)

    def test_operatorctl_uses_the_single_airflow_grants_owner(self) -> None:
        text = (ROOT / "deploy/operator/operatorctl.sh").read_text(encoding="utf-8")
        self.assertIn("--airflow-grants", text)
        self.assertNotIn("GRANT USAGE ON SCHEMA airflow TO airflow_runtime", text)
        self.assertNotIn("GRANT USAGE ON SCHEMA ${AIRFLOW_SCHEMA:-airflow} TO platform_migrate", text)

    def test_runtime_sql_stays_schema_free_for_the_role_search_path(self) -> None:
        pattern = re.compile(r"\b(FROM|INTO|UPDATE|WHERE|JOIN) platform\.")
        for path in PYTHON_SQL_OWNERS:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), str(path))


class CanonicalFactSchemaTests(unittest.TestCase):
    def test_legacy_business_tables_are_absent(self) -> None:
        text = (PLATFORM_DIR / "schema.sql").read_text(encoding="utf-8")
        for table in ("request", "run", "stage", "operation", "run_event"):
            with self.subTest(table=table):
                self.assertNotRegex(
                    text,
                    rf'CREATE TABLE IF NOT EXISTS :"platform_schema"\.{table}\b',
                )
                self.assertIn(f'DROP TABLE IF EXISTS :"platform_schema".{table}', text)
        self.assertIn('CREATE TABLE IF NOT EXISTS :"platform_schema".admission', text)

    def test_six_stage_decisions_are_unique_and_never_store_unresolved(self) -> None:
        text = (PLATFORM_DIR / "schema.sql").read_text(encoding="utf-8")
        decision = text.split('CREATE TABLE IF NOT EXISTS :"platform_schema".stage_decision', 1)[1]
        decision = decision.split(");", 1)[0]
        self.assertIn("UNIQUE (run_id, stage)", decision)
        self.assertIn("command_id text NOT NULL UNIQUE", decision)
        self.assertIn("outcome IN ('completed', 'failed', 'cancelled')", decision)
        self.assertNotIn("unresolved", decision)

    def test_claims_are_fenced_and_effect_facts_are_ordered(self) -> None:
        text = (PLATFORM_DIR / "schema.sql").read_text(encoding="utf-8")
        self.assertIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS execution_claim_one_open_command",
            text,
        )
        self.assertIn("UNIQUE (command_id, fence)", text)
        self.assertIn("executor_sha256 char(64) NOT NULL", text)
        self.assertIn("CHECK (owner_sha256 <> executor_sha256)", text)
        self.assertIn("UNIQUE (effect_id, seq)", text)
        self.assertIn("state_class IN ('known', 'absent', 'unobservable')", text)
        self.assertIn("metadata_artifact_id text REFERENCES", text)
        candidate = text.split('CREATE TABLE IF NOT EXISTS :"platform_schema".stage_candidate', 1)[1]
        candidate = candidate.split(");", 1)[0]
        self.assertIn("command_id text NOT NULL UNIQUE", candidate)
        self.assertIn("output_artifact_id text NOT NULL", candidate)
        self.assertIn("result_record_artifact_id text NOT NULL UNIQUE", candidate)
        self.assertIn("output_artifact_id <> result_record_artifact_id", candidate)
        decision = text.split('CREATE TABLE IF NOT EXISTS :"platform_schema".stage_decision', 1)[1]
        decision = decision.split(");", 1)[0]
        self.assertIn("failure_envelope jsonb", decision)
        self.assertIn("failure_digest char(64)", decision)

    def test_technical_dispatch_is_separate_from_stage_commands_and_unique_per_run(self) -> None:
        text = (PLATFORM_DIR / "schema.sql").read_text(encoding="utf-8")
        command = text.split('CREATE TABLE IF NOT EXISTS :"platform_schema".command', 1)[1]
        command = command.split(");", 1)[0]
        self.assertIn("kind IN ('stage', 'dispatch', 'cancel', 'reconcile')", command)
        self.assertIn("CHECK (kind <> 'stage' OR stage IS NOT NULL)", command)
        self.assertIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS command_dispatch_once",
            text,
        )
        self.assertIn("WHERE kind = 'dispatch';", text)
        self.assertIn(
            "CREATE UNIQUE INDEX IF NOT EXISTS command_cancel_target_once",
            text,
        )
        self.assertIn("WHERE kind = 'cancel';", text)

    def test_artifact_producer_is_descriptive_and_has_no_local_run_fk(self) -> None:
        text = (PLATFORM_DIR / "schema.sql").read_text(encoding="utf-8")
        artifact = text.split('CREATE TABLE IF NOT EXISTS :"platform_schema".artifact', 1)[1]
        artifact = artifact.split(");", 1)[0]
        self.assertIn("producer_run_id text,", artifact)
        self.assertNotIn("producer_run_id text REFERENCES", artifact)

    def test_immutable_facts_have_no_service_update_grant(self) -> None:
        text = (PLATFORM_DIR / "roles.sql").read_text(encoding="utf-8")
        immutable = re.search(
            r"GRANT SELECT, INSERT ON\s+"
            r":\"platform_schema\"\.run_admission.*?"
            r"TO platform_service;",
            text,
            re.S,
        )
        self.assertIsNotNone(immutable)
        self.assertNotIn("UPDATE", immutable.group(0))
        self.assertRegex(
            text,
            r'GRANT SELECT, INSERT, UPDATE ON\s+[^;]*execution_claim',
        )
        for table in ("request", "run", "stage", "operation", "run_event"):
            self.assertNotRegex(
                text,
                rf":\"platform_schema\"\.{table}\b",
            )


if __name__ == "__main__":
    unittest.main()
