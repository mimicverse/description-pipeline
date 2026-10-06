"""Opt-in real scheduler run (set RUN_SCHEDULED_AIRFLOW=1); not part of the fast suite."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("RUN_SCHEDULED_AIRFLOW") == "1", "set RUN_SCHEDULED_AIRFLOW=1")
class ScheduledSmokeTests(unittest.TestCase):
    def test_scheduled_mock_run_succeeds(self) -> None:
        venv = Path(os.environ["AIRFLOW_VENV"])
        home = Path(os.environ["AIRFLOW_HOME"])
        result = subprocess.run(
            [
                str(venv / "bin/python"),
                str(ROOT / "deploy/airflow/scripts/scheduled_smoke.py"),
                "--root",
                str(ROOT),
                "--venv",
                str(venv),
                "--airflow-home",
                str(home),
            ],
            capture_output=True,
            text=True,
            timeout=420,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-500:])
        self.assertIn('"ok": true', result.stdout)


if __name__ == "__main__":
    unittest.main()
