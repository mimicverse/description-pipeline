"""Adversarial integrity tests for the split-capture chain (independent).

Wave-2 port of the legacy Linux-store integrity suite onto ``platform/flow`` +
``platform/capture`` + ``PlatformStore``.  The store is the only authority this
suite attacks: the registered frozen original anchors both mutable request
copies, every portable stage rechecks the admitted capture and the portable
runtime identity before consuming anything, a committed admission is never
silently re-downloaded, and a tampered (even self-consistently resealed)
in-flight tree is refused against the recorded admission.

The legacy file-state cases (crash windows between the install and the meta
write, malformed meta/events JSON, rollback of an advanced meta state) have no
file-state subject here - cross-restart durability is the deploy side's
real-PostgreSQL battery and the store's own confirmation rules - and the
preview/open_artifact streaming cases have no subject on the platform read
face yet; both are mapped in the wave-2 deletion/gap report.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline import solidworks
from description_pipeline.io import PipelineError, digest, file_digest, read_data, write_json
from description_pipeline.orchestration.stage_transfer import CAPTURE_MANIFEST, seal_capture
from description_pipeline.platform import capture as platform_capture
from description_pipeline.platform import flow
from description_pipeline.platform import storage as platform_storage
from description_pipeline.platform.store import PlatformStore, PlatformStoreError

from .linux_split_support import HANDOFF, UPLOAD, SplitAdmissionMixin, capture_job, portable_record
from .test_platform_store import FakeWorld
from .test_stage_transfer import MAIN_ASSEMBLY, capture_root, native_tool

RUN = "0b1f2c33-1f1e-4b0e-9d6a-2f9a5c7e1d20"
FRESH = "4c2d3e44-2a2f-4c1f-8e7b-3a0b6d8f2e31"


class LinuxStoreIntegrityTests(SplitAdmissionMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="linux-store-integrity-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_root = self.tmp / "store"
        self.world = FakeWorld()
        self.store = PlatformStore(self.world.connect, sealed=platform_storage.sealed_resolver(self.store_root))
        self.store.admit_run(
            request_id="q-integrity",
            run_id=RUN,
            principal="portal",
            target_hardware="nd",
            main_assembly=MAIN_ASSEMBLY,
            dispatch={"stage": "capture", "operation_key": f"{RUN}/capture:0", "params_digest": "a" * 64},
        )
        self.tool = native_tool()
        self.upload_sha256 = digest(UPLOAD)
        source = capture_root(
            self.tmp,
            run_id=RUN,
            handoff=HANDOFF,
            upload=self.upload_sha256,
        )
        self.archive = self.tmp / "native-evidence.zip"
        seal_capture(
            source,
            self.archive,
            run_id=RUN,
            handoff_sha256=HANDOFF,
            upload_sha256=self.upload_sha256,
            main_assembly=MAIN_ASSEMBLY,
            native_tool=self.tool,
        )

    def _revalidate(self, run_id: str = RUN) -> None:
        paths = self._paths(run_id)
        flow.revalidate_capture(self.store, run_id, paths.capture_dir, paths.run_dir / "input" / "request.json")

    # ---------------------------------------------------- frozen request anchoring

    def test_a_carried_frozen_original_revalidates_without_touching_the_native_manifest(self) -> None:
        self.admit(RUN)
        paths = self._paths(RUN)
        manifest = paths.capture_dir / "transfer-manifest.json"
        original = manifest.read_bytes()
        self._revalidate()
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(
            (paths.run_dir / "input" / "request.json").read_bytes(),
            (paths.capture_dir / "input" / "request.json").read_bytes(),
        )

    def test_a_tampered_carried_original_is_refused(self) -> None:
        self.admit(RUN)
        carried = self._paths(RUN).capture_dir / "input" / "request.json"
        carried.write_bytes(b'{"tampered": true}')
        with self.assertRaisesRegex(PipelineError, "carried input/request.json differs from the registered original"):
            self._revalidate()

    def test_mutable_request_copies_are_anchored_to_the_registered_original(self) -> None:
        self.admit(RUN)
        paths = self._paths(RUN)
        frozen = paths.run_dir / "input" / "request.json"
        carried = paths.capture_dir / "input" / "request.json"
        carried.write_bytes(b'{"tampered": true}')
        with self.assertRaisesRegex(PipelineError, "carried input/request.json differs from the registered original"):
            self._revalidate()
        # Copies that drifted together but away from the registration refuse too.
        carried.write_bytes(b'{"tampered": true}')
        frozen.write_bytes(b'{"tampered": true}')
        with self.assertRaisesRegex(PipelineError, "frozen input/request.json differs from the registered original"):
            self._revalidate()
        # Missing mutable copies refuse loudly; there is no file fallback.
        frozen.unlink()
        carried.unlink()
        with self.assertRaisesRegex(PipelineError, "missing the registered frozen input/request.json"):
            self._revalidate()
        # A run whose freeze stage was never registered has no fallback original either.
        self.store.admit_run(
            request_id="q-fresh",
            run_id=FRESH,
            principal="portal",
            target_hardware="nd",
            main_assembly=MAIN_ASSEMBLY,
            dispatch={"stage": "capture", "operation_key": f"{FRESH}/capture:0", "params_digest": "a" * 64},
        )
        with self.assertRaisesRegex(PipelineError, "no registered frozen original"):
            self._revalidate(FRESH)
        # An unconfigured sealed storage refuses instead of degrading to file reads.
        unsealed = PlatformStore(self.world.connect)
        with self.assertRaisesRegex(PlatformStoreError, "Sealed storage is not configured"):
            flow.revalidate_capture(
                unsealed, RUN, paths.capture_dir, paths.run_dir / "input" / "request.json"
            )
        # A damaged registration refuses through the store's own sealed re-verification.
        freeze_uri = self.store.stage_records(RUN)["freeze"]["checkpoint_uri"]
        (self.store.sealed_path(freeze_uri) / "input" / "handoff.json").unlink()
        with self.assertRaisesRegex(PipelineError, "manifest"):
            self._revalidate()

    # ------------------------------------------------------------ roles / drift

    def test_verify_refuses_a_runtime_that_drifted_from_the_native_capture(self) -> None:
        """A drifted host must not re-verify a retained checkpoint."""

        self.admit(RUN)
        drifted = portable_record(self.tool, source_sha256="0" * 64)
        with (
            mock.patch.object(flow, "tool_record", return_value=drifted),
            mock.patch.object(flow.solidworks, "verify", side_effect=AssertionError("must not run")),
            self.assertRaisesRegex(PipelineError, "differs from the native capture"),
        ):
            flow.run_stage(self.store, self.store_root, RUN, "verify", expected_subject="a" * 64)

    def test_generate_gate_control_refuses_the_same_drift(self) -> None:
        self.admit(RUN)
        drifted = portable_record(self.tool, source_sha256="0" * 64)
        with (
            mock.patch.object(flow, "tool_record", return_value=drifted),
            mock.patch.object(flow.solidworks, "generate", side_effect=AssertionError("must not run")),
            self.assertRaisesRegex(PipelineError, "differs from the native capture"),
        ):
            flow.run_stage(self.store, self.store_root, RUN, "generate")

    # ------------------------------------------------------------ frozen source

    def test_generate_refuses_a_capture_tampered_after_admission(self) -> None:
        """Revalidate admitted capture before consuming it."""

        self.admit(RUN)
        capture = self._paths(RUN).capture_dir
        (capture / "input" / "robot.yaml").write_text('{"hardware_id": "tampered"}\n', encoding="utf-8")
        with (
            mock.patch.object(flow, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(flow.solidworks, "generate", side_effect=AssertionError("must not run")),
            self.assertRaisesRegex(PipelineError, "inventory differs from the transfer manifest"),
        ):
            flow.run_stage(self.store, self.store_root, RUN, "generate")

    # ------------------------------------------------------------ failed runs

    def test_raised_stage_is_recorded_as_a_visible_failure(self) -> None:
        """A raising delegate leaves failure products in the work tree and no completion."""

        self.admit(RUN)
        with (
            mock.patch.object(flow, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(flow.solidworks, "generate", side_effect=PipelineError("boom")),
            self.assertRaisesRegex(PipelineError, "boom"),
        ):
            flow.run_stage(self.store, self.store_root, RUN, "generate")
        self.assertNotIn((RUN, "generate"), self.world.stages)
        products = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in self._paths(RUN).run_dir.rglob("*.json")
        ]
        self.assertTrue(any("boom" in json.dumps(payload) for payload in products), "the failure is not visible")

    def test_failed_receipt_from_the_runner_is_surfaced_not_registered(self) -> None:
        """A failed delegate receipt surfaces to the caller and never becomes a completion."""

        self.admit(RUN)
        failed = {
            "state": "failed",
            "passed": False,
            "error": "PipelineError: verification failed",
            "events": [{"stage": "generate", "state": "failed", "at": "2026-10-10T05:00:00+00:00"}],
        }
        with (
            mock.patch.object(flow, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(flow.solidworks, "generate", return_value=failed),
        ):
            receipt = flow.run_stage(self.store, self.store_root, RUN, "generate")
        self.assertEqual(receipt["state"], "failed")
        self.assertNotIn((RUN, "generate"), self.world.stages)

    # ------------------------------------------------------------ transfer stream

    def test_fetch_retry_preserves_the_recorded_selection(self) -> None:
        """An admitted retry reuses the recorded selection and never re-downloads."""

        self.admit(RUN)
        endpoint = mock.Mock()
        endpoint.stream_capture_archive.side_effect = AssertionError("must not re-download")
        job = capture_job(RUN, self.archive, self.tool)
        binding = platform_capture.import_native_capture(self.store, self._paths(RUN), RUN, job, endpoint)
        self.assertEqual(binding["state"], "capture_admitted")
        self.assertEqual(binding["selection"], job["selection"]["manifest"])
        drifted = capture_job(RUN, self.archive, self.tool)
        drifted["selection"]["staged_files"] = {**drifted["selection"]["staged_files"], "extra.txt": "0" * 64}
        with self.assertRaisesRegex(PipelineError, "sealed source selection"):
            platform_capture.import_native_capture(self.store, self._paths(RUN), RUN, drifted, endpoint)
        endpoint.stream_capture_archive.assert_not_called()

    # ------------------------------------------------------------ run lock

    def test_overlapping_portable_runs_for_one_attempt_are_refused(self) -> None:
        """The attempt-level lock must refuse a second portable stage for the same attempt."""

        self.admit(RUN)
        with (
            solidworks.output_lock(self._paths(RUN).run_dir),
            mock.patch.object(flow, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(flow.solidworks, "generate", side_effect=AssertionError("must not run")),
            self.assertRaisesRegex(PipelineError, "Another run owns"),
        ):
            flow.run_stage(self.store, self.store_root, RUN, "generate")

    # ------------------------------------------------------------ recorded-digest binding

    def test_generate_refuses_a_self_consistent_manifest_tamper(self) -> None:
        """Bind the entire recorded admission even when the replacement manifest is internally consistent."""

        self.admit(RUN)
        capture = self._paths(RUN).capture_dir
        report_path = capture / "input" / "handoff.json"
        discovery_path = capture / "evidence" / "discovery.json"
        evidence_manifest = capture / "evidence" / "manifest.json"
        target = capture / "input" / "cad" / MAIN_ASSEMBLY
        target.write_bytes(b"CAD-TAMPERED-AFTER-ADMISSION\n")
        report = read_data(report_path)
        report["input_receipt"]["inventory"][MAIN_ASSEMBLY] = file_digest(target)
        report["package_files"]["cad/" + MAIN_ASSEMBLY] = file_digest(target)
        write_json(report_path, report)
        discovery = read_data(discovery_path)
        discovery["native_files"][MAIN_ASSEMBLY] = file_digest(target)
        write_json(discovery_path, discovery)
        snapshot = read_data(evidence_manifest)
        snapshot["files"]["discovery.json"] = file_digest(discovery_path)
        write_json(evidence_manifest, snapshot)
        manifest_path = capture / CAPTURE_MANIFEST
        manifest = read_data(manifest_path)
        for name, path in (
            ("input/cad/" + MAIN_ASSEMBLY, target),
            ("input/handoff.json", report_path),
            ("evidence/discovery.json", discovery_path),
            ("evidence/manifest.json", evidence_manifest),
        ):
            manifest["files"][name] = file_digest(path)
        manifest["main_assembly_sha256"] = file_digest(target)
        manifest["file_count"] = len(manifest["files"])
        manifest["total_bytes"] = sum((capture / name).stat().st_size for name in manifest["files"])
        write_json(manifest_path, manifest)
        with (
            mock.patch.object(flow, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(flow.solidworks, "generate", side_effect=AssertionError("must not run")),
            self.assertRaisesRegex(PipelineError, "does not digest to its handoff"),
        ):
            flow.run_stage(self.store, self.store_root, RUN, "generate")


if __name__ == "__main__":
    unittest.main()
