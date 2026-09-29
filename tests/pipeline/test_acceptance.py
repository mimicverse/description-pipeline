import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.build import PROFILE
from description_pipeline.io import digest, file_digest, write_json
from description_pipeline.verification.acceptance import verify_acceptance


class AcceptanceTests(unittest.TestCase):
    @patch(
        "description_pipeline.verification.acceptance.verify_external_record",
        return_value={"trusted": True, "status": "passed"},
    )
    def test_application_record_binds_subject_profile_and_actual_logs(self, _authority):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = {
                **PROFILE,
                "purpose": "training",
                "acceptance_suites": ["training-smoke"],
                "consumer_environment": {"training-runner": "1.0.0"},
            }
            self.assertEqual(verify_acceptance(root, "a" * 64, profile)["status"], "not_run")
            log = root / "docs/acceptance/smoke.log"
            log.parent.mkdir(parents=True)
            log.write_text("external fixture evidence\n")
            record: dict = {
                "schema_version": "description.acceptance/v2",
                "subject": "a" * 64,
                "profile_digest": digest(profile),
                "environment": profile["consumer_environment"],
                "results": [
                    {
                        "suite": "training-smoke",
                        "suite_version": "1.0.0",
                        "producer": "fixture-runner",
                        "executed_at": "2026-09-20T00:00:00Z",
                        "passed": True,
                        "artifacts": {"docs/acceptance/smoke.log": file_digest(log)},
                        "validation_data": {"docs/acceptance/smoke.log": file_digest(log)},
                        "conditions": {"scenario": "controlled test fixture"},
                        "data_role": "validation",
                        "used_for_fitting": False,
                        "evidence_class": "simulation",
                    }
                ],
            }
            write_json(root / "docs/acceptance/training.json", record)
            self.assertEqual(verify_acceptance(root, "a" * 64, profile)["status"], "passed")
            self.assertEqual(
                verify_acceptance(root, "a" * 64, profile, input_hashes={file_digest(log)})["status"], "failed"
            )
            record["results"][0]["used_for_fitting"] = True
            write_json(root / "docs/acceptance/training.json", record)
            self.assertEqual(verify_acceptance(root, "a" * 64, profile)["status"], "failed")
            record["results"][0]["used_for_fitting"] = False
            record["environment"] = {"training-runner": "2.0.0"}
            write_json(root / "docs/acceptance/training.json", record)
            self.assertEqual(verify_acceptance(root, "a" * 64, profile)["status"], "failed")
            record["environment"] = profile["consumer_environment"]
            write_json(root / "docs/acceptance/training.json", record)
            self.assertEqual(verify_acceptance(root, "b" * 64, profile)["status"], "failed")
            log.write_text("different evidence")
            self.assertEqual(verify_acceptance(root, "a" * 64, profile)["status"], "failed")
