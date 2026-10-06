"""Pipeline identity: catalog integrity, CLI discovery, bindings and run bookkeeping."""

import contextlib
import copy
import importlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from description_pipeline import pipeline
from description_pipeline.build import assess, build, definition, freeze, subject_files
from description_pipeline.cli import main
from description_pipeline.io import PipelineError, read_data, write_json
from description_pipeline.repository import (
    _review_request,
    init_model,
    promote,
    promotion_plan,
    update,
)
from tests.pipeline.test_simulation_acceptance import Workspace
from description_pipeline.sources.snapshot import write_manifest


ROOT = Path(__file__).resolve().parents[2]


class PipelineIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.workspace = Workspace(self.base / "fixture")
        self.root = self.workspace.root

    def config(self) -> dict:
        return read_data(self.root / "config/robot.yaml")

    def source_lock(self) -> dict:
        return read_data(self.root / "sources/source.lock.json")

    def write_config(self, value: dict) -> None:
        write_json(self.root / "config/robot.yaml", value)

    # --- the catalog itself -------------------------------------------------

    def test_catalog_binds_real_code_and_documents(self):
        self.assertEqual(pipeline.known_ids(), ("fixture-to-urdf", "onshape-to-urdf", "solidworks-to-urdf"))
        for entry in pipeline.catalog():
            self.assertEqual(entry, pipeline.describe(entry["id"]))
            self.assertTrue(entry["source_kinds"])
            self.assertTrue(entry["stages"])
            for stage in entry["stages"]:
                module_name, _, attribute = stage["code"].partition(":")
                self.assertTrue(module_name and attribute, stage["code"])
                self.assertTrue(hasattr(importlib.import_module(module_name), attribute), stage["code"])
                self.assertTrue((ROOT / stage["document"]).is_file(), stage["document"])
            for document in entry["documents"]:
                self.assertTrue((ROOT / document).is_file(), document)

    def test_identity_resolution_prefers_the_real_source_kind(self):
        fixture = pipeline.resolve_identity(None, source={"provider": "fixture"})
        self.assertEqual(fixture["id"], "fixture-to-urdf")
        self.assertFalse(fixture["declared"])
        self.assertEqual(fixture["resolved_from"], "source_kind")
        replay = pipeline.resolve_identity(
            None, source={"provider": "snapshot"}, robot={"provider": "solidworks"}, frozen_kind="snapshot"
        )
        self.assertEqual(replay["id"], "solidworks-to-urdf")
        native = pipeline.resolve_identity(None, source={"provider": "fixture"}, frozen_kind="onshape")
        self.assertEqual(native["id"], "onshape-to-urdf")
        with self.assertRaisesRegex(PipelineError, "does not match"):
            pipeline.resolve_identity("solidworks-to-urdf", source={"provider": "fixture"})

    # --- CLI discovery ------------------------------------------------------

    def run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_cli_pipeline_list_and_show(self):
        status, out, _ = self.run_cli(["pipeline", "list", "--json"])
        self.assertEqual(status, 0)
        listed = json.loads(out)["pipelines"]
        self.assertEqual([entry["id"] for entry in listed], list(pipeline.known_ids()))

        status, out, _ = self.run_cli(["pipeline", "show", "solidworks-to-urdf", "--json"])
        self.assertEqual(status, 0)
        shown = json.loads(out)["pipeline"]
        self.assertEqual(shown["id"], "solidworks-to-urdf")
        self.assertTrue(shown["stages"] and shown["documents"])

        status, out, _ = self.run_cli(["pipeline", "show", "--root", str(self.root), "--json"])
        self.assertEqual(status, 0)
        workspace = json.loads(out)["workspace"]
        self.assertEqual(workspace["declared_id"], "fixture-to-urdf")
        self.assertEqual(workspace["identity"]["id"], "fixture-to-urdf")

        status, _, err = self.run_cli(["pipeline", "show", "not-a-pipeline"])
        self.assertEqual(status, 2)
        self.assertIn("Unknown pipeline_id", err)

        status, out, _ = self.run_cli(["pipeline", "list"])
        self.assertEqual(status, 0)
        self.assertIn("solidworks-to-urdf", out)

    # --- model init ---------------------------------------------------------

    def test_model_init_persists_the_pipeline_id(self):
        self.assertEqual(self.config()["pipeline_id"], "fixture-to-urdf")
        init_model(
            self.base / "solidworks",
            "m3",
            {
                "provider": "solidworks",
                "assembly": "D:/models/robot.SLDASM",
                "configuration": "Default",
                "allowed_roots": ["D:/models"],
                "worker_url": "http://127.0.0.1:8765",
            },
        )
        self.assertEqual(read_data(self.base / "solidworks/config/robot.yaml")["pipeline_id"], "solidworks-to-urdf")
        init_model(self.base / "onshape", "m3", {"provider": "onshape", "url": "https://cad.onshape.com/a"})
        self.assertEqual(read_data(self.base / "onshape/config/robot.yaml")["pipeline_id"], "onshape-to-urdf")

    # --- bindings -----------------------------------------------------------

    def test_unknown_or_mismatched_declaration_is_rejected_everywhere(self):
        config = self.config()
        config["pipeline_id"] = "not-a-pipeline"
        self.write_config(config)
        with self.assertRaisesRegex(PipelineError, "Unknown pipeline_id"):
            definition(self.root)
        with self.assertRaisesRegex(PipelineError, "Unknown pipeline_id"):
            freeze(self.root)

        config["pipeline_id"] = "solidworks-to-urdf"
        self.write_config(config)
        # A fixture/snapshot wrapper can carry any provider's data, so the mismatch is decided at
        # freeze time against the kind the snapshot actually carries - never silently remapped.
        with self.assertRaisesRegex(PipelineError, "does not match"):
            freeze(self.root)

    def test_lock_manifest_quality_and_api_results_carry_the_identity(self):
        locked = self.source_lock()
        self.assertEqual(locked["pipeline_id"], "fixture-to-urdf")
        self.assertEqual(
            locked["pipeline"],
            {
                "schema_version": pipeline.SCHEMA,
                "id": "fixture-to-urdf",
                "declared": True,
                "resolved_from": "config/robot.yaml",
                "source_kind": "fixture",
            },
        )
        report = build(self.root, "kinematics")
        self.assertTrue(report["passed"], report["blockers"])
        self.assertEqual(report["pipeline_id"], "fixture-to-urdf")
        self.assertEqual(report["pipeline"]["declared"], True)
        self.assertEqual(read_data(self.root / "manifest.json")["pipeline_id"], "fixture-to-urdf")
        self.assertEqual(read_data(self.root / "docs/quality.json")["pipeline_id"], "fixture-to-urdf")
        self.assertIn("Pipeline: `fixture-to-urdf`", (self.root / "docs/quality.md").read_text(encoding="utf-8"))
        self.assertEqual(freeze(self.root)["pipeline_id"], "fixture-to-urdf")
        self.assertEqual(assess(self.root, "kinematics")["pipeline_id"], "fixture-to-urdf")

    def test_removal_mismatch_and_tamper_are_rejected(self):
        build(self.root, "kinematics")

        config = self.config()
        declared = config.pop("pipeline_id")
        self.write_config(config)
        with self.assertRaisesRegex(PipelineError, "freeze the source again|predates"):
            assess(self.root, "kinematics")

        config["pipeline_id"] = declared
        self.write_config(config)
        locked = self.source_lock()
        locked["pipeline"]["id"] = "onshape-to-urdf"
        locked["pipeline_id"] = "onshape-to-urdf"
        write_json(self.root / "sources/source.lock.json", locked)
        with self.assertRaisesRegex(PipelineError, "Pipeline identity changed"):
            assess(self.root, "kinematics")

        freeze(self.root)
        build(self.root, "kinematics")
        manifest = read_data(self.root / "manifest.json")
        manifest["pipeline_id"] = "onshape-to-urdf"
        write_json(self.root / "manifest.json", manifest)
        report = assess(self.root, "kinematics")
        self.assertFalse(report["passed"])
        self.assertIn("bundle.identity", report["blockers"])

        build(self.root, "kinematics")
        quality = read_data(self.root / "docs/quality.json")
        quality["pipeline_id"] = "onshape-to-urdf"
        write_json(self.root / "docs/quality.json", quality)
        report = assess(self.root, "kinematics")
        self.assertFalse(report["passed"])

    def test_legacy_definition_and_lock_take_the_explicit_default(self):
        config = self.config()
        config.pop("pipeline_id")
        self.write_config(config)
        locked = self.source_lock()
        locked.pop("pipeline", None)
        locked.pop("pipeline_id", None)
        write_json(self.root / "sources/source.lock.json", locked)

        report = build(self.root, "kinematics")
        self.assertTrue(report["passed"], report["blockers"])
        self.assertEqual(report["pipeline_id"], "fixture-to-urdf")
        self.assertFalse(report["pipeline"]["declared"])
        self.assertEqual(report["pipeline"]["resolved_from"], "legacy_lock")

        config["pipeline_id"] = "fixture-to-urdf"
        self.write_config(config)
        with self.assertRaisesRegex(PipelineError, "predates pipeline identities"):
            assess(self.root, "kinematics")
        freeze(self.root)
        self.assertTrue(assess(self.root, "kinematics")["pipeline"]["declared"])

    def test_inspection_and_torn_locks_fail_closed(self):
        locked = self.source_lock()
        locked["manifest_digest"] = "0" * 64
        write_json(self.root / "sources/source.lock.json", locked)
        with self.assertRaisesRegex(PipelineError, "Source lock does not match snapshot"):
            pipeline.workspace_identity(self.root)
        status, _, err = self.run_cli(["pipeline", "show", "--root", str(self.root), "--json"])
        self.assertEqual(status, 2)
        self.assertIn("Source lock does not match snapshot", err)

        freeze(self.root)
        write_json(self.root / "sources/source.lock.json", {**self.source_lock(), "pipeline": None})
        with self.assertRaisesRegex(PipelineError, "Unsupported pipeline identity"):
            pipeline.workspace_identity(self.root)
        torn = self.source_lock()
        torn.pop("pipeline")
        write_json(self.root / "sources/source.lock.json", torn)
        with self.assertRaisesRegex(PipelineError, "torn identity"):
            pipeline.workspace_identity(self.root)

    def test_native_onshape_replay_binds_identity_without_the_api(self):
        """A snapshot captured from Onshape resolves to onshape-to-urdf without any API call."""

        self.assertTrue(build(self.root, "kinematics")["passed"])
        canonical = read_data(self.root / "model/robot.json")
        # Re-label the frozen fixture as a captured Onshape snapshot (fixture evidence class), the
        # shape a native replay has once it reaches a build machine.
        write_manifest(
            self.workspace.source,
            kind="onshape",
            identity={"provider": "onshape", "document_id": "doc", "workspace_id": "ws"},
            evidence_class="fixture",
        )
        config = self.config()
        config["pipeline_id"] = "onshape-to-urdf"
        self.write_config(config)
        frozen = freeze(self.root)
        self.assertEqual(frozen["pipeline_id"], "onshape-to-urdf")
        self.assertEqual(frozen["pipeline"]["source_kind"], "onshape")

        with (
            patch("description_pipeline.build.normalize", side_effect=lambda *a, **k: copy.deepcopy(canonical)),
            patch("description_pipeline.sources.onshape.verify_normalization", return_value=[]),
        ):
            report = build(self.root, "kinematics")
            self.assertTrue(report["passed"], report["blockers"])
            self.assertEqual(report["pipeline_id"], "onshape-to-urdf")
            checked = assess(self.root, "kinematics")
        self.assertTrue(checked["passed"], checked["blockers"])
        self.assertEqual(checked["pipeline_id"], "onshape-to-urdf")
        self.assertTrue(checked["pipeline"]["declared"])

    def test_import_error_keeps_the_structured_cli_diagnostic(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch(
                "description_pipeline.cli.assess",
                side_effect=ImportError("DLL load failed while importing _callbacks"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = main(["check", "--root", str(self.root), "--profile", "kinematics"])
        self.assertEqual(status, 2)
        value = json.loads(stderr.getvalue())
        self.assertFalse(value["passed"])
        self.assertEqual(value["error"], "ImportError")
        self.assertIn("_callbacks", value["message"])
        self.assertTrue(value["run_id"])

    def test_wrapped_app_control_import_error_reuses_the_doctor_classifier(self):
        class BlockedOSError(OSError):
            winerror = 4551

        blocked = BlockedOSError("[WinError 4551] 策略阻止了此文件")
        blocked.filename = r"C:\runtime\mujoco\plugin\elasticity.dll"
        error = ImportError("DLL load failed while importing _callbacks")
        error.__cause__ = blocked
        structured = {
            "code": "windows_app_control",
            "winerror": 4551,
            "library": blocked.filename,
            "error": f"OSError: {blocked}",
        }

        def classify(candidate):
            return structured if getattr(candidate, "winerror", None) == 4551 else None

        stderr = io.StringIO()
        with (
            patch("description_pipeline.doctor.app_control_rejection", side_effect=classify),
            patch("description_pipeline.cli.assess", side_effect=error),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            status = main(["check", "--root", str(self.root), "--profile", "kinematics"])
        self.assertEqual(status, 2)
        value = json.loads(stderr.getvalue())
        self.assertEqual(value["code"], "windows_app_control")
        self.assertEqual(value["winerror"], 4551)
        self.assertIn("elasticity.dll", value["message"])

    # --- run bookkeeping ----------------------------------------------------

    def test_run_records_do_not_touch_the_subject_or_reproducible_artifacts(self):
        self.assertIn("build/", (self.root / ".gitignore").read_text(encoding="utf-8"))
        self.assertTrue(build(self.root, "kinematics")["passed"])
        before = subject_files(self.root)
        status, out, _ = self.run_cli(["check", "--root", str(self.root), "--profile", "kinematics"])
        self.assertEqual(status, 0)
        value = json.loads(out)
        run_id = value["run_id"]
        self.assertTrue(run_id)
        record_path = self.root / "build/runs" / f"{run_id}.json"
        self.assertTrue(record_path.is_file())
        record = read_data(record_path)
        self.assertEqual(record["command"], "check")
        self.assertEqual(record["outcome"], "passed")
        self.assertEqual(record["pipeline_id"], "fixture-to-urdf")
        self.assertIsNone(record["source_job_id"])
        self.assertEqual(record["run_id"], run_id)
        self.assertEqual(subject_files(self.root), before)
        self.assertNotIn(record_path.relative_to(self.root).as_posix(), subject_files(self.root))

        quality = (self.root / "docs/quality.json").read_bytes()
        manifest = (self.root / "manifest.json").read_bytes()
        status, out, _ = self.run_cli(["check", "--root", str(self.root), "--profile", "kinematics"])
        self.assertEqual(status, 0)
        second = json.loads(out)["run_id"]
        self.assertNotEqual(run_id, second)
        self.assertEqual((self.root / "docs/quality.json").read_bytes(), quality)
        self.assertEqual((self.root / "manifest.json").read_bytes(), manifest)
        self.assertEqual(subject_files(self.root), before)

        # A command whose result is not a boolean (source freeze returns the lock) is recorded as
        # completed, not failed, and still carries the resolved pipeline id.
        status, out, _ = self.run_cli(["source", "freeze", "--root", str(self.root)])
        self.assertEqual(status, 0)
        freeze_run = read_data(self.root / "build/runs" / f"{json.loads(out)['run_id']}.json")
        self.assertEqual(freeze_run["outcome"], "completed")
        self.assertEqual(freeze_run["pipeline_id"], "fixture-to-urdf")

    # --- results, PR and release carry the identity -------------------------

    def test_update_pr_and_promotion_carry_the_pipeline_id(self):
        self.assertTrue(build(self.root, "kinematics")["passed"])
        report = assess(self.root, "kinematics")
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        with (
            patch(
                "description_pipeline.repository.update_preflight",
                return_value={"hardware": "fixture", "branch": "feature/fixture"},
            ),
            patch("description_pipeline.repository.build", return_value={**report, "blockers": []}),
            patch(
                "description_pipeline.repository._submit",
                return_value={"passed": True, "state": "pull_request_open", "pipeline_id": "fixture-to-urdf"},
            ),
        ):
            result = update(self.root, "kinematics", reuse_source=True)
        self.assertEqual(result["pipeline_id"], "fixture-to-urdf")
        self.assertEqual(result["build"]["pipeline_id"], "fixture-to-urdf")

        captured: dict[str, str] = {}

        def fake_github(path, payload=None, method="GET"):
            if payload:
                captured["body"] = str(payload["body"])
            return [] if payload is None else {"number": 1, "html_url": "https://example/pr/1"}

        with (
            patch("description_pipeline.repository.repository_slug", return_value="owner/repo"),
            patch("description_pipeline.repository.github_api", side_effect=fake_github),
        ):
            _review_request(self.root, "work/model/fixture/change", "fixture", "a" * 40, "kinematics", report, "msg")
        self.assertIn("Pipeline: `fixture-to-urdf`", captured.get("body", ""))

        git_result = CompletedProcess([], 0, stdout="", stderr="")
        release_report = {
            **report,
            "source": {**report["source"], "evidence_class": "cad"},
            "toolchain": {"source_commit": "b" * 40, "development": False},
        }
        with (
            patch("description_pipeline.repository._require_checkout"),
            patch("description_pipeline.repository.git", return_value=git_result),
            patch("description_pipeline.repository.validate_commit", return_value=release_report),
            patch("description_pipeline.repository._accepted_tool_main", return_value="public-main"),
        ):
            plan = promotion_plan(self.root, "fixture", "1" * 40, "kinematics")
        self.assertEqual(plan["pipeline_id"], "fixture-to-urdf")
        with (
            patch(
                "description_pipeline.repository.promotion_plan",
                return_value={**plan, "pipeline_id": "onshape-to-urdf"},
            ),
            patch("description_pipeline.repository.push"),
            self.assertRaisesRegex(PipelineError, "pipeline_id"),
        ):
            promote(self.root, plan)


if __name__ == "__main__":
    unittest.main()
