"""Real CAS/result IO with explicit business-proof doubles; no native qualification."""

from copy import deepcopy
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.io import canonical, digest
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.simulation.evidence import RecordingDiagnostic
from description_pipeline.simulation.recording import prepare_bound, recording_binding, session_identity
from description_pipeline.simulation.resources import SessionResourceError, directory_bytes
from description_pipeline.simulation.service import RunningSession, SimulationService
from tests.v1.test_pico_evidence import EvidenceFixture


class _StoreDouble:
    def __init__(self, row):
        self.row = row

    def get(self, identifier):
        assert identifier == self.row["session_id"]
        return deepcopy(self.row)

    def worker_commands(self, _identifier):
        return []

    def active_session_ids(self):
        return [self.row["session_id"]]


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
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())
        self.assertTrue((self.fixture.native / "records.jsonl.gz").exists())
        self.assertEqual([], list(self.artifacts.root.glob("session-result-*")))
