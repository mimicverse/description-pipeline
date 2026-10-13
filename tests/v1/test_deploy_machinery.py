"""Repository-boundary guard: deployment machinery is versioned, parameterized and secret-free."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REHEARSAL = ROOT / "deploy/operator/rehearsal"
CANDIDATE = ROOT / "deploy/windows/candidate"
SCRIPTS = ("prepare-venv.sh", "stage-and-validate.sh", "switch.sh", "quiesce-admission.sh")
GENERATION = re.compile(r"native(?:[0-9]{1,2}|-product[0-9]{1,2})")


class DeployMachineryTests(unittest.TestCase):
    def test_rehearsal_scripts_present_strict_and_parameterized(self) -> None:
        for name in SCRIPTS:
            text = (REHEARSAL / name).read_text(encoding="utf-8")
            self.assertIn("set -euo pipefail", text, name)
            self.assertNotRegex(text, GENERATION, name)
        switch = (REHEARSAL / "switch.sh").read_text(encoding="utf-8")
        self.assertIn("LIVE_TAG", switch)
        self.assertIn("LIVE_RELEASE", switch)
        self.assertIn("zero active Airflow", switch)

    def test_candidate_harness_present_and_generation_free(self) -> None:
        expected = {
            "install-candidate.py",
            "register-candidate.ps1",
            "run-candidate-endpoint.ps1",
            "stop-candidate-endpoint-safe.ps1",
            "stop-production-for-promotion.ps1",
            "promote-endpoint.ps1",
            "retain-production-state.py",
        }
        files = [path for path in CANDIDATE.iterdir() if path.is_file()]
        self.assertEqual(expected, {path.name for path in files})
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotRegex(text, GENERATION, path.name)

    def test_candidate_stage_binding_is_single_sourced(self) -> None:
        # The register action passes -Stage <base>; the child must operate on exactly that
        # directory instead of re-deriving it (a non-default -Stamp diverged once and the
        # first live exercise exited before it could write its journal).
        register = (CANDIDATE / "register-candidate.ps1").read_text(encoding="utf-8")
        child = (CANDIDATE / "run-candidate-endpoint.ps1").read_text(encoding="utf-8")
        self.assertIn('-Stage "', register)
        self.assertIn("$B = $Stage", child)

    def test_endpoint_example_carries_no_secret_material(self) -> None:
        text = (ROOT / "deploy/windows/endpoint.example.json").read_text(encoding="utf-8")
        for marker in ("gho_", "ghp_", "Bearer ", "BEGIN OPENSSH", "app_secret", "password"):
            self.assertNotIn(marker, text)
        self.assertIn("token_file", text)

    @unittest.skipUnless(shutil.which("bash"), "bash is unavailable")
    def test_shell_scripts_parse(self) -> None:
        for name in SCRIPTS:
            result = subprocess.run(
                ["bash", "-n", str(REHEARSAL / name)], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_acceptance_checker_is_generation_free_and_argument_driven(self) -> None:
        text = (ROOT / "tools/acceptance/m3_acceptance.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"portal-\d{8}T\d{6}-[0-9a-f]{8}")
        self.assertIn("sys.argv[1]", text)
