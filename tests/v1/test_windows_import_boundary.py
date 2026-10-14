"""Import-boundary negative control for the Windows capture closure (PQ-06 / slice 3).

Importing - and constructing - the Windows endpoint must stay inside the
capture closure.  The generation driver (``solidworks.run``), the stage driver
(``steps``) and everything in the verification domain are Linux concerns; if
any of them becomes reachable from this path, the fresh-interpreter probe
records the leak and this test fails.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = r"""
import sys
import tempfile
from pathlib import Path

import description_pipeline.orchestration.windows as windows

_GENERATION_LEAVES = {
    "scene",
    "freeze",
    "native",
    "discovery",
    "native_selection",
    "isolation",
    "protocol",
    "evidence",
}


def leaks():
    bad = []
    for name in list(sys.modules):
        if not name.startswith("description_pipeline"):
            continue
        if name.startswith("description_pipeline.verification"):
            bad.append(name)
        elif name.startswith("description_pipeline.steps"):
            bad.append(name)
        elif name == "description_pipeline.solidworks":
            bad.append(name)
        elif "sources.solidworks." in name and name.rsplit(".", 1)[-1] in _GENERATION_LEAVES:
            bad.append(name)
    return sorted(bad)


after_import = leaks()
with tempfile.TemporaryDirectory(prefix="windows-closure-") as state_root:
    windows.Jobs({"state_root": Path(state_root)})
after_construct = leaks()

print("WINDOWS_LOADED=" + str("description_pipeline.orchestration.windows" in sys.modules))
print("LEAKS_IMPORT=" + ",".join(after_import))
print("LEAKS_CONSTRUCT=" + ",".join(after_construct))
"""


def _probe_fields(stdout):
    fields = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key] = value
    return fields


class WindowsImportBoundaryTests(unittest.TestCase):
    def test_windows_endpoint_closure_stays_out_of_generation_and_verification(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        completed = subprocess.run(
            [sys.executable, "-B", "-c", _PROBE],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        fields = _probe_fields(completed.stdout)
        self.assertEqual(fields.get("WINDOWS_LOADED"), "True", msg=completed.stdout)
        self.assertEqual(fields.get("LEAKS_IMPORT"), "", msg=f"import leaked: {fields.get('LEAKS_IMPORT')}")
        self.assertEqual(
            fields.get("LEAKS_CONSTRUCT"), "", msg=f"JobStore construction leaked: {fields.get('LEAKS_CONSTRUCT')}"
        )


if __name__ == "__main__":
    unittest.main()
