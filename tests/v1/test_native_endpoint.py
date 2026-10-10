"""Native admission and queue controls; fake CAD never grants qualification."""

from __future__ import annotations

import shutil
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.orchestration.windows import Jobs, RequestError
from .endpoint_support import EndpointFixture


class NativeEndpointTests(EndpointFixture, unittest.TestCase):
    def test_outside_source_root_is_rejected_before_any_file_copy(self):
        jobs = self.jobs()
        # This parent contains both CAD and service credentials. Admission must
        # not sweep it into a delivery merely because an assembly exists below it.
        with patch("description_pipeline.orchestration.windows.freeze_handoff") as freeze:
            with self.assertRaisesRegex(ValueError, "outside the configured"):
                jobs.resolve_handoff({"handoff_path": str(self.root)})
            freeze.assert_not_called()

    def test_nested_source_uses_the_same_identity_for_absolute_and_relative_paths(self):
        nested = self.source / "新版结构"
        nested.mkdir()
        (nested / "总装.SLDASM").write_bytes(b"Synthetic nested assembly")
        jobs = self.jobs()
        absolute = jobs.resolve_handoff({"handoff_path": str(nested)})
        relative = jobs.resolve_handoff({"handoff_path": "新版结构"})
        self.assertEqual(absolute, relative)

    def test_folder_submission_discovers_routes_and_is_idempotent(self):
        calls = []

        def runner(package, output, **kwargs):
            calls.append(package)
            self.assertEqual("capture", kwargs["stop_after"])
            self.assertTrue((package / "robot.yaml").is_file())
            return self.capture_transfer_result(output, run_id=kwargs["run_id"], on_event=kwargs["on_event"])

        jobs = self.jobs(runner=runner)
        request = self.request(jobs)
        self.assertTrue(jobs.create(request)[1])
        jobs.queue.join()
        job, created = jobs.create(request)
        self.assertFalse(created)
        self.assertEqual("native_complete", job["status"])
        self.assertEqual("arm", job["hardware_id"])
        self.assertEqual("a/b", job["repository_slug"])
        self.assertEqual(set(job["capture_archive"]), {"name", "sha256", "size", "manifest_sha256"})
        self.assertEqual(1, len(calls))
        self.assertFalse((self.source / "robot.yaml").exists())
        with self.assertRaises(RequestError) as error:
            jobs.create({**request, "handoff_sha256": "f" * 64})
        self.assertEqual(409, error.exception.status)

    def test_blocking_discovery_retains_objects_and_never_calls_runner(self):
        calls = []

        def prepare(*args, **kwargs):
            return SimpleNamespace(
                passed=False,
                hardware_id="",
                revision="",
                discovery_sha256="d" * 64,
                findings=({"code": "discovery.rigid_group_ambiguous", "object": "component:arm", "blocking": True},),
            )

        jobs = self.jobs(preparer=prepare, runner=lambda *args, **kwargs: calls.append(args))
        request = self.request(jobs)
        jobs.create(request)
        jobs.queue.join()
        job = jobs.snapshot(request["run_id"])
        self.assertEqual("failed", job["status"])
        self.assertEqual("component:arm", job["discovery"]["findings"][0]["object"])
        self.assertEqual([], calls)

    def test_changed_native_bytes_or_discovery_identity_block_execution(self):
        for mutation in ("source", "digest", "hardware"):
            with self.subTest(mutation=mutation):
                # Each case owns a separate state root and prepared directory.
                config = {**self.config, "state_root": self.root / ("state-" + mutation)}
                calls = []

                def prepare(source, output, run_id, mutation=mutation, **kwargs):
                    result = self.prepare(source, output, run_id, **kwargs)
                    if mutation == "source":
                        (source / "总装.SLDASM").write_bytes(b"Changed during discovery")
                    elif mutation == "digest":
                        result.handoff_sha256 = "f" * 64
                    else:
                        result.hardware_id = "unknown"
                    return result

                def runner(*args, calls=calls, **kwargs):
                    calls.append(args)

                jobs = Jobs(config, native_preparer=prepare, runner=runner)
                try:
                    request = self.request(jobs)
                    jobs.create(request)
                    jobs.queue.join()
                    self.assertEqual("failed", jobs.snapshot(request["run_id"])["status"])
                    self.assertEqual([], calls)
                finally:
                    jobs.close()
                # Restore the synthetic frozen store for the next independent case.
                shutil.rmtree(self.packages / "imports")

    def test_native_job_without_a_sealed_transfer_is_never_qualified(self):
        jobs = self.jobs(runner=lambda *args, **kwargs: {"passed": True, "subject_sha256": "a" * 64})
        request = self.request(jobs)
        jobs.create(request)
        jobs.queue.join()
        job = jobs.snapshot(request["run_id"])
        self.assertEqual("failed", job["status"])
        self.assertIn("sealed capture transfer", job["error"])

    def test_sealed_transfer_artifacts_are_hash_bound_and_state_gated(self):
        jobs = self.jobs()
        request = self.request(jobs)
        jobs.create(request)
        jobs.queue.join()
        job = jobs.snapshot(request["run_id"])
        self.assertEqual("native_complete", job["status"])
        output = self.config["output_root"] / request["run_id"]
        archive = job["capture_archive"]
        stream, size = jobs.artifact(request["run_id"], "native-evidence.zip")
        with stream:
            self.assertEqual(size, len(stream.read()))
        stream, size = jobs.artifact(request["run_id"], "transfer-manifest.json")
        with stream:
            self.assertEqual(size, len(stream.read()))
        with self.assertRaises(RequestError) as unknown:
            jobs.artifact(request["run_id"], "urdf/robot.urdf")
        self.assertEqual(404, unknown.exception.status)
        (output / "native-evidence.zip").write_bytes(b"tampered")
        with self.assertRaises(RequestError) as tampered:
            jobs.artifact(request["run_id"], "native-evidence.zip")
        self.assertEqual(409, tampered.exception.status)
        self.assertEqual(archive["sha256"], job["capture_archive"]["sha256"])
