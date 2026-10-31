"""Actual isolated Airflow scheduling uses only the admitted MJCF execution range."""

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
class MJCFScopedAirflowTests(unittest.TestCase):
    def test_real_dag_skips_upstream_and_uses_default_portable_composition(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="mjcf-airflow-scope-") as directory:
            completed = subprocess.run(
                [sys.executable, "-B", "-m", "tests.v1.airflow_scope_probe", directory],
                cwd=root, env={"PATH": os.environ["PATH"], "PYTHONPATH": str(root / "src") + os.pathsep + str(root)},
                capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(0, completed.returncode, completed.stdout[-8000:] + completed.stderr[-8000:])
            proof = json.loads((Path(directory) / "R4-AIRFLOW-RESULT.json").read_text())
            self.assertEqual("success", proof["state"])
            self.assertEqual(["mjcf", "publish"], proof["execution_scope"])
            self.assertEqual(["mjcf", "publish"], proof["stage_decisions"])
            self.assertEqual(dict.fromkeys(("freeze", "discover", "capture", "generate", "verify"), "skipped"),
                             {stage: state for stage, state in proof["tasks"].items() if state == "skipped"})
            self.assertFalse(proof["cad_environment_present"])


if __name__ == "__main__":
    unittest.main()
