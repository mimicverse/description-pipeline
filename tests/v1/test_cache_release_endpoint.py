"""IF-10 staged-cache release: one deletion path, receipts, refusals, resumes.

These drive the endpoint's release route with synthetic terminal runs; they
never touch CAD, COM or a Windows host.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from description_pipeline.interface_records import (
    CACHE_IMPORT_CONFIRMATION_SCHEMA,
    cache_import_confirmation_digest,
    validate_cache_release_receipt,
)
from description_pipeline.orchestration.windows import RequestError, handler
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256
from .endpoint_support import EndpointFixture

RELEASE = {
    "version": "1.4.0",
    "source_commit": "ab" * 20,
    "source_sha256": "cd" * 32,
}
TOOL_RECORD = {
    "version": RELEASE["version"],
    "source_sha256": RELEASE["source_sha256"],
    "release": {"source_commit": RELEASE["source_commit"]},
}
SAMPLE_ENTRY = {"path": "cad/robot.SLDASM", "bytes": 1, "sha256": "ef" * 32}


class CacheReleaseTests(EndpointFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        identity = patch(
            "description_pipeline.orchestration.windows.tool_record",
            return_value=copy.deepcopy(TOOL_RECORD),
        )
        identity.start()
        self.addCleanup(identity.stop)

    # -- helpers -------------------------------------------------------------

    def cache_root(self, jobs, run_id):
        return Path(jobs.config["package_root"]) / "selected" / run_id

    def manifest(self, root):
        entries = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                with path.open("rb") as stream:
                    checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                entries.append(
                    {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": checksum}
                )
        return entries

    def release_request(self, run_id, imported, *, operation_id="op-rel-1"):
        production = {"run_id": run_id, "operation_id": "prod-op-1"}
        confirmation = {
            "schema_version": CACHE_IMPORT_CONFIRMATION_SCHEMA,
            "imported": copy.deepcopy(imported),
            "production": copy.deepcopy(production),
            "sealed": {"confirmed_at": "2026-10-15T06:00:00Z", "receipt_sha256": "12" * 32},
            "digest": "",
        }
        confirmation["digest"] = cache_import_confirmation_digest(confirmation)
        return {
            "operation_id": operation_id,
            "binding": {
                "release": dict(RELEASE),
                "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            },
            "import": {"linux_run_id": "linux-run-1", "production": production, "confirmation": confirmation},
        }

    def terminal_job(self, jobs):
        request = self.request()
        jobs.create(request)
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        self.assertTrue(self.cache_root(jobs, request["run_id"]).is_dir())
        return request["run_id"]

    # -- the sanctioned release ----------------------------------------------

    def test_release_removes_the_cache_and_replays_identically(self):
        jobs = self.jobs()
        run_id = self.terminal_job(jobs)
        root = self.cache_root(jobs, run_id)
        imported = self.manifest(root)
        self.assertTrue(imported)
        request = self.release_request(run_id, imported)
        receipt = jobs.release_cache(run_id, request)
        validate_cache_release_receipt(receipt)
        self.assertEqual("released", receipt["status"])
        self.assertEqual(len(imported), receipt["released"]["files"])
        self.assertEqual(sum(entry["bytes"] for entry in imported), receipt["released"]["bytes"])
        self.assertEqual(["terminal_state", "import_confirmation"], receipt["retained"])
        self.assertFalse(root.exists())
        self.assertEqual(receipt, jobs.release_cache(run_id, request))
        second = jobs.release_cache(run_id, self.release_request(run_id, imported, operation_id="op-rel-2"))
        validate_cache_release_receipt(second)
        self.assertEqual("already_released", second["status"])
        self.assertEqual({"files": 0, "bytes": 0}, second["released"])
        self.assertEqual(receipt["released_at"], second["released_at"])

    def test_reused_operation_with_different_parameters_conflicts(self):
        jobs = self.jobs()
        run_id = self.terminal_job(jobs)
        imported = self.manifest(self.cache_root(jobs, run_id))
        jobs.release_cache(run_id, self.release_request(run_id, imported))
        different = self.release_request(run_id, imported)
        different["import"]["confirmation"]["sealed"]["receipt_sha256"] = "34" * 32
        different["import"]["confirmation"]["digest"] = cache_import_confirmation_digest(
            different["import"]["confirmation"]
        )
        with self.assertRaises(RequestError) as raised:
            jobs.release_cache(run_id, different)
        self.assertEqual(409, raised.exception.status)
        self.assertEqual("cache_release_conflict", raised.exception.payload["error_code"])

    # -- the four negative controls ------------------------------------------

    def test_non_terminal_run_refuses_before_any_deletion(self):
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        with self.assertRaises(RequestError) as raised:
            jobs.release_cache(request["run_id"], self.release_request(request["run_id"], [dict(SAMPLE_ENTRY)]))
        self.assertEqual("cache_release_refused", raised.exception.payload["error_code"])
        self.assertEqual("job_not_terminal", raised.exception.payload["reason"])

    def test_manifest_mismatch_refuses_without_deleting(self):
        jobs = self.jobs()
        run_id = self.terminal_job(jobs)
        root = self.cache_root(jobs, run_id)
        imported = self.manifest(root)
        broken = copy.deepcopy(imported)
        broken[0]["sha256"] = "00" * 32
        with self.assertRaises(RequestError) as raised:
            jobs.release_cache(run_id, self.release_request(run_id, broken))
        self.assertEqual("imported_manifest_mismatch", raised.exception.payload["reason"])
        self.assertEqual(imported, self.manifest(root))

    def test_interrupted_release_resumes_and_returns_one_receipt(self):
        jobs = self.jobs()
        run_id = self.terminal_job(jobs)
        root = self.cache_root(jobs, run_id)
        imported = self.manifest(root)
        request = self.release_request(run_id, imported)

        def die_mid_delete(path, *args, **kwargs):
            for victim in sorted(Path(path).rglob("*")):
                if victim.is_file():
                    victim.unlink()
                    break
            raise KeyboardInterrupt

        with (
            patch("description_pipeline.orchestration.windows.shutil.rmtree", side_effect=die_mid_delete),
            self.assertRaises(KeyboardInterrupt),
        ):
            jobs.release_cache(run_id, request)
        journal_path = Path(jobs.config["state_root"]) / "releases" / f"{run_id}.json"
        self.assertTrue(journal_path.is_file())
        self.assertEqual("started", json.loads(journal_path.read_text())["state"])
        self.assertTrue(root.is_dir())
        receipt = jobs.release_cache(run_id, request)
        validate_cache_release_receipt(receipt)
        self.assertEqual("released", receipt["status"])
        self.assertEqual(len(imported), receipt["released"]["files"])
        self.assertFalse(root.exists())
        self.assertEqual("done", json.loads(journal_path.read_text())["state"])
        self.assertEqual(receipt, jobs.release_cache(run_id, request))

    def test_out_of_bounds_cache_refuses_with_zero_deletion(self):
        jobs = self.jobs()
        run_id = self.terminal_job(jobs)
        root = self.cache_root(jobs, run_id)
        imported = self.manifest(root)
        outside = Path(jobs.config["package_root"]).parent / "outside-cache"
        outside.mkdir(exist_ok=True)
        keep = outside / "keep.SLDPRT"
        keep.write_bytes(b"stray bytes")
        shutil.rmtree(root)
        root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RequestError) as raised:
            jobs.release_cache(run_id, self.release_request(run_id, imported))
        self.assertEqual("path_outside_cache_root", raised.exception.payload["reason"])
        self.assertEqual(b"stray bytes", keep.read_bytes())
        self.assertTrue(outside.is_dir())

    # -- the HTTP route ------------------------------------------------------

    def test_route_maps_receipts_and_refusals_over_http(self):
        jobs = self.jobs()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(jobs, jobs.config["token"]))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(run_id, payload, *, token=True):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + jobs.config["token"]
            request = urllib.request.Request(
                f"{base}/v1/jobs/{run_id}/cache/release",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        run_id = self.terminal_job(jobs)
        imported = self.manifest(self.cache_root(jobs, run_id))
        status, body = call(run_id, self.release_request(run_id, imported))
        self.assertEqual(200, status)
        self.assertEqual("released", body["status"])
        self.assertFalse(self.cache_root(jobs, run_id).exists())
        other = self.request()
        jobs.create(other)
        refused = self.release_request(other["run_id"], [dict(SAMPLE_ENTRY)])
        status, body = call(other["run_id"], refused)
        self.assertEqual(409, status)
        self.assertEqual("cache_release_refused", body["error_code"])
        self.assertEqual("job_not_terminal", body["reason"])
        status, _body = call(other["run_id"], refused, token=False)
        self.assertEqual(401, status)
