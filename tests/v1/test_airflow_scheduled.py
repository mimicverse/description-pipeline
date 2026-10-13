"""Opt-in real scheduler run (set RUN_SCHEDULED_AIRFLOW=1); not part of the fast suite.

The smoke uses an isolated, migrated AIRFLOW_HOME and the current Feishu auth manager on a free
loopback port, and submits the run through the portal's Airflow client with a real operator JWT,
so the pinned strict request model and the scheduler's task serialization are exercised without
touching a running deployment.  The mocked Windows delivery is the sealed control fixture: the
run must be rejected by Linux verification on the documented fixture limits and must never reach
publication, because a control fixture is not native CAD qualification.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.v1.split_capture_fixture import CONTROL_LIMITS, QUALIFICATION
from tests.v1.test_airflow_dag import migrated_airflow_home

ROOT = Path(__file__).resolve().parents[2]
#: The split transport graph the scheduler must serialize for one run.
TASK_IDS = {
    "resolve_handoff",
    "start_job",
    "fetch_capture",
    "run_generate",
    "run_verify",
    "run_publish",
}


@unittest.skipUnless(os.environ.get("RUN_SCHEDULED_AIRFLOW") == "1", "set RUN_SCHEDULED_AIRFLOW=1")
class ScheduledSmokeTests(unittest.TestCase):
    def test_scheduled_control_run_is_rejected_at_verification(self) -> None:
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
            timeout=900,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-6000:] + result.stderr[-2000:])
        report = json.loads([line for line in result.stdout.splitlines() if line.startswith("{")][-1])
        self.assertEqual(report["problems"], [])

        # Expected rejection, serialized by the real scheduler: every transport task ran through
        # the metadata database, the control fixture was stopped by Linux verification, and
        # publication never executed.
        self.assertEqual(report["dag_state"], "failed")
        tasks = report["task_states"]
        self.assertEqual(set(tasks), TASK_IDS)
        for task_id in ("resolve_handoff", "start_job", "fetch_capture", "run_generate"):
            self.assertEqual(tasks[task_id]["state"], "success", task_id)
        self.assertEqual(tasks["run_verify"]["state"], "failed")
        self.assertGreaterEqual(tasks["run_verify"]["try_number"], 1)
        # Slot release is proven by the run's own raw records: TaskReschedule rows for the native
        # wait task plus the observed up_for_reschedule state on that same task.  Counts and the
        # sensor-mode string are not evidence.
        evidence = report["reschedule_evidence"]
        self.assertIsNone(evidence["error"])
        self.assertTrue(evidence["rows"], "TaskReschedule rows for start_job must exist")
        self.assertTrue(evidence["observed"], "start_job must be observed in up_for_reschedule")
        for task_id in ("run_publish",):
            # Blocked without execution: the scheduler marked it upstream_failed (or skipped) and
            # it never entered a running attempt, so publication starting and failing early can
            # never be mistaken for a prevention proof.
            self.assertIn(tasks[task_id]["state"], {"upstream_failed", "skipped"}, task_id)
            self.assertFalse(tasks[task_id]["try_number"], task_id)
        self.assertEqual(report["publication"], {"executed": False, "pr_json": None})
        self.assertEqual(report["verify"]["state"], "failed")
        self.assertIn("URDF verification failed", report["verify"]["error"])
        # The rejection is the documented control-fixture rejection: only the release-only gates
        # may fail, everything else must pass, and the run is never native CAD qualification.
        self.assertTrue(set(report["verify"]["failed_gates"]) <= set(CONTROL_LIMITS))
        self.assertEqual(report["verify"]["unexpected_gates"], [])
        self.assertEqual(report["verify"]["missing_required_gates"], [])
        self.assertEqual(report["qualification"], QUALIFICATION)

        # The one supported transfer: exactly one full-folder staging upload, exactly one
        # generated definition post, and both sealed artifacts (raw record and native
        # capture) streamed.
        self.assertEqual(report["selection_uploads"], 1)
        self.assertEqual(len(report["definition_posts"]), 1)
        self.assertEqual(len(report["definition_posts"][0]["prepared_sha256"]), 64)
        self.assertGreater(report["definition_posts"][0]["size"], 0)
        self.assertEqual(len(report["artifact_paths"]), 2)
        self.assertTrue(any(path.endswith("/artifacts/raw-record.zip") for path in report["artifact_paths"]))
        self.assertTrue(any(path.endswith("/artifacts/native-evidence.zip") for path in report["artifact_paths"]))

        # The api-server runs the real Feishu manager with a configured smoke app, and the run was
        # submitted through the portal client, so Airflow recorded the compound audit identity.
        self.assertEqual(report["auth_health"], {"status": 200, "configured": True, "request_context": True})
        self.assertEqual(report["trigger"], "portal-airflow-client")
        from description_pipeline.orchestration.run_ownership import recorded_actor_principal

        actor = report["triggering_user_name"]
        self.assertEqual(recorded_actor_principal(actor), "cli_smoke:smoke-tenant:ou_smoke")
        self.assertEqual(json.loads(actor.split("|", 1)[1]), "smoke")


if __name__ == "__main__":
    unittest.main()
