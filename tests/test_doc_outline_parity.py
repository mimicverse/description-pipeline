"""Each language pair has to document the same sections at the same levels.

``tests/test_translation_parity.py`` compares the runnable command lines, which cannot see a section
that only one side still has.  Wording may be translated; the shape may not change silently, so this
compares the number of headings and the level sequence of every pair, ignoring code fences.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def pairs() -> list[tuple[Path, Path]]:
    found = [(ROOT / "README.md", ROOT / "README.en.md"), (ROOT / "CONTRIBUTING.md", ROOT / "CONTRIBUTING.en.md")]
    for chinese in sorted((ROOT / "docs").rglob("*.md")):
        if chinese.name.endswith(".en.md"):
            continue
        english = chinese.with_name(chinese.stem + ".en.md")
        if english.is_file():
            found.append((chinese, english))
    return [(chinese, english) for chinese, english in found if chinese.is_file() and english.is_file()]


def outline(text: str) -> list[tuple[int, str]]:
    """Every heading as ``(level, text)``; headings inside code fences do not count."""

    found: list[tuple[int, str]] = []
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        match = HEADING.match(line)
        if match:
            found.append((len(match.group(1)), match.group(2)))
    return found


def differences(chinese: str, english: str, left: list[tuple[int, str]], right: list[tuple[int, str]]) -> list[str]:
    problems: list[str] = []
    if len(left) != len(right):
        problems.append(f"{chinese} has {len(left)} headings, {english} has {len(right)}")
    levels_left = [level for level, _ in left]
    levels_right = [level for level, _ in right]
    if levels_left != levels_right:
        first = next(
            (index for index, (a, b) in enumerate(zip(levels_left, levels_right, strict=False)) if a != b),
            min(len(levels_left), len(levels_right)),
        )
        problems.append(
            f"{chinese} / {english}: heading levels diverge at position {first + 1} "
            f"({levels_left[max(0, first - 1) : first + 2]} vs {levels_right[max(0, first - 1) : first + 2]})"
        )
    return problems


class OutlineParityTests(unittest.TestCase):
    def test_language_pairs_document_the_same_sections(self):
        documents = pairs()
        self.assertGreaterEqual(len(documents), 8, "language pairs should be discovered automatically")
        problems: list[str] = []
        for chinese, english in documents:
            problems.extend(
                differences(
                    chinese.relative_to(ROOT).as_posix(),
                    english.relative_to(ROOT).as_posix(),
                    outline(chinese.read_text(encoding="utf-8")),
                    outline(english.read_text(encoding="utf-8")),
                )
            )
        self.assertEqual(problems, [], "the two language versions must keep the same sections")

    def test_the_outline_ignores_comments_inside_code_fences(self):
        text = "# One\n\n```sh\n# not a heading\n```\n\n## Two\n"
        self.assertEqual([level for level, _ in outline(text)], [1, 2])

    def test_a_dropped_section_is_reported(self):
        left = outline("# A\n\n## One\n\n## Two\n")
        right = outline("# A\n\n## One\n")
        self.assertTrue(differences("a.md", "a.en.md", left, right))
        self.assertEqual(differences("a.md", "a.en.md", left, left), [])


if __name__ == "__main__":
    unittest.main()
