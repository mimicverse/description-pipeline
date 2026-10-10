"""Synthetic native-boundary controls; these helpers cannot qualify a CAD model.

The endpoint contract is now: staged selection rounds -> sealed selection ->
raw native record -> derived definition -> native capture transfer.  These
helpers drive the *client* side of every leg with synthetic bytes and the real
discovery-transfer bundles, so endpoint tests never touch CAD, COM or a Windows
host.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import digest, file_digest, write_json
from description_pipeline.orchestration.discovery_transfer import (
    RAW_RECORD_ARCHIVE,
    derive_from_raw_bundle,
    seal_capture_request,
)
from description_pipeline.orchestration.windows import Jobs, read_config
from description_pipeline.sources.solidworks.discovery import DiscoverySettings
from tests.sources.test_solidworks_discovery import record as native_record
from .protocol_support import protocol_events

#: The neutral discovery fixture's exact file set and byte layout.
SOURCE_FILES = ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT")
MAIN = "cad/robot.SLDASM"
PART = "cad/base.SLDPRT"
ARM = "cad/arm.SLDPRT"
TOOL = {
    "schema_version": "solidworks-to-urdf.tool/v1",
    "pipeline_id": "test",
    "source_sha256": "ab" * 32,
}


def source_bytes() -> dict[str, bytes]:
    return {name: f"placeholder {name}\n".encode() for name in SOURCE_FILES}


class RefusingSelectionReader:
    """A same-thread selection reader that can refuse with structured evidence."""

    def __init__(self, documents, log, refusal=None):
        self.documents = documents
        self.log = log
        self.refusal = refusal

    def __enter__(self):
        self.log.append(("enter", threading.get_ident()))
        return self

    def __exit__(self, *exc_info):
        self.log.append(("exit", threading.get_ident()))
        return False

    def observe(self, stage, pending, upload_files):
        self.log.append(("observe", threading.get_ident(), Path(stage).name, tuple(sorted(pending))))
        if self.refusal is not None:
            raise self.refusal
        return {
            name: {"sha256": upload_files[name], "references": sorted(self.documents.get(name, []))}
            for name in pending
        }


class CaptureBackend:
    """Stand-in capture backend; the fake runner never touches CAD."""


class EndpointFixture:
    """One isolated endpoint state root plus synthetic selection/native fixtures."""

    def setUp(self):
        identity = patch(
            "description_pipeline.orchestration.windows._native_tool_record",
            return_value=copy.deepcopy(TOOL),
        )
        identity.start()
        self.addCleanup(identity.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packages = self.root / "packages"
        self.packages.mkdir()
        token = self.root / "token"
        token.write_text("t" * 64)
        self.path = self.root / "endpoint.json"
        self.config_data = {
            "schema_version": "solidworks-to-urdf.endpoint/v1",
            "package_root": str(self.packages),
            "output_root": str(self.root / "outputs"),
            "state_root": str(self.root / "state"),
            "token_file": str(token),
            "targets": {"nd_fixture": {"repository_slug": "a/b", "base": "feature/arm"}},
        }
        self.write_config()
        self.config = read_config(self.path)
        self.record = native_record()
        self.upload_files = {
            name: hashlib.sha256(payload).hexdigest() for name, payload in source_bytes().items()
        }
        self.upload_sha = digest(self.upload_files)
        self.documents = {MAIN: [PART, ARM], PART: [], ARM: []}
        self.reader_log: list = []
        self.selection_refusal = None
        #: A nonempty approved-name registry: the discovery contract must record
        #: its digest instead of digest({}).
        self.frozen_names = {"arm": "arm_link"}

    def write_config(self, **changes) -> None:
        write_json(self.path, {**self.config_data, **changes})

    def config_for(self, name: str, **changes) -> dict:
        """An independent config whose state/output roots cannot collide."""

        self.write_config(
            state_root=str(self.root / f"state-{name}"),
            output_root=str(self.root / f"outputs-{name}"),
            **changes,
        )
        return read_config(self.path)

    # -- requests, selection rounds, native record --------------------------

    def request(self, run_id: str | None = None, **extra) -> dict:
        return {
            "run_id": run_id or str(uuid.uuid4()),
            "upload_files": copy.deepcopy(self.upload_files),
            "upload_sha256": self.upload_sha,
            "main_assembly": MAIN,
            **extra,
        }

    def jobs(
        self,
        runner=None,
        *,
        config=None,
        selection_reader=None,
        native_reader=None,
        backend_factory=None,
    ) -> Jobs:
        def default_reader():
            return RefusingSelectionReader(
                self.documents, self.reader_log, refusal=self.selection_refusal
            )

        record = self.record
        jobs = Jobs(
            config or self.config,
            runner=runner
            or (
                lambda package, output, **kwargs: self.capture_transfer_result(
                    output, run_id=kwargs["run_id"], on_event=kwargs.get("on_event")
                )
            ),
            selection_reader_factory=selection_reader or default_reader,
            native_reader=native_reader
            or (lambda frozen, settings, on_activity=None: copy.deepcopy(record)),
            backend_factory=backend_factory or (lambda package, files: CaptureBackend()),
        )
        self.addCleanup(jobs.close)
        return jobs

    def start_round(self, jobs: Jobs, run_id: str, *, timeout: float = 15.0) -> str:
        snapshot = self.wait_state(jobs, run_id, {"awaiting_files"}, timeout=timeout)
        if snapshot["status"] == "failed":
            raise AssertionError(f"the native job failed during selection: {snapshot.get('error')!r}")
        round_id = snapshot["selection"]["round_id"]
        jobs.submit_files(run_id, self.batch_zip(snapshot["selection"]["requested_files"]), round_id)
        return round_id

    def wait_state(self, jobs: Jobs, run_id: str, states, *, round_id: str | None = None, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while True:
            snapshot = jobs.snapshot(run_id)
            if snapshot["status"] == "failed":
                return snapshot
            selection = snapshot.get("selection") or {}
            if selection.get("state") in states and (round_id is None or selection.get("round_id") != round_id):
                return snapshot
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"timed out waiting for {sorted(states)}; last state {selection.get('state')!r} "
                    f"(status {snapshot['status']!r})"
                )
            time.sleep(0.01)

    def selection_manifest(self, jobs: Jobs, run_id: str):
        from description_pipeline.sources.solidworks.selection import seal_selection

        observations = jobs.snapshot(run_id)["selection"]["observations"]
        selected = {name: self.upload_files[name] for name in self.documents}
        return seal_selection(
            self.upload_files,
            MAIN,
            observations,
            selected_files=selected,
            reader_sha256=TOOL["source_sha256"],
        )

    def seal_selection(self, jobs: Jobs, run_id: str) -> dict:
        """Drive every selection round, then seal with the client manifest."""

        for _ in range(4):
            previous = self.start_round(jobs, run_id)
            snapshot = self.wait_state(jobs, run_id, {"awaiting_files", "ready"}, round_id=previous)
            if snapshot["status"] == "failed":
                return snapshot
            if snapshot["selection"]["state"] == "ready":
                return jobs.seal_selection(run_id, {"selection": self.selection_manifest(jobs, run_id)})
        raise AssertionError("the selection never became ready")

    def batch_zip(self, names) -> Path:
        import zipfile

        payloads = source_bytes()
        path = self.root / f"batch-{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
            for name in names:
                output.writestr(name, payloads[name])
        return path

    # -- the definition leg (Linux side of the two-leg transfer) ------------

    def raw_record_archive(self, jobs: Jobs, run_id: str) -> Path:
        """The raw-record bundle the endpoint exposes to the Linux continuation."""

        candidate = jobs.config["state_root"] / "native_records" / run_id / RAW_RECORD_ARCHIVE
        if candidate.is_file():
            return candidate
        raise AssertionError(f"the raw-record bundle is missing: {candidate}")

    def complete_definition(self, jobs: Jobs, run_id: str, *, submit=True) -> Path:
        """Derive the prepared package from the sealed raw record and answer it."""

        snapshot = jobs.snapshot(run_id)
        if snapshot["status"] != "awaiting_definition":
            raise AssertionError(
                f"the run is not awaiting a definition: {snapshot['status']!r} {snapshot.get('error')!r}"
            )
        bundle = self.raw_record_archive(jobs, run_id)
        selection = snapshot["selection"]["manifest"]
        prepared = derive_from_raw_bundle(
            bundle,
            Path(jobs._source_dir(jobs.jobs[run_id])),
            self.root / f"prepared-{run_id}",
            expected_run_id=run_id,
            expected_selection=selection,
            expected_native_tool=TOOL,
            settings=DiscoverySettings(record_roots=[self.records_dir()], frozen_names=dict(self.frozen_names)),
        )
        archive = self.root / f"capture-request-{run_id}.zip"
        seal_capture_request(
            prepared,
            archive,
            record=self.record,
            selection=selection,
            native_tool=TOOL,
        )
        self.last_definition = {"archive": archive, "prepared_sha256": prepared.prepared_sha256}
        if submit:
            jobs.submit_definition(run_id, archive, prepared.prepared_sha256)
        return archive

    def records_dir(self) -> Path:
        """The derivation record roots the raw record's properties point at."""

        records = self.root / "records"
        joints = records / "joints" / "arm.json"
        joints.parent.mkdir(parents=True, exist_ok=True)
        joints.write_text(
            json.dumps({"limits": {"lower": -1.5, "upper": 1.5}, "drive": {"effort": 6.0, "velocity": 2.0}}),
            encoding="utf-8",
        )
        (records / "budget.json").write_text(
            json.dumps({"robot": {"expected_mass_kg": [0.28, 0.32], "expected_extent_m": [0.35, 0.5]}}),
            encoding="utf-8",
        )
        return records

    def run_to_terminal(self, jobs: Jobs, run_id: str, *, timeout: float = 20.0) -> dict:
        """The whole native boundary: selection -> definition -> capture."""

        sealed = self.seal_selection(jobs, run_id)
        if sealed["status"] == "failed":
            return sealed
        state = self.wait_state_any_status(jobs, run_id, {"awaiting_definition"}, timeout=timeout)
        if state["status"] != "awaiting_definition":
            return state
        self.complete_definition(jobs, run_id)
        return self.await_terminal(jobs, run_id, timeout=timeout)

    def wait_state_any_status(self, jobs: Jobs, run_id: str, statuses, *, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            snapshot = jobs.snapshot(run_id)
            if snapshot["status"] in statuses or snapshot["status"] in {"failed", "native_complete", "passed"}:
                return snapshot
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"timed out waiting for {sorted(statuses)}; last status {snapshot['status']!r}"
                )
            time.sleep(0.01)

    def await_terminal(self, jobs: Jobs, run_id: str, *, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = jobs.snapshot(run_id)
            if job["status"] in {"passed", "failed", "native_complete"}:
                return job
            time.sleep(0.01)
        raise AssertionError(f"{run_id} did not finish: {jobs.snapshot(run_id)['status']}")

    # -- capture-side fixtures (unchanged semantics) ------------------------

    def capture_transfer_result(self, output, *, run_id, on_event=None):
        """A sealed capture-transfer result: native_complete, never qualified success."""

        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        archive = output / "native-evidence.zip"
        manifest = output / "transfer-manifest.json"
        archive.write_bytes(b"PK\x03\x04synthetic-transfer")
        write_json(manifest, {"schema_version": "solidworks-to-urdf.transfer/v1", "run_id": run_id})
        if on_event is not None:
            for event in protocol_events(stages=("freeze", "discover", "capture")):
                on_event(event)
        return {
            "native_complete": True,
            "passed": False,
            "state": "native_complete",
            "run_id": run_id,
            "capture_archive": {
                "name": "native-evidence.zip",
                "sha256": file_digest(archive),
                "size": archive.stat().st_size,
                "manifest_sha256": file_digest(manifest),
            },
        }

    def passing_result(self, *, on_event=None):
        subject = "a" * 64
        if on_event is not None:
            for event in protocol_events(stages=("capture", "generate", "verify", "publish"), subject=subject):
                on_event(event)
        return {
            "passed": True,
            "subject_sha256": subject,
            "quality": {
                "passed": True,
                "subject_sha256": subject,
                "checks": [{"id": "source.native_discovery", "passed": True}],
            },
            "submission": {
                "passed": True,
                "subject_sha256": subject,
                "url": "https://github.com/a/b/pull/1",
                "repository_slug": "a/b",
                "base": "feature/arm",
                "branch": "work/solidworks/arm",
                "state": "published",
                "commit": "b" * 40,
            },
        }
