"""Explicit main-assembly selection: client format, staged admission and resume identity.

These tests cover the protocol, admission and checkpoint-reuse contracts only;
they do not qualify native CAD behavior.
"""

from __future__ import annotations

from tests.v1.freeze_event_support import freeze_request_fields

import hashlib
import json
import unittest
import uuid
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from description_pipeline.io import PipelineError, digest
from description_pipeline.orchestration.airflow_client import (
    EndpointProtocolError,
    validate_main_assembly,
)
from description_pipeline.orchestration.windows import RequestError
from description_pipeline.sources.solidworks.discovery import DISCOVERY_FILE
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.steps import freeze_inputs

from .endpoint_support import MAIN, EndpointFixture, source_bytes


class MainAssemblyFormatTests(unittest.TestCase):
    def test_client_validator_accepts_canonical_values(self):
        for value in ("3.0 总装1008.SLDASM", "sub dir/arm.SLDASM", "a.SLDASM"):
            with self.subTest(value=value):
                self.assertEqual(value, validate_main_assembly(value))

    def test_client_validator_rejects_unsafe_values(self):
        for value in (
            None,
            5,
            "",
            " ",
            " x.SLDASM",
            "x.SLDASM ",
            "/abs.SLDASM",
            "..\\x.SLDASM",
            "../x.SLDASM",
            "a/../x.SLDASM",
            "a//b.SLDASM",
            "a\\b.SLDASM",
            "x.step",
            "x.sldasm/",
            "e\u0301.SLDASM",
            "x" * 1025,
        ):
            with self.subTest(value=repr(value)[:24]), self.assertRaises(EndpointProtocolError):
                validate_main_assembly(value)


class FreezeSelectionTests(unittest.TestCase):
    def test_freeze_requires_exact_membership_and_records_the_binding(self):
        with TemporaryDirectory() as temporary:
            package = Path(temporary) / "package"
            package.mkdir()
            (package / "top.SLDASM").write_bytes(b"assembly")
            (package / "part.SLDPRT").write_bytes(b"part")
            files = package_inventory(package)
            identity = freeze_inputs(package, digest(files), files, main_assembly="top.SLDASM")
            self.assertEqual("top.SLDASM", identity["main_assembly"])
            self.assertEqual(files["top.SLDASM"], identity["main_assembly_sha256"])
            with self.assertRaises(PipelineError):
                freeze_inputs(package, digest(files), files, main_assembly="other.SLDASM")
            with self.assertRaises(PipelineError) as error:
                freeze_inputs(package, digest(files), files, main_assembly="TOP.sldasm")
            self.assertIn("case", str(error.exception).lower())


