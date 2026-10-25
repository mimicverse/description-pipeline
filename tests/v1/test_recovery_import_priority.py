"""The recovery entry must prefer its own release source over caller shadows."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECOVERY = ROOT / "deploy" / "operator" / "recovery.py"
RELEASE_SOURCE = ROOT / "src"


class RecoveryImportPriorityTests(unittest.TestCase):
    def _run(self, program: str) -> dict:
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", program],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": "",
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def test_shadow_control_and_release_source_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="recovery-import-priority-") as temporary:
            shadow = Path(temporary) / "shadow"
            package = shadow / "description_pipeline"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")

            control = self._run(
                f"""
import json
import sys

sys.path.insert(0, {str(shadow)!r})
sys.path.append({str(RELEASE_SOURCE)!r})
try:
    import description_pipeline.pipeline
except ModuleNotFoundError as error:
    print(json.dumps({{"error": error.name}}))
else:
    raise SystemExit("shadow package unexpectedly resolved")
"""
            )
            self.assertEqual(control, {"error": "description_pipeline.pipeline"})

            result = self._run(
                f"""
import json
import runpy
import sys
from pathlib import Path

sys.path.insert(0, {str(RECOVERY.parent)!r})
sys.path.insert(0, {str(shadow)!r})
sys.path.append({str(RELEASE_SOURCE)!r})
runpy.run_path({str(RECOVERY)!r}, run_name="recovery_import_priority_probe")

import description_pipeline

source = Path(description_pipeline.__file__).resolve()
release = Path({str(RELEASE_SOURCE)!r}).resolve()
if not source.is_relative_to(release):
    raise SystemExit(f"recovery imported outside its release source: {{source}}")
print(json.dumps({{"source": str(source)}}))
"""
            )
            self.assertTrue(Path(result["source"]).is_relative_to(RELEASE_SOURCE))


if __name__ == "__main__":
    unittest.main()
