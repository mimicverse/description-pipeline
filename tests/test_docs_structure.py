"""Every Markdown file has to render as written: one cell count per table, closed code fences.

A row with one cell too many, or a fence that is never closed, is invisible in the source and
obvious on GitHub - the surface a public project is judged on.  ``tests/test_docs_links.py`` covers
links and anchors, ``tests/test_docs_diagrams.py`` the Mermaid blocks; this covers the shapes around
them, and a planted defect has to be reported so the guard cannot pass by accident.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", "build", "dist", "node_modules"}
FENCE = re.compile(r"^(```|~~~)")
SEPARATOR = re.compile(r"\|?[\s:|-]+\|?")


def markdown_files() -> list[Path]:
    return [
        path
        for path in sorted(ROOT.rglob("*.md"))
        if not any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)
    ]


def cells(line: str) -> list[str]:
    """Split a table row the way GitHub does; an escaped pipe stays inside its cell."""

    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [part.strip() for part in re.split(r"(?<!\\)\|", stripped)]


def audit(text: str, relative: str) -> tuple[list[str], int, int]:
    """Return the structural problems, the tables seen and the code fences closed."""

    problems: list[str] = []
    tables = fences = 0
    fence: str | None = None
    fence_open_line = 0
    table_start: int | None = None
    table_width = 0
    table_has_separator = False

    def close_table() -> None:
        nonlocal table_start, table_width, table_has_separator, tables
        if table_start is not None:
            tables += 1
            if not table_has_separator:
                problems.append(f"{relative}:{table_start}: table has no |---| separator row")
        table_start = None
        table_width = 0
        table_has_separator = False

    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if fence is None and FENCE.match(stripped):
            fence = stripped[:3]
            fence_open_line = number
            continue
        if fence is not None:
            if stripped.startswith(fence):
                fences += 1
                fence = None
            continue
        if stripped.startswith("|") and stripped.count("|") >= 2:
            width = len(cells(line))
            if table_start is None:
                table_start = number
                table_width = width
                continue
            if SEPARATOR.fullmatch(stripped) and set(stripped) <= set("|:- "):
                table_has_separator = True
                if width != table_width:
                    problems.append(f"{relative}:{number}: separator row has {width} cells, header has {table_width}")
                continue
            if width != table_width:
                problems.append(f"{relative}:{number}: row has {width} cells, table has {table_width}")
            continue
        close_table()
    close_table()
    if fence is not None:
        problems.append(f"{relative}:{fence_open_line}: code fence is never closed")
    return problems, tables, fences


class MarkdownStructureTests(unittest.TestCase):
    def test_every_table_and_code_fence_is_well_formed(self):
        problems: list[str] = []
        tables = fences = 0
        for path in markdown_files():
            found, count_tables, count_fences = audit(path.read_text(encoding="utf-8"), path.name)
            problems.extend(found)
            tables += count_tables
            fences += count_fences
        self.assertGreater(tables, 50, "table scan looks broken: too few tables found")
        self.assertGreater(fences, 50, "code fence scan looks broken: too few fences found")
        self.assertEqual(problems, [], "these tables or code fences would not render as written")

    def test_a_planted_defect_is_reported(self):
        clean = "| a | b |\n| --- | --- |\n| `x\\|y` | 2 |\n"
        planted = {
            "row with an extra cell": "| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n",
            "table without a separator": "| a | b |\n| 1 | 2 |\n",
            "separator with the wrong width": "| a | b |\n| --- |\n| 1 | 2 |\n",
            "code fence that never closes": "text\n```sh\ndescription --version\n",
        }
        self.assertEqual(audit(clean, "clean.md")[0], [], "a well-formed table must not be reported")
        for label, text in planted.items():
            with self.subTest(case=label):
                self.assertTrue(audit(text, "planted.md")[0], f"{label} was not detected")


if __name__ == "__main__":
    unittest.main()
