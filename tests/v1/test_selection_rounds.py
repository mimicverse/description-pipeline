"""Focused selection-transport tests: client contract and DAG round wiring (fake HTTP).

The fake endpoint implements the frozen selection contract: POST /v1/jobs with
the upload index, GET snapshots, exact-batch pushes bound by X-Selection-Round,
a seal that re-hashes its own stage, and the awaiting-definition leg (raw-record
artifact download, definition submission, abort). The tests drive the real client
and the real DAG poke loop against it; no SolidWorks, no deployed services.
"""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from description_pipeline.io import PipelineError, digest
from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    EndpointConflict,
    EndpointProtocolError,
    WindowsEndpoint,
    drive_selection_round,
)
from description_pipeline.orchestration.selection_client import prepare_selection_batch
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.sources.solidworks.selection import seal_selection, selection_plan

ROOT = Path(__file__).resolve().parents[2]
DAG_PATH = ROOT / "deploy/airflow/dags/solidworks_to_urdf.py"


def _round_id(requested: dict[str, str]) -> str:
    return digest({"requested": requested})


class _FakeSelection:
    """In-memory endpoint state machine for exactly one selection job."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.pushed_batches: list[list[str]] = []
        self.initial_all = False
        self.switch_reader_after_pushes: int | None = None
        self.extra_staged_at_ready: dict[str, str] = {}
        self.missing_staged_at_ready: bool = False

    def create(self, payload: dict) -> dict:
        upload = payload["upload_files"]
        if digest(upload) != payload["upload_sha256"]:
            raise ValueError("upload digest mismatch")
        main = payload["main_assembly"]
        refs = {
            main: ["零件.SLDPRT"],
            "零件.SLDPRT": ["deps/子零件.SLDPRT"],
            "deps/子零件.SLDPRT": [],
        }
        requested = {name: upload[name] for name in upload} if self.initial_all else {main: upload[main]}
        job = {
            "request": dict(payload),
            "upload": dict(upload),
            "refs": refs,
            "reader": "a" * 64,
            "staged": {},
            "observations": {},
            "requested": requested,
            "state": "awaiting_files",
            "settle": None,
            "status": "selecting",
            "pushes": 0,
            "reading_gets": 0,
        }
        self.jobs[payload["run_id"]] = job
        return self.snapshot(job)

    def snapshot(self, job: dict) -> dict:
        selection = {
            "state": job["state"],
            "round_id": _round_id(job["requested"]),
            "reader_sha256": job["reader"],
            "observations": {name: dict(record) for name, record in job["observations"].items()},
            "staged_files": dict(job["staged"]),
            "requested_files": dict(job["requested"]),
        }
        if job.get("manifest") is not None:
            selection["manifest"] = job["manifest"]
        snapshot = {
            "schema_version": "solidworks-to-urdf.job/v1",
            "pipeline_id": "solidworks-to-urdf",
            "run_id": job["request"]["run_id"],
            "status": job["status"],
            "events": [],
            "request": dict(job["request"]),
            "native_tool": {"source_sha256": job["reader"], "role": "native"},
            "selection": selection,
        }
        if job.get("record") is not None:
            snapshot["native_record"] = job["record"]
        return snapshot

    def _settle(self, job: dict) -> None:
        if job["state"] != "reading":
            return
        job["reading_gets"] += 1
        if job["reading_gets"] % 2:
            return
        plan = selection_plan(job["upload"], job["request"]["main_assembly"], job["observations"])
        if plan["state"] == "complete":
            job["state"] = "ready"
            job["requested"] = {}
            if self.missing_staged_at_ready:
                job["staged"].pop(job["request"]["main_assembly"], None)
            job["staged"].update(self.extra_staged_at_ready)
        else:
            job["state"] = "awaiting_files"
            job["requested"] = plan["requested_files"]

    def get(self, run_id: str) -> dict:
        job = self.jobs[run_id]
        self._settle(job)
        return self.snapshot(job)

    def push(self, run_id: str, round_id: str, members: dict[str, bytes]) -> dict:
        job = self.jobs[run_id]
        if round_id != _round_id(job["requested"]):
            raise _Conflict("stale selection round")
        for name, blob in members.items():
            checksum = hashlib.sha256(blob).hexdigest()
            if job["upload"].get(name) != checksum:
                raise ValueError(f"unauthorized member: {name}")
        retry = set(members) <= set(job["staged"]) and all(
            job["staged"].get(name) == hashlib.sha256(blob).hexdigest() for name, blob in members.items()
        )
        if not retry and set(members) != set(job["requested"]):
            raise ValueError("push differs from the current request")
        if not retry:
            job["staged"].update({name: job["upload"][name] for name in members})
            for name in members:
                job["observations"][name] = {
                    "sha256": job["staged"][name],
                    "references": list(job["refs"].get(name, [])),
                }
            job["pushes"] += 1
            self.pushed_batches.append(sorted(members))
            if self.switch_reader_after_pushes == job["pushes"]:
                job["reader"] = "b" * 64
            job["state"] = "reading"
            job["reading_gets"] = 0
        return self.snapshot(job)

    def seal(self, run_id: str, manifest: dict) -> dict:
        job = self.jobs[run_id]
        expected = seal_selection(
            job["upload"],
            job["request"]["main_assembly"],
            job["observations"],
            selected_files=job["staged"],
            reader_sha256=job["reader"],
        )
        if manifest != expected:
            raise ValueError("seal differs from the endpoint graph")
        job["state"] = "sealed"
        job["requested"] = {}
        job["status"] = "awaiting_definition"
        job["manifest"] = expected
        record_manifest = {
            "schema_version": "solidworks-to-urdf.raw-record/v1",
            "run_id": job["request"]["run_id"],
            "selection_sha256": expected["selection_sha256"],
            "files": {"record.json": "e" * 64},
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("record.json", json.dumps({"run_id": job["request"]["run_id"], "fixture": True}))
            bundle.writestr("manifest.json", json.dumps(record_manifest))
        blob = buffer.getvalue()
        job["record_blob"] = blob
        job["record"] = {
            "name": "raw-record.zip",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "size": len(blob),
            "manifest_sha256": digest(record_manifest),
            "manifest": record_manifest,
        }
        return self.snapshot(job)

    def definition(self, run_id: str, prepared_sha256: str, members: dict) -> dict:
        job = self.jobs[run_id]
        if job["status"] != "awaiting_definition":
            raise ValueError("job does not await a definition")
        job["definition"] = {"prepared_sha256": prepared_sha256, "members": sorted(members)}
        job["status"] = "running"
        return self.snapshot(job)

    def abort(self, run_id: str, payload: dict) -> dict:
        job = self.jobs[run_id]
        if job["status"] != "awaiting_definition":
            raise ValueError("job does not await a definition")
        job["abort"] = payload
        job["status"] = "failed"
        return self.snapshot(job)


class _Conflict(Exception):
    pass


def _handler(state: _FakeSelection):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: A003 - silence the test server
            return

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length)

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                if self.path == "/v1/jobs":
                    payload = json.loads(self._body())
                    self._reply(201, state.create(payload))
                    return
                parts = self.path.strip("/").split("/")
                run_id = parts[2] if len(parts) > 2 else ""
                if len(parts) == 4 and parts[3] == "files":
                    members = {}
                    with zipfile.ZipFile(io.BytesIO(self._body())) as archive:
                        for name in archive.namelist():
                            members[name] = archive.read(name)
                    self._reply(200, state.push(run_id, self.headers.get("X-Selection-Round"), members))
                    return
                if len(parts) == 4 and parts[3] == "seal":
                    manifest = json.loads(self._body())["selection"]
                    self._reply(200, state.seal(run_id, manifest))
                    return
                if len(parts) == 4 and parts[3] == "definition":
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/zip":
                        raise _Conflict("definition body must be a zip")
                    prepared = self.headers.get("X-Prepared-SHA256")
                    if prepared is None:
                        raise _Conflict("missing prepared digest header")
                    members = {}
                    with zipfile.ZipFile(io.BytesIO(self._body())) as archive:
                        for name in archive.namelist():
                            members[name] = archive.read(name)
                    self._reply(202, state.definition(run_id, prepared, members))
                    return
                if len(parts) == 4 and parts[3] == "abort":
                    self._reply(200, state.abort(run_id, json.loads(self._body())))
                    return
            except _Conflict as error:
                self._reply(409, {"error": str(error)})
                return
            except (ValueError, KeyError) as error:
                self._reply(400, {"error": str(error)})
                return
            self._reply(404, {"error": "unknown route"})

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            parts = self.path.strip("/").split("/")
            if len(parts) == 5 and parts[0] == "v1" and parts[1] == "jobs" and parts[3] == "artifacts":
                job = state.jobs.get(parts[2])
                if job is None or parts[4] != "raw-record.zip" or "record_blob" not in job:
                    self._reply(404, {"error": "no such artifact"})
                    return
                blob = job["record_blob"]
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            if len(parts) == 3 and parts[0] == "v1" and parts[1] == "jobs":
                try:
                    self._reply(200, state.get(parts[2]))
                except KeyError:
                    self._reply(404, {"error": "no such job"})
                return
            self._reply(404, {"error": "unknown route"})

    return Handler


def _intake(root: Path) -> Path:
    intake = root / "intake"
    (intake / "deps").mkdir(parents=True)
    (intake / "总装.SLDASM").write_bytes(b"assembly bytes")
    (intake / "零件.SLDPRT").write_bytes(b"part bytes")
    (intake / "deps" / "子零件.SLDPRT").write_bytes(b"child part bytes")
    (intake / "notes.txt").write_bytes(b"not a dependency")
    (intake / "decoy.SLDPRT").write_bytes(b"unrelated CAD decoy")
    return intake


def _upload_files(intake: Path) -> dict[str, str]:
    return package_inventory(intake)


class SelectionTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _FakeSelection()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.endpoint = WindowsEndpoint(
            EndpointConfig(base_url=f"http://127.0.0.1:{self.server.server_port}", token="t")
        )
        self.tmp = Path(tempfile.mkdtemp(prefix="selection-transport-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.intake = _intake(self.tmp)
        self.upload = _upload_files(self.intake)
        self.run_id = "1b3faa2a-0000-4000-8000-000000000001"
        self.batch_dir = self.tmp / "batches"

    def _start(self) -> dict:
        return self.endpoint.start_selection_job(
            run_id=self.run_id, upload_files=self.upload, main_assembly="总装.SLDASM"
        )

    def _drive(self, previous=None) -> dict:
        return drive_selection_round(
            self.endpoint,
            run_id=self.run_id,
            upload_files=self.upload,
            main_assembly="总装.SLDASM",
            intake_dir=self.intake,
            batch_dir=self.batch_dir,
            previous=previous,
        )

    def test_round_loop_reaches_a_sealed_manifest_without_decoys(self) -> None:
        self._start()
        actions: list[str] = []
        previous = None
        outcome = {}
        for _ in range(8):
            outcome = self._drive(previous)
            actions.append(outcome["action"])
            previous = outcome.get("progress")
            if outcome["action"] == "sealed":
                break
        self.assertEqual(outcome["action"], "sealed")
        self.assertIn("wait", actions)
        manifest = outcome["manifest"]
        self.assertEqual(set(manifest["files"]), {"总装.SLDASM", "零件.SLDPRT", "deps/子零件.SLDPRT"})
        self.assertEqual(manifest["upload_sha256"], digest(self.upload))
        self.assertEqual(manifest["handoff_sha256"], digest(manifest["files"]))
        seal_free = {key: value for key, value in manifest.items() if key != "selection_sha256"}
        self.assertEqual(manifest["selection_sha256"], digest(seal_free))
        self.assertEqual(
            manifest["excluded_files"],
            {"notes.txt": self.upload["notes.txt"], "decoy.SLDPRT": self.upload["decoy.SLDPRT"]},
        )
        pushed = {name for batch in self.state.pushed_batches for name in batch}
        self.assertEqual(pushed, {"总装.SLDASM", "零件.SLDPRT", "deps/子零件.SLDPRT"})
        self.assertNotIn("decoy.SLDPRT", pushed)
        self.assertNotIn("notes.txt", pushed)

    def test_stale_round_push_is_refused(self) -> None:
        self._start()
        with self.assertRaises(EndpointConflict):
            self.endpoint.push_selection_batch(
                run_id=self.run_id,
                round_id="0" * 64,
                archive=self._sealed_batch(),
                batch={"总装.SLDASM": self.upload["总装.SLDASM"]},
                upload_files=self.upload,
            )

    def _sealed_batch(self) -> Path:
        archive = self.tmp / "probe.zip"
        prepare_selection_batch(
            self.intake, archive, {"总装.SLDASM": self.upload["总装.SLDASM"]}, expected_upload=self.upload
        )
        return archive

    def test_reader_identity_change_between_rounds_is_refused(self) -> None:
        self.state.switch_reader_after_pushes = 1
        self._start()
        with self.assertRaises(EndpointProtocolError) as caught:
            self._drive()
        self.assertIn("reader identity changed", str(caught.exception))

    def test_initial_request_must_be_main_only(self) -> None:
        self.state.initial_all = True
        with self.assertRaises(EndpointProtocolError) as caught:
            self._start()
        self.assertIn("main-assembly-only", str(caught.exception))

    def test_missing_and_extra_stage_bytes_are_refused_before_sealing(self) -> None:
        for knob in ("missing_staged_at_ready", "extra_staged_at_ready"):
            with self.subTest(knob=knob):
                self.state.jobs.clear()
                self.state.pushed_batches.clear()
                self.state.switch_reader_after_pushes = None
                self.state.missing_staged_at_ready = False
                self.state.extra_staged_at_ready = {}
                setattr(
                    self.state,
                    knob,
                    {"decoy.SLDPRT": self.upload["decoy.SLDPRT"]} if "extra" in knob else True,
                )
                self._start()
                previous = None
                with self.assertRaises(EndpointProtocolError):
                    for _ in range(8):
                        previous = self._drive(previous).get("progress")
                self.assertNotEqual(self.state.jobs[self.run_id]["state"], "sealed")

    def test_idempotent_retry_of_one_batch_is_tolerated(self) -> None:
        self._start()
        first = self._drive()
        self.assertEqual(first["action"], "pushed")
        archive = self._sealed_batch()
        snapshot = self.state.get(self.run_id)
        self.endpoint.push_selection_batch(
            run_id=self.run_id,
            round_id=snapshot["selection"]["round_id"],
            archive=archive,
            batch={"总装.SLDASM": self.upload["总装.SLDASM"]},
            upload_files=self.upload,
        )
        self.assertEqual(self.state.jobs[self.run_id]["pushes"], 1)

    def test_missing_local_member_is_refused(self) -> None:
        self._start()
        first = self._drive()
        self.assertEqual(first["action"], "pushed")
        (self.intake / "零件.SLDPRT").unlink()
        previous = first["progress"]
        with self.assertRaises(PipelineError) as caught:
            for _ in range(4):
                previous = self._drive(previous).get("progress")
        self.assertIn("Missing artifact", str(caught.exception))

    def _sealed_manifest(self) -> dict:
        self._start()
        previous = None
        for _ in range(8):
            outcome = self._drive(previous)
            previous = outcome.get("progress")
            if outcome["action"] == "sealed":
                return outcome["manifest"]
        self.fail("selection did not reach a sealed manifest")

    def test_definition_snapshot_and_record_transport(self) -> None:
        manifest = self._sealed_manifest()
        snapshot = self.endpoint.definition_snapshot(self.run_id, upload_files=self.upload)
        job = self.state.jobs[self.run_id]
        self.assertEqual(snapshot.record_name, "raw-record.zip")
        self.assertEqual(snapshot.record_sha256, job["record"]["sha256"])
        self.assertEqual(snapshot.record_size, job["record"]["size"])
        self.assertEqual(snapshot.record_manifest, job["record"]["manifest"])
        self.assertEqual(snapshot.selection.manifest, manifest)
        destination = self.tmp / "raw-record.zip"
        self.endpoint.stream_native_record(self.run_id, job["record"], destination)
        self.assertEqual(destination.read_bytes(), job["record_blob"])
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), job["record"]["sha256"])

    def test_definition_hand_off_refuses_broken_blocks(self) -> None:
        self._start()
        with self.assertRaises(EndpointProtocolError):
            self.endpoint.definition_snapshot(self.run_id)
        self._sealed_manifest()
        record = self.state.jobs[self.run_id]["record"]
        for tampered in (
            {**record, "manifest_sha256": "0" * 64},
            {**record, "name": "native-evidence.zip"},
            {**record, "size": 0},
            {**record, "manifest": {}},
        ):
            with self.subTest(tampered=str(tampered)[:60]), self.assertRaises(EndpointProtocolError):
                self.endpoint.stream_native_record(self.run_id, tampered, self.tmp / "tampered.zip")

    def test_definition_submission_carries_the_prepared_digest(self) -> None:
        self._sealed_manifest()
        archive = self.tmp / "capture-request.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("robot.yaml", "robot:\n  name: fixture\n")
        prepared = "f" * 64
        response = self.endpoint.post_definition_archive(self.run_id, archive, prepared_sha256=prepared)
        self.assertEqual(response["status"], "running")
        accepted = self.state.jobs[self.run_id]["definition"]
        self.assertEqual(accepted["prepared_sha256"], prepared)
        self.assertEqual(accepted["members"], ["robot.yaml"])

    def test_abort_reports_the_local_derivation_failure(self) -> None:
        self._sealed_manifest()
        response = self.endpoint.abort_job(
            self.run_id,
            error="derivation refused",
            error_code="definition_derivation_failed",
            detail="findings retained in the Linux output",
        )
        self.assertEqual(response["status"], "failed")
        self.assertEqual(
            self.state.jobs[self.run_id]["abort"],
            {
                "error": "derivation refused",
                "error_code": "definition_derivation_failed",
                "detail": "findings retained in the Linux output",
            },
        )
        with self.assertRaises(EndpointProtocolError):
            self.endpoint.abort_job(self.run_id, error="", error_code="definition_derivation_failed")


class SelectionDagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            import airflow.sdk  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("Airflow is not installed in this interpreter") from None
        spec = importlib.util.spec_from_file_location("solidworks_to_urdf_selection_test", DAG_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.dag = module

    def test_selection_main_is_explicit_or_sole_assembly(self) -> None:
        upload = {"a.SLDASM": "0" * 64, "b.SLDPRT": "0" * 64}
        self.assertEqual(self.dag._selection_main(upload, None), "a.SLDASM")
        self.assertEqual(self.dag._selection_main(upload, "a.SLDASM"), "a.SLDASM")
        with self.assertRaises(self.dag.AirflowFailException):
            self.dag._selection_main({"a.SLDASM": "0" * 64, "b.SLDASM": "0" * 64}, None)
        with self.assertRaises(self.dag.AirflowFailException):
            self.dag._selection_main(upload, "missing.SLDASM")

    def test_same_request_binds_the_selection_payload(self) -> None:
        request = {
            "run_id": "1b3faa2a-0000-4000-8000-000000000001",
            "upload_files": {"a.SLDASM": "0" * 64},
            "upload_sha256": digest({"a.SLDASM": "0" * 64}),
            "main_assembly": "a.SLDASM",
        }
        job = {"request": dict(request)}
        self.dag._same_request(job, request)
        with self.assertRaises(self.dag.AirflowFailException):
            self.dag._same_request({"request": {**request, "upload_sha256": "0" * 64}}, request)

    def test_linked_source_reuses_the_parent_selection(self) -> None:
        upload = {"a.SLDASM": "0" * 64, "b.SLDASM": "1" * 64, "c.SLDPRT": "2" * 64}
        retained = {
            "run_id": "parent",
            "upload_files": upload,
            "upload_sha256": digest(upload),
            "main_assembly": "a.SLDASM",
        }
        files, checksum, main = self.dag._linked_source(retained, None)
        self.assertEqual(files, upload)
        self.assertEqual(checksum, digest(upload))
        self.assertEqual(main, "a.SLDASM")
        self.assertEqual(self.dag._linked_source(retained, "a.SLDASM")[2], "a.SLDASM")
        for retained_payload, supplied in (
            (retained, "b.SLDASM"),
            ({**retained, "upload_sha256": "0" * 64}, None),
            ({"package": "handoff/m3.0", "main_assembly": "a.SLDASM"}, None),
            ({**retained, "upload_files": {"a.SLDASM": "not-a-digest"}}, None),
        ):
            with (
                self.subTest(supplied=supplied, retained=str(retained_payload)[:48]),
                self.assertRaises(self.dag.AirflowFailException),
            ):
                self.dag._linked_source(retained_payload, supplied)


if __name__ == "__main__":
    unittest.main()
