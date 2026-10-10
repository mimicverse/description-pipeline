"""Per-stage stop boundaries: capture seal roundtrip, generated checkpoint, verified."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks, steps
from description_pipeline.io import PipelineError, file_digest
from description_pipeline.orchestration import stage_transfer as transfer
from description_pipeline.stages import CONTRACT

from .test_native_resume_runner import REPLAYED_INSPECTION, make_seed
from .test_stage_transfer import capture_root as build_capture_root
from .test_stage_transfer import native_tool as build_native_tool

HANDOFF = "b" * 64
MAIN = "robot.SLDASM"


def stage_events(stage_id: str) -> list[dict]:
    """Contract-valid boundary-check events for one stage, then completion."""

    definition = next(stage for stage in CONTRACT["stages"] if stage["id"] == stage_id)
    events = []
    for boundary, key in (("input", "input_qc"), ("output", "output_qc")):
        for item in definition[key]:
            events.append(
                {
                    "stage": stage_id,
                    "state": "running",
                    "check": {"id": item["id"], "boundary": boundary, "state": "passed", "details": {}},
                }
            )
    events.append({"stage": stage_id, "state": "completed"})
    return events


def forbid(calls, name):
    def blocked(*args, **kwargs):
        calls[name] += 1
        raise AssertionError(f"{name} must not run when stopped earlier")

    return blocked


class StopAfterRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.package = self.root / "handoff"
        self.package.mkdir()
        (self.package / "assembly.SLDASM").write_bytes(b"control")
        self.output = self.root / "delivery"

    @staticmethod
    def _fake_capture(calls):
        def fake_capture(package, staging, *, backend=None, on_event=None, expected_inputs=None, handoff_sha256=None):
            calls["capture"] += 1
            fixture = build_capture_root(Path(staging).parent / "fixture")
            shutil.copytree(fixture, Path(staging), dirs_exist_ok=True)
            if on_event is not None:
                for event in stage_events("capture"):
                    on_event(event)
            return {"hardware_id": "robot"}, {"cad_revision": {"revision": "r1"}, "package_files": {}}

        return fake_capture

    def test_capture_stop_seals_transfer_and_stops(self) -> None:
        calls = {"capture": 0, "generate": 0, "verify": 0, "publish": 0}

        with (
            patch.object(solidworks, "_native_tool_record", return_value=build_native_tool()),
            patch.object(steps, "capture_evidence", side_effect=self._fake_capture(calls)),
            patch.object(steps, "generate_model", side_effect=forbid(calls, "generate")),
            patch.object(steps, "verify_delivery", side_effect=forbid(calls, "verify")),
            patch.object(steps, "publish_model", side_effect=forbid(calls, "publish")),
        ):
            result = solidworks.run(
                self.package,
                self.output,
                stop_after="capture",
                backend=object(),
                run_id="7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865",
                handoff_sha256=HANDOFF,
                main_assembly=MAIN,
                prior_events=[*stage_events("freeze"), *stage_events("discover")],
            )
        self.assertIs(result["native_complete"], True)
        self.assertIs(result["passed"], False)
        self.assertEqual(result["state"], "native_complete")
        self.assertEqual(result["execution_scope"], ["freeze", "discover", "capture"])
        archive = self.output / transfer.CAPTURE_ARCHIVE
        manifest = self.output / transfer.CAPTURE_MANIFEST
        self.assertTrue(archive.is_file())
        self.assertTrue(manifest.is_file())
        info = result["capture_archive"]
        self.assertEqual(set(info), {"name", "sha256", "size", "manifest_sha256"})
        self.assertEqual(info["name"], transfer.CAPTURE_ARCHIVE)
        self.assertEqual(info["sha256"], file_digest(archive))
        self.assertEqual(info["size"], archive.stat().st_size)
        self.assertEqual(info["manifest_sha256"], file_digest(manifest))
        self.assertEqual(calls, {"capture": 1, "generate": 0, "verify": 0, "publish": 0})
        self.assertTrue((self.output / "reports/native-tool.json").is_file())
        self.assertTrue((self.output / "reports/stages.json").is_file())
        native_stages = self.output / "reports" / "native-stages.json"
        self.assertTrue(native_stages.is_file())
        self.assertEqual(native_stages.read_bytes(), (self.output / "reports/stages.json").read_bytes())
        receipt = json.loads((self.output / "reports/run.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "native_complete")
        self.assertIs(receipt["passed"], False)
        self.assertEqual(receipt["capture_archive"], info)

        # Real-sealer roundtrip: seal → admit, and only the immutable native receipt travels.
        with zipfile.ZipFile(archive) as opened:
            names = opened.namelist()
        self.assertIn("reports/native-stages.json", names)
        self.assertNotIn("reports/stages.json", names)
        self.assertNotIn("reports/run.json", names)
        destination = self.root / "admitted"
        transfer.admit_capture(
            archive,
            destination,
            expected_run_id=result["run_id"],
            expected_handoff_sha256=HANDOFF,
            expected_main_assembly=MAIN,
            expected_native_tool=build_native_tool(),
        )
        self.assertEqual(
            (destination / "reports" / "native-stages.json").read_bytes(),
            native_stages.read_bytes(),
        )

    def test_generate_stop_installs_unverified_checkpoint(self) -> None:
        seed = make_seed(self.root)
        calls = {"generate": 0, "verify": 0, "publish": 0}

        def fake_generate(staging, definition, input_report, on_event=None):
            calls["generate"] += 1
            return "a" * 64

        with (
            patch.object(steps, "generate_model", side_effect=fake_generate),
            patch.object(steps, "verify_delivery", side_effect=forbid(calls, "verify")),
            patch.object(steps, "publish_model", side_effect=forbid(calls, "publish")),
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
            patch(
                "description_pipeline.sources.solidworks.input.resolve_package",
                return_value={"hardware_id": "arm"},
            ),
        ):
            result = solidworks.run(
                self.package,
                self.output,
                resume_from="generate",
                stop_after="generate",
                seed_dir=seed,
                resume={"parent_run": "8b6adf2e-5e19-4d87-a177-e26c2f0f4a1c", "from_stage": "generate"},
            )
        self.assertEqual(result["state"], "generated")
        self.assertIs(result["passed"], False)
        self.assertEqual(result["subject_sha256"], "a" * 64)
        self.assertEqual(result["execution_scope"], ["generate"])
        self.assertEqual(calls, {"generate": 1, "verify": 0, "publish": 0})
        self.assertTrue((self.output / "input" / "robot.yaml").is_file())
        self.assertTrue((self.output / "reports/stages.json").is_file())
        self.assertFalse((self.output / "reports" / "quality.json").exists())
        receipt = json.loads((self.output / "reports/run.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "generated")
        self.assertIs(receipt["passed"], False)

    def test_verify_stop_is_verified_without_submission(self) -> None:
        seed = make_seed(self.root)
        calls = {"generate": 0, "verify": 0, "publish": 0}

        def fake_generate(staging, definition, input_report, on_event=None):
            calls["generate"] += 1
            return "a" * 64

        def fake_verify(staging, generated_subject, on_event=None):
            calls["verify"] += 1
            return {"passed": True, "subject_sha256": generated_subject}

        with (
            patch.object(steps, "generate_model", side_effect=fake_generate),
            patch.object(steps, "verify_delivery", side_effect=fake_verify),
            patch.object(steps, "publish_model", side_effect=forbid(calls, "publish")),
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
            patch(
                "description_pipeline.sources.solidworks.input.resolve_package",
                return_value={"hardware_id": "arm"},
            ),
        ):
            result = solidworks.run(
                self.package,
                self.output,
                resume_from="generate",
                stop_after="verify",
                seed_dir=seed,
                resume={"parent_run": "8b6adf2e-5e19-4d87-a177-e26c2f0f4a1c", "from_stage": "generate"},
            )
        self.assertEqual(result["state"], "verified")
        self.assertIs(result["passed"], True)
        self.assertEqual(result["quality"]["passed"], True)
        self.assertEqual(calls, {"generate": 1, "verify": 1, "publish": 0})

    def test_stop_after_guards(self) -> None:
        with self.assertRaises(PipelineError) as unknown:
            solidworks.run(self.package, self.output, stop_after="bogus")
        self.assertIn("Unknown stop_after", str(unknown.exception))
        with self.assertRaises(PipelineError) as precedes:
            solidworks.run(self.package, self.output, stop_after="capture", resume_from="generate")
        self.assertIn("must not precede", str(precedes.exception))
        with self.assertRaises(PipelineError) as publishless:
            solidworks.run(self.package, self.output, stop_after="capture", repository=self.root / "repo")
        self.assertIn("omit the repository", str(publishless.exception))
        with self.assertRaises(PipelineError) as unrepository:
            solidworks.run(self.package, self.output, stop_after="publish")
        self.assertIn("requires a model repository", str(unrepository.exception))

    def test_generate_resume_carries_transfer_provenance(self) -> None:
        seed = make_seed(self.root)
        (seed / "reports" / "native-tool.json").write_text('{"role": "native"}\n', encoding="utf-8")
        (seed / "reports" / "native-stages.json").write_text('{"stages": []}\n', encoding="utf-8")
        (seed / "transfer-manifest.json").write_text('{"schema_version": "stub"}\n', encoding="utf-8")
        calls = {"generate": 0, "verify": 0}

        def fake_generate(staging, definition, input_report, on_event=None):
            calls["generate"] += 1
            return "a" * 64

        def fake_verify(staging, generated_subject, on_event=None):
            calls["verify"] += 1
            return {"passed": True, "subject_sha256": generated_subject}

        with (
            patch.object(steps, "generate_model", side_effect=fake_generate),
            patch.object(steps, "verify_delivery", side_effect=fake_verify),
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
            patch(
                "description_pipeline.sources.solidworks.input.resolve_package",
                return_value={"hardware_id": "arm"},
            ),
        ):
            result = solidworks.run(
                self.package,
                self.output,
                resume_from="generate",
                seed_dir=seed,
                resume={"parent_run": "8b6adf2e-5e19-4d87-a177-e26c2f0f4a1c", "from_stage": "generate"},
            )
        self.assertTrue(result["passed"], result)
        self.assertEqual(calls, {"generate": 1, "verify": 1})
        self.assertEqual(
            (self.output / "transfer-manifest.json").read_text(encoding="utf-8"),
            '{"schema_version": "stub"}\n',
        )
        self.assertEqual(
            (self.output / "reports" / "native-tool.json").read_text(encoding="utf-8"),
            '{"role": "native"}\n',
        )
        self.assertEqual(
            (self.output / "reports" / "native-stages.json").read_text(encoding="utf-8"),
            '{"stages": []}\n',
        )


if __name__ == "__main__":
    unittest.main()
