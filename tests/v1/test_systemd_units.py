"""One static systemd unit inventory for shell and Python deployment consumers."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"
UNITS_MODULE = OPERATOR / "systemd_units.py"
SERVICES = ROOT / "deploy" / "airflow" / "services.sh"


def load_systemd_units():
    spec = importlib.util.spec_from_file_location("systemd_units_unit_test", UNITS_MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SystemdUnitInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_systemd_units()

    def test_manifest_matches_every_service_file(self) -> None:
        unit_files = {
            path.stem
            for directory in (ROOT / "deploy/airflow/systemd", ROOT / "deploy/operator/systemd")
            for path in directory.glob("*.service")
            if path.is_file()
        }
        manifest_units = self.manifest.units_for_role("all")
        self.assertEqual(len(unit_files), 8)
        self.assertEqual(len(manifest_units), len(set(manifest_units)))
        self.assertEqual(set(manifest_units), unit_files)

    def test_maintenance_v1_and_operator_roles_preserve_contracts(self) -> None:
        self.assertEqual(
            self.manifest.units_for_role("maintenance-v1"),
            (
                "description-solidworks-tunnel",
                "description-airflow-dag-processor",
                "description-airflow-scheduler",
                "description-airflow-triggerer",
                "description-airflow-api-server",
                "description-operator-proxy",
                "description-portal",
            ),
        )
        self.assertEqual(
            self.manifest.units_for_role("operator"),
            ("description-portal", "description-operator-proxy"),
        )

    def test_shell_and_python_read_the_same_roles(self) -> None:
        for role in ("all", "services", "airflow", "executor", "database", "operator", "maintenance-v1"):
            result = subprocess.run(
                ["bash", str(SERVICES), "units", role],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, role)
            self.assertEqual(tuple(result.stdout.splitlines()), self.manifest.units_for_role(role), role)

    def test_triggerer_is_the_managed_deferred_effect_process(self) -> None:
        unit = (ROOT / "deploy/airflow/systemd/description-airflow-triggerer.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("ExecStart=@VENV@/bin/airflow triggerer", unit)
        self.assertIn("Environment=AIRFLOW_HOME=@AIRFLOW_HOME@", unit)
        self.assertIn("description-airflow-triggerer", self.manifest.units_for_role("airflow"))

    def test_unknown_missing_and_empty_roles_fail_closed(self) -> None:
        with self.assertRaisesRegex(self.manifest.UnitManifestError, "unknown unit role"):
            self.manifest.units_for_role("unknown")
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.tsv"
            with self.assertRaisesRegex(self.manifest.UnitManifestError, "missing or a link"):
                self.manifest.load_units(missing)
            no_operator = Path(temporary) / "units.tsv"
            no_operator.write_text("description-postgres\tdatabase,services\t-\n", encoding="utf-8")
            with self.assertRaisesRegex(self.manifest.UnitManifestError, "no members: operator"):
                self.manifest.units_for_role("operator", no_operator)
            deploy = Path(temporary) / "deploy"
            (deploy / "airflow").mkdir(parents=True)
            copied_services = deploy / "airflow" / "services.sh"
            copied_services.write_bytes(SERVICES.read_bytes())
            (deploy / "systemd-units.tsv").write_text(
                "description-postgres\tdatabase,services\t-\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["bash", str(copied_services), "units", "operator"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            (deploy / "systemd-units.tsv").unlink()
            result = subprocess.run(
                ["bash", str(copied_services), "units", "all"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
