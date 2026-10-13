"""Repository-boundary guard: deployment machinery is versioned, parameterized and secret-free."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "deploy/windows"


class DeployMachineryTests(unittest.TestCase):
    def test_windows_lifecycle_is_one_bound_script(self) -> None:
        # The isolated instance has exactly one lifecycle script plus the documented minimal
        # configuration example; the legacy candidate harness and the launcher are gone.
        self.assertEqual(
            {path.name for path in WINDOWS.iterdir() if path.is_file()},
            {"endpoint.example.json", "instance-lifecycle.ps1"},
        )
        script = (WINDOWS / "instance-lifecycle.ps1").read_text(encoding="utf-8")
        for marker in (
            "-Binding",
            "'stage'",
            "'promote'",
            "'rollback'",
            "'health'",
            "description.exe",
            "serve --config",
            "stage-receipt.json",
        ):
            self.assertIn(marker, script)
        for forbidden in ("description-v1-review", "GH_CONFIG", "description-v1-endpoint", "native-product"):
            self.assertNotIn(forbidden, script)

    def test_endpoint_example_is_the_documented_minimum(self) -> None:
        path = WINDOWS / "endpoint.example.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(document),
            {"schema_version", "package_root", "output_root", "state_root", "token_file", "host", "port"},
        )
        text = path.read_text(encoding="utf-8")
        for marker in (
            "gho_",
            "ghp_",
            "Bearer ",
            "BEGIN OPENSSH",
            "app_secret",
            "password",
            "description-v1-review",
            "mimicverse/description",
        ):
            self.assertNotIn(marker, text)
        self.assertIn("token_file", text)

if __name__ == "__main__":
    unittest.main()
