"""Endpoint contract: the full indexed folder stages once and the native closure seals once.

Written against the locked staging workflow (``Jobs`` with ``selection_reader_factory``
returning closure evidence and the single ``POST /v1/jobs/<id>/files`` route).  The
staging worker resolves and seals the closure server-side; only then may native
discovery run.  No CAD, no Windows and no network beyond an ephemeral loopback server.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import unittest
import uuid
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from description_pipeline import interface_records as ir
from description_pipeline.io import digest, file_digest, read_data, write_json
from description_pipeline.orchestration import windows as windows_module
from description_pipeline.orchestration.windows import Jobs, RequestError, handler, provision_install_id, read_config
from description_pipeline.schemas import TOOL_SCHEMA
from description_pipeline.sources.solidworks.selection import SelectionError
from tests.sources.selection_support import closure_evidence
from tests.v1.test_interface_records import install_record, release as release_identity

MAIN = "assembly.SLDASM"
FRAME = "parts/frame.SLDASM"
LINK = "parts/link.SLDPRT"
TOOL_SHA = "f" * 64
SENTINEL = "native discovery must not run before the closure seals"


class SentinelNativeReader:
    """Selection-only tests: successful discovery after the seal is a contract sentinel."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, frozen, settings, on_activity=None):
        self.calls.append({"frozen": str(frozen), "settings": settings})
        raise RuntimeError(SENTINEL)


class StagingReader:
    """Same-thread staging reader returning the shared closure evidence."""

    def __init__(self, edges, log, *, refuse=False, transform=None, wait=None):
        self.edges = edges
        self.log = log
        self.refuse = refuse
        self.transform = transform
        self.wait = wait

    def __enter__(self):
        self.log.append(("enter", threading.get_ident()))
        return self

    def __exit__(self, *exc_info):
        self.log.append(("exit", threading.get_ident()))
        return False

    def observe(self, stage, main_assembly, upload_files):
        # The native reader owns its session inside one observe call.
        self.log.append(("enter", threading.get_ident()))
        try:
            self.log.append(("observe", threading.get_ident(), str(stage), main_assembly))
            if self.wait is not None:
                self.wait.wait(10)
            if self.refuse:
                raise SelectionError(
                    "selection_dependencies_refused",
                    "Stored references cannot be admitted into the staged closure",
                    problems=[
                        {
                            "document": MAIN,
                            "name": "link",
                            "location": "C:\\elsewhere\\link.SLDPRT",
                            "problem": "outside_stage",
                        }
                    ],
                    count=1,
                )
            evidence = closure_evidence(
                upload_files,
                main_assembly,
                reader_sha256=TOOL_SHA,
                stage_root=str(stage),
                edges=self.edges,
            )
            if self.transform is not None:
                evidence = self.transform(evidence)
            return evidence
        finally:
            self.log.append(("exit", threading.get_ident()))


class SelectionEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        #: The deployment pins the one canonical CAD domain; this fixture's
        #: state root is that pin, so endpoint suites never resolve an unbound
        #: host (and never invent a domain of their own).
        pin = patch.dict(
            os.environ, {"DESCRIPTION_CAD_OWNERSHIP_FILE": str(self.root / "state" / "cad-ownership.json")}
        )
        pin.start()
        self.addCleanup(pin.stop)
        self.packages = self.root / "packages"
        self.engineering = self.root / "engineering"
        for path in (self.packages, self.engineering, self.root / "outputs", self.root / "state"):
            path.mkdir(parents=True)
        token = self.root / "token"
        token.write_text("t" * 64, encoding="utf-8")
        config_path = self.root / "endpoint.json"
        write_json(
            config_path,
            {
                "schema_version": "solidworks-to-urdf.endpoint/v1",
                "package_root": str(self.packages),
                "output_root": str(self.root / "outputs"),
                "state_root": str(self.root / "state"),
                "token_file": str(token),
            },
        )
        self.config_path = config_path
        self.config = read_config(config_path)
        identity = patch(
            "description_pipeline.orchestration.windows._native_tool_record",
            return_value={
                "name": "native-selection-control",
                "version": "test",
                "source_sha256": TOOL_SHA,
                "runtime": {"role": "native", "python": "3.12.10", "packages": {}},
            },
        )
        identity.start()
        self.addCleanup(identity.stop)

        self.data = {
            MAIN: b"synthetic assembly bytes",
            FRAME: b"synthetic frame bytes",
            LINK: b"synthetic link bytes",
        }
        self.documents = {MAIN: [FRAME], FRAME: [LINK], LINK: []}
        self.upload_files = {name: hashlib.sha256(value).hexdigest() for name, value in self.data.items()}
        self.upload_sha = digest(self.upload_files)
        self.reader_log: list = []
        self.sentinel = SentinelNativeReader()
        self.runner_calls: list = []
        self.refusing = False
        self.transform = None
        self.reader_wait = None
        self._current_jobs: Jobs | None = None
        self.jobs = self.new_jobs()

    def new_jobs(self, *, native_reader=None) -> Jobs:
        previous = getattr(self, "_current_jobs", None)
        if previous is not None:
            previous.close()

        def runner(*args, **kwargs):
            self.runner_calls.append(kwargs.get("run_id"))
            return {"passed": False, "error": "The selection-only contract must not reach the runner"}

        jobs = Jobs(
            self.config,
            runner=runner,
            selection_reader_factory=lambda: StagingReader(
                self.documents,
                self.reader_log,
                refuse=self.refusing,
                transform=self.transform,
                wait=self.reader_wait,
            ),
            native_reader=native_reader or self.sentinel,
        )
        self.addCleanup(jobs.close)
        self._current_jobs = jobs
        return jobs

    # -- helpers -------------------------------------------------------------

    def request(self, run_id: str) -> dict:
        return {
            "run_id": run_id,
            "upload_files": self.upload_files,
            "upload_sha256": self.upload_sha,
            "main_assembly": MAIN,
        }

    def create(self, jobs: Jobs | None = None, run_id: str | None = None) -> str:
        identifier = run_id or str(uuid.uuid4())
        snapshot, created = (jobs or self.jobs).create(self.request(identifier))
        self.assertTrue(created)
        self.assertEqual(snapshot["run_id"], identifier)
        return identifier

    def archive(self, names=None) -> Path:
        path = self.root / f"upload-{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
            for name in names if names is not None else sorted(self.data):
                output.writestr(name, self.data[name])
        return path

    def stage(self, jobs: Jobs, identifier: str) -> dict:
        jobs.submit_files(identifier, self.archive())
        return self.await_failed_or_running(jobs, identifier)

    def await_failed_or_running(self, jobs: Jobs, identifier: str, *, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            snapshot = jobs.snapshot(identifier)
            if snapshot["status"] in {"failed", "native_complete", "passed"}:
                return snapshot
            if snapshot["status"] == "running" and self.reader_wait is None:
                return snapshot
            if time.monotonic() >= deadline:
                return snapshot
            time.sleep(0.02)

    def wait_failed(self, jobs: Jobs, identifier: str, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            snapshot = jobs.snapshot(identifier)
            if snapshot["status"] == "failed":
                return snapshot
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out waiting for the refusal; last status {snapshot['status']!r}")
            time.sleep(0.02)

    def await_report(self, jobs: Jobs, identifier: str, snapshot: dict, *, timeout: float = 5.0) -> dict:
        """A terminal run must finish sealing its failure diagnostics.

        The worker records the terminal status first and seals afterwards (the
        sealed receipt must include the final failure event and completion
        timestamp), so a snapshot taken between the two is transient by design:
        poll until the seal is visible instead of asserting on the first sight
        of ``failed``.
        """

        deadline = time.monotonic() + timeout
        while snapshot.get("failure_diagnostics") is None:
            if time.monotonic() >= deadline:
                self.assertIsNotNone(snapshot.get("failure_diagnostics"))
            time.sleep(0.02)
            snapshot = jobs.snapshot(identifier)
        return snapshot

    def observe_events(self) -> list:
        return [entry for entry in self.reader_log if entry[0] == "observe"]

    def observed_stage_root(self) -> str:
        return str(self.observe_events()[-1][2])

    def expected_manifest(self) -> dict:
        from tests.sources.selection_support import sealed_selection

        evidence = closure_evidence(
            self.upload_files,
            MAIN,
            reader_sha256=TOOL_SHA,
            stage_root=self.observed_stage_root(),
            edges=self.documents,
        )
        if self.transform is not None:
            evidence = self.transform(evidence)
        return sealed_selection(
            self.upload_files,
            MAIN,
            reader_sha256=TOOL_SHA,
            stage_root=self.observed_stage_root(),
            edges=self.documents,
        )

    # -- synchronous contract -------------------------------------------------

    def test_initial_stage_awaits_one_full_folder_upload(self) -> None:
        identifier = self.create()
        snapshot = self.jobs.snapshot(identifier)
        self.assertEqual(snapshot["status"], "selecting")
        self.assertEqual(
            set(snapshot["selection"]),
            {"state", "reader_sha256", "upload_sha256", "staged_files"},
        )
        self.assertEqual(snapshot["selection"]["state"], "awaiting_files")
        self.assertEqual(snapshot["selection"]["staged_files"], {})
        self.assertEqual(snapshot["selection"]["upload_sha256"], self.upload_sha)
        self.assertEqual(snapshot["selection"]["reader_sha256"], TOOL_SHA)
        # No native work runs before any staged bytes arrive.
        self.assertEqual(self.observe_events(), [])
        self.assertEqual(self.sentinel.calls, [])
        self.assertEqual(self.jobs.queue.qsize(), 0)

    def test_incomplete_extra_invalid_and_changed_batches_are_refused(self) -> None:
        identifier = self.create()

        extra = self.root / "extra-member.zip"
        with zipfile.ZipFile(extra, "w", zipfile.ZIP_DEFLATED) as output:
            for name in sorted(self.data):
                output.writestr(name, self.data[name])
            output.writestr("decoy.SLDPRT", b"unrelated native document")
        partial = self.archive([FRAME])  # missing the assembly
        invalid = self.root / "not-a-zip.zip"
        invalid.write_bytes(b"not a zip archive")
        wrong = self.root / "wrong-hash.zip"
        with zipfile.ZipFile(wrong, "w") as output:
            for name in sorted(self.data):
                output.writestr(name, b"different bytes" if name == MAIN else self.data[name])
        cases = {
            "selection_batch_inventory": [extra, partial],
            "selection_batch_invalid": [invalid],
            "selection_source_changed": [wrong],
        }
        for expected_code, archives in cases.items():
            for archive in archives:
                with self.subTest(code=expected_code, archive=archive.name):
                    with self.assertRaises(SelectionError) as caught:
                        self.jobs.submit_files(identifier, archive)
                    self.assertEqual(caught.exception.code, expected_code)

        snapshot = self.jobs.snapshot(identifier)
        self.assertEqual(snapshot["selection"]["state"], "awaiting_files")
        self.assertEqual(snapshot["selection"]["staged_files"], {})
        self.assertEqual(self.observe_events(), [])
        self.assertEqual(self.jobs.queue.qsize(), 0)

    def test_close_and_restart_fail_unfinished_states_without_replay(self) -> None:
        identifier = self.create()
        self.jobs.close()
        restarted = self.new_jobs()
        snapshot = restarted.snapshot(identifier)
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("restart", snapshot["error"].lower())
        self.assertIsNotNone(snapshot.get("failure_diagnostics"))
        self.assertEqual(self.observe_events(), [])
        self.assertEqual(self.sentinel.calls, [])
        with self.assertRaises(RequestError) as caught:
            restarted.submit_files(identifier, self.archive())
        self.assertEqual(caught.exception.status, 409)

    # -- stage once, seal once, then discovery --------------------------------

    def test_staging_seals_the_closure_once_and_only_then_runs_discovery(self) -> None:
        identifier = self.create()
        self.jobs.submit_files(identifier, self.archive())
        snapshot = self.wait_failed(self.jobs, identifier)
        snapshot = self.await_report(self.jobs, identifier, snapshot)
        self.assertEqual(len(self.observe_events()), 1)  # one staging observation, no rounds
        self.assertEqual(self.sentinel.calls and True, True)
        self.assertIn(SENTINEL, snapshot["error"])
        self.assertEqual(self.runner_calls, [])
        self.assertEqual(snapshot["selection"]["state"], "sealed")
        self.assertEqual(snapshot["selection"]["staged_files"], self.upload_files)
        self.assertEqual(snapshot["selection"]["manifest"], self.expected_manifest())
        receipt = snapshot.get("failure_diagnostics") or {}
        self.assertEqual(set(receipt), {"path", "sha256"})
        self.assertEqual(receipt["path"], "diagnostic.json")
        root = self.root / "state" / "diagnostics" / identifier
        manifest_path = root / "diagnostic.json"
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(file_digest(manifest_path), receipt["sha256"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "solidworks-to-urdf.failure-diagnostics/v1")
        self.assertEqual(manifest["run_id"], identifier)
        self.assertEqual(file_digest(root / "receipt.json"), manifest["receipt"]["sha256"])
        for relative, checksum in manifest["files"].items():
            self.assertEqual(file_digest(root / "evidence" / relative), checksum)
        # The native host stores raw diagnostics only: no report is rendered here.
        self.assertFalse((self.root / "state" / "structural_reports").exists())

    def test_identical_staging_retry_does_not_requery(self) -> None:
        self.reader_wait = threading.Event()
        identifier = self.create()
        archive = self.archive()
        self.jobs.submit_files(identifier, archive)
        deadline = time.monotonic() + 8
        while not self.observe_events() and time.monotonic() < deadline:
            time.sleep(0.02)
        observed = len(self.observe_events())
        self.assertEqual(observed, 1)
        retry = self.jobs.submit_files(identifier, archive)
        self.assertEqual(retry["selection"]["staged_files"], self.upload_files)
        self.assertIn(retry["selection"]["state"], {"reading", "sealed"})
        self.assertEqual(len(self.observe_events()), observed)
        self.reader_wait.set()
        self.wait_failed(self.jobs, identifier)

    def test_reader_refusal_fails_the_run_with_the_report_digest(self) -> None:
        self.refusing = True
        jobs = self.new_jobs()
        identifier = self.create(jobs)
        jobs.submit_files(identifier, self.archive())
        snapshot = self.wait_failed(jobs, identifier)
        snapshot = self.await_report(jobs, identifier, snapshot)
        self.assertEqual(snapshot.get("error_code"), "selection_dependencies_refused")
        self.assertEqual(snapshot["detail"]["problems"][0]["location"], "C:\\elsewhere\\link.SLDPRT")
        self.assertEqual(snapshot["detail"]["problems"][0]["problem"], "outside_stage")
        self.assertEqual(snapshot["selection"]["state"], "reading")
        self.assertEqual(self.sentinel.calls, [])
        receipt = snapshot.get("failure_diagnostics") or {}
        self.assertEqual(set(receipt), {"path", "sha256"})
        root = self.root / "state" / "diagnostics" / identifier
        self.assertEqual(file_digest(root / "diagnostic.json"), receipt["sha256"])
        manifest = json.loads((root / "diagnostic.json").read_text(encoding="utf-8"))
        self.assertEqual(file_digest(root / "receipt.json"), manifest["receipt"]["sha256"])

    def test_sealed_failure_diagnostics_render_on_linux(self) -> None:
        from description_pipeline.structural_report import build_structural_report

        identifier = self.create()
        self.jobs.submit_files(identifier, self.archive())
        snapshot = self.wait_failed(self.jobs, identifier)
        self.await_report(self.jobs, identifier, snapshot)
        root = self.root / "state" / "diagnostics" / identifier
        receipt = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
        report = build_structural_report(receipt, evidence_root=root / "evidence")
        self.assertEqual(report["summary"]["blockers"], 1)
        blocker = next(item for item in report["findings"] if item["source"] == "run/error")
        self.assertEqual(blocker["severity"], "blocker")
        self.assertTrue(str(blocker["object"] or "").strip())

    def test_failure_diagnostics_seal_exactly_once_after_finalization(self) -> None:
        from unittest.mock import patch as mock_patch

        identifier = self.create()
        original = type(self.jobs)._seal_failure_diagnostics
        sealed = []

        def spy(jobs_self, job):
            sealed.append(dict(job))
            return original(jobs_self, job)

        with mock_patch.object(type(self.jobs), "_seal_failure_diagnostics", spy):
            self.jobs.submit_files(identifier, self.archive())
            self.wait_failed(self.jobs, identifier)
        self.assertEqual(len(sealed), 1)
        self.assertEqual(sealed[0]["status"], "failed")
        self.assertIsInstance(sealed[0].get("completed_at"), str)
        self.assertTrue(sealed[0]["events"])
        self.assertEqual(sealed[0]["events"][-1]["state"], "failed")

    def test_closure_evidence_forgeries_are_refused(self) -> None:
        def drop_stage_root(evidence):
            evidence = copy.deepcopy(evidence)
            evidence.pop("stage_root", None)
            return evidence

        def outside_reference(evidence):
            evidence = copy.deepcopy(evidence)
            evidence["graph"][MAIN]["references"] = [*evidence["graph"][MAIN]["references"], "outside.SLDASM"]
            evidence["graph"]["outside.SLDPRT"] = {"sha256": "0" * 64, "references": []}
            return evidence

        def wrong_hash(evidence):
            evidence = copy.deepcopy(evidence)
            evidence["graph"][FRAME]["sha256"] = "0" * 64
            return evidence

        for forge, transform in (
            ("missing_stage_root", drop_stage_root),
            ("outside_reference", outside_reference),
            ("wrong_hash", wrong_hash),
        ):
            with self.subTest(forge=forge):
                self.reader_log.clear()
                self.transform = transform
                jobs = self.new_jobs()
                try:
                    identifier = self.create(jobs)
                    jobs.submit_files(identifier, self.archive())
                    snapshot = self.wait_failed(jobs, identifier)
                    self.assertEqual(snapshot.get("error_code"), "selection_closure_invalid")
                    self.assertEqual(self.sentinel.calls, [])
                    self.assertEqual(self.runner_calls, [])
                finally:
                    jobs.close()
                    self.transform = None
                    self._current_jobs = None

    def test_two_jobs_serialize_native_contexts(self) -> None:
        self.reader_wait = threading.Event()
        jobs = self.new_jobs(native_reader=lambda frozen, settings, on_activity=None: copy.deepcopy(self.records_ok()))
        first = self.create(jobs, run_id=str(uuid.uuid4()))
        second = self.create(jobs, run_id=str(uuid.uuid4()))
        jobs.submit_files(first, self.archive())
        deadline = time.monotonic() + 8
        while not self.observe_events() and time.monotonic() < deadline:
            time.sleep(0.02)
        jobs.submit_files(second, self.archive())
        time.sleep(0.2)
        observed = [entry[2] for entry in self.observe_events()]
        self.assertTrue(observed)
        self.assertEqual({entry[2] for entry in self.observe_events()}, {observed[0]})
        depth = 0
        for entry in self.reader_log:
            if entry[0] == "enter":
                depth += 1
            elif entry[0] == "exit":
                depth -= 1
            self.assertGreaterEqual(depth, 0)
            self.assertLessEqual(depth, 1, "native reader contexts overlapped across jobs")
        self.reader_wait.set()
        deadline = time.monotonic() + 8
        while len(self.observe_events()) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len({entry[2] for entry in self.observe_events()}), 2)

    def records_ok(self) -> dict:
        from tests.sources.test_solidworks_discovery import record as native_record

        return native_record()

    # -- loopback HTTP adapter -------------------------------------------------

    def http_call(self, base: str, method: str, path: str, body=None, headers=None):
        request = Request(base + path, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - ephemeral loopback only
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_the_install_id_is_written_once_and_survives_restarts(self) -> None:
        first = provision_install_id(self.root / "state")
        self.assertTrue(first.startswith("win-"))
        self.assertEqual(provision_install_id(self.root / "state"), first)

    def test_the_documented_minimal_config_needs_no_targets(self) -> None:
        minimal = self.root / "minimal-endpoint.json"
        write_json(
            minimal,
            {
                "schema_version": "solidworks-to-urdf.endpoint/v1",
                "package_root": str(self.packages),
                "output_root": str(self.root / "outputs"),
                "state_root": str(self.root / "state"),
                "token_file": str(self.root / "token"),
            },
        )
        config = read_config(minimal)
        self.assertNotIn("targets", config)
        self.assertEqual(config["config_digest"], digest(read_data(minimal)))

    def test_the_health_producer_satisfies_the_real_consumer(self) -> None:
        triple = release_identity()
        installed = {
            "schema_version": TOOL_SCHEMA,
            "pipeline_id": "solidworks-to-urdf",
            "version": triple["version"],
            "source_sha256": triple["source_sha256"],
            "release": {
                "schema_version": "solidworks-to-urdf.release/v1",
                "pipeline_id": "solidworks-to-urdf",
                "source_tree": "a" * 40,
                **triple,
            },
        }
        token = self.jobs.config["token"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.jobs, token))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        from description_pipeline.sources.solidworks import isolation

        quiet = {
            "owned_sessions": 0,
            "lease_held": False,
            "external_session_detected": False,
            "problems": [],
        }
        # Real normal-OS observation doubles: win32 platform, quiet CAD facts
        # and a passing native readiness probe.
        with (
            patch.object(windows_module, "tool_record", return_value=installed),
            patch("sys.platform", "win32"),
            patch("description_pipeline.runtime.native_readiness", return_value={"role": "native"}),
            patch.object(isolation, "registered_executable", return_value="C:/SW/SLDWORKS.exe"),
            patch.object(isolation, "cad_session_facts", return_value=quiet),
        ):
            status, body = self.http_call(base, "GET", "/health", headers={"Authorization": "Bearer " + token})
        self.assertEqual(status, 200, body)
        validated = ir.validate_health(body)  # the real consumer accepts the real producer document
        self.assertEqual(set(body), set(ir.HEALTH_KEYS))
        self.assertNotIn("targets", body)
        self.assertTrue(validated["ready"], body)
        self.assertEqual(body["release"]["role"], "native")
        self.assertEqual(body["install"]["config_digest"], digest(read_data(self.config_path)))
        self.assertEqual(body["install"]["install_id"], self.jobs.install_id)
        self.assertEqual(body["endpoint"]["port"], self.jobs.config["port"])
        record = install_record()
        record["ends"]["windows"]["install_id"] = body["install"]["install_id"]
        record["ends"]["windows"]["config_digest"] = body["install"]["config_digest"]
        ir.require_endpoint_matches_install_record(body, record)
        zeroed = {**body, "install": {**body["install"], "config_digest": "0" * 64}}
        with self.assertRaises(ir.InterfaceRecordError):
            ir.require_endpoint_matches_install_record(zeroed, record)
        # The owner gate refuses a readable-but-not-ready document; the ready
        # gate itself is never weakened for the unsupported platform.
        with patch.object(windows_module, "tool_record", return_value=installed):
            unready = windows_module.health_payload(self.jobs)
        self.assertFalse(unready["ready"])
        with self.assertRaises(ir.InterfaceRecordError) as raised:
            ir.require_endpoint_matches_install_record(unready, record)
        self.assertEqual(raised.exception.code, "endpoint_not_ready")

    def test_a_queue_without_its_thread_is_never_ready(self) -> None:
        triple = release_identity()
        installed = {
            "schema_version": TOOL_SCHEMA,
            "pipeline_id": "solidworks-to-urdf",
            "version": triple["version"],
            "source_sha256": triple["source_sha256"],
            "release": {
                "schema_version": "solidworks-to-urdf.release/v1",
                "pipeline_id": "solidworks-to-urdf",
                **triple,
            },
        }

        class _DeadThread:
            def is_alive(self):
                return False

        with (
            patch.object(windows_module, "tool_record", return_value=installed),
            patch.object(self.jobs, "thread", _DeadThread()),
        ):
            payload = windows_module.health_payload(self.jobs)
        self.assertIn("queue_thread_unavailable", payload["readiness"]["native"]["problems"])
        self.assertFalse(payload["ready"])

    def test_default_selection_reader_shares_the_endpoint_cad_ledger(self) -> None:
        jobs = self.jobs
        probe = {"native_tool": {"source_sha256": "0" * 64}}
        jobs.jobs["ledger-probe"] = probe
        pinned = self.root / "canonical-domain" / "cad-ownership.json"
        try:
            with patch.dict(os.environ, {"DESCRIPTION_CAD_OWNERSHIP_FILE": str(pinned)}):
                reader = jobs._default_selection_reader("ledger-probe")
        finally:
            del jobs.jobs["ledger-probe"]
        backend = reader._walker.__self__
        self.assertEqual(jobs.cad_ownership_file, pinned)
        self.assertNotEqual(pinned, jobs.config["state_root"] / "cad-ownership.json")
        self.assertEqual(backend._cad_ownership_file, jobs.cad_ownership_file)
        self.assertEqual(reader._reader_sha256, "0" * 64)

    def test_http_routes_stage_the_full_folder_once(self) -> None:
        token = self.jobs.config["token"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.jobs, token))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        auth = {"Authorization": "Bearer " + token}
        json_headers = {**auth, "Content-Type": "application/json"}

        status, body = self.http_call(base, "GET", "/health", headers=auth)
        # Dev trees carry no installed release identity: the stable 500 code.
        self.assertEqual(status, 500, body)
        self.assertEqual(body["error_code"], "health_identity_unavailable")

        anonymous = json.dumps(self.request(str(uuid.uuid4()))).encode()
        status, _body = self.http_call(base, "POST", "/v1/jobs", anonymous, {"Content-Type": "application/json"})
        self.assertEqual(status, 401)

        identifier = str(uuid.uuid4())
        status, body = self.http_call(
            base, "POST", "/v1/jobs", json.dumps(self.request(identifier)).encode(), json_headers
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["selection"]["state"], "awaiting_files")
        self.assertEqual(body["selection"]["staged_files"], {})

        status, _body = self.http_call(
            base, "POST", f"/v1/jobs/{identifier}/files", b"xxxx", {**auth, "Content-Type": "text/plain"}
        )
        self.assertEqual(status, 400)

        partial = self.archive([MAIN]).read_bytes()
        status, body = self.http_call(
            base, "POST", f"/v1/jobs/{identifier}/files", partial, {**auth, "Content-Type": "application/zip"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error_code"), "selection_batch_inventory")

        archive = self.archive().read_bytes()
        status, body = self.http_call(
            base, "POST", f"/v1/jobs/{identifier}/files", archive, {**auth, "Content-Type": "application/zip"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["selection"]["staged_files"], self.upload_files)

        status, body = self.http_call(
            base, "POST", f"/v1/jobs/{identifier}/files", archive, {**auth, "Content-Type": "application/zip"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["selection"]["staged_files"], self.upload_files)

        status, _body = self.http_call(base, "POST", f"/v1/jobs/{identifier}/seal", b"{}", json_headers)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
