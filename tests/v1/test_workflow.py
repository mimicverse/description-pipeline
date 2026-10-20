"""Workflow failure/ownership controls; these tests do not qualify native CAD."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks, steps
from description_pipeline.cli import main
from description_pipeline.io import PipelineError, digest, file_digest, inventory, read_data, write_json
from description_pipeline.sources.solidworks.revision import package_inventory
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
        from description_pipeline.schemas import SOURCE_RECORD_SCHEMA

        identity = {
            "kind": "source-revision",
            "handoff_digest": digest({"fixture": "assembly.SLDASM"}),
            "main_assembly": "assembly.SLDASM",
            "configuration": "Default",
            "hardware_id": "arm",
            "records": [],
        }
        write_json(
            self.package / "source.json",
            {
                "schema_version": SOURCE_RECORD_SCHEMA,
                "source_revision_id": digest(identity),
                "identity": identity,
                "selection": {
                    "main_assembly": "assembly.SLDASM",
                    "configuration": "Default",
                    "hardware_id": "arm",
                    "hardware_source": "cad",
                },
                "metadata": {
                    "revision": "r1",
                    "owner": "mechanical",
                    "control": {"system": "handoff", "reference": "fixture/arm/r1"},
                    "change_summary": "Control test",
                    "change_summary_source": "submission",
                },
            },
        )

    def test_inspection_defers_until_the_content_identity_record_exists(self):
        report = steps.inspect_prepared_input(self.package)
        self.assertTrue(report["passed"], report["errors"])
        self.assertIn("input.source_identity_pending", [row["code"] for row in report["warnings"]])
        self.seal()
        report = steps.inspect_prepared_input(self.package)
        self.assertTrue(report["passed"], report["errors"])
        self.assertNotIn("input.source_identity_pending", [row["code"] for row in report["warnings"]])
        self.assertEqual(report["cad_revision"]["hardware_id"], "arm")
        self.assertEqual(report["cad_revision"]["revision"], "r1")

    def test_replay_uses_the_native_robot_name_without_a_registry(self):
        staging = self.root / "replay"
        write_json(
            staging / "input/handoff.json",
            {
                "passed": True,
                "package_files": {},
                "input": {"files": 0},
                "cad_revision": {"hardware_id": "m3.0", "revision": None},
                "input_receipt": {"inventory": []},
            },
        )
        write_json(
            staging / "evidence/discovery.json",
            {
                "identity": {
                    "hardware_id": "m3.0",
                    "robot_name": "cad_robot",
                    "robot_name_technical": "m3_0",
                },
            },
        )
        with patch.object(steps, "inspect_prepared_input", return_value=read_data(staging / "input/handoff.json")):
            definition, _report = solidworks._replay_seeded_inputs(staging)
        self.assertEqual(definition["source"]["robot_name"], "cad_robot")
        self.assertEqual(definition["source"]["robot_name_source"], "cad")
        self.assertEqual(definition["hardware_id"], "m3.0")

    def acquisition_without_admin(self):
        """Byte-binding control only; these placeholders do not qualify CAD."""
        from tests.sources.selection_support import sealed_selection

        author = read_data(self.package / "robot.yaml")
        author["hardware_id"] = None
        author["source"]["robot_name"] = "robot"
        files = {
            name: checksum
            for name, checksum in package_inventory(self.package).items()
            if name not in ("robot.yaml", "source.json")
        }
        identity = {
            "hardware_id": None, "revision": None, "owner": None, "change_summary": None,
            "control": None, "robot_name": None, "main_assembly": "assembly.SLDASM",
        }
        record = {
            "schema_version": "solidworks-to-urdf.native-discovery/v1",
            "contract": "native-discovery/v1", "run_id": "metadata-control",
            "handoff_sha256": digest(files), "native_files": files, "identity": identity,
            "derivation": {"main_assembly": "assembly.SLDASM"},
            "raw": {
                "saved_configuration": "Default",
                "files": dict(files),
                "source_selection": sealed_selection(files, "assembly.SLDASM"),
                "components": [
                    {
                        "instance_id": "assembly-1",
                        "document": "assembly.SLDASM",
                        "suppression_state": 2,
                    },
                    {
                        "instance_id": "part-1",
                        "document": "part.SLDPRT",
                        "suppression_state": 2,
                    },
                ],
            },
        }
        record_path = self.package / "discovery/native-discovery.json"
        write_json(record_path, record)
        author["provenance"] = {
            "generator": "native-discovery", "generator_version": "1", "contract": record["contract"],
            "run_id": record["run_id"], "discovery_sha256": file_digest(record_path),
            "native_inventory_sha256": digest(files),
        }
        write_json(self.package / "robot.yaml", author)
        # The sealed pre-acquisition capture plan: the only plan that travels to
        # the native side, bound to the sealed discovery bytes and the frozen
        # inventory (the exact field set the consumer requires).
        write_json(
            self.package / "capture-plan.json",
            {
                "schema_version": "solidworks-to-urdf.capture-plan/v1",
                "run_id": record["run_id"],
                "handoff_sha256": digest(files),
                "discovery_sha256": file_digest(record_path),
                "main_assembly": "assembly.SLDASM",
                "configuration": "Default",
                "identity": {"hardware_id": None, "revision": None},
                "selection": {"files": dict(sorted(files.items()))},
                "objects": [
                    {"name": "assembly-1", "document": "assembly.SLDASM", "state": 2},
                    {"name": "part-1", "document": "part.SLDPRT", "state": 2},
                ],
                "groups": [{"name": "assembly", "components": ["assembly-1", "part-1"]}],
                "axis_requests": [],
                "datums": [],
                "evidence_requests": list(steps._CAPTURE_EVIDENCE_REQUESTS),
            },
        )
        return author, record

    def test_bound_missing_admin_allows_inspection_but_not_publication(self):
        self.acquisition_without_admin()
        report = steps.inspect_prepared_input(self.package)
        self.assertTrue(report["passed"], report["errors"])
        self.assertIsNone(report["input"])
        self.assertIsNone(report["cad_revision"])
        self.assertIn("input.source_identity_pending", [row["code"] for row in report["warnings"]])
        bundle = self.root / "unpublished"
        bundle.mkdir()
        shutil.copytree(self.package, bundle / "input")
        with (
            patch.object(
                steps, "tool_record", return_value={"release": {"schema_version": "solidworks-to-urdf.release/v1"}}
            ),
            patch("description_pipeline.repository.urdf_pr.submit_bundle") as publish,
            self.assertRaisesRegex(PipelineError, "input/source.json"),
        ):
            steps.publish_model(bundle, self.root / "repository")
        publish.assert_not_called()

    def test_malformed_content_identity_is_refused(self):
        write_json(self.package / "source.json", {"schema_version": "x"})
        report = steps.inspect_prepared_input(self.package)
        self.assertFalse(report["passed"])
        self.assertIn("input.source_identity", [row["code"] for row in report["errors"]])
        # Changed engineering bytes stay bound at the gates that own the frozen
        # package (steps.bind_discovery) and the delivery subject (verify_discovery),
        # not in the identity inspection.

    def test_linux_never_opens_native_cad_and_keeps_failure_evidence(self):
        self.seal()
        self.acquisition_without_admin()
        events = []
        with (
            patch.object(solidworks.sys, "platform", "linux"),
            patch("description_pipeline.sources.solidworks.freeze.freeze") as native,
            patch(
                "description_pipeline.sources.solidworks.discovery.source_definition",
                return_value={"provider": "solidworks", "assembly": "assembly.SLDASM"},
            ),
            patch("description_pipeline.sources.solidworks.input.native_source_closure", return_value={}),
        ):
            result = solidworks.capture(self.package, self.output, on_event=events.append)
        native.assert_not_called()
        self.assertFalse(result["passed"])
        self.assertIn("Windows", result["error"])
        self.assertTrue(result["run_id"])
        self.assertEqual("failed", events[-1]["state"])
        self.assertFalse(self.output.exists())
        diagnostic = Path(result["diagnostic_path"])
        listing = sorted(path.relative_to(diagnostic).as_posix() for path in diagnostic.rglob("*"))
        self.assertTrue((diagnostic / "input/handoff.json").is_file(), listing)

    def test_a_non_empty_output_is_never_replaced(self):
        self.output.mkdir()
        marker = self.output / "README.md"
        marker.write_text("Existing output bytes")
        with self.assertRaisesRegex(PipelineError, "absent or empty"):
            solidworks.capture(self.package, self.output)
        self.assertEqual("Existing output bytes", marker.read_text())

    def test_native_preflight_failure_blocks_capture_and_retains_diagnostics(self):
        self.seal()
        self.acquisition_without_admin()
        error = PipelineError("Native readiness failed")
        error.details = {"reason": "SolidWorks registration unavailable"}
        with (
            patch.object(solidworks.sys, "platform", "win32"),
            patch("description_pipeline.runtime.native_readiness", side_effect=error),
            patch("description_pipeline.sources.solidworks.freeze.freeze") as capture,
            patch(
                "description_pipeline.sources.solidworks.discovery.source_definition",
                return_value={"provider": "solidworks", "assembly": "assembly.SLDASM"},
            ),
            patch("description_pipeline.sources.solidworks.input.native_source_closure", return_value={}),
        ):
            result = solidworks.capture(self.package, self.output)
        capture.assert_not_called()
        self.assertFalse(result["passed"])
        self.assertEqual(result["stage"], "capture")
        self.assertEqual(result["error"], "Native readiness failed")
        self.assertEqual(result["detail"], error.details)
        root = Path(result["diagnostic_path"])
        self.assertEqual(result["stage_report"]["sha256"], file_digest(root / "reports/stages.json"))
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
            result = solidworks.capture(self.package, self.output, expected_inputs=expected)
        native.assert_not_called()
        self.assertFalse(result["passed"])
        self.assertIn("changed after discovery", result["error"])
        check = next(stage for stage in stage_view(result)["stages"] if stage["id"] == "capture")["input_qc"][0]
        self.assertEqual(check["state"], "failed")

    def test_static_inspection_is_consumed_once_and_changed_inspected_bytes_are_rejected(self):
        self.seal()
        self.acquisition_without_admin()
        inspect_prepared = steps.inspect_prepared_input
        calls: list = []

        def change_after_inspection(package):
            report = inspect_prepared(package)
            calls.append(package)
            (package / "part.SLDPRT").write_bytes(b"Changed after inspection")
            return report

        with (
            patch.object(steps, "inspect_prepared_input", side_effect=change_after_inspection),
            patch(
                "description_pipeline.sources.solidworks.discovery.source_definition",
                return_value={"provider": "solidworks", "assembly": "assembly.SLDASM"},
            ),
            patch("description_pipeline.sources.solidworks.input.native_source_closure", return_value={}),
            patch("description_pipeline.sources.solidworks.freeze.freeze") as native,
        ):
            result = solidworks.capture(self.package, self.output)
        self.assertEqual(len(calls), 1)
        native.assert_not_called()
        self.assertIn("Archived CAD originals differ from the frozen inventory", result["error"])
        self.assertFalse(result["passed"])

    def test_capture_input_qc_records_handoff_and_frozen_file_binding(self):
        from description_pipeline.verification.consumer import ConsumerError

        self.seal()
        self.acquisition_without_admin()
        expected = package_inventory(self.package)
        handoff = "c" * 64
        error = ConsumerError("Consumer loading failed", stderr="ImportError: control")
        with (
            patch.object(solidworks.sys, "platform", "win32"),
            patch("description_pipeline.runtime.native_readiness", side_effect=error),
            patch("description_pipeline.sources.solidworks.freeze.freeze") as native,
            patch(
                "description_pipeline.sources.solidworks.discovery.source_definition",
                return_value={"provider": "solidworks", "assembly": "assembly.SLDASM"},
            ),
            patch("description_pipeline.sources.solidworks.input.native_source_closure", return_value={}),
        ):
            result = solidworks.capture(self.package, self.output, expected_inputs=expected, handoff_sha256=handoff)
        native.assert_not_called()
        self.assertFalse(result["passed"])
        check = next(stage for stage in stage_view(result)["stages"] if stage["id"] == "capture")["input_qc"][0]
        self.assertEqual(check["id"], "input.valid")
        self.assertEqual(check["state"], "passed")
        self.assertTrue(check["details"]["passed"])
        self.assertEqual(check["details"]["handoff_sha256"], handoff)
        delivered = {
            "cad/assembly.SLDASM": file_digest(self.package / "assembly.SLDASM"),
            "cad/part.SLDPRT": file_digest(self.package / "part.SLDPRT"),
            "source.json": file_digest(self.package / "source.json"),
        }
        self.assertEqual(check["details"]["files_sha256"], digest(delivered))
        self.assertEqual(check["details"]["files"], {"input/" + path: value for path, value in delivered.items()})

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
        from description_pipeline.delivery import stamped_output, subject_digest

        self.seal()
        bundle = self.root / "frozen"
        (bundle / "reports").mkdir(parents=True)
        import shutil

        shutil.copytree(self.package, bundle / "input")
        (bundle / "input" / "discovery").mkdir()
        write_json(
            bundle / "input" / "discovery" / "native-discovery.json",
            {"identity": {"hardware_id": "arm"}, "handoff_sha256": "d" * 64},
        )
        input_files = inventory(bundle / "input")
        write_json(
            bundle / "input" / "handoff.json",
            {
                "passed": True,
                "input": None,
                "cad_revision": {"revision": "r1"},
                "input_receipt": None,
                "package_files": input_files,
            },
        )
        (bundle / "evidence").mkdir()
        (bundle / "evidence/raw.json").write_text('"Synthetic driver fixture"')
        write_json(
            bundle / "evidence" / "discovery.json",
            {"identity": {"hardware_id": "arm"}, "handoff_sha256": "d" * 64},
        )
        for name in ("model", "urdf", "meshes"):
            (bundle / name).mkdir()
        (bundle / "README.md").write_text("Synthetic driver fixture")
        write_json(bundle / "reports/tool.json", {})
        write_json(bundle / "tool.json", {})
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
            patch.object(
                steps,
                "inspect_prepared_input",
                return_value={
                    "passed": True,
                    "input": None,
                    "cad_revision": {"revision": "r1"},
                    "input_receipt": None,
                },
            ),
        ):
            result = solidworks.rebuild(bundle, self.output)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["rebuild_from"]["subject_sha256"], source_subject)
        self.assertEqual(result["rebuild_from"]["handoff_sha256"], "d" * 64)
        self.assertEqual(result["handoff_sha256"], "d" * 64)
        view = read_data(self.output / "reports/stages.json")
        self.assertEqual(view["execution_scope"], ["generate", "verify"])
        self.assertEqual([row["id"] for row in view["stages"] if row["state"] == "completed"], ["generate", "verify"])
        self.assertTrue(all(row["state"] == "not_run" for row in view["stages"] if not row["in_scope"]))
        self.assertTrue(stamped_output(self.output))

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
        write_json(bundle / "tool.json", {})
        with (
            patch.object(solidworks, "_check", return_value={"passed": True, "subject_sha256": "0" * 64}),
            self.assertRaisesRegex(PipelineError, "changed after verification"),
        ):
            solidworks.rebuild(bundle, self.output)
        self.assertFalse(self.output.exists())

    def test_rebuild_refuses_frozen_evidence_changed_during_preparation(self):
        import shutil

        from description_pipeline.delivery import subject_digest

        self.seal()
        bundle = self.root / "frozen"
        (bundle / "reports").mkdir(parents=True)
        shutil.copytree(self.package, bundle / "input")
        definition = read_data(bundle / "input/robot.yaml")
        definition["provenance"] = {"native_inventory_sha256": "d" * 64}
        write_json(bundle / "input/robot.yaml", definition)
        (bundle / "evidence").mkdir()
        (bundle / "evidence/raw.json").write_text('"Synthetic driver fixture"')
        for name in ("model", "urdf", "meshes"):
            (bundle / name).mkdir()
        (bundle / "README.md").write_text("Synthetic driver fixture")
        write_json(bundle / "reports/tool.json", {})
        write_json(bundle / "tool.json", {})
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
        self.assertFalse(self.output.exists())

    def test_annotated_diagnostic_is_preserved_on_retry(self):
        first = solidworks.capture(self.package, self.output)
        old = Path(first["diagnostic_path"])
        note = old / "operator-note.txt"
        note.write_text("Do not erase this review")
        second = solidworks.capture(self.package, self.output)
        self.assertNotEqual(first["diagnostic_path"], second["diagnostic_path"])
        self.assertEqual("Do not erase this review", note.read_text())

    def test_output_and_input_cannot_overlap(self):
        before = (self.package / "robot.yaml").read_bytes()
        with self.assertRaises(PipelineError):
            solidworks.capture(self.package, self.package / "output")
        self.assertEqual(before, (self.package / "robot.yaml").read_bytes())

    def test_report_option_cannot_overwrite_delivery_input(self):
        before = (self.package / "robot.yaml").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = main(["check", str(self.package), "--report", str(self.package / "robot.yaml")])
        self.assertEqual(1, result)
        self.assertEqual(before, (self.package / "robot.yaml").read_bytes())

    def test_original_cad_is_archived_without_editing(self):
        self.seal()
        self.acquisition_without_admin()
        target = self.root / "staging"
        target.mkdir()
        original = (self.package / "assembly.SLDASM").read_bytes()
        steps._archive_input(self.package, target)
        self.assertEqual(original, (target / "input/cad/assembly.SLDASM").read_bytes())
        self.assertEqual(original, (self.package / "assembly.SLDASM").read_bytes())
        self.assertTrue((target / "input/source.json").is_file())

    def test_concurrent_output_writer_is_refused(self):
        with solidworks.output_lock(self.output), self.assertRaises(PipelineError), solidworks.output_lock(self.output):
            self.fail("Second writer acquired the same output lock")


if __name__ == "__main__":
    unittest.main()
