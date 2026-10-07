"""Adversarial archive paths and changed release assets must fail before installation."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from description_pipeline.io import PipelineError, inventory, write_json
from tools.verify_distribution import safe_members, verify_release


class DistributionTests(unittest.TestCase):
    def test_windows_paths_and_case_collisions_are_rejected_on_linux(self):
        for names in (["../outside"], ["..\\..\\outside"], ["C:\\outside"], ["/outside"], ["a.py", "A.py"]):
            with self.subTest(names=names), self.assertRaises(PipelineError):
                safe_members(names)

    def test_release_checks_every_asset_and_its_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = {"source_commit": "a" * 40}
            path = root / "deployment.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("docs/mechanical-handoff-spec.md", "approved\n")
            write_json(root / "release.json", {**identity, "artifacts": inventory(root)})
            write_json(root / "SHA256SUMS.json", inventory(root))
            verify_release(root, identity)
            with self.assertRaises(PipelineError):
                verify_release(root, {"source_commit": "b" * 40})
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("docs/unknown.md", "extra\n")
            with self.assertRaises(PipelineError):
                verify_release(root, identity)
