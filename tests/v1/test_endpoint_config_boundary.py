"""The endpoint configuration boundary, pinned on the release's own canonical reader.

The Windows lifecycle's pre-stage strict scan (deploy/windows/instance-lifecycle.ps1) mirrors
this reader's duplicate/unknown-key rules without re-implementing its value checks; these cases
pin the reader side so the two layers stay honest about who refuses what.  The file is
cross-platform on purpose: the Windows PowerShell 5.1 CI job runs it with the bound CPython.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError
from description_pipeline.orchestration.windows import read_config

TOKEN = "t" * 40


class EndpointConfigBoundaryTests(unittest.TestCase):
    def _valid(self, root: Path) -> dict:
        for name in ("package", "output", "state"):
            (root / name).mkdir()
        token = root / "state" / "token"
        token.write_text(TOKEN, encoding="utf-8")
        return {
            "schema_version": "solidworks-to-urdf.endpoint/v1",
            "package_root": str(root / "package"),
            "output_root": str(root / "output"),
            "state_root": str(root / "state"),
            "token_file": str(token),
            "host": "127.0.0.1",
            "port": 8765,
        }

    def _write(self, root: Path, text: str) -> Path:
        path = root / "endpoint.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_valid_configuration_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._write(root, json.dumps(self._valid(root)))
            loaded = read_config(config)
            self.assertEqual(loaded["host"], "127.0.0.1")
            self.assertEqual(loaded["port"], 8765)
            self.assertNotIn("token_file", loaded)
            self.assertEqual(loaded["token"], TOKEN)

    def test_duplicate_keys_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = self._valid(root)
            text = json.dumps(valid)[:-1] + ', "port": 8766}'
            config = self._write(root, text)
            with self.assertRaisesRegex(PipelineError, "Duplicate input key"):
                read_config(config)

    def test_unknown_keys_are_refused_even_the_removed_upgrade_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = self._valid(root)
            valid["targets"] = {}
            config = self._write(root, json.dumps(valid))
            with self.assertRaisesRegex(PipelineError, "Unknown endpoint configuration key"):
                read_config(config)

    def test_invalid_values_are_refused(self) -> None:
        def mutate(document: dict) -> dict:
            return json.loads(json.dumps(document))

        cases = (
            (lambda doc: doc.__setitem__("schema_version", "solidworks-to-urdf.endpoint/v2"),
             "Unknown endpoint configuration"),
            (lambda doc: doc.__setitem__("port", 0), "Invalid endpoint port"),
            (lambda doc: doc.__setitem__("port", True), "Invalid endpoint port"),
            (lambda doc: doc.__setitem__("host", "0.0.0.0"), "loopback"),
            (lambda doc: doc.__setitem__("host", 5), "loopback"),
            (lambda doc: doc.__setitem__("token_file", str(Path(doc["state_root"]) / "missing")),
             "Missing endpoint token file"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = self._valid(root)
            for index, (mutate_case, message) in enumerate(cases):
                with self.subTest(case=index):
                    document = mutate(valid)
                    mutate_case(document)
                    config = self._write(root, json.dumps(document))
                    with self.assertRaisesRegex(PipelineError, message):
                        read_config(config)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = self._valid(root)
            Path(valid["token_file"]).write_text("short", encoding="utf-8")
            config = self._write(root, json.dumps(valid))
            with self.assertRaisesRegex(PipelineError, "at least 32"):
                read_config(config)

    def test_non_finite_constants_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = self._valid(root)
            text = json.dumps(valid).replace('"port": 8765', '"port": NaN')
            config = self._write(root, text)
            with self.assertRaisesRegex(PipelineError, "non-finite constant"):
                read_config(config)


if __name__ == "__main__":
    unittest.main()
