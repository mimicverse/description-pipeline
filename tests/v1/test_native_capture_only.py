"""Capture-only native runs: sealed transfer, native_complete, no downstream stages."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks, steps
from description_pipeline.io import PipelineError, file_digest

from .test_native_resume_runner import REPLAYED_INSPECTION, make_seed


def install_stub_transfer() -> types.ModuleType:
    """Minimal stand-in for C's stage_transfer until it lands."""

    module = types.ModuleType("description_pipeline.stage_transfer")
    module.CAPTURE_ARCHIVE = "native-evidence.zip"
    module.CAPTURE_MANIFEST = "transfer-manifest.json"

    def seal_capture(root, archive, *, run_id, handoff_sha256, main_assembly, native_tool):
        Path(archive).write_bytes(b"PK\x03\x04stub")
        return {
            "schema_version": "solidworks-to-urdf.transfer/v1",
            "run_id": run_id,
            "handoff_sha256": handoff_sha256,
            "main_assembly": main_assembly,
            "native_tool_source_sha256": native_tool.get("source_sha256"),
            "files": {"input/robot.yaml": file_digest(Path(root) / "input/robot.yaml")},
        }

    module.seal_capture = seal_capture
    return module


class CaptureOnlyRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.package = self.root / "handoff"
        self.package.mkdir()
        (self.package / "assembly.SLDASM").write_bytes(b"control")
        self.output = self.root / "delivery"
        self.transfer = install_stub_transfer()

    @staticmethod
    def _fake_capture(calls):
        def fake_capture(package, staging, *, backend=None, on_event=None, expected_inputs=None, handoff_sha256=None):
            calls["capture"] += 1
            staging = Path(staging)
            (staging / "input").mkdir(parents=True)
            (staging / "input" / "robot.yaml").write_text("hardware_id: arm\n", encoding="utf-8")
            (staging / "evidence").mkdir()
            (staging / "evidence" / "manifest.json").write_text("{}\n", encoding="utf-8")
            (staging / "reports").mkdir(parents=True)
            (staging / "reports" / "input.json").write_text(
                json.dumps({"cad_revision": {"revision": "r1"}}) + "\n", encoding="utf-8"
            )
            if on_event is not None:
                on_event({"stage": "capture", "state": "completed"})
            return {"hardware_id": "arm"}, {"cad_revision": {"revision": "r1"}, "package_files": {}}

        return fake_capture

    def test_capture_only_seals_transfer_and_stops(self) -> None:
        calls = {"capture": 0, "generate": 0, "verify": 0, "publish": 0}

        def forbid(name):
            def blocked(*args, **kwargs):
                calls[name] += 1
                raise AssertionError(f"{name} must not run in a capture-only run")

            return blocked

        with (
            patch.dict(sys.modules, {"description_pipeline.stage_transfer": self.transfer}),
            patch.object(steps, "capture_evidence", side_effect=self._fake_capture(calls)),
            patch.object(steps, "generate_model", side_effect=forbid("generate")),
            patch.object(steps, "verify_delivery", side_effect=forbid("verify")),
            patch.object(steps, "publish_model", side_effect=forbid("publish")),
        ):
            result = solidworks.run(
                self.package,
                self.output,
                capture_only=True,
                backend=object(),
                run_id="7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865",
                handoff_sha256="b" * 64,
                main_assembly="3.0 总装1008.SLDASM",
            )
        self.assertIs(result["native_complete"], True)
        self.assertIs(result["passed"], False)
        self.assertEqual(result["state"], "native_complete")
        archive = self.output / "native-evidence.zip"
        manifest = self.output / "transfer-manifest.json"
        self.assertTrue(archive.is_file())
        self.assertTrue(manifest.is_file())
        info = result["capture_archive"]
        self.assertEqual(info["name"], "native-evidence.zip")
        self.assertEqual(info["sha256"], file_digest(archive))
        self.assertEqual(info["size"], archive.stat().st_size)
        self.assertEqual(info["manifest_name"], "transfer-manifest.json")
        self.assertEqual(info["manifest_sha256"], file_digest(manifest))
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["run_id"], result["run_id"])
        self.assertEqual(calls, {"capture": 1, "generate": 0, "verify": 0, "publish": 0})
        self.assertTrue((self.output / "reports/native-tool.json").is_file())
        self.assertTrue((self.output / "reports/stages.json").is_file())
        receipt = json.loads((self.output / "reports/run.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "native_complete")
        self.assertIs(receipt["passed"], False)
        self.assertEqual(receipt["capture_archive"], info)

    def test_capture_only_refuses_publish_and_downstream_resume(self) -> None:
        repository = self.root / "repo"
        with self.assertRaises(PipelineError) as published:
            solidworks.run(self.package, self.output, capture_only=True, repository=repository)
        self.assertIn("Capture-only", str(published.exception))
        with self.assertRaises(PipelineError) as resumed:
            solidworks.run(self.package, self.output, capture_only=True, resume_from="generate")
        self.assertIn("Capture-only", str(resumed.exception))

    def test_generate_resume_carries_transfer_provenance(self) -> None:
        seed = make_seed(self.root)
        (seed / "reports" / "native-tool.json").write_text('{"role": "native"}\n', encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
