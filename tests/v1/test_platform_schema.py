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
    ROOT / "src/description_pipeline/orchestration/lifecycle_state.py",
)


def fake_psql(log: Path) -> Path:
    script = log.parent / "fake-psql"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{log}"\n',
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

    def test_runtime_sql_stays_schema_free_for_the_role_search_path(self) -> None:
        pattern = re.compile(r"\b(FROM|INTO|UPDATE|WHERE|JOIN) platform\.")
        for path in PYTHON_SQL_OWNERS:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), str(path))


if __name__ == "__main__":
    unittest.main()
