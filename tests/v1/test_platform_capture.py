"""The platform capture import owns the admission: verified bytes, no file authority."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, file_digest
from description_pipeline.orchestration.stage_transfer import seal_capture
from description_pipeline.platform import capture as platform_capture
from description_pipeline.platform.capture import source_identity
from description_pipeline.platform import flow
from description_pipeline.platform.store import PlatformStore

from .linux_split_support import HANDOFF, SELECTED, UPLOAD, capture_job
from .test_platform_store import FakeWorld
from .test_stage_transfer import MAIN_ASSEMBLY, capture_root, native_tool

RUN = "0b1f2c33-1f1e-4b0e-9d6a-2f9a5c7e1d20"
FRESH = "4c2d3e44-2a2f-4c1f-8e7b-3a0b6d8f2e31"


class FakeEndpoint:
    """A deliberately unverifying artifact client; the import must not trust it."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = 0

    def stream_capture_archive(self, run_id, archive, destination, **kwargs):
        self.calls += 1
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payload)
        return destination


class PlatformCaptureImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="platform-capture-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_root = self.tmp / "store"
        self.world = FakeWorld()
        self.store = PlatformStore(self.world.connect, sealed=self._sealed)
        self.store.admit_run(
            request_id="q-capture",
            run_id=RUN,
            principal="portal",
            target_hardware="nd",
            main_assembly=MAIN_ASSEMBLY,
            dispatch={"stage": "capture", "operation_key": f"{RUN}/capture:0", "params_digest": "a" * 64},
        )
        self.tool = native_tool()
        source = capture_root(self.tmp, run_id=RUN, handoff=HANDOFF)
        self.archive = self.tmp / "native-evidence.zip"
        seal_capture(
            source,
            self.archive,
            run_id=RUN,
            handoff_sha256=HANDOFF,
            main_assembly=MAIN_ASSEMBLY,
            native_tool=self.tool,
        )

    def _sealed(self, uri):
        return self.store_root / "sealed" / uri.removeprefix("store://sealed/")

    def _paths(self, run_id):
        return flow.RunPaths(self.store_root, run_id)

    def _job(self, run_id, archive, *, declared_sha256):
        job = capture_job(run_id, archive, self.tool)
        job["result"]["capture_archive"]["sha256"] = declared_sha256
        return job

    def test_the_declared_archive_digest_is_enforced(self) -> None:
        endpoint = FakeEndpoint(self.archive.read_bytes())
        job = self._job(RUN, self.archive, declared_sha256="d" * 64)
        with self.assertRaisesRegex(PipelineError, "declared receipt"):
            platform_capture.import_native_capture(self.store, self._paths(RUN), RUN, job, endpoint)

    def test_capture_source_identity_never_degrades_to_missing(self) -> None:
        missing = self.tmp / "missing-identity"
        missing.mkdir()
        with self.assertRaisesRegex(PipelineError, "source.json"):
            source_identity(missing)
        (missing / "input").mkdir()
        (missing / "input/source.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(PipelineError):
            source_identity(missing)

    def test_matching_bytes_admit_and_mirror_the_native_events(self) -> None:
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        endpoint = FakeEndpoint(self.archive.read_bytes())
        job = self._job(RUN, self.archive, declared_sha256=digest)
        result = platform_capture.import_native_capture(self.store, self._paths(RUN), RUN, job, endpoint)
        self.assertEqual(result["state"], "capture_admitted")
        self.assertEqual(result["handoff_sha256"], HANDOFF)
        self.assertEqual(result["upload_files"], UPLOAD)
        self.assertEqual(result["selected_files"], SELECTED)
        self.assertEqual(result["selection_sha256"], job["selection"]["manifest"]["selection_sha256"])
        self.assertTrue((self._paths(RUN).capture_dir / "transfer-manifest.json").is_file())
        recorded = [event for event in self.world.events if event[0] == RUN]
        self.assertTrue(recorded, "the sealed native events must mirror into the platform stream")

    def test_changed_selection_refuses_before_downloading(self) -> None:
        mutations = (
            lambda job: job["request"].update(upload_sha256="0" * 64),
            lambda job: job.update(handoff_sha256=job["request"]["upload_sha256"]),
            lambda job: job["selection"]["manifest"].update(selection_sha256="0" * 64),
            lambda job: job["request"].update(main_assembly="another.SLDASM"),
            lambda job: job.update(package_files=UPLOAD),
            lambda job: job["selection"].update(state="ready"),
            lambda job: job["request"].update(run_id=FRESH),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                endpoint = FakeEndpoint(self.archive.read_bytes())
                job = self._job(RUN, self.archive, declared_sha256=file_digest(self.archive))
                mutate(job)
                with self.assertRaises(PipelineError):
                    platform_capture.import_native_capture(self.store, self._paths(RUN), RUN, job, endpoint)
                self.assertEqual(endpoint.calls, 0)

    def test_a_resealed_capture_with_another_native_inventory_refuses(self) -> None:
        import json

        from description_pipeline.io import write_json

        source = capture_root(self.tmp / "foreign-inventory", run_id=FRESH, handoff=HANDOFF)
        record_path = source / "evidence/discovery.json"
        record = json.loads(record_path.read_text())
        record["native_files"]["stray.SLDPRT"] = "0" * 64
        write_json(record_path, record)
        manifest_path = source / "evidence" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["discovery.json"] = file_digest(record_path)
        write_json(manifest_path, manifest)
        archive = self.tmp / "foreign-inventory.zip"
        with self.assertRaisesRegex(PipelineError, "Native discovery inventory"):
            seal_capture(
                source,
                archive,
                run_id=FRESH,
                handoff_sha256=HANDOFF,
                main_assembly=MAIN_ASSEMBLY,
                native_tool=self.tool,
            )

    def test_a_manifest_receipt_mismatch_is_refused_and_leaves_no_tree(self) -> None:
        endpoint = FakeEndpoint(self.archive.read_bytes())
        job = self._job(RUN, self.archive, declared_sha256=file_digest(self.archive))
        job["result"]["capture_archive"]["manifest_sha256"] = "e" * 64
        with self.assertRaisesRegex(PipelineError, "manifest"):
            platform_capture.import_native_capture(self.store, self._paths(RUN), RUN, job, endpoint)
        self.assertFalse(self._paths(RUN).capture_dir.exists())


if __name__ == "__main__":
    unittest.main()
