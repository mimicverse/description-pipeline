"""Folder-staging transport: one exact indexed ZIP, server-side closure seal, retries.

The fake endpoint implements the locked contract: ``POST /v1/jobs`` with the
upload index, one ``POST /v1/jobs/{id}/files`` carrying exactly the full index,
and a server-side native closure that becomes the sealed manifest. The tests
drive the real client and ``drive_staging``; no SolidWorks, no deployed services.
"""

from __future__ import annotations

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

from description_pipeline.io import digest
from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    EndpointError,
    EndpointProtocolError,
    JOB_SCHEMA,
    PIPELINE_ID,
    WindowsEndpoint,
    drive_staging,
)
from description_pipeline.orchestration.selection_client import prepare_staging_archive
from description_pipeline.sources.solidworks.revision import package_inventory

ROOT = Path(__file__).resolve().parents[2]
DAG_PATH = ROOT / "deploy/airflow/dags/solidworks_to_urdf.py"
READER_SHA256 = "d" * 64


class _FakeStaging:
    """In-memory endpoint implementing staging plus the native closure seal."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.closure_runs = 0
        self.failed_closure = False
        self.extra_block_key = False
        self.sealed_without_manifest = False
        self.sealed_staged_short = False
        self.linked_foreign_main = False
        self.linked_outside_file = False

    def create(self, payload: dict) -> dict:
        upload = payload["upload_files"]
        if digest(upload) != payload["upload_sha256"]:
            raise ValueError("upload digest mismatch")
        main = payload["main_assembly"]
        resume = payload.get("resume")
        if resume is not None:
            parent = self.jobs[resume["parent_run"]]
            if parent["state"] != "sealed":
                raise ValueError("linked parent closure is not sealed")
            manifest = dict(parent["manifest"])
            if self.linked_foreign_main:
                manifest["main_assembly"] = "other.SLDASM"
            if self.linked_outside_file:
                manifest["files"] = {**manifest["files"], "outside.SLDPRT": "a" * 64}
            job = {
                "request": dict(payload),
                "upload": dict(upload),
                "upload_sha256": payload["upload_sha256"],
                "main_assembly": main,
                "staged": dict(parent["upload"]),
                "state": "sealed",
                "manifest": manifest,
                "status": "running",
                "reading_gets": 0,
            }
        else:
            job = {
                "request": dict(payload),
                "upload": dict(upload),
                "upload_sha256": payload["upload_sha256"],
                "main_assembly": main,
                "staged": {},
                "state": "awaiting_files",
                "manifest": None,
                "status": "selecting",
                "reading_gets": 0,
            }
        self.jobs[payload["run_id"]] = job
        return self.snapshot(job)

    def _closure(self, job: dict) -> dict:
        refs = {
            "总装.SLDASM": ["零件.SLDPRT"],
            "零件.SLDPRT": ["deps/子零件.SLDPRT"],
            "deps/子零件.SLDPRT": [],
        }
        closure = {}
        pending = [job["main_assembly"]]
        while pending:
            name = pending.pop()
            if name in closure or name not in job["upload"]:
                continue
            closure[name] = job["upload"][name]
            pending.extend(refs.get(name, ()))
        return closure

    def _seal(self, job: dict) -> None:
        closure = self._closure(job)
        job["state"] = "sealed"
        job["status"] = "running"
        job["manifest"] = {
            "schema_version": "solidworks-to-urdf.selection/v1",
            "run_id": job["request"]["run_id"],
            "main_assembly": job["main_assembly"],
            "files": closure,
            "excluded_files": {name: checksum for name, checksum in job["upload"].items() if name not in closure},
            "upload_sha256": job["upload_sha256"],
            "handoff_sha256": digest(closure),
            "selection_sha256": "f" * 64,
        }

    def snapshot(self, job: dict) -> dict:
        if self.sealed_without_manifest and job["state"] == "sealed":
            job["manifest"] = None
        if self.sealed_staged_short and job["state"] == "sealed" and job["staged"]:
            job["staged"] = dict(list(job["staged"].items())[:1])
        block = {
            "state": job["state"],
            "reader_sha256": READER_SHA256,
            "upload_sha256": job["upload_sha256"],
            "staged_files": dict(job["staged"]),
        }
        if job["manifest"] is not None:
            block["manifest"] = job["manifest"]
        if self.extra_block_key:
            block["round_id"] = "0" * 64
        snapshot = {
            "schema_version": JOB_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": job["request"]["run_id"],
            "status": job["status"],
            "events": [],
            "result": None,
            "request": dict(job["request"]),
            "native_tool": {"source_sha256": READER_SHA256, "role": "native"},
            "selection": block,
        }
        if job.get("error"):
            snapshot["error"] = job["error"]
        return snapshot

    def stage(self, run_id: str, members: dict[str, bytes]) -> dict:
        job = self.jobs[run_id]
        if set(members) != set(job["upload"]):
            raise ValueError("staging archive does not match the upload index")
        for name, blob in members.items():
            if hashlib.sha256(blob).hexdigest() != job["upload"][name]:
                raise ValueError(f"staged bytes differ from the upload index: {name}")
        if job["staged"] == job["upload"]:
            return self.snapshot(job)  # logical retry: same members and hashes, no second run
        job["staged"] = dict(job["upload"])
        job["state"] = "reading"
        job["reading_gets"] = 0
        self.closure_runs += 1
        if self.failed_closure:
            job["status"] = "failed"
            job["error"] = {
                "error": "a resolved reference lies outside the staged root",
                "error_code": "selection_reference_outside_stage",
                "detail": {
                    "stored": "C:\\Users\\Mi\\Desktop\\M3.0\\step\\HD-1910-C001-20260902(2).SLDASM",
                    "resolved": "D:\\archive\\HD-1910-C001-20260902(2).SLDASM",
                },
            }
        return self.snapshot(job)

    def get(self, run_id: str) -> dict:
        job = self.jobs[run_id]
        if job["state"] == "reading" and job["status"] != "failed":
            job["reading_gets"] += 1
            if job["reading_gets"] % 2 == 0:
                self._seal(job)
        return self.snapshot(job)


class _Conflict(Exception):
    pass


def _handler(state: _FakeStaging):
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

        def _zip_members(self, data: bytes) -> dict[str, bytes]:
            members = {}
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for name in archive.namelist():
                    members[name] = archive.read(name)
            return members

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                if self.path == "/v1/jobs":
                    self._reply(201, state.create(json.loads(self._body())))
                    return
                parts = self.path.strip("/").split("/")
                run_id = parts[2] if len(parts) > 2 else ""
                if len(parts) == 4 and parts[3] == "files":
                    if self.headers.get("X-Selection-Round") is not None:
                        raise _Conflict("staging carries no round binding")
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/zip":
                        raise _Conflict("staging body must be a zip")
                    members = self._zip_members(self._body())
                    self._reply(202, state.stage(run_id, members))
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


class FolderStagingTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _FakeStaging()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.endpoint = WindowsEndpoint(
            EndpointConfig(base_url=f"http://127.0.0.1:{self.server.server_port}", token="t")
        )
        self.tmp = Path(tempfile.mkdtemp(prefix="folder-staging-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.intake = _intake(self.tmp)
        self.upload = package_inventory(self.intake)
        self.run_id = "1b3faa2a-0000-4000-8000-000000000001"
        self.batch_dir = self.tmp / "staging"

    def _start(self, **overrides) -> dict:
        payload = {"run_id": self.run_id, "upload_files": self.upload, "main_assembly": "总装.SLDASM"}
        payload.update(overrides)
        return self.endpoint.start_staging_job(**payload)

    def _drive(self) -> dict:
        return drive_staging(
            self.endpoint,
            run_id=self.run_id,
            upload_files=self.upload,
            main_assembly="总装.SLDASM",
            intake_dir=self.intake,
            batch_dir=self.batch_dir,
        )

    def test_folder_staging_reaches_the_sealed_closure(self) -> None:
        self._start()
        actions = []
        outcome = {}
        for _ in range(6):
            outcome = self._drive()
            actions.append(outcome["action"])
            if outcome["action"] == "sealed":
                break
        self.assertEqual(outcome["action"], "sealed")
        self.assertIn("staged", actions)
        manifest = outcome["manifest"]
        self.assertEqual(set(manifest["files"]), {"总装.SLDASM", "零件.SLDPRT", "deps/子零件.SLDPRT"})
        self.assertEqual(manifest["upload_sha256"], digest(self.upload))
        self.assertEqual(manifest["handoff_sha256"], digest(manifest["files"]))
        self.assertEqual(set(manifest["excluded_files"]), {"notes.txt", "decoy.SLDPRT"})
        job = self.state.jobs[self.run_id]
        self.assertEqual(job["staged"], self.upload)  # the full folder stays staged
        self.assertEqual(self.state.closure_runs, 1)

    def test_staging_retry_is_logically_idempotent(self) -> None:
        self._start()
        self.assertEqual(self._drive()["action"], "staged")
        archive = self.batch_dir / "probe.zip"
        prepare_staging_archive(self.intake, archive, self.upload)
        first = self.endpoint.stage_folder_archive(self.run_id, archive, upload_files=self.upload)
        second = self.endpoint.stage_folder_archive(self.run_id, archive, upload_files=self.upload)
        self.assertEqual(first["selection"]["staged_files"], self.upload)
        self.assertEqual(second["selection"]["staged_files"], self.upload)
        self.assertEqual(self.state.closure_runs, 1)  # no second native closure run

    def test_wrong_member_set_or_bytes_are_refused(self) -> None:
        self._start()
        wrong = self.tmp / "wrong.zip"
        with zipfile.ZipFile(wrong, "w") as archive:
            archive.writestr("总装.SLDASM", b"assembly bytes")
        with self.assertRaises(EndpointError):
            self.endpoint.stage_folder_archive(self.run_id, wrong, upload_files=self.upload)
        (self.intake / "零件.SLDPRT").write_bytes(b"tampered after upload")
        rewritten = self.tmp / "rewritten.zip"
        with zipfile.ZipFile(rewritten, "w") as archive:
            archive.writestr("总装.SLDASM", (self.intake / "总装.SLDASM").read_bytes())
            archive.writestr("零件.SLDPRT", b"tampered after upload")
            archive.writestr("deps/子零件.SLDPRT", (self.intake / "deps" / "子零件.SLDPRT").read_bytes())
            archive.writestr("notes.txt", (self.intake / "notes.txt").read_bytes())
            archive.writestr("decoy.SLDPRT", (self.intake / "decoy.SLDPRT").read_bytes())
        with self.assertRaises(EndpointError):
            self.endpoint.stage_folder_archive(self.run_id, rewritten, upload_files=self.upload)
        self.assertEqual(self.state.jobs[self.run_id]["state"], "awaiting_files")

    def test_snapshot_contract_is_exact(self) -> None:
        self.state.extra_block_key = True
        with self.assertRaises(EndpointProtocolError) as caught:
            self._start()
        self.assertIn("unexpected fields", str(caught.exception))
        self.state.extra_block_key = False

    def test_sealed_blocks_must_be_complete_and_full(self) -> None:
        self._start()
        self.assertEqual(self._drive()["action"], "staged")
        self.state.sealed_without_manifest = True
        with self.assertRaises(EndpointProtocolError) as caught:
            for _ in range(6):
                if self._drive()["action"] == "sealed":
                    break
        self.assertIn("sealed selection must carry its manifest", str(caught.exception))
        self.state.sealed_without_manifest = False
        self.state.sealed_staged_short = True
        self.run_id = "1b3faa2a-0000-4000-8000-00000000000a"
        self._start()
        self.assertEqual(self._drive()["action"], "staged")
        with self.assertRaises(EndpointProtocolError) as caught:
            for _ in range(6):
                if self._drive()["action"] == "sealed":
                    break
        self.assertIn("full upload index", str(caught.exception))

    def test_linked_rerun_inherits_the_sealed_closure(self) -> None:
        self._start()
        for _ in range(6):
            if self._drive()["action"] == "sealed":
                break
        child = "2b3faa2a-0000-4000-8000-000000000002"
        response = self.endpoint.start_staging_job(
            run_id=child,
            upload_files=self.upload,
            main_assembly="总装.SLDASM",
            resume={"parent_run": self.run_id, "from_stage": "capture"},
        )
        self.assertEqual(response["selection"]["state"], "sealed")
        self.assertEqual(
            set(response["selection"]["manifest"]["files"]),
            {"总装.SLDASM", "零件.SLDPRT", "deps/子零件.SLDPRT"},
        )

    def test_linked_rerun_refuses_a_foreign_closure(self) -> None:
        self._start()
        for _ in range(6):
            if self._drive()["action"] == "sealed":
                break
        self.state.linked_foreign_main = True
        with self.assertRaises(EndpointProtocolError) as caught:
            self.endpoint.start_staging_job(
                run_id="2b3faa2a-0000-4000-8000-000000000003",
                upload_files=self.upload,
                main_assembly="总装.SLDASM",
                resume={"parent_run": self.run_id, "from_stage": "capture"},
            )
        self.assertIn("different main assembly", str(caught.exception))
        self.state.linked_foreign_main = False
        self.state.linked_outside_file = True
        with self.assertRaises(EndpointProtocolError) as caught:
            self.endpoint.start_staging_job(
                run_id="2b3faa2a-0000-4000-8000-000000000004",
                upload_files=self.upload,
                main_assembly="总装.SLDASM",
                resume={"parent_run": self.run_id, "from_stage": "capture"},
            )
        self.assertIn("outside the upload index", str(caught.exception))

    def test_external_reference_failure_is_a_structured_job_failure(self) -> None:
        self.state.failed_closure = True
        self._start()
        outcome = self._drive()
        self.assertEqual(outcome["action"], "staged")
        job = self.endpoint.get_job(self.run_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["error_code"], "selection_reference_outside_stage")
        self.assertIn("resolved", job["error"]["detail"])
        self.assertIn("stored", job["error"]["detail"])


if __name__ == "__main__":
    unittest.main()
