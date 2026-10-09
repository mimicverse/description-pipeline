"""Opt-in real scheduler run (set RUN_SCHEDULED_AIRFLOW=1); not part of the fast suite.

The smoke uses an isolated, migrated AIRFLOW_HOME and the current Feishu auth manager on a free
loopback port, and submits the run through the portal's Airflow client with a real operator JWT,
so the pinned strict request model is exercised and nothing touches a running deployment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.v1.test_airflow_dag import migrated_airflow_home

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("RUN_SCHEDULED_AIRFLOW") == "1", "set RUN_SCHEDULED_AIRFLOW=1")
class ScheduledSmokeTests(unittest.TestCase):
    def test_scheduled_mock_run_succeeds(self) -> None:
        venv = Path(os.environ.get("AIRFLOW_VENV", sys.prefix))
        home = migrated_airflow_home(venv)
        env = dict(os.environ, AIRFLOW_HOME=str(home))
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
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-500:])
        self.assertIn('"ok": true', result.stdout)
        report = json.loads([line for line in result.stdout.splitlines() if line.startswith("{")][-1])
        # The api-server runs the real Feishu manager with a configured smoke app, and the run was
        # submitted through the portal client, so Airflow recorded the compound audit identity.
        self.assertEqual(report["auth_health"], {"status": 200, "configured": True})
        self.assertEqual(report["trigger"], "portal-airflow-client")
        self.assertEqual(report["triggering_user_name"], "cli_smoke:smoke-tenant:ou_smoke")


if __name__ == "__main__":
    unittest.main()
