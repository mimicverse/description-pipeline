"""Adversarial integrity tests for the Linux store and portable runner (independent).

These tests pin the contracts the split depends on; the ones marked RED in the review are
expected to fail against the current draft and document the exact defect until it is fixed:

* verify must not run on a runtime that drifted from the admitted native capture;
* generate must re-validate the admitted capture before consuming it;
* a portable run that raises must still record a failed receipt and keep it visible;
* the downloaded capture archive must match the declared digest even if the client did not;
* overlapping portable runs for one attempt must be refused by the run lock;
* malformed store state must be refused, never silently emptied.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from description_pipeline import solidworks
from description_pipeline.io import PipelineError, write_json
from description_pipeline.orchestration import linux_runner
from description_pipeline.orchestration.linux_store import LinuxStore
from description_pipeline.orchestration.stage_transfer import CAPTURE_ARCHIVE, CAPTURE_MANIFEST, seal_capture
from description_pipeline.runtime import RUNTIME_VERSIONS

from .test_stage_transfer import HANDOFF, MAIN_ASSEMBLY, capture_root, native_tool

RUN = "0b1f2c33-1f1e-4b0e-9d6a-2f9a5c7e1d20"
FRESH = "4c2d3e44-2a2f-4c1f-8e7b-3a0b6d8f2e31"


def portable_twin(native: dict, *, source_sha256: str | None = None, mujoco: str | None = None) -> dict:
    """A portable twin of a fixture native tool record, with an optional drift."""

    record = json.loads(json.dumps(native))
    record["runtime"] = {
        **record.get("runtime", {}),
        "role": "portable",
        "system": "Linux",
        "packages": {"mujoco": mujoco or RUNTIME_VERSIONS["mujoco"]},
    }
    if source_sha256 is not None:
        record["source_sha256"] = source_sha256
    return record


class FakeEndpoint:
    """A deliberately unverifying artifact client; the runner must not trust it."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = 0

    def stream_capture_archive(self, run_id, archive, destination, **kwargs):
        self.calls += 1
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payload)
        return destination


class LinuxStoreIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="linux-store-integrity-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = LinuxStore(self.tmp / "store")
        self.tool = native_tool()
        source = capture_root(self.tmp, run_id=RUN)
        self.archive = self.tmp / "native-evidence.zip"
        seal_capture(
            source,
            self.archive,
            run_id=RUN,
            handoff_sha256=HANDOFF,
            main_assembly=MAIN_ASSEMBLY,
            native_tool=self.tool,
        )
        self.store.import_capture(
            RUN,
            self.archive,
            expected_handoff_sha256=HANDOFF,
            expected_main_assembly=MAIN_ASSEMBLY,
            expected_native_tool=self.tool,
        )

    # ------------------------------------------------------------ roles / drift

    def test_verify_refuses_a_runtime_that_drifted_from_the_native_capture(self) -> None:
        """RED until the identity gate also covers verify: a drifted host must not re-verify."""

        drifted = portable_twin(self.tool, source_sha256="0" * 64)
        with (
            mock.patch.object(linux_runner, "tool_record", return_value=drifted),
            mock.patch.object(linux_runner.solidworks, "run", side_effect=AssertionError("must not run")),
            self.assertRaises(PipelineError),
        ):
            linux_runner.run_portable_stage(self.store, RUN, "verify", expected_subject="a" * 64)

    def test_generate_gate_control_refuses_the_same_drift(self) -> None:
        drifted = portable_twin(self.tool, source_sha256="0" * 64)
        with (
            mock.patch.object(linux_runner, "tool_record", return_value=drifted),
            mock.patch.object(linux_runner.solidworks, "run", side_effect=AssertionError("must not run")),
            self.assertRaises(PipelineError),
        ):
            linux_runner.run_portable_stage(self.store, RUN, "generate")

    # ------------------------------------------------------------ frozen source

    def test_generate_refuses_a_capture_tampered_after_admission(self) -> None:
        """RED until the runner re-validates the admitted capture before consuming it."""

        (self.store.capture_dir(RUN) / "input/robot.yaml").write_text('{"hardware_id": "tampered"}\n', encoding="utf-8")
        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_twin(self.tool)),
            mock.patch.object(linux_runner.solidworks, "run", side_effect=AssertionError("must not run")),
            self.assertRaises(PipelineError),
        ):
            linux_runner.run_portable_stage(self.store, RUN, "generate")

    # ------------------------------------------------------------ failed runs

    def test_raised_stage_is_recorded_as_a_visible_failure(self) -> None:
        """RED until a raising delegate still leaves a failed receipt and meta entry."""

        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_twin(self.tool)),
            mock.patch.object(linux_runner.solidworks, "run", side_effect=PipelineError("boom")),
            self.assertRaises(PipelineError),
        ):
            linux_runner.run_portable_stage(self.store, RUN, "generate")
        receipt_path = self.store.receipt_path(RUN, "generate")
        self.assertTrue(receipt_path.is_file(), "a failed portable stage left no receipt")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertIn("boom", str(receipt.get("error")))
        meta = self.store.meta(RUN) or {}
        self.assertEqual(meta.get("state"), "failed")
        self.assertIn("boom", str(meta.get("error")))

    def test_failed_receipt_from_the_runner_is_persisted(self) -> None:
        failed = {
            "state": "failed",
            "passed": False,
            "error": "PipelineError: verification failed",
            "events": [{"stage": "generate", "state": "failed", "at": "2026-10-10T05:00:00+00:00"}],
        }
        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_twin(self.tool)),
            mock.patch.object(linux_runner.solidworks, "run", return_value=failed),
        ):
            linux_runner.run_portable_stage(self.store, RUN, "generate")
        meta = self.store.meta(RUN) or {}
        self.assertEqual(meta.get("state"), "failed")
        self.assertIn("verification failed", str(meta.get("error")))

    # ------------------------------------------------------------ transfer stream

    def _job(self, run_id: str, archive: Path, *, declared_sha256: str) -> dict:
        payload = archive.read_bytes()
        with zipfile.ZipFile(archive) as sealed:
            manifest_sha256 = hashlib.sha256(sealed.read(CAPTURE_MANIFEST)).hexdigest()
        return {
            "status": "native_complete",
            "run_id": run_id,
            "main_assembly": MAIN_ASSEMBLY,
            "request": {"handoff_sha256": HANDOFF, "main_assembly": MAIN_ASSEMBLY},
            "result": {
                "native_complete": True,
                "native_tool": self.tool,
                "capture_archive": {
                    "name": CAPTURE_ARCHIVE,
                    "sha256": declared_sha256,
                    "size": len(payload),
                    "manifest_sha256": manifest_sha256,
                },
            },
        }

    def test_fetch_capture_enforces_the_declared_archive_digest(self) -> None:
        """RED until the runner (not only the client) checks bytes against the declared digest."""

        source = capture_root(self.tmp / "fresh", run_id=FRESH)
        fresh_archive = self.tmp / "fresh.zip"
        seal_capture(
            source,
            fresh_archive,
            run_id=FRESH,
            handoff_sha256=HANDOFF,
            main_assembly=MAIN_ASSEMBLY,
            native_tool=self.tool,
        )
        endpoint = FakeEndpoint(fresh_archive.read_bytes())
        job = self._job(FRESH, fresh_archive, declared_sha256="d" * 64)
        with self.assertRaises(PipelineError):
            linux_runner.fetch_capture(self.store, endpoint, FRESH, job)

    def test_fetch_capture_accepts_matching_bytes(self) -> None:
        source = capture_root(self.tmp / "fresh-ok", run_id=FRESH)
        fresh_archive = self.tmp / "fresh-ok.zip"
        seal_capture(
            source,
            fresh_archive,
            run_id=FRESH,
            handoff_sha256=HANDOFF,
            main_assembly=MAIN_ASSEMBLY,
            native_tool=self.tool,
        )
        digest = hashlib.sha256(fresh_archive.read_bytes()).hexdigest()
        endpoint = FakeEndpoint(fresh_archive.read_bytes())
        job = self._job(FRESH, fresh_archive, declared_sha256=digest)
        result = linux_runner.fetch_capture(self.store, endpoint, FRESH, job)
        self.assertEqual(result["state"], "capture_admitted")
        self.assertEqual((self.store.meta(FRESH) or {}).get("handoff_sha256"), HANDOFF)

    # ------------------------------------------------------------ run lock

    def test_overlapping_portable_runs_for_one_attempt_are_refused(self) -> None:
        """The store-level run lock must refuse a second portable stage for the same attempt."""

        with (
            solidworks.output_lock(self.store.run_dir(RUN)),
            mock.patch.object(linux_runner, "tool_record", return_value=portable_twin(self.tool)),
            mock.patch.object(linux_runner.solidworks, "run", side_effect=AssertionError("must not run")),
            self.assertRaises(PipelineError),
        ):
            linux_runner.run_portable_stage(self.store, RUN, "generate")

    # ------------------------------------------------------------ malformed state

    def test_malformed_state_is_refused_not_silently_emptied(self) -> None:
        """RED for events: a non-list events file must raise, not read as an empty stream."""

        write_json(self.store.events_path(RUN), {"stage": "generate", "state": "running"})
        with self.assertRaises(PipelineError):
            self.store.events(RUN)

        self.store.meta_path(RUN).write_text("{not json", encoding="utf-8")
        with self.assertRaises(PipelineError):
            self.store.meta(RUN)


if __name__ == "__main__":
    unittest.main()
