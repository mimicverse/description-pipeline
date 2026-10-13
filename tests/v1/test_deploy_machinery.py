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
#: Schema ids outside the ``solidworks-to-urdf.*`` family are retired; the two
#: documented exceptions below intentionally keep their historical ids (XC-01).
RETIRED_SCHEMA_ID = re.compile(r"\b(?:description-pipeline|description)\.[A-Za-z][A-Za-z0-9._-]*/v[0-9]+\b")
DOCUMENTED_ID_EXCEPTIONS = ("native-discovery/v1", "swbridge.material-assignment/v1")


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

    def test_retired_schema_namespace_is_absent_from_product_code(self) -> None:
        """No ``description-pipeline.*``/``description.*`` schema ids in product code.

        Tests are out of scope on purpose: the negative-control tests carry retired
        ids as deliberate rejection fixtures.  Deploy-history review material is not
        product code either.
        """

        found = []
        for relative in ("src", "deploy", "tools"):
            for path in sorted((ROOT / relative).rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                for match in RETIRED_SCHEMA_ID.finditer(path.read_text(encoding="utf-8", errors="ignore")):
                    found.append(f"{path.relative_to(ROOT).as_posix()}: {match.group(0)}")
        self.assertEqual([], found)
        for exception in DOCUMENTED_ID_EXCEPTIONS:
            self.assertNotRegex(exception, RETIRED_SCHEMA_ID)

    def test_candidate_receipts_use_the_current_namespace(self) -> None:
        for name, expected in (
            ("stop-production-for-promotion.ps1", "solidworks-to-urdf.production-stop/v1"),
            ("stop-candidate-endpoint-safe.ps1", "solidworks-to-urdf.candidate-safe-stop/v1"),
        ):
            text = (CANDIDATE / name).read_text(encoding="utf-8")
            self.assertIn(expected, text)
            self.assertNotIn("description-pipeline.", text)

    def test_candidate_stage_binding_is_single_sourced(self) -> None:
        # The register action passes -Stage <base>; the child must operate on exactly that
        # directory instead of re-deriving it (a non-default -Stamp diverged once and the
        # first live exercise exited before it could write its journal).
        register = (CANDIDATE / "register-candidate.ps1").read_text(encoding="utf-8")
        child = (CANDIDATE / "run-candidate-endpoint.ps1").read_text(encoding="utf-8")
        self.assertIn('-Stage "', register)
        self.assertIn("$B = $Stage", child)

    def test_candidate_paths_use_interpolating_quotes(self) -> None:
        # Single-quoted PowerShell strings do not expand $variables; a parameterized path
        # in that form silently reads or writes the wrong file (caught live once).
        shape = re.compile(r"'\\[^'\n]*\$[A-Za-z_]")
        for path in CANDIDATE.iterdir():
            if path.suffix == ".ps1":
                self.assertNotRegex(path.read_text(encoding="utf-8", errors="replace"), shape, path.name)

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
