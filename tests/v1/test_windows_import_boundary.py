"""The Windows operation entry stays outside portable engineering domains."""

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
from description_pipeline.schemas import TOOL_SCHEMA

_NATIVE_LEAVES = {
    "isolation",
    "native",
    "native_selection",
}


def leaks():
    bad = []
    for name in list(sys.modules):
        if not name.startswith("description_pipeline"):
            continue
        if name.startswith("description_pipeline.verification"):
            bad.append(name)
        elif name == "description_pipeline.solidworks":
            bad.append(name)
        elif "sources.solidworks." in name and name.rsplit(".", 1)[-1] in _NATIVE_LEAVES:
            bad.append(name)
    return sorted(bad)


after_import = leaks()
with tempfile.TemporaryDirectory(prefix="windows-operation-boundary-") as root_text:
    root = Path(root_text)
    for name in ("packages", "outputs", "state"):
        (root / name).mkdir()
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
    ownership_file = root / "cad-ownership.json"
    tool = {
        "schema_version": TOOL_SCHEMA,
        "pipeline_id": "boundary",
        "source_sha256": "ab" * 32,
    }
    with (
        patch.object(windows, "_cad_ownership_file", return_value=ownership_file),
        patch.object(windows, "_native_tool_record", return_value=tool),
    ):
        runtime = windows.build_runtime(config)
    try:
        after_build = leaks()
        states = runtime.stats()
    finally:
        runtime.close()

print("WINDOWS_LOADED=" + str("description_pipeline.orchestration.windows" in sys.modules))
print("LEAKS_IMPORT=" + ",".join(after_import))
print("LEAKS_BUILD=" + ",".join(after_build))
print("STATS=" + json.dumps(states, sort_keys=True))
print("EXECUTORS=" + ",".join(sorted(runtime.executors)))
"""


def _probe_fields(stdout: str) -> dict[str, str]:
    fields = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key] = value
    return fields


class WindowsImportBoundaryTests(unittest.TestCase):
    def test_operation_entry_stays_out_of_portable_engineering_domains(self) -> None:
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
        self.assertEqual(fields.get("LEAKS_IMPORT"), "", msg=fields.get("LEAKS_IMPORT"))
        self.assertEqual(fields.get("LEAKS_BUILD"), "", msg=fields.get("LEAKS_BUILD"))
        self.assertEqual(
            fields.get("STATS"),
            '{"active": 0, "queued": 0, "unresolved": 0}',
        )
        self.assertEqual(fields.get("EXECUTORS"), "capture,discover")


if __name__ == "__main__":
    unittest.main()
