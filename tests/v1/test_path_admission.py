"""Windows-forbidden characters must fail admission on any host, Chinese names must pass.

Focused regressions for ``io.artifact_path_parts``: the same helper gates static package
inspection (``confined`` callers in ``sources/solidworks/input.py``), endpoint and client package
paths, and archive admission (``tools/verify_distribution.safe_members``).  The tests exercise the
helper directly plus the two admission surfaces that must reject before any transfer.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError, artifact_path_parts, confined
from tools.verify_distribution import safe_members

FORBIDDEN = ("<", ">", '"', "|", "?", "*")
CHINESE = "结构组交付规范/机械手-左腕_r2/装配体.SLDASM"


class ArtifactPathTests(unittest.TestCase):
    def test_windows_forbidden_characters_are_rejected(self) -> None:
        for character in FORBIDDEN:
            with self.subTest(character=character):
                for candidate in (f"arm{character}r2", f"arm/r2{character}.SLDASM", f"{character}leading"):
                    with self.assertRaises(PipelineError) as caught:
                        artifact_path_parts(candidate)
                    self.assertIn("Nonportable artifact path", str(caught.exception))

    def test_existing_restrictions_still_hold(self) -> None:
        for candidate in ("..", "a/../b", ".git/config", "C:/arm", "a\\b", "trailing.", "trailing ", "CON"):
            with self.subTest(candidate=candidate), self.assertRaises(PipelineError):
                artifact_path_parts(candidate)

    def test_chinese_names_remain_valid(self) -> None:
        self.assertEqual(
            artifact_path_parts(CHINESE),
            ("结构组交付规范", "机械手-左腕_r2", "装配体.SLDASM"),
        )

    def test_package_admission_rejects_before_touching_the_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "中文目录").mkdir()
            (root / "中文目录" / "零件.SLDPRT").write_bytes(b"fixture")
            self.assertTrue(confined(root, "中文目录/零件.SLDPRT").parent.is_dir())
            for character in FORBIDDEN:
                with self.subTest(character=character):
                    with self.assertRaises(PipelineError):
                        confined(root, f"中文目录/零件{character}2.SLDPRT")

    def test_archive_admission_rejects_forbidden_members(self) -> None:
        safe_members(["docs/机械手规范.md", "meshes/中文零件.STL"])
        for character in FORBIDDEN:
            with self.subTest(character=character):
                with self.assertRaises(PipelineError):
                    safe_members([f"docs/规范{character}.md"])


if __name__ == "__main__":
    unittest.main()
