"""HTTP error paths: unknown routes, bad bodies, disallowed paths, missing jobs."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from tools.solidworks_export.backends import FakeBackend
from tools.solidworks_export.server import create_server


class ServerErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swbridge-errors-")
        self.allowed = os.path.join(self.tmp, "allowed")
        os.makedirs(self.allowed)
        self.server = create_server(
            FakeBackend(), os.path.join(self.tmp, "state"), "127.0.0.1", 0, allowed_roots=[self.allowed]
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address[:2]
        if isinstance(host, bytes):
            host = host.decode()
        self.endpoint = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(
            self.endpoint + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_unknown_route_is_404(self):
        status, payload = self.request("GET", "/v1/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_bad_json_body_is_400(self):
        status, payload = self.request("POST", "/v1/documents/open", raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "usage_error")

    def test_missing_field_is_400(self):
        status, payload = self.request("POST", "/v1/documents/open", body={})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "usage_error")

    def test_path_outside_allowed_roots_is_403(self):
        status, payload = self.request(
            "POST", "/v1/documents/open", body={"path": os.path.join(self.tmp, "elsewhere", "x.SLDPRT")}
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "path_not_allowed")

    def test_job_rejects_unknown_kind(self):
        status, payload = self.request("POST", "/v1/jobs", body={"kind": "nope"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "usage_error")

    def test_unknown_job_is_404(self):
        status, payload = self.request("GET", "/v1/jobs/job-missing")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "job_not_found")


if __name__ == "__main__":
    unittest.main()
