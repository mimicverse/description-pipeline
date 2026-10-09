"""Exercise the deployment's asset probe against actual browser module bytes."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "description_pipeline" / "orchestration" / "static"
HEALTH = ROOT / "deploy" / "operator" / "health.sh"
PROBE = HEALTH.read_text(encoding="utf-8").split("<<'ASSETS'\n", 1)[1].split("\nASSETS", 1)[0]


class BrowserAssetHealthTests(unittest.TestCase):
    def probe(self, fault: str | None = None) -> subprocess.CompletedProcess[str]:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                name = self.path.removeprefix("/static/")
                body = (STATIC / name).read_bytes()
                status, content_type = 200, "application/javascript"
                if fault == "missing" and name == "vendor/three.core.min.js":
                    status, body = 404, b"missing"
                elif fault == "html" and name == "viewer.js":
                    content_type = "text/html"
                elif fault == "changed" and name == "vendor/three.module.min.js":
                    body += b"\n// changed\n"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                return subprocess.run(
                    [sys.executable, "-", str(server.server_port)],
                    input=PROBE,
                    env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            finally:
                server.shutdown()
                thread.join()

    def test_actual_module_bundle_passes(self):
        result = self.probe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_core_module_fails(self):
        result = self.probe("missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("404", result.stdout)

    def test_html_response_fails(self):
        result = self.probe("html")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("viewer.js: expected JavaScript", result.stdout)

    def test_changed_served_bytes_fail(self):
        result = self.probe("changed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("three.module.min.js: served bytes differ", result.stdout)
