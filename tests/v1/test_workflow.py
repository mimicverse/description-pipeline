"""Workflow failure/ownership controls; these tests do not qualify native CAD."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks, steps
from description_pipeline.cli import main
from description_pipeline.delivery import BUNDLE_SCHEMA, PIPELINE_ID
from description_pipeline.io import PipelineError, digest, file_digest, read_data, write_json
from description_pipeline.sources.solidworks.revision import package_inventory, seal_revision
from description_pipeline.stages import stage_view


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.package = self.root / "handoff"
        self.package.mkdir()
        (self.package / "assembly.SLDASM").write_bytes(b"non-native control fixture")
        (self.package / "part.SLDPRT").write_bytes(b"non-native control fixture part")
        definition = {
            "schema_version": "solidworks-to-urdf.input/v1",
            "hardware_id": "arm",
            "source": {
                "provider": "solidworks",
                "robot_name": "arm",
                "assembly": "assembly.SLDASM",
                "configuration": "Default",
                "material_source": "cad",
                "bodies": [
                    {
                        "id": "base",
                        "name": "base_link",
                        "components": ["part-1"],
                        "frame": {"coordinate_system": "base_datum"},
                    },
                    {
                        "id": "arm",
                        "name": "arm_link",
                        "components": ["part-2"],
                        "frame": {"coordinate_system": "arm_datum"},
                    },
                ],
                "joints": [
                    {
                        "id": "base_to_arm",
                        "name": "arm_fixed_joint",
                        "type": "fixed",
                        "parent": "base_link",
                        "child": "arm_link",
                    }
                ],
            },
            "checks": {"expected_mass_kg": [0.01, 1.0], "expected_extent_m": [0.001, 1.0]},
        }
        # JSON is a YAML subset; no SolidWorks capture or green verifier is mocked here.
        (self.package / "robot.yaml").write_text(json.dumps(definition))
        self.output = self.root / "delivery"

    def seal(self):
        seal_revision(
            self.package,
            hardware_id="arm",
            revision="r1",
            owner="mechanical",
            system="handoff",
            reference="fixture/arm/r1",
            summary="Control test",
        )

    def test_inspection_requires_mechanical_revision(self):
        report = steps.inspect_prepared_input(self.package)
        self.assertFalse(report["passed"])
        self.assertIn("input.cad_revision", [e["code"] for e in report["errors"]])
        self.seal()
        self.assertTrue(steps.inspect_prepared_input(self.package)["passed"])

    def test_linux_never_opens_native_cad_and_keeps_failure_evidence(self):
        self.seal()
        events = []
        with (
            patch.object(solidworks.sys, "platform", "linux"),
            patch("description_pipeline.sources.solidworks.freeze.freeze") as native,
        ):
            result = solidworks.run(self.package, self.output, on_event=events.append)
        native.assert_not_called()
        self.assertFalse(result["passed"])
        self.assertIn("Windows", result["error"])
        self.assertTrue(result["run_id"])
        self.assertEqual("failed", events[-1]["state"])
        self.assertFalse(self.output.exists())
        self.assertTrue((Path(result["diagnostic_path"]) / "reports/input.json").is_file())

    def test_failed_input_does_not_replace_existing_owned_output(self):
        self.output.mkdir()
        marker = self.output / "README.md"
        marker.write_text("Existing output bytes")
        solidworks._stamp(self.output, {"schema_version": BUNDLE_SCHEMA, "pipeline_id": PIPELINE_ID})
        result = solidworks.run(self.package, self.output)
        self.assertFalse(result["passed"])
        self.assertEqual("Existing output bytes", marker.read_text())
        self.assertTrue(solidworks._owned_output(self.output))

    def test_native_preflight_failure_blocks_capture_and_retains_diagnostics(self):
        self.seal()
        error = PipelineError("Native readiness failed")
        error.details = {"reason": "SolidWorks registration unavailable"}
        with (
            patch.object(solidworks.sys, "platform", "win32"),
            patch("description_pipeline.runtime.native_readiness", side_effect=error),
            patch("description_pipeline.sources.solidworks.freeze.freeze") as capture,
        ):
            result = solidworks.run(self.package, self.output)
        capture.assert_not_called()
        self.assertFalse(result["passed"])
        self.assertEqual(result["stage"], "capture")
        self.assertEqual(result["error"], "Native readiness failed")
        self.assertEqual(result["detail"], error.details)
        self.assertTrue((Path(result["diagnostic_path"]) / "reports/run.json").is_file())
        root = Path(result["diagnostic_path"])
        receipt = read_data(root / "reports/run.json")
        self.assertEqual(receipt["stage_report"]["sha256"], file_digest(root / "reports/stages.json"))
        view = read_data(root / "reports/stages.json")
        capture = next(stage for stage in view["stages"] if stage["id"] == "capture")
        self.assertEqual(capture["state"], "failed")
        self.assertEqual(capture["input_qc"][1]["details"]["diagnostic"], error.details)
        self.assertTrue(all(stage["state"] == "blocked" for stage in view["stages"][3:]))

    def test_prepared_inputs_are_bound_to_discovery_before_any_capture(self):
        self.seal()
        expected = package_inventory(self.package)
        (self.package / "assembly.SLDASM").write_bytes(b"Changed after discovery")
        with patch("description_pipeline.sources.solidworks.freeze.freeze") as native:
            result = solidworks.run(self.package, self.output, expected_inputs=expected)
        native.assert_not_called()
        self.assertFalse(result["passed"])
        self.assertIn("changed after discovery", result["error"])
        check = next(stage for stage in stage_view(result)["stages"] if stage["id"] == "capture")["input_qc"][0]
        self.assertEqual(check["state"], "failed")

    def test_static_inspection_is_consumed_once_and_changed_inspected_bytes_are_rejected(self):
        from description_pipeline.sources.solidworks.input import inspect_package

        self.seal()
        inspect_prepared = steps.inspect_prepared_input

        def change_after_inspection(package):
            report = inspect_prepared(package)
            (package / "part.SLDPRT").write_bytes(b"Changed after inspection")
            return report

        with (
            patch("description_pipeline.sources.solidworks.input.inspect_package", wraps=inspect_package) as inspect,
            patch.object(steps, "inspect_prepared_input", side_effect=change_after_inspection),
            patch("description_pipeline.sources.solidworks.freeze.freeze") as native,
        ):
            result = solidworks.run(self.package, self.output)
        inspect.assert_called_once()
        native.assert_not_called()
        self.assertIn("static validation", result["error"])
        self.assertFalse(result["passed"])

    def test_capture_input_qc_records_handoff_and_frozen_file_binding(self):
        from description_pipeline.verification.consumer import ConsumerError

        self.seal()
        expected = package_inventory(self.package)
        handoff = "c" * 64
        error = ConsumerError("Consumer loading failed", stderr="ImportError: control")
        with (
            patch.object(solidworks.sys, "platform", "win32"),
            patch("description_pipeline.runtime.native_readiness", side_effect=error),
            patch("description_pipeline.sources.solidworks.freeze.freeze") as native,
        ):
            result = solidworks.run(self.package, self.output, expected_inputs=expected, handoff_sha256=handoff)
        native.assert_not_called()
        self.assertFalse(result["passed"])
        check = next(stage for stage in stage_view(result)["stages"] if stage["id"] == "capture")["input_qc"][0]
        self.assertEqual(check["id"], "input.valid")
        self.assertEqual(check["state"], "passed")
        self.assertTrue(check["details"]["passed"])
        self.assertEqual(check["details"]["handoff_sha256"], handoff)
        self.assertEqual(check["details"]["files_sha256"], digest(expected))
        self.assertEqual(check["details"]["files"], {"input/" + path: value for path, value in expected.items()})

    def test_snapshot_details_rejects_a_manifest_changed_after_verification(self):
        from description_pipeline import steps
        from description_pipeline.io import PipelineError
        from description_pipeline.sources.snapshot import SCHEMA, verify_snapshot

        with tempfile.TemporaryDirectory() as directory:
            delivery = Path(directory) / "delivery"
            evidence = delivery / "evidence"
            evidence.mkdir(parents=True)
            scene = evidence / "scene.json"
            scene.write_text(json.dumps({"synthetic": True}))
            manifest_path = evidence / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA,
                        "evidence_class": "fixture",
                        "kind": "fixture",
                        "identity": {"id": "adversarial control"},
                        "files": {"scene.json": file_digest(scene)},
                        "scene": "scene.json",
                    }
                )
            )
            manifest = verify_snapshot(evidence)
            details = steps._snapshot_details(delivery, manifest)
            self.assertEqual(details["manifest_sha256"], file_digest(manifest_path))
            self.assertEqual(details["files"]["evidence/scene.json"], file_digest(scene))
            mutated = json.loads(manifest_path.read_text())
            mutated["files"]["ghost/never-written.bin"] = "0" * 64
            manifest_path.write_text(json.dumps(mutated))
            with self.assertRaisesRegex(PipelineError, "changed after verification"):
                steps._snapshot_details(delivery, manifest)

    def test_rebuild_records_only_executed_steps_without_claiming_native_replay(self):
        from .protocol_support import protocol_events
        from description_pipeline.delivery import subject_digest

        self.seal()
        bundle = self.root / "frozen"
        (bundle / "reports").mkdir(parents=True)
        write_json(bundle / "reports/input.json", {"synthetic": True})
        write_json(bundle / "reports/run.json", {"run_id": "source-run"})
        import shutil

        shutil.copytree(self.package, bundle / "input")
        (bundle / "evidence").mkdir()
        (bundle / "evidence/raw.json").write_text('"Synthetic driver fixture"')
        for name in ("model", "urdf", "meshes"):
            (bundle / name).mkdir()
        (bundle / "README.md").write_text("Synthetic driver fixture")
        write_json(bundle / "reports/tool.json", {})
        source_subject = subject_digest(bundle)

        def generate(staging, definition, report, *, on_event):
            for event in protocol_events(stages=("generate",), subject="a" * 64):
                on_event(event)
            return "a" * 64

        def verify(staging, subject, *, on_event):
            for event in protocol_events(stages=("verify",), subject=subject):
                on_event(event)
            return {"passed": True, "subject_sha256": subject, "scope": "Synthetic driver; no native qualification"}

        with (
            patch.object(solidworks, "_check", return_value={"passed": True, "subject_sha256": source_subject}),
            patch.object(steps, "generate_model", side_effect=generate),
            patch.object(steps, "verify_delivery", side_effect=verify),
        ):
            result = solidworks.rebuild(bundle, self.output)
        self.assertTrue(result["passed"])
        self.assertEqual(result["rebuild_from"]["run_id"], "source-run")
        self.assertEqual(result["rebuild_from"]["subject_sha256"], source_subject)
        view = read_data(self.output / "reports/stages.json")
        self.assertEqual(view["execution_scope"], ["generate", "verify"])
        self.assertEqual([row["id"] for row in view["stages"] if row["state"] == "completed"], ["generate", "verify"])
        self.assertTrue(all(row["state"] == "not_run" for row in view["stages"] if not row["in_scope"]))
        self.assertTrue(solidworks._owned_output(self.output))

    def test_rebuild_refuses_a_frozen_delivery_that_changed_after_verification(self):
        bundle = self.root / "frozen"
        (bundle / "input").mkdir(parents=True)
        write_json(bundle / "input/robot.yaml", {"synthetic": True})
        (bundle / "evidence").mkdir()
        (bundle / "evidence/raw.json").write_text('"Synthetic driver fixture"')
        for name in ("model", "urdf", "meshes"):
            (bundle / name).mkdir()
        (bundle / "README.md").write_text("Synthetic driver fixture")
        write_json(bundle / "reports/input.json", {"synthetic": True})
        write_json(bundle / "reports/tool.json", {})
        with (
            patch.object(solidworks, "_check", return_value={"passed": True, "subject_sha256": "0" * 64}),
            self.assertRaisesRegex(PipelineError, "changed after verification"),
        ):
            solidworks.rebuild(bundle, self.output)
        self.assertFalse(self.output.exists())

    def test_rebuild_refuses_frozen_evidence_changed_during_preparation_and_records_handoff(self):
        import shutil

        from description_pipeline.delivery import subject_digest

        self.seal()
        bundle = self.root / "frozen"
        (bundle / "reports").mkdir(parents=True)
        write_json(bundle / "reports/run.json", {"run_id": "source-run"})
        shutil.copytree(self.package, bundle / "input")
        definition = read_data(bundle / "input/robot.yaml")
        definition["provenance"] = {"native_inventory_sha256": "d" * 64}
        write_json(bundle / "input/robot.yaml", definition)
        (bundle / "evidence").mkdir()
        (bundle / "evidence/raw.json").write_text('"Synthetic driver fixture"')
        for name in ("model", "urdf", "meshes"):
            (bundle / name).mkdir()
        (bundle / "README.md").write_text("Synthetic driver fixture")
        write_json(bundle / "reports/input.json", {"synthetic": True})
        write_json(bundle / "reports/tool.json", {})
        source_subject = subject_digest(bundle)
        real_copytree = shutil.copytree
        copies = []

        def copying(source, destination, **kwargs):
            result = real_copytree(source, destination, **kwargs)
            copies.append(str(destination))
            if len(copies) == 2:
                (bundle / "evidence/raw.json").write_text('"Changed during rebuild preparation"')
            return result

        with (
            patch.object(solidworks, "_check", return_value={"passed": True, "subject_sha256": source_subject}),
            patch.object(solidworks.shutil, "copytree", side_effect=copying),
        ):
            result = solidworks.rebuild(bundle, self.output)
        self.assertFalse(result["passed"])
        self.assertIn("Frozen evidence changed during rebuild preparation", result["error"])
        self.assertEqual(result["rebuild_from"]["run_id"], "source-run")
        self.assertEqual(result["rebuild_from"]["handoff_sha256"], "d" * 64)
        self.assertEqual(result["handoff_sha256"], "d" * 64)
        self.assertFalse(self.output.exists())

    def test_publication_retry_retains_prior_attempts_and_resets_scope_to_publish(self):
        from .protocol_support import protocol_events

        self.output.mkdir()
        prior = {
            "passed": True,
            "state": "published",
            "subject_sha256": "a" * 64,
            "url": "https://github.com/example/control/pull/1",
        }
        receipt = {
            "schema_version": BUNDLE_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": "retry-run",
            "execution_scope": ["generate", "verify", "publish"],
            "submission": prior,
            "events": protocol_events(stages=("generate", "verify", "publish"), subject="a" * 64),
        }
        solidworks._stamp(self.output, receipt)
        previous = list(receipt["events"])

        def recover(bundle, repository, *, base, message, on_event):
            for event in protocol_events(stages=("publish",), subject="a" * 64):
                on_event(event)
            return {
                "passed": True,
                "state": "noop",
                "subject_sha256": "a" * 64,
                "url": "https://github.com/example/control/pull/1",
                "scope": "Synthetic publication retry",
            }

        with patch.object(steps, "publish_model", side_effect=recover):
            result = solidworks.submit(self.output, self.root / "repository")
        self.assertTrue(result["passed"])
        after = read_data(self.output / "reports/run.json")
        self.assertEqual(after["execution_scope"], ["publish"])
        self.assertEqual(after["publication_attempts"], [prior, result])
        self.assertEqual(after["events"][: len(previous)], previous)
        self.assertEqual(after["submission"], result)
        self.assertEqual(after["stage_report"]["sha256"], file_digest(self.output / "reports/stages.json"))
        self.assertTrue(solidworks._owned_output(self.output))

    def test_publication_retry_keeps_history_and_updates_the_hashed_receipt(self):
        from .protocol_support import protocol_events

        self.output.mkdir()
        receipt = {
            "schema_version": BUNDLE_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": "retry-run",
            "execution_scope": ["publish"],
            "events": protocol_events(stages=("publish",), failed_stage="publish"),
        }
        solidworks._stamp(self.output, receipt)
        previous = list(receipt["events"])

        def recover(bundle, repository, *, base, message, on_event):
            for event in protocol_events(stages=("publish",), subject="a" * 64):
                on_event(event)
            return {
                "passed": True,
                "state": "published",
                "subject_sha256": "a" * 64,
                "url": "https://github.com/example/control/pull/1",
                "scope": "Synthetic publication retry",
            }

        with patch.object(steps, "publish_model", side_effect=recover):
            result = solidworks.submit(self.output, self.root / "repository")
        self.assertTrue(result["passed"])
        after = read_data(self.output / "reports/run.json")
        self.assertEqual(after["events"][: len(previous)], previous)
        self.assertEqual(after["publication_attempts"], [result])
        self.assertEqual(after["stage_report"]["sha256"], file_digest(self.output / "reports/stages.json"))
        self.assertTrue(solidworks._owned_output(self.output))

    def test_annotated_diagnostic_is_preserved_on_retry(self):
        first = solidworks.run(self.package, self.output)
        old = Path(first["diagnostic_path"])
        note = old / "operator-note.txt"
        note.write_text("Do not erase this review")
        second = solidworks.run(self.package, self.output)
        self.assertNotEqual(first["diagnostic_path"], second["diagnostic_path"])
        self.assertEqual("Do not erase this review", note.read_text())

    def test_output_and_input_cannot_overlap(self):
        before = (self.package / "robot.yaml").read_bytes()
        with self.assertRaises(PipelineError):
            solidworks.run(self.package, self.package / "output")
        self.assertEqual(before, (self.package / "robot.yaml").read_bytes())

    def test_report_option_cannot_overwrite_delivery_input(self):
        before = (self.package / "robot.yaml").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = main(["check", str(self.package), "--report", str(self.package / "robot.yaml")])
        self.assertEqual(1, result)
        self.assertEqual(before, (self.package / "robot.yaml").read_bytes())

    def test_original_cad_is_archived_without_editing(self):
        self.seal()
        target = self.root / "staging"
        target.mkdir()
        original = (self.package / "assembly.SLDASM").read_bytes()
        steps._archive_input(self.package, target)
        self.assertEqual(original, (target / "input/assembly.SLDASM").read_bytes())
        self.assertEqual(original, (self.package / "assembly.SLDASM").read_bytes())
        self.assertTrue((target / "input/cad-revision.json").is_file())

    def test_concurrent_output_writer_is_refused(self):
        with solidworks.output_lock(self.output), self.assertRaises(PipelineError), solidworks.output_lock(self.output):
            self.fail("Second writer acquired the same output lock")


if __name__ == "__main__":
    unittest.main()
