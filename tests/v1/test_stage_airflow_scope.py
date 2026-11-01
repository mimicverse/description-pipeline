"""Actual isolated Airflow scheduling honors every single-stage execution range."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(
    all(importlib.util.find_spec(name) is not None for name in ("airflow", "pgserver", "psycopg2")),
    "The isolated Airflow and PostgreSQL dependencies are required",
)
class StageScopedAirflowTests(unittest.TestCase):
    def test_real_dag_runs_each_stage_independently_and_continues_new_results(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="mjcf-airflow-scope-") as directory:
            completed = subprocess.run(
                [sys.executable, "-B", "-m", "tests.v1.airflow_scope_probe", directory],
                cwd=root, env={"PATH": os.environ["PATH"], "PYTHONPATH": str(root / "src") + os.pathsep + str(root)},
                capture_output=True, text=True, timeout=600,
            )
            self.assertEqual(0, completed.returncode, completed.stdout[-8000:] + completed.stderr[-8000:])
            proofs = json.loads((Path(directory) / "AIRFLOW-SCOPE-RESULTS.json").read_text())
            stages = ("freeze", "discover", "capture", "generate", "verify", "mjcf", "publish")
            self.assertEqual([[stage] for stage in stages] + [["mjcf", "publish"]],
                             [proof["execution_scope"] for proof in proofs])
            for proof in proofs:
                with self.subTest(scope=proof["execution_scope"]):
                    self.assertEqual("success", proof["state"])
                    self.assertEqual(proof["execution_scope"], proof["stage_decisions"])
                    self.assertEqual({stage: "success" if stage in proof["execution_scope"] else "skipped"
                                      for stage in stages}, proof["tasks"])
                    self.assertFalse(proof["cad_environment_present"])


if __name__ == "__main__":
    unittest.main()
