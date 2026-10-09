"""Focused regressions for linked-run resume inside the capture..publish runner."""

from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks, steps
from description_pipeline.delivery import subject_inventory
from description_pipeline.io import PipelineError, digest, inventory
from description_pipeline.verification import solidworks_urdf

SHA = "a" * 64


def make_seed(root: Path, *, reports: bool = True) -> Path:
    seed = root / "seed"
    seed.mkdir(parents=True)
    (seed / "README.md").write_text("resume fixture\n", encoding="utf-8")
    (seed / "input").mkdir(parents=True)
    (seed / "input" / "robot.yaml").write_text("{}\n", encoding="utf-8")
    input_files = inventory(seed / "input")
    (seed / "evidence").mkdir()
    (seed / "evidence" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (seed / "model").mkdir()
    (seed / "model" / "robot.json").write_text("{}\n", encoding="utf-8")
    (seed / "urdf").mkdir()
    (seed / "urdf" / "robot.urdf").write_text("<robot/>\n", encoding="utf-8")
    (seed / "meshes").mkdir()
    (seed / "meshes" / "part.stl").write_bytes(b"stl")
    if reports:
        (seed / "reports").mkdir()
        (seed / "reports" / "input.json").write_text(
            json.dumps({"cad_revision": {"revision": "r1"}, "input": None, "package_files": input_files}) + "\n",
            encoding="utf-8",
        )
        (seed / "reports" / "tool.json").write_text("{}\n", encoding="utf-8")
    return seed


def tree_bytes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


#: Replayed static inspection of one synthetic archived input; mirrors the recorded report.
REPLAYED_INSPECTION = {
    "passed": True,
    "input": None,
    "cad_revision": {"revision": "r1"},
    "input_receipt": None,
}


class RunnerResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.package = self.root / "handoff"
        self.package.mkdir()
        (self.package / "assembly.SLDASM").write_bytes(b"control")
        self.output = self.root / "delivery"

    def test_generate_resume_reuses_capture_and_reruns_downstream(self) -> None:
        seed = make_seed(self.root)
        before = tree_bytes(seed)
        calls = {"capture": 0, "generate": 0, "verify": 0}

        def fake_capture(*args, **kwargs):
            calls["capture"] += 1
            raise AssertionError("capture must not run on a generate resume")

        def fake_generate(staging, definition, input_report, on_event=None):
            calls["generate"] += 1
            self.assertEqual(definition["hardware_id"], "arm")
            self.assertTrue((Path(staging) / "evidence" / "manifest.json").is_file())
            return SHA

        def fake_verify(staging, generated_subject, on_event=None):
            calls["verify"] += 1
            self.assertEqual(generated_subject, SHA)
            return {"passed": True, "subject_sha256": generated_subject}

        with (
            patch.object(steps, "capture_evidence", side_effect=fake_capture),
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
        self.assertEqual(
            result["resume"],
            {"parent_run": "8b6adf2e-5e19-4d87-a177-e26c2f0f4a1c", "from_stage": "generate"},
        )
        self.assertEqual(calls, {"capture": 0, "generate": 1, "verify": 1})
        self.assertTrue((self.output / "input" / "robot.yaml").is_file())
        self.assertEqual(tree_bytes(seed), before)

    def test_verify_resume_recomputes_subject_and_rejects_mismatch(self) -> None:
        seed = make_seed(self.root)
        expected = digest(subject_inventory(seed))
        with (
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
            patch.object(
                steps,
                "verify_delivery",
                side_effect=lambda staging, subject, on_event=None: {
                    "passed": True,
                    "subject_sha256": subject,
                },
            ),
            patch(
                "description_pipeline.sources.solidworks.input.resolve_package",
                return_value={"hardware_id": "arm"},
            ),
        ):
            ok = solidworks.run(
                self.package, self.output, resume_from="verify", seed_dir=seed, expected_subject=expected
            )
            self.assertTrue(ok["passed"], ok)
            mismatch = solidworks.run(
                self.package,
                self.root / "delivery-mismatch",
                resume_from="verify",
                seed_dir=seed,
                expected_subject="b" * 64,
            )
        self.assertFalse(mismatch["passed"])
        self.assertIn("does not match", mismatch["error"])

    def test_publish_resume_skips_native_and_generation_and_keeps_parent(self) -> None:
        seed = make_seed(self.root)
        before = tree_bytes(seed)
        subject = digest(subject_inventory(seed))
        repository = self.root / "repo"
        repository.mkdir()
        calls = {"capture": 0, "generate": 0, "verify": 0, "publish": 0}

        def fake_publish(bundle, repo, base=None, message=None, on_event=None):
            calls["publish"] += 1
            return {
                "passed": True,
                "state": "noop",
                "url": "https://github.com/example/repo/pull/1",
                "subject": subject,
            }

        with (
            patch.object(
                steps,
                "capture_evidence",
                side_effect=lambda *a, **k: calls.__setitem__("capture", calls["capture"] + 1),
            ),
            patch.object(
                steps,
                "generate_model",
                side_effect=lambda *a, **k: calls.__setitem__("generate", calls["generate"] + 1),
            ),
            patch.object(
                steps, "verify_delivery", side_effect=lambda *a, **k: calls.__setitem__("verify", calls["verify"] + 1)
            ),
            patch.object(steps, "publish_model", side_effect=fake_publish),
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
            patch.object(solidworks_urdf, "check_bundle", return_value={"passed": True, "subject_sha256": subject}),
            patch.object(
                solidworks_urdf,
                "require_qualified_report",
                side_effect=lambda report: {"passed": True, "subject_sha256": subject},
            ),
            patch(
                "description_pipeline.sources.solidworks.input.resolve_package",
                return_value={"hardware_id": "arm"},
            ),
        ):
            result = solidworks.run(
                self.package,
                self.output,
                resume_from="publish",
                seed_dir=seed,
                expected_subject=subject,
                repository=repository,
            )
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["submission"]["state"], "noop")
        self.assertEqual(calls["capture"], 0)
        self.assertEqual(calls["generate"], 0)
        self.assertEqual(calls["verify"], 0)
        self.assertEqual(calls["publish"], 1)
        self.assertEqual(tree_bytes(seed), before)

    def test_seed_helper_rejects_missing_parts_and_unsupported_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "seed-missing"
            (missing / "input").mkdir(parents=True)
            with self.assertRaises(PipelineError):
                solidworks._seed_resume(Path(tmp) / "staging", missing, "generate")
        result = solidworks.run(self.package, self.output, resume_from="freeze", seed_dir=self.package)
        self.assertFalse(result["passed"])
        self.assertIn("not supported", result["error"])


if __name__ == "__main__":
    unittest.main()
