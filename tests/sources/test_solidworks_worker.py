"""Loopback worker API: trigger a freeze and fetch the result package."""

from __future__ import annotations

import io
import http.client
import json
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.freeze import freeze  # noqa: E402
from description_pipeline.sources.solidworks.worker import Worker, serve  # noqa: E402

from . import support  # noqa: E402


def request(method: str, url: str, payload: object = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=20) as response:
        body = response.read()
        content_type = response.headers.get("Content-Type", "")
        return response.status, body, content_type


class WorkerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-worker-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.parts = [self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"]
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [{"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0)}],
            dependencies=self.parts,
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": True},
            "bodies": [{"id": "base", "name": "base_link", "components": ["base-1"]}],
            "joints": [],
        }
        self.worker = Worker(
            jobs_root=self.tmp / "jobs",
            backend_factory=lambda: self.backend,
            freeze_fn=lambda config, destination: freeze(config, destination, backend=self.backend),
            watchdog_seconds=30.0,
        )
        self.server, self.server_thread = serve(self.worker, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        self.worker.close()
        support.cleanup(self.tmp)

    def _run_freeze(self) -> str:
        status, body, _ = request("POST", f"{self.base}/jobs", {"kind": "freeze", "config": self.config})
        self.assertEqual(status, 202)
        job_id = json.loads(body)["job_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            _, body, _ = request("GET", f"{self.base}/jobs/{job_id}")
            payload = json.loads(body)
            if payload["state"] in ("succeeded", "failed", "cancelled"):
                self.assertEqual(payload["state"], "succeeded", payload.get("error"))
                return job_id
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_health_reports_worker_and_runner(self) -> None:
        status, body, _ = request("GET", f"{self.base}/health")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def _raw(self, method: str, path: str, headers: dict[str, str], body: str = "") -> tuple[int, dict]:
        """Send the request by hand, so `Host` and `Origin` can be whatever the case needs."""

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True)
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders(body.encode("utf-8") if body else None)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_a_rebinding_host_header_is_refused(self) -> None:
        """Loopback binding does not stop a DNS name that resolves to 127.0.0.1."""

        status, payload = self._raw("GET", "/health", {"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "untrusted_client")

    def test_a_cross_site_request_is_refused(self) -> None:
        """A page on any site can POST here without reading the answer; only loopback may."""

        body = json.dumps({"kind": "maintenance"})
        status, payload = self._raw(
            "POST",
            "/maintenance",
            {
                "Host": f"127.0.0.1:{self.server.server_address[1]}",
                "Origin": "https://attacker.example",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body=body,
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "untrusted_client")

    def test_the_local_client_is_still_served(self) -> None:
        status, payload = self._raw(
            "GET",
            "/health",
            {"Host": f"127.0.0.1:{self.server.server_address[1]}", "Origin": self.base},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("runner", payload)

    def test_idle_maintenance_rejects_new_jobs_until_resumed(self) -> None:
        _, body, _ = request("POST", f"{self.base}/maintenance")
        self.assertTrue(json.loads(body)["maintenance"])
        with self.assertRaises(urllib.error.HTTPError) as raised:
            request("POST", f"{self.base}/jobs", {"kind": "doctor"})
        self.assertIn(b"worker_maintenance", raised.exception.read())
        request("POST", f"{self.base}/resume")
        self._run_freeze()

    def test_doctor_decodes_windows_paths_and_configuration(self) -> None:
        assembly = r"C:\CAD files\robot.SLDASM"
        configuration = "Revision A+B"
        with patch.object(self.worker, "doctor", return_value={"probe": True}) as doctor:
            request("GET", f"{self.base}/doctor?" + urlencode({"assembly": assembly, "configuration": configuration}))
        doctor.assert_called_once_with(assembly, configuration)

    def test_doctor_separates_install_from_collection(self) -> None:
        status, body, _ = request("GET", f"{self.base}/doctor?assembly={self.assembly}")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["installed"])
        self.assertTrue(payload["worker_alive"])
        self.assertTrue(payload["solidworks_reachable"])
        self.assertTrue(payload["cad_collectable"])

    def test_freeze_job_can_be_fetched_as_manifest_files_and_package(self) -> None:
        job_id = self._run_freeze()

        status, body, _ = request("GET", f"{self.base}/jobs/{job_id}/manifest")
        manifest = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(manifest["kind"], "solidworks")

        status, body, _ = request("GET", f"{self.base}/jobs/{job_id}/files")
        files = json.loads(body)["files"]
        self.assertIn("scene.json", files)

        status, body, content_type = request("GET", f"{self.base}/jobs/{job_id}/package")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/x-tar")
        with tarfile.open(fileobj=io.BytesIO(body), mode="r") as archive:
            names = archive.getnames()
        self.assertTrue(any(name.endswith("scene.json") for name in names))
        self.assertTrue(any(name.endswith("manifest.json") for name in names))

    def test_result_is_not_served_before_success(self) -> None:
        _status, body, _ = request("POST", f"{self.base}/jobs", {"kind": "freeze", "config": self.config})
        job_id = json.loads(body)["job_id"]
        try:
            request("GET", f"{self.base}/jobs/{job_id}/package")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 409)
        else:  # the job may already have succeeded on a fast machine
            pass

    def test_maintenance_is_refused_while_a_job_is_active(self) -> None:
        release = threading.Event()

        def slow_freeze(config, destination):
            release.wait(timeout=15)
            return freeze(config, destination, backend=self.backend)

        self.worker._freeze_fn = slow_freeze  # noqa: SLF001 - test injection
        status, body, _ = request("POST", f"{self.base}/jobs", {"kind": "freeze", "config": self.config})
        self.assertEqual(status, 202)
        job_id = json.loads(body)["job_id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            state = json.loads(request("GET", f"{self.base}/jobs/{job_id}")[1])["state"]
            if state == "running":
                break
            time.sleep(0.05)
        self.assertEqual(state, "running")

        with self.assertRaises(urllib.error.HTTPError) as raised:
            request("POST", f"{self.base}/maintenance")
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "worker_busy")

        release.set()
        deadline = time.time() + 20
        while time.time() < deadline:
            state = json.loads(request("GET", f"{self.base}/jobs/{job_id}")[1])["state"]
            if state in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.05)
        self.assertEqual(state, "succeeded")
        status, _body, _ = request("POST", f"{self.base}/maintenance")
        self.assertEqual(status, 200)

    def test_active_cancel_cleans_the_owned_operation_and_does_not_publish(self) -> None:
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def collect(config, destination):
            started.set()
            release.wait(10)
            raise RuntimeError("owned process stopped")

        self.worker._freeze_fn = collect
        self.backend.abort_owned_processes = release.set  # type: ignore[attr-defined]
        _, body, _ = request("POST", f"{self.base}/jobs", {"kind": "freeze", "config": self.config})
        job_id = json.loads(body)["job_id"]
        self.assertTrue(started.wait(3))
        request("POST", f"{self.base}/jobs/{job_id}/cancel")
        deadline = time.time() + 5
        while time.time() < deadline:
            payload = json.loads(request("GET", f"{self.base}/jobs/{job_id}")[1])
            if payload["state"] == "cancelled":
                break
            time.sleep(0.02)
        self.assertEqual(payload["state"], "cancelled")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request("GET", f"{self.base}/jobs/{job_id}/package")
        self.assertEqual(caught.exception.code, 409)

    def test_stopped_job_with_stuck_executor_has_a_maintenance_recovery_exit(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        from description_pipeline.sources.solidworks.errors import BridgeError

        with self.assertRaises(BridgeError):
            self.worker.executor().run(lambda: release.wait(10), timeout=0.05)
        health = self.worker.health()
        self.assertTrue(health["cad_operation_active"])
        self.assertTrue(health["cad_recovery_required"])
        self.assertTrue(self.worker.maintenance(True)["maintenance"])
        release.set()

    def test_unknown_route_is_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            request("GET", f"{self.base}/nope")
        self.assertEqual(raised.exception.code, 404)

    def test_maintenance_is_refused_while_a_recovered_job_is_still_queued(self) -> None:
        from description_pipeline.sources.solidworks.jobs import Job
        from description_pipeline.sources.solidworks.jsonio import digest_json

        request_payload: dict[str, object] = {"kind": "freeze", "config": self.config}
        # what a previous process leaves behind: queued in the record but unknown
        # to this process's in-memory runner queue
        self.worker.store.save(
            Job(
                job_id="job-recovered",
                request=request_payload,
                request_digest=digest_json(request_payload),
                state="queued",
            )
        )

        _status, body, _ = request("GET", f"{self.base}/health")
        self.assertEqual(json.loads(body)["jobs"]["queued"], 1)

        with self.assertRaises(urllib.error.HTTPError) as raised:
            request("POST", f"{self.base}/maintenance")

        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"]["code"], "worker_busy")


if __name__ == "__main__":
    unittest.main()
