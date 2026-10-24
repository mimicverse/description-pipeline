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
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

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

# Runtime path: configuration intake, job admission, plan and snapshot must
# also stay inside the capture closure (import-time cleanliness alone is not
# enough).
with tempfile.TemporaryDirectory(prefix="windows-runtime-") as root_text:
    root = Path(root_text)
    (root / "packages").mkdir()
    token = root / "token"
    token.write_text("t" * 64, encoding="utf-8")
    config_path = root / "endpoint.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "solidworks-to-urdf.endpoint/v1",
                "package_root": str(root / "packages"),
                "output_root": str(root / "outputs"),
                "state_root": str(root / "state"),
                "token_file": str(token),
            }
        ),
        encoding="utf-8",
    )
    config = windows.read_config(config_path)
    files = {"cad/robot.SLDASM": "ab" * 32}
    runtime_run = "00000000-0000-4000-8000-000000000001"
    from tests.v1.freeze_event_support import freeze_request_fields
    request = {
        **freeze_request_fields(),
        "run_id": runtime_run,
        "upload_files": files,
        "upload_sha256": windows.digest(files),
        "main_assembly": "cad/robot.SLDASM",
    }
    with patch.object(windows.Jobs, "_tool_identity", return_value={"source_sha256": "cd" * 32}):
        jobs = windows.Jobs(config)
        jobs.create(request)
        jobs.snapshot(runtime_run)
        jobs.plan(runtime_run)
        jobs.close()
after_runtime = leaks()

print("WINDOWS_LOADED=" + str("description_pipeline.orchestration.windows" in sys.modules))
print("LEAKS_IMPORT=" + ",".join(after_import))
print("LEAKS_CONSTRUCT=" + ",".join(after_construct))
print("LEAKS_RUNTIME=" + ",".join(after_runtime))
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
        self.assertEqual(
            fields.get("LEAKS_RUNTIME"), "", msg=f"runtime path leaked: {fields.get('LEAKS_RUNTIME')}"
        )


if __name__ == "__main__":
    unittest.main()
