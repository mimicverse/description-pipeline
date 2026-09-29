"""Real local acceptance and forgery rejection, with no mocked trust authority."""

import contextlib
import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.build import assess, build, profile_for
from description_pipeline.cli import main
from description_pipeline.io import PipelineError, file_digest, write_json
from description_pipeline.repository import update
from description_pipeline.verification.acceptance import verify_acceptance
from description_pipeline.verification.simulation import AcceptanceError, load_config, run_acceptance
from tests.pipeline.test_simulation_acceptance import Workspace


class LocalAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.workspace = Workspace(self.base / "fixture")
        self.workspace.write_config()
        pending = build(self.workspace.root, "simulation")
        self.assertEqual(pending["blockers"], ["consumer.application"])
        self.candidate = Path(pending["diagnostic_path"])
        self.profile = profile_for(self.candidate, "simulation")
        self.output = self.base / "measurements"
        self.record = run_acceptance(
            self.candidate, "simulation", Path("config/simulation-acceptance.json"), self.output
        )
        shutil.copytree(self.output / "docs/acceptance", self.workspace.root / "docs/acceptance", dirs_exist_ok=True)

    def check_record(self, record, **kwargs):
        # Candidate inputs retain their exact identity; only acceptance files change.
        shutil.copytree(self.output / "docs/acceptance", self.candidate / "docs/acceptance", dirs_exist_ok=True)
        write_json(self.candidate / "docs/acceptance/simulation.json", record)
        return verify_acceptance(self.candidate, self.record["subject"], self.profile, **kwargs)

    def test_pending_to_published_and_relocated_consumer_replay(self):
        verdict = self.check_record(self.record)
        self.assertEqual(verdict["status"], "passed", verdict)
        self.assertEqual(verdict["details"]["execution"], "local_replay")
        published = build(self.workspace.root, "simulation")
        self.assertTrue(published["passed"], published["blockers"])
        self.assertEqual(published["qualified_for"], ["simulation"])
        relocated = self.base / "consumer"
        shutil.copytree(self.workspace.root, relocated, ignore=shutil.ignore_patterns("build"))
        shutil.rmtree(self.workspace.root)
        shutil.rmtree(self.workspace.source)
        checked = assess(relocated, "simulation")
        self.assertTrue(checked["passed"], checked["blockers"])
        self.assertEqual(checked["subject"], self.record["subject"])

    def test_public_cli_runs_from_the_package(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(
                [
                    "model",
                    "accept",
                    "--root",
                    str(self.candidate),
                    "--profile",
                    "simulation",
                    "--out",
                    str(self.base / "cli-result"),
                ]
            )
        value = json.loads(stdout.getvalue())
        self.assertEqual(status, 0, value)
        self.assertEqual(value["subject"], self.record["subject"])
        self.assertFalse(value["release_qualified"])

    def test_public_cli_preserves_failed_run_diagnostic(self):
        config = json.loads((self.workspace.root / "config/simulation-acceptance.json").read_text(encoding="utf-8"))
        config["initial_state"]["joints"]["arm_joint"] = 0.5
        self.workspace.write_config(config)
        pending = build(self.workspace.root, "simulation")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = main(
                [
                    "model",
                    "accept",
                    "--root",
                    pending["diagnostic_path"],
                    "--out",
                    str(self.base / "cli-failed"),
                ]
            )
        failure = json.loads(stderr.getvalue())
        self.assertEqual(status, 2)
        self.assertTrue((Path(failure["diagnostic_path"]) / "failure.json").is_file())

    def test_forged_identity_environment_tool_and_measurements_are_rejected(self):
        mutations = [
            lambda r: r.update(subject="0" * 64),
            lambda r: r.update(profile_digest="0" * 64),
            lambda r: r.update(purpose="hardware"),
            lambda r: r["environment"].update(python="3.12.0"),
            lambda r: r["tool"]["identity"].update(package_digest="0" * 64),
            lambda r: r["runtime"].update(nu=9),
            lambda r: r["model"].update(config_sha256="0" * 64),
            lambda r: r["model"].update(config="../outside.json"),
            lambda r: r["results"].pop(),
            lambda r: r["results"][0]["metrics"].update(tracking_rmse_rad=0.12345),
            lambda r: r["results"][0]["conditions"].update(duration_s=60),
            lambda r: r["results"][0]["thresholds"].update(tracking_rmse_rad=3.0),
            lambda r: r["results"][0].update(used_for_fitting=True),
            lambda r: r["results"][0].update(evidence_class="physical_measurement"),
            lambda r: r["results"].append(copy.deepcopy(r["results"][0])),
            lambda r: r.update(results=[None]),
            lambda r: r["attestation"].update(schema_version="unknown"),
        ]
        for index, mutate in enumerate(mutations):
            record = copy.deepcopy(self.record)
            mutate(record)
            with self.subTest(mutation=index):
                self.assertEqual(self.check_record(record)["status"], "failed")

    def test_rehashed_fake_telemetry_and_reused_fitting_data_are_rejected(self):
        record = copy.deepcopy(self.record)
        name = next(iter(record["results"][0]["artifacts"]))
        original = (self.output / name).read_bytes()
        (self.output / name).write_bytes(b"forged telemetry")
        checksum = file_digest(self.output / name)
        record["results"][0]["artifacts"][name] = checksum
        record["results"][0]["validation_data"][name] = checksum
        self.assertEqual(self.check_record(record)["status"], "failed")
        # The genuine record also fails if its observations were used as fitting input.
        (self.output / name).write_bytes(original)
        self.assertEqual(self.check_record(self.record)["status"], "passed")
        genuine = next(iter(self.record["results"][0]["validation_data"].values()))
        self.assertEqual(self.check_record(self.record, input_hashes={genuine})["status"], "failed")

    def test_purpose_escalation_and_input_mutation_are_rejected(self):
        for purpose in ("training", "hardware"):
            record = {**self.record, "purpose": purpose}
            write_json(self.candidate / f"docs/acceptance/{purpose}.json", record)
            verdict = verify_acceptance(self.candidate, self.record["subject"], {**self.profile, "purpose": purpose})
            self.assertEqual(verdict["status"], "failed")
        path = self.candidate / "config/simulation-acceptance.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["tests"][0]["duration_s"] = 0.01
        write_json(path, config)
        self.assertEqual(self.check_record(self.record)["status"], "failed")

    def test_failed_test_cannot_be_turned_into_passed_record(self):
        config = json.loads((self.workspace.root / "config/simulation-acceptance.json").read_text(encoding="utf-8"))
        config["tests"][1]["thresholds"]["tracking_rmse_rad"] = 1e-12
        self.workspace.write_config(config)
        pending = build(self.workspace.root, "simulation")
        candidate = Path(pending["diagnostic_path"])
        output = self.base / "failed-test"
        record = run_acceptance(candidate, "simulation", Path("config/simulation-acceptance.json"), output)
        self.assertFalse(all(result["passed"] for result in record["results"]))
        shutil.copytree(output / "docs/acceptance", candidate / "docs/acceptance", dirs_exist_ok=True)
        for result in record["results"]:
            result.update(passed=True, failures=[])
        write_json(candidate / "docs/acceptance/simulation.json", record)
        verdict = verify_acceptance(candidate, record["subject"], profile_for(candidate, "simulation"))
        self.assertEqual(verdict["status"], "failed")

    def test_alternate_experiment_cannot_qualify_by_reusing_suite_names(self):
        config = json.loads((self.workspace.root / "config/simulation-acceptance.json").read_text(encoding="utf-8"))
        config["tests"][0]["duration_s"] = 0.02
        write_json(self.workspace.root / "config/cheap.json", config)
        pending = build(self.workspace.root, "simulation")
        candidate = Path(pending["diagnostic_path"])
        alternate = Path("config/cheap.json")
        with self.assertRaisesRegex(AcceptanceError, "config_not_canonical"):
            run_acceptance(candidate, "simulation", alternate, self.base / "cheap-local")
        output = self.base / "cheap-external"
        record = run_acceptance(candidate, "simulation", alternate, output, external=True)
        record["attestation"] = self.record["attestation"]
        shutil.copytree(output / "docs/acceptance", candidate / "docs/acceptance", dirs_exist_ok=True)
        write_json(candidate / "docs/acceptance/simulation.json", record)
        verdict = verify_acceptance(candidate, record["subject"], profile_for(candidate, "simulation"))
        self.assertEqual(verdict["status"], "failed")

    def test_missing_or_unknown_authority_never_qualifies_a_local_record(self):
        record = copy.deepcopy(self.record)
        del record["attestation"]
        self.assertEqual(self.check_record(record)["status"], "not_run")
        record["attestation"] = {"kind": "local"}
        self.assertEqual(self.check_record(record)["status"], "failed")
        record["attestation"] = {**self.record["attestation"], "trusted": True}
        self.assertEqual(self.check_record(record)["status"], "failed")

    def test_measurements_do_not_bypass_other_model_checks(self):
        self.workspace.profile(contact=None)
        pending = build(self.workspace.root, "simulation")
        self.assertIn("consumer.contact_parameters", pending["blockers"])
        with self.assertRaises(AcceptanceError):
            run_acceptance(
                Path(pending["diagnostic_path"]),
                "simulation",
                Path("config/simulation-acceptance.json"),
                self.base / "blocked",
            )

    def test_pathological_experiment_is_rejected_before_simulation(self):
        path = self.workspace.root / "config/simulation-acceptance.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config.update(timestep_s=0.000001, control_period_s=0.000001)
        config["tests"][0]["duration_s"] = 60.0
        write_json(path, config)
        with self.assertRaisesRegex(AcceptanceError, "config_workload"):
            load_config(path)

    def test_one_click_update_runs_acceptance_before_submission(self):
        root = self.workspace.root
        shutil.rmtree(root / "docs/acceptance")
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)

        def submit_verified(root, profile, message, *, ci=False):
            self.assertTrue((root / ".git/description-update.lock").exists())
            verdict = assess(root, profile)
            self.assertTrue(verdict["passed"], verdict["blockers"])
            return {"passed": True, "state": "pull_request_open", "pull_request": "https://example/pr/1"}

        with (
            patch(
                "description_pipeline.repository.update_preflight",
                return_value={"hardware": "fixture", "branch": "feature/fixture"},
            ),
            patch("description_pipeline.repository._submit", side_effect=submit_verified) as submitted,
        ):
            result = update(root, "simulation", reuse_source=True)
        self.assertTrue(result["ok"])
        submitted.assert_called_once()
        self.assertFalse((root / ".git/description-update.lock").exists())
        record = json.loads((root / "docs/acceptance/simulation.json").read_text(encoding="utf-8"))
        self.assertEqual(record["attestation"]["kind"], "local_replay")

    def test_failed_one_click_experiment_preserves_evidence_and_never_submits(self):
        root = self.workspace.root
        original = (root / "docs/acceptance/simulation.json").read_bytes()
        config = json.loads((root / "config/simulation-acceptance.json").read_text(encoding="utf-8"))
        config["tests"][1]["thresholds"]["tracking_rmse_rad"] = 1e-12
        self.workspace.write_config(config)
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)
        with (
            patch(
                "description_pipeline.repository.update_preflight",
                return_value={"hardware": "fixture", "branch": "feature/fixture"},
            ),
            patch("description_pipeline.repository._submit") as submitted,
            self.assertRaisesRegex(PipelineError, "Candidate does not qualify") as rejected,
        ):
            update(root, "simulation", reuse_source=True)
        submitted.assert_not_called()
        self.assertIsNotNone(rejected.exception.diagnostic_path)
        diagnostic = Path(str(rejected.exception.diagnostic_path))
        self.assertTrue((diagnostic / "acceptance.json").is_file())
        self.assertEqual((root / "docs/acceptance/simulation.json").read_bytes(), original)
        self.assertFalse((root / ".git/description-update.lock").exists())