class MainAssemblyEndpointTests(EndpointFixture, unittest.TestCase):
    def test_malformed_selection_is_refused_at_submit(self):
        jobs = self.jobs(lambda *args, **kwargs: self.fail("an inadmissible selection must not run"))
        request = self.request()
        for bad in ("../cad/robot.SLDASM", "cad/robot.step", "cad\\robot.SLDASM", " cad/robot.SLDASM", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(RequestError) as error:
                    jobs.create({**request, "run_id": str(uuid.uuid4()), "main_assembly": bad})
                self.assertEqual(400, error.exception.status)

    def test_selection_binds_the_job_the_sealed_manifest_and_the_derivation(self):
        jobs = self.jobs()
        request = self.request()
        created, is_new = jobs.create(request)
        self.assertTrue(is_new)
        self.assertEqual(MAIN, created["main_assembly"])
        finished = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        self.assertEqual(MAIN, finished["main_assembly"])
        self.assertEqual(MAIN, finished["selection"]["manifest"]["main_assembly"])
        self.assertEqual(self.upload_files, finished["selection"]["manifest"]["files"])
        self.assertEqual(MAIN, finished["request"]["main_assembly"])
        prepared = self.config["state_root"] / "prepared" / request["run_id"]
        payload = json.loads((prepared / DISCOVERY_FILE).read_text(encoding="utf-8"))
        self.assertEqual(MAIN, payload["identity"]["main_assembly"])
        self.assertEqual(MAIN, payload["derivation"]["main_assembly"])

    def test_selection_outside_the_upload_index_never_starts_native_work(self):
        calls: list = []
        jobs = self.jobs(
            lambda *args, **kwargs: calls.append(args),
            native_reader=lambda frozen, settings, on_activity=None: calls.append((frozen, settings)),
        )
        with self.assertRaises(PipelineError) as error:
            jobs.create({**self.request(), "main_assembly": "cad/absent.SLDASM"})
        self.assertEqual("selection_main_invalid", getattr(error.exception, "code", None))
        self.assertEqual([], calls)
        self.assertEqual([], self.reader_log)

    def test_case_only_variant_is_named_in_the_failure(self):
        jobs = self.jobs(lambda *args, **kwargs: self.fail("an unbound selection must not run"))
        with self.assertRaises(PipelineError) as error:
            jobs.create({**self.request(), "main_assembly": "CAD/ROBOT.SLDASM"})
        self.assertEqual("selection_main_invalid", getattr(error.exception, "code", None))
        self.assertEqual("CAD/ROBOT.SLDASM", error.exception.detail["path"])


class MainAssemblyResumeTests(EndpointFixture, unittest.TestCase):
    def native_parent(self):
        jobs = self.jobs()
        request = self.request()
        jobs.create(request)
        parent = self.run_to_terminal(jobs, request["run_id"])
        self.assertEqual(parent["status"], "native_complete", parent.get("error"))
        return jobs, parent

    @staticmethod
    def linked_request(parent: dict, *, main_assembly: str | None = MAIN) -> dict:
        request = {
            **freeze_request_fields(parent["request"]),
            "run_id": str(uuid.uuid4()),
            "upload_files": parent["request"]["upload_files"],
            "upload_sha256": parent["request"]["upload_sha256"],
            "resume": {"parent_run": parent["run_id"], "from_stage": "capture"},
        }
        if main_assembly is not None:
            request["main_assembly"] = main_assembly
        return request

    def test_resume_accepts_the_same_selection(self):
        parent_jobs, parent = self.native_parent()
        parent_jobs.close()
        child_jobs = self.jobs(native_reader=lambda *args, **kwargs: self.fail("discovery must not rerun"))
        request = self.linked_request(parent)
        child, created = child_jobs.create(request)
        self.assertTrue(created)
        self.assertEqual(MAIN, child["main_assembly"])
        finished = self.await_terminal(child_jobs, request["run_id"])
        self.assertEqual("native_complete", finished["status"], finished.get("error"))
        self.assertEqual(MAIN, finished["main_assembly"])

    def test_resume_without_the_selection_is_refused(self):
        parent_jobs, parent = self.native_parent()
        parent_jobs.close()
        jobs = self.jobs(native_reader=lambda *args, **kwargs: self.fail("discovery must not rerun"))
        with self.assertRaises(RequestError) as error:
            jobs.create(self.linked_request(parent, main_assembly=None))
        self.assertEqual(400, error.exception.status)

    def test_changed_selection_cannot_reuse_checkpoints(self):
        extra = b"other assembly\n"
        upload = {**self.upload_files}
        upload["cad/other.SLDASM"] = hashlib.sha256(extra).hexdigest()
        self.upload_files = upload
        self.upload_sha = digest(upload)
        payloads = {**source_bytes(), "cad/other.SLDASM": extra}

        def batch_zip(names=None) -> Path:
            """The exact full-index archive, extended with the second assembly."""

            selected = sorted(payloads) if names is None else list(names)
            path = self.root / f"upload-{uuid.uuid4().hex}.zip"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
                for name in selected:
                    output.writestr(name, payloads[name])
            return path

        self.batch_zip = batch_zip
        parent_jobs, parent = self.native_parent()
        parent_jobs.close()
        jobs = self.jobs(native_reader=lambda *args, **kwargs: self.fail("discovery must not rerun"))
        with self.assertRaises(RequestError) as error:
            jobs.create(self.linked_request(parent, main_assembly="cad/other.SLDASM"))
        self.assertEqual(400, error.exception.status)
        self.assertIn("exact upload", str(error.exception))

    def test_null_effective_selection_is_refused_before_any_capture(self):
        calls: list = []
        parent_jobs, parent = self.native_parent()
        parent_jobs.close()
        ledger = self.config["state_root"] / "jobs" / (parent["run_id"] + ".json")
        stored = json.loads(ledger.read_text(encoding="utf-8"))
        stored["main_assembly"] = None
        ledger.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        jobs = self.jobs(
            lambda *args, **kwargs: calls.append(args),
            native_reader=lambda *args, **kwargs: self.fail("discovery must not rerun"),
        )
        with self.assertRaises(RequestError) as error:
            jobs.create(self.linked_request(parent))
        self.assertEqual(400, error.exception.status)
        self.assertIn("effective main assembly", str(error.exception))
        self.assertEqual([], calls)

    def test_changed_effective_selection_is_refused_before_any_capture(self):
        for forge in ("effective", "manifest"):
            with self.subTest(forge=forge):
                calls: list = []
                parent_jobs, parent = self.native_parent()
                parent_jobs.close()
                ledger = self.config["state_root"] / "jobs" / (parent["run_id"] + ".json")
                stored = json.loads(ledger.read_text(encoding="utf-8"))
                if forge == "effective":
                    stored["main_assembly"] = "cad/other.SLDASM"
                else:
                    stored["selection"]["manifest"]["main_assembly"] = "cad/other.SLDASM"
                ledger.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
                jobs = self.jobs(
                    lambda *args, calls=calls, **kwargs: calls.append(args),
                    native_reader=lambda *args, **kwargs: self.fail("discovery must not rerun"),
                )
                try:
                    jobs.create(self.linked_request(parent))
                except RequestError as error:
                    # A changed effective selection is refused either by the
                    # linked-parent guard (400) or by the re-probed plan (409).
                    self.assertIn(error.status, {400, 409})
                    if error.status == 400:
                        self.assertIn("effective main assembly", str(error))
                else:
                    self.fail("a mutated parent selection must be refused")
                self.assertEqual([], calls)
                jobs.close()


if __name__ == "__main__":
    unittest.main()
