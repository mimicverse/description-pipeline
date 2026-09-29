import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest

from tools.solidworks_export import cli
from tools.solidworks_export.backends import FakeBackend
from tools.solidworks_export.server import create_server

from .helpers import make_config_dict


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swbridge-server-")
        self.server = create_server(FakeBackend(), os.path.join(self.tmp, "state"), "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        if isinstance(host, bytes):  # typeshed 允许 bytes 地址；这里统一成字符串
            host = host.decode()
        self.endpoint = f"http://{host}:{port}"
        self.client = cli.Client(self.endpoint)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_health_and_documents(self):
        health = self.client.request("GET", "/v1/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["health"]["backend"], "fake")
        documents = self.client.request("GET", "/v1/documents")
        self.assertIn("FAKE.SLDASM", documents["documents"])

    def test_export_job_roundtrip(self):
        config_path = os.path.join(self.tmp, "export_config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(make_config_dict(), handle)
        out_dir = os.path.join(self.tmp, "out")
        response = self.client.request(
            "POST",
            "/v1/jobs",
            {
                "kind": "export-urdf",
                "doc": "D:/models/FAKE.SLDASM",
                "out": out_dir,
                "config": config_path,
            },
        )
        job_id = response["job"]["id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            job = self.client.request("GET", f"/v1/jobs/{job_id}")["job"]
            if job["status"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "succeeded", job.get("error"))
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "robot.urdf")))
        log = self.client.request("GET", f"/v1/jobs/{job_id}/log")["log"]
        self.assertIn("export-urdf", log)
        with open(os.path.join(out_dir, "native_source.json"), encoding="utf-8") as handle:
            native = json.load(handle)
        self.assertEqual(native["evidence_class"], "synthetic")
        self.assertEqual(job["params"]["evidence_class"], "synthetic")

    def test_cli_ping_exit_code(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["--endpoint", self.endpoint, "ping"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue().strip())
        self.assertTrue(payload["ok"])

    def test_selftest_failure_is_not_http_success_or_cli_zero(self):
        self.server.backend.selftest = lambda **kwargs: {  # type: ignore[method-assign]
            "points": {"mass": {"ok": False, "error": "null"}}
        }
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["--endpoint", self.endpoint, "selftest"])
        self.assertEqual(code, 3)
        self.assertIs(json.loads(buffer.getvalue())["ok"], False)

    def test_selftest_empty_cannot_pass(self):
        self.server.backend.selftest = lambda **kwargs: {"points": {}}  # type: ignore[method-assign]
        self.assertIs(self.client.request("GET", "/v1/selftest")["ok"], False)

    def test_unknown_job_returns_error_code(self):
        with self.assertRaises(Exception) as ctx:
            cli.Client(self.endpoint).request("GET", "/v1/jobs/nope")
        self.assertEqual(getattr(ctx.exception, "code", None), "job_not_found")

    def test_audit_log_records_requests(self):
        self.client.request("GET", "/v1/health")
        audit_path = os.path.join(self.tmp, "state", "audit.jsonl")
        self.assertTrue(os.path.isfile(audit_path))
        with open(audit_path, encoding="utf-8") as handle:
            entries = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(entries)
        last = entries[-1]
        self.assertEqual(last["path"], "/v1/health")
        self.assertEqual(last["status"], 200)
        self.assertIsNotNone(last["duration_ms"])
        self.assertEqual(last["client"], "127.0.0.1")

    def test_allowlist_rejects_outside_paths(self):
        server = create_server(
            FakeBackend(),
            os.path.join(self.tmp, "state-allow"),
            "127.0.0.1",
            0,
            allowed_roots=["D:\\models", "D:\\export"],
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            if isinstance(host, bytes):
                host = host.decode()
            client = cli.Client(f"http://{host}:{port}")
            with self.assertRaises(Exception) as ctx:
                client.request("POST", "/v1/documents/open", {"path": "C:\\other\\x.SLDASM"})
            self.assertEqual(getattr(ctx.exception, "code", None), "path_not_allowed")
            response = client.request("POST", "/v1/documents/open", {"path": "D:\\models\\x.SLDASM"})
            self.assertTrue(response["ok"])
        finally:
            server.shutdown()
            server.server_close()

    def test_response_waits_for_audit_attempt(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        record = self.server.audit.record
        errors = []

        def delayed_record(payload):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release audit writer")
            record(payload)

        def request():
            try:
                self.client.request("GET", "/v1/health")
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        self.server.audit.record = delayed_record  # type: ignore[method-assign]
        client_thread = threading.Thread(target=request)
        client_thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertFalse(completed.wait(0.1), "Response acknowledged before audit")
        finally:
            release.set()
            client_thread.join(5)
        self.assertFalse(client_thread.is_alive())
        self.assertFalse(errors)
        self.assertTrue(completed.is_set())


if __name__ == "__main__":
    unittest.main()
