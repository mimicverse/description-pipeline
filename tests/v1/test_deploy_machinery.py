"""Repository-boundary guard: deployment machinery is versioned, parameterized and secret-free."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
WINDOWS = ROOT / "deploy/windows"

#: Schema ids outside the ``solidworks-to-urdf.*`` family are retired (XC-01); the two
#: documented exceptions below intentionally keep their historical ids.
RETIRED_SCHEMA_ID = re.compile(r"\b(?:description-pipeline|description)\.[A-Za-z][A-Za-z0-9._-]*/v[0-9]+\b")
DOCUMENTED_ID_EXCEPTIONS = ("native-discovery/v2", "swbridge.material-assignment/v1")


def endpoint_template_targets_violations(root: Path) -> list[str]:
    """Deploy templates that declare the endpoint schema but still ask for a targets key."""

    violations = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".template"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "solidworks-to-urdf.endpoint" in text and '"targets"' in text:
            violations.append(path.relative_to(root).as_posix())
    return violations


class DeployMachineryTests(unittest.TestCase):
    def run_script(self, path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for name in ("AIRFLOW_VENV", "AIRFLOW_HOME", "PIPELINE_WHEEL"):
            env.pop(name, None)
        return subprocess.run(
            ["bash", str(path), *arguments],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_services_cli_requires_one_explicit_live_verb(self) -> None:
        services = DEPLOY / "airflow/services.sh"
        for arguments in ((), ("install",), ("status",), ("render", "extra"), ("units", "unknown")):
            result = self.run_script(services, *arguments)
            self.assertEqual(result.returncode, 2, arguments)
            self.assertIn("usage:", result.stderr)
            self.assertIn("render|start|stop|stop-executors", result.stderr)

        text = services.read_text(encoding="utf-8")
        self.assertNotIn("install)", text)
        self.assertNotIn("status)", text)
        self.assertNotIn('ACTION="${1:-install}"', text)

    def test_systemd_unit_manifest_has_one_role_authority(self) -> None:
        services = DEPLOY / "airflow/services.sh"
        expected = {
            "all": (
                "description-postgres",
                "description-solidworks-tunnel",
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-triggerer",
                "description-airflow-api-server",
                "description-portal",
                "description-operator-proxy",
            ),
            "services": (
                "description-postgres",
                "description-solidworks-tunnel",
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-triggerer",
                "description-airflow-api-server",
            ),
            "airflow": (
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-triggerer",
                "description-airflow-api-server",
            ),
            "executor": (
                "description-solidworks-tunnel",
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-triggerer",
                "description-airflow-api-server",
            ),
            "database": ("description-postgres",),
            "operator": ("description-portal", "description-operator-proxy"),
            "maintenance-v1": (
                "description-solidworks-tunnel",
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-triggerer",
                "description-airflow-api-server",
                "description-operator-proxy",
                "description-portal",
            ),
        }
        for role, units in expected.items():
            result = self.run_script(services, "units", role)
            self.assertEqual(result.returncode, 0, role)
            self.assertEqual(tuple(result.stdout.splitlines()), units, role)
        unit_files = {
            path.relative_to(DEPLOY).as_posix()
            for path in DEPLOY.rglob("*.service")
            if path.is_file()
        }
        self.assertEqual(
            unit_files,
            {f"airflow/systemd/{unit}.service" for unit in expected["all"] if unit not in expected["operator"]}
            | {f"operator/systemd/{unit}.service" for unit in expected["operator"]},
        )

        control = (DEPLOY / "operator/operatorctl.sh").read_text(encoding="utf-8")
        self.assertIn('bash "$AIRFLOW_HERE/services.sh" units "$1"', control)
        for variable, role in {
            "UNITS": "all",
            "AIRFLOW_UNITS": "airflow",
            "EXECUTOR_UNITS": "executor",
            "DATABASE_UNITS": "database",
            "OPERATOR_UNITS": "operator",
        }.items():
            self.assertIn(f"load_unit_role {variable} {role}", control)
        self.assertNotIn("UNITS=(description-postgres", control)
        health = (DEPLOY / "operator/health.sh").read_text(encoding="utf-8")
        self.assertIn('services.sh" units all', health)
        self.assertIn('services.sh" units operator', health)

    def test_airflow_installer_refuses_arguments_before_migration(self) -> None:
        installer = DEPLOY / "airflow/install.sh"
        for arguments in (("unknown",), ("--stage",), ("--stage", "extra"), ("--staging",)):
            result = self.run_script(installer, *arguments)
            self.assertEqual(result.returncode, 2, arguments)
            self.assertIn("usage:", result.stderr)
            self.assertNotIn("AIRFLOW_VENV", result.stderr)

        for arguments in ((),):
            result = self.run_script(installer, *arguments)
            self.assertEqual(result.returncode, 1, arguments)
            self.assertIn("AIRFLOW_VENV", result.stderr)
            self.assertNotIn("usage:", result.stderr)

    def test_endpoint_templates_never_ask_for_targets(self) -> None:
        # A8 (6fffdfc): the endpoint configuration refuses a targets key, so no deploy template,
        # example or config may ship one again; the gate itself must discriminate.
        self.assertEqual(endpoint_template_targets_violations(ROOT / "deploy"), [])
        with tempfile.TemporaryDirectory() as temporary:
            sample = Path(temporary) / "sample.json"
            sample.write_text('{"schema_version": "solidworks-to-urdf.endpoint/v1", "targets": {}}', encoding="utf-8")
            self.assertEqual(endpoint_template_targets_violations(Path(temporary)), ["sample.json"])

    def test_windows_deployment_does_not_embed_site_identity(self) -> None:
        for path in WINDOWS.glob("*.ps1"):
            text = path.read_text(encoding="utf-8")
            for forbidden in ("description-v1-review", "GH_CONFIG", "description-v1-endpoint", "native-product"):
                with self.subTest(file=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, text)

    def test_endpoint_example_is_the_documented_minimum(self) -> None:
        path = WINDOWS / "endpoint.example.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(document),
            {"schema_version", "package_root", "output_root", "state_root", "token_file", "host", "port"},
        )
        self.assertNotIn("targets", document)
        text = path.read_text(encoding="utf-8")
        for marker in (
            "gho_",
            "ghp_",
            "Bearer ",
            "BEGIN OPENSSH",
            "app_secret",
            "password",
            "description-v1-review",
            "mimicverse/description",
        ):
            self.assertNotIn(marker, text)
        self.assertIn("token_file", text)

    def test_retired_schema_namespace_is_absent_from_product_code(self) -> None:
        """No ``description-pipeline.*``/``description.*`` schema ids in product code.

        Tests are out of scope on purpose: the negative-control tests carry retired
        ids as deliberate rejection fixtures.  Deploy-history review material is not
        product code either.
        """

        found = []
        for relative in ("src", "deploy", "tools"):
            for path in sorted((ROOT / relative).rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                for match in RETIRED_SCHEMA_ID.finditer(path.read_text(encoding="utf-8", errors="ignore")):
                    found.append(f"{path.relative_to(ROOT).as_posix()}: {match.group(0)}")
        self.assertEqual([], found)
        for exception in DOCUMENTED_ID_EXCEPTIONS:
            self.assertNotRegex(exception, RETIRED_SCHEMA_ID)


if __name__ == "__main__":
    unittest.main()
