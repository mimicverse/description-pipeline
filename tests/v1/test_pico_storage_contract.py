"""Real CAS/result IO with explicit business-proof doubles; no native qualification."""

from copy import deepcopy
import asyncio
import hashlib
import json
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import zipfile
import tracemalloc
from unittest.mock import patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import canonical, digest
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.simulation.evidence import RecordingDiagnostic
from description_pipeline.simulation.recording import prepare_bound, recording_binding, session_identity
from description_pipeline.simulation.resources import SessionResourceError, directory_bytes
from description_pipeline.orchestration.report_view import text_warning_inventory
from description_pipeline.simulation.service import RunningSession, SimulationService
from tests.v1.test_pico_evidence import EvidenceFixture


class _StoreDouble:
    def __init__(self, row):
        self.row = row
        self.attempts = []

    def get(self, identifier):
        assert identifier == self.row["session_id"]
        return deepcopy(self.row)

    def worker_commands(self, _identifier):
        return []

    def acquire_verification_slot(self):
        class Slot:
            def close(self):
                pass

        return Slot()

    def active_session_ids(self):
        return [self.row["session_id"]] if self.row["state"] not in {"stopped", "failed"} else []

    def verification_attempts(self, _identifier=None):
        return deepcopy(self.attempts)

    def begin_verification(self, session_id, command_id, request, owner, *, slot=None):
        self.attempts.append(
            {
                "session_id": session_id,
                "command_id": command_id,
                "request": request,
                "result": {"state": "starting", "owner_nonce": owner, "process_identity": None},
            }
        )

    def transition_verification(self, session_id, command_id, owner, state, **options):
        item = next(row for row in self.attempts if row["command_id"] == command_id)
        item["result"].update(state=state, **options)


class PicoStorageContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sessions = self.root / "sessions"
        self.session = self.sessions / "software-session"
        self.fixture = EvidenceFixture(self.session)
        (self.session / "source").mkdir()
        (self.session / "source/model.xml").write_bytes(b"x" * 32)
        (self.session / "worker-stderr.log").write_bytes(b"Software fixture stderr\n")
        self.runtime = {"scope": "design-only fixture"}
        request = {"runtime": self.runtime, "source": {}, "record": True}
        self.row = {
            "session_id": self.session.name,
            "actor": "fixture-operator",
            "source_run_id": "fixture-source",
            "request": request,
            "request_digest": digest(request),
            "state": "stopping",
            "error": None,
            "recording_artifact": None,
            "revision": 0,
        }
        self.store = _StoreDouble(self.row)
        recording = self.session / "recording"
        (recording / "request.json").write_bytes(canonical(request) + b"\n")
        (recording / "session.json").write_bytes(canonical(session_identity(self.row)) + b"\n")
        self.fixture.finalization_fixture()
        self.fixture.manifest.update({"status": "stopped", "exit_code": 0, "reason": "normal_stop"})
        self.fixture.finalization_fixture()
        self.artifacts = ArtifactStore(self.root / "artifacts")
        self.service = SimulationService(self.store, self.artifacts, self.sessions, Path(sys.executable))
        self.service.running[self.session.name] = RunningSession(
            None,
            self.session,
            expected_policy=self.fixture.manifest["resource_policy"],
        )
        self.report = {
            "runtime": self.runtime,
            "manifest_sha256": hashlib.sha256((self.fixture.native / "session-manifest.json").read_bytes()).hexdigest(),
            "manifest": self.fixture.manifest,
            "container_complete": True,
            "business_complete": True,
            "finalization_verification": self.fixture.finalization_result_double(),
            "streams": {},
            "counts": self.fixture.manifest["counts"],
            "error": None,
        }

        async def proof_double(_execution, **_options):
            return deepcopy(self.report)

        self.service._native_probe = proof_double

    def snapshot(self):
        return {
            path.relative_to(self.session).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.session.rglob("*")
            if path.is_file()
        }

    async def seal(self, save=True):
        return await self.service._seal_recording(self.session, True, None, runtime=self.runtime, save=save)

    def outcome(self, record):
        ref = ArtifactRef.from_record(record)
        self.artifacts.verify(ref)
        return json.loads((self.artifacts.objects / ref.artifact_id / "outcome.json").read_bytes())

    async def test_successful_result_is_external_and_cas_keeps_every_original_byte(self):
        before = self.snapshot()
        record = await self.seal()
        outcome = self.outcome(record)
        self.assertTrue(outcome["complete"] and outcome["session_complete"])
        self.assertFalse(outcome["formal_one_hour_qualified"])
        self.assertEqual(self.report["finalization_verification"], outcome["finalization_verification"])
        self.assertFalse((self.session / "recording/outcome.json").exists())
        self.assertEqual(before, self.snapshot())
        ref = ArtifactRef.from_record(record)
        self.assertEqual(
            (self.session / "worker-stderr.log").read_bytes(),
            (self.artifacts.objects / ref.artifact_id / "worker-stderr.log").read_bytes(),
        )

    async def test_archive_stream_reads_verified_cas_without_disk_copies_and_closes_on_cancellation(self):
        export = self.root / "export-input"
        export.mkdir()
        (export / "payload.bin").write_bytes(b"Original archive bytes\n" * 100000)
        ref = self.artifacts.seal_tree(
            export, kind="teleop_recording", producer_run_id=None, producer_stage=None, producer_tool_sha256="c" * 64
        )
        cas = self.artifacts.objects / ref.artifact_id

        def snapshot():
            return {
                p.relative_to(self.root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
                for p in self.root.rglob("*")
                if p.is_file()
            }

        before = snapshot()
        with patch.object(self.artifacts, "materialize", side_effect=AssertionError("Export must not copy CAS")):
            stream = self.service.recording_archive(ref)
            tracemalloc.start()
            try:
                for _chunk in stream:
                    pass
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
                stream.close()
            self.assertLess(peak, 1024 * 1024)
            archive_bytes = b"".join(self.service.recording_archive(ref))
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                self.assertEqual({"manifest.json", "payload.bin"}, set(archive.namelist()))
                for name in archive.namelist():
                    self.assertEqual((cas / name).read_bytes(), archive.read(name))
                    self.assertEqual(zipfile.ZIP_STORED, archive.getinfo(name).compress_type)

            def readers():
                paths = []
                for fd in Path("/proc/self/fd").iterdir():
                    try:
                        target = os.readlink(fd)
                    except OSError:
                        continue
                    if target.startswith(str(cas) + "/"):
                        paths.append(target)
                return paths

            stream = self.service.recording_archive(ref)
            next(stream)
            self.assertTrue(readers())
            stream.close()
            self.assertEqual([], readers())
        self.assertEqual(before, snapshot())

    async def test_whole_directory_overquota_is_diagnostic_not_success_or_discard(self):
        padding = self.session / "source/padding.bin"
        with padding.open("xb") as stream:
            stream.truncate(536870913 - directory_bytes(self.session))
        with self.assertRaises(RecordingDiagnostic) as caught:
            await self.seal()
        outcome = self.outcome(caught.exception.recording)
        self.assertFalse(outcome["complete"] or outcome["session_complete"])
        self.assertEqual(536870913, directory_bytes(self.session))
        self.assertTrue((self.fixture.native / "records.jsonl.gz").exists())

    async def test_incomplete_diagnostic_is_preserved_and_rejected_by_the_binding_entry(self):
        self.report.update({"container_complete": False, "business_complete": False, "error": "Missing gzip footer"})
        before = self.snapshot()
        with self.assertRaises(RecordingDiagnostic) as caught:
            await self.seal()
        record = caught.exception.recording
        self.assertFalse(self.outcome(record)["container_complete"])
        self.assertEqual(before, self.snapshot())
        row = {**self.row, "state": "stopped", "recording_artifact": record}
        with self.assertRaisesRegex(ValueError, "Diagnostic recording"):
            prepare_bound(recording_binding(row), self.artifacts, self.session / "source", commands=[], execution={})

    async def test_unsaved_incomplete_session_retains_native_evidence_and_external_outcome(self):
        self.report.update({"container_complete": False, "business_complete": False, "error": "Missing gzip footer"})
        before = self.snapshot()
        with self.assertRaises(RecordingDiagnostic) as caught:
            await self.seal(save=False)
        self.assertIsNone(caught.exception.recording)
        self.assertEqual(before, self.snapshot())
        outcomes = list(self.artifacts.root.glob("session-result-*/recording/outcome.json"))
        self.assertEqual(1, len(outcomes))
        self.assertFalse(json.loads(outcomes[0].read_bytes())["session_complete"])

    async def test_native_finalization_failure_or_false_byte_proof_retains_the_original_session(self):
        original = deepcopy(self.report["finalization_verification"])
        for change in (
            {"pass": False, "detail": "Native finalization rejected"},
            {"observed_bytes": original["observed_bytes"] - 1},
        ):
            self.report["finalization_verification"] = {**original, **change}
            before = self.snapshot()
            with self.subTest(change=change), self.assertRaises(RecordingDiagnostic) as caught:
                await self.seal(save=False)
            self.assertIsNone(caught.exception.recording)
            self.assertEqual(before, self.snapshot())

    async def test_unsaved_success_discards_only_after_complete_verification(self):
        self.assertIsNone(await self.seal(save=False))
        self.assertFalse((self.session / "recording").exists())
        self.assertTrue((self.session / "source/model.xml").exists())
        self.assertEqual([], list(self.artifacts.root.glob("session-result-*")))

    async def test_storage_refusal_retains_all_original_bytes_without_an_artifact(self):
        before = self.snapshot()
        with (
            patch.object(self.service, "_admit_storage", side_effect=SessionResourceError("Storage refused")),
            self.assertRaises(RecordingDiagnostic) as caught,
        ):
            await self.seal()
        self.assertIsNone(caught.exception.recording)
        self.assertEqual(before, self.snapshot())
        self.assertEqual([], list(self.artifacts.objects.iterdir()))

    @unittest.skipIf(os.name == "posix" and os.geteuid() == 0, "Requires actual directory permission enforcement")
    async def test_cleanup_failure_keeps_a_complete_external_snapshot(self):
        original = {
            path.relative_to(self.session / "recording").as_posix(): path.read_bytes()
            for path in (self.session / "recording").rglob("*")
            if path.is_file()
        }
        self.fixture.native.chmod(0o500)
        try:
            with self.assertRaisesRegex(RecordingDiagnostic, "cleanup failed"):
                await self.seal(save=False)
            results = list(self.artifacts.root.glob("session-result-*/recording"))
            self.assertEqual(1, len(results))
            for name, data in original.items():
                self.assertEqual(data, (results[0] / name).read_bytes())
            self.assertFalse(json.loads((results[0] / "outcome.json").read_bytes())["session_complete"])
        finally:
            self.fixture.native.chmod(0o700)

    async def test_cancellation_waits_for_the_actual_cas_storage_thread(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        seal_tree = self.artifacts.seal_tree

        def blocked_seal(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("Storage test release was not delivered")
            result = seal_tree(*args, **kwargs)
            finished.set()
            return result

        with patch.object(self.artifacts, "seal_tree", blocked_seal):
            task = asyncio.create_task(self.seal())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0.02)
                    self.assertFalse(task.done())
                    self.assertTrue(self.service._storage_gate.locked())
                    self.assertFalse(finished.is_set())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())
        self.assertTrue((self.fixture.native / "records.jsonl.gz").exists())
        self.assertEqual([], list(self.artifacts.root.glob("session-result-*")))

    async def test_verification_copy_waits_for_the_common_gate_and_refuses_before_reading_source(self):
        record = await self.seal()
        self.store.row.update(state="stopped", recording_artifact=record)
        original = self.snapshot()
        self.service._lifecycle_state = "open"
        calls, reads = [], []

        def refuse(*args, **options):
            calls.append((args, options))
            raise SessionResourceError("Storage refused")

        def reader(*args):
            reads.append(args)
            raise AssertionError("Source must not be read before admission")

        async with self.service._storage_gate:
            with patch.object(self.service, "_admit_storage", refuse):
                task = asyncio.create_task(self.service.verify_recording(self.session.name, {}, reader))
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
                self.assertEqual([], calls)
            # Keep the refusal installed while the queued operation enters.
            replacement = patch.object(self.service, "_admit_storage", refuse)
            replacement.start()
        try:
            with self.assertRaisesRegex(SessionResourceError, "Storage refused"):
                await task
        finally:
            replacement.stop()
        self.assertEqual(1, len(calls))
        self.assertEqual([], reads)
        self.assertEqual(original, self.snapshot())
        self.assertEqual("failed", self.store.attempts[-1]["result"]["state"])
        self.assertEqual([], list(self.sessions.glob("recording-verification-*")))

    async def test_verification_copy_survives_repeated_cancellation_until_its_real_writer_finishes(self):
        certificate = {
            "report": {
                "checks": [
                    {"id": "provenance.warnings", "details": {"warning_comments": [], "warning_sha256": digest([])}}
                ]
            }
        }
        certificate_bytes = canonical(certificate)
        contents = {"model.xml": b"<mujoco/>", "certificate.json": certificate_bytes}
        source_input = self.root / "verification-input"
        source_input.mkdir()
        for name, value in contents.items():
            (source_input / name).write_bytes(value)
        source_ref = self.artifacts.seal_tree(
            source_input,
            kind="pico_test_source",
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256="c" * 64,
        )
        members = {
            item["path"]: {**item, "artifact": source_ref.to_record()}
            for item in self.artifacts.verify(source_ref)["members"]
        }
        source = {
            "files": {name: value["sha256"] for name, value in members.items()},
            "warning_inventory": text_warning_inventory([], members["certificate.json"]),
        }
        self.store.row["request"]["source"] = source
        self.store.row["request_digest"] = digest(self.store.row["request"])
        (self.session / "recording/request.json").write_bytes(canonical(self.store.row["request"]) + b"\n")
        (self.session / "recording/session.json").write_bytes(canonical(session_identity(self.store.row)) + b"\n")
        self.fixture.finalization_fixture()
        self.report["finalization_verification"] = self.fixture.finalization_result_double()
        self.report["manifest_sha256"] = hashlib.sha256(
            (self.fixture.native / "session-manifest.json").read_bytes()
        ).hexdigest()
        record = await self.seal()
        self.store.row.update(state="stopped", recording_artifact=record)
        self.service._lifecycle_state = "open"
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def reader(name, _member):
            if name == "model.xml":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Source test release was not delivered")
                finished.set()
            return contents[name]

        task = asyncio.create_task(self.service.verify_recording(self.session.name, members, reader))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                self.assertTrue(self.service._storage_gate.locked())
                self.assertFalse(finished.is_set())
                self.assertTrue(list(self.sessions.glob("recording-verification-*")))
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        self.assertFalse(self.service._storage_gate.locked())
        self.assertEqual("cancelled", self.store.attempts[-1]["result"]["state"])
        self.assertEqual([], list(self.sessions.glob("recording-verification-*")))
