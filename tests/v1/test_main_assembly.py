"""Explicit main-assembly selection: transport format, freeze binding and resume identity.

These tests cover the protocol, admission and checkpoint-reuse contracts only;
they do not qualify native CAD behavior.
"""

from __future__ import annotations

import json
import time
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from description_pipeline.io import PipelineError, digest
from description_pipeline.orchestration.airflow_client import (
    EndpointProtocolError,
    validate_main_assembly,
)
from description_pipeline.orchestration.windows import RequestError
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.steps import freeze_inputs

from .endpoint_support import EndpointFixture


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
        jobs = self.jobs()
        request = self.request()
        for bad in ("../top.SLDASM", "top.step", "sub\\top.SLDASM", " top.SLDASM", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(RequestError) as error:
                    jobs.create({**request, "run_id": str(uuid.uuid4()), "main_assembly": bad})
                self.assertEqual(400, error.exception.status)

    def test_selection_binds_freeze_and_discovery_evidence(self):
        jobs = self.jobs()
        request = self.request()
        selection = "总装.SLDASM"
        _snapshot, created = jobs.create({**request, "main_assembly": selection})
        self.assertTrue(created)
        jobs.queue.join()
        job = jobs.snapshot(request["run_id"])
        self.assertEqual("native_complete", job["status"], job.get("error"))
        self.assertEqual(selection, job["main_assembly"])
        integrity = next(
            event["check"] for event in job["events"] if (event.get("check") or {}).get("id") == "handoff.integrity"
        )
        self.assertEqual(selection, integrity["details"]["main_assembly"])
        self.assertEqual(job["package_files"][selection], integrity["details"]["main_assembly_sha256"])
        inputs = next(
            event["check"] for event in job["events"] if (event.get("check") or {}).get("id") == "discovery.inputs"
        )
        self.assertEqual(selection, inputs["details"]["main_assembly"])

    def test_selection_missing_from_the_frozen_handoff_fails_before_native(self):
        prepared: list = []

        def preparer(*args, **kwargs):
            prepared.append(args)
            raise AssertionError("native discovery must not run for an unbound selection")

        jobs = self.jobs(preparer=preparer)
        request = self.request()
        jobs.create({**request, "main_assembly": "other.SLDASM"})
        jobs.queue.join()
        job = jobs.snapshot(request["run_id"])
        self.assertEqual("failed", job["status"])
        self.assertIn("not part of the frozen handoff", job.get("error") or "")
        self.assertEqual([], prepared)
        integrity = [
            event["check"] for event in job["events"] if (event.get("check") or {}).get("id") == "handoff.integrity"
        ]
        self.assertTrue(integrity)
        self.assertTrue(all(check.get("state") == "failed" for check in integrity))
        self.assertIn("frozen handoff", str(integrity[-1].get("details")))

    def test_case_only_variant_is_named_in_the_failure(self):
        jobs = self.jobs()
        request = self.request()
        jobs.create({**request, "main_assembly": "总装.sldasm"})
        jobs.queue.join()
        job = jobs.snapshot(request["run_id"])
        self.assertEqual("failed", job["status"])
        self.assertIn("case", (job.get("error") or "").lower())


class MainAssemblyResumeTests(EndpointFixture, unittest.TestCase):
    def await_terminal(self, jobs, run_id: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = jobs.snapshot(run_id)
            if job["status"] in {"passed", "failed", "native_complete"}:
                return job
            time.sleep(0.01)
        raise AssertionError(f"{run_id} did not finish")

    def finished_parent(self, selection: str | None):
        jobs = self.jobs()
        request = self.request()
        if selection is not None:
            request["main_assembly"] = selection
        jobs.create(request)
        parent = self.await_terminal(jobs, request["run_id"])
        self.assertEqual("native_complete", parent["status"], parent.get("error"))
        return jobs, parent

    @staticmethod
    def linked_request(parent: dict, selection: str | None):
        request = {
            "run_id": str(uuid.uuid4()),
            "package": parent["request"]["package"],
            "handoff_sha256": parent["request"]["handoff_sha256"],
            "resume": {"parent_run": parent["run_id"], "from_stage": "capture"},
        }
        if selection is not None:
            request["main_assembly"] = selection
        return request

    def test_resume_accepts_the_same_selection_and_inherits_when_absent(self):
        parent_jobs, parent = self.finished_parent("总装.SLDASM")
        parent_jobs.close()
        for selection in ("总装.SLDASM", None):
            with self.subTest(selection=selection):
                child_jobs = self.jobs(preparer=lambda *args, **kwargs: self.fail("discovery must not rerun"))
                request = self.linked_request(parent, selection)
                child, created = child_jobs.create(request)
                self.assertTrue(created)
                self.assertEqual("总装.SLDASM", child["main_assembly"])
                finished = self.await_terminal(child_jobs, request["run_id"])
                self.assertEqual("native_complete", finished["status"], finished.get("error"))
                child_jobs.close()

    def test_changed_selection_cannot_reuse_checkpoints(self):
        parent_jobs, parent = self.finished_parent("总装.SLDASM")
        parent_jobs.close()
        jobs = self.jobs()
        with self.assertRaises(RequestError) as error:
            jobs.create(self.linked_request(parent, "other.SLDASM"))
        self.assertEqual(409, error.exception.status)
        self.assertEqual("selection_changed", error.exception.payload.get("reason"))

    def test_legacy_parent_cannot_gain_a_selection_on_resume(self):
        parent_jobs, parent = self.finished_parent(None)
        parent_jobs.close()
        # A run stored before effective selections were recorded carries none; it cannot
        # acquire one retroactively on resume.
        ledger = self.config["state_root"] / "jobs" / (parent["run_id"] + ".json")
        stored = json.loads(ledger.read_text(encoding="utf-8"))
        stored["main_assembly"] = None
        ledger.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        parent["main_assembly"] = None
        jobs = self.jobs()
        with self.assertRaises(RequestError) as error:
            jobs.create(self.linked_request(parent, "总装.SLDASM"))
        self.assertEqual("selection_changed", error.exception.payload.get("reason"))
