"""Native admission and queue controls; fake native legs never grant qualification."""

from __future__ import annotations

import copy
import unittest

from description_pipeline.io import PipelineError
from description_pipeline.orchestration.windows import RequestError
from description_pipeline.sources.solidworks.errors import CadError
from .endpoint_support import MAIN, PART, EndpointFixture


class NativeEndpointTests(EndpointFixture, unittest.TestCase):
    def test_upload_identity_and_selection_are_admitted_before_any_native_work(self):
        jobs = self.jobs(lambda *args, **kwargs: self.fail("an inadmissible request must not run"))
        for mutation in (
            {"upload_sha256": "0" * 64},
            {"main_assembly": "cad/absent.SLDASM"},
            {"main_assembly": PART},
            {"upload_files": {MAIN: "0" * 64}},
        ):
            with self.subTest(mutation=sorted(mutation)), self.assertRaises(PipelineError):
                jobs.create({**self.request(), **mutation})
        created = jobs.create(self.request())[0]
        self.assertEqual("awaiting_files", created["selection"]["state"])
        self.assertEqual({}, created["selection"]["staged_files"])
        self.assertEqual(self.upload_sha, created["selection"]["upload_sha256"])
        self.assertEqual([], self.reader_log)

    def test_staging_observes_the_full_folder_once_then_seals(self):
        jobs = self.jobs(lambda *args, **kwargs: {"passed": False, "error": "expected"})
        request = self.request()
        jobs.create(request)
        sealed = self.stage_and_seal(jobs, request["run_id"])
        observed = [entry for entry in self.reader_log if entry[0] == "observe"]
        self.assertEqual(1, len(observed))
        self.assertEqual(MAIN, observed[0][3])
        self.assertEqual("sealed", sealed["selection"]["state"])
        self.assertEqual(sealed["selection"]["manifest"], self.expected_manifest())

    def test_changed_staged_bytes_or_record_identity_block_capture(self):
        for mutation in ("staged", "record"):
            with self.subTest(mutation=mutation):
                config = self.config_for(mutation)
                calls: list = []

                def native_reader(frozen, settings, on_activity=None, mutation=mutation):
                    record = copy.deepcopy(self.record)
                    if mutation == "record":
                        record["identity"]["main_assembly"] = "cad/other.SLDASM"
                    return record

                jobs = self.jobs(
                    lambda *args, calls=calls, **kwargs: calls.append(args),
                    config=config,
                    native_reader=native_reader,
                )
                request = self.request()
                jobs.create(request)
                self.stage_and_seal(jobs, request["run_id"])
                if mutation == "staged":
                    staged = jobs._source_dir(jobs.jobs[request["run_id"]]) / PART
                    staged.write_bytes(b"changed after sealing\n")
                finished = self.wait_state_any_status(jobs, request["run_id"], {"awaiting_definition"})
                if finished["status"] != "failed":
                    self.complete_definition(jobs, request["run_id"])
                    finished = self.await_terminal(jobs, request["run_id"])
                self.assertEqual("failed", finished["status"])
                self.assertEqual([], calls)

    def test_blocking_native_reader_retains_objects_and_never_calls_runner(self):
        def native_reader(frozen, settings, on_activity=None):
            raise CadError(
                "cad_discovery_blocked",
                "Discovery is ambiguous",
                detail={"object": "component:arm", "blocking": True},
            )

        calls: list = []
        jobs = self.jobs(lambda *args, **kwargs: calls.append(args), native_reader=native_reader)
        request = self.request()
        jobs.create(request)
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("failed", finished["status"])
        self.assertEqual("cad_discovery_blocked", finished["error_code"])
        self.assertEqual("component:arm", finished["detail"]["object"])
        self.assertEqual([], calls)

    def test_native_job_without_a_sealed_transfer_is_never_qualified(self):
        jobs = self.jobs(lambda *args, **kwargs: {"passed": True, "subject_sha256": "a" * 64})
        request = self.request()
        jobs.create(request)
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("failed", finished["status"])
        self.assertIn("sealed capture transfer", finished["error"])

    def test_explicit_selection_is_unchanged_by_derivation(self):
        seen: list = []

        def runner(package, output, **kwargs):
            seen.append(kwargs.get("main_assembly"))
            return self.capture_transfer_result(output, run_id=kwargs["run_id"], on_event=kwargs.get("on_event"))

        jobs = self.jobs(runner)
        request = self.request()
        jobs.create(request)
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        self.assertEqual([MAIN], seen)
        self.assertEqual(request, finished["request"])
        self.assertEqual(MAIN, finished["main_assembly"])

    def test_sealed_transfer_artifacts_are_hash_bound_and_state_gated(self):
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        output = self.config["output_root"] / request["run_id"]
        archive = finished["capture_archive"]
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
        self.assertEqual(archive["sha256"], finished["capture_archive"]["sha256"])

    def test_selection_artifacts_are_not_served_before_a_sealed_transfer(self):
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        with self.assertRaises(RequestError):
            jobs.artifact(request["run_id"], "native-evidence.zip")


if __name__ == "__main__":
    unittest.main()
