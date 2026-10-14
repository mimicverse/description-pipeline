"""Focused regressions for the per-stage entries: seeding, subject binding, reuse."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline import solidworks, steps
from description_pipeline.delivery import subject_inventory
from description_pipeline.io import digest, inventory, write_json
from description_pipeline.verification import solidworks_urdf

SHA = "a" * 64


def make_seed(root: Path) -> Path:
    """A recorded-checkpoint shaped fixture: the subject plus the seeded parts."""

    seed = root / "seed"
    seed.mkdir(parents=True)
    (seed / "README.md").write_text("stage fixture\n", encoding="utf-8")
    (seed / "input").mkdir(parents=True)
    (seed / "input" / "robot.yaml").write_text("{}\n", encoding="utf-8")
    input_files = inventory(seed / "input")
    write_json(
        seed / "input" / "handoff.json",
        {
            "passed": True,
            "input": None,
            "cad_revision": {"revision": "r1"},
            "input_receipt": None,
            "package_files": input_files,
        },
    )
    (seed / "evidence").mkdir()
    (seed / "evidence" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (seed / "model").mkdir()
    (seed / "model" / "robot.json").write_text("{}\n", encoding="utf-8")
    (seed / "urdf").mkdir()
    (seed / "urdf" / "robot.urdf").write_text("<robot/>\n", encoding="utf-8")
    (seed / "meshes").mkdir()
    (seed / "meshes" / "part.stl").write_bytes(b"stl")
    (seed / "tool.json").write_text("{}\n", encoding="utf-8")
    (seed / "manifest.json").write_text("{}\n", encoding="utf-8")
    (seed / "reports").mkdir()
    (seed / "reports" / "tool.json").write_text("{}\n", encoding="utf-8")
    (seed / "reports" / "quality.json").write_text(json.dumps({"passed": True}) + "\n", encoding="utf-8")
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


class StageEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "delivery"

    def test_generate_seeds_the_capture_checkpoint(self) -> None:
        seed = make_seed(self.root)
        before = tree_bytes(seed)
        seen = {}

        def fake_generate(staging, definition, input_report, on_event=None):
            seen["hardware_id"] = definition["hardware_id"]
            seen["evidence"] = (Path(staging) / "evidence" / "manifest.json").is_file()
            return SHA

        with (
            patch.object(steps, "generate_model", side_effect=fake_generate),
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
            patch(
                "description_pipeline.sources.solidworks.input.resolve_package",
                return_value={"hardware_id": "arm"},
            ),
        ):
            result = solidworks.generate(
                seed,
                self.output,
                run_id="8b6adf2e-5e19-4d87-a177-e26c2f0f4a1c",
                resume={"parent_run": "8b6adf2e-5e19-4d87-a177-e26c2f0f4a1c", "from_stage": "generate"},
            )
        self.assertEqual(result["state"], "generated")
        self.assertIs(result["passed"], False)
        self.assertEqual(result["subject_sha256"], SHA)
        self.assertEqual(result["execution_scope"], ["generate"])
        self.assertEqual(
            result["resume"],
            {"parent_run": "8b6adf2e-5e19-4d87-a177-e26c2f0f4a1c", "from_stage": "generate"},
        )
        self.assertEqual(seen, {"hardware_id": "arm", "evidence": True})
        self.assertTrue((self.output / "input" / "robot.yaml").is_file())
        self.assertEqual(tree_bytes(seed), before)

    def test_verify_is_bound_to_the_recorded_subject(self) -> None:
        seed = make_seed(self.root)
        expected = digest(subject_inventory(seed))

        def fake_verify(staging, generated_subject, on_event=None):
            self.assertEqual(generated_subject, expected)
            return {"passed": True, "subject_sha256": generated_subject}

        with (
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
            patch.object(steps, "verify_delivery", side_effect=fake_verify),
            patch(
                "description_pipeline.sources.solidworks.input.resolve_package",
                return_value={"hardware_id": "arm"},
            ),
        ):
            ok = solidworks.verify(seed, self.output, expected_subject=expected)
            self.assertEqual(ok["state"], "verified")
            self.assertIs(ok["passed"], True)
            mismatch = solidworks.verify(seed, self.root / "delivery-mismatch", expected_subject="b" * 64)
        self.assertIs(mismatch["passed"], False)
        self.assertIn("does not match", mismatch["error"])

    def test_publish_seals_the_verified_checkpoint_and_records_the_submission(self) -> None:
        seed = make_seed(self.root)
        before = tree_bytes(seed)
        subject = digest(subject_inventory(seed))
        repository = self.root / "repo"
        repository.mkdir()
        calls = {"publish": 0}

        def fake_publish(bundle, repo, base=None, message=None, on_event=None):
            calls["publish"] += 1
            return {
                "passed": True,
                "state": "noop",
                "url": "https://github.com/example/repo/pull/1",
                "subject": subject,
            }

        with (
            patch.object(steps, "inspect_prepared_input", return_value=REPLAYED_INSPECTION),
            patch.object(steps, "publish_model", side_effect=fake_publish),
            patch.object(solidworks_urdf, "check_bundle", return_value={"passed": True, "subject_sha256": subject}),
            patch.object(solidworks_urdf, "rebuilt_definition", return_value={}),
            patch.object(
                solidworks_urdf,
                "require_qualified_report",
                side_effect=lambda report, definition, root=None: {"passed": True, "subject_sha256": subject},
            ),
            patch(
                "description_pipeline.sources.solidworks.input.resolve_package",
                return_value={"hardware_id": "arm"},
            ),
        ):
            result = solidworks.publish(
                seed,
                self.output,
                expected_subject=subject,
                repository=repository,
            )
        self.assertIs(result["passed"], True)
        self.assertEqual(result["submission"]["state"], "noop")
        self.assertEqual(result["execution_scope"], ["publish"])
        self.assertEqual(calls["publish"], 1)
        self.assertEqual(tree_bytes(seed), before)

    def test_a_missing_checkpoint_part_fails_closed(self) -> None:
        seed = self.root / "seed-missing"
        (seed / "input").mkdir(parents=True)
        (seed / "input" / "robot.yaml").write_text("{}\n", encoding="utf-8")
        result = solidworks.generate(seed, self.output)
        self.assertIs(result["passed"], False)
        self.assertIn("Stage checkpoint is missing: evidence", result["error"])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
