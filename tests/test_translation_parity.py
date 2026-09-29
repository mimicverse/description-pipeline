"""Both language versions must document the same runnable commands.

The Chinese and English documents are maintained side by side. Prose may be translated, but a
command, flag or path may not: an operator following the English version has to be able to run
exactly what the Chinese version documents, and the other way round. This checks every fenced code
block of every language pair and compares the command lines only.
"""

import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: A runnable line starts with a program name or a shell prompt; comments, diagrams and translated
#: YAML values are intentionally excluded.
COMMAND = re.compile(r"^\s*(?:description|python3?|pip3?|git|gh|curl|powershell|cd|export|\./|& \$Python)\b")


def pairs() -> list[tuple[Path, Path]]:
    found = [(ROOT / "README.md", ROOT / "README.en.md"), (ROOT / "CONTRIBUTING.md", ROOT / "CONTRIBUTING.en.md")]
    for chinese in sorted((ROOT / "docs").rglob("*.md")):
        if chinese.name.endswith(".en.md"):
            continue
        english = chinese.with_name(chinese.stem + ".en.md")
        if english.is_file():
            found.append((chinese, english))
    return [(chinese, english) for chinese, english in found if chinese.is_file() and english.is_file()]


def command_lines(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    lines: set[str] = set()
    for _language, body in re.findall(r"```(\w*)\n(.*?)```", text, re.S):
        for raw in body.splitlines():
            line = re.split(r"\s{2,}#|\t+#", raw.rstrip(), maxsplit=1)[0].rstrip()
            if line.strip() and not line.lstrip().startswith("#") and COMMAND.match(line):
                lines.add(line)
    return lines


def parity_problems(chinese: Path, english: Path) -> list[str]:
    """Commands one side documents and the other does not."""

    left, right = command_lines(chinese), command_lines(english)
    problems = [f"missing from the English version: {line}" for line in sorted(left - right)]
    problems += [f"missing from the Chinese version: {line}" for line in sorted(right - left)]
    return problems


class TranslationParityTests(unittest.TestCase):
    def test_language_pairs_document_the_same_commands(self):
        self.assertGreaterEqual(len(pairs()), 8, "language pairs should be discovered automatically")
        for chinese, english in pairs():
            with self.subTest(document=chinese.relative_to(ROOT).as_posix()):
                self.assertEqual(parity_problems(chinese, english), [], "两种语言记录的命令必须一致")

    def test_a_one_sided_command_is_reported(self):
        """The comparison has to fail when the pair differs, or the sweep above proves nothing."""

        with tempfile.TemporaryDirectory() as temporary:
            chinese = Path(temporary) / "doc.md"
            english = Path(temporary) / "doc.en.md"
            chinese.write_text("```sh\ndescription doctor\n```\n", encoding="utf-8")
            english.write_text("```sh\ndescription doctor\ndescription build --root .\n```\n", encoding="utf-8")
            self.assertEqual(
                parity_problems(chinese, english),
                ["missing from the Chinese version: description build --root ."],
            )
            english.write_text("```sh\ndescription doctor\n```\n", encoding="utf-8")
            self.assertEqual(parity_problems(chinese, english), [])


if __name__ == "__main__":
    unittest.main()
