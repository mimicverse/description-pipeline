"""Linked attempts consume their own new checkpoints and preserve upstream lineage."""

from pathlib import Path
from unittest import TestCase, mock

from description_pipeline.io import PipelineError
from description_pipeline.orchestration import linux_runner

from .test_linux_split import LinuxSplitTests, portable_record

CHILD = "2ed90b89-de34-44a8-92e9-bc64e827aa37"
GRANDCHILD = "4ca44547-8f76-4faa-bef4-63e8556f4a80"


class LinuxLineageTests(TestCase):
    setUp = LinuxSplitTests.setUp
    admit = LinuxSplitTests.admit

    def set_parent_checkpoints(self):
        self.admit()
        for stage in ("generate", "verify"):
            path = self.store.stage_dir(self.run_id, stage)
            path.mkdir(parents=True)
            (path / "marker").write_text("old-parent")
            self.store.record_stage(self.run_id, stage, {
                "state": "verified" if stage == "verify" else "generated",
                "subject_sha256": "a" * 64,
            })

    def run_stage(self, run, stage, parent, subject="b" * 64):
        return linux_runner.run_portable_stage(
            self.store, run, stage, source_run_id=parent,
            expected_subject=None if stage == "generate" else subject,
            repository=self.tmp / "repo" if stage == "publish" else None,
        )

    def test_linked_generation_then_verification_uses_new_child_files(self):
        self.set_parent_checkpoints()
        observed = []

        def driver(package, output, **kw):
            stage, seed = kw["stop_after"], Path(kw["seed_dir"])
            observed.append((stage, seed))
            if stage != "generate":
                self.assertEqual((seed / "marker").read_text(), "new-child")
            Path(output).mkdir(parents=True)
            (Path(output) / "marker").write_text("new-child")
            return {"state": stage + "d", "subject_sha256": "b" * 64}

        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(linux_runner.solidworks, "run", side_effect=driver),
        ):
            for stage in ("generate", "verify", "publish"):
                self.run_stage(CHILD, stage, self.run_id)
        self.assertEqual(observed[1][1], self.store.stage_dir(CHILD, "generate"))
        self.assertEqual(observed[2][1], self.store.stage_dir(CHILD, "verify"))
        self.assertEqual(self.store.meta(CHILD)["resume_from"], "generate")
        self.assertEqual((self.store.stage_dir(self.run_id, "verify") / "marker").read_text(), "old-parent")

    def test_verification_rerun_can_itself_be_rerun_without_native_capture(self):
        self.set_parent_checkpoints()

        def driver(package, output, **kw):
            self.assertEqual(Path(package), self.store.capture_dir(self.run_id))
            self.assertEqual(Path(kw["seed_dir"]), self.store.stage_dir(self.run_id, "generate"))
            Path(output).mkdir(parents=True)
            return {"state": "verified", "passed": True, "subject_sha256": "a" * 64}

        with (
            mock.patch.object(linux_runner, "tool_record", return_value=portable_record(self.tool)),
            mock.patch.object(linux_runner.solidworks, "run", side_effect=driver),
        ):
            self.run_stage(CHILD, "verify", self.run_id, "a" * 64)
            self.run_stage(GRANDCHILD, "verify", CHILD, "a" * 64)
        self.assertEqual(self.store.checkpoint_owner(GRANDCHILD, "capture"), self.run_id)
        self.assertEqual(self.store.checkpoint_owner(GRANDCHILD, "generate"), self.run_id)
        self.assertEqual(self.store.checkpoint_owner(GRANDCHILD, "verify"), GRANDCHILD)
        self.assertEqual(self.store.checkpoint_receipt(CHILD, "generate"), self.store.receipt_path(self.run_id, "generate"))
        self.assertFalse(self.store.capture_dir(CHILD).exists())

    def test_missing_regenerated_output_never_falls_back_to_old_parent(self):
        self.set_parent_checkpoints()
        self.store.update_meta(CHILD, source_run_id=self.run_id, resume_from="generate")
        with self.assertRaisesRegex(PipelineError, "no retained generate"):
            self.store.checkpoint_dir(CHILD, "generate")

    def test_lineage_cycle_is_refused(self):
        self.store.update_meta(CHILD, source_run_id=GRANDCHILD, resume_from="verify")
        self.store.update_meta(GRANDCHILD, source_run_id=CHILD, resume_from="verify")
        with self.assertRaisesRegex(PipelineError, "cycle"):
            self.store.checkpoint_dir(CHILD, "capture")
