"""Repository-boundary guard: deployment machinery is versioned, parameterized and secret-free."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "deploy/windows"

#: Schema ids outside the ``solidworks-to-urdf.*`` family are retired (XC-01); the two
#: documented exceptions below intentionally keep their historical ids.
RETIRED_SCHEMA_ID = re.compile(r"\b(?:description-pipeline|description)\.[A-Za-z][A-Za-z0-9._-]*/v[0-9]+\b")
DOCUMENTED_ID_EXCEPTIONS = ("native-discovery/v1", "swbridge.material-assignment/v1")


def endpoint_template_targets_violations(root: Path) -> list[str]:
    """Deploy templates that declare the endpoint schema but still ask for a targets key."""

    violations = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".template"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "solidworks-to-urdf.endpoint" in text and '"targets"' in text:
            violations.append(path.relative_to(root).as_posix())
    return violations


class DeployMachineryTests(unittest.TestCase):
    def test_endpoint_templates_never_ask_for_targets(self) -> None:
        # A8 (6fffdfc): the endpoint configuration refuses a targets key, so no deploy template,
        # example or config may ship one again; the gate itself must discriminate.
        self.assertEqual(endpoint_template_targets_violations(ROOT / "deploy"), [])
        with tempfile.TemporaryDirectory() as temporary:
            sample = Path(temporary) / "sample.json"
            sample.write_text('{"schema_version": "solidworks-to-urdf.endpoint/v1", "targets": {}}', encoding="utf-8")
            self.assertEqual(endpoint_template_targets_violations(Path(temporary)), ["sample.json"])

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
            "'status'",
            "Write-Utf8NoBom",
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
        self.assertNotIn("targets", document)
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


if __name__ == "__main__":
    unittest.main()
