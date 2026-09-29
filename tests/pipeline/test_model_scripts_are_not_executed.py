"""The pipeline reads a model; it never runs programs the model ships.

The engineering standard promises that a model branch cannot obtain tool execution rights, and
``SECURITY.md`` treats an escape from that promise as a vulnerability.  This plants executable-looking
files inside an allowed model directory and requires the whole author flow to finish without ever
creating the marker they would leave behind.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from description_pipeline.build import assess, build, freeze, lock_toolchain

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "demo-arm"
IGNORED = shutil.ignore_patterns("build", ".venv", "__pycache__")
MARKER = "model-script-ran"

PLANTS = {
    "docs/hooks/build.sh": "#!/bin/sh\n: > model-script-ran\n",
    "docs/hooks/check.py": "from pathlib import Path\nPath('model-script-ran').write_text('ran')\n",
    "docs/hooks/freeze.cmd": "@echo off\r\necho ran > model-script-ran\r\n",
    "docs/hooks/Makefile": "all:\n\t: > model-script-ran\n",
}


class ModelScriptIsolationTests(unittest.TestCase):
    def test_planted_scripts_are_never_executed(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "demo-arm"
            shutil.copytree(EXAMPLE, workspace, ignore=IGNORED)
            lock_toolchain(workspace)
            freeze(workspace)
            for relative, body in PLANTS.items():
                path = workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8", newline="\n")
                if relative.endswith(".sh"):
                    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            report = build(workspace, "kinematics")
            self.assertTrue(report["passed"], report["blockers"] + [report.get("diagnostic_path", "")])
            check = assess(workspace, "kinematics")
            self.assertTrue(check["passed"], check["blockers"])

            marker = workspace / MARKER
            self.assertFalse(marker.exists(), f"a model-provided script was executed ({os.fspath(marker)})")
            # The files stay ordinary evidence: they are read, never handed to an interpreter.
            self.assertTrue((workspace / "docs/hooks/build.sh").is_file())

    @unittest.skipIf(os.name == "nt", "needs a POSIX shell to prove the probe is sensitive")
    def test_the_probe_detects_execution_when_it_happens(self):
        """Falsification: the marker really does appear when the planted script is run."""

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "demo-arm"
            shutil.copytree(EXAMPLE, workspace, ignore=IGNORED)
            for relative, body in PLANTS.items():
                path = workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8", newline="\n")
            subprocess.run(["sh", "docs/hooks/build.sh"], cwd=workspace, check=True)
            self.assertTrue((workspace / MARKER).is_file())


if __name__ == "__main__":
    unittest.main()
