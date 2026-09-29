"""Drive every CLI command against a live fake-backend service."""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest

from tools.solidworks_export import cli
from tools.solidworks_export.backends import FakeBackend
from tools.solidworks_export.server import create_server

from .helpers import make_config_dict


class CliEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swbridge-cli-")
        self.state = os.path.join(self.tmp, "state")
        self.server = create_server(FakeBackend(), self.state, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address[:2]
        if isinstance(host, bytes):
            host = host.decode()
        self.endpoint = f"http://{host}:{port}"
        self.config_path = os.path.join(self.tmp, "export_config.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(make_config_dict(), handle)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--endpoint", self.endpoint, *args])
        payload = out.getvalue().strip() or err.getvalue().strip()
        return code, (json.loads(payload) if payload else {})

    def test_health_commands(self):
        code, payload = self.run_cli("ping")
        self.assertEqual(code, 0)
        self.assertTrue(payload["health"]["ok"])
        code, payload = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertEqual(payload["health"]["backend"], "fake")
        code, payload = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("FAKE.SLDASM", payload["documents"])
        code, payload = self.run_cli("selftest")
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

    def test_open_and_close_document(self):
        code, payload = self.run_cli("open", "D:/models/Other.SLDASM")
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["opened"], "OTHER.SLDASM")
        code, payload = self.run_cli("close", "Other.SLDASM", "--confirm")
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["closed"], "OTHER.SLDASM")

    def test_export_writes_a_verified_package(self):
        out = os.path.join(self.tmp, "out")
        code, payload = self.run_cli("export-urdf", "--doc", "FAKE.SLDASM", "--out", out, "--config", self.config_path)
        self.assertEqual(code, 0)
        self.assertEqual(payload["job"]["status"], "succeeded")
        self.assertTrue(os.path.isfile(os.path.join(out, "robot.urdf")))
        code, verified = self.run_cli("verify-package", out)
        self.assertEqual(code, 0)
        self.assertTrue(verified["ok"])

    def test_job_inspection_and_cancel_paths(self):
        out = os.path.join(self.tmp, "out2")
        code, payload = self.run_cli(
            "export-urdf", "--doc", "FAKE.SLDASM", "--out", out, "--config", self.config_path, "--no-wait"
        )
        self.assertEqual(code, 0)
        job_id = payload["job"]["id"]
        code, payload = self.run_cli("job", "status", job_id)
        self.assertEqual(code, 0)
        self.assertIn(payload["job"]["status"], ("queued", "running", "succeeded"))
        code, payload = self.run_cli("job", "log", job_id)
        self.assertEqual(code, 0)
        self.assertIn("log", payload)
        code, payload = self.run_cli("job", "cancel", job_id)
        self.assertEqual(code, 0)

    def test_unknown_job_is_reported_with_an_error_code(self):
        code, payload = self.run_cli("job", "status", "job-does-not-exist")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error"]["code"], "job_not_found")


if __name__ == "__main__":
    unittest.main()
