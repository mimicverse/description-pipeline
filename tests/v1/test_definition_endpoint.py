"""Definition exchange: raw artifact binding, admission rollback, abort and loopback.

Every leg is synthetic: the native reader, the capture runner and the Linux
derivation are controlled by this fixture, so nothing here can qualify CAD.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from description_pipeline.io import PipelineError, digest
from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    WindowsEndpoint,
)
from description_pipeline.orchestration.discovery_transfer import (
    RAW_RECORD_ARCHIVE,
    derive_from_raw_bundle,
    seal_capture_request,
)
from description_pipeline.orchestration.selection_client import prepare_staging_archive
from description_pipeline.orchestration.windows import RequestError, handler
from description_pipeline.sources.solidworks.discovery import DISCOVERY_FILE, DiscoverySettings
from .endpoint_support import MAIN, TOOL, EndpointFixture, source_bytes


class DefinitionEndpointTests(EndpointFixture, unittest.TestCase):
    def reached_definition(self, jobs, run_id: str) -> dict:
        sealed = self.stage_and_seal(jobs, run_id)
        self.assertNotEqual("failed", sealed["status"], sealed.get("error"))
        snapshot = self.wait_state_any_status(jobs, run_id, {"awaiting_definition"})
        self.assertEqual("awaiting_definition", snapshot["status"], snapshot.get("error"))
        return snapshot

    def build_response(self, jobs, run_id: str) -> dict:
        self.complete_definition(jobs, run_id, submit=False)
        return dict(self.last_definition)

    def test_raw_record_tamper_is_refused_and_rolled_back(self) -> None:
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        self.reached_definition(jobs, request["run_id"])
        response = self.build_response(jobs, request["run_id"])
        bundle = self.raw_record_archive(jobs, request["run_id"])
        original = bundle.read_bytes()
        bundle.write_bytes(original + b"tampered")
        with self.assertRaises(PipelineError):
            jobs.submit_definition(request["run_id"], response["archive"], response["prepared_sha256"])
        snapshot = jobs.snapshot(request["run_id"])
        self.assertEqual("awaiting_definition", snapshot["status"])
        self.assertFalse((self.config["state_root"] / "prepared" / request["run_id"]).exists())
        bundle.write_bytes(original)
        jobs.submit_definition(request["run_id"], response["archive"], response["prepared_sha256"])
        finished = self.await_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))

    def test_definition_retry_is_idempotent_even_after_capture(self) -> None:
        calls: list = []

        def runner(package, output, **kwargs):
            calls.append(kwargs["run_id"])
            return self.capture_transfer_result(output, run_id=kwargs["run_id"], on_event=kwargs.get("on_event"))

        jobs = self.jobs(runner)
        request = self.request()
        jobs.create(request)
        self.reached_definition(jobs, request["run_id"])
        response = self.build_response(jobs, request["run_id"])
        jobs.submit_definition(request["run_id"], response["archive"], response["prepared_sha256"])
        finished = self.await_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        repeat = jobs.submit_definition(request["run_id"], response["archive"], response["prepared_sha256"])
        self.assertEqual("native_complete", repeat["status"])
        self.assertEqual(finished["capture_archive"], repeat["capture_archive"])
        self.assertEqual(1, len(calls))
        mutated = Path(response["archive"]).read_bytes() + b"x"
        other = Path(response["archive"]).with_name("mutated-request.zip")
        other.write_bytes(mutated)
        with self.assertRaises(RequestError) as refused:
            jobs.submit_definition(request["run_id"], other, response["prepared_sha256"])
        self.assertEqual(409, refused.exception.status)
        self.assertEqual(1, len(calls))

    def test_wrong_prepared_identity_is_refused_and_rollback_is_clean(self) -> None:
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        self.reached_definition(jobs, request["run_id"])
        response = self.build_response(jobs, request["run_id"])
        with self.assertRaises(PipelineError):
            jobs.submit_definition(request["run_id"], response["archive"], "0" * 64)
        snapshot = jobs.snapshot(request["run_id"])
        self.assertEqual("awaiting_definition", snapshot["status"])
        self.assertFalse((self.config["state_root"] / "prepared" / request["run_id"]).exists())
        jobs.submit_definition(request["run_id"], response["archive"], response["prepared_sha256"])
        finished = self.await_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))

    def test_foreign_run_definition_is_refused(self) -> None:
        jobs = self.jobs()
        first, second = self.request(), self.request()
        jobs.create(first)
        jobs.create(second)
        self.reached_definition(jobs, first["run_id"])
        response = self.build_response(jobs, first["run_id"])
        # The native pipeline is serial end to end: one awaiting definition
        # blocks the queue, so the first run is aborted before the second may
        # reach its own definition.
        jobs.abort(
            first["run_id"],
            {"error": "released for the serial control", "error_code": "control", "detail": {}},
        )
        self.reached_definition(jobs, second["run_id"])
        with self.assertRaises(PipelineError):
            jobs.submit_definition(second["run_id"], response["archive"], response["prepared_sha256"])
        snapshot = jobs.snapshot(second["run_id"])
        self.assertEqual("awaiting_definition", snapshot["status"])
        self.assertFalse((self.config["state_root"] / "prepared" / second["run_id"]).exists())
        second_response = self.build_response(jobs, second["run_id"])
        jobs.submit_definition(second["run_id"], second_response["archive"], second_response["prepared_sha256"])
        finished_second = self.await_terminal(jobs, second["run_id"])
        self.assertEqual("native_complete", finished_second["status"], finished_second.get("error"))

    def test_restart_fails_awaiting_definition_and_refuses_later_definitions(self) -> None:
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        self.reached_definition(jobs, request["run_id"])
        response = self.build_response(jobs, request["run_id"])
        jobs.close()
        restarted = self.jobs(lambda *args, **kwargs: self.fail("an interrupted definition must not resume"))
        snapshot = restarted.snapshot(request["run_id"])
        self.assertEqual("failed", snapshot["status"])
        self.assertIn("Endpoint closed while waiting", snapshot["error"])
        with self.assertRaises(RequestError) as refused:
            restarted.submit_definition(request["run_id"], response["archive"], response["prepared_sha256"])
        self.assertIn(refused.exception.status, {400, 409})

    def test_abort_retains_the_derivation_failure(self) -> None:
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        self.reached_definition(jobs, request["run_id"])
        failure = {
            "error": "the derivation rejected two mates",
            "error_code": "derivation_failed",
            "detail": {"findings": [{"code": "joints.limit_missing", "blocking": True}]},
        }
        first = jobs.abort(request["run_id"], failure)
        self.assertEqual("failed", first["status"])
        self.assertEqual(failure["error"], first["error"])
        self.assertEqual("derivation_failed", first["error_code"])
        self.assertEqual(failure["detail"], first["detail"])
        self.assertEqual(failure, first["client_failure"])
        repeat = jobs.abort(request["run_id"], dict(failure))
        self.assertEqual(first, repeat)
        with self.assertRaises(RequestError) as refused:
            jobs.abort(request["run_id"], {**failure, "error": "another failure"})
        self.assertEqual(409, refused.exception.status)

    def test_registry_and_derivation_settings_are_bound(self) -> None:
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        self.assertTrue(self.frozen_names)
        self.assertNotEqual(digest(self.frozen_names), digest({}))
        check = next(
            event["check"] for event in finished["events"] if (event.get("check") or {}).get("id") == "discovery.inputs"
        )
        self.assertEqual(digest(self.frozen_names), check["details"]["frozen_names_sha256"])
        self.assertEqual([str(self.records_dir())], check["details"]["record_roots"])
        payload = json.loads(
            (self.config["state_root"] / "prepared" / request["run_id"] / DISCOVERY_FILE).read_text(encoding="utf-8")
        )
        self.assertEqual(self.frozen_names, payload["derivation"]["frozen_names"])
        self.assertEqual(MAIN, payload["derivation"]["main_assembly"])

    def test_http_definition_route_rejects_bad_headers(self) -> None:
        jobs = self.jobs()
        token = jobs.config["token"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(jobs, token))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}/v1/jobs"
        auth = {"Authorization": "Bearer " + token}
        request = self.request()
        jobs.create(request)
        self.reached_definition(jobs, request["run_id"])
        response = self.build_response(jobs, request["run_id"])
        body = Path(response["archive"]).read_bytes()

        def post(headers):
            attempt = urllib.request.Request(
                f"{base}/{request['run_id']}/definition", data=body, method="POST", headers=headers
            )
            try:
                with urllib.request.urlopen(attempt, timeout=5) as reply:
                    return reply.status, json.load(reply)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        plain = {**auth, "Content-Type": "text/plain", "X-Prepared-SHA256": response["prepared_sha256"]}
        status, _payload = post(plain)
        self.assertEqual(400, status)
        status, _payload = post({**auth, "Content-Type": "application/zip"})
        self.assertEqual(400, status)
        status, _payload = post({**auth, "Content-Type": "application/zip", "X-Prepared-SHA256": "0" * 64})
        self.assertIn(status, {400, 409})
        self.assertEqual("awaiting_definition", jobs.snapshot(request["run_id"])["status"])
        status, _payload = post(
            {**auth, "Content-Type": "application/zip", "X-Prepared-SHA256": response["prepared_sha256"]}
        )
        self.assertEqual(202, status)
        finished = self.await_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))


class DefinitionClientLoopbackTests(EndpointFixture, unittest.TestCase):
    def test_client_drives_the_whole_native_boundary_over_loopback(self) -> None:
        jobs = self.jobs()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(jobs, jobs.config["token"]))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = WindowsEndpoint(
            EndpointConfig(base_url=f"http://127.0.0.1:{server.server_port}", token=jobs.config["token"], timeout=5.0)
        )
        run_id = str(uuid.uuid4())
        client.start_staging_job(run_id=run_id, upload_files=self.upload_files, main_assembly=MAIN)

        source = Path(self.enterContext(TemporaryDirectory()))
        for name, payload in source_bytes().items():
            target = source.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

        archive = self.root / f"upload-{run_id}.zip"
        batch = prepare_staging_archive(source, archive, self.upload_files)
        self.assertEqual(set(batch["files"]), set(self.upload_files))
        client.stage_folder_archive(run_id, archive, upload_files=self.upload_files)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and client.get_job(run_id)["status"] != "awaiting_definition":
            time.sleep(0.02)
        snapshot = client.staging_snapshot(run_id, upload_files=self.upload_files)
        self.assertEqual("sealed", snapshot.state)
        manifest = snapshot.manifest
        record = client.definition_snapshot(run_id, upload_files=self.upload_files)
        bundle = client.stream_native_record(run_id, record, self.root / f"{run_id}-raw-record.zip")
        prepared = derive_from_raw_bundle(
            bundle,
            Path(jobs._source_dir(jobs.jobs[run_id])),
            self.root / f"prepared-{run_id}",
            expected_run_id=run_id,
            expected_selection=manifest,
            expected_native_tool=TOOL,
            settings=DiscoverySettings(record_roots=[self.records_dir()], frozen_names=dict(self.frozen_names)),
        )
        request_archive = self.root / f"capture-request-{run_id}.zip"
        sealed_record = {**copy.deepcopy(self.record), "source_selection": copy.deepcopy(manifest)}
        seal_capture_request(prepared, request_archive, record=sealed_record, selection=manifest, native_tool=TOOL)
        client.post_definition_archive(run_id, request_archive, prepared_sha256=prepared.prepared_sha256)
        finished = self.await_terminal(jobs, run_id)
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        self.assertEqual(MAIN, finished["main_assembly"])
        self.assertTrue((jobs.config["state_root"] / "native_records" / run_id / RAW_RECORD_ARCHIVE).is_file())


if __name__ == "__main__":
    unittest.main()
