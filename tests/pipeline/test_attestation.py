import copy
import hashlib
import io
import json
import subprocess
import unittest
import zipfile
from unittest.mock import patch

from description_pipeline.verification.attestation import verify_external_record


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self.policy = {
            "schema_version": "description.acceptance-trust/v1",
            "sources": [
                {"repository": "trusted/consumer", "workflow": ".github/workflows/accept.yml", "branch": "main"}
            ],
        }
        log = b"independent acceptance results"
        self.payload: dict = {
            "subject": "a" * 64,
            "results": [{"passed": True, "artifacts": {"docs/acceptance/run.log": hashlib.sha256(log).hexdigest()}}],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("acceptance.json", json.dumps(self.payload))
            archive.writestr("docs/acceptance/run.log", log)
        self.archive = buffer.getvalue()
        self.record: dict = {
            **self.payload,
            "attestation": {"repository": "trusted/consumer", "run_id": 42, "artifact_id": 17},
        }
        self.run_info = {
            "conclusion": "success",
            "path": ".github/workflows/accept.yml",
            "head_branch": "main",
            "event": "workflow_dispatch",
            "display_title": f"accept {'a' * 64} (hardware)",
        }
        self.artifact = {
            "expired": False,
            "workflow_run": {"id": 42},
            "digest": "sha256:" + hashlib.sha256(self.archive).hexdigest(),
        }

    def check_record(self, record, run=None, artifact=None, archive=None):
        payload = self.archive if archive is None else archive
        artifact = artifact or self.artifact
        if archive is not None:
            # A case that replaces the archive has to keep the digest consistent, or it would be
            # rejected for the wrong reason.
            artifact = {**artifact, "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}
        with (
            patch("description_pipeline.verification.attestation.read_data", return_value=self.policy),
            patch(
                "description_pipeline.verification.attestation._api",
                side_effect=[
                    json.dumps(run or self.run_info).encode(),
                    json.dumps(artifact).encode(),
                    payload,
                ],
            ),
        ):
            return verify_external_record(record, "hardware")

    def test_an_invalid_reference_is_rejected_before_any_request(self):
        """The reference is checked before a single API call, so a typo cannot look like a network fault."""

        cases = {
            "repository is not owner/name": {"repository": "not-a-repo", "run_id": 42, "artifact_id": 17},
            "repository is not a string": {"repository": 3, "run_id": 42, "artifact_id": 17},
            "run id is a string": {"repository": "trusted/consumer", "run_id": "42", "artifact_id": 17},
            "run id is zero": {"repository": "trusted/consumer", "run_id": 0, "artifact_id": 17},
            "artifact id is negative": {"repository": "trusted/consumer", "run_id": 42, "artifact_id": -1},
            "artifact id is missing": {"repository": "trusted/consumer", "run_id": 42},
        }
        for label, reference in cases.items():
            with self.subTest(case=label):
                result = self.check_record({**self.payload, "attestation": reference})
                self.assertFalse(result["trusted"])
                self.assertEqual(result["status"], "failed")

    def test_an_unregistered_producer_is_refused(self):
        record = {**self.payload, "attestation": {"repository": "someone/else", "run_id": 1, "artifact_id": 2}}
        with patch(
            "description_pipeline.verification.attestation.read_data",
            return_value={"schema_version": "description.acceptance-trust/v1", "sources": []},
        ):
            result = verify_external_record(record, "hardware")
        self.assertEqual(result["status"], "failed")
        self.assertIn("not registered", result["reason"])

    def test_an_expired_artifact_or_one_from_another_run_is_refused(self):
        for label, artifact in (
            ("expired", {**self.artifact, "expired": True}),
            ("another run", {**self.artifact, "workflow_run": {"id": 99}}),
        ):
            with self.subTest(case=label):
                result = self.check_record(self.record, artifact=artifact)
                self.assertFalse(result["trusted"])
                self.assertIn("expired or belongs to another run", result["reason"])

    def test_an_ambiguous_archive_is_refused(self):
        """Two entries with one name can carry two different payloads; nothing may guess which wins."""

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("acceptance.json", json.dumps(self.payload))
            archive.writestr("acceptance.json", json.dumps(self.payload))
        result = self.check_record(self.record, archive=buffer.getvalue())
        self.assertEqual(result["status"], "failed")
        self.assertIn("Ambiguous or oversized", result["reason"])

    def test_a_log_that_differs_from_its_checksum_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("acceptance.json", json.dumps(self.payload))
            archive.writestr("docs/acceptance/run.log", b"rewritten log")
        result = self.check_record(self.record, archive=buffer.getvalue())
        self.assertEqual(result["status"], "failed")
        self.assertIn("differs from the trusted producer", result["reason"])

    def test_the_fetch_is_deadline_and_size_bounded(self):
        from description_pipeline.verification import attestation

        oversized = subprocess.CompletedProcess([], 0, stdout=b"x" * (attestation.MAX_ARTIFACT_BYTES + 1))
        with (
            patch.object(attestation.subprocess, "run", return_value=oversized),
            self.assertRaisesRegex(ValueError, "exceeds size limit"),
        ):
            attestation._api("repos/x/y/artifacts/1/zip")
        ok = subprocess.CompletedProcess([], 0, stdout=b"payload")
        with patch.object(attestation.subprocess, "run", return_value=ok) as run:
            self.assertEqual(attestation._api("repos/x/y/artifacts/1/zip"), b"payload")
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_a_missing_gh_is_a_not_run_verdict(self):
        from description_pipeline.verification import attestation

        with (
            patch.object(attestation, "read_data", return_value=self.policy),
            patch.object(attestation.subprocess, "run", side_effect=FileNotFoundError("gh")),
        ):
            result = verify_external_record(self.record, "hardware")
        self.assertFalse(result["trusted"])
        self.assertEqual(result["status"], "not_run")
        self.assertIn("authenticate gh", result["reason"])

    def test_self_authored_candidate_record_is_not_qualification(self):
        self.assertEqual(verify_external_record(self.payload, "hardware")["status"], "not_run")
        self.assertTrue(self.check_record(self.record)["trusted"])

    def test_candidate_cannot_rewrite_trusted_results(self):
        tampered = copy.deepcopy(self.record)
        tampered["results"][0]["passed"] = False
        self.assertFalse(self.check_record(tampered)["trusted"])

    def test_wrong_subject_run_and_untrusted_workflow_are_rejected(self):
        for run in (
            {**self.run_info, "display_title": f"accept {'b' * 64} (hardware)"},
            {**self.run_info, "head_branch": "feature/untrusted"},
        ):
            self.assertFalse(self.check_record(self.record, run)["trusted"])

    def test_expired_or_changed_artifact_does_not_qualify(self):
        self.artifact["digest"] = "sha256:" + "0" * 64
        self.assertFalse(self.check_record(self.record)["trusted"])

    def test_unregistered_private_producer_is_refused(self):
        record = copy.deepcopy(self.record)
        record["attestation"]["repository"] = "mimicverse/description"
        with patch(
            "description_pipeline.verification.attestation.read_data",
            return_value={"schema_version": "description.acceptance-trust/v1", "sources": []},
        ):
            result = verify_external_record(record, "simulation")
        self.assertFalse(result["trusted"])
        self.assertIn("not registered", result["reason"])
