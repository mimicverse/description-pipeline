"""文档可达性：本地链接与 #锚点必须存在，关键文档必须从 README 可达。"""

import unicodedata
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
# 允许外链与纯锚点；其余按相对路径解析。
EXTERNAL = ("http://", "https://", "mailto:", "codex://")
SKIP_DIRS = {".git", "__pycache__", ".venv", "build", "dist"}
#: The packaged demo is a byte-for-byte copy of ``examples/demo-arm``, which is checked in place;
#: its relative links describe a checkout, not a location inside an installed package.
SKIP_PREFIXES = ("src/description_pipeline/templates/quickstart/",)


def markdown_files() -> list[Path]:
    found = []
    for path in sorted(ROOT.rglob("*.md")):
        relative = path.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if relative.startswith(SKIP_PREFIXES):
            continue
        found.append(path)
    return found


def heading_slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces become hyphens."""

    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", re.sub(r"`([^`]*)`", r"\1", heading.strip().lower()))
    out: list[str] = []
    for char in text:
        if char.isalnum() or char in "-_" or unicodedata.category(char).startswith("L"):
            out.append(char)
        elif char in " \t":
            out.append("-")
    return "".join(out)


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if not match:
            continue
        base = heading_slug(match.group(2))
        index = counts.get(base, 0)
        counts[base] = index + 1
        found.add(base if index == 0 else f"{base}-{index}")
    return found


def broken_links(paths: list[Path]) -> tuple[int, list[str]]:
    """Every local link that does not resolve, with the number of links examined."""

    broken: list[str] = []
    checked = 0
    for path in paths:
        for target in LINK.findall(path.read_text(encoding="utf-8")):
            cleaned = target.split("#", 1)[0].strip()
            if not cleaned or target.startswith(EXTERNAL):
                continue
            checked += 1
            if not (path.parent / cleaned).resolve().exists():
                broken.append(f"{path.relative_to(ROOT)} → {target}")
    return checked, broken


def broken_anchors(paths: list[Path]) -> tuple[int, list[str]]:
    """Every deep link whose fragment is not a heading in the target document."""

    broken: list[str] = []
    checked = 0
    for path in paths:
        for target in LINK.findall(path.read_text(encoding="utf-8")):
            if target.startswith(EXTERNAL) or "#" not in target:
                continue
            file_part, fragment = target.split("#", 1)
            target_path = (path.parent / file_part).resolve() if file_part.strip() else path
            if not target_path.is_file():
                continue
            checked += 1
            if fragment not in anchors(target_path):
                broken.append(f"{path.relative_to(ROOT)} → {target}")
    return checked, broken


class DocumentationLinkTests(unittest.TestCase):
    def test_local_markdown_links_resolve(self):
        checked, broken = broken_links(markdown_files())
        self.assertGreater(checked, 10, "链接收集异常，检查正则")
        self.assertEqual(broken, [], "这些本地链接指向不存在的文件")

    def test_local_markdown_anchors_resolve(self):
        """A deep link that names a renamed section is as broken as a missing file."""

        checked, broken = broken_anchors(markdown_files())
        self.assertGreater(checked, 5, "锚点收集异常，检查正则")
        self.assertEqual(broken, [], "这些锚点在目标文档里不存在")

    def test_the_checks_report_planted_defects(self):
        """Both checks have to fail when the document is wrong, or passing them means nothing."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.md"
            target.write_text("# Real section\n\nText.\n", encoding="utf-8")
            source = root / "source.md"
            source.write_text(
                "[missing file](nowhere.md)\n[missing anchor](target.md#gone)\n[good](target.md#real-section)\n",
                encoding="utf-8",
            )
            # `broken_links` uses `relative_to(ROOT)`, so the control runs on a copy inside a fake root.
            with mock.patch.object(sys.modules[__name__], "ROOT", root):
                _, links = broken_links([source])
                _, anchors_found = broken_anchors([source])
        self.assertEqual(links, ["source.md → nowhere.md"])
        self.assertEqual(anchors_found, ["source.md → target.md#gone"])

    def test_readme_points_at_the_key_documents(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for relative in (
            "docs/engineering_standard.md",
            "docs/urdf_standard.md",
            "docs/onshape_export.md",
            "docs/solidworks_export.md",
            "CONTRIBUTING.md",
            "tools/quality.py",
        ):
            with self.subTest(relative=relative):
                self.assertIn(relative, readme, f"README 没有指向 {relative}")
                self.assertTrue((ROOT / relative).exists(), f"{relative} 不存在")


if __name__ == "__main__":
    unittest.main()
