from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline.delivery import SUBJECT_DIRECTORIES, SUBJECT_FILES, subject_digest
from description_pipeline.io import PipelineError


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "bundle"
        self.root.mkdir()
        for name in SUBJECT_DIRECTORIES:
            (self.root / name).mkdir()
            (self.root / name / "subject.txt").write_text(name)
        for name in SUBJECT_FILES:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(name)

    def test_every_delivered_asset_is_bound_and_report_is_not_self_referential(self):
        before = subject_digest(self.root)
        (self.root / "reports/quality.json").write_text('{"passed": true}')
        (self.root / "reports/pr.json").write_text('{"url": "https://example.invalid/1"}')
        self.assertEqual(subject_digest(self.root), before)
        (self.root / "meshes/subject.txt").write_text("changed")
        self.assertNotEqual(subject_digest(self.root), before)

    def test_new_file_in_subject_directory_changes_identity(self):
        before = subject_digest(self.root)
        (self.root / "evidence/extra.json").write_text("{}")
        self.assertNotEqual(subject_digest(self.root), before)

    def test_missing_tool_receipt_fails(self):
        (self.root / "reports/tool.json").unlink()
        with self.assertRaises(PipelineError):
            subject_digest(self.root)

    @unittest.skipIf(__import__("os").name == "nt", "symlink creation needs Windows privileges")
    def test_symlink_in_delivery_fails(self):
        target = self.root / "meshes/subject.txt"
        (self.root / "input/linked.txt").symlink_to(target)
        with self.assertRaises(PipelineError):
            subject_digest(self.root)

    @unittest.skipIf(__import__("os").name == "nt", "symlink creation needs Windows privileges")
    def test_symlinked_report_directory_fails(self):
        reports = self.root / "reports"
        reports.rename(self.root / "real-reports")
        reports.symlink_to(self.root / "real-reports", target_is_directory=True)
        with self.assertRaises(PipelineError):
            subject_digest(self.root)


if __name__ == "__main__":
    unittest.main()
