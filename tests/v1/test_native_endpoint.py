"""Native admission and queue controls; fake CAD never grants qualification."""

from __future__ import annotations

import shutil
import unittest
from types import SimpleNamespace

from description_pipeline.delivery import subject_inventory
from description_pipeline.io import digest, write_json
from description_pipeline.orchestration.windows import Jobs, RequestError
from .endpoint_support import EndpointFixture


class NativeEndpointTests(EndpointFixture, unittest.TestCase):
    def test_folder_submission_discovers_routes_and_is_idempotent(self):
        calls = []

        def runner(package, output, **kwargs):
            calls.append(package)
            self.assertEqual("feature/arm", kwargs["base"])
            self.assertTrue((package / "robot.yaml").is_file())
            return self.passing_result()

        jobs = self.jobs(runner=runner)
        request = self.request(jobs)
        self.assertTrue(jobs.create(request)[1])
        jobs.queue.join()
        job, created = jobs.create(request)
        self.assertFalse(created)
        self.assertEqual("passed", job["status"])
        self.assertEqual("arm", job["hardware_id"])
        self.assertEqual("a/b", job["repository_slug"])
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

    def test_native_job_requires_independent_discovery_gate(self):
        result = self.passing_result()
        result["quality"]["checks"] = []
        jobs = self.jobs(runner=lambda *args, **kwargs: result)
        request = self.request(jobs)
        jobs.create(request)
        jobs.queue.join()
        self.assertEqual("failed", jobs.snapshot(request["run_id"])["status"])

    def test_preview_survives_pr_failure_but_rejects_mutation_and_nonviewer_files(self):
        def runner(package, output, **kwargs):
            for directory in ("input", "evidence", "model", "urdf", "meshes"):
                (output / directory).mkdir(parents=True, exist_ok=True)
            (output / "urdf/robot.urdf").write_text('<robot name="control"/>')
            (output / "input/hidden.txt").write_text("Never a viewer asset")
            (output / "README.md").write_text("Synthetic viewer boundary test")
            write_json(output / "reports/input.json", {})
            write_json(output / "reports/tool.json", {})
            subject = digest(subject_inventory(output))
            return {
                "passed": False,
                "error": "PR service unavailable",
                "subject_sha256": subject,
                "quality": {
                    "passed": True,
                    "subject_sha256": subject,
                    "checks": [{"id": "source.native_discovery", "passed": True}],
                },
                "submission": {},
            }

        jobs = self.jobs(runner=runner)
        request = self.request(jobs)
        jobs.create(request)
        jobs.queue.join()
        self.assertEqual("failed", jobs.snapshot(request["run_id"])["status"])
        preview = jobs.preview(request["run_id"])
        self.assertEqual(["urdf/robot.urdf"], list(preview["files"]))
        with self.assertRaises(RequestError):
            jobs.artifact(request["run_id"], "input/hidden.txt")
        stream, size = jobs.artifact(request["run_id"], "urdf/robot.urdf")
        with stream:
            self.assertEqual(size, len(stream.read()))
        (self.config["output_root"] / request["run_id"] / "urdf/robot.urdf").write_text("Changed")
        with self.assertRaises(RequestError):
            jobs.preview(request["run_id"])
