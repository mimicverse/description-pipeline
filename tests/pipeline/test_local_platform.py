import copy
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LocalPlatformTests(unittest.TestCase):
    def test_central_environment_accepts_only_pinned_supported_hosts(self):
        resolve = runpy.run_path(str(Path(__file__).resolve().parents[2] / ".github/scripts/resolve_model_tool.py"))[
            "environment"
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            lock: dict = {
                "python": "3.12.14",
                "platform": {"system": "Linux", "machine": "x86_64", "implementation": "CPython"},
            }
            path = root / "config/toolchain.lock.json"
            for system, machine, runner in (("Linux", "x86_64", "ubuntu-24.04"), ("Windows", "AMD64", "windows-2022")):
                with self.subTest(system=system):
                    lock["platform"].update(system=system, machine=machine)
                    path.write_text(json.dumps(lock))
                    result = resolve(lock)
                    self.assertEqual(result["runner"], runner)
                    self.assertEqual(result["python"], "3.12.14")
                    self.assertTrue((Path(__file__).resolve().parents[2] / result["requirements"]).is_file())
            for field, value in (
                ("system", "self-hosted"),
                ("system", "Linux\nrunner=self-hosted"),
                ("machine", []),
                ("machine", "ARM64"),
                ("implementation", "PyPy"),
            ):
                bad = copy.deepcopy(lock)
                bad["platform"][field] = value
                path.write_text(json.dumps(bad))
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    resolve(bad)
            for version in ("3.12", "3.13.1", "3.12.14\nrunner=self-hosted", 31214):
                bad = {**lock, "python": version}
                path.write_text(json.dumps(bad))
                with self.subTest(version=version), self.assertRaises(ValueError):
                    resolve(bad)

    def test_cli_roundtrips_utf8_with_an_ascii_process_locale(self):
        code = """
from unittest.mock import patch
from description_pipeline.cli import main
with patch('description_pipeline.cli.update', side_effect=lambda root, profile, message, **kw: {'message': message}):
    raise SystemExit(main(['model', 'update', '--message-file', '-']))
"""
        message = "Update café / 关节 / 🤖\nsecond line"
        result = subprocess.run(
            [sys.executable, "-c", code],
            input=message.encode("utf-8"),
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(json.loads(result.stdout)["message"], message)
