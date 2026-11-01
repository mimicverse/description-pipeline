"""Exercise downloaded setup parsing and the dependency-free CLI boundary."""

from pathlib import Path
import json
import runpy
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/pico_pipeline_link.py"
LINK = runpy.run_path(str(SCRIPT))


class PicoLinkSetupTests(unittest.TestCase):
    def test_setup_uses_one_origin_and_session_without_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.json"
            setup = {
                "schema_version": "solidworks-to-urdf.pico-link/v1",
                "origin": "wss://portal.example.test",
                "session_id": "a" * 32,
            }
            path.write_text(json.dumps(setup))
            self.assertEqual((setup["origin"], setup["session_id"]), LINK["read_setup"](path))
            for update in (
                {"pairing_code": "fixture-only"}, {"schema_version": "unrecognized"}, {"session_id": "../other"},
            ):
                with self.subTest(update=update):
                    path.write_text(json.dumps({**setup, **update}))
                    with self.assertRaises(ValueError):
                        LINK["read_setup"](path)
            path.write_bytes(b" " * 4097)
            with self.assertRaisesRegex(ValueError, "size limit"):
                LINK["read_setup"](path)

    def test_endpoints_reject_missing_hosts_ports_and_embedded_credentials(self):
        for value in ("wss://", "wss://[broken", "wss://host:0", "wss://host:bad", "ws://remote.test", "wss://user:fixture-only@host"):
            with self.subTest(origin=value), self.assertRaises(ValueError):
                LINK["validate_endpoint"](value)
        for value in ("tcp://localhost", "tcp://remote.test:5560", "tcp://127.0.0.1:0"):
            with self.subTest(gateway=value), self.assertRaises(ValueError):
                LINK["validate_endpoint"](value, local=True)
        self.assertEqual("ws://[::1]:8080", LINK["validate_endpoint"]("ws://[::1]:8080/"))

    def test_cli_help_and_bad_input_do_not_need_transport_packages(self):
        for args, expected, message in (
            (["--help"], 0, "downloaded"),
            (["--origin", "wss://user:fixture-only@host", "--session", "a" * 32], 1, "must not contain credentials"),
            (["--origin", "wss://portal.test"], 2, "Select a downloaded setup file"),
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(SCRIPT), *args], capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(expected, result.returncode, result.stderr)
                self.assertIn(message, result.stdout + result.stderr)
                self.assertNotIn("fixture-only", result.stdout + result.stderr)
